# 018 · Agent Runtime 主循环

- 状态：已完成
- 开始日期：2026-10-22
- 关联设计：[06 检索侧 Agent 循环](../../design/06-retrieval-pure-agent.md)、[09 LangGraph 与 DeepSeek 接入](../../design/09-technology-selection.md)、[ADR 0008 Harness token 归属](../../decisions/0008-harness-owned-result-token-accounting.md)
- 前置切片：[017 混合检索 search_blocks](./017-hybrid-search.md)

## 控制契约

### 用户可观察目标

1. 一个问题可以在同一 collection 快照上完成多轮工具调用，直到模型输出 `Finish`。
2. 工具结果以统一 Observation 返回模型；协议错误可被显式观察和恢复。
3. 请求达到工具次数或 token 预算时停止继续检索，并给出明确终止原因。

### 核心不变量

1. 请求开始时固定 collection 快照，一次运行内所有工具共用同一个 reader。
2. Agent 只能调用不可变工具白名单中的五个检索工具。
3. 模型每次响应只允许一个 Thought 和一个 Action；协议解析失败不执行工具。
4. 工具业务 payload 不携带 `result_tokens`；Harness 对最终 Observation 统一计数。
5. 每次工具或模型瞬时失败最多重试 1 次；预算错误不重试。

### 明确禁止

- 不让模型选择 collection 快照或检索模式；
- 不让模型决定 token 预算；
- 不在工具失败时静默降级或伪造回答；
- 不在 018 内接入聊天 UI、SSE 网络层或完整 trace viewer；
- 不在提示词尚未人工复核时把它标成发布基线。

### 失败语义

| 场景 | Harness 结果 |
| --- | --- |
| Thought／Action 格式错误 | 追加 `protocol_error` Observation，下一轮修正 |
| 未在白名单的工具 | 追加 `tool_unavailable`，不执行 |
| 工具业务错误 | `harness_status=ok`，payload 内返回业务错误 |
| 上下文预算不足 | `context_budget_exceeded`，停止继续检索 |
| 请求累计预算不足 | `request_budget_exceeded`，停止继续检索 |
| 模型瞬时失败 | 最多重试 1 次，仍失败则终止并返回明确错误 |

### 决策预算

- 预计 B 级决策：7 项。
- C 级决策：0 项，但提示词基线生效前必须人工复核。

## 目标

用 LangGraph 推进状态、由自有 Harness 掌握协议与预算，完成可注入 fake 模型和真实 DeepSeek provider 的单轮 Agent 主循环。

## 输入与输出

输入：

- 用户问题；
- 请求级 `AgentRunConfig`；
- 固定的 retrieval reader；
- embedding provider 与 chat provider。

输出：

- 结构化运行事件；
- 最终回答或稳定终止错误；
- 每轮 Thought／Action、Observation、工具调用次数、模型调用次数和预算状态。

## 不包含

- 聊天 UI 与 SSE HTTP 事件流；
- 多轮会话存储、历史和长期记忆；
- 完整 OpenTelemetry／JSONL trace exporter；
- 评测台；
- 用户设置界面；
- 模型或检索模式的动态路由。

## 验收条件

- [x] 可用 fake chat provider 完成“工具调用 → Observation → Finish”闭环。
- [x] 五个工具都通过统一工具适配器执行，且白名单外调用被拒绝。
- [x] Action 解析覆盖 Finish、非法 JSON、多个 Action、未知工具和 Finish 后尾随内容。
- [x] Harness 对最终 Observation 统一计算 `result_tokens`，并写入运行事件。
- [x] `max_tool_calls=8`、上下文与请求预算均有确定性测试。
- [x] 模型瞬时失败最多重试 1 次，预算失败不重试。
- [x] DeepSeek Key 可单独保存和测试，真实 provider 使用 `deepseek-flash` 与 OpenAI-compatible `base_url`。
- [x] 没有活动快照、embedding Key 缺失或 chat Key 缺失时返回明确错误。
- [x] 完整质量门通过。

## 待确认决策

| ID | 等级 | 问题 | 建议 | 候选与取舍 |
| --- | --- | --- | --- | --- |
| D1 | B | 018 交付边界 | 只做后端 Runtime、DeepSeek Key 后端接口和可注入事件输出；聊天 UI／SSE 放 019 | 同时做 UI 会混合模型协议、图状态、网络流和前端状态，失败定位困难 |
| D2 | B | `result_tokens` 首版实现 | 定义可注入 `ObservationTokenCounter`；首版使用与模型 tokenizer 解耦的固定保守估算器 `chat-budget-conservative-v1`，记录版本，真实 provider usage 另记用于后续标定 | 立即随包 DeepSeek tokenizer 需要先验证分发与体积；设计允许使用标定后的保守估算器作为正式后备路径 |
| D3 | B | 系统提示词基线 | Runtime 使用设计阶段已评审的提示词作为版本化 v1 prompt bundle，并记录模板版本与 hash | 设计基线已经人工评审；若再次重建草案会造成提示词实现与设计文档分叉 |
| D4 | B | 工具描述与 schema 的呈现 | 当前实现使用浓缩中文描述＋ JSON schema；拟改为设计文档中的完整工具说明，并保留 schema | 工具说明是提示词基础件；当前实现偏离了已评审的工具语义文字 |
| D4.1 | B | `title_contains` 与别名的关系 | 来源筛选支持别名；`title_contains` 只匹配规范标题子串 | 设计描述原句与 T1 明确约束冲突，需要收敛为单一语义 |
| D5 | B | DeepSeek 采样与输出上限 | 显式关闭 thinking，保留应用层 ReAct；`temperature=0.0`、`max_output_tokens=8192`、SDK 自动重试关闭、60 秒超时 | 显式 ReAct 已承担规划；默认 thinking=high 会重复推理并增加成本；8192 兼顾多文档综合 |
| D6 | B | 保守 token 估算公式 | 对规范 JSON 的基础估算乘以 `1.15` 后加 `8` token 固定余量 | 需要防止低估；真实样本中已观察到明显高估 |
| D6.1 | B | usage 缺失、失败尝试与下一轮上下文估算 | 成功调用使用 provider 实际 usage；下一轮以上一次实际 input tokens 为锚点，只估算新增输出、Observation、system prompt 变化和包装开销；usage 缺失或失败尝试使用保守估算回退并标记来源 | 设计要求 usage 缺失时估算，不能静默按 0；同时避免每轮全量重算历史 |
| D7 | B | Agent API 的运行失败表达 | `/api/agent/run` 对预算、模型等运行失败返回 HTTP 200 + `status=failed`；缺少 Key／数据根仍返回 HTTP 4xx／5xx | 需要区分“请求已执行但运行失败”和“请求无法启动” |
| D8 | B | 如何保证预算导致的不完整回答被明确呈现 | Harness 保存结构化 `answer_completeness=partial` 与 `limitations[]`，并在最终回答前加入确定性预算警告 | 提示词不能保证模型主动披露预算限制，不能让模型自述成为唯一事实源 |

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 018 交付边界 | 只做后端 Runtime、DeepSeek Key 后端接口和可注入事件输出；聊天 UI／SSE 放 019 | 06／09 要求先稳定领域契约与图状态 | 同时实现 UI 会扩大故障面并混合传输层与运行时问题 | 018 可通过受保护后端接口和 fake provider 验证，UI 在 019 接入 | 工具循环与 Key 后端接口集成测试 | 已确认 |
| D2 | B | `result_tokens` 首版实现 | 定义可注入 `ObservationTokenCounter`；首版使用版本化保守估算器 `chat-budget-conservative-v1` | ADR 0008 将计数收敛到 Harness；09 允许 tokenizer 不可稳定分发时采用标定估算器 | 随包模型 tokenizer 需要先验证依赖和体积；直接用字符数估算又缺少完整 payload 口径 | 预算可能略偏保守，但不会绑定某个 tokenizer 实现 | canonical JSON payload 计数、版本稳定和 usage 偏差记录测试 | 已确认 |
| D3 | B | 系统提示词基线 | 使用设计阶段已评审的提示词作为 v1 bundle，并记录版本与内容 hash | 沛东确认设计阶段提示词已评审 | 重新起草会制造第二份事实源；直接内联字符串会让版本不可追溯 | Runtime 可端到端验证，同时保留未来 prompt 实验入口 | prompt bundle hash 与渲染测试 | 已确认 |
| D4 | B | 工具描述与 schema 的呈现 | 使用设计文档的完整工具语义，并附 JSON schema；`lang` 只允许 `zh/en` | 06 要求工具说明同时承担局部选择规则 | 完整说明更长但保留使用边界；浓缩说明会遗漏关键否定与完整性语义 | 直接影响模型选择工具的质量 | 工具描述快照与真实模型工具选择回归 | 已确认 |
| D4.1 | B | `title_contains` 与别名的关系 | 来源筛选支持 `source_label`／别名，`title_contains` 只匹配规范标题 | 06 T1 的大小写匹配与标题约束 | 标题也匹配别名会增加歧义；只匹配规范标题更可预测 | 工具行为与设计文字统一 | 文档筛选测试 | 已确认 |
| D5 | B | DeepSeek 采样与输出上限 | 显式 `thinking.type=disabled`，应用层保留 ReAct；`temperature=0.0`、`max_output_tokens=8192`、`max_retries=0`、超时 60 秒 | 06 已定义显式 Thought／Action 循环；DeepSeek 默认 thinking=high 会重复消耗推理 token | 开启隐藏推理可改善复杂归纳但增加单轮延迟和成本；关闭后须用评测验证质量 | output token 和延迟预计下降；复杂多文档质量需观察 | provider 参数测试与真实 DeepSeek 回归 | 已确认 |
| D6 | B | 保守 token 估算公式 | 规范 JSON 基础估算 × `1.15` ＋ `8` token | ADR 0008 要求 Harness 统一计数；09 允许标定估算器 | 更高余量更安全但更早触发预算；更低余量节省预算但可能低估 | 两轮真实验证分别高估约 56%／57%，首版接受保守余量 | 确定性计数测试与真实 provider usage 对照 | 已确认 |
| D6.1 | B | usage 缺失、失败尝试与下一轮上下文估算 | 成功调用使用 provider `prompt_tokens + completion_tokens`；下一轮以上一次实际 input tokens 为锚点，只估算新增输出、Observation、system prompt 变化和包装开销；缺失或失败尝试使用保守估算回退并标记 `usage_source` | 06 要求缺失 usage 时估算，实际请求全部计入预算；设计也需要减少重复估算累计误差 | 全量重算会重复估算历史并持续放大误差；全部相信缺失 usage 会低计 | 请求预算更接近真实用量，上下文准入只估算增量 | 实际 usage、缺失 usage、失败尝试、增量上下文和 tool observation 测试 | 已确认 |
| D7 | B | Agent API 的运行失败表达 | 运行失败使用 HTTP 200 + 结构化 `status=failed`；启动前置错误仍用 HTTP 409／503 | 需要区分“请求已执行但运行失败”和“请求无法启动” | 全用 HTTP 错误会让客户端丢弃运行事件；全用 200 会混淆前置错误 | 影响前端和评测端错误处理 | API 失败路径测试 | 已确认 |
| D8 | B | 如何保证预算导致的不完整回答被明确呈现 | Harness 独立保存 `answer_completeness=partial`、`limitations[]`、触发原因、可调参数名和限制值；最终 API 返回结构化状态，并确定性附加用户可见警告 | 06 要求预算超界时说明未完成部分，不能把生成侧承诺当作保证 | 仅依赖 prompt 会让模型可能隐瞒超界；让第二次 LLM 判断会引入更多成本和不确定性 | 用户能区分 `max_tool_calls`、`max_context_tokens` 和 `max_request_tokens` 应调整哪一项 | 预算失败到 Finish、结构化参数与确定性警告测试 | 已确认 |

## 证据包

### 自动化证据

- Agent Protocol、chat provider、Runtime、tool adapter 和后端设置定向测试：26 passed。
- `corepack pnpm quality`：164 passed。
- Ruff、Pyright、Vue typecheck、前端构建、Rust fmt／check／clippy：通过。
- PyInstaller one-folder：1,520 files、404.6 MB；打包 sidecar `/health` 返回 200。

### 运行证据

- 当前真实 collection `s_6b787258ba7d700c` 上完成一次关闭 thinking 的 Agent 工具循环：
  `list_documents → list_blocks → Finish`，共 2 次工具调用、3 次模型决策，
  使用 13,990 input tokens、2,671 output tokens。
- 该运行使用真实 DeepSeek `deepseek-flash`、真实 Qwen query embedding 和当前活动快照，
  验证固定快照、工具适配、Observation、prompt hash 与 Harness `result_tokens` 事件。
- 增量上下文账本在真实运行中生效：第 2 轮预估 2,220、实际 2,145；第 3 轮预估 12,799、
  实际 9,945，均使用 `provider_anchor`。
- DeepSeek 最小连通性测试成功；供应商返回 prompt／completion usage，未返回 request ID。
- `scripts/set-chat-key.ps1` 提供隐藏输入方式，把 DeepSeek Key 直接写入 Windows
  Credential Manager；Key 不进入命令行、仓库文件或日志。

### 人工验收

- [x] D1-D3 已确认。
- [x] D4、D4.1 已确认。
- [x] D5 已确认。
- [x] D6 已确认。
- [x] D6.1 已确认。
- [x] D7 已确认。
- [x] D8 已确认，`status=partial`、`limitations[]` 与确定性警告已实现。
- [x] 反向解释第 1-4 题已通过。
- [x] 反向解释第 5 题已通过：请求期间固定旧快照，新快照只供后续请求使用。
- [x] DeepSeek Key 已通过安全入口录入，真实对话模型和 Agent 工具循环验证通过。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 工具结果超过上下文预算时如何处理？ | 初次回答“直接中止”不完整；纠正后确认完整结果丢弃、Harness 报告超界并关闭工具 | 通过 | 新增 D8，确保不依赖模型主动说明 |
| R2 | 达到 `max_tool_calls` 后如何处理？ | 不能继续调用；Harness 拦截；处理方式应与预算超界类似 | 通过 | 补充“先调用一次、再越界尝试”的回归测试 |
| R3 | 多个 `Action:` 如何处理？ | 不执行第一个；返回显式协议错误 Observation，由模型下一轮 Thought 决定 | 通过 | 无 |
| R4 | 工具返回 `scope_not_found` 时如何表达？ | `harness_status=ok`；这是正常业务流程，由模型继续处理 | 通过 | 无 |
| R5 | 运行中发布新快照时使用哪个版本？ | 继续使用请求开始时固定的旧快照，新快照只供后续请求 | 通过 | 无 |

<!-- reverse-explanation-gate: passed 5/5 -->

## 协作复盘

- fake provider 先锁定协议、预算和失败路径，随后真实 DeepSeek 首轮暴露了 reasoning token
  会占用 `max_tokens` 的问题；通过提高最小测试预算并检查 `finish_reason` 修正，避免把“预算截断”
  误判成“鉴权失败”。
- 真实 Agent 已按设计自然选择 `list_documents → search_blocks → list_blocks → Finish`，
  证明工具描述与 ReAct 文本协议可被实际模型遵循。

## 已知风险与后续

- LangGraph 与 OpenAI SDK 已通过 Windows PyInstaller 打包和 sidecar 启动冒烟；Agent 使后端目录从
  约 396.6 MB 增至 404.6 MB。018 使用不依赖模型 tokenizer artifact 的保守估算器。
- DeepSeek 供应商未返回 request ID；后续 trace 必须记录“不提供”而不是伪造。
- 聊天 UI、SSE、trace 和多轮会话在后续切片完成，018 只保证核心运行时可测试。
- 关闭 thinking 后，两次真实问法中一次直接 Finish、未调用知识库工具，另一次正常调用工具。
  这是工具选择稳定性风险，必须由后续 N 次评测比较 thinking disabled／low 的质量与延迟。
- 流程复盘：实现完成后遗漏了 D4-D7 的集中确认和负责人反向解释，不能以自动化测试
  和真实调用代替；本次已把切片退回“待确认”。
- 负责人追问累计 token 口径后发现：当前 `used_tokens` 只累计成功响应的 provider usage，
  缺失 usage 时按 0 处理，失败尝试也未计入请求预算；这不符合设计，已登记 D6.1。
- 反向解释第 1 题暴露了预算披露只依赖提示词的缺口：模型可能不说明超界而直接给出
  看似完整的回答；D8 已将 `partial`、结构化 limitations 和用户可见警告设为 Harness 强制行为。
