# 021 · Trace 持久化与本地诊断

- 状态：已完成
- 开始日期：2026-10-22
- 关联设计：[06 可观测性与可复现性](../../design/06-retrieval-pure-agent.md)、[08 评测台](../../design/08-evaluation-console-design.md)
- 前置切片：[018 Agent Runtime](./018-agent-runtime-core.md)、[020 流式问答工作台](./020-streaming-chat.md)
- 人工验收：[021 Trace 本地诊断人工验收](../manual-tests/021-trace-local-diagnostics.md)

## 控制契约

### 用户可观察目标

1. 每次问答都有稳定 `trace_id`，成功、部分完成、失败和取消都能在应用重启后被查看。
2. 用户可以从问答结果打开运行详情，看到轮次、模型调用、工具调用、预算、token、耗时、错误和证据来源。
3. trace 写入异常时问答仍能继续，诊断页明确显示完整、部分或丢失状态。

### 核心不变量

1. Agent Runtime 只调用 `TraceEmitter`，不直接持有 trace 文件句柄，也不因 trace 失败重试或中止问答。
2. `trace_id` 使用 32 位小写十六进制 W3C 标识，`span_id` 使用 16 位小写十六进制；子 span 保留父关系。
3. 每次运行最终生成 manifest，区分 `complete`、`partial`、`dropped`，并记录终止原因、版本、快照、token 和预算状态。
4. `metadata` 只保存问题前 80 个字符作为运行列表标签，以及 hash／计数／状态；不保存完整问题、提示词、Action、工具 payload 或回答正文。`full` 才保存上述完整 artifact。任何模式都不得记录 API Key。
5. 本地 viewer 直接读取 trace 文件；派生 `index.sqlite` 损坏时可重建，不反向成为事实源。

### 明确禁止

- 不让 trace I/O 阻塞模型或工具调用；
- 不把 trace 写入失败伪装成问答失败；
- 不在 `metadata` 模式保存提示词或文档正文；
- 不把 trace 当知识库事实源，也不让评分混入运行时 trace；
- 不在本切片实现评测跑批、判官、OTLP 或诊断包导出。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| trace 目录不可写 | 回答正常；从该次问答打开诊断时显示“trace 未写入或已清理” | `trace_id`、日志告警；磁盘完全不可写时无法伪造落盘失败记录 | 修复磁盘后下次运行可恢复 |
| 有界队列满 | 回答正常，诊断显示“trace 部分记录” | 已落盘事件、`dropped_events` | 是 |
| manifest 写入失败 | 已有 JSONL 可读，诊断显示不完整 | JSONL 与失败原因 | 可重建索引 |
| artifact 缺失或 checksum 不符 | 来源 span 保留，artifact 显式失效 | hash、artifact 状态 | 不能伪造恢复 |
| 应用异常退出 | 留下已 flush 的部分 trace | 启动时恢复索引 | 是 |

### 人工检查点

- 编码前确认 021 范围、模式默认、writer 策略和 viewer 位置；
- 后端 trace 跑通后先核对一次成功与预算失败 trace；
- viewer 主路径跑通后确认真实问答可打开详情；
- 最后人工制造只读目录或队列满，确认问答不受影响。

### 决策预算

- 预计 B 级决策：5 项。
- C 级决策：无。
- 超过预算时的拆分方式：保留 021A trace 后端与最小 viewer，把保留清理、导出和复杂查询拆为后续。

## 目标

为每次 Agent 问答建立本地、非阻塞、可恢复的 trace 事实链，并提供最小本地诊断入口，为评测执行和失败归因提供 `trace_id` 现场。

## 输入与输出

输入：

- 请求开始时间固定的 `trace_id`、运行配置、prompt／tool／loop 版本和 collection 快照；
- Runtime 的轮次、模型、工具、预算、来源和终止事件；
- 用户问题、渲染提示词、模型原始输出、工具 payload 和最终回答。

输出：

- `traces/YYYY-MM-DD/{trace_id}/trace.jsonl`；
- 同目录 `manifest.json` 和可选 `artifacts/`；
- `traces/index.sqlite` 派生查询索引；
- 本地 trace 列表与详情 API／UI；
- 每次运行明确的 `trace_status` 和 `replay_scope`。

## 不包含

- 评测 run、自动判官和指标聚合；
- 诊断包导入／导出；
- OTLP／Langfuse 外部接入；
- trace 数据分析和采样；
- 多轮会话与会话记忆。

## 验收条件

- [ ] 成功、部分成功、失败和取消问答均生成可查询 trace。
- [ ] 应用重启后仍能列出并打开历史 trace。
- [ ] 成功 trace 可还原请求、轮次、模型／工具 span、预算检查和最终回答。
- [ ] `search_blocks` trace 包含模式、Top-K、空结果原因、候选 rank／score 和 query embedding usage。
- [ ] `full` artifact 引用可打开并通过 checksum；`metadata` 引用明确标注 `omitted`。
- [ ] trace 目录不可写或队列满时，主问答仍完成并显示明确降级状态。
- [ ] `index.sqlite` 损坏或删除后可从 trace 文件重建。
- [ ] API Key 不出现在 trace、manifest、artifact、索引或应用日志。
- [ ] 完整质量门与真实桌面人工验收通过。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 021 交付边界 | 一次完成 TraceEmitter、非阻塞 writer、JSONL／manifest／artifacts／index 和最小本地 viewer；覆盖成功、部分成功、失败、取消及 trace 降级；评测跑批、判官、诊断包导出和 OTLP 留后续 | 06 要求本地 trace 和 viewer；08 依赖 `trace_id` | 只做文件不满足日常诊断；同时做评测会扩大故障面 | 本切片闭合日常运行诊断主路径 | 成功／失败 trace 到 viewer 的端到端测试 | 已确认 |
| D2 | B | trace 模式与默认值 | 支持 `off`／`metadata`／`full`；开发默认 `full`、分发默认 `metadata`；用户可全局切换且只影响之后运行。`metadata` 额外保存问题前 80 个字符作为本地运行列表标签，但不保存完整问题正文或其他正文 artifact | 06 定义三种模式；运行列表需要可辨认标签 | 完全不保存问题会让 metadata trace 无法区分；保存完整问题会扩大默认隐私面 | 在可辨认性与最小内容留存之间取折中 | 三种模式 artifact、问题预览截断和 manifest 测试 | 已确认 |
| D3 | B | Trace schema 与现有事件映射 | 不依赖完整 OTel SDK，但使用 32 位 `trace_id`、16 位 `span_id`、OTel 字段和父子 span；建立 `agent.turn`／`llm.call`／`tool.call`，预算检查作为 span event；`full` 写 prompt／Action／payload／answer artifact，`metadata` 只保留 hash、计数和状态；SSE 展示事件不另造 trace span | 06 只要求兼容 OTel 数据模型和本地 exporter；显式 emitter 更易保证非阻塞、artifact 与脱敏边界 | 完整 SDK 会增加包体积，且其批处理和退出行为仍不足以直接满足 `dropped_events`／`trace_status` 要求 | 后续可通过 OTLP exporter adapter 接入标准生态 | schema 快照、W3C ID、父子树、预算事件与 artifact omitted 测试 | 已确认 |
| D4 | B | Writer 队列与保留策略 | 待确认 | 06 要求有界队列、后台 writer、best-effort flush 和时间／容量上限 | 同步写最可靠但影响主流程；完全异步且无 flush 会丢最后事件 | 影响可靠性、磁盘与退出行为 | 队列满、只读目录、退出 flush 和清理测试 | 待确认 |
| D5 | B | 最小本地 viewer | 不增加主导航；在设置中提供“运行诊断”，问答运行 ID／状态可直接打开本次 trace。列表展示最近请求摘要与状态，详情展示 span 时间线、轮次树、模型／工具、预算、错误、版本、快照和 artifact；同时提供全局 trace 模式切换。首版不做筛选、图表、导出、OTLP 和单条删除 | 06 要求直接读取本地 trace，不依赖导入或 OTLP | 纯 API 不满足普通用户自助诊断；完整 trace 分析工作台超出本切片 | 从异常回答可直达现场，同时避免主导航膨胀 | 列表、轮次树、span 详情、artifact、模式切换与降级展示 | 已确认 |

## 证据包

### 自动化证据

- 后端完整测试：187 passed。
- `quality:quick`：通过。
- TraceStore 覆盖完整／元数据模式、W3C ID、父子 span、artifact checksum、索引重建、保留清理、队列满降级。
- Runtime 覆盖成功问答的请求 → 轮次 → 模型／工具 → 预算 → 回答 span 树。
- API 覆盖 trace 列表、详情、artifact 下载、设置持久化与 trace 模式。

### 运行证据

- trace 事实文件布局：`traces/YYYY-MM-DD/{trace_id}/trace.jsonl`、`manifest.json`、`artifacts/`。
- 派生索引：`traces/index.sqlite`，删除后可由 manifest 重建。
- 本地诊断页支持最近运行列表、span 时间线、版本／预算详情、artifact 与 trace 降级状态。
- 设置页支持全局 `off`／`metadata`／`full`，问答运行 ID 可直接进入诊断页。

### 失败探针

- 队列满时 Agent 回答仍成功，trace 标记为 `partial` 且记录 dropped events。
- artifact checksum 不匹配时拒绝读取。
- 元数据模式不包含完整问题、提示词、工具正文或回答正文，只保留问题前 80 个字符和 hash／omitted 引用。
- 索引文件删除后从 manifest 自动重建。

### 人工验收

- [x] 沛东已按 [021 人工验收清单](../manual-tests/021-trace-local-diagnostics.md) 完成真实窗口验收。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | trace 写失败为什么不能影响问答？ | 问答是主流程，trace 是可选辅助流程，辅助写失败不能让主流程失败。队列满或部分写入时诊断显示 partial／dropped；根目录完全不可写时只能保留 `trace_id` 和日志，并在本次运行入口明确提示 trace 不存在，不能伪造落盘记录 | 通过 | 个人产品不增加独立失败关系表，保留为接受风险 |
| R2 | `metadata` 与 `full` 模式分别能证明什么、不能证明什么？ | metadata 可做版本审计、流程和指标判断，但不能复现提示词、工具 payload 或证据；full 可做证据复现，但检索重放还要求对应 collection 快照仍存在且可读，快照缺失时降级为“证据可复现、不可检索重放” | 通过 | 无 |
| R3 | `index.sqlite` 损坏后为什么不能丢失原始 trace？ | 原始事实是 trace 文件、manifest 和 artifacts；`index.sqlite` 只是加速查询的派生索引，损坏或删除后应从 manifest 重建，不能反过来把索引当事实源 | 通过 | 无 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 协作复盘

- AI 明显加速的环节：trace schema、非阻塞 writer、Runtime 插桩、索引重建、保留策略和 Viewer 可以围绕现有事件模型快速串通。
- 负责人失去系统模型的环节：首版没有把“trace 根目录完全不可写时，失败本身也无处持久化”说清；metadata 只存 hash 与运行列表可用性的冲突也由人工验收暴露。
- 真正发现问题的控制点：负责人逐项检查 `full`、`metadata`、`off`、问题预览和 omitted artifact 展示，使数据留存边界变得可感知。
- 只有仪式感、没有产生判断价值的流程：本轮反向问题整体有效，没有明显纯仪式项。
- 下一次准备调整的一到两项：涉及可选辅助设施的失败语义时，先明确“辅助数据本身不可写”这一极端边界；数据模式先讲清最少保留字段再设计 UI。

## 已知风险与后续

- 未验证项：真实 trace 体积、写入吞吐、应用退出时 flush 时限和保留阈值。
- 接受的风险：待决策后填写。
- 接受的风险：trace 根目录完全不可写时，失败本身无法持久化为 trace；只能保留运行日志和打开诊断时的明确缺失提示。
- 后续待办：评测执行与判官、诊断包导出、OTLP 接入、复杂 trace 查询。
