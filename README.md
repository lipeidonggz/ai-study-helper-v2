# ai-study-helper v2

一个面向普通 Windows 用户的本地文档学习问答 Agent。用户可以导入自己的 HTML 文档，基于文档与模型内置知识提问；系统需要给出可核对、可复现、可评测的回答。

## 当前状态

产品需求、用例、架构、检索、入库、评测与技术选型已经完成。当前已实现第一个垂直切片：Tauri 启动并监管 FastAPI sidecar，完成本地鉴权、状态展示、有限重启和退出清理。

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
backend\.venv\Scripts\python.exe -m pip install -e "backend[dev]"
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

sidecar 开发日志写入 `backend/.runtime/sidecar.log`，Vite 日志写入 `frontend/.runtime/`；两个运行目录都不进入版本控制。

## 设计文档

- [设计文档与实现记录索引](docs/README.md)
- [001 · 桌面 sidecar 最小启动链](docs/development/slices/001-desktop-sidecar-bootstrap.md)

## 开发原则

先做可运行的最小垂直切片，再按设计扩展；不从旧仓库复制代码或测试；基础件必须人工复核；所有变更必须能通过测试、trace 和评测解释。
