# 2026-10-28 · c9 新数据基线

## 目的

使用当前产品配置重新执行 c9-001 N=10，不复用历史 attempt 或历史判定，确认“两篇文章是否在人在回路问题上互相矛盾”的综合判断在最新数据上是否稳定。

## 固定现场

| 项目 | 值 |
| --- | --- |
| case revision | `cr_c9_001_minimal_judge_v2` |
| question | OpenAI《Harness engineering》说人类不一定要评审 PR，而 Anthropic《How we contain Claude》说人类审批会退化、不可靠——这两篇对「人在回路」是不是在唱反调？ |
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

- Plan：`plan_47aa6359162944dbb0a1e9d820b78984`
- Run：`run_19f0ba0f555c43969e419dac490f5899`
- Judgment revision：`evalrev_2b1a0cae70fb4b7f98cdf606b5a4f8b5`
- Agent attempt：10/10 succeeded
- infrastructure failure：0
- partial：0
- failed：0
- model calls：66
- input tokens：494,793
- output tokens：43,400

## 判定结果

- criterion：50/50 pass，0 fail，0 pending
- case：10/10 pass，达到 K=9
- X 红线违规：0
- citation integrity：10/10 pass
- citation support 质量：8 次 good、2 次 review；无 unsupported，review 来自 unknown occurrence

## 答案共性

10 个通过样本都明确区分了两个“人在回路”层次：

- Anthropic 质疑的是运行时逐次权限审批作为安全防线的可靠性；
- OpenAI 讨论的是代码评审和合并流程，人类不必承担逐 PR 评审；
- 两篇都没有主张人类完全退出，而是把人的职责上移到方向、优先级、验收和例外判断。

## 结论

当前配置在 c9-001 上达到 N=10、K=9。该结果是本次新生成的数据和本次判定的结果，不包含旧 attempt。

## 复现

- 执行脚本：`backend/.runtime/run_c9_fresh.py`
- 结果文件：`backend/.runtime/c9-fresh-result.json`
