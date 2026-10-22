# 0008 · Harness 统一计算 Tool Observation token

- 状态：已接受
- 决定日期：2026-10-22
- 影响范围：检索工具契约、Agent Harness、上下文预算、trace 与可复现性

## 背景

早期检索设计让每个工具在自身返回 payload 中携带 `result_tokens`，再由 Harness 做上下文准入。该方案会把对话模型 tokenizer／估算器、序列化规则和预算概念渗透到所有工具中：

- 工具无法知道 Harness 最终采用的 Observation 包装和 JSON 序列化；
- 五个工具可能分别实现 token 计数，产生口径漂移；
- 工具需要感知本不属于检索职责的对话模型预算；
- 工具 payload 中会出现模型决策不需要的预算元数据。

`result_tokens` 描述的是工具结果进入模型上下文前的大小，属于 Harness 生命周期，不属于检索领域数据。

## 决定

1. 五个工具只返回业务 payload，不计算也不返回 `result_tokens`。
2. Harness 在工具返回后，按固定序列化规则生成准备加入 History 的完整 Observation。
3. Harness 使用本次请求固定的对话模型 tokenizer；若无法稳定分发模型 tokenizer，则使用已经标定的保守估算器并记录版本。
4. Harness 对最终 Observation 统一计算 `result_tokens`，写入 Harness 状态、`tool.call` trace 和预算检查记录。
5. `result_tokens` 不作为工具业务 payload 的一部分，也不要求模型直接看到。
6. 是否允许结果进入 History，由 Harness 对加入后的完整 prospective History 做唯一一次上下文预算判断；`result_tokens` 只表示该工具结果的大小。
7. embedding usage 与 `context_token_estimate` 继续使用各自独立口径，不与 `result_tokens` 混算。

## 候选方案

| 方案 | 结论 | 原因 |
| --- | --- | --- |
| 每个工具自行计算 | 不采用 | tokenizer、序列化和预算逻辑重复，容易漏算包装字段并产生跨工具差异 |
| 工具返回原始大小，调用方自行猜算 | 不采用 | 调用方可能看到不同中间结构，无法保证与实际 Observation 一致 |
| 工具返回业务 payload，Harness 统一计算 | 采用 | 责任边界清晰；计数对象就是模型实际收到的完整工具结果 |

## 后果

- 工具契约更纯粹，后续 `search_blocks` 和新工具不需要接入 tokenizer。
- `result_tokens` 的口径、版本和估算误差只在 Harness 维护。
- 工具结果进入 History 前需要经过统一的 canonical serialization；该序列化也进入可复现版本范围。
- trace 仍记录 `result_tokens`，但其来源标注为 Harness。
- 如果未来需要给模型显示预算数字，应由 Harness 显式添加，而不是由工具返回。

## 验证

- 五个工具的 payload schema 中不存在 `result_tokens`。
- Harness 对同一份 canonical Observation 重复计算得到相同结果。
- 一次请求内切换工具时，tokenizer／估算器版本保持一致。
- `tool.call` trace 与实际预算检查使用同一个 `result_tokens`。
- 完整工具 payload 中新增字段时，不需要修改工具自身的计数实现。

## 推翻条件

- Harness 无法获得工具的完整模型可见 payload；
- 工具必须在进入 Harness 前自行裁剪，且裁剪结果无法被统一序列化；
- 统一计数被实测证明产生不可接受的性能或缓存开销，并存在经过验证的更优边界。
