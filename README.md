# ai-study-helper v2

一个面向普通 Windows 用户的本地文档学习问答 Agent。用户可以导入自己的 HTML 文档，基于文档与模型内置知识提问；系统需要给出可核对、可复现、可评测的回答。

## 当前状态

产品需求、用例、架构、检索、入库、评测与技术选型已经完成。当前仓库处于实现启动阶段，尚未开始业务代码。

本项目采用 clean-room 方式重建。旧仓库只作为学习档案，不作为实现来源：

<https://github.com/lipeidonggz/ai-study-helper>

## 技术基线

```text
Tauri 2
Vue 3 + TypeScript + Vite
Python + FastAPI + Pydantic
LangGraph
SQLite + SQLAlchemy + Alembic
LanceDB
DeepSeek API / Qwen embedding API
```

首版定位为 Windows x64 单机桌面应用：不依赖 Docker、外部数据库或本地模型；只监听本机回环地址；用户分别配置对话与 embedding API Key。

## 设计基线

- [产品需求](memory/0052-product-requirements.html)
- [Agent 开发方法](memory/0053-how-to-build-an-agent.html)
- [用例与金标准](memory/0054-cases-and-gold-standard.html)
- [架构设计方法](memory/0055-architecture-design-method.html)
- [存储与检索策略](memory/0056-architecture-part1-storage.html)
- [检索侧设计](memory/0057-retrieval-pure-agent.html)
- [HTML 入库设计](memory/0058-html-ingestion-design.html)
- [评测台设计](memory/0059-evaluation-console-design.html)
- [技术选型](memory/0060-technology-selection.html)

## 开发原则

先做可运行的最小垂直切片，再按设计扩展；不从旧仓库复制代码或测试；基础件必须人工复核；所有变更必须能通过测试、trace 和评测解释。
