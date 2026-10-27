# 0015 · 有界恢复合法 finish 后的序列化尾随内容

- 状态：已采纳
- 决定日期：2026-10-27
- 影响范围：结构化 ReAct action parser、Agent 终态、trace 与评测结果

## 背景

DeepSeek Flash 在 c3-001 中已生成完整合法 `finish` JSON，但偶发追加多余 `}` 或输出两个完全相同的 finish 对象。严格解析整个字符串会把它记为协议失败，连续两次后触发 `agent_protocol_loop`，尽管答案正文有效。

## 决定

1. 工具阶段仍要求严格单 JSON，不恢复 `tool_call` 错误。
2. `finish` 完整且 schema 合法时，允许尾随内容仅为空白、多余 `}`，或一个以上与首个对象完全相同的 finish 对象。
3. 不同 finish、代码围栏、尾随自由文本、重复键、非法字段和截断 JSON 仍拒绝。
4. 恢复只消费首个严格校验后的 finish 对象，不执行任何后续 JSON 或工具动作。

## 候选与取舍

- 全局放宽 JSON：会削弱防伪造 Action/Observation 边界，不采用。
- finalization 纯文本：当前模型在完整上下文中稳定返回空内容或继续模仿 JSON，暂不采用。
- 有界 finish 尾随恢复：不改变工具阶段协议，可覆盖真实序列化伪影。

## 验证

- 真实 turn4/turn5 额外 `}` 可恢复。
- 两个完全相同的 finish 可恢复。
- 不同 finish、代码围栏和尾随正文仍失败。
- 修复后 c3-001 N=10 中 protocol loop 为 0。
