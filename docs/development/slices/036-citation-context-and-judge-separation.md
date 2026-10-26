# 036 · 引用上下文包与判官职责拆分

- 状态：已完成
- 开始日期：2026-10-26
- 关联设计：[03 用例与金标准](../design/03-cases-and-gold-standard.md)、[06 检索侧与 Agent](../design/06-retrieval-pure-agent.md)
- 前置切片：[031 引用语义校准](./031-citation-semantic-calibration.md)、[035 Agent 因果与权衡保真](./035-agent-causal-relation-fidelity.md)
- 暂停基线：035 中的 `chat-v7` Agent 提示词保持冻结，本切片 Completion 前不继续调整 Agent。

## 控制契约

### 用户可观察目标

1. 列表项作为一个完整 claim，不再按项内分号拆分。
2. 列表项、表格行和普通句子获得由 Harness 确定的完整结构上下文，不依赖模型自行选择上下文。
3. 主引用判官只判断 focus claim 是否被证据支持；关系判官独立判断目标框架、语义角色和方向。
4. 无法确定关系方向时返回 pending，不能把不确定性算成 pass。

### 核心不变量

1. `focus_text` 是唯一判定对象；`context_fragments` 只用于解释，不能作为证据。
2. 一个列表项是一个 claim；多个列表项不合并为同一个 occurrence。
3. 一个 claim 仍可由多个 evidence block 联合支持。
4. 关系判官对主判官判为 `supported` 的冻结 claim 执行，不依赖关键词触发。
5. 历史 artifact、CheckResult、Judgment 和 revision 不改写；新结果只追加新 revision。

### 明确禁止

- 不把整个列表作为一个 claim。
- 不让 annotation LLM 自由选择或改写上下文。
- 不把 context 中的其他正确内容算作 focus claim 的支持证据。
- 不为具体 c2 指标、列表项或答案原句增加特判。
- 在判官未完成校准前继续调 Agent prompt。

### 失败语义

| 场景 | 期望行为 |
| --- | --- |
| 列表项自包含 | 只核验完整列表项 |
| 列表项依赖引导段或相邻项 | Harness 提供完整列表容器，只核验 focus |
| 表格行依赖表头 | Harness 提供完整表格容器 |
| 关系不存在 | `relation_present=false`，维持主判官 verdict |
| 关系方向保持 | 维持 supported |
| 关系方向反转 | 改为 unsupported |
| 目标或角色无法可靠判断 | 改为 unknown，最终 pending |
| context 不能逐字回溯答案 | 整个 citation check pending |

### 决策预算

- 预计 B 级决策：4 项。
- C 级决策：无。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 列表项 claim 粒度 | 一个列表项作为一个完整 claim，内部不再按分号拆分 | 当前失败来自列表项内部角色丢失；分号级拆分会让关系判官失去完整命题 | 整列表合并会混合多个指标并降低定位能力 | `claim-spans-v3` 与 annotation prompt v3 | 分号列表项单测、现有 50 attempt 重判 | 已确认 |
| D2 | B | 上下文如何提供 | 由 Harness 提供完整段落／列表／表格容器；annotation LLM 不选择 context | 若让模型判断是否需要上下文，会再次引入不可审计的语义漏选 | 只附相邻项仍可能漏指代；整容器有噪声但可通过 focus 规则隔离 | citation artifact 保存精确 context fragments | 列表、表格和 context 回溯单测 | 已确认 |
| D3 | B | 判官职责如何拆分 | 主判官只判支持；claim 与 evidence 分别在不看对方的情况下提取角色；最终比较器只比较两份结构化角色，并用明确冲突／未匹配角色和数字角色守卫收敛 | 单次关系判官同时看到 evidence 时会反向改写 claim 标签；evidence 角色也会被 claim 诱导；unmatched 角色对象和字符串混用会造成协议失败 | 继续扩写单判官会造成规则竞争；让关系判官读取主判官理由会继承错误；多个窄任务各自输入独立 | `citation-rubric-v9`、`citation-claim-roles-v1`、`citation-evidence-roles-v3`、`citation-relation-v8`、checker version 11 | 支持、方向保持、方向反转和 unknown 定向测试 | 已确认 |
| D4 | B | 关系不确定时如何收敛 | `direction_preserved=null` 转 unknown，最终 pending，不猜 pass/fail | 引用是红线，未知结果需要人工复核 | 直接 pass 会漏红线；直接 fail 会把判官不确定性伪装成产品错误 | 新增 `citation_relation_review_failed` 与关系 unknown 语义 | pending 定向测试、历史 attempt 重判 | 已确认 |

## 验收条件

- [x] 列表项内部保持为单一 claim，表格行与句子规则未被破坏。
- [x] 新 annotation artifact 保存可逐字回溯的 context fragments。
- [x] 旧 citation artifact 仍可按历史语义读取。
- [x] 主判官输出不再包含 `relation_check`。
- [x] 所有 supported claim 都经过独立关系判官。
- [x] 关系方向保持、反转和 unknown 三种路径均通过定向测试。
- [x] 现有 50 个冻结 attempt 使用新判官重判，所有 verdict 变化逐条复核。
- [x] 定向上下文与关系校准集通过。
- [x] `quality:quick` 与完整后端测试通过。

## 已冻结的 v8 实验基线

以下结果只作为 `citation-rubric-v8` 的实验证据，不作为最终基线：

| Run | Revision | Citation support |
| --- | --- | --- |
| `run_87c6db3d4bb44ca18770ff4bcd61a872` | `evalrev_a254257b919c4c1987c7046fc18dede3` | 10 pass |
| `run_b971bfe633c74e31a5caf04274d6be37` | `evalrev_e526589a7b6c46b996549ce8ca5ed9a6` | 9 pass, 1 fail |
| `run_6b5b68269c8945809a61a31df960a144` | `evalrev_0b5c3e7fab0f478c89530eb9b435ec00` | 9 pass, 1 fail |
| `run_31281787a0674cd6a4896d74b7febddb` | `evalrev_0855e4f519ba475489a6c192357e13cc` | 10 pass |
| `run_e9f8b52836c446ffb2fe336d9ec282ec` | `evalrev_7447ac0d9aff4e298d5ce7f459c05ae6` | 10 pass |

- 50/50 product、finish、complete trace。
- 300/300 criterion judgments pass。
- 50/50 citation integrity pass。
- 48/50 citation support pass。
- 两个 failure 分别来自 `attempt_1bef16a70d5f4e0bb651be887f30c358` 和 `attempt_26797bd4357243e4bfd17a0d61a62b5b`。
- 两个 failure 都是同一列表中把 0.4% 误拦标成“代价端”，与证据声明“降低摩擦为收益、漏过风险为代价”的目标框架冲突。

## 证据包

### v9 / checker 11 冻结回归

| Run | Revision | Citation support |
| --- | --- | --- |
| `run_87c6db3d4bb44ca18770ff4bcd61a872` | `evalrev_ca21db75aa3c49f69a420c1cdd79f605` | 7 pass, 3 pending |
| `run_b971bfe633c74e31a5caf04274d6be37` | `evalrev_e4af9cef221344e8aef73a43ef34c7da` | 6 pass, 3 fail, 1 pending |
| `run_6b5b68269c8945809a61a31df960a144` | `evalrev_3a1dfff8c4024939b750aa3e08ee14ee` | 8 pass, 2 fail |
| `run_31281787a0674cd6a4896d74b7febddb` | `evalrev_f2b715f3bfe846389f6d590765b63629` | 6 pass, 3 fail, 1 pending |
| `run_e9f8b52836c446ffb2fe336d9ec282ec` | `evalrev_15e06f1c899e473995ce60786c358d04` | 6 pass, 1 fail, 3 pending |

- 50/50 product 样本没有重跑 Agent；只追加新 judgment revision。
- 163 个 claim 中，144 supported、9 unsupported、10 unknown。
- 50/50 citation integrity pass，300/300 criterion judgments pass。
- 所有协议失败为 0；pending 均来自显式 unknown，而非 schema 或 provider 失败。
- 9 个 unsupported 都是把 0.4% 良性命令误拦从 evidence 的 benefit 端改写成 cost 端，属于真实关系反转。
- 10 个 unknown 中，9 个是答案新增“因此”因果桥而 evidence 只联合支持两侧事实；另一个是同类 0.4% 角色混写，但比较器没有形成明确冲突。两类均保持 pending，未算 pass。
- 每轮 judge 执行为 100 次调用，总 token 约 8.9 万–9.7 万；这是评测侧额外调用，不进入产品运行时。

### 定向关系校准

使用当前版本化 prompt、同一 DeepSeek Flash 配置，每个案例重复 5 次：

| 用例 | 期望 | 结果 |
| --- | --- | --- |
| 收益／代价角色反转 | false | 5/5 false |
| 混合角色列表项 | false | 5/5 false |
| 保真收益／代价转述 | true | 5/5 true |
| 因果方向保持 | true | 5/5 true |
| 因果方向反转 | false | 5/5 false |
| 条件方向保持 | true | 5/5 true |
| 条件方向反转 | false | 5/5 false |
| 增减方向保持 | true | 5/5 true |
| 增减方向反转 | false | 5/5 false |
| 非关系型 claim | relation_present=false | 5/5 |

### 自动化验证

- `backend\.venv\Scripts\python.exe -m pytest backend/tests -q --basetemp .pytest-t36-final5`：通过。
- `corepack pnpm quality:quick`：注释、Ruff、Pyright、Vue TypeScript、Vite build、Rust fmt／check／clippy 全部通过。
- `test_list_annotation_attaches_complete_list_context_without_model_selection` 验证列表项整项成 claim 且上下文由 Harness 固定提供。
- `test_table_row_annotation_attaches_table_header_and_complete_table` 验证表格行上下文。
- `test_claim_role_normalization_drops_low_information_other_relation` 验证 all-other 不形成关系。
- `test_numeric_role_guard_rejects_same_metric_in_opposite_roles` 验证同一指标相反角色的确定性守卫。
- `test_inconsistent_relation_output_is_narrowed_to_unknown` 验证协议不一致只缩小为单 claim pending。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么 claim 角色必须在不读取 evidence 时独立提取？ | claim 来自答案，只有只读答案才能忠实记录答案实际写出的角色；混入 evidence 会把原文的正确角色带进来，诱导关系判官按证据反向改写 claim 的提取结果。 | 已通过 | 三个角色阶段保持输入隔离。 |
| R2 | 为什么整个列表作为 context，但不能作为同一个 claim？ | 整个列表通常包含多个原子事实；原子事实越多，判官越可能忽略其中局部错误而给出整体 pass，同时也无法定位具体失败项。 | 已通过 | 列表只作为固定 context，每个列表项独立作为 focus claim。 |
| R3 | 为什么关系结果“无法比较”必须 pending，而不能自动判 pass 或 fail？ | 无法比较表示现有信息不足以作出可靠结论，必须引入人工二次裁决；pending 用于保留该状态，不能伪装成通过或失败。 | 已通过 | 关系 unknown 保持 pending，进入人工复核流程。 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 已知风险与后续

- 完整列表或表格上下文会增加 judge token；超预算时必须 pending，不能静默截断。
- 历史 inline citation 没有冻结 annotation focus 时，不执行关系判官，避免把不完整 fallback 文本伪装成精确 claim。
- 新判官通过后，再恢复 035 的 Agent 调优；`chat-v7` 先生成新 attempt，验证 `claim-spans-v3` 真实链路，并保持本切片 judge/checker 版本冻结。
