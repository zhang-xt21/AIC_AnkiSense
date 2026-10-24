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

默认检查 `http://127.0.0.1:8765/health` 的应用标识、API 版本、`capabilities.anki_setup` 和 `capabilities.ai_connection_test`；仅复用同时支持 Anki 连接向导和模型连接验证的兼容服务。旧服务占用端口时提示退出旧版，不会静默连接到旧界面。没有服务时启动项目 `.venv` 后端。界面顶部显示后端版本。

- `npm run start:dev`：只连接已运行的兼容服务。
- `npm run start:packaged`：强制启动自己的服务，端口已占用时提示关闭旧服务。
- 同一用户配置目录只运行一个桌面实例。关闭窗口只停止本实例创建的后台进程，复用的服务继续运行。

安装版把数据保存在 Electron 的用户数据目录下的 `data/`，不写安装目录。启动失败会显示可读提示，诊断日志位于用户数据目录的 `startup.log`。

## 连接 Anki

在「导出至 Anki」展开连接向导：检测或选择 Anki → 退出 Anki 后安装并配对插件 → 打开 Anki 等待连接回执 → 在 Anki「工具 → AnkiSense · 同步卡片」选择牌组并同步。安装包包含插件文件，不包含 Anki 主程序；没有 Anki 可通过向导前往官网安装，也可继续独立学习或导出 APKG。

首次需启动一次 Anki 初始化资料。自定义资料位置可选择含 `prefs21.db` 的根目录。向导只更新本插件并备份旧文件，不打开学习数据库；仅记录本机连接文件路径，不复制模型密钥。插件心跳有效期为 90 秒，安装成功不会显示已连接。同步结果来自插件完成写入后的计数回执。

## 配置模型 API

在「模型设置」填写 Base URL、模型名和 API Key，先保存，再点击「验证连接」。保存不会调用模型；验证只发送一次固定短文本，不发送学习材料。界面显示验证状态、耗时和失败原因，修改配置或重启服务后回到未验证状态。文字与 JSON 测试通过不代表图片能力已验证。

更换服务商须提供新密钥或清除旧密钥；密钥留空会保留同一服务商的已有配置。404 提示检查接口地址和模型名，401/403 提示检查密钥与权限，限流和超时需要主动重试。界面不回显密钥，安装包不包含开发机的模型配置。

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

`pack` 和 `dist` 都会先重建 Python 后端，避免安装包误用旧源码。`build-backend.ps1` 把 PyInstaller 缓存限制在忽略的 `desktop/build/` 中。后端包含静态资源、`addons/ankisense_sync/` 插件、PowerPoint 转换脚本、RapidOCR 模型与 ONNX Runtime DLL。`electron-builder` 把它复制到安装程序的 `resources/backend/`。不打包开发数据库、用户设置、API Key 或 `.env`。

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
npm run test:connection
npm run test:connection  # 验证 desktop/dist/win-unpacked 中的真实程序，仅请求本机模拟模型
```

原生验收使用随机空闲端口、隐藏窗口、单独的 `.ui-checks/native-desktop/` 用户目录和演示模式，检查真实后端、静态资源、窗口按钮、渲染器隔离以及退出后的进程清理。`test:frozen` 需要先构建后端。报告和截图保存在对应 `.ui-checks/` 目录。

`test:connection` 在打包后的程序中使用独立用户目录与本机模拟模型，验证配置持久化、成功、凭据错误、地址错误、限流、超时、无效 JSON、页面切换和重启恢复，不请求真实服务商。`node smoke.cjs --packaged` 额外验证随包插件安装到隔离目录与回执展示；真实 Anki 写入另用官方运行时和隔离资料库验收。

`test:connection` 使用 `.ui-checks/native-connection/` 下的独立配置与虚构密钥，覆盖状态颜色、拒绝访问、超时、重试、切换页面、配置保存与服务重启后的状态失效。等待采用定时轮询；截图单独限时，不阻塞功能断言。设置 `ANKISENSE_TEST_EXECUTABLE` 可验收其他目录中的同一程序。测试不会访问真实模型或个人 Anki 资料。

`ANKISENSE_DESKTOP_PORT` 和 `ANKISENSE_DESKTOP_USER_DATA` 仅用于显式覆盖端口/用户目录（主要供隔离测试）；一般用户无需设置。
