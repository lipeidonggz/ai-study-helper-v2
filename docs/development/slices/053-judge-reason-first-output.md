# 053 · Judge reason-first 输出契约

- 状态：已完成
- 开始日期：2026-10-29
- 关联设计：[03 用例与金标准](../../design/03-cases-and-gold-standard.md)、[08 评测台设计](../../design/08-evaluation-console-design.md)
- 前置切片：[052 c13 补充隔离手段范围校准](./052-c13-additional-controls-scope.md)

## 控制契约

### 背景与失败场景

- 触发问题：v2 判官在 c13 的 `attempt_2e8fa87d...` 中先输出了 `satisfied`，但其 reason 明确认定缺少凭证隔离，形成 verdict/reason 自相矛盾。
- 不处理时的用户损失：结构化输出合法但语义自相矛盾，Harness 会把错误 verdict 正常计入 K。
- 现有流程为什么不足：v2 按 `verdict → reason` 生成，模型可能先下结论，再为结论编理由。
- 本切片负责什么：新增 reason-first 的 v3 判官 prompt，并支持 criteria 显式选择 v2/v3。
- 本切片不负责什么：不修改任何 R/X 规则，不重跑 Agent，不自动切换其他 active criteria 到 v3。
- 停止或推翻条件：若 v3 仍频繁出现 reason 与 verdict 矛盾，应停止依赖自由文本顺序，改为结构化逐项判定。

### 用户可观察目标

判官原始输出先说明各项要求的核对结果，再给 verdict；历史 v2 判定仍可按原 prompt 重放。

### 核心不变量

1. `judge-rubric-v2` 原 prompt 与 hash 保持不变。
2. v3 只改变输出顺序和一致性约束，不改变 criterion 的语义。
3. 每个 judgment 的 invocation 记录其 criterion 实际使用的 prompt version。
4. 新 c13 case revision 可使用 v3，其他 active criteria 仍可用 v2。
5. prompt version 不支持时必须 pending，不能静默使用另一版本。

### 明确禁止

- 不得原地修改 v2。
- 不得把 v3 默认套到尚未升级的 criteria。
- 不得因输出字段顺序正确就跳过语义核对。
- 不得为 c13 单独写 Harness 判定语义。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| criterion 引用未知 judge 版本 | 判定 pending | criterion 与错误码 | 是 |
| v3 输出字段顺序错误 | output_invalid 或 pending | 原始输出 | 是 |
| reason 与 verdict 语义冲突 | 人工发现并阻止采纳 | 原始输出与判定 | 是 |

### 决策预算

- 实际 B 级决策：1。
- 是否存在 C 级决策：否。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 如何减少先下 verdict 再补 reason | 新增 v3，要求 reason 前置并明确一致性 | c13 已出现 verdict/reason 矛盾 | 原地修改 v2 会破坏历史；仅增加提示词说明不一定改变生成顺序 | 所有可选用 v3 的语义判官 | 30/30 原始输出 reason-first；已知矛盾用例改判正确 | 已确认 |

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 |
| --- | --- | --- | --- | --- | --- |
| A1 | 历史兼容 | v2 原文和 hash 不变 | v2 criteria 仍可选择 | 必须 | 单元测试 |
| A2 | 版本隔离 | v3 仅由显式 criterion 引用 | invocation 记录 v3 | 必须 | 冻结重判 |
| A3 | 输出顺序 | 原始输出 reason 在前 | 30/30 | 必须 | 原始输出审计 |
| A4 | 矛盾修复 | 理由说缺项时 verdict 不满足 | `attempt_2e8...` 改判 fail | 必须 | 冻结重判 |
| A5 | 边界收敛 | 低影响的“额外提及排除项”不再成为整题红线 | 删除 X1 后最终 X2 10/10 not_violated | 必须 | 人工复核与冻结重判 |

## 证据包

- 详情：[2026-10-29 judge reason-first](../experiments/2026-10-29-judge-reason-first.md)
- 过渡版 v3 case revision：`cr_c13_001_84e75a91dbcb`
- 最终 case revision：`cr_c13_001_6c08a5db0d8f`
- 最终 judgment revision：`evalrev_d44ad528832a472c98c125f47eb90dd6`
- 原始输出顺序：30/30 reason-first。
- 最终 criterion：15 pass、5 fail、0 pending。
- 最终 case：5 pass、5 fail、0 pending；其中 `attempt_2e8...` 的矛盾已修复。
- 最终 X2：10/10 not_violated；X1 已由负责人确认删除。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么不直接修改 v2 提示词？ | 会破坏历史 revision 与 invocation prompt/hash 的可追溯性 | 通过 | 保留 v2，新增 v3 |
| R2 | 为什么 reason 前置可能减少矛盾？ | 让模型先完成条件核对，再基于理由选择 verdict | 部分通过 | 30/30 顺序正确，已知矛盾消失 |
| R3 | 为什么只让显式引用的 criteria 使用 v3？ | 保持历史 v2 可重放，并把输出契约升级与全部 criterion 迁移分开控制 | 通过 | v2/v3 并存，c13 显式使用 v3 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 已知风险与后续

- v3 已确认用于 c13，但尚未把全部 active criteria 迁移到 v3；后续迁移需另行做语义回归。
- reason-first 只降低 verdict 与理由脱节的风险，不替代 criterion 语义校准，也不保证判官判断本身正确。
