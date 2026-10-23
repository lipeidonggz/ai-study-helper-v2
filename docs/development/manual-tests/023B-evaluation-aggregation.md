# 023B · 生效判定绑定与 N/K 聚合人工验收

- 状态：已完成
- 日期：2026-10-23
- 关联切片：[023B 生效判定绑定与 N/K 聚合](../slices/023B-evaluation-aggregation.md)

## 验收项

- [x] 每个有效 attempt 的 criterion 都有唯一生效 binding。
- [x] 人工改判新增 human Judgment 与 binding，原 Judgment 仍可查看。
- [x] `citation_support` 可按 block 人工复核；`citation_integrity` 不可人工修改。
- [x] R fail、X 违规、CheckResult fail／pending 和缺失绑定按约定聚合。
- [x] 正式 N/K case 结果正确展示 pass／fail／pending 及缺口。
- [x] N<K 快速试跑只显示 exploratory 分布。
- [x] 重算结果可从数据库事实复现，不依赖前端缓存。

## 人工证据

- 自动证据见 [023B 证据包](../slices/023B-evaluation-aggregation.md#证据包)。
- 2026-10-23 真实 c1 判定 revision `evalrev_ddc888e0510c4f148be53a4daea995a4` 中，同一 R1 保留原 LLM pass 和两次 human Judgment；最新 binding 生效为 human pass。
- 2026-10-23 同一 revision 的 `citation_support` 人工复核为 fail，程序按该复核将 attempt 聚合为 fail；原 LLM CheckResult 保持 pass。
- 2026-10-23 该 run 为 N=1、K=9，case 结果为 exploratory；EvaluationRevision summary 按 3 条生效 Judgment 重算为 pass=3。
- 2026-10-23 formal run `run_ca42b6b380ce43daac2ecc6ee0dbe088` 因调度超发产生 7 条伪基础设施失败，不通过正式 N/K 验收；已修复并在新 formal run 上重验。
- 2026-10-23 formal run `run_21e50c302aa84068afc46ddf96589046` 进一步暴露“无缺口但仍有运行任务时提前封存”；同一正式 suite 需在第二次修复后再次重跑。
- 2026-10-23 修复后 formal run `run_861667d15e764d84b9484dff51cd162e` 恰好产生 10 个有效 product 样本，trace 全部完整，调度主路径通过。
- 2026-10-23 同一 run 的两个失败样本确认不是工具异常：模型自行伪造 `Observation: count=0`，Harness 没有实际派发 `scan_term`；已改为协议错误响应不进入下一轮 History，并将 Agent Loop 版本升级为 `react-langgraph-v2`。
- 2026-10-23 `run_5cb3608c35a74c40b2199f21fc491ccf` 的 10 个样本均真实调用 `scan_term` 且返回 3 个命中，30 条 Judgment 全通过；trace 版本仍为 `react-langgraph-v1`，需重启后台后再用 v2 验证。
- 2026-10-23 正式 case `run_5cb3608c35a74c40b2199f21fc491ccf` 判定 revision `evalrev_18b54ba8c219413eafc900f522faeb7c` 验收通过：30 条生效 Judgment 全部 pass，10 个引用完整性和 10 个引用支持检查全部 pass，case 达到 N=10、K=9。
- 2026-10-23 `run_14ee6d1b673f4f00ae32f36d8a1ddcb8` 确认修复后的运行链生效：1 个有效 product 样本、trace complete、Agent Loop 为 `react-langgraph-v2`、prompt 为 `chat-v3`；真实执行 `list_documents`／`scan_term`／`list_blocks` 且无 protocol_error。
