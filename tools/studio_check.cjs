"use strict";

// Browser acceptance check for the unified lecture-to-card workspace. It uses
// only the repository's isolated PDF fixture, a fresh SQLite directory, and a
// mocked machine-review read. No personal data or cloud model is contacted.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");
const { project, pythonExecutable, browserOptions } = require("./browser_runtime.cjs");
const navigation = require("./browser_navigation.cjs");

const base = `http://127.0.0.1:${process.env.ANKISENSE_UI_PORT || 8976}`;
const output = path.join(project, ".ui-checks", "20260927-workbench");
const runDir = path.join(output, `studio-${new Date().toISOString().replace(/[:.]/g, "-")}`);
const failures = [];
const blockedRequests = [];
let browser;
let server;
let page;

fs.mkdirSync(runDir, { recursive: true });

async function api(url) {
  const response = await fetch(base + url);
  assert.equal(response.status, 200, `${url}: ${response.status}`);
  return response.json();
}

async function check(name, action) {
  await action();
  process.stdout.write(`PASS ${name}\n`);
}

async function screenshot(name) {
  await page.locator("#toast").waitFor({ state: "hidden", timeout: 10000 });
  await page.evaluate(() => window.scrollTo({ top: 0, left: 0, behavior: "instant" }));
  await page.screenshot({ path: path.join(runDir, `${name}.png`), fullPage: true });
}

async function startFixture() {
  // Never connect to an unrelated process that happens to own the fixture port.
  try {
    await fetch(`${base}/health`, { signal: AbortSignal.timeout(500) });
    throw new Error("Port 8976 is in use; stop the prior isolated fixture before this check.");
  } catch (error) {
    if (!["TimeoutError", "TypeError"].includes(error.name)) throw error;
  }
  const log = fs.openSync(path.join(runDir, "server.log"), "a");
  server = spawn(pythonExecutable(), ["-X", "utf8", path.join(project, "tools", "fast_draft_fixture.py")], {
    cwd: project,
    windowsHide: true,
    env: { ...process.env, ANKISENSE_UI_DATA: path.join(runDir, "data") },
    stdio: ["ignore", log, log],
  });
  server.on("error", error => failures.push(`Fixture: ${error.message}`));
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (server.exitCode !== null) throw new Error(`Fixture exited (${server.exitCode}); inspect ${path.join(runDir, "server.log")}`);
    try {
      const status = await api("/health");
      if (status.mode === "demo") return;
    } catch { /* Startup is still in progress. */ }
    await new Promise(resolve => setTimeout(resolve, 150));
  }
  throw new Error(`Fixture did not start; inspect ${path.join(runDir, "server.log")}`);
}

async function selectDocument(title) {
  await navigation.openMaterials(page);
  const row = page.locator(".document-row").filter({ hasText: title });
  await row.click();
}

async function stage(name) {
  await navigation.stage(page, name);
}

async function selectCard(cardId) {
  await page.getByLabel("选择编辑卡片", { exact: true }).selectOption(cardId);
  await page.locator(".workbench-editor").waitFor();
}

(async () => {
  await startFixture();
  const state = await api("/api/state");
  const card = state.cards.find(item => item.id === "fixture-pdf-stack");
  assert.ok(card, "The isolated PDF card must exist");
  const lecture = state.documents.find(item => item.id === card.document_id);
  assert.ok(lecture, "The fixture card must retain its source document");

  browser = await chromium.launch(browserOptions());
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: "block" });
  await context.route("**/*", route => {
    const url = route.request().url();
    if (url.startsWith(`${base}/`) || url.startsWith("data:")) return route.continue();
    blockedRequests.push(url);
    return route.abort();
  });
  page = await context.newPage();
  page.on("pageerror", error => failures.push(error.message));
  let fastState = true;
  let fastGenerated = false;
  const fastDraft = { ...card, status: "draft" };
  const draftCalls = [];
  const proofCalls = [];
  const fastJob = { id: "offline-generation-job", document_id: lecture.id, status: "completed", target_count: 3, generated_count: 1, completed_batches: 1, total_batches: 1, card_ids: [fastDraft.id], warnings: [], created_at: "2026-10-16T08:00:00Z" };
  await page.route("**/api/state", async route => {
    if (!fastState) return route.continue();
    const response = await route.fetch();
    const payload = await response.json();
    payload.mode = "cloud";
    payload.configured = true;
    payload.model = "offline-fixture";
    payload.documents.find(item => item.id === lecture.id).warning = "请逐页对照原图校对并确认，再选择片段制卡；页码为 PDF 物理页码。以下页面处理失败：2。";
    payload.cards = fastGenerated ? [fastDraft] : [];
    payload.generation_jobs = fastGenerated ? [fastJob] : [];
    for (const item of payload.source_pages) if (item.document_id === lecture.id) {
      item.status = item.page === 1 ? "pending" : "failed";
      item.reviewed_at = null;
    }
    payload.blocks.push({ block_id: "stale-source-block", document_id: lecture.id, page: 1, text: "An outdated revision must not be offered for generation." });
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(payload) });
  });
  await page.route("**/generation-plan", route => {
    if (!fastState) return route.continue();
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      document_id: lecture.id, scope: "all", source_fingerprint: "offline-fingerprint", recommended_count: 3, target_count: 3,
      suggestion_reason: "根据本地有效原文估计。", warnings: [], stats: { source_page_count: 1, source_block_count: 1, source_chars: 200, batch_count: 1, coverage_ratio: 1 },
    }) });
  });
  await page.route("**/api/generation-jobs", route => {
    assert.equal(fastState, true, "Only the isolated fast-generation scenario may call generation");
    draftCalls.push(route.request().postDataJSON());
    fastGenerated = true;
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(fastJob) });
  });
  await page.route(/\/api\/documents\/[^/]+\/pages\/\d+(?:\/ai-proofread)?$/, route => {
    proofCalls.push(route.request().url());
    if (!fastState) return route.continue();
    return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "No proofreading request belongs in the quick draft flow" }) });
  });
  await page.route(`**/api/cards/${card.id}/auto-review`, route => {
    if (route.request().method() !== "GET") return route.continue();
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
      status: "auto_pass", current: true, reasons: [], checks: {
        question_answerable: true, answer_relevant: true, answer_supported: true,
        rubric_checks: [{ point_id: "fixture_lifo", verdict: "supported", block_id: card.evidence[0].block_id, exact_quote: card.evidence[0].quote }],
        answer_citations: [{ block_id: card.evidence[0].block_id, exact_quote: card.evidence[0].quote }],
      },
    }) });
  });
  await page.goto(base);

  await check("Pending source text generates a draft without page confirmation or visual proofreading", async () => {
    await selectDocument(lecture.title);
    assert.equal(await page.locator("#source-proofreader").count(), 1, "Source checking is available only after expanding advanced tools");
    assert.deepEqual(proofCalls, [], "Opening advanced tools never starts paid proofreading");
    assert.doesNotMatch(await page.locator("#manual-tools").innerText(), /请逐页对照原图校对并确认，再选择片段制卡/);
    assert.match(await page.locator("#manual-tools").innerText(), /以下页面处理失败：2/);
    const currentBlock = state.blocks.find(item => item.block_id === card.evidence[0].block_id);
    await page.getByText("高级设置 · 指定出题范围", { exact: true }).click();
    const pendingSource = page.locator("#manual-tools .source-block").filter({ hasText: currentBlock.block_id });
    assert.match(await pendingSource.innerText(), /已提取/);
    assert.equal(await pendingSource.getByRole("checkbox").isChecked(), false);
    const failedSource = page.locator("#manual-tools .source-block").filter({ hasText: "识别失败" });
    assert.equal(await failedSource.getByRole("checkbox").isDisabled(), true);
    assert.equal(await page.getByText("An outdated revision must not be offered for generation.").count(), 0);
    assert.equal(await page.evaluate(() => blockCanGenerate({ document_id: app.documentId, page: 1, block_id: "stale-source-block", text: "outdated" })), false);
    assert.equal(await page.evaluate(() => blockCanGenerate({ ...app.data.blocks.find(block => block.document_id === app.documentId), text: " " })), false);
    await page.getByText("高级设置 · 指定出题范围", { exact: true }).click();
    await screenshot("studio-fast-source");
    await page.getByRole("button", { name: "开始制卡 →", exact: true }).click();
    await page.getByRole("button", { name: "查看已生成卡片", exact: true }).click();
    await page.waitForFunction(id => document.querySelector(".workbench-grid")?.dataset.cardId === id, fastDraft.id);
    assert.equal(await page.evaluate(() => app.view), "review");
    assert.equal(draftCalls.length, 1);
    assert.equal(draftCalls[0].block_ids, null);
    assert.equal(draftCalls[0].count, 3);
    assert.equal(draftCalls[0].source_fingerprint, "offline-fingerprint");
    assert.deepEqual(proofCalls, []);
    assert.match(await page.locator(".workbench-editor").innerText(), /待处理/);
    assert.match(await page.locator(".workbench-source").innerText(), /无需逐页确认/);
    await screenshot("studio-fast-draft");
    await page.locator(".workbench-source").getByRole("button", { name: "检查第 1 页原文", exact: true }).click();
    assert.equal(await page.locator("#proof-page-selector").inputValue(), "1");
    await page.getByLabel("第 1 页校对文字", { exact: true }).fill(`${currentBlock.text}\nUnsaved correction.`);
    assert.match(await page.locator("#source-generation-note").innerText(), /所选页面有未保存修改.*使用已保存原文/);
    await page.getByRole("button", { name: "放弃修改，恢复已保存文字" }).click();
    assert.match(await page.locator("#source-generation-note").innerText(), /不会自动采纳/);
    await page.locator("#manual-tools > summary").click();
    await page.setViewportSize({ width: 390, height: 844 });
    const width = await page.evaluate(() => ({ viewport: document.documentElement.clientWidth, content: document.documentElement.scrollWidth }));
    assert.ok(width.content <= width.viewport + 1, "The fast source screen must fit a phone");
    await screenshot("studio-fast-mobile");
    await page.setViewportSize({ width: 1440, height: 1000 });
    fastState = false;
    await page.reload();
  });

  await check("Confirming unchanged source preserves draft evidence and allows later approval", async () => {
    const before = (await api("/api/state")).source_pages.find(item => item.document_id === lecture.id && item.page === 1);
    assert.equal(before.status, "pending");
    await stage("card");
    await selectCard(card.id);
    await page.locator(".workbench-source").getByRole("button", { name: "检查第 1 页原文", exact: true }).click();
    assert.equal(await page.locator("#proof-page-selector").inputValue(), "1");
    await page.getByRole("button", { name: "保存原文", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("#proof-page-selector")?.selectedOptions[0]?.textContent.includes("已人工核对"));
    const after = (await api("/api/state")).source_pages.find(item => item.document_id === lecture.id && item.page === 1);
    assert.equal(after.revision, before.revision);
    assert.deepEqual(after.block_ids, before.block_ids);
    assert.equal(after.status, "reviewed");
    assert.notEqual((await api("/api/state")).cards.find(item => item.id === card.id).source_outdated, true);
    await page.locator("#manual-tools > summary").click();
  });

  await check("A source document stays selected while moving through the studio", async () => {
    await selectDocument(lecture.title);
    assert.equal(await page.evaluate(() => app.view), "materials");
    assert.equal(await page.evaluate(() => app.documentId), lecture.id);
    await screenshot("studio-source-collapsed");
    await page.getByRole("button", { name: "检查原文 ↓", exact: true }).click();
    await page.locator("#source-proofreader").waitFor();
    assert.equal(await page.locator("#proof-page-selector").inputValue(), "1");
    await page.locator("#proof-page-selector").selectOption("2");
    assert.equal(await page.locator("#proof-page-selector").inputValue(), "2");
    await page.waitForFunction(() => {
      const image = document.querySelector("#source-proofreader .proof-page-image");
      return image?.complete && image.naturalWidth > 0;
    });
    await screenshot("studio-source-expanded");
    // A second page is useful here: a later card switch must not silently
    // select another course or reset the user's proofreading position.
  });

  await check("A proofreading edit survives a step change without an implicit save", async () => {
    const input = page.getByLabel("第 2 页校对文字", { exact: true });
    const before = await input.inputValue();
    await input.fill(`${before}\nUncommitted studio check.`);
    assert.match(await page.locator(".proof-draft-status").innerText(), /未保存/);
    await stage("card");
    await selectCard(card.id);
    assert.equal(await page.evaluate(() => app.documentId), lecture.id);
    await stage("source");
    assert.equal(await page.locator("#proof-page-selector").inputValue(), "2");
    assert.equal(await page.getByLabel("第 2 页校对文字", { exact: true }).inputValue(), `${before}\nUncommitted studio check.`);
    const after = (await api("/api/state")).source_pages.find(item => item.document_id === lecture.id && item.page === 2);
    assert.equal(after.text, before, "Navigating must not save a proofreading draft implicitly");
  });

  await check("A card edit survives source and card step changes", async () => {
    await stage("card");
    await selectCard(card.id);
    const question = page.locator(".workbench-editor").getByLabel("问题", { exact: true });
    const edited = `${card.question} (pending)`;
    await question.fill(edited);
    await stage("source");
    await stage("card");
    await selectCard(card.id);
    assert.equal(await page.locator(".workbench-editor").getByLabel("问题", { exact: true }).inputValue(), edited);
    assert.equal((await api("/api/state")).cards.find(item => item.id === card.id).question, card.question);
    await page.locator(".workbench-editor").getByRole("button", { name: "保存并检查", exact: true }).click();
    await page.waitForFunction(id => !document.querySelector(".workbench-save-bar")?.textContent.includes("未保存") && document.querySelector(".workbench-grid")?.dataset.cardId === id, card.id);
    assert.equal((await api("/api/state")).cards.find(item => item.id === card.id).question, edited);
  });

  await check("A legacy pass does not invent approval; manual handling stays an explicit exception", async () => {
    await stage("audit");
    await page.locator(".card-auto-review-status").filter({ hasText: "机器检查通过，学习状态尚未更新" }).waitFor();
    await screenshot("studio-audit");
    const before = (await api("/api/state")).cards.find(item => item.id === card.id);
    assert.equal(before.status, "draft");
    await page.getByText("自行核对并处理", { exact: true }).click();
    await page.locator(".workbench-editor").getByRole("button", { name: "人工核对并批准", exact: true }).click();
    await page.waitForFunction(id => document.querySelector(".workbench-grid")?.dataset.cardId === id && document.querySelector(".workbench-editor")?.textContent.includes("人工批准"), card.id);
    const approved = (await api("/api/state")).cards.find(item => item.id === card.id);
    assert.equal(approved.status, "approved");
    await page.locator('button[data-view="practice"]').click();
    await page.getByLabel("选择练习卡片", { exact: true }).selectOption(card.id);
    assert.match(await page.locator("#practice-question").innerText(), /stack/i);
  });

  await check("Studio stages remain usable at phone width", async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('button[data-view="materials"]').click();
    for (const name of ["source", "card", "audit"]) {
      await stage(name);
      const width = await page.evaluate(() => ({ viewport: document.documentElement.clientWidth, content: document.documentElement.scrollWidth }));
      assert.ok(width.content <= width.viewport + 1, `${name} overflows at phone width: ${JSON.stringify(width)}`);
    }
  });

  assert.deepEqual(blockedRequests, [], "The studio test must not request external resources");
  assert.deepEqual(failures, [], "The studio must not raise browser or fixture errors");
  process.stdout.write(`Screenshots: ${path.join(runDir, "studio-source-collapsed.png")}, ${path.join(runDir, "studio-source-expanded.png")}, ${path.join(runDir, "studio-audit.png")}\n`);
})().catch(async error => {
  process.exitCode = 1;
  process.stderr.write(`${error.stack || error}\n`);
  if (page) {
    try { await page.screenshot({ path: path.join(runDir, "failure.png"), fullPage: true }); } catch { /* Diagnostic only. */ }
  }
}).finally(async () => {
  if (browser) await browser.close();
  if (server && server.exitCode === null) server.kill();
});
