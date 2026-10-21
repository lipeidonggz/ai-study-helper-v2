# 0005 · 代码注释与可读性门禁

- 状态：已接受
- 决定日期：2026-10-21
- 影响范围：本地开发、Git 提交、GitHub Actions、切片验收

## 背景

`AGENTS.md` 已要求公共接口、边界类型、安全边界、资源生命周期和重要不变量必须有注释，并与代码同步。仅依靠 Agent 记忆或人工抽查会产生遗漏，而且子 Agent 同样不是确定性检查器。

需要一个每次代码变化都会自动执行、失败时明确阻断提交或 CI 的机制。

## 候选方案

| 方案 | 问题 | 结论 |
| --- | --- | --- |
| 每次调用子 Agent 审查 | 无法保证触发；输出有遗漏概率；不能提供稳定退出码 | 只作为语义复核 |
| 只写人工检查清单 | 容易忘记，无法阻止不合格提交 | 保留，但不单独依赖 |
| 静态检查＋Git hook＋CI | 可重复、可失败、可阻断 | 采用 |

## 决策

采用三层门禁：

1. **静态检查**
   - Python Ruff 要求公共类、方法和函数有 docstring。
   - `scripts/check-comments.mjs` 要求前端边界类型和顶层函数有紧邻 JSDoc。
   - Rust 在 `clippy -D warnings` 下开启 `missing_docs`。
   - 检查无上下文 TODO 和已知过期术语。
2. **确定性执行**
   - `pnpm quality:quick` 检查注释、后端静态检查、前端类型检查与 Rust check。
   - `pnpm quality` 在 quick 基础上运行全部后端测试。
   - `pnpm install` 的 `prepare` 自动把 Git hooks 路径设置为 `.githooks`。
   - pre-commit 执行 `pnpm quality:quick`。
   - GitHub Actions 在每个 push 和 PR 上运行相同门禁；仓库设置必须把该 workflow 设为 required check。
3. **语义审查**
   - 每个切片增加“注释与可读性审查”记录。
   - 人工或子 Agent 检查旧注释是否过期、命名是否符合当前职责、关键不变量是否解释充分。
   - 子 Agent 的结论不能替代静态检查和 CI。

## 后果

- 注释缺失和已知过期描述会使命令失败，不再依赖记忆。
- 本地 hook 可被跳过，因此 GitHub required check 是最终强制门禁。
- Lint 无法证明注释“语义正确”；这部分仍需切片审查和独立 reviewer。
- 新增公共接口时，类型检查与注释检查会同时要求更新契约说明。

## 推翻条件

如果工具链无法在 Windows 或 CI 稳定执行，或维护成本明显高于实际拦截收益，可以替换具体工具；但“确定性检查＋required CI＋语义审查”的分层目标不能退化为纯 Agent 提醒。
