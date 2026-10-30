(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.CardAutoReview = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function savedCard(card, draft) {
    return Boolean(card && draft && !draft.dirty && draft.version === card.version && !card.source_outdated);
  }

  function endpoint(card) {
    return `/api/cards/${encodeURIComponent(card.id)}/auto-review`;
  }
  function pendingImages(card) { return ["queued", "matching", "checking"].includes(card?.auto_illustration?.state); }

  function labelFor(result) {
    if (result?.stale || (result?.current === false && result?.review_id) || (result?.status === "auto_pass" && result?.current !== true)) return "机审结果已失效";
    return ({ queued: "等待自动检查", running: "正在自动检查…", interrupted: "检查已中断，可重试", auto_pass: "机器审核通过，可开始学习", needs_review: "待处理", attention: "发现需要修改的内容", unchecked: "尚未检查" })[result?.status] || "尚未检查";
  }

  function markDraftDirty() {
    if (typeof document === "undefined") return;
    for (const section of document.querySelectorAll(".card-auto-review")) {
      const status = section.querySelector(".card-auto-review-status");
      if (status) {
        status.textContent = "编辑内容未保存，原机审结果不适用于当前修改";
        status.className = "card-auto-review-status";
      }
      const action = section.querySelector("button.card-auto-review-action");
      if (action) action.disabled = true;
      const refresh = section.querySelector("button.card-auto-review-refresh");
      if (refresh) refresh.disabled = true;
      const study = section.querySelector("button.card-auto-review-study");
      if (study) study.hidden = true;
    }
  }

  function node(tag, className, content) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (content !== undefined) item.textContent = content;
    return item;
  }

  function citation(parent, entry, onLocate) {
    if (!entry || typeof entry !== "object") return;
    const item = node("li", "card-auto-review-citation");
    item.append(node("span", "helper", `原文片段 ${entry.block_id || "未知"}`));
    if (typeof entry.exact_quote === "string" && entry.exact_quote) item.append(node("blockquote", "", entry.exact_quote));
    if (entry.block_id && onLocate) {
      const locate = node("button", "btn quiet small", "定位原文");
      locate.type = "button";
      locate.addEventListener("click", () => onLocate(entry.block_id));
      item.append(locate);
    }
    parent.append(item);
  }

  function renderChecks(result, card, onLocate) {
    const checks = result?.checks;
    if (!checks || typeof checks !== "object" || Array.isArray(checks)) return null;
    const details = node("details", "card-auto-review-checks");
    details.append(node("summary", "", "查看题目、答案和评分要点的逐项结果"));
    const body = node("div", "card-auto-review-check-body");
    if (checks.minimal_answer) body.append(node("h4", "", "机审先核对的最短充分答案"), node("p", "helper", checks.minimal_answer));
    const labels = [["question_answerable", "题目能根据引用作答"], ["answer_relevant", "答案回应了题目"], ["answer_supported", "答案全部断言有引文支持"]];
    const basics = node("ul", "card-auto-review-basics");
    for (const [key, label] of labels) {
      if (typeof checks[key] === "boolean") basics.append(node("li", checks[key] ? "passed" : "needs-review", `${label}：${checks[key] ? "是" : "否，请核对"}`));
    }
    if (basics.children.length) body.append(basics);

    if (Array.isArray(checks.unsupported_claims) && checks.unsupported_claims.length) {
      body.append(node("h4", "", "答案中缺少证据或存在矛盾的表述"));
      const unsupported = node("ul", "card-auto-review-problems");
      for (const claim of checks.unsupported_claims.slice(0, 12)) if (typeof claim === "string") unsupported.append(node("li", "", claim));
      body.append(unsupported);
    }

    if (Array.isArray(checks.rubric_checks) && checks.rubric_checks.length) {
      body.append(node("h4", "", "评分要点逐项核对"));
      const rubrics = node("ol", "card-auto-review-rubrics");
      const verdicts = { supported: "有原文支持", contradicted: "与原文矛盾", uncertain: "证据不足，需人工确认" };
      for (const check of checks.rubric_checks.slice(0, 12)) {
        if (!check || typeof check !== "object") continue;
        const point = (card.rubric || []).find(item => item.point_id === check.point_id);
        const item = node("li", "");
        item.append(node("strong", "", point?.text || check.point_id || "未找到对应评分要点"));
        item.append(node("span", check.verdict === "supported" ? "passed" : "needs-review", ` · ${verdicts[check.verdict] || "需人工确认"}`));
        const scope = { required: "本题必答", given: "题干已给，不应要求重复", background: "额外背景，不应强制评分", uncertain: "是否必答尚不确定" };
        if (check.scope) item.append(node("p", check.scope === "required" ? "helper" : "needs-review", `${scope[check.scope] || "需核对题目范围"}：${check.scope_reason || ""}`));
        if (check.exact_quote && check.block_id) {
          const proof = node("ul", "card-auto-review-rubric-proof");
          citation(proof, check, onLocate);
          item.append(proof);
        } else item.append(node("p", "helper", "当前要点没有可核验的直接引文，请对照讲义确认。"));
        rubrics.append(item);
      }
      body.append(rubrics);
    }
    if (checks.question_coverage) {
      body.append(node("p", "helper", `题目各部分覆盖：${({ complete: "已覆盖", incomplete: "有遗漏", uncertain: "尚不确定" })[checks.question_coverage]}`));
      for (const missing of checks.missing_question_parts || []) body.append(node("p", "needs-review", `缺少：${missing}`));
    }

    if (Array.isArray(checks.answer_citations) && checks.answer_citations.length) {
      body.append(node("h4", "", "参考答案的原文依据"));
      const citations = node("ul", "card-auto-review-citations");
      for (const entry of checks.answer_citations.slice(0, 8)) citation(citations, entry, onLocate);
      body.append(citations);
    }
    details.append(body);
    return details;
  }

  function create({ card, draft, api, run, configured = false, busy = false, checking = false, onChecked, onRead, onLocate, onStudy }) {
    const section = node("section", "card-auto-review");
    section.setAttribute("aria-label", "整卡自动审核");
    section.append(node("h3", "", "自动检查"));
    const status = node("strong", "card-auto-review-status", checking ? "正在检查当前卡片…" : "正在读取机审状态…");
    status.setAttribute("role", "status");
    const detail = node("div", "card-auto-review-detail");
    const action = node("button", "btn secondary small card-auto-review-action", "检查当前卡片");
    action.type = "button";
    const refresh = node("button", "btn quiet small card-auto-review-refresh", "刷新机审状态");
    refresh.type = "button";
    const study = node("button", "btn small card-auto-review-study", "开始学习 →");
    study.type = "button";
    study.hidden = true;
    study.addEventListener("click", () => { if (savedCard(card, draft) && card.status === "approved" && onStudy) onStudy(); });
    section.append(status, detail, action, refresh, study);
    let loading = savedCard(card, draft) && !checking;
    let lastResult = null;

    function show(result, error) {
      if (!savedCard(card, draft)) {
        status.textContent = card?.source_outdated ? "引用原文已更新，请先修正卡片并保存" : "先保存当前修改，再对最终卡片进行机审";
        status.className = "card-auto-review-status";
        detail.replaceChildren();
        action.disabled = true;
        refresh.disabled = true;
        study.hidden = true;
        return;
      }
      const active = ["queued", "running"].includes(result?.status || card.review_state);
      const imaging = pendingImages(card);
      const approved = card.status === "approved" && !card.source_outdated && !((card.approval?.method === "machine" || card.approval_method === "automatic") && result && result.current !== true);
      status.textContent = checking ? "正在检查当前卡片…" : loading ? "正在读取机审状态…" : error ? "检查状态暂不可用" : approved ? (card.approval?.method === "machine" || card.approval_method === "automatic") ? "机器审核通过，可开始学习" : "人工批准，可开始学习" : imaging ? "正在配图，完成后继续检查卡片" : result?.status === "auto_pass" && result.current === true ? "机器检查通过，学习状态尚未更新" : labelFor(result);
      status.className = `card-auto-review-status${result?.status === "auto_pass" && result?.current === true ? " passed" : ""}`;
      detail.replaceChildren();
      if (error) detail.append(node("p", "inline-error", error));
      else {
        const reasons = Array.isArray(result?.reasons) ? result.reasons : typeof result?.reason === "string" ? [result.reason] : [];
        for (const reason of reasons.slice(0, 8)) if (typeof reason === "string" && reason.trim()) detail.append(node("p", "helper", reason));
        for (const issue of (Array.isArray(result?.issues) ? result.issues : []).filter(item => !reasons.includes(item)).slice(0, 8)) {
          if (typeof issue === "string" && issue.trim()) detail.append(node("p", "card-auto-review-issue", issue));
        }
        const checks = renderChecks(result, card, onLocate);
        if (checks) detail.append(checks);
      }
      if (!configured && !approved) detail.append(node("p", "helper", "请先在模型设置中配置云端模型，再开始自动检查。演示卡片可自行核对。"));
      const localBlock = !result?.retryable && result?.status === "needs_review" && result?.current === false && Array.isArray(result?.reasons) && result.reasons.length > 0;
      const finished = result?.current === true && !result?.retryable && !["unchecked", "queued", "running"].includes(result?.status);
      action.textContent = imaging ? "等待自动配图…" : checking || active ? "自动检查中…" : localBlock ? "先处理卡片或来源问题" : finished || approved ? "已检查当前版本" : result?.retryable ? "重试自动检查" : "检查当前卡片";
      action.disabled = checking || active || imaging || loading || busy || !configured || localBlock || finished || approved;
      action.hidden = approved;
      refresh.disabled = checking || loading || busy;
      study.hidden = !approved || !onStudy;
    }

    show(null);
    async function loadStatus() {
      if (checking || busy || loading || !savedCard(card, draft)) return;
      loading = true;
      if (!lastResult) show(null);
      try {
        const result = await api(endpoint(card));
        loading = false;
        lastResult = result;
        if (onRead) onRead(result);
        if (section.isConnected) show(result);
      } catch (error) {
        loading = false;
        if (section.isConnected) show(null, error.message);
      }
    }
    section.refreshReview = loadStatus;
    loading = false;
    if (savedCard(card, draft) && !checking) loadStatus();
    refresh.addEventListener("click", loadStatus);

    action.addEventListener("click", async () => {
      if (checking || loading || busy || pendingImages(card) || !configured || !savedCard(card, draft)) return;
      await run(async () => {
        const result = await api(endpoint(card), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ expected_version: card.version }) });
        if (onChecked) await onChecked(result);
      });
    });
    return section;
  }

  return { savedCard, endpoint, labelFor, markDraftDirty, create };
});
