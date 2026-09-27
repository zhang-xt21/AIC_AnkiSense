"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const CardImages = require("../ankisense/static/card-images.js");

// Load the real renderer without the application's initial /api/state request.
const script = fs.readFileSync(require.resolve("../ankisense/static/app.js"), "utf8");
assert.match(script, /\binitialize\(\);\s*$/);
const source = script.replace(/\binitialize\(\);\s*$/, "");

function frozen(value) {
  if (value && typeof value === "object") {
    Object.values(value).forEach(frozen);
    Object.freeze(value);
  }
  return value;
}

function attempt(diagnosis = {}, changes = {}) {
  return {
    id: "history-attempt", card_id: "railway", status: "completed",
    answer: "你好", created_at: "2026-09-26T15:50:00Z", prompt_version: "v1",
    card_snapshot: {
      id: "railway", version: 1, status: "approved", images: [],
      question: "输入 A、B、C，讲义展示了哪两种输出序列？",
      answer: "A、B、C 和 C、B、A。",
      rubric: [
        { point_id: "out_abc", text: "列出输出 A、B、C。" },
        { point_id: "out_cba", text: "列出输出 C、B、A。" },
      ],
    },
    source_snapshot: [{ block_id: "source-1", document_id: "pdf-railway", page: 13, text: "输入 A、B、C，可以输出 A、B、C 或 C、B、A。" }],
    diagnosis: {
      verdict: "incomplete", missing_point_ids: ["out_abc", "out_cba"],
      contradicted_point_ids: [], answer_spans: [], source_block_ids: ["source-1"],
      reason: "旧分析只发现要点缺失，未记录任何相关知识依据。", ...diagnosis,
    },
    ...changes,
  };
}

function harness(record) {
  const requests = [];
  class FakeNode {
    constructor(tagName = "#text", value = "") {
      this.tagName = tagName;
      this.children = [];
      this.attributes = {};
      this.className = "";
      this.valueText = value;
      this.classList = {
        add: (...classes) => { this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...classes])].join(" "); },
      };
    }
    get textContent() { return this.valueText + this.children.map(child => child.textContent).join(""); }
    set textContent(value) { this.children = []; this.valueText = String(value); }
    append(...children) { this.children.push(...children); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] ?? null; }
    addEventListener() {}
    matches(selector) {
      if (selector.startsWith(".")) return this.className.split(/\s+/).includes(selector.slice(1));
      if (selector.startsWith("[")) return Object.hasOwn(this.attributes, selector.slice(1, -1));
      return this.tagName === selector;
    }
    querySelector(selector) {
      const [head, ...tail] = selector.split(/\s+/);
      for (const child of this.children) {
        if (child.matches(head)) {
          const found = tail.length ? child.querySelector(tail.join(" ")) : child;
          if (found) return found;
        }
        const found = child.querySelector(selector);
        if (found) return found;
      }
      return null;
    }
  }
  const document = {
    createElement: tag => new FakeNode(tag),
    createTextNode: value => new FakeNode("#text", value),
    querySelectorAll: () => [],
  };
  const context = vm.createContext({
    Node: FakeNode, document, CardImages,
    window: { addEventListener() {} },
    sessionStorage: { getItem: () => null },
    fetch: (...args) => { requests.push(args); throw new Error("Feedback rendering must not access the network"); },
  });
  vm.runInContext(source, context, { filename: "app.js" });
  const { app, renderAttempt, partialFeedbackNeedsReview } = vm.runInContext("({ app, renderAttempt, partialFeedbackNeedsReview })", context);
  // Deliberately disagree with the historical snapshot: it must remain authoritative.
  app.data = frozen({
    documents: [{ id: "pdf-railway", title: "栈与队列" }], media: [], blocks: [],
    cards: [{ id: "railway", status: "draft", version: 2, rubric: [{ point_id: "other", text: "当前版本的新要点" }], answer: "当前版本的新答案" }],
  });
  const before = JSON.stringify(record);
  frozen(record);
  const page = renderAttempt(record);
  assert.equal(JSON.stringify(record), before, "rendering preserves the diagnosis and every submitted snapshot");
  assert.deepEqual(requests, [], "viewing feedback never calls AI or another endpoint");
  const focusTitle = page.querySelector("[data-feedback-title]") || page.querySelector(".panel-header h2");
  assert.equal(focusTitle.id, "feedback-focus", "feedback remains keyboard-focusable");
  return { page, requests, needsReview: partialFeedbackNeedsReview(record) };
}

test("legacy incomplete feedback retains its verdict and explanation but warns instead of praising unrelated answers", () => {
  const record = attempt();
  const { page, needsReview } = harness(record);
  assert.equal(needsReview, true);
  assert.match(page.textContent, /历史分析 · 待复核/);
  assert.match(page.textContent, /原判：还不完整/);
  assert.match(page.textContent, /当时的判断说明（待复核）/);
  assert.ok(page.textContent.includes(record.diagnosis.reason));
  assert.ok(page.textContent.includes(record.answer));
  assert.equal(page.querySelector("[data-verdict]").getAttribute("data-verdict"), "incomplete");
  assert.doesNotMatch(page.textContent, /方向对了|回答已涉及的要点/);
  assert.match(page.textContent, /列出输出 A、B、C。/);
  assert.doesNotMatch(page.textContent, /当前版本的新要点|当前版本的新答案/);
});

test("partial answers display supported rubric points even when that compound point still needs completion", () => {
  const record = attempt({
    addressed_point_ids: ["out_abc"], answer_spans: ["A、B、C"],
    reason: "回答已有相关内容，还需要补充。",
  }, { answer: "我记得 A、B、C。", prompt_version: "v2-relevance-evidence" });
  const { page, needsReview } = harness(record);
  assert.equal(needsReview, false, "addressed and missing may overlap for a partially answered compound point");
  assert.match(page.textContent, /本次理解情况/);
  assert.match(page.textContent, /回答已涉及的要点/);
  assert.match(page.textContent, /涉及部分内容不代表已完整满足该要点/);
  assert.match(page.textContent, /需要补充的要点/);
  assert.match(page.textContent, /为什么这样判断/);
  assert.equal(page.querySelector("[data-feedback-title]").textContent, "还不完整");
  assert.doesNotMatch(page.textContent, /待复核|方向对了|当前版本的新要点/);
});

for (const [name, diagnosis] of [
  ["empty addressed points", { addressed_point_ids: [] }],
  ["unknown addressed point", { addressed_point_ids: ["made-up"] }],
  ["invalid addressed value", { addressed_point_ids: "out_abc" }],
  ["missing answer evidence", { answer_spans: [] }],
  ["blank answer evidence", { answer_spans: ["  "] }],
  ["fabricated answer evidence", { answer_spans: ["不是原回答的内容"] }],
  ["non-string answer evidence", { answer_spans: [42] }],
]) {
  test(`incomplete feedback with ${name} is flagged without rewriting stored results`, () => {
    const record = attempt({ addressed_point_ids: ["out_abc"], answer_spans: ["你好"], ...diagnosis });
    const { page, needsReview } = harness(record);
    assert.equal(needsReview, true);
    assert.match(page.textContent, /历史分析 · 待复核/);
    assert.ok(page.textContent.includes(record.diagnosis.reason));
    assert.doesNotMatch(page.textContent, /回答已涉及的要点|方向对了/);
  });
}

for (const [verdict, title] of [["correct", "回答正确"], ["uncertain", "暂时无法判断"], ["incorrect", "需要修正"]]) {
  test(`${verdict} feedback is not treated as an unsupported partial diagnosis`, () => {
    const record = attempt({ verdict, missing_point_ids: [], answer_spans: verdict === "uncertain" ? [] : ["你好"] });
    const { page, needsReview } = harness(record);
    assert.equal(needsReview, false);
    assert.equal(page.querySelector("[data-feedback-title]").textContent, title);
    assert.doesNotMatch(page.textContent, /历史分析 · 待复核|原判：|方向对了/);
    assert.ok(page.textContent.includes(record.diagnosis.reason));
  });
}

test("an unanswered attempt stays unanswered and does not expose the stored reference answer", () => {
  const { page } = harness(attempt({}, { status: "unanswered", answer: "", diagnosis: null }));
  assert.match(page.textContent, /你还没有填写回答/);
  assert.doesNotMatch(page.textContent, /历史分析 · 待复核|查看已审核的参考答案/);
});
