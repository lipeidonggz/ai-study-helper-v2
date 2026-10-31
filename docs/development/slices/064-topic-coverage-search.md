# 064 · 主题覆盖检索

- 状态：历史切片，已被 [065 的 corpus-grounded 唯一实现](./065-corpus-grounded-topic-coverage.md)取代
- 开始日期：2026-10-29
- 关联设计：[06 检索、Agent 与提示词设计](../../design/06-retrieval-pure-agent.md)
- 前置问题：[063 c13-003 隔离知识语义覆盖](./063-c13-003-semantic-coverage.md)

## 控制契约

### 背景与失败场景

- 触发问题：c13-003 的 Agent 找到一篇最相关文档后即停止，并声明其它文档未检查；连续两轮通用提示词调整都没有改变候选发现行为。
- 用户损失：需要跨文档找全的问题会得到“单篇充分、整体不完整”的答案，用户无法判断知识库中还有哪些互补内容。
- 现有流程为什么不足：`search_blocks` 只承担一次查询的 Top-K 证据召回；当前没有任何工具承担查询展开、跨查询合并和候选去重。
- 本切片负责什么：增加一个以主题为输入、分别生成 dense／BM25 查询、执行两路检索并对合并候选做语义重排的 L1 证据检索工具。
- 本切片不负责什么：不实现自主子 Agent、不做动态无限循环、不修改 c13-003 的金标准、不修改 Judge、不运行其它用例。

### 用户可观察目标

Agent 可以用一次工具调用获得同一主题在多个互补语义面上的完整证据块，并直接用于回答；工具明确承诺高召回，但不伪装成数学意义上的全覆盖。

### 核心不变量

1. 工具输入是主题，不是完整用户问题；不接受具体评测用例的知识点清单。
2. 工具基于一个固定 collection 快照，生成有限且可记录的检索面。
3. 返回块必须来自 dense／BM25 两路候选的并集，保留原始 `block_id`、`doc_id` 和 `section_path`。
4. 跨路合并按 `block_id` 去重，并保留 subtopic／facet 来源；最终裁剪只能选择现有候选。
5. 规划或任一路查询失败时必须 fail-closed；候选截断只进入 trace，不能返回未声明的部分结果。
6. 工具不返回 `complete／partial` 覆盖状态，也不承诺证明知识库中不存在其它相关内容。

### V2 子主题与证据预算扩展

在 v1 双通道工具之后继续验证“找全”职责。用户表达可能把多个信息需求
混在一个 topic 中；工具需要先还原用户明确表达的子主题，再在子主题内
发现检索面并生成查询。

本节新增约束：

1. planner 先输出 `topic_scope` 和 `subtopics[]`；每个 subtopic 的
   `source_fragment` 必须来自原始 topic。
2. 每个 subtopic 不得因为可能共享证据而与另一个用户子主题合并；每个
   subtopic 内再生成独立 facets 和双通道查询。
3. 召回候选保留 subtopic／facet／query 来源；最终裁剪不能只按全局
   RRF 或命中次数排序。
4. 候选先按 facet 保留代表证据，再在 token 预算内补齐；默认目标 40
   块、硬上限 48 块、模型可见证据 token 上限 20,000。
5. v2 取消独立的 LLM reranker；裁剪由确定性的 facet-aware 算法完成，
   降低成本和重排不稳定性。
6. 工具仍不返回覆盖完成状态；subtopic／facet 只用于内部选择和 trace。
7. 同一 subtopic 存在多种合理解释时，planner 必须对每种解释分别列举
   facets，不能只保留最主要的一种解释。

### 明确禁止

- 不在查询规划提示词中写入隔离、worktree、子 Agent 等用例词。
- 不让工具生成最终答案或代替 Judge。
- 不把向量相似度低直接解释为知识不存在。
- 不静默跳过失败的查询面。
- 不把内部模型调用排除在 trace 和运行成本之外。

### 失败语义

| 场景 | Agent 得到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 查询规划 JSON 非法 | `coverage_plan_invalid`，不执行后续检索 | planner prompt/output、错误 | 是 |
| 某个 dense／keyword 查询失败 | `coverage_query_failed`，不返回部分结果 | 查询计划、逐路结果与失败原因 | 是 |
| 合并结果超过证据预算或块上限 | 正常返回有界证据块，不声明覆盖完成 | `truncated=true`、facet 覆盖与 token 统计 | 是 |
| 所有查询无匹配 | 正常空结果与 `empty_reason` | 查询计划和空结果 | 是 |

### 决策预算

- 已知 B 级决策：8。
- 是否存在 C 级决策：否。

## 输入与输出

### 输入

- `topic: string`：需要高召回检索的主题。
- planner 先还原用户表达中的 subtopics，再在每个 subtopic 内发现 facets
  并生成双通道查询。
- 当前 planner 为 `topic-coverage-planner-v15`，prompt hash 为
  `a22791f7d60a28b70b01f41c5c1275748ab175e4717f3d740ffb32577eb9bdf9`；
  当前策略为 `topic-coverage-strategy-v6`。
- semantic／keyword 每路各保留 64 条隐藏安全上限，不把数量目标写进 planner。

### 输出

- `blocks[]`：各查询按通道取 Top-20，按 `block_id` 合并并保留
  subtopic／facet 来源；先为每个 facet 保留代表，再在约 20,000 evidence
  tokens 内补齐，目标 40 块、硬上限 48 块；
- `empty_reason`：与 `search_blocks` 一致的空结果原因；
- 子主题、检索面、查询、文档分布、裁剪理由和内部用量只写入
  trace／diagnostics。

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 |
| --- | --- | --- | --- | --- | --- |
| A1 | 工具职责独立 | 只接受主题并返回证据，不生成答案 | schema/revision 审查 | 必须 | 单元 |
| A2 | 查询可追溯 | planner 先生成 subtopics／facets，再生成 semantic／keyword 查询；查询、归属与各自 Top-20 进入 trace | 计划与 trace | 必须 | 单元 |
| A3 | 合并无重复 | 同一 block 跨查询、跨通道命中只保留一个候选 | 构造重叠结果 | 必须 | 单元 |
| A4 | 证据可信 | 所有返回块均来自两路候选，并保留 subtopic／facet 来源 | block/doc/section 对账 | 必须 | 单元 |
| A5 | 失败不静默 | planner 或任一路查询失败返回公共错误；裁剪只记录 trace | 故障注入 | 必须 | 单元 |
| A6 | 成本可审计 | planner chat、dense embedding 与 evidence token 估算进入 trace/运行统计 | usage 对账 | 必须 | 集成 |
| A7 | 实际增益 | c13-003 出现跨文档候选和 R1 覆盖提升 | N=10、固定题面 | 观察 | 产品回放 |

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 工具还是自主子 Agent | L1 固定流程工具 | 先验证多查询覆盖本身 | 子 Agent 需要独立状态、预算和停止规则 | 查询规划只调用一次 | 单元测试 | 已确认 |
| D2 | B | 输入完整问题还是主题 | 主题 | 检索能力与用户意图解耦 | 完整问题会重复 Agent 职责 | API 与复用边界 | schema 测试 | 已确认 |
| D3 | B | 是否接受 aspects | 不接受 | 避免第一版过拟合和过度设计 | 可选方面能增加提示但也会扩大接口 | 查询规划只从主题生成 | c13 与通用探针 | 已确认 |
| D4 | B | 查询数与候选深度 | 先拆 subtopics／facets，再分别生成可变查询；每查询 depth 60／top 20，每路最多 64 条唯一查询 | Agent 没有足够信息调参；固定数量会诱导凑数 | 暴露目标数量或候选参数会把内部实现决策推回 Agent | 工具成本与召回率 | 探针与 trace | 待确认 |
| D5 | B | 内部模型调用记账 | 计入 `model_calls` 和 token usage | 不能隐藏真实成本 | 独立记账会低估运行成本 | 运行统计 | usage 对账 | 待确认 |
| D6 | B | 是否对外返回覆盖状态 | 不返回覆盖状态，只返回 block 或公共错误 | 向量检索无法证明语义全集 | 返回状态会诱发主 Agent 二次决策 | API 语义 | 单元 + 产品 | 已确认 |
| D7 | B | 跨路选择使用 RRF、LLM 重排还是 facet-aware 裁剪 | 按 `block_id` 合并后先保每个 facet 代表，再按 token 预算补齐；facet 内排名优先，文档／章节新颖度只做 tie-break，同章节仅在正文近重复时排除 | 全局排序会挤压低频但独有的证据；来源多样性不能压过显著相关性；确定性裁剪可审计且少一次模型调用 | LLM 重排可做跨块语义比较，但成本和不稳定性更高 | 成本、稳定性和覆盖 | 单元 + trace + c13 | 待确认 |
| D8 | B | 模型可见证据预算 | 目标 40 块、硬上限 48 块、evidence token 上限 20,000 | 当前 100k context 下需为 History／协议／回答保留空间 | 24 块不足以覆盖多 facet；64 块约 27k tokens，噪声和成本更高 | 回答覆盖与上下文成本 | token 单测 + c13 | 待确认 |

## 实施顺序

1. 查询规划 schema 与确定性测试；
2. 多查询执行、block 合并和文档聚合；
3. Tool Adapter 接入与 usage 记账；
4. 工具级只读探针；
5. c13-003 单例 N=10 回放；
6. 根据结果决定是否进入动态子 Agent 版本。

## 当前证据状态

- 工具：`search_topic`
- tool contract：`retrieval-tools-v7`
- planner：`topic-coverage-planner-v15`；
  strategy：`topic-coverage-strategy-v6`；
  Agent prompt：`chat-v20`。
- 历史对照：planner v10 在三类主题上自然生成 10／8／8 条补充查询，说明查询数已经
  随主题复杂度变化，而不是撞固定上限。
- 历史对照：strategy v4 将每路 `candidate_depth` 从 20 提升到 60，同时保持每路
  `top_k=20` 和最终 24 块不变。
- c13-003 第二阶段 run：`run_8f15417ac77a4e18a5a3e80d248c8643`；
  judgment：`evalrev_b869a33e48a14ba9b25ba706c20395ad`。
- 结果为 1 pass、9 fail、0 pending；R2 10/10、X2 9/10，
  1 次 X2 违规；50 次模型调用、354,394 输入 tokens、43,065 输出 tokens。
- 说明可变查询数带来了首个 R1 通过，但候选证据进入最终 24 块仍不稳定。
- 双通道 run：`run_3293f66835e145409337e4f21935b0b5`；
  judgment：`evalrev_f7392244cfa14a41becd3279056be144`。
- 双通道结果：0 pass、10 fail、0 X 违规；60 次模型调用，
  输入 437,738 tokens、输出 48,648 tokens。
- 工具级探针中两路各取 20 个候选，合并 36 个（双路重合 4、
  dense-only 16、keyword-only 16），语义重排返回 24 个。
- 240 个最终块中，dense-only 137、keyword-only 44、双路重合 59；
  citation 直接引用分别为 77／18／25，keyword-only 贡献了 18 个被引用块。
- 按 R1 判官明确认可结果统计，核心边界覆盖从 v10 的 40/70（57.14%）
  提升到 v11 的 50/70（71.43%）；至少覆盖五组的样本从 4/10 提升到 9/10。
- 但七组全过的样本从 1/10 降为 0/10；第⑤组从 3/10 降到 0/10，
  第⑥组从 4/10 降到 1/10，严格阻断条件没有改善。
- 该版本改善了来源平衡和引用覆盖，但没有解决 query planning 对
  相邻语义的遗漏；c13 最终证据仍未稳定包含工作状态边界。

### V2 子主题／facet 产品回放

固定现场：

- case revision：`cr_c13_003_fff9fbc82a83`
- run：`run_79a1e05c90ca46bfa590ceb9517f3fb6`
- 首次判定：`evalrev_34feece7dba64c3dbb6dfee058646021`
- 完整重判：`evalrev_db348b30fc8543cfa32b6f5ca6e4f656`
- tool contract：`retrieval-tools-v7`
- planner／strategy：`topic-coverage-planner-v14`／
  `topic-coverage-strategy-v6`

Agent 与工具：

- 10/10 succeeded，0 infrastructure failure。
- 每次只有一个 `search_topic` 调用；10/10 planner 输出均通过嵌套 schema。
- 每次生成 2～3 个 subtopics、6～7 个 facets、15～29 条 semantic 查询、
  12～28 条 keyword 查询。
- 去重候选 64～132 个；每次稳定返回 40 个不同 block。
- 估算 evidence tokens 为 16,623～18,670，均低于 20,000 预算。
- 运行总计 60 次模型调用、499,015 输入 tokens、65,360 输出 tokens。

判定：

- 首次判定：0 pass、9 fail、1 pending；R1 1 pass、9 fail，
  R2 9 pass、1 pending，X2 9 pass、1 fail。
- 完整重判仍因 1 条 `judge_output_invalid` 为 incomplete：
  R1 0 pass、9 fail、1 pending；R2 9 pass、1 fail，X2 9 pass、1 fail。
- 同一个 R1 样本在两次判定间从 pass 变为 pending，当前不能宣称严格 R1
  覆盖率有稳定提升。
- 已知 worktree block 在 0/10 次最终 40 块中出现；
  checkpoint resume block 在 1/10 中出现；checkpoint 评估 block 在 0/10
  中出现。其他同义状态证据可能被答案使用，因此该目标块统计只用于定位，
  不替代 Judge 的七组判定。
- 冻结 v14 查询上的选择器对照显示，改为“facet 排名优先＋正文近重复判断”后，
  checkpoint resume 从 1/10 提升到 2/10。新增保留的是 attempt 8：该块
  在相关查询中排名第 1／2，旧算法因同文档同章节先占而误排，新算法成功
  保留。其余候选仍因最佳排名仅 12～19，未进入每个 facet 的前两个代表。

结论：

1. 嵌套 subtopic／facet schema、查询去重、40 块与 20k token 预算在产品
   调用链中均按合同工作。
2. 移除 LLM reranker 后没有引入协议失败；内部 planner 调用降为 1 次。
3. 子主题拆分没有稳定解决第⑤组和第⑥组，T7 的原题面仍主要激活安全
   控制语义。
4. 主 Agent 在 40 块证据下出现了一些新的状态/记忆表述，但 Judge 输出在
   同一冻结答案上不稳定，不能把个别差异作为版本增益。

### 已否决的 planner v6 产品回放

以下数据只说明“把 c13 答案维度写进 planner”时的上限，不代表当前
v11 产品基线：

- c13-003 run：`run_0a742b9cdb274e52a872133786f4ef36`
- c13-003 judgment：`evalrev_982b2ca0fefb4b07b07f1ccd15cf4fea`
- Agent：10/10 succeeded，0 infrastructure failure；52 次模型调用，
  输入 379,809 tokens、输出 40,394 tokens。
- 逐 trace 对账：8/10 的主题证据含工作状态／恢复内容，7/10 的最终答案
  实际使用该内容；相比 planner v3 的“证据 0/10、答案 0/10”有明确改善。
- 最终用例仍为 0 pass、9 fail、1 pending：R1 是七组全部覆盖的阻断判定，
  每次样本缺少的组不同；R2 9/10 pass、X2 9/10 pass、1 次 X2 违规。
- chat-v20 移除“一次全库检索不能代替逐源检查”的泛化提示后，单次运行的
  模型调用从 79–82 次降至 52 次，输入 token 从约 76 万降至约 38 万。

当前结论：

1. `planner-v5` 和 `planner-v6` 把用例预期答案维度写进检索提示词，
   已否决，不作为产品基线。
2. 当前双通道方案按 dense／BM25 分别生成查询、分别取 Top-20 后语义重排；
   来源分布明显更均衡，keyword-only 证据也能进入最终答案。
3. 双通道没有解决语义盲点：如果两路查询都没有覆盖某个相邻概念，reranker
   无法从候选之外创造证据。因此当前首要问题仍是 query planning。
4. 下一步应验证“基于首轮语料结果发现未覆盖语义”的有界二轮检索，而不是
   继续增加固定候选深度或向 planner 写领域词。
