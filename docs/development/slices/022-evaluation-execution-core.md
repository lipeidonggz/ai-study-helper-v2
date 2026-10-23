# 022A · 评测执行内核与只读用例目录

- 状态：已完成
- 开始日期：2026-10-22
- 关联设计：[03 用例与金标准](../../design/03-cases-and-gold-standard.md)、[06 Agent Runtime](../../design/06-retrieval-pure-agent.md)、[08 评测台设计](../../design/08-evaluation-console-design.md)、[ADR 0009](../../decisions/0009-development-shell-with-compiled-evaluation.md)、[ADR 0010](../../decisions/0010-evaluation-case-and-suite-revision-workflow.md)
- 后续切片：[022B 评测用例、用例集编写与快速试跑](./022B-evaluation-authoring.md)
- 前置切片：[018 Agent Runtime](./018-agent-runtime-core.md)、[020 流式问答](./020-streaming-chat.md)、[021 Trace](./021-trace-persistence.md)
- 人工验收：[022A 评测执行内核人工验收](../manual-tests/022-evaluation-execution-core.md)

## 控制契约

### 用户可观察目标

1. 开发者从评测构建进入“评测”工作区，选择当前 suite revision 和冻结 collection 快照。
2. 启动前可看到 ExecutionPlan 的预检结果、版本、预算、并发、用例数和预计 attempt 数。
3. 启动后可看到各用例 attempt 进度，并能暂停、继续或取消 run。
4. 已完成 attempt 可查看答案、usage、耗时、状态、失败原因和关联 trace。
5. 应用重启后，已封存 run、attempt 和 trace 关联仍可查看。

### 核心不变量

1. 评测 runner 与产品 API 复用同一个 Agent 执行服务、prompt、工具、预算和 trace 实现，禁止复制第二条问答链。
2. ExecutionPlan 固化 suite／case revision、collection snapshot、Agent／模型版本、预算、并发和实验变量；运行中不可改变。
3. 每个 attempt 都从全新 Agent 会话开始，使用计划中固定的 snapshot，不继承产品会话或其他 attempt 的上下文。
4. 评测数据库、trace 和运行文件位于独立 eval data root，不写产品 SQLite、collection 或用户 trace。
5. attempt 在封存后不可修改；基础设施失败保留为无效 attempt，不能静默补写成成功样本。

### 明确禁止

- 不通过正在运行的产品应用或产品 HTTP API 执行评测；
- 不把金标准、目标文档、干扰文档标记注入 Agent 提示词或检索白名单；
- 不让评测模块进入产品发布包；
- 不在 022A 实现可变草稿、用例修订审核、suite builder 或快速试跑；这些转入 022B；
- 不在 022A 实现 LLM 判官、评分、N／K 通过率聚合和基线比较；
- 不自动重发结果未知的 attempt。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 预检失败 | `invalid_plan` 与具体失败项 | 候选 plan、失败原因 | 修复后创建新 plan |
| Agent attempt 失败 | 无效 attempt 与错误码 | 状态、usage、部分 trace | 是，按策略创建替代 attempt |
| trace 缺失 | attempt 仍保留，标记证据不足 | 答案、usage、`trace_id` | 可继续查看，但不能做完整归因 |
| run 暂停 | 当前 attempt 完成后进入 paused | 已完成 attempt | 可继续 |
| 用户取消 | 当前 attempt 完成边界后 canceled | 已完成回答与 usage | 可新建 run |
| 进程重启 | running attempt 标为 outcome_unknown | 已落盘 attempt 与 trace | 创建替代 attempt |

### 人工检查点

- 编码前确认切片边界、数据模型、调度策略和最小 UI；
- 共享 Runtime 与指定 snapshot 绑定完成后先做契约验证；
- 执行主路径完成后查看一次成功与失败 run；
- 最后验证暂停／取消、重启恢复和产品数据隔离。

### 决策预算

- 预计 B 级决策：5 项。
- C 级决策：无。
- 超过预算时的拆分方式：022A 执行内核、只读目录与执行工作台；022B 用例／用例集编写、版本审核与快速试跑。

## 目标

建立独立评测数据事实源和执行链，使用冻结 suite、case、collection 快照和 Agent 版本运行端到端 attempt，并提供最小可视化运行工作台。

## 输入与输出

输入：

- 已审核 suite revision 与 case revisions；
- 冻结 collection snapshot；
- 固定 Agent／prompt／模型／工具／预算版本；
- N／K、并发、总预算和唯一实验变量。

输出：

- 独立评测 SQLite；
- ExecutionPlan、ExecutionRun、Attempt 和关联 eval trace；
- 预检、运行进度、暂停／取消／封存 API；
- 最小评测工作台和 attempt 详情。

## 不包含

- checker、LLM judge、人工判定和评分；
- N／K 通过率聚合与结果矩阵；
- Baseline 与回归比较；
- 诊断包导入／导出；
- 自动生成用例或自动修改 Agent。

## 验收条件

- [x] 产品 API 与评测 runner 调用同一共享 Agent 执行服务。
- [x] 评测可按指定 `collection_snapshot_id` 只读绑定，不跟随活动指针变化。
- [x] 18 条设计用例以不可变 case revision 被 suite revision 引用。
- [x] ExecutionPlan 固化全部版本、预算、并发和实验变量。
- [x] Run 支持 planned／running／paused／canceled／sealed_complete／sealed_partial。
- [x] 每次 attempt 使用全新 Agent 会话并关联 eval trace。
- [x] 应用重启后 run 和 attempt 状态可恢复，未知结果不自动重发。
- [x] 最小工作台可预检、启动、暂停、取消、查看 attempt 和打开 trace。
- [x] 产品发布构建不包含评测 UI、评测 sidecar 或 `/api/eval`。
- [x] 完整质量门和真实开发版人工验收通过。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 022 交付边界 | 共享 Runtime 工厂、指定 snapshot 绑定、Case／Criterion／Suite revision、18 条用例种子和最小用例库、ExecutionPlan／Run／Attempt、端到端执行、暂停／取消／封存、重启恢复和 attempt trace；自动判定、通过率、判定结论和基线比较留后续 | 08 主工作流和 MVP 边界 | 没有不可变用例与 suite 就没有稳定评测输入；一次加入判官又会混入语义判定故障 | 本切片闭合从选用例到封存执行现场的链路 | 18 条 revision、suite、plan 到 attempt 的端到端测试 | 已确认 |
| D2 | B | 评测台构建边界 | 同一开发壳，显式 `eval-console` 编译特性；独立评测 sidecar 和 `eval-data`；发布构建完全移除评测 | ADR 0009；08／09 已同步 | 两个独立 Tauri 应用重复宿主设施；单包始终携带评测违反发布边界 | 决定目录、启动方式与发布检查 | 产品／评测产物扫描和双数据根测试 | 已确认 |
| D3 | B | 评测数据库与 revision 模型 | 独立 `eval.db`：稳定 Case／Criterion／Suite 身份＋不可变 revision；金标准拆成有序 CriterionRevision；Case revision 保存问题、逻辑目标文档／章节、干扰元数据、N／K 和执行策略；Suite revision 只引用 active revision；ExecutionPlan 把目标文档解析为指定 snapshot 内的具体文档版本／章节；review lifecycle 单独允许 draft／reviewed／active／retired，内容变化必须新建 revision | 08 要求 SQLite 事实源、不可变 revision、ExecutionRun／Attempt，评测元数据不能进入 Agent 输入 | 把当前用例直接当可变记录会破坏历史复现；单个 JSON blob 难以约束 criterion 和 review 状态 | 决定 schema、迁移、seed 与历史追溯 | migration、revision 不可修改、active-only suite、目标现场解析和 foreign key 测试 | 已确认 |
| D4 | B | 执行与调度策略 | 单 eval sidecar 同时一个 ExecutionRun；quality 使用全局 attempt worker pool，并发可配 1–32、默认 8；latency 固定并发 1；每个 attempt 全新会话；N／K 取 case revision；token／请求／替代 attempt 设硬预算；暂停／取消在 attempt 安全边界生效；429／持续上游失败暂停启动新 attempt；重启遗留 running attempt 标 `outcome_unknown` 且不自动重发 | 08 要求全新会话、硬预算、并发质量评测和独立串行延迟评测 | 1–3 对 180 次 attempt 过慢；无上限会放大限流、成本和恢复复杂度 | 质量评测吞吐明显提高，同时保留可控成本与延迟测量纯度 | worker pool、并发范围、暂停取消、429、预算和 outcome_unknown 测试 | 已确认 |
| D5 | B | 用例种子与只读目录 | 将设计 03 的 18 条用例幂等导入为稳定 ID 和 r1 active revision，R／X 金标准拆成 criterion revisions 并记录 source/hash/review；工作台只读展示用例库、suite revision、预检、执行进度和 trace。草稿编辑、修订审核、suite builder 与快速试跑转到 022B | 03 的 18 条用例；08 第一版六类视图；ADR 0010 | 022A 同时实现编写工作流会让执行内核验收继续膨胀；只读种子目录已足够支撑执行主路径 | 收窄 022A 范围并保留 022B 明确入口 | 18 case revision、seed hash、只读 suite、预检、运行与 attempt 详情验收 | 已确认；已按 022A／022B 重新划线 |

## 证据包

### 自动化证据

- `corepack pnpm quality:quick`：通过。
- `cargo clippy --features eval-console -- -D warnings`：通过。
- `pytest backend/tests`：199 条测试全部通过。
- 新增测试覆盖：
  - `test_agent_factory.py`：产品与评测共用冻结设置／prompt 的 Runtime 工厂。
  - `test_evaluation_core.py`：18 条种子、73 个 R／X 判定项、不可变 revision、active-only suite、并发、硬预算、暂停／恢复、封存和重启恢复。
  - `test_evaluation_api.py`：独立数据根、评测路由隔离、预检失败不可启动。
  - `test_retrieval_read_layer.py`：显式 snapshot 绑定不跟随活动指针。
- 产品前端构建扫描：`评测执行台`、`eval_backend_connection`、`DEVELOPMENT ONLY` 命中数为 0。
- PyInstaller 产品 sidecar 构建：通过，1,524 个文件、404.6 MB；产物中不存在评测 sidecar、`eval_migrations`、`eval_main`、`app/evaluation` 或 `/api/eval` 标记。

### 运行证据

- 使用真实产品数据根和独立临时 `eval-data` 启动评测 sidecar：
  - schema version：`1`；
  - suite：`sr_design03_r1`；
  - case revision：18；
  - criterion revision：73；
  - 当前冻结快照：`s_6b787258ba7d700c`，15 篇文档；
  - ExecutionPlan 预检：`planned`，失败项 0，预计 180 次 attempt；
  - 探针未调用 LLM，未产生模型费用。
- 探针前后产品 `application.db` 与活动 snapshot manifest 的修改时间未变化；评测 SQLite 只写入独立 `eval-data/db/eval.db`。

### 失败探针

- `invalid_plan`：不存在的 snapshot 返回可操作失败项，启动接口返回 409，未创建 attempt。
- `total_budget_exhausted`：预算不足以预留一个 attempt 时 run 进入 paused，attempt 数保持 0。
- 进程重启：遗留 running attempt 被标记为 `outcome_unknown`，run 进入 paused，不自动重发。
- 用例内容修改：SQLite trigger 拒绝原地更新不可变 revision。

### 人工验收

- [x] 按 [022A 人工验收清单](../manual-tests/022-evaluation-execution-core.md)执行真实开发版验证。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么评测不通过产品 HTTP API 运行？ | 产品 HTTP API 是入口适配层；评测需要冻结指定 snapshot、prompt／设置、独立 trace 和并发控制，直接走 API 会迫使产品接口承载评测控制面。评测测量共享 Agent 核心在冻结现场中的行为，HTTP／前端契约由产品侧独立测试。 | 已确认 | 后续如需要发布前黑盒验证，另加少量 API smoke tests，不与 Agent 能力评测混用 |
| R2 | ExecutionPlan 固定了哪些事实，为什么运行中不能修改？ | 固定 suite／case revision 集合、指定的 collection snapshot、Agent 参数、prompt／模型／工具等版本、N／K、并发、预算和唯一实验变量。它们共同决定 Agent 行为、执行成本与速度；运行中修改会让同一 run 的 attempt 不再处于同一现场，导致结果无法解释、恢复不能续接且无法比较。 | 已确认：负责人正确指出控制 Agent 的参数与已入库快照会影响行为，未固定就会混入不可控变量；补充评测输入身份和运行策略同属冻结范围 | 运行中不得修改 plan；需要改变现场时新建 plan／run |
| R3 | 基础设施失败为什么不能算作答错，也不能自动静默重发？ | 基础设施失败不反映 Agent 的回答能力，算作答错会把供应商或进程故障误判为质量问题。静默重发也不能接受：失败会改变有效 N 和通过率，重试增加成本与抖动，且崩溃后的 `outcome_unknown` 可能已经产生结果，自动重发会生成重复或未知样本。失败 attempt 必须保留，替代 attempt 必须显式记录关系并在预算内执行。 | 已确认：负责人正确指出不可控基础设施故障不能算答错；补充静默重发还破坏样本完整性、因果归因与恢复语义，成本只是其中一个约束 | 基础设施失败不计入有效 N；替代 attempt 显式留痕并受预算限制；`outcome_unknown` 永不自动重发 |

## 协作复盘

- AI 明显加速的环节：共享 Runtime 工厂、独立评测库／迁移、执行器测试和编译期构建边界能够并行快速成形。
- 负责人失去系统模型的环节：原设计对草稿编辑、用例升级和 suite 版本关系描述不足，负责人无法从既有文档判断预期的版本管理操作。
- 真正发现问题的控制点：真实快照预检、逐项人工验收和反向解释暴露了“180 次成本”“suite 选择只能全选”和版本语义未定问题。
- 只有仪式感、没有产生判断价值的流程：本轮没有发现需要保留的纯仪式项；显式列失败语义和执行样本状态均有实际判断价值。
- 下一次准备调整的一到两项：领域语义未收敛前不先扩展实现；新增功能先确认用户操作路径，再确定数据模型和切片边界。

## 已知风险与后续

- 未验证项：真实模型批量运行完整 180 次时的上游限流、总成本和长期稳定性；本切片使用小批量真实调用和确定性测试覆盖控制逻辑。
- 接受的风险：评测首次启动需读取设计 03 基线并初始化独立 `eval.db`；开发机必须保留该文档。
- 安全边界：评测只读取产品 collection 快照；产品 Key 仍从 Windows Credential Manager 读取，不复制到评测库或前端。
- 后续：022B 用例／用例集编写与快速试跑；之后再进入 checker／judge、结果矩阵、基线比较和诊断包。

<!-- reverse-explanation-gate: passed 3/3 -->
