"use strict";
const { app, BrowserWindow, dialog, ipcMain, Menu, session, shell } = require("electron");
const { spawn } = require("node:child_process");
const path = require("node:path");
const fs = require("node:fs");
const { probeBackend } = require("./backend-health.cjs");
const { createAnkiIntegration } = require("./anki-integration.cjs");
const HOST = "127.0.0.1";
const requestedPort = Number(process.env.ANKISENSE_DESKTOP_PORT || 8765);
const PORT = Number.isInteger(requestedPort) && requestedPort > 0 && requestedPort <= 65535 ? requestedPort : 8765;
const BASE_URL = `http://${HOST}:${PORT}`;
const READY_TIMEOUT_MS = 60_000;
const PROJECT_ROOT = path.resolve(__dirname, "..");
let backendProc = null, backendFailure = "", backendLog = "", mainWindow = null, quitting = false;
let ankiIntegration;
if (process.env.ANKISENSE_DESKTOP_USER_DATA) app.setPath("userData", path.resolve(process.env.ANKISENSE_DESKTOP_USER_DATA));
function isInternal(value) {
  try { return new URL(value).origin === BASE_URL; } catch { return false; }
}
function backendCommand() {
  if (app.isPackaged || process.argv.includes("--frozen-backend")) {
    const name = process.platform === "win32" ? "ankisense-backend.exe" : "ankisense-backend";
    const directory = app.isPackaged ? path.join(process.resourcesPath, "backend") : path.join(__dirname, "dist", "ankisense-backend");
    return { executable: path.join(directory, name), args: ["--host", HOST, "--port", String(PORT)], cwd: directory };
  }
  return {
    executable: path.join(PROJECT_ROOT, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python"),
    args: ["-m", "uvicorn", "ankisense.app:app", "--host", HOST, "--port", String(PORT)], cwd: PROJECT_ROOT,
  };
}
function spawnBackend() {
  const { executable, args, cwd } = backendCommand();
  if (!fs.existsSync(executable)) throw new Error("找不到本地后端。开发环境请先运行 setup.ps1；安装版请重新安装完整程序。");
  const env = { ...process.env };
  if (app.isPackaged || process.env.ANKISENSE_DESKTOP_USER_DATA) env.ANKISENSE_DATA_DIR = path.join(app.getPath("userData"), "data");
  backendFailure = "";
  const child = spawn(executable, args, { cwd, windowsHide: true, env });
  backendProc = child;
  const log = chunk => { backendLog = (backendLog + chunk.toString()).slice(-16000); };
  child.stdout.on("data", log); child.stderr.on("data", log);
  child.on("error", error => { backendFailure = error.message; });
  child.on("exit", code => { if (backendProc === child) backendProc = null; if (!quitting) backendFailure = `本地后端已退出（${code ?? "未知状态"}）。`; });
}
async function waitForBackend() {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (backendFailure) throw new Error(backendFailure);
    const result = await probeBackend({ host: HOST, port: PORT, requireAnkiSetup: true, requireConnectionTest: true, requireTaskSwitch: true });
    if (result.ready) return;
    if (result.responding) throw new Error(result.reason);
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error("本地后端启动超时，请关闭程序后重试。");
}
function stopBackend() {
  quitting = true;
  // Only this instance's own child is terminated. Reused services are not owned.
  const child = backendProc; backendProc = null;
  if (!child || child.exitCode !== null || !child.pid) return;
  if (process.platform === "win32") spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" });
  else child.kill();
}
function openExternal(value) {
  try { if (["https:", "http:", "mailto:"].includes(new URL(value).protocol)) shell.openExternal(value); } catch { /* Ignore malformed navigation. */ }
}
async function createWindow() {
  const win = new BrowserWindow({
    width: 1280, height: 860, minWidth: 960, minHeight: 640, frame: false,
    show: !process.argv.includes("--hidden"), title: "AnkiSense", backgroundColor: "#f6f7f9",
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  mainWindow = win;
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isInternal(url)) return { action: "allow", overrideBrowserWindowOptions: { webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true } } };
    openExternal(url); return { action: "deny" };
  });
  win.webContents.on("will-navigate", (event, url) => { if (!isInternal(url)) { event.preventDefault(); openExternal(url); } });
  win.on("closed", () => { if (mainWindow === win) mainWindow = null; });
  win.on("maximize", () => win.webContents.send("window:maximize-change", true));
  win.on("unmaximize", () => win.webContents.send("window:maximize-change", false));
  await win.loadURL(BASE_URL);
}
function windowFor(event) { return BrowserWindow.fromWebContents(event.sender); }
ipcMain.on("window:minimize", event => windowFor(event)?.minimize());
ipcMain.on("window:maximize", event => { const win = windowFor(event); if (win) win.isMaximized() ? win.unmaximize() : win.maximize(); });
ipcMain.on("window:close", event => windowFor(event)?.close());
ipcMain.handle("anki:setup", async (event, action) => {
  if (event.sender !== mainWindow?.webContents || !isInternal(event.sender.getURL()) || !ankiIntegration
      || !["detect", "chooseProgram", "chooseData", "install", "launch", "revealConnection", "downloadAnki"].includes(action)) return { ok: false, error: "此操作仅能从 AnkiSense 连接向导发起。" };
  try { return { ok: true, result: await ankiIntegration[action]() }; }
  catch (error) { return { ok: false, error: error.message || "Anki 连接操作未完成。" }; }
});
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on("second-instance", (_event, argv) => { if (argv.includes("--hidden")) return; if (mainWindow) { if (mainWindow.isMinimized()) mainWindow.restore(); mainWindow.show(); mainWindow.focus(); } });
  app.whenReady().then(async () => {
    ankiIntegration = createAnkiIntegration({ userData: app.getPath("userData"), projectRoot: PROJECT_ROOT,
      backendUrl: BASE_URL, dialog, shell, getWindow: () => mainWindow });
    Menu.setApplicationMenu(null);
    const result = await probeBackend({ host: HOST, port: PORT, requireAnkiSetup: true, requireConnectionTest: true, requireTaskSwitch: true });
    if (result.responding && !result.ready) throw new Error(`${result.reason}。请释放端口 ${PORT} 后重试。`);
    if (process.argv.includes("--spawn-backend") && result.responding) throw new Error(`端口 ${PORT} 已有 AnkiSense 运行。请关闭旧服务或使用默认启动方式。`);
    if (!result.ready) {
      if (process.argv.includes("--dev")) throw new Error(`未找到开发后端，请先启动 ${BASE_URL}。`);
      spawnBackend(); await waitForBackend();
    }
    await session.defaultSession.clearCache(); await createWindow();
    app.on("activate", () => { if (!BrowserWindow.getAllWindows().length) createWindow().catch(showStartupError); });
  }).catch(showStartupError);
}
function showStartupError(error) {
  let logPath = "";
  try { const directory = app.getPath("userData"); fs.mkdirSync(directory, { recursive: true }); logPath = path.join(directory, "startup.log"); fs.writeFileSync(logPath, `${error.message}\n${backendLog}`); } catch { /* Dialog still reports failure. */ }
  dialog.showErrorBox("AnkiSense 启动未完成", `${error.message}${logPath ? `\n\n诊断日志：${logPath}` : ""}`); app.quit();
}
app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });
app.on("before-quit", stopBackend);
