# 024B · 基线管理与跨 run 比较

- 状态：已完成
- 开始日期：2026-10-24
- 关联设计：[08 评测台](../../design/08-evaluation-console-design.md)
- 前置切片：[024A 评测结果矩阵](./024A-evaluation-result-matrix.md)
- 人工验收：[024B 基线与比较人工验收](../manual-tests/024B-evaluation-baseline-comparison.md)

## 控制契约

### 用户可观察目标

1. 用户只能从已封存、有效样本完整且判定完整的正式 run 中人工设置当前基线。
2. 同一 suite identity 与 run type 只有一个当前基线，设置新基线后旧基线历史仍可查看。
3. 用户可以查看当前基线的 run、EvaluationRevision、collection snapshot、用例集与设置人。
4. 用户可以把另一 run 与当前基线比较，看到 case 结果翻转、质量分布、X 红线、token 和调用量差异。
5. 当 suite revision、case revision 集合、collection snapshot、运行策略或聚合规则不一致时，界面明确标记“环境已变化，仅供排查，不可直接归因”，不把它当作直接回归结论。

### 核心不变量

1. baseline 记录 append-only；当前基线通过独立 pointer 切换，切换不修改旧记录。
2. 基线资格只由后端确定性校验，前端按钮状态不能替代校验。
3. 当前基线按 `suite_id + run_type` 唯一；exploratory run 与非 formal run 永远不能成为基线。
4. Baseline 冻结设置时的 ResultMatrix 投影；同一结果现场重复设置保持幂等，人工改判产生新结果现场后允许追加新基线，既有基线快照不可改写。
5. 同场比较要求 suite revision、case revision 集合、collection snapshot、运行策略和聚合规则一致；模型、prompt、循环或工具配置可以作为被比较变量。

### 明确禁止

- 不自动选择或替换基线；
- 不把 sealed partial、canceled、pending、有效 N 不完整或判定不完整的 run 设为基线；
- 不把跨 collection snapshot 或不同 case 集合的差异写成单变量回归；
- 不覆盖或删除历史 Baseline；
- 不修改已封存 run、EvaluationRevision、Judgment 或 aggregate。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 非正式或未封存 run | “该 run 不能设为基线”及原因 | run 与现有基线 | 是，完成或重新运行 |
| 有效 N 或 pending 不完整 | 明确列出缺口 | aggregate、binding、trace | 是，补跑或复核 |
| 缺少当前基线 | 显示“尚无基线”，比较入口不可用 | 所有历史 run | 设置基线后恢复 |
| 跨场比较 | 结果可见但显示不可直接比较的字段差异 | 两份 run 与版本信息 | 是，统一现场后重跑 |
| 设置同一 run 且结果现场未变化的基线 | 幂等返回已有基线，不重复创建 | baseline 与 pointer | 是 |
| 人工改判后设置同一 run 的新结果现场 | 追加新基线并切换当前指针，保留旧基线 | 改判前后两份 baseline | 是 |
| 非同一 baseline scope 比较 | 不返回误导性的跨 suite 结果 | 两个 run | 在目标 suite 设置基线 |

### 人工检查点

- 编码前确认 scope、资格规则和同场条件。
- 迁移后确认旧 eval.db 自动升级且旧 run/revision 可读。
- 后端完成后用真实完整 run 设置基线并查询当前基线。
- 前端完成后设置基线、查看历史、切换并与第二个 run 比较。
- 最后检查“跨场比较”不会显示成直接通过／回归结论。

### 决策预算

- 预计 B 级决策：5 项。
- C 级决策：0 项。
- 超过预算时拆分：024B1 基线设置与当前指针；024B2 比较投影与差异 UI。

## 目标

把 024A 的结果矩阵提升为可人工确认的版本基线，并提供同一评测范围内可解释的跨 run 差异，为正式回归和发布判断提供入口。

## 输入与输出

输入：

- 已封存 ExecutionRun；
- 完整 EvaluationRevision 与 024A ResultMatrix；
- suite scope、case revision、collection snapshot、版本与运行策略；
- 人工设置基线的 reviewer 与 reason。

输出：

- append-only Baseline；
- 每个 `suite_id + run_type` 的当前 Baseline pointer；
- 当前基线 API；
- target run 与 baseline 的比较投影；
- 评测历史中的基线设置和比较界面。

## 不包含

- 自动设置基线；
- 发布门禁自动阻断或自动回归判定；
- 结果矩阵图表、报告导出和长期趋势；
- 跨 suite 比较；
- 判官一致性统计或上游失败率报告。

## 验收条件

- [x] baseline 与 pointer 跨重启持久化，旧 baseline 记录不可修改或删除。
- [x] 不符合资格的 run 在 API 层被拒绝，且给出稳定原因。
- [x] 同一 suite＋run type 设置新基线后 pointer 切换，历史 baseline 仍存在。
- [x] 同场比较准确列出 case 结果翻转、计数与成本差异。
- [x] 跨场比较显示具体不一致项，不声称单变量回归。
- [x] 当前 run 是基线时可识别；无基线时有明确空状态。
- [x] 迁移、后端测试、前端检查、构建和完整质量门通过。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 基线作用域 | 当前基线按 `suite_id + run_type` 唯一，显示 suite scope_id 作为业务范围 | 08 要求每个互不重叠评测范围保留一个当前基线，并区分质量与延迟 | 只用 scope_id 会让不同 suite 共享基线；只用 run_id 没有当前指针语义 | 决定 pointer 主键、API 查询和 UI 过滤 | 同 suite 切换、跨 suite 隔离、质量／延迟隔离测试 | 已确认 |
| D2 | B | 基线资格 | 仅 formal、sealed_complete、EvaluationRevision=complete、所有 case valid N 完整且无 pending 的 run 可设基线 | 08 基线资格与 023A／023B 样本语义 | 允许 partial 会把缺口写成稳定基线；允许 exploratory 会破坏统计语义 | 后端统一返回不可设原因 | 资格组合测试与真实 run 设置 | 已确认 |
| D3 | B | 基线历史与当前指针 | Baseline 行 append-only，并冻结设置时的 ResultMatrix 快照；单独 pointer 表按 scope 指向当前 baseline；幂等键为 `run + revision + ResultMatrix 哈希`，人工改判产生新结果现场后追加新基线 | 08 人工设置、历史保留；ADR 0007 要求可恢复和可审计；人工改判既不能反写旧基线，也不能迫使重跑 run 才能重新确认结果 | 只按 revision 幂等会阻止人工修正生效；覆盖旧基线会破坏审计；每次重复提交相同结果会制造伪历史 | 同一结果幂等、改判后追加、旧基线保留且当前指针切换 | 迁移、不可变 trigger、重复设置、切换和基线设置后改判测试 | 已确认 |
| D4 | B | 比较兼容性 | 同 suite revision、case set、snapshot、run policy 与 aggregate rule 为同场；模型／prompt／loop／tool 可作为比较变量；不满足时标记 cross_scene 与差异原因 | 08 同场比较规则 | 禁止跨场比较会让历史结果无用；无提示直接比较会制造错误因果 | 比较结果始终给出 comparable 和 reasons | 同场、snapshot 变化、suite revision 变化和 target 设置变化测试 | 已确认 |
| D5 | B | 前端交互 | 历史矩阵增加基线状态与人工设置表单；比较使用独立“基线比较”视图，显示摘要和 case 差异 | 08 报告与基线模块 | 自动按钮会绕过负责人选择；把比较塞进矩阵会增加判读负担 | 人工入口明确，差异可扫描 | 前端构建与真实界面人工验收 | 已确认 |

## 证据包

### 自动化证据

- 命令：`.\.venv\Scripts\python.exe -m pytest tests\test_evaluation_baseline.py tests\test_evaluation_api.py -q --basetemp=..\.pytest-tmp-024b`
- 结果：8 passed。
- 覆盖的不变量：基线幂等、基线冻结、suite／run type 隔离、历史保留、不可变 trigger、回归 change、跨场原因、pending／exploratory 拒绝。
- 命令：`corepack pnpm quality:quick`
- 结果：通过；包含前后端静态检查、前端构建和桌面 Rust 检查。
- 命令：完整后端测试 `pytest backend\tests -q --basetemp=.pytest-tmp-024b-final`
- 结果：238 passed；使用全新的工作区 basetemp 运行。
- 命令：`backend\.venv\Scripts\python.exe -m pytest backend\tests\test_evaluation_baseline.py backend\tests\test_evaluation_api.py backend\tests\test_evaluation_core.py -q`
- 结果：32 passed，包含同一 revision 改判后追加新基线且旧基线不变。
- 命令：`corepack pnpm review:check`
- 结果：通过。

### 运行证据

- 启动方式：复制当前真实 eval.db 后进行 SQLite 在线备份，不触碰运行中的原件。
- 迁移前备份：`%LOCALAPPDATA%\com.lipeidong.aistudyhelper\eval-data\db\eval.db.before-024b-20261024`。
- 用户操作序列：schema 5→6 迁移；设置 `run_5cb…` 为基线；比较 `run_861…`；再以 `run_8acf0145a8eb44dfaa4fa52f804e5fd8` 与另一 collection snapshot 的基线做跨场检查；最后以真实库副本执行 schema 6→7 迁移。
- 预期结果：旧行数不变；新增 baseline 表与结果哈希；基线 case_count=1；比较识别 1 个 case 回归；迁移后 append-only trigger 仍存在。
- 实际结果：迁移和比较均符合预期；schema 6→7 副本迁移后为 14 runs／91 attempts／13 revisions／120 judgments／1 baseline，原基线自动补齐 `result_matrix_hash`，两个 append-only trigger 均保留。

### 失败探针

- 注入或复现的失败：pending／exploratory run 尝试设为基线被拒绝；baseline 行 UPDATE／DELETE 被 SQLite trigger 拒绝；collection snapshot 不一致时比较返回 `collection_snapshot_changed`。
- 系统保留的旧状态：拒绝操作不新增或覆盖 baseline；跨场比较仍保留双方 run 与各自冻结结果。
- 用户看到的错误：不合格 run 显示具体不可设原因；跨场界面显示“环境已变化，仅供排查，不可直接归因”。

### 人工验收

- [x] 负责人确认基线切换后旧历史保留、无基线空状态清晰，并接受人工改判后的重新基线化由自动化测试覆盖。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么 Baseline 除了引用 EvaluationRevision，还必须冻结当时的 ResultMatrix？ | 人工改判会追加 Judgment 和 binding，并使当前 ResultMatrix 重算；基线冻结的是人工确认时点的结果现场，否则历史基线会随改判漂移，无法审计原始基线。 | 已通过 | 改判后按新结果哈希追加基线并切换 pointer，旧基线保持不变。 |
| R2 | 为什么当前基线要按 suite＋run type 分域，且 exploratory／pending 不能设为基线？ | suite 不同则用例不可比；run type 区分质量与延迟两种评测目的；exploratory 和仍含 pending 的 run 没有最终稳定结果，不能作为稳定比较基准。 | 已通过 | 完整延迟指标与延迟基线闭环尚未实现，已转入后续待办 BL-0008。 |
| R3 | collection snapshot 或 case revision 集合不同时，为什么仍展示差异但不能称为单变量回归？ | 单变量回归中的变量指 Agent 实现变化；collection snapshot 是输入资料变化，case revision 是用例／金标准变化，都属于 Agent 外部条件。展示差异用于排查非 Agent 原因，但不能把结果变化归因于 Agent。 | 已通过 | 比较结果继续显式标记环境变化，不进入单变量回归结论。 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 协作复盘

- AI 明显加速的环节：baseline schema、pointer、冻结快照、比较投影和前端基线段落可以围绕 024A 事实链快速实现。
- 负责人失去系统模型的环节：最初容易把“冻结基线”误解成“禁止人工改判”；通过 R1 明确为冻结人工确认时点的结果现场，并允许改判后的新现场追加基线。
- 真正发现问题的控制点：真实库副本迁移和 `run_5cb → run_861` 比较探针同时验证了 schema、资格和差异语义。
- 只有仪式感、没有产生判断价值的流程：本切片未发现，离线迁移探针与真实界面验收都发现了具体风险或确认了关键语义。
- 下一次准备调整的一到两项：基线设置表单的信息量，以及比较页对大量 case 的折叠方式。

### 本次试行的流程规则

| 规则／假设 | 来源 | 想防住的失败 | 使用成本 | 实际效果 | 下次决定 |
| --- | --- | --- | --- | --- | --- |
| 基线与当前指针分离 | 08／ADR 0007 | 切换基线时丢失审计历史 | 多一张表与事务处理 | 自动化切换与历史保留测试通过，人工确认旧历史可见 | 保留 |

## 已知风险与后续

- 未验证项：完整首字延迟指标、延迟聚合与延迟基线闭环，已登记 [BL-0008](../backlog.md)。
- 接受的风险：比较一次返回同 suite 的全部 case；当前规模足够，暂不分页。
- 后续待办或 ADR：自动回归门槛、结果趋势与报告导出；完整延迟评测闭环见 [BL-0008](../backlog.md)。
