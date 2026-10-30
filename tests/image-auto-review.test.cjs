"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const Review = require("../ankisense/static/image-auto-review.js");

const card = { id: "card-1", version: 3, images: [{ media_id: "img-1", side: "answer", caption: "栈顶示意图" }] };
const draft = { version: 3, dirty: false, images: [{ media_id: "img-1", side: "answer", caption: "栈顶示意图" }] };
const media = [{ id: "img-1", source: { origin: "figure_recommendation" } }];

test("only the saved matching recommendation may request a paid image check", () => {
  assert.deepEqual(Review.eligibleImages(card, draft, media), draft.images);
  assert.deepEqual(Review.eligibleImages(card, { ...draft, dirty: true }, media), []);
  assert.deepEqual(Review.eligibleImages(card, { ...draft, version: 2 }, media), []);
  assert.deepEqual(Review.eligibleImages(card, { ...draft, images: [{ ...draft.images[0], side: "question" }] }, media), []);
  assert.deepEqual(Review.eligibleImages(card, { ...draft, images: [{ ...draft.images[0], caption: "新说明" }] }, media), []);
  assert.deepEqual(Review.eligibleImages(card, draft, [{ id: "img-1", source: { origin: "upload" } }]), []);
  assert.deepEqual(Review.eligibleImages(card, { ...draft, images: [...draft.images, { media_id: "img-2", side: "answer", caption: "" }] }, media), draft.images);
});

test("a stale or uncertain result is never presented as automatic approval", () => {
  assert.equal(Review.labelFor({ status: "auto_pass", current: true }), "配图机审通过");
  assert.equal(Review.labelFor({ status: "auto_pass", current: false, review_id: "old" }), "检查结果已失效");
  assert.equal(Review.labelFor({ status: "unchecked", current: false, stale: true }), "检查结果已失效");
  assert.equal(Review.labelFor({ status: "unchecked", current: false, review_id: null }), "尚未检查");
  assert.equal(Review.labelFor({ status: "needs_review" }), "配图待核对");
  assert.equal(Review.labelFor({ status: "attention" }), "建议重裁或更换");
  assert.equal(Review.labelFor({ status: "unchecked" }), "尚未检查");
});


test("saved automatic source pages can be checked but unsaved changes cannot", () => {
  const pages = [{ id: "img-1", source: { origin: "automatic_source_page" } }];
  assert.deepEqual(Review.eligibleImages(card, draft, pages), draft.images);
  assert.deepEqual(Review.eligibleImages(card, { ...draft, dirty: true }, pages), []);
  assert.deepEqual(Review.eligibleImages(card, { ...draft, images: [{ ...draft.images[0], side: "question" }] }, pages), []);
});
