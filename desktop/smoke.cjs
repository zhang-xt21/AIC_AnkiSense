"use strict";
// Hidden native Electron + actual local backend, always isolated demo storage.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const net = require("node:net");
const http = require("node:http");
const { spawnSync, spawn } = require("node:child_process");
const { _electron } = require("playwright");
const { probeBackend } = require("./backend-health.cjs");
const { checkFigureCrop } = require("../tools/browser/figure_crop_acceptance.cjs");
const project = path.resolve(__dirname, "..");
const directory = path.join(project, ".ui-checks", "native-desktop", new Date().toISOString().replace(/[:.]/g, "-"));
fs.mkdirSync(directory, { recursive: true });
let electronApp, externalBackend, port;
const errors = [], results = [], screenshotWarnings = [];
let installationSkipped = false;
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
  const electronExecutable = packaged ? process.env.ANKISENSE_TEST_EXECUTABLE
    ? path.resolve(process.env.ANKISENSE_TEST_EXECUTABLE) : path.join(__dirname, "dist", "win-unpacked", "AnkiSense.exe") : require("./node_modules/electron");
  const reuse = process.argv.includes("--reuse-backend");
  if (reuse) {
    const executable = frozen ? path.join(__dirname, "dist", "ankisense-backend", "ankisense-backend.exe") : path.join(project, ".venv", "Scripts", "python.exe");
    const args = frozen ? ["--host", "127.0.0.1", "--port", String(port)] : ["-m", "uvicorn", "ankisense.app:app", "--host", "127.0.0.1", "--port", String(port)];
    externalBackend = spawn(executable, args, { env, cwd: project, windowsHide: true, stdio: "ignore" });
    let ready = false;
    for (let i = 0; i < 120; i++) { if ((await probeBackend({ port })).ready) { ready = true; break; } await new Promise(resolve => setTimeout(resolve, 250)); }
    assert.equal(ready, true, "Isolated reusable backend did not start");
  }
  electronApp = await _electron.launch({ executablePath: electronExecutable, args: [...(packaged ? [] : [__dirname]), "--hidden", ...(frozen && !packaged ? ["--frozen-backend"] : [])], env, timeout: 90000 });
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
  await electronApp.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1280, 640));
  await checkFigureCrop(window, { check, native: true, screenshot: name => screenshot(window, name) });
  await electronApp.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1280, 860));
  await check("Bundled setup pairs an isolated add-on or blocks updates while Anki is running", async () => {
    await window.locator('[data-view="export"]').first().click();
    await window.getByText("直接连接 Anki · 安装与连接向导", { exact: true }).click();
    const install = window.getByRole("button", { name: "安装并配对插件", exact: true });
    await install.waitFor({ timeout: 15000 });
    const target = path.join(ankiBase, "addons21", "ankisense_sync");
    let config;
    if (await install.isEnabled()) {
      await install.click();
      await window.getByText("插件已安装并配对，请重新打开 Anki。", { exact: true }).waitFor({ timeout: 15000 });
      assert.equal(fs.existsSync(path.join(target, "onboarding.py")), true);
      config = JSON.parse(fs.readFileSync(path.join(target, "config.json"), "utf8"));
    } else {
      await window.getByText(/Anki 正在运行。请正常退出/).waitFor();
      assert.equal(fs.existsSync(target), false, "A running Anki must prevent any add-on install");
      installationSkipped = true;
      const enabled = await window.request.post(`http://127.0.0.1:${port}/api/anki/setup/enable`, { data: {} });
      assert.equal(enabled.status(), 200);
      const addon = await window.request.get(`http://127.0.0.1:${port}/api/anki/setup/addon`);
      assert.equal(addon.status(), 200);
      assert.equal((await addon.body()).subarray(0, 2).toString(), "PK");
      config = { connection_file: path.join(env.ANKISENSE_DATA_DIR, "plugin-connection.json") };
    }
    assert.equal(config.connection_file, path.join(env.ANKISENSE_DATA_DIR, "plugin-connection.json"));
    assert.equal(typeof config.token, "undefined");
    assert.equal(await window.locator('[data-connected="true"]').count(), 0);
    // Emulate the local receipt here; real Anki writes are a separate acceptance.
    const connection = JSON.parse(fs.readFileSync(config.connection_file, "utf8"));
    const event = await window.request.post(`http://127.0.0.1:${port}/api/plugin/v1/events`, { headers: { Authorization: `Bearer ${connection.token}`, "X-AnkiSense-Client": "isolated-native-test" }, data: { kind: "sync", plugin_version: "0.3.2", added: 2, unchanged: 1 } });
    assert.equal(event.status(), 200);
    await window.getByRole("button", { name: "刷新检查", exact: true }).click();
    await window.locator('[data-connected="true"]').waitFor();
    await window.getByText("最近同步：新增 2 · 更新 0 · 未变化 1 · 冲突 0 · 跳过 0", { exact: true }).waitFor();
    await screenshot(window, "anki-setup.png", true);
    assert.equal(fs.readFileSync(path.join(ankiBase, "prefs21.db"), "utf8"), "isolated prefs fixture");
    await window.locator('[data-view="materials"]').first().click();
  });
  await check("Packaged backend serves APKG and media ZIP for a selected document", async () => {
    const base = `http://127.0.0.1:${port}`;
    assert.equal((await window.request.post(base + "/api/demo")).status(), 200);
    const state = await (await window.request.get(base + "/api/state")).json();
    const card = state.cards[0];
    const body = Object.fromEntries(["question", "answer", "concept", "kind", "rubric", "evidence", "images"].filter(key => key in card).map(key => [key, card[key]]));
    const approved = await window.request.put(`${base}/api/cards/${card.id}`, { data: { ...body, status: "approved" } });
    assert.equal(approved.status(), 200, await approved.text());
    for (const format of ["apkg", "media"]) {
      const response = await window.request.get(`${base}/api/export/${format}?document_id=${encodeURIComponent(card.document_id)}`);
      assert.equal(response.status(), 200, response.status() === 200 ? "" : await response.text());
      assert.equal((await response.body()).subarray(0, 2).toString(), "PK");
    }
  });
  await check("The native APKG download button writes a complete file into an isolated destination", async () => {
    const destination = path.join(directory, "ui-export.apkg");
    await electronApp.evaluate(({ session }, target) => {
      globalThis.nativeDownloadState = null;
      session.defaultSession.once("will-download", (_event, item) => {
        item.setSavePath(target);
        item.once("done", (_done, state) => { globalThis.nativeDownloadState = state; });
      });
    }, destination);
    await window.evaluate(async () => refresh());
    await window.locator('[data-view="export"]').first().click();
    await window.getByRole("button", { name: /下载.*APKG/ }).click();
    let complete = false;
    for (let attempt = 0; attempt < 80; attempt++) {
      const state = await electronApp.evaluate(() => globalThis.nativeDownloadState);
      if (state) { assert.equal(state, "completed"); complete = true; break; }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    assert.equal(complete, true);
    assert.equal(fs.readFileSync(destination).subarray(0, 2).toString(), "PK");
  });
  await check("Viewing saved cards from a cancelled task refreshes and focuses the packaged result list", async () => {
    const base = `http://127.0.0.1:${port}`;
    const state = await (await window.request.get(base + "/api/state")).json();
    const documentId = state.cards[0].document_id;
    const expected = state.cards.filter(card => card.document_id === documentId && card.status === "approved" && !card.source_outdated).length;
    const job = { id: "autjob-native-progress", document_id: documentId, status: "cancelled", stage: "complete",
      message: "任务已取消，已保存卡片保留。", options: { autonomous: true }, progress: { completed: 2, total: 2 },
      card_ids: state.cards.filter(card => card.document_id === documentId).map(card => card.id), issues: [],
      batches: [{ batch_id: "done", status: "completed", block_ids: [], knowledge: [] },
        { batch_id: "failed", status: "failed", block_ids: [], pages: [4, 5, 6], knowledge: [] }] };
    const record = { id: "import-native-view", filename: "自编卡片导航测试.pdf", document_id: documentId,
      status: "cancelled", message: "任务已取消，已保存卡片保留。", job_id: job.id, created_at: new Date().toISOString() };
    const script = "import sys,json;from pathlib import Path;root=Path(sys.argv[1]).resolve();sys.path.insert(0,str(root));data=Path(sys.argv[2]).resolve();assert data.is_relative_to(root/'.ui-checks');from ankisense.db import Store;s=Store(data/'ankisense.sqlite3');s.put('auto_card_jobs',json.loads(sys.argv[4]));s.put('material_imports',json.loads(sys.argv[3]))";
    const seeded = spawnSync(path.join(project, ".venv", "Scripts", "python.exe"), ["-c", script, project, env.ANKISENSE_DATA_DIR, JSON.stringify(record), JSON.stringify(job)], { windowsHide: true });
    assert.equal(seeded.status, 0, seeded.stderr?.toString());
    await window.locator('[data-view="materials"]').first().click();
    await window.evaluate(() => loadMaterialImports());
    await window.getByText("成功 1/2 批 · 失败 1 批", { exact: true }).waitFor();
    await window.getByText("待处理记录（1 批）", { exact: true }).click();
    await window.getByText(/第 4、5、6 页.*等待继续处理/).waitFor();
    for (let repeat = 0; repeat < 2; repeat++) {
      await window.locator('[data-view-import="import-native-view"]').click();
      await window.waitForFunction(() => !app.importViewing && document.activeElement?.id === "card-results");
      assert.equal(await window.locator(".automatic-card").count(), expected);
      // A short result list can reach the document's scroll limit before its
      // top reaches mid-screen. Verify that navigation exposes the list.
      const bounds = await window.locator("#card-results").evaluate(node => {
        const rect = node.getBoundingClientRect();
        return { top: rect.top, bottom: rect.bottom, viewport: innerHeight };
      });
      assert.equal(bounds.top < bounds.viewport && bounds.bottom > 0, true, JSON.stringify(bounds));
    }
    await window.locator('[data-view="materials"]').first().click();
  });
  await check("Packaged card and practice filters preserve the answer draft and selected lecture", async () => {
    const state = await (await window.request.get(`http://127.0.0.1:${port}/api/state`)).json();
    const ready = state.cards.find(card => card.status === "approved");
    await window.locator('[data-view="cards"]').first().click();
    await window.getByLabel("查看哪份讲义的卡牌").selectOption(ready.document_id);
    await window.getByLabel("筛选卡牌状态").selectOption("ready");
    await window.getByLabel("筛选卡牌配图").selectOption("with");
    assert.equal(await window.locator(".automatic-card").count(), 0);
    await window.getByText("没有匹配的卡片", { exact: true }).waitFor();
    await window.getByLabel("筛选卡牌配图").selectOption("without");
    assert.equal(await window.locator(".automatic-card").count(), 1);
    await window.locator("#main").getByRole("button", { name: "开始练习", exact: true }).click();
    assert.equal(await window.getByLabel("选择练习讲义").inputValue(), ready.document_id);
    assert.equal(await window.getByLabel("筛选练习配图").inputValue(), "without");
    await window.getByLabel("你的回答", { exact: true }).fill("Native isolated draft");
    await window.getByLabel("搜索练习卡片").fill("no-such-fictional-card");
    await window.getByText("没有匹配的练习卡片", { exact: true }).waitFor();
    await window.locator(".practice-scope-toolbar").getByRole("button", { name: "清除筛选", exact: true }).click();
    assert.equal(await window.getByLabel("你的回答", { exact: true }).inputValue(), "Native isolated draft");
    await window.locator('[data-view="materials"]').first().click();
  });
  await check("A second desktop instance exits without opening another window", async () => {
    const second = spawn(electronExecutable, [...(packaged ? [] : [__dirname]), "--hidden"], { env, windowsHide: true, stdio: "ignore" });
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
  await check("Packaged original-page crops reach a local vision API without an extra interpreter", async () => {
    const calls = [];
    const provider = http.createServer((request, response) => {
      let raw = "";
      request.on("data", chunk => { raw += chunk; });
      request.on("end", () => {
        calls.push(JSON.parse(raw));
        response.writeHead(200, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ choices: [{ finish_reason: "stop", message: { content: JSON.stringify({
          classification: "text", description: "Fictional source-page answer text", relation: "supported",
          completeness: "complete", legibility: "clear", answer_leakage: "yes", reasons: ["Offline image fixture is readable."] }) } }] }));
      });
    });
    await new Promise(resolve => provider.listen(0, "127.0.0.1", resolve));
    const base = `http://127.0.0.1:${port}`;
    try {
      const state = await (await window.request.get(base + "/api/state")).json();
      const source = state.source_pages[0], block = state.blocks.find(item => item.block_id === source.block_ids[0]);
      const card = { id: "native-vision-card", document_id: source.document_id, question: "What order does a stack use?",
        answer: "Last in first out.", concept: "stack", kind: "basic", status: "draft", version: 1, is_demo: false, images: [],
        rubric: [{ point_id: "lifo", text: "Explain last in first out." }], evidence: [{ block_id: block.block_id, quote: block.text }] };
      const seed = "import sys,json;from pathlib import Path;root=Path(sys.argv[1]).resolve();sys.path.insert(0,str(root));data=Path(sys.argv[2]).resolve();assert data.is_relative_to(root/'.ui-checks');from ankisense.db import Store;Store(data/'ankisense.sqlite3').put('cards',json.loads(sys.argv[3]))";
      const seeded = spawnSync(path.join(project, ".venv", "Scripts", "python.exe"), ["-c", seed, project, env.ANKISENSE_DATA_DIR, JSON.stringify(card)], { windowsHide: true });
      assert.equal(seeded.status, 0, seeded.stderr?.toString());
      const saved = await window.request.put(base + "/api/settings/ai", { data: { mode: "cloud", base_url: `http://127.0.0.1:${provider.address().port}/v1`, model: "fictional-vision", api_key: "local-only-fixture", timeout_seconds: 15 } });
      assert.equal(saved.status(), 200, await saved.text());
      assert.equal(calls.length, 0, "Saving settings must not call the model");
      const capability = await (await window.request.get(base + "/api/image-check/status")).json();
      assert.equal(capability.available, true); assert.equal(capability.engine, "vision-api");
      const started = await window.request.post(`${base}/api/cards/${card.id}/image-recommendations`, { data: { card_version: 1 } });
      assert.equal(started.status(), 200, await started.text());
      let job = await started.json();
      for (let attempt = 0; attempt < 80 && ["pending", "running"].includes(job.status); attempt++) {
        await new Promise(resolve => setTimeout(resolve, 100));
        job = await (await window.request.get(`${base}/api/image-recommendations/${job.id}`)).json();
      }
      assert.equal(job.status, "completed"); assert.equal(job.candidates[0].kind, "page"); assert.equal(calls.length, 0);
      const response = await window.request.post(`${base}/api/cards/${card.id}/image-recommendations/check`, { data: {
        job_id: job.id, candidate_id: job.candidates[0].id, card_version: 1, bbox: [0, 0, 1, 1], side: "answer" } });
      assert.equal(response.status(), 200, await response.text());
      assert.equal((await response.json()).verdict, "pass"); assert.equal(calls.length, 1);
      assert.equal(calls[0].messages[0].content.filter(item => item.type === "image_url").length, 2);
      const front = await window.request.post(`${base}/api/cards/${card.id}/image-recommendations/check`, { data: {
        job_id: job.id, candidate_id: job.candidates[0].id, card_version: 1, bbox: [0, 0, 1, 1], side: "question" } });
      assert.equal(front.status(), 200, await front.text());
      assert.equal((await front.json()).verdict, "attention");
      const adoption = await window.request.post(`${base}/api/cards/${card.id}/image-recommendations/adopt`, { data: {
        job_id: job.id, candidate_id: job.candidates[0].id, card_version: 1, bbox: [0, 0, 1, 1] } });
      assert.equal(adoption.status(), 200, await adoption.text());
      const media = await adoption.json();
      // Seed only the isolated saved-picture fixture. The product's save API
      // queues whole-card review, which this vision-only provider cannot serve.
      const updated = { ...card, version: 2,
        images: [{ media_id: media.id, side: "answer", caption: "Offline source-page answer" }] };
      const savedFixture = spawnSync(path.join(project, ".venv", "Scripts", "python.exe"),
        ["-c", seed, project, env.ANKISENSE_DATA_DIR, JSON.stringify(updated)], { windowsHide: true });
      assert.equal(savedFixture.status, 0, savedFixture.stderr?.toString());
      const savedCheck = await window.request.post(`${base}/api/cards/${card.id}/images/${media.id}/auto-check`, {
        data: { card_version: updated.version } });
      assert.equal(savedCheck.status(), 200, await savedCheck.text());
      assert.equal((await savedCheck.json()).status, "auto_pass");
      await window.evaluate(async () => refresh());
      await window.locator('[data-view="review"]').first().click();
      await window.getByLabel("筛选课程", { exact: true }).selectOption(card.document_id);
      await window.getByLabel("选择编辑卡片", { exact: true }).selectOption(card.id);
      await window.getByText("配图机审通过", { exact: true }).waitFor();
      assert.equal(await window.getByText("答案侧允许包含答案文字；仍须满足来源、相关性、完整性和清晰度要求。", { exact: true }).count(), 1);
      assert.equal(calls.length, 3, "Opening the editor must not run additional model checks");
    } finally {
      const restored = await window.request.put(base + "/api/settings/ai", { data: { mode: "demo", base_url: "", model: "", timeout_seconds: 15, clear_api_key: true } });
      provider.closeAllConnections?.(); await new Promise(resolve => provider.close(resolve));
      assert.equal(restored.status(), 200, await restored.text());
    }
  });
  await check("Native workbench practice follows the selected lecture and blocks unsaved edits", async () => {
    await window.evaluate(async () => refresh());
    const state = await (await window.request.get(`http://127.0.0.1:${port}/api/state`)).json();
    const ready = state.cards.find(card => card.status === "approved");
    await window.evaluate(() => { app.practiceQuery = "previous-scope"; app.practiceImages = "with"; app.practiceFilter = "completed"; });
    await window.locator('[data-view="review"]').first().click();
    await window.getByLabel("筛选课程", { exact: true }).selectOption(ready.document_id);
    await window.getByLabel("选择编辑卡片", { exact: true }).selectOption(ready.id);
    await window.locator(".page-actions").getByRole("button", { name: "开始练习 →", exact: true }).click();
    assert.equal(await window.getByLabel("选择练习讲义").inputValue(), ready.document_id);
    assert.equal(await window.getByLabel("选择练习卡片").inputValue(), ready.id);
    assert.equal(await window.getByLabel("搜索练习卡片").inputValue(), "");
    await window.locator('[data-view="review"]').first().click();
    const source = state.cards.find(card => card.id === "native-vision-card");
    await window.getByLabel("筛选课程", { exact: true }).selectOption(source.document_id);
    await window.getByLabel("选择编辑卡片", { exact: true }).selectOption(source.id);
    assert.equal(await window.locator(".page-actions").getByRole("button", { name: "开始练习 →", exact: true }).isDisabled(), true);
    await window.getByLabel("筛选课程", { exact: true }).selectOption(ready.document_id);
    await window.getByLabel("选择编辑卡片", { exact: true }).selectOption(ready.id);
    await window.locator(".workbench-editor").getByLabel("问题", { exact: true }).fill("Native unsaved edit");
    const study = window.locator(".page-actions").getByRole("button", { name: "开始练习 →", exact: true });
    assert.equal(await study.isDisabled(), true);
    assert.match(await study.getAttribute("title"), /保存/);
    // Discard only this isolated test draft before testing application shutdown.
    // Otherwise Playwright races Electron while handling the unload confirmation.
    await window.evaluate(id => { delete app.cardDrafts[id]; }, ready.id);
  });
  await check("Packaged math preview uses local fonts and preserves LaTeX editor source", async () => {
    const state = await (await window.request.get(`http://127.0.0.1:${port}/api/state`)).json();
    const card = state.cards.find(item => item.status === "approved");
    await window.locator('[data-view="review"]').first().click();
    await window.getByLabel("筛选课程", { exact: true }).selectOption(card.document_id);
    await window.getByLabel("选择编辑卡片", { exact: true }).selectOption(card.id);
    const question = window.locator(".workbench-editor").getByLabel("问题", { exact: true });
    const answer = window.locator(".workbench-editor").getByLabel("参考答案", { exact: true });
    const source = String.raw`计算 \(x_1^2+x_2^2\)`;
    await question.fill(source);
    await answer.fill(String.raw`$$\frac{1}{1+x^2}$$ $$\begin{bmatrix}1&2\\3&4\end{bmatrix}$$`);
    await window.locator(".preview-face-tabs").getByRole("button", { name: "背面", exact: true }).click();
    assert.equal(await question.inputValue(), source);
    assert.equal(await window.locator(".preview-question .katex").count(), 1);
    assert.equal(await window.locator(".preview-answer .katex").count(), 2);
    const fonts = await window.evaluate(async () => {
      await document.fonts.ready;
      return [...document.fonts].filter(font => font.family.includes("KaTeX") && font.status === "loaded").length;
    });
    assert.equal(fonts > 0, true);
    await question.fill(String.raw`错误 $\frac{1}{$`);
    assert.equal(await window.locator(".preview-question .math-source-fallback").count(), 1);
    await window.evaluate(id => { delete app.cardDrafts[id]; }, card.id);
  });
  await check("Packaged deletion supports cancel, single removal and scoped clearing", async () => {
    const state = await (await window.request.get(`http://127.0.0.1:${port}/api/state`)).json();
    const card = state.cards.find(item => item.id === "native-vision-card");
    await window.locator('[data-view="cards"]').first().click();
    await window.getByLabel("查看哪份讲义的卡牌").selectOption(card.document_id);
    if (card.status !== "approved") await window.getByLabel("筛选卡牌状态").selectOption("pending");
    await window.locator(`.automatic-card[data-card-id="${card.id}"]`).getByRole("button", { name: "删除", exact: true }).click();
    await window.getByRole("dialog").getByRole("button", { name: "取消", exact: true }).click();
    assert.equal(await window.locator(`.automatic-card[data-card-id="${card.id}"]`).count(), 1);
    await window.locator(`.automatic-card[data-card-id="${card.id}"]`).getByRole("button", { name: "删除", exact: true }).click();
    await window.getByRole("dialog").getByRole("button", { name: "确认删除", exact: true }).click();
    await window.waitForFunction(id => !app.data.cards.some(item => item.id === id), card.id);
    const other = state.cards.find(item => item.document_id !== card.document_id);
    await window.getByLabel("查看哪份讲义的卡牌").selectOption(other.document_id);
    await window.locator(".card-material-more > summary").click();
    await window.getByRole("button", { name: "删除本材料及卡片", exact: true }).click();
    await window.getByRole("dialog").getByRole("button", { name: "确认删除", exact: true }).click();
    await window.waitForFunction(id => !app.data.cards.some(item => item.document_id === id), other.document_id);
    const after = await (await window.request.get(`http://127.0.0.1:${port}/api/state`)).json();
    assert.equal(after.documents.length, state.documents.length - 1);
    assert.equal(after.documents.some(item => item.id === other.document_id), false);
    await window.getByLabel("查看哪份讲义的卡牌").selectOption(card.document_id);
    await window.locator(".card-material-more > summary").click();
    await window.getByRole("button", { name: "删除本材料及卡片", exact: true }).click();
    await window.getByRole("dialog").getByText(/0 张卡片/).waitFor();
    await window.getByRole("dialog").getByRole("button", { name: "确认删除", exact: true }).click();
    await window.waitForFunction(id => !app.data.documents.some(item => item.id === id), card.document_id);
  });
  await check(reuse ? "A reused backend remains running when the shell closes" : "Only the spawned backend closes with the native shell", async () => {
    await electronApp.close(); electronApp = null;
    if (reuse) { assert.equal((await probeBackend({ port })).ready, true); return; }
    let closed = false;
    for (let i = 0; i < 40; i++) { if (!(await probeBackend({ port })).responding) { closed = true; break; } await new Promise(resolve => setTimeout(resolve, 200)); }
    assert.equal(closed, true);
  });
  assert.deepEqual(errors, []);
  fs.writeFileSync(path.join(directory, "report.json"), JSON.stringify({ frozen, packaged, reuse, installationSkipped, port, results, errors, screenshotWarnings }, null, 2));
  console.log(`REPORT ${directory}`);
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => { if (electronApp) await electronApp.close(); if (externalBackend) externalBackend.kill(); });
