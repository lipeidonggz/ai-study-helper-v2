# 043 引用标注的结构过滤与来源元门禁

- 状态：已完成
- 开始日期：2026-10-27
- 重做日期：2026-10-28
- 关联设计：[06 检索与 Agent](../../design/06-retrieval-pure-agent.md)、[02 Agent 开发方法](../../design/02-how-to-build-an-agent.md)
- 前置切片：[027 Claim-aware 引用标注](./027-claim-aware-citation-annotation.md)、[038 引用证据全文覆盖与截断片段](./038-citation-evidence-full-text-merge.md)
- 真实产品验证：`run_11800cba865142bdb16cc8dabfd1bd64`
- 新判官 revision：`evalrev_5226a64a321a468db41f83509d0db8fc`

## 控制契约

### 背景与失败场景

- 触发问题：来源覆盖数量、来源缺失、来源结构和回答过程说明无法由普通内容 block 直接支持，却会进入 claim 级引用标注，产生错误绑定或判官 pending。
- 不处理时的用户损失：用户无法区分知识正文引用与来源统计、来源缺失、回答自述；人工复核成本上升。
- 现有流程为什么不足：Markdown 标题和导航原先会进入 span；自然语言分类又容易把“文档称 X”或“多来源关系”误当成来源元描述。
- 本切片负责什么：确定性过滤真正的非正文结构；以高精度门禁排除少量来源元描述；其余 span 一律进入正文引用标注。
- 本切片不负责什么：不选择证据、不判断引用支持性、不修改正文、不改变 Agent 回答质量、不修改 citation judge。
- 停止或撤掉条件：如果同类提示词连续多轮仍在正文错杀与元描述漏放之间振荡，停止继续润色，重新拆分职责或撤掉该门禁。

### 用户可观察目标

标题、表头和导航不产生引用；来源元描述不绑定普通正文 block；普通正文不会因为门禁被静默跳过，仍进入后续引用标注与判官。产品运行中可追溯门禁版本、规则版本、逐 span 决定、原因和模型成本。

### 核心不变量

1. 标题、表头、导航由 Harness 按 Markdown 结构确定性排除，完整粗体知识命题不得被当成标题删除。
2. 任何含实质事实、观点、方法、分类、关系、建议或结论的 span 默认进入正文；来源位置、回答提纲等外壳不能覆盖其中的内容命题。
3. 语义模型不能稳定区分的“纯位置”和“纯回答过程”改由 Harness 的窄泛化规则判定；普通提纲、能力边界、缺少输入和追问一律默认正文。
4. 来源覆盖统计、来源缺失、来源结构和回答过程之外，门禁只允许高置信度、可由规则直接解释的排除。
5. 门禁协议失败、预算不足或恢复失败时，记录门禁 pending 并回退到全部 span；最终 citation 状态只由 annotator 决定，禁止因预筛失败移除全部用户引用。

### 错误代价与非对称门禁

| 错误类型 | 具体例子 | 用户影响 | 处理分类 | 指标与阈值 |
| --- | --- | --- | --- | --- |
| 正文错杀 | “作者把防御分成三个组件”被当作来源结构 | 绕过正文引用标注和判官，形成静默漏引 | 阻断 | 已确认 body 金标准在 N=10 中错杀为 0 |
| 元描述漏放 | “来源没有出现该说法”进入正文标注 | 可能多引用或 pending，但后续可见 | 可容忍 | 记录次数；全部已知 span 达到 K=9 |
| 结构误删正文 | 完整粗体知识命题被当成标题 | 静默漏引 | 阻断 | 结构金标准误删为 0 |
| 门禁协议失败 | 两次输出均不满足 schema 或 span 覆盖 | 预筛失效，但正文引用仍应工作 | 可降级 | 记录 pending + fallback；annotator 继续 |

### 明确禁止

- 在最终验证期间修改分类目标、金标准或 N/K 口径来制造通过。
- 写用例专属短语、文档标题或 c6 等现场摘要进提示词。
- 只要出现“文档”“本文”“回答”等词就排除，而不检查 span 的核心命题。
- 把普通开场、过渡、提纲、能力边界或缺少用户输入当作回答过程元描述。
- 在门禁输出不完整时继续调用 annotator，或把失败伪装为 complete。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 结构标题或导航 | 该段无引用 | 正文与正文 annotations 不变 | 是 |
| 高置信度来源元描述 | 该段无引用 | span 决定、category、reason、版本与 hash | 是 |
| 门禁两次协议失败 | 正文与引用继续生成，显示预筛降级提示 | 正文、门禁原始输出、fallback 标记、调用与预算审计 | 是 |
| annotation 失败 | 正文正常显示，引用状态 pending | 正文、门禁与 annotator 审计 | 是 |

### 人工检查点

- 编码前冻结：正文错杀为阻断错误，元描述漏放可容忍；无法确定时默认正文。
- 提示词迭代：必须查看 `reason`，每次只验证一个主要语义假设。
- 产品接入前：分享同一提示词、schema、解析、重试、预算和 Harness 合成规则。
- 完成后：沛东复核金标准 v6、提示词 v8、规则 v4 以及 c6 判官 pending 的实际语义。

### 决策预算

- B 级决策：5。
- C 级决策：否。
- 超预算时拆分：Markdown 结构过滤、语义门禁、产品等价实验通道、产品运行接入和评测判官分别拆分。

## 目标

1. Harness 确定性过滤标题、表头、导航和纯结构性列表标题。
2. 语义门禁只做高精度排除，默认放行正文；正文错杀是阻断错误。
3. 实验与产品共用同一分类器、提示词、schema、解析、重试和预算语义。
4. 分类 reason、prompt/rule 版本、hash 和排除数量进入 artifact 与 trace。
5. 在 c6 和 18-case 上验证 N=10、K=9，并接入产品链后重跑 c6 与判官。

## 固定分类目标

### 输入

- `question`：用户问题。
- `answer`：已经冻结的完整回答。
- `spans`：经 Markdown 结构过滤后的正文 span，包含 `span_id`、`structure_role` 和 `text`。

### 模型语义槽

模型对每个 span 返回且只返回一次：

```json
{
  "span_id": "span_0001",
  "has_content_claim": false,
  "source_relation": "none",
  "answer_relation": "none",
  "reason": "简短分类依据"
}
```

- `has_content_claim`：是否包含普通内容 block 可直接支持的内容命题。
- `source_relation`：`none`、`coverage_stat`、`coverage_not_appear`、`location`。
- `answer_relation`：`none`、`answer_process`。

### Harness 合成优先级

1. `coverage_stat` → `source_coverage_stat`。
2. `coverage_not_appear` → `source_coverage_not_appear`。
3. `has_content_claim=true` → body，其他外壳不覆盖正文。
4. `answer_process` 仅在文本含明确回答过程/证据/标注信号时 → `answer_provenance`。
5. `location` 仅在文本是不含实质内容的纯位置指针时 → `source_structure`。
6. 其余一律 body。

该合成顺序把高精度责任放在 Harness，避免模型在“位置+内容”“回答组织+内容”之间反复振荡。

### 默认与错误边界

- 不确定时返回 body。
- 纯标签、提纲、能力边界、缺少输入和追问默认 body，由后续 annotation 自然跳过。
- 来源覆盖统计和来源缺失作为例外，可由模型的高置信度语义槽排除。
- 模型只能提供语义信号，最终 `source_meta` 决定由 Harness 固定推导。

## 输入与输出

- 实验输入来自冻结 evaluation run 的 question、answer 与确定性 spans。
- 产品输入同样来自冻结正文，门禁通过后才把放行 spans 交给 annotator。
- 输出保存逐 span decision、category、reason、协议错误、模型调用和 token。
- `CitationAnnotationArtifact.source_meta` 保存门禁状态、prompt version/hash、rule version、调用摘要和逐 span 决定。

## 不包含

- 不修改主 Agent 回答提示词。
- 不改 citation judge 的通过标准来消除 pending。
- 不把实验脚本复制成另一套产品逻辑。
- 不为单个 c6/c14/cf-003 短语添加特例。

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 |
| --- | --- | --- | --- | --- | --- |
| A1 | 结构过滤正确 | 标题、表头、导航不进入；正文粗体命题保留 | 结构金标准 | 0 误删、0 漏过滤 | 单元测试 |
| A2 | 门禁分类稳定 | 每个已知 span 满足冻结 target | c6 与 18-case，N=10、K=9 | 全部 span ≥9/10 | 真实模型回放 |
| A3 | 不静默错杀正文 | body 金标准不得被排除 | 已确认正文反例 | 0 错杀 | 真实模型回放 |
| A4 | 协议可恢复 | 两次尝试内得到完整合法输出 | schema、span 集合、截断 | 最终协议错误为 0 | 单元 + 真实模型 |
| A5 | 产品等价 | 实验与产品共用 classifier 和门禁合成 | 管线契约测试 | 无分叉实现 | 自动化 |
| A6 | 可追溯 | artifact 保存版本、hash、reason、category | trace 审计字段 | 字段完整 | 单元 + 运行 |
| A7 | 产品链可用 | c6 N=10 完成并通过新判官回归 | 产品 run 与 judgment revision | 0 基础设施失败；结果不得伪报通过 | 真实运行 |

### 验收冻结与变更

- 冻结时点：2026-10-28，规则版本 `citation-source-meta-rules-v4`、提示词 `citation-source-meta-v8`、金标准 `citation-source-meta-calibration-v6`。
- 金标准 v1→v6 的变更原因：旧标签把带实质内容的来源归因、来源定位、模式分类和回答组织说明标成元描述，与“默认正文、零错杀”的冻结目标冲突。
- 旧标签已显式废弃，不用总体 K 值掩盖正文错杀。
- 后续如再次调整目标，必须结束本轮实验并另立决策，不得边跑边改标准。

## 验收执行

- [x] Markdown 结构与粗体命题过滤测试通过。
- [x] 模型语义槽、Harness 合成、schema 和一次重试测试通过。
- [x] 产品管线先跑门禁，仅把放行 span 交给 annotator。
- [x] 门禁 pending 时整条引用管线 pending，正文保留。
- [x] c6 正式回放达到全部 span ≥9/10。
- [x] 18-case 正式回放达到全部 span ≥9/10。
- [x] 已确认 body 金标准错杀为 0。
- [x] 产品 c6 N=10 运行完成，0 基础设施失败。
- [x] 新判官 revision 的 R/X 规则项和 citation integrity 已执行。
- [ ] citation support 的 4 个 pending 样本由沛东复核。
- [ ] 沛东复核提示词、金标准与关键语义边界。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | Markdown 标题和导航如何处理 | Harness 确定性过滤；完整粗体命题保留 | 结构信息无需语义模型 | 交给模型会增加抖动和错杀 | span 版本升至 `claim-spans-v4` | 结构单测 | 已确认 |
| D2 | B | 门禁优化目标 | body 错杀为阻断；meta 漏放可容忍；不确定默认 body | 门禁只做高置信度排除 | 追求高召回会绕过下游引用和判官 | 分类策略与指标 | 两组 N=10 回放 | 已确认 |
| D3 | B | 模型与 Harness 的职责 | 模型只给语义槽；Harness 固定合成最终决定 | 自然语言类别边界不稳定，程序应负责动作 | 直接让模型输出最终类别反复振荡 | 分类器 API 与审计 | schema/合成单测 | 已确认 |
| D4 | B | 位置和回答提纲为何仍不稳定 | `source_structure`、`answer_provenance` 只接受 Harness 窄规则识别；其余默认正文 | 模型稳定地把“表格内容摘要”当位置、把“回答提纲”当过程 | 继续改同类措辞无法建立互斥边界 | 减少错杀，可能少量 meta 漏放 | v8 全量回放 | 已确认 |
| D5 | B | 产品接入关系 | 门禁成功时只标注放行 span；门禁失败时 fail-open 到全部 span，最终 citation 状态只由 annotator 决定 | 预筛是质量优化，不能阻断普通用户获得引用 | 让整条管线 pending 会因预筛故障清空全部引用；无记录回退则是静默降级 | artifact 同时记录 source-meta pending 与 fallback | 管线与 runtime 测试 | 已确认 |

## 证据包

### 自动化证据

- 命令：`backend\.venv\Scripts\python.exe -m pytest tests/test_citation_source_meta.py tests/test_source_meta_experiment.py tests/test_citation_pipeline.py`
- 结果：22 passed。
- 命令：`backend\.venv\Scripts\python.exe -m pytest tests/test_citation_pipeline.py tests/test_agent_runtime.py tests/test_citation_source_meta.py tests/test_source_meta_experiment.py tests/test_citation_annotation.py`
- 结果：83 passed。
- 命令：`.\backend\.venv\Scripts\python.exe -m pytest backend\tests --basetemp backend\.runtime\pytest-043-full`
- 结果：348 passed。
- 命令：`corepack pnpm quality:quick`
- 结果：通过；Ruff、Pyright、Vue TypeScript、前端构建、Rust fmt/check/clippy 和注释检查均通过。
- 覆盖不变量：结构过滤、默认正文、窄规则合成、协议重试、管线 pending、产品 artifact 审计和 runtime trace。
- 环境说明：系统 pytest 临时根 `C:\Users\lida\AppData\Local\Temp\pytest-of-lida` 自身存在 ACL 拒绝；因此完整测试使用工作区内独立 `--basetemp`，不是代码失败。

### 运行证据

#### c6 门禁回放

- Run：`run_33ddea64024b4ba698e83bcfefe78db8`
- Prompt：`citation-source-meta-v8`
- Prompt hash：`e7cb0005d4c9e2be3eb24c2988f6997bd301538687415268faa7506266c0a937`
- Rule：`citation-source-meta-rules-v4`
- Gold：`citation-source-meta-calibration-v6`
- Span：301。
- 全部 10/10：301/301。
- K=9：301/301。
- 正文错杀：0。
- 元描述漏放：0。
- 协议错误：0。

#### 18-case 门禁回放

- Run：`run_a36943ad6bcf4edf85f3f789e8f89e49`
- Span：528。
- 全部 10/10：526/528。
- K=9：528/528。
- 正文错杀：0 次。
- 元描述漏放：2 次，均来自 c7 同一 run 的两条来源覆盖澄清句；均满足 K=9。
- 协议错误：0。

#### 产品 c6 N=10

- Plan：`plan_12e23897301a444fb535ff742499ae48`
- Run：`run_11800cba865142bdb16cc8dabfd1bd64`
- 执行：10/10 succeeded。
- 基础设施失败、产品失败、未知结果：均为 0。
- 模型调用：74；工具调用：40；输入/输出 token：592895 / 44886。
- 10 个 trace 的门禁状态均为 complete；prompt v8、rule v4；本批 c6 排除 span 数为 0，说明没有错杀正文。

#### 新判官 revision

- Revision：`evalrev_5226a64a321a468db41f83509d0db8fc`
- R/X 规则：48 pass、1 fail、1 pending。
- 失败项为 c6 R1：某次答案对沙箱和出口控制的具体定义不完整；pending 项为一次 judge 上游失败。
- Citation integrity：10/10 pass。
- Citation support：6 pass、4 pending；pending 中合计 6 个 quality 级 unknown occurrence，另有 1 次 evidence quote 校验失败。
- Case 聚合：5 pass、1 fail、4 pending，因此 case 保持 pending，不得写成 K=9 通过。
- pending 与来源门禁无关：10 个产品 trace 的门禁均为 complete，且本批排除数量为 0；问题位于既有 citation annotation/judge 的支持性边界。

### 失败探针

- 门禁连续两次非法输出：`CitationPipeline` 记录 source-meta pending，并将全部 spans 回退给 annotator。
- 门禁 fail-open 集成：最终 citation 状态为 complete，引用结果保留，limitations 记录 `citation_source_meta_fallback`。
- 全部门禁排除：annotation 保持 complete、annotations 为空，不发起无意义的 annotator 调用。
- 部分排除：annotator 输入只含放行 span，artifact 仍记录原始 span 数量与排除数量。
- `location` 语义槽出现在表格内容摘要：Harness 窄规则拒绝排除，回到 body。
- `answer_process` 语义槽出现在普通“下面先……”提纲：Harness 窄规则拒绝排除，回到 body。

### 已知反例账本

| 场景或反例 | 期望行为 | 最终结果 | 是否阻断 | 处理结论 |
| --- | --- | --- | --- | --- |
| c14 来源缺失与引用建议 | 核心来源缺失/标注指导排除；带实质内容句放行 | c14 全部 span 10/10 | 否 | 通过 |
| c11 多来源关系与普通提纲 | 内容关系和提纲均 body | c11 全部 span ≥9/10 | 否 | 通过 |
| c3 位置+内容混合 | 带实质内容一律 body | c3 全部 span ≥9/10 | 否 | 通过 |
| c15 表格内容摘要 | 说明表格包含的类别、代价和边界属于 body | c15 全部 span 10/10 | 否 | 通过 |
| cf-002 能力边界/缺少输入 | 默认 body | cf-002 全部 span 10/10 | 否 | 通过 |
| c7 来源覆盖澄清句 | 可排除；偶发漏放可容忍 | 两条各 9/10 | 否 | 达到 K=9 |

### 人工验收

- [x] 沛东确认金标准 v1→v6 的语义修订。
- [x] 沛东确认 `source_structure` 与 `answer_provenance` 由 Harness 窄规则收口。
- [x] 沛东确认来源门禁失败必须 fail-open，不能移除正常用户引用。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 这个门禁防止的用户损失是什么？为什么不能追求把所有元描述都排除？ | 首先防的是应进入正文引用的内容被错误漏标；既要零错杀又要零漏放超出了语义分类的稳定能力，因此门禁只接受高置信度排除，不确定时回到正文。 | 通过 | 保持“宁可放过、不可错杀”和不确定默认正文。 |
| R2 | 为什么“带来源归因的正文”和“来源元描述”不能由模型直接给最终布尔决定？ | 分类规则和优先级较复杂，直接让模型输出最终布尔值会不稳定；应让模型只识别语义信号，由 Harness 按固定顺序合成动作。 | 通过 | 保持语义理解与门禁动作分层，并保留 reason 供审计。 |
| R3 | 门禁 pending 时产品应怎样降级？ | 门禁只是减少评测人工量的优化，不能影响正常用户获得引用；失败时回退到全量 span annotation，并显式记录 pending 与 fallback。 | 通过 | citation 顶层状态由 annotator 决定，禁止因门禁失败清空引用。 |
| R4 | 实验已达到全 span K=9，但产品 c6 仍是 4 pending，为什么不能算通过？ | 引用支持 pending 不只来自来源门禁处理的来源元描述，还包含证据选择、缺失指代和 quote 校验等情况；门禁分类通过不能代表产品端到端通过。 | 通过 | c6 的 1 fail、4 pending 保留为后续引用标注专项问题。 |

<!-- reverse-explanation-gate: passed 4/4 -->

## 协作复盘

- AI 明显加速的环节：将失败 reason 聚类为语义职责冲突，并用冻结 target 连续做 N=10 回放。
- 负责人失去系统模型的环节：前期把“更完整分类”当成改进，实际让模型承担了不可稳定互斥的最终路由职责。
- 真正发现问题的控制点：以正文错杀为阻断项，而不是只看总体 K；reason 暴露了来源归因、位置和回答组织的概念重叠。
- 只有仪式感、没有产生判断价值的流程：继续润色同类类别措辞；后续改为直接调整职责或 Harness 合成。
- 下一次准备调整的一到两项：先冻结错误代价，再决定语义由模型、程序还是下游判官负责。

## 已知风险与后续

- 风险：来源覆盖统计与来源缺失仍依赖模型语义槽，可能在未覆盖语料中出现漏放或少量错杀。
- 可容忍项：已知来源元描述偶发漏放，只要不造成静默正文错杀并满足 K=9。
- 未验证项：18-case 只验证了门禁分类，没有重新执行全部 18 个产品回答与判官。
- 后续：c6 的 4 个 citation support pending 转入引用标注专项；043 只对结构过滤、来源元门禁和 fail-open 契约负责。

## 停止与推翻条件

- 新语料中任一已确认正文被稳定排除，停止产品接入并回放根因。
- 同类语义边界继续在多轮提示词调整中振荡，撤掉对应模型分类槽，改由确定性规则或下游判官处理。
- 产品门禁版本或规则版本无法完整进入 trace，视为不可审计，不得标记完成。
