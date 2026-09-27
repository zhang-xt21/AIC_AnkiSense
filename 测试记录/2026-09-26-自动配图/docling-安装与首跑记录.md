# Docling 安装与首次真实解析

> 归档记录：以下环境、服务状态和结果属于报告日期；历史截图、数据库、JSON 与日志未随源码分发，见[测试记录索引](../README.md)。

2026-09-26 恢复工作后完成，Windows / Python 3.12。

## 已验证环境

- 独立环境：`.venv-docling`，未把 Torch/Docling 加入网页服务的 `.venv`。
- Docling / docling-slim：2.130.0；docling-core：2.99.0；Torch：2.14.0。
- `pip check`：No broken requirements found。
- 107 项运行依赖已固定在项目根目录 `requirements-docling.lock.txt`；安装器另固定 pip 26.2.1。
- 恢复安装使用清华 PyPI 镜像，下载报告为 `docling-pip-report.json`。对照暂停前 `docling-install-report.json`，筛选实际安装版本后，91 个下载条目的 SHA256 均与官方 PyPI JSON 元数据相同，明细为 `docling-install-hashes.json`。
- 环境共 108 个分发包（含 pip）；另外 17 个此前已安装的包没有保留对应版本下载条目，因此上述哈希核验覆盖 91 项，不能表述为所有 108 项均已核验。所有当前版本均已冻结。
- 第一次使用 urllib 请求 PyPI 出现 TLS EOF；改用已安装的 httpx 保持 HTTPS 证书验证后完成核验，没有关闭 TLS 校验。

安装脚本支持断点续传及慢网络：先安装 pip 26.2.1，随后设置 120 秒 socket timeout、5 次连接重试、10 次下载续传重试，并在安装后执行 pip check。默认使用官方 PyPI；可用 `-IndexUrl https://pypi.tuna.tsinghua.edu.cn/simple` 显式选择镜像。脚本 PowerShell 语法检查通过。

## 模型

- 官方仓库：`docling-project/docling-layout-heron`。
- 固定 revision：`8f39ad3c0b4c58e9c2d2c84a38465abf757272d8`，不再跟随 main。
- 主权重：`model.safetensors`，171,658,996 字节。
- 主权重 SHA256：`00333a43451945aaf89db8ca9c0a17e75d1537c17db60fdb91aa95f4c7929e0c`。
- 模型文件版本、大小、哈希保存在 `data/docling-models/ready.json`。
- 解析标识：`docling-2.130.0-pdfium-heron-8f39ad3-cpu-v2`。模型/后端变化使用新的标识，避免复用旧缓存。
- 推理子进程在导入 Hugging Face 前设置 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`，CPU 4 线程；OCR、表格结构识别、图片描述及远程服务均关闭。这里只获取图表区域，不把讲义送往云端。

## Windows 中文路径修复

默认 docling-parse 原生后端在本项目中文安装目录中报告找不到 `pdf_resources/glyphs/standard/additional.dat`，但该文件实际存在。改用 Docling 官方提供的 `PyPdfiumDocumentBackend` 后，同一输入解析成功。未移动用户项目或改写第三方包。

## 首次真实解析

- 输入：`test_ppt/03-栈队列_finalversion.pdf`，物理页码 9。
- 输出：`docling-page9.json`。
- 成功提取 3 个 picture 区域：栈示意图、顶部标识、底部装饰。装饰是否被推荐层排除，需以独立 20 例验收为准。
- worker 内部耗时：12.337 秒，包含转换器初始化及转换，不含 Python 启动/顶层依赖 import。界面/接口端实际等待时间由完整验收记录。
- 适配器针对性测试：13 passed，0.25 秒。

8 页/20 例真实推荐以及合成扫描测试由独立验收脚本继续进行。本记录只证明安装、离线模型首跑与适配器测试成功，不代表推荐质量已全部通过。
