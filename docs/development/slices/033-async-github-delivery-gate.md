# 033 · 异步 GitHub 交付与依赖门禁

- 状态：待后台验证
- 开始日期：2026-10-26
- 关联设计：[08 评测台](../../design/08-evaluation-console-design.md)
- 前置切片：[032 判定总 Token 用量展示](./032-judge-token-usage-visibility.md)

## 控制契约

### 用户可观察目标

1. 本地提交后的一条命令可以创建远端分支、PR 并启用 auto-merge，然后立即返回。
2. GitHub 继续执行 required checks 和自动合并，主对话不持续轮询。
3. 后续工作只有在明确依赖该合并时，才检查或等待并核对目标分支确实包含 merge commit。

### 核心不变量

1. 每次交付有稳定 `delivery_id`，状态原子写入本地 JSON，并维护 `latest.json` 指针。
2. `start` 只发布已经存在的 commit，不隐式提交、改写或 rebase 工作区。
3. `require-merged` 只有在 PR 已合并且 merge commit 已包含于目标分支时返回成功。
4. CI 失败、PR 关闭、auto-merge 未启用、超时和 merge commit 不在目标分支必须返回不同失败状态。
5. 同时开始依赖工作时必须使用独立 worktree／分支，不能建立在一个尚未合并的基线上。

### 明确禁止

- 不在主对话中持续运行默认 20～30 分钟的同步 watcher。
- 不因 PR 创建或检查通过就提前假定已经合并。
- 不把本地 `latest.json` 直接视为远端事实；执行依赖门禁时必须刷新 GitHub 状态。
- 不把凭证或完整 API Key 写入状态文件、日志或提交。

### 失败语义

| 场景 | 命令结果 | 后续工作 |
| --- | --- | --- |
| required checks 未完成 | `blocked`／退出码 3 | 不依赖该合并，改用独立分支或稍后再等 |
| PR 已合并且 merge commit 在 base | `ready`／退出码 0 | 可以继续 |
| required check 失败、PR 关闭或冲突 | `failed`／退出码 1 | 停止依赖工作并修复 |
| 等待超过上限 | `timed_out`／退出码 2 | 显式报告，不假定成功 |
| 远端 base 已变化且本地基线不匹配 | `start` 失败 | 先同步或 rebase，禁止覆盖远端 |

### 决策预算

- 预计 A 级决策：2 项。
- 预期 B 级决策：0 项。
- C 级决策：无。

## 命令

```powershell
corepack pnpm delivery:start --branch fix/example
corepack pnpm delivery:status --delivery-id delivery_YYYYMMDDTHHMMSSZ_abcdef12
corepack pnpm delivery:require --delivery-id delivery_YYYYMMDDTHHMMSSZ_abcdef12
corepack pnpm delivery:require --delivery-id delivery_YYYYMMDDTHHMMSSZ_abcdef12 --wait
```

`delivery:start` 不等待 checks。只有在后续步骤确实需要该合并时，才使用 `delivery:require`。

## 验收条件

- [ ] `start` 创建远端 commit、branch、PR 并开启 auto-merge。
- [ ] `status` 刷新 `checks_pending`、`merge_pending`、`merged`、`failed`。
- [ ] `require-merged` 在 pending 时返回 blocked，在 merged 时返回 ready。
- [ ] merge commit 已进入 base 的验证不依赖本地远端分支引用。
- [ ] 状态文件和 latest 指针原子更新，非法 delivery ID 不能路径穿越。
- [ ] 定向测试、完整后端测试和项目质量门通过。

## 证据包

### 自动化证据

- `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_github_async_delivery.py backend/tests/test_delivery_watcher.py -q --basetemp backend\.runtime\pytest-033-targeted-3`：17 项测试通过。
- `backend\.venv\Scripts\python.exe -m pytest backend/tests -q --basetemp backend\.runtime\pytest-033-full`：完整后端测试通过。
- `corepack pnpm quality:quick`：注释、Ruff、Pyright、Vue TypeScript、Vite build、Rust fmt／check／clippy 全部通过。
- 单测覆盖 pending、merge pending、merged、check failure、merge commit 不在 base、PR head 被改写、状态文件原子更新、非法 delivery ID、rename／delete diff 和 blob 上传。

### 后台交付演练

- 待使用 `delivery:start` 发布本次切片后填写 delivery ID、PR 和最终状态。

## 已有优化

- 当前 GitHub CI 实际执行约 2 分 14 秒，两个 job 并行。
- 主对话不应占用这段时间；后台交付由 GitHub auto-merge 负责。
- 后续依赖点检查是一次 GitHub API 查询；仅在依赖未满足且必须等待时轮询。

## 已知风险与后续

- GitHub auto-merge 依赖仓库设置和分支保护；未启用时必须显式失败。
- 本地真实 merge commit 可能需要后续单独同步；门禁通过 GitHub compare API 验证远端正向包含关系。
