# 003 · Windows 安装、首次启动与数据保留

- 状态：已完成
- 开始日期：2026-10-20
- 关联设计：[09 技术选型](../../design/09-technology-selection.md)
- 关联决策：[0003 Windows 按用户安装与本地数据根](../../decisions/0003-windows-installer-and-data-root.md)

## 目标

生成可安装的 Windows x64 NSIS 包，并验证安装、首次启动、随包 sidecar、用户数据保留、升级/重装和卸载行为。

## 范围

本切片包含：

- Tauri NSIS `currentUser` 安装。
- WebView2 `downloadBootstrapper` 配置。
- 统一的 LocalAppData 数据根和首次目录初始化。
- 安装包构建、静默安装、应用启动、静默卸载。
- 重装/升级场景下的数据保留验证。

本切片不包含：

- 代码签名证书和正式公开分发。
- 自动更新器。
- HTML 入库、检索、模型或 Agent 功能。

## 验收条件

- [x] 只生成 NSIS 安装包，不生成 MSI。
- [x] NSIS 使用 `currentUser` 安装和中文/英文语言。
- [x] WebView2 使用 `downloadBootstrapper`。
- [x] 安装包包含打包 sidecar 和静态前端资源。
- [x] 安装后的应用可以启动并运行随包 sidecar。
- [x] 用户数据根位于 `%LOCALAPPDATA%`，不在安装目录。
- [x] 首次启动创建配置、数据库、collection、prompt、trace、日志和备份目录。
- [x] 重装或升级后已有用户数据保持不变。
- [x] 静默卸载后安装目录和快捷方式被清理。
- [x] 卸载默认保留用户数据。
- [x] 记录安装包大小和首次启动耗时。
- [x] 明确记录 WebView2 缺失路径与代码签名尚未验证的限制。

## 验证证据

安装包：

- NSIS 输出：`ai-study-helper_0.1.0_x64-setup.exe`，18,033,193 bytes（17.20 MiB）。
- 生成脚本确认：`RequestExecutionLevel user`、`INSTALLMODE currentUser`。
- 生成脚本确认：`downloadBootstrapper`，WebView2 安装参数为 `/silent`。
- 语言包含 `SimpChinese` 和 `English`。
- 未生成 MSI。

安装与首次启动：

- 静默安装退出码为 0。
- 安装目录为 `%LOCALAPPDATA%\ai-study-helper`。
- 开始菜单快捷方式创建成功。
- 安装目录包含主程序、VC Runtime 和完整 `backend/` sidecar。
- 首次启动成功，packaged sidecar 约 2.6 秒健康。
- 首次启动创建 `config / db / collections / prompts/experiments / traces / logs / backups`。
- `config/app.json` 首次创建为 schema version 1。
- 关闭应用后 sidecar 和监听端口均无残留。

重装、升级与降级：

- 同版本重装退出码为 0，用户 marker 保持不变。
- 使用临时 `0.1.1` 配置构建升级包并覆盖安装，注册版本更新为 `0.1.1`，用户 marker 保持不变。
- 升级后的应用正常启动 packaged sidecar。
- 尝试用 `0.1.0` 覆盖 `0.1.1` 时退出码为 2，注册版本仍为 `0.1.1`，降级被阻止。

卸载：

- 静默卸载退出码为 0。
- 安装目录、卸载注册项和开始菜单快捷方式均被清理。
- 用户数据根仍存在，`config/retention-marker.txt` 内容保持不变。

## 限制

- 当前机器已安装 WebView2，因此只验证了“运行时已存在”路径；缺失运行时时的下载与安装必须进入干净 Windows 验收。
- 安装包未签名，`Get-AuthenticodeSignature` 结果为 `NotSigned`。正式公开分发前必须配置代码签名。
- 本机访问 GitHub release asset 超时，构建时通过 `TAURI_BUNDLER_TOOLS_GITHUB_MIRROR=https://gh-proxy.com/https://github.com` 获取 NSIS 工具；Tauri 对下载包执行了内置 hash 校验。该镜像只用于本次受网络限制的构建，不写入项目配置。

## 结论与主线

Windows 按用户安装链成立，普通用户侧只需要安装一个 NSIS 包：

```text
NSIS currentUser
→ LocalAppData 安装目录
→ WebView2 可用
→ Tauri release 应用
→ 随包 PyInstaller sidecar
→ LocalAppData 独立数据根
```

安装版本与用户数据已经解耦，重装、升级和卸载都不会默认覆盖知识库或配置。003 可以作为后续产品功能切片的分发基础；正式发布前仍需完成签名和缺失 WebView2 的干净机验收。
