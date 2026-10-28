# 046 · c6 通用 containment 金标准修订

- 状态：已完成
- 开始日期：2026-10-28
- 关联设计：[03 用例与金标准](../../design/03-cases-and-gold-standard.md)
- 前置切片：[045 引用语义支持改为软质量门禁](./045-citation-support-soft-quality-gate.md)
- 关联 ADR：[0010 评测用例与用例集修订流程](../../decisions/0010-evaluation-case-and-suite-revision-workflow.md)

## 控制契约

### 背景与失败场景

- 触发问题：c6-001 题面询问沙箱、出口控制、虚拟机在 containment 中的通用作用和配合方式，但 R1 要求 Claude Code 沙箱的读写网络配置、Claude Cowork VM 的内部资源清单以及 egress 的实现细节。
- 不处理时的用户损失：回答已经正确说明三种通用机制的角色时，仍会因为未复述产品配置被判失败；团队会错误地去修改 Agent 检索和提示词。
- 现有流程为什么不足：R1 把产品级实现示例当成通用机制定义，并与同 case 的 X1“区分产品模式和底层控制手段”冲突。
- 本切片负责什么：修订 c6 的 R1/R2，让判定对象重新与题面一致；用 v7 冻结答案重新判定。
- 本切片不负责什么：不重新运行 Agent；不修改判官提示词；不修改引用标注；不改变 N/K。
- 停止条件：新 R1 仍允许只写“安全工具”“隔离手段”等没有作用对象的空泛回答。

### 用户可观察目标

同一个 c6 题面和冻结答案在新 criteria 下重新判定；结果矩阵显示新的 case revision 和 judgment revision，旧 revision 仍可追溯。

### 核心不变量

1. c6 v2 case revision 和原 judgment revision 保持不变。
2. R1 只要求三种通用机制的核心作用正确，不强制复述单一产品配置。
3. R2 要求三者同属环境层、控制轴不同且互补，不再表述为“不同层面”。
4. 重判只复用 v7 冻结答案与 trace，不重新检索或生成答案。
5. 新 case revision 必须通过 reviewed→active 生命周期启用。

### 明确禁止

- 不得修改原 case revision 或覆盖旧 judgments。
- 不得把 Claude Code 或 Claude Cowork 的配置写成通用机制的唯一答案。
- 不得因为本次修订放宽 X1、X2 或 X3。
- 不得用新的 Agent 输出替代冻结答案来证明判定修订有效。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 新 R1 过宽 | 空泛回答通过 | 新 case revision 与新 judgments | 可停用并发布 v4 |
| 新 R1 仍绑产品配置 | 通用回答继续失败 | v3 判定证据 | 可继续修订 |
| 冻结答案重判失败 | 新 judgment revision 保持 fail/pending | 原 trace、答案和判据 | 可追溯 |

### 决策预算

- 预计 B 级决策数量：2。
- 是否存在 C 级决策：否。
- 超过预算时的拆分方式：题面修订与 criterion 修订拆为两个 revision。

## 目标

1. 将 c6 R1 改为通用机制定义，产品配置只作为允许示例。
2. 将 R2 改为同属环境层、作用轴不同且互补。
3. 创建并激活新的 c6 case revision。
4. 对 v7 冻结答案执行新 revision 判定。

## 输入与输出

- 输入：c6 v2 的题面、目标文档、N/K、执行策略，以及 v7 run 的冻结 attempts。
- 输出：新的不可变 c6 case revision、新的 judgment revision、新的 K/N 结果。

## 不包含

- 不修改 Agent prompt。
- 不增加检索工具或调整排名。
- 不修改 citation support 和 citation integrity。
- 不重跑 c6 Agent attempts。

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 |
| --- | --- | --- | --- | --- | --- |
| A1 | 题面与判据一致 | R1 不再要求单一产品配置 | 移除 Claude Code 读写网络强制项 | 必须 | 文档审查 |
| A2 | 机制角色仍可判 | 空泛“安全工具”回答仍失败 | 三对象核心作用必须可辨认 | 必须 | 人工样例 |
| A3 | 关系判据正确 | R2 表述为同层不同轴、互补 | 不出现“不同层面” | 必须 | 文档审查 |
| A4 | 旧版可追溯 | v2 case 与原 judgments 保留 | v2 仍可读取 | 必须 | 集成 |
| A5 | 冻结答案重判 | 新 judgment revision 只复用 v7 attempts | attempts 集合完全一致 | 必须 | 真实现场 |
| A6 | K/N 达标 | 新判定按 N=10、K=9 聚合 | 至少 9 pass 且无红线 | 必须 | 真实现场 |

## 验收执行

- [x] c6 通用判据 revision 已发布并激活。
- [x] v7 冻结答案已生成新 judgment revision。
- [x] 新 K/N 结果已记录。
- [x] 旧 revision 未改写。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 通用题面是否要求产品配置 | 不要求；产品配置只作示例 | 题面询问通用机制；X1 要求区分模式与手段 | 若要求配置，应改写题面明确 Claude 产品 | criterion 语义 | v7 重判 | 已确认 |
| D2 | B | 三者关系如何表述 | 同属环境层，但作用轴不同、可重叠互补 | 原文环境层分类和 overlap rule | “不同层面”会与 X1 冲突 | R2 语义 | v7 重判 | 已确认 |

## 证据包

### 自动化证据

- 命令：`.\backend\.venv\Scripts\python.exe -m pytest backend\tests\test_evaluation_judgment.py::test_frozen_run_can_be_rejudged_with_case_revision_override -q`。
- 结果：通过；验证同一 execution run 可以用替代 case revision 重判，结果矩阵保留原 attempt 与 trace。
- 命令：`corepack pnpm quality:quick`。
- 结果：通过；包含注释检查、Ruff、Pyright、Vue 类型检查、前端构建和桌面 Rust 检查。
- 命令：`.\backend\.venv\Scripts\python.exe -m pytest backend -q --basetemp .pytest-046-full`。
- 结果：完整后端测试通过。

### 运行证据

- 原 run：`run_9219aed750914d2d9c0279bea64ea24c`
- 原 judgment revision：`evalrev_a781b0c8827749189ab8607744928a34`
- 原 case revision：`cr_c6_001_minimal_judge_v2`（保持可读取）
- 新 case revision：`cr_c6_001_947b66f41ac5`（revision number 8，通用 containment 判据）
- 新 suite revision：`sr_design03_fd56c88c7bdd`（revision number 7，stale=false）
- 新 judgment revision：`evalrev_8a054db705684ab6b85354efa7b31772`
- 新判定：50/50 criterion pass；R1 10/10 pass、R2 10/10 pass。
- Case 聚合：10/10 pass、0 fail、0 pending，达到 K=9；红线为零。

### 失败探针

- 原 R1：`attempt_64b7e17826234b21a4fe97a90c9b0b94` 和 `attempt_77099a31ebac4cedb90f9592d655cff2`。
- 两轮均未取得 Claude Code 具体沙箱配置，但都说明通用 sandbox/VM/egress 作用。
- 新判据必须仍拒绝缺少三者核心角色的空泛回答。

## 已知反例账本

| 场景或反例 | 期望行为 | 实际结果／分布 | 是否阻断 | 处理结论 |
| --- | --- | --- | --- | --- |
| 通用回答漏产品配置 | 不因此失败 | 原两条 R1 fail 在新判据下均 pass | 是 | 已验证 |
| 只写“安全工具”不说明作用 | 失败 | 待构造 | 是 | 待验证 |
| 首次写入中文经 PowerShell 管道损坏 | 不保留为有效 revision | 已生成并退休 `cr_c6_001_db2c0dd0b7c7` | 是 | 改用 UTF-8 文档源重新生成 |
| 第二次 Markdown 分段解析吞入边界规则 | 不保留为有效 revision | 已生成并退休 `cr_c6_001_88237ac19e66` | 是 | 改为按 `<br>` 边界解析 |

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么通用题面不能强制 Claude Code 的读写网络配置？ | Claude Code 的规则是产品级个性化配置；沙箱的通用本质是提供受限执行环境，具体限制随场景变化。 | 通过 | 继续核对 R2 |
| R2 | 为什么 R2 从“不同层面”改为“同层不同作用轴”？ | 三者本来都属于环境层；它们的差别在控制轴，而不是分属不同防御层。 | 通过 | 继续核对 R3 |
| R3 | 为什么重判必须复用 v7 冻结答案，而不能顺便重跑 Agent？ | 重跑 Agent 会引入新的生成与检索变量，无法把结果变化归因于 criterion 修订。 | 通过 | 反向解释完成 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 已知风险与后续

- 未验证项：通用 R1 的边界需要通过后续失败案例持续校准。
- 接受的风险：题面仍是“沙箱/VM/egress”这种混合分类，X1 继续约束产品模式与底层手段的混淆。
- 后续待办：如果新标准仍不稳定，先修订题面而不是继续增加实现细节。

## 停止与推翻条件

- 如果通用判据无法区分机制说明与空泛安全口号，则增加“作用对象”要求。
- 如果用户实际要考的是具体产品配置，应改写题面明确 Claude Code/Cowork，而不是保留通用问法。
