# 059 · c15 用户监督能力语义校准

- 状态：已完成
- 开始日期：2026-10-29
- 关联设计：[03 用例与金标准](../../design/03-cases-and-gold-standard.md)
- 前置切片：[058 Judge v4 位置无关语义判定](./058-judge-v4-location-neutrality.md)
- 人工验收：沛东同意将 R3 的用户身份表述改为用户监督能力假设

## 目标

把 c15 R3 中 Cowork 的“面向无法判断 bash 风险的非技术用户”改为：

- 威胁来源／失效模式；
- 用户监督能力假设；
- 不能把 HITL 审批作为可靠边界；
- 环境层挂载与 VM 边界。

不修改题面、R1、R2、X1、R3 以外的规则或 Agent prompt。

## 验收

- 新 revision：`cr_c15_001_8e3e924a477c`；
- 所有 LLM criteria 仍使用 Judge v4；
- Judgment：`evalrev_7746eae747884557858ef36efbd6b49a`；
- criterion：56 pass、4 fail、0 pending；
- case：7 pass、3 fail、0 pending；
- X1/X3：全部通过；
- 真实失败：
  - `b114e9e0...` R3 缺少 Cowork 用户监督能力假设；
  - `40833a6c...` 同一 R3 缺口；
  - `7056ba70...` R2 缺“Claude Code 运行在用户机器上”，同时缺 R3 监督能力假设。

<!-- reverse-explanation-gate: passed 3/3 -->
