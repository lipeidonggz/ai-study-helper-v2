# 模型太会演完整 ReAct：为什么只阻止伪造 Observation 还不够

> **摘要**：[上一篇](https://juejin.cn/post/7699740355599745074)阻断了伪造 Observation 的上下文污染，模型却继续批量演完整 ReAct。本文用真实评测前后对照，说明从自由文本升级为单 JSON 协议的必要性。

> **30 秒判断**：如果模型还能在一段自由文本里同时写 Thought、Action 和 Observation，Harness 就算能识别错误，也仍然在诱导模型继续跑完整 transcript。真正有效的修复不是再加一句“不要伪造”，而是改变每轮输出的合法语法。
>
> **适合谁读**：已经实现文本 ReAct、正在接工具、或遇到模型伪造工具结果的 Agent 开发者；正在建设 trace、预算和 Agent 回归体系的人。
>
> **系列位置**：本文属于[《学习问答助手Agent实践》](https://juejin.cn/column/7697101748126007305)的实现复盘。[上一篇《模型会自己伪造工具结果：一次 Agent 评测暴露的 Harness 工程问题》](https://juejin.cn/post/7699740355599745074)解决了“伪造结果不能污染下一轮上下文”；本文继续回答：为什么这还不够。
>
> **代码库**：[ai-study-helper-v2](https://github.com/lipeidonggz/ai-study-helper-v2)
>
> **模型环境**：DeepSeek Flash 4.1，运行时 API 模型标识为 `deepseek-flash`，`temperature=0`，thinking disabled。新旧两次运行使用相同模型版本和采样参数；供应商未在 trace 中暴露不可变的后端权重快照。
>
> **结构速览**：§0 旧修复后的新故障；§1 为什么隔离没有止血；§2 一次 42 连击；§3 三种方案取舍；§4 新协议；§5 真实前后对照；§6 结构化不是银弹；§7 工程清单；§8 一页速查。
>
> **证据标记**：✅ 项目实测＝真实运行或真实 trace；🧪 可回归＝已有确定性自动化测试；🤔 当前判断＝由当前证据推导，不声称是普适规律。

---

## 0. [上一篇](https://juejin.cn/post/7699740355599745074)之后，问题没有“消失”，只是换了表现

[上一篇](https://juejin.cn/post/7699740355599745074)记录了一个很隐蔽的故障：

- 模型在回答“MCP 在文档里出现几次”时；
- 没有真实执行 `scan_term`；
- 却自己写下了 `Observation: {"count":0,...}`；
- 下一轮模型把这条伪造结果当成了真实检索事实。

当时的修复切开了三个边界：

1. 模型只能提出 Action；
2. 只有 Harness 能执行工具并生成 Observation；
3. 协议错误原文只进入 trace，不进入下一轮 History。

这个修复本身是正确的。它解决了“伪造结果污染后续推理”，也加了版本号 `react-langgraph-v2`。

但后续评测又暴露了新的问题：

> 被识别为协议错误的伪造结果已经绝不可能带进下一轮，但模型仍然反复尝试生成完整的 ReAct transcript。

这里的“阻断”不是降低概率，而是确定性切断：一旦模型输出被识别为
`protocol_error`，这次原始响应绝不会进入下一轮 History，只会保留在 trace。
因此，这条伪造结果造成上下文污染的概率是 0，而不是“更不容易”。

但模型仍可能在新一轮重新伪造。那是新的非法输出，同样会被拒绝。
所以，“伪造尝试没有停止”和“上次伪造结果绝不可能污染下一轮”并不矛盾。

最终结果不是少量错误：

- 17 个已产生 trace 的 attempt 中，11 个出现 `protocol_error`；
- 另有 1 个 partial attempt 的原始输出也包含伪造 Observation，只是被输出长度截断先行终止；
- 大多数样本在 1～2 次纠正后恢复；
- `c12-001` 连续伪造 42 次，最后触发 `GraphRecursionError`。

旧结论“提示词不能代替 Harness”没有错，但还不够完整：

> Harness 拒绝非法输出，只能保护系统事实；它不会自动降低模型继续生成非法 transcript 的概率。

---

## 1. 为什么“隔离错误输出”没有真正止血

假设每轮允许的输入形态长这样：

```text
Thought: 我需要检索 MCP。
Action: scan_term[{"term":"MCP"}]
```

模型很容易继续预测：

```text
Observation: {"count":0,"hits":[]}
Thought: 检索结果为 0。
Action: Finish[文中没有提到 MCP]
```

这段文本在人类眼里只是一段回答，在模型眼里却非常像训练中见过无数次的标准 ReAct 轨迹。

Harness 可以识别整个响应非法，可以把原始输出只放进 trace，然后追加一条受控错误：

```text
上一轮协议错误，请重新输出一个合法动作。
```

但下一次请求仍然具有相同的结构特征：

```text
Thought → Action → （请继续）
```

模型天然最可能补出的下一段，仍是 `Observation`。

所以旧修复保护的是：

```text
伪造事实不能继续成为推理证据。
```

没有改变的是：

```text
模型仍被邀请生成“动作之后的完整剧本”。
```

这两件事必须分开看。

---

## 2. 一次 42 连击，和一条“看似不存在”的 trace

`c12-001` 的失败尤其典型。

第一个 attempt 用了约 230 秒，模型确实调用了工具，但随后连续多次输出类似的完整轨迹：

```text
Thought: 已定位文档。
Action: search_blocks[{...}]
Observation: {...模型自己编的结果...}
Thought: 现在可以回答。
Action: Finish[...]
```

Harness 每次都能识别出伪造 Observation，也每次都拒绝它。但模型下一次仍然换一种说法，再演一遍。

真实 trace 中共有：

- 43 次 `llm.call`；
- 42 个 `protocol_error`；
- 1 次真实工具调用；
- 最终触发 `GraphRecursionError`。

更严重的是，完整 trace 其实已经写到了磁盘，但异常逃出 AgentRuntime 后，runner 把数据库中的 `trace_id`、模型调用数和 token 全部记成 0/missing。

这意味着旧实现同时存在两个问题：

1. **协议只拦截结果，不阻止重复尝试；**
2. **异常收尾没有保住已经产生的事实。**

替代 attempt 还能继续启动，于是同一个确定性循环又出现了两次。

> ✅ 项目实测：旧 run `run_d72af6d476c54710818dd517b5baaa69` 中，c12-001 连续触发 `GraphRecursionError`，并产生了实际存在但未正确关联的 trace。

---

## 3. 我们比较了三种方案

### 3.1 把所有判官和 Agent 提示词都写得更强硬

能做的包括：

- 再加禁止条款；
- 重复强调 Observation 只能由 Harness 生成；
- 增加反例。

但旧提示词已经明确写了“不得自行输出 Observation”，模型仍在 17 个 attempt 中让 11 个出现协议错误。

问题不是规则没写，而是输出仍采用模型最熟悉的自由文本 ReAct 形态。提示词只能改变概率，不能改变合法输出空间。

### 3.2 用更少见的停止词

另一个直觉方案是把 `Observation:` 换成随机私有边界，再通过 API `stop` 截断。

它能降低正常技术文档误命中的概率，但仍有局限：

- 模型仍可能输出旧的 `Observation:`；
- 其他越界文本仍可能出现；
- DeepSeek 是否稳定支持该 stop 需要额外验证；
- 模型输出仍是自由文本，只是换了一个分隔词。

### 3.3 单 JSON 结构化 ReAct

最终选择保留 ReAct 的决策循环，但改变每轮响应的语法：

```json
{
  "type": "tool_call",
  "decision_summary": "需要检索文档中的 MCP 相关内容。",
  "tool": "search_blocks",
  "arguments": {"query": "MCP"}
}
```

或：

```json
{
  "type": "finish",
  "answer": "最终回答"
}
```

工具结果不再写成模型可续写的 `Observation:`，而是 Runtime 独占的 `tool_result`：

```json
{
  "type": "tool_result",
  "harness_status": "ok",
  "tool": "search_blocks",
  "payload": {},
  "message": null
}
```

核心变化不是“JSON 更漂亮”，而是：

> 模型输出和 Harness 执行结果从同一种文本语法，变成了不同角色拥有的不同消息类型。

完整决策取舍见 [ADR 0011](https://github.com/lipeidonggz/ai-study-helper-v2/blob/59cd3cf384f0c3fafc368986344b5e0c77a2fbb2/docs/decisions/0011-structured-react-response-protocol.md)。

---

## 4. 新协议真正约束了什么

### 4.1 每轮只能有一个合法对象

响应必须是一个完整 JSON 对象，且 `type` 只能是：

- `tool_call`；
- `finish`。

模型输出 `tool_result`、`protocol_error`、第二个 JSON、Markdown 围栏或 JSON 后正文，都会被判为协议错误。

### 4.2 决策说明被保留下来

工具调用仍要求简短的 `decision_summary`：

```json
{
  "type": "tool_call",
  "decision_summary": "需要定位目标文档有哪些章节。",
  "tool": "get_outline",
  "arguments": {"doc_ids": ["doc_x"]}
}
```

它不是为了展示完整思维链，而是保留最小可审计信息：模型为什么在这一轮选了这个工具。

### 4.3 Finish 不再有自由文本语法

```json
{
  "type": "finish",
  "answer": "最终 Markdown 回答"
}
```

Harness 只需要解析 `answer` 字段，不再猜测 `Finish[...]` 的闭合位置，也不会把字段外壳推送给用户。

### 4.4 协议错误有硬预算

新 Runtime 不无限重试：

```text
第 1 次协议错误 → 返回受控 protocol_error，再试一次
第 2 次协议错误 → 关闭工具，进入 finalization
finalization 仍非法 → agent_protocol_loop，产品失败
```

这条规则让错误从“可能跑几十轮的随机行为”变成“有上限的状态机”。

### 4.5 GraphRecursionError 不再吞掉现场

评测中真正需要知道的不是“程序最后失败了”，而是：

- 模型调用了几次；
- 消耗了多少 token；
- 最后状态是什么；
- 哪个输出触发了异常。

所以图执行改为可保留最后状态的 streaming 收集。即使仍触发递归保护，也会生成稳定的 `agent_protocol_loop` 终态，并保留 trace、usage 和部分回答。

---

## 5. 关键实现只有四个小边界

源代码入口见固定提交
[`59cd3cf`](https://github.com/lipeidonggz/ai-study-helper-v2/tree/59cd3cf384f0c3fafc368986344b5e0c77a2fbb2)。

### 5.1 严格解析唯一对象

```python
payload = json.loads(
    _escape_control_characters_in_strings(response),
    object_pairs_hook=_object_without_duplicate_keys,
    parse_constant=_reject_non_finite_number,
)
if not isinstance(payload, dict):
    raise ActionProtocolError("response must be a JSON object")
```

来源：[backend/app/agent/actions.py](https://github.com/lipeidonggz/ai-study-helper-v2/blob/59cd3cf384f0c3fafc368986344b5e0c77a2fbb2/backend/app/agent/actions.py#L86)。

这里没有“找到第一个 JSON 就继续”的宽容逻辑。重复键、`NaN`、第二个对象和尾随正文都会失败。

### 5.2 协议错误两次后强制收口

```python
protocol_errors = state.get("protocol_errors", 0) + 1
enter_finalization = protocol_errors >= self._config.max_protocol_errors
```

来源：[backend/app/agent/runtime.py](https://github.com/lipeidonggz/ai-study-helper-v2/blob/59cd3cf384f0c3fafc368986344b5e0c77a2fbb2/backend/app/agent/runtime.py#L1150)。

错误不再只写进 trace 后继续原状态循环，而是推进 Runtime 状态。

### 5.3 图异常保留最后状态

运行中对每一步保留最后状态；捕获 `GraphRecursionError` 后转换为 `agent_protocol_loop`，再由正常的 trace 收尾路径写入真实调用数和 token。

相关失败测试：
[test_agent_runtime.py](https://github.com/lipeidonggz/ai-study-helper-v2/blob/59cd3cf384f0c3fafc368986344b5e0c77a2fbb2/backend/tests/test_agent_runtime.py#L495)。

### 5.4 DeepSeek 使用 JSON response format

```python
response_format={"type": "json_object"}
```

来源：[backend/app/providers/chat.py](https://github.com/lipeidonggz/ai-study-helper-v2/blob/59cd3cf384f0c3fafc368986344b5e0c77a2fbb2/backend/app/providers/chat.py#L257)。

这只是第一层提示，最终仍由 Runtime 做 schema 校验。

---

## 6. 前后对照：不是“变好了”，而是失败模式真的消失

| 指标 | 旧 run `d72af…` | 新 run `69bb6…` |
| --- | ---: | ---: |
| 模型 | DeepSeek Flash 4.1（`deepseek-flash`） | DeepSeek Flash 4.1（`deepseek-flash`） |
| 采样参数 | temperature 0，thinking disabled | temperature 0，thinking disabled |
| 目标 attempt | 18 | 18 |
| 实际 attempt | 20 | 18 |
| 基础设施失败 | 3 | 0 |
| 替代 attempt | 2 | 0 |
| 有协议错误的 attempt | 11/17 个已关联 trace | 0/18 |
| 含伪造 Observation 的输出 | 12/17 | 0/18 |
| trace complete | 存在丢失关联 | 18/18 |
| c10-001 | partial | succeeded |
| c12-001 | 连续递归失败 | succeeded |
| c13-001 | partial | succeeded |

新 run 里：

- 17 个 attempt 成功；
- 1 个 attempt partial；
- 68 条模型输出中，没有 `Observation:`；
- 没有模型伪造 `tool_result`；
- 全部 trace 标记 `react-structured-v3 / chat-v4`；
- 非法协议错误为 0。

唯一的 partial 是 cf-003：答案达到输出长度上限并正确声明“内容未完”。这不是基础设施失败，也不应该因为它是 partial 就被排除出评测。

> ✅ 项目实测：这是同一条 Agent 执行链、同一套 18 条用例上的一次前后对照。它证明本次修复在该样本上有效，不等同于已经证明所有模型、所有 prompt 下永不发生。

两次运行均使用 DeepSeek Flash 4.1、`temperature=0` 和 thinking disabled，主要变化是 `react-langgraph-v2 / chat-v3` 到 `react-structured-v3 / chat-v4` 的交互协议。这个对照能说明本次协议改造对当前模型版本的影响，但不能外推成“所有 DeepSeek 版本都不会伪造工具结果”。

---

## 7. 结构化不是银弹：真实模型仍然会制造格式噪音

第一次真实结构化探针并没有一次通过。

DeepSeek 已经接受了 `json_object`，但仍出现两类问题：

1. 多次 `finish` 省略了 `decision_summary`；
2. Markdown 中的换行有时没有按 JSON 字符串要求转义。

处理没有采用“看到像 JSON 就放行”：

- `tool_call.decision_summary` 仍然必填；
- `finish.decision_summary` 改为可选；
- 只修复字符串内部的裸控制字符；
- 额外字段、多个对象、伪造类型仍然拒绝。

在新 N=1 run 的 68 条模型输出中，仍有 6 条需要控制字符修复。它们全部通过结构校验，但这也说明：

> `response_format=json_object` 不是 schema 保证，更不是事实保证。

结构化协议解决的是“合法消息空间”，Runtime 校验解决的是“字段与边界”，真实评测解决的才是“模型能不能稳定完成产品任务”。

---

## 8. 这次修复最值得复用的检查清单

设计 Agent 协议时，可以依次问：

1. 模型输出里是否混有多个生命周期阶段？
2. 工具结果能否由模型自行拼写？
3. “拒绝一次”之后，下一轮是否又从同一形态继续生成？
4. 模型异常运行时，是否还能保留 trace、usage 和部分回答？
5. 该异常是供应商故障，还是模型行为本身需要被评测？
6. 更换协议后，loop、prompt、trace 版本是否一起升级？
7. 旧 trace 是否还能被新 viewer 正确读取？

如果答案是：

```text
模型继续写完整 transcript
Harness 只能反复识别、丢弃
重试没有硬上限
异常后 trace 丢失
```

那问题通常不在“提示词还不够强”，而在协议本身仍把执行边界暴露成了可续写文本。

---

## 9. 一页速查

**旧修复解决了什么**：一旦输出被识别为伪造 Observation，这次原始响应就绝不会作为证据进入下一轮 History，污染概率是 0。

**旧修复没解决什么**：模型仍会反复生成完整 ReAct transcript，导致多次协议错误，极端情况下撞到图递归上限。

**根因**：自由文本协议本身提供了“Action 后面还能继续写 Observation”的预测空间。

**最终修复**：

- 每轮只允许一个 `tool_call` 或 `finish` JSON；
- `tool_result` 只能由 Runtime 生成；
- 连续 2 次协议错误后强制 finalization；
- 仍失败则记为 `agent_protocol_loop` 产品失败；
- 异常也保留 trace、token、调用数和部分回答；
- `react-structured-v3 / chat-v4` 与旧 run 明确分版。

**真实结果**：

- 旧 run 中，11/17 个已关联 trace 的 attempt 出现协议错误；
- 新 N=1 run 中，18/18 attempt 为产品结果，68 条模型输出中 0 条伪造 Observation；
- c10、c12、c13 均恢复成功；
- cf-003 剩余 partial 属于输出长度限制，不是协议失败。

**最重要的一句话**：

> 只把模型的错误输出隔离起来，只能保护事实；只有重新设计输出协议，才能减少模型不断制造错误 transcript 的机会。
