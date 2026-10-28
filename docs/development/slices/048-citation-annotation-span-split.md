# 048 · 引用标注输出截断后的 span 二分

- 状态：已完成
- 开始日期：2026-10-28
- 关联设计：[06 检索与 Agent](../../design/06-retrieval-pure-agent.md)、[08 评测台](../../design/08-evaluation-console-design.md)
- 前置切片：[044 表格行联合证据](./044-citation-table-evidence-coverage.md)、[047 c8 综合归属边界](./047-c8-synthesis-attribution-boundary.md)

## 控制契约

### 背景与失败场景

- 触发问题：c8 最新 run 中 10 个 attempt 有 2 个 citation integrity pending，错误码都是 `citation_annotation_output_truncated`。
- 不处理时的用户损失：长答案的引用标注整体失效，引用完整性和后续 support 都无法完成。
- 现有流程为什么不足：annotator 只按 evidence block 分批，每个 evidence 批次仍携带全部 spans；annotation JSON 达到 4096 token 上限后直接 pending。
- 本切片负责什么：遇到输出长度截断时将 span 组二分后重试，直到成功或只剩单个 span。
- 本切片不负责什么：不增加总输出上限，不牺牲 span 完整性，不改变 evidence 批次上限，不修改 citation judge。

### 用户可观察目标

长答案仍能生成完整的 span 引用关系；只有单个 span 自身无法在输出预算内完成时才保持显式 pending。

### 核心不变量

1. `finish_reason=length` 且 span 组大于 1 时，必须二分后继续，不能整批 pending。
2. 二分后仍覆盖原 span 集合，不增加、遗漏或重复选择。
3. 最终 annotations 按答案位置排序。
4. 单个 span 仍截断时保留 `citation_annotation_output_truncated`。
5. 所有二分调用进入 messages、raw_outputs、token usage 和模型调用审计。

### 明确禁止

- 不得通过跳过部分 span 伪造 complete。
- 不得只提高输出上限而不改变截断行为。
- 不得修改 answer 或 evidence。
- 不得把二分失败静默降级为空 annotations。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 多 span 截断 | 自动二分并继续 | 原始截断输出和子批调用 | 是 |
| 单 span 截断 | citation integrity pending | 截断输出与错误码 | 是 |
| 子批协议错误 | pending | 所有子批输出 | 是 |

### 决策预算

- 预计 B 级决策数量：1。
- 是否存在 C 级决策：否。

## 目标

1. 实现截断触发的 span 二分。
2. 更新 batch 协议版本。
3. 用 c8 两个截断冻结现场验证，不重跑 Agent。

## 不包含

- 不新增 span 数量硬上限。
- 不改变 citation support 软质量规则。
- 不修改引用提示词。

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 |
| --- | --- | --- | --- | --- | --- |
| A1 | 长答案不再整体失败 | 多 span 首次截断后二分成功 | 4 spans -> 1 次截断 + 2 次成功 | 必须 | 单元 |
| A2 | 单 span 不掩盖失败 | 单 span 持续截断仍 pending | 2 次失败调用 | 必须 | 单元 |
| A3 | 合并完整 | 二分结果按答案顺序合并、无遗漏 | span 集合一致 | 必须 | 单元 |
| A4 | 审计完整 | 每次调用进入 messages/raw_outputs/usage | 调用数一致 | 必须 | 单元 |
| A5 | 真实现场恢复 | c8 两个截断 attempt 可重新完成标注 | 2/2 attempt、6/6 repeat 均 complete | 必须 | 冻结回放 |

## 验收执行

- [x] span 二分实现并测试。
- [x] c8 两个截断现场完成重标。
- [x] citation integrity 不再 pending。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 输出截断如何恢复 | 保留同一 evidence 批，递归二分 spans | 输出大小主要随 annotation 数增长 | 直接提高上限仍会再次截断；预先按固定 span 数拆会增加常态调用 | 调用数与 trace | 截断回放 | 已确认 |

## 证据包

- 原 c8 run：`run_3b43769e813a437c84a766db4b1bd98f`
- 截断 attempts：`attempt_037d95f5efc14423a511ad6a379e9482`、`attempt_fa08f59eb5374d668f83b05c1d3df1ce`
- 原 judgment：`evalrev_ee043ddd3d2e4883b8d24a53e51cfc6e`
- 重标输出：`backend/.runtime/c8-reannotate.json`
- 两个 attempt 各重复 3 次，共 6 records：
  - annotation complete：6/6
  - `citation_annotation_output_truncated`：0
  - annotation 总数：636
  - reason 覆盖率：100%
  - 每轮 annotation 模型调用最多 3 次
  - context／request／协议错误：0
- 两个截断现场重新标注后均不再产生 citation integrity pending。
- 与 c8 新判据的 R/X 结果组合后，c8 投影为 9 pass、1 fail、0 pending，达到 K=9；唯一 fail 是待讨论的 R2。
- 重标后的 citation support 仍产生 soft pending：每轮 supported 53～69、unknown 26～47、unsupported 2～13；该结果用于后续引用质量分析，不阻塞 integrity 或 case K。
- 自动化测试：
  - 多 span 截断：4 spans 首次截断，随后 2+2 成功；3 次调用、3 份原始输出、4 个 annotation 按答案顺序合并。
  - 单 span 截断：两次失败后保持 `citation_annotation_output_truncated`，不伪造成功。
  - 冻结 evidence 短引用可恢复为 trace 中的真实 block_id。
  - `corepack pnpm quality:quick`：通过。
  - 完整后端 pytest：通过。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么二分对象应是 span，而不是继续缩小 evidence 批次？ | 输出长度的主要影响因素是 span／annotation 数量，缩小 evidence 主要影响输入且会增加调用。 | 通过 | 继续核对 R2 |
| R2 | 怎样证明二分没有通过漏掉 span 伪造成功？ | 校验二分前后 span_id 集合一致、annotation 属于对应子组、最终按答案顺序排列，并确认单 span 截断仍为 pending。 | 通过 | 继续核对 R3 |
| R3 | 什么情况下仍必须保持 pending？ | 二分到单 span 后仍输出超限、协议或 quote 非法、上下文／请求预算不足，或基础设施失败时，都保持 pending。 | 通过 | 反向解释完成 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 已知风险与后续

- 接受的风险：二分可能增加少量模型调用，但只在输出截断时发生。
- 后续：根据真实 long-answer 分布决定是否再加固定 span 上限。

## 协作复盘

- AI 明显加速的环节：把截断恢复实现为递归 span 二分，并补齐真实 trace 的短引用到真实 block_id 映射。
- 负责人失去系统模型的环节：未出现；负责人及时质疑“是否真的按 span 二分”，促成机制复核。
- 真正发现问题的控制点：两个 c8 长答案 3×2 次冻结重标全部 complete，且没有输出截断。
- 只有仪式感、没有产生判断价值的流程：无。
- 下一次准备调整的一到两项：为长期批量实验增加进度输出，避免只能等待整轮结束。

## 停止与推翻条件

- 如果二分调用频繁触碰请求总预算，再评估预先按 span token 上限分批。
