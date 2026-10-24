"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createAnkiIntegration } = require("./anki-integration.cjs");
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
