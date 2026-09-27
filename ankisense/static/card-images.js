(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.CardImages = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const verdicts = new Set(["correct", "incomplete", "incorrect", "uncertain"]);
  const asArray = value => Array.isArray(value) ? value : [];
  function refs(card) {
    return asArray(card?.images).filter(item => item && typeof item.media_id === "string" && ["question", "answer"].includes(item.side))
      .map(item => ({ media_id: item.media_id, side: item.side, caption: typeof item.caption === "string" ? item.caption : "" }));
  }
  function questionImages(card, attempt = null) {
    // Historical images must come from the submitted snapshot, never today's card.
    return refs(attempt ? attempt.card_snapshot : card).filter(item => item.side === "question");
  }
  function answerImages(attempt) {
    if (attempt?.status !== "completed" || typeof attempt.answer !== "string" || !attempt.answer.trim() || !verdicts.has(attempt.diagnosis?.verdict)) return [];
    return refs(attempt.card_snapshot).filter(item => item.side === "answer");
  }
  function mediaFor(reference, media) {
    const record = asArray(media).find(item => item?.id === reference?.media_id);
    if (!record || !/^[a-zA-Z0-9_-]+$/.test(record.id)) return null;
    // Only immutable media served by this application can be rendered or opened.
    return record.url === `/api/card-media/${encodeURIComponent(record.id)}/file` ? record : null;
  }
  function node(tag, className, content) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (content !== undefined) result.textContent = content;
    return result;
  }
  let previewNumber = 0;
  function preview(record, label, opener) {
    const dialog = node("dialog", "card-image-dialog");
    const title = node("h2", "", label);
    title.id = `card-image-preview-${++previewNumber}`;
    dialog.setAttribute("aria-labelledby", title.id);
    const image = node("img");
    image.src = record.url;
    image.alt = label;
    const viewport = node("div", "card-image-dialog-viewport");
    viewport.tabIndex = 0;
    viewport.setAttribute("aria-label", "配图大图预览，可滚动查看；按 Escape 关闭");
    viewport.append(image);
    const zoom = node("button", "btn secondary small", "放大图片");
    zoom.type = "button";
    zoom.setAttribute("aria-pressed", "false");
    zoom.addEventListener("click", () => {
      const enlarged = viewport.classList.toggle("is-zoomed");
      zoom.textContent = enlarged ? "适合窗口" : "放大图片";
      zoom.setAttribute("aria-pressed", String(enlarged));
    });
    const close = node("button", "btn small", "关闭预览");
    close.type = "button";
    close.addEventListener("click", () => dialog.close());
    const actions = node("div", "two-buttons");
    actions.append(zoom, close);
    const header = node("div", "card-image-dialog-header");
    header.append(title, actions);
    image.addEventListener("error", () => { viewport.replaceChildren(node("p", "card-image-placeholder", "图片暂时无法读取，请检查本地媒体文件是否完整。")); zoom.disabled = true; }, { once: true });
    dialog.append(header, viewport);
    dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
    dialog.addEventListener("close", () => { dialog.remove(); if (opener?.isConnected) opener.focus(); });
    document.body.append(dialog);
    dialog.showModal();
    close.focus();
  }
  function thumbnail(reference, media, label = "卡片配图") {
    const wrapper = node("div", "card-image-preview");
    const record = mediaFor(reference, media);
    if (!record) {
      wrapper.append(node("p", "card-image-placeholder", "配图文件暂时无法读取，已保留此图片引用。"));
      return wrapper;
    }
    const open = node("button", "card-image-zoom");
    open.type = "button";
    open.setAttribute("aria-label", `查看大图：${label}`);
    const image = node("img");
    image.src = record.url;
    image.alt = reference.caption || label;
    image.loading = "lazy";
    image.addEventListener("error", () => wrapper.replaceChildren(node("p", "card-image-placeholder", "图片暂时无法读取，请检查本地媒体文件是否完整。")), { once: true });
    open.append(image);
    open.addEventListener("click", () => preview(record, reference.caption || label, open));
    wrapper.append(open);
    return wrapper;
  }
  function gallery(references, media, label = "卡片配图") {
    if (!asArray(references).length) return null;
    const result = node("div", "card-image-gallery");
    result.setAttribute("aria-label", label);
    references.forEach((reference, index) => {
      const figure = node("figure", "card-image-figure");
      figure.append(thumbnail(reference, media, `${label} ${index + 1}`));
      if (reference.caption) figure.append(node("figcaption", "card-image-caption", reference.caption));
      result.append(figure);
    });
    return result;
  }
  return { refs, questionImages, answerImages, mediaFor, thumbnail, gallery };
});
