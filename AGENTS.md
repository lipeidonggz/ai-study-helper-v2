# ai-study-helper v2 项目约定

## 项目目标

从零实现一个面向普通 Windows x64 用户的本地文档学习问答 Agent，并在此过程中练习 Agent 产品定义、架构设计、实现、评测和回归的完整方法。

旧仓库 `lipeidonggz/ai-study-helper` 只作为学习过程和实验资料的归档。它的代码、测试、数据、提示词和结论都不是本仓库的实现基线。

## 设计权威来源

本仓库只以 `docs/design/01-product-requirements.md` 至 `docs/design/09-technology-selection.md` 为当前产品设计输入：

- 01：产品定义、15 类问题、范围与质量属性
- 02：Agent 开发方法
- 03：用例与金标准
- 04：架构设计方法
- 05：存储与检索策略
- 06：检索侧、工具、Agent 循环、trace 与部署
- 07：HTML 入库与 collection 快照
- 08：评测台
- 09：技术选型与总体架构

出现冲突时，以编号更晚、且专门覆盖该主题的设计为准。旧仓库记录只有在沛东明确要求时才可作为参考，不能自动进入实现上下文。

## 产品硬约束

- 目标用户：普通非技术用户
- 平台：Windows x64
- 产品形态：可安装桌面应用
- 使用边界：单用户、单知识库、本地运行
- 网络边界：只监听 `127.0.0.1`，不开放局域网或远程访问
- 模型：只使用云端 API，不做本地模型推理
- 凭证：对话与 embedding 使用两个独立 API Key
- 首版入库：本地 HTML 和公开静态／SSR HTML URL，不执行 JavaScript
- 首版规模：约 10 万 block／1,000 篇文档

## 技术基线

- 桌面壳：Tauri 2
- 前端：Vue 3 + TypeScript + Vite
- 后端：Python + FastAPI + Pydantic + Uvicorn
- Agent 编排：LangGraph
- 主数据：SQLite + SQLAlchemy + Alembic
- 索引：LanceDB；首版 dense 精确扫描 + BM25/FTS，不建 ANN
- HTML：httpx + lxml + readability-lxml + 自有语义遍历
- DeepSeek：OpenAI Python SDK
- Qwen embedding：DashScope Python SDK
- trace：OpenTelemetry 数据模型 + 本地 JSONL/SQLite exporter
- 评测台：独立 Tauri + Vue + Python + SQLite 工具
- 打包：PyInstaller sidecar + Tauri NSIS + WebView2 bootstrapper

## Clean-room 规则

1. 不复制旧仓库的业务代码、测试、fixture、数据库、trace、提示词或前端组件。
2. 不因为旧实现“已经能用”而跳过当前设计中的接口、测试和验收。
3. 任何从旧仓库借用的经验都必须转写成当前设计语言，并由沛东明确确认。
4. 新代码按 01–09 的契约重新设计；命名、模块边界和数据模型不得从旧实现倒推。
5. 遇到设计未覆盖的问题，先记录问题和候选方案，再实现；不要用旧代码填空。

## 实现纪律

- 按垂直切片实现：每个切片必须有输入、输出、失败语义和可运行验证。
- 先写验收，再实现；测试失败先定位根因，不先改断言。
- 金标准、判官提示词、系统提示词和工具行为契约属于基础件，AI 可起草，必须由沛东人工复核后才能生效。
- 评测失败必须做根因闭环：失败 → 根因 → 代表探针 → 修复 → 回归。
- 版本、模型 ID、prompt、工具契约、collection 快照和评测 revision 必须可追溯。
- 不允许静默降级、静默截断、静默换模型或把缺失样本算成通过。

## 代码与目录方向

计划采用单仓库多应用结构；实际目录在第一个实现切片启动时确认：

```text
desktop/         Tauri 桌面壳
frontend/        Vue 产品 UI
eval-ui/         Vue 评测 UI
backend/         Python 产品与评测服务
docs/            设计基线、决策与开发文档
```

不要提前生成空模块。目录只在对应垂直切片开始实现时创建。

## 文档与记录规范

所有长期文档使用 Markdown，`docs/README.md` 是统一索引。新增或移动文档后必须同步更新索引。

记录按用途分类，不建立按时间排列的总日志：

- `docs/design/`：已审核的产品、架构与行为设计基线。现有 `01–09` 保持稳定；只有形成新的稳定设计主题时才继续编号。
- `docs/decisions/`：Architecture Decision Record（ADR，架构决策记录），文件名使用 `NNNN-短描述.md`，独立从 `0001` 开始编号。记录背景、候选方案、决定、后果、验证方式和推翻条件。
- `docs/development/slices/`：垂直切片的实现记录，文件名使用 `NNN-短描述.md`。每个切片记录目标、范围外事项、输入／输出、失败语义、验收条件、关联版本、关键转折与根因、验证证据和遗留问题。
- `docs/development/experiments/`：性能、质量和兼容性实验，文件名使用 `YYYY-MM-DD-短描述.md`。记录固定配置、版本、样本、结果、结论和复现方式。
- `docs/operations/`：安装、排障、备份、升级和诊断等长期使用说明。

实现记录只保留有解释价值的过程，不记录逐轮操作流水、模型内部思考或无关会话。大型数据库、trace、artifact 和原始评测产物仍保存在本地或被忽略目录中，不直接提交进 Git。

GitHub Issue、Project 和 PR 用于临时任务、进行中状态和开放式讨论。只有形成可长期复用的结论后，才整理进 `docs/`。

各类目录在第一次产生对应内容时创建，不提前生成空目录。新代号、缩写和状态名首次出现时必须解释。

## 提交规范

- 每次提交都包含标题和正文。
- 正文写明本次改动与当前项目主要功能摘要。
- API Key、token、密码和其他敏感信息不得出现在代码、测试、日志、trace、文档或提交中。

## 当前阶段

设计阶段完成，已进入实现。前三个垂直切片完成桌面运行链、PyInstaller one-folder release 分发以及 NSIS 按用户安装；下一步开始本地 HTML 导入、正文提取、章节树与 block 预览，不提前实现完整 RAG 或评测台。
