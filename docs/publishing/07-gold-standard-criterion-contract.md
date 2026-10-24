# 金标准不是参考答案：为什么最后把判官输入删到只剩答案和一条判据

> **摘要**：18 个用例的判据被全部重写：去掉问题、证据和指代，只保留答案与一条可独立执行的规则。本文复盘金标准如何从评分说明变成判官契约。

## 从“判官应该看到更多上下文”到“判官只执行一条规则”

> **30 秒判断**：把用户问题、检索证据、运行状态和参考答案一起交给 LLM 判官，看起来更公平，实际可能让判官重新回答一遍问题。真正需要修的不是提示词，而是金标准：每条 criterion 必须只凭当前答案就能独立判断。否则判官不是在验契约，而是在替测试作者补条件。
>
> **适合谁读**：正在写 LLM-as-a-Judge、人工标注规则或 Agent 评测集的人；遇到过“判官总是不稳定”“同一答案重跑结论不同”“判据明明写了，判官却各理解一套”的开发者。
>
> **系列位置**：本文属于[《学习问答助手Agent实践》](https://juejin.cn/column/7697101748126007305)的实现复盘部分。设计侧已经有[《金标准五步：用一个文档问答 Agent 演示“什么算对、怎么判”》](https://juejin.cn/post/7697089423961014308)，本文继续记录真正接入 checker 和 LLM Judge 后，18 个用例、69 条判据是怎样被整体重写的。上一篇[《模型会自己伪造工具结果》](./04-model-fabricated-observation-harness.md)讨论模型不能伪造执行事实；这一篇讨论判官不能被测试上下文替它补全规则。
>
> **代码库**：[ai-study-helper-v2](https://github.com/lipeidonggz/ai-study-helper-v2)
>
> **结构速览**：§0 为什么要删输入；§1 金标准不是参考答案；§2 criterion 自包含的五条规则；§3 18 个用例的整体重写；§4 主判官最小输入；§5 checker、主判官与引用判官；§6 不可变版本与解析边界；§7 真实运行；§8 检查清单；§9 一页速查。
>
> **证据标记**：✅ 项目实测＝真实 Judgment；🧪 可回归＝自动化测试固定行为；🤔 当前判断＝由当前实现推导出的结论。

---

## 0. 一开始，我们给了判官太多东西

早期方案很符合直觉：让 LLM 判官看到

```text
用户问题
+ Agent 答案
+ 当前 criterion
+ 本次检索到的证据块
+ 引用列表
+ 运行完整性
+ 其他辅助状态
```

理由是“信息越多，判断越准确”。

但这会带来四种污染：

1. **判官重新回答用户问题**，而不是只检查当前 criterion；
2. criterion 本来缺少的条件，被问题、证据或模型常识偷偷补齐；
3. 完整检索上下文可能让判官偏向 Agent 当时的推理路径；
4. 输入和调用成本随 trace 增大，长答案甚至无法完成判断。

所以我们最终做了一个反直觉的删除：

> 主判官只接收 `answer` 和当前一条 `criterion`。

没有用户问题，没有工具证据，没有 trace 状态，没有 checker 结果，也没有其他 Judgment。

这不是认为上下文永远没有价值，而是把不同认知任务拆给了不同执行者：

| 任务 | 应该看什么 | 执行者 |
| --- | --- | --- |
| 回答是否满足某条语义规则 | answer＋criterion | LLM 主判官 |
| 答案是否发生了截断 | Harness 记录的完整性 | checker |
| 引用 block 是否存在、归属是否正确 | answer 引用＋本次工具证据 | checker |
| 引用原文是否支持对应表述 | 引用片段＋对应句子 | LLM 引用判官 |

让一个判官同时做全部事情，最后得到的不是更全面，而是一个无法归因的混合结论。

---

## 1. 金标准不是“参考答案长什么样”

人工评测常把答案是否正确理解成：

```text
参考答案 ≈ Agent 答案
```

但 Agent 会用不同结构、不同措辞和不同细节回答问题。单纯比较参考答案文本，会把同义表达判错，也会把“措辞相同但漏掉关键条件”判对。

更可执行的模型是 predicate：

```text
答案是否满足 criterion？
  ├─ R：必须满足
  └─ X：不得违反
```

R 和 X 不是分数权重，而是不同方向的规则：

- R 是必要条件，缺一个就失败；
- X 是产品红线，出现一次就失败；
- `unknown` 表示输入不足以判断，不伪装成 pass 或 fail。

金标准因此不是“标准答案正文”，而是**对答案的一组可验证断言**。

这直接决定了判官的职责：

```text
不是：请你判断这个回答是否足够好
而是：请判断当前答案是否满足这一条明确规则
```

---

## 2. criterion 自包含的五条规则

### 2.1 不依赖用户问题

如果一条 criterion 必须看到问题才知道“该文”是哪篇、对象是什么、范围是什么，那么这条 criterion 本身就不完整。

错误依赖：

```text
答案必须说明该文把 MCP 归为外部内容来源。
```

`该文` 没有定义。判官如果知道问题，就可能替规则补上下文；如果不知道问题，只能靠猜。

自包含写法：

```text
针对 Anthropic《How we contain Claude》中 MCP 的定位，
答案说明 MCP servers 属于 agent 可触及的外部内容／外部资源之一，
并与第三方插件、网页搜索工具并列；
没有说明其所属类别，不算满足。
```

### 2.2 不使用跨 criterion 指代

以下词都不允许默认判官知道：

```text
该文
原文
两篇
三者
上述内容
```

每条 criterion 都要自己写清完整文章标题、对象、范围和判断方向。

### 2.3 不重复问题里的场景

用户问题在 ExecutionPlan 中已经冻结，criterion 不需要再写：

```text
当用户询问 MCP 时……
如果问题是……
```

这些条件对一件已经确定的事情没有增加信息，反而会让规则看起来比实际更完整。

只有答案自身存在分支时才使用条件，例如：

```text
如果答案声称完整列出架构调整，则必须包含“本地 MCP server 也移到 VM 外”。
```

### 2.4 一条 criterion 只表达一个判断

不能写：

```text
答案满足 R1，同时没有违反 X2，并且引用正确。
```

这会混合 criterion、红线、引用检查和人工规则。每条 criterion 必须能独立产生一个 verdict，才能独立重判、人工复核和聚合。

### 2.5 反例要写清，而不是靠判官猜边界

规则不仅说明“什么算满足”，还尽量说明“什么不算”：

```text
只重复“MCP 是攻击面”而没有指出其在文中的归类位置，不算满足。
```

这不是让文本变长，而是减少判官自行补条件。

> 🤔 当前判断：criterion 中每一条反例，都是把一次潜在的解释分歧转成可审计的文字。

---

## 3. 为什么最后重写了 18 个用例

真实判官接入后，我们最终重写了全部 18 个用例，共形成 69 条 active criterion revision。

主要动作包括：

- 删除问题、证据、运行状态等判官不需要的输入；
- 删除 5 条重复或互相覆盖的判据；
- 把所有 `该文／原文／两篇／三者` 改成完整指代；
- 删除问题已经固定的“当用户……时”前缀；
- 把“答案说了什么”和“运行层发生了什么”拆给不同执行者；
- 重新标注 checker 与 LLM Judge 的路由；
- 将整个审查结果形成不可变 case revision 和 active suite。

最终 69 条中：

```text
68 条由 LLM Judge 判断
1 条由 checker 判断
```

### 例：把“运行事实”和“用户告知”拆开

`cf-003` 要求模型在长答案被截断时明确告知用户。这里其实有两个问题：

```text
运行事实：这次 answer 是否真的 partial？
表达事实：答案是否明确告诉用户“不完整”？
```

前者可以由 checker 从 Harness 状态确定性判断，后者才需要 LLM Judge 理解答案文本。

所以它被拆成：

- `R1`：checker 检查 `answer_completeness=partial`；
- `R2`：LLM Judge 检查答案是否明确说明内容不完整。

这不是把同一个错误算两遍，而是把“发生了什么”和“用户是否被正确告知”拆成两种责任。

设计定稿入口：[023A 判据自包含修订](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/docs/development/slices/023A-evaluation-judgment.md#L113)、[cf-003 checker／judge 路由](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/evaluation/repository.py#L1325)。

---

## 4. 主判官只拿两样东西

最终的主判官输入在代码里就是两个字面字段：

```python
JudgeInputPacket(
    answer=task.attempt.answer or "",
    criterion=JudgeCriterionInput(
        rule_type=task.criterion.rule_type,
        rule_text=task.criterion.rule_text,
    ),
)
```

真实测试直接校验 JSON 顶层集合：

```python
assert set(judge_payload) == {"answer", "criterion"}
```

这行断言比“prompt 里没有写问题”更可靠，因为它检查的是实际发送给模型的数据。

主判官提示词也只要求一件事：

```text
criterion.rule_text 是唯一判定标准。
不得增加 criterion 未写明的条件。
```

### 为什么连问题都不给

因为问题已经通过 criterion 的书写质量被编码进去。

如果测试作者无法在 criterion 内说明要判断的对象，而必须额外提供问题才能判断，就说明规则仍然依赖隐含上下文。

这时正确动作是：

```text
修 criterion
而不是
给判官更多输入替它补洞
```

源码与测试入口：[judge_prompt.py](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/evaluation/judge_prompt.py#L9)、[主判官最小输入](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/evaluation/judgment.py#L809)、[输入字段测试](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/tests/test_evaluation_judgment.py#L335)。

---

## 5. checker、主判官和引用判官各管什么

拆分后的判定链更像“分诊”，而不是一个万能判官。

### 5.1 checker 管确定性事实

适合 checker 的内容：

- answer 是否被截断；
- 引用 block ID 是否存在；
- block 是否属于被引用文档；
- 精确数值是否相等；
- 输出字段和格式是否合法。

这些不需要语言理解，交给 LLM 只会增加抖动。

### 5.2 主判官管当前 criterion 的语义

只回答：

```text
satisfied / unsatisfied / unknown
violated / not_violated / unknown
```

它不负责查引用是否真实，也不负责诊断 Agent 为什么失败。

### 5.3 引用判官管“原文是否支持这句话”

引用完整性和引用支持不能混为一谈：

```text
引用完整性：block 存在，而且确实属于当前运行结果
引用支持：block 原文是否支持答案中的对应表述
```

前者是程序可以确定的事实，后者需要语义判断。两个检查都失败时，也不应该把责任直接推给主判官。

### 5.4 空答案不需要给每条规则加补丁

产品失败和非基础设施失败都进入判官，空答案也以 `answer=""` 进入主判官。

这意味着不需要给 checker 或 R1 额外增加“答案为空时失败”的条件。语义 criterion 本身就会判断空答案没有满足要求。

如果把空答案补丁塞进每条确定性规则，反而会让 criterion 承担多种判断，破坏职责边界。

---

## 6. 金标准本身也必须版本化

金标准不是配置文件里可以随手改写的一段字符串。

一次 criterion 修改可能改变：

- 某条历史 attempt 的 verdict；
- case 的 N/K 聚合结果；
- baseline 是否还能比较；
- Operator 人工复核是否有意义。

因此系统对 case revision、criterion revision 和 suite revision 都做了数据库级不可变约束。修改必须派生新 revision，不能原地覆盖。

### 一个真实解析问题

人工逐条复核时还发现，设计表的 `**边界**` 分区标题被旧解析器并入了 14 条用例的最后一项 R 判据。

这意味着数据库里的 active criterion 文本与设计原文不完全一致。

最终处理不是直接改旧记录，而是：

1. 修复解析器；
2. 重新生成 `minimal_judge_v2` revision；
3. 建立新的 active suite；
4. 保留 v1 和历史 suite 作为审计记录。

这说明金标准从 Markdown 进入执行系统时，还有一条容易被忽略的链路：

```text
设计文本
  ↓ 解析
criterion revision
  ↓ 路由
checker / LLM Judge
  ↓ 生成
Judgment
```

任何一步“看起来只是文本处理”，都可能改变最终评分。

源码入口：[SQLite 版本不可变约束](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/evaluation/database.py#L91)、[Markdown 判据解析](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/evaluation/cases.py#L15)、[minimal_judge_v2 revision](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/evaluation/repository.py#L1308)。

---

## 7. 真实运行验证了什么

在单样本 run `run_db855cdda0fe4c3687de521d5f7b2645` 中：

- 主判官 user 消息只包含 answer 和当前 criterion；
- 没有用户问题、工具证据、运行状态或其他 Judgment；
- R1／R2 输出 `satisfied`；
- X1 输出 `not_violated`；
- 原始输出只含 `verdict` 和 `reason`；
- prompt version、prompt hash、模型、usage 和 revision 都可追溯。

另一个 `partial` 样本 `run_0cd1a6649ed340d0b71df10b0a626b09` 则验证了拆分：

- checker 根据运行事实判断 R1 通过；
- LLM Judge 根据答案文本判断 R2 通过；
- 引用完整性独立通过；
- 引用支持因 73,611 字符超过输入上限保持 pending；
- 引用检查 pending 没有被伪装成引用失败，也没有覆盖两条 criterion Judgment。

这个案例很好地说明了为什么要拆：

```text
长答案可能让引用检查无法完成
但不等于答案本身一定错误
更不等于 checker 的运行事实没有结论
```

证据入口：[v2 主判官最小输入验收](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/docs/development/manual-tests/023A-evaluation-judgment.md#L41)、[cf-003 拆分运行验收](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/docs/development/manual-tests/023A-evaluation-judgment.md#L48)。

---

## 8. 一份可执行的 criterion 检查清单

写完一条 criterion，至少问：

- 不看用户问题，能判断吗？
- 不看其他 criterion，能判断吗？
- 不看检索证据，能判断吗？
- 文中出现的“该文／原文／两者”是否都能解析到唯一对象？
- 是否还在重复问题里已经固定的场景？
- “什么算满足”写清楚了吗？
- “什么不算满足”有没有覆盖最容易产生的偏差？
- 这条规则应该由 checker、LLM Judge 还是人工执行？
- 修改这条规则后，是否需要新建 revision？

只要有一条答不上来，先别怪判官。

---

## 9. 一页速查

**原设计**：让 LLM 判官看到问题、答案、criterion、证据和运行状态，综合判断。

**最终实现**：

```text
主判官：answer + 当前 criterion
checker：确定性运行事实
引用判官：引用原文是否支持答案
人工：只在明确边界内追加纠错
```

**五条写作规则**：

- 不依赖问题；
- 不跨 criterion 指代；
- 不重复已冻结的问题场景；
- 一条规则只回答一个问题；
- 正例和反例都写清。

**最重要的一句话**：

> 如果金标准离开问题和证据就无法判断，要修的是金标准；把问题、证据和运行状态塞给判官，只会把规则缺陷藏得更深。
