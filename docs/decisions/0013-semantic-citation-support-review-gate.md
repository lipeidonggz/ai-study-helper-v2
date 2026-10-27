# 0013 · 引用语义支持采用候选与人工复核门禁

- 状态：已接受
- 决定日期：2026-10-26
- 确认日期：2026-10-27
- 影响范围：引用完整性、引用支持判官、人工复核、attempt 红线聚合、评测成本与基线资格

## 背景

引用完整性可以由程序确定性验证，例如 span 是否存在、block 是否来自本次 attempt、quote 是否能逐字回溯。引用语义支持则不同：它依赖自然语言解释、目标框架、范围和语义关系，即使常驻温度固定，LLM judge 仍存在输出抖动和误伤。

固定答案只改判官的对照实验显示，同一个 run 的 pass／fail／pending 会随判官版本、规则复杂度和输入组织变化。早期做法把单次语义判官 `unsupported` 直接作为 attempt 红线，会让评测结论同时受 Agent 质量和判官抖动支配。

## 决定

1. `citation_integrity` 保持确定性硬红线，不允许人工覆盖。
2. `citation_support` 使用一个默认语义判官，按 claim 和联合 evidence block 组判定 supported／unsupported／unknown。
3. 自动 `unsupported` 形成语义红线候选：attempt 可投影为未确认的普通失败，但检查、case 和 revision 保持待复核，且红线为零。
4. 自动 `unknown`、非法输出、provider 或预算失败同样保持 pending，不能折算成 pass。
5. 人工按 occurrence 复核完整 claim 和联合 block；全部 supported 为 pass，任一确认 unsupported 为 fail，仍有 unknown 为 pending。
6. 人工复核采用 append-only 记录，原 CheckResult、trace、答案和 invocation 不改写。
7. claim 角色、evidence 角色和关系比较三阶段判官退出默认评测链路，保留为可选诊断能力。
8. pending 不算 pass；含未处理 pending 的 revision 不能作为通过基线。

## 候选方案

| 方案 | 结论 | 原因 |
| --- | --- | --- |
| 单次 LLM unsupported 直接 fail | 不采用 | 把语义灰色地带和模型抖动升级为确定性红线 |
| 单次 LLM supported 直接 pass，unsupported 人工复核 | 不采用 | 会把 judge 漏检静默变成通过 |
| 所有语义结果统一 pending 后人工处理 | 暂不采用 | 人工成本过高；supported 可作为自动筛查结果，但仍受 revision pending 门禁约束 |
| 自动结果只做候选，人工复核争议状态 | 采用 | 保留自动化效率，同时让最终红线有可追溯的人类裁决 |

## 后果

- 评测结论不会因为一次 judge `unsupported` 自动判死，但会增加人工复核工作。
- `citation_support` 自动检查的持久化状态仍为 pass 或 pending；评测聚合可把 unsupported 投影为未确认普通 attempt 失败。跨项 check fail 和红线主要来自人工确认。
- 默认 judge 调用从最多五次降为主判官加一次协议重试，降低 token 成本和规则竞争。
- 历史 CheckResult 和人工复核继续可回放；新 checker revision 与旧 revision 不做直接混比。
- 关系诊断能力保留，但不再影响默认 pass／fail。

## 验证

- 定向测试覆盖 unsupported → pending、unknown → pending、非法输出 → pending。
- 定向测试覆盖人工确认 fail、人工驳回恢复 pass、人工 unknown 维持 pending。
- 定向测试覆盖一个 claim 的多个 block 联合复核，以及 occurrence 集合不完整时拒绝提交。
- 固定 N=10 run 追加新 judgment revision，确认 integrity 全通过时不因单次语义候选自动产生 case fail。
- v11 / checker 18 的固定 run 证明门禁状态流转可用；但已知角色反转仍出现 5/5 主判官假阴性。该结果推动保留关系诊断能力，并阻止把当前 revision 升格为质量基线。

## 推翻条件

- 语义候选经人工复核后仍无法形成稳定结论，说明当前 claim／context 粒度不足；
- 人工复核成本高到评测无法持续运行，且能证明另一种低成本复核方式保持同等安全；
- provider 提供可本地验证、可解释且稳定的 claim 级语义支持能力，能够替代当前候选门禁。
