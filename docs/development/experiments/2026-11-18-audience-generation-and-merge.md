# 角色卡候选 thinking 生成与第二阶段归并实验

## 文档定位

本文验证 16 号设计中“领域卡经多轮 thinking 生成角色卡候选，再由第二阶段只做归并”的可行性。
实验不读取当前知识库，不使用 `c13-003`，也不修改正式产品代码。提示词仍处于人工审核前的候选状态；
本文结论只来自 AI Agent 单个领域和一次候选输入，不能证明角色卡候选覆盖完整。

## 固定条件

- 模型：`deepseek-flash`
- temperature：0
- 输出上限：60,000 token
- 生成提示词：实验时版本；现行契约见 16 号设计 §4.3.1“领域相关知识使用角色枚举器”（实验执行时位于旧版 §4.10）
- 归并提示词：现行契约见 16 号设计 §4.3.2“使用角色候选归并器”；实验执行时位于旧版 §4.11
- 生成输入：领域名称“AI Agent”及一段关键名词说明，不含知识库、文章标题、测试问题或参考答案
- 生成 thinking：开启；没有把它和同配置的完整多次基线混合统计
- 归并 thinking：当前实验配置开启；非 thinking 结果只作为对照
- 生成执行入口：`backend/.runtime/audience_generation_probe.py`
- 归并执行入口：`backend/.runtime/audience_merge_probe.py`
- 原始结果：保存在被忽略的 `backend/.runtime/audience_generation_*` 与 `backend/.runtime/audience_merge_*`

## 候选生成结果

此前另有一次同提示词、thinking 关闭的探索运行，只用于确认 thinking 的候选扩展方向，不与开启
thinking 的运行合并成稳定性比例。

| 运行 | thinking | 状态 | 角色卡候选数 | 耗时（秒） | 输入 token | 输出 token |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| 关闭 thinking 探索 | 否 | 合法 | 12 | 26.5 | 870 | 5,042 |
| `20261118T114624Z` | 是 | 合法 | 30 | 83.1 | 895 | 13,363 |
| `20261118T115445Z` run 1 | 是 | 非法 | 29 | 72.8 | 895 | 11,636 |
| `20261118T115445Z` run 2 | 是 | 合法 | 38 | 101.3 | 895 | 18,005 |
| `20261118T115558Z` | 是 | 合法 | 26 | 59.8 | 895 | 9,148 |

唯一非法生成结果不是截断，而是一个候选的错误字段：`decisions[1]` 使用了 `task`，缺少
`decision`。宿主按结构契约拒绝该轮，没有把候选送入归并。

开启 thinking 后出现了未开启时没有覆盖到的最终用户、一线业务用户、客服运营、采购与供应商、
售前、保险核保、监管政策、组织采用和培训等角色。数量同时明显波动，说明 thinking 有助于扩大
候选方向，但不能把单次角色数量当作覆盖指标。

## 归并结果

归并输入取三次合法 thinking 生成的并集，共 94 个已加运行命名空间的来源候选。

| 运行 | 结构状态 | 归并组 | 发生合并的组 | 单例组 | 减少数 | 未决问题 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `20261118T115625Z` | 合法 | 54 | 27 | 27 | 40 | 3 |
| `20261118T115727Z` | 非法 | 51 | 28 | 23 | 43 | 0 |
| `20261118T115730Z` | 非法 | 52 | 27 | 25 | 42 | 3 |
| `20261118T115822Z` | 合法 | 55 | 26 | 29 | 39 | 5 |

两次非法归并都遗漏了同一个上下文与提示策略工程师候选组；94 个来源中各漏 2 个。它们不是模型
主动排除，也没有进入 `unresolved_issues`，因此不能通过调整统计口径视为成功。

### 切换到 thinking

20,000 token 上限下，thinking 归并曾因输出截断失败。提高至 40,000 后，thinking 能完整输出全局
分组。非 thinking 在相同 40,000 上限下连续三次仍分别遗漏 2～3 个来源候选，说明问题不是单纯
输出长度，而是全局分组和完整性检查能力不足。

当前提示词调整后的 thinking 回放如下：

| 运行 | 结构状态 | 归并组 | 发生合并的组 | 单例组 | 未决问题 | 遗漏来源 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `20261118T130540044802Z` | 合法 | 62 | 23 | 39 | 7 | 0 |
| `20261118T131215458343Z` | 合法 | 54 | 27 | 27 | 0 | 0 |
| `20261118T131437974476Z` | 合法 | 60 | 25 | 35 | 0 | 0 |
| `20261118T131529900471Z` | 合法 | 54 | 28 | 26 | 0 | 0 |
| `20261118T131919758171Z` | 合法 | 56 | 25 | 31 | 0 | 0 |

其中前四轮用于收紧“按职责语义归并、不按职位名称硬拆”的边界；最后一轮加入了归并器不得
补充或纠正第一阶段职责描述的边界说明。所有轮次都覆盖 94 个来源，没有新增、重复或遗漏候选。

## 语义复核

当前归并对象不是现实组织中的正式岗位，而是角色卡候选已经写出的知识使用职责。职位名称、职级和
组织称谓不同不构成拆分理由。若两个候选的任务、关键决定、责任边界和错误代价可以双向对应为
同一含义，即使一个叫“负责人”、另一个叫“响应员”，或一个叫“培训师”、另一个叫“组织采用负责人”，
也应先按当前描述合并。

以下合并较稳定，且任务、关键决定和错误代价基本一致：

- 产品经理三份候选；
- 系统架构师三份候选；
- Agent 应用／LLM 工程角色；
- 工具／API 集成与 API 提供方角色；
- 检索增强与知识库工程角色；
- 评测、SRE、平台运行时、事故响应、交互设计、采购、售前和保险核保等职责相近角色。

### 不稳定边界

- 安全工程、红队和同时包含两类职责的混合候选：当候选卡把防护设计与攻击验证写得接近时，组边界会
  变化。这属于第一阶段候选职责区分度问题，不应由归并器根据名称补猜。
- 培训、组织采用和变革：不同运行会决定是否把纯培训候选与采用／流程改造候选合并，取决于前者
  是否也明确写出采用推进和误用治理义务。
- 业务流程分析、流程责任和运行运营：只有候选卡写出的完整义务能够双向对应时才合并；额外在线
  运营或独立结果责任如果没有对应项，仍需单列。
- 最终用户、一线业务用户和授权责任人：是否合并取决于三者的目标约束、授权动作、异常升级和错误
  代价是否在当前候选卡中实质相同。

这些变化不是继续添加职位名称规则的依据。若业务上应当分开的两个角色因职责描述过近而被合并，
根因是第一阶段没有把角色任务、关键决定、责任边界或错误代价写开，应回到候选生成提示词修复。

## 结论

1. thinking 生成应保留为候选扩展阶段。它显著扩大了角色方向，但不能声称已经覆盖全部角色。
2. 归并阶段也应开启 thinking。非 thinking 在 40,000 输出上限下仍连续遗漏来源，thinking 的当前
   五次回放均完整覆盖 94 个来源。
3. 第二阶段“只归并、不补缺”的方向成立。当前最终回放把 94 个来源整理为 56 组，其中 25 个组发生
   合并、31 个单例、减少 38 个重复候选，且没有新增候选、遗漏、重复或未决问题。
4. 归并判据应是角色卡候选中的知识使用职责是否实质相同，而不是岗位名称、职级或组织称谓是否相同。
   职位称谓不同但任务、关键决定和错误代价能够双向对应时，应合并。
5. 归并器不得依靠现实岗位常识补充第一阶段没有写出的职责差异。若职责描述过近导致错误合并，
   应修复第一阶段候选卡的职责区分度，而不是继续在归并阶段添加名称型例外规则。
6. 当前结果只证明在单个 AI Agent 领域和固定 94 个来源候选上形成阶段可用版本，不能证明跨领域
   候选覆盖完整或归并边界已经最终稳定。

## 复现

```powershell
backend\.venv\Scripts\python.exe backend\.runtime\audience_generation_probe.py --runs 1 --thinking --timeout-seconds 240

backend\.venv\Scripts\python.exe backend\.runtime\audience_merge_probe.py `
  backend\.runtime\audience_generation_20261118T114624Z\results.json `
  backend\.runtime\audience_generation_20261118T115445Z\results.json `
  backend\.runtime\audience_generation_20261118T115558Z\results.json `
  --thinking --timeout-seconds 360
```

## 附录：当前 56 组归并结果

以下记录最终回放 `20261118T131919758171Z` 的完整归并结果。前 25 组发生合并，后 31 组为单例。
`group_key` 只用于本次运行内追踪；同一名称重复出现表示它来自不同生成运行，仍作为独立来源候选保留。
这些组是后续用户选择、拆分和调整的输入，不是已确认的最终角色卡。

| 编号 | 组键 | 代表名 | 类型 | 来源成员 |
| ---: | --- | --- | --- | --- |
| 1 | `grp_01_agent_product_manager` | AI Agent 产品经理 | 合并 | AI Agent 产品经理<br>Agent 产品经理<br>AI Agent产品经理 |
| 2 | `grp_02_agent_architect` | Agent 架构师 | 合并 | Agent 架构师<br>Agent 系统架构师<br>Agent系统架构师 |
| 3 | `grp_03_agent_app_engineer` | Agent 应用/LLM 工程师 | 合并 | Agent 应用/LLM 工程师<br>Agent应用开发工程师 |
| 4 | `grp_04_context_prompt_engineer` | 上下文与提示词工程师 | 合并 | 上下文与提示词工程师<br>Agent上下文与提示策略工程师 |
| 5 | `grp_05_tool_api_integration` | 工具/API 集成工程师 | 合并 | 工具/API 集成工程师<br>工具/API 集成工程师<br>Agent工具/API提供方设计者 |
| 6 | `grp_06_rag_knowledge_engineer` | 检索增强/知识工程师 | 合并 | 检索增强/知识工程师<br>检索增强/RAG 工程师<br>检索增强与知识库工程师 |
| 7 | `grp_07_agent_evaluation_engineer` | Agent 评测工程师 | 合并 | Agent 评测工程师<br>Agent 评测工程师<br>Agent评测工程师 |
| 8 | `grp_08_qa_test_engineer` | Agent 软件测试/QA 工程师 | 合并 | Agent 软件测试/QA 工程师<br>Agent质量保障与自动化测试工程师 |
| 9 | `grp_09_ai_security_engineer` | Agent 安全与对齐工程师 | 合并 | Agent 安全与对齐工程师<br>AI 安全工程师 |
| 10 | `grp_10_ai_red_team` | Agent 红队测试员 | 合并 | Agent 红队测试员<br>AI 红队/安全评估员<br>AI Agent安全工程师/红队 |
| 11 | `grp_11_agent_sre` | Agent SRE/运维工程师 | 合并 | Agent SRE/运维工程师<br>Agent 可靠性/SRE 工程师<br>Agent运维/SRE |
| 12 | `grp_12_agent_platform_engineer` | Agent 平台/运行时工程师 | 合并 | Agent 平台/运行时工程师<br>AI Agent 平台工程师 |
| 13 | `grp_13_agent_interaction_designer` | Agent 交互设计师 | 合并 | Agent 交互设计师<br>对话与交互体验设计师<br>Agent交互与人因设计师 |
| 14 | `grp_14_agent_incident_responder` | Agent 事故响应负责人 | 合并 | Agent 事故响应负责人<br>Agent 事件响应员<br>AI Agent事故响应负责人 |
| 15 | `grp_15_agent_finops` | Agent 成本/FinOps 管理者 | 合并 | Agent 成本/FinOps 管理者<br>成本与性能优化工程师<br>AI Agent成本管理员/FinOps |
| 16 | `grp_16_procurement_vendor_manager` | Agent 采购/供应商评估专员 | 合并 | Agent 采购/供应商评估专员<br>采购与供应商经理<br>企业Agent技术选型与采购负责人 |
| 17 | `grp_17_agent_insurance_underwriter` | AI Agent 责任保险核保人 | 合并 | AI Agent 责任保险核保人<br>AI Agent责任保险承保与风险分析师 |
| 18 | `grp_18_ai_regulator_policy` | 监管/政策分析师（AI Agent） | 合并 | 监管/政策分析师（AI Agent）<br>AI Agent监管与标准政策制定者 |
| 19 | `grp_19_workforce_adoption_trainer` | Agent 培训师 | 合并 | Agent 培训师<br>组织采用与培训负责人<br>企业Agent采用培训与变革负责人 |
| 20 | `grp_20_presales_solutions` | Agent 解决方案售前架构师 | 合并 | Agent 解决方案售前架构师<br>Agent解决方案售前工程师 |
| 21 | `grp_21_end_user_authorizer` | 授权Agent代办事务的最终用户 | 合并 | 授权Agent代办事务的最终用户<br>Agent 终端业务用户 |
| 22 | `grp_22_human_in_the_loop_reviewer` | 人工在环复核员 | 合并 | 人工在环复核员<br>Agent人机协作审批员 |
| 23 | `grp_23_eval_dataset_curator` | 评测数据策展/标注负责人 | 合并 | 评测数据策展/标注负责人<br>Agent评测集构建与数据标注员 |
| 24 | `grp_24_privacy_compliance` | AI 合规与隐私顾问 | 合并 | AI 合规与隐私顾问<br>隐私合规专员<br>AI Agent隐私与合规法务顾问 |
| 25 | `grp_25_ai_risk_governance` | AI 风险治理专员 | 合并 | AI 风险治理专员<br>模型风险/治理分析师 |
| 26 | `single_multi_agent_orchestrator` | 多 Agent 编排工程师 | 单例 | 多 Agent 编排工程师 |
| 27 | `single_agent_ops_configurator` | Agent 运营配置员 | 单例 | Agent 运营配置员 |
| 28 | `single_agent_trace_auditor` | Agent 轨迹审计/可观测性分析师 | 单例 | Agent 轨迹审计/可观测性分析师 |
| 29 | `single_biz_process_analyst` | 业务流程自动化分析师 | 单例 | 业务流程自动化分析师 |
| 30 | `single_customer_service_supervisor` | 客服运营主管（Agent辅助） | 单例 | 客服运营主管（Agent辅助） |
| 31 | `single_coding_agent_developer` | 使用编码Agent的软件开发者 | 单例 | 使用编码Agent的软件开发者 |
| 32 | `single_research_agent_analyst` | 使用Agent的研究分析师 | 单例 | 使用Agent的研究分析师 |
| 33 | `single_finance_agent_reviewer` | 使用Agent的财务审核员 | 单例 | 使用Agent的财务审核员 |
| 34 | `single_ai_usage_policy_owner` | 组织内 AI 使用政策负责人 | 单例 | 组织内 AI 使用政策负责人 |
| 35 | `single_external_tool_provider_api_owner` | 外部工具提供方 API 负责人 | 单例 | 外部工具提供方 API 负责人 |
| 36 | `single_memory_personalization_engineer` | 记忆与个性化工程师 | 单例 | 记忆与个性化工程师 |
| 37 | `single_agent_policy_guardrail_author` | Agent 行为策略/护栏作者 | 单例 | Agent 行为策略/护栏作者 |
| 38 | `single_agent_identity_access_engineer` | Agent 身份与访问控制工程师 | 单例 | Agent 身份与访问控制工程师 |
| 39 | `single_legal_counsel_ai` | AI 法律顾问 | 单例 | AI 法律顾问 |
| 40 | `single_observability_llmops_engineer` | 可观测性与 LLMOps 工程师 | 单例 | 可观测性与 LLMOps 工程师 |
| 41 | `single_business_automation_owner` | 业务自动化负责人 | 单例 | 业务自动化负责人 |
| 42 | `single_customer_support_engineer` | Agent 客户支持工程师 | 单例 | Agent 客户支持工程师 |
| 43 | `single_release_change_manager` | 发布与变更经理 | 单例 | 发布与变更经理 |
| 44 | `single_data_pipeline_engineer` | Agent 数据管道工程师 | 单例 | Agent 数据管道工程师 |
| 45 | `single_knowledge_corpus_curator` | 知识库/语料策展人 | 单例 | 知识库/语料策展人 |
| 46 | `single_domain_expert_validator` | 领域专家验收员 | 单例 | 领域专家验收员 |
| 47 | `single_high_stakes_professional_supervisor` | 高风险执业决策复核者 | 单例 | 高风险执业决策复核者 |
| 48 | `single_model_provider_manager` | 基础模型供应商/版本经理 | 单例 | 基础模型供应商/版本经理 |
| 49 | `single_agent_interoperability_protocol_engineer` | Agent 互操作协议工程师 | 单例 | Agent 互操作协议工程师 |
| 50 | `single_simulation_environment_engineer` | 仿真环境工程师 | 单例 | 仿真环境工程师 |
| 51 | `single_regulatory_auditor` | 监管/独立审计员 | 单例 | 监管/独立审计员 |
| 52 | `single_frontline_agent_user` | 使用Agent辅助的一线业务人员（如客服坐席） | 单例 | 使用Agent辅助的一线业务人员（如客服坐席） |
| 53 | `single_agent_risk_auditor` | AI Agent风控与审计员 | 单例 | AI Agent风控与审计员 |
| 54 | `single_business_process_owner` | 业务流程图所有者/自动化责任人 | 单例 | 业务流程图所有者/自动化责任人 |
| 55 | `single_agent_platform_governance` | Agent平台治理管理员 | 单例 | Agent平台治理管理员 |
| 56 | `single_devrel_agent_advocate` | Agent开发者关系/技术布道师 | 单例 | Agent开发者关系/技术布道师 |
