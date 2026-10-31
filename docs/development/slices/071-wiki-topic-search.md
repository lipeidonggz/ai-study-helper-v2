# 071 · Wiki Topic Build + Search

- 状态：已完成（初版实现与自动验证、集中反向解释通过）
- 开始日期：2026-10-31
- 关联设计：[11 LLM Wiki 编译设计](../../design/11-llm-wiki-compilation.md)
- 前置切片：069；070
- 人工验收：最终集中进行

## 控制契约

### 用户可观察目标

一个 topic ledger 可以被原子发布为 Wiki build；系统可以按标题、别名、摘要和正文找到主题入口，并按 topic ID 返回完整、可回源的证据块。

### 核心不变量

1. active Wiki build 必须绑定一个 collection snapshot 和 manifest hash。
2. Wiki build 与请求 snapshot 不一致时返回 `wiki_build_not_ready_for_snapshot`，不混用。
3. 搜索结果的 alias、topic 与 evidence 查询均来自同一 build。
4. 目录不参与完整性判断。
5. build 失败不切换 active pointer。

### 明确禁止

- 不把 Wiki 页面写入 block 索引。
- 不把搜索 top-k 冒充主题全集。
- 不在无匹配时伪造“知识库不存在相关内容”。
- 不自动回退旧 build。

## 输入与输出

输入：070 topic ledger、snapshot manifest、embedding provider。

输出：

- SQLite `wiki_builds`、`wiki_topics`、aliases、evidence、coverage slots；
- 独立 topic FTS 和可选 dense 向量；
- `active_wiki_pointer`；
- `search_wiki_topics` 与 `get_topic_evidence` 服务。

## 验收矩阵

| ID | 行为 | 阻断阈值 | 层级 |
| --- | --- | --- | --- |
| 071-A1 | build 原子发布 | partial active=0 | 集成 |
| 071-A2 | 标题／别名搜索 | 固定探针漏=0 | 集成 |
| 071-A3 | snapshot 不匹配明确失败 | 混用=0 | 单元 |
| 071-A4 | `get_topic_evidence` 返回的 block 全部可回源当前 snapshot | 悬空=0 | 集成 |
| 071-A5 | 索引可重建且 topic identity 不变 | identity drift=0 | 集成 |

## 决策账本

| ID | 等级 | 选择 | 状态 |
| --- | --- | --- | --- |
| 071-D1 | B | Wiki metadata 进 SQLite，搜索索引为可重建派生数据 | 已确认（2026-11-01） |
| 071-D2 | B | title／alias／summary 建 topic FTS 与 dense；narrative 正文追加到 FTS | 已确认（2026-11-01） |
| 071-D3 | B | active pointer 按 generation 原子切换 | 已确认（2026-11-01） |
| 071-D4 | A | W6 采用严格 snapshot 匹配 | 已确认 |
| 071-D5 | B | W9：全量轻量目录，按来源轮转且不截断 | 已确认（2026-11-01） |

## 证据包与反向解释

- 自动化：Wiki 专项 16 项通过；受影响的 Agent／迁移／快照／检索回归 89 项通过。
- 真实结果：搜索返回全部 365 个主题。原始完整条目被 Harness 计为 168,518 tokens；轻量目录为 82,498 tokens，默认 100k 可接收。
- 来源轮转后前 15 条覆盖 15 个来源；三个历史目标仍在目录中。详细数据见[实验记录](../experiments/2026-11-01-llm-wiki-compilation.md)。
- 反向解释：R1 为什么 Wiki 索引必须独立；R2 snapshot 不匹配为何不能回退旧 Wiki；R3 为什么目录不能作为完整性依据。
- 集中反向解释：[R1–R5](../experiments/2026-11-01-llm-wiki-compilation.md#集中反向解释)，5/5 通过。

## 停止条件

若无法保证 build 原子性、alias 召回或证据回源，则停止进入 072。

## 后续待办

- [BL-0015](../backlog.md)：把当前固定 `DENSE_MIN_SIMILARITY=0.15` 改为带说明、持久化和验证的高级配置，作为检索效果与 token 消耗的权衡点。

<!-- reverse-explanation-gate: passed 5/5 -->
