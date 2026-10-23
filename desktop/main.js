"use strict";

// Thin Electron shell for the AnkiSense web app.
//
// This file owns no web content. It either connects to an already-running
// backend (`--dev`), or spawns the FastAPI backend itself, then loads the
// same local URL in a native window. The web UI keeps living in
// ../ankisense/static and ../ankisense/*.py; any change there shows up here
// on the next reload.

const { app, BrowserWindow, dialog, ipcMain, Menu, session, shell } = require("electron");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const fs = require("fs");

const HOST = "127.0.0.1";
const PORT = 8765; // Keep in sync with the backend default in start.ps1.
const URL = `http://${HOST}:${PORT}`;
const READY_TIMEOUT_MS = 30_000;

const PROJECT_ROOT = path.resolve(__dirname, "..");

let backendProc = null;
let mainWindow = null;

function isInternal(url) {
  return url.startsWith("http://127.0.0.1:") || url.startsWith("http://localhost:");
}

function backendCommand() {
  if (app.isPackaged) {
    const name = process.platform === "win32" ? "ankisense-backend.exe" : "ankisense-backend";
    return {
      executable: path.join(process.resourcesPath, "backend", name),
      args: ["--host", HOST, "--port", String(PORT)],
      cwd: path.dirname(process.execPath),
    };
  }
  const scriptDir = process.platform === "win32" ? "Scripts" : "bin";
  const exe = process.platform === "win32" ? "python.exe" : "python";
  return {
    executable: path.join(PROJECT_ROOT, ".venv", scriptDir, exe),
    args: ["-m", "uvicorn", "ankisense.app:app", "--host", HOST, "--port", String(PORT)],
    cwd: PROJECT_ROOT,
  };
}

function backendIsUp() {
  return new Promise((resolve) => {
    const req = http.get({ host: HOST, port: PORT, path: "/", timeout: 1000 }, () => {
      resolve(true);
    });
    req.on("error", () => resolve(false));
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function waitForBackend() {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (await backendIsUp()) return true;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}

function spawnBackend() {
  const { executable, args, cwd } = backendCommand();
  if (!fs.existsSync(executable)) {
    throw new Error(`Backend not found at ${executable}. Run .\\setup.ps1 in the project root first.`);
  }
  const env = { ...process.env };
  if (app.isPackaged) {
    // Installed app has no project ./data; point the backend at a writable,
    // per-user data directory instead of the read-only install location.
    env.ANKISENSE_DATA_DIR = path.join(app.getPath("userData"), "data");
  }
  backendProc = spawn(
    executable,
    args,
    { cwd, windowsHide: true, env }
  );
  backendProc.stdout.on("data", (chunk) => process.stdout.write(`[backend] ${chunk}`));
  backendProc.stderr.on("data", (chunk) => process.stderr.write(`[backend] ${chunk}`));
  backendProc.on("error", (error) => process.stderr.write(`[backend] ${error.message}\n`));
  backendProc.on("exit", () => {
    backendProc = null;
  });
}

function stopBackend() {
  if (!backendProc) return;
  backendProc.kill();
  backendProc = null;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 960,
    minHeight: 640,
    frame: false,
    title: "AnkiSense",
    backgroundColor: "#f6f7f9",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  // Internal practice links stay in the app; external links open in the browser.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (isInternal(url)) return { action: "allow" };
    shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (isInternal(url)) return;
    event.preventDefault();
    shell.openExternal(url);
  });

  mainWindow.loadURL(URL)
    .then(() => console.log(`AnkiSense window loaded from ${URL}`))
    .catch((err) => {
      dialog.showErrorBox(
        "AnkiSense",
        `Could not open the AnkiSense app at ${URL}.\n\n${err.message}`
      );
    });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
  mainWindow.on("maximize", () => mainWindow.webContents.send("window:maximize-change", true));
  mainWindow.on("unmaximize", () => mainWindow.webContents.send("window:maximize-change", false));
}

function windowFor(event) {
  return BrowserWindow.fromWebContents(event.sender);
}

ipcMain.on("window:minimize", (event) => windowFor(event)?.minimize());
ipcMain.on("window:maximize", (event) => {
  const win = windowFor(event);
  if (!win) return;
  if (win.isMaximized()) win.unmaximize();
  else win.maximize();
});
ipcMain.on("window:close", (event) => windowFor(event)?.close());

app.whenReady().then(async () => {
  // No File/Edit/View/Window menu; the app is a single-purpose shell.
  Menu.setApplicationMenu(null);

  const spawnFlag = process.argv.includes("--spawn-backend");
  const devFlag = process.argv.includes("--dev");

  if (spawnFlag) {
    spawnBackend();
  } else if (!devFlag) {
    // Default: reuse a running backend if present, otherwise start one.
    if (!(await backendIsUp())) spawnBackend();
  }
  // In --dev mode, assume the backend is already running; loadURL surfaces a
  // friendly error if it is not.

  if (backendProc && !(await waitForBackend())) {
    dialog.showErrorBox(
      "AnkiSense",
      `Backend did not become ready on ${URL} within ${READY_TIMEOUT_MS / 1000}s.`
    );
    app.quit();
    return;
  }

  await session.defaultSession.clearCache();

  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", stopBackend);
