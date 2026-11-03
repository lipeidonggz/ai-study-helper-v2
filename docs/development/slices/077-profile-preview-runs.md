# 077 · Profile Preview Run

- 状态：已完成
- 开始日期：2026-11-02
- 关联设计：[12 Profile-Driven Wiki Extraction](../../design/12-profile-driven-wiki-extraction.md)
- 前置切片：[076 Profile 管理与选择](076-profile-management.md)
- 人工验收：批次验收；与 076、078 统一复审

## 控制契约

### 背景与失败场景

- 触发问题：Profile 配置需要在实际文档上运行，才能形成可比较的候选结果。
- 不处理时的用户损失：用户只能看配置，无法判断 Profile 是否按预期工作。
- 现有流程为什么不足：V20 是实验脚本，没有持久 Run、复用、取消、失败和快照绑定语义。
- 本切片负责什么、不负责什么：负责单 Profile 的多文档 Preview Run、四通道执行、可选遗漏补抽、精确去重和执行 API；不负责跨文档语义归并、页面和问答。
- 什么情况下应停止或撤掉本切片引入的机制：无法保证 source evidence 合法、失败 run 不发布结果，或必须通过共现合成 candidate 才能得到结果时停止。

### 用户可观察目标

用户可以选择已完成入库的文档启动一个 Profile Preview，看到进度、状态和按文档展示的 candidate/evidence；相同输入可选择复用历史成功结果或强制重新运行。

### 核心不变量

1. Preview Run 绑定 Profile 配置快照、snapshot、文档版本、Prompt/model/window policy 和 fingerprint，完成后不可变。
2. 只有全部文档、通道和启用补抽成功，run 才进入 completed；其他终态不发布正式候选集。
3. 每个 candidate 只引用当前 snapshot 文档版本中的真实 block，禁止共现合成和 alias 连通合并。
4. 第一阶段只按 normalized primary name 进行文档级精确去重，不做跨文档语义归并。
5. 同一时间只有一个 run 运行，其他 run 排队；同一 run 内部最多两路受控并发。

### 错误代价与非对称门禁

| 错误类型 | 具体例子 | 用户影响 | 处理分类 | 指标与阈值 |
| --- | --- | --- | --- | --- |
| 非法证据 | source block 不属于 snapshot | 结果不可追溯 | 阻断 | 0 容忍 |
| 共现合成 | 同段对象和机制自动拼成新候选 | 注入未支持事实 | 阻断 | 0 容忍 |
| 失败被标成功 | 单文档失败但 run=completed | 比较基于残缺结果 | 阻断 | 0 容忍 |
| 静默复用 | fingerprint 不同却返回旧 run | 配置差异被掩盖 | 阻断 | 0 容忍 |
| 重复运行覆盖历史 | 强制 rerun 改写旧 run | 稳定性比较丢失 | 阻断 | 0 容忍 |

### 明确禁止

- 不实现 `mechanism_application` 或其他共现组合。
- 不在本切片执行语义候选归并或跨文档合并。
- 不让前端计算 fingerprint、执行模型调用或写入 candidate。
- 不恢复 failed／cancelled／interrupted run。
- 不对模型输出做静默截断或用空结果冒充成功。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 无 Chat Key | 明确配置错误，run=failed | run 诊断和已用 usage | 配置后新建 run |
| 单窗口模型失败 | run=failed | 已完成的非正式诊断 | 新建 run |
| 用户取消 | cancelled | 已完成步骤的 diagnostics | 新建 run |
| 进程重启 | interrupted | 状态与诊断 | 新建 run |
| 输入非法 | 请求校验错误 | 不创建 run | 是 |

### 人工检查点

- 编码前需要确认的契约或风险：无新增产品决策；按 12 号设计 D34、D36、D38、D39、D43、D46 实现。
- 实现中途需要查看的可运行路径：完成一个单文档 run 并读取 candidate/evidence。
- 完成后需要人工执行的操作：并入最终批次验收。

### 决策预算

- 预计 B 级决策数量：5
- 是否存在 C 级决策：否
- 超过预算时的拆分方式：拆分通道执行器／持久化与复用／API 与前端

## 目标

从 V20 中提炼可产品化的四通道候选抽取，建立不可变 Preview Run 和 API。

## 输入与输出

- 输入：Profile ID、snapshot、document IDs、`reuse_if_exact|force_new`。
- 输出：run 状态、按文档候选、source evidence、usage、诊断和 fingerprint。

## 不包含

- 文档清单和 Profile 比较；
- 跨文档语义归并；
- canonical topic、页面、链接、索引、问答；
- 自动重试、局部窗口恢复或后台重跑。

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 |
| --- | --- | --- | --- | --- | --- |
| A1 | 启动完整预览 | 整个文档全部 block 被处理 | block 覆盖审计 | 100% 入窗 | 单元／集成 |
| A2 | 结果可追溯 | candidate evidence 都属于 run snapshot | 非法 ref=0 | 0 | 单元／集成 |
| A3 | 配置可复用 | 相同 fingerprint 默认复用最新 completed | 返回相同 run | 100% | 集成 |
| A4 | 强制重跑 | force_new 不覆盖旧 run | 新 run ID，旧 run 不变 | 100% | 集成 |
| A5 | 全有或全无 | 任一文档／通道失败则 run=failed | partial 无正式候选 | 100% | 集成 |
| A6 | 无共现合成 | 同块对象＋机制不自动新候选 | 反例候选数=0 | 0 | 单元 |
| A7 | 保守去重 | alias-only 组合不自动合并 | 两候选仍分开 | 100% | 单元 |

### 验收冻结与变更

- 冻结时点：控制契约确认后。
- 已确认的阻断反例：非法 source、共现合成、alias 连通合并、失败 run 被比较。
- 验收变更记录：负责人要求将人工复审合并到 076–078 阶段结束后。

## 验收执行

- [x] 用户可观察的主路径成立。
- [x] 核心不变量已被自动化测试或批次人工验收覆盖。
- [x] 失败路径不会破坏旧状态或伪装成功。
- [x] 验收矩阵中的阻断项全部通过。
- [x] 未通过的已知反例已修复，或由负责人明确接受风险。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 077-D1 | B | candidate 与 evidence 持久化 | candidate 行保存 evidence ID 列表和内容 hash | D41 | 独立 evidence 表更规范但首版增加连接 | 简化读写且可校验 | round-trip／非法引用测试 | 已确认（2026-11-03） |
| 077-D2 | B | 通道调度 | 单 dispatcher + run 内 `asyncio.Semaphore(2)` | D43、D46 | 全并行更快但不控供应商压力 | 控制成本与并发 | 调度测试 | 已确认（2026-11-03） |
| 077-D3 | B | token 预估算 | 使用确定性字符估算拆分输入与输出，并明确标记 estimate | D43 | 精确 token 需额外调用 | 启动前可提示成本量级 | 请求响应测试 | 已确认（2026-11-03） |
| 077-D4 | B | 通道结果诊断 | 按 channel/window 保存调用摘要到 diagnostics JSON | D34、D43 | 独立表可查询但首版无分析 UI | 支持失败定位 | 失败诊断测试 | 已确认（2026-11-03） |
| 077-D5 | B | 前台更新方式 | 前端轮询 run detail，不新增流式通道 | D43 | SSE 实时性高但扩大切片 | 控制复杂度 | UI 状态测试 | 已确认（2026-11-03） |

## 证据包

### 自动化证据

- 命令：`backend\.venv\Scripts\python.exe -m pytest backend\tests\test_profile_extraction_preview.py backend\tests\test_profile_extraction_api.py -q`
- 结果：通过；覆盖整篇窗口、evidence 正文持久化、completed 复用、force_new、两文档独立、非法 source 失败、主名称精确合并和 alias-only 不合并。
- 命令：`corepack pnpm quality:quick`
- 结果：通过。
- 命令：`corepack pnpm quality`
- 结果：377 项通过、17 项旧 11 号测试显式跳过。
- 回归：除已废弃的 `test_wiki_topic_map_identity.py` 外，后端 377 项测试通过。
- 覆盖的不变量：1–5。

### 运行证据

- 启动方式：桌面开发壳或 `pnpm dev`
- 用户操作序列：选择 Profile 与两篇文档 → 计算计划 → 确认成本 → 启动预览 → 查看进度和候选 evidence
- 预期结果：一次只运行一个 run；相同 fingerprint 可显式复用；失败不产生正式候选
- 实际结果：用户完成试抽取成本、运行状态、候选浏览、原文顺序和翻译流程的界面验收。

### 失败探针

- 注入或复现的失败：测试 Provider 返回窗口外的 source block。
- 系统保留的旧状态：没有正式 candidate；run 记录 `failed` 和诊断。
- 用户看到的错误：`candidate_source_invalid` 及失败 run 状态。

### 已知反例账本

| 场景或反例 | 期望行为 | 实际结果／分布 | 是否阻断 | 处理结论 |
| --- | --- | --- | --- | --- |
| 同段无关对象与机制 | 不生成组合候选 | 新实现无共现组合路径 | 是 | 已消除 |
| 非法 source block | run 失败且 candidates 为空 | 集成测试通过 | 是 | 已修复 |

### 人工验收

- [x] 已在 076–080 批次中完成试抽取、成本摘要、运行反馈和证据审核。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 确定使用后会不会自动产生试抽取或全库构建？ | 不会；确定使用只冻结 Profile 并更新知识库选择指针。 | 通过 | 无 |
| R2 | 正式比较必须固定哪些输入？ | 固定 snapshot、文档版本、两个 Preview Run、Profile 版本与冻结质量基准；新 snapshot 或新 Profile 另生成新记录。 | 通过 | 无 |
| R3 | 未关联候选与翻译应如何影响证据判断？ | 未关联不等于漏提，是否值得提取由 Profile 决定；翻译不是原文，不能替代 evidence 或改变覆盖关系。 | 通过 | 无 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 协作复盘

- AI 明显加速的环节：run 状态机、调度、持久化、候选查询和界面反馈同步实现。
- 负责人失去系统模型的环节：候选数量增大后，单一纵向列表难以支持审核，需要真实数据才能暴露。
- 真正发现问题的控制点：用户用完整 run 和大量候选进行操作，推动主从布局与原顺序视图。
- 只有仪式感、没有产生判断价值的流程：在没有真实候选规模时反复讨论列表细节，价值有限。
- 下一次准备调整的一到两项：先用代表性真实数据校准信息密度，再冻结交互细节。

## 已知风险与后续

- 未验证项：真实 Provider 下的多文档成本、通道稳定性和中文文档。
- 接受的风险：首版只做 exact primary-name 去重，名称不同但同义的候选会重复。
- 后续待办或 ADR：BL-0018 跨文档全局语义候选归并。

## 停止与推翻条件

- 通道执行无法保证证据边界。
- 输出稳定性需要重新引入共现关系合成。
