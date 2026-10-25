# 027 · Claim-aware 引用标注人工验收

- 状态：已完成
- 日期：2026-10-25
- 关联切片：[027 Claim-aware 引用标注](../slices/027-claim-aware-citation-annotation.md)

## 验收项

- [x] 提问一个需要连续两句话回答、且两句话来自不同证据的问题；正文保持一次生成，不出现模型自写的 `【来源：...】`。
- [x] 两句话分别显示来源角标；点击角标就地打开悬浮卡展示来源内容，不滚动或离开当前阅读位置。
- [x] 悬浮卡中的标题、章节和证据摘要正确；可点击外部或 Escape 关闭，也可显式进入文档详情。
- [x] 一句话由两个 block 联合支持时，能显示两个来源，且 trace 中 annotation 的 `block_ids` 保留同一组关系。
- [x] 运行轨迹中存在 `citations` artifact，状态为 `complete`，annotation offset 能从答案原文精确切回 `text`。
- [x] 评测判定详情中的引用 claim context 与 trace annotation 的 `text` 一致，不再由判官重新猜测该引用管哪句话。
- [x] 人为触发或复现 annotation 失败时，正文仍完整显示，并明确提示引用标注未完成，而不是显示通过。
- [x] 历史含 inline 引用的旧 run 仍能打开、重判和显示来源。

## 人工证据

- 产品问答：`c5-001`，`deepseek-flash`，`chat-v5`。
- Trace `2c04b586e3c478c5ac84612050d06b42`：同一摘要段第一句标注 `b00012`，界面显示来源 3；第二句标注 `b00013 + b00014`，界面在同一句末尾显示来源 1、2。
- 同一 trace 的表头无引用，三个数据行分别保留来源 4。表格列边界、行内代码后引用和多 block 角标均由负责人确认。
- Trace `0f982875386a86c3f9480b9e07705642`：32 个 annotation 的 offset 和 block 白名单全部合法；表格引用在修复后由负责人确认显示正常。
- 负责人确认引用角标改为就地悬浮卡后不再打断阅读位置；点击外部、Escape 和显式进入文档详情的交互符合预期。
- 评测 run `run_780dcfeedc474985a77f12f967b6fe67`：EvaluationRevision `evalrev_69bad9f7cef8470b8efbcbe2969fa410`，trace `b673b20f23759ea9910c2a23e8f1b6ff`，citation annotation 状态 `complete`。
- 评测 run 的 29 个 trace annotation 与 29 个 judge context 一一对应，context ID、文本和 `kind=claim` 全部一致；29 个 occurrence、5 个 block、6 个批次，无缺失、多余或 preflight failure，29 个 occurrence 均为 supported。
- `citation_integrity=pass`、`citation_support=pass`；提示词版本为 `citation-rubric-v4`，模型为 `deepseek-flash`。
- 历史回退：`run_14ee6d1b673f4f00ae32f36d8a1ddcb8` / trace `980888c4763636ccf3076ef649b0a7fb` 使用 `chat-v3`，没有 `citations` artifact，答案含 3 个 inline 引用。重判生成新 revision `evalrev_988a719b901743ef9ff82387d1b34cc7`，原 revision `evalrev_e7a09eb71c2f4ef8833be441af4c433d` 保留。
- 历史回退生成 3 个 `list_item` context 和 3 个 occurrence，3 个均为 supported；引用完整性通过，缺失 block 为 0；引用支持版本为 `citation-rubric-v4`。
- Pending 探针：临时将最大输出 token 设为 `2,048`，trace `327ce59b5795aebfd0db7cc1d568c73a` 产生 93 个 span、86 个证据块；citation annotation 两次调用均未通过 schema，状态为 `pending`，错误码 `citation_annotation_output_invalid`。
- 该 trace 保留 3,877 字符正文，answer completeness 为 `partial`，limitations 同时包含 `answer_output_truncated` 和 `citation_annotation_pending`；界面显示“内容未完”和“回答完成，引用标注未完成”，没有把 pending 伪装成通过。
- 负责人确认正文连续性、来源位置、悬浮卡、表格展示、partial 与 annotation pending 提示符合产品语义。
