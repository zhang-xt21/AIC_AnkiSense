# 开发工具

从仓库根目录运行工具；脚本内部以文件位置定位项目，不依赖终端所在目录。原有 npm 命令保持不变，直接执行脚本时使用下列新路径。

| 目录 | 用途 | 常用入口 |
| --- | --- | --- |
| `setup/` | 环境准备、Anki 下载与配置 | `setup_anki.ps1`，通常由根目录 `setup.ps1 -IncludeAnki` 调用 |
| `build/` | 插件构建、离线公式资源复制 | `build_anki_addon.py`、`vendor_math.cjs` |
| `browser/` | 浏览器验收、公共函数和 Node 测试运行器 | `npm test`、`npm run test:browser` |
| `fixtures/` | 浏览器检查使用的隔离后端和数据 | `workbench_fixture.py` 等，由验收脚本启动 |
| `evaluation/` | 卡片、配图、原文校准评估 | `evaluate_automatic_cards.py` 等 |
| `anki/` | 插件连接、官方 Anki 运行器和隔离验收 | `plugin_connection.py`、`run_anki_sync_acceptance.py` |

示例：

```powershell
.\.venv\Scripts\python.exe tools/build/build_anki_addon.py --help
node tools/build/vendor_math.cjs
npm test
npm run test:browser:workbench
```

Anki 运行器使用专用资料目录；不要将 `tools/anki/test_*.py` 作为普通 pytest 测试直接运行。评估脚本是否调用模型以其说明及参数为准，不属于默认离线回归。桌面构建和原生窗口检查仍放在 `desktop/`，以保留 Electron 的依赖与打包边界。
