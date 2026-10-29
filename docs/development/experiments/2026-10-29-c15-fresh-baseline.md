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

## 复现

- 执行脚本：`backend/.runtime/run_case_fresh.py --case-id c15-001`
- 结果文件：`backend/.runtime/c15-fresh-result.json`

