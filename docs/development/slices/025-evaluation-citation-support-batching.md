# 025 · 长答案引用支持分批检查

- 状态：已完成
- 开始日期：2026-10-24
- 关联设计：[03 用例与金标准](../../design/03-cases-and-gold-standard.md)、[08 评测台](../../design/08-evaluation-console-design.md)
- 前置切片：[023A 程序 Checker 与 LLM Judge](./023A-evaluation-judgment.md)、[023B 生效判定与 N/K 聚合](./023B-evaluation-aggregation.md)
- 人工验收：[025 引用支持分批检查人工验收](../manual-tests/025-citation-support-batching.md)

## 控制契约

### 用户可观察目标

1. 长答案不再因为全部被引用证据一次发送超过预算而直接 pending；cf-003 的历史长答案能够完成引用支持检查。
2. 引用检查按句子、段落、列表项或表格行等局部语义上下文执行，不再把每个引用错误绑定到“前一个句子”。
3. 用户能看到批次数量、完成覆盖、每个 block 的合并 verdict、失败批次和 token／请求审计。
4. 旧 EvaluationRevision 与旧 CheckResult 保持不变；修复通过重判生成新 revision。

### 核心不变量

1. 引用语义上下文必须来自答案本身，按 Markdown 段落、列表项、表格行和普通行稳定解析。
2. 每个 `(block_id, context_id)` occurrence 必须被恰好判定一次；重复、遗漏、跨批归属错误都不得判 pass。
3. 批次结果按 occurrence 合并到 block：任一 unsupported 为 fail，否则任一 unknown／批次失败为 pending，全部 supported 才 pass。
4. 单个语义单元、单块证据或批次超过预算时显式 pending，不截断、不拆句猜测、不丢 block。
5. 所有批次的输入、输出、usage、request ID 和错误与一个 attempt 级 CheckResult 一起保留。

### 明确禁止

- 不把“引用只支持前一个句子”作为判定假设；
- 不发送完整长答案到每个批次；
- 不在预算不足时静默跳过 citation 或只检查前 N 个；
- 不把批次部分成功当成整体 pass；
- 不改写旧 CheckResult、Judgment 或 EvaluationRevision；
- 不自动修改 Agent 回答或引用。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 总证据超限但可分批 | 多个批次完成后合并结果 | 每批审计与 block 合并 verdict | 是，一次重判完成 |
| 任一 occurrence unsupported | citation_support fail | unsupported 理由及全部批次结果 | 可人工复核或重判 |
| 任一 occurrence unknown | citation_support pending | unknown 理由及覆盖详情 | 可人工复核或重判 |
| 单批超时、预算耗尽或输出非法 | citation_support pending | 已完成批次、失败批次与 invocation | 可重判 |
| 单个语义单元或证据块自身超预算 | 明确 pending，不发起该次模型调用 | 超限 occurrence、上下文和大小 | 修复异常输出后重判 |
| occurrence 输出缺失、重复或跨批混入 | citation_support pending | 原始输出与批次期望集合 | 可重判 |

### 人工检查点

- `citation-rubric-v3` 全文必须由负责人审核后才能生效。
- 主路径先用 fake provider 验证语义单元、分批、合并和失败路径。
- 完成后用 cf-003 历史 `73,611` 字符引用证据现场和真实模型各验证一次。

### 决策预算

- 预计 B 级决策：3 项，均已通过对话确认方向。
- C 级决策：无。
- 不拆分：上下文解析、分批、合并和审计属于同一条不可分割的引用判定链。

## 目标

修复长答案引用支持检查因总输入超预算而永久 pending 的能力缺口，同时避免用单句假设错误缩小引用语义范围。

## 输入与输出

输入：

- 一个 attempt 的最终答案；
- 答案中的显式引用；
- 被引用 block 的 title、section path 和 text；
- 当前 Judge 并发、请求和 token 预算。

输出：

- 一个 attempt 级 `citation_support` CheckResult；
- block 级 supported／unsupported／unknown 合并结果；
- occurrence、批次、invocation、usage、错误和覆盖审计。

## 不包含

- 不自动修复答案或引用；
- 不重新检索缺失 block；
- 不实现长语义单元的内部句子拆分；
- 不修改现有用例金标准；
- 不实现判官一致性和失败诊断工作台。

## 验收条件

- [x] 旧单批输入在预算内时仍得到等价结果。
- [x] cf-003 历史长证据完成多批检查，不再出现 input_too_large pending。
- [x] 段落、列表项和表格行引用均使用正确局部上下文。
- [x] occurrence 覆盖、block 合并和 fail／pending 优先级有自动化测试。
- [x] 超长单单元、超时、预算和非法输出均显式 pending。
- [x] citation-rubric-v3 与 checker version 被固化在结果中。
- [x] 自动化质量门和真实 cf-003 探针通过。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 引用对应的答案范围 | 不假设单个前句；按空白段、列表项、表格行和普通行构造局部语义上下文 | 03 要求逐条验引用；真实 cf-003 同时存在句末、段末、列表项和表格引用 | 单句映射会漏支持多种主张；发送完整答案会重复放大 token | 决定 context parser、prompt input 和 occurrence 身份 | 三种上下文形态与 cf-003 样例测试 | 已确认 |
| D2 | B | 批次边界 | 每批最多 5 个 occurrence，引用支持输出上限独立为 4,096 token，序列化输入不超过现有 60k 字符预算；单单元超限直接 pending | 真实 cf-003 探针中 10 条结果发生截断或 evidence quote 幻觉；拆成两个 5 条批次后输出完整 | 无限拆句会制造上下文碎片；所有判官统一提高到 4,096 会扩大主判官成本 | 决定请求数、成本和异常语义 | 多批、5 上限、输出连续、单单元超限测试 | 已确认 |
| D3 | B | 批次结果合并 | 每个 occurrence 独立判定；block 任一 unsupported 为 fail，否则任何 unknown／失败为 pending，全支持才 pass | 023B fail > pending > pass 与红线不可平均 | 任一批 pass 即 pass 会让遗漏被掩盖；直接重试整条长答案会放大成本 | 决定 CheckResult、人工复核输入和 UI 摘要 | 混合 verdict、覆盖缺失与预算耗尽测试 | 已确认 |

等级：

- A：AI 可自主执行，交付时列入审计摘要；
- B：完成后集中人工确认；
- C：执行前必须停止并获得人工决定。

## 证据包

### 自动化证据

- 命令：`backend\.venv\Scripts\python.exe -m pytest backend\tests -q --basetemp=.pytest-tmp-025-full1`
- 结果：244 passed。
- 命令：`corepack pnpm quality:quick`
- 结果：通过。
- 提示词：`citation-rubric-v3` 已由负责人于 2026-10-24 审核确认，hash 为 `7f37bcb5d7faee0e50c88c4c1271471aae06d029d785715b372e4b2178671953`。
- 覆盖的不变量：
  - 段落、列表项、表格行和普通行上下文解析；
  - 多批覆盖全部 occurrence；
  - 任一批 unsupported 时整体 fail；
  - 单单元超限、上下文缺失、occurrence 漏报均 pending；
  - 引用支持输出上限为 4,096 token；
  - `citation-rubric-v3` 与 `citation_support:2` 被固化。

### 运行证据

- 启动方式：复制真实 eval.db，使用 cf-003 历史 trace 和真实 `deepseek-flash` 引用判官；不重跑 Agent。
- 用户操作序列：对 `run_0cd1a6649ed340d0b71df10b0a626b09` 的 `attempt_cdc1bc05d2ce4313b0931e1e2cd2d986` 执行引用支持检查。
- 预期结果：15,575 字答案、70 个唯一证据块、51 个上下文和 72 个 occurrence 全部被合法覆盖，不再因 60k 总证据超限直接 pending。
- 实际结果：按每批最多 5 个 occurrence 计划为 15 批；15 批全部返回合法结构化结果，无输入超限、截断或 occurrence 覆盖错误。共使用 41,906 input token、13,290 output token。
- 判定结果：整体 fail；68 个 block supported、1 个 unsupported、1 个 unknown。unsupported block 为 `doc_3683c99bf98342cf958ba8b9384f2008#v0001#c1#b00018`，判官理由是该块只讨论客服，未支持上下文中关于编码 agent 与“两个最有价值领域”的并列主张。

### 失败探针

- 注入或复现的失败：
  - 22 个长证据块跨批测试；
  - 单语义单元超过 60k 字符；
  - answer 中的引用无法定位上下文；
  - 模型遗漏 occurrence 输出；
  - 首批 unsupported、后续批 supported。
- 系统保留的旧状态：原 EvaluationRevision、CheckResult 和 trace 不变；真实探针只写工作区副本。
- 用户看到的错误：超限、上下文缺失、occurrence 缺失分别显示稳定错误码；单批 unsupported 不能让其他批次覆盖为 pass。

### 人工验收

- [x] 负责人确认引用支持分批摘要和 pending 错误展示可读。

## 反向解释

实质性切片必须准备 3～5 道行为问题，由负责人逐题独立回答。AI 每次提问必须说明当前题号和剩余题数。

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么引用检查不能默认只检查引用前面的一个句子？ | 引用标注本身没有严格的人类统一语法，可能覆盖多句话、列表项或表格内容；真实答案里也未出现跨段落标注。因此应以引用所在的局部语义单元为边界，不能硬编码成前一句。 | 已通过 | 保持段落、列表项、表格行和普通行上下文，不做跨段落拼接。 |
| R2 | 为什么某个引用在部分批次 supported、部分批次 unknown 时不能判 pass？ | pass 表示引用的全部相关主张都被支持。一个 block 被多处引用时，只有全部 occurrence 都 supported 才能算该 block 引用正确；整次检查更要完整覆盖。按 block 聚合主要是实现和批次效率策略，不是降低判定语义。 | 已通过 | 保持 occurrence 全覆盖，block 任一 unknown 为 pending、任一 unsupported 为 fail。 |
| R3 | 为什么单个语义上下文自身超预算时只能 pending，不能截断后继续？ | 截断会改变实际判定输入，被截断部分可能包含反向证据；此时得到 supported 可能是假通过，破坏引用完整性和可追溯语义，因此只能 pending。 | 已通过 | 单个语义单元或证据块超预算时不调用模型，显式保留超限现场。 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 协作复盘

- AI 明显加速的环节：语义单元解析、occurrence 覆盖、批次打包、聚合和测试骨架可围绕既有判定链快速实现。
- 负责人失去系统模型的环节：首版把单个引用上下文错误假设成前一个句子；负责人通过真实答案形态纠正为段落、列表项和表格行语义单元。
- 真正发现问题的控制点：真实 cf-003 模型探针依次暴露 2,048 token 截断、10 条批次 evidence quote 幻觉，最终以 5 条批次得到合法完整输出；这些都不是单元测试能覆盖的。
- 只有仪式感、没有产生判断价值的流程：本轮未发现；提示词人工复核、真实模型探针和界面摘要都产生了有效证据。
- 下一次准备调整的一到两项：继续观察其他长答案的 unknown 比例；如批次成本明显上升，再评估面向 claim 的更细预筛，但不改变 fail > pending > pass 语义。

### 本次试行的流程规则

| 规则／假设 | 来源 | 想防住的失败 | 使用成本 | 实际效果 | 下次决定 |
| --- | --- | --- | --- | --- | --- |
| 引用上下文以语义单元而不是单句为准 | 2026-10-24 人工讨论与真实 cf-003 答案 | 把段落、列表和表格引用错误缩小到前一句 | 需要 Markdown 局部解析与 occurrence 审计 | 真实 cf-003 的段落、列表和表格引用均完成分批 | 保留 |

## 已知风险与后续

- 未验证项：其他长答案在 `citation-rubric-v3` 下的长期稳定性。
- 接受的风险：每批最多 5 个 occurrence、输出上限 4,096 token；真实判官仍可能因证据不足返回 unknown；cf-003 真实探针已出现 1 个 unsupported 和 1 个 unknown，均为有效判官结论而非检查失败。
- 后续待办或 ADR：如长答案经常产生大量语义单元，再评估按上下文聚类或本地预筛。
