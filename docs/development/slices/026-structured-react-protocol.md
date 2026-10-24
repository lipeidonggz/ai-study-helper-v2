# 026 · 结构化 ReAct 输出协议

- 状态：已完成
- 开始日期：2026-10-24
- 关联设计：[06 检索与纯 Agent](../../design/06-retrieval-pure-agent.md)、[08 评测台](../../design/08-evaluation-console-design.md)
- 前置切片：[018 Agent Runtime 主循环](./018-agent-runtime-core.md)、[025 长答案引用支持分批检查](./025-evaluation-citation-support-batching.md)
- 人工验收：[026 结构化 ReAct 人工验收](../manual-tests/026-structured-react-protocol.md)

## 控制契约

### 用户可观察目标

1. 模型每轮只能返回一个严格 JSON 对象，明确表示 `tool_call` 或 `finish`，不再通过自由文本 ReAct transcript 表达动作。
2. 模型不能输出 `tool_result`；工具结果只能由 Harness 在执行真实工具后追加。
3. 连续协议错误达到预算后，Runtime 停止重试、进入强制 finalization；仍失败时以可判定的产品失败结束。
4. 即使发生递归或 Runtime 异常，数据库仍保留实际 trace、模型调用次数、token 和已有的部分状态。
5. 新协议通过 `agent_loop_version`、prompt version 和 trace 版本固定，不能与旧协议 run 静默混比。

### 核心不变量

1. 模型响应必须是单个完整 JSON 对象；前后不得有 Markdown、解释文字或第二个对象。
2. `type=tool_call` 必须包含合法 `tool` 和对象型 `arguments`；`type=finish` 必须包含字符串 `answer`。
3. `decision_summary` 必须存在且非空，只记录简短、可审计的决策依据，不作为隐藏思维链。
4. `protocol_error` 连续计数：第 2 次后进入 finalization；finalization 再次非法时以 `agent_protocol_loop` 结束。
5. Harness 工具结果与协议错误使用 Runtime 独占的消息类型；模型伪造这些类型只能得到协议错误。

### 明确禁止

- 不接受 Markdown 代码围栏包裹的动作；
- 不接受 Action／Finish 自由文本；
- 不接受模型输出 `tool_result`、`protocol_error` 或其他 Harness-only 类型；
- 不把模型协议错误分类为基础设施失败；
- 不在异常时丢弃已有 trace_id、usage、模型调用数或部分回答；
- 不把结构化输出等同于原生 tool calling，本切片仍由 Runtime 解析 JSON 并执行工具。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 非法 JSON／额外文本 | 第一次协议错误，Runtime 继续重试 | 原始模型输出与协议错误事件 | 是 |
| 连续第 2 次协议错误 | 工具关闭，进入强制 finalization | 两次原始输出与错误原因 | 再尝试一次 |
| finalization 仍非法 | `agent_protocol_loop`，作为产品失败 | trace、全部模型输出、usage、部分回答 | 重跑新 attempt |
| 模型输出 `tool_result` | 协议错误，不执行任何模型伪造结果 | 原始输出和拒绝原因 | 是 |
| GraphRecursionError | 转换为稳定的产品失败终态 | 已建立 trace、调用数、token 和最后状态 | 重跑新 attempt |
| provider 超时／限流 | 保持现有基础设施失败分类 | 既有 provider 错误现场 | 按基础设施策略替代 |

### 人工检查点

- 编码前确认单 JSON schema、协议错误预算和版本升级；完成前由负责人复核。
- 先验证 tool_call、finish、伪造 tool_result 和连续错误路径。
- 完成后用相同 case 复现旧 `Observation:` 污染，确认不再形成无限 ReAct transcript。
- 人工审阅 `chat-v4` 与新协议 JSON schema 后才能启用。

### 决策预算

- 预计 B 级决策：4 项。
- C 级决策：无。
- 不拆分：协议 schema、解析、错误预算、trace 收尾共同决定同一 Agent 行为版本。

## 目标

把自由文本 ReAct 改为单 JSON 结构化 ReAct，消除模型续写伪造 Observation 的协议空间，并保证循环失败可收敛、可判定、可审计。

## 输入与输出

输入：用户问题、结构化历史、Runtime 追加的工具结果、预算配置。

输出：

- `tool_call`：工具名、参数和决策摘要；
- `finish`：最终答案和决策摘要；
- 协议错误事件与可收敛的产品失败终态；
- 新版本 trace 和完整 usage。

## 不包含

- 不切换到供应商 native tool calling；
- 不实现隐藏思维链或要求模型输出完整内部推理；
- 不增加并行工具调用；
- 不改变检索工具契约、评测判官或评分规则；
- 不修改历史 run 的协议格式。

## 验收条件

- [x] `tool_call` 与 `finish` 的 JSON schema 严格校验。
- [x] 模型伪造 `tool_result`、多 JSON、代码围栏和尾部正文均被拒绝。
- [x] 连续协议错误按预算进入 finalization 并最终收口。
- [x] 协议循环作为产品失败进入 N 与判官，不记基础设施失败。
- [x] 异常时 trace_id、模型调用数、token 和部分回答不丢失。
- [x] 旧协议 run 保持可读，新 run 使用新的 agent loop 版本。
- [x] 完整质量门、故障回归和真实复现通过。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 模型动作的表达方式 | 每轮一个 JSON 对象，`type=tool_call｜finish`；tool_call 的 `decision_summary` 必填，finish 可选 | 保留 ReAct 显式决策语义，同时消除文本 transcript 续写空间 | 自由文本 ReAct 易伪造 Observation；native tool calling 会改变显式决策可见性和 provider 兼容面 | 决定 prompt、parser、state schema 和 trace | schema、错误格式、工具执行与 Finish 测试 | 已确认 |
| D2 | B | 协议错误预算 | 连续 2 次非法响应后关闭工具进入 finalization；再失败一次则以 `agent_protocol_loop` 结束 | 既要允许一次格式修复，也不能无限消耗预算 | 一次就终止过于脆弱；继续重试会重现 42 次协议错误问题 | 决定循环上限、产品失败分类和成本 | 连续错误、成功恢复与 finalization 测试 | 已确认 |
| D3 | B | Harness-only 消息 | `tool_result` 和 `protocol_error` 只能由 Runtime 生成，模型输出这些类型一律非法 | Harness 是工具事实和执行状态的唯一生产者 | 允许模型写 tool_result 会直接污染上下文，等同伪造 Observation | 决定解析器和 history 安全边界 | 伪造类型、重复结果与上下文污染测试 | 已确认 |
| D4 | B | 版本与异常现场 | 新 loop 使用 `react-structured-v3`／`chat-v4`；异常保留 trace 与 usage，旧 run 不改写 | 协议变化会改变 Agent 行为；ADR 0007 要求可解释与可恢复 | 不升级版本会让新旧结果混比；异常后统一清零会破坏审计 | 决定 trace、数据库和评测比较兼容性 | 版本快照、递归异常与 trace 回写测试 | 已确认 |

等级：

- A：AI 可自主执行，交付时列入审计摘要；
- B：完成后集中人工确认；
- C：执行前必须停止并获得人工决定。

## 证据包

### 自动化证据

- 命令：`backend\.venv\Scripts\python.exe -m pytest backend\tests -q --basetemp=.pytest-tmp-026-full4`
- 结果：255 passed。
- 命令：`corepack pnpm quality:quick`
- 结果：通过。
- 覆盖的不变量：
  - tool_call／finish 单一 JSON 校验；
  - 伪造 tool_result、重复键、额外字段、多个 JSON 和代码围栏拒绝；
  - 协议错误预算与 finalization；
  - GraphRecursionError 转换为 `agent_protocol_loop` 并保留 trace／usage；
  - 结构化 Finish 增量流式输出；
  - 前端同时兼容新 JSON trace 与旧文本 trace。

### 运行证据

- 启动方式：真实产品知识库、DeepSeek 和独立 workspace trace root。
- 用户操作序列：提问 Anthropic《How we contain Claude》是否提到 MCP，并要求简要说明依据。
- 预期结果：至少一次真实工具调用、合法 finish JSON、协议错误为 0、trace 版本升级。
- 实际结果：`succeeded / finish`；3 次模型调用、2 次工具调用、0 次协议错误、trace complete；635 字回答；版本为 `react-structured-v3 / chat-v4`。
- 提示词版本：`chat-v4`，hash `d6127e318727183e7600ecd8dbcb35324a7cca8ef5dad08d5f0c49c58d1dce11`。
- N=1 全量快速试跑 `run_69bb6c200d6f4afcb826e06adaa7c623`：18/18 attempt 均为 product，17 succeeded、1 partial（cf-003 输出长度截断），0 基础设施失败、0 替代样本、0 协议错误；68 条模型输出中 0 条包含伪造 Observation 或 tool_result；18 份 trace 全部 complete，并统一标记 `react-structured-v3 / chat-v4`。
- 提示词已于 2026-10-24 经负责人审核确认。

### 失败探针

- 注入或复现的失败：
  - 模型输出两个连续 JSON；
  - 模型输出 Harness-only `tool_result`；
  - 模型省略 Finish 的可选决策摘要；
  - 模型在 JSON 字符串中输出裸换行；
  - 连续非法输出达到 finalization 上限。
- 系统保留的旧状态：原始模型输出、trace、usage、模型调用数和部分回答；旧协议 run 保持原样。
- 用户看到的错误：第二次协议错误后关闭工具并提示 finalization；仍非法时显示 `agent_protocol_loop`，不显示为基础设施失败。

### 人工验收

- [x] 负责人确认结构化模型输出、工具结果、决策摘要和版本显示正确。

## 反向解释

实质性切片必须准备 3～5 道行为问题，由负责人逐题独立回答。AI 每次提问必须说明当前题号和剩余题数。

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么每轮必须只返回一个 JSON 动作或 Finish？ | 自由文本 ReAct 会诱导模型续写和编造下游 transcript，结构化 JSON 能增强指令遵循并收窄合法输出空间。本轮 N=1 回归中 68 条模型输出未再出现伪造 Observation。 | 已通过 | 保持单 JSON 协议与严格解析。 |
| R2 | 为什么模型伪造 `tool_result` 必须直接报协议错误？ | 工具结果只能来自真实工具调用和 Harness 执行；模型输出的 `tool_result` 必然是捏造的执行事实，不能容忍或写入上下文。 | 已通过 | 继续由 Runtime 独占 `tool_result` 和 `protocol_error` 类型。 |
| R3 | 为什么连续协议错误应作为产品失败并保留 trace？ | 协议循环是模型能力不足导致的确定性问题，属于 Agent 必须解决的产品行为，因此应进入有效样本和判官。trace、token 与部分回答是定位根因所必需，部分回答本身也可能对用户仍有价值。 | 已通过 | `agent_protocol_loop` 保持产品失败分类，并完整保留现场。 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 协作复盘

- AI 明显加速的环节：协议模型、Runtime 状态机、trace 收口、前端兼容和故障测试可以围绕真实失败现场快速闭环。
- 负责人失去系统模型的环节：最初把私有 boundary 与结构化协议叠加，协议层次重复；经讨论收敛为单一 JSON 契约。
- 真正发现问题的控制点：真实 N=1 回归暴露了 Finish 可选摘要和裸换行行为，证明供应商 JSON mode 仍需 Runtime 校验与有限兼容。
- 只有仪式感、没有产生判断价值的流程：本轮未发现；真实复现、提示词审阅和 trace 界面检查都产生有效判断。
- 下一次准备调整的一到两项：观察新协议在 N=10 下的未知率；后续用独立实验评估 native tool calling，不直接替换。

### 本次试行的流程规则

| 规则／假设 | 来源 | 想防住的失败 | 使用成本 | 实际效果 | 下次决定 |
| --- | --- | --- | --- | --- | --- |
| Agent 动作应使用结构化协议而非自由文本 transcript | 2026-10-24 `run_d72af6d476c54710818dd517b5baaa69` 真实失败 | 模型续写伪造 Observation 并耗尽递归上限 | 需要升级 prompt、parser、版本与回归 | 旧 run 11/17 个 attempt 出现协议错误；新 N=1 run 68 条输出为 0 | 保留 |

## 已知风险与后续

- 未验证项：DeepSeek 在更大 N 和更广泛问题类型下的 JSON 输出长期稳定性。
- 接受的风险：`decision_summary` 是模型生成的简短说明，不能等同于真实内部推理。
- 后续待办或 ADR：在结构化 ReAct 稳定后，再到独立实验中比较 native tool calling 的工具选择准确率、延迟和成本。
