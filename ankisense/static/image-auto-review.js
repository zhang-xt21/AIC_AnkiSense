(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.ImageAutoReview = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function savedRecommendation(card, draft, reference, media) {
    if (!card || !draft || draft.dirty || draft.version !== card.version) return false;
    const saved = (card.images || []).find(item => item.media_id === reference?.media_id);
    if (!saved || saved.side !== reference.side || saved.caption !== reference.caption) return false;
    return media?.source?.origin === "figure_recommendation";
  }

  function eligibleImages(card, draft, media) {
    return (draft?.images || []).filter(reference =>
      savedRecommendation(card, draft, reference, (media || []).find(item => item.id === reference.media_id)));
  }

  function labelFor(result) {
    if (result?.stale || (result?.current === false && result?.review_id)) return "检查结果已失效";
    return ({ auto_pass: "配图机审通过", attention: "建议重裁或更换", needs_review: "配图待处理", unchecked: "尚未检查" })[result?.status] || "尚未检查";
  }

  function endpoint(card, reference) {
    return `/api/cards/${encodeURIComponent(card.id)}/images/${encodeURIComponent(reference.media_id)}/auto-check`;
  }

  function markDraftDirty() {
    if (typeof document === "undefined") return;
    for (const panel of document.querySelectorAll(".image-auto-review")) {
      const status = panel.querySelector(".image-auto-review-status");
      if (status) {
        status.textContent = "编辑内容未保存，审核结果待更新";
        status.className = "image-auto-review-status";
      }
      const action = panel.querySelector("button");
      if (action) action.disabled = true;
    }
  }

  function node(tag, cls, content) {
    const item = document.createElement(tag);
    if (cls) item.className = cls;
    if (content !== undefined) item.textContent = content;
    return item;
  }

  function create({ card, draft, reference, media, vision, api, busy = false }) {
    const section = node("section", "image-auto-review");
    section.setAttribute("aria-label", "配图自动审核");
    if (media?.source?.origin !== "figure_recommendation") {
      section.append(node("p", "helper", "手动添加的配图需要对照原图核对。"));
      return section;
    }
    if (!savedRecommendation(card, draft, reference, media)) {
      section.append(node("p", "helper", "先保存当前文字和配图，再检查最终版本。"));
      return section;
    }

    const status = node("strong", "image-auto-review-status", "正在读取配图审核状态…");
    status.setAttribute("role", "status");
    const detail = node("div", "image-auto-review-detail");
    const action = node("button", "btn secondary small", "自动检查配图");
    action.type = "button";
    section.append(status, detail, action);
    let checking = false;
    function show(result, error) {
      if (!savedRecommendation(card, draft, reference, media)) {
        status.textContent = "编辑内容未保存，审核结果待更新";
        status.className = "image-auto-review-status";
        detail.replaceChildren();
        action.disabled = true;
        return;
      }
      const automatic = ["queued", "running"].includes(card.review_state);
      status.textContent = checking ? "正在检查配图…" : error ? "检查暂不可用" : automatic && result?.status !== "auto_pass" ? "随卡片自动检查中…" : labelFor(result);
      status.className = `image-auto-review-status${result?.status === "auto_pass" && result.current !== false ? " passed" : ""}`;
      detail.replaceChildren();
      if (error) detail.append(node("p", "inline-error", error));
      else for (const reason of (result?.reasons || []).slice(0, 4)) detail.append(node("p", "helper", reason));
      if (!vision?.available) detail.append(node("p", "helper", vision?.message || "请配置支持图片输入的模型。"));
      if (result?.status === "auto_pass") detail.append(node("p", "helper", "仅代表这张配图通过机器筛查；题目与答案仍按卡片审核状态处理。"));
      const localBlock = result?.current === false && result?.status === "needs_review";
      const finished = result?.current === true && !result?.retryable;
      action.textContent = checking ? "正在检查…" : localBlock ? "先处理来源疑点"
        : finished ? "已检查当前版本" : result?.retryable ? "重试自动检查" : "自动检查配图";
      action.disabled = checking || automatic || busy || !vision?.available || localBlock || finished;
    }
    section.refreshReview = () => {
      if (checking || !savedRecommendation(card, draft, reference, media)) return;
      return api(endpoint(card, reference)).then(result => { if (section.isConnected && !checking) show(result); })
        .catch(error => { if (section.isConnected && !checking) show(null, error.message); });
    };
    section.refreshReview();
    action.addEventListener("click", async () => {
      if (checking || busy || !vision?.available || !savedRecommendation(card, draft, reference, media)) return;
      if (!window.confirm("将把这张最终裁图、对应讲义原页和当前卡片文字发送到配置的云端模型，可能消耗 API 额度。是否检查？")) return;
      checking = true;
      show(null);
      let result = null, errorMessage = null;
      try { result = await api(endpoint(card, reference), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ card_version: card.version }) }); }
      catch (error) { errorMessage = error.message; }
      finally { checking = false; show(result, errorMessage); }
    });
    return section;
  }

  return { savedRecommendation, eligibleImages, labelFor, endpoint, markDraftDirty, create };
});
