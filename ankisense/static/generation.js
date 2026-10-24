(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Generation = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const active = job => ["queued", "running"].includes(job?.status);
  const validCount = value => /^\d+$/.test(String(value)) && Number(value) >= 1 && Number(value) <= 30;
  const statusLabel = job => ({ queued: "等待开始", running: job?.stop_requested ? "正在停止后续批次" : "正在生成卡片", completed: "本次制卡完成", partial: "已生成部分卡片", failed: "本次制卡未完成", stopped: "已停止后续批次", interrupted: "制卡已中断" })[job?.status] || "";
  function node(tag, className, value) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined) element.textContent = value;
    return element;
  }
  function button(label, handler, style = "btn secondary small") {
    const element = node("button", style, label); element.type = "button";
    element.addEventListener("click", handler); return element;
  }
  function create({ documentId, preferences, api, getBlockIds, getJob, configured, onJob, onView, disclosure }) {
    let job = getJob();
    if (active(job) && preferences.count == null) { preferences.custom = true; preferences.count = job.target_count; }
    if (active(job)) preferences.autoImages = job.auto_images !== false;
    const section = node("div", "generation-planner");
    const scope = node("p", "generation-scope", "正在整理当前讲义…");
    const title = node("h3", "", "准备生成多少张卡片？");
    const count = node("input", "generation-count");
    count.type = "number"; count.min = "1"; count.max = "30"; count.step = "1";
    count.id = "generation-count"; count.setAttribute("aria-label", "制卡数量");
    count.value = preferences.count ?? "";
    const countLabel = node("label", "field"); countLabel.append(node("span", "", "目标张数（1–30）"), count);
    const recommended = node("p", "generation-recommendation helper", "系统会根据有效原文建议数量，不调用模型。");
    const explanation = node("p", "helper generation-coverage");
    const warnings = node("ul", "generation-warnings helper");
    const outline = node("details", "generation-outline"); outline.hidden = true;
    const outlineTitle = node("summary", "", "查看提纲候选");
    const outlineBody = node("div"); outline.append(outlineTitle, outlineBody);
    const error = node("p", "inline-error generation-error"); error.setAttribute("role", "alert");
    const reset = button("使用系统建议", () => { preferences.custom = false; preferences.count = null; preferences.requestId = null; planNow(); });
    const retry = button("重新计算建议", () => planNow(), "btn quiet small"); retry.hidden = true;
    const start = button("开始制卡 →", startJob, "btn generation-start"); start.disabled = true;
    const autoImages = node("input"); autoImages.type = "checkbox";
    autoImages.checked = preferences.autoImages !== false;
    autoImages.setAttribute("aria-label", "自动匹配讲义图片");
    const imageOption = node("div", "generation-image-option");
    const imageLabel = node("label", "generation-scope-toggle");
    imageLabel.append(autoImages, node("span", "", "自动匹配讲义图片"));
    imageOption.append(imageLabel, node("p", "helper", "需要配图时，从引用页寻找原图。匹配在本机完成；检查会将候选图、原页及相关卡片内容发送至当前模型，会消耗 API 额度。"));
    autoImages.addEventListener("change", () => { preferences.autoImages = autoImages.checked; preferences.requestId = null; });
    const progress = node("section", "generation-job"); progress.hidden = true;
    progress.setAttribute("aria-label", "本次制卡进度");
    const jobTitle = node("h3", "generation-job-title");
    const jobText = node("p", "helper generation-job-progress"); jobText.setAttribute("aria-live", "polite");
    const bar = node("progress", "generation-progress"); bar.max = 1; bar.value = 0; bar.setAttribute("aria-label", "批次完成进度");
    const jobError = node("p", "generation-job-error helper");
    const jobWarnings = node("ul", "generation-job-warnings helper");
    const coverage = node("details", "generation-topic-coverage"); coverage.hidden = true;
    const coverageTitle = node("summary"); const coverageBody = node("div"); coverage.append(coverageTitle, coverageBody);
    const rejected = node("details", "generation-rejections"); rejected.hidden = true;
    const rejectedTitle = node("summary");
    const rejectedContent = node("div");
    rejected.append(rejectedTitle, rejectedContent);
    let rejectedSignature;
    const view = button("查看已生成卡片", () => onView(job), "btn small");
    const stop = button("停止后续批次", stopJob);
    const jobActions = node("div", "two-buttons"); jobActions.append(view, stop);
    progress.append(jobTitle, jobText, bar, jobError, jobWarnings, rejected, coverage, jobActions);
    const controls = node("div", "generation-controls"); controls.append(countLabel, reset, start);
    section.append(scope, title, controls, recommended, explanation, warnings, outline, error, retry, imageOption,
      node("p", "helper", "这是目标数量；系统会分批出题并去重，证据不足时少生成，不凑数。"),
      node("p", "helper automatic-review-notice", disclosure), progress);
    let plan, serial = 0, timer, pending = false, starting = false;
    function enabled() {
      const busy = active(job) || starting;
      count.disabled = busy; reset.disabled = busy; autoImages.disabled = busy;
      start.disabled = !configured || busy || pending || !plan || !validCount(count.value) || plan.target_count !== Number(count.value);
      start.textContent = active(job) ? "本讲义正在制卡" : starting ? "正在创建任务…" : "开始制卡 →";
    }
    function showJob(value) {
      job = value; progress.hidden = !job;
      if (job) {
        if (active(job)) { preferences.autoImages = job.auto_images !== false; autoImages.checked = preferences.autoImages; }
        jobTitle.textContent = statusLabel(job);
        jobText.textContent = `已生成 ${job.generated_count || 0} / 目标 ${job.target_count} 张 · 已处理 ${job.completed_batches || 0} / ${job.total_batches || 0} 批${job.rejected_count ? ` · ${job.rejected_count} 张未通过校验` : ""}。卡片生成后会继续自动检查。`;
        bar.max = Math.max(1, job.total_batches || 1); bar.value = job.completed_batches || 0;
        jobError.textContent = job.error || (job.status === "interrupted" ? "服务中断，已保存的卡片仍在。新任务不会自动续跑或重试。" : job.status === "stopped" ? "已经生成的卡片已保留；正在检查的卡片会继续完成检查。" : "");
        jobWarnings.replaceChildren(...(job.warnings || []).map(message => node("li", "", message)));
        const report = job.coverage;
        coverage.hidden = !report?.total;
        if (report?.total) {
          coverageTitle.textContent = `提纲查漏 · ${report.total - report.matched_count} 项未匹配到题目`;
          coverageBody.replaceChildren(node("p", "helper", report.note),
            ...(report.stale ? [node("p", "inline-error", "原文已更新，此次覆盖提示已过期，请按新版本重新制卡。")] : []),
            ...report.topics.map(topic => node("p", "helper", `第 ${topic.page} 页 · ${topic.title} · ${topic.status === "possible_match" ? "可能已有对应题目" : "未匹配，建议检查"}`)));
        }
        const failures = job.rejected_cards || [];
        rejected.hidden = !failures.length;
        rejectedTitle.textContent = `查看未保存的候选与原因 · ${failures.length} 张`;
        const signature = JSON.stringify(failures);
        if (signature !== rejectedSignature) {
          rejectedSignature = signature;
          rejectedContent.replaceChildren(node("p", "helper", "这些候选未进入卡片库。其余有效卡片已保留，并继续自动检查。"));
          failures.forEach(item => {
            const detail = node("details", "generation-rejection");
            detail.append(node("summary", "", `第 ${item.batch} 批 · 第 ${item.index} 张：${item.question || "未提供有效题目"}`),
              node("p", "inline-error", item.reason), node("pre", "", item.draft_preview || "没有可预览的内容。"));
            if (item.preview_truncated) detail.append(node("p", "helper", "候选内容较长，此处仅保留前一部分预览。"));
            rejectedContent.append(detail);
          });
        }
        view.disabled = !(job.card_ids?.length); stop.hidden = !active(job); stop.disabled = Boolean(job.stop_requested);
        stop.textContent = job.stop_requested ? "等待当前批次结束…" : "停止后续批次";
      }
      enabled();
    }
    async function planNow() {
      clearTimeout(timer);
      const version = ++serial; plan = null; pending = true; retry.hidden = true;
      error.textContent = ""; enabled();
      if (preferences.custom && !validCount(count.value)) {
        pending = false; error.textContent = "请输入 1 到 30 之间的整数。"; enabled(); return;
      }
      try {
        const result = await api(`/api/documents/${encodeURIComponent(documentId)}/generation-plan`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ block_ids: getBlockIds(), count: preferences.custom ? Number(count.value) : null }) });
        if (version !== serial) return;
        plan = result;
        if (!preferences.custom) { count.value = result.target_count; preferences.count = result.target_count; }
        scope.textContent = `${result.scope === "selected" ? "指定出题范围" : "本次导入的全部有效原文"} · ${result.stats.source_page_count} 页 · ${result.stats.source_block_count} 个片段`;
        recommended.textContent = `系统建议 ${result.recommended_count} 张${preferences.custom ? " · 当前使用自定数量" : " · 已采用"}。${result.suggestion_reason || "根据有效原文长度与范围估计，可随时调整。"}`;
        explanation.textContent = `预计分 ${result.stats.batch_count} 批生成${result.stats.coverage_ratio < 1 ? `，本次覆盖约 ${Math.round(result.stats.coverage_ratio * 100)}% 原文` : ""}。建议计算在本机完成，不消耗 API 额度。`;
        warnings.replaceChildren(...(result.warnings || []).map(message => node("li", "", message)));
        outline.hidden = !result.topics?.length;
        outlineTitle.textContent = `查看提纲候选 · ${result.topics?.length || 0} 项`;
        outlineBody.replaceChildren(node("p", "helper", "按标题、编号和条目在本机提取；子项可合为一题，候选数量不是知识点总数。"),
          ...(result.topics || []).map(topic => node("p", "helper", `第 ${topic.page} 页 · ${topic.title}`)));
      } catch (failure) {
        if (version !== serial) return;
        error.textContent = failure.message; scope.textContent = "暂时无法计算制卡范围"; retry.hidden = false;
      } finally { if (version === serial) { pending = false; enabled(); } }
    }
    count.addEventListener("input", () => {
      preferences.custom = true; preferences.count = count.value; preferences.requestId = null;
      ++serial; plan = null; pending = true; enabled(); clearTimeout(timer); timer = setTimeout(planNow, 300);
    });
    async function startJob() {
      if (start.disabled) return;
      starting = true; error.textContent = ""; enabled();
      preferences.requestId ||= globalThis.crypto.randomUUID();
      try {
        const created = await api("/api/generation-jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ document_id: documentId, block_ids: getBlockIds(), count: Number(count.value), auto_images: autoImages.checked, source_fingerprint: plan.source_fingerprint, request_id: preferences.requestId }) });
        preferences.requestId = null; showJob(created); onJob(created);
      } catch (failure) {
        if (!failure.status || failure.status >= 500) {
          try {
            const latest = await api("/api/state");
            const recovered = (latest.generation_jobs || []).find(item => item.document_id === documentId && item.request_id === preferences.requestId);
            if (recovered) { preferences.requestId = null; showJob(recovered); onJob(recovered); return; }
          } catch { /* Keep the request ID for an explicit, idempotent retry. */ }
        }
        error.textContent = failure.message;
        if (failure.status === 409) { preferences.requestId = null; await planNow(); error.textContent = `${failure.message} 请确认更新后的范围再开始。`; }
      } finally { starting = false; enabled(); }
    }
    async function stopJob() {
      if (!job || stop.disabled) return;
      stop.disabled = true;
      try { const result = await api(`/api/generation-jobs/${encodeURIComponent(job.id)}/stop`, { method: "POST" }); showJob(result); onJob(result); }
      catch (failure) { error.textContent = failure.message; stop.disabled = false; }
    }
    section.refreshJob = () => showJob(getJob());
    section.refreshPlan = () => { preferences.requestId = null; return planNow(); };
    showJob(job); planNow();
    return section;
  }
  return { active, validCount, statusLabel, create };
});
