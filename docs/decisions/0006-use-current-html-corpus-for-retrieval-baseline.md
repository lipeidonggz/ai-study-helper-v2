# 0006 · 使用当前 HTML 入库快照作为检索基线

- 状态：已接受
- 决定日期：2026-10-21
- 影响范围：产品用例示例、检索策略验证、评测台测试环境

## 背景

原设计把首批 10 篇文章定义为冻版评测语料。其中 OpenAI《A Practical Guide to Building Agents》只有 PDF，没有可直接入库的 HTML 来源。

当前首版入库明确只支持本地 HTML 和公开静态／SSR HTML URL，不执行 JavaScript，也不解析 PDF。为了给原 10 篇中缺失的 PDF 文章留位置而等待 PDF 能力，会阻塞检索与评测实现；把 PDF 临时转成 HTML，又会改变原始来源和冻版依据。

用户已完成当前 HTML 素材入库。活动 collection 为：

- snapshot ID：`s_896a7ab08b84f25f`
- 文档数：15
- 来源：全部为本地 HTML

## 候选方案

| 方案 | 结论 | 说明 |
| --- | --- | --- |
| A. 继续等待原 10 篇齐全 | 不采用 | 会阻塞检索评测；PDF 支持不属于当前入库范围 |
| B. 将 PDF 转成 HTML 后入库 | 不采用 | 转换会改变来源 artifact，破坏引用、抓取时间和内容 hash 的可追溯性 |
| C. 以当前 15 篇 HTML 活动快照作为检索基线 | 采用 | 与当前产品能力一致，可立即开展检索、Agent 和评测工作 |

## 决策

后续检索、Agent 和评测的第一版基础数据使用 snapshot `s_896a7ab08b84f25f` 中的 15 篇 HTML 文档：

1. Anthropic《Building Effective Agents》
2. Anthropic《How we built our multi-agent research system》
3. Anthropic《Effective context engineering for AI agents》
4. Anthropic《Effective harnesses for long-running agents》
5. Anthropic《How we contain Claude》
6. Anthropic《Best practices for Claude Code》
7. OpenAI《Harness engineering》
8. OpenAI《New tools for building agents》
9. Lilian Weng《LLM Powered Autonomous Agents》
10. Lilian Weng《Harness Engineering for Self-Improvement》
11. Lilian Weng《2025: The year in LLMs》
12. Chip Huyen《Agents》
13. Eugene Yan《Patterns for Building LLM-based Systems & Products》
14. Hamel《Your AI Product Needs Evals》
15. Hamel《AI Evals: Everything You Need to Know》

OpenAI《A Practical Guide to Building Agents》不进入当前基线。未来增加 PDF 支持后，也不得静默补入当前快照；若要纳入，必须形成新的冻版 collection 快照并重新校准评测基线。

## 对用例的影响

- `cf-003` 的“四篇要点”改为当前库内四篇：Anthropic《Building Effective Agents》、OpenAI《New tools for building agents》、OpenAI《Harness engineering》、Anthropic《How we contain Claude》。
- `cf-001` 改为跨 OpenAI／Anthropic 的指代消歧：题面使用“刚才那篇”，但会话没有对应前文。答案必须让用户看见实际采用的文档和推断依据；为回答宽泛主题，一侧采用多篇相关文档可以接受。
- 以《How we contain Claude》和《Harness engineering》为原文锚点的主用例保持不变，这两篇都在当前快照中。

## 后果

- 检索验证规模从 10 篇变为 15 篇，干扰文档更丰富，更接近真实单知识库。
- 快照 ID 成为评测现场身份；任何文档增删、版本替换或重新入库都会形成新快照，不能继续与当前基线逐条比较。
- PDF 入库成为独立未来能力，不与本基线混用。

## 验证

- 读取 active collection manifest，确认 snapshot 为 `s_896a7ab08b84f25f`、文档数为 15。
- 15 篇均可从当前活动 collection 的 manifest 和文档详情 API 读取。
- 当前入库适配器只注册 HTML，PDF 不在当前支持范围内。

## 推翻条件

- 入库侧正式支持 PDF，并完成来源、版本、切块和引用语义设计；
- 用户提供原缺 PDF 文章的官方 HTML 来源；
- 评测需要切换到另一套明确命名和冻版的 collection。

发生任一情况时，应创建新的 snapshot／revision，并更新 03、05、08 及相关基线，而不是原地改写本快照的结论。
