# 023A · 程序 Checker 与 LLM Judge

- 状态：已完成
- 开始日期：2026-10-23
- 关联设计：[03 用例与金标准](../../design/03-cases-and-gold-standard.md)、[08 评测台设计](../../design/08-evaluation-console-design.md)
- 前置切片：[022A 评测执行内核](./022-evaluation-execution-core.md)、[022B 用例编写](./022B-evaluation-authoring.md)
- 人工验收：[023A 判官人工验收](../manual-tests/023A-evaluation-judgment.md)

## 控制契约

### 用户可观察目标

1. 对已封存或已取消但仍有有效 attempts 的 ExecutionRun 启动一次判定。
2. 每个有效产品样本 `attempt + criterion revision` 产生一条 append-only Judgment。
3. 确定性 criteria 由 checker 判定，语义 criteria 由固定版本的 LLM Judge 判定。
4. 用户能查看判定进度、原始原因、证据 block、模型版本、usage 和 pending／失败原因。
5. 判定失败不得伪装成 criterion fail，也不得阻断其他判定任务。

### 核心不变量

1. LLM Judge 是语义 criteria 的默认执行者；human 只用于后续覆盖。
2. JudgeInput 使用白名单投影，不包含其他判定、通过率、基线、自动诊断或人工意见。
3. prompt、模型配置、checker 版本、criterion revision 和输入投影必须随判定记录固化。
4. Judgment 只追加不覆盖；模型失败、schema 非法和证据不足记为 pending／infrastructure failure。
5. 023A 不生成 N/K 聚合、最终 case pass/fail 或基线资格。

### 明确禁止

- 不把 Judge prompt、金标准或目标文档注入 Agent 执行输入；
- 不把 checker 能确定判断的规则交给 LLM；
- 不把基础设施失败 attempt 提交给 Judge；
- 不把模型调用失败记作 criterion fail；
- 不根据模型自然语言自由选择“大概通过”；
- 不在 023A 实现 human override、EvaluationRevision 聚合、结果矩阵或基线。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| Judge 输入缺少证据 | `pending` / `judge_evidence_missing` | 可见输入摘要和错误 | 可从有效 trace 重试 |
| Judge 输出 schema 非法 | `pending` / `judge_output_invalid` | 原始模型输出 artifact | 是，创建新 Judgment |
| Judge 上游失败 | `pending` / `judge_upstream_unavailable` | 错误和 usage | 是，有界重试 |
| Checker 失败 | checker error，不判成 fail | criterion 与执行输入 | 修复 checker 后重判 |
| Agent 产品行为失败（包括空答案） | 计入有效样本并产生逐 criterion Judgment | 原 attempt、answer 或空答案、trace | 否，不以替代样本掩盖 |
| attempt 属于基础设施失败 | 不产生 Judgment、不计有效 N | 原 attempt 与失败码 | 是，创建显式替代 attempt |

### 判定执行者

| 规则类型 | 默认执行者 |
| --- | --- |
| 精确格式、引用存在性、截断声明等确定性规则 | `checker` |
| 语义覆盖、归因、冲突、整合、边界判断 | `llm_judge` |
| 人工纠错 | `human`，023B 实现 |

### 人工检查点

- 确认全部 active criteria 的 evaluator 分类；
- 确认 JudgeInput 白名单投影；
- 审阅 `judge-rubric-v2`、`citation-rubric-v2` 与 `chat-v3` finalization 行为；
- 检查成功、pending、非法输出和上游失败现场。

### 决策预算

- 预计 B 级决策：5 项；
- C 级决策：无；
- 超过预算时拆分为 023A1 checker／Judgment 与 023A2 LLM Judge。

## 目标

建立统一 Judgment 事实源和可验证的 checker／LLM Judge 执行链，把 answer 与 evidence 转换为逐 criterion 的三态结论。

## 输入与输出

输入：ExecutionRun、Attempt、trace、CaseRevision、CriterionRevision、checker／judge 版本和模型配置。

输出：judge invocation artifacts、append-only Judgment、EvaluationRevision 判定容器、进度和失败现场。

## 不包含

- human override 与 effective judgment binding；
- N/K 聚合、case pass/fail；
- EvaluationRevision 完成／不完整报告；
- 结果矩阵、基线比较和回归；
- 判官校准工作台；
- 自动生成或修改 criteria。

## 验收条件

- [x] 所有 active criteria 的路由符合 checker／llm_judge 分类。
- [x] 全部产品结果 attempts（包括 failed 和空答案）产生逐 criterion Judgment。
- [x] LLM Judge 只接收白名单输入。
- [x] 输出 schema 非法、上游失败和证据不足保持 pending。
- [x] checker 与 LLM Judge 结果均可追溯版本和输入。
- [x] 重判追加新记录，不改写历史 Judgment。
- [x] 不产生 N/K 或正式 pass/fail。
- [x] 完整质量门和真实开发版人工验收通过。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | active criteria 的 evaluator 分类与迁移 | 语义 R/X 绑定 `llm_judge`；`cf-003 R1` 绑定 `checker:truncation_declared`；human 仅用于后续覆盖。旧 revision 保持历史，最终 active 版本由 D6／D8 固定 | 03／08；沛东 2026-10-23 确认 | 全部交 LLM 会浪费确定性检查能力；大量 checker 又难以覆盖语义金标准 | 决定 seed、criterion revision 与默认 suite | 18 cases／69 criteria 路由完整 | 已确认 |
| D2 | B | JudgeInput 证据投影 | 本地 JudgeInput 固定 question、answer、当前 criterion、attempt full trace 中的工具证据块、citation 标记、answer completeness／limitations；LLM 只接收最小白名单投影，不接收 checker 结果、其他 judgment、历史、基线或金标准答案。missing／partial trace、artifact 缺失或输入超限均 pending，不重新检索、不静默截断 | 08 白名单原则；沛东 2026-10-23 确认 question 是必要语境 | 全量 trace 直接发送会污染判定；仅发 answer 会失去覆盖与证据判断；question 不属于答案泄漏，是判卷所需题目 | 决定 JudgeInput builder、前置校验与 pending 语义 | 白名单、citation 解析、证据隔离、missing trace 测试 | 已确认 |
| D3 | B | Judge prompt／模型与输出 schema | 复用 DeepSeek chat provider／Key，固定当前 `deepseek-flash`、temperature=0、thinking 关闭、非流式；每次一个 attempt＋criterion；独立 `judge-rubric-v1`；R 输出 satisfied／unsatisfied／unknown，X 输出 violated／not_violated／unknown，并附 reason、evidence block IDs 和 evidence quotes。本地校验 block 归属与 quote 原文，非法输出 pending；原始输出、prompt hash、模型版本、usage、request ID 全部留痕 | 08 JudgeInput／版本化判官要求；沛东 2026-10-23 确认 | 自由文本无法可靠聚合；更高温度或 thinking 会增加抖动和成本 | 决定 Judge provider、prompt、schema 与 artifact | fake provider、schema、证据校验与失败测试 | 已确认；judge-rubric-v1 全文已于 2026-10-23 人工审核通过 |
| D4 | B | 判定任务与并发边界 | 只判定 sealed_complete／sealed_partial／canceled run；只提交 succeeded／partial、answer 非空且 trace complete 的 attempts。逐 attempt×criterion 提交全局 worker pool，默认并发 2、范围 1～8；一个 run 同时只有一个 judging EvaluationRevision，固定 checker／judge 版本、预算和超时。预算耗尽停止启动新任务并保留已完成结果 | 08 执行判定分离和硬预算要求；沛东 2026-10-23 确认 | 按 case 串行过慢；无界并发会放大成本和限流 | 决定 EvaluationRevision 状态、任务分配与预算暂停 | 可判定性、并发、预算和失败隔离测试 | 已确认 |
| D5 | B | Judgment 重试与不可变规则 | 每个 attempt×criterion×EvaluationRevision 最终绑定一条 Judgment；网络、429、5xx 最多调用 2 次，每次都保留 invocation artifact、usage 和错误。非法 JSON、证据不存在或 quote 不匹配直接 pending，不做自由修复追问。重判创建新 EvaluationRevision 和新 Judgment，禁止覆盖旧记录；human override 在 023B 追加 | 08 运行判定不可变和 append-only 原则；沛东 2026-10-23 确认 | 覆盖旧判定会破坏审计；无限重试会放大成本；把非法输出强行归一化会制造假结论 | 决定 invocation 历史、重试、pending 和重判语义 | artifact、retry、非法输出和 append-only 测试 | 已确认 |
| D6 | B | criterion 自包含与最终 revision | 18 个用例全部改写为不依赖问题、证据、运行状态或其他 criterion 的规则；删除 5 条重复判据，将 cf-003 拆成 checker 截断事实与 LLM Judge 截断声明。生成 18 个 `selfcontained_v1` case revision、69 个 criterion revision 和 active suite | 03 金标准编写规则；沛东逐条审核确认 | 保留问题／证据输入会掩盖 criterion 缺陷并放大 token；保留重复判据会重复计错 | 决定 03 v0.2 基线和评测数据库 active revision | seed 内容 18／69；数据库 active suite 与旧 revision 并存 | 已确认 |
| D7 | B | 主判官输入与引用职责 | 主判官只接收 answer 和当前 criterion，不接收 question、证据、运行状态、checker 结果或其他 judgment；输出只保留三态 verdict 和 reason。引用真实性拆成独立检查，失败层留给诊断。该决定替代 D2 的全量证据输入和 D3 的 evidence_block_ids／quotes 输出要求 | 沛东 2026-10-23 确认：criterion 必须自包含，判官不能依赖问题或证据补齐规则 | 继续发送全量和问题会稀释 criterion、增加 token，并掩盖判据缺陷；引用检查需要证据，但不能污染主判官 | 决定 `judge-rubric-v2` 输入、输出 schema 与调用边界 | 主判官、引用判官、UI、checker 分工和 229 条测试均完成 | 已确认 |
| D8 | B | 失败 attempt 的样本资格 | attempt 显式保存 `outcome_class`：产品行为结果为 `product`，计入有效 N 并进入全部 criterion 判定；只有供应商超时／限流／不可用、执行器异常、进程中断等明确基础设施故障记为 `infrastructure`，不计 N 且允许显式替代。空答案仍进入主判官，使用 `answer=""`；R1 等 checker 规则不改，R2 等语义 criterion 负责判空答案失败。trace 完整性与 execution status 分离记录 | 沛东 2026-10-23 确认：除基础设施失败外，所有用例失败都必须进入判官；R1 不需要增加空答案条件，由 R2 兜底 | 按 `execution_status=failed` 整体排除会隐藏预算耗尽、协议错误和静默截断，并通过自动替代制造选择偏差；把所有 failed 都视为基础设施又会掩盖产品失败 | 决定 attempt schema、N 计数、替代执行、判定资格和运行台样本展示 | outcome_class 迁移、空答案 Judgment、产品失败不替代、基础设施失败替代及 trace_status 分离测试 | 已确认 |

> D2／D3 保留为历史决策记录，其输入输出边界已由 D7 替代；D4 中“只提交 succeeded／partial”的样本资格口径由 D8 替代，不能再作为后续实现依据。

## 判据自包含修订清单

- 原则：主判官只接收当前答案和当前 criterion；若缺少问题、证据或运行状态就无法判断，视为 criterion 缺陷。
- 禁止跨 criterion 指代：每条都必须重复写出完整文章标题、对象和范围，不得使用未在本条内定义的“该文”“原文”“两篇”或“三者”。
- 禁止复述输入条件：用例问题已经固定，criterion 直接描述答案必须满足或不得出现的内容，不写“当用户……时”一类场景前缀；只有判断答案自身分支时才使用“如果答案……”。
- 状态：已完成。18／18 个用例、69 条 criterion 已确认并写入不可变 active revision。

### c1-001

- 状态：已确认
- R1：针对 Anthropic《How we contain Claude》是否提到 MCP，答案明确判断该文提到过 MCP；仅介绍 MCP、讨论其他文章，或只说 MCP 是什么，不算满足。
- R2：针对 Anthropic《How we contain Claude》中 MCP 的定位，答案说明 MCP servers 属于 agent 可触及的外部内容／外部资源之一，并与第三方插件、网页搜索工具并列；没有说明其所属类别，不算满足。
- X1：针对 Anthropic《How we contain Claude》中 MCP 的定位，答案不得把 MCP 称为“唯一”或“最大的攻击面”，也不得使用同义的最高级、排他性表述。把 MCP 仅列为攻击面或外部风险来源之一，不算违规。

### c2-001

- 状态：已确认
- R1：针对 Claude Code auto mode 在执行前拦下过度行为（overeager behaviors）的比例，答案给出约 83%，并保留“约／roughly”等不确定性；写成精确的 83.0%，或把这个比例归于其他指标，不算满足。
- R2：答案说明约 83% 是 Claude Code auto mode 在过度行为真正执行之前进行拦截的比例，而不是事后发现、攻击成功率或用户批准权限的比例；没有说明拦截发生在执行前，不算满足。
- X1：答案不得把“auto mode 在过度行为执行前的拦截率 83%”与“用户批准权限提示的比例 93%”对调、合并或混说。只同时提到两个数字且归属清楚，不算违规。
- X2：答案不得由约 83% 的拦截率推出“模型层已经足够”“可以取消环境边界”或“可以单靠模型层”等结论。说明该比例只是防御体系的一部分，不算违规。
- X3：答案不得把“拦下过度行为”扩大为“拦下所有攻击”“阻止所有外泄”或同义的全称结论。明确说该比例只对应过度行为，不算违规。
- X4：答案中凡出现比例或百分比，都必须归到正确指标名下：83%＝auto mode 对过度行为的执行前拦截率；93%＝用户批准权限提示的比例；0.1%／5–6%＝Gray Swan 红队测试的攻击成功率，分别是单次和百次自适应场景。只提其中一个指标不算违规；没有说明指标名称、对调名称或把不同指标合并，才算违规。

### c3-001

- 状态：已确认
- R1：答案指出“用户批准了约 93% 的权限提示”出自 Anthropic《How we contain Claude》中讨论人类逐次审批及其可靠性的章节；只复述这句话，或只给出文章名而不指出对应章节主题，不算满足。
- R2：答案说明该句所处上下文：随着审批数量增加，用户对每条提示的注意力下降，监督逐渐变得不细致，因此逐次审批并不可靠。只说明批准率高低，没有说明审批疲劳机制，不算满足。
- X1：答案不得把约 93% 的权限提示批准率说成 Claude Code auto mode 对过度行为的拦截率，也不得说成攻击成功率。明确说明 93% 是用户批准权限提示的比例，不算违规。
- X2：答案不得把约 93% 的高批准率解释为“用户满意”“用户认同所有操作”或“人类监督有效”。把它解释为审批疲劳、注意力下降或逐次审批不可靠的证据，不算违规。
- X3：答案不得把审批疲劳或高批准率归因于用户懒惰、不负责任或缺乏安全意识。说明这是长时间、高频审批造成的机制性注意力衰减，不算违规。

### c4-001

- 状态：已确认
- R1：针对 Anthropic《How we contain Claude》中的审批疲劳论证，答案给出完整因果链：审批数量大 → 对每条提示的注意力下降 → 监督越来越不细致 → 逐次审批作为监督方式不可靠（fallible）。四段关系可以用不同措辞表达，但缺少任一关键环节，不算满足。
- R2：答案指出 Anthropic《How we contain Claude》用真实产品使用数据支持审批疲劳论证，并给出“约 93% 的权限提示被用户批准”。只说“有数据”而不说明数据内容，或把该数据称为受控实验，不算满足。
- X1：答案不得把审批疲劳或高批准率归因于用户懒惰、不负责任或缺乏安全意识。把它解释为高频审批导致的机制性注意力衰减，不算违规。
- X2：答案不得把“逐次审批作为监督方式不可靠”扩大成“逐次审批毫无价值”或“任何逐次审批都没有作用”。说明它理论上可行但实际不可靠（fallible），不算违规。
- X3：针对审批疲劳导致人类监督失效这一风险，答案不得声称 Claude Code auto mode 已经解决、消除或替代了该监督问题。说明 auto mode 是额外防线但未消除该问题，不算违规。
- X4：答案不得把约 93% 的权限提示批准率说成受控实验、A/B 测试或模型测试结论。明确称其为真实产品使用统计，不算违规。

### c5-001

- 状态：已确认
- R1：针对 Claude Cowork 的 VM 隔离演进，答案说明：最初把整个 agent loop 放进完整 VM；后来把 agent loop 移到 VM 外，只保留代码执行在 VM 内；理由是完整 VM 的启动失败会让产品不可用。三个要点必须齐全，缺少演进关系或原因，不算满足。
- R2：针对 Claude Cowork 当前的 VM 隔离方案，答案说明：VM 具有独立内核、文件系统和进程表，宿主机其他部分不可见；只挂载用户选择的工作区和 `.claude`；凭证留在宿主机钥匙串，不进入 guest。没有说明可触达范围或外部不可见边界，不算满足。
- X1：答案不得把“整个 agent loop 都运行在 VM 内”描述为 Claude Cowork 的当前架构。明确说明这是早期 full-VM 方案，当前只把代码执行留在 VM 内，不算违规。
- X2：答案不得把 Claude Cowork 后来把 agent loop 移出 VM 描述成降低隔离或安全性。说明该调整是为避免 VM 启动失败导致产品不可用，且代码执行仍留在 VM 内，不算违规。
- X3：如果答案声称完整列出 Claude Cowork 的架构调整，则必须包含“本地 MCP server 也移到 VM 外”这一项；没有声称列全时可以不提，不算违规。声称完整但漏掉该项，才算违规。

### c6-001

- 状态：已确认
- R1：针对 Anthropic《How we contain Claude》中的 containment 机制，答案同时给出三项且定义正确：沙箱＝进程级边界，读操作允许、写操作限制在工作区、网络默认拒绝；虚拟机＝整机级隔离，具有独立内核、文件系统和进程表；出口控制＝出站网络边界，限制可连接的目的地和可发起的出站请求。缺少任一项或任一定义错误，不算满足。
- R2：答案说明沙箱、虚拟机、出口控制作用在不同层面，不是互相替代，而是可以重叠、互补形成多层防御。只分别定义三者但没有说明互补关系，不算满足。
- X1：答案不得把产品级隔离模式（例如 claude.ai 的服务端容器、Claude Code 的本地沙箱、Claude Cowork 的完整 VM）与环境层的具体控制手段（进程沙箱、虚拟机、出口控制）当作同一分类体系。明确区分产品形态和底层控制手段，不算违规。
- X2：答案不得把“出口控制存在域名白名单”解释成“域名白名单通过后网络就安全”或“出口控制可以完全阻止外泄”。说明白名单仍可能因已批准域名、请求内容或代理配置而失效，不算违规。
- X3：答案不得声称沙箱、虚拟机、出口控制可以互相替代，或其中一种已经涵盖另外两种的全部作用。说明三者作用轴不同并应叠加使用，不算违规。

### c7-001

- 状态：已确认
- R1：针对 Anthropic《How we contain Claude》明确承认的四起已发生风险，答案必须完整列出并分别辨认出：① 项目设置或 hook 在用户确认信任对话框之前已经执行；② 用户本人成为提示注入向量，例如钓鱼诱导员工粘贴恶意 prompt，25 次重试中有 24 次导致外泄；③ 通过已批准域名外泄，例如 `api.anthropic.com` 被放行后，文件被上传到攻击者的 Anthropic 账号；④ VM 隔离同时挡住企业 EDR，使企业安全团队看不到 guest 内部。四起必须齐全且事件内容可辨认；只概括为“四类风险”但没有说明具体事件，不算满足。
- X1：答案不得把持久记忆投毒、多智能体信任升级或智能体身份这三项前瞻性风险，列为 Anthropic《How we contain Claude》已经承认的“我们漏掉的风险”。如果答案明确把它们标为未来风险、前瞻风险或未发生风险，不算违规。

### c8-001

- 状态：已确认
- 决定：删除原 X1“罗列冒充整合”，由 R3 同时承担正向归并要求，避免同一问题重复判定。
- R1：答案明确提出 OpenAI《Harness engineering》和 Anthropic《How we contain Claude》的共同主线：agent 能力增强后，工程重点不能停留在模型或提示词本身，而要转向为 agent 设计可工作的环境、工具、知识入口和反馈回路，同时通过分层环境边界控制其影响范围。意思一致即可；只分别介绍两篇文章但没有提出共同主线，不算满足。
- R2：答案必须完整覆盖并正确归属 9 个概念。OpenAI《Harness engineering》：① 人掌舵、agent 执行；② 工程对象转向环境与脚手架；③ 仓库知识即事实源；④ 环境对 agent 可读、可执行；⑤ agent 互审的反馈回路。Anthropic《How we contain Claude》：① 爆炸半径随能力和访问范围扩大；② 环境层硬边界优先；③ 三层防护：环境／模型／外部内容；④ 隔离强度匹配用户的监督能力。缺少任一项或归属错误，不算满足。
- R3：答案必须把来自 OpenAI《Harness engineering》和 Anthropic《How we contain Claude》的至少两个概念放进同一个关系或对比框架中，并说明它们如何共同解释 agent 工程，例如“环境既是工作表面也是安全边界”或“人负责方向与验收，但不承担可靠的逐次监督”。只按文章分段罗列要点，或只有一句“两篇文章都强调工程很重要”，不算满足。
- X2：如果答案使用一个名称、概念或口号概括 OpenAI《Harness engineering》和 Anthropic《How we contain Claude》两篇文章，该表述必须确实同时来自两篇；只属于其中一篇时，必须标明来源，否则算违规。标明“借自某篇”后再用于整体比较，不算违规。

### c9-001

- 状态：已确认
- 决定：删除原 X3“说成完全一致”，由 R1、R3 共同排除该错误，避免同一维度重复判定。
- R1：答案指出 OpenAI《Harness engineering》和 Anthropic《How we contain Claude》讨论的不是同一层“人在回路”：Anthropic《How we contain Claude》否定的是逐动作审批作为监督方式的可靠性；OpenAI《Harness engineering》中的人在回路是方向设定、验收和优先级判断，PR 级逐次评审交给 agent 互审。缺少任意一侧的层次说明，不算满足。
- R2：答案指出 OpenAI《Harness engineering》和 Anthropic《How we contain Claude》的共同点：两者都不把人类视为可靠的逐次监督者；同时必须保留人在方向、目标、验收或更高层决策中的作用。只写“两篇都认为人不可靠”而没有说明人仍承担何种职责，不算满足。
- R3：答案明确判断 OpenAI《Harness engineering》和 Anthropic《How we contain Claude》不是在事实层唱反调，而是讨论不同层级、不同前提下的分工，可以同时成立。只分别复述两篇文章观点但没有给出“不矛盾”的明确结论，不算满足。
- X1：答案不得把 Anthropic《How we contain Claude》对逐次审批可靠性的否定，扩大为“人不需要参与”“人在回路没有价值”或“完全不需要人类监督”。说明人仍负责方向、目标、验收或更高层决策，不算违规。
- X2：答案不得把 OpenAI《Harness engineering》中的“humans remain in the loop”等同于“人类逐行评审 PR”或“人类审阅每一处改动”。明确说明人在回路指更高层的方向与验收，不算违规。

### c10-001

- 状态：已确认
- 决定：删除原 X3“只给结论”，由 R2 的证据要求覆盖，避免重复判定。
- R1：针对“AGENTS.md 写得越详细越好”这一说法，答案明确依据 OpenAI《Harness engineering》判断其不成立，并指出该文反对用一个庞大而面面俱到的 AGENTS.md 承载全部要求。只说“需要平衡”或没有明确判断，不算满足。
- R2：答案给出 OpenAI《Harness engineering》反对“写得越详细越好”的依据：上下文空间稀缺，大而全的指令会挤占任务说明和代码；当所有内容都显得重要时，实际没有重点；过长的指令也难机器校验、容易漂移。意思一致即可，但缺少核心论证，不算满足。
- R3：答案说明 OpenAI《Harness engineering》的正向主张：把 AGENTS.md 当作目录或入口，控制在约 100 行；把详细知识放入结构化的 `docs/`，并以仓库中的结构化文档作为事实源。只说明应该简短，没有给出目录、`docs/` 或事实源的替代方案，不算满足。
- X1：答案不得把 OpenAI《Harness engineering》反对“大而全 AGENTS.md”的主张，扩大为“AGENTS.md 不重要”或“应该删除 AGENTS.md”。说明 AGENTS.md 应作为精简目录或入口，不算违规。
- X2：答案不得把 OpenAI《Harness engineering》提到的约 100 行视为强制阈值，也不得据此断言 101 行必然错误、少于 100 行必然正确。把约 100 行表述为经验性参考，不算违规。

### c11-001

- 状态：已确认
- 决定：删除原 R3“依据准确”，由 R2 的双方适用前提与立场覆盖，避免同一维度重复判定。
- R1：答案明确指出 OpenAI《Harness engineering》和 Anthropic《How we contain Claude》不是事实层互相矛盾，而是基于不同产品和用户前提形成不同侧重点，因此不能脱离场景无条件判断“该听谁的”。只分别复述两篇文章观点但不说二者的关系，不算满足。
- R2：答案说明两篇文章的适用前提：Anthropic《How we contain Claude》面向通用产品和非技术用户，不能把安全建立在可靠的逐次人工审批上，因此优先设置环境层硬边界；OpenAI《Harness engineering》面向自家工程师团队和代码库，人类负责方向、优先级和验收，逐次 PR 评审可以更多交给 agent 互审。缺少任意一侧的适用前提，不算满足。
- X1：答案不得把 OpenAI《Harness engineering》和 Anthropic《How we contain Claude》的差异简化为“一篇反对人类参与、一篇支持人类参与”。说明两篇文章都保留人类在方向、目标或验收中的作用，只是对逐次监督的依赖不同，不算违规。
- X2：答案不得编造 OpenAI《Harness engineering》或 Anthropic《How we contain Claude》未表达的立场，例如“Anthropic 要求完全取消人工监督”“OpenAI 要求所有 PR 必须由 agent 独自批准”或“环境边界可以替代产品目标和验收”。只作有原文依据的概括或明确标注为推断，不算违规。

### c12-001

- 状态：已确认
- R1：答案说明 gVisor 是一种容器沙箱运行时，在容器与宿主内核之间增加隔离层。只说 gVisor 是“沙箱”“安全工具”或“Anthropic 使用的组件”，没有说明它与宿主内核之间的隔离关系，不算满足。
- R2：答案给出 Anthropic《How we contain Claude》中的依据：gVisor、seccomp、hypervisor 等成熟原语经历了远长于 agentic AI 的对抗检验；自建的薄层往往最弱；该文举出的例子是自建 allowlist proxy 发生过事故。只说“成熟组件更可靠”而没有说明对抗检验或自建组件风险，不算满足。
- X1：答案不得声称 Anthropic《How we contain Claude》做了“gVisor 与自建代理”的受控对照实验，也不得把文中的事故案例描述成实验数据。说明该文基于成熟原语经验和事故案例进行论证，不算违规。
- X2：答案不得把 Anthropic《How we contain Claude》中“自建的那层往往最弱”扩大为“所有自建组件一律不能用”。说明该文要求对自定义组件额外审查、优先成熟原语，不算违规。

### c13-001

- 状态：已确认
- 决定：不单独增加“遗漏透明”判据，R1 已要求五类齐全；判官只按 criterion 内的五类清单判断，不要求它自行知道知识库全貌。
- R1：答案必须覆盖以下五类隔离手段，并分别说明其核心作用：① 文件系统边界与挂载模式，区分只读、读写、读写但不可删除等权限；② 凭证隔离，凭证留在宿主机钥匙串，VM 仅取得按会话缩权的 token；③ 出口代理与检查，通过代理限制或检查出站请求；④ 工具权限最小化，例如只读数据库访问；⑤ 工具输出检查，例如使用小模型分类器检查工具返回内容的风险。五类必须齐全且每类作用可辨认；只列类别名称但没有说明作用，不算满足；遗漏任一类，也不算满足。

### c14-001

- 状态：已确认
- R1：答案必须对“MCP 是新的攻击面”这句话做分层归属：① “MCP 属于 agent 攻击面或外部风险来源”这一核心判断有 Anthropic《How we contain Claude》的原文依据，不是模型凭空补充；② “新的”这一限定是答案的概括，文章没有直接把 MCP 定性为“新的攻击面”。只笼统回答“是原文说的”或“是模型编的”，没有区分这两层，不算满足。
- R2：答案指出 Anthropic《How we contain Claude》把 MCP servers 与第三方插件、网页搜索工具并列，归为 agent 可触及的外部内容，并说明这些外部内容不能被完全控制。只重复“MCP 是攻击面”而没有指出其在文中的归类位置，不算满足。
- R3：答案说明 Anthropic《How we contain Claude》的进一步论证：任何进入 agent 上下文的外部资源都可能同时带来代码执行风险和提示注入向量。只说 MCP 有风险，没有说明这两种风险，不算满足。
- X1：答案不得把 Anthropic《How we contain Claude》对 MCP 的描述升级为“MCP 是最大的攻击面”“MCP 是最主要的风险来源”，也不得使用同义的最高级或唯一性表述。只把 MCP 列为攻击面或外部风险来源之一，不算违规。

### c15-001

- 状态：已确认
- 决定：删除原 X2“两个隔离混谈”，合并进 X1，避免同一问题重复判定。
- R1：答案必须正确给出 Anthropic《How we contain Claude》中的三种隔离模式及产品对应关系：ephemeral container ↔ claude.ai；HITL sandbox ↔ Claude Code；local VM ↔ Claude Cowork。三项缺失或产品对应错误，不算满足。
- R2：答案必须说明三种隔离模式各自的机制：claude.ai 的 ephemeral container 使用 gVisor 容器、跑在服务端、文件系统按会话销毁；Claude Code 的 HITL sandbox 运行在用户机器上、使用 OS 级沙箱，读操作允许、写操作限制在工作区、网络默认拒绝；Claude Cowork 的 local VM 使用具有独立内核、文件系统和进程表的完整 VM，只挂载工作区和 `.claude`，凭证留在宿主机钥匙串。缺少任一模式的机制或机制归属错误，不算满足。
- R3：答案必须说明三种隔离模式各自的威胁模型和边界：claude.ai ephemeral container 保护服务端基础设施、隔离不同租户，边界是服务端容器及宿主基础设施；Claude Code HITL sandbox 面向能够评估风险的开发者，边界是本地工作区；Claude Cowork local VM 面向无法判断 bash 风险的非技术用户，边界是挂载工作区及 VM 的 hypervisor／vsock 隔离。若答案使用“爆炸半径”表述对应的威胁范围，且三项关系正确，同样算满足。
- R4：答案必须说明三种隔离模式各自的代价及用户依赖：claude.ai ephemeral container 需要启动容器，不要求用户能读懂 bash；Claude Code HITL sandbox 使用低延迟原生 OS 沙箱，需要用户能够理解并评估 bash 风险；Claude Cowork local VM 需要启动完整 VM，不要求用户能读懂 bash。任一项代价或用户依赖缺失、对应错误，不算满足。
- X1：答案不得把 Anthropic《How we contain Claude》中的 ephemeral container、HITL sandbox、local VM 描述成同一机制仅强度不同，也不得把 claude.ai 的 gVisor 容器与 Claude Cowork 的完整 VM 说成同一层隔离。明确三者在运行形态、隔离边界或信任模型上不同，不算违规。
- X3：答案不得给 claude.ai 的 ephemeral container 或 Claude Cowork 的 local VM 编造“用户必须能读懂 bash”这一依赖。该依赖只属于 Claude Code 的 HITL sandbox；把这个依赖正确归给 Claude Code，不算违规。

### cf-001

- 状态：已确认
- R1：答案必须要求用户澄清具体是哪一篇 OpenAI 文章和哪一篇 Anthropic 文章；不得自行选择任意两篇后直接比较。只询问其中一侧，或直接给出两篇观点，均不算满足。

### cf-002

- 状态：已确认
- R1：答案必须至少满足一项：① 明确指出“写学习计划”超出本产品仅基于文档进行知识问答的能力范围；② 明确指出“这份文档”指代不明，并要求用户说明具体是哪一份文档。直接生成学习计划，或自行选择一份文档继续执行，均不算满足。

### cf-003

- 状态：已确认
- 决定：拆分为“运行事实 checker + 声明语义 LLM Judge”，不把运行状态发送给主判官。
- R1 截断事实（checker）：该 attempt 的 `answer_completeness` 必须为 `partial`。
- R2 截断声明（LLM Judge）：答案必须明确告知用户本次回答不完整、被截断或仍有内容未提供。直接停在句子中间、只遗漏部分文档但不说明、或仅使用与本回答无关的泛化免责声明，不算满足。
- 审查完成：18／18 个用例，共 69 条最终 criterion；其中 68 条由 LLM Judge 判定，1 条由 checker 判定。

## 证据包

### 自动化证据

- `corepack pnpm quality:quick`：通过。
- `pytest backend/tests`：229 条测试全部通过。
- `pyright --project backend`：0 errors。
- 产品与评测前端完整构建通过。
- 覆盖：
  - 历史 judge-ready v1：72 条 llm_judge、1 条 truncation checker；
  - 自包含 v2：68 条 llm_judge、1 条 truncation checker，另 1 条截断声明由 llm_judge 判定；
  - 白名单 JudgeInput 与 citation 解析；
  - LLM Judge 严格 JSON、R/X verdict 与证据 quote 校验；
  - 非法输出 pending；
  - invocation append-only 和 usage 记录；
  - checkpoint／restart 判定恢复语义。
  - 产品侧与评测侧共享 `TraceDetailViewer`，统一概览、调用树、artifact 和降级呈现；
  - 评测侧 trace artifact 读取端点及 checksum 保护。
  - 主判官白名单仅包含 answer 与当前 criterion；
  - 引用完整性与引用支持检查分离，引用支持检查使用独立最小输入。
  - `outcome_class` 区分产品结果与基础设施失败；产品失败计入 N，基础设施失败才补跑。
  - 无 answer 的产品失败仍产生 criterion Judgment，空答案送入主判官并判为 unsatisfied。
  - execution status 与 trace status 分离，failed attempt 的完整 trace 不再被误标为 partial。
  - `chat-v3` 在 finalization 轮注入短而闭合的 Finish 约束；Finish 被输出长度截断时保留已展示正文、追加“内容未完”声明并返回 partial。

### 运行证据

- 干净 schema 4 eval-data 启动探针通过：
  - active suite：`sr_design03_minimal_judge_v2`，18 cases／69 criteria；
  - c1 active：`cr_c1_001_minimal_judge_v2`；
  - cf-003 active：`cr_cf_003_minimal_judge_v2`，R1 为 `checker:truncation_declared`，R2 为 `llm_judge:judge-rubric-v2`；
  - 历史 suite／case revision 保持只读且未被改写；
- 人工验收发现设计表分区标题 `**边界**` 被旧解析器并入 14 条最后一项 R 判据；解析器修复后追加 `minimal_judge_v2`，真实库 active criterion 的误并入数量为 0，v1 及其历史 revision 仍保留。
- 真实本地 eval-data 已核对：
  - 18 个 active case revision、69 条 active criterion revision 与设计 03 的 ID、标题、规则文本完全一致；
  - 历史 suite `sr_design03_judge_v1` 已 retired，旧 case／criterion revision 仍保留；
  - 首次 seed 误补入旧 `cr_cf_003_r1` 的唯一链接已清理；append-only criterion revision 保留为 retired 审计记录。
  - 探针未调用 LLM。
- `judge-rubric-v2` 全文已由沛东于 2026-10-23 审核通过，hash `b1eed19be1ffae6c43be9831507aa7976e6333ff990fdf393e3f0dfb58bfb6e6`。
- `citation-rubric-v2` 全文已由沛东于 2026-10-23 审核通过，hash `7321cd67e0a661828e6b0ac2aa92f83963a70f0e81f61b0c16757071177bf4d8`。
- `chat-v3` finalization 提示词已由沛东于 2026-10-23 审核通过，hash `03f344febcac964251d8004c9a8e189b2033891314eaecf5b345c59a37ba4fd4`。
- `run_0cd1a6649ed340d0b71df10b0a626b09` 验证 D8：仅 1 个 `partial / product` attempt，trace complete，`chat-v3` 保留 15,575 字符部分答案并追加“内容未完”；R1、R2 均通过。引用支持因 73,611 字符超限 pending，未影响两条 criterion Judgment。
- 产品与评测前端分别在发布模式和评测模式完成生产构建。

### 失败探针

- 非法 JSON 输出：Judgment 保持 pending，invocation 标记 output_invalid，不转成 fail。
- Judge 预算不足：后续 criterion 保持 pending，已完成结果不丢失。
- JudgeInput trace missing／artifact missing／输入超限：pending，不重新检索补齐。
- 进程重启：running EvaluationRevision 恢复为 incomplete。

### 人工验收

- [x] 按 [023A 人工验收清单](../manual-tests/023A-evaluation-judgment.md)执行，全部通过。

## 反向解释

实现完成后逐题确认。

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么语义 criteria 默认交给 LLM Judge，而 human 只做覆盖？ | 用例数量和每次 Agent 更新的回归量决定全量人工主判不可行；人工只用于待定、争议、抽检和 LLM Judge 错误纠偏。 | 已确认：负责人独立说明规模与回归成本，并确认人工纠错的覆盖边界 | 后续人工覆盖按 append-only Judgment 记录，不替代主判官批量判定 |
| R2 | 为什么 Judge 上游失败只能 pending，不能记作 criterion fail？ | 上游失败不在可调教的 Agent 行为范围内，直接算 fail 会污染 Agent 质量指标；但产品体验仍会受影响，后续应单独统计上游失败率用于供应商选型。 | 已确认：负责人说明目标聚焦与产品体验的双重影响，并确认暂不在本切片实现失败率统计 | 已登记 [BL-0006](../backlog.md) |
| R3 | 为什么重判必须追加 Judgment，不能覆盖旧判定？ | 判官本身存在抖动，多次 Judgment 才能记录和衡量抖动；覆盖会丢失现场，无法区分问题来自 Agent 还是判官。 | 已确认：负责人指出判官抖动、审计和根因区分三个关键原因 | 人工覆盖与 effective binding 在 023B 追加，不原地改写 |

## 协作复盘

- AI 明显加速的环节：判定契约收敛、schema 迁移、执行/判定分层、失败探针与 UI 证据展示能够快速形成可运行闭环。
- 负责人失去系统模型的环节：`failed` 曾被当作单一“无效 attempt”，没有区分基础设施故障与产品行为；cf-003 的真实运行暴露了这个语义缺口。
- 真正发现问题的控制点：逐条 criterion 复核、真实 cf-003 执行、查看原始判官输入/输出和反向解释，而不是只依赖自动化测试。
- 只有仪式感、没有产生判断价值的流程：本轮未保留；真实 trace 和运行现场检查都直接发现了问题。
- 下一次准备调整的一到两项：先固定失败分类和样本资格，再实现语义判定；涉及失败行为的用例必须用真实失败现场验收。

## 已知风险与后续

- LLM Judge 本身存在随机抖动；023A 保存多次 Judgment 以支持后续校准，但尚未实现统计置信度和自动一致性评估。
- 长答案可能产生大量引用；该输入超限缺口已由 [025](./025-evaluation-citation-support-batching.md) 按语义上下文 occurrence 分批修复。
- LLM Judge 成本随 attempts × criteria 增长；023A 已有界并发、预算和失败暂停，但真实大规模成本仍需观察。
- 人工覆盖、N/K 聚合、结果矩阵和基线留给 023B。
