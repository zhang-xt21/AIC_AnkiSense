"use strict";

// Frameless-window title bar.
//
// Injects the min/max/close controls into the page's own `.topbar` so the
// page source files stay untouched, and exposes a small window-control API
// (`window.ankisenseDesktop`) in case the page wants to reuse it later.

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("ankisenseDesktop", {
  minimize: () => ipcRenderer.send("window:minimize"),
  maximize: () => ipcRenderer.send("window:maximize"),
  close: () => ipcRenderer.send("window:close"),
  ankiSetup: async (action) => {
    const response = await ipcRenderer.invoke("anki:setup", action);
    if (!response.ok) throw new Error(response.error);
    return response.result;
  },
  onMaximizeChange: (callback) => {
    ipcRenderer.on("window:maximize-change", (_event, maximized) => callback(maximized));
  },
});

const STYLE = `
.window-controls { display: none; }
.ankisense-electron,
.ankisense-electron body {
  scrollbar-gutter: auto !important;
}
.ankisense-electron::-webkit-scrollbar,
.ankisense-electron body::-webkit-scrollbar {
  display: none !important;
  width: 0 !important;
  height: 0 !important;
}
.ankisense-electron .topbar { -webkit-app-region: drag; justify-content: flex-start; position: sticky; top: 0; z-index: 1000; background: #ffffff; box-shadow: 0 1px 2px rgba(15, 23, 42, 0.06); }
.ankisense-electron .mode-badge { margin-left: auto; }
.ankisense-electron .window-controls {
  -webkit-app-region: no-drag;
  display: flex;
  align-items: center;
  gap: 2px;
  margin-left: 12px;
  flex-shrink: 0;
}
.window-controls .wc-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 30px;
  padding: 0;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: #5b6b84;
  cursor: pointer;
}
.window-controls .wc-btn:hover { background: rgba(0, 0, 0, 0.06); color: #1f2a37; }
.window-controls .wc-close:hover { background: #d64541; color: #fff; }
.window-controls .wc-btn svg { width: 15px; height: 15px; }
.window-controls .wc-restore { display: none; }
.ankisense-electron.maximized .window-controls .wc-max { display: none; }
.ankisense-electron.maximized .window-controls .wc-restore { display: block; }
`;

function icon(paths) {
  return `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${paths}</svg>`;
}

function injectTitleBar() {
  if (document.querySelector(".window-controls")) return;

  document.documentElement.classList.add("ankisense-electron");

  const style = document.createElement("style");
  style.textContent = STYLE;
  document.head.appendChild(style);

  const controls = document.createElement("div");
  controls.className = "window-controls";
  controls.innerHTML =
    `<button class="wc-btn wc-minimize" title="最小化" aria-label="最小化">` + icon('<path d="M5 12h14"/>') + `</button>` +
    `<button class="wc-btn wc-maximize" title="最大化" aria-label="最大化">` +
      `<span class="wc-max">` + icon('<rect x="5" y="5" width="14" height="14" rx="1"/>') + `</span>` +
      `<span class="wc-restore">` + icon('<rect x="4" y="8" width="12" height="12" rx="1"/><path d="M8 8V5a1 1 0 0 1 1-1h7a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1h-3"/>') + `</span>` +
    `</button>` +
    `<button class="wc-btn wc-close" title="关闭" aria-label="关闭">` + icon('<path d="M6 6l12 12M18 6L6 18"/>') + `</button>`;

  const topbar = document.querySelector(".topbar");
  if (!topbar) return; // Layout changed; skip rather than misplace the buttons.
  topbar.appendChild(controls);

  controls.querySelector(".wc-minimize").addEventListener("click", () => ipcRenderer.send("window:minimize"));
  controls.querySelector(".wc-maximize").addEventListener("click", () => ipcRenderer.send("window:maximize"));
  controls.querySelector(".wc-close").addEventListener("click", () => ipcRenderer.send("window:close"));

  ipcRenderer.on("window:maximize-change", (_event, maximized) => {
    document.documentElement.classList.toggle("maximized", maximized);
    const maxBtn = controls.querySelector(".wc-maximize");
    if (maxBtn) maxBtn.title = maximized ? "还原" : "最大化";
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", injectTitleBar);
} else {
  injectTitleBar();
}
