# 057 · 全量 active Judge v3 迁移

- 状态：已完成
- 开始日期：2026-10-29
- 关联设计：[08 评测台设计](../../design/08-evaluation-console-design.md)
- 前置切片：[053 Judge reason-first 输出契约](./053-judge-reason-first-output.md)、[055 来源归属总判断与分层表达](./055-source-attribution-language.md)
- 人工验收：沛东要求剩余所有 active judge 迁移到 v3，并先用 c15 既有答案做冻结重判

## 控制契约

### 背景与失败场景

- 触发问题：Judge v3 已进入产品支持范围，但除 c13、c14 外，其余 active case criteria 仍显式绑定 v2；新库 seed 也仍硬编码 v2。
- 不处理时的用户损失：同一评测台会根据 case revision 的创建时间使用不同判官契约，新增 run 无法保证基于当前产品判官。
- 现有流程为什么不足：全局 prompt 版本展示为 v3，但实际判定由 criterion 的 `evaluator_ref` 决定，存在“看起来已升级、实际未迁移”的错位。
- 本切片负责什么：把全部 active LLM judge criteria 发布为 v3，修正新库 seed，重绑 active suites，并冻结重判 c15。
- 本切片不负责什么：不修改 Agent prompt、题面、R／X 规则或历史 judgment；不重跑 c15 Agent。
- 停止或推翻条件：出现无法解释的 v2/v3 verdict 漂移或 criterion 绑定遗漏时，停止激活并先做逐项审计。

### 用户可观察目标

后续任意 active case 的新评测运行都使用 `judge-rubric-v3`；历史 v2 revision 和 judgment 仍可读取。

### 核心不变量

1. 所有 active LLM judge criteria 的 `evaluator_ref` 必须为 `judge-rubric-v3`。
2. checker 仍使用确定性 checker，不改成 Judge。
3. 每个被迁移 case 发布新 revision，旧 revision 和 judgment 不覆盖。
4. active suites 必须绑定迁移后的 active case revisions。
5. 新库 seed 默认创建 v3 criteria。
6. c15 重判使用原 run 的 10 个答案，不重跑 Agent。

### 错误代价与非对称门禁

| 错误类型 | 具体例子 | 用户影响 | 处理分类 | 指标或阈值 |
| --- | --- | --- | --- | --- |
| active criterion 仍绑 v2 | 迁移后执行仍调用旧 judge | 新 run 结论口径不一致 | 阻断 | 审计必须为 0 |
| suite 仍绑 retired revision | 评测台继续启动旧 case | 迁移表面成功但实际无效 | 阻断 | stale case 必须为 0 |
| 历史被覆盖 | 旧 revision/judgment 消失 | 不可复现 | 阻断 | 只新增 revision |
| c15 用新生成答案比较 | 无法归因 Judge 变化 | 结论不可比较 | 阻断 | 只冻结重判旧 run |

### 明确禁止

- 不得原地修改历史 criterion revision。
- 不得删除 v2 支持代码或历史 judgment。
- 不得在同一步修改 c15 题面、R／X 规则或 Agent prompt。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 迁移中断 | 已完成 case 为 v3，其余为 v2 | 每个已发布 revision | 是，按审计续跑 |
| suite 同步失败 | active suite 引用 retired revision | 旧 suite 与迁移 revision | 是，重新发布 suite |
| c15 重判失败 | pending／reason 可见 | 原 run 与 v2 judgment | 是 |

### 决策预算

- 预计 B 级决策数量：1。
- 是否存在 C 级决策：否。

## 目标

完成 active criteria 与 suites 的 v3 迁移，并比较 c15 在 v2/v3 下的判定。

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 |
| --- | --- | --- | --- | --- | --- |
| A1 | active 全量 v3 | 所有 LLM criteria 绑定 v3 | 非 v3 数量为 0 | 必须 | repository 审计 |
| A2 | suite 同步 | active suites 不引用 retired case | stale 数量为 0 | 必须 | repository 审计 |
| A3 | 新库默认 v3 | 临时库 seed 后的 active criteria 均为 v3 | 单元测试 | 必须 | 自动化 |
| A4 | c15 可比 | 使用原 run 的 10 个答案重判 | attempt 集合不变 | 必须 | 冻结重判 |
| A5 | 历史保留 | 原 v2 revision/judgment 可读 | 不覆盖 | 必须 | repository 查询 |

## 验收执行

- [x] 新库 seed 单元测试通过。
- [x] 所有 active case 完成 v3 迁移。
- [x] 所有 active suites 完成重绑。
- [x] c15 v3 冻结重判完成。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 如何保证未来运行使用 v3 | 迁移全部 active criteria，并修改新库 seed | 实际判定由 criterion 显式绑定 | 仅改全局默认不会影响已绑定 v2 的 criteria | 所有 active case 与 suite | revision 与 suite 审计 | 已确认 |

## 证据包

### 自动化证据

- 新库 seed 的 v3 断言通过；完整 active case/suite 审计为 0 个非 v3、0 个 stale suite。

### 运行证据

- 迁移前 active v2 case：16。
- 迁移后：18 个 active case、全部 LLM criteria 均为 v3；5 个 active suites 均不 stale。
- c15 v3 第一次：5/10，criterion 54 pass、6 fail。
- c15 v3 第二次：5/10，criterion 55 pass、5 fail。
- 两次失败 attempt 组成不完全相同，详见迁移实验。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么不只改全局默认版本？ | 实际判定由 criterion 的 evaluator_ref 决定 | 通过 | 迁移全部 active criteria |
| R2 | 哪些历史必须保留？ | 旧 v2 revision、judgment 与 trace | 通过 | 只新增 revision |
| R3 | 如何避免把 Agent 变化混入比较？ | 只重判 c15 原 run，不重跑 Agent | 通过 | 冻结重判 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 已知风险与后续

- 未验证项：全量历史 run 的 v3 重判不在本切片内。
- 接受的风险：v2/v3 在 c15 上产生 8/10 → 5/10 的显著差异，并出现 R2/R4 样本级抖动。
- 后续待办或 ADR：依据 c15 及其他历史 run 决定是否有 criterion 需要单独校准。
