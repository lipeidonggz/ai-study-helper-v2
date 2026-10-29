# 2026-10-29 · c14 新数据基线

## 目的

使用当前产品执行链重新执行 c14-001 N=10，验证 Agent 能否区分：

- “MCP 属于 agent 可触及的外部内容／外部风险来源”有原文依据；
- “新的攻击面”这一限定是答案概括，不是原文直接定性。

## 固定现场

| 项目 | 值 |
| --- | --- |
| case revision | `cr_c14_001_minimal_judge_v2` |
| question | "MCP 是新的攻击面"这个说法，在 Anthropic《How we contain Claude》里有原文依据吗？还是模型补充的？ |
| collection snapshot | `s_6b787258ba7d700c` |
| model | `deepseek-flash` |
| temperature | `0.0` |
| thinking | disabled |
| prompt bundle | `chat-v15` |
| prompt hash | `3d52504d28d999d70ba3070bb2f78e26166d0c57538d703f53f29d646bb67220` |
| tool contract | `retrieval-tools-v5` |
| N／K | 10／9 |
| concurrency | 10 |

## 运行证据

- Plan：`plan_4d12293774ee49d5b6b845af9eae7e7a`
- Run：`run_386d819e7c54443e83665c3f0f4166b3`
- Judgment revision：`evalrev_3a6faeacb6274e2b9626565f5ad35e13`
- Agent attempt：10/10 succeeded
- infrastructure failure：0
- partial：0
- failed：0
- model calls：61
- input tokens：282,744
- output tokens：16,685

## 判定结果

- criterion：20 pass、20 fail、0 pending
- case：0/10 pass，未达到 K=9
- R 分布：
  - R1：2 pass、8 fail
  - R2：8 pass、2 fail
  - R3：0 pass、10 fail
- X1：10/10 `not_violated`
- citation integrity：10/10 pass
- citation support：多数为 `insufficient_sample` 且样本内全部 supported；一个 attempt 为 1/2 supported

## 工具路径

10/10 都是同一路径：

```text
list_documents
→ scan_term("attack surface")
→ scan_term("MCP")
→ finish
```

所有工具结果均被 Harness 准入，没有预算失败。10 次都没有调用 `get_blocks`，也没有调用 `search_blocks` 或 `list_blocks`。

## 初步根因

### R3：10/10 是同一种停止过早

`scan_term("MCP")` 命中 3 个 block。其中 block
`doc_fd4dec9976e041308e2b39ed797c75d1#v0001#c1#b00019`
的完整原文包含：

> Any external resource provided to an agent represents two risks at once:
> a code execution risk, in the traditional supply-chain sense, and a prompt
> injection vector.

`scan_term` 的 snippet 只返回了以 MCP 为中心的句界上下文，没有把紧随其后的 R3 句子放进 prompt。提示词明确要求 snippet 只用于定位，需要完整正文时调用 `get_blocks`；10 个 attempt 都没有继续读取完整 block。

因此，R3 失败不是原文不存在，也不是检索工具没有命中，而是 Agent 在字面扫描后过早结束。

### R1：分层归属表达不稳定

8 个失败答案通常都复述了“MCP 服务器与第三方插件、网页搜索工具并列为不可控外部内容来源”，但仍以“没有原文依据”作为总答案，没有明确拆成：

1. 核心风险归属有依据；
2. “新的攻击面”这一限定没有依据。

2 个通过答案完成了这两层切分。

### R2：大多已定位归类

8 个答案明确指出 MCP 与第三方插件、网页搜索工具同属 agent 可触及的外部内容，并说明来源不可控。2 个失败答案没有完整指出并列关系和不可控性。

## 删除 R3 后冻结重判

负责人确认 R3 属于另一个问题“外部资源有哪些风险”，不属于 c14 的来源归属目标，因此删除 R3，保留 R1、R2、X1。

- 新 case revision：`cr_c14_001_022feb967323`
- 判官：`judge-rubric-v3`
- Judgment revision：`evalrev_f346eeb3118b4fc58078403de21525c9`
- criterion：29 pass、1 fail、0 pending
- case：9 pass、1 fail、0 pending，达到 K=9
- X1：10/10 `not_violated`
- 唯一失败：`attempt_cb87702f...` 的 R2 未说明 MCP 与第三方插件、网页搜索工具的并列归类及不可完全控制
- active suites：
  - `sr_design03_f2cf0c264381`
  - `sr_除截断用例外所有_b8f6d46c3322`

该结果没有重跑 Agent，只说明原 10 个答案在删除 R3 后的新判据下达到 K=9。

需要注意，旧 v2 对 R1 为 2 pass、8 fail，新 v3 对同一批答案的 R1 为 10 pass。0/10 到 9/10 同时包含“删除 R3”和“使用 reason-first Judge v3”两个变化，不能全部归因于 R3。

## 收紧来源归属总判断

R3 删除后，9 个 pass 答案仍以“没有原文依据”或“纯模型补充”作为总判断，尽管正文承认 MCP 的外部风险来源定位有原文语义依据。为把这一用户可观察问题纳入系统评测：

- 设计基线 v0.9 收紧 c14 R1，要求最终总判断与分层归属一致；
- 新 case revision：`cr_c14_001_06a69f448d24`；
- 判官继续保持 Judge v3。

### 改提示词前：旧答案冻结重判

- Judgment revision：`evalrev_dde73f03d99d46dd84a13f52f1deca40`
- criterion：19 pass、11 fail、0 pending
- case：0/10
- R1：0 pass、10 fail
- R2：9 pass、1 fail
- X1：10/10 `not_violated`

10 个旧答案都识别了部分原文关系，但总判断仍把整句归为“无原文依据／模型补充”，因此全部触发新 R1。这说明收紧后的判据能够系统识别该问题，不依赖人工主观评价。

### 加入 chat-v16 通用提示词

新增三条 `<source_attribution>` 原则：

1. 把“是否有原句”“核心命题是否有原文支持”“哪些限定属于概括或推断”分别判断；
2. “不是原句”不等于“没有原文依据”；
3. 需要合并多个陈述或补充通用知识时，必须标为“基于原文的推断”或“模型补充”。

可靠性边界未加入。

### chat-v16 fresh c14

- Run：`run_3da3fa28c7514d64aabebf31af619b88`
- Prompt bundle：`chat-v16`
- Prompt hash：`681fe6c18081787ac678440e953fbb40826f2c39ef166dd461757e250b0cf49f`
- Judgment revision：`evalrev_672b16c427554cba80c29f9af926540a`
- Agent attempt：10/10 succeeded
- infrastructure failure：0
- criterion：29 pass、1 fail、0 pending
- case：9 pass、1 fail、0 pending，达到 K=9
- R1：10 pass
- R2：9 pass、1 fail
- X1：10/10 `not_violated`

唯一失败为 `attempt_cd726763...` 的 R2：答案列出了 MCP servers、第三方插件和网页搜索工具，但没有明确说明这些外部内容不能被完全控制。verdict 正确，但 v3 reason 同时称其未列出三类来源，reason 局部不准确。

本次没有执行全量用例回归；chat-v16 为全局提示词升级，整体产品结论仍待后续统一回归。

## 复现

- 执行脚本：`backend/.runtime/run_case_fresh.py --case-id c14-001`
- 重判脚本：`backend/.runtime/rejudge_case.py`
- 结果文件：`backend/.runtime/c14-fresh-result.json`
- 重判结果：`backend/.runtime/c14-rejudge-no-r3.json`
- 收紧后旧答案结果：`backend/.runtime/c14-rejudge-r1-consistency.json`
- chat-v16 结果：`backend/.runtime/c14-v16-fresh-result.json`
