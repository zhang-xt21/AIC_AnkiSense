"use strict";
// Deterministic browser acceptance. Image lifecycle comes from local fixture
// responses; no personal files or model requests are used.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");
const { project, pythonExecutable, browserOptions } = require("./browser_runtime.cjs");
const navigation = require("./browser_navigation.cjs");
const base = `http://127.0.0.1:${process.env.ANKISENSE_UI_PORT || 8976}`;
const runDir = path.join(project, ".ui-checks", "20260927-workbench", `auto-illustration-${new Date().toISOString().replace(/[:.]/g, "-")}`);
fs.mkdirSync(runDir, { recursive: true });
let server, browser, page;
const failures = [], external = [], writes = [];
async function check(name, run) { await run(); console.log(`PASS ${name}`); }
async function screenshot(name) { await page.screenshot({ path: path.join(runDir, `${name}.png`), fullPage: true }); }
(async () => {
  try { await fetch(`${base}/health`, { signal: AbortSignal.timeout(500) }); throw new Error("Test port 8976 is in use."); }
  catch (error) { if (!["TypeError", "TimeoutError"].includes(error.name)) throw error; }
  const log = fs.openSync(path.join(runDir, "server.log"), "a");
  server = spawn(pythonExecutable(), ["-X", "utf8", path.join(project, "tools", "fast_draft_fixture.py")], { cwd: project, windowsHide: true, env: { ...process.env, ANKISENSE_UI_DATA: path.join(runDir, "data") }, stdio: ["ignore", log, log] });
  let state;
  for (let i = 0; i < 200; i++) {
    if (server.exitCode !== null) throw new Error(`Fixture exited; ${runDir}/server.log`);
    try { const response = await fetch(`${base}/api/state`); if (response.ok) { state = await response.json(); break; } } catch {}
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  assert.ok(state, "Fixture failed to start");
  const original = state.cards.find(item => item.id === "fixture-pdf-stack");
  const sourcePage = state.source_pages.find(item => item.document_id === original.document_id);
  assert.ok(sourcePage.image_url);
  const sourceImage = Buffer.from(await (await fetch(`${base}${sourcePage.image_url}`)).arrayBuffer());
  const makeCard = (id, need, imageState) => ({ ...original, id, question: `${id}: 根据讲义中的图，指出栈顶的位置。`, status: "draft", images: [], review_state: "needs_review", approval: undefined, image_intent: { need, reason: "离线测试配图状态", block_ids: original.evidence.map(ref => ref.block_id) }, auto_illustration: { state: imageState, message: imageState === "queued" ? "正在等待配图任务。" : "离线测试：未获得合适图片。" } });
  state.cards = [makeCard("image-pending", "required", "queued"), makeCard("image-optional", "helpful", "failed"), makeCard("image-required", "required", "not_found")];
  state.mode = "cloud"; state.configured = true; state.model = "offline-fixture"; state.generation_jobs = []; state.media = [];
  browser = await chromium.launch(browserOptions());
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: "block" });
  await context.route("**/*", route => {
    const url = route.request().url();
    if (url.startsWith(`${base}/`) || url.startsWith("data:")) return route.continue();
    external.push(url); return route.abort();
  });
  page = await context.newPage(); page.on("pageerror", error => failures.push(error.message));
  page.on("request", request => { if (["POST", "PUT", "DELETE"].includes(request.method())) writes.push(request.url()); });
  const reply = (route, body) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  await page.route("**/api/state", route => reply(route, state));
  await page.route("**/generation-plan", route => reply(route, { document_id: original.document_id, scope: "all", source_fingerprint: "f".repeat(64), recommended_count: 1, target_count: 1, stats: { source_page_count: 1, source_block_count: 1, source_chars: 200, batch_count: 1, coverage_ratio: 1 }, warnings: [] }));
  await page.route("**/auto-review", route => reply(route, { status: "unchecked", current: false, reasons: [] }));
  await page.route("**/api/card-media/fixture-auto-image/file", route => route.fulfill({ status: 200, contentType: "image/png", body: sourceImage }));
  await page.goto(base);
  await navigation.stage(page, "card");
  const imagePanel = () => page.locator(".card-image-editor .auto-illustration");
  await check("Image-only background work continues polling after generation and text review finish", async () => {
    await imagePanel().getByText("等待配图", { exact: true }).waitFor();
    assert.match(await imagePanel().innerText(), /需要看图作答.*才能开始学习/);
    assert.equal(await page.locator(".card-auto-review-action").isDisabled(), true, "Image work must not offer a duplicate paid review");
    state.cards[0].auto_illustration = { state: "matching", message: "正在引用页中寻找图块。" };
    await imagePanel().getByText("正在匹配讲义图片", { exact: true }).waitFor();
    state.cards[0].auto_illustration = { state: "checking", message: "正在核对候选图与题目。" };
    await imagePanel().getByText("正在检查配图", { exact: true }).waitFor();
    await screenshot("checking-desktop");
  });
  await check("A newly attached image never replaces unsaved input or focus and updates media records", async () => {
    const input = page.locator(".workbench-editor").getByLabel("问题", { exact: true });
    await input.fill("我正在修改的问题，不要被后台覆盖"); const handle = await input.elementHandle();
    state.media.push({ id: "fixture-auto-image", url: "/api/card-media/fixture-auto-image/file", width: 600, height: 800, source: { page: sourcePage.page, revision: sourcePage.revision } });
    state.cards[0] = { ...state.cards[0], version: state.cards[0].version + 1, images: [{ media_id: "fixture-auto-image", side: "question", caption: "讲义原图" }], auto_illustration: { state: "attached", message: "已保存第 1 页的配图。", media_id: "fixture-auto-image" } };
    await imagePanel().getByText("已自动配图", { exact: true }).waitFor();
    assert.equal(await input.inputValue(), "我正在修改的问题，不要被后台覆盖");
    assert.ok(await handle.evaluate(node => node.isConnected && document.activeElement === node)); await handle.dispose();
    assert.match(await imagePanel().innerText(), /未保存修改和当前预览已保留/);
    assert.equal(await page.locator("#card-live-preview img").count(), 0);
    assert.ok(await page.evaluate(() => app.data.media.some(item => item.id === "fixture-auto-image")));
    await screenshot("saved-image-draft-preserved");
  });
  await check("Loading the saved image is explicit and cancelling preserves the current draft", async () => {
    page.once("dialog", dialog => dialog.dismiss());
    await imagePanel().getByRole("button", { name: "载入最新版本", exact: true }).click();
    assert.equal(await page.locator(".workbench-editor").getByLabel("问题", { exact: true }).inputValue(), "我正在修改的问题，不要被后台覆盖");
    page.once("dialog", dialog => dialog.accept());
    await imagePanel().getByRole("button", { name: "载入最新版本", exact: true }).click();
    await page.locator("#card-live-preview img").waitFor();
    assert.equal(await page.locator(".workbench-editor").getByLabel("问题", { exact: true }).inputValue(), state.cards[0].question);
    assert.match(await page.locator(".preview-image-count").innerText(), /正面 1 张图/);
    await screenshot("attached-desktop");
  });
  await check("Optional failure and required missing images explain different learning outcomes", async () => {
    await page.getByLabel("选择编辑卡片", { exact: true }).selectOption("image-optional");
    assert.match(await imagePanel().innerText(), /不影响文字卡片继续检查/);
    await page.getByLabel("选择编辑卡片", { exact: true }).selectOption("image-required");
    assert.match(await imagePanel().innerText(), /题目配图完成并通过检查后才能开始学习/);
    await page.setViewportSize({ width: 390, height: 844 });
    const width = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
    assert.ok(width[0] <= width[1] + 1, width.join(" / "));
    await screenshot("required-mobile");
  });
  assert.ok(writes.every(url => url.endsWith("/generation-plan")), "Viewing image progress must never request a paid operation");
  assert.deepEqual(external, []); assert.deepEqual(failures, []); console.log(`Screenshots: ${runDir}`);
})().catch(async error => { process.exitCode = 1; console.error(error.stack || error); if (page) try { await screenshot("failure"); } catch {} }).finally(async () => { if (browser) await browser.close(); if (server && server.exitCode === null) server.kill(); });
