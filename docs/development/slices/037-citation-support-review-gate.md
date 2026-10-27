# 037 · 引用语义支持候选与人工复核门禁

- 状态：待人工验收
- 开始日期：2026-10-26
- 关联设计：[03 用例与金标准](../design/03-cases-and-gold-standard.md)、[08 评测台设计](../design/08-evaluation-console-design.md)、[ADR 0013](../../decisions/0013-semantic-citation-support-review-gate.md)
- 前置切片：[025 引用支持分批检查](./025-evaluation-citation-support-batching.md)、[029 引用评测可靠性](./029-citation-evaluation-reliability.md)、[036 引用上下文与判官职责拆分](./036-citation-context-and-judge-separation.md)
- 对外说明：[11 RAG 可信引用评测纠偏](../../publishing/11-rag-citation-evaluation-redline.md)

## 控制契约

### 用户可观察目标

1. 引用完整性问题继续作为确定性硬红线直接失败。
2. 单次语义判官的 `unsupported` 生成“语义红线候选”：attempt 显示为未确认的普通失败，检查、case 和 revision 仍待复核，且不计为红线。
3. 人工按 claim 复核；一个 claim 的多个 block 作为联合证据一次性裁决。人工确认 `unsupported` 后才进入语义红线，人工驳回后可以恢复通过。
4. 关系判官退出默认评测链路；主语义判官继续理解完整 claim 的关系，但关系诊断结果不再自动阻断。

### 核心不变量

1. `citation_integrity=fail` 不可由人工覆盖，仍直接阻断 attempt。
2. LLM `unsupported` 可产生未确认的普通 attempt 失败，但检查、case 和 revision 必须保持待复核且红线为零；`unknown` 和基础设施失败保持 pending。
3. 人工提交必须覆盖检查中的全部 occurrence，以便重算整体结论；服务端逐项比较当前有效结果，只把实际变化的 claim 追加为人工复核事实。
4. 人工确认任一 claim 为 `unsupported` 时，有效 `citation_support` 为 fail；全部 supported 时为 pass；存在 unknown 时为 pending。
5. pending 不计入 pass；旧 CheckResult、trace 和原答案不被改写。

### 明确禁止

- 不把单次 LLM judge 的 verdict 直接升级为绝对红线。
- 不把 pending 自动折算为 pass 或 fail。
- 不用逐 block 结论替代 claim 级联合支持结论。
- 不在默认链路中调用 claim 角色、evidence 角色或关系比较判官。
- 不为具体数字、句式、列表或历史 case 增加提示词补丁。
- 不修改或在原地覆盖历史 revision、CheckResult 和人工复核事实。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| citation integrity fail | 引用完整性失败，attempt 失败 | 具体缺失 block、span 和 quote 事实 | 修复 Agent／annotation 后重跑 |
| judge 返回 unsupported | attempt 显示普通失败但标记待复核，case 不能通过 | claim、联合 block、judge reason、逐字证据和红线零状态 | 是；人工确认、驳回或保持争议 |
| judge 返回 unknown | 引用支持待定 | 不确定原因和 claim 证据组 | 是；人工复核或重判 |
| judge 输出、provider、预算失败 | 引用支持待定 | invocation、错误码和输入摘要 | 是；修复后新 revision |
| 人工确认 unsupported | citation_support 失败，attempt 失败 | 原 LLM 结果和人工复核双份事实 | 是；可追加新复核 |
| 人工全部 supported | 检查通过 | 旧 pending 结果和人工复核双份事实 | 是；可追加新复核 |
| 人工仍有 unknown | 检查继续 pending | 复核理由和未知 claim | 是；需要进一步裁决 |

### 人工检查点

- 编码前已确认的契约：用户认可对外文中的“确定性红线、语义候选、人工复核、关系诊断”分层。
- 实现中途检查：先看自动 `unsupported → pending`，再看人工确认／驳回后的 attempt 聚合翻转。
- 完成后人工操作：用一个真实待复核 claim 确认复核内容能同时看到 claim、多个 block、judge verdict 和理由。

### 决策预算

- 预计 B 级决策：4 项。
- C 级决策：无。
- 若出现额外数据库迁移或历史数据不可回放，再拆分独立切片。

## 目标

把引用语义支持从“单次 LLM 判决定生死”改为“LLM 生成可审计候选，人工确认后形成有效门禁”，同时把关系判官从默认调用链降级为可选诊断能力。

## 输入与输出

- 输入：冻结答案、claim context、本次真实 evidence blocks、citation annotation、`citation_integrity` 检查结果。
- 自动输出：单一 `citation_support` 判官结果；状态为 pass 或 pending，pending 带明确错误码和 candidate。
- 人工输出：提交当前完整 claim 快照；服务端只保存与当前有效结果不同的 claim delta，并据此重算 pass、fail 或 pending。
- 聚合输出：确定性完整性失败优先；人工确认的语义 fail 参与红线；pending 阻塞通过资格。

## 不包含

- 不重新调整 Agent 生成提示词。
- 不重跑 Agent；先用冻结 run 追加 judgment revision。
- 不实现关系 advisory 的新交互入口。
- 不改变 citation annotation、claim spans 或证据快照协议。
- 不新增多 reviewer 投票或自动争议裁决。

## 验收条件

- [x] `unsupported` 自动结果把 attempt 投影为未确认普通失败，检查保持 pending + `citation_support_review_required`。
- [x] `unknown` 仍为 pending，基础设施失败不伪装成语义 fail。
- [x] 多 batch 中任一语义候选不能让其他 pass batch 掩盖。
- [x] 人工复核按 claim 和联合 block 组校验，不能遗漏或增加 occurrence。
- [x] 人工确认 fail 后 attempt 失败；人工驳回后可以恢复 pass；unknown 保持 pending。
- [x] 默认 citation support 每个 batch 只调用主语义判官，不调用关系三阶段判官。
- [x] `citation_integrity` fail 仍不可人工覆盖。
- [x] 固定 N=10 run 只追加新 revision，不重跑 Agent。
- [x] `quality:quick` 与完整后端测试通过。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 单次语义 unsupported 的聚合语义 | attempt 投影为未确认普通失败；check、case 和 revision 保持待复核；severity 为 quality，不进入红线 | 语义支持受自然语言歧义和 judge 抖动影响；结果质量、复核状态和严重程度必须分开 | 旧实现直接 fail 会耦合红线；一律 pending 又会丢失“普通失败候选”的可读信号 | AttemptAggregate、CaseAggregate、CheckResult result、error_code 和 UI 文案 | 自动 unsupported、unknown、batch 覆盖及人工翻转测试 | 已确认 |
| D2 | B | 人工复核粒度与差异记录 | 按 occurrence／claim 复核，联合 block 列表作为一个证据组；提交完整快照，只持久化和展示实际变化的 claim delta | 一个 claim 可能需要多个 block 联合支持；全量快照用于重算，但未变化的 claim 不属于人工改判 | 逐 block 无法表达联合覆盖；把整份快照都记成人工复核会错误放大人审范围 | CheckReviewRequest、复核 payload 与 UI | 多 block claim、occurrence 集合、delta 持久化及人工翻转测试 | 已确认 |
| D3 | B | 关系判官的默认地位 | 不进入默认门禁，保留代码作为可选诊断能力；接受主判官对显式角色反转的稳定假阴性风险 | 主判官已知错误 20/20 稳定漏判；关系候选器在同一 run 的 38 个关系型 claim 上产生 5 个候选，其中仅 1 个为已知错误 | 完全关闭会保留稳定漏检；恢复默认关系链路会为 1 个目标错误增加约 4 个额外人工候选；重复主判官投票不能修复稳定偏差 | 默认调用次数、候选工作量、token 用量和 check payload | 主判官 20 次重复、关系候选器全量固定 packet 探针 | 已确认 |
| D4 | B | 人工复核后的 revision 状态 | 重新计算有效 attempt 聚合；没有 pending 时恢复 complete | 现有 append-only 复核链路已能参与聚合，但需要扩展到 claim 级 | 另建最终裁决表会增加第二事实源；原地更新旧检查会破坏审计 | revision 状态、case 聚合与 baseline 资格 | 确认、驳回、unknown 三条路径 | 已确认 |

## 证据包

### 自动化证据

- `backend\.venv\Scripts\python.exe -m pytest backend/tests -q --basetemp .pytest-t37-full-root`：完整后端测试通过。
- `corepack pnpm quality:quick`：注释检查、Ruff、Pyright、Vue TypeScript、Vite build、Rust fmt／check／clippy 全部通过。
- 覆盖的不变量：自动 pending、人工确认／驳回／争议、多 block 联合、关系诊断默认关闭和 integrity 不可覆盖。
- `test_human_review_persists_only_changed_claims` 验证：提交完整 claim 快照时，只有 verdict／reason 实际变化的 claim 进入人工复核 delta；未变化 claim、block 结构和证据引文继续归属原判官结果。

### 运行证据

- 固定 run：`run_a33c5d27b0b845d5833ea4dac701ffb2`
- 启动方式：只追加新 judgment revision。
- 最终实验 revision：`evalrev_7e351c4638af4f46a1da3ed06fa0bc30`
- 实际结果：60/60 criterion pass，citation integrity 10/10 pass，citation support 10/10 pass，semantic candidate 0。
- 关键限制：已知 `attempt_80cd96f68bcd432aa6d66824d14c6cf9` 角色反转在完整冻结 packet 中重复 5 次仍被判 supported。该 10/10 只证明未自动误杀，不证明候选召回充分。
- 详细实验：[2026-10-26 引用语义支持候选与人工复核门禁](../experiments/2026-10-26-citation-support-review-gate.md)
- 判官稳定性补充：主判官对已知角色反转稳定 20/20 supported；现有关系候选器在 38 个关系型 claim 上产生 5 个候选，除已知错误外仍有 4 个待人工排除。

### 失败探针

- 注入场景：主判官返回 unsupported、unknown、非法输出、provider 失败和 batch 部分失败。
- 系统保留：原答案、trace、evidence snapshot、全部 invocation 和历史 revision。
- 用户看到：attempt 普通失败、红线为零、明确的待复核原因与可复核 claim 证据组。
- 真实失败探针：完整 relation-aware packet 的 5/5 假阴性证明当前主判官不能替代关系候选召回；不得据此把 10/10 视为质量门禁已闭合。

### 人工验收

- [ ] 在评测台选择一个 pending claim，确认页面同时展示 claim context、联合 block 和判官理由。
- [ ] 人工驳回一个候选后，确认 effective support 可变为 pass。
- [ ] 人工确认一个候选后，确认 attempt 与 case 红线聚合变为 fail。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 主路径完成后，LLM unsupported 与人工确认 fail 分别是什么状态？ | 自动 unsupported 时 CheckResult pending、attempt 未确认普通 fail、case pending、revision incomplete、无红线；人工确认后生效检查、attempt 与 case 均为 fail，revision complete，触发红线。 | 已通过 | 状态回答正确，保留源 CheckResult 与人工复核分层。 |
| R2 | 人工复核驳回候选后，旧 CheckResult 和 trace 会怎样保留？ | 原 CheckResult、invocation、trace 和答案保留并追加人工复核；生效引用支持为 pass；该 attempt 在其他判据也通过时恢复 pass，case/revision 重新聚合，不会自动成为基线。 | 已通过 | 明确区分 attempt 自身聚合与 case/revision 的 N/K 聚合。 |
| R3 | 为什么关系判官不再默认调用，却不能把它当作不存在的质量问题？ | 关系判官会引入不可忽视的假阳性并增加人工复核量；主判官对已知收益／代价反转稳定 20/20 判 supported，说明角色保真风险仍真实存在。 | 已通过 | 当前以关系判官默认关闭作为成本取舍，并由 BL-0011 跟踪主判官稳定假阴性。 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 协作复盘

- AI 明显加速的环节：待完成后复盘。
- 负责人失去系统模型的环节：待完成后复盘。
- 真正发现问题的控制点：固定答案只改判官的对照实验。
- 只有仪式感、没有产生判断价值的流程：待完成后复盘。
- 下一次准备调整的一到两项：待完成后复盘。

## 已知风险与后续

- 未验证项：真实 DeepSeek 判官在新门禁下的重复运行分布。
- 接受的风险：未被人工处理的语义候选会阻塞 baseline 完成资格，这比自动判死更慢，但能保留可解释性。
- 已接受风险：主语义判官对显式角色反转存在稳定假阴性。`citation-rubric-v11` 对已知错误完整 packet 重复 20 次全部 supported；当前 revision 即使 10/10 pass 也不能晋级质量基线。
- 当前缓解：关系三阶段判官保留为可选诊断，不进入默认门禁；该风险由 [BL-0011](../backlog.md) 跟踪，后续必须用平衡探针评估候选召回与人工负担。
- 后续待办：[BL-0011](../backlog.md) 显式语义角色保真候选探测；多人复核一致性和语义候选质量指标。
