# 007 · collection 快照、manifest 与原子发布

- 状态：已完成
- 开始日期：2026-10-21
- 关联设计：[07 S6 发布](../../design/07-html-ingestion-design.md)、[06 检索侧契约](../../design/06-retrieval-pure-agent.md)、[09 SQLite 与 LanceDB](../../design/09-technology-selection.md)

## 目标

把已完成的单文档 S5 候选索引组合成一份自包含、可验证、不可变的 collection 候选快照，并在 manifest 通过完整性验证后，用单条 SQLite 活动指针原子切换新快照。

本切片只完成数据闭环和内部 API，不让检索工具读取 collection，也不实现正式 UI 或 008 的后台任务系统。

## 输入与输出

输入：

- 一个或多个已完成 S5 的 `job_id`；
- 每个 job 的 `persisted-document.json`、`index.json` 和候选 LanceDB；
- 当前活动 collection 指针。

输出：

- `collections/{snapshot_id}/` 自包含快照目录；
- `CandidateSnapshotManifest` 和 `manifest.json`；
- collection 级合并后的 LanceDB dense／FTS 表；
- `PublishRecord`；
- `ActiveCollectionPointer`；
- 当前 collection 查询接口。

## 不包含

- `search_blocks`、query embedding、RRF 排序和检索工具。
- 正式 UI、后台任务、暂停／取消和重启续跑；这些归 008。
- 旧快照自动清理、保留期管理和 GC。
- 跨应用重启后的发布队列恢复。

## 验收条件

- [x] 从多个已索引 job 构建快照，成功文档全部进入 manifest，失败文档进入 `failed_documents`。
- [x] 每个成功文档复制 source、extracted、section tree、block set、index bundle 和 persisted document 到快照目录，并记录 sha256。
- [x] 成功文档共享同一组 extractor、section builder、chunker、sizing estimator、embedding profile、输入模板和关键词 profile。
- [x] 所有成功文档的 candidate rows 合并为一个 collection LanceDB 表，dense 与 FTS 使用同一组 block ID。
- [x] 快照 `ready`、`ready_partial`、`failed` 状态和 `complete`／`partial` 语义符合设计。
- [x] `manifest_hash` 可重算，快照目录内的 manifest、artifact checksum 和索引行可对账。
- [x] 零成功快照不产生活动指针，并记录 aborted publish record。
- [x] 首个成功发布生成 `generation=1`；后续发布递增 generation 并保留 previous snapshot。
- [x] 多个发布并发时使用 generation CAS，过期发布不能覆盖较新的活动指针。
- [x] 发布失败或 CAS 冲突不能破坏旧活动指针。
- [x] 可从当前 pointer 的 previous snapshot 回滚；回滚验证旧 manifest 后以新 generation 切换。
- [x] 同一 snapshot／manifest 重复发布是幂等的，不生成新的 generation。
- [x] 内部 API 可发布 `job_ids` 并查询当前活动 collection。
- [x] 迁移、PyInstaller 和完整质量门通过。

## 实现决策与二次确认

| ID | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | 快照目录和 ID | 使用 `collections/s_{16hex}` 作为不可变快照目录；staging 使用短 `.b-*` 临时目录，最终原子改名 | 07 要求候选在隔离 workspace 构建，S6 通过后切换 | 使用 job 目录会让快照依赖可清理 job；长 ID 在 Windows 路径上会触发 LanceDB `os error 3` | 快照可独立保留，不依赖原 job artifact | 首次发布、重复发布、长路径测试 | 已确认 |
| D2 | collection 索引存放方式 | 将每个成功文档的 candidate rows 合并到快照内单个 `index/lance` 表；原始 job 候选索引只作为构建输入 | 07 要求 dense／关键词来自同一批 block，检索只读取活动快照 | 保留多张 per-doc 表会让检索需跨表合并，增加一致性和排序复杂度 | 每个快照只有一个 collection 级索引引用 | dense／FTS 行覆盖、block ID 唯一性测试 | 已确认 |
| D3 | 多文档版本兼容边界 | 成功文档必须共享 extractor、section builder、chunker、sizing estimator、embedding profile、输入模板和关键词 profile；不一致的文档进入失败清单 | 07 要求快照 version profile 可追溯且不能被静默混合 | 允许混用会让 manifest 无法准确描述索引语义；逐文档混合索引会需要更复杂 schema | 同一快照保持单一版本组 | 版本不一致 partial 测试 | 已确认 |
| D4 | partial 发布 | 至少一个成功文档且存在失败文档时允许发布 `ready_partial`；零成功文档只生成 failed manifest 和 aborted record，不切换 pointer | 07 明确允许 ready_partial，并要求遗漏透明 | 全部失败仍切换会制造空库；拒绝 partial 会丢了可用的成功文档 | 检索侧后续必须读取 failed_documents | partial API 与零成功 API 测试 | 已确认 |
| D5 | 检索常数的归属 | Snapshot manifest 只记录 `index_schema=lancedb-blocks-v1` 及索引语义参数；Top-K、RRF 常数和候选深度改为查询时 RetrievalProfile／ExecutionPlan，记录在 trace，不进入 snapshot 版本身份 | 05／06 要求参数由评测决定；07 要求索引语义可追溯 | 把查询时参数放入快照会让调参无谓重建 collection，也会把未经评测的 60 冒充最终值 | 调整检索参数不触发重建；索引 schema／embedding／analyzer 变化才重建快照 | manifest 字段、检索参数边界和调参回归测试 | 已确认 |
| D6 | 活动指针存储 | 使用 SQLite 单行 `active_collection_pointer`，以 generation 做 CAS；旧指针不依赖文件覆盖 | 07 要求唯一可见性开关和事务保护；09 指定 SQLite 主数据 | 文件指针难以跨进程原子 CAS；多文件复制无法作为可见性开关 | 新请求只读取一个 pointer | 指针迁移、CAS、旧指针保留测试 | 已确认 |
| D7 | 发布记录与幂等 | 先写 aborted／publish_intent record，再提交指针并写 committed；同一 snapshot＋manifest hash 重复发布不增 generation | 07 要求 intent／committed 记录和崩溃可恢复 | 只写最终记录会在崩溃时丢失意图；每次重复发布都加 generation 会制造无意义版本 | 活动指针仍是唯一事实源 | 发布记录、重复发布和冲突测试 | 已确认 |
| D8 | 发布触发 API | `POST /api/collections/publish` 接收 `job_ids[]`；`GET /api/collections/current` 返回 pointer 与 manifest；新增 rollback API；不接正式 UI | 07 要求 S6 可运行验证，008 再提供后台体验 | 直接在同步 UI 中做全量编排会耦合任务系统 | 可作为 007 内部和人工验证入口 | API 集成测试 | 已确认 |
| D9 | 快照失败清理 | staging 构建失败时删除 staging；已验证并成功 rename 的 snapshot 保留供诊断；失败 snapshot 不产生 pointer | 07 要求失败进入清单且旧库继续服务 | 删除所有失败候选会丢失诊断证据；保留未验证 staging 会被误当快照 | 旧活动库不受失败影响 | staging 清理和无 pointer 测试 | 已确认 |
| D10 | 回滚入口 | 只允许回滚到 current pointer 的 `previous_snapshot_id`；回滚前重新验证旧 manifest／artifacts／索引，再以新 generation CAS 切换；不重跑 embedding | 07 要求回滚只切回仍有效的旧快照 | 任意指定历史 snapshot 需要额外保留期和安全校验；回滚后 previous 记录当前被替换 snapshot | 回滚不改写旧快照，只改变活动指针 | 两次发布后回滚、旧 manifest 校验和 CAS 测试 | 已确认 |

## 验证证据

- 后端完整回归：94 passed；007 新增 10 项，覆盖多文档合并、快照 artifact/checksum、partial、零成功、generation 递增、CAS 冲突、幂等、回滚、staging 清理和篡改拒绝。
- `corepack pnpm quality`：Ruff、Pyright、Vue／TypeScript、Rust fmt／check／clippy、注释检查和后端测试全部通过（实现完成后执行）。
- PyInstaller sidecar 重建成功（1,491 个文件、396.6 MiB），包含 0002 collection 迁移、SQLite pointer／publish record 和 collection 代码。
- 打包 sidecar 健康检查通过，新数据根创建 `active_collection_pointer` 与 `collection_publish_records`。
