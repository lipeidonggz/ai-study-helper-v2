# 032 · 判定总 Token 用量展示

- 状态：待人工验收
- 开始日期：2026-10-26
- 关联设计：[08 评测台](../../design/08-evaluation-console-design.md)
- 前置切片：[023A 评测判定](./023A-evaluation-judgment.md)、[025 引用支持分批](./025-evaluation-citation-support-batching.md)、[031 引用语义校准](./031-citation-semantic-calibration.md)

## 控制契约

### 用户可观察目标

1. 选择一个判定 revision 后，可以直接看到该 revision 的判官总 Token 用量。
2. 总用量区分输入、输出和合计；不要求用户展开逐调用记录后手工累加。
3. 汇总覆盖 R/X criterion judge、citation support 判官、分批调用和协议重试。

### 核心不变量

1. `input_tokens + output_tokens = total_tokens`，两者均来自已记录的 provider usage。
2. 每个已记录 invocation 最多计入一次；人工判定和人工复核不产生模型 Token。
3. 汇总只读取现有不可变事实，不修改 Judgment、CheckResult、invocation 或历史 revision。
4. 旧 revision 只展示现有记录能够还原的用量，不推测未保存的供应商消耗。

### 明确禁止

- 不用提示词字符数估算替代已记录的 provider usage。
- 不把 Agent 回答生成 Token 混入判官 Token 汇总。
- 不因人工改判删除此前真实发生的判官调用成本。

### 失败语义

| 场景 | 期望行为 |
| --- | --- |
| invocation 记录为 provider error | 计入调用数；已记录 Token 为 0 时按 0 汇总 |
| citation support 多批或重试 | 每个批次、每次 invocation 分别计入 |
| 人工改判后旧 LLM 结论不再生效 | 旧调用 Token 仍保留在 revision 总成本中 |
| 旧 revision 缺少某类 usage | 只汇总已有字段，不伪造估算值 |

### 决策预算

- 预计 A 级决策：1 项。
- 预计 B 级决策：0 项。
- C 级决策：无。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | A | revision 总 Token 应统计哪些调用 | 汇总 revision 内全部已记录 judge invocation，包括 R/X、citation support、分批和重试；人工操作不计 Token | 用户需要知道一次判定实际发生的模型成本，已生效结果不能代表历史成本 | 只统计当前生效结果会漏掉重试和被改判调用；使用字符估算则不是供应商 usage | 新增只读 API 字段和界面摘要 | 调用聚合单元测试、人工改判用量不变测试、真实 revision 回放 | 已执行（A） |

## 验收条件

- [x] 后端 revision 返回判官输入、输出、合计和调用次数。
- [x] 汇总包含 criterion judge 与 citation support 的所有 invocation。
- [x] 重试调用不会漏计或重复计。
- [x] 前端 revision 摘要直接展示三项 Token 数值。
- [x] 定向测试、完整后端测试和项目质量门通过。
- [ ] 重启桌面应用后目视确认摘要行展示正常。

## 证据包

### 自动化证据

- `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_evaluation_judgment.py backend/tests/test_evaluation_api.py backend/tests/test_evaluation_baseline.py -q --basetemp backend\.runtime\pytest-032-targeted`：通过。
- `backend\.venv\Scripts\python.exe -m pytest backend/tests -q --basetemp backend\.runtime\pytest-032-full`：完整后端测试通过。
- `corepack pnpm quality:quick`：注释、Ruff、Pyright、Vue TypeScript、Vite build、Rust fmt／check／clippy 全部通过。
- `test_citation_support_retries_invalid_output_once_then_passes` 同时核对 criterion 与 citation 重试 invocation 的输入、输出、合计和调用次数。
- `test_human_override_appends_judgment_and_switches_effective_binding` 验证人工改判后历史判官 Token 不变。

### 真实数据回放

- Revision：`evalrev_0556d60f3be544a0a0efaf406642168a`
- 返回：输入 `38,735`、输出 `5,933`、合计 `44,668`、调用 `43` 次。
- 汇总与持久化 invocation 逐项手工累加一致。

### 人工验收

- [ ] 重启桌面应用，在判定 revision 摘要行确认“调用次数、输入、输出、合计”四项可见且换行正常。

## 已知风险与后续

- 超时或连接中断且未取得供应商 usage 的调用只能记录为 0；该限制沿用现有 invocation 契约。
