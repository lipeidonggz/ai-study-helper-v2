# 0010 · 评测用例与用例集采用草稿、不可变修订版和显式启用

- 状态：已接受
- 决定日期：2026-10-23
- 影响范围：评测用例编辑、判定项版本、用例集管理、快速试跑、正式评测入口与历史复现

## 背景

设计 08 已要求用例、判定项和 suite 使用不可变 revision，但没有充分定义草稿编辑、审核粒度、快速试跑和用例升级后的 suite 生命周期。按“每次内容变化立即产生 revision”实现，会产生大量无意义历史，并让单用例调试成本过高。按“suite 动态引用当前最新用例”实现，又会让已批准的评测范围静默变化。

本决定只改变评测域的版本工作流，不改变执行现场的不可变原则。

## 决定

### 用例

1. 稳定 `case_id` 表示长期评测意图。
2. 草稿是可反复编辑的工作副本，不参与执行。
3. 提交审核后生成不可变 case revision，至少包含问题、目标文档／章节、干扰元数据、默认 N/K、执行策略、精确 criterion revision IDs、source、change note 和 content hash。
4. R/X 判定项内部保留独立 revision；用户以整个 case revision 为审核单位。审核通过时原子创建变化的 criterion revisions、复用未变化的 criterion revisions，并生成 case revision。
5. 审核通过后不可修改内容；需修改时从现有修订版派生新草稿。
6. 同一 case 最多一个 active revision。激活新 revision 时，旧 active 转为 retired。

### 快速试跑

1. 快速试跑可选择 reviewed 或 active case revisions，不能选择 draft 或 retired。
2. 快速试跑不要求先创建 suite；ExecutionPlan 冻结所选 case revisions、snapshot、有效 N/K 和预算。
3. 快速试跑可以覆盖 N，K 保持源 case revision 的值。
4. 快速试跑标记为 `exploratory`；N<K 时不生成正式 pass/fail，不能成为正式基线。

### 用例集

1. suite 是具名、可复用的评测范围；suite revision 精确引用一组 case revision IDs。
2. 同一个 case 在一个 suite revision 中最多一个 revision。
3. suite 不自动跟随 case 新版本。用例升级导致旧 revision retired 后，引用它的 suite 显示“已过期”，禁止启动新正式 run。
4. 每个 suite 身份最多一个 active revision；不同 suite 身份可以同时 active。
5. 新 suite revision 从 draft 发布并激活时，同一 suite 的旧 active 退休。
6. 历史 suite revision、ExecutionPlan、run 和 trace 保持原引用；历史 suite 只读，重新运行需克隆成 draft 后重新发布。
7. 正式 run 必须使用 active suite，且所有成员 case revisions 仍为 active、snapshot 与版本预检通过。

## 候选方案

| 方案 | 结论 | 原因 |
| --- | --- | --- |
| 草稿每次修改立即生成不可变 revision | 不采用 | 产生大量垃圾历史，审核和比较不可读 |
| 草稿可执行 | 不采用 | 执行后内容还会变化，结果无法复现 |
| 判定项逐条独立审核生效 | 不采用 | 会形成问题、目标和判定规则版本不一致的组合 |
| 修订包整体审核、内部判定项细粒度版本化 | 采用 | 操作简单，同时保留规则级追溯 |
| 快速试跑强制先建 suite | 不采用 | 单用例调试和界面验证成本过高 |
| 快速试跑由 ExecutionPlan 冻结 ad-hoc 选择 | 采用 | 不增加临时 suite，同时保持可复现 |
| suite 自动引用最新 case | 不采用 | 已批准评测范围会静默变化 |
| suite 精确引用 case revisions，升级后显式重建 | 采用 | 历史稳定，升级可解释 |

## 后果

- 评测 UI 需要区分 case draft、case revision、suite draft、suite revision。
- 审核必须提供完整 diff，并在单个事务内发布判定项与用例修订包。
- suite 需要计算 stale 状态，并在用例升级后提示重新组套。
- 快速试跑与正式 run 必须能从 ExecutionPlan 的 mode、source status 和 N/K 字段明确区分。
- 所有历史 ExecutionPlan、run 和 trace 继续引用原 revision ID，不随当前 active 状态变化。

## 验证

- 草稿反复编辑不会产生 revision。
- 提交审核原子生成一个 case revision 与必要 criterion revisions。
- 未修改判定项复用原 revision。
- reviewed case 可快速试跑，draft／retired 被拒绝。
- N 覆盖和 K 不可覆盖进入 plan；N<K 标记 exploratory 且无正式结论。
- suite 创建拒绝同一 case 的多个 revisions。
- case revision 退休后 suite 显示 stale，新正式 run 预检失败；历史 run 不变。
- 同一 suite 多个 revert 时最多一个 active revision。

## 推翻条件

- 将来需要多人并行编辑同一用例，并要求每个字段独立审批；
- 评测成为多人服务，需要更细的权限和发布流；
- ExecutionPlan 不再作为临时选择的复现事实源；
- suite 需要自动跟随用例版本，且能证明不会破坏历史可比性。
