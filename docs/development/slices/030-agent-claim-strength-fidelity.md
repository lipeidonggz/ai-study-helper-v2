# 030 · Agent 证据强度与转折保真

- 状态：已否决
- 开始日期：2026-10-26
- 关联设计：[06 检索侧与 Agent](../../design/06-retrieval-pure-agent.md)
- 前置切片：[027 引用标注](./027-claim-aware-citation-annotation.md)、[029 引用评测可靠性](./029-citation-evaluation-reliability.md)
- 发现现场：`run_619f5e25271b4a9a99ac115be6c6879b` / `c1-001`

> 后续人工复核确认：“这是个好问题，但真正该问的范围更广”与
> “这个问题本身合理，但真正该问的范围更广”在中文语境中是保真的近义转述。
> 本切片最初把该表达误判为 Agent 语义漂移。其 12/50、10/50 数字不能继续作为
> Agent 质量结论；相关评测语义修复转入 031，`scan_term` 句界修复也由 031 接管。

## 控制契约

### 用户可观察目标

1. Agent 基于文档作答时，不得把“好问题”写成“问题本身正确”，也不得改变原文的可能性、数量、范围、状态、归因或转折条件。
2. 回答可以使用同义转述，但转述不能比证据更强、更确定或更宽泛。
3. 本轮不新增运行时判官门禁；先验证通用生成约束能否把真实语义漂移降到零。
4. `scan_term` 的命中片段按句界返回，避免把半句话作为证据上下文交给 Agent。

### 核心不变量

1. `<evidence>` 只增加通用语义保真规则，不写 `c1-001` 的具体问句、答案或疑似误判词对。
2. `but/however` 后的限制条件必须保留，不得合并成无条件肯定。
3. 评测 citation judge 不变；不得通过放宽判官来改善指标。
4. 实验曾升级到 `chat-v6`；确认前提误判后已撤回为 `chat-v5`，旧 run 保留可追溯。
5. `scan_term` 工具契约升级为 `retrieval-tools-v2`，其验证转入 031。

### 明确禁止

- 不在生产提示词中枚举单个已知错误样本。
- 不新增运行时 claim 支持门禁。
- 不修改当前 `citation-rubric-v5`。
- 不把 c1 的既有失败人工改成通过。

### 实验设计

- 用例：仅 `c1-001`。
- 每个 run：N=10，K 保持 9，exploratory。
- 轮数：5 个独立 run。
- 总样本：50 个 attempt。
- 成功标准：50/50 的 R、X 与 citation integrity/support 全部 pass；任何语义漂移或 citation failure 都保留并分析。

### 决策预算

- 预计 B 级决策：2 项。
- C 级决策：无。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 如何降低泛化转述漂移 | 在证据规则中增加主体、极性、数量、范围、时点、归因、强度和转折保真要求；提示词升级为 `chat-v6` | 负责人在 `c1-001` N=10 中确认 1 次红线也不可接受，且要求不得把个案错误写入提示词 | 只保留旧“限定词一致”过于抽象；运行时门禁增加一次全量判官调用且会形成双判官 | 影响所有后续 Agent run，需要新基线 | 5 个 c1-only N=10 run，共 50 attempts；仍有 12/50 citation support fail，未达到目标 | 已否决 |
| D2 | B | scan_term 的上下文片段如何返回 | 保留命中位置前后约 80 字的窗口，但向最近完整句子边界扩展；短块返回全文 | 负责人确认应排除任意字符截断的影响 | 固定字符窗口会切断句子；无条件返回整块会扩大常见存在性查询的 token 成本 | `scan_term` 输出与工具契约版本变化 | 句内命中、句末标点、块首尾与真实 c1 数据测试 | 已确认；转入 031 |

## 验收条件

- [x] prompt bundle 曾升级为 `chat-v6` 且不包含 c1 具体句子；由于前提误判已撤回。
- [x] prompt、协议和完整质量门通过。
- [x] 5 个 c1-only exploratory run 均完成，共 50 个 attempt。
- [x] 五轮判定完成并逐项分析任何 fail/pending。
- [x] `scan_term` 返回完整句子边界，并通过新的 5 个 c1-only N=10 run 验证。
- [ ] Agent 语义漂移结论不成立；“本身是对的，但范围更广”是合理的近义转述。

## 证据包

### 实验配置

- Prompt：`chat-v6`
- Prompt hash：`a4542ce695625a426abb2caf7b79c033d519664a98cb49c28ca0bd1afcab8f3e`
- 用例：仅 `c1-001`
- 每轮：N=10、K=9、exploratory
- 轮数：5
- 总样本：50
- Agent 配置：`max_output_tokens=8192`，当前 collection snapshot
- Citation judge：保持 `citation-rubric-v5`，未修改

### 五轮结果

| 轮次 | Run | Evaluation revision | 运行结果 | Citation support |
| --- | --- | --- | --- | --- |
| 1 | `run_9f57d53f585b445ea61f0b6ea720b9ed` | `evalrev_0119e8c87fac4318b74c55d9a517e395` | 10 succeeded | 9 pass、1 fail |
| 2 | `run_bbe5cdd0cb034ef788cb958f4d8e32b7` | `evalrev_9f2d96982a564550812fbfdf6c26ba8d` | 10 succeeded | 10 pass |
| 3 | `run_a839ecdf8ded4abd9123e11a27a11632` | `evalrev_eca24ff7a7fd41c59c0500ecb43aeb33` | 10 succeeded | 8 pass、2 fail |
| 4 | `run_2de4ade384664e55adbc7e1a3e9acdd2` | `evalrev_ef53ac259df045438f4c192ed31c86e6` | 10 succeeded | 5 pass、5 fail |
| 5 | `run_1904b5b114e142c89b72e2594c6b5c3a` | `evalrev_ad51514db9784b2db1ba95fa4a539a48` | 10 succeeded | 6 pass、4 fail |

### 语义审计

- 150 条 R／X criterion：150 pass、0 fail。
- 50 条 citation integrity：50 pass。
- 50 条 citation support：38 pass、12 fail、0 pending。
- 151 个 claim 引用组：139 supported、12 unsupported。
- 12 个 fail 全部集中在 `annotation_0003`：Agent 把 `It's a good question` 写成“这个问题本身是对的”或同义加强。
- 50 个答案中 11 个明确含“本身是对的”，另有 1 个使用“这个问题是对的”；判官对这 12 个语义增强全部判 unsupported。
- 与修改前 c1 单轮 1/10 fail 比较，`chat-v6` 为 12/50 fail；Fisher 双侧检验 `p=0.436`，没有证据证明此次提示词修改降低了失败率。

### 修正结论

`chat-v6` 所针对的前提不成立。原被计为 12 个“语义漂移”的样本，至少 11 个
明确保留了“但范围更广”的限制，应视为合理近义转述。该实验不能证明 Agent 转述失败，
也不能证明 `chat-v6` 有效；提示词已撤回为 `chat-v5`。

### scan_term 句界上下文对照实验

- 保持 `chat-v6` 与 `citation-rubric-v5` 不变，将 `scan_term` 工具契约升级为 `retrieval-tools-v2`。
- 命中上下文仍以命中点前后约 80 字为目标窗口，但向最近完整句界扩展；不再从任意字符处截断。
- 新一轮仍是 5 个 c1-only exploratory run，每个 N=10，共 50 个 attempt，全部 succeeded。

| 轮次 | Run | Evaluation revision | Citation support |
| --- | --- | --- | --- |
| 1 | `run_b9adc0e8826544f1a7a40dfe0982cfed` | `evalrev_76e6b4cdf7934002b2954af6fa88284a` | 9 pass、1 fail |
| 2 | `run_055fbbb0929848a39ef8b80c0f76bca8` | `evalrev_47e23acb96fe42768e9bbdde1c788f1a` | 9 pass、1 pending |
| 3 | `run_08da82da81e64e25af75d4aa966bcee0` | `evalrev_384a77d5320e412c9c0b887ac261ecfd` | 10 pass |
| 4 | `run_dea48ce101a243e0bd7cb5736879d956` | `evalrev_c05f291c230e4b9da65282a3901e711f` | 8 pass、1 fail、1 pending |
| 5 | `run_a25c0ba2e99642d1ac98b78988c45643` | `evalrev_355c62f440d04db687396111e8247cad` | 9 pass、1 fail |

- R／X criterion：150 pass、0 fail。
- Citation integrity：50 pass。
- Citation support：45 pass、3 fail、2 pending。
- 50 个答案中仍有 10 个写出“这个问题本身是对的”或同义表达；结合后半句范围限制，这是保真转述，不是语义漂移。
- 这 10 个样本中，7 个被 support judge 判 pass、3 个判 fail；说明 judge 对同义表达和批次上下文不稳定。
- 另有 2 个 pending 来自 annotation 把“提到过。”切成独立 claim，无法从该短语判断具体主张。

### 对照结论

- 完整句界上下文修正了真实工具输出中的半句截断，应保留。
- Agent 的所谓 10/50“语义漂移”是评测误判，不能作为 Agent 结论。
- citation support 表面从 12/50 非通过降到 5/50，但其中包含同一个目标 claim 在不同批次下 7 pass／3 fail 的不稳定判定，不能解释为单纯质量提升。
- 新发现两个独立问题：batch 内其他 claim 会影响目标 claim 的 verdict；claim annotation 仍可能把“提到过。”这类无独立语义片段当作 claim。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么这次修改 Agent 而不是放宽 citation judge？ | 待填写 | 待核对 | 待填写 |
| R2 | 为什么通用规则不能包含 c1 的具体错误句子？ | 待填写 | 待核对 | 待填写 |
| R3 | 为什么本轮暂不增加运行时门禁？ | 待填写 | 待核对 | 待填写 |

## 已知风险与后续

- 50 次零漂移仍不能证明概率为零，只能提高置信度。
- 如果仍出现语义漂移，再评估独立运行时验证器。
