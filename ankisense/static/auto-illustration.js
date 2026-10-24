(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.AutoIllustration = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const active = card => ["queued", "matching", "checking"].includes(card?.auto_illustration?.state);
  const needsImage = card => card?.image_intent?.need === "required";
  const requiredMissing = card => needsImage(card) && !(card.images || []).some(image => image?.media_id && image.side === "question");
  const labels = { queued: "等待配图", matching: "正在匹配讲义图片", checking: "正在检查配图", attached: "已自动配图", not_found: "未找到合适配图", needs_review: "配图需要处理", failed: "配图未完成", interrupted: "配图已中断", skipped: "未自动配图" };
  function describe(card) {
    const result = card?.auto_illustration;
    if (!result && !needsImage(card)) return null;
    const required = requiredMissing(card);
    return {
      label: result?.state === "skipped" && card?.image_intent?.need === "none" ? "无需配图" : labels[result?.state] || (required ? "等待题目配图" : "配图状态待确认"),
      message: result?.message || "",
      detail: required ? "这道题需要看图作答，题目配图完成并通过检查后才能开始学习。" : needsImage(card) && result?.state !== "attached" ? "题目配图仍需通过检查，完成后才能开始学习。" : result?.state === "attached" ? "配图已保存；卡片内容检查通过后即可学习。" : card?.image_intent?.need === "helpful" && !active(card) ? "这张图用于辅助理解；未配图不影响文字卡片继续检查。" : "",
      attention: required || ["needs_review", "failed", "interrupted"].includes(result?.state),
    };
  }
  function create({ card, draft, onReload, compact = false }) {
    const section = document.createElement("section");
    section.className = `auto-illustration${compact ? " compact" : ""}`;
    section.setAttribute("aria-label", "自动配图状态");
    const title = document.createElement("strong"); title.className = "auto-illustration-status";
    const message = document.createElement("p"); message.className = "helper auto-illustration-message";
    const detail = document.createElement("p"); detail.className = "helper auto-illustration-detail";
    const changed = document.createElement("p"); changed.className = "helper auto-illustration-changed";
    const reload = document.createElement("button"); reload.type = "button"; reload.className = "btn secondary small";
    reload.textContent = "载入最新版本";
    reload.addEventListener("click", () => onReload?.());
    section.append(title, message, detail, changed, reload);
    section.refreshIllustration = () => {
      const view = describe(card);
      const stale = draft && draft.version !== card.version;
      section.hidden = !view && !stale;
      section.classList.toggle("attention", Boolean(view?.attention));
      title.textContent = view?.label || "卡片已更新";
      message.textContent = view?.message || "";
      detail.textContent = view?.detail || "";
      changed.textContent = stale ? draft.dirty ? "后台已保存新版本。你的未保存修改和当前预览已保留；载入前请先复制需要的修改。" : "后台已保存新版本，当前预览仍保留旧版本。载入最新版本即可查看。" : "";
      reload.hidden = !stale || !onReload || compact;
    };
    section.refreshIllustration();
    return section;
  }
  return { active, needsImage, requiredMissing, describe, create };
});
