# 2026-10-29 · active Judge v3 全量迁移

## 目的

把全部 active LLM judge criteria 从按 case 零散绑定迁移为统一 `judge-rubric-v3`，并用 c15 原 run 比较 v2 与 v3 的判定差异。

## 迁移范围

- 迁移前：16 个 active case 仍绑定 v2；c13、c14 已绑定 v3。
- 迁移后：18 个 active case 的所有 `llm_judge` criteria 均为 v3。
- checker：`cf-003R1` 继续使用 `truncation_declared`。
- 新库 seed：`minimal_judge_v2` 输入契约保留历史 id，但新创建 criterion 默认绑定 `JUDGE_PROMPT_VERSION`（当前 v3）。

## Active suites

- `sr_023b-formal-acceptance_5beb3ece4b83`
- `sr_c7-current_f352cd634972`
- `sr_design03_b90e59cd6c51`
- `sr_逐步增加的用例_3c3cfb4ec800`
- `sr_除截断用例外所有_55dc5abba473`

以上 suite 均已重绑迁移后的 active case revisions，`stale=false`。

## c15 比较

原 run：`run_83e8580ee0244a1cb0cb3a6874d559e2`

| Judge | Case revision | Judgment | Criterion | Case |
| --- | --- | --- | --- | --- |
| v2 | `cr_c15_001_minimal_judge_v2` | `evalrev_a1872b3e51ce46adae4a0f212d82215a` | 58 pass、2 fail | 8/10 |
| v3 第一次 | `cr_c15_001_8a87e56d8461` | `evalrev_8c7ba13b6f0f4cfab6ebe1e05134b3f1` | 54 pass、6 fail | 5/10 |
| v3 第二次 | `cr_c15_001_8a87e56d8461` | `evalrev_defbeac29dc84886b6b9e1a05b6ba850` | 55 pass、5 fail | 5/10 |

两次 v3 都是 5/10，但失败 attempt 集合并不稳定：

- 两次稳定 fail：`b114e9e0...`、`40833a6c...`、`7056ba70...`
  - 均为 R3：Cowork 威胁模型没有说明面向无法判断 bash 风险的非技术用户。
- 两次稳定 pass：`352e211a...`、`57a871e3...`、`2e4fb15a...`、`ea7261a7...`
- R2/R4 抖动：
  - `41768933...`：v2 pass；两次 v3 均 fail（第一次 R2+R4，第二次 R4）。
  - `ac8b7fcb...`：v2 pass；第一次 v3 R4 fail，第二次 pass。
  - `3ae8b611...`：v2 pass；第一次 v3 pass，第二次 R2 fail。

## 结论

- 全量 active Judge v3 迁移本身完成，后续新运行不再受 case 历史创建时间影响。
- c15 的 v3 结果明显低于 v2，不能解释为 Agent 退化。
- R3 的 3 个稳定失败与“用户监督能力属于威胁模型”一致。
- R2/R4 在相同答案上存在额外判定抖动，尤其 R4 对补充说明是否算“代价”解释不稳定。
- 当前不应据此修改 Agent prompt；应先校准 c15 R2/R4 的判官语义或确认 v3 是否过度要求表格单元格位置。

## Judge v4 位置无关与 c15 语义校准

为解决判官因信息不在“代价”单元格而误杀，新增 `judge-rubric-v4`：

- 保持 v3 的 reason-first 和理由/verdict 一致性；
- 除 criterion 明确要求位置外，答案任意位置的语义等价内容都计入；
- v2、v3 原文与 hash 保持独立。

全部 18 个 active case 和 5 个 active suite 已迁移到 v4。

### v4 净重判

- c15 case revision：`cr_c15_001_69dd7dafb275`
- Judgment：`evalrev_9e7b7ffe6a2c4c44b5a101a52ff2b607`
- criterion：55 pass、5 fail
- case：5/10

v4 消除了大部分 R4 位置误杀，但 `41768933...` 仍有一次 R4 误判，说明“位置无关”规则不能消除全部 Judge 抖动。

### R3 监督能力语义校准后的最终重判

- case revision：`cr_c15_001_8e3e924a477c`
- Judgment：`evalrev_7746eae747884557858ef36efbd6b49a`
- criterion：56 pass、4 fail、0 pending
- case：7 pass、3 fail、0 pending
- X1/X3：全部通过

剩余失败是：

- `b114e9e0...`：R3 缺少 Cowork 用户监督能力假设；
- `40833a6c...`：R3 同类缺失；
- `7056ba70...`：R2 缺少 Claude Code 本地运行位置，R3 缺少用户监督能力假设。

这说明 R4 位置误杀已消除，剩余失败主要来自 Agent 没有把原文中解释设计差异的关键前提写入回答。

## 复现

- 迁移前审计：`backend/.runtime/migrate_active_judge_v3.py`
- v3 第一次重判：`backend/.runtime/c15-rejudge-v3.json`
- v3 第二次重判：`backend/.runtime/c15-rejudge-v3-repeat.json`
- v4 语义修订前重判：`backend/.runtime/c15-rejudge-v4.json`
- v4 最终重判：`backend/.runtime/c15-rejudge-v4-semantic.json`
