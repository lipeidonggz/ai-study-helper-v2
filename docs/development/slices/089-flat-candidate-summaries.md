# 089 · 候选同级摘要契约与 7.6 Topic Ledger 实现

- 状态：待产品复核（代码与自动化验证完成，真实新 Run 与 c13 检索验证待执行）
- 开始日期：2026-11-10
- 关联设计：[13 Profile-Driven LLM Wiki](../design/13-profile-driven-wiki-evidence-retrieval.md)
- 前置切片：088
- 人工验收：完成新抽取 Run 后，由沛东审核候选摘要界面和 Topic Ledger 行为

## 控制契约

### 背景与失败场景

- 触发问题：07.6 Topic 身份要求候选拥有同级 `summaries[]`，当前抽取仍保存
  `summary + summary_variants`，存在主摘要与补充摘要的隐含层级。
- 不处理时的用户损失：Topic 向量和身份判断仍会偏向主摘要；跨通道新增信息不能等价参与
  消歧和合并。
- 现有流程为什么不足：旧 schema、Prompt、审核提示词、API 和前端都围绕主摘要设计。
- 本切片负责什么：先完成候选摘要平铺契约并冻结新的 Extraction Run，再按 7.6 实现
  ReviewSet／Topic Ledger 主路径。
- 不负责什么：Group Ledger、在线检索工具和答案生成不因本切片重写；旧 Extraction Run
  不自动迁移为新 7.6 正式输入。
- 停止或撤掉条件：如果平铺摘要无法支持现有抽取审核，或新 Topic 流程在固定样本上无法
  证明候选覆盖与证据回源，则停止后续接线。

### 用户可观察目标

- 候选详情和 API 只展示一个同级摘要列表，不再区分主摘要和补充变体。
- 同一候选被多个通道或窗口补充时，摘要按同级集合去重累积。
- 新抽取 Run 的候选可被 7.6 编译器完整消费。
- Topic 合并只依据当前 Topic revision 的标题、别名和完整摘要集合。

### 核心不变量

1. 每个候选至少有一个非空摘要，且没有 primary summary 身份。
2. 候选摘要集合与来源证据仍可追溯到原 Extraction Run 和 block。
3. Topic 身份判断不使用原始 block，只使用当前 NodeRef 的标题、别名和完整摘要集合。
4. 任一 attach／detach／merge 都使 Topic revision 和旧比较结果失效。
5. 失败编译不修改旧生效地图，也不发布部分地图。

### 明确禁止

- 不允许用 `summaries[0]` 隐式充当主摘要。
- 不允许把旧 `summary_variants` 通过 `ordered_unique` 后继续维持主次逻辑。
- 不允许 Topics 仅因候选之间的相似度路径自动合并。
- 不允许无提示地截断目标 Topic、摘要、pair 或 LLM 输出。
- 不允许失败或中断的运行留下可查询的半成品地图。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 候选摘要为空 | 抽取运行失败 | 失败运行与诊断，不发布 | 是 |
| 摘要 schema 非法 | 结构化错误 | 原始失败输出和请求 trace | 是 |
| 目标 Topic 超预算 | 明确预算失败 | 临时诊断，不发布地图 | 是 |
| 判官失败 | 明确 judge 失败 | 已成功请求的 trace，不提交 Ledger | 是 |
| 地图发布失败 | 旧地图继续生效 | 暂存产物和失败原因 | 是 |

### 人工检查点

- 编码前不需要追加产品决策。
- 实现中途检查摘要列表界面和新 Extraction Run。
- 完成后请沛东执行候选详情与 Topic Ledger 验收。

### 决策预算

- 预计 B 级决策：3（摘要列表存储方式、旧运行展示方式、7.6 参数初值）
- 是否存在 C 级决策：否

## 目标

完成候选同级摘要契约、新的抽取 Run，以及基于该 Run 的 7.6 Topic Ledger 主路径。

## 输入与输出

- 输入：当前 snapshot、technical learning Profile、15 篇文档。
- 输出：新 Extraction Run、候选摘要列表、新的不可变 Topic Ledger 地图和审计数据。

## 不包含

- Group Ledger 重新设计。
- 在线检索 `search_wiki_topic_v2` 算法重写。
- 旧地图回填或旧候选 schema 的静默兼容。

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 |
| --- | --- | --- | --- | --- | --- |
| S1 | 摘要契约 | 抽取 API、DB、UI 均返回同级 `summaries[]` | 无 `summary_variants` 读路径 | 任一残留读取路径 | 单元／集成 |
| S2 | 多通道累积 | 同一候选多通道补充摘要同级去重 | 任一摘要丢失或主次层级 | 任一反例 | 单元／集成 |
| S3 | 新 Run | 生成新的 pipeline version 与 Extraction Run | run 绑定新 schema | 旧 run 被当作新输入 | 真实运行 |
| S4 | Topic 身份 | Candidate 只与当前 Topic revision 比较 | 无候选间 `same` 传递 | 任一传递合并 | 集成 |
| S5 | Revision 失效 | attach／merge／detach 后旧比较失效 | stale NodeRef 不参与操作 | 任一旧结果复用 | 集成 |
| S6 | 证据门禁 | 每个 Topic 来源回到候选和 block | 不得出现无来源 Topic | 任一悬空 | 集成 |
| S7 | c13-003 | 新 Topic 地图可服务后续检索验收 | 不回归现有目标证据 | 目标证据丢失或噪声失控 | 真实运行／人工 |

## 验收执行

- [x] S1：候选模型、数据库迁移、API、前端和 Topic 消费均使用同级 `summaries[]`。
- [x] S2：多通道摘要按规范化文本同级去重累积。
- [x] S4：候选只与当前 Topic revision 比较，不建立候选间身份链。
- [x] S5：attach／merge／detach 后 revision 改变，stale WorkItem 直接丢弃。
- [x] S6：Topic 来源保存 `extraction_run_id + candidate_id`，并回到原始 block。
- [ ] S3：真实 15 篇新 Extraction Run 尚未生成，等待以应用所有者身份重启后执行。
- [ ] S7：基于新 Run 的 c13-003 检索验收尚未执行。
- [x] 失败路径不会发布部分地图；预算、判官和 schema 失败均为整次 compile attempt 失败。
- [x] 已通过自动化覆盖的阻断项没有已知反例；真实运行阻断项仍为未验证。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 089-D1 | A | 摘要持久化形态 | 新列 `summaries_json`，旧列只保留历史审计用途 | 13 §6.4、§7.6 | 迁回旧主摘要 / 继续写 summary variants | 影响 DB、API、UI | migration + tests | 待确认 |
| 089-D2 | B | 旧 Extraction Run 处理 | 界面可删除终态 Run；仍被比较或清单引用时明确拒绝；未删除的 v8 旧 Run 也禁止进入 v6 编译 | 13 §7.3.2 | 静默升格 / 允许带悬空引用删除 | 影响兼容、审计和引用完整性 | API tests + UI | 已确认 |
| 089-D3 | B | 新索引和阈值参数 | 阈值、预算、Prompt、判官模型及 embedding 输入契约冻结为 `ProfileMapCompileContract`，内容和 ID／hash 同步校验，并随 manifest 发布 | 13 §7.4.1、§7.6 | 代码常量 / 运行时猜测 | 影响地图可复现性、复用和 hash | integration tests | 已确认 |
| 089-D4 | A | WorkSet 提交粒度 | 一个 WorkItem 的多个 LLM 请求全部成功后才修改 Ledger；请求批次只是传输单位 | 13 §7.6.6、§7.6.7 | 按批提前提交 / partial WorkItem | 影响一致性和失败恢复 | budget／judge tests | 已确认 |
| 089-D5 | B | Topic pair cache | 仅当前 attempt 内 Topic↔Topic 复用，键包含双向规范化 NodeRef 和判官契约哈希；缓存只保存轻量结论并在读取时复验 | 13 §7.6.5 | Candidate↔Topic 也缓存 / 跨 attempt 持久化 / 只信 pair key | 影响调用成本和判断一致性 | cache-key／reverse hit test | 已确认 |
| 089-D6 | A | 身份向量持久缓存 | Candidate 使用独立不可变 bundle，Topic 使用 revision 绑定的身份向量；逐项保存契约、摘要输入与向量 hash | 13 §7.6.4、§7.6.5、§7.6.9 | 只保存当前 attempt 内向量 / 仅用整个 NPZ hash | 影响跨 attempt 成本、一致性和审计 | bundle load／reuse tests | 已确认 |
| 089-D7 | A | compile attempt 与判官契约身份 | manifest 保存 `compile_input_key`、`compile_attempt_id`；judgement／decision 同时保存判官 contract ID 和 hash | 13 §7.4.1、§7.6.8 | 仅使用 contract hash / 由 NodeRef 生成 decision ID | 影响审计稳定性和跨 attempt 区分 | contract／attempt tests | 已确认 |
| 089-D8 | A | Aspect 派生产物身份 | TopicAspect、Support、Exclusion 的 ID 绑定 Topic revision；最终 exclusion 只允许四种 reason code，missing_aspect 只作为编译中间态 | 13 §7.6.10 | 使用 topic ID / 发布 missing_aspect / 共用旧 reason code | 影响 revision 失效、审计和 catalog 扩展语义 | aspect identity tests | 已确认 |
| 089-D9 | A | Ledger membership 与摘要来源 | manifest 发布显式 TopicCandidateMembership；每条同级摘要保存来源 CandidateRefs；Topic source 仍作为物化投影 | 13 §7.6.2、§7.6.8 | 只用 Topic 内嵌 source / 摘要文本无来源 | 影响成员覆盖、摘要回源和地图自解释 | manifest invariant tests | 已确认 |

## 证据包

### 自动化证据

- 命令：
  `python -m pytest tests/test_profile_knowledge_map.py -q`
- 结果：54 项通过。
- 覆盖：摘要身份向量、Candidate→Topic 直接比较、target 全部判断、200 对预算失败、
  stale WorkItem、Topic pair key、revision 向量复用、跨 Run 重算、判断审计、WorkItem
  decision、Topic Ledger 操作、旧 pipeline 拒绝、compile contract 变化和篡改校验、
  最终 CandidateRef 覆盖率、Prompt 计入 WorkItem token 预算、canonical title 排序、
  Aspect／Support／Exclusion revision 身份、显式 membership、摘要来源、独立候选 bundle、
  反向 pair-cache 命中、持久身份向量复用及 compile attempt 身份。

- 命令：
  `python -m pytest tests/test_profile_extraction_profiles.py
  tests/test_profile_extraction_preview.py tests/test_profile_extraction_api.py
  tests/test_profile_extraction_comparison.py tests/test_migration.py -q`
- 结果：49 项通过。
- 覆盖：`summaries[]` schema、多通道同级累积、API、迁移、旧列移除。

- 命令：`ruff check --no-cache`，范围为本切片 Python 实现和测试。
- 结果：通过。

- 命令：`pytest backend/tests/test_c13_wiki_v2_gate.py`，仓库根目录执行。
- 结果：3 项通过。该结果只验证 gate 代码，不代表新 Run 已产生真实 c13 结果。

### 真实运行证据

- 已确认 snapshot：`s_6b787258ba7d700c`。
- 已确认冻结 Profile：`ep_system_technical_learning_v1`。
- 旧正式候选 Run：`pr_ca0dd3ab3f41430f`，流水线 `profile-preview-pipeline-v8`，不得作为
  本切片 7.6 正式输入。
- 尝试从产品数据根目录生成新 Run 时，迁移备份文件创建被 Windows ACL 拒绝；当前
  Codex 进程属于 `CodexSandboxUsers`，对该目录只有只读权限。失败发生在迁移开始前，
  未产生新 Run、部分候选或半迁移数据库。

## 反向解释

待沛东在应用所有者身份下完成新 Run 后逐题进行。建议问题：

1. 为什么新 Extraction Run 不能让旧 candidate 仅凭相同 `candidate_id` 继续挂在原 Topic 上？
2. 为什么 Topic 合并必须先完成一个 WorkItem 的全部 target 判断，不能按 LLM 请求批次提前提交？
3. 为什么 `same` 只能作用于当前 CandidateNode／Topic revision 对，而不能沿候选图传递？
4. 为什么 Topic revision 变化后，旧 pair cache 和旧 WorkItem 必须立即失效？
5. 为什么超过 target 预算时必须整次编译失败，而不是截断低分目标？

## 协作复盘

- AI 加速点：把两个层级都收口到 WorkSet，并用自动化测试锁定失败语义。
- 负责人需要重点检查：真实 Run 的 Topic 合并、拆分边界和 c13 检索噪声。
- 有效控制点：先平铺摘要再重写 Topic 身份，避免在同一轮同时改变输入契约和身份算法。
- 仪式性风险：若真实 c13 不通过，当前 WorkSet 单测只能证明流程自洽，不能证明产品有效。
- 下次调整：真实运行应作为切片完成门禁，不能只以单元测试替代。

## 已知风险与后续

- 旧候选不能进入新 7.6 正式编译；必须先以应用所有者身份重启并生成新 Run。
- 真实 15 篇 Topic 数量、合并率、审查用量和 c13 检索覆盖／噪声仍未知。
- 身份向量已经持久化到不可变 vector bundle，并绑定 CandidateRef／TopicNodeRef、embedding
  契约、摘要输入 hash 和向量内容 hash。
- 试抽取界面已经支持删除终态 Run，但为了保护比较记录和文档清单引用，当前会拒绝删除仍被
  引用的 Run；若要强制级联删除这些引用，需要另立数据删除切片。

### Topic 账本结果审核队列

基于 `pr_0d976f2ca26f4edb` 和地图 `km_33d76adca64eb2d3`，依次人工抽查：

1. 有 target、但所有 target 均被判为 `different` 的 Candidate 工作项，检查 Topic 判官是否漏合并；
2. 没有任何 target 的 Candidate 工作项，重点检查同文档高相似候选是否存在漏召回；
3. 29 个跨文档 Topic 及同名、泛称异常，检查合并结果和入口名称质量。

当前 874 个候选生成 811 个 Topic，其中 758 个 Topic 只有一个候选来源；Topic 关系判断为
`same=64`、`different=316`、`uncertain=5`。审核先从第 1 组开始。

#### 第 1 组：全部 target 均判 different

- 总体：189 个 Candidate 工作项；其中 142 个只有 1 个 target，34 个有 2 个，
  13 个有 3～6 个 target。
- 抽样：28 个，覆盖全部 15 篇文档，并包含全部多 target 情形。
- 结论：抽样中没有发现明显的“本应 same”漏合并。`different` 主要集中在：
  - 父机制与具体实现；
  - 整体对象与成员／组成项；
  - 并列产品、模型或测试套件；
  - 通用概念与该文档中的特定作用范围；
  - 同一系统的不同组件、上下游或用途关系。
- 判断：当前 Topic 判官在抽样范围内保持了“同一对象、同粒度”的高精度边界。当前 811 个
  Topic 的碎片化不应通过降低该判官标准直接解决；父机制、组成项和方面之间的关系应交给
  Group／检索区域层处理。

#### 第 2 组：没有任何 target

- 总体：619 个 Candidate 工作项的 `target_count=0`。该数字本身不表示漏召回，因为候选
  第一次进入空 Ledger，或全部 active Topic 的当前 revision 都低于阈值时，都会合法地
  产生零 target。另有 36 个 Topic 工作项也是零 target。
- 高相似度检查：对 619 个候选分别寻找同文档、但最终属于不同 Topic 的最近候选。余弦相似度
  `>=0.90` 有 21 个，`>=0.85` 有 52 个，`>=0.80` 有 110 个，`>=0.78` 有 146 个；
  这些案例覆盖全部 15 篇文档。
- 判官回查：146 个 `>=0.78` 案例中，134 个能找到当时另一节点与其目标 Topic revision 的
  实际判官记录。按案例统计，133 个全部为 `different`，1 个为 `uncertain`，没有实际
  判为 `same` 却仍分裂的案例。其余 12 个之所以没有同一条记录，是因为 Topic 在后续挂接后
  revision 和摘要集合发生变化；人工抽查仍没有发现应合并为同一对象的情况。
- `different` 的典型边界仍然是：不同产品、不同组件、整体与方面、类别与成员、文档与其中
  描述的对象。例如 `QLoRA` 与 `QLoRA 的内存优化结构`、`Guidance` 与
  `Guidance 的结构强制机制`、`Claude Code` 与 `Codex` 都应保持不同 Topic。
- 多个摘要能在不同候选之间完全复用，因而产生余弦相似度 `1.0`，但标题指向不同对象。
  例如 `long-term memory` 与 `middleware` 共用了同一条 harness 组件摘要，
  `DeepSeek V3.2` 与 `Kimi K2 Thinking` 共用了同一条开放权重模型摘要。此次 Run 中
  共有 15 组、涉及 46 个候选的跨标题完全相同摘要。
- 这不是摘要生成错误。对应证据块本身只给出了一个父级命题或并列列表，没有提供每个成员
  各自的额外信息；要求为每个候选补出不同摘要会引入证据不支持的内容。完全相同的摘要
  准确反映了“这些候选只共享同一父级上下文”这一事实。
- 结论：当前 `0.78` 阈值没有证据表明过高；不应依据零 target 数量或最终 Topic 数量直接
  调整阈值，也不能因为候选当前只有名称和父级成员关系就删除“空壳”候选。抽取按单篇文档
  执行，当时无法预知未来文档是否会补足定义、机制或用途；空壳 Topic 是未来证据的稳定
  挂接点和检索入口，删除它会同时丢失“该概念属于某个父机制”的既有关系。
- `pr_0d976f2ca26f4edb` 实际同时抽取了父级候选 `Harness 的 7 个组件`
  （`wc_c3e1d1aba0978938`）和七个成员候选。七个成员共享同一摘要因此是当前证据状态的
  正确表达，而不是摘要缺陷。后续文档给出成员定义时，应把新摘要来源追加到既有 Topic，
  同时永久保留原始父级 Block 作为关联证据。
- 当前输出真正需要继续核查的是关联关系的承载方式。七个成员 Topic 最终被分到多个不同
  Group：`middleware`、`system prompt`、`tool description`、`tool implementation`
  与父 Topic 同组，而 `long-term memory`、`skill`、`sub-agent configuration` 因各自
  更强的语义邻域进入其他 Group。共享 Block 仍能在来源层证明它们同属七个组件，但仅靠
  当前单父 Group 不能完整表达“学习时同时看到父机制与全部成员”的关系。
- 因此后续设计要区分两类状态：候选／Topic 的**身份有效性**与当前**知识充实度**。空壳
  Topic 身份有效但知识有限；未来证据到来时只扩充摘要和来源，不替换身份。父级成员关系
  需要独立于 Group 的单父归类保存，否则成员进入更合适的语义 Group 后，原机制族关系会
  在可读结构中消失。具体关系模型、空壳状态字段和检索加权方式尚待沛东确认。

## 停止与推翻条件

- 新摘要契约无法支撑候选审核。
- 7.6 在当前 15 篇上没有可用证据覆盖。
