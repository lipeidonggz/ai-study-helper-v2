# 2026-10-29 · cf-001 新数据基线

## 目的

验证跨源消歧：当问题只说“一篇 OpenAI、一篇 Anthropic”而没有唯一标题或主题锚点时，Agent 是否要求用户澄清，而不是自行选择文档比较。

## 固定现场

| 项目 | 值 |
| --- | --- |
| case revision | `cr_cf_001_187df285440e` |
| question | 一篇 OpenAI 的、一篇 Anthropic 的，它们对 agent 工程怎么做有什么看法？ |
| collection snapshot | `s_6b787258ba7d700c` |
| model | `deepseek-flash` |
| temperature | `0.0` |
| thinking | disabled |
| prompt bundle | `chat-v17` |
| prompt hash | `ceba17252fd6ff920caebd8c51c6b83b3d37bf1b0786d5c6090ff835c5c74a93` |
| tool contract | `retrieval-tools-v5` |
| N／K | 10／9 |

## 运行证据

- Plan：`plan_2d66467ad34846cd959df4a5a5c18131`
- Run：`run_4ace04aa83204c54ba982d8913e3f9f6`
- Judgment revision：`evalrev_64b785a095f24aadb071eb83c605fae7`
- Agent attempt：10/10 succeeded
- infrastructure failure：0
- partial：0
- failed：0
- model calls：93
- input tokens：1,338,215
- output tokens：106,694

## 判定结果

- criterion：0 pass、10 fail、0 pending
- case：0/10，未达到 K=9
- citation integrity：10/10 pass
- citation support：多数 good；2 个 review，无 check failure/pending

## 失败模式

10/10 都是同一种 R1 失败：

- Agent 自行选择了 OpenAI 与 Anthropic 的具体文章；
- 直接展开观点比较；
- 没有同时要求用户澄清 OpenAI 和 Anthropic 各指哪一篇。

常见自选组合包括 `OpenAI：Harness engineering` 与 `Anthropic：Building Effective AI Agents`，也有答案选择多篇 Anthropic 文章后直接归并。

## 初步判断

问题缺少唯一文档锚点，Agent 应将其视为目标不明确，先询问两篇文章标题或主题。当前 `chat-v17` 尚未稳定执行这一步，属于真实的消歧行为缺口，而不是 Judge 语义问题。

本轮不修改提示词或金标准。

## 工具路径根因

10/10 都先调用 `list_documents`，并且已经看到每个来源有多篇候选。之后 Agent 仍继续：

- 对 2～7 篇文档调用 `get_outline`；
- 对 2～5 篇文档调用 `list_blocks`；
- 选择《Harness engineering》《Building Effective AI Agents》等作为“代表”，或直接归并同来源多篇文档；
- 最后才输出比较结论，没有要求澄清。

因此不是 Agent 没发现多篇候选，而是把用户所说的“一篇 OpenAI、一篇 Anthropic”解释成“从每类中自行选择代表”。这造成了大量无效检索：

- 93 次模型调用；
- 1,338,215 input tokens；
- 106,694 output tokens。

## 修复建议

不应加入 OpenAI、Anthropic 或具体文章标题特例。应增加通用的“目标选择前门禁”：

1. 先识别问题要求的实体槽位，例如“每个来源一篇”是两个独立槽位。
2. 用户没有给出唯一标题、doc_id 或可判定筛选条件时，可以调用 `list_documents` 获取候选。
3. 某个必需槽位仍有两个或以上同样合理的候选时，必须停止深入读取，列出候选并要求用户选择。
4. 只有候选唯一，或用户明确授权“任选代表／比较全部／按某规则选择”时，才能自行决定目标。
5. 选择澄清后直接 `finish`，不继续调用 `get_outline`、`list_blocks` 或生成比较正文。

对应的通用提示词可放入独立 `target_resolution` 区块，与检索和回答覆盖规则分开。验证应使用：

- cf-001：两个来源均有多篇候选，必须同时澄清两个槽位；
- cf-002：单数“这个文档”指代不明，必须澄清；
- 唯一标题或唯一主题锚点正例：不得无谓追问，应直接执行。

## 复现

- 执行脚本：`backend/.runtime/run_case_fresh.py --case-id cf-001`
- 结果文件：`backend/.runtime/cf-001-fresh-result.json`

## 中间校准：从“必须等待澄清”改为“存在澄清动作”

上文“初步判断”和“修复建议”建立在旧题面“各选一篇”之上。沛东复核后指出，该题面按普通用户语义可以理解为允许 Agent 从每类中各选一篇，因此旧结论不能直接用于修改 Prompt。

后续按两个变量阶段验证：

1. 只改题面为“刚才那篇 OpenAI 的、那篇 Anthropic 的”，Prompt 保持 `chat-v17`。
2. 使用旧 R1“必须先澄清且不得自行比较”，首次新数据结果为 3 pass、7 fail、0 pending。7 个失败中，既有完全自选的答案，也有“先说明指代不明、再按最可能候选比较”的答案。

沛东进一步确认：从用户视角看，澄清后按最可能候选提供暂定比较并非不可接受，关键是答案中是否存在可操作的澄清动作。因此只修改 cf-001 R1，不修改 Agent Prompt：

- 明确“刚才那篇”缺少可解析前文；
- 向用户提供标题、候选确认或假设纠正入口；
- 允许澄清后继续比较；
- 完全没有澄清动作、只自行选文后直接比较，仍判 fail。

### 冻结重判证据

| 项目 | 值 |
| --- | --- |
| case revision | `cr_cf_001_df0231184380` |
| question | 刚才那篇 OpenAI 的、那篇 Anthropic 的，它们对 agent 工程怎么做有什么看法？ |
| frozen run | `run_3ddae5d9a196429bb7e1888b945d327e` |
| prompt bundle | `chat-v17` |
| prompt hash | `ceba17252fd6ff920caebd8c51c6b83b3d37bf1b0786d5c6090ff835c5c74a93` |
| judgment revision | `evalrev_45a4e1ac98b243a48b1824e40ce22465` |
| 结果 | 6 pass、4 fail、0 pending；未达到 K=9 |

4 个 fail 均没有可操作的澄清动作：一个直接断言两篇对象，两个完全未解释“刚才那篇”的指代，一个只列出多个候选后直接按四篇比较。该结果只证明新判据边界可运行，不证明 Agent 已稳定具备澄清行为。

## 中间校准：从“澄清动作”改为“目标可见＋一边一篇”

沛东进一步确认：澄清的真实目的不是强制用户先回答一轮，而是：

1. 能精确回答时直接回答；
2. 不能精确回答时，让用户能够发现对象或假设上的不精确，并能低成本纠正。

因此最终不再把“是否出现澄清动作”作为单独指标，而是形成两条判据：

- R1 目标可见：答案必须列出实际采用的 OpenAI 和 Anthropic 文档，并通过明确推断、候选披露或邀请纠正中的至少一种方式，让用户知道目标是否确定；不要求固定措辞或等待回复。
- R2 来源与数量：实际比较必须只使用一篇 OpenAI 和一篇 Anthropic 文档，机构归属正确，不允许同源多篇合并或扩展比较；只请求澄清而未比较时本项满足。

### 中间冻结重判证据

| 项目 | 值 |
| --- | --- |
| case revision | `cr_cf_001_5c95ca1ce786` |
| question | 刚才那篇 OpenAI 的、那篇 Anthropic 的，它们对 agent 工程怎么做有什么看法？ |
| frozen run | `run_3ddae5d9a196429bb7e1888b945d327e` |
| prompt bundle | `chat-v17` |
| prompt hash | `ceba17252fd6ff920caebd8c51c6b83b3d37bf1b0786d5c6090ff835c5c74a93` |
| judgment revision | `evalrev_50a5158dde3342b6aa6bb24e37e11e00` |
| criterion 结果 | 18 pass、2 fail、0 pending |
| case 结果 | 8 pass、2 fail、0 pending；未达到 K=9 |

样本差异：

- 样本 3：R1、R2 均 pass。末尾纠正入口和正文中的推断说明足以让用户判断目标。
- 样本 8：R1、R2 均 pass。“我按知识库里的文档对应为”被视为透明披露，未要求固定措辞。
- 样本 9：R1 pass、R2 fail。开头披露了自选，但实际合并三篇 Anthropic，违反一边一篇。
- 样本 10：R1 pass、R2 fail。实际使用四篇扩展比较。

中间曾尝试要求纯澄清答案的候选清单也校验机构归属，但 Judge 对“仅列候选、不做比较”的 R2 适用范围产生 pending，且固定措辞导致样本 3/8 抖动。按提示词语义对齐闭环，停止继续堆叠 cf-001 措辞；候选清单元数据校验转记 [BL-0014](../backlog.md)。

## 最终校准：目标范围可见

沛东确认，“agent 工程”是宽泛主题。Agent 抓住这个主题，把一侧明显相关的多篇文档一起用于综合，是合理行为；因此一边一篇不应继续作为硬门槛。cf-001 最终只保留 R1：

> 答案必须让用户看见实际采用了哪些文档、如何理解“刚才那篇”，并至少通过推断说明、候选/假设披露或邀请纠正中的一种方式让范围可发现。

### 最终冻结重判证据

| 项目 | 值 |
| --- | --- |
| case revision | `cr_cf_001_c1898e3ba761` |
| question | 刚才那篇 OpenAI 的、那篇 Anthropic 的，它们对 agent 工程怎么做有什么看法？ |
| frozen run | `run_3ddae5d9a196429bb7e1888b945d327e` |
| prompt bundle | `chat-v17` |
| prompt hash | `ceba17252fd6ff920caebd8c51c6b83b3d37bf1b0786d5c6090ff835c5c74a93` |
| judgment revision | `evalrev_9bf055e2804c4bc3bdd44e8410f0f0d3` |
| 结果 | 10 pass、0 fail、0 pending；达到 K=9 |

样本 3、8、9、10 均通过。样本 9 合并三篇 Anthropic、样本 10 采用四篇跨机构文档，均因实际范围已向用户披露而接受。该结果表明 cf-001 在当前 `chat-v17` 上已饱和；未来若要考察更严格的一边一篇或候选机构归属，应新增独立用例或确定性 checker，而不是继续收紧本用例。
