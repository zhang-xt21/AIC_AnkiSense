# 第三方组件与许可记录

本仓库保存 AnkiSense 自有业务代码、插件及测试。Anki 程序、Python/npm 已安装依赖、OCR/Docling 模型和个人学习库不在源码仓库中分发。

团队尚未确定本项目自身的公开发行许可证，当前没有添加代表团队授权选择的 LICENSE。公开发布前需要结合代码来源、最终集成与打包方式完成选择；下面的上游许可证不等同于本项目许可证。

## 主要组件

| 组件 | 当前用途 | 上游许可与来源 |
| --- | --- | --- |
| Anki | 用户独立安装的复习客户端；插件调用其接口 | [AGPL-3.0-or-later，部分组件另有许可](https://github.com/ankitects/anki/blob/main/LICENSE) |
| Docling | 可选的本地讲义版面解析和图片描述适配 | [MIT](https://github.com/docling-project/docling/blob/main/LICENSE)；模型权重需分别核对模型说明 |
| RapidOCR | 本地 OCR 运行库 | [Apache-2.0](https://github.com/RapidAI/RapidOCR/blob/main/LICENSE)；模型与运行库分别记录来源 |
| genanki | 生成 APKG 与插件模板包 | [MIT；项目说明](https://github.com/kerrickstaley/genanki)，已核对安装包 0.13.1 的许可元数据 |
| Playwright | 开发用离线浏览器验收 | [Apache-2.0](https://github.com/microsoft/playwright/blob/main/LICENSE) |

其他直接与间接依赖的准确版本见 `requirements.lock.txt`、`requirements-docling.lock.txt`、`package-lock.json`，各包的许可文件随对应依赖保留。本表是主要组件索引，不是完整的软件物料清单或完整发行许可清单。

## 仓库内的模板和测试材料

- `addons/ankisense_sync/bootstrap.apkg` 是由本项目构建工具生成的最小模板初始化包，不是用户导出的学习牌组。生成入口为 `tools/build_anki_addon.py`。
- `examples/栈与队列_自编演示讲义.md` 是自编流程示例。真实课程 PDF 与个人截图未随仓库分发。
- `测试记录/` 保留历史文字结论，部分原始本机产物未分发，不能把缺失的附件当作当前验收结果。

AnkiSense 为团队项目，不代表 Anki、Docling 或其他上游项目官方。后续若复制上游源码或制作包含依赖的桌面安装包，应同时补全相应来源、版本、许可文本与发行说明。
