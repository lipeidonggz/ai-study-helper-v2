# 2026-10-29 · c15 新数据基线

## 目的

使用 `chat-v16` 重新执行 c15-001 N=10，验证 Agent 能否把 Anthropic《How we contain Claude》的三种隔离模式整理为包含模式、产品、机制、威胁模型和代价的表格。

## 固定现场

| 项目 | 值 |
| --- | --- |
| case revision | `cr_c15_001_minimal_judge_v2` |
| question | 把 Anthropic《How we contain Claude》里的三种隔离模式整理成一张表：模式／产品／机制／威胁模型／代价。 |
| collection snapshot | `s_6b787258ba7d700c` |
| model | `deepseek-flash` |
| temperature | `0.0` |
| thinking | disabled |
| prompt bundle | `chat-v16` |
| prompt hash | `681fe6c18081787ac678440e953fbb40826f2c39ef166dd461757e250b0cf49f` |
| tool contract | `retrieval-tools-v5` |
| N／K | 10／9 |

## 运行证据

- Plan：`plan_01c5c888aae84299aa520a487e1c7118`
- Run：`run_83e8580ee0244a1cb0cb3a6874d559e2`
- Judgment revision：`evalrev_a1872b3e51ce46adae4a0f212d82215a`
- Agent attempt：10/10 succeeded
- infrastructure failure：0
- partial：0
- failed：0
- model calls：60
- input tokens：406,951
- output tokens：38,609

## 判定结果

- criterion：58 pass、2 fail、0 pending
- case：8/10 pass，未达到 K=9
- X 红线违规：0
- citation integrity：10/10 pass
- citation support：大部分 good；一个 attempt 为 review（20/25 supported，5 unknown），无 check failure/pending

## 失败模式

两个失败的 attempt 都是 SQL 型失败，且都缺少同一条 R3 要素：

- `attempt_b114e9e0...`：Cowork 的威胁模型写成“Claude 可能以不对齐方式行事”，没有说明其面向“无法判断 bash 风险的非技术用户”。
- `attempt_40833a6c...`：同样把 Cowork 威胁模型写成“模型可能以不对齐方式行为”，没有明确面向无法判断 bash 风险的非技术用户。

R1、R2、R4、X1、X3 在 10 个 attempt 中全部通过。失败不是模式、机制、代价或边界违规，而是同一条威胁模型描述缺失。

## 待复核

R3 当前要求三种模式的威胁模型与隔离边界，其中 Cowork 部分要求说明“面向无法判断 bash 风险的非技术用户”。R4 已经单独要求三种模式的代价及用户依赖，因此需要确认：

- 用户监督能力是否属于 R3 的威胁模型必要组成；
- 还是应主要放在 R4 的用户依赖中；
- 若保留在 R3，答案需要多明确才能算通过。

在确认前不修改 c15 判据、提示词或重跑 Agent。

## 全量 Judge v3 迁移后重判

全部 active LLM judge criteria 迁移到 v3 后，对同一 run 做两次冻结重判：

- 新 active case revision：`cr_c15_001_8a87e56d8461`
- 第一次 judgment：`evalrev_8c7ba13b6f0f4cfab6ebe1e05134b3f1`
- 第二次 judgment：`evalrev_defbeac29dc84886b6b9e1a05b6ba850`
- 两次 case 结果均为 5/10
- 第一次 criterion：54 pass、6 fail
- 第二次 criterion：55 pass、5 fail

两次 v3 都不是 v2 的 8/10，说明 c15 对 Judge 版本敏感。具体差异见 [Judge v3 全量迁移实验](2026-10-29-judge-v3-active-migration.md)。

## Judge v4 与 R3 语义校准

- v4 位置无关重判：5/10，criterion 55 pass、5 fail。
- 最终 R3 监督能力语义 revision：`cr_c15_001_8e3e924a477c`
- Judgment：`evalrev_7746eae747884557858ef36efbd6b49a`
- criterion：56 pass、4 fail、0 pending
- case：7 pass、3 fail，未达到 K=9

最终 3 个失败：

- `b114e9e0...`：R3 缺少 Cowork 用户监督能力假设；
- `40833a6c...`：同类 R3 缺失；
- `7056ba70...`：R2 缺少 Claude Code 本地运行位置，R3 缺少监督能力假设。

R4 的位置误杀已消除；剩余问题属于 Agent 对复合维度的覆盖不完整。

## chat-v17 通用覆盖规则

新增：

- `answer_coverage`：识别问题要求的语义覆盖单位；机制、原因、代价、前提、边界等复合维度有多个关键要素时不得只写一个显眼点；
- `output_structure`：用户指定表格、清单、矩阵或字段时保持对应结构，但细节可以放在表后。

固定现场：

- prompt bundle：`chat-v17`
- prompt hash：`ceba17252fd6ff920caebd8c51c6b83b3d37bf1b0786d5c6090ff835c5c74a93`
- run：`run_3703aae0e8a947bb8368868259364d64`
- Agent：10/10 succeeded，0 infrastructure failure
- model calls：60
- input tokens：424,180
- output tokens：38,943

首次判定 `evalrev_09e74132086c449bb13db4ff95a35263` 为 58 pass、1 fail、1 pending。pending 来自判官未返回合法 criterion 结果，不是 Agent 失败。

对同一 run 第二次重判：

- Judgment revision：`evalrev_563a075780124887b5d3e0cacceac71e`
- criterion：59 pass、1 fail、0 pending
- case：9/10，达到 K=9
- X1/X3：全部通过

唯一失败：

- `attempt_20014899...`：R3 只写了 Cowork 防止模型不对齐和挂载/VM 边界，没有写不能依赖用户判断 bash 风险，因此不能把 HITL 审批作为可靠安全边界。

结果从旧答案的 7/10 提升到新答案的 9/10，但没有证明所有复合维度都能稳定覆盖。

## 复现

- 执行脚本：`backend/.runtime/run_case_fresh.py --case-id c15-001`
- 结果文件：`backend/.runtime/c15-fresh-result.json`
