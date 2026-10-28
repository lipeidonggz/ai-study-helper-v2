# 045 · 引用语义支持改为软质量门禁

- 状态：已完成
- 开始日期：2026-10-28
- 关联设计：[08 评测台](../../design/08-evaluation-console-design.md)
- 前置切片：[025 引用支持分批](./025-evaluation-citation-support-batching.md)、[029 引用评测可靠性](./029-citation-evaluation-reliability.md)、[037 引用语义支持候选与人工复核门禁](./037-citation-support-review-gate.md)
- 关联 ADR：[0018 引用语义支持阶段性改为软质量门禁](../../decisions/0018-citation-support-as-soft-quality-gate.md)

## 控制契约

### 背景与失败场景

- 触发问题：c6 的最新 v7 run 中，R／X 为 48 pass、2 fail，引用完整性为 10/10 pass；citation support 却因 7 个 unknown 和 3 个 unsupported 候选留下 6 个 pending attempt，阻塞 case 达到 K=9。
- 不处理时的用户损失：评测结果无法区分“答案不满足 criterion”和“语义判官无法确认少数综合句”，团队被迫优先调整不稳定判官，而不是处理 R1 内容缺失和明确的出口控制方向错误。
- 现有流程为什么不足：ADR 0013 把全部 claim 的 supported 当作 attempt pass 条件；只要一个 claim 语义判断有争议，attempt 和 case 就无法完成 K/N 判定。
- 本切片负责什么：计算并展示“已标注引用的语义支持率”；把 citation support 从 attempt/case 的阻塞集合移出；低于阈值时保留可见的软质量警告。
- 本切片不负责什么：不修改 citation judge 提示词；不降低 citation integrity；不判断漏标 claim；不修改 R／X criterion；不把语义支持异常从详情中删除。
- 什么情况下应停止或撤掉本切片引入的机制：用户无法从结果矩阵识别低支持率 attempt，或软门禁导致确定性引用完整性问题被隐藏。

### 用户可观察目标

评测结果矩阵为每个有效 attempt 显示引用支持率和质量状态。达到阈值的 attempt 标记为优；低于阈值时标记为待观察，但不改变 attempt/case 的 pass、fail、pending 或 N/K 结论。

### 核心不变量

1. `citation_integrity` 仍是不可人工覆盖的确定性硬红线；citation support 不再改变 attempt 和 case 的通过结论。
2. 支持率只使用已标注 occurrence：`supported / occurrence_count`；unknown 计入分母且不计入分子。
3. 至少 10 个 occurrence 才启用 90% 阈值；少于 10 个显示“样本不足”，不做优／待观察判断。
4. 原始 checker 结果、claim verdict、人工复核、trace 和 invocation 全部保留，不从 UI 或数据库中删除。
5. 没有 citation support occurrence 时显示“不适用”，不得显示为 100% 或优。

### 错误代价与非对称门禁

| 错误类型 | 具体例子 | 用户影响 | 处理分类 | 指标与阈值 |
| --- | --- | --- | --- | --- |
| 引用指向不存在的 block | citation integrity 出现 missing block | 来源不可回溯、形成伪引用 | 阻断 | integrity 必须 10/10 pass |
| 少量语义支持不确定 | 30 个 occurrence 中 2 个 unknown | 不影响 K 判定，但降低引用质量 | 软警告 | 支持率仍为 28/30=93.3% |
| 大部分引用语义不支持 | 10 个 occurrence 中 5 个 unsupported | 用户看到的引用整体不可信 | 软警告并保留复核入口 | 支持率低于 90% |
| 小样本引用全错 | 1 个 occurrence 为 unsupported | 不能仅凭比例判断稳定性 | 记录但不评级 | occurrence 少于 10 |
| 漏标整个 claim | annotator 没有产生 annotation | 当前指标无法发现覆盖缺口 | 本切片不处理 | 后续 coverage 专项 |

### 明确禁止

- 不得把 unknown 排除出支持率分母。
- 不得因为 citation support pass／fail／pending 改变 R／X 或 citation integrity 结论。
- 不得把没有 occurrence 的 attempt 显示为支持率 100%。
- 不得覆盖或删除原判官与人工复核记录。
- 不得修改 citation judge 提示词来迎合 90% 阈值。
- 不得把“已标注引用支持率”表述成全答案引用覆盖率。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 自动 unsupported | 引用支持率下降和待观察标记；attempt 仍按 R/X/integrity 聚合 | checker pending、全部 claim candidate、调用审计 | 是 |
| 人工确认 unsupported | 支持率下降；attempt/case 不因该项阻断 | append-only 人工复核和原判官结果 | 是 |
| unknown | 不进入分子，计入分母 | 全部 unknown claim 与理由 | 是 |
| occurrence 少于 10 | 样本不足 | 原始计数与 verdict | 是 |
| citation integrity fail | attempt 失败 | 缺失 block 与引用事实 | 修复后重跑 |

### 人工检查点

- 编码前需要确认的契约或风险：threshold、unknown 计入分母、最少 10 个 occurrence、support 完全退出阻塞。
- 实现中途需要查看的可运行路径：评测结果矩阵中的 attempt 支持率与软质量状态。
- 完成后需要人工执行的操作：用 v7 c6 的现有 revision 确认结果矩阵不再因 citation support pending 阻塞 K。

### 决策预算

- 预计 B 级决策数量：3。
- 是否存在 C 级决策：否。
- 超过预算时的拆分方式：先只实现聚合和矩阵展示，人工复核统计另开切片。

## 目标

1. 从现有 citation support payload 计算 attempt 级支持率，不重新调用模型。
2. 将 citation support 从 attempt/case 阻塞条件中移出。
3. 在结果矩阵显示 supported、unsupported、unknown、支持率和软质量状态。
4. 保持 citation integrity、R／X 和既有人工复核入口不变。

## 输入与输出

- 输入：现有 `citation_support` CheckResult 的 `claim_results`，以及可选的人工复核 payload。
- 输出：每个有效 attempt 的 citation support 质量对象；case 仍按 R／X／citation integrity 和 N/K 聚合。
- 软质量状态：
  - `good`：occurrence_count >= 10 且 support_rate >= 90%；
  - `review`：occurrence_count >= 10 且 support_rate < 90%；
  - `insufficient_sample`：0 < occurrence_count < 10；
  - `not_applicable`：没有 citation support claim。

## 不包含

- 不新增“应标未标”覆盖率检查。
- 不修改 `claim-citation-v7` 或 `citation-rubric-v11`。
- 不自动把 unsupported 候选改成 supported。
- 不改变 citation integrity 的硬红线。
- 不新增数据库 schema；质量指标由现有 payload 在聚合时计算。

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 |
| --- | --- | --- | --- | --- | --- |
| A1 | 支持率可见 | 10 个 occurrence 中 9 supported、1 unknown 显示 90% 和 good | 9/10 | 必须 | 集成 |
| A2 | 低支持率可见 | 10 个 occurrence 中 8 supported 显示 80% 和 review | 8/10 | 必须 | 集成 |
| A3 | 小样本不误判 | 3 个 occurrence 显示 insufficient_sample | 3/3 也为样本不足 | 必须 | 单元 |
| A4 | support 不阻塞 | pending/fail/unreviewed citation support 不改变 R/X 全部通过时的 attempt/case pass | 10/10 support candidate，K=1 | 必须 | 集成 |
| A5 | integrity 仍阻断 | citation integrity fail 的 attempt 仍 fail | integrity=1/10 | 必须 | 集成 |
| A6 | 人工结果可观察 | 人工确认 unsupported 后支持率下降但 attempt 仍 pass | 1/10 review | 必须 | 集成 |
| A7 | 旧现场可回放 | v7 c6 现有 revision 重新投影后显示各 attempt 支持率且不被 support pending 阻塞 | run_9219… | 必须 | 真实现场 |

### 验收冻结与变更

- 冻结时点：2026-10-28 编码前。
- 已确认的阻断反例：citation integrity fail 必须继续阻止 attempt pass。
- 验收变更记录：暂无。

## 验收执行

- [x] 用户可观察的主路径成立。
- [x] 核心不变量已被自动化测试或明确的人工验收覆盖。
- [x] 失败路径不会破坏旧状态或伪装成功。
- [x] 验收矩阵中的阻断项全部通过。
- [x] 未通过的已知反例已修复，或由负责人明确接受风险。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 软质量阈值如何定义 | 10 个以上 occurrence 时，support_rate >= 90% 为 good | 沛东要求暂时放宽为单 attempt 90% | 低于 10 不评级；unknown 计入分母 | 只影响展示，不改变 K | 9/10、8/10、3/3 测试 | 已确认 |
| D2 | B | support 是否还能阻塞 K | citation support 完全退出 attempt/case 阻塞 | 沛东明确“不做红线了” | 保留 R/X 与 citation integrity 为真实门禁 | 旧 revision 重新投影后可能从 pending 变为可判定 | pending/fail 不改变 pass | 已确认 |
| D3 | B | 人工确认 unsupported 如何生效 | 保留人工复核并更新支持率，但不改变 attempt/case 结果 | 保留审计价值和软质量目标 | 若继续阻断会重新形成红线 | 人工失败显示为 review 质量 | 人工复核测试 | 已确认 |

## 证据包

### 自动化证据

- 命令：`corepack pnpm quality:quick`。
- 结果：通过；包含注释检查、Ruff、Pyright、Vue 类型检查、前端构建、Rust fmt／check／clippy。
- 覆盖的不变量：前后端类型、静态质量和桌面构建未被新字段破坏。
- 命令：`.\backend\.venv\Scripts\python.exe -m pytest backend\tests\test_evaluation_aggregation.py backend\tests\test_evaluation_judgment.py -q --basetemp .pytest-045-backend-final`。
- 结果：42 passed。
- 覆盖的不变量：9/10 good、8/10 review、3/3 insufficient_sample、support pending/fail 不阻塞、integrity fail 仍阻塞、人工复核历史参与软质量计算。
- 命令：`.\backend\.venv\Scripts\python.exe -m pytest backend -q --basetemp .pytest-045-full-final`。
- 结果：完整后端测试通过。
- 覆盖的不变量：新增聚合字段和规则版本未破坏既有评测、基线、API 与引用链路。

### 运行证据

- 启动方式：读取已有评测数据库并调用 `EvaluationJudgmentService.get_revision`，不执行 Agent、不调用 judge。
- 用户操作序列：打开 v7 c6 revision `evalrev_a781b0c8827749189ab8607744928a34`。
- 预期结果：旧 revision 不再仅因 citation support pending 显示不完整；显示各 attempt 支持率；case 仅按 R/X/integrity 与 N/K 判定。
- 实际结果：revision 投影为 `complete`；聚合规则为 `023b-nk-v2-citation-soft`；10 个 attempt 的引用支持均评级 good，最低 91.7%、最高 100%；case 为 8 pass、2 fail、0 pending，仍因低于 K=9 而 fail。

### 失败探针

- 注入或复现的失败：历史上 `citation_support=pending` 会把 revision 标成 incomplete；v7 原 revision 的持久化状态仍为旧值。
- 系统保留的旧状态：CheckResult、claim verdict、答案、trace 与旧 revision 状态均不覆盖；读取时按当前聚合规则投影。
- 用户看到的错误：只显示软质量支持率和异常计数；R/X 与 citation integrity 仍按原失败语义显示。

### 已知反例账本

| 场景或反例 | 期望行为 | 实际结果／分布 | 是否阻断 | 处理结论 |
| --- | --- | --- | --- | --- |
| support pending 阻塞 case | 只显示软质量状态 | v7 从 3 pass／4 fail candidate／3 pending 变为 8 pass／2 fail／0 pending | 是 | 已修复 |
| unknown 不计分母 | unknown 计入分母但不计 supported | 9 supported + 1 unknown 为 90% good | 是 | 已验证 |
| 支持率高于 90% 但存在方向错误 | 保留 good 评级并显示异常计数，不自动阻断 | attempt_70f3 为 27/28=96.4%，含 1 unsupported | 否 | 阶段性接受 |

### 人工验收

- [x] 评测台结果矩阵和样本详情能看到支持率、计数与软质量状态；沛东确认检查通过。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 一个 has 9 supported、1 unknown 的 attempt 为什么可显示 good，但支持率不是 100%？ | unknown 本身不算支持，所以分子为 9、分母为 10；citation support 已改为质量属性，不改变 attempt 通过结论。 | 通过 | 继续核对 R2 |
| R2 | citation support 不再阻塞后，哪种引用错误仍然必须阻止 attempt 通过？ | 引用完整性问题仍必须阻断；必须由 Harness 确定性确认所引 block 存在且来自本次 attempt 的真实工具结果，不能人工覆盖。 | 通过 | 继续核对 R3 |
| R3 | 为什么 3 个 occurrence 全 supported 仍显示样本不足，而不是 good？ | 样本数少于 10 时不做百分比质量评级，避免小分母把偶然的 100% 当作稳定质量。 | 通过 | 反向解释完成 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 协作复盘

- AI 明显加速的环节：从现有 claim payload 推导软质量、补聚合测试、用 v7 revision 直接重投影，以及同步前后端展示。
- 负责人失去系统模型的环节：未出现阻塞；负责人先明确了“只做质量属性、不做红线”的目标，再确认 90% 与小样本边界。
- 真正发现问题的控制点：读取 v7 原 revision 后，case 从 support pending 中释放并只剩两条 R1 失败，直接证明软门禁解决了原来的阻塞。
- 只有仪式感、没有产生判断价值的流程：本轮无。
- 下一次准备调整的一到两项：为软质量展示增加前端组件级测试；把“应标未标”的 coverage 作为独立指标设计。

## 已知风险与后续

- 未验证项：支持率只覆盖已标注 claim，不覆盖应标未标。
- 接受的风险：少量错误或未知引用不会阻止 case 达到 K；例如 v7 中审计正确的出口控制方向错误所在 attempt 仍以 27/28=96.4% 评为 good。
- 后续待办或 ADR：增加 citation coverage 指标时单独建切片。

## 停止与推翻条件

- 如果软门禁被界面表达成整体通过率，导致 citation integrity fail 被忽略，立即撤掉该投影。
- 如果低于 90% 的支持率长期与用户可见引用错误严重不一致，重新评估 90% 和 10 个 occurrence 的阈值。
