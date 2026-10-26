# 029 · 引用标注分批与联合支持判定

- 状态：已完成
- 开始日期：2026-10-25
- 关联设计：[03 用例与金标准](../../design/03-cases-and-gold-standard.md)、[08 评测台](../../design/08-evaluation-console-design.md)、[06 检索侧与 Agent](../../design/06-retrieval-pure-agent.md)
- 前置切片：[025 引用支持分批](./025-evaluation-citation-support-batching.md)、[027 引用标注](./027-claim-aware-citation-annotation.md)、[028 失败分类拆分](./028-evaluation-check-separation.md)
- 人工验收：[029 引用评测可靠性人工验收](../manual-tests/029-citation-evaluation-reliability.md)
- 真实问题现场：`run_a36943ad6bcf4edf85f3f789e8f89e49`

## 控制契约

### 用户可观察目标

1. 同一 block 同时由检索 snippet 和完整正文返回时，评测使用完整正文，不因工具调用先后顺序降低引用判定质量。
2. 长答案的 citation annotation 自动分批；其输出预算按批次内 span 数独立推导，不受界面上的 Agent 回答预算影响；某批输出被 token 上限截断时给出明确错误。
3. 一个 claim 引用多个 block 时，按引用组判断联合覆盖，不再要求每个 block 单独覆盖整段 claim。
4. citation support 判官首次输出非法时执行一次重试；重试后仍非法才进入 pending。
5. 点击“启动执行批次”后，页面立即显示对应 run、禁用重复启动，并滚动到运行详情。

### 核心不变量

1. 同一 `block_id` 的证据必须优先采用完整 `text`，不得用先出现的截断 `snippet` 覆盖。
2. citation annotation 每批都携带完整 spans，只切分 evidence blocks；同一 span 跨批出现时合并 block 组，最终每个 span 最多一个 annotation，并按答案位置稳定排序。
3. citation annotation 的输出预算只由输出 schema 和批次 span 数推导，不继承 Agent 的 `max_output_tokens`。
4. citation support 的一个 occurrence 代表一个 claim 与一组 block；组内联合覆盖全部可识别主张才可 pass，任一必需部分无支持则 fail，无法判断则 pending。
5. 重试只追加调用事实，不改变输入、判定标准或预算；重试耗尽后保持显式 pending；历史 revision、CheckResult 和人工复核记录不被改写。

### 明确禁止

- 不把 citation annotation 的输出截断当作空结果或部分成功。
- 不因为先发现 snippet 就忽略同一 attempt 后续返回的完整正文。
- 不把多个 block 出现于同一组就自动视为通过；必须由判官确认联合覆盖。
- 不无限重试，不用模型重写答案或补充证据。

### 失败语义

| 场景 | 期望行为 |
| --- | --- |
| 同一 block 先 snippet、后完整 text | 采用完整 text |
| 单个 annotation 批次 `finish_reason=length` | 重试一次；仍发生则 `citation_annotation_output_truncated` pending |
| 某个 annotation 批次 schema 非法 | 仅重试该批次；耗尽后 pending，其他批次事实保留在审计摘要 |
| citation group 的多个 block 联合覆盖 claim | `supported` |
| citation group 只覆盖部分子主张 | `unsupported` |
| occurrence 输出遗漏、重复或 quote 不属于该组 block | citation support pending，并重试一次 |
| 重试后仍非法 | 保持 pending，记录两次 invocation |
| 启动请求成功但运行在视口外 | 计划区立即显示 run ID 与状态，并滚动到运行详情 |

### 人工检查点

- 编码前确认引用组联合覆盖语义和 annotation 分批上限。
- 主路径完成后检查 c13 型“两项证据共同覆盖一个列表 claim”和 c3 型“snippet 后跟完整正文”。
- 合并前由负责人回答 3 道反向解释问题。

### 决策预算

- 预计 B 级决策：4 项。
- C 级决策：无。
- 不拆分：四项共同决定同一条“长答案、多证据引用是否可信且可解释”的评测链。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | A | 重复 block 如何选证据正文 | 优先完整 `text`，同等完整度再优先已引用、正文更长者 | 判官只能消费真实工具事实，且不得因检索调用顺序丢失上下文 | 保留首项实现简单但会复现 c3 误判；重新检索违反 attempt 证据边界 | 只改变同一 trace 内重复 block 的选取 | snippet + full text 单元测试 | 已执行（A） |
| D2 | B | 长答案 annotation 如何避免输出截断 | 每批携带完整 224 spans，最多切 24 个 evidence blocks；`finish_reason=length` 显式映射为 `citation_annotation_output_truncated` | 当前 224 span 单批两次均在 4096 token 处截断；真实 86 block 现场按 24 块切为 4 批，预计输入与输出预留约 14.6 万 token | 只增加输出上限仍可能截断；按 span 分批会把全部 86 块重复发送四次并逼近 40 万请求预算 | 增加模型调用次数；跨批结果按 span 合并 block 组 | 多批合并、跨批顺序、截断与预算测试 | 已确认 |
| D3 | B | 一个 claim 多 block 如何判支持 | 一个 claim 与其全部 blocks 构成引用组，由判官判断联合覆盖；组结论投影为逐 block 展示 | 真实 c13/c11/c8 出现互补证据；逐 block 单独覆盖整段 claim 会产生假失败 | 继续逐 block 会拒绝联合证据；自动并集 pass 又可能掩盖真正的错误引用 | citation support payload 和提示词版本变化 | 联合覆盖、部分覆盖、错误 quote 和旧 payload 测试 | 已确认 |
| D4 | B | citation judge 协议错误如何处理 | 非法 JSON、occurrence 集合和 quote 校验失败时在同一批次内重试一次 | c6/c7/cf-001 都是可重试的模型输出不稳定，而非证据缺失 | 直接 pending 增加人工负担；无限重试会放大成本 | 增加最多一次模型调用，保留两次 invocation | 首次非法后成功、连续非法后 pending 测试 | 已确认 |
| D5 | B | citation annotation 输出预算是否继承 Agent 设置 | 完全解耦；按 `min(4096, 512 + span_count × 128)` 推导批次预算 | 输出 JSON 的基数由 span 数决定，与用户回答长度预算无关 | 继承全局值会令 512 token 的截断探针意外压坏 annotation；固定 4096 又忽略小批次 | annotation 调用参数、trace 与预算预留独立于 Agent `max_output_tokens` | 2-span 与 224-span 的输出预算测试、Agent 4096 设置下 annotation 独立预算测试 | 已确认 |

等级：

- A：AI 可自主执行，交付时列入审计摘要；
- B：完成后集中人工确认；
- C：执行前必须停止并获得人工决定。

## 验收条件

- [x] 同一 block 的 snippet 不再覆盖完整正文。
- [x] 长答案 citation annotation 按 evidence block 分批并合并为完整有序结果。
- [x] 输出因长度结束时可明确诊断，不与普通非法 JSON 混淆。
- [x] citation support 以 claim 引用组为单位判断联合覆盖。
- [x] citation judge 对协议非法结果最多重试一次，耗尽后保持 pending。
- [x] 定向测试、完整后端测试和项目质量门通过。

## 证据包

### 真实现场

- Run：`run_a36943ad6bcf4edf85f3f789e8f89e49`
- `c3-001` 的 block `...b00002` 原判只看到 165 字检索 snippet；当前证据提取恢复为后续 `list_blocks` 返回的 1387 字完整正文。
- `c13-001` 的 `annotation_0008` 当前形成 `b00007 + b00008` 联合引用组，不再拆成两个各自覆盖整段列表的 occurrence。
- `cf-003` 有 224 个 answer spans、86 个 evidence blocks；按每批 24 个 evidence blocks 切为 `24+24+24+14` 四批。
- `cf-003` 四批估算输入共 129,541 token，加输出预留共 145,925 token；叠加生成阶段已用 213,593 token，合计 359,518 token，低于 400,000 请求预算。
- citation annotation 输出预算按 span 数独立计算：1 span 为 640 token，224 spans 封顶 4,096 token；不会继承 Agent 界面里设置的 512 或 8,192 token。
- 截断探针 run `run_4044c38044d44ea2810ae40aa09f721a` 使用 `max_output_tokens=2048`：正文 3,836 字符并以 `answer_output_truncated` 标记 partial；citation annotation 仍以独立 4,096 token 上限完成 100 spans／86 blocks 的 4 批标注。
- 该 run 的判定 revision `evalrev_e970b926662546e497f4e3953bbf6063` 已完成：R1 截断事实与 R2 截断声明均 pass；citation integrity pass；citation support 以 `citation-context-group-v1` 分 17 批，84 个 claim 引用组全部 supported、无 pending／preflight failure。
- 同一次启动曾因成功提示和执行面板都在当前视口之外而看起来“没有反应”；真实 run 已完成，现已增加计划内 run 状态、重复启动保护和自动滚动。
- `c6-001` 的非法 JSON、`c7-001` 的章节标题伪 quote、`cf-001` 的跨 block 错 quote 都由同一有限重试路径覆盖。

### 自动化证据

- `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_citation_annotation.py backend/tests/test_evaluation_judgment.py backend/tests/test_evaluation_api.py backend/tests/test_agent_runtime.py -q --basetemp backend\.runtime\pytest-029-main`：退出码 0。
- `backend\.venv\Scripts\python.exe -m pytest backend/tests -q --basetemp .t29-decoupled`：完整后端测试退出码 0。
- `corepack pnpm quality:quick`：注释检查、Ruff、Pyright、Vue TypeScript、Vite production build、Rust fmt／check／clippy 全部通过。
- `test_long_answer_annotations_are_batched_and_merged_in_answer_order` 使用与 cf-003 相同的 224 spans／86 blocks 规模及近似的 213,593 已消费 token，验证四批完成与跨批合并。
- `test_agent_api_uses_saved_runtime_settings` 验证 Agent 保存设置后，主回答调用使用 4,096 token 配置，而单 span annotation 使用独立推导的 640 token。
- `test_citation_support_judges_one_claim_with_multiple_blocks_as_a_group` 验证双 block claim 只产生一个 occurrence，并投影回两个 block。
- `test_citation_support_retries_invalid_output_once_then_passes` 与 `test_citation_support_retries_invalid_quote_once_then_passes` 验证非法 JSON／quote 的有限重试和调用审计。

### 失败探针

- snippet 先出现、完整正文后出现：最终判官输入必须采用完整正文。
- annotation `finish_reason=length`：错误码必须是 `citation_annotation_output_truncated`，不能降级为部分 annotation。
- 组内两个 block 互补：claim 组可 pass，逐 block 投影保留同一组结论。
- 组内证据只覆盖部分子主张：claim 组仍 fail。
- citation judge 首次非法、第二次合法：CheckResult pass，两个 invocation 都保留。
- citation judge 两次非法：CheckResult pending，不伪造 pass。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么同一 block 的完整正文必须覆盖先出现的 snippet？ | snippet 不完整，只适合展示局部命中；判官若拿它核验 claim，会把仅被截掉的上下文误判为不支持，因此必须使用同一 block 的完整正文。 | 已通过 | `text` 优先于 `snippet`，同等完整度再比较引用状态和长度。 |
| R2 | 为什么引用支持应按 claim 的 block 组判断，而不是逐 block 独立判断？ | 一个 claim 可能需要多个 block 才能完整支持；联合覆盖解决逐块误判，但配了多个 block 不代表自动通过，判官仍须确认这些 block 合起来能支撑 claim。 | 已通过 | occurrence 表示 claim 引用组，结论由联合覆盖决定，并投影到逐 block 视图。 |
| R3 | 为什么 citation judge 的非法输出可以重试，但 annotation 输出截断不能当成部分成功？ | 判官输入完整，非法输出只是模型一次不稳定，可以让模型重新生成；annotation 截断意味着结果数据本身不完整，未输出部分可能包含不支持判定，不能当作有效部分结果。 | 已通过 | 判官对协议错误最多重试一次；annotation 截断显式 pending，不吸收部分输出。 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 已知风险与后续

- 联合覆盖暂不评价每个 block 是否必要，冗余引用仍留给 `BL-0010` 后续增强。
- annotation 分批会增加长答案的模型调用次数，需要真实现场观察总 token 与延迟。
- 本轮不修改 `cf-003` 用例预算假设，也不处理 c15 类真实内容过度推断。
