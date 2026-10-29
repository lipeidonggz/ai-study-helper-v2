# 062 · retired case revision 的历史判定读取

- 状态：已完成
- 开始日期：2026-10-29
- 关联设计：[08 评测台](../../design/08-evaluation-console.md)
- 前置切片：[061 cf-001 透明目标选择](061-cf001-transparent-target-selection.md)
- 人工验收：在评测台打开引用 `cr_cf_001_c16a9d61af9c` 的历史判定，确认矩阵和详情不再报错

## 控制契约

### 背景与失败场景

- 触发问题：历史 judgment 的冻结配置覆盖了 `cr_cf_001_c16a9d61af9c`；该 revision 在 cf-001 升级到下一版后变成 `retired`，界面读取判定时报“override case revision is not reviewable”。
- 不处理时的用户损失：历史 run 和判定虽然仍存在，但无法从评测台查看，影响结果复核与故障追溯。
- 现有流程为什么不足：读取历史判定和发起新重判共用了同一套 override 可审核状态校验。
- 本切片负责什么：区分“历史读取”和“发起新重判”的状态约束，并让新重判默认使用当前 active revision。
- 本切片不负责什么：不恢复 retired revision 为 active，不修改历史 judgment，不改变 Judge 判定语义，也不在界面增加 revision 选择器。
- 什么情况下应停止或撤掉本机制：若允许 retired revision 读取会绕过新判定的 review 门禁，则必须回退并重新设计投影方式。

### 用户可观察目标

case revision 被后续版本替代后，引用旧 revision 的历史 judgment 仍能在评测台正常显示矩阵、样本详情和判定理由。

### 核心不变量

1. 读取既有 judgment 时，其冻结 override 可以使用 `reviewed`、`active` 或 `retired` revision。
2. 发起新重判且未显式指定 override 时，默认解析到当前 active revision，并把解析结果写入新 judgment 配置。
3. 发起新重判时不能使用 retired revision；面向用户选择 revision 的入口以后单做。
4. 历史读取只改变状态校验，不修改 judgment、case revision 或 run；历史矩阵仍显示当时冻结的 revision。

### 错误代价与非对称门禁

| 错误类型 | 具体例子 | 用户影响 | 处理分类 | 指标与阈值 |
| --- | --- | --- | --- | --- |
| 历史不可读 | 旧 revision retired 后详情报错 | 无法复核历史结论 | 阻断 | 旧 judgment detail 必须可读 |
| 新重判默认使用旧 revision | 新 case revision 生效后仍按旧金标准判定 | 新结论不符合当前规则 | 阻断 | 无显式 override 时必须解析到 active |
| 使用 retired 发起新重判 | 绕过新 revision review 门禁 | 产生不可信新判定 | 阻断 | 新 judgment 启动必须拒绝 |

### 明确禁止

- 不得为了让历史可读而允许 `draft` revision 进入历史投影。
- 不得把历史 override 自动改写成当前 active revision。
- 不得删除或覆盖原 judgment、binding 或 invocation。

### 失败语义

| 场景 | 用户看到什么 | 系统保留什么 | 是否可恢复 |
| --- | --- | --- | --- |
| 历史 override 为 retired | 正常显示历史结果 | 原 revision 与 judgment | 不需要恢复 |
| 新重判未指定 revision | 默认使用当前 active revision | 新 judgment 配置记录实际映射 | 不需要恢复 |
| 新重判使用 retired | 明确拒绝并说明 revision 不可审核 | 现有 run 与判定不变 | 是，等待后续 revision 选择器或传 reviewed／active revision |

### 人工检查点

- 编码前：确认问题发生在读取历史判定，而不是新判定预检。
- 完成后：沛东在评测台打开截图中失败的历史判定并视觉确认。

### 决策预算

- 预计 B 级决策数量：0。
- 是否存在 C 级决策：否。
- 超过预算时的拆分方式：不适用。

## 目标

解除历史判定读取对 case revision 当前审核状态的错误依赖，同时保留新判定的 review 门禁。

## 输入与输出

- 读取输入：包含冻结 `case_revision_overrides` 的历史 evaluation revision。
- 重判输入：空 override 表示使用每个 case 的当前 active revision。
- 输出：历史读取保留冻结 revision；新判定将实际解析出的 override 写入配置。

## 不包含

- 不改变前端矩阵和详情组件。
- 不增加用户手动选择 revision 的界面。
- 不迁移历史数据库记录。
- 不处理 criterion 元数据缺失导致的独立问题。

## 验收矩阵

| ID | 用户目标／风险 | 要证明的行为 | 指标或反例 | 阻断阈值 | 验证层级 |
| --- | --- | --- | --- | --- | --- |
| A1 | 旧 revision retired 后历史仍可读 | `get_revision` 成功且矩阵保持旧 revision | 不得抛 reviewable 错误 | 抛错即阻断 | 集成 |
| A2 | run 的历史列表仍包含该判定 | `list_run_revisions` 成功 | 旧 revision 出现在列表 | 缺失即阻断 | 集成 |
| A3 | 新判定默认使用最新规则 | 空 override 解析为各 case 的 active revision 并写入配置 | 不得沿用旧 revision | 不一致即阻断 | 集成 |
| A4 | 历史事实不被迁移 | case aggregate revision ID 等于冻结 override | 不得变成当前 active | 不一致即阻断 | 集成 |
| A5 | 新判定门禁不被放宽 | retired override 启动新判定被拒绝 | 必须抛出不可审核错误 | 未拒绝即阻断 | 集成 |

## 验收执行

- [x] A1、A2、A3、A4、A5 已由集成测试覆盖。
- [x] 使用真实本地数据库读取截图对应的历史 judgment。
- [x] 沛东在界面完成视觉验收。

## 决策账本

| ID | 等级 | 问题 | 选择 | 设计依据 | 候选与取舍 | 影响 | 验证 | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| D1 | A | 历史读取是否接受 retired override | 接受，但新重判继续拒绝 | 历史 revision 是冻结事实，读取与创建语义不同 | 直接放宽全部校验会破坏新判定门禁 | 历史详情可读，新判定约束不变 | 集成测试与真实数据库读取 | 已确认 |
| D2 | A | 新重判未指定 revision 时用哪一个 | 默认使用当前 active revision，并在新配置中冻结实际映射 | 沛东确认新判定应使用最新规则 | 沿用 attempt 原 revision 会让新判定使用旧金标准 | 新 judgment 反映最新规则，历史 judgment 不受影响 | 集成测试 | 已确认 |

## 证据包

### 自动化证据

- 命令：`backend\.venv\Scripts\python.exe -m pytest tests\test_evaluation_judgment.py -q`
- 结果：36 passed。

### 运行证据

- 历史 judgment：`evalrev_baca4055e7164860b4050c62933d872f`
- 冻结 case revision：`cr_cf_001_c16a9d61af9c`
- 该 revision 当前状态：`retired`
- 直接读取结果：`complete`，case aggregate 保持原 revision。
- `list_run_revisions`：成功返回 4 个 revision，并包含该 judgment。

### 失败探针

- 将测试 override revision 标记为 retired 后读取旧 judgment，读取成功。
- 不传 override 发起重判时，配置自动写入替代后的 active revision。
- 同一 retired revision 用于启动新判定，明确抛出 `override case revision is not reviewable`。

## 反向解释

| 编号 | 问题 | 负责人回答摘要 | 核对结果 | 跟进 |
| --- | --- | --- | --- | --- |
| R1 | 为什么历史读取可以接受 retired？ | 否则已绑定的历史判定无法读取和复核。 | 通过 | 保留历史只读能力 |
| R2 | 为什么新重判仍不能使用 retired？ | retired 是被替代的旧规则，新判定应使用最新 active revision；允许 retired 会乱套。 | 通过 | 默认解析 active，retired 拒绝 |
| R3 | 修复后历史矩阵应显示哪个 case revision？ | 显示 judgment 当时冻结的 revision，不跟随当前 active。 | 通过 | 历史投影保持冻结 |

<!-- reverse-explanation-gate: passed 3/3 -->

## 已知风险与后续

- 未验证项：无。
- 接受的风险：历史判定继续展示旧 criterion 原文，这是冻结记录的正确行为。
- 后续待办：无。

## 停止与推翻条件

- 若读取 retired override 需要修改或替换历史 binding，则停止当前方案，改用独立的历史快照投影模型。
