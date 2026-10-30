# 文档导航

第一次使用先读[项目首页](../README.md)和[使用教程](user/使用教程.md)。加入开发先读[协作说明](../CONTRIBUTING.md)、[代码目录指南](development/代码目录指南.md)和[协作接口契约](development/协作接口契约.md)。

| 目录 | 内容 |
| --- | --- |
| `user/` | 面向使用者的操作说明 |
| `development/` | 代码结构、模块职责、接口约定和验收方法 |
| `evaluation/` | 对外测试结论与脱敏验证记录 |
| `history/` | 旧实施方案、阶段验证与修复记录，按各文档记录的版本阅读 |

## 使用说明

- [演示指南与截图位置](user/演示指南.md)
- [使用教程](user/使用教程.md)
- [模型 API 设置](user/AI接入指南.md)
- [自动制卡](user/自动制卡使用说明.md) · [制卡工作台](user/制卡工作台使用说明.md)
- [OCR 与校对](user/OCR与校对使用说明.md)
- [原图配卡与裁剪](user/自动配图使用说明.md) · [整卡机审](user/整卡机审使用说明.md)
- [Anki 同步插件](user/Anki同步插件使用说明.md) · [导入导出](user/导入导出参考.md)

## 开发说明

- [代码目录指南](development/代码目录指南.md)
- [架构与分工](development/架构与分工.md)
- [协作接口契约](development/协作接口契约.md)
- [Anki 插件接口](development/插件接口说明.md)
- [自动制卡接口与验收](development/自动制卡接口与验收.md)
- [配图验收方法](development/自动配图验收方法.md)
- [桌面构建与验收](../desktop/README.md)
- [提交与发布检查清单](development/提交与发布检查清单.md)

## 验证结论

- [测试结论与限制](evaluation/测试结论.md)
- [0.1.19 本地验收脱敏记录](evaluation/0.1.19-local-verification.json)

历史文档保留原来的测试范围与限制，不作为当前功能清单。新使用说明放入 `user/`，接口或维护说明放入 `development/`，阶段记录放入 `history/`。截图、安装包和本地验收产物不放进文档目录。
