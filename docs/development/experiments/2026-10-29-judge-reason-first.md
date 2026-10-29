# 2026-10-29 · Judge reason-first 输出顺序校准

## 目的

验证把单 criterion 判官的输出顺序从 `verdict → reason` 改为
`reason → verdict`，是否减少“理由已经指出缺失，verdict 却仍为 satisfied”
的自相矛盾。

## 实现

- 保留 `judge-rubric-v2` 原文和 hash，保证历史判定可重放。
- 新增 `judge-rubric-v3`：
  - 先逐项核对 criterion；
  - reason 必须先说明是否缺少必要内容；
  - 完成 reason 后再输出 verdict；
  - verdict 必须与 reason 最后结论一致。
- 判官服务允许 criteria 分别引用 v2 或 v3；未升级的 active criteria 继续使用 v2。
- c13 使用 reviewed case revision `cr_c13_001_84e75a91dbcb` 引用 v3；该过渡 revision 仍包含 X1。

## 冻结重判

- Run：`run_4e54628da1024fc9bf749c7810c5881b`
- 过渡 judgment revision：`evalrev_3d9efdb5526a474580fb0b324bd228b3`
- 判官调用：30
- reason-first 原始输出：30/30
- verdict 分布：satisfied 5、unsatisfied 5、not_violated 19、violated 1
- criterion：24 pass、6 fail、0 pending
- case：5 pass、5 fail、0 pending

## 删除 X1 后的最终重判

负责人确认 X1“把排除项当作新增”只影响低风险分类，不应升级为整题红线，因此：

- 最终 case revision：`cr_c13_001_6c08a5db0d8f`
- 最终 judgment revision：`evalrev_d44ad528832a472c98c125f47eb90dd6`
- 判官调用：20
- reason-first 原始输出：20/20
- criterion：15 pass、5 fail、0 pending
- case：5 pass、5 fail、0 pending
- X2：10/10 not_violated

最终 5 个失败都由 R1 的真实类别缺失造成，不再包含 X1 误伤。

## 与 v2 的差异

v2 系统聚合为 7 pass、3 fail，但 `attempt_2e8fa87d...` 的 verdict
与 reason 自相矛盾。v3 后该 attempt 正确变为 fail。

另一个变化：

- `attempt_5ccff8c8...` 从 pass 变为 fail。
- R1 认为它没有明确说明工具／外部内容在进入上下文前经过检查；
- 该答案仍被识别为缺少四类中的“工具及外部内容检查”；删除 X1 后这个失败继续保留。

## 结论

reason-first 消除了本次观察到的 verdict/reason 矛盾。X1 对“先说明排除项、
后面仍补充出口细节”的判定被负责人确认为低影响分类问题，因此从最终 c13
revision 删除；v3 reason-first 输出契约保留，并随最终 c13 revision 激活。

## 复现

- 过渡版 case revision：`cr_c13_001_84e75a91dbcb`
- 最终 case revision：`cr_c13_001_6c08a5db0d8f`
- 过渡重判结果：`backend/.runtime/c13-rejudge-v3.json`
- 最终重判结果：`backend/.runtime/c13-rejudge-v3-final.json`
- 原始顺序审计：`backend/.runtime/inspect_c13_judge_v3_outputs.py`
