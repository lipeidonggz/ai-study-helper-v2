# 087 · 增量 Knowledge Map Ledger 内核与 Delta 计划

- 状态：Ledger／Delta Planner 实现完成；MVP typed executor 已在 088 接通，设计决策待确认
- 开始日期：2026-11-09
- 关联设计：[13 增量 Ledger 知识地图编译](../../design/13-profile-driven-wiki-evidence-retrieval.md)
- 前置切片：[086 Group 生成与收敛稳定性](086-group-convergence-stability.md)
- 人工验收：固定地图 no-op、增量新增、来源修改、删除后的 delta 分类与局部 dirty 范围

## 控制契约

### 背景与失败场景

- 触发问题：当前 `ProfileKnowledgeMapService.build()` 每次都从全部 Preview Candidates
  重建 Topic、Aspect、Group 和 vectors；`reused` 只复用完全相同的最终 artifact，不是增量。
- 用户损失：地图重复编译产生 Group partition 漂移；新增一篇文档也可能改变无关 Groups。
- 现有流程不足：没有跨 Run 的 candidate source key、稳定 Ledger、delta journal 或局部
  affected frontier。
- 本切片负责：把已有 map 投影为可验证的 Ledger snapshot；比较目标 Preview Runs 和 base
  map；产出 added／removed／unchanged candidates、Topic delta 和局部受影响 Groups；
  full build 以 empty base + all candidates 表示。
- 本切片不负责：调用 LLM 执行 Topic／Group 增量操作、写入 v5 active map、删除持久化 Ledger、
  UI 和 50／1,000 篇规模。
- 停止条件：无法从现有 snapshot／candidate 数据构造稳定 source key；无法证明未变化对象不
  进入 dirty frontier。

### 用户可观察目标

1. 同一 base map 与同一候选集合生成 no-op plan，不改动任何 Topic／Group。
2. 新增 candidate 被准确识别，只标记候选 Topic 为 pending，并给出有界邻近 Groups。
3. 候选来源删除能定位其当前 Topic、原 Group memberships 和局部 neighbor Groups。
4. 修改 candidate 能区分 unchanged、revised 和 removed sources。
5. 所有计划都是只读 artifact，不改变 active map。

### 核心不变量

1. 每个目标 candidate 恰好属于 unchanged 或 added；每个 base candidate 恰好属于
   unchanged 或 removed。
2. source key 不依赖 candidate ID 或 Preview Run ID，跨 Run 内容相同必须得到同一 key。
3. 未变化 Topic 不进入 topic dirty set；未受影响 Group 不进入 group candidate set。
4. Plan 只读，旧 active map、旧 manifest 和 vector artifact 不得变化。
5. base 为空时，全部候选进入 added，因此 full build 是 increment 的特例。

### 错误代价与非对称门禁

| 错误类型 | 具体例子 | 用户影响 | 处理分类 | 指标与阈值 |
| --- | --- | --- | --- | --- |
| Source 误复用 | 文档内容变了但判为 unchanged | 旧证据进入新地图 | 阻断 | fingerprint 不完整=0 |
| Source 误删除 | 同一内容换了 Run 被判 removed | 不必要重编译 | 阻断 | 跨 Run 同内容映射失败=0 |
| Dirty 扩散 | 新增一个候选使全部 Groups 进入计划 | 增量收益消失 | 阻断 | unrelated Group dirty=0 |
| 计划副作用 | 生成 plan 修改 active map | 用户看到错误新地图 | 阻断 | artifact hash 差异=0 |

### 明确禁止

- 不得用 candidate ID、run ID、ordinal 或数据库 rowid 作为跨 Run identity。
- 不得在 planner 中调用 chat model。
- 不得把 added candidate 自动当作新 Topic 或新 Group。
- 不得静默丢弃无法识别的 base source。
- 不得把“本批变化 Runs”误当作完整目标范围，否则未传入的旧文档会被误判删除。
- 不得在 plan 阶段发布新 map 或切换 active pointer。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| Base candidate row 缺失 | `incremental_source_identity_missing` | base manifest 和全部已找到 rows | 可修复数据后重试 |
| Preview Run scope 不一致 | `knowledge_map_scope_mismatch` | 不产生 plan | 可重新选择 Runs |
| Snapshot／Profile／pipeline 不匹配 | 显式 contract error | base map 不变 | 可 full rebuild |
| Plan 含未分类 delta | `incremental_delta_invalid` | 诊断和输入 key | 可修复指纹逻辑 |

### 人工检查点

- 编码前：确认 source fingerprint 字段和 legacy v4 projection 方案。
- 中途：同一 15 篇输入生成 no-op plan。
- 完成后：手工检查 added／revised／removed 和受影响 Groups。

### 决策预算

- 预计 B 级决策：4。
- C 级决策：无。
- 超过预算时：plan 内核与 Ledger 执行器拆成两个切片。

## 目标

```text
base immutable map
+ target Preview candidate set
→ source fingerprint comparison
→ Topic delta plan
→ affected Group frontier
→ read-only IncrementalMapPlan
```

## 输入与输出

输入：

- base map ID；
- target snapshot／覆盖完整目标文档范围的 Preview Runs；
- 当前 Profile 和编译契约；
- chat／embedding provider 仅用于后续 executor，planner 不调用 chat。

输出：

- `IncrementalMapPlan` JSON；
- added／removed／unchanged source keys；
- created／revised／retired Topic candidates；
- affected current Groups；
- bounded candidate Groups；
- blocker diagnostics。

## 不包含

- 实际生成新 Topic／Group 名称；
- LLM attach／merge／split；
- 新地图发布和 active pointer 切换；
- 文档数据库 schema migration；
- UI 和查询链路切换。

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 | 结果 |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | No-op 不 dirty | 与 base 完全相同的 candidates 全部 unchanged | unchanged=全部；dirty=0 | 任一 unrelated dirty | 单元／集成 | 通过；真实 map 801→801 |
| A2 | 跨 Run source identity | 内容相同、Run／candidate ID 不同仍匹配 | key 相等 | 任一错配 | 单元 | 通过 |
| A3 | 新增 | 新 candidate 只形成 pending source | old Topics 不变；added 精确 | 任一旧对象修改 | 集成 | 通过 |
| A4 | 删除 | removed source 映射到当前 Topic 和 Groups | 引用链完整 | 悬空引用 | 集成 | 通过 |
| A5 | 局部性 | 新增 Topic 候选 Groups 有界 | candidate Groups 数 ≤ 安全上限 | 全图 Groups | 集成 | 通过；≤16 |
| A6 | 只读 | plan 前后 base artifact hash 不变 | 差异=0 | 任一写入 | 集成 | 通过 |

### 验收冻结与变更

- 冻结时点：2026-11-09。
- 已确认的阻断反例：cross-run ID 作为 identity、no-op 仍 dirty、plan 修改 active map。
- 验收变更记录：无。

## 验收执行

- [x] 用户可观察的主路径成立。
- [x] 核心不变量已被自动化测试覆盖。
- [x] 失败路径不会破坏旧状态。
- [x] 本轮 planner 验收矩阵阻断项通过。
- [ ] 已知反例修复或接受。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 087-D1 | B | v4 map 如何进入 planner | 投影为 legacy Ledger；stable ID 使用当前 map ID，membership role 暂标记 legacy | 13 §7.3／§7.5 | 不等同于正式 v5 build，但允许先验证 delta | 为后续 v5 冷启动提供测试底座 | 投影测试 | 待确认 |
| 087-D2 | B | source fingerprint 组成 | 文档身份／版本、normalized name、summary variants、source blocks、channel、candidate content hash | 13 §7.3.2 | 不用 candidate ID／run ID | 决定跨 Run 复用正确性 | 跨 Run 单测 | 待确认 |
| 087-D3 | B | full build 表示 | `base_map_id=None`，所有 target candidates 进入 added | 13 §7.8.1 | 不保留第二条全量 planner | 后续 executor 可统一 | full plan 单测 | 待确认 |
| 087-D4 | B | Group candidate 范围 | 变化 Topic 的旧 memberships + Group vector top-K + source overlap Groups，资源上限显式记录 | 13 §7.4.1／§7.7.2 | 不扫全图，不直接修改 membership | 决定增量成本和噪声 | 局部性测试 | 待确认 |

## 证据包

### 自动化证据

- 命令：`pytest backend/tests/test_profile_knowledge_map.py -q`
- 结果：30 passed。
- 命令：`pytest backend/tests -q`
- 结果：453 passed，退出码 0。
- Ruff format／check：通过。
- Pyright：0 errors。
- `comments:check` 和 frontend `vue-tsc --noEmit`：通过。

### 运行证据

- 固定 map：`km_b07c05f74865252a`
- 目标 Preview Run：`pr_ca0dd3ab3f41430f`
- 真实 no-op artifact：
  `.runtime-087/real-noop-plan.json`
- 结果：`status=noop`，base／target 都是 801 candidates，15 个 documents 全 unchanged，
  801 source keys unchanged，736 Topics 全 unchanged，affected Groups=0，
  pending Group candidates=0。
- active map、manifest 和 vectors 未发生修改。

### 失败探针

- 缺失 candidate row；
- 跨 Run 同内容；
- 删除最后 source；
- base artifact hash 前后比较。

### 已知反例账本

| 场景或反例 | 期望行为 | 实际结果／分布 | 是否阻断 | 处理结论 |
| --- | --- | --- | --- | --- |
| 同内容换 Preview Run | 仍为 no-op | 单测中 candidate ID 全变，source keys 全相等 | 否 | 已验证 |
| 单 source 内容更新 | 旧 key removed、新 key added，并标记原 Topic revised | 单测通过 | 否 | 已验证 |
| 删除某 Topic 最后 source | 标记 Topic retired，并带出旧 Group memberships | 单测通过 | 否 | 已验证 |
| 新增 source | 只产生 pending source，不改旧 Groups | 单测通过，候选 Groups 有界 | 否 | 已验证 |
| 读取 planner | base map artifact 不变 | map.json／vectors.npz字节不变 | 否 | 已验证 |

### 人工验收

- [ ] 待填写

## 反向解释

待实现和人工验收后填写。

## 已知风险与后续

- legacy v4 projection 没有正式 primary／secondary membership 语义；正式 v5 冷启动必须重新生成。
- 当前切片只计划 delta，不执行 Topic／Group 操作。
- `status=full`／`incremental` 目前只是计划状态；尚未写出 v5 map。
- 后续：088 已接通新增／更新／删除 source、dirty Aspect 重编译、Group attach／create 和
  immutable v5 map publish；完整 Group merge／split／relabel 仍待实现。

## 停止与推翻条件

- 无法从现有数据构造跨 Run 稳定的 source key；
- no-op 无法保持零 dirty；
- bounded Group candidate 必须扫描全图才能成立。
