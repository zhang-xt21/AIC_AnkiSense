(function (root) {
  "use strict";
  function create({ api, jsonOptions }) {
    const guide = document.createElement("details"); guide.className = "anki-setup panel";
    const summary = document.createElement("summary"); summary.textContent = "直接连接 Anki · 安装与连接向导";
    const body = document.createElement("div"); body.className = "panel-body";
    guide.append(summary, body);
    let setup = null, native = null, busy = false, error = "", loadError = "", message = "", timer;
    const desktop = root.ankisenseDesktop?.ankiSetup;
    const node = (tag, content, className) => { const n = document.createElement(tag); if (content !== undefined) n.textContent = content; if (className) n.className = className; return n; };
    const button = (label, action, disabled = false) => { const b = node("button", label, "btn secondary small"); b.type = "button"; b.disabled = busy || disabled; b.addEventListener("click", () => run(action)); return b; };
    const step = (heading, description) => { const n = node("section", undefined, "anki-setup-step"); n.append(node("h3", heading), node("p", description, "helper")); return n; };
    async function run(action) {
      if (busy) return;
      busy = true; error = ""; message = "正在处理，请稍候…"; render();
      try { message = await action() || "操作完成。"; }
      catch (e) { error = e.message || "操作未完成，请重试。"; message = ""; }
      finally { busy = false; await load(true); }
    }
    async function load(detect = false) {
      const before = JSON.stringify([setup, native, error, loadError]);
      try {
        setup = await api("/api/anki/setup");
        if (desktop && (detect || !native)) native = await desktop("detect");
        loadError = "";
      } catch (e) {
        loadError = e.message || "无法读取连接状态。";
        if (setup) setup = { ...setup, connected: false };
      }
      if (detect || before !== JSON.stringify([setup, native, error, loadError])) render();
      clearTimeout(timer);
      if (guide.isConnected && guide.open) timer = setTimeout(() => load(), 3000);
    }
    async function enable() { setup = await api("/api/anki/setup/enable", jsonOptions("POST", {})); }
    function render() {
      const opened = new Set([...body.querySelectorAll("details[open]")].map(n => n.querySelector("summary")?.textContent));
      body.replaceChildren();
      if (error || loadError) { const alert = node("div", error || loadError, "notice warning"); alert.setAttribute("role", "alert"); body.append(alert); }
      if (message) { const note = node("p", message, "helper"); note.setAttribute("role", "status"); body.append(note); }
      if (!setup) { body.append(button("重新检查", () => load(true))); return; }
      const a = step("1 · 准备 Anki", desktop ? native?.executable ? `已找到：${native.executable}` : "尚未找到 Anki；已有安装可手动选择。" : "浏览器模式可下载插件；桌面版支持自动检测和安装。");
      if (desktop) {
        a.append(button("选择 Anki 程序", async () => { native = await desktop("chooseProgram"); }), button("前往 Anki 官网", () => desktop("downloadAnki")));
        const advanced = node("details"); advanced.append(node("summary", "资料位置（默认无需修改）"), node("p", native?.dataDirectory || "尚未找到资料目录", "helper"),
          node("p", "自定义或独立资料请选择含 prefs21.db 的 Anki 资料根目录。程序不会读取或修改你的学习数据库。", "helper"), button("选择 Anki 资料目录", async () => { native = await desktop("chooseData"); }));
        a.append(advanced);
        if (!native?.dataReady && native?.executable) a.append(node("p", "请先打开一次 Anki 完成初始化，再退出并刷新检查。", "helper"));
      }
      body.append(a);
      const installed = native?.addon?.installed && native.addon.version === setup.addon_version;
      const b = step("2 · 安装插件并配对", installed ? `已安装插件 ${native.addon.version}` : "首次使用需安装同步插件。");
      if (desktop) {
        if (native?.running) b.append(node("p", "Anki 正在运行。请正常退出 Anki 后刷新，再安装或更新插件。", "notice warning"));
        b.append(button(installed ? "检查并更新插件" : "安装并配对插件", async () => { await enable(); const result = await desktop("install"); native = result.native; return result.changed ? "插件已安装并配对，请重新打开 Anki。" : "插件已是当前版本，配对配置保持不变。"; }, !native?.executable || !native?.dataReady || native?.running || !setup.addon_available));
      } else {
        b.append(button(setup.connection.enabled ? "连接已启用" : "启用本机连接", async () => { await enable(); return "本机连接已启用。"; }, setup.connection.enabled));
        const download = node("a", "下载同步插件", "btn secondary small"); download.href = "/api/anki/setup/addon"; download.download = "AnkiSense-Sync.ankiaddon"; b.append(download);
        b.append(node("p", "在 Anki 的「工具 → 插件 → 从文件安装」选择下载的插件，重启 Anki。首次在同步窗口选择下方连接文件。", "helper"));
      }
      if (setup.connection_file) {
        const detail = node("details"); detail.append(node("summary", "连接文件位置（故障排查）"), node("p", setup.connection_file, "helper"));
        if (desktop) detail.append(button("打开连接文件位置", () => desktop("revealConnection")));
        b.append(detail);
      }
      body.append(b);
      const c = step("3 · 打开 Anki，检查连接", setup.connected ? `已连接 · 插件 ${setup.plugin_version}` : "等待连接，请打开 Anki 并进入资料。");
      c.dataset.connected = String(setup.connected);
      if (desktop) c.append(button("打开 Anki", () => desktop("launch").then(result => result.message), !native?.executable));
      c.append(button("刷新检查", () => load(true)));
      body.append(c);
      const d = step("4 · 选择牌组，首次同步", "在 Anki 打开「工具 → AnkiSense · 同步卡片」，选择牌组并点击「同步已批准卡片」。");
      if (setup.last_sync) {
        const s = setup.last_sync;
        const status = node("p", `最近同步：新增 ${s.added} · 更新 ${s.updated} · 未变化 ${s.unchanged} · 冲突 ${s.conflicts} · 跳过 ${s.excluded}`, "notice anki-sync-result");
        status.setAttribute("role", "status"); d.append(status);
        d.append(node("p", new Date(s.at * 1000).toLocaleString(), "helper"));
      } else d.append(node("p", "尚未收到同步完成回执。", "helper"));
      body.append(d);
      const help = node("details", undefined, "help-details");
      help.append(node("summary", "同步说明与故障排查"),
        node("p", "同步图文卡片会保留复习进度，不调用云端模型。", "helper"),
        node("p", "仍未连接时，检查 Anki「工具 → 插件」是否启用 AnkiSense，并确认已进入资料。", "helper"));
      body.append(help);
      for (const detail of body.querySelectorAll("details")) detail.open = opened.has(detail.querySelector("summary")?.textContent);
    }
    guide.addEventListener("toggle", () => {
      clearTimeout(timer);
      if (guide.open) load(true);
    });
    body.append(node("p", "正在读取连接状态…", "helper"));
    return guide;
  }
  root.AnkiSetup = { create };
})(typeof globalThis !== "undefined" ? globalThis : this);
