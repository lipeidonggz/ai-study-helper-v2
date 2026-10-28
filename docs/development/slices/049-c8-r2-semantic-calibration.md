# 049 · c8 R2 整体图景范围校准

- 状态：已完成
- 开始日期：2026-10-28
- 关联设计：[03 用例与金标准](../../design/03-cases-and-gold-standard.md)
- 前置切片：[047 c8 综合框架与名称归属边界](./047-c8-synthesis-attribution-boundary.md)、[048 引用标注输出截断后的 span 二分](./048-citation-annotation-span-split.md)
- 人工验收：沛东确认“agent 互审”是通用反馈回路的具体实现，不作为 c8 整体图景的独立必选项

## 控制契约

### 背景与失败场景

- 触发问题：旧 c8 R2 要求 9 个概念全部出现，其中把 OpenAI 文章中的一个具体实践“agent-to-agent 评审”提升为整体图景必选项。
- 不处理时的用户损失：一个已经正确概括两篇文章共同主线、并说明环境、脚手架和反馈回路的答案，会因省略一个具体实现细节被判失败；评测把记忆细节误当成综合能力。
- 现有流程为什么不足：同一模型在用户明确询问“如何节省人的注意力”时，能准确回答“把评审负担尽量转移给 Agent”，说明它并非不知道该事实；c8 失败来自判分粒度与题面目标不一致。
- 本切片负责什么：把 c8 R2 收敛为 7 个整体图景核心概念，保留通用反馈回路，移除 agent 互审独立必选项，并明确保留 Anthropic 爆炸半径增长关系。
- 本切片不负责什么：不修改问题、R1、R3、X2、N/K、Agent、Harness、citation annotation 或原答案。
- 停止或推翻条件：如果 7 项仍然把具体实现当作整体图景核心，应继续按题面粒度收敛，而不是通过修改 Agent 迎合判据。

### 用户可观察目标

c8 使用新的 R2 revision 时，凡正确覆盖两篇文章共同主线所需的核心概念、但没有单独展开 agent 互审的答案，可以正常通过；原 10 个冻结答案均有可核验的新 R2 判定。

### 核心不变量

1. 使用原 run `run_3b43769e813a437c84a766db4b1bd98f` 的同一批 10 个答案，不重跑 Agent。
2. 只修改 c8 R2；R1、R3、X2 和问题不变。
3. 旧 case revision、旧 judgment 和中间校准 revision 保持可追溯。
4. 新 case revision 激活后，所有包含 c8 的 active suite 同步到该 revision。
5. pending 不计 pass；引用完整性仍由 Harness 确定性检查。

### 明确禁止

- 不得把“agent 互审”重新作为 c8 整体图景的独立必选项。
- 不得为 c8 修改 Harness 或增加 case-specific 分支。
- 不得重跑 Agent、替换答案或用其他 attempt 的证据补齐。
- 不得删除旧 revision 或旧 judgment。
- 不得把两个 annotation pending 手工当作 pass。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 核心概念确实缺少 | R2 fail 和缺失项 | 答案、旧/新 judgment | 是 |
| citation annotation 截断 | attempt pending | 原答案与完整重标结果 | 是，048 已修复 |
| judge 输出非法 | criterion pending | invocation 原始输出与错误码 | 是 |
| suite 含过期 case revision | 拒绝发布 suite | 原 active suite 和 draft | 是 |

### 决策预算

- 实际 B 级决策数量：2。
- 是否存在 C 级决策：否。
- 人工确认：沛东已确认移除 agent 互审独立必选项，并保留爆炸半径增长关系。

## 目标

1. 将 c8 R2 从“9 个概念全列”收敛为“7 个整体图景核心概念”。
2. 用原冻结答案验证新判据，不重跑 Agent。
3. 激活新 case revision，并同步所有包含 c8 的 active suite。

## 输入与输出

- 原 run：`run_3b43769e813a437c84a766db4b1bd98f`
- 原 case revision：`cr_c8_001_ffa884207340`
- 新 case revision：`cr_c8_001_75625c0fd27b`
- 新 R2 revision：`ccr_c8_001_r2_79567dca2b`
- 新 judgment revision：`evalrev_105e2bb124aa48e39ecdcc8d9ec02849`
- 本地证据：`backend/.runtime/c8-r2-rejudge-049-final.json`

## 不包含

- 不修改全局 Agent 提示词。
- 不修改通用 Judge 提示词。
- 不修改 c8 的问题或 N/K。
- 不修改 citation annotation 与 citation support 规则。

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 |
| --- | --- | --- | --- | --- | --- |
| A1 | 整体图景粒度 | agent 互审不再单独阻塞 c8 | 未写 agent-to-agent 的答案可通过其他 7 项覆盖 | 必须 | 冻结重判 |
| A2 | 核心关系保留 | 爆炸半径增长关系仍被检查 | 答案仍需说明能力/访问范围与爆炸半径的关系 | 必须 | 冻结重判 |
| A3 | 同现场 | 使用原 run 的 10 个答案 | attempt 集合完全一致，无新 Agent 运行 | 必须 | SQL 完整性校验 |
| A4 | 判定完整 | 每个 attempt 恰好一条 R2 | 10 个 attempt，10 条 R2，无重复 | 必须 | SQL 完整性校验 |
| A5 | 可追溯 | 旧 revision 保留，中间错误校准退休 | 旧数据和中间 revision 均可读取 | 必须 | SQL 完整性校验 |
| A6 | suite 一致 | 所有 active c8 suite 指向新 revision | 无 inactive c8 case | 必须 | suite 状态校验 |

## 验收执行

- [x] 7 项核心概念 R2 已发布。
- [x] 原 10 个冻结答案已各重判一次。
- [x] 每个 attempt 恰好一条新 R2，10/10 satisfied。
- [x] 新 case revision 已激活。
- [x] `design03` 与“除截断用例外所有”两个 active suite 已同步。
- [x] 旧 revision 与中间校准 revision 保留并标记退休。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | agent 互审是否作为 c8 独立必选项 | 不单独要求；保留为通用反馈回路的一个可选实现 | 题面要求整体图景；R1/R3 已覆盖反馈回路和跨源归并；模型在直接询问该机制时能准确回答 | 若保留，会把一个具体实现提升为整体图景门槛，并与其他未列出的实现形成不对称 | R2 从 9 项收敛为 7 项 | 原 10 个冻结答案 R2 10/10 pass | 已确认 |
| D2 | B | 爆炸半径增长关系是否保留 | 保留，并要求说明能力或访问范围扩大导致潜在爆炸半径增长 | Anthropic 文章开篇直接提出该关系，并由此推出 containment | 若删除，会削弱该文对整体图景的核心贡献 | R2 继续检查核心因果方向 | `attempt_11e...` 在该关系完整后通过 | 已确认 |
| D3 | B | 是否修改全局 Agent 提示词以迎合 R2 | 不修改，撤回 `chat-v14` 实验 | agent 互审不是 c8 整体图景必选项；模型已能在直接问题中回答该机制 | 修改全局提示词会增加无关约束，并把金标准问题误归因为 Agent 能力问题 | Agent 基线恢复为 `chat-v13` | 代码 diff 已撤回 | 已确认 |

## 证据包

### 冻结重判

- R2 新旧对比：旧 revision 为 9 satisfied／1 unsatisfied；新 revision 为 10 satisfied。
- 唯一变化：`attempt_11e766c4d39041faa659374342478e9f` 从 fail 改为 pass。
- R1、R3、X2：10/10 pass，无 X 违规。
- 当前 case 聚合：8 pass、0 fail、2 pending；两个 pending 均为 048 已修复的 citation annotation 截断现场。
- 与 048 的冻结重标结果组合：两个 `citation_integrity` 均可恢复为 pass，因此投影为 10 pass、0 fail、0 pending。

### 完整性校验

- 原 run：10 个 attempt。
- 新 R2：10 条，每个 attempt 恰好一条。
- 旧 judgment revision `evalrev_703e013079be46e6881173f7c5881ddf` 保留可读。
- 新 case revision `cr_c8_001_75625c0fd27b` 状态为 active。
- 中间严格 revision `cr_c8_001_2189adcd31dc`、`cr_c8_001_7c3d909b2b3a` 已退休，未覆盖。
- active suites：`sr_design03_fd408efbebc6`、`sr_除截断用例外所有_09d8893f4a1a`，均含新 c8 revision 且无 inactive case。

### chat-v14 失败探针

- `chat-v14` 增加通用跨来源综合规则后，实际重跑 c8 N=10：Agent 10/10 成功，但严格 R2 为 2 pass、7 fail、1 pending，未达到 K=9。
- trace 显示 10/10 已取得 agent 互审和爆炸半径原文，工具结果全部 admitted。
- 用户直接询问“如何有效使用稀缺的人类注意力”时，Agent 明确回答“把评审负担尽量转移给 Agent”。
- 结论：agent 互审不是 Agent 能力缺口，也不应作为 c8 整体图景的独立必过项；`chat-v14` 实验已撤回。

### 自动化证据

- `pytest backend/tests -q --basetemp backend/.runtime/pytest-049-full`：通过。
- `corepack pnpm quality:quick`：通过。
- `git diff --check`：通过。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么 agent 互审不应作为 c8 的独立必选项？ | 它是通用反馈回路的具体实现；在直接询问注意力治理时模型能回答，但在整体图景中没有必要单独展开 | 通过 | 保留通用反馈回路 |
| R2 | 为什么爆炸半径增长关系仍应保留？ | 它是 Anthropic 文章开篇的核心命题，也是环境层 containment 必要性的直接依据 | 通过 | 保留方向性关系 |
| R3 | 为什么这次不应继续修改 Agent 提示词？ | 问题来自判据粒度而非 Agent 知识缺失，继续加全局规则会把测试要求污染到所有问题 | 通过 | 撤回 chat-v14 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 协作复盘

- AI 明显加速的环节：从旧判定差异、原文位置和 trace 证据中定位“判据粒度过细”，并完成同现场重判。
- 负责人失去系统模型的环节：没有；负责人指出需要回答“就 c8 而言是否重要”，把讨论从文章重要性拉回用例目标。
- 真正发现问题的控制点：用户直接询问 agent 互审时得到正确回答，证明问题不是 Agent 不知道。
- 只有仪式感、没有产生判断价值的流程：全局提示词试跑本身没有解决问题，但它形成了明确反例，支持撤回该方向。
- 下一次准备调整的一到两项：新增用例或判定项前，先问“回答这个具体问题时它是否不可替代”，并检查是否与同题其他必选项粒度一致。

## 已知风险与后续

- 接受的风险：R2 仍要求 7 项核心概念，其他合理但不同层面的概括可能被判 fail；后续出现反例时按同一“题面必要性”标准复核。
- 未完成事项：当前 2 个 annotation pending 来自旧 run；048 已用冻结重标证明可恢复，尚未把组合结果持久化为新的最终 judgment revision。
- 后续优化：如产品面板需要直接显示 10/10，可在独立切片中把 048 重标注与最终判定正式组合；这不是本次金标准修订的一部分。

## 停止与推翻条件

- 如果出现正确答案仅因没有覆盖某个具体实现而被 fail，应检查该实现是否被不恰当地列为核心概念。
- 如果 7 项仍导致大量表面正确但关键关系错误的答案 pass，应提高语义边界，而不是增加实现细节数量。
