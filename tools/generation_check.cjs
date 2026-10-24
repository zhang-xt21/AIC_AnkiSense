"use strict";
// Offline interaction acceptance: API responses are deterministic and no model
// or personal database is used. Real job execution is exercised separately by
// auto_learning_check.cjs and the backend integration tests.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");
const { project, pythonExecutable, browserOptions } = require("./browser_runtime.cjs");
const navigation = require("./browser_navigation.cjs");
const base = `http://127.0.0.1:${process.env.ANKISENSE_UI_PORT || 8976}`;
const runDir = path.join(project, ".ui-checks", "20260927-workbench", `generation-${new Date().toISOString().replace(/[:.]/g, "-")}`);
fs.mkdirSync(runDir, { recursive: true });
let server, browser, page;
const failures = [], external = [], plans = [], starts = [], stops = [];
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
  const card = state.cards.find(item => item.id === "fixture-pdf-stack");
  const document = state.documents.find(item => item.id === card.document_id);
  state.mode = "cloud"; state.configured = true; state.model = "offline-fixture"; state.generation_jobs = [];
  const first = state.blocks.find(item => item.block_id === card.evidence[0].block_id);
  state.blocks = Array.from({ length: 8 }, (_, i) => ({ ...first, block_id: `fixture-range-${i + 1}`, page: i + 1, text: `自编学习材料，第 ${i + 1} 节。${first.text}` }));
  state.source_pages = []; state.cards = []; state.documents = [document];
  let conflict = false, loseStartResponse = false, latePlanResolve;
  browser = await chromium.launch(browserOptions());
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: "block" });
  await context.route("**/*", route => {
    const url = route.request().url();
    if (url.startsWith(`${base}/`) || url.startsWith("data:")) return route.continue();
    external.push(url); return route.abort();
  });
  page = await context.newPage(); page.on("pageerror", error => failures.push(error.message));
  const reply = (route, body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  await page.route("**/api/state", route => reply(route, state));
  await page.route("**/generation-plan", async route => {
    const request = route.request().postDataJSON(); plans.push(request);
    if (request.block_ids?.length === 0) return reply(route, { detail: "请至少选择一个原文片段。" }, 422);
    if (request.count === 11) await new Promise(resolve => { latePlanResolve = resolve; });
    const recommended = request.block_ids ? 2 : 8;
    return reply(route, { document_id: document.id, scope: request.block_ids ? "selected" : "all", source_fingerprint: "f".repeat(64), recommended_count: recommended, target_count: request.count ?? recommended, suggestion_reason: "按有效原文长度和分批覆盖需要估计。", warnings: request.count === 1 ? ["本次仅覆盖部分原文，可以增加数量。"] : [], stats: { source_page_count: request.block_ids?.length || 8, source_block_count: request.block_ids?.length || 8, source_chars: 5400, batch_count: Math.ceil((request.count ?? recommended) / 6), coverage_ratio: request.count === 1 ? 0.5 : 1 } });
  });
  await page.route("**/api/generation-jobs", route => {
    const body = route.request().postDataJSON(); starts.push(body);
    if (conflict) { conflict = false; return reply(route, { detail: "原文已更新，请重新计算。" }, 409); }
    const job = { id: `fixture-job-${starts.length}`, document_id: document.id, request_id: body.request_id, auto_images: body.auto_images, status: "running", target_count: body.count, generated_count: 0, completed_batches: 0, total_batches: Math.ceil(body.count / 6), card_ids: [], warnings: [], created_at: new Date().toISOString() };
    state.generation_jobs.push(job);
    if (loseStartResponse) { loseStartResponse = false; return route.abort("failed"); }
    return reply(route, job);
  });
  await page.route("**/api/generation-jobs/*/stop", route => {
    stops.push(route.request().url()); const job = state.generation_jobs.at(-1);
    job.status = "stopped"; job.stop_requested = true; return reply(route, job);
  });
  await page.route("**/auto-review", route => reply(route, { status: "unchecked", current: false, reasons: [] }));
  await page.goto(base);
  await navigation.openMaterials(page);
  const count = () => page.getByLabel("制卡数量", { exact: true });
  const start = () => page.getByRole("button", { name: "开始制卡 →", exact: true });
  async function value(number) { await count().fill(String(number)); await page.waitForFunction(() => !document.querySelector(".generation-start")?.disabled); }
  await check("All valid source text and a free local quantity recommendation are the default", async () => {
    await page.waitForFunction(() => document.querySelector("#generation-count")?.value === "8");
    assert.equal(plans.at(-1).block_ids, null); assert.equal(plans.at(-1).count, null);
    assert.equal(await page.locator(".generation-advanced").getAttribute("open"), null);
    assert.match(await page.locator(".generation-scope").innerText(), /全部有效原文.*8 页/);
    assert.equal(starts.length, 0); await screenshot("suggested-desktop");
    assert.equal(await page.getByLabel("自动匹配讲义图片", { exact: true }).isChecked(), true);
  });
  await check("Custom quantity survives range changes and old recommendation responses", async () => {
    await value(12);
    await page.getByText("高级设置 · 指定出题范围", { exact: true }).click();
    await page.getByLabel("仅使用勾选的原文片段", { exact: true }).check();
    await page.locator(".generation-error").filter({ hasText: "至少选择" }).waitFor();
    await page.locator(".source-block input").first().check();
    await page.waitForFunction(() => document.querySelector(".generation-recommendation")?.textContent.includes("系统建议 2 张"));
    assert.equal(await count().inputValue(), "12");
    await page.getByLabel("仅使用勾选的原文片段", { exact: true }).uncheck();
    await page.waitForFunction(() => document.querySelector(".generation-recommendation")?.textContent.includes("系统建议 8 张"));
    assert.equal(plans.at(-1).block_ids, null);
    await count().fill("11");
    await page.waitForFunction(() => document.querySelector(".generation-start")?.disabled);
    for (let i = 0; i < 40 && !latePlanResolve; i++) await new Promise(resolve => setTimeout(resolve, 50));
    assert.ok(latePlanResolve);
    await value(12); latePlanResolve();
    await page.waitForTimeout(100);
    assert.equal(await count().inputValue(), "12");
    assert.equal(await start().isEnabled(), true);
    await page.getByRole("button", { name: "使用系统建议", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("#generation-count")?.value === "8");
    await page.getByText("高级设置 · 指定出题范围", { exact: true }).click();
  });
  await check("Invalid counts cannot start, and reduced coverage is shown before submission", async () => {
    for (const invalid of ["0", "31", "1.5", ""]) {
      await count().fill(invalid); await page.locator(".generation-error").filter({ hasText: "1 到 30" }).waitFor();
      assert.equal(await start().isDisabled(), true);
    }
    await value(1); assert.match(await page.locator(".generation-warnings").innerText(), /部分原文/);
    await value(12);
  });
  await check("One explicit start creates a background job; reload restores it without another POST", async () => {
    await page.getByLabel("自动匹配讲义图片", { exact: true }).uncheck();
    await start().click();
    await page.getByRole("button", { name: "本讲义正在制卡", exact: true }).waitFor();
    assert.equal(starts.length, 1); assert.equal(starts[0].block_ids, null); assert.equal(starts[0].count, 12);
    assert.equal(starts[0].auto_images, false);
    assert.equal(await page.getByLabel("自动匹配讲义图片", { exact: true }).isDisabled(), true);
    assert.equal(await count().isDisabled(), true);
    await page.reload();
    await navigation.openMaterials(page);
    await page.getByText("正在生成卡片", { exact: true }).waitFor(); assert.equal(starts.length, 1);
    assert.equal(await count().inputValue(), "12", "Reloaded active jobs retain their submitted target");
    assert.equal(await page.getByLabel("自动匹配讲义图片", { exact: true }).isChecked(), false, "Reloaded active jobs retain the image option");
    const job = state.generation_jobs.at(-1);
    state.cards = [{ ...card, id: "generated-first", question: "First generated card", images: [] }];
    Object.assign(job, { generated_count: 1, completed_batches: 1, card_ids: ["generated-first"] });
    await page.waitForFunction(() => document.querySelector(".generation-job-progress")?.textContent.includes("已生成 1 /"));
    await screenshot("generating-desktop");
    await page.getByRole("button", { name: "查看已生成卡片", exact: true }).click();
  });
  await check("Later batches update saved cards without replacing a focused unsaved editor", async () => {
    const input = page.locator(".workbench-editor").getByLabel("问题", { exact: true });
    await input.fill("An unsaved question while batches run"); const handle = await input.elementHandle();
    state.cards.push({ ...card, id: "generated-second", question: "Second generated card", images: [] });
    Object.assign(state.generation_jobs.at(-1), { generated_count: 2, card_ids: ["generated-first", "generated-second"] });
    await page.waitForFunction(() => app.data.cards.some(card => card.id === "generated-second"));
    assert.equal(await input.inputValue(), "An unsaved question while batches run");
    assert.ok(await handle.evaluate(node => node.isConnected && document.activeElement === node)); await handle.dispose();
    await navigation.stage(page, "source");
    await page.getByRole("button", { name: "停止后续批次", exact: true }).click();
    await page.getByText("已停止后续批次", { exact: true }).waitFor();
    assert.equal(stops.length, 1); assert.equal(state.cards.length, 2);
  });
  await check("Stale plans are refreshed, and partial failure preserves cards without automatic retry", async () => {
    conflict = true; await value(12); await start().click();
    await page.locator(".generation-error").filter({ hasText: "请确认更新后的范围" }).waitFor();
    assert.equal(state.generation_jobs.length, 1);
    await start().click(); const job = state.generation_jobs.at(-1);
    Object.assign(job, { status: "partial", generated_count: 2, completed_batches: 1, card_ids: state.cards.map(card => card.id), error: "离线测试：第二批请求失败，已生成卡片保留。" });
    await page.getByText("已生成部分卡片", { exact: true }).waitFor();
    assert.match(await page.locator(".generation-job-error").innerText(), /第二批请求失败/);
    assert.equal(starts.length, 3); assert.equal(await page.getByRole("button", { name: "查看已生成卡片", exact: true }).isEnabled(), true);
    await page.setViewportSize({ width: 390, height: 844 });
    const width = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
    assert.ok(width[0] <= width[1] + 1, width.join(" / ")); await screenshot("partial-mobile");
  });
  await check("An unknown start response is recovered by reading state without a second paid start", async () => {
    await page.getByLabel("自动匹配讲义图片", { exact: true }).check();
    loseStartResponse = true;
    await start().click();
    await page.getByRole("button", { name: "本讲义正在制卡", exact: true }).waitFor();
    assert.equal(starts.length, 4);
    assert.equal(state.generation_jobs.at(-1).request_id, starts[3].request_id);
    assert.equal(starts[3].auto_images, true);
    await page.getByRole("button", { name: "停止后续批次", exact: true }).click();
  });
  await check("Rejected candidates show reasons and inert previews without losing valid cards", async () => {
    const job = state.generation_jobs.at(-1);
    state.cards = Array.from({ length: 5 }, (_, i) => ({ ...card, id: `isolated-valid-${i}`, question: `Valid candidate ${i}`, images: [] }));
    Object.assign(job, { status: "partial", target_count: 6, generated_count: 5, completed_batches: 1,
      card_ids: state.cards.map(item => item.id), rejected_count: 1, error: null, rejected_cards: [{ batch: 1, index: 6,
        question: '表格候选 <img src=x onerror="window.badCandidate=true">',
        reason: "卡片引用必须是对应讲义片段中的连续原文。", draft_preview: '{"question":"待核对候选","evidence":[{"quote":"不连续的引文"}]}' }] });
    await page.reload();
    await navigation.openMaterials(page);
    const rejected = page.locator(".generation-rejections");
    await rejected.waitFor();
    assert.match(await page.locator(".generation-job-progress").innerText(), /5 \/ 目标 6.*1 张未通过校验/);
    await rejected.locator(":scope > summary").click();
    await rejected.locator(".generation-rejection > summary").click();
    assert.match(await rejected.innerText(), /连续原文/);
    assert.match(await rejected.locator("pre").innerText(), /不连续的引文/);
    assert.equal(await rejected.locator("img").count(), 0);
    assert.equal(await page.evaluate(() => Boolean(window.badCandidate)), false);
    assert.equal(await page.getByRole("button", { name: "查看已生成卡片", exact: true }).isEnabled(), true);
    assert.equal(starts.length, 4, "Reading rejected candidates never generates or retries cards");
    await screenshot("rejected-mobile");
    const width = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
    assert.ok(width[0] <= width[1] + 1, width.join(" / "));
    await page.setViewportSize({ width: 1440, height: 1000 }); await screenshot("rejected-desktop");
  });
  await check("Outline gaps remain distinct from approval and text-range coverage", async () => {
    const job = state.generation_jobs.at(-1);
    job.coverage = { total: 2, matched_count: 1, note: "按题目词面匹配，不代表语义覆盖或审核通过。", topics: [
      { page: 1, title: "栈的后进先出", status: "possible_match" },
      { page: 2, title: "队列的先进先出", status: "unmatched" }] };
    await page.reload();
    await navigation.openMaterials(page);
    const coverage = page.locator(".generation-topic-coverage");
    await coverage.waitFor(); await coverage.locator("summary").click();
    assert.match(await coverage.innerText(), /1 项未匹配/);
    assert.match(await coverage.innerText(), /队列的先进先出 · 未匹配/);
    assert.match(await coverage.innerText(), /不代表语义覆盖或审核通过/);
    assert.equal(starts.length, 4);
    await screenshot("outline-desktop");
    await page.setViewportSize({ width: 390, height: 844 });
    await screenshot("outline-mobile");
    const width = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
    assert.ok(width[0] <= width[1] + 1, width.join(" / "));
  });
  assert.deepEqual(external, []); assert.deepEqual(failures, []); console.log(`Screenshots: ${runDir}`);
})().catch(async error => { process.exitCode = 1; console.error(error.stack || error); if (page) try { await screenshot("failure"); } catch {} }).finally(async () => { if (browser) await browser.close(); if (server && server.exitCode === null) server.kill(); });
