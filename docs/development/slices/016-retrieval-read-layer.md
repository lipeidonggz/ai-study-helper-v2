# 016 · 检索确定性读取层

- 状态：已完成
- 开始日期：2026-10-22
- 关联设计：[06 检索侧工具契约](../../design/06-retrieval-pure-agent.md)
- 前置切片：[015 GitHub 后台交付与失败可见性](./015-background-delivery.md)

## 控制契约

### 用户可观察目标

1. 一次检索会话只读取开始时绑定的同一份 collection 快照。
2. 可以按知识库范围列出文档、查看章节大纲、按章节取全块，以及执行不分词的字面扫描。
3. 范围不存在与合法零结果严格区分，不返回部分结果冒充完整结果。

### 核心不变量

1. `RetrievalSnapshotReader` 创建后不再读取活动指针；后台发布新快照不改变本次读取结果。
2. 所有文档和块都从该快照的 `document.json` artifact 读取，并校验文档、版本与块集合身份。
3. `get_outline` 保持输入 `doc_ids` 顺序并对重复 ID 取首次结果。
4. `list_blocks` 的章节范围包含该节全部后代，输出按 `block_index` 升序。
5. `scan_term` 只做 NFC 与英文大小写折叠，不做分词、词干或同义扩展；显式范围含缺失文档时整体失败。

### 明确禁止

- 不在 reader 生命周期内切换 collection 快照。
- 不调用聊天模型、embedding 模型或任何 LLM。
- 不计算 `result_tokens`，不实现上下文预算。
- 不在 `scan_term` 零命中时推断概念不存在。
- 不在范围缺失时返回其余文档或块的部分结果。

### 失败语义

| 场景 | 错误码 | 是否返回部分结果 |
| --- | --- | --- |
| 没有活动 collection | `collection_unavailable` | 否 |
| 必填参数为空 | `invalid_argument` | 否 |
| 指定文档或章节不存在 | `scope_not_found` | 否 |
| 快照 artifact 或身份不一致 | `snapshot_integrity_error` | 否 |
| 字面扫描合法零命中 | 成功，`count=0` | 是，空列表 |

### 决策预算

- 预计 B 级决策：1 项，字面扫描 snippet 的确定性截取规则。
- C 级决策：无。

## 目标

建立不依赖 LLM 的确定性 retrieval read model，为后续 `search_blocks`、Agent 工具适配和 trace 提供稳定的快照读取边界。

## 输入与输出

输入：

- 当前活动 collection 指针；
- 文档筛选条件、章节路径和字面扫描词。

输出：

- 不可变快照 reader；
- 文档清单、章节大纲、完整块枚举和字面扫描命中。

## 不包含

- `search_blocks`、query embedding、dense／FTS 与 RRF；
- Agent 循环、工具 envelope、`result_tokens` 和预算；
- 用户界面；
- snapshot artifact 性能目录或新索引格式。

## 验收条件

- [x] reader 在后台发布新快照后仍只返回绑定快照中的文档。
- [x] 文档清单筛选、稳定排序和总数正确。
- [x] 章节大纲包含直属／子树块数与 token 估算。
- [x] 单章节范围包含全部后代块，并按原文顺序返回。
- [x] 字面扫描支持 NFC＋英文大小写折叠，合法零命中不是错误。
- [x] 缺失文档、缺失章节、空参数与无活动快照返回稳定错误码。
- [x] 完整质量门通过。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | `scan_term` 的 snippet 长度 | 命中位置前后各保留 80 个字符，超出时用全角省略号标记；匹配与截取均基于 NFC 后文本 | 06 只要求返回 snippet，未规定长度；固定字符窗口可复现且与模型无关 | 返回全文会放大后续上下文；按 token 截取会引入尚未实现的运行时 token 口径 | snippet 可稳定复现，不参与预算计数 | 中英文命中位置、首尾边界与零命中测试 | 已确认 |

## 证据包

### 自动化证据

- 016 定向测试：8 passed。
- `corepack pnpm quality`：128 passed。
- Ruff、Pyright、Vue typecheck、前端构建、Rust fmt／check／clippy：通过。
- 快照文档在保持合法 JSON 结构但被改写后，因 SHA-256 与 manifest 不一致而被拒绝。

### 运行证据

- 临时数据根中依次发布两个快照，已有 reader 仍只返回第一个快照的一篇文档；
  新建 reader 返回第二个快照的两篇文档。
- 章节范围 `Alpha` 同时返回 `Alpha` 与 `Alpha child` 下的块；重复范围不会重复返回同一块。
- 字面扫描 `mcp` 命中含 `MCP` 的块；不存在的字面词返回合法 `count=0`。
- 显式扫描范围包含不存在文档时返回 `scope_not_found`，不扫描其余文档冒充部分成功。

### 人工验收

- [x] D1 固定字符 snippet 规则已确认。

## 反向解释

1. 为什么 reader 创建后再次查询不能重新读取活动指针？
2. 为什么 `scan_term` 的 `count=0` 不是“概念不存在”？
3. 为什么章节路径不存在时不能返回其他章节的块？

## 协作复盘

- `result_tokens` 由 Harness 统一计算，016 不混入对话模型和 tokenizer 依赖。

## 已知风险与后续

- 当前快照只为每篇文档保存完整 `document.json`；全库 `scan_term` 会按需读取文档 artifact。若 1,000 篇规模下性能不达标，后续增加轻量 block 目录或 SQLite 派生索引。
