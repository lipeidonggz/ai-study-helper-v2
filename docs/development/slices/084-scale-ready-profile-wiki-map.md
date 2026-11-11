# 084 · Profile Wiki 地图规模化就绪改造

- 状态：P0 实现完成，待 B 级决策确认与 50 篇规模验证
- 开始日期：2026-11-06
- 关联设计：[13 Profile-Driven LLM Wiki](../..//design/13-profile-driven-wiki-evidence-retrieval.md)
- 前置切片：[083 search_wiki_topic_v2 最小闭环](083-c13-minimal-wiki-closed-loop.md)
- 人工验收：先确认当前 15 篇无回归，再批准逐级扩大知识库规模

## 控制契约

### 背景与失败场景

- 触发问题：083 已证明主检索路径，但 topic 是 candidate 的一对一投影，group 构建依赖固定顺序 48-batch，查询每次重算全部 group embeddings，地图容量与查询窗口没有分离，运行时也没有 active map／selected Profile 约束。
- 不处理时的用户损失：知识库扩大后重复 topic 和单文档 group 会快速累积；地图在 512 groups／2,000 topics 时直接失败；查询成本随地图规模增长；Agent 可能读取其他 Profile 的最新实验地图。
- 现有流程为什么不足：现有实现为 15 篇、801 topics、204 groups 定制的完整查询和容量假设，不能作为规模化验证基础。
- 本切片负责什么：完成 GAP-01～07、GAP-10；先在当前 15 篇回归，再建立可控制扩大的规模探针。
- 本切片不负责什么：不解决 GAP-08、09、11、12、13；不证明 1,000 篇最终质量；不生成最终答案；不实现正式 Wiki 页面体系。
- 什么情况下应停止或撤掉机制：跨文档 topic 合并无法保持 candidate evidence 可追溯；group 构建无法保证全 topic 覆盖；查询仍需要读取或 embedding 全地图；当前 15 篇核心证据覆盖出现不可接受回归。

### 用户可观察目标

1. 同一 knowledge base 中，多个文档的 candidate 可以形成跨文档 Topic，查询诊断能从 Topic 回到全部 source candidates 和原文 blocks。
2. Group 基于合并后的 Topics 构建；实际允许形成跨文档 group，且每个 Topic 至少属于一个最终 group。
3. Query 的 embedding 输入规模只与查询和 group 候选窗口有关，不再为地图中每个 group 重新调用 embedding provider。
4. 地图总 group 数不再受 512 的业务上限限制。
5. Agent 只使用与当前 snapshot 和 selected Profile 对应的 active map。
6. 优化后当前 15 篇仍满足 c13 证据门禁。

### 核心不变量

1. Candidate 必须继续绑定原文档、版本和真实 evidence；Topic 只聚合 candidate，不替代或篡改原文。
2. 每个 candidate 必须且只能归入一个 Topic；每个 Topic 必须进入至少一个 final group。
3. Group 只能引用当前地图中的 Topic；group 的 source documents 必须等于 child topics 的来源文档并集。
4. 在线查询不得重新 embedding 全部地图 groups；使用构建时持久化的 group／topic vectors，并只 embedding 当前 query。候选 block vectors 必须按 block ID 从当前 snapshot 索引读取。group／topic／block 数量上限只负责异常资源保护，不得作为相关性选择目标。
5. Active map 必须同时匹配 snapshot 与 selected Profile；不匹配时显式不可用，不静默回退。

### 错误代价与非对称门禁

| 错误类型 | 具体例子 | 用户影响 | 处理分类 | 指标与阈值 |
| --- | --- | --- | --- | --- |
| Candidate 丢失或重复归属 | Topic 合并后 candidate 数减少 | 无法回源，证据链破坏 | 阻断 | 输入 candidate 集合与 Topic candidate_ids 全等 |
| 跨文档 Topic 错并 | 上下位、成员或不同粒度合并 | 证据污染且难恢复 | 阻断 | 单元探针中这些关系必须保持分开 |
| Topic 未进入 group | final manifest 出现 orphan topic | 部分知识不可检索 | 阻断 | orphan topic=0 |
| Group 来源不一致 | source_doc_ids 与 child topics 不符 | 诊断和跨文档判断错误 | 阻断 | 不一致=0 |
| 地图无持久化向量 | 查询回退到全量 embedding | 规模扩大后成本和延迟失控 | 阻断 | 查询 group embedding provider 调用=0 |
| Active map 错配 | selected Profile 与地图 Profile 不同 | Agent 使用错误领域规则 | 阻断 | 错配读取=0 |
| 固定配额调参过拟合 | 为让单个问题通过反复调整 group／topic／block 数量 | 参数只适配调参样本 | 阻断 | 固定地图下做跨宽窄主题、多次运行消融；不得仅凭单样本调整 |
| 当前 15 篇回归 | c13 核心组漏项 | 优化牺牲已成立效果 | 阻断 | 连续 3 次核心 7/7、扩展≥3/4 |

### 明确禁止

- 不得删除 candidate 的文档绑定或 evidence provenance。
- 不得把 Topic summary 当事实证据返回。
- 不得通过固定顺序 batch 作为最终全局 Topic／Group 归并策略。
- 不得在查询时重新计算地图全部 group embeddings。
- 不得静默忽略不兼容的 active map。
- 不得为通过 c13 写入 case-specific Topic、Group 或阈值。
- 不得把资源硬上限当作正常检索目标，也不得通过反复调整硬数量让单个测试主题通过。
- 不得在未固定地图和未建立多主题对照的情况下继续调 group／topic／block 配额。
- 不得声称实现 GAP-08、09、11～13。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| Topic 合并输入不完整 | `knowledge_map_candidate_scope_invalid` | 原 candidate 和诊断 | 修复输入后重建 |
| Topic／Group 结构无效 | `knowledge_map_structure_invalid` | 未发布的新 artifact 与诊断 | 修复后重建 |
| Group vector 缺失或模型不符 | `knowledge_map_index_invalid` | 旧 active map 不变 | 重建地图 |
| 没有兼容 active map | `wiki_topic_v2_not_ready` | selected Profile、snapshot 和候选地图状态 | 构建或显式激活兼容地图 |
| 查询超出阶段预算 | `search_wiki_topic_budget_exceeded` | 完整阶段计数 | 调整容量或查询 |

### 人工检查点

- 当前 15 篇自动回归通过后，人工核对跨文档合并样本与 active map。
- 50 篇阶梯通过后，人工批准是否继续到 100／300／1,000 篇。
- 本轮不能把 1,000 篇运行成本当作已批准预算，必须先看 50 篇的实测增长。

### 决策预算

- 预计 B 级决策数量：6。已重新评估：第 6 项“自适应停止策略”与持久化向量、查询回归直接耦合，拆出会制造一个使用固定配额的假基线，因此保留在同一 P0 范围。
- 是否存在 C 级决策：否；只新增不可变地图 artifact、索引和 active pointer，不删除旧地图、Profile 或 Preview Run。
- 超过预算时的拆分方式：先完成 Topic 身份和索引，再拆 Group 构建和 Active map。

## 目标

```text
document-bound candidates
→ conservative cross-document topic identity
→ topic-based groups
→ immutable map + persisted group vectors
→ active map(snapshot + selected Profile)
→ bounded online retrieval
```

## 输入与输出

输入：

- completed Profile Preview Run；
- 冻结 Profile 与 snapshot；
- chat provider；
- embedding provider。

输出：

- `profile-knowledge-map-v3` immutable artifact；
- 跨文档 Topics 与 Groups；
- 持久化 group vectors 和文本索引；
- active map pointer；
- 当前 15 篇回归和规模探针结果。

## 不包含

- GAP-08 coverage fallback 优化；
- GAP-09 group 多归属策略调整；
- GAP-11 旧 Wiki 工具清理；
- GAP-12 更大规模的单篇选择性 fixture；
- GAP-13 held-out 主题和 1,000 篇质量结论；
- Profile 背景 embedding 消融（BL-0019）；
- 最终回答和答案 Judge。

## 下一版设计修订（13 §3.8／§6.7／§7.6／§8.7，待实现）

以下内容来自 2026-11-06 的后续讨论，不属于当前 P0 已实现行为：

- map schema 直接替换，不保留历史 map 兼容读取；旧 map 全部重建；
- manifest、metadata 和 vectors 分离，vectors 使用独立向量 artifact 并预计算 normalized vector；
- 增加不可变地图热缓存、Group lexical 倒排和按需 Topic 加载；
- 取消固定前 64，Group 候选按相关性、分数断层和 child Topic 覆盖自适应选择；
- 不增加 Group Cluster；Group／Topic 数量随文档数线性增长视为构图质量异常；
- Topic identity 和 Group identity 在整张 map 范围内建立全局候选图并固定点收敛；
- semantic batch 只控制 LLM 输入规模，不能成为身份比较边界；固定 pass 上限只能作为安全护栏；
- 选中 Group 后展开全部 Topics，取消 Topic top-k 和相对分数裁剪；
- 系统维护受控 Aspect Kind，Profile 根据领域和代表问题自动派生 Aspect Schema；
- 地图编译期先建立 TopicAspect catalog，再把 Blocks 映射到 catalog，最后复核 unclassified；
- TopicAspect 和 BlockAspectSupport 的 importance／support strength 冻结到 map；
- 在线查询先选择 Aspect，再在每个 Aspect 内选择代表 Block；
- Block 仍以原始证据为单位，不强制以 Candidate 分层；
- Aspect 审核合并到 setwise evidence audit，不新增独立 LLM judge；
- 覆盖修复 LLM 仅在确定性审计发现缺口时执行。

完整目标设计见 [13](../../design/13-profile-driven-wiki-evidence-retrieval.md)。

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 |
| --- | --- | --- | --- | --- | --- |
| A1 | Candidate 可追溯 | Topic 保存全部 source candidate，不丢失文档绑定 | 输入／输出 candidate ID 集合 | 差异=0 | 单元／集成 |
| A2 | Topic 严格合并 | 精确同义可合并，成员／上下位／不同粒度保持分开 | 固定正反例 | 阻断反例=0 | 单元 |
| A3 | Group 基于 Topics | Topic 到 group 覆盖完整，来源文档一致 | orphan／source mismatch | 0 | 单元／真实地图 |
| A4 | 查询不重嵌全图 | 使用 map 持久化 group／topic vectors；query 单独嵌入；候选 block vectors 从 collection snapshot 索引读取 | provider 调用计数与候选数 | 查询期 group embedding=0；document embedding=0；持久化 vectors 覆盖全部候选 ID | 集成 |
| A5 | 容量分离 | group 总数增长不改变查询候选上限 | 15／50 篇探针 | query candidate bounded | 运行探针 |
| A6 | Active map 正确 | 只解析 snapshot＋selected Profile 匹配地图 | 错 Profile／错 snapshot | 错配读取=0 | 集成 |
| A7 | 当前效果保持 | 15 篇 c13 门禁不回归 | 现有 gate | 连续 3 次核心 7/7、扩展≥3/4 | 真实运行 |
| A8 | 数量随主题复杂度变化 | 窄／中／宽主题的 group、topic、evidence 选择数量不同，硬护栏不常驻饱和 | 多主题 distribution | 所有主题长期等于同一配额=失败 | 单元／真实运行 |

### 验收冻结与变更

- 冻结时点：2026-11-06，P0 实施前。
- 已确认的阻断反例：candidate 丢失、Topic 错并、orphan topic、active map 错配、查询全量 embedding。
- 验收变更记录：无。

## 验收执行

- [x] 用户可观察的主路径成立。
- [x] 核心不变量已被自动化测试或明确的人工验收覆盖。
- [x] 失败路径不会破坏旧状态或伪装成功。
- [x] 验收矩阵中的阻断项全部通过。
- [ ] 未通过的已知反例已修复，或由负责人明确接受风险。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 084-D1 | B | 如何形成跨文档 Topic 身份 | 确定性 name／alias 合并＋有界语义近邻 LLM 判同；uncertain 保持分开 | 13 §13.6.2 | 不做全量自由合并，不新增 alias／事实 | 决定地图规模与证据聚合 | A1／A2 | 待确认 |
| 084-D2 | B | 如何避免固定顺序 group batch | 用 Topic／Group embedding 形成语义邻域批次，并进行有界迭代归并直至无合并或达到上限 | 13 §13.6.3 | 不依赖 doc_id 顺序，不一次把全 topics 交 LLM | 决定跨文档 group 质量 | A3／A5 | 待确认 |
| 084-D3 | B | 如何持久化检索向量 | map artifact 保存 group／topic vectors；block vectors 继续由 collection snapshot 的 LanceDB 保存；查询只 embed query，按 candidate block IDs 读取存量 vectors | 13 §13.6.4 | 不查询时重嵌全图，也不对候选原文重复 document embedding；检索匹配不足时优先调整 query，不新增第二套主题向量 | 降低查询 embedding 成本，同时保持 block vector 输入契约 | A4 | 已确认 |
| 084-D4 | B | 地图容量如何分离 | 地图 group／topic 提高为资源护栏；查询候选、模型选择、输出预算独立 | 13 §8.5 | 不把 512 当产品语义上限 | 支持更大知识库 | A5 | 待确认 |
| 084-D5 | B | 如何选择 active map | 使用不可变 active pointer，绑定 snapshot、Profile ID/hash、marker | 13 §13.6.5 | 不读取最新任意地图；不回退旧 Profile | 避免运行时错用地图 | A6 | 待确认 |
| 084-D6 | B | 查询如何决定 Group／Topic／Aspect／Block 数量 | Group 按相关性、分数断层和 child Topic 覆盖自适应扩选；选中 Group 展开全部 Topics；在线选择 TopicAspect，再在每个 Aspect 内选择代表 Block；固定数量只作异常资源护栏 | 13 §8.5、§8.7、GAP-14、GAP-15 | 不增加 Group Cluster；不再在查询时动态发现 Aspect；不按来源数量机械保留 | 结果规模随主题宽窄变化，LLM 调用次数不能随文档数增长 | A8 | 待确认 |

## 证据包

### 自动化证据

- 命令：Ruff format/check、`pyright --project backend/pyproject.toml`、`pytest backend/tests`
- 结果：Ruff 通过；Pyright 0 errors；后端全量 430 passed，其中新增持久化 block vector
  读取测试验证 query embedding 期间 document embedding 调用为 0。
- 命令：`corepack pnpm --dir frontend check`、`corepack pnpm --dir desktop check`
- 结果：Vue type check、Vite build、Rust fmt、cargo check、clippy 全部通过。
- 覆盖的不变量：A1 candidate 可追溯、A2 Topic 严格合并、A3 group 来源一致、A4 持久化向量、
  A6 active map 绑定，以及 Partial 覆盖审核与 direct score floor。

### 运行证据

- 固定输入：snapshot `s_6b787258ba7d700c`；Profile `ep_system_technical_learning_v1`；
  Preview Run `pr_ca0dd3ab3f41430f`；topic `AI Agent 隔离与控制边界`。
- 15 篇地图：`km_7609264fcc3cf100`，schema v3，750 topics、150 groups、750 topic vectors、
  150 group vectors。
- 单篇地图：使用当前代码从 `pr_8bd488e68aaa4a72` 重建为 `km_2acf5024c8a17b29`。
- 15 篇连续 3 次：核心 7/7、扩展 3/4；selected blocks 26／27／26；precision
  0.808／0.815／0.885；无关 token 15.2%／14.4%／7.7%；均远低于 96 blocks／50k tokens 护栏。
- 15 篇每次读取 61／59／62 个持久化 block vectors；`block_embedding_tokens=0`，
  查询期 document embedding provider 调用为 0。
- 单篇连续 3 次：6/6；每次 21 blocks；precision 0.952；无关 token 1.29%。
- 目标失败样本：`parallel_work_state` 在前两版分别因 group 未稳定覆盖和固定 group-relative
  Partial floor 被删。修复后 3/3 由 Partial 覆盖审核稳定恢复，未写 case-specific ID 或阈值。

### 失败探针

- 注入或复现的失败：固定地图下连续回放 c13 topic；观察 `parallel_work_state` 目标证据
  `doc_7f36f426b84f40eb9439acaad4949e32#v0001#c1#b00016` 是否被路由、进入候选、被
  Partial 预筛删除，以及覆盖审核是否恢复。
- 系统保留的旧状态：active map 指针不变；失败运行只写独立 probe artifact，不覆盖地图。
- 用户看到的错误：门禁输出 `required_groups_hit=false`、missing group 和目标 block 诊断；
  不把缺项伪装成成功。
- 结果：修复前稳定复现缺项；修复后 3 次均通过，噪声仍在 20% 门禁内。

### 50 篇扩大准备

- 数据前置：需要与同一冻结 Profile 相容的 50 篇 completed Preview Runs，且文档范围不重叠。
- 构建入口：`scripts/wiki_topic_v2_probe.py --build-run-id ... --force-new`。
- 查询入口：同一脚本使用新 map ID，连续运行 3 次；每轮执行 `scripts/c13_wiki_v2_gate.py --mode cross`。
- 必记数据：topic 合并前后数量、group 数、跨文档 group 数、group candidate 数、gold group
  是否进入候选、precision、无关 token、构建／查询 tokens 和延迟。
- 当前状态：尚未执行。现有 live snapshot 只有 15 篇，不能把 15 篇结果外推为 50 篇结论。

### 已知反例账本

| 场景或反例 | 期望行为 | 实际结果／分布 | 是否阻断 | 处理结论 |
| --- | --- | --- | --- | --- |
| 固定 group-relative Partial floor | 新 aspect 不应仅因低于组内最高分被永久删除 | 目标块连续被删，核心组缺 1 | 是 | 增加覆盖审核，并以本轮 direct 最低分作为最终自适应下限 |
| 覆盖审核只增不删 | 补缺口时不能扩张为更多背景和细节 | precision 降到 0.750～0.767 | 是 | 改为固定 direct、对 Partial 做最终覆盖与去重审核 |
| 多轮 Prompt 仍扩大 Partial | 应只补中心缺口 | selected 32～34，precision 0.75～0.765 | 是 | 增加 direct score floor，3 次恢复通过 |

### 人工验收

- [ ] 当前 15 篇跨文档 Topic／Group 与 active map 人工复核。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | Candidate 与 Topic 的身份边界是什么？ | Candidate 来自单文档抽取；Topic 由候选跨文档合并，并通过全部相关 candidate 的文档、版本和 block ID 回到原文证据。 | 通过 1/3 | 无 |
| R2 | 为什么查询只 embedding query，并从当前 snapshot 读取候选 block vectors？ | block 向量已经持久化，读取避免重复成本；必须校验 snapshot、索引、block 集合和 embedding 契约。维度相同但模型或 snapshot 不同仍处于不同语义空间。 | 通过 2/3 | 无 |
| R3 | Active map 不匹配时为什么不能回退最新地图？ | 地图必须锚定同一 snapshot 的 block；Profile 改变后抽取候选、Topic 和 Group 也已改变，旧地图不能代表当前抽取契约。 | 通过 3/3 | 无 |
| R4 | 复用入库 block vector 和新增第二套主题检索向量之间如何取舍？ | 选择复用 A。入库向量已包含标题、章节等元数据；若匹配不足，应调整 query 以适应现有向量基础设施，而不是增加第二套索引。 | 通过 4/4 | 后续用 held-out precision 验证该判断 |

<!-- reverse-explanation-gate: passed 4/4 -->

## 协作复盘

- AI 明显加速的环节：快速建立跨文档 Topic／Group、active map 和三轮真实检索回归，并把失败定位到固定 Partial floor 与重复 block embedding。
- 负责人失去系统模型的环节：早期把“读取持久化 block vector”设计成查询时重新嵌入；负责人通过“索引里已经有向量”直接指出实现偏离基础设施。
- 真正发现问题的控制点：固定地图下的真实回放、trace usage 和负责人对向量基础设施的追问共同发现成本问题；反向解释中的方案 A／B 权衡确认了单一向量契约。
- 只有仪式感、没有产生判断价值的流程：前三道反向解释偏向身份事实，负责人能够直接回答，但没有充分检验设计判断。
- 下一次准备调整的一到两项：反向解释改为围绕替代方案、错误代价和推翻条件；在实现前显式检查已有基础设施是否已经满足需求，避免重复建设。

## 已知风险与后续

- 未验证项：真实 50／100／300／1,000 篇规模。
- 未通过的设计目标：15 篇 precision 中位数 0.815，低于旧基线 0.879；当前只满足本轮
  ≥0.80 和无关 token ≤20% 门禁，不能宣称质量无回归。
- 接受的风险：本轮不解决 P2 差距和 Profile 背景消融；新增一次 Partial 覆盖审核模型调用，
  需要纳入后续成本分析。
- 后续待办或 ADR：GAP-08、09、11～13；BL-0019。

## 停止与推翻条件

- Topic 合并导致 candidate/evidence 丢失或大量错并；
- Group 构建仍依赖全量顺序 batch 或无法覆盖 topics；
- Query 仍需要重新 embedding 全图；
- Active map 无法保持 Profile／snapshot 一致；
- 当前 15 篇 c13 核心覆盖回归且无法在 P0 范围内修复。
