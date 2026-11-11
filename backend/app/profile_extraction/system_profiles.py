from __future__ import annotations

from app.profile_extraction.models import (
    ExtractionChannel,
    ExtractionProfileConfig,
    ProfileLanguagePolicy,
)
from app.profile_extraction.util import stable_hash

SYSTEM_TECHNICAL_PROFILE_ID = "ep_system_technical_learning_v1"


def profile_hash(config: ExtractionProfileConfig) -> str:
    """计算不含展示时间和通道 ID 的 Profile 语义 hash。"""

    payload = config.model_dump(mode="json")
    payload["extraction_channels"] = [
        {key: value for key, value in channel.items() if key != "channel_id"}
        for channel in payload["extraction_channels"]
    ]
    return stable_hash(payload)


def technical_profile_config() -> ExtractionProfileConfig:
    """返回首版只读“技术学习与参考”的确定性配置。"""

    return ExtractionProfileConfig(
        name="技术学习与参考",
        description="面向技术文档，保留可独立学习和检索的概念、机制、风险与关键对象。",
        domain_context="软件、系统、工具、工程方法或相关技术材料",
        primary_uses=[
            "学习一个新主题",
            "理解机制和原理",
            "查询操作方法与限制",
        ],
        representative_questions=[
            "这个系统或机制是什么，如何工作？",
            "某项任务应该如何完成？",
            "有哪些限制、风险和失败条件？",
        ],
        extraction_channels=[
            ExtractionChannel(
                channel_id="ec_1111111111111111",
                name="解释性知识对象",
                purpose="发现正文实质建立、可以脱离当前段落独立学习和检索的解释性知识对象。",
                activation=(
                    "正文对某一术语、概念、理论、规则、方法、过程、机制、分类、风险、"
                    "影响、比较框架或评估方式提供实质说明。"
                ),
                include_criteria=[
                    "存在稳定名称",
                    "正文具备定义、构成、作用、适用条件、效果、边界、比较或独立风险中的至少一项事实",
                ],
                exclude_criteria=["只有名称、章节标题、导航、链接、宣传语或顺带提及"],
                granularity_rule=(
                    "离开承载对象后仍能独立解释或检索的对象成为 candidate；属性、原因、"
                    "效果和普通细节留在承载对象的 evidence 中。"
                ),
                omission_retry_enabled=False,
            ),
            ExtractionChannel(
                channel_id="ec_2222222222222222",
                name="具名技术对象",
                purpose="发现正文实质讨论并具有稳定专名的技术系统、产品、组件、工具、模型或标准。",
                activation="正文用稳定专名指代对象，并说明其独立行为、作用、属性、边界、评价或结果。",
                include_criteria=["具有稳定专名", "该事实不是另一个对象事实的同义复述"],
                exclude_criteria=[
                    "仅作为端点、URL、接口调用、配置项、字段、参数、版本标签、位置或比较参照出现",
                ],
                granularity_rule=(
                    "一个具名对象对应一个 candidate；内部实现若本身有稳定名称、独立作用"
                    "和事实，可以独立成为 candidate，但不能充当所属对象的 alias。"
                ),
                omission_retry_enabled=False,
            ),
            ExtractionChannel(
                channel_id="ec_3333333333333333",
                name="技术结构模型",
                purpose="发现正文明确建立的技术结构关系。",
                activation="正文明确表达威胁／约束模型、架构位置决策或父机制结构。",
                include_criteria=[
                    "针对对象或环境说明所防范的风险、保护边界和残余限制",
                    "说明组件位于何处、从何处迁移、为何这样安排以及代价或效果",
                    "明确命名父机制，并给出实现、组成或具体应用",
                ],
                exclude_criteria=["普通概念、事件叙述、单个数据点、端点、配置项、参数和比较参照"],
                granularity_rule=(
                    "一个 candidate 只表达一种结构关系；父机制、具体实现和应用对象如需"
                    "同时存在，保持平级并分别绑定证据。"
                ),
                omission_retry_enabled=False,
            ),
            ExtractionChannel(
                channel_id="ec_4444444444444444",
                name="机制族",
                purpose="发现正文明确给出的父机制及其具体实现、组成项。",
                activation="同一句中先命名父机制，再通过括号、列表或枚举给出至少两个实现或组成项。",
                include_criteria=[
                    "父机制可以从同一句中直接识别",
                    "正文给出至少两个具体实现或组成项",
                ],
                exclude_criteria=[
                    "普通机制说明、独立控制项、风险、原则、事件、数据点、产品介绍、"
                    "端点、URL、配置项、字段、参数和版本标签",
                ],
                granularity_rule=(
                    "父机制和具有独立事实的具体实现分别成为 candidate，不得互为 alias，"
                    "不得由共现自动生成对象与机制组合 candidate。"
                ),
                omission_retry_enabled=False,
            ),
        ],
        exclude_criteria=[
            "导航、页脚、版本提示和宣传性套话",
            "没有正文解释的章节标题或链接文字",
            "仅出现一次且无法独立理解或检索的普通参数值",
            "与正文论述无关的作者、品牌和页面元信息",
        ],
        granularity="standard",
        additional_guidance="",
        language_policy=ProfileLanguagePolicy(),
    )


SYSTEM_TECHNICAL_PROFILE_HASH = profile_hash(technical_profile_config())
