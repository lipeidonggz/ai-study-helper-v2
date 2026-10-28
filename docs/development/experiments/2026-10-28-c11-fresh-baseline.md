# 2026-10-28 · c11 新数据基线

## 目的

使用当前产品配置重新执行 c11-001 N=10，验证 Agent 能否识别 OpenAI《Harness engineering》与 Anthropic《How we contain Claude》对“人在回路”的论述不是同一命题下的对立，并说明各自适用前提。

## 固定现场

| 项目 | 值 |
| --- | --- |
| case revision | `cr_c11_001_minimal_judge_v2` |
| question | OpenAI《Harness engineering》主张「人类掌舵、智能体执行」，而 Anthropic《How we contain Claude》主张先把环境边界焊死、不把安全建立在人类监督上——该听谁的？ |
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

- Plan：`plan_e88b2c67946644ed86eea3305da6fec0`
- Run：`run_cf2f32388c8e4366ad743541249286c7`
- Judgment revision：`evalrev_71f5e74037544d2383030b4a2b20b38f`
- Agent attempt：10/10 succeeded
- infrastructure failure：0
- partial：0
- failed：0
- model calls：73
- input tokens：650,474
- output tokens：54,027

## 判定结果

- criterion：39/40 pass，1 fail，0 pending
- case：9/10 pass，达到 K=9
- X 红线违规：0
- citation integrity：10/10 pass
- citation support：6 次 good、4 次 review；无 unsupported，review 来自 unknown occurrence

唯一 fail：

- attempt：`attempt_5fba251e31c547a199005739d8afc59b`
- criterion：`c11-001-r2`
- judge reason：答案没有说明 Anthropic 面向“通用产品和非技术用户”

## 首轮失败与金标准修正

该答案指出：

- Anthropic 的 Claude Code human-in-the-loop sandbox 明确以“懂 bash、能评估风险”的开发者作为可行前提；
- 同时区分能读 bash 的开发者与不能判断命令的知识工作者；
- 并用“隔离强度要匹配用户的监督能力”概括不同产品的差异。

这与 Anthropic 原文一致。原文并非只面向通用产品和非技术用户，而是同时覆盖 claude.ai、面向开发者的 Claude Code 和面向非技术知识工作者的 Claude Cowork。

因此本次唯一 fail 是 R2 前提表述过窄造成的 false negative，而不是 Agent 漏答。

修正后的 case revision 和判定：

- case revision：`cr_c11_001_4f89ebe124da`
- R2 revision：`ccr_c11_001_r2_92dedd6661`
- judgment revision：`evalrev_e187ee90c93a4c4bbeff1ee9a098bd18`
- criterion：40/40 pass
- case：10/10 pass

新 R2 要求 Anthropic 面向监督能力不同的产品用户：非技术用户需要环境层绝对边界；开发者 HITL 仍受审批疲劳限制；隔离强度匹配用户监督能力。

## 结论

本次新数据在修正后的 R2 下为 10/10 pass，原 false negative 已消除，c11 可视为干净基线。

## 复现

- 执行脚本：`backend/.runtime/run_case_fresh.py --case-id c11-001`
- 结果文件：`backend/.runtime/c11-fresh-result.json`
