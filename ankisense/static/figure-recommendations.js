(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FigureRecommendations = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const sessions = new Map();
  const active = job => ["pending", "running"].includes(job?.status);
  const json = body => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  function sameSource(card, draft) {
    try {
      const evidence = typeof draft.evidence === "string" ? JSON.parse(draft.evidence) : draft.evidence;
      return card.version === draft.version && card.question.trim() === draft.question.trim()
        && card.concept.trim() === draft.concept.trim()
        && JSON.stringify(card.evidence) === JSON.stringify(evidence);
    } catch { return false; }
  }
  function sameCheckSource(card, draft) {
    if (!sameSource(card, draft)) return false;
    try {
      const rubric = typeof draft.rubric === "string" ? draft.rubric.split("\n").filter(line => line.trim()).map(line => {
        const divider = line.indexOf("|");
        if (divider < 1) throw new Error("invalid rubric");
        return { point_id: line.slice(0, divider).trim(), text: line.slice(divider + 1).trim() };
      }) : draft.rubric;
      return card.answer.trim() === draft.answer.trim() && card.kind === draft.kind
        && JSON.stringify(card.rubric) === JSON.stringify(rubric);
    } catch { return false; }
  }
  function checkKey(jobId, candidateId, box, side) {
    return JSON.stringify([jobId, candidateId, box, side]);
  }
  function candidateCheckScope(candidate, jobId, draft, media = []) {
    let otherCrop = false;
    for (const reference of draft.images || []) {
      const source = media.find(item => item.id === reference.media_id)?.source;
      if (source?.job_id !== jobId || source?.candidate_id !== candidate.id) continue;
      if (JSON.stringify(source.bbox) === JSON.stringify(candidate.bbox)) {
        return { side: reference.side === "question" ? "question" : "answer", adopted: true, otherCrop };
      }
      otherCrop = true;
    }
    return { side: "answer", adopted: false, otherCrop };
  }
  function validBox(box) {
    return Array.isArray(box) && box.length === 4 && box.every(v => typeof v === "number" && Number.isFinite(v) && v >= 0 && v <= 1)
      && box[2] - box[0] >= .005 && box[3] - box[1] >= .005;
  }
  function dragBox(start, end) {
    const clamp = value => Math.max(0, Math.min(1, value));
    const [a, b, c, d] = [...start, ...end].map(clamp);
    return [Math.min(a, c), Math.min(b, d), Math.max(a, c), Math.max(b, d)];
  }
  function localImage(url) {
    return typeof url === "string" && /^\/api\/(?:image-recommendations|documents)\/[a-zA-Z0-9_/-]+$/.test(url);
  }
  function node(tag, cls, value) {
    const result = document.createElement(tag);
    if (cls) result.className = cls;
    if (value !== undefined) result.textContent = value;
    return result;
  }
  function btn(label, fn, disabled = false, cls = "btn secondary small") {
    const result = node("button", cls, label);
    result.type = "button";
    result.disabled = disabled;
    result.addEventListener("click", fn);
    return result;
  }
  // The original page remains untouched. This dialog only selects normalized
  // coordinates; the server renders the accepted rectangle to immutable media.
  function cropDialog(candidate, onAccept, opener, state) {
    if (!localImage(candidate.page_url)) return;
    const dialog = node("dialog", "card-image-dialog figure-crop-dialog");
    const title = node("h2", "", `第 ${candidate.page} 页 · 裁剪配图`);
    title.id = "figure-crop-title";
    dialog.setAttribute("aria-labelledby", title.id);
    const close = btn("取消", () => dialog.close());
    const header = node("div", "card-image-dialog-header");
    header.append(title, close);
    const help = node("p", "helper figure-crop-help", "拖动框选需要的范围，按 Esc 退出。");
    const viewport = node("div", "figure-crop-viewport");
    const stage = node("div", "figure-crop-stage");
    const image = node("img");
    image.src = candidate.page_url;
    image.alt = `讲义第 ${candidate.page} 页完整原图`;
    image.draggable = false;
    const rect = node("div", "figure-crop-selection");
    rect.setAttribute("aria-hidden", "true");
    stage.append(image, rect);
    viewport.append(stage);
    const controls = node("div", "figure-crop-controls");
    const fields = node("div", "figure-crop-fields");
    let box = validBox(candidate.bbox) ? [...candidate.bbox] : [0, 0, 1, 1];
    let loaded = false;
    let start = null;
    let check = null;
    const side = node("select");
    side.setAttribute("aria-label", "采纳配图的显示位置");
    for (const [value, label] of [["answer", "答案侧 · 作答后查看"], ["question", "题目侧 · 作答前可见"]]) {
      const option = node("option", "", label); option.value = value; side.append(option);
    }
    side.addEventListener("change", () => check?.refresh());
    const error = node("p", "inline-error");
    error.setAttribute("role", "alert");
    const accept = btn("加入卡片", () => { if (loaded && validBox(box)) { dialog.close(); onAccept([...box], side.value); } }, true, "btn small");
    const inputs = ["左边界 %", "上边界 %", "右边界 %", "下边界 %"].map((label, index) => {
      const wrapper = node("label", "field");
      const input = node("input");
      input.type = "number"; input.min = "0"; input.max = "100"; input.step = "0.1";
      input.setAttribute("aria-label", label);
      input.addEventListener("input", () => {
        box[index] = input.value === "" ? NaN : Number(input.value) / 100;
        update(false);
      });
      wrapper.append(node("span", "", label), input);
      fields.append(wrapper);
      return input;
    });
    function update(syncInputs = true) {
      const valid = validBox(box);
      accept.disabled = !loaded || !valid;
      error.textContent = valid ? "" : "请设置有效范围：右边界大于左边界，下边界大于上边界，均在 0–100% 内。";
      rect.hidden = !valid;
      if (valid) Object.assign(rect.style, { left: `${box[0] * 100}%`, top: `${box[1] * 100}%`, width: `${(box[2] - box[0]) * 100}%`, height: `${(box[3] - box[1]) * 100}%` });
      if (syncInputs) inputs.forEach((input, index) => { input.value = String(Math.round(box[index] * 1000) / 10); });
      check?.refresh();
    }
    const position = event => {
      const bounds = stage.getBoundingClientRect();
      return [(event.clientX - bounds.left) / bounds.width, (event.clientY - bounds.top) / bounds.height];
    };
    stage.addEventListener("pointerdown", event => {
      if (!loaded || event.button !== 0) return;
      start = position(event);
      stage.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    stage.addEventListener("pointermove", event => {
      if (!start) return;
      const next = dragBox(start, position(event));
      if (validBox(next)) { box = next; update(); }
    });
    const finish = () => { start = null; };
    stage.addEventListener("pointerup", finish);
    stage.addEventListener("pointercancel", finish);
    image.addEventListener("load", () => { loaded = true; update(); });
    image.addEventListener("error", () => { loaded = false; accept.disabled = true; error.textContent = "原图暂时无法读取，请关闭后重试。"; });
    const actions = node("div", "two-buttons");
    actions.append(btn("选择整页", () => { box = [0, 0, 1, 1]; update(); }), accept);
    const sideField = node("label", "field");
    sideField.append(node("span", "", "图片显示位置"), side);
    const precision = node("details", "figure-crop-precision");
    precision.append(node("summary", "", "精确调整范围"), fields);
    controls.append(precision, error, sideField);
    if (state) { check = checkPanel(state, candidate, () => box, () => side.value, "crop"); controls.append(check.element); }
    controls.append(actions);
    dialog.append(header, help, viewport, controls);
    dialog.addEventListener("close", () => { dialog.remove(); if (opener?.isConnected) opener.focus(); });
    document.body.append(dialog);
    update();
    dialog.showModal();
    close.focus();
  }
  function notifyCheckPanels(state) {
    for (const [key, view] of state.checkPanels || []) {
      if (view.element.isConnected) view.refresh();
      else state.checkPanels.delete(key);
    }
  }
  function checkPanel(state, candidate, getBox = () => candidate.bbox, getSide = () => "answer", location = "candidate") {
    const element = node("section", "figure-vision");
    element.setAttribute("aria-label", "配图分类与视觉检查");
    state.checks ||= new Map();
    state.checkPanels ||= new Map();
    function refresh() {
      const { card, draft, vision } = state.options;
      const box = getBox(), side = getSide();
      const key = checkKey(state.job?.id, candidate.id, box, side);
      const unchanged = sameCheckSource(card, draft) && !card.source_outdated;
      const entry = unchanged ? state.checks.get(key) : null;
      const available = Boolean(vision?.available);
      const check = btn(entry?.loading ? "正在检查…" : entry?.result ? "重新检查配图" : "检查配图", async () => {
        if (!sameCheckSource(state.options.card, state.options.draft) || !validBox(getBox())) return;
        const submitted = { job_id: state.job.id, candidate_id: candidate.id, card_version: card.version, bbox: [...getBox()], side: getSide() };
        const sentKey = checkKey(submitted.job_id, candidate.id, submitted.bbox, submitted.side);
        const pending = { loading: true };
        state.checks.set(sentKey, pending); notifyCheckPanels(state);
        try {
          pending.result = await state.options.api(`/api/cards/${encodeURIComponent(card.id)}/image-recommendations/check`, json(submitted));
        } catch (error) { pending.error = error.message; }
        finally { pending.loading = false; notifyCheckPanels(state); }
      }, !available || !unchanged || !validBox(box) || entry?.loading, "btn secondary small");
      element.replaceChildren(check);
      if (location === "candidate" && candidateCheckScope(candidate, state.job?.id, draft, state.options.media).otherCrop) {
        element.append(node("p", "notice warning", "已采纳图片的裁剪范围与上方候选不同。这里的结论只适用于上方推荐范围；请在裁剪窗口检查实际使用的范围。"));
      }
      if (available) element.append(node("p", "helper", "检查会将裁图、原页与卡片文字发送到当前模型，可能产生费用。"));
      if (!available) element.append(node("p", "helper", vision?.message || "请配置支持图片输入的云端模型后使用。"));
      if (!unchanged) element.append(node("p", "notice warning", "文字或评分要点有未保存的修改，请先保存，再检查当前图文组合。"));
      if (entry?.loading) {
        const loading = node("p", "helper", "正在核对图片与卡片内容…");
        loading.setAttribute("role", "status"); element.append(loading);
      }
      if (entry?.error) { const error = node("p", "inline-error", entry.error); error.setAttribute("role", "alert"); element.append(error); }
      if (entry?.result) {
        const result = entry.result;
        const names = { pass: "机器检查：未发现明显问题", attention: "机器检查：建议调整", uncertain: "机器检查：无法确定" };
        const report = node("div", `figure-vision-result ${result.verdict === "pass" ? "checked" : "attention"}`);
        report.append(node("strong", "", names[result.verdict] || names.uncertain));
        for (const reason of (result.reasons || []).slice(0, 2)) report.append(node("p", "figure-vision-reason", reason));
        const details = node("details", "figure-check-details");
        details.append(node("summary", "", "检查详情"));
        if (result.description) details.append(node("p", "", result.description));
        const classes = { diagram: "示意图", table: "表格", chart: "统计图", formula: "公式", photo: "照片", decoration: "装饰图", text: "文字区域", unknown: "无法确定" };
        if (result.classification?.label) details.append(node("p", "helper", `图像类别：${classes[result.classification.label] || "其他"}`));
        const labels = { supported: "支持题目", unrelated: "不相关", uncertain: "无法确定", complete: "完整", incomplete: "可能缺少内容", clear: "可辨认", unclear: "不清晰", yes: "可能泄露", no: "未发现" };
        const rows = [["与题目关系", result.relation], ["裁剪完整性", result.completeness], ["文字清晰度", result.legibility]];
        if (side === "question") rows.push(["题面泄露答案", result.answer_leakage]);
        const list = node("dl", "figure-vision-facts");
        for (const [label, value] of rows) list.append(node("dt", "", label), node("dd", "", labels[value] || "无法确定"));
        details.append(list);
        if (side === "answer") details.append(node("p", "helper", "答案侧允许包含答案文字；移到题目侧后需重新检查。"));
        for (const reason of (result.reasons || []).slice(2)) details.append(node("p", "figure-vision-reason", reason));
        report.append(details);
        element.append(report);
      }
    }
    state.checkPanels.set(`${state.job?.id}:${candidate.id}:${location}`, { element, refresh });
    refresh();
    return { element, refresh };
  }
  function create(options) {
    const host = node("section", "figure-recommendations");
    host.setAttribute("aria-label", "自动推荐讲义配图");
    const key = `${options.card.id}:${options.card.version}`;
    let state = sessions.get(key);
    if (!state) {
      state = { job: null, error: "", starting: false, polling: false, adopted: new Set() };
      sessions.set(key, state);
      try {
        const id = sessionStorage.getItem(`ankisense-figure-job:${key}`);
        if (id && /^[a-zA-Z0-9-]+$/.test(id)) state.job = { id, status: "pending" };
      } catch { /* A tab can work without storage. */ }
    }
    state.host = host;
    state.options = options;
    state.draw = () => draw(state);
    state.draw();
    if (active(state.job)) poll(state);
    return host;
  }
  function changed(cardId) {
    for (const state of sessions.values()) if (state.options.card.id === cardId && state.host.isConnected) state.draw();
  }
  async function poll(state) {
    if (state.polling || !state.job?.id) return;
    state.polling = true;
    const previous = JSON.stringify([state.job, state.error]);
    try {
      state.job = await state.options.api(`/api/image-recommendations/${encodeURIComponent(state.job.id)}`);
      state.error = "";
    } catch (error) {
      if ([404, 409].includes(error.status)) {
        state.job = { ...state.job, status: "failed", message: "原推荐已失效，请重新获取推荐。" };
        state.error = error.message;
      } else state.error = `${error.message} 可以点击「刷新状态」继续查看，本地任务不会因切换页面而丢失。`;
    }
    finally {
      state.polling = false;
      // Keep keyboard focus and avoid repeated live-region announcements while
      // a local worker reports the same state across multiple polls.
      if (JSON.stringify([state.job, state.error]) !== previous) state.draw();
      if (active(state.job) && !state.error) {
        clearTimeout(state.timer);
        state.timer = setTimeout(() => poll(state), 2500);
      }
    }
  }
  async function start(state) {
    const { card, draft, api } = state.options;
    if (state.starting || active(state.job) || !sameSource(card, draft)) return;
    state.starting = true; state.error = ""; state.draw();
    try {
      state.job = await api(`/api/cards/${encodeURIComponent(card.id)}/image-recommendations`, json({ card_version: card.version }));
      try { sessionStorage.setItem(`ankisense-figure-job:${card.id}:${card.version}`, state.job.id); } catch { /* Optional refresh recovery. */ }
      if (active(state.job)) poll(state);
    } catch (error) { state.error = error.message; }
    finally { state.starting = false; state.draw(); }
  }
  function draw(state) {
    const { card, draft, busy, run, onAdopt, api } = state.options;
    const host = state.host;
    const unchanged = sameSource(card, draft) && !card.source_outdated;
    const running = state.starting || active(state.job);
    const disabled = busy || !unchanged || draft.images.length >= 4;
    const header = node("div", "figure-recommend-header");
    header.append(node("strong", "", "引用页原图"), btn(running ? "正在准备…" : state.job ? "刷新原图" : "选择原图",
      () => start(state), disabled || running));
    host.replaceChildren(header);
    if (!unchanged) host.append(node("p", "notice warning", "请先保存题目和引用，再选择原图。"));
    if (draft.images.length >= 4) host.append(node("p", "helper", "已有 4 张配图，移除一张后可继续添加。"));
    if (running) {
      const progress = node("p", "helper", state.job?.message || "正在准备引用页原图…");
      progress.setAttribute("role", "status"); host.append(progress);
      if (state.job?.id) host.append(btn("取消", async () => {
        try {
          state.job = await api(`/api/image-recommendations/${encodeURIComponent(state.job.id)}/cancel`, json({}));
          clearTimeout(state.timer); state.error = "";
        } catch (error) { state.error = error.message; }
        state.draw();
      }));
    }
    if (state.error || state.job?.status === "failed") {
      const error = node("p", "inline-error", state.error || state.job.message || "原图准备失败，请重试。");
      error.setAttribute("role", "alert"); host.append(error);
    }
    const candidates = Array.isArray(state.job?.candidates) ? state.job.candidates : [];
    async function adopt(candidate, bbox, side) {
      if (!sameSource(card, draft) || draft.images.length >= 4) return;
      await run(async () => {
        const record = await api(`/api/cards/${encodeURIComponent(card.id)}/image-recommendations/adopt`,
          json({ job_id: state.job.id, candidate_id: candidate.id, card_version: card.version, bbox }));
        onAdopt(record, side);
      });
    }
    if (candidates.length) {
      const pages = node("div", "figure-page-list");
      for (const candidate of candidates) {
        const open = btn("", () => cropDialog(candidate, (box, side) => adopt(candidate, box, side), open, state),
          disabled || !localImage(candidate.page_url), "figure-page-choice");
        open.setAttribute("aria-label", `裁剪第 ${candidate.page} 页`);
        const preview = node("img"); preview.alt = `讲义第 ${candidate.page} 页`; preview.loading = "lazy";
        if (localImage(candidate.preview_url)) preview.src = candidate.preview_url;
        preview.addEventListener("error", () => preview.remove(), { once: true });
        open.append(preview, node("span", "", `第 ${candidate.page} 页 · 裁剪`)); pages.append(open);
      }
      host.append(pages);
    } else if (!running && !state.error && !state.job) {
      host.append(node("p", "helper", "从本卡引用页裁剪图片，默认放在答案侧。"));
    }
  }
  return { create, changed, sameSource, sameCheckSource, checkKey, candidateCheckScope, notifyCheckPanels, validBox, dragBox, localImage };
});
