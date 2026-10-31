# 2026-10-31 章节优先主题检索初步实验

- 关联切片：[066 章节优先的主题覆盖检索](../slices/066-section-first-topic-coverage.md)
- 关联设计：[11 语料锚定的主题覆盖检索](../../design/11-corpus-grounded-topic-coverage.md)
- Snapshot：`s_6b787258ba7d700c`
- Embedding：`qwen3.7-text-embedding`，1024 维
- 实验状态：初步 N=1，仅用于方案筛选，不作为稳定性结论
- 结构化结果：`backend/.runtime/section-first-probe-20261031.json`

## 问题

当前知识库中的人类章节标题能否成为独立的主题检索面，并在展开子树 block 后提高
主题证据覆盖？

现有 `embedding-input-v1` 已经把文档标题和完整 section path 放进每个 block 的
embedding 输入，keyword search text 也包含相同结构。因此本切片的增量不是简单地把
标题附到 block，而是把 section 本身提升为可独立检索、可展开的证据路由对象。

本次比较三个候选来源：

1. 原始 topic 直接检索章节；
2. 20 个已知 facet 名称分别检索章节，再按章节累计 RRF；
3. 现有 block hybrid 检索 Top-60。

支持判断和最终 reranker 在本次候选用例中未运行，因此这里只比较候选覆盖率，不比较
最终答案质量。

## 固定语料

当前活动快照包含：

- 15 篇文档；
- 526 个 block；
- 328 个 section-tree 节点。

章节分类：

| 类别 | 数量 |
| --- | ---: |
| 可路由 section | 286 |
| 文档根节点 | 7 |
| 页脚／作者等 boilerplate | 23 |
| 代码语言页签 | 4 |
| 无 subtree block 的排除节点 | 8 |
| generic section | 11 |

章节索引有效映射到 block 的比例满足预定的 95% 门槛。标题清洗后，当前语料没有空标题。

## 构建成本

- 可路由 section：286；
- 文档 embedding 批次数：15；
- 索引输入 token：18,322；
- 索引构建耗时：两次试跑为 7.9～10.8 秒；
- 后续单 topic 查询只需一次 query embedding 和 286 次本地余弦比较。

章节索引构建成本可接受。

## 单 topic 章节路由

### `AI Agent 隔离与控制边界`

Dense Top-10 主要命中：

1. `How we contain Claude across products > Patterns for containing agents`
2. `... > Pattern 3: The local VM > Risk: VM isolation kept endpoint detection out`
3. `... > Pattern 2: The human-in-the-loop sandbox`
4. `... > Three types of risk, three components of defense`

说明 topic 级 dense 路由能准确聚集到 containment 主文档，但 Top-10 被同一文档大量占据。
展开 Top-20 section 后得到 34 个 block，三个已知目标均未出现。

### `会话与上下文隔离`

`Automate and scale > Run multiple Claude sessions` 排名第 2。展开后命中目标
`worktree` block，说明章节路由确实可以找到 block 词面中没有直接表达、但章节主题
明确覆盖的证据。

### `checkpoint 恢复与状态持久化`

Top section 包含：

- `Pattern 2: File System as Persistent Memory`
- `Environment management > Incremental progress`
- `Environment management > Testing`

已知 checkpoint resume 目标位于 `Production reliability and engineering challenges`。
该标题没有出现“checkpoint／resume／恢复”等词，Top-20 section 展开后未命中目标。

### `状态变更评估与恢复正确性`

Top-20 section 展开后未命中 checkpoint evaluation 目标。该目标位于 `Appendix`，
章节标题本身无法为该证据提供可用检索信号。

## 多 facet 章节路由

使用已有 20-facet plan 的 facet 名称作为 20 条 section 查询：

- 每 query 保留 Top-10 sections；
- RRF 合计；
- 合并得到 80 个唯一 section；
- 展开上限为 250 blocks / 70k tokens；
- 实际展开 157 个 block；
- 三个目标只命中 `worktree`，覆盖为 1/3。

结果说明：

- 章节路由能从 `Run multiple Claude sessions` 找到 worktree；
- 但 section 标题不包含 checkpoint 词面时，无法仅凭标题定位该 block；
- `Appendix` 这类无语义容器标题会导致其子树成为结构检索盲区。

## 与现有 block hybrid 的补充关系

使用原始 topic 的 block hybrid Top-60：

- 找到 `checkpoint resume`；
- 未找到 `worktree`；
- 未找到 `checkpoint evaluation`。

与多 facet section 路由结果合并后，候选集合可达到 2/3：block hybrid 提供 checkpoint
resume，section routing 提供 worktree。checkpoint evaluation 仍缺失。

因此，本实验支持：

> section routing 是有价值的候选补充面，但不是 block 检索的替代品。

## Held-out 候选面

用已有 held-out facet 名称加 topic 组成章节查询：

| 主题 | facet 数 | 候选 section | 展开 block | 覆盖文档 | 与当前返回集重合 | 当前未返回 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Agent 工具使用与函数调用设计 | 18 | 69 | 158 | 13 | 77 | 81 |
| LLM 应用评测的数据集、判据与判官设计 | 21 | 95 | 172 | 10 | 101 | 71 |

这说明章节路由能形成跨文档候选面，并补充当前最终返回集合之外的内容。但这里没有新的人工
block qrels，`novel_vs_current` 只表示“不同”，不能当作“新增正确覆盖”。初步抽查还发现，
直接展开整个子树会带入一些同章节但只与 facet 边缘相关的 block；最终仍需 block 级支持
判断或二次排序。

## 关键负结果

1. **单 topic dense section routing 会向单篇大文档聚集。**
   Top-10 可以主要由同一文档的不同子章节占据，降低文档多样性。

2. **好标题只能帮助找到“标题表达出来的语义面”。**
   `Production reliability` 和 `Appendix` 下面的具体机制不会被标题索引发现。

3. **中文 topic 与英文标题的 lexical 路由不可用。**
   BM25/标题词面可作为英文 facet query 的补充，但不能承担跨语言主路径。

4. **章节路由的 block 预算不一定更小。**
   20 个 facet 路由后展开 152 个 block，仍可能显著大于当前候选集。

## 决策含义

- 不应在当前阶段用 section routing 替换 block hybrid。
- 应优先实现 `current block candidates ∪ section candidates`。
- 下一轮应测试“section candidate”是否能提高人工定义的 facet 覆盖率，而不是只看
  三个回归目标块。
- 后续需要把 section-level 子点和 block 内容纳入 section 表示，或仅将标题路由作为
  第一层导航，第二层仍回退到 block 检索。

## 复现入口

- 单元测试：

```powershell
D:\lida-data\vscode\ai-study-helper-v2\backend\.venv\Scripts\python.exe `
  -m pytest backend\tests\test_section_index.py -q
```

- 当前真实快照统计通过只读 `RetrievalReadService.bind_current()` 和
  `SectionIndex.from_reader()` 获取；不修改 collection 或数据库。
- 新增模块与现有 retrieval/read-layer 定向测试：19 passed；
- 完整后端测试集通过；
- 全后端 Ruff format/check 通过；
- Pyright：0 errors。

## 结论

人类章节结构值得使用，但正确的位置是**路由与补充召回**。当前证据不支持把章节标题
当作完整的主题证据索引；仅靠 section title + first line 仍会漏掉藏在泛化标题下的
具体机制。最终方案仍需要 block 内容检索和证据级覆盖判断共同参与。

## 待确认决策

- D1：第一版继续使用 snapshot artifact 的运行时只读索引，还是持久化 Section Index；
- D2：section 表示是否继续只用 `title + full path + first_line`，还是加入章节摘要或
  subtree term hints；
- D3：词面检索是否只用于英文 facet query，跨语言主路径固定为 dense；
- D4：section 路由补充候选的 Top-N 和展开预算；
- D5：generic section 是保留并降权，还是在最终产品路由中完全排除。

在上述决策确认前，不接入默认 `search_topic` 路径。
