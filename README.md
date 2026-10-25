# ai-study-helper v2

一个面向普通 Windows 用户的本地文档学习问答 Agent。用户可以导入自己的 HTML 文档，基于文档与模型内置知识提问；系统需要给出可核对、可复现、可评测的回答。

## 当前状态

产品需求、用例、架构、检索、入库、评测与技术选型已经完成。当前已实现桌面运行与分发基础：Tauri 启动并监管 FastAPI sidecar，支持 PyInstaller one-folder、NSIS 按用户安装、WebView2 引导、本地鉴权、有限重启、进程树清理和用户数据保留。

本项目采用 clean-room 方式重建。旧仓库只作为学习档案，不作为实现来源：

<https://github.com/lipeidonggz/ai-study-helper>

## 技术基线

```text
Tauri 2
Vue 3 + TypeScript + Vite
Python 3.13 + FastAPI + Pydantic + Uvicorn
LangGraph
SQLite + SQLAlchemy + Alembic
LanceDB
DeepSeek API / Qwen embedding API
```

首版定位为 Windows x64 单机桌面应用：不依赖 Docker、外部数据库或本地模型；只监听本机回环地址；用户分别配置对话与 embedding API Key。

## 本地开发

前置环境：

- Windows 10/11 x64
- Python 3.13
- Node.js 22.12 或更高版本
- Corepack
- Rust stable MSVC toolchain
- Visual Studio 2022 Build Tools，包含 C++ workload
- WebView2 Runtime

首次安装：

```powershell
py -3.13 -m venv backend\.venv
backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.lock
backend\.venv\Scripts\python.exe -m pip install -e backend --no-deps
corepack pnpm install
```

启动桌面应用：

```powershell
corepack pnpm dev
```

执行检查：

```powershell
corepack pnpm check
corepack pnpm backend:test
```

创建 PR 后可在后台观察 required checks 并开启 auto-merge：

```powershell
backend\.venv\Scripts\python.exe scripts\watch_github_delivery.py `
  --pr <PR编号> `
  --enable-auto-merge
```

结果原子写入 `backend/.runtime/delivery-status.json`。退出码：
`0=已合并`、`1=失败`、`2=超时`。watcher 不保存 GitHub token。

在 Windows PowerShell 中把含中文的 Python 脚本通过管道交给 `python -`
时，必须先把 `$OutputEncoding` 设为 UTF-8；否则中文会在进入 Python 前被
系统代码页替换为 `?`，即使 GitHub API 请求本身按 UTF-8 发送也无法恢复：

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$code | backend\.venv\Scripts\python.exe -
```

需要临时验证 DeepSeek 对话连通性、但设置界面尚未完成时，可使用安全提示录入 Key：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\set-chat-key.ps1
```

Key 通过隐藏输入写入 Windows Credential Manager，不出现在命令行、仓库文件或日志中。

sidecar 开发日志写入 `backend/.runtime/sidecar.log`，Vite 日志写入 `frontend/.runtime/`；两个运行目录都不进入版本控制。

构建 PyInstaller sidecar 与 release 桌面二进制：

```powershell
corepack pnpm backend:package
corepack pnpm --dir desktop tauri build --no-bundle
```

release sidecar 位于 `desktop/src-tauri/target/release/backend/`，日志写入 Tauri 的应用日志目录。

构建 Windows 安装包：

```powershell
corepack pnpm --dir desktop tauri build
```

NSIS 安装包输出在 `desktop/src-tauri/target/release/bundle/nsis/`。正式公开分发前必须配置代码签名。

## 设计文档

- [设计文档与实现记录索引](docs/README.md)
- [001 · 桌面 sidecar 最小启动链](docs/development/slices/001-desktop-sidecar-bootstrap.md)
- [002 · PyInstaller one-folder sidecar 与 release 冒烟](docs/development/slices/002-packaged-sidecar.md)
- [003 · Windows 安装、首次启动与数据保留](docs/development/slices/003-windows-installer.md)

## 开发原则

先做可运行的最小垂直切片，再按设计扩展；不从旧仓库复制代码或测试；基础件必须人工复核；所有变更必须能通过测试、trace 和评测解释。
