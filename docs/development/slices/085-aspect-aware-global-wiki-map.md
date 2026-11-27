# 085 · Aspect-aware 全局 Wiki 地图与证据选择

- 状态：待设计审阅（契约实现完成，c13 噪声稳定性未通过）
- 开始日期：2026-11-06
- 关联设计：[13 Profile-Driven Wiki 证据检索](../../design/13-profile-driven-wiki-evidence-retrieval.md)
- 前置切片：[084 Profile Wiki 地图规模化就绪改造](084-scale-ready-profile-wiki-map.md)
- 人工验收：c13-003 单篇/15 篇检索覆盖与噪声，加 Aspect／全局收敛诊断

## 控制契约

### 背景与失败场景

- 触发问题：当前 Topic／Group 归并受固定 batch 和 3 passes 限制；在线直接从 source blocks 选证据，Block 数量随文档线性增长。
- 不处理时的用户损失：相似文档使 Topic／Group 重复、Block 候选膨胀，在线查询成本和不稳定性持续上升。
- 现有流程为什么不足：没有全局固定点收敛、Profile Aspect Schema、编译期 BlockAspectSupport，也没有 Aspect-first 查询。
- 本切片负责什么：实现 13 §3.8、§6.7、§7.6、§7.7、§8.7 的首版代码路径；新 map schema；c13-003 验证。
- 本切片不负责什么：Aspect Schema 编辑 UI、50／100／1,000 篇正式规模结论、最终答案 Judge、页面体系。
- 停止或撤掉条件：全局候选图无法保持 provenance；Aspect 编译无法形成合法 Block support；c13 核心覆盖无法恢复。

### 用户可观察目标

1. 新地图包含 Profile Aspect Schema、TopicAspect 和 BlockAspectSupport。
2. Topic／Group 身份在整图候选图中迭代到稳定或显式 `ready_partial`。
3. 在线查询展开全部 Topics，选择 Aspects，再为每个 Aspect 选择代表 Block。
4. c13-003 返回跨文档核心证据，噪声不高于设计门禁。
5. 相同事实只保留一个代表，来源冲突和新增细节可保留。

### 核心不变量

1. Candidate 文档绑定和原文 evidence 不得丢失；Aspect support 不得替代原文。
2. Topic／Group 的 semantic batch 只能限制模型输入，不能作为身份边界。
3. 每个 TopicAspect 必须有合法 BlockAspectSupport；每个 support 必须属于同一 Topic 的 source block。
4. 在线 Block 数量不能随 source document 数线性增长；Aspect 内默认只选一个代表。
5. map／profile aspect schema／vector bundle／convergence 状态必须可追溯且不静默降级。

### 错误代价与非对称门禁

| 错误类型 | 具体例子 | 用户影响 | 处理分类 | 指标与阈值 |
| --- | --- | --- | --- | --- |
| Candidate／证据丢失 | Topic 合并后 source block 消失 | 无法回源 | 阻断 | 输入 candidate/block 集合与 map 全等 |
| Aspect 悬空 | support 指向不存在 block/aspect | 查询错误 | 阻断 | 悬空引用=0 |
| 全局未收敛 | pass 上限时仍有待处理高相似候选 | 重复 Topic/Group | 阻断自动激活 | `ready_partial`，不自动 active |
| 在线候选线性膨胀 | Block 候选随文档数增长 | 成本和噪声失控 | 阻断 | 不触安全护栏；相同事实去重 |
| c13 核心漏项 | 七组核心证据缺失 | 无法回答目标问题 | 阻断 | 7/7 核心、≥3/4 扩展 |
| 噪声超限 | 返回大量弱相关 blocks | 稀释上下文 | 阻断 | precision≥0.80；无关 token≤20% |

### 明确禁止

- 不得把 summary、TopicAspect 或 reason 当事实证据返回。
- 不得把固定 Group／Topic／Aspect／Block 数量当作质量目标。
- 不得因达到 pass 上限而把未收敛地图标成 `converged`。
- 不得在查询时自由创建未编译 Aspect 并直接进入结果。
- 不得写入 c13 文档 ID、qrels 或核心组名到通用 Prompt。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| Topic/Group 未收敛 | `ready_partial` 和计数诊断 | 全部中间 artifact | 调整预算/规则后重建 |
| Aspect 编译非法 | `knowledge_map_aspect_invalid` | 旧 active map 不变 | 修复后重建 |
| Aspect support 悬空 | 地图不发布或查询显式失败 | 诊断和输入 artifact | 修复后重建 |
| 在线候选超护栏 | 显式 budget error | 完整阶段计数 | 调容量或修复构图 |

### 人工检查点

- 编码前：确认 085 切片边界和 B 级实现决策。
- 中途：新 map 可加载，Aspect/support 引用完整。
- 完成后：查看 c13-003 三轮结果、重复率和诊断。

### 决策预算

- 预计 B 级决策：6；设计收口阶段新增 D7，共 7 个 B 级决策。
- C 级决策：无；新 schema 不兼容旧 map，但旧 map 保留不删除，可重建。
- 超过预算时：拆为“编译侧 map v4”和“Aspect-first 查询”两个切片。

## 目标

```text
Preview candidates
→ global Topic convergence
→ Profile Aspect Schema + TopicAspect/BlockAspectSupport
→ global Group convergence
→ split map/vector artifacts
→ adaptive Group → all Topics → Aspect → representative Block
→ c13-003 verification
```

## 输入与输出

输入：当前冻结 Profile 的 Preview Run、chat provider、embedding provider、snapshot block vectors。

输出：schema v4 不可变 Knowledge Map、Aspect 元数据、全局收敛诊断、c13 查询 evidence bundle。

## 不包含

- Aspect Schema 配置 UI。
- 历史 v3 map 兼容。
- 50／100／1,000 篇正式验收。
- 最终答案和 Judge。

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 |
| --- | --- | --- | --- | --- | --- |
| A1 | 身份可追溯 | 全部 candidates 进入唯一 Topic，全部 source blocks 保留 | 集合差异 | 0 | 单元／集成 |
| A2 | 全局收敛 | semantic batch 不再是身份边界 | 跨 batch accepted merge 探针；未收敛状态 | 静默收敛=0 | 单元 |
| A3 | Aspect 完整 | 每个 TopicAspect 有合法 support，每个 block 有 support/exclusion | 悬空／未分类无原因 | 阻断反例=0 | 单元／集成 |
| A4 | 查询不线性膨胀 | 选中 Group 展开全部 Topics，但 Aspect 后只选代表 Blocks | 模拟重复文章 | 文档增而 block 不线性增 | 单元／运行探针 |
| A5 | c13 覆盖 | 15 篇核心 7/7、扩展≥3/4 | 现有 gate | 连续 3 次 | 真实运行 |
| A6 | c13 噪声 | 控制无关 token | precision／irrelevant tokens | precision≥0.80；≤20% | 真实运行 |

## 验收执行

- [x] 用户可观察的主路径成立。
- [x] 核心不变量已被自动化测试或明确的人工验收覆盖。
- [x] 失败路径不会破坏旧状态或伪装成功。
- [ ] 验收矩阵中的阻断项全部通过（A6 噪声稳定性未通过）。
- [ ] 未通过的已知反例已修复，或由负责人明确接受风险。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 085-D1 | B | Aspect Schema 首版如何产生 | 使用系统固定 Kind 集合并按冻结 Profile 派生 base schema；编辑 UI 暂缓 | 13 §6.7 | 不从空表要求用户维护，不做任意自定义 Kind | 后续可增加 Profile override | 单元／c13 | 待确认 |
| 085-D2 | B | 全局候选图如何实现 | 当前规模使用本地精确向量近邻；另加 lexical、alias、共享 Block、child Topic 和来源文档 blocking；未变化 cluster 复用向量，已审阅 edge 不重复调用；更大图后续换 LanceDB ANN | 13 §7.7 | 不做 N² Prompt，不把 batch 当边界；blocking 只表示候选，不直接合并 | 构图成本和候选边数量增加，但减少重复模型调用 | 跨 batch／多 blocking 探针 | 待确认 |
| 085-D3 | B | Aspect 编译批次与未分类处理 | Catalog-first；按 Topic 和 Block 双预算分批；每个 Block 必须支持某 Aspect 或给出结构化 exclusion；漏项只做一次定向补映射；`missing_aspect` 触发一次受控 catalog 扩展并重映射 | 13 §7.6.4 | 不用“模型漏输出”自动伪造 exclusion；不整批重复调用，也不逐 Block 自由生成 Aspect | 控制输出漏项和修复成本 | 单元／真实 map | 待确认 |
| 085-D4 | B | map artifact 拆分 | manifest JSON + normalized vector bundle 分离，v4 不兼容 v3 | 13 §8.7.2 | 不做双读迁移 | 旧 map 需重建 | 加载／hash 测试 | 待确认 |
| 085-D5 | B | Group/Aspect 查询阈值 | Group high／rescue／Topic coverage 为 0.61／0.52／0.50；Aspect 阈值和成本护栏未动 | 13 §8.7.3／§8.7.5 | 不调硬护栏；不写 fixture 特例 | 参数仍需 held-out 主题确认 | c13 三次 | 待确认 |
| 085-D6 | B | 未收敛地图发布 | `converged` 自动激活；`ready_partial` 可保存但默认拒绝激活，只有显式 `confirm_ready_partial=True` 才允许；结构错误 failed 永不激活 | 13 §7.7.5 | 不把未收敛伪装成 ready；把人工确认做成可审计的显式参数 | 影响 active map 选择 | 单元 | 待确认 |
| 085-D7 | B | Aspect Schema 如何参与 Profile／地图身份 | 冻结 Profile 与 `profile_hash` 不变；active pointer v2 联合校验 snapshot、Profile、`profile_aspect_schema_hash` 和 `aspect_policy_version` | 13 §6.7／§8.7.2 | 不把系统派生 Aspect 写回冻结 Profile；也不只按最新地图猜测兼容性 | Aspect policy 变化可使旧地图失效，但 Profile 仍保持稳定身份 | 单元／真实 active 回读 | 已确认 |
| 085-D8 | A | 查询 coverage 修复和 diagnostics | `missing` 只用第二个已编译 support 修复；没有第二 support 时保留 missing；保留 duplicate／conflict／coverage diagnostics | 13 §8.7.6／§8.7.7 | 不用被审核拒绝的代表 Block 伪装修复，不新增运行时 Aspect | 提高解释性并保持公共 payload 不变 | 定向查询探针 | 已实现 |

## 证据包

### 自动化证据

- 命令：`pytest backend/tests/test_profile_knowledge_map.py -q --basetemp D:\lida-data\vscode\ai-study-helper-v2\.pytest-085-targeted`
- 结果：16 passed。
- 命令：`pytest backend/tests -q --basetemp D:\lida-data\vscode\ai-study-helper-v2\.pytest-085-full`
- 结果：退出码 0。
- 命令：本切片涉及的 map／model／test／scripts 文件 Ruff format、Ruff check。
- 结果：通过。
- 命令：`pyright --venvpath D:\lida-data\vscode\ai-study-helper-v2\backend --project backend`
- 结果：0 errors。
- 覆盖的不变量：map v4 schema、vector bundle hash、Topic／Group 收敛、Aspect catalog／extension、
  support／exclusion 完整、跨文档候选 blocking、`ready_partial` 激活门禁、查询期 Block vector
  复用和 c13 覆盖／噪声。

### 运行证据

- 启动方式：`scripts/wiki_topic_v2_probe.py`，冻结 snapshot `s_6b787258ba7d700c`、
  Preview Run `pr_ca0dd3ab3f41430f`、Profile `ep_system_technical_learning_v1`。
- 用户操作序列：构建 15 篇 v4 map，显式激活，再以 topic `AI Agent 隔离与控制边界`
  连续查询三次。
- 预期结果：核心 7/7、扩展≥3/4、precision≥0.80、无关 token≤20%、无悬空引用。
- 实际结果：active map `km_c046f7d55ceba425`；736 topics、490 groups、2,391 aspects、
  2,542 supports、19 个 `weak_background` exclusions；Topic／Group 均为 `converged`。
  active pointer 为 v2，Profile hash、Aspect Schema hash `b685512f...a455` 和
  `profile-knowledge-map-aspect-catalog-v1` 与 map manifest 一致。
  合同收口前曾有三轮通过；最终代码复跑时核心仍为 7/7、扩展 3/4，但噪声未稳定：
  run A 返回 38 blocks、precision 0.789、无关 token 22.83%；
  run B 返回 52 blocks、precision 0.673、无关 token 31.93%。

### 失败探针

- 注入或复现的失败：mapping 同时 support／exclude；越界 Block exclusion；
  合法 Block 漏映射；Aspect Schema hash 与 active map 不一致；`ready_partial` 尝试激活；
  旧 v3 active map 被 v4 读取；c13 Group routing 单次选择过宽。
- 系统保留的旧状态：Aspect 编译失败时不写新 map；旧 active map 不因失败改变。
- 用户看到的错误：映射非法返回 `knowledge_map_aspect_mapping_invalid`；旧 map 返回
  `knowledge_map_invalid`；未确认 partial map 返回 `knowledge_map_activation_not_ready`。

### 已知反例账本

| 场景或反例 | 期望行为 | 实际结果／分布 | 是否阻断 | 处理结论 |
| --- | --- | --- | --- | --- |
| 安全上限时仍有可合并对象 | 不标记 converged，默认不能激活 | `ready_partial` 合成探针证明默认拒绝激活 | 是 | 已验证 |
| 相似文章重复同一事实 | 只保留代表 Block | 最终代码 c13 分别返回 38／52 个去重 Block，未全量返回但噪声不稳定 | 是 | 待设计审阅 |
| 映射模型同时给出 support 与 block-level exclusion | 具体 support 优先，去除冗余 exclusion 并记录 usage | v4 build 实际出现 8 次并确定性修复；定向探针通过 | 否 | 已修复 |
| 映射模型返回越界 Block reference | 删除越界引用并记录；合法输入仍须完整分类 | v4 build 实际出现 3 次并确定性修复；定向探针通过 | 否 | 已修复 |

### 人工验收

- [ ] c13-003 三轮结果和 Aspect/support 质量由沛东复核。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么把 Aspect 编译放在离线阶段，而不是继续查询时动态发现？ | Profile 预定义与编译 artifact 便于审查；减少运行抖动并避免每次查询重复生成；成本由多次查询转为一次编译；snapshot／Profile 变化后必须重建，旧地图只代表旧输入 | 通过 | 后续验证 Aspect 跨运行稳定性 |
| R2 | 为什么 semantic batch 只能控制输入规模，不能成为全局身份边界？ | 待填写 | 待核对 | 待填写 |
| R3 | 为什么 `ready_partial` 不能自动激活，而 uncertain 可保守分开？ | 待填写 | 待核对 | 待填写 |
| R4 | 为什么每个 Aspect 默认只选一个代表 Block，什么条件才允许保留第二个？ | 待填写 | 待核对 | 待填写 |
| R5 | 为什么本轮只调 Group 相关性阈值，而不通过固定 Group／Block 数量或降低硬护栏来改善噪声？ | 待填写 | 待核对 | 待填写 |

## 协作复盘

- AI 明显加速的环节：
- 负责人失去系统模型的环节：
- 真正发现问题的控制点：
- 只有仪式感、没有产生判断价值的流程：
- 下一次准备调整的一到两项：

## 已知风险与后续

- 重要回归：084 地图 `km_7609264fcc3cf100` 的 283 个初始 Groups 经三轮串行归并得到
  150；085 的 `km_c046f7d55ceba425` 改成重叠候选邻域后，global pass 仍允许重新输出 Group
  decomposition，最终膨胀到 490。Group membership 也从 767 增至 2,377。084／085 manifest
  都写 `semantic-groups-v7`，未正确反映执行语义变化；086 已用 merge-only policy v8 修复
  版本与执行语义，并记录该回归。
- 未验证项：50／100／300／1,000 篇规模；跨运行 Aspect 分类稳定性；跨 support 冲突发现。
- 当前阻断项：c13-003 最终代码的 Group routing 会偶发选择过多宽 Group，导致噪声门禁失败；
  覆盖指标不受影响。
- 接受的风险：首版 Aspect Schema 没有完整编辑 UI；离线全局候选图仍为本地精确向量扫描。
- 后续待办或 ADR：全局 ANN 索引优化、Aspect Schema 编辑、增量构图、冲突／多代表探针。

## 停止与推翻条件

- Aspect catalog 无法稳定覆盖 Blocks，导致大量 unclassified。
- 全局候选图漏掉明显同义对象且无法通过多种 blocking 修复。
- 在线 Aspect-aware 查询无法保持 c13 覆盖和噪声门禁。
