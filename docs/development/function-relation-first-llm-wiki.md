# Function-Relation-First LLM Wiki：通用谓词、晚绑定与证据覆盖

- 状态：讨论稿，不是设计基线
- 日期：2026-11-10
- 名称变化：`Term-first` 是讨论中点；继续推演后，方法核心已经转为领域无关的功能关系和
  查询时领域语义晚绑定。Term 降为必要的词面锚点，不再是主要矛盾。
- 设计收敛：[14 双入口概念—功能知识索引候选设计](../design/14-dual-entry-concept-function-knowledge-index.md)
- 关联：[13 Profile-Driven LLM Wiki 总体设计](../design/13-profile-driven-wiki-evidence-retrieval.md)、
  [设计思考记录](design-thinking.md)、
  [c13-003 固定目标](../../backend/tests/fixtures/c13_003_wiki_topic_v2_targets.json)
- 外部概念来源：[Andrej Karpathy · llm-wiki Gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)

## 1. 这份讨论想解决什么

当前 Profile-Driven LLM Wiki 的完整性依赖预编译出的 Topic／Group 是否恰好覆盖用户的提问视角。
但用户可以从任意角度提问，而预编译只能覆盖有限视角。

`c13-003` 的“AI Agent 隔离与控制边界”是一个典型横切问题：

- 它不是 Agent 领域唯一的主分类；
- 它会穿过文件、凭证、工具、外部内容、上下文、并行状态和子 Agent 等多个区域；
- 很多证据块根本不会出现“隔离”或“边界”，而是用具体机制描述隐含的边界。

讨论得到一个候选方向：

> 不再主要依赖预编译领域分类和 Group，而是先编译术语、概念义项、领域无关的功能断言
> 和证据；查询时再由 LLM 把这些可组合原语晚绑定成用户需要的领域切面和隐含概念。

### 1.1 当前核心命题

本轮推演已经把原来的“Term-first”修正为：

> **通用谓词离线泛化，领域语义在线晚绑定。**

即：

```text
离线：
Evidence → Concept Sense → Functional Assertion

在线：
领域问题 → 领域谓词/功能切面 → 通用谓词组合
→ 关系约束 → Evidence
```

领域谓词不需要预先进入 schema。例如：

```text
领域谓词：controls_filesystem(worktree)
通用表达：constrains(worktree, filesystem_access)
```

查询时才把 `constrains`、`separates`、`protects`、`recovers` 等关系组合成
“隔离与控制边界”。

因此，这套方法的本质可以概括为：

> **利用 LLM 的在线推理能力，降低对领域知识图谱和领域谓词 schema 的要求。**

它仍然认为谓词重要，只是把谓词拆成：

- 领域无关的通用功能谓词；
- 领域无关的角色和条件；
- 领域相关的具体角色填充；
- 查询时的领域谓词重建。

### 1.2 与知识图谱的关系

这不是“完全不要知识图谱”，而是：

- 不要求完整实体关系图；
- 不要求预先建立领域谓词体系；
- 仍需要一个浅层的 Functional Assertion 索引；
- 所有功能断言必须能回到原始 Evidence。

因此更准确的说法是：

> 不需要完整图谱作为推理引擎，但需要功能关系作为检索索引。

## 2. Karpathy LLM Wiki 给出的基础

Karpathy 的原始 `llm-wiki` 思路不是“把文档切块后做 RAG”，而是让 LLM：

- 增量维护一个持久的 Markdown Wiki；
- 将新资料整合到已有页面、链接和综合中；
- 通过 `index.md` 导航，通过 `log.md` 记录变化；
- 定期检查矛盾、过期内容、孤儿页面和缺失概念；
- 由人负责选资料、判断重点和指导分析。

这仍然是领域相关的：

- schema 包含领域组织约定；
- 人决定什么重要；
- 人指导 LLM 应该强调什么。

它没有消除领域知识，只是把领域知识放在 schema、人的判断和持续维护中。

## 3. RAG 中要区分两类知识

LLM 同时拥有：

1. 通用知识、领域背景和推理能力；
2. 当前知识库中的私有、时效性和可引用事实。

最终回答不应禁止 LLM 使用自身知识，但必须区分：

| 知识类型 | 使用方式 |
| --- | --- |
| 知识库事实 | 优先使用检索证据，必须可回源 |
| 模型通用背景 | 可用于解释概念、拆解问题和补足基础背景 |
| 两者冲突 | 以当前知识库证据为准，并明确冲突 |

因此，“允许模型使用通用知识”不等于“模型生成内容可以冒充知识库事实”。

## 4. 术语是锚点，功能关系是核心

传统知识图谱倾向于：

```text
实体 → 类型 → 关系 → 证据
```

但对开放、持续扩充的个人知识库，会遇到：

- 一个被抽出的实体不一定自洽；
- 同一个名称可能一词多义；
- 一个概念可能没有唯一实体；
- 严格实体关系图成本高，而且关系经常不重要；
- 人类 Wiki 的页面标题更接近“主题／术语”，而不是严格实体。

候选模型：

```text
Term（术语／标题性词项）
  → Concept Sense（上下文中的概念义项）
  → Functional Assertion（功能关系）
  → Evidence（原文块）
  → Aspect（证据方面）
```

这里的“第一等元素”需要进一步修正：

- Term 是词面定位和证据索引入口；
- Concept Sense 为关系提供类型明确的参与者；
- **Functional Assertion 才是把问题语言映射到实现证据的核心语义层**；
- Evidence 是唯一事实源。

因此 `Term-first` 只是早期名称。严格地说，当前方向是
**Function-Relation-First**。

### Term

Term 是词面检索锚点，可以来自：

- 普通技术术语；
- 多词短语；
- 产品名、标准名和缩写；
- 事件、方法、风险或过程标题。

Term 不承诺唯一语义。同一个 Term 可以有多个 Concept Sense。

### Concept Sense

Concept Sense 是 LLM 根据当前知识库上下文归纳出的概念义项。

例如：

```text
Term: checkpoint
Concept Sense:
  Agent 执行过程中的持久状态保存点，
  用于失败或中断后恢复，而非从头重启。
```

### Entity

Entity 不是第一等必需品，可以视为 Concept Sense 的一个子集：

- 产品；
- 组织；
- 人物；
- 标准；
- 版本化模型。

因此不需要先证明某词一定是实体，再允许它成为知识入口。

## 5. 空壳 Topic 仍然合法

单篇抽取时，一个概念可能只有名称和父级关系，没有定义或机制。例如：

```text
harness 包含 7 个组件：
  system prompt、tool description、tool implementation、middleware、
  skill、sub-agent configuration、long-term memory
```

此时为每个组件建立空壳入口是合理的：

- 它是未来证据的稳定挂接点；
- 它保留“属于某个父机制”的关系；
- 后续文档提供定义时，可以追加到同一身份；
- 如果提前删除，未来即使抽出定义，也可能丢失原来的机制族关联。

需要区分：

- **身份有效性**：这个对象是否值得作为稳定入口继续积累证据；
- **知识充实度**：目前已经知道它多少内容。

二者不能互相替代。

## 6. c13-003 暴露了词面检索的根本缺口

当前跨文档金标准的 7 个核心组去重后有 48 个目标 block，其中 47 个能在本次
Extraction Run 的候选证据中找到原文。

检查原文直接词形后得到：

- 只有 13/48 个 block 出现 `isolation`、`isolated`、`isolating` 或
  `boundary/boundaries`；
- 加上 `sandbox`、`control`、`bound` 等相关词，也只有 19/48；
- 剩余 28/47 个核心 block 完全没有这些直接相关词；
- 英文原文中自然不会出现中文“隔离／边界／限制”。

例如：

- checkpoint／resume 描述保存和恢复执行状态，但没有写“隔离”；
- `/clear`、`/compact`、`/rewind` 描述上下文清理、压缩和回退，但没有写“边界”；
- 工具清单描述文件、shell、网络和子 Agent 能力，但没有写“边界”；
- 子 Agent 使用 clean context window，但没有写“隔离”；
- permission、Esc 中断和 rewind 控制 Agent，但没有共同使用 `boundary`；
- trace、evaluator 和 checkpoint 属于审计与可观察边界，但通常不写这些词。

这说明：

> “隔离和控制边界”不是语料中的共同词项，而是查询者施加在多种实现机制上的功能切面。

## 7. 关键一跳：领域无关的功能关系

仅有 Term 和 Concept Sense 仍然不够。查询“隔离”无法直接找到 `worktree`。

需要编译一层领域无关的功能语义：

```text
FunctionalAssertion
  subject_concept_ref
  predicate
  object_ref / object_role
  affected_actor
  affected_state / resource / action / source
  condition
  polarity
  evidence_block_id
```

通用谓词可以包括：

- `enables`
- `constrains`
- `separates`
- `protects`
- `requires`
- `depends_on`
- `persists`
- `recovers`
- `resets`
- `removes`
- `trusts / distrusts`
- `observes`
- `delegates`
- `changes`
- `consumes`
- `fails_when`

通用角色可以包括：

- `actor`
- `action`
- `object`
- `state`
- `source`
- `resource`
- `condition`
- `outcome`
- `evidence`

这些谓词和角色类型不依赖具体领域；具体填充值仍然领域相关。

## 8. 用功能关系解释 c13-003

| 原文概念 | 通用功能关系 | c13 中的隐含切面 |
| --- | --- | --- |
| worktree | `separates(edit_state, git_checkout)` | 并行工作区隔离 |
| checkpoint | `persists + recovers(agent_state)` | 持久状态保存与恢复 |
| `/clear` | `resets(context_window)` | 上下文生命周期控制 |
| `/compact` | `compresses(context)` | 上下文容量控制 |
| permission allowlist | `constrains(tool_action)` | 工具与行动权限边界 |
| credential proxy | `protects(credential)` | 凭证与身份边界 |
| MCP local／remote | `trusts_or_distrusts(external_source)` | 外部工具来源信任边界 |
| tool inventory | `enables + constrains(agent_action)` | 行动能力边界 |
| subagent | `delegates + separates(context)` | 子 Agent 委派边界 |
| Esc／rewind | `interrupts + restores(agent_state)` | 人工中断与恢复边界 |
| trace／evaluator | `observes(agent_execution)` | 审计与可观察边界 |

查询时可以把“control boundary”组合成：

```text
control_boundary(AI Agent, X)
≈
constrains(AI Agent, X)
OR separates(AI Agent, X)
OR protects(AI Agent, X)
OR controls_lifecycle(AI Agent, X)
OR observes(AI Agent, X)
```

这样即使原文完全没有“isolation”或“boundary”，仍然可以按功能关系召回。

## 9. 编译流程

### 9.1 编译术语

从全部 block 中提取值得检索的术语和短语，并保留全部出现位置。

不要求术语是实体，也不要求预先确定领域类型。

### 9.2 归纳概念义项

对每个 Term 的全部出现上下文进行聚类和解释，形成一个或多个 Concept Sense。

同一个 Term 可以生成多个义项；多个 Term 也可以指向同一个概念。

### 9.3 生成功能断言

对每个 Concept Sense，根据其证据提取领域无关的功能关系：

- 限制了谁；
- 分离了什么；
- 保护了什么；
- 保存或恢复什么状态；
- 信任或隔离什么来源；
- 需要什么条件；
- 产生什么可观察结果。

功能断言必须绑定原文 block，不能只保留 LLM 摘要。

### 9.4 建立证据索引

最终形成多路可检索索引：

```text
Term → Occurrence → Evidence
Term → Concept Sense → Evidence
Functional Relation → Concept Sense → Evidence
Aspect／Facet → Evidence
```

这里的 relation 是检索索引，不是完整知识图谱。

### 9.5 可选导航结构

Topic、Group 和 Wiki 页面仍可作为导航层存在，但不再承担完整性证明。

编译不需要保证已经预编译出“隔离”Group。

## 10. 查询流程

这一节故意不提供 `sandbox`、`VM`、`egress` 等 seed，直接检验系统能否从问题本身
形成控制面：

> AI Agent 中有哪些隔离和控制边界？

有 seed 时，系统可以用正例锚定“边界”的义项；没有 seed 时，系统必须依靠：

- `AI Agent` 提供的领域上下文；
- `隔离／控制` 提供的功能意图；
- 通用功能谓词和角色模型；
- LLM 的领域先验；
- 当前知识库的术语和概念邻域；
- 迭代覆盖审查。

这比有 seed 的查询更容易漏掉非显眼控制面。因此系统不能只生成一个“最可能”的边界
义项，而要生成多个候选控制面并做覆盖检查。

### 10.1 解析查询

```text
domain = AI Agent
predicate = isolate / control / constrain
target = boundary set
```

### 10.2 生成可能的义项

“边界”可能包括多个义项，不能只选一个：

- 安全与权限隔离；
- 执行环境和资源隔离；
- 状态与上下文生命周期控制；
- 任务委派与责任边界；
- 人工监督与中断边界；
- 审计和可观察边界。

这一步在没有具体机制 seed 时尤其关键，因为“边界”本身也是一个多义词。

### 10.3 生成控制面候选

通用的控制面检查可以包括：

- Agent 可以访问什么；
- Agent 可以做什么；
- Agent 以什么身份行动；
- Agent 信任什么输入；
- Agent 保存什么状态；
- Agent 如何管理上下文；
- Agent 如何委派任务；
- 谁能控制或中断 Agent；
- 系统如何观察 Agent；
- Agent 消耗什么资源。

### 10.4 匹配功能关系

通过编译索引查找满足以下关系的概念：

```text
constrains(agent, ?)
separates(agent_state, ?)
protects(agent_secret, ?)
observes(agent_execution)
...
```

### 10.5 迭代补齐

如果某一控制面没有结果：

1. 检查知识库是否确实没有；
2. 用概念近邻和术语别名扩展；
3. 使用共享对象、来源和 Aspect 关系扩展；
4. 由 Agent 生成新的功能假设；
5. 再次检索并记录新增覆盖。

### 10.6 覆盖审查

最终检查每个候选控制面是否有证据支持，并区分：

- `covered`；
- `partial`；
- `missing`；
- `not_applicable`。

### 10.7 返回证据

工具仍只返回原始 blocks；概念、功能和覆盖状态进入诊断或内部选择过程，不冒充事实源。

## 11. 领域知识如何被弱化

可以大幅弱化的：

- 预定义完整领域分类；
- 预定义实体类型；
- 预定义全部 Group；
- 预定义“隔离”等具体问题切面；
- 手写大量领域抽取通道。

不能消除的：

- 通用功能谓词和角色模型；
- 术语抽取、别名和规范化；
- 概念义项归纳；
- 查询时的功能切面和语义扩展；
- 什么算完整覆盖的产品判断；
- LLM 领域先验不足时的补充知识。

所以最终不是“无领域配置”，而是：

> **领域无关的索引机制＋查询时领域化＋少量可审查的覆盖契约。**

## 12. 与业界思路的关系

截至 2026-11-10，在公开论文、GitHub 项目和文档中，没有找到一个与完整组合完全相同的
成熟标准方案：

```text
固定的小型通用功能谓词
+ LLM 离线抽取 Functional Assertion
+ 领域谓词在查询时晚绑定
+ 原始证据回源
+ 覆盖审查
```

但每一部分都有明确先例，而且有两脉特别值得关注。

| 方向 | 代表 | 与本方案的关系 |
| --- | --- | --- |
| Open Information Extraction | [OpenIE survey](https://arxiv.org/abs/2208.08690)、[复杂问题 OpenIE 问答](https://aclanthology.org/P17-2049/) | 不要求预定义领域谓词，直接从文本抽取开放关系并支持组合检索 |
| Universal Schema | [KB＋Text 联合问答](https://aclanthology.org/P17-2057/) | 把结构化 KB 关系和开放文本关系映射到公共嵌入空间，最接近“通用关系空间” |
| Open-schema KB Construction | [ImPaKT](https://arxiv.org/abs/2212.10770) | 同时发现属性、类型、关系和 schema，不要求固定关系枚举 |
| Schema-adaptable KGC | [Schema-adaptable Knowledge Graph Construction](https://aclanthology.org/2023.findings-emnlp.425/) | 面向动态领域持续扩展 schema，而不是固定封闭 schema |
| 动态本体／概念级 RAG | [DRAGON-AI](https://doi.org/10.1186/s13326-024-00320-3)、[SchemaRAG](https://doi.org/10.2139/ssrn.6636731)、[TraceRAG](https://doi.org/10.1007/978-981-92-3432-5_7) | 探索 LLM 动态生成 ontology、概念级图和查询时概念追踪 |
| 持久 LLM Wiki | Karpathy `llm-wiki`、多个 GitHub 实现 | 持久化、主题页、索引和维护 |
| Topic Maps | ISO Topic Maps | 主题、名称、出现位置和关联分离 |
| Concept-based IR | Explicit Semantic Analysis | 用概念而不是纯词面表示文档和查询 |
| Word Sense Induction | 词义归纳研究 | 从上下文自动形成 Term 的多个义项 |
| GraphRAG／HippoRAG／RAPTOR | 图、社区、层次摘要 | 预编译结构与多跳检索 |
| Agentic RAG | 查询拆解与迭代检索 | 查询时补足隐含切面和证据 |
| Faceted Search | 多视角信息检索 | 同一对象按多个功能面检索 |

### 12.1 最接近的两条学术路线

#### OpenIE

OpenIE 已经在做“不预定义领域谓词”的关系发现：

```text
(Arg1, relation phrase, Arg2)
```

差异是 OpenIE 通常保留自由关系短语，稳定性有限；本方案希望进一步归一化为一个小型的
通用功能谓词集合。

可以理解为：

> OpenIE 的关系归一化和 Schema 化。

#### Universal Schema

Universal Schema 将 KB 关系类型和文本开放关系对齐到公共空间，并已用于 KB＋Text
联合问答。这与本方案想解决的“知识库不完整、但文本含有事实”的问题高度接近。

差异是 Universal Schema 主要依赖关系表示学习和已观测关系类型；本方案进一步使用 LLM：

- 归纳概念义项；
- 归一化功能谓词；
- 在查询时把领域谓词动态绑定为通用谓词组合。

可以暂称为：

> **Late-Bound Universal Schema for LLM Wiki Retrieval**

中文：

> **查询时晚绑定的通用关系 Schema。**

### 12.2 本方案的差异

本方案的辨识点不是任何单一组件，而是：

```text
Term 作为检索锚点
→ LLM 归纳 Concept Sense
→ 领域无关 Functional Assertion 成为核心索引
→ 原始 Evidence 回源
→ 查询时绑定领域谓词并组合覆盖切面
```

可以暂称为：

> **Function-Relation-First Retrieval**
>
> 功能关系驱动、义项感知、领域谓词晚绑定、证据可回源的 LLM Wiki。

## 13. 主要优势

- 不要求普通用户先构建领域本体；
- 支持同一术语多个概念义项；
- 支持空壳 Topic 持续积累；
- 不要求预编译猜中用户的全部提问角度；
- 能把隐含概念映射回具体实现机制；
- 领域谓词可以被表达为通用谓词的查询时组合；
- 原文仍是唯一事实源；
- Topic／Group 可以退回导航和加速角色。

## 14. 主要风险

- 术语爆炸，需要入口资格和聚合机制；
- 同义词、上位词、隐喻和跨语言表达；
- Concept Sense 归纳不稳定；
- Functional Assertion 可能抽取错误或过度推断；
- 一个 Functional Relation 缺少角色、条件和极性时会产生错误匹配；
- 查询时的切面生成可能受 LLM 领域先验限制；
- 新领域或新概念可能尚未进入模型知识；
- 覆盖判断仍需要产品或人工契约；
- 不能证明理论上的绝对完备性。

## 15. 明天优先讨论的问题

1. Functional Predicate 的最小闭集应该包含哪些关系？
2. Functional Assertion 需要哪些角色、条件、极性和强度？
3. 功能关系在什么阶段抽取：候选抽取、Topic 编译、Aspect 编译，还是单独一层？
4. Functional Assertion 是否必须绑定 Concept Sense，还是可以直接绑定 Evidence？
5. 编译时抽象功能关系，还是查询时从概念摘要临时推导？
6. 如何审计功能关系没有引入原文不支持的信息？
7. 如何控制同一证据被抽象成过多功能关系？
8. Concept Sense 的稳定性和增量更新如何处理？
9. 查询时的切面生成如何避免只想到明显的安全边界？
10. 覆盖完成条件如何定义和测试？
11. Group／Topic 在新模型中保留哪些职责？
12. 先用 `c13-003` 做哪种消融：
    - 纯 Term；
    - Term＋Concept；
    - Term＋Concept＋Functional Relation；
    - 再加查询时切面组合；
    - 再加 Agentic 覆盖补齐。

## 16. 用于验证的目标

固定 snapshot、Profile 和模型，在**不把 c13-003 词项或目标 block 写入通用提示词**的前提下：

1. 完成术语、概念义项、功能关系和证据编译；
2. 直接查询 `AI Agent 隔离与控制边界`，不提供 sandbox 等 seed；
3. 检查 7 组核心证据覆盖、扩展证据覆盖和噪声；
4. 对比预编译 Group 路径；
5. 统计 core 7/7 的次数、证据 precision 和无关 token；
6. 若失败，只允许根据通用功能模型和检索参数调整，不能为 c13 写特例。

## 17. 当前结论

这轮讨论最重要的结论是：

> **预编译不应主要编译“用户可能怎么问”，而应编译“知识中的术语、义项、领域无关的
> 功能关系和证据”。**

有限预编译仍然存在，但不再体现为有限的问题视角集合，而体现为一套可组合的功能语义语法。

当前最准确的总判断是：

> **Predicate generalization offline + domain semantics binding online.**
>
> 通用谓词离线泛化，领域语义在线晚绑定。

其中，领域无关的 Functional Relation 是关键一跳：

- 它把查询语言和实现语言连接起来；
- 它让“隔离／边界”可以从具体机制中被召回；
- 它减少对预先领域分类的依赖；
- 它使领域谓词成为查询时视图，而不是离线 schema；
- 但它不能替代证据、覆盖判断和必要的领域先验。

因此，Term 仍然必要，但不再是方法核心；Concept Sense 仍然必要，但主要作用是给功能
关系提供明确的参与者；Functional Assertion 才是整个检索语义层的关键。

最终目标不是证明“任意角度都能绝对答全”，而是把完整性从：

> 预编译是否猜中用户分类

转化为：

> 术语、概念、功能关系和覆盖审查是否足够完整、可解释、可回归。
