"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { automaticPlanLabel, automaticFailure, summarizeAutomaticIssues } = require("../ankisense/static/automatic.js");

test("automatic quantity is readable instead of rendering null or undefined", () => {
  assert.equal(automaticPlanLabel({ recommended_count: 85, target_count: null, total_batches: 67 }), "系统建议 85 张 · 本次按材料自动决定数量 · 共 67 批");
  assert.match(automaticPlanLabel({ target_count: 5 }), /本次目标 5 张/);
  assert.doesNotMatch(automaticPlanLabel({}), /null|undefined/);
});

test("current service failures and older failed imports have actionable messages", () => {
  const failure = { code: "connection", message: "offline" };
  assert.equal(automaticFailure({ status: "failed", failure }), failure);
  assert.equal(automaticFailure({ status: "running", failure }), null);
  assert.equal(automaticFailure({ status: "cancelled", failure }), null);
  const old = { status: "failed", job: { card_ids: [], issues: [{ code: "batch_failed", reason: "无法连接模型服务，请检查网络与接口地址后重试。" }] } };
  assert.equal(automaticFailure(old).code, "model_service");
  old.status = "completed";
  assert.equal(automaticFailure(old), null);
  old.status = "partial";
  assert.equal(automaticFailure(old), null, "A later partial result must not reuse an old connection error");
});

test("repeated signals are grouped and failed requests appear before extraction warnings", () => {
  const groups = summarizeAutomaticIssues([
    { code: "critical_number", reason: "含数字", page: 2 },
    { code: "critical_number", reason: "含数字", pages: [2, 5] },
    { code: "batch_failed", reason: "无法连接", pages: [8, 9] },
  ]);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].code, "batch_failed");
  assert.equal(groups[1].count, 2);
  assert.deepEqual(groups[1].pages, [2, 5]);
});
