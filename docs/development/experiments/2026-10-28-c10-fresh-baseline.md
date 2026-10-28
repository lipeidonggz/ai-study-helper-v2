# 2026-10-28 · c10 新数据基线

## 目的

使用当前产品配置重新执行 c10-001 N=10，验证 Agent 能否准确判断“AGENTS.md 写得越详细越好”不成立，并给出原文依据和正向替代方案。

## 固定现场

| 项目 | 值 |
| --- | --- |
| case revision | `cr_c10_001_minimal_judge_v2` |
| question | “AGENTS.md 写得越详细越好”——按 OpenAI《Harness engineering》的说法，这个说法对吗？ |
| collection snapshot | `s_6b787258ba7d700c` |
| model | `deepseek-flash` |
| temperature | `0.0` |
| thinking | disabled |
| prompt bundle | `chat-v13` |
| prompt hash | `0ae72288d45eb7bfbede0aae0a4a259bc9809e49733eb58f15c912d0ce256e95` |
| tool contract | `retrieval-tools-v5` |
| N／K | 10／9 |
| concurrency | 10 |
| token budget | 5,000,000 |
| model request budget | 2,000 |

## 运行证据

- Plan：`plan_ae77494e7ed14aaebe91112210085b6d`
- Run：`run_f40fdbfc013d4748babac8d4d6d3f9c3`
- Judgment revision：`evalrev_55313e993b68429c86948c29a3abca69`
- Agent attempt：10/10 succeeded
- infrastructure failure：0
- partial：0
- failed：0
- model calls：55
- input tokens：273,653
- output tokens：21,383

## 判定结果

- criterion：50/50 pass，0 fail，0 pending
- case：10/10 pass，达到 K=9
- X 红线违规：0
- citation integrity：10/10 pass
- citation support：10/10 good，全部 occurrence supported，无 unsupported／unknown

## 答案共性

10 个通过样本都：

- 开头明确判断“AGENTS.md 越详细越好”不成立；
- 给出上下文空间稀缺、过多指导等于没有指导、难机械校验且容易漂移等原文依据；
- 说明正确处理方式是让 AGENTS.md 作为约 100 行的目录或地图，详细知识放入结构化 `docs/` 并作为事实源；
- 没有把 AGENTS.md 说成不重要，也没有把约 100 行解释成强制阈值。

## 结论

当前配置在 c10-001 上达到 N=10、K=9。该结果使用本次新生成的数据和本次判定，不包含旧 attempt。

## 复现

- 执行脚本：`backend/.runtime/run_case_fresh.py --case-id c10-001`
- 结果文件：`backend/.runtime/c10-fresh-result.json`
