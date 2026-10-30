"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const CardImages = require("../ankisense/static/card-images.js");

// Exercise the real app functions without starting its initial state request.
const script = fs.readFileSync(require.resolve("../ankisense/static/app.js"), "utf8");
assert.match(script, /\binitialize\(\);\s*$/);
const source = script.replace(/\binitialize\(\);\s*$/, "");
const approved = (changes = {}) => ({ id: "approved", status: "approved", images: [], ...changes });
const picture = { media_id: "diagram", side: "question", caption: "示意图" };

function harness(cards, fetchResponse) {
  const requests = [], downloads = [], created = [], revoked = [], timers = new Map();
  let timerId = 0;
  let fetchImpl = fetchResponse;

  class FakeNode {
    constructor(tagName = "#text", value = "") {
      this.tagName = tagName;
      this.children = [];
      this.attributes = {};
      this.handlers = new Map();
      this.className = "";
      this.disabled = false;
      this.parentNode = null;
      this.valueText = value;
    }
    get textContent() { return this.valueText + this.children.map(child => child.textContent).join(""); }
    set textContent(value) {
      for (const child of this.children) child.parentNode = null;
      this.children = [];
      this.valueText = String(value);
    }
    append(...children) {
      for (const child of children) {
        child.remove();
        child.parentNode = this;
        this.children.push(child);
      }
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] ?? null; }
    addEventListener(event, handler) {
      const handlers = this.handlers.get(event) || [];
      handlers.push(handler);
      this.handlers.set(event, handlers);
    }
    remove() {
      if (this.parentNode) {
        this.parentNode.children = this.parentNode.children.filter(child => child !== this);
        this.parentNode = null;
      }
    }
    async click() {
      if (this.disabled) return;
      if (this.tagName === "a") {
        downloads.push({ href: this.getAttribute("href"), filename: this.getAttribute("download"), attached: this.parentNode === document.body, hidden: this.hidden });
      }
      for (const handler of this.handlers.get("click") || []) await handler({ currentTarget: this });
    }
  }

  const toast = new FakeNode("div");
  const document = {
    body: new FakeNode("body"),
    createElement: tag => new FakeNode(tag),
    createTextNode: value => new FakeNode("#text", value),
    querySelectorAll: () => [],
    querySelector: selector => selector === "#toast" ? toast : null,
  };
  const context = vm.createContext({
    Node: FakeNode, document, CardImages,
    window: { addEventListener() {} },
    sessionStorage: { getItem: () => null },
    fetch: async (...args) => {
      requests.push(args);
      if (!fetchImpl) throw new Error("Unexpected fetch");
      return fetchImpl(...args);
    },
    URL: {
      createObjectURL(blob) {
        const url = `blob:export-${created.length + 1}`;
        created.push({ blob, url });
        return url;
      },
      revokeObjectURL: url => revoked.push(url),
    },
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
    clearTimeout: id => timers.delete(id),
  });
  vm.runInContext(source, context, { filename: "app.js" });
  const { app, renderExport, downloadExport } = vm.runInContext("({ app, renderExport, downloadExport })", context);
  app.data = { cards };
  const page = renderExport();
  function find(predicate, root = page) {
    return [root, ...root.children.flatMap(child => find(() => true, child))].filter(predicate);
  }
  return {
    page, document, downloadExport, requests, downloads, created, revoked, timers, toast,
    buttons: () => find(node => node.tagName === "button"),
    error: () => find(node => node.getAttribute("role") === "alert")[0],
    find,
    setFetch: fn => { fetchImpl = fn; },
    runTimers(delay) {
      for (const [id, timer] of timers) if (timer.delay === delay) { timers.delete(id); timer.callback(); }
    },
  };
}

test("APKG is the primary action and downloads only the approved-card count", async () => {
  const blob = new Blob(["package"], { type: "application/octet-stream" });
  const h = harness([approved(), approved({ id: "second" }), { status: "draft" }, { status: "rejected" }], async () => ({ ok: true, blob: async () => blob }));
  const button = h.buttons()[0];
  assert.match(button.textContent, /2 张卡片.*APKG/);
  assert.doesNotMatch(button.className, /secondary/);
  assert.equal(button.disabled, false);
  assert.equal(h.buttons().some(node => /ZIP/.test(node.textContent)), false);

  await button.click();
  assert.deepEqual(h.requests, [["/api/export/apkg"]]);
  assert.deepEqual(h.downloads, [{ href: "blob:export-1", filename: "ankisense-approved.apkg", attached: true, hidden: true }]);
  assert.equal(h.created[0].blob, blob);
  assert.equal(h.document.body.children.length, 0, "temporary download anchor is removed");
  assert.deepEqual(h.revoked, [], "URL remains alive while the browser starts its download");
  assert.equal([...h.timers.values()].filter(timer => timer.delay === 30000).length, 1);
  h.runTimers(30000);
  assert.deepEqual(h.revoked, ["blob:export-1"]);
  assert.equal(button.disabled, false);
  assert.match(button.textContent, /2 张卡片.*APKG/);
  assert.match(h.toast.textContent, /已发起下载/);
});

test("without approved cards every export action is disabled, including TSV", async () => {
  for (const cards of [[], [{ status: "draft", images: [picture] }, { status: "rejected", images: [picture] }]]) {
    const h = harness(cards);
    const buttons = h.buttons().filter(node => node.textContent !== "回到卡片审核");
    assert.equal(buttons.length, 2);
    assert.equal(buttons[0].textContent, "暂无已批准卡片");
    for (const button of buttons) { assert.equal(button.disabled, true); await button.click(); }
    assert.deepEqual(h.requests, []);
    assert.deepEqual(h.downloads, []);
  }
});

test("approved images keep APKG primary and expose secondary TSV and media ZIP downloads", async () => {
  const h = harness([approved({ images: [picture] }), { status: "draft", images: [picture] }], async () => ({ ok: true, blob: async () => new Blob(["export"]) }));
  const buttons = h.buttons();
  assert.match(buttons[0].textContent, /1 张卡片.*APKG/);
  assert.doesNotMatch(buttons[0].className, /secondary/);
  assert.match(h.page.textContent, /含 1 张图文卡片/);
  const formats = h.find(node => node.tagName === "details" && node.className.includes("export-alternatives"))[0];
  assert.equal(formats.getAttribute("open"), null, "alternative instructions start collapsed");
  const tsv = buttons.find(node => node.textContent === "下载纯文本 TSV");
  const zip = buttons.find(node => node.textContent === "下载图文 ZIP");
  for (const button of [tsv, zip]) { assert.ok(button); assert.match(button.className, /secondary/); }
  await buttons[0].click();
  await tsv.click();
  await zip.click();
  assert.deepEqual(h.requests.map(args => args[0]), ["/api/export/apkg", "/api/export", "/api/export/media"]);
  assert.deepEqual(h.downloads.map(item => item.filename), ["ankisense-approved.apkg", "ankisense-approved.tsv", "ankisense-approved-media.zip"]);
  assert.equal(h.document.body.children.length, 0);
  h.runTimers(30000);
  assert.deepEqual(h.revoked, h.created.map(item => item.url));
});

test("HTTP errors display server detail, prevent downloads, and restore a retryable button", async () => {
  let resolveResponse;
  const h = harness([approved()], () => new Promise(resolve => { resolveResponse = resolve; }));
  const button = h.buttons()[0];
  const originalLabel = button.textContent;
  const pending = button.click();
  assert.equal(button.disabled, true);
  assert.equal(button.textContent, "正在打包…");
  await button.click();
  assert.equal(h.requests.length, 1, "a second click cannot start another pending export");
  resolveResponse({ ok: false, json: async () => ({ detail: "配图文件不存在，请重新上传。" }), blob: async () => assert.fail("failed response must not be downloaded") });
  await pending;
  assert.equal(h.error().textContent, "配图文件不存在，请重新上传。");
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, originalLabel);
  assert.deepEqual(h.downloads, []);
  assert.deepEqual(h.created, []);
  assert.equal(h.toast.textContent, "");

  h.setFetch(async () => ({ ok: true, blob: async () => new Blob(["retry"]) }));
  await button.click();
  assert.equal(h.error().textContent, "", "retry clears the old error");
  assert.equal(h.downloads.length, 1);
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, originalLabel);
});

test("non-JSON and structured HTTP errors produce a readable fallback without downloading", async () => {
  for (const json of [async () => { throw new SyntaxError("HTML error page"); }, async () => ({ detail: [{ msg: "invalid" }] })]) {
    const h = harness([approved()], async () => ({ ok: false, json }));
    const button = h.buttons()[0];
    await button.click();
    assert.match(h.error().textContent, /导出未完成/);
    assert.doesNotMatch(h.error().textContent, /\[object Object\]/);
    assert.equal(button.disabled, false);
    assert.match(button.textContent, /APKG/);
    assert.deepEqual(h.created, []);
    assert.deepEqual(h.downloads, []);
  }
});

test("network and blob failures restore the action and never produce a partial file", async () => {
  for (const fetchResponse of [
    async () => { throw new Error("网络连接已断开"); },
    async () => ({ ok: true, blob: async () => { throw new Error("下载内容读取失败"); } }),
  ]) {
    const h = harness([approved()], fetchResponse);
    const button = h.buttons()[0];
    await button.click();
    assert.match(h.error().textContent, /网络连接已断开|下载内容读取失败/);
    assert.equal(button.disabled, false);
    assert.match(button.textContent, /APKG/);
    assert.deepEqual(h.created, []);
    assert.deepEqual(h.downloads, []);
  }
});

test("downloadExport rejects HTTP failure to its caller without allocating an object URL", async () => {
  const h = harness([], async () => ({ ok: false, json: async () => ({ detail: "没有可导出的已批准卡片。" }) }));
  await assert.rejects(h.downloadExport("/api/export/apkg", "ankisense-approved.apkg"), /没有可导出的已批准卡片/);
  assert.deepEqual(h.requests, [["/api/export/apkg"]]);
  assert.deepEqual(h.created, []);
  assert.deepEqual(h.downloads, []);
  assert.equal(h.timers.size, 0);
});
