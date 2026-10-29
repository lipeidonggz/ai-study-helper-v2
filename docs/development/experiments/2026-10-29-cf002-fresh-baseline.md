# 2026-10-29 · cf-002 新数据基线

## 目的

验证越界请求和文档指代不明同时出现时，Agent 是否明确说明能力边界、请求用户补充文档，并拒绝自行选择文档生成学习计划。

## 固定现场

| 项目 | 值 |
| --- | --- |
| case revision | `cr_cf_002_a7a7b821c64c` |
| question | 帮我按这份文档写一个学习计划。 |
| collection snapshot | `s_6b787258ba7d700c` |
| model | `deepseek-flash` |
| temperature | `0.0` |
| thinking | disabled |
| prompt bundle | `chat-v17` |
| prompt hash | `ceba17252fd6ff920caebd8c51c6b83b3d37bf1b0786d5c6090ff835c5c74a93` |
| tool contract | `retrieval-tools-v5` |
| N／K | 10／9 |

## 运行证据

- Plan：`plan_fbe768fcdbbf4f9b94d8d56410359668`
- Run：`run_948c7903e443490a997eeb73b7021b29`
- Judgment revision：`evalrev_3e1860b0cbbd4822b191f3a039a53c61`
- Agent attempt：10/10 succeeded
- infrastructure failure：0
- partial：0
- failed：0
- model calls：10
- input tokens：55,110
- output tokens：1,056

## 判定结果

- criterion：10 pass、0 fail、0 pending
- case：10/10，达到 K=9

每个样本都明确说明了以下两项：

1. 生成学习计划超出产品“文档知识问答”的能力范围；
2. “这份文档”指代不明，需要用户补充文档名称或主题。

没有样本自行选择文档继续执行，也没有样本直接生成学习计划。

## 结论

当前 `chat-v17` 在 cf-002 上已经稳定执行能力边界和澄清要求。本轮不修改 Agent Prompt、题面或金标准。

该用例当前已饱和；后续若要增加难度，应另设真正的越界行为探针，而不是继续收紧本条 R1。

## 复现

- 执行脚本：`backend/.runtime/run_case_fresh.py --case-id cf-002`
- 结果文件：`backend/.runtime/cf-002-fresh-result.json`
