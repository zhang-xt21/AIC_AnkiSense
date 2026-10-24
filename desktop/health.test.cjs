"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const { EventEmitter } = require("node:events");
const { probeBackend } = require("./backend-health.cjs");

test("only the compatible, healthy AnkiSense API is ready", async () => {
  const original = http.get;
  try {
    for (const [statusCode, body, ready] of [
      [200, { application: "AnkiSense", api_version: 1, status: "ok" }, true],
      [200, { application: "OtherApp", api_version: 1, status: "ok" }, false],
      [200, { application: "AnkiSense", api_version: 2, status: "ok" }, false],
      [503, { application: "AnkiSense", api_version: 1, status: "ok" }, false],
      [200, { application: "AnkiSense", api_version: 1, status: "starting" }, false],
      [200, "<html>another service</html>", false],
    ]) {
      http.get = (options, callback) => {
        assert.equal(options.path, "/health");
        const request = new EventEmitter(), response = new EventEmitter();
        response.statusCode = statusCode; response.setEncoding = () => {}; response.destroy = () => {};
        queueMicrotask(() => { callback(response); response.emit("data", typeof body === "string" ? body : JSON.stringify(body)); response.emit("end"); });
        return request;
      };
      assert.deepEqual((await probeBackend()).ready, ready);
    }
  } finally { http.get = original; }
});

test("the setup desktop rejects a legacy backend instead of silently reusing it", async () => {
  const original = http.get;
  let supportsSetup = false;
  try {
    http.get = (_options, callback) => {
      const request = new EventEmitter(), response = new EventEmitter();
      response.statusCode = 200; response.setEncoding = () => {};
      queueMicrotask(() => { callback(response); response.emit("data", JSON.stringify({ application: "AnkiSense", api_version: 1, status: "ok", ...(supportsSetup ? { capabilities: { anki_setup: true } } : {}) })); response.emit("end"); });
      return request;
    };
    assert.equal((await probeBackend({ requireAnkiSetup: true })).ready, false);
    supportsSetup = true;
    assert.equal((await probeBackend({ requireAnkiSetup: true })).ready, true);
  } finally { http.get = original; }
});

test("connection verification requires an explicit capability even on a backend with Anki setup", async () => {
  const original = http.get;
  try {
    for (const capability of [undefined, false, "true", true]) {
      http.get = (_options, callback) => {
        const request = new EventEmitter(), response = new EventEmitter();
        response.statusCode = 200; response.setEncoding = () => {};
        queueMicrotask(() => {
          callback(response);
          response.emit("data", JSON.stringify({ application: "AnkiSense", api_version: 1, status: "ok",
            capabilities: { anki_setup: true, ai_connection_test: capability } }));
          response.emit("end");
        });
        return request;
      };
      const result = await probeBackend({ requireAnkiSetup: true, requireConnectionTest: true });
      assert.equal(result.ready, capability === true);
      if (!result.ready) assert.match(result.reason, /退出旧版/);
      assert.equal((await probeBackend({ requireAnkiSetup: true })).ready, true);
    }
  } finally { http.get = original; }
});
