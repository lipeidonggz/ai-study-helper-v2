# 0017 · 精确 block 读取与显式章节范围

- 状态：已采纳
- 决定日期：2026-10-27
- 影响范围：Agent 工具契约、模型提示词、trace、证据合并

## 背景

`c3-001` 中，`scan_term("93%")` 已精确定位 `b00002`，但工具只返回 snippet。Agent 为了补全正文调用 `list_blocks`，传入只包含文档标题的根路径；由于章节选择包含全部后代，最终读取了整篇 26 个 block。

同时，工具 descriptor 只向模型声明输入 schema。系统提示虽声称 payload 结构由工具定义决定，模型实际看不到 output schema。`get_outline` 的 adapter 最终 payload 也没有独立模型校验。

## 决定

1. 新增 `get_blocks(block_ids)`，按请求顺序去重并返回每个 ID 对应的完整 block；任一 ID 缺失时整次返回 `scope_not_found`。
2. `list_blocks` 不传 `section_paths` 时读取整篇。
3. `list_blocks` 传非空 `section_paths` 时必须显式传 `include_descendants`：
   - `false`：只返回 path 完全相等的章节直属 block；
   - `true`：返回章节及全部后代。
4. 章节非空但缺少 `include_descendants`，或整篇请求携带该参数时，返回 `invalid_argument`。
5. 工具 descriptor 同时声明输入和输出 schema；adapter 在返回模型前校验最终 payload。
6. 工具契约升级版本，并在 trace 记录请求范围、ID 和返回数量。
7. 完整 block 和 scan match 就地携带 `section_context`，包含章节深度、是否存在子章节和文档章节总数；结构信息不与 block 分离，避免模型在长 payload 中做远端关联。

## 候选与取舍

- 让 `scan_term` 直接返回全文：会放大所有命中的上下文，并混淆定位与读取职责。
- 保持 `section_path` 固定包含后代：根章节会继续静默等同于整篇，模型无法表达 own section。
- 让章节范围默认 own：可能造成现有“读整节”调用漏掉子章节，且仍依赖隐含默认值。
- 仅把内部 Pydantic schema 原样暴露给模型：会包含内部字段并扩大提示词；应采用 adapter 最终 payload 模型和压缩 schema。
- 用顶层 `section_contexts[]` 按 path 去重：更省 token，但要求模型在两个数组间按 `doc_id + section_path` 远端关联，真实长 payload 下可靠性不足。

## 后果

- Agent 可以用一条 snippet 精确补全一个 block，不再因章节根路径读取整篇。
- 模型与 trace 能明确区分整篇、own section 和 subtree。
- 工具提示词会增长，需要监控上下文成本。
- 同一章节的多个 block 会重复三个结构字段；这是为保持证据局部可读性而接受的小幅 token 成本。
- 现有 `list_blocks(section_paths=...)` 调用必须同步补 `include_descendants`。

## 验证

- 单元测试覆盖 `get_blocks` 精确读取、缺失 ID 原子失败。
- 单元测试覆盖 `list_blocks` 整篇、own、subtree 和参数错误。
- 测试 output schema 被渲染且 payload 校验失败不进入证据。
- `c3-001` N=10 的真实 trace 检查 `scan_term → get_blocks` 路径。

## 推翻条件

如果真实任务中精确 block 补全的需求极少，而新增工具和提示词成本显著增加，可回退为 `scan_term.include_text`；若模型对显式 `include_descendants` 的调用错误率明显高于范围错误收益，则重新评估默认值和工具拆分。
