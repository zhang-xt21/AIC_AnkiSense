"use strict";

// Workbench helpers operate on the local editor draft; practice reads saved cards.
function draftEvidence(draft) {
  try { const refs = JSON.parse(draft.evidence); return Array.isArray(refs) ? refs : []; }
  catch { return []; }
}
function touchCardDraft(card, draft, invalidate = true) {
  draft.dirty = true;
  ImageAutoReview.markDraftDirty();
  CardAutoReview.markDraftDirty();
  updateReviewIndicators();
  for (const illustration of document.querySelectorAll(".auto-illustration")) illustration.refreshIllustration?.();
  if (invalidate) FigureRecommendations.changed(card.id);
  updateWorkbenchPreview(card, draft);
  const status = document.getElementById("workbench-save-status");
  if (status) status.textContent = "有未保存的修改";
}
function updateWorkbenchPreview(card, draft) {
  if (app.view !== "review" || app.cardId !== card.id) return;
  const target = document.getElementById("card-live-preview");
  if (!target) return;
  const signature = workbenchPreviewSignature(draft);
  // Text entry must not download every attached picture again on each keystroke.
  if (target.dataset.signature === signature) {
    target.querySelector(".preview-question").textContent = draft.question || "在中间写下你的问题";
    target.querySelector(".preview-card-brand .pill").textContent = draft.concept || "知识概念";
    const answer = target.querySelector(".preview-answer");
    if (answer) answer.textContent = draft.answer || "参考答案将在这里显示";
    const source = target.querySelector(".preview-source-label");
    if (source) source.textContent = workbenchSourceLabel(card, draft);
  } else {
    target.replaceChildren(workbenchPreviewContent(card, draft));
    target.dataset.signature = signature;
  }
}
function workbenchPreviewSignature(draft) {
  return JSON.stringify([draft.previewSide || "question", draft.images]);
}
function workbenchSourceLabel(card, draft) {
  const pages = [...new Set(draftEvidence(draft).map(ref => app.data.blocks.find(block => block.block_id === ref.block_id)).filter(Boolean).map(sourceLabel))];
  return `${documentFor(card.document_id)?.title || "学习材料"}${pages.length ? ` · ${pages.join("、")}` : ""}`;
}
function workbenchPreviewContent(card, draft) {
  const back = draft.previewSide === "answer";
  const frontImages = draft.images.filter(image => image.side === "question");
  const backImages = draft.images.filter(image => image.side === "answer");
  const body = el("article", { class: "preview-study-card", "aria-label": back ? "卡片背面内容" : "卡片正面内容" },
    el("div", { class: "preview-card-brand" }, text("span", "AnkiSense"), badge(draft.concept || "知识概念", "green")),
    text("p", back ? "回到依据，补全理解" : "先用自己的话回答", "preview-eyebrow"),
    text("h3", draft.question || "在中间写下你的问题", "preview-question"),
    CardImages.gallery(frontImages, app.data.media, "正面配图"));
  if (back) {
    body.append(el("hr", { class: "section-divider" }), text("p", "参考答案", "preview-eyebrow"), text("p", draft.answer || "参考答案将在这里显示", "preview-answer"), CardImages.gallery(backImages, app.data.media, "背面配图"));
    body.append(text("p", workbenchSourceLabel(card, draft), "preview-source-label"));
  } else body.append(text("div", "用自己的话回答…", "preview-answer-placeholder"));
  return el("div", {}, body, text("p", `正面 ${frontImages.length} 张图 · 背面 ${backImages.length} 张图`, "helper preview-image-count"), text("p", "这里预览正在编辑的内容；练习与导出使用已保存、审核通过的版本。", "helper"));
}
function renderWorkbenchPreview(card, draft) {
  const tabs = el("div", { class: "preview-face-tabs", "aria-label": "预览卡片正反面" });
  for (const [side, label] of [["question", "正面"], ["answer", "背面"]]) {
    const tab = button(label, () => {
      draft.previewSide = side;
      for (const item of tabs.children) item.setAttribute("aria-pressed", String(item === tab));
      updateWorkbenchPreview(card, draft);
    }, "btn secondary small");
    tab.setAttribute("aria-pressed", String((draft.previewSide || "question") === side));
    tabs.append(tab);
  }
  const result = panel("卡片预览", "文字与配图随编辑实时更新", el("div", {}, tabs, AutoIllustration.create({ card, draft, compact: true }), el("div", { id: "card-live-preview", "data-signature": workbenchPreviewSignature(draft) }, workbenchPreviewContent(card, draft))));
  result.classList.add("workbench-preview");
  return result;
}
function renderWorkbenchSources(card, draft) {
  const doc = documentFor(card.document_id);
  const refs = draftEvidence(draft);
  const relevantIds = new Set([...refs, ...(card.evidence || [])].map(ref => ref.block_id));
  const currentIds = new Set(blocksFor(card.document_id).map(block => block.block_id));
  const blocks = app.data.blocks.filter(block => block.document_id === card.document_id && (currentIds.has(block.block_id) || relevantIds.has(block.block_id)));
  const sourcePages = sourcePagesFor(card.document_id);
  const pages = [...new Set([...sourcePages.map(page => page.page), ...blocks.map(block => block.page)])].sort((a, b) => a - b);
  const firstCited = blocks.find(block => refs.some(ref => ref.block_id === block.block_id))?.page;
  const pageNumber = pages.includes(draft.sourcePage) ? draft.sourcePage : firstCited || pages[0];
  draft.sourcePage = pageNumber;
  const sourcePage = sourcePages.find(page => page.page === pageNumber);
  const pageIndex = pages.indexOf(pageNumber);
  const select = el("select", { "aria-label": "查看讲义页", disabled: !pages.length }, ...pages.map(page => el("option", { value: page, selected: page === pageNumber }, `${doc?.file_url ? "第" : "片段"} ${page}${doc?.file_url ? " 页" : ""}${blocks.some(block => block.page === page && relevantIds.has(block.block_id)) ? " · 有引用" : ""}`)));
  const changePage = number => { draft.sourcePage = number; render(); };
  select.addEventListener("change", () => changePage(Number(select.value)));
  const content = el("div", {}, text("p", doc?.title || "来源讲义", "workbench-document-name"), el("div", { class: "workbench-source-nav" }, button("←", () => changePage(pages[pageIndex - 1]), "btn secondary small", pageIndex <= 0), select, button("→", () => changePage(pages[pageIndex + 1]), "btn secondary small", pageIndex < 0 || pageIndex >= pages.length - 1)));
  if (sourcePage?.image_url) {
    const open = button("", () => previewSourceImage(doc, sourcePage, open), "workbench-page-image");
    open.setAttribute("aria-label", `放大讲义第 ${pageNumber} 页`);
    const image = el("img", { src: sourcePage.image_url, alt: `讲义第 ${pageNumber} 页原图`, decoding: "async" });
    image.addEventListener("error", () => { open.replaceChildren(text("span", "原页图片无法加载，请打开原始 PDF 查看。", "helper")); open.disabled = true; });
    open.append(image, text("span", "点击放大原页 ↗", "helper"));
    content.append(open);
  } else content.append(text("p", doc?.is_demo ? "自编演示材料 · 暂无 PDF 原页，可上传截图体验配图。" : "文本材料无原页图片，可从中间的配图区上传截图。", "helper"));
  if (sourcePage) content.append(badge(sourcePageStatus(sourcePage), sourcePageConfirmed(sourcePage) ? "green" : "amber"));
  const excerpts = el("div", { class: "workbench-excerpts" });
  for (const block of blocks.filter(block => block.page === pageNumber).sort((a, b) => Number(relevantIds.has(b.block_id)) - Number(relevantIds.has(a.block_id)))) {
    const cited = refs.filter(ref => ref.block_id === block.block_id);
    const current = currentIds.has(block.block_id);
    const add = button(cited.length ? "已在引用中" : "加入卡片引用", () => {
      const next = draftEvidence(draft);
      if (next.length >= 8) return toast("每张卡片最多保留 8 条引用。", true);
      next.push({ block_id: block.block_id, quote: block.text.slice(0, 4000) });
      draft.evidence = JSON.stringify(next);
      touchCardDraft(card, draft);
      render();
    }, "btn quiet small", Boolean(app.cardEditBusy) || !current || !blockCanGenerate(block) || Boolean(cited.length));
    excerpts.append(el("article", { class: `source-block${cited.length ? " cited-source" : ""}` }, el("div", { class: "source-top" }, badge(current ? cited.length ? "已引用" : "原文" : "历史引用", current ? cited.length ? "green" : "" : "amber")), text("p", block.text, "source-text"), ...cited.filter(ref => ref.quote.trim() !== block.text.trim()).map(ref => text("blockquote", ref.quote, "quote")), add));
  }
  if (!excerpts.children.length) excerpts.append(note("此页还没有可用文字，可以检查原文后重新识别或补入文字。"));
  content.append(excerpts);
  const links = el("div", { class: "workbench-source-links" });
  if (doc?.file_url) links.append(el("a", { href: doc.file_url, target: "_blank", rel: "noopener noreferrer", class: "btn secondary small" }, "打开原始 PDF ↗"));
  if (sourcePages.length) links.append(button(`检查第 ${pageNumber} 页原文`, () => { app.documentId = card.document_id; app.sourcePage[card.document_id] = pageNumber; setView("materials"); focusProofreader(); }, "btn secondary small"));
  content.append(links);
  if (sourcePage && !sourcePageConfirmed(sourcePage)) content.append(text("p", "本页使用当前提取文字，可以直接制卡和自动检查，无需逐页确认。发现错误时可在原文检查中修正。", "helper"));
  const result = panel("讲义与依据", "保留原图、原文与引用位置", content);
  result.classList.add("workbench-source");
  return result;
}
function renderRubricFields(card, draft, busy) {
  const points = draft.rubric.map(point => ({ ...point }));
  const save = () => { draft.rubric = points.map(point => ({ ...point })); touchCardDraft(card, draft); };
  const content = el("div", { class: "workbench-rubric" }, text("h3", "评分要点"), text("p", "一条只检查一个要点。AI 会据此指出回答中的遗漏与矛盾。", "helper"));
  points.forEach((point, index) => {
    const input = el("textarea", { rows: "2", maxlength: "1000", required: true, value: point.text, disabled: busy, "aria-label": `评分要点 ${index + 1}` });
    input.addEventListener("input", () => { point.text = input.value; save(); });
    content.append(el("div", { class: "workbench-rubric-row" }, text("span", String(index + 1).padStart(2, "0"), "rubric-number"), input, button("移除", () => { points.splice(index, 1); save(); render(); }, "btn quiet small", busy || points.length === 1)));
  });
  content.append(button("＋ 添加评分要点", () => { points.push({ point_id: `point_${crypto.randomUUID().slice(0, 8)}`, text: "" }); save(); render(); }, "btn secondary small", busy || points.length >= 12));
  return content;
}
function renderEvidenceFields(card, draft, busy) {
  const refs = draftEvidence(draft);
  const blocks = app.data.blocks.filter(block => block.document_id === card.document_id);
  const currentIds = new Set(blocksFor(card.document_id).map(block => block.block_id));
  const save = () => { draft.evidence = JSON.stringify(refs); touchCardDraft(card, draft); };
  const content = el("div", { class: "workbench-evidence" }, text("h3", "卡片依据"), text("p", "保留支持答案的原文。引用需与讲义一致，可在左侧加入新的原文片段。", "helper"));
  refs.forEach((ref, index) => {
    const block = blocks.find(item => item.block_id === ref.block_id);
    const quote = el("textarea", { rows: "3", maxlength: "4000", required: true, disabled: busy, value: ref.quote, "aria-label": `原文引用 ${index + 1}` });
    quote.addEventListener("input", () => { ref.quote = quote.value; save(); });
    content.append(el("div", { class: "workbench-evidence-row" }, el("div", { class: "source-top" }, badge(`${block ? sourceLabel(block) : "来源缺失"}${currentIds.has(ref.block_id) ? "" : " · 历史引用"}`, currentIds.has(ref.block_id) ? "green" : "amber"), button("定位原文", () => { if (block) { draft.sourcePage = block.page; app.reviewPane = "source"; render(); document.querySelector(".workbench-source")?.scrollIntoView({ block: "start", behavior: "instant" }); } }, "btn quiet small", !block), button("移除引用", () => { refs.splice(index, 1); save(); render(); }, "btn quiet small", busy || refs.length === 1)), quote));
  });
  if (!refs.length) content.append(note("请从左侧讲义加入至少一条引用。", "warning"));
  return content;
}
