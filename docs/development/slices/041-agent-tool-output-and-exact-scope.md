# 041 Agent 工具输出契约与精确读取范围

- 状态：已完成
- 开始日期：2026-10-27
- 关联设计：[06 检索与纯 Agent](../design/06-retrieval-pure-agent.md)、[ADR 0017](../../decisions/0017-exact-block-read-and-explicit-section-scope.md)
- 前置切片：[038 引用证据全文覆盖截断片段](./038-citation-evidence-full-text-merge.md)、[040 Agent 通用证据作用域](./040-agent-evidence-scope.md)
- 人工验收：沛东复核工具 schema、真实 c3-001 trace 和章节范围行为

## 控制契约

### 用户可观察目标

Agent 能区分“扫描命中后补全一个 block”“完整读取整篇文档”和“读取指定章节”三种请求；
模型能看到每个工具的输入与 payload 输出契约，不再依赖自然语言描述猜测返回字段。

### 核心不变量

1. `scan_term` 只负责定位并返回 snippet；需要全文时必须通过 `get_blocks(block_ids)` 精确补全。
2. `search_blocks` 返回完整 block；`list_blocks` 不传章节时读取整篇。
3. `list_blocks` 传章节时必须显式选择 own 或 subtree，不能静默把根章节等同于整篇。
4. 每个工具的实际 payload 在进入模型上下文前都经过对应结构校验；成功结构与公共错误结构都可被模型识别。
5. 任一请求的 block 或章节路径缺失时整次失败，不返回部分结果冒充完整结果。

### 明确禁止

- 不把 `c3-001`、93% 或其它具体用例事实写入提示词。
- 不通过增加提示词规则掩盖缺失的工具能力。
- 不在 block 缺失、章节路径缺失或 payload 校验失败时静默截断或降级。
- 不把 `harness_status=ok` 解释成工具业务成功。
- 不修改本切片的入库标题、前置正文归章或 block 粒度规则。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| `get_blocks` 含不存在的 block_id | 结构化 `scope_not_found`，不返回部分 block | 固定 collection 快照不变 | 是，Agent 可修正 ID 或说明缺失 |
| `list_blocks` 传章节但未指定 own/subtree | 结构化 `invalid_argument` | 文档与快照不变 | 是，Agent 可补全参数后重试 |
| 章节路径不存在 | 结构化 `scope_not_found` | 不返回同文档其它章节冒充结果 | 是，Agent 可从 outline 修正路径 |
| 工具 payload 不符合已发布结构 | `tool_internal_error` | 原始 payload 只进入诊断，不进入模型证据 | 否，需要修复实现 |
| 结果超过上下文预算 | 既有 `context_budget_exceeded` 语义 | 被拒绝结果不进入证据或 annotation | 是，Agent 应改用精确范围 |

### 人工检查点

- 编码前需要确认的契约或风险：`get_blocks` 方案、显式 own/subtree、模型可见 output schema。
- 实现中途需要查看的可运行路径：`scan_term → get_blocks → finish` 不再读取整篇。
- 完成后需要人工执行的操作：在评测台运行 `c3-001`，检查 trace 中的工具选择、范围和实际返回大小。

### 决策预算

- 预计 B 级决策数量：3。
- 是否存在 C 级决策：否。
- 超过预算时的拆分方式：工具输出契约、范围读取和入库章节修复拆成独立切片；本切片不扩展入库规则。

## 目标

1. 为 `ToolDescriptor` 增加模型可见的 `output_schema`。
2. 为 adapter 最终 payload 建立 Pydantic 边界并校验。
3. 新增 `get_blocks`，按 block_id 精确返回完整正文。
4. 为 `list_blocks` 增加显式 `include_descendants` 参数和范围结果。
5. 调整 Agent 提示词，使工具选择与上述边界一致。
6. 补充 trace 诊断，区分整篇、own section 和 subtree。
7. 为完整 block 和 scan match 就地提供最小 `section_context`。

## 输入与输出

`get_blocks`：

```json
{
  "block_ids": ["doc_x#v0001#c1#b00002"]
}
```

```json
{
  "blocks": [
    {
      "block_id": "doc_x#v0001#c1#b00002",
      "doc_id": "doc_x",
      "doc_title": "Document",
      "source_label": "Local",
      "section_path": ["Document"],
      "section_context": {
        "section_depth": 1,
        "has_child_sections": true,
        "document_section_count": 16
      },
      "text": "Full block text"
    }
  ],
  "total": 1
}
```

`list_blocks`：

```json
{"doc_id": "doc_x"}
```

表示整篇。

```json
{
  "doc_id": "doc_x",
  "section_paths": [["Document", "Section"]],
  "include_descendants": false
}
```

只返回 path 完全相等的章节直属 block。

```json
{
  "doc_id": "doc_x",
  "section_paths": [["Document", "Section"]],
  "include_descendants": true
}
```

返回该章节及全部后代。

## 不包含

- 535 等具体用例提示词。
- 引用判官、annotation 和“一句多引用”边界。
- `og:title` 与 H1 重复建章。
- 前置正文归章和 block 粒度调整。
- 全量评测回归。

## 验收条件

- [x] 工具描述同时渲染输入与输出 schema。
- [x] 所有已注册工具的最终 payload 经过结构校验。
- [x] `get_blocks` 精确补全扫描命中块，不读取同章节其它块。
- [x] `list_blocks` 能区分整篇、own section 和 subtree。
- [x] 章节参数缺少范围选择时返回 `invalid_argument`。
- [x] 不存在的 block 或章节路径返回 `scope_not_found`，不返回部分结果。
- [x] Agent 提示词包含通用工具选择规则且不含用例数值。
- [x] 完整 block 和 scan match 随附确定性 `section_context`。
- [x] 定向自动化测试与 `quality:quick` 通过。
- [x] `c3-001` N=10 的真实 trace 可解释。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 扫描命中后如何补全全文 | 新增 `get_blocks(block_ids)` | scan 负责定位，精确读取必须有独立原语 | 让 scan 直接返回全文会放大所有命中的上下文；按章节读取仍可能过宽 | 工具集合、trace 与 annotation 证据来源 | 单元测试与 c3-001 trace | 已确认 |
| D2 | B | 章节选择是否包含后代 | 章节非空时强制传入 `include_descendants` | 已有“不传章节＝整篇”，根章节不能静默复刻整篇 | 固定 subtree 默认会继续产生隐式过读；默认 own 又可能漏读子章节 | `list_blocks` 接口与模型调用 | own/subtree/整篇对照测试 | 已确认 |
| D3 | B | 模型如何获知 payload 结构 | 工具描述渲染压缩 output schema，并在 adapter 边界校验 | 系统提示已声明 payload 由工具定义决定，但旧 descriptor 没有输出契约 | 只写自然语言会漂移；直接暴露内部模型会混入不必要的内部字段 | 提示词大小、错误语义与版本 | schema 渲染和 payload 校验测试 | 已确认 |
| D4 | B | 章节结构上下文如何关联到证据 | 每个 block／scan match 就地携带 `section_context` | 模型需要在证据旁直接判断当前章节位置 | 顶层 contexts 去重更省 token，但要求远端关联，可靠性不足 | 每个 block 增加三个稳定字段和少量重复 token | 根节、叶子节、无标题文档和 scan 测试 | 已确认 |

## 证据包

### 自动化证据

- 命令：`python -m pytest backend/tests/test_agent_protocol.py backend/tests/test_retrieval_read_layer.py backend/tests/test_agent_runtime.py -q --basetemp .pytest-t41-final-targeted`
- 结果：76 passed。
- 命令：`corepack pnpm quality:quick`
- 结果：通过，Ruff、Pyright、Vue TypeScript、Vite 构建和 Rust fmt/check/clippy 均通过。
- 覆盖的不变量：输出 schema、精确 block 读取、own／subtree／整篇范围、参数和缺失范围错误。

### 运行证据

- 启动方式：使用产品运行时设置和当前活动 snapshot `s_6b787258ba7d700c`，执行 `c3-001` quick plan。
- 用户操作序列：`c3-001` N=10，随后启动当前 `citation-rubric-v11` 判定。
- 预期结果：`scan_term` 仅定位；需要全文时调用 `get_blocks`；按章节读取显式选择范围。
- 实际结果：
  - plan：`plan_cab88d6e553e40859d92387b6abe4b99`
  - run：`run_6da8b6ff9fe74ba1a93a2e38758cc312`
  - revision：`evalrev_a683d02b5e5f42e1a94f7bb247e2c61a`
  - attempt：10/10 succeeded，0 failed，0 partial。
  - criterion：50/50 pass。
  - citation support：10/10 pass。
  - 10/10 attempt 的工具序列都是 `scan_term → get_blocks`，没有整篇 `list_blocks`。
  - `get_blocks` 单次约 575 result tokens；旧 `list_blocks(root)` 路径约 11,526 tokens。
  - 3/10 回答仍写了“文档未识别出更细章节／整篇归在同一节”，说明范围修复成功，但模型仍会从根 `section_path` 推断章节数量。

### `section_context` 复跑

- run：`run_3460664b648c4a0cb1ead52113b1ad3b`
- revision：`evalrev_5c616e34bdda4caeb5a2026403102103`
- 版本：`chat-v12`、`retrieval-tools-v5`
- attempt：10/10 succeeded。
- criterion：50/50 pass。
- 工具序列：10/10 为 `scan_term → get_blocks`。
- 结构误述：从 3/10 降至 0/10。
- token：`scan_term` 约 263、`get_blocks` 约 608；结构字段带来约 33 tokens。
- citation support：7 pass、3 pending unknown；三个 unknown 都是答案包含“共 16 个章节／位于顶层章节”等结构 claim，而 citation judge 当前只收到 block 正文，没有收到 `section_context`。

### 位置措辞约束复跑

- run：`run_dc4a0aaddad2446b9348ab8d52f28d35`
- revision：`evalrev_0d90dd09797a4fa582c59914a0b70c8d`
- 版本：`chat-v13`、`retrieval-tools-v5`
- attempt：10/10 succeeded。
- criterion：50/50 pass。
- citation support：10/10 pass。
- 工具序列：10/10 为 `scan_term → get_blocks`。
- “文章开头／最开始／开头部分”等不可核验位置措辞：0/10。
- 结论：不增加 `section_ordinal`，通过提示词约束 Agent 直接使用可核验章节名或 `section_path`，即可消除该结构误述和对应 citation pending。

### 失败探针

- 注入或复现的失败：待执行
- 系统保留的旧状态：固定 collection 快照不变
- 用户看到的错误：结构化公共错误

### 人工验收

- [x] 沛东确认结构误述与位置措辞复跑结果。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么 `scan_term` 后不能继续用根章节 `list_blocks` 补全文？ | 应使用 `get_blocks` 精确补全；根章节会展开整篇，超出模型当前需要的范围。 | 通过 | 无 |
| R2 | `section_paths` 省略与传入根 path 的结果为什么必须可区分？ | 省略表示整篇；传章节时由 `include_descendants` 明确选择直属块或子树。 | 通过 | 无 |
| R3 | output schema 已存在时，为什么运行时仍必须校验 payload？ | schema 声明与运行时实现是两处独立变化，只有实际校验才能保证 payload 符合声明。 | 通过 | 无 |
| R4 | `section_path` 与 `section_context` 分别有什么边界？ | `section_path` 只表示当前位置；结构字段来自 `section_context`，位置措辞必须直接使用可核验章节名，不能自行写成“文章开头”。 | 通过 | 无 |
| R5 | 为什么最终证据必须使用完整 block 而不是已拒绝或截断结果？ | 使用本次已进入 History 的完整证据；预算拒绝结果不可见，snippet 则由包含它的全文覆盖。 | 通过 | 无 |

<!-- reverse-explanation-gate: passed 5/5 -->

## 协作复盘

- AI 明显加速的环节：待填写
- 负责人失去系统模型的环节：待填写
- 真正发现问题的控制点：待填写
- 只有仪式感、没有产生判断价值的流程：待填写
- 下一次准备调整的一到两项：待填写

### 本次试行的流程规则

| 规则／假设 | 来源 | 想防住的失败 | 使用成本 | 实际效果 | 下次决定 |
| --- | --- | --- | --- | --- | --- |
| 先固定工具 I/O 契约再调提示词 | 本项目 040 与 c3-001 实验 | 用提示词补工具结构缺口 | 中等 | 待验证 | 待复核 |

## 已知风险与后续

- 未验证项：模型是否稳定优先选择 `get_blocks`。
- 接受的风险：工具 schema 会增加系统提示词长度。
- 结构 claim 不再依赖判官读取 `section_context`：`chat-v13` 要求 Agent 只使用章节名／`section_path` 和正文可核验内容描述位置，复跑后 citation support 为 10/10 pass。
- 后续待办或 ADR：ADR 0017；章节树标题消费与前置正文归章另立切片。
