# 070 · Wiki Identity Ledger

- 状态：已完成（初版实现与自动验证、集中反向解释通过）
- 开始日期：2026-10-31
- 关联设计：[11 LLM Wiki 编译设计](../../design/11-llm-wiki-compilation.md)
- 前置切片：069 Snapshot Topic Map
- 人工验收：最终与 069、071–074 集中进行

## 控制契约

### 用户可观察目标

把多篇文档中的候选归一为稳定 `WikiTopic`；同一主题保留一个 ID，合并别名和跨源证据，相关但不同的主题保持分开。

### 核心不变量

1. `semantic_tags` 不参与 topic identity。
2. 同一规范名或别名精确命中时优先复用既有 topic ID。
3. LLM 只能从系统给出的候选 ID 中选择合并目标，不能发明 ID。
4. 未合并和低置信度候选必须保留并进入诊断，不得静默丢弃。
5. 所有 topic evidence 必须能回到 069 的合法 block refs。

### 明确禁止

- 不按 entity/concept 硬类型拆页。
- 不把“相关”“同属一类”或共享缩写直接判同。
- 不因 semantic tag 差异生成孪生 topic。
- 不以标题相似度单独自动合并。

## 输入与输出

输入：一个或多个 069 topic maps；可选上一 Wiki build 的身份账本。

输出：不可变 topic-ledger artifact，包含 topic ID、canonical name、aliases、summary、semantic tags、doc IDs、block refs、coverage slots、输入 candidate IDs 和 unresolved merge 数量。初版不保存每个被拒绝的近邻判定原文。

## 验收矩阵

| ID | 行为 | 阻断阈值 | 层级 |
| --- | --- | --- | --- |
| 070-A1 | 别名图确定性合并 | 应合未合=0 | 单元 |
| 070-A2 | 近邻异义主题保持分开 | 已知误合=0 | 单元 |
| 070-A3 | 标签跨类型不拆页 | 孪生=0 | 单元 |
| 070-A4 | 增量复用旧 topic ID | 应复用未复用=0 | 单元 |
| 070-A5 | 非法 LLM merge target 被拒绝 | 接受=0 | 单元 |
| 070-A6 | 每个输入 candidate 均可追踪 | 丢项=0 | 集成 |

## 决策账本

| ID | 等级 | 选择 | 状态 |
| --- | --- | --- | --- |
| 070-D1 | B | 先用 normalized name／alias 做确定性合并 | 已确认（2026-11-01） |
| 070-D2 | B | 只对有限近邻调用 LLM 判同 | 已确认（2026-11-01） |
| 070-D3 | B | 低置信度保留独立 topic | 已确认（2026-11-01） |
| 070-D4 | B | 新 topic ID 由初始身份锚确定性生成，后续由账本继承 | 已确认（2026-11-01） |
| 070-D5 | B | coverage slots 按 label／question／slot ID 排序，避免增量误判 | 已确认（2026-11-01） |

## 证据包与反向解释

- 自动化：`python -m pytest backend/tests/test_wiki_topic_map_identity.py`，16 项通过。
- 真实结果：420 个 candidates 归一为 365 个 topics，`unresolved_merge_count=255`；三个历史回归目标均保留直接证据。
- 真实故障：同一主题重编译后 coverage slot 顺序变化，曾被误判为 affected topic；已通过确定性排序修复，并由“未变 page 复制”回归锁定。
- 身份提示词曾因缺少 `JSON` 契约词被 DeepSeek 400 拒绝；升级为 `topic-identity-v2` 并增加回归测试。
- 反向解释：R1 为什么类型不是身份键；R2 为什么低置信度宁拆不并；R3 增量时如何保持 topic ID。
- 集中反向解释：[R1–R5](../experiments/2026-11-01-llm-wiki-compilation.md#集中反向解释)，5/5 通过。

## 停止条件

若别名对齐不能控制孪生页，或 LLM 判同持续误合并已知异义对象，则停止进入 071。

<!-- reverse-explanation-gate: passed 5/5 -->
