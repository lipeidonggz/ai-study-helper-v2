# 039 · 结构化 finish 尾随序列恢复

- 状态：已完成
- 开始日期：2026-10-27
- 关联设计：[ADR 0011](../../decisions/0011-structured-react-response-protocol.md)、[ADR 0015](../../decisions/0015-bounded-finish-serialization-recovery.md)
- 前置切片：[026 结构化 ReAct 输出协议](./026-structured-react-protocol.md)

## 控制契约

### 用户可观察目标

当模型已经输出一个完整合法的 `finish` JSON，但随后只追加多余右括号或完全相同的重复 `finish` 对象时，Agent 保留该答案并正常完成，不再因为序列化尾随内容丢失答案。

### 核心不变量

1. 只有第一个完整 JSON 对象是合法 `finish` 时才允许恢复。
2. `tool_call`、不同内容的第二个 JSON、代码围栏、尾随正文和真正截断的 JSON 仍拒绝。
3. 允许的尾随内容仅限空白、多余 `}` 或与第一个对象完全相同的 finish 对象。
4. 工具能力、白名单、Observation 归属和工具阶段 JSON 严格性不变。

### 明确禁止

- 不恢复含不同答案的第二个 finish。
- 不把尾随自由文本当作答案。
- 不放宽 `tool_call` 的严格 JSON 解析。
- 不把协议错误静默算成 pass；合法 finish 直接完成，非法输入仍进入现有协议错误路径。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 完成态 JSON 的尾随序列化错误如何处理 | 只恢复首个合法 finish 后的多余右括号或完全相同的重复 finish | 真实失败中正文完整、无工具执行风险；防伪造边界针对 Action／Observation | 全局放宽 JSON 会削弱动作协议；纯文本 finalization 在当前模型上稳定返回空内容 | `parse_action`、trace 与 Agent 终态 | 真实 turn4/turn5 回放、错误尾随测试 | 已确认 |

## 证据包

### 自动化证据

- `test_finish_recovers_extra_closing_brace`
- `test_finish_recovers_exact_duplicate_object`
- 不同 finish、代码围栏、tool_call、尾随正文仍保持失败。
- 完整后端测试与 `quality:quick`：通过。

### 运行证据

- 修复前 run：`run_983155d040f64488881567b9ce27938d`
- 修复前 trace：`7bbaddb53d81ee99772478285b04f652`
- turn4/turn5：首个 finish JSON 均可解析，仅末尾多 `}`。
- 修复后 run：`run_37bf3e33d01f4b73bae7c4dbaa1b1af1`
- 修复后结果：10/10 succeeded，protocol loop 为 0。

### 被否决方案

曾实现“finalization 阶段直接输出纯 Markdown”。真实 DeepSeek Flash 在完整 finalization 上下文中连续返回空内容，且仍可能模仿 JSON；该方案撤销，不作为生产路径。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 首个合法 finish 后允许恢复哪些尾随内容？ | 只允许一个完全重复的 finish 结构或多余 `}`。 | 通过 | 后续按新失败形态再枚举 |
| R2 | 其它无法解析的尾随内容如何处理？ | 仍判为协议错误，不放宽工具阶段和自由文本边界。 | 通过 | 无 |
| R3 | 为什么恢复范围不能继续机械扩大？ | 当前规则只覆盖已观测的序列化伪影；新形态需要先采样和验证。 | 通过 | 无 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 已知风险与后续

- 恢复规则仍是针对终端序列化伪影的兼容层。若后续出现更多不同形态的尾随内容，应先采样验证，不继续机械放宽。
