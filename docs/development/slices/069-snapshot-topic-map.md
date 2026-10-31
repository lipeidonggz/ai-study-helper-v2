# 069 · Snapshot Topic Map

- 状态：已完成（初版实现与自动验证、集中反向解释通过）
- 开始日期：2026-10-31
- 关联设计：[11 LLM Wiki 编译设计](../../design/11-llm-wiki-compilation.md)
- 前置切片：016 检索读取层；017 混合检索
- 人工验收：最终与 070–074 集中进行

## 控制契约

### 背景与失败场景

- 触发问题：经典检索只能围绕当前 query 取候选，不能从冻结知识库中主动形成主题入口。
- 用户损失：开放主题回答看似完整，实际漏掉没有被 query 措辞覆盖的主题面。
- 本切片负责：把 snapshot 的全部 block 分批抽取为有证据的 `WikiCandidate`。
- 不负责：跨文档身份合并、构建索引、叙述页和 Agent 工具。

### 用户可观察目标

对一个冻结 snapshot 执行编译后，得到一份 topic map：每个候选都有名称、别名、摘要、可选语义标签，以及至少一个属于该 snapshot 的 source block。

### 核心不变量

1. 所有输入 block 都必须进入恰好一个处理 window，或使编译显式失败。
2. candidate 和 coverage slot 只能引用本 window 输入中的 block。
3. 无 block 证据的 candidate 不得进入输出。
4. 通用抽取提示词不能包含用例、目标 block 或测试答案。
5. 任一 window 失败时不发布部分 topic map。

### 明确禁止

- 不做整篇静默截断。
- 不把模型返回的自由文本当 JSON。
- 不硬分 `entity/concept/topic`；`semantic_tags` 只是非主键标签。
- 不用文档目录作为全覆盖证明。

### 失败语义

| 场景 | 错误码 | 行为 |
| --- | --- | --- |
| snapshot 不可用 | `wiki_snapshot_unavailable` | 不启动 |
| block 未覆盖 | `topic_map_block_gap` | 整次失败 |
| 模型 JSON/字段非法 | `topic_map_response_invalid` | 整次失败 |
| candidate 无证据 | `topic_map_candidate_unsupported` | 整次失败 |
| 跨 window block 引用 | `topic_map_source_out_of_scope` | 整次失败 |

## 输入与输出

输入：snapshot ID、ChatProvider、抽取粒度和 window profile。

输出：

- `wiki/topic-maps/<snapshot_id>/<map_id>/manifest.json`；
- 每篇文档的候选、coverage slots、source refs；
- provider、prompt、schema、usage 和 hash。

## 验收矩阵

| ID | 行为 | 阻断阈值 | 层级 |
| --- | --- | --- | --- |
| 069-A1 | 所有 block 被覆盖 | gap=0 | 单元／集成 |
| 069-A2 | candidate 全有合法 block refs | 悬空=0 | 单元 |
| 069-A3 | 失败不留成功 map | partial map=0 | 单元 |
| 069-A4 | 固定 fake provider 产物稳定 | 两次 hash 相同 | 单元 |
| 069-A5 | 通用 prompt 无案例专属信息 | 命中=0 | 静态 |

## 决策账本

| ID | 等级 | 选择 | 状态 |
| --- | --- | --- | --- |
| 069-D1 | B | 按文档顺序组合 block window，保持 section context | 已确认（2026-11-01） |
| 069-D2 | B | topic map 先写独立 artifact，不提前建产品表 | 已确认（2026-11-01） |
| 069-D3 | B | window 结果只在内存中合并；artifact 持久化 per-doc normalized candidate map 与 usage | 已确认（2026-11-01） |
| 069-D4 | B | 仅 exact normalized name 在文档内做确定性合并 | 已确认（2026-11-01） |
| 069-D5 | B | 抽取出错整 build 失败，不保存部分 window | 已确认（2026-11-01） |
| 069-D6 | B | 同一文档最多 3 个 window 并发，结果按 window 顺序合并 | 已确认（2026-11-01） |

## 证据包与反向解释

- 自动化：`python -m pytest backend/tests/test_wiki_topic_map_identity.py`，16 项通过；含 100,000 block 分区测试和并发窗口输出顺序测试。
- 真实小样本：15 篇／526 blocks 编译为 420 个 candidates；最终 topic map `tm_b1dbd1934e56239c` 覆盖 526/526 blocks。topic-map 阶段共 54 次模型请求，输入 160,052 tokens、输出 126,987 tokens。
- 通用提示词静态门禁：`test_wiki_prompt_modules_do_not_contain_case_specific_probe_terms` 通过。
- 详细记录：[2026-11-01 LLM Wiki 编译实验](../experiments/2026-11-01-llm-wiki-compilation.md)。
- 反向解释：R1 block 如何保证无遗漏；R2 为什么 topic 不是抽取类型；R3 为什么失败不留 partial map。
- 集中反向解释：[R1–R5](../experiments/2026-11-01-llm-wiki-compilation.md#集中反向解释)，5/5 通过。

## 停止条件

如果无法从全库形成有证据的候选，或通用提示词在 held-out 主题上持续只返回泛化分类，则停止进入 070。

<!-- reverse-explanation-gate: passed 5/5 -->
