"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { getProgress, filterCards, nextCard, draftKey } = require("../ankisense/static/practice-progress.js");

const cards = [{ id: "a", version: 2 }, { id: "b", version: 1 }, { id: "c", version: 1 }];
const attempt = (overrides = {}) => ({
  id: "attempt-a", card_id: "a", card_snapshot: { version: 2 },
  answer: "后进先出", status: "completed", diagnosis: { verdict: "correct" },
  created_at: "2026-09-25T04:00:00Z", ...overrides,
});

test("only the current card revision establishes completion", () => {
  for (const wrong of [attempt({ card_snapshot: { version: 1 } }), attempt({ card_snapshot: null }), attempt({ card_id: "other" })]) {
    assert.deepEqual(getProgress(cards, [wrong]).get("a"), { completed: false, latestAttempt: null, completedAttempt: null });
  }
  assert.equal(getProgress(cards, [attempt()]).get("a").completed, true);
});

test("all valid verdicts count as finished, including incorrect and uncertain", () => {
  for (const verdict of ["correct", "incomplete", "incorrect", "uncertain"]) {
    assert.equal(getProgress(cards, [attempt({ diagnosis: { verdict } })]).get("a").completed, true, verdict);
  }
});

test("blank answers, unfinished calls and malformed verdicts do not count", () => {
  for (const change of [{ answer: "  \n\t" }, { answer: "" }, { answer: null }, { status: "pending" }, { status: "failed" }, { status: "unanswered" }, { diagnosis: null }, { diagnosis: { verdict: "unknown" } }]) {
    assert.equal(getProgress(cards, [attempt(change)]).get("a").completed, false, JSON.stringify(change));
  }
});

test("later failure or blank does not erase a completed attempt", () => {
  const complete = attempt();
  for (const change of [{ status: "failed" }, { status: "unanswered", answer: "" }]) {
    const latest = attempt({ id: "later", created_at: "2026-09-25T05:00:00Z", ...change });
    const item = getProgress(cards, [latest, complete]).get("a");
    assert.equal(item.completed, true);
    assert.equal(item.latestAttempt, latest);
    assert.equal(item.completedAttempt, complete);
  }
});

test("latest records use timestamps; ties favor the later array entry", () => {
  const old = attempt();
  const latest = attempt({ id: "new", created_at: "2026-09-25T05:00:00Z" });
  const tied = attempt({ id: "tie", created_at: "2026-09-25T05:00:00Z" });
  assert.equal(getProgress(cards, [latest, old]).get("a").latestAttempt, latest);
  const item = getProgress(cards, [latest, old, tied]).get("a");
  assert.equal(item.latestAttempt, tied);
  assert.equal(item.completedAttempt, tied);
});

test("filters retain original card order", () => {
  const progress = getProgress(cards, [attempt()]);
  assert.deepEqual(filterCards(cards, progress, "all"), cards);
  assert.deepEqual(filterCards(cards, progress, "completed"), [cards[0]]);
  assert.deepEqual(filterCards(cards, progress, "uncompleted"), [cards[1], cards[2]]);
  assert.deepEqual(filterCards(cards, progress, "unknown"), cards);
});

test("next navigates the current category and stops at its end", () => {
  const progress = getProgress(cards, [attempt()]);
  assert.equal(nextCard(cards, progress, "all", "a"), cards[1]);
  assert.equal(nextCard(cards, progress, "all", "c"), null);
  assert.equal(nextCard(cards, progress, "uncompleted", "b"), cards[2]);
  assert.equal(nextCard(cards, progress, "uncompleted", "c"), null);
  assert.equal(nextCard(cards, progress, "completed", "a"), null);
});

test("newly completed card advances to the next remaining unfinished card", () => {
  const progress = getProgress(cards, [attempt()]);
  assert.equal(nextCard(cards, progress, "uncompleted", "a"), cards[1]);
  const lastFinished = getProgress(cards, [attempt({ card_id: "c", card_snapshot: { version: 1 } })]);
  assert.equal(nextCard(cards, lastFinished, "uncompleted", "c"), cards[0]);
});

test("empty or missing lists are safe, unknown selection picks first", () => {
  assert.equal(getProgress(null, null).size, 0);
  assert.deepEqual(filterCards(null, new Map(), "all"), []);
  assert.equal(nextCard(null, new Map(), "all", null), null);
  assert.equal(nextCard(cards, getProgress(cards, []), "completed", "a"), null);
  assert.equal(nextCard(cards, getProgress(cards, []), "all", "unknown"), cards[0]);
});

test("draft keys separate revisions and arbitrary card IDs", () => {
  assert.equal(draftKey(cards[0]), draftKey({ ...cards[0] }));
  assert.notEqual(draftKey(cards[0]), draftKey({ ...cards[0], version: 3 }));
  assert.notEqual(draftKey({ id: "a:1", version: 2 }), draftKey({ id: "a", version: "1:2" }));
});

test("ordinary script loads the same browser global without module tooling", () => {
  const context = vm.createContext({});
  vm.runInContext(fs.readFileSync(require.resolve("../ankisense/static/practice-progress.js"), "utf8"), context);
  assert.equal(typeof context.PracticeProgress.getProgress, "function");
  assert.equal(typeof context.PracticeProgress.nextCard, "function");
  assert.equal(context.PracticeProgress.getProgress(cards, [attempt()]).get("a").completed, true);
});
