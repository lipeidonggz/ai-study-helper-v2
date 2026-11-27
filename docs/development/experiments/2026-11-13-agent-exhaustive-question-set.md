# 基于当前 15 篇知识库文档的找全类问题集

## 0. 本文目的与使用边界

本文不是让模型凭领域常识猜“一个主题可能包含哪些方面”，而是从当前知识库的真实原文中，反向设计用于测试“找全能力”的问题集。

“找全类”在这里指：一个合格答案必须覆盖多个彼此不能由同一近义关键词直接命中的功能方向，并能把这些方向组织成一条完整关系链。只命中一个标题、一个高相似段落或几组重复表述，不能视为找全。

本文中的答案要点有两种来源标记：

- `[原文]`：该要点可由所列原文块直接支持。
- `[综合]`：该要点由多个原文块共同推出，推理范围没有超出原文表达。

本文只提供参考答案要点和证据定位，不要求答案复述本文措辞。后续评测应允许同义表达、不同顺序和合理的额外补充。

### 0.1 固定快照

- collection snapshot：`s_6b787258ba7d700c`
- 文档数：15
- 原文块数：526
- 快照根目录：`C:\Users\lida\AppData\Local\com.lipeidong.aistudyhelper\collections\s_6b787258ba7d700c`

下文用 `D01`—`D15` 指代文档，用 `b00003` 这类短号指代块。短号与完整块 ID 的对应关系固定为：

```text
<doc_id>#v0001#c1#<block_short_id>
```

例如，`D03 b00003` 的完整块 ID 是：

```text
doc_6c0a83dc425349d6ac08a20fba5ab94f#v0001#c1#b00003
```

### 0.2 文档索引

| 代号 | 文档标题 | doc_id | 块数 |
| --- | --- | --- | ---: |
| D01 | How we built our multi-agent research system | `doc_7f36f426b84f40eb9439acaad4949e32` | 24 |
| D02 | Effective context engineering for AI agents | `doc_a0ffdc255fe64a00b41f69a95dfbbf1b` | 18 |
| D03 | Effective harnesses for long-running agents | `doc_6c0a83dc425349d6ac08a20fba5ab94f` | 17 |
| D04 | How we contain Claude across products | `doc_fd4dec9976e041308e2b39ed797c75d1` | 26 |
| D05 | Harness engineering: leveraging Codex in an agent-first world | `doc_360a240d79e04c7397bdd2ebea093b03` | 19 |
| D06 | New tools for building agents | `doc_791cd26200c247ac92e2dfeece9ff11d` | 19 |
| D07 | LLM Powered Autonomous Agents | `doc_62940728641f4ecaad2bd104cc6b6ea5` | 44 |
| D08 | Harness Engineering for Self-Improvement | `doc_23f78d4aa7c1444ab4d4a48c96bf6736` | 39 |
| D09 | 2025: The year in LLMs | `doc_4953bd8d2e2d433bb2a523cc28636841` | 38 |
| D10 | Agents | `doc_f8694a12cc744187ae86b4efb7b284c2` | 44 |
| D11 | Patterns for Building LLM-based Systems & Products | `doc_59ef1806a7424ef3bfe7f54257657402` | 74 |
| D12 | Your AI Product Needs Evals | `doc_92d53ce1440f4ff69a60ddd7325d91e2` | 34 |
| D13 | AI Evals: Everything You Need to Know | `doc_626ac52197294086a1b2ba3eb0ee7d8d` | 74 |
| D14 | Building Effective AI Agents | `doc_3683c99bf98342cf958ba8b9384f2008` | 22 |
| D15 | Best practices for Claude Code | `doc_983ba4e2cbff4185873d525a69195fdf` | 34 |

## 1. 如何设计和运行一个能够在无人持续监督下完成数小时编码任务的 Agent？

### 1.1 问题

如何设计和运行一个能够在无人持续监督下完成数小时编码任务的 Agent？

### 1.2 为什么属于找全类

合格答案必须同时覆盖跨会话的任务连续性、无人持续监督下的执行边界、失败后的持久恢复，以及最终状态和关键节点的验证。只回答 harness 生命周期，或只回答沙箱和权限控制，都只能覆盖一个局部方向。

### 1.3 参考答案要点与来源

| 方面 | 参考答案要点 | 来源 |
| --- | --- | --- |
| 长期任务形态 | `[原文]` 编码 Agent 需要跨多个上下文窗口完成持续数小时甚至数天的复杂任务，而每个新会话开始时没有前一会话的记忆。 | D03 `b00001`；“The long-running agent problem” |
| 初始化与功能全集 | `[原文]` 第一个会话建立启动脚本、进度文件和 git 基线；同时在开始时把高层需求展开成完整、可验证的功能清单，所有功能先标为 failing。 | D03 `b00003`、`b00005`；“The long-running agent problem”“Environment management > Feature list” |
| 清单稳定性 | `[原文]` 后续 Agent 只修改功能的通过状态，不删除或重写测试；结构化 JSON 比 Markdown 更不容易被误改。 | D03 `b00007`；“Environment management > Feature list” |
| 增量执行与交接 | `[原文]` 每轮只推进一个功能，结束后留下描述性 git commit、进度摘要和可合入主分支的干净状态，使下一轮能快速理解并继续工作。 | D03 `b00003`、`b00008`；“The long-running agent problem”“Incremental progress” |
| 会话恢复 | `[原文]` 每轮先确认工作目录，读取进度文件和 git 日志，选择最高优先级的未完成功能；启动服务并跑基础端到端测试，发现上一轮遗留的断点后再继续。 | D03 `b00010`、`b00013`；“Getting up to speed” |
| 无人运行边界 | `[原文]` 逐步审批会因审批疲劳而失效；应优先在环境层用沙箱、VM、文件系统边界和出站控制限制 Agent 能做什么，使紧密边界内的 Agent 可以在没有逐步审批的情况下运行。 | D04 `b00002`、`b00004`、`b00008`；“Three types of risk, three components of defense”“Patterns for containing agents” |
| 安全取舍 | `[原文]` 应先在环境层设计 containment，再在模型层引导行为；隔离强度应与用户理解和监督 Agent 的能力相匹配，并用经过验证的成熟原语建立确定性边界。 | D04 `b00023`；“Summary” |
| 持久执行与容错 | `[原文]` 长时运行的 Agent 有状态且错误会累积；失败后不能全部重跑，应支持断点恢复，并结合模型适应性、确定性重试和定期 checkpoint。 | D01 `b00016`；“Production reliability and engineering challenges” |
| 完成验证 | `[原文]` 不能只依赖单元测试或 curl，应像真实用户一样做端到端测试；有状态多轮任务还应验证最终状态和关键 checkpoint，避免仅按固定过程或过早声明完成。 | D03 `b00009`、`b00013`；D01 `b00022`；“Environment management > Testing”“Getting up to speed”“Appendix” |

### 1.4 覆盖关系

`[综合]` 这条链是“长期任务连续性 → 环境层安全边界 → 单功能增量执行 → 持久恢复 → 最终状态和关键节点验证”。功能清单负责定义完成范围，沙箱和确定性边界负责限制无人运行时的潜在破坏，git、进度文件和 checkpoint 负责跨会话与故障恢复，端到端测试和最终状态评测负责防止 Agent 过早宣布完成。少掉任一层，都可能把“运行了数小时”误当成“已经在无人监督下可靠完成了任务”。

### 1.5 与普通 Agentic RAG 的区分度

如果不使用 14 号设计中的通用关系索引，主 Agent 仍然可以通过普通 Agentic RAG 处理本题。典型流程是：判断问题属于开放式、多方面的复合问题；利用模型自有知识拆成若干子问题；为每个子问题生成检索表达；通过向量、BM25 或文档主题分组召回原文块；阅读证据后继续补查；最后汇总答案并反思可能缺少的方面。

对于本题，主 Agent 可能拆出跨会话连续性、无人监督下的安全边界、故障恢复和完成验证等问题，并生成 `long-running agent`、`progress file`、`git history`、`unattended`、`sandbox`、`permission approval`、`checkpoint`、`resume`、`state evaluation` 等检索表达。它很可能先通过标题命中 D03，再通过无人监督和沙箱相关表达命中 D04，通过状态、恢复和评测相关表达命中 D01。因此，一个能力足够强的模型配合普通混合检索，有可能较好地回答本题。

这种路径的语义桥接完全发生在查询时。“无人持续监督”能否连接到 `sandbox` 和 `permission`，“中断后继续”能否连接到 `checkpoint` 和 `resume`，都取决于模型这一次是否想到这些具体表达。不同模型、不同措辞和不同轮次可能生成不同检索词；即使已经召回大量相关块，也无法证明每个必要方面都已被覆盖。模型事后判断“应该差不多了”不能替代候选证据覆盖核对。

14 号设计的区别是预先保存“原文块＋功能关系＋概念义项＋证据来源”，查询时把无人监督稳定映射到分离、约束和保护等关系，再枚举满足关系与范围的证据。这样不必完全依赖模型临时想到某个机制词，也能显式报告哪些关系已经找到、哪些仍缺失。

但对本题需要保持客观：它可能不足以明显区分普通 Agentic RAG 和 14 号设计。题面机制较常见，D03 又与题面高度匹配，强模型大概率可以通过拆解和查询扩展找到主要证据。因此，本题更适合作为端到端可用性回归题，而不是通用关系索引价值的唯一判别题。更能体现该索引独特价值的题目应同时具备以下特征：题面不出现具体机制词；正确证据分散在多篇文档；不同机制使用不同术语；需要按一个横切功能视角连续找全；普通关键词、标题和 Top-K 向量检索容易漏掉其中一部分。

## 2. 会执行代码、读写文件并访问外部工具的 Agent，应如何设计完整的安全边界？

### 2.1 问题

设计一个能执行代码、访问文件系统、调用网络或第三方工具的 Agent。请完整说明威胁来源、防御层、隔离模式、外部内容信任、人在回路以及残余风险。

### 2.2 为什么属于找全类

答案需要同时考虑恶意用户、模型自身行为和外部攻击，并把环境隔离、模型约束、工具权限、外部内容检查、用户监督和部署代价连成多层防线。只回答“加沙箱”或“加系统提示词”都不是完整方案。

### 2.3 参考答案要点与来源

| 方面 | 参考答案要点 | 来源 |
| --- | --- | --- |
| 威胁分类 | `[原文]` 风险至少包括用户滥用、模型误行为、外部攻击者；能力更强的模型也可能绕开没人写明的限制来完成任务。 | D04 `b00003`；“Three types of risk, three components of defense” |
| 环境层 | `[原文]` 用进程沙箱、VM、文件系统边界和出站控制限制 Agent 能到哪里、能做什么；凭证不进入沙箱，即使模型或攻击者诱导也无法轻易外传。 | D04 `b00004`；“Three types of risk, three components of defense” |
| 模型层 | `[原文]` 系统提示、分类器、探针和训练调整只能改变倾向，不能给出理论上的硬边界，所以不能单独承担安全。 | D04 `b00004`；“Three types of risk, three components of defense” |
| 外部内容层 | `[原文]` MCP、插件、网页和工具返回值都可能把不可信内容送入上下文；连接器经过审计不等于它读到的数据可信。应限制工具权限和爆炸半径。 | D04 `b00005`；“Three types of risk, three components of defense” |
| 隔离模式选择 | `[原文]` 可采用临时容器、人在回路沙箱或本地 VM；隔离强度应匹配用户的检查能力。过强会制造审批疲劳，过弱会把不可理解的操作交给非专家。 | D04 `b00007`、`b00008`、`b00009`、`b00018`；“Patterns for containing agents” |
| 文件与路径 | `[原文]` 文件挂载可分为只读、读写、读写但不可删除；符号链接必须在路径校验前解析，否则授权目录内的链接可逃逸。 | D04 `b00014`；“Pattern 3: The local VM” |
| 工具与来源信任 | `[原文]` 本地工具可审计、可固定版本；远程工具可能在批准后改变行为。工具输出也应按不可信输入处理，并在进入模型前检查。 | D04 `b00019`、`b00020`；“Trusting what the agent reads” |
| 运行时保护 | `[原文]` 计算机操作型 Agent 需要提示注入检查、敏感操作确认、环境隔离、违规检测，并保留人工监督；基准成功率仍说明其不可靠。 | D06 `b00013`；“Computer use” |
| 设计原则 | `[原文]` 先做环境层 containment，再做模型层引导；匹配隔离与用户监督能力；优先使用成熟的容器、虚拟化和系统调用过滤原语。 | D04 `b00023`；“Summary” |

### 2.4 覆盖关系

`[综合]` 完整防线不是单点控件，而是“识别威胁 → 硬隔离环境和数据流 → 限制工具能力 → 检查外部内容 → 用模型策略降低误动作 → 保留可理解的人审 → 接受残余风险”。环境层负责确定性下限，模型层负责概率性拦截，工具权限决定一旦失守时的爆炸半径。

## 3. 什么情况下应使用多智能体研究系统？它带来什么收益、代价和可靠性问题？

### 3.1 问题

请说明多智能体研究系统适合什么任务、典型架构是什么、相对于单 Agent 的收益来自哪里，以及 token 成本、协调、同步执行、评测和部署方面有哪些限制。

### 3.2 为什么属于找全类

答案必须同时覆盖适用任务、架构、性能来源、经济成本、任务不适用条件、状态可靠性和评测方式。只说“多个 Agent 并行更快”会漏掉 15 倍 token 成本、同步瓶颈、共享上下文任务不适配和端到端状态评测。

### 3.3 参考答案要点与来源

| 方面 | 参考答案要点 | 来源 |
| --- | --- | --- |
| 适用任务 | `[原文]` 开放式研究无法预设固定步骤，需要根据中间发现不断改方向；多 Agent 尤其适合同时探索多个独立方向的 breadth-first 查询。 | D01 `b00002`、`b00003`；“Benefits of a multi-agent system” |
| 架构 | `[原文]` 典型结构是 orchestrator-worker：lead Agent 分析问题、保存计划、创建专门 subagent，并行探索不同方面，最后综合并交给 Citation Agent 定位引用。 | D01 `b00006`、`b00007`；“Architecture overview for Research” |
| 性能来源 | `[原文]` subagent 用独立上下文并行探索并压缩结果，减少路径依赖；内部 breadth-first 研究中，多 Agent 比单 Agent 基线的成绩高 90.2%。 | D01 `b00002`、`b00003`；“Benefits of a multi-agent system” |
| 成本机制 | `[原文]` BrowseComp 分析中 token、工具调用数和模型选择解释 95% 的方差，其中 token 本身解释 80%；架构的收益很大程度来自获得更多并行 token 预算。 | D01 `b00004`；“Benefits of a multi-agent system” |
| 经济代价 | `[原文]` Agent 约为聊天的 4 倍 token，多 Agent 约为 15 倍。只有任务价值足够高、确实可并行、信息量超过单窗口或工具复杂时才值得。 | D01 `b00005`；“Benefits of a multi-agent system” |
| 不适用条件 | `[原文]` 需要所有 Agent 共享同一上下文或依赖很强的领域不适合；多数编码任务真正可并行的部分少于研究，实时协调和委派也仍困难。 | D01 `b00005`；“Benefits of a multi-agent system” |
| 资源分配 | `[原文]` 应按问题复杂度规定 effort：简单事实、比较问题、复杂研究使用不同数量的 Agent 和工具调用，避免在简单问题上过度投入。 | D01 `b00010`；“Prompt engineering and evaluations for research agents” |
| 状态可靠性 | `[原文]` Agent 有状态且错误会累积，需要持久执行、恢复、重试和 checkpoint；同步等待 subagent 会形成信息瓶颈，异步化又增加结果协调和状态一致性复杂度。 | D01 `b00016`、`b00018`；“Production reliability and engineering challenges” |
| 评测 | `[原文]` 不同运行可以走不同有效路径，因此不能只检查预设步骤；应看最终结果与过程是否合理。改变持久状态的任务更适合 end-state evaluation 和离散 checkpoint。 | D01 `b00013`、`b00022`；“Effective evaluation of agents”“Appendix” |

### 3.4 覆盖关系

`[综合]` 适用性判断是“开放式、可分解、可并行、任务价值高”；架构收益来自独立上下文的并行探索和信息压缩；成本来自 token 和协调；可靠性要求持久执行、同步/异步权衡与结果导向评测。如果任务强依赖共享状态或规模不足以支付成本，多 Agent 反而会放大问题。

## 4. 长任务 Agent 如何管理有限上下文？

### 4.1 问题

长任务 Agent 如何管理有限上下文？

### 4.2 为什么属于找全类

一个完整答案必须解释“为什么要压缩”“压缩保留什么”“什么可以丢弃”“跨会话状态放哪里”“何时用独立子上下文”，不能只给一个 context engineering 定义。

### 4.3 参考答案要点与来源

| 方面 | 参考答案要点 | 来源 |
| --- | --- | --- |
| 根本问题 | `[原文]` 上下文增长会出现 context rot，召回和长程推理精度下降；上下文是有限资源，存在递减的注意力预算。 | D02 `b00003`、`b00004`；“Why context engineering is important” |
| 优化目标 | `[原文]` 不是无条件塞入更多信息，而是找到“最小的高信号 token 集合”，最大化期望结果。 | D02 `b00005`；“The anatomy of effective context” |
| compaction | `[原文]` compaction 总结消息历史，保留架构决策、未解决 bug 和实现细节，丢弃冗余工具输出；可附带最近访问文件。 | D02 `b00013`；“Context engineering for long-horizon tasks” |
| 压缩取舍 | `[原文]` 过度压缩会丢失后来才体现重要性的细节；应先追求高召回，再迭代提高精确率。 | D02 `b00013`；“Context engineering for long-horizon tasks” |
| 工具结果清理 | `[原文]` 很久以前调用过、后续不再需要的工具原始结果通常可以清除，属于轻量 compaction。 | D02 `b00013`；“Context engineering for long-horizon tasks” |
| 结构化笔记 | `[原文]` Agent 把关键进度和依赖写入 `NOTES.md` 或外部记忆，上下文重置后再读回，支持跨工具调用和多小时任务。 | D02 `b00014`；“Context engineering for long-horizon tasks” |
| subagent | `[原文]` subagent 在干净上下文中完成专门调查，可消耗大量 token，但只把约 1,000—2,000 token 的浓缩结果交回主 Agent，实现关注点分离。 | D02 `b00015`；“Context engineering for long-horizon tasks” |
| 选择规则 | `[原文]` 需要大量来回交流时用 compaction；有清晰里程碑的迭代开发适合笔记；复杂研究需要并行探索时用多 Agent。 | D02 `b00015`；“Context engineering for long-horizon tasks” |
| 会话级记忆 | `[原文]` 长会话可在阶段完成时总结到外部记忆，接近上限时启动干净 subagent，再通过计划和 artifact 保持连续性。 | D01 `b00023`；“Appendix” |
| 文件化交接 | `[原文]` 大结果直接写入文件或 artifact，只把引用交回主 Agent，可减少多级转述导致的信息损失和 token 复制。 | D01 `b00023`；“Appendix” |

### 4.4 覆盖关系

`[综合]` 四种机制处理的是不同层次：compaction 管“当前对话怎么压缩”，笔记/文件管“状态怎么跨轮次保存”，subagent 管“调查过程怎么隔离”，工具清理管“历史中哪些重复证据可以丢”。它们可以组合，但不能互相替代。

## 5. 构建 Agentic 系统时有哪些工作流和 Agent 结构？应如何选择？

### 5.1 问题

请从最简单的增强型 LLM 出发，完整说明 prompt chaining、routing、parallelization、orchestrator-workers、evaluator-optimizer 和自主 Agent 的适用条件、取舍与组合原则。

### 5.2 为什么属于找全类

答案不能只列六个名称，还必须说明预定义工作流与自主 Agent 的边界、何时不值得引入 Agent、每个模式解决的控制问题，以及为什么“简单优先”并不等于永远不用 Agent。

### 5.3 参考答案要点与来源

| 方面 | 参考答案要点 | 来源 |
| --- | --- | --- |
| 基础区分 | `[原文]` Workflow 由预定义代码路径编排 LLM 和工具；Agent 由 LLM 动态决定过程与工具使用方式，两者同属 agentic systems。 | D14 `b00002`；“What are agents?” |
| 选择原则 | `[原文]` 从最简单方案开始，只在确有必要时增加复杂度；很多应用优化单次 LLM 调用加检索和示例就够了。 | D14 `b00003`、`b00015`；“When (and when not) to use agents”“Summary” |
| 基础构件 | `[原文]` augmented LLM 是基础块：模型加上检索、工具和记忆，并能主动搜索、选工具、决定保留什么。 | D14 `b00006`；“Building block: The augmented LLM” |
| Prompt chaining | `[原文]` 把任务拆成固定顺序步骤，前一步输出给下一步，可在中间加程序化 gate；适合能清晰分解的固定子任务。 | D14 `b00007`；“Workflow: Prompt chaining” |
| Routing | `[原文]` 先分类输入，再交给专门流程、提示或模型；适合类别清晰且分类可靠的复杂任务。 | D14 `b00008`；“Workflow: Routing” |
| Parallelization | `[原文]` 可做任务分片并行，也可让同一任务多次运行后投票；适合独立子任务、速度或多视角置信度需求。 | D14 `b00009`；“Workflow: Parallelization” |
| Orchestrator-workers | `[原文]` 中央模型根据输入动态拆解、委派并综合；适合无法预先知道子任务数量和性质的编码或搜索。 | D14 `b00010`；“Workflow: Orchestrator-workers” |
| Evaluator-optimizer | `[原文]` 一个模型生成，另一个按明确标准评价并循环改进；适合评价标准清楚、反馈能带来可测提升的任务。 | D14 `b00011`；“Workflow: Evaluator-optimizer” |
| 自主 Agent | `[原文]` Agent 在环境中多轮规划、调用工具、读取真实反馈，在 checkpoint 或阻塞时回到人；适合路径不可预测的开放任务。 | D14 `b00012`；“Agents” |
| 自主代价 | `[原文]` 自主性提高成本并可能累积错误，需要沙箱测试、guardrail 和停止条件；Agent 的具体实现往往就是“LLM + 工具 + 环境反馈循环”。 | D14 `b00012`、`b00013`；“Agents” |
| 实现边界 | `[原文]` 框架能简化工具调用和链式编排，但额外抽象会遮蔽 prompt/响应并诱导过度复杂；应理解底层，必要时直接用 API。 | D14 `b00004`；“When and how to use frameworks” |

### 5.4 覆盖关系

`[综合]` 选型顺序是：先看单次增强 LLM 是否足够；若任务结构固定，用最弱的 workflow 模式；只有路径无法预先确定、需要环境反馈时才升级到 Agent。复杂 Agent 可组合上述模式，但复杂度必须由可测收益证明。

## 6. 如何建立一套完整、可持续迭代的 AI 产品评测体系？

### 6.1 问题

请完整说明 AI 产品评测应包含哪些层级、如何写测试与看数据、如何做人工和模型评测、如何处理错误分析、A/B、CI、生产监控以及评测与 guardrail 的区别。

### 6.2 为什么属于找全类

完整体系跨越测试数据、判定方法、失败归因、运行环境、反馈闭环和上线保护。只回答“用 LLM-as-a-Judge”会漏掉测试范围、人工校准、失败分类、生产采样和同步 guardrail 的不同职责。

### 6.3 参考答案要点与来源

| 方面 | 参考答案要点 | 来源 |
| --- | --- | --- |
| 核心循环 | `[原文]` 快速迭代依赖三个过程：评测质量、调试问题、改变系统行为。三者形成循环，评测投入会降低其他环节成本。 | D12 `b00002`；“Iterating Quickly == Success” |
| 三层评测 | `[原文]` Level 1 是快速廉价的单元测试，Level 2 是人工和模型评测/调试，Level 3 是 A/B；成本和运行频率决定节奏。 | D12 `b00005`；“The Types Of Evaluation” |
| 测试范围 | `[原文]` 把 LLM 能力拆成 feature 和 scenario，每个场景用明确 assertion 验证；这些断言也可用于数据清洗和推理期纠错。 | D12 `b00006`、`b00007`、`b00008`；“Level 1: Unit Tests” |
| 轨迹与人工 | `[原文]` trace 应先以领域相关方式呈现，消除查看数据摩擦；人工标注可从简单 good/bad 开始，但应持续抽样检查。 | D12 `b00019`、`b00020`、`b00021`、`b00022`；“Level 2: Human & Model Eval” |
| 模型判官校准 | `[原文]` 模型评测要与人工判断对齐；类别不平衡时不要只看 raw agreement，应分别测 precision 和 recall，并持续监控一致性。 | D12 `b00023`；“Automated Evaluation w/ LLMs” |
| 错误分析 | `[原文]` 先收集代表性 trace，开放编码记录首个失败，再轴向编码形成 failure taxonomy，持续迭代到新样本不再暴露新类别。 | D13 `b00008`、`b00009`、`b00010`、`b00011`、`b00012`；“Error Analysis & Data Collection” |
| 二元判定 | `[原文]` pass/fail 比 1—5 分更容易稳定和行动；渐进改善可通过多个独立的二元子项衡量。 | D13 `b00025`；“Evaluation Design & Methodology” |
| CI 与生产 | `[原文]` CI 用小型、专门、可重复的测试集，优先确定性断言；生产端异步抽样真实 trace，更多依赖 reference-free 判官并跟踪置信区间。 | D13 `b00053`；“Production & Deployment” |
| evaluator 与 guardrail | `[原文]` guardrail 同步位于请求路径，负责即时阻断高影响错误；evaluator 通常异步评估正确性和完整性，不阻断当次回答。 | D13 `b00054`、`b00056`；“Production & Deployment” |
| Agent 两阶段评测 | `[原文]` 先做端到端任务成功，并记录首个上游失败；找到高频失败流程后，再评分工具选择、参数、错误恢复、上下文保持、效率和 checkpoint。 | D13 `b00073`；“Domain-Specific Applications” |
| 失败转移矩阵 | `[原文]` 用“最后成功状态 × 首个失败位置”的矩阵定位故障热点，比逐条 trace 更容易比较失败分布。 | D13 `b00074`；“Domain-Specific Applications” |
| 端到端状态 | `[原文]` 对会改变持久状态的多轮 Agent，应验证最终状态和关键 checkpoint，而不是强制每条运行走相同过程。 | D01 `b00022`；“Appendix” |

### 6.4 覆盖关系

`[综合]` 完整评测闭环是“定义场景和断言 → 运行 trace → 人工审查与错误分类 → 校准自动判官 → CI 回归 → 生产抽样监控 → 新失败回流测试集”。guardrail 是同步安全层，evaluator 是异步质量层，二者不能互相冒充。

## 7. 如何设计、评估并持续改进一个 RAG 系统？

### 7.1 问题

请完整说明 RAG 为什么存在、基本检索链如何构成、dense 与关键词检索的区别、ANN 索引如何取舍、怎样组合检索策略，以及如何分别评估检索和生成。

### 7.2 为什么属于找全类

答案要连接“业务目的、索引与表示、召回策略、延迟/内存取舍、生成使用证据、检索评测、生成评测”多个环节。只讲向量数据库或只讲 Recall@k 都无法回答系统如何落地。

### 7.3 参考答案要点与来源

| 方面 | 参考答案要点 | 来源 |
| --- | --- | --- |
| RAG 目的 | `[原文]` RAG 从模型外取回相关数据注入上下文，用于降低幻觉、提供更新数据和更容易增删资料，而不必持续重训模型。 | D11 `b00015`、`b00016`；“Retrieval-Augmented Generation” |
| 向量表示 | `[原文]` Embedding 把任意长度文本压缩为固定维度向量，相似内容在向量空间中更接近；其质量应以目标任务表现衡量。 | D11 `b00017`；“More about RAG” |
| Dense retrieval | `[原文]` DPR 用独立 query/passage 编码器学习内积排序，先用 passage 向量建索引，再对 query 取近似 top-k。 | D11 `b00018`；“More about RAG” |
| 混合检索 | `[原文]` 纯 embedding 对专名、缩写、ID 可能不足，关键词检索对同义词和上下位概念不足；二者组合通常优于单独使用。 | D11 `b00025`；“How to apply RAG” |
| 元数据 | `[原文]` 传统搜索索引可结合日期、分类、评分等元数据过滤和排序，embedding 不是唯一召回维度。 | D11 `b00025`；“How to apply RAG” |
| 冷启动查询表示 | `[原文]` 缺少 relevance labels 时，HyDE 先让 LLM 生成假设文档，再用其 embedding 检索真实文档，把相关性建模转为生成任务。 | D11 `b00024`；“More about RAG” |
| ANN 取舍 | `[原文]` ANN 用一点准确性换大量速度；LSH、FAISS、HNSW、ScaNN 各有结构，选择时需测 recall、延迟/吞吐、内存和增量更新难度。 | D11 `b00027`、`b00028`；“How to apply RAG”；D07 `b00014`、`b00015`、`b00016`；“Maximum Inner Product Search” |
| 选型原则 | `[原文]` 没有对所有场景都最好的索引；先定义功能和非功能要求，再用基准测量。 | D11 `b00029`；“How to apply RAG” |
| 检索评测 | `[原文]` RAG 评测要拆成 retrieval 和 generation；检索用 Recall@k、Precision@k、MRR 等 IR 指标，并可反向从文档事实生成 query-document 对。 | D13 `b00060`；“Q: How should I approach evaluating my RAG system?” |
| 关系评测 | `[原文]` 生成侧可检查 Context 与 Question 的相关性、Answer 对 Context 的忠实性、Answer 对 Question 的回答性，并补充领域特有失败指标。 | D13 `b00061`；“Q: How should I approach evaluating my RAG system?” |
| 调试顺序 | `[原文]` 先用 IR 指标调 retrieval，再处理 generation；LLM judge 要经过错误分析、标注样本，并测与人类判断的误差。 | D13 `b00061`；“Q: How should I approach evaluating my RAG system?” |

### 7.4 覆盖关系

`[综合]` RAG 的完整设计链是“明确要补的知识 → 选择表示和索引 → 组合语义/关键词/元数据召回 → 把充分证据放入上下文 → 分离评估 retrieval 与 generation → 按 failure taxonomy 回改索引、查询或提示”。如果 retrieval 没取到正确证据，生成侧优化只能修补表面症状。

## 8. Agent 的工具系统应覆盖哪些能力？如何选择、设计、使用和评估工具？

### 8.1 问题

请完整说明 Agent 工具的作用、工具类别、读写风险、工具清单规模、function calling、Agent-Computer Interface 设计、常见工具失败和效率指标。

### 8.2 为什么属于找全类

完整答案既包含“能给 Agent 什么能力”，也包含“怎样让模型正确使用”和“如何验证失败”。只列举搜索、代码解释器等工具，无法覆盖写操作风险、工具选择实验、参数错误、ACI 和信息缺失。

### 8.3 参考答案要点与来源

| 方面 | 参考答案要点 | 来源 |
| --- | --- | --- |
| 工具价值 | `[原文]` 没有外部工具，模型通常只能生成内容；工具让 Agent 能感知环境并作用于环境，显著扩展能力。 | D10 `b00006`；“Tools” |
| 能力分类 | `[原文]` 工具可提供知识增强、能力扩展和环境写操作；知识工具补私有或最新信息，能力工具补算术、代码、多模态等模型短板。 | D10 `b00006`、`b00007`、`b00008`、`b00009`；“Tools” |
| 读写风险 | `[原文]` 读操作获取信息，写操作改变环境；写权限更大，必须考虑安全、信任和用户授权，不能因模型“看起来能用”就自动给高风险写权。 | D10 `b00010`、`b00011`；“Write actions” |
| 工具清单规模 | `[原文]` 工具越多能力越强，但理解和高效选择越难，描述也会挤占上下文；必须通过实验寻找合适集合。 | D10 `b00006`、`b00036`；“Tools”“Tool selection” |
| 选择实验 | `[原文]` 比较不同工具集、做移除 ablation、定位经常用错的工具、统计调用分布；没有性能贡献的工具应删除或替换。 | D10 `b00037`；“Tool selection” |
| 数据驱动改进 | `[原文]` 可根据工具转移频率组合常用工具；成功的新技能可写入技能库，供未来任务检索复用。 | D10 `b00038`；“Tool selection” |
| Function calling | `[原文]` 先声明工具入口、参数和说明，再按查询限制可用工具；模型生成调用后必须检查实际参数，系统只能保证结构合法，不能保证值和意图正确。 | D10 `b00025`、`b00027`；“Plan generation > Function calling” |
| ACI 设计 | `[原文]` 工具说明应像优秀的人机接口：给示例、边界、输入格式、与其他工具的区分，并测试模型会怎样误用。 | D14 `b00020`、`b00021`；“Appendix 2: Prompt engineering your tools” |
| 防错设计 | `[原文]` 通过改参数和接口降低误用，例如强制绝对路径；工具格式应接近模型熟悉的自然文本，减少转义和行数计算等额外负担。 | D14 `b00020`、`b00021`；“Appendix 2” |
| 规划失败 | `[原文]` 常见失败包括调用不存在的工具、参数结构错误、参数值错误、计划不达目标、误解完成或忽略时间约束。 | D10 `b00040`、`b00041`；“Planning failures” |
| 工具失败 | `[原文]` 工具即使被正确调用也可能返回错误结果，翻译层也可能出错；每个工具都必须独立测试，并记录调用和输出。 | D10 `b00042`；“Tool failures” |
| 缺失工具 | `[原文]` 如果某领域持续失败，可能是工具清单缺失；应观察领域专家实际怎样工作，识别需要补充的工具。 | D10 `b00042`；“Tool failures” |
| 效率 | `[原文]` 正确完成也可能过慢或过贵；应记录步骤、成本、动作延迟，并与 Agent 或人工基线比较。 | D10 `b00043`；“Efficiency” |

### 8.4 覆盖关系

`[综合]` 工具系统的完整设计是“能力覆盖 → 最小充分清单 → 清晰 ACI → 结构合法和参数值校验 → 独立工具测试 → 规划/工具/效率失败归因 → 按证据增删和组合工具”。更多工具并不天然更好，缺少关键工具也会让 Agent 从起点就无法完成任务。

## 9. 如何把大型软件仓库、运行环境和验证链路改造成适合 Agent 长期自主开发的形式？

### 9.1 问题

请从仓库知识、应用可观测性、执行环境、验证、架构约束、并行会话和漂移治理等方面，完整说明怎样让大型软件仓库适合 Agent 自主开发。

### 9.2 为什么属于找全类

答案不只是“写 AGENTS.md”。它必须覆盖 Agent 能看到什么、如何运行、如何验证、如何避免架构漂移、多个会话如何隔离，以及如何持续清理自动生成代码产生的熵。

### 9.3 参考答案要点与来源

| 方面 | 参考答案要点 | 来源 |
| --- | --- | --- |
| 应用可读性 | `[原文]` 把 UI、日志、指标和 trace 直接暴露给 Agent，并让每个 git worktree 可独立启动应用；Agent 才能复现、修复并验证行为。 | D05 `b00005`、`b00006`；“Increasing application legibility” |
| 仓库入口 | `[原文]` `AGENTS.md` 应是约百行的目录和入口，而不是千页说明书；把稳定知识拆到结构化 `docs/`，避免上下文挤占、失效和不可验证。 | D05 `b00007`；“We made repository knowledge the system of record” |
| 系统记录 | `[原文]` 设计、架构、质量评分、计划、进度、决策和技术债都进入版本库；Agent 通过小入口逐步展开，而不依赖外部聊天或人的记忆。 | D05 `b00008`、`b00009`；“We made repository knowledge the system of record”“Agent legibility is the goal” |
| 可验证知识 | `[原文]` 用专用 linter 和 CI 检查知识库是否更新、交叉链接和结构正确；定期 doc-gardening Agent 扫描过时文档并提修复 PR。 | D05 `b00008`；“We made repository knowledge the system of record” |
| 环境脚手架 | `[原文]` 长期编码任务需要初始化功能清单、进度文件、git 历史和可重复启动脚本，让每个新窗口快速获得已有状态。 | D03 `b00003`、`b00010`；“The long-running agent problem”“Getting up to speed” |
| 验证链路 | `[原文]` 验证标准要明确；可运行测试、截图比较、goal evaluator、确定性 Stop hook 或独立 verification subagent 都能减少“模型自评自己”。 | D15 `b00001`、`b00002`；“Give Claude a way to verify its work” |
| 架构约束 | `[原文]` 文档不能保证一致性；应把关键不变量和规则编码进边界解析、schema、linter、测试和工具，而不是微观控制每条实现。 | D05 `b00011`、`b00012`；“Enforcing architecture and taste” |
| 人机分工 | `[原文]` 人负责确定优先级、把用户反馈转成验收条件并验证结果；Agent 可在工具、guardrail、文档不足时给出信号，由 Agent 本身补工具和修复。 | D05 `b00014`；“What “agent-generated” actually means” |
| 并行会话 | `[原文]` 独立 git worktree 可避免多个 CLI 会话互相覆盖；多会话还可以做 writer/reviewer 分工和跨会话消息。 | D15 `b00025`、`b00026`；“Automate and scale > Run multiple Claude sessions” |
| 漂移治理 | `[原文]` Agent 会复制已有好坏模式，导致架构漂移；应把 golden principles 机械化为检查，并用周期性后台任务发现偏差、更新质量评分和提交小修复。 | D05 `b00016`；“Entropy and garbage collection” |
| Harness 自改进 | `[原文]` harness 应使用 plan—execute—observe/test—improve 循环；文件保存持久状态，subagent/后台任务并行，且输出必须 inspectable。 | D08 `b00003`、`b00004`、`b00005`；“Harness Design Patterns” |

### 9.4 覆盖关系

`[综合]` Agent-first 仓库的本质是“让 Agent 能看见、能运行、能验证、能恢复、能被约束”。知识文档提供导航，应用可观测性提供真实反馈，工具和 worktree 提供执行边界，测试与 linter 提供验证，周期性治理阻止自动生成代码把局部坏模式扩散成系统熵。

## 10. 生产环境中的 Agent 为什么容易失败？如何做到可观测、可恢复、可验证和可升级？

### 10.1 问题

请完整说明生产 Agent 的主要失败机制、持久执行与恢复、trace 与隐私、同步/异步架构、部署升级、结果验证、上游失败定位和对抗式复核。

### 10.2 为什么属于找全类

答案必须跨越运行时、状态、观测、评测、部署和组织验证。只说“加日志”不够；只看最终答案也不足以解释多轮状态和运行中版本升级。

### 10.3 参考答案要点与来源

| 方面 | 参考答案要点 | 来源 |
| --- | --- | --- |
| 失败机制 | `[原文]` Agent 有状态，小系统错误会在多次工具调用中累积；复杂任务还会产生规划、工具和效率失败。 | D01 `b00016`；“Production reliability and engineering challenges” |
| 持久执行 | `[原文]` 不能失败后全部重跑；应持久执行、从断点恢复，并结合确定性 retry、checkpoint 与模型对工具失败的适应。 | D01 `b00016`；“Production reliability and engineering challenges” |
| 可观测性 | `[原文]` 同提示也可能产生不同轨迹；生产 trace 能区分搜索词、来源选择、工具失败等原因，还应观察决策模式和交互结构。 | D01 `b00017`；“Production reliability and engineering challenges” |
| 隐私 | `[原文]` 可观测性可以关注决策结构和交互模式，而不采集私聊内容本身。 | D01 `b00017`；“Production reliability and engineering challenges” |
| 部署升级 | `[原文]` 运行中的 Agent 可能停在任意状态；部署不能同时切换所有版本，应采用新旧并行、逐步切流的 rainbow deployment。 | D01 `b00017`；“Production reliability and engineering challenges” |
| 编排瓶颈 | `[原文]` 同步执行便于协调，但会被单个 subagent 阻塞；异步增加并行，也增加结果协调、状态一致和错误传播复杂度。 | D01 `b00018`；“Production reliability and engineering challenges” |
| 多 Agent trace | `[原文]` 每个用户请求分配统一 trace/session ID，记录每条消息的来源、顺序和工具，才能跨 Agent 重建完整路径。 | D13 `b00067`；“Multi-agent trace logging” |
| 首个上游失败 | `[原文]` debug 时先看全链路是否达成目标，再定位第一个上游失败；下游错误常由上游级联，优先修最早根因。 | D13 `b00066`、`b00068`；“Q: How do I debug multi-turn conversation traces?” |
| 状态与过程指标 | `[原文]` 多步工作流要记录 LLM、工具、人工审批和数据库写入；同时测结果和过程，按阶段切分，并用转移矩阵定位热点。 | D13 `b00072`；“Q: How do I evaluate complex multi-step workflows?” |
| 端到端判定 | `[原文]` 改变持久状态的 Agent 应看最终状态，并在复杂流程中验证关键 checkpoint，而不是只奖励某条固定过程。 | D01 `b00022`；“Appendix” |
| 系统级验证 | `[原文]` 把验证条件写进 prompt，在会话中用 goal evaluator，或用确定性 Stop hook 阻止错误完成；连续阻塞要有上限。 | D15 `b00002`；“Give Claude a way to verify its work” |
| 独立复核 | `[原文]` 用 fresh subagent 或 dynamic workflow 尝试反驳结果，让执行者不是唯一评分者；adversarial review 应报告缺口而非风格偏好。 | D15 `b00002`、`b00032`；“Give Claude a way to verify its work”“Add an adversarial review step” |
| 自修正与工具循环 | `[原文]` 成功的生产 Agent 可从错误、工具结果和代码执行中迭代；推理模型结合工具能规划、执行、读取结果并更新计划。 | D09 `b00003`；“The year of “reasoning”” |

### 10.4 覆盖关系

`[综合]` 可靠性闭环是“状态化执行 → 检查点和恢复 → 全链路 trace → 首个上游失败定位 → 结果和 checkpoint 验证 → 独立对抗复核 → 运行中安全升级”。可观测性负责解释，持久执行负责恢复，评测负责判断，部署策略负责避免修复动作本身造成新的运行中断。

## 11. 组卷检查

### 11.1 覆盖检查

| 题目 | 主要能力维度 | 主要来源 |
| --- | --- | --- |
| Q1 | 长时编码 harness、需求、测试、恢复 | D03 |
| Q2 | 威胁模型、隔离、权限、外部内容、人审 | D04、D06 |
| Q3 | 多 Agent 适用性、架构、成本、可靠性 | D01 |
| Q4 | 上下文退化、压缩、记忆、subagent | D01、D02 |
| Q5 | workflow/Agent 选型、组合和复杂度边界 | D14 |
| Q6 | 评测层级、数据、错误分析、CI/生产 | D12、D13、D01 |
| Q7 | RAG 索引、召回、ANN、分层评测 | D11、D13、D07 |
| Q8 | 工具能力、ACI、调用、失败和效率 | D10、D14 |
| Q9 | Agent-first 仓库、可观测性、治理和自改进 | D03、D05、D08、D15 |
| Q10 | 生产失败、trace、恢复、验证和升级 | D01、D09、D13、D15 |

### 11.2 防止“伪找全”的判定

- 答案只覆盖同名概念、同一文章或同一段落的多个重复表述，不算找全。
- 答案只列名词而没有说明这些方面如何共同构成一条可实现、可验证的链，不算找全。
- 答案中的额外内容若不能由所列原文块支持，应标为模型外部知识；在“仅基于知识库”的评测模式中，不应计入文档证据覆盖。
- 对“合理综合”可以接受不同组织方式，但底层事实必须落到所列 `doc_id + block_id`。
- 每道题都应至少出现两个来源文档，或出现同一文档中三个以上不同章节；否则该题更容易退化成单段检索题。

## 12. 来源完整性说明

本文所有块号均按活动快照 `s_6b787258ba7d700c` 的 `v0001` 原文校验。参考答案只使用 `[原文]` 标记支撑的事实；`[综合]` 只负责连接这些事实，不补充新的领域定义、产品参数或外部研究结论。
