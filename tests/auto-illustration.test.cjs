"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const illustration = require("../ankisense/static/auto-illustration.js");
const card = (need, state, images = []) => ({ image_intent: { need }, auto_illustration: { state }, images });

test("only unfinished image work keeps state polling alive", () => {
  for (const state of ["queued", "matching", "checking"]) assert.equal(illustration.active(card("helpful", state)), true);
  for (const state of ["attached", "not_found", "needs_review", "failed", "interrupted", "skipped", "unknown"]) assert.equal(illustration.active(card("helpful", state)), false);
  assert.equal(illustration.active({}), false);
});
test("a question that needs an image cannot use an answer-side image as its question illustration", () => {
  assert.equal(illustration.requiredMissing(card("required", "not_found")), true);
  assert.equal(illustration.requiredMissing(card("required", "attached", [{ media_id: "image", side: "answer" }])), true);
  assert.equal(illustration.requiredMissing(card("required", "attached", [{ media_id: "image", side: "question" }])), false);
  assert.equal(illustration.requiredMissing(card("helpful", "not_found")), false);
});
test("failed optional illustrations allow the text card to keep being checked", () => {
  for (const state of ["not_found", "failed", "interrupted", "skipped"]) {
    const result = illustration.describe(card("helpful", state));
    assert.match(result.detail, /不影响文字卡片继续检查/);
    assert.doesNotMatch(result.detail, /不能开始学习/);
  }
});
test("attached pictures do not imply whole-card approval", () => {
  const result = illustration.describe(card("required", "attached", [{ media_id: "image", side: "question" }]));
  assert.match(result.detail, /内容检查通过后/);
  assert.match(illustration.describe(card("required", "queued")).detail, /配图完成并通过检查后才能/);
});
test("legacy cards stay quiet and explicitly unnecessary images are named clearly", () => {
  assert.equal(illustration.describe({}), null);
  assert.equal(illustration.describe(card("none", "skipped")).label, "无需配图");
  assert.match(illustration.describe({ image_intent: { need: "required" } }).detail, /需要看图作答/);
});
test("browser global can be loaded without DOM access", () => {
  const context = vm.createContext({});
  vm.runInContext(fs.readFileSync(require.resolve("../ankisense/static/auto-illustration.js"), "utf8"), context);
  assert.equal(context.AutoIllustration.active(card("helpful", "checking")), true);
});
