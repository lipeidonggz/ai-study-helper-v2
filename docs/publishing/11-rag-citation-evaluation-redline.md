# RAG 可信引用评测纠偏：为什么引用语义支持不应成为绝对红线

> **摘要**：RAG 引用支持是受文本歧义和模型抖动影响的语义判断。本文用真实实验说明，为什么应把确定性完整性与语义风险分开治理。

> **30 秒判断**：有角标、block 存在，甚至 block 与 claim 共享相同数字，都不等于引用可信。但“引用支持”也不能由一次 LLM 判定直接升级成绝对红线。

> **适合谁读**：正在实现 RAG、Agentic RAG、引用展示、评测台或 LLM-as-a-Judge 的工程人员。

> **系列位置**：本文接续 [10 · DeepSeek RAG 实战：为问答系统实现可验证的可信引用](./10-deepseek-rag-trustworthy-citations.md)。文章 10 讲引用链路如何建立；本文讲引用评测的红线应该画在哪里。

> **实验环境**：DeepSeek Flash，`temperature=0`，thinking disabled。方法与具体模型解耦；这里观察到的评测边界同样适用于其他 LLM 和经典 `retrieve → generate` 流程。

> **源码基线**：[项目仓库](https://github.com/lipeidonggz/ai-study-helper-v2) · [固定提交](https://github.com/lipeidonggz/ai-study-helper-v2/tree/d18825576407a6d0b50c000d5fdb7abfffc881d8)

> **证据标记**：✅ 项目实测；🧪 可回归；🧠 当前判断。单次运行只作为故障现场，不写成长期发生率。

---

## 0. 术语先对齐

RAG 和引用评测里经常混用“来源”“证据”“通过”等词。本文按下表使用它们：

| 术语 | 本文含义 |
| --- | --- |
| **RAG** | Retrieval-Augmented Generation。先从知识库检索内容，再让模型基于检索结果生成答案。 |
| **LLM** | Large Language Model。本文指负责生成答案或进行语义判断的大语言模型。 |
| **Claim** | 答案中一段可以被单独核验真假的文本主张，例如一句话、一个列表项或一条表格行。“主张”是它在这里的通俗叫法。 |
| **Span** | Claim 在冻结答案中的精确字符范围，用 `start/end` offset 定位。UI 角标和评测都必须指向同一段原文。 |
| **Evidence block** | 本次检索或工具调用真实返回的一块原文。Block 有稳定 `block_id`，并属于某个版本文档和 collection 快照。下文简称“证据块”。 |
| **Citation annotation** | 正文生成后，把 claim 与 evidence blocks 建立关联的结构化记录，例如 `span_0001 → [block_1, block_2]`。下文简称“引用标注”。 |
| **Citation integrity** | 引用完整性。检查 span 是否存在、block 是否来自本次 attempt、quote 是否逐字出现在原文。它适合做确定性检查。 |
| **Citation support** | 引用支持性。检查被引用的证据在语义上是否支持 claim。它需要语义判断。 |
| **Supported** | evidence 在范围、强度、对象和关系上支持 claim。 |
| **Unsupported** | evidence 明确不能支持 claim，或与 claim 的数字、归属、方向、关系等发生冲突。 |
| **Unknown** | 现有输入不足以可靠判断。它既不是 supported，也不是 unsupported。 |
| **Pending** | 评测项尚未形成可靠结论。本文约定 pending 不算 pass，也不能为了产出发布结论而自动折算成 fail。 |
| **Redline** | 红线。发布门禁中“一旦确认违反就不能通过”的条件。本文会区分确定性红线和需要人工确认的语义红线。 |
| **LLM-as-a-Judge** | 用一个 LLM 评价另一个模型输出或证据关系的方法。它可以做语义筛查，但不能天然获得确定性裁判权。 |

这里最关键的一组区别是：

```text
Citation integrity：引用事实是否真实、可回溯
Citation support：被引用的证据是否在语义上支持答案
```

前者可以由程序复现；后者包含自然语言歧义和模型判断。

## 1. 一个很自然、但会走偏的直觉

做 RAG 引用时，最自然的工程判断通常是下面三步：

1. **答案引用的来源是否真的存在？**
   例如答案声称内容来自 `block_17`，但本次检索结果中没有这个 block，或它来自另一篇文档。这是“引用接到了不存在的地址”。

2. **答案展示的原文片段是否真的出现在来源中？**
   例如答案引用了一句话，但这个来源只包含相似主题，并没有出现该原文。这是“引用的原文对不上”。

3. **即使来源和原文都真实存在，这段原文是否真的支持答案中的完整主张？**
   例如原文说“83% 被拦截”，答案却写成“83% 被漏过”。数字、来源和原文都能找到，但数字含义被改了。这是“证据存在，但不支持该主张”。

前两项判断的是引用对象本身：

```text
来源 block 是否存在？它是否属于本次检索和正确文档？
quote 是否能从该 block 中逐字找到？
```

这些是确定的来源事实，可以由程序直接验证。

第三项判断的是两段自然语言之间的语义关系：

```text
这段证据是否支持该 claim 的全部对象、范围、强度和关系？
```

它看起来也像工程判断，但实际已经进入语义判断。用户点开引用，是为了确认答案有出处；当证据不支持答案时，引用确实不应当通过。难点在于，“支持”不是来源是否存在之类的二值事实，而是需要解释和裁决的语义结论。

例如：

```text
原文：
系统把良性命令被误拦的比例控制在约 0.4%，
代价是约 17% 的风险行为会漏过。

错误答案：
系统以降低摩擦为代价，
产生约 17% 的风险行为漏过。
```

两段文字有相同实体、相同对象和相同指标。只看数字，它们几乎一样；但收益和代价的角色已经被对调。

反过来，同一种语言现象也可能没有唯一解释：

```text
为了减少摩擦，约 0.4% 的良性命令被误拦；
代价是漏掉一部分风险行为。
```

如果目标是“最小化摩擦”，0.4% 是低摩擦的量化说明，17% 才在原文的代价位置。

如果换成“拦住所有过度行为”作为目标，分析者也可能把 0.4% 的良性误拦看作代价。此时他讨论的是一个不同的目标函数，而不是原文自己的取舍。

这不是简单的真假题。它同时涉及：

- 目标函数；
- 语义角色；
- 句子范围；
- 转述强度；
- 读者解释。

因此，引用支持不能只因为“它是产品红线”就自动获得确定性。

---

## 2. 业界方案解决了哪一半问题

截至 2026-10，主流模型平台已经提供了不同程度的引用能力。

### 2.1 模型平台的引用 API

| 方案 | 提供的能力 | 仍需应用补齐的部分 |
| --- | --- | --- |
| [Anthropic Citations](https://platform.claude.com/docs/en/build-with-claude/citations) | 答案文本块、文档内位置、`cited_text` | claim 与 evidence 的最终语义支持判断 |
| [OpenAI File Search](https://platform.openai.com/docs/guides/tools-file-search) | 答案片段、`file_id`、文件名 | 文件内部 block 范围、快照和语义 verdict |
| [Google Search grounding](https://ai.google.dev/gemini-api/docs/google-search) | answer span、来源 URL | 页面内部证据位置、可重建快照和语义 verdict |

这些能力解决的是“来源指针”：

```text
这句话对应哪个文件、URL、页面或文本片段？
```

它们没有替应用定义完整事实：

```text
这个 block 是否在本次快照中？
哪段答案文本是 claim？
多个 block 是否联合支持它？
证据在语义上是否真的支持？
```

### 2.2 学术研究与 LLM-as-a-Judge

[ALCE](https://arxiv.org/abs/2305.14627) 把引用质量作为独立评测维度；[RARR](https://arxiv.org/abs/2210.08726) 说明后置找证据和修正答案可以改善归因。

[LLM-as-a-Judge](https://arxiv.org/abs/2306.05685) 则提供了语义评估的通用范式。

三者共同说明：

> 答案正确性、引用位置和引用支持不是同一个问题。

但它们没有给出一个工程上的保证：

> 任意语义支持都能被单次 LLM 判断稳定地二值化。

引用完整性可以由程序证明；引用支持只能被评估，并带有不确定性。

---

## 3. 可信引用需要哪四层事实

我们的实现把引用拆成四个对象：

| 层 | 回答的问题 | 典型字段 |
| --- | --- | --- |
| Claim | 被支持的到底是哪段答案文本？ | `span_id + start/end + text` |
| Source | 证据来自哪个版本化来源？ | `snapshot_id + doc_id + block_id + text` |
| Link | 哪些证据联合支持这个 claim？ | `span_id → block_ids[]` |
| Verdict | 证据在语义上是否支持 claim？ | `supported / unsupported / unknown` |

前两层和 Link 的一部分可以确定性验证：

```text
span 是否精确落在冻结答案？
block 是否来自本次真实工具结果？
quote 是否能从 block 原文逐字找到？
```

只有最后的语义支持判断，才需要 LLM。

这也是第一个重要分界：

```text
引用完整性 ≠ 引用支持性
```

完整实现见 [ADR 0012](https://github.com/lipeidonggz/ai-study-helper-v2/blob/d18825576407a6d0b50c000d5fdb7abfffc881d8/docs/decisions/0012-harness-owned-claim-citation-annotation.md)。

---

## 4. 实验：固定答案，只改变判官

为了区分 Agent 问题与判官问题，我们冻结同一个 `c2-001 N=10` 运行：

- 10 个答案不变；
- evidence blocks 不变；
- prompt bundle 和 trace 不变；
- 只追加新的 citation judgment revision。

然后迭代引用判官。

### 4.1 第一轮：判官规则看起来正确

早期判官能发现一部分明显的数字错配，但在“收益／代价角色对调”上出现漏检。

于是我们加入了：

- 因果、条件、比较、取舍和方向规则；
- `relation_check` 结构；
- 数字、实体和关系角色的比较。

结果并不是稳定收敛，而是出现了新的问题：

- 主判官有时漏填 `relation_check`；
- 补上窄关系复核后，复核模型又被 evidence 反向影响；
- 独立提取角色后，claim 和 evidence 仍需解释标点、连接词和目标框架；
- 为修一个表达增加规则，又会让另一种合法表达变成 fail 或 pending。

### 4.2 同一个答案，判官版本变化就能改变红线

在完全相同的 10 个答案上，判官版本的演进曾出现这样的结果：

```text
版本 A：7 pass / 3 fail
版本 B：8 pass / 1 fail / 1 pending
版本 C：9 pass / 1 fail / 0 pending
```

后来针对根因重做角色提取，结果又发生波动。

这不是说最后一次数字一定更接近真值，而是说明：

> 当 Agent、答案和 evidence 全都不变时，仅判官变化就足以改变“是否发生红线”。

如果一次判官 fail 会自动阻断发布，发布结论就同时受语义灰区和模型抖动支配。

---

## 5. 三类最容易伪装成“确定性”的语义错误

### 5.1 数字相同，角色相反

```text
83% 是拦截率，不是漏过率。
17% 是漏过比例，不是拦截比例。
```

只匹配数字会通过；只有比较“数字对应的指标含义”才能发现错误。

这种错误风险很高，可以作为语义红线候选，但需要：

- 数字含义在 claim 和 evidence 中都可定位；
- 对象和时点一致；
- 由多名复核者或固定金标准确认，而不是只凭一次 judge fail。

### 5.2 标点看起来像关系

```text
减少摩擦——约 0.4% 的良性命令被误拦——
代价是漏掉风险行为。
```

破折号不是角色赋值。0.4% 可能是前一项收益的量化说明，不是新的代价。

但在另一个上下文里，同一个 0.4% 也可能真的被作者写成代价。因此不能只靠“出现破折号”决定。

这里暴露的是语义结构问题：

```text
哪个 cue 支配哪个 argument？
哪个 argument 是收益、代价、原因或结果？
```

### 5.3 连接词看起来像因果

```text
因此，auto mode 只是纵深防御的一层，不能替代沙箱。
```

“因此”承接前文，但这句话本身可能只是结论或分类，不一定构成新的 `条件 → 结果`。

连接词可以帮助读者理解上下文，但不能单独成为 LLM 建立关系三元组的依据。

---

## 6. 为什么继续加提示词，会形成“补丁摞补丁”

语义判官的常见演进是：

```text
发现一种错误表达
→ 增加一条规则
→ 当前样本通过
→ 另一种近似表达失败
→ 再增加一条规则
```

提示词里很快会出现：

- 特定数字；
- 特定连接词；
- 特定句式；
- 特定列表结构；
- 针对单一 case 的“例外说明”。

最后即使当前 10 个答案全绿，也无法回答：

- 规则是否覆盖没见过的语言；
- 新规则是否误伤了合法转述；
- 测试集是否已经被提示词看过；
- 判官是在评测，还是在记住样本。

这不是 prompt engineering 不够努力，而是把语义歧义问题错误地当成了封闭规则问题。

更合理的做法不是继续扩充规则，而是改变评测目标：

```text
程序负责能够证明的事实
LLM 负责不能确定证明的语义筛查
人负责人工复核和争议裁决
```

---

## 7. 红线应该分成三类

我们最终把引用质量拆成三层。

### 7.1 确定性引用红线

可以要求零违反：

- span 不存在；
- block 不在本次 attempt；
- block 不存在于快照；
- quote 不在原文；
- annotation offset 与答案不一致；
- 引用来源与工具结果对不上。

这些不需要“相信模型”。程序可以逐项重放和验证。

### 7.2 高置信语义红线

语义红线不能由 judge 直接宣布。judge 最多产生一个“红线候选”：

```text
semantic_redline_candidate
```

候选需要经过明确的复核流程，才可能变成最终红线：

```text
judge 发现明确语义冲突
        │
        ▼
形成红线候选：记录 error_type + claim + evidence + 具体冲突
        │
        ▼
人工复核
        │
        ├─ 确认错误 → 7.2 语义红线
        ├─ 否定候选 → 7.3 普通质量问题
        └─ 仍有合理分歧 → 7.3，并记录 disputed
```

候选必须同时满足：

- 错误类型明确；
- 能定位到具体 claim 和 evidence；
- 证据中有明确的数字、对象、方向或角色；
- 理由指出具体冲突，而不是只给一个低分或“感觉不支持”；
- 有人工复核记录。

例如：

```text
原文：83% 是执行前拦截率。
答案：83% 是执行后漏过率。
```

这种错误不应被平均掉，但也不是“任意一次 judge 说 fail 就自动红线”。

### 7.3 普通语义质量问题

下列情况默认归入 7.3：

- 近义转述；
- 范围大小；
- 强度变化；
- 目标函数不同；
- 隐含因果；
- 多 block 联合覆盖；
- judge pending；
- judge 自身多次翻转；
- judge 给出 fail，但无法指出具体错误位置；
- judge 与人工判断不一致；
- 复核者之间无法达成一致。

可以用下面这张表落地区分：

| 情况 | 初始归类 | 最终处理 |
| --- | --- | --- |
| 原文 83% 是拦截率，答案写成漏过率，对象和指标唯一 | 红线候选 | 人工确认后可成为红线 |
| 答案明确把原文的收益端写成代价端，且证据角色明确 | 红线候选 | 人工确认后可成为红线 |
| judge 说 unsupported，但无法指出具体冲突 | 普通质量 | 人工复核或判官改进 |
| judge 对同一 packet 反复 flip | 普通质量＋判官问题 | 重新校准判官，不阻断产品 |
| 两位复核者对语义有合理分歧 | 普通质量 | 记录 disputed，不伪装成确定红线 |
| 部分支持、范围偏宽或者属于隐含推断 | 普通质量 | 进入质量指标和后续改进 |

复核记录至少要保存：

```json
{
  "semantic_severity": "critical_candidate | quality",
  "error_type": "metric_misattribution | direction_swap | paraphrase | scope | other",
  "judge_verdict": "unsupported",
  "review_status": "pending | confirmed | rejected | disputed",
  "reviewer": "reviewer-a",
  "reason": "说明具体冲突或为何存在合理分歧"
}
```

只有满足下面条件，才进入 7.2：

```text
semantic_severity=critical_candidate
AND review_status=confirmed
```

其余情况仍会影响质量指标、排期和 prompt 改进，但不应该把正文直接判死。

---

## 8. 人工判断也不是绝对确定

即使把语义判断全部交给人，也仍会出现分歧：

- 两位复核者采用不同目标框架；
- 同一事实在不同上下文中承担不同角色；
- 短列表项离开原文后产生歧义；
- 读者可能接受合理推断，也可能要求严格复述。

因此不能把“人工看过”直接等价为“客观且唯一”。

更可审计的做法是记录：

- 复核者；
- 判定时间和版本；
- 判官结论；
- 人工结论；
- 改判理由；
- 争议是否保留。

语义红线的单位不是一个分数，而是一条可追溯的裁决记录。

---

## 9. 推荐的引用评测架构

```text
冻结答案与 evidence 快照
        │
        ▼
确定性 citation integrity
        │
        ├─ 失败 ⇒ 确定性红线
        │
        ▼
单次语义 citation support
        │
        ├─ supported ⇒ 记录通过
        ├─ unsupported / unknown ⇒ 风险队列与人工复核
        │
        ▼
可选 relation advisory
        └─ 帮助诊断原因、结果、收益、代价和方向，不直接一票否决
```

主语义判官仍然需要理解关系。它可以回答：

```text
证据是否支持整个 claim？
如果有问题，是数字、范围、强度，还是关系方向？
```

但不必要求一个通用 judge 每次都稳定输出完整的角色三元组，再把单次结构化结果升级成红线。

关系判官可以作为：

- 高风险样本的二次诊断；
- 固定 packet 校准工具；
- 人工复核候选排序；
- 问题根因分析工具。

不作为默认产品门禁，也不把每一次语义分歧都变成发布失败。

---

## 10. 哪些工程实践值得保留

无论采用哪种模型，以下边界都值得保留。

### 10.1 正文先生成、后标注

正文只生成一次并冻结。citation annotator 只返回：

```json
{"span_id": "span_0001", "block_ids": ["block_1"]}
```

它不能重写答案，也没有资格把所有引用问题藏进正文。

### 10.2 引用证据必须来自本次真实检索

不能使用模型记忆、其他 attempt 的 block 或后来重新检索到的新快照补引用。

### 10.3 列表项作为 claim，完整列表作为 context

列表项需要独立核验，否则一个局部指标错误会被其他正确项稀释。

但判官需要看到完整列表上下文，否则“前者／后者／该比例”无法解释。

正确结构是：

```text
focus claim：一个列表项
context：完整列表和引导段
evidence：该列表项实际引用的 blocks
```

### 10.4 annotation 失败保留正文并进入 pending

引用标注失败不等于答案生成失败。

产品应保留正文并说明“引用标注未完成”；评测不能把 pending 当成“无引用即通过”。

### 10.5 正文先流式展示，引用最后批量出现

引用标注发生在正文冻结后。用户可以先阅读正文，再看到来源角标一次性出现；不应为了注记而阻塞首字。

### 10.6 展示引用不打断阅读

点击角标时，优先使用锚定悬浮卡显示标题、章节和证据摘要；只有用户明确选择时才进入文档详情页。

---

## 11. 最小工程检查清单

- [ ] Claim 有稳定 ID、精确 offset 和不可变原文。
- [ ] Block 来自本次真实检索或工具结果。
- [ ] Annotation 只返回 `span_id + block_ids[]`。
- [ ] Span、block、snapshot 和 quote 都能确定性校验。
- [ ] `citation_integrity` 与 `citation_support` 分开。
- [ ] 语义 support 不因一次 judge fail 自动成为绝对红线。
- [ ] 高置信语义错误有人工复核记录。
- [ ] Pending 不算 pass。
- [ ] Judge prompt 不含具体 case 数字和原句。
- [ ] 固定 packet 重复测试，区分输入变化与 judge 抖动。
- [ ] 列表项 claim 有完整列表 context。
- [ ] Trace 保存 prompt、模型、token、artifact 和 revision。
- [ ] 产品 UI 不修改原始答案文本。

---

## 12. 结论

可信 RAG 引用不是“给每句话加一个角标”，也不是“让一个 LLM 判官说 pass”。

它至少需要：

```text
可回溯 claim
+ 固定 evidence snapshot
+ 确定性完整性检查
+ 受控语义判断
+ 可审计人工复核
```

红线应该画在系统能够稳定证明的地方：

| 问题 | 合适的门禁 |
| --- | --- |
| block 不存在、quote 不在原文、snapshot 对不上 | 确定性硬红线 |
| 明确的事实、数字或方向错误，且人工复核一致 | 条件语义红线 |
| 转述、范围、目标框架、隐含关系等争议 | 质量指标、pending、人工复核 |

一句话：

> 引用语义支持是必须测的质量，但不是可以由单次 LLM 判断绝对化的红线。

## 13. 一页速查

```text
可信引用 ≠ 有角标
可信引用 ≠ block 存在
可信引用 ≠ LLM judge pass

可信引用 =
  可回溯 claim
  + 固定 evidence snapshot
  + 确定性完整性
  + 受控语义判断
  + 可审计人工复核
```

---

## 参考资料

以下资料核对于 2026-10-26：

1. [Anthropic — Citations](https://platform.claude.com/docs/en/build-with-claude/citations)
2. [OpenAI — File search](https://platform.openai.com/docs/guides/tools-file-search)
3. [Google — Grounding with Google Search](https://ai.google.dev/gemini-api/docs/google-search)
4. [Gao et al. — Enabling Large Language Models to Generate Text with Citations](https://arxiv.org/abs/2305.14627)
5. [Gao et al. — RARR: Researching and Revising What Language Models Say](https://arxiv.org/abs/2210.08726)
6. [Zheng et al. — Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena](https://arxiv.org/abs/2306.05685)
7. [项目 ADR 0012](https://github.com/lipeidonggz/ai-study-helper-v2/blob/d18825576407a6d0b50c000d5fdb7abfffc881d8/docs/decisions/0012-harness-owned-claim-citation-annotation.md)
8. [项目切片 029](https://github.com/lipeidonggz/ai-study-helper-v2/blob/d18825576407a6d0b50c000d5fdb7abfffc881d8/docs/development/slices/029-citation-evaluation-reliability.md)
9. [项目切片 031](https://github.com/lipeidonggz/ai-study-helper-v2/blob/d18825576407a6d0b50c000d5fdb7abfffc881d8/docs/development/slices/031-citation-semantic-calibration.md)
10. [项目切片 036](https://github.com/lipeidonggz/ai-study-helper-v2/blob/d18825576407a6d0b50c000d5fdb7abfffc881d8/docs/development/slices/036-citation-context-and-judge-separation.md)
