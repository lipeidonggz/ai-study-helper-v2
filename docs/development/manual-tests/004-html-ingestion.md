# 004 · HTML 导入人工验收

- 状态：已完成（2026-10-20）
- 对应切片：[004 HTML 导入与结构预览](../slices/004-html-ingestion-preview.md)
- 目的：验证真实 Tauri 界面、原生文件选择、本地 HTML 多样性和公开 URL 主流程

## 启动

在仓库根目录执行：

```powershell
corepack pnpm dev
```

等待右上状态显示“运行正常”后开始。

## 主流程

按顺序执行以下步骤。每一步都记录结果，不因前一项通过而跳过后续类型。

### 1. 复杂本地文章

- 来源：本地文件
- 文件：`backend/tests/fixtures/complex-article.html`
- 点击“解析文档结构”
- 预期：
  - 标题为“复杂 HTML 学习资料”
  - 来源为“本地文件”
  - 章节数 6，Blocks 数 7
  - 章节包含“学习目标 / 具体范围 / 示例代码 / 对照表格 / 边界情况 / 边界情况”
  - 同名章节有不同的展示序号
  - 页面可见警告说明前置正文归属和同名章节消歧
  - 列表保留嵌套；代码保留换行缩进；表格保留行列；图片说明保留
  - 图片文件和纯视觉内容不显示；只验证 alt/caption 文本，后续能力见 BL-0001
  - 页头、导航、侧栏和页脚内容不进入 blocks

### 2. 无标题结构

- 文件：`backend/tests/fixtures/no-headings.html`
- 预期：
  - 标题为“Untitled notes”
  - 章节数 1
  - Blocks 数 1
  - 文档不生成虚构章节标题

### 3. 中文和空格路径

- 文件：`backend/tests/fixtures/复杂 路径/带 空格的文档.html`
- 预期：
  - 原生文件选择器可以打开该路径
  - 标题为“中文与空格路径”
  - 章节数 1，Blocks 数 1

### 4. JavaScript 空壳失败

- 文件：`backend/tests/fixtures/javascript-shell.html`
- 预期：
  - 不生成预览
  - 明确显示 `javascript_rendering_unsupported`

### 5. 登录页面失败

- 文件：`backend/tests/fixtures/login.html`
- 预期：
  - 不生成预览
  - 明确显示 `login_required`

### 6. 空正文失败

- 文件：`backend/tests/fixtures/empty-content.html`
- 预期：
  - 不生成预览
  - 明确显示 `no_extractable_content`

### 7. 公开静态 HTML URL

- 切换来源为“网页 URL”
- 输入：`https://docs.python.org/3/tutorial/controlflow.html`
- 预期：
  - 来源显示站点域名或站点名
  - 标题包含“More Control Flow Tools”
  - 生成多个章节和 blocks
  - block 正文包含 Python 教程内容

### 8. URL 非 HTML

- 输入：`https://www.python.org/static/img/python-logo.png`
- 预期：不生成预览，显示 `unsupported_media_type`

### 9. URL 私网地址

- 输入：`http://127.0.0.1:8421/private`
- 预期：不发请求，显示 `ssrf_blocked`

### 10. 非法 URL 协议

- 输入：`file:///C:/temp/test.html`
- 预期：显示 `invalid_url`

### 11. 退出清理

- 关闭应用窗口
- 预期：
  - 不再出现文件选择框
  - 任务管理器没有本次 Python sidecar
  - 1420 端口不再监听

## 测试记录

| 编号 | 结果（通过/失败） | 实际现象或错误文本 | 截图/备注 |
| --- | --- | --- | --- |
| 1 复杂本地文章 | 通过 | 6 章节、7 blocks；列表、代码、表格、alt/caption 正确 | 人工截图 |
| 2 无标题结构 | 通过 | 1 个根章节、1 个 block，无虚构标题 | 人工截图 |
| 3 中文空格路径 | 通过 | 标题、语言、章节和 block 正常 | 人工截图 |
| 4 JS 空壳失败 | 通过 | `javascript_rendering_unsupported`；提示随后改为中文 | 人工截图 |
| 5 登录页失败 | 通过 | `login_required`，中文提示正确 | 人工截图 |
| 6 空正文失败 | 通过 | `no_extractable_content`，中文提示正确 | 人工截图 |
| 7 URL 成功 | 通过 | Python 文档标题、章节树和多段代码 blocks 正常；无需逐 block 截图 | 人工截图 |
| 8 URL 非 HTML | 通过 | `unsupported_media_type`，中文提示正确 | 人工截图 |
| 9 URL 私网阻断 | 通过 | `ssrf_blocked`，未发起私网请求 | 人工截图 |
| 10 非法协议 | 通过 | `invalid_url`，中文提示正确 | 人工截图 |
| 11 退出清理 | 通过 | 应用、sidecar、Vite 和文件选择框残留数均为 0 | 进程/端口检查 |

## 判定

全部通过后，004 才能进入“人工验收完成、等待自行决策二次确认”。任一失败都必须保留实际错误文本和截图，先定位是否属于代码缺陷、fixture 问题或外部 URL 环境问题。
