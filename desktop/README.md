# AnkiSense Windows 桌面版

Electron 只负责本地服务、原生窗口和窗口控制。网页与后端共用 `ankisense/` 的实现；默认选择 PPT/PPTX/PDF 后自动处理，卡牌结果页可查看、编辑、练习和导出。张数、配图与补充制卡位于高级选项。

## 开发运行

先在根目录创建 `.venv` 并安装 `requirements.lock.txt`，然后在 `desktop/` 执行：

```powershell
npm ci --cache ../.ui-checks/npm-cache
# 若当前 npm 阻止依赖安装脚本，用锁定的 electron 安装器补齐二进制：
node node_modules/electron/install.js
npm start
```

默认检查 `http://127.0.0.1:8765/health` 的 `application=AnkiSense`、`api_version=1`、`status=ok`；仅复用兼容服务。端口被其他程序占用时显示错误，不把任意 HTTP 页面当成本应用。没有服务时启动项目 `.venv` 后端。

- `npm run start:dev`：只连接已运行的兼容服务。
- `npm run start:packaged`：强制启动自己的服务，端口已占用时提示关闭旧服务。
- 同一用户配置目录只运行一个桌面实例。关闭窗口只停止本实例创建的后台进程，复用的服务继续运行。

安装版把数据保存在 Electron 的用户数据目录下的 `data/`，不写安装目录。启动失败会显示可读提示，诊断日志位于用户数据目录的 `startup.log`。

## 构建独立后端和安装包

在 `desktop/` 执行：

```powershell
../.venv/Scripts/python.exe -m pip install --cache-dir ../.ui-checks/pip-cache -r requirements-build.txt
npm ci --cache ../.ui-checks/npm-cache
node node_modules/electron/install.js
npm run build:backend
npm run pack     # Windows 可运行目录：dist/win-unpacked/
npm run dist     # NSIS 安装包：dist/AnkiSense Setup *.exe
```

`pack` 和 `dist` 都会先重建 Python 后端，避免安装包误用旧源码。`build-backend.ps1` 把 PyInstaller 缓存限制在忽略的 `desktop/build/` 中。后端包含静态资源、`ankisense/workers/powerpoint-export.ps1`、RapidOCR 模型与 ONNX Runtime DLL。`electron-builder` 把它复制到安装程序的 `resources/backend/`。不打包开发数据库、用户设置、API Key 或 `.env`。

PPT/PPTX 转 PDF 仍需要机器安装 PowerPoint 或 LibreOffice，缺少时请先另存为 PDF。基础后端包含 PDF 识别与 OCR；可选 Docling 环境及其布局模型不包含在这个基础安装包中。没有该组件时，依赖本地图块的自动配图会给出待处理状态；可以手动上传/裁剪配图，不把整页静默替代题图。

当前未配置代码签名证书。正式分发前还需在干净 Windows 环境检查安装、卸载、Office/LibreOffice 转换、真实 OCR 和升级后数据保留；通过开发窗口测试不等于这些安装验收全部完成。

若下载需要本机代理，只设置当前 PowerShell 进程变量，不修改系统代理：

```powershell
$env:HTTP_PROXY = 'http://127.0.0.1:7892'
$env:HTTPS_PROXY = 'http://127.0.0.1:7892'
$env:ELECTRON_GET_USE_PROXY = '1'
$env:GLOBAL_AGENT_HTTP_PROXY = 'http://127.0.0.1:7892'
$env:ELECTRON_CACHE = Join-Path (Resolve-Path '..') '.ui-checks/electron-cache'
$env:ELECTRON_BUILDER_CACHE = Join-Path (Resolve-Path '..') '.ui-checks/electron-builder-cache'
```

## 隔离验收

在根目录执行 `node tools/integrated_desktop_check.cjs`，使用本机 HTTP 样例验证单一默认入口、数量/配图参数、两类批准展示、题背图、补跑、草稿保护和响应式布局，不读取真实材料或调用云端模型。浏览器可通过 `CHROME_PATH` 指定本机 Chrome。

在 `desktop/` 执行：

```powershell
npm test
npm run test:native
npm run test:frozen
```

原生验收使用随机空闲端口、隐藏窗口、单独的 `.ui-checks/native-desktop/` 用户目录和演示模式，检查真实后端、静态资源、窗口按钮、渲染器隔离以及退出后的进程清理。`test:frozen` 需要先构建后端。报告和截图保存在对应 `.ui-checks/` 目录。

`ANKISENSE_DESKTOP_PORT` 和 `ANKISENSE_DESKTOP_USER_DATA` 仅用于显式覆盖端口/用户目录（主要供隔离测试）；一般用户无需设置。
