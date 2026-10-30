"use strict";
// Setup UI with controlled localhost fixtures. No real Anki profile is used.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { project, browserOptions } = require("./browser_runtime.cjs");
const directory = path.join(project, ".ui-checks", "anki-setup-browser", new Date().toISOString().replace(/[:.]/g, "-"));
fs.mkdirSync(directory, { recursive: true });
const state = { connection: { enabled: false }, connected: false, addon_version: "0.2.0", addon_available: true, last_sync: null };
const native = { executable: "C:\\Program Files\\Anki\\anki.exe", dataDirectory: "F:\\Anki-test", dataReady: true, running: false, addon: null };
let installFails = false, statusFails = false;
const actions = [], errors = [], results = [];
const server = http.createServer((request, response) => {
  if (request.url.startsWith("/api/anki/setup")) {
    if (statusFails) { response.writeHead(503, { "Content-Type": "application/json" }); response.end(JSON.stringify({ detail: "测试：连接状态暂不可用" })); return; }
    if (request.method === "POST") { state.connection.enabled = true; state.connection_file = "F:\\fixture\\plugin-connection.json"; }
    response.setHeader("Content-Type", "application/json"); response.end(JSON.stringify(state)); return;
  }
  const file = request.url === "/anki-setup.js" ? "anki-setup.js" : request.url === "/styles.css" ? "styles.css" : null;
  if (file) { response.setHeader("Content-Type", file.endsWith("js") ? "text/javascript" : "text/css"); response.end(fs.readFileSync(path.join(project, "ankisense", "static", file))); return; }
  response.end('<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/styles.css"><main style="padding:16px"></main><script src="/anki-setup.js"></script>');
});
let browser;
async function check(name, fn) { await fn(); results.push(name); console.log("PASS " + name); }
(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  browser = await chromium.launch(browserOptions());
  const page = await browser.newPage({ viewport: { width: 1280, height: 860 } });
  page.on("pageerror", error => errors.push(error.message));
  await page.exposeFunction("nativeAction", async action => {
    actions.push(action);
    if (action === "install") { if (installFails) throw new Error("测试：安装失败，请检查目录"); native.addon = { installed: true, version: "0.2.0" }; return { changed: true, native }; }
    if (action === "launch") return { message: "已启动测试 Anki" };
    return native;
  });
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.evaluate(() => {
    window.ankisenseDesktop = { ankiSetup: action => window.nativeAction(action) };
    document.querySelector("main").append(AnkiSetup.create({ api: async (url, options) => { const response = await fetch(url, options); const body = await response.json(); if (!response.ok) throw new Error(body.detail); return body; }, jsonOptions: (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) }));
  });
  await check("Opening the guide detects only; no silent installation", async () => {
    assert.equal(actions.length, 0);
    await page.getByText("直接连接 Anki · 安装与连接向导", { exact: true }).click();
    await page.getByRole("button", { name: "安装并配对插件", exact: true }).waitFor();
    assert.deepEqual(actions, ["detect"]);
    assert.equal(await page.locator('[data-connected="false"]').count(), 1);
  });
  await check("Failed installation is visible and never claims connected", async () => {
    installFails = true;
    await page.getByRole("button", { name: "安装并配对插件", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "安装失败" }).waitFor();
    assert.equal(await page.locator('[data-connected="true"]').count(), 0);
  });
  await check("Install success still waits for a real plugin heartbeat", async () => {
    installFails = false;
    await page.getByRole("button", { name: "安装并配对插件", exact: true }).click();
    await page.getByText("插件已安装并配对，请重新打开 Anki。", { exact: true }).waitFor();
    assert.equal(await page.locator('[data-connected="true"]').count(), 0);
    state.connected = true; state.plugin_version = "0.2.0";
    await page.getByRole("button", { name: "刷新检查", exact: true }).click();
    await page.locator('[data-connected="true"]').waitFor();
  });
  await check("Shows real sync counts and preserves expanded help during polling", async () => {
    state.last_sync = { at: Date.now() / 1000, added: 2, updated: 1, unchanged: 3, conflicts: 0, excluded: 1 };
    await page.getByRole("button", { name: "刷新检查", exact: true }).click();
    await page.getByText("最近同步：新增 2 · 更新 1 · 未变化 3 · 冲突 0 · 跳过 1", { exact: true }).waitFor();
    await page.getByText("资料位置（默认无需修改）", { exact: true }).click();
    await page.getByRole("button", { name: "刷新检查", exact: true }).click();
    assert.equal(await page.getByRole("button", { name: "选择 Anki 资料目录", exact: true }).isVisible(), true);
    await page.screenshot({ path: path.join(directory, "connected.png"), fullPage: true });
  });
  await check("Running Anki blocks updates; disconnected status and narrow layout are clear", async () => {
    native.running = true; state.connected = false;
    await page.getByRole("button", { name: "刷新检查", exact: true }).click();
    await page.getByText(/Anki 正在运行。请正常退出/).waitFor();
    assert.equal(await page.getByRole("button", { name: "检查并更新插件", exact: true }).isDisabled(), true);
    assert.equal(await page.locator('[data-connected="true"]').count(), 0);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
    await page.screenshot({ path: path.join(directory, "narrow.png"), fullPage: true });
  });
  await check("A lost status response clears connected state and recovers without a stale error", async () => {
    state.connected = true; native.running = false;
    await page.getByRole("button", { name: "刷新检查", exact: true }).click();
    await page.locator('[data-connected="true"]').waitFor();
    statusFails = true;
    await page.getByRole("button", { name: "刷新检查", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "连接状态暂不可用" }).waitFor();
    assert.equal(await page.locator('[data-connected="true"]').count(), 0);
    statusFails = false;
    await page.getByRole("button", { name: "刷新检查", exact: true }).click();
    await page.locator('[data-connected="true"]').waitFor();
    assert.equal(await page.getByRole("alert").count(), 0);
  });
  assert.deepEqual(errors, []);
  fs.writeFileSync(path.join(directory, "report.json"), JSON.stringify({ results, errors }, null, 2));
  console.log("REPORT " + directory);
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => { if (browser) await browser.close(); server.close(); });
