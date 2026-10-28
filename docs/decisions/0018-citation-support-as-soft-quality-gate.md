# 0018 · 引用语义支持阶段性改为软质量门禁

- 状态：已接受
- 决定日期：2026-10-28
- 影响范围：引用语义支持、attempt/case 聚合、评测结果矩阵、人工复核

## 背景

ADR 0013 为避免单次 LLM 语义判断直接形成红线，要求所有 claim supported 才让 attempt pass。真实 c6 v7 run 表明，即使 R／X 和引用完整性已经稳定，少量关系型综合 claim 的 unknown 仍会让整个 N/K 结论停留在 pending。

当前更重要的改进目标是先稳定答案完整性、证据选择和确定性引用完整性。语义支持率仍需观察，但不适合在现阶段作为 K 的阻塞门禁。

## 决定

1. 保留 `citation_integrity` 为确定性硬红线。
2. `citation_support` 改为 attempt 级软质量指标，不再改变 attempt 或 case 的 pass／fail／pending。
3. 支持率定义为 `supported_count / occurrence_count`；unknown 计入分母，不计入 supported。
4. 至少 10 个 occurrence 才按 90% 阈值评级：`>= 90%` 为 good，`< 90%` 为 review。
5. occurrence 少于 10 时为 `insufficient_sample`；没有 occurrence 时为 `not_applicable`。
6. 原判官结果、claim 结果、人工复核、trace 与 invocation 继续 append-only 保留。
7. 人工确认 unsupported 只更新软质量统计，不再自动形成语义红线。

## 候选方案

| 方案 | 结论 | 原因 |
| --- | --- | --- |
| 继续要求所有 occurrence supported | 不采用 | 少量 judge 分歧会无限阻塞 N/K |
| 低于 90% 仍阻止 attempt/case | 不采用 | 仍是语义红线，只是换了阈值 |
| 支持率作为独立品质指标 | 采用 | 可观察、可回归，同时不把 judge 抖动升级为硬失败 |
| 直接删除 citation support | 不采用 | 会丢失真实引用支持问题与后续改进数据 |

## 后果

- c6 等真实 run 可以按 R／X 与 citation integrity 完成 K 判定，同时保留引用支持率。
- 少量错误引用可能不再阻止 case pass；结果矩阵必须明确显示支持率和待观察状态。
- “支持率”只表示已标注引用的语义支持情况，不代表所有应标 claim 均已覆盖。

## 验证

- 覆盖 9/10 good、8/10 review、3/3 insufficient_sample。
- 覆盖 citation support pending/fail 不改变完全通过的 R/X attempt。
- 覆盖 citation integrity fail 仍使 attempt fail。
- 使用 v7 c6 现有 revision 重新投影，不重新调用 Agent 或 judge：revision 从旧门禁下的 incomplete 投影为 complete；10 个 attempt 的支持率均为 good，最低 91.7%；case 变为 8 pass、2 fail、0 pending，并因低于 K=9 而失败。

## 推翻条件

- 评测台无法稳定区分 citation support 软质量与 citation integrity 红线；
- 支持率低于 90% 的答案持续伴随高用户可见引用错误，且 soft gate 被误当作质量通过；
- 漏标覆盖率成为主要质量缺口，需要先补 citation coverage 而不是继续调整支持率。
