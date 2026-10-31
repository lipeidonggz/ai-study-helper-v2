# 2026-10-29 · c13-003 隔离知识语义覆盖实验

## 目标

验证修订后的 c13-003 能否稳定区分：

1. 四类核心隔离知识是否全部覆盖；
2. 四类扩展隔离知识是否至少覆盖两类；
3. 答案是否在未核验知识库全集时作排他性全集断言。

本次不再单独判定文档事实依据和“冒充文档”：前者由引用机制负责，后者无法仅凭答案和单条 criterion 可靠判定。

## 固定配置

| 项 | 值 |
| --- | --- |
| case | c13-003 |
| case revision | `cr_c13_003_b9df0ad7dab8` |
| collection snapshot | `s_6b787258ba7d700c` |
| prompt bundle | `chat-v17` |
| agent loop | `react-structured-v4` |
| tool contract | `retrieval-tools-v5` |
| model | `deepseek-flash` |
| temperature | 0 |
| N／K | 10／9 |
| judge | `judge-rubric-v4` |
| run | `run_643e5e5e9f3f43298ee9ba63a9619e7a` |
| judgment | `evalrev_a35666909a81479197a5f61363fb24cb` |

## 结果

| attempt | R1 核心四类 | R2 扩展四选二 | X2 全集边界 | 最终 |
| ---: | --- | --- | --- | --- |
| 1 | pass | pass | fail | fail |
| 2 | pass | pass | pass | pass |
| 3 | pass | pass | pass | pass |
| 4 | pass | fail | fail | fail |
| 5 | pass | fail | fail | fail |
| 6 | pass | pass | fail | fail |
| 7 | pass | fail | pass | fail |
| 8 | pass | pass | fail | fail |
| 9 | pass | pass | pass | pass |
| 10 | pass | fail | pass | fail |

汇总：

- 单样本结论：3 pass、7 fail、0 pending。
- R1：10 pass、0 fail。
- R2：6 pass、4 fail。
- X2：5 pass、5 fail。
- 5 个样本出现红线条目违规。
- 10/10 运行成功；0 基础设施失败、0 partial、0 unknown。
- 总 usage：输入 557,901 tokens、输出 45,506 tokens、68 次模型调用。

## Trace 观察

- 10 个样本都先调用 `list_documents`，看到 15 篇文档。
- 9 个样本只对《How we contain Claude across products》调用 `get_outline` 和 `list_blocks`。
- 1 个样本对该文档调用 `search_blocks` 后再用 `list_blocks` 读取全文。
- 没有任何样本实际读取第二篇文档。

因此：

- R1 10/10 说明模型能够从主要文档完整提取四类核心隔离知识。
- R2 6 pass、4 fail 说明扩展知识点覆盖仍有生成波动。
- X2 5/10 fail 与 trace 一致：Agent 普遍只查一篇，却有多数答案明确声称其它文档未涉及。
- 当前主要阻塞仍是跨文档候选扫描和覆盖验证，而不是单文档提炼质量。

## 结论

修订后的 R1 已具备较好的语义稳定性；R2 比旧的 R1/R2 重叠结构更可执行，但“上下文与记忆边界”与“外部内容进入上下文前的信任检查”仍有潜在交叉，需要人工复核。

X2 仍然稳定触发，证明它并非需要删除的重复判据，而是直接暴露了当前 Agent 在只检查一篇文档时作全集断言的行为。

## 结果文件

- `backend/.runtime/c13-003-semantic-n10-result.json`
- case revision：`cr_c13_003_b9df0ad7dab8`
- run：`run_643e5e5e9f3f43298ee9ba63a9619e7a`
- judgment：`evalrev_a35666909a81479197a5f61363fb24cb`

## 后续终止记录

曾尝试把 c13-003 题面提升为跨文档综合题（revision
`cr_c13_003_b853c2498ac9`），但沛东判断该题面不合适，在 Agent 完成
N=10、判定进行中时主动停止。该 revision 已退休；中断的判定 revision
`evalrev_7021ed925d99480dab0ff69cd3b00c6d` 状态为 `incomplete`，
不作为实验结论。

后续进一步确认：用户不会主动要求 Agent“综合不同文档、不要只依据一篇”。
因此最终只保留自然的“在当前知识库中”范围，不在题面暴露跨文档评测意图。
当前唯一 reviewed revision 为 `cr_c13_003_fff9fbc82a83`。

## 自然题面跨文档验证

### 固定现场

- case revision：`cr_c13_003_fff9fbc82a83`
- run：`run_d326c99184cb4735bfe56ac49d073ad9`
- judgment：`evalrev_b63d00ae7c354483b134d943b3e0282d`
- collection snapshot：`s_6b787258ba7d700c`
- prompt bundle：`chat-v17`
- judge：`judge-rubric-v4`

### 判定结果

- 用例：0 pass、10 fail、0 pending。
- R1 跨文档核心七组：0 pass、10 fail。
- R2 扩展四选二：9 pass、1 fail。
- X2 全集边界：10 pass、0 fail。
- 0 个 X 违规，0 个基础设施失败。
- 总 usage：输入 410,347 tokens、输出 36,941 tokens、60 次模型调用。

### Trace 结论

10 个样本全部执行：

`list_documents → get_outline → list_blocks`

每个 trace 只读取同一篇文档，没有任何样本检查第二篇文档。Agent 普遍完整覆盖了该文档中的文件、凭证、工具和外部内容边界，但缺失 worktree／并行状态、上下文与记忆生命周期、子 Agent 委派三组跨文档知识点。

这说明：

1. 自然题面没有把评测方法直接告诉 Agent；
2. 修订后的 R1 能识别最终答案中的跨文档覆盖缺口；
3. 当前失败主要来自 Agent 没有从“当前知识库中”的宽范围问题继续扩展候选文档；
4. X2 已能稳定区分“我只查了这一篇”的诚实限定与“其它文档均未涉及”的无依据排他断言。

## chat-v18 通用范围提示词试验

在 chat-v17 的自然题面结果之后，尝试把基于关键词的 coverage 规则改为
“证据范围 + 完整性责任”的通用提示词，发布 chat-v18。提示词不包含
“隔离”“worktree”“子 Agent”等用例术语。

固定现场：

- case revision：`cr_c13_003_fff9fbc82a83`
- run：`run_297cc332677943cb9c2204ae4d130be9`
- judgment：`evalrev_51181c45b6cb4a9c9f81c1efee22f8bd`
- prompt bundle：`chat-v18`

结果：

- Agent：10/10 成功，0 基础设施失败。
- R1：0 pass、10 fail。
- R2：9 pass、0 fail、1 pending。
- X2：10 pass、0 fail。
- 工具调用：30 次。
- 10/10 trace 均为 `list_documents → get_outline → list_blocks`，均只读取一篇文档。
- 多文档检索样本：0。

与 chat-v17 相比，工具序列和多文档覆盖完全一致；模型调用从 60 增至 61，
输入 token 从 410,347 增至 435,157。该提示词没有改变范围决策，因此已撤回，
生产提示词恢复为 `chat-v17`。后续应优先验证候选发现工具或 Harness 覆盖状态，
而不是继续增加同类提示词。

## chat-v19 强制结束依据试验

在 chat-v18 基础上进一步把 `finish.decision_summary` 设为协议必填，
要求正常完成和降级都说明依据，并明确列出允许降级的条件。

固定现场：

- case revision：`cr_c13_003_fff9fbc82a83`
- run：`run_a8c594adc94c42878dcffef974740d0d`
- judgment：`evalrev_e30939ea542b472e8c5c434fb575c73e`
- prompt bundle：`chat-v19`

结果：

- Agent：10 个有效样本、7 succeeded、3 failed；另有 1 次基础设施失败替换。
- R1：0 pass、10 fail。
- R2：10 pass、0 fail。
- X2：10 pass、0 fail。
- 工具调用：39 次，10 个有效 trace 仍然都只读取一篇文档。
- 模型调用：82 次；输入 814,170 tokens；输出 53,517 tokens。

成功结束的样本都提供了结束依据，但多数把“本篇文档已完整读取”解释成
“问题范围已完整覆盖”，或者直接声称其它文档不相关而未实际检查。
强制说明没有改变检索策略，只增加了 token、轮次和协议失败。

因此撤回 chat-v19、协议必填和对应提示规则；仅保留
`app.decision_summary` 作为可选观测字段。生产版本恢复为 chat-v17。
