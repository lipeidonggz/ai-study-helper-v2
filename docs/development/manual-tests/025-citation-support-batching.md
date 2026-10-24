# 025 · 引用支持分批检查人工验收

- 状态：已完成
- 日期：2026-10-24
- 关联切片：[025 长答案引用支持分批检查](../slices/025-evaluation-citation-support-batching.md)

## 验收项

- [x] cf-003 长答案不再因总证据输入超限而直接 pending。
- [x] 判定详情能看清批次数、总 occurrence 数和 block 合并结果。
- [x] 段落、列表项和表格引用没有错误退化成只检查前一单句。
- [x] 超长单语义单元、超时或非法输出显示明确 pending 原因。
- [x] 人工逐 block 引用复核仍可使用。
- [x] 旧 EvaluationRevision 和旧 CheckResult 未被修改。

## 人工证据

- 2026-10-24 负责人确认引用支持分批摘要和 pending 错误展示可读。
- 真实 cf-003 trace 使用 15 个批次完成 72 个 occurrence，无输入超限、输出截断或 occurrence 覆盖错误。
- 真实判定发现 68 supported、1 unsupported、1 unknown；检查链正常完成，结果为 fail。
