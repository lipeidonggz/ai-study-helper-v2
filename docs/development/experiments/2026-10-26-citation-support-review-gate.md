# 2026-10-26 · 引用语义支持候选与人工复核门禁

## 目的

验证把 `citation_support` 从“单次 LLM unsupported 直接 fail”改为“自动候选或 pending + 人工 claim 级复核”后：

1. 确定性 `citation_integrity` 仍可独立硬失败；
2. 单次语义 judge 不再自动判死 attempt；
3. 人工确认、驳回和 unknown 能改变有效检查与聚合；
4. 关系三阶段判官退出默认调用后，主语义判官是否能独立识别已知角色反转；
5. pending 不计 pass，历史运行和旧 revision 不被改写。

## 固定现场

- ExecutionRun：`run_a33c5d27b0b845d5833ea4dac701ffb2`
- 样本：c2-001 N=10，10 个 product attempt
- Agent、答案、citation annotation、evidence snapshot 和 trace 均冻结
- 只追加 judgment revision，不重跑 Agent
- 人工复核裁决未包含在本实验结果中

## 门禁机制验证

自动化测试覆盖：

- 自动 `unsupported` → `citation_support=pending`、`observed_result=fail`，错误码 `citation_support_review_required`；
- unsupported 的 attempt 投影为未确认普通失败，`review_pending=true`，`check_failures` 与红线数为 0；
- 存在未确认候选时，case 与 revision 保持 pending，不能提前通过；
- 自动 `unknown`、非法输出、provider 失败和预检失败保持 pending；
- 多批次中任一批出现语义候选，不能被其他 pass 批次掩盖；
- 人工按 occurrence 提交完整 claim 与联合 block 组；
- 人工确认 unsupported → 有效检查 fail；
- 人工驳回 → 有效检查可恢复 pass；
- 人工保留 unknown → 有效检查保持 pending；
- 错误 occurrence、错误 block 组和对 `citation_integrity` 的人工覆盖被拒绝；
- 默认 citation judge 每个批次只调用主语义判官，关系三阶段调用次数为 0。

## 真实重判

### citation-rubric-v9 / checker 16

- Revision：`evalrev_71a3d1112d9e47f2b70fa56d161b8205`
- criterion：60/60 pass
- citation integrity：10/10 pass
- citation support：10/10 pass
- semantic candidate：0

### citation-rubric-v10 / checker 17

在 v9 基础上加入不含具体样本事实的通用关系保真规则：

- 收益／代价、因果、条件、方向关系必须在 claim 与证据中保持；
- 数字和修饰从句跟随其明确修饰对象；
- 标点和连接词不能单独建立关系。

- Revision：`evalrev_5b56b28f8d5a49b2ae1de91065b01d90`
- criterion：60/60 pass
- citation integrity：10/10 pass
- citation support：10/10 pass
- semantic candidate：0

### citation-rubric-v11 / checker 18

进一步加入通用规则：答案显式使用“收益／代价／原因／结果”等角色标签时，该标签属于 claim 命题，证据必须把相同事实放在相同角色下。

- Revision：`evalrev_7e351c4638af4f46a1da3ed06fa0bc30`
- criterion：60/60 pass
- citation integrity：10/10 pass
- citation support：10/10 pass
- semantic candidate：0

## 已知角色反转探针

`attempt_80cd96f68bcd432aa6d66824d14c6cf9` 的 `citation_0004` 把“约 0.4% 良性命令被拦截”显式归为“代价”；证据原文的对应位置是：

```text
it minimizes friction (roughly 0.4% of benign commands blocked)
at the cost of missing a fraction of risky ones
```

已知事实关系是：0.4% 属于“降低摩擦”的量化，漏掉风险行为才处于 cost 位置。

### 完整冻结批次重复

使用 v11 对该 attempt 的完整冻结 packet 重复 5 次：

| 次数 | 正确目标 | 实际结果 |
| --- | --- | --- |
| 1 | 形成 unsupported 候选 | supported |
| 2 | 形成 unsupported 候选 | supported |
| 3 | 形成 unsupported 候选 | supported |
| 4 | 形成 unsupported 候选 | supported |
| 5 | 形成 unsupported 候选 | supported |

主判官 5/5 把完整证据中的 `at the cost ...` 误认为是 0.4% 的角色依据。

### 单 claim 最小探针

把同一 claim 和同一证据块单独送入 v11，重复 5 次：

| 次数 | 实际结果 |
| --- | --- |
| 1 | unknown |
| 2 | unknown |
| 3 | unknown |
| 4 | unknown |
| 5 | unknown |

说明输入上下文和同批 occurrence 会显著影响判断。单 claim 探针只证明该模型能在最小输入下表达不确定，不能替代完整批次的候选召回。

### 主判官重复稳定性

对上述完整冻结 packet 重复调用当前 `citation-rubric-v11` 20 次：

| 结果 | 次数 |
| --- | --- |
| supported | 20 |
| unsupported | 0 |
| unknown | 0 |
| 非法输出 | 0 |

主判官不是随机漂移导致的一次漏判，而是在该输入上稳定偏向 supported。因此增加同一主判官的重复次数并投票，不能修复这个盲区，只会增加成本。

### 关系候选器工作量探针

对同一固定 revision 的 10 个 attempt 重新运行现有关系三阶段候选探测；只统计主判官已判 supported、且关系阶段实际进入比较的 claim：

- 关系型判定：38 个
- supported：33 个
- unsupported 候选：3 个
- unknown 候选：2 个
- 候选合计：5 个
- 已知真实错误：`attempt_80cd96f68bcd432aa6d66824d14c6cf9 / citation_0004`
- 除该已知错误外，其余 4 个候选仍需人工排除
- 协议与模型调用失败：0

这组数据说明关系候选器能够抓到已知错误，但在本 run 上每个真实错误伴随约 4 个额外人工候选。将该阶段直接恢复为默认链路会明显增加复核工作量，不能仅凭“抓到了目标错误”就认为收益大于成本。

## 结论

1. “自动候选 → 人工复核 → 有效门禁”的状态流转和 append-only 审计已成立。
2. 当前真实 run 没有因单次语义判断自动判死，满足本切片对误杀的第一阶段目标。
3. 主语义判官对已知角色反转仍存在稳定假阴性；当前 10/10 pass 不能解释为引用质量提升。
4. 关系三阶段判官不应直接恢复为默认一票红线，但它作为高风险候选探测器的价值得到保留证据支持。
5. 在候选召回校准完成前，037 必须保持“待确认”，不能作为新的质量基线。
6. 负责人接受当前 D3：关系判官不进入默认门禁；主判官稳定假阴性作为已接受风险，由 BL-0011 跟踪，而不是通过恢复默认关系链路立即解决。

## 复现

- 自动化：
  - `backend\.venv\Scripts\python.exe -m pytest backend/tests -q --basetemp .pytest-t37-full-root`
  - `corepack pnpm quality:quick`
- 固定 run：只追加 judgment revision；从评测 revision 的 `citation_support.payload.batches[].input` 可重放完整判官输入。
- 探针：使用同一 `citation-rubric-v11` 系统提示词、同一模型和 `max_output_tokens=4096`，分别重复完整 packet 与单 claim packet 5 次。
