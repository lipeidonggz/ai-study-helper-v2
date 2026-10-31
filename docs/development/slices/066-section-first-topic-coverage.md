# 066 章节优先的主题覆盖检索

- 状态：实验中（待确认）
- 开始日期：2026-10-31
- 关联设计：[11 语料锚定的主题覆盖检索](../../design/11-corpus-grounded-topic-coverage.md)
- 前置切片：[065 语料锚定的主题覆盖检索](./065-corpus-grounded-topic-coverage.md)
- 工作分支：`feat/066-section-first-topic-coverage`
- 人工验收：待完成

## 当前证据

- 初步实验：[2026-10-31 章节优先主题检索初步实验](../experiments/2026-10-31-section-first-routing.md)
- 已实现 `SectionIndex`、词面/dense/hybrid 路由和多查询 RRF；
- 当前 15 篇语料共有 286 个可路由 section，标题清洗和 snapshot 绑定测试通过；
- section-only 路由找到 `worktree`，但没有覆盖位于泛化章节标题下的两个 checkpoint 目标；
- 当前结论是继续验证“现有 block hybrid ∪ section routing”，而不是替换 block 检索。

## 控制契约

### 背景与失败场景

- 触发问题：block 级向量/关键词检索可能遗漏只由章节结构表达的语义面。
- 不处理时的用户损失：主题答复看似有大量证据，但若干机制或边界完全缺失。
- 现有流程为什么不足：章节大纲只作为 seed 文档的 planner 上下文，没有成为
  全库可检索的一级索引；未进入 seed 的文档章节不可见。
- 本切片负责：验证“章节路由后展开子树 block”能否作为独立候选来源，提高主题
  证据覆盖，并为后续产品接入提供可回放证据。
- 本切片不负责：最终回答生成、80% 覆盖率认证、覆盖率模型训练或替代现有
  `search_topic`。
- 什么情况下应停止：章节索引无法稳定映射到 block，或章节路由只改善单一回归
  主题、对 held-out 主题无收益。

### 用户可观察目标

实验入口能够针对当前固定 snapshot 构建章节索引，按 topic 返回可解释的候选章节，
并展开为可追溯的 block；同一 snapshot 和参数下结果稳定，不混入其他快照或历史
缓存。

### 核心不变量

1. 066 在主完成门禁前不得修改默认 `search_topic` 行为，也不得删除 065 路径。
2. Section Index 必须绑定一个固定 snapshot；返回的每个 section/block 都可回溯到
   当前 snapshot 的 section-tree 和 block-set。
3. 章节根标题、页脚、作者和代码语言页签不得作为主题 facet；无 subtree block 的
   节点不得作为证据候选。
4. 对照实验必须固定 facet plan、支持判断和 reranker，才能把差异归因到候选来源。
5. 产品代码、提示词和测试不得写入具体评测主题、目标 block 或由用例反推的标题。

### 错误代价与非对称门禁

| 错误类型 | 具体例子 | 用户影响 | 处理分类 | 指标与阈值 |
| --- | --- | --- | --- | --- |
| 静默漏章节 | section 标题存在但未进入索引 | 主题证据缺失 | 阻断 | 有效 section 索引覆盖率 ≥ 95% |
| 跨快照混用 | section/block 来自历史 collection | 证据不可追溯 | 阻断 | 所有 ID 必须属于绑定 snapshot |
| 伪章节 | newsletter/author/JavaScript 被当作 facet | 查询和候选污染 | 阻断 | 回归夹具必须验证过滤 |
| 路由退化 | section-only 候选少于人工关键章节 | 实验无效 | 阻断 | 固定主题关键章节召回必须显式报告 |
| 非确定性排序 | 相同输入得到不稳定顺序 | 无法复现实验 | 阻断 | 同 snapshot/参数重复排序一致 |

### 明确禁止

- 不得为通过实验把评测主题硬编码到 section classifier 或查询提示词；
- 不得在章节无有效 block 时静默退回整篇文档；
- 不得在索引损坏或标题清洗失败时返回部分成功；
- 不得在未记录对照条件时把 section 路由与 facet 变化混为同一收益；
- 不得修改金标准或 Judge。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 无可用章节 | `no_semantic_sections` | snapshot 标识与过滤统计 | 是 |
| section-tree 缺失 | `snapshot_integrity_error` | 文档与 artifact 标识 | 是 |
| block 引用越界 | `snapshot_integrity_error` | 查询与异常 section 路径 | 是 |
| 全部候选被过滤 | `no_match` | 被过滤原因计数 | 是 |

### 人工检查点

- 编码前：确认实验不改变默认产品路径。
- 中途：检查 index 统计与标题清洗结果，尤其是当前 15 篇基准语料。
- 完成后：由沛东确认 Section Index 是否达到可继续产品化的语义质量。

### 决策预算

- 预计 B 级决策：5
- 是否存在 C 级决策：否
- 超过预算时：拆为索引构建、路由策略和产品接入三个切片。

## 目标

建立一个只读、snapshot-bound 的章节索引，并完成以下最小闭环：

```text
topic
→ section BM25/dense routing
→ 候选 sections
→ subtree block expansion
→ 与现有 block 检索候选对照
```

## 输入与输出

- 输入：当前或显式指定的 collection snapshot、topic、section 检索参数。
- 输出：排序稳定且带 provenance 的 section 候选，以及展开后的 block 候选。
- 实验入口不得成为主 Agent 可调用的产品工具。

## 不包含

- 不修改 `search_topic` 默认实现；
- 不新增数据库表或迁移；
- 不让 LLM 为无标题文档生成标题；
- 不训练 Section/Block reranker；
- 不实现最终边际覆盖选择；
- 不改变现有 evaluation suite 和金标准。

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 |
| --- | --- | --- | --- | --- | --- |
| A1 | 语义章节可索引 | 有效非根章节进入索引 | 当前基准 snapshot 覆盖统计 | 有效映射率 < 95% 阻断 | 单元＋真实快照 |
| A2 | 标题噪声过滤 | 根、页脚、作者、代码页签被排除 | 专项 fixture 与当前语料统计 | 任一噪声进入 facet 候选即失败 | 单元 |
| A3 | 快照可追溯 | section 与 block 全来自绑定快照 | 跨快照与篡改夹具 | 任一越界即失败 | 单元＋集成 |
| A4 | 路由可复现 | 相同输入顺序和分数稳定 | 重复构建/查询 | 排序不一致即失败 | 单元 |
| A5 | 实验可解释 | section 路由与 baseline 固定其他变量 | 固定 facet plan 对照报告 | 缺对照或混变量即失败 | 实验回放 |

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | Section Index 第一版存放位置 | 运行时从 snapshot artifact 构建只读索引 | 避免过早数据库迁移 | 持久化表性能更好但增加生命周期复杂度 | 首版冷启动需读取少量 JSON | A1、A3 | 待确认 |
| D2 | B | section 检索文本 | doc title＋完整 path＋heading＋first_line | 保留层级并便于消歧 | 仅 heading 太短；全文子树会丢失路由优势 | 需要控制索引长度 | A4 | 待确认 |
| D3 | B | 首版检索模式 | BM25 与 dense 分开跑，再以 RRF 对照 | 便于判断标题词面与语义各自贡献 | 只跑 hybrid 无法解释收益来源 | 实验组增加 | A5 | 待确认 |
| D4 | B | 子树展开预算 | 以 section 排名和 subtree token 数共同限制 | 防止单个大章节垄断候选 | 固定 block 上限简单但可能截断机制 | 预算参数会进入实验记录 | A1、A5 | 待确认 |
| D5 | B | 噪声标题处理 | 默认排除无 subtree block 节点；保留 generic section 但降权 | 当前语料质量分析与最小风险 | 全部过滤可能误删 Conclusions 中的证据 | 需要分类统计 | A2 | 待确认 |

等级：

- A：AI 可自主执行，交付时列入审计摘要；
- B：完成后集中人工确认；
- C：执行前必须停止并获得人工决定。
