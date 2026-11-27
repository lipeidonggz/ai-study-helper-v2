# 086 · Group 生成与收敛稳定性

- 状态：实现审计待确认；A4 编译稳定性与 Build 3 质量阻断
- 开始日期：2026-11-09
- 关联设计：[13 Profile-Driven Wiki 证据检索](../../design/13-profile-driven-wiki-evidence-retrieval.md)
- 前置切片：[085 Aspect-aware 全局 Wiki 地图](085-aspect-aware-global-wiki-map.md)
- 人工验收：固定地图重复检索、固定代码重复编译、c13-003 覆盖与噪声

## 控制契约

### 背景与失败场景

- 触发问题：c046 地图从 268 个 initial Groups 变成 490 个 final Groups，Group convergence
  仍报告 `converged` 和 `accepted_merge_count=0`。
- 用户损失：大量语义重叠的 Groups 进入 routing，候选边界模糊；同一地图重复查询会因为
  LLM 选择了不同重叠子集而产生 38～52 个 Blocks 的波动。
- 现有流程不足：global 阶段允许模型重新提出任意 Groups，不是 merge-only；收敛使用净
  Group 数判断；最后只合并 child Topics 完全相同的 Groups。
- 本切片负责：Group 编译观测、merge-only 收敛、高置信重复合并、proposal Prompt 约束、
  routing 稳定性验证。
- 本切片不负责：Topic identity 重做、Aspect 编译重做、最终答案质量验证、1,000 篇正式规模。
- 停止条件：merge-only 无法减少重叠；确定性合并导致 Group 过宽；routing 波动无法改善。

### 用户可观察目标

1. 固定代码和输入重复编译时，Group 数量和 Topic membership 不再大幅膨胀。
2. global Group pass 只能 merge／keep，不能自由创建新的 Group decomposition。
3. 高度重复的 Groups 被合并，允许的多重归属有清晰检索职责。
4. 同一不可变地图重复查询时，选中 Groups、Topics 和 Blocks 的波动明显缩小。
5. c13-003 核心 7/7、扩展≥3/4，precision≥0.80、无关 token≤20%。

### 核心不变量

1. global merge 的每个输出必须由输入 Groups 组成；不能创建输入之外的 Group。
2. 单个 merge batch 内，一个输入 Group 最多进入一个输出。
3. merge 后 child Topics 必须是输入 child Topics 的并集，不能丢失 provenance。
4. Group 数、Topic membership 或 high-overlap pairs 增加时，不得报告稳定收敛。
5. 所有 Group 至少引用一个 Topic；所有 Topic 至少属于一个 Group。

### 错误代价与非对称门禁

| 错误类型 | 具体例子 | 用户影响 | 处理分类 | 指标与阈值 |
| --- | --- | --- | --- | --- |
| 静默重新分组 | merge pass 输出新 Group | 无法判断收敛 | 阻断 | 约束失败即整次 map 失败 |
| 输入 Group 重复消费 | 一个 Group 进入多个输出 | membership 膨胀 | 阻断 | 每次 batch 每个输入恰好消费一次 |
| 伪收敛 | Group 数增加仍标 converged | routing 继续不稳定 | 阻断 | churn/membership/high-overlap 必须进入状态 |
| 合并过宽 | 用大 Group 隐藏重叠 | 噪声和漏项风险 | 阻断 | 组内 Topic 语义离散度不得恶化 |
| routing 偶发扩张 | 同地图选中 18/23 Groups | 成本和噪声波动 | 阻断 | 重复运行最差值必须通过 |

### 明确禁止

- 不得用固定 Group 数、固定 membership 上限或固定输出 Blocks 作为质量目标。
- 不得把 summary、retrieval question 或 Group 名称当事实证据。
- 不得为通过 c13 写入 case-specific Group、Topic 或 Block 规则。
- 不得为了降低重叠而把不同对象、不同生命周期或不同作用范围强行合并。
- 不得用净 Group 数代替真实的 merge／split／membership churn 指标。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| merge 输出违反 merge-only | `knowledge_map_group_merge_invalid` | 旧 active map 和输入 artifact | 修复后重建 |
| 收敛仍有高重叠候选 | `ready_partial` | 全部 pass 诊断 | 调整规则后重建 |
| Group provenance 丢失 | 地图不发布 | 候选和 Topic 来源 | 修复后重建 |
| routing 最差运行超噪声 | c13 gate 失败 | query trace 和 selection artifact | 调整路由契约 |

## 四阶段实施与记录

### 阶段 1：基线观测与 artifact

**原问题**

c046 地图暴露的不是普通“Group 名还有点相近”，而是全图重复：268 个初始 Groups 经过
global grouping 后变成 490 个，`group_convergence` 仍写 `converged` 且
`accepted_merge_count=0`。旧 usage 的 71 个 global grouping 调用合计输出 1,674 个
Groups，说明每一批都在重新发明 Group decomposition，而不是只判断已有 Group 是否应合并。

早期 v1 地图 `km_bb58600230baded1` 的 204 Groups 不是本切片直接基线：它是 801 candidates
直接投影、无 TopicAspect 的旧 schema。086 的同类比较只使用 schema v4 的 c046／c7。

**过程**

1. 从 manifest 读取 Topic、Group、usage 和 `group_convergence`，确认初始 Group 数为 268。
2. 从 `vectors.npz` 读取 Group vectors，对所有 Group pair 计算 cosine 和 child Topic Jaccard。
3. 统计 membership、重复 Group 比例和最终 Group 数，不只看 Group 总数。
4. 把同一指标加入新 map 的 `group_overlap_audit` usage，保证每次以后构建都自动留证。

**基线结果**

| 指标 | `km_c046f7d55ceba425` |
| --- | ---: |
| Topics | 736 |
| initial Groups | 268 |
| final Groups | 490 |
| Group count delta | +222 |
| Topic membership | 2,377 |
| 平均 Topic membership | 3.23 |
| 多归属 Topics | 640 |
| 最大 membership | 9 |
| 至少一个 cosine ≥0.80 近邻的 Groups | 418 / 490（85.3%） |
| 至少一个 child Jaccard ≥0.50 近邻的 Groups | 340 / 490（69.4%） |
| 最大 cosine | 0.989215 |
| 最大 child Jaccard | 0.909091 |

**结论**

仅比较 net Group count 不足以判断收敛。旧实现即使把 268 重组为 490，也可以因为最后
一轮没有“净新增”而误报稳定。后续每个 pass 必须记录输入、输出、真实 merge actions、
membership 和 overlap，而不只记录最终数量。

### 阶段 2：merge-only 全局收敛

**实现**

1. 新增 `_GroupMergePayload` 和 `GROUP_MERGE_PROMPT`；模型只能输出 `merge`／`keep`
   及 `uncertain_pairs`。
2. `_validate_group_merge` 对每个 batch 强制完整消费：输入 Group 必须出现且只能出现
   一次；`keep` 只能有一个 Group；`merge` 至少两个 Groups；未知 ID、重复 ID、无
   canonical label／summary 都使本批结构化输出失败。
3. `_merge_groups_by_actions` 用并查集构造单调 cluster；merge 后的 child Topics 是输入
   union，source docs 由 Topics 重算，aliases 和 retrieval questions 去重。
4. 每轮只审核尚未审阅的全局候选边；Group 发生变化后生成新 ID，并重新进入候选图。
5. `GROUPING_PROMPT_VERSION` 升为 `profile-knowledge-map-grouping-v3`，
   `GROUPING_POLICY_VERSION` 升为 `profile-knowledge-map-merge-only-groups-v8`。

**硬不变量**

- 一个 batch 内每个输入 Group 恰好消费一次；
- 一次 pass 后 Group 数只能不增；
- 输出不能包含输入之外的 Group；
- merged Group 的 child Topics 必须等于输入 union；
- 不能再出现旧流程的 268→490。

**真实结果**

| Pass | input Groups | output Groups | membership | merged clusters | incomplete canonical | uncertain |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 245 | 207 | 736 → 736 | 21 | 4 | 298 |
| 2 | 207 | 205 | 736 → 736 | 2 | 0 | 138 |
| 3 | 205 | 202 | 736 → 736 | 1 | 0 | 21 |
| 4 | 202 | 202 | 736 → 736 | 0 | 0 | 20 |

最终 `accepted_merge_count=43`，`passes_used=4`，`termination_reason=fixed point reached`，
`status=converged_with_uncertain`。第 1 pass 的 4 个 incomplete canonical cluster 来自多个
合法 merge actions 的传递闭包；当前代码选择覆盖 cluster 的最大兼容 canonical action，并把
数量写入 usage，没有静默丢失。

### 阶段 3：高置信重复与多重归属约束

**实现**

1. LLM merge review 前增加 `_auto_merge_high_overlap_groups`：同时满足 Group cosine
   `>=0.95` 与 child Topic Jaccard `>=0.50` 的边做确定性合并。
2. 初始 grouping Prompt 默认每个 Topic 只进一个主 Group；额外归属必须说明不同检索职责。
3. 两个候选 Group 回答同一核心 retrieval question 时必须合并；Group label／summary
   必须覆盖全部主要对象和边界。
4. final map 记录 Group overlap、membership count 和 multi-membership count。

**真实结果**

| 指标 | 旧 c046 | build 1 | build 2 | build 3 |
| --- | ---: | ---: | ---: | ---: |
| final Groups | 490 | 202 | 250 | 206 |
| Topic membership | 2,377 | 736 | 742 | 740 |
| 平均 membership | 3.23 | 1.00 | 1.008 | 1.004 |
| 多归属 Topics | 640 | 0 | 6 | 3 |
| cosine ≥0.80 Groups | 418（85.3%） | 0 | 6（2.4%） | 0 |
| child Jaccard ≥0.50 Groups | 340（69.4%） | 0 | 0 | 0 |
| 最大 cosine | 0.989215 | 0.799252 | 0.824102 | 0.798555 |
| 最大 child Jaccard | 0.909091 | 0.0 | 0.166667 | 0.066667 |
| Cross-document Groups | 293 | 103 | 100 | 86 |
| 最大 Group size | 26 Topics | 26 Topics | 17 Topics | 36 Topics |

这次真实构建没有触发 0.95／0.50 的确定性预合并，因此该路径目前只有单元测试证据；43 个
净减少来自 LLM merge-only actions。最终没有多归属 Topic，说明当前 15 篇样本被收敛为
一个 Topic 分区；这修复了重复和 routing 歧义，但“允许多归属”尚未在自动触发样本中得到
真实回归证据。

Group 没有被明显合并成超大块：202 Groups 的 size 中位数 3、P75 4、最大 26；最大 Group
仍是“LLM 评估系统、方法、工具与基准”，与旧 map 的最大 Group size 相同，没有因为减少
Groups 而突破原来的最大粒度。

### 阶段 4：Routing 稳定性

**实现**

1. Group routing payload 增加 `group_score`、`candidate_rank`、`child_topic_count` 和
   `top_child_topics[].query_score`。
2. 每个 selected Group 必须返回 `supported_topic_ids`，且 Topic 必须属于该 Group；
   空列表或跨 Group Topic 会触发结构化重试。
3. `partial` 必须说明新增了哪个尚未覆盖的 Topic；reason 必须说明补足的检索问题。
4. diagnostics 保留 Group selection 的 relevance、supported Topics 和 reason。

**固定地图 5 次真实查询**

固定地图 `km_b07c05f74865252a`，固定 snapshot `s_6b787258ba7d700c`，固定 topic
“AI Agent 隔离与控制边界”。五次查询的候选 Group 集合均为 54 个。

| Run | selected Groups | Topics | Blocks | core | extension | precision | 无关 token |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 10 | 57 | 32 | 7/7 | 3/4 | 0.9062 | 9.95% |
| 2 | 12 | 65 | 36 | 7/7 | 3/4 | 0.8333 | 16.94% |
| 3 | 12 | 65 | 36 | 7/7 | 3/4 | 0.8333 | 16.94% |
| 4 | 11 | 62 | 33 | 7/7 | 3/4 | 0.8485 | 14.85% |
| 5 | 12 | 66 | 36 | 7/7 | 3/4 | 0.8611 | 13.71% |

五次全部通过冻结 c13 gate。两两 Jaccard：

| 对象 | 最低 | 最高 |
| --- | ---: | ---: |
| selected Groups | 0.6923 | 1.0 |
| expanded Topics | 0.7429 | 1.0 |
| returned Blocks | 0.7436 | 1.0 |

57 次 selected-Group 判断都给出了合法 `supported_topic_ids`；每次查询的支持 Topic 总数为
34～39，没有一个 Group 用空支持集通过 routing。

旧地图行为是同一查询选择 18／23 Groups、返回 38／52 Blocks；当前为 10～12 Groups、
32～36 Blocks。改进同时来自上游 Group 去重和 routing 的支持 Topic 约束。routing 仍不是
确定性的，因此验收采用“最差运行仍达标”而不是要求五次完全相同。

## 重复编译稳定性

固定 snapshot、Preview Run、Profile、代码版本和 topic，不激活后续试验地图。

| Build | map ID | initial | final | passes | merges | membership | multi-membership | max cosine | max Jaccard | c13 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | `km_b07c05f74865252a` | 245 | 202 | 4 | 43 | 736 | 0 | 0.799252 | 0.0 | 5/5 通过 |
| 2 | `km_b15b567c3693fc2b` | 287 | 250 | 4 | 37 | 742 | 6 | 0.824102 | 0.166667 | 5/5 通过 |
| 3 | `km_d99dc67a009a7f37` | 286 | 206 | 4 | 80 | 740 | 3 | 0.798555 | 0.066667 | 2/5 通过，阻断 |

三个 build 都以相同的 801 个 candidate 为身份锚点比较：

| Pair | Topic pair Jaccard | Group pair Jaccard |
| --- | ---: | ---: |
| Build 1 / 2 | 0.964706 | 0.296866 |
| Build 1 / 3 | 0.941176 | 0.223552 |
| Build 2 / 3 | 0.952381 | 0.207684 |

Topic identity 的 candidate-pair 一致度保持在 0.94～0.96，但 Group 只有
0.21～0.30。波动来自 Group proposal：三个 build 的初始 Groups 分别是 245、287、286。
merge-only 只能在这些初始 Groups 上做单调 union，不能重建被初始 proposal 拆开或遗漏的
检索区域。

### Build 2 固定地图 5 次

| Run | Groups | Topics | Blocks | core | extension | precision | 无关 token |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 17 | 49 | 33 | 7/7 | 3/4 | 0.8182 | 14.95% |
| 2 | 18 | 52 | 35 | 7/7 | 3/4 | 0.8286 | 14.38% |
| 3 | 16 | 47 | 31 | 7/7 | 3/4 | 0.8710 | 8.48% |
| 4 | 17 | 49 | 33 | 7/7 | 3/4 | 0.8182 | 14.95% |
| 5 | 18 | 52 | 35 | 7/7 | 3/4 | 0.8286 | 14.38% |

### Build 3 固定地图 5 次

| Run | Groups | Topics | Blocks | core | extension | precision | 无关 token | Gate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 14 | 50 | 29 | 6/7 | 3/4 | 0.7931 | 12.35% | 失败 |
| 2 | 16 | 55 | 32 | 6/7 | 3/4 | 0.7812 | 14.14% | 失败 |
| 3 | 17 | 60 | 33 | 7/7 | 3/4 | 0.8182 | 10.75% | 通过 |
| 4 | 16 | 56 | 31 | 7/7 | 3/4 | 0.8065 | 11.65% | 通过 |
| 5 | 16 | 53 | 31 | 6/7 | 3/4 | 0.7742 | 15.58% | 失败 |

Build 3 的三次失败都缺 `parallel_work_state`。地图候选集合稳定为 53 Groups，其中始终有
8 个 Group 包含该目标的 9 个 evidence blocks；失败运行没有选中这 8 个中的任何一个，
通过运行选中了 Claude 产品形态／宿主模式 Groups 并因此命中。问题由此定位到 Group routing
没有稳定覆盖一个跨机制边界的检索维度，而不是候选召回或原文回读。

### Routing 提示词实验

尝试在 Group routing Prompt 中加入“开放边界盘点必须覆盖不同边界维度”。同一 Build 3
地图连续 5 次结果反而只有 1/5 通过，并出现 38 blocks、precision 0.658、无关 token
29.46% 的过选样本。该 Prompt 已回退，不进入当前基线。这个实验说明继续增加措辞会同时带来
漏选和过选，不能作为稳定性修复。

## 编译可靠性修复

第三次原始 build 在 `topic_aspect_extension` 阶段失败两次：模型合法省略了一个没有新增
Aspect 的待复核 Topic，但 validator 要求 Topic 集合完全相等，因此整张地图被阻断。

修复不把 omission 当作新增内容，也不假装成功：

1. Aspect extension 允许返回 review Topics 的子集；
2. 未知 Topic、重复 Topic、未知 candidate 和非法 Kind 仍然失败；
3. 漏返的 Topic 规范化为 `aspects=[]`；
4. usage 明确写入 `omitted_review_topics`；
5. schema 允许 `{"topics":[]}`，语义是整批均无新增 Aspect。

新增集成测试覆盖“首次扩展漏一个 Topic 仍完成地图”和“空扩展 payload”；全量后端测试
448 项通过。修复后的第三次真实编译成功生成 `km_d99dc67a009a7f37`。

## 验收矩阵

| ID | 目标 | 指标 | 阻断条件 | 验证 | 当前结果 |
| --- | --- | --- | --- | --- | --- |
| A1 | merge-only 正确 | 新 Group=0；输入重复消费=0；split=0 | 任一失败 | 单元 | 通过；覆盖重复消费、漏项和 union |
| A2 | 收敛真实 | Group 数不增；churn／membership 完整记录 | 伪 converged | 单元／map | build 1 为 245→202→fixed point |
| A3 | 重叠下降 | high-overlap Group 比例、平均 membership | 无改善或 Group 过宽 | map | cosine≥0.80：85.3%→0／2.4%；membership：3.23→1.00／1.008 |
| A4 | 编译稳定 | 重复构建 Group 数、co-membership ARI；多归属时用 pair Jaccard | 大幅波动 | 3 次构建 | 失败；Group pair Jaccard 0.208～0.297 |
| A5 | 检索稳定 | selected Group／Block Jaccard、数量范围 | 最差运行超门禁 | 每张固定地图 5 次 | Build 1／2 通过；Build 3 仅 2/5，失败 |
| A6 | 质量不回退 | c13 核心、扩展、precision、tokens | 任一阻断失败 | 真实运行 | Build 3 三次缺 core，A6 失败 |

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 086-D1 | B | global merge 输出形式 | merge／keep／uncertain；不允许自由分组 | 13 §7.7 | 从根上阻止 Group 膨胀 | 单元／map | 待确认 |
| 086-D2 | B | 高置信重复阈值 | 同时要求 high cosine 与 high child Jaccard | 当前近重复分布 | 避免 summary 相似但内容不同的 Group 被误合并 | map | 待确认 |
| 086-D3 | B | 多重归属 | 默认唯一主 Group；额外 membership 必须说明独立检索职责 | 旧 map 640/736 Topics 多归属 | 降低 routing 歧义 | 单元／map | 待确认 |
| 086-D4 | B | Routing 稳定性 | GroupScore + 未覆盖 Topic 增益 + partial 缺口约束 | 同地图 18/23 Groups 波动 | 缩小选择方差 | 5 次查询 | 待确认 |

## 证据包

### 自动化证据

- 定向测试：`test_profile_knowledge_map.py` 25 passed。
- 全量测试：448 passed，退出码 0。
- 静态检查：Ruff format／check 通过；Pyright 在显式指定后端虚拟环境后 0 errors。
- `git diff --check` 通过。

### 运行证据

- 基线 map：`km_c046f7d55ceba425`
- 新 map build 1：`km_b07c05f74865252a`，736 Topics、202 Groups，active。
- Build 1 日志：`.runtime-086/km-15-mergeonly.out.log`
- Build 2：`.runtime-086/km-15-mergeonly-build2.out.log`、`km-15-mergeonly-build2.json`
- Build 2 gate：`.runtime-086/km-15-mergeonly-build2-gate.json`
- Build 3：`.runtime-086/km-15-mergeonly-build3.json`、`km-15-build3-query1..5*`
- Build 3 gate：`.runtime-086/km-15-build3-query1-gate.json` 至 `run5-gate.json`
- Routing 反例实验：`.runtime-086/km-15-build3-routingv2-run1..5*`
- Routing 5 次：
  `.runtime-086/km-15-stage4-run1.json` 至 `run5.json`
- Gate 5 次：
  `.runtime-086/km-15-stage4-run1-gate.json` 至 `run5-gate.json`

### 编译成本

| 指标 | c046 | build 1 | build 2 | build 3 |
| --- | ---: | ---: | ---: | ---: |
| 总 LLM usage records | 486 | 573 | 604 | 615 |
| input tokens | 2,771,751 | 2,957,599 | 3,034,988 | 3,238,353 |
| output tokens | 811,589 | 721,778 | 732,473 | 777,357 |

新流程 input token 比 c046 增加约 6.7%～16.8%，output token 在 Build 1／2 下降、在
Build 3 下降约 4.2%。这是 Group 去重、更多审阅 pass 和模型输出长度波动的成本；不能只看
token 平均值判断成功，仍需结合结构稳定性和检索质量。

### 已知反例账本

| 场景 | 期望 | 实际 | 是否阻断 | 结论 |
| --- | --- | --- | --- | --- |
| global pass 268→490 Groups | 不增加 | 已复现并记录 | 是 | 已由 merge-only 关闭 |
| 同地图 routing 18/23 Groups | 稳定且最差达标 | 旧行为已复现 | 是 | 新地图 5 次稳定通过 |
| 高相似 Group 仍进入 routing | 先合并或明确保留 | build 1 高相似审计为 0 | 否 | 当前样本关闭；需更多样本确认 |
| 5 个 pass 仍不收敛 | 标记 ready_partial | build 1 4 passes 到 fixed point | 否 | 当前样本通过 |
| Aspect 扩展省略无新增 Aspect 的 Topic | 规范化为空扩展并记录，不阻断地图 | 原实现两次重试后整图失败 | 是 | 已修复并加测试 |
| 同一代码重复编译的 Group 边界 | 结构应可重复且下游不退化 | 3 次 Group pair Jaccard 0.208～0.297 | 是 | A4 失败 |
| Build 3 丢失并行任务边界 | 五次最差运行仍覆盖 7/7 | 3/5 缺 `parallel_work_state` | 是 | A6 失败；Build 3 不激活 |
| Routing 增加“边界维度覆盖”措辞 | 减少漏选且噪声受控 | 仅 1/5 通过，最差 precision 0.658、噪声 29.46% | 是 | 已回退，不保留 |

## 反向解释

待实现和人工验收后填写。

## 已知风险与后续

- 三个 build 的第 1 pass 分别有 4／3／5 个 incomplete canonical clusters。当前选择最大
  兼容 canonical action，避免事务失败，但 label／summary 可能只完整覆盖 cluster 的主要
  子集；需要决定是否增加一次 cluster-level canonicalization。
- 0.95 cosine＋0.50 child Jaccard 的确定性预合并没有在 Build 1 真实触发；目前只有单元
  证明。需要在相似文章组中验证阈值，不只看当前样本是否“恰好不用它”。
- 确定性预合并的 canonical label／summary 由输入 label／summary 稳定拼接，不重新调用
  LLM。它保证可复现，但可能产生较短输入下不自然的组合名称；真实触发后需要人工检查。
- 三个 build 的 multi-membership Topics 为 0／6／3。Prompt 允许“不同检索职责”的额外
  归属，但 membership 理由仍是语义约束；尚无稳定正例证明它足以形成跨批次检索维度。
- `converged_with_uncertain` 的 477／500／557 是各 pass 累计 uncertain 关系，不是仍存在
  同等数量的高相似 Group。三个 build 的 overlap audit 都没有 child Jaccard ≥0.50 的组。
- 三次 Group partition 不稳定已经实测失败：Topic identity 稳定，但 merge-only 无法创建
  初始 proposal 中不存在的跨机制检索组。Build 3 的 `parallel_work_state` 证据分散在 8 个
  候选 Groups，LLM routing 在三次运行中一个都未选。
- Build 3 的最大 Group 为 36 Topics，高于 Build 1／2 的 26／17；当前也没有发现它的 child
  Jaccard 高重叠，但这可能是另一种“把不同边界并入大组”的过宽信号，需要纳入下一次审计。
- 下一项需要审核的设计是：在 merge-only 之后增加受控的“additive coverage group”或
  cross-cutting membership 阶段，只允许组合已有 Topics、保留原 Groups，并要求独立检索
  问题和可追溯 usage；不能恢复旧式自由重组，也不能只在 Routing Prompt 上继续堆措辞。
- 当前 c13 验收只覆盖一个开放主题和 15 篇文档；不能据此证明 50／100／1,000 篇下 Group
  数量、routing 延迟和 overlap audit 成本仍可控。
- Build 1 input token 比 c046 增加约 6.7%，主要是更多 pass 和更长的 merge review；需要
  在规模验证中区分固定样本偶然波动与结构性成本。
