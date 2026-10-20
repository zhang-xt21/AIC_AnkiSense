"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const app = { data: null, view: "materials", uploadTab: "pdf", documentId: null, cardId: null, cardDrafts: {}, cardEditBusy: null, cardEditErrors: {}, practiceId: null, practiceFilter: "all", practiceLoadedKey: null, practiceDrafts: {}, selectedBlocks: new Set(), reviewFilter: "all", attempt: null, answer: "", busy: false, sourcePage: {}, proofDrafts: {}, proofBusy: null, proofErrors: {} };
const viewNames = { materials: "学习材料", review: "制卡工作台", practice: "开始练习", export: "导出至 Anki", settings: "模型设置" };
const verdictNames = { correct: "回答正确", incomplete: "还不完整", incorrect: "需要修正", uncertain: "暂时无法判断" };
const verdictColors = { correct: "green", incomplete: "amber", incorrect: "red", uncertain: "blue" };
const statusNames = { draft: "待审核", approved: "已批准", rejected: "已拒绝" };
const pageStatusNames = { pending: "待校对", reviewed: "已校对", auto_verified: "机器已核对", empty: "已确认空白", failed: "识别失败" };
function sourcePageStatus(page) { return page.status === "empty" && !page.reviewed_at ? "未识别到文字" : pageStatusNames[page.status] || page.status; }
function sourcePageConfirmed(page) { return page.status === "reviewed" || (page.status === "auto_verified" && page.verification?.revision === page.revision) || (page.status === "empty" && Boolean(page.reviewed_at)); }
app.sourceCalibrations = {};
const proofStorageKey = "ankisense-proofreading-drafts-v1";
const practiceStorageKey = "ankisense-practice-drafts-v1";
const practiceFilterNames = { all: "全部", uncompleted: "未完成", completed: "已完成" };
const autoStorageKey = "ankisense-auto-card-jobs-v1";
const autoStatusNames = { pending: "已排队", running: "正在制卡", awaiting_confirmation: "等待确认原文", completed: "制卡完成", failed: "制卡未完成", cancelled: "已取消", outdated: "来源已更新" };
Object.assign(app, { autoJobs: {}, autoLoaded: new Set(), autoLoading: new Set(), autoBusy: {}, autoErrors: {}, autoOptions: {}, autoTimers: {}, autoEpoch: {} });
try { const saved = JSON.parse(sessionStorage.getItem(autoStorageKey) || "{}"); if (saved && typeof saved === "object" && !Array.isArray(saved)) app.autoJobs = saved; } catch { /* Job history is also saved by the service. */ }
let toastTimer;
try { app.practiceDrafts = JSON.parse(sessionStorage.getItem(practiceStorageKey) || "{}"); } catch { app.practiceDrafts = {}; }
if (!app.practiceDrafts || typeof app.practiceDrafts !== "object" || Array.isArray(app.practiceDrafts)) app.practiceDrafts = {};
try { app.proofDrafts = JSON.parse(sessionStorage.getItem(proofStorageKey) || "{}"); } catch { app.proofDrafts = {}; }
if (!app.proofDrafts || typeof app.proofDrafts !== "object" || Array.isArray(app.proofDrafts)) app.proofDrafts = {};
function keepProofDrafts() {
  try { sessionStorage.setItem(proofStorageKey, JSON.stringify(app.proofDrafts)); } catch { /* The in-memory draft still survives navigation within the app. */ }
}
window.addEventListener("beforeunload", event => {
  if (!Object.keys(app.proofDrafts).length && !Object.values(app.cardDrafts).some(draft => draft.dirty) && !app.cardEditBusy) return;
  event.preventDefault();
  event.returnValue = "";
});

function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (value === undefined || value === null) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "on") for (const [event, handler] of Object.entries(value)) node.addEventListener(event, handler);
    else if (["value", "checked", "disabled", "hidden", "required", "selected"].includes(key)) node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child !== undefined && child !== null && child !== false) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
const text = (tag, value, className) => el(tag, { text: value, class: className });
const button = (label, action, className = "btn", disabled = false) => el("button", { type: "button", class: className, disabled, on: { click: action } }, label);
const badge = (label, color = "") => text("span", label, `pill ${color}`);
const note = (message, kind = "") => text("div", message, `notice ${kind}`);
function field(label, input, help) {
  return el("label", { class: "field" }, text("span", label), input, help ? text("span", help, "helper") : null);
}
function panel(title, subtitle, content, extra) {
  return el("section", { class: "panel" }, el("div", { class: "panel-header" }, el("div", {}, text("h2", title), subtitle ? text("div", subtitle, "meta") : null), extra), el("div", { class: "panel-body" }, content));
}
function empty(title, description, action, symbol = "▱") {
  return el("div", { class: "empty-state" }, text("span", symbol, "empty-symbol"), text("h2", title), text("p", description), action);
}
function heading(eyebrow, title, description, actions) {
  return el("div", { class: "page-heading" }, el("div", {}, text("span", eyebrow, "eyebrow page-kicker"), text("h1", title), text("p", description, "lead")), actions ? el("div", { class: "page-actions" }, actions) : null);
}
function toast(message, error = false) {
  clearTimeout(toastTimer);
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast${error ? " error" : ""}`;
  node.hidden = false;
  toastTimer = setTimeout(() => { node.hidden = true; }, error ? 7000 : 4200);
}
async function api(path, options = {}) {
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); } catch { throw new Error("本地服务返回了无法读取的结果，请确认服务仍在运行。"); }
  if (!response.ok) {
    const detail = data.detail;
    const error = new Error(typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map(item => item.msg || "输入格式不正确").join("；") : typeof detail?.message === "string" ? detail.message : "请求未完成，请稍后重试。");
    error.status = response.status;
    throw error;
  }
  return data;
}
function jsonOptions(method, body) { return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }; }
async function refresh() {
  app.data = await api("/api/state");
  for (const key of ["documents", "blocks", "cards", "attempts", "source_pages", "media"]) app.data[key] ||= [];
  if (!app.data.documents.some(item => item.id === app.documentId)) app.documentId = app.data.documents[0]?.id || null;
  const eligibleIds = new Set(app.data.documents.flatMap(doc => blocksFor(doc.id).filter(blockCanGenerate).map(block => block.block_id)));
  for (const id of app.selectedBlocks) if (!eligibleIds.has(id)) app.selectedBlocks.delete(id);
  render();
}
function setView(view) {
  app.view = view;
  render();
  window.scrollTo({ top: 0, behavior: "instant" });
}
async function actionWithButton(btn, label, callback, errorNode) {
  if (btn.disabled) return;
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = label;
  if (errorNode) errorNode.textContent = "";
  try { await callback(); } catch (error) {
    if (errorNode) errorNode.textContent = error.message;
    else toast(error.message, true);
  } finally { btn.disabled = false; btn.textContent = original; }
}
function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function documentFor(id) { return app.data.documents.find(item => item.id === id); }
function sourcePagesFor(id) { return (app.data.source_pages || []).filter(item => item.document_id === id).sort((a, b) => a.page - b.page); }
function blocksFor(id) {
  const pages = sourcePagesFor(id);
  const ids = pages.length ? new Set(pages.flatMap(page => page.block_ids || [])) : null;
  return app.data.blocks.filter(item => item.document_id === id && (!ids || ids.has(item.block_id))).sort((a, b) => a.page - b.page || a.block_id.localeCompare(b.block_id, "en", { numeric: true }));
}
function blockCanGenerate(block) {
  const page = sourcePagesFor(block.document_id).find(item => item.page === block.page);
  return !page || sourcePageConfirmed(page);
}
function proofKey(doc, page) { return `${doc.id}:${page.page}`; }
function focusProofreader() { $("#source-proofreader")?.scrollIntoView({ block: "start", behavior: "instant" }); $("#proof-page-selector")?.focus({ preventScroll: true }); }
function previewSourceImage(doc, page, opener) {
  const dialog = el("dialog", { class: "proof-preview", "aria-labelledby": "proof-preview-title" });
  const image = el("img", { src: page.image_url, alt: `${doc.title}，PDF 第 ${page.page} 页原图` });
  const viewport = el("div", { class: "proof-preview-viewport", tabindex: "0", "aria-label": "原图预览，可滚动查看" }, image);
  const zoom = button("放大原图", () => { const expanded = viewport.classList.toggle("is-zoomed"); zoom.textContent = expanded ? "适合窗口" : "放大原图"; zoom.setAttribute("aria-pressed", String(expanded)); }, "btn secondary small");
  zoom.setAttribute("aria-pressed", "false");
  dialog.append(el("div", { class: "proof-preview-header" }, el("h2", { id: "proof-preview-title" }, `PDF 第 ${page.page} 页 · 原图`), el("div", { class: "two-buttons" }, zoom, button("关闭预览", () => dialog.close(), "btn small"))), viewport);
  dialog.addEventListener("close", () => { dialog.remove(); opener.focus(); });
  dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
  document.body.append(dialog);
  dialog.showModal();
}
function sourceLabel(block) { return block.document_id?.startsWith("pdf-") ? `第 ${block.page} 页` : `片段 ${block.page}`; }
function announce(message) { $("#announcer").textContent = message; }
function showFeedback() {
  if (app.view !== "practice") return;
  const attempt = app.attempt;
  announce(attempt?.diagnosis ? `分析完成：${verdictNames[attempt.diagnosis.verdict] || "请查看反馈"}` : { failed: "分析未完成，你的回答已保留。", unanswered: "你还没有填写回答。", pending: "回答已保存，正在分析。" }[attempt?.status] || "反馈已更新。");
  const target = $("#feedback-focus");
  if (!target) return;
  target.focus({ preventScroll: true });
  const bounds = target.getBoundingClientRect();
  if (window.matchMedia("(max-width: 980px)").matches || bounds.top < 24 || bounds.bottom > window.innerHeight) (target.closest(".feedback-panel") || target).scrollIntoView({ block: "start", behavior: "instant" });
}
function modeIsCloud() { return app.data.mode === "cloud" && app.data.configured; }
function autoRunning(job) { return ["pending", "running"].includes(job?.status); }
function autoDocumentDirty(documentId) { return Object.keys(app.proofDrafts).some(key => key.startsWith(`${documentId}:`)); }
function autoPath(documentId) { return `/api/documents/${encodeURIComponent(documentId)}/auto-card-jobs`; }
function persistAutoJobs() { try { sessionStorage.setItem(autoStorageKey, JSON.stringify(app.autoJobs)); } catch { /* The server retains all jobs. */ } }
function updateAutoPanel(documentId) {
  if (app.view !== "materials" || app.documentId !== documentId) return;
  const previous = $("#auto-card-console");
  const doc = documentFor(documentId);
  if (previous && doc) previous.replaceWith(renderAutoGenerationPanel(doc));
}
function updateAutoSources(documentId, state) {
  app.data = state;
  if (app.view !== "materials" || app.documentId !== documentId) return;
  const doc = documentFor(documentId);
  if (!doc) return;
  // Update only source panes. Import forms and in-progress proofreading stay intact.
  const proof = $("#source-proofreader");
  if (proof && !app.proofBusy && !autoDocumentDirty(documentId)) {
    const focused = document.activeElement?.id;
    proof.replaceWith(renderProofreader(doc));
    if (["proof-text", "proof-page-selector"].includes(focused)) $(`#${focused}`)?.focus({ preventScroll: true });
  }
  const source = $("#material-source-panel");
  if (source) {
    const scroll = source.querySelector(".source-scroll")?.scrollTop || 0;
    source.replaceWith(renderSourcePanel());
    const next = $("#material-source-panel .source-scroll");
    if (next) next.scrollTop = scroll;
  }
}
function rememberAutoJob(job) {
  if (!job?.id || !job.document_id) return;
  const jobs = Array.isArray(app.autoJobs[job.document_id]) ? app.autoJobs[job.document_id] : [];
  const index = jobs.findIndex(item => item.id === job.id);
  if (index < 0) jobs.unshift(job); else jobs[index] = job;
  app.autoJobs[job.document_id] = jobs;
  persistAutoJobs();
  if (autoRunning(job)) scheduleAutoPoll(job);
  else { clearTimeout(app.autoTimers[job.id]); delete app.autoTimers[job.id]; }
}
function scheduleAutoPoll(job, delay = 2000) {
  if (app.autoTimers[job.id]) return;
  app.autoTimers[job.id] = setTimeout(async () => {
    delete app.autoTimers[job.id];
    const epoch = app.autoEpoch[job.id] || 0;
    try {
      const current = await api(`/api/auto-card-jobs/${encodeURIComponent(job.id)}`);
      if (epoch !== (app.autoEpoch[job.id] || 0)) return;
      rememberAutoJob(current);
      if (!autoRunning(current)) {
        const state = await api("/api/state");
        if (epoch !== (app.autoEpoch[job.id] || 0)) return;
        updateAutoSources(job.document_id, state);
      }
      delete app.autoErrors[job.document_id];
      updateAutoPanel(job.document_id);
    } catch (error) {
      if (epoch !== (app.autoEpoch[job.id] || 0)) return;
      app.autoErrors[job.document_id] = `${error.message} 任务保存在本机，稍后会再次查询。`;
      updateAutoPanel(job.document_id);
      scheduleAutoPoll(job, 6000);
    }
  }, delay);
}
async function loadAutoJobs(documentId, force = false) {
  if (app.autoLoading.has(documentId) || (!force && app.autoLoaded.has(documentId))) return;
  app.autoLoading.add(documentId);
  try {
    const result = await api(autoPath(documentId));
    app.autoJobs[documentId] = Array.isArray(result.jobs) ? result.jobs : [];
    app.autoLoaded.add(documentId);
    delete app.autoErrors[documentId];
    persistAutoJobs();
    for (const job of app.autoJobs[documentId]) if (autoRunning(job)) scheduleAutoPoll(job);
  } catch (error) { app.autoErrors[documentId] = error.message; }
  finally { app.autoLoading.delete(documentId); updateAutoPanel(documentId); }
}
async function autoJobOperation(doc, operation, job = null) {
  if (app.autoBusy[doc.id]) return;
  if (["start", "retry"].includes(operation) && autoDocumentDirty(doc.id)) return toast("请先保存或放弃此材料的校对修改，再自动制卡。", true);
  if (["start", "retry"].includes(operation) && !modeIsCloud()) return toast("请先配置云端模型，再自动制卡。", true);
  if (operation === "start" && (app.autoJobs[doc.id] || []).some(autoRunning)) return;
  app.autoBusy[doc.id] = operation;
  if (job) app.autoEpoch[job.id] = (app.autoEpoch[job.id] || 0) + 1;
  delete app.autoErrors[doc.id];
  updateAutoPanel(doc.id);
  try {
    const options = app.autoOptions[doc.id] || { max_cards: 30, image_mode: "recommend" };
    if (operation === "start" && (!Number.isInteger(options.max_cards) || options.max_cards < 1 || options.max_cards > 60)) throw new Error("卡片上限应为 1–60 的整数。");
    const path = operation === "start" ? autoPath(doc.id) : `/api/auto-card-jobs/${encodeURIComponent(job.id)}/${operation}`;
    const result = await api(path, jsonOptions("POST", operation === "start" ? options : {}));
    rememberAutoJob(result);
    announce(`${doc.title}：${autoStatusNames[result.status] || result.message || "任务已更新"}`);
  } catch (error) { app.autoErrors[doc.id] = error.message; }
  finally { delete app.autoBusy[doc.id]; updateAutoPanel(doc.id); }
}
async function showAutoCards(doc, job) {
  const initiatingDocument = app.documentId;
  try {
    const state = await api("/api/state");
    app.data = state;
    if (app.view !== "materials" || initiatingDocument !== doc.id || app.documentId !== doc.id) return;
    const ids = new Set(job.card_ids || []);
    app.cardId = app.data.cards.find(card => ids.has(card.id))?.id || null;
    app.reviewDocument = doc.id;
    app.reviewFilter = "all";
    setView("review");
  } catch (error) { toast(error.message, true); }
}
function renderAutoIssues(issues) {
  const evidenceText = issue => typeof issue.evidence === "string" ? issue.evidence : (issue.evidence || []).map(item => {
    const source = app.data.blocks.find(block => block.block_id === item.block_id);
    return `${source ? sourceLabel(source) : "原文依据"}：${item.quote || ""}`;
  }).join("\n");
  const blockLabels = issue => [...new Set((issue.block_ids || []).map(id => app.data.blocks.find(block => block.block_id === id)).filter(Boolean).map(sourceLabel))].join("、");
  return el("ul", { class: "auto-issues" }, ...issues.map(issue => el("li", {},
    el("div", { class: "auto-issue-heading" }, badge(issue.severity === "error" ? "需修正" : "需核对", issue.severity === "error" ? "red" : "amber"), text("strong", issue.reason || issue.code || "请核对此疑点")),
    issue.page ? text("p", `来源：第 ${issue.page} 页`, "helper") : null,
    blockLabels(issue) ? text("p", `原文位置：${blockLabels(issue)}`, "helper") : null,
    evidenceText(issue) ? text("p", evidenceText(issue), "auto-issue-evidence") : null,
    issue.suggestion ? text("p", issue.suggestion, "helper") : null)));
}
function renderAutoGenerationPanel(doc) {
  const jobs = Array.isArray(app.autoJobs[doc.id]) ? app.autoJobs[doc.id] : [];
  const job = jobs[0];
  const active = jobs.some(autoRunning);
  const busy = Boolean(app.autoBusy[doc.id]);
  const options = active ? { max_cards: 30, image_mode: "recommend", ...jobs.find(autoRunning)?.options }
    : (app.autoOptions[doc.id] ||= { max_cards: 30, image_mode: "recommend" });
  const dirty = autoDocumentDirty(doc.id);
  const body = el("section", { id: "auto-card-console", class: "auto-card-console", "aria-label": "自动制卡控制台", "aria-busy": String(busy) });
  const maxCards = el("input", { type: "number", min: "1", max: "60", step: "1", value: options.max_cards, disabled: active || busy, "aria-label": "自动制卡张数上限" });
  maxCards.addEventListener("input", () => { options.max_cards = Number(maxCards.value); });
  const imageMode = el("select", { disabled: active || busy, "aria-label": "自动配图方式" }, ...[["recommend", "本机推荐配图"], ["off", "仅生成文字"], ["checked", "配图并云端检查"]].map(([value, label]) => el("option", { value, selected: options.image_mode === value }, label)));
  const imageHint = text("p", options.image_mode === "checked" ? "已选择云端检查：会发送候选裁图、对应原页和卡片内容给所配置的模型。" : options.image_mode === "recommend" ? "配图推荐在本机处理引用页；准备候选后供你核对采纳。" : "本次只生成文字卡片。", "helper auto-image-hint");
  const sourceOption = doc.file_url ? el("input", { type: "checkbox", checked: options.source_mode === "visual", disabled: active || busy, "aria-label": "自动校准原页并继续制卡" }) : null;
  if (sourceOption) sourceOption.addEventListener("change", () => { if (sourceOption.checked) options.source_mode = "visual"; else delete options.source_mode; });
  imageMode.addEventListener("change", () => { options.image_mode = imageMode.value; imageHint.textContent = imageMode.value === "checked" ? "已选择云端检查：会发送候选裁图、对应原页和卡片内容给所配置的模型。" : imageMode.value === "recommend" ? "配图推荐在本机处理引用页；准备候选后供你核对采纳。" : "本次只生成文字卡片。"; });
  const start = button(busy && app.autoBusy[doc.id] === "start" ? "正在提交…" : "整份材料自动制卡 →", () => autoJobOperation(doc, "start"), "btn", busy || active || dirty || !modeIsCloud() || !app.autoLoaded.has(doc.id));
  start.dataset.autoStart = "";
  body.append(text("h3", "整份材料自动制卡"), text("p", "自动整理知识点并分批生成草稿，再检查答案、依据与疑点。张数是上限，不会为凑数重复出题。", "helper"), el("div", { class: "auto-card-options" }, field("卡片上限", maxCards), field("配图方式", imageMode)), imageHint,
    sourceOption ? field("自动校准原页并继续制卡", sourceOption, "主动开启后，每个待确认原页会发送给模型读取两次；可靠修正自动采用，其余疑点等你确认。需要模型支持图片输入。") : null, start,
    text("p", dirty ? "有未保存的校对修改，请先保存或放弃。" : !modeIsCloud() ? "请先到模型设置配置云端模型。" : "疑点来源需要你确认；机器检查通过后，卡片仍需人工审核。", "helper auto-start-hint"));
  if (app.autoErrors[doc.id]) body.append(el("div", { class: "notice error", role: "alert" }, app.autoErrors[doc.id], button("重新查询任务", () => loadAutoJobs(doc.id, true), "btn quiet small", busy)));
  if (job) {
    const total = Number(job.progress?.total) || 0;
    const completed = Number(job.progress?.completed) || 0;
    const status = el("div", { class: "auto-job-state", "data-job-id": job.id, "data-job-status": job.status },
      el("div", { class: "auto-job-heading" }, badge(autoStatusNames[job.status] || job.status, job.status === "completed" ? "green" : ["failed", "outdated"].includes(job.status) ? "red" : "blue"), text("span", `${completed} / ${total} 批 · ${(job.card_ids || []).length} 张草稿`, "meta")),
      el("progress", { value: completed, max: Math.max(1, total), "aria-label": `自动制卡进度：${completed} / ${total} 批` }), text("p", job.message || "任务已保存。", "helper"));
    if (job.error && job.error !== job.message) status.append(note(typeof job.error === "string" ? job.error : "本次处理未完成，请查看疑点后重试。", "error"));
    const controls = el("div", { class: "two-buttons" });
    if ((job.card_ids || []).length) controls.append(button("查看已生成卡片 →", () => showAutoCards(doc, job), "btn secondary small", busy));
    if (autoRunning(job)) controls.append(button("取消任务", () => autoJobOperation(doc, "cancel", job), "btn quiet small", busy));
    if (["awaiting_confirmation", "failed", "cancelled"].includes(job.status)) controls.append(button(job.status === "awaiting_confirmation" ? "已确认原文，继续制卡" : "重试未完成部分", () => autoJobOperation(doc, "retry", job), "btn secondary small", busy || dirty || !modeIsCloud()));
    if (job.status === "awaiting_confirmation") controls.append(button("定位待确认原文 ↑", () => { const page = (job.issues || []).find(issue => issue.page)?.page; if (page) app.sourcePage[doc.id] = page; render(); focusProofreader(); }, "btn quiet small"));
    status.append(controls);
    if (job.issues?.length) status.append(el("details", { class: "auto-job-issues", open: job.status === "awaiting_confirmation" ? "" : null }, text("summary", `${job.issues.length} 项疑点与处理建议`), renderAutoIssues(job.issues)));
    body.append(status);
    if (jobs.length > 1) body.append(el("details", { class: "auto-job-history" }, text("summary", `历史任务 · ${jobs.length - 1} 次`), ...jobs.slice(1).map(item => el("div", { class: "auto-history-row" }, text("span", `${autoStatusNames[item.status] || item.status} · ${(item.card_ids || []).length} 张 · ${formatDate(item.created_at)}`, "helper"), (item.card_ids || []).length ? button("查看卡片", () => showAutoCards(doc, item), "btn quiet small") : null))));
  } else if (app.autoLoading.has(doc.id)) body.append(text("p", "正在查询本材料的任务…", "helper"));
  return body;
}
function renderAutoCardQuality(card, draft) {
  const quality = card.auto_quality;
  if (!quality && !card.auto_job_id) return null;
  const stale = Boolean(draft.dirty || card.source_outdated || quality?.verdict === "stale" || (quality && quality.card_version !== card.version));
  const verdict = stale ? "stale" : quality?.verdict || "not_checked";
  const names = { clear: "文字检查未发现明显疑点", needs_review: "有疑点需要核对", stale: "检查结果已过期", not_checked: "尚未完成机器检查" };
  const result = el("section", { class: `auto-card-quality ${verdict}`, "data-auto-quality": "", "data-quality-verdict": verdict, "aria-label": "自动制卡质量检查" },
    el("div", { class: "auto-issue-heading" }, text("h3", "自动制卡检查"), badge(names[verdict] || names.not_checked, verdict === "clear" ? "green" : "amber")),
    text("p", stale ? "文字、评分要点、引用或配图已有变化。下方保留旧版检查供核对，不能作为当前版本已通过的依据。" : `检查对应卡片版本 ${quality?.card_version || card.version}。机器建议仍需对照讲义审核。`, "helper"));
  if (quality?.issues?.length) result.append(renderAutoIssues(quality.issues));
  if (card.image_result) result.append(text("p", `配图：${card.image_result.message || card.image_result.status || "待处理"}`, "helper"));
  result.append(button("重新检查已保存卡片", () => cardEditOperation(card, "quality", async () => {
    await api(`/api/cards/${encodeURIComponent(card.id)}/auto-quality-check`, jsonOptions("POST", { expected_version: card.version }));
    await refresh();
    toast("已重新检查当前卡片，请核对结果与原文。");
  }), "btn secondary small", Boolean(draft.dirty || app.cardEditBusy || card.source_outdated || !modeIsCloud())));
  if (draft.dirty) result.append(text("p", "先保存草稿，再检查修改后的内容。", "helper"));
  return result;
}
function demoButton(className = "btn secondary") {
  const btn = button("导入演示材料", () => actionWithButton(btn, "正在导入…", async () => {
    await api("/api/demo", { method: "POST" });
    app.selectedBlocks.clear();
    await refresh();
    app.documentId = app.data.documents.find(item => item.is_demo)?.id || app.documentId;
    render();
    toast("自编演示材料已导入，6 张卡片等待人工审核。");
  }), className);
  return btn;
}

function render() {
  if (!app.data) return;
  for (const nav of document.querySelectorAll("[data-view]")) {
    const active = nav.dataset.view === app.view;
    nav.classList.toggle("active", active);
    if (active) nav.setAttribute("aria-current", "page"); else nav.removeAttribute("aria-current");
  }
  $("#breadcrumb-current").textContent = viewNames[app.view];
  $("#mode-status").textContent = app.data.mode === "demo" ? "演示模式 · 本地规则反馈" : app.data.configured ? `云端模式 · ${app.data.model || "已配置模型"}` : "云端模式 · 待配置 API Key";
  const main = $("#main");
  const manualOpen = main.querySelector("#manual-tools")?.open;
  const oldGrid = main.querySelector(".workbench-grid");
  const scrollPositions = oldGrid && oldGrid.dataset.cardId === app.cardId ? ["source", "editor", "preview"].map(name => [name, oldGrid.querySelector(`.workbench-${name} > .panel-body`)?.scrollTop || 0]) : [];
  if (app.view !== "settings" && app.settingsPanel) {
    app.settingsPanel.querySelectorAll('input[type="password"]').forEach(input => { input.value = ""; });
    app.settingsPanel = null;
  }
  main.dataset.view = app.view;
  main.replaceChildren(({ materials: renderMaterials, cards: renderCards, review: renderReview, practice: renderPractice, export: renderExport, settings: renderModelSettings })[app.view]());
  if (manualOpen && main.querySelector("#manual-tools")) main.querySelector("#manual-tools").open = true;
  if (main.querySelector(".workbench-grid")?.dataset.cardId === oldGrid?.dataset.cardId) {
    for (const [name, position] of scrollPositions) {
      const body = main.querySelector(`.workbench-${name} > .panel-body`);
      if (body) body.scrollTop = position;
    }
  }
}

function renderModelSettings() {
  return app.settingsPanel ||= ModelSettings.create({ api, jsonOptions, onSaved: async () => { await refresh(); } });
}

function renderMaterials() {
  const page = el("div", {}, heading("01 / 学习材料", "导入讲义，卡牌自动生成", "选择文件后，系统自动处理，完成后直接显示卡牌。", [demoButton(), button("查看卡片  →", () => setView("cards"))]));
  page.append(renderAutomaticImport());
  if (app.data.documents.length) page.append(el("div", { class: "workspace-summary", "aria-label": "学习空间概览" }, ...[
    [app.data.documents.length, "份学习材料"],
    [app.data.cards.filter(card => card.status === "draft").length, "张卡片附有疑点"],
    [app.data.cards.filter(card => card.status === "approved").length, "张卡片可练习"]
  ].map(([count, label]) => el("div", { class: "summary-item" }, text("strong", String(count).padStart(2, "0")), text("span", label)))));
  page.append(el("div", { class: "flow" }, ...[
    ["01", "选择讲义", "PPT / PPTX / PDF"],
    ["02", "系统自动处理", "识别、制卡、检查与原页配图"],
    ["03", "直接得到卡牌", "阅读、练习或导出至 Anki"]
  ].map(([n, title, desc]) => el("div", { class: "flow-step" }, text("span", n, "step-number"), el("div", {}, text("strong", title), text("small", desc))))));
  const currentDocument = documentFor(app.documentId);
  const advanced = el("details", { id: "manual-tools", class: "manual-tools" }, el("summary", {}, "高级工具：原文校对、手动制卡与编辑（可选）"));
  if (currentDocument && sourcePagesFor(currentDocument.id).length) advanced.append(renderProofreader(currentDocument));
  const left = el("div", { class: "stack" }, renderImportPanel());
  if (app.data.documents.length) left.append(renderDocumentList());
  left.append(el("div", { class: "demo-card" }, text("span", "⌘", "demo-symbol"), el("div", {}, text("h3", "还没有准备好讲义？"), text("p", "先用自编的「栈与队列」示例走通流程。")), demoButton("btn secondary small")));
  const right = app.documentId ? renderSourcePanel() : panel("材料原文", "导入后，可按页选择用于生成卡片的片段", empty("让知识有一个可靠的起点", "选择 PDF 或粘贴课程原文。也可以先导入演示材料，体验栈与队列的学习流程。", demoButton(), "▤"), badge("来源可追溯", "green"));
  advanced.append(el("div", { class: "grid" }, left, right));
  page.append(advanced);
  return page;
}
function renderImportPanel() {
  const body = el("div");
  body.append(el("div", { class: "segmented", role: "group", "aria-label": "导入方式" }, ...[["pdf", "上传 PDF"], ["text", "粘贴文本"]].map(([key, label]) => {
    const option = button(label, () => { app.uploadTab = key; render(); }, app.uploadTab === key ? "active" : "");
    option.setAttribute("aria-pressed", String(app.uploadTab === key));
    return option;
  })));
  const errorNode = text("div", "", "inline-error");
  errorNode.setAttribute("role", "alert");
  if (app.uploadTab === "pdf") {
    const file = el("input", { type: "file", accept: "application/pdf,.pdf", required: true, "aria-label": "选择 PDF 讲义" });
    const start = el("input", { type: "number", min: "1", placeholder: "从第一页", "aria-label": "起始页" });
    const end = el("input", { type: "number", min: "1", placeholder: "到最后一页", "aria-label": "结束页" });
    const recognitionMode = el("select", { "aria-label": "PDF 识别方式" }, el("option", { value: "auto" }, "自动识别 · 扫描页使用 OCR"), el("option", { value: "ocr" }, "全部使用 OCR · 适合乱码或扫描件"), el("option", { value: "text" }, "仅提取 PDF 内置文字"));
    const form = el("form", {}, el("div", { class: "dropzone" }, text("div", "↥", "upload-symbol"), text("strong", "把课堂知识带进来"), text("p", "支持文字型与扫描型 PDF 讲义"), file), el("div", { class: "field-row" }, field("起始页（可选）", start), field("结束页（可选）", end)), field("识别方式", recognitionMode, "OCR 在本机识别印刷文字，导入后请对照页面校对。"), errorNode);
    const submit = el("button", { type: "submit", class: "btn full" }, "导入并提取原文  ↗");
    const importHint = text("p", "按 PDF 实际页序选择，封面也算一页。建议先导入 2–3 页；OCR 可能需要数秒到数分钟。", "helper");
    form.append(submit, importHint);
    form.addEventListener("submit", event => {
      event.preventDefault();
      actionWithButton(submit, "正在导入与识别，请稍候…", async () => {
        if (!file.files[0]) throw new Error("请先选择 PDF 文件。");
        if (start.value && end.value && Number(start.value) > Number(end.value)) throw new Error("结束页应大于或等于起始页。");
        const data = new FormData();
        data.append("file", file.files[0]);
        data.append("mode", recognitionMode.value);
        if (start.value) data.append("start_page", start.value);
        if (end.value) data.append("end_page", end.value);
        const result = await api("/api/documents", { method: "POST", body: data });
        app.documentId = result.id || result.document?.id || null;
        app.selectedBlocks.clear();
        await refresh();
        focusProofreader();
        toast("讲义已导入，请逐页对照原图，保存校对后再生成卡片。");
      }, errorNode);
    });
    body.append(form);
  } else {
    const title = el("input", { type: "text", maxlength: "150", required: true, placeholder: "例如：数据结构 · 栈与队列" });
    const content = el("textarea", { rows: "8", minlength: "20", maxlength: "50000", required: true, placeholder: "在这里粘贴课程讲义的原文（至少 20 字）。保留概念定义、适用条件和例子，有助于生成有依据的卡片。" });
    const form = el("form", {}, field("材料标题", title), field("课程原文", content), errorNode);
    const submit = el("button", { type: "submit", class: "btn full" }, "保存课程原文  ↗");
    form.append(submit, text("p", "文本材料使用逻辑页编号，来源会保留为你提供的标题。", "helper"));
    form.addEventListener("submit", event => {
      event.preventDefault();
      actionWithButton(submit, "正在保存…", async () => {
        const result = await api("/api/documents/text", jsonOptions("POST", { title: title.value.trim(), text: content.value.trim() }));
        app.documentId = result.id || result.document?.id || null;
        app.selectedBlocks.clear();
        await refresh();
        toast("课程原文已保存。");
      }, errorNode);
    });
    body.append(form);
  }
  return panel("导入课程材料", "建议先选一个小节，便于逐条核对", body);
}
function renderDocumentList() {
  return panel("已导入材料", `${app.data.documents.length} 份本地材料`, el("div", { class: "documents" }, ...app.data.documents.map(doc => button(el("span", { class: "document-info" }, text("strong", doc.title), text("span", `${doc.is_demo ? "自编演示 · " : ""}${blocksFor(doc.id).length} 个原文片段`, "meta")), () => { app.documentId = doc.id; app.selectedBlocks.clear(); render(); }, `document-row${doc.id === app.documentId ? " selected" : ""}`))));
}
async function updateSourcePage(doc, sourcePage, action) {
  if (app.proofBusy) return;
  const key = proofKey(doc, sourcePage);
  const draft = app.proofDrafts[key];
  if (action === "ocr" && draft) return;
  app.proofBusy = key;
  delete app.proofErrors[key];
  render();
  announce(action === "ocr" ? "正在本机重新识别本页。" : "正在保存本页校对。");
  try {
    const path = `/api/documents/${encodeURIComponent(doc.id)}/pages/${sourcePage.page}`;
    const result = await api(action === "ocr" ? `${path}/ocr` : path, jsonOptions(action === "ocr" ? "POST" : "PUT", action === "ocr" ? { revision: sourcePage.revision } : { revision: draft?.revision ?? sourcePage.revision, text: draft?.text ?? sourcePage.text }));
    delete app.proofDrafts[key];
    keepProofDrafts();
    app.selectedBlocks.clear();
    await refresh();
    const message = result.status === "failed" ? result.error || "本页识别未完成，可以重试或对照原图手动录入。" : action === "ocr" ? "重新识别完成，请核对新文字后保存。" : "本页校对已保存。原始识别文字与历史引用均已保留。";
    toast(message, result.status === "failed");
    announce(message);
  } catch (error) {
    app.proofErrors[key] = `${error.message}${error.status === 409 ? " 你的编辑内容仍然保留，请对照最新版本后继续。" : ""}`;
    if (error.status === 409) {
      try { await refresh(); } catch { /* Keep both the error and the local draft available for retry. */ }
    }
    announce(app.proofErrors[key]);
  } finally {
    app.proofBusy = null;
    render();
  }
}
async function calibrateSourcePage(doc, sourcePage, readOnly = false) {
  if (app.proofBusy || (!readOnly && app.proofDrafts[proofKey(doc, sourcePage)])) return;
  const key = proofKey(doc, sourcePage);
  app.proofBusy = key;
  delete app.proofErrors[key];
  render();
  try {
    const path = `/api/documents/${encodeURIComponent(doc.id)}/pages/${sourcePage.page}/calibration`;
    const previous = app.sourceCalibrations[`${key}:${sourcePage.revision}`];
    const report = await api(path, readOnly ? {} : jsonOptions("POST", { revision: sourcePage.revision, force: previous?.status === "needs_confirmation" }));
    app.sourceCalibrations[`${key}:${sourcePage.revision}`] = report;
    if (report.applied_revision) app.sourceCalibrations[`${key}:${report.applied_revision}`] = report;
    await refresh();
    if (!readOnly) toast(report.status === "auto_applied" ? "本页已完成机器核对，可继续自动制卡；请在卡片审核时检查原文。" : "校准结果有疑点，请查看差异并确认。注意：候选文字尚未覆盖原文。");
  } catch (error) { app.proofErrors[key] = error.message; }
  finally { app.proofBusy = null; render(); }
}
function renderSourceCalibration(doc, sourcePage, draft, busy) {
  if (!doc.file_url) return null;
  const key = proofKey(doc, sourcePage);
  const report = app.sourceCalibrations[`${key}:${sourcePage.revision}`];
  const section = el("section", { class: "source-calibration", "aria-label": "原页自动校准" }, text("h3", "原页自动校准"),
    text("p", "对照原始页面校准文字。点击会把本页原图发送给当前模型读取两次；疑点和关键内容变化会保留待确认。", "helper"),
    el("div", { class: "two-buttons" }, button(busy ? "正在处理…" : "对照原页自动校准（云端）", () => calibrateSourcePage(doc, sourcePage), "btn secondary small", busy || Boolean(draft) || sourcePageConfirmed(sourcePage) || !modeIsCloud()),
      button("查看校准差异", () => calibrateSourcePage(doc, sourcePage, true), "btn quiet small", busy)));
  section.querySelector("button").dataset.sourceCalibrate = "";
  if (sourcePage.status === "auto_verified") section.append(note("本页通过机器核对，可用于制卡。这个标记与人工校对分别记录，卡片仍需你审核。"));
  if (report && report.status !== "not_checked") {
    section.append(text("p", report.status === "auto_applied" ? "已自动采用并保存新来源版本。" : "候选文字等待确认，原始识别与历史版本保留。", "helper"));
    if (report.issues?.length) section.append(el("ul", {}, ...report.issues.map(reason => text("li", reason))));
    if (report.changes?.length) section.append(el("div", { class: "source-calibration-changes" }, ...report.changes.map(change => el("div", {}, text("del", change.before || "（新增）"), text("span", " → "), text("ins", change.after || "（删除）")))));
    if (report.proposed_text && report.status === "needs_confirmation") section.append(
      el("details", {}, text("summary", "查看完整候选文字"), text("p", report.proposed_text, "source-text")),
      button("将建议载入校对框", () => { app.proofDrafts[key] = { text: report.proposed_text, revision: sourcePage.revision, baseText: sourcePage.text }; keepProofDrafts(); render(); $("#proof-text")?.focus(); }, "btn secondary small", busy || Boolean(draft)));
  } else if (report) section.append(text("p", "此版本尚无校准结果。", "helper"));
  return section;
}
function renderProofreader(doc) {
  const pages = sourcePagesFor(doc.id);
  const sourcePage = pages.find(item => item.page === app.sourcePage[doc.id]) || pages.find(item => !sourcePageConfirmed(item)) || pages[0];
  app.sourcePage[doc.id] = sourcePage.page;
  const key = proofKey(doc, sourcePage);
  const draft = app.proofDrafts[key];
  const conflict = Boolean(draft && draft.revision !== sourcePage.revision);
  const busy = Boolean(app.proofBusy);
  const reviewed = pages.filter(sourcePageConfirmed).length;
  const machineCount = pages.filter(item => item.status === "auto_verified" && sourcePageConfirmed(item)).length;
  const body = el("div", { class: "proofreader-body" });
  const select = el("select", { id: "proof-page-selector", "aria-label": "选择校对页", disabled: busy }, ...pages.map(item => el("option", { value: item.page, selected: sourcePage.page === item.page }, `第 ${item.page} 页 · ${sourcePageStatus(item)}${app.proofDrafts[proofKey(doc, item)] ? " · 有未保存修改" : ""}`)));
  select.addEventListener("change", () => { app.sourcePage[doc.id] = Number(select.value); render(); $("#proof-page-selector")?.focus(); });
  const pageIndex = pages.indexOf(sourcePage);
  function turnPage(offset) { app.sourcePage[doc.id] = pages[pageIndex + offset].page; render(); $("#proof-page-selector")?.focus(); }
  const navigation = el("div", { class: "proof-page-navigation" }, button("← 上一页", () => turnPage(-1), "btn secondary small", busy || pageIndex === 0), select, button("下一页 →", () => turnPage(1), "btn secondary small", busy || pageIndex === pages.length - 1));
  body.append(el("div", { class: "proof-toolbar" }, navigation, el("div", { class: "proof-progress" }, text("span", `${reviewed} / ${pages.length} 页可制卡 · ${machineCount} 页机器核对`, "meta"), el("progress", { value: reviewed, max: pages.length, "aria-label": "页面校对进度" }))));
  if (sourcePage.error) body.append(note(sourcePage.error, "error"));
  if (app.proofErrors[key]) body.append(el("div", { class: "notice error", role: "alert" }, app.proofErrors[key]));
  if (conflict) body.append(el("div", { class: "proof-conflict" }, note(`此页已有更新版本 v${sourcePage.revision}。你的草稿基于 v${draft.revision}，请先对照，避免覆盖其他修改。`, "warning"), el("details", { open: "" }, text("summary", `查看最新已保存的文字 · v${sourcePage.revision}`), text("p", sourcePage.text || "（空白）", "source-text")), button("以此草稿继续校对新版本", () => { app.proofDrafts[key] = { text: draft.text, revision: sourcePage.revision, baseText: sourcePage.text }; keepProofDrafts(); delete app.proofErrors[key]; render(); }, "btn secondary small", busy)));
  const preview = sourcePage.image_url ? button("查看大图 ⤢", () => previewSourceImage(doc, sourcePage, preview), "btn quiet small") : null;
  const imageRegion = el("div", { class: "proof-original" }, el("div", { class: "proof-column-heading" }, text("h3", "PDF 原始页面"), preview));
  if (sourcePage.image_url) {
    const image = el("img", { src: sourcePage.image_url, alt: `${doc.title}，PDF 第 ${sourcePage.page} 页原图`, class: "proof-page-image", decoding: "async" });
    const frame = el("div", { class: "proof-image-frame" }, image);
    image.addEventListener("error", () => frame.replaceChildren(note("页面预览暂时无法加载，请打开原始 PDF 核对后重试。", "error")));
    imageRegion.append(frame);
  } else imageRegion.append(note("此页暂无图像预览，请打开原始文件对照。", "warning"));
  if (doc.file_url) imageRegion.append(el("a", { href: doc.file_url, target: "_blank", rel: "noopener noreferrer", class: "helper" }, "打开原始 PDF ↗"));
  const input = el("textarea", { id: "proof-text", class: "proof-text-input", rows: "13", maxlength: "50000", spellcheck: "false", disabled: busy, value: draft?.text ?? sourcePage.text ?? "", "aria-label": `第 ${sourcePage.page} 页校对文字`, placeholder: "对照原图修正文字。识别失败时也可手动补入本页原文；确认空白页时可留空。" });
  const dirtyNote = text("p", draft ? "有未保存修改 · 切换页面时保留在此标签页中" : "对照原图检查文字、符号与顺序，确认后再用于制卡。", "helper proof-draft-status");
  const save = button(busy && app.proofBusy === key ? "正在处理本页，请稍候…" : input.value.trim() ? "保存并确认校对 ✓" : "保存并确认空白 ✓", () => updateSourcePage(doc, sourcePage, "save"), "btn", busy || conflict);
  const reocr = button("重新 OCR 此页", () => updateSourcePage(doc, sourcePage, "ocr"), "btn secondary", busy || Boolean(draft));
  const discard = button("放弃修改，恢复已保存文字", () => { delete app.proofDrafts[key]; delete app.proofErrors[key]; keepProofDrafts(); render(); }, "btn quiet small", busy);
  discard.hidden = !draft;
  input.addEventListener("input", () => {
    if (input.value === (sourcePage.text || "") && !conflict) delete app.proofDrafts[key];
    else app.proofDrafts[key] = { text: input.value, revision: draft?.revision ?? sourcePage.revision, baseText: draft?.baseText ?? sourcePage.text };
    keepProofDrafts();
    const dirty = Boolean(app.proofDrafts[key]);
    dirtyNote.textContent = dirty ? "有未保存修改 · 切换页面时保留在此标签页中" : "对照原图检查文字、符号与顺序，确认后再用于制卡。";
    save.textContent = input.value.trim() ? "保存并确认校对 ✓" : "保存并确认空白 ✓";
    reocr.disabled = busy || dirty;
    const calibrate = body.querySelector("[data-source-calibrate]");
    if (calibrate) calibrate.disabled = busy || dirty || sourcePageConfirmed(sourcePage) || !modeIsCloud();
    discard.hidden = !dirty;
    updateAutoPanel(doc.id);
  });
  const statusColor = sourcePageConfirmed(sourcePage) ? "green" : sourcePage.status === "failed" ? "red" : "amber";
  const editor = el("div", { class: "proof-editor" }, el("div", { class: "proof-column-heading" }, text("h3", "校对后的原文"), badge(sourcePageStatus(sourcePage), statusColor)), field(`第 ${sourcePage.page} 页 · ${sourcePage.method === "ocr" ? "本地 OCR" : "PDF 内置文字"} · 版本 ${sourcePage.revision}`, input), dirtyNote, el("div", { class: "proof-actions" }, save, reocr), discard, text("p", "重新识别会保留旧版本，并将本页重新标记为待校对。已有修改请先保存或放弃。", "helper"));
  const calibrationPanel = renderSourceCalibration(doc, sourcePage, draft, busy);
  if (calibrationPanel) editor.append(calibrationPanel);
  editor.append(el("details", { class: "proof-raw" }, text("summary", "查看原始识别文字"), text("p", `${sourcePage.method === "ocr" ? "OCR" : "文字提取"}${sourcePage.engine ? ` · ${sourcePage.engine}` : ""}${sourcePage.engine_version ? ` ${sourcePage.engine_version}` : ""}`, "meta"), text("p", sourcePage.raw_text || "（未识别出文字）", "source-text")));
  body.append(el("div", { class: "proof-columns" }, imageRegion, editor));
  const proof = panel("逐页校对", `${doc.title} · 对照原图确认后，文字才可用于生成卡片`, body, badge("本机处理", "green"));
  proof.id = "source-proofreader";
  proof.classList.add("proofreader-panel");
  return proof;
}
function renderSourcePanel() {
  const doc = documentFor(app.documentId);
  const blocks = blocksFor(doc.id);
  const pages = sourcePagesFor(doc.id);
  const selectedCount = () => blocks.filter(b => blockCanGenerate(b) && app.selectedBlocks.has(b.block_id)).length;
  const body = el("div");
  body.append(renderAutoGenerationPanel(doc));
  if (!app.autoLoaded.has(doc.id) && !app.autoLoading.has(doc.id) && !app.autoErrors[doc.id]) void loadAutoJobs(doc.id);
  if (doc.is_demo) body.append(note("这是一份为验证流程而自编的「栈与队列」示例，不是真实课程讲义。演示卡片仍需人工审核后才能练习和导出。", "warning"));
  if (doc.warning && !doc.is_demo) body.append(note(doc.warning, "warning"));
  if (pages.length) body.append(el("div", { class: "source-review-summary" }, text("span", "人工或机器核对的来源可用于制卡；有疑点的页面等待确认。修正原文后，相关卡片需要重新审核。", "helper"), button("前往逐页校对 ↑", focusProofreader, "btn quiet small")));
  const selectionCount = text("span", `已选 ${selectedCount()} 个片段 · 每次最多 6 个`, "helper");
  const generate = button("生成 3 张卡片草稿  →", () => actionWithButton(generate, "正在生成草稿…", async () => {
    const ids = blocks.filter(b => blockCanGenerate(b) && app.selectedBlocks.has(b.block_id)).map(b => b.block_id);
    if (!ids.length) throw new Error("请先选择至少一个原文片段。");
    const result = await api("/api/card-drafts", jsonOptions("POST", { block_ids: ids, count: 3 }));
    const created = result.cards || (Array.isArray(result) ? result : []);
    app.cardId = created[0]?.id || null;
    app.reviewDocument = created[0]?.document_id || doc.id;
    app.reviewFilter = "all";
    app.view = "review";
    await refresh();
    toast("草稿已生成，请核对问题、评分要点与原文依据。");
  }), "btn small", !modeIsCloud() || !selectedCount());
  body.append(el("div", { class: "source-tools source-selection" }, selectionCount, generate));
  if (!modeIsCloud()) body.append(text("p", "真实材料生成需要配置后端 API Key，并开启云端模式。当前可以导入材料，或使用演示卡片审核和练习。", "helper"));
  else body.append(text("p", "选中的原文将发送给当前配置的模型服务。生成结果是草稿，需要你逐条核对。", "helper"));
  body.append(el("hr", { class: "section-divider" }));
  const sources = el("div", { class: "source-scroll" });
  if (!blocks.length) sources.append(note(pages.some(item => item.status === "failed") ? "暂时没有可用原文。请到逐页校对区重试识别，或对照原图手动补入文字。" : "此材料当前没有文字片段。可以到逐页校对区检查页面。"));
  for (const block of blocks) {
    const eligible = blockCanGenerate(block);
    const sourcePage = pages.find(item => item.page === block.page);
    const check = el("input", { type: "checkbox", checked: eligible && app.selectedBlocks.has(block.block_id), disabled: !eligible, "aria-label": `选择${sourceLabel(block)} ${block.block_id}${eligible ? "" : "，请先校对"}` });
    check.addEventListener("change", () => {
      if (check.checked) {
        if (app.selectedBlocks.size >= 6) { check.checked = false; toast("每次最多选择 6 个片段。"); return; }
        app.selectedBlocks.add(block.block_id);
      } else app.selectedBlocks.delete(block.block_id);
      selectionCount.textContent = `已选 ${selectedCount()} 个片段 · 每次最多 6 个`;
      generate.disabled = !modeIsCloud() || !selectedCount();
      check.closest(".source-block").classList.toggle("selected", check.checked);
    });
    sources.append(el("article", { class: `source-block${check.checked ? " selected" : ""}${eligible ? "" : " source-unreviewed"}` }, el("div", { class: "source-top" }, el("label", {}, check, sourceLabel(block), sourcePage ? badge(sourcePageStatus(sourcePage), eligible ? "green" : "amber") : null), text("span", block.block_id, "source-id")), text("p", block.text, "source-text"), !eligible ? button("校对此页 →", () => { app.sourcePage[doc.id] = block.page; render(); focusProofreader(); }, "btn quiet small") : null));
  }
  body.append(sources);
  const result = panel(doc.title, `${doc.filename || "文本材料"} · ${blocks.length} 个当前原文片段`, body, badge(doc.is_demo ? "自编演示" : "本地材料", doc.is_demo ? "amber" : "green"));
  result.id = "material-source-panel";
  return result;
}

function renderReview() {
  const all = app.data.cards;
  const approved = all.filter(c => c.status === "approved").length;
  const page = el("div", { class: "card-workspace" }, heading("02 / 制卡工作台", "把讲义，做成看得懂的卡片", "左边核对讲义，中间编辑内容，右边预览正反面。保存并批准后，就可以开始学习。", [button("导入讲义", () => setView("materials"), "btn secondary"), button("前往练习  →", () => setView("practice"), "btn", !approved)]));
  if (!all.length) { page.append(panel("等待第一张卡片", "先从材料开始", empty("从一小节讲义开始制卡", "在学习材料中选取原文生成草稿，或先用演示卡片体验编辑与配图。", demoButton()))); return page; }
  const course = el("select", { "aria-label": "筛选课程" }, el("option", { value: "all" }, "全部课程"), ...app.data.documents.map(doc => el("option", { value: doc.id, selected: doc.id === app.reviewDocument }, doc.title)));
  course.addEventListener("change", () => { app.reviewDocument = course.value; render(); });
  const courseCards = all.filter(c => !app.reviewDocument || app.reviewDocument === "all" || c.document_id === app.reviewDocument);
  const filter = el("select", { "aria-label": "筛选卡片状态" }, ...[["all", "全部卡片"], ["draft", "待审核"], ["approved", "已批准"], ["rejected", "已拒绝"]].map(([value, label]) => el("option", { value, selected: value === app.reviewFilter }, `${label} · ${value === "all" ? courseCards.length : courseCards.filter(c => c.status === value).length}`)));
  filter.addEventListener("change", () => { app.reviewFilter = filter.value; render(); });
  const cards = courseCards.filter(c => app.reviewFilter === "all" || c.status === app.reviewFilter);
  const card = cards.find(c => c.id === app.cardId) || cards[0];
  app.cardId = card?.id || null;
  const selector = el("select", { "aria-label": "选择编辑卡片", disabled: !cards.length }, ...cards.map((c, i) => el("option", { value: c.id, selected: c.id === card?.id }, `${i + 1}. ${app.cardDrafts[c.id]?.dirty ? "[未保存] " : ""}${c.question}`)));
  selector.addEventListener("change", () => { app.cardId = selector.value; render(); });
  const index = cards.indexOf(card);
  const step = offset => { app.cardId = cards[index + offset]?.id || card?.id; render(); };
  page.append(el("div", { class: "workbench-toolbar" }, field("课程", course), field("状态", filter), field("当前卡片", selector), el("div", { class: "workbench-pager" }, button("← 前一张", () => step(-1), "btn secondary small", index <= 0), text("span", `${index + 1} / ${cards.length}`, "meta"), button("后一张 →", () => step(1), "btn secondary small", index < 0 || index >= cards.length - 1))));
  const mobileTabs = el("div", { class: "workbench-mobile-tabs", "aria-label": "工作台区域" });
  for (const [pane, label] of [["source", "查看讲义"], ["editor", "编辑内容"], ["preview", "卡片预览"]]) {
    const tab = button(label, () => { app.reviewPane = pane; render(); }, "btn secondary small");
    tab.setAttribute("aria-pressed", String((app.reviewPane || "editor") === pane));
    mobileTabs.append(tab);
  }
  if (card) page.append(mobileTabs);
  page.append(card ? renderCardEditor(card) : panel("暂无此状态卡片", null, empty("这里暂时没有卡片", "可以切换课程或状态筛选，查看其他卡片。")));
  return page;
}
function cardEditDraft(card) {
  const previous = app.cardDrafts[card.id];
  if (previous && !previous.dirty && previous.version !== card.version) delete app.cardDrafts[card.id];
  return app.cardDrafts[card.id] ||= { question: card.question, answer: card.answer, concept: card.concept, kind: card.kind, rubric: (card.rubric || []).map(point => ({ ...point })), evidence: JSON.stringify(card.evidence || [], null, 2), images: CardImages.refs(card), dirty: false, version: card.version };
}
async function cardEditOperation(card, operation, callback) {
  if (app.cardEditBusy) return;
  app.cardEditBusy = { cardId: card.id, operation };
  delete app.cardEditErrors[card.id];
  render();
  try { await callback(); }
  catch (error) {
    app.cardEditErrors[card.id] = `${error.message} 未保存的编辑内容仍然保留。`;
    if (error.status === 409) (app.cardEditConflicts ||= {})[card.id] = true;
  }
  finally { app.cardEditBusy = null; render(); }
}
function renderCardImageEditor(card, draft) {
  const busy = Boolean(app.cardEditBusy);
  const full = draft.images.length >= 4;
  const section = el("section", { class: "card-image-editor", "aria-label": "卡片配图" });
  const pages = sourcePagesFor(card.document_id).filter(page => page.image_url);
  const markChanged = () => { draft.dirty = true; delete app.cardEditErrors[card.id]; render(); };
  const addMedia = (record, side = "answer") => {
    if (!record?.id) throw new Error("服务没有返回可用的图片记录，请重试。");
    app.data.media ||= [];
    if (!app.data.media.some(item => item.id === record.id)) app.data.media.push(record);
    if (draft.images.some(item => item.media_id === record.id)) throw new Error("此图片已在当前卡片中，请调整已有图片。");
    if (draft.images.length >= 4) throw new Error("每张卡片最多附加 4 张图片。");
    draft.images.push({ media_id: record.id, side, caption: record.source?.origin === "figure_recommendation" ? `讲义第 ${record.source.page} 页` : "" });
    draft.dirty = true;
    toast("配图已加入待保存内容。保存草稿或核对并批准后，才会附加到卡片。");
  };
  const upload = el("input", { type: "file", accept: "image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp", disabled: busy || full, "aria-label": "上传讲义截图或图片" });
  upload.addEventListener("change", () => {
    const file = upload.files?.[0];
    if (!file || app.cardEditBusy || full) return;
    cardEditOperation(card, "upload", async () => {
      if (file.size > 5 * 1024 * 1024) throw new Error("图片不能超过 5 MiB。");
      if (file.type && !["image/png", "image/jpeg", "image/webp"].includes(file.type)) throw new Error("请选择 PNG、JPEG 或 WebP 图片。");
      const body = new FormData();
      body.append("file", file);
      addMedia(await api("/api/card-media", { method: "POST", body }));
    });
  });
  section.append(el("div", { class: "card-image-heading" }, text("h3", "讲义配图"), badge(`${draft.images.length} / 4 张`)), text("p", "先从引用页推荐配图，也可上传截图。新增图片默认放在答案侧；整页讲义可能包含答案，请谨慎改到题目侧。", "helper"));
  if (pages.length) section.append(FigureRecommendations.create({ card, draft, api, busy, media: app.data.media, vision: app.data.image_check,
    run: callback => cardEditOperation(card, "upload", callback), onAdopt: addMedia }));
  const controls = el("div", { class: "card-image-controls" }, field("上传截图", upload, "PNG / JPEG / WebP，每张最多 5 MiB；上传时保存到本机，主动使用云端检查时才发送图片。"));
  if (pages.length) {
    const selectedPage = pages.find(page => page.page === draft.imagePage) || pages[0];
    const pageSelector = el("select", { disabled: busy || full, "aria-label": "选择本材料 PDF 原图页" }, ...pages.map(page => el("option", { value: String(page.page), selected: page.page === selectedPage.page }, `PDF 第 ${page.page} 页 · 修订 ${page.revision}`)));
    pageSelector.addEventListener("change", () => { draft.imagePage = Number(pageSelector.value); });
    const addPage = button("附加本页原图", () => {
      if (app.cardEditBusy || draft.images.length >= 4) return;
      const source = pages.find(page => page.page === Number(pageSelector.value));
      if (!source) return;
      cardEditOperation(card, "upload", async () => addMedia(await api("/api/card-media/from-page", jsonOptions("POST", { document_id: card.document_id, page: source.page, revision: source.revision }))));
    }, "btn secondary small", busy || full);
    controls.append(field("从本材料附加原图", pageSelector), addPage);
  } else controls.append(text("p", "此材料暂无 PDF 页面原图，可上传讲义截图。", "helper"));
  section.append(controls);
  if (app.cardEditBusy?.cardId === card.id && app.cardEditBusy.operation === "upload") section.append(text("p", "正在准备配图，请稍候…", "helper"));
  const list = el("div", { class: "card-image-list" });
  draft.images.forEach((reference, index) => {
    const imageSource = CardImages.mediaFor(reference, app.data.media)?.source;
    const provenance = imageSource?.page ? text("p", `来源：PDF 第 ${imageSource.page} 页 · 校对版本 ${imageSource.revision}${imageSource.bbox ? " · 已记录裁剪范围" : " · 整页原图"}`, "helper") : null;
    const side = el("select", { disabled: busy, "aria-label": `第 ${index + 1} 张配图显示位置` }, el("option", { value: "answer", selected: reference.side === "answer" }, "答案侧 · 分析完成后查看"), el("option", { value: "question", selected: reference.side === "question" }, "题目侧 · 作答前可见"));
    side.addEventListener("change", () => { reference.side = side.value; markChanged(); toast("图片显示位置已改变。原位置的检查结论不再适用，请按新位置检查是否泄露答案。"); });
    const caption = el("input", { type: "text", maxlength: "500", value: reference.caption, disabled: busy, placeholder: "例如：入栈操作示意图", "aria-label": `第 ${index + 1} 张配图说明` });
    caption.addEventListener("input", () => { reference.caption = caption.value; touchCardDraft(card, draft, false); });
    const move = step => { const other = index + step; [draft.images[index], draft.images[other]] = [draft.images[other], draft.images[index]]; markChanged(); };
    list.append(el("article", { class: "card-image-item" }, CardImages.thumbnail(reference, app.data.media, `配图 ${index + 1}`), el("div", { class: "card-image-options" }, provenance, field(`配图 ${index + 1} · 显示位置`, side), field("图片说明（可选）", caption), el("div", { class: "card-image-actions" }, button("上移", () => move(-1), "btn secondary small", busy || index === 0), button("下移", () => move(1), "btn secondary small", busy || index === draft.images.length - 1), button("移除", () => { draft.images.splice(index, 1); markChanged(); }, "btn danger small", busy)))));
  });
  section.append(list, text("p", "上传只是准备配图。请点击「保存草稿」或「核对并批准」，将当前文字、配图位置与说明一起保存到卡片。", "helper card-image-save-note"));
  if (draft.images.length) section.append(text("p", "答题诊断仍依据文字和评分要点。配图可在推荐区单独进行视觉检查；请把解题所需的图中条件写入问题，并把判断依据写入评分要点。", "notice card-image-ai-note"));
  return section;
}
function renderCardEditor(card) {
  const draft = cardEditDraft(card);
  const busy = Boolean(app.cardEditBusy);
  const question = el("textarea", { rows: "3", required: true, maxlength: "2000", value: draft.question, disabled: busy });
  const answer = el("textarea", { rows: "4", required: true, maxlength: "4000", value: draft.answer, disabled: busy });
  const concept = el("input", { required: true, maxlength: "100", value: draft.concept, disabled: busy });
  const kind = el("select", { disabled: busy }, el("option", { value: "basic", selected: draft.kind === "basic" }, "基础卡片"), el("option", { value: "remediation", selected: draft.kind === "remediation" }, "补充练习"));
  for (const [key, input] of Object.entries({ question, answer, concept, kind })) {
    const remember = () => { draft[key] = input.value; touchCardDraft(card, draft); };
    input.addEventListener("input", remember);
    input.addEventListener("change", remember);
  }
  const errorNode = text("div", app.cardEditErrors[card.id] || "", "inline-error");
  errorNode.setAttribute("role", "alert");
  const form = el("form", { class: "workbench-form" }, card.is_demo ? text("p", "自编演示卡片 · 请核对讲义后批准", "helper") : null, field("问题", question), field("参考答案", answer), el("div", { class: "field-row" }, field("知识概念", concept), field("卡片用途", kind)), renderCardImageEditor(card, draft), renderRubricFields(card, draft, busy), renderEvidenceFields(card, draft, busy));
  const autoQuality = renderAutoCardQuality(card, draft);
  if (autoQuality) {
    form.prepend(autoQuality);
    const updateQuality = () => form.querySelector("[data-auto-quality]")?.replaceWith(renderAutoCardQuality(card, draft));
    form.addEventListener("input", updateQuality);
    form.addEventListener("change", updateQuality);
  }
  if (app.cardEditConflicts?.[card.id]) form.prepend(el("div", { class: "notice warning" }, text("p", "保存版本冲突。请先保留你需要的修改，再载入最新版本重新编辑。"), button("载入最新版本", () => {
    if (!window.confirm("载入最新版本将替换这张卡片当前未保存的编辑。请先复制保留需要的内容，是否继续？")) return;
    cardEditOperation(card, "reload", async () => {
      const latest = await api("/api/state");
      if (!latest.cards?.some(item => item.id === card.id)) throw new Error("这张卡片已不可用，请返回学习材料检查。");
      app.data = latest;
      delete app.cardDrafts[card.id];
      delete app.cardEditConflicts[card.id];
      toast("已载入最新保存的版本。");
    });
  }, "btn secondary small", busy)));
  if (card.source_outdated) form.prepend(note("这张卡片引用的原文已更新，已退回待审核。请对照最新原文更新答案与引用，或重新生成卡片。旧引用与历史练习仍然保留。", "warning"));
  if (draft.version !== card.version) form.prepend(note("卡片已有新版本。你的草稿仍保留，保存时会校验版本，避免覆盖其他窗口的修改。", "warning"));
  form.addEventListener("submit", event => event.preventDefault());
  const controls = el("div", { class: "form-actions workbench-save-actions" });
  for (const [status, label, style] of [["draft", "保存草稿", "btn secondary"], ["rejected", "拒绝", "btn danger"], ["approved", "核对并批准", "btn"]]) {
    const btn = button(label, () => {
      if (app.cardEditBusy || !form.reportValidity()) return;
      cardEditOperation(card, "save", async () => {
        const points = draft.rubric.map((point, index) => {
          if (!point.text.trim()) throw new Error(`请填写第 ${index + 1} 条评分要点。`);
          return { point_id: point.point_id, text: point.text.trim() };
        });
        if (!points.length) throw new Error("至少保留一个评分要点。");
        if (new Set(points.map(p => p.point_id)).size !== points.length) throw new Error("评分要点编号不能重复。");
        let refs;
        try { refs = JSON.parse(draft.evidence); } catch { throw new Error("原文引用格式不正确，请从左侧重新加入引用。"); }
        if (!Array.isArray(refs) || !refs.length) throw new Error("原文引用应为至少包含一项的 JSON 数组。");
        const payload = { question: draft.question.trim(), answer: draft.answer.trim(), concept: draft.concept.trim(), kind: draft.kind, rubric: points, evidence: refs, images: draft.images.map(image => ({ ...image })), status, expected_version: draft.version };
        const saved = await api(`/api/cards/${encodeURIComponent(card.id)}`, jsonOptions("PUT", payload));
        const savedIndex = app.data.cards.findIndex(item => item.id === card.id);
        if (savedIndex >= 0) app.data.cards[savedIndex] = saved;
        const previewSide = draft.previewSide;
        const sourcePage = draft.sourcePage;
        delete app.cardDrafts[card.id];
        if (app.cardEditConflicts) delete app.cardEditConflicts[card.id];
        Object.assign(cardEditDraft(saved), { previewSide, sourcePage });
        toast(saved.status === "approved" ? "卡片已批准，可以练习和导出。" : saved.status === "rejected" ? "卡片已标记为拒绝。" : status === "approved" ? "新配图已保存，卡片已退回待审核。请核对后再次批准。" : "卡片草稿已保存。");
      });
    }, style, busy);
    controls.append(btn);
  }
  form.append(el("div", { class: "workbench-save-bar" }, text("p", draft.dirty ? "有未保存的修改" : `已保存 · 版本 ${card.version}`, "helper"), errorNode, controls));
  form.querySelector(".workbench-save-bar .helper").id = "workbench-save-status";
  if (app.cardEditBusy?.cardId === card.id && app.cardEditBusy.operation === "save") form.append(text("p", "正在保存卡片与配图…", "helper"));
  const editor = panel("编辑卡片", `版本 ${card.version || 1} · ${statusNames[card.status]}`, form);
  editor.classList.add("workbench-editor");
  return el("div", { class: "workbench-grid", "data-card-id": card.id, "data-pane": app.reviewPane || "editor" }, renderWorkbenchSources(card, draft), editor, renderWorkbenchPreview(card, draft));
}

function persistPracticeDrafts() {
  try { sessionStorage.setItem(practiceStorageKey, JSON.stringify(app.practiceDrafts)); } catch { /* Keep drafts in memory if browser storage is unavailable. */ }
}
function practiceDraft(card) {
  const draft = app.practiceDrafts[PracticeProgress.draftKey(card)];
  return typeof draft?.answer === "string" && draft.answer.length <= 4000 ? draft : null;
}
function savePracticeDraft(card, answer) {
  app.practiceDrafts[PracticeProgress.draftKey(card)] = { answer };
  persistPracticeDrafts();
}
function rememberAttempt(attempt) {
  const index = app.data.attempts.findIndex(item => item.id === attempt.id);
  if (index < 0) app.data.attempts.push(attempt); else app.data.attempts[index] = attempt;
}
function loadPractice(id, attempt = null) {
  const card = app.data.cards.find(item => item.id === id && item.status === "approved");
  app.practiceId = id;
  app.practiceLoadedKey = card ? PracticeProgress.draftKey(card) : null;
  const draft = card && practiceDraft(card);
  const latest = card ? PracticeProgress.getProgress([card], app.data.attempts).get(id)?.latestAttempt : null;
  app.attempt = attempt || (draft ? null : latest) || null;
  app.answer = attempt?.answer ?? draft?.answer ?? latest?.answer ?? "";
}
function focusPracticeQuestion() {
  const target = $("#practice-question");
  if (!target) return;
  target.focus({ preventScroll: true });
  const bounds = target.getBoundingClientRect();
  if (bounds.top < 0 || bounds.bottom > window.innerHeight) target.scrollIntoView({ block: "start", behavior: "instant" });
}
function selectPractice(id, attempt = null) {
  if (app.busy) return;
  if (!attempt) {
    const approved = app.data.cards.filter(card => card.status === "approved");
    if (!PracticeProgress.filterCards(approved, PracticeProgress.getProgress(approved, app.data.attempts), app.practiceFilter).some(card => card.id === id)) app.practiceFilter = "all";
  }
  loadPractice(id, attempt);
  app.view = "practice";
  render();
  if (attempt) showFeedback(); else focusPracticeQuestion();
}
function changePracticeFilter(filter) {
  if (app.busy) return;
  app.practiceFilter = filter;
  const approved = app.data.cards.filter(card => card.status === "approved");
  const cards = PracticeProgress.filterCards(approved, PracticeProgress.getProgress(approved, app.data.attempts), filter);
  loadPractice(cards.find(card => card.id === app.practiceId)?.id || cards[0]?.id || null);
  render();
  $(`[data-practice-filter="${filter}"]`)?.focus({ preventScroll: true });
  announce(`${practiceFilterNames[filter]}，${cards.length} 张卡片。`);
}
function renderPracticeOverview(cards, progress) {
  const completed = cards.filter(card => progress.get(card.id)?.completed).length;
  const counts = { all: cards.length, uncompleted: cards.length - completed, completed };
  const tabs = el("div", { class: "practice-filter-tabs", role: "group", "aria-label": "按完成状态筛选" }, ...Object.entries(practiceFilterNames).map(([key, label]) => {
    const btn = button([text("span", label), text("span", counts[key], "filter-count")], () => changePracticeFilter(key), "practice-filter", app.busy);
    btn.setAttribute("aria-pressed", String(app.practiceFilter === key));
    btn.setAttribute("data-practice-filter", key);
    btn.setAttribute("aria-label", `${label} ${counts[key]} 张`);
    return btn;
  }));
  return el("section", { class: "practice-overview", "aria-label": "练习进度" },
    el("div", { class: "practice-overview-top" }, el("div", {}, text("h2", "练习进度"), text("p", "一步一步，完成这一组回忆。")), el("div", { class: "practice-progress-count" }, text("strong", completed), ` / ${cards.length} 张已完成`)),
    el("progress", { class: "practice-progress-bar", value: completed, max: Math.max(1, cards.length), "aria-label": `已完成 ${completed} 张，共 ${cards.length} 张` }),
    el("div", { class: "practice-overview-bottom" }, tabs, text("p", "按当前卡片版本统计。收到分析结果即完成，完成不代表答对。", "helper")));
}
function renderRecentAttempts() {
  const recent = app.data.attempts.slice().reverse().slice(0, 5);
  if (!recent.length) return null;
  return panel("最近练习", "题目与原文保留提交时的版本", el("div", { class: "attempt-list" }, ...recent.map(attempt => button(el("div", {}, text("p", attempt.card_snapshot?.question || app.data.cards.find(c => c.id === attempt.card_id)?.question || "练习记录"), el("div", { class: "two-buttons" }, badge(attempt.status === "completed" ? verdictNames[attempt.diagnosis?.verdict] || "已分析" : { pending: "分析中", failed: "分析失败", unanswered: "未作答" }[attempt.status] || attempt.status, attempt.status === "failed" ? "red" : verdictColors[attempt.diagnosis?.verdict] || ""), text("small", formatDate(attempt.created_at)))), () => selectPractice(attempt.card_id, attempt), "attempt-item", app.busy))));
}
function renderPractice() {
  const approved = app.data.cards.filter(c => c.status === "approved");
  const progress = PracticeProgress.getProgress(approved, app.data.attempts);
  const filtered = PracticeProgress.filterCards(approved, progress, app.practiceFilter);
  const completedCount = approved.filter(card => progress.get(card.id)?.completed).length;
  const page = el("div", {}, heading("03 / 主动回忆", "把记住的，变成理解的", "先用自己的话回答，再对照反馈和讲义，补上遗漏的知识点。"));
  page.append(renderPracticeOverview(approved, progress));
  if (!approved.length && !app.attempt) {
    page.append(panel("练习空间", null, empty("先批准一张你信任的卡片", "到卡片审核页对照原文核对答案。原文更新后，相关卡片也需要重新审核。", button("去审核卡片  →", () => setView("review")), "◎")));
    const history = renderRecentAttempts();
    if (history) { history.classList.add("practice-history-only"); page.append(history); }
    return page;
  }
  if (approved.length && completedCount === approved.length) page.append(el("div", { class: "practice-complete-banner", role: "status" }, el("div", {}, text("strong", "本组练习已完成 ✓"), text("p", "每张卡片都已收到分析结果。可以回看反馈，继续巩固需要修正的地方。", "helper")), app.practiceFilter !== "completed" ? button("查看已完成卡片", () => changePracticeFilter("completed"), "btn secondary small", app.busy) : null));
  if (!filtered.length && !app.attempt) {
    page.append(panel(practiceFilterNames[app.practiceFilter], null, empty(app.practiceFilter === "completed" ? "还没有已完成的卡片" : "当前没有未完成的卡片", app.practiceFilter === "completed" ? "提交非空答案并收到分析结果后，卡片会出现在这里。" : "可以切换到已完成，回看反馈或再次作答。", button(app.practiceFilter === "completed" ? "去做未完成卡片 →" : "回看已完成卡片 →", () => changePracticeFilter(app.practiceFilter === "completed" ? "uncompleted" : "completed")), "✓")));
    const history = renderRecentAttempts();
    if (history) { history.classList.add("practice-history-only"); page.append(history); }
    return page;
  }
  if (!app.attempt && !filtered.some(card => card.id === app.practiceId)) loadPractice(filtered[0]?.id || null);
  const selected = approved.find(card => card.id === app.practiceId);
  if (!app.attempt && selected && app.practiceLoadedKey !== PracticeProgress.draftKey(selected)) loadPractice(selected.id);
  let card = app.attempt?.card_snapshot || approved.find(c => c.id === app.practiceId);
  if (!card) { loadPractice(filtered[0]?.id); card = filtered[0]; }
  app.practiceId = card.id || app.attempt?.card_id;
  const currentCard = approved.find(c => c.id === app.practiceId);
  const historicalVersion = Boolean(app.attempt && (!currentCard || currentCard.version !== card.version));
  const currentProgress = currentCard && progress.get(currentCard.id);
  const options = filtered.map(c => el("option", { value: c.id, selected: c.id === app.practiceId }, c.id === app.practiceId && historicalVersion ? `${card.question}（历史版本）` : `${progress.get(c.id)?.completed ? "已完成" : "未完成"} · ${c.kind === "remediation" ? "补练 · " : ""}${c.question}`));
  const outsideFilter = !filtered.some(c => c.id === app.practiceId);
  if (outsideFilter && app.attempt) options.unshift(el("option", { value: app.practiceId, selected: true }, `${card.question}（${historicalVersion ? "历史记录" : "本次反馈"}）`));
  const selector = el("select", { "aria-label": "选择练习卡片", disabled: app.busy }, ...options);
  selector.addEventListener("change", () => selectPractice(selector.value));
  const answer = el("textarea", { id: "practice-answer", class: "practice-answer", maxlength: "4000", placeholder: "写下你的理解。可以是一句定义，也可以是你自己的例子……", value: app.answer, disabled: app.busy || historicalVersion, "aria-label": "你的回答" });
  const answerCount = text("span", `${app.answer.length} / 4000`, "answer-count");
  const draftNote = text("p", currentCard && practiceDraft(currentCard) ? "未提交的草稿已保留在当前标签页，切换卡片或刷新后可继续。" : "切换卡片时会保留未提交的草稿。", "helper practice-draft-note");
  answer.addEventListener("input", () => {
    app.answer = answer.value;
    answerCount.textContent = `${answer.value.length} / 4000`;
    if (currentCard && !historicalVersion) savePracticeDraft(currentCard, answer.value);
    draftNote.textContent = "未提交的草稿已保留在当前标签页，切换卡片或刷新后可继续。";
    // Editing starts a new attempt. A previous completion cannot reveal its answer images.
    if (app.attempt) {
      app.attempt = null;
      // Replace only feedback: keep the textarea, selection and IME composition intact.
      const waiting = panel("新的回答尚未提交", "提交后查看本次反馈与参考答案", text("p", "你的修改已保留。重新提交并完成分析后，即可查看对应的参考答案与配图。", "helper"));
      waiting.classList.add("feedback-panel");
      $("#practice-feedback")?.replaceChildren(...[waiting, renderRecentAttempts()].filter(Boolean));
    }
  });
  const errorNode = text("div", "", "inline-error");
  errorNode.setAttribute("role", "alert");
  const status = el("div", { class: "practice-card-status" }, badge(historicalVersion ? "历史版本" : currentProgress?.completed ? "已完成" : "未完成", historicalVersion ? "amber" : currentProgress?.completed ? "green" : "blue"));
  if (currentProgress?.completedAttempt && !historicalVersion) status.append(text("span", `最近一次完成：${verdictNames[currentProgress.completedAttempt.diagnosis.verdict]}`, "helper"));
  const imageCard = app.attempt ? app.attempt.card_snapshot : card;
  const form = el("form", {}, field("当前练习", selector), status, el("div", { class: "two-buttons" }, badge(card.concept, "green"), badge(card.kind === "remediation" ? "补充练习" : "基础卡片", "blue")), el("h2", { id: "practice-question", tabindex: "-1", class: "practice-question" }, card.question), CardImages.gallery(CardImages.questionImages(card, app.attempt), app.data.media, "题目配图"), CardImages.refs(imageCard).length ? text("p", "配图仅供本机查看，AI 诊断不会理解图片。作答与评分以题目文字、原文和评分要点为依据；答案侧配图在有效分析完成后的参考答案中查看。", "helper card-image-ai-note") : null, field("你的回答", answer), el("div", { class: "answer-toolbar" }, text("span", "不必照抄原句，表达清楚就好。"), answerCount), !historicalVersion ? draftNote : null, errorNode);
  const justCompleted = outsideFilter && !historicalVersion && app.practiceFilter === "uncompleted" && currentProgress?.completed;
  if (outsideFilter && !historicalVersion) form.append(text("p", justCompleted ? "这张卡片已移入「已完成」。先看完反馈，再继续下一张。" : "正在查看当前分类以外的练习记录。可切换分类或继续下一张。", "practice-selection-note"));
  if (historicalVersion) form.append(note(currentCard ? "正在查看提交时的题目版本。点击「重新作答」，可使用当前已批准的版本。" : "这张卡片目前未批准，历史回答与反馈仍可查看。", "warning"));
  const submit = el("button", { type: "submit", class: "btn", disabled: app.busy || !currentCard || historicalVersion }, app.busy ? "正在分析…" : "提交回答并分析  →");
  const reset = button("重新作答", () => { savePracticeDraft(currentCard, ""); loadPractice(currentCard.id); if (!filtered.some(item => item.id === currentCard.id)) app.practiceFilter = "all"; render(); $("#practice-answer")?.focus(); }, "btn secondary", app.busy || !currentCard);
  const currentIndex = filtered.findIndex(item => item.id === app.practiceId);
  const previousCard = currentIndex > 0 ? filtered[currentIndex - 1] : null;
  const nextCard = PracticeProgress.nextCard(approved, progress, app.practiceFilter, app.practiceId);
  const next = button("下一张 →", () => { if (nextCard) { selectPractice(nextCard.id); announce(`已切换到下一张：${nextCard.question}`); } }, "btn", app.busy || !nextCard);
  const position = currentIndex >= 0 ? `${practiceFilterNames[app.practiceFilter]} · 第 ${currentIndex + 1} / ${filtered.length} 张${!nextCard ? " · 已到最后一张" : ""}` : justCompleted ? `未完成还剩 ${filtered.length} 张` : `${practiceFilterNames[app.practiceFilter]} ${filtered.length} 张 · 查看历史反馈`;
  const navigation = el("div", { class: "practice-navigation" }, text("span", position, "practice-position"), el("div", { class: "practice-navigation-actions" }, button("← 上一张", () => { if (previousCard) selectPractice(previousCard.id); }, "btn secondary", app.busy || !previousCard), next));
  form.append(el("div", { class: "two-buttons" }, submit, app.attempt ? reset : null), navigation, el("div", { class: "practice-tip" }, text("span", "◎"), text("span", app.data.mode === "demo" ? "当前为演示模式，使用本地规则演示反馈流程，不代表云端模型的诊断效果。" : "提交时会将这道题、相关原文、评分要点和你的回答发送给所配置的云端模型。")));
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (app.busy || !currentCard || historicalVersion) return;
    app.busy = true;
    app.answer = answer.value;
    const answerValue = app.answer;
    const cardId = app.practiceId;
    if (!filtered.some(item => item.id === cardId)) app.practiceFilter = "all";
    savePracticeDraft(currentCard, answerValue);
    app.attempt = null;
    announce("正在分析你的回答。");
    render();
    try {
      app.attempt = await api("/api/attempts", jsonOptions("POST", { card_id: cardId, answer: answerValue, request_id: crypto.randomUUID() }));
      rememberAttempt(app.attempt);
      delete app.practiceDrafts[PracticeProgress.draftKey(currentCard)];
      persistPracticeDrafts();
      app.data = await api("/api/state");
    } catch (error) { toast(`${error.message} 你的回答仍保留在输入框中。`, true); }
    finally { app.busy = false; render(); if (app.attempt) showFeedback(); else if (app.view === "practice") $("#practice-answer")?.focus(); }
  });
  const right = el("div", { class: "stack", id: "practice-feedback", "aria-busy": String(app.busy) });
  if (app.busy) {
    const pending = panel("正在分析", "对照原文，逐项检查你的理解", el("div", { class: "pending-feedback" }, el("span", { class: "spinner", "aria-hidden": "true" }), text("h3", "正在整理本次反馈"), text("p", "分析完成后，会在这里显示判断和具体依据。", "helper")));
    pending.classList.add("feedback-panel");
    right.append(pending);
  } else if (app.attempt) right.append(renderAttempt(app.attempt));
  else {
    const waiting = panel("你的学习反馈", "提交回答后，在这里查看", el("div", { class: "feedback-empty" }, text("span", "◎", "empty-symbol"), text("h3", "每一次回忆，都更进一步"), text("p", "先独立回答，再了解哪些已掌握、哪些还需要补充。", "helper"), el("ol", { class: "steps-list" }, el("li", {}, text("strong", "看清理解程度"), text("div", "区分正确、不完整、错误与无法判断。", "helper")), el("li", {}, text("strong", "找到具体依据"), text("div", "对照评分要点与讲义原文。", "helper")), el("li", {}, text("strong", "继续巩固知识"), text("div", "有匹配的已审核补练时，再试一道题。", "helper")))));
    waiting.classList.add("feedback-panel");
    right.append(waiting);
  }
  const history = renderRecentAttempts();
  if (history) right.append(history);
  const practice = panel("主动回忆", `${practiceFilterNames[app.practiceFilter]} ${filtered.length} 张 · 作答后可继续下一张`, form);
  practice.classList.add("practice-panel");
  page.append(el("div", { class: "practice-grid" }, practice, right));
  return page;
}
function feedbackPanel(title, subtitle, body) {
  const resultPanel = panel(title, subtitle, body);
  resultPanel.classList.add("feedback-panel");
  const focusTitle = $("[data-feedback-title]", resultPanel) || $(".panel-header h2", resultPanel);
  focusTitle.id = "feedback-focus";
  focusTitle.tabIndex = -1;
  return resultPanel;
}
function partialFeedbackNeedsReview(attempt) {
  const result = attempt?.diagnosis;
  if (result?.verdict !== "incomplete") return false;
  const ids = new Set((attempt.card_snapshot?.rubric || []).map(point => point.point_id));
  return !Array.isArray(result.addressed_point_ids) || !result.addressed_point_ids.length
    || new Set(result.addressed_point_ids).size !== result.addressed_point_ids.length
    || result.addressed_point_ids.some(id => !ids.has(id))
    || !Array.isArray(result.missing_point_ids) || !result.missing_point_ids.length
    || result.missing_point_ids.some(id => !ids.has(id)) || Boolean(result.contradicted_point_ids?.length)
    || !Array.isArray(result.answer_spans) || !result.answer_spans.length
    || typeof attempt.answer !== "string"
    || result.answer_spans.some(span => typeof span !== "string" || !span.trim() || !attempt.answer?.includes(span));
}
function renderAttempt(attempt) {
  const body = el("div");
  if (attempt.is_demo) body.append(note("演示反馈：由本地规则生成，仅用于验证流程。", "warning"));
  if (attempt.status === "unanswered") {
    body.append(text("h3", "你还没有填写回答"), text("p", "未作答不会被记为回答错误。写下你的理解后，再提交分析。", "helper"));
    return feedbackPanel("本次反馈", "未作答", body);
  }
  if (attempt.status === "failed") {
    const retryLimitReached = (attempt.retry_count || 0) >= 2;
    const retry = button("保留原回答，重试分析", async () => {
      if (app.busy) return;
      app.busy = true;
      render();
      try {
        app.attempt = await api(`/api/attempts/${encodeURIComponent(attempt.id)}/retry`, { method: "POST" });
        rememberAttempt(app.attempt);
        app.data = await api("/api/state");
      } catch (error) { toast(`${error.message} 原回答仍然保留。`, true); }
      finally { app.busy = false; render(); showFeedback(); }
    }, "btn secondary", app.busy || retryLimitReached);
    body.append(note(attempt.error || "模型分析暂未完成，回答已保存。", "error"), text("p", retryLimitReached ? "原回答已保存。本次重试次数已用完，请检查模型配置后重新作答。" : "你的回答已保留，不会因分析失败丢失。", "helper"), text("p", attempt.answer, "answer-text"), retry);
    return feedbackPanel("分析暂未完成", `本次已重试 ${attempt.retry_count || 0} / 2 次`, body);
  }
  if (attempt.status === "pending") {
    const reload = button("刷新分析状态", () => actionWithButton(reload, "正在刷新…", async () => {
      await refresh();
      app.attempt = app.data.attempts.find(a => a.id === attempt.id) || attempt;
      render();
    }), "btn secondary", app.busy);
    body.append(text("p", "回答已保存，正在等待分析完成。", "helper"), reload);
    return feedbackPanel("分析中", "请勿重复提交", body);
  }
  const result = attempt.diagnosis;
  if (!result) return feedbackPanel("暂无分析结果", "回答已保存", text("p", "没有收到可显示的诊断结果，请刷新页面查看。", "helper"));
  const snapshot = attempt.card_snapshot || app.data.cards.find(c => c.id === attempt.card_id) || {};
  const rubric = snapshot.rubric || [];
  const pointText = id => rubric.find(point => point.point_id === id)?.text || id;
  const needsReview = partialFeedbackNeedsReview(attempt);
  const verdictDescription = {
    correct: "这次回答覆盖了已审核的评分要点。",
    incomplete: "请补充下面这些要点，再对照原文核对。",
    incorrect: "有些表述与原文冲突，一起核对一下。",
    uncertain: "现有表述还不足以做出可靠判断。"
  };
  body.append(el("div", { class: "feedback-state", "data-verdict": result.verdict },
    el("div", { class: "result-heading" }, el("span", { class: "result-icon", "aria-hidden": "true" }, { correct: "✓", incomplete: "◐", incorrect: "↻", uncertain: "?" }[result.verdict] || "?"),
      el("div", {}, text("span", needsReview ? "历史分析 · 待复核" : "本次理解情况", "feedback-eyebrow"), el("h3", { "data-feedback-title": "" }, `${needsReview ? "原判：" : ""}${verdictNames[result.verdict] || "分析结果"}`))),
    text("p", needsReview ? "此条分析缺少已涉及要点的依据，不能据此认定方向正确。" : verdictDescription[result.verdict] || "请结合讲义核对本次反馈。", "feedback-subtitle")));
  if (needsReview) body.append(note("这条记录缺少有效的已涉及要点或相关作答依据。下方保留当时的结论与说明；建议在当前卡片批准后重新作答，获取新版分析。", "warning"));
  if (!needsReview && result.verdict === "incomplete" && result.addressed_point_ids?.length) body.append(el("section", { class: "feedback-section" }, text("h3", "回答已涉及的要点", "label"), text("p", "涉及部分内容不代表已完整满足该要点。", "helper"), el("ul", { class: "feedback-list" }, ...result.addressed_point_ids.map(id => text("li", pointText(id))))));
  if (result.missing_point_ids?.length) body.append(el("section", { class: "feedback-section" }, text("h3", "需要补充的要点", "label"), el("ul", { class: "feedback-list" }, ...result.missing_point_ids.map(id => text("li", pointText(id))))));
  if (result.contradicted_point_ids?.length) body.append(el("section", { class: "feedback-section" }, text("h3", "需要修正的要点", "label"), el("ul", { class: "feedback-list" }, ...result.contradicted_point_ids.map(id => text("li", pointText(id))))));
  body.append(el("section", { class: "feedback-section" }, text("h3", needsReview ? "当时的判断说明（待复核）" : "为什么这样判断", "label"), text("p", result.reason, "result-reason")));
  const submittedAnswer = el("details", { class: "feedback-details" }, text("summary", "查看本次回答"), text("p", attempt.answer, "answer-text"));
  if (result.answer_spans?.length) submittedAnswer.append(text("div", "分析引用的表述", "label"), ...result.answer_spans.map(span => text("blockquote", span, "quote")));
  body.append(submittedAnswer);
  const sourceIds = result.source_block_ids || [];
  const sources = el("details", { class: "feedback-details" }, text("summary", `回到原文核对 · ${sourceIds.length} 个片段`));
  for (const id of sourceIds) {
    const source = (attempt.source_snapshot || app.data.blocks).find(block => block.block_id === id);
    if (source) sources.append(el("article", { class: "source-block" }, el("div", { class: "source-top" }, badge(sourceLabel(source), "green")), text("p", source.text, "source-text"), text("div", documentFor(source.document_id)?.title || "来源材料", "meta")));
    else sources.append(text("p", `来源片段 ${id} 当前无法读取。`, "helper"));
  }
  if (sourceIds.length) body.append(sources);
  const answerImages = CardImages.answerImages(attempt);
  if (snapshot.answer || answerImages.length) body.append(el("details", { class: "feedback-details" }, text("summary", `查看已审核的参考答案${answerImages.length ? ` · ${answerImages.length} 张配图` : ""}`), snapshot.answer ? text("p", snapshot.answer, "answer-text") : null, CardImages.gallery(answerImages, app.data.media, "参考答案配图")));
  if (attempt.remediation) {
    const card = attempt.remediation;
    body.append(el("div", { class: "remediation-box" }, text("div", "针对这个知识点，再试一题", "label"), text("p", card.question), button("去做补充练习  →", () => selectPractice(card.id), "btn small", app.busy)));
  } else if (result.verdict !== "correct") body.append(text("p", "当前没有匹配的已审核补充练习。可先回顾原文，并到审核页批准相应补练。", "helper"));
  return feedbackPanel("本次反馈", `${formatDate(attempt.created_at)} · 基于提交时的卡片版本`, body);
}

async function downloadExport(path, filename) {
  const response = await fetch(path);
  if (!response.ok) {
    let detail;
    try { detail = (await response.json()).detail; } catch { /* Keep a readable error if the server returns a non-JSON response. */ }
    throw new Error(typeof detail === "string" ? detail : "导出未完成，请确认本地服务已更新并正在运行，再重试。");
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = el("a", { href: url, download: filename, hidden: true });
  document.body.append(link);
  try { link.click(); } finally {
    link.remove();
    // Let the browser consume the download before releasing its object URL.
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
}

function renderExport() {
  const cards = app.data.cards;
  const approved = cards.filter(card => card.status === "approved");
  const imageCards = approved.filter(card => CardImages.refs(card).length);
  const hasImages = imageCards.length > 0;
  const page = el("div", {}, heading("04 / 长期复习", "把理解，留给未来的自己", "将已批准的卡片导入 Anki，安排后续复习。来源与本机练习链接会一并保留。"));
  page.append(el("div", { class: "stats" }, ...[[approved.length, "可导出的已批准卡片"], [cards.filter(c => c.status === "draft").length, "仍待人工审核"], [approved.filter(c => c.kind === "remediation").length, "其中的补充练习"]].map(([n, desc]) => el("div", { class: "stat" }, text("div", String(n).padStart(2, "0"), "stat-number"), text("p", desc)))));
  const errorNode = el("p", { class: "inline-error", role: "alert" });
  const downloadButton = (label, path, filename, primary = false) => {
    const btn = button(label, () => actionWithButton(btn, "正在打包…", async () => {
      await downloadExport(path, filename);
      toast("已发起下载，请在浏览器下载列表中查看。");
    }, errorNode), `btn ${primary ? "" : "secondary "}full`, !approved.length);
    return btn;
  };
  const exportButton = downloadButton(approved.length ? `下载 ${approved.length} 张卡片 · APKG  ↓` : "暂无已批准卡片", "/api/export/apkg", "ankisense-approved.apkg", true);
  const downloads = el("div", { class: "export-downloads" }, exportButton, errorNode);
  const download = el("div", {},
    el("div", { class: "export-art", "aria-hidden": "true" }, text("span", "a", "card-mini"), text("span", "→"), text("span", "✦", "card-mini anki-mini")),
    badge("推荐 · Anki 牌组包", "green"), text("h2", "一个文件，带上完整卡片"),
    text("p", hasImages ? `其中 ${imageCards.length} 张带配图 · 图片、来源与卡片样式一起打包` : "题目、答案、来源与卡片样式一起打包", "helper"),
    el("hr", { class: "section-divider" }),
    approved.some(c => c.is_demo) ? note("导出中包含自编演示卡片，来源信息会保留演示标记。", "warning") : null,
    downloads,
    text("p", "在 Anki 中直接导入 .apkg，无需解压或手动建立字段。题目配图在正面显示，答案配图在翻面后显示。", "helper"),
    button("回到卡片审核", () => setView("review"), "btn quiet"));
  const fieldRows = [["NoteID", "稳定标识，用于重复导入更新"], ["Question", "问题"], ["Answer", "参考答案"], ["Concept", "知识概念"], ["SourceFile", "来源文件或材料标题"], ["PDFPage", "PDF 实际页序（文本材料留空）"], ["SourceQuote", "原文引用"], ["PracticeURL", "本机练习地址"]];
  const alternativeFormats = el("details", { class: "feedback-details export-alternatives" }, text("summary", "其他格式 · TSV / 图文 ZIP"),
    el("div", {},
      el("div", { class: "export-downloads" },
        downloadButton("下载纯文本 TSV", "/api/export", "ankisense-approved.tsv"),
        hasImages ? downloadButton("下载图文 ZIP", "/api/export/media", "ankisense-approved-media.zip") : null),
      text("p", "以下步骤仅适用于 TSV / ZIP 导入。使用 APKG 时无需设置。", "helper"),
      el("ol", { class: "steps-list" },
        el("li", {}, text("strong", "建立笔记类型"), text("div", "创建 AnkiSense 笔记类型，按下表建立 8 个字段；正面放 Question，背面放 Answer 与来源字段。", "helper")),
        el("li", {}, text("strong", "导入文件"), text("div", "纯文本 TSV 选择 Tab 分隔、UTF-8 编码，关闭 HTML。图文 ZIP 先解压，将 media 内的 PNG 文件复制到当前用户的 collection.media，再导入包内 TSV 并启用 HTML。", "helper")),
        el("li", {}, text("strong", "重复导入按 NoteID 更新"), text("div", "保持首列 NoteID 和笔记类型一致，核对字段映射，选择更新已有笔记。", "helper"))),
      el("table", { class: "field-table" }, el("thead", {}, el("tr", {}, text("th", "字段顺序"), text("th", "内容"))), el("tbody", {}, ...fieldRows.map(([name, description], index) => el("tr", {}, text("td", `${index + 1}. ${name}`), text("td", description)))))));
  const instructions = el("div", {}, el("ol", { class: "steps-list" },
    el("li", {}, text("strong", "下载 APKG 牌组包"), text("div", "先审核并批准卡片，再点击左侧下载按钮。已保存的配图会自动随卡片打包。", "helper")),
    el("li", {}, text("strong", "在 Anki 中导入文件"), text("div", "打开 Anki，点击「导入文件」，选择 ankisense-approved.apkg。导入后进入 AnkiSense 牌组开始复习。", "helper")),
    el("li", {}, text("strong", "修改后，再次导入更新"), text("div", "在网页修改、批准并重新导出同一卡片。导入时可将更新笔记设为「始终」，以网页内容更新；这会覆盖你直接在 Anki 中修改的字段。不要启用「导入任何学习进度」。", "helper"))),
    text("p", "如果之前用 TSV 导入过相同卡片，首次改用 APKG 可能出现两份笔记；两种格式的已有笔记不会自动合并。", "helper"),
    text("p", "卡片上的「用自己的话回答」会打开本机 AnkiSense，需要网页服务正在运行；在其他设备上不能直接使用这个本机链接。", "helper"),
    alternativeFormats);
  page.append(el("div", { class: "export-grid" }, panel("导出已批准卡片", "草稿与已拒绝卡片不会导出", download), panel("导入 Anki，只需两步", "下载牌组包，再在 Anki 中打开", instructions)));
  return page;
}

for (const nav of document.querySelectorAll("[data-view]")) nav.addEventListener("click", () => setView(nav.dataset.view));
async function initialize() {
  try {
    initializeAutomaticWorkspace();
    const params = new URLSearchParams(window.location.search);
    const id = params.get("card") || params.get("card_id");
    if (id) { app.practiceId = id; app.view = "practice"; }
    await refresh();
    await loadMaterialImports(true);
    for (const jobs of Object.values(app.autoJobs)) if (Array.isArray(jobs)) for (const job of jobs) if (autoRunning(job)) scheduleAutoPoll(job);
    if (id && !app.data.cards.some(c => c.id === id && c.status === "approved")) toast("此卡片不存在或尚未批准，请先到卡片审核页检查。", true);
  } catch (error) {
    $("#mode-status").textContent = "本地服务未连接";
    $("#main").replaceChildren(empty("暂时无法连接学习空间", `${error.message} 请确认本地 Python 服务正在运行。`, button("重新连接", initialize), "↻"));
  }
}
initialize();
