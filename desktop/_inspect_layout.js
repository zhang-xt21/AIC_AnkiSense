"use strict";
const { app, BrowserWindow } = require("electron");

const dump = `(() => {
  const head = document.querySelector(".page-heading");
  if (!head) return null;
  const cs = getComputedStyle(head);
  const walk = (n, depth) => {
    const r = n.getBoundingClientRect();
    const c = getComputedStyle(n);
    const info = {
      tag: n.tagName.toLowerCase(),
      cls: typeof n.className === "string" ? n.className : "",
      text: n.children.length === 0 ? (n.textContent || "").slice(0, 60) : "",
      rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
      scrollH: n.scrollWidth, clientW: n.clientWidth, wrap: n.scrollWidth > n.clientWidth + 1,
      lineH: c.lineHeight, fontSize: c.fontSize, whiteSpace: c.whiteSpace,
    };
    if (depth < 4) info.children = Array.from(n.children).map(ch => walk(ch, depth + 1));
    return info;
  };
  return {
    heading: { rect: (() => { const r = head.getBoundingClientRect(); return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) }; })(), gap: cs.gap, align: cs.alignItems },
    tree: walk(head, 0),
  };
})()`;

app.whenReady().then(async () => {
  const win = new BrowserWindow({ show: false, width: 1280, height: 860 });
  await win.webContents.session.clearCache();
  await win.loadURL("http://127.0.0.1:8765");
  await new Promise(r => setTimeout(r, 1200));
  const materials = await win.webContents.executeJavaScript(dump);
  await win.webContents.executeJavaScript(`document.querySelector('[data-view="review"]').click()`);
  await new Promise(r => setTimeout(r, 600));
  const review = await win.webContents.executeJavaScript(dump);
  console.log("=== MATERIALS ==="); console.log(JSON.stringify(materials, null, 1));
  console.log("=== REVIEW ==="); console.log(JSON.stringify(review, null, 1));
  app.quit();
});
