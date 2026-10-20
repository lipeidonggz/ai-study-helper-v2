# 设计文档

本目录收录 ai-study-helper v2 的当前设计基线。旧仓库资料不自动进入本项目的设计和实现上下文。

设计文档以 Markdown 为唯一源文件；GitHub 直接渲染。若后续需要独立文档站，应由这批 Markdown 生成，不另行维护一套 HTML 源文件。

## 当前状态

- 阶段：实现阶段，第一个垂直切片已完成。
- 实现状态：第一个桌面 sidecar 最小启动切片已完成。
- 下一步：验证 PyInstaller one-folder sidecar 与 Windows 打包链。

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

## 记录分类

实现阶段不在设计基线中追加流水账。不同类型的记录使用不同入口：

| 位置 | 用途 | 命名 |
| --- | --- | --- |
| [`design/`](design/) | 已审核的产品与架构设计基线 | `NN-短描述.md` |
| [`decisions/`](decisions/) | 技术取舍与 ADR | `NNNN-短描述.md` |
| `development/slices/` | 垂直切片的实现记录 | `NNN-短描述.md` |
| `development/experiments/` | 性能、质量和兼容性实验 | `YYYY-MM-DD-短描述.md` |
| `operations/` | 安装、排障、备份与升级说明 | 按使用场景命名 |

临时任务、进行中状态和开放式讨论优先放在 GitHub Issue、Project 或 PR；只有形成长期结论后才整理进 `docs/`。

当前已记录：

- [0001 · 文档与实现记录结构](decisions/0001-documentation-and-record-structure.md)
- [001 · 桌面 sidecar 最小启动链（已完成）](development/slices/001-desktop-sidecar-bootstrap.md)

## 旧仓库

旧学习项目保留在 [lipeidonggz/ai-study-helper](https://github.com/lipeidonggz/ai-study-helper)。其中的代码、测试、数据与历史结论仅作参考，不构成本仓库的实现输入。
