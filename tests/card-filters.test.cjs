"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { matches, filter, status } = require("../ankisense/static/card-filters.js");
const cards = [
  { id: "one", document_id: "a", status: "approved", question: "栈遵循 LIFO 顺序", concept: "栈", answer: "隐藏的答案", images: [{ media_id: "pic", side: "answer" }] },
  { id: "two", document_id: "a", status: "draft", question: "队列的顺序", review_state: "running" },
  { id: "three", document_id: "b", status: "draft", question: "机器指令", images: [] },
];
test("search combines normalized terms in the question and concept without searching answers", () => {
  assert.equal(matches(cards[0], { query: "  ｌｉｆｏ 栈  " }), true);
  assert.equal(matches(cards[0], { query: "lifo 队列" }), false);
  assert.equal(matches(cards[0], { query: "隐藏的答案" }), false);
  assert.equal(matches({}, { query: "任意" }), false);
});
test("document, status and image filters compose while preserving source order", () => {
  assert.deepEqual(filter(cards, { documentId: "a" }).map(card => card.id), ["one", "two"]);
  assert.deepEqual(filter(cards, { documentId: "a", status: "ready", images: "with" }).map(card => card.id), ["one"]);
  assert.deepEqual(filter(cards, { status: "checking", images: "without" }).map(card => card.id), ["two"]);
  assert.deepEqual(filter(cards, { query: "指令", status: "pending" }).map(card => card.id), ["three"]);
});
test("checking matches visible card state, while approved cards remain ready", () => {
  assert.equal(status({ status: "approved", review_state: "running" }), "ready");
  for (const state of ["queued", "matching", "checking"]) assert.equal(status({ auto_illustration: { state } }), "checking");
  assert.equal(status({ review_state: "failed" }), "pending");
});
test("malformed image references do not claim that a card has a displayable picture", () => {
  assert.equal(matches({ images: [{ media_id: "x", side: "invalid" }] }, { images: "without" }), true);
  assert.equal(matches({ images: [{ side: "answer" }] }, { images: "with" }), false);
  assert.deepEqual(filter(null), []);
});
