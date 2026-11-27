# 5.1 v17 术语独立指称资格实验

## 目的

验证把术语资格收敛为单一词面规则后，能否同时做到：

1. 排除谓词、裸数值和句法片段；
2. 保留作者名、产品名和正文中独立使用的普通名词；
3. 改善同一快照多次编译之间的术语集合稳定性。

本实验不判断术语是否重要、是否技术、是否专业、是否常见，也不使用信息量作为资格条件。

## 方案

术语资格只有一条：完整词面必须在当前原文中能够独立作为名词性指称使用，即可以成为独立的名词、
名称、名词短语或习用缩写词面条目。

- 人物、组织、产品、文档和来源等完整名称可以成为术语；
- 谓词性、修饰性、数值和句子片段不能成为术语；
- 候选只作为更长名词性词面的组成部分出现时，不单独成立；
- 同一词面在块内另有独立指称用法时，按独立用法判断；
- 语义价值和检索用途留给概念义项、功能关系与在线检索，不在术语阶段提前评价。

本次版本为 `dual-entry-stage51-v17`、`stage51-induction-v16`、
`stage51-compile-v16` 和 `stage51-term-sense-v10-independent-reference`。

## 固定配置

| 项目 | 值 |
| --- | --- |
| 快照 | `s51_7f7e71a6b581c451` |
| 文档 | 《How we contain Claude across products》 |
| 原文块 | 26 |
| 候选出现／候选组 | 3401／2958 |
| 模型判断候选组 | 2699 |
| 模型 | `deepseek-flash` |
| 并发 | 8 |
| 最大输入字符 | 100000 |
| 最大输出 token | 8000 |
| 最大结构重试 | 2 |
| 温度 | 0 |

## 运行结果

| 运行 | 结果 | 总耗时 | 请求／尝试 | 失败／重试 | 输入 token | 输出 token | 术语 | 概念义项 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| run1 | 成功 | 140.79 秒 | 181／182 | 1／1 | 592436 | 202628 | 662 | 711 |
| run2 | 成功 | 182.85 秒 | 221／223 | 2／2 | 656679 | 243419 | 662 | 751 |

两次运行都完整发布，失败均在一次重试内恢复。术语数稳定为 662，但概念义项为 711 和 751，
两者仍有明显差距。

## 规则效果

v16 中明显不符合名词性独立指称的候选项在 v17 两次运行中均未进入最终术语集合，包括：

- `84`、`93`；
- `Across 25 retries`、`Delivered monthly`；
- `prefer`、`conceptualized`、`contained`、`attentive enough`；
- `Match isolation strength`、`read bash`；
- `any probabilistic defense`、`second approach`、`more approvals`、`less attention`、
  `different audience`、`two components`、`three categories`。

对应理由稳定落在“数词”“介词短语”“谓词性片段”“动宾片段”“依赖限定词或序数的临时描述”，
不再依赖信息量或技术性。

`agent`、`platform`、`layer`、`environment layer`、`model layer`、`Claude Code`、`egress`、
`sandbox` 和 `isolation` 在两次运行中均存在。`layer` 的独立用法被接受，明确作为更长词面组成
部分的用法被判为噪声，说明块内局部判断方向有效。

两次分别有 87 和 88 个 `accepted/mention_only` 候选组，作者名、产品名和裸名称得到保留。
但作者列表中的姓名首名也会作为局部名称被接受，例如 `Abel` 与 `Abel Ribbink` 同时出现。
`Special thanks` 和 `N/A` 也仍进入最终术语集合。这说明“只作为更长名称的一部分”仍未在所有
短名称和表格标签上稳定执行。

## 稳定性与成本

| 版本 | 术语数 | 术语交集 | 术语 Jaccard | 候选组翻转 | 翻转率 |
| --- | ---: | ---: | ---: | ---: | ---: |
| v14 run4／run5 | 256／264 | 246 | 0.8978 | 37 | 1.25% |
| v16 run1／run2 | 556／544 | 516 | 0.8836 | 99 | 3.35% |
| v17 run1／run2 | 662／662 | 653 | 0.9732 | 53 | 1.79% |

v17 的术语集合稳定性是三次方案中最好的。术语数虽然相同，但集合并非完全一致：两次各有 9 个
独有术语。53 个翻转中 47 个改变术语资格，6 个只改变义项状态；最终只有 9 个术语没有跨运行
保留下来，说明多数局部翻转通过其他支持块收敛。

术语规则改善带来了明显的下游成本：

- 进入概念归并的草稿约 932～940，高于 v16 的 726～740；
- 总请求 181～221，高于 v16 的 115～126；
- 总耗时 140.79～182.85 秒，高于 v16 的 98.76～111.22 秒。

## 结论

1. “可独立指称的名词性词面”作为唯一术语资格规则有效，明显优于“信息增量”。
2. 两轮术语数均为 662，Jaccard 0.9732，术语入口稳定性显著改善。
3. 明显谓词、裸数值和句子片段已经稳定排除，同时作者名、产品名和独立普通名词得到保留。
4. 当前代价是术语、概念义项和归并成本整体增加；概念义项数仍为 711 和 751，尚未稳定。
5. 仍有局部名称和标签边界残余，例如姓名首名、`Special thanks` 和 `N/A`。D39 保持待确认。

## 复现

```powershell
.\backend\.venv\Scripts\python.exe .\scripts\wiki_stage51_probe.py `
  --snapshot .\backend\.runtime\dual_entry_index_stage51_091\s51_7f7e71a6b581c451 `
  --output-dir .\backend\.runtime\dual_entry_index_stage51_v17_independent_reference_run1 `
  --concurrency 8 --max-input-chars 100000 `
  --max-output-tokens 8000 --max-retries 2
```

运行产物保存在本地 `backend/.runtime/`，不提交 Git。

## 产物

- run1：`backend/.runtime/dual_entry_index_stage51_v17_independent_reference_run1/km51_2b5550dd4e4dc414`
- run2：`backend/.runtime/dual_entry_index_stage51_v17_independent_reference_run2/km51_28f39a9368a495ac`
