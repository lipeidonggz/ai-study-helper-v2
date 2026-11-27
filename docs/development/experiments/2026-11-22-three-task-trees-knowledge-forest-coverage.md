# 三棵任务树的知识主题森林与跨树覆盖实验（2026-11-22）

## 1. 目的与结论边界

本实验验证两件事：三棵任务树各自生成的冻结知识主题森林，能否逐任务回答本树；以及每套森林能否跨树回答另外两棵任务树。

这里的“可以回答”只表示知识主题级覆盖：森林中存在完成该任务所需领域知识的可路由入口。它不读取原文 block、不判断证据挂接是否充分，也不证明最终问答答案正确或完整。根任务和中间任务允许由叶任务对应主题的并集支持。

## 2. 固定输入与生成结果

| 森林 | 来源任务树 | 任务/叶任务 | 局部主题 | 全局主题 | 父子边 | 根主题 | 全局修订 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 树 1 | 树 1 | 30 / 21 | 217 | 57 | 2 | 55 | G1 |
| 树 2 | 树 2 | 24 / 16 | 155 | 31 | 28 | 3 | G2 |
| 树 3 | 树 3 | 26 / 18 | 190 | 44 | 9 | 35 | F1（同契约 3 次修复阻塞后，聚焦拆分恢复） |

树 3 的初始全局归并没有直接通过：同契约初始版加 3 次修复后，`control_flow_boundary_conditions` 与 `task_interface_contract_semantics` 仍被判为过度合并。实验保留该 blocked 轨迹，随后只针对这两个节点执行一次范围重新划分；完整替换图通过同一全局审查后才冻结为 `tree_3_frozen.json`。

全局审查器在运行前先用已知失败的整树候选校准。它必须把“Agent 统一决策控制流规范编制”判为 `non_topic_node + task_action_leak`，否则实验停止；校准实际通过。

## 3. 3×3 覆盖矩阵

| 森林 \ 任务树 | 树 1（30 任务） | 树 2（24 任务） | 树 3（26 任务） |
| --- | --- | --- | --- |
| 森林 1 | 通过<br>30 covered / 0 partial / 0 missing | 通过<br>24 covered / 0 partial / 0 missing | 部分覆盖<br>21 covered / 5 partial / 0 missing |
| 森林 2 | 通过<br>30 covered / 0 partial / 0 missing | 通过<br>24 covered / 0 partial / 0 missing | 通过<br>26 covered / 0 partial / 0 missing |
| 森林 3 | 部分覆盖<br>18 covered / 12 partial / 0 missing | 部分覆盖<br>18 covered / 6 partial / 0 missing | 通过<br>26 covered / 0 partial / 0 missing |

## 4. 结论

1. **各自任务树均可回答。** 对角线 3 组均为全部任务 covered：森林 1 为 30/30、森林 2 为 24/24、森林 3 为 26/26。
2. **森林 2 是唯一 9 组全部通过的森林。** 它不仅覆盖本树 24/24，也覆盖树 1 的 30/30 和树 3 的 26/26。它是当前三棵任务树中复用范围最广的一套森林。
3. **森林 1 可跨树覆盖树 2，但不能完整覆盖树 3。** 森林 1 对树 2 为 24/24；对树 3 为 21 covered、5 partial、0 missing。主要缺口是状态/转移 I/O 契约、计划生成/选择、重规划控制状态与退出条件。
4. **森林 3 对另外两棵更细的树覆盖较弱。** 对树 1 为 18 covered、12 partial；对树 2 为 18 covered、6 partial。反复出现的缺口包括首次规划触发、重规划输入输出与范围约束、上下文/策略组件契约、转移动作与副作用、不变量违反处置、目标解析异常和审计交互。
5. **没有出现 missing。** 所有跨树不足都是 partial；说明三套森林的主要知识方向一致，但任务树越细，越需要更具体的知识入口。
6. **主题数量不等于复用能力。** 森林 1 有 57 个主题但只有 2 条边、55 个根主题；它覆盖本树和树 2，却没有自动补足树 3 要求的细粒度契约知识。森林 2 只有 31 个主题和 28 条边，却覆盖三棵树，说明稳定上位组织和必要细分比单纯堆主题更重要。

## 5. 方法、证据与限制

- 局部生成：55 个叶任务逐个调用 §4A.5.1，无父任务、无兄弟任务、无完整树上下文；温度 0，开启 thinking，最大输出 60000。55/55 通过局部结构与语义门禁。
- 树内归并：丢弃局部主题键和局部边，只把标题、范围、排除边界和来源任务送入每棵树的一次全局归并；全局审查逐任务、逐主题、逐边执行。
- 覆盖检查：每套冻结森林分别对三棵完整任务树逐任务评估；覆盖输入不包含 `source_leaf_task_keys`、`coverage_reason` 或归并审查结论，避免来源声明自证覆盖。
- 交付核验：九个配对共 240 条任务判断，与三棵任务树的 30、24、26 个任务逐项对齐；每个主题 ID 都存在于对应冻结森林，partial 项均有明确缺口，covered 项没有残留缺口。
- 冻结完整性：三套 `tree_*_frozen.json` 均按 `topics + edges` 的规范化 JSON 重算 SHA-256，结果与文件内 `frozen_sha256` 一致。
- 森林 1→树 3 的原始输出有一个 `tree1:topic/0034` 主题 ID 拼写错误。最终化只把 `/` 规范为 `:`，并记录为语法纠正；它不改变任何任务的 covered/partial 判断。
- 覆盖调用固定模型 `deepseek-flash`、温度 0、thinking 开启、最大输出 60000。完整原始调用与逐任务理由见 `backend/.runtime/role_task_tree_v2_forest_task_coverage_20261122T100213Z`。
- 未验证项：原文证据是否足以回答、主题内部证据挂接、查询时路由召回、答案完整性和最终用户答案质量。

## 6. 主题身份索引

### 6.1 森林 1

| 全局主题 ID | 主题 |
| --- | --- |
| `tree1:topic:0001` | AI Agent 状态机控制流形式语义 |
| `tree1:topic:0002` | AI Agent 决策循环状态集合与运行状态模型 |
| `tree1:topic:0003` | AI Agent 状态转移规格、转移表与执行语义 |
| `tree1:topic:0004` | AI Agent 决策循环控制流与运行时不变量 |
| `tree1:topic:0005` | AI Agent 不变量规范条目、检查与违反处理 |
| `tree1:topic:0006` | AI Agent 执行循环与决策点 |
| `tree1:topic:0007` | AI Agent 输入目标解析与规范化 |
| `tree1:topic:0008` | AI Agent 目标状态表示与可检查性 |
| `tree1:topic:0009` | AI Agent 目标解析异常与未决状态 |
| `tree1:topic:0010` | AI Agent 目标进展与停滞判定 |
| `tree1:topic:0011` | AI Agent 规划与重规划触发条件 |
| `tree1:topic:0012` | AI Agent 重规划范围、约束与目标一致性 |
| `tree1:topic:0013` | AI Agent 行动空间与候选行动生成 |
| `tree1:topic:0014` | AI Agent 行动合法性判定 |
| `tree1:topic:0015` | AI Agent 行动选择依据、优先级与效用比较 |
| `tree1:topic:0016` | AI Agent 行动输出契约与选择可追溯性 |
| `tree1:topic:0017` | AI Agent 观察输入、解析与吸收生命周期 |
| `tree1:topic:0018` | AI Agent 观察驱动状态更新规则 |
| `tree1:topic:0019` | AI Agent 观察吸收合法性校验与异常观察处理 |
| `tree1:topic:0020` | AI Agent 上下文组件职责与接口契约 |
| `tree1:topic:0021` | AI Agent 上下文载荷数据模型与消息格式 |
| `tree1:topic:0022` | AI Agent 上下文访问控制契约 |
| `tree1:topic:0023` | AI Agent 上下文与运行状态一致性模型 |
| `tree1:topic:0024` | AI Agent 决策循环-工具组件交互契约 |
| `tree1:topic:0025` | AI Agent 工具参数返回格式与失败拒绝语义 |
| `tree1:topic:0026` | AI Agent 工具权限边界契约 |
| `tree1:topic:0027` | AI Agent 策略组件职责与决策循环交互定位 |
| `tree1:topic:0028` | AI Agent 策略查询决策接口、数据格式与调用时序 |
| `tree1:topic:0029` | AI Agent 策略权限边界、决策结果与拒绝控制流 |
| `tree1:topic:0030` | AI Agent 正常完成判定与完成输出语义 |
| `tree1:topic:0031` | AI Agent 执行中止触发条件与原因分类 |
| `tree1:topic:0032` | AI Agent 执行中止状态转移与终态竞态裁决 |
| `tree1:topic:0033` | AI Agent 执行中止输出契约 |
| `tree1:topic:0034` | AI Agent 不可恢复失败与失败终止触发判定 |
| `tree1:topic:0035` | AI Agent 失败终止状态转移、输出与终态边界 |
| `tree1:topic:0036` | AI Agent 执行暂停触发条件 |
| `tree1:topic:0037` | AI Agent 暂停状态保持要求 |
| `tree1:topic:0038` | AI Agent 恢复条件、续执行点与恢复后状态转移 |
| `tree1:topic:0039` | AI Agent 人工升级控制状态与触发条件 |
| `tree1:topic:0040` | AI Agent 人工交接内容与格式规范 |
| `tree1:topic:0041` | AI Agent 人工等待与超时处理语义 |
| `tree1:topic:0042` | AI Agent 人工返回处理与继续执行 |
| `tree1:topic:0043` | AI Agent 决策循环控制流集成模型与子规范引用 |
| `tree1:topic:0044` | AI Agent 控制流规范一致性检查要素与引用完整性 |
| `tree1:topic:0045` | AI Agent 集成与控制流规范冲突分类 |
| `tree1:topic:0046` | AI Agent 控制流规范一致性审计追溯模型 |
| `tree1:topic:0047` | AI Agent 集成规范冲突修复策略与未定义点填补 |
| `tree1:topic:0048` | AI Agent 集成规范取舍优先级与未决风险标注 |
| `tree1:topic:0049` | AI Agent 架构组件对控制流集成的约束 |
| `tree1:topic:0050` | AI Agent 控制流验证场景分类与覆盖框架 |
| `tree1:topic:0051` | AI Agent 正常路径控制流场景模式 |
| `tree1:topic:0052` | AI Agent 失败路径控制流场景模式 |
| `tree1:topic:0053` | AI Agent 边界条件控制流场景模式 |
| `tree1:topic:0054` | AI Agent 组件交互控制流语义 |
| `tree1:topic:0055` | AI Agent 控制流转移完备性与未定义转移判定 |
| `tree1:topic:0056` | AI Agent 控制流场景走查与原型验证方法 |
| `tree1:topic:0057` | AI Agent 未定义转移检查证据与覆盖判据 |

### 6.2 森林 2

| 全局主题 ID | 主题 |
| --- | --- |
| `tree2:topic:0001` | AI Agent 决策循环总体控制结构 |
| `tree2:topic:0002` | AI Agent 决策循环状态-转移-契约控制模型 |
| `tree2:topic:0003` | AI Agent 决策循环状态模型 |
| `tree2:topic:0004` | AI Agent 状态转移规则与事件守卫 |
| `tree2:topic:0005` | AI Agent 决策循环内部控制契约 |
| `tree2:topic:0006` | AI Agent 目标-规划-行动-观察控制链 |
| `tree2:topic:0007` | AI Agent 目标解析与目标表示语义 |
| `tree2:topic:0008` | AI Agent 目标进度判定与控制流传递语义 |
| `tree2:topic:0009` | AI Agent 规划与重规划控制语义 |
| `tree2:topic:0010` | AI Agent 行动选择语义与结果表示 |
| `tree2:topic:0011` | AI Agent 行动发出与执行异常控制契约 |
| `tree2:topic:0012` | AI Agent 观察吸收与状态更新机制 |
| `tree2:topic:0013` | AI Agent 运行结束与人工介入控制模型 |
| `tree2:topic:0014` | AI Agent 运行终态判定与优先级 |
| `tree2:topic:0015` | AI Agent 暂停/恢复控制语义 |
| `tree2:topic:0016` | AI Agent 人工升级与升级后控制 |
| `tree2:topic:0017` | AI Agent 决策循环与外部组件交互约束 |
| `tree2:topic:0018` | AI Agent 决策循环与上下文/记忆组件交互约束 |
| `tree2:topic:0019` | AI Agent 决策循环与工具调用组件交互约束 |
| `tree2:topic:0020` | AI Agent 决策循环与策略/权限/审计组件交互约束 |
| `tree2:topic:0021` | AI Agent 控制流规范集成与一致性治理 |
| `tree2:topic:0022` | AI Agent 控制流规范集成与引用完整性 |
| `tree2:topic:0023` | AI Agent 决策循环跨要素规范一致性约束 |
| `tree2:topic:0024` | AI Agent 规范冲突识别、消解与取舍记录 |
| `tree2:topic:0025` | AI Agent 控制流验证与缺陷治理 |
| `tree2:topic:0026` | AI Agent 控制流验证方法与场景覆盖 |
| `tree2:topic:0027` | AI Agent 正常、失败与边界场景控制流行为 |
| `tree2:topic:0028` | AI Agent 控制流原型验证环境与运行轨迹 |
| `tree2:topic:0029` | AI Agent 控制流缺陷与偏差识别 |
| `tree2:topic:0030` | AI Agent 控制流缺陷修复模式 |
| `tree2:topic:0031` | AI Agent 控制流复验与遗留风险评估 |

### 6.3 森林 3

| 全局主题 ID | 主题 |
| --- | --- |
| `tree3:topic:0001` | AI Agent 决策循环状态模型与职责划分 |
| `tree3:topic:0002` | AI Agent 决策循环状态转移关系、触发条件与守卫 |
| `tree3:topic:0003` | AI Agent 决策循环不变量：语义与表达 |
| `tree3:topic:0004` | AI Agent 决策循环不变量类别与具体约束 |
| `tree3:topic:0005` | AI Agent 状态与关键转移的输入输出契约 |
| `tree3:topic:0006` | AI Agent 控制流接口完整性检查 |
| `tree3:topic:0007` | AI Agent 决策循环组件交互职责边界 |
| `tree3:topic:0008` | AI Agent 决策循环与上下文组件的交互约束 |
| `tree3:topic:0009` | AI Agent 决策循环与工具组件的交互边界 |
| `tree3:topic:0010` | AI Agent 决策循环与策略组件的交互约束 |
| `tree3:topic:0011` | AI Agent 行动选择语义 |
| `tree3:topic:0012` | AI Agent 行动表示、行动空间与候选行动生成 |
| `tree3:topic:0013` | AI Agent 行动选择依据与可行性约束 |
| `tree3:topic:0014` | AI Agent 行动选择决策规则与结果输出契约 |
| `tree3:topic:0015` | AI Agent 目标解析与可执行目标规格 |
| `tree3:topic:0016` | AI Agent 目标推进与受阻判定 |
| `tree3:topic:0017` | AI Agent 计划表示与规划问题建模 |
| `tree3:topic:0018` | AI Agent 计划生成与选择语义 |
| `tree3:topic:0019` | AI Agent 计划执行监控与偏差检测 |
| `tree3:topic:0020` | AI Agent 重规划触发条件、控制状态与退出语义 |
| `tree3:topic:0021` | AI Agent 观察接收、吸收与归一化语义 |
| `tree3:topic:0022` | AI Agent 观察驱动的控制状态更新与控制权移交 |
| `tree3:topic:0023` | AI Agent 行动执行语义 |
| `tree3:topic:0024` | AI Agent 工具调用接口契约与结果返回语义 |
| `tree3:topic:0025` | AI Agent 行动-观察循环调度与步骤依赖控制 |
| `tree3:topic:0026` | AI Agent 运行终态框架、触发来源与审计证据 |
| `tree3:topic:0027` | AI Agent 完成判定与完成状态语义 |
| `tree3:topic:0028` | AI Agent 中止判定与中止状态语义 |
| `tree3:topic:0029` | AI Agent 终止判定与终止状态语义 |
| `tree3:topic:0030` | AI Agent 暂停与人工升级控制模型 |
| `tree3:topic:0031` | AI Agent 暂停触发、状态保持与恢复语义 |
| `tree3:topic:0032` | AI Agent 人工升级触发、交接与恢复语义 |
| `tree3:topic:0033` | AI Agent 控制流边界条件分类框架 |
| `tree3:topic:0034` | AI Agent 重复观察控制语义 |
| `tree3:topic:0035` | AI Agent 空计划与无下一动作边界语义 |
| `tree3:topic:0036` | AI Agent 控制流验证方法 |
| `tree3:topic:0037` | AI Agent 正常路径控制流验证 |
| `tree3:topic:0038` | AI Agent 失败路径控制流验证 |
| `tree3:topic:0039` | AI Agent 边界场景控制流验证 |
| `tree3:topic:0040` | AI Agent 未定义转移的识别、汇总与处置 |
| `tree3:topic:0041` | AI Agent 控制流规范元素完备性判据 |
| `tree3:topic:0042` | AI Agent 跨子任务语义一致性与冲突分类 |
| `tree3:topic:0043` | AI Agent 跨子任务冲突取舍、修复与同步 |
| `tree3:topic:0044` | AI Agent 失败分支控制流与回退/升级语义 |

## 7. 逐任务结果

### 7.1 森林 1 → 任务树 1

结果：pass；covered 30，partial 0，missing 0。

<details>
<summary>展开逐任务检查</summary>

| 任务 | 状态 | 相关主题 ID | 缺口 | 判断依据 |
| --- | --- | --- | --- | --- |
| 设计 Agent 的决策循环与执行控制状态机，定义目标推进、规划/重规划、行动选择、观察吸收、终止/暂停/人工升级的可执行语义。<br><code>decision_loop_control_architecture</code> | covered | `tree1:topic:0001`, `tree1:topic:0002`, `tree1:topic:0003`, `tree1:topic:0004`, `tree1:topic:0005`, `tree1:topic:0006`, `tree1:topic:0007`, `tree1:topic:0008`, `tree1:topic:0009`, `tree1:topic:0010`, `tree1:topic:0011`, `tree1:topic:0012`, `tree1:topic:0013`, `tree1:topic:0014`, `tree1:topic:0015`, `tree1:topic:0016`, `tree1:topic:0017`, `tree1:topic:0018`, `tree1:topic:0019`, `tree1:topic:0020`, `tree1:topic:0021`, `tree1:topic:0022`, `tree1:topic:0023`, `tree1:topic:0024`, `tree1:topic:0025`, `tree1:topic:0026`, `tree1:topic:0027`, `tree1:topic:0028`, `tree1:topic:0029`, `tree1:topic:0030`, `tree1:topic:0031`, `tree1:topic:0032`, `tree1:topic:0033`, `tree1:topic:0034`, `tree1:topic:0035`, `tree1:topic:0036`, `tree1:topic:0037`, `tree1:topic:0038`, `tree1:topic:0039`, `tree1:topic:0040`, `tree1:topic:0041`, `tree1:topic:0042`, `tree1:topic:0043`, `tree1:topic:0044`, `tree1:topic:0045`, `tree1:topic:0046`, `tree1:topic:0047`, `tree1:topic:0048`, `tree1:topic:0049`, `tree1:topic:0050`, `tree1:topic:0051`, `tree1:topic:0052`, `tree1:topic:0053`, `tree1:topic:0054`, `tree1:topic:0055`, `tree1:topic:0056`, `tree1:topic:0057` | — | 森林中的主题覆盖状态机形式语义、状态模型、转移、不变量、目标/规划、行动/观察、组件契约、终止/升级、集成一致性及验证场景，并由叶任务主题并集支持根任务。 |
| 设计决策循环的状态集合、转移条件与循环不变量，作为控制流规范的核心状态模型。<br><code>state_model</code> | covered | `tree1:topic:0001`, `tree1:topic:0002`, `tree1:topic:0003`, `tree1:topic:0004`, `tree1:topic:0005` | — | 0001提供状态机形式语义，0002覆盖状态集合与状态语义，0003覆盖转移规格，0004/0005覆盖不变量及违反处理，组合满足状态模型设计所需知识入口。 |
| 定义决策循环的状态集合，并明确每个状态的语义、进入/退出含义以及状态内允许的活动。<br><code>state_set</code> | covered | `tree1:topic:0001`, `tree1:topic:0002` | — | 0002直接覆盖状态类型、命名、核心语义、进入/退出、允许活动、完整性与职责不重叠原则，0001提供状态概念的形式语义。 |
| 定义状态之间的转移条件、守卫判断和转移动作，确保每个转移有明确触发语义。<br><code>transitions</code> | covered | `tree1:topic:0001`, `tree1:topic:0003` | — | 0003直接定义转移规格、转移表字段、源/目标引用约束、触发条件、守卫、动作副作用、冲突优先级和完整性检查，0001提供转移的形式语义。 |
| 定义决策循环的循环不变量和关键状态不变量，用于约束控制流执行与验证。<br><code>invariants</code> | covered | `tree1:topic:0001`, `tree1:topic:0004`, `tree1:topic:0005` | — | 0004覆盖控制流与运行时不变量，0005覆盖不变量规范条目、检查机制、违反分类及恢复/升级/终止语义和审计要求。 |
| 设计目标解析、目标推进判定以及规划/重规划触发与范围语义。<br><code>goal_planning</code> | covered | `tree1:topic:0007`, `tree1:topic:0008`, `tree1:topic:0009`, `tree1:topic:0010`, `tree1:topic:0011`, `tree1:topic:0012` | — | 0007-0012覆盖目标解析、目标状态表示、解析异常、进展/停滞判定、规划/重规划触发和重规划范围/约束/一致性，组合满足该任务。 |
| 定义目标解析、目标状态表示以及目标推进与停滞判定语义。<br><code>goal_parse_progress</code> | covered | `tree1:topic:0007`, `tree1:topic:0008`, `tree1:topic:0009`, `tree1:topic:0010` | — | 0007-0009覆盖目标解析、表示与解析异常，0010覆盖目标进展与停滞判定、三态裁决和证据冲突原则。 |
| 定义 Agent 如何解析输入目标并形成可用的目标状态表示。<br><code>goal_parse_representation</code> | covered | `tree1:topic:0007`, `tree1:topic:0008`, `tree1:topic:0009` | — | 0007覆盖输入目标解析与规范化，0008覆盖目标状态表示与可检查性，0009覆盖解析异常分类、处理动作和未决状态表示。 |
| 定义如何判定一次决策循环是否推进目标，以及何时判定目标停滞。<br><code>goal_progress_stagnation</code> | covered | `tree1:topic:0010` | — | 0010直接覆盖目标进展与停滞判定，包括观察比较、推进/停滞条件、三态裁决、阈值、证据冲突和结论更新。 |
| 定义规划/重规划触发条件以及重规划范围与约束语义。<br><code>planning_replanning</code> | covered | `tree1:topic:0011`, `tree1:topic:0012` | — | 0011覆盖规划/重规划触发机制与优先级，0012覆盖重规划契约、范围、约束、目标一致性和合法性判定。 |
| 定义何时触发初始规划或重规划，包括触发事件、条件和优先级。<br><code>planning_replanning_triggers</code> | covered | `tree1:topic:0011`, `tree1:topic:0003` | — | 0011直接覆盖初始规划/重规划触发事件、条件、优先级和计划有效性依据；0003提供与状态模型转移事件/守卫衔接的知识。 |
| 定义重规划时可调整的计划范围、约束以及与原目标的一致性要求。<br><code>replanning_scope_constraints</code> | covered | `tree1:topic:0012` | — | 0012直接覆盖重规划契约模型、变更范围、硬/软约束、既有承诺、与原目标一致性和合法性判定。 |
| 设计行动选择、观察吸收以及决策循环与上下文、工具和策略组件的交互约束。<br><code>action_observation_contracts</code> | covered | `tree1:topic:0013`, `tree1:topic:0014`, `tree1:topic:0015`, `tree1:topic:0016`, `tree1:topic:0017`, `tree1:topic:0018`, `tree1:topic:0019`, `tree1:topic:0020`, `tree1:topic:0021`, `tree1:topic:0022`, `tree1:topic:0023`, `tree1:topic:0024`, `tree1:topic:0025`, `tree1:topic:0026`, `tree1:topic:0027`, `tree1:topic:0028`, `tree1:topic:0029` | — | 0013-0019覆盖行动选择与观察吸收，0020-0029覆盖上下文、工具、策略组件契约及交互约束，组合满足该父任务。 |
| 定义 Agent 在给定状态和计划下如何选择行动，包括候选行动、选择依据和行动输出契约。<br><code>action_selection</code> | covered | `tree1:topic:0013`, `tree1:topic:0014`, `tree1:topic:0015`, `tree1:topic:0016` | — | 0013覆盖候选行动来源与生成，0014覆盖合法性判定，0015覆盖选择依据/优先级/效用比较，0016覆盖输出契约与可追溯性。 |
| 定义 Agent 如何接收、解析观察并更新上下文、记忆状态或目标推进状态。<br><code>observation_absorption</code> | covered | `tree1:topic:0017`, `tree1:topic:0018`, `tree1:topic:0019` | — | 0017覆盖观察输入/解析/吸收生命周期，0018覆盖观察驱动状态更新规则，0019覆盖吸收合法性校验与异常观察处理。 |
| 定义决策循环与上下文、工具和策略组件之间的输入输出契约、时序、权限边界和数据流约束。<br><code>component_contracts</code> | covered | `tree1:topic:0020`, `tree1:topic:0021`, `tree1:topic:0022`, `tree1:topic:0023`, `tree1:topic:0024`, `tree1:topic:0025`, `tree1:topic:0026`, `tree1:topic:0027`, `tree1:topic:0028`, `tree1:topic:0029` | — | 0020-0023覆盖上下文组件契约、载荷、访问控制和一致性，0024-0026覆盖工具组件契约、失败拒绝与权限，0027-0029覆盖策略组件契约、接口和拒绝控制流。 |
| 定义决策循环与上下文组件之间的输入输出契约、时序、数据格式和权限边界。<br><code>context_component_contract</code> | covered | `tree1:topic:0020`, `tree1:topic:0021`, `tree1:topic:0022`, `tree1:topic:0023` | — | 0020覆盖上下文接口契约与调用时序，0021覆盖载荷数据模型与消息格式，0022覆盖访问控制与权限边界，0023覆盖上下文与运行状态一致性。 |
| 定义决策循环与工具组件之间的输入输出契约、调用时序、权限边界和失败/拒绝语义。<br><code>tool_component_contract</code> | covered | `tree1:topic:0024`, `tree1:topic:0025`, `tree1:topic:0026` | — | 0024覆盖工具组件交互契约整体模型、调用入口、时序、等待/超时、取消和衔接，0025覆盖参数返回格式及失败/拒绝语义，0026覆盖工具权限边界契约。 |
| 定义决策循环与策略组件之间的输入输出契约、时序、权限边界和策略决策约束。<br><code>policy_component_contract</code> | covered | `tree1:topic:0027`, `tree1:topic:0028`, `tree1:topic:0029` | — | 0027覆盖策略组件职责与交互定位，0028覆盖策略查询/决策接口、数据格式和调用时序，0029覆盖策略权限、决策结果与拒绝控制流。 |
| 设计决策循环的完成判定、中止、失败终止、暂停/恢复和人工升级语义。<br><code>termination_escalation</code> | covered | `tree1:topic:0030`, `tree1:topic:0031`, `tree1:topic:0032`, `tree1:topic:0033`, `tree1:topic:0034`, `tree1:topic:0035`, `tree1:topic:0036`, `tree1:topic:0037`, `tree1:topic:0038`, `tree1:topic:0039`, `tree1:topic:0040`, `tree1:topic:0041`, `tree1:topic:0042` | — | 0030覆盖完成，0031-0033覆盖中止，0034-0035覆盖失败终止，0036-0038覆盖暂停/恢复，0039-0042覆盖人工升级、交接、等待和返回继续。 |
| 定义 Agent 何时判定任务完成并进入正常完成状态，包括完成条件和完成后的输出语义。<br><code>completion_semantics</code> | covered | `tree1:topic:0030` | — | 0030直接覆盖正常完成判定、完成条件、状态转移与终态性、完成输出契约及与未完成/中止/失败的边界。 |
| 定义 Agent 何时中止执行，包括中止触发条件、状态转移和输出语义。<br><code>abort_semantics</code> | covered | `tree1:topic:0031`, `tree1:topic:0032`, `tree1:topic:0033` | — | 0031覆盖中止触发条件与原因分类，0032覆盖中止状态转移与终态竞态裁决，0033覆盖中止输出契约与输出边界。 |
| 定义 Agent 何时因不可恢复失败而终止，包括失败终止触发条件、状态转移和输出语义。<br><code>failure_termination_semantics</code> | covered | `tree1:topic:0034`, `tree1:topic:0035` | — | 0034覆盖不可恢复失败类别与失败终止触发判定，0035覆盖失败终止状态转移、输出契约与终态边界。 |
| 定义 Agent 何时暂停执行、暂停期间的状态保持以及如何恢复执行。<br><code>pause_resume_semantics</code> | covered | `tree1:topic:0036`, `tree1:topic:0037`, `tree1:topic:0038` | — | 0036覆盖暂停触发条件，0037覆盖暂停状态保持要求，0038覆盖恢复条件、续执行点和恢复后状态转移。 |
| 定义 Agent 何时触发人工升级，以及向人工交接、等待、返回和继续执行的控制语义。<br><code>human_escalation_semantics</code> | covered | `tree1:topic:0039`, `tree1:topic:0040`, `tree1:topic:0041`, `tree1:topic:0042` | — | 0039覆盖人工升级控制状态与触发条件，0040覆盖交接内容与格式，0041覆盖等待与超时处理，0042覆盖人工返回处理和继续执行。 |
| 集成各子规范形成完整决策循环状态机/控制流规范，执行跨子规范一致性检查、综合判断与冲突修复。<br><code>integration_consistency</code> | covered | `tree1:topic:0043`, `tree1:topic:0044`, `tree1:topic:0045`, `tree1:topic:0046`, `tree1:topic:0047`, `tree1:topic:0048`, `tree1:topic:0049` | — | 0043覆盖集成模型与子规范引用，0044-0046覆盖一致性检查、冲突分类和审计追溯，0047-0048覆盖冲突修复与取舍，0049覆盖架构组件约束。 |
| 将状态模型、目标/规划、行动/观察/契约和终止/升级等子规范集成为完整决策循环状态机/控制流规范。<br><code>integrate_control_flow_spec</code> | covered | `tree1:topic:0043`, `tree1:topic:0049` | — | 0043直接覆盖控制流集成模型、子规范边界、组合方式、挂接位置和引用关系；0049补充架构组件对控制流集成的约束。 |
| 对集成后的控制流规范执行跨子规范一致性检查，识别状态、转移、契约和语义之间的冲突与未定义引用。<br><code>cross_spec_consistency_check</code> | covered | `tree1:topic:0044`, `tree1:topic:0045`, `tree1:topic:0046` | — | 0044覆盖一致性检查要素与引用完整性，0045覆盖跨子规范冲突分类，0046覆盖检查记录与审计追溯模型。 |
| 针对一致性检查发现的冲突和未定义点，进行综合判断、冲突修复并形成最终取舍。<br><code>conflict_repair_tradeoffs</code> | covered | `tree1:topic:0047`, `tree1:topic:0048` | — | 0047覆盖冲突修复策略与未定义点填补原则，0048覆盖取舍优先级、权衡维度和未决风险标注准则。 |
| 使用正常路径、失败路径和边界场景走查或原型验证集成后的控制流，确认无未定义转移。<br><code>validation_walkthrough_prototype</code> | covered | `tree1:topic:0050`, `tree1:topic:0051`, `tree1:topic:0052`, `tree1:topic:0053`, `tree1:topic:0054`, `tree1:topic:0055`, `tree1:topic:0056`, `tree1:topic:0057` | — | 0050-0053覆盖验证场景分类及正常/失败/边界模式，0054覆盖组件交互控制流语义，0055覆盖未定义转移判定，0056覆盖走查/原型验证方法，0057覆盖证据字段与回修确认。 |

</details>

### 7.2 森林 1 → 任务树 2

结果：pass；covered 24，partial 0，missing 0。

<details>
<summary>展开逐任务检查</summary>

| 任务 | 状态 | 相关主题 ID | 缺口 | 判断依据 |
| --- | --- | --- | --- | --- |
| 设计 Agent 的决策循环与执行控制状态机，定义目标推进、规划/重规划、行动选择、观察吸收、终止/暂停/人工升级的可执行语义。<br><code>decision_loop_control_architecture</code> | covered | `tree1:topic:0001`, `tree1:topic:0002`, `tree1:topic:0003`, `tree1:topic:0004`, `tree1:topic:0006`, `tree1:topic:0007`, `tree1:topic:0010`, `tree1:topic:0011`, `tree1:topic:0012`, `tree1:topic:0013`, `tree1:topic:0016`, `tree1:topic:0017`, `tree1:topic:0018`, `tree1:topic:0019`, `tree1:topic:0020`, `tree1:topic:0024`, `tree1:topic:0027`, `tree1:topic:0030`, `tree1:topic:0031`, `tree1:topic:0032`, `tree1:topic:0034`, `tree1:topic:0035`, `tree1:topic:0036`, `tree1:topic:0038`, `tree1:topic:0039`, `tree1:topic:0042`, `tree1:topic:0043`, `tree1:topic:0050`, `tree1:topic:0051`, `tree1:topic:0052`, `tree1:topic:0053`, `tree1:topic:0055`, `tree1:topic:0056`, `tree1:topic:0057` | — | 森林以状态机形式语义、状态/转移/不变量、目标规划、行动观察、终止暂停升级、组件交互、集成一致性和验证场景为主题群，范围覆盖根任务所需的控制流入口；排除项主要排除存储实现、工具网关、重试细节等非本任务核心内容。 |
| 定义决策循环的状态集合、状态职责与循环不变量。<br><code>state_model</code> | covered | `tree1:topic:0001`, `tree1:topic:0002`, `tree1:topic:0004`, `tree1:topic:0005` | — | 0002 覆盖状态集合、状态职责、进入/退出含义和状态完整性/不重叠准则；0004 与 0005 覆盖循环不变量、检查与违反处理语义；0001 提供状态机形式语义基础。 |
| 定义状态转移条件、事件映射以及状态/转移的输入输出契约。<br><code>transition_contracts</code> | covered | `tree1:topic:0001`, `tree1:topic:0002`, `tree1:topic:0003`, `tree1:topic:0044` | — | 0003 覆盖转移规格、转移表字段、触发事件/条件、守卫、动作与副作用；0002 提供状态运行变量与表示；0001 提供形式语义；0044 提供接口契约与引用完整性检查维度。 |
| 定义状态转移条件与事件映射。<br><code>transition_guards_events</code> | covered | `tree1:topic:0001`, `tree1:topic:0003`, `tree1:topic:0055` | — | 0003 覆盖转移表、触发事件、守卫表达式、冲突优先级与完整性检查；0055 覆盖未定义转移和转移完备性判定；0001 提供状态机形式语义。 |
| 定义决策循环状态和转移的内部输入输出契约。<br><code>io_contracts</code> | covered | `tree1:topic:0002`, `tree1:topic:0003`, `tree1:topic:0004`, `tree1:topic:0044` | — | 0003 覆盖转移表字段、守卫、动作与执行语义；0002 覆盖状态运行变量和抽象表示；0004 覆盖运行时良构性与一致性；0044 覆盖接口契约与引用完整性检查，可支持内部契约检查。 |
| 设计目标推进以及规划/重规划的可执行语义。<br><code>goal_planning_semantics</code> | covered | `tree1:topic:0007`, `tree1:topic:0008`, `tree1:topic:0010`, `tree1:topic:0011`, `tree1:topic:0012` | — | 0007/0008 覆盖目标输入解析与目标状态表示；0010 覆盖推进/停滞/达成判定；0011 覆盖规划与重规划触发；0012 覆盖重规划契约、结果合法性与控制衔接。 |
| 定义目标解析与目标推进判定语义。<br><code>goal_advancement</code> | covered | `tree1:topic:0007`, `tree1:topic:0008`, `tree1:topic:0010` | — | 0007 覆盖目标输入解析与规范化；0008 覆盖目标状态表示与可检查性；0010 覆盖目标进展、停滞与裁决及结论更新。 |
| 定义规划与重规划触发条件及其控制语义。<br><code>planning_replanning_triggers</code> | covered | `tree1:topic:0006`, `tree1:topic:0011`, `tree1:topic:0012` | — | 0011 覆盖首次规划与重规划触发、优先级和计划有效性判断；0012 覆盖重规划输入输出契约、结果合法性与目标一致性；0006 提供执行循环中继续、重试、终止等决策点语义。 |
| 设计行动选择与观察吸收的可执行控制语义。<br><code>action_observation_semantics</code> | covered | `tree1:topic:0013`, `tree1:topic:0014`, `tree1:topic:0015`, `tree1:topic:0016`, `tree1:topic:0017`, `tree1:topic:0018`, `tree1:topic:0019` | — | 0013/0014/0015/0016 覆盖候选行动、合法性、选择依据、输出契约与可追溯性；0017/0018/0019 覆盖观察输入解析、状态更新和异常观察处理。 |
| 定义行动选择与行动发出的控制契约。<br><code>action_selection</code> | covered | `tree1:topic:0013`, `tree1:topic:0014`, `tree1:topic:0015`, `tree1:topic:0016`, `tree1:topic:0025` | — | 0013 覆盖行动空间与候选生成；0014 覆盖合法性、不可行、禁止和需补充参数判定；0015 覆盖选择依据与优先级；0016 覆盖行动输出契约与可追溯性；0025 覆盖工具失败、拒绝与失败语义。 |
| 定义观察吸收与状态更新语义。<br><code>observation_absorption</code> | covered | `tree1:topic:0017`, `tree1:topic:0018`, `tree1:topic:0019` | — | 0017 覆盖观察输入、解析和吸收生命周期；0018 覆盖观察驱动状态更新规则；0019 覆盖吸收合法性校验与异常/不完整观察处理。 |
| 定义终止、中止、完成判定、暂停/恢复和人工升级语义。<br><code>termination_pause_escalation</code> | covered | `tree1:topic:0030`, `tree1:topic:0031`, `tree1:topic:0032`, `tree1:topic:0033`, `tree1:topic:0034`, `tree1:topic:0035`, `tree1:topic:0036`, `tree1:topic:0037`, `tree1:topic:0038`, `tree1:topic:0039`, `tree1:topic:0040`, `tree1:topic:0041`, `tree1:topic:0042` | — | 0030-0035 覆盖完成、中止、失败终止的判定、状态转移、输出与终态边界；0036-0038 覆盖暂停触发、状态保持和恢复；0039-0042 覆盖人工升级触发、交接内容、等待超时、返回处理与继续执行。 |
| 定义完成、中止与终止的判定条件和差异。<br><code>completion_abort_termination</code> | covered | `tree1:topic:0030`, `tree1:topic:0031`, `tree1:topic:0032`, `tree1:topic:0034`, `tree1:topic:0035` | — | 0030 覆盖正常完成判定、终态性和完成输出；0031/0032 覆盖中止触发、原因分类、中止状态转移与终态竞态；0034/0035 覆盖失败终止触发、状态转移和终态边界。 |
| 定义暂停/恢复与人工升级语义。<br><code>pause_escalation</code> | covered | `tree1:topic:0036`, `tree1:topic:0037`, `tree1:topic:0038`, `tree1:topic:0039`, `tree1:topic:0040`, `tree1:topic:0041`, `tree1:topic:0042` | — | 0036-0038 覆盖暂停触发、暂停状态保持、恢复条件与续执行；0039-0042 覆盖人工升级控制状态、触发、交接内容、等待超时、人工返回与继续执行。 |
| 定义决策循环与上下文、工具和策略组件的交互约束。<br><code>component_interaction_constraints</code> | covered | `tree1:topic:0020`, `tree1:topic:0021`, `tree1:topic:0022`, `tree1:topic:0023`, `tree1:topic:0024`, `tree1:topic:0025`, `tree1:topic:0026`, `tree1:topic:0027`, `tree1:topic:0028`, `tree1:topic:0029`, `tree1:topic:0054` | — | 0020-0023 覆盖上下文组件职责、载荷、访问控制与运行状态一致性；0024-0026 覆盖工具交互契约、参数/返回/失败语义和权限边界；0027-0029 覆盖策略组件职责、查询接口、权限边界与拒绝控制流；0054 提供组件交互控制流语义。 |
| 定义决策循环与上下文/记忆组件的交互约束。<br><code>context_interaction</code> | covered | `tree1:topic:0020`, `tree1:topic:0021`, `tree1:topic:0022`, `tree1:topic:0023` | — | 0020 覆盖上下文接口契约、读写语义、调用时序、重试顺序与事务边界；0021 覆盖载荷数据模型和消息格式；0022 覆盖访问控制与越权处理；0023 覆盖上下文与运行状态一致性及版本协调。 |
| 定义决策循环与工具调用组件的交互约束。<br><code>tool_interaction</code> | covered | `tree1:topic:0024`, `tree1:topic:0025`, `tree1:topic:0026` | — | 0024 覆盖工具交互契约整体模型、请求/响应、时序、超时、并发、取消和结果回传；0025 覆盖工具参数/返回格式、失败分类和拒绝语义；0026 覆盖工具权限边界契约，且不进入权限矩阵/沙箱实现。 |
| 定义决策循环与策略/权限/审计组件的交互约束。<br><code>policy_interaction</code> | covered | `tree1:topic:0027`, `tree1:topic:0028`, `tree1:topic:0029`, `tree1:topic:0046` | — | 0027 覆盖策略组件职责与交互定位；0028 覆盖策略查询决策接口、数据格式与调用时序；0029 覆盖权限边界、决策结果、拒绝/不确定/超时控制流与审计语义；0046 提供一致性审计追溯模型。 |
| 综合各子任务形成统一的决策循环控制流规范，并解决跨子任务冲突。<br><code>integrate_control_flow</code> | covered | `tree1:topic:0043`, `tree1:topic:0044`, `tree1:topic:0045`, `tree1:topic:0046`, `tree1:topic:0047`, `tree1:topic:0048`, `tree1:topic:0049` | — | 0043 覆盖集成模型与子规范引用；0044 覆盖一致性检查要素与引用完整性；0045 覆盖冲突分类；0046 覆盖审计追溯；0047 覆盖冲突修复与未定义点填补；0048 覆盖取舍优先级与未决风险；0049 覆盖架构组件对控制流集成的约束。 |
| 整合各子任务输出，形成统一的决策循环控制流规范。<br><code>assemble_spec</code> | covered | `tree1:topic:0043`, `tree1:topic:0044` | — | 0043 覆盖子规范边界、组合方式、挂接位置、共享/局部元素、接口面与命名/引用机制；0044 覆盖命名元素跨子规范的定义-解析关系、唯一性、可解析性和引用完整性。 |
| 执行跨子任务一致性检查并修复冲突，记录最终取舍。<br><code>consistency_conflict_resolution</code> | covered | `tree1:topic:0044`, `tree1:topic:0045`, `tree1:topic:0046`, `tree1:topic:0047`, `tree1:topic:0048` | — | 0044 覆盖一致性检查维度与引用完整性；0045 覆盖术语、接口、行为、状态、权限、职责、命名、端点、守卫等冲突分类；0046 覆盖审计追溯；0047 覆盖修复策略与未定义点填补；0048 覆盖取舍优先级与未决风险标注。 |
| 用正常路径、失败路径和边界场景走查或原型验证控制流，并修复发现的问题。<br><code>validate_control_flow</code> | covered | `tree1:topic:0050`, `tree1:topic:0051`, `tree1:topic:0052`, `tree1:topic:0053`, `tree1:topic:0055`, `tree1:topic:0056`, `tree1:topic:0057` | — | 0050 覆盖场景分类与覆盖框架；0051-0053 覆盖正常、失败、边界场景模式；0055 覆盖未定义转移判定；0056 覆盖走查/原型验证方法与回修确认；0057 覆盖检查证据与覆盖判据。 |
| 执行正常路径、失败路径和边界场景的控制流走查或原型验证。<br><code>walkthrough_prototype_validation</code> | covered | `tree1:topic:0050`, `tree1:topic:0051`, `tree1:topic:0052`, `tree1:topic:0053`, `tree1:topic:0055`, `tree1:topic:0056`, `tree1:topic:0057` | — | 0050-0053 提供场景分类、覆盖框架及正常/失败/边界场景模式；0055 提供未定义转移与转移完备性判定；0056 提供走查/原型验证操作方法和问题记录；0057 提供证据字段、覆盖判据和回修确认记录。 |
| 修复验证发现的控制流问题并复验。<br><code>validation_fix_recheck</code> | covered | `tree1:topic:0047`, `tree1:topic:0048`, `tree1:topic:0050`, `tree1:topic:0051`, `tree1:topic:0052`, `tree1:topic:0053`, `tree1:topic:0055`, `tree1:topic:0056`, `tree1:topic:0057` | — | 0047 覆盖冲突修复策略与未定义点填补；0048 覆盖取舍优先级和未决风险标注；0050-0053 提供复验场景模式；0055 提供未定义转移判定；0056 覆盖回修确认与复验方法；0057 覆盖回修确认证据与覆盖判据。 |

</details>

### 7.3 森林 1 → 任务树 3

结果：partial；covered 21，partial 5，missing 0。

<details>
<summary>展开逐任务检查</summary>

| 任务 | 状态 | 相关主题 ID | 缺口 | 判断依据 |
| --- | --- | --- | --- | --- |
| 设计 Agent 的决策循环与执行控制状态机，定义目标推进、规划/重规划、行动选择、观察吸收、终止/暂停/人工升级的可执行语义。<br><code>decision_loop_control_architecture</code> | partial | `tree1:topic:0001`, `tree1:topic:0002`, `tree1:topic:0003`, `tree1:topic:0004`, `tree1:topic:0006`, `tree1:topic:0007`, `tree1:topic:0010`, `tree1:topic:0011`, `tree1:topic:0012`, `tree1:topic:0013`, `tree1:topic:0014`, `tree1:topic:0015`, `tree1:topic:0016`, `tree1:topic:0017`, `tree1:topic:0018`, `tree1:topic:0019`, `tree1:topic:0020`, `tree1:topic:0024`, `tree1:topic:0027`, `tree1:topic:0029`, `tree1:topic:0030`, `tree1:topic:0031`, `tree1:topic:0032`, `tree1:topic:0034`, `tree1:topic:0035`, `tree1:topic:0036`, `tree1:topic:0039`, `tree1:topic:0040`, `tree1:topic:0042`, `tree1:topic:0043`, `tree1:topic:0045`, `tree1:topic:0047`, `tree1:topic:0050`, `tree1:topic:0051`, `tree1:topic:0052`, `tree1:topic:0053`, `tree1:topic:0055`, `tree1:topic:0056`, `tree1:topic:0057` | - 状态与关键转移的输入输出契约（所需输入、允许输出、前置条件与后置效果）<br>- 计划生成/选择规则与选择标准 | 森林覆盖了状态、转移、不变量、目标推进、行动/观察、终止/暂停/升级、集成与验证等多数类别，但缺少独立的状态/转移输入输出契约入口，且规划主题主要覆盖触发与重规划约束，未明确覆盖计划生成/选择语义。 |
| 定义决策循环的状态集合、合法转移条件与循环不变量。<br><code>state_transition_invariant_model</code> | covered | `tree1:topic:0001`, `tree1:topic:0002`, `tree1:topic:0003`, `tree1:topic:0004`, `tree1:topic:0005` | — | 主题 0001 提供状态机形式语义，0002 覆盖状态集合与职责，0003 覆盖转移规格与守卫，0004/0005 覆盖循环不变量及检查条目；排除边界仅涉及具体实现、算法与验证，不影响这些知识入口。 |
| 枚举决策循环的状态并定义每个状态的职责与边界。<br><code>decision_loop_states_and_responsibilities</code> | covered | `tree1:topic:0002` | — | 主题 0002 明确覆盖决策循环状态集合建模、状态命名、核心语义、进入/退出含义、允许活动、完整性与职责不重叠准则，且排除转移条件、不变量和规划算法，符合任务边界。 |
| 定义决策循环中状态间的合法转移关系、转移条件与守卫。<br><code>transition_conditions_and_guards</code> | covered | `tree1:topic:0003` | — | 主题 0003 覆盖转移表字段、源/目标状态引用约束、触发事件与条件、守卫表达式、转移动作与副作用、冲突优先级及完整性检查，正好对应本任务且不越过状态职责或算法实现。 |
| 定义决策循环中必须始终成立的循环不变量。<br><code>loop_invariants</code> | covered | `tree1:topic:0004`, `tree1:topic:0005` | — | 主题 0004 覆盖跨迭代控制流不变量及运行时良构性，0005 覆盖不变量规范条目、检查机制与违反分类；二者共同提供定义与检查不变量的知识入口。 |
| 定义状态与关键转移的输入输出契约，以及与上下文、工具和策略组件的交互约束。<br><code>state_io_component_interaction_contracts</code> | partial | `tree1:topic:0001`, `tree1:topic:0002`, `tree1:topic:0003`, `tree1:topic:0020`, `tree1:topic:0024`, `tree1:topic:0027`, `tree1:topic:0029` | - 状态与关键转移的输入输出契约字段（所需输入、允许输出）<br>- 状态/转移的前置条件与后置效果建模<br>- 基于契约检查控制流接口完整性的规则 | 上下文、工具和策略组件交互约束分别由 0020、0024、0027/0029 覆盖；状态与转移的形式/行为语义由 0001/0002/0003 支持，但森林没有明确提供状态与关键转移的输入输出契约、前置/后置效果及接口完整性检查这一知识入口。 |
| 定义每个状态与关键转移的输入输出契约。<br><code>state_transition_io_contracts</code> | partial | `tree1:topic:0001`, `tree1:topic:0002`, `tree1:topic:0003` | - 状态与关键转移的输入输出契约字段（所需输入、允许输出）<br>- 状态/转移的前置条件与后置效果建模<br>- 基于契约检查控制流接口完整性的规则 | 0002 覆盖状态职责与进入/退出语义，0003 覆盖转移表、守卫、动作与副作用，0001 提供形式语义；但均未明确覆盖每个状态/关键转移的输入输出契约、前置/后置效果及接口完整性检查。 |
| 定义决策循环与上下文、工具和策略组件交互时必须满足的约束。<br><code>component_interaction_constraints</code> | covered | `tree1:topic:0020`, `tree1:topic:0024`, `tree1:topic:0027`, `tree1:topic:0028`, `tree1:topic:0029` | — | 0020 覆盖上下文组件职责与接口契约，0024 覆盖工具组件交互契约，0027/0028/0029 覆盖策略组件职责、查询接口、权限边界与拒绝控制流；各主题排除存储、网关、权限矩阵等实现细节，符合任务边界。 |
| 定义目标解析、目标推进判定、计划生成/选择以及重规划触发条件与语义。<br><code>goal_planning_replanning_semantics</code> | partial | `tree1:topic:0006`, `tree1:topic:0007`, `tree1:topic:0008`, `tree1:topic:0009`, `tree1:topic:0010`, `tree1:topic:0011`, `tree1:topic:0012` | - 计划生成/选择规则与选择标准<br>- 重规划期间控制状态与退出条件语义 | 0007-0010 覆盖目标解析、目标表示、解析异常与推进/停滞判定，0011/0012 覆盖规划/重规划触发条件与重规划约束；但森林主要排除或未明确覆盖计划生成/选择语义，也未明确提供重规划期间控制状态与退出条件。 |
| 定义目标解析和目标推进判定语义。<br><code>goal_parsing_progress_evaluation</code> | covered | `tree1:topic:0007`, `tree1:topic:0008`, `tree1:topic:0009`, `tree1:topic:0010` | — | 0007 覆盖目标输入解析与规范化，0008 覆盖目标状态表示与可检查性，0009 覆盖解析异常与未决状态，0010 覆盖目标推进/停滞判定、观察窗口与证据冲突；未依赖规划或行动选择主题。 |
| 定义计划生成/选择以及重规划触发条件与重规划状态语义。<br><code>planning_replanning_trigger_semantics</code> | partial | `tree1:topic:0006`, `tree1:topic:0011`, `tree1:topic:0012` | - 计划生成/选择规则与选择标准<br>- 重规划期间控制状态与退出条件语义 | 0011 覆盖初始规划/重规划触发条件与计划生命周期，0012 覆盖重规划范围、约束、目标一致性与合法性，0006 提供执行循环决策点；但森林未明确提供计划生成/选择语义，也未明确重规划期间控制状态与退出条件。 |
| 定义行动选择、观察吸收以及控制权在步骤间的流转契约。<br><code>action_observation_control_flow</code> | covered | `tree1:topic:0006`, `tree1:topic:0013`, `tree1:topic:0014`, `tree1:topic:0015`, `tree1:topic:0016`, `tree1:topic:0017`, `tree1:topic:0018`, `tree1:topic:0019` | — | 0013-0016 覆盖候选行动、合法性、选择依据与输出契约，0017-0019 覆盖观察输入、解析、状态更新与异常处理，0006 提供循环决策点和继续/退出语义，足以覆盖行动/观察控制流。 |
| 定义行动选择语义。<br><code>action_selection_semantics</code> | covered | `tree1:topic:0013`, `tree1:topic:0014`, `tree1:topic:0015`, `tree1:topic:0016` | — | 0013 覆盖行动空间与候选行动生成，0014 覆盖合法性判定，0015 覆盖选择依据、优先级与效用比较，0016 覆盖输出契约与可追溯性；各主题排除实现算法细节但保留领域语义。 |
| 定义观察吸收以及吸收后的控制权流转语义。<br><code>observation_absorption_control_handoff</code> | covered | `tree1:topic:0006`, `tree1:topic:0017`, `tree1:topic:0018`, `tree1:topic:0019` | — | 0017 覆盖观察输入、解析与吸收生命周期，0018 覆盖观察驱动状态更新规则，0019 覆盖合法性校验与异常观察处理，0006 覆盖观察合并、继续与循环退出决策点；控制权流转可由这些主题组合支持。 |
| 定义完成、中止、终止、暂停和人工升级的判定条件与状态语义。<br><code>termination_pause_escalation_semantics</code> | covered | `tree1:topic:0030`, `tree1:topic:0031`, `tree1:topic:0032`, `tree1:topic:0033`, `tree1:topic:0034`, `tree1:topic:0035`, `tree1:topic:0036`, `tree1:topic:0037`, `tree1:topic:0038`, `tree1:topic:0039`, `tree1:topic:0040`, `tree1:topic:0041`, `tree1:topic:0042` | — | 0030-0035 覆盖完成、中止、失败终止的判定、状态转移与输出，0036-0038 覆盖暂停触发、状态保持与恢复，0039-0042 覆盖人工升级控制状态、交接、等待与返回恢复；整体覆盖本任务。 |
| 定义完成、中止和终止的判定条件与状态语义。<br><code>completion_abort_termination_criteria</code> | covered | `tree1:topic:0030`, `tree1:topic:0031`, `tree1:topic:0032`, `tree1:topic:0033`, `tree1:topic:0034`, `tree1:topic:0035` | — | 0030 覆盖正常完成判定与输出，0031/0032/0033 覆盖中止触发、状态转移、竞态裁决与输出，0034/0035 覆盖不可恢复失败终止的触发、状态转移、输出及终态边界；可区分完成、中止与失败终止。 |
| 定义暂停和人工升级的判定条件与交接语义。<br><code>pause_human_escalation_semantics</code> | covered | `tree1:topic:0036`, `tree1:topic:0037`, `tree1:topic:0038`, `tree1:topic:0039`, `tree1:topic:0040`, `tree1:topic:0041`, `tree1:topic:0042` | — | 0036-0038 覆盖暂停触发、暂停状态保持与恢复转移，0039 覆盖人工升级控制状态与触发条件，0040 覆盖交接内容与格式，0041 覆盖人工等待/超时，0042 覆盖人工返回与继续执行。 |
| 汇总并协调状态、契约、规划/重规划、行动/观察、终止与升级语义，形成统一的决策循环状态机或等价控制流规范，并解决跨子任务冲突。<br><code>control_flow_integration_consistency</code> | covered | `tree1:topic:0043`, `tree1:topic:0044`, `tree1:topic:0045`, `tree1:topic:0046`, `tree1:topic:0047`, `tree1:topic:0048` | — | 0043 覆盖集成模型与子规范引用，0044 覆盖一致性检查要素与引用完整性，0045 覆盖冲突分类，0046 覆盖审计追溯，0047/0048 覆盖修复策略、取舍优先级与未决风险标注；足以支持集成与冲突解决。 |
| 识别并解决状态、契约、规划/重规划、行动/观察、终止/升级语义之间的跨子任务冲突。<br><code>cross_subtask_conflict_resolution</code> | covered | `tree1:topic:0044`, `tree1:topic:0045`, `tree1:topic:0046`, `tree1:topic:0047`, `tree1:topic:0048` | — | 0045 提供跨子规范冲突分类，0044 提供一致性检查与引用完整性，0047/0048 提供修复策略、取舍优先级与未决风险标注，0046 提供审计追溯记录模型。 |
| 将状态、契约、规划/重规划、行动/观察、终止/升级语义整合为统一的决策循环状态机或等价控制流规范。<br><code>unified_control_flow_specification</code> | covered | `tree1:topic:0043`, `tree1:topic:0049`, `tree1:topic:0002`, `tree1:topic:0003`, `tree1:topic:0004`, `tree1:topic:0011`, `tree1:topic:0013`, `tree1:topic:0017`, `tree1:topic:0020`, `tree1:topic:0024`, `tree1:topic:0027`, `tree1:topic:0030`, `tree1:topic:0036`, `tree1:topic:0039` | — | 0043 直接覆盖集成模型、子规范挂接、共享/局部元素与引用组织，0049 提供架构组件约束，其余主题提供状态、转移、不变量、规划触发、行动/观察、契约、终止/暂停/升级等组成内容；可组合形成统一规范。 |
| 检查统一控制流规范是否覆盖决策循环所需的全部元素。<br><code>required_element_completeness_check</code> | covered | `tree1:topic:0043`, `tree1:topic:0044`, `tree1:topic:0046` | — | 0043 提供集成模型和子规范边界，0044 覆盖状态、转移、接口契约、行为语义的一致性检查要素及引用完整性，0046 提供检查项、发现、未定义引用、严重度与审计记录模型；可支持逐项完整性核对。 |
| 用正常路径、失败路径和边界场景走查或原型验证决策循环控制流无未定义转移。<br><code>control_flow_validation</code> | covered | `tree1:topic:0050`, `tree1:topic:0051`, `tree1:topic:0052`, `tree1:topic:0053`, `tree1:topic:0054`, `tree1:topic:0055`, `tree1:topic:0056`, `tree1:topic:0057` | — | 0050 覆盖场景分类与覆盖框架，0051-0053 覆盖正常/失败/边界场景模式，0054 覆盖组件交互控制流，0055 覆盖未定义转移判定，0056 覆盖走查/原型验证方法，0057 覆盖证据记录与覆盖判据。 |
| 用正常路径场景走查或原型验证控制流。<br><code>normal_path_validation</code> | covered | `tree1:topic:0050`, `tree1:topic:0051`, `tree1:topic:0056`, `tree1:topic:0057` | — | 0051 提供正常路径场景模式，0050 提供场景分类与覆盖框架，0056 提供走查/原型验证方法，0057 提供证据字段与覆盖判据；满足正常路径验证需求。 |
| 用失败路径场景走查或原型验证控制流。<br><code>failure_path_validation</code> | covered | `tree1:topic:0050`, `tree1:topic:0052`, `tree1:topic:0056`, `tree1:topic:0057`, `tree1:topic:0011`, `tree1:topic:0012` | — | 0052 提供工具/检索/环境/模型输出/超时/重试耗尽/升级或中止等失败路径模式，0050/0056/0057 提供场景框架、验证方法与证据判据，0011/0012 可支持规划失败与重规划相关分支设计。 |
| 用边界场景走查或原型验证控制流。<br><code>boundary_scenario_validation</code> | covered | `tree1:topic:0050`, `tree1:topic:0053`, `tree1:topic:0056`, `tree1:topic:0057`, `tree1:topic:0019`, `tree1:topic:0036`, `tree1:topic:0039` | — | 0053 覆盖最大步数/资源限制、畸形输入、上下文溢出、并发/取消、暂停恢复、持久化恢复等边界模式，0050/0056/0057 提供框架、方法与证据，0019/0036/0039 支持重复观察、暂停恢复与人工升级边界设计。 |
| 汇总各场景验证结果，识别并处理未定义转移。<br><code>undefined_transition_consolidation</code> | covered | `tree1:topic:0055`, `tree1:topic:0057`, `tree1:topic:0047`, `tree1:topic:0046` | — | 0055 定义未定义转移的判定标准与完备性概念，0057 提供场景、期望/实际转移、发现、覆盖判据与回修确认记录，0047 提供未定义点填补与修复策略，0046 提供审计追溯结构。 |

</details>

### 7.4 森林 2 → 任务树 1

结果：pass；covered 30，partial 0，missing 0。

<details>
<summary>展开逐任务检查</summary>

| 任务 | 状态 | 相关主题 ID | 缺口 | 判断依据 |
| --- | --- | --- | --- | --- |
| 设计 Agent 的决策循环与执行控制状态机，定义目标推进、规划/重规划、行动选择、观察吸收、终止/暂停/人工升级的可执行语义。<br><code>decision_loop_control_architecture</code> | covered | `tree2:topic:0001`, `tree2:topic:0002`, `tree2:topic:0003`, `tree2:topic:0004`, `tree2:topic:0005`, `tree2:topic:0006`, `tree2:topic:0007`, `tree2:topic:0008`, `tree2:topic:0009`, `tree2:topic:0010`, `tree2:topic:0011`, `tree2:topic:0012`, `tree2:topic:0013`, `tree2:topic:0014`, `tree2:topic:0015`, `tree2:topic:0016`, `tree2:topic:0017`, `tree2:topic:0018`, `tree2:topic:0019`, `tree2:topic:0020`, `tree2:topic:0021`, `tree2:topic:0022`, `tree2:topic:0023`, `tree2:topic:0024`, `tree2:topic:0025`, `tree2:topic:0026`, `tree2:topic:0027`, `tree2:topic:0028`, `tree2:topic:0029`, `tree2:topic:0030`, `tree2:topic:0031` | — | 森林覆盖总体控制结构、状态-转移-契约模型、目标/规划/行动/观察控制链、终止升级、外部组件交互、集成一致性与验证缺陷治理；排除项仅排除存储/工具实现/重试熔断/提示词等非本任务知识，不形成核心缺口。 |
| 设计决策循环的状态集合、转移条件与循环不变量，作为控制流规范的核心状态模型。<br><code>state_model</code> | covered | `tree2:topic:0002`, `tree2:topic:0003`, `tree2:topic:0004`, `tree2:topic:0005` | — | 0003 覆盖状态集合与不变量，0004 覆盖转移规则与守卫，0005 覆盖内部控制契约与前后置条件，0002 提供状态-转移-契约共同模型；排除项未涉及目标/行动/终止/验证等本任务之外内容。 |
| 定义决策循环的状态集合，并明确每个状态的语义、进入/退出含义以及状态内允许的活动。<br><code>state_set</code> | covered | `tree2:topic:0003` | — | 0003 明确覆盖状态集合、状态职责与边界、命名与粒度、进入/停留/退出语义、状态与阶段性控制目标映射；排除项排除转移条件、契约字段、外部交互和实现代码，符合本任务边界。 |
| 定义状态之间的转移条件、守卫判断和转移动作，确保每个转移有明确触发语义。<br><code>transitions</code> | covered | `tree2:topic:0004`, `tree2:topic:0005` | — | 0004 覆盖转移表、触发事件、守卫、非法/未定义转移入口和完整性检查；0005 覆盖转移契约、前置/后置条件与转移动作的内部控制语义。 |
| 定义决策循环的循环不变量和关键状态不变量，用于约束控制流执行与验证。<br><code>invariants</code> | covered | `tree2:topic:0003`, `tree2:topic:0004`, `tree2:topic:0005` | — | 0003 覆盖跨状态循环不变量和状态模型中的不变量检查含义；0005 提供可独立检查的内部控制约束；0004 提供非法/未定义转移的规则层处理入口，可承接违反处理语义。 |
| 设计目标解析、目标推进判定以及规划/重规划触发与范围语义。<br><code>goal_planning</code> | covered | `tree2:topic:0006`, `tree2:topic:0007`, `tree2:topic:0008`, `tree2:topic:0009` | — | 0007 覆盖目标解析与结构化目标表示，0008 覆盖进度/停滞/达成判定，0009 覆盖规划与重规划控制生命周期、触发、输入构成和结果吸收；0006 提供目标-规划控制链衔接。 |
| 定义目标解析、目标状态表示以及目标推进与停滞判定语义。<br><code>goal_parse_progress</code> | covered | `tree2:topic:0007`, `tree2:topic:0008` | — | 0007 覆盖目标解析与目标表示，0008 覆盖推进/停滞/达成判定及判定结果作为控制流输入的信号语义。 |
| 定义 Agent 如何解析输入目标并形成可用的目标状态表示。<br><code>goal_parse_representation</code> | covered | `tree2:topic:0007` | — | 0007 覆盖目标解析控制输入、结构化目标表示、完整性/一致性校验；解析异常可通过其完整性/一致性校验范围承接。 |
| 定义如何判定一次决策循环是否推进目标，以及何时判定目标停滞。<br><code>goal_progress_stagnation</code> | covered | `tree2:topic:0008` | — | 0008 直接覆盖推进、停滞、达成三类目标状态的证据模型、指标维度、比较基线、时间窗口、可检查判定条件及控制流信号语义。 |
| 定义规划/重规划触发条件以及重规划范围与约束语义。<br><code>planning_replanning</code> | covered | `tree2:topic:0009`, `tree2:topic:0007` | — | 0009 覆盖规划/重规划控制生命周期、触发、输入构成、结果吸收、版本切换与失败入口；0007 提供原目标约束和成功判据，可支撑一致性判断。 |
| 定义何时触发初始规划或重规划，包括触发事件、条件和优先级。<br><code>planning_replanning_triggers</code> | covered | `tree2:topic:0009` | — | 0009 覆盖首次规划触发、重规划触发源与计划失效条件、触发判定与仲裁、失败控制入口以及与状态/契约的衔接。 |
| 定义重规划时可调整的计划范围、约束以及与原目标的一致性要求。<br><code>replanning_scope_constraints</code> | covered | `tree2:topic:0009`, `tree2:topic:0007`, `tree2:topic:0005` | — | 0009 覆盖重规划输入构成、结果吸收与版本切换等控制语义；0007 提供原目标约束与成功判据以支撑一致性判断；0005 提供内部控制契约框架以组织输入输出契约。 |
| 设计行动选择、观察吸收以及决策循环与上下文、工具和策略组件的交互约束。<br><code>action_observation_contracts</code> | covered | `tree2:topic:0010`, `tree2:topic:0011`, `tree2:topic:0012`, `tree2:topic:0017`, `tree2:topic:0018`, `tree2:topic:0019`, `tree2:topic:0020` | — | 0010 覆盖行动选择语义与结果表示，0011 覆盖行动发出与执行异常控制契约，0012 覆盖观察吸收与状态更新，0017-0020 覆盖外部组件交互及上下文、工具、策略/权限/审计的具体控制边界。 |
| 定义 Agent 在给定状态和计划下如何选择行动，包括候选行动、选择依据和行动输出契约。<br><code>action_selection</code> | covered | `tree2:topic:0010`, `tree2:topic:0011` | — | 0010 覆盖行动选择依据、约束与资格、选择决定类型、行动表示与参数模式、选择结果语义边界；0011 补充行动发出请求/响应与执行异常控制契约。 |
| 定义 Agent 如何接收、解析观察并更新上下文、记忆状态或目标推进状态。<br><code>observation_absorption</code> | covered | `tree2:topic:0012` | — | 0012 直接覆盖观察输入模型与边界、吸收判定、可更新状态类别、状态更新操作与结果、控制流反馈及异常/不完整观察控制入口。 |
| 定义决策循环与上下文、工具和策略组件之间的输入输出契约、时序、权限边界和数据流约束。<br><code>component_contracts</code> | covered | `tree2:topic:0017`, `tree2:topic:0018`, `tree2:topic:0019`, `tree2:topic:0020` | — | 0017 覆盖外部组件交互总体约束，0018 覆盖上下文/记忆组件交互，0019 覆盖工具调用组件交互，0020 覆盖策略/权限/审计治理组件交互；均排除具体实现，符合任务边界。 |
| 定义决策循环与上下文组件之间的输入输出契约、时序、数据格式和权限边界。<br><code>context_component_contract</code> | covered | `tree2:topic:0018` | — | 0018 覆盖上下文/记忆组件的控制点与调用时序、读写控制边界、接口角色与状态语义、输入输出契约、同步与一致性不变量，以及不可用/过期/陈旧时的控制入口。 |
| 定义决策循环与工具组件之间的输入输出契约、调用时序、权限边界和失败/拒绝语义。<br><code>tool_component_contract</code> | covered | `tree2:topic:0019`, `tree2:topic:0020` | — | 0019 覆盖工具调用组件交互的控制权划分、请求/结果契约、责任边界及不可用/执行失败返回入口；0020 补充策略/权限治理约束。 |
| 定义决策循环与策略组件之间的输入输出契约、时序、权限边界和策略决策约束。<br><code>policy_component_contract</code> | covered | `tree2:topic:0020` | — | 0020 覆盖策略、权限、审计组件的治理交互模型、策略判断接入契约、权限约束检查入口、调用顺序、短路条件和结果依赖；排除策略语言/引擎算法等实现细节。 |
| 设计决策循环的完成判定、中止、失败终止、暂停/恢复和人工升级语义。<br><code>termination_escalation</code> | covered | `tree2:topic:0013`, `tree2:topic:0014`, `tree2:topic:0015`, `tree2:topic:0016` | — | 0013 覆盖运行结束与人工介入总体控制模型，0014 覆盖终态判定与优先级，0015 覆盖暂停/恢复，0016 覆盖人工升级与升级后控制；排除项仅排除重试/补偿/熔断详细策略等。 |
| 定义 Agent 何时判定任务完成并进入正常完成状态，包括完成条件和完成后的输出语义。<br><code>completion_semantics</code> | covered | `tree2:topic:0014`, `tree2:topic:0013` | — | 0014 覆盖 completed、aborted、terminated 的概念边界、完成/中止/终止判定条件、优先级、冲突消解和终态结果映射；0013 提供整体运行结束控制模型。 |
| 定义 Agent 何时中止执行，包括中止触发条件、状态转移和输出语义。<br><code>abort_semantics</code> | covered | `tree2:topic:0014`, `tree2:topic:0013` | — | 0014 覆盖 aborted 终态边界、中止判定条件、优先级、冲突消解和终态结果映射；0013 提供运行结束控制分类。 |
| 定义 Agent 何时因不可恢复失败而终止，包括失败终止触发条件、状态转移和输出语义。<br><code>failure_termination_semantics</code> | covered | `tree2:topic:0014`, `tree2:topic:0013` | — | 0014 覆盖 terminated 等终态概念边界、判定条件、优先级、冲突消解和终态映射；0013 提供运行结束与介入控制模型。 |
| 定义 Agent 何时暂停执行、暂停期间的状态保持以及如何恢复执行。<br><code>pause_resume_semantics</code> | covered | `tree2:topic:0015` | — | 0015 直接覆盖暂停触发来源、可暂停执行点、暂停期间控制权与状态保持、恢复前提、恢复入口、状态校验、恢复点选择及恢复后一致性约束。 |
| 定义 Agent 何时触发人工升级，以及向人工交接、等待、返回和继续执行的控制语义。<br><code>human_escalation_semantics</code> | covered | `tree2:topic:0016` | — | 0016 覆盖人工升级触发条件类型、来源与紧急度、交接内容类别、人工介入后的控制权安排、恢复自动执行、继续或终止控制路径；等待/超时可由人工介入后的控制权安排承接。 |
| 集成各子规范形成完整决策循环状态机/控制流规范，执行跨子规范一致性检查、综合判断与冲突修复。<br><code>integration_consistency</code> | covered | `tree2:topic:0021`, `tree2:topic:0022`, `tree2:topic:0023`, `tree2:topic:0024` | — | 0021 覆盖规范集成与一致性治理总体关系，0022 覆盖集成组织与引用完整性，0023 覆盖跨要素一致性维度与冲突类型，0024 覆盖冲突识别、消解和取舍记录模型。 |
| 将状态模型、目标/规划、行动/观察/契约和终止/升级等子规范集成为完整决策循环状态机/控制流规范。<br><code>integrate_control_flow_spec</code> | covered | `tree2:topic:0021`, `tree2:topic:0022`, `tree2:topic:0001`, `tree2:topic:0002`, `tree2:topic:0003`, `tree2:topic:0004`, `tree2:topic:0005`, `tree2:topic:0006`, `tree2:topic:0007`, `tree2:topic:0008`, `tree2:topic:0009`, `tree2:topic:0010`, `tree2:topic:0011`, `tree2:topic:0012`, `tree2:topic:0013`, `tree2:topic:0014`, `tree2:topic:0015`, `tree2:topic:0016`, `tree2:topic:0017`, `tree2:topic:0018`, `tree2:topic:0019`, `tree2:topic:0020` | — | 0021/0022 覆盖集成组织、单一事实源、引用完整性和未决接口占位；0001-0020 提供集成所需的状态、转移、契约、目标/规划、行动/观察及终止升级和组件边界知识入口。 |
| 对集成后的控制流规范执行跨子规范一致性检查，识别状态、转移、契约和语义之间的冲突与未定义引用。<br><code>cross_spec_consistency_check</code> | covered | `tree2:topic:0021`, `tree2:topic:0022`, `tree2:topic:0023`, `tree2:topic:0024` | — | 0022 覆盖引用有效性和未定义引用检查规则，0023 覆盖跨要素一致性维度和冲突类型/传播关系，0024 覆盖冲突识别线索与证据来源，0021 提供一致性治理框架。 |
| 针对一致性检查发现的冲突和未定义点，进行综合判断、冲突修复并形成最终取舍。<br><code>conflict_repair_tradeoffs</code> | covered | `tree2:topic:0024`, `tree2:topic:0021` | — | 0024 覆盖多子任务规范冲突类型、识别线索、影响范围分析、消解原则、优先级判断、修复模式和取舍记录模型；0021 提供规范一致性治理背景。 |
| 使用正常路径、失败路径和边界场景走查或原型验证集成后的控制流，确认无未定义转移。<br><code>validation_walkthrough_prototype</code> | covered | `tree2:topic:0025`, `tree2:topic:0026`, `tree2:topic:0027`, `tree2:topic:0028`, `tree2:topic:0029`, `tree2:topic:0030`, `tree2:topic:0031` | — | 0026 覆盖验证方法和场景覆盖，0027 覆盖正常/失败/边界控制流行为，0028 覆盖原型环境与运行轨迹，0029 覆盖缺陷/偏差识别，0030 覆盖缺陷修复模式，0031 覆盖复验与遗留风险；0025 提供验证与缺陷治理总体框架。 |

</details>

### 7.5 森林 2 → 任务树 2

结果：pass；covered 24，partial 0，missing 0。

<details>
<summary>展开逐任务检查</summary>

| 任务 | 状态 | 相关主题 ID | 缺口 | 判断依据 |
| --- | --- | --- | --- | --- |
| 设计 Agent 的决策循环与执行控制状态机，定义目标推进、规划/重规划、行动选择、观察吸收、终止/暂停/人工升级的可执行语义。<br><code>decision_loop_control_architecture</code> | covered | `tree2:topic:0001`, `tree2:topic:0002`, `tree2:topic:0003`, `tree2:topic:0004`, `tree2:topic:0005`, `tree2:topic:0006`, `tree2:topic:0007`, `tree2:topic:0008`, `tree2:topic:0009`, `tree2:topic:0010`, `tree2:topic:0011`, `tree2:topic:0012`, `tree2:topic:0013`, `tree2:topic:0014`, `tree2:topic:0015`, `tree2:topic:0016`, `tree2:topic:0017`, `tree2:topic:0018`, `tree2:topic:0019`, `tree2:topic:0020`, `tree2:topic:0021`, `tree2:topic:0022`, `tree2:topic:0023`, `tree2:topic:0024`, `tree2:topic:0025`, `tree2:topic:0026`, `tree2:topic:0027`, `tree2:topic:0028`, `tree2:topic:0029`, `tree2:topic:0030`, `tree2:topic:0031` | — | 森林覆盖总体控制结构、状态-转移-契约模型、目标-规划-行动-观察控制链、终止与人工介入、外部组件交互、规范集成一致性和验证缺陷治理，能够支持根任务所需的完整知识入口。 |
| 定义决策循环的状态集合、状态职责与循环不变量。<br><code>state_model</code> | covered | `tree2:topic:0003` | — | 主题 0003 明确覆盖状态集合、状态职责、进入/停留/退出语义、状态与阶段性控制目标映射以及循环不变量；其排除项仅涉及转移、契约和外部交互，不影响本任务。 |
| 定义状态转移条件、事件映射以及状态/转移的输入输出契约。<br><code>transition_contracts</code> | covered | `tree2:topic:0002`, `tree2:topic:0004`, `tree2:topic:0005` | — | 主题 0004 覆盖转移规则、事件守卫和非法转移入口，主题 0005 覆盖内部控制契约与前后置条件，主题 0002 提供状态-转移-契约之间的依赖与引用边界；组合满足本任务。 |
| 定义状态转移条件与事件映射。<br><code>transition_guards_events</code> | covered | `tree2:topic:0004` | — | 主题 0004 的范围包括合法转移组织、事件分类与映射、守卫条件建模、非法/未定义转移规则入口以及转移表覆盖与完整性检查，完整对应本任务。 |
| 定义决策循环状态和转移的内部输入输出契约。<br><code>io_contracts</code> | covered | `tree2:topic:0005` | — | 主题 0005 专门覆盖状态契约、转移契约、前置/后置条件、内部控制接口数据边界及可独立检查约束，且排除外部组件交互，符合本任务边界。 |
| 设计目标推进以及规划/重规划的可执行语义。<br><code>goal_planning_semantics</code> | covered | `tree2:topic:0007`, `tree2:topic:0008`, `tree2:topic:0009` | — | 主题 0007 覆盖目标解析与表示，主题 0008 覆盖目标进度判定与控制流传递，主题 0009 覆盖首次规划、重规划触发、结果吸收和失败控制入口；组合满足本任务。 |
| 定义目标解析与目标推进判定语义。<br><code>goal_advancement</code> | covered | `tree2:topic:0007`, `tree2:topic:0008` | — | 主题 0007 覆盖目标解析与结构化表示，主题 0008 覆盖推进/停滞/达成的证据模型、判定条件和控制流传递语义；共同满足本任务。 |
| 定义规划与重规划触发条件及其控制语义。<br><code>planning_replanning_triggers</code> | covered | `tree2:topic:0009` | — | 主题 0009 完整覆盖首次规划、重规划触发源、计划失效、触发仲裁、输入构成、结果吸收、版本切换、失败入口以及与状态/契约的衔接。 |
| 设计行动选择与观察吸收的可执行控制语义。<br><code>action_observation_semantics</code> | covered | `tree2:topic:0010`, `tree2:topic:0011`, `tree2:topic:0012` | — | 主题 0010 覆盖行动选择语义，主题 0011 覆盖行动发出与执行异常控制契约，主题 0012 覆盖观察吸收与状态更新机制；组合覆盖本任务所需知识入口。 |
| 定义行动选择与行动发出的控制契约。<br><code>action_selection</code> | covered | `tree2:topic:0010`, `tree2:topic:0011` | — | 主题 0010 覆盖选择依据、选择结果类型和行动表示，主题 0011 覆盖行动发出请求/响应契约及不可执行/结果不可用两类异常控制入口；组合满足本任务。 |
| 定义观察吸收与状态更新语义。<br><code>observation_absorption</code> | covered | `tree2:topic:0012` | — | 主题 0012 覆盖观察输入、吸收判定、可更新状态类别、状态更新操作、控制流反馈及异常/不完整观察入口，且排除存储/检索算法，符合本任务边界。 |
| 定义终止、中止、完成判定、暂停/恢复和人工升级语义。<br><code>termination_pause_escalation</code> | covered | `tree2:topic:0013`, `tree2:topic:0014`, `tree2:topic:0015`, `tree2:topic:0016` | — | 主题 0013 提供运行结束与人工介入的整体控制模型，主题 0014 覆盖终态判定与优先级，主题 0015 覆盖暂停/恢复，主题 0016 覆盖人工升级与升级后控制；组合满足本任务。 |
| 定义完成、中止与终止的判定条件和差异。<br><code>completion_abort_termination</code> | covered | `tree2:topic:0014` | — | 主题 0014 专门覆盖 completed、aborted、terminated 的概念边界、判定条件、优先级、冲突消解及终态与结束状态映射，符合本任务。 |
| 定义暂停/恢复与人工升级语义。<br><code>pause_escalation</code> | covered | `tree2:topic:0015`, `tree2:topic:0016` | — | 主题 0015 覆盖暂停/恢复控制语义，主题 0016 覆盖人工升级触发、交接内容与升级后控制；组合满足本任务。 |
| 定义决策循环与上下文、工具和策略组件的交互约束。<br><code>component_interaction_constraints</code> | covered | `tree2:topic:0017`, `tree2:topic:0018`, `tree2:topic:0019`, `tree2:topic:0020` | — | 主题 0017 覆盖外部组件交互总体约束，主题 0018、0019、0020 分别覆盖上下文/记忆、工具、策略/权限/审计组件交互约束；组合满足本任务。 |
| 定义决策循环与上下文/记忆组件的交互约束。<br><code>context_interaction</code> | covered | `tree2:topic:0018` | — | 主题 0018 覆盖决策循环与上下文/记忆组件的控制点、读写边界、接口契约、同步一致性不变量及不可用/过期/陈旧控制入口，且排除内部存储与检索算法，符合本任务。 |
| 定义决策循环与工具调用组件的交互约束。<br><code>tool_interaction</code> | covered | `tree2:topic:0019` | — | 主题 0019 覆盖决策循环与工具调用组件的控制权划分、请求/结果契约、行动选择与工具调用边界、组件职责及工具不可用/失败返回入口，符合本任务。 |
| 定义决策循环与策略/权限/审计组件的交互约束。<br><code>policy_interaction</code> | covered | `tree2:topic:0020` | — | 主题 0020 覆盖策略判断接入、权限检查入口、审计触发点与内容边界，以及策略-权限-审计的调用顺序、短路条件和一致性约束，符合本任务。 |
| 综合各子任务形成统一的决策循环控制流规范，并解决跨子任务冲突。<br><code>integrate_control_flow</code> | covered | `tree2:topic:0021`, `tree2:topic:0022`, `tree2:topic:0023`, `tree2:topic:0024` | — | 主题 0021 覆盖规范集成与一致性治理，主题 0022 覆盖集成组织与引用完整性，主题 0023 覆盖跨要素一致性约束，主题 0024 覆盖冲突识别、消解与取舍记录；组合满足本任务。 |
| 整合各子任务输出，形成统一的决策循环控制流规范。<br><code>assemble_spec</code> | covered | `tree2:topic:0021`, `tree2:topic:0022` | — | 主题 0022 覆盖规范要素组织、单一事实源、唯一标识、引用有效性和未决接口占位，主题 0021 提供集成治理框架；组合满足本任务。 |
| 执行跨子任务一致性检查并修复冲突，记录最终取舍。<br><code>consistency_conflict_resolution</code> | covered | `tree2:topic:0023`, `tree2:topic:0024` | — | 主题 0023 覆盖跨要素一致性约束与冲突传播关系，主题 0024 覆盖冲突识别、消解原则、修复模式和取舍记录模型；组合满足本任务。 |
| 用正常路径、失败路径和边界场景走查或原型验证控制流，并修复发现的问题。<br><code>validate_control_flow</code> | covered | `tree2:topic:0025`, `tree2:topic:0026`, `tree2:topic:0027`, `tree2:topic:0028`, `tree2:topic:0029`, `tree2:topic:0030`, `tree2:topic:0031` | — | 主题 0025 提供验证与缺陷治理总体框架，0026 覆盖方法与场景覆盖，0027 覆盖正常/失败/边界行为，0028 覆盖原型环境和轨迹，0029 覆盖缺陷识别，0030 覆盖修复模式，0031 覆盖复验与遗留风险；组合满足本任务。 |
| 执行正常路径、失败路径和边界场景的控制流走查或原型验证。<br><code>walkthrough_prototype_validation</code> | covered | `tree2:topic:0026`, `tree2:topic:0027`, `tree2:topic:0028`, `tree2:topic:0029` | — | 主题 0026 覆盖验证方法与场景覆盖，0027 覆盖正常/失败/边界控制流行为，0028 覆盖原型验证环境与运行轨迹，0029 覆盖缺陷与偏差识别；组合满足本任务。 |
| 修复验证发现的控制流问题并复验。<br><code>validation_fix_recheck</code> | covered | `tree2:topic:0030`, `tree2:topic:0031` | — | 主题 0030 覆盖控制流缺陷修复模式，主题 0031 覆盖修复后复验、未定义转移检查和遗留风险评估；组合满足本任务。 |

</details>

### 7.6 森林 2 → 任务树 3

结果：pass；covered 26，partial 0，missing 0。

<details>
<summary>展开逐任务检查</summary>

| 任务 | 状态 | 相关主题 ID | 缺口 | 判断依据 |
| --- | --- | --- | --- | --- |
| 设计 Agent 的决策循环与执行控制状态机，定义目标推进、规划/重规划、行动选择、观察吸收、终止/暂停/人工升级的可执行语义。<br><code>decision_loop_control_architecture</code> | covered | `tree2:topic:0001`, `tree2:topic:0002`, `tree2:topic:0003`, `tree2:topic:0004`, `tree2:topic:0005`, `tree2:topic:0006`, `tree2:topic:0007`, `tree2:topic:0008`, `tree2:topic:0009`, `tree2:topic:0010`, `tree2:topic:0011`, `tree2:topic:0012`, `tree2:topic:0013`, `tree2:topic:0014`, `tree2:topic:0015`, `tree2:topic:0016`, `tree2:topic:0017`, `tree2:topic:0018`, `tree2:topic:0019`, `tree2:topic:0020`, `tree2:topic:0021`, `tree2:topic:0022`, `tree2:topic:0023`, `tree2:topic:0024`, `tree2:topic:0025`, `tree2:topic:0026`, `tree2:topic:0027`, `tree2:topic:0028`, `tree2:topic:0029`, `tree2:topic:0030`, `tree2:topic:0031` | — | 森林从总体控制结构、状态-转移-契约模型、目标-规划-行动-观察链、终止与升级、外部组件交互，到规范集成和验证治理均有对应主题；各主题排除项主要排除实现算法、存储/网关/权限实现、提示词和重试熔断细节，与本任务边界一致。 |
| 定义决策循环的状态集合、合法转移条件与循环不变量。<br><code>state_transition_invariant_model</code> | covered | `tree2:topic:0002`, `tree2:topic:0003`, `tree2:topic:0004` | — | 0003覆盖状态集合、职责边界和循环不变量；0004覆盖合法转移、触发事件与守卫；0002提供状态-转移-契约的共同约束关系。排除项不要求输入输出契约、外部组件交互或终止升级完整语义。 |
| 枚举决策循环的状态并定义每个状态的职责与边界。<br><code>decision_loop_states_and_responsibilities</code> | covered | `tree2:topic:0003` | — | 0003的范围明确包含决策循环状态集合、状态职责与边界、命名与粒度、进入/停留/退出语义；且排除转移条件、契约字段和外部组件交互，正符合本任务范围。 |
| 定义决策循环中状态间的合法转移关系、转移条件与守卫。<br><code>transition_conditions_and_guards</code> | covered | `tree2:topic:0004` | — | 0004明确覆盖合法状态转移的源/目标、触发事件与守卫条件组织、事件到转移映射、守卫建模、非法/未定义转移规则入口及转移表完整性检查；其排除项为状态集合、字段级契约和验证执行，不构成本任务缺口。 |
| 定义决策循环中必须始终成立的循环不变量。<br><code>loop_invariants</code> | covered | `tree2:topic:0003` | — | 0003的范围包含跨状态循环不变量的定义与检查含义，并覆盖进入/停留/退出语义；本任务只需不变量陈述及分阶段含义，不需要转移守卫或输入输出契约。 |
| 定义状态与关键转移的输入输出契约，以及与上下文、工具和策略组件的交互约束。<br><code>state_io_component_interaction_contracts</code> | covered | `tree2:topic:0005`, `tree2:topic:0017`, `tree2:topic:0018`, `tree2:topic:0019`, `tree2:topic:0020` | — | 0005提供内部控制契约框架；0017提供外部组件交互总体约束；0018、0019、0020分别覆盖上下文、工具、策略/权限/审计组件交互边界。各主题均排除存储、网关、权限矩阵、沙箱和具体实现，符合任务排除边界。 |
| 定义每个状态与关键转移的输入输出契约。<br><code>state_transition_io_contracts</code> | covered | `tree2:topic:0005` | — | 0005的范围包含状态契约、转移契约、前置条件、后置条件和内部控制接口数据边界；其排除项仅排除转移事件映射表、外部组件交互和验证执行，不影响本任务。 |
| 定义决策循环与上下文、工具和策略组件交互时必须满足的约束。<br><code>component_interaction_constraints</code> | covered | `tree2:topic:0017`, `tree2:topic:0018`, `tree2:topic:0019`, `tree2:topic:0020` | — | 0017给出外部组件交互总体责任边界；0018、0019、0020分别细化上下文、工具、策略/权限/审计交互约束，并明确排除存储、网关、权限矩阵和沙箱实现，正符合本任务的不越界要求。 |
| 定义目标解析、目标推进判定、计划生成/选择以及重规划触发条件与语义。<br><code>goal_planning_replanning_semantics</code> | covered | `tree2:topic:0006`, `tree2:topic:0007`, `tree2:topic:0008`, `tree2:topic:0009` | — | 0006提供目标-规划-行动-观察控制链；0007覆盖目标解析与表示；0008覆盖目标推进判定；0009覆盖规划与重规划控制语义。排除项不涉及具体规划算法、行动选择和失败重试细节。 |
| 定义目标解析和目标推进判定语义。<br><code>goal_parsing_progress_evaluation</code> | covered | `tree2:topic:0007`, `tree2:topic:0008` | — | 0007覆盖目标解析与结构化表示；0008覆盖目标进度判定、可检查条件及其控制流信号语义。二者排除项分别排除进度判定和规划触发，组合后覆盖本任务且不越界。 |
| 定义计划生成/选择以及重规划触发条件与重规划状态语义。<br><code>planning_replanning_trigger_semantics</code> | covered | `tree2:topic:0009` | — | 0009明确覆盖从首次规划到重规划的控制生命周期，包括触发源、失效条件、触发判定与仲裁、输入构成、结果吸收、版本切换、失败控制入口及与状态/契约衔接；排除具体规划算法和失败重试策略，符合任务边界。 |
| 定义行动选择、观察吸收以及控制权在步骤间的流转契约。<br><code>action_observation_control_flow</code> | covered | `tree2:topic:0006`, `tree2:topic:0010`, `tree2:topic:0011`, `tree2:topic:0012` | — | 0006提供目标-规划-行动-观察控制链；0010覆盖行动选择语义与结果表示；0011覆盖行动发出/执行异常控制入口；0012覆盖观察吸收、状态更新及控制流反馈。排除项不要求工具权限矩阵、沙箱或失败重试详细策略。 |
| 定义行动选择语义。<br><code>action_selection_semantics</code> | covered | `tree2:topic:0010` | — | 0010的范围明确包含行动选择依据、选择决定类型、行动表示与参数模式，以及选择结果与后续行动发出的语义边界；其排除行动执行异常控制、观察吸收和工具权限矩阵实现，符合本任务边界。 |
| 定义观察吸收以及吸收后的控制权流转语义。<br><code>observation_absorption_control_handoff</code> | covered | `tree2:topic:0012` | — | 0012明确覆盖观察输入模型与边界、吸收判定、可更新状态类别、状态更新操作、反馈信号及异常/不完整观察控制入口；其排除行动选择、上下文存储算法和失败重试策略，与本任务排除项一致。 |
| 定义完成、中止、终止、暂停和人工升级的判定条件与状态语义。<br><code>termination_pause_escalation_semantics</code> | covered | `tree2:topic:0013`, `tree2:topic:0014`, `tree2:topic:0015`, `tree2:topic:0016` | — | 0013提供运行结束与人工介入总体控制模型；0014覆盖终态判定与优先级；0015覆盖暂停/恢复；0016覆盖人工升级与升级后控制。排除项仅排除具体业务工作流和失败重试策略，符合任务边界。 |
| 定义完成、中止和终止的判定条件与状态语义。<br><code>completion_abort_termination_criteria</code> | covered | `tree2:topic:0014` | — | 0014明确覆盖completed、aborted、terminated等终态概念边界、判定条件、优先级、冲突消解和终态映射；其排除暂停/恢复、人工升级和非终态转换细节，正好对应本任务范围。 |
| 定义暂停和人工升级的判定条件与交接语义。<br><code>pause_human_escalation_semantics</code> | covered | `tree2:topic:0015`, `tree2:topic:0016` | — | 0015覆盖暂停/恢复控制语义；0016覆盖人工升级触发、交接内容和升级后控制。二者排除完成/中止/终止判定和具体告警工单实现，符合任务排除边界。 |
| 汇总并协调状态、契约、规划/重规划、行动/观察、终止与升级语义，形成统一的决策循环状态机或等价控制流规范，并解决跨子任务冲突。<br><code>control_flow_integration_consistency</code> | covered | `tree2:topic:0021`, `tree2:topic:0022`, `tree2:topic:0023`, `tree2:topic:0024` | — | 0021覆盖规范集成与一致性治理；0022覆盖规范组织、引用完整性和占位约定；0023覆盖跨要素一致性约束；0024覆盖冲突识别、消解与取舍记录。排除项不涉及验证执行和子任务内部细节重设计，符合本任务整合范围。 |
| 识别并解决状态、契约、规划/重规划、行动/观察、终止/升级语义之间的跨子任务冲突。<br><code>cross_subtask_conflict_resolution</code> | covered | `tree2:topic:0021`, `tree2:topic:0023`, `tree2:topic:0024` | — | 0021提供一致性治理框架；0023覆盖跨要素一致性维度与冲突传播关系；0024覆盖冲突识别、消解原则、优先级、修复模式和取舍记录。排除项不涉及统一规范最终编排和验证执行。 |
| 将状态、契约、规划/重规划、行动/观察、终止/升级语义整合为统一的决策循环状态机或等价控制流规范。<br><code>unified_control_flow_specification</code> | covered | `tree2:topic:0001`, `tree2:topic:0021`, `tree2:topic:0022` | — | 0001提供统一控制流总体结构；0021提供规范集成治理；0022提供规范组织、单一事实源、引用完整性和占位约定。排除项不涉及验证执行和内部细节重新设计，足以支持统一规范编写。 |
| 检查统一控制流规范是否覆盖决策循环所需的全部元素。<br><code>required_element_completeness_check</code> | covered | `tree2:topic:0003`, `tree2:topic:0004`, `tree2:topic:0005`, `tree2:topic:0009`, `tree2:topic:0014`, `tree2:topic:0015`, `tree2:topic:0016`, `tree2:topic:0021`, `tree2:topic:0022` | — | 0003、0004、0005、0009、0014、0015、0016分别提供状态下、转移、契约、重规划、终态、暂停和升级等必备元素定义；0021/0022提供规范集成、引用完整性和未决占位约定，可支持逐项核对与缺口记录。 |
| 用正常路径、失败路径和边界场景走查或原型验证决策循环控制流无未定义转移。<br><code>control_flow_validation</code> | covered | `tree2:topic:0004`, `tree2:topic:0025`, `tree2:topic:0026`, `tree2:topic:0027`, `tree2:topic:0028`, `tree2:topic:0029`, `tree2:topic:0030`, `tree2:topic:0031` | — | 0004提供未定义转移规则层处理入口；0025-0031覆盖验证治理、方法与场景覆盖、正常/失败/边界控制流行为、原型轨迹、缺陷识别、修复模式和复验风险。排除项不涉及生产部署或重试熔断策略。 |
| 用正常路径场景走查或原型验证控制流。<br><code>normal_path_validation</code> | covered | `tree2:topic:0026`, `tree2:topic:0027`, `tree2:topic:0028` | — | 0026覆盖验证方法与正常场景覆盖判断；0027覆盖正常成功推进的控制流行为；0028覆盖原型环境、轨迹观测和结果记录语义。排除项不要求失败路径和边界验证执行。 |
| 用失败路径场景走查或原型验证控制流。<br><code>failure_path_validation</code> | covered | `tree2:topic:0009`, `tree2:topic:0011`, `tree2:topic:0012`, `tree2:topic:0026`, `tree2:topic:0027`, `tree2:topic:0028` | — | 0009提供重规划失败控制入口；0011提供行动执行异常入口；0012提供异常/不完整观察控制入口；0026/0027/0028提供失败场景验证方法、预期行为和轨迹记录。排除项不要求重试、补偿、熔断详细策略。 |
| 用边界场景走查或原型验证控制流。<br><code>boundary_scenario_validation</code> | covered | `tree2:topic:0009`, `tree2:topic:0012`, `tree2:topic:0014`, `tree2:topic:0015`, `tree2:topic:0016`, `tree2:topic:0026`, `tree2:topic:0027`, `tree2:topic:0028` | — | 0009覆盖空计划/重规划边界；0012覆盖重复观察与异常观察；0014/0015/0016覆盖终止、暂停恢复和人工升级边界；0026/0027/0028提供边界场景验证、预期行为和轨迹记录。排除项不涉及具体业务工作流实现。 |
| 汇总各场景验证结果，识别并处理未定义转移。<br><code>undefined_transition_consolidation</code> | covered | `tree2:topic:0004`, `tree2:topic:0029`, `tree2:topic:0030`, `tree2:topic:0031` | — | 0004提供未定义转移规则层处理入口；0029覆盖未定义转移等缺陷识别；0030覆盖修复模式；0031覆盖复验与遗留风险。排除项不要求单场景走查执行或跨子任务冲突修复，符合本任务汇总处理范围。 |

</details>

### 7.7 森林 3 → 任务树 1

结果：partial；covered 18，partial 12，missing 0。

<details>
<summary>展开逐任务检查</summary>

| 任务 | 状态 | 相关主题 ID | 缺口 | 判断依据 |
| --- | --- | --- | --- | --- |
| 设计 Agent 的决策循环与执行控制状态机，定义目标推进、规划/重规划、行动选择、观察吸收、终止/暂停/人工升级的可执行语义。<br><code>decision_loop_control_architecture</code> | partial | `tree3:topic:0001`, `tree3:topic:0002`, `tree3:topic:0003`, `tree3:topic:0004`, `tree3:topic:0005`, `tree3:topic:0006`, `tree3:topic:0007`, `tree3:topic:0008`, `tree3:topic:0009`, `tree3:topic:0010`, `tree3:topic:0011`, `tree3:topic:0012`, `tree3:topic:0013`, `tree3:topic:0014`, `tree3:topic:0015`, `tree3:topic:0016`, `tree3:topic:0017`, `tree3:topic:0018`, `tree3:topic:0019`, `tree3:topic:0020`, `tree3:topic:0021`, `tree3:topic:0022`, `tree3:topic:0023`, `tree3:topic:0024`, `tree3:topic:0025`, `tree3:topic:0026`, `tree3:topic:0027`, `tree3:topic:0028`, `tree3:topic:0029`, `tree3:topic:0030`, `tree3:topic:0031`, `tree3:topic:0032`, `tree3:topic:0033`, `tree3:topic:0034`, `tree3:topic:0035`, `tree3:topic:0036`, `tree3:topic:0037`, `tree3:topic:0038`, `tree3:topic:0039`, `tree3:topic:0040`, `tree3:topic:0041`, `tree3:topic:0042`, `tree3:topic:0043`, `tree3:topic:0044` | - 初始规划触发条件<br>- 重规划输入输出契约、可变更范围与原目标一致性约束<br>- 上下文与策略组件的接口、时序、数据格式和权限边界契约<br>- 转移动作与副作用的定义<br>- 不变量违反处理语义<br>- 目标解析异常处理语义 | 森林覆盖状态、转移、不变量、行动选择、观察、终止、暂停和验证的大部分入口，但若干子域只有边界约束或上位语义，缺少初始规划触发、重规划范围/契约、上下文与策略组件接口契约、转移动作和不变量违反处理等明确入口。 |
| 设计决策循环的状态集合、转移条件与循环不变量，作为控制流规范的核心状态模型。<br><code>state_model</code> | covered | `tree3:topic:0001`, `tree3:topic:0002`, `tree3:topic:0003`, `tree3:topic:0004`, `tree3:topic:0006` | — | 0001 覆盖状态集合与职责，0002 覆盖转移条件和守卫，0003/0004 覆盖不变量语义与类别，0006 覆盖接口完整性检查，足以支持该父任务要求。 |
| 定义决策循环的状态集合，并明确每个状态的语义、进入/退出含义以及状态内允许的活动。<br><code>state_set</code> | covered | `tree3:topic:0001` | — | 0001 的范围明确覆盖状态类型体系、核心与可选状态集合、状态语义、职责边界以及状态集合完整性和职责不重叠原则。 |
| 定义状态之间的转移条件、守卫判断和转移动作，确保每个转移有明确触发语义。<br><code>transitions</code> | partial | `tree3:topic:0002`, `tree3:topic:0005` | - 转移动作与副作用的明确定义或契约（包括执行归属和成功/失败反映） | 0002 覆盖转移关系、触发条件和守卫，0005 可支持关键转移的前置/后置效果声明，但森林缺少对转移动作本身及其副作用的明确主题入口。 |
| 定义决策循环的循环不变量和关键状态不变量，用于约束控制流执行与验证。<br><code>invariants</code> | partial | `tree3:topic:0003`, `tree3:topic:0004` | - 不变量违反时的检测、处置及与暂停/中止/升级/重规划衔接的语义 | 0003/0004 覆盖不变量语义、表达方式和具体类别约束，但未明确给出不变量违反后的处理语义。 |
| 设计目标解析、目标推进判定以及规划/重规划触发与范围语义。<br><code>goal_planning</code> | partial | `tree3:topic:0015`, `tree3:topic:0016`, `tree3:topic:0017`, `tree3:topic:0018`, `tree3:topic:0019`, `tree3:topic:0020`, `tree3:topic:0042` | - 初始规划触发条件<br>- 重规划输入输出契约与可变更范围约束<br>- 重规划结果与原目标一致性要求 | 0015/0016 覆盖目标解析和推进判定，0017/0018/0019/0020 覆盖计划表示、生成、监控和重规划触发，但初始规划触发和重规划范围/契约/原目标一致性缺少明确入口。 |
| 定义目标解析、目标状态表示以及目标推进与停滞判定语义。<br><code>goal_parse_progress</code> | covered | `tree3:topic:0015`, `tree3:topic:0016` | — | 0015 覆盖目标解析与可执行目标规格，0016 覆盖目标推进、停滞和受阻判定，足以支持该父任务。 |
| 定义 Agent 如何解析输入目标并形成可用的目标状态表示。<br><code>goal_parse_representation</code> | partial | `tree3:topic:0015` | - 目标解析异常的分类与处置语义 | 0015 覆盖意图/约束解析、模糊性与缺失信息识别、可执行目标结构和可执行性判定，但其范围未明确给出解析异常后的处置语义。 |
| 定义如何判定一次决策循环是否推进目标，以及何时判定目标停滞。<br><code>goal_progress_stagnation</code> | covered | `tree3:topic:0016` | — | 0016 的范围覆盖以可执行目标规格为基准的推进、停滞、受阻、失败或暂时未完成分类，以及判定基线、比较方法、证据和可观察结果。 |
| 定义规划/重规划触发条件以及重规划范围与约束语义。<br><code>planning_replanning</code> | partial | `tree3:topic:0017`, `tree3:topic:0018`, `tree3:topic:0019`, `tree3:topic:0020`, `tree3:topic:0042` | - 初始规划触发条件<br>- 重规划输入输出契约、可变更范围与约束<br>- 重规划结果与原目标一致性要求 | 0017/0018/0019/0020 覆盖计划表示、生成选择、执行监控和重规划触发/退出，但初始规划触发及重规划范围、契约、原目标一致性缺少明确入口。 |
| 定义何时触发初始规划或重规划，包括触发事件、条件和优先级。<br><code>planning_replanning_triggers</code> | partial | `tree3:topic:0018`, `tree3:topic:0020` | - 初始规划触发条件（从目标/上下文进入首次规划的事件、条件与优先级） | 0020 明确覆盖重规划触发条件、优先级和退出语义，0018 覆盖计划生成/选择，但未明确给出首次初始规划的触发条件与优先级。 |
| 定义重规划时可调整的计划范围、约束以及与原目标的一致性要求。<br><code>replanning_scope_constraints</code> | partial | `tree3:topic:0017`, `tree3:topic:0018`, `tree3:topic:0020`, `tree3:topic:0042` | - 重规划输入输出契约<br>- 重规划可变更范围<br>- 重规划约束条件<br>- 重规划结果与原目标一致性要求 | 现有主题覆盖计划建模、生成、重规划控制和跨子任务一致性，但未明确展开重规划输入输出契约、可变更范围、约束和原目标一致性判定。 |
| 设计行动选择、观察吸收以及决策循环与上下文、工具和策略组件的交互约束。<br><code>action_observation_contracts</code> | partial | `tree3:topic:0007`, `tree3:topic:0008`, `tree3:topic:0009`, `tree3:topic:0010`, `tree3:topic:0011`, `tree3:topic:0012`, `tree3:topic:0013`, `tree3:topic:0014`, `tree3:topic:0021`, `tree3:topic:0022`, `tree3:topic:0024`, `tree3:topic:0025` | - 上下文组件接口、调用时序、数据格式和权限边界<br>- 策略组件查询/决策接口、调用时序、数据格式、权限边界和拒绝语义 | 行动选择和观察吸收有较完整入口，工具组件契约由 0009/0024/0025 支持，但上下文和策略组件更多只有交互边界原则，缺少明确接口、时序、数据格式和权限边界契约。 |
| 定义 Agent 在给定状态和计划下如何选择行动，包括候选行动、选择依据和行动输出契约。<br><code>action_selection</code> | covered | `tree3:topic:0011`, `tree3:topic:0012`, `tree3:topic:0013`, `tree3:topic:0014` | — | 0011 提供行动选择整体语义，0012 覆盖行动表示和候选生成，0013 覆盖选择依据与可行性约束，0014 覆盖决策规则和结果输出契约。 |
| 定义 Agent 如何接收、解析观察并更新上下文、记忆状态或目标推进状态。<br><code>observation_absorption</code> | covered | `tree3:topic:0021`, `tree3:topic:0022` | — | 0021 覆盖观察接收、吸收判定、归一化、证据和不确定性，0022 覆盖观察驱动的控制状态更新与控制权移交。 |
| 定义决策循环与上下文、工具和策略组件之间的输入输出契约、时序、权限边界和数据流约束。<br><code>component_contracts</code> | partial | `tree3:topic:0007`, `tree3:topic:0008`, `tree3:topic:0009`, `tree3:topic:0010`, `tree3:topic:0024`, `tree3:topic:0025` | - 上下文组件读写接口、调用时序、数据格式和权限边界<br>- 策略组件查询/决策接口、调用时序、数据格式、权限边界和拒绝语义 | 工具组件契约可由 0009/0024/0025 及共享边界 0007 支持，但上下文和策略组件的主题主要提供交互约束原则，未明确覆盖接口、时序、数据格式和权限边界。 |
| 定义决策循环与上下文组件之间的输入输出契约、时序、数据格式和权限边界。<br><code>context_component_contract</code> | partial | `tree3:topic:0007`, `tree3:topic:0008`, `tree3:topic:0025` | - 上下文读写接口<br>- 上下文调用时序<br>- 上下文数据格式<br>- 上下文权限边界 | 0008 覆盖上下文交互约束原则，0007 覆盖共享权责边界，0025 提供一般调度边界，但没有明确给出上下文组件的读写接口、调用时序、数据格式和权限边界契约。 |
| 定义决策循环与工具组件之间的输入输出契约、调用时序、权限边界和失败/拒绝语义。<br><code>tool_component_contract</code> | covered | `tree3:topic:0007`, `tree3:topic:0009`, `tree3:topic:0024`, `tree3:topic:0025` | — | 0024 覆盖工具调用接口、参数/返回信封和错误/超时/取消表示，0009 覆盖工具边界、拒绝和失败处理归属，0025 覆盖调度与步骤依赖，0007 提供共享权责边界。 |
| 定义决策循环与策略组件之间的输入输出契约、时序、权限边界和策略决策约束。<br><code>policy_component_contract</code> | partial | `tree3:topic:0007`, `tree3:topic:0010`, `tree3:topic:0025` | - 策略查询/决策接口<br>- 策略输入输出格式<br>- 策略调用时序<br>- 策略权限边界 | 0010 覆盖决策循环与策略组件的交互约束、允许/拒绝/附加义务消费和不得绕过策略，但未明确给出策略查询接口、输入输出格式、调用时序和权限边界契约。 |
| 设计决策循环的完成判定、中止、失败终止、暂停/恢复和人工升级语义。<br><code>termination_escalation</code> | covered | `tree3:topic:0026`, `tree3:topic:0027`, `tree3:topic:0028`, `tree3:topic:0029`, `tree3:topic:0030`, `tree3:topic:0031`, `tree3:topic:0032`, `tree3:topic:0044` | — | 0026 提供终态框架，0027/0028/0029 覆盖完成、中止和终止，0030/0031/0032 覆盖暂停与人工升级控制模型、触发、保持和恢复，0044 补充失败分支控制。 |
| 定义 Agent 何时判定任务完成并进入正常完成状态，包括完成条件和完成后的输出语义。<br><code>completion_semantics</code> | covered | `tree3:topic:0027` | — | 0027 明确覆盖完成判定、验收/验证条件、完成目标状态、可观察结果以及完成与中止、终止的区别。 |
| 定义 Agent 何时中止执行，包括中止触发条件、状态转移和输出语义。<br><code>abort_semantics</code> | covered | `tree3:topic:0028` | — | 0028 覆盖中止判定条件、触发来源、中止目标状态、可观察结果，并明确区分非正常提前停止与正常完成。 |
| 定义 Agent 何时因不可恢复失败而终止，包括失败终止触发条件、状态转移和输出语义。<br><code>failure_termination_semantics</code> | covered | `tree3:topic:0026`, `tree3:topic:0029`, `tree3:topic:0044` | — | 0029 覆盖终止判定、触发来源、终止状态和可观察结果，0026 提供终态框架与目标状态映射，0044 补充失败分支的控制流和终止/继续关系。 |
| 定义 Agent 何时暂停执行、暂停期间的状态保持以及如何恢复执行。<br><code>pause_resume_semantics</code> | covered | `tree3:topic:0031` | — | 0031 明确覆盖暂停触发条件、暂停期间状态保持、未升级人工的暂停恢复条件、授权、重新校验、幂等续跑和恢复失败语义。 |
| 定义 Agent 何时触发人工升级，以及向人工交接、等待、返回和继续执行的控制语义。<br><code>human_escalation_semantics</code> | covered | `tree3:topic:0032` | — | 0032 覆盖人工升级触发条件、升级对象选择、权限/责任/升级链路、交接包语义、人工处置后的恢复与结束规则以及边界控制转移。 |
| 集成各子规范形成完整决策循环状态机/控制流规范，执行跨子规范一致性检查、综合判断与冲突修复。<br><code>integration_consistency</code> | covered | `tree3:topic:0006`, `tree3:topic:0041`, `tree3:topic:0042`, `tree3:topic:0043` | — | 0041 覆盖控制流规范元素完备性判据，0042 覆盖跨子任务一致性与冲突分类，0043 覆盖冲突取舍、修复与同步，0006 支持接口完整性检查。 |
| 将状态模型、目标/规划、行动/观察/契约和终止/升级等子规范集成为完整决策循环状态机/控制流规范。<br><code>integrate_control_flow_spec</code> | covered | `tree3:topic:0041`, `tree3:topic:0042` | — | 0041 提供必需元素完备性判据和元素间衔接一致性要求，0042 提供跨子任务依赖关系，足以支持集成完整控制流规范所需的领域入口。 |
| 对集成后的控制流规范执行跨子规范一致性检查，识别状态、转移、契约和语义之间的冲突与未定义引用。<br><code>cross_spec_consistency_check</code> | covered | `tree3:topic:0006`, `tree3:topic:0041`, `tree3:topic:0042` | — | 0042 覆盖跨子任务一致性维度与冲突分类，0041 覆盖元素覆盖和衔接检查，0006 支持接口完整性及缺失/冲突契约识别。 |
| 针对一致性检查发现的冲突和未定义点，进行综合判断、冲突修复并形成最终取舍。<br><code>conflict_repair_tradeoffs</code> | covered | `tree3:topic:0042`, `tree3:topic:0043` | — | 0043 明确覆盖冲突取舍准则、修复模式、冲突决策记录模型、未决问题标注以及修复后的依赖追踪、变更传播和一致性复核。 |
| 使用正常路径、失败路径和边界场景走查或原型验证集成后的控制流，确认无未定义转移。<br><code>validation_walkthrough_prototype</code> | covered | `tree3:topic:0036`, `tree3:topic:0037`, `tree3:topic:0038`, `tree3:topic:0039`, `tree3:topic:0040` | — | 0036 提供控制流验证方法框架，0037/0038/0039 分别覆盖正常、失败和边界场景验证，0040 覆盖未定义转移的识别、汇总与处置。 |

</details>

### 7.8 森林 3 → 任务树 2

结果：partial；covered 18，partial 6，missing 0。

<details>
<summary>展开逐任务检查</summary>

| 任务 | 状态 | 相关主题 ID | 缺口 | 判断依据 |
| --- | --- | --- | --- | --- |
| 设计 Agent 的决策循环与执行控制状态机，定义目标推进、规划/重规划、行动选择、观察吸收、终止/暂停/人工升级的可执行语义。<br><code>decision_loop_control_architecture</code> | partial | `tree3:topic:0001`, `tree3:topic:0002`, `tree3:topic:0003`, `tree3:topic:0004`, `tree3:topic:0005`, `tree3:topic:0006`, `tree3:topic:0007`, `tree3:topic:0008`, `tree3:topic:0009`, `tree3:topic:0010`, `tree3:topic:0011`, `tree3:topic:0012`, `tree3:topic:0013`, `tree3:topic:0014`, `tree3:topic:0015`, `tree3:topic:0016`, `tree3:topic:0017`, `tree3:topic:0018`, `tree3:topic:0019`, `tree3:topic:0020`, `tree3:topic:0021`, `tree3:topic:0022`, `tree3:topic:0023`, `tree3:topic:0024`, `tree3:topic:0025`, `tree3:topic:0026`, `tree3:topic:0027`, `tree3:topic:0028`, `tree3:topic:0029`, `tree3:topic:0030`, `tree3:topic:0031`, `tree3:topic:0032`, `tree3:topic:0036`, `tree3:topic:0037`, `tree3:topic:0038`, `tree3:topic:0039`, `tree3:topic:0040`, `tree3:topic:0041`, `tree3:topic:0042`, `tree3:topic:0043`, `tree3:topic:0044` | - 首次规划触发条件及其与目标解析、状态转移的衔接语义 | 森林覆盖了状态、转移、契约、不变量、重规划触发、行动/观察、终态、暂停/升级、组件交互与验证等大部分入口；但规划触发部分只有重规划触发，缺少首次规划触发的明确主题入口。 |
| 定义决策循环的状态集合、状态职责与循环不变量。<br><code>state_model</code> | covered | `tree3:topic:0001`, `tree3:topic:0003`, `tree3:topic:0004` | — | topic0001 覆盖状态类型、状态职责与边界；topic0003 覆盖不变量语义与保持条件；topic0004 覆盖不变量类别与具体约束。排除项只排除转移条件、IO 契约和外部交互，不影响本任务。 |
| 定义状态转移条件、事件映射以及状态/转移的输入输出契约。<br><code>transition_contracts</code> | covered | `tree3:topic:0002`, `tree3:topic:0005`, `tree3:topic:0006` | — | topic0002 覆盖转移关系、触发事件与守卫；topic0005 覆盖状态/转移的 IO 契约表达；topic0006 覆盖控制流接口完整性检查。排除项不要求外部组件交互或具体实现。 |
| 定义状态转移条件与事件映射。<br><code>transition_guards_events</code> | covered | `tree3:topic:0002`, `tree3:topic:0040` | — | topic0002 覆盖合法转移、触发事件、守卫谓词和禁止转移；topic0040 覆盖未定义转移的识别、分类与处置入口。二者组合满足本任务。 |
| 定义决策循环状态和转移的内部输入输出契约。<br><code>io_contracts</code> | covered | `tree3:topic:0005`, `tree3:topic:0006` | — | topic0005 提供状态/转移契约字段、前置/后置条件与组合关系；topic0006 提供契约要素覆盖和衔接完整性检查。topic0005 已明确排除外部组件交互，符合任务边界。 |
| 设计目标推进以及规划/重规划的可执行语义。<br><code>goal_planning_semantics</code> | partial | `tree3:topic:0015`, `tree3:topic:0016`, `tree3:topic:0017`, `tree3:topic:0018`, `tree3:topic:0019`, `tree3:topic:0020`, `tree3:topic:0044` | - 首次规划触发条件及其与目标解析、状态转移的衔接语义 | topic0015/topic0016 覆盖目标解析与推进/受阻判定；topic0017/topic0018 覆盖计划表示与生成选择；topic0019/topic0020 覆盖执行监控与重规划触发、控制状态和退出；topic0044 覆盖规划失败分支。但缺少首次规划触发的明确知识入口。 |
| 定义目标解析与目标推进判定语义。<br><code>goal_advancement</code> | covered | `tree3:topic:0015`, `tree3:topic:0016`, `tree3:topic:0022` | — | topic0015 覆盖目标解析和可执行目标规格；topic0016 覆盖推进、停滞、受阻等判定及可观察结果；topic0022 覆盖观察驱动的控制状态更新和控制权移交。排除项不要求规划触发或终止升级语义。 |
| 定义规划与重规划触发条件及其控制语义。<br><code>planning_replanning_triggers</code> | partial | `tree3:topic:0017`, `tree3:topic:0018`, `tree3:topic:0019`, `tree3:topic:0020`, `tree3:topic:0044` | - 首次规划触发条件及其与目标解析、状态转移的衔接 | topic0020 覆盖重规划触发条件、控制状态、退出条件与失败分类；topic0019 覆盖偏差检测；topic0018 覆盖计划生成；topic0044 覆盖规划失败分支。但森林没有明确覆盖首次规划触发条件。 |
| 设计行动选择与观察吸收的可执行控制语义。<br><code>action_observation_semantics</code> | covered | `tree3:topic:0011`, `tree3:topic:0012`, `tree3:topic:0013`, `tree3:topic:0014`, `tree3:topic:0021`, `tree3:topic:0022`, `tree3:topic:0023`, `tree3:topic:0024`, `tree3:topic:0044` | — | topic0011-topic0014 覆盖行动表示、候选生成、选择依据和决策输出；topic0023/topic0024 覆盖行动执行和工具调用契约；topic0021/topic0022 覆盖观察吸收与控制状态更新；topic0044 覆盖失败分支。排除项避免深入工具基础设施和重试策略。 |
| 定义行动选择与行动发出的控制契约。<br><code>action_selection</code> | covered | `tree3:topic:0011`, `tree3:topic:0012`, `tree3:topic:0013`, `tree3:topic:0014`, `tree3:topic:0023`, `tree3:topic:0024`, `tree3:topic:0044` | — | topic0011-topic0014 完整覆盖行动选择语义、候选生成、选择依据和输出契约；topic0023/topic0024 覆盖行动执行与工具调用请求/结果契约；topic0044 覆盖失败控制入口。 |
| 定义观察吸收与状态更新语义。<br><code>observation_absorption</code> | covered | `tree3:topic:0021`, `tree3:topic:0022`, `tree3:topic:0044` | — | topic0021 覆盖观察接收、吸收、归一化和不确定性语义；topic0022 覆盖观察驱动的控制状态更新和控制权移交；topic0044 覆盖观察异常分支。排除项不要求存储或检索算法。 |
| 定义终止、中止、完成判定、暂停/恢复和人工升级语义。<br><code>termination_pause_escalation</code> | covered | `tree3:topic:0026`, `tree3:topic:0027`, `tree3:topic:0028`, `tree3:topic:0029`, `tree3:topic:0030`, `tree3:topic:0031`, `tree3:topic:0032` | — | topic0026 覆盖终态框架、触发来源和状态关系；topic0027-topic0029 分别覆盖完成、中止、终止判定；topic0030-topic0032 覆盖暂停/恢复与人工升级控制模型、触发、交接和恢复。 |
| 定义完成、中止与终止的判定条件和差异。<br><code>completion_abort_termination</code> | covered | `tree3:topic:0026`, `tree3:topic:0027`, `tree3:topic:0028`, `tree3:topic:0029` | — | topic0026 覆盖终态框架、终态性、状态关系、目标状态映射和冲突处理；topic0027-topic0029 分别覆盖完成、中止、终止的判定与可观察结果。 |
| 定义暂停/恢复与人工升级语义。<br><code>pause_escalation</code> | covered | `tree3:topic:0030`, `tree3:topic:0031`, `tree3:topic:0032` | — | topic0031 覆盖暂停触发、暂停期间状态保持、恢复条件与恢复失败语义；topic0032 覆盖人工升级触发、对象选择、交接包和升级后恢复/结束规则；topic0030 提供暂停与升级的组合控制模型。 |
| 定义决策循环与上下文、工具和策略组件的交互约束。<br><code>component_interaction_constraints</code> | partial | `tree3:topic:0007`, `tree3:topic:0008`, `tree3:topic:0009`, `tree3:topic:0010`, `tree3:topic:0024`, `tree3:topic:0044` | - 上下文不可用或过期时的控制入口与上下文侧输入输出契约细节<br>- 审计记录在决策循环中的触发节点、内容边界及策略/权限/审计责任划分 | topic0007 覆盖共享组件交互职责边界；topic0008 覆盖上下文交互约束；topic0009/topic0024 覆盖工具交互边界与调用契约；topic0010 覆盖策略组件交互。但森林没有明确覆盖上下文过期/不可用控制入口，也没有审计记录交互主题。 |
| 定义决策循环与上下文/记忆组件的交互约束。<br><code>context_interaction</code> | partial | `tree3:topic:0007`, `tree3:topic:0008` | - 上下文读取、消费和更新的输入输出契约字段与缺失/过期标记语义<br>- 上下文不可用或过期时进入的控制状态/转移及判定条件 | topic0008 覆盖上下文请求应围绕目标、交付需区分证据/缺失/不确定性、不得管理存储、不得把无上下文视为事实；topic0007 提供共享请求/响应/失败不得静默忽略的边界。但未明确给出上下文侧 IO 契约字段和过期/不可用控制入口。 |
| 定义决策循环与工具调用组件的交互约束。<br><code>tool_interaction</code> | covered | `tree3:topic:0009`, `tree3:topic:0024`, `tree3:topic:0044` | — | topic0009 覆盖决策循环与工具组件的职责边界和越权约束；topic0024 覆盖工具调用请求、返回观察信封、错误/超时/取消表示与接口职责划分；topic0044 覆盖工具/行动失败分支控制。排除项保持不进入注册、网关、权限矩阵和沙箱实现。 |
| 定义决策循环与策略/权限/审计组件的交互约束。<br><code>policy_interaction</code> | partial | `tree3:topic:0007`, `tree3:topic:0010` | - 审计记录触发点、记录内容边界及审计组件的交互责任 | topic0010 覆盖策略判断请求、允许/拒绝/附加义务的消费、策略组件的解释边界以及不得绕过策略结论；topic0007 提供共享组件责任边界。但森林没有审计记录相关主题，无法覆盖审计触发点和内容边界。 |
| 综合各子任务形成统一的决策循环控制流规范，并解决跨子任务冲突。<br><code>integrate_control_flow</code> | covered | `tree3:topic:0040`, `tree3:topic:0041`, `tree3:topic:0042`, `tree3:topic:0043` | — | topic0041 提供控制流规范元素完备性判据与缺口记录；topic0042 提供跨子任务一致性维度和冲突分类；topic0043 提供取舍准则、修复模式、决策记录与同步复核；topic0040 可补充未定义转移的统一处置。 |
| 整合各子任务输出，形成统一的决策循环控制流规范。<br><code>assemble_spec</code> | covered | `tree3:topic:0041`, `tree3:topic:0042`, `tree3:topic:0043` | — | topic0041 覆盖必需元素清单、元素覆盖判定、元素间衔接一致性和缺口记录；topic0042 覆盖跨元素一致性维度与冲突边界；topic0043 覆盖依赖追踪、变更传播、同步复核和决策记录，可支持未决接口与引用关系的标注记录。 |
| 执行跨子任务一致性检查并修复冲突，记录最终取舍。<br><code>consistency_conflict_resolution</code> | covered | `tree3:topic:0042`, `tree3:topic:0043` | — | topic0042 覆盖跨状态、契约、规划、行动、观察、终止和升级等语义的一致性维度与冲突分类；topic0043 覆盖优先级和取舍准则、修复模式、决策记录模型以及修复后的依赖追踪和一致性复核。 |
| 用正常路径、失败路径和边界场景走查或原型验证控制流，并修复发现的问题。<br><code>validate_control_flow</code> | covered | `tree3:topic:0036`, `tree3:topic:0037`, `tree3:topic:0038`, `tree3:topic:0039`, `tree3:topic:0040`, `tree3:topic:0041`, `tree3:topic:0042`, `tree3:topic:0043` | — | topic0036 提供控制流验证方法框架；topic0037-topic0039 分别覆盖正常、失败和边界场景验证；topic0040 覆盖未定义转移识别与处置；topic0041/topic0042/topic0043 支持缺口识别、一致性冲突修复和复验同步。 |
| 执行正常路径、失败路径和边界场景的控制流走查或原型验证。<br><code>walkthrough_prototype_validation</code> | covered | `tree3:topic:0036`, `tree3:topic:0037`, `tree3:topic:0038`, `tree3:topic:0039`, `tree3:topic:0040` | — | topic0036 覆盖验证活动总体方法和证据记录；topic0037-topic0039 分别覆盖正常、失败和边界场景走查/原型验证；topic0040 覆盖跨场景未定义转移的识别、汇总与问题表示。 |
| 修复验证发现的控制流问题并复验。<br><code>validation_fix_recheck</code> | covered | `tree3:topic:0036`, `tree3:topic:0037`, `tree3:topic:0038`, `tree3:topic:0039`, `tree3:topic:0040`, `tree3:topic:0041`, `tree3:topic:0043` | — | topic0040 覆盖未定义转移的修复、待决记录、修复后验证结果与规范同步；topic0041 覆盖缺口分类与记录；topic0043 覆盖冲突取舍、修复模式、决策记录和一致性复核；topic0036-topic0039 可支持修复后的正常、失败和边界场景复验。 |

</details>

### 7.9 森林 3 → 任务树 3

结果：pass；covered 26，partial 0，missing 0。

<details>
<summary>展开逐任务检查</summary>

| 任务 | 状态 | 相关主题 ID | 缺口 | 判断依据 |
| --- | --- | --- | --- | --- |
| 设计 Agent 的决策循环与执行控制状态机，定义目标推进、规划/重规划、行动选择、观察吸收、终止/暂停/人工升级的可执行语义。<br><code>decision_loop_control_architecture</code> | covered | `tree3:topic:0001`, `tree3:topic:0002`, `tree3:topic:0003`, `tree3:topic:0004`, `tree3:topic:0005`, `tree3:topic:0006`, `tree3:topic:0007`, `tree3:topic:0008`, `tree3:topic:0009`, `tree3:topic:0010`, `tree3:topic:0011`, `tree3:topic:0012`, `tree3:topic:0013`, `tree3:topic:0014`, `tree3:topic:0015`, `tree3:topic:0016`, `tree3:topic:0017`, `tree3:topic:0018`, `tree3:topic:0019`, `tree3:topic:0020`, `tree3:topic:0021`, `tree3:topic:0022`, `tree3:topic:0023`, `tree3:topic:0025`, `tree3:topic:0026`, `tree3:topic:0027`, `tree3:topic:0028`, `tree3:topic:0029`, `tree3:topic:0030`, `tree3:topic:0031`, `tree3:topic:0032`, `tree3:topic:0036`, `tree3:topic:0037`, `tree3:topic:0038`, `tree3:topic:0039`, `tree3:topic:0040`, `tree3:topic:0041`, `tree3:topic:0042`, `tree3:topic:0043`, `tree3:topic:0044` | — | 森林覆盖状态模型、转移与守卫、不变量、I/O 契约、组件交互、目标/计划/重规划、行动/观察、终止/暂停/升级、冲突整合、完备性检查及控制流验证等全部必需知识入口，可由多个主题组合支持根任务。 |
| 定义决策循环的状态集合、合法转移条件与循环不变量。<br><code>state_transition_invariant_model</code> | covered | `tree3:topic:0001`, `tree3:topic:0002`, `tree3:topic:0003`, `tree3:topic:0004` | — | 状态职责由 0001 覆盖，转移与守卫由 0002 覆盖，不变量的概念/表达与类别/具体约束由 0003、0004 组合覆盖；排除边界与本任务范围一致。 |
| 枚举决策循环的状态并定义每个状态的职责与边界。<br><code>decision_loop_states_and_responsibilities</code> | covered | `tree3:topic:0001` | — | 主题 0001 正好覆盖状态类型体系、核心与可选状态集合、命名语义、职责边界及完整性/不重叠原则，且未纳入本任务排除的转移与契约内容。 |
| 定义决策循环中状态间的合法转移关系、转移条件与守卫。<br><code>transition_conditions_and_guards</code> | covered | `tree3:topic:0002` | — | 主题 0002 覆盖合法转移关系、触发条件、守卫谓词、禁止转移及触发条件与守卫的组合语义，范围与本任务一致。 |
| 定义决策循环中必须始终成立的循环不变量。<br><code>loop_invariants</code> | covered | `tree3:topic:0003`, `tree3:topic:0004` | — | 主题 0003 提供不变量概念、表达规范与进入/停留/转移语义，主题 0004 提供候选不变量类别和具体约束，二者组合可支持不变量定义。 |
| 定义状态与关键转移的输入输出契约，以及与上下文、工具和策略组件的交互约束。<br><code>state_io_component_interaction_contracts</code> | covered | `tree3:topic:0005`, `tree3:topic:0006`, `tree3:topic:0007`, `tree3:topic:0008`, `tree3:topic:0009`, `tree3:topic:0010` | — | 0005 覆盖状态与转移契约，0006 覆盖接口完整性检查，0007 至 0010 分别覆盖组件交互职责边界及上下文、工具、策略组件交互约束。 |
| 定义每个状态与关键转移的输入输出契约。<br><code>state_transition_io_contracts</code> | covered | `tree3:topic:0005`, `tree3:topic:0006` | — | 0005 提供状态与关键转移契约化表达框架，0006 提供基于契约的接口完整性检查；两者组合覆盖任务完成条件。 |
| 定义决策循环与上下文、工具和策略组件交互时必须满足的约束。<br><code>component_interaction_constraints</code> | covered | `tree3:topic:0007`, `tree3:topic:0008`, `tree3:topic:0009`, `tree3:topic:0010` | — | 0007 给出共享职责边界，0008、0009、0010 分别细化上下文、工具和策略组件交互约束，且排除实现细节，符合本任务边界。 |
| 定义目标解析、目标推进判定、计划生成/选择以及重规划触发条件与语义。<br><code>goal_planning_replanning_semantics</code> | covered | `tree3:topic:0015`, `tree3:topic:0016`, `tree3:topic:0017`, `tree3:topic:0018`, `tree3:topic:0019`, `tree3:topic:0020` | — | 0015、0016 覆盖目标解析与推进判定，0017、0018 覆盖计划表示与生成/选择，0019 覆盖执行偏差检测，0020 覆盖重规划触发、状态与退出语义。 |
| 定义目标解析和目标推进判定语义。<br><code>goal_parsing_progress_evaluation</code> | covered | `tree3:topic:0015`, `tree3:topic:0016` | — | 0015 覆盖可执行目标规格与目标解析，0016 覆盖推进/受阻判定及可观察结果，且二者均排除计划生成和重规划触发，与本任务边界一致。 |
| 定义计划生成/选择以及重规划触发条件与重规划状态语义。<br><code>planning_replanning_trigger_semantics</code> | covered | `tree3:topic:0017`, `tree3:topic:0018`, `tree3:topic:0019`, `tree3:topic:0020` | — | 0017 提供计划表示与规划建模，0018 提供计划生成与选择语义，0019 提供偏差检测，0020 提供重规划触发、控制状态与退出条件；详细失败恢复设计被排除。 |
| 定义行动选择、观察吸收以及控制权在步骤间的流转契约。<br><code>action_observation_control_flow</code> | covered | `tree3:topic:0011`, `tree3:topic:0012`, `tree3:topic:0013`, `tree3:topic:0014`, `tree3:topic:0021`, `tree3:topic:0022`, `tree3:topic:0025` | — | 0011 至 0014 覆盖行动选择整体语义、候选生成、选择依据与输出契约，0021 覆盖观察吸收，0022 覆盖控制状态更新与控制权移交，0025 覆盖步骤依赖与调度控制。 |
| 定义行动选择语义。<br><code>action_selection_semantics</code> | covered | `tree3:topic:0011`, `tree3:topic:0012`, `tree3:topic:0013`, `tree3:topic:0014` | — | 0011 提供行动选择整体框架，0012 覆盖行动表示与候选生成，0013 覆盖选择依据与可行性约束，0014 覆盖决策规则和结果输出契约；观察吸收与计划生成被明确排除。 |
| 定义观察吸收以及吸收后的控制权流转语义。<br><code>observation_absorption_control_handoff</code> | covered | `tree3:topic:0021`, `tree3:topic:0022` | — | 0021 覆盖观察接收、吸收、归一化及证据/不确定性，0022 覆盖观察驱动的控制状态更新和控制权移交；两者组合覆盖任务，且不涉及存储实现和失败恢复详细设计。 |
| 定义完成、中止、终止、暂停和人工升级的判定条件与状态语义。<br><code>termination_pause_escalation_semantics</code> | covered | `tree3:topic:0026`, `tree3:topic:0027`, `tree3:topic:0028`, `tree3:topic:0029`, `tree3:topic:0030`, `tree3:topic:0031`, `tree3:topic:0032` | — | 0026 至 0029 覆盖终态框架及完成、中止、终止判定，0030 至 0032 覆盖暂停与人工升级的控制模型、触发、状态保持、交接和恢复/结束语义。 |
| 定义完成、中止和终止的判定条件与状态语义。<br><code>completion_abort_termination_criteria</code> | covered | `tree3:topic:0026`, `tree3:topic:0027`, `tree3:topic:0028`, `tree3:topic:0029` | — | 0026 提供终态框架和映射，0027、0028、0029 分别覆盖完成、中止、终止的判定与状态语义；暂停和人工升级被排除，与本任务一致。 |
| 定义暂停和人工升级的判定条件与交接语义。<br><code>pause_human_escalation_semantics</code> | covered | `tree3:topic:0030`, `tree3:topic:0031`, `tree3:topic:0032` | — | 0030 覆盖暂停与人工升级控制模型，0031 覆盖暂停触发、状态保持与恢复，0032 覆盖人工升级触发、对象、交接和恢复/结束规则；完成/中止/终止判定被排除。 |
| 汇总并协调状态、契约、规划/重规划、行动/观察、终止与升级语义，形成统一的决策循环状态机或等价控制流规范，并解决跨子任务冲突。<br><code>control_flow_integration_consistency</code> | covered | `tree3:topic:0001`, `tree3:topic:0002`, `tree3:topic:0003`, `tree3:topic:0004`, `tree3:topic:0005`, `tree3:topic:0007`, `tree3:topic:0008`, `tree3:topic:0009`, `tree3:topic:0010`, `tree3:topic:0011`, `tree3:topic:0012`, `tree3:topic:0013`, `tree3:topic:0014`, `tree3:topic:0015`, `tree3:topic:0016`, `tree3:topic:0017`, `tree3:topic:0018`, `tree3:topic:0019`, `tree3:topic:0020`, `tree3:topic:0021`, `tree3:topic:0022`, `tree3:topic:0026`, `tree3:topic:0027`, `tree3:topic:0028`, `tree3:topic:0029`, `tree3:topic:0030`, `tree3:topic:0031`, `tree3:topic:0032`, `tree3:topic:0041`, `tree3:topic:0042`, `tree3:topic:0043` | — | 各组件语义由对应主题覆盖，跨子任务一致性与冲突分类由 0042 覆盖，冲突取舍、修复和同步由 0043 覆盖，必需元素完备性由 0041 覆盖；可由主题组合支持整合任务。 |
| 识别并解决状态、契约、规划/重规划、行动/观察、终止/升级语义之间的跨子任务冲突。<br><code>cross_subtask_conflict_resolution</code> | covered | `tree3:topic:0042`, `tree3:topic:0043` | — | 0042 覆盖跨子任务依赖、一致性约束和冲突分类，0043 覆盖取舍准则、修复模式、决策记录及修复后同步复核；最终整合、完备性检查和场景验证被排除。 |
| 将状态、契约、规划/重规划、行动/观察、终止/升级语义整合为统一的决策循环状态机或等价控制流规范。<br><code>unified_control_flow_specification</code> | covered | `tree3:topic:0001`, `tree3:topic:0002`, `tree3:topic:0003`, `tree3:topic:0004`, `tree3:topic:0005`, `tree3:topic:0007`, `tree3:topic:0008`, `tree3:topic:0009`, `tree3:topic:0010`, `tree3:topic:0011`, `tree3:topic:0012`, `tree3:topic:0013`, `tree3:topic:0014`, `tree3:topic:0015`, `tree3:topic:0016`, `tree3:topic:0017`, `tree3:topic:0018`, `tree3:topic:0019`, `tree3:topic:0020`, `tree3:topic:0021`, `tree3:topic:0022`, `tree3:topic:0026`, `tree3:topic:0027`, `tree3:topic:0028`, `tree3:topic:0029`, `tree3:topic:0030`, `tree3:topic:0031`, `tree3:topic:0032`, `tree3:topic:0042` | — | 统一规范所需各领域语义由状态、契约、不变量、规划/重规划、行动/观察、终止/暂停/升级等主题并集提供，0042 补充跨部分引用与一致性关系；冲突修复、完备性检查和验证已由其他子任务承担。 |
| 检查统一控制流规范是否覆盖决策循环所需的全部元素。<br><code>required_element_completeness_check</code> | covered | `tree3:topic:0041` | — | 主题 0041 直接覆盖必需元素清单、元素覆盖判定、衔接一致性、缺口分类与记录要求，且不展开详细定义、冲突修复或场景验证。 |
| 用正常路径、失败路径和边界场景走查或原型验证决策循环控制流无未定义转移。<br><code>control_flow_validation</code> | covered | `tree3:topic:0033`, `tree3:topic:0034`, `tree3:topic:0035`, `tree3:topic:0036`, `tree3:topic:0037`, `tree3:topic:0038`, `tree3:topic:0039`, `tree3:topic:0040`, `tree3:topic:0044` | — | 0036 覆盖验证方法，0037 至 0039 分别覆盖正常、失败、边界场景验证，0040 覆盖未定义转移汇总与处置，0033 至 0035 和 0044 补充边界分类及失败回退/升级语义。 |
| 用正常路径场景走查或原型验证控制流。<br><code>normal_path_validation</code> | covered | `tree3:topic:0036`, `tree3:topic:0037` | — | 0037 覆盖正常路径场景规格、主要转移覆盖、走查或原型验证与证据记录，0036 提供验证方法总体框架；失败与边界验证被排除。 |
| 用失败路径场景走查或原型验证控制流。<br><code>failure_path_validation</code> | covered | `tree3:topic:0038`, `tree3:topic:0044` | — | 0038 覆盖失败路径场景建模、失败注入点、分支覆盖、逐转移核对和证据记录，0044 提供失败分支控制流及回退/升级语义；详细重试恢复设计被排除。 |
| 用边界场景走查或原型验证控制流。<br><code>boundary_scenario_validation</code> | covered | `tree3:topic:0030`, `tree3:topic:0031`, `tree3:topic:0032`, `tree3:topic:0033`, `tree3:topic:0034`, `tree3:topic:0035`, `tree3:topic:0039` | — | 0039 覆盖边界场景验证方法、覆盖判定和证据记录，0033 至 0035 提供边界分类、重复观察和空计划边界，0030 至 0032 提供暂停/恢复与人工升级语义；正常和失败路径验证被排除。 |
| 汇总各场景验证结果，识别并处理未定义转移。<br><code>undefined_transition_consolidation</code> | covered | `tree3:topic:0040` | — | 主题 0040 直接覆盖未定义转移的分类、验证结果结构化、跨场景归一化去重、修复或待决记录，以及修复后与规范同步；单场景执行、冲突修复和完备性检查被排除。 |

</details>
