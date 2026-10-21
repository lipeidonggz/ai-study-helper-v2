# 008 · 后台入库任务编排

- 状态：已完成
- 开始日期：2026-10-21
- 关联设计：[07 S0–S6 与可恢复性](../../design/07-html-ingestion-design.md)、[09 后台任务](../../design/09-technology-selection.md)
- 前置切片：[007 collection 快照与原子发布（已完成）](../slices/007-collection-snapshot-publish.md)

## 目标

把现有 S0–S6 服务编排成可持久查询的后台入库任务：提交若干本地文件或 URL 后立即返回 `task_id`，由单写者 worker 串行执行，提供进度、暂停、恢复、取消和进程中断可见性。

## 输入与输出

输入：

- `sources[]`：每个来源包含 `source_kind=file|url` 和 `source_value`；
- 当前 S0–S6 服务和 fake／真实 embedding provider。

输出：

- SQLite `ingestion_tasks` 持久记录；
- 每个来源的 preview job、正式文档版本、候选索引和最终 collection snapshot；
- `task_id`、阶段、完成数、失败数、失败清单和最终 `snapshot_id`；
- 暂停、恢复、取消和任务查询 API。

## 不包含

- 正式前端任务中心、首启向导和 API Key 设置 UI；Key 设置链另立用户设置切片。
- Redis、Celery 或其他外部任务队列。
- 自动重发“请求结果未知”的云端 embedding 调用。
- collection 检索、评测台和旧任务清理策略。

## 验收条件

- [x] `POST /api/tasks/ingest` 立即返回 queued task，不阻塞到所有 embedding 完成。
- [x] worker 按来源顺序执行 preview→persist→index，全部成功后发布一次 collection snapshot。
- [x] 任务状态持久化为 queued／running／paused／succeeded／failed／canceled／interrupted。
- [x] 暂停在阶段边界生效；恢复从 `next_source_index` 继续；取消后不再启动新阶段或新来源。
- [x] 单个来源失败不丢弃其他成功来源；有成功文档时可发布 partial collection，并记录失败清单。
- [x] 零成功来源时任务失败，不产生活动 pointer。
- [x] 进程重启后遗留 running 任务标为 interrupted，需用户显式 resume，不自动重发未知云端请求。
- [x] `GET /api/tasks/{task_id}` 可观察阶段、进度、失败和 snapshot ID。
- [x] pause／resume／cancel API 对非法状态返回稳定错误码。
- [x] 迁移、完整质量门和 PyInstaller sidecar 启动冒烟通过。

## 实现决策与二次确认

| ID | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | 后台任务持久状态 | 新增 SQLite `ingestion_tasks` 表，保存来源、状态、阶段、next source、成功 job IDs、失败清单和 snapshot ID | 09 指定 SQLite 任务表；07 要求阶段进度、暂停续跑和状态可见 | 只用内存任务会在重启后丢事实；使用外部队列违背单机安装边界 | 任务状态可跨 API 请求和进程重启查询 | migration、任务 CRUD 和状态转换测试 | 已确认 |
| D2 | worker 并发度 | 008 使用单个进程内 worker，任务之间串行；S5 内部仍允许 3 个 embedding 批次在途 | 07 要求单机个人版优先可靠；09 指定进程内 worker | 多任务并发会增加同一 SQLite／LanceDB／供应商限流竞争 | 任务顺序可预测，状态和总成本更容易对账 | 多任务状态和 worker 生命周期测试 | 已确认 |
| D3 | 来源失败与 partial | 单来源失败记录后继续下一个来源；至少一个成功时自动调用 007 发布 `ready_partial`，零成功则任务 failed 且无 pointer | 07 允许 partial，并要求遗漏透明 | fail-fast 会丢弃已成功文档；全部成功才发布会让长批次收益变差 | 用户能看到哪些来源失败，成功文档仍可服务 | 单来源失败、全失败测试 | 已确认 |
| D4 | 暂停／取消边界 | pause 在阶段 checkpoint 等待；cancel 在 checkpoint 退出；正在发出的 HTTP／embedding 请求不会被强制伪装中断 | 07 要求可暂停／取消且不能丢失真实 usage | 强制 kill 云端请求会造成未知计费；只在阶段边界协作式停止 | 取消后可能等待当前在途请求结束 | pause/resume/cancel API 测试 | 已确认 |
| D5 | 进程重启恢复 | 启动时把遗留 running 任务标记 interrupted，保留 next source index 和已完成成功 job IDs；不自动 resume，不自动重发未知请求 | 09 明确崩溃恢复检查且不自动重发结果未知请求 | 自动 resume 会产生不可控重复计费；丢弃状态会让用户无法继续 | 用户可看到中断点并显式恢复 | interrupted 状态和 resume 测试 | 已确认 |
| D6 | 自动发布边界 | 任务所有来源处理完后自动调用 007 发布一次 collection；不在每个来源后发布多个中间 snapshot | 07 要求校验通过后发布完整候选；008 不增加用户手动发布步骤 | 每篇发布会产生大量无意义 pointer 和旧快照；永不自动发布会让任务失去闭环 | 任务成功通常对应一个 collection generation | 单/多渠道任务和 pointer 测试 | 已确认 |
| D7 | Key 设置链位置 | 008 只调用既有 Credential Manager provider；首启 Key 输入、连通性测试和状态页另立用户设置切片 | 09 要求两个 Key 独立设置且由非技术用户操作；任务系统不应承担设置 UI | 把设置 UI 混入任务运行时会扩大切片并耦合界面 | 008 可在 fake provider 和已配置 Key 下运行 | fake provider 任务测试 | 已确认 |

## 验证证据

- 后端完整回归：99 passed。
- 008 定向测试覆盖后台成功、部分来源失败、暂停恢复、取消和进程重启后的 interrupted→resume。
- `corepack pnpm quality` 通过（实现完成后执行）。
- PyInstaller sidecar 重建成功（1,493 个文件、396.6 MiB），并完成启动／0003 migration 冒烟。

## 后续

- 用户设置切片：两个 Key 的安全配置、连通性测试和设置状态。
- 任务清理、旧快照保留和前端任务中心。
