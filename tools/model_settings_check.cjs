"use strict";

// Real-browser checks against an isolated local service. Every model/HTTP
// client in the fixture is disabled; only clearly fictitious credentials are used.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const playwright = require("playwright");
const { project, pythonExecutable, browserOptions } = require("./browser_runtime.cjs");
const output = path.join(project, ".ui-checks", "20260927-model-settings");
const runId = new Date().toISOString().replace(/[:.]/g, "-");
const runDir = path.join(output, `run-${runId}`);
const dataDir = path.join(runDir, "data");
const base = "http://127.0.0.1:8977";
const keyValue = "test-only-fictional-api-key";
const results = [], errors = [], consoleErrors = [], blockedRequests = [], requests = [], responseLeaks = [];
let server, browser, page;
fs.mkdirSync(runDir, { recursive: true });
fs.writeFileSync(path.join(output, "server.py"), `"""Offline fixture; never reads .env or permits model/network client calls."""
import os
import sys
import asyncio
from pathlib import Path
HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
sys.path.insert(0, str(PROJECT))
DATA = Path(os.environ["ANKISENSE_UI_DATA"]).resolve()
if not DATA.is_relative_to(HERE):
    raise RuntimeError("Fixture data must remain inside its isolated folder")
os.environ["ANKISENSE_DATA_DIR"] = str(DATA)
os.environ.update(AI_MODE="demo", AI_API_KEY="", AI_BASE_URL="", AI_MODEL="")
from ankisense.llm import AIError, AIService, Settings
Settings.from_env = classmethod(lambda cls: cls(mode="demo"))
COUNTS = {"model_calls": 0, "http_calls": 0, "test_calls": 0}
async def test_connection(self):
    COUNTS["test_calls"] += 1
    await asyncio.sleep(1)
    if COUNTS["test_calls"] == 2:
        raise AIError("模型服务拒绝访问，请检查 API Key 和模型权限。", code="authentication")
AIService.verify_connection = test_connection
async def deny_model(*args, **kwargs):
    COUNTS["model_calls"] += 1
    raise RuntimeError("Model calls are forbidden in this fixture")
async def deny_http_async(*args, **kwargs):
    COUNTS["http_calls"] += 1
    raise RuntimeError("External HTTP calls are forbidden in this fixture")
def deny_http_sync(*args, **kwargs):
    COUNTS["http_calls"] += 1
    raise RuntimeError("External HTTP calls are forbidden in this fixture")
AIService._request = deny_model
import httpx
httpx.AsyncClient.send = deny_http_async
httpx.Client.send = deny_http_sync
from ankisense.app import create_app
app = create_app(data_dir=DATA, settings=Settings(mode="demo"))
@app.get("/__test/metrics")
def metrics():
    return COUNTS
@app.post("/__test/busy/{enabled}")
def busy(enabled: int):
    app.state.active_model_requests = 1 if enabled else 0
    return {"busy": bool(enabled)}
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8977, log_level="warning")
`, "utf8");
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function api(url, options = {}) {
  const response = await fetch(base + url, options);
  const data = await response.json();
  assert.ok(response.ok, `${url}: ${response.status} ${JSON.stringify(data)}`);
  return data;
}
async function stopServer() {
  if (server && server.exitCode === null && server.signalCode === null) {
    const owned = server;
    const stopped = new Promise(resolve => owned.once("exit", resolve));
    owned.kill();
    await Promise.race([stopped, delay(5000)]);
    assert.ok(owned.exitCode !== null || owned.signalCode !== null, "Owned fixture did not terminate");
  }
}
async function startServer() {
  const log = fs.openSync(path.join(runDir, "server.log"), "a");
  server = spawn(pythonExecutable(), ["-X", "utf8", path.join(output, "server.py")], {
    cwd: project, windowsHide: true, env: { ...process.env, ANKISENSE_UI_DATA: dataDir }, stdio: ["ignore", log, log],
  });
  server.on("error", error => errors.push(`server: ${error.message}`));
  for (let i = 0; i < 100; i++) {
    if (server.exitCode !== null) throw new Error(`Fixture exited (${server.exitCode}); see server.log`);
    try { if ((await api("/health")).status === "ok") return; } catch {}
    await delay(150);
  }
  throw new Error("Independent settings fixture did not become ready");
}
async function check(name, fn) {
  const started = Date.now();
  try { await fn(); results.push({ name, status: "passed", elapsed_ms: Date.now() - started }); console.log(`PASS ${name}`); }
  catch (error) { results.push({ name, status: "failed", elapsed_ms: Date.now() - started, error: error.message }); throw error; }
}
const setting = name => page.locator(`.model-settings [name="${name}"]`);
const saveButton = () => page.getByRole("button", { name: "保存设置", exact: true });
async function openSettings() {
  await page.locator('button[data-view="settings"]').click();
  await page.locator(".model-settings-form").waitFor();
  await page.waitForFunction(() => document.querySelector(".model-settings")?.getAttribute("aria-busy") === "false");
}
async function save(expected = 200) {
  const received = page.waitForResponse(response => response.url() === `${base}/api/settings/ai` && response.request().method() === "PUT");
  await saveButton().click();
  const response = await received;
  assert.equal(response.status(), expected, await response.text());
  const data = await response.json();
  await page.waitForFunction(() => document.querySelector(".model-settings")?.getAttribute("aria-busy") === "false");
  if (expected === 200) {
    await page.waitForFunction(saved => {
      const panel = document.querySelector(".model-settings");
      return panel?.querySelector('[name="base_url"]')?.value === saved.base_url
        && panel?.querySelector('[name="model"]')?.value === saved.model
        && panel?.querySelector(".model-settings-feedback")?.hidden === false
        && (saved.mode !== "cloud" || !saved.configured || document.querySelector("#mode-status")?.textContent.includes(saved.model));
    }, data);
  }
  return data;
}
async function screenshot(name) { await page.evaluate(async () => { document.activeElement?.blur(); window.scrollTo({ top: 0, behavior: "instant" }); await new Promise(requestAnimationFrame); }); await page.screenshot({ path: path.join(runDir, `${name}.png`), fullPage: true }); }
async function assertNoKeyReadback() {
  const settings = await api("/api/settings/ai");
  assert.equal(Object.hasOwn(settings, "api_key"), false);
  assert.ok(!JSON.stringify(settings).includes(keyValue));
  assert.equal(await setting("api_key").inputValue(), "");
  assert.equal(await setting("api_key").getAttribute("type"), "password");
  const browserState = await page.evaluate(() => ({ local: JSON.stringify(localStorage), session: JSON.stringify(sessionStorage), html: document.documentElement.outerHTML }));
  for (const value of Object.values(browserState)) assert.ok(!value.includes(keyValue), "Secret must not appear in browser storage or HTML");
}
(async () => {
  try { const response = await fetch(`${base}/health`, { signal: AbortSignal.timeout(500) }); if (response) throw new Error("TEST_PORT_IN_USE"); }
  catch (error) { if (error.message === "TEST_PORT_IN_USE") throw new Error("Port 8977 already in use; refusing to reuse an unknown service."); }
  await startServer();
  browser = await playwright.chromium.launch(browserOptions());
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, deviceScaleFactor: 1 });
  await context.route("**/*", route => {
    const url = route.request().url();
    if (url.startsWith(`${base}/`) || url.startsWith("data:")) return route.continue();
    blockedRequests.push(url);
    return route.abort();
  });
  page = await context.newPage();
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") consoleErrors.push(message.text()); });
  page.on("request", request => requests.push({ method: request.method(), path: new URL(request.url()).pathname }));
  page.on("response", async response => {
    if (![`${base}/api/settings/ai`, `${base}/api/state`].includes(response.url())) return;
    try { if ((await response.text()).includes(keyValue)) responseLeaks.push(response.url()); } catch {}
  });
  await page.goto(base);
  await check("Settings loads once with a masked empty key and native labels", async () => {
    await openSettings();
    assert.equal(await page.locator('.model-settings input[value="demo"]').isChecked(), true);
    assert.equal(await setting("api_key").getAttribute("autocomplete"), "new-password");
    assert.equal(await setting("api_key").evaluate(input => input.validity.valid && !input.required && input.labels.length === 1), true);
    assert.equal(await page.locator(".model-settings-advanced").getAttribute("open"), null);
    assert.equal(requests.filter(request => request.path === "/api/settings/ai" && request.method === "GET").length, 1);
    await assertNoKeyReadback();
  });
  await check("Save clears key immediately, updates badge, keeps feedback and does not refetch settings", async () => {
    await page.locator('.model-settings input[value="cloud"]').check();
    await setting("base_url").fill("https://model.test/v1");
    await setting("model").fill("fictional-test-model");
    await setting("api_key").fill(keyValue);
    await page.locator(".model-settings-advanced summary").click();
    await setting("timeout_seconds").fill("90");
    await page.locator(".model-settings-advanced summary").click();
    const readsBefore = requests.filter(request => request.path === "/api/settings/ai" && request.method === "GET").length;
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    await page.route("**/api/settings/ai", async route => {
      if (route.request().method() === "PUT") await gate;
      await route.continue();
    });
    const response = page.waitForResponse(item => item.url() === `${base}/api/settings/ai` && item.request().method() === "PUT");
    await saveButton().click();
    assert.equal(await setting("api_key").inputValue(), "");
    assert.equal(await page.getByRole("button", { name: "正在保存…", exact: true }).isDisabled(), true);
    release();
    assert.equal((await response).status(), 200);
    await page.waitForFunction(() => document.querySelector("#mode-status")?.textContent.includes("fictional-test-model"));
    await page.unroute("**/api/settings/ai");
    assert.match(await page.locator(".model-settings-feedback").innerText(), /设置已保存/);
    assert.equal(requests.filter(request => request.path === "/api/settings/ai" && request.method === "GET").length, readsBefore);
    await assertNoKeyReadback();
    await screenshot("01-settings-desktop-saved");
  });
  await check("Browser reload and service restart retain settings without returning the key", async () => {
    await page.reload();
    await openSettings();
    assert.equal(await setting("base_url").inputValue(), "https://model.test/v1");
    assert.equal(await setting("model").inputValue(), "fictional-test-model");
    assert.equal(await setting("timeout_seconds").inputValue(), "90");
    await assertNoKeyReadback();
    await stopServer();
    await startServer();
    await page.reload();
    await openSettings();
    assert.equal((await api("/api/settings/ai")).api_key_configured, true);
    assert.equal(await setting("model").inputValue(), "fictional-test-model");
    await assertNoKeyReadback();
  });
  await check("An empty key preserves the saved key and remains valid in the browser", async () => {
    assert.equal(await setting("api_key").inputValue(), "");
    assert.equal(await setting("api_key").evaluate(input => input.checkValidity()), true);
    await setting("model").fill("fictional-test-model-2");
    const saved = await save();
    assert.equal(saved.api_key_configured, true);
    assert.equal(saved.model, "fictional-test-model-2");
    const stored = JSON.parse(fs.readFileSync(path.join(dataDir, "model-settings.json"), "utf8"));
    assert.equal(stored.api_key, keyValue);
  });
  await check("Switching servers with an old key returns 422 and preserves the draft", async () => {
    await setting("base_url").fill("https://other-model.test/v1");
    await save(422);
    assert.match(await page.locator(".model-settings .inline-error").innerText(), /更换模型服务器/);
    assert.equal(await setting("base_url").inputValue(), "https://other-model.test/v1");
    assert.equal((await api("/api/settings/ai")).base_url, "https://model.test/v1");
    await screenshot("02-settings-server-change-validation");
  });
  await check("Explicit clear deletes the key, disables key entry and updates readiness", async () => {
    await setting("api_key").fill(keyValue);
    await setting("clear_api_key").check();
    assert.equal(await setting("api_key").inputValue(), "");
    assert.equal(await setting("api_key").isDisabled(), true);
    assert.equal(await setting("api_key").evaluate(input => input.willValidate), false);
    const saved = await save();
    assert.equal(saved.api_key_configured, false);
    assert.equal(saved.configured, false);
    assert.equal(saved.base_url, "https://other-model.test/v1");
    await page.waitForFunction(() => document.querySelector("#mode-status")?.textContent.includes("待补全配置"));
    assert.match(await page.locator(".model-settings-feedback").innerText(), /尚未填完整/);
    assert.equal(JSON.parse(fs.readFileSync(path.join(dataDir, "model-settings.json"), "utf8")).api_key, "");
    await assertNoKeyReadback();
  });
  await check("Leaving settings clears the detached password input and reloads cleanly", async () => {
    await setting("api_key").fill(keyValue);
    const detachedInput = await setting("api_key").elementHandle();
    await page.locator('button[data-view="materials"]').click();
    assert.equal(await detachedInput.evaluate(input => input.isConnected), false);
    assert.equal(await detachedInput.evaluate(input => input.value), "");
    await detachedInput.dispose();
    assert.equal(await page.locator('input[type="password"]').count(), 0);
    await openSettings();
    assert.equal(await setting("api_key").inputValue(), "");
  });
  await check("Leaving while settings loads never reattaches the panel or preserves a key", async () => {
    await page.locator('button[data-view="materials"]').click();
    let release, intercepted;
    const gate = new Promise(resolve => { release = resolve; });
    const waiting = new Promise(resolve => { intercepted = resolve; });
    await page.route("**/api/settings/ai", async route => {
      if (route.request().method() === "GET") { intercepted(); await gate; }
      await route.continue();
    });
    await page.locator('button[data-view="settings"]').click();
    await waiting;
    const detachedPanel = await page.locator(".model-settings").elementHandle();
    assert.equal(await page.locator(".model-settings-form").isVisible(), false);
    await page.locator('button[data-view="materials"]').click();
    const loaded = page.waitForResponse(response => response.url() === `${base}/api/settings/ai`);
    release();
    await loaded;
    await page.waitForFunction(node => node.getAttribute("aria-busy") === "false", detachedPanel);
    assert.equal(await detachedPanel.evaluate(node => node.isConnected), false);
    assert.equal(await detachedPanel.evaluate(node => node.querySelector('[name="api_key"]').value), "");
    assert.equal(await page.locator(".model-settings").count(), 0);
    assert.equal(await page.evaluate(() => app.view), "materials");
    await detachedPanel.dispose();
    await page.unroute("**/api/settings/ai");
    await openSettings();
  });  await check("Network failure preserves non-secret drafts, clears the key and permits retry", async () => {
    await setting("base_url").fill("https://model.test/v1");
    await setting("model").fill("draft-after-network-error");
    await setting("api_key").fill(keyValue);
    await page.route("**/api/settings/ai", route => route.request().method() === "PUT" ? route.abort("failed") : route.continue());
    await saveButton().click();
    await page.waitForFunction(() => document.querySelector(".model-settings .inline-error")?.textContent.includes("重试前请重新填写"));
    assert.equal(await setting("api_key").inputValue(), "");
    assert.equal(await setting("base_url").inputValue(), "https://model.test/v1");
    assert.equal(await setting("model").inputValue(), "draft-after-network-error");
    assert.equal(await saveButton().isEnabled(), true);
    assert.equal((await api("/api/settings/ai")).model, "fictional-test-model-2");
    await screenshot("03-settings-network-error-draft");
    await page.unroute("**/api/settings/ai");
    await setting("api_key").fill(keyValue);
    await save();
  });
  await check("Busy service returns 409 without losing drafts or mutating stored settings", async () => {
    await api("/__test/busy/1", { method: "POST" });
    await setting("model").fill("draft-during-busy-service");
    await save(409);
    assert.match(await page.locator(".model-settings .inline-error").innerText(), /进行|完成/);
    assert.equal(await setting("model").inputValue(), "draft-during-busy-service");
    assert.equal((await api("/api/settings/ai")).model, "draft-after-network-error");
    await api("/__test/busy/0", { method: "POST" });
    await save();
  });
  await check("Native URL and timeout validation prevent invalid submissions", async () => {
    const puts = () => requests.filter(request => request.method === "PUT" && request.path === "/api/settings/ai").length;
    let previous = puts();
    await setting("base_url").fill("not a url");
    await saveButton().click();
    assert.equal(await setting("base_url").evaluate(input => input.validity.typeMismatch), true, JSON.stringify(await setting("base_url").evaluate(input => ({ type: input.type, value: input.value, disabled: input.disabled, fieldsetDisabled: input.closest("fieldset").disabled, message: input.validationMessage }))));
    assert.equal(puts(), previous);
    await setting("base_url").fill("https://model.test/v1");
    await page.locator(".model-settings-advanced summary").click();
    await setting("timeout_seconds").fill("181");
    await page.locator(".model-settings-advanced summary").click();
    previous = puts();
    await saveButton().click();
    assert.equal(await setting("timeout_seconds").evaluate(input => input.validity.rangeOverflow), true);
    assert.equal(puts(), previous);
    assert.equal(await setting("timeout_seconds").isVisible(), true, "Browser must reveal invalid field inside details");
    await setting("timeout_seconds").fill("75");
    await save();
  });
  await check("Explicit verification shows pending, success and failure colors and allows retry", async () => {
    const verify = () => page.getByRole("button", { name: "验证连接", exact: true });
    const badge = page.locator("#mode-status");
    assert.equal(await badge.getAttribute("data-connection"), "unverified");
    assert.equal(await verify().isEnabled(), true);
    await verify().click();
    assert.equal(await page.getByRole("button", { name: "正在验证…", exact: true }).isDisabled(), true);
    assert.equal(await badge.getAttribute("data-connection"), "testing");
    assert.equal(await saveButton().isDisabled(), true);
    await page.locator('[data-view="materials"]').click();
    await openSettings();
    await page.waitForFunction(() => document.querySelector("#mode-status")?.dataset.connection === "success");
    await page.waitForFunction(() => document.querySelector(".model-settings-test-result")?.dataset.connection === "success");
    assert.equal(await verify().isEnabled(), true, "Returning during verification must not leave the new panel stuck in testing");
    assert.match(await page.locator(".model-settings-test-result").innerText(), /连接验证成功.*耗时/);
    assert.equal(await badge.evaluate(el => getComputedStyle(el, "::before").backgroundColor), "rgb(39, 130, 87)");
    await screenshot("05-connection-success");
    await page.reload();
    await openSettings();
    assert.equal(await badge.getAttribute("data-connection"), "success", "Reload reuses last explicit result without sending another test");
    assert.equal((await api("/__test/metrics")).test_calls, 1);
    await verify().click();
    await page.waitForFunction(() => document.querySelector("#mode-status")?.dataset.connection === "error");
    assert.match(await page.locator(".model-settings-test-result").innerText(), /API Key/);
    assert.equal(await badge.evaluate(el => getComputedStyle(el, "::before").backgroundColor), "rgb(199, 67, 67)");
    await screenshot("06-connection-error");
    await verify().click();
    await page.waitForFunction(() => document.querySelector("#mode-status")?.dataset.connection === "success");
    await setting("model").fill("changed-model-requires-saving");
    assert.equal(await verify().isDisabled(), true);
    assert.match(await page.locator(".model-settings-actions .helper").innerText(), /先保存/);
    await save();
    assert.equal(await badge.getAttribute("data-connection"), "unverified");
    assert.equal(await verify().isEnabled(), true);
  });
  await check("Desktop, tablet and phone widths have no horizontal overflow", async () => {
    for (const width of [1440, 1024, 736, 390, 320]) {
      await page.setViewportSize({ width, height: 1100 });
      const dimensions = await page.evaluate(() => ({ viewport: document.documentElement.clientWidth, content: document.documentElement.scrollWidth }));
      assert.ok(dimensions.content <= dimensions.viewport + 1, `${width}px overflows: ${JSON.stringify(dimensions)}`);
      assert.equal(await setting("api_key").isVisible(), true);
      await screenshot(`04-settings-${width}`);
    }
  });
  await check("No model calls, external requests, secret readbacks or browser exceptions", async () => {
    assert.deepEqual(await api("/__test/metrics"), { model_calls: 0, http_calls: 0, test_calls: 3 });
    assert.deepEqual(blockedRequests, []);
    assert.deepEqual(responseLeaks, []);
    assert.deepEqual(errors, []);
    assert.deepEqual(requests.filter(request => request.method === "POST").map(request => request.path), Array(3).fill("/api/settings/ai/test"));
    const unexpected = consoleErrors.filter(message => !/422|409|net::ERR_FAILED/.test(message));
    assert.deepEqual(unexpected, []);
    await assertNoKeyReadback();
  });
})().catch(async error => {
  errors.push(error.stack || String(error));
  console.error(error.stack || error);
  if (page) { try { await screenshot("failure"); } catch {} }
  process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close();
  try { await stopServer(); } catch (error) { errors.push(error.message); process.exitCode = 1; }
  const report = { runId, offline: true, baseUrl: base, results, errors, consoleErrors, blockedRequests, responseLeaks, requests, directory: runDir };
  fs.writeFileSync(path.join(runDir, "report.json"), JSON.stringify(report, null, 2));
  fs.writeFileSync(path.join(output, "latest-report.json"), JSON.stringify(report, null, 2));
  console.log(`Report: ${path.join(runDir, "report.json")}`);
});
