# 023A · 程序 Checker 与 LLM Judge 人工验收

- 状态：已完成
- 日期：2026-10-23
- 关联切片：[023A 程序 Checker 与 LLM Judge](../slices/023A-evaluation-judgment.md)

## 验收项

- [x] active criteria 的 checker／llm_judge 路由符合已确认分类。
- [x] 对含有效 attempts 的 run 启动判定后，能看到逐 attempt／criterion 进度。
- [x] 主判官输入只包含 answer 和当前 criterion 的 rule_type／rule_text。
- [x] LLM Judge 输出包含符合 R／X 约定的三态结论和原因，提示词及模型版本可从调用记录追溯。
- [x] 引用完整性与引用支持检查分开，引用判官逐 block 输出结论。
- [x] 证据不足、非法输出和上游失败显示 pending／基础设施错误。
- [x] criterion 级 checker 与 LLM Judge 使用相同 Judgment 事实模型；跨判定项检查独立使用 CheckResult。
- [x] 非基础设施失败计入有效样本并全部进入判官；无 answer 时仍产生 criterion Judgment。
- [x] 人工审核 `chat-v3` 的 finalization 模式提示词。
- [x] 预算或输出长度导致截断时，保留已生成正文、明确声明“内容未完”，并返回 partial。
- [x] 历史页面可查看 judge input 摘要、原始输出 artifact、usage 和 trace。
- [x] 重判追加新 Judgment，旧记录仍可查看。
- [x] 引用完整性、引用支持以及 R／X 分区说明在界面中可理解。

## 人工证据

- 自动证据与干净 schema 4 探针见 [023A 证据包](../slices/023A-evaluation-judgment.md#证据包)。
- 2026-10-23 负责人已审核通过 `judge-rubric-v1` 系统提示词。
- 2026-10-23 负责人确认 judge-ready suite、c1 judge revision、cf-003 checker revision 和单样本真实执行现场正确。
- 2026-10-23 负责人确认历史执行时间、总耗时和 trace 展开详情正常，单样本执行验收通过。
- 2026-10-23 负责人确认 judge prompt 审阅门禁、默认判定配置及可编辑范围符合预期。
- 2026-10-23 负责人完成一次真实 LLM Judge：c1-001 单样本 run 状态完整，3 个 criterion 任务全部通过，待定 0。
- 2026-10-23 负责人确认 checker 明细、3 条判定原因、有效 evidence block、quote、prompt 版本和调用 usage 可追溯。
- 2026-10-23 负责人完成一次正常重判：修订版选择器出现两版，切换后旧 Judgment 与 revision ID 均保持不变。
- 2026-10-23 负责人通过真实界面确认判定预算不足时 3 个任务均保持待定；证据缺失、非法输出和上游连续失败由定向自动化测试覆盖。
- 2026-10-23 负责人确认产品侧与评测侧共享轨迹详情，调用层级配色、artifact 链接、弹框预览和模型输入/输出结构化查看符合预期。
- 2026-10-23 负责人逐条确认 18 个用例的 69 条自包含 criterion；验收时发现旧解析器把设计表分区标题“边界”并入 14 条最后一项 R 判据。
- 2026-10-23 已修复解析器并生成 `sr_design03_minimal_judge_v2`；真实库 69 条 active criterion 的误并入数量为 0，v1 与历史 revision 保持只读。修订前备份为 `eval.db.before-minimal-v2-20261023`。
- 2026-10-23 负责人审核通过 `judge-rubric-v2` 主判官提示词。
- 2026-10-23 负责人审核通过 `citation-rubric-v2` 引用支持提示词。
- 2026-10-23 负责人确认 active suite 为 `sr_design03_minimal_judge_v2`，c1／cf-003 active revision 和两份提示词展示正确。
- 2026-10-23 新单样本 run 验收通过：`sealed_complete`、1 个有效样本、执行成功、trace 完整。
- 2026-10-23 v2 新单样本 run `run_db855cdda0fe4c3687de521d5f7b2645` 验收通过：主判官 user 消息只包含 answer 和当前 criterion，未包含问题、证据、运行状态或其他 Judgment；criterion 文本末尾不再含设计表分区标题。
- 2026-10-23 上述 run 的 R1／R2 为 `satisfied`、X1 为 `not_violated`；原始输出只含 `verdict` 与 `reason`，提示词版本、模型、hash、usage 和 revision 可追溯。
- 2026-10-23 负责人通过真实界面确认引用完整性单独列出 3 个已引用 block、0 个缺失和 3 个证据块；引用支持对同一 3 个 block 分别输出 `supported`、原因和原文片段，单次调用 usage 为 1081／340 token。
- 2026-10-23 负责人通过 cf-003 历史界面确认 checker“截断事实”和 LLM Judge“截断声明”都进入同一 R 判据列表；引用完整性通过、引用支持 pending 独立显示，未遮蔽两条 Judgment。
- 2026-10-23 D8 已实施：schema 4 将 `request_budget_exceeded` 回填为产品样本，将执行器异常保留为基础设施失败；产品失败的 N 计数、无答案判定、基础设施替代和 trace_status 分离由自动化测试覆盖。
- 2026-10-23 `chat-v3` 已加入 finalization 专用提示词和截断 Finish 恢复逻辑；定向及完整自动化测试通过。
- 2026-10-23 负责人审核通过 `chat-v3` finalization 提示词，模板 hash 为 `03f344febcac964251d8004c9a8e189b2033891314eaecf5b345c59a37ba4fd4`。
- 2026-10-23 新 cf-003 run `run_0cd1a6649ed340d0b71df10b0a626b09` 验收通过：只有 1 个 `partial / product` attempt，trace complete，prompt 为 `chat-v3`；answer 保留 15,575 字符并追加“内容未完”；R1、R2 均通过。
- 2026-10-23 上述 run 的引用完整性通过；引用支持因 73,611 字符超过输入上限保持 pending，EvaluationRevision 因该项为 incomplete，但两条 criterion Judgment 已完成。
- 2026-10-23 负责人通过界面确认上述 run 的判官输入、原始输出、模型与 usage 可回看，trace 调用树与 artifact 可打开。
- 2026-10-23 负责人确认引用完整性与引用支持说明、R 必选区与 X 边界区说明清晰，单 R 用例不会显示空 X 区域。
