# 2026-10-30 · 语料锚定的主题覆盖面检索实验

- 状态：首轮完成，待产品化决策
- 前置实验：[主题覆盖检索工具实验](./2026-10-29-topic-coverage-tool.md)
- 相关切片：[064 主题覆盖检索](../slices/064-topic-coverage-search.md)
- 目标规模：约 1,000 篇文档、100,000 个 block
- 当前实验快照：`s_6b787258ba7d700c`

## 问题

现有 planner 从 topic 出发，先拆 subtopic，再枚举 meaning/facet，最后生成
semantic 和 keyword 查询。这条路径有三个问题：

1. facet 完全由 query-side LLM 根据通用知识猜测，没有观察当前知识库；
2. 模型倾向生成安全、权限、网络等常见分类，遗漏知识库特有的工作区、
   会话、任务状态和恢复语义；
3. 很难判断某个 facet 在当前知识库中是否有证据，以及何时已经搜全。

因此本实验把目标明确为：

> 对一个有限知识库中的主题，尽量找全所有真正相关的 block，并尽可能
> 证明预定义的语义覆盖面都已有证据。

这不是一次 Top-k 问答检索，而是高召回集合检索。

## 语义面的定义

本实验不再把 facet 定义为“subtopic 的一种抽象解释”，而定义为：

> 一个可以独立检索、独立判断覆盖状态，并能对应可观察证据的语义义务。

一个有效 facet 必须：

- 与其他 facet 有可辨认的语义差异；
- 说明要寻找的对象、机制、关系或风险；
- 能生成少量 dense 和 keyword 查询；
- 能根据返回 block 判断是否已有证据；
- 不依赖用例专属答案词。

## 假设

### H1：纯 topic 规划不足

只根据 topic 生成 subtopic/meaning，会遗漏知识库特有但语义相邻的覆盖面。

### H2：解释文本比直接关键词更完整

完整 Markdown 解释能保留机制、限制和关系，比直接生成短关键词更适合作为
语义展开的中间表示。

### H3：语料锚定能补齐盲点

先做少量种子检索，观察真实文档标题、章节路径和正文，再让 LLM 生成或修订
facet，可以补入纯 topic 规划没有发现的语义面。

### H4：高召回不能由单一向量路径承担

解释 chunk 的 dense 查询擅长整体语义召回；具体实现词由 keyword/FTS 召回。
两者应互补，而不是互相替代。

## 比较方案

### A. Topic-only planner

```text
topic
→ subtopics
→ meanings/facets
→ dense + keyword queries
```

已有结果见前置实验。该方案作为 query-side 基线。

### B. Explanation-only planner

```text
topic
→ 完整 Markdown 解释
→ 低层语义段/名词
→ dense 或 keyword queries
```

已有探针表明：

- 逐名词英文 FTS 在当前小库上召回很高，但候选几乎覆盖全库；
- 解释 chunk dense 查询候选较少，但目标块排名较差。

### C. Corpus-grounded planner

```text
topic
→ 少量通用种子检索
→ 观察命中文档、章节路径和完整 block 正文
→ LLM 生成 top-down facet hypotheses
→ 与 corpus-observed facets 合并
→ 去重、检查可检索性和证据义务
→ dense + keyword 混合召回
→ coverage matrix
→ 仅对未覆盖 facet 补查
```

该方案是本次实验的主要候选。

## 语料锚定的 facet 生成流程

1. 用原始 topic 做一次 hybrid 种子检索。
2. 对种子块按文档和章节聚合，并将命中的 block 全文提供给 planner。
3. 向 LLM 提供：
   - 原始 topic；
   - 种子命中的文档标题、章节路径和完整正文 block。
4. LLM 输出 coverage facets。每个 facet 包含：
   - 名称；
   - 要寻找的证据类型；
   - 来源类型：`corpus`、`hypothesis` 或 `both`；
   - 种子文档/章节引用；
   - semantic queries；
   - keyword queries。
5. 合并语义重复 facet，保留 corpus-grounded facet 的引用。
6. 限制查询总量，但不在提示词中写固定查询条数。
7. 分别执行 dense 和 keyword 查询，按 `block_id` 合并。
8. 统计每个 facet 的独立证据、文档多样性和缺口。

## 100k block 下的约束

- 不逐块调用 LLM 判断相关性；
- 不搜索并检索所有名词；
- 单轮查询数和候选池必须有预算；
- 种子检索发生在文档/章节级，不扫描整库正文；
- facet 缺口分析只针对有限 coverage 清单；
- 完整相关集合与最终 40 块回答上下文分开管理。

## 实验指标

### 召回

- 已知目标块进入候选池的数量和最佳排名；
- 候选 block 总数；
- 候选文档数；
- 每个 facet 的独立候选数；
- 不同 facet 带来的边际新增 block。

### 精度和噪声

- 模板、代码碎片和明显无关块占比；
- 只被通用词命中的候选占比；
- 同一文档、同一章节的重复候选数。

### 效率

- LLM 调用数；
- embedding 查询数和 token；
- FTS 查询数；
- 候选池规模；
- 从种子检索到最终集合的耗时。

### 稳定性

- 相同 topic 多次生成 facet 的交集和差异；
- facet 数量是否受 prompt 固定数量影响；
- 中英文知识库下的查询产出差异。

## 当前语料上的已知探针

当前快照有 15 篇文档、526 个 block。该数字只用于验证流程，不作为产品容量假设。

固定目标块：

- worktree：`doc_983ba4e2cbff4185873d525a69195fdf#v0001#c1#b00025`
- checkpoint resume：`doc_7f36f426b84f40eb9439acaad4949e32#v0001#c1#b00016`
- checkpoint evaluation：`doc_7f36f426b84f40eb9439acaad4949e32#v0001#c1#b00022`

已有结果：

- 逐名词英文 FTS：346 个唯一查询，候选 511/526，三个目标块均进入 Top-20；
- 解释 chunk dense：17 个查询，Top-20 候选 76-107，仅 worktree 曾进入 rank 19；
- 解释 chunk Top-60：候选 255，worktree 和 checkpoint resume 进入 Top-60，
  checkpoint evaluation 仍未召回。

## 预期输出

- 一份 corpus-grounded facet plan；
- 每个 facet 的查询和来源依据；
- dense/keyword 合并候选；
- 目标块召回结果；
- 与 topic-only、explanation-only 的候选规模对比；
- 是否能证明“不需要预先拆 subtopic”。

## 首轮结果

### 固定配置

- topic：`AI Agent隔离与控制边界`
- seed retrieval：原始 topic、`hybrid`、`candidate_depth=60`、`top_k=50`
- planner：`corpus-grounded-facet-planner-v1`
- planner prompt hash：
  `b633e90746678863f52faca9af79bcb9b2f69aa8c6a548ecdf20b0944b4d2ea1`
- planner LLM：`deepseek-flash`，temperature 0，thinking off
- per-query retrieval：`candidate_depth=60`、`top_k=20`
- execution：dense 和 keyword 分开执行
- 实验产物：
  `backend/.runtime/corpus-grounded-facets-ai-agent-isolation-control-boundary.json`

### 产出

| 指标 | 结果 |
| --- | ---: |
| seed 命中 block | 50 |
| seed 覆盖文档 | 13 |
| corpus-grounded facets | 20 |
| dense query | 80 |
| keyword query | 115 |
| 非空查询 | 157 / 195 |
| 唯一候选 block | 423 |
| 候选文档 | 15 |

facet 来源分布：

- `both`：14
- `corpus`：2
- `hypothesis`：4

具体 facet 包括：

1. Agent 隔离与控制边界的总体定位与核心命题
2. Agent 威胁模型与三类风险来源
3. 执行环境隔离：容器、VM 与系统调用沙箱
4. 网络出口控制与数据外泄防护
5. 三种隔离模式：临时容器、人在环沙箱与密封 VM
6. 人在环审批的局限与审批疲劳
7. 工具与能力隔离：最小权限、工具分级与能力令牌
8. 策略执行点与策略决策点及策略引擎
9. 输入侧指令与数据分离及污点标记
10. 输出侧校验、DLP 与动作预演
11. 身份、租户与委托链隔离
12. 多 Agent 隔离：通信、角色分离与信任域
13. 上下文与记忆隔离及持久记忆投毒
14. 审计、可观测与溯源
15. 能力与安全的权衡及过度隔离失败
16. 边界绕过、混淆代理与侧信道等失败方式
17. 沙箱环境中的测试、评估与红队
18. MCP 服务器与第三方连接器的信任边界
19. 密钥管理与短期凭证代理
20. 可逆性设计与高危动作事务化

20 个 facet 均有非空候选，单 facet 候选数为 51–130。

### 已知目标块召回

| 目标 | 查询 | 来源 facet | 模式 | rank |
| --- | --- | --- | --- | ---: |
| worktree | `dry-run` | 输出侧校验、DLP 与动作预演 | keyword | 1 |
| worktree | 多 Agent 系统中如何隔离 Agent 间的通信和共享状态 | 多 Agent 隔离：通信、角色分离与信任域 | dense | 5 |
| worktree | 跨会话持久上下文（CLAUDE.md、挂载工作区）带来的隔离风险 | 上下文与记忆隔离及持久记忆投毒 | dense | 6 |
| checkpoint resume | 如何通过可逆性设计降低 AI Agent 写操作的破坏 | 可逆性设计与高危动作事务化 | dense | 2 |
| checkpoint resume | 软删除、快照和事务如何保证 Agent 动作可回滚 | 可逆性设计与高危动作事务化 | dense | 2 |
| checkpoint evaluation | 多 Agent 系统的评估与传统评估有何不同挑战 | 沙箱环境中的测试、评估与红队 | dense | 2 |
| checkpoint evaluation | `guardrail evaluation` | 沙箱环境中的测试、评估与红队 | keyword | 5 |

`worktree` 的 keyword rank 1 是偶然词面命中：`dry-run` 被 keyword
analyzer 拆成 `dry`、`run` 等 token，而目标块正文包含
“run separate CLI sessions”，因此 `run` 命中了该块。这一条不能证明
“输出侧校验与动作预演” facet 在语义上覆盖了 worktree。

worktree 更可置信的召回来自以下 dense 查询：

- “多 Agent 系统中如何隔离 Agent 间的通信和共享状态”，rank 5；
- “跨会话持久上下文（CLAUDE.md、挂载工作区）带来的隔离风险”，rank 6；
- “claude.ai、Claude Code 和 Claude Cowork 分别采用什么隔离模式”，rank 7；
- “多租户 SaaS 场景下 Agent 的数据和记忆如何隔离”，rank 9。

checkpoint resume 同时由快照、软删除、事务回滚和两阶段提交相关 dense
查询召回；checkpoint evaluation 同时由评估、红队和
`guardrail evaluation` 等查询召回。

### 与已有方案对比

| 方案 | 查询数 | 候选 block | 已知目标召回 |
| --- | ---: | ---: | ---: |
| topic/subtopic/facet planner | 204 | 320 | 3/3 |
| explanation-only 逐名词 FTS | 346 | 511 | 3/3 |
| explanation chunk dense Top-20 | 17 | 76–107 | 0–1/3 |
| corpus-grounded planner | 195 | 423 | 3/3 |

corpus-grounded planner 的候选规模介于 topic/subtopic 与逐名词 FTS 之间，
但 195 条查询仍然偏多。候选 423 个覆盖 15 篇文档，说明高召回已实现，
但最终精确筛选尚未解决。

### 产品真实 plan 的分阶段诊断

固定 20-facet 实验证明的是实验 plan 的上限，不能直接解释产品实时 planner
的表现。为定位产品 c13-003 回放中三个目标块为何没有返回，使用该次真实
trace `802e8add918bb40965ab554d6634b3f2` 中的 18 个 facets 做固定 plan
重放，并分别记录查询深度命中、Top-20 候选、facet 内排名、短名单和 reranker。

重放的查询与候选阶段和原产品运行一致：

| 阶段 | 数量 |
| --- | ---: |
| facets | 18 |
| 去重查询 | 160 |
| 候选 block | 442 |
| facet shortlist | 102 |
| reranker 批次 | 13 |

重放 reranker 后支持块为 65 个，原产品运行为 67 个，说明 reranker 本身仍有
少量波动；但三个目标块在重放中仍全部未返回。

| 目标 | Top-20 候选 | 最好 facet 排名 | 进入短名单 | reranker 支持 | 首个缺失阶段 |
| --- | --- | ---: | --- | --- | --- |
| worktree | 是 | 15 / 103，“检索范围与记忆隔离” | 否 | 否 | facet shortlist |
| checkpoint resume | 是 | 38 / 98，“多 Agent 隐信道与合谋越权” | 否 | 否 | facet shortlist |
| checkpoint evaluation | 是 | 10 / 107，“读写分离与不可逆动作闸门” | 是 | 否 | reranker support |

具体结论：

1. 三个目标块都已被查询和 Top-20 候选召回，因此不是 embedding、FTS 或
   seed retrieval 完全失效。
2. `worktree` 在相关 facet 中最高只到第 15 名，未进入每 facet Top-10。
   动态 facet plan 含有“工作区”和“回滚”，但没有 `worktree`、独立检出或
   并行任务文件冲突这一证据义务。
3. `checkpoint resume` 虽被“可回滚事务”等查询以单查询 rank 2 命中，但在
   所属 facet 内综合排名最高仅为第 38，未进入 Top-10。动态 plan 没有把
   durable execution、resume 和 durable checkpoint 定义成独立 facet。
4. `checkpoint evaluation` 借“读写分离与不可逆动作闸门”的偶然词面进入
   Top-10，但 reranker 面对该错误 facet 没有判支持。固定 20-facet 实验
   中存在“沙箱环境中的测试、评估与红队”，并曾将该块判为 `partial`；
   动态 18-facet plan 没有这个 facet。

因此，本次产品回放的根因不是最终 40 块或 20k token 裁剪，也不能简单归因于
查询失败。最早失败点分别是 facet 内 Top-10 和 reranker support，但二者共同
来自动态 facet plan 没有生成“并行工作状态、checkpoint/恢复、状态变更评估”
这些具体证据义务。后续应先提高 facet plan 对这一语义面的覆盖率，再评估
每 facet Top-10 的粒度和 reranker 判断契约。

诊断产物：
`backend/.runtime/c13-003-corpus-grounded-stage-diagnosis.json`。

### 主题独立与完整 block seed 实验

上一轮定位表明，产品 planner 输入的 350 字符截断和用户问题目标混入都会
影响 facet。按“coverage tool 对主题负责，主 Agent 负责使用结果”的边界，
第二轮只改变两个变量：

1. corpus-grounded coverage 输入不再使用 `retrieval_goal`，只接收 `topic`；
2. seed evidence 从每个 block 前 350 字符改为完整 block 全文。

其他条件保持不变：seed Top-50、每 facet Top-10、reranker、返回全部有支持块。

当前快照中 50 个 seed block 的全文合计 60,091 字符、估算 18,666 tokens，
而原截断版为 17,228 字符、估算 6,971 tokens。全文只增加约 1.2 万 tokens，
没有触及当前 Harness 的上下文预算。

真实运行结果：

| 指标 | v1：目标混入 + 350 字 | v2：topic-only + 全文 |
| --- | ---: | ---: |
| facets | 18 | 24 |
| 查询 | 160 | 192 |
| 候选 block | 442 | 447 |
| facet shortlist | 102 | 103 |
| reranker 支持块 | 67 | 75 |
| 工具返回块 | 67 | 75 |
| result tokens | 32,920 | 35,104 |
| 工具耗时 | 59.5 秒 | 63.3 秒 |
| 工具输入 tokens | 59,607 | 73,767 |
| 工具输出 tokens | 17,237 | 20,897 |

v2 的 facet plan 明确新增了：

- “长时运行 Agent 的状态与检查点控制”；
- “多 Agent 信任升级与状态隔离”；
- “沙箱环境中的测试与评估”。

固定 v2 plan 的分阶段重放确认三个目标块全部贯穿成功：

| 目标 | Top-20 候选 | 短名单 | reranker 判断 | 返回 |
| --- | --- | --- | --- | --- |
| worktree | 是 | 是 | `partial`，多 Agent 状态隔离 | 是 |
| checkpoint resume | 是 | 是 | `direct`，长时运行状态与检查点 | 是 |
| checkpoint evaluation | 是 | 是 | `partial`，测试与评估 | 是 |

这说明全文 seed 确实改变了 facet planner，且目标缺失首先来自 facet 输入和
规划，而不是最终 40 块裁剪。

该方案已被沛东确认作为 `search_topic` 的正式唯一实现。旧 subtopic planner、
运行开关和 `retrieval_goal` 工具参数均已移除；工具契约升级为
`retrieval-tools-v9`。本轮保留旧 trace 和旧实现记录，仅用于实验对照。

正式默认路径又执行了一次 N=1 验证：

| 指标 | 结果 |
| --- | ---: |
| 工具参数 | 仅 `{"topic":"AI Agent 隔离与控制边界"}` |
| strategy | `corpus-grounded-coverage-v2` |
| facets | 14 |
| 查询 | 136 |
| 候选 block | 392 |
| facet shortlist | 67 |
| 返回 block | 53 |
| result tokens | 24,587 |
| 工具耗时 | 50.7 秒 |

该次运行中 `checkpoint resume` 被返回，但 `worktree` 和
`checkpoint evaluation` 未返回。与上一次 24 facets、75 blocks 的运行相比，
说明正式接入已经完成，但 planner 仍存在明显的跨运行波动。不能把单次
3/3 目标召回写成稳定能力。

v2 的产品答案仍然没有覆盖 R1 中的第⑤组并行任务边界和第⑥组上下文与记忆
生命周期边界。由于三个对应目标块已经全部返回，当前剩余问题不能再归因于
主题覆盖检索没有拿到证据，而属于主 Agent 如何使用已返回证据的问题。

运行：

- run：`run_cec276fadd384bf6aeac435aadf0d1bf`
- trace：`2994dfe9aded4987fe2a8de2ab03490d`
- judgment：`evalrev_89f235285b02401fae904232dca7a587`
- 结果文件：
  `backend/.runtime/c13-003-topic-only-full-seed-n1-result.json`
- 阶段诊断：
  `backend/.runtime/c13-003-topic-only-full-seed-stage-diagnosis.json`
- 正式默认路径结果：
  `backend/.runtime/c13-003-product-corpus-grounded-n1.json`

## 候选筛选首轮

### 固定产物

- 带原始分数的候选：
  `backend/.runtime/corpus-grounded-candidates-with-scores-ai-agent-isolation-control-boundary.json`
- 按 facet 归一化预筛后的候选：
  `backend/.runtime/corpus-grounded-shortlist-ai-agent-isolation-control-boundary.json`
- facet 审计：
  `backend/.runtime/corpus-grounded-canonical-facets-ai-agent-isolation-control-boundary.json`
- reranker：
  `backend/.runtime/corpus-grounded-rerank-ai-agent-isolation-control-boundary.json`
- 预算选择结果：
  `backend/.runtime/corpus-grounded-selection-ai-agent-isolation-control-boundary.json`

### 预筛

对每条查询内部分别归一化 dense distance 或 BM25 score，再按每个 facet
保留 Top-10 候选：

- 原始候选：423
- 预筛候选：107
- 候选-facet 对：200
- 三个已知目标块均保留在预筛集合

### facet 审计

LLM 审计后认为 20 个 facets 均具有独立证据义务，没有进行强制合并。

### reranker 结果

对预筛候选重新检查全部 20 个 facets，而不是只检查召回时挂载的 facet：

- 77 个候选至少支持一个 facet
- direct candidate-facet 对：49
- partial candidate-facet 对：81
- none 不输出

三个已知目标块都被判为 `partial`，未进入 `direct`：

- worktree：支持“多 Agent 隔离”，但未满足信任域和来源校验的完整范围
- checkpoint resume：支持“可逆性设计与高危动作事务化”，但描述的是
  重试和检查点恢复，不是事务化回滚
- checkpoint evaluation：支持“测试、评估与红队”，但描述的是状态变化
  评估，不涉及沙箱和红队

### 动态预算实验

| 预算 | 实际选中 | token | 文档数 | direct facet | partial-only facet | 目标块 |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 20 | 20 | 9,652 | 5 | 18 | 2 | 0/3 |
| 30 | 30 | 13,046 | 9 | 18 | 2 | 0/3 |
| 40 | 40 | 17,840 | 10 | 18 | 2 | 0/3 |
| 60 | 47（token 截断） | 19,875 | 14 | 18 | 2 | 0/3 |

结果说明单靠 facet 布尔覆盖会过早停止：20 块已经覆盖全部 20 个 facets，
但其中 18 个由 `direct` 总览块覆盖，剩余 2 个只达到 `partial`。
多 facet 的通用总览块会优先于只支持一个 facet、但给出具体机制的块。

因此当前筛选器不能作为最终方案。它证明了两点：

1. 粗粒度 facet coverage 不足以保证具体内容查全；
2. “覆盖全部 facet”与“覆盖每个 facet 下的具体机制”不是同一个指标。

下一轮需要把 `direct/partial` 判断进一步沉淀为 facet 下的子点
（subpoints），并按子点覆盖选块；不能仅因某个总览块已让 facet 变为
`direct` 就停止收集该 facet 下的其他具体实现。

### 首轮结论

1. facet 不必只由 query-side LLM 凭空拆解；引入真实文档标题、章节和正文
   片段后，planner 能形成更接近知识库表达方式的覆盖面。
2. 不需要机械地把 topic 先拆成多个 subtopic。由 `topic + explanation +
   seed evidence` 直接生成扁平 facets 已经能覆盖三个已知目标块。
3. 本实验不能证明 subtopic 永久无用。当前只验证了单 topic、单快照和
   一个 planner 版本，还需多主题稳定性验证。
4. 20 个 facets × 多查询会继续产生较大候选池。195 条查询、423 个候选
   对 100k block 产品形态仍偏高，需要后续加入查询预算、facet 合并和
   更严格的候选筛选。
5. 已知目标块全部召回只证明关键探针命中，不等于已经证明“所有真正相关
   的 block 都已找全”。仍需建立 block 级相关性 qrels 或抽样漏召评估。
6. facet 级 Boolean 覆盖仍不足；需要在 facet 下建立子点覆盖矩阵，
   并区分“总览块让 facet 变为 covered”和“具体机制块提供独特证据”。

## 业界参考

- BM25/稠密混合检索：BEIR, https://arxiv.org/abs/2104.08663
- HyDE：https://arxiv.org/abs/2212.10496
- RAG-Fusion：https://arxiv.org/abs/2402.03367
- RAPTOR 分层检索：https://arxiv.org/abs/2401.18059
- GraphRAG 全局检索：https://arxiv.org/abs/2404.16130
- Self-RAG：https://arxiv.org/abs/2310.11511
- Corrective RAG：https://arxiv.org/abs/2401.15884
- IRCoT：https://arxiv.org/abs/2212.10509
- Anthropic Contextual Retrieval：https://www.anthropic.com/news/contextual-retrieval
