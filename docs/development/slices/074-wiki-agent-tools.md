# 074 · Wiki Agent Tools

- 状态：已完成（初版工具接入；真实覆盖效果由 BL-0012 继续）
- 开始日期：2026-10-31
- 关联设计：[11 LLM Wiki 编译设计](../../design/11-llm-wiki-compilation.md)
- 前置切片：071；072；073
- 人工验收：最终集中进行

## 控制契约

### 用户可观察目标

当请求 snapshot 有兼容 Wiki build 时，主 Agent 可以搜索主题入口、读取页面或直接读取 topic evidence，并用原始 block 完成回答；没有兼容 build 时仍可使用原 block 检索。

### 核心不变量

1. Wiki 工具绑定请求固定的 snapshot。
2. snapshot 与 Wiki build 不一致时明确不可用。
3. Wiki 工具不生成最终答案。
4. 最终 citation 仍指向 block。
5. 不兼容或失败不能静默降级为旧 Wiki。

### 明确禁止

- 不把 topic/page ID 冒充 citation block ID。
- 不在工具中硬编码特定用例主题。
- 不因 Wiki 不可用而禁用原有 block 检索。
- 不把 page summary 当作文档原文。

## 输入与输出

输入：固定 snapshot reader、兼容 Wiki build、`search_wiki_topics`、`read_wiki_page`、`get_topic_evidence`。

输出：新的工具描述、payload schema、trace diagnostics 和 Wiki／block 对照回归结果。

## 验收矩阵

| ID | 行为 | 阻断阈值 | 层级 |
| --- | --- | --- | --- |
| 074-A1 | 兼容 build 才注册 Wiki 工具 | 错绑=0 | 单元／集成 |
| 074-A2 | topic search 命中别名 | 固定探针漏=0 | 集成 |
| 074-A3 | evidence 返回完整 block | 缺失=0 | 集成 |
| 074-A4 | 无 Wiki 时原工具可用 | 工具数/行为退化=0 | 集成 |
| 074-A5 | Agent trace 保留 topic 到 block 路径 | 断链=0 | 集成 |

## 决策账本

| ID | 等级 | 选择 | 状态 |
| --- | --- | --- | --- |
| 074-D1 | A | W6 采用严格 snapshot 匹配 | 已确认 |
| 074-D2 | B | Wiki 工具与原六工具共同注册，不替换 block 工具 | 已确认（2026-11-01） |
| 074-D3 | B | Wiki search 返回主题入口，get_topic_evidence 返回 block | 已确认（2026-11-01） |
| 074-D4 | B | Wiki 不可用时发出 `wiki_status`，前端提示后继续原文检索 | 已确认（2026-11-01） |

## 证据包与反向解释

- 自动化：兼容 build 注册 Wiki 工具；不兼容时保留原块检索工具、拒绝 Wiki 工具，并通过 `wiki_status` 事件向用户明确提示；工具路由、prompt、迁移和检索回归通过。
- 真实回放：`chat-v18` 完全不调用 Wiki；`chat-v19` 能稳定先调用 `search_wiki_topics`，轻量目录可进入 100k 预算，但主 Agent 仍只选择同一主文档的显眼主题，三个历史目标没有进入回答来源。
- 根因判断：当前阻塞已从“工具不可用／召回缺失／目录过大”转移为“主 Agent 缺少开放找全的覆盖决策职责”。未继续增加同构提示词。
- 详细记录：[2026-11-01 LLM Wiki 编译实验](../experiments/2026-11-01-llm-wiki-compilation.md)。
- 反向解释：R1 为什么 Wiki 与 block 工具并存；R2 为什么 topic ID 不能做 citation；R3 snapshot 不匹配时用户会看到什么。
- 集中反向解释：[R1–R5](../experiments/2026-11-01-llm-wiki-compilation.md#集中反向解释)，5/5 通过。

## 停止条件

若 Wiki 工具诱导 Agent 跳过原文、破坏 citation，或没有 Wiki 时影响原链路，则撤掉工具注册，仅保留离线 Wiki artifact。

<!-- reverse-explanation-gate: passed 5/5 -->
