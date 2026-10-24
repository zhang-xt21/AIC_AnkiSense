(function (root, factory) {
  "use strict";
  const settings = factory();
  if (typeof module === "object" && module.exports) module.exports = settings;
  else root.ModelSettings = settings;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  let nextId = 0;
  const endpoint = "/api/settings/ai";

  function node(tag, className, content) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (content !== undefined) result.textContent = content;
    return result;
  }

  function renderStatus(target, settings) {
    const connection = settings.model_connection || {};
    const state = settings.mode === "demo" ? "demo" : !settings.configured ? "unverified" : connection.status || "unverified";
    const label = { unverified: "未验证", testing: "验证中…", success: "验证通过", error: "验证失败" }[state] || "未验证";
    target.dataset.connection = state;
    target.textContent = settings.mode === "demo" ? "演示模式 · 本地规则反馈"
      : settings.configured ? `云端模式 · ${settings.model || "已配置模型"} · ${label}` : "云端模式 · 待补全配置";
    target.title = settings.mode === "demo" ? "使用本地规则，无需连接模型。"
      : connection.checked_at ? `上次验证：${new Date(connection.checked_at).toLocaleString()}。${connection.message || ""} 验证结果不代表持续在线。`
        : "请在模型设置中验证连接；保存配置不会发起模型请求。";
  }

  function create({ api, jsonOptions, onSaved, onConnection } = {}) {
    if (typeof api !== "function" || typeof jsonOptions !== "function") {
      throw new TypeError("ModelSettings.create requires api and jsonOptions.");
    }
    const prefix = `model-settings-${++nextId}`;
    const panel = node("section", "panel model-settings");
    const title = node("h2", "", "模型与 API");
    title.id = `${prefix}-title`;
    panel.setAttribute("aria-labelledby", title.id);
    const header = node("div", "panel-header");
    const headerText = node("div");
    headerText.append(title, node("div", "meta", "为卡片生成与回答分析选择运行方式。"));
    const badge = node("span", "pill", "读取中");
    header.append(headerText, badge);
    const body = node("div", "panel-body");
    const loading = node("p", "model-settings-loading", "正在读取本机模型设置…");
    loading.setAttribute("role", "status");
    const error = node("div", "inline-error");
    error.setAttribute("role", "alert");
    const feedback = node("div", "notice model-settings-feedback");
    feedback.setAttribute("role", "status");
    feedback.setAttribute("aria-live", "polite");
    feedback.hidden = true;
    const retry = node("button", "btn secondary small", "重新读取");
    retry.type = "button";
    retry.hidden = true;
    const form = node("form", "model-settings-form");
    form.hidden = true;
    const controls = node("fieldset", "model-settings-controls");
    const modeGroup = node("fieldset", "model-settings-mode");
    modeGroup.append(node("legend", "", "运行模式"));
    const modes = {};
    const modeOptions = node("div", "model-settings-mode-options");
    for (const [value, label, description] of [
      ["demo", "演示模式", "使用本地规则，先体验学习流程"],
      ["cloud", "自配 API", "使用你选择的服务商与模型"]
    ]) {
      const option = node("label", "model-settings-mode-option");
      const radio = node("input");
      radio.type = "radio";
      radio.name = `${prefix}-mode`;
      radio.value = value;
      const copy = node("span");
      copy.append(node("strong", "", label), node("span", "helper", description));
      option.append(radio, copy);
      modeOptions.append(option);
      modes[value] = radio;
    }
    modeGroup.append(modeOptions);
    const modeHint = node("p", "notice model-settings-mode-hint");
    modeHint.setAttribute("aria-live", "polite");
    const connection = node("div", "model-settings-connection");
    connection.append(node("h3", "", "服务商配置"));
    const baseUrl = node("input");
    baseUrl.type = "url";
    baseUrl.name = "base_url";
    baseUrl.placeholder = "https://你的服务商地址/v1";
    baseUrl.autocomplete = "off";
    baseUrl.spellcheck = false;
    const model = node("input");
    model.type = "text";
    model.name = "model";
    model.placeholder = "填写服务商提供的模型名称";
    model.autocomplete = "off";
    model.spellcheck = false;
    const key = node("input");
    key.type = "password";
    key.name = "api_key";
    key.autocomplete = "new-password";
    key.spellcheck = false;
    key.setAttribute("autocapitalize", "none");
    const keyHelp = node("span", "helper");
    const keyStatus = node("span", "pill model-settings-key-status");
    function field(label, input, help) {
      const wrapper = node("label", "field");
      const caption = node("span", "model-settings-field-label", label);
      input.id = `${prefix}-${input.name}`;
      wrapper.htmlFor = input.id;
      wrapper.append(caption, input);
      if (help) {
        const helpNode = typeof help === "string" ? node("span", "helper", help) : help;
        helpNode.id = `${input.id}-help`;
        input.setAttribute("aria-describedby", helpNode.id);
        wrapper.append(helpNode);
      }
      return wrapper;
    }
    const keyField = field("API Key", key, keyHelp);
    keyField.firstElementChild.append(keyStatus);
    const clearKey = node("input");
    clearKey.type = "checkbox";
    clearKey.name = "clear_api_key";
    const clearKeyLabel = node("label", "model-settings-clear-key");
    clearKeyLabel.append(clearKey, node("span", "", "保存时清除已保存的密钥"));
    connection.append(
      field("API 地址", baseUrl, "填写兼容 Chat Completions 的 Base URL，不包含 /chat/completions。更换服务器时，请重新填写密钥或勾选清除旧密钥。"),
      field("模型名称", model, "按服务商提供的名称填写。图片检查还需要模型支持图片输入。"),
      keyField, clearKeyLabel
    );
    const advanced = node("details", "model-settings-advanced");
    advanced.append(node("summary", "", "高级设置"));
    const timeout = node("input");
    timeout.type = "number";
    timeout.name = "timeout_seconds";
    timeout.min = "1";
    timeout.max = "180";
    timeout.step = "any";
    timeout.required = true;
    timeout.addEventListener("invalid", () => { advanced.open = true; });
    advanced.append(field("请求超时（秒）", timeout, "可设置 1–180 秒。模型响应较慢时，可适当增加等待时间。"));
    const storageNote = node("p", "helper model-settings-storage", "配置与密钥保存在本机配置文件中，不写入浏览器存储，读取设置时不会返回密钥。请妥善保管本机配置文件。保存只更新设置，不会请求模型或验证连通性。");
    const save = node("button", "btn", "保存设置");
    save.type = "submit";
    const test = node("button", "btn secondary", "验证连接");
    test.type = "button";
    const testHint = node("span", "helper");
    const testNote = node("p", "helper model-settings-test-note", "验证会向已保存的服务商发送一次固定的简短测试文本，消耗少量 API 额度；不会发送讲义、卡片或回答。仅检查文字请求与 JSON 输出，图片能力需另行确认。");
    const connectionResult = node("div", "notice model-settings-test-result");
    connectionResult.setAttribute("role", "status");
    connectionResult.setAttribute("aria-live", "polite");
    let savedSettings = null;
    let connectionState = { status: "unverified" };
    let connectionPoll;
    const actions = node("div", "model-settings-actions");
    actions.append(save, test, testHint);
    controls.append(modeGroup, modeHint, connection, advanced, storageNote, actions, testNote, connectionResult);
    form.append(controls);
    body.append(loading, error, retry, feedback, form);
    panel.append(header, body);
    let busy = false;
    let keyConfigured = false;

    function isDirty() {
      return !savedSettings || savedSettings.mode !== (modes.cloud.checked ? "cloud" : "demo")
        || savedSettings.base_url !== baseUrl.value.trim() || savedSettings.model !== model.value.trim()
        || savedSettings.timeout_seconds !== Number(timeout.value) || Boolean(key.value.trim()) || clearKey.checked;
    }
    function updateTestAvailability() {
      const dirty = isDirty();
      test.disabled = busy || dirty || savedSettings?.mode !== "cloud" || !savedSettings?.configured || connectionState.status === "testing";
      testHint.textContent = dirty ? "修改后请先保存，再验证连接。"
        : savedSettings?.mode !== "cloud" ? "演示模式无需验证连接。"
          : !savedSettings?.configured ? "请先补全并保存 API 地址、模型和密钥。" : "验证已保存的模型配置。";
    }
    function showConnection(value, followPending = false) {
      clearTimeout(connectionPoll);
      connectionState = value || { status: "unverified" };
      connectionResult.dataset.connection = connectionState.status;
      connectionResult.textContent = connectionState.status === "testing" ? "正在验证连接，请等待模型响应…"
        : connectionState.status === "success" ? `${connectionState.message} 耗时 ${(connectionState.latency_ms / 1000).toFixed(1)} 秒。仅代表本次测试通过。`
          : connectionState.status === "error" ? `验证失败：${connectionState.message}`
            : savedSettings?.mode === "demo" ? "演示模式使用本地规则，无需验证连接。" : "当前配置尚未验证连接。";
      if (connectionState.checked_at) connectionResult.textContent += ` 上次验证：${new Date(connectionState.checked_at).toLocaleString()}。`;
      updateTestAvailability();
      if (typeof onConnection === "function") onConnection(connectionState);
      // Returning to settings during a test only reads its status; it never
      // starts another model request or replaces an unsaved form draft.
      if (followPending && connectionState.status === "testing") {
        connectionPoll = setTimeout(async () => {
          if (!panel.isConnected) return;
          try {
            const latest = await api(endpoint);
            if (panel.isConnected) showConnection(latest.connection, true);
          } catch {
            if (panel.isConnected) showConnection({ status: "error", message: "无法读取验证结果，请刷新页面查看。" });
          }
        }, 1000);
      }
    }
    function updateModeHint() {
      modeHint.textContent = modes.cloud.checked
        ? "云端模式会向你配置的服务商发送相关原文与回答；主动检查配图时，还会发送所选图片及原页。"
        : "演示模式使用本地规则，不调用模型。你也可以先保存服务商配置，之后再切换到自配 API。";
    }
    function updateKeyState() {
      clearKeyLabel.hidden = !keyConfigured;
      key.disabled = busy || clearKey.checked;
      keyStatus.textContent = keyConfigured ? "已保存" : "未配置";
      keyStatus.className = `pill model-settings-key-status${keyConfigured ? " green" : ""}`;
      key.placeholder = clearKey.checked ? "保存后将清除已有密钥" : keyConfigured ? "留空保留已保存的密钥" : "粘贴你的 API Key";
      keyHelp.textContent = clearKey.checked ? "已选择清除密钥；保存后云端模式需要重新配置密钥才能使用。"
        : keyConfigured ? "已有密钥不会回显；只有需要替换时才填写。" : "密钥仅用于向你配置的服务商发起请求。可以先留空，稍后补充。";
    }
    function setBusy(value) {
      busy = value;
      controls.disabled = value;
      panel.setAttribute("aria-busy", String(value));
      retry.disabled = value;
      updateKeyState();
      updateTestAvailability();
    }
    function applySettings(settings) {
      modes.cloud.checked = settings.mode === "cloud";
      modes.demo.checked = !modes.cloud.checked;
      baseUrl.value = settings.base_url || "";
      model.value = settings.model || "";
      timeout.value = String(settings.timeout_seconds ?? 60);
      key.value = "";
      keyConfigured = settings.api_key_configured === true;
      clearKey.checked = false;
      savedSettings = settings;
      badge.textContent = settings.mode === "cloud" ? settings.configured ? "云端已配置" : "待补全配置" : "演示模式";
      badge.className = `pill${settings.mode === "cloud" ? " amber" : " green"}`;
      updateModeHint();
      updateKeyState();
      showConnection(settings.connection, true);
    }
    async function load() {
      if (busy) return;
      setBusy(true);
      error.textContent = "";
      retry.hidden = true;
      loading.hidden = false;
      key.value = "";
      try {
        applySettings(await api(endpoint));
        form.hidden = false;
      } catch (failure) {
        badge.textContent = "读取失败";
        error.textContent = failure.message || "未能读取模型设置，请重试。";
        retry.hidden = false;
      } finally {
        loading.hidden = true;
        setBusy(false);
      }
    }
    retry.addEventListener("click", load);
    form.addEventListener("input", () => {
      feedback.hidden = true;
      error.textContent = "";
      updateTestAvailability();
    });
    for (const radio of Object.values(modes)) radio.addEventListener("change", updateModeHint);
    clearKey.addEventListener("change", () => {
      if (clearKey.checked) key.value = "";
      updateKeyState();
      updateTestAvailability();
    });
    test.addEventListener("click", async () => {
      if (test.disabled || busy || isDirty()) return;
      setBusy(true);
      test.textContent = "正在验证…";
      error.textContent = "";
      feedback.hidden = true;
      showConnection({ status: "testing" });
      try {
        showConnection(await api(`${endpoint}/test`, jsonOptions("POST", {})));
      } catch (failure) {
        showConnection({ status: "error", message: failure.message || "连接验证未完成，请重试。" });
      } finally {
        setBusy(false);
        test.textContent = "验证连接";
      }
    });
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (busy || !form.reportValidity()) return;
      const replacingKey = Boolean(key.value.trim());
      const request = jsonOptions("PUT", {
        mode: modes.cloud.checked ? "cloud" : "demo",
        base_url: baseUrl.value.trim(),
        model: model.value.trim(),
        timeout_seconds: Number(timeout.value),
        api_key: clearKey.checked ? "" : key.value.trim(),
        clear_api_key: clearKey.checked
      });
      // Keep the credential only in the pending request, never in saved UI state.
      key.value = "";
      setBusy(true);
      save.textContent = "正在保存…";
      error.textContent = "";
      feedback.hidden = true;
      let settings;
      try {
        settings = await api(endpoint, request);
        applySettings(settings);
        feedback.className = `notice model-settings-feedback${settings.mode === "cloud" && !settings.configured ? " warning" : ""}`;
        feedback.textContent = settings.mode === "cloud" && !settings.configured
          ? "设置已保存。API 地址、模型名称或密钥尚未填完整，补全后即可使用云端模式。"
          : "设置已保存，将用于后续生成与分析。尚未验证模型连通性。";
        feedback.hidden = false;
      } catch (failure) {
        error.textContent = (failure.message || "设置未能保存，请稍后重试。")
          + (replacingKey ? " 本次输入的密钥已清空；重试前请重新填写。" : "");
      } finally {
        request.body = "";
        setBusy(false);
        save.textContent = "保存设置";
      }
      if (settings && typeof onSaved === "function") {
        try { await onSaved(settings); }
        catch {
          feedback.className = "notice warning model-settings-feedback";
          feedback.textContent = "设置已保存，但工作台状态暂未刷新。请刷新页面查看，无需重复保存。";
          feedback.hidden = false;
        }
      }
    });
    load();
    return panel;
  }

  return Object.freeze({ create, renderStatus });
});
