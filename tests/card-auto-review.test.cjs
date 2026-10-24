"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const Review = require("../ankisense/static/card-auto-review.js");

const card = { id: "card-1", version: 4, status: "draft", rubric: [{ point_id: "stack_lifo", text: "说明后进先出" }] };
const draft = { version: 4, dirty: false };

class FakeNode {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.handlers = new Map();
    this.className = "";
    this.disabled = false;
    this.ownText = "";
  }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(""); }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get isConnected() { return Boolean(this.connected || this.parentNode?.isConnected); }
  append(...children) { for (const child of children) { child.parentNode = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  setAttribute() {}
  addEventListener(name, handler) { this.handlers.set(name, [...(this.handlers.get(name) || []), handler]); }
  querySelector(selector) {
    const [tag, cls] = selector.split(".");
    return find(this, node => (!tag || node.tagName === tag) && (!cls || node.className.split(" ").includes(cls)));
  }
  async click() { if (!this.disabled) for (const handler of this.handlers.get("click") || []) await handler(); }
}

function find(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children) {
    const result = find(child, predicate);
    if (result) return result;
  }
  return null;
}

async function mount(t, options = {}) {
  const priorDocument = global.document;
  const priorWindow = global.window;
  global.document = { createElement: tag => new FakeNode(tag), querySelectorAll: () => [] };
  global.window = { confirm: options.confirm || (() => true) };
  t.after(() => { global.document = priorDocument; global.window = priorWindow; });
  const requests = [];
  const checked = [];
  const api = async (path, config) => {
    requests.push({ path, config });
    return config ? { status: "auto_pass", current: true } : options.getResult || { status: "unchecked", current: false, reasons: [] };
  };
  const section = Review.create({ card: options.card || card, draft: options.draft || draft, api,
    configured: true, run: callback => callback(), onChecked: result => checked.push(result), onStudy: () => {} });
  section.connected = true;
  await new Promise(resolve => setImmediate(resolve));
  const action = find(section, node => node.className.includes("card-auto-review-action"));
  return { section, action, requests, checked };
}

test("only a saved current card is eligible for a paid whole-card check", () => {
  assert.equal(Review.savedCard(card, draft), true);
  assert.equal(Review.savedCard(card, { ...draft, dirty: true }), false);
  assert.equal(Review.savedCard(card, { ...draft, version: 3 }), false);
  assert.equal(Review.savedCard({ ...card, source_outdated: true }, draft), false);
  assert.equal(Review.endpoint({ id: "a/b" }), "/api/cards/a%2Fb/auto-review");
});

test("an approved machine pass offers learning without manual approval", async t => {
  const h = await mount(t, { card: { ...card, status: "approved", approval: { method: "machine" } }, getResult: { status: "auto_pass", current: true, reasons: [], checks: {
    question_answerable: true, answer_relevant: true, answer_supported: true,
    rubric_checks: [{ point_id: "stack_lifo", verdict: "supported", block_id: "p1", exact_quote: "最后入栈的元素最先出栈" }],
    answer_citations: [{ block_id: "p1", exact_quote: "最后入栈的元素最先出栈" }]
  } } });
  assert.match(h.section.textContent, /机器审核通过，可开始学习/);
  assert.match(h.section.textContent, /评分要点逐项核对/);
  assert.match(h.section.textContent, /说明后进先出.*有原文支持/);
  assert.match(h.section.textContent, /最后入栈的元素最先出栈/);
  assert.equal(h.action.disabled, true);
  assert.equal(card.status, "draft");
});

test("loading status does not call the model; explicit retry sends the saved version", async t => {
  const h = await mount(t);
  assert.equal(h.action.disabled, false);
  assert.equal(h.requests.filter(request => request.config?.method === "POST").length, 0);
  await h.action.click();
  const posts = h.requests.filter(request => request.config?.method === "POST");
  assert.equal(posts.length, 1);
  assert.equal(posts[0].path, "/api/cards/card-1/auto-review");
  assert.deepEqual(JSON.parse(posts[0].config.body), { expected_version: 4 });
  assert.equal(h.checked.length, 1);
});

test("queued and running reviews disable paid checks and remain read-only", async t => {
  const h = await mount(t, { getResult: { status: "queued", current: false, reasons: [] } });
  assert.equal(h.action.disabled, true);
  assert.match(h.section.textContent, /等待自动检查/);
  await h.action.click();
  assert.equal(h.requests.filter(request => request.config?.method === "POST").length, 0);
  assert.equal(Review.labelFor({ status: "running" }), "正在自动检查…");
});

test("an automatic image task prevents a duplicate paid whole-card check", async t => {
  const h = await mount(t, { card: { ...card, auto_illustration: { state: "checking" } } });
  assert.equal(h.action.disabled, true);
  assert.match(h.section.textContent, /正在配图，完成后继续检查卡片/);
  await h.action.click();
  assert.equal(h.requests.filter(request => request.config?.method === "POST").length, 0);
});

test("a pass alone does not claim approval when the saved card is still a draft", async t => {
  const h = await mount(t, { getResult: { status: "auto_pass", current: true } });
  assert.match(h.section.textContent, /学习状态尚未更新/);
  assert.doesNotMatch(h.section.textContent, /机器审核通过，可开始学习/);
});

test("editing an approved card hides the learning action for the unsaved changes", async t => {
  const edited = { ...draft };
  const h = await mount(t, { card: { ...card, status: "approved", approval: { method: "machine" } }, draft: edited,
    getResult: { status: "auto_pass", current: true } });
  const study = h.section.querySelector("button.card-auto-review-study");
  assert.equal(study.hidden, false);
  global.document.querySelectorAll = () => [h.section];
  edited.dirty = true;
  Review.markDraftDirty();
  assert.equal(study.hidden, true);
  assert.match(h.section.textContent, /编辑内容未保存/);
});

test("dirty or outdated cards do not request review status or offer a paid action", async t => {
  const h = await mount(t, { draft: { ...draft, dirty: true } });
  assert.equal(h.requests.length, 0);
  assert.equal(h.action.disabled, true);
  assert.match(h.section.textContent, /先保存当前修改/);
});

test("stale and uncertain results are never presented as a pass", () => {
  assert.equal(Review.labelFor({ status: "auto_pass", current: false }), "机审结果已失效");
  assert.equal(Review.labelFor({ status: "unchecked", current: false, stale: true }), "机审结果已失效");
  assert.equal(Review.labelFor({ status: "needs_review", current: true }), "待处理");
  assert.equal(Review.labelFor({ status: "attention", current: true }), "发现需要修改的内容");
});

test("an obsolete machine approval still offers an explicit recheck", async t => {
  const h = await mount(t, { card: { ...card, status: "approved", approval: { method: "machine" } },
    getResult: { status: "unchecked", current: false, stale: true, reasons: [] } });
  assert.doesNotMatch(h.section.textContent, /机器审核通过，可开始学习/);
  assert.equal(h.action.disabled, false);
  assert.equal(h.action.hidden, false);
  assert.equal(h.section.querySelector("button.card-auto-review-study").hidden, true);
});
