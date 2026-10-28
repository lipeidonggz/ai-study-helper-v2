# 2026-10-28 · c8 chat-v14 跨来源综合校准

## 目的

验证一条不含 c8 专用事实的全局综合规则能否改善 c8 的最终答案覆盖：

- 多来源综合不能只保留与自拟主线一致的机制；
- 可以压缩和抽象，但不能删除主体、动作、因果或增长方向、限制条件；
- 不要求覆盖所有细节，但回答当前问题所需、能区分来源贡献的关键机制不能静默丢失；
- 输出结构服从问题，不强制统一模板。

## 固定现场

| 项目 | 值 |
| --- | --- |
| case revision | `cr_c8_001_7c3d909b2b3a` |
| R2 criterion revision | `ccr_c8_001_r2_4bac84af9a` |
| collection snapshot | `s_6b787258ba7d700c` |
| model | `deepseek-flash` |
| temperature | `0.0` |
| thinking | disabled |
| prompt bundle | `chat-v14` |
| prompt hash | `6d73d0d9db11f10af041d4a1306db0d9e7400a869d4596f1b841b92e40a061be` |
| tool contract | `retrieval-tools-v5` |
| N／K | 10／9 |
| concurrency | 10 |
| token budget | 5,000,000 |
| model request budget | 2,000 |

`chat-v14` 相对 `chat-v13` 只增加 `<cross_source_synthesis>` 段；历史 case、快照、模型、预算和工具契约不变。

## 运行结果

- Plan：`plan_f83f0eedd4dc4415b43a51c9a03e5b44`
- Run：`run_31b8b1cfaf6441b5977276d359aef6c7`
- Judgment revision：`evalrev_c021e7f8b0f2499ab6149a9c74483df0`
- Agent attempt：10/10 成功，0 infrastructure failure，0 partial，0 failed
- `citation_integrity`：10/10 pass
- criterion 总矩阵：32 pass、7 fail、1 pending
- case：2 pass、7 fail、1 pending，未达到 K=9

7 个 fail 全部来自 R2，且都缺少 OpenAI 的 agent 互审反馈回路；1 个 pending 是判官输出非法，不是 Agent 运行失败。即使该 pending 最终通过，也最多 3/10。

## trace 诊断

10/10 attempt 都执行了相同的四条工具链：

1. `list_documents`
2. `get_outline`
3. `list_blocks` 读取 OpenAI《Harness engineering》
4. `list_blocks` 读取 Anthropic《How we contain Claude》

四个工具结果全部 `admitted=true`。每次运行的 45 个证据块中都同时包含：

- `request additional specific agent reviews`、`agent-to-agent` 等 OpenAI agent 互审原文；
- `only grows as capabilities and access expand` 等 Anthropic 爆炸半径关系原文。

因此这不是检索或上下文预算问题。本实验只证明：即使原文进入上下文，通用提示词也不能保证模型主动展开这个具体实现；后续则确认这个具体实现不应作为 c8 的独立必过要求。

## 结论

- 这条通用的跨来源综合提示词没有让模型稳定写出 agent-to-agent 评审。
- 新版规则没有破坏 R1、R3、X2 或 citation integrity。
- 后续复查表明：agent 互审是通用反馈回路的一个具体实现，不是 c8“整体图景”的独立必过项；模型在用户直接询问注意力治理时能够正确回答该机制。
- 因此本实验没有证明 Agent 存在该知识或表达能力缺口。以它为依据继续修改全局提示词会把过细的判据污染到所有问题。
- 决定：撤回 `chat-v14`，保留 `chat-v13` Agent 基线；c8 R2 改为 7 个整体图景核心概念。

## 复现

- 执行脚本：`backend/.runtime/run_c8_v14.py`
- 结果文件：`backend/.runtime/c8-v14-result.json`
- trace 诊断：`backend/.runtime/diagnose_c8_agent_gap.py --run-id run_31b8b1cfaf6441b5977276d359aef6c7`

自动化验证：

- `pytest backend/tests -q --basetemp backend/.runtime/pytest-049-full`：通过
- `corepack pnpm quality:quick`：通过
- `git diff --check`：通过
