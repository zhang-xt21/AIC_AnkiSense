"use strict";
// Exercise the packaged application against a loopback-only model fixture.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const net = require("node:net");
const { spawnSync } = require("node:child_process");
const { _electron } = require("playwright");
const { probeBackend } = require("./backend-health.cjs");
const { version } = require("./package.json");
const project = path.resolve(__dirname, "..");
const directory = path.join(project, ".ui-checks", "native-connection", new Date().toISOString().replace(/[:.]/g, "-"));
const executable = process.env.ANKISENSE_TEST_EXECUTABLE
  ? path.resolve(process.env.ANKISENSE_TEST_EXECUTABLE) : path.join(__dirname, "dist", "win-unpacked", "AnkiSense.exe");
fs.mkdirSync(directory, { recursive: true });
const results = [], errors = [], screenshotWarnings = [], calls = [], externalRequests = [];
const generationCalls = [];
let heldModel = null;
const fixtureKey = "native-fictional-key";
let electronApp, window, provider, port, base, env;
let replyMode = "success";
async function listen(server) {
  await new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  return server.address().port;
}
async function closeServer(server) {
  server.closeAllConnections?.();
  await new Promise(resolve => server.close(resolve));
}
async function check(name, fn) {
  await fn(); results.push({ name, status: "passed" }); console.log("PASS " + name);
}
async function capture(name, badgeOnly = false) {
  try {
    const clip = badgeOnly ? await window.locator("#mode-status").boundingBox() : undefined;
    await window.screenshot({ path: path.join(directory, `${name}.png`), ...(clip ? { clip } : {}), timeout: 5000 });
  } catch (error) { screenshotWarnings.push({ name, message: error.message }); }
}
async function launch() {
  electronApp = await _electron.launch({ executablePath: executable, args: ["--hidden"], env, timeout: 60000 });
  window = await electronApp.firstWindow();
  // Keep the test window hidden, but allow renderer timers and screenshots.
  await electronApp.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.setBackgroundThrottling(false));
  window.on("pageerror", error => errors.push(error.message));
  await window.route("**/*", route => {
    const url = route.request().url();
    if (url.startsWith(`${base}/`) || url.startsWith("data:")) return route.continue();
    externalRequests.push(url); return route.abort();
  });
  await window.getByLabel("选择讲义，自动制卡").waitFor({ timeout: 30000 });
}
async function stop() {
  await electronApp.close(); electronApp = null; window = null;
  for (let attempt = 0; attempt < 40; attempt++) {
    if (!(await probeBackend({ port, timeout: 500 })).responding) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error("The application's own backend did not stop with the window");
}
async function openSettings() {
  await window.locator('[data-view="settings"]').click();
  await window.locator(".model-settings-form").waitFor();
  await window.waitForFunction(() => document.querySelector(".model-settings")?.getAttribute("aria-busy") === "false", null, { polling: 100, timeout: 10000 });
}
const field = name => window.locator(`.model-settings [name="${name}"]`);
const verifyButton = () => window.getByRole("button", { name: "验证连接", exact: true });
const waitState = status => window.waitForFunction(expected => document.querySelector("#mode-status")?.dataset.connection === expected, status, { polling: 100, timeout: 10000 });
async function save() {
  const received = window.waitForResponse(response => response.url() === `${base}/api/settings/ai` && response.request().method() === "PUT");
  await window.getByRole("button", { name: "保存设置", exact: true }).click();
  const response = await received;
  assert.equal(response.status(), 200, await response.text());
  await window.waitForFunction(() => document.querySelector(".model-settings")?.getAttribute("aria-busy") === "false", null, { polling: 100, timeout: 10000 });
}
(async () => {
  assert.ok(fs.existsSync(executable), "Build the packaged application before running this check");
  const socket = net.createServer(); port = await listen(socket); await closeServer(socket);
  base = `http://127.0.0.1:${port}`;
  provider = http.createServer(async (request, response) => {
    try {
      const mode = replyMode;
      let body = ""; for await (const chunk of request) body += chunk;
      const payload = JSON.parse(body);
      assert.equal(request.url, "/v1/chat/completions");
      assert.equal(request.headers.authorization, "Bearer " + fixtureKey);
      const userContent = payload.messages[1].content;
      const input = JSON.parse(Array.isArray(userContent) ? userContent.find(item => item.type === "text").text : userContent);
      if (input.source_blocks) {
        generationCalls.push({ model: payload.model, pages: input.page_numbers });
        while (payload.model === heldModel && !response.destroyed) await new Promise(resolve => setTimeout(resolve, 50));
        if (!response.destroyed) {
          response.writeHead(200, { "Content-Type": "application/json" });
          response.end(JSON.stringify({ choices: [{ finish_reason: "stop", message: { content: '{"points":[]}' } }] }));
        }
        return;
      }
      assert.equal(payload.max_tokens, 64);
      assert.equal(payload.response_format.type, "json_object");
      assert.deepEqual(JSON.parse(payload.messages[1].content), { test: "AnkiSense connection check" });
      calls.push({ mode, model: payload.model });
      await new Promise(resolve => setTimeout(resolve, mode === "timeout" ? 2000 : mode === "navigation" ? 1200 : 600));
      if (response.destroyed) return;
      response.writeHead(({ authentication: 401, address: 404, limit: 429 })[mode] || 200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ choices: [{ finish_reason: "stop", message: { content: mode === "invalid_json" ? "invalid fixture JSON" : '{"ok":true}' } }] }));
    } catch (error) { errors.push(error.message); response.writeHead(500); response.end(); }
  });
  const providerPort = await listen(provider);
  const profile = path.join(directory, "profile");
  const fakeAnki = path.join(directory, "isolated-Anki");
  fs.mkdirSync(profile, { recursive: true }); fs.mkdirSync(fakeAnki, { recursive: true });
  const fakeProgram = path.join(directory, "anki.exe");
  fs.writeFileSync(fakeProgram, "test fixture, never executed");
  fs.writeFileSync(path.join(fakeAnki, "prefs21.db"), "isolated prefs fixture");
  fs.writeFileSync(path.join(profile, "anki-setup.json"), JSON.stringify({ executable: fakeProgram, dataDirectory: fakeAnki }));
  env = { ...process.env, AI_MODE: "demo", AI_API_KEY: "", AI_BASE_URL: "", AI_MODEL: "", AI_TIMEOUT_SECONDS: "45",
    ANKISENSE_DESKTOP_PORT: String(port), ANKISENSE_DESKTOP_USER_DATA: profile, ANKISENSE_DATA_DIR: path.join(profile, "data"),
    NO_PROXY: "127.0.0.1,localhost,::1", no_proxy: "127.0.0.1,localhost,::1" };
  delete env.ELECTRON_RUN_AS_NODE;
  await launch();
  await check("Packaged version, capabilities and learning data are correct", async () => {
    const health = await probeBackend({ port, requireAnkiSetup: true, requireConnectionTest: true });
    assert.equal(health.ready, true); assert.equal(health.health.app_version, version);
    const state = await window.evaluate(async () => (await fetch("/api/state")).json());
    assert.equal(state.mode, "demo"); assert.equal(state.documents.length, 0); assert.equal(state.cards.length, 0);
    assert.equal(await window.locator("#app-version").innerText(), `v${version}`);
    await openSettings();
    assert.equal(await verifyButton().isDisabled(), true);
    assert.equal(calls.length, 0);
  });
  await check("Existing Anki setup guide opens without installing or launching Anki", async () => {
    await window.locator('[data-view="export"]').first().click();
    await window.getByText("直接连接 Anki · 安装与连接向导", { exact: true }).click();
    await window.getByRole("button", { name: "安装并配对插件", exact: true }).waitFor();
    assert.equal(fs.existsSync(path.join(fakeAnki, "addons21")), false);
    assert.equal(fs.readFileSync(path.join(fakeAnki, "prefs21.db"), "utf8"), "isolated prefs fixture");
    await openSettings();
  });
  await check("Saving fixed fixture settings does not contact the model", async () => {
    await window.locator('.model-settings input[value="cloud"]').check();
    await field("base_url").fill(`http://127.0.0.1:${providerPort}/v1`);
    await field("model").fill("native-connection-fixture"); await field("api_key").fill(fixtureKey);
    await window.locator(".model-settings-advanced summary").click(); await field("timeout_seconds").fill("3");
    await save(); await waitState("unverified");
    assert.equal(calls.length, 0); assert.equal(await field("api_key").inputValue(), "");
  });
  await check("Verification shows pending then green, and rejects duplicate requests", async () => {
    await verifyButton().click(); await waitState("testing");
    assert.equal(await window.getByRole("button", { name: "正在验证…", exact: true }).isDisabled(), true);
    assert.equal(await window.getByRole("button", { name: "保存设置", exact: true }).isDisabled(), true);
    const duplicate = await window.request.post(`${base}/api/settings/ai/test`, { data: {} });
    assert.equal(duplicate.status(), 409);
    await waitState("success"); assert.equal(calls.length, 1);
    assert.equal(await window.locator("#mode-status").evaluate(el => getComputedStyle(el, "::before").backgroundColor), "rgb(39, 130, 87)");
    await capture("verified-badge", true);
  });
  await check("Refreshing preserves the last result without another model request", async () => {
    await window.reload(); await window.getByLabel("选择讲义，自动制卡").waitFor(); await openSettings();
    await waitState("success"); assert.equal(calls.length, 1);
  });
  await check("Rejected credentials turn red and a deliberate retry recovers", async () => {
    replyMode = "authentication"; await verifyButton().click(); await waitState("error");
    assert.match(await window.locator(".model-settings-test-result").innerText(), /API Key/);
    assert.equal(await window.locator("#mode-status").evaluate(el => getComputedStyle(el, "::before").backgroundColor), "rgb(199, 67, 67)");
    await capture("rejected-credentials");
    replyMode = "success"; await verifyButton().click(); await waitState("success"); assert.equal(calls.length, 3);
  });
  await check("A stalled model times out with a useful error and no automatic retry", async () => {
    await window.locator(".model-settings-advanced summary").click(); await field("timeout_seconds").fill("1");
    await save(); replyMode = "timeout"; await verifyButton().click(); await waitState("error");
    assert.match(await window.locator(".model-settings-test-result").innerText(), /超时/); assert.equal(calls.length, 4);
    await field("timeout_seconds").fill("3"); await save();
  });
  await check("Leaving and returning during verification recovers the final result", async () => {
    replyMode = "navigation"; await verifyButton().click(); await waitState("testing");
    await window.locator('[data-view="materials"]').click(); await openSettings();
    await window.waitForFunction(() => document.querySelector(".model-settings-test-result")?.dataset.connection === "success", null, { polling: 100, timeout: 10000 });
    assert.equal(await verifyButton().isEnabled(), true); assert.equal(calls.length, 5);
  });
  await check("Changed configuration requires saving and invalidates the previous success", async () => {
    await field("model").fill("changed-native-fixture"); assert.equal(await verifyButton().isDisabled(), true);
    await save(); await waitState("unverified"); assert.equal(calls.length, 5);
    replyMode = "success"; await verifyButton().click(); await waitState("success"); assert.equal(calls.length, 6);
    await capture("verified-settings");
  });
  await check("Address, rate limit and invalid JSON errors explain recovery without retrying", async () => {
    for (const [mode, message] of [["address", /Base URL.*模型名称/], ["limit", /额度|频繁/], ["invalid_json", /JSON/]]) {
      replyMode = mode;
      const before = calls.length;
      await verifyButton().click(); await waitState("error");
      assert.match(await window.locator(".model-settings-test-result").innerText(), message);
      assert.equal(calls.length, before + 1);
      assert.equal(await verifyButton().isEnabled(), true);
    }
    replyMode = "success";
    await verifyButton().click(); await waitState("success");
    assert.equal(calls.length, 10);
  });
  await check("Restart preserves saved configuration but clears its old verification result", async () => {
    await stop(); await launch(); await openSettings(); await waitState("unverified");
    assert.equal(await field("model").inputValue(), "changed-native-fixture");
    assert.equal(await field("api_key").inputValue(), ""); assert.equal(calls.length, 10);
    const settings = await window.evaluate(async () => (await fetch("/api/settings/ai")).json());
    assert.equal(settings.api_key_configured, true); assert.ok(!JSON.stringify(settings).includes(fixtureKey));
    assert.deepEqual(errors, []); assert.deepEqual(externalRequests, []);
  });
  await check("Saving a new model cancels the current generation and resumes its original job", async () => {
    assert.equal(await field("resume_active_jobs").isChecked(), true);
    await window.locator(".model-settings-advanced summary").click();
    await field("timeout_seconds").fill("30"); await save();
    const pdf = path.join(directory, "switch-fixture.pdf");
    const script = "from pypdf import PdfWriter;from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject;import sys;w=PdfWriter();p=w.add_blank_page(width=720,height=540);f=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')});p[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):w._add_object(f)})});s=DecodedStreamObject();s.set_data(b'BT /F1 18 Tf 40 460 Td (Stacks use last in first out.) Tj ET');p[NameObject('/Contents')]=w._add_object(s);w.write(sys.argv[1])";
    const prepared = spawnSync(path.join(project, ".venv", "Scripts", "python.exe"), ["-c", script, pdf], { windowsHide: true });
    assert.equal(prepared.status, 0, prepared.stderr?.toString());
    heldModel = "changed-native-fixture";
    const upload = await window.request.post(`${base}/api/material-imports`, { multipart: { file: { name: "switch-fixture.pdf", mimeType: "application/pdf", buffer: fs.readFileSync(pdf) } } });
    assert.equal(upload.status(), 200);
    const record = await upload.json();
    for (let i = 0; i < 100 && !generationCalls.length; i++) await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal(generationCalls.length, 1);
    const original = await (await window.request.get(`${base}/api/material-imports/${record.id}`)).json();
    await field("model").fill("switched-generation-fixture"); await save();
    await window.getByText(/1 个制卡任务已用新配置继续/).waitFor();
    let finished;
    for (let i = 0; i < 100; i++) {
      finished = await (await window.request.get(`${base}/api/material-imports/${record.id}`)).json();
      if (!["pending", "running"].includes(finished.status)) break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    heldModel = null;
    assert.equal(finished.job.id, original.job.id);
    assert.ok(!["pending", "running"].includes(finished.status));
    assert.deepEqual(generationCalls.map(item => item.model), ["changed-native-fixture", "switched-generation-fixture"]);
    await capture("model-switch-resumed");
  });
  await check("Closing the packaged application stops its own backend", stop);
})().catch(async error => {
  errors.push(error.stack || String(error)); console.error(error);
  if (window) {
    try { console.error(await window.locator(".model-settings-test-result").innerText({ timeout: 1000 })); } catch {}
    await capture("failure");
  }
  process.exitCode = 1;
}).finally(async () => {
  if (electronApp) { try { await stop(); } catch (error) { errors.push(error.message); process.exitCode = 1; } }
  if (provider) await closeServer(provider);
  fs.writeFileSync(path.join(directory, "report.json"), JSON.stringify({ executable, version, passed: errors.length === 0,
    real_model_calls: 0, local_fixture_calls: calls, generation_fixture_calls: generationCalls, results, errors, screenshotWarnings, externalRequests }, null, 2));
  console.log(`REPORT ${directory}`);
});
