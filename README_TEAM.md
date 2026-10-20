# AnkiSense 队友对接 README

这份文档用于接手开发和联调。目标功能是：**选择 PPT / PPTX / PDF 后，后台自动解析、制卡、对照原页检查并展示结果，用户无需逐页确认或再次点击生成。**

当前自动制卡版本在 `codex/automatic-card-import` 分支，对应 [PR #2](https://github.com/z252342373-ai/AIC_AnkiSense/pull/2)。合并前按下面的功能分支启动；合并后可从最新 `main` 开发。仓库首页默认显示的分支不一定包含这套功能。

## 1. 先把项目跑起来

### 拉取代码

仓库是私有仓库，队友需要仓库访问权限。第一次下载：

```powershell
git clone --branch codex/automatic-card-import https://github.com/z252342373-ai/AIC_AnkiSense.git
cd AIC_AnkiSense
```

已有仓库时，先处理好自己的未提交改动，再执行：

```powershell
git fetch origin
git switch codex/automatic-card-import
git pull --ff-only
```

### 安装与启动

主要开发环境为 Windows、Python 3.12。前端单元测试需要 Node.js 20 或更新版本；浏览器测试还需要 Chromium。项目使用 FastAPI 和原生 JavaScript，启动网页不需要先构建前端。

从项目根目录执行：

```powershell
.\setup.ps1
.\start.ps1
```

打开 <http://127.0.0.1:8765>。默认是演示模式，可以先加载自编的栈与队列示例，体验编辑、练习和导出。演示模式不能代替真实模型完成新的自动制卡。

首次准备浏览器测试时，可用以下命令代替普通初始化：

```powershell
.\setup.ps1 -IncludeBrowserTests
```

PPT / PPTX 的原页转换需要本机安装 PowerPoint 或 LibreOffice；使用 LibreOffice 时，`soffice` 或 `libreoffice` 命令需在 PATH 中。只联调 PDF 时不需要安装它们。Docling 和 Anki 桌面程序是额外能力，默认自动制卡不依赖 Docling 图块推荐。可选安装方式见 [项目 README](README.md#按需启用额外能力)。

`127.0.0.1` 指向访问者自己的电脑。每位队友需要在自己的电脑启动服务，克隆代码不会复制其他人的模型配置、材料或卡牌。

### 配置真实模型

在侧栏「模型设置」填写并保存：

| 项目 | 填写内容 |
| --- | --- |
| 模式 | 云端模式 `cloud` |
| API 地址 | 模型服务的 Base URL；项目已实测 `https://api.deepseek.com`，不要附加 `/chat/completions` |
| 模型名称 | 自己账号可用、支持图片输入和 JSON 输出的模型；2026-10-19 实测使用 `deepseek-flash` |
| API Key | 每位成员自己的 Key |
| 超时 | 界面支持 1–180 秒，根据模型响应时间设置 |

保存的配置位于当前数据目录的 `model-settings.json`，优先于环境变量和 `.env`。已在页面保存后，优先通过页面修改配置。Key 只在本机保存，不会由读取设置接口返回；配置文件中的 Key 未加密。

“已配置”只表示字段已填写，不表示调用成功。用自编的 1–2 页材料实际导入，确认卡牌能生成、显示原页并导出。自动制卡会向所配置模型发送相关文字和原页图片，真实调用使用自己的 API 额度。具体配置操作见 [AI 接入指南](docs/AI接入指南.md)。

## 2. 当前链路与修改入口

```text
选择讲义 → material-imports 后台导入 → 自动制卡任务
        → 提取知识 → 生成卡牌 → 独立对照原页检查 → 保存卡牌和答案侧原页
        → 卡牌结果自动更新 → 练习 / APKG 导出 / Anki 同步
```

默认导入上限为 50 MiB、500 页、提取文字合计 100 万个字符；一次自动任务最多保存 600 张卡牌。旧高级 PDF 导入仍有 10 MiB、30 页的限制，不要把旧入口的限制当作新默认入口的限制。

| 对接方向 | 主要文件 | 修改时关注 |
| --- | --- | --- |
| 导入与卡牌结果页 | `ankisense/static/automatic.js`、`app.js`、`index.html`、`styles.css` | 文件选择即启动、轮询恢复、直接展示结果；保留可选高级工具 |
| 文件解析与转换 | `ankisense/automatic_import.py`、`ingest.py`、`presentations.py`、`ocr.py`、`workers/powerpoint-export.ps1` | 原生文字、OCR、原页转换、导入队列和文件限制 |
| 自动制卡与检查 | `ankisense/auto_cards.py`、`automatic_runner.py`、`automatic_policy.py`、`card_quality.py` | 任务、检查点、重试、疑点、当前版本批准依据 |
| 模型接入与提示词 | `ankisense/llm.py`、`prompts.py`、`auto_card_models.py`、`model_settings.py` | 图片与 JSON 调用、严格输出结构、引用和质量检查 |
| 公共 API 与存储 | `ankisense/app.py`、`schemas.py`、`db.py`、`sources.py`、`paths.py` | 接口校验、事务、来源版本与本机路径 |
| 练习与 Anki | `ankisense/attempts.py`、`apkg.py`、`plugin_api.py`、`plugin_sync.py`、`addons/ankisense_sync/` | 作答快照、可用卡牌、导出、插件 v1 契约与 Anki API 写入 |

`static/` 是当前网页前端，服务端同时提供页面和 API。独立前端开发服务器需要设计同源代理，保持转发的 Host / Origin 一致；直接从另一个端口发修改请求可能收到 403。

详细责任边界见 [架构与分工](docs/架构与分工.md)。当前 Anki 插件界面负责同步，卡牌上的作答链接打开本机网页；网页项目不修改 Anki 调度算法。

## 3. 前后端怎么联调

默认入口是 `POST /api/material-imports`。旧的 `POST /api/documents` 用于高级手动 PDF 导入，不会自动接上新的制卡流程。

| 接口 | 用途 |
| --- | --- |
| `GET /health` | 确认服务可用，返回 `status` 和 `mode` |
| `GET /openapi.json` | 查看当前运行版本的请求/响应 Schema |
| `POST /api/material-imports` | `multipart/form-data`，字段名 `file`；返回导入任务并后台处理 |
| `GET /api/material-imports` | 返回 `{ "imports": [...] }`，用于重新进入页面时恢复任务 |
| `GET /api/material-imports/{import_id}` | 查看一份材料的解析和制卡状态 |
| `GET /api/auto-card-jobs/{job_id}` | 查看制卡批次进度、卡牌 ID、疑点和错误 |
| `GET /api/state` | 读取材料、块、卡牌、来源页、配图与作答；按材料 ID 或任务的 `card_ids` 关联 |
| `PUT /api/cards/{card_id}` | 按 `CardUpdate` 提交完整卡片，携带 `expected_version`；不是任意字段的 PATCH |
| `POST /api/attempts` | 提交 `card_id`、`answer` 和 `request_id`，随后轮询作答任务；字段约束见 Schema |
| `GET /api/export/apkg` | 下载当前符合规则的已批准卡牌；导出范围不是按导入任务自动隔离 |

下面的上传命令是模板，先将文件路径换成自己的测试材料：

```powershell
curl.exe -X POST -F "file=@C:\path\lecture.pdf" http://127.0.0.1:8765/api/material-imports
```

初始响应示意，省略了时间等字段；示例 ID 不能直接调用：

```json
{
  "id": "import-example",
  "status": "pending",
  "message": "讲义已接收，正在自动处理。",
  "document_id": null,
  "job_id": null
}
```

轮询同一个 `import_id`。进入制卡后，响应会包含 `document_id`、`job_id` 和嵌套的 `job`，示意如下：

```json
{
  "id": "import-example",
  "status": "running",
  "document_id": "pdf-example",
  "job_id": "autjob-example",
  "job": {
    "status": "running",
    "progress": { "completed": 2, "total": 10 },
    "card_ids": ["autcard-example"],
    "issues": []
  }
}
```

前端接入时遵守以下约定：

- 使用接口返回的 ID，不自行拼接真实任务、材料或卡牌 ID。
- 顶层 `progress` 是文件解析页数；制卡进度读取 `job.progress`，其单位是原文批次。两者都不是知识覆盖率。
- 导入接口在建立制卡任务后，会将公开的 `status` / `message` 映射为制卡状态；页面应继续轮询到当前任务结束。
- 顶层 `error` 和时间字段仍属于导入记录；制卡阶段也要读取 `job.error` / `job.updated_at`，不能只依赖顶层错误判断是否成功。
- `job.card_ids` 只有标识，完整卡牌从 `/api/state` 获取。可边处理边显示已保存结果；原有页面轮询间隔约为 1.8 秒，不需要每秒多次请求。
- 刷新后先恢复导入列表，再恢复选中的材料。网页支持 `/?import=<实际返回的导入ID>` 直接进入结果页。
- `completed` 不保证每页都有卡牌或没有局部失败，仍需展示 `issues`、`error` 和实际结果数。

全部自动与高级接口说明见 [自动制卡接口与验收](docs/自动制卡接口与验收.md)。Anki 客户端使用独立的 [插件接口 v1](docs/插件接口说明.md)，按其本机连接文件、鉴权和错误约定接入。

插件 v1 使用连接 token 的 Bearer 鉴权及固定持久的 `X-AnkiSense-Client` 标识，连接 token 与模型 API Key 是不同凭据。插件通过接口读取卡牌并经 Anki Collection API 写入，不直接编辑本项目或 Anki 的 SQLite。

网页作答的 `request_id` 用于标识同一次提交，网络重试保留同一编号。作答任务 `failed` 表示分析失败，不能当作答错；有效诊断需等到该次任务完成后再展示。

## 4. 必须保持的数据约定

### 来源、任务和卡牌状态分别记录

| 对象 | 关键约定 |
| --- | --- |
| 来源页 `source_pages` | `pending` 可以与可用的自动卡牌同时存在；未人工确认整页不等于自动制卡被阻塞 |
| 自动任务 `auto_card_jobs` | `pending` / `running` 为活动任务；结束可能是 `completed`、`failed`、`cancelled` 或 `outdated` |
| 卡牌 `cards` | `draft` / `approved` / `rejected`；自动通过使用 `approved` 并记录 `approval_method=automatic` |

高级手动流程还可能出现 `awaiting_confirmation`。新默认导入不要求先走这个确认流程。自动批准由后端校验卡牌自身的原页检查依据，不是前端把 `status` 改成 `approved` 就能替代。

引用使用 `evidence: [{ "block_id": "...", "quote": "..." }]`；`quote` 必须是对应块中的连续原文，块属于同一份材料。来源页的 `revision`、卡牌的 `version`、修改请求的 `expected_version` 各有用途，不能混用。

修改文字、评分点、引用或图片会使旧检查失效；修改来源会使引用旧来源的卡牌过期。遇到 409 时重新读取当前状态并保留未保存草稿，不要去掉版本检查或直接修改 SQLite 绕过接口。人工确认和自动检查需要保持可区分，历史作答快照也需要保留。

### 本机数据不随 Git 共享

默认数据目录为 `data/`。可通过 `ANKISENSE_DATA_DIR` 指定其他位置，相对路径以项目根目录为起点。

| 路径 | 内容 |
| --- | --- |
| `data/ankisense.sqlite3` | `records(collection, id, payload)` 中的 JSON 记录、材料、卡牌、来源版本与任务 |
| `data/incoming/` | 自动导入接收的文件和转换中间文件 |
| `data/uploads/` | 用于原页显示的 PDF |
| `data/media/` | 卡牌图片 |
| `data/model-settings.json` | 本机模型配置与 Key |
| `data/plugin-connection.json` | 本机 Anki 插件连接凭据 |

每位成员独立使用自己的 `.env`、数据目录和 Anki 测试资料。需要备份时先停止服务，再备份完整数据目录。运行一个后端进程；暂不使用多个 worker，也不要用两个服务同时处理同一个数据目录。

同一文件内容和后缀重复上传会复用现有导入任务，包括已有失败记录。当前没有导入任务的单独重试接口；已建立的制卡任务可通过 `POST /api/auto-card-jobs/{job_id}/retry` 按现有规则继续。更换模型或重复选同一文件，不等于重新生成一套结果；重试、版本变化和重新生成逻辑修改前应先约定行为。

## 5. 各方向的最小验证

以下命令从项目根目录执行。只有文档改动时检查链接与说明即可；功能修改按涉及范围运行测试。

```powershell
# Python 全量离线回归
.\.venv\Scripts\python.exe -m pytest tests -q

# 自动导入 / 制卡 / 模型契约定向回归
.\.venv\Scripts\python.exe -m pytest tests/test_automatic_import.py tests/test_auto_cards.py tests/test_auto_card_ai.py -q

# 前端单元测试
npm test

# 新自动导入页面与刷新恢复
npm run test:browser:automatic-import

# 重要网页改动后的全量浏览器检查
npm run test:browser
```

默认测试使用隔离数据与模拟模型，不需要个人 Key。真实模型验收是额外步骤：用获准的小样本，记录模型、提示版本、输入、疑点和导出结果；程序通过数不能替代准确率评估。Docling 和官方 Anki 运行时测试按各自文档准备额外环境。

第一次对接完成后，至少确认：演示可运行；自己的模型配置生效；小样本能自动进入卡牌结果；结果可阅读和导出；相关离线测试通过。当前能力与真实小样本记录见 [验收记录](docs/导入后自动制卡验收.md)。

## 6. 常见联调问题

| 现象 | 优先检查 |
| --- | --- |
| 拉代码后还是旧页面 | 当前分支是否正确，服务是否重启、页面是否刷新；不要同时运行两个不同目录的旧服务 |
| 本地服务未连接 / 端口占用 | 启动终端是否仍在运行；需要其他端口时用 `.\start.ps1 -Port 8766`，并访问对应地址 |
| 云端模式待配置 / 不能自动制卡 | 是否保存自己的配置；页面保存的配置优先于 `.env`；demo 不会生成新的云端卡牌 |
| 保存模型设置返回 409 | 是否仍有导入、制卡或分析任务；等待结束或按现有接口取消相关任务 |
| 导入完成但卡牌数较少 | 查看制卡任务的 `issues`；目录、无明确依据的内容或处理失败的批次会被跳过 |
| PPT 转换失败 | 本机 Office 转换组件是否可用；先用 PDF 隔离模型与转换问题 |
| 原文显示待校对，但已有可用卡牌 | 核对卡牌自身 `auto_quality` 和自动批准记录；不要在新默认流程强制增加整页确认步骤 |
| 更改卡牌后不能继续练习或导出 | 检查卡牌版本、来源有效性和旧检查是否失效，按高级编辑流程重新检查 |
| 独立前端请求返回 403 | 检查页面/API 是否同源以及代理转发的 Host / Origin，保持现有来源校验 |

## 7. 如何交接下一项任务

在 Issue 或 PR 中写清：用户问题、期望行为、主要文件、请求/响应示例、数据兼容方式和验收步骤。涉及 `app.py`、`static/app.js`、`schemas.py`、`db.py` 或插件契约时，先与同样修改这些文件的队友对齐。

当前功能分支用于这次自动制卡集成；长期开发按 [CONTRIBUTING.md](CONTRIBUTING.md) 从最新稳定主线拆小任务、建立独立分支，通过相关检查和队友评审后合并。接口变化需要同步前端、后端、示例和契约测试。

当前交接重点是完善知识覆盖和疑点处理、增加固定人工标注样本，以及评估图块选择和裁剪。答案侧现有图片为完整讲义原页；自动精确裁剪、统一桌面客户端和直接理解图片的答题诊断不属于已验收能力。
