# 设计文档

本目录收录 ai-study-helper v2 的当前设计基线。旧仓库资料不自动进入本项目的设计和实现上下文。

设计文档以 Markdown 为唯一源文件；GitHub 直接渲染。若后续需要独立文档站，应由这批 Markdown 生成，不另行维护一套 HTML 源文件。

## 当前状态

- 阶段：实现阶段，027 claim-aware 引用标注已完成，028 评测失败分类拆分已实现。
- 实现状态：桌面运行链、入库、collection 快照、文档工作台、后台交付、检索与 Agent 运行链、流式问答、本地 trace 诊断和逐 criterion 评测判定已实现。
- 当前工作：031 已完成 citation judge 与 annotation 校准，基础件和反向解释均已人工确认。

## 设计基线

| 编号 | 主题 | 状态 |
| --- | --- | --- |
| [01](design/01-product-requirements.md) | 产品需求、问题空间、范围和质量属性 | 已审核 · 实现基线 |
| [02](design/02-how-to-build-an-agent.md) | Agent 开发方法与知识面 | 已审核 · 实现基线 |
| [03](design/03-cases-and-gold-standard.md) | 用例与金标准 | v0.2 · 实现基线 |
| [04](design/04-architecture-design-method.md) | 架构设计方法 | 已审核 · 实现基线 |
| [05](design/05-storage-and-retrieval-strategy.md) | 存储结构与检索策略 | 已审核 · 实现基线 |
| [06](design/06-retrieval-pure-agent.md) | 检索工具、Agent 循环、提示词、trace 与部署 | 已审核 · 实现基线 |
| [07](design/07-html-ingestion-design.md) | HTML 入库与 collection 快照 | 已审核 · 实现基线 |
| [08](design/08-evaluation-console-design.md) | 评测台、诊断与回归 | 已审核 · 实现基线 |
| [09](design/09-technology-selection.md) | 技术选型与总体架构 | v0.4 · 实现基线 |
| [10](design/10-document-detail-design.md) | 文档详情页信息架构与数据边界 | 已审核 · 首版实现基线 |

01–09 已于 2026-10-20 审核，并作为当前实现基线；03 已于 2026-10-23 完成 criterion 自包含修订并更新为 v0.2，09 已依据 ADR 0004 更新为 v0.4。后续修改必须保留变更原因，同时更新本索引。

## 记录分类

实现阶段不在设计基线中追加流水账。不同类型的记录使用不同入口：

| 位置 | 用途 | 命名 |
| --- | --- | --- |
| [`design/`](design/) | 已审核的产品与架构设计基线 | `NN-短描述.md` |
| [`decisions/`](decisions/) | 技术取舍与 ADR | `NNNN-短描述.md` |
| `development/slices/` | 垂直切片的实现记录 | `NNN-短描述.md` |
| `development/experiments/` | 性能、质量和兼容性实验 | `YYYY-MM-DD-短描述.md` |
| `publishing/` | 面向外部读者的文章草稿 | `NN-短描述.md` |
| `operations/` | 安装、排障、备份与升级说明 | 按使用场景命名 |

临时任务、进行中状态和开放式讨论优先放在 GitHub Issue、Project 或 PR；只有形成长期结论后才整理进 `docs/`。

当前已记录：

- [实现待办](development/backlog.md)
- [对外文章草稿](publishing/README.md)
- [对外文章写作规范](publishing/writing-guide.md)
- [异步 GitHub 交付与依赖门禁](operations/async-github-delivery.md)
- [0001 · 文档与实现记录结构](decisions/0001-documentation-and-record-structure.md)
- [0002 · 使用 PyInstaller one-folder 分发后端](decisions/0002-package-backend-as-pyinstaller-directory.md)
- [0003 · Windows 按用户安装与本地数据根](decisions/0003-windows-installer-and-data-root.md)
- [0004 · 使用轻量确定性语言检测](decisions/0004-lightweight-language-detection.md)
- [0005 · 代码注释与可读性门禁](decisions/0005-code-quality-gates.md)
- [0006 · 使用当前 HTML 入库快照作为检索基线](decisions/0006-use-current-html-corpus-for-retrieval-baseline.md)
- [0007 · 人-AI 协同控制模型（试行中）](decisions/0007-human-ai-collaboration-control-model.md)
- [0008 · Harness 统一计算 Tool Observation token](decisions/0008-harness-owned-result-token-accounting.md)
- [0009 · 单开发壳承载产品和评测](decisions/0009-development-shell-with-compiled-evaluation.md)
- [0010 · 评测用例与用例集采用草稿、不可变修订版和显式启用](decisions/0010-evaluation-case-and-suite-revision-workflow.md)
- [0011 · 使用单 JSON 结构化 ReAct 响应协议](decisions/0011-structured-react-response-protocol.md)
- [0012 · 由 Harness 组织 claim 级引用标注](decisions/0012-harness-owned-claim-citation-annotation.md)
- [0008 · Harness 统一计算 Tool Observation token](decisions/0008-harness-owned-result-token-accounting.md)
- [切片控制契约与证据包模板](development/slices/TEMPLATE.md)
- [001 · 桌面 sidecar 最小启动链（已完成）](development/slices/001-desktop-sidecar-bootstrap.md)
- [002 · PyInstaller one-folder sidecar 与 release 冒烟（已完成）](development/slices/002-packaged-sidecar.md)
- [003 · Windows 安装、首次启动与数据保留（已完成）](development/slices/003-windows-installer.md)
- [004 · HTML 导入与结构预览（已完成）](development/slices/004-html-ingestion-preview.md)
- [004 · HTML 导入人工验收清单（已完成）](development/manual-tests/004-html-ingestion.md)
- [005 · SQLite 文档身份与版本登记（已完成）](development/slices/005-sqlite-document-registry.md)
- [005 · 文档保存人工验收清单（已完成）](development/manual-tests/005-document-persistence.md)
- [006 · Qwen embedding 与 dense／关键词双索引（已完成）](development/slices/006-embedding-dual-index.md)
- [007 · collection 快照、manifest 与原子发布（已完成）](development/slices/007-collection-snapshot-publish.md)
- [008 · 后台入库任务编排（已完成）](development/slices/008-background-ingestion-orchestration.md)
- [009 · 用户模型设置与 embedding Key 连通性（已完成）](development/slices/009-model-settings-onboarding.md)
- [010 · 入库工作台（已完成）](development/slices/010-ingestion-workbench.md)
- [010 · 入库工作台人工验收清单（待补充）](development/manual-tests/010-ingestion-workbench.md)
- [011 · 文档详情页（已完成）](development/slices/011-document-detail.md)
- [011 · 文档详情页人工验收清单（待验收）](development/manual-tests/011-document-detail.md)
- [012 · 入库任务串行执行与中断恢复（已完成）](development/slices/012-ingestion-task-runtime.md)
- [012 · 入库任务运行与恢复人工验收清单（已完成）](development/manual-tests/012-ingestion-task-runtime.md)
- [013 · 入库工作台交互优化（已完成）](development/slices/013-ingestion-workbench-ux.md)
- [013 · 入库工作台交互优化人工验收清单（已完成）](development/manual-tests/013-ingestion-workbench-ux.md)
- [014 · 本地时间显示与 UTC 时间戳契约（已完成）](development/slices/014-local-time-timestamp-contract.md)
- [014 · 本地时间显示人工验收清单（已完成）](development/manual-tests/014-local-time-timestamp-contract.md)
- [015 · GitHub 后台交付与失败可见性（已完成）](development/slices/015-background-delivery.md)
- [016 · 检索确定性读取层（已完成）](development/slices/016-retrieval-read-layer.md)
- [017 · 混合检索 search_blocks（已完成）](development/slices/017-hybrid-search.md)
- [018 · Agent Runtime 主循环（已完成）](development/slices/018-agent-runtime-core.md)
- [019 · Agent 与检索统一设置页（已完成）](development/slices/019-agent-settings.md)
- [019 · Agent 与检索统一设置页人工验收清单（已完成）](development/manual-tests/019-agent-settings.md)
- [020 · 流式问答工作台（已完成）](development/slices/020-streaming-chat.md)
- [020 · 流式问答工作台人工验收清单（已完成）](development/manual-tests/020-streaming-chat.md)
- [021 · Trace 持久化与本地诊断（已完成）](development/slices/021-trace-persistence.md)
- [021 · Trace 本地诊断人工验收清单（已完成）](development/manual-tests/021-trace-local-diagnostics.md)
- [022A · 评测执行内核与只读用例目录（已完成）](development/slices/022-evaluation-execution-core.md)
- [022B · 评测用例、用例集编写与快速试跑（已完成）](development/slices/022B-evaluation-authoring.md)
- [022A · 评测执行内核人工验收清单（已完成）](development/manual-tests/022-evaluation-execution-core.md)
- [022B · 评测用例与用例集编写人工验收清单（已完成）](development/manual-tests/022B-evaluation-authoring.md)
- [023A · 程序 Checker 与 LLM Judge（已完成）](development/slices/023A-evaluation-judgment.md)
- [023A · 程序 Checker 与 LLM Judge 人工验收清单（已完成）](development/manual-tests/023A-evaluation-judgment.md)
- [023B · 生效判定绑定与 N/K 聚合（已完成）](development/slices/023B-evaluation-aggregation.md)
- [023B · 生效判定绑定与 N/K 聚合人工验收清单（已完成）](development/manual-tests/023B-evaluation-aggregation.md)
- [024A · 评测结果矩阵（已完成）](development/slices/024A-evaluation-result-matrix.md)
- [024A · 评测结果矩阵人工验收清单（已完成）](development/manual-tests/024A-evaluation-result-matrix.md)
- [024B · 基线管理与跨 run 比较（已完成）](development/slices/024B-evaluation-baseline-comparison.md)
- [024B · 基线与比较人工验收清单（已完成）](development/manual-tests/024B-evaluation-baseline-comparison.md)
- [025 · 长答案引用支持分批检查（已完成）](development/slices/025-evaluation-citation-support-batching.md)
- [025 · 引用支持分批检查人工验收清单（已完成）](development/manual-tests/025-citation-support-batching.md)
- [026 · 结构化 ReAct 输出协议（已完成）](development/slices/026-structured-react-protocol.md)
- [026 · 结构化 ReAct 人工验收清单（已完成）](development/manual-tests/026-structured-react-protocol.md)
- [027 · Claim-aware 引用标注（已完成）](development/slices/027-claim-aware-citation-annotation.md)
- [027 · Claim-aware 引用标注人工验收清单（已完成）](development/manual-tests/027-claim-aware-citation-annotation.md)
- [028 · 拆开边界违规、跨项检查与引用切分版本（已完成，待合并后复跑）](development/slices/028-evaluation-check-separation.md)
- [029 · 引用标注分批与联合支持判定（已完成，待真实重判）](development/slices/029-citation-evaluation-reliability.md)
- [029 · 引用评测可靠性人工验收（待验收）](development/manual-tests/029-citation-evaluation-reliability.md)
- [030 · Agent 证据强度与转折保真（已否决，前提被人工复核推翻）](development/slices/030-agent-claim-strength-fidelity.md)
- [031 · 引用语义同一性与判官稳定性校准（已完成）](development/slices/031-citation-semantic-calibration.md)
- [031 · 引用语义与 annotation 校准人工验收（已完成）](development/manual-tests/031-citation-semantic-calibration.md)
- [2026-10-26 · citation-rubric-v7 语义与稳定性校准](development/experiments/2026-10-26-citation-rubric-v7-calibration.md)
- [032 · 判定总 Token 用量展示（待人工验收）](development/slices/032-judge-token-usage-visibility.md)
- [033 · 异步 GitHub 交付与依赖门禁（待后台验证）](development/slices/033-async-github-delivery-gate.md)
- [009 · 用户模型设置人工验收清单（已完成）](development/manual-tests/009-model-settings.md)

## 旧仓库

旧学习项目保留在 [lipeidonggz/ai-study-helper](https://github.com/lipeidonggz/ai-study-helper)。其中的代码、测试、数据与历史结论仅作参考，不构成本仓库的实现输入。
