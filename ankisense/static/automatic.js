"use strict";

function initializeAutomaticWorkspace() {
  clearTimeout(app.importTimer);
  Object.assign(app, { materialImports: [], materialImportId: null, importBusy: false, importError: "", importTimer: null, automaticAnswers: new Set(), importPreferences: { count: "", autoImages: true }, importRetrying: null, importCancelling: null, importCancelErrors: {} });
  viewNames.cards = "卡牌结果";
}

function activeMaterialImport(record) { return ["pending", "running"].includes(record.status); }
function selectedMaterialImport() { return app.materialImports.find(r => r.id === app.materialImportId) || app.materialImports.find(r => r.document_id === app.documentId); }

function automaticPlanLabel(plan) {
  const parts = [];
  if (Number.isInteger(plan.recommended_count)) parts.push(`系统建议 ${plan.recommended_count} 张`);
  parts.push(Number.isInteger(plan.target_count) ? `本次目标 ${plan.target_count} 张` : "本次按材料自动决定数量");
  if (plan.total_batches) parts.push(`共 ${plan.total_batches} 批`);
  return parts.join(" · ");
}

function automaticFailure(record) {
  if (activeMaterialImport(record) || ["completed", "cancelled"].includes(record.status)) return null;
  if (record.failure || record.job?.failure) return record.failure || record.job.failure;
  // Older installed versions only persisted safe messages, without error codes.
  const messages = [record.error, record.job?.error];
  if (record.status === "failed" && !record.job?.card_ids?.length) messages.push(...(record.job?.issues || []).filter(issue => issue.code === "batch_failed").map(issue => issue.reason));
  const message = messages.filter(Boolean).find(value => /无法连接.*模型|模型.*超时|模型服务.*不可用|API Key|模型服务.*额度|模型接口.*配置/.test(value));
  return message ? { code: "model_service", message, action: "先检查模型设置、网络和额度，再继续未完成部分。" } : null;
}

function summarizeAutomaticIssues(issues) {
  const groups = new Map();
  for (const issue of issues) {
    const reason = issue.reason || "处理时发现待核对内容。";
    const key = `${issue.code || "other"}:${reason}`;
    if (!groups.has(key)) groups.set(key, { code: issue.code, reason, count: 0, pages: new Set() });
    const group = groups.get(key);
    group.count += 1;
    for (const page of issue.pages || (issue.page ? [issue.page] : [])) group.pages.add(page);
  }
  return [...groups.values()].sort((a, b) => Number(b.code === "batch_failed" || b.code === "card_failed") - Number(a.code === "batch_failed" || a.code === "card_failed"))
    .map(group => ({ ...group, pages: [...group.pages].sort((a, b) => a - b) }));
}

async function loadMaterialImports(initial = false) {
  try {
    const before = JSON.stringify(app.materialImports);
    const result = await api("/api/material-imports");
    app.materialImports = result.imports || [];
    app.importError = "";
    const active = app.materialImports.find(activeMaterialImport);
    const requested = new URLSearchParams(location.search).get("import");
    if (initial && requested && app.materialImports.some(r => r.id === requested)) {
      app.materialImportId = requested;
      app.view = "cards";
    }
    if (initial && !requested && active && !new URLSearchParams(location.search).has("card") && !new URLSearchParams(location.search).has("card_id")) {
      app.materialImportId = active.id;
      app.view = "cards";
    }
    const selected = selectedMaterialImport();
    if (selected?.document_id) app.documentId = selected.document_id;
    if (before !== JSON.stringify(app.materialImports)) {
      if (app.view === "cards") await refresh();
      else if (app.view === "materials") {
        const current = document.querySelector("#automatic-import-status");
        if (current) current.replaceWith(renderAutomaticImportStatus());
      }
    }
    clearTimeout(app.importTimer);
    if (active) app.importTimer = setTimeout(() => loadMaterialImports(), 1800);
  } catch (error) {
    if (app.materialImportId) {
      app.importError = "连接暂时中断，后台任务会继续。正在重新连接…";
      const current = document.querySelector("#automatic-import-status");
      if (current) current.replaceWith(renderAutomaticImportStatus());
      clearTimeout(app.importTimer);
      app.importTimer = setTimeout(() => loadMaterialImports(), 4000);
    }
  }
}

async function retryMaterialImport(record) {
  if (app.importRetrying || app.importCancelling || activeMaterialImport(record)) return;
  app.importRetrying = record.id;
  try {
    const updated = await api(`/api/material-imports/${encodeURIComponent(record.id)}/retry`, { method: "POST" });
    app.materialImports = app.materialImports.map(item => item.id === updated.id ? updated : item);
    app.materialImportId = updated.id;
    history.replaceState(null, "", `?import=${encodeURIComponent(updated.id)}`);
    app.view = "cards";
    await refresh();
    await loadMaterialImports();
  } catch (error) { app.importError = error.message; }
  finally { app.importRetrying = null; render(); }
}

async function cancelMaterialImport(record) {
  if (app.importCancelling || app.importRetrying || !activeMaterialImport(record)) return;
  app.importCancelling = record.id;
  delete app.importCancelErrors[record.id];
  render();
  try {
    const updated = await api(`/api/material-imports/${encodeURIComponent(record.id)}/cancel`, { method: "POST" });
    app.materialImports = app.materialImports.map(item => item.id === updated.id ? updated : item);
    await refresh();
    await loadMaterialImports();
  } catch (error) { app.importCancelErrors[record.id] = `取消未完成：${error.message}。任务可能仍在处理，请重试取消。`; }
  finally { app.importCancelling = null; render(); }
}

function renderAutomaticImportStatus() {
  const box = el("div", { id: "automatic-import-status", "aria-live": "polite" });
  if (app.importError) box.append(note(app.importError, "warning"));
  for (const record of app.materialImports.slice(0, 6)) {
    const job = record.job;
    const failure = automaticFailure(record);
    const progress = job?.progress || record.progress;
    const row = el("article", { class: "automatic-import-row", "data-import-status": record.status },
      el("div", {}, text("strong", record.filename), text("p", record.message || "正在准备…", "meta")),
      badge(failure ? "模型异常，已停止" : ({ pending: "等待处理", running: "自动处理中", completed: "已完成", partial: "部分完成", failed: "未完成", cancelled: "已取消，可继续", interrupted: "已中断，可继续", outdated: "来源已更新", awaiting_confirmation: "需要处理" })[record.status] || "处理详情", activeMaterialImport(record) ? "blue" : record.status === "completed" ? "green" : "amber"));
    if (app.importCancelErrors[record.id]) row.append(note(app.importCancelErrors[record.id], "warning"));
    if (progress?.total) {
      if (activeMaterialImport(record) || record.status === "completed") row.append(el("progress", { max: progress.total, value: progress.completed, "aria-label": `${record.filename}处理进度` }));
      row.append(text("p", `${job ? "已处理批次" : "已识别页面"} ${progress.completed}/${progress.total}${record.status !== "completed" ? "，进度不代表制卡成功" : ""}`, "helper"));
    }
    if (record.plan) {
      const plan = record.plan;
      row.append(text("p", automaticPlanLabel(plan), "helper automatic-import-plan"));
      if (plan.suggestion_reason) row.append(text("p", plan.suggestion_reason, "helper"));
    }
    if (job?.card_ids?.length) row.append(text("span", `${job.card_ids.length} 张卡牌已生成`, "meta"));
    if (failure) row.append(el("div", { class: "notice warning automatic-import-failure", role: "alert" },
      text("strong", "模型服务异常，尚未处理的内容已停止"), text("p", failure.message),
      text("p", `已保留 ${job?.card_ids?.length || 0} 张卡片及原文件。${failure.action || "检查模型设置后，可继续未完成部分。"}`),
      button("检查模型设置", () => setView("settings"), "btn secondary small")));
    else if (record.error || job?.error) row.append(note(record.error || job.error, "warning"));
    row.append(button("查看卡牌", () => {
      app.materialImportId = record.id;
      history.replaceState(null, "", `?import=${encodeURIComponent(record.id)}`);
      if (record.document_id) app.documentId = record.document_id;
      setView("cards");
    }, "btn secondary small"));
    const retryable = record.retryable ?? ["failed", "partial", "cancelled", "interrupted"].includes(record.status);
    if (activeMaterialImport(record)) row.append(el("div", { class: "automatic-import-actions" },
      button(app.importCancelling === record.id ? "正在取消…" : "取消处理", () => cancelMaterialImport(record), "btn secondary small", Boolean(app.importCancelling || app.importRetrying)),
      text("p", "取消后保留已保存卡片，停止后续处理；已经发送的模型请求无法撤回。", "helper")));
    if (retryable) row.append(button(app.importRetrying === record.id ? "正在补跑…" : ["interrupted", "cancelled"].includes(record.status) ? "继续未完成部分" : "重试未完成部分", () => retryMaterialImport(record), "btn secondary small", Boolean(app.importRetrying || app.importCancelling)));
    box.append(row);
  }
  return box;
}

function renderAutomaticImport() {
  const preferences = app.importPreferences;
  const count = el("input", { type: "number", min: "1", max: "600", step: "1", value: preferences.count, placeholder: "按讲义自动决定", "aria-label": "自动制卡目标数量" });
  count.addEventListener("input", () => { preferences.count = count.value; });
  const images = el("input", { type: "checkbox", checked: preferences.autoImages, "aria-label": "自动匹配并检查讲义配图" });
  images.addEventListener("change", () => { preferences.autoImages = images.checked; });
  const options = el("details", { class: "automatic-import-options" }, text("summary", "高级选项：张数与配图"),
    field("目标张数（留空使用系统建议，1–600）", count),
    el("label", { class: "generation-scope-toggle" }, images, "自动匹配并检查讲义配图"),
    text("p", "张数是目标；证据不足时少生成，不凑数。生成和保存后自动检查，必要时会发送相关文字、配图与原页至已配置模型。", "helper"));
  const file = el("input", { type: "file", accept: ".ppt,.pptx,.pdf", "aria-label": "选择讲义，自动制卡", disabled: app.importBusy });
  file.addEventListener("change", async () => {
    if (!file.files[0] || app.importBusy) return;
    if (count.value && (!count.checkValidity() || !/^\d+$/.test(count.value))) {
      app.importError = "目标张数应为 1–600 的整数，或留空使用系统建议。"; render(); return;
    }
    app.importBusy = true;
    app.importError = "";
    file.disabled = true;
    try {
      const data = new FormData();
      data.append("file", file.files[0]);
      if (preferences.count) data.append("count", preferences.count);
      data.append("auto_images", String(preferences.autoImages));
      const record = await api("/api/material-imports", { method: "POST", body: data });
      app.materialImportId = record.id;
      history.replaceState(null, "", `?import=${encodeURIComponent(record.id)}`);
      app.materialImports = [record, ...app.materialImports.filter(r => r.id !== record.id)];
      if (record.document_id) app.documentId = record.document_id;
      app.view = "cards";
      await refresh();
      await loadMaterialImports();
    } catch (error) {
      app.importError = error.message;
      render();
    } finally { app.importBusy = false; file.disabled = false; if (app.importError && app.view === "materials") render(); }
  });
  return panel("导入讲义，自动得到卡牌", "选好文件即可，系统会自动完成识别、制卡、检查和按需配图。",
    el("div", { class: "automatic-import" }, el("div", { class: "dropzone" }, text("div", "↥", "upload-symbol"),
      text("strong", app.importBusy ? "正在上传讲义…" : "选择 PPT / PPTX / PDF 讲义"),
      text("p", "支持整份讲义，最多 500 页、50 MB。导入后无需逐页确认或再次点击生成。"), file),
      !modeIsCloud() || !app.data.configured ? note("请先在模型设置中保存可用的 API 配置。", "warning") : null,
      options, renderAutomaticImportStatus()));
}

function renderCards() {
  const record = selectedMaterialImport();
  const documentId = record ? record.document_id : app.documentId;
  const cards = app.data.cards.filter(c => c.document_id === documentId && c.status !== "rejected" && !c.source_outdated);
  const readyCount = cards.filter(card => card.status === "approved").length;
  const page = el("div", {}, heading("卡牌结果", documentFor(documentId)?.title || record?.filename || "你的学习卡牌",
    activeMaterialImport(record || {}) ? "系统正在自动处理，生成的卡牌会陆续出现在这里。" : cards.length ? `已保存 ${cards.length} 张卡牌，${readyCount} 张可练习和导出。` : record ? "尚未保存可用卡片，请查看下方处理状态与说明。" : "导入讲义后，可在这里查看卡片。",
    [button("导入其他讲义", () => setView("materials"), "btn secondary"),
      button("开始练习", () => { app.practiceId = cards.find(c => c.status === "approved")?.id || null; setView("practice"); }, "btn", !cards.some(c => c.status === "approved")),
      button("导出至 Anki", () => setView("export"), "btn secondary", !cards.some(c => c.status === "approved"))]));
  page.append(renderAutomaticImportStatus());
  if (app.data.documents.length) {
    const selector = el("select", { "aria-label": "查看哪份讲义的卡牌" }, ...app.data.documents.map(d => el("option", { value: d.id, selected: d.id === documentId }, d.title)));
    selector.addEventListener("change", () => { app.documentId = selector.value; app.materialImportId = null; history.replaceState(null, "", location.pathname); render(); });
    page.append(field("学习材料", selector));
  }
  const gallery = el("div", { class: "automatic-card-grid" });
  for (const [index, card] of cards.entries()) {
    const answer = el("details", { class: "automatic-card-answer", ...(app.automaticAnswers.has(card.id) ? { open: "" } : {}) }, el("summary", {}, "显示答案"), text("p", card.answer, "card-answer-text"));
    answer.addEventListener("toggle", () => { if (answer.open) app.automaticAnswers.add(card.id); else app.automaticAnswers.delete(card.id); });
    const images = CardImages.refs(card);
    const answerGallery = CardImages.gallery(images.filter(image => image.side === "answer"), app.data.media, "答案配图");
    if (answerGallery) answer.append(answerGallery);
    const issues = card.auto_quality?.issues || [];
    gallery.append(el("article", { class: "automatic-card", "data-card-id": card.id },
      el("div", { class: "automatic-card-top" }, text("span", `卡牌 ${index + 1} · ${card.concept}`, "meta"),
        badge(isMachineApproved(card) && card.status === "approved" ? "已自动检查" : cardStatusLabel(card), card.status === "approved" ? "green" : "amber")),
      text("h2", card.question), CardImages.gallery(images.filter(image => image.side === "question"), app.data.media, "题目配图"), AutoIllustration.create({ card, compact: true }), answer,
      issues.length ? note(issues.map(i => i.reason).join("；"), "warning") : null,
      el("details", { class: "automatic-card-source" }, el("summary", {}, "查看来源"), ...(card.evidence || []).map(e => text("blockquote", e.quote))),
      button("编辑卡牌", () => { app.cardId = card.id; app.reviewDocument = documentId; app.reviewFilter = "all"; setView("review"); }, "btn quiet small")));
  }
  page.append(gallery);
  if (!cards.length) page.append(empty(activeMaterialImport(record || {}) ? "正在自动制卡…" : record?.status === "cancelled" ? "本次处理已取消" : record && record.status !== "completed" ? "本次尚未成功保存卡片" : "这里会显示生成的卡牌",
    activeMaterialImport(record || {}) ? "无需操作，页面会自动更新。整份讲义需要一些时间。" : record && record.status !== "completed" ? "请查看上方处理原因；原文件已保留，无需重新上传。" : "导入讲义后，系统会自动处理并显示结果。", record ? null : button("导入讲义", () => setView("materials"))));
  const issues = record?.job?.issues || [];
  if (issues.length) {
    const groups = summarizeAutomaticIssues(issues);
    page.append(el("details", { class: "automatic-processing-notes" }, el("summary", {}, `处理说明（${groups.length} 类）`),
      text("p", `${issues.length} 条记录已按原因合并；数字、公式等本机提示仅表示需要留意，不代表已确认错误。`, "helper"),
      ...groups.map(group => text("p", `${group.reason}（${group.count} 条${group.pages.length ? `，第 ${group.pages.slice(0, 8).join("、")}${group.pages.length > 8 ? ` 等 ${group.pages.length}` : ""} 页` : ""}）`))));
  }
  return page;
}

if (typeof module !== "undefined" && module.exports) module.exports = { automaticPlanLabel, automaticFailure, summarizeAutomaticIssues };
