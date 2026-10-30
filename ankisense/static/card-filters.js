(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.CardFilters = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const normalize = value => String(value || "").normalize("NFKC").toLowerCase().trim();
  function status(card) {
    if (card.status === "approved") return "ready";
    return ["queued", "running"].includes(card.review_state)
      || ["queued", "matching", "checking"].includes(card.auto_illustration?.state) ? "checking" : "pending";
  }
  function matches(card, { documentId = "all", query = "", images = "all" } = {}) {
    if (documentId && documentId !== "all" && card.document_id !== documentId) return false;
    const terms = normalize(query).split(/\s+/).filter(Boolean);
    const text = normalize(`${card.question || ""} ${card.concept || ""}`);
    if (!terms.every(term => text.includes(term))) return false;
    const illustrated = (Array.isArray(card.images) ? card.images : []).some(image => image?.media_id && ["question", "answer"].includes(image.side));
    return images === "with" ? illustrated : images === "without" ? !illustrated : true;
  }
  function filter(cards, options = {}) {
    return (Array.isArray(cards) ? cards : []).filter(card => matches(card, options)
      && (!options.status || options.status === "all" || status(card) === options.status));
  }
  return { status, matches, filter };
});
