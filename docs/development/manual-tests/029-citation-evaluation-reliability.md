# 029 · 引用评测可靠性人工验收

- 状态：待验收
- 关联切片：[029 引用标注分批与联合支持判定](../slices/029-citation-evaluation-reliability.md)

## 操作

1. 重启产品和评测应用，确保运行的是包含 029 的当前构建。
2. 对 `run_a36943ad6bcf4edf85f3f789e8f89e49` 创建新的重判 revision，不覆盖历史 revision。
3. 使用低预算快速试跑单独启动 `cf-003`，确认点击启动后立即看到 run ID 与执行面板。
4. 打开 `c3-001`、`c13-001`、`c6-001`、`c7-001`、`cf-001`、`cf-003` 的检查详情。
5. 打开 `cf-003` 对应 trace，核对其 citation annotation batch 数与输出状态。

## 期望

- [ ] `c3-001` 的判官证据使用 block 完整正文，不再使用带省略号的短 snippet。
- [ ] `c13-001` 的多 block claim 显示为引用组联合结论，并投影到逐 block 结果。
- [ ] `c6-001`、`c7-001`、`cf-001` 若首次输出非法，能看到两次 invocation；第二次成功则 CheckResult 不应 pending。
- [x] `cf-003` citation annotation 使用 `evidence-blocks-v1`，batch count 为 4，不再因单次输出 4096 token 截断。
- [ ] citation annotation 若仍达到输出上限，错误码明确为 `citation_annotation_output_truncated`。
- [ ] 检查详情中的引用组文案、逐 block 投影和人工复核入口可正常使用。
- [ ] 点击启动后计划区显示 run ID／状态，按钮不能重复启动，并自动定位到执行面板。

## 记录

- 新 EvaluationRevision：`evalrev_e970b926662546e497f4e3953bbf6063`
- citation annotation batch count / status：4 / `complete`；`evidence-blocks-v1`；100 spans、86 blocks
- `c3-001`：
- `c13-001`：
- `c6-001`：
- `c7-001`：
- `cf-001`：
- `cf-003`：run `run_4044c38044d44ea2810ae40aa09f721a` 使用 2048 主回答上限；R1、R2 pass；citation integrity pass；citation support 17 批、84 个 claim 组全部 supported
- 人工结论：`cf-003` 低预算截断探针已通过；其余跨用例现场待复跑
