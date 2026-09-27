"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const CardImages = require("../ankisense/static/card-images.js");

const front = { media_id: "media-front", side: "question", caption: "题中条件" };
const back = { media_id: "media-back", side: "answer", caption: "解题讲义" };
const current = { images: [{ media_id: "media-current", side: "answer", caption: "最新图" }] };
const attempt = changes => ({ status: "completed", answer: "后进先出", diagnosis: { verdict: "correct" }, card_snapshot: { images: [front, back] }, ...changes });

test("legacy and malformed cards have no rendered image references", () => {
  for (const card of [null, {}, { images: null }, { images: [null, { media_id: "x", side: "elsewhere" }, { side: "question" }] }]) assert.deepEqual(CardImages.refs(card), []);
  const card = { images: [front, { ...back, caption: null }] };
  assert.deepEqual(CardImages.refs(card), [front, { ...back, caption: "" }]);
  CardImages.refs(card)[0].caption = "local edit";
  assert.equal(card.images[0].caption, "题中条件");
});

test("questions and history use the correct side and immutable submission snapshot", () => {
  assert.deepEqual(CardImages.questionImages({ images: [front, back] }), [front]);
  assert.deepEqual(CardImages.questionImages(current, attempt()), [front]);
  assert.deepEqual(CardImages.questionImages({ images: [front] }, attempt({ card_snapshot: null })), []);
  assert.deepEqual(CardImages.answerImages(attempt()), [back]);
  assert.deepEqual(CardImages.answerImages(attempt({ card_snapshot: null })), []);
});

test("answer images require a nonempty completed attempt with a valid diagnosis", () => {
  for (const verdict of ["correct", "incomplete", "incorrect", "uncertain"]) assert.deepEqual(CardImages.answerImages(attempt({ diagnosis: { verdict } })), [back]);
  for (const changes of [{ answer: "" }, { answer: " \n\t" }, { answer: null }, { status: "pending" }, { status: "failed" }, { status: "unanswered" }, { diagnosis: null }, { diagnosis: { verdict: "invalid" } }]) assert.deepEqual(CardImages.answerImages(attempt(changes)), [], JSON.stringify(changes));
  assert.deepEqual(CardImages.answerImages(null), []);
});

test("a new failed or blank submission never inherits answer images from old completion", () => {
  const completed = attempt();
  const latest = attempt({ status: "failed" });
  assert.equal(CardImages.answerImages(completed).length, 1);
  assert.deepEqual(CardImages.answerImages(latest), []);
  assert.deepEqual(CardImages.answerImages(attempt({ status: "unanswered", answer: "" })), []);
});

test("media lookup accepts only this application's immutable local route", () => {
  const record = { id: "media-front", url: "/api/card-media/media-front/file", width: 100, height: 100 };
  assert.equal(CardImages.mediaFor(front, [record]), record);
  for (const url of ["https://example.com/image.png", "//example.com/image.png", "javascript:alert(1)", "data:image/png;base64,eA==", "/api/card-media/other/file", "/api/card-media/media-front/file?redirect=https://example.com", "/api/documents/example/pages/1/image"]) assert.equal(CardImages.mediaFor(front, [{ ...record, url }]), null, url);
  assert.equal(CardImages.mediaFor(front, []), null);
  assert.equal(CardImages.mediaFor({ media_id: "../media" }, [{ id: "../media", url: "/api/card-media/..%2Fmedia/file" }]), null);
});

test("plain script exposes a browser global without executing DOM work", () => {
  const context = vm.createContext({});
  vm.runInContext(fs.readFileSync(require.resolve("../ankisense/static/card-images.js"), "utf8"), context);
  assert.equal(typeof context.CardImages.gallery, "function");
  assert.equal(context.CardImages.answerImages(attempt()).length, 1);
});
