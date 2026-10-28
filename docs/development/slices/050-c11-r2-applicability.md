# 050 · c11 R2 适用前提校准

- 状态：已完成
- 开始日期：2026-10-28
- 关联设计：[03 用例与金标准](../../design/03-cases-and-gold-standard.md)
- 前置切片：[049 c8 R2 整体图景范围校准](./049-c8-r2-semantic-calibration.md)
- 人工验收：沛东确认修正 c11 R2 中过窄的 Anthropic 适用前提

## 控制契约

### 背景与失败场景

- 触发问题：c11 新数据唯一 fail 的答案指出 Anthropic 同时覆盖能评估风险的开发者和不能判断命令的非技术用户，并使用“隔离强度匹配用户监督能力”概括产品差异；旧 R2 却要求 Anthropic“面向通用产品和非技术用户”，把官方文章中的多种用户前提压成单一前提。
- 不处理时的用户损失：更准确的答案会因没有迎合过窄表述而被误判失败，评测会把金标准缺陷归因成 Agent 能力问题。
- 本切片负责什么：修正 c11 R2 的 Anthropic 前提；对现有 c11 run 的 10 个冻结答案重判；激活新 revision 并同步所有包含 c11 的 active suite。
- 本切片不负责什么：不修改问题、R1、R3、X1、X2、Agent、Harness、答案或 N/K；不重跑 Agent。
- 停止或推翻条件：如果答案没有说明任何用户能力差异、审批疲劳或隔离强度关系，仍应判 R2 fail。

### 用户可观察目标

c11-001 对 Anthropic 的适用范围按原文判断为“受众监督能力不同的产品”，而不是单一的非技术用户场景；原 10 个冻结答案在新 R2 下重新判定。

### 核心不变量

1. 使用原 run `run_cf2f32388c8e4366ad743541249286c7` 的同一批 10 个答案。
2. 只修改 c11 R2，其他 criterion 与问题保持不变。
3. 旧 case/judgment 保留；新 revision 激活后同步所有 active suite。
4. 不重跑 Agent，不修改 citation annotation。

### 明确禁止

- 不得把 Anthropic 的适用范围简化为只有非技术用户。
- 不得删除“非技术用户需要环境层硬边界”的要求。
- 不得删除“开发者 HITL 仍受审批疲劳限制”的要求。
- 不得因修正措辞而接受把两篇文章说成事实层对立的答案。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| Anthropic 前提仍被说错 | R2 fail | 答案、旧/新判定 | 是 |
| OpenAI 前提缺失 | R2 fail | 答案、旧/新判定 | 是 |
| judge 输出非法 | criterion pending | invocation 与错误码 | 是 |
| suite 仍引用旧 revision | 拒绝发布 suite | 原 suite 和 draft | 是 |

### 决策预算

- 实际 B 级决策：1。
- 是否存在 C 级决策：否。

## 目标

1. 修正 c11 R2 的 Anthropic 适用前提。
2. 对原 c11 新数据重判，确认 false negative 消失。
3. 激活新 revision 并同步 active suite。

## 输入与输出

- 原 run：`run_cf2f32388c8e4366ad743541249286c7`
- 原 case revision：`cr_c11_001_minimal_judge_v2`
- 新 case revision：`cr_c11_001_4f89ebe124da`
- 新 R2 revision：`ccr_c11_001_r2_92dedd6661`
- 新 judgment revision：`evalrev_e187ee90c93a4c4bbeff1ee9a098bd18`
- 本地证据：`backend/.runtime/c11-rejudge-final.json`

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 |
| --- | --- | --- | --- | --- | --- |
| A1 | 修正 false negative | 区分开发者与非技术用户监督能力的答案可行 | `attempt_5fba251e...` 改为 R2 pass | 必须 | 冻结重判 |
| A2 | 保留语义边界 | 完全不说明用户能力差异的答案仍应 fail | 原 R2 的负例语义仍保留 | 必须 | 文档审查 |
| A3 | 同现场 | 使用原 10 个 c11 答案 | attempt 集合不变，不重跑 Agent | 必须 | SQL 完整性校验 |
| A4 | suite 一致 | 所有 active c11 suite 指向新 revision | 无 inactive c11 case | 必须 | suite 状态校验 |

## 验收执行

- [x] 新 c11 R2 reviewed revision 已发布。
- [x] 原 10 个答案已重判。
- [x] 新判定 revision 为 40/40 pass，case 为 10/10 pass。
- [x] 新 case revision 已激活。
- [x] 两个 active suite 已同步。
- [x] 旧 case revision 保留为 retired。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | Anthropic 的适用前提怎么写 | 面向监督能力不同的产品用户；非技术用户需绝对环境边界，开发者 HITL 仍受审批疲劳限制，隔离强度匹配监督能力 | Anthropic 原文同时覆盖 claude.ai、Claude Code 和 Claude Cowork，并明确提出用户能力差异 | 若只写非技术用户，会误杀更准确的答案；若只写开发者，则丢失安全主线 | R2 前提与原文及题目一致 | 原失败答案及全部 10 条重判通过 | 已确认 |

## 证据包

### 冻结重判

- 旧判定：39 pass、1 fail、0 pending。
- 新判定：40 pass、0 fail、0 pending。
- Case：10/10 pass，达到 K=9。
- X 违规：0。
- citation integrity：10/10 pass。
- citation support：保持软质量属性，无 unsupported。

### 完整性校验

- 原 run：10 个 attempt。
- 每个 attempt 恰有 R1、R2、R3、X1、X2 五项判定。
- 新 case revision `cr_c11_001_4f89ebe124da` 为 active。
- 旧 case revision `cr_c11_001_minimal_judge_v2` 保留并退休。
- 两个 active suite 均为新 revision，且无 inactive case。

### 自动化证据

- 重判脚本：`backend/.runtime/rejudge_case.py`
- 激活与 suite 同步：`backend/.runtime/activate_case_revision.py`
- 最终结果：`backend/.runtime/c11-rejudge-final.json`

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么不能再要求 Anthropic 只面向非技术用户？ | 原文同时覆盖通用产品、开发者和非技术用户，关键是隔离强度匹配监督能力 | 通过 | 改为多用户前提 |
| R2 | 为什么开发者场景仍不能删掉审批疲劳限制？ | Claude Code 的 HITL 即使对开发者可行，也已被证明会因高频审批退化 | 通过 | 保留审批疲劳与环境兜底 |
| R3 | 为什么只重判、不重跑 Agent？ | 运行结果没问题，问题在 criterion 前提，冻结重判才能控制变量 | 通过 | 不重跑 Agent |

<!-- reverse-explanation-gate: passed 3/3 -->

## 协作复盘

- AI 明显加速的环节：从唯一失败答案与 Anthropic 原文中识别出 R2 把多种用户前提压成单一前提。
- 负责人失去系统模型的环节：未出现；负责人明确要求先修金标准。
- 真正发现问题的控制点：c11 新数据中出现一条比旧 R2 更忠实于原文的答案，验证了判据而非 Agent 有误。
- 只有仪式感、没有产生判断价值的流程：无。

## 已知风险与后续

- 接受的风险：R2 仍要求同时覆盖非技术用户与开发者场景，可能对只讨论单一产品的答案偏严；后续出现新反例时继续按原文范围复核。
- 后续：重新运行正式 suite 时使用新的 active case revision。
