# 073 · Wiki Incremental Build

- 状态：已完成（初版实现与自动验证、集中反向解释通过）
- 开始日期：2026-10-31
- 关联设计：[11 LLM Wiki 编译设计](../../design/11-llm-wiki-compilation.md)
- 前置切片：071；072
- 人工验收：最终集中进行

## 控制契约

### 用户可观察目标

新增、更新或删除文档时，系统基于上一 Wiki build 构建新 snapshot 的完整 Wiki build；未变主题身份保持，受影响主题证据和页面按当前 snapshot 重建。

### 核心不变量

1. 新 build 只能引用新 snapshot 的 block。
2. 文档删除后，其 block refs 不出现在 active build。
3. 未变化的 topic ID 尽量稳定。
4. 单源页在来源消失后不再发布。
5. 失败增量 build 不切换 pointer。

### 明确禁止

- 不以“可重建”为由永久保留旧 block refs。
- 不把文档更新当新增造成重复 topic。
- 不把增量失败伪装成全量成功。
- 不覆盖历史 Wiki build。

## 输入与输出

输入：新 snapshot、上一 build ID、变更文档集合。

输出：`base_build_id`、新 topic maps、保留／新增／更新／撤回主题清单、新 page set、新 active pointer。

## 验收矩阵

| ID | 行为 | 阻断阈值 | 层级 |
| --- | --- | --- | --- |
| 073-A1 | 新文档只新增受影响 map | 无关 map 重算=0 | 集成 |
| 073-A2 | 更新移除旧版本 refs | 旧 refs=0 | 集成 |
| 073-A3 | 删除更新页面和链接 | 悬空=0 | 集成 |
| 073-A4 | 未变 topic ID 稳定 | 漂移=0 | 集成 |
| 073-A5 | 失败 build 不改 active pointer | 切换=0 | 集成 |

## 决策账本

| ID | 等级 | 选择 | 状态 |
| --- | --- | --- | --- |
| 073-D1 | B | 按 `doc_id + doc_version` 判定变更 | 已确认（2026-11-01） |
| 073-D2 | B | 每篇文档保存独立 topic-map ref | 已确认（2026-11-01） |
| 073-D3 | B | 身份账本作为稳定 ID 来源 | 已确认（2026-11-01） |
| 073-D4 | B | 只重写受影响 narrative pages，未变页复制引用 | 已确认（2026-11-01） |
| 073-D5 | B | 未变文档复用 map，但 ledger 以全量 map＋base ledger 重新归一 | 已确认（2026-11-01） |

## 证据包与反向解释

- 自动化：新增、删除、未变页复制、受影响主题和 `base_build_id` 路径均通过；确定性 coverage slot 排序回归同时通过。
- 真实故障：未变主题曾因 coverage slot 顺序漂移被判定为受影响。修复后 fingerprint 与产物顺序均稳定。
- 已知边界：真实 15 篇实验主要验证全量路径；100／1,000 篇增量成本和中断续跑仍使用工程测试或后续规模实验。
- 反向解释：R1 文档更新如何移除旧证据；R2 为什么未变 topic ID 要复用；R3 为什么删除必须发布新 build。
- 集中反向解释：[R1–R5](../experiments/2026-11-01-llm-wiki-compilation.md#集中反向解释)，5/5 通过。

## 停止条件

若无法稳定计算变更范围或删除后仍残留旧证据，则停止 Agent 接入。

<!-- reverse-explanation-gate: passed 5/5 -->
