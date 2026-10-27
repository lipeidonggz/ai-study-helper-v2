# 0014 · 同一证据块优先使用全文而不是命中片段

- 状态：已采纳
- 决定日期：2026-10-27
- 影响范围：Agent 工具证据合并、citation annotation、上下文预算与引用评测

## 背景

同一 attempt 中，`scan_term` 可能先返回 block 的局部命中 snippet，`list_blocks` 后返回同一 block 的全文。原实现按 `block_id` 首次出现合并，导致全文被当作重复项丢弃。

`c3-001` 因此让 annotation 只看到 `b00002` 的中间片段，无法看到 `at each turn` 原句，转而选择主题相近但错误的 block。

## 决定

1. 相同 `block_id` 保持首次出现位置。
2. 来自 `text` 的全文可以原位覆盖来自 `snippet` 的局部命中窗口。
3. 已被上下文预算拒绝的工具结果不进入 sources、evidence、annotation 或 finalization。
4. 不使用字符串长度作为通用质量判断，必须由工具字段来源决定优先级。

## 后果

- Citation annotation 总能看到本次已获准进入上下文的完整证据。
- 不改变 block 顺序、身份或 citation artifact schema。
- 被拒绝的工具结果不能通过后续阶段重新进入上下文。

## 验证

- 失败现场回放：完整 `b00002` 覆盖 snippet 后，单 span annotation 从 `b00008` 改为 `b00002`。
- c3-001 修复前 4 次 quote invalid；修复后为 0。
- 最终 c3-001 run：10/10 attempt succeeded、50/50 criterion pass。
