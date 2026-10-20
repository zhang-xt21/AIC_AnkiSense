"use strict";
// Isolated UI fixture: no user files, API keys, real database or model calls.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const playwright = require("playwright");
const { project, browserOptions } = require("./browser_runtime.cjs");
const directory = path.join(project, ".ui-checks", "automatic-import", new Date().toISOString().replace(/[:.]/g, "-"));
fs.mkdirSync(directory, { recursive: true });
const errors = [], results = [];
let submitted = 0, started = 0, manualStarts = 0;
const doc = { id: "pdf-automatic-fixture", title: "自动导入示例讲义", filename: "lecture.pptx", automatic_import: true };
const card = { id: "autcard-example", document_id: doc.id, question: "栈遵循什么顺序？", answer: "后进先出。", concept: "栈", status: "approved", approval_method: "automatic", version: 1, kind: "basic", rubric: [{ point_id: "order", text: "后进先出" }], evidence: [{ block_id: "b1", quote: "栈遵循后进先出。" }], images: [], auto_quality: { verdict: "clear", card_version: 1, issues: [] } };
function state() { return { mode: "cloud", configured: true, model: "fixture-visual", documents: submitted ? [doc] : [], blocks: submitted ? [{ block_id: "b1", document_id: doc.id, page: 1, text: "栈遵循后进先出。" }] : [], cards: started && Date.now() - started > 1200 ? [card] : [], source_pages: [], attempts: [], media: [] }; }
function imports() {
  if (!submitted) return [];
  const done = Date.now() - started > 4000;
  return [{ id: "import-example", filename: "lecture.pptx", document_id: doc.id, status: done ? "completed" : "running", message: done ? "已生成 1 张卡牌，可直接查看。" : "正在自动处理讲义，无需操作。", job: { id: "job-example", status: done ? "completed" : "running", card_ids: state().cards.map(c => c.id), progress: { completed: done ? 1 : 0, total: 1 }, issues: [] } }];
}
function json(response, value, status = 200) { response.writeHead(status, { "Content-Type": "application/json; charset=utf-8" }); response.end(JSON.stringify(value)); }
const server = http.createServer(async (request, response) => {
  const pathname = new URL(request.url, "http://localhost").pathname;
  if (pathname === "/api/state") return json(response, state());
  if (pathname === "/api/material-imports" && request.method === "POST") {
    for await (const chunk of request) { /* Consume fixture multipart body. */ }
    submitted++; started = Date.now(); return json(response, imports()[0]);
  }
  if (pathname === "/api/material-imports") return json(response, { imports: imports() });
  if (pathname.endsWith("/auto-card-jobs")) { if (request.method === "POST") manualStarts++; return json(response, { jobs: [] }); }
  const relative = pathname === "/" ? "index.html" : pathname.startsWith("/static/") ? pathname.slice(8) : null;
  if (relative && !relative.includes("..") && !relative.includes("\\")) {
    const file = path.join(project, "ankisense", "static", relative);
    if (fs.existsSync(file)) { response.writeHead(200, { "Content-Type": file.endsWith(".js") ? "text/javascript; charset=utf-8" : file.endsWith(".css") ? "text/css; charset=utf-8" : file.endsWith(".svg") ? "image/svg+xml" : "text/html; charset=utf-8" }); return response.end(fs.readFileSync(file)); }
  }
  return json(response, { detail: "Fixture route missing" }, 404);
});
let browser;
async function check(name, callback) { await callback(); results.push({ name, status: "passed" }); console.log("PASS " + name); }
(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  browser = await playwright.chromium.launch(browserOptions());
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/*", route => route.request().url().startsWith(base + "/") ? route.continue() : route.abort());
  await page.goto(base);
  await check("Default entry accepts PPT/PPTX/PDF and hides optional manual tools", async () => {
    await page.getByLabel("选择讲义，自动制卡").waitFor();
    assert.equal(await page.getByLabel("选择讲义，自动制卡").getAttribute("accept"), ".ppt,.pptx,.pdf");
    assert.equal(await page.locator("#manual-tools").evaluate(n => n.open), false);
    await page.screenshot({ path: path.join(directory, "import-desktop.png"), fullPage: true });
  });
  await check("Selecting a file alone starts work and automatically opens results", async () => {
    await page.getByLabel("选择讲义，自动制卡").setInputFiles({ name: "lecture.pptx", mimeType: "application/vnd.openxmlformats-officedocument.presentationml.presentation", buffer: Buffer.from("fixture content") });
    await page.locator('#main[data-view="cards"]').waitFor();
    assert.equal(submitted, 1);
    assert.equal(manualStarts, 0);
  });
  await check("Reload resumes the active result view and cards appear without any extra click", async () => {
    await page.reload();
    await page.locator('#main[data-view="cards"]').waitFor();
    await page.locator(".automatic-card").waitFor({ timeout: 15000 });
    assert.equal(submitted, 1);
    assert.equal(manualStarts, 0);
    await page.getByText("已自动检查", { exact: true }).waitFor();
    await page.getByText("显示答案", { exact: true }).click();
    await page.getByText("后进先出。", { exact: true }).waitFor();
    await page.screenshot({ path: path.join(directory, "results-desktop.png"), fullPage: true });
  });
  await check("Result cards fit a narrow screen", async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
    await page.screenshot({ path: path.join(directory, "results-mobile.png"), fullPage: true });
  });
  await check("Completed result link survives another reload without uploading again", async () => {
    await page.locator('[data-import-status="completed"]').waitFor({ timeout: 15000 });
    await page.reload();
    await page.locator('#main[data-view="cards"] .automatic-card').waitFor();
    assert.equal(submitted, 1);
    assert.equal(manualStarts, 0);
  });
  assert.deepEqual(errors, []);
  fs.writeFileSync(path.join(directory, "report.json"), JSON.stringify({ results, errors, submitted, manualStarts }, null, 2));
  console.log(`REPORT ${directory}`);
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => { if (browser) await browser.close(); server.close(); });
