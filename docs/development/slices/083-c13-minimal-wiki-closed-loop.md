# 083 · search_wiki_topic_v2：单文档到 15 篇的 Wiki 证据检索验收

- 状态：实现完成，待 B 级决策集中确认与反向解释门禁（尚未提交业务代码）
- 开始日期：2026-11-05
- 关联设计：[01 产品需求](../../design/01-product-requirements.md)、[02 Agent 开发方法](../../design/02-how-to-build-an-agent.md)、[05 存储与检索策略](../../design/05-storage-and-retrieval-strategy.md)、[06 检索、工具与 Agent](../../design/06-retrieval-pure-agent.md)、[08 评测台](../../design/08-evaluation-console-design.md)、[11 LLM Wiki 编译](../../design/11-llm-wiki-compilation.md)、[12 Profile-Driven Wiki Extraction](../../design/12-profile-driven-wiki-extraction.md)
- 前置切片：076 Profile 管理；077 Profile Preview Run；078 Profile 比较；079 试抽取反馈与证据翻译；080 按原文顺序检查候选覆盖；081 文档级候选标题消歧；082 逐通道候选累积与增量补充
- 后续改造：[084 Profile Wiki 地图规模化就绪改造](084-scale-ready-profile-wiki-map.md)；084 的持久化向量、跨文档 Topic／Group 和自适应证据选择取代本切片中的旧简化容量参数
- 基线约束：081／082 已作为 `39e6413` 完成提交；083 分支已在其上重新建立，不依赖旧 069–074 Wiki 实现
- 历史参考：064／065 的 query-side `search_topic` 和 068–074 的旧 Wiki 全流程都不是本切片的实现基线
- 已确认：首轮单文档 anchor 使用 `How we contain Claude across products`；`search_wiki_topic_v2` 采用 `topic → 原始 evidence blocks` 契约；单篇先建 local topic groups，15 篇再做 global group 归并并复用同一工具路径
- 人工验收：先用固定证据目标验收单篇证据检索，再执行 15 篇 Wiki 编译；本切片不验收最终回答

## 控制契约

### 背景与失败场景

- 触发问题：076–082 已把 Wiki 的第一阶段收敛为 Profile-driven 单文档候选抽取，但“候选看起来合理”不能独立证明它对最终 Wiki 有用。候选太碎、太粗、标题有歧义或证据绑定不足，只有在最终检索行为中才会暴露。
- 旧路线为什么失败：068–074 直接从尚未校准的抽取结果开始构建 Wiki。15 篇／526 blocks 最终形成 420 个 candidates、365 个 topics 和 1,092 个 coverage slots；`search_wiki_topics` 一次查询返回全部 365 个 topics，轻量目录约 82k tokens，已知目标主题排名为 87／107／184，主 Agent 仍为 0/3。
- 本切片如何改变验收方式：不再维护一套独立的“单文档候选质量基准”作为完成条件，而是增加 `search_wiki_topic_v2`，以最终可观察行为反向验收第一阶段：给定主题，它能否从该文档返回回答该主题所需的主要证据块，并控制无关块。
- 两步门禁：先只用一篇文档。如果单篇 Wiki 都无法返回该文档中应提供的正确证据块，就停止，不执行 15 篇编译。单篇通过后，才允许使用同一冻结 Profile 编译全部 15 篇并检验跨文档证据选择。
- 本切片负责什么：定义并实现 `search_wiki_topic_v2`；以“AI Agent 隔离与控制边界”为主输入；建立单篇和 15 篇两级证据检索验收；保证工具输出原始 blocks、结果规模有界、无答案生成。
- 本切片不负责什么：不生成最终答案，不修改问答 Agent 的系统提示词，不运行答案 Judge；不新增独立的 Profile 质量分数或通用基准平台；不做 narrative/index 页面、产品 UI、增量构建或 held-out 泛化。
- 什么情况下应停止或撤掉机制：单篇无法取齐该文档应有证据时，不进入 15 篇；15 篇阶段如果需要把全部 topic 或全部 block 交给 LLM 才能取齐，也不成立；只有靠人工挑 topic 或人工补目标 block 才能通过时，工具无效。

### 用户可观察目标

固定 topic 为：

```json
{"topic":"AI Agent 隔离与控制边界"}
```

这是历史主 Agent 实际调用 `search_topic` 时最常使用的输入，不使用完整问题。

两级用户可观察目标：

1. **单篇门禁**：以 `How we contain Claude across products` 为首个 anchor document。使用冻结 Profile 和 076–082 的第一阶段输出建立单篇 Wiki 后，`search_wiki_topic_v2` 返回该文档中回答该主题所需的全部主要证据，同时无关 blocks 在阈值内。
2. **15 篇门禁**：单篇通过后，才允许对同一 snapshot 的全部 15 篇文档执行 Wiki 编译。`search_wiki_topic_v2` 返回跨相关文档回答 c13-003 所需的主要证据集，覆盖七组核心边界和至少两类扩展边界，同时不引入过多无关 blocks。

两个阶段都只输出 evidence bundle，不生成答案。

### 核心不变量

1. 第一阶段输入必须来自冻结 Profile、冻结 snapshot 和 076–082 的最终输出：先做 082 通道累积，再做 081 文档级标题消歧；不得从旧 069–074 Wiki map 反向补数据。
2. 必须先通过单篇门禁，之后才允许启动 15 篇 Wiki 编译；单篇失败时不得用全库编译碰运气。
3. `search_wiki_topic_v2` 只接收 topic，返回当前 build 中与 topic 相关、足以支持后续回答的原始 evidence blocks；topic、summary 和规划文字不得进入最终证据包。
4. 两级检索都必须有固定容量护栏：地图内部最多合并 512 个 local groups、保留 512 个 groups 和 2,000 个 child topics；单次查询先取 embedding 排序前 64 groups，保留 embedding 前 16 作为召回保底，并与 group 路由器最多 16 个选择取并集，最多进入 32 groups；随后用“每组至少一个 topic、其余按相关性回填”的配额机制最多展开 80 topics；向证据模型暴露不超过 120 candidate blocks，最终返回 96 blocks／50k tokens。group 查询窗口 32/64 为硬上限的 50%，topic 的 80/2,000 保留充分余量，final bundle 的正常门禁为 48；所有 eligible 与 selected 数量、排序分数和 group 选择理由都进入诊断。达到模型暴露上限或地图高水位时显式失败，不得无记录截断；真实运行后只有在有证据时才能版本化上调。
5. 通用抽取、地图和工具提示词不得包含 c13 七组名称、固定文档 ID、evidence qrels 或目标 block；本切片不生成答案、不调用答案 Judge。

### 错误代价与非对称门禁

| 错误类型 | 具体例子 | 用户影响 | 处理分类 | 指标或阈值 |
| --- | --- | --- | --- | --- |
| 单篇证据漏项 | anchor document 明确包含某类控制边界，工具未返回对应 block | 证明第一阶段或其 Wiki 投影不可用 | 阻断 | anchor 必需证据组必须全中 |
| 单篇噪声过高 | 为覆盖少量证据返回文档的大多数 blocks | 后续工具只能做全篇粗筛 | 阻断 | 单篇证据精度 ≥0.80；无关 token ≤20% |
| 跳过单篇直接全库 | 单篇尚未通过就启动 15 篇编译 | 浪费模型成本和调试时间 | 阻断 | 单篇门禁未通过时全库调用=0 |
| 15 篇核心证据漏项 | 七组核心边界中任一组没有认可证据 | 后续回答必然漏项 | 阻断 | 核心证据组 7/7 |
| 15 篇扩展证据不足 | 扩展边界少于两类 | 跨文档知识面过窄 | 阻断 | 扩展证据组 ≥2/4 |
| 15 篇噪声过高 | 为覆盖七组带入大量 qrels 外 blocks | 上下文被稀释，成本上升 | 阻断 | 精度 ≥0.80；无关 token ≤20% |
| 工具退化为全库过滤 | 一次把全部 topics 或全部 blocks 交给 LLM | 在 1,000 篇规模无法使用 | 阻断 | 模型可见候选必须低于固定预算 |
| 证据不可回源 | 返回 summary、规划文字或不存在的 block ID | 无法核验事实 | 阻断 | 悬空 block=0 |
| 答案耦合 | 为通过检索而生成答案或调用答案 Judge | 检索质量与回答质量无法归因 | 阻断 | answer generation/judge=0 |
| 评测泄漏 | 地图或工具输入含 qrels、固定边界名称或目标 block | 形成按答案检索的假成功 | 阻断 | 敏感字段命中=0 |

### 明确禁止

- 不得把 076–082 理解为与 Wiki 无关的独立产品；它们是 Profile-driven Wiki 的第一阶段。
- 不得直接复用旧 365-topic Wiki、旧 topic IDs 或 `search_wiki_topics` 的返回目录。
- 不得以“候选列表看起来不错”代替 `search_wiki_topic_v2` 的实际证据检索验收。
- 不得在单篇门禁失败后启动 15 篇 Wiki 编译。
- 不得把全部 topic 或全部 block 交给 LLM 做一轮粗筛。
- 不得把 topic summary、区域摘要或内部规划文本当作 evidence block。
- 不得在本切片中生成最终回答、修改问答 Agent 提示词或运行答案 Judge。
- 不得把 c13 七组边界、anchor 文档、qrels 或目标 block 写入通用提示词。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 第一阶段输入缺失或不完整 | `wiki_topic_source_incomplete` 及缺失文档 | 原 Preview Run 与诊断 | 补跑第一阶段 |
| Profile／snapshot 不匹配 | `wiki_topic_scope_mismatch`；不建 Wiki | 原 Profile、snapshot 与 run | 使用一致输入重建 |
| 单篇门禁未通过 | `single_document_evidence_gate_failed`；禁止全库编译 | 单篇候选、Wiki、结果和未命中证据 | 修正抽取或工具后重测 |
| `search_wiki_topic_v2` 无可匹配 topic | `no_match` 与检查范围，不回退全篇扫描 | 查询与地图诊断 | 修复地图或查询 |
| 候选或返回 blocks 超预算 | `wiki_topic_budget_exceeded`；不返回未声明的部分结果 | 完整计数、阶段分布与预算 | 调整地图粒度或预算 |
| evidence 不属于当前 snapshot | `wiki_topic_evidence_invalid`；调用失败 | topic 路径与非法 block | 修复证据绑定 |
| 15 篇阶段输出了答案或调用 Judge | 本次实验无效并显式报告 | trace 与失败原因 | 移除答案链路后重跑 |

### 人工检查点

- 编码前决策已确认：anchor document、单篇 evidence 目标、`search_wiki_topic_v2` 输出契约、两级成本护栏和 15 篇通过线；实现前仍需把单篇及 15 篇 evidence 目标整理为可执行 fixture。
- 单篇门禁后：沛东检查“应有但未返回”的证据、返回块总数/精度，并明确批准是否进入 15 篇编译。
- 15 篇完成后：沛东检查七组核心与至少两组扩展的 evidence 路径，以及返回结果是否仍保持紧凑。

### 决策预算

- B 级决策共 5 项，已全部确认；D4 的预算数值为暂行高水位，真实运行后只能在记录证据的前提下版本化调整。
- 是否存在 C 级决策：否。本切片只新增独立实验 Wiki artifacts、工具和 trace，不删除或覆盖 Profile、Preview Run、collection snapshot 或评测历史。
- 超过预算时的拆分方式：先完成单篇门禁与工具主路径，再拆 15 篇知识地图；不得为了缩减范围而跳过单篇门禁。

## 目标

用最终检索行为反向验收 Profile-driven 单文档抽取：

```text
冻结 Profile + anchor document
→ 082 通道累积
→ 081 文档级标题消歧
→ 单篇 Wiki
→ search_wiki_topic_v2("AI Agent 隔离与控制边界")
→ 单篇所需 evidence blocks
→ 单篇门禁

只有单篇通过：
全部 15 篇
→ 同一 Profile 和第一阶段流程
→ 跨文档 Wiki
→ search_wiki_topic_v2(相同 topic)
→ 跨文档所需 evidence blocks
→ 15 篇门禁
```

验收对象是证据检索，不是答案。

## 输入与输出

### 输入

- `snapshot_id`：`s_6b787258ba7d700c`；
- 冻结 Profile 及其 `profile_hash`；
- 冻结 Profile 的 `domain_context` 与代表问题；它们写入知识地图 manifest，只用于内部查询扩展和 group 判断，不改变 `search_wiki_topic_v2` 只接收 topic 的对外契约；
- 单篇 anchor：`How we contain Claude across products`；
- 单篇第一阶段 artifacts：经过 082 通道累积和 081 文档级标题消歧的 local candidates；
- 单篇 evidence 目标：该文档中回答主题所需的主要证据块及合理替代块；
- 单篇通过后才读取其余 14 篇的第一阶段 artifacts，并构造 15 篇 Wiki；
- 固定 topic：`AI Agent 隔离与控制边界`；
- 15 篇 evidence 目标：七组核心边界、扩展边界和无关 block 的判定材料。

### c13 证据目标

单篇 anchor 的固定证据目标为六类：

1. 文件与工作区边界；
2. 凭证与身份隔离；
3. 工具与行动能力限制；
4. 外部内容与工具结果信任；
5. 人工监督、审批与中断；
6. 配置、hook、MCP 等来源信任。

单篇不要求覆盖并行工作状态、上下文生命周期和子 Agent 委派；这三类由 15 篇阶段的跨文档证据补足。

15 篇阶段使用冻结 case revision `cr_c13_003_fff9fbc82a83` 的七组核心边界和四类扩展边界作为 evidence 组织依据，但不调用答案 Judge。2026-11-09 起，下一份 case revision 草稿收窄第五组：

- 并行工作现场隔离：独立会话、worktree／检出等机制必须限制并行 Agent 的文件修改和任务状态污染；
- 持久状态保存与恢复：progress state、checkpoint、resume 或 rollback 等机制必须限制失败改动不可回退和影响扩散；
- 两个子关系必须同时成立；并行工具调用、提示工程、并行性能提升和终态评估本身不能替代该边界。

对应检索证据基线为 `b00025`（工作现场隔离）加 `b00016` 或 `b00013`（持久状态与恢复）。
该修订在人工审核前保持 draft，不替换上述已审核 revision。

15 篇阶段仍使用七组核心边界和四类扩展边界作为 evidence 组织依据，但不调用答案 Judge：

```text
七组核心必须全部有认可证据
四类扩展至少有两类有认可证据
```

两级 evidence 目标都只用于 `search_wiki_topic_v2` 的检索验收，不建立独立的 Profile 评分系统。

### 输出

- 单篇 Wiki artifact 和 `search_wiki_topic_v2` evidence bundle；
- 单篇门禁结果：必需证据命中、证据精度、无关 token 比例、返回块数和预算；
- 单篇通过后生成的 15 篇 Wiki artifact；
- 15 篇 `search_wiki_topic_v2` evidence bundle；
- 跨文档门禁结果：七组核心、至少两组扩展、证据精度、无关 token 比例；
- 完整 trace：topic 命中、topic 展开、候选 blocks、过滤结果、最终 blocks 和每阶段预算；
- 单篇失败证据：明确记录哪一类应返回证据没有进入最终结果。

## 不包含

- 单文档抽取内核的重新设计；081／082 内部实现不在本切片重复；
- 旧 069–074 Wiki build、旧 365-topic ledger 或 `search_wiki_topics` 的直接复用；
- 用候选列表单独打分作为本切片完成条件；
- narrative、index、document summary 和页面链接；
- 最终答案生成、问答 Agent 提示词修改和答案 Judge；
- 产品 UI、增量构建、1,000 篇真实规模验收和 held-out 泛化；
- 通用“找全能力”或数学意义上的完备性声明。

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 |
| --- | --- | --- | --- | --- | --- |
| A1 | 第一阶段来源正确 | 单篇和 15 篇 Wiki 都只使用当前 Profile 经 082→081 后的 candidates | Profile／snapshot／run hash 不一致 | 不一致=0 | 单元／集成 |
| A2 | 单篇工具可运行 | 固定 topic 可调用 `search_wiki_topic_v2` 并返回原始 blocks | schema、block 来源、empty 语义 | 非法输出=0 | 单元／集成 |
| A3 | 单篇证据覆盖 | anchor 中六类证据全部有认可 block | 以逐 block evidence 目标核对 | 六类证据均命中，连续 3 次无漏组 | 真实运行＋人工 |
| A4 | 单篇噪声可接受 | 返回结果以相关 evidence 为主 | 证据精度、无关 token；成本护栏必须高于正常结果且不得成为事实目标 | 每轮精度 ≥0.80；无关 token ≤20% | 真实运行＋人工 |
| A5 | 单篇门禁有效 | 单篇失败时不启动 15 篇编译 | 全库编译调用计数 | 未通过时调用=0 | 集成／trace |
| A6 | 15 篇证据覆盖 | 七组核心边界均有 evidence，扩展至少两组 | evidence 目标核对 | 每轮核心 7/7、扩展 ≥2/4，连续 3 次无核心漏组 | 真实运行＋人工 |
| A7 | 15 篇噪声可接受 | 相关证据占主导，不全量返回候选 | 证据精度、无关 token；成本护栏必须高于正常结果且不得成为事实目标 | 每轮精度 ≥0.80；无关 token ≤20% | 真实运行＋人工 |
| A8 | 检索有界且护栏不误裁 | candidates 正常值低于高水位护栏，并保留至少约 2 倍余量；异常增长时才触发失败 | groups/topics/blocks/tokens 计数、正常值与护栏距离 | 正常值优于护栏 50%；异常超限即失败 | 单元／集成 |
| A9 | 规模策略可解释 | 15 篇返回规模不因 81→526 blocks 而变成全库候选 | 模型可见候选数 | 超过固定预算=0 | 集成／规模探针 |
| A10 | 无答案耦合 | 两级运行都不生成答案、不调用答案 Judge | 调用与事件检查 | answer/judge=0 | 代码审查／trace |
| A11 | 无评测泄漏 | 通用 Wiki/tool 提示词不含 c13 固定边界、文档或 block | 敏感字段／字符串命中 | 0 | 静态门禁 |

### 验收冻结与变更

- 冻结时点：控制契约审核通过、编码开始前。
- 已确认的阻断反例：单篇失败仍执行 15 篇编译；全 topic／全 block LLM 过滤；topic summary 冒充证据；核心证据漏项；答案 Judge 参与检索通过判定。
- 验收变更记录：暂无。

## 验收执行

- [x] 用户可观察的单篇主路径成立。
- [ ] 单篇门禁通过并经沛东明确确认。
- [x] 单篇门禁通过后才执行 15 篇 Wiki 编译。
- [x] 15 篇证据覆盖和噪声门禁通过。
- [x] 失败路径不会破坏旧状态或伪装成功。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 083-D1 | B | 单篇 anchor 如何选择 | 首轮使用 `How we contain Claude across products`；验证该文档中回答主题所需的证据子集，不要求单篇覆盖全部七组 | c13-003 的主要安全边界证据集中在该文；单篇本来无法提供跨文档全部边界 | 不用与题目弱相关的文档；不要求单篇完成全库验收 | 单篇门禁范围清楚，完整七组留到 15 篇阶段 | A1／A3 | 已确认（2026-11-05） |
| 083-D2 | B | `search_wiki_topic_v2` 输入输出 | 输入只有 `{"topic":"..."}`；输出只包含 topic 回显、完整原始 blocks 和 empty_reason；topic 匹配、区域展开、scores、过滤理由与预算只进 trace/diagnostics | 历史主 Agent 稳定输入是“AI Agent 隔离与控制边界”；工具职责应与答案生成和主 Agent 二次选题分离 | 不接收完整问题、答案计划或 qrels；不向主 Agent 返回 topic 列表、summary 或规划结果 | 工具可复用且可单独评测 | A2／A10 | 已确认（2026-11-05） |
| 083-D3 | B | 单篇 Wiki 如何产生并检索 | anchor 的 076–082 candidates 先形成文档内 topic entries，再构建 local topic groups；15 篇阶段保留各文档 local groups，仅对 group cards 做 global 归并；两级复用同一 `search_wiki_topic_v2` 路径 | 单篇先用最小范围验证抽取与检索；相同地图结构使单篇成功后 15 篇只增加数据规模，不更换代码路径 | 不做单篇扁平候选、15 篇另一套 region 搜索；单篇阶段不做跨文档身份归并 | 单篇结论可迁移到全库阶段，失败归因更清楚 | A2–A5 | 已确认（2026-11-05） |
| 083-D4 | B | 两级 Wiki 检索如何控制候选规模 | 采用固定预算漏斗：地图内部 local groups 与 groups 各 512、child topics 2,000；单次查询先取 embedding 前 64 groups，以 embedding 前 16 保底，并与 group 路由器最多 16 个选择取并集，最终最多 32 groups；每个 group 先保留最相关 topic，再按分数补到 80 topics；向证据模型暴露最多 120 candidates，最终 bundle 96 blocks／50k tokens | v8 真实 15 篇运行产生 801 个 topic 和 469 个唯一证据块；原 240 topic、150 local-group 上限会误裁正常输入，全量展开又会退化为全库过滤。查询组 32/64 为 50%，topic 80/2,000 为 4%；实测正常最终 bundle 37 块，96 的高水位保留约 2.5 倍余量 | 不让 LLM 扫描全部 topics 或全部 blocks；不把 map 容量当成查询目标；eligible/selected、排序分数和 group 选择理由进入诊断 | 能限制异常失控，又避免正常输入被硬上限误裁 | A5／A6／A8／A9 | 已修订（2026-11-05，待确认） |
| 083-D5 | B | 检索通过标准如何定义 | 单篇六类证据全中；15 篇七组核心 7/7、扩展至少 2/4；两级均要求每轮精度 ≥0.80、无关 token ≤20%，连续 3 次不出现必需证据组漏项 | 用户目标是证据找齐且噪声可控，而非答案通过 | 不维护独立 Profile 分数系统；不使用答案 Judge、N/K 或模型自评分 | 需要一次 c13 证据标注，但该标注只服务工具验收 | A3／A4／A6／A7 | 已确认（2026-11-05） |

等级：

- A：AI 可自主执行，交付时列入审计摘要；
- B：完成后集中人工确认；
- C：执行前必须停止并获得人工决定。

## 证据包

### 自动化证据

- 命令：
  - `pytest backend/tests/test_profile_knowledge_map.py`
  - `pytest backend/tests/test_agent_runtime.py backend/tests/test_agent_protocol.py backend/tests/test_wiki_topic_map_identity.py`
  - `pytest backend/tests`
  - `corepack pnpm quality:quick`
  - `ruff check` 与 `ruff format --check`
  - `pyright --pythonpath <shared backend venv> --project backend`
- 结果：083 定向测试通过；完整 `backend/tests` 中出现 1 次与本切片无关的 trace artifact 临时文件竞态（404），单测隔离重跑通过；`quality:quick` 通过（comments、Ruff、Pyright、frontend `vue-tsc`、Vite build、Rust fmt/check/clippy）。
- 覆盖的不变量：地图引用完整性、snapshot hash、Preview Run 版本与文档范围一致性、工具只向 Agent 返回原始 blocks、未就绪与无匹配错误、成本护栏、无 case-specific prompt 泄漏。

### 运行证据

- 启动方式：
  - 使用 `scripts/profile_preview_full_probe.py` 对固定 snapshot 和冻结 Profile 执行全部 15 篇 Preview。
  - 使用 `scripts/wiki_topic_v2_probe.py` 构建知识地图并调用固定 topic。
  - 使用 `scripts/c13_wiki_v2_gate.py` 按 `backend/tests/fixtures/c13_003_wiki_topic_v2_targets.json` 计算覆盖、精度、噪声和预算门禁。
- 用户操作序列：单篇抽取与 Wiki → 调用 `search_wiki_topic_v2` → 审核单篇证据与噪声 → 明确批准 → 15 篇抽取与 Wiki → 同一 topic 调用 → 审核跨文档证据与噪声。
- 预期结果：同一 trace 可还原 topic 到 evidence blocks 的路径；单篇门禁结果决定是否发生 15 篇编译。
- 实际结果：
  - v8 单篇 Preview Run：`pr_8bd488e68aaa4a72`；29 candidates。
  - 最终单篇地图：`km_f864dfe46845eb9b`；29 topics、11 groups、22 candidate blocks、22 selected blocks。
  - 单篇门禁连续 3 次通过：必需 6 组全中；三次均为 22 blocks，证据精度 0.955，无关 token 比例约 1.24%；唯一噪声为 anchor 文档脚注 `b00025`。
  - 15 篇 Preview Run `pr_7f82a7c2ed43413d` 在 `Harness Engineering for Self-Improvement` 的候选抽取窗口返回非法结构；该失败运行未写入部分候选，也未构建 Wiki。
  - 修复为最多两次结构化输出尝试：首次 16,384 output tokens，仅 `candidate_output_invalid` 时追加一次修复请求并使用 24,576 tokens；候选管线版本升至 `profile-preview-pipeline-v8`。
  - 失败文档单独重跑成功（82 candidates），证明失败是该模型调用的输出稳定性问题，不是输入不可处理。
  - 一次并行启动错误导致 `pr_7203fc63cecc4c42` 被产品恢复逻辑标记为 `interrupted`；此后探针默认拒绝在已有 queued／running Preview 时启动，只有显式 `--recover-active` 才允许接管。
  - 顺序执行的最终 15 篇 v8 运行：`pr_ca0dd3ab3f41430f`，完成 15 篇、801 candidates。
  - 最终 15 篇地图：`km_bb58600230baded1`；801 topics、204 groups；Profile 领域背景与代表问题已写入 manifest 并参与内部查询扩展。
  - 15 篇门禁连续 3 次通过：每轮核心 7/7、扩展 3/4；返回 blocks 分别为 37／35／33，精度 0.946／0.914／0.879，无关 token 7.5%／9.1%／12.8%。
  - 三次运行的候选 blocks 分别为 55／57／57，均低于 120；最终 blocks 均低于 48 的正常门禁和 96 的高水位。

### 失败探针

- 注入或复现的失败：单篇必需证据缺失、单篇噪声超限、全库提前编译、topic 无匹配、候选超预算、evidence 悬空、答案 Judge 被调用。
- 系统保留的旧状态：知识地图构建失败不删除 Preview Run 或旧地图；搜索失败不修改地图 artifact。
- 用户看到的错误：`wiki_topic_v2_not_ready`、`knowledge_map_scope_mismatch`、`knowledge_map_snapshot_mismatch`、`search_wiki_topic_budget_exceeded`；预算错误同时保留阶段计数诊断。

### 已知反例账本

| 场景或反例 | 期望行为 | 实际结果／分布 | 是否阻断 | 处理结论 |
| --- | --- | --- | --- | --- |
| 单篇候选看起来完整但工具无法返回关键证据 | 单篇门禁失败，不执行 15 篇 | 最终地图单篇连续 3 次 6/6 证据组命中 | 是 | 通过 |
| 单篇返回绝大多数 blocks | 噪声门禁失败 | 三次均返回 22／22 candidate blocks，精度 0.955、无关 token 1.24% | 是 | 通过 |
| 单篇失败后仍启动全库 Wiki | 明确阻断并记录 | 单篇通过后才启动 15 篇 | 是 | 通过（当前试验流程） |
| 候选模型偶发返回非法结构 | 有界重试一次，仍失败则整次显式失败并保留文档、通道、窗口和请求诊断 | 首次 15 篇运行在文档 8 失败；同文档独立运行成功；最终 v8 完整运行完成 | 否（可恢复） | 已修复并通过完整运行 |
| 15 篇命中七组但返回大量无关块 | 噪声门禁失败 | 三次返回 33～37 blocks，精度 0.879～0.946、无关 token 7.5%～12.8% | 是 | 通过 |
| 工具需要一问一规划、扫描全 Wiki | 规模门禁失败 | 单次只接受 32 groups／80 topics／≤120 candidate blocks；15 篇实际 55～57 blocks 进入筛选 | 是 | 通过 |
| 使用答案 Judge 代替证据覆盖率 | 实验无效 | 两级运行只执行地图分组、查询路由和证据筛选，无答案生成或 Judge 调用 | 是 | 通过 |
| 老 top-240 预算误裁 801 topics | 以真实正常值为依据版本化上调 high-water | 首次地图构建显式失败，未静默截断 | 是 | 已修订为 2,000 |
| 单次 global merge 输入过大导致超时 | 按批归并并保留每条 group 的来源 | 150+ local groups 首次触发，未产生半成品地图 | 是 | 已修复为每批 48 |
| group 判官漏掉高排名但较隐蔽的边界 | embedding 前 16 保底与模型选择取并集 | 连续测试中 context/subagent 组曾漏选 | 是 | 已修复并连续 3 次通过 |

### 人工验收

- [x] 沛东确认 083-D1：单篇 anchor 使用 `How we contain Claude across products`。
- [x] 沛东确认 083-D2：`search_wiki_topic_v2` 只接收 topic，只向主 Agent 返回原始 evidence blocks。
- [x] 沛东确认 083-D3：单篇 local topic groups 与 15 篇 global topic groups 复用同一检索路径。
- [x] 沛东确认 083-D4 原则：采用 high-water 成本护栏；具体 map groups 与 local merge input 已依据 v8 真实运行修订到 512，map topics 修订到 2,000，单次查询限制为 embedding 前 16 与模型前 16 的并集（最多 32 groups）／80 topics，final bundle 依据实测 37 块修订到 96，待集中确认。
- [x] 沛东确认 083-D5：单篇六类证据全中；15 篇核心 7/7、扩展至少 2/4；两级精度 ≥0.80、无关 token ≤20%，连续 3 次无必需证据组漏项。
- [ ] 沛东确认单篇 anchor 的必需 evidence、合理替代 evidence 和无关 evidence。
- [ ] 沛东确认 15 篇阶段的核心组、扩展组和噪声阈值。
- [ ] 沛东明确批准从单篇阶段进入 15 篇阶段。
- [ ] 沛东确认运行链路没有生成答案、没有调用答案 Judge。

## 反向解释

实质性切片必须准备 3～5 道行为问题，由负责人逐题独立回答。AI 每次提问必须说明当前题号和剩余题数。

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么第一阶段不再单独用候选列表打分验收，而要由 `search_wiki_topic_v2` 反向验收？ | Wiki 的目标是支撑检索行为；候选级审核无法证明最终证据覆盖完整，也无法证明返回噪声受控。 | 通过 | 无 |
| R2 | 为什么单篇门禁不能使用七组核心 7/7 作为条件？ | 单篇 anchor 本来就不包含回答跨文档问题所需的全部七组证据；缺组不等于该文档漏提。 | 通过 | 无 |
| R3 | 为什么单篇证据检索失败时不能直接编译 15 篇碰运气？ | 需要的是可解释、可复现的行为闭环，不能靠偶然成功；单篇都不能满足时，全库编译只会浪费 token，且失败无法归因。 | 通过 | 无 |
| R4 | 15 篇工具命中七组证据后，为什么还要检查无关 token 比例？ | 只检查组覆盖会掩盖噪声；噪声会占用上下文并稀释注意力，降低最终回答正确性，同时增加无必要成本。若不看该指标，系统就退化成了把所有库内内容交给模型的路径。 | 通过 | 无 |

全部通过后写入以下门禁标记，才能把切片标记为“已完成”并提交业务代码：

```text
<!-- reverse-explanation-gate: passed 4/4 -->
```

## 协作复盘

- AI 明显加速的环节：待本轮完成后复盘。
- 负责人失去系统模型的环节：待观察。
- 真正发现问题的控制点：先用单篇真实检索验证抽取与 Wiki 投影，再决定是否承担 15 篇编译成本。
- 只有仪式感、没有产生判断价值的流程：待观察。
- 下一次准备调整的一到两项：待观察。

### 本次试行的流程规则

| 规则／假设 | 来源 | 想防住的失败 | 使用成本 | 实际效果 | 下次决定 |
| --- | --- | --- | --- | --- | --- |
| 用最终工具行为反向验收第一阶段 | 用户对候选式验收的修正 | 候选看似正确但无法形成可用证据检索 | 需要单篇工具和证据目标 | 待验证 | 待定 |
| 先单篇、后 15 篇 | 历史全库回放成本与失败 | 单篇尚未成立时浪费全库编译与检索成本 | 多一道人工门禁 | 待验证 | 待定 |

## 已知风险与后续

- 未验证项：单篇 Wiki 能否保留足够的多方面证据；`search_wiki_topic_v2` 的主题路由是否稳定；15 篇跨文档知识地图能否在固定候选预算内取齐证据。
- 接受的风险：本切片不生成答案、不做产品 UI，结论只适用于固定 snapshot、冻结 Profile 和 c13-003。
- 后续待办或 ADR：单篇与 15 篇均通过后，再单独评估答案 Agent、held-out 主题、增量构建和真实规模性能。

## 停止与推翻条件

- 单篇 anchor 中明确存在的必需证据无法由 `search_wiki_topic_v2` 返回时，停止，不进入 15 篇。
- 单篇返回范围接近整篇文档或噪声超过阈值时，停止，不把全篇粗筛当作 Wiki 检索。
- 15 篇阶段模型可见 candidate 数随文档数线性增长时，停止当前地图和工具设计。
- c13 证据只能通过人工挑 topic 或人工补目标 block 才取齐时，本切片不成立。
- 出现答案生成、答案 Judge 或 qrels 泄漏时，本轮实验无效。
