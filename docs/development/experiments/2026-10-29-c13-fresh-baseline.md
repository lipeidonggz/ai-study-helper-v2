# 2026-10-29 · c13 新数据基线

## 目的

使用当前 `chat-v15` 重新执行 c13-001 N=10，验证 Agent 能否列出除题面已给的沙箱、虚拟机和出口控制之外的隔离手段。

## 固定现场

| 项目 | 值 |
| --- | --- |
| case revision | `cr_c13_001_minimal_judge_v2` |
| question | 除了沙箱／虚拟机／出口控制，还有哪些隔离手段？ |
| collection snapshot | `s_6b787258ba7d700c` |
| model | `deepseek-flash` |
| temperature | `0.0` |
| thinking | disabled |
| prompt bundle | `chat-v15` |
| prompt hash | `3d52504d28d999d70ba3070bb2f78e26166d0c57538d703f53f29d646bb67220` |
| tool contract | `retrieval-tools-v5` |
| N／K | 10／9 |
| concurrency | 10 |
| token budget | 5,000,000 |
| model request budget | 2,000 |

## 运行证据

- Plan：`plan_5af0f7dfb2f14b0b8c6ac2564401e204`
- Run：`run_4e54628da1024fc9bf749c7810c5881b`
- Judgment revision：`evalrev_19aa3bfecc8c44129162869726d7e583`
- Agent attempt：10/10 succeeded
- infrastructure failure：0
- partial：0
- failed：0
- model calls：63
- input tokens：468,560
- output tokens：32,992

## 判定结果

- criterion：2 pass、8 fail、0 pending
- case：2/10 pass，未达到 K=9
- X 红线违规：0
- citation integrity：10/10 pass
- citation support：大部分 good；2 次 review，无 check failure/pending

## 失败模式

8 个失败样本并非完全同因，主要遗漏包括：

- 文件系统挂载模式不完整；
- 凭证隔离只写“凭据不进沙箱”，没有宿主机钥匙串或会话级缩权；
- 工具权限最小化缺失；
- 工具输出检查缺失。

同时发现 R1 与题面存在直接冲突：

- 题面明确要求回答“除了沙箱／虚拟机／出口控制，还有哪些隔离手段”；
- R1 却把“出口代理与检查”列为五类必选之一。

这会把题面明确排除的手段重新变成必答项。

## 初步结论

c13 不能直接归因为 Agent 能力不足。当前金标准至少存在两个问题：

1. R1 包含题面已排除的出口控制；
2. R1 要求具体的挂载模式和会话级 token 等实现细节，而题目只要求识别其他隔离手段及其作用。

应先修正 c13 criterion，再决定是否调整 Agent。

## 复现

- 执行脚本：`backend/.runtime/run_case_fresh.py --case-id c13-001`
- 结果文件：`backend/.runtime/c13-fresh-result.json`
