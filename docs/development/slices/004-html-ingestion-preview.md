# 004 · HTML 导入与结构预览

- 状态：已完成
- 开始日期：2026-10-20
- 关联设计：[07 HTML 入库设计](../../design/07-html-ingestion-design.md)、[06 检索侧契约](../../design/06-retrieval-pure-agent.md)
- 人工验收：[004 HTML 导入人工验收](../manual-tests/004-html-ingestion.md)

## 目标

同时支持本地 HTML 文件和公开静态／SSR URL，将两类来源统一转换为可检查的 SourceArtifact、语义节点、章节树和有序 block，并在桌面应用中预览。

## 范围

包含：

- Tauri 原生本地文件选择器和 URL 输入。
- 本地文件与 HTTP／HTTPS URL 两种 S0 来源适配器。
- URL SSRF 防护、重定向校验、响应类型与体积限制。
- HTML 正文提取、候选标题、语言、语义节点和提取 hash。
- 稳定章节路径、章节树警告和正文节点归属。
- 固定 sizing estimator、无重叠 block、表格与长文本拆分。
- LocalAppData job workspace 与预览 API。

不包含：

- SQLite 文档／版本持久化和正式 collection 发布。
- embedding、LanceDB、BM25、模型或 Agent。
- JavaScript 渲染、登录页、付费墙和整站抓取。

## 验收条件

- [x] 本地 `.html/.htm` 文件和公开 URL 使用同一预览流程。
- [x] 原始来源在解析前原子写入不可变 job artifact。
- [x] URL 仅允许 http／https，阻止 localhost、私网和链路本地地址。
- [x] URL 每次重定向重新校验目标，最多 5 次。
- [x] 非 HTML、超时、超大响应、空正文和 JavaScript 空壳返回不同错误码。
- [x] 标题、语言、语义节点、提取 hash 和警告可检查。
- [x] 标题级别跳级、重名标题、前置正文和无标题文档按 07 契约处理。
- [x] 长段落、列表、代码和表格的拆分不静默丢失内容。
- [x] block 不跨章节、无 overlap，`block_index` 连续，字符区间可对账。
- [x] 相同输入和版本得到相同章节、block 边界、hash 和 ID。
- [x] Vue 可展示文档统计、章节路径和 block 预览。
- [x] 后端测试、前端检查、Rust 编译和实际桌面导入通过。

## 实现决策与设计对应

“待确认”表示这是实现自行选择、需要沛东二次确认的点；“设计明确”表示 07/09 已经规定，不进入二次确认。

| ID | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | 单篇 HTML 大小上限 | 本地与 URL 原始内容均限制 20 MiB | 07 要求体积限制；沛东已确认采用 20 MiB | 10 MiB 更保守；50 MiB 资源风险更高 | 影响可导入资料上限 | 超限样本测试 | 已确认 |
| D2 | URL 超时与重试 | 连接 10s、总 60s；429／5xx 最多 2 次，退避 1s／4s | 07 §3.3.1 明确给定 | 不额外重试更简单；更多重试增加等待 | 影响弱网体验与成本 | MockTransport 测试 | 设计明确 |
| D3 | URL 可接受 MIME | 仅 `text/html` | 07 §4.2 指定当前适配器只接受 text/html | 扩展 XHTML 会扩大未验证范围 | 影响可接收页面类型 | Content-Type 测试 | 设计明确 |
| D4 | 预览阶段文档身份 | 使用 `preview_` + source_key SHA-256 前缀；不冒充已持久化 doc_id | 07 S2 要求持久化分配，本切片尚无 SQLite；沛东已确认临时身份方案 | 先做 SQLite 会越过 004 范围 | 005 持久化时必须替换为正式 doc_id | 结构稳定测试 | 已确认 |
| D5 | preview block ID | `{preview_doc_id}#b{block_index:05d}` | 07 要求由文档、切块版本和位置生成；沛东已确认预览格式 | 使用纯内容 hash 会在重复文本处冲突 | 005 需要正式 ID 策略 | 重复文本块测试 | 已确认 |
| D6 | sizing estimator 的近似公式 | CJK 每字约 1 token，拉丁字母数字串约 4 字符 1 token，标点约 4 字符 1 token，空文本计 1；版本 `cjk-ascii-v1` | 07 要求固定 estimator 和 450 目标；沛东已确认采用近似公式 | 精确 tokenizer 增加依赖且仍非模型唯一口径 | 只影响切块和章节预算提示，不承担运行时精确计费 | 单元测试与覆盖对账 | 已确认 |
| D7 | 超长普通文本拆分 | 依次按句子、分句／空白／标点、字符边界拆分；列表和代码使用独立降级规则 | 07 §8.3 明确拆分顺序 | 固定窗口会提前破坏语义 | 影响块边界与警告 | 超长中文句子、分句、无标点文本、列表项和代码行测试 | 设计明确 |
| D8 | 大表格阈值 | `TableAtomicMax = 4000` context token；超过后按完整表格行组拆分，单行超限保留原子块并告警 | 07 §8.3 明确阈值与单行规则 | 与 450 同阈值会过度拆表 | 影响表格块数量和上下文 | 普通大表与超 4000 token 单行测试 | 设计明确 |
| D9 | URL DNS 防护范围 | 对解析出的所有地址要求 `ipaddress.is_global`，并记录 DNS rebinding 残余风险 | 07 要求阻止私网／链路本地；沛东已确认当前单用户本地场景暂时接受残余风险 | 完整 IP pinning 需自定义连接层；公开发布前由 BL-0002 重新评估 | 影响 SSRF 防护强度 | 地址分类测试 | 已确认 |
| D10 | 预览 job 存储 | `data_root/jobs/{job_id}`，原始文件、提取 JSON、预览 JSON 采用临时文件＋原子替换 | 07 要求 job workspace 和不可变 artifact；沛东已确认当前存储方式 | 内存处理不满足可追溯；清理策略由 BL-0003 跟踪 | 需要后续清理策略 | 文件存在与 hash 测试 | 已确认 |
| D11 | 登录／空壳识别 | 页面正文≤800 字符、表单外说明≤200 字符、表单内密码框和登录语义同时满足才判 `login_required`；极低正文＋脚本判 JS 空壳；其他低正文归 empty content | 07 要求失败分层但未给启发式阈值；沛东已确认采用保守组合规则 | 仅凭密码框会误伤登录表单教程；纯文本关键词又容易过度分类 | 影响少数页面错误码 | 登录页、JS 空壳和教程误判样本 | 已确认 |
| D12 | 语言识别依赖与体积 | 改为 `script-ratio-v1`：20 字符信号门槛、20% mixed 双向下限、80% 单一语言优势；文档声明优先，block 独立判断并在信号不足时回退文档语言；移除 Lingua | 沛东已确认轻量方向及具体阈值；设计变化见 ADR 0004 | 保留 Lingua 体积过大；纯声明方案无法识别 block mixed；阈值仍需真实语料校准 | 显著降低 sidecar 体积；中文／英文／mixed／unknown 成为可解释提示 | 语言单测、block 集成测试和打包体积检查 | 已确认 |
| D13 | 普通文本拆分 warning 粒度 | 语义／标点边界成功拆分记录 `text_semantic_split`；只能按字符降级时记录 `text_boundary_split` | 07 要求超大文本拆分不静默降级；沛东已确认两个 warning 的语义 | 统一用 `text_boundary_split` 会掩盖语义拆分与字符降级差异 | 影响日志可诊断性和后续回归断言 | 长句子、长分句和无标点文本测试 | 已确认 |

## 设计变更记录

本切片对当前设计基线的影响集中记录如下；“设计明确”不是自行决策，不需要额外确认。

| 变更 | 原设计 | 变更后 | 原因 | 用户确认 | ADR | 已更新的设计文档 |
| --- | --- | --- | --- | --- | --- | --- |
| D1 输入体积上限 | 07 只要求设置最大体积，没有数值 | 本地与 URL 原始内容统一限制 20 MiB | 控制解析内存、磁盘和异常输入风险 | 已确认 | 不单独新增 ADR；属于 07 内的量化补充 | [07](../../design/07-html-ingestion-design.md) |
| D9 DNS rebinding 残余风险 | 07 要求阻止私网和链路本地目标，未规定防护深度 | 当前使用全地址 `is_global` 校验，接受 rebinding 时间窗；公开发布前由 BL-0002 评估 IP pinning | 自定义连接层超出 004 范围，但风险必须显式保留 | 已确认 | 不单独新增 ADR；由 07 与 BL-0002 跟踪 | [07](../../design/07-html-ingestion-design.md) |
| D10 job artifact 清理 | 07 要求 job workspace，未规定清理策略 | 当前保留成功／失败 job artifacts，清理策略由 BL-0003 跟踪 | 清理依赖正式持久化和用户设置 | 已确认 | 不单独新增 ADR；由 BL-0003 跟踪 | 不修改设计基线 |
| D11 登录页识别 | 07 要求区分登录页，未给启发式阈值 | 页面正文≤800 字符、表单外说明≤200 字符、表单内密码框和登录语义同时满足才判定登录页 | 避免登录表单教程被误伤 | 已确认 | 不单独新增 ADR；属于 07 内的行为细化 | [07](../../design/07-html-ingestion-design.md) |
| D12 语言检测组件 | 09 指定 Lingua，只配置首版中英文 | 使用无额外模型依赖的 `script-ratio-v1` 确定性检测，并输出文档／block 级 `zh`／`en`／`mixed`／`unknown` | Lingua 使 sidecar 增加约 290 MB，不符合普通 Windows 用户的交付成本 | 已确认 | [0004](../../decisions/0004-lightweight-language-detection.md) | [06](../../design/06-retrieval-pure-agent.md)、[07](../../design/07-html-ingestion-design.md)、[09](../../design/09-technology-selection.md) |
| D13 文本拆分 warning | 07 只规定字符降级记录 `text_boundary_split` | 语义拆分记录 `text_semantic_split`，字符降级继续记录 `text_boundary_split` | 单个 warning 无法区分正常语义拆分与边界降级，影响诊断和回归 | 已确认 | 不单独新增 ADR；属于 07 内的契约澄清 | [07](../../design/07-html-ingestion-design.md) |

## 验证证据

自动化与后端直连：

- 后端测试：39 passed，包含 `script-ratio-v1` 语言、block 语言、长段落、长列表、长代码行、超大表格行和登录表单教程误判测试。
- Ruff format／lint：通过。
- Pyright standard：0 errors。
- 前端类型检查与生产构建：通过。
- Rust check、fmt、clippy：通过。
- Tauri release 与 NSIS 生产构建：通过；安装包为 19.81 MiB。
- 真实公开 URL `https://docs.python.org/3/tutorial/controlflow.html`：成功提取 24 个章节、126 个 blocks。
- 非 HTML URL、私网 URL、非法协议分别得到 `unsupported_media_type`、`ssrf_blocked`、`invalid_url`。
- PyInstaller sidecar 重建成功，共 725 个文件、40.93 MiB；产物中未发现 Lingua 文件。
- 打包 sidecar 冒烟：健康检查为 `ok`；中文 fixture 得到 `zh`／5 blocks，英文 fixture 得到 `en`／1 block，登录表单教程 fixture 成功导入；退出后端口与进程已清理。

人工界面：

- [004 HTML 导入人工验收](../manual-tests/004-html-ingestion.md) 11 项全部通过。
- 覆盖复杂本地文章、无标题、中文和空格路径、三类本地失败、成功 URL、三类 URL 失败以及退出清理。

当前实现结果：

- 移除 Lingua 后，PyInstaller sidecar 从约 331.5 MiB 降至 40.93 MiB。
- DNS rebinding 加固与 job artifact 清理已分别登记为 BL-0002、BL-0003，作为公开发布前的后续事项。

## 后续待办

- [BL-0001](../backlog.md)：HTML 图片资源归档与预览。当前只保留 alt/caption，不下载或显示图片。
