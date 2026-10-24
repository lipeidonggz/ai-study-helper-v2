# 0011 · 使用单 JSON 结构化 ReAct 响应协议

- 状态：已确认
- 日期：2026-10-24

## 背景

真实评测 run `run_d72af6d476c54710818dd517b5baaa69` 中，17 个已产生 trace 的 attempt 有 11 个出现 `protocol_error`，另有 1 个模型输出包含伪造 Observation。大多数样本经过 1～2 次纠正后恢复，但 `c12-001` 连续伪造 42 次，最终触发 `GraphRecursionError`；其 trace 已完整写入，却因异常逃逸而被数据库记成 missing，usage 也被清零。

自由文本 ReAct 使模型可以从合法 Action 继续补写工具结果。仅靠提示词无法可靠阻止，继续把 `Observation:` 作为 stop 词又会与常见技术文本冲突。

## 决定

Agent 保留 ReAct 的 Thought→Action→Result 核心，但每轮模型响应改为严格单 JSON：

```json
{
  "type": "tool_call",
  "decision_summary": "为什么需要这一步",
  "tool": "search_blocks",
  "arguments": {}
}
```

或：

```json
{
  "type": "finish",
  "answer": "最终回答"
}
```

`tool_result` 和 `protocol_error` 只能由 Runtime 生成。连续 2 次协议错误后关闭工具进入 finalization；仍非法时以 `agent_protocol_loop` 产品失败结束。

## 候选与取舍

- 保持自由文本 ReAct，只增加随机私有分隔符：改动小，但仍依赖模型遵循私有文本语法，不能解决其他 Action 污染。
- 继续用 `Observation:` 作为 stop 词：会误伤正常技术内容。
- 供应商 native tool calling：动作传输最强，但当前会削弱显式决策摘要可见性，并引入 tool-call streaming、多工具调用和 provider 兼容差异。
- 单 JSON 结构化 ReAct：保留显式决策摘要，消除自由文本 transcript，可由 Runtime 严格验证，作为当前阶段折中。

## 后果

- `chat-v3` 升级为 `chat-v4`，`react-langgraph-v2` 升级为 `react-structured-v3`；
- 新旧 Agent run 不能直接混比，需要重新建立基线；
- Trace viewer 同时兼容旧文本协议和新 JSON 协议；
- JSON mode 只能保证可解析对象，字段 schema 仍由 Runtime 校验；
- `finish.decision_summary` 可选，`tool_call.decision_summary` 必填；
- 模型或供应商仍可能输出非法 JSON，因此协议错误预算、finalization 和 trace 保留不可省略。

## 验证

- 255 项后端测试通过；
- `quality:quick` 通过；
- 真实 DeepSeek 探针完成 3 次模型调用、2 次工具调用、0 次协议错误，trace complete；
- 人为注入伪造 `tool_result`、多 JSON、重复键、尾随内容和协议错误循环均有回归测试；
- `GraphRecursionError` 转换为 `agent_protocol_loop` 后仍保留 trace、usage 和调用数。

## 推翻条件

- 单 JSON 协议在真实评测中持续产生显著高于文本协议的未知失败；
- 显式决策摘要不再有产品价值，且 provider 的 native tool calling 已被证明更稳定；
- JSON streaming 无法稳定满足首字延迟或流式展示要求。
