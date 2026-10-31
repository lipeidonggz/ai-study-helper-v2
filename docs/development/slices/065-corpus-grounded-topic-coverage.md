# 065 · 语料锚定的主题覆盖检索

- 状态：已正式接入；facet 稳定性与每 facet 候选预算待确认
- 开始日期：2026-10-30
- 工作分支：`feat/065-corpus-grounded-coverage`
- 关联设计：[06 检索、工具、Agent 循环](../../design/06-retrieval-pure-agent.md)
- 当前实现设计：[11 语料锚定的主题覆盖检索](../../design/11-corpus-grounded-topic-coverage.md)
- 前置切片：[064 主题覆盖检索](./064-topic-coverage-search.md)
- 关联实验：[2026-10-30 语料锚定 facet 实验](../experiments/2026-10-30-corpus-grounded-facet-retrieval.md)
- 完善实验：[2026-10-31 facet contract、审计与稳定性](../experiments/2026-10-31-facet-contract-audit-stability.md)
- 人工验收：待完成

## 控制契约

### 背景与失败场景

- 触发问题：query-only subtopic/facet planner 倾向产生通用安全分类，遗漏
  知识库中的 workspace、session、checkpoint、恢复等具体机制。
- 不处理时的用户损失：工具返回大量高风险上下文，但最终答案遗漏跨文档
  的具体限制关系。
- 现有流程为什么不足：facet 完全由 topic 生成，没有观察当前知识库；
  40 块和 20,000 token 的固定裁剪进一步放大通用总览块。
- 本切片负责：把 corpus-grounded planner 作为 `search_topic` 的唯一实现接入
  产品，并返回所有经 reranker 判定至少部分相关的候选，不执行最终 40 块裁剪。
- 本切片不负责：最终回答排序、长期多轮记忆、ANN 索引、动态查询重试和
  100k block 性能优化。
- 停止条件：若 77+ 块导致 Harness 上下文溢出、Agent 无法稳定使用，或
  endpoint 超时不可恢复，则停止并回退。

### 用户可观察目标

当用户提出需要跨文档找全的主题问题时，`search_topic` 返回基于真实知识库
标题、章节和内容生成的覆盖面，并返回全部高召回相关块。实验 trace 能看出
explanation、facets、查询、预筛和 reranker 的来源。

### 核心不变量

1. `search_topic` 只使用 corpus-grounded planner，不再保留旧 planner 开关或
   静默回退路径。
2. 返回块必须来自当前冻结 snapshot，不能使用历史缓存中的 block。
3. 只有 reranker 判定为 `direct` 或 `partial` 的候选可以返回。
4. 任何模型或查询失败都不能静默返回部分成功；必须形成明确错误。
5. trace 或 diagnostics 必须记录策略版本、prompt hash、查询数、候选数和
   token 使用。
6. corpus-grounded coverage 的输入范围只有 `topic`；用户问题目标属于主
   Agent 对返回结果的使用语义，不得影响主题覆盖面规划。

### 错误代价与非对称门禁

| 错误类型 | 具体例子 | 用户影响 | 处理分类 | 指标与阈值 |
| --- | --- | --- | --- | --- |
| 静默漏块 | reranker 批次失败后仍返回其余结果 | 用户误以为已查全 | 阻断 | 任一必需调用失败即整体失败 |
| 上下文溢出 | 返回块超过 Harness context budget | Agent 无法完成回答 | 阻断 | tool payload 必须能被 Harness 完整接纳 |
| 相关性误判 | 通用块被大量判为 direct | 压缩质量退化 | 可观察但本切片接受 | 记录 direct/partial 和来源 |
| 返回快照外 block | 查询结果跨 snapshot | 证据不可追溯 | 阻断 | 所有 block_id 必须属于当前 reader |

### 明确禁止

- 不得恢复或静默回退到旧 subtopic planner。
- 不得静默截断到旧的 40 块或 20,000 tokens。
- 不得在失败时返回“空结果成功”。
- 不得把实验硬编码的当前 topic、目标 block 或 c13 术语写入产品提示词。
- corpus-grounded coverage 不得读取或使用 `retrieval_goal`。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| explanation 模型失败 | 明确的 topic coverage 失败 | trace 与已完成查询诊断 | 是 |
| facet 结构非法 | `coverage_plan_invalid` | 原始模型输出 trace | 是 |
| 任一检索查询失败 | `coverage_query_failed` | 全部查询结果与错误 | 是 |
| reranker 批次失败或协议非法 | `coverage_rerank_*` | 已完成批次、短名单和 prompt hash | 是 |
| 返回超过 Harness 上下文 | Runtime 明确预算错误 | 工具诊断和实际块数 | 是 |

### 人工检查点

- 接入前：确认 corpus-grounded 已成为 `search_topic` 唯一实现，且工具输入
  只保留 `topic`。
- 中途：检查 c13-003 是否实际调用一次 `search_topic`，以及返回块数。
- 完成前：由沛东复核已返回证据在最终答案中的使用情况。

### 决策预算

- 预计 B 级决策：5（返回全部相关块、固定每 facet Top-10、reranker 批次大小、
  coverage 输入边界、seed 全文）
- 是否存在 C 级决策：否

## 目标

在产品工具链内复现当前实验：

```text
topic
→ 详细解释
→ hybrid seed retrieval
→ seed block 全文
→ corpus-grounded facets
→ dense/keyword 多查询
→ facet 内 Top-10
→ LLM reranker
→ 返回全部 direct/partial 支持块
```

## 输入与输出

- 输入：`search_topic.topic`
- 输出：当前 snapshot 内的 `SearchBlocksResult`
- 工具契约版本：`retrieval-tools-v9`

## 不包含

- 不做最终 40 块 coverage selection；
- 不做跨主题稳定性评估；
- 不新增工具名或改变 Agent 对 `search_topic` 的调用方式；
- 不把 runtime 实验脚本导入产品代码。

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 |
| --- | --- | --- | --- | --- | --- |
| A1 | 唯一实现 | `search_topic` 直接走 corpus-grounded 且无旧路径回退 | 适配器与工具描述契约测试 | 必须 | 单元／集成 |
| A2 | 高召回 | c13-003 调用新策略后返回足够候选 | trace 返回块数 > 40 | 必须 | 真实运行 |
| A3 | 证据可追溯 | 返回 block 全部属于当前 snapshot | block_id 集合校验 | 必须 | 单元／集成 |
| A4 | 无静默失败 | reranker 或查询失败时整体显式失败 | 故障探针 | 必须 | 单元 |
| A5 | 答案表现 | 大证据集能否改善 R1/final | c13-003 单次回放 | 观察 | 真实运行 + Judge |

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 是否替换旧 planner | corpus-grounded 为唯一实现，删除旧 planner | 用户确认正式接入 | 保留双路径会增加契约和回归复杂度 | 工具入口只有一个策略 | A1 | 已确认 |
| D2 | B | 是否继续 20k/40 块裁剪 | 返回全部 reranker 支持块 | 用户要求覆盖优先，结果使用交给主 Agent | 固定裁剪可能误删具体机制 | 当前工具输出约 35k tokens | A2 | 已确认 |
| D3 | B | 预筛粒度 | 每 facet 归一化 Top-20 作为临时覆盖基线 | 固定 v6 plan 下 Top-10 漏 worktree，Top-20 才 3/3 | Top-20 会让 reranker 输入增至 214,491 tokens，后续仍需动态预算 | 需要新的预算策略 | A2、Q-05 | 已确认（临时基线） |
| D4 | B | coverage 是否读取问题目标 | corpus-grounded 路径只使用 topic | 覆盖职责与回答使用职责分离 | 混入 retrieval_goal 会让主题规划随问题措辞变化 | facet 可独立于当前回答复用 | A2、A5 | 已确认 |
| D5 | B | seed evidence 是否截断 | 使用 Top-50 block 全文 | 350 字截断丢失状态恢复等关键句 | 只给前缀会把语料观察退化为摘要片段 | 增加约 1.2 万输入 tokens | A2 | 已确认 |

## 证据包

### 自动化证据

- 命令：
  `backend\.venv\Scripts\python.exe -m pytest backend\tests\test_corpus_grounded_coverage.py backend\tests\test_agent_protocol.py backend\tests\test_agent_runtime.py -q`
- 结果：60 passed
- 命令：`backend\.venv\Scripts\ruff.exe check backend\app\agent\corpus_grounded_coverage.py backend\app\agent\tool_adapter.py backend\tests\test_corpus_grounded_coverage.py`
- 结果：通过
- 命令：`corepack pnpm quality:quick`
- 结果：通过，包括注释检查、Ruff、Pyright、Vue TypeScript、Vite build、
  Rust fmt／check／clippy
- 覆盖的不变量：A1、A3、A4；“返回全部有支持候选”和
  “reranker 可省略无支持候选”由确定性单元测试覆盖

### 运行证据

- 启动方式：
  `run_case_fresh.py --case-id c13-003 --case-revision-id cr_c13_003_fff9fbc82a83 --n 1`
- run：`run_7e2921c5c8c14e7091ed77bbe28e7e0a`
- trace：`802e8add918bb40965ab554d6634b3f2`
- judgment：`evalrev_61141bc252a249279c4b4d8659c7be8b`
- 预期结果：新策略被调用，返回明显多于 40 个完整证据块，且主 Agent
  能在没有上下文截断的情况下完成回答。
- 实际结果：成功完成一次；`search_topic` 返回 67 块、32,920 result tokens，
  `admitted=true`；工具内部 15 次模型调用，输入 59,607 tokens、输出
  17,237 tokens。整次 run 共 21 次模型调用、输入 131,930 tokens、输出
  21,811 tokens。
- Judge：R2 pass、X2 pass、R1 fail。R1 仅缺第⑤组“并行任务与工作状态边界”；
  其余六组均可辨认。
- 结果文件：
  `backend\.runtime\c13-003-corpus-grounded-n1-result.json`

#### v2：topic-only + 完整 block seed

- 启动方式同上，实现版本为 `corpus-grounded-coverage-v2`
- run：`run_cec276fadd384bf6aeac435aadf0d1bf`
- trace：`2994dfe9aded4987fe2a8de2ab03490d`
- judgment：`evalrev_89f235285b02401fae904232dca7a587`
- 工具结果：24 facets、192 查询、447 候选、103 短名单、75 支持块返回；
  result 35,104 tokens，`admitted=true`
- 工具耗时 63.3 秒，15 次工具内模型调用；整次 run 22 次模型调用，
  输入 161,755 tokens、输出 28,064 tokens
- Judge：R2 pass、X2 pass、R1 fail；R1 缺第⑤组并行任务边界和第⑥组
  上下文与记忆生命周期边界
- 阶段诊断确认 `worktree`、`checkpoint resume`、`checkpoint evaluation`
  三个目标块均进入短名单、获得 reranker 支持并返回
- 结果文件：
  `backend\.runtime\c13-003-topic-only-full-seed-n1-result.json`
- 阶段诊断：
  `backend\.runtime\c13-003-topic-only-full-seed-stage-diagnosis.json`

#### 正式默认路径验证

- 启动方式：
  `run_case_fresh.py --case-id c13-003 --case-revision-id cr_c13_003_fff9fbc82a83 --n 1`
- 工具参数只有：`{"topic":"AI Agent 隔离与控制边界"}`
- run：`run_4fae800620714007a61e0faccbbff595`
- trace：`7c66f2b7534c7224c722c332105d68ba`
- judgment：`evalrev_0156d42b467b4360a1a81de2db86a41a`
- 工具契约：`retrieval-tools-v9`
- 结果：14 facets、136 查询、392 候选、67 短名单、53 支持块返回；
  24,587 result tokens，`admitted=true`
- 目标块：`checkpoint resume` 返回；`worktree` 和 `checkpoint evaluation`
  本次未返回
- Judge：R2 pass、X2 pass、R1 fail，仍缺第⑤组和第⑥组
- 结果文件：
  `backend\.runtime\c13-003-product-corpus-grounded-n1.json`

### 产品 plan 阶段定位

为避免把产品实时 planner 波动误判为排序问题，使用上述 trace 的真实 18 个
facets 做固定 plan 重放，并记录三个已知目标块分别在哪一阶段消失：

- `worktree`：已进入 Top-20 候选；最相关 facet 内排名 15/103，被每 facet
  Top-10 挡掉。
- `checkpoint resume`：已进入 Top-20 候选；最好 facet 内排名 38/98，被每
  facet Top-10 挡掉。
- `checkpoint evaluation`：进入 Top-10 短名单，但 reranker 未判该块支持
  当前任何 facet；固定 20-facet 实验中的“状态变更评估” facet 在本次动态
  plan 中不存在。

三个目标块都先通过了查询和候选召回，因此当前首要问题不是 embedding／FTS
失效，也不是最终 40 块裁剪。动态 facet plan 没有明确覆盖“并行工作状态、
checkpoint／恢复、状态变更评估”，导致前两个目标在 facet 内排序中被挤出，
第三个目标只能挂到错误 facet 后被 reranker 丢弃。

证据文件：
`backend\.runtime\c13-003-corpus-grounded-stage-diagnosis.json`。

### 失败探针

- 首次产品回放通过新策略执行到 reranker，但 reranker 只返回存在支持的候选，
  产品校验错误地要求每个输入 block_id 都出现在响应中，最终返回
  `coverage_rerank_invalid`，主 Agent 回退到普通 `search_blocks`。
- 修复：允许 evaluation 是输入候选的真子集；未知和重复 block_id 仍失败。
  未返回候选按“无支持”处理。
- 修复后单测与真实 N=1 回放均通过。

### 人工验收

- [x] 沛东确认 corpus-grounded 正式接入，旧 planner 不再保留
- [x] 沛东确认 `search_topic` 不读取 `retrieval_goal`
- [ ] 每 facet Top-10 粒度仍待确认

## 完善前问题清单

本节记录 065 从“主路径可用”到“相对完善、可提交”的全部已知问题。问题编号
在后续实验和提交说明中保持稳定；完成一项后改为勾选，并链接对应证据。

### P0：提交前必须解决

- [ ] **Q-01 Facet plan 稳定性**
  - 证据：同 topic、同快照两次正式运行分别为 24 facets／192 查询／75 块和
    14 facets／136 查询／53 块；已知目标召回分别为 3/3 和 1/3。
  - 完成条件：固定 topic 和快照执行 N≥5，记录 facet 交集、差异、目标阶段
    覆盖率；明确并达到可复现的稳定性阈值。
  - 当前结果：v5 N=5 已完成，facet 数 19～24，三目标同时返回 0/5；
    worktree、resume、evaluation 返回率分别为 1/5、4/5、0/5；R1 0/5。
    未达到稳定性门槛。该批次的 audit 仍含方向性镜头，只能作为工程探针，
    不能作为跨主题稳定性结论。干净 v10 的 c13 N=1 已返回 worktree 和
    checkpoint resume，checkpoint evaluation 进入短名单后被 reranker 丢弃；
    仍不足以判断稳定性。

- [x] **Q-02 Facet contract 不足**
  - 证据：当前只有 `name`、`scope`、`origin`、`seed_refs` 和查询；
    “多 Agent 信任升级与状态隔离”仍可能混合多个独立证据义务。
  - 完成条件：facet 增加并验证 `evidence_obligation`、
    `coverage_question`、`required_relation` 等结构化字段；reranker 使用
    evidence obligation 判断，而非只依赖名称和简短 scope。
  - 当前结果：已在 v3 contract 中实现，并由 v4/v5/v6 真实运行验证；详见
    2026-10-31 实验的 Q-02。

- [x] **Q-03 Facet 缺项审计缺失**
  - 证据：动态 plan 可能遗漏并行工作状态、durable checkpoint、状态变更评估；
    目前没有独立阶段发现这类遗漏。
  - 完成条件：增加 facet audit，至少识别未覆盖机制、过宽 facet、语义重复、
    无语料证据的 hypothesis 和无法指导检索的泛化 facet。
  - 当前结果：受污染的 v2 结果已废弃。内容中性的 audit v3 在两个 held-out
    主题上运行成功，其中“LLM 应用评测”主题独立新增 4 个有 seed 依据的
    facet。审计机制已完成；跨主题稳定性统一由 Q-01 评估。

- [ ] **Q-04 Seed 观察面仍可能不足**
  - 证据：全文解决了 350 字截断，但 seed 仍来自单个 topic hybrid Top-50；
    目标章节可能完全不进入 seed，同一 seed 文档的相邻相关章节也不可见。
  - 完成条件：比较单 seed 与多 seed／章节结构补充方案，记录文档和章节覆盖，
    并确认关键机制不会因 seed 偏置整体消失。
  - 当前结果：已补 seed 文档章节大纲；v4 单次增加 12 篇文档、245 个章节，
    但 N=5 中 worktree 只有 1/5 返回，仍未达到稳定豁免。

- [ ] **Q-05 每 facet 预筛策略未定（D3）**
  - 证据：`worktree` 曾在 facet 内排 15/103，`checkpoint resume` 曾排
    38/98；固定 Top-10 会直接排除它们，且各 facet 候选规模差异很大。
  - 完成条件：在固定 Top-10、动态 Top-K、覆盖配额和多样性选择之间完成实验，
    确认 D3，并把结果同步到决策账本。
  - 当前结果：固定 v6 plan 下，Top-10 漏 worktree，Top-20 可返回 3/3；但
    Top-20 将 shortlist 从 113 增至 167、reranker 输入从 149,175 增至
    214,491 tokens。沛东已确认 Top-20 作为临时正确性基线；固定 Top-20 不作为
    最终方案，动态预算仍需继续研究。

- [ ] **Q-06 缺真正的覆盖完整性评估**
  - 证据：当前只有三个目标探针，只能证明个别块命中，不能证明主题相关集合
    已找到；也没有精确率和重复块指标。
  - 完成条件：建立 block 级 qrels，覆盖相关块、易混淆负例、文档和章节层级；
    至少在 3 个主题上报告 recall、precision、重复率和目标阶段覆盖率。

- [ ] **Q-07 覆盖集合与主 Agent 上下文边界未解决**
  - 证据：当前一次返回 53～75 个完整块、约 2.5～3.5 万 tokens；面对
    100,000 blocks 和更宽主题时可能超过 Harness 预算。
  - 完成条件：明确完整覆盖集合的持久化位置、主 Agent 默认载荷、按需读取
    方式和超预算行为；禁止静默截断，完整集合与回答上下文必须解耦。

- [x] **Q-08 内部规划和 reranker 缺细粒度 trace**
  - 证据：当前 trace 只有工具级 aggregate，没有 explanation、facet、
    query、shortlist、reranker batch 的独立 span／artifact。
  - 完成条件：trace 能定位每一步输入摘要、输出、prompt/version、token、
    耗时和被排除候选，不要求记录模型隐藏推理。
  - 当前结果：已增加 explanation、planning、audit、reranker 和每条检索
    query 的 span/artifact；真实 trace 为 145 spans、51 artifacts、0 dropped。

- [x] **Q-09 全量测试隔离问题**
  - 证据：`quality:quick` 和相关门禁通过，但 Windows 全量测试出现 trace
    临时文件竞态和后台入库停在 S6；单个代表用例复跑可以通过。
  - 完成条件：定位并修复或明确隔离该测试基础设施问题，确保提交前全量测试
    结果可解释、可重复，不能以单测旁路代替全套回归。
  - 当前结果：完整后端测试在默认系统临时目录、沙箱外执行时 364 passed。
    此前 workspace basetemp 的失败是长路径与开发沙箱目录权限组合，不是业务
    回归；后续本地验收应使用默认系统临时目录或短 basetemp。

### P1：相对完善前应解决

- [ ] **Q-10 Reranker 稳定性**
  - 证据：固定 facet plan 重放时支持块曾从 67 变为 65；候选省略虽已按无支持
    处理，但判断本身仍有波动。
  - 完成条件：N≥3 重放同一候选集，报告 direct/partial 稳定率，并验证完整
    facet contract 是否能降低波动。
  - 当前结果：已为协议非法输出增加一次重试，第二次失败仍整体阻断；单元测试
    覆盖恢复路径。direct/partial 稳定性仍需后续重放。

- [ ] **Q-11 查询数量与边际价值**
  - 证据：单次运行产生 136～192 条查询，查询数随 facet plan 大幅变化。
  - 完成条件：统计每条查询的 unique candidate、unique supported block 和
    耗时贡献；合并低价值查询，并确定 100k block 下的查询预算。

- [ ] **Q-12 主题覆盖缓存**
  - 证据：coverage 只依赖 `topic + snapshot`，但每次提问都会重新规划和检索。
  - 完成条件：在覆盖质量稳定后，设计以 snapshot、topic、策略版本和 prompt
    hash 为键的缓存；明确失效、审计和重建语义。
  - 边界：缓存用于复用带来的性能和成本下降，不用于解决首次规划稳定性，
    也不能把错误 facet 固定下来。

- [ ] **Q-13 近重复与证据多样性**
  - 证据：当前没有 coverage 层的近重复控制；宽 facet 可能由同章节或同机制
    的多个近似块占据。
  - 完成条件：区分完全重复、同章节近重复和跨文档独立证据；去重不能误删
    独立机制，并要报告去重前后数量和保留理由。

- [ ] **Q-15 主题解释阶段缺直接消融证据**
  - 证据：当前主链路包含 topic → explanation → facet planner，但尚未做
    “固定 seed、facet 提示词、audit、reranker，只移除 explanation”的 A/B；
    历史 explanation-only dense 查询只召回 0～1/3 个目标。
  - 完成条件：在 c13 回归和至少两个 held-out 主题上以 N≥5 对比
    with-explanation / without-explanation 的 facet 质量、目标阶段覆盖率、
    查询数、返回块和 token 成本；结果出来前不得默认解释步骤有效。

### 下游独立问题

- [ ] **Q-14 主 Agent 未使用已返回证据**
  - 证据：一次 v2 运行已返回三个目标块，但最终答案仍漏第⑤、⑥组；正式默认
   运行也出现工具返回 `checkpoint resume`、答案仍未使用的情况。
  - 处理边界：这是主 Agent 的证据选择、综合和回答前覆盖检查问题，不回退为
    扩大主题检索；应另立切片或在现有 Agent 证据使用切片中处理。

### 建议完成顺序

```text
Q-02 facet contract + Q-04 seed 结构
→ Q-03 facet 缺项审计
→ Q-01 N=5 稳定性
→ Q-05 预筛策略 D3
→ Q-06 覆盖 qrels 与 Q-07 规模边界
→ Q-08 trace + Q-09 测试基础设施
→ Q-10～Q-13 优化
→ Q-15 explanation 消融
```

## 已知风险与后续

- 单次工具调用包含多次 LLM 调用，耗时显著增加。
- 本次实际返回 67 块、32,920 tokens，主 Agent 能接纳，但最终答案仍漏掉
  第⑤组，说明“大证据集”不会自动转化为完整答案。
- 实验中的 77 块是固定 plan 的结果；真实产品运行会因 planner 和查询结果
  波动改变 shortlist 与 supported 数量，不能写死。
- 固定 plan 重放中查询数、候选数和短名单数与原产品运行一致，但 reranker
  支持块从 67 变为 65，说明 reranker 本身仍有小幅不稳定。
- facet、query 和 reranker 的稳定性尚未在多个主题上验证。
- 正式默认路径仍存在跨运行 facet 波动：两次运行生成 24 和 14 个 facets，
  已知目标召回分别为 3/3 和 1/3。正式接入完成不等于覆盖稳定性完成。
- v2 已经把三个已知目标块全部返回，但答案仍未使用其中关于并行任务、
  checkpoint 和状态生命周期的证据；后续若继续推进，应单独处理主 Agent
  的证据选择与综合覆盖，不回退到扩大主题检索。
- 全量 `backend:test` 在当前 Windows 环境中存在既有测试隔离问题：使用
  系统临时目录时有 176 个 fixture setup 权限错误；改用工作区临时目录后
  为 288 passed、74 failed，失败集中在后台入库停在 S6。Agent、后台任务和
  collection snapshot 的代表用例单独复跑均通过。该问题不由本次
  `search_topic` 切换引入，仍需独立排查。
