# AnkiSense

从讲义生成有来源依据的图文卡片，用自己的话回答，再通过 Anki 安排复习。

本仓库包含 AnkiSense 网页、Python 后端、文档解析、自动配图和 Anki 同步插件源码。它是独立项目，**不是 Anki 官方源码的分叉**。当前产品是单人本地原型：PDF 制卡在网页完成；已同步的卡片可在 Anki 内输入回答并查看分析，Anki 负责复习排期。统一桌面客户端仍在规划中。

## 启动

目前以 Windows PowerShell、Python 3.12 为主要开发环境。安装 Python 后，从项目根目录（包含本文件与 `setup.ps1` 的文件夹）运行：

```powershell
.\setup.ps1
.\start.ps1
```

初始化脚本安装锁定的核心依赖，在缺少 `.env` 时复制配置示例；不会覆盖已有配置。打开 <http://127.0.0.1:8765>。服务运行期间保持终端开启，停止时在该终端按 `Ctrl+C`。端口占用时先停止原来的服务，或使用 `.\start.ps1 -Port 8766`。

默认是 **演示模式**，不需要模型 API 密钥。新仓库只包含源码和自编示例，不继承原开发目录的讲义、卡片、模型配置或个人 Anki 资料。

如果脚本执行受本机策略限制，可手动完成相同步骤；已有环境或配置时跳过相应创建操作：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn ankisense.app:app --host 127.0.0.1 --port 8765
```

## 先跑通一个完整流程

1. 在「学习材料」载入自编的栈与队列演示材料。
2. 在「制卡工作台」对照原文检查题目、答案和评分要点，保存并批准卡片。
3. 在「开始练习」用自己的话回答，查看反馈与引用。演示反馈使用本地规则，不代表真实 AI 效果。
4. 在「导出至 Anki」下载 APKG，再导入自己的 Anki 测试牌组；也可以安装同步插件。

真实讲义流程是：**导入 PDF → 按页 OCR 与校对 → 生成卡片草稿 → 编辑与配图 → 审核 → 作答反馈 → 复习**。只有已批准且来源有效的卡片可练习、导出和同步。

## 按需启用额外能力

核心依赖已包含本地印刷文字 OCR。下列能力单独准备，第一次启动不必全部安装：

| 能力 | 准备方式 |
| --- | --- |
| 云端卡片生成与回答分析 | 在网页「模型设置」填写自己的 API 地址、模型名称和密钥，见 [AI 接入指南](docs/AI接入指南.md) |
| Docling 图块与配图推荐 | `.\setup.ps1 -IncludeDocling`；使用独立 `.venv-docling` 并下载模型，见 [自动配图说明](docs/自动配图使用说明.md) |
| 浏览器测试 | 安装 Node.js 20 或更新版本，然后运行 `.\setup.ps1 -IncludeBrowserTests` |
| 本地 Anki 与同步插件 | `.\setup.ps1 -IncludeAnki` 下载并校验官方安装包、准备插件；首次仍需按提示完成安装，见 [Anki 本地试用](origin/Anki本地试用说明.md) |

Anki 准备脚本不会静默安装或升级系统已有的 Anki。已经安装时可用 `-AnkiRoot` 指向程序目录，详见本地试用说明。

云端模式会按操作发送选中的原文、题目、评分要点和回答；主动使用配图视觉检查时也会发送所选图片与原页。普通配图推荐在本机运行。当前答题诊断仍以文字为依据，不会自动理解卡片图片。

## 导入 Anki

推荐使用 **APKG**：在网页下载牌组包，然后在 Anki「导入文件」中打开。题目、答案、已保存图片、来源和模板会一起导入。题目配图在正面，答案配图在背面。修改并批准后，可以再次导出更新同一卡片。

同步插件提供「工具 → AnkiSense · 同步卡片…」，使用本机 v1 接口读取已批准卡片并通过 Anki Collection API 写入。在已安装插件、完成同步且本机 AnkiSense 服务运行时，点击复习卡片上的「用自己的话回答」会在 Anki 内打开作答窗口；分析后仍由你自己选择 Anki 的复习评分。云端模式会在每次新提交和主动重试前确认发送范围。只导入 APKG、没有安装插件时，卡片按钮仍打开本机网页。

详细操作见 [同步插件使用说明](docs/Anki同步插件使用说明.md)；TSV / 图文 ZIP 字段与模板见 [导入导出参考](docs/导入导出参考.md)。

## 数据与目录

```text
ankisense/          后端与网页静态资源
addons/             Anki 同步插件源码
examples/           自编演示讲义、插件客户端示例
tests/              自动化测试
tools/              安装、构建和验收工具
docs/               使用与技术说明
origin/             本机 Anki 试用入口（Anki 程序和资料不受 Git 管理）
.github/            Issue、PR 模板和离线 CI
```

每位成员独立使用自己的 `.env`、`data/` 和 Anki 资料。默认 SQLite 数据库为 `data/ankisense.sqlite3`；PDF、原图、卡片图片也在 `data/` 下。网页模型设置保存于 `data/model-settings.json`，密钥仅存本机、未加密，不应分享此文件。

`ANKISENSE_DATA_DIR` 接受自定义相对或绝对路径；相对路径统一以项目根目录为起点。更换目录后重建虚拟环境，并在 Anki 同步窗口重新选择当前的 `plugin-connection.json`。备份个人学习内容时先停止服务，再备份完整数据目录；Git 只管理源码，不替代学习数据备份。

## 开发与测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
npm test
npm run test:browser
```

Node 和浏览器测试先运行上面的 `-IncludeBrowserTests` 初始化。浏览器测试使用隔离数据与演示输入，不调用真实云端模型。`tools/test_anki_*.py` 使用专用 Anki 运行器，不直接交给网页环境的 pytest 收集。Docling、Anki 官方运行时与真实云端评估需要各自的额外环境。

请在自己的电脑运行与改动相关的检查。默认测试不调用真实云端模型；Docling 和官方 Anki 运行时需要另外准备。测试通过不等于 AI 判断准确，应使用人工标注的样本单独评估。

加入开发请先读 [协作说明](CONTRIBUTING.md)、[架构与分工](docs/架构与分工.md) 和 [协作接口契约](docs/协作接口契约.md)。主分支 `main` 保持可运行，小任务使用独立分支与 PR 评审；是否合并以该次 PR 的实际 CI 结果和队友评审为依据。

## 使用与技术文档

- [完整使用教程](docs/使用教程.md) · [制卡工作台](docs/制卡工作台使用说明.md)
- [OCR 与校对](docs/OCR与校对使用说明.md) · [自动配图与裁剪](docs/自动配图使用说明.md)
- [AI 接入指南](docs/AI接入指南.md) · [插件接口 v1](docs/插件接口说明.md)
- [Anki 同步插件](docs/Anki同步插件使用说明.md) · [Anki 本地试用](origin/Anki本地试用说明.md)

本仓库未包含 Anki 安装包、第三方模型缓存、个人学习库和真实课程讲义。统一桌面客户端、全自动审核和视觉模型直接评分仍是后续目标。公开发布前，团队需确认项目许可证和可公开材料范围；第三方清单见 [依赖与来源说明](THIRD_PARTY_NOTICES.md)。
