# 11 · 语料锚定的主题覆盖检索

- 状态：实现设计草案，待人工审核
- 当前策略版本：`corpus-grounded-coverage-v10`
- 当前工具契约：`retrieval-tools-v9`
- 实现入口：[corpus_grounded_coverage.py](../../backend/app/agent/corpus_grounded_coverage.py)
- 接入位置：[tool_adapter.py](../../backend/app/agent/tool_adapter.py)
- 当前切片：[065](../development/slices/065-corpus-grounded-topic-coverage.md)
- 实验记录：[2026-10-31 facet contract、审计与稳定性](../development/experiments/2026-10-31-facet-contract-audit-stability.md)

## 1. 为什么单独设计

`search_topic` 与普通 `search_blocks` 的职责不同：

- `search_blocks` 回答“一条明确查询最相关的前几个 block 是什么”；
- `search_topic` 回答“围绕一个主题，知识库中存在哪些互补语义面，以及每个
  语义面有哪些证据”。

因此它不是一个更大的 Top-K 查询，而是包含主题解释、语义覆盖规划、查询展开、
候选合并、证据判断和覆盖诊断的复合工具。

## 2. 职责边界

### 2.1 本工具负责

- 以 topic 为唯一输入范围；
- 发现 topic 的多个合理解释和具体证据义务；
- 为每个证据义务生成 dense 与 keyword 查询；
- 从当前固定 collection 快照召回候选；
- 判断候选是否支持某个 facet；
- 返回全部至少部分相关的支持块；
- 在 trace 中记录规划、查询、短名单和 reranker 过程。

### 2.2 本工具不负责

- 不回答最终用户问题；
- 不决定最终答案应重点写哪些内容；
- 不根据当前问题的回答格式裁剪主题覆盖集合；
- 不读取 `retrieval_goal` 或其他答案使用意图；
- 不承诺数学意义上的知识库穷尽；
- 不修改金标准或 Judge。

原则：

```text
coverage tool 负责主题和证据覆盖
main Agent 负责从覆盖结果中选择、综合和回答
```

## 3. 输入与输出

### 3.1 输入

```json
{
  "topic": "需要覆盖的主题"
}
```

`topic` 是最小输入单位。当前不再接收 `retrieval_goal`。

### 3.2 输出

```json
{
  "blocks": [
    {
      "block_id": "...",
      "doc_id": "...",
      "doc_title": "...",
      "source_label": "...",
      "section_path": ["..."],
      "section_context": {},
      "text": "完整正文"
    }
  ],
  "empty_reason": null
}
```

输出只包含：

- reranker 判定为 `direct` 的 block；
- reranker 判定为 `partial` 的 block。

工具不做最终 40 块或 20k token 裁剪。

## 4. 主流程

```text
topic
  ↓
seed hybrid retrieval Top-50
  ↓
seed block 全文 + seed 文档章节大纲
  ↓
主题解释
  ↓
初版 facet plan
  ↓
facet contract / seed 引用校验
  ↓
facet audit
  ↓
审计后 facet contract / seed 引用校验
  ↓
dense + keyword 查询去重
  ↓
每条查询 Top-20
  ↓
block 合并 + facet provenance
  ↓
每 facet 临时 Top-20 短名单
  ↓
reranker direct / partial
  ↓
返回全部支持块
```

## 5. Seed 观察面

### 5.1 Seed 检索

工具首先使用原始 topic 做 hybrid 检索：

- `SEED_TOP_K = 50`
- `candidate_depth >= 60`

seed 的作用是观察当前知识库如何表达该主题，不是最终结果集。

### 5.2 Seed evidence

每个 seed block 包含：

```json
{
  "id": "S1",
  "doc_id": "...",
  "doc_title": "...",
  "section_path": ["...", "..."],
  "text": "完整正文"
}
```

block 正文不再按 350 字截断。

### 5.3 Seed 文档大纲

工具对 seed block 所属文档调用 `get_outline`，并向 planner 提供：

- 文档标题；
- 所有章节 path/title/depth；
- own/subtree block 数；
- 每节首句前 200 字符。

大纲只用于观察文档结构，不作为命中的正文证据。

失败时返回：

```text
coverage_seed_outline_failed
```

不允许把缺失大纲静默当成空结构。

## 6. 主题解释

主题解释只根据 topic 生成，目标是提供：

- 概念和对象；
- 机制与实现方式；
- 关系、适用范围和权衡。

解释不读取当前问题，不生成查询，不读取金标准。

### 证据状态

该步骤目前是设计假设，而不是已被受控实验证明的必要步骤。

已有证据只说明：

- 历史 explanation-only dense 查询在 17 条查询、76～107 个候选时，三个回归
  目标只召回 0～1 个；
- explanation-only 逐名词 FTS 能 3/3 召回，但需要 346 条查询并召回
  511/526 个 block，筛选成本不可接受；
- 当前 corpus-grounded 主链路同时使用 explanation 和 seed evidence，尚无
  “固定其他条件、只移除 explanation”的消融结果。

因此 explanation 目前只能视为待验证的 planner 辅助输入。后续必须用
with-explanation / without-explanation A/B 判断真实增益；不能因为主链路
包含该步骤就默认它有效。

## 7. Facet 数据契约

每个 facet 至少包含：

```json
{
  "name": "...",
  "scope": "...",
  "evidence_obligation": "...",
  "coverage_question": "...",
  "required_relation": "...",
  "origin": "corpus|hypothesis|both",
  "seed_refs": ["S1"],
  "semantic_queries": ["..."],
  "keyword_queries": ["..."]
}
```

字段职责：

| 字段 | 含义 |
| --- | --- |
| `name` | 便于人阅读的短名称 |
| `scope` | facet 的语义边界 |
| `evidence_obligation` | 必须找到哪些证据 |
| `coverage_question` | 检索完成后必须能回答的问题 |
| `required_relation` | 证据必须表达的机制、限制或因果关系 |
| `origin` | 来自语料、假设或两者 |
| `seed_refs` | 支撑该 facet 的 seed block |
| `semantic_queries` | dense 检索输入 |
| `keyword_queries` | FTS/BM25 检索输入 |

校验规则：

- facet 名称必须唯一；
- `corpus` 和 `both` 至少有一个 seed ref；
- 所有 seed ref 必须存在于本轮 seed evidence；
- initial plan 和 audit 后的 final plan 都执行同一校验。

## 8. Facet 缺项审计

### 8.1 输入

- topic；
- explanation；
- 完整 seed evidence；
- seed 文档大纲；
- 初版 facet plan。

### 8.2 检查目标

- 输入中重要但未落入任何 facet 的概念、机制、状态、过程和风险；
- 混合了多个独立证据义务的过宽 facet；
- 同义或重复 facet；
- 无输入依据的 corpus facet；
- 受单一解释、单一文档或单一分类框架影响的缺项。

### 8.3 输出

审计器返回：

- 最终 facets；
- `add / split / merge / rewrite / remove` 变化及原因。

审计只允许使用输入中存在的主题、解释、正文和章节信息，不得加入当前评测用例
专属的对象或目标答案。

## 9. 查询执行与候选合并

### 9.1 查询

最终 facet 中的 dense/keyword 查询按字符串去重。一个查询可以同时属于多个
facet，但只执行一次。

每条查询：

- `candidate_depth >= 60`；
- `top_k = 20`。

这里的 Top-20 是单条查询返回上限。

### 9.2 候选 provenance

每个候选记录：

- 来自哪条查询；
- dense 或 keyword；
- 查询内排名；
- 所属 facets。

### 9.3 Facet 内归一化

每条查询内部先把 dense distance 或 BM25 score 归一化到 `[0, 1]`，避免直接
比较不同查询的原始分数。

在每个 facet 内按以下顺序排序：

1. 最佳归一化分数；
2. 支持该 block 的查询数；
3. dense/keyword 模式覆盖；
4. block id 稳定顺序。

当前每个 facet 保留 Top-20。该值是覆盖正确性的临时基线，不是最终设计。

## 10. Reranker

### 10.1 输入

每个候选向 reranker 提供：

- block id；
- 前 1,600 字符正文；
- provenance facets 的完整 contract。

同时提供全部 facet catalog。reranker 必须检查全部 facets，而不只是候选召回时
挂载的 facet。

### 10.2 判断

- `direct`：正文明确满足 evidence obligation 和 required relation；
- `partial`：覆盖部分对象、机制或关系，但没有完整满足；
- 没有支持的 facet 不输出。

### 10.3 批量与重试

- 每批 8 个候选；
- 最多 3 批并发；
- 非法 JSON、未知 block id 或重复 block id 可重试一次；
- 第二次仍非法时返回 `coverage_rerank_invalid`；
- 不静默降级为空结果。

## 11. 最终排序与返回

支持块按以下顺序排序：

1. direct facet 数；
2. 支持 facet 总数；
3. 最佳 facet 归一化分数；
4. block id 稳定顺序。

当前不执行最终 40 块或 20k token 截断。

全部支持块交给 Harness。Harness 负责计算最终 `result_tokens` 并执行上下文
准入门禁；工具不返回自己的 token 计量结果。

## 12. 预算与并发

当前常量：

| 参数 | 当前值 | 状态 |
| --- | ---: | --- |
| seed block | 50 | 当前实现 |
| seed document outline 首句 | 200 字符 | 当前实现 |
| 单查询候选深度 | ≥60 | 当前实现 |
| 单查询返回 | Top-20 | 当前实现 |
| facet shortlist | Top-20 | 临时正确性基线 |
| reranker batch | 8 | 当前实现 |
| reranker 并发 | 3 | 当前实现 |
| reranker attempts | 2 | 当前实现 |
| 最终 40 块裁剪 | 关闭 | 已确认 |

## 13. Trace

`search_topic` 的 tool span 下包含：

- seed retrieval；
- explanation LLM；
- initial facet planning LLM；
- facet audit LLM；
- 每条 dense/keyword query；
- 每个 reranker batch。

LLM span 保存：

- provider/model；
- prompt version/hash；
- prompt/output artifact；
- input/output tokens；
- request id；
- 状态和错误码。

query span 保存 mode、query、facet 归属、结果数和融合候选数。

tool diagnostics 保存 facet 数、查询数、候选数、shortlist 数、支持数、
shortlist limit 和 audit changes。

## 14. 失败语义

| 阶段 | 错误码 | 行为 |
| --- | --- | --- |
| seed retrieval | `coverage_seed_query_failed` 或上游错误码 | 整体失败 |
| seed outline | `coverage_seed_outline_failed` | 整体失败 |
| explanation | `coverage_explanation_failed` | 整体失败 |
| initial facets | `coverage_facet_planning_failed` | 整体失败 |
| facet audit | `coverage_facet_audit_failed` | 整体失败 |
| plan 校验 | `coverage_plan_invalid` | 整体失败 |
| 任一 facet query | `coverage_query_failed` 或上游错误码 | 整体失败 |
| reranker 请求 | `coverage_rerank_failed` | 整体失败 |
| reranker 结构化输出 | `coverage_rerank_invalid` | 两次失败后整体失败 |

任一阶段失败都不能返回“部分成功”的 coverage 集合并让调用方误以为主题已覆盖。

## 15. 用例信息隔离

产品 prompt 中不得出现：

- 当前评测主题的专有术语；
- 目标 block 名称或 id；
- 由某个用例缺失项反推出来的固定 facet 名称或检查清单。

该规则由 [提示词卫生测试](../../backend/tests/test_corpus_grounded_prompt_hygiene.py)
保护。

评测用的目标块只属于回归探针，只能在评测和诊断代码中出现，不能进入产品
prompt、planner 规则或 reranker 规则。

## 16. “稳定性”的定义

### 16.1 当前 c13-003 回归探针

当前用三个块作为工程回归探针：

- 并行工作区隔离；
- checkpoint 恢复；
- 状态变更评估。

它们是用于检查召回链路的已知目标块，不是产品需求中写死的主题分类，也不是
prompt 输入。

### 16.2 真正的稳定性

“稳定召回三个目标块”是当前回归门槛之一，但完整的稳定性至少还包括：

1. 同一 topic、同一 snapshot 的重复运行持续生成有效的 facet contract；
2. 三个回归探针在重复运行中稳定进入候选、短名单、reranker 和最终结果；
3. 查询数、候选数和返回块数不出现不可解释的剧烈波动；
4. reranker 判断和协议错误率可接受；
5. 返回集合能够被 Harness 接纳；
6. held-out 主题也能形成合理 facets，而不是只对 c13-003 有效；
7. 主 Agent 能使用已返回证据形成完整答案。

所以：

```text
三个目标块 3/3 = 回归门槛
不是完整的主题覆盖稳定性
```

## 17. 当前实测状态

### 17.1 已确认有效

- 工具已经作为 `search_topic` 唯一实现接入；
- 旧 subtopic planner、运行开关和 `retrieval_goal` 参数已删除；
- facet contract、facet audit、细粒度 trace 和 reranker 单次协议重试已实现；
- 提示词卫生测试通过；
- 两个 held-out 主题可以生成内容相关的 facet plan。

### 17.2 尚未稳定

- 修复前的 c13 N=5 中三目标同时返回为 0/5；
- 干净 v10 的 c13 N=1 返回 worktree 和 checkpoint resume；
  checkpoint evaluation 进入短名单后未被 reranker 保留；
- held-out 主题在临时 Top-20 下分别返回 144 和 125 块，输出规模过大；
- block 级覆盖 qrels 尚未建立；
- 动态候选预算尚未确定；
- 完整覆盖集合与主 Agent 上下文的分离尚未实现；
- 主 Agent 仍存在已返回证据却未使用的问题。

## 18. 待解决问题

完整编号和状态见
[065 的完善前问题清单](../development/slices/065-corpus-grounded-topic-coverage.md#完善前问题清单)。

优先级最高的方向：

1. 用干净提示词在 c13 回归和多个 held-out 主题上完成稳定性测量；
2. 设计动态 facet 候选预算，替代固定 Top-20；
3. 建立 block 级 qrels；
4. 把完整覆盖集合与主 Agent 默认载荷分离；
5. 继续完成去重和查询边际价值分析；
6. 覆盖质量稳定后再处理缓存，优先获得重复调用成本和延迟收益。

## 19. 当前结论

当前实现已经解决了“单一 topic 查询无法覆盖多证据面”的基础问题，并建立了
可观测、可失败的执行链。但它仍处于实现设计阶段：

- 功能主路径可用；
- 泛化安全性已有自动门禁；
- 主题覆盖稳定性尚未达标；
- 候选预算和规模边界尚未解决。

在这些问题解决前，不能把 065 标记为完成，也不应把单次 `3/3` 目标召回写成
稳定能力。

## 20. 设计依据与相关工作

当前整体架构不是首创算法，而是成熟检索方法的组合：

| 当前模块 | 已有方法传统 |
| --- | --- |
| topic seed retrieval | pseudo-relevance feedback、pearl growing |
| 从 seed 观察语料再生成覆盖面 | corpus-grounded query expansion |
| topic 解释 | query expansion、semantic query planning |
| facet 分解 | faceted search、aspect-based retrieval、query decomposition |
| 每 facet 多查询 | multi-query retrieval、query fan-out |
| dense + keyword 合并 | hybrid retrieval、RAG-Fusion |
| 候选统一 rerank | two-stage retrieval：recall + rerank |
| facet audit | corrective retrieval、self-reflection |
| 多轮补查（未来） | iterative retrieval、IRCoT |
| 文档章节大纲 | structure-aware retrieval、parent/child retrieval |

### 20.1 系统综述检索

系统综述和证据综合领域长期使用：

- block building：把研究问题拆成若干概念块；
- 为每个概念块枚举同义词、受控词和拼写变体；
- 多查询、多数据库检索；
- 去重、筛选、补充检索和引用追踪；
- pearl growing：从已找到的关键文献继续扩展术语和证据。

当前 facet + 多查询 + 审计的思路与这类高召回检索最接近。

### 20.2 经典信息检索

经典 IR 已研究：

- query expansion；
- relevance feedback / pseudo-relevance feedback；
- multi-query expansion；
- recall-oriented retrieval；
- candidate generation + reranking。

其中“先取一批 seed，再从 seed 推 new queries”部分接近 pseudo-relevance
feedback。区别是当前使用 LLM 观察完整 block 和文档结构，而不是单纯用词频
模型扩展 query。

### 20.3 RAG 与 Agentic Retrieval

相关工作提供了直接依据：

- RAG-Fusion：多查询、多路召回和融合；
- HyDE：先构造语义展开，再进行向量检索；
- RAPTOR：利用分层摘要和树结构改善长文档检索；
- GraphRAG：围绕全局问题做多实体、多关系覆盖；
- Self-RAG / Corrective RAG：通过判断、补查或修正提高证据质量；
- IRCoT：让检索和推理交替推进；
- Anthropic Contextual Retrieval：利用上下文增强块的可检索性。

当前工具与这些工作的区别主要在系统边界：

- 输入固定为 topic，而不是完整问题；
- facet 被定义为可验证的证据义务；
- 返回的是覆盖证据集合，不在工具内部完成最终回答；
- 覆盖规划和内部阶段全部进入本地 trace。

## 21. 我们的增量边界

可以主张的增量是工程设计，而不是基础算法发明：

1. 把“主题覆盖”从回答 Agent 中拆成独立工具职责；
2. 用 topic-only 约束避免把答案使用意图混入覆盖范围；
3. 从固定 collection 的 seed block 全文和章节结构中生成 facets；
4. 为 facet 增加 evidence obligation、coverage question 和 required
   relation；
5. 在正式查询前增加 facet 缺项审计；
6. 用内部 span/artifact 追踪规划、审计、查询和 reranker；
7. 将最终 40 块和 Agent 上下文预算与主题覆盖集合解耦。

这些设计的有效性和新颖性都需要通过跨主题实验证明。当前不能声称该架构已经
优于其他 RAG 或 facet 检索方案。
