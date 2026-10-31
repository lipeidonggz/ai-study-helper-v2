# 2026-10-31 LLM Wiki 证据地图 c13-003 探索

## 目的

验证 068 的最小 Wiki 证据地图是否能在固定知识库快照上把隐藏命题面组织出来，并验证页面段落能否稳定回到原始 block。

这不是回答质量评测，也不是完整 Wiki 编译评测。本实验只回答：

1. 单页、全语料一次生成是否稳定；
2. 把证据面收敛到明确子主题后是否更稳定；
3. 三个已知探针是否进入生成页面，并能否找到对应 block；
4. 页面引用数量能否直接代表主题覆盖程度。

## 固定配置

| 项 | 值 |
| --- | --- |
| 原始问题 | 在当前知识库中，除了沙箱、虚拟机和出口控制，AI Agent 还有哪些隔离或控制边界？请分别说明各限制什么。 |
| 用例 | `c13-003` |
| collection snapshot | `s_6b787258ba7d700c` |
| snapshot manifest hash | `e0a4cd8e1a959e2e5039ce6206c243338d555ddffcb5826fccc76731680683a7` |
| 文档数 | 15 |
| block 数 | 526 |
| 章节数 | 328 |
| chat provider | `deepseek` / `deepseek-flash` |
| temperature | `0.0` |
| thinking | `disabled` |
| max output tokens | `8192` |
| 编译器 | `llm-wiki-evidence-map-v1` |
| 提示词 | `wiki-page-v1` |
| 提示词模板 hash | `9bbb307c84e72a7123f771cbd18ff478d14b644ab224c8fb9405d67590fe463f` |
| 复现脚本 | [`scripts/wiki_c13_probe.py`](../../../scripts/wiki_c13_probe.py) |

实验数据根复制自产品快照，位于 068 worktree 的忽略目录：

```text
backend/.runtime/wiki-c13-068/
```

Wiki 编译只写该实验根的 `wiki/`，不写产品 collection。

## 输入方式

第一组 `full`：把快照中的 526 个 block 全部交给一个页面。

第二组 `task-state`：只选择以下五个文档的 132 个 block：

- How we built our multi-agent research system
- Effective harnesses for long-running agents
- Best practices for Claude Code
- Effective context engineering for AI agents
- Harness Engineering for Self-Improvement

这五个文档是依据已知任务状态探针人工选择的，只用于测试“聚焦证据面下的页面 materialize”，不能作为主题发现、召回率或“找全能力”的证据。

页面问题固定为：

1. 除了沙箱、虚拟机和出口控制，AI Agent 还有哪些隔离或控制边界？
2. 每类边界分别限制什么？
3. 并行工作、任务状态与恢复过程受到哪些边界约束？

`task-state` 聚焦为：

1. 并行任务和工作区之间如何保持状态隔离？
2. 检查点、断点续跑和回退分别控制什么？
3. 如何验证恢复后的终态正确，而不是只检查过程？
4. 上下文、记忆和子 Agent 如何限制状态污染？

这些用例词只存在于实验请求中，没有写进通用提示词；因此不污染其他主题的页面生成。

## 实际提示词

系统提示词完整原文：

```text
你是知识库 Wiki 的单页编译器。你的任务是把给定证据块整理成一个可导航的主题页面。

必须遵守：
1. 只能使用用户消息中的 evidence_blocks，不得引入外部知识或猜测。
2. 每个段落必须给出 source_block_ids，且只能使用 evidence_blocks 中出现的 block_id。
3. 页面正文直接写证据支持的内容；不要输出成员清单来代替主题组织，不要虚构关系或结论。
4. 如果证据不足以形成页面，返回 {"paragraphs": []} 让程序显式失败。
5. 只返回 JSON 对象，结构必须是：
   {"paragraphs":[{"text":"...","source_block_ids":["..."]}]}
6. 不得增加结构外字段，不得用 Markdown 代码围栏包裹 JSON。
```

用户消息是实际 block 证据包，结构为：

```json
{
  "page": {
    "title": "...",
    "aliases": [],
    "coverage_questions": []
  },
  "evidence_blocks": [
    {
      "block_id": "...",
      "doc_title": "...",
      "section_path": [],
      "text": "..."
    }
  ]
}
```

完整证据包由固定 snapshot 机械展开，不在文档中重复 50 万字符正文。可依据 snapshot、页面请求和脚本重建；manifest 同时记录 `request_hash`。

## 结果

### 全语料单页

| 尝试 | 结果 | 候选 block | 页面引用 | 段数 | 输入 tokens | 输出 tokens | 三个探针被直接引用 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 成功；生成于加入 prompt/request hash 字段前 | 526 | 56 | 25 | 141,890 | 5,613 | worktree ✔；checkpoint 恢复 ✔；恢复正确性评估 ✘ |
| 2 | 失败：JSON 在 char 9,378 处损坏，未写 artifact | 526 | - | - | - | - | - |
| 3 | 失败：JSON 在 char 1,286 处损坏，未写 artifact | 526 | - | - | - | - | - |

第 1 次成功结果中，“恢复正确性评估”没有被目标 block 直接引用，但页面第 17 段、第 22–24 段已经明确讲到了验证器、guardrail/evaluator 区别和 CI 回归。因此直接把“目标 block 是否被引用”当成覆盖失败会误判；它是引用命中探针，不是语义覆盖判据。

### 聚焦子主题页

| 尝试 | build | 候选 block | 页面引用 | 段数 | 输入 tokens | 输出 tokens | 三个探针被直接引用 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | `w_c04715a7835b3855` | 132 | 30 | 11 | 33,036 | 2,463 | 全部 ✔ |
| 2 | `w_a70995bcf472dcaf` | 132 | 24 | 10 | 33,036 | 2,169 | 全部 ✔ |
| 3 | `w_6b334d1a70a0d3ea` | 132 | 19 | 9 | 33,036 | 1,699 | 全部 ✔ |

三次均成功通过 artifact hash、manifest 和 block 回源校验，`page_id` 均为 `wp_dfbe3220541dc100`。

第一次聚焦页中：

- 第 0 段直接引用 worktree 目标 block，说明独立工作区与独立上下文窗口如何隔离并行任务；
- 第 2 段直接引用 checkpoint 恢复目标 block，区分中断、回退、进度持久化、重试和定期检查点；
- 第 4 段直接引用恢复正确性评估目标 block，明确“以终态而非过程验证”，并把复杂流程拆成离散检查点；
- 另有上下文压缩、外部笔记、子 Agent 摘要和同步/异步执行边界。

## 发现

### 1. 整库塞进一个页面不是可用形态

526 block、14 万输入 token 的单页在成功时确实能组织出较完整的主题内容，但相同输入 3 次只有 1 次形成合法 JSON。失败按设计被整次拒绝，没有产生伪成功 artifact。

结论：LLM Wiki 不能等同于“把全库交给一个大模型写一页”。

### 2. 聚焦证据面显著改善结构稳定性

132 block 的页面 3/3 成功，延迟约 15–20 秒，输出 1,699–2,463 tokens；全量页约 26–30 秒、输出更大。

结论：编译单位应是一个可解释的主题页或证据面，而不是整个知识库。

### 3. Wiki 能补出经典检索未稳定拿到的命题面

在同一固定快照下，只要页面范围覆盖任务状态相关文档，三个已知探针都能进入页面并被原始 block 支撑。页面不是“先找答案再包装”，而是先组织出“工作区、检查点、恢复正确性、上下文记忆、子 Agent”几个需要检查的主题位置。

这支持继续探索预编译路径，但不等于已经解决开放主题的完整枚举。

### 4. 引用数量不是覆盖率

聚焦页只引用 19–30 个候选 block，却覆盖了三个探针和上下文边界；未引用 block 仍可能是事实源的一部分。页面引用只证明段落有 provenance，不证明页面完成了主题枚举。

因此后续必须把两种指标分开：

- provenance：每段是否可回源；
- coverage：该主题应检查的证据面是否都被检查。

### 5. 最大未解问题从“回源”转移到了“页面规划”

本实验验证了页面回源底座，但页面来源仍是调用方显式选择的文档。真正决定“找全”的是：

1. 哪些页面应该存在；
2. 每个页面应覆盖哪些问题；
3. 一页的证据从哪里来；
4. 当语料没有现成语义标题时，如何从正文发现页面和别名。

下一步应优先验证页面目录／问题面的生成与回灌，而不是继续美化单页正文。

## 可验证产物

聚焦页成功 artifact：

```text
backend/.runtime/wiki-c13-068/wiki/s_6b787258ba7d700c/w_c04715a7835b3855/
```

| 项 | 值 |
| --- | --- |
| page | `wp_dfbe3220541dc100` |
| request hash | `3a057e6a4ce991f8dc1274acb0aef0be9cc6ba30f79686586a81102a69e3e508` |
| manifest hash | `40a8388fae336a98e9ee5d48b758ca8d551608e2552e689c82e98708093e7d61` |
| page artifact hash | `d3a1ee51f24469c8ef5fbdf872b73296224a0a5e87322be2cd1424120b71fbc7` |

这些是本地实验 artifact，不进入 Git。重新执行 `scripts/wiki_c13_probe.py` 会生成新的 build；同一输入和提示词版本下页面身份稳定。

## 复现

```powershell
backend\.venv\Scripts\python.exe scripts\wiki_c13_probe.py --mode full --summary backend\.runtime\wiki-c13-068-summary.json
backend\.venv\Scripts\python.exe scripts\wiki_c13_probe.py --mode task-state --summary backend\.runtime\wiki-c13-068-task-state-summary.json
```

## 当前结论与边界

支持继续做 LLM Wiki 的最小闭环：

- 页面段落可稳定回源；
- 聚焦主题页可以组织出跨文档、跨章节的隐藏命题面；
- 聚焦页比全库单页更稳定、更便宜。

这里的“聚焦”是人工给定的实验条件，不是系统自动发现的结果。完整主题发现与页面规划见 [11 号设计](../../design/11-llm-wiki-compilation.md)。

尚未证明：

- 自动生成完整页面目录；
- 对开放主题枚举真正完整；
- 页面摘要的事实正确性；
- Wiki 接入主 Agent 后的回答增益；
- 10 万 block 规模下的编译成本与增量维护。
