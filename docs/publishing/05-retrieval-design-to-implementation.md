# Agentic RAG 检索侧落地：五个工具跑起来后，我们改了哪些契约？

> **摘要**：五个检索工具接上真实模型后，预算归属、范围过滤、工具语义和引用键都暴露了新问题。本文复盘实现如何反向修正设计。

## 从“设计出五个工具”到“证明它们能作为证据进入 Agent”

> **30 秒判断**：检索工具返回若干 chunks，不等于 Agent 获得了可用证据。证据还必须可限定范围、能进入上下文、能追溯原文，并且能在同一次运行中保持不变。本文复盘一个 Agentic RAG 检索设计从纸面契约落到真实代码后，哪些接口被修正、哪些原则被验证。
>
> **适合谁读**：已经会搭建向量检索，正在实现 Agentic RAG、工具调用或多轮检索的人；遇到过“检索结果看起来没错，但回答引用、预算和失败归因混乱”的开发者；以及正在做 RAG 设计落地和回归体系的人。
>
> **系列位置**：本文属于[《学习问答助手Agent实践》](https://juejin.cn/column/7697101748126007305)的实现复盘部分。设计侧已经发布[《Agentic RAG检索侧设计：从模式选型到工具、循环与提示词》](https://juejin.cn/post/7697829509392121865)，本文不重复模式选型和 ReAct 基础，而是回答一个更落地的问题：设计进入实现后，哪些契约必须改？
>
> **代码库**：[ai-study-helper-v2](https://github.com/lipeidonggz/ai-study-helper-v2)
>
> **结构速览**：§0 设计落地后的核心问题；§1 五个工具怎样划分不确定性；§2 为什么范围必须先于召回；§3 双路检索与不可静默降级；§4 `result_tokens` 为什么最终归 Harness；§5 引用键与快照；§6 真实模型反馈；§7 哪些设计被修改、补全和验证；§8 一页速查。
>
> **证据标记**：✅ 项目实测＝真实快照或真实模型运行；🧪 可回归＝已有确定性测试；🤔 当前判断＝由当前实现推导出的结论。

---

## 0. 设计完成后，真正的问题变成了“什么算证据”

公开设计文已经回答了三个大问题：

```text
经典 RAG 还是 Agentic RAG？
Agent 应该拥有哪些检索工具？
纯 Agent 循环和提示词怎样组织？
```

但开始实现后，代码必须继续回答更细的问题：

- 工具返回一个 block，是否等于它可以进入模型上下文？
- 用户指定了文档范围，范围过滤应该发生在召回前还是召回后？
- dense 检索失败时，能不能悄悄退化成关键词检索？
- 一次工具结果超过上下文预算，应该截断、分页还是拒绝？
- 模型看到的结果、trace 保存的结果、用户点击的引用，是否指向同一份快照？

这些问题不改变“要用 Agentic RAG”的大方向，却会改变工具契约和系统边界。

最终形成的核心认识是：

> 检索侧真正交付的不是“若干文本块”，而是一份能被范围约束、预算准入、身份追溯和引用定位的证据。

---

## 1. 五个工具不是五个搜索按钮

设计阶段推导出五个工具：

| 工具 | 主要解决的不确定性 |
| --- | --- |
| `list_documents` | 用户说的是哪篇文档？知识库里有哪些候选？ |
| `get_outline` | 这篇文档大致讲什么，后续该读哪些章节？ |
| `search_blocks` | 语义相关证据可能在哪里？ |
| `list_blocks` | 指定章节的完整内容是什么？ |
| `scan_term` | 某个具体字符串到底有没有出现，出现了几次？ |

它们不能互相替代，因为输出的“确定性”不同。

`search_blocks` 是 Top-5 候选召回，适合语义搜索；`scan_term` 是对当前范围全部原文做固定字符串扫描，适合“有没有出现”“出现几次”“是否属于某篇文档”这类需要精确回答的问题。

源码里对这两个工具的说明非常直接：

```python
ToolDescriptor(
    name="search_blocks",
    description=(
        "需要按语义查找候选证据时调用；按语义与关键词混合检索相关块，"
        "可按文档限定范围，返回排序后的前 5 条候选（块引用＋全文）。"
        "它是候选召回，不是完整枚举；确认精确字符串是否存在应使用 scan_term。"
    ),
)

ToolDescriptor(
    name="scan_term",
    description=(
        "按原文字符串扫描，大小写不敏感，不做分词、词干或同义扩展。"
        "工具返回当前范围的全部命中块与命中数，不做上下文预算判断。"
        "需要概念或语义匹配时使用 search_blocks；"
        "count=0 只表示该字符串没有出现。"
    ),
)
```

这不是产品文案，而是模型选择工具时看到的协议。工具描述如果只写“搜索文档”，模型很容易用语义搜索回答“某字符串出现几次”；而 Top-5 本质上不能证明完整命中数。

实现中还收敛了一处细节：

```text
source 筛选：匹配规范来源名和用户可用别名
title_contains：只匹配规范标题
```

别名帮助用户找到文档，但不能变成文档的第二套身份；否则“同一个来源的多个别名”和“规范标题”会在筛选、引用和审计时产生歧义。

> ✅ 项目实测：真实 Agent 曾自然选择 `list_documents → search_blocks → list_blocks → Finish`，说明工具语义可以被模型理解，但工具选择是否稳定仍需评测验证。

源码入口：[tool_adapter.py](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/agent/tool_adapter.py#L96)

运行证据：[018 Agent Runtime 工具循环](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/docs/development/slices/018-agent-runtime-core.md#L129)、[工具选择风险](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/docs/development/slices/018-agent-runtime-core.md#L179)。

---

## 2. 范围不是召回后的过滤器，而是检索前的边界

Agentic RAG 很容易出现一个问题：用户或模型指定了一篇文档，但系统先在全库召回 Top-K，再过滤掉其他文档。

这看起来只是实现顺序不同，结果却可能完全不同：

```text
先全库召回 20 条
  ↓
其中目标文档只有 2 条
  ↓
过滤后只剩 2 条

先限定目标文档
  ↓
在该范围内召回 20 条
  ↓
可能得到完整的 20 条候选
```

前者会降低目标范围内的召回率，还会让“范围内没有相关结果”的语义失真。

实现把同一个文档范围同时传给 dense 和 FTS，并在 LanceDB 查询时启用 prefilter：

```python
where_clause = _doc_scope_filter(doc_ids)

dense_query = (
    table.search(query_vector, vector_column_name="vector", query_type="vector")
    .select(["block_id", "doc_id", "block_index", "_distance"])
    .limit(profile.candidate_depth)
)
if where_clause is not None:
    dense_query = dense_query.where(where_clause, prefilter=True)

keyword_query = (
    table.search(keyword_terms, query_type="fts", fts_columns=FTS_FIELD_NAME)
    .select(["block_id", "doc_id", "block_index", "_score"])
    .limit(profile.candidate_depth)
)
if where_clause is not None:
    keyword_query = keyword_query.where(where_clause, prefilter=True)
```

范围还必须区分三种结果：

| 结果 | 含义 |
| --- | --- |
| `scope_empty` | 范围合法，但范围内没有 block |
| `no_match` | 范围有内容，但本次查询没有候选 |
| `scope_not_found` | 显式指定的文档或章节不存在 |

`scope_not_found` 不能返回“其余文档的部分结果”。否则模型会以为已经完成指定范围检查，而实际上系统悄悄缩小了范围。

实现因此采用 fail closed：

```python
missing_ids = [
    doc_id
    for doc_id in unique_ids
    if doc_id not in self._documents_by_id
]
if missing_ids:
    raise RetrievalFailure(
        "scope_not_found",
        f"documents do not exist in the fixed snapshot: {', '.join(missing_ids)}",
    )
```

> 🧪 可回归：测试同时覆盖了“缺失范围必须在 query embedding 前失败”和“两路检索只能返回指定文档的块”。

源码入口：[hybrid.py 双路 prefilter](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/retrieval/hybrid.py#L71)、[service.py 范围校验](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/retrieval/service.py#L461)、[范围回归测试](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/tests/test_hybrid_search.py#L98)、[缺失范围测试](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/tests/test_hybrid_search.py#L118)。

---

## 3. 混合检索的难点不是拼两条结果，而是定义不可变性

第一版混合检索采用：

```text
dense 候选 20 个
关键词候选 20 个
RRF k = 60
最终返回 Top-5
版本：hybrid-rrf-v1
```

这些数字不是经过大规模评测得到的最优值，而是第一版可复现、可调优的起点。比数字更关键的是几条不变量：

1. dense 与 FTS 读取同一个 collection 快照；
2. 两路使用同一个 block 集合和同一个文档范围；
3. 融合公式和平局规则固定；
4. query embedding 使用入库时同一模型、同一维度、`text_type=query`；
5. 检索分数只进入内部诊断和 trace，不进入 Agent payload。

RRF 的实现很短，但平局规则不能省：

```python
scores[block_id] = (
    scores.get(block_id, 0.0)
    + 1.0 / (rrf_k + rank)
)

candidates.sort(
    key=lambda candidate: (
        -candidate.score,
        candidate.doc_id,
        candidate.block_index,
        candidate.block_id,
    )
)
```

如果只按分数排序，浮点分数相同的候选可能因底层存储顺序不同而变化。固定 `doc_id → block_index → block_id` 之后，同一快照、查询和参数才能得到稳定顺序。

源码入口：[hybrid.py RRF 与平局规则](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/retrieval/hybrid.py#L123)。

### 为什么不静默降级为纯关键词

当 query embedding 鉴权、限流或上游服务失败时，系统返回 `upstream_unavailable`，最多重试一次，不自动切换成 FTS-only。

原因不是“关键词检索不好”，而是它会改变接口语义：

```text
正常路径：dense + FTS + RRF
故障路径：FTS only
```

如果系统不明确暴露这次切换，调用方会以为两种结果来自同一检索能力。诊断时也无法区分：

- 是混合检索本身召回差？
- 还是 dense 已经失败，只剩关键词路径？

源码与回归入口：[service.py query embedding 重试](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/retrieval/service.py#L624)、[重试但不降级测试](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/tests/test_hybrid_search.py#L144)、[上游失败测试](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/tests/test_hybrid_search.py#L177)。

> ✅ 项目实测：真实快照查询产生 20 个 dense 候选、14 个 FTS 候选，最终返回 5 个完整块；两路 rank、原始分数和 RRF 分数保留在内部诊断中。

运行证据：[017 混合检索真实快照结果](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/docs/development/slices/017-hybrid-search.md#L105)。

---

## 4. `result_tokens` 最终为什么归 Harness

这是落地后最值得记录的契约调整。

一个看似自然的方案是：每个检索工具返回业务结果时，顺便计算 `result_tokens`，再交给 Agent 决定是否加入上下文。

但很快会发现：工具其实不知道完整计数对象。

真正进入模型上下文的是：

```text
Observation 包装
  + harness_status
  + tool 名称
  + canonical JSON
  + 业务 payload
```

如果让 `search_blocks`、`list_blocks` 和 `scan_term` 分别计算 token，它们不仅要共享 tokenizer，还要共享序列化规则、包装格式和预算概念。任何一个工具漏掉包装字段，预算就会低估。

最终责任被移回 Harness：

```text
工具：只返回业务 payload
Harness：生成完整 Observation
Harness：计算 result_tokens
Harness：判断能否进入 History
```

把关键控制流压到一起，代码大致是这样：

```python
observation_text = f"Observation:\n{_canonical_json(observation)}"
result_tokens = self._token_counter.count(observation_text)
prospective = (state.get("next_context_estimate") or 0) + result_tokens

if prospective > self._config.max_context_tokens:
    limitations = _with_limitation(
        limitations,
        code="context_budget_exceeded",
        message="工具结果超过单次上下文预算，完整结果未进入历史。",
        parameter="max_context_tokens",
        limit=self._config.max_context_tokens,
    )
    budget_observation = _observation(
        harness_status="context_budget_exceeded",
        tool=action.tool_name,
        payload=None,
        message="tool result would exceed the context budget",
    )
    budget_text = f"Observation:\n{_canonical_json(budget_observation)}"
    history.append(budget_text)
    tools_enabled = False
    finalization_mode = True
else:
    history.append(observation_text)
```

这里有一个非常重要的行为：超界时，完整工具结果不会进入下一轮 History，只留下明确的 `context_budget_exceeded` Observation。

为什么不能截断工具结果？

因为检索结果可能是一组证据块。按 JSON、字符或 token 随机截断后：

- 模型可能只看到句子的一半；
- 引用仍指向完整 block，但模型没有真正读过它；
- 用户无法区分“没有找到”和“找到了但系统没放进来”；
- 截断发生的边界很难稳定解释。

实现选择是完整结果要么准入，要么拒绝；拒绝后关闭工具，由 Harness 标记 `answer_completeness=partial` 和具体限制参数。

这形成了三个容易混淆、但约束对象不同的控制上限：

| 控制项 | 约束对象 |
| --- | --- |
| `max_context_tokens` | 单次模型调用最多携带多少上下文 |
| `max_request_tokens` | 整次问题所有对话模型调用累计消耗多少 token |
| `max_tool_calls` | 一次问题最多执行多少次逻辑工具调用 |

此外还预留 `finalization_reserve_tokens`，避免累计预算耗尽后连最终收尾回答都无法生成。

配置与默认值源码入口：[runtime.py AgentRunConfig](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/agent/runtime.py#L68)。

> 🧪 可回归：测试会把一条工具 Observation 计为超界，断言完整正文没有进入第二次模型调用、终止原因为 `context_budget_exceeded`，并生成了用户可见的未完成声明。

对应断言的关注点也很直接：

```python
assert tool_event.payload["admitted"] is False
assert "context_budget_exceeded" in chat_provider.requests[1][1].content
assert "MCP 沙箱边界" not in chat_provider.requests[1][1].content
```

第二次模型可以知道“刚才的工具结果没有准入”，但不能继续读取那份超界正文。

源码入口：[runtime.py](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/agent/runtime.py#L1396)、[test_agent_runtime.py](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/tests/test_agent_runtime.py#L533)。

相关决策已经独立记录在 [ADR 0008](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/docs/decisions/0008-harness-owned-result-token-accounting.md#L1)。这说明它不是一次局部重构，而是检索工具与 Harness 之间的责任边界调整。

---

## 5. 快照和引用：检索正确，不等于证据可用

检索侧还有两个身份问题。

### 5.1 一次请求只能使用一份快照

请求开始时固定 collection 快照。此后即使后台发布了新快照，本次 Agent 循环中的所有工具仍读取旧快照；新快照只影响后续请求。

否则会出现一种很难解释的状态：

```text
第 1 轮 search_blocks 查旧快照
第 2 轮 list_blocks 查新快照
第 3 轮引用拼成第三套版本
```

同一次回答里的证据可能来自不同版本，trace 也无法解释最终结论。

读取文档 artifact 时还要同时校验：

- 文档 ID 和版本；
- block 集合身份；
- artifact SHA-256；
- section 与 block 数量；
- block ID 与 `block_index` 唯一性。

这里的原则是：快照不是目录名，而是一组可验证事实。

源码与回归入口：[service.py artifact 校验](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/retrieval/service.py#L473)、[checksum 篡改测试](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/tests/test_retrieval_read_layer.py#L236)。

### 5.2 引用键必须只有一套规范格式

真实界面验收曾连续发现两个问题：

1. 工具返回的 `block_id` 已经包含文档身份，后端来源键又拼接了一次 `doc_id`；
2. `block_id` 本身可能包含多个 `#`，前端按第一个 `#` 截断后无法匹配完整键。

最终规范来源键直接使用全局唯一的 `block_id`，前端按引用分隔符拆分并保留完整键值。

这不是单纯的显示 bug。它说明检索侧的最后一个交付边界不是“返回 block”，而是：

```text
用户能从答案里的引用
回到本次运行返回的同一 block
```

一旦快照漂移或引用键被重新拼装，模型即使引用了真实证据，产品层也可能无法证明。

源码与故障记录入口：[runtime.py 规范来源键](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/agent/runtime.py#L1772)、[chat-markdown.ts 引用解析](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/frontend/src/chat-markdown.ts#L23)、[020 引用键故障记录](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/docs/development/slices/020-streaming-chat.md#L148)。

---

## 6. 真实模型给出的反馈

Fake provider 能证明控制流，但不能证明真实模型会遵守工具语义。

第一轮真实运行暴露了几个设计文无法提前确定的问题：

- DeepSeek 默认开启的隐藏 reasoning 会和显式 ReAct 重复承担规划，首版因此关闭 thinking；
- 一次真实运行自然选择了 `list_documents → list_blocks → Finish`，使用 2 次工具调用、3 次模型决策；
- 同一阶段另一次真实问法直接 `Finish`，没有调用知识库工具，说明工具选择稳定性还不能靠提示词保证；
- 运行中真实 usage 成为下一轮上下文估算锚点，不能每次把全部历史重新估算并累积误差；
- 缺失 usage 和失败调用不能按 0 处理，否则累计预算会被系统性低估。

这组结果同时说明了两件事：

1. 设计中的纯 Agent 循环可以运行，工具协议能被真实模型理解；
2. “设计正确 + 一条真实 trace 成功”仍不足以证明稳定性，工具选择必须进入重复评测。

因此这篇文章的结论不是“检索侧已经完成”，而是“检索侧已经形成可评测的行为边界”。

证据入口：[018 真实运行与 usage 记录](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/docs/development/slices/018-agent-runtime-core.md#L129)、[reasoning token 与工具选择风险](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/docs/development/slices/018-agent-runtime-core.md#L167)。

---

## 7. 哪些设计被修改、补全和验证

把实现反馈按性质分开，比笼统说“设计调整”更准确。

| 类型 | 问题 | 落地结果 |
| --- | --- | --- |
| 修正设计 | 早期让工具返回 `result_tokens` | 工具只返回业务 payload，Harness 统一生成 Observation 并计数 |
| 补全契约 | 来源别名与标题筛选语义不清 | 别名用于定位，`title_contains` 只匹配规范标题 |
| 补全契约 | 混合检索只在原则上说 dense＋FTS | 固定 20／20／60／5，并记录 `hybrid-rrf-v1` |
| 补全契约 | 文档范围应在哪一步过滤 | dense 与 FTS 均在召回前使用同一 prefilter |
| 修正实现 | 引用键出现双 `doc_id` 和多 `#` 截断 | `block_id` 作为唯一规范来源键 |
| 强制边界 | 工具结果超过预算时怎么办 | 不静默截断；拒绝准入并生成确定性 partial 状态 |
| 验证设计 | 一次请求是否应固定快照 | reader 生命周期内固定快照，新快照只供后续请求 |
| 暴露风险 | 真实模型是否总选正确工具 | 单次 trace 不足，必须进入 N 次评测 |

可以看到，真正被“推翻”的设计只有预算归属这一处；更多工作是把设计中的原则补成可实现、可测试的契约。

这也是从设计阶段进入实现阶段后的正常状态。设计不可能预知所有接口细节，但必须提供足够稳定的原则，让实现可以判断：

```text
这是局部算法选择，
还是系统责任边界发生了变化？
```

像 `result_tokens` 从工具迁到 Harness，属于责任边界变化，必须升格为明确决策；像候选深度取 20，则属于可回退参数，用 profile 版本记录下来即可。

---

## 8. 一页速查

**设计文回答**：为什么选择 Agentic RAG，五个工具分别做什么，纯 Agent 循环与提示词怎样组织。

**实现文回答**：检索结果怎样才算真正成为可进入 Agent 的证据。

**六条落地不变量**：

- 同一请求固定一份 collection 快照；
- 文档范围在两路召回前生效；
- 语义搜索和字面扫描不能互相冒充；
- 工具只返回业务结果，Harness 负责 Observation 与预算；
- 完整结果不能进入上下文时，明确拒绝，不静默截断；
- 用户引用必须能解析回本次运行的真实 block。

**概念化后的源码边界**：

```python
# 工具只返回业务 payload
execution = await self._adapter.execute(action.tool_name, action.arguments)

# Harness 统一包装并在准入前计数
observation_text = f"Observation:\n{_canonical_json(observation)}"
result_tokens = self._token_counter.count(observation_text)

# 不满足预算就不把完整结果写入 History
if prospective > self._config.max_context_tokens:
    history.append(budget_text)
```

**最重要的一句话**：

> Agentic RAG 的检索质量不只在“有没有搜到”，还在于系统能否证明搜到了什么、结果是否被允许进入上下文，以及用户能否沿着引用回到同一份原文。
