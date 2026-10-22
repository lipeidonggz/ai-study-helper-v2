# 017 · 混合检索 search_blocks

- 状态：已完成
- 开始日期：2026-10-22
- 关联设计：[06 检索侧 T3](../../design/06-retrieval-pure-agent.md)、[ADR 0008 Harness token 归属](../../decisions/0008-harness-owned-result-token-accounting.md)
- 前置切片：[016 检索确定性读取层](./016-retrieval-read-layer.md)

## 控制契约

### 用户可观察目标

1. 对当前固定快照输入自然语言查询，返回最多 5 个相关块及其完整正文。
2. 查询可按文档范围限定；指定范围中有缺失文档时整体失败，不返回部分范围结果。
3. 相同快照、查询和参数产生稳定顺序；无匹配、范围为空与上游失败可区分。

### 核心不变量

1. dense 与 FTS 在同一 collection 快照和同一 block 集上执行。
2. query embedding 使用快照固定的 Qwen 模型、1024 维和 `text_type=query`。
3. 两路候选先按固定 RRF 公式融合，再按 `doc_id → block_index → block_id` 升序平局。
4. 最终最多返回 5 个块全文，不向 Agent 暴露检索分数。
5. query embedding 失败且重试无效时返回 `upstream_unavailable`，不得降级为纯关键词。

### 明确禁止

- 不由 Agent 或调用方选择 dense／FTS 模式；
- 不返回部分范围结果；
- 不用 Top-5 冒充完整枚举；
- 不把 `result_tokens` 放进工具 payload；
- 不把检索分数暴露给 Agent。

### 失败语义

| 场景 | 结果 |
| --- | --- |
| `query` 去除首尾空白后为空 | `invalid_argument` |
| 显式 `doc_ids` 为空 | `invalid_argument` |
| 显式范围包含不存在文档 | `scope_not_found` |
| 查询两路均无候选 | 成功，`empty_reason=no_match` |
| 有效文档范围中没有 block | 成功，`empty_reason=scope_empty` |
| query embedding 鉴权／限流／上游失败 | `upstream_unavailable`，不降级 |
| LanceDB 索引无法读取 | `tool_internal_error` |

### 决策预算

- 预计 B 级决策：3 项。
- C 级决策：无。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 单路候选深度、RRF 常数和最终条数 | 每路取 20 个候选，RRF `k=60`，最终返回 Top-5 | 06 要求查询时参数可追溯并支持调优 | 10／20／60 均可运行；20 在召回和精确扫描成本间保守，60 是常规起点 | 检索 profile 固定为 `hybrid-rrf-v1`，后续由评测调整 | RRF 单测、真实快照候选数与 Top-5 验收 | 已确认 |
| D2 | B | query embedding 如何进入现有 provider | 新增独立 `embed_query()`，固定 `text_type=query`，与 document embedding 分开记录 request ID 和 usage | 07 明确入库使用 document、查询使用 query | 复用 document 会改变查询语义；复制 adapter 会增加维护面 | query 调用边界明确，不改变入库 profile | provider 单测与真实 Qwen query 调用 | 已确认 |
| D3 | B | 文档范围如何过滤两路检索 | 在 dense 与 FTS 查询进入 LanceDB 前使用同一 `doc_id IN (...)` prefilter | T3 要求显式范围缺失时整体失败 | 查询后过滤会漏候选；只过滤一路会造成范围不一致 | 两路在 RRF 前共享同一范围 | 范围过滤集成测试 | 已确认 |

## 目标

实现 T3 `search_blocks` 的快照绑定混合召回和数据读取，不接 Agent 循环或 Harness 预算。

## 输入与输出

输入：

- 非空 `query`；
- 可选 `doc_ids`，省略表示固定快照全库。

输出：

- 最多 5 个 `{block_id, doc_id, doc_title, source_label, section_path, text}`；
- `empty_reason`；
- 调试侧保留两路 rank、RRF 分数和 embedding usage，但不进入 Agent payload。

## 不包含

- Agent 循环、系统提示词和工具 envelope；
- `result_tokens` 与上下文预算；由 Harness 在后续切片统一处理；
- query rewrite、路由和模型选择；
- 检索工具 trace exporter；
- ANN 索引或 SQLite FTS 替换。

## 验收条件

- [x] 两路检索读取同一个固定快照，并覆盖同一 block 集。
- [x] Qwen query embedding 使用 `text_type=query`、1024 维并校验响应。
- [x] RRF 排序和平局规则确定。
- [x] 最终最多返回 5 个完整块，不返回检索分数。
- [x] 空 query、空范围、缺失文档、no-match 和 upstream failure 语义稳定。
- [x] query embedding 失败不降级为纯关键词。
- [x] 完整质量门通过。

## 证据包

### 自动化证据

- 017 定向测试与 provider 测试：15 passed。
- `corepack pnpm quality:quick`：通过。
- 后端完整回归：135 passed。
- RRF 单测覆盖双路分数、排位与平局规则。
- query embedding 测试确认 `text_type=query`、单向量响应、重试一次和失败不降级。

### 运行证据

- 真实 Qwen query embedding：1024 维、22 input tokens，返回有效 request ID。
- 当前活动快照 `s_6b787258ba7d700c` 真实混合检索：20 个 dense 候选、14 个 FTS 候选，最终返回 5 个完整块。
- 同一真实查询的 Top RRF 候选同时保留 dense `_distance`、FTS `_score` 和最终 RRF 分数，内部诊断不进入 Agent payload。

### 人工验收

- [x] D1-D3 已确认；真实快照检索结果数量与排序路径完成实际运行验证。

## 反向解释

1. 为什么查询范围必须在两路候选进入 RRF 前过滤？
2. 为什么 embedding 上游失败不能退化成 FTS-only？
3. 为什么检索分数只留 trace，不进入 Agent payload？

## 协作复盘

- 先用纯 RRF 单测锁定排序，再用真实快照验证 LanceDB 查询，快速发现并消除了 `_distance/_score` 自动投影兼容风险。
- 将 query embedding 明确拆为 provider 的独立输入类型，避免入库与查询共用错误语义。

## 已知风险与后续

- 20／60／5 是首轮保守参数，不代表已经通过金标准质量与首字延迟评测。
- 当前真实检索只验证候选生成与稳定排序，尚未接入 Agent、引用生成或 trace exporter。
- Top-K 的用户界面配置与其他 Agent 运行参数一起延后，见 [BL-0004](../backlog.md#候选事项)；017 继续使用默认 `top_k=5`。
