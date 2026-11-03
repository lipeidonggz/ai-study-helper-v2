# 12 Profile-Driven Wiki Extraction

- 状态：第一阶段实现和 076–080 批次验收完成（2026-11-03）
- 开始日期：2026-11-02
- 关联设计：[11 LLM Wiki 编译设计](11-llm-wiki-compilation.md)
- 当前目的：先完成 Profile 自定义、比较和确定闭环；页面、构建、检索和增量后续再定

## 当前阶段目标与范围

当前阶段只建立 Profile 管理和验证闭环，让用户通过界面自定义、比较并确定 Profile：

```text
选择、复制或新建 Profile
→ 配置抽取通道
→ 从已完成入库的文档中选择预览文档
→ 对两个 Profile 执行完整文档预览
→ 比较 candidate 与 evidence
→ 用户判断并明确选定一个 Profile
```

当前阶段允许：

- 查看、空白新建、从系统模板复制或从用户已有 Profile 复制；
- 编辑 Profile 并按确定规则编译为只读 prompt 结果；
- 选择一篇或多篇已完成入库的文档作为预览样本；
- 对多个 Profile 在相同 snapshot、相同文档和相同 block 范围上执行完整文档预览；
- 查看和比较 candidate、summary、source blocks 与确定性检查结果；
- 保存必要的文档级清单信息，用于判断 Profile 差异；
- 将用户选定结果保存为一个明确、不可变、可在后续阶段使用的 Profile。

当前阶段不包含：

- 跨文档 canonical topic 合并与发布；
- catalog／narrative 页面和 topic link 生成；
- 全库 build、active Profile 和 active build 切换；
- topic／page 索引、搜索、问答和 Agent 工具接入；
- 新增文档时的增量抽取；
- 构建后手工新增、删除或改绑知识条目。

因此，本阶段的完成标准不是产出可用于问答的 Wiki，而是：

1. 用户能从已有或自定义 Profile 中形成至少两个可比候选；
2. 比较使用相同的文档与 snapshot，差异能够归因于 Profile；
3. 用户能基于 candidate 与 evidence 作出明确判断；
4. 被确定的 Profile 内容、hash 和来源可追溯；
5. 整个过程不会隐式消耗全库构建 token。

## 背景

现有 11 号设计把局部覆盖槽（coverage slot）作为 candidate 的附属结构。
该结构有助于页面覆盖检查，但同时承担了候选发现、粒度判断和回答面规划，容易造成：

- 独立知识对象被错误降为 facet；
- 不同领域和用户目标被迫使用相同的回答面；
- candidate 身份与页面组织方式耦合；
- 用户教学时难以判断应该修正 candidate 还是 facet。

本轮重新从产品问题出发：

> 用户通过领域模板、任务模板和少量教学样本，定义“在他的场景里什么值得成为知识入口”；
> 系统使用稳定的抽取内核完成证据化候选发现，并生成可搜索、可追溯、可导航的 Wiki。

## 已确认决策

### D1：核心抽取不生成 facet

核心产物只包含：

```text
candidate + evidence
```

不包含：

- coverage slot；
- facet ID；
- facet question；
- 以 facet 为单位的独立检索和身份合并。

页面中的章节标题只是页面呈现层的组织方式：

- 不持久化为独立知识对象；
- 不参与 candidate 身份；
- 不参与证据合法性之外的语义校验；
- 可以随 Profile、页面模板或版本变化。

### D2：所有 canonical topic 都有条目页

首版页面分为两层：

1. `catalog`：所有 canonical topic 都具备；
2. `narrative`：只有证据足够的 topic 才生成。

`catalog` 页面由确定性数据生成：

```text
标题
别名
一句话摘要
Profile 标签
来源文档
直接证据 blocks
相关条目
```

`narrative` 页面由 LLM 在现有 evidence 内组织：

```text
概述
若干段落
每段的 source_block_ids
相关条目
```

页面正文不是事实源。所有事实引用最终仍回到当前 snapshot 的原文 block。

### D3：首版只提供受控的导航链接

导航链接放在独立“相关知识条目”区域，不在自由正文中让模型任意生成 Markdown 链接。

首版只允许两类链接：

1. `mentions`
   - 当前 topic 的证据文本中出现另一个 topic 的 name 或 alias；
2. `shared_evidence`
   - 两个 topic 共享至少一个当前 snapshot 的 evidence block。

两类链接都只表示导航相关性，不代表因果、依赖、比较、父子或其他事实关系。

链接目标必须使用稳定 `topic_id`，不能使用 title、slug 或模型生成 URL。

反向链接在读取时查询，不要求为同一关系额外保存两条记录。

### D4：candidate 与 canonical topic 保持两阶段、两层结构

抽取阶段只产生文档局部 candidate：

```text
document / window
→ local candidate
→ local evidence bindings
```

局部 candidate 在后续全库阶段经过跨文档全局候选归并与身份判定，形成 canonical topic：

```text
全部文档的 local candidates
→ 全局候选归并
→ 跨文档身份判定
→ canonical topic
```

条目页、叙述页、导航链接和检索入口只指向 canonical topic，不直接指向原始 local candidate。

local candidate 继续保留：

- 来源文档和文档版本；
- 原始名称和 aliases；
- source block IDs；
- 抽取 Profile、prompt 和 run；
- 合并、拆分和拒绝记录。

因此两层分别承担：

- candidate：记录“某个文档中观察到了什么”；
- canonical topic：回答“这些观察是否属于同一稳定知识入口”。

### D5：canonical topic 采用分层判同与默认不合并策略

本节描述后续 canonical topic 身份判定。第一阶段 Profile 比较候选的精确去重使用
D39 的更严格规则，不使用 alias 连通合并。

身份解析分为三步：

1. 对 candidate name 和 alias 做确定性归一；
2. 任意 candidate 的完整 normalized name／alias 与其他 candidate 的完整
   normalized name／alias 相等时，自动合并；
3. 仅名称相近的 candidate 形成少量近邻对，再由 LLM 判断
   `same`、`different` 或 `uncertain`。

约束：

- 不使用子串包含关系自动合并；
- 不用共享词语自动合并；
- 不用共享 evidence block 自动合并；
- 不把上下位概念、父机制／实现、同域相关对象视为同一 topic；
- LLM 不生成新 alias；
- `uncertain`、冲突或低置信度结果默认保持分开；
- 人工只复核有歧义的近邻对，不承担全量判同。

该策略的目标是优先避免错误合并。重复 topic 可以在后续复核中合并；
错误合并会污染证据、链接和页面，恢复代价更高。

### D6：Profile 使用与 Profile 编写是两条不同流程

#### 使用已有 Profile

适用于查看已有 Profile、使用样本文档验证效果，以及在当前知识库中确定 selected Profile。

第一阶段流程：

```text
选择 Profile
→ 查看 Profile 范围与说明
→ 可选执行样本文档预览
→ 与其他 Profile 比较
→ 确定使用此 Profile
```

使用已有 Profile 不应要求用户重新教学，也不应修改 Profile 本身。

后续全库阶段可以基于 selected Profile 创建 build，但该行为不属于第一阶段。

#### 新建 Profile

适用于现有模板不能满足领域或用户目标的情况。

目标是让用户通过少量教学建立可重复使用的 Profile：

```text
选择起点
→ 配置抽取通道
→ 保存草稿
→ 可选选择样本文件并执行预览
→ 与其他 Profile 比较并调整
→ 冻结并确定使用
```

两条流程可以复用同一抽取内核和预览组件，但交互、确认强度、失败恢复和产物发布语义不同。

当前阶段切换 selected Profile 必须：

- 只更新知识库的 selected 指针，不自动执行抽取；
- 不覆盖旧 Profile、Preview Run 或比较结果；
- 允许在任意两个 Profile 的 candidate 结果之间比较；
- 只有在用户执行“确定使用此 Profile”后才切换 selected。

后续全库 build 切换仍受 D21 的 active 约束。

### D7：Profile 预览样本文档由用户手选

Profile 不内置用于判断抽取效果的静态示例。用户对 Profile 的有效判断应建立在他
已经熟悉的一篇或多篇真实知识库文档上，而不是根据一组预设正反例推断。

流程：

```text
选择已有 Profile
→ 从已完成入库的文档中手选一篇或多篇样本文档
→ 可选执行预览
→ 用户判断是否继续
→ 可以更换样本文档或跳过预览
→ 参与 Profile 比较或确定 selected Profile
```

约束：

- 只能选择当前知识库中已经完成入库且仍可读取的文档；
- 不自动替用户选择样本；
- 不要求用户编写或选择静态示例；
- 用户可以直接跳过预览；
- 用户可以随时更换样本文档重新预览；
- 从文档详情入口进入时，可以预选当前文档，但用户仍可更换；
- 预览不是为了冻结 Profile、确定 selected Profile 或后续执行全量抽取的必要条件。

### D8：文档预览覆盖整篇文档

用户选中样本文档后，预览必须处理该文档的全部 block，不截取开头若干窗口，
也不使用不完整文档假装代表整篇内容。

预览阶段包括：

```text
选定文档全部 blocks
→ 窗口切分
→ 各抽取通道独立 discovery
→ 可选通道内遗漏补抽
→ 汇总全部通道和窗口结果
→ 精确去重
→ candidate + evidence 预览
```

预览阶段不包括：

- 跨文档身份合并；
- canonical topic 发布；
- catalog／narrative 页面生成；
- topic link 生成；
- 切换 active Profile。

预览结果在满足以下条件时，可以直接复用于后续构建：

- snapshot manifest 相同；
- doc_id 和 doc_version 相同；
- Profile ID 和 hash 相同；
- compiler／prompt／model 配置相同；
- candidate pipeline 和 name normalization 版本相同；
- window policy 和重排参数相同；
- 文档 block 内容 hash 相同。

任一条件变化都必须重新抽取，不能静默复用预览结果。

### D9：已有 Profile 的预览结果保持只读

已有 Profile 的预览只用于判断方向是否正确，不允许在当前 Profile 上直接修改
local candidate。用户可以查看 candidate、summary 和直接 evidence，但不能在这里
逐条修改候选并写回 Profile。

当前阶段的预览完成后可以：

1. `比较其他 Profile`
   - 在相同 snapshot 和文档范围上预览另一个 Profile；
2. `创建或调整清单`
   - 为正式比较准备文档级判断清单；
3. `另存并调整`
   - 复制当前 Profile 为新草稿；
   - 进入新建／编辑 Profile 流程；
   - 原始冻结 Profile 保持不变；
4. `确定使用此 Profile`
   - draft 先冻结；
   - 已冻结 Profile 直接更新 selected 指针；
   - 不触发全库 build。

如果用户在预览中发现漏抽、噪声或粒度问题，只能通过 `另存并调整` 进入 Profile
编辑，不在本次 Preview Run 中静默应用个例修正。

后续阶段的全库 build 出口由 D14、D16 和 D21 管理，不属于第一阶段界面。

### D10：Profile 创建采用“可选语义推导 + 确定性编译”

新建或修改 Profile 时：

- `Profile 名称`最终必填；
- `description` 选填；
- `domain_context` 选填；
- `primary_uses` 选填；
- `representative_questions` 选填；
- `extraction_channels` 最终必填。

`extraction_channels` 有两种产生方式：

1. 用户直接新增和填写抽取通道，此时不调用 LLM；
2. 用户提供足够语义上下文后，由 LLM 生成通道草案，再由用户确认或修改。

只有 `primary_uses` 存在时，系统才提供自动生成抽取策略的入口。
代表性问题可以为生成通道草案提供补充信息。

LLM 只输出结构化 `extraction_channels` 和全局 `exclude_criteria` 建议，不直接生成最终
system prompt。

生成结果只进入待审建议区，不自动保存、不自动覆盖现有通道或排除规则。用户可以逐项
采用、编辑、忽略，也可以取消并保持原草稿不变。

以下操作全部是确定性的：

- 字段校验；
- 默认值合并；
- Profile 结构生成；
- Prompt 模板编译；
- Profile hash；
- 草稿保存。

`granularity` 默认 `standard`，不是首版必填项。首版不提供通用粒度示例，
用户在熟悉文档的预览结果中判断并调整。

### D11：domain_context 是选填的条件性语义上下文

`domain_context` 不由固定领域枚举强制限定，可使用自然语言和多值标签描述。

它用于：

- 参与抽取通道草案生成；
- 辅助判断任务词在领域中的含义；
- 辅助生成全局 `exclude_criteria` 建议；
- 展示 Profile 的适用范围。

它不用于：

- candidate 身份；
- evidence 合法性；
- 固定 schema 或对象类型；
- 覆盖用户明确填写的 `extraction_channels`。

用户直接填写 `extraction_channels` 时，可以不填 `domain_context`。

用户请求系统生成 `extraction_channels` 时，若 `primary_uses` 本身过于宽泛，且没有足够
具体的代表性问题，则必须提供 `domain_context`。例如“安全审查”不能在没有 IT、
建筑、消防、工业等上下文时直接推导出默认的网络安全候选类型。

### D12：任意 Profile 使用文档级清单比较

正式 Profile 比较由一个或多个冻结 `DocumentChecklist` 组成。每个清单绑定具体
文档版本和 snapshot，用于比较任意 Profile 的 Preview Run。

清单包含两类信息：

- 期望保留项：`must`、`should`、`nice_to_have`；
- 明确噪声项：`noise`。

比较结果只展示事实：

- 各级期望项在两侧的 matched／missing／uncertain 数量；
- 两侧命中的 noise 数量；
- 不在清单内的额外 candidate 数量；
- 每个判定对应的 candidate 和 source block。

系统不生成综合质量分，也不自动判定某个 Profile“更好”。用户必须显式给出比较结论：

- A 更好；
- B 更好；
- 基本相当；
- 都不够好，继续调整。

比较结论只记录判断，不自动修改 selected Profile；“确定使用此 Profile”仍是独立操作。

`must` 缺失或仍处于未解决的 `uncertain_match` 属于显著阻断提示。用户仍然可以选择
该 Profile，但必须显式确认忽略并记录原因。

清单一经用于最终比较即应冻结。用户需要修改时，必须基于原清单创建新的
`checklist_id`；旧清单、旧比较和双方 Preview Run 继续保留。

同一文档的反复微调只能证明局部改善。进入后续语义质量评估阶段后，系统应强烈建议
用户在冻结自定义 Profile 前进行 held-out 文档检查，但不将其设为当前功能阶段的
强制门禁。

held-out 检查的篇数、具体文档和执行时机完全由用户决定。系统不得设置最低篇数，
也不得替用户自动选择 held-out 文档。

跳过 held-out 检查的 Profile 仍可冻结和确定使用，但必须标记为：

```text
generalization_review = skipped
```

完成 held-out 检查后标记为：

```text
generalization_review = completed
```

后续语义质量评估记录可以包含：

```json
{
  "calibration_documents": ["doc_..."],
  "heldout_documents": ["doc_..."],
  "heldout_review_note": "用户自行选择 2 篇未参与调参的文档"
}
```

`heldout_documents` 可以为空。系统可以在 review 缺失时提示覆盖风险，
但不能阻止 Profile 冻结或确定使用。

held-out 记录属于后续语义质量评估，不进入第一阶段六类持久对象；当前阶段不要求用户
维护该记录。

### D13：轻量检查只生成证据化疑点，不伪造自动质量结论

轻量检查可分为三层。

#### 确定性检查

程序可以可靠判断：

- source block 是否属于当前 snapshot；
- aliases 是否出现在 candidate 自己的 evidence 中；
- JSON／字段／ID 是否合法；
- 精确规范化名称和 alias 是否重复；
- 是否出现 self-link；
- candidate 是否引用错误文档版本；
- Profile label 是否超出受控枚举（启用 label 的后续阶段）；
- candidate 数量、各 label 数量和 section 分布（启用 label 的后续阶段）；
- 未被任何 candidate 引用的 block 集合；
- 是否出现与 section path 完全相同的 candidate name。

这些结果可以作为硬失败或明确 warning。

#### 启发式检查

程序可以生成“疑似问题”，但不能自动定性：

- candidate 名称接近章节标题；
- 某 section 的 candidate 密度异常；
- candidate 与另一对象的名称高度相似；
- candidate 可能只是一个配置项、端点或单一数据点；
- 两个 candidate 的 evidence 高度重叠，可能重复；
- 两个对象存在父子或实现关系的文字迹象。

启发式检查只产生待审项，不直接删除、合并或拒绝 candidate。

#### LLM 质量建议

LLM 可以基于 Profile 和文档生成：

- 疑似漏抽的 candidate；
- 疑似粒度过粗或过细的项目；
- 疑似错误合并或拆分；
- 对关键区域覆盖不足的提醒。

这里描述的是质量诊断建议，不是 D36 中会补充 candidate 的“遗漏补抽”。

LLM 质量建议必须：

- 只读取当前 snapshot 的 evidence；
- 为每个疑点给出 source block IDs；
- 不把疑点直接写入 candidate map；
- 不把自己的建议当作金标准；
- 明确标记为 `suggestion`。

#### 人工判断

以下问题最终仍需要用户判断：

- 某个对象是否真的具有独立检索价值；
- 某个 candidate 对当前用户是否重要；
- 粒度是否符合预期；
- 两个对象是否应视为同一 topic；
- 某段内容是否确实存在重要遗漏。

轻量检查应优先展示：

- 所有硬失败；
- 少量高风险启发式 warning；
- 未被 candidate 引用的 section 摘要；
- 按 section 分层抽取的少量 candidate；启用 label 的后续阶段再按 label 分层；
- LLM 提出的高置信度遗漏建议。

用户只需确认这些问题是否值得继续处理，不需要给全部输出逐项打分。

以下 D14–D23 保留后续全库构建、页面与 active 使用阶段的设计。第一阶段只执行
已明确标记为当前阶段的 Profile、Preview、Checklist 和 Comparison 决策。

### D14：全库 build 必须由用户显式触发

选择 Profile、查看 Profile 说明、选择样本文档、执行预览或完成 Profile 编辑，
都不会自动启动全库 build。

以下操作不得隐式消耗全库构建 token：

- 打开或切换 Profile；
- 保存 Profile 草稿；
- 完成文档预览；
- 离开或返回设置页；
- 切换页面；
- 应用展示设置。

只有用户明确点击全库构建操作并确认范围、Profile 和预期成本后，才开始 build。

### D15：清单外候选不要求逐项审核

任意两个 Profile 的正式比较中，不属于冻结文档清单的 candidate 标记为
`extra_candidate`。

它不是：

- 抽取失败；
- candidate 的待审核状态；
- 必须逐条人工确认的队列；
- 比清单内候选更低质量或更高风险。

用户可以在冻结清单前从 A／B 结果中挑选合适候选加入清单。冻结并进入最终比较后，
`extra_candidate` 只作为额外信息展示，不阻止选择任一 Profile。

### D16：build 历史不可变、可比较、可回退

每次全库构建生成独立、不可变的 build：

```text
snapshot_id
profile_id + profile_hash
extraction_run_id
canonical topic revision
page/link generation versions
manifest/hash
created_at
status
```

build 状态至少包括：

- `building`；
- `failed`；
- `ready`；
- `active`；
- `superseded`；
- `incompatible`。

规则：

- 任一时刻，只允许一个 build 是 active；
- 构建成功不会覆盖旧 build；
- 构建成功后只进入 `ready`，不得自动成为 active，包括首次构建；
- 切换 active 使用显式、原子化操作；
- 切换后旧 active build 保留为历史版本；
- 如果当前没有 active build，启用前继续保持无 active；
- 用户可以在兼容 snapshot 的 build 之间比较；
- 用户可以将旧 build 重新设为 active；
- 不自动删除构建历史；
- active build 不能直接删除；
- 删除非 active 历史 build 必须显式确认，并保留操作审计；
- snapshot 或必要 artifact 已不可用时，历史 build 只能作为记录查看，不能启用。

首版不自动设置历史保留数量。后续需要清理时，应另行定义基于大小、时间和用户确认的保留策略；
不得静默清理。

### D17：全库 build 不设置逐项人工验收

全库构建完成后，不要求用户逐条审核 candidate，也不要求用户在全库范围内标注
`must`、`should`、`nice_to_have` 或 `noise`。

全库 build 的可用性由以下条件组成：

- 所有确定性技术校验通过；
- failed／partial artifact 不发布；
- manifest、hash、snapshot 和 evidence 引用一致；
- 构建过程和模型 usage 可追溯。

冻结文档清单只服务于 Profile 设计和 Profile 比较，不是全库验收门禁。

用户可以：

- 直接使用搜索、页面和链接；
- 通过抽样报告检查潜在问题；
- 完全跳过语义抽查；
- 在使用过程中发现遗漏或错误。

系统不得把“用户尚未审核”作为阻止 build 启用的状态。

### D18：构建后的手工增删知识条目暂缓实现

后续需要支持用户删除某个知识条目、新增知识条目或调整身份，但该能力暂不纳入
当前切片。

该能力至少涉及：

- manual candidate／topic override；
- evidence 合法性；
- alias 和身份传播；
- 反向链接、页面和搜索索引更新；
- 删除后的 tombstone；
- 重新抽取时保留或撤销用户修正；
- incremental build 与完整 build 的一致性；
- 用户修正与后续 Profile 结果冲突时的优先级。

因此不得用简单的“删除一条记录”或“直接写一个新 JSON”方式提前实现。

该能力登记为 [BL-0016](../development/backlog.md)。

### D19：ready build 允许只读预览，但不影响 active

build 完整成功并进入 `ready` 后，用户可以在启用前检查：

- 构建使用的 snapshot 和 Profile；
- 文档、candidate、canonical topic 和 link 数量；
- 失败与 warning 汇总；
- 与当前 active build 的基础差异；
- 若干 candidate／topic 页面；
- 少量预览搜索。

预览访问必须显式绑定该 `build_id`。在用户执行“启用此 build”前：

- 现有 active build 不变；
- 普通产品问答和默认 Wiki 搜索继续使用旧 active；
- ready build 不写入 active 指针；
- preview 请求不能把 ready build 的 topic 当作现有 active topic；
- preview 页面必须明确标识“尚未启用”。

启用操作和用户预览是两个独立动作。用户可以不预览直接启用，也可以在 ready 状态下
切换比较多个 build，但不能隐式启用。

### D20：全库 build 必须绑定已冻结的 Profile

`draft` Profile 可以用于：

- 字段校验；
- 单文档预览；
- Profile 调整；
- 文档级清单比较。

`draft` Profile 不能启动全库 build。

全库 build 必须绑定：

- `profile_id`；
- `profile_hash`；
- 由该哈希确定性编译出的 prompt version／hash。

冻结后的 Profile 不得原地修改。用户需要继续调整时，必须复制为一个拥有新
`profile_id` 的 draft；完成后冻结为新 Profile。旧 Profile 和历史 build 永远保留，
历史 build 继续引用原 `profile_id` 与 `profile_hash`。

### D21：首版每个知识库只有一个 active Profile

首版的知识库、Profile 与 active build 关系固定为：

```text
一个知识库
→ 一个 active Profile
→ 一个 active Wiki build
```

因此：

- 同一知识库只维护一套 canonical topic；
- 页面和链接只引用当前 active build；
- 普通搜索和问答不需要在查询时选择 Profile；
- Profile 切换生成新的整库 build，由用户手动启用；
- 用户可以在任意两个 Profile 的抽取结果之间比较，但同一时刻只有一个 active。

当前第一阶段只产生 `selected`，不会产生 `active`。selected 表示后续构建的输入；
只有在后续 build 流程成功并显式启用后，才形成 active Profile／active build。

混合领域文档不通过“同一知识库多 Profile”或“逐文档选择 Profile”解决。
首版建议用户建立多个知识库。

同一知识库在运行时使用多 Profile、按文档选择 Profile、Profile overlay 和跨 Profile
topic 合并暂缓，登记为 [BL-0017](../development/backlog.md)。

### D22：新建 Profile 支持空白创建和从已有 Profile 克隆

新建自定义 Profile 时提供三个起点：

1. 从空白开始；
2. 从系统预定义 Profile 克隆；
3. 从用户已有 Profile 克隆。

克隆行为：

- 生成新的 draft；
- 生成新的 `profile_id`；
- 保留 `created_from_profile_id` 用于追溯；
- 不修改来源 Profile；
- 不自动复制来源 Profile 的 selected 状态或 Preview Run；
- 文档清单可以继续复用于新 Profile，但旧 Preview Run 不能作为新 Profile 的结果。

系统预定义 Profile 和其他只读 Profile 不能原地修改。用户选择“调整”时只能创建副本。

界面推荐优先展示可克隆模板，同时保留明确的“从空白创建”入口。

### D23：首版只提供“技术学习与参考”预定义 Profile

首版只提供一个只读预定义 Profile：**技术学习与参考**。

企业流程与操作、研究资料与文献综述、方案对比与决策继续作为后续候选，
首版不实现、不预置，也不以此扩大 Profile 配置和验收范围。

该 Profile：

- 只读；
- 有独立的不可变 `profile_id` 和 `profile_hash`；
- 内置 `domain_context`、主要用途、抽取通道和默认粒度；
- 不包含用于判断质量的静态测试示例；
- 不参与自动选择样本文档；
- 可克隆为自定义 draft，但不能原地修改。

该 Profile 的具体通道配置由 D38 固定。

系统 Profile 作为不可变种子记录写入 `extraction_profiles`，使用稳定的 `profile_id`
和 `profile_hash`。后续系统模板需要变化时创建新的 Profile 记录，不原地修改或删除
可能已被 selected／Preview Run 引用的旧记录。

首版也不预置 IT 安全审查、建筑安全审查、法律合规、医学等细分领域。其领域语义差异
较大，先通过“技术学习与参考”、空白自定义或克隆后调整验证框架，避免用不准确模板
误导用户。

### D24：candidate label 是可选 Profile 扩展

第一阶段不启用 candidate label。本节保留为后续扩展设计。

首版 candidate 不强制输出类型标签。Profile 未配置 labels 时，candidate 只需要：

- name；
- aliases；
- summary；
- source block IDs。

Profile 可以显式定义受控 label 集。只有启用该配置时，candidate 才允许携带 `labels`。

规则：

- label 只能使用当前 Profile 定义的受控值；
- label 可以缺失，不阻止 candidate 成立；
- label 不参与 candidate 身份；
- label 不参与 canonical topic 合并；
- label 不参与 alias 合法性；
- label 不参与页面链接关系；
- label 主要用于 UI 筛选、诊断和评测分层。

如果实际使用证明 label 缺乏价值，应删除该能力，而不是为了字段完整继续生成无意义分类。

### D25：extraction_channels 取代 focus_targets

Profile 不保留独立的 `focus_targets`。用户通过一个或多个职责单一的
`extraction_channels` 同时表达“关注什么”和“按什么边界发现候选”。

首版限制为 1–8 个通道。每个通道至少包含：

- `name`：用户可理解的通道名称；
- `purpose`：这个通道要发现哪一类知识对象；
- `activation`：正文出现什么条件时启用该通道；
- `include_criteria`：必须满足的准入条件；
- `exclude_criteria`：该通道明确排除的情况；
- `granularity_rule`：什么情况下应独立成为一个 candidate；
- `coverage_audit_enabled`：是否对该通道执行遗漏检查。

`activation` 是编译进通道 Prompt 的语义适用条件，由模型结合当前 evidence 判断；
它不是程序级硬过滤，也不会在调用模型前单独执行一次确定性筛选。

通道不携带自定义输出 schema，不生成 facet、关系边或候选类型。所有通道统一输出：

```text
name
aliases
summary
source_block_ids
```

规则：

- 每个通道只承担一个主要抽取视角，不把多个语义目标塞进同一通道；
- 通道名称和说明不是 candidate label，也不进入 candidate 身份；
- 通道顺序只用于展示和组织，不解释为权重或优先级；
- 多个通道发现同一 candidate 时按 D39 的 normalized primary name 规则精确去重；
- 用户可手工新增、编辑、删除和排列通道，也可接受 LLM 生成的通道草案；
- Profile 冻结后修改通道必须创建新 Profile。

### D26：Profile 保存时校验通道与排除规则冲突

draft 保存和冻结／确定使用采用不同校验强度：

保存 draft 时只阻止无法安全持久化或存在明显协议错误的数据：

- 未知字段；
- 非法枚举、类型或超过字段长度限制；
- 通道数量超过 8；
- 无法序列化或包含不允许的嵌套结构。

以下不完整或无冲突条件允许保存为 draft，但会阻止冻结和确定使用：

- Profile 名称为空；
- 通道数量为 0；
- `name`、`purpose`、`activation` 或 `granularity_rule` 为空；
- 通道名称完全重复；
- 同一个通道的 include／exclude 条件完全重复；
- 通道要求的对象与全局排除规则通过规范化名称直接冲突。

语义校验可以识别：

- 通道的 exclude 条件覆盖了该通道全部 include 范围；
- 全局排除规则使某个通道无法产出候选；
- 不同措辞实际表达同一个 include／exclude 条件；
- activation 条件与 purpose 明显互斥。

处理规则：

- 协议错误阻止保存 draft；
- 不完整或结构冲突允许保存 draft，但不能冻结或确定使用；
- 语义冲突允许保存 draft，但 Profile 状态标记为 `conflicted`；
- `conflicted` Profile 不能冻结，也不能被确定为 selected；
- 系统必须展示冲突所在的通道、字段和双方来源，不能自动删除任意一侧；
- 用户必须明确修改、删除或标记为“非冲突”后才能继续；
- 用户对“非冲突”的确认需要形成审计记录；
- 语义校验的 LLM 输出只是冲突建议，不得静默重写 Profile。

### D27：用户不能直接编辑最终 system prompt

用户可在结构化表单中编辑：

- domain context；
- primary uses；
- representative questions；
- extraction channels；
- 全局 exclude criteria；
- granularity；
- 受控的补充说明。

可选 candidate labels 属于后续阶段，第一阶段不允许用户配置。

最终 system prompt 由确定性模板编译，用户只能只读查看，不能直接修改。

不可由 Profile 覆盖的系统级约束至少包括：

- evidence 只能来自当前 snapshot；
- source block IDs 必须真实且不越界；
- 不引入外部知识；
- aliases 必须同指、同层且出现在 candidate 自己的 evidence 中；
- 不生成 facet；
- 不生成 parent／child 或 typed relation；
- 输出字段和 JSON 契约固定；
- 失败必须显式返回，不能静默降级。

因此 Profile 的差异只能来自受控配置，不能来自任意 system prompt 替换。

### D28：允许长度受限的 additional_guidance

Profile 可以提供一个选填的 `additional_guidance` 字段，用于表达结构化字段难以覆盖的
领域约定。

约束：

- 选填；
- 首版长度上限建议为 1,000 字符；
- 只作为 Profile 配置的补充说明；
- 不得覆盖系统级证据、输出、alias、身份和失败契约；
- 不得要求模型引入外部知识或执行 evidence 中出现的指令；
- 不得要求生成 facet、父子关系、typed relation 或任意页面链接；
- 编译 Prompt 时放入低优先级 profile section；
- 修改后必须改变 Profile hash；
- 已有 Preview Run 保持不可变，但不能再作为修改后当前 Profile 草稿的验证结果；
- 已有 Comparison 保留为其绑定 Preview Run 的历史结果，并明确显示所基于的旧 Profile hash；
- 文档清单独立于 Profile，不因 `additional_guidance` 变化而失效。

最终编译 Prompt 必须显式规定：系统契约和固定输出契约优先于 `additional_guidance`。

### D29：草稿可以反复保存和修改

用户创建或从已有 Profile 复制后得到一个 `draft`。草稿用于继续编辑，不表示用户已经
选定该 Profile。

约束：

- 用户可以保存草稿并稍后继续修改；
- 草稿的字段可以任意调整，不产生 Profile 版本；
- 每次保存草稿不调用全库 build，也不隐式执行预览；
- 草稿可以执行单文档或多文档预览，但预览 run 必须记录当时使用的配置 hash；
- 修改草稿后，已有 Preview Run 仍保持不可变，但不能再作为修改后当前草稿的验证结果；
- 已有 Comparison 保留为历史结果，并显示其绑定的旧 Profile hash；
- 文档清单独立于 Profile，不因草稿变化而失效；
- 草稿只有通过校验并由用户执行“确定使用此 Profile”后才冻结。

### D30：确定使用会冻结 Profile 并更新 selected 指针

用户通过“确定使用此 Profile”完成当前阶段的选择。该操作不是构建，也不是发布 Wiki。

执行顺序：

```text
校验 Profile
→ draft 生成最终 profile_hash 并冻结
→ 将当前知识库的 selected_profile_id 指向该 Profile
```

约束：

- 已冻结的系统 Profile 或用户 Profile 被确定时，只更新 selected 指针；
- draft 的确定必须通过结构校验和 channel／exclude 冲突校验；
- 同一知识库只有一个 selected Profile，确定新 Profile 会原子替换旧指针；
- 旧 Profile、旧预览 run 和旧比较结果继续保留；
- 确定操作不执行预览，也不触发全库抽取；
- 没有成功预览时允许确定，但界面必须明确提示“尚未在样本文档上预览”；
- 界面必须提示“该 Profile 将冻结且不可编辑；仍可切换其他 Profile，或复制后创建新的
  草稿 Profile”；
- 冻结后的 Profile 不能原地修改，后续调整必须生成新的 `profile_id`；
- selected 是知识库上的引用，不是 Profile 自身的 `draft／frozen` 状态。

### D31：旧 11 号实现不作为新功能基线

Profile 驱动的候选抽取与页面设计不再兼容或迁移旧 11 号 Wiki 编译实现。旧实现中的
coverage slot、topic identity ledger、topic build、页面生成和 Wiki Agent 工具契约
不进入新功能的数据模型或行为契约。

允许复用的只有与本功能无业务耦合的通用基础设施，包括：

- 文档、collection snapshot 和 block 身份；
- 后台任务状态机、串行 dispatcher、进度、取消和失败恢复的设计模式；
- 前端已有的任务状态展示和轮询／刷新交互模式；
- 模型客户端、API Key 隔离、usage 和 trace 基础设施；
- 桌面／前端应用外壳、页面布局、导航和通用组件。

现有 `BackgroundTaskService` 和 `ingestion_tasks` 表直接绑定了 S0–S6 入库流程，
不作为 Preview Run 的持久模型或服务直接复用。Preview Run 使用独立表和独立 runner，
只复用其状态与调度模式。

约束：

- 不要求旧 Wiki build 数据、表结构或 API 与新功能兼容；
- 不要求新旧两套 Wiki 产物并行运行或进行数据迁移；
- 不复用 coverage slot、facet、旧 canonical topic 身份和旧页面作为新功能起点；
- 复用通用代码前必须通过依赖审查，避免把旧 Wiki 业务语义带入新链路；
- 实际删除旧实现、数据表或历史产物时，必须单独立项，完成依赖盘点、备份和迁移
  影响说明，不能在设计文档修改中顺带执行。

### D32：Profile 编辑界面采用基本信息与高级设置分组

Profile 编辑界面只决定字段的呈现层级，不改变已有 Profile 数据模型的字段语义。

基本信息区域包含：

- Profile 名称；
- Profile 说明；
- 领域或文档范围；
- 主要用途；
- 代表性问题；
- `extraction_channels`，以通道卡片列表呈现，支持手工新增、编辑、删除和排序。

高级设置默认折叠，包含：

- 全局 `exclude_criteria`；
- `granularity`；
- `additional_guidance`；
- 只读的全部通道编译 Prompt。

第一阶段不在界面中暴露：

- candidate labels；
- positive／negative examples；
- 手工拼接或替换最终 system prompt；
- 自定义输出 schema、任意代码和自定义组合后处理；
- 父子关系、typed relation 和任意扩展字段；
- 针对具体文档编写的抽取特例。

页面主操作为“保存草稿”和“确定使用此 Profile”。基础信息之后、抽取通道之前
提供“生成待审建议”入口；只有用户主动点击时才调用模型，根据名称、领域、主要用途
和代表性问题生成可编辑的通道与全局排除条件草案。建议不会自动保存，应用后仍须
用户保存草稿。

每个通道卡片在名称和目的之后提供“补全其余通道属性”入口。只有名称和目的都非空时
可用；模型结合 Profile 上下文生成语义触发条件、包含条件、通道排除条件、粒度规则和
遗漏补抽开关，不修改名称、目的、其他通道或全局排除条件。生成结果先以待审预览展示，
用户确认后才写入当前草稿。

界面将 Preview Run 称为“试抽取”，避免与文档预览或页面预览混淆。该页签必须说明：
用户选择一个 Profile 和少量文档执行真实抽取，检查候选与 evidence，但不生成 Wiki
页面、不修改 Profile，也不改变当前知识库内容。

### D33：文档清单采用三级期望项与独立噪声项

界面统一称其为“质量校验基准”，避免用户把校验标准误解为某次抽取产生的候选清单。
页面必须说明：它独立于被比较的 Profile，冻结后作为判断抽取结果是否覆盖关键知识对象
的文档级标准。

文档清单为每个 `DocumentChecklistItem` 指定一种期望级别：

- `must`：缺失构成显著阻断；
- `should`：应覆盖的重要对象，缺失是有意义的质量损失；
- `nice_to_have`：有价值但不要求覆盖；
- `noise` 单独作为噪声项，不参与三级重要度。

每个清单项必须绑定当前文档版本和 snapshot 中的至少一个 source block。允许的来源：

- 系统基于当前文档生成的初始建议；
- 用户从 Profile A 的 Preview Run 中选择 candidate；
- 用户从 Profile B 的 Preview Run 中选择 candidate；
- 用户手工填写，并手选原文 block 作为证据。

系统生成初始清单时必须独立于两个被比较 Profile，只使用当前文档、当前 snapshot
以及用户显式填写的非 Profile 专属评测说明。不得读取 Profile A 或 Profile B 的
抽取结果，避免清单偏向任一 Profile。

文档正文是不可信数据，清单生成不得执行其中的指令，不得引入文档外知识；每个建议项
仍必须通过 source block 归属校验，并作为用户可修改的草案存在。

清单编辑流程：

```text
系统生成清单草稿
→ 用户调整优先级、删除或补充
→ 可从 A／B 结果中选择 candidate 加入
→ 可手工添加并绑定 evidence
→ 冻结清单
→ 分别应用到 A／B Preview Run
→ 用户作出最终比较结论
```

匹配规则：

- normalized name／alias 完整相等，且 candidate 与 checklist item 至少共享一个
  source block 时，标记为 `matched`；
- normalized name／alias 完整相等，但 evidence 没有交集时，标记为
  `uncertain_match`；
- 名称不同但 evidence 重叠，也只标记为 `uncertain_match` 或作为提示；
- 不把 evidence 重叠自动认定为同一个清单项；
- 一个清单项命中多个 candidate，或一个 candidate 命中多个清单项时，标记为
  `uncertain_match`，不自动计数；
- 用户可以把 uncertain 项显式确认成 matched／missing，并形成审计记录；
- checklist matching 只用于比较视图，不修改 candidate、不执行候选去重，也不写入
  后续 canonical topic 身份。

比较界面按以下顺序呈现：

```text
清单概览
→ must 差异
→ should 差异
→ nice_to_have 差异
→ noise 对比
→ 清单外额外 candidate
→ 逐项查看 source evidence
→ 最终选择
```

冻结清单一经用于最终比较，只能复制成新的清单再调整；不得在保留原比较
结论的同时静默修改清单。

### D34：Preview Run 不可变、显式复用并受引用保护

每次 Profile 预览生成独立的 `ExtractionRun`，首版 `run_kind=preview`。完成后的
Profile 配置快照、candidate 和 evidence 不原地修改。

Preview Run 必须记录：

- `profile_id`、`profile_hash` 和完整的 `profile_config_snapshot`；
- `snapshot_id`、文档范围、文档版本和 block hashes；
- candidate pipeline、name normalization、compiler、prompt、model 和 provider 参数；
- 状态、时间、usage、失败与诊断信息；
- 由 `profile_id + profile_hash`、snapshot、文档范围、系统 Prompt 契约、candidate
  pipeline、name normalization、compiler、模型／provider 配置和 window policy 确定的
  `preview_fingerprint`；并发调度参数单独记录，不进入该 fingerprint。
- completed candidate 必须持久化其 evidence block 的 ID、section path 和正文，保证历史
  run 不依赖当前活动快照也能审阅。

状态至少包括：

- `queued`；
- `running`；
- `completed`；
- `failed`；
- `cancelled`；
- `interrupted`。

复用规则：

- 只有 `completed` run 可以复用或参与正式比较；
- fingerprint 完全相同的成功 run 可以复用；
- 复用必须由用户明确选择“查看已有结果”，不能只凭 Profile ID 静默复用；
- 存在多个相同 fingerprint 的 completed run 时，默认展示完成时间最新的一条，同时
  允许用户在历史列表中选择其他 run；
- 用户选择“重新运行”时始终创建新 run，即使 fingerprint 相同；
- 相同配置多次运行的稳定性检查不得覆盖历史结果；
- failed、cancelled、interrupted 和 partial run 不发布 candidate，也不能参与比较。

历史与删除规则：

- 第一阶段不自动清理 Preview Run；
- 历史列表至少支持按 Profile、文档和时间筛选；
- 列表展示 Profile 名称快照、状态、时间、usage 和是否被 Comparison 或文档清单引用；
- 未被引用的 run 可以由用户显式删除；
- 已被 Comparison Session 或文档清单来源引用的 run 不得直接删除，必须先展示引用
  关系并解除引用或删除上层对象；
- 删除操作必须保留审计记录。

### D35：禁止仅凭共现合成 candidate

系统不得把同一段落、同一窗口或同一 source block 中共同出现，但未被原文明确建立
关系的两个对象，自动组合成一个新的 candidate。

该规则同时禁止：

- 使用“对象 A 的名称 + 对象 B 的名称”机械拼接 candidate name；
- 根据共享 block 自动断言 A 使用了、属于、实现了、依赖或影响 B；
- 用确定性模板生成“A 对 B 的具体应用”等原文没有明确作出的判断；
- 把共现、相同 section 或共享 evidence 当作关系事实；
- 用确定性重复率证明该合成规则正确。

V20 中的 `mechanism_application` 通过同一 window、共享 source block 和名称共现生成
“具名对象＋机制”候选，不包含对明确关系表达的校验，因此不得进入产品实现。

如果需要保留组合型知识入口，只能由独立的 discovery channel 完成，并同时满足：

- 原文明确表达对象与机制之间的关系；
- 该关系不是否定、假设、比较参照或无关并列；
- candidate name 和 summary 都能由同一组 source blocks 直接支持；
- 系统不得仅因两个独立 candidate 共享 evidence 就补造关系。

该规则属于证据契约，不允许被 Profile 或自定义抽取策略覆盖。

### D36：用户配置通道，系统固定执行管线

用户可以通过界面定义职责单一的 `extraction_channels`，但不能改变抽取管线本身。

第一阶段固定管线按文档及窗口执行：

```text
文档切分为窗口
→ 对每个窗口并行或顺序执行各通道
   → 通道 discovery
   → 可选通道内遗漏补抽
→ 汇总该文档全部通道和窗口的候选
→ 按 normalized primary name 精确去重
→ 文档级 Profile 比较候选集
```

约束：

- 独立是指逻辑独立：各 discovery channel 使用同一 snapshot 和同一文档窗口，各自
  编译 Prompt 并独立产生结果，初始抽取阶段不读取其他通道的中间结果；
- 通道可以串行或按受控并发执行，调度方式不改变最终语义；
- 通道内遗漏补抽只读取本通道在相同窗口的 discovery 结果和 evidence，不读取其他
  通道结果；它使用同一通道的 purpose／activation／include／exclude／granularity
  配置，只补充该通道遗漏，不重写已有 candidate，也不得扩展到其他通道的对象范围；
- 全部通道及窗口完成后，才汇总为文档级候选池并执行 normalized primary name 精确去重；
- 精确去重是文档级全局步骤，只执行一次，不由每个通道分别执行；
- 第一阶段不执行语义候选归并，也不自动删除未确认独立的候选；
- 名称不同但可能语义重复的候选保持分开，供 Profile 比较和用户判断；
- 精确去重完成后形成该文档的 Profile 比较候选集，而不是最终 Wiki 候选集；
- 多个预览文档各自独立完成上述流程，不在当前阶段执行跨文档归并；
- 每个通道独立使用固定输出契约；
- 通道运行顺序不作为语义权重；
- normalized primary name 的确定性去重属于系统实现，不由用户定义；
- 遗漏补抽只补充遗漏，不重写已有 candidate；
- 语义候选归并及其筛选规则留到后续阶段单独设计和验证；
- 用户不能定义任意 Prompt、代码、输出 schema 或关系合成后处理；
- 系统级证据、alias、身份、失败和防提示注入契约不能被通道配置覆盖；
- `mechanism_application` 等基于共现的自动组合逻辑不属于允许的通道能力；
- 通道配置必须经过结构校验和预览验证后才能随 Profile 冻结。

### D37：语义候选归并是跨文档全局阶段

语义候选归并不属于单个文档或单个抽取通道，而是后续全库阶段的全局步骤。

输入范围：

- 当前 Profile；
- 当前 snapshot；
- 全部文档产生的 local candidates；
- 每个 candidate 的 source blocks、alias 和通道来源。

目标：

- 合并跨文档指向同一知识对象的候选；
- 识别语义重复但名称不同的候选；
- 判断候选是否具备独立检索价值；
- 修正不同文档间不一致的粒度；
- 保留无法确定的候选为分开状态；
- 为 canonical topic 身份判定提供结构化关系输入。

约束：

- 不能只把全部名称列表交给 LLM 并要求自由合并；
- 先根据名称、alias、evidence 和文档位置形成候选关系或分组草案；
- 只在关系组内判断同一对象、独立对象、局部属性、实现细节等语义；
- 无法确定时保持分开，不能为了减少数量而强制合并或删除；
- 不得新增原文无法支持的 candidate；
- 所有 merge、keep 和 reject 决定必须可追溯；
- 本阶段不进入第一阶段实现，登记为 BL-0018。

### D38：“技术学习与参考”使用四个 discovery channels

系统预定义 Profile“技术学习与参考”包含四个只读通道。用户克隆该 Profile 后可以调整。

#### 通道一：解释性知识对象

- `purpose`：发现正文实质建立、可以脱离当前段落独立学习和检索的解释性知识对象；
- `activation`：正文对某一术语、概念、理论、规则、方法、过程、机制、分类、风险、
  影响、比较框架或评估方式提供实质说明；
- `include_criteria`：存在稳定名称，并具备定义、构成、作用、适用条件、效果、边界、
  比较或独立风险中的至少一项事实；
- `exclude_criteria`：只有名称、章节标题、导航、链接、宣传语或顺带提及；
- `granularity_rule`：离开承载对象后仍能独立解释或检索的对象成为 candidate；属性、
  原因、效果和普通细节留在承载对象的 evidence 中。

#### 通道二：具名技术对象

- `purpose`：发现正文实质讨论并具有稳定专名的技术系统、产品、组件、工具、模型或标准；
- `activation`：正文用稳定专名指代对象，并说明其独立行为、作用、属性、边界、评价或结果；
- `include_criteria`：具有稳定专名，并且该事实不是另一个对象事实的同义复述；
- `exclude_criteria`：仅作为端点、URL、接口调用、配置项、字段、参数、版本标签、
  位置或比较参照出现，且没有独立事实；
- `granularity_rule`：一个具名对象对应一个 candidate；内部实现若本身是有稳定名称、
  独立作用和事实的可复用机制，可以独立成为 candidate，但不能充当所属对象的 alias。

#### 通道三：技术结构模型

- `purpose`：发现正文明确建立的技术结构关系；
- `activation`：正文明确表达威胁／约束模型、架构位置决策或父机制结构；
- `include_criteria`：至少满足以下一项：
  - 针对对象或环境说明所防范的风险、保护边界和残余限制；
  - 说明组件位于何处、从何处迁移、为何这样安排以及代价或效果；
  - 明确命名父机制，并给出实现、组成或具体应用；
- `exclude_criteria`：普通概念、事件叙述、单个数据点、端点、配置项、参数和比较参照；
- `granularity_rule`：一个 candidate 只表达一种结构关系；父机制、具体实现和应用对象
  如需同时存在，保持平级并分别绑定证据。

#### 通道四：机制族

- `purpose`：发现正文明确给出的父机制及其具体实现、组成项；
- `activation`：同一句中先命名父机制，再通过括号、列表或枚举给出至少两个实现或组成项；
- `include_criteria`：父机制可以从同一句中直接识别，且正文给出至少两个具体实现或组成项；
- `exclude_criteria`：普通机制说明、独立控制项、风险、原则、事件、数据点、产品介绍、
  端点、URL、配置项、字段、参数和版本标签；
- `granularity_rule`：父机制和具有独立事实的具体实现分别成为 candidate，不得互为
  alias，不得由共现自动生成“对象＋机制”组合 candidate。

#### 通用设置

- 四个通道的 `coverage_audit_enabled` 首版默认均为 `false`；
- 后续通过同一组文档对照开启遗漏补抽前后的召回、噪声、粒度和 token 成本，再决定默认值；
- `mechanism_application` 不属于任何通道，也不得由固定管线恢复；
- 四个通道共享同一 candidate schema、证据校验和失败契约；
- 通道名称、触发条件和规则是只读预置内容；用户需要修改时必须克隆为新 Profile。

### D39：第一阶段精确去重只使用 normalized primary name

Profile 比较候选集的精确去重只比较 `candidate.name` 的规范化结果。alias 不触发
自动合并。

规则：

- name 归一后完全相同才允许合并；
- 不做子串、词干、相似度或语义匹配；
- 一个 candidate 的 alias 与另一个 candidate 的 name／alias 相同，不得自动合并；
- alias 仍作为候选信息保留，供用户查看和后续语义归并使用；
- 合并后对 source block IDs 和 channel provenance 取有序并集；
- 保留原有 summary variants，不生成新的综合摘要；
- 通过别名间接连接的多个 candidate 必须保持分开，不执行连通分量式合并。

该规则只适用于第一阶段 Profile 比较候选集。后续跨文档全局语义候选归并仍按 D37
单独设计，不继承本阶段的别名合并行为。

### D40：多文档 Profile 比较按文档独立计算

一次 ProfileComparison 可以包含多个 DocumentChecklist，但每个文档必须独立计算和展示
比较结果。

每个文档展示：

- `must`、`should`、`nice_to_have` 在 A／B 两侧的 matched／missing／uncertain 数量；
- A／B 两侧命中的 `noise`；
- 清单外额外 candidate 数量；
- 该文档的用户结论或未结论状态；
- 对应的 candidate 和 source evidence。

约束：

- 不生成跨文档百分比或综合质量分；
- 每个参与正式比较的文档必须且只能绑定一个兼容的冻结 DocumentChecklist；
- checklist 的 doc_id、doc_version 和 snapshot 必须与两侧 Preview Run 一致；
- 不用多数文档自动决定哪个 Profile 更好；
- 不同文档可以得出不同结论，界面必须明确显示结论分歧；
- 任一文档存在 `must` 缺失或未解决的 `uncertain_match` 时，必须在比较顶部显示
  阻断提示；
- 用户忽略 `must` 阻断项时必须显式确认并记录原因；
- 最终总体结论只能由用户选择：
  - A 更好；
  - B 更好；
  - 基本相当；
  - 都不够好，继续调整；
- ProfileComparison 同时保存每文档结果、总体结论、结论理由和参与比较的 hash。

### D41：第一阶段采用独立数据模型和分阶段 API

第一阶段使用独立于旧 Wiki 实现的 Profile 抽取模块，不导入 `app.wiki` 业务模型，
也不依赖 `wiki_*` 数据表。

实现采用的新模块边界：

```text
backend/app/profile_extraction/
├── models.py
├── util.py
├── errors.py
├── system_profiles.py
├── profile_service.py
├── prompt_compiler.py
├── preview_service.py
├── checklist_service.py
├── comparison_service.py
└── api.py
```

第一阶段计划新增六类持久对象：

- `extraction_profiles`：Profile 配置、状态、hash 和来源；
- `knowledge_base_profile_selection`：知识库当前 selected Profile；
- `profile_preview_runs`：不可变预览执行和配置快照；
- `preview_candidates`：预览候选、evidence、摘要变体和通道来源；
- `document_checklists`：文档级判断清单；
- `profile_comparisons`：两个 Preview Run 的比较和用户结论。

API 按三个边界组织：

```text
Profile 管理
GET    /api/extraction-profiles
POST   /api/extraction-profiles
GET    /api/extraction-profiles/{profile_id}
PUT    /api/extraction-profiles/{profile_id}
DELETE /api/extraction-profiles/{profile_id}
POST   /api/extraction-profiles/{profile_id}/clone
POST   /api/extraction-profiles/{profile_id}/validate
POST   /api/extraction-profiles/{profile_id}/select
GET    /api/extraction-profiles/{profile_id}/compiled-prompts
POST   /api/extraction-profiles/strategy-suggestions
GET    /api/extraction-profile-selection

Preview Run
POST   /api/extraction-preview-runs/plan
POST   /api/extraction-preview-runs
GET    /api/extraction-preview-runs
GET    /api/extraction-preview-runs/{run_id}
GET    /api/extraction-preview-runs/{run_id}/candidates
POST   /api/extraction-preview-runs/{run_id}/cancel
DELETE /api/extraction-preview-runs/{run_id}

Checklist 与比较
POST   /api/document-checklists/suggest
POST   /api/document-checklists
GET    /api/document-checklists
GET    /api/document-checklists/blocks?snapshot_id=...&doc_id=...
GET    /api/document-checklists/{checklist_id}
PUT    /api/document-checklists/{checklist_id}
DELETE /api/document-checklists/{checklist_id}
POST   /api/document-checklists/{checklist_id}/freeze
POST   /api/profile-comparisons
GET    /api/profile-comparisons
GET    /api/profile-comparisons/{comparison_id}
POST   /api/profile-comparisons/{comparison_id}/item-decisions
POST   /api/profile-comparisons/{comparison_id}/conclude
```

职责边界：

- 后端负责校验、hash、通道 Prompt 编译、fingerprint、执行调度、复用判断、候选持久化、
  evidence 合法性、清单匹配和比较条件校验；
- 前端负责 Profile 与通道编辑、文档选择、启动预览、展示进度和候选、展示比较结果及提交
  用户结论；
- 前端不得自行拼接 Prompt、计算 fingerprint、修改冻结 Profile、执行跨 snapshot 比较
  或删除 candidate。

补充接口要求：

- 草稿 Profile 支持显式删除，冻结 Profile 不支持删除；
- 未被 Comparison 或 Checklist 引用的 Preview Run 支持显式删除；
- 未冻结且未被 Comparison 使用的 DocumentChecklist 支持显式删除；
- compiled prompts 只读；
- Preview candidates 按文档和通道来源提供分页读取；
- Comparison 的 `uncertain_match` 必须通过 `item-decisions` 由用户显式确认；
- 所有删除、selected 切换、冻结和结论动作必须记录审计信息。

实现建议拆为三个垂直切片：

1. Profile 管理、通道配置和 selected；
2. 单 Profile Preview Run；
3. 文档清单、双 Profile 比较和最终结论。

### D42：清单允许先初建、后补充，并在正式比较前冻结

文档清单的流程固定为：

```text
选择样本文档
→ 用户显式请求生成清单草案
→ 用户初步调整
→ 运行 Profile A／B Preview
→ 用户可从 A／B 结果补充清单
→ 冻结清单
→ 执行正式比较
→ 用户作出结论
```

约束：

- 系统生成清单草案必须由用户显式触发，不得在选择文档时自动调用模型；
- 初始生成只使用当前文档、snapshot 和用户显式填写的非 Profile 专属评测说明，
  不使用 A／B 的抽取结果；
- 生成建议不得自动覆盖已有清单；用户采用建议后才能写入清单 draft；
- 用户可以选择不生成草案，直接手工创建或从 A／B 结果创建清单；
- 从 A／B candidate 添加项目时，自动继承 source blocks 并记录来源；
- 手工添加项目必须绑定当前文档版本中的 source block；
- 查看 A／B 原始候选差异不要求先有清单；
- 创建正式 Comparison Session 前必须冻结至少包含一个项目的清单；
- 冻结清单不能原地修改，调整时必须复制为新 `checklist_id` 并重新冻结；
- 一个 Comparison Session 必须绑定固定 `checklist_id + checklist_hash`；
- 冻结后的清单变化只能用于新的比较，不能改变已有比较结果。

### D43：Preview Run 使用系统窗口参数并采用全有或全无语义

第一阶段由系统管理窗口和并发参数，Profile 和用户界面不能修改：

- 窗口默认最多包含 16 个 block；
- 窗口默认最多 12,000 字符；
- 最大并发调用数固定为 2，并作为执行参数记录；
- 窗口算法版本和参数必须写入 Preview Run；
- 任一窗口算法或窗口参数变化都会改变 `preview_fingerprint`；
- 仅并发调度参数变化不改变语义 fingerprint，但必须记录在实际 run 的诊断字段中。

Preview Run 必须由用户显式启动。启动前界面至少展示：

- Profile、snapshot 和选中文档；
- 文档数、block 数和预计通道调用数；
- 将复用的已有 run，或“将创建新 run”；
- 基于当前输入的长度与 token 估算，并明确标记为估算值；
- provider 是否已配置；未配置时阻止启动并指向设置页；
- 可能产生的 Provider 费用风险。

保存、选择、打开页面、编辑通道、生成清单或确定 Profile 均不得自动启动 Preview Run。

一次 Preview Run 可以包含多篇文档。只有以下条件全部满足时才进入 `completed`：

- 所有选定文档成功；
- 所有启用通道成功；
- 所有启用的遗漏补抽成功；
- source block 范围和 alias 校验通过；
- 每篇文档完成 normalized primary name 精确去重；
- 全部候选和诊断已持久化。

失败语义：

- 任一必需步骤失败时，整次 run 进入 `failed`；
- failed run 不发布正式候选集，不允许参与 Profile 比较；
- 已完成子步骤的 usage、候选和诊断可以保留用于排查，但必须标记为非正式结果；
- 用户再次执行时创建新 Preview Run，不使用半成品补成完成态；
- 首版不提供只重跑失败窗口的局部恢复能力。

取消与中断：

- 用户可以取消整次 run，状态进入 `cancelled`；
- 应用退出导致未完成的 run 进入 `interrupted`；
- cancelled／interrupted run 不自动恢复，也不能参与比较；
- 已完成且 fingerprint 相同的旧 run 仍可被显式复用。

### D44：第一阶段使用两篇文档做功能验收，不评价抽取语义质量

第一阶段端到端验收使用两篇真实技术文档：

- 一篇当前熟悉的固定文档；
- 一篇未用于确定 Profile、通道和界面规则的第二篇技术文档。

两篇文档用于验证产品功能链路：

- 单文档和多文档 Preview Run 均能执行；
- 每篇文档独立处理；
- 所有文档成功时 run 才进入 completed；
- 任一文档失败时 run 不产生正式比较候选集；
- 不同文档的候选不会在当前阶段自动合并；
- 两篇文档都能分别创建和冻结清单；
- 多文档比较按文档独立展示；
- 至少有一个功能探针开启单通道遗漏补抽，验证它只能追加遗漏候选，不能改写已有候选；
- must 缺失提示、noise 对照和最终结论可以正常记录；
- Profile 的保存、克隆、冻结、selected 和比较流程可完整走通。

第一阶段不把以下内容设为核心验收门槛：

- must／should 的抽取召回率；
- 候选数量是否处于某个目标区间；
- 候选粒度是否达到最终产品要求；
- 语义重复未合并是否可接受；
- 具体领域对象的抽取完整度；
- 两篇文档上的“哪个 Profile 抽取质量更好”。

这些属于后续语义质量评估，不在 Profile 管理功能验收中混合判定。

详细的状态组合、失败和受控比较反例可以使用固定 fixture 或 mock provider 验证，
不要求每次都调用真实模型；两篇真实文档用于确认端到端链路可以运行。

与模型质量无关的功能边界仍需严格验证，包括：

- 非法 source reference 为 0；
- 禁止基于共现合成 candidate；
- alias 不触发第一阶段自动合并；
- 跨 snapshot 的 run 不能正式比较；
- 失败、取消和中断状态不能被伪装成 completed；
- 冻结 Profile 和清单不可原地修改。

### D45：Profile 原始候选差异采用保守临时对齐

两个 Preview Run 的 candidate ID 相互独立。原始候选差异视图只建立临时展示对齐，
不修改任一 run 的 candidate。

匹配规则：

- normalized primary name 完整相等的候选自动显示为同一行；
- 名称不同但 alias 相等时，只显示 `alias_match_suggested`；
- 名称不同但 evidence 重叠时，只显示 `possible_overlap`；
- 一个候选对应多个候选时，全部保持分开并标记歧义；
- 不因临时对齐而修改 candidate、不合并 evidence、不触发后续身份判定；
- A only／B only 候选始终保留在差异视图中。

冻结 DocumentChecklist 的匹配规则仍按 D33 执行。原始差异对齐和清单匹配都只是比较视图，
不会写回 Profile 或 Preview Run。

### D46：同一时间只运行一个 Preview Run

当前产品是单用户本地应用，第一阶段使用单 dispatcher 执行 Preview Run：

- 同一时间最多一个 Preview Run 处于 `running`；
- 其他新建 run 进入 `queued`，按创建顺序调度；
- 单个 running run 内部仍可使用 D43 规定的最大 2 路并发；
- queued run 可以取消；
- running run 的取消为协作式取消，在安全 checkpoint 终止；
- 启动应用时，遗留的 queued／running run 统一标记为 `interrupted`，不自动恢复；
- `reuse_if_exact` 只复用 completed run，不复用 queued／running 结果。

该设计复用现有串行 dispatcher 的控制模式，但不复用 `ingestion_tasks` 数据表或
`BackgroundTaskService` 的入库业务实现。

### D47：试抽取反馈与证据翻译

token 计划必须先展示预计总量，再展示输入、输出、模型调用、文档和窗口等明细。
输入 token 按每次调用实际编译 Prompt、窗口正文和固定消息开销估算；输出 token 在首版
按每次调用固定预算估算。界面必须标明这是计划值，实际用量以 Provider usage 为准。

queued／running 的试抽取必须使用持续可见的动态状态，不得只依赖静态状态文字。

试抽取完成后采用“候选列表 + 当前候选详情”的主从布局：左侧按文档分组展示候选，支持
按名称／摘要、文档和来源通道筛选；右侧使用独立区域区分模型归纳与原始证据。试抽取
历史侧栏可以折叠，以扩大候选阅读区，并支持按 Profile／运行 ID 和运行状态筛选。

英文原文块可以按用户显式请求批量翻译为简体中文。候选视图翻译 evidence，原文顺序视图
可以翻译当前 run 文档中的完整 blocks：

- 原文和 block ID 始终保留，翻译只作为辅助阅读，不参与候选抽取、身份合并或比较判断；
- 只翻译属于当前 completed run 文档范围的真实 block；
- 缓存键由翻译 Prompt 版本、Provider 模型 ID 和原文内容 hash 构成；
- 相同缓存键跨试抽取复用，文本或模型变化后自动失效；
- 响应必须区分新翻译与缓存复用数量，失败时不得写入部分伪造结果。

### D48：试抽取同时提供候选浏览和按原文顺序检查

completed run 提供两个结果视图：

- “候选浏览”按文档分组展示候选，适合从候选反查摘要和证据；
- “按原文阅读”按 snapshot 中 block 的原始顺序逐块展示原文，并在同一行右侧显示
  通过 `source_block_ids` 引用该 block 的全部候选。

原文顺序视图必须支持切换 run 范围内的文档、只查看未关联候选的 block，并显示
“已关联 block / 总 block”的覆盖事实。block 没有关联候选只表示当前抽取结果没有建立
引用，不得自动标成漏提或质量失败，最终是否应提取由用户结合原文判断。

从原文块关联的候选可以打开原位详情弹窗或直接加入质量校验基准。详情弹窗不得切换当前
结果视图或丢失原文滚动位置；关闭弹窗后继续在原来的 block 审核。若该 block 已有缓存
翻译，可以在“原文／中文”显示模式之间切换；切到中文时按需补齐当前文档尚未缓存的
英文块，翻译不改变左右两侧的覆盖关系。

同一候选引用多个原文块时，按当前文档的 block 顺序在第一次出现处展示完整候选卡；
后续块只展示紧凑提示“候选名 + 也引用了本段”，并保留查看详情的入口。这样既不丢失
多 evidence 关系，也减少同一候选在长文档中的重复视觉负担。

窄屏下逐块双栏改为原文在上、候选在下的单列结构，不把两个独立滚动列表强行并排。

## 数据模型

当前阶段对象关系：

```text
KnowledgeBaseProfileSelection
              │ selected_profile_id
              ▼
ExtractionProfile
              │
              ▼
ExtractionRun (run_kind=preview)
              │
              └── LocalCandidate[]
                        │
                        └── source_block_ids[]

DocumentChecklist ──────┐
                        ▼
              ProfileComparison
              （绑定两个 completed Preview Run
                和一个或多个冻结 Checklist）
```

本节当前阶段字段是实现输入；CanonicalTopic、TopicPage、TopicLink 和 Review Record
属于后续全库阶段，保留用于说明未来对象边界。

后续完整对象关系：

```text
ExtractionProfile
      │
      ▼
ExtractionRun
      │
      ├── LocalCandidate[]
      │        │
      │        └── source_block_ids[]
      │
      ▼
CanonicalTopic[]
      │
      ├── TopicPage
      └── TopicLink[]
```

### ExtractionProfile

Profile 是“领域 + 用户目的 + 抽取规则”的配置记录。draft 可以修改；冻结后的
Profile 不可变。用户实质性调整已冻结的 Profile 时，系统生成新的 `profile_id`，
不在同一 Profile 下维护版本序列。

```json
{
  "profile_id": "ep_...",
  "name": "技术学习与参考",
  "description": "面向技术文档，保留可独立学习和检索的概念、机制、风险与关键对象。",
  "domain_context": "软件、系统、工具、工程方法或相关技术材料",
  "primary_uses": [
    "学习一个新主题",
    "理解机制和原理",
    "查询操作方法与限制"
  ],
  "representative_questions": [],
  "extraction_channels": [
    {
      "channel_id": "ec_...",
      "name": "核心概念与机制",
      "purpose": "发现正文实质建立、可独立学习和检索的概念、机制、方法或过程。",
      "activation": "正文对对象给出定义、构成、作用、条件、效果或边界时启用。",
      "include_criteria": [
        "存在稳定名称",
        "正文对该对象提供了独立于其他对象的事实"
      ],
      "exclude_criteria": [
        "只有章节标题、链接或顺带提及"
      ],
      "granularity_rule": "一个可独立学习和检索的知识对象对应一个 candidate。",
      "coverage_audit_enabled": false
    }
  ],
  "exclude_criteria": [],
  "granularity": "standard",
  "additional_guidance": "",
  "language_policy": {
    "name": "preserve_source_term",
    "summary": "zh-CN"
  },
  "created_from_profile_id": null,
  "status": "draft",
  "created_at": "...",
  "updated_at": "...",
  "frozen_at": null,
  "profile_hash": "..."
}
```

约束：

- `extraction_channels` 是用户意图和通道边界的唯一结构化来源；
- 每个通道使用同一 candidate schema，不携带自定义 Prompt 或输出字段；
- `exclude_criteria` 是应用于全部通道的结构化排除规则；
- `status` 只允许 `draft`、`frozen` 和 `conflicted`；
- candidate labels、positive／negative examples 和自定义输出字段不属于第一阶段
  Profile schema；
- 普通用户调整 Profile 时必须生成新 Profile；
- draft 的 `profile_hash` 表示最近一次成功保存的内容；每次保存重新计算；
- 冻结时固定最终 `profile_hash`，冻结后的 Profile 不能原地修改。

`selected_profile_id` 存放在知识库级配置上，不写入 Profile，也不作为 Profile 的
`draft／frozen` 状态。一个 Profile 被 selected 后仍然可以保持原状态记录，selected
只表示当前知识库选择了它。

### Knowledge Base Profile Selection

当前产品只有一个知识库，selection 使用单行记录：

```json
{
  "knowledge_base_id": "default",
  "selected_profile_id": null,
  "selected_profile_hash": null,
  "selected_at": null
}
```

约束：

- 新知识库可以没有 selected Profile；
- `selected_profile_id`、`selected_profile_hash` 和 `selected_at` 必须同时为空或同时非空；
- selected Profile 必须是 `frozen` 状态；
- 更新 selection 是单事务原子操作；
- selection 变化不删除旧 Profile、Preview Run 或 Comparison；
- selection 变化不自动执行 Preview 或全库 build。

### ExtractionRun

一次 Profile 在固定 snapshot 上的抽取执行。

```json
{
  "run_id": "er_...",
  "run_kind": "preview",
  "snapshot_id": "s_...",
  "snapshot_manifest_hash": "...",
  "profile_id": "ep_...",
  "profile_hash": "...",
  "profile_config_snapshot": {},
  "candidate_pipeline_version": "...",
  "name_normalization_version": "...",
  "compiler_version": "...",
  "prompt_version": "...",
  "prompt_hash": "...",
  "provider_profile": {},
  "window_policy_version": "...",
  "window_policy": {
    "max_blocks": 16,
    "max_chars": 12000
  },
  "execution_policy": {
    "max_parallel_calls": 2
  },
  "document_scope": ["doc_..."],
  "document_version_ids": ["doc_...#v0001"],
  "block_hashes": {},
  "preview_fingerprint": "...",
  "status": "completed",
  "created_at": "...",
  "started_at": "...",
  "completed_at": "...",
  "usage": [],
  "diagnostics": []
}
```

Run 的作用是固定输入、配置和模型版本。任何 Profile 或 snapshot 变化都必须产生新 run。

### Local Candidate

Local candidate 是某一篇文档或某个窗口中被发现的局部知识对象。

```json
{
  "candidate_id": "wc_...",
  "run_id": "er_...",
  "profile_id": "ep_...",
  "snapshot_id": "s_...",
  "doc_id": "doc_...",
  "doc_version": 1,
  "name": "OS-level sandbox",
  "normalized_name": "os level sandbox",
  "aliases": [],
  "summary": "操作系统层提供隔离边界的沙箱机制。",
  "summary_variants": [
    "操作系统层提供隔离边界的沙箱机制。"
  ],
  "source_block_ids": [
    "doc_...#v0001#c1#b00008"
  ],
  "channel_ids": ["ec_..."],
  "content_hash": "...",
  "created_at": "..."
}
```

约束：

- `candidate_id` 在 extraction artifact 内唯一，不作为跨 run 的最终身份；
- 所有 `source_block_ids` 必须属于当前 snapshot 和当前文档版本；
- `aliases` 必须出现在 candidate 自己的 source blocks 中；
- `channel_ids` 只用于诊断来源，不参与 candidate 身份；
- `summary_variants` 只在精确去重合并时合并去重，不生成新摘要；
- 不包含 coverage slot、facet、parent ID 或 typed relation；
- 第一阶段不包含 candidate labels。

### Document Checklist

文档清单绑定文档版本和 snapshot，独立于被比较的 Profile。

```json
{
  "checklist_id": "dc_...",
  "snapshot_id": "s_...",
  "doc_id": "doc_...",
  "doc_version": 1,
  "status": "draft",
  "evaluation_note": "",
  "items": [
    {
      "item_id": "dci_...",
      "name": "...",
      "aliases": [],
      "priority": "must",
      "source_block_ids": ["..."],
      "origin": "system_suggested",
      "origin_profile_id": null,
      "origin_preview_run_id": null
    }
  ],
  "checklist_hash": "...",
  "created_at": "...",
  "frozen_at": null
}
```

约束：

- `priority` 只允许 `must`、`should`、`nice_to_have` 和 `noise`；
- `evaluation_note` 为选填的非 Profile 专属评测说明；清单生成时不得读取任一 Profile；
- 所有 item 必须绑定当前文档版本和 snapshot 中的 source block；
- `origin` 只允许 `system_suggested`、`from_preview` 和 `manual`；
- 从 Preview Run 选择 candidate 加入时，记录来源 Profile 和 run；
- 清单草稿可以修改，冻结后不能原地修改；
- 最终比较必须绑定不可变 `checklist_id + checklist_hash`；
- 修改冻结清单必须创建新的 `checklist_id`，旧清单和旧比较继续保留。

### Profile Comparison

Profile Comparison 绑定两个 completed Preview Run 和一组冻结 Document Checklist。

```json
{
  "comparison_id": "pc_...",
  "snapshot_id": "s_...",
  "run_a_id": "er_...",
  "run_b_id": "er_...",
  "profile_a_id": "ep_...",
  "profile_b_id": "ep_...",
  "checklist_refs": [
    {
      "checklist_id": "dc_...",
      "checklist_hash": "..."
    }
  ],
  "status": "concluded",
  "document_results": [
    {
      "doc_id": "doc_...",
      "doc_version": 1,
      "checklist_id": "dc_...",
      "checklist_hash": "...",
      "profile_a": {
        "must": {"matched": 0, "missing": 0, "uncertain": 0},
        "should": {"matched": 0, "missing": 0, "uncertain": 0},
        "nice_to_have": {"matched": 0, "missing": 0, "uncertain": 0},
        "noise_hits": 0,
        "extra_candidate_count": 0
      },
      "profile_b": {
        "must": {"matched": 0, "missing": 0, "uncertain": 0},
        "should": {"matched": 0, "missing": 0, "uncertain": 0},
        "nice_to_have": {"matched": 0, "missing": 0, "uncertain": 0},
        "noise_hits": 0,
        "extra_candidate_count": 0
      },
      "item_results": [],
      "user_outcome": null,
      "must_override_reason": null
    }
  ],
  "outcome": "a_better",
  "outcome_note": "...",
  "comparison_hash": "...",
  "created_at": "...",
  "completed_at": "..."
}
```

约束：

- 两个 run 必须均为 `completed`；
- 两个 run 必须满足同一 snapshot、文档范围、系统 Prompt 契约、candidate pipeline、
  name normalization、Prompt compiler、模型／provider 参数和 window policy；
- Profile 编译产生的具体 Prompt 内容可以不同，但必须使用同一编译器与系统级不可覆盖
  契约；
- checklist 必须已冻结；
- `document_results` 按文档保存，不生成跨文档综合质量分；
- `item_results` 必须记录每个清单项在 A／B 两侧的
  `matched／missing／uncertain_match`、命中的 candidate 和用户覆盖决定；
- `outcome` 只允许 `a_better`、`b_better`、`equivalent` 和 `both_insufficient`；
- `status` 只允许 `draft` 和 `concluded`；只有全部文档产生结果后才能进入
  `concluded`；
- `status=draft` 时 `outcome` 为空；`status=concluded` 时 `outcome` 必填；
- 每篇文档可以提供独立的 `user_outcome`，不同文档的结论允许不同；
- 任一 `must` 缺失被忽略时，对应文档必须填写 `must_override_reason`；
- 最终 `outcome_note` 必须汇总存在 `must` 覆盖的文档及原因；
- comparison 创建后不得因 Profile 或 checklist 后续变化而改变；
- comparison 进入 `concluded` 后不可修改；需要改变结论时创建新的 comparison。

### Candidate-to-Topic Assignment

Local candidate 通过独立记录映射到 canonical topic。这样可以在不修改原始 candidate
的前提下重跑身份判定或回滚合并。

```json
{
  "assignment_id": "ca_...",
  "run_id": "er_...",
  "candidate_id": "wc_...",
  "topic_id": "wt_...",
  "decision": "same",
  "method": "exact_name_alias",
  "review_status": "auto_accepted",
  "created_at": "..."
}
```

首版 decision 至少包括：

- `same`；
- `uncertain_keep_separate`；
- `different`；
- `rejected`。

是否允许 `topic_id=null` 表示“暂未归入 canonical topic”，在实现前确认。

### Canonical Topic

Canonical topic 是跨文档合并后的稳定知识入口。

```json
{
  "topic_id": "wt_...",
  "canonical_name": "OS-level sandbox",
  "normalized_name": "os level sandbox",
  "aliases": [],
  "summary": "操作系统层提供隔离边界的沙箱机制。",
  "labels": ["mechanism"],
  "source_doc_ids": ["doc_..."],
  "evidence_block_ids": ["..."],
  "candidate_ids": ["wc_..."],
  "identity_status": "auto_merged",
  "identity_version": 1,
  "created_at": "...",
  "updated_at": "..."
}
```

约束：

- 页面、链接和检索入口只使用 `topic_id`；
- canonical topic 不代表额外事实，只是 evidence／candidate 的稳定归并视图；
- canonical name 可以调整，但 `topic_id` 不随展示名称变化；
- 上位、下位、实现、成员和相关对象不得仅因共现或相似自动合并。

首版按 Profile 隔离 topic 身份。每个 Profile 的抽取结果拥有自己的 canonical
topics、`topic_id`、页面和链接，不跨 Profile 继承 `topic_id`。Profile 比较时只建立
临时对应视图，不把对应关系写回任一 Profile 的正式 topic 身份。

### Topic Page

页面是 canonical topic 的展示投影，不是事实源。

```json
{
  "page_id": "wp_...",
  "build_id": "wb_...",
  "snapshot_id": "s_...",
  "topic_id": "wt_...",
  "content_level": "catalog",
  "title": "OS-level sandbox",
  "aliases": [],
  "summary": "操作系统层提供隔离边界的沙箱机制。",
  "labels": ["mechanism"],
  "sections": [],
  "source_doc_ids": ["doc_..."],
  "evidence_block_ids": ["..."],
  "page_hash": "...",
  "created_at": "..."
}
```

`narrative` 页面可以有：

```json
{
  "sections": [
    {
      "heading": "作用与边界",
      "paragraphs": [
        {
          "text": "...",
          "source_block_ids": ["..."]
        }
      ]
    }
  ]
}
```

约束：

- `catalog` 页面由确定性数据生成；
- `narrative` 页面只能使用该 topic 的 evidence；
- sections 不是 facet，不具有独立 ID 或身份；
- 页面中的事实引用必须回到 snapshot block；
- 页面读取时重新校验 page identity、hash 和引用范围。

### Topic Link

链接是 canonical topic 之间的导航边。

```json
{
  "link_id": "wl_...",
  "build_id": "wb_...",
  "from_topic_id": "wt_a",
  "to_topic_id": "wt_b",
  "kind": "mentions",
  "source_block_ids": ["..."],
  "created_by": "deterministic",
  "status": "active",
  "created_at": "..."
}
```

首版 kind：

- `mentions`：A 的 evidence 中出现了 B 的 name 或 alias；
- `shared_evidence`：A 和 B 共享当前 snapshot 中的 evidence block。

约束：

- 链接不代表因果、依赖、比较或父子关系；
- 链接目标必须是当前 build 中存在的 canonical topic；
- 不允许 self-link；
- 不允许模型生成 URL 或未知 topic ID；
- 反向链接通过读取反向索引生成，不要求存储两条边；
- 后续如果增加模型选择的相关链接，也只能从 allowed topic IDs 中选择。

### Review Record

人工接受、拒绝、合并、拆分和补充都必须形成独立审计记录。

```json
{
  "review_id": "wr_...",
  "target_type": "candidate",
  "target_id": "wc_...",
  "action": "reject",
  "reason": "该对象只适合作为另一个条目的证据，不值得独立检索。",
  "actor": "user",
  "before": {},
  "after": {},
  "created_at": "..."
}
```

Review Record 不直接覆盖数据；后续通过显式 assignment、topic revision 或
新 Profile 应用结果。

## 当前阶段设计状态

第一阶段已按 076–080 五个切片实现：

- Profile 管理、通道编辑、编译 Prompt 和 selected；
- 不可变 Preview Run、四通道抽取、可选遗漏补抽和两文档独立结果；
- 文档清单、双 Preview Run 比较和 must 门禁。
- 试抽取成本反馈、运行中动效和带持久缓存的英文证据翻译。
- 候选主从浏览和按原文顺序的 block／候选覆盖检查。

当前不再存在产品设计阻塞。自动化证据和待批次确认的 B 级决策记录在对应切片文档中。

## 后续阶段待定

- 全局语义候选归并的分组、筛选、粒度修正和审计规则；
- canonical topic 合并、冲突处理和发布；
- catalog 与 narrative 的生成阈值及证据校验；
- `mentions` 和 `shared_evidence` 的排序、去重、数量和消歧策略；
- candidate、canonical topic、page、link 和 index 的最终存储结构；
- 全库 build、active 指针、切换和回退流程；
- 检索与问答如何组合 topic 入口、页面和原始 block；
- 新文档加入、旧文档修改或删除时的增量构建；
- 构建后手工新增、删除和改绑知识条目；
- Candidate labels 的最终受控取值；
- canonical topic 不确定性在用户界面中的呈现与复核流程；
- 跨领域评测集和产品级检索问答验收门槛。

## 非目标

- 本轮暂不实现 facet；
- 暂不建立 parent／child 层级；
- 暂不建立 typed knowledge graph；
- 暂不允许模型自由生成页面 URL；
- 暂不把页面正文当作事实来源；
- 旧 11 号实现不再作为新功能基线；物理删除在独立清理事项中处理。
