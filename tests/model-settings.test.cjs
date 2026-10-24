"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { renderStatus } = require("../ankisense/static/model-settings.js");

test("model badge distinguishes saved configuration from an explicit connection result", () => {
  const badge = { dataset: {}, textContent: "", title: "" };
  const settings = { mode: "cloud", model: "fixture-model", configured: true };
  for (const [status, label] of [["unverified", "未验证"], ["testing", "验证中"], ["success", "验证通过"], ["error", "验证失败"]]) {
    renderStatus(badge, { ...settings, model_connection: { status } });
    assert.equal(badge.dataset.connection, status);
    assert.ok(badge.textContent.includes(label));
    assert.ok(badge.textContent.includes(settings.model));
  }
  renderStatus(badge, { ...settings, model_connection: { status: "success", checked_at: "2026-10-24T08:00:00Z", message: "连接验证成功" } });
  assert.match(badge.title, /上次验证.*持续在线/);
  renderStatus(badge, { ...settings, configured: false });
  assert.match(badge.textContent, /待补全配置/);
  renderStatus(badge, { ...settings, mode: "demo", model_connection: { status: "success" } });
  assert.equal(badge.dataset.connection, "demo");
  assert.match(badge.textContent, /演示模式/);
});
