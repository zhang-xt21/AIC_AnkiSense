"use strict";
// Hidden native Electron + actual local backend, always isolated demo storage.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const net = require("node:net");
const { spawnSync, spawn } = require("node:child_process");
const { _electron } = require("playwright");
const { probeBackend } = require("./backend-health.cjs");
const project = path.resolve(__dirname, "..");
const directory = path.join(project, ".ui-checks", "native-desktop", new Date().toISOString().replace(/[:.]/g, "-"));
fs.mkdirSync(directory, { recursive: true });
let electronApp, externalBackend, port;
const errors = [], results = [], screenshotWarnings = [];
async function check(name, fn) { await fn(); results.push({ name, status: "passed" }); console.log("PASS " + name); }
async function screenshot(page, name, fullPage = false) {
  try { await page.screenshot({ path: path.join(directory, name), fullPage, timeout: 5000 }); }
  catch (error) { screenshotWarnings.push({ name, message: error.message }); }
}
(async () => {
  const socket = net.createServer(); await new Promise(resolve => socket.listen(0, "127.0.0.1", resolve)); port = socket.address().port; await new Promise(resolve => socket.close(resolve));
  const env = { ...process.env, AI_MODE: "demo", AI_API_KEY: "", AI_BASE_URL: "", AI_MODEL: "", ANKISENSE_DESKTOP_PORT: String(port), ANKISENSE_DESKTOP_USER_DATA: path.join(directory, "profile"), ANKISENSE_DATA_DIR: path.join(directory, "profile", "data") };
  // Never launch this dummy executable. The guide installs only into this fixture.
  const ankiBase = path.join(directory, "isolated-Anki"), fakeProgram = path.join(directory, "anki.exe");
  fs.mkdirSync(ankiBase, { recursive: true }); fs.mkdirSync(env.ANKISENSE_DESKTOP_USER_DATA, { recursive: true });
  fs.writeFileSync(fakeProgram, "native setup test fixture"); fs.writeFileSync(path.join(ankiBase, "prefs21.db"), "isolated prefs fixture");
  fs.writeFileSync(path.join(env.ANKISENSE_DESKTOP_USER_DATA, "anki-setup.json"), JSON.stringify({ executable: fakeProgram, dataDirectory: ankiBase }));
  delete env.ELECTRON_RUN_AS_NODE;
  const packaged = process.argv.includes("--packaged");
  const frozen = packaged || process.argv.includes("--frozen-backend");
  const electronExecutable = packaged ? path.join(__dirname, "dist", "win-unpacked", "AnkiSense.exe") : require("./node_modules/electron");
  const reuse = process.argv.includes("--reuse-backend");
  if (reuse) {
    const executable = frozen ? path.join(__dirname, "dist", "ankisense-backend", "ankisense-backend.exe") : path.join(project, ".venv", "Scripts", "python.exe");
    const args = frozen ? ["--host", "127.0.0.1", "--port", String(port)] : ["-m", "uvicorn", "ankisense.app:app", "--host", "127.0.0.1", "--port", String(port)];
    externalBackend = spawn(executable, args, { env, cwd: project, windowsHide: true, stdio: "ignore" });
    let ready = false;
    for (let i = 0; i < 120; i++) { if ((await probeBackend({ port })).ready) { ready = true; break; } await new Promise(resolve => setTimeout(resolve, 250)); }
    assert.equal(ready, true, "Isolated reusable backend did not start");
  }
  electronApp = await _electron.launch({ executablePath: electronExecutable, args: [...(packaged ? [] : [path.join(__dirname, "main.js")]), "--hidden", ...(frozen && !packaged ? ["--frozen-backend"] : [])], env, timeout: 90000 });
  const window = await electronApp.firstWindow(); window.on("pageerror", error => errors.push(error.message));
  await electronApp.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.setBackgroundThrottling(false));
  await window.getByLabel("选择讲义，自动制卡").waitFor({ timeout: 90000 });
  await check("Actual backend identifies itself and demo data is isolated", async () => {
    assert.equal((await probeBackend({ port })).ready, true);
    const state = await window.evaluate(async () => (await fetch("/api/state")).json());
    assert.equal(state.mode, "demo"); assert.equal(state.documents.length, 0); assert.equal(state.cards.length, 0);
  });
  await check("Native title controls and renderer isolation", async () => {
    await window.getByRole("button", { name: "最小化", exact: true }).waitFor();
    const preferences = await electronApp.evaluate(({ BrowserWindow }) => { const win = BrowserWindow.getAllWindows()[0]; return { visible: win.isVisible(), ...win.webContents.getLastWebPreferences() }; });
    assert.equal(preferences.visible, false); assert.equal(preferences.contextIsolation, true); assert.equal(preferences.nodeIntegration, false); assert.equal(preferences.sandbox, true);
    assert.equal(await window.evaluate(() => typeof window.require), "undefined");
    assert.equal(await window.evaluate(() => typeof window.ankisenseDesktop.close), "function");
  });
  await check("Actual static assets and settings render inside Electron", async () => {
    assert.equal(await window.evaluate(() => typeof Generation.create), "function");
    assert.equal(await window.evaluate(() => typeof CardAutoReview.create), "function");
    assert.equal(await window.evaluate(() => typeof cancelMaterialImport), "function");
    assert.match(await window.evaluate(() => automaticPlanLabel({ target_count: null })), /自动决定/);
    await window.locator('[data-view="settings"]').click();
    await window.getByRole("heading", { name: "模型与 API", exact: true }).waitFor();
    await window.locator('[data-view="materials"]').click();
    await window.getByLabel("选择讲义，自动制卡").waitFor();
    await screenshot(window, "native-window.png");
  });
  await check("Bundled setup installs and pairs only the isolated add-on directory", async () => {
    await window.locator('[data-view="export"]').first().click();
    await window.getByText("直接连接 Anki · 安装与连接向导", { exact: true }).click();
    const install = window.getByRole("button", { name: "安装并配对插件", exact: true });
    await install.waitFor({ timeout: 15000 });
    assert.equal(await install.isEnabled(), true, "Close any real Anki before this isolated install test");
    await install.click();
    await window.getByText("插件已安装并配对，请重新打开 Anki。", { exact: true }).waitFor({ timeout: 15000 });
    const target = path.join(ankiBase, "addons21", "ankisense_sync");
    assert.equal(fs.existsSync(path.join(target, "onboarding.py")), true);
    const config = JSON.parse(fs.readFileSync(path.join(target, "config.json"), "utf8"));
    assert.equal(config.connection_file, path.join(env.ANKISENSE_DATA_DIR, "plugin-connection.json"));
    assert.equal(typeof config.token, "undefined");
    assert.equal(await window.locator('[data-connected="true"]').count(), 0);
    // Emulate the local receipt here; real Anki writes are a separate acceptance.
    const connection = JSON.parse(fs.readFileSync(config.connection_file, "utf8"));
    const event = await window.request.post(`http://127.0.0.1:${port}/api/plugin/v1/events`, { headers: { Authorization: `Bearer ${connection.token}`, "X-AnkiSense-Client": "isolated-native-test" }, data: { kind: "sync", plugin_version: "0.2.0", added: 2, unchanged: 1 } });
    assert.equal(event.status(), 200);
    await window.getByRole("button", { name: "刷新检查", exact: true }).click();
    await window.locator('[data-connected="true"]').waitFor();
    await window.getByText("最近同步：新增 2 · 更新 0 · 未变化 1 · 冲突 0 · 跳过 0", { exact: true }).waitFor();
    await screenshot(window, "anki-setup.png", true);
    assert.equal(fs.readFileSync(path.join(ankiBase, "prefs21.db"), "utf8"), "isolated prefs fixture");
    await window.locator('[data-view="materials"]').first().click();
  });
  await check("A second desktop instance exits without opening another window", async () => {
    const second = spawn(electronExecutable, [...(packaged ? [] : [path.join(__dirname, "main.js")]), "--hidden"], { env, windowsHide: true, stdio: "ignore" });
    let timer;
    const code = await Promise.race([new Promise(resolve => second.on("exit", resolve)), new Promise((_, reject) => { timer = setTimeout(() => { second.kill(); reject(new Error("Second instance did not exit")); }, 10000); })]).finally(() => clearTimeout(timer));
    assert.equal(code, 0);
    assert.equal(await electronApp.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length), 1);
  });
  await check("Frozen PDF renderer and bundled OCR work without downloading models", async () => {
    const fixture = path.join(directory, "scanned-fixture.pdf");
    const prepared = spawnSync(path.join(project, ".venv", "Scripts", "python.exe"), ["-c", "from PIL import Image,ImageDraw,ImageFont;import sys;im=Image.new('RGB',(1100,480),'white');d=ImageDraw.Draw(im);f=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',45);d.text((50,60),'STACK: LAST IN FIRST OUT',font=f,fill='black');d.text((50,160),'Push A then B. Pop B first.',font=f,fill='black');im.save(sys.argv[1],'PDF',resolution=150)", fixture], { windowsHide: true });
    assert.equal(prepared.status, 0, prepared.stderr?.toString());
    const response = await window.request.post(`http://127.0.0.1:${port}/api/documents`, { multipart: { file: { name: "scanned-fixture.pdf", mimeType: "application/pdf", buffer: fs.readFileSync(fixture) }, start_page: "1", end_page: "1", mode: "ocr" }, timeout: 90000 });
    assert.equal(response.status(), 200, await response.text());
    const state = await window.evaluate(async () => (await fetch("/api/state")).json());
    assert.equal(state.source_pages.length, 1);
    assert.equal(state.source_pages[0].method, "ocr");
    assert.match(state.source_pages[0].text, /STACK/i);
    for (const name of ["PP-OCRv6_det_small.onnx", "ch_ppocr_mobile_v2.0_cls_mobile.onnx", "PP-OCRv6_rec_small.onnx"]) assert.equal(fs.existsSync(path.join(directory, "profile", "data", "ocr-models", name)), true);
  });
  await check(reuse ? "A reused backend remains running when the shell closes" : "Only the spawned backend closes with the native shell", async () => {
    await electronApp.close(); electronApp = null;
    if (reuse) { assert.equal((await probeBackend({ port })).ready, true); return; }
    let closed = false;
    for (let i = 0; i < 40; i++) { if (!(await probeBackend({ port })).responding) { closed = true; break; } await new Promise(resolve => setTimeout(resolve, 200)); }
    assert.equal(closed, true);
  });
  assert.deepEqual(errors, []);
  fs.writeFileSync(path.join(directory, "report.json"), JSON.stringify({ frozen, packaged, reuse, port, results, errors, screenshotWarnings }, null, 2));
  console.log(`REPORT ${directory}`);
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => { if (electronApp) await electronApp.close(); if (externalBackend) externalBackend.kill(); });
