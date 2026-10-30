"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createAnkiIntegration } = require("./anki-integration.cjs");
const { EventEmitter } = require("node:events");
const { showAnkiWindow } = require("./anki-window.cjs");
test("native setup persists chosen locations and forwards only the selected data root", async () => {
  const root = path.resolve(__dirname, "../.ui-checks/native-anki-helper", Date.now().toString());
  const program = path.join(root, "Anki", "anki.exe"), data = path.join(root, "AnkiData"), userData = path.join(root, "desktop");
  fs.mkdirSync(path.dirname(program), { recursive: true }); fs.writeFileSync(program, "not executed");
  fs.mkdirSync(data, { recursive: true }); fs.writeFileSync(path.join(data, "prefs21.db"), "fixture");
  const chosen = [program, data], requests = [];
  const make = () => createAnkiIntegration({ userData, projectRoot: root, backendUrl: "http://127.0.0.1:18888", getWindow: () => null, shell: {},
    dialog: { showOpenDialog: async () => ({ canceled: false, filePaths: [chosen.shift()] }) },
    request: async (url, options) => { requests.push({ url, options }); return { ok: true, json: async () => ({ installed: true, changed: true }) }; } });
  const bridge = make();
  assert.equal((await bridge.chooseProgram()).executable, program);
  assert.equal((await bridge.chooseData()).dataDirectory, data);
  assert.equal((await make().detect()).dataDirectory, data);
  // No registry/global application locations or personal files are written.
  const saved = JSON.parse(fs.readFileSync(path.join(userData, "anki-setup.json")));
  assert.deepEqual(saved, { executable: program, dataDirectory: data });
  const state = await bridge.detect();
  if (!state.running) {
    assert.equal((await bridge.install()).installed, true);
    assert.deepEqual(JSON.parse(requests[0].options.body), { anki_base: data });
  } else await assert.rejects(bridge.install(), /退出 Anki/);
});

function launchFixture(overrides) {
  const root = path.resolve(__dirname, "../.ui-checks/native-anki-launch", `${Date.now()}-${Math.random().toString(16).slice(2)}`);
  const userData = path.join(root, "desktop"), executable = path.join(root, "Anki", "anki.exe"), dataDirectory = path.join(root, "AnkiData");
  fs.mkdirSync(userData, { recursive: true }); fs.mkdirSync(path.dirname(executable), { recursive: true }); fs.mkdirSync(dataDirectory);
  fs.writeFileSync(executable, "not executed"); fs.writeFileSync(path.join(dataDirectory, "prefs21.db"), "fixture");
  fs.writeFileSync(path.join(userData, "anki-setup.json"), JSON.stringify({ executable, dataDirectory }));
  return { executable, dataDirectory, bridge: createAnkiIntegration({ userData, projectRoot: root, backendUrl: "http://127.0.0.1:18888", ...overrides }) };
}

test("opening Anki starts a visible GUI with the selected data directory", async () => {
  let call, unreferenced = false;
  const { bridge, executable, dataDirectory } = launchFixture({ isRunning: async () => false,
    start: (...args) => { call = args; const child = new EventEmitter(); child.unref = () => { unreferenced = true; }; queueMicrotask(() => child.emit("spawn")); return child; },
    showWindow: () => assert.fail("A new launch should not restore an existing process") });
  const result = await bridge.launch();
  assert.equal(call[0], executable); assert.deepEqual(call[1], ["-b", dataDirectory]);
  assert.equal(call[2].windowsHide, false); assert.equal(unreferenced, true);
  assert.match(result.message, /正在打开/);
});

test("an existing hidden or minimized Anki is shown without starting another instance", async () => {
  let selected;
  const { bridge, executable } = launchFixture({ isRunning: async () => true, start: () => assert.fail("Never duplicate an already running Anki"),
    showWindow: async value => { selected = value; return { visible: true, activated: true }; } });
  assert.match((await bridge.launch()).message, /已显示 Anki/); assert.equal(selected, executable);
});

test("failed window restoration never claims that Anki is visible", async () => {
  for (const showWindow of [async () => ({ visible: false }), async () => { throw new Error("fixture failure"); }]) {
    const { bridge } = launchFixture({ isRunning: async () => true, start: () => assert.fail("No extra process"), showWindow });
    await assert.rejects(bridge.launch(), /窗口/);
  }
  const { bridge } = launchFixture({ isRunning: async () => true, showWindow: async () => ({ visible: true, activated: false }) });
  assert.match((await bridge.launch()).message, /任务栏/);
});

test("window recovery uses a fixed helper and passes the chosen program as data", async () => {
  const executable = 'F:\\Anki space\\Anki.exe';
  const result = await showAnkiWindow(executable, { platform: "win32", run: async (program, args, options) => {
    assert.equal(program, "powershell.exe"); assert.ok(!args.at(-1).includes(executable));
    assert.equal(options.env.ANKISENSE_TARGET_ANKI, executable); assert.equal(options.windowsHide, true);
    return { stdout: '{"visible":true,"activated":true}' };
  } });
  assert.deepEqual(result, { visible: true, activated: true });
});
