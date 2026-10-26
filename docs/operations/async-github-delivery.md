# 异步 GitHub 交付

本流程把“提交后的远端等待”交给 GitHub，而不是让主对话持续轮询。

OpenAI 官方文档说明 [Codex Cloud](https://learn.chatgpt.com/docs/cloud)
可在后台持续处理任务，CLI 也能通过
[`codex cloud`](https://learn.chatgpt.com/docs/developer-commands?surface=cli#cli-codex-cloud)
启动任务并以 JSON 查询状态。本仓库的交付对象是 GitHub PR 和 required
checks，因此使用 GitHub auto-merge 作为持久执行者；本地只在依赖点通过
GitHub API 查询。

## 启动

先完成本地提交和质量门，然后运行：

```powershell
corepack pnpm delivery:start --branch fix/example
```

命令会：

1. 读取当前 `HEAD` 和指定本地基线；
2. 创建远端分支、等价提交和 PR；
3. 启用 merge commit auto-merge；
4. 写入状态 JSON 并立即返回。

默认以 `HEAD^` 作为本地基线，并通过默认的 `main` 校验远端基线。只有当前分支包含多个待交付 commit 时，才显式传 `--base-commit <完整 SHA>`；不要在 PowerShell 参数中使用未加保护的 `HEAD^`。

`start` 成功输出 delivery ID、PR 和初始状态后，当前对话轮次应立即结束。不要在同一轮继续执行 status 或 wait，否则负责人无法提交下一条命令，后台交付失去意义。

默认状态目录为 `backend/.runtime/delivery/`：

- 每次交付一个 `<delivery_id>.json`；
- `latest.json` 指向最近一次交付；
- 状态文件不进入 Git。

## 查询

不需要等待时，只查询一次：

```powershell
corepack pnpm delivery:status --delivery-id <delivery_id>
```

## 依赖门禁

新工作依赖该 PR 已合并时：

```powershell
backend\.venv\Scripts\python.exe -m scripts.github_async_delivery require-merged --delivery-id <delivery_id>
```

退出码：

- `0`：已合并，且 merge commit 已包含于目标分支；
- `1`：检查失败、PR 关闭、冲突或其他终态失败；
- `2`：等待超时；
- `3`：仍在执行，当前不允许依赖它继续。

只有确实无法绕开依赖时才追加 `--wait`。不依赖合并的新工作应使用独立分支或 worktree。

等待权限属于负责人：只有负责人明确要求等待合并完成时才能使用 `--wait`。如果只是新任务依赖旧交付，先做一次非等待检查；未完成时返回 `blocked`，结束当前轮并等待负责人决定。

门禁通过直接执行 Python 调用以保留稳定退出码。`corepack pnpm delivery:require`
适合人工查看，但 pnpm 会把子进程的非零码统一报告为 1，不适合作为机械依赖门禁。

## 安全边界

- `start` 不提交工作区，不隐式 rebase，不覆盖已有远端分支。
- 依赖门禁必须刷新 GitHub 远端事实，不能只读本地状态文件。
- PR head 必须仍等于 start 记录的交付 commit；被 force-push 或改写时门禁失败。
- GitHub token 从本机 Git credential helper 临时读取，不进入状态文件和日志。
