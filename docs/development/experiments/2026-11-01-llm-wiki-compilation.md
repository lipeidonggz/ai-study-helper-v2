# 2026-11-01 · LLM Wiki 全库编译与 c13 固定快照验证

## 目的

验证 069–074 的最小产品闭环：

1. 从 15 篇、526 个 block 的冻结 snapshot 自动生成 topic ledger；
2. 发布绑定 snapshot 的 Wiki build；
3. 从产品搜索路径找回包含三个历史回归目标 block 的主题入口；
4. 生成可按段回源的 narrative page；
5. 观察主 Agent 在现有 100k 默认预算下能否实际使用 Wiki，而不是把“主题存在”误写成“回答已覆盖”。

这不是最终回答质量评测，也不把单次 c13 结果写成通用能力结论。

## 固定现场

| 项 | 值 |
| --- | --- |
| collection snapshot | `s_6b787258ba7d700c` |
| snapshot manifest hash | `e0a4cd8e1a959e2e5039ce6206c243338d555ddffcb5826fccc76731680683a7` |
| 文档／block | 15／526 |
| chat provider | `deepseek` / `deepseek-flash` |
| embedding provider | Qwen embedding |
| granularity | `standard` |
| block window | 每窗最多 16 blocks、12,000 chars |
| map concurrency | 3 |
| 固定问题 | `在当前知识库中，除了沙箱、虚拟机和出口控制，AI Agent 还有哪些隔离或控制边界？请分别说明各限制什么。` |

固定问题只存在于实验调用中，没有进入 Wiki 或主 Agent 的通用提示词。自动化测试同时检查 `c13-003`、`worktree`、checkpoint 探针名和目标 block ID 不出现在通用提示词中。

## 编译结果

### Topic map

第一次真实编译成功生成 topic map，随后在身份归一阶段被 DeepSeek 以 400 拒绝：

```text
Prompt must contain the word 'json' in some form
to use 'response_format' of type 'json_object'.
```

| 轮次 | topic map | 请求数 | 输入 tokens | 输出 tokens | candidates | 结果 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 1 | `tm_0dd9b8f74bfe58db` | 54 | 160,052 | 127,326 | 412 | identity 请求被拒绝；未发布 ledger/build |
| 2 | `tm_b1dbd1934e56239c` | 54 | 160,052 | 126,987 | 420 | 成功 |

根因是 `topic-identity-v1` 的系统提示只有 `merges` 示例，没有 `JSON` 契约词。修复为 `topic-identity-v2`，新增“只返回如下 JSON 对象”，并补回归测试。

### Topic ledger / build

| 项 | 值 |
| --- | --- |
| map ID | `tm_b1dbd1934e56239c` |
| ledger ID | `tl_d700e3c781d83e36` |
| build ID | `wb_52f87c018823821e` |
| candidate 数 | 420 |
| topic 数 | 365 |
| catalog-only | 194 |
| narrative 候选 | 171 |
| evidence refs | 709 |
| coverage slots | 1,092 |
| unresolved merges | 255 |

真实编译从启动到 build 发布约 4 分钟，主要耗时在 54 次 topic-map 调用、身份归一和全主题 embedding。

真实数据还暴露了一个增量幂等缺陷：同一主题重编译后，coverage slots 的插入顺序可能变化，导致“未变主题”被误判为受影响，重复生成页面。修复为按 label、question、slot ID 确定性排序，并以现有增量回归锁定。

## 三个历史回归目标

三个目标 block 都进入了 topic ledger 的直接证据：

| 探针 | 目标 block | 命中主题 |
| --- | --- | --- |
| worktree | `doc_983ba4e2cbff4185873d525a69195fdf#v0001#c1#b00025` | `wt_3c96db3e1d0e20fa` · 多会话并行运行 |
| checkpoint 恢复 | `doc_7f36f426b84f40eb9439acaad4949e32#v0001#c1#b00016` | `wt_4d19eb3944a905c1` · 生产可靠性工程挑战 |
| 恢复后的终态评估 | `doc_7f36f426b84f40eb9439acaad4949e32#v0001#c1#b00022` | `wt_3332da58e7593e11` · 多智能体系统的终态评估 |

这说明固定快照的“主题发现 + block 证据绑定”在 V1 上成立。它不说明主 Agent 会选中这些主题。

## 产品 Agent 回放

所有回放使用同一问题、同一 snapshot、同一真实 provider，并启用 Wiki 工具。

| 版本 | 上下文预算 | Agent 工具序列 | 输入／输出 tokens | 三个目标进入回答来源 | 结论 |
| --- | ---: | --- | ---: | --- | --- |
| `chat-v18` | 100k | `list_documents → get_outline → list_blocks` | 49,143／3,834 | 0/3 | 有兼容 build，但完全不调用 Wiki |
| `chat-v18` full trace | 100k | 同上 | 46,842／3,271 | 0/3 | 结构化 decision 表明只围绕主文档定位 |
| `chat-v19` 首版 | 100k | `search_wiki_topics` | 15,211／362 | 0/3 | 完整目录 168,528 tokens，被上下文预算拒绝 |
| `chat-v19` | 200k | `search_wiki_topics → get_topic_evidence` | 363,787／1,380 | 0/3 | 预算可容纳，但只选同篇 secure 文档的 8 个主题 |
| `chat-v19` compact catalog | 100k | `search_wiki_topics → get_topic_evidence` | 141,243／1,402 | 0/3 | 轻量目录 74,225 tokens，模型仍只看字面最相似的同一来源 |
| `chat-v19` source-aware | 100k | `search_wiki_topics → get_topic_evidence ×2` | 230,784／1,817 | 0/3 | 加入来源标题后仍围绕同一主文档 |
| `chat-v19` source-diverse | 100k | `search_wiki_topics → get_topic_evidence` | 154,839／1,485 | 0/3 | 目录前 15 条覆盖 15 个来源，但模型仍选了同一主文档的 9 个主题加 1 个相邻来源 |

### 提示词语义对齐

固定失败样本中，`chat-v18` 的结构化决定是：

```text
需要先了解知识库中有哪些文档，
才能定位涉及 AI Agent 隔离与控制边界的材料。
```

随后它选择一篇最相关文档并读取章节。语义根因不是工具缺失，而是原契约没有说明“当前知识库范围内的开放找全应先发现主题面”。`chat-v19` 把该规则写成通用职责，不含用例词，并在首轮成功触发 `search_wiki_topics`。

但连续回放显示，主 Agent 仍把问题解释为“围绕最相关文档再找几个边界”。按照提示词连续 3 轮不收敛后的规则，本轮停止继续堆措辞，改为在工具侧做两项结构性调整：

1. 搜索结果改为不截断的轻量主题目录，只返回标题、摘要、来源标题、证据数、覆盖标签与匹配分数；
2. 目录按来源轮转，避免单一来源占据前部，但不删除任何主题，也不改变响应中的 dense score。

source-diverse 排序仍没有让模型选中三个隐藏目标，说明“把正确候选放进上下文”与“模型形成完整覆盖决策”仍是两个不同问题。

## 搜索规模

| 形态 | 主题数 | Harness 计入 tokens | 三个目标是否在结果中 |
| --- | ---: | ---: | --- |
| 原始完整条目 | 365 | 168,518 | 是，rank 100／117／124 |
| 轻量目录 | 365 | 82,498 | 是，rank 87／107／184 |

轻量目录让默认 100k 预算可以接收完整主题目录；它没有解决 Agent 如何理解“找全”。

## Narrative page

直接对三个目标主题执行 materialize：

| topic | 结果 | page | 段数 | 页面直接引用目标 block |
| --- | --- | --- | ---: | --- |
| 多会话并行运行 | narrative | `wp_eb79be8ef95adf22` | 3 | worktree ✔ |
| 生产可靠性工程挑战 | narrative | `wp_eaf76a07ea6ecdfa` | 4 | checkpoint 恢复 ✔ |
| 多智能体系统的终态评估 | catalog-only | - | - | 按阈值不生成完整页 |

页面 JSON、段落与 source block 校验通过。终态评估主题只有 1 个直接 block，按 `072-D1` 留在 catalog-only，没有把元数据页伪装成完整叙述页。

## 结论

1. V1 全库 topic map、身份账本、独立 Wiki build 和原子发布已经运行成功。
2. 三个历史目标 block 都能被 ledger 发现并绑定到主题。
3. 搜索与 evidence 工具可返回完整、snapshot 内有效的证据。
4. narrative page 的段落级回源成立；catalog-only 阈值没有被绕过。
5. 主 Agent 的实际使用仍未闭环。当前失败不再是召回或预算，而是开放找全问题下缺少“如何从全量主题面形成完整覆盖决定”的职责分解。
6. 继续增加同构提示词没有依据。下一步若要产品化，应单独设计覆盖规划／主题分组职责，且必须保持主题目录“不截断”和原始 block 作为事实源。

## 集中反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结论 |
| --- | --- | --- | --- |
| R1 | 为什么最终事实和 citation 必须指向当前 snapshot 的原始 block，而不能直接把 Wiki topic ID 或叙述页正文当作 citation 来源？ | Wiki 经过 LLM 二次加工，不是可信来源；引用的目的是提供信任，只有原文能承担这件事。 | 通过。回答正确区分了导航产物与事实源，并识别 citation 的信任职责。 |
| R2 | 用户要求“找全”时，为什么不能用 Top-20 或二次加工目录证明语义覆盖；预算不足时为什么不能静默只返回前几条？ | 二次加工结果无法证明自己覆盖了全部语义面；只返回前 20 条时，也无法确定 20 条之后没有相关主题。 | 通过。回答指出截断在语义上没有闭合依据；另要求把 dense 入选阈值登记为后续可配置的用户权衡项。 |
| R3 | 新 snapshot 已发布但 Wiki 未完成，或 active Wiki 属于旧 snapshot 时，为什么不能沿用旧 Wiki？用户应看到什么？ | 这会产生不一致，违背来源可信原则；用户应看到“Wiki 尚未完成处理，部分工具暂时不可用”这类提示。 | 通过。回答后补齐用户可见状态：Agent 发出 `wiki_status`，前端在 `not_ready_for_snapshot` 时显示提示，同时保留原文检索。 |
| R4 | 如何持续证明通用提示词没有混入单个用例、目标 block 或测试答案？ | 静态扫描，以及 held-out 用例验证。 | 通过。两类证据分别覆盖已知敏感信息持续门禁和泛化能力回归；当前另有 prompt version／template hash 作为运行可追溯性。 |
| R5 | 全量主题目录为什么按来源轮转；来源更分散是否等于更相关或更完整？ | 轮转用于防止单个文档占据全部高注意力位置，降低单文档对答案的负面影响；代价是牺牲相似度排序精度，弱相关来源可能提前挤占注意力。来源分散仍不能证明找全，因为 Wiki 是二次加工产物，天然有信息损失。 | 通过。回答同时识别注意力分配目标、排序精度代价和二次加工无法证明语义闭合的边界。 |

## 复现

```powershell
backend\.venv\Scripts\python.exe scripts\wiki_compile.py `
  --data-root backend\.runtime\wiki-c13-068 `
  --snapshot-id s_6b787258ba7d700c `
  --granularity standard `
  --skip-narrative `
  --map-parallel 3

backend\.venv\Scripts\python.exe scripts\wiki_query.py `
  --data-root backend\.runtime\wiki-c13-068 `
  --snapshot-id s_6b787258ba7d700c `
  --query "AI Agent 隔离 控制边界 权限 沙箱 虚拟机 出口控制"

backend\.venv\Scripts\python.exe scripts\wiki_materialize.py `
  --data-root backend\.runtime\wiki-c13-068 `
  --snapshot-id s_6b787258ba7d700c `
  --topic-id wt_3c96db3e1d0e20fa `
  --topic-id wt_4d19eb3944a905c1 `
  --topic-id wt_3332da58e7593e11

backend\.venv\Scripts\python.exe scripts\wiki_agent_probe.py `
  --data-root backend\.runtime\wiki-c13-068 `
  --snapshot-id s_6b787258ba7d700c `
  --question "在当前知识库中，除了沙箱、虚拟机和出口控制，AI Agent 还有哪些隔离或控制边界？请分别说明各限制什么。" `
  --trace-mode full
```

运行时大文件只保存在被忽略的 `backend/.runtime/`，不进入 Git。
