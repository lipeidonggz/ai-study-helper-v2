# 0002 · 使用 PyInstaller one-folder 分发后端

- 状态：已接受
- 决定日期：2026-10-20
- 影响范围：Python sidecar 构建、Tauri resources、发布路径与本地日志

## 背景

001 已证明 Tauri、Vue 与 FastAPI sidecar 可以在开发环境组成完整运行链。但开发模式直接启动 `backend/.venv/Scripts/python.exe`，普通用户机器不会安装项目源码、Python 或虚拟环境，因此不能作为产品分发方式。

需要在以下方案中选择一个可验证的首版后端分发形态：

- Python venv 由用户自行安装；
- PyInstaller one-file；
- PyInstaller one-folder；
- Tauri externalBin 单文件 sidecar。

## 决策

首版采用 **PyInstaller one-folder**，通过 Tauri `bundle.resources` 分发完整后端目录。

构建产物写入：

```text
desktop/src-tauri/resources/backend/
├─ ai-study-helper-backend.exe
└─ _internal/
```

该目录属于生成物，不进入 Git。

运行时按构建模式选择 sidecar：

- debug：启动 `backend/.venv/Scripts/python.exe -m app.main`；
- release：只启动 `resource_dir/backend/ai-study-helper-backend.exe`；
- release 找不到打包 sidecar 时明确失败，不回退到系统 Python。

sidecar 日志：

- debug：`backend/.runtime/sidecar.log`；
- release：Tauri `app_log_dir()/sidecar.log`。

## 候选与取舍

### PyInstaller one-folder

采用。依赖文件保持展开，启动快、构建和排障直观，原生库路径比 one-file 稳定。代价是发行包包含多个文件，需要连同整个目录分发。

### PyInstaller one-file

暂不采用。每次启动需要解压到临时目录，冷启动更慢，原生库和杀毒软件行为更复杂。

### Tauri externalBin

暂不采用。Tauri sidecar 机制更适合单个可执行文件；one-folder 需要同时携带 `_internal` 与资源，因此使用普通 resources 目录并直接启动 exe 更清楚。

### 要求用户安装 Python

不采用。它直接违反“普通非技术用户只安装一个应用”的产品约束。

## 后果

- release 构建前必须先运行后端打包脚本。
- 构建脚本必须清理旧目录，避免把上一次构建的残留文件带入发行包。
- release 启动逻辑不能依赖源码路径或开发 venv。
- Tauri Job Object 继续管理打包 sidecar 的进程树。
- one-folder 的体积、冷启动和杀毒软件兼容性需要实测记录。

## 验证与推翻条件

002 切片至少验证：

- 开发模式继续使用 venv；
- release 模式使用随包 sidecar；
- 打包 sidecar 可完成鉴权、健康检查、自动重启和退出清理；
- 临时移走开发 venv 后，release 应用仍能正常运行。

如果 one-folder 在 Windows 上出现无法定位原生库、启动明显过慢、更新残留严重或杀毒软件高误报，则重新评估 one-file、安装器侧额外安装或其他分发方式。
