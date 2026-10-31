# 实现待办

本文件记录跨切片、当前不实现但值得长期保留的候选事项。当前切片未完成的验收项仍留在对应切片中。

## 状态口径

| 状态 | 含义 |
| --- | --- |
| 候选 | 已识别，但尚未确认是否值得做 |
| 已接受 | 方向确认，尚未进入具体切片 |
| 已排期 | 已进入明确的后续切片计划 |
| 进行中 | 正在当前开发切片中实现 |
| 已完成 | 验收通过并已进入基线 |
| 已拒绝 | 明确不做，保留原因供以后复查 |

## 候选事项

| ID | 状态 | 主题 | 来源 | 当前基线 | 暂缓原因 | 触发条件 | 目标切片 | 初步验收 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BL-0001 | 候选 | HTML 图片资源归档与预览 | [004 人工验收](manual-tests/004-html-ingestion.md) | 首版只保留 `alt`、caption 和说明文本，不下载图片，不显示图片，不做 OCR 或视觉理解 | 需要新增资源获取、URL 安全、本地相对路径解析、快照存储、引用展示和隐私边界，适合独立设计 | 用户需要图片预览、图片本身承载关键事实，或真实学习资料中图片价值被证明不足 | 待定 | 原始图片有稳定 artifact；block 可回指图片；URL 下载仍经过 SSRF 与体积限制；未支持时继续明确告警 |
| BL-0002 | 已接受 | URL DNS rebinding 防护加固 | [004 决策 D9](slices/004-html-ingestion-preview.md) | 当前校验 DNS 解析出的所有地址均为公网，但实际连接时可能再次解析，存在 rebinding 时间窗 | 完整 IP pinning 需要自定义连接层，超出 004 预览范围 | 公开发布前，或 URL 获取威胁模型扩大到自动抓取／第三方提供 URL 时 | 待定 | 校验与实际连接使用同一组已批准的 IP；重定向继续复核；覆盖 rebinding 与代理环境测试 |
| BL-0003 | 已接受 | 预览 job 与 artifact 清理策略 | [004 决策 D10](slices/004-html-ingestion-preview.md)、[005 决策 D6](slices/005-sqlite-document-registry.md) | job 写入 `data_root/jobs/{job_id}`，成功和失败产物均原子保留，当前没有保留期或清理入口 | 清理规则依赖正式持久化、快照保留和用户设置，不能提前拍死 | 公开发布前，或预览产生的磁盘占用开始累积时 | 待定 | 定义保留期／数量上限、失败与成功 job 的统一清理入口、正在使用任务的保护；被 `document_versions` 引用的 artifact 在 S6 正式落库前不得删除；覆盖引用保护与清理测试 |
| BL-0004 | 已完成 | 检索参数与 Agent 用户设置界面 | [017 已知风险](slices/017-hybrid-search.md)、[018 D5](slices/018-agent-runtime-core.md)、[019 统一设置页](slices/019-agent-settings.md) | 019 已统一持久化两个 API Key、Top-K、最大输出 token、上下文预算、请求预算和工具调用上限；检索模式、候选深度与 RRF 仍由系统固定 | 已完成，不暂缓 | 已满足，后续仅在新增可配置参数时扩展 | 019 | 统一设置页、跨重启、运行中隔离与非法值拒绝均已验收 |
| BL-0005 | 已完成 | 评测与产品共享 Agent 执行链边界 | 沛东 2026-10-22 对评测台形态和代码复用关系的要求 | 评测台通过独立 sidecar 进程内调用共享 `AgentRuntime`、检索工具和 trace 核心，不要求产品应用运行，也不走产品 HTTP API；评测库与产品库隔离，并只读绑定冻结 collection 快照 | 已完成，不暂缓 | 已满足 022A 执行内核与人工验收 | 022A | 产品 API 与评测 runner 调用同一 Execution Service；代码注释明确该单一行为路径、版本固定、数据隔离和禁止复制 Agent 逻辑；契约测试证明两者运行结果与 trace 行为一致 |
| BL-0006 | 已接受 | 上游失败率统计与供应商选型依据 | [023A 反向解释 R2](slices/023A-evaluation-judgment.md) | attempt 已区分 `product` 与 `infrastructure`，基础设施失败不计有效 N；当前尚未形成按供应商、模型和时间统计的上游失败率报告 | 023A 先完成 Agent 能力判定闭环；上游失败率属于产品运行质量与供应商选型指标，暂不混入当前判官切片 | 开始比较模型／供应商，或正式报告需要解释产品可用性时 | 待定（评测报告／供应商评估） | 按 provider／model／error_code 统计 attempt 与请求失败率、区分超时／限流／不可用／输出非法，展示趋势，且不混入 Agent criterion fail |
| BL-0007 | 已完成 | 长答案引用支持分批检查 | [023A cf-003 验收](slices/023A-evaluation-judgment.md) | [025](slices/025-evaluation-citation-support-batching.md) 已实现引用上下文 occurrence 分批；cf-003 历史现场按 15 批完成 72 个 occurrence，不再因总输入超限失败 | 已完成，不暂缓 | 新增长答案可继续观察 unknown 比例，但不再复现 input_too_large | 025 | 段落、列表项和表格行上下文稳定解析；每批最多 5 个 occurrence；输出上限 4,096 token；重复/缺失 occurrence 可检测；真实 cf-003 完成全批覆盖 |
| BL-0008 | 已接受 | 完整延迟评测与延迟基线闭环 | [024B 反向解释 R2](slices/024B-evaluation-baseline-comparison.md) | `run_type=latency` 已可创建并强制串行，attempt 已记录 `first_token_at`；但首字延迟尚未进入 ResultMatrix、基线比较和发布判定 | 当前先完成基线与跨 run 比较；延迟分类、统计和准出语义应在独立切片中实现 | 开始正式延迟评测、比较首字延迟或使用 latency run 形成发布结论时 | 待定（延迟评测切片） | `first_token_at` 进入结果矩阵与基线；按简单／复杂类计算 P50／P90；按设计阈值生成确定结论；延迟差异与总耗时明确分离；资格、聚合和基线比较有自动化测试 |
| BL-0009 | 已接受 | 一阶段 answer-block + citation 生成对照 | [027 Claim-aware 引用标注](slices/027-claim-aware-citation-annotation.md)、[ADR 0012](../decisions/0012-harness-owned-claim-citation-annotation.md) | 当前先验证“正文一次生成 + 独立 annotation”的两阶段方案，通常比一阶段多一次模型调用 | 两阶段基线尚未经过真实 N=1 与人工引用复核；现在切到一阶段没有可比基线 | 两阶段真实样本证明正文连续、引用归属和 pending 语义可接受后，且仍有降低成本的需要 | 待定（一阶段引用对照） | 同模型、同 collection／case revisions 下比较总 token、延迟、正文连贯性、引用正确率和失败可归因性；一阶段若碎片化或引用不可核验则保留两阶段 |
| BL-0010 | 已接受 | 引用组必要性与冗余判定 | [029 联合支持](slices/029-citation-evaluation-reliability.md) | 029 已实现 claim 与 block 组级联合覆盖判定；尚不能区分组内每个 block 是否必要，或识别多余引用 | 联合覆盖先解决真实“互补证据被逐块判错”问题；必要性需要更细的判官输出，混入本轮会扩大协议风险 | 出现“组级通过但包含明显无关 block”，或人工复核需要逐 block 解释必要性时 | 待定（引用语义增强切片） | 区分充分、必要与冗余引用；组级结论与逐 block 解释一致；旧 CheckResult 保持可回放 |
| BL-0011 | 已接受 | 显式语义角色保真候选探测 | [037 D3](slices/037-citation-support-review-gate.md)、[引用语义门禁实验](experiments/2026-10-26-citation-support-review-gate.md) | 037 将关系三阶段判官移出默认门禁；主判官对已知“0.4% 收益／代价角色反转”完整 packet 重复 20 次全部 supported；现有关系候选器在 38 个关系型 claim 上产生 5 个候选，其中仅 1 个为已知错误 | 直接恢复关系判官会给单个目标错误增加约 4 个额外人工候选，暂不进入默认门禁 | 需要把引用角色保真纳入稳定质量门禁，或该类稳定漏判在实际 run 中重复影响结论时 | 待定（角色保真候选校准切片） | 建立已知错误、语义等价正例、近义转写和角色边界的平衡探针；分别报告目标召回、额外候选率、重复稳定性、token 与人工成本；任何自动结果仍只能形成未确认普通失败，不能直接成为红线；当前 `citation-rubric-v11` revision 不得因 10/10 pass 直接晋级质量基线 |
| BL-0012 | 已接受 | [高优先级] c13 原题恢复与千篇规模补充检索 | [c13 范围校准](slices/052-c13-additional-controls-scope.md)、[开放范围问题分析](experiments/2026-10-29-c13-open-scope-retrieval-analysis.md)、[LLM Wiki 固定快照实验](experiments/2026-11-01-llm-wiki-compilation.md)、[01 §2.3 第 13 类](../design/01-product-requirements.md) | c13 当前单篇文章题面与四类 R1 是过渡版本；`search_blocks` 只有有限 top-k 且无续页，`list_blocks` 虽能按篇／节取全，但无法在 1,000 篇、10 万 block 规模下逐篇装入上下文。069–074 初版 Wiki 已能发现并绑定三个目标 block，但主 Agent 仍未形成开放找全的覆盖决策 | 当前先完成其余 active 用例；c13 涉及产品口径、测试范围与检索架构，不能继续混在生成提示词校准中。Wiki 初版先作为可审阅的独立能力合入，覆盖规划另做优化 | 当前其余 active 用例全部完成，或跨文档“还有什么”再次成为主要阻塞时；Wiki 初版合并后即可开展覆盖规划对照 | 待定（预计 054 或之后） | 恢复 c13 跨知识库“还有什么”语义；把“全”定义为相关范围高覆盖＋缺口透明而非开放世界完备证明；在 1,000 篇／10 万 block 模拟规模验证多角度查询→候选文档／章节聚合→候选内大纲／正文展开→覆盖记录，不退化为全库逐篇读取；Wiki／图不作为前置依赖；冻结 run 使用明确金标准重判并保持 N／K 可比较；Wiki 路径需证明主题目录、来源轮转与主 Agent 选证相比普通块检索有可归因增量 |
| BL-0013 | 已接受 | [高优先级] 历史判定缺少 criterion 元数据导致矩阵为空和问号 | cf-001 人工查看结果矩阵与样本详情 | evaluation revision/judgment 只返回 `criterion_revision_id`；前端 `criterionById` 仅从启动时缓存的 case catalog 构建。active case revision 迁移后，旧 run 的新 criterion revision 不在缓存中，判定矩阵为空，详情 badge 显示 `?`，金标准显示“当前 revision 的 criterion 原文不可用” | 当前先继续用例分析；该问题需要同时改评测读取模型和前端回退逻辑，并补历史 revision 迁移场景回归 | 下一次评测台维护，或继续查看跨 revision 的历史 run 前 | 待定（评测历史可解释性修复切片） | evaluation revision/judgment 返回不可变 criterion 快照（revision/rule_type/title/rule_text）；前端不依赖 active catalog 即可渲染历史判定；旧 run 在 case revision 迁移后矩阵仍有行、详情无 `?`；覆盖 catalog 缓存过期与后端元数据直出两条测试 |
| BL-0014 | 候选 | 澄清候选清单中的文档来源归属校验 | [cf-001 透明目标选择](slices/061-cf001-transparent-target-selection.md) | cf-001 已收敛为“目标范围可见”；候选清单和综合范围中的机构归属准确性属于质量观察，不进入当前 gate | 现有 LLM Judge 不直接持有候选文档的来源元数据，逐标题判断会引入不稳定；当前数据基础下也允许归属不精确 | 纯澄清答案多次出现机构错挂，或来源归属成为用户主要误判来源时 | 待定（cf-001 候选消歧确定性校验） | 从 `list_documents` trace 或 collection manifest 解析候选标题及 `source_label`；候选来源错挂可确定识别；不依赖模型记忆机构归属；与 cf-001 的目标可见性分开报告 |
| BL-0015 | 已接受 | Wiki topic dense 入选阈值高级配置 | [2026-11-01 LLM Wiki 编译实验](experiments/2026-11-01-llm-wiki-compilation.md)、[071](slices/071-wiki-topic-search.md) | `search_wiki_topics` 固定使用 `DENSE_MIN_SIMILARITY=0.15`；c13 固定快照实测返回 365/365 个主题，实际未形成有意义的相似度筛选 | 当前先提交并审查 069–074 的初步产品闭环，不在本轮扩大设置模型、前端和回归范围 | 本轮初步实现合并后，开始优化 Wiki 检索效果与 token 成本时 | 待定（Wiki 检索优化切片） | 阈值成为用户可设置并持久化的高级配置；界面说明其是“检索效果／token 消耗”权衡；配置只在请求开始时冻结；合理上下界和非法值拒绝；同 query 对比不同阈值的主题数、上下文 tokens、目标主题召回和 Agent 行为；默认值由实测决定，不再直接沿用 0.15 |
