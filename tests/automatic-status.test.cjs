"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { automaticPlanLabel, automaticFailure, summarizeAutomaticIssues, automaticProgressLabel, automaticCoverageLabel } = require("../ankisense/static/automatic.js");

test("source-page citations are reported separately from knowledge coverage", () => {
  const label = automaticCoverageLabel({ planned_source_pages: [1,2,3,4], card_cited_pages: [1,4], uncited_source_pages: [2,3] });
  assert.match(label, /2\/4/);
  assert.match(label, /第 2、3 页/);
  assert.match(label, /不等于知识点覆盖率/);
});

test("all attempted batches never imply that every batch succeeded", () => {
  const job = { batch_summary: { total: 17, successful: 5, failed: 10, partial: 2, running: 0, pending: 0 } };
  assert.equal(automaticProgressLabel(job, { total: 17, completed: 17 }), "成功 5/17 批 · 失败 10 批 · 部分完成 2 批");
  assert.equal(automaticProgressLabel({}, { total: 17, completed: 17 }), "已尝试批次 17/17");
});

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
  old.status = "failed";
  old.job.remaining_details = [{ reasons: ["当前引文校验失败"] }];
  assert.equal(automaticFailure(old), null, "Current checkpoint errors take precedence over old network failures");
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
