from __future__ import annotations

import hashlib
import json

from app.providers.chat import ChatMessage
from app.retrieval.models import RetrievalBlock
from app.wiki.topic_models import TopicIdentityGroup

TOPIC_MAP_PROMPT_VERSION = "topic-map-v1"
TOPIC_IDENTITY_PROMPT_VERSION = "topic-identity-v2"

_TOPIC_MAP_SYSTEM_PROMPT = """你是知识库的主题地图编译器。
你的任务是从给定证据块中发现值得作为 Wiki 主题入口的语义对象，
并为它们绑定真实来源块。

必须遵守：
1. 只能使用用户消息中的 evidence_blocks，不得引入外部知识、答案或目标主题。
2. 每个 candidate 和 coverage_slot 必须列出至少一个 evidence_blocks 中实际存在的 block_id。
3. 统一返回主题候选，不要强行划分实体、概念或主题。semantic_tags 只是可选描述标签，不是互斥分类。
4. 候选应覆盖具体对象、机制、方法、流程、风险、条件、边界和评估方式中确实被证据实质讨论的内容。
5. 仅仅顺带出现、没有具体信息支撑的词不作为 candidate。
6. aliases 只放同指一物的全称、缩写、翻译、旧名或明确替代名，不放相关概念。
7. coverage_slots 只记录当前证据能够观察到的检查面；不要编造该主题应当存在的方面。
8. 如果本窗口没有值得建入口的候选，返回 {"candidates":[]}。
9. 只返回 JSON 对象，不得增加结构外字段，不得使用 Markdown 代码围栏。

输出结构：
{
  "candidates": [
    {
      "name": "...",
      "aliases": ["..."],
      "summary": "一句话，说明这个主题是什么",
      "semantic_tags": ["..."],
      "source_block_ids": ["..."],
      "coverage_slots": [
        {
          "label": "...",
          "question": "这个主题下值得继续核对的语义面",
          "source_block_ids": ["..."]
        }
      ]
    }
  ]
}"""

_GRANULARITY_GUIDANCE = {
    "focused": (
        "只保留少量最核心、贯穿当前证据窗口的主题入口。优先覆盖理解本窗口所必需的主要对象和机制。"
    ),
    "standard": (
        "保留被实质性讨论的主题入口。"
        "一个对象、机制、方法、流程、风险或边界应至少获得一句具体事实、"
        "多个要点或一个专门段落，才可进入候选。"
    ),
    "exhaustive": (
        "在不制造噪声的前提下尽量完备。保留所有被具体讨论的具名对象、技术、方法、"
        "机制、风险、条件、操作边界和评估方式；仍需忽略纯泛词和无信息顺带提及。"
    ),
}

_TOPIC_IDENTITY_SYSTEM_PROMPT = """你是知识库主题身份归一的严格判定器。
你的任务是判断候选组是否指向完全相同的知识入口。

必须遵守：
1. 只能把 candidate group 合并到同一输入中列出的 compare group。
2. 不得发明 group_id。
3. related != same；同领域、同类别、上下位概念、不同版本或经常共现都不等于同一身份。
4. semantic_tags 不作为身份依据，不得因为类型标签不同而拒绝合并。
5. aliases、翻译、明确缩写、旧名和完整名称是支持同一身份的证据，但单独共享一个模糊缩写不充分。
6. 不确定时保持分开。

只返回如下 JSON 对象：
{"merges":{"source_group_id":"target_group_id"}}"""

TOPIC_MAP_PROMPT_HASH = hashlib.sha256(_TOPIC_MAP_SYSTEM_PROMPT.encode("utf-8")).hexdigest()
TOPIC_IDENTITY_PROMPT_HASH = hashlib.sha256(
    _TOPIC_IDENTITY_SYSTEM_PROMPT.encode("utf-8")
).hexdigest()


def build_topic_map_messages(
    *,
    doc_title: str,
    source_label: str,
    granularity: str,
    blocks: list[RetrievalBlock],
) -> list[ChatMessage]:
    """构造与具体用例无关的主题地图抽取提示。"""

    guidance = _GRANULARITY_GUIDANCE.get(granularity, _GRANULARITY_GUIDANCE["standard"])
    payload = {
        "document": {
            "title": doc_title,
            "source_label": source_label,
            "granularity_guidance": guidance,
        },
        "evidence_blocks": [
            {
                "block_id": block.block_id,
                "section_path": block.section_path,
                "text": block.text,
            }
            for block in blocks
        ],
    }
    return [
        ChatMessage(role="system", content=_TOPIC_MAP_SYSTEM_PROMPT),
        ChatMessage(
            role="user",
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        ),
    ]


def build_topic_identity_messages(groups: list[TopicIdentityGroup]) -> list[ChatMessage]:
    """构造只含候选身份的批量判同提示。"""

    payload = {
        "candidate_groups": [group.model_dump(mode="json") for group in groups],
    }
    return [
        ChatMessage(role="system", content=_TOPIC_IDENTITY_SYSTEM_PROMPT),
        ChatMessage(
            role="user",
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        ),
    ]
