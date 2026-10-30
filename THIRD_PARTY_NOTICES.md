# 第三方组件与许可记录

本仓库保存 AnkiSense 自有业务代码、插件及测试。Anki 程序、Python/npm 已安装依赖、OCR 模型和个人学习库不在源码仓库中分发。

AnkiSense 自有代码采用 GNU Affero General Public License v3.0 only（SPDX：`AGPL-3.0-only`），完整条款见 [LICENSE](LICENSE)。Copyright (C) 2026 AnkiSense contributors。第三方代码、依赖、字体、模型和素材仍按各自许可证或授权条件使用；本声明不将它们重新授权为 AGPL，也不表示全部发行义务已经完成核验。

对外分发安装包时，同时提供本项目许可文本、第三方声明和与该安装包匹配的源码获取方式。现有 0.1.19 安装包生成于本次许可配置之前，文件内容没有改变；交付时需另附上述文件，不应宣称许可文本已经嵌入该安装器。

## 主要组件

| 组件 | 当前用途 | 上游许可与来源 |
| --- | --- | --- |
| Anki | 用户独立安装的复习客户端；插件调用其接口 | [AGPL-3.0-or-later，部分组件另有许可](https://github.com/ankitects/anki/blob/main/LICENSE) |
| RapidOCR | 本地 OCR 运行库 | [Apache-2.0](https://github.com/RapidAI/RapidOCR/blob/main/LICENSE)；模型与运行库分别记录来源 |
| genanki | 生成 APKG 与插件模板包 | [MIT；项目说明](https://github.com/kerrickstaley/genanki)，已核对安装包 0.13.1 的许可元数据 |
| Playwright | 开发用离线浏览器验收 | [Apache-2.0](https://github.com/microsoft/playwright/blob/main/LICENSE) |
| KaTeX 0.19.1 | 网页与桌面端的本地公式渲染；分发文件位于 `ankisense/static/vendor/katex/` | [MIT](https://github.com/KaTeX/KaTeX/blob/main/LICENSE)；许可文本随静态文件保留 |

其他直接与间接依赖的准确版本见 `requirements.lock.txt`、`package-lock.json`，各包的许可文件随对应依赖保留。本表是主要组件索引，不是完整的软件物料清单或完整发行许可清单。

## 仓库内的模板和测试材料

- `addons/ankisense_sync/bootstrap.apkg` 是由本项目构建工具生成的最小模板初始化包，不是用户导出的学习牌组。生成入口为 `tools/build/build_anki_addon.py`。
- `examples/栈与队列_自编演示讲义.md` 是自编流程示例。真实课程 PDF 与个人截图未随仓库分发。

AnkiSense 为团队项目，不代表 Anki 或其他上游项目官方。后续若复制上游源码或制作包含依赖的桌面安装包，应同时补全相应来源、版本、许可文本与发行说明。
