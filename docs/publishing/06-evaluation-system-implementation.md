# Agent 评测台落地：为什么真正难的不是跑 10 次，而是证明这 10 次可比较

> **摘要**：Agent 评测最容易把执行失败、判官失败和产品失败混成一个分数。本文复盘执行现场、样本资格、判定绑定与 N/K 聚合的落地边界。

## 从“有评测页”到“有可信的实验事实”

> **30 秒判断**：一个 Agent 跑完 10 次并显示“90 分”，不代表它被正确评测了。系统还要证明这 10 次使用了同一快照、模型和 prompt，失败样本是否应计入 N，判官给的是哪一版结论，以及当前生效结果为什么可以覆盖旧结果。评测台的核心交付物不是分数，而是可比较、可审计的实验事实。
>
> **适合谁读**：正在给 LLM／Agent 做回归评测的人；已经有一个“跑用例、看通过率”的脚本，但开始遇到版本漂移、上下游失败、判官抖动或历史结果无法复现的开发者。
>
> **系列位置**：本文属于[《学习问答助手Agent实践》](https://juejin.cn/column/7697101748126007305)的实现复盘部分。设计侧已经有[《Agentic RAG 的评测台：从跑分页到可诊断、可回归的系统》](https://juejin.cn/post/7698452217793347611)，本文不复述评测对象与方法，而是记录它在实现中怎样变成运行事实、样本资格和聚合规则。
>
> **代码库**：[ai-study-helper-v2](https://github.com/lipeidonggz/ai-study-helper-v2)
>
> **结构速览**：§0 分数为什么不是事实；§1 四层评测现场；§2 不可变性要落到数据库；§3 执行与判定分离；§4 失败到底算不算样本；§5 N/K 与红线；§6 人工改判为什么只能追加；§7 哪些设计在落地时被修正；§8 一页速查。
>
> **证据标记**：✅ 项目实测＝真实评测运行；🧪 可回归＝自动化测试固定行为；🤔 当前判断＝由当前实现推导出的结论。

---

## 0. 一次运行会留下四层事实和一层投影

普通脚本通常只保存一张表：

```text
case_id | 是否通过 | 分数
```

但这张表回答不了：

- 这次回答来自哪个 collection 快照？
- 用的是哪一版 prompt、模型和 Agent Loop？
- 这是一次有效产品样本，还是上游超时？
- 某条 criterion 为什么通过？
- 人工改判后，原判官结论还在不在？

实现最终把评测拆成四层：

```text
ExecutionPlan
  └─ ExecutionRun
       └─ Attempt × N
            └─ Judgment × criterion
                 └─ Effective Binding

两级聚合：
Attempt Aggregate
Case Aggregate
```

其中一层只回答一个问题：

| 层 | 回答的问题 |
| --- | --- |
| ExecutionPlan | 这次准备比较什么，整个实验现场是什么？ |
| Attempt | 某一次 Agent 执行实际发生了什么？ |
| Judgment | 某个 answer 是否满足某一条 criterion？ |
| Effective Binding | 当前聚合应该采用哪一条 Judgment？ |
| Aggregate | 一次 attempt 和一个 case 最终是什么结果？ |

聚合不是事实源，而是从前面这些记录重新计算出来的投影。

---

## 1. ExecutionPlan 冻结的是整个实验现场

只冻结“问题”和“用例 ID”远远不够。模型和 prompt 改变、知识库换快照、预算或并发改变，都会影响结果。

实现中的计划对象至少固定：

```python
class ExecutionPlanView(BaseModel):
    suite_revision_id: str | None
    collection_snapshot_id: str
    run_type: RunType
    mode: Literal["formal", "exploratory"]
    concurrency: int
    agent_version_set: dict[str, Any]
    model_configuration: dict[str, Any]
    run_policy: dict[str, Any]
    experiment_variable: dict[str, Any]
    checks: list[PreflightCheck]
    cases: list[PlanCaseView]
```

`agent_version_set` 里继续记录 prompt bundle、Agent Loop、工具契约、token 估算器和 embedding profile；`run_policy` 记录预算、超时、替代样本上限和 trace 模式。

这带来一个很朴素但重要的约束：

> 只要影响结果的因素变化，就应该产生新的 plan／run，而不是在旧 run 里继续追加样本。

否则同一个 run 中会同时存在两套模型、两版 prompt 或两张知识库快照。最终分数仍然可以算出来，但它已经不再对应一个可解释的实验。

源码入口：[ExecutionPlanView](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/evaluation/models.py#L318)、[计划冻结现场](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/evaluation/planner.py#L123)。

---

## 2. “不可变”不能只靠代码自觉

设计里很容易写“revision 不可修改”，实现时如果只是上层 service 不提供修改接口，一条维护脚本或未来新增的 API 仍可能原地改写内容。

因此不可变性落到了 SQLite trigger 上：

```sql
CREATE TRIGGER IF NOT EXISTS eval_criterion_revision_content_immutable
BEFORE UPDATE OF
    criterion_id, revision_number, rule_type, ordinal, title,
    rule_text, evaluator, evaluator_ref, source_ref, content_hash,
    change_note
ON eval_criterion_revisions
BEGIN
    SELECT RAISE(ABORT, 'criterion revision content is immutable');
END
```

case revision、criterion revision、suite revision 和 ExecutionPlan 都采用同类约束。内容变化只能生成新 revision；旧 run 继续引用旧 ID。

这比“数据库里再放一个 updated_at”有意义得多：

```text
可以新建版本
可以停用版本
不能把历史现场改成另一套内容
```

草稿仍然可以反复编辑，但草稿不是执行事实。只有审核后生成的不可变 revision 才能进入正式 suite。

源码入口：[SQLite 不可变性 trigger](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/evaluation/database.py#L91)、[ADR 0010 版本工作流](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/docs/decisions/0010-evaluation-case-and-suite-revision-workflow.md#L1)。

---

## 3. 执行与判定必须是两条状态链

设计文已经提出“重跑”和“重判”不是一回事，实现在这里继续拆成两个独立对象：

```text
ExecutionRun
  执行 Agent，产生 answer / trace / usage

EvaluationRevision
  读取已封存执行结果，产生 Judgment / CheckResult / Aggregate
```

这带来几种必须区分的操作：

| 操作 | 是否重新调用 Agent | 是否保留已有 trace |
| --- | --- | --- |
| 重跑 | 是 | 否，创建新 attempt |
| 重判 | 否 | 是，从原 trace 读取 |
| 人工改判 | 否 | 是，原 Judgment 仍保留 |
| 修复 checker 后重判 | 否 | 是，checker 版本变化 |

如果这两条链共用“retry”，系统很容易把“模型重新回答了一次”和“用更好的判据重新看了一遍原答案”混在一起。

更严重的是，重新执行会产生一个新 Answer。此时即使判定通过，也不能证明原始失败回答已经通过。反过来，重判失败也不能说明 Agent 本身变差。

评测台必须让用户从 attempt 回到原始 answer、工具 trace 和判官输入，而不是只展示一个汇总分数。

> 🤔 当前判断：可诊断性不是评测台的附加功能。没有原始现场，就无法判断一次回归到底来自 Agent、Judge、checker 还是基础设施。

---

## 4. `failed` 不是一个足够好的样本分类

实现过程中最重要的修正之一，是不能把所有 `execution_status=failed` 都当成基础设施失败。

例如：

- 模型超时：基础设施问题，不应算 Agent 答错；
- 预算耗尽：产品行为结果，必须进入判官；
- 模型协议错误：产品行为结果，必须进入判官；
- 答案为空：产品行为结果，仍要以空 answer 进入判官；
- 进程中断、结果未知：不能自动重发，也不能直接判成产品失败。

最终分类逻辑明确区分四种 outcome：

```python
AttemptOutcomeClass = Literal[
    "product",
    "infrastructure",
    "canceled",
    "unknown",
]

def classify_attempt_outcome(execution_status, error_code):
    if execution_status in {"succeeded", "partial"}:
        return "product"
    if execution_status == "canceled":
        return "canceled"
    if execution_status != "failed":
        return "unknown"
    if error_code in INFRASTRUCTURE_FAILURE_CODES:
        return "infrastructure"
    if error_code and error_code.startswith(("chat_", "embedding_")):
        return "infrastructure"
    return "product"
```

最后一行很重要：**未识别的失败默认归为产品结果**。

这样宁可让未知问题进入判官并留下失败样本，也不让系统通过“未知错误 → 自动替代 → 样本消失”制造虚假通过。

只有 `product` 样本计入有效 N 并进入全部 criterion 判定。基础设施失败可以按显式预算创建替代 attempt；`outcome_unknown` 不能自动重发，因为崩溃前可能已经产生过调用和结果。

源码与测试入口：[outcomes.py](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/evaluation/outcomes.py#L20)、[分类测试](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/tests/test_evaluation_core.py#L631)、[基础设施替代测试](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/tests/test_evaluation_core.py#L672)。

---

## 5. N/K 不是“大多数通过”

第一版聚合如果只写“通过率达到 90% 就 pass”，会丢掉两种重要语义。

第一种是 X 红线。X 不是普通加分项，而是产品定义的不可接受边界：

```text
10 次里 9 次正常，第 10 次泄露了用户文件
```

这不能因为 90% 通过就叫成功。

第二种是 pending。判官上游失败、证据不足或人工复核尚未完成，都不等于答案失败。直接把 pending 算 fail，会把 Judge 的问题栽给 Agent；直接把 pending 忽略，又可能制造虚假通过。

实现中的 case 聚合顺序是：

```python
if plan_case.n < plan_case.k:
    result = "exploratory"
elif redline_violation_count:
    result = "fail"
elif len(valid) < plan_case.n:
    result = "pending"
elif pass_count >= plan_case.k:
    result = "pass"
elif pass_count + pending_count >= plan_case.k:
    result = "pending"
else:
    result = "fail"
```

它表达的是：

- N＜K 只能叫探索性结果；
- 任意红线违规一票 fail；
- 有效样本不足为 pending；
- 已经达到 K 才是 pass；
- 当前还不够 K，但人工复核后仍可能达到 K，也是 pending；
- 即使把所有 pending 都改判为 pass 也达不到 K，才是 fail。

这不只是“算分算法”，而是定义了失败、待定和产品边界之间的优先级。

源码入口：[case N/K 聚合](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/evaluation/aggregation.py#L143)。

### 真实 N=10 运行暴露的问题

正式运行最初并没有直接产出合格结果：

- `run_ca42b6b380ce43daac2ecc6ee0dbe088` 达到 N 后继续超发 attempt，封存时把取消样本误记为基础设施失败；
- `run_21e50c302aa84068afc46ddf96589046` 又暴露“没有缺口但仍有运行中样本时提前封存”。

它们不是模型质量问题，而是实验样本完整性问题。修复后，`run_861667d15e764d84b9484dff51cd162e` 恰好产生 10 个有效 product 样本，没有替代、取消或基础设施失败。

这两个并发错误本身算不上高深，但它们证明了一件事：

> 只要调度器能改变样本数量，“N=10”就不再是一个可靠的实验参数。

证据入口：[023B 运行证据](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/docs/development/slices/023B-evaluation-aggregation.md#L130)。

---

## 6. 人工改判为什么不能覆盖原判官

判官本身也会抖动。即使模型温度设为 0，同一条 criterion 仍可能因服务端实现、上下文或模型更新出现不同判断。

所以人工纠错不是直接改总分，也不是覆盖原 Judgment，而是：

```python
if criterion.evaluator != "llm_judge":
    raise ValueError("only llm_judge criteria can be manually overridden")

session.add(EvalJudgmentRow(
    executor="human",
    verdict=request.verdict,
    reason=request.reason,
    evidence_json=_json({"reviewer": request.reviewer}),
))
session.add(EvalJudgmentBindingRow(
    judgment_id=judgment_id,
    ...
))
```

注意这里同时保留了三件事：

1. 原 LLM Judgment；
2. 新 human Judgment；
3. 当前生效的 binding。

所以同一 `attempt + criterion` 可以有多条 Judgment，但当前聚合只跟随最新的显式 binding。

人工权限也被限制：

- 可以改 LLM 的语义判定；
- 可以逐 block 复核 LLM 的引用支持判断；
- 不能改 checker 的确定性 Judgment；
- 不能改 `citation_integrity` 这类程序检查。

原因很直接：如果人工可以直接覆盖 checker，那么“引用 block 不存在”这种确定性事实就变成了主观意见。

源码入口：[人工追加 Judgment 与 Binding](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/backend/app/evaluation/judgment.py#L455)。

---

## 7. 哪些设计在落地时被修正

| 设计／初始实现 | 落地修正 |
| --- | --- |
| 评测台是独立 Tauri 应用 | 改为单开发壳，通过编译期 feature 启用评测；数据根和 sidecar 仍独立 |
| `failed` 统一视为无效 attempt | 只有明确基础设施失败不计 N；产品失败全部进入判官 |
| 判定结果直接覆盖或取最后一次 | Judgment append-only，通过 binding 指定当前生效版本 |
| 人工纠错修改汇总结果 | 只追加人工语义判定，再重新计算 attempt／case aggregate |
| 不可变性依赖业务代码 | SQL trigger 直接禁止历史 revision 修改和删除 |
| 快速试跑必须是正式 suite | reviewed／active case 可直接生成 exploratory plan |

部署形态的调整记录在 [ADR 0009](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/docs/decisions/0009-development-shell-with-compiled-evaluation.md#L1)，用例版本工作流记录在 [ADR 0010](https://github.com/lipeidonggz/ai-study-helper-v2/blob/3c3a3ec76cbaa17a7c9e24491d7396f98d6b1923/docs/decisions/0010-evaluation-case-and-suite-revision-workflow.md#L1)。

### 当前完成到哪里

到 023B 为止，系统已经完成：

- 用例、suite、plan、run 和 attempt 的冻结与追溯；
- checker、主判官、引用检查和 append-only Judgment；
- attempt 与 case 两级聚合；
- N/K、pending、exploratory、X 红线；
- 人工改判和逐 block 引用复核。

结果矩阵、跨版本基线比较和正式诊断建议仍在后续切片中。这篇文章不把“设计已定义”写成“实现已完成”。

在正式 c1 运行中，`run_5cb3608c35a74c40b2199f21fc491ccf` 达到 N=10、K=9，30 条生效 Judgment 全部 pass，引用完整性和引用支持检查也全部 pass。它证明当前聚合链可以闭环，但只有后续基线比较才能回答“这次 Agent 比上一版好多少”。

---

## 8. 一页速查

**评测结果不是一行分数，而是五类事实的组合**：

```text
Plan：实验现场
Run：一次评测任务
Attempt：一次真实 Agent 执行
Judgment：一条 criterion 的一次判定
Binding：当前采用哪一条判定
```

**四条关键规则**：

- 影响结果的因素变化时，创建新 plan／run，不改旧现场；
- 只有基础设施失败不计入 N，产品失败必须进入判官；
- X 红线一票 fail，不能被 N/K 平均；
- 人工改判追加新事实，不能覆盖原判官记录。

**最重要的判断**：

> 评测台首先是一个可追溯的实验系统，其次才是一个显示通过率的产品。
