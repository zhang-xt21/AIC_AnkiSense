"use strict";

// Offline browser verification using only repository assets and self-authored
// fixtures. No server, personal database, browser profile or model API is used.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const playwright = require("playwright");
const { project, browserOptions } = require("./browser_runtime.cjs");
const assets = path.join(project, "ankisense", "static");
const output = path.join(project, ".ui-checks", "20260927-feedback");
const results = [], errors = [], requests = [];
fs.mkdirSync(output, { recursive: true });
const read = file => fs.readFileSync(path.join(assets, file), "utf8");
const originalHtml = read("index.html");
const assetPath = url => url.split("?")[0].replace(/^\/static\//, "");
const styles = [...originalHtml.matchAll(/<link\b[^>]*rel="stylesheet"[^>]*href="([^"]+)"[^>]*>/g)].map(match => assetPath(match[1]));
const scripts = [...originalHtml.matchAll(/<script\b[^>]*src="([^"]+)"[^>]*><\/script>/g)].map(match => assetPath(match[1]));
const html = originalHtml.replace(/<link\b[^>]*>/g, "").replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, "");

function fixture(kind) {
  const card = {
    id: "offline-stack", document_id: "offline-notes", version: 1, status: "approved", is_demo: false,
    kind: "basic", concept: "栈的操作", images: [],
    question: "栈的插入和删除在哪一端进行？栈遵循什么顺序？",
    answer: "都在栈顶进行，遵循后进先出。",
    rubric: [{ point_id: "stack_top", text: "指出插入和删除都在栈顶进行。" }, { point_id: "stack_lifo", text: "说明后进先出的顺序。" }],
    evidence: [{ block_id: "offline-source", quote: "栈的插入和删除都在栈顶进行，遵循后进先出。" }],
  };
  const diagnosis = {
    verdict: kind === "uncertain" ? "uncertain" : "incomplete",
    missing_point_ids: kind === "uncertain" ? [] : kind === "partial" ? ["stack_top"] : ["stack_top", "stack_lifo"],
    contradicted_point_ids: [], source_block_ids: ["offline-source"],
    answer_spans: kind === "partial" ? ["后进先出"] : [],
    reason: kind === "historical" ? "学生只回答了问候语，没有给出栈操作的相关内容。此处保留自编旧版分析结果，供界面回归检查。" : kind === "partial" ? "已说明后进先出，还需补充插入和删除所在的位置。" : "只回答了问候语，无法据此判断对栈操作的理解。",
  };
  if (kind !== "historical") diagnosis.addressed_point_ids = kind === "partial" ? ["stack_lifo"] : [];
  const block = { block_id: "offline-source", document_id: "offline-notes", page: 1, text: card.evidence[0].quote };
  const attempt = {
    id: `offline-${kind}`, card_id: card.id, status: "completed", is_demo: false,
    created_at: "2026-09-27T06:00:00Z", answer: kind === "partial" ? "后进先出" : "你好",
    prompt_version: kind === "historical" ? "v1" : "v2-relevance-evidence",
    card_snapshot: card, source_snapshot: [block], diagnosis,
  };
  return {
    attempt,
    data: {
      mode: "demo", configured: false, model: "offline-fixture", media: [], source_pages: [],
      documents: [{ id: "offline-notes", title: "自编离线测试材料" }], blocks: [block], attempts: [attempt],
      cards: [kind === "historical" ? { ...card, version: 2, status: "draft" } : card],
    },
  };
}

let browser;
(async () => {
  browser = await playwright.chromium.launch(browserOptions());
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1, serviceWorkers: "block" });
  await context.route("**/*", route => { requests.push(route.request().url()); return route.abort(); });
  const page = await context.newPage();
  page.on("pageerror", error => errors.push(error.message));
  await page.setContent(html, { waitUntil: "domcontentloaded" });
  for (const file of styles) await page.addStyleTag({ content: read(file) });
  for (const file of scripts) {
    let content = read(file);
    if (file === "app.js") {
      assert.match(content, /\binitialize\(\);\s*$/);
      content = content.replace(/\binitialize\(\);\s*$/, "");
    }
    await page.addScriptTag({ content });
  }
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const kind of ["historical", "partial", "uncertain"]) {
      const input = fixture(kind);
      const before = JSON.stringify(input.attempt);
      await page.evaluate(input => {
        function freeze(value) {
          if (value && typeof value === "object") { Object.values(value).forEach(freeze); Object.freeze(value); }
          return value;
        }
        app.data = input.data;
        app.attempt = freeze(input.attempt);
        app.practiceId = input.attempt.card_id;
        app.practiceFilter = "all";
        app.answer = input.attempt.answer;
        app.view = "practice";
        render();
        window.scrollTo({ top: 0, left: 0, behavior: "instant" });
      }, input);
      const feedback = page.locator(".feedback-panel");
      await feedback.waitFor({ state: "visible" });
      const content = await feedback.innerText();
      assert.ok(!content.includes("方向对了"));
      if (kind === "historical") {
        assert.match(content, /历史分析 · 待复核/);
        assert.match(content, /原判：还不完整/);
        assert.ok(content.includes(input.attempt.diagnosis.reason));
        assert.equal(await feedback.getByText("历史分析 · 待复核", { exact: true }).isVisible(), true);
        assert.equal(await page.getByLabel("你的回答", { exact: true }).isDisabled(), true);
      } else if (kind === "partial") {
        assert.match(content, /回答已涉及的要点/);
        assert.match(content, /说明后进先出的顺序。/);
        assert.equal(await feedback.getByText("回答已涉及的要点", { exact: true }).isVisible(), true);
        assert.equal(await feedback.getByText("说明后进先出的顺序。", { exact: true }).isVisible(), true);
        assert.doesNotMatch(content, /待复核/);
      } else {
        assert.match(content, /暂时无法判断/);
        assert.doesNotMatch(content, /待复核|回答已涉及的要点|需要补充的要点/);
      }
      const layout = await page.evaluate(() => ({
        viewport: innerWidth, documentWidth: document.documentElement.scrollWidth,
        bodyWidth: document.body.scrollWidth,
        overflowing: [...document.querySelectorAll("main *")].filter(node => {
          const rect = node.getBoundingClientRect();
          return rect.width > 0 && (rect.left < -1 || rect.right > innerWidth + 1);
        }).map(node => ({ tag: node.tagName, className: node.className })),
        attempt: JSON.stringify(app.attempt),
      }));
      assert.ok(layout.documentWidth <= width + 1, `${kind} at ${width}: document overflow ${layout.documentWidth}`);
      assert.ok(layout.bodyWidth <= width + 1, `${kind} at ${width}: body overflow ${layout.bodyWidth}`);
      assert.deepEqual(layout.overflowing, [], `${kind} at ${width}: child overflow`);
      assert.equal(layout.attempt, before, "rendering must not modify the recorded answer or snapshot");
      delete layout.attempt;
      const filename = `${width}-${kind}.png`;
      // Capture the finished feedback fade-in instead of a translucent frame.
      await page.evaluate(() => Promise.all(document.getAnimations().filter(animation => Number.isFinite(animation.effect?.getTiming().iterations)).map(animation => animation.finished.catch(() => {}))));
      layout.feedbackOpacity = await feedback.locator(".feedback-state").evaluate(node => getComputedStyle(node).opacity);
      assert.equal(layout.feedbackOpacity, "1", "the final feedback state must be fully opaque");
      await page.screenshot({ path: path.join(output, filename), fullPage: true, animations: "disabled" });
      results.push({ width, fixture: kind, status: "passed", layout, screenshot: filename });
      console.log(`PASS ${width}px ${kind}`);
    }
  }
  assert.deepEqual(requests, [], "offline rendering must not attempt any network request");
  assert.deepEqual(errors, [], "no browser script errors");
})().catch(error => {
  errors.push(error.stack || String(error));
  process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close();
  fs.writeFileSync(path.join(output, "results.json"), JSON.stringify({ created_at: new Date().toISOString(), offline: true, results, errors, blocked_requests: requests }, null, 2));
  console.log(JSON.stringify({ checks: results.length, errors, blocked_requests: requests, output }, null, 2));
});
