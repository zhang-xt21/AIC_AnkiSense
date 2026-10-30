"use strict";
// Exercise the user's existing renderer with future generated formula formats.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { chromium } = require("playwright");
const { project, browserOptions } = require("./browser_runtime.cjs");
const directory = path.join(project, ".ui-checks", "study-quality-math", new Date().toISOString().replace(/[:.]/g, "-"));
fs.mkdirSync(directory, { recursive: true });
const staticRoot = path.join(project, "ankisense", "static");
const results = [], requests = [];
let browser;
const server = http.createServer((request, response) => {
  const url = new URL(request.url, "http://127.0.0.1");
  if (url.pathname === "/") {
    response.setHeader("Content-Type", "text/html; charset=utf-8");
    return response.end('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><link rel="stylesheet" href="/static/styles.css"><link rel="stylesheet" href="/static/vendor/katex/katex.min.css"><script defer src="/static/vendor/katex/katex.min.js"></script><script defer src="/static/math-text.js"></script></head><body><main><h1>公式与独立题干检查</h1><p id="question"></p><p id="answer"></p><p id="rubric"></p><pre id="code"></pre><textarea id="editor"></textarea><p id="unsafe"></p><p id="prices"></p></main></body></html>');
  }
  const target = path.resolve(staticRoot, "." + url.pathname.replace(/^\/static/, ""));
  if (!target.startsWith(staticRoot + path.sep) || !fs.existsSync(target) || !fs.statSync(target).isFile()) {
    response.writeHead(404); return response.end();
  }
  const ext = path.extname(target);
  response.setHeader("Content-Type", ({ ".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2", ".ttf": "font/ttf" })[ext] || "application/octet-stream");
  response.end(fs.readFileSync(target));
});
(async () => {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch(browserOptions());
  const page = await browser.newPage({ viewport: { width: 1050, height: 780 } });
  await page.route("**/*", route => {
    requests.push(route.request().url());
    return route.request().url().startsWith(base + "/") ? route.continue() : route.abort();
  });
  await page.goto(base);
  await page.waitForFunction(() => window.MathText && window.katex);
  const result = await page.evaluate(() => {
    const question = String.raw`设 \(V=\{(x,y,0):x,y\in\mathbb{R}\}\)，\(u=(0,0,1)\)，\(u+V\) 表示什么平面？`;
    const answer = String.raw`答案：\[u+V=\{(x,y,1):x,y\in\mathbb{R}\}\]`;
    const rubric = String.raw`说明平面 $z=1$。`;
    for (const [id, value] of [["question", question], ["answer", answer], ["rubric", rubric]]) MathText.set(document.getElementById(id), value);
    const code = "for(exp1; exp2; exp3) { a[i] = i; }";
    MathText.set(document.getElementById("code"), code);
    MathText.set(document.getElementById("editor"), String.raw`$\frac{a}{b}$`);
    MathText.set(document.getElementById("unsafe"), String.raw`$\htmlClass{external}{x}$`);
    MathText.set(document.getElementById("prices"), "$5 and $10");
    return {
      rendered: document.querySelectorAll("#question .katex, #answer .katex, #rubric .katex").length,
      display: document.querySelectorAll("#answer .katex-display").length,
      code: document.getElementById("code").textContent,
      codeMath: document.querySelectorAll("#code .katex").length,
      editor: document.getElementById("editor").textContent,
      unsafeNodes: document.querySelectorAll("#unsafe .external").length,
      prices: document.getElementById("prices").textContent,
      mathErrors: document.querySelectorAll("#question .math-source-fallback, #answer .math-source-fallback, #rubric .math-source-fallback").length,
    };
  });
  assert.equal(result.rendered, 5); assert.equal(result.display, 1); assert.equal(result.mathErrors, 0);
  results.push("Self-contained question, answer and rubric render inline and block formulas");
  assert.equal(result.code, "for(exp1; exp2; exp3) { a[i] = i; }"); assert.equal(result.codeMath, 0);
  assert.equal(result.editor, String.raw`$\frac{a}{b}$`);
  results.push("Code and editable formula source remain unchanged");
  assert.equal(result.unsafeNodes, 0); assert.equal(result.prices, "$5 and $10");
  results.push("Untrusted commands cannot create trusted HTML and prices remain prose");
  await page.screenshot({ path: path.join(directory, "formula-preview.png"), fullPage: true });
  assert(requests.every(url => url.startsWith(base + "/")));
  fs.writeFileSync(path.join(directory, "report.json"), JSON.stringify({ passed: true, results, requests, real_model_calls: 0 }, null, 2));
  console.log(JSON.stringify({ directory, results, real_model_calls: 0 }));
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  server.closeAllConnections?.(); server.close();
});
