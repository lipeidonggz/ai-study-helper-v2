# 2026-10-31 · Facet contract、审计与稳定性实验

- 状态：进行中
- 工作分支：`feat/065-corpus-grounded-coverage`
- 关联切片：[065 语料锚定主题覆盖](../slices/065-corpus-grounded-topic-coverage.md)
- 固定快照：`s_6b787258ba7d700c`
- 固定主题：`AI Agent 隔离与控制边界`
- 已知探针：
  - worktree：`doc_983ba4e2cbff4185873d525a69195fdf#v0001#c1#b00025`
  - checkpoint resume：`doc_7f36f426b84f40eb9439acaad4949e32#v0001#c1#b00016`
  - checkpoint evaluation：`doc_7f36f426b84f40eb9439acaad4949e32#v0001#c1#b00022`

## 目的

按 065 的问题清单逐项验证：

- Q-02：facet contract 是否足以指导查询和 reranker；
- Q-04：seed 全文之外，补充文档章节大纲是否有信息增益；
- Q-03：独立 facet audit 是否能发现缺项或过宽 facet；
- Q-01：同一 topic、同一快照下，覆盖结果是否稳定；
- Q-05：每 facet Top-10 是否不足，Top-20 的收益与成本如何。

## 提示词污染审查

本轮中途发现，部分实验提示词是从当前 c13-003 的缺失分组反推出来的，不能
作为泛化效果证据：

- 最初 planner 规则含“限制、隔离、恢复”的示例关系词，可能形成方向性偏置；
- facet audit v2 含“生命周期、检查点、回滚、并行任务、工作区、测试、评估”
  等检查镜头，直接对应当前用例缺口；
- 三个 planner 视角的提案同样来自当前缺口，因此不实施。

因此，受污染提示词产生的增量只能证明“给定规则后模型会执行”，不能证明
“模型能独立发现缺项”。Q-01／Q-04 结果保留为工程探针，不作为跨主题泛化
结论；后续验证必须使用 held-out 主题。

清理结果：

- planner 关系改为内容中性的“限制、机制、状态、因果或作用关系”；
- audit v3 删除固定领域镜头，只检查 topic、解释、seed 和文档结构内部信息；
- strategy 升级为 `corpus-grounded-coverage-v9`。

### 干净提示词的 held-out 验证

使用两个与 c13-003 无关的主题运行当前完整覆盖链路：

| 主题 | facets | 查询 | 候选 | shortlist | 返回 | audit 变化 | 输入 tokens | 输出 tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Agent 工具使用与函数调用设计 | 18 | 135 | 401 | 172 | 144 | 0 | 215,248 | 35,164 |
| LLM 应用评测的数据集、判据与判官设计 | 21 | 137 | 381 | 179 | 125 | 4 | 225,244 | 38,325 |

第二个主题中 audit 独立新增：

- 公开基准与排行榜的适用性与实现敏感性；
- 评测数据查看与标注工具设计；
- LLM 辅助评测工作流的边界；
- 评测器与护栏的区分及生产部署。

这些新增均由 seed evidence 的引用支持，说明 audit 机制在没有 c13 专属镜头时
仍能工作。两个主题均未出现 c13 的目标块 id；唯一出现“沙箱”字样的 Agent
工具主题，其依据也来自该主题自身 seed evidence，属于正常语料内容。

产出的直接问题：

- Top-20 下两个 held-out 主题分别返回 144 和 125 块，说明固定 Top-20 在宽主题
  上仍会输出过大的证据集；
- 审计并非每次都提出变化；
- 当前只验证了结构有效性，尚未为 held-out 主题建立 block 级 qrels。

结果文件：`backend/.runtime/clean-facet-held-out-topics.json`。

### 干净 v10 的 c13 回归

当前干净提示词在 c13-003 上完成一次 N=1 回归：

- run：`run_f6be4dbdef9f46f98babc06fa631b6cc`
- trace：`ffd8735ef42557c94ac616c4bbc52491`
- judgment：`evalrev_376e98dbe03c4f31b555a28d79c03198`
- initial facets：17；
- audit 后 facets：19；
- audit changes：2；
- 查询：151；
- 候选：431；
- shortlist：155；
- 返回：76；
- result tokens：35,232；
- `admitted=true`；
- trace：complete，0 dropped；
- LLM spans：25；
- query spans：152；
- artifacts：57。

目标块：

- worktree：返回；
- checkpoint resume：返回；
- checkpoint evaluation：未返回。

固定 plan 重放中，checkpoint evaluation 进入了短名单，但 reranker 没有给出支持
判断，首个缺失阶段为 `reranker_support`。

Judge：

- R2：pass；
- X2：pass；
- R1：fail，只缺第⑥组上下文与记忆生命周期边界；
- 第⑤组并行任务与工作状态边界已被答案识别。

这说明干净提示词下仍可以得到 worktree 与 checkpoint resume，但单次样本仍
不能证明稳定性；Q-01 仍需同版本 N≥5。

## Q-02：结构化 facet contract

### 变更

`GroundedFacet` 在原有 `name／scope` 之外增加：

- `evidence_obligation`
- `coverage_question`
- `required_relation`

facet planner 必须为每个 facet 填写这三个字段。reranker 同时收到 facet
catalog 和候选 provenance facet 的完整 contract，并按证据义务和必要关系判断，
不再只依据名称和简短 scope。

同时增加以下校验：

- facet 名称不得重复；
- `seed_refs` 必须引用当前 seed evidence 中存在的 id；
- `corpus` 和 `both` facet 必须至少引用一个 seed block；
- 审计前和审计后的 facet plan 都执行同一校验。

### 结果

真实 v4/v5/v6 运行中，模型均能稳定返回新字段。典型 evidence obligation
能够表达“机制 → 限制对象 → 必要关系”，例如：

```text
必须找到长运行 Agent 错误累积的具体机制、从错误点恢复而非重启的做法、
重试逻辑和定期检查点等确定性保障。
```

reranker 能据此把 checkpoint resume 判为 `direct`，而不是只得到宽泛的
名称相似度。

### 当前结论

Q-02 的结构化 contract 已实现并通过真实模型验证。仍需在 Q-01 多轮实验中
继续观察它是否降低 facet 漂移。

## 主题解释步骤的证据审查

当前主链路的 explanation 是设计假设，不是已经被受控实验证明的必要阶段。

设计目的：

- 把简短 topic 展开为主题语义空间；
- 为 facet planner 提供比关键词更完整的机制、关系和上下文；
- 在真实文档表达不足时补充 hypothesis。

已有证据：

- explanation-only 的 17 条 dense 查询只召回 0～1/3 个已知目标；
- explanation-only 逐名词 FTS 能召回 3/3，但需要 346 条查询和 511/526 个候选；
- corpus-grounded planner 同时使用 explanation 和 seed evidence，无法把
  3/3 的结果单独归因于 explanation。

缺少的关键实验：

```text
A：topic + seed + outline → facet planner
B：topic + explanation + seed + outline → facet planner
```

除 explanation 外保持 audit、query、Top-K、reranker 和 prompt 版本一致，
在相同 snapshot 上重复 N≥5，并比较 facet plan、目标阶段覆盖与成本。

在完成该消融前，不能宣称主题解释步骤已经带来稳定收益。

## Q-04：补充 seed 文档章节大纲

### 假设

单个 topic hybrid Top-50 可能只命中文档的某个章节。即使 block 全文完整，
同一文档中结构相关但未命中的章节仍不可见。

### 变更

对 seed block 所属文档调用固定快照的 `get_outline`，向 facet planner 增加：

```json
{
  "seed_documents": [
    {
      "doc_id": "...",
      "doc_title": "...",
      "sections": [
        {
          "path": ["..."],
          "title": "...",
          "depth": 1,
          "own_blocks": 4,
          "subtree_blocks": 12,
          "first_line": "..."
        }
      ]
    }
  ]
}
```

block 正文仍是证据，章节大纲只负责补充文档结构视野。未知文档或大纲读取失败
会显式返回 `coverage_seed_outline_failed`。

### 当前数据

v4 首次回放中：

- seed 文档：12；
- 补充章节：245；
- facets：17；
- 查询：153；
- 候选：447；
- shortlist：95；
- 返回：60；
- `worktree` 和 `checkpoint resume` 返回；
- `checkpoint evaluation` 未返回。

说明章节大纲可以让同一文档其他章节进入规划视野，但仍不能保证目标章节
每次都形成明确 facet。

## Q-03：facet 缺项审计

### v1 审计

初版审计输入：

- topic；
- explanation；
- seed evidence；
- seed document outlines；
- initial facets。

输出审计后的完整 facets，以及 `add／split／merge／rewrite／remove`
变化记录。v1 在真实运行中得到：

```text
initial facets = 19
final facets   = 19
changes        = 0
```

同时，N=5 批次中的 4 个正常完成工具调用的样本也全部为 `changes=0`。
因此“有审计阶段”不等于“审计能发现缺项”。

### v2 审计

v2 增加一组通用审计镜头，但不把这些镜头直接当作 facet：

- 可访问对象与范围；
- 动作、工具、能力与权限；
- 数据、状态、记忆的保存／读取／清空／跨边界复用；
- 生命周期、失败、恢复、检查点、回滚、可逆性；
- 多 Agent、多会话、并行任务的工作区和任务状态；
- 人类监督、治理、审计；
- 测试、评估、红队、可观测性；
- 隔离或控制失效、旁路、信任升级和风险权衡。

审计仅能在 topic、explanation 或输入语料有依据时新增 facet。

### v2 结果

注意：v2 审计规则包含由 c13-003 缺口反推的固定镜头，以下数据是受污染探针，
不能作为泛化能力证明。

真实 v6 运行：

- initial facets：20；
- final facets：22；
- audit changes：2；
- 查询：152；
- 候选：458；
- shortlist：113；
- 返回：74；
- tool duration：94.0 秒；
- tool model calls：18。

新增 facet：

1. 沙箱化测试与红队评估；
2. 用户作为注入向量与信任对话框前风险。

固定 plan 重放中：

- `checkpoint resume`：direct 并返回；
- `checkpoint evaluation`：partial 并返回；
- `worktree`：仍停在 facet shortlist，最相关排名 20/73。

审计已能修正评估类缺项，但还不能稳定发现“并行会话 → 独立 checkout →
工作区冲突”这条机制链。

## Q-01：N=5 稳定性

### 配置

- strategy：`corpus-grounded-coverage-v5`
- facet contract：v3
- seed outline：启用
- 独立 facet audit：v1 审计镜头
- 每 facet Top-10
- topic-only 输入
- `n=5`，5 个样本并行执行

### 结果

| 样本 | facets | 查询 | 候选 | shortlist | 返回 | worktree | resume | evaluation |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |
| 1 | 20 | 120 | 392 | 76 | 44 | 否 | 是 | 否 |
| 2 | tool error | - | - | - | 0 | 否 | 否 | 否 |
| 3 | 22 | 132 | 371 | 81 | 53 | 是 | 是 | 否 |
| 4 | 24 | 166 | 425 | 99 | 61 | 否 | 是 | 否 |
| 5 | 19 | 114 | 342 | 79 | 48 | 否 | 是 | 否 |

汇总：

- 目标返回率：
  - worktree：1/5；
  - checkpoint resume：4/5；
  - checkpoint evaluation：0/5；
- 三目标同时返回：0/5；
- R1 pass：0/5；
- X2 pass：3/5；
- 2 个样本出现排他性全集断言；
- 1 个样本的 reranker 返回未知或重复候选 id，工具以
  `coverage_rerank_invalid` fail-closed，主 Agent 随后改走普通检索。

R1 的主要缺口仍集中在：

- 并行任务与工作状态边界；
- 上下文与记忆生命周期边界。

### 当前结论

当前 v5 不能称为稳定。即使 facet contract 和大纲已改善单次质量，
facet plan 数量、查询数、目标召回和答案边界仍有明显波动。

## Q-05：每 facet Top-10 vs Top-20

### 实验方法

固定 v6 的 22 个 facet，只把每 facet shortlist 从 10 改为 20，再执行同一
query 集合和 reranker。

| 指标 | Top-10 | Top-20 |
| --- | ---: | ---: |
| 查询 | 152 | 152 |
| 候选 | 458 | 458 |
| shortlist | 113 | 167 |
| reranker batch | 15 | 21 |
| reranker 支持块 | 74 | 102 |
| worktree | 未返回 | 返回 |
| checkpoint resume | 返回 | 返回 |
| checkpoint evaluation | 返回 | 返回 |
| reranker 输入 tokens | 149,175 | 214,491 |
| reranker 输出 tokens | 20,789 | 25,703 |

Top-20 能把三个已知目标全部带回，但会增加：

- 54 个 shortlist 候选；
- 6 个 reranker batch；
- 约 6.5 万 reranker 输入 tokens；
- 28 个最终返回块。

这验证了 Top-10 的裁剪确实会丢失边界目标，但 Top-20 的额外成本也很大。
沛东确认：先将 Top-20 作为覆盖正确性的临时基线，同时继续设计动态预算；
固定 Top-20 不是最终方案。

v8 单次正式路径验证：

- facets：20；
- audit changes：0；
- 查询：120；
- 候选：375；
- shortlist：153；
- 返回：58；
- result tokens：28,249；
- worktree 和 checkpoint resume 返回；
- checkpoint evaluation 未返回；
- `admitted=true`。

## Q-08：内部阶段 trace

### 变更

`search_topic` 的工具调用现在会记录以下子 span：

- seed hybrid retrieval；
- 每条 dense／keyword facet query；
- explanation LLM；
- initial facet planning LLM；
- facet audit LLM；
- 每个 reranker batch。

LLM span 记录 provider、model、temperature、调用用途、prompt version/hash、
prompt/output artifact、token 和 request id。查询 span 记录 query、mode、
facet 归属、返回数和 fused candidate 数。

### 验证

真实 trace `f267e7a0df961674f88246d180da76bf`：

- trace status：complete；
- dropped events：0；
- spans：145；
- LLM spans：19；
- facet query spans：114，另有 seed query span 1；
- tool calls：4；
- artifacts：51；
- total duration：101.4 秒。

该次 `search_topic` 因 reranker 返回字段不完整而 fail-closed，但 trace 已能
准确定位失败阶段和输出 artifact，达到了 Q-08 的可诊断目标。

v8 再次验证：

- trace status：complete；
- dropped events：0；
- LLM spans：25；
- query spans：121；
- artifacts：57；
- 工具耗时：89.6 秒；
- 无 reranker 协议错误，20 个 reranker batch 正常完成。

## Q-10：reranker 协议失败重试

### 现象

N=5 中有 1 个样本返回未知或重复 candidate id；Q-08 验证运行中又出现
judgment 缺少 `facet／relevance／evidence` 字段。两者都属于结构化输出协议
失败，而不是检索失败。

### 变更

每个 reranker batch 增加一次重试：

- 第一次输出无法解析或 candidate id 非法时重试一次；
- 第二次仍失败则返回 `coverage_rerank_invalid`，保持 fail-closed；
- 两次无效输出不会降级为空结果或普通检索结果；
- trace 中会看到独立的两次 reranker LLM span。

### 验证

- 单元测试覆盖“第一次协议非法、第二次合法”的恢复路径；
- strategy version 更新为 `corpus-grounded-coverage-v7`，Top-20 临时基线为 v8；
- v8 单次真实回放中 20 个 reranker batch 均一次成功；
- 判断稳定性仍需后续 N≥3 重放验证，Q-10 尚未完成。

## Q-09：全量测试隔离

全量测试先用系统临时目录但受开发沙箱 ACL 限制，出现 fixture setup 权限错误；
改用 workspace 内长 basetemp 后，trace artifact 文件路径接近 Windows 长度
边界，产生 `FileNotFoundError` 和后台入库 S6 超时。

最终使用默认系统临时目录，在沙箱外执行：

```text
corepack pnpm backend:test
364 passed in 113.42s
```

代表失败用例在隔离执行时也能通过。当前结论：

- 不是 `search_topic` 业务回归；
- 本地全量验收应使用默认系统临时目录或足够短的 basetemp；
- CI 应使用正常可写的短临时目录；
- 该问题通过复现条件闭环，不修改业务代码。

## 已决策与待决策

### D-Q05：每 facet 候选预算（临时已确认）

候选方案：

1. 保持 Top-10：成本最低，但已证明会丢目标；
2. 全部改为 Top-20：目标覆盖改善，成本明显增加；
3. 分档 Top-K：按 facet 候选数、机制密度或查询支持动态分配；
4. 查询级并集：先保留每条查询 Top-10，再按 facet 覆盖选择，避免单条分数
   挤压；
5. 两阶段 rerank：先用轻量判断扩展候选，再对高价值候选做完整契约判断。

该决策影响成本、召回和最终上下文规模，需要沛东确认方向后再实现。

当前决定：

- v8 将每 facet shortlist 设为 Top-20；
- trace diagnostics 记录 `facet_shortlist_limit=20`；
- 该配置只作为覆盖正确性的临时基线；
- 动态预算仍作为后续实验，不把固定 Top-20 视为最终算法。

### 待决策：Q-04/Q-03 facet 稳定性目标

N=5 表明当前 facet contract、seed 大纲和审计仍未达到稳定覆盖：

- worktree 返回 1/5；
- checkpoint resume 返回 4/5；
- checkpoint evaluation 返回 0/5；
- 三目标同时返回 0/5。

可以继续探索的更强制、但与当前用例解耦的方案包括：

1. 同一个内容中性提示词的多个独立 planner 结果取并集；
2. facet audit 对输入 facet 与 seed 文档章节做通用覆盖映射；
3. facet gap 探针：对候选缺口先检索少量证据，再决定是否形成 facet。

缓存不列入稳定性方案。它只能降低同 topic、同 snapshot 的重复调用成本和
等待时间；一致性只是复用带来的结果，不能修复首次规划覆盖，反而可能冻结
错误 facet。缓存应在覆盖质量稳定后再作为性能优化处理。

上述结果必须在 c13-003 之外的 held-out 主题上验证。不得再根据 c13 缺失组
直接设计 planner 视角或审计镜头。

这些方案会增加模型调用和延迟，是否需要继续按“覆盖优先”路线推进，需要
沛东在产品成本边界上确认。

## 复现产物

- Q-02/Q-04 v4 结果：
  `backend/.runtime/c13-003-q02-q04-n1.json`
- Q-03 v1 审计结果：
  `backend/.runtime/c13-003-q03-audit-n1.json`
- Q-01 N=5：
  `backend/.runtime/c13-003-q01-v5-n5.json`
- Q-03 v2 / v6 结果：
  `backend/.runtime/c13-003-q03-audit-v2-n1.json`
- Q-05 Top-20 对照：
  `backend/.runtime/c13-003-q05-v6-top20-stage.json`
