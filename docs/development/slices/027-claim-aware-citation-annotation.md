# 027 · Claim-aware 引用标注

- 状态：已完成
- 开始日期：2026-10-25
- 关联设计：[06 检索侧与 Agent](../../design/06-retrieval-pure-agent.md)、[08 评测台](../../design/08-evaluation-console-design.md)
- 前置切片：[020 流式问答](./020-streaming-chat.md)、[025 引用支持分批](./025-evaluation-citation-support-batching.md)、[026 结构化 ReAct](./026-structured-react-protocol.md)
- 人工验收：[027 Claim-aware 引用标注人工验收](../manual-tests/027-claim-aware-citation-annotation.md)

## 控制契约

### 用户可观察目标

1. 最终回答仍由一次正文生成完成，后置引用标注不能改写、重排或重新分段正文。
2. 用户看到的引用编号来自结构化 citation annotation；点击后在角标旁的就地悬浮卡中查看本次真实 evidence，不打断正文阅读位置。
3. 判定台收到明确的 `claim span -> block_ids` 关系，不再从自由文本 `【来源：block_id】` 猜测引用归属。
4. 标注失败时原回答继续可见，但引用状态明确为未完成，不能伪装为引用通过。

### 核心不变量

1. 正文只生成一次；citation annotator 的输出禁止包含正文，Harness 只接受合法的 span ID 和 evidence block ID。
2. 每个 annotation 的字符范围必须能从不含引用的原始答案中稳定切出同一段文本，原始答案 artifact 不得被覆盖。
3. 每个 block ID 必须来自本次 attempt 的真实工具结果；未知、越界或重复引用按协议失败处理。
4. 可见引用由前端或 Harness 在 span 末尾追加，事实源仍是原始答案加结构化 annotations。
5. 引用标注失败、输入超预算或输出非法时保留正文，显式记录 `citation_annotation_*` 错误并进入 pending，不得静默退化为无引用通过。

### 明确禁止

- 不允许主回答模型继续自由生成 `【来源：block_id】`，再把它当结构化 citation。
- 不允许 annotator 返回或修改答案正文。
- 不允许使用其他 attempt、重检 collection 或模型记忆补造 evidence block。
- 不允许静默截断 evidence、忽略无法解析的 span 或把 annotator 失败算作成功。
- 不允许本切片改变既有 R/X 判定项和历史手工改判记录。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| annotator 输出非法 JSON 或越界 ID | 原回答正常显示，提示“引用标注未完成” | 原回答、输入 spans 与 evidence 摘要、调用错误 | 可重跑标注 |
| annotation 输入超过上下文或请求预算 | 原回答正常显示，引用状态 pending | 预算与输入规模事实 | 可调整预算后重跑 |
| annotator 调用瞬时失败 | 原回答正常显示，引用状态 pending | 调用计数和 usage | 可在新 revision 重跑 |
| 原答案没有可用 evidence | 原回答正常显示，无自动引用 | annotation 跳过原因 | 无需恢复 |
| annotation 成功但 support judge 失败 | 引用结构与来源仍可审计 | 原始 answer、annotations、Judgment | 可人工复核或重判 |

### 人工检查点

- 编码前确认两阶段方案、正文不可变和 annotation 失败语义。
- 主路径完成后检查一次 `c5-001` 风格的同段双引用，以及一次多 block 引用。
- 完成前由负责人确认：最终回答不需要 annotator 重写，失败时的 pending 展示可接受。

### 决策预算

- 预计 B 级决策：5 项。
- 是否存在 C 级决策：否。
- 超过预算时的拆分方式：本切片只交付后端 annotation 与评测消费，前端渲染拆到 027B。

## 目标

把引用从主回答的自由文本中移出，新增一次基于完整答案和本次真实 evidence blocks 的结构化标注阶段。答案文字保持不变，annotation 作为追加事实进入 trace、流式事件和评测输入。

## 输入与输出

输入：

- 已完成的 `finish.answer`；
- 本次 attempt 所有真实工具返回的 evidence blocks；
- 稳定的答案 claim spans；
- 当前对话 provider 和预算。

输出：

- `citation_annotations[]`：`span_id/start_index/end_index/text/block_ids`；
- citation annotation 调用 usage、版本与状态；
- trace 中的 citation artifact；
- 评测 Citation Check 使用 annotation 生成明确 context，而非解析答案文本。

## 不包含

- 一次生成 `answer_blocks + citations` 的对照实现。
- Claim group 级联合支持判定的重新设计；本切片先以精确 span 降低现有 occurrence 歧义。
- 新的人工引用标注界面；沿用现有 citation_support 复核入口。
- 修改历史 attempt 或历史 inline citation 数据。

## 验收条件

- [x] 最终答案只由主回答调用生成，annotator 不得返回正文。
- [x] 同段双引用案例被拆成独立 span，引用不再跨句误归属。
- [x] 未知 block ID、空 span、越界 offset 和重复 span 被拒绝。
- [x] annotation 失败时正文保留，引用状态为 pending 而非 pass。
- [x] trace、流式事件和评测 citation check 使用同一份结构化 annotations。
- [x] 历史 `【来源：block_id】` 数据仍可读取和重判。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 引用生成采用一阶段还是两阶段 | 先生成完整答案，再由独立 citation annotator 选择 spans 和 block IDs | 负责人 2026-10-25 明确选择先试两阶段；优先保证正文连贯并建立对照基线 | 一阶段更省调用，但碎片化与引用错误难以区分；后置标注增加一次调用 | 增加一次模型调用和引用等待 | c5 等定向测试 | 已确认 |
| D2 | B | claim span 如何确定 | 由 Harness 按 Markdown 段落、列表项、表格正文行和句末标点确定性切分；跳过代码块、标题和表头；保护 inline code、列表编号和粗体闭合边界 | 引用归属应是规则而非判官猜测 | 任意字符 span 更灵活但易被模型操纵；严格句子更简单但复合列表项可能过宽 | 需要稳定的 span 算法 | `test_split_answer_spans_covers_claims_without_headings_or_code` 等覆盖中文句末、列表、表格、标题、代码块、inline code 和粗体边界 | 已确认 |
| D3 | B | annotator 输入输出协议 | 同一 chat provider，严格 JSON；只输出 `span_id + block_ids[]`，空数组表示无引用 | 与现有 JSON mode 一致，禁止正文复制 | 串行引用文本会引入碎片化；让 annotator 返回 claim 文本会制造第二事实源 | 可独立重跑引用，正文不变 | schema、重复 span、越界 ID 和 inline 标记测试 | 已确认 |
| D4 | B | annotation 失败语义 | 保留完整 answer，记录 limitation 和 pending；不改变 answer_completeness | 原有事实分层与不静默降级原则 | 整体 attempt fail 会丢失正文；静默为空会伪装引用通过 | 评测需显式 pending | 调用失败、非法输出、超预算和真实 pending 界面测试 | 已确认 |
| D5 | B | 评测量如何消费 annotation | trace 保存结构化 artifact；citation checker 优先使用 annotations，旧答案才回退解析 inline marker | 兼容新协议并保持历史可重判 | 只存 UI 文本会再次依赖解析；新建 DB 表会增加重复事实源 | 评测无需迁移旧 attempt | 结构化 claim context、空 annotation 抑制回退、pending、真实评测对齐和历史回退测试 | 已确认 |
| D6 | B | 引用详情如何呈现 | 点击角标就地打开锚定悬浮卡，展示来源标题、章节和摘要；Escape／点击外部关闭，文档详情需用户再次显式点击 | 负责人 2026-10-25 人工验收指出跳转到来源区会打断阅读顺序 | 跳转来源区会丢失阅读位置；纯 hover 对触控和键盘不友好；悬浮卡保留就地阅读并支持显式深入 | 仅改变交互呈现，不改变 annotation、来源编号或评测事实 | 悬浮卡定位、关闭与来源内容人工复验 | 已确认 |

等级：

- A：AI 可自主执行，交付时列入审计摘要；
- B：完成后集中人工确认；
- C：执行前必须停止并获得人工决定。

## 证据包

### 自动化证据

- 命令：`backend\.venv\Scripts\python.exe -m pytest backend/tests -q --basetemp backend/.runtime/pytest-all-027`
- 结果：276 个后端测试通过，退出码 0。
- 命令：`corepack pnpm backend:check`
- 结果：Ruff format／lint 和 Pyright 全部通过，0 errors。
- 命令：`corepack pnpm --dir frontend check`
- 结果：`vue-tsc --noEmit` 通过。
- 命令：`corepack pnpm --dir frontend build`
- 结果：TypeScript 与 Vite production build 通过。
- 命令：`corepack pnpm comments:check`
- 结果：注释与可读性静态门禁通过。
- 覆盖的不变量：正文不进入 annotator 输出；span 精确切回原文；只有证据白名单 block 可被引用；非法标注进入 pending；structured annotation 同时进入 trace、流式事件、非流式 API 和评测；历史 inline 引用仍可回退。

### 运行证据

- 启动方式：待负责人使用当前本地产品与评测台执行真实 N=1。
- 用户操作序列：见 [027 人工验收](../manual-tests/027-claim-aware-citation-annotation.md)。
- 预期结果：同段两个 claim 得到独立来源；点击来源定位真实证据；trace 中 annotation、prompt/output artifact 和 usage 可解释。
- 实际结果：`c5-001` 首次真实运行 `3de2e5d8ce76dc9d5d489ac76230ecd4` 的 33 个 annotation offset 和证据 ID 均合法；后续运行 `0f982875386a86c3f9480b9e07705642` 和 `2c04b586e3c478c5ac84612050d06b42` 验证了 inline code、表头跳过和表格行引用的真实界面。期间依次修复 inline code 误切、表格表头误标、有序编号伪 span、表格引用被 Markdown 丢弃、表格样式和粗体闭合边界问题。

### 失败探针

- 注入或复现的失败：连续两次 annotator 输出非法 JSON、返回未知 block ID、重复 span、上下文／请求预算超限和 provider 瞬时失败。
- 真实 trace 失败探针：`c5-001` 的 inline code 文本 `` `find . -name "*.tmp" -exec rm {} \;` `` 曾被句号切成两个 span，且 Markdown 表头被列为 table row；现分别由 inline code 保护范围和表头跳过分隔处理，`test_split_answer_spans_keeps_inline_code_inside_one_sentence` 覆盖前者。
- 真实界面失败探针：表格行 annotation 已存在于 trace，但来源标记被插到行尾 `|` 之后，被 Markdown 表格解析器丢弃；现改为写入最后一个单元格。真实答案还暴露有序列表编号被单独切成 span，以及表格缺少列边界样式；均已完成修复。
- 真实 Markdown 失败探针：粗体中的 `**1.` 和 `**整句。**` 曾在粗体内部切句；现在有序编号与句末闭合强调标记都会并入同一 claim。
- 系统保留的旧状态：完整 answer、本次真实 evidence、输入 span、模型调用次数、usage、原始 prompt／output artifact 和 pending 错误码。
- 用户看到的错误：正文继续显示，activity 和 warning 明确显示“回答完成，引用标注未完成”。

### 人工验收

- [x] 按 [027 人工验收](../manual-tests/027-claim-aware-citation-annotation.md) 完成真实产品问答、评测引用复核、历史 inline 回退和 annotation pending 探针。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么正文只能生成一次，annotation 不能返回改写后的答案？ | 正文就是答案，生成时拥有完成答案所需的最完整上下文；引用标注只是附属动作，不应产生第二份答案。 | 已通过 | 保持正文事实单源，annotation 只追加引用关系。 |
| R2 | annotation 失败时为什么保留正文并进入 pending，而不能重写或当作无引用通过？ | 产品侧正文是回答用户问题的主体，必须保留；评测侧引用是红线，annotation 失败只能 pending，由人判断。 | 已通过 | 正文与引用状态分层，禁止把附属动作失败伪装成通过。 |
| R3 | 为什么评测必须消费结构化 annotation，而不能继续从答案文本解析引用？ | 让判官重新猜测引用归属会造成二度猜测并引入更多不确定性，导致通过与不通过都带随机性。 | 已通过 | 评测直接消费 claim annotation，仅历史 trace 才解析 inline marker。 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 协作复盘

- AI 明显加速的环节：业界方案检索与讨论、设计初稿、代码实现和问题诊断。
- 负责人失去系统模型的环节：没有明显失去；在设计与方案决策阶段，不能只看 AI 总结，需要负责人自行阅读原文、形成初步判断后再与 AI 论证。本轮 AI 对产品语义的理解正确。
- 真正发现问题的控制点：人工界面验收发现悬浮引用、表格列边界等用户体验问题；真实 trace 检查发现 inline code 误切、表头误标、列表编号伪 span、表格引用丢失和粗体边界错误。
- 只有仪式感、没有产生判断价值的流程：无。
- 下一次准备调整的一到两项：面对开放设计问题，AI 提供参考方案前应先检索并对齐业界相关经验。

### 本次试行的流程规则

| 规则／假设 | 来源 | 想防住的失败 | 使用成本 | 实际效果 | 下次决定 |
| --- | --- | --- | --- | --- | --- |
| 引用先标注后判定 | 2026-10-25 设计与业界方案对照 | 判官为自由文本引用补猜归属 | 多一次模型调用 | 真实产品问答、评测 claim 对齐、历史回退和 pending 探针均完成 | 保留 |

## 已知风险与后续

- 未验证项：标注模型能否稳定选择正确 span；中文/英文混合句切分边界；多 block 联合支持。
- 接受的风险：本切片先不改写 group-level support judge，单个 span 多 block 仍沿用现有 occurrence 语义。
- 后续待办：[BL-0009 一阶段 answer-block + citation 生成对照](../backlog.md)、[BL-0010 Claim group 联合支持与引用必要性](../backlog.md)；annotation 人工复核界面在现有 citation_support 复核不足以定位问题时再进入 backlog。
