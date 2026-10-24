# 026 · 结构化 ReAct 人工验收

- 状态：已完成
- 日期：2026-10-24
- 关联切片：[026 结构化 ReAct 输出协议](../slices/026-structured-react-protocol.md)

## 验收项

- [x] 正常工具调用、直接 Finish 和多轮工具调用均可运行。
- [x] 模型输出自由文本 Action／Finish 时被拒绝。
- [x] 模型输出伪造 `tool_result` 时被拒绝，且不会进入上下文。
- [x] 连续协议错误后自动停止并显示 `agent_protocol_loop`。
- [x] 协议失败按产品失败进入判官，不显示为基础设施失败。
- [x] 异常现场仍可查看 trace、模型调用和 token。
- [x] 新产品 run 标记新的 Agent loop、prompt 和 trace 版本。

## 人工证据

- 2026-10-24 负责人确认运行轨迹中的决策摘要、结构化工具调用、Harness 工具结果和版本显示正确。
- N=1 全量 run `run_69bb6c200d6f4afcb826e06adaa7c623`：18/18 product，17 succeeded、1 partial，0 infrastructure，0 protocol error，0 伪造 Observation。
