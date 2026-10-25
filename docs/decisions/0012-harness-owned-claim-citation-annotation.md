# 0012 · 由 Harness 组织 claim 级引用标注

- 状态：已接受
- 决定日期：2026-10-25
- 影响范围：最终回答协议、引用展示、trace artifact、引用判官、评测预算与基线可比性

## 背景

此前由最终回答模型在正文里直接输出 `【来源：block_id】`。这种自由文本引用有三个问题：

1. 引用标记可以放在句子、段落或多个列表项之后，判官很难可靠判断它到底承诺支持哪一段主张。
2. 同一段里多个独立 claim 可能共用或粘连一个引用标记，导致支持与不支持无法分别判定。
3. 引用格式与正文生成耦合后，一旦引用表达出错，只能重写正文或让判官猜测，违背“正文事实单源”和“判官只看明确输入”的原则。

2026-10-25 对照业界方案后，负责人确认先验证“正文一次生成，引用独立标注”的两阶段方案。它比一阶段生成多一次模型调用，但通常只相当于增加一次迭代的消耗；分批策略留到实测确认输入规模需要时再引入。

## 决定

1. 最终回答模型只生成一次完整正文，不输出来源、`block_id` 或引用占位符。
2. 正文冻结后，Harness 按段落、列表项、表格行和句末标点确定性切出 claim spans；标题和代码块不参与引用标注。
3. 独立 citation annotator 只接收问题、冻结正文、spans 和本次 attempt 的真实工具证据，只返回 `span_id + block_ids[]`。
4. annotator 不得返回正文或文本摘要。Harness 从原答案按 offset 切回 span，并校验每个 block ID 属于本次真实证据白名单。
5. 每个 annotation 保存 `span_id`、精确 offset、原文、kind 和 block IDs，作为 trace 中的只读 citation artifact，并随流式事件提供给 UI。
6. 前端只在渲染副本中按 offset 追加来源标记；原答案字符串不变。点击角标就地打开锚定悬浮卡展示来源内容，不滚动主页面；历史 trace 的 inline 引用仍支持只读回退。
7. 标注调用最多尝试两次。非法输出、越界 ID、重复 span、超上下文或超请求预算时保留原正文，引用进入 pending，并记录明确错误；不得把 pending 当成“无引用即通过”。
8. 评测引用检查直接使用结构化 annotation 的 span 文本作为 claim context。没有 citation artifact 的历史 attempt 才回退解析 inline marker。

一阶段生成 `answer_blocks + citations` 暂不实现。后续可在同等样本和模型版本下做成本、正文连贯性、引用正确率与失败可归因性的对照。

## 候选方案

| 方案 | 结论 | 原因 |
| --- | --- | --- |
| 继续由正文模型自由写 inline 引用 | 不采用 | 引用归属不确定，判官需要猜，同一段多个 claim 无法可靠拆分 |
| 一次生成 `answer_blocks + citations` | 暂不采用 | 调用更少，但正文可能被结构切碎，需先建立两阶段对照基线 |
| 两阶段：冻结正文后独立标注 spans | 采用 | 正文连续、引用关系结构化、失败可保留正文并显式 pending |
| 引入第三方引用 API | 不采用 | 当前国内可选模型没有完全等价能力，无法覆盖本地证据协议 |

## 后果

- 每次正常回答增加一次 citation annotation 模型调用及其 token、延迟和重试预算。
- Trace 新增 `citations` artifact；评测和 UI 消费同一份结构化事实。
- `prompt` bundle 升级为 `chat-v5`。新旧 run 的引用协议不同，引用支持结果不能直接混比，需要新基线。
- 本切片只消除“引用归属哪段 claim”的歧义。“一个 span 的多个 block 是否必须联合覆盖”以及“引用是否必要”仍沿用现有 occurrence 语义，后续再设计。
- annotation 失败不会把正文改成空或触发重写，但用户会看到“引用标注未完成”，评测中的引用检查为 pending。

## 验证

- 276 项后端测试、Ruff、Pyright、前端类型检查、Vite production build 和注释门禁通过。
- 单元测试覆盖同段双 claim、多 block、非法 span、未知 block ID、重复 span、超预算、模型失败和历史 inline 回退。
- Runtime 测试覆盖 annotation 事件、usage 累加、limitation、trace artifact 和非流式 API 投影。
- 评测测试覆盖 claim context 精确等于 annotation 原文，以及 pending annotation 使引用检查保持 pending。
- 前端渲染按 Unicode code point offset 映射到 JavaScript UTF-16，仅在渲染副本插入来源，不修改原答案。
- 真实模型 N=1 试跑与人工引用复核作为本切片的最终验收门槛。

## 推翻条件

- 真实样本显示两阶段带来的额外调用、延迟或成本明显高于可接受范围，且一阶段结构生成没有正文碎片化问题；
- 当前聊天模型无法稳定选择 span 或 block，失败率持续高于可人工复核的范围；
- provider 提供可直接约束 claim 与证据关系的原生引用能力，且能本地验证同一证据白名单与 trace 语义。
