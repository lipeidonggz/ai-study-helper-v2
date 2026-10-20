# 001 · 桌面 sidecar 最小启动链

- 状态：已完成
- 开始日期：2026-10-20
- 关联设计：[09 技术选型](../../design/09-technology-selection.md)
- 关联决策：[0001 文档与实现记录结构](../../decisions/0001-documentation-and-record-structure.md)

## 目标

建立第一个可运行的桌面垂直切片：Tauri 启动隐藏的 Python sidecar，Vue 页面确认后端可用，并在 sidecar 异常或应用退出时显示/执行正确行为。

## 范围

本切片包含：

- Tauri 2 桌面壳与 Vue 3 + TypeScript + Vite 前端。
- Python 3.13 + FastAPI + Uvicorn sidecar。
- Tauri 选择随机回环端口，为每次启动生成独立令牌。
- sidecar 只监听 `127.0.0.1`，非健康检查接口必须校验启动令牌。
- sidecar 异常退出时有限次重启并向前端发送状态。
- Tauri 退出时终止 sidecar，避免残留进程。
- 开发环境启动说明与自动化检查。

本切片不包含：

- HTML 入库、collection、SQLite 或 LanceDB。
- DeepSeek、Qwen 或其他模型 API。
- SSE 流式回答、Agent 循环、评测台。
- Windows 安装包与正式 PyInstaller sidecar 发布链；这些在后续切片验证。

## 输入、输出与失败语义

输入：

- 桌面应用启动命令。
- 可用的本地 Python 3.13 虚拟环境。

输出：

- 一个显示后端连接状态的桌面窗口。
- sidecar 的端口、PID、启动次数和运行状态。

失败语义：

- 找不到 Python 解释器或后端启动失败：界面显示 `failed` 和可操作错误，不假装 `ready`。
- sidecar 异常退出：显示 `restarting` 或 `failed`，最多自动重启 2 次。
- 缺少或错误启动令牌：受保护接口返回 HTTP 401。
- 应用退出：先停止监控线程并终止 sidecar。

## 验收条件

- [x] Tauri 开发模式可通过一个仓库根命令启动。
- [x] Vue 页面显示 backend 的 `starting / running / restarting / failed` 状态。
- [x] sidecar 仅监听 `127.0.0.1`。
- [x] 无令牌访问受保护接口返回 401，正确令牌返回运行信息。
- [x] sidecar 异常退出后自动重启，超过上限后显示失败。
- [x] 关闭桌面应用后没有残留 Python 进程。
- [x] sidecar 日志写入本地运行目录，不包含启动令牌或其他敏感信息。
- [x] 后端单元测试、前端类型检查和 Rust 编译检查通过。

## 验证证据

后端：

- `backend/.venv/Scripts/python.exe -m pytest backend/tests`：4 passed。
- 实际 Uvicorn 进程验证：仅监听 `127.0.0.1`，`/health` 返回 200，无令牌访问 `/api/runtime` 返回 401。

前端与桌面壳：

- `corepack pnpm --dir frontend check`：通过。
- `corepack pnpm --dir frontend build`：通过。
- `cargo check --manifest-path desktop/src-tauri/Cargo.toml`：通过。
- `cargo clippy --manifest-path desktop/src-tauri/Cargo.toml -- -D warnings`：通过。
- Tauri 实际启动后，Vue 页面显示 sidecar 的地址、PID、版本和重启次数。
- 连续终止 sidecar 三次：前两次自动重启成功，第三次停止恢复并显示 `failed`。
- 根命令 `corepack pnpm dev` 关闭窗口后退出码为 0，应用、Vite、sidecar 和监听端口均无残留。
- 直接运行编译后的桌面进程，正常关闭退出码为 0。

## 结论与主线

001 已完成，并证明以下运行主线可行：

```text
Tauri 启动应用
→ 选择回环端口并生成启动令牌
→ 启动并监管 FastAPI sidecar
→ Vue 页面通过受保护接口确认后端状态
→ 异常退出有界恢复
→ 应用退出时清理完整进程树
```

本切片确定了两条后续实现必须遵守的基础约束：

- 启动令牌只用于本机进程间鉴权，不进入日志或长期持久化。
- sidecar 生命周期由桌面壳统一管理，不能依赖用户手工启动或停止 Python。

当前证明范围只到开发运行链。产品仍依赖开发机 Python 虚拟环境，尚未证明普通用户机器上的可分发形态。因此架构不需要重开，但下一步必须完成 PyInstaller one-folder sidecar 与 release 应用冒烟验证，再进入 HTML 入库和 RAG 实现。

## 现场细节

Windows venv 的 `python.exe` 会再启动一个 Python 子进程，实际监听端口的是孙进程。只终止直接子进程不足以保证清理，因此桌面壳使用 Windows Job Object 把 sidecar 进程树绑定到应用生命周期，并设置 `KILL_ON_JOB_CLOSE`。

Tauri CLI 在人工关闭窗口后由包装层返回 `0xFFFFFFFF`，并可能遗留 Vite 进程。根开发脚本因此分别启动 Vite 与 `cargo run`，在应用退出后统一清理 Vite；直接运行编译后的应用进程正常退出码为 0。
