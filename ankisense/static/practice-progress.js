(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.PracticeProgress = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const verdicts = new Set(["correct", "incomplete", "incorrect", "uncertain"]);
  const asArray = value => Array.isArray(value) ? value : [];
  const timestamp = attempt => {
    const value = Date.parse(attempt?.created_at);
    return Number.isNaN(value) ? -Infinity : value;
  };
  const isCompleted = attempt => attempt.status === "completed"
    && typeof attempt.answer === "string" && attempt.answer.trim().length > 0
    && verdicts.has(attempt.diagnosis?.verdict);

  function getProgress(cards, attempts) {
    const cardMap = new Map(asArray(cards).map(card => [card.id, card]));
    const progress = new Map([...cardMap.keys()].map(id => [id, {
      completed: false, latestAttempt: null, completedAttempt: null,
    }]));
    for (const attempt of asArray(attempts)) {
      const card = cardMap.get(attempt?.card_id);
      // A previous revision does not establish progress on the current question.
      if (!card || card.version == null || attempt.card_snapshot?.version !== card.version) continue;
      const item = progress.get(card.id);
      if (!item.latestAttempt || timestamp(attempt) >= timestamp(item.latestAttempt)) item.latestAttempt = attempt;
      if (isCompleted(attempt)) {
        item.completed = true;
        if (!item.completedAttempt || timestamp(attempt) >= timestamp(item.completedAttempt)) item.completedAttempt = attempt;
      }
    }
    return progress;
  }

  function filterCards(cards, progress, filter) {
    return asArray(cards).filter(card => {
      const completed = Boolean(progress?.get(card.id)?.completed);
      return filter === "completed" ? completed : filter === "uncompleted" ? !completed : true;
    });
  }

  function nextCard(cards, progress, filter, currentId) {
    const filtered = filterCards(cards, progress, filter);
    if (!filtered.length) return null;
    const filteredIndex = filtered.findIndex(card => card.id === currentId);
    if (filteredIndex !== -1) return filtered[filteredIndex + 1] || null;
    // Completing a card removes it from the unfinished list. Continue after its
    // original position first, then return to an earlier unfinished card if any.
    const allCards = asArray(cards);
    const currentIndex = allCards.findIndex(card => card.id === currentId);
    const remainingIds = new Set(filtered.map(card => card.id));
    return allCards.slice(currentIndex + 1).find(card => remainingIds.has(card.id)) || filtered[0];
  }

  function draftKey(card) {
    return JSON.stringify([card?.id ?? null, card?.version ?? null]);
  }

  return { getProgress, filterCards, nextCard, draftKey };
});
