# 044 引用标注的表格行联合证据覆盖

- 状态：已完成
- 开始日期：2026-10-28
- 关联设计：[06 检索与 Agent](../../design/06-retrieval-pure-agent.md)、[08 评测台](../../design/08-evaluation-console-design.md)
- 前置切片：[027 Claim-aware 引用标注](./027-claim-aware-citation-annotation.md)、[038 引用证据全文覆盖与截断片段](./038-citation-evidence-full-text-merge.md)、[043 引用标注的结构过滤与来源元门禁](./043-citation-annotation-scope-rules.md)
- 冻结现场：`run_11800cba865142bdb16cc8dabfd1bd64`
- 新判官 revision：`evalrev_5226a64a321a468db41f83509d0db8fc`
- v7 产品验证：`run_9219aed750914d2d9c0279bea64ea24c`
- v7 判官 revision：`evalrev_a781b0c8827749189ab8607744928a34`

## 控制契约

### 背景与失败场景

- 触发问题：c6 最新 N=10 中有 4 个 citation-support pending attempt、11 个待处理 claim occurrence；其中 3 个 claim 是把来源表格重排成回答表格的 row。
- 代表现场：`span_0028` 把“临时容器、防护对象、能力代价”写成一行，annotator 只选择来源表格 `b00018`，漏掉包含第三列细节的正文 `b00007`。
- 不处理时的用户损失：引用看似存在，但不能支持整行内容；评测进入 pending，人工复核量增加，错误引用还可能误导用户。
- 本切片负责什么：让 annotator 解释选证依据；若进入实现阶段，修复表格行按列完整覆盖和来源表重排问题。
- 本切片不负责什么：不修改 citation judge 标准；不处理其余残缺 claim 和 quote 协议问题；不拆改最终引用 UI。

### 用户可观察目标

表格行可以引用一个或多个 block，但所选 block 的联合内容必须覆盖表头定义的每个非空单元格；证据不足时不得用主题相近或形状相似的表 block 冒充支持。

### 核心不变量

1. 表格行仍是一个完整 claim span，不按单元格拆成多个用户可见引用。
2. 表格行的每个非空单元格都必须被至少一个所选 block 支持。
3. 多个 block 可以作为同一表格 span 的联合证据。
4. 来源表格与回答表格可能转置或重排，不能只按表格形状选择。
5. 无法完整支持时不得伪造引用；失败必须通过 reason、弃标或评测 pending 可见。

### 错误代价与非对称门禁

| 错误类型 | 具体例子 | 用户影响 | 处理分类 | 指标与阈值 |
| --- | --- | --- | --- | --- |
| 单 block 冒充整行 | 用 `b00018` 支持包含其他正文细节的表格行 | 引用不可回锚 | 阻断 | 目标表格行 N=10、K=9 且无已知错证 |
| 为过检强行引用 | 主题相近即选 block | 形成错误来源归属 | 阻断 | 固定反例不得出现错误 block |
| 证据不足却无披露 | 省略 annotation 但不说明 | 引用覆盖不可见 | 可容忍但需统计 | reason/弃标需保留 |
| 只优化总体 K | 其他 claim 改善掩盖表格错误 | 已知错误被平均值覆盖 | 阻断 | 表格类单独达标 |

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 单行由多个 block 支持 | 一个行级引用，详情列出联合 block | 完整 annotation 与 reason | 是 |
| 证据不足 | 不产生虚假绑定；评测保持可见 pending | span、候选 block、reason | 是 |
| 模型输出非法 | annotation pending，正文保留 | 原始输出与重试记录 | 是 |

### 明确禁止

- 不放宽 citation judge 来消除 pending。
- 不把“表格看起来相似”当成支持。
- 不为 c6 表格的固定文案写提示词特例。
- 不在 reason 观察阶段同时加入表格规则，避免混淆变量。

### 决策预算

- 预计 B 级决策：3。
- 是否存在 C 级决策：否。
- 超预算时拆分：reason 观察、表格规则、产品接入分别拆切片。

## 目标

1. 建立冻结 c6 answer 的 citation annotation 回放通道。
2. 让 annotator 为每个 annotation 返回简短 reason，暴露模型如何理解 claim 与 evidence。
3. 根据 reason 归类表格行选证错误，再决定 prompt 或输入结构的最小修改。
4. 修改后先验证 3 个表格行 claim，再验证完整 c6。

## 输入与输出

- 输入来自冻结 run 的原始 annotation prompt packet：question、answer、spans、evidence blocks。
- 输出保存每次重复的 annotations、block_ids、reason、协议错误、调用量与 prompt 版本。
- 不重新生成 answer，不重新检索 evidence，不改变 citation judge。

## 不包含

- 不处理 `- 挡的是数据外流` 等残缺 claim 的 span/context 设计。
- 不处理 citation judge 的非法 quote 输出。
- 不调整主 Agent 回答提示词。

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 |
| --- | --- | --- | --- | --- | --- |
| A1 | 可观察理解 | 每个选中 span 都有非空 reason | 冻结 c6 annotation 回放 | reason 覆盖率 100% | 真实模型 |
| A2 | reason 可定位问题 | reason 能区分表格形状、主题近邻和逐列覆盖 | `span_0028` 代表现场 | 人工可归类 | 真实模型 |
| A3 | 表格行正确 | 3 个表格 row 的联合 block 覆盖每个非空单元格 | 冻结 c6 N=10、K=9 | 3/3 达 K=9 | 真实模型 |
| A4 | 无回归 | 其他 body claims 不因表格规则产生错误绑定 | 完整 c6 | 已知错证为 0 | 真实模型 + 判官 |
| A5 | 产品等价 | 实验与 CitationAnnotator 共用 prompt、schema、解析和重试 | 契约测试 | 无分叉 | 自动化 |

## 验收执行

- [x] 冻结 c6 回放通道建立。
- [x] annotation reason 输出与审计完成。
- [x] 表格行 reason 分类完成。
- [x] 表格规则修改完成。
- [x] 完整 c6 annotation 回归完成。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 如何先观察选证理解 | annotator 输出简短 reason，但不加入表格规则 | 提示词语义对齐方法 | 直接改规则无法知道是否命中真实原因 | prompt schema 增加 reason | c6 冻结回放 | 已确认 |
| D2 | B | 表格行是否拆成 cell span | 保持 row 为一个 span；内部按列校验覆盖 | 避免破坏行身份和用户引用粒度 | 拆 cell 会改变偏移和展示语义 | 暂不改 span 粒度 | 已确认表格 row 现场 | 已确认 |
| D3 | B | 表格修改入口 | 先提示词，必要时再增加局部表头上下文；仍不收敛才做结构化逐列输出 | 改动最小且可回退 | 一开始做复杂 schema 成本高 | prompt/input 可能迭代 | N=10 收敛情况 | 已确认 |

## 证据包

### 自动化证据

- `ruff check` 与定向 `pytest`：31 passed。
- 完整后端：349 passed。
- `corepack pnpm quality:quick`：通过。

### 运行证据

- 基线：4 pending attempt、11 pending claim occurrence；其中 3 个属于表格 row。
- 表格 row：`annotation_0028`、`annotation_0029`、`annotation_0030`。
- reason 观察，`claim-citation-v4`，冻结 c6 N=10：
  - 100 次 attempt replay、2669 个 annotation，reason 覆盖率 100%，协议错误 0。
  - `span_0028` 只有 4/10 选择 `b00018 + b00007`；其余 6/10 只选 `b00018`，并在 reason 中错误声称该 block 包含实际没有的“无持久工作区、无用户文件访问”。
  - `span_0029` 只有 1/10 选择同时覆盖本地网络边界、bash 依赖和沙箱外进程风险的 block 组合；5/10 只选 `b00018`。
  - `span_0030` 5/10 选择 `b00018 + b00013 + b00017`；3/10 只选 `b00018`。
  - 共同根因：把回答表格的单元格文字当成来源表格的内容，没有回到 block 原文逐格核验。

#### 表格规则与短 evidence 引用

- v5 表格规则：三条 row 均命中完整联合证据；但长 block_id 出现 3/100 次复制错误。
- v6 仅增加“逐字复制真实 block_id”规则：定向 30 次仍有 4 次 `citation_annotation_source_invalid`，其中模型把 `doc_...` 写成 `block_...`。
- v7 改由 Harness 暴露 `evidence_001` 等短引用并在解析时映射回真实 block_id：
  - 定向 3 attempts × 10 次：协议错误 0。
  - 表格三行均 10/10 选择覆盖全部非空单元格的 block 集合。
  - 完整冻结 c6 10 attempts × 10 次：2689 个 annotation、reason 覆盖率 100%、协议错误 0。

#### v7 产品 c6 N=10

- Plan：`plan_71eecc4f72944de9904f65762d14a661`
- Run：`run_9219aed750914d2d9c0279bea64ea24c`
- 执行：10/10 succeeded，基础设施失败、产品执行失败、unknown 均为 0。
- 模型调用：71；输入/输出 token：547114 / 45850。
- R/X：48 pass、2 fail、0 pending。
- Citation integrity：10/10 pass。
- Citation support：4 pass、6 pending attempt；合计 7 个 unknown 与 3 个 unsupported candidate。
- Case：3 pass、4 fail candidate、3 pending，保持 pending，未达到 K=9。
- 本批回答没有再次生成那三行表格，因此表格规则由冻结回放验证，不能由这次随机重答直接证明。
- 新 pending 集中在关系型综合句和残缺指代片段，不是本切片的表格 row 类。

#### 冻结 answer + v7 重标 + citation judge 重判

- 固定 answer：`attempt_4760756571864c56b840e4124435e4ca`。
- 从原 trace 重放原始 question、answer、spans 和 evidence blocks，不重新生成答案、不重新检索。
- 用 `claim-citation-v7` 重新标注 10 次，再直接调用生产 `_judge_citation_task` 和 `citation-rubric-v11` 判官。
- Annotation 协议错误：0。
- Citation judge：10/10 pass。
- 三条表格 claim 均 10/10 选择完整联合证据：
  - `span_0028`：`b00018 + b00007`。
  - `span_0029`：`b00018 + b00008 + b00013`。
  - `span_0030`：`b00018 + b00012 + b00013 + b00017`。
- 产物：`backend/.runtime/citation-annotation-v7-table-judge.json`。

### 失败探针

- 来源表格形状相似但不覆盖完整 row。
- 多个 block 分别覆盖不同列。
- 表格中某一列完全没有证据。

## 已知反例账本

| 场景或反例 | 期望行为 | 实际结果／分布 | 是否阻断 | 处理结论 |
| --- | --- | --- | --- | --- |
| `span_0028` 临时容器行 | 选择 `b00018 + b00007` 或等价联合证据 | v7 10/10 选择联合证据，judge 10/10 pass | 否 | 已修复 |

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么先加 reason，而不是直接加表格规则？ | reason 用来暴露模型是否把回答中的表格内容误当成来源证据，先定位语义理解再改规则 | 通过 | 观察后在 v5 加入逐列覆盖规则 |
| R2 | 为什么表格 row 不拆成 cell？ | 表格 row 是用户可理解的一次 claim，拆 cell 会丢失行身份并改变引用粒度 | 通过 | 保持 `body_table_row` 为单个 span |
| R3 | 为什么不能通过放宽 citation judge 解决？ | judge 正确发现了证据不足；放宽标准会掩盖错误引用，真正问题在 annotator 选证 | 通过 | 保持 citation-rubric-v11 不变 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 协作复盘

- 待完成后填写。

## 已知风险与后续

- 未验证项：表格规则是否泛化到更多转置表和其他表格结构。
- 接受的风险：增加 reason 会提高少量输出 token。
- 后续：v7 产品 c6 的 2 个 R1 fail、3 个 unsupported candidate、7 个 unknown 已转入关系型综合句与残缺指代专项。

## 停止与推翻条件

- reason 显示 error source 不在表格覆盖而大量来自 claim 本身，则撤销表格规则假设，重新划分问题。
- prompt 连续三轮无法在正反表格行上达到 K=9，则改用局部表头上下文或结构化逐列覆盖。
