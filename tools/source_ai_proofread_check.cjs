"use strict";

// Offline UI check: the fixture serves real PDF pages, while this browser
// supplies fake model suggestions. No cloud service or personal data is used.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");
const { project, pythonExecutable, browserOptions } = require("./browser_runtime.cjs");

const base = "http://127.0.0.1:8976";
const runDir = path.join(project, ".ui-checks", "20260927-workbench", `ai-proof-${new Date().toISOString().replace(/[:.]/g, "-")}`);
fs.mkdirSync(runDir, { recursive: true });
let server;
let browser;
let page;

async function startFixture() {
  try {
    await fetch(`${base}/health`, { signal: AbortSignal.timeout(500) });
    throw new Error("Port 8976 is in use; stop the prior isolated UI fixture before this check.");
  } catch (error) {
    if (!["TimeoutError", "TypeError"].includes(error.name)) throw error;
  }
  const log = fs.openSync(path.join(runDir, "server.log"), "a");
  server = spawn(pythonExecutable(), ["-X", "utf8", path.join(project, "tools", "workbench_fixture.py")], {
    cwd: project,
    windowsHide: true,
    env: { ...process.env, ANKISENSE_UI_DATA: path.join(runDir, "data") },
    stdio: ["ignore", log, log],
  });
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (server.exitCode !== null) throw new Error(`Fixture exited (${server.exitCode}); see ${path.join(runDir, "server.log")}`);
    try {
      const response = await fetch(`${base}/health`);
      if (response.ok) return;
    } catch { /* Startup is still in progress. */ }
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  throw new Error(`Fixture did not start; see ${path.join(runDir, "server.log")}`);
}

(async () => {
  await startFixture();
  const state = await (await fetch(`${base}/api/state`)).json();
  const doc = state.documents.find(item => state.source_pages.some(source => source.document_id === item.id));
  assert.ok(doc, "The isolated fixture must contain a PDF document");
  const pages = state.source_pages.filter(item => item.document_id === doc.id).sort((a, b) => a.page - b.page);
  assert.equal(pages.length, 2);
  const proposals = [{ id: "stale-proposal", document_id: doc.id, page: pages[1].page, revision: pages[1].revision - 1,
    suggested_text: "stale text", issues: [], uncertain: false, model: "fixture-vision", created_at: new Date().toISOString(), cached: false }];
  const calls = [];
  const external = [];
  let putCount = 0;
  let failSecondPage = true;
  browser = await chromium.launch(browserOptions());
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: "block" });
  await context.route("**/*", route => {
    const url = route.request().url();
    if (url.startsWith(`${base}/`) || url.startsWith("data:")) return route.continue();
    external.push(url);
    return route.abort();
  });
  page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/api/state", async route => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.mode = "cloud";
    payload.configured = true;
    payload.model = "fixture-vision";
    payload.source_proposals = proposals;
    for (const item of payload.source_pages) if (item.document_id === doc.id) {
      item.status = "pending";
      item.reviewed_at = null;
      item.quality = { needs_attention: item.page === pages[1].page, issues: item.page === pages[1].page ? [{ code: "numbering_gap", message: "自编测试：分项编号可能缺失。" }] : [] };
    }
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(payload) });
  });
  await page.route(/\/api\/documents\/[^/]+\/pages\/\d+\/ai-proofread$/, route => {
    const request = route.request();
    const number = Number(request.url().match(/\/pages\/(\d+)\/ai-proofread$/)[1]);
    const revision = request.postDataJSON().revision;
    calls.push({ page: number, revision });
    if (number === pages[1].page && failSecondPage) {
      failSecondPage = false;
      return route.fulfill({ status: 502, contentType: "application/json", body: JSON.stringify({ detail: "模拟视觉模型失败" }) });
    }
    const current = pages.find(item => item.page === number);
    const proposal = { id: `mock-${number}`, document_id: doc.id, page: number, revision,
      suggested_text: `${current.text}\nAI 建议核对公式和符号。`, issues: ["请检查公式和符号"], uncertain: true,
      model: "fixture-vision", created_at: new Date().toISOString(), cached: calls.filter(item => item.page === number).length > 1 };
    proposals.push(proposal);
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(proposal) });
  });
  await page.route(/\/api\/documents\/[^/]+\/pages\/\d+$/, route => {
    if (route.request().method() === "PUT") putCount += 1;
    return route.continue();
  });
  await page.goto(base);
  const row = page.locator(".document-row").filter({ hasText: doc.title });
  await row.click();
  assert.equal(await page.locator("#source-proofreader").count(), 0, "Pending pages must not force the checking area open");
  assert.equal(await page.getByRole("button", { name: /AI 检查未人工核对的/ }).count(), 0, "The slow batch action stays inside optional source checking");
  await page.getByRole("button", { name: "检查原文", exact: true }).click();
  await page.locator("#source-proofreader").waitFor();
  assert.equal(await page.locator("#proof-page-selector").inputValue(), String(pages[0].page));
  assert.match(await page.locator(".studio-ai-disclosure").innerText(), /原页图片和识别文字.*2 页.*不自动重试/);
  await page.locator("#proof-page-selector").selectOption(String(pages[1].page));
  assert.equal(await page.locator("#source-ai-proposal").count(), 0, "A proposal from an old source revision must stay hidden");
  process.stdout.write("PASS stale proposals are hidden and the cloud scope is disclosed\n");

  await page.locator("#proof-page-selector").selectOption(String(pages[0].page));
  const input = page.getByLabel(`第 ${pages[0].page} 页校对文字`, { exact: true });
  const draftText = `${pages[0].text}\nUncommitted human edit.`;
  await input.fill(draftText);
  await page.getByRole("button", { name: "AI 校对当前页" }).click();
  await page.locator("#source-ai-proposal").waitFor();
  assert.equal(await page.getByLabel(`第 ${pages[0].page} 页校对文字`, { exact: true }).inputValue(), draftText);
  assert.equal(await page.getByRole("button", { name: "将建议填入编辑框" }).isDisabled(), true);
  assert.equal(await page.locator(".proof-ai-comparison > div").count(), 2);
  assert.equal(putCount, 0, "Generating a suggestion must not confirm a page");
  process.stdout.write("PASS AI suggestions compare text without overwriting an unsaved draft or confirming the page\n");

  await page.getByRole("button", { name: "放弃修改，恢复已保存文字" }).click();
  await page.getByRole("button", { name: "将建议填入编辑框" }).click();
  assert.match(await page.getByLabel(`第 ${pages[0].page} 页校对文字`, { exact: true }).inputValue(), /AI 建议核对公式和符号/);
  assert.match(await page.locator(".proof-draft-status").innerText(), /未保存/);
  assert.equal(putCount, 0);
  process.stdout.write("PASS adopting a suggestion only fills the editor\n");

  await page.getByRole("button", { name: "放弃修改，恢复已保存文字" }).click();
  let confirmation = "";
  page.once("dialog", dialog => { confirmation = dialog.message(); void dialog.accept(); });
  await page.getByRole("button", { name: /AI 检查未人工核对的 2 页/ }).click();
  await page.waitForFunction(() => document.querySelector("#source-ai-batch-status")?.textContent.includes("本轮已完成"));
  assert.match(confirmation, /2 页.*原页图片和识别文字.*每页请求一次.*失败不自动重试/);
  assert.deepEqual(calls.map(item => item.page), [pages[0].page, pages[0].page, pages[1].page]);
  assert.match(await page.locator("#source-ai-batch-status").innerText(), /已处理 2\/2 页.*成功 1.*失败 1/);
  assert.match(await page.locator("#source-ai-batch-status").innerText(), /模拟视觉模型失败/);
  assert.equal(putCount, 0, "A batch must never auto-confirm pages");
  assert.deepEqual(external, []);
  assert.deepEqual(errors, []);
  await page.screenshot({ path: path.join(runDir, "ai-proof-desktop.png"), fullPage: true });
  process.stdout.write("PASS batch requests are sequential, never retried, and failures remain visible\n");
  const beforeSelective = calls.length;
  page.once("dialog", dialog => { confirmation = dialog.message(); void dialog.accept(); });
  await page.getByRole("button", { name: "AI 仅校对疑点页（1）", exact: true }).click();
  await page.waitForFunction(() => document.querySelector("#source-ai-batch-status")?.textContent.includes("已处理 1/1 页"));
  assert.equal(calls.length, beforeSelective + 1);
  assert.equal(calls.at(-1).page, pages[1].page);
  assert.match(confirmation, /1 页/);
  assert.equal(putCount, 0);
  process.stdout.write("PASS selective checking sends only flagged pages and does not save suggestions\n");

  await page.setViewportSize({ width: 390, height: 844 });
  const dimensions = await page.evaluate(() => ({ viewport: document.documentElement.clientWidth, content: document.documentElement.scrollWidth }));
  assert.ok(dimensions.content <= dimensions.viewport + 1, `The page overflows at phone width: ${JSON.stringify(dimensions)}`);
  await page.screenshot({ path: path.join(runDir, "ai-proof-mobile.png"), fullPage: true });
  process.stdout.write("PASS AI proofreading remains usable at phone width\n");
})().catch(async error => {
  process.exitCode = 1;
  process.stderr.write(`${error.stack || error}\n`);
  if (page) try { await page.screenshot({ path: path.join(runDir, "failure.png"), fullPage: true }); } catch { /* Diagnostic only. */ }
}).finally(async () => {
  if (browser) await browser.close();
  if (server && server.exitCode === null) server.kill();
});
