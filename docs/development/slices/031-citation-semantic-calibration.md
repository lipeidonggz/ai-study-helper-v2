# 031 · 引用语义同一性与判官稳定性校准

- 状态：已完成
- 开始日期：2026-10-26
- 关联设计：[03 用例与金标准](../../design/03-cases-and-gold-standard.md)、[08 评测台](../../design/08-evaluation-console-design.md)
- 前置切片：[027 引用标注](./027-claim-aware-citation-annotation.md)、[029 引用评测可靠性](./029-citation-evaluation-reliability.md)、[030 Agent 强度保真（已否决）](./030-agent-claim-strength-fidelity.md)
- 发现现场：`run_619f5e25271b4a9a99ac115be6c6879b`、`run_9f57d53f585b445ea61f0b6ea720b9ed`
- 人工验收：[031 引用语义与 annotation 校准人工验收](../manual-tests/031-citation-semantic-calibration.md)

## 控制契约

### 用户可观察目标

1. citation judge 接受保留核心命题、范围限定和语义强度的中文近义转述，不要求逐词对应。
2. citation judge 只有在 claim 删除限制条件或提高语义强度时才判 unsupported。
3. 相同 claim 与 evidence 的 verdict 不因同批其他 occurrence 而改变。
4. annotation 不再把“提到过。”这类无独立、可核验主张的碎片当作 claim。

### 核心不变量

1. 近义判断采用“核心命题＋范围＋强度”，不采用关键词或逐词匹配。
2. `good question, but broader scope` 的保真中文转述可 pass；删除 broader scope 或声称完整正确则 fail。
3. calibration 样本用于测试判官，不写入生产提示词；生产提示词不包含具体 case ID 或答案原句。
4. 正式 redline 语义不变：任何真实 unsupported claim 仍使 attempt fail；无法判断仍 pending。
5. 历史 revision、Judge invocation 和 CheckResult 不改写。

### 明确禁止

- 不为单个 case 增加特判词或答案字符串。
- 不把 citation support pending 自动算 pass。
- 不用新 judge 重写历史 revision。
- 不让 annotation 为了覆盖率而标注无独立语义的碎片。

### 失败语义

| 场景 | 期望行为 |
| --- | --- |
| 同义转述保留核心命题、范围和强度 | supported |
| claim 删除 but／however 的限制 | unsupported |
| claim 把合理/好问题提升为完整正确 | unsupported |
| 同一固定 packet 重复判定结果不一致 | 记录稳定性失败，不进入正式基线 |
| annotation 只包含确认词或纯过渡语 | 不生成 annotation |
| 无法确定 claim 语义 | pending，不猜 pass |

### 决策预算

- 预计 B 级决策：3 项。
- C 级决策：无。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | citation judge 如何识别保真近义转述 | 增加通用“核心命题、范围、强度一致即可 supported”规则，升级为 `citation-rubric-v7` | 030 人工复核确认中文近义表达被误判 | 继续逐词对照会放大中文 paraphrase 假失败；放宽所有差异会漏过真实增强 | judge prompt/checker 版本变化 | 单 occurrence 正例 10/10、反例 3/3；真实 c1 v7 revision 10/10 pass | 已确认 |
| D2 | B | batch 是否影响 occurrence verdict | 固定真实 packet 重复测量；v7 下三个多 occurrence packet 均 10/10 稳定，本轮不拆 occurrence | 相同目标 claim 在 v6 下出现 7 pass／3 fail 和 0 pass／10 fail，完整批次输入已固定 | 直接拆分所有 occurrence 成本高；“一次通过”不足以证明稳定 | 保持当前 batch 策略，不增加 judge 请求数 | 三个真实 packet 各重复 10 次，目标 claim 30/30 supported | 已确认 |
| D3 | B | annotation 如何排除无独立语义片段 | 增加通用 claim 完整性规则并升级 claim annotation prompt 版本 | “提到过。”被标注后产生无意义 pending | 用长度阈值会误伤合法短答案；完全依赖 judge 又会使 revision incomplete | annotation prompt 版本变化 | 单测覆盖低信息短语、完整短句和表格行；真实 c1 N=10 低信息 annotation 为 0 | 已确认 |

## 验收条件

- [x] citation judge 接受保留限制范围的近义转述。
- [x] citation judge 拒绝删除限制或增强语义的 claim。
- [x] 固定 packet 重复实验完成并给出稳定性结论。
- [x] annotation 不再产生低信息确认短语。
- [x] 真实 c1 重判结果完成，不用历史 revision 覆盖。
- [x] 完整质量门通过。

## 证据包

完整实验配置与逐批结果见
[2026-10-26 citation-rubric-v7 校准](../experiments/2026-10-26-citation-rubric-v7-calibration.md)。

### 自动化证据

- `corepack pnpm quality:quick`：注释、Ruff、Pyright、Vue TypeScript、Vite build、Rust fmt／check／clippy 全部通过。
- `backend\.venv\Scripts\python.exe -m pytest backend/tests -q --basetemp backend\.runtime\pytest-031-full`：完整后端测试通过。
- `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_citation_annotation.py backend/tests/test_evaluation_judgment.py backend/tests/test_evaluation_api.py backend/tests/test_retrieval_read_layer.py -q --basetemp .t31-v7-tests`：退出码 0。
- `test_citation_prompt_uses_semantic_equivalence_and_isolates_occurrences` 验证 v7 的通用近义规则、语义增强边界、occurrence 隔离和 prompt 不含 c1 特判。
- `test_annotation_prompt_rejects_underinformative_claim_fragments` 验证 annotation prompt 拒绝单独确认词、承接语、标题和过渡句。
- `test_citation_support_judges_one_claim_with_multiple_blocks_as_a_group` 验证一个 claim 的多 block 联合判定。
- `test_scan_snippet_expands_to_complete_sentence_boundaries` 与 `test_scan_snippet_keeps_opening_and_closing_sentence_punctuation` 验证 `scan_term` 使用完整句界。

### 真实模型证据

- 模型：DeepSeek Flash 4.1（项目当前版本）；API alias 和响应 `actual_model_id` 均为 `deepseek-flash`。
- 参数：temperature `0.0`，thinking disabled。
- 单 occurrence 正例：10/10 supported。
- 删除范围限制的反例：3/3 unsupported。
- 三个固定真实多 occurrence packet：各自 10/10 supported，合计 30/30。
- v6 同一组实验曾出现 packet A 7/10 supported，packet B/C 各 0/10 supported；证明问题不是靠单个 case 的偶然样本关闭。

### 真实运行证据

- 旧 run 新 revision：`run_b9adc0e8826544f1a7a40dfe0982cfed` / `evalrev_82e596da3adc4b5ead90e7af84daa888`，citation support 10/10 pass，目标 claim 8/8 supported。
- 新 c1-only N=10：`run_b2211fcc2f2b45f9903d0f1921354482` / `evalrev_0556d60f3be544a0a0efaf406642168a`。
- 新 run 结果：10 attempts succeeded；R／X 30/30 pass；citation integrity 10/10 pass；citation support 10/10 pass；annotation artifact 10/10 complete。
- 新 run 的 10 份真实 annotation artifact 中，`提到过。`类低信息 claim 为 0。
- 历史 revision、CheckResult 和 invocation 均未改写。

### 失败探针

- v6 在完整多 occurrence 输入下稳定误判保真转述；v7 改为判断完整命题的正面评价强度，而非遇到“正确／对”就判增强。
- 删除 broader scope 的反例在 v7 下 3/3 unsupported，说明 prompt 没有退化成“所有近义都 pass”。
- 无答案 claim 仍返回 unknown／pending，不自动算 pass。

### 人工验收

- [x] 负责人复核 `citation-rubric-v7` 与 `claim-citation-v2` 全文并确认可以生效。
- [x] 负责人逐题回答 R1–R3。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么 citation judge 不能要求 claim 与 evidence 逐词对应？ | 引用支持是跨中英文转述的语义判断，逐词匹配会制造更多误伤；判官应检查证据段落是否支持整个 claim 的完整语义，而不是只匹配局部词。 | 已通过 | 语义判断以完整 claim 为核验对象，并保留范围与强度边界。 |
| R2 | 为什么“好问题，但范围更广”和“问题本身合理，但范围更广”应视为近义保真？ | “好的”和“对的”都是对问题的正面评价，单独比较可能有细微差异；但两句都保留了后面的转折和范围限制，所以整句语义没有被提高。 | 已通过 | 判断落在完整命题上，范围和强度限制未被删除。 |
| R3 | 为什么固定 packet 重复测试是判断 batch 污染的必要证据？ | 结果同时受输入上下文和模型抖动两个变量影响；固定输入重复可观察抖动，对照单 occurrence 与完整批次则隔离上下文差异，这是消融验证。 | 已通过 | 生产判官不重复执行；重复与消融只用于稳定性验证。 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 已知风险与后续

- LLM judge 无法提供形式化确定性保证；本切片只减小可解释的假阳性和假阴性。
- 当前 `deepseek-flash` 只返回公开 alias，不能从 API 响应独立核验内部 `4.1` 子版本。
- 真实模型结果绑定当前 DeepSeek Flash 版本；模型升级后需要重跑固定 packet 与 c1 N=10。
- 本轮不拆 occurrence；若未来固定 packet 再次翻转，再评估单 occurrence 调用、双判官或其他稳定性策略。

## 协作复盘

- AI 明显加速的环节：从失败批次中恢复固定 packet、重复调用真实模型、聚合同 claim 的 verdict 分布。
- 负责人失去系统模型的环节：曾把“问题本身是对的”误认为 Agent 语义漂移，后续通过人工阅读原文推翻。
- 真正发现问题的控制点：固定输入重放，以及区分单 occurrence 与完整多 occurrence packet。
- 只有仪式感、没有产生判断价值的流程：无。
- 下一次准备调整的一到两项：开放语义边界先做小样本正反校准，再启动长回归。
