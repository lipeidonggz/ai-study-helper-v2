# 072 · Wiki Narrative Pages

- 状态：已完成（初版实现、真实小样本与集中反向解释通过）
- 开始日期：2026-10-31
- 关联设计：[11 LLM Wiki 编译设计](../../design/11-llm-wiki-compilation.md)
- 前置切片：068 证据地图；071 Topic Search
- 人工验收：最终集中进行

## 控制契约

### 用户可观察目标

只有达到 materialization 阈值的 `WikiTopic` 才生成叙述页；每个正文段落都可以回源到当前 snapshot 的 block，catalog-only 主题不会被伪装成完整页面。

### 核心不变量

1. 页面不能包含无 block refs 的事实段。
2. 页面引用不能跨 snapshot。
3. 页面截断、非法 JSON 或 page hash 不一致时整页失败。
4. catalog-only 与 narrative 是显式内容等级。
5. 页面链接只能指向同一 build 已存在的 topic。

### 明确禁止

- 不再接受调用方手工给定 source block 来替代 topic ledger evidence。
- 不自动生成 synthesis／comparison。
- 不把页面正文当作 citation 来源。
- 不保存半页。

## 输入与输出

输入：071 已发布 topic ledger、topic evidence blocks、coverage slots、ChatProvider、允许链接的 topic IDs。

输出：`wiki_pages`、段落级 block refs、渲染 artifact、页面版本和页面检索文本。

## 验收矩阵

| ID | 行为 | 阻断阈值 | 层级 |
| --- | --- | --- | --- |
| 072-A1 | 每个叙述段有合法 block refs | 无来源段=0 | 单元 |
| 072-A2 | 跨 snapshot refs 被拒绝 | 接受=0 | 单元 |
| 072-A3 | 截断不发布半页 | partial page=0 | 单元 |
| 072-A4 | catalog-only 不调用写页模型 | 调用=0 | 单元 |
| 072-A5 | 页面失败不破坏 topic search | active 不变 | 集成 |

## 决策账本

| ID | 等级 | 选择 | 状态 |
| --- | --- | --- | --- |
| 072-D1 | B | 达到 ≥2 direct blocks 或跨 ≥2 docs 才 narrative | 已确认（2026-11-01） |
| 072-D2 | B | sections + paragraph refs 的结构化协议 | 已确认（2026-11-01） |
| 072-D3 | B | 初版只生成 topic 页；document_summary/index 后续 | 已确认（2026-11-01） |
| 072-D4 | B | 页面批次失败不保存部分页面；topic build 已发布时保持其 topic search 可用 | 已确认（2026-11-01） |

## 证据包与反向解释

- 自动化：Wiki 专项 16 项通过，覆盖逐段 block 引用、跨 snapshot 拒绝、截断失败和增量页复制。
- 真实小样本：对三个目标主题执行 materialize，生成 `wp_eb79be8ef95adf22`（3 段）与 `wp_eaf76a07ea6ecdfa`（4 段）；前两页分别直接引用 worktree 与 checkpoint 恢复 block。
- catalog-only：终态评估主题只有 1 个 block，按阈值不生成叙述页，返回 `catalog_only_topic_ids`。
- 详细记录：[2026-11-01 LLM Wiki 编译实验](../experiments/2026-11-01-llm-wiki-compilation.md)。
- 反向解释：R1 为什么引用必须落到段落；R2 为什么不是所有 topic 都写正文；R3 为什么页面不能替代 block。
- 集中反向解释：[R1–R5](../experiments/2026-11-01-llm-wiki-compilation.md#集中反向解释)，5/5 通过。

## 停止条件

若叙述页普遍无信息增量、成本失控或无法维持段落回源，则停止 narrative 层，只保留 topic ledger。

<!-- reverse-explanation-gate: passed 5/5 -->
