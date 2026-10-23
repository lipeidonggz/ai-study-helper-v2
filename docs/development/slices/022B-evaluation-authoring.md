# 022B · 评测用例、用例集编写与快速试跑

- 状态：已完成
- 开始日期：2026-10-23
- 关联设计：[03 用例与金标准](../../design/03-cases-and-gold-standard.md)、[08 评测台设计](../../design/08-evaluation-console-design.md)、[ADR 0010](../../decisions/0010-evaluation-case-and-suite-revision-workflow.md)
- 前置切片：[022A 评测执行内核](./022-evaluation-execution-core.md)
- 人工验收：[022B 评测编写人工验收](../manual-tests/022B-evaluation-authoring.md)

## 控制契约

### 用户可观察目标

1. 用户可创建或派生可反复编辑的用例草稿，修改问题、目标文档、N/K、执行策略和 R/X 判定项。
2. 提交审核时看到相对基准修订版的完整差异；审核通过后原子生成不可变用例修订包。
3. 用户可勾选 active 用例修订版创建或修改具名用例集，并看到成员、预计样本数和版本变化。
4. 用户可使用 reviewed／active 用例做不依赖 suite 的快速试跑，覆盖 N 与运行预算但不覆盖 K。
5. 用例修订版退休后，引用它的 suite 显示已过期并禁止启动新正式 run；历史 plan／run／trace 不受影响。

### 核心不变量

1. 草稿可编辑，reviewed／active case revision 不可修改；retired 只用于历史引用。
2. Case revision 的审核发布是单事务：变化的 criterion revisions、未变 revisions 的复用和 case revision 同时提交。
3. 同一 case 最多一个 active revision；同一 suite revision 中同一 case 最多一个 revision。
4. Suite revision 精确引用 case revision IDs，不自动跟随最新版。
5. 快速试跑必须冻结为 exploratory ExecutionPlan；N<K 时不产生正式 pass/fail，不能成为正式基线。

### 明确禁止

- 不允许执行 draft case revision；
- 不允许判定项脱离 case 修订包独立生效；
- 不允许新建 formal suite 引用非 active case revision；
- 不允许 active suite 在成员 case revision 退休后继续启动新正式 run；
- 不允许 suite 或历史 run 静默切换到更新用例；
- 不允许快速试跑覆盖 K 或进入正式基线；
- 不实现 judge、评分矩阵和基线比较。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 草稿校验失败 | 字段级错误与未完成项 | 可继续编辑的草稿 | 是 |
| 审核发布事务失败 | 发布失败，修订包未生效 | 原 active revision 与草稿 | 是 |
| 快速试跑包含 draft／retired | 明确指出不可使用的 revision | 原选择与错误项 | 是 |
| suite 含 retired case revision | `suite_stale` 与具体成员 | 历史 suite、run 和 trace | 克隆新 draft |
| suite 含同一 case 多个 revisions | 明确指出重复 case | 原 suite draft | 是 |
| 快速试跑 N<K | exploratory 完成标记，无正式结论 | attempt、trace 与 effective N/K | 可调整 N 后新 plan |

### 人工检查点

- 编码前确认草稿表、原子发布事务、suite stale 与 exploratory plan 的边界；
- 草稿与原子发布主路径可运行后检查一次 diff 和版本切换；
- suite builder 可运行后检查一次子集创建和 stale 重建；
- 快速试跑完成后检查正式 run 入口拒绝 reviewed／draft。

### 决策预算

- 确认 B 级决策：D6～D9 共 4 项；
- C 级决策：无；
- 本切片是原 022 超过决策预算后的显式拆分，不再继续扩张范围。

## 目标

在 022A 的执行内核和只读种子目录之上，建立可维护但仍可复现的用例、判定项和 suite 编写工作流，并提供低成本快速试跑。

## 输入与输出

输入：草稿编辑内容、基准 revision、判定项变更、suite 成员选择、snapshot、快速试跑 N 与预算。

输出：不可变 case／criterion／suite revisions、suite draft、stale 状态、exploratory／formal ExecutionPlan、完整 diff 和审核记录。

## 不包含

- checker、LLM judge、评分与聚合；
- 基线比较和正式回归门槛；
- 多人并行编辑、权限系统和审批角色；
- 任意模型实验配置器；
- 自动生成用例。

## 验收条件

- [x] 草稿反复编辑不产生 revision。
- [x] 审核通过原子生成 case revision 和必要的 criterion revisions，未变项复用。
- [x] draft／retired 不能用于快速试跑。
- [x] 快速试跑允许 reviewed／active、覆盖 N、不覆盖 K，并生成 exploratory plan。
- [x] suite builder 可勾选任意 active case revisions 子集，拒绝同一 case 多个 revisions。
- [x] case revision 退休后 suite 标记 stale，新正式 run 被拒绝，历史 run 不变。
- [x] 历史 suite 可克隆为 draft，显式更新成员后重新发布。
- [x] UI 使用“草稿、修订版、用例集、执行样本、运行轨迹”等中文主标签。
- [x] 完整质量门和真实开发版人工验收通过。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D6 | B | 草稿不可变边界与快速试跑来源 | 草稿可反复编辑；提交审核后生成不可变 revision；reviewed／active 可快速试跑，draft／retired 不可；快速试跑不要求 suite，由 ExecutionPlan 冻结现场；exploratory run 不能成为基线 | ADR 0010 | 每次修改即建 revision 会产生垃圾历史；草稿执行无法复现 | 新增 draft、reviewed 与 ad-hoc plan 边界 | 草稿编辑、原子发布、来源状态和 exploratory 禁入基线 | 已确认 |
| D7 | B | 快速试跑 N／K 覆盖 | 快速试跑可覆盖 N，K 保持源 revision；N<K 只产生 exploratory 结果；plan 记录 source／effective N/K | ADR 0010 | 覆盖 K 会改变正式标准；禁止 N 覆盖会让调试成本过高 | 快速试跑与正式评测统计口径分离 | N 覆盖、K 不可覆盖、N<K 无正式结论 | 已确认 |
| D8 | B | 用例修订包审核粒度 | 判定项内部独立 revision；整个 case draft 一次审核；发布事务创建变化项、复用未变项并生成精确绑定的不可变 case revision | ADR 0010、03 | 逐条独立生效会形成不一致组合 | 审核 UI 以用例为单位，底层保留规则追溯 | 原子发布、diff、复用与失败回滚 | 已确认 |
| D9 | B | suite 发布与失效规则 | 每个 suite 最多一个 active revision；suite 精确引用 case revisions，不自动跟随；成员 retired 后 suite stale；历史 suite 克隆后重新发布；正式 run 要求全部成员 active | ADR 0010、08 | 自动跟随会静默换题；允许 retired 成员会使用撤下定义 | 新增 suite draft、stale 计算和历史复制流程 | 子集选择、active 唯一、stale 拒绝与历史不变 | 已确认 |

## 证据包

### 自动化证据

- `corepack pnpm quality:quick`：通过。
- `pytest backend/tests`：204 条测试全部通过。
- `pyright --project backend`：0 errors。
- 产品与评测两种前端构建均通过；产品构建不包含评测 chunk 或评测入口标记。
- 覆盖：
  - 草稿反复保存不会新增 case revision；
  - reviewed 发布原子创建变化 criteria、复用未变 criteria；
  - active 唯一与旧 active 自动退休；
  - suite 任意 active 子集发布；
  - case revision 退休后 suite stale，正式 preflight 失败；
  - quick plan `mode=exploratory`、`suite_revision_id=null`、N 覆盖且 K 不变；
  - schema 0001→0002 真实升级并回填既有 plan facts。
  - Run 投影携带 suite 身份、修订版号与 exploratory mode。
  - 半迁移恢复、旧 suite 多 active 收敛和 CORS PUT 预检。

### 运行证据

- 使用真实产品 collection 快照和独立临时 eval data root：
  - `eval.db` schema：2；
  - 从 c1 创建并反复保存草稿，再发布为 reviewed revision；
  - 正式 suite 正确拒绝 reviewed case，只接受 active case；
  - 创建包含 c1、c2 两条 active revisions 的 suite revision，`stale=false`；
  - reviewed c1 revision 生成 quick plan：`planned`、`exploratory`、`N=1`、`source_n=10`、`K=9`；
  - 探针未调用 LLM，未产生模型费用。
  - 历史结果页可按 suite 身份筛选，并在批次详情显示 suite 名称与修订版号。
  - 草稿编辑器展示字段级、判定项级的前后 diff。

### 失败探针

- reviewed case revision 进入正式 suite：返回 409 `evaluation_state_conflict`。
- case revision 退休：引用它的 suite 进入 stale，正式 preflight 失败。
- quick plan 使用 draft／retired 或同一 case 多 revision：预检失败。
- 发布事务失败：草稿和原 active 版本保留，不产生半发布的 case revision。
- schema 0001 已有 plan-case 不可变 trigger：0002 迁移按“移除→回填→重建”顺序完成，不修改 plan 业务内容。

### 人工验收

- [ ] 按 [022B 人工验收清单](../manual-tests/022B-evaluation-authoring.md)执行。

## 反向解释

实现完成后由负责人逐题回答，当前尚未开始。

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么草稿不能直接执行或进入 suite？ | 草稿用于中断、继续和反复编辑，因此不是不可变现场，不能用于快速试跑或正式 suite；reviewed 可快速试跑，active 既可快速试跑也可进入正式 suite。 | 已确认；回答正确。补充：草稿不可执行不仅影响 trace，也会使 ExecutionPlan、content hash、结果关联和历史复现失去稳定内容。 | reviewed 仍不是正式生效版本；formal suite 只接受 active |
| R2 | case revision 退休后，为什么 suite 会失效而历史 run 不失效？ | suite 若自动跟随最新版，会破坏已冻结范围与 run／trace 的对应关系；每个 case 只有一个 active，旧 suite 继续启动新正式 run 会让人混淆版本。历史 run 引用原 ExecutionPlan、suite／case revision 和 trace，必须保持有效才能分析历史。 | 已确认；回答正确。补充：stale 只限制新正式 run 的准入，不改写历史 suite revision、run 或 trace。 | 历史 suite 通过克隆新 draft、显式更新成员后重新启用 |
| R3 | 快速试跑为什么可以改 N、不能改 K？ | 快速试跑用于降低调试成本，所以可以覆盖采样次数 N；K 是正式质量门槛，修改 K 等于改变评测标准，因此必须保持 case revision 原值。N<K 时样本不足以达到门槛，加上 Agent 行为存在随机性，只能得到不可信的探索性结果，不能生成正式通过／失败结论。 | 已确认；回答正确。补充：即使快速试跑 N>=K，exploratory plan 仍不能成为正式基线。 | N<K 显示探索性限制；所有 explorative run 禁止进入正式基线 |

## 协作复盘

- 待实现和人工验收后填写。

## 已知风险与后续

- 草稿首版采用单用户覆盖保存；尚无多窗口并发编辑的乐观锁 UI。
- criterion evaluator 可以保存为 checker／judge／human，但本切片仍不执行 checker 或 judge。
- 目标章节通过“标题 | 章节／子章节”编辑，复杂绑定仍可使用 JSON/API 扩展。
- 后续 judge、评分矩阵和正式基线在 022B 完成后另立切片。

<!-- reverse-explanation-gate: passed 3/3 -->
