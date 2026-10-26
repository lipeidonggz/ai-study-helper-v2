# 034 · c2-001 指标归属金标准修订

- 状态：待确认
- 开始日期：2026-10-26
- 关联设计：[03 用例与金标准](../design/03-cases-and-gold-standard.md)
- 前置切片：[023A 评测判定](./023A-evaluation-judgment.md)、[033 异步 GitHub 交付](./033-async-github-delivery-gate.md)
- 发现现场：`run_619f5e25271b4a9a99ac115be6c6879b`

## 控制契约

### 用户可观察目标

1. `c2-001` 的 X4 不再把原文中的合法补充比例误判为指标归错。
2. X4 仍拒绝把 83%、93%、Gray Swan 成功率和其他指标对调、合并或赋予错误含义。
3. 新 case revision 和 suite revision 只追加，不修改历史 revision。

### 核心不变量

1. X4 的判定对象是“百分比与指标含义的归属关系”，不是“是否只能出现某些百分比”。
2. 当前固定 HTML 快照中的显式百分比必须完整列入 X4。
3. `100%` 只作为“模型层防御不可能完全有效”的一般性表述，不当作性能指标。
4. 答案只提及一个或部分比例不构成违规。
5. 额外百分比是否真实存在不由 X4 判断，不能把 X4 扩成事实真实性检查。

### 明确禁止

- 不修改 `run_619f5e25271b4a9a99ac115be6c6879b` 的历史 revision 或判定。
- 不因为答案出现 17%、0.4% 或 84% 等合法比例而判 X4 违规。
- 不用 X4 替代引用完整性或引用支持检查。

### 失败语义

| 场景 | 期望行为 |
| --- | --- |
| 83%、17%、0.4% 均归属正确 | X4 `not_violated` |
| 把 83% 说成用户批准率 | X4 `violated` |
| 把 5–6% 说成 auto mode 拦截率 | X4 `violated` |
| 把 84% 说成过度行为拦截率 | X4 `violated` |
| 额外百分比未被 X4 列出 | X4 不直接判违规 |

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | B | X4 如何识别百分比指标归错 | 列全当前固定快照中的显式百分比及含义，并明确只判断对调、合并和错误归属 | c2 旧 X4 列出 83%、93%、Gray Swan 成功率，但遗漏同文 84%、17%、0.4% | 保留概括规则仍会被判官解释成数字白名单；把事实真实性全部塞入 X4 会侵占引用检查职责 | 新 case revision 与新 suite revision | 固定正例 10 次、反例 9 次、c2-only N=10 | 待确认 |

## 已发布对象

- Case revision：`cr_c2_001_639a1a601db1`
- Case revision number：6
- Design suite：`sr_design03_704a3f5b6a6b`
- 17 用例 suite：`sr_除截断用例外所有_8aa0caa50c03`

## 证据包

### 固定判官验证

- 模型：DeepSeek Flash 4.1（API alias `deepseek-flash`），temperature `0.0`，thinking disabled。
- 使用 `run_619...` 中实际被判 X4 fail 的 answer。
- 修订后 X4：10/10 `not_violated`。
- 错误归属反例：9/9 `violated`，包括 83% 冒充用户批准率、5–6% 冒充 auto mode 拦截率、84% 冒充过度行为拦截率。

### c2-only N=10

- Plan：`plan_0d9a8c9e69df410396e82797a2a023e2`
- Run：`run_7388764117024001aa8aa24bd5720afd`
- Revision：`evalrev_72c307d419924cab8c038d02a27577be`
- 运行：10 succeeded，0 infrastructure failure。
- X4：10/10 pass，0 violation。
- R1/R2：20/20 pass。
- Citation integrity：10/10 pass。
- Case aggregate：9 pass、1 fail，唯一失败为一次真实 citation support 错误。

### 唯一 citation failure

- Attempt：`attempt_e06b3c6d9b9a491990f4768ff44e7558`
- 答案写“这是以降低打扰为代价换来的：它大约只拦下 0.4% 的正常命令”。
- 原文含义是“约 0.4% 良性命令被误拦”本身代表低摩擦，真正的代价是漏掉一部分风险行为。
- 该答案把因果方向写反，citation judge 判 `unsupported` 有事实依据，不是引用判官误判。

### 历史因果反转审计

按唯一 attempt 统计，`c2-001` 共有 44 个有效 product attempt；其中 8 个明确把原文的
“低摩擦／低误拦是收益，漏过风险行为是代价”写反。

| Run | Attempts | 明确因果颠倒 |
| --- | ---: | ---: |
| `run_d72af6d476c54710818dd517b5baaa69` | 1 | 1 |
| `run_69bb6c200d6f4afcb826e06adaa7c623` | 1 | 0 |
| `run_1658cca35bd34a06b2942e632970fc41` | 1 | 0 |
| `run_a36943ad6bcf4edf85f3f789e8f89e49` | 1 | 0 |
| `run_183432c830554ee69c4ae573886e0bf8` | 10 | 3 |
| `run_1a0fd3812c3a4fb7bf2d152d0a9e8e2d` | 10 | 1 |
| `run_619f5e25271b4a9a99ac115be6c6879b` | 10 | 1 |
| `run_7388764117024001aa8aa24bd5720afd` | 10 | 2 |

- 全部历史：8/44，约 18.2%。
- 四个完整 N=10 run：7/40，约 17.5%。
- 新 X4 revision 对 X4 误判的修复有效，但生成侧因果颠倒仍存在。
- 新 run 的 2 个因果颠倒中只有 1 个被 citation support 抓到；另一个 claim 已包含完整错误关系，
  仍被 citation-rubric-v7 判为 supported，说明判官还需要通用关系方向检查。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么 X4 应列全固定快照的百分比，而不是只列三个易混指标？ | 待填写 | 待核对 | 待填写 |
| R2 | 为什么 X4 不应负责判断额外百分比是否真实存在？ | 待填写 | 待核对 | 待填写 |
| R3 | 为什么新 c2 run 中唯一的 citation fail 不能改判为 X4 回归？ | 待填写 | 待核对 | 待填写 |

## 已知风险与后续

- X4 与当前 HTML 快照内容绑定；文档内容更新后需要重新核对比例全集。
- 剩余 Agent 问题不是单次偶发：历史明确出现 8 次因果颠倒，且在最新 N=10 中仍有 2 次。
- 后续 035 先修复 citation judge 对因果和权衡方向的漏检，再修改 Agent 的通用关系保真约束。
