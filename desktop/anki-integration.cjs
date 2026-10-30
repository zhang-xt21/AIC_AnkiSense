"use strict";
// Narrow native actions for the setup guide; no collection or API key access.
const fs = require("node:fs");
const path = require("node:path");
const { spawn, execFile } = require("node:child_process");
const { promisify } = require("node:util");
const execute = promisify(execFile);
const { showAnkiWindow } = require("./anki-window.cjs");

async function runningAnki() {
  if (process.platform !== "win32") return false;
  const { stdout } = await execute("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", "if (Get-Process -Name anki -ErrorAction SilentlyContinue) { 'running' }"], { windowsHide: true, timeout: 8000 });
  return stdout.trim() === "running";
}

function createAnkiIntegration({ userData, projectRoot, backendUrl, dialog, shell, getWindow, request = fetch,
  isRunning = runningAnki, start = spawn, showWindow = showAnkiWindow }) {
  const settingsPath = path.join(userData, "anki-setup.json");
  let selected = {};
  try { selected = JSON.parse(fs.readFileSync(settingsPath, "utf8")); } catch { /* First setup. */ }
  function save() { fs.mkdirSync(userData, { recursive: true }); fs.writeFileSync(settingsPath, JSON.stringify({ executable: selected.executable, dataDirectory: selected.dataDirectory }, null, 2)); }
  const isProgram = filename => typeof filename === "string" && path.basename(filename).toLowerCase() === "anki.exe" && fs.existsSync(filename) && fs.statSync(filename).isFile();
  async function detect() {
    let candidates = [selected.executable,
      process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, "Programs", "Anki", "anki.exe"),
      process.env.ProgramFiles && path.join(process.env.ProgramFiles, "Anki", "anki.exe"),
      path.join(projectRoot, "origin", "Anki", "anki.exe")];
    if (process.platform === "win32" && !candidates.some(isProgram)) {
      try {
        const script = "$p = Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\anki.exe','HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\anki.exe' -ErrorAction SilentlyContinue; @($p | ForEach-Object { $_.'(default)' }) | ConvertTo-Json -Compress";
        const { stdout } = await execute("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", script], { windowsHide: true, timeout: 5000 });
        const values = JSON.parse(stdout || "[]"); candidates.push(...(Array.isArray(values) ? values : [values]));
      } catch { /* The user can select an installation manually. */ }
    }
    const executable = candidates.find(isProgram) || null;
    const dataDirectory = selected.dataDirectory || (process.env.APPDATA && path.join(process.env.APPDATA, "Anki2")) || null;
    let addon = null;
    if (dataDirectory) {
      try {
        const value = JSON.parse(fs.readFileSync(path.join(dataDirectory, "addons21", "ankisense_sync", "manifest.json"), "utf8"));
        if (value.package === "ankisense_sync") addon = { installed: true, version: value.human_version };
      } catch { /* No add-on yet. */ }
    }
    const result = { executable, dataDirectory, dataReady: Boolean(dataDirectory && fs.existsSync(path.join(dataDirectory, "prefs21.db"))), addon, running: await isRunning() };
    return result;
  }
  async function chooseProgram() {
    const result = await dialog.showOpenDialog(getWindow(), { title: "选择 Anki 程序", properties: ["openFile"], filters: [{ name: "Anki", extensions: ["exe"] }] });
    if (!result.canceled && result.filePaths[0]) {
      if (!isProgram(result.filePaths[0])) throw new Error("请选择有效的 anki.exe。");
      selected.executable = result.filePaths[0]; save();
    }
    return detect();
  }
  async function chooseData() {
    const result = await dialog.showOpenDialog(getWindow(), { title: "选择 Anki 资料根目录（含 prefs21.db）", properties: ["openDirectory"] });
    if (!result.canceled && result.filePaths[0]) {
      const folder = result.filePaths[0];
      if (!fs.existsSync(path.join(folder, "prefs21.db"))) throw new Error("该目录没有 prefs21.db，请选择 Anki 资料根目录；自定义资料目录也在这里选择。");
      selected.dataDirectory = folder; save();
    }
    return detect();
  }
  async function install() {
    const current = await detect();
    if (!current.executable) throw new Error("请先安装或选择 Anki 程序。");
    if (!current.dataReady) throw new Error("请先打开一次 Anki 完成初始化，再选择资料根目录。");
    if (current.running) throw new Error("请正常退出 Anki 后安装插件，完成后再打开 Anki。");
    const response = await request(backendUrl + "/api/anki/setup/install", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ anki_base: current.dataDirectory }) });
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "插件安装未完成，请检查资料目录。");
    return { ...body, native: await detect() };
  }
  async function launch() {
    const current = await detect();
    if (!current.executable) throw new Error("尚未找到 Anki，请先安装或选择程序。");
    if (current.running) {
      let result;
      try { result = await showWindow(current.executable); }
      catch { throw new Error("Anki 已在运行，但窗口未能显示。请稍后再点「打开 Anki」，无需重新安装插件。"); }
      if (!result.visible) throw new Error("Anki 已在运行，但暂未找到可显示的主窗口。请稍后再点「打开 Anki」。");
      return { running: true, message: result.activated ? "已显示 Anki 窗口，可在「工具 → AnkiSense · 同步卡片」中继续。"
        : "Anki 窗口已显示；若仍在后台，请从任务栏切换到 Anki。" };
    }
    const args = current.dataReady ? ["-b", current.dataDirectory] : [];
    const child = start(current.executable, args, { detached: true, windowsHide: false, stdio: "ignore" });
    await new Promise((resolve, reject) => { child.once("spawn", resolve); child.once("error", () => reject(new Error("Anki 未能启动，请检查程序位置。"))); });
    child.unref();
    return { running: true, message: "正在打开 Anki；若启动较慢，请稍候。选择资料后，插件会自动检查本机连接。" };
  }
  async function revealConnection() {
    const response = await request(backendUrl + "/api/anki/setup"); const body = await response.json();
    if (!response.ok || !body.connection_file || path.basename(body.connection_file) !== "plugin-connection.json") throw new Error("请先启用本机连接。");
    shell.showItemInFolder(body.connection_file);
  }
  return { detect, chooseProgram, chooseData, install, launch, revealConnection,
    downloadAnki: () => shell.openExternal("https://apps.ankiweb.net/") };
}
module.exports = { createAnkiIntegration, runningAnki };
