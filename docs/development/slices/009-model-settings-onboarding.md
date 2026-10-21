# 009 · 用户模型设置与 embedding Key 连通性

- 状态：已完成
- 开始日期：2026-10-21
- 关联设计：[09 凭证与首启](../../design/09-technology-selection.md)、[06 检索侧模型边界](../../design/06-retrieval-pure-agent.md)
- 前置切片：[008 后台入库任务（已完成）](../slices/008-background-ingestion-orchestration.md)
- 人工验收：[009 模型设置人工验收](../manual-tests/009-model-settings.md)

## 目标

为普通用户提供 embedding API Key 的安全配置入口：Key 写入 Windows Credential Manager，设置页只显示配置状态，并能用已保存 Key 做一次真实短文本 embedding 连通性测试。

## 输入与输出

输入：

- 用户输入的 embedding API Key；
- 当前 Windows Credential Manager 状态；
- Qwen embedding provider 配置。

输出：

- `provider_settings` 非敏感状态记录；
- embedding 配置状态、模型 ID、维度、最近测试结果；
- 保存、删除、测试 API；
- 前端模型设置卡片。

## 不包含

- 对话 DeepSeek Key 的配置和连通性测试；当前只预留独立 `chat-api-key` 状态。
- 检索工具、搜索页面和 Agent 对话。
- Key 明文回显、导出、日志、trace 或数据库存储。
- 自动轮询云端连通性。

## 验收条件

- [x] 设置页只显示 embedding 是否配置，不显示 Key 或 Key 前后缀。
- [x] 保存 Key 后进入 Credential Manager，`provider_settings` 不包含 Key。
- [x] 保存新 Key 会清除旧 Key 的连通性结果。
- [x] 删除 Key 会清除配置状态和最近测试结果。
- [x] 无 Key 测试返回稳定的 `embedding_api_key_missing`。
- [x] 连通性测试使用已保存 Key 调用 Qwen，校验 1024 维非零向量并记录 request ID、token usage 和时间。
- [x] 连通性失败保留失败类别和消息，不把失败伪装成成功。
- [x] 前端设置卡片支持保存和测试，输入成功后清空。
- [x] 迁移、Vue 类型检查、完整质量门和 sidecar 打包通过。

## 实现决策与二次确认

| ID | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | Key 的存储位置 | 复用 `ai-study-helper` service 和 `embedding-api-key` username；Key 只经 Windows Credential Manager 存取 | 09 要求两个 Key 独立保存且不进入配置、日志、备份 | 环境变量或设置文件会泄漏；改用自定义加密文件会增加密钥管理风险 | 设置状态与 Key 明文分离 | 内存 CredentialStore、状态与测试 API | 已确认 |
| D2 | 非敏感设置状态 | 新增 SQLite `provider_settings`，只存 provider、model ID、dimension、最近测试状态、request ID、token usage 和时间 | 09 要求状态页和连通性可观察；09 又禁止保存 Key | 只存内存会导致重启后测试结果丢失；把 Key 存入表违反安全边界 | 重启后仍能展示最近测试 | 0004 migration 与设置 API 测试 | 已确认 |
| D3 | Key 更新语义 | 保存新 Key 或删除 Key 时清除旧测试结果；测试只读取当前已保存 Key | 避免用户误把旧 Key 的结果看成新 Key 的结果 | 保留旧结果可能造成误导；自动测试会额外产生费用 | Key 更换后必须重新测试 | 保存/测试/删除状态测试 | 已确认 |
| D4 | 连通性测试方式 | 使用 `qwen3.7-text-embedding` 单条短文本、document 类型、1024 维 dense；校验响应数量、维度、有限非零值；测试只读取已保存 Key | 006 已确认真实模型契约 | 只检查 Key 是否存在无法发现鉴权或模型问题；重复大批测试会增加费用 | 一次最小真实请求即可验证协议 | fake provider 与真实 Key 人工测试 | 已确认 |
| D5 | 设置页入口 | 在当前单页顶部展示模型设置卡片，不引入独立设置路由或复杂向导 | 首版只有 embedding Key 一个阻塞项；普通用户需要可见入口 | 独立页面增加导航和状态同步面；只做 CLI 无法完成用户流程 | 用户在导入前能看到 Key 状态 | Vue 类型检查与人工界面验收 | 已确认 |
| D6 | 对话 Key 范围 | 009 只提供 embedding Key 配置/测试；`chat-api-key` 仅保留独立 username 和状态占位，不等同于已实现对话配置 | 当前对话模型和 Agent 尚未进入实现 | 同时实现 DeepSeek 测试会扩大切片；忽略独立 Key 会违背未来架构 | 先解除入库阻塞，对话接入时复用同一 CredentialStore | 设置响应 chat_configured 与后续切片 | 已确认 |

## 验证证据

- 后端定向设置 API：2 passed。
- Vue TypeScript 检查和前端生产构建：通过。
- 完整质量门：101 passed。
- PyInstaller sidecar 重建成功（1,495 个文件、396.6 MiB），启动、0004 migration 和 `provider_settings` schema 冒烟通过。
- 真实 Key 的模型连通性已在 006 验证；设置页保存、真实连通性测试、删除、重新保存和重启恢复均完成人工验收。
