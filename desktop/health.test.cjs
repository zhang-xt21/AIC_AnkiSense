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
