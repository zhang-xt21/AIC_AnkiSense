"use strict";

// Uses the real local API and a fresh, isolated SQLite directory for each run.
// Never connects to a personal browser, production store or a cloud model.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");
const { project, pythonExecutable, browserOptions } = require("./browser_runtime.cjs");
const output = path.join(project, ".ui-checks", "20260927-workbench");
const runId = new Date().toISOString().replace(/[:.]/g, "-");
const runDir = path.join(output, `run-${runId}`);
const base = "http://127.0.0.1:8976";
const png = path.join(runDir, "stack-fixture.png");
const results = [];
const errors = [];
const consoleErrors = [];
const blockedRequests = [];
const confirmations = [];
let browser, server, page;
fs.mkdirSync(runDir, { recursive: true });

function payload(card, overrides = {}) {
  return Object.assign(Object.fromEntries(["question", "answer", "concept", "kind", "rubric", "evidence", "images", "status"].filter(key => key in card).map(key => [key, card[key]])), { expected_version: card.version }, overrides);
}
async function api(url, options = {}) {
  const response = await fetch(base + url, options);
  const body = await response.json();
  assert.ok(response.ok, `${url}: ${response.status} ${JSON.stringify(body)}`);
  return body;
}
const json = body => ({ headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
async function check(name, fn) {
  const started = Date.now();
  try { await fn(); results.push({ name, status: "passed", elapsed_ms: Date.now() - started }); console.log(`PASS ${name}`); }
  catch (error) { results.push({ name, status: "failed", error: error.message, elapsed_ms: Date.now() - started }); throw error; }
}
async function screenshot(name) {
  await page.locator("#toast").waitFor({ state: "hidden", timeout: 10000 });
  // Capture from the top: full-page captures taken after auto-scrolling can
  // otherwise place offscreen fixed accessibility links inside the image.
  await page.evaluate(() => window.scrollTo({ top: 0, left: 0, behavior: "instant" }));
  await page.screenshot({ path: path.join(runDir, `${name}.png`), fullPage: true });
  const layout = await page.evaluate(() => {
    const link = document.querySelector(".skip-link");
    const style = getComputedStyle(link);
    const box = link.getBoundingClientRect();
    return { active: document.activeElement?.outerHTML.slice(0, 300), scrollY, skipLink: { top: box.top, bottom: box.bottom, transform: style.transform, position: style.position } };
  });
  fs.writeFileSync(path.join(runDir, `${name}-layout.json`), JSON.stringify(layout, null, 2));
}
async function review(cardId = "demo-stack") {
  if (await page.locator('[data-studio-stage="card"]').count() === 0) {
    await page.locator('button[data-view="materials"]').click();
  }
  await page.locator('[data-studio-stage="card"]').click();
  await page.getByLabel("选择编辑卡片", { exact: true }).selectOption(cardId);
  await page.locator(".workbench-editor").waitFor();
}
function editor() { return page.locator(".workbench-editor"); }
async function previewSide(side) { await page.locator(".workbench-preview").getByRole("button", { name: side, exact: true }).click(); }
async function save(label = "保存并检查", expected = 200) {
  const response = page.waitForResponse(r => r.url().startsWith(`${base}/api/cards/`) && r.request().method() === "PUT");
  await editor().getByRole("button", { name: label, exact: true }).click();
  const received = await response;
  assert.equal(received.status(), expected, await received.text());
  await page.waitForFunction(() => !document.querySelector(".workbench-editor textarea")?.disabled);
  return received.json();
}
async function getCard(id = "demo-stack") { return (await api("/api/state")).cards.find(card => card.id === id); }
async function hasPreviewImage(expected) {
  await page.waitForFunction(count => document.querySelectorAll("#card-live-preview img").length === count, expected);
}

(async () => {
  // Refuse to reuse an unknown service accidentally listening on our test port.
  try { const response = await fetch(`${base}/health`, { signal: AbortSignal.timeout(500) }); if (response) throw new Error("TEST_PORT_IN_USE"); }
  catch (error) { if (error.message === "TEST_PORT_IN_USE") throw new Error("Port 8976 is already in use; close only the previous owned test fixture before rerunning."); }
  const log = fs.openSync(path.join(runDir, "server.log"), "a");
  server = spawn(pythonExecutable(), ["-X", "utf8", path.join(project, "tools", "workbench_fixture.py")], {
    cwd: project, windowsHide: true, env: { ...process.env, ANKISENSE_UI_DATA: path.join(runDir, "data") }, stdio: ["ignore", log, log],
  });
  server.on("error", error => errors.push(`server: ${error.message}`));
  let ready = false;
  for (let i = 0; i < 100; i++) {
    if (server.exitCode !== null) throw new Error(`Fixture server exited (${server.exitCode}); see server.log`);
    try { const result = await api("/health"); if (result.mode === "demo") { ready = true; break; } } catch {}
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  assert.ok(ready, "Isolated server did not become ready");
  await api("/api/demo", { method: "POST" });
  await api("/api/documents/text", { method: "POST", ...json({ title: "另一门课程 · 筛选测试", text: "这是独立的工作台筛选测试材料。它没有学习卡片，用于验证按课程筛选时的空状态，不应显示其他材料的卡片。" }) });
  const initial = await getCard();
  browser = await chromium.launch(browserOptions());
  const context = await browser.newContext({ viewport: { width: 1600, height: 1050 }, deviceScaleFactor: 1 });
  await context.route("**/*", route => {
    const url = route.request().url();
    if (url.startsWith(`${base}/`) || url.startsWith("data:")) return route.continue();
    blockedRequests.push(url); return route.abort();
  });
  page = await context.newPage();
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", msg => { if (msg.type() === "error" && !msg.text().includes("409")) consoleErrors.push(msg.text()); });
  page.on("dialog", dialog => { if (dialog.type() === "confirm") confirmations.push(dialog.message()); return dialog.accept(); });
  await page.goto(base);

  await check("Three-column workspace and live text preview preserve input focus", async () => {
    await review();
    assert.equal(await page.locator(".workbench-source").count(), 1);
    assert.equal(await page.locator(".workbench-editor").count(), 1);
    assert.equal(await page.locator(".workbench-preview").count(), 1);
    const question = editor().getByLabel("问题", { exact: true });
    await question.fill("栈的出入顺序规则是什么？（编辑中）");
    await page.waitForFunction(() => document.querySelector("#card-live-preview")?.textContent.includes("（编辑中）"));
    assert.ok(await question.evaluate(node => document.activeElement === node), "Editing must not rebuild and blur the input");
    await screenshot("01-workbench-desktop");
  });

  await check("Switching cards retains unsaved text", async () => {
    await page.getByLabel("选择编辑卡片", { exact: true }).selectOption("demo-queue");
    await page.getByLabel("选择编辑卡片", { exact: true }).selectOption("demo-stack");
    assert.equal(await editor().getByLabel("问题", { exact: true }).inputValue(), "栈的出入顺序规则是什么？（编辑中）");
    await editor().getByLabel("问题", { exact: true }).fill(initial.question);
  });

  await check("Uploading an image keeps it unsaved and defaults to the answer side", async () => {
    await editor().getByLabel("上传讲义截图或图片", { exact: true }).setInputFiles(png);
    await editor().locator(".card-image-item").waitFor();
    assert.equal(await editor().getByLabel("第 1 张配图显示位置", { exact: true }).inputValue(), "answer");
    assert.equal((await getCard()).images?.length || 0, 0);
    await previewSide("正面"); await hasPreviewImage(0);
    await previewSide("背面"); await hasPreviewImage(1);
    const image = page.locator("#card-live-preview img");
    await image.waitFor();
    await page.waitForFunction(() => [...document.querySelectorAll("#card-live-preview img")].every(node => node.complete && node.naturalWidth > 0));
    await screenshot("02-answer-image-preview");
  });

  await check("Image side and caption edits are reflected immediately in both previews", async () => {
    await editor().getByLabel("第 1 张配图显示位置", { exact: true }).selectOption("question");
    await previewSide("正面"); await hasPreviewImage(1);
    await editor().getByLabel("第 1 张配图说明", { exact: true }).fill("栈示意图 · 测试说明");
    await page.waitForFunction(() => document.querySelector("#card-live-preview")?.textContent.includes("栈示意图 · 测试说明"));
    await editor().getByLabel("第 1 张配图显示位置", { exact: true }).selectOption("answer");
    await hasPreviewImage(0);
    await previewSide("背面"); await hasPreviewImage(1);
  });

  await check("Typing updates an illustrated preview while preserving the image node and input focus", async () => {
    await previewSide("背面"); await hasPreviewImage(1);
    const originalImage = await page.locator("#card-live-preview img").elementHandle();
    const question = editor().getByLabel("问题", { exact: true });
    const before = await question.inputValue();
    const edited = `${before}（配图实时预览测试）`;
    await question.fill(edited);
    await page.waitForFunction(value => document.querySelector("#card-live-preview .preview-question")?.textContent === value, edited);
    assert.ok(await originalImage.evaluate(node => node.isConnected && node === document.querySelector("#card-live-preview img")), "Text editing must reuse the existing image DOM node");
    assert.ok(await question.evaluate(node => document.activeElement === node), "Live preview updates must preserve the editor focus");
    await question.fill(before);
    await page.waitForFunction(value => document.querySelector("#card-live-preview .preview-question")?.textContent === value, before);
    assert.ok(await originalImage.evaluate(node => node.isConnected && node === document.querySelector("#card-live-preview img")), "Restoring text must still preserve the image DOM node");
    await originalImage.dispose();
  });

  await check("Saving and refreshing persists text, image side, caption and version", async () => {
    const saved = await save();
    assert.equal(saved.status, "draft"); assert.ok(saved.version > initial.version);
    assert.equal(saved.images[0].side, "answer"); assert.equal(saved.images[0].caption, "栈示意图 · 测试说明");
    await page.reload(); await review();
    assert.equal(await editor().getByLabel("第 1 张配图说明", { exact: true }).inputValue(), "栈示意图 · 测试说明");
    await previewSide("背面"); await hasPreviewImage(1);
  });

  await check("Approval enables practice without exposing answer-side images before a valid answer", async () => {
    await page.locator('[data-studio-stage="audit"]').click();
    await editor().getByText("自行核对并处理", { exact: true }).click();
    const saved = await save("人工核对并批准"); assert.equal(saved.status, "approved");
    await page.locator('button[data-view="practice"]').click();
    await page.getByLabel("选择练习卡片", { exact: true }).selectOption("demo-stack");
    assert.equal(await page.locator(".practice-panel .card-image-gallery img").count(), 0);
    await page.getByLabel("你的回答", { exact: true }).fill("后进先出");
    const response = page.waitForResponse(r => r.url() === `${base}/api/attempts` && r.request().method() === "POST");
    await page.getByRole("button", { name: "提交回答并分析", exact: false }).click();
    const attempt = await (await response).json();
    assert.equal(attempt.status, "completed"); assert.equal(attempt.diagnosis.verdict, "correct");
    const details = page.locator("details").filter({ has: page.locator("summary", { hasText: "查看已审核的参考答案" }) });
    await details.locator("summary").click();
    await details.locator("img").waitFor();
    await screenshot("03-practice-answer-image");
  });

  await check("A stale editor receives HTTP 409 and retains the unsaved draft", async () => {
    await review();
    await editor().getByLabel("问题", { exact: true }).fill("本标签页尚未保存的题目");
    const fresh = await getCard();
    const updated = await api("/api/cards/demo-stack", { method: "PUT", ...json(payload(fresh, { question: "另一标签页保存的新题目", status: "draft" })) });
    await save("保存并检查", 409);
    assert.equal(await editor().getByLabel("问题", { exact: true }).inputValue(), "本标签页尚未保存的题目");
    assert.equal((await getCard()).question, updated.question);
    assert.match(await editor().locator(".inline-error").innerText(), /更新|版本|刷新/);
    await screenshot("04-version-conflict-preserved");
    const before = confirmations.length;
    await editor().getByRole("button", { name: "载入最新版本", exact: true }).click();
    await page.waitForFunction(() => document.querySelector(".workbench-editor textarea")?.value === "另一标签页保存的新题目");
    assert.equal(confirmations.length, before + 1, "Replacing unsaved edits must request confirmation");
    assert.match(confirmations.at(-1), /替换.*未保存/);
    assert.equal(await editor().getByRole("button", { name: "载入最新版本", exact: true }).count(), 0);
  });

  await check("A real PDF renders in the source pane and supports full-page zoom and page navigation", async () => {
    await review("fixture-pdf-stack");
    const source = page.locator(".workbench-source");
    await page.waitForFunction(() => { const image = document.querySelector(".workbench-page-image img"); return image?.complete && image.naturalWidth > 0; });
    assert.match(await source.locator(".workbench-page-image img").getAttribute("src"), /\/pages\/1\/image$/);
    await source.getByRole("button", { name: "放大讲义第 1 页", exact: true }).click();
    const dialog = page.locator("dialog.proof-preview");
    await dialog.waitFor();
    await page.waitForFunction(() => { const image = document.querySelector("dialog.proof-preview img"); return image?.complete && image.naturalWidth > 0; });
    await dialog.getByRole("button", { name: "放大原图", exact: true }).click();
    assert.ok(await dialog.locator(".proof-preview-viewport").evaluate(node => node.classList.contains("is-zoomed")));
    await dialog.getByRole("button", { name: "关闭预览", exact: true }).click();
    await source.getByLabel("查看讲义页", { exact: true }).selectOption("2");
    await page.waitForFunction(() => { const image = document.querySelector(".workbench-page-image img"); return image?.src.endsWith("/pages/2/image") && image.complete && image.naturalWidth > 0; });
    await previewSide("背面"); await hasPreviewImage(1);
    await screenshot("06-pdf-source-and-card-image");
  });

  await check("Image-side changes preserve editor scroll position and multiline rubric edits save intact", async () => {
    const side = editor().getByLabel("第 1 张配图显示位置", { exact: true });
    await side.scrollIntoViewIfNeeded();
    const before = await page.locator(".workbench-editor > .panel-body").evaluate(node => node.scrollTop);
    assert.ok(before > 0, "Fixture must exercise a scrolled editor");
    await side.selectOption("question");
    const after = await page.locator(".workbench-editor > .panel-body").evaluate(node => node.scrollTop);
    assert.ok(Math.abs(after - before) <= 2, `Editor scrolled from ${before} to ${after}`);
    await side.selectOption("answer");
    const rubric = "Explain last in, first out.\nIdentify the last inserted element as the first item removed.\nUse the stack top consistently in the explanation.";
    await editor().getByLabel("评分要点 1", { exact: true }).fill(rubric);
    const saved = await save();
    assert.equal(saved.rubric[0].text, rubric);
    assert.equal(saved.rubric[0].point_id, "fixture_lifo");
  });

  await check("Adding and editing a source citation persists and updates the preview source pages", async () => {
    const source = page.locator(".workbench-source");
    await source.getByLabel("查看讲义页", { exact: true }).selectOption("2");
    await source.getByRole("button", { name: "加入卡片引用", exact: true }).click();
    const quote = "The first pop returns C.";
    await editor().getByLabel("原文引用 2", { exact: true }).fill(quote);
    const saved = await save();
    assert.equal(saved.evidence.length, 2);
    assert.equal(saved.evidence[1].quote, quote);
    await previewSide("背面");
    const sourceLabel = await page.locator("#card-live-preview .preview-source-label").innerText();
    assert.match(sourceLabel, /第 1 页/); assert.match(sourceLabel, /第 2 页/);
    await source.getByLabel("查看讲义页", { exact: true }).selectOption("2");
    await screenshot("07-pdf-edited-evidence-preview");
  });

  await check("Workspace has no horizontal overflow at desktop, tablet and phone widths", async () => {
    for (const width of [1600, 1280, 1050, 736, 390, 320]) {
      await page.setViewportSize({ width, height: 1050 });
      const panes = width <= 980 ? [["source", "查看讲义"], ["editor", "编辑内容"], ["preview", "卡片预览"]] : [["desktop", null]];
      for (const [pane, label] of panes) {
        if (label) {
          await page.getByRole("button", { name: label, exact: true }).click();
          assert.ok(await page.locator(`.workbench-${pane}`).isVisible(), `${pane} should be visible at ${width}`);
        }
        const sizes = await page.evaluate(() => ({ viewport: document.documentElement.clientWidth, content: document.documentElement.scrollWidth }));
        assert.ok(sizes.content <= sizes.viewport + 1, `Width ${width}, ${pane} overflows: ${JSON.stringify(sizes)}`);
        await screenshot(`05-workbench-${width}-${pane}`);
      }
    }
  });
  await check("No browser exceptions or external requests", async () => {
    assert.deepEqual(errors, []); assert.deepEqual(blockedRequests, []);
  });
})().catch(async error => {
  errors.push(error.stack || String(error));
  console.error(error.stack || error);
  if (page) { try { await screenshot("failure"); } catch {} }
  process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close();
  if (server && server.exitCode === null) server.kill();
  const report = { runId, offline: true, baseUrl: base, results, errors, consoleErrors, blockedRequests, confirmations, directory: runDir };
  fs.writeFileSync(path.join(runDir, "report.json"), JSON.stringify(report, null, 2));
  fs.writeFileSync(path.join(output, "latest-report.json"), JSON.stringify(report, null, 2));
  console.log(`Report: ${path.join(runDir, "report.json")}`);
});
