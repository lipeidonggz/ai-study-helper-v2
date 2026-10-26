# 2026-10-26 · citation-rubric-v7 语义与稳定性校准

## 目的

验证 citation judge 能否：

1. 接受保留原意、转折和范围限制的中文近义转述；
2. 拒绝删除范围限制或把局部判断提升为排他结论的 claim；
3. 在固定真实多 occurrence packet 和不同真实 c1 批次中保持稳定。

本实验解决的是判官语义与稳定性问题。Agent 的“问题本身是对的，但范围更宽”不是语义漂移，030 对其失败的统计已否决。

## 固定配置

| 项目 | 值 |
| --- | --- |
| 模型版本 | DeepSeek Flash 4.1（项目当前使用版本） |
| API model alias | `deepseek-flash` |
| 响应 `actual_model_id` | `deepseek-flash` |
| temperature | `0.0` |
| thinking | disabled |
| Judge prompt | `citation-rubric-v7` |
| Citation checker | `6` |
| Claim annotation prompt | `claim-citation-v2` |
| Agent prompt bundle | `chat-v5` |
| Tool contract | `retrieval-tools-v2` |

DeepSeek API 当前只返回公开 alias `deepseek-flash`，不单独返回内部 `4.1` 子版本号；实验记录同时保留项目确认的产品版本和 API 返回事实。

## 样本

正例 claim：

```text
3. **Trusting what the agent reads**：文中提到企业常问如何保护 MCP 连接，并指出这个问题本身是对的，但真正该问的问题比“MCP 本身”更宽泛。
```

evidence：

```text
Enterprises often ask us how to secure MCP connections. It's a good question, but the right one is broader than MCP specifically.
```

反例保留同一 evidence，但删除“真正问题范围更宽”的限制，并把 MCP 连接安全写成企业唯一、完整的安全问题。

真实批次来自 `evalrev_8789451cdbe141acbad6af7a6307e06b`：

- 一个 v6 下成功的目标 packet；
- 两个 v6 下失败的目标 packet；
- 三个 packet 均为 4 个 contexts、4 个 occurrences、3 个 evidence blocks。

## v6 观察

| 实验 | 结果 |
| --- | --- |
| 单独目标 occurrence，正例 | 10/10 supported |
| 单独目标 occurrence，删除范围限制的反例 | 3/3 unsupported |
| 真实 4-occurrence packet A，目标 claim | 7/10 supported、3/10 unsupported |
| 真实 4-occurrence packet B，目标 claim | 0/10 supported、10/10 unsupported |
| 真实 4-occurrence packet C，目标 claim | 0/10 supported、10/10 unsupported |
| v6 对真实 c1 run 重判 | 10 个 citation checks 中 8 pass、2 fail；目标 claim 6 supported、2 unsupported |

失败理由集中在把 “good question” 与“问题本身是对的”解释为语义强度差异。输入越复杂时，这个错误边界被稳定放大。

## v7 结果

v7 没有加入 case ID 或答案原句，只补充了通用规则：判断正面评价要看完整命题；保留转折和更宽范围时，“问题本身合理／有道理／是对的”与原文等价；只有提升为唯一、完整、排他结论时才算语义增强。

| 实验 | 结果 |
| --- | --- |
| 单独目标 occurrence，正例 | 10/10 supported |
| 单独目标 occurrence，删除范围限制的反例 | 3/3 unsupported |
| 真实 4-occurrence packet A，目标 claim | 10/10 supported；每次 4 个 occurrence 全 supported |
| 真实 4-occurrence packet B，目标 claim | 10/10 supported；每次 4 个 occurrence 全 supported |
| 真实 4-occurrence packet C，目标 claim | 10/10 supported；每次 4 个 occurrence 全 supported |

三个固定真实 packet 合计 30 次独立调用，目标 claim 30/30 supported，没有 flip。

## 真实 c1 回归

### 旧 run 新 revision

- Run：`run_b9adc0e8826544f1a7a40dfe0982cfed`
- v6 revision：`evalrev_8789451cdbe141acbad6af7a6307e06b`，8 pass、2 fail
- v7 revision：`evalrev_82e596da3adc4b5ead90e7af84daa888`，10/10 citation checks pass
- v7 目标 claim：8/8 supported
- 历史 revision 未修改

### 新 c1-only N=10

- Plan：`plan_c71064542068475c8baa3527461d0846`
- Run：`run_b2211fcc2f2b45f9903d0f1921354482`
- Revision：`evalrev_0556d60f3be544a0a0efaf406642168a`
- 运行：10 attempts，10 succeeded，0 infrastructure failure
- R／X：30/30 pass
- Citation integrity：10/10 pass
- Citation support：10/10 pass，0 fail，0 pending
- Annotation artifact：10/10 complete
- 低信息 annotation：0

## 结论

1. v7 在正反例、固定真实批次和不同真实 c1 批次上都保持目标语义与正反区分。
2. 当前证据不支持把所有 occurrence 拆成单条调用；三个固定多 occurrence packet 在 v7 下均 30/30 稳定。
3. `claim-citation-v2` 在 c1 N=10 的真实 annotation artifact 中没有再产生低信息 claim。
4. 这是当前模型版本上的质量证据，不构成对所有未来模型版本的形式化保证。
5. `citation-rubric-v7` 和 `claim-citation-v2` 仍须负责人人工复核后才能视为生效基础件。

## 复现限制

真实模型实验依赖当前 Windows Credential Manager 中的 DeepSeek Key、`deepseek-flash` 服务端版本和本地 eval trace。仓库不保存 Key 或大型 trace；自动化测试只验证输入构造、prompt 不变量和判定编排，不把一次真实模型输出伪装成确定性单元测试。
