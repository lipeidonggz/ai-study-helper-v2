# 033 · 异步 GitHub 交付与依赖门禁

- 状态：已完成
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
backend\.venv\Scripts\python.exe -m scripts.github_async_delivery require-merged --delivery-id delivery_YYYYMMDDTHHMMSSZ_abcdef12
backend\.venv\Scripts\python.exe -m scripts.github_async_delivery require-merged --delivery-id delivery_YYYYMMDDTHHMMSSZ_abcdef12 --wait
```

`delivery:start` 不等待 checks。只有在后续步骤确实需要该合并时，才使用 `delivery:require`。

## 验收条件

- [x] `start` 创建远端 commit、branch、PR 并开启 auto-merge。
- [x] `status` 刷新 `checks_pending`、`merge_pending`、`merged`、`failed`。
- [x] `require-merged` 在 pending 时返回 blocked，在 merged 时返回 ready。
- [x] merge commit 已进入 base 的验证不依赖本地远端分支引用。
- [x] 状态文件和 latest 指针原子更新，非法 delivery ID 不能路径穿越。
- [x] 定向测试、完整后端测试和项目质量门通过。

## 证据包

### 自动化证据

- `backend\.venv\Scripts\python.exe -m pytest backend/tests/test_github_async_delivery.py backend/tests/test_delivery_watcher.py -q --basetemp backend\.runtime\pytest-033-targeted-3`：17 项测试通过。
- `backend\.venv\Scripts\python.exe -m pytest backend/tests -q --basetemp backend\.runtime\pytest-033-full`：完整后端测试通过。
- `corepack pnpm quality:quick`：注释、Ruff、Pyright、Vue TypeScript、Vite build、Rust fmt／check／clippy 全部通过。
- 单测覆盖 pending、merge pending、merged、check failure、merge commit 不在 base、PR head 被改写、状态文件原子更新、非法 delivery ID、rename／delete diff 和 blob 上传。

### 后台交付演练

- Delivery：`delivery_20261026T042845Z_4d5e116d`
- PR：[#32](https://github.com/lipeidonggz/ai-study-helper-v2/pull/32)
- `delivery:start` 约 19 秒返回 `checks_pending`，主流程未等待 CI。
- `delivery:status` 非阻塞查询约 3 秒，正确返回两个 required checks 正在执行。
- 依赖点直接调用 Python `require-merged --wait`，约 48 秒后返回 `ready`。
- Required checks：`backend-and-frontend`、`desktop` 均 success。
- Merge commit：`6f70979921e540643992b3a14da731b97fb7d01e`，GitHub compare 验证已进入 `main`。

第二次真实交付用于验证依赖修复和完整状态迁移：

- Delivery：`delivery_20261026T043455Z_c564e232`
- PR：[#33](https://github.com/lipeidonggz/ai-study-helper-v2/pull/33)
- `delivery:start` 约 17 秒返回 `checks_pending`。
- 非阻塞状态依次观测到 `checks_pending` 和 `merge_pending`。
- 依赖点 `require-merged --wait` 返回 `ready`，没有只凭 checks success 提前放行。
- Merge commit：`aaae654cafd6ee30cfd055236882fce11ec77650`，已进入 `main`。

## 已有优化

- 当前 GitHub CI 实际执行约 2 分 14 秒，两个 job 并行。
- 主对话不应占用这段时间；后台交付由 GitHub auto-merge 负责。
- 后续依赖点检查是一次 GitHub API 查询；仅在依赖未满足且必须等待时轮询。

## 已知风险与后续

- GitHub auto-merge 依赖仓库设置和分支保护；未启用时必须显式失败。
- 本地真实 merge commit 可能需要后续单独同步；门禁通过 GitHub compare API 验证远端正向包含关系。
