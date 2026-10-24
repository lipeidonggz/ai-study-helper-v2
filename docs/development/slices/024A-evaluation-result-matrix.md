# 024A · 评测结果矩阵

- 状态：已完成
- 开始日期：2026-10-24
- 关联设计：[08 评测台](../../design/08-evaluation-console-design.md)
- 前置切片：[023B 生效判定绑定与 N/K 聚合](./023B-evaluation-aggregation.md)
- 人工验收：[024A 评测结果矩阵人工验收](../manual-tests/024A-evaluation-result-matrix.md)

## 控制契约

### 用户可观察目标

1. 用户可以从历史结果打开一次执行批次的结果矩阵，并按 run、问题类别、case、attempt 四层查看结果。
2. 每一层都能看到 pass／fail／pending／exploratory、X 红线、token、模型调用、工具调用和可计算的耗时。
3. 用户可以从 attempt 在原页面打开该样本的逐项判定或运行轨迹弹窗；判定弹窗展示冻结问题、每条 R／X 的完整金标准和判定理由；轨迹调用树在 `tool.call` 旁直接显示工具名与参数摘要，关闭后保持矩阵的展开状态与滚动位置。
4. 判定尚未开始时明确显示“尚未判定”，判定运行中显示当前 pending 分布，不提前生成正式通过结论。

### 核心不变量

1. 结果矩阵只从 ExecutionRun、Attempt、CaseAggregate、AttemptAggregate 和冻结用例 revision 重新计算，不创建第二套事实源。
2. case 级质量结论继续使用 023B 的 N/K、pending 与 X 红线规则，不在展示层重新解释或改算。
3. 成本与资源指标按 attempt 全量统计；质量样本数按 `outcome_class=product` 统计，两者不能混为一个分母。
4. 类别分组来自冻结 case revision 的 category；未知类别显式显示，不静默丢弃。
5. attempt 顺序来自 run 中按开始时间排序的稳定样本序号，不能用最终状态排序替代。

### 明确禁止

- 不在本切片设置基线、比较跨 run 结果或生成诊断结论；
- 不在前端缓存中形成独立评分；
- 不把 excluded／基础设施失败样本计入有效 N；
- 不用全局多数覆盖任一 X 红线；
- 不修改已有 Judgment、CheckResult 或 aggregate 事实。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 尚未启动判定 | 运行与 attempt 矩阵；“尚未判定”，不显示 pass／fail | ExecutionRun、Attempt 与 trace | 启动判定后恢复 |
| 判定运行中 | 当前聚合进度和 pending 项，结果标记为待定 | 已落盘 Judgment 与 partial aggregate | 继续判定后刷新 |
| 判定不完整 | pending 数量与原因可见 | 全部 Judgment、binding 与 checks | 人工复核或重判 |
| 某类别为空或未知 | 显示“未分类”或明确空状态 | 原 case revision 与 run | 无需恢复 |
| attempt 缺少 trace | 保留矩阵指标，trace 按钮禁用并显示缺失 | attempt 与 trace_status | 依赖原 trace，不伪造 |

### 人工检查点

- 编码前确认四层信息架构与下钻目标。
- 后端矩阵投影和 API 完成后查看一次真实 run 的四层数量和状态。
- 前端完成后从 attempt 分别打开判定与运行轨迹弹窗，并确认关闭后矩阵位置不变。
- 最后核对矩阵与 023B aggregate 在 pass／fail／pending、X 红线和 token 上逐项一致。

### 决策预算

- 预计 B 级决策：4 项。
- C 级决策：0 项。
- 超过预算时拆分：先交付只读后端投影与 API，再把前端矩阵拆为 024A2。

## 目标

把已有 attempt/case 聚合整理成可扫描、可下钻、可解释的四层结果矩阵，为后续基线和跨 run 比较提供稳定只读入口。

## 输入与输出

输入：

- 已启动判定的 ExecutionRun；
- RunView、CaseRevisionView、AttemptAggregateView、CaseAggregateView；
- 已有 attempt trace 与 Judgment。

输出：

- EvaluationRevision 中的只读结果矩阵投影；
- 评测历史的“结果矩阵”页签；
- attempt 到判定结果／trace 的下钻入口。

## 不包含

- Baseline 对象、基线选择与跨 run 比较；
- 自动诊断建议和人工诊断结论；
- 报告导出、图表和长期趋势；
- 修改聚合规则、判定规则或 Agent 行为。

## 验收条件

- [x] 后端矩阵覆盖 run、类别、case、attempt 四层，且保留稳定排序。
- [x] run／类别／case 质量计数与 023B aggregate 完全一致。
- [x] token、模型调用、工具调用和耗时来自 attempts，不混入质量状态。
- [x] 判定前、判定中和判定不完整都有明确展示。
- [x] attempt 可在结果矩阵内打开逐项判定或运行轨迹弹窗，关闭后保持原有展开状态与滚动位置。
- [x] 判定弹窗展示冻结问题和每条 R／X 的完整金标准。
- [x] 判定或轨迹弹窗打开时，主窗口不会响应背景滚动。
- [x] 轨迹中的每个 `tool.call` 显示工具名称与参数摘要；metadata 模式明确显示参数未记录。
- [x] 前端类型检查、构建、后端定向测试和完整质量门通过。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 结果矩阵放在哪个 API 边界 | 作为 EvaluationRevision 的只读派生字段随现有响应返回 | 08 要求报告从 run／judgment／aggregate 重建，不另建事实源 | 新 API 增加前端请求与状态同步；只在浏览器计算会让 API 与其他消费者口径漂移 | 不新增写模型，历史页一次请求获得判定与矩阵 | schema 序列化、API 响应和 aggregate 对照测试 | 已确认：负责人完成界面验收 |
| D2 | B | 状态与成本指标口径 | 质量状态取 case aggregate；token／调用／耗时取 attempts 全量；有效样本只计 product | 08 要求红线、状态、延迟、token、工具独立列；023B 已冻结样本资格 | 全部使用 product 会隐藏基础设施成本；全部使用 attempts 会污染 N/K | 页面明确区分质量与运行成本 | 混合 product／infrastructure 样本测试 | 已确认：反向解释 R1 通过 |
| D3 | B | 四层矩阵的交互方式 | 历史页增加“结果矩阵”，类别与 case 使用嵌套展开；attempt 的判定与 trace 均使用上下文内弹窗，判定弹窗展示问题和完整 criterion 金标准，打开时锁定主窗口滚动 | 08 要求 attempt／case／类别／run 四层与诊断入口 | 切换 tab 会中断当前矩阵分析位置；不展示问题与金标准会让判定理由失去上下文；滚动穿透会破坏背景分析位置 | 用户可在同一上下文逐层定位问题，关闭弹窗后继续原分析 | 前端构建与真实界面人工验收 | 已确认：负责人明确要求弹窗、问题、金标准和滚动隔离 |
| D4 | B | 判定未完成时 matrix 语义 | 已启动判定即显示当前矩阵；pending 显式呈现，状态保持待定，未判定则显示空状态 | 08 要求 pending 不伪装 pass／fail | 等 complete 才显示会隐藏进行状态；提前 pass 会制造虚假结论 | 判定中和不完整 run 仍可诊断 | pending／running API 与 UI 测试 | 已确认：反向解释 R2 通过 |

等级：

- A：AI 可自主执行，交付时列入审计摘要；
- B：完成后集中人工确认；
- C：执行前必须停止并获得人工决定。

## 证据包

### 自动化证据

- 命令：`.\.venv\Scripts\python.exe -m pytest tests\test_evaluation_aggregation.py tests\test_evaluation_judgment.py tests\test_evaluation_api.py -q --basetemp=..\.pytest-tmp-024a`
- 结果：20 passed。
- 覆盖的不变量：类别和 case 顺序、N/K 与 023B aggregate 一致、X 红线、attempt 稳定序号、成本覆盖全部 attempt、有效 N 排除基础设施失败。
- 命令：`corepack pnpm quality:quick`
- 结果：通过；包含 frontend typecheck、Vite build、Rust fmt／check／clippy。
- 命令：完整后端测试 `pytest backend\tests -q --basetemp=.pytest-tmp-024a-full`
- 结果：235 passed。

### 运行证据

- 启动方式：开发评测构建，打开历史结果并选择已判定 run。
- 用户操作序列：进入“历史结果”→打开已判定 run→选择“结果矩阵”→展开类别、case 与 attempt。
- 预期结果：四层数量与状态一致；attempt 的“查看判定”和“运行轨迹”进入对应样本现场。
- 实际结果：负责人确认结果矩阵、判定弹窗、完整金标准、背景滚动锁定和工具调用参数摘要符合预期。

### 失败探针

- 注入或复现的失败：同一 case 混入 2 个 product 样本和 1 个 infrastructure attempt；另一 case 含 pending Judgment。
- 系统保留的旧状态：基础设施 attempt 的成本进入 run 总计，但 `valid_samples` 仍为 2；pending case 保持 pending，不转成 pass。
- 用户看到的错误：矩阵显示基础设施失败数量、excluded attempt 和 pending 原因，不伪装成正式通过。

### 人工验收

- [x] 负责人已完成真实界面验收。

## 反向解释

实质性切片必须准备 3～5 道行为问题，由负责人逐题独立回答。AI 每次提问必须说明当前题号和剩余题数。

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么 token、模型调用和工具调用统计包含基础设施失败 attempt，而有效样本数不包含？ | token 等是事实层面的统计量；有效样本数用于形成产品能力结论，基础设施失败不能产出产品评测结论。 | 通过 | 保持成本分母与质量分母分离。 |
| R2 | 当 case 还有 pending 判定时，矩阵应显示什么结果，为什么不能先显示 pass？ | 应显示 pending；不能基于待定的原子数据生成明确 pass 或 fail 评测结论。 | 通过 | 保持 fail > pending > pass 的确定性与缺口优先级。 |
| R3 | 为什么结果矩阵必须从 ExecutionRun、Attempt 和 aggregate 重新计算，而不能单独保存一份分数表？ | 结果矩阵是事实的派生投影，不是新事实源；独立保存会破坏事实一致性并产生同步漂移。当前重算成本足够低，不需要用一致性换性能。 | 通过 | 保持单一事实源和按需重算。 |

全部通过后写入以下门禁标记，才能把切片标记为“已完成”并提交业务代码：

```text
<!-- reverse-explanation-gate: passed 3/3 -->
```

## 协作复盘

- AI 明显加速的环节：后端投影、前端四层矩阵、弹窗、滚动隔离、定向测试和类型检查可以围绕现有事实源快速闭环。
- 负责人失去系统模型的环节：首版下钻会切换页签，实际分析时丢失矩阵位置；负责人据此要求改为上下文内弹窗。
- 真正发现问题的控制点：反向解释确认成本与质量分母不能混用；定向测试还暴露出“1 个样本仍应按 N=10/K=9 显示 pending”。
- 只有仪式感、没有产生判断价值的流程：本轮没有保留明显仪式性步骤。
- 下一次准备调整的一到两项：矩阵字段密度继续由真实失败样本驱动；基线比较复用同一事实投影，不另存分数。

### 本次试行的流程规则

| 规则／假设 | 来源 | 想防住的失败 | 使用成本 | 实际效果 | 下次决定 |
| --- | --- | --- | --- | --- | --- |
| 展示层不能重新解释 N/K | 023B | 判官通过被前端误显示为 case 通过 | 需要后端投影和测试 | 已由 aggregate 对照测试固定 | 继续 |
| 成本分母与质量分母分离 | 08／023A | 基础设施失败污染有效 N 或成本被隐藏 | 字段更多、说明更长 | 测试已覆盖混合样本 | 继续 |

## 已知风险与后续

- 未验证项：尚未在完整 18 用例正式基线上观察全量字段密度。
- 接受的风险：当前矩阵一次返回 run 内全部 case／attempt；在 18×N 规模下可能较大，首版不做分页。
- 后续待办或 ADR：024B 基线与跨 run 比较。
