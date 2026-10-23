# 022B · 评测用例与用例集编写人工验收

- 状态：已完成
- 日期：2026-10-23
- 关联切片：[022B 评测用例、用例集编写与快速试跑](../slices/022B-evaluation-authoring.md)

## 验收项

- [x] 从 active case revision 创建草稿，反复修改问题、N/K、目标和 R/X 判定项不会产生新 revision。
- [x] 提交审核时可看到相对基准版本的完整 diff。
- [x] 审核通过后只生成一个 case revision；未修改判定项复用，修改项生成新 criterion revisions。
- [x] reviewed revision 可用于快速试跑；draft／retired revision 被拒绝。
- [x] 快速试跑可覆盖 N、不可覆盖 K；N<K 时只显示探索性结果。
- [x] suite builder 可从 active cases 中勾选任意子集，并显示用例数、预计样本数和精确 revision IDs。
- [x] 同一 case 的两个 revisions 不能进入同一 suite revision。
- [x] case revision 退休后，关联 suite 标记已过期，新正式 run 被拒绝；历史 run 和 trace 保持可查看。
- [x] 历史 suite 可克隆为 draft，更新成员后发布新 revision。
- [x] 所有新界面使用中文主标签，技术 ID 保持可复制原值。

## 人工证据

- 自动证据见 [022B 切片证据包](../slices/022B-evaluation-authoring.md#证据包)。
- 2026-10-23 负责人确认：草稿保存后可重新打开并保留修改，反复保存没有生成新的不可变修订版。
- 2026-10-23 负责人确认：第 3 版只修改了 R1，X1 与 R2 正确复用第 1 版 criterion revisions；第 2 版已停用，第 1 版仍生效。
- 2026-10-23 负责人确认：快速试跑可选择 reviewed／active 历史版本，draft 与 retired 版本不可选。
- 2026-10-23 负责人确认：快速试跑只覆盖 N，K 保持源 revision；N<K 展示探索性执行且不产生正式结论。
- 2026-10-23 负责人确认：可新建 `manual-smoke` 用例集，精确选择 c1、c2 两条 active 用例并发布生效。
- 2026-10-23 负责人确认：suite builder 每组只展示 active revision，同一稳定用例最多选择一条。
- 2026-10-23 负责人确认：c1 激活第 3 版后，引用第 1 版的 suite 变为 stale，正式预检被阻止；历史 run 不受影响。
- 2026-10-23 负责人确认：过期 `manual-smoke` 可预填当前 active 成员，经新 draft 发布新 revision 后恢复为生效中，旧 revision 保留。
- 2026-10-23 负责人确认：草稿编辑器可展示字段与判定项的完整前后 diff；四页签、历史 suite 来源与筛选、中文标签和最终正式预检界面均通过。
