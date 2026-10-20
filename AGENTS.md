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
docs/            设计基线与开发文档
```

不要提前生成空模块。目录只在对应垂直切片开始实现时创建。

## 记录规范

- 设计文档和过程记录一律使用 Markdown，保存在 `docs/`。
- `docs/README.md` 是文档索引；新增记录后必须同步更新。
- 设计基线保存在 `docs/design/`，文件名使用 `NN-短描述.md`，后续从 `10` 继续编号。
- 开发过程和使用文档保存在 `docs/development/`，在对应内容开始编写时再创建。
- 每篇记录区分“结论与主线”和“现场细节”，保留真实决策过程、失败与转折。
- 新代号、缩写和状态名首次出现时必须解释。

## 提交规范

- 每次提交都包含标题和正文。
- 正文写明本次改动与当前项目主要功能摘要。
- API Key、token、密码和其他敏感信息不得出现在代码、测试、日志、trace、文档或提交中。

## 当前阶段

设计阶段完成，进入实现启动。第一步只建立可运行的最小工程骨架，不提前实现完整 RAG 或评测台。
