# 设计文档

本目录收录 ai-study-helper v2 的当前设计基线。旧仓库资料不自动进入本项目的设计和实现上下文。

设计文档以 Markdown 为唯一源文件；GitHub 直接渲染。若后续需要独立文档站，应由这批 Markdown 生成，不另行维护一套 HTML 源文件。

## 当前状态

- 阶段：设计完成，进入实现启动。
- 实现状态：尚未开始业务代码。
- 下一步：建立 Tauri + Vue + Python sidecar 的最小可运行骨架。

## 设计基线

| 编号 | 主题 | 状态 |
| --- | --- | --- |
| [01](design/01-product-requirements.md) | 产品需求、问题空间、范围和质量属性 | 已审核 · 实现基线 |
| [02](design/02-how-to-build-an-agent.md) | Agent 开发方法与知识面 | 已审核 · 实现基线 |
| [03](design/03-cases-and-gold-standard.md) | 用例与金标准 | 已审核 · 实现基线 |
| [04](design/04-architecture-design-method.md) | 架构设计方法 | 已审核 · 实现基线 |
| [05](design/05-storage-and-retrieval-strategy.md) | 存储结构与检索策略 | 已审核 · 实现基线 |
| [06](design/06-retrieval-pure-agent.md) | 检索工具、Agent 循环、提示词、trace 与部署 | 已审核 · 实现基线 |
| [07](design/07-html-ingestion-design.md) | HTML 入库与 collection 快照 | 已审核 · 实现基线 |
| [08](design/08-evaluation-console-design.md) | 评测台、诊断与回归 | 已审核 · 实现基线 |
| [09](design/09-technology-selection.md) | 技术选型与总体架构 | 已审核 · 实现基线 |

01–09 已于 2026-10-20 审核，并作为当前实现基线。后续修改必须保留变更原因，同时更新本索引。

## 旧仓库

旧学习项目保留在 [lipeidonggz/ai-study-helper](https://github.com/lipeidonggz/ai-study-helper)。其中的代码、测试、数据与历史结论仅作参考，不构成本仓库的实现输入。
