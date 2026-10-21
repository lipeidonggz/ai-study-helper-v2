# 006 · Qwen embedding 与 dense／关键词双索引

- 状态：已完成
- 开始日期：2026-10-21
- 关联设计：[07 S5 建索引](../../design/07-html-ingestion-design.md)、[09 Qwen 与 LanceDB](../../design/09-technology-selection.md)
- 后续切片：007 collection 快照、manifest 与原子发布

## 目标

把一篇已经持久化的文档版本转换成不可发布的候选 `IndexBundle`：用 Qwen 文档 embedding 生成 dense 向量，用固定本地 analyzer 生成关键词 search text，并把两路索引写入同一份 LanceDB 候选表。

本切片只证明 S5 能生成完整、可对账、可诊断的候选索引，不把候选索引暴露给检索侧。

## 输入与输出

输入：

- 已持久化的 `doc_id`／`doc_version`；
- 文档标题与来源元数据；
- 同一版本的 `SectionTree` 与 `BlockSet`；
- Qwen embedding provider 配置或用于自动测试的确定性 fake provider。

输出：

- `IndexBundle`：embedding profile、输入模板、关键词 profile、逐 block 覆盖信息、usage preview、实际 usage 和候选索引引用；
- job 目录内不可变候选 LanceDB 表，dense 向量与关键词 search text 共享同一组 `block_id`；
- `index.json` 候选构建证书与失败时的 `index-failure.json` usage 证据。

## 不包含

- collection manifest、候选状态汇总和 checksum 清单。
- `ActiveCollectionPointer`、自动发布、回滚和旧快照清理。
- `search_blocks` 查询链路、RRF、query embedding 和 Top-5 返回。
- API Key 设置向导与前端界面；本切片只实现 Windows Credential Manager 存取边界。
- 生产级后台任务队列、暂停／取消、重启续跑和进度推送；006 的服务接口暂为同进程内调用，手动 API 入口只用于当前切片验证。
- 完整正文的付费索引压测；本切片只做一次短文本真实连通性验证和 fake provider 全链路测试。

## 验收条件

- [x] embedding 输入固定包含文档标题、章节路径、表格上下文／确定性提示和 block 原文，并记录模板版本与 hash。
- [x] 输入为空、只有不可见字符或 block hash 不一致时明确失败。
- [x] Qwen 单批不超过 20 条；2,001 个唯一输入规划为 101 批。
- [x] 批次之间默认最多 3 个请求在途；失败后不再启动排队批次，已发请求完成并保留 usage。
- [x] 同一 `embedding_input_hash` 在一次构建中只请求一次，并把相同向量复用于对应 block。
- [x] 网络错误、429 和 5xx 最多重试 2 次；`Retry-After` 在 60 秒内时优先遵守，超过 60 秒立即终止同步构建，缺失时才使用 1s／4s 抖动退避；4xx、鉴权失败和空输入不重试。
- [x] 实际 usage 记录请求次数、input tokens、request ID 和响应丢失后的模糊重试次数。
- [x] 返回空、零向量、维度不为 1024 或响应数量不匹配时失败，不生成关键词-only 降级结果。
- [x] 关键词 analyzer 固定执行 Unicode NFC、英文 case folding、技术标识符保留／子词展开、中文单字和相邻双字；不做词干化或同义词扩展。
- [x] 关键词 search text 包含标题、章节路径、表格上下文和 block 原文；`scan_term` 语义不在本切片实现。
- [x] dense 与关键词写入同一 LanceDB 候选表，且 block ID 集合与 `BlockSet` 完全一致。
- [x] LanceDB 使用 1024 维 float32 dense 精确扫描，不创建 ANN；关键词使用固定 analyzer 产出的 token 串建立 FTS 索引。
- [x] 候选 bundle 明确标记为 `candidate`，不创建活动指针，不被检索侧视为已发布。
- [x] 同一文档版本、block set、模型 profile 和输入策略重复构建时复用同一候选索引，不静默覆盖。
- [x] Python API 可用 fake provider 完成端到端构建；无效 API key 和上游失败返回稳定错误码。
- [x] PyInstaller sidecar 包含 LanceDB、PyArrow、DashScope 与 Windows keyring 所需运行时文件，并通过启动／数据库迁移冒烟。
- [x] 真实 Qwen embedding 连通性：`qwen3.7-text-embedding` 接受 `text_type=document`、1024 维 dense 请求并返回有效向量与 usage。

## 实现决策与二次确认

| ID | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | 设计产品名与 DashScope 真实 model ID 的对应 | 使用设计基线名称 `qwen3.7-text-embedding` 作为默认请求 ID，并让 provider 显式记录该 ID；不替换成 SDK 常量中的其他模型 | 07／09 要求固定并记录供应商真实 model ID，不允许静默换模型 | 直接替换成 SDK 常量会偏离设计；真实调用已证明该 model ID 可被供应商接受 | 模型 ID 固定；后续更换模型必须形成新的 embedding profile 和候选索引 | fake 协议测试、真实 key 冒烟 | 已确认 |
| D2 | 候选索引在 job workspace 中如何落盘 | 使用 `jobs/{job_id}/idx/{index_id}/lance`，`index_id` 由 doc version、block set、embedding profile、输入模板和关键词 profile 共同确定；`index.json` 与 LanceDB 同目录；目录名保持短路径以适配 Windows 经典路径上限 | 07 要求候选在 job workspace 构建，S6 通过后统一发布 | 固定单一路径会覆盖历史候选；过长路径在 LanceDB Windows 写入中出现 `os error 3` | 候选索引不可变、可复用，S6 可把目录引用写入 manifest | 路径、重复构建、LanceDB 覆盖测试 | 已确认 |
| D3 | embedding 文本的精确模板与 hash | 固定字段顺序为“文档标题／章节／上下文提示／正文”，每段带固定中文标签；章节为空时写“根章节”；提示按 key 排序；对模板元数据而不是单个 block 内容计算 `template_hash`；`source_label` 不进入模板 | 07 要求精确标点、换行、字段顺序版本化，并记录模板 hash | 只存版本号无法发现模板实现被改；对内容一起 hash 会失去模板复用意义；拼入来源名可能引入语义偏置 | 模板变化必须提升 policy version 并触发重新 embedding | 快照式字符串断言与确定性 hash 测试 | 已确认 |
| D4 | 中文与代码关键词 analyzer 的具体实现 | 使用自有 `unicode-cjk-code-v1`：NFC、拉丁 case fold、技术标识符原 token 加分隔符／驼峰子词、CJK 单字与相邻双字；LanceDB FTS 使用 whitespace tokenizer 且关闭额外 lowercase、stem、stop words | 07 要求固定 analyzer、保留标识符、中文单双字、禁止词干化和同义词扩展 | ICU／jieba 会引入数据文件或词典版本；LanceDB 默认 simple tokenizer 会再次改变 token | 查询侧必须复用同一 analyzer；analyzer 变化提升 profile | 中英混排、camelCase、snake_case、版本号与确定性测试 | 已确认 |
| D5 | 原始 dense 向量是否重复写入 JSON bundle | `IndexBundle` 的 block 记录保存向量引用、input hash 和 content hash；1024 维原始向量只保存在 LanceDB 候选表，不复制进 JSON | 09 规定 LanceDB 是可重建派生索引，主数据不重复存同一份大数组 | 完全按逻辑契约把向量复制到 JSON 会让 10 万 block 再增加约 GB 级冗余；只存引用会偏离文字版 `dense_vector` 字段 | S6 对账通过 `block_id`、hash、维度和 LanceDB 行读取验证；避免双份事实源 | bundle 与 LanceDB 逐 block 对账测试 | 已确认 |
| D6 | LanceDB 原生 BM25 参数的记录方式 | `keyword_profile` 记录 `lancedb-native-bm25` 与实际 `engine_version`；`bm25_k1`／`bm25_b` 为 null，`bm25_parameters_source=lancedb-engine-managed`。不提供无实际作用的参数开关，也不写入未验证默认值 | 07 要求 BM25 参数进入 profile，且不能把未验证数值伪装成可配置事实 | 当前 LanceDB 建索引和查询接口都没有 k1／b 入口；记录假设会误导，声称可配置则无效 | 关键词 profile 以引擎版本表达排序行为；引擎升级或开放参数时必须升版本 | profile 序列化、FTS 可检索、配置真实性测试 | 已确认 |
| D7 | 零 block 文档的 S5 行为 | 单篇持久化文档没有 block 时返回 `index_empty_block_set`，不生成空候选索引 | 07 规定单篇文档必须完整完成 embedding 和索引后才算成功 | 生成空 LanceDB 会被误认为完整；自动发布空文档会隐藏提取错误 | 该文档在 S6 只能进入失败清单 | 空 BlockSet 错误码测试 | 已确认 |
| D8 | API Key 的 Windows Credential Manager 命名 | keyring service 使用 `ai-study-helper`，username 使用 `embedding-api-key`；对话 key 使用独立 username `chat-api-key` | 09 要求两个 Key 独立保存、不进入配置、日志和 trace | 用同一个 username 可能互相覆盖；把 service 写成带版本名会增加未来迁移负担 | 后续设置向导和状态页共享该契约 | fake keyring 存取与缺失错误测试 | 已确认 |
| D9 | S5 的 API／job 状态边界 | 006 新增只接受 `job_id` 的候选索引命令；job 进入 `S5/running`，成功后保持 `S5/succeeded`，失败保留 `S5/failed` 和错误码；命令同步等待本次构建并只返回候选 bundle，不发布。该同步入口只作为当前切片验证，不接正式 UI | 07 要求分阶段诊断和不暴露半成品，同时要求最终产品使用后台任务 | 在 persist 请求内隐式调用会耦合保存和外发；完整后台队列会显著扩大 006；同步入口耗时随批次增长 | 当前可确定性验证 S5；正式 UI／发布前必须由独立任务切片补齐 progress、pause、cancel 和重启续跑 | fake provider API 集成、失败状态测试 | 已确认 |
| D10 | 同一输入 hash 的缓存范围 | 只在单次 S5 构建内对相同 profile＋input hash 去重；本切片不跨请求持久化向量缓存；完全相同的文档版本配置直接复用整个候选目录 | 07 允许相同输入复用，但缓存失效还要绑定文本、模型和模板 | 跨 job 持久缓存能省费用，但需要额外缓存目录、生命周期和撤销策略 | 当前避免单次构建重复付费；跨文档重复块的成本优化留给后续 | provider 调用次数、候选复用与向量一致性测试 | 已确认 |
| D11 | embedding 批次并发度 | 每批最多 20 个 block；默认最多 3 个批次请求在途。终态失败后不再启动排队批次，已发请求允许完成并汇总 usage；不提供无上限并发 | 设计要求记录并发并兼顾 Q8；用户确认 3 路上限 | 顺序执行吞吐不足；高并发放大 429、取消和计费不确定性；3 路在可靠性与大文档速度间取保守平衡 | 大文档缩短构建时间；仍需后台任务切片提供取消与进度 | 最大在途数、失败停止调度、usage 证据与全量回归 | 已确认 |
| D12 | `Retry-After` 的实现边界 | DashScope 响应包装实际包含 HTTP headers；适配器解析标准秒数和 HTTP 日期两种 `Retry-After`。0–60 秒内优先遵守；超过 60 秒立即终止当前同步构建；缺失时才用 1s／4s 抖动退避 | 07 要求优先遵守 `Retry-After`，但同步验证入口不能被超长等待阻塞 | 忽略 headers 会违背设计；无限等待会让一次 API 请求挂起一小时；私有协议解码会耦合 SDK 内部实现 | 错误分类和最大重试次数不变；超长限流交给后续后台任务稍后重试 | provider header、短等待与超长等待测试 | 已确认 |

## 验证证据

- 后端完整回归：84 passed；其中 006 新增 30 项，覆盖输入模板、2,001 输入分 101 批、最多 3 批并发、失败停止调度、失败 usage 证据、去重、重试、短／超长 `Retry-After`、零向量拒绝、关键词 analyzer、BM25 参数真实性、LanceDB dense／FTS 同覆盖、候选复用、API 成败状态和 DashScope 适配器。
- `corepack pnpm quality`：Ruff format／lint、Pyright、Vue／TypeScript、Rust fmt／check／clippy、注释检查和后端测试全部通过。
- `corepack pnpm backend:package`：PyInstaller one-folder 成功，1,489 个文件、396.5 MiB；包含 LanceDB、PyArrow、DashScope、Windows keyring 与 migrations。
- 打包 sidecar 启动冒烟：`/health` 与带启动令牌的 `/api/runtime` 返回 200，新数据根成功创建 `db/application.db`。
- 并发构建：最多 3 个批次请求在途；终态批次失败后不再启动排队批次，已发请求完成后把 attempts／tokens／request IDs 写入 `jobs/{job_id}/index-failure.json`。
- LanceDB 候选实表冒烟：5 个 fixture block 的 dense／FTS 集合完全一致，1024 维向量可读取，`沙箱 网络` FTS 可召回相关 block。
- 真实 Qwen embedding 冒烟（2026-10-21）：`qwen3.7-text-embedding` 返回 HTTP 200、1 条 embedding、1024 维全有限非零 dense 向量；usage 为 27 input tokens、27 total tokens。
- 真实 Key 仅在本轮验证进程的内存环境中使用，未写入 Credential Manager、配置、artifact、日志或仓库；验证后已清除进程环境并搜索确认工作区无该 Key。

## 已知风险

- LanceDB 0.40 的 `_lancedb.pyd` 单文件约 224.8 MiB，使后端 one-folder 体积从原先约 47 MiB 增长到 396.5 MiB。NSIS 压缩后的实际安装包体积尚未重新验收；发行前应确认压缩率与磁盘占用是否可接受。
- 真实调用已经确认 model ID、`text_type=document`、1024 维 dense 输出和 usage 字段；128k 单行上限与单批 20 条仍按设计约束执行，尚无必要用超长／满批样本额外产生费用。
- 三批次真实并发尚未单独压测；当前用确定性 provider 验证最大在途数、失败停止调度和 usage 聚合，真实 DashScope 单请求连通性已验证。
- LanceDB 原生 FTS 没有公开 k1／b 覆写参数，profile 已改为 null＋`lancedb-engine-managed`，并记录实际 LanceDB 版本；未来引擎开放参数时必须提升 keyword profile 版本。

## 后续

- 007 实现 `CandidateSnapshotManifest`、逐项验证、`ActiveCollectionPointer` 和原子发布。
- 007 完成后再接入 `search_blocks`；本切片不创建任何可被检索侧读取的活动集合。
