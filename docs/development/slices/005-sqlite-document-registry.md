# 005 · SQLite 文档身份与版本登记

- 状态：已完成
- 开始日期：2026-10-21
- 关联设计：[07 S2 建档](../../design/07-html-ingestion-design.md)、[09 SQLite 与迁移](../../design/09-technology-selection.md)
- 人工验收：[005 文档保存人工验收](../manual-tests/005-document-persistence.md)

## 目标

在 004 已完成 HTML 预览的基础上增加正式文档持久化：把预览 job 中同一份 `SourceArtifact` 与 `ExtractedDocument` 登记到 SQLite，按来源分配稳定 `doc_id`，按提取内容 hash 登记不可变 `doc_version`，再用正式身份重新生成章节树和 block。

## 输入与输出

输入：

- 004 预览产生的 `source.json`、`extracted.json` 和原始 HTML artifact；
- 用户的“保存到知识库”动作。

输出：

- `documents`、`document_versions`、`source_artifacts` 和 `ingestion_jobs` 持久记录；
- 正式 `doc_id`／`doc_version`；
- 使用正式身份生成的 SectionTree、BlockSet 和 job artifact；
- 前端保存结果：新文档、新版本或已有版本。

## 不包含

- Qwen embedding、LanceDB dense／关键词索引。
- CandidateSnapshotManifest、活动 collection 指针和 S6 发布。
- 文档列表、删除、人工改标题和跨来源合并。
- 文件移动后的重新关联。

因此本切片只完成 S2 与正式 S3／S4 身份接入，不让文档进入可检索 collection。

## 验收条件

- [x] 应用启动时自动把 SQLite schema 升级到 Alembic head。
- [x] 首次保存同一 `source_key` 创建 `doc_id` 和 `doc_version=1`。
- [x] 同来源、同 `extraction_hash` 重复保存不新增文档或版本。
- [x] 同来源正文变化时保持 `doc_id`，递增 `doc_version`。
- [x] 不同 `source_key` 创建不同 `doc_id`。
- [x] 保存时复用预览 job artifact，不重新抓取 URL 或重新读取本地文件。
- [x] SectionTree／BlockSet 携带正式 `doc_id` 和 `doc_version`。
- [x] 正式 block ID 包含文档、版本、chunker 版本和块位置。
- [x] 文档版本能回指 extracted、section-tree 和 block-set artifact。
- [x] SQLite 写入失败不会产生“保存成功”响应，job 记录失败状态和错误码。
- [x] schema 需要迁移时先创建一致性备份，失败自动恢复，最多保留 3 份。
- [x] 预览与正式生成结果的版本、tree hash 和 block set hash 一致后才保存。
- [x] 已有派生 artifact 缺失时显式失败，空 ref 可安全补齐。
- [x] 打包 sidecar 包含 Alembic migrations，并完成真实保存冒烟。

## 实现决策与二次确认

| ID | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | 主数据库目录与文件名 | `data_root/db/application.db` | 06／09 已规定数据根下使用 `db/` 保存核心数据库 | 与配置目录分文件；改用每个 collection 一个 DB 会增加跨库事务 | 后续备份和迁移路径固定 | 迁移与集成测试 | 已确认 |
| D2 | 新文档 `doc_id` 格式 | `doc_` + UUID4 十六进制 | 07 要求首次建档分配并持久化，不由 URL／标题／hash 拼接；06 将 doc_id 用作检索排序的最终平局键 | 使用 source hash 更可读，但会暴露来源且难以彻底避免碰撞；UUID7 会引入不必要的时间语义 | 对外仍把 doc_id 当不透明身份；持久化后作为稳定字符串参与平局排序 | 首次／重复／不同来源测试 | 已确认 |
| D3 | schema 升级时机与迁移保护 | sidecar 启动时检查 revision；需要迁移时先通过 SQLite backup API 创建一致性备份，最多保留 3 份，再执行 Alembic upgrade head；失败时恢复迁移前备份并终止启动。新库和已在 head 时不备份、不清理 | 09 指定 Alembic；06 明确要求迁移前备份、失败恢复和可逆升级；沛东确认保留最多 3 份 | 手工迁移不适合普通用户；首次保存时迁移暴露失败太晚；直接执行 upgrade 缺少数据保护 | 增加 schema 变化时的启动耗时；防止迁移失败损坏唯一主库 | 新建库、已最新、旧版本、迁移失败恢复、备份保留与精确清理测试 | 已确认 |
| D4 | 保存的数据来源 | 从既有预览 job 的 `source.json`／`extracted.json` 复用，不重新抓取 | 07 要求同 job 重试优先复用已登记结果；沛东确认保存必须保证所见即所得 | 重新读取文件或 URL 会破坏预览与保存一致性，并产生 TOCTOU 和二次网络成本 | artifact 丢失时明确要求重新解析，不静默读取新内容 | API 集成测试 | 已确认 |
| D5 | 任务状态粒度 | 本切片记录 `status=running/succeeded/failed` 与 `stage=S2/S4`，不引入异步队列；进入正式 S3／S4 前推进 stage，失败时保留最后 stage 并另写 error_code | 07 要求 job 可诊断；完整暂停续跑属于后续任务系统；沛东已确认该粒度 | 只写 artifact 无法查询失败；内存状态重启即丢；完整 worker 超出范围；`stage=failed` 会混淆状态与阶段 | 保存失败可定位到具体阶段 | SQLite job 行及 S2／S4 失败测试 | 已确认 |
| D6 | 别名、警告与结构化行 | 文档别名用 JSON 列；提取节点和块正文暂存 job artifact，不拆成 SQLite 子表；被版本引用的 artifact 必须保留到 S6 正式落库 | 当前只需身份／版本查询；块正文将在 S6 发布事务中落库；沛东已确认该分层 | 提前规范化全部节点会增加 schema 面且无当前查询消费者 | 不影响后续 manifest 对账，但暂不能按 block 查询；清理策略必须识别版本引用 | artifact ref、版本引用与清理保护测试 | 已确认 |
| D7 | 正式 block ID | `{doc_id}#v{doc_version:04d}#c{chunker_version}#b{block_index:05d}` | 07 要求由文档、版本、chunker 版本和位置确定性生成；沛东已确认 | 纯 hash 在重复正文处冲突；短 ID 可读但缺版本 | 可用于引用、精确扫描和平局排序；estimator 或切块配置变化必须提升 chunker 版本 | 正式 ID 断言 | 已确认 |
| D8 | 相同 hash 但派生 artifact 缺失或损坏 | 保留原 doc_version；仅为空 ref 补写 section-tree／block-set 并记录 `derived_artifacts_repaired`；已有 ref 必须验证文件存在、可解析且 doc_id／doc_version 一致，损坏时返回 `document_version_artifact_invalid`，绝不静默覆盖 | 07 要求同 job 重试可续跑、历史版本不可覆盖；沛东已确认补全版 | 总是创建新版本破坏幂等；总是覆盖 ref 会改变历史；只查 null 会留下悬空引用 | 支持中断恢复，同时保护历史引用并显式暴露修复 | 重复保存、空 ref 补齐、文件缺失、JSON 损坏和身份不匹配测试 | 已确认 |
| D9 | 预览与保存一致性屏障 | 保存前读取预览 SectionTree／BlockSet，比较 extractor／section builder／chunker／estimator 版本、`extraction_hash`、完整 `tree_hash` 与排除文档身份和 block ID 的 `block_set_hash`；不一致返回 `preview_consistency_failed`，job 失败且不附加派生 ref | 07 要求证据完整、版本可追溯且不能静默降级；沛东已确认采用一致性屏障 | 仅依赖确定性实现和测试属于隐式保证，无法防御代码升级、artifact 损坏或遗漏修改 | 防止用户保存到一份与刚才预览不同的文档；需要扩展 hash 口径并增加对账成本 | 正常一致、篡改 section tree、篡改 block set、版本错配失败测试 | 已确认 |

## 验证证据

- 后端完整回归：54 passed（截至当前所有后端测试的累计口径；005 新增 15 项）。
- 覆盖首次保存、重复保存、内容变化递增版本、不同来源新建文档、迁移备份／恢复／保留、S2 写失败、artifact 修复／损坏和预览一致性屏障。
- 迁移备份额外覆盖 WAL 中未 checkpoint 数据，以及旧备份删除失败不阻断迁移。
- Ruff format／lint 与 Pyright：通过。
- 前端类型检查与生产构建：通过。
- Rust check／fmt／clippy：通过。
- PyInstaller sidecar：747 个文件、46.79 MiB，包含 2 个 Alembic migration 关键文件。
- Tauri release 与 NSIS：通过，安装包 24.12 MiB。
- 打包 sidecar 在新数据根自动创建 `alembic_version`、`source_artifacts`、`documents`、`document_versions`、`ingestion_jobs`。
- 首次打包冒烟：创建 `doc_56862acfa2bc4707aea1c3970f9a4197`／版本 1。
- 重启后重复保存：保持同一 `doc_id`／版本 1，`is_new_document=false`、`is_new_version=false`。
- 最终打包冒烟：预览与正式 `tree_hash` 均为 `5117eca2972b…`，`block_set_hash` 均为 `27d81618fe16…`。
- 真实桌面首次保存：生成 `doc_94ecb9234d5f4baa89ee02691b75f5e4`，界面显示版本 1／新文档；用户截图确认。
- 真实桌面重启后重复保存：保持同一 `doc_id`／版本 1，界面显示“已有版本”；用户截图确认。
- 真实桌面重新解析后重复保存：保持同一 `doc_id`／版本 1，界面显示“已有版本”；用户截图确认。
- 真实桌面退出清理：应用与 sidecar 进程为 0，1420 端口不再监听。

## 开发期兼容说明

本轮按 D9 将 SectionTree builder 从 v1 升到 v2，并为 BlockSet 增加 `block_set_hash`。005 尚未发布，之前用于人工验收的旧候选库和旧 job artifact 不具备新字段，不再作为兼容对象；后续桌面开发验证应使用新预览解析产生的新 artifact。不能通过静默覆盖旧 ref 来掩盖这一差异。

## 注释与可读性审查

- 将 `IngestionPreviewService` 改名为 `IngestionService`，同步修正 preview-only 类说明和包说明。
- 更新 ArtifactStore、build_block_set 和 run_migrations 中已经过期的职责描述。
- 为 Rust crate、sidecar 生命周期、Tauri 命令和前端 API 边界补充用途说明。
- 为 SQLite 迁移备份、失败恢复、DocumentRegistry 事务、artifact 验证和预览一致性补充重要不变量。
- Ruff 公共 docstring、前端 JSDoc 检查、Rust `missing_docs`、Git hook 和 CI 已实际执行通过。
- 子 Agent 仍只承担语义复核，不作为确定性的执行门禁。

## 后续

- 006 已完成 Qwen embedding、dense／关键词候选索引；下一步进入 007 S6 manifest 与 collection 原子发布。
- 文档列表与删除、人工改标题、跨来源合并另立切片。
