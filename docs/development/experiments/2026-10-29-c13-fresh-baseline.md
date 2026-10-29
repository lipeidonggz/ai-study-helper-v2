# 2026-10-29 · c13 新数据基线

## 目的

使用当前 `chat-v15` 重新执行 c13-001 N=10，验证 Agent 能否列出除题面已给的沙箱、虚拟机和出口控制之外的隔离手段。

> **后续更正：**本文后续的单篇文章重判结果只说明特定过渡判据下的生成表现，不能作为第 13 类“全库补充召回”的最终基线。原题恢复、产品口径与千篇规模检索方案已登记到 [BL-0012](../backlog.md) 和 [开放范围问题分析](2026-10-29-c13-open-scope-retrieval-analysis.md)。

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

## 修正后重判

新题面、R1 和边界项：

- case revision：`cr_c13_001_3b7d3737f841`
- R1 revision：`ccr_c13_001_r1_eb048a4d3d`
- X1 revision：`ccr_c13_001_x1_027ac78e1b`
- X2 revision：`ccr_c13_001_x2_50758d2ea5`
- judgment revision：`evalrev_6ab295d8c0c343cc80d59aaa78f51f81`

结果：

- criterion：27 pass、3 fail、0 pending
- case：7 pass、3 fail、0 pending
- X1：10/10 不违规
- X2：10/10 不违规

判官一致性异常：`attempt_2e8fa87d...` 被记录为 R1
`satisfied`，但同一条 reason 明确写道没有凭证隔离，
“四类缺少一类，不能算完整满足”。这是 verdict 与 reason
自相矛盾，系统中按 verdict 暂时计为 pass；人工核对答案正文后，
该 attempt 应视为 fail。因此 7/10 只能视为待修正的判官聚合结果，
人工有效结果至少为 6 pass、4 fail。

剩余失败是真实遗漏，而非措辞差异：

- `attempt_af9efea7...`：缺文件系统边界与凭证隔离；
- `attempt_b00c2499...`：文件系统边界覆盖不足，缺凭证隔离，外部内容检查不充分；
- `attempt_de845a1b...`：缺工具权限最小化和工具／外部内容检查。

修正后，c13 仍有 3/10 输出没有完整覆盖四类要求，因此未达到 K=9。问题已经从“题面冲突和过度细粒度”收敛为“Agent 在枚举型问题上的完整覆盖不稳定”。

## judge-rubric-v3 reason-first 最终重判

为排除判官 verdict/reason 自相矛盾，新增 `judge-rubric-v3`：

- 保留 `judge-rubric-v2` 不变，用于历史重放；
- v3 要求先写完整 reason，再给 verdict；
- 过渡重判使用 `cr_c13_001_84e75a91dbcb` 和
  `evalrev_3d9efdb5526a474580fb0b324bd228b3`；
- 负责人确认 X1“排除项冒充新增”是低影响分类问题后，最终
  case revision 删除 X1，只保留 R1 和 X2：
  `cr_c13_001_6c08a5db0d8f`；
- 最终 judgment revision：
  `evalrev_d44ad528832a472c98c125f47eb90dd6`。

结果：

- 20 次最终 judge invocation 全部为 reason-first；
- `attempt_2e8fa87d...` 的 verdict/reason 矛盾消失，正确改判 fail；
- `attempt_5ccff8c8...` 最终被 R1 判 fail，原因是缺少工具／外部内容检查；
- criterion：15 pass、5 fail、0 pending；
- case：5 pass、5 fail、0 pending；
- X2：10/10 not_violated；
- 两个 active suite 已同步到最终 revision。

### 五个失败的具体原因

| attempt | 已覆盖 | 实际缺失 |
| --- | --- | --- |
| `attempt_af9efea7...` | 工具权限最小化；工具输出检查；模型层和外部内容层说明 | 没有说明文件系统边界或挂载范围，也没有说明凭证不进入 agent／沙箱 |
| `attempt_2e8fa87d...` | 工具权限最小化；工具输出检查；模型层和外部内容层说明 | 没有说明文件系统边界或挂载范围，也没有说明凭证不进入 agent／沙箱 |
| `attempt_b00c2499...` | 工具权限最小化；模型层；人在回路；防御性代理 | 只用一句提及文件系统边界，未说明限制范围；完全缺少凭证隔离；只说明连接器数据可能不可信，没有说明进入上下文前的检查机制 |
| `attempt_5ccff8c8...` | 文件系统边界；凭证隔离；工具权限最小化 | 没有说明工具返回值或外部内容在进入模型上下文前经过检查；模型层分类器不能替代该类 |
| `attempt_de845a1b...` | 文件系统边界、挂载模式和符号链接风险；凭证不进入沙箱 | 没有工具权限最小化；没有工具输出或外部内容检查；模型层分类器和 Egress 代理不能替代这两类 |

这 5 条不是 judge 漏看同义表达：逐条核对完整答案后，缺失类别确实不存在。它们共同表现出一个模式：答案优先组织“三层防御、人和流程、架构取舍”等叙述，却没有把用户要求的具体手段作为清单逐项覆盖。

所有 10 个 attempt 的上下文都能取得四类措施的原文，且全部工具结果已准入，所以剩余问题属于最终答案的信息选择与枚举覆盖，不是检索问题。

注意：本轮只是用最终 criterion 重判旧题面生成的 10 个答案；最终题面已经改为明确指向 Anthropic《How we contain Claude》的“隔离或控制手段”，仍需要重新生成 N=10，才能评价新题面下的最终生成稳定性。

## 复现

- 执行脚本：`backend/.runtime/run_case_fresh.py --case-id c13-001`
- 结果文件：`backend/.runtime/c13-fresh-result.json`
