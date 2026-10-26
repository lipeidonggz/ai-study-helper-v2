# 035 · Agent 因果关系与权衡保真

- 状态：暂停，等待 036 判官冻结
- 开始日期：2026-10-26
- 关联设计：[03 用例与金标准](../design/03-cases-and-gold-standard.md)、[06 检索侧与 Agent](../design/06-retrieval-pure-agent.md)
- 前置切片：[031 引用语义校准](./031-citation-semantic-calibration.md)、[034 c2 指标归属金标准](./034-c2-metric-attribution.md)
- 发现现场：`run_619f5e25271b4a9a99ac115be6c6879b`、`run_7388764117024001aa8aa24bd5720afd`

> 2026-10-26：关系判官任务边界、列表上下文包和既有 attempt 重判移入
> [036](./036-citation-context-and-judge-separation.md)。本切片冻结 `chat-v7`
> 生成提示词，待 036 完成后继续 Agent 调优；v8 判官结果仅保留为实验证据。

## 控制契约

### 用户可观察目标

1. citation judge 能识别 claim 与 evidence 在因果、条件、比较、取舍和方向上的关系反转。
2. Agent 转述证据时保留原文的语义角色，不把收益与代价、原因与结果或相反方向的事实对调。
3. 修复不依赖具体用例数字、文档名或答案原句。

### 核心不变量

1. 即使实体、数字和主题词相同，只要收益／代价、原因／结果、前提／结论或作用方向被对调，引用支持应判 `unsupported`。
2. Agent 可以使用不同句式和语序，但不能改变事实在关系中的语义角色。
3. 生产提示词不包含 `c2-001`、83%、17%、0.4%、84% 或具体答案片段。
4. 第一轮不增加运行时关系门禁；先验证判官和 Agent prompt 的效果。
5. 历史 CheckResult、Judgment 和 revision 不改写；重判和重跑都追加新事实。

### 明确禁止

- 不为 c2 的固定句对增加模板匹配。
- 不因出现相似数字、关键词或连接词就判 supported。
- 不使用判官自行补充的外部知识判断关系。
- 不在本切片提前运行全量 suite；本切片只做 c2 单 case 校准与回归。

### 失败语义

| 场景 | 期望行为 |
| --- | --- |
| benefit／cost 方向保持 | citation support `supported` |
| benefit／cost 对调 | citation support `unsupported` |
| 原因与结果对调 | citation support `unsupported` |
| 拦截与漏过、增加与减少等方向反转 | citation support `unsupported` |
| Agent 改写保持关系角色 | 答案通过关系检查 |
| Agent 改写交换关系角色 | 不进入正式基线 |

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | citation judge 如何识别关系反转 | 升级为 `citation-rubric-v8`，比较语义角色和方向，不比较固定句式 | 现有错误 claim 已有完整证据，但 v7 因共享实体和数字误判 supported | 只加具体反例会造成用例特判；依赖 annotation 再拆句无法修复完整句内关系反转 | judge prompt 与 checker version 变化 | 固定正反例重复 10 次、旧 run 新 revision | 已确认 |
| D2 | B | Agent prompt 如何表达关系保真 | 升级为 `chat-v7`，增加通用因果、条件、比较、取舍和方向不变量，不复用已否决的 `chat-v6` | 44 个历史 attempt 中有 8 个明确关系反转，最新 N=10 仍有 2 个 | 具体 A/B/C 模板过窄；增加运行时门禁会先受判官稳定性制约 | 影响所有后续 Agent run | prompt 不含 c2 专用词；c2 50 attempts | 已确认 |
| D3 | B | 第一轮是否增加运行时关系门禁 | 不增加；先验证 v8 判官和 v7 Agent prompt | 当前 citation judge 有已知漏检，不能作为可靠前置门禁 | 双判官会增加成本并复制不确定性；仅靠 prompt 可能仍有残余错误 | 本轮不改变产品响应时延 | 只有 50 次回归仍存在关系反转时才评估 | 已确认 |
| D4 | A | 回归顺序 | 先验证判官能抓到两个已知错误，再修 Agent；仅跑 c2 单 case，不跑全量 suite | 防止判官漏检导致 Agent 指标偏乐观；用户明确暂不启动全量回归 | 直接跑全量 suite 成本高且会掩盖判官问题 | 节约 token，逐 case 验收 | 固定 packet、旧 run 重判、c2 5 × N=10 | 已确认 |

## 验收条件

- [ ] `citation-rubric-v8` 包含通用关系方向规则，且不含 c2 专用数字或原句。
- [ ] citation judge 对两个已知关系反转尝试均判 unsupported。
- [ ] citation judge 对保真权衡转述保持 supported。
- [ ] `chat-v7` 包含通用关系保真规则，且不含 c2 专用数字或原句。
- [ ] c2-only 50 attempts 中不存在“低摩擦／低打扰是代价”的因果反转。
- [ ] 固定正反校准、定向测试和 `quality:quick` 通过。
- [ ] 不运行全量 suite；留待后续基线阶段。

## 证据包

待实现完成后填写。

## 已知风险与后续

- 通用规则仍依赖模型语义能力，不能形式化保证零错误。
- 如果 50 次后仍出现关系反转，再评估轻量最终关系自检；不在本轮预设。
