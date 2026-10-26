# 031 · 引用语义与 annotation 校准人工验收

- 状态：验收通过
- 关联切片：[031 引用语义同一性与判官稳定性校准](../slices/031-citation-semantic-calibration.md)

## 验收目标

负责人确认以下基础件可以进入正式评测基线：

- `citation-rubric-v7`：引用支持判官的语义等价、范围限制与 occurrence 隔离规则；
- `claim-citation-v2`：后置引用标注只接受包含完整、独立、可核验主张的 span。

## 自动证据入口

- 旧 run v7 revision：`run_b9adc0e8826544f1a7a40dfe0982cfed` / `evalrev_82e596da3adc4b5ead90e7af84daa888`
- 新 c1-only N=10：`run_b2211fcc2f2b45f9903d0f1921354482` / `evalrev_0556d60f3be544a0a0efaf406642168a`
- 固定 packet 与版本对照：[citation-rubric-v7 校准实验](../experiments/2026-10-26-citation-rubric-v7-calibration.md)

## 人工检查

- [x] `citation-rubric-v7` 没有出现具体 case ID、答案原句或仅供 c1 使用的特判。
- [x] prompt 允许“正面评价＋转折＋更宽范围”的保真转述。
- [x] prompt 仍拒绝删除范围限制、排他正确和完整覆盖等语义增强。
- [x] `claim-citation-v2` 明确排除单独确认词、承接语、标题、过渡句和低信息碎片。
- [x] 新 c1 run 的 10 份 annotation 可读，未出现“提到过。”这类独立 claim。
- [x] 可以接受旧 revision 与历史 CheckResult 保持不变，新结论只追加新 revision。

## 验收结论

- 验收人：沛东
- 验收时间：2026-10-26
- 结论：通过
- 备注：逐项确认 D1–D3，并完成 R1–R3 反向解释。
