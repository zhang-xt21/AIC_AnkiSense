# Anki 本地试用

本目录保存 Anki 启动入口和说明。源码仓库不包含 Anki 程序或个人资料；初始化后在本机准备官方 Anki 与本项目独立的 `AnkiData/`。AnkiSense 网页服务仍单独启动。

## 第一次准备

从项目根目录运行；如果 Anki 正在使用，先正常退出，以便准备插件：

```powershell
.\setup.ps1 -IncludeAnki
```

如果尚未选择已安装的 Anki，脚本下载固定的 Windows x64 官方 MSI、核验 SHA256，并准备本项目独立资料目录和同步插件。**脚本不会自动运行 MSI；下载完成不等于已安装。** 按终端提示打开官方安装包完成安装，安装器可能更新系统已有 Anki，请核对安装位置。

安装完成后指定实际程序目录。下面使用官方常见位置，若安装位置不同请替换：

```powershell
.\tools\setup\setup_anki.ps1 -AnkiRoot "$env:LOCALAPPDATA\Programs\Anki"
```

已安装 Anki 的成员可以直接选择现有程序，不必再次下载：

```powershell
.\setup.ps1 -IncludeAnki -AnkiRoot "$env:LOCALAPPDATA\Programs\Anki"
```

`-AnkiRoot` 应指向含 `anki.exe` 的目录。传给初始化脚本的相对路径以项目根目录为起点，例如 `origin\Anki`。初始化不会把插件写入系统默认的个人 Anki 资料，只准备本项目 `origin/AnkiData/`。

## 打开软件

```powershell
.\origin\启动Anki.ps1
```

启动脚本读取本机 `origin/anki-runtime.json` 中的程序位置，以 `origin/AnkiData/` 作为独立资料目录。没有本机配置时默认查找 `origin/Anki/anki.exe`。还可向启动脚本显式传入 `-AnkiRoot`；此启动脚本的相对参数以 `origin/` 为起点。

换电脑后重新运行初始化并选择自己的程序目录；不要共享另一位成员的 `anki-runtime.json`。系统开始菜单中的普通 Anki 入口可能打开默认个人资料，试用本项目时优先用上述脚本。

网页服务在另一个终端运行 `.\start.ps1`。进入 Anki「工具 → AnkiSense · 同步卡片…」，检查本机连接并同步已批准的卡片。详见 [同步插件使用说明](../docs/user/Anki同步插件使用说明.md)。卡片上的「用自己的话回答」仍打开网页；普通翻卡和图片展示不依赖网页服务。

## 先试一张卡片

1. 在 Anki 主界面创建「AnkiSense 试用」牌组。
2. 点击「添加」，使用基础笔记类型，问题写「栈的出入顺序是什么？」，答案写「后进先出：最后放入的元素最先取出。」。
3. 保存后进入牌组，先回忆，再显示答案并按实际记忆情况评分。
4. 再尝试从网页导出 APKG 或通过插件同步一张已批准的图文卡片。

## 文件在哪里

| 内容 | 本机位置 |
| --- | --- |
| 启动入口 | `origin/启动Anki.ps1`（源码） |
| 程序位置配置 | `origin/anki-runtime.json`（不提交） |
| 学习数据、插件和媒体 | `origin/AnkiData/`（不提交） |
| 已下载官方安装包 | `origin/downloads/`（不提交） |
| Anki 程序 | 自己选择的安装目录，可为 `origin/Anki/` |

程序和学习资料分开存放。备份时先关闭 Anki，再备份完整 `AnkiData/`。更新同步插件前同样先退出 Anki，再重跑初始化；插件更新时的本地备份位于 `dist/installation-backups/`。

## 安装来源

初始化脚本固定使用 Anki 26.09.3 Windows x64 官方安装包 `anki-26.09.3-win-x64.msi`，SHA256 为 `8a45823d1c31017cebb7d3af4493d69b6ae3c185345fcb2390ede4af01e0a950`。其他操作系统或架构需选择对应官方程序，不适用这份 Windows 安装辅助脚本。

- [Anki 官方网站](https://apps.ankiweb.net/)
- [固定版本官方发布页](https://github.com/ankitects/anki/releases/tag/26.09.3)
- [官方资料目录与启动参数](https://docs.ankiweb.net/files.html#startup-options)
