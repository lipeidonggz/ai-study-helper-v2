# 08 · 评测台设计：从评测、诊断到回归闭环

上游：[01](01-product-requirements.md)（产品需求）、 [03](03-cases-and-gold-standard.md)（用例与金标准）、 [06](06-retrieval-pure-agent.md)（检索与 trace）、 [07](07-html-ingestion-design.md)（入库）。

**状态：已审核（2026-10-20），作为当前实现基线。** 从产品需求出发设计，不继承现有实现。

[← 返回文档索引](../README.md)

## 0. 这篇解决什么

我们已经有用例、金标准、判官规则、Agent trace 和入库检查，但这些能力需要在一个地方形成闭环：选哪些题、在什么环境下跑、怎样判定、为什么失败、与上一次相比有没有退化。

>  **评测台不是跑分页，而是“固定现场 → 跑 → 判 → 诊断 → 比较 → 回归”的工作台。**

**第一版以端到端评测为主：**直接运行完整 Agent，从用户问题一直到最终回答，再结合 trace 做组件级诊断。入库和检索工具不单独建设完整评测台，只补必要的确定性检查，避免第一版同时展开三套评测体系。

本文定评测台的功能边界、核心对象、执行与判定架构、运行流程、诊断范围、数据隔离和第一版取舍。具体判官提示词、阈值校准、统计公式和 UI 视觉不在本文展开。

## 1. 评测、诊断、回归分别回答什么

| 能力 | 回答的问题 | 核心产物 | 不负责什么 |
| --- | --- | --- | --- |
| **评测** | 这次答案是否满足用例金标准？通过率、红线、延迟和成本是多少？ | 单次判定、用例结果、类别汇总、评测报告 | 不解释根因，不自动修改 Agent |
| **诊断** | 为什么没过？问题在获取、检索、Agent 循环、提示词、模型、引用还是判官？ | Trace 现场、证据链、失败分类、归因记录 | 不替代评测结论，不凭印象打总评 |
| **回归** | 与冻结基线相比，哪些用例、红线、延迟或成本变好或变差？ | Run 对比、翻转项、回归门槛和发布结论 | 不把单次随机抖动当成能力变化 |

**结论：评测和诊断都要，但必须是两个独立产物。**评测给出“过没过”，诊断给出“为什么”。执行现场通过 attempt 和可选的 `trace_id` 关联判定与诊断，不把诊断意见混进原始评测判定。

架构上还要再拆一层：**ExecutionRun 负责固定“系统实际跑出了什么”，EvaluationRevision 负责固定“用哪套规则把它判成了什么”**。重判时复用原执行结果，生成新的 EvaluationRevision，不改写旧判定。

## 2. 设计原则

1. **用例是长期资产。**输入、环境、金标准、判定项和执行方式都要版本化；金标准与判官口径属于基础件，AI 可起草，人工复核后才能生效。
2. **一次评测必须固定完整现场。**这里既包括 Agent 当时实际可检索到的 collection 快照，也包括应用版本、prompt bundle、Agent 循环、工具契约、模型参数和判官版本；它们都必须随 run 固化。
3. **不重新定义判定项。**判定项沿用 01／03／06 已定义的口径；评测台负责执行、记录、聚合和展示，不另造一套评价体系。
4. **先判判定项，再做聚合。**程序可判的确定性检查不交给 LLM；LLM 只处理语义判定；争议项允许人工复核。
5. **红线不能被平均。**安全、引用真实性、来源可溯源、遗漏透明等红线必须单独报告；总体高分不能抵消红线失败。
6. **多次运行吸收抖动。**同一用例重复 N 次，记录 pass／fail／pending 分布和跨次翻转，不把单次输出当稳定能力。
7. **诊断必须指向现场。**每个失败结论都应能回到 attempt、trace、工具调用、证据块和版本；不能只留一句“模型答错了”。
8. **运行与判定不可变。**原始 attempt、trace 和 judge 输入只追加不改写；人工备注、诊断标签和重新判定作为新记录追加。

## 3. 主工作流

```text
选择 suite revision
  → 展开不可变 case／criterion revision
  → 预检 collection、版本、目标文档、模型与预算
  → 生成 ExecutionPlan
  → 通过统一 Agent 执行核心按各用例 N 次运行
  → 封存 ExecutionRun
  → 绑定 checker／judge／聚合规则并生成 EvaluationRevision
  → 逐项判定 → 用例聚合 → 报告投影
  → 对失败项生成诊断建议并由人确认
  → 与当前基线比较
  → 人工设置新基线／继续修复／保持旧基线
```

### 3.1 ExecutionRun 与 EvaluationRevision

| 对象状态 | 含义 | 允许动作 |
| --- | --- | --- |
| `planned` | 预检通过，ExecutionPlan 已固化，尚未运行 | 启动、复制为新计划 |
| `running` | 按计划执行 attempt，可并发运行多个用例 | 暂停、取消、查看进度 |
| `paused` | 因用户、预算或依赖故障暂停；后续可继续追加 attempt | 继续、取消、查看已完成项 |
| `sealed_complete` | 所有必跑用例已达到有效 N，执行现场已封存 | 进入判定；可创建新的 EvaluationRevision |
| `sealed_partial` | 用户主动结束或替代 attempt 达到上限，仍有有效样本缺口 | 查看缺口；只能诊断，不能设为基线 |
| `invalid_plan` | 预检失败，未执行任何 attempt | 修复输入或版本后重新生成计划 |
| `EvaluationRevision: judging` | 正在对一个已封存 ExecutionRun 做判定与聚合 | 查看进度；不展示为最终结论 |
| `EvaluationRevision: complete` | 所有应判项均已完成，无 pending | 查看、诊断、人工设为基线 |
| `EvaluationRevision: incomplete` | 仍有 pending 或因证据不足无法判定 | 人工复核或生成新 EvaluationRevision；不能设为基线 |

ExecutionRun 在未封存时允许因基础设施失败追加替代 attempt；封存后执行现场不可再修改。若要继续一个已封存的残缺运行，新建带 `resume_of` 关系的 ExecutionRun，不能把新结果混进旧 run。

**运行策略：**N／K 属于 EvalCaseRevision 的执行属性，新建用例默认 N＝10、K＝9。同一用例至少 K 次满足金标准且红线零违反才通过；任何一次 attempt 出现红线违规，该用例直接失败。

**有效样本口径：**基础设施失败保留 attempt，但不计入有效 N，允许在预算与次数上限内补跑；回答已生成但判官为 pending 时计入 N、不算通过；崩溃或未知结果标记 `abandoned`／`outcome_unknown`，不自动重发。

## 4. 核心对象

```text
EvalSuite
└─ revisions[]: EvalSuiteRevision
   ├─ scope_id
   └─ case_revision_ids[]

EvalCase
└─ revisions[]: EvalCaseRevision
   ├─ input
   ├─ environment_metadata
   ├─ golden_standard
   ├─ criterion_revision_ids[]
   └─ execution_policy（N／K／延迟阈值）

Criterion
└─ revisions[]: CriterionRevision
   ├─ rule_type: R（必选）｜X（边界）
   ├─ evaluator: checker｜llm_judge｜human
   ├─ evaluator_ref
   └─ review_status: draft｜reviewed｜active

ExecutionPlan
├─ suite_revision_id
├─ collection_snapshot_id
├─ agent_version_set
├─ model_config
├─ evaluation_version_set
├─ run_policy（并发／超时／预算）
├─ experiment_variable
└─ preflight_result

ExecutionRun
├─ plan_id
├─ status
├─ resume_of?
├─ attempts[]
└─ sealed_at?

Attempt
├─ case_revision_id
├─ execution_run_id
├─ session_id
├─ trace_id?
├─ trace_status: complete｜partial｜missing
├─ execution_status
├─ answer
├─ timestamps（started／first_token／finished）
└─ usage

EvaluationRevision
├─ execution_run_id
├─ checker_version_set
├─ judge_version_set
├─ aggregation_policy_revision_id
├─ judgments[]
├─ effective_judgment_bindings[]
├─ case_aggregates[]
└─ report_projection

Judgment
├─ attempt_id
├─ criterion_id
├─ executor: program｜llm_judge｜human
├─ raw_result
├─ normalized_result: pass｜fail｜pending
└─ evidence／reason／generated_at

DiagnosisSuggestion
├─ attempt_id
├─ generator_version
├─ suggested_failure_layer
├─ suggested_root_cause_tags[]
├─ evidence_refs[]
└─ confidence?

DiagnosisConclusion
├─ attempt_id
├─ suggestion_refs[]
├─ confirmed_failure_layer
├─ confirmed_root_cause_tags[]
├─ evidence_refs[]
└─ reviewer／notes

Baseline
├─ scope_id
├─ execution_run_id
├─ evaluation_revision_id
├─ set_by／reason／set_at
└─ supersedes?
```

| 对象 | 职责 |
| --- | --- |
| `EvalSuiteRevision` | 一次评测选择的不可变用例 revision 集合和评测范围；suite 只引用 revision，不拥有会被后续编辑改写的当前用例。 |
| `EvalCaseRevision` | 问题输入、测试环境元数据、金标准、判定项引用和执行属性。N／K 属于该 revision；目标文档与干扰文档只供判定和诊断，不进入 Agent 运行输入。 |
| `CriterionRevision` | 把一条 R／X 判定规则绑定到受控 checker 或版本化判官模板；必须经人工复核后才可进入 active 状态。 |
| `ExecutionPlan` | 预检通过后的不可变执行计划，固定 collection、Agent、模型、判定版本、预算、并发和唯一实验变量。 |
| `ExecutionRun` | 只记录系统实际跑出了什么。运行中允许追加替代 attempt；封存后执行现场不可修改。 |
| `Attempt` | 一个用例的一次全新 Agent 会话。允许没有完整 trace，但必须保留执行状态、答案、时间戳和 usage 等可得信息。 |
| `EvaluationRevision` | 针对某个 ExecutionRun、使用特定 checker／judge／聚合规则计算出的不可变判定快照。重判产生新 revision。 |
| `Judgment` | 程序、LLM 或人工产生的单次判定。所有记录只追加；通过 `effective_judgment_binding` 明确聚合时采用哪一条。 |
| `DiagnosisSuggestion` | 自动诊断的候选失败层、根因与证据，只供人工参考。 |
| `DiagnosisConclusion` | 人工确认后的正式诊断；只有它能进入正式统计和修复任务。 |
| `Baseline` | 绑定一个已封存 ExecutionRun 和一个完整的 EvaluationRevision；每个评测范围只保留一个当前基线。 |

**判定解析不采用“最后写入覆盖”。**同一 `attempt + criterion` 的多次 Judgment 全部保留；EvaluationRevision 显式记录当前生效的 judgment ID，人工复核通过替换绑定来完成覆盖。底层原始结论保留 R 的“满足／未满足／不确定”和 X 的“违规／无违规／不确定”，聚合后再归一成 pass／fail／pending。

### 4.1 用例与用例集的版本工作流

详细决定见 [ADR 0010](../decisions/0010-evaluation-case-and-suite-revision-workflow.md)。本节定义评测台必须遵守的用户可观察语义。

**用例：**

- 稳定 `case_id` 表示长期评测意图；草稿是可反复编辑的工作副本，不参与执行。
- 草稿提交审核后，在单个事务中生成不可变 case revision。问题、逻辑目标文档／章节、干扰元数据、默认 N／K、执行策略、精确 criterion revision IDs、变更说明和 content hash 同属该修订包。
- R／X 判定项内部保持独立 criterion revision；未修改项复用旧 revision，只修改变化的项。用户以整个 case revision 为审核单位，禁止判定项脱离用例包独立生效。
- 同一 case 最多一个 active revision。激活新 revision 时，旧 active 自动退休。

**快速试跑：**

- 可选择 reviewed 或 active case revisions，不能选择 draft 或 retired。
- 不要求先创建 suite；ExecutionPlan 直接冻结所选 case revisions、snapshot、有效 N／K 和预算。
- 允许覆盖 N，但 K 始终取源 case revision。N<K 时只产生探索性执行结果，不生成正式 pass／fail，也不能成为基线。
- ExecutionPlan 记录 `mode=exploratory`、source／effective N／K，并明确区分探索性试跑与正式评测。

**用例集：**

- suite 是具名、可复用评测范围；suite revision 精确引用 case revision IDs，不动态查询“当前最新版”。
- 同一 case 在一个 suite revision 中最多一个 revision。suite draft 发布并激活时，同一 suite 的旧 active 退休；不同 suite 身份可同时 active。
- suite 不自动跟随 case 新版本。任一成员 case revision 退休后，旧 suite revision 标记为 stale，禁止启动新正式 run，但历史 ExecutionPlan、run 和 trace 保持原引用。
- 历史 suite revision 只读；使用旧范围形成新 run 时，必须克隆成 suite draft，显式替换用例修订版并重新发布。
- 新正式 run 必须同时满足：suite active、全部成员 case revisions active、同一 case 唯一、snapshot 与版本预检通过。

## 5. 第一版五个核心模块

| 模块 | 核心功能 | 关键边界 |
| --- | --- | --- |
| **用例与金标准** | 用本地数据库管理 suite、case、criterion revision；绑定 01／03／06 已有的判定项；新建用例默认 N＝10、K＝9；显示 15 类问题覆盖。 | 金标准与判官口径必须先 draft、经人工 review 才能 active；历史 run 只引用旧 revision，不能被“当前最新版”替换。 |
| **运行编排** | 运行前预检并生成 ExecutionPlan；通过统一 Agent 执行核心启动全新会话；固定 collection 和整套 Agent／模型版本；执行硬预算、暂停、替代 attempt 和封存。 | 同一 run 内除输入、N／K、延迟阈值和明确声明的预算覆盖外，模型、prompt、循环、工具与 collection 必须统一；封存后不得追加结果。 |
| **判定** | 用受控 checker 注册表和版本化判官模板判定；保留所有 Judgment，并显式绑定当前生效结果；按版本化聚合规则生成 EvaluationRevision。 | 判官输入采用白名单投影，不能看到历史判定、基线、自动诊断或通过率；第一版每条语义项只判一次，不声称已测量判官抖动。 |
| **诊断** | 从失败 attempt 进入可用的 trace、证据、工具调用和版本；自动生成 DiagnosisSuggestion，人工再形成 DiagnosisConclusion。 | 自动建议不能进入正式统计；trace 缺失或部分时不给出确定性根因，只标记证据不足。 |
| **报告与比较** | 从 ExecutionRun、EvaluationRevision、Judgment 和聚合规则重建报告投影；面向开发人员展示通过率、红线、成本、质量抖动和独立延迟结果。 | 报告不是事实源；并发质量 run 的延迟不能与串行延迟基线混比；“跨场比较”必须显式标记。 |

**判官校准工作台不进入第一版。**第一版只固定判官版本、保留判定依据和必要的人审入口；系统性校准样本、分歧队列、重复一致性看板和判官边界治理后续再做。

## 6. 诊断层怎么设计

诊断不是再让模型写一段“失败原因”。它要把失败拆到可验证的层次，并回指现场。

| 失败层 | 典型证据 | 典型结论 |
| --- | --- | --- |
| 基础设施 | trace 中的超时、限流、5xx、连接失败 | 本次没有形成有效答案，属于执行失败 |
| 入库／collection | collection 状态、failed_documents、section／block 对账 | 目标文档未成功入库或证据不完整 |
| 检索 | search 排名、召回块、list／scan 返回、章节范围 | 没召回、召回错块、漏范围、枚举不完整 |
| Agent 循环 | 轮次、工具选择、停止原因、预算检查 | 未调用工具、调错工具、过早结束、重复无效检索 |
| 提示词／协议 | 实际渲染提示词、Action／Observation 解析 | 规则理解错、协议格式错、输出槽缺失 |
| 模型 | 原始回答、模型版本、温度、token 使用 | 漏写、事实错误、跨源混淆、输出不稳定 |
| 引用 | 引用键、证据块存在性、归属和支持关系 | 引用无效、归属错误、引用不支持结论 |
| 判官 | 判官输入、判官版本、重复判定分布、人审意见 | 判官翻转、误判或判定项本身有歧义 |

一个失败可以有主因和次因，但正式结论必须至少有一个可核验的证据引用。自动归因与人工结论分开保存：自动侧写候选失败层、根因标签、证据和生成器版本；人工侧确认、修改或否决，并留下 reviewer。

第一版每条语义判定只跑一次，失败诊断可以重放证据和人工复核，但不系统测量判官自身抖动。因此质量抖动报告只能把多次 Agent attempt 的差异作为生成侧读数；除非未来对同一答案做重复判定，否则不得声称判定侧抖动已验证。

## 7. 第一版视图

**第一读者是开发人员。**评测台是独立开发工具，不随桌面产品面向普通用户发布。第一版只做本地 Web UI，不做 CLI；服务只绑定 `127.0.0.1`，不提供局域网或远程访问。

1. **用例库**：15 类问题覆盖、revision 差异、draft／reviewed／active 状态和最近结果。
2. **运行台**：选择 suite revision 与 collection 快照，配置并发、预算和唯一实验变量；启动前展示预检结果与计划。
3. **结果矩阵**：attempt／case／问题类／run 四层；红线、pass／fail／pending、延迟、token、工具有独立列。
4. **失败队列**：按失败层和根因筛选；区分自动建议与人工结论；点进 attempt 现场。
5. **Trace 现场**：请求、轮次、工具、证据、预算、版本和 artifact；明确展示 trace 完整度与缺失原因。
6. **基线比较**：只有已封存且完整判定的 ExecutionRun + EvaluationRevision 可设为当前基线；比较前检查是否满足同场条件。

第一版先让“选 revision → 预检 → 跑 → 封存 → 判 → 看失败 → 比较”走通，不追求复杂图表和全自动分析。

## 8. MVP 边界

| 第一版要做 | 第一版不做 |
| --- | --- |
| 以端到端 Agent 评测为主，组件检查只服务诊断 | 同时建设三套独立评测台 |
| 用例、金标准、判定项和 suite 管理 | 自动生成大量合成用例 |
| 固定版本的后台运行、N 次重复和缺失项显示 | 自动修改提示词或 Agent 实现 |
| 程序判定＋LLM 判官＋人工复核 | 让判官自由决定总分和发布 |
| 红线单独报告、抖动和跨次翻转 | 复杂模型竞技场和全排行榜 |
| 失败队列、trace 跳转和人工诊断标签 | 全自动根因定案 |
| 与冻结基线比较 | 自动把某次 run 设为新基线 |
| 固定判官版本并保留人工复核入口 | 系统性判官校准工作台 |
| 独立本地评测服务＋Web UI，仅监听 `127.0.0.1` | CLI、局域网访问、远程访问 |
| 执行与判定分离、不可变 revision、人工封存与基线 | 最后写入覆盖、自动续跑并改写旧 run |
| 质量评测允许配置并发；延迟评测独立串行 | 不同并发度下混比首字延迟 |
| 按失败、pending、基线关联项保留完整 trace | 无上限保留所有 attempt 的完整 artifact |
| Web UI 数据库保存用例 revision 与人工复核状态 | 第一版做 CLI、用例导入／导出和第二事实源 |

## 9. 已确认的产品口径

>  **第一版的产品方向已经收敛：先把端到端 Agent 评测闭环跑通，复杂评测能力后置。** 测试环境确定为 suite 级共享现场：同一 suite 里的用例使用同一个冻结 collection 快照，保证真实知识库体验和跨 run 可比性。

### 9.1 已确认决策

| 决策点 | 已确认口径 |
| --- | --- |
| **先评什么** | 先做端到端 Agent 评测：从用户问题一直跑到最终回答；入库、检索和 Agent 循环的组件检查只服务失败诊断，不在第一版另建三套完整评测台。 |
| **判定项定义** | 不重新定义。沿用 [03](03-cases-and-gold-standard.md) 已定的用例级判定结构、必选／边界口径与聚合规则，以及 [06](06-retrieval-pure-agent.md) 的检索和 trace 诊断口径；评测台只负责执行、记录、聚合和展示。 |
| **重复运行** | 默认 N＝10、K＝9，即同一用例跑 10 次，至少 9 次满足金标准且红线零违反才算通过；允许按用例覆盖 N／K，但覆盖值必须在运行前进入 run 配置。 |
| **失败归因** | 自动诊断只给候选失败层、根因标签和证据建议；最终诊断结论由人确认，自动结果不得覆盖人工判断。 |
| **基线** | 由人明确选择哪次 run 成为基线；系统记录操作人、原因和时间，不自动切换，也不因为一次结果更好就替换基线。 |
| **报告读者** | 第一读者是开发人员。优先支持定位失败、比较版本和验证改动，不优先做外部演示或面向非技术用户的运营看板。 |
| **判官校准** | 第一版只固定判官版本、保留判定依据和必要的人审入口；系统性校准样本、分歧队列、重复一致性看板和判官边界治理后置。 |
| **trace 与评测记录** | trace 保留完整运行现场；评测记录只保存判定、诊断和引用，通过 `attempt_id`／`trace_id` 关联，不复制正文、不把评分混进 trace。 |

### 9.2 测试环境：Suite 共享现场

这里的“测试环境”不是运行代码，而是知识库现场的三件事：**Agent 实际能检索到哪套冻结 collection 快照、这条用例的目标文档是哪些、是否故意放入干扰文档**。目标文档和干扰文档主要用于判定与诊断，不能默认把答案范围直接告诉 Agent。

**第一版采用方案 A：**[03](03-cases-and-gold-standard.md) 当前定义的 15 篇 HTML 测试语料组成同一个冻结 collection 快照，suite 内所有用例都在这个现场中运行。基线与 PDF 排除原因见 [ADR 0006](../decisions/0006-use-current-html-corpus-for-retrieval-baseline.md)。每条用例仍记录自己的目标文档、相关干扰文档标记和判定元数据，但这些记录只供判定与诊断使用，不缩小 Agent 的实际检索范围。

| 方案 | 结论 | 口径与理由 |
| --- | --- | --- |
| **A. Suite 共享现场** | 第一版采用 | 整个 suite 使用同一个冻结 collection 快照；每条用例只记录自己的目标文档、相关干扰文档标记和判定元数据。 |
| **B. Case 隔离现场** | 第一版不采用，保留为后续能力 | 逐用例隔离更适合精确定位入库／检索问题，但会引入大量快照或文档子集，削弱跨用例比较，也不如共享现场接近真实用户的大知识库。等端到端评测闭环稳定后再按诊断需求补。 |

**边界：**共享现场不等于把所有文档都塞进上下文。Agent 仍通过正常检索路径访问知识库；用例的金标准、目标文档和干扰范围不能被注入提示词，也不能成为检索白名单。这样才是在考系统真实能力，而不是把答案位置提前告诉它。

**比较规则：**同一 suite 的 run 必须固定 collection 快照 ID。快照变化后，即使问题、模型和提示词都没变，也只能视为换了评测现场，不能直接与旧基线逐条比较。

## 10. 架构设计

### 10.1 部署与系统边界

| 问题 | 已确认设计 |
| --- | --- |
| **产品形态** | 评测台是开发工具，不随桌面产品面向普通用户发布。第一版复用产品桌面宿主源码和 Vue 源码，通过显式评测编译特性增加工作区；发布构建在编译期移除评测 UI、评测 sidecar 和评测模块。评测运行仍使用独立本地 sidecar 与数据根，只监听 `127.0.0.1`；不做 CLI、局域网访问或远程访问。 |
| **数据根目录** | 评测数据使用独立目录，和正常产品数据、活动知识库、正常会话与用户 trace 主目录隔离。评测 sidecar 独立于产品 sidecar，只读引用冻结 collection 快照；API Key 沿用开发环境配置，不写入任何评测记录。 |
| **被测系统入口** | 评测台只绕开 UI 和人工会话层，通过统一 Agent 执行核心运行用例。它不能复制 Agent 逻辑，也不能直接调用检索工具、数据库或模型来拼另一条问答链。 |
| **会话隔离** | 每次 attempt 都从全新 Agent 会话开始，不继承上一条 attempt 的对话、工具结果或模型上下文；collection 快照在该 attempt 内固定不变。 |
| **事实源** | suite、case、criterion 和人工复核状态存本地数据库。第一版不做用例导入／导出，也不建立第二个文件事实源。 |

### 10.2 数据隔离与防泄漏

评测元数据和 Agent 运行输入必须分开。金标准、目标文档、干扰文档标记、预期引用和判定项属于评测专用数据，不能进入系统提示词、工具参数、检索过滤器或正常问答会话。

判官输入采用显式白名单投影。每条语义判定只接收完成该判定所需的最少信息，例如用户问题、回答、当前判定项、必要依据和可核验证据；不得看到历史判定、当前通过率、基线、自动诊断或“这是回归测试”等提示。

自动诊断与正式诊断分开：`DiagnosisSuggestion` 只提供候选失败层和证据，`DiagnosisConclusion` 才由人确认并进入正式统计。trace 缺失或只有部分数据时，诊断必须标记“证据不足”。

### 10.3 版本与实验控制

| 对象 | 版本规则 |
| --- | --- |
| **Suite／Case／Criterion** | 均采用不可变 revision。一次 ExecutionRun 引用当时的具体 revision，不引用可被继续编辑的“当前版”。 |
| **Checker／Judge／Aggregation** | 程序 checker 使用受控注册表；判官模板和聚合规则各自版本化。数据库只保存引用和参数，不保存可执行代码。 |
| **人工复核** | 金标准、判定项和判官口径采用 `draft → reviewed → active`；只有 active revision 能被新计划引用。 |
| **模型** | 固定精确模型标识和实际生效参数，包括对话模型、embedding、判官模型、温度与输出上限；provider 返回实际模型标识时一并记录。 |
| **N／K** | 作为 EvalCaseRevision 的执行属性，新建默认 N＝10、K＝9；不设会在运行时覆盖全部用例的全局 N／K。 |
| **实验变量** | 同一 ExecutionRun 内模型、prompt、循环、工具契约和 collection 必须统一。对照实验只改变一个主要变量；组合变更必须显式标记，不能把结果归因给单项。 |

### 10.4 预检、执行与恢复

启动前生成 ExecutionPlan，并统一检查 suite／case／criterion revision、collection 状态与完整性、目标文档、目标 section／block、checker／judge／聚合版本、模型配置和预算。任一项失败都返回 `invalid_plan`，不进入执行阶段。

**collection 完整性：**允许对 `ready_partial` 快照运行，但任一用例的目标文档在失败清单中时禁止启动；若只有非目标文档失败，run 标记 `environment_degraded`，只能诊断，不能设为基线。

**运行状态：**ExecutionRun 在未封存时可以因基础设施失败追加替代 attempt；达到有效 N 或用户主动结束后封存。封存后执行现场不可修改，继续残缺运行必须新建带 `resume_of` 关系的新 run。

**崩溃恢复：**执行中的 attempt 若因崩溃或断电结果未知，标记 `abandoned`／`outcome_unknown`，保留已落盘的部分 trace，不自动假定失败并立即重发。恢复后由调度器检查或显式创建替代 attempt，并记录替代关系。

### 10.5 调度、预算与延迟

评测 run 必须设置硬预算，至少限制每个 provider 的 token 与请求次数，并提供可选费用估算。达到上限时暂停后续 attempt、保留已完成结果，不把未运行项算成失败，也不无限重试。

质量评测允许配置并发以缩短批量运行时间；Q8 首字延迟必须使用独立、固定并发度的串行运行。首字延迟来自统一执行适配器的 `request_started_at` 与 `first_answer_token_at` 事件，不由完整回答结束后倒推。

**有效 N：**基础设施失败不计入有效 N，可在预算和替代次数上限内补跑；回答已生成但判官 pending 计入 N、不算通过；任何一次红线违规都使该用例直接失败。

### 10.6 判定与聚合

第一版每条语义判定只运行一次，失败或 pending 可人工复核。由于没有系统测量判官自身抖动，报告只能把多次 Agent attempt 的差异作为生成侧读数，不能声称已经验证判定侧稳定性。

同一 `attempt + criterion` 可以有多条 Judgment，但不用“最后写入覆盖”。EvaluationRevision 显式绑定当前生效的判定；人工裁决通过替换绑定生效，所有旧结果保留。聚合规则单独版本化，并由 EvaluationRevision 固定引用。

### 10.7 报告与基线

报告是可重建的投影，不是事实源。事实源包括 ExecutionRun、Attempt、Judgment、版本化聚合规则、诊断结论与人工复核记录；报告可以缓存，但必须能从这些对象重新计算。

每个互不重叠的评测范围只保留一个当前基线，历史基线可留存。只有**已封存、所有必跑用例达到有效 N、判定完成且无 pending／基础设施缺口**的 ExecutionRun + EvaluationRevision 才能设为基线。

基线比较前检查 suite、case revision 集合、collection 快照、运行策略和聚合规则是否一致。模型、prompt、循环或工具可以作为被比较变量，但不能同时改变现场；不满足条件时标记为“跨场比较”。

### 10.8 存储与保留

所有 attempt 长期保留轻量记录：输入引用、配置、答案、判定、usage、时间戳和 `trace_id`。失败、pending 与基线关联的 attempt 保留完整 trace 和 artifacts；普通通过项默认只保留 trace 摘要。清理前允许从 Web UI 导出诊断包，保留策略可配置。

**后续详细设计：**数据库表与事务边界、ExecutionRun 状态转换的并发控制、checker 接口、JudgeInput schema、报告指标公式、成本价目表、artifact 路径和具体保留天数。

---

* 08 · 评测台设计 · 2026-10-20 · 状态：已审核（2026-10-20），作为当前实现基线 *
