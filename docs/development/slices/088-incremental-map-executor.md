# 088 · 增量 Knowledge Map Executor

- 状态：MVP 执行器实现完成；从零 v5 地图可构建，但固定地图 c13 稳定性失败，
  完整 Group Ledger 裁决待后续切片
- 开始日期：2026-11-09
- 关联设计：[13 增量 Ledger 知识地图编译](../../design/13-profile-driven-wiki-evidence-retrieval.md)
- 前置切片：[087 增量 Ledger 内核与 Delta 计划](087-incremental-map-ledger-core.md)
- 人工验收：增量新增、更新、删除后的 `search_wiki_topic_v2` 证据结果

## 控制契约

### 背景与失败场景

- 触发问题：087 只能计算 delta，不能发布新地图；问答侧仍只能使用旧 active map。
- 用户损失：新增或修改文档后，检索工具看不到新知识，或必须全量重建。
- 现有流程不足：没有 typed Topic／Group operation executor、dirty Aspect 重编译和
  incremental map publish。
- 本切片负责：no-op 复用；full fallback；新增／来源更新／删除的 Topic revision；
  dirty Aspects 重编译；Group attach／create；不可变 v5 map 发布；查询回归。
- 本切片不负责：复杂 Topic merge／split 的 LLM 裁决、Group merge／split／relabel 的完整
  集合、周期全局维护、UI 和 50 篇规模结论。
- 停止条件：增量地图无法通过查询回源；未变化 Topic ID 大面积变化；新增 Topic 无法获得
  primary Group。

### 用户可观察目标

1. 同一输入调用 incremental build 返回 no-op，不生成新地图。
2. 新增候选后生成新 map，`search_wiki_topic_v2` 能返回新增原文块。
3. 修改候选后原 Topic 身份保留，查询能使用修改后的 source/Aspect。
4. 删除最后 source 后 Topic 和空 Group 不再出现在新地图。
5. 未变化 Topics／Groups 保持 ID，不重新调用 chat group resolver。
6. 新 map 记录 base map、编译模式和 delta summary。

### 核心不变量

1. no-op 不写新 map，不改变 active pointer。
2. 新 map 只引用目标 snapshot 的合法 Block；每个 Block 必须有 Aspect support 或 exclusion。
3. 每个 active Topic 至少属于一个 Group。
4. 未 dirty 的 Topic／Group 保留原 ID 和语义内容。
5. 失败的新地图不发布，旧 active map 不变。

### 错误代价与非对称门禁

| 错误类型 | 具体例子 | 用户影响 | 处理分类 | 指标与阈值 |
| --- | --- | --- | --- | --- |
| 新文档不可检索 | 新 Topic 无 Group／Aspect support | 检索工具看不到新知识 | 阻断 | 新增目标查询命中=0 |
| 旧对象漂移 | 新增一个 candidate 导致无关 Topic ID 改变 | 结果不可解释 | 阻断 | unchanged Topic ID 差异=0 |
| 来源悬空 | 新 map 引用旧 snapshot Block | 回源失败 | 阻断 | 悬空引用=0 |
| 静默局部失败 | Group 更新失败但 active map 切换 | 用户使用错误地图 | 阻断 | 失败发布=0 |

### 明确禁止

- 不得用全量 rebuild 冒充增量执行。
- 不得修改 base map artifact。
- 不得让新 Topic 无 primary Group。
- 不得重新调用未 dirty Topics 的 Aspect 编译。
- 不得用 c13 文档 ID 或固定 Group 名称作为增量规则。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| Topic resolver 尚不支持的变化 | 显式 `incremental_topic_resolution_required` | base map 和 delta plan | 可转 full 或后续实现 |
| Group decision 非法 | `incremental_group_update_invalid` | base map | 修复后重试 |
| Aspect 编译失败 | 地图不发布 | base map 和诊断 | 可重试 |
| 发布校验失败 | 新 artifact 不可 active | 旧 active map | 可重建 |

### 人工检查点

- 中途：no-op 和新增 Topic 的新 map 能加载。
- 完成后：用 `search_wiki_topic_v2` 查询新增 Topic，并检查原文 block 来源。

### 决策预算

- B 级决策：4。
- C 级决策：无。

## 目标

```text
IncrementalMapPlan
→ Topic source revisions / retirement
→ dirty Topic Aspect compilation
→ Group attach / create
→ vector reuse + dirty vector rebuild
→ immutable v5 map
→ search_wiki_topic_v2 verification
```

## 输入与输出

输入：base map、完整目标 Preview Runs、chat provider、embedding provider。

输出：base 复用的 no-op，或新的 schema v5 不可变地图及查询结果。

## 不包含

- semantic Topic merge／split；
- Group merge／split／relabel；
- 周期全局维护；
- UI 激活按钮；
- 50／1,000 篇规模结论。

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 |
| --- | --- | --- | --- | --- | --- |
| A1 | No-op | 相同目标直接复用 base | 新 map=0 | 任一写入 | 集成 |
| A2 | 新增可检索 | 新 Topic 有 Group、Aspect 和 Block support | 查询命中新增 Block | 任一缺失 | 集成 |
| A3 | 更新复用身份 | 更新 source 后 Topic ID 不变 | ID 差异=0 | 任一无关变化 | 集成 |
| A4 | 删除清理 | retired Topic 不再引用，空 Group 删除 | 悬空／orphan=0 | 任一残留 | 集成 |
| A5 | 未变化复用 | unaffected Topics／Groups 不重算 | reuse counts 精确 | 任一无关 LLM 调用 | 集成 |
| A6 | 查询效果 | 新 map 可由 search tool 读取并返回原文 | evidence 可回源 | 查询失败 | 集成／真实运行 |

### 冻结与变更

- 冻结时点：2026-11-09。
- 阻断反例：新增知识不可检索、旧 active 被误切换、悬空 source。
- 变更记录：无。

## 验收执行

- [x] 用户可观察主路径成立。
- [x] 核心不变量覆盖。
- [x] 失败不破坏旧状态。
- [x] 本轮 MVP 验收矩阵通过。
- [ ] 已知反例处理。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 088-D1 | B | v5 与 v4 读取兼容 | manifest schema 接受 v4/v5，新构建写 v5 | 13 §7.5 | 不强制删除旧 active map | 保留历史 artifact | 加载测试 | 待确认 |
| 088-D2 | B | Topic 精确匹配 | 新 source 先按 normalized name／alias 附到已有 Topic；其余创建新 Topic | 13 §7.6.3 | 不做无证据 semantic merge | 控制错误合并 | 单测 | 待确认 |
| 088-D3 | B | Group 最小操作集 | 先实现 primary attach_topic 和 create_group；merge/split 留后续 | 13 §7.7.3 | 保证新增 Topic 有检索区域 | 不解决已有 Group 重复 | 集成 | 待确认 |
| 088-D4 | B | v5 增量元数据 | manifest 记录 base_map_id、compilation_mode、delta_summary | 13 §7.5.3 | 不把 provenance 只写 usage | 增加 schema 字段 | 加载测试 | 待确认 |
| 088-D5 | B | `parallel_work_state` 证据口径 | 改为“并行工作与持久状态控制”，要求工作现场隔离与持久状态保存／恢复两个子关系同时命中；`b00025` 证明前者，`b00016`／`b00013` 任一证明后者 | c13-003 原始问题、13 §1.3；沛东 2026-11-09 确认当前口径过宽 | 不再让并行性能、提示工程、终态评估或单独状态恢复冒充完整边界 | 收紧核心组 Gate；旧运行需按新口径重判 | 单测与既有 artifact 重判 | 方向已确认；criterion v6 draft 待审核 |
| 088-D6 | A | Group Router 断点的隔离诊断 | 增加仅离线可用的 `forced_group_ids`，强制 Group 必须已进入正式候选集，只绕过 LLM Router，继续执行 Topic、Aspect、证据审核和原文回源 | 13 §8.3、§8.4；当前五次候选集合稳定但相关 Group 未被选中 | 直接修改 Router 无法区分后续链路是否仍会丢证据 | 不进入产品工具参数 | 单测与两组真实 oracle 探针 | 已执行 |
| 088-D7 | B | Group 覆盖缺口审查 | 增加独立第二次 LLM 审查，只从未选候选中补“现有选择未覆盖的独立检索维度”；当前证据未证明稳定收益，默认关闭，仅诊断探针显式开启 | 13 §8.4；沛东同意先验证“Router 漏选是否为唯一断点” | 与 Router 同提示词合并会让职责混杂；默认开启会增加一次大输入调用 | 默认产品路径不改变；保留实验入口 | 单测与两组真实探针 | 待确认 |

## 证据包

### 自动化证据

- 命令：`pytest backend/tests/test_profile_knowledge_map.py -q`
- 结果：35 passed。
- 命令：`pytest backend/tests/test_c13_wiki_v2_gate.py -q`
- 结果：3 passed；覆盖子条件单独命中、完整命中和工作区隔离单边命中失败。
- 命令：`pytest backend/tests/test_profile_knowledge_map.py::test_search_wiki_topic_v2_builds_map_and_returns_bounded_evidence -q`
- 结果：通过；覆盖 forced-group 跳过 Router、0 次 Router 调用和候选外 Group 显式失败。
- 命令：`pytest backend/tests/test_profile_knowledge_map.py::test_group_coverage_audit_supplements_an_unselected_candidate -q`
- 结果：通过；覆盖只从 unselected candidates 补选并校验组内支撑 Topic。
- 命令：`pytest backend/tests -q`
- 结果：458 passed，退出码 0。
- Ruff format／check、Pyright、comments check、frontend typecheck：通过。

### 运行证据

- 真实 no-op：`km_b07c05f74865252a` + `pr_ca0dd3ab3f41430f` 返回 base map，未写新 map。
- 真实 v4 兼容 query：`search_wiki_topic_v2("AI Agent 隔离与控制边界")` 返回 33 blocks；
  precision 0.8485、无关 token 14.85%，c13 gate 通过。
- 从零 full build：新 v5 map `km_e79e342956ed8eb2`，`base_map_id=null`、
  `compilation_mode=full`，739 Topics、224 Groups。
- 从零 v5 首轮查询：32 blocks，核心 7/7、扩展 3/4，precision 0.875、无关 token
  10.99%，gate 通过。
- 同一 v5 地图继续查询四次：只有首轮通过；后四轮均缺 `parallel_work_state`。
  selected Groups 为 14～17，候选 Group 始终为 53；相关 10 个候选 Group 都存在，
  失败来自 LLM routing 没有选中任何一个包含目标证据的候选 Group。
- Oracle routing `wg_51275650cae15186 + wg_f4e4fbeda1a2ae85`：最终返回 7 blocks，
  `b00025` 与 `b00016` 都被直接选中，证明两个子条件在 Topic、Aspect、证据审核和原文回源
  阶段可以贯通。
- Oracle routing `wg_51275650cae15186 + wg_b05da8b21b845859`：返回 36 blocks，
  `b00025` 被选中；`b00013` 已进入 candidate blocks，但没有通过 Aspect seed／证据审核。
  因而 `wg_b05...` 不能作为持久状态恢复边界的稳定代表 Group。
- Group 覆盖审查第 1 轮：基础选择 15 Groups，补选 `wg_e027...`（宿主执行架构）和
  `wg_7a99...`（harness 自编辑边界），仍未返回 `b00016`；精确率 0.8286、无关 token
  18.85%，但核心仅 6/7。审查输入 39,232 tokens，并发生一次结构化输出重试。
- Group 覆盖审查第 2 轮：补选为空，核心 6/7；审查输入 38,635 tokens，同样发生一次重试。
  两轮均说明通用“覆盖缺口”判断会选择模型认为独立但并非 c13 必要的其他边界。
- 2026-11-09 收紧 `parallel_work_state` 后重判既有五次 v5 artifact：五次均为核心 6/7。
  首轮只返回工作区隔离 `b00025`，缺持久状态保存／恢复；其余四次两个子关系都未命中。
  因此旧口径下首轮“通过”不再成立。
- 增量新增：新增 `工具输出风险协调` 后生成 schema v5 地图，base map 保留，原 4 个 Topic ID
  全部保留。
- 增量新增查询：`search_wiki_topic_v2("工具输出风险协调")` 返回新增候选绑定的原始 block。
- 增量更新：原 Topic ID 保持不变，summary 和 source candidate 更新，检索可返回原文。
- 增量删除：删除最后 source 后 Topic 与对应 membership 消失，没有空 Group。
- 验证入口：`scripts/wiki_incremental_build_probe.py`。

### 失败探针

- no-op 写文件；
- 新 Topic 无 Group；
- 旧 Block 跨 snapshot；
- 失败前 active map 改变。
- retryable chat provider error 会进行一次带诊断的重试，不把瞬时网络错误直接当作地图失败。

### 已知反例账本

| 场景或反例 | 期望行为 | 实际结果／分布 | 是否阻断 | 处理结论 |
| --- | --- | --- | --- | --- |
| 同内容换 Run | no-op 并复用 base map | 单测通过 | 否 | 已验证 |
| 新增 Topic | 新 map 可被 search tool 返回 | 单测命中新增 block | 否 | MVP 已验证 |
| 更新 source | 保留 Topic ID 与检索能力 | 单测通过 | 否 | MVP 已验证 |
| 删除最后 source | Topic／membership 清理 | 单测通过 | 否 | MVP 已验证 |
| new map v5 读取 | 查询链路兼容 v5 | 全量测试通过 | 否 | 已验证 |
| 从零 v5 固定地图最差运行 | 五次都应通过 c13 | 仅 1/5 通过，4 次缺 parallel_work_state | 是 | 待修复 routing 稳定性 |
| 工作区隔离单边命中 | 不得冒充完整的并行工作与持久状态控制 | 旧首轮命中 `b00025`，新 Gate 仍阻断 | 是 | 已通过失败探针验证 |
| 强制选择宽上下文 Group | 选中 Group 后仍应能返回直接状态证据 | `wg_b05...` 中 `b00013` 进入候选但被后续审核丢弃 | 是 | 该 Group 不适合作为稳定代表 |
| 通用覆盖缺口审查 | 应稳定补足 Router 漏掉的必需独立边界 | 两轮分别补非目标维度或补空，且各一次输出重试 | 是 | 默认关闭，不能替代 Group 重构 |

### 人工验收

- [ ] 待填写

## 反向解释

待实现后填写。

## 已知风险与后续

- 本切片不是完整 Group Ledger executor；只覆盖新增 Topic 的 attach/create。
- 当前 Group 放置仍是确定性 top-candidate attach／singleton create，不是完整 LLM
  attach／create／merge／split 裁判；该规则只用于打通可检索主路径。
- 当前 executor 只允许目标 Preview Runs 与 base map 使用同一 snapshot；跨 snapshot 的
  Block 身份重绑尚未实现，遇到时显式失败。
- 从零 v5 地图首轮检索通过，但固定地图重复查询仅 1/5 通过；候选集合稳定，缺口发生在
  LLM Group routing，而不是 v5 map artifact 或 Block 回源。
- 按收紧后的 c13 证据口径，已有五次 v5 artifact 全部未覆盖 `parallel_work_state`；此前
  首轮通过不能继续作为质量证据。
- Oracle 诊断表明 Group Router 是正常五次查询的首要缺口；强制
  `wg_512... + wg_f4...` 可以贯通两个必需子条件。当前剩余问题是该组合尚未被正常 Router
  选中，而且宽 Group 同时带入脚手架噪声。
- 通用覆盖缺口审查会选择“宿主执行架构”“harness 自编辑边界”等其他合理边界，不能推导出
  金标准指定集合；当前成本约 38k～39k input tokens/次，默认关闭。
- 下一阶段必须重构 Group 的检索职责与成员，使并行工作区和持久状态恢复成为清晰、独立的
  retrieval region，而不是继续给 Router 或覆盖审查堆叠通用措辞。
- Topic semantic merge／split、Group merge／split／relabel、周期维护仍需后续切片。

## 停止与推翻条件

- 无法在不全量重编译 Topic／Group 的前提下保持检索效果；
- 增量 map 的查询质量显著低于同输入 full build；
- 未变化对象无法稳定复用。
