"use strict";
// Real static UI, entirely fictional data and loopback HTTP. No personal store or model.
const assert = require("node:assert/strict");
const fs = require("node:fs"), path = require("node:path"), http = require("node:http");
const { chromium } = require("playwright");
const { project, browserOptions } = require("./browser_runtime.cjs");
const directory = path.join(project, ".ui-checks", "card-filters", new Date().toISOString().replace(/[:.]/g, "-"));
fs.mkdirSync(directory, { recursive: true });
const results = [], errors = [], writes = [];
const docA = { id: "text-a", title: "数据结构" }, docB = { id: "text-b", title: "计算机组成" }, docC = { id: "text-c", title: "待处理讲义" };
function card(id, doc, question, status, picture = false) {
  return { id, document_id: doc.id, question, concept: question.split(" ")[0], answer: "自编答案", status, version: 1, kind: "basic",
    evidence: [{ block_id: `${doc.id}-b1`, quote: "自编测试原文" }], rubric: [{ point_id: "p1", text: "说明规则" }],
    images: picture ? [{ media_id: "fixture-pic", side: "answer" }] : [] };
}
const cards = [card("a1", docA, "队列 FIFO 的顺序？", "approved", true), card("a2", docA, "栈 LIFO 的顺序？", "approved"),
  card("a3", docA, "机器指令 操作码的作用？", "draft"), { ...card("a4", docA, "检查中的示例图？", "draft", true), review_state: "running" },
  card("b1", docB, "基址寻址如何计算？", "approved"), card("b2", docB, "队列与处理器？", "approved", true),
  card("b3", docB, "已拒绝的卡片", "rejected"), { ...card("b4", docB, "来源过期卡片", "approved"), source_outdated: true }, card("c1", docC, "尚待处理的题目", "draft")];
const attempts = [cards[0], cards[4]].map((item, i) => ({ id: `attempt-${i}`, card_id: item.id, card_snapshot: item,
  source_snapshot: [], answer: "测试回答", status: "completed", created_at: "2026-10-27T00:00:00Z",
  diagnosis: { verdict: "correct", reason: "自编反馈", missing_point_ids: [], contradicted_point_ids: [] } }));
const data = { mode: "demo", configured: false, model: "fixture", documents: [docA, docB, docC], cards, attempts,
  blocks: [docA, docB, docC].map(doc => ({ block_id: `${doc.id}-b1`, document_id: doc.id, page: 1, text: "自编测试原文" })),
  media: [{ id: "fixture-pic", url: "/api/card-media/fixture-pic/file", source: { origin: "manual" } }], source_pages: [] };
let failNextDelete = false;
let materialImports = [{ id: "import-a", filename: "历史任务A.pdf", document_id: docA.id, status: "completed", message: "已有卡片" }];
const server = http.createServer((request, response) => {
  const pathname = new URL(request.url, "http://fixture.local").pathname;
  if (request.method !== "GET" && !pathname.endsWith("/generation-plan")) writes.push(pathname);
  if (request.method === "DELETE") {
    response.setHeader("Content-Type", "application/json; charset=utf-8");
    if (failNextDelete) { failNextDelete = false; response.writeHead(409); return response.end(JSON.stringify({ detail: "卡片数量已变化，请刷新后重新确认删除范围。" })); }
    const doc = pathname.match(/^\/api\/documents\/([^/]+)$/)?.[1];
    const id = pathname.match(/^\/api\/cards\/([^/]+)$/)?.[1];
    const removed = data.cards.filter(item => doc ? item.document_id === doc : item.id === id).map(item => item.id);
    data.cards = data.cards.filter(item => !removed.includes(item.id));
    data.attempts = data.attempts.filter(item => !removed.includes(item.card_id));
    if (doc) {
      data.documents = data.documents.filter(item => item.id !== doc);
      materialImports = materialImports.filter(item => item.document_id !== doc);
    }
    return response.end(JSON.stringify({ deleted_ids: removed, deleted_count: removed.length, deleted_document_id: doc || null }));
  }
  if (pathname.endsWith("/generation-plan")) {
    response.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
    return response.end(JSON.stringify({ target_count: 2, recommended_count: 2, source_fingerprint: "fixture", outline: [], warnings: [], stats: { batch_count: 1, coverage_ratio: 1 } }));
  }
  if (pathname === "/api/state" || pathname === "/api/material-imports") {
    response.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
    return response.end(JSON.stringify(pathname === "/api/state" ? data : { imports: materialImports }));
  }
  if (pathname === "/api/card-media/fixture-pic/file") {
    response.writeHead(200, { "Content-Type": "image/svg+xml" });
    return response.end('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="90"><text x="10" y="45">Fictional image</text></svg>');
  }
  const name = pathname === "/" ? "index.html" : pathname.startsWith("/static/") ? pathname.slice(8) : "";
  if (name && !name.includes("..") && !name.includes("\\")) {
    const file = path.join(project, "ankisense/static", name);
    if (fs.existsSync(file)) {
      response.writeHead(200, { "Content-Type": name.endsWith(".js") ? "text/javascript; charset=utf-8" : name.endsWith(".css") ? "text/css; charset=utf-8" : name.endsWith(".svg") ? "image/svg+xml" : "text/html; charset=utf-8" });
      return response.end(fs.readFileSync(file));
    }
  }
  response.writeHead(404); response.end('{}');
});
let browser, page;
async function check(name, fn) { await fn(); results.push({ name, status: "passed" }); console.log(`PASS ${name}`); }
(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch(browserOptions());
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.route("**/*", route => route.request().url().startsWith(base + "/") ? route.continue() : route.abort());
  page = await context.newPage(); page.on("pageerror", error => errors.push(error.message));
  await page.goto(base);
  await page.locator('[data-view="cards"]').click();
  await check("Results combine status, images and search with correct counts", async () => {
    assert.equal(await page.getByLabel("筛选卡牌状态").inputValue(), "ready");
    assert.equal(await page.locator(".automatic-card").count(), 2, "Default results contain only usable cards");
    await page.getByLabel("筛选卡牌状态").selectOption("all");
    assert.equal(await page.locator(".automatic-card").count(), 4);
    await page.getByLabel("筛选卡牌状态").selectOption("ready");
    assert.equal(await page.locator(".automatic-card").count(), 2);
    await page.getByLabel("筛选卡牌配图").selectOption("with");
    assert.equal(await page.locator(".automatic-card").count(), 1);
    await page.getByLabel("搜索卡牌", { exact: true }).fill("ｆｉｆｏ");
    assert.equal(await page.locator(".automatic-card").first().getAttribute("data-card-id"), "a1");
    await page.getByLabel("搜索卡牌", { exact: true }).fill("不存在");
    await page.getByText("没有匹配的卡片", { exact: true }).waitFor();
    assert.equal(await page.locator("#main").getByRole("button", { name: "开始练习", exact: true }).isDisabled(), true);
    await page.locator(".card-filter-toolbar").getByRole("button", { name: "清除筛选", exact: true }).click();
    await page.getByLabel("筛选卡牌状态").selectOption("checking");
    assert.equal(await page.locator(".automatic-card").first().getAttribute("data-card-id"), "a4");
    await page.getByLabel("筛选卡牌状态").selectOption("pending");
    assert.equal(await page.locator(".automatic-card").first().getAttribute("data-card-id"), "a3");
  });
  await check("Search survives background refresh and Chinese composition", async () => {
    await page.locator(".card-filter-toolbar").getByRole("button", { name: "清除筛选", exact: true }).click();
    const search = page.getByLabel("搜索卡牌", { exact: true });
    await search.fill("FIFO");
    const handle = await search.elementHandle();
    await search.evaluate(input => input.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true })));
    await page.evaluate(() => render());
    assert.equal(await handle.evaluate(node => node.isConnected && node === document.activeElement), true);
    await search.evaluate(input => input.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true })));
    assert.equal(await search.inputValue(), "FIFO");
    assert.equal(await search.evaluate(node => node === document.activeElement), true);
    await page.evaluate(() => render());
    assert.equal(await search.evaluate(node => node === document.activeElement), true);
    await page.screenshot({ path: path.join(directory, "results-filter.png"), fullPage: true });
    await handle.dispose();
  });
  await check("Starting practice carries the visible lecture and search scope", async () => {
    await page.locator("#main").getByRole("button", { name: "开始练习", exact: true }).click();
    assert.equal(await page.getByLabel("选择练习讲义").inputValue(), docA.id);
    assert.equal(await page.getByLabel("搜索练习卡片").inputValue(), "FIFO");
    assert.equal(await page.getByLabel("选择练习卡片").locator("option").count(), 1);
    assert.equal(await page.getByRole("button", { name: "下一张 →", exact: true }).isDisabled(), true);
  });
  await check("Practice progress and navigation stay inside the chosen lecture", async () => {
    await page.getByLabel("搜索练习卡片").fill("");
    assert.match(await page.locator(".practice-progress-count").innerText(), /1\s*\/\s*2/);
    await page.getByLabel("选择练习卡片").selectOption("a1");
    await page.getByRole("button", { name: "下一张 →", exact: true }).click();
    assert.equal(await page.getByLabel("选择练习卡片").inputValue(), "a2");
    assert.equal(await page.getByRole("button", { name: "下一张 →", exact: true }).isDisabled(), true);
    assert.equal(await page.locator(".attempt-list .attempt-item").count(), 1);
  });
  await check("Filtering practice preserves typed answers and completion categories", async () => {
    await page.getByLabel("你的回答", { exact: true }).fill("尚未提交的回答");
    await page.locator('[data-practice-filter="uncompleted"]').click();
    await page.getByLabel("选择练习讲义").selectOption(docB.id);
    assert.equal(await page.getByLabel("选择练习卡片").inputValue(), "b2");
    await page.getByLabel("选择练习讲义").selectOption(docA.id);
    assert.equal(await page.getByLabel("你的回答", { exact: true }).inputValue(), "尚未提交的回答");
    await page.getByLabel("筛选练习配图").selectOption("with");
    assert.match(await page.locator(".practice-progress-count").innerText(), /1\s*\/\s*1/);
    await page.locator('[data-practice-filter="completed"]').click();
    assert.equal(await page.getByLabel("选择练习卡片").inputValue(), "a1");
    await page.getByLabel("搜索练习卡片").fill("不存在");
    await page.getByText("没有匹配的练习卡片", { exact: true }).waitFor();
    await page.locator(".practice-scope-toolbar").getByRole("button", { name: "清除筛选", exact: true }).click();
    assert.equal(await page.getByLabel("选择练习讲义").inputValue(), "all");
    assert.match(await page.locator(".practice-progress-count").innerText(), /2\s*\/\s*4/);
  });
  await check("Practice search retains focus while typing and the layout fits narrow screens", async () => {
    const search = page.getByLabel("搜索练习卡片");
    await search.fill("队列");
    assert.equal(await search.evaluate(node => node === document.activeElement), true);
    assert.equal(await page.getByLabel("选择练习卡片").locator("option").count(), 2);
    await page.screenshot({ path: path.join(directory, "practice-filter.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
    await page.screenshot({ path: path.join(directory, "practice-mobile.png"), fullPage: true });
    await page.locator('[data-view="cards"]').click();
    await page.getByLabel("查看哪份讲义的卡牌").selectOption(docB.id);
    assert.equal(await page.locator(".automatic-card").count(), 2, "Rejected and outdated cards stay outside results");
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
    await page.screenshot({ path: path.join(directory, "results-mobile.png"), fullPage: true });
  });
  await check("The workbench study button opens the current lecture rather than a previous scope", async () => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.locator('[data-view="practice"]').click();
    await page.getByLabel("选择练习讲义").selectOption(docA.id);
    await page.getByLabel("搜索练习卡片").fill("FIFO");
    await page.locator('[data-view="cards"]').click();
    await page.getByLabel("查看哪份讲义的卡牌").selectOption(docB.id);
    await page.locator('.automatic-card[data-card-id="b1"]').getByRole("button", { name: "编辑卡牌", exact: true }).click();
    await page.locator(".page-actions").getByRole("button", { name: "开始练习 →", exact: true }).click();
    assert.equal(await page.getByLabel("选择练习讲义").inputValue(), docB.id);
    assert.equal(await page.getByLabel("选择练习卡片").inputValue(), "b1");
  });
  await check("An edited card must be saved before the workbench starts practice", async () => {
    await page.locator('[data-view="cards"]').click();
    await page.getByLabel("查看哪份讲义的卡牌").selectOption(docB.id);
    await page.locator('.automatic-card[data-card-id="b1"]').getByRole("button", { name: "编辑卡牌", exact: true }).click();
    await page.locator(".workbench-editor").getByLabel("问题", { exact: true }).fill("未保存的新题目");
    const study = page.locator(".page-actions").getByRole("button", { name: "开始练习 →", exact: true });
    assert.equal(await study.isDisabled(), true);
    assert.match(await study.getAttribute("title"), /保存/);
  });
  await check("The workbench cannot start a different lecture when its current lecture has no ready cards", async () => {
    await page.locator('[data-view="cards"]').click();
    await page.getByLabel("查看哪份讲义的卡牌").selectOption(docC.id);
    assert.equal(await page.locator(".automatic-card").count(), 0, "Pending cards are not default learning results");
    await page.getByRole("button", { name: "查看待处理草稿", exact: true }).click();
    await page.locator('.automatic-card[data-card-id="c1"]').getByRole("button", { name: "编辑卡牌", exact: true }).click();
    assert.equal(await page.locator(".page-actions").getByRole("button", { name: "开始练习 →", exact: true }).isDisabled(), true);
  });
  await check("Offline math renders in cards, live previews and feedback while editors retain source", async () => {
    const item = data.cards.find(card => card.id === "b2");
    item.images = [];
    item.question = String.raw`求 $x_1^2+x_2^2$，并计算 \(\frac{a}{b}\)。`;
    item.answer = String.raw`分式 $$\frac{1}{1+x^2}$$ 矩阵 $$\begin{bmatrix}1&2\\3&4\end{bmatrix}$$`;
    data.attempts.push({ ...attempts[0], id: "math-attempt", card_id: item.id, card_snapshot: item,
      answer: "$x=1$", diagnosis: { ...attempts[0].diagnosis, reason: String.raw`由 \(x_1^2+x_2^2=1\) 得到结果。` } });
    await page.evaluate(async () => refresh());
    await page.locator('[data-view="cards"]').click();
    await page.getByLabel("查看哪份讲义的卡牌").selectOption(docB.id);
    const article = page.locator('.automatic-card[data-card-id="b2"]');
    assert.equal(await article.locator("h2 .katex").count(), 2);
    await article.getByRole("button", { name: "编辑卡牌", exact: true }).click();
    const question = page.locator(".workbench-editor").getByLabel("问题", { exact: true });
    assert.equal(await question.inputValue(), item.question);
    await page.locator(".preview-face-tabs").getByRole("button", { name: "背面", exact: true }).click();
    assert.equal(await page.locator(".preview-answer .katex").count(), 2);
    const edited = String.raw`更新 \(\sum_{i=1}^{n}i\)，错误公式 $\frac{1}{$`;
    await question.fill(edited);
    assert.equal(await question.inputValue(), edited);
    assert.equal(await page.locator(".preview-question .katex").count(), 1);
    assert.equal(await page.locator(".preview-question .math-source-fallback").count(), 1);
    await question.fill(item.question);
    await page.screenshot({ path: path.join(directory, "math-workbench.png"), fullPage: true });
    await page.locator("#card-live-preview").screenshot({ path: path.join(directory, "math-card-preview.png") });
    await page.locator('[data-view="practice"]').click();
    await page.getByLabel("选择练习讲义").selectOption(docB.id);
    await page.getByLabel("搜索练习卡片").fill("");
    await page.getByLabel("选择练习卡片").selectOption(item.id);
    assert.equal(await page.locator("#practice-question .katex").count(), 2);
    assert.equal(await page.locator("#practice-feedback .katex").count() > 0, true);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
    await page.screenshot({ path: path.join(directory, "math-practice-mobile.png"), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1000 });
    const safety = await page.evaluate(() => {
      const node = document.createElement("p");
      MathText.set(node, String.raw`<img src=x onerror=alert(1)> $\href{javascript:alert(1)}{x}$`);
      return { images: node.querySelectorAll("img").length, links: node.querySelectorAll("a").length };
    });
    assert.deepEqual(safety, { images: 0, links: 0 });
  });
  await check("Delete confirmation is cancellable and does not send a request", async () => {
    await page.locator('[data-view="cards"]').click();
    await page.getByLabel("查看哪份讲义的卡牌").selectOption(docB.id);
    await page.locator('.automatic-card[data-card-id="b1"]').getByRole("button", { name: "删除", exact: true }).click();
    await page.getByRole("dialog").getByText(/已同步到 Anki/).waitFor();
    await page.getByRole("dialog").getByRole("button", { name: "取消", exact: true }).click();
    assert.equal(writes.length, 0);
    assert.equal(await page.locator('.automatic-card[data-card-id="b1"]').count(), 1);
  });
  await check("Single deletion clears the card and its unsaved draft without touching another lecture", async () => {
    failNextDelete = true;
    await page.locator('.automatic-card[data-card-id="b1"]').getByRole("button", { name: "删除", exact: true }).click();
    await page.getByRole("dialog").getByRole("button", { name: "确认删除", exact: true }).click();
    await page.getByRole("dialog").getByRole("alert").getByText(/卡片数量已变化/).waitFor();
    assert.equal(data.cards.some(item => item.id === "b1"), true);
    await page.getByRole("dialog").getByRole("button", { name: "重试删除", exact: true }).click();
    await page.waitForFunction(() => !app.data.cards.some(card => card.id === "b1"));
    assert.equal(await page.evaluate(() => Boolean(app.cardDrafts.b1)), false);
    assert.equal(data.cards.filter(item => item.document_id === docA.id).length, 4);
    assert.equal(writes.filter(item => item === "/api/cards/b1").length, 2);
  });
  await check("Material deletion removes the lecture and its task row as well as cards", async () => {
    await page.getByLabel("查看哪份讲义的卡牌").selectOption(docA.id);
    await page.locator(".card-material-more > summary").click();
    await page.getByRole("button", { name: "删除本材料及卡片", exact: true }).click();
    await page.getByRole("dialog").getByRole("heading", { name: "删除本材料及卡片？" }).waitFor();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(directory, "delete-confirm-mobile.png"), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true,
      JSON.stringify(await page.evaluate(() => [...document.querySelectorAll("body *")].filter(node => node.getBoundingClientRect().right > innerWidth + 1).slice(-8).map(node => [node.className, node.getBoundingClientRect().right]))));
    await page.getByRole("dialog").getByRole("button", { name: "确认删除", exact: true }).click();
    await page.waitForFunction(() => !app.data.cards.some(card => card.document_id === "text-a"));
    await page.waitForFunction(() => !app.data.documents.some(doc => doc.id === "text-a"));
    assert.notEqual(await page.getByLabel("查看哪份讲义的卡牌").inputValue(), docA.id);
    assert.equal(data.cards.some(item => item.id === "b2"), true);
    assert.equal(await page.getByText("历史任务A.pdf", { exact: true }).count(), 0);
    await page.reload();
    await page.locator('[data-view="cards"]').click();
    assert.equal(await page.getByLabel("查看哪份讲义的卡牌").locator('option[value="text-a"]').count(), 0);
    data.documents.push({ id: "empty-doc", title: "已经清空的材料" });
    await page.evaluate(async () => refresh());
    await page.getByLabel("查看哪份讲义的卡牌").selectOption("empty-doc");
    await page.locator(".card-material-more > summary").click();
    await page.getByRole("button", { name: "删除本材料及卡片", exact: true }).click();
    await page.getByRole("dialog").getByText(/0 张卡片/).waitFor();
    await page.getByRole("dialog").getByRole("button", { name: "确认删除", exact: true }).click();
    await page.waitForFunction(() => !app.data.documents.some(doc => doc.id === "empty-doc"));
  });
  assert.deepEqual(errors, []); assert.deepEqual(writes, ["/api/cards/b1", "/api/cards/b1", "/api/documents/text-a", "/api/documents/empty-doc"]);
})().catch(error => { errors.push(error.stack || String(error)); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close(); await new Promise(resolve => server.close(resolve));
  fs.writeFileSync(path.join(directory, "report.json"), JSON.stringify({ results, errors, writes, model_requests: 0 }, null, 2));
  console.log(`REPORT ${directory}`); if (errors.length) console.error(errors.join("\n"));
});
