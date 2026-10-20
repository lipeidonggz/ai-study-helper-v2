# 002 · PyInstaller one-folder sidecar 与 release 冒烟

- 状态：已完成
- 开始日期：2026-10-20
- 关联设计：[09 技术选型](../../design/09-technology-selection.md)
- 关联决策：[0002 使用 PyInstaller one-folder 分发后端](../../decisions/0002-package-backend-as-pyinstaller-directory.md)

## 目标

让 release 桌面应用使用随应用分发的 PyInstaller one-folder 后端，而不是开发机的 Python 虚拟环境。

## 范围

本切片包含：

- PyInstaller 构建脚本与 spec。
- Tauri resources 配置。
- debug 与 release 后端路径分流。
- release 应用、sidecar 日志路径和失败语义。
- one-folder 体积与冷启动测量。

本切片不包含：

- NSIS 安装器、签名、更新和卸载。
- HTML 入库、检索、模型或 Agent 功能。

## 验收条件

- [x] 后端构建脚本可从头生成完整的 one-folder 目录。
- [x] 构建脚本会清理旧产物，不混入历史文件。
- [x] debug 模式继续使用 `backend/.venv`。
- [x] release 模式只启动随包 sidecar，不回退到系统 Python。
- [x] release 模式健康检查正常，受保护接口无令牌返回 401。
- [x] release sidecar 异常退出保持有限重启语义。
- [x] release 应用退出后 sidecar 进程树和端口无残留。
- [x] 临时移走开发 venv 后，release 应用仍能运行。
- [x] 记录 one-folder 体积、应用冷启动和 sidecar 就绪时间。

## 验证证据

后端打包：

- PyInstaller 6.22.3 / Python 3.13.15。
- one-folder 产物：707 个文件，约 33.4 MB。
- 打包 exe 独立启动：`/health` 返回 200，无令牌访问 `/api/runtime` 返回 401。
- 单独启动打包 exe 的就绪时间约 1.1 秒。
- 构建脚本连续执行两次，均正确清理并重建产物。

调试与发布分流：

- debug 应用实际启动：
  `backend/.venv/Scripts/python.exe -m app.main --port ...`。
- release 应用实际启动随包：
  `target/release/backend/ai-study-helper-backend.exe --port ...`。
- release sidecar 日志写入
  `%LOCALAPPDATA%/com.lipeidong.aistudyhelper/logs/sidecar.log`，未记录启动令牌。

release 冒烟：

- 临时移走 `backend/.venv` 后，release 应用仍正常启动打包 sidecar。
- 两次 release 启动观测：窗口约 0.3 秒可见，packaged sidecar 约 1.9–3.1 秒健康。
- 连续终止 release sidecar 三次：前两次自动重启成功，第三次停止恢复。
- 关闭 release 应用后：应用、sidecar 和监听端口均为 0。

## 结论与主线

PyInstaller one-folder + Tauri resources 的发布路径成立，release 后端不再依赖开发机的 Python 或源码目录。

```text
PyInstaller one-folder
→ Tauri resources
→ release resource_dir/backend
→ 桌面壳直接启动 packaged exe
→ Job Object 管理进程树
→ app_log_dir 保存运行日志
```

本切片没有解决安装、升级、卸载和 WebView2 首次引导。因此下一阶段应验证 NSIS 安装链，再进入真实 HTML 数据切片。
