# 015 · GitHub 后台交付与失败可见性

- 状态：已完成
- 开始日期：2026-10-22
- 关联设计：[ADR 0007 人-AI 协同控制模型](../../decisions/0007-human-ai-collaboration-control-model.md)
- 前置切片：[014 本地时间显示与 UTC 时间戳契约](./014-local-time-timestamp-contract.md)

## 控制契约

### 用户可观察目标

1. 提交、PR、required checks 与合并不再占用当前开发对话等待。
2. checks 失败、PR 冲突、PR 被关闭或 auto-merge 未启用时，本地状态文件明确记录原因和链接。

### 核心不变量

1. watcher 只把认证 token 保存在进程内存，不写入状态文件、日志或命令行。
2. required checks 同名重复运行时，以最新一次运行为准。
3. 只有 required checks 全部成功且 GitHub 完成合并，才记录 `merged`。
4. 检查、冲突、关闭、auto-merge 配置或 API 连续失败都进入 `failed`，不能静默超时成功。
5. 状态文件使用原子替换，读取方不会看到半写入 JSON。

### 明确禁止

- 不把“PR 已创建”当作“已经合并”。
- 不在 check 失败时继续把该分支当作新基线。
- 不记录或输出 GitHub token。

### 失败语义

| 场景 | 状态文件结果 | 后续动作 |
| --- | --- | --- |
| required check 失败 | `failed / checks_failed` | 停留在当前分支修复 |
| PR 冲突 | `failed / merge_conflict` | 更新分支并重新验证 |
| PR 未合并即关闭 | `failed / pr_closed` | 人工确认是否重建 PR |
| auto-merge 未启用 | `failed / auto_merge_disabled` | 修正仓库或 PR 设置 |
| API 连续失败 | `failed / github_api_error` | 检查凭证与网络 |
| 超出观察时限 | `timed_out / timeout` | 保留 PR 并人工检查 |

### 决策预算

- 预计 A 级决策：2 项。
- B／C 级决策：无。

## 目标

把等待 CI 的时间移到 GitHub 后台，同时让失败结果可被下一轮开发可靠发现。

## 输入与输出

输入：

- PR 编号；
- GitHub checks、mergeable 与 auto-merge 状态。

输出：

- `backend/.runtime/delivery-status.json`；
- watcher 退出码 `0=merged`、`1=failed`、`2=timed_out`。

## 不包含

- 自动修改失败代码；
- 自动解决 merge conflict；
- 在聊天客户端主动弹出系统通知；
- 绕过 required checks。

## 验收条件

- [x] feature branch 不再同时触发 `push` 与 `pull_request` 两套检查。
- [x] Windows desktop job 使用 Rust 构建缓存。
- [x] watcher 可识别 merged、checks 失败、冲突、关闭、auto-merge 缺失和超时。
- [x] watcher 状态文件不包含 token。
- [x] 完整质量门通过。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | A | 如何结束前台等待 | PR 创建后启用 auto-merge，由独立 watcher 轮询并写状态 | ADR 0007 要求降低等待成本且保留失败可发现性 | 前台轮询阻塞对话；完全不等待会丢失失败信号 | 当前对话可立即继续，GitHub 后台完成合并 | watcher 分类测试与真实 PR 观察 | 已完成 |
| D2 | A | 重复 CI 的处理 | `push` 只监听 `main`，功能分支由 `pull_request` 检查 | 同一提交不需要两套等价 required checks | 保留双触发会浪费时间；删除 push 全部触发会漏掉主分支保护 | feature 分支 CI 运行数减半 | workflow 变更与后续 PR 检查数 | 已完成 |

## 证据包

### 自动化证据

- watcher 分类测试：7 passed。
- 真实 PR #10 观察：识别 `merged`，写入两个 required checks 与合并 SHA `b0f2e973`。
- `corepack pnpm quality`：120 passed。
- Ruff、Pyright、Vue typecheck、前端构建、Rust fmt／check／clippy：通过。

### 运行证据

- watcher 状态文件真实写入 `backend/.runtime/pr10-watch-test.json`，内容不包含 token。
- feature branch 工作流改为只由 `pull_request` 触发，`main` 的 push 继续执行质量门。
- Windows desktop job 在运行 `pnpm --dir desktop check` 前启用
  `Swatinem/rust-cache@v2`，缓存工作目录为 `desktop/src-tauri -> target`。

### 人工验收

- [x] PR #11 的 required checks 全部通过，GitHub auto-merge 成功，watcher 状态更新为 `merged`。

## 反向解释

1. 为什么 PR 创建成功不等于交付完成？
2. auto-merge 开启后，哪些情况仍会让 PR 保持打开？
3. 下一个切片开始前，为什么必须先检查 delivery 状态？

## 协作复盘

- 等待 GitHub checks 占用了实际开发时间，暴露的是交付流程边界问题，不是 CI 本身错误。
- 修复方向是减少重复检查并把等待移出前台，而不是跳过 required checks。

## 已知风险与后续

- 本地后台 watcher 只能在开发机运行；机器关机或进程被杀时会停止更新，下次读取到的是最后状态。
