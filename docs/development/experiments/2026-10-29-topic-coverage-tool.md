# 2026-10-29 · 主题覆盖检索工具实验

## 首轮目标（历史）

验证 L1 主题覆盖工具能否替代主 Agent 临时规划多轮查询：

- 输入一个主题；
- 内部生成最多 6 个互补检索面；
- 执行全库 `search_blocks`；
- 合并、去重并返回候选证据；
- 明确报告候选饱和、截断和失败状态。

## 首轮固定配置（历史）

| 项 | 值 |
| --- | --- |
| case | c13-003 |
| case revision | `cr_c13_003_fff9fbc82a83` |
| collection snapshot | `s_6b787258ba7d700c` |
| prompt bundle | `chat-v17` |
| tool contract | `retrieval-tools-v6` |
| planner | `topic-coverage-planner-v1` |
| N／K | 10／9 |
| planner search top-k | 6 |
| max queries | 6 |
| max documents | 12 |
| max blocks | 24 |

## 工具级探针

主题：

> AI Agent 安全中的隔离或控制边界

规划器生成 6 个查询，全部成功：

1. AI Agent 安全中的隔离或控制边界
2. AI Agent 沙箱隔离机制与容器逃逸风险
3. Agent 工具调用权限最小化与访问控制策略
4. 多智能体系统中信任边界与权限提升攻击
5. LLM Agent 提示注入导致的边界绕过与防御
6. Agent 执行环境网络隔离与数据外泄防护

结果：

- 候选文档：5
- 去重证据块：22
- 查询失败：0
- 最后一个查询新增文档：0
- 倒数第二个查询新增文档：2
- 状态：`partial`
- 原因：`query_plan_exhausted_before_candidate_saturation`
- planner：1 次 chat 调用，输入 223 tokens，输出 66 tokens
- embedding：159 input tokens

说明工具能发现跨文档候选，也能在查询计划耗尽但尚未饱和时拒绝报告
`complete`。

## Agent 产品回放

- run：`run_421bb5e563354b78a98dddbc5069fd25`
- judgment：`evalrev_ca46010bf89246b2bc8a643faf205ffd`
- Agent：10/10 succeeded，0 infrastructure failure。
- 10/10 样本都调用了 `search_topic_coverage`。
- 单次覆盖检索返回 3–7 篇候选文档，16–24 个去重证据块。
- 工具级覆盖状态：4 次 `complete`、6 次 `partial`。
- 最终用例：0 pass、9 fail、1 pending；R1 0/10、R2 10/10、X2 9/10。
- 1 个 X2 违规来自没有完成核验却作排他性覆盖断言。
- 1 个 R1 判官输出非法，判定 revision 状态为 `incomplete`。
- 总 usage：输入 678,723 tokens、输出 53,609 tokens、71 次模型调用。

## 结果判断

工具改变了失败形态：

- 原先 10/10 只读一篇主要文档；
- 现在 10/10 先执行主题覆盖检索，命中 3–7 篇候选；
- 但仍没有稳定覆盖工作状态、上下文/记忆和子 Agent 委派三组知识；
- planner 仍偏向沙箱、权限、审批、网络和审计等常见安全检查项；
- 严格判定结果仍为 0 pass，且有一个 R1 pending；工具带来的检索路径
  变化尚未转化为稳定答案通过。

这一结果支持继续在主题覆盖检索层迭代，而不是回头增加主 Agent 提示词。

## 后续候选方向

1. 改进 planner 的通用维度覆盖，不加入 c13 专用词，但要求显式覆盖
   “资源/权限之外”的状态、生命周期和协作对象；
2. 评估是否需要在计划耗尽时进行有界第二轮规划；
3. 保持候选饱和和 partial 状态，不允许为了通过用例而伪报 complete；
4. 暂不运行其它用例，待该工具在 c13-003 上收敛后再做全量回归。

## planner v2 回放

首轮回放显示 planner 的查询面偏向沙箱、权限、审批、网络和审计。
逐 trace 对账后：

- 10/10 的候选证据都没有 checkpoint／recoverable state 内容；
- 10/10 都已有上下文／记忆证据，但 2 个最终答案未使用；
- 因此首轮主因是 query planning，而不是最终生成。

planner v2 增加一条通用要求：涉及过程、系统或机制的主题，
查询面还应覆盖状态变化、生命周期、持久化和失败恢复。

- run：`run_69021e3de7d84c1bb886177930fdeb9f`
- judgment：`evalrev_bba3459da7124e3fa3b6bd0bd18edd3f`
- 10/10 调用了 `search_topic_coverage`
- 每次候选文档：4–6 篇
- 10/10 工具状态：`partial`
- 8/10 的候选证据已经包含 checkpoint／恢复内容
- 最终答案：0/10 使用该证据
- 最终用例：0 pass、10 fail；R1 0/10、R2 10/10、X2 7/10
- 总 usage：输入 772,464 tokens、输出 57,879 tokens、75 次模型调用

结论：

1. planner v2 改善了检索，状态/恢复知识已进入大多数候选证据集；
2. 当前主要瓶颈转为生成阶段没有利用低显著度的跨文档候选；
3. 仍有 2/10 没有召回该知识，检索覆盖率尚未完全解决；
4. 3 个 X2 违规说明最终回答的范围声明仍不稳定。

## 当前固定策略：双通道 planner v11 + reranker v1 + strategy v5

下表是后续实验必须同时核对的冻结配置。运行 trace 中的
`tool.call.app.diagnostics` 与 planner `llm.call` span 应记录相同的版本和 hash。
当前状态是“可复核的通用分析基线”，已完成 v10 c13-003 N=10 回放。

| 项 | 值 |
| --- | --- |
| planner version | `topic-coverage-planner-v11` |
| planner prompt hash | `64060538d5303427bf9964a555b90716fa48289a7a2dac5c8f671224a4f98814` |
| reranker version | `topic-coverage-reranker-v1` |
| reranker prompt hash | `c2caffff01c372d1fd6a7c4f743f3aa610e3fb01624458679045c4c2cf2207a8` |
| strategy version | `topic-coverage-strategy-v5` |
| query count | 每通道可变（semantic 1–10 条，keyword 1–10 条；数量由语义增量决定） |
| per-query candidate depth | 60 |
| per-query returned candidates | 20 |
| per-route candidates after fusion | 20 |
| search concurrency | 3 |
| final evidence blocks | 最多 24 |
| planner max output tokens | 800 |
| failure policy | planner 或任一路查询失败即 fail-closed |

### planner 输入与输出

system message 是下面的完整提示词；user message 只包含：

```json
{"topic":"<用户主题>"}
```

planner 返回一组数量可变的补充查询：

```json
{
  "semantic_queries":["自然语言语义查询 1","..."],
  "keyword_queries":["关键词组合 1","..."]
}
```

代码先把原始 topic 放入 dense 通道，再分别校验 semantic／keyword 两组
查询非空、互不重复，并受每通道 10 条隐藏安全上限约束。上限不写进 planner
提示词，避免再次形成“凑满上限”的数量目标。

### planner v11 system prompt（与源码逐字一致）

```text
<role>
你是文档检索查询规划器。你只生成两路检索查询，不回答问题。
</role>

<task>
根据用户给出的 topic，生成互补的自然语言语义查询和精确关键词查询。

semantic_queries 供向量检索使用：写成完整、自然的语义表达，可以包含
同义改写、上下位或相邻概念，也可以使用适合知识库语言的表达。不要只堆
关键词，不要重复原始 topic。

keyword_queries 供 BM25/FTS 使用：写成长度短、信息密度高的词项组合，
优先保留原文中可能直接出现的术语、别名、缩写和实现词。不要写长句，
不要使用没有检索价值的连接词。

两组查询都必须能从 topic 自然推导，不得预设固定答案维度，也不得把
可能的问题答案写成检索清单。知识库可能同时包含中文和英文文档，不能
假设 topic 的语言与文档语言相同。查询数量由不同语义关系数量决定，
达到语义饱和后停止，不得为了凑数添加低增量查询。
</task>

<rules>
1. semantic_queries 和 keyword_queries 都至少包含一条查询。
2. 每组内部查询必须有独立信息增量，不能只做措辞近似。
3. 当 topic 的概念可以用中英文表达时，两路查询都应覆盖两种语言。
4. 不得重复 topic 或彼此重复。
5. 只输出合法 JSON 对象，不输出 Markdown 或额外文字。
6. JSON 结构必须为
   {"semantic_queries":["..."],"keyword_queries":["..."]}。
</rules>
```

源码位置：[topic_coverage.py](../../../backend/app/agent/topic_coverage.py)。
prompt hash 由上述 UTF-8 原文直接计算；测试固定该 hash，因此提示词发生
任何字节变化而没有同步版本和记录时，单元测试会失败。

### reranker v1 system prompt（与源码逐字一致）

```text
<role>
你是文档证据重排器。候选块已经由检索器选出；你只排序和筛选候选块，
不回答问题，不补充候选之外的证据。
</role>

<task>
根据 topic 判断候选块对回答该主题的价值，并按从高到低返回最多
max_return_blocks 个 candidate_id。

排序目标：
1. 优先直接支持 topic 核心语义的块；
2. 在相关块之间保留互补信息，降低重复章节和近似重复内容的权重；
3. 不要仅因为同时包含主题词就选择泛化块；
4. 不得因为某个来源出现得多就忽略其它可能提供独特信息的来源。
</task>

<rules>
1. candidate_id 必须逐字来自输入，不得生成新 ID。
2. 不得重复 candidate_id。
3. 候选数量足够时，返回恰好 max_return_blocks 个；
   候选不足时返回全部。
4. 只输出合法 JSON 对象，不输出 Markdown 或额外文字。
5. JSON 结构必须为 {"block_ids":["candidate_id", "..."]}。
</rules>
```

### 版本差异

| 版本 | 关键提示词变化 | 触发证据 |
| --- | --- | --- |
| v1 | 从 topic 生成互补查询，主要覆盖显性机制和安全控制 | 首轮大量重复查询沙箱、权限、审批、网络 |
| v2 | 过程／系统／机制类主题增加状态变化、生命周期、持久化、失败恢复 | v1 候选证据 0/10 包含 checkpoint／恢复 |
| v3 | 增加并发执行、共享资源、版本化状态和回滚语境 | v2 仍主要检索显性安全边界 |
| v4 | 移除“查询必须跟随 topic 语言”，要求至少 2 条中文＋2 条英文；保留原文可能直接出现的术语 | 知识库以英文文档为主，中文查询的 FTS 无法命中 `worktree`、`checkpoint` 等实现词 |
| v5（已否决） | 中英文各至少一条查询强制覆盖生命周期／持久化／失败恢复／独立实例 | 把 c13 的预期答案维度写进 planner，违反“只根据 topic 扩展”的职责边界 |
| v6（已否决） | 进一步固定并行会话、独立副本／检出、工作区分离与恢复 | 召回提升明显，但属于针对用例答案调提示词；不能作为产品基线 |
| v7 | 删除全部预设答案维度，只保留同义表达、上下位／相邻概念和不同措辞；保留中英双语 | 恢复 planner 的领域无关性；但模型偶尔把原始 topic 计入补充查询，返回 6 条后 fail-closed |
| v8 | 明确“只输出补充查询、不得重复原始 topic、数量恰好等于输入” | v7 的查询数量协议不稳定；此版本仍固定 5 条 |
| v9 | 取消固定数量，改为按语义复杂度生成，但提示词暴露了 7 条上限 | 三个不同主题都恰好返回上限 7，说明上限仍在诱导凑数 |
| v10 | 提示词完全不提数量，只要求语义增量和饱和停止；Harness 保留 12 条隐藏安全上限 | v9 的数量仍被上限锚定；v10 三个主题自然返回 10／8／8 条 |
| v11 | 拆成 semantic_queries 与 keyword_queries 两条独立查询通道；每通道最多 10 条；新增 reranker-v1 对合并候选做语义排序 | 同一查询同时服务 dense／BM25 会让长句和词串分别不适配，且 RRF 不能理解最终证据的互补性 |

更早版本的完整 prompt 不继续复制到当前文档中；对应 run 的 planner
`llm.call.attributes["llm.prompt"]` artifact 保存了当时逐字输入。
复核旧 run 时应读取该 artifact，而不是用本表反向重建旧 prompt。

### strategy v5：双通道候选、合并与语义重排

1. semantic 通道包含原始 topic 和 planner 生成的自然语言查询，只执行
   dense 检索；keyword 通道只执行 BM25/FTS，不产生 query embedding。
2. 每个子查询使用 `candidate_depth=60`、`top_k=20`。同一通道内按
   block 的 RRF 支持度、命中查询数和最佳排名聚合，最终保留该通道
   Top-20。
3. dense Top-20 与 keyword Top-20 按 `block_id` 合并，最多 40 个候选；
   每个候选记录 `routes` 和两条通道内的排名。
4. 任一路查询失败则整次调用返回 `coverage_query_failed`，不返回部分证据。
5. merge 后调用一次 `topic-coverage-reranker-v1`。输入是原始 topic 和
   最多 40 个候选的标题、章节、最多 800 字符正文与通道排名；模型只能
   返回输入 candidate_id。
6. Harness 校验 candidate_id 数量、重复和未知 ID；成功结果按重排顺序
   返回最多 24 个完整 block，失败则显式返回 `coverage_rerank_*`。
7. 查询、两路候选分布、逐查询使用、重排选择和最终 block 全部进入 trace。

strategy v1 的选择缺陷是“查询代表”错误地先比较跨查询命中数。一个被多条
查询共同命中的泛化块会挤掉某条查询自己排名第一、但只被该查询命中的
独特证据。strategy v2 改为先比较该查询内的 `rank_by_query`，并增加了
`test_topic_coverage_keeps_unique_top_hit_from_each_query` 反向测试。
strategy v3 把每路候选深度从 20 提高到 60，同时继续保持对外每路 20 个块。
strategy v4 改为每通道可变查询数。strategy v5 则把混合查询拆成
dense／keyword 两个独立通道，并用语义重排替代跨路 RRF 作为最终选择依据。

可复核的 trace 字段：

- planner `llm.call`：`app.planner_version`、`app.planner_prompt_hash`、
  `app.strategy_version`、完整 `llm.prompt`／`llm.output` artifact；
- reranker `llm.call`：`app.reranker_version`、`app.reranker_prompt_hash`、
  完整重排 prompt／output artifact；
- `retrieval.coverage_query`：`app.query_index`、`app.query`、
  `app.search_mode`、
  `app.result_count`、`app.query_input_tokens`；
- `tool.call.app.diagnostics`：semantic／keyword 查询、两路逐查询结果、
  两路候选数和重合数、语义重排选择、planner／reranker prompt hash 和
  strategy version。

### v8 分层探针：遗漏到底发生在哪一层

使用同一 topic，分别以每路 `20/20`、`60/20`、`100/20` 和
`100/100`（`candidate_depth/top_k`）执行，并比较最终
24／32／48 块。
目标块为：

- `worktree`：Best practices 中的独立 checkout／并行会话块；
- `checkpoint_resume`：多 Agent 系统从 checkpoint 恢复长期状态的块；
- `checkpoint_evaluation`：同一文档中按 checkpoint 验证状态变化的块。

代表性 v8 查询计划为：

1. `AI Agent 安全中的隔离或控制边界`
2. `AI agent sandboxing privilege separation execution environment`
3. `LLM agent 权限边界 最小权限 访问控制`
4. `prompt injection 防御 工具调用 权限隔离`
5. `multi-agent system trust boundary isolation security`
6. `AI agent 越权操作 人工审批 中断机制`

结果：

| 配置 | worktree | checkpoint_resume | checkpoint_evaluation |
| --- | --- | --- | --- |
| depth 20／top-k 20 | dense、BM25、fused 均未进入 | 未进入 | BM25 第 5 路第 11，但 fused 第 5 路第 21，刚好被 top-k 20 截掉 |
| depth 60／top-k 20 | dense 第 2 路 54，fused 第 2 路 90，仍高于 top-k 20 | dense 第 6 路 33；BM25 第 1 路 30、第 5 路 22、第 6 路 30；fused 第 6 路 9，实际返回 | dense 第 5 路 48；BM25 第 5 路 12；fused 第 5 路 18，实际返回 |
| depth 100／top-k 20 | dense 第 2 路 54、第 5 路 60；fused 第 2 路 95、第 5 路 92，均高于 top-k 20 | dense 第 6 路 22；BM25 第 1 路 30、第 5 路 22、第 6 路 30；fused 第 1 路 17、第 6 路 6，实际返回 | dense 第 5 路 44；BM25 第 5 路 11；fused 第 5 路 18，实际返回 |
| depth 100／top-k 100 | 第 2／5 路分别返回第 95／92，进入合并候选 | 进入合并候选 | 进入合并候选 |

把已进入合并候选的目标交给最终选择器：

| 最终预算 | worktree | checkpoint_resume | checkpoint_evaluation |
| --- | --- | --- | --- |
| 24 块 | 未选中 | `depth100/top20` 未选中；`depth100/top100` 选中 | 未选中 |
| 32 块 | 未选中 | 选中 | 未选中 |
| 48 块 | 未选中 | 选中 | 未选中 |

第一阶段只采纳 `candidate_depth=60 / top_k=20`：在这个配置中，
`checkpoint_resume` 进入最终 24 块，`checkpoint_evaluation` 进入
单路证据池但被最终选择器排除。前者证明加深融合候选有效，后者留给
后续单独实验，避免同时修改查询数、返回数和选择算法。

该探针还观察到 planner 计划间波动：另一个 v8 计划中，worktree
只在第 2／4 路 dense 第 95／80 名出现；`checkpoint_evaluation`
在 Top-100 内也完全不存在。因此不能把一次计划的排名当作固定常量。

分层结论：

1. **中英文是必要条件，但不是充分原因。** 英文查询确实覆盖了英文文档，
   说明不能退回只用 topic 语言；但这些英文查询仍围绕 sandbox、权限、
   injection 等显性语义，没有自然生成工作副本、checkpoint 等相邻语义。
   另一个 v8 计划中的纯中文查询 BM25 为 0 命中，只能依靠向量召回，
   因此中英双语仍应保留。
2. **每路 candidate_depth=20 确实过窄。** `checkpoint_resume` 的 dense
   和 BM25 排名都在 20 名外，但双路共同命中后，在 depth 100 下 RRF 可升到
   第 6 名；只扩大返回 `top_k` 没有用，必须先扩大每路候选深度。
3. **top-k=20 也是独立瓶颈。** `checkpoint_evaluation` 在
   depth100/top20 中 fused 第 18 名，能返回；但另一计划中 fused 第 21，
   就差一名被截掉。
4. **最终 24 块不是唯一原因，选择算法更关键。**
   `worktree` 在 depth100/top100 下已合并，但 24、32、48 块都未选中；
   `checkpoint_evaluation` 同样到 48 块仍未选中。当前“每查询代表＋每文档
   代表＋多查询支持度”会系统性地偏向泛化块。
5. **首要断点仍是 query planning。** 通用 planner 没有稳定生成覆盖相邻
   语义的查询时，扩大候选深度或最终块数都不能保证找回目标；
   `search_topic` 只能做有界高召回，不能承诺语义穷尽。

### 第二阶段：可变查询数回放

使用三个不同复杂度的主题做工具级探针，v10 在提示词中不给任何目标数量，
仅在 Harness 内保留 12 条隐藏安全上限：

| topic | 实际补充查询数 | 候选块 |
| --- | ---: | ---: |
| AI Agent 安全中的隔离或控制边界 | 10 | 110 |
| 什么是 MCP | 8 | 79 |
| 如何评估 RAG 检索质量 | 8 | 45 |

三组查询都同时包含中英文，且没有全部撞到隐藏上限，说明“数量由语义增量
决定”比固定数量或显式上限更符合预期。

固定 `candidate_depth=60 / top_k=20` 后运行 c13-003：

- run：`run_8f15417ac77a4e18a5a3e80d248c8643`
- judgment：`evalrev_b869a33e48a14ba9b25ba706c20395ad`
- Agent：10/10 succeeded，50 次模型调用，输入 354,394 tokens，
  输出 43,065 tokens。
- 每次生成 10–12 条补充查询，候选证据 89–104 块。
- 最终 1 pass、9 fail、0 pending；R2 10/10 pass，
  X2 9/10 pass、1 次违规。
- 相比固定查询版本首次出现 R1 通过样本，说明可变查询数产生了真实增量。

该阶段的剩余失败有两种：

1. 目标证据已经进入候选池，但没有进入最终 24 块；例如
   `checkpoint_evaluation` 在 query 7 的 fused rank 16，最终 24／32／48
   都未选中。
2. 通用 query planner 仍可能没有生成目标语义；例如 worktree 在当前计划中
   只到 query 7 的 dense rank 50、fused rank 70，高于单路 top-k，
   最终没有进入合并候选。

因此下一阶段才进入选择器实验；不在本阶段同时修改合并输入、返回预算和
主 Agent 提示词。

### 历史对照：v10 最终 24 块的来源与引用分布

对 c13-003 N=10 的 240 个最终 block 做离线回溯：

- 用 trace 中每路实际查询重放相同 `candidate_depth=60 / top_k=20`；
- 57 个去重查询全部可复现，最终 240 个 block 均能在 dense／BM25
  候选中重新找到；
- “有用”定义为该 block 至少被一次最终 citation annotation 直接引用，
  不是“对判官通过有因果贡献”的替代指标。

按 60 个候选范围内是否存在 dense／BM25 命中：

| 来源 | 最终块 | 占 240 块 | 被引用 | 来源内引用率 |
| --- | ---: | ---: | ---: | ---: |
| dense＋BM25 都有 | 231 | 96.25% | 104 | 45.02% |
| 仅 BM25 | 9 | 3.75% | 0 | 0% |
| 仅 dense | 0 | 0% | 0 | 0% |
| 无法复现 | 0 | 0% | 0 | 0% |

这说明最终被选中的块没有出现“只有 dense 能找到”的情况；9 个
纯 keyword-only 块也没有被最终答案引用。但这不是关键词路径无效，因为
“都在 60 候选内”不代表关键词没有影响 RRF。

按单路 Top-20 排名再拆分：

| 单路 Top-20 来源 | 最终块 | 占 240 块 | 被引用 | 来源内引用率 | 占全部被引用块 |
| --- | ---: | ---: | ---: | ---: | ---: |
| dense＋BM25 均在 Top-20 | 165 | 68.75% | 91 | 55.15% | 87.50% |
| 仅 BM25 在 Top-20 | 53 | 22.08% | 12 | 22.64% | 11.54% |
| 仅 dense 在 Top-20 | 11 | 4.58% | 1 | 9.09% | 0.96% |
| 两路都在 Top-20 外、靠更深候选融合 | 11 | 4.58% | 0 | 0% | 0% |

关键解释：

1. 最终 240 块中 104 块被 citation annotation 直接引用，总体可观察
   使用率为 43.33%。
2. BM25-only 的深候选有 9 块，但没有一个进入答案；关键词当前的贡献主要
   不是“发现 dense 完全找不到的块”，而是把 dense 中排名较后的 53 块通过
   RRF 推入单路 Top-20，其中 12 块最终被引用。
3. dense-only Top-20 只有 11 块，其中仅 1 块被引用。
4. “两路 Top-20 外、仅靠融合进候选”的 11 块没有一个被引用，说明继续
   向下放候选容易增加噪声。
5. 该统计是组成与引用相关性，不是移除关键词后的反事实实验；移除 BM25
   会改变 RRF 顺序和最终选择，不能简单从 53 或 12 推算最终损失。

### 双通道 v11＋reranker v1 回放

工具级探针：

- dense 通道：原始 topic ＋ 8 条语义查询，聚合后 Top-20；
- keyword 通道：8 条关键词查询，聚合后 Top-20；
- 两路去重后 36 个候选：both 4、dense-only 16、keyword-only 16；
- reranker 从 36 个候选中选择 24 个，并由 Harness 校验全部 ID。

产品回放：

- run：`run_3293f66835e145409337e4f21935b0b5`
- judgment：`evalrev_f7392244cfa14a41becd3279056be144`
- Agent：10/10 succeeded，60 次模型调用，输入 437,738 tokens，
  输出 48,648 tokens。
- 最终 0 pass、10 fail、0 pending；R2 10/10、X2 10/10。
- 每次选择仍为 24 块，两路候选合并后通常为 32–37 块。

最终 240 个 block 的来源与 citation 使用率：

| 来源 | 最终块 | 占 240 块 | 被引用 | 来源内引用率 | 占全部被引用块 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 仅 dense | 137 | 57.08% | 77 | 56.20% | 64.17% |
| 仅 BM25 | 44 | 18.33% | 18 | 40.91% | 15.00% |
| 双路重合 | 59 | 24.58% | 25 | 42.37% | 20.83% |

总计 120/240 块被 citation annotation 直接引用，使用率 50.00%。

与 v10 单通道混合检索相比：

1. 两路来源更均衡，keyword-only 从几乎不影响结果变为贡献 18 个被引用块；
2. 不再由 RRF 强制奖励双路共同命中的泛化块，双路重合块占比从 96.25%
   降到 24.58%；
3. 语义重排提升了最终答案的 citation 覆盖，但没有解决 query planning
   没有生成的语义盲点；
4. 本轮回放仍为 0 pass，且因为额外 reranker 调用，模型调用数从 50 增至 60。

因此双通道结构本身有可观察价值，但当前尚不能证明它改善了 c13 的最终
任务结果。

### 核心边界覆盖率：v10 与 v11 对比

按 R1 判官明确认可的七组核心边界统计，分母固定为 `7 × 10 = 70`。
“部分覆盖”不计为通过，只有判官明确说明该组限制对象或限制关系时才算覆盖。

| 核心边界 | v10 单通道 | v11 双通道＋reranker | 变化 |
| --- | ---: | ---: | ---: |
| ①文件与工作区可达 | 9/10 | 10/10 | +1 |
| ②凭证与身份 | 5/10 | 10/10 | +5 |
| ③工具与行动能力 | 10/10 | 10/10 | 0 |
| ④外部内容与工具结果信任 | 4/10 | 10/10 | +6 |
| ⑤并行任务与工作状态 | 3/10 | 0/10 | -3 |
| ⑥上下文与记忆生命周期 | 4/10 | 1/10 | -3 |
| ⑦子 Agent 委派 | 5/10 | 9/10 | +4 |
| **总计** | **40/70（57.14%）** | **50/70（71.43%）** | **+10（+14.29pp）** |

分布形态：

- v10 每次覆盖 `3,2,6,5,4,4,5,7,2,2` 组，4/10 次达到至少 5 组，
  1/10 次达到全部 7 组；
- v11 每次覆盖 `6,5,5,5,5,4,5,5,5,5` 组，9/10 次达到至少 5 组，
  0/10 次达到全部 7 组；
- R1 的严格全通过数仍从 1/10 降为 0/10。

因此结论是：

1. 双通道显著提高了“多数核心边界能够被稳定覆盖”的比例，尤其改善了
   凭证身份、外部内容信任和子 Agent 委派。
2. 它没有解决最难的并行工作状态和上下文/记忆生命周期，反而因为
   planner 和 reranker 都集中在显性安全机制上，这两组出现退步。
3. 严格指标没有提升，是因为 R1 要求七组同时覆盖；平均覆盖提升不能
   替代阻断条件。
4. 来源平衡改善与核心边界覆盖改善不是同一个指标。双通道改善了前者，
   但 query planning 的语义盲点仍限制后者。

### 历史预探针：为什么没有采用“查询独有证据”选择器

在冻结候选上离线比较了当前选择器与一个泛化候选方案：

- 当前方案：每查询一个代表、每文档一个代表、剩余按多查询支持度填充；
- 候选方案：在查询代表之后，优先选择“只被该查询命中”的最高排名证据，
  再进行全局填充。

结果：

- 在 `depth60/top20` 下，两个选择器都没有把
  `checkpoint_evaluation` 选进 24 块；
- 在 `depth100/top100` 下，候选方案能把 `checkpoint_evaluation` 选进
  24 块，但会把原本可被当前方案选中的 `checkpoint_resume` 挤出 24 块，
  直到 48 块才恢复。

这说明“查询独有”不等于“语义重要”，该选择器只是把偏好从泛化证据
换成了深尾噪声，不具备稳定增益。因此没有进入产品代码。

下一候选设计必须同时满足：

1. 合并 `fused_candidates` 而不是先截成每路 top20 块；
2. 选择阶段只持有候选身份与排名，最终一次性回填 24 个完整 block；
3. 对文档、章节和语义近重复同时降权，不能只按命中次数或独有性排序。

这已经改变工具内部的数据流，需作为独立实验，不与本轮的深度和查询数
变化混在一起。

## 精简返回结构与 planner v4–v6

宽返回结构（文档聚合、覆盖状态和查询明细回给主 Agent）会让主 Agent
继续做检索决策，与工具职责冲突。后续版本改为：

- 对外只返回与 `search_blocks` 相同的 `blocks[] + empty_reason`；
- 查询、候选、截断和模型用量只进入 diagnostics；
- 文档聚合只用于内部证据选择；
- 每个内部查询优先选取该查询自身排名最高的代表块；
- 中英文各生成至少一条“执行单元与工作状态隔离”查询，覆盖并行会话、
  独立副本／检出、工作区分离、持久化、失败恢复或回滚中的多个方面。

工具级验证使用主题：

> AI Agent 安全中的隔离或控制边界

planner v6 生成 6 个查询：

1. `AI Agent 安全中的隔离或控制边界`
2. `AI agent sandbox isolation workspace separation concurrent sessions state persistence rollback`
3. `AI agent 隔离 沙箱 工作区 目录分离 并发会话 状态持久化 回滚`
4. `LLM agent privilege boundary tool permission control least privilege escape prevention`
5. `AI agent 执行单元 独立副本 检出 版本状态 失败恢复 隔离`
6. `multi-agent shared resource concurrency isolation failure recovery checkpoint restore`

结果为 65 个候选块、13 篇候选文档，最终返回 24 个完整块；同时包含：

- `Best practices for Claude Code` 的
  `worktree／isolated git checkouts／separate sessions`；
- `How we built our multi-agent research system` 的
  `checkpoint／resume recovery`。

### planner v3（chat-v17）

- run：`run_dad10574e55942b0943e90c146e41df4`
- judgment：`evalrev_648febcad2ac4ded828798db5fb7c0b1`
- Agent 10/10 succeeded，83 次模型调用，输入 790,208 tokens，
  输出 48,851 tokens。
- 最终 0 pass、9 fail、1 pending。
- 逐 trace 对账：工作状态／恢复证据 0/10，最终答案使用 0/10。

### planner v4（chat-v17）

增加中英双语查询和“每路查询自身排名代表”选择。

- run：`run_a6143e0911f843d8932340ce1e5a6464`
- judgment：`evalrev_afe0712e5cb14c9b8b09bdac7ba74621`
- Agent 10/10 succeeded，79 次模型调用，输入 729,653 tokens，
  输出 52,918 tokens。
- 最终 0 pass、10 fail。
- 工作状态／恢复证据进入 4/10，最终答案使用 0/10。

### planner v5 + chat-v20

chat-v20 明确 `search_topic` 返回的是主 Agent 的完整证据池，单篇补读
只能补充，不能替代跨文档证据；planner v5 把状态／生命周期检索面从
建议改为强制。

- run：`run_5c8995ffa15a4ba1bc894b31ba27d765`
- judgment：`evalrev_4224653d46f94f08802fa3aab3c9f59d`
- Agent 10/10 succeeded，54 次模型调用，输入 375,258 tokens，
  输出 42,013 tokens。
- 最终 0 pass、9 fail、1 pending。
- 工作状态／恢复证据进入 5/10，最终答案使用 3/10。
- 相比 chat-v17，模型调用和 token 大幅下降，说明主 Agent 不再稳定执行
  “先主题检索、再复读一篇主文档”的退化路径。

### planner v6 + chat-v20

补足“执行单元与工作状态的隔离”这一空间维度。

- run：`run_0a742b9cdb274e52a872133786f4ef36`
- judgment：`evalrev_982b2ca0fefb4b07b07f1ccd15cf4fea`
- Agent 10/10 succeeded，52 次模型调用，输入 379,809 tokens，
  输出 40,394 tokens。
- 工作状态／恢复证据进入 8/10，最终答案使用 7/10。
- 最终仍为 0 pass、9 fail、1 pending；R2 9/10 pass，
  X2 9/10 pass、1 次违规。

最终归因：

1. 跨语言查询和每路查询代表解决了主要检索遗漏，工具已能稳定发现
   worktree、独立会话、checkpoint 和恢复状态。
2. chat-v20 解决了主 Agent 不信任跨文档证据、反复退化为单篇读取的问题，
   同时把单次运行成本降低约一半。
3. 当前 R1 仍失败的原因是“七组必须全部覆盖”的阻断判定；10 次样本
   缺少的组不同，继续针对 c13-003 增加固定词或硬门禁会过拟合。
4. 后续应重新评估该用例的产品定位，或在更上层引入面向覆盖矩阵的任务
   分解；本轮不继续通过提示词打地鼠。

## subtopic／facet v14 产品回放

实验日期：2026-10-30。

| 项目 | 值 |
| --- | --- |
| case revision | `cr_c13_003_fff9fbc82a83` |
| collection snapshot | `s_6b787258ba7d700c` |
| tool contract | `retrieval-tools-v7` |
| planner | `topic-coverage-planner-v14` |
| strategy | `topic-coverage-strategy-v6` |
| run | `run_79a1e05c90ca46bfa590ceb9517f3fb6` |
| 首次 judgment | `evalrev_34feece7dba64c3dbb6dfee058646021` |
| 完整重判 | `evalrev_db348b30fc8543cfa32b6f5ca6e4f656` |

固定执行语义：

1. planner 一次调用先生成用户 subtopics，再生成每个 subtopic 下的 facets
   与 semantic／keyword 查询；
2. 所有查询去重执行，结果按 `block_id` 合并并保留 subtopic／facet 来源；
3. 每个 facet 先保留代表证据，再在 20,000 evidence tokens 内补齐；
4. 目标 40 块、硬上限 48 块，不调用 LLM reranker。

### Agent 与返回结构

- 10/10 succeeded，0 infrastructure failure。
- 每次恰好一个 `search_topic` 调用，10/10 planner 输出合法。
- 每次 2～3 个 subtopics、6～7 个 facets。
- 每次 15～29 条 semantic 查询、12～28 条 keyword 查询。
- 去重候选 64～132 个；每次返回 40 个不同 block。
- 每次 evidence tokens 估算 16,623～18,670，均低于 20,000。
- 总计 60 次模型调用、499,015 输入 tokens、65,360 输出 tokens。

### Judgment

首次判定：

- 用例：0 pass、9 fail、1 pending。
- R1：1 pass、9 fail。
- R2：9 pass、1 pending。
- X2：9 pass、1 fail。

完整重判仍有一条 `judge_output_invalid`：

- 用例：0 pass、9 fail、1 pending。
- R1：0 pass、9 fail、1 pending。
- R2：9 pass、1 fail。
- X2：9 pass、1 fail。

同一 R1 样本在首次判定中 pass，重判为 pending，不能把单次 pass 视为稳定
覆盖增益。

### 召回与已知目标块

- worktree 目标块出现在最终 40 块中：0/10。
- checkpoint resume 目标块出现在最终 40 块中：1/10。
- checkpoint 评估目标块出现在最终 40 块中：0/10。
- 这些固定目标块只用于定位；答案可能使用同一语义面的其他块，最终覆盖以
  R1 判定为准。

结论：

1. v14 的嵌套 schema、子主题保真、查询去重和 token-aware 裁剪在产品链路
   中按合同运行。
2. 取消 reranker 后少一次固定 LLM 调用；但证据扩充使主 Agent 的轮次和
   token 成本上升，总模型调用数仍为 60。
3. “隔离与控制边界”原题面下，planner 仍主要生成安全控制类 subtopics 与
   facets，不能稳定发现任务生命周期和上下文／记忆边界。
4. 当前 Judge 对长答案的同一 criterion 存在 pass／pending 级不稳定，
   在修复 Judge 输出可靠性前，不宜用单次七组 pass 宣称产品回归。

### checkpoint resume 裁剪对照

在完全相同的 v14 planner 查询、collection 快照、`depth=60/top20` 和
40 块预算上，只替换最终选择算法：

- 旧算法：facet 代表选择时先偏好新文档和新章节，再比较查询排名；
- 新算法：先比较 facet 内查询排名，文档／章节新颖度只做 tie-break；
- 同章节块不再自动降权，仅当正文 token Jaccard 达到近重复阈值时才排除。

checkpoint resume 固定目标块的结果：

| 层级 | 次数 |
| --- | ---: |
| 进入单查询 depth 60 | 10/10 |
| 进入单查询 Top-20 | 7/10 |
| 旧算法进入最终 40 | 1/10 |
| 新算法进入最终 40 | 2/10 |

新增保留的是 attempt 8。该目标块有两条 query 分别排名第 1、第 2，
但旧算法先选中了同文档同章节的另一个不同知识点块，随后把目标块压到
后面。新算法证明“相关性优先＋正文近重复判断”修复了这一类误裁剪。

剩余未选中的情况：

- attempt 1、3、4、5、7 中，目标块在候选池里的最佳排名只有 12～19，
  落后于对应 facet 已选的第 1／2 名候选；
- attempt 6、9、10 中，目标块没有进入任何单查询 Top-20。

因此本次修复解决了高排名块被来源多样性误伤的问题，但没有解决
Top-20 截断和低排名候选覆盖问题。checkpoint resume 仍不能称为稳定搞定。

### `AI Agent 隔离` + thinking 探针

使用产品 v14 的 subtopic／facet prompt，topic 改为 `AI Agent 隔离`，
开启 thinking 并走当前查询、召回和裁剪路径：

- planner 输出 1 个 subtopic、7 个 facets；
- semantic 查询 30 条；keyword 查询 33 条；
- 去重候选 278 块，覆盖 15 篇文档；
- 最终返回 40 块，估算 18,161 evidence tokens；
- planner 1 次调用，input／output tokens 为 1,001／2,529。

生成的相关 facets：

- 多 Agent 与会话边界隔离；
- 工具调用与数据记忆隔离；
- 资源隔离与配额；
- 沙箱与执行环境隔离；
- 权限与最小特权隔离；
- 网络隔离；
- 隔离失效与逃逸风险。

查询已经明确包含“上下文与状态隔离”“会话隔离”“共享内存污染”
和“记忆库／向量库数据隔离”。最终 40 块也召回了一些会话、上下文和记忆
内容，但没有包含 worktree、checkpoint、revert 或失败任务恢复块。

因此 thinking 能稳定把 planner 从纯安全机制扩展到“会话／状态／记忆”
语义面，但还没有自然扩展到“并行任务工作区和生命周期恢复”。

首次 thinking 运行生成了 7 个 facets，被旧 schema 的 6-facet 上限拒绝。
该上限没有写进提示词，且与“查询数量随主题复杂度变化”的策略冲突；
当前已放宽为每个 subtopic 最多 8 个 facets。

### planner v15 多解释 facet 探针

在 planner v15 中新增规则：

> 如果一个 subtopic 具有多种合理解释，请对每种解释分别发现和列举
> facets，不要只保留最主要的一种解释。

使用 topic `AI Agent 的隔离`、thinking 关闭调用一次：

- 生成 1 个 subtopic、6 个 facets；
- semantic 23 条、keyword 23 条；
- 耗时 8.926 秒；
- input／output tokens 为 1,007／2,026。

新出现的 facets 包括：

- 多 Agent 系统隔离；
- 资源与权限隔离；
- 隔离的必要性与目标；
- 隔离的挑战与最佳实践。

其中“多 Agent 系统隔离”明确生成：

- `多 Agent 故障隔离与容错`；
- `多 Agent 状态隔离与共享状态管理`；
- `Agent 故障隔离`；
- `Agent 状态隔离`。

这说明新增规则能把“状态隔离”显式带进 facet 和双通道查询。该探针只验证
planner 输出，尚未重新执行检索和 Judge。
