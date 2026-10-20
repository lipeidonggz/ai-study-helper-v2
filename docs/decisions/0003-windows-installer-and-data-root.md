# 0003 · Windows 按用户安装与本地数据根

- 状态：已接受
- 决定日期：2026-10-20
- 影响范围：NSIS、WebView2 引导、安装路径、用户数据与升级/卸载

## 背景

002 已证明 release 应用可运行 PyInstaller one-folder sidecar，但尚未验证普通用户如何安装、升级和卸载。桌面程序需要同时满足：

- 普通用户权限可安装；
- 缺少 WebView2 时有明确安装路径；
- 用户数据不写入安装目录；
- 升级不覆盖用户数据；
- 卸载默认保留知识库、配置和日志。

## 决策

首版采用 Tauri NSIS，配置为 `currentUser` 安装。

WebView2 使用 `downloadBootstrapper`：系统缺失运行时时由安装器下载并静默安装引导程序。当前开发机已安装 WebView2，因此本次只能验证“已安装”路径；缺失运行时安装路径必须进入干净 Windows 验收。

应用数据根使用 Tauri `app_local_data_dir()`，在 Windows 上为：

```text
%LOCALAPPDATA%\com.lipeidong.aistudyhelper\
├─ config\
├─ db\
├─ collections\
├─ prompts\experiments\
├─ traces\
├─ logs\
└─ backups\
```

应用启动时创建这些目录；`config/app.json` 只在不存在的首次启动时创建。安装目录只保存程序、sidecar 和静态资源。

## 发布边界

- NSIS 包只面向 Windows x64。
- 安装模式为当前用户，不请求管理员权限。
- 允许安装升级，不允许自动降级。
- 卸载程序只删除安装文件和快捷方式；用户数据需要单独清理。
- 代码签名不属于本地开发构建的通过条件，但正式公开发布必须签名。

## 后果

- 用户数据与安装版本解耦，重装、升级和回滚可以复用数据。
- NSIS 安装测试必须清理安装目录，但不能删除 `%LOCALAPPDATA%\com.lipeidong.aistudyhelper`。
- 数据 schema 迁移必须在后续切片中显式设计，不能复用安装器逻辑。
- 未签名安装包会触发 SmartScreen 警告，因此不能视为正式发布物。

## 验证与推翻条件

003 需验证：

- `currentUser` 安装且无管理员提示；
- 安装后的应用使用 resources sidecar；
- 用户数据位于 LocalAppData；
- 安装、重装/升级和卸载默认保留用户数据；
- 卸载后快捷方式和安装目录被清理；
- 已安装 WebView2 的机器可直接启动。

如果 NSIS 无法满足按用户安装、升级保留数据或 WebView2 引导约束，再评估 MSI 或自定义安装器。
