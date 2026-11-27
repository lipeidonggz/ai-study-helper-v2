# FrameNet 框架词元对 C15 的来源流转覆盖实验

## 文档定位

本文验证 FrameNet 暴露的不同情境和谓词，能否作为 C15“来源、去向与流转”的观察轴证据来源。
实验只检查 C15，不改正式抽取提示词、关系轴目录或产品实现。本文只保留最新版本；原始输出保存在
被忽略的运行目录中，不复制模型全文。

## 固定条件

- 模型：`deepseek-flash`
- temperature：0
- thinking：disabled
- 每例运行：5 次
- 正例：8 个，覆盖 `Giving`、`Transfer`、`Sending`、`Communication`、`Receiving`
- 负例：6 个，其中 `Using`、`Creating`、`Locative_relation`、`Possession` 是官方框架，
  `Success`、`Constraint` 是边界描述；后两者不是本次审查时的 FrameNet 框架名
- 提示词版本：`framenet-c15-lexical-v1`
- 指标版本：`strict-and-normalized-participants-v1`
- 执行入口：[wiki_framenet_c15_lexical_probe.py](../../../scripts/wiki_framenet_c15_lexical_probe.py)
- 原始结果：`backend/.runtime/framenet_c15_lexical_latest/results.json`

实验同时保留两套指标：

1. **严格匹配**：参与对象必须与人工预期逐字相同，包含冠词、大小写和全部修饰语；
2. **规范化匹配**：只忽略句首冠词差异（`the`、`a`、`an`）、大小写和空白，其他词面必须相同。

第二套指标只用于区分“模型找对了对象和关系拓扑”与“模型没有逐字返回端点”，不能证明来源忠实。
例如 `the same access logs` 去掉 `same` 后会变成 `access logs`，仍记为信息损失。

## 样本

| 组 | 框架或边界描述 | 原文 | 人工预期拓扑 |
| --- | --- | --- | --- |
| P1 | `Giving` | The collector gave the filtered records to the indexing worker. | `The collector → the filtered records → the indexing worker` |
| P2 | `Transfer` | The gateway transferred the session transcript to the audit store. | `The gateway → the session transcript → the audit store` |
| P3 | `Sending` | The agent sent the generated summary to the memory module. | `The agent → the generated summary → the memory module` |
| P4 | `Communication` | The reviewer communicated the updated policy to the deployment agent. | `The reviewer → the updated policy → the deployment agent` |
| P5 | `Receiving` | The retrieval service received the query text from the user. | `the user → the query text → The retrieval service` |
| P6 | `Giving` | The configuration snapshot was handed over to the evaluation service by the release manager. | `the release manager → The configuration snapshot → the evaluation service` |
| P7 | `Giving` | The collector passed a copy of the access logs along to the monitoring service. | `The collector → a copy of the access logs → the monitoring service` |
| P8 | `Sending` | The collector sent the access logs to the gateway. The gateway forwarded the same access logs to the audit store. | 两条边：`collector → access logs → gateway`；`gateway → same access logs → audit store` |
| N1 | `Using` | The retrieval service used the cached document to draft a summary. | 无 C15 |
| N2 | `Creating` | The scheduler created an empty task record. | 无 C15 |
| N3 | `Locative_relation` | The audit log is stored in the encrypted volume. | 无 C15 |
| N4 | `Possession` | The operator owns the signing key. | 无 C15 |
| N5 | `Success`（边界描述） | The batch job passed after the review. | 无 C15 |
| N6 | `Constraint`（边界描述） | The policy restricts the agent to read-only access. | 无 C15 |

## 汇总结果

| 指标 | 结果 | 解释 |
| --- | ---: | --- |
| 正例返回非空关系 | 40/40 | 八类正例每次都发现了至少一条 C15 关系 |
| 负例返回空结果 | 30/30 | 六类边界均未误报 C15 |
| 严格逐字完全匹配 | 40/70 | 其中大量是负例天然精确；正例严格命中很低 |
| 严格逐字关系集合命中 | 正例 10/40 | 只有 P3、P6、P7 的部分运行保留完整端点 |
| 规范化完全匹配 | 64/70 | 多数差异只是冠词、大小写和空白 |
| 规范化关系集合命中 | 正例 34/40 | 只有 P7 一次丢失 `copy`，P8 五次丢失 `same` |
| P8 两条独立边发现 | 5/5 | 未把双跳传递压成一条边，也没有跨句补边 |

逐例结果：

| 组 | 严格完全匹配 | 规范化完全匹配 | 唯一剩余差异 |
| --- | ---: | ---: | --- |
| P1 | 0/5 | 5/5 | 模型删除冠词 |
| P2 | 0/5 | 5/5 | 模型删除冠词 |
| P3 | 2/5 | 5/5 | 三次删除冠词或主语冠词 |
| P4 | 0/5 | 5/5 | 模型删除冠词 |
| P5 | 0/5 | 5/5 | 模型删除冠词 |
| P6 | 4/5 | 5/5 | 一次同时删除三个冠词 |
| P7 | 4/5 | 4/5 | 一次把 `a copy of the access logs` 压成 `access logs` |
| P8 | 0/5 | 0/5 | 两条边都在，但五次都把 `the same access logs` 写成 `access logs` |
| N1～N6 | 各 5/5 | 各 5/5 | 无 |

## 结论

1. 这组结果支持 C15 不从某个具体动词或 FrameNet 框架来定义。`give`、`transfer`、`send`、
   `communicate`、`receive`、`hand over`、`pass along` 和 `forward` 都能出现同一来源流转语义。
2. 负例边界稳定。使用、生成、存放、拥有、成功和约束均没有被补写成来源流转。
3. 当前主要失败不是关系类别判断，而是端点词面保真：模型倾向删除冠词，也会丢掉 `copy`、`same`
   这类承载关系身份的修饰语。
4. 因而不能只看关系非空或规范化对象正确就宣称 C15 抽取通过。严格 span 合规和关系拓扑覆盖必须
   分开报告。
5. 暂不根据本实验修改正式提示词。C15 的 FrameNet 来源审查到此收口，不再扩展官方标注例句专项探针；
   后续直接进入真实语料验证。严格 span 与规范化匹配的差异继续保留为端点保真风险，不能把规范化结果
   当作产品验收结果。

## 设计影响

- FrameNet 支持“对象关系语义可以跨不同场景和谓词反复出现”，但不直接生成新的观察轴。
- C15 继续保留“来源、去向与流转”候选，并需要后续用真实语料验证：同一块内是否存在足够多
  直接、间接和最终来源实例，以及去向是否值得独立检索。
- 本实验只覆盖英文短句和人工构造边界，尚未验证中文语料、长段上下文、跨块来源链或真实块规模下的
  注意力漂移。

## 复现

```powershell
backend\.venv\Scripts\python.exe scripts\wiki_framenet_c15_lexical_probe.py --runs 5 --concurrency 10
```
