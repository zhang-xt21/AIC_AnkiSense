"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { project, browserOptions } = require("./browser_runtime.cjs");
const { checkFigureCrop } = require("./figure_crop_acceptance.cjs");
const directory = path.join(project, ".ui-checks", "figure-crop", new Date().toISOString().replace(/[:.]/g, "-"));
fs.mkdirSync(directory, { recursive: true });
const results = [], errors = [];
let browser;
const server = http.createServer((request, response) => {
  const pathname = new URL(request.url, "http://fixture.local").pathname;
  if (pathname === "/") {
    response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    return response.end('<!doctype html><html lang="zh-CN"><head><link rel="stylesheet" href="/static/styles.css"><script src="/static/figure-recommendations.js"></script></head><body><header class="topbar">离线配图测试</header><main id="main"></main></body></html>');
  }
  const files = { "/static/styles.css": "styles.css", "/static/figure-recommendations.js": "figure-recommendations.js" };
  if (files[pathname]) {
    response.writeHead(200, { "Content-Type": pathname.endsWith(".js") ? "text/javascript; charset=utf-8" : "text/css; charset=utf-8" });
    return response.end(fs.readFileSync(path.join(project, "ankisense", "static", files[pathname])));
  }
  response.writeHead(404); response.end();
});
(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch(browserOptions());
  const context = await browser.newContext();
  await context.route("**/*", route => route.request().url().startsWith(`${base}/`) ? route.continue() : route.abort());
  const page = await context.newPage();
  page.on("pageerror", error => errors.push(error.message));
  for (const viewport of [{ width: 1280, height: 640 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport); await page.goto(base);
    await checkFigureCrop(page, {
      check: async (name, action) => { await action(); results.push({ name, viewport, status: "passed" }); console.log(`PASS ${viewport.width}: ${name}`); },
      screenshot: name => page.screenshot({ path: path.join(directory, `${viewport.width}-${name}`) }),
    });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
  }
  assert.deepEqual(errors, []);
})().catch(error => { errors.push(error.stack || String(error)); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  await new Promise(resolve => server.close(resolve));
  fs.writeFileSync(path.join(directory, "report.json"), JSON.stringify({ results, errors, model_requests: 0 }, null, 2));
  console.log(`REPORT ${directory}`); if (errors.length) console.error(errors.join("\n"));
});
