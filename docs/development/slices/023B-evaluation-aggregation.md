# 023B · 生效判定绑定与 N/K 聚合

- 状态：已完成
- 开始日期：2026-10-23
- 关联设计：[03 用例与金标准](../../design/03-cases-and-gold-standard.md)、[08 评测台设计](../../design/08-evaluation-console-design.md)
- 前置切片：[023A Checker 与 LLM Judge](./023A-evaluation-judgment.md)
- 人工验收：[023B 聚合人工验收](../manual-tests/023B-evaluation-aggregation.md)

## 控制契约

### 用户可观察目标

1. 用户可查看每个有效 attempt 的逐 criterion 生效结果，并能人工改判指定 `attempt + criterion`。
2. 人工改判必须新增 human Judgment 并切换显式生效绑定；原 checker／LLM Judgment 继续可查看。
3. 用户可看到 attempt 的 `pass / fail / pending`，以及 case 在 N 次有效样本上的 `pass / fail / pending / exploratory`。
4. case 结果同时展示 R 通过数、X 违规数、跨项检查失败／pending 和距离 K 的缺口，不允许只显示一个总分。
5. 修改某条有效绑定后，attempt 和 case 聚合即时按事实重算，并明确标记当前结果仍属于哪个 EvaluationRevision。

### 核心不变量

1. 底层 Judgment、CheckResult 与人工复核都只追加；人工改判新增 `executor=human` 记录，不更新或删除旧事实。
2. 聚合只读取显式生效绑定。缺少绑定、checker／judge 为 pending 或必备事实缺失时，不得当成 pass。
3. 单次 attempt：全部生效 R 为 pass、全部生效 X 为无违规、无失败跨项检查时才能 pass；任一 R fail、X 违规或跨项检查 fail 时为 fail；无 fail 但存在 pending／缺绑定时为 pending。
4. 正式 case：`N` 个有效产品样本中至少 `K` 个 attempt pass，且所有 N 个样本的 X 违规和跨项检查 fail 均为零，才能 pass。任一红线违规时 case 直接 fail，不能用其他通过样本抵消。
5. `N<K` 的快速试跑只展示探索性分布，不生成正式 pass／fail，也不能成为基线候选。

### 明确禁止

- 不覆盖、删除或重写 checker／LLM／human Judgment；
- 不人工改判 checker Judgment；checker 有错时必须修复 checker 后重判；
- 不让人工改判脱离 `attempt + criterion` 指向整条 case 或整次 run；
- 不把 pending、缺失绑定、基础设施失败或未执行样本折算成 pass；
- 不用多数通过抵消任何 X 红线违规；
- 不在本切片实现结果矩阵、基线比较、诊断建议或判官校准工作台；
- 不自动设置基线，不自动修改 case、criterion 或 Agent。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 某 criterion 无生效绑定 | attempt 为 pending，并列出缺失项 | 全部原 Judgment 与 bindings | 可人工补判或重判 |
| 人工 Judgment 与目标不匹配 | 操作被拒绝并说明事实不一致 | 原绑定与原 Judgment | 是 |
| R fail 与 pending 并存 | attempt fail，原因列出失败项 | pending 与 fail 原事实都保留 | 人工改判或重判 |
| X 违规 | attempt／case fail，红线不可被 K 抵消 | 违规 evidence 与原文 | 可改判，但需追加 human Judgment |
| 跨项检查 fail／pending | 分别显示红线失败或聚合阻塞 | CheckResult 原事实 | 修复后重判 |
| N／K 事实缺口 | case 显示 pending 或 exploratory | 原始 N、K、有效样本数 | 补跑或调整试跑计划 |

### 人工检查点

- 编码前确认人工改判 API、生效绑定历史和聚合优先级；
- 主路径打通后检查一次 pass、一次人工改判和一次 pending；
- 完成后用真实 EvaluationRevision 验证非探索与 N<K 两种 case 结果。

### 决策预算

- 预计 B 级决策：5 项；
- C 级决策：无；
- 超过预算时拆为 023B1 人工绑定与 023B2 N/K 聚合。

## 目标

把 023A 的逐项 Judgment 转换为可审计的生效判定、单次 attempt 结果和 N/K case 结果，并提供必要的人工纠错入口。

## 输入与输出

输入：已结束的 EvaluationRevision、逐 attempt×criterion Judgment、CheckResult、case 的 N／K、有效样本分类。

输出：append-only human Judgment、显式 effective judgment bindings、attempt aggregate、case aggregate、聚合失败原因和 UI 投影。

## 不包含

- 结果矩阵和 suite／类别级汇总；
- Baseline 选择、跨 run 比较和回归；
- DiagnosisSuggestion／Conclusion；
- 判官重复一致性工作台；
- 自动修复 Agent、prompt、case 或 criterion。

## 验收条件

- [x] 每个有效 attempt 的 criterion 都能解析到唯一生效绑定。
- [x] 人工改判追加 human Judgment 与 binding，旧记录不变。
- [x] attempt 与 case 聚合严格执行 R／X、pending 和红线优先级。
- [x] N<K 只产生 exploratory 分布。
- [x] UI 能展示绑定、人工改判入口和聚合缺口。
- [x] 完整质量门和真实开发版人工验收通过。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 生效绑定的数据结构 | 新增 append-only `effective_judgment_binding` 历史；同一 `evaluation_revision + attempt + criterion` 以最新 binding 为当前生效项，人工改判只追加新 binding | 03 A4／A5；08 禁止最后写入覆盖 | 布尔覆盖字段会丢失审计；每次人工改判创建新 EvaluationRevision 又会让聚合结果频繁换容器 | 决定 schema、查询和 UI 当前绑定 | 多次改判后旧绑定仍存在、当前绑定唯一 | 已确认 |
| D2 | B | 人工改判输入与权限 | 人工只能改判 LLM 产生的语义结果：criterion `evaluator=llm_judge` 的 Judgment，以及 LLM 型 CheckResult。criterion 改判必须指定 attempt、criterion、R/X 合法 verdict、reason、reviewer，系统新增 human Judgment 并绑定；人工不能改 checker Judgment 或程序型 CheckResult，确定性规则有错时修复实现后重判 | 08 人工复核与事实分层；沛东确认人工只纠正 LLM Judge | 允许人工改 checker 会把确定性事实变成主观覆盖；只填备注无法参与聚合 | 决定 API、UI 与审计字段 | 非法 verdict、checker 改判、跨 revision、缺 reason 均拒绝 | 已确认 |
| D3 | B | attempt 聚合优先级 | 任一 R fail、X violated 或 CheckResult fail ⇒ fail；否则任一 R／X pending、CheckResult pending 或缺失 binding ⇒ pending；全部满足才 pass | 03「R 全满足、X 零违反、unknown 为 pending」 | pending 优先于 pass 会隐藏已有失败；fail 优先于 pending 能保留确定失败 | 决定单次尝试结果与缺口语义 | R/X/pending/check fail 组合测试 | 已确认 |
| D4 | B | case N/K 聚合 | `N<K` 为 exploratory；正式 case 在 N 个有效产品样本中 `pass_count≥K` 且全样本 X／跨项红线违规为零时 pass；任何红线违规直接 fail；否则若 `pass_count+pending_count≥K` 为 pending，否则 fail | 03 A6；08 N/K 与红线规则 | 简单多数会允许红线被平均；忽略 pending 会制造虚假通过 | 决定 case 结果与基线资格前置条件 | N/K、红线、pending 和基础设施缺口的组合测试 | 已确认 |
| D5 | B | 跨项 CheckResult 的聚合与人工纠正 | CheckResult 作为 attempt 级全局检查参与聚合：fail 为红线失败，pending 阻塞 pass，pass 不增加分数。`citation_integrity` 等程序检查不可人工修改；`citation_support` 等 LLM 检查允许逐 block 新增 human review 与 effective check binding，原 LLM 结果保留，由程序按人工逐块 verdict 重算聚合。截断事实已作为 criterion 时不重复计为全局红线 | 03 通用边界；023A 的 CheckResult 分层；沛东确认 LLM Judge 可人工纠正 | 完全排除会允许伪引用通过；直接改总分无法审计且会绕过 block 完整性；把所有 CheckResult 都视为不可改会忽略 LLM 误判 | 决定 CheckResult 聚合、人工复核与 UI 阻断原因 | 引用 fail／pending／无引用、人工逐 block 重算、block 集合校验及 cf-003 不重复计数测试 | 已确认 |

## 证据包

### 自动化证据

- `pytest backend/tests`：234 条测试全部通过。
- `corepack pnpm quality:quick`：通过。
- 产品模式和 `VITE_EVAL_CONSOLE=true` 评测模式前端构建通过。
- 覆盖：
  - 旧 Judgment 迁移后自动生成初始 binding；
  - 连续两次人工改判保留全部 Judgment／binding，并只让最新 binding 生效；
  - checker Judgment、程序型 CheckResult 拒绝人工覆盖；
  - citation_support 逐 block 人工复核后重算有效检查结果；
  - attempt 的 fail／pending 优先级、跨项红线，以及 case 的 N/K、pending 与 exploratory 分支。

### 运行证据

- 真实 eval DB 从 schema 4 迁移到 schema 5：
  - 25 条历史 Judgment 全部回填为 25 条初始 binding；
  - active suite 保持 `sr_design03_minimal_judge_v2`；
  - 迁移前备份：`eval.db.before-023b-20261023`。
- 真实 revision `evalrev_ddc888e0510c4f148be53a4daea995a4` 完成：
  - R1 原 LLM pass、human fail、human pass 三条 Judgment 与三条 binding 并存，最新 human pass 生效；
  - citation_support 追加人工 fail review，原 LLM pass 保留；
  - attempt 因人工引用支持 fail 聚合为 fail；
  - run 为 N=1、K=9，case 聚合为 exploratory。

### 失败探针

- 连续人工改判：3 条 binding 保留，仅最新一条 `is_effective=true`。
- 人工覆盖 checker：服务拒绝并保留原 Judgment。
- 人工覆盖 citation_integrity：服务拒绝；citation_support 只接受完整 block 集合。
- Case 红线：任一有效 attempt 出现 X 违规时 case 为 fail，即使其他样本通过。
- N<K：只返回 exploratory，不产生正式 pass／fail。
- 2026-10-23 formal run `run_ca42b6b380ce43daac2ecc6ee0dbe088` 暴露调度超发：达到 N=10 后又启动 7 个 attempt，封存时取消并误记为基础设施失败。根因是缺口计算只统计已完成样本、未扣除运行中 attempt；修复后回归断言总 attempt 数不得越过目标 N，取消任务记为 unknown 而非 infrastructure。该 run 保留为缺陷现场，不作为 023B 最终验收证据。
- 2026-10-23 跟进 run `run_21e50c302aa84068afc46ddf96589046` 暴露第二个竞态：3 个完成 + 7 个运行中已经在数量上覆盖 N=10，runner 把“无需再启动”误当成“可以封存”，取消了 7 个运行任务。现在只有“无缺口且无运行中任务”才封存；新增“前 3 个快速完成、其余 7 个继续运行”的确定性回归测试。该 run 同样只作为缺陷现场。
- 2026-10-23 修复后 formal run `run_861667d15e764d84b9484dff51cd162e` 执行通过：N=10、K=9，恰好 10 个 attempt，全部 `succeeded / product` 且 trace complete，无替代、取消或基础设施失败。
- 同 run 判定发现两个失败样本由模型伪造 Observation 引起：8 个正常样本的 `scan_term` 均真实返回 3 次命中；两个失败样本没有真实 `scan_term` 执行，却在原始模型输出中自行生成 `count=0` 的 Observation，随后被 Harness 识别为 protocol_error。旧实现仍把伪造响应放回 History，造成下一轮继续依赖假结果。`react-langgraph-v2` 改为协议错误原文不进入 History，仅用“原文已省略，不得伪造 Observation”的占位提示，原始输出继续保存在 trace artifact。
- 2026-10-23 重跑 `run_5cb3608c35a74c40b2199f21fc491ccf`：10 个 attempt 均真实调用 `list_documents` 和 `scan_term`，全部 `scan_term` 返回 3 个命中，无 protocol_error；30 条 Judgment 全部通过。但该 run 的 trace 仍标记 `react-langgraph-v1`，说明运行中的 sidecar 尚未加载 v2，因此只能视为模型未复现，不能作为 v2 修复的运行证据。
- 2026-10-23 `run_14ee6d1b673f4f00ae32f36d8a1ddcb8` 完成 v2 运行探针：单样本 product、trace complete、`react-langgraph-v2`、`chat-v3`；真实执行 `list_documents`／`scan_term`／`list_blocks`，无 protocol_error。

### 人工验收

- [x] 按 [023B 人工验收清单](../manual-tests/023B-evaluation-aggregation.md)执行，全部通过。

## 反向解释

实现完成后由负责人逐题回答。

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么人工改判必须新增 Judgment 和 binding，而不能改旧记录？ | 直接覆盖会丢失判官抖动读数、原始判定现场和人工修改是否合理的依据；根因分析还必须区分 Agent 问题与 Judge 问题，覆盖原始判定等于毁坏现场。 | 已确认：负责人独立说明抖动、审计和根因区分三方面影响 | 人工改判按 append-only Judgment + 新 binding 实现 |
| R2 | 为什么 case 已经有 K 个 pass，仍可能因一次 X 违规而 fail？ | X 是产品定义中不可接受的硬边界；一次红线违规不能被其他通过样本抵消。 | 已确认：负责人指出 X 红线与 R 数量阈值不是同一种取舍 | K 只衡量生成质量稳定性，X 违规保持一票失败 |
| R3 | 为什么 N<K 的探索性结果不能生成正式 pass／fail 或成为基线？ | N<K 无法满足统计判定语义；Agent 引入大模型后具有随机性，只有满足 N/K 的样本语义才可能可靠判断是否 pass。 | 已确认：负责人指出统计语义与模型随机性是根本原因 | N<K 固定输出 exploratory，不能成为基线 |

## 协作复盘

- AI 明显加速的环节：append-only binding、schema 迁移、两级聚合、人工复核 API、按样本诊断 UI 和确定性回归可以快速形成闭环。
- 负责人失去系统模型的环节：正式 N/K 初期同时混入调度超发、提前封存和模型伪造 Observation 三类问题，单看失败数量无法区分层次。
- 真正发现问题的控制点：真实 N=10 执行、逐 attempt trace、原始 LLM 输出、判官输入输出和反向解释；自动化单元测试只能证明已想到的路径。
- 只有仪式感、没有产生判断价值的流程：本轮未发现；重复 formal run 虽成本较高，但连续暴露了两个调度竞态。
- 下一次准备调整的一到两项：付费正式评测前先跑小规模并发探针并确认 trace 版本；诊断 UI 始终以 attempt 为第一层，criterion 视图作为第二层。

## 已知风险与后续

- 人工改判的正确性依赖 reviewer 对证据和 criterion 的理解；
- 长答案引用支持 pending 会阻塞正式 case pass，相关增强见 `BL-0007`；
- 人工复核 citation_support 时必须逐个 block 填写原因，长答案的人工成本可能较高；
- 模型协议违规具有随机性；v2 已隔离伪造 Observation 的上下文污染，但长期发生率仍需观察；
- 样本序号当前由开始时间推导；若后续出现恢复或并发异常，应在数据层固化稳定 ordinal；
- 结果矩阵、基线和诊断在后续切片实现。

<!-- reverse-explanation-gate: passed 3/3 -->
