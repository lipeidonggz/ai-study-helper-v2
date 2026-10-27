# 040 · Agent 通用证据作用域约束

- 状态：已完成
- 开始日期：2026-10-27
- 关联设计：[ADR 0016](../../decisions/0016-evidence-scope-cannot-exceed-tool-results.md)
- 前置切片：[038 引用证据全文覆盖截断片段](./038-citation-evidence-full-text-merge.md)

## 控制契约

### 用户可观察目标

Agent 不得把单条结果、局部枚举、Top-k 候选或单个对象的元数据，扩大为未返回范围的集合、唯一性或完整结构结论。

### 核心不变量

1. 工具结果只在其实际返回范围内提供证据。
2. 单条记录中的标签、位置和元数据只证明该记录自身。
3. 全集、唯一性、排他性和完整结构结论必须取得目标范围的完整枚举。
4. 无法取得完整枚举时，应省略全局结论或明确声明该部分尚未验证。
5. 不得用模型知识补全工具尚未返回的范围。

### 明确禁止

- 不增加具体数字、文档标题、章节名或问题句式。
- 不把“未返回”直接解释为“不存在”。
- 不把当前记录的局部属性外推为整个集合的属性。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | 如何阻止 Agent 从局部证据外推全集或完整结构 | 增加纯作用域不变量：证据覆盖范围不得小于结论影响范围 | 单个 block 的元数据不能证明完整目录或集合边界 | 写具体“不要判断章节唯一”会泄漏用例特性；只依赖模型常识又无法审计 | `chat-v9` system prompt | c3-001 N=10 前后对比 | 已确认 |

## 证据包

### 修改前

- Run：`run_37bf3e33d01f4b73bae7c4dbaa1b1af1`
- Revision：`evalrev_20cc2bace8c240efa8fed452da3db448`
- attempt：10/10 succeeded
- criterion：50/50 pass
- citation support：6 pass、4 structural unknown

### 修改后

- Run：`run_cafbe278332b4c69b9290fb875888d68`
- Revision：`evalrev_22d28430061a4094823f4b86a420e68a`
- attempt：10/10 succeeded
- criterion：49/50 pass
- citation support：9 pass、1 structural unknown
- `citation_evidence_quote_invalid`：0
- `agent_protocol_loop`：0

### 显式系统契约实验

随后移除抽象作用域长规则，改为两条系统契约：

1. 普通 block 的 `section_path` 只表示当前位置，不表示完整目录。
2. `get_outline` 的无标题 fallback 单独说明。

- Run：`run_f003be3f41f844e1b029e29a739e13d7`
- Revision：`evalrev_98dbb6a9f92a4dc88bd1066bce30430d`
- attempt：10/10 succeeded
- criterion：48/50 pass
- citation support：7 pass、1 semantic unknown、2 quote invalid
- `agent_protocol_loop`：0

两次 criterion fail 仍然都只调用了 `scan_term`，没有取得完整结构；其中一次把局部结果改写为“未识别出更细章节”，另一次未指出章节主题。该实验未优于前一轮，不能视为修复完成。

### 分析

- 通用作用域规则把结构 unknown 从 4 次降到 1 次，说明规则方向有效。
- 后续显式系统契约把结构 unknown 降到 1，但 criterion fail 增至 2，并重新出现 2 个 quote invalid；说明只加入字段语义说明仍不足以稳定约束工具选择和 annotation 证据完整性。
- 当前提示词约束仍是概率性的，没有形成确定性门禁。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么拒绝进入 History 的 block 不能成为最终证据？ | Agent 生成时没有看到这些 block，不能把它们补进答案证据。 | 通过 | 无 |
| R2 | 为什么单个 block 的 section_path 不能证明整篇结构？ | 它只描述当前 block 的位置，不包含整篇目录和章节数量。 | 通过 | 无 |
| R3 | 描述位置时为什么不能自行改写成“文章开头”？ | 该表述模糊且缺少工具证据，应直接使用可核验的章节名或 section_path。 | 通过 | 无 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 已知风险与后续

- 如果后续运行仍有同类稳定性问题，应评估结构信息是否必须由专用工具结果提供，而不是继续提高提示词强度。
- 当前不把模型知识、局部 `section_path` 或其他单记录元数据作为完整集合证据。
- 另一个独立问题：仅由 `scan_term.snippet` 形成的证据仍可能缺少 annotation 所需完整上下文，后续应评估由 Harness 按 block_id 补全全文，而不是继续调整 annotation prompt。
