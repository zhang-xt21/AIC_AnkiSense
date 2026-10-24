"use strict";

// Real local APIs and background queue; a deterministic model stub never uses
// personal lectures, credentials or external API quota.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");
const { project, pythonExecutable, browserOptions } = require("./browser_runtime.cjs");
const navigation = require("./browser_navigation.cjs");
const base = `http://127.0.0.1:${process.env.ANKISENSE_UI_PORT || 8976}`;
const runDir = path.join(project, ".ui-checks", "20260927-workbench", `auto-learning-${new Date().toISOString().replace(/[:.]/g, "-")}`);
fs.mkdirSync(runDir, { recursive: true });
const failures = [], external = [], proofRequests = [], reviewPosts = [];
let browser, server, page;
async function state() { return (await fetch(`${base}/api/state`)).json(); }
async function check(name, work) { await work(); console.log(`PASS ${name}`); }
async function screenshot(name) {
  await page.locator("#toast").waitFor({ state: "hidden", timeout: 10000 });
  await page.evaluate(() => window.scrollTo({ top: 0, left: 0, behavior: "instant" }));
  await page.screenshot({ path: path.join(runDir, `${name}.png`), fullPage: true });
}
async function select(id) {
  await page.getByLabel("选择编辑卡片", { exact: true }).selectOption(id);
}
async function waitReview(id, status) {
  await page.waitForFunction(({ id, status }) => app.data.cards.find(card => card.id === id)?.review_state === status, { id, status }, { timeout: 30000 });
}

(async () => {
  try { await fetch(`${base}/health`, { signal: AbortSignal.timeout(500) }); throw new Error("Test port 8976 is already in use."); }
  catch (error) { if (!["TypeError", "TimeoutError"].includes(error.name)) throw error; }
  const log = fs.openSync(path.join(runDir, "server.log"), "a");
  server = spawn(pythonExecutable(), ["-X", "utf8", path.join(project, "tools", "auto_learning_fixture.py")], {
    cwd: project, windowsHide: true, env: { ...process.env, ANKISENSE_UI_DATA: path.join(runDir, "data") }, stdio: ["ignore", log, log],
  });
  let ready = false;
  for (let i = 0; i < 240; i++) {
    if (server.exitCode !== null) throw new Error(`Fixture exited: ${server.exitCode}; ${runDir}/server.log`);
    try { if ((await fetch(`${base}/health`)).ok) { ready = true; break; } } catch {}
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  assert.ok(ready, `Fixture failed to start; ${runDir}/server.log`);
  const initial = await state();
  const source = initial.source_pages[0];
  assert.equal(source.status, "pending");
  const lecture = initial.documents.find(doc => doc.id === source.document_id);
  browser = await chromium.launch(browserOptions());
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: "block" });
  await context.route("**/*", route => {
    const url = route.request().url();
    if (url.startsWith(`${base}/`) || url.startsWith("data:")) return route.continue();
    external.push(url); return route.abort();
  });
  page = await context.newPage();
  page.on("pageerror", error => failures.push(error.message));
  page.on("request", request => {
    if (/\/pages\/\d+(?:\/ai-proofread)?$/.test(request.url()) && request.method() !== "GET") proofRequests.push(request.url());
    if (request.url().endsWith("/auto-review") && request.method() === "POST") reviewPosts.push(request.url());
  });
  await page.goto(base);
  await navigation.openMaterials(page);
  let generated, pass;
  await check("Pending PDF source generates and starts review without confirmation or extra review POST", async () => {
    await page.locator(".document-row").filter({ hasText: lecture.title }).click();
    assert.deepEqual(proofRequests, [], "Advanced tools never trigger proofreading without a request");
    assert.equal(await page.getByLabel("仅使用勾选的原文片段", { exact: true }).isChecked(), false);
    await page.getByLabel("制卡数量", { exact: true }).fill("3");
    const response = page.waitForResponse(r => r.url() === `${base}/api/generation-jobs` && r.request().method() === "POST");
    await page.getByRole("button", { name: "开始制卡 →", exact: true }).click();
    const result = await response;
    assert.equal(result.status(), 200, await result.text());
    const job = await result.json();
    await page.waitForFunction(id => (app.data.generation_jobs || []).find(job => job.id === id)?.card_ids?.length > 0, job.id);
    const latest = await state();
    const ids = latest.generation_jobs.find(item => item.id === job.id).card_ids;
    generated = latest.cards.filter(card => ids.includes(card.id));
    pass = generated.find(card => card.question.startsWith("Pass:"));
    assert.ok(pass);
    assert.ok(["queued", "running"].includes(pass.review_state));
    assert.equal(pass.status, "draft");
    await page.getByRole("button", { name: "查看已生成卡片", exact: true }).click();
    await page.waitForFunction(id => document.querySelector(".workbench-grid")?.dataset.cardId === id, pass.id);
    assert.deepEqual(proofRequests, []);
    assert.deepEqual(reviewPosts, []);
  });

  await check("Background completion preserves unsaved input, input focus and saved version", async () => {
    const question = page.locator(".workbench-editor").getByLabel("问题", { exact: true });
    const handle = await question.elementHandle();
    await question.fill(`${pass.question} Unsaved edit.`);
    await waitReview(pass.id, "auto_pass");
    assert.equal(await question.inputValue(), `${pass.question} Unsaved edit.`);
    assert.ok(await handle.evaluate(node => node.isConnected && document.activeElement === node), "Review must not replace or blur the focused input");
    const saved = (await state()).cards.find(card => card.id === pass.id);
    assert.equal(saved.status, "approved");
    assert.equal(saved.approval.method, "machine");
    assert.equal(saved.version, pass.version, "Approval must not increment the content version");
    assert.equal(saved.question, pass.question, "Unsubmitted text must not enter saved model review");
    assert.match(await page.locator(".card-auto-review-status").innerText(), /未保存|先保存/);
    await handle.dispose();
  });

  await check("Saving edits clears the old approval and automatically checks the new version", async () => {
    const response = page.waitForResponse(r => r.url() === `${base}/api/cards/${pass.id}` && r.request().method() === "PUT");
    await page.getByRole("button", { name: "保存并检查", exact: true }).click();
    const saved = await (await response).json();
    assert.equal(saved.status, "draft");
    assert.ok(!saved.approval, "Old machine approval must be cleared before review finishes");
    assert.ok(saved.version > pass.version);
    await waitReview(pass.id, "auto_pass");
    await page.locator(".card-auto-review-status").filter({ hasText: "机器审核通过，可开始学习" }).waitFor();
    await screenshot("machine-approved-desktop");
    await page.getByRole("button", { name: "开始学习 →", exact: true }).click();
    await page.locator("#practice-question").waitFor();
    assert.match(await page.locator("#practice-question").innerText(), /Unsaved edit/);
    assert.deepEqual(proofRequests, [], "No page save or proofreading was required");
    assert.deepEqual(reviewPosts, [], "Saving must queue review on the server without extra UI POSTs");
  });

  await check("Uncertain and failed reviews retain drafts and are actionable without losing content", async () => {
    await page.locator('button[data-view="materials"]').click();
    await navigation.stage(page, "audit");
    const uncertain = generated.find(card => card.question.startsWith("Needs evidence"));
    await select(uncertain.id);
    await page.locator(".card-auto-review-status").filter({ hasText: "待处理" }).waitFor();
    assert.equal(await page.getByRole("button", { name: "开始学习 →", exact: true }).count(), 0);
    assert.match(await page.locator(".card-auto-review-detail").innerText(), /条件需要补充/);
    assert.ok(await page.getByText("自行核对并处理", { exact: true }).isVisible());
    const failed = generated.find(card => card.question.startsWith("Offline failure"));
    await select(failed.id);
    const retry = page.getByRole("button", { name: "重试自动检查", exact: true });
    await retry.waitFor();
    assert.equal(await retry.isEnabled(), true);
    assert.match(await page.locator(".card-auto-review-detail").innerText(), /超时/);
    const latest = await state();
    for (const id of [uncertain.id, failed.id]) {
      const card = latest.cards.find(item => item.id === id);
      assert.equal(card.status, "draft");
      assert.ok(card.answer && card.evidence.length);
    }
    await screenshot("needs-attention-desktop");
  });

  await check("Export and empty practice refresh automatically when a background review finishes", async () => {
    for (const view of ["export", "practice"]) {
      await page.locator('button[data-view="materials"]').click();
      await navigation.stage(page, "card");
      await select(pass.id);
      await page.locator(".workbench-editor").getByLabel("问题", { exact: true }).fill(`${pass.question} Refresh ${view}.`);
      const response = page.waitForResponse(r => r.url() === `${base}/api/cards/${pass.id}` && r.request().method() === "PUT");
      await page.getByRole("button", { name: "保存并检查", exact: true }).click();
      assert.equal((await (await response).json()).status, "draft");
      await page.locator(`button[data-view="${view}"]`).click();
      if (view === "export") assert.equal(await page.getByRole("button", { name: "暂无已批准卡片", exact: true }).isDisabled(), true);
      else await page.getByText("还没有可学习的卡片", { exact: true }).waitFor();
      await waitReview(pass.id, "auto_pass");
      if (view === "export") {
        const download = page.getByRole("button", { name: /下载 1 张卡片 · APKG/ });
        await download.waitFor();
        assert.equal(await download.isEnabled(), true);
      } else {
        await page.locator("#practice-question").waitFor();
        assert.match(await page.locator("#practice-question").innerText(), /Refresh practice/);
      }
    }
    await page.locator('button[data-view="materials"]').click();
    await navigation.stage(page, "audit");
    await select(generated.find(card => card.question.startsWith("Offline failure")).id);
  });

  await check("Automatic review and optional manual controls fit a narrow screen", async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("button", { name: "编辑内容", exact: true }).click();
    await page.getByText("自行核对并处理", { exact: true }).click();
    const widths = await page.evaluate(() => ({ viewport: document.documentElement.clientWidth, content: document.documentElement.scrollWidth }));
    assert.ok(widths.content <= widths.viewport + 1, JSON.stringify(widths));
    await screenshot("needs-attention-mobile");
    assert.deepEqual(external, []);
    assert.deepEqual(failures, []);
  });
  console.log(`Screenshots: ${runDir}`);
})().catch(async error => {
  process.exitCode = 1; console.error(error.stack || error);
  if (page) try { await page.screenshot({ path: path.join(runDir, "failure.png"), fullPage: true }); } catch {}
}).finally(async () => {
  if (browser) await browser.close();
  if (server && server.exitCode === null) server.kill();
});
