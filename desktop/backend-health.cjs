"use strict";
const http = require("node:http");
const API_VERSION = 1;
// Identify this API, never infer readiness from an arbitrary listening port.
function probeBackend({ host = "127.0.0.1", port = 8765, timeout = 1000, requireAnkiSetup = false, requireConnectionTest = false, requireTaskSwitch = false, requireLargeExports = false, requireAppVersion = null } = {}) {
  return new Promise(resolve => {
    let done = false;
    const finish = result => { if (!done) { done = true; resolve(result); } };
    const req = http.get({ host, port, path: "/health", timeout }, response => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", chunk => {
        body += chunk;
        if (body.length > 8192) { response.destroy(); finish({ responding: true, ready: false, reason: "健康检查响应过大" }); }
      });
      response.on("error", () => finish({ responding: true, ready: false, reason: "健康检查响应中断" }));
      response.on("end", () => {
        let health;
        try { health = JSON.parse(body); } catch { /* Identity mismatch below. */ }
        const identity = response.statusCode === 200 && health?.application === "AnkiSense" && health.api_version === API_VERSION && health.status === "ok";
        const versionMatches = !requireAppVersion || health?.app_version === requireAppVersion;
        const ready = identity && versionMatches && (!requireAnkiSetup || health.capabilities?.anki_setup === true)
          && (!requireConnectionTest || health.capabilities?.ai_connection_test === true)
          && (!requireTaskSwitch || health.capabilities?.ai_task_switch === true)
          && (!requireLargeExports || health.capabilities?.large_exports === true);
        finish({ responding: true, ready, reason: ready ? "" : identity ? versionMatches ? "旧版 AnkiSense 服务仍在运行，请退出旧版桌面端或停止旧服务后重新打开。" : "后台服务版本与当前客户端不一致，请退出其他版本的 AnkiSense 或停止旧服务后重新打开。" : "端口由其他服务或不兼容的 AnkiSense 占用", health });
      });
    });
    req.on("error", error => finish({ responding: error.code !== "ECONNREFUSED", ready: false, reason: error.message }));
    req.on("timeout", () => { finish({ responding: true, ready: false, reason: "健康检查超时" }); req.destroy(); });
  });
}
module.exports = { API_VERSION, probeBackend };
