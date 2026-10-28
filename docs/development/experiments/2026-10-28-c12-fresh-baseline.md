# 2026-10-28 · c12 新数据基线

## 目的

使用当前产品配置重新执行 c12-001 N=10，验证 Agent 能否在文档只提到 gVisor 名称和可靠性判断时，补足题目直接要求的通用定义，并正确说明原文依据。

## 固定现场

| 项目 | 值 |
| --- | --- |
| case revision | `cr_c12_001_minimal_judge_v2` |
| question | Anthropic《How we contain Claude》提到的 gVisor 是什么？凭什么它比自建代理更可靠？ |
| collection snapshot | `s_6b787258ba7d700c` |
| model | `deepseek-flash` |
| temperature | `0.0` |
| thinking | disabled |
| prompt bundle | `chat-v13` |
| prompt hash | `0ae72288d45eb7bfbede0aae0a4a259bc9809e49733eb58f15c912d0ce256e95` |
| tool contract | `retrieval-tools-v5` |
| N／K | 10／9 |
| concurrency | 10 |
| token budget | 5,000,000 |
| model request budget | 2,000 |

## 运行证据

- Plan：`plan_161f7485b4fa49fbbdff6562bd39c354`
- Run：`run_c057c0c0cf694b91a37c5a994529c487`
- Judgment revision：`evalrev_5098c8ad67334e74b5379098043f2fae`
- Agent attempt：10/10 succeeded
- infrastructure failure：0
- partial：0
- failed：0
- model calls：50
- input tokens：234,864
- output tokens：23,618

## 判定结果

- criterion：30/40 pass，10 fail，0 pending
- case：0/10 pass，未达到 K=9
- R2、X1、X2：全部通过
- 10 个 fail 全部是 `c12-001-r1`
- X 红线违规：0
- citation integrity：10/10 pass
- citation support：无 pending／check failure；软质量中 8 次 good、1 次 review、1 次 insufficient sample

## 失败分析

10 个答案都能说明：

- gVisor 在原文中是 Anthropic 使用的容器级沙箱或隔离层；
- gVisor、seccomp、hypervisor 等成熟原语经历了更长时间的对抗检验；
- 自建 allowlist proxy 是实际事故中的薄弱环节；
- 文档没有声称做过 gVisor 与自建代理的受控对照实验。

但 10 个答案都没有说明 gVisor 的核心机制：它是在容器与宿主内核之间增加隔离层的容器沙箱运行时。多数答案明确写“文档没有给出技术定义”，然后停在原文用法层面。

该用例 R1 备注已经说明：gVisor 的通用解释可以由模型内置知识提供。问题不是原文缺失，而是 Agent 没有区分：

- 文档事实必须由工具块支持；
- 用户直接问“X 是什么”时，可以在不冒充原文的前提下，用模型知识补一个简短、通用的定义。

## 结论

`chat-v13` 首次运行未达到 K=9。失败根因是回答策略问题，不是检索、引用或 R1 标准错误。

## 后续建议

应讨论一条通用答案规则：当问题直接询问某个术语“是什么”，而证据只给出它在文档中的角色时，Agent 应：

1. 先用模型内置知识给出简短通用定义，并明确不是原文引述；
2. 再说明该术语在文档中的具体用法和边界；
3. 不得用“文档没定义”替代用户直接要求的定义。

## chat-v15 修正验证

`chat-v15` 增加通用知识与文档事实边界后，使用相同 snapshot、模型和预算重新生成 N=10：

- Run：`run_51145dd049d3404488dd01f0c6db0097`
- Judgment：`evalrev_45677ad772cf43278ee165325ea24f46`
- Agent：10/10 succeeded
- criterion：40/40 pass
- case：10/10 pass，达到 K=9
- citation integrity：10/10 pass

所有通过答案都明确区分了：

- gVisor 的通用定义：容器运行时沙箱，通过用户态内核拦截系统调用，在容器与宿主内核之间增加隔离层；
- 文档中的具体用法和可靠性依据；
- 哪些是通用背景、哪些是原文事实。

## 回归

- c10：`run_2dd47621ce004b0aa3cad79f6a2e7672`，10/10 pass，50/50 criterion。
- c11：`run_403ea517f3fb4501afb9a492de494977`，10/10 pass，40/40 criterion。

## 最终结论

`chat-v15` 解决了 c12 的通用定义缺口，且 c10、c11 回归未发现新增失败。

## 复现

- 执行脚本：`backend/.runtime/run_case_fresh.py --case-id c12-001`
- 结果文件：`backend/.runtime/c12-fresh-result.json`
