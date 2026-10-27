# 038 · 引用证据全文覆盖截断片段

- 状态：已完成
- 开始日期：2026-10-27
- 关联设计：[06 检索与 Agent](../design/06-retrieval-pure-agent.md)、[08 评测台设计](../design/08-evaluation-console-design.md)、[ADR 0014](../../decisions/0014-prefer-full-evidence-over-snippets.md)
- 前置切片：[027 Claim-aware 引用标注](./027-claim-aware-citation-annotation.md)、[036 引用上下文与判官职责拆分](./036-citation-context-and-judge-separation.md)、[037 引用语义支持候选与人工复核门禁](./037-citation-support-review-gate.md)

## 控制契约

### 用户可观察目标

同一个 `block_id` 被多个工具返回时，citation annotation 必须看到本次 attempt 已获得的完整 block 正文；不得让更早返回的命中 snippet 覆盖后来的全文。

### 核心不变量

1. `block_id` 仍是证据身份，同一 attempt 内相同 ID 只保留一个最终候选。
2. 候选顺序仍按首次出现确定，后续全文只原位替换，不改变 block 排序。
3. `list_blocks`／`search_blocks` 的 `text` 视为全文；`scan_term` 的 `snippet` 视为局部窗口。
4. 被上下文预算拒绝的工具结果不进入最终 evidence、annotation 或最终回答上下文。

### 明确禁止

- 不因为后出现的全文相同而重复添加 block。
- 不用字符串长度猜测证据质量。
- 不让被拒绝的工具结果通过 finalization 或 annotation 重新进入上下文。

## 根因

`c3-001` 第一轮中，`scan_term("93%")` 先返回同一个 `b00002` 的截断 snippet：

```text
…Theoretically that works… Our telemetry showed users approved roughly 93%…
```

随后 `list_blocks` 返回完整 `b00002`，其中包含：

```text
Claude Code previously protected against agents taking unintended actions
by asking users for permission at each turn.
```

原实现按 `block_id` 首次出现合并，导致后到的全文被丢弃。annotation 因看不到精确原句，只能选择主题相近的 `b00004/b00005/b00008`，最终产生 4 个 `citation_evidence_quote_invalid`。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 同一 block 的 snippet 与全文如何合并 | 保持首次出现位置，用全文原位覆盖 snippet | block_id 是稳定身份；annotation 必须看到本次已获得的完整证据 | 先到先得会让检索窗口污染 annotation；按长度猜测不够明确 | Runtime evidence 合并和 annotation 输入 | snippet→full 单测、真实失败 span 回放 | 已确认 |
| D2 | B | 上下文预算拒绝的工具结果是否保留为证据 | 拒绝结果不进入 sources／evidence／finalization | 未进入模型上下文的工具结果不应在后续绕过预算 | 保留供 annotation 会规避上下文预算，属于静默扩展 | Runtime state、finalization、citation artifact | 超预算工具结果定向测试 | 已确认 |

## 证据包

### 自动化证据

- `test_full_evidence_text_replaces_earlier_snippet_for_same_block`：验证全文原位覆盖 snippet。
- `test_oversized_tool_result_is_dropped_and_answer_is_marked_partial`：验证被拒绝结果不重新进入 finalization。
- 完整后端测试与 `quality:quick`：通过。

### 运行证据

- 基线 run：`run_884d3fcc2d8246b58f1b6b101bf36de6`
- 基线 revision：`evalrev_3b1c29f21c43470596fb1a2b71590bbe`
- 基线：4 次 `citation_evidence_quote_invalid`、2 次 semantic unknown、1 次 protocol loop。
- 合并修复后 run：`run_dec939b11a314000ba1cc29e1560628d`
- 合并修复后 revision：`evalrev_5031d4a39ad744a09f51cf9c0d41f8c6`
- 结果：quote invalid 降为 0；8 次 support pass、2 次 structural unknown。
- 合并 + finish recovery 后 run：`run_37bf3e33d01f4b73bae7c4dbaa1b1af1`
- 最终 revision：`evalrev_20cc2bace8c240efa8fed452da3db448`
- 结果：10/10 attempt succeeded、50/50 criterion pass、6 support pass、4 structural unknown、0 quote invalid、0 protocol loop。

### 失败探针

- `attempt_9d0259b9c8b74ad0afa22237c57d49eb`：修复前 annotation 的 `b00002` 是 snippet；完整文本覆盖后，单 span 回放选中 `b00002`。
- 4 个最终 unknown 都是“文档只有一个章节／位于开头”等结构元信息，正文 block 本身不能证明结构，保持 pending 是合理的。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 同一 block 先返回 snippet、后返回全文时，annotation 应使用哪一份？ | 使用全文；模型很可能依据全文生成，而且全文包含先前 snippet。 | 通过 | 无 |
| R2 | 因上下文预算被拒绝的工具结果能否重新进入 evidence 或 annotation？ | 不能；这些 block 没有进入 Agent 当时可见的 History。 | 通过 | 无 |
| R3 | 为什么不按字符串长度猜证据质量？ | 证据完整性必须由工具字段来源和读取范围决定，不能靠启发式长度判断。 | 通过 | 无 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 已知风险与后续

- 结构元信息 claim 仍会形成 citation unknown；后续可决定是否由 Harness metadata 提供单独的可验证来源，而不是让正文 block 冒充结构证据。
