"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const Figures = require("../ankisense/static/figure-recommendations.js");
const card = { version: 4, question: "栈如何入栈？", concept: "栈", evidence: [{ block_id: "p1", quote: "在栈顶插入" }] };
const draft = () => ({ ...card, evidence: JSON.stringify(card.evidence, null, 2), images: [] });

test("recommendations survive attachment-only edits but not changed source or question", () => {
  assert.equal(Figures.sameSource(card, draft()), true);
  assert.equal(Figures.sameSource(card, { ...draft(), images: [{ media_id: "new" }], dirty: true }), true);
  for (const changes of [{ question: "队列如何入队？" }, { concept: "队列" }, { version: 5 }, { evidence: "broken json" }, { evidence: JSON.stringify([{ block_id: "p2", quote: "在栈顶插入" }]) }, { evidence: JSON.stringify([{ block_id: "p1", quote: "栈顶" }]) }]) {
    assert.equal(Figures.sameSource(card, { ...draft(), ...changes }), false);
  }
});
test("crop coordinates reject reversed, empty, non-finite and out-of-page rectangles", () => {
  assert.equal(Figures.validBox([0, 0, 1, 1]), true);
  assert.equal(Figures.validBox([.1, .2, .8, .6]), true);
  for (const box of [null, [], [0, 0, 1], [0, 0, 1, 1, 1], [0, 0, NaN, 1], [0, 0, Infinity, 1], [0, 0, "1", 1], [-.1, 0, 1, 1], [0, 0, 1.1, 1], [.5, .5, .4, .4], [0, 0, .001, 1], [0, 0, 1, 0]]) {
    assert.equal(Figures.validBox(box), false, String(box));
  }
});
test("dragging works in every direction and clips to the page edges", () => {
  for (const [start, end] of [[[.1, .2], [.8, .9]], [[.8, .9], [.1, .2]], [[.1, .9], [.8, .2]], [[.8, .2], [.1, .9]]]) {
    assert.deepEqual(Figures.dragBox(start, end), [.1, .2, .8, .9]);
  }
  assert.deepEqual(Figures.dragBox([.2, .3], [2, -.5]), [.2, 0, 1, .3]);
});
test("candidate previews accept only application's local image routes", () => {
  for (const url of ["/api/image-recommendations/job-a/candidates/figure-1/image", "/api/documents/pdf-ab/pages/8/image"]) assert.equal(Figures.localImage(url), true);
  for (const url of ["https://example.com/a.png", "//example.com/a", "data:image/png;base64,eA==", "javascript:alert(1)", "/api/documents/a/../../secrets", "/api/documents/a/image?redirect=https://example.com", "/api/card-media/x/file", null]) assert.equal(Figures.localImage(url), false);
});

test("visual checks bind the saved answer, scoring points and card kind", () => {
  const saved = { ...card, answer: "后进先出", kind: "basic", rubric: [{ point_id: "lifo", text: "最后进入的元素先出来" }] };
  const edited = { ...saved, evidence: JSON.stringify(saved.evidence), rubric: "lifo | 最后进入的元素先出来", images: [] };
  assert.equal(Figures.sameCheckSource(saved, edited), true);
  for (const change of [{ answer: "先进先出" }, { kind: "remediation" }, { rubric: "lifo | 不同标准" }, { rubric: "bad" }, { question: "队列呢？" }, { version: 9 }]) {
    assert.equal(Figures.sameCheckSource(saved, { ...edited, ...change }), false);
  }
  assert.equal(Figures.sameCheckSource(saved, { ...edited, images: [{ media_id: "x" }] }), true);
});

test("visual results from other crops, display sides and jobs are never reused", () => {
  const key = Figures.checkKey("job-1", "candidate-1", [.1, .2, .8, .9], "answer");
  assert.equal(key, Figures.checkKey("job-1", "candidate-1", [.1, .2, .8, .9], "answer"));
  for (const args of [["job-2", "candidate-1", [.1, .2, .8, .9], "answer"], ["job-1", "candidate-2", [.1, .2, .8, .9], "answer"], ["job-1", "candidate-1", [.1, .2, .8, .91], "answer"], ["job-1", "candidate-1", [.1, .2, .8, .9], "question"]]) {
    assert.notEqual(key, Figures.checkKey(...args));
  }
});

test("an in-flight check refreshes the currently mounted panels after navigation", () => {
  const visits = [];
  const state = { checkPanels: new Map([
    ["old-card", { element: { isConnected: false }, refresh: () => visits.push("detached") }],
    ["candidate", { element: { isConnected: true }, refresh: () => visits.push("new-current") }],
    ["crop", { element: { isConnected: true }, refresh: () => visits.push("open-dialog") }],
  ]) };
  Figures.notifyCheckPanels(state);
  assert.deepEqual(visits, ["new-current", "open-dialog"]);
  assert.equal(state.checkPanels.has("old-card"), false);
});

test("moving an adopted candidate to the question side invalidates the answer-side check key", () => {
  const candidate = { id: "candidate-1", bbox: [.1, .2, .8, .9] };
  const media = [{ id: "media-1", source: { job_id: "job-1", candidate_id: candidate.id, bbox: candidate.bbox } }];
  const edited = { images: [{ media_id: "media-1", side: "answer" }] };
  const previous = Figures.candidateCheckScope(candidate, "job-1", edited, media);
  assert.equal(previous.side, "answer");
  edited.images[0].side = "question";
  const current = Figures.candidateCheckScope(candidate, "job-1", edited, media);
  assert.equal(current.side, "question");
  assert.equal(current.adopted, true);
  assert.notEqual(Figures.checkKey("job-1", candidate.id, candidate.bbox, previous.side), Figures.checkKey("job-1", candidate.id, candidate.bbox, current.side));
  assert.equal(Figures.candidateCheckScope(candidate, "job-2", edited, media).side, "answer");
});

test("a separately cropped attachment is identified instead of treated as the candidate's image", () => {
  const candidate = { id: "candidate-1", bbox: [.1, .2, .8, .9] };
  const media = [{ id: "media-1", source: { job_id: "job-1", candidate_id: candidate.id, bbox: [.1, .2, .8, .95] } }];
  const edited = { images: [{ media_id: "media-1", side: "question" }] };
  assert.deepEqual(Figures.candidateCheckScope(candidate, "job-1", edited, media), { side: "answer", adopted: false, otherCrop: true });
  edited.images = [];
  assert.equal(Figures.candidateCheckScope(candidate, "job-1", edited, media).otherCrop, false);
});
