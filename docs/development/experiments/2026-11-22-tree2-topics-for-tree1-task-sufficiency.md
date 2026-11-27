# 树 2 知识主题对树 1 任务的跨树充分性审查

- 日期：2026-11-22
- 状态：审查完成；树 2 主题不能支撑树 1 的全部任务
- 结论：树 1 的 20 个任务中，15 个充分，5 个不足
- 关联设计：[使用角色驱动的领域知识组织](../../design/16-使用角色驱动的领域知识组织.md) §4.5、§4.6
- 同树审查：[三棵任务树的知识主题充分性审查](2026-11-22-task-tree-knowledge-topic-sufficiency.md)

## 1. 问题与判断边界

本实验固定：

```text
任务树 = 树 1
知识主题树 = 树 2 归并后的知识主题
```

检查树 2 的主题能否支撑树 1 的每一个任务完成。

由于两棵树的叶任务不同，树 2 主题中的 `source_task_keys` 不能用于证明树 1 的任务覆盖。本审查完全忽略来源任务对应关系，只看树 2 主题的标题、scope、排除边界和主题间关系，再与树 1 任务的完成条件、问题范围、包含项和排除项逐项比较。

判断规则与前次审查一致：

- 多个主题共同支持一个任务即可；
- 主题比目标要求更宽、更多或彼此重叠不扣分；
- 不要求任务名与主题名相同；
- 项目私有事实、最终架构取舍、代码、参数值和实际走查记录不算知识主题缺口；
- 只有在完成条件中的必要领域知识没有任何树 2 主题承载时，才判“不足”。

## 2. 固定输入

| 项目 | 固定产物 |
| --- | --- |
| 树 1 任务 | `backend/.runtime/role_task_tree_single_root_decision_control_loop_current_20261122T032905Z/summary.json` 的 `runs[0].payload.task_tree` |
| 树 2 知识主题 | `backend/.runtime/per_leaf_topic_merge_20261122T043725Z/summary.json` 的 `records[1].stage.payload.topics` |
| 树 2 主题数 | 73 |
| 树 1 任务数 | 20 |

## 3. 总结果

| 结果 | 任务数 | 任务 |
| --- | ---: | --- |
| 充分 | 15 | 根架构、控制模型、状态、转移、终止、并发、运行时映射、场景走查及其六类子场景、一致性修复 |
| 不足 | 5 | 多步编排接口总体任务、输入与上下文初始化、规划与重规划、行动选择与工具请求、观察反馈 |

树 1 的根任务本身可按“状态／转移／约束＋接口映射＋场景＋一致性”完成，树 2 对这些顶层知识有覆盖。但只要继续检查到编排细节，就会看到树 2 提供的是控制状态和阶段骨架，不是各阶段内部流程知识。因此根任务充分不能消除子任务缺口。

## 4. 逐任务审查

| 树 1 任务 | 完成所必需的领域知识 | 树 2 中的支撑主题（标题＋键） | 结论 |
| --- | --- | --- | --- |
| `primary:decision_control_loop`<br>控制循环与多步编排架构 | 状态／转移／不变量、执行约束、接口映射、场景走查、一致性修复 | 控制循环状态与转移模型（`C01_control_state_transition_model`）；终止条件与执行预算（`T01_termination_budget`）；并发与运行时约束（`K01_concurrency_constraints`）；控制流到运行时接口映射（`I01_interface_mapping`）；正常场景覆盖判据（`N05_normal_scenario_coverage_consistency`）；失败与异常场景（`N06_failure_exception_scenario`）；跨产物一致性与修复（`X01_consistency`）；架构定稿与评审（`A10_arch_finalization_review`） | 充分 |
| `control_model`<br>状态、转移与不变量模型 | 状态语义与所有权、正常／异常转移、终止和并发不变量 | 控制循环状态与转移模型（`C01_control_state_transition_model`）；状态语义与不变量（`C03_state_semantics_invariants`）；转移、守卫与执行约束（`C04_transition_guard_constraints`）；状态所有权与责任治理（`O01_ownership_governance`）；终止条件与执行预算（`T01_termination_budget`）；并发与运行时约束（`K01_concurrency_constraints`） | 充分 |
| `control_model.states`<br>状态集合、语义与所有权 | 正常／终止／恢复／升级状态，职责、所有权和进入退出条件 | 状态集合、生命周期与分类（`C02_state_set_lifecycle_classification`）；状态语义与不变量（`C03_state_semantics_invariants`）；状态所有权与决策权限（`O03_state_ownership_decision_authority`）；跨状态移交责任（`O05_cross_state_handoff_responsibility`）；人工升级控制状态（`H03_escalation_control_states`） | 充分 |
| `control_model.transitions`<br>转移图、触发事件与动作 | 正常、失败、重试、重规划、升级路径的源目标、守卫和动作 | 转移、守卫与执行约束（`C04_transition_guard_constraints`）；正常执行路径（`N02_normal_execution_phases_paths`）；失败与异常路径场景（`N06_failure_exception_scenario`）；重试转移（`R01_retry_mechanism`）；重规划转移（`P01_replan_mechanism`）；人工升级转移（`H01_human_escalation`） | 充分 |
| `control_model.termination_invariants`<br>终止、放弃、超时与升级守卫 | 正常／异常终止、步数与超时、超限处置、升级优先级和检查点 | 正常完成与终止（`N04_normal_completion_termination`）；终止条件与执行预算（`T01_termination_budget`）；终止判定优先级（`T02_termination_condition_model_priority`）；超限处置（`T05_overlimit_handling_strategy`）；放弃终止约束（`B06_abandon_termination_constraints_invariants`）；人工升级失败与超时（`H05_escalation_failure_timeout`） | 充分 |
| `control_model.concurrency_invariants`<br>并发、重入与执行所有权不变量 | 可并发性、顺序依赖、并发上限、共享上下文冲突和运行时实施 | 并发执行模型与步骤依赖（`K02_concurrency_execution_model_step_dependencies`）；步骤可并发性判定（`K03_step_concurrency_eligibility`）；并发上限与准入（`K04_concurrency_limits_admission`）；共享上下文并发访问（`K06_shared_context_memory_concurrency`）；并发约束运行时映射（`K07_concurrency_runtime_mapping`） | 充分 |
| `orchestration_interface`<br>多步编排流程与运行时映射 | 输入理解、规划、行动／工具、观察反馈四个阶段的内部流程，以及整体接口映射 | 正常执行阶段与路径（`N02_normal_execution_phases_paths`）；上下文与记忆架构（`A03_arch_context_memory`）；工具与环境接口架构（`A02_arch_tool_env_interface`）；控制流到接口映射（`I01_interface_mapping`） | 不足 |
| `orchestration_interface.input_context`<br>输入理解与上下文初始化 | 意图／约束抽取、任务上下文元素、记忆装载、窗口预算和初始化就绪判据 | 上下文与记忆架构（`A03_arch_context_memory`）；输入理解状态语义（包含于 `C03_state_semantics_invariants`）；正常阶段路径（`N02_normal_execution_phases_paths`） | 不足 |
| `orchestration_interface.planning_replanning`<br>规划与重规划流程 | 计划表示、初始计划生成、重规划触发、修订、替换／合并与验证 | 重规划机制（`P01_replan_mechanism`）；重规划计划与上下文管理（`P03_replan_plan_context_management`）；返回规划与恢复（`P04_replan_transition_resumption`）；规划状态语义（包含于 `C03_state_semantics_invariants`） | 不足 |
| `orchestration_interface.action_tool_request`<br>行动选择与工具请求 | 候选行动生成与选择策略、工具能力与调用边界、请求参数生成、调用前校验 | 工具与环境接口架构（`A02_arch_tool_env_interface`）；转移守卫（`C04_transition_guard_constraints`）；安全、权限与护栏（`A05_arch_security_guardrails`）；接口契约要素（`I02_runtime_interface_system_contracts`） | 不足 |
| `orchestration_interface.observation_feedback`<br>观察接收与反馈处理 | 工具结果／环境观察接收、解析规范化、状态／记忆更新、失败归因和反馈路由 | 正常执行阶段与路径（`N02_normal_execution_phases_paths`）；失败与异常路径场景（`N06_failure_exception_scenario`）；上下文与记忆架构（`A03_arch_context_memory`）；可观测性与评估架构（`A06_arch_observability_eval`） | 不足 |
| `orchestration_interface.runtime_mapping`<br>控制模型到运行时接口映射 | 状态／转移／不变量／约束到接口、事件、持久化、配置及覆盖验证 | 控制流到运行时接口映射（`I01_interface_mapping`）；运行时接口体系与契约（`I02_runtime_interface_system_contracts`）；控制流元素到接口映射（`I03_control_flow_to_interface_mapping`）；接口契约覆盖与可执行性（`I04_interface_contract_coverage_executability`） | 充分 |
| `scenario_walkthroughs`<br>正常与异常场景走查 | 场景构造、路径追踪、覆盖判据、异常路径验证和接口执行性 | 正常场景覆盖与一致性判据（`N05_normal_scenario_coverage_consistency`）；失败与异常路径场景（`N06_failure_exception_scenario`）；重试路径可观测性（`R07_retry_observability`）；接口契约覆盖与可执行性（`I04_interface_contract_coverage_executability`） | 充分 |
| `scenario_walkthroughs.normal`<br>正常完成走查 | 正常路径选例、状态转移覆盖、完成守卫和接口检查 | 正常多步执行模型（`N01_normal_execution_model`）；正常执行阶段与路径（`N02_normal_execution_phases_paths`）；正常转移守卫（`N03_normal_transition_guards`）；正常完成终止（`N04_normal_completion_termination`）；正常场景覆盖判据（`N05_normal_scenario_coverage_consistency`） | 充分 |
| `scenario_walkthroughs.abandon`<br>放弃场景走查 | 放弃触发、守卫、最终状态、清理、原因记录和终止不变量 | 放弃语义与触发（`B02_abandon_semantics_triggers`）；放弃转移与最终状态（`B03_abandon_transition_control_final_state`）；放弃清理（`B04_abandon_cleanup_resource_release`）；放弃原因审计（`B05_abandon_reason_audit`）；放弃终止不变量（`B06_abandon_termination_constraints_invariants`） | 充分 |
| `scenario_walkthroughs.retry`<br>重试场景走查 | 可重试故障、触发守卫、检查点、上下文副作用、预算、终止和诊断 | 重试机制（`R01_retry_mechanism`）；可重试失败判定（`R02_retry_failure_classification`）；重试触发与守卫（`R03_retry_trigger_guard`）；检查点恢复（`R04_retry_scope_entry_checkpoint`）；重试预算与终止（`R06_retry_budget_termination`）；重试可观测性（`R07_retry_observability`） | 充分 |
| `scenario_walkthroughs.replan`<br>重规划场景走查 | 失效触发、计划与上下文管理、返回规划、恢复执行和不变量 | 重规划机制（`P01_replan_mechanism`）；重规划触发与守卫（`P02_replan_trigger_guard`）；计划与上下文管理（`P03_replan_plan_context_management`）；返回规划与执行恢复（`P04_replan_transition_resumption`）；重规划不变量（`P05_replan_invariants_constraints`）；异常路径场景（`N06_failure_exception_scenario`） | 充分 |
| `scenario_walkthroughs.escalation`<br>人工升级场景走查 | 升级触发、状态暂停／移交／等待、恢复或终止、责任边界和输出 | 人工升级控制（`H01_human_escalation`）；触发守卫（`H02_escalation_trigger_guard`）；恢复／终止状态（`H03_escalation_control_states`）；升级包与上下文交接（`H04_escalation_payload_context_handoff`）；人工升级失败与超时（`H05_escalation_failure_timeout`）；升级不变量（`H07_escalation_invariants_safety`） | 充分 |
| `scenario_walkthroughs.boundaries`<br>步数、超时与并发边界走查 | 最大步数与超时、并发和共享状态边界、触发后终止或等待、违规映射 | 终止条件与执行预算（`T01_termination_budget`）；超限处置（`T05_overlimit_handling_strategy`）；并发执行约束（`K01_concurrency_constraints`）；共享上下文并发访问（`K06_shared_context_memory_concurrency`）；失败与异常路径场景（`N06_failure_exception_scenario`） | 充分 |
| `integration_consistency_fix`<br>跨模型、接口与场景一致性修复 | 跨产物准则、冲突分类、修复模式、证据追溯、风险闭环和最终基线 | 跨产物一致性与修复（`X01_consistency`）；一致性准则（`X02_cross_artifact_consistency_criteria`）；不一致修复模式（`X03_inconsistency_repair_patterns`）；一致性证据追溯（`X04_consistency_evidence_traceability`）；架构决策与取舍（`A08_arch_decision_tradeoff`）；风险闭环（`A09_arch_risk_management`）；架构定稿评审（`A10_arch_finalization_review`） | 充分 |

## 5. 五个不足任务的具体缺口

### 5.1 输入理解与任务上下文初始化

完成条件要求：

- 从用户输入中解析目标、约束和任务语义；
- 形成控制循环可消费的任务上下文；
- 装载会话、记忆和必要的环境信息；
- 给出上下文装配、窗口预算和初始化就绪判据。

树 2 只有输入理解状态本身的分类和语义（`C02_state_set_lifecycle_classification`、`C03_state_semantics_invariants`），以及上下文／记忆的总体架构（`A03_arch_context_memory`）。它没有输入规范化、意图识别、目标陈述、约束／实体抽取、歧义与澄清、任务上下文字段模型和就绪判据。`A03` 能说明上下文和记忆的架构位置，不能替代初始化流程知识。

### 5.2 规划与重规划

完成条件要求：

- 定义计划对象和计划元数据；
- 从目标与上下文生成初始计划；
- 定义执行监控和重规划触发；
- 定义计划修订、校验、新旧计划替换或合并。

树 2 的 `P01` 至 `P05` 主要覆盖重规划的触发、计划／上下文管理、返回规划和约束验证，能够支持重规划转移，但没有完整承载计划表示和初始计划生成。`C03_state_semantics_invariants` 只说明规划状态的职责和边界，不提供计划内容模型与生成过程。因此本任务不能仅靠现有主题完整完成。

### 5.3 行动选择与工具调用请求

完成条件要求：

- 从计划、上下文和可用动作中产生候选行动；
- 按目标、约束、风险和效用选择行动；
- 描述工具能力、输入输出、副作用、风险和调用边界；
- 生成调用参数并在调用前完成模式、权限、前置条件和可用性校验。

`A02_arch_tool_env_interface` 提供了工具选择、参数约定、权限和调用错误的架构视角；`A05` 补充权限与护栏，`C04` 补充转移守卫。但这些主题没有完整覆盖候选行动集合与选择策略、请求构造、参数生成和调用前校验流程。现有内容更接近“这些能力位于架构何处”，而不是“如何完成一次行动选择和工具请求”。

### 5.4 观察反馈与失败归因

完成条件要求：

- 接收工具返回值或环境观察；
- 对结果、错误载荷和元数据进行模式校验、解析和规范化；
- 更新控制状态、记忆和证据记录；
- 分类失败、判断责任边界和可恢复性；
- 把反馈路由到规划、行动选择、重试、替代行动、澄清或终止。

树 2 的 `N02` 只把“观察整合”列为正常阶段，`N06` 描述失败与异常路径，`A03` 提供上下文／记忆架构，`A06` 提供可观测性能力。它们没有承载观察解析、状态／记忆更新规则和失败反馈路由本身，因此不能完整支撑这个任务。

### 5.5 多步编排接口总体任务

`orchestration_interface` 要求明确输入理解、规划／重规划、行动选择与工具调用、观察反馈以及运行时映射的编排方式。树 2 对运行时映射覆盖充分，对四种阶段只提供名称、状态语义和路径骨架；其内部流程恰由前述四个不足任务承载。因此该总体任务也判不足。

## 6. 为什么其他编排相关任务仍然可复用

树 2 不足以覆盖全部树 1 编排任务，不代表树 2 主题整体不可复用。以下能力在树 2 中反而比树 1 更集中：

- 状态语义、不变量、生命周期和所有权；
- 正常与异常转移；
- 终止预算、超时、并发和共享上下文约束；
- 重试、重规划、放弃和人工升级机制；
- 运行时接口映射、场景覆盖、跨产物一致性和最终定稿。

因此，更合适的结论是“可复用但需要补层”，而不是“树 1 或树 2 必须二选一”。若要只保留一套知识主题树，应把树 1 中被证明必要的编排流程主题增量合入树 2；若保留多棵树，则可把树 2 作为控制治理骨架，把树 1 或领域特定的编排主题作为补充视图。

## 7. 最终结论

回答本轮问题：**不能。**

树 2 的知识主题树可以支撑树 1 中 15/20 个任务，不能完整支撑 5 个与多步编排内部流程有关的任务。不能完成的部分集中在：

- 输入理解与上下文初始化；
- 计划表示与初始规划；
- 行动选择和工具请求构造；
- 观察解析、状态更新和反馈路由；
- 由上述缺口组成的多步编排接口总体任务。

## 8. 未验证项

本审查没有验证：

- 树 2 的主题是否能在原文知识库中找到足够证据；
- 在线检索是否能正确选中树 2 某组主题并联合回答树 1 的充分任务；
- 复合主题的标题是否会造成路由歧义；
- 将缺失主题补入树 2 后的证据覆盖和检索效果。

这些属于后续编译与检索实验，不改变本轮“主题 scope 不足以支撑全部树 1 任务”的结论。

## 9. 复现方式

1. 读取任务树 summary 的 `runs[0].payload.task_tree`，得到树 1 的 20 个任务及其完成条件；
2. 读取归并 summary 的 `records[1].stage.payload.topics`，得到树 2 的 73 个主题；
3. 不读取或使用树 2 的 `source_task_keys` 与树 1 做机械匹配；
4. 对每个树 1 任务，将完成条件拆成领域知识单元，在树 2 的主题 `title + scope + exclusions` 中逐项查找承载；
5. 按本报告 §4 的判定记录充分或不足，并对不足项说明具体缺少了什么知识。
