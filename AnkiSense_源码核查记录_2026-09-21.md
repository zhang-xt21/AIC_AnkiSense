# AnkiSense：开源能力与源码核查记录

> 历史设计资料：保留 2026-09-21 当时的计划和核查结论，不代表当前实现。最新状态见[当前状态与路线图](docs/当前状态与路线图.md)。原调研副本和竞赛附件未随源码分发。

研究日期：2026-09-21。用途：为 Anki 讲义学习诊断助手的实施方案提供选型依据。

本次做了官方文档与关键源码静态核查；下载了 pdf2anki 源码副本并核对提交。没有安装运行完整 Anki/pdf2anki/Docling 链路，没有调用付费模型，也没有真实讲义或学习者实验。下列“实现存在”与“实际效果已验证”是不同结论。

## 1. 最重要的修正：pdf2anki 不能直接按 README 接入

此前讨论依据 README，曾建议先用 pdf2anki 跑通一章。深入检查后，需要修正这一建议：**它可以作为设计参考，当前核查版本不适合作为团队项目的核心依赖。**

仓库：[NicholasMcCarthy/pdf2anki](https://github.com/NicholasMcCarthy/pdf2anki)。本次固定提交：`bbea5b08c2d335bdfbf54f526027f670468f68ac`。本地副本位于 `tmp/anki_study/pdf2anki_audit/`，未在该副本内开发产品功能。

| 项目 | 源码中的实际情况 | 对实施的影响 |
|---|---|---|
| 完整生成入口 | `_run_full_generation()` 留有 TODO，只打印未实现提示 | 不能把默认 `generate` 命令当作现成端到端管线 |
| 示例生成 | `--sample` 对应路径使用固定 mock 卡片 | 示例运行结果不能视作模型制卡效果 |
| 引用校验 | 引用字段只检查非空；引文检查只覆盖英文双引号内长度超过 10 的内容 | 没有匹配引文的卡片也可能通过；需自建强制证据字段和校验 |
| 页码绑定 | 策略自动补上块起始页，原文字段只保留前 500 字符 | 有页码不等于问题和答案受到该页支持 |
| 全文分块 | `entire` 路径对自动分块统一设置全文页范围 | 无法由该范围直接证明精确来源页 |
| 旧调用路径 | 配置中的字符串仍被部分代码按枚举 `.value` 使用；dataclass 对象仍被部分路径调用 `.dict()` | 静态可见接口不一致，不宜指望旧入口直接绕开未实现部分 |
| 扩展能力 | OCR、部分 embedding/RAG 路径存在 TODO 或返回 `None` | README 中列出功能不等于该路径已完成 |
| 可参考实现 | 逐页文本抽取、字段组织、genanki 打包代码确实存在 | 参考设计，逐段核查后才考虑复用代码 |

逐项依据：

- [CLI 的 mock 示例与未实现生成入口](https://github.com/NicholasMcCarthy/pdf2anki/blob/bbea5b08c2d335bdfbf54f526027f670468f68ac/src/pdf2anki/cli.py#L557)
- [完整生成函数](https://github.com/NicholasMcCarthy/pdf2anki/blob/bbea5b08c2d335bdfbf54f526027f670468f68ac/src/pdf2anki/cli.py#L700)
- [引用校验函数](https://github.com/NicholasMcCarthy/pdf2anki/blob/bbea5b08c2d335bdfbf54f526027f670468f68ac/src/pdf2anki/preprocess.py#L321)
- [策略自动填写来源元数据](https://github.com/NicholasMcCarthy/pdf2anki/blob/bbea5b08c2d335bdfbf54f526027f670468f68ac/src/pdf2anki/strategies/base.py#L170)
- [逐页解析](https://github.com/NicholasMcCarthy/pdf2anki/blob/bbea5b08c2d335bdfbf54f526027f670468f68ac/src/pdf2anki/pdf.py#L49)
- [全文分块页范围](https://github.com/NicholasMcCarthy/pdf2anki/blob/bbea5b08c2d335bdfbf54f526027f670468f68ac/src/pdf2anki/chunking.py#L645)
- [LLM 配置调用](https://github.com/NicholasMcCarthy/pdf2anki/blob/bbea5b08c2d335bdfbf54f526027f670468f68ac/src/pdf2anki/llm.py#L56)
- [RAG 的 embedding 路径](https://github.com/NicholasMcCarthy/pdf2anki/blob/bbea5b08c2d335bdfbf54f526027f670468f68ac/src/pdf2anki/rag.py#L203)
- [genanki 打包实现](https://github.com/NicholasMcCarthy/pdf2anki/blob/bbea5b08c2d335bdfbf54f526027f670468f68ac/src/pdf2anki/build.py#L218)

这些结论仅针对上述提交，不代表所有同名 PDF 制卡项目，也不代表其他分支或未来版本。静态发现的接口问题还未逐一做运行时复现。

## 2. Anki 集成：真实答案与复习评分要分开

### 推荐主线：独立网页答题＋文本导入

Anki 支持 UTF-8 分隔文本导入与字段映射。首列可放稳定的自有 NoteID，用于匹配更新；不要把自造 ID 当成 Anki 内部 GUID。文本导入不承担外部答题记录或复习日志的双向同步。[官方文本导入说明](https://docs.ankiweb.net/importing/text-files.html)

独立网页保存原始答案、评分结果、依据与补题选择，再导出审核后的卡片到 Anki。这个路径最符合目前的 Python 技能和工时。

### 原生插件可行，但作为后续选项

已核查 Anki `25.09` 标签的官方钩子定义：

- `reviewer_will_compare_answer` 能访问期望答案与用户输入；
- `reviewer_will_render_compared_answer` 能拿到比较前的原始输入；
- `reviewer_did_answer_card` 对应用户选择的 1—4 复习评分。

这些数据语义不同。内置 typed answer 是有限的字符串对照，不等于语义评分；单个回答也不能直接证明稳定误解。插件需要处理重复渲染、重复提交、题目 ID 与答题事件关联，并在固定桌面版本上实测。

依据：[钩子源码](https://github.com/ankitects/anki/blob/25.09/qt/tools/genhooks_gui.py)、[钩子和网页消息桥接](https://addon-docs.ankiweb.net/hooks-and-filters.html)、[内置输入答案的限制](https://docs.ankiweb.net/templates/fields.html#checking-your-answer)。本次没有安装插件或修改个人牌组。

### AnkiConnect 不作为答案采集器

可选用途是桌面 Anki 运行时通过本地 API 创建牌组、添加笔记。归档仓库的 `guiCurrentCard` 不提供用户输入的短答案。GitHub 仓库已迁移到 SourceHut，不能仅凭 GitHub 归档状态判断整个项目停更；本次未验证新站当前版本。[原仓库及迁移说明](https://github.com/FooSoft/anki-connect)

APKG 后续可使用 [genanki](https://github.com/kerrickstaley/genanki) 生成，以打包模板及媒体；第一版文本导入已经足够，不同时维护三套导入路径。

## 3. 讲义处理：轻量解析优先，Docling 按需接入

对文字型 PDF，pypdf 提供逐页文字提取。它不是 OCR，也不保证复杂排版、表格和公式的语义顺序；抽出的文字仍需与页面核对。[官方提取说明](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)

Docling 的统一文档对象可以表达文本、表格、图片、层次和来源信息，位置信息取决于解析结果是否可用。它适合需要结构和版面信息时作为替换解析器；并不自动保证卡片答案正确。[Docling 文档结构](https://docling-project.github.io/docling/concepts/docling_document/)

两者通过本队统一的 `SourceBlock` 接口接入：上层制卡与诊断只接收块 ID、文档 ID、页码和原文。第一版没有可靠框坐标时显示原页与原文，不伪造高亮。

## 4. 相关工作与创新边界

| 原始工作 | 与方案相近之处 | 对本队的约束 |
|---|---|---|
| [AToM / AdapT，ACL 2024](https://aclanthology.org/2024.acl-long.718/) | 观察回答，推断误解，选择教学例题 | 不宣称首次实现自适应补题闭环 |
| [Stepwise Verification and Remediation，EMNLP 2024](https://aclanthology.org/2024.emnlp-main.478/) | 先核验学生推理错误，再提供针对性反馈 | “核验后反馈”不是新范式 |
| [The Correct Answer Trap，PEAF 2026](https://peaf-workshop.github.io/2026/papers/paper7.pdf) | 分级诊断与追问；追问部分为少量概念验证，未采集真实追问回答 | 真实追问前后变化可以做探索性验证，但不能宣称首次提出追问 |

可验证的项目问题应收窄为：**在单章讲义和审核题库下，依据原文评分要点与真实短答案，能否比直接模型诊断选卡减少无依据判断，提高补题匹配率，并控制交互成本？**

小规模结果可以说明原型可用性和该数据条件下的差异，不能自动证明长期学习收益。

## 5. 许可证记录

下面是所查源码的许可标识，正式选定版本后仍需生成依赖清单、保留相应声明。模型权重与讲义内容不自动继承代码许可证。

| 资源 | 已查许可 / 使用边界 | 来源 |
|---|---|---|
| Anki | 核心 AGPL-3.0-or-later；主线使用桌面程序及数据导入，不修改分发核心 | [LICENSE](https://github.com/ankitects/anki/blob/main/LICENSE) |
| pypdf | BSD-3-Clause | [LICENSE](https://github.com/py-pdf/pypdf/blob/main/LICENSE) |
| Docling | MIT；如启用模型，另记录模型许可 | [LICENSE](https://github.com/docling-project/docling/blob/main/LICENSE) |
| pdf2anki | MIT；其依赖应分别核查，本方案不直接依赖整个项目 | [LICENSE](https://github.com/NicholasMcCarthy/pdf2anki/blob/bbea5b08c2d335bdfbf54f526027f670468f68ac/LICENSE) |
| genanki | MIT，可选 APKG 导出 | [LICENSE](https://github.com/kerrickstaley/genanki/blob/main/LICENSE.txt) |
| AnkiConnect | 所查归档源码 GPL-3.0-or-later，当前迁移版本需重新固定 | [LICENSE](https://github.com/FooSoft/anki-connect/blob/master/LICENSE) |

实际立项以主实施方案中的最小依赖为准。此次研究没有替团队选择课程，也没有证明任一解析器在未来所选讲义上的效果。
