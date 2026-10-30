"use strict";

// Entirely fictitious HTTP fixture: reads only static source files, with no
// Python service, .env, personal material, database or model client involved.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const playwright = require("playwright");
const { project, browserOptions } = require("./browser_runtime.cjs");
const runDir = path.join(project, ".ui-checks", "20261013-auto-cards", `run-${new Date().toISOString().replace(/[:.]/g, "-")}`);
fs.mkdirSync(runDir, { recursive: true });
const requests = [], failures = [], results = [], jobs = [];
const docId = "pdf-fictional-auto";
const secondId = "text-fictional-other";
const state = {
  mode: "cloud", configured: true, model: "fictional-model", image_check: { available: false, message: "Fixture does not invoke models." },
  documents: [{ id: docId, title: "自动制卡离线示例", filename: "fixture.pdf", file_url: "/fixture.pdf" }, { id: secondId, title: "第二份离线示例" }],
  blocks: [{ block_id: "fixture-block-1", document_id: docId, page: 1, text: "栈是后进先出的线性结构，入栈和出栈都在栈顶进行。" }, { block_id: "fixture-block-2", document_id: docId, page: 2, text: "队列是先进先出的线性结构，入队在队尾，出队在队头。" }, { block_id: "fixture-block-other", document_id: secondId, page: 1, text: "这是第二份独立的测试材料，仅用于检查任务不会串到其他材料。" }],
  source_pages: [{ id: "fixture-page-1", document_id: docId, page: 1, status: "reviewed", revision: 1, reviewed_at: "2026-10-13T01:00:00Z", text: "栈是后进先出的线性结构，入栈和出栈都在栈顶进行。", raw_text: "栈是后进先出的线性结构。", block_ids: ["fixture-block-1"], image_url: "/fixture-page.svg", method: "text" }, { id: "fixture-page-2", document_id: docId, page: 2, status: "pending", revision: 1, text: "队列是先进先出的线性结构，入队在队尾，出队在队头。", raw_text: "队列是先进先出的线性结构。", block_ids: ["fixture-block-2"], image_url: "/fixture-page.svg", method: "text" }],
  cards: [], attempts: [], media: []
};
const fixtureCard = {
  id: "fixture-auto-card", document_id: docId, status: "draft", version: 1, kind: "basic", concept: "栈", question: "栈的出入顺序是什么？", answer: "后进先出。", images: [],
  rubric: [{ point_id: "p1", text: "指出后进先出。" }], evidence: [{ block_id: "fixture-block-1", quote: "栈是后进先出的线性结构" }], auto_job_id: "fixture-job-1", knowledge_id: "fixture-knowledge-1",
  auto_quality: { verdict: "needs_review", card_version: 1, source_block_ids: ["fixture-block-1"], prompt_version: "fixture-v1", repair_attempts: 0, issues: [{ code: "answer_scope", severity: "warning", reason: "请确认答案覆盖入栈和出栈位置。", evidence: "原文同时说明栈顶操作。", suggestion: "对照原文补充说明，再审核。" }] },
  image_result: { status: "recommended", message: "本机候选已准备，等待核对采纳。", candidate_ids: ["fixture-image"] }
};
let calibrationReport = null;
function json(response, value, status = 200) { response.writeHead(status, { "Content-Type": "application/json; charset=utf-8" }); response.end(JSON.stringify(value)); }
function readBody(request) { return new Promise((resolve, reject) => { let content = ""; request.on("data", part => { content += part; }); request.on("end", () => { try { resolve(JSON.parse(content || "{}")); } catch (error) { reject(error); } }); }); }
const server = http.createServer(async (request, response) => {
  try {
    const pathname = new URL(request.url, "http://fixture.test").pathname;
    const body = ["POST", "PUT"].includes(request.method) ? await readBody(request) : null;
    requests.push({ pathname, method: request.method, body });
    if (pathname === "/api/state") return json(response, state);
    if (pathname === "/fixture-page.svg") { response.writeHead(200, { "Content-Type": "image/svg+xml" }); return response.end('<svg xmlns="http://www.w3.org/2000/svg" width="600" height="800"><rect width="600" height="800" fill="white"/><text x="50" y="100" font-size="26">Fictional source page</text></svg>'); }
    const documentMatch = pathname.match(/^\/api\/documents\/([^/]+)\/auto-card-jobs$/);
    if (documentMatch && request.method === "GET") return json(response, { jobs: jobs.filter(job => job.document_id === documentMatch[1]) });
    if (documentMatch && request.method === "POST") {
      const job = { id: `fixture-job-${jobs.length + 1}`, document_id: documentMatch[1], status: "pending", stage: "planning", progress: { completed: 0, total: 2 }, card_ids: [], issues: [], message: "已排队，正在整理知识点。", created_at: "2026-10-13T02:00:00Z", options: body };
      jobs.unshift(job); return json(response, job);
    }
    const jobMatch = pathname.match(/^\/api\/auto-card-jobs\/([^/]+)(?:\/(retry|cancel))?$/);
    if (jobMatch) {
      const job = jobs.find(item => item.id === jobMatch[1]);
      if (!job) return json(response, { detail: "Unknown fixture job" }, 404);
      if (jobMatch[2] === "cancel") { job.status = "cancelled"; job.message = "任务已取消，已生成草稿保留。"; }
      if (jobMatch[2] === "retry") { job.status = "running"; job.message = "继续处理剩余批次。"; job.issues = []; }
      return json(response, job);
    }
    const pageMatch = pathname.match(/^\/api\/documents\/[^/]+\/pages\/(\d+)$/);
    const calibrationMatch = pathname.match(/^\/api\/documents\/[^/]+\/pages\/(\d+)\/calibration$/);
    if (calibrationMatch) {
      const source = state.source_pages.find(item => item.page === Number(calibrationMatch[1]));
      if (request.method === "POST") calibrationReport = { id: "fixture-calibration", status: "needs_confirmation", base_revision: source.revision,
        original_text: source.text, proposed_text: source.text.replace("队列是", "队列遵循"), changes: [{ before: "是", after: "遵循" }], issues: ["两次原页读取不一致，请对照原页确认。"] };
      return json(response, calibrationReport || { status: "not_checked" });
    }
    if (pageMatch && request.method === "PUT") { const source = state.source_pages.find(item => item.page === Number(pageMatch[1])); Object.assign(source, { text: body.text, status: "reviewed", revision: source.revision + 1, reviewed_at: "2026-10-13T02:00:00Z" }); return json(response, source); }
    if (pathname === `/api/cards/${fixtureCard.id}` && request.method === "PUT") { const card = state.cards[0]; Object.assign(card, body, { version: card.version + 1 }); card.auto_quality.verdict = "stale"; return json(response, card); }
    if (pathname === `/api/cards/${fixtureCard.id}/auto-quality-check` && request.method === "POST") { const card = state.cards[0]; if (body.expected_version !== card.version) return json(response, { detail: "Version changed" }, 409); card.auto_quality = { verdict: "clear", card_version: card.version, issues: [], supported_point_ids: ["p1"] }; return json(response, card); }
    const local = pathname === "/" ? "index.html" : pathname.startsWith("/static/") ? pathname.slice(8) : null;
    if (local && !local.includes("..") && !local.includes("\\")) {
      const file = path.join(project, "ankisense", "static", local);
      if (fs.existsSync(file)) { response.writeHead(200, { "Content-Type": file.endsWith(".js") ? "text/javascript; charset=utf-8" : file.endsWith(".css") ? "text/css; charset=utf-8" : file.endsWith(".svg") ? "image/svg+xml" : "text/html; charset=utf-8" }); return response.end(fs.readFileSync(file)); }
    }
    return json(response, { detail: "Fixture route not implemented" }, 404);
  } catch (error) { failures.push(error.message); json(response, { detail: "Fixture error" }, 500); }
});
let browser, page;
async function check(name, action) { await action(); results.push({ name, status: "passed" }); console.log(`PASS ${name}`); }
const consolePanel = () => page.locator("#auto-card-console");
const start = () => page.getByRole("button", { name: "整份材料自动制卡 →", exact: true });
const jobState = () => consolePanel().locator("[data-job-status]");
async function materialReady() { await page.locator("#manual-tools").evaluate(n => { n.open = true; }); await consolePanel().waitFor(); await page.waitForFunction(() => document.querySelector("[data-auto-start]") && !document.querySelector("[data-auto-start]").disabled); }
async function openManualTools() { await page.locator("#manual-tools").evaluate(n => { n.open = true; }); await consolePanel().waitFor(); }
(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  browser = await playwright.chromium.launch(browserOptions());
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
  await context.route("**/*", route => { if (route.request().url().startsWith(base + "/") || route.request().url().startsWith("data:")) return route.continue(); failures.push("External request blocked"); return route.abort(); });
  page = await context.newPage();
  page.on("pageerror", error => failures.push(error.message));
  await page.goto(base);
  await check("Full-document entry defaults to 30 and local recommendations without selected blocks", async () => {
    await materialReady();
    assert.equal(await page.getByLabel("自动制卡张数上限").inputValue(), "30");
    assert.equal(await page.getByLabel("自动配图方式").inputValue(), "recommend");
    assert.equal(await page.getByLabel("自动校准原页并继续制卡").isChecked(), false);
    assert.equal(await page.locator('.source-scroll input:checked').count(), 0);
    await start().click();
    await page.waitForFunction(() => document.querySelector("[data-job-status]")?.dataset.jobStatus === "pending");
    const sent = requests.find(item => item.method === "POST" && item.pathname.endsWith("/auto-card-jobs"));
    assert.deepEqual(sent.body, { max_cards: 30, image_mode: "recommend" });
    assert.equal(await start().isDisabled(), true);
  });
  await check("Polling continues across navigation without switching the user's view", async () => {
    await page.locator('[data-view="practice"]').click();
    jobs[0].status = "awaiting_confirmation";
    jobs[0].progress.completed = 1;
    jobs[0].message = "第 2 页需确认识别文字，已有批次保留。";
    jobs[0].issues = [{ code: "source_confirmation", severity: "warning", reason: "请对照原页确认第 2 页。", page: 2, block_ids: ["fixture-block-2"], suggestion: "校对并保存后继续制卡。" }];
    await page.waitForFunction(() => JSON.parse(sessionStorage.getItem("ankisense-auto-card-jobs-v1"))["pdf-fictional-auto"][0].status === "awaiting_confirmation");
    assert.equal(await page.locator("#main").getAttribute("data-view"), "practice");
    await page.locator('[data-view="materials"]').click();
    await materialReady();
    assert.match(await consolePanel().innerText(), /1 \/ 2 批/);
    assert.match(await consolePanel().innerText(), /第 2 页/);
    await page.getByRole("button", { name: "定位待确认原文 ↑", exact: true }).click();
    assert.equal(await page.locator("#proof-page-selector").inputValue(), "2");
  });
  await check("Original-page calibration is explicit and candidates do not overwrite source text", async () => {
    const original = state.source_pages[1].text;
    await page.getByRole("button", { name: "对照原页自动校准（云端）", exact: true }).click();
    await page.getByRole("button", { name: "将建议载入校对框", exact: true }).waitFor();
    assert.equal(state.source_pages[1].text, original);
    assert.match(await page.getByLabel("原页自动校准", { exact: true }).innerText(), /两次原页读取不一致/);
    await page.getByLabel("原页自动校准", { exact: true }).screenshot({ path: path.join(runDir, "03-calibration-diff.png") });
    await page.getByRole("button", { name: "将建议载入校对框", exact: true }).click();
    assert.equal(await page.locator("#proof-text").inputValue(), calibrationReport.proposed_text);
    assert.equal(await start().isDisabled(), true);
  });
  await check("Unsaved proofreading immediately disables start and retry; saving allows resume", async () => {
    await page.locator("#proof-text").fill(state.source_pages[1].text + " 已校对。");
    assert.equal(await start().isDisabled(), true);
    assert.equal(await page.getByRole("button", { name: "已确认原文，继续制卡", exact: true }).isDisabled(), true);
    assert.match(await consolePanel().innerText(), /未保存/);
    await page.getByRole("button", { name: "保存并确认校对 ✓", exact: true }).click();
    await page.waitForFunction(() => !document.querySelector("#proof-text")?.disabled);
    await page.getByRole("button", { name: "已确认原文，继续制卡", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("[data-job-status]")?.dataset.jobStatus === "running");
    assert.equal(requests.filter(item => item.method === "POST" && item.pathname.endsWith("/retry")).length, 1);
  });
  await check("Completion persists, shows generated cards and version-bound quality issues", async () => {
    await page.getByRole("button", { name: "粘贴文本", exact: true }).click();
    await page.getByLabel("材料标题", { exact: true }).fill("保留未提交的新材料");
    state.source_pages[0].status = "auto_verified";
    state.source_pages[0].reviewed_at = null;
    state.source_pages[0].verification = { revision: 1 };
    state.cards = [structuredClone(fixtureCard)];
    jobs[0].status = "completed"; jobs[0].progress.completed = 2; jobs[0].card_ids = [fixtureCard.id]; jobs[0].message = "已生成 1 张草稿，请审核。";
    await page.waitForFunction(() => document.querySelector("[data-job-status]")?.dataset.jobStatus === "completed");
    await page.waitForFunction(() => document.querySelector("#source-proofreader")?.textContent.includes("1 页机器核对"));
    assert.equal(await page.getByLabel("材料标题", { exact: true }).inputValue(), "保留未提交的新材料");
    await page.reload();
    await materialReady();
    await page.getByRole("button", { name: "查看已生成卡片 →", exact: true }).waitFor();
    await page.getByRole("button", { name: "查看已生成卡片 →", exact: true }).click();
    await page.locator('[data-quality-verdict="needs_review"]').waitFor();
    assert.match(await page.locator("[data-auto-quality]").innerText(), /栈顶操作/);
    assert.match(await page.locator("[data-auto-quality]").innerText(), /本机候选已准备/);
    await page.screenshot({ path: path.join(runDir, "01-quality-desktop.png"), fullPage: true });
    await page.getByLabel("问题", { exact: true }).fill("栈的出入顺序和操作位置是什么？");
    assert.equal(await page.locator("[data-auto-quality]").getAttribute("data-quality-verdict"), "stale");
    assert.equal(await page.getByRole("button", { name: "重新检查已保存卡片", exact: true }).isDisabled(), true);
    await page.getByRole("button", { name: "保存草稿", exact: true }).click();
    await page.waitForFunction(() => document.querySelector(".workbench-editor .panel-header .meta")?.textContent.includes("版本 2"));
    assert.equal(await page.locator("[data-auto-quality]").getAttribute("data-quality-verdict"), "stale");
    await page.getByRole("button", { name: "重新检查已保存卡片", exact: true }).click();
    await page.locator('[data-quality-verdict="clear"]').waitFor();
    assert.equal(requests.find(item => item.pathname.endsWith("/auto-quality-check"))?.body.expected_version, 2);
  });
  await check("Material changes keep jobs isolated and cancellation preserves history", async () => {
    await page.locator('[data-view="materials"]').click();
    await materialReady();
    await page.getByRole("button", { name: /第二份离线示例/ }).click();
    await materialReady();
    assert.equal(await jobState().count(), 0);
    await page.getByRole("button", { name: /自动制卡离线示例/ }).click();
    await materialReady();
    await start().click();
    await page.getByRole("button", { name: "取消任务", exact: true }).waitFor();
    await page.getByRole("button", { name: "取消任务", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("[data-job-status]")?.dataset.jobStatus === "cancelled");
    assert.match(await consolePanel().innerText(), /历史任务/);
  });
  await check("Vision uploads require explicit mode selection and layout fits narrow screens", async () => {
    await page.getByLabel("自动配图方式").selectOption("checked");
    await page.getByLabel("自动校准原页并继续制卡").check();
    assert.match(await consolePanel().locator(".auto-image-hint").innerText(), /发送候选裁图、对应原页/);
    await page.getByLabel("自动制卡张数上限").fill("12");
    await start().click();
    await page.getByRole("button", { name: "取消任务", exact: true }).waitFor();
    assert.deepEqual(jobs[0].options, { max_cards: 12, image_mode: "checked", source_mode: "visual" });
    await page.reload();
    await openManualTools();
    await page.getByRole("button", { name: "取消任务", exact: true }).waitFor();
    assert.equal(await page.getByLabel("自动校准原页并继续制卡").isChecked(), true);
    assert.equal(await page.getByLabel("自动配图方式").inputValue(), "checked");
    await page.getByRole("button", { name: "取消任务", exact: true }).click();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(runDir, "02-console-mobile.png"), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
  });
  await check("Machine-reviewed pages are visibly distinct and eligible for card generation", async () => {
    state.source_pages[0].status = "auto_verified";
    state.source_pages[0].reviewed_at = null;
    state.source_pages[0].verification = { revision: 1 };
    await page.reload();
    await openManualTools();
    await consolePanel().waitFor();
    await page.locator("#proof-page-selector").selectOption("1");
    assert.match(await page.locator("#source-proofreader").innerText(), /机器已核对/);
    assert.match(await page.locator("#source-proofreader").innerText(), /1 页机器核对/);
    assert.equal(await page.locator('.source-scroll input[type="checkbox"]').first().isDisabled(), false);
    assert.equal(await page.getByRole("button", { name: "对照原页自动校准（云端）", exact: true }).isDisabled(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
  });
  await check("Unconfigured cloud disables generation", async () => {
    state.configured = false;
    await page.reload();
    await openManualTools();
    await consolePanel().waitFor();
    assert.equal(await start().isDisabled(), true);
    assert.match(await consolePanel().innerText(), /配置云端模型/);
  });
  assert.deepEqual(failures, []);
})().catch(error => { failures.push(error.stack || error.message); process.exitCode = 1; }).finally(async () => {
  fs.writeFileSync(path.join(runDir, "results.json"), JSON.stringify({ results, failures, requests }, null, 2));
  if (browser) await browser.close();
  await new Promise(resolve => server.close(resolve));
  console.log(`Artifacts: ${runDir}`);
  if (failures.length) console.error(failures.join("\n"));
});
