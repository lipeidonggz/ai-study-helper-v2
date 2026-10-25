# 028 · 拆开边界违规、跨项检查与引用切分版本

- 状态：已完成
- 开始日期：2026-10-25
- 关联设计：[03 用例与金标准](../../design/03-cases-and-gold-standard.md)、[08 评测台](../../design/08-evaluation-console-design.md)、[06 检索侧与 Agent](../../design/06-retrieval-pure-agent.md)
- 前置切片：[023B 聚合](./023B-evaluation-aggregation.md)、[024A 结果矩阵](./024A-evaluation-result-matrix.md)、[027 引用标注](./027-claim-aware-citation-annotation.md)
- 人工验收：待重启应用后复跑真实 N=1

## 控制契约

### 用户可观察目标

1. 结果矩阵分别显示边界 X 违规、跨项检查失败和两者合并后的红线失败，不再把 `citation_support=fail` 显示成 X 违规。
2. Citation annotation pending 不会阻塞只依赖 trace manifest 的 checker；例如截断事实仍能根据 `answer_completeness` 独立判定。
3. Citation artifact 明确记录 claim 切分版本；修复切分规则后，新旧 run 不再共享含义不明的版本。

### 核心不变量

1. 正式准出规则不变：X 违规或跨项检查失败仍使 attempt／case fail。
2. `x_violation_count` 只统计 X criterion fail；`check_failure_count` 只统计跨项 CheckResult fail。
3. `redline_violation_count` 仍是上述两类样本的并集，只用于正式红线状态，不用于解释“X 违规数”。
4. 截断 checker 只读取 trace manifest 的 generation state，不读取 citation artifact。
5. 旧 citation artifact 缺少版本时读取为 `legacy-unknown`；新 artifact 写入当前 `claim-spans-v2`。

### 明确禁止

- 不把跨项 citation failure 统计成 X criterion violation。
- 不因 annotation pending 把 `truncation_declared` 判成 pending。
- 不把旧 artifact 默认猜成当前切分版本。
- 不修改历史判定、历史 revision 或正式红线优先级。

### 失败语义

| 场景 | 期望行为 |
| --- | --- |
| X criterion fail，无跨项失败 | 只增加 `x_violation_count` |
| `citation_support` fail，无 X fail | 只增加 `check_failure_count` |
| X fail 与跨项失败同时存在 | 两者分别计数，并集仍为 1 个红线失败样本 |
| Citation annotation pending | citation check pending，但 truncation checker 正常执行 |
| 旧 citation artifact 无 `span_version` | 读取为 `legacy-unknown`，保持可读 |
| 新 citation artifact | 写入 `claim-spans-v2` |

### 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 红线统计如何拆分 | 保留并集 `redline_violation_count`，新增 `x_violation_count` 与 `check_failure_count` | 03／08 要求红线不可被平均，同时要求诊断可解释 | 只改前端标签会继续保留模糊 API；删除并集字段会改变准出语义与历史比较 | API、矩阵和比较视图新增细分字段 | 混合 X fail、check fail、两者同时存在 | 已确认 |
| D2 | A | checker 是否依赖 citation artifact | 拆出 generation checker 输入，截断检查只读 trace manifest | 确定性 checker 不应被无关跨项检查阻塞 | 整体失败会让已知事实 pending；忽略失败继续判又可能误用缺失事实 | cf-003 截断事实可独立判定 | pending citation + partial answer 服务级测试 | 已执行（A） |
| D3 | A | claim 切分版本放在哪里 | 写入 CitationAnnotationArtifact，旧 artifact 缺失时默认 `legacy-unknown` | 运行版本必须可追溯 | 改 prompt 版本会混淆提示词与切分规则；不记录则不可比较 | trace artifact、span attributes 和评测诊断新增版本 | 新旧 artifact 兼容 + runtime trace 测试 | 已执行（A） |

## 目标

修复一次真实快速试跑暴露的三个评测语义问题：

- 5 个红色样本实际来自 `citation_support=fail`，界面却显示为“X 红线”；
- cf-003 的截断事实 checker 因 citation annotation pending 被错误阻塞；
- claim 切分规则已多次修正，但 citation artifact 没有自己的版本。

## 不包含

- 不新增 claim group 联合支持判定；
- 不调整 citation support prompt 或 quote 校验规则；
- 不修改 R／X criterion 内容；
- 不重写历史 run 或判定。

## 验收条件

- [x] 矩阵可分别看到 X 违规和跨项检查失败。
- [x] 正式红线并集数量及 fail 优先级不变。
- [x] Citation pending 时，截断 checker 仍按 manifest 得出 pass／fail。
- [x] 新 citation artifact 写入 `claim-spans-v2`。
- [x] 旧 citation artifact 读取为 `legacy-unknown`。
- [x] 后端、前端、桌面和完整质量门通过。
- [ ] 合并后重启应用，用 `run_1658cca35bd34a06b2942e632970fc41` 对应的同一配置复跑 N=1，确认界面显示与真实失败类型一致。

## 证据包

### 真实运行事实

- Run：`run_1658cca35bd34a06b2942e632970fc41`
- Evaluation revision：`evalrev_80044ad0b587462db1bdc8fc090cd575`
- 18 个 product attempt 均执行成功，trace complete。
- 69 条生效判定：R pass 31、R fail 6、R pending 1；X pass 31、X fail 0、X pending 0。
- 6 条 R fail 分布在 5 个 case：`c12-001`、`c13-001`、`c14-001`（两条）、`cf-001`、`cf-003`。
- 5 个 `citation_support=fail` case：`c11-001`、`c15-001`、`c3-001`、`c8-001`、`cf-001`。
- 旧界面把这 5 个跨项引用失败样本显示成“X 红线”，但该 run 的 X 规则违规实际为 0。

### 自动化证据

- `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_evaluation_aggregation.py backend/tests/test_citation_annotation.py backend/tests/test_agent_runtime.py backend/tests/test_evaluation_judgment.py backend/tests/test_evaluation_baseline.py -q --basetemp backend/.runtime/pytest-028`：退出码 0。
- `backend\.venv\Scripts\python.exe -m pytest backend/tests -q --basetemp backend\.runtime\pytest-full-028`：完整后端测试退出码 0。
- `corepack pnpm quality:quick`：注释检查、Ruff format／lint、Pyright、Vue TypeScript、Vite production build、Rust fmt／check／clippy 全部通过。
- 覆盖的不变量：X fail 与跨项 CheckResult fail 分别计数，红线并集对同一 attempt 不重复；pending citation annotation 不阻塞只依赖 manifest 的 `truncation_declared`；新旧 citation artifact 的 claim span 版本可区分，并进入 runtime trace。

### 失败探针

- `citation_support=fail` 只增加 `check_failure_count`，不会增加 `x_violation_count`。
- 同一 attempt 同时存在 X fail 与跨项 failure 时，两个分项各计 1，`redline_violation_count` 仍只计 1。
- `cf-003` 在 citation annotation pending 且 `answer_completeness=partial` 时，截断 checker 仍为 pass，citation integrity 独立保持 pending。
- 旧 artifact 缺失 `span_version` 时读取为 `legacy-unknown`；新 artifact 为 `claim-spans-v2`，不把未知历史猜成当前规则。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么 X 违规和跨项检查失败要分开显示，但正式聚合仍允许两者合并为红线失败？ | 两者都不可接受，因此正式准出都要 fail；显示上合并会掩盖不同失败类型，导致定位问题很困难。 | 已通过 | 保留分项统计与明细分项标签。 |
| R2 | 为什么 citation annotation pending 不应阻塞截断事实 checker？ | 截断事实来自先完成的回答生成阶段，不依赖后置引用标注；实现上读取 trace manifest 的 `answer_completeness`。若被 citation annotation pending 阻断，事实检查会失去本可确定的结论。 | 已通过 | 截断 checker 独立构造 generation 输入，citation integrity 单独保持 pending。 |
| R3 | 为什么 claim 切分规则必须有独立版本，不能只依赖 prompt bundle 版本？ | prompt bundle 指导正文生成；claim 切分属于后置引用标注。后者依赖正文结果，但不依赖正文生成规则，因此两类规则可以独立演化，必须分别追溯版本。 | 已通过 | artifact、trace span 和引用事件记录独立的 `span_version`。 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 协作复盘

- AI 明显加速的环节：真实 run 事实提取、计数语义拆分、服务级回归补齐、前后端字段贯通与门禁执行。
- 负责人失去系统模型的环节：没有；负责人最先指出“红色 X”不能等价于任意跨项失败，并纠正了把两类失败混为一谈的方向。
- 真正发现问题的控制点：真实 N=1 运行和人工判读暴露 5 个 citation support 失败被旧文案归因为 X 违规；trace 复核暴露 `cf-003` 的 pending 阻塞。
- 只有仪式感、没有产生判断价值的流程：无。
- 下一次准备调整的一到两项：先定义统计字段的“计数对象”再命名；对跨项结果同时核对后端聚合、矩阵明细和实际 run 分布。

### 本次试行的流程规则

| 规则／假设 | 来源 | 想防住的失败 | 使用成本 | 实际效果 | 下次决定 |
| --- | --- | --- | --- | --- | --- |
| 评测失败类型必须区分 criterion 违规与跨项 checker 失败 | `run_1658...` 真实现场 | 把 citation support failure 误称为 X 红线 | 新增两个统计字段 | 真实事实、聚合与服务级回归均能解释 | 保留 |

## 已知风险与后续

- 本次没有解决 claim group 联合支持和引用必要性；
- citation judge 的 evidence quote 精确匹配仍可能产生 pending；
- 需要重启应用并用当前版本重跑，才能排除旧切分规则造成的表头误标。
