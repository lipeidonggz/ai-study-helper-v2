# 11 LLM Wiki 编译设计

- 状态：069–074 初版实现基线已确认（2026-11-01）；`document_summary`、`index` 和后台编译任务暂缓
- 日期：2026-10-31
- 上游设计：[02 Agent 开发方法](02-how-to-build-an-agent.md)、[05 存储与检索策略](05-storage-and-retrieval-strategy.md)、[06 检索、工具与 Agent](06-retrieval-pure-agent.md)、[07 HTML 入库与快照](07-html-ingestion-design.md)、[09 技术选型](09-technology-selection.md)
- 外部参考：
  - [WeKnora Wiki 编译拆解（v0.8.2 源码与实测）](https://juejin.cn/post/7694866345338765350)
  - [WeKnora 官方 Wiki 文档（v0.9.0，2026-10-31 核对）](https://weknora.weixin.qq.com/docs/03-features/14-wiki)
  - [Tencent/WeKnora v0.8.2 源码](https://github.com/Tencent/WeKnora/tree/v0.8.2)
  - [Tencent/WeKnora v0.9.0](https://github.com/Tencent/WeKnora/tree/v0.9.0)

## 0. 结论

我们的 LLM Wiki 不应是“把一批候选 block 交给模型写一页”，也不应是把整库交给一次 LLM 调用。

目标架构是一个**绑定冻结 collection snapshot 的离线知识地图编译系统**：

```text
全库 block
→ 发现“值得建主题入口”的候选
→ 归一身份与别名
→ 为候选绑定真实证据 block
→ 规划主题入口、覆盖面和页间导航
→ 生成可选的叙述页
→ 建立 Wiki topic 索引与可选页面投影
→ 原子发布一个可查询的 Wiki build
```

其中有两个独立层次：

1. **主题台账（topic ledger）**：所有被识别、且有真实 block 支撑的主题入口、别名、摘要和证据引用。它负责召回与覆盖面。
2. **叙述页（narrative page）**：为达到材料化阈值的主题生成更完整的可读正文。它负责阅读体验和跨源组织。

这两层不能混为一层。若每个候选都必须生成一篇完整正文，成本会按候选数爆炸；若只生成高层摘要，又会重新丢掉开放主题所需的细粒度入口。

Wiki 始终是索引层。页面文字不是事实源，最终引用必须回到当前 snapshot 的 block。

## 1. 问题与目标

### 1.1 要解决的问题

经典 dense／BM25 检索和现场 Agentic 查询都在回答“哪些 block 与当前查询表述接近”。它们难以稳定回答：

- 一个开放主题还有哪些没有被当前查询措辞覆盖的合法方面；
- 哪些名词、机制、流程、条件、限制或失败模式应被视为同一主题下的独立入口；
- 不同文档中措辞不同、但在知识库中同指一物的主题应该如何合并；
- 用户要“找全”一个主题时，哪些证据面值得继续检查。

### 1.2 用户可观察目标

用户在知识库中查询一个主题时，系统能够：

1. 返回与该主题相关的预编译主题入口，而不只依赖查询措辞的相似度；
2. 对每个入口提供别名、摘要、来源文档和真实 block 引用；
3. 通过入口或页面链接继续扩展到相邻主题；
4. 最终交给主 Agent 的是完整、可回源的证据集合；
5. 在任何时候都能解释该结果绑定的是哪个 collection snapshot 与 Wiki build。

### 1.3 产品约束

- 首版规模：约 1,000 篇文档、10 万 block；
- 单用户、单知识库、本地运行；
- 主数据仍为 SQLite＋artifact＋manifest，LanceDB 是可重建派生索引；
- 不执行旧仓库 Wiki 代码，不把 WeKnora 的数据模型或提示词直接复制进产品；
- 原始 block 是唯一事实源；
- 不允许静默截断、静默跳过页面、静默回退到旧快照。

### 1.4 非目标

- 不生成带类型谓词的严格知识图谱；
- 不承诺 Wiki 页面集合等于主题全集；
- 不用页面正文替代最终引用；
- 不在首版做多用户协作、RBAC、网页端编辑器或公网发布；
- 不在查询时临时发现整个知识库的主题目录。

## 2. 对 WeKnora 的取舍

### 2.1 版本口径

文章拆解的是 WeKnora `v0.8.2`，commit `3e8b0bf`。当前官方文档对应 `v0.9.0`。

官方文档把生成流程概括为四阶段 Map-Reduce：

1. Pass 0：候选实体／概念抽取；
2. Pass 1..N：逐 block 为候选标注支持引用；
3. Reduce：按主题合并、增量更新或新建页面；
4. Finalize：索引、死链、交叉链接和目录清理，不调用 LLM。

文章中的七阶段描述与这一主干一致，只是把身份去重、summary 页和目录规划拆得更细。本文采用“概念上的七阶段、实现上的 Map-Reduce 任务”这一口径。

### 2.2 值得吸收

| WeKnora 机制 | 价值 | 我们的采用方式 |
| --- | --- | --- |
| 候选抽取与页面写作分离 | 先决定有哪些主题入口，再决定哪些值得写正文 | 采用，并进一步拆成 topic ledger 与 narrative page |
| 逐 block 引用匹配 | 页面证据不是文章级模糊来源，而是可回查的 block | 采用，引用必须属于当前 snapshot |
| 稳定 slug／身份账本 | 文档更新后能把新内容归回原主题 | 采用类似 page identity ledger，但 UUID 与可变 slug 分离 |
| `related != same` 的去重原则 | 防止把相邻概念错误合并 | 采用；合并必须同时满足名称证据与语义同一性 |
| 页面增量更新与删除撤回 | 不让新文档无限堆叠、删文档后页面仍保留旧事实 | 采用，并通过新 Wiki build 整体发布 |
| 页面版本历史 | 用户可回滚，自动生成与人工修改可区分 | 采用 |
| 来源文档与 block 双引用 | 支持定位文档与精确证据 | 采用，并强化为段落级 provenance |
| Finalize 用确定性算法 | 索引、死链和映射不需要再次调用 LLM | 采用 |

### 2.3 明确不能照搬

| WeKnora 限制 | 对目标的影响 | 我们的修正 |
| --- | --- | --- |
| 旧版页面未进入主向量／关键词索引，agent 只能靠正则找页 | Wiki 无法作为稳定的检索通道 | 页面标题、别名、摘要和正文进入独立 Wiki 索引 |
| 旧版别名不完全参与 agent 搜索 | 同一主题的异名召回不稳定 | 别名是页面索引的一等字段，并记录来源与版本 |
| 正文只在页级记录 `chunk_refs`，没有段级锚点 | 删除或矛盾处理时难定位“哪句话来自哪块” | 每个段落／表格行必须携带 block IDs |
| 候选抽取后有截断上限 | 超长文档尾部可能不产生主题入口 | 以 snapshot block 为工作单位，禁止整篇静默截断 |
| 每个 slug 都进入 Reduce，写页占主要成本 | 10 万 block 下主题页成本可能失控 | topic ledger 全量保留；只有达标主题 materialize 叙述页 |
| 目录随批次增量规划，跨批可能混轴 | 目录会漂移，难以作为稳定枚举面 | 目录只做展示；检索依赖稳定主题身份、别名和链接，不依赖目录 |
| 页面链接只靠标题注入，旧链接不会自动全量回填 | 图会稀疏、长尾召回弱 | 链接由确定性身份账本＋页面规划产出，发布前全量 lint |
| 编译不幂等，删除再重投不能精确复原 | 无法承诺二进制可重现 | 承诺来源与结构可恢复，不承诺 LLM 文本逐字复原 |

### 2.4 抽取分类取舍

“LLM Wiki”不是一个已经标准化到统一知识模型的概念。公开实现至少存在三种做法：

| 实现 | 核心对象 | 是否硬分实体／概念 |
| --- | --- | --- |
| [WeKnora](https://weknora.weixin.qq.com/docs/03-features/14-wiki) v0.8.2/v0.9.0 | entity、concept、summary、index 页面 | 是；slug 与页面类型都带分类，并额外处理同名跨类型复用 |
| [Microsoft GraphRAG](https://microsoft.github.io/graphrag/index/default_dataflow/) | Entity、Relationship、Claim/Covariate、Community Report | 否；统一使用宽泛 Entity，通过 `type` 描述人物、地点、事件或领域自定义类型 |
| [LightRAG](https://github.com/HKUDS/LightRAG) | Entity、Relationship、Text Chunk | 否；抽象概念是检索目标，不是独立持久化页面类型 |
| [DeepWiki-Open](https://github.com/AsyncFuncAI/deepwiki-open) | 代码结构、文档主题、Wiki 页面 | 否；核心是页面和文档组织，没有全局实体／概念分类层 |

明确分类的价值：

- 实体与概念可以采用不同的身份比较提示和页面模板；
- UI 可以按类型筛选；
- 实体页更关注别名、版本和对象属性，概念页更关注定义、机制和适用边界。

明确分类的代价：

- 边界高度模糊，`worktree`、MCP、检查点恢复等对象可被不同模型判成不同类别；
- 类型如果进入身份主键，分类漂移会制造孪生页面；
- 分类判断会占用抽取注意力，但不会直接增加 coverage；
- 预置本体不适合“大量通用学习资料”的产品范围；
- 引入第三种 `topic` 类别后，抽取对象和对外检索资源被混在同一层级。

**本项目决定：不在抽取阶段硬分 entity／concept／topic。**

采用统一 `WikiCandidate`：

- 名称、别名、摘要和证据是必需字段；
- `semantic_tags` 是可选、非主键的提示字段，例如 `entity`、`concept`、`method`、`process`、`tool`；
- 标签不参与 slug、topic identity 或去重主键；
- 对外统一提供 `WikiTopic`；
- 只有后续实测证明某类标签能显著改善身份归一或页面生成时，才升级为更硬的内部规则。

## 3. 核心设计决策

### D1：主题台账与叙述页分层

每个通过最低证据门槛的候选主题都进入 topic ledger，但不必都生成完整叙述页。

主题台账记录：

- 稳定 `topic_id`；
- 当前标题、别名和归一名称；
- 一句话摘要；
- 可选的 `semantic_tags`；
- 来源 block IDs 与文档 IDs；
- 语料内观察到的覆盖槽。

叙述页记录：

- 结构化章节；
- 每个事实段落的 block 引用；
- 允许的相邻主题导航；初版不承诺完整父／子目录或关系图；
- 与 topic ledger 相同的主体身份。

这样，“找全”首先依赖全量 topic ledger 的召回；高成本页面正文只服务阅读和复杂综合。

### D2：页面是结构化文档，不是自由 Markdown 字符串

模型输出：

```json
{
  "summary": "一句话摘要",
  "sections": [
    {
      "heading": "章节标题",
      "paragraphs": [
        {
          "text": "贴近证据原文的段落",
          "source_block_ids": ["block-id"]
        }
      ]
    }
  ],
  "related_topic_ids": ["topic-id"]
}
```

程序校验后保存结构化 JSON artifact；展示层不得把 JSON 当成事实源。禁止让模型只在正文里内联写不可校验的 block ID。

### D3：身份、slug 和展示标题分离

- `topic_id`：稳定 UUID，跨文档版本保持；
- `canonical_name`：用于身份比较的当前规范名；
- `slug`：可读、可变，不作为事实主键；
- `aliases`：多语言名、缩写、旧名和合并前名称；
- `semantic_tags`：可选的展示／诊断标签，不参与身份键；
- `identity_version`：每次合并、拆分或重命名均递增。

去重流程先由确定性名称／别名召回候选，再由 LLM 判断是否同一身份。仅共享一个词、同属一个类别、类型标签相同或“经常一起出现”不构成合并依据。类型标签不同也不能单独阻止合并。

### D4：Wiki build 绑定 collection snapshot

每个 Wiki build 必须引用：

- `snapshot_id`；
- `snapshot_manifest_hash`；
- `base_wiki_build_id`；
- 编译器、各阶段提示词、模型和索引 schema 版本；
- topic ledger、counts 和 pipeline versions 的确定性 hash。

叙述页是 topic build 发布后的可选投影，不在 build 首次发布时阻塞 active pointer。每个页面 artifact 单独记录 page ID、引用和 hash；读取时必须重新校验。页面物化失败不会把已发布的 topic index 伪装成完整页面，也不会自动混用其他 snapshot。

不允许一个 Wiki build 混用多个 snapshot 的 block。

### D5：Wiki 索引与 block 索引分开

不能把 Wiki 页写入 block 表并伪装成原文。

分别维护：

- block dense＋FTS：返回事实源；
- 初版 Wiki topic FTS＋topic dense：返回主题入口；标题、别名、摘要进入两路索引；
- 叙述页物化后，页面正文追加到同一 topic FTS 的 `body`，但不改变 topic dense 向量；
- Wiki 页面候选链接：只用于导航，不是事实关系图；
- source refs：从页面回到底层 block。

### D6：目录不是完整性判据

目录和全量搜索结果是浏览／候选导航辅助，不是主题全集。允许：

- 最多三层展示路径；
- 手工移动并锁定页面；
- 空目录；
- 稳定路径账本和跨 build 对比。

禁止把“某目录已经列完”或“所有候选都已返回”当成主题覆盖已完成的证据。

### D7：链接不是事实关系

页面链接只表示可导航关系，不自动形成事实断言。

当前初版只落地下述可解释的子集：

1. 页面生成时，把与当前主题共享直接证据 block 的主题作为允许的相邻候选；
2. 模型只能从该候选集合中选择 `related_topic_ids`；
3. 读取时把它们作为导航链接，不形成因果、依赖、版本或比较声明；
4. 父／子目录、全量共现图和手工链接暂不实现。

后续若落地共现边，只能命名为 `co_occurs_in_block`，不能升级为因果、依赖或比较关系。

## 4. 总体架构

```text
SQLite + snapshot artifacts
    │
    ├─ Document / section / block 事实层
    │
    └─ Wiki Compiler（后台，按 snapshot）
          ├─ Map：文档窗口 → 候选主题与覆盖槽
          ├─ Align：候选 → 稳定身份
          ├─ Cite：主题 → 支持 block
          ├─ Plan：主题 → 目录／链接／页面计划
          ├─ Materialize：选定主题 → 叙述页
          └─ Finalize：topic 索引、可选页面物化、manifest
                    │
                    ▼
              Immutable Wiki build
                    │
                    ├─ 当前 Active Wiki pointer
                    ├─ 历史 build／页面版本
                    └─ 页面 FTS／dense 索引
```

编译是入库后的派生任务，不阻塞正式 collection 发布；但 Wiki build 未完成时，Wiki 工具不能假装已有完整覆盖。

## 5. 编译流程

### S0：冻结输入与清点

输入：一个已发布的 `CandidateSnapshotManifest`。

输出：

- 文档清单；
- section tree；
- 全部 block 及 content hash；
- 每篇文档的稳定 `doc_id + doc_version`；
- 本次待处理文档集合和被替换／删除的旧文档集合。

硬约束：

- 后续所有阶段只读该 snapshot；
- 任一 block 不在 snapshot 中即失败；
- 不允许因文档过长而只读取前 N 个字符。

长文档用 section window 或 block batch 覆盖全部内容，窗口之间有 chapter path 作为上下文。

### S1：Document Map——抽取候选主题与覆盖槽

按文档和 block window 调用 LLM，输入包含：

- `doc_id / doc_version / title`；
- `section_path / first_line / ordinal`；
- 当前窗口的完整 block 列表；
- 该文档上一版产生的 candidate IDs（增量时）。

输出：

```json
{
  "candidates": [
    {
      "local_key": "...",
      "name": "...",
      "aliases": [],
      "summary": "...",
      "semantic_tags": [],
      "coverage_slots": [
        {
          "label": "...",
          "question": "...",
          "source_block_ids": ["..."]
        }
      ],
      "source_block_ids": ["..."]
    }
  ]
}
```

提取范围不是固定领域词表，也不依赖章节标题一定有效。每个候选必须至少绑定一个当前窗口 block；没有 block 的候选只进入诊断，不进入产品索引。

候选是统一的“可能需要一个 Wiki 入口的语义对象”，可以对应产品、组织、人物、项目、工具、机制、方法、流程、风险、操作边界、评估方式或更综合的主题。

`semantic_tags` 只帮助模型表达，不形成互斥分类，也不允许出现 `topic` 这个标签；topic 是后续规划与服务层生成的统一资源身份，不是抽取类别。

“覆盖槽”只描述语料中观察到的检查面，不生成答案，也不能被当成完整成员清单。

这一阶段输出的是文档局部覆盖槽。身份对齐后，S4 会把同一主题跨文档的局部槽合并成 build 级覆盖槽。

### S2：Identity Resolve——全局身份对齐

候选先经过文档内合并，再做过期身份处理，最后进入全局身份对齐。

确定性步骤：

1. Unicode NFC、大小写和空白归一；
2. 精确名称／别名命中；
3. FTS 或 embedding 召回少量候选；
4. 旧 candidate key 命中上一版身份；
5. 人工锁定的标题和 slug 优先。

LLM 判同输入只包含：

- 新候选；
- 少量召回候选页；
- 双方名称、别名、摘要和少量证据块。

LLM 只能返回已有候选 ID 或 `new`，不得发明身份。

合并规则：

- `related != same`；
- 不同版本、不同产品、父概念与子概念、同域相邻概念不合并；
- 共享缩写不足以判同；
- 同一对象曾被标成不同 `semantic_tags`，不构成拆分理由；
- 低置信度保留独立身份并记录候选合并问题，不冒险误合并。

### S3：Evidence Bind——为候选绑定 block

WeKnora 的关键启发是候选和证据分成两步。

对每篇文档，把当前有效候选清单与文档 block 分批送入引用模型：

```json
{
  "citations": {
    "topic-id": ["block-id-1", "block-id-2"]
  },
  "new_candidates": []
}
```

要求：

- 只允许 snapshot 内的 block ID；
- 只有包含具体事实、属性、步骤、条件、数字或关系的 block 才能成为直接证据；
- 同一 block 可以支持多个主题；
- 引用模型发现的高价值遗漏候选可以补入，但仍要经过身份对齐；
- 无引用候选不能进入产品 topic ledger。

此阶段独立于页面正文，是覆盖召回的底座。

### S4：Coverage Surface——归并主题覆盖槽

初版不设置独立的覆盖槽生成器。覆盖槽由 S1 在证据窗口内随候选一起观察，并在 S2 身份归一后按 label／question 合并：

```json
{
  "slot_id": "...",
  "label": "检查点与恢复",
  "question": "该主题下检查点、恢复与终态验证分别限制什么？",
  "status": "observed",
  "source_block_ids": ["..."]
}
```

规则：

- 覆盖槽必须来自 topic map 中已校验的当前 window block；
- “observed” 表示语料明确讨论；
- “hypothesis” 只能进入诊断，不进入面向 Agent 的覆盖结果；
- 初版按标准化 label＋question 合并同一主题的不同文档槽；
- 对每个 topic 的 coverage slots 按 label、question、slot ID 确定性排序，保证增量 fingerprint 稳定；
- 覆盖槽不是最终答案，不持久化模型对用户问题的临时检索计划。

这一步替代过去在查询时生成 subtopic／含义／query 的做法：把“可能有哪些语义面”变成可版本化、可审查的编译产物。

### S5：Materialization Policy——初版页面计划

初版不实现独立全局 Planner，也不生成展示目录、父主题或手工锁定关系。topic build 直接按确定性阈值决定 `catalog` 或 `narrative`：

materialization 阈值：

| 条件 | 结果 |
| --- | --- |
| 至少 1 个直接证据 block | 进入 topic ledger |
| 至少 2 个直接证据 block，或跨 2 篇文档 | 可生成叙述页 |
| 只有一次顺带提及 | 默认 catalog_only |
| 与已有主题同义 | 合并到已有主题 |
| 无直接 block | 不发布 |

阈值是产品策略，不写死在抽取提示词里。

未来的独立 Planner、`document_summary`、`index`、父子目录和手工锁定作为后续增强，不参与初版完成判定。

### S6：Narrative Materialize——按主题写页

页面输入包含：

- 当前主题身份；
- 该主题已绑定的 verbatim blocks；
- 覆盖槽；
- 与当前主题共享直接证据的允许相邻主题；
- 增量更新通过复制未受影响页面或重新生成受影响页面完成。

生成要求：

- 只使用输入证据；
- 贴近原文，不做创作式扩写；
- 每段至少一个 block 引用；
- 不把块级句柄写进最终正文；
- 不发明页面链接；
- 初版不单独生成固定 `Contradictions / Updates` 章节；模型只能在给定证据内组织内容，不能自行补造冲突判断；
- 输出被截断时整页失败，不写半页。

对于 `catalog_only` 主题，不调用页面正文模型。页面根据台账生成：

```text
标题
一句话摘要
别名
来源列表
直接证据 block 列表
```

这类页面明确标记 `content_level=catalog`，不能伪装成完整叙述页。

### S7：Finalize——分层发布与页面物化

当前实现分为两层：

1. topic map 与 topic ledger 写入不可变 artifact；
2. 构建 topic metadata、FTS、dense vector 和 active pointer；
3. 原子切换与请求 snapshot 匹配的 active topic build；
4. 若请求 materialize，则为满足阈值的 topics 生成结构化页面 JSON artifact；
5. 校验每段 block 引用属于当前 topic evidence 和当前 snapshot；
6. 校验页面身份、hash、允许的相邻 topic ID；
7. 写入页面、段落和候选导航链接投影；
8. 将页面正文追加到 topic FTS 的 `body`；topic dense 仍由标题、别名和摘要构成；
9. 增量时，未受影响页面复制页面投影，受影响页按当前 snapshot 重建。

topic build 发布失败时 active pointer 不变。topic build 已成功后的可选页面物化失败会显式返回错误；页面读取分别以 `wiki_page_not_materialized` 或 artifact 校验失败表达，不能伪装成完整页面。正式产品若要求“页面全部成功才允许 topic build 可见”，需要在后续后台任务切片中把两层合并为可恢复事务，不能静默假设。

## 6. 覆盖面模型

### 6.1 Topic Entry

```json
{
  "topic_id": "topic-uuid",
  "build_id": "wiki-build-id",
  "title": "检查点与恢复",
  "canonical_name": "checkpoint and recovery",
  "semantic_tags": ["concept", "process"],
  "aliases": ["checkpoint", "断点续跑"],
  "summary": "说明长时程 Agent 如何从中断处恢复并验证终态。",
  "source_doc_ids": ["doc_..."],
  "evidence_block_ids": ["doc_...#v0001#c1#b00016"],
  "coverage_slots": ["slot-uuid-1"],
  "content_level": "narrative"
}
```

### 6.2 Coverage Slot

```json
{
  "slot_id": "slot-uuid",
  "topic_id": "topic-uuid",
  "label": "恢复正确性",
  "question": "恢复后的哪个终态必须正确，如何验证？",
  "source_block_ids": ["doc_...#v0001#c1#b00022"],
  "status": "observed"
}
```

覆盖槽的价值是让查询阶段知道“该主题有哪些检查面值得核对”，不是替用户回答，也不是宣告主题已经完整。

### 6.3 页面与完整性

页面不承担“成员清单载体”。目录、页面链接和覆盖槽都只是导航投影。

完整性只能在具体主题和明确判据下评估：

- 金标准列出必须出现的主题／方面／block；
- 检查 Wiki topic ledger 是否召回这些主题；
- 检查每个主题的直接 block 引用是否覆盖 qrels；
- 检查最终页面或返回证据是否漏掉已知点；
- 对未知遗漏仍需通过新增用例和失败样本回填。

### 6.4 页面类型

| 类型 | 自动生成 | 作用 | 是否承担覆盖完整性 |
| --- | --- | --- | --- |
| `document_summary` | 初版不生成；后续候补 | 让主 Agent 理解单篇文档的范围和主题走向 | 否 |
| `topic` | 初版只生成达到阈值的 topic 叙述页 | 所有跨源知识入口的统一页面类型；实体、概念、方法、流程等都由此承载 | 否；入口和证据由 topic ledger 维护 |
| `index` | 初版不生成；后续候补 | Wiki 入口、目录概览和 build 说明 | 否 |
| `synthesis` | 首版不自动生成 | 主 Agent 按具体问题创建跨源综合分析 | 否 |
| `comparison` | 首版不自动生成 | 主 Agent 按具体问题创建对比 | 否 |

`semantic_tags` 可以决定页面内部的默认章节提示，但不能改变页面类型。`document_summary` 和 `index` 是后续增强，不属于 069–074 初版；若实现，它们仍必须带来源 block refs，不能替代 topic ledger。

## 7. 存储与数据模型

首版采用 SQLite 保存可查询元数据和当前内容，artifact 保存完整页面渲染和诊断输入。

### 7.1 Build

```text
wiki_builds
  build_id
  snapshot_id
  snapshot_manifest_hash
  base_build_id
  status                 ready（初版只发布完整 ready build）
  compiler_version
  pipeline_versions_json
  counts_json
  manifest_hash
  created_at / published_at
```

### 7.2 Topic 与别名

```text
wiki_topics
  topic_id
  build_id
  canonical_name
  normalized_name
  slug
  title
  semantic_tags_json     // 可选标签，不参与唯一键
  summary
  content_level

wiki_topic_aliases
  build_id
  topic_id
  alias
  normalized_alias
```

### 7.3 证据与覆盖槽

```text
wiki_topic_evidence
  build_id
  topic_id
  block_id
  doc_id
  doc_version
  content_hash
  relation                direct

wiki_coverage_slots
  slot_id
  build_id
  topic_id
  label
  question
  status                  observed | hypothesis | rejected

wiki_slot_evidence
  build_id
  slot_id
  block_id
```

### 7.4 页面与链接

```text
wiki_pages
  page_id
  build_id
  topic_id
  content_level
  summary
  artifact_ref
  artifact_hash
  version

wiki_page_paragraphs
  build_id
  page_id
  ordinal
  text
  source_block_ids_json

wiki_page_links
  build_id
  from_topic_id
  to_topic_id
  kind                    explicit（初版只保存允许的候选导航）
  source_block_ids_json   初版为空数组；链接不是事实关系，不携带引用
```

### 7.5 活动指针与历史

```text
active_wiki_pointer
  build_id
  snapshot_id
  generation
  previous_build_id
  published_at
```

页面 artifact 不可变并单独记录 hash，但初版尚未提供用户编辑、版本浏览或页面回滚；所有页面仍绑定原 snapshot，不能与新 snapshot 的页面证据混用。

### 7.6 派生索引

- `wiki_topic_fts`：title、aliases、summary、body；body 在叙述页物化后追加；
- `wiki_topic_vectors`：初版保存在 SQLite，按 topic 的 title、aliases、summary 生成；
- block FTS／dense 保持原样；
- topic 索引可从 ledger／build 重建；页面 artifact 可重新校验 hash，但页面正文重建需要模型调用。

## 8. 检索与 Agent 契约

本设计的检索目标是返回**主题入口和完整证据范围**，不是让 Wiki 工具直接回答用户问题。

### 8.1 `search_wiki_topics`

输入：

- `query`；
- 可选 `semantic_tag`；
- 当前请求固定的 `snapshot_id`。

输出：

- 匹配主题 ID、标题、命中别名；
- 摘要；
- `content_level`；
- 来源文档标题、来源文档数／证据 block 数；
- 覆盖标签摘要与覆盖槽数量；
- 与当前 active Wiki build 的版本信息。

约束：

- active Wiki build 的 snapshot 必须与请求 snapshot 一致；否则返回 `wiki_build_not_ready_for_snapshot`；
- 不以目录路径作为唯一匹配来源；
- 选择集是 FTS 命中、标题／别名／摘要直接命中，以及 dense cosine 分数达到阈值（初版固定 `0.15`）的主题；
- 初版不使用 Top-k；返回全部入选主题，并在用户要求找全时不能把结果数量当成覆盖完成证明；
- 搜索结果是轻量目录，不重复携带完整 block ID、来源 ID 或覆盖问题；完整内容由 `get_topic_evidence` 读取；
- 为降低单一来源垄断模型注意力，目录按来源分组轮转；轮转只改变顺序，不删除主题，也不篡改 dense score；
- 来源轮转会牺牲纯相似度排序精度；弱相关来源可能提前，因此它只是注意力分配启发式；
- 不把 Wiki 摘要冒充事实证据。

### 8.2 `read_wiki_page`

返回结构化页面、段落级 block IDs 和 neighbor topics。页面中的链接只用于导航。

### 8.3 `get_topic_evidence`

把一个或多个 topic IDs 的全部直接证据 block 返回给主 Agent。

约束：

- 所有 block ID 必须属于请求绑定 snapshot；
- 找不到完整 block 时整次失败；
- 不由 Wiki 工具给出最终答案；
- 主 Agent 根据这些证据自行判断什么是“真正相关”。

### 8.4 召回路径

```text
query
→ topic FTS＋topic dense 找到主题入口
→ 返回不截断的轻量主题目录
→ 主 Agent 按语义和来源分组选择 topic IDs
→ 汇总 topic evidence blocks
→ 如需补足，再用 block dense＋FTS 在已识别文档／章节范围内补查
→ 主 Agent 读完整证据并回答
```

Wiki 不是 block 检索的替代品，而是额外的“主题面导航层”。

### 8.5 不可用与失败状态

- 请求 snapshot 没有兼容 active Wiki build 时，不注册 Wiki 工具，保留全部 block 检索工具；
- Agent 发出结构化 `wiki_status` 事件；前端明确提示“Wiki 尚未完成当前知识库的处理，部分主题工具暂时不可用；当前仍会使用原文检索”；
- 不自动回退到旧 snapshot 的 Wiki；
- Wiki 工具结果超过上下文预算时，显式产生 `context_budget_exceeded`，不能静默截断目录；
- catalog-only 页面可读取摘要与导航元数据；未物化的 narrative topic 返回 `wiki_page_not_materialized`。

### 8.6 初版控制 API

`POST /api/wiki/compile` 同步执行全量或增量编译，接受 `snapshot_id`、可选 `base_build_id`、granularity、`materialize_pages` 和 `map_parallel`。省略 `snapshot_id` 时使用当前 collection snapshot。

`GET /api/wiki/current/{snapshot_id}` 只返回与该 snapshot 严格匹配的 active topic build。

同步 API 只服务初版验证和本地管理入口；长任务进度、暂停／恢复、后台队列和“页面全部成功后再整体激活”属于后续任务化切片。调用方必须按实际执行时长设置客户端超时，不能把同步请求超时误判为 build 一定失败。

## 9. 增量、重解析与删除

### 9.1 新增文档

1. 新建 collection snapshot；
2. 只对新增文档跑 Document Map；
3. 与旧 topic ledger 做身份对齐；
4. 对新文档 block 做 Evidence Bind；
5. 更新受影响主题的证据和覆盖槽；
6. materialize 受影响叙述页；
7. finalize 后发布新 Wiki build。

未受影响页面的页面投影可复制到新 build；新 build 的 topic evidence 仍必须全部来自当前 snapshot。

### 9.2 文档更新

使用 `doc_id + doc_version`：

1. 从受影响主题移除旧版本 block refs；
2. 对新版本重新执行 Map、Align、Evidence Bind；
3. 保留仍有效的跨源页面内容；
4. 重写发生实质变化的页面；
5. 删除不再出现的单源主题；
6. 更新别名、覆盖槽、链接与索引。

### 9.3 文档删除

1. 通过 doc ID 反查所有主题与页面；
2. 移除只来自该文档的 block refs；
3. 重新计算受影响覆盖槽；
4. 单源页面若已无证据，则不进入新 build；
5. 多源页面按剩余证据重写；
6. 删除死链与孤儿主题；
7. 保留身份 tombstone，避免同一主题日后无必要地生成新 ID。

不承诺“删除后再导入同一文档”恢复到逐字相同的 LLM 页面；只保证身份、来源和结构可重建。

## 10. 失败语义与可观测性

| 失败 | 错误码 | 行为 |
| --- | --- | --- |
| snapshot 缺少或损坏 | `wiki_snapshot_unavailable` | 不启动编译 |
| 文档局部 block 未全部处理 | `topic_map_block_gap` | topic map 整体失败 |
| 模型引用窗口外 block | `topic_map_source_out_of_scope` | topic map 整体失败 |
| topic 模型 JSON 损坏 | `topic_map_response_invalid` | topic map 整体失败 |
| topic 输出截断 | `topic_map_response_truncated` | 不写该 topic map |
| 非法或越界的身份合并 | `topic_identity_merge_invalid` | topic ledger 整体失败 |
| topic evidence 不属于 snapshot | `wiki_evidence_out_of_snapshot` | build 失败 |
| 页面模型 JSON 损坏 | `wiki_page_response_invalid` | 不写该页面批次 |
| 页面输出截断 | `wiki_page_truncated` | 不写半页 |
| 页面段无证据 | `wiki_page_unsupported_paragraph` | 页面不发布 |
| 页面含跨 snapshot block | `wiki_page_cross_snapshot_ref` | 页面不发布 |
| 两个既有身份被错误合并 | `topic_identity_ambiguous` | topic ledger 整体失败 |
| 请求读取未物化叙述页 | `wiki_page_not_materialized` | 明确失败，不回退 catalog |
| active build 与请求 snapshot 不一致 | `wiki_build_not_ready_for_snapshot` | 明确失败，不暗中混用 |

每个阶段记录：

- build ID、snapshot ID；
- document／window／topic／page 范围；
- 模型、提示词和 schema 版本；
- request ID、usage、耗时、重试；
- 输入 block IDs 和输出 object IDs；
- 失败对象和是否可重试；
- 阶段产物 hash。

不记录 API Key、完整系统提示词外的敏感配置或用户私有正文到不必要的日志。

## 11. 成本与规模

WeKnora 的实测成本约为源文 token 的 25–42 倍，其中页面写作占 79%–82%。直接把这一模型套到 10 万 block 会过重。

本设计的成本控制点：

1. 每篇文档内部按 block 窗口 Map，默认 16 blocks／12,000 chars，窗口并发上限由调用方控制；
2. candidate extraction 只产出轻量骨架；同文档窗口结果在内存中按顺序合并；
3. 身份归一先做确定性别名图，再只对最多 5 个近邻、每批 20 个组调用 LLM；
4. topic ledger 全量保存，narrative page 只为达到阈值的主题生成；
5. topic embedding 按 provider batch size 发送；页面正文不进入 topic dense；
6. 增量只重算变更文档 map，未变 map 直接 adopt；身份仍以全量 map＋base ledger 重新归一；
7. 未受影响页面复制投影，受影响页面重新生成；
8. topic 与 block 索引分开，互不污染。

首轮产品实验必须记录：

- 每 1,000 block 的 Map／Cite／Page token；
- topic 数、catalog-only 数、narrative 数；
- 页面平均引用数和未引用证据数；
- 每篇文档新增／更新／删除的主题与页面数；
- 编译时间、失败率、显式重跑次数和页面复制／重写数量。

## 12. 验收与评测

### 12.1 结构门禁

- 所有 topic evidence 指向当前 snapshot 的有效 block；
- 所有 narrative paragraph 至少一个 block ref；
- 无跨 snapshot 引用；
- 无无证据叙述页；
- 查询只接受与请求 snapshot 一致的 active Wiki build；
- topic 索引可由 ledger／build 重建；
- 删除文档后，相关页面和链接不得引用旧 block。

### 12.2 主题覆盖评测

对每个代表主题建立：

- 必须出现的主题／方面；
- 必须命中的 block IDs；
- 允许不覆盖的边界；
- 已知干扰项。

指标：

- topic recall：应存在的主题入口中被召回的占比；
- block recall：评分相关 block 被 topic evidence 覆盖的占比；
- page evidence precision：返回证据中被人工／判官判为相关的占比；
- coverage-slot recall：已知方面被覆盖槽覆盖的占比；
- unsupported claim rate：无有效 block refs 的叙述段占比；
- resolution latency 和 token cost。

c13-003 只能作为小样本探针，不能代表全主题召回率。必须增加未被历史实验污染的 held-out 主题。

### 12.3 增量／删除回归

- 新增文档只影响预期页面；
- 同一主题不会因重解析产生重复身份；
- 删除文档后，单源页消失或多源页失去旧来源；
- 页面导航只指向当前 build 的 topic；topic FTS 的 body 与已物化页面一致；
- 失败 build 不切换 active pointer。

## 13. 实施拆分

### 13.1 推荐顺序

```text
069 Snapshot Topic Map
  → 070 Identity Ledger
  → 071 Topic Build + Search
  → 072 Narrative Materialize
  → 073 Incremental Build / Retraction
  → 074 Agent Integration
```

依赖原则：

- 069 不依赖叙述页，也不依赖主 Agent；
- 070 以 069 的文档局部候选为输入；
- 071 只消费稳定 topic ledger，不在查询时补做身份对齐；
- 068 的页面 artifact 与段落回源校验作为 072 的底座；
- 073 在 071／072 的全量链稳定后开始；
- 074 最后接入，避免检索底座尚未稳定时污染主 Agent 行为。

069–074 是线性依赖链，使用同一个从 `origin/main` 创建的集成 worktree 连续实现：

```text
worktree: D:\lida-data\vscode\ai-study-helper-v2-068-llm-wiki
branch:   feat/069-074-llm-wiki
```

该分支不进入产品 `main`，也不与 065／066／文章 worktree 混用。切片之间保持独立文档、测试组和证据包；在集中反向解释通过前不提交、不推送、不合并。通过后按切片拆分逻辑提交。

### 13.2 切片 069：Snapshot Topic Map

**目的**

证明系统能从冻结 snapshot 的全部 block 中发现“值得继续查”的 topic candidates，并为每个 candidate 绑定真实 block。

**输入**

- 当前 collection snapshot；
- 全部文档、section tree 和 block；
- 模型与提示词版本；
- 可选 `semantic_tags` 输出开关。

**输出**

- 按文档和 block window 生成的不可变 `topic-map` artifact；
- 统一 `WikiCandidate`：名称、别名、摘要、可选语义标签、文档局部覆盖槽和 block refs；
- per-document candidate map、逐 window usage 和 manifest hash；
- 与 snapshot manifest 绑定的编译 manifest。

**本切片必须验证**

- 全部 block 都被纳入某个处理 window，或显式失败；禁止静默截断；
- candidate 不得引用 window 外或 snapshot 外 block；
- 没有 block 证据的 candidate 不进入输出索引；
- 同一固定输入与 fake provider 下 candidate 输出和 artifact 结构稳定；
- 通用提示词中不出现 c13 或任何用例专属主题词。

**不包含**

- 跨文档身份合并；
- narrative page；
- FTS／dense topic index；
- 主 Agent 工具；
- 增量更新和删除。

**进入下一片的门禁**

- 固定测试集上，人工已知的必要主题至少能被文档局部候选发现；
- 对超长文档、无标题文档和表格文档不丢尾部 block；
- 失败和重试可恢复，且不会写出 partial build。

**预计 B 级决策**：5 个以内，主要是 window 大小、抽取粒度、语义标签输出、失败重试和候选准入规则。

### 13.3 切片 070：Wiki Identity Ledger

**目的**

把多篇文档中的同类候选归一为稳定 `WikiTopic`，同时确保相邻但不同的主题不被误合并。

**输入**

- 069 的文档局部 topic maps；
- 上一 Wiki build 的身份／别名账本；
- 精确名称、别名、FTS 或 embedding 召回的少量相似候选。

**输出**

- build 级 topic ledger；
- 稳定 `topic_id`；
- canonical name、aliases、语义标签；
- 跨文档 evidence refs 与合并后的 coverage slots；
- 未解决 merge 数量；初版不保存逐对低置信度拒绝原文。

**本切片必须验证**

- 同一主题的翻译、缩写和旧名可以合并；
- “相关但不相同”、不同版本、父子概念不合并；
- `semantic_tags` 不同不能制造孪生 topic；
- 同一文档重解析时优先复用既有 `topic_id`；
- LLM 只能选择召回候选 ID 或 `new`，不能发明 topic identity；
- 任何被丢弃的 candidate 都必须出现在诊断账本中。

**不包含**

- 页面正文；
- 目录；
- 页面链接；
- 查询工具。

**进入下一片的门禁**

- 身份测试集达到既定合并与不合并门槛；
- topic evidence 仍全部可回源到 069 的原始 block；
- 同一输入重复运行不会批量生成孪生 topic。

**预计 B 级决策**：4 个以内，主要是相似候选召回方式、判同置信门槛、人工锁定优先级和同主题拆分策略。

### 13.4 切片 071：Topic Build + Search

**目的**

把 topic ledger 变成可原子发布、可搜索、可读取完整证据的 Wiki 数据层；仍然不生成回答。

**输入**

- 070 的 topic ledger；
- snapshot manifest；
- 页面／主题搜索 schema。

**输出**

- SQLite Wiki build 与 active pointer；
- alias-aware topic FTS；
- 可选 topic dense index；
- `search_wiki_topics` 服务；
- `get_topic_evidence` 服务；
- 可重建的派生索引。

**本切片必须验证**

- 只有完整 build 才能切换 active pointer；
- 搜索会命中标题、别名和摘要；
- 搜索结果的 evidence block 全部属于请求绑定 snapshot；
- active Wiki build 与请求 snapshot 不一致时返回 `wiki_build_not_ready_for_snapshot`；
- 索引损坏后可从头重建，不改变 topic identity 和证据；
- 目录不参与“是否找全”的计算。

**不包含**

- narrative page；
- 主 Agent 工具注册；
- 增量更新。

**进入下一片的门禁**

- 在同一测试主题上，topic search＋evidence 读取能返回完整证据集合；
- 搜索不再依赖查询时重新生成 subtopics；
- 15 篇快照通过后，再补 100／1,000 篇规模测试。

**预计 B 级决策**：3 个以内，主要是索引方案、搜索返回形态和 active build 状态语义。W4 与 W6 必须在开始前确认。

### 13.5 切片 072：Narrative Materialize

**目的**

为达到阈值的 `WikiTopic` 生成结构化叙述页，验证阅读价值和页面级成本。

**输入**

- 070／071 的 topic ledger；
- topic 的全部直接证据 block；
- 覆盖槽；
- 与当前主题共享直接证据的允许相邻 topic IDs。

**输出**

- `topic` 叙述页；
- 每段 `source_block_ids`；
- 页面 artifact、hash 和版本；
- materialization 计数和 token 成本。

**本切片必须验证**

- 无证据段、跨 snapshot 引用和页面截断均阻断发布；
- `catalog_only` 主题不会被伪装成完整叙述页；
- 068 的 artifact／hash／回源校验继续成立；
- 页面相关链接只能引用本 build 已存在的 topic；
- 页面生成失败不会破坏 071 已发布的 topic search 数据。

**不包含**

- 自动生成 synthesis／comparison；
- 初版不生成 `document_summary` 和 `index`；
- 页面编辑器；
- 人工版本合并；
- 增量重写。

**进入下一片的门禁**

- 代表性主题页可读且每段可回源；
- catalog-only 与 narrative 的准入阈值经过实测；
- 全库单页和整库一次性写正文的退化路径被明确拒绝。

**预计 B 级决策**：4 个以内，主要是 W2 阈值、W3 输出协议、页面规模和续写策略。开始前至少确认 W2／W3。

### 13.6 切片 073：Incremental Build / Retraction

**目的**

在已有完整 Wiki build 上处理文档新增、更新和删除，保持 topic identity、页面和索引一致。

**输入**

- 新 collection snapshot；
- 上一 Wiki build；
- 文档新增／替换／删除集合。

**输出**

- 基于 `base_wiki_build_id` 的新 Wiki build；
- changed／removed／affected topic IDs、物化页数和复制页数摘要；
- 对新增／更新文档的局部 topic map 和身份变更；
- active pointer 中保留 `previous_build_id` 审计引用；初版不提供 Wiki rollback API。

**本切片必须验证**

- 未变文档的 topic ID 和页面身份尽量保持；
- 文档更新会移除旧 block refs；
- 删除只移除对应来源，多源页保留其余证据；
- 单源页失去全部证据后不进入新 build；
- 页面导航无未知 topic，证据仍属于当前 snapshot，FTS body 与页面投影一致；
- 失败增量 build 不切换 active pointer。

**不包含**

- 多用户并发编辑；
- 任意人工正文与自动更新的冲突合并；
- 删除后要求逐字复原；
- 重新设计全局 taxonomy。

**进入下一片的门禁**

- 新增、更新、删除三类回归通过；
- 同一主题不会因重解析产生重复 identity；
- build 失败路径可观察；人工回滚入口留待后续切片。

**预计 B 级决策**：4 个以内，主要是变更范围计算、旧页面复用、删除 tombstone 和重写策略。

### 13.7 切片 074：Agent Integration

**目的**

将 Wiki 作为主 Agent 的一条独立主题面导航通道接入，不改变主 Agent 的最终作答职责。

**输入**

- 请求开始时固定的 snapshot；
- 兼容 snapshot 的 active Wiki build；
- `search_wiki_topics`、`read_wiki_page`、`get_topic_evidence`；
- 原有 block 检索工具。

**输出**

- 条件注册的 Wiki 工具；
- trace 中可观察的 topic、page、evidence block IDs；
- Wiki 与 block 检索合并后的 Tool Observations；
- 用户可见的 Wiki ready／not-ready 状态；
- 明确的不兼容和预算错误。

**本切片必须验证**

- Wiki tool 只使用请求绑定的 snapshot；
- 不兼容 build 明确失败，不自动混用旧 Wiki；
- Wiki 工具不直接生成最终答案；
- 主 Agent 可沿着 topic → page → evidence → block 回源；
- 没有 Wiki build 时原 block 检索链路仍可独立工作；
- 已知 c13 探针有真实回放结果；新增 held-out 主题作为后续泛化实验。

**不包含**

- 自动替用户决定“相关”范围；
- 用页面摘要代替 citation；
- 将页面正文写入 block 索引；
- 移除原有检索工具。

**预计 B 级决策**：2 个以内，主要是工具注册时机和 Wiki／block 结果的组合策略。

### 13.8 跨切片验证阶梯

每个切片都必须按同一阶梯验证，不能等全部完成后再测规模：

| 阶段 | 语料 | 目的 |
| --- | --- | --- |
| V1 | 当前 15 篇快照 | 验证功能与可回源 |
| V2 | 100 篇、约 1 万 block | 初版使用工程测试／模拟验证批处理、并发与身份稳定性，不默认多轮真实 LLM |
| V3 | 1,000 篇、约 10 万 block | 初版只验证 100,000 block 分区和工程边界，不执行整库真实 LLM 编译 |

任何切片在 V1 失败时不得用更大语料“跑通”掩盖问题。

### 13.9 决策前置门

| 准备开始的切片 | 必须先确认的决策 | 当前状态 |
| --- | --- | --- |
| 069 | W1：topic ledger＋narrative 两层；W8：抽取不做硬分类 | 已确认 |
| 070 | 069 自动验证通过 | 已完成 |
| 071 | W4：Wiki 独立索引；W5：目录不承担完整性；W6：snapshot 不同步时不混用；W9：全量轻量目录 | 已确认（2026-11-01） |
| 072 | W2：materialization 阈值；W3：结构化页面协议；W7：初版只生成 topic 页面 | 已确认（2026-11-01） |
| 073 | 071／072 的 build、版本和删除语义稳定 | 自动验证完成 |
| 074 | W6 的产品行为与 071／073 回归通过 | 已确认；真实覆盖效果作为 BL-0012 后续 |

068 的 artifact、段落回源与失败门禁可继续作为独立子层审查，但在 072 使用前必须完成其 B 级决策确认和反向解释门禁；未确认的 068 代码不能隐式成为本设计的产品基线。

## 14. 决策账本

| ID | 问题 | 建议 | 备选 | 状态 |
| --- | --- | --- | --- | --- |
| W1 | 是否采用 topic ledger＋narrative 两层 | 采用 | 每个主题立即写完整页，成本高 | 已确认（2026-10-31） |
| W2 | 首版 materialization 阈值 | ≥2 个直接 block 或跨 ≥2 文档；其余 catalog-only | 所有主题一视同仁 | 已确认（2026-11-01） |
| W3 | 页面正文输出格式 | 结构化 sections＋paragraph 引用 | 自由 Markdown＋页级 block refs | 已确认（2026-11-01） |
| W4 | 主题检索入口 | 独立 topic FTS＋dense；叙述页正文追加到 FTS，初版不重建 dense 正文 | 只按 slug／正则搜索 | 已确认（2026-11-01） |
| W5 | 目录作用 | 只做候选导航，不参与完整性判断 | 把目录当枚举通道 | 已确认（2026-11-01） |
| W6 | Wiki build 与 snapshot 不同步时 | 明确 `wiki_build_not_ready_for_snapshot` | 自动使用旧 wiki＋新 block | 已确认（2026-10-31，方案 A） |
| W7 | 首轮自动生成的页面类型 | 初版只生成达到阈值的 `topic` 页；`document_summary`／`index` 后续 | 同时自动生成全部页面类型 | 已确认（2026-11-01） |
| W8 | 抽取阶段是否硬分实体／概念／主题 | 不硬分；统一 `WikiCandidate`，类型仅作非主键 `semantic_tags` | 分为 entity／concept／topic 或 entity／concept | 已确认（2026-10-31） |
| W9 | 全量主题目录的返回与排序 | 轻量目录、不截断、按来源轮转 | 完整证据随搜索返回；纯相似度顺序 | 已确认（2026-11-01） |
| W10 | 初版 Wiki 编译触发方式 | 产品 API 同步触发，正式后台任务化后续 | 本轮同时重做任务队列 | 已确认（2026-11-01） |

W6 采用“禁止混用”是 C 级边界候选：如果产品要求新 snapshot 发布后 Wiki 必须立即可用，就必须把 Wiki 编译纳入发布门禁。当前建议保持 collection 与 Wiki 异步，但查询时严格检查版本。

## 15. 明确禁止

- 禁止把 10 万 block 全量塞进一次 LLM 请求并称为 Wiki。
- 禁止调用方先用已知答案或已知目标 block 手工圈定页面，再把命中称为“找全能力”。
- 禁止把通用提示词写成只适用于某个 c13 类问题的模板。
- 禁止无 block 来源的主题页和段落。
- 禁止把目录、页面链接或摘要当成事实关系。
- 禁止从旧 Wiki build 借 block 来补新 snapshot。
- 禁止在页面截断时保存半页并报告成功。
- 禁止用“页面引用数”直接代表主题覆盖。

## 16. 停止与推翻条件

满足任一条件时停止当前实现并重做架构：

- topic ledger 在 held-out 主题上仍无法稳定召回已知必要主题；
- 页面段落级回源无法在增量／删除后保持一致；
- 目录和页面链接持续漂移，导致同一主题无法稳定解析；
- 编译成本超过产品可接受上限且 catalog-only 降级仍不能缓解；
- 用户最终仍只能依赖临时查询扩展，预编译产物没有独立增益；
- 页面摘要引入无法接受的合法 ID 错误事实，且独立验证无法阻断。

## 17. 与 068 证据地图的关系

068 已经验证了两个可复用基础设施：

- Wiki artifact 可以绑定冻结 snapshot；
- 页面段落可以严格回源，并在异常时整次失败。

但 068 当前由调用方显式提供页面与候选 block，不属于完整的 Wiki 编译。其手工选择任务状态相关文档的实验不能作为“找全能力”证据。

068 应保留为后续 Narrative Materialize 的 artifact／校验底座；主题发现、身份对齐、证据绑定、topic ledger 和检索接入由本设计重新承接。
