# DeepSeek RAG 实战：为问答系统实现可验证的可信引用（附源码）

> **摘要**：本文以 DeepSeek RAG 为例，附源码复盘如何把一次生成的答案拆成可检查主张、为每条主张绑定真实文档证据，并贯通引用展示、评测和 trace。

> **30 秒判断**：来源存在只证明“指针指向了一篇文档”，不证明“这段原文支持这句话”。可信引用必须明确被引用的主张范围、证据 ID、引用关系和支持结论。
>
> **适合谁读**：正在用 DeepSeek 或其它 OpenAI-compatible 模型做 RAG／Agent，希望实现可点击、可评测、可审计引用的工程师。
>
> **实现环境**：DeepSeek Flash 4.1，运行时模型标识 `deepseek-flash`，`temperature=0`，thinking disabled。供应商没有在 trace 中提供不可变权重快照。
>
> **代码基线**：[ADR 0012](https://github.com/lipeidonggz/ai-study-helper-v2/blob/1e436c3b554b1d1c497f07b1f51c7741478c4cda/docs/decisions/0012-harness-owned-claim-citation-annotation.md) · [027 实现切片](https://github.com/lipeidonggz/ai-study-helper-v2/blob/1e436c3b554b1d1c497f07b1f51c7741478c4cda/docs/development/slices/027-claim-aware-citation-annotation.md) · [源码库](https://github.com/lipeidonggz/ai-study-helper-v2)
>
> **证据标记**：✅ [项目](https://github.com/lipeidonggz/ai-study-helper-v2)实测；🧪 可回归；🤔 当前判断。

---

## 1. 先看一个“看起来有引用，实际上判不了”的答案

在基于文档的 RAG 问答中，“答案有出处、用户能检查”不是可有可无的 UI 装饰，而是核心质量要求。它直接影响用户能否确认事实、开发者能否定位幻觉、评测能否区分答案错误与引用错误，以及系统能否留下可审计的证据链。

问题在于，有角标不等于可检查。下面这个答案就是典型：

假设 DeepSeek 回答：

> Claude Cowork 当前的 agent loop 运行在虚拟机之外，代码执行仍留在 VM 内。本地 MCP server 也运行在宿主机上。[1][2]

我们用 **claim** 指答案中可以被单独判断真假、也应当能被证据支持或反驳的一段主张。上面这段答案就包含两个 claim，分别对应前后两句话；claim 也可以是列表项或表格中的一条结论。后文为简洁仍使用 claim，但在产品语境中可以直接理解为“可核验主张”。

它有两句话、一个引用组。这个问题不只属于评测环节：

- **用户**看到两个角标，却不知道该点哪个来源核验哪一句，也无法判断两个来源是否必须联合阅读。
- **评测模型**需要知道 `[1]`、`[2]` 分别覆盖哪条主张，不能从距离和标点推断。
- **开发者**遇到失败时，无法区分是模型放错来源、评测模型猜错范围，还是证据本身不支持。

具体到这段答案，用户和评测模型共同面临这些问题：

1. `[1]` 支持第一句、第二句，还是两句都需要？
2. `[2]` 是补充第一句，还是只对应第二句？
3. 如果两个 block 各自只支持半句，它们联合后是否足够？
4. 如果 `[1]` 不存在于本次检索，用户看到的来源是否失效，评测模型又该如何处理？

很多系统只把整段答案交给 LLM-as-a-judge 型评测模型，让它替用户和开发者一起猜测引用范围。最终得到的“引用通过率”混合了模型、评测模型和证据三层错误，无法知道问题究竟出在哪一层。

本文实现的目标不是让句末多几个角标，而是建立一套明确事实：

```text
哪个 claim
引用了本次哪段真实证据
这段证据是否支持它
```

> 下文把负责冻结正文、切 claim、执行工具、校验模型输出和保存 trace 的应用层称为 Harness。
>
> [本项目](https://github.com/lipeidonggz/ai-study-helper-v2)内部把负责判断引用是否支持答案主张的模型角色称为 `Citation Judge`。为避免把它误解成正式业务角色，本文统一使用更直观的名称“引用核验模型”。这种用模型评价模型输出的模式，业界通常称为 [LLM-as-a-judge](https://arxiv.org/abs/2306.05685)。

---

## 2. 为什么需要自己造引用层

截至 2026-10-25，几家大模型厂商的能力边界并不相同：

| 方案 | 能提供什么 | 仍缺什么 |
| --- | --- | --- |
| [Anthropic Citations](https://platform.claude.com/docs/en/build-with-claude/citations) | 多个 claim text blocks、文档内字符／页／content block location、`cited_text` | 仍需单独判断证据是否支持 claim；当前不兼容 structured outputs |
| [OpenAI File Search](https://platform.openai.com/docs/guides/tools-file-search) | 输出插入位置、`file_id`、文件名 | 文件内精确证据范围与支持 verdict |
| [Google Search grounding](https://ai.google.dev/gemini-api/docs/google-search) | response `start/end_index`、来源 URL | 页面内证据位置、快照与支持 verdict |
| [国内通用模型 API（含 DeepSeek）](https://api-docs.deepseek.com/) | 文本生成、tool calling；部分产品另提供搜索工具 | 面向调用方本地证据的完整 claim → source → support 契约 |

Anthropic 方案最接近本文需要的契约。官方文档说明 citation pointer 会被解析为结构化位置，并比纯 prompt 引用可靠；但“指针有效”仍不等于“语义上充分支持”。

OpenAI 和 Google 的 annotation 更偏“答案片段 → 文件／URL”，也没有直接替应用完成本地证据支持和引用核验。

尤其在国内，通用模型 API 的主要能力仍是生成、工具调用和搜索接入。它们可以帮助应用取得资料，但不会自动知道调用方 block 的版本、来源范围和评测口径，也不会替应用定义：

```text
哪段答案主张
由哪些本地证据支持
支持结论是否充分
```

所以问题不是 DeepSeek“不能做 RAG”，而是它提供的是生成与工具能力，不是完整的可信引用产品契约。

学术上，[ALCE](https://arxiv.org/abs/2305.14627) 把引用质量作为独立评价维度；[RARR](https://arxiv.org/abs/2210.08726) 则证明后置找证据并修正答案可行。两者共同支持一个判断：

> 引用生成和引用验证不是同一个问题。

因此，[本项目](https://github.com/lipeidonggz/ai-study-helper-v2)需要在模型之外补一层 Harness：

```text
冻结正文
+ Harness 切 claim
+ DeepSeek 只选证据 ID
+ 程序校验
+ 独立引用核验
```

---

## 3. [本项目](https://github.com/lipeidonggz/ai-study-helper-v2)的职责边界

[当前项目](https://github.com/lipeidonggz/ai-study-helper-v2)的知识来自本地 HTML collection，检索由其自有的五个只读工具完成。DeepSeek 负责：

- 选择工具和参数；
- 组织最终答案；
- 在标注阶段选择 claim 与 evidence 的对应关系。

Harness 则负责固定证据、校验引用和保存事实。OpenAI SDK 的 tool calling 只定义工具协议，不定义引用语义，因此引用层必须由应用自己补齐。

### 3.1 为什么不只靠提示词

最省成本的方案是要求主回答模型写：

```text
结论。【来源：block_17】
```

它的问题不是格式不好看，而是正文同时承担了用户表达、机器引用和引用核验输入。一旦模型把同一段两个 claim 共用一个标记，后续既不能可靠展示，也不能可靠评测。

最终方案是两阶段：

```text
第一阶段：DeepSeek 生成完整正文
第二阶段：DeepSeek annotator 只选择 span_id + block_ids[]
```

注意，“两阶段”不等于总共只调用两次模型。Agent 检索循环本身可能调用多次；这里增加的是**一次独立的引用标注阶段**。

### 3.2 方法产生于 Agentic RAG，但不依赖 Agentic RAG

这套方法是在[当前项目](https://github.com/lipeidonggz/ai-study-helper-v2)中形成的。[项目本身](https://github.com/lipeidonggz/ai-study-helper-v2)是 Agentic RAG：DeepSeek 会多轮选择工具，检索结果不是一次性 Top-K，而是本次 attempt 中多个工具调用的证据集合。

但后续可信引用流程并不依赖 Agent 循环。经典 RAG 同样可以使用：

| 环节 | Agentic RAG | 经典 retrieve-then-read |
| --- | --- | --- |
| 证据来源 | 多轮工具返回的 blocks | 一次检索返回的 Top-K chunks |
| 证据固定 | 合并本次 attempt 全部工具结果 | 固定本次请求使用的 chunk 集合 |
| 正文生成 | Agent finish | 生成模型输出 |
| claim 标注 | 冻结正文后独立 annotation | 完全相同 |
| trace/UI/评测 | 消费 citation artifact | 完全相同 |

两种路线真正共用的不是“多轮 Agent”，而是这个边界：

```text
先固定本次真实证据
→ 再标注 claim 与 evidence 的关系
→ 最后独立判断支持性
```

因此，即使系统只有 `query → top_k → prompt → answer`，也可以在同一套契约上增加可信引用。Agentic RAG 只是让证据来源更动态，并没有改变 claim、source 和 verdict 的关系。

---

## 4. 契约与架构

| 层 | 回答的问题 | 保存的数据 |
| --- | --- | --- |
| Claim | 被支持的是哪段答案文本？ | `span_id + start/end/text` |
| Source | 证据来自哪个版本化来源？ | `doc_id + block_id + 原文` |
| Link | 哪些证据对应哪个 claim？ | `span_id → block_ids[]` |
| Verdict | 证据是否真的支持 claim？ | `supported / unsupported / unknown` |

block 是本次工具实际返回、带稳定 ID 的证据块。

### 4.1 两个检查必须分开

```text
引用完整性：block 是否存在、是否属于本次 attempt
引用支持性：block 原文是否支持对应 claim
```

前者适合程序确定性检查，后者才需要语义核验模型。

如果答案引用了 `block_17`，而 `block_17` 确实是本次检索结果，我们只能证明指针有效。它可能只支持半句话，也可能内容相反。

---

### 4.2 完整实现链路

```text
question
  │
  ▼
DeepSeek Agent loop
  │  finish.answer（不写引用）
  ▼
frozen answer artifact
  │
  ├─ Harness 切 claim spans
  └─ 本次真实 tool evidence blocks
          │
          ▼
DeepSeek citation annotator
  │  { span_id, block_ids[] }
  ▼
contract validator
  ├─ span 精确匹配原文
  ├─ block 在 evidence 白名单
  └─ schema / 重复项 / 预算检查
          │
          ▼
citation artifact
  ├─ SSE → 前端角标与悬浮卡
  ├─ trace → 审计与复现
  └─ evaluation → 引用核验
```

五个不变量：

1. 正文只生成一次，原答案 artifact 不可改；
2. annotator 只返回 ID，不返回正文；
3. 所有 block 必须来自本次真实检索或工具结果；
4. annotation 失败保留正文并显式 pending；
5. 展示与评测消费同一份 citation artifact。

---

## 5. 实现步骤

### 5.1 冻结正文，确定性切 claim

主回答提示词为 `chat-v5`，不再要求模型输出 block ID：

```text
不要在 finish.answer 中输出“来源”、block_id、引用角标。
引用由 Harness 在正文生成完成后独立标注。
```

正文生成后，Harness 按 Markdown 结构切 claim：

- 段落；
- 列表项；
- 表格正文行；
- 句末标点。

并跳过标题、表格表头、fenced code、inline code 内部标点、列表编号和 emphasis 闭合标记边界。

每个 span 保存：

```python
class CitationSpan(BaseModel):
    span_id: str
    kind: CitationSpanKind
    start_index: int
    end_index: int
    text: str
```

然后在冻结答案上重复验证：

```python
answer[span.start_index : span.end_index] == span.text
```

源码：[`split_answer_spans()`](https://github.com/lipeidonggz/ai-study-helper-v2/blob/1e436c3b554b1d1c497f07b1f51c7741478c4cda/backend/app/agent/citations.py#L370) · [切分回归测试](https://github.com/lipeidonggz/ai-study-helper-v2/blob/1e436c3b554b1d1c497f07b1f51c7741478c4cda/backend/tests/test_citation_annotation.py#L133)

这一步不能交给 annotator 自由发挥。claim 边界会直接进入评测，切错范围等于改变判定对象。

---

### 5.2 DeepSeek 只做 ID 选择

#### 5.2.1 输入只来自本次 attempt

```python
class CitationAnnotationInput(BaseModel):
    question: str
    answer: str
    spans: list[CitationSpan]
    evidence_blocks: list[CitationEvidenceBlock]
```

evidence 只来自本次 Agent 工具调用，不使用模型记忆、其他 attempt 或后来重新检索的新快照。

#### 5.2.2 输出只有引用关系

```json
{
  "annotations": [
    {
      "span_id": "span_0001",
      "block_ids": ["block_17"]
    },
    {
      "span_id": "span_0002",
      "block_ids": ["block_12", "block_18"]
    }
  ]
}
```

annotator 不返回正文、摘要或改写后的 claim，避免制造第二份答案事实。

#### 5.2.3 Harness 做白名单和结构校验

Runtime 拒绝：

- 非法 JSON；
- 不存在的 span；
- 重复 span；
- 重复 block ID；
- evidence 白名单之外的 block；
- 无法从原文精确重建的 span。

核心源码：[`CitationAnnotator`](https://github.com/lipeidonggz/ai-study-helper-v2/blob/1e436c3b554b1d1c497f07b1f51c7741478c4cda/backend/app/agent/citations.py#L128) · [`_parse_annotations()`](https://github.com/lipeidonggz/ai-study-helper-v2/blob/1e436c3b554b1d1c497f07b1f51c7741478c4cda/backend/app/agent/citations.py#L549)

多个 block 的支持关系保留在同一条 annotation 中。UI 可以在同一 claim 后显示两个来源，评测也仍能看到原始组合。

---

### 5.3 失败时保留正文，引用进入 pending

annotation 可能因为非法输出、越界 ID、重复项、预算不足或 provider 故障而失败。

失败不会：

- 丢弃正文；
- 让 annotator 重写正文；
- 清空引用后显示通过。

系统会保留 answer，并同时记录：

```text
limitation
citation_annotation_status = pending
trace.error_code
前端“引用标注未完成”
评测引用检查 pending
```

#### 5.3.1 真实 pending 现场

Trace `327ce59b5795aebfd0db7cc1d568c73a` 中：

- 主答案因输出预算被截断；
- 候选 span 93 个；
- evidence block 86 个；
- annotation 输出上限 2,048 token；
- 两次调用均返回非法 schema。

系统保留了 3,877 字符正文，answer 为 `partial`，citation artifact 为 `pending`。界面同时显示“内容未完”和“回答完成，引用标注未完成”。

> ✅ [项目](https://github.com/lipeidonggz/ai-study-helper-v2)实测：pending 不会伪装成引用通过。单次样本不代表长期失败率。

---

### 5.4 trace 和前端消费同一份事实

#### 5.4.1 trace

每次 annotation 会增加独立 `citation.annotation` span，保存：

- provider / model；
- prompt 版本与 hash；
- claim span 数；
- evidence block 数；
- prompt / output artifact；
- 实际调用次数；
- 输入 / 输出 token；
- annotation 状态与错误码。

一次正常产品运行 `2c04b586e3c478c5ac84612050d06b42` 中，annotation：

- 输入 6,108 token；
- 输出 1,446 token；
- 耗时约 4.44 秒；
- 整个 run 约 15.58 秒。

这组数字只用于成本观察。证据越多，annotation 输入越大。

#### 5.4.2 Markdown offset

前端只在渲染副本插入角标，原答案字符串不变。

后端 offset 使用 Unicode code point，JavaScript 字符串使用 UTF-16。必须先转换 offset，否则 emoji 会让后续标记漂移。

源码：[`applyChatCitationMarkers()`](https://github.com/lipeidonggz/ai-study-helper-v2/blob/1e436c3b554b1d1c497f07b1f51c7741478c4cda/frontend/src/chat-markdown.ts#L67)

#### 5.4.3 表格引用

错误写法：

```markdown
| 用户负担 | 无需解读 bash |【来源：block_17】
```

Markdown parser 会正常解析表格，却丢弃行尾标记。

正确写法：

```markdown
| 用户负担 | 无需解读 bash【来源：block_17】 |
```

表格引用必须进入最后一个单元格，而不是紧跟在行尾竖线之后。

#### 5.4.4 就地悬浮卡

点击角标时，在角标旁展示来源标题、章节、摘要和“查看文档详情”，不滚动主页面。点击外部、Escape 或滚动时关闭。

引用核对首先应该服务于阅读，而不是把用户带到页面另一处。

#### 5.4.5 正文先流式展示，引用最后批量出现

两阶段方案不能等到引用标注结束才展示正文，否则新增调用会直接拖慢首字时间。

实际交互是：

1. DeepSeek 生成正文时，UI 继续通过 `answer_delta` 流式展示；
2. 正文完成后，界面进入“正在核对引用依据”；
3. 引用标注完成后，通过一次 `citations_available` 事件返回完整 annotation 集合；
4. 前端一次性把来源角标加到已经显示的正文上。

因此，额外调用增加的是**完整回答时间**，不是首字延迟。用户可以先阅读正文，再看到角标出现；如果 annotation pending，正文仍然保留，只显示“引用标注未完成”。

这里不应假装做“流式引用逐字出现”。在完整正文尚未冻结时，逐句确定 claim 范围并选择证据，反而会重新引入范围不确定性。引用是在正文之后形成的第二层事实，UI 也应清楚表达这个顺序。

源码：[Runtime 引用事件](https://github.com/lipeidonggz/ai-study-helper-v2/blob/1e436c3b554b1d1c497f07b1f51c7741478c4cda/backend/app/agent/runtime.py#L574) · [前端事件消费](https://github.com/lipeidonggz/ai-study-helper-v2/blob/1e436c3b554b1d1c497f07b1f51c7741478c4cda/frontend/src/App.vue#L2660)

---

### 5.5 评测直接消费 claim

#### 5.5.1 完整性与支持性分开

```text
citation_integrity
→ annotation 中的 block 是否存在于本次 trace 证据

citation_support
→ 每个 block 是否支持对应 claim
```

完整性失败是确定性红线；支持性由 `citation-rubric-v7` 判断。

引用核验模型的输入是：

```json
{
  "context_id": "annotation_0002",
  "kind": "claim",
  "text": "本地 MCP server 也运行在宿主机上。"
}
```

而不是整段答案。提示词明确规定：`kind=claim` 时不得扩展到前后句。

源码：[`_load_citation_annotations()`](https://github.com/lipeidonggz/ai-study-helper-v2/blob/1e436c3b554b1d1c497f07b1f51c7741478c4cda/backend/app/evaluation/evidence.py#L148) · [`_build_citation_task()`](https://github.com/lipeidonggz/ai-study-helper-v2/blob/1e436c3b554b1d1c497f07b1f51c7741478c4cda/backend/app/evaluation/judgment.py#L1501)

#### 5.5.2 真实评测对齐

评测 run `run_780dcfeedc474985a77f12f967b6fe67`、trace `b673b20f23759ea9910c2a23e8f1b6ff` 中有：

- 29 个 trace annotations；
- 29 个核验 contexts；
- 29 个 occurrences；
- 5 个 evidence blocks；
- 6 个批次。

逐项比对结果：context 缺失 0、多余 0、文本不一致 0、occurrence 缺失 0、preflight failure 0。29 个 occurrence 全部 supported。

> ✅ [项目](https://github.com/lipeidonggz/ai-study-helper-v2)实测：引用核验模型拿到的是 trace 中同一批 claim，不是重新解析出的近似上下文。

#### 5.5.3 pending 不参与平均

annotation pending 时，引用检查必须 pending。不能用其它 criterion 的 pass 抵消，也不能把它当作“无引用通过”。

---

## 6. 实测效果与边界

### 6.1 同段两句，不同证据

Trace `2c04b586e3c478c5ac84612050d06b42` 中：

- 第一句 `annotation_0001 → b00012`；
- 第二句 `annotation_0002 → b00013 + b00014`。

界面在第一句后显示一个来源，在第二句后显示两个来源。引用核验模型看到两个 claim，而不是一个“整段引用”。

### 6.2 Markdown 边界

真实样本依次暴露并修复了：

- inline code 内句号误切；
- 表头被当成 claim；
- 有序列表编号被单独切成 span；
- 粗体关闭标记与句子分离；
- 表格引用被放到行尾竖线后被 parser 丢弃。

这些不是演示用的边角案例，而是普通技术答案里真实出现的内容。

---

## 7. 什么时候该用原生 citations

判断标准不是“响应里有没有 citation 字段”，而是三件事能否恢复：

1. 答案中被引用的主张范围；
2. 来源内部可追溯、不会随页面更新而漂移的证据；
3. 证据是否支持该主张的独立结论。

按这个标准分三种情况：

1. **Provider 同时给出 claim、source-local evidence 和支持接口**：可以直接接入原生能力。当前多数 citation API 仍不是这种完整形态。
2. **Provider 只给文件或 URL annotation**：它只是来源定位，不等于可信引用。只有在能够恢复并固定来源内部证据快照时，才能把它补成完整链路；如果只能拿到一个会变化的 URL，应显示为“来源提示”，不能显示为“已验证支持”。
3. **Provider 只有 tool calling 或自建检索链路**：由 Harness 固定 evidence，自建从 claim 到支持判定的完整流程。

Anthropic document citations 最接近第一种的 claim + source 部分，但仍需要独立支持核验。OpenAI File Search 和 Google grounding 更接近第二种：它们的 annotation 指明了来源或答案片段，却不会替应用保证文件内部证据与语义支持。

[本项目](https://github.com/lipeidonggz/ai-study-helper-v2)属于第三种。DeepSeek 负责生成与选择，Harness 负责固定事实边界，引用核验模型负责语义支持。

---

## 8. 最小工程检查清单

- [ ] 正文只生成一次，finish 不写引用；
- [ ] claim 有稳定 ID、精确 offset 和不可变原文；
- [ ] block 只来自本次真实工具或 Top-K 检索结果；
- [ ] annotator 只返回 `span_id + block_ids[]`；
- [ ] schema、重复项、越界 ID 和预算全部校验；
- [ ] 失败保留正文，引用显式 pending；
- [ ] trace 保存模型、prompt hash、token 和 prompt/output artifact；
- [ ] UI 不修改原答案，正确处理 code point / UTF-16 offset；
- [ ] 引用核验模型收到精确 claim，而不是整段猜测；
- [ ] 完整性、支持性和 pending 分开聚合；

---

## 9. 一页速查

```text
DeepSeek Agent
  → 一次完整正文

Harness
  → 冻结 answer
  → 生成 claim spans
  → 固定本次 evidence blocks

DeepSeek annotator
  → 只返回 span_id + block_ids[]

Validator
  → span 精确匹配
  → block 白名单
  → schema / 重复项 / 预算

Citation artifact
  → UI 引用与 trace
  → 引用核验

Failure
  → 保留正文
  → citation pending
  → 不算通过
```

经典 RAG 可以把最上面的 `DeepSeek Agent` 替换成一次 `retrieve → generate`，从“冻结 answer”开始的后半段不需要改变。

一句话：

> 可信 RAG 引用不是句末编号，而是 claim、真实证据和支持判定三者之间可验证、可复现的关系。

---

## 参考资料

以下资料核对于 2026-10-25：

1. [Anthropic — Citations](https://platform.claude.com/docs/en/build-with-claude/citations)
2. [OpenAI — File search](https://platform.openai.com/docs/guides/tools-file-search)
3. [Google — Grounding with Google Search](https://ai.google.dev/gemini-api/docs/google-search)
4. [Gao et al. — Enabling Large Language Models to Generate Text with Citations](https://arxiv.org/abs/2305.14627)
5. [Gao et al. — RARR: Researching and Revising What Language Models Say, Using Language Models](https://arxiv.org/abs/2210.08726)
6. [项目 ADR 0012](https://github.com/lipeidonggz/ai-study-helper-v2/blob/1e436c3b554b1d1c497f07b1f51c7741478c4cda/docs/decisions/0012-harness-owned-claim-citation-annotation.md)
7. [项目 027 人工验收](https://github.com/lipeidonggz/ai-study-helper-v2/blob/1e436c3b554b1d1c497f07b1f51c7741478c4cda/docs/development/manual-tests/027-claim-aware-citation-annotation.md)
8. [Zheng et al. — Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena](https://arxiv.org/abs/2306.05685)
