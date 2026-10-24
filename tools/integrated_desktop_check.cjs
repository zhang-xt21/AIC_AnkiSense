"use strict";
// Integrated LIN layout + ZHANG engines: local HTTP fixtures only.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { project, browserOptions } = require("./browser_runtime.cjs");
const { probeBackend } = require("../desktop/backend-health.cjs");
const directory = path.join(project, ".ui-checks", "integrated-desktop", new Date().toISOString().replace(/[:.]/g, "-"));
fs.mkdirSync(directory, { recursive: true });
const results = [], errors = [], writes = [];
let uploaded = false, retried = false, backendVersion = 1, pending = false;
let controlStatus = null, controlFailure = null, cancelFails = false, hasCards = true;
const doc = { id: "pdf-integrated", title: "整合示例", filename: "fixture.pdf" };
const media = ["front", "back"].map(id => ({ id, url: `/api/card-media/${id}/file`, source: { origin: "manual" } }));
function card(id, legacy = false) {
  return { id, document_id: doc.id, question: `${id} 看图说出出栈顺序`, answer: "后进先出", concept: "栈", kind: "basic", status: "approved", version: backendVersion,
    ...(legacy ? { approval_method: "automatic" } : { approval: { method: "machine" } }), review_state: pending ? "running" : "auto_pass",
    rubric: [{ point_id: "order", text: "后进先出" }], evidence: [{ block_id: "block1", quote: "栈遵循后进先出" }],
    images: [{ media_id: "front", side: "question", caption: "题图" }, { media_id: "back", side: "answer", caption: "答案图" }], image_intent: { need: "required" }, auto_illustration: { state: "attached" } };
}
function state() { return { mode: "cloud", configured: true, model: "isolated-fixture", image_check: { available: false },
  documents: uploaded ? [doc, { id: "demo-only", title: "隐藏的演示", is_demo: true }] : [],
  cards: uploaded && hasCards ? [card("machine-card"), card("legacy-card", true), { ...card("demo-card"), document_id: "demo-only", is_demo: true }] : [],
  blocks: uploaded ? [{ block_id: "block1", document_id: doc.id, page: 1, text: "栈遵循后进先出" }] : [], source_pages: [], source_proposals: [], generation_jobs: [], media, attempts: [] }; }
function imports() { return uploaded ? [{ id: "import-fixture", filename: doc.filename, document_id: doc.id, status: controlStatus || (retried ? "completed" : "partial"), retryable: controlStatus ? controlStatus !== "running" : !retried,
  failure: controlFailure, plan: { recommended_count: 85, target_count: null, total_batches: 67 },
  message: retried ? "制卡完成" : "已保留有效卡片，剩余批次可补跑", job: { card_ids: hasCards ? ["machine-card", "legacy-card"] : [], progress: { completed: retried ? 2 : 1, total: 2 },
  issues: controlFailure ? [...Array.from({ length: 2000 }, () => ({ code: "critical_number", reason: "数字需留意", page: 1 })), { code: "batch_failed", reason: "无法连接模型服务", pages: [2] }] : [] } }] : []; }
function json(response, value, status = 200) { response.writeHead(status, { "Content-Type": "application/json; charset=utf-8" }); response.end(JSON.stringify(value)); }
const server = http.createServer(async (request, response) => {
  const pathname = new URL(request.url, "http://localhost").pathname;
  if (pathname === "/health") return json(response, { application: "AnkiSense", api_version: 1, status: "ok" });
  if (pathname === "/api/state") return json(response, state());
  if (pathname === "/api/material-imports" && request.method === "POST") { let body = ""; for await (const chunk of request) body += chunk; writes.push({ path: pathname, body }); uploaded = true; return json(response, imports()[0]); }
  if (pathname === "/api/material-imports/import-fixture/retry") { writes.push({ path: pathname }); retried = true; return json(response, imports()[0]); }
  if (pathname === "/api/material-imports/import-fixture/cancel") {
    writes.push({ path: pathname });
    if (cancelFails) { cancelFails = false; return json(response, { detail: "offline cancel fixture" }, 500); }
    controlStatus = "cancelled"; return json(response, imports()[0]);
  }
  if (pathname === "/api/material-imports") return json(response, { imports: imports() });
  if (pathname.endsWith("/auto-review")) return json(response, { status: pending ? "running" : "auto_pass", current: true, checks: [], reasons: [], card: card(pathname.split("/")[3], pathname.includes("legacy")) });
  if (pathname.endsWith("/generation-plan")) return json(response, { target_count: 2, recommended_count: 2, warnings: [], outline: [], source_fingerprint: "fixture", stats: { batch_count: 1, coverage_ratio: 1 } });
  if (pathname.startsWith("/api/card-media/")) { response.writeHead(200, { "Content-Type": "image/svg+xml" }); return response.end('<svg xmlns="http://www.w3.org/2000/svg" width="320" height="100"><rect width="320" height="100" fill="#eaf3ff"/><text x="20" y="55" fill="#245db0">Stack fixture</text></svg>'); }
  const relative = pathname === "/" ? "index.html" : pathname.startsWith("/static/") ? pathname.slice(8) : null;
  if (relative && !relative.includes("..") && !relative.includes("\\")) { const file = path.join(project, "ankisense", "static", relative); if (fs.existsSync(file)) { response.writeHead(200, { "Content-Type": file.endsWith(".js") ? "text/javascript; charset=utf-8" : file.endsWith(".css") ? "text/css" : file.endsWith(".svg") ? "image/svg+xml" : "text/html; charset=utf-8" }); return response.end(fs.readFileSync(file)); } }
  if (request.method !== "GET") writes.push({ path: pathname });
  json(response, { detail: "Fixture route missing" }, 404);
});
let browser;
async function check(name, fn) { await fn(); results.push({ name, status: "passed" }); console.log("PASS " + name); }
(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  await check("Desktop health verifies the API identity and version", async () => { assert.equal((await probeBackend({ port: server.address().port })).ready, true); });
  browser = await chromium.launch(browserOptions());
  const page = await browser.newPage({ viewport: { width: 1280, height: 860 } });
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/*", route => route.request().url().startsWith(base + "/") ? route.continue() : route.abort());
  await page.goto(base);
  await check("Single automatic entry with optional count and image controls", async () => {
    await page.getByLabel("选择讲义，自动制卡").waitFor();
    assert.equal(await page.locator(".studio-steps").count(), 0);
    assert.equal(await page.locator("#manual-tools").evaluate(node => node.open), false);
    await page.getByText("高级选项：张数与配图", { exact: true }).click();
    await page.getByLabel("自动制卡目标数量", { exact: true }).fill("12");
    await page.getByLabel("自动匹配并检查讲义配图", { exact: true }).uncheck();
    await page.getByLabel("选择讲义，自动制卡").setInputFiles({ name: "fixture.pdf", mimeType: "application/pdf", buffer: Buffer.from("isolated fixture") });
    await page.locator('#main[data-view="cards"] .automatic-card').first().waitFor();
    assert.equal(writes.filter(item => item.path === "/api/material-imports").length, 1);
    assert.match(writes[0].body, /name="count"\r\n\r\n12/);
    assert.match(writes[0].body, /name="auto_images"\r\n\r\nfalse/);
  });
  await check("Machine and legacy approval, front/back images and demo isolation", async () => {
    assert.equal(await page.locator(".automatic-card").count(), 2);
    assert.equal(await page.getByText("已自动检查", { exact: true }).count(), 2);
    const first = page.locator('.automatic-card[data-card-id="machine-card"]');
    await first.getByRole("img", { name: "题图", exact: true }).waitFor({ state: "visible" });
    assert.equal(await first.getByRole("img", { name: "答案图", exact: true }).isVisible(), false);
    await first.getByText("显示答案", { exact: true }).click();
    await first.getByRole("img", { name: "答案图", exact: true }).waitFor({ state: "visible" });
    assert.equal(await page.getByText("隐藏的演示", { exact: true }).count(), 0);
    await page.screenshot({ path: path.join(directory, "results.png"), fullPage: true });
  });
  await check("Partial retry reuses the uploaded file without a second pipeline", async () => {
    await page.getByRole("button", { name: "重试未完成部分" }).click();
    await page.locator('[data-import-status="completed"]').waitFor();
    assert.equal(writes.filter(item => item.path === "/api/material-imports").length, 1);
    assert.equal(writes.filter(item => item.path.endsWith("/retry")).length, 1);
    assert.equal(writes.some(item => item.path.includes("auto-card-jobs") || item.path === "/api/card-drafts"), false);
  });
  await check("Editing selects the right document and background reviews preserve drafts", async () => {
    await page.locator('[data-card-id="machine-card"]').getByRole("button", { name: "编辑卡牌" }).click();
    await page.locator(".workbench-grid").waitFor();
    assert.equal(await page.locator('select[aria-label="筛选课程"]').inputValue(), doc.id);
    assert.equal(await page.locator('select[aria-label="筛选卡片状态"]').inputValue(), "all");
    const input = page.locator(".workbench-form textarea").first();
    await input.fill("我的未保存修改");
    await input.evaluate(node => { node.dataset.fixtureMarker = "preserved"; });
    backendVersion = 2; pending = true;
    await page.evaluate(() => { app.data.cards[0].review_state = "running"; return pollReviewProgress(); });
    assert.equal(await input.inputValue(), "我的未保存修改");
    assert.equal(await input.getAttribute("data-fixture-marker"), "preserved");
    await page.screenshot({ path: path.join(directory, "editor.png"), fullPage: true });
  });
  await check("Layout fits desktop and narrow result views", async () => {
    await page.locator('[data-view="cards"]').first().click();
    for (const width of [960, 390]) { await page.setViewportSize({ width, height: 844 }); assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true); }
    await page.screenshot({ path: path.join(directory, "mobile.png"), fullPage: true });
  });
  await check("Cancellation stops the selected task and preserves saved cards", async () => {
    controlStatus = "running";
    await page.evaluate(() => loadMaterialImports());
    await page.getByRole("button", { name: "取消处理", exact: true }).click();
    await page.getByText("已取消，可继续", { exact: true }).waitFor();
    assert.equal(await page.locator(".automatic-card").count(), 2);
    assert.equal(await page.getByRole("button", { name: "继续未完成部分", exact: true }).isEnabled(), true);
    assert.equal(writes.filter(item => item.path.endsWith("/cancel")).length, 1);
  });
  await check("Failed cancellation does not falsely claim that processing stopped", async () => {
    controlStatus = "running"; cancelFails = true;
    await page.evaluate(() => loadMaterialImports());
    await page.getByRole("button", { name: "取消处理", exact: true }).click();
    await page.getByText(/取消未完成：.*offline cancel fixture/).waitFor();
    assert.equal(await page.getByRole("button", { name: "取消处理", exact: true }).isEnabled(), true);
    assert.equal(await page.getByText("已取消，可继续", { exact: true }).count(), 0);
    await page.getByRole("button", { name: "取消处理", exact: true }).click();
    await page.getByText("已取消，可继续", { exact: true }).waitFor();
  });
  await check("Service errors explain recovery, quantities never show null and repeated signals are grouped", async () => {
    controlStatus = "failed"; hasCards = false;
    controlFailure = { code: "connection", message: "无法连接模型服务", action: "检查网络与模型设置后继续。" };
    await page.evaluate(() => loadMaterialImports());
    await page.getByRole("alert").filter({ hasText: "无法连接模型服务" }).waitFor();
    assert.match(await page.locator(".automatic-import-failure").innerText(), /已保留 0 张卡片/);
    assert.doesNotMatch(await page.locator(".automatic-import-plan").innerText(), /null|undefined/);
    assert.equal(await page.locator("#automatic-import-status progress").count(), 0);
    await page.getByText("本次尚未成功保存卡片", { exact: true }).waitFor();
    await page.getByText("处理说明（2 类）", { exact: true }).click();
    assert.equal(await page.locator(".automatic-processing-notes > p").count(), 3);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
    await page.screenshot({ path: path.join(directory, "failure-mobile.png"), fullPage: true });
    await page.setViewportSize({ width: 1280, height: 860 });
    await page.screenshot({ path: path.join(directory, "failure-desktop.png"), fullPage: true });
    await page.getByRole("button", { name: "检查模型设置", exact: true }).click();
    assert.equal(await page.evaluate(() => app.view), "settings");
    assert.equal(writes.filter(item => item.path === "/api/material-imports").length, 1);
  });
  assert.deepEqual(errors, []);
  fs.writeFileSync(path.join(directory, "report.json"), JSON.stringify({ results, errors, writes: writes.map(item => item.path) }, null, 2));
  console.log(`REPORT ${directory}`);
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => { if (browser) await browser.close(); server.close(); });
