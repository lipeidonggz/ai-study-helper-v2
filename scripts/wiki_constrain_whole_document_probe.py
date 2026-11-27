"""一次输入整篇文档，抽取约束关系并与逐块基线做结构化和用量对比。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

import keyring
from openai import AsyncOpenAI

SCRIPT_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = SCRIPT_ROOT.parent / "backend"
sys.path.insert(0, str(SCRIPT_ROOT))
sys.path.insert(0, str(BACKEND_ROOT))

from app.dual_entry_index.snapshot import SnapshotLoader
from wiki_constrain_block_level_probe import RELATION_CARDS, RelationCard

DEFAULT_SNAPSHOT = Path(
    r"C:\Users\lida\AppData\Local\com.lipeidong.aistudyhelper\collections\s_6b787258ba7d700c"
)
DEFAULT_DOCUMENT_LABEL = "D04"
DEFAULT_RELATION_ID = "constrain"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
CREDENTIAL_SERVICE_NAME = "ai-study-helper"
CHAT_API_KEY_USERNAME = "chat-api-key"
DEFAULT_BASELINE_GLOB = "constrain_prompt_layout_v2_behavior_r*"
GOLD_FIXTURE = BACKEND_ROOT / "tests" / "fixtures" / "c13_003_wiki_topic_v2_targets.json"


@dataclass(frozen=True, slots=True)
class DocumentBlock:
    """整篇输入中保留稳定定位身份的原文块。"""

    suffix: str
    block_id: str
    block_index: int
    section_path: tuple[str, ...]
    text: str


@dataclass(frozen=True, slots=True)
class DocumentInput:
    """一篇文档按原文顺序组织的完整输入。"""

    label: str
    doc_id: str
    title: str
    blocks: tuple[DocumentBlock, ...]


def _write_json(path: Path, payload: Any) -> None:
    """写入实验产物，统一保留中文和稳定缩进。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _block_suffix(block_id: str) -> str:
    """从完整块标识中取出文档内稳定后缀。"""

    return block_id.rsplit("#", 1)[-1]


def _load_document(snapshot_path: Path, document_label: str) -> tuple[DocumentInput, Any]:
    """按快照文档顺序读取指定标签的完整文档。"""

    match = re.fullmatch(r"D(\d+)", document_label)
    if match is None:
        raise SystemExit(f"document label must look like D04: {document_label}")
    document_index = int(match.group(1)) - 1
    snapshot = SnapshotLoader().load(snapshot_path)
    if document_index < 0 or document_index >= len(snapshot.documents):
        raise SystemExit(f"document label is outside snapshot range: {document_label}")
    document = snapshot.documents[document_index]
    source_blocks = sorted(
        (block for block in snapshot.blocks if block.doc_id == document.doc_id),
        key=lambda block: block.block_index,
    )
    if not source_blocks:
        raise SystemExit(f"document has no blocks: {document_label}")
    blocks = tuple(
        DocumentBlock(
            suffix=_block_suffix(block.block_id),
            block_id=block.block_id,
            block_index=block.block_index,
            section_path=tuple(block.section_path),
            text=block.text,
        )
        for block in source_blocks
    )
    return (
        DocumentInput(
            label=document_label,
            doc_id=document.doc_id,
            title=source_blocks[0].doc_title,
            blocks=blocks,
        ),
        snapshot,
    )


def _render_document(document: DocumentInput) -> str:
    """渲染保留块身份的整篇原文；标识和章节路径明确标为元数据。"""

    rendered_blocks: list[str] = []
    for block in document.blocks:
        section_path = " > ".join(block.section_path) or "（无章节路径）"
        rendered_blocks.append(f"[BLOCK {block.suffix}]\n[SECTION {section_path}]\n{block.text}")
    return "\n\n".join(rendered_blocks)


def _build_prompt(
    relation_card: RelationCard,
    document: DocumentInput,
    *,
    input_scope_label: str = "整篇文档",
    source_description: str = "完整文档",
    include_theme_independent_candidate_rule: bool = False,
) -> str:
    """构造单次多块判断提示词，并将原文置于稳定前缀之后。

    默认参数保持整篇文档实验的既有语义；分批探针通过显式标签说明当前输入只包含
    原文的一部分，避免模型把批外未提供的 BLOCK 误认为不存在。
    """

    input_noun = "全文" if input_scope_label == "整篇文档" else "本批输入"
    output_scope_label = "本文档" if input_scope_label == "整篇文档" else input_scope_label
    theme_independent_candidate_rule = (
        "\n15. 候选发现不得根据文档主题、批次主题或段落主旨进行预筛选。即使一句话看起来只是背景、示例、补充说明、附加信息，或与当前主题关联较弱，只要其中可能有一个对象、行动或状态被机制、条件或动作限定到更小范围，就必须纳入候选并按定义校验。主题上下文只能在候选发现之后用于解释目标、原因和范围消歧，不能作为跳过候选的理由。"
        if include_theme_independent_candidate_rule
        else ""
    )

    return f"""你是{input_scope_label}关系判定器。你的任务不是从{input_noun}中挑选少量代表性证据，而是逐个检查所有原文 BLOCK，再汇总所有符合定义的关系。

判断目标：

{relation_card.display_name}：{relation_card.definition}

证据与推断边界：

{relation_card.evidence_boundary}

工作规则：

1. 必须按输入顺序检查全部 {len(document.blocks)} 个 BLOCK，不得跳过任何 BLOCK。blocks 必须包含每个 BLOCK 恰好一次，block_id 顺序必须与输入顺序完全一致。
2. 对每个 BLOCK 判断它本身是否独立表达至少一个“{relation_card.display_name}”。独立表达表示该 BLOCK 内已有足够的原文支持该关系，不依赖其他 BLOCK 才能成立。
3. 不同 BLOCK 重复或重述同一种{relation_card.display_name}时，每个 BLOCK 都应判为 has_constraint=true；后文出现更具体的例子，不会使前文的总述、总结、定义或原则变成无效或重复证据。禁止因为语义相似而做跨 BLOCK 去重。
4. 不需要整个 BLOCK 的主要目的就是{relation_card.display_name}；只要 BLOCK 中有一处明确表达该含义，就应判为 true。
5. 可以使用{input_scope_label}的上下文帮助判断目标、原因和范围；但限定动作、被限定对象、缩小后的范围以及“范围被缩小”这一关系本身，必须能由该 BLOCK 内返回的连续原文证据直接支持。
6. 每个直接证据段必须是所在 BLOCK 中的最小连续原文片段。一句话已经足够时，不要返回整个 BLOCK。
7. 同一 BLOCK 中多个彼此独立的{relation_card.display_name}应分别返回证据段；多个相邻句子共同表达同一个{relation_card.display_name}时可以合并为一个连续证据段。
8. has_constraint=true 的 BLOCK 必须在自己的 relations 列表中至少有一条证据；has_constraint=false 的 BLOCK 必须返回空的 relations 列表。不要只返回{input_noun}中最好的证据，必须返回所有独立成立的证据位置。
9. 判否前必须进行块内局部复核。closest_candidates 只用于 has_constraint=false 的 BLOCK，记录你在该 BLOCK 中找到的、最接近但最终未达到{relation_card.display_name}定义的原文候选。最多返回两条；如果块内没有任何句子与定义中的主体、范围、边界、可见性或限定作用相关，可以返回空数组。
10. has_constraint=true 时，closest_candidates 必须为空数组，不得保留任何其他未成立候选。has_constraint=false 时，closest_candidates 中的候选不能被同时写入 relations。每条 why_not 必须指出该原文还缺哪一个定义要素，或哪个词语不能据此推断出{relation_card.display_name}；不得只写“主题不同”“不是主要目的”或概括整个 BLOCK 的内容。
11. blocks 中每个块记录的字段顺序固定为 block_id、relations、closest_candidates、has_constraint、reason：先列出该块已成立的关系、判否前检查过的候选，再写最终判断和判断理由。relations 是证据列表，不是对全篇关系的摘要或去重结果。
12. quote 必须逐字复制所在 BLOCK 的原文，保留原文标点和语序，不得翻译、概括或改写。BLOCK 标识和 SECTION 元数据不是原文，不得写入 quote。relations 和 closest_candidates 中的 quote 都适用此要求。
13. 每个 reason 只说明所在 BLOCK 或对应证据如何表达上述定义，不新增定义之外的判断要求。has_constraint=false 时，reason 应与 closest_candidates 的复核结果一致。
14. 本任务只判断单个 BLOCK 内独立成立的{relation_card.display_name}，不输出跨块关系。{theme_independent_candidate_rule}

只输出一个 JSON 对象。

输出结构：

{{
  "relation": "{relation_card.relation_id}",
  "blocks": [
    {{
      "block_id": "b00000",
      "relations": [],
      "closest_candidates": [
        {{
          "quote": "该 BLOCK 中最接近但未满足定义之一的逐字连续原文",
          "why_not": "该原文缺少哪一个定义要素，或为何不能据此成立{relation_card.display_name}"
        }}
      ],
      "has_constraint": false,
      "reason": "该 BLOCK 没有独立表达约束的原因"
    }},
    {{
      "block_id": "b00001",
      "relations": [
        {{
          "quote": "只属于该 BLOCK 的逐字连续原文",
          "reason": "该原文如何表达{relation_card.display_name}"
        }}
      ],
      "closest_candidates": [],
      "has_constraint": true,
      "reason": "该 BLOCK 如何独立表达约束"
    }}
  ]
}}

上面的 JSON 只是字段结构示例。实际输出必须把{output_scope_label}的全部 {len(document.blocks)} 个 BLOCK 按原文顺序逐一写入 blocks，不得只输出示例中的两个 BLOCK。即使{input_scope_label}没有任何{relation_card.display_name}，也必须输出全部 {len(document.blocks)} 个块记录：has_constraint=false 的块填写判否前复核过的 closest_candidates；has_constraint=true 的块必须把 closest_candidates 写成空数组。每个块都要写明 reason。

文档：{document.label} {document.title}

下面是按原文顺序提供的{source_description}。块标识和章节路径只用于定位证据，不是原文内容。

\"\"\"
{_render_document(document)}
\"\"\""""


def _build_structured_candidate_prompt(
    relation_card: RelationCard,
    document: DocumentInput,
) -> str:
    """构造结构化候选提示词，由 runtime 根据候选字段是否齐全派生关系成立。"""

    candidate_boundary = """候选提取边界：

候选必须表达原文中真实存在的范围收紧关系，而不是把一条建议、价值判断或最佳实践改写成三个结构字段。

1. limiter 必须是原文表达的、能够实际约束目标的具体机制、规则、边界、条件或配置。它不能只是“建议”“应该”“需要注意”“常见做法”“when needed”“when appropriate”“when beneficial”这类泛化决策准则。
2. target 必须是 limiter 实际约束的对象、行动、状态、输入或结果。
3. excluded_case 必须能由 quote 中的 limiter 直接推出，不能为了填满结构而虚构一个可能后果。
4. 如果原文只是在比较、说明使能条件、强调重要性、描述适用场景、建议一种做法，或在多个方案中进行选择，即使可以各自想象出“受限对象”和“被排除情形”，也不能据此建立候选。
5. 机制被描述为已经存在、已被采用、允许配置、常见做法还是仅被建议，本身不决定候选是否保留。判断依据是：该表达中是否同时存在真实的限制机制及其排除范围。
6. 缺少任一结构，就保留候选并将对应字段填为 null；如果原文没有明确的范围收紧关系，则返回空候选。"""

    return f"""你是整篇文档关系候选抽取器。你的工作不是最终判断每个 BLOCK 是否属于“{relation_card.display_name}”，而是保留并结构化表达原文中可能参与该关系的候选。最终是否成立由 runtime 根据候选字段是否齐全统一派生。

关系定义：

{relation_card.display_name}：{relation_card.definition}

{candidate_boundary}

抽取任务：

1. 必须按输入顺序检查全部 {len(document.blocks)} 个 BLOCK，不得跳过任何 BLOCK。blocks 必须包含每个 BLOCK 恰好一次，block_id 顺序必须与输入顺序完全一致。
2. 对每个 BLOCK，找出原文直接表达“某个东西把另一个对象的可能性、可接受性、可见性或可执行集合压到更小部分”的连续原文片段，或至少明确出现真实限制机制的连续片段。不要只挑选最像关系的少数片段；同一 BLOCK 中彼此独立的候选应分别记录。
3. 不同 BLOCK 重复或重述同一种关系时，每个 BLOCK 都独立记录自己的候选。禁止根据语义相似做跨 BLOCK 去重，也不输出跨 BLOCK 关系。
4. 可以使用整篇文档的上下文解释候选，但每个候选必须由一个属于当前 BLOCK 的最小连续原文片段直接支持。不要因为相邻 BLOCK 或全文主题才把候选加入当前 BLOCK。
5. 不要因为表达方式被描述为“强制要求”“建议”“常见做法”“可选配置”或“最佳实践”而删除一个已经包含真实 limiter 和排除范围的候选。但一条只有泛化建议、重要性、适用性或方案选择的句子不能仅靠补齐 target 和 excluded_case 变成候选。
6. 每个候选必须填写以下五个字段：
   - quote：当前 BLOCK 中逐字复制的连续原文。
   - limiter：原文中执行限制或缩小作用的机制、规则、条件、动作或配置；无法确定时为 null。
   - target：可能性、可接受性、可见性或可执行集合被缩小的对象、行动、状态、输入或结果；无法确定时为 null。
   - excluded_case：被排除、禁止、跳过、阻止或不再允许发生的一个具体情形；无法确定时为 null。
   - reason：简要说明 quote 中哪些原文分别支持 limiter、target 和 excluded_case，不写最终是否成立的结论。
7. limiter、target、excluded_case 必须是三个不同角色的具体内容。不要把同一短语复制到多个字段来凑齐结构；也不要把“建议”“应该”“需要”等泛化评价词单独当作 limiter。
8. excluded_case 必须是一个具体情形，而不是“某些情况”“不合适的操作”这类没有信息量的占位表达。若只能判断有某种限制，但无法确定至少一个具体被排除情形，excluded_case 填 null。
9. 候选字段可以在整篇上下文帮助下解释，但不得补写原文没有表达的机制、目标或排除情形。无法确定的字段必须保留为 null，不要猜测后填满。
10. 原文没有候选的 BLOCK 返回 candidates: []；有候选的 BLOCK 返回全部候选。每个 BLOCK 的 reason 只说明该 BLOCK 是否存在值得保留的候选，以及候选列表的整体情况，不输出 has_constraint、present 或任何布尔判断。
11. quote 必须逐字复制所在 BLOCK 的原文，保留原文标点和语序，不得翻译、概括或改写。BLOCK 标识和 SECTION 元数据不是原文，不得写入 quote。

只输出一个 JSON 对象，不要输出 Markdown 代码围栏或额外说明。

输出结构：

{{
  "relation": "{relation_card.relation_id}",
  "blocks": [
    {{
      "block_id": "b00000",
      "candidates": [],
      "reason": "该 BLOCK 没有值得保留的结构化候选"
    }},
    {{
      "block_id": "b00001",
      "candidates": [
        {{
          "quote": "只属于该 BLOCK 的逐字连续原文",
          "limiter": "原文中的限制来源；无法确定时为 null",
          "target": "原文中的被限制对象、行动、状态、输入或结果；无法确定时为 null",
          "excluded_case": "原文支持的一个具体被排除情形；无法确定时为 null",
          "reason": "quote 中分别支持三个结构字段的原文表达"
        }}
      ],
      "reason": "该 BLOCK 有一条或多条结构化候选"
    }}
  ]
}}

上面的 JSON 只是字段结构示例。实际输出必须把本文档的全部 {len(document.blocks)} 个 BLOCK 按原文顺序逐一写入 blocks，不得只输出示例中的两个 BLOCK。

文档：{document.label} {document.title}

下面是按原文顺序提供的完整文档。块标识和章节路径只用于定位证据，不是原文内容。

\"\"\"
{_render_document(document)}
\"\"\""""


def _normalized_whitespace(text: str) -> str:
    """仅归一化空白差异，用于识别只是换行格式不同的逐字证据。"""

    return " ".join(text.split())


def _validate_quote(quote: Any, *, label: str, block: DocumentBlock) -> str | None:
    """校验逐字引用属于当前块，并原地记录精确或空白归一化匹配状态。"""

    if not isinstance(quote, str) or not quote.strip():
        return f"{label} quote is missing"
    if quote in block.text:
        return None
    if _normalized_whitespace(quote) in _normalized_whitespace(block.text):
        return None
    return f"{label} quote is not a substring of {block.suffix}"


def _set_quote_match(quote: str, *, item: dict[str, Any], block: DocumentBlock) -> None:
    """在已经通过子串校验的引用上记录实际匹配方式。"""

    item["quote_match"] = "exact" if quote in block.text else "whitespace_normalized"


def _validate_relation(
    relation: Any,
    *,
    label: str,
    block: DocumentBlock,
) -> str | None:
    """校验块内一条关系证据的逐字引用和理由。"""

    if not isinstance(relation, dict):
        return f"{label} must be an object"
    quote = relation.get("quote")
    error = _validate_quote(quote, label=label, block=block)
    if error is not None:
        return error
    _set_quote_match(quote, item=relation, block=block)
    reason = relation.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return f"{label} reason is missing"
    return None


def _validate_closest_candidate(
    candidate: Any,
    *,
    label: str,
    block: DocumentBlock,
) -> str | None:
    """校验判否前复核候选的块内原文和具体未满足原因。"""

    if not isinstance(candidate, dict):
        return f"{label} must be an object"
    quote = candidate.get("quote")
    error = _validate_quote(quote, label=label, block=block)
    if error is not None:
        return error
    _set_quote_match(quote, item=candidate, block=block)
    why_not = candidate.get("why_not")
    if not isinstance(why_not, str) or not why_not.strip():
        return f"{label} why_not is missing"
    return None


def _parse_model_output(
    content: str,
    *,
    relation_card: RelationCard,
    document: DocumentInput,
) -> tuple[dict[str, Any] | None, str | None]:
    """解析整篇结果并严格校验定位身份与证据，不静默修复模型输出。"""

    normalized = content.strip()
    if normalized.startswith("```"):
        normalized = re.sub(r"^```(?:json)?\s*", "", normalized)
        normalized = re.sub(r"\s*```$", "", normalized)
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError as error:
        return None, f"invalid JSON: {error}"
    if not isinstance(payload, dict):
        return None, "JSON root must be an object"
    if payload.get("relation") != relation_card.relation_id:
        return None, f"relation must be {relation_card.relation_id}"
    forbidden_fields = {
        "present",
        "block_reviews",
        "evidence_spans",
        "cross_block_relations",
    }
    unexpected_fields = forbidden_fields & payload.keys()
    if unexpected_fields:
        return (
            None,
            f"obsolete output fields are not allowed: {sorted(unexpected_fields)}",
        )
    blocks = payload.get("blocks")
    if not isinstance(blocks, list):
        return None, "blocks must be a list"

    blocks_by_suffix = {block.suffix: block for block in document.blocks}
    expected_block_ids = [block.suffix for block in document.blocks]
    reviewed_block_ids: list[str] = []
    has_relation = False
    for index, block_record in enumerate(blocks, 1):
        if not isinstance(block_record, dict):
            return None, f"block record {index} must be an object"
        block_id = block_record.get("block_id")
        if not isinstance(block_id, str) or block_id not in blocks_by_suffix:
            return None, f"block record {index} block_id is unknown: {block_id!r}"
        if block_id in reviewed_block_ids:
            return None, f"block record {index} duplicates block_id: {block_id}"
        relations = block_record.get("relations")
        closest_candidates = block_record.get("closest_candidates")
        has_constraint = block_record.get("has_constraint")
        reason = block_record.get("reason")
        if not isinstance(relations, list):
            return None, f"block record {index} relations must be a list"
        if not isinstance(closest_candidates, list):
            return None, f"block record {index} closest_candidates must be a list"
        if len(closest_candidates) > 2:
            return None, f"block record {index} closest_candidates exceeds two items"
        if not isinstance(has_constraint, bool):
            return None, f"block record {index} has_constraint must be boolean"
        if has_constraint != bool(relations):
            return None, (
                f"block record {index} has_constraint must match whether relations is empty"
            )
        if not isinstance(reason, str) or not reason.strip():
            return None, f"block record {index} reason is missing"
        if has_constraint and closest_candidates:
            return None, (f"block record {index} closest_candidates must be empty when present")
        block = blocks_by_suffix[block_id]
        for relation_index, relation in enumerate(relations, 1):
            error = _validate_relation(
                relation,
                label=f"block record {index} relation {relation_index}",
                block=block,
            )
            if error is not None:
                return None, error
        for candidate_index, candidate in enumerate(closest_candidates, 1):
            error = _validate_closest_candidate(
                candidate,
                label=f"block record {index} closest candidate {candidate_index}",
                block=block,
            )
            if error is not None:
                return None, error
        reviewed_block_ids.append(block_id)
        has_relation = has_relation or has_constraint

    if reviewed_block_ids != expected_block_ids:
        return None, (
            "blocks must contain every block exactly once in input order; "
            f"expected {expected_block_ids}, got {reviewed_block_ids}"
        )
    payload["present"] = has_relation
    return payload, None


def _optional_candidate_text(value: Any) -> tuple[str | None, str | None]:
    """把结构化候选中的可缺失字段归一化，不把占位文本伪装成有效内容。"""

    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "must be a string or null"
    normalized = value.strip()
    if normalized.casefold() in {"", "null", "none", "n/a", "na", "unknown"}:
        return None, None
    return normalized, None


def _parse_structured_candidate_output(
    content: str,
    *,
    relation_card: RelationCard,
    document: DocumentInput,
) -> tuple[dict[str, Any] | None, str | None]:
    """解析候选字段并由 runtime 派生关系是否成立，不接受模型给出的最终布尔值。"""

    normalized = content.strip()
    if normalized.startswith("```"):
        normalized = re.sub(r"^```(?:json)?\s*", "", normalized)
        normalized = re.sub(r"\s*```$", "", normalized)
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError as error:
        return None, f"invalid JSON: {error}"
    if not isinstance(payload, dict):
        return None, "JSON root must be an object"
    if payload.get("relation") != relation_card.relation_id:
        return None, f"relation must be {relation_card.relation_id}"
    forbidden_root_fields = {"present", "has_constraint", "relations"} & payload.keys()
    if forbidden_root_fields:
        return (
            None,
            (
                "structured candidate mode must not output final judgment fields: "
                f"{sorted(forbidden_root_fields)}"
            ),
        )
    blocks = payload.get("blocks")
    if not isinstance(blocks, list):
        return None, "blocks must be a list"

    blocks_by_suffix = {block.suffix: block for block in document.blocks}
    expected_block_ids = [block.suffix for block in document.blocks]
    reviewed_block_ids: list[str] = []
    has_relation = False
    required_candidate_fields = (
        "quote",
        "limiter",
        "target",
        "excluded_case",
        "reason",
    )
    derivation_fields = ("limiter", "target", "excluded_case")
    for block_index, block_record in enumerate(blocks, 1):
        if not isinstance(block_record, dict):
            return None, f"block record {block_index} must be an object"
        obsolete_block_fields = {
            "has_constraint",
            "present",
            "relations",
            "closest_candidates",
        } & block_record.keys()
        if obsolete_block_fields:
            return (
                None,
                (
                    f"block record {block_index} must not output final judgment fields: "
                    f"{sorted(obsolete_block_fields)}"
                ),
            )
        block_id = block_record.get("block_id")
        if not isinstance(block_id, str) or block_id not in blocks_by_suffix:
            return None, f"block record {block_index} block_id is unknown: {block_id!r}"
        if block_id in reviewed_block_ids:
            return None, f"block record {block_index} duplicates block_id: {block_id}"
        candidates = block_record.get("candidates")
        block_reason = block_record.get("reason")
        if not isinstance(candidates, list):
            return None, f"block record {block_index} candidates must be a list"
        if not isinstance(block_reason, str) or not block_reason.strip():
            return None, f"block record {block_index} reason is missing"

        block = blocks_by_suffix[block_id]
        relations: list[dict[str, Any]] = []
        incomplete_candidates: list[dict[str, Any]] = []
        for candidate_index, candidate in enumerate(candidates, 1):
            if not isinstance(candidate, dict):
                return (
                    None,
                    f"block record {block_index} candidate {candidate_index} must be an object",
                )
            missing_model_fields = set(required_candidate_fields) - candidate.keys()
            if missing_model_fields:
                return (
                    None,
                    (
                        f"block record {block_index} candidate {candidate_index} is missing "
                        f"fields: {sorted(missing_model_fields)}"
                    ),
                )
            quote = candidate.get("quote")
            error = _validate_quote(
                quote,
                label=f"block record {block_index} candidate {candidate_index}",
                block=block,
            )
            if error is not None:
                return None, error
            _set_quote_match(quote, item=candidate, block=block)
            candidate_reason = candidate.get("reason")
            if not isinstance(candidate_reason, str) or not candidate_reason.strip():
                return (
                    None,
                    f"block record {block_index} candidate {candidate_index} reason is missing",
                )

            missing_derivation_fields: list[str] = []
            normalized_fields: dict[str, str | None] = {}
            for field_name in derivation_fields:
                normalized_value, field_error = _optional_candidate_text(candidate.get(field_name))
                if field_error is not None:
                    return (
                        None,
                        (
                            f"block record {block_index} candidate {candidate_index} "
                            f"{field_name} {field_error}"
                        ),
                    )
                normalized_fields[field_name] = normalized_value
                candidate[field_name] = normalized_value
                if normalized_value is None:
                    missing_derivation_fields.append(field_name)

            is_established = not missing_derivation_fields
            derivation = {
                "established": is_established,
                "missing_fields": missing_derivation_fields,
            }
            candidate["runtime_derivation"] = derivation
            if is_established:
                relations.append(
                    {
                        "quote": quote,
                        "reason": candidate_reason,
                        **normalized_fields,
                        "runtime_derivation": derivation,
                    }
                )
            else:
                incomplete_candidates.append(
                    {
                        "quote": quote,
                        "why_not": (
                            "runtime derivation requires non-empty fields: "
                            + ", ".join(missing_derivation_fields)
                        ),
                        **normalized_fields,
                        "runtime_derivation": derivation,
                    }
                )

        block_record["candidates"] = candidates
        block_record["relations"] = relations
        block_record["closest_candidates"] = incomplete_candidates
        block_record["has_constraint"] = bool(relations)
        reviewed_block_ids.append(block_id)
        has_relation = has_relation or block_record["has_constraint"]

    if reviewed_block_ids != expected_block_ids:
        return None, (
            "blocks must contain every block exactly once in input order; "
            f"expected {expected_block_ids}, got {reviewed_block_ids}"
        )
    payload["judgment_mode"] = "structured-candidate-v1"
    payload["present"] = has_relation
    return payload, None


def _optional_usage_tokens(usage: Any, field_name: str) -> int | None:
    """读取供应商可能省略的 token 指标，不用零值伪造缺失数据。"""

    value = getattr(usage, field_name, None)
    return None if value is None else int(value)


def _collect_evidence(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """把每个块内关系展开成便于比较的统一证据记录。"""

    collected: list[dict[str, Any]] = []
    for block_record in payload.get("blocks", []):
        for relation in block_record.get("relations", []):
            collected.append(
                {
                    **relation,
                    "block_id": block_record["block_id"],
                    "source": "block",
                }
            )
    return collected


def _summarize_parsed(
    parsed: dict[str, Any],
    *,
    document: DocumentInput,
) -> dict[str, Any]:
    """汇总单次模型输出的块覆盖和块内关系证据。"""

    evidence = _collect_evidence(parsed)
    evidence_block_ids = sorted({span["block_id"] for span in evidence})
    reviewed_constraint_block_ids = sorted(
        record["block_id"] for record in parsed.get("blocks", []) if record["has_constraint"]
    )
    closest_candidate_blocks = [
        record["block_id"]
        for record in parsed.get("blocks", [])
        if record.get("closest_candidates")
    ]
    return {
        "present": parsed["present"],
        "reviewed_block_count": len(parsed.get("blocks", [])),
        "reviewed_constraint_block_ids": reviewed_constraint_block_ids,
        "evidence_blocks": evidence_block_ids,
        "evidence_span_count": len(evidence),
        "closest_candidate_count": sum(
            len(record.get("closest_candidates", [])) for record in parsed.get("blocks", [])
        ),
        "closest_candidate_block_ids": closest_candidate_blocks,
        "covered_block_count": len(evidence_block_ids),
        "all_document_block_count": len(document.blocks),
    }


async def _run_once(
    client: AsyncOpenAI,
    *,
    relation_card: RelationCard,
    document: DocumentInput,
    model: str,
    max_tokens: int,
    prompt: str,
    run_index: int,
    output_root: Path,
    structured_candidate_judgment: bool = False,
) -> dict[str, Any]:
    """执行一次整篇文档请求，并把成功、格式失败和调用失败分别落盘。"""

    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    started_at = datetime.now().isoformat(timespec="seconds")
    started = time.perf_counter()
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=max_tokens,
            extra_body={"thinking": {"type": "disabled"}},
        )
        elapsed_seconds = time.perf_counter() - started
        choice = response.choices[0]
        content = choice.message.content or ""
        parser = (
            _parse_structured_candidate_output
            if structured_candidate_judgment
            else _parse_model_output
        )
        parsed, validation_error = parser(
            content,
            relation_card=relation_card,
            document=document,
        )
        input_tokens = _optional_usage_tokens(response.usage, "prompt_tokens")
        cache_hit_tokens = _optional_usage_tokens(response.usage, "prompt_cache_hit_tokens")
        cache_miss_tokens = _optional_usage_tokens(response.usage, "prompt_cache_miss_tokens")
        response_class = (
            "error"
            if validation_error is not None
            else ("present" if parsed and parsed["present"] else "absent")
        )
        run = {
            "started_at": started_at,
            "finish_reason": choice.finish_reason,
            "elapsed_seconds": elapsed_seconds,
            "input_tokens": input_tokens,
            "cache_hit_tokens": cache_hit_tokens,
            "cache_miss_tokens": cache_miss_tokens,
            "cache_tokens_consistent": (
                None
                if None in (input_tokens, cache_hit_tokens, cache_miss_tokens)
                else cache_hit_tokens + cache_miss_tokens == input_tokens
            ),
            "output_tokens": int(getattr(response.usage, "completion_tokens", 0) or 0),
            "response": content,
            "parsed": parsed,
            "validation_error": validation_error,
            "response_class": response_class,
            "summary": (
                _summarize_parsed(parsed, document=document) if parsed is not None else None
            ),
        }
    except Exception as error:  # noqa: BLE001 - 调用失败必须原样落盘供审计
        run = {
            "started_at": started_at,
            "finish_reason": None,
            "elapsed_seconds": time.perf_counter() - started,
            "input_tokens": None,
            "cache_hit_tokens": None,
            "cache_miss_tokens": None,
            "cache_tokens_consistent": None,
            "output_tokens": None,
            "response": "",
            "parsed": None,
            "validation_error": f"{type(error).__name__}: {error}",
            "response_class": "call_error",
            "summary": None,
        }
    payload = {
        "run_index": run_index,
        "doc_label": document.label,
        "doc_id": document.doc_id,
        "doc_title": document.title,
        "relation_card": asdict(relation_card),
        "prompt_sha256": prompt_sha256,
        "prompt": prompt,
        "run": run,
    }
    _write_json(output_root / f"run-{run_index:02d}.json", payload)
    return payload


def _discover_baseline_dirs(runtime_root: Path, explicit_dirs: list[Path]) -> list[Path]:
    """定位逐块基线目录；显式参数优先，否则读取当前 stable-prefix 批次。"""

    candidates = (
        explicit_dirs
        if explicit_dirs
        else sorted(path for path in runtime_root.glob(DEFAULT_BASELINE_GLOB) if path.is_dir())
    )
    if not candidates:
        raise SystemExit("no block-level baseline directories were found")
    missing = [path for path in candidates if not path.is_dir()]
    if missing:
        raise SystemExit(f"baseline directories do not exist: {missing}")
    return candidates


def _load_baseline_run(
    baseline_dir: Path,
    *,
    document_label: str,
) -> dict[str, Any]:
    """读取一批逐块结果，形成与整篇探针相同的块和证据摘要。"""

    aggregate = json.loads((baseline_dir / "aggregate.json").read_text(encoding="utf-8"))
    experiment = json.loads((baseline_dir / "experiment.json").read_text(encoding="utf-8"))
    block_payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((baseline_dir / document_label).glob("b*.json"))
    ]
    if not block_payloads:
        raise SystemExit(f"baseline has no {document_label} block results: {baseline_dir}")

    present_blocks: list[str] = []
    evidence_blocks: set[str] = set()
    evidence_quotes: set[str] = set()
    evidence_spans = 0
    for payload in block_payloads:
        run = payload["run"]
        if run["response_class"] != "present":
            continue
        suffix = payload["block_suffix"]
        present_blocks.append(suffix)
        evidence_blocks.add(suffix)
        for span in run["parsed"]["evidence_spans"]:
            evidence_spans += 1
            evidence_quotes.add(_normalized_whitespace(span["quote"]))
    return {
        "directory": str(baseline_dir),
        "prompt_layout": experiment.get("prompt_layout"),
        "relation_card": experiment.get("relation_card"),
        "call_count": len(block_payloads),
        "present_blocks": sorted(present_blocks),
        "evidence_blocks": sorted(evidence_blocks),
        "evidence_span_count": evidence_spans,
        "evidence_quotes_normalized": sorted(evidence_quotes),
        "format_or_validation_errors": aggregate.get("format_or_validation_errors"),
        "call_errors": aggregate.get("call_errors"),
        "input_tokens": aggregate.get("input_tokens"),
        "cache_hit_tokens": aggregate.get("cache_hit_tokens"),
        "cache_miss_tokens": aggregate.get("cache_miss_tokens"),
        "output_tokens": aggregate.get("output_tokens"),
        "elapsed_seconds": aggregate.get("elapsed_seconds"),
        "request_elapsed_seconds": sum(
            document["request_elapsed_seconds"]
            for document in aggregate.get("documents", [])
            if document.get("doc_label") == document_label
        ),
    }


def _load_gold(document_label: str) -> dict[str, Any] | None:
    """读取 c13-003 的 D04 单篇金标准；文档标签不是 D04 时不做错误联想。"""

    if not GOLD_FIXTURE.is_file():
        return None
    fixture = json.loads(GOLD_FIXTURE.read_text(encoding="utf-8"))
    single_document = fixture.get("single_document")
    if not isinstance(single_document, dict):
        return None
    if document_label != DEFAULT_DOCUMENT_LABEL:
        return None
    groups = single_document.get("groups", {})
    return {
        "topic": fixture.get("topic"),
        "doc_id": single_document.get("doc_id"),
        "required_group_ids": single_document.get("required_group_ids", []),
        "known_relevant_block_suffixes": sorted(
            _block_suffix(block_id)
            for block_id in single_document.get("known_relevant_block_ids", [])
        ),
        "group_block_suffixes": {
            group_id: sorted(
                _block_suffix(block_id) for block_id in group.get("evidence_block_ids", [])
            )
            for group_id, group in groups.items()
        },
        "group_labels": {
            group_id: group.get("label", group_id) for group_id, group in groups.items()
        },
    }


def _score_gold(
    evidence_blocks: list[str],
    *,
    gold: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """计算 c13-003 已知相关块和必需证据组的覆盖面。"""

    if gold is None:
        return None
    evidence_set = set(evidence_blocks)
    known = set(gold["known_relevant_block_suffixes"])
    hit = evidence_set & known
    covered_groups = [
        group_id
        for group_id in gold["required_group_ids"]
        if evidence_set & set(gold["group_block_suffixes"].get(group_id, []))
    ]
    missed_groups = [
        group_id for group_id in gold["required_group_ids"] if group_id not in covered_groups
    ]
    return {
        "known_relevant_total": len(known),
        "known_relevant_hit": len(hit),
        "known_relevant_hit_block_ids": sorted(hit),
        "known_relevant_missed_block_ids": sorted(known - evidence_set),
        "required_group_total": len(gold["required_group_ids"]),
        "required_group_covered": len(covered_groups),
        "required_group_covered_ids": covered_groups,
        "required_group_missed_ids": missed_groups,
        "returned_block_count": len(evidence_set),
        "returned_blocks_overlapping_gold_ratio": (
            len(hit) / len(evidence_set) if evidence_set else None
        ),
    }


def _set_stability(sets: list[set[str]]) -> dict[str, Any]:
    """汇总多次运行中稳定出现、波动出现的元素集合。"""

    if not sets:
        return {
            "run_count": 0,
            "all_runs": [],
            "any_run": [],
            "unstable_union": [],
            "frequency": {},
        }
    frequencies: Counter[str] = Counter()
    for values in sets:
        frequencies.update(values)
    run_count = len(sets)
    all_runs = sorted(value for value, count in frequencies.items() if count == run_count)
    any_run = sorted(frequencies)
    unstable = sorted(value for value, count in frequencies.items() if 0 < count < run_count)
    return {
        "run_count": run_count,
        "all_runs": all_runs,
        "any_run": any_run,
        "unstable_union": unstable,
        "frequency": dict(sorted(frequencies.items())),
    }


def _numeric_average(values: list[int | float | None]) -> float | None:
    """计算存在指标时的平均值；缺失指标保持缺失，不用零代替。"""

    present = [value for value in values if value is not None]
    return mean(present) if present else None


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    """计算可能缺项的比率，缺项或零分母时返回空值。"""

    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def _summarize_whole_runs(
    payloads: list[dict[str, Any]],
    *,
    document: DocumentInput,
    gold: dict[str, Any] | None,
) -> dict[str, Any]:
    """汇总整篇文档多次运行的覆盖稳定性、证据和用量。"""

    runs: list[dict[str, Any]] = []
    block_sets: list[set[str]] = []
    quote_sets: list[set[str]] = []
    for payload in payloads:
        run = payload["run"]
        summary = run.get("summary")
        if summary is None:
            block_set: set[str] = set()
            quote_set: set[str] = set()
            gold_score = _score_gold([], gold=gold)
        else:
            block_set = set(summary["evidence_blocks"])
            evidence = _collect_evidence(run["parsed"])
            quote_set = {_normalized_whitespace(span["quote"]) for span in evidence}
            gold_score = _score_gold(summary["evidence_blocks"], gold=gold)
        block_sets.append(block_set)
        quote_sets.append(quote_set)
        runs.append(
            {
                "run_index": payload["run_index"],
                "response_class": run["response_class"],
                "finish_reason": run["finish_reason"],
                "validation_error": run["validation_error"],
                "elapsed_seconds": run["elapsed_seconds"],
                "input_tokens": run["input_tokens"],
                "cache_hit_tokens": run["cache_hit_tokens"],
                "cache_miss_tokens": run["cache_miss_tokens"],
                "cache_tokens_consistent": run["cache_tokens_consistent"],
                "output_tokens": run["output_tokens"],
                "total_tokens": (
                    run["input_tokens"] + run["output_tokens"]
                    if run["input_tokens"] is not None and run["output_tokens"] is not None
                    else None
                ),
                "evidence_block_count": len(block_set),
                "evidence_span_count": (
                    summary["evidence_span_count"] if summary is not None else None
                ),
                "closest_candidate_count": (
                    summary["closest_candidate_count"] if summary is not None else None
                ),
                "closest_candidate_block_ids": (
                    summary["closest_candidate_block_ids"] if summary is not None else None
                ),
                "evidence_blocks": sorted(block_set),
                "quote_count": len(quote_set),
                "gold": gold_score,
            }
        )

    aggregate = {
        "run_count": len(runs),
        "call_count": len(runs),
        "successful_parse_runs": sum(
            run["response_class"] in {"present", "absent"} for run in runs
        ),
        "format_or_validation_error_runs": sum(run["response_class"] == "error" for run in runs),
        "call_error_runs": sum(run["response_class"] == "call_error" for run in runs),
        "average_elapsed_seconds": _numeric_average([run["elapsed_seconds"] for run in runs]),
        "average_input_tokens": _numeric_average([run["input_tokens"] for run in runs]),
        "average_cache_hit_tokens": _numeric_average([run["cache_hit_tokens"] for run in runs]),
        "average_cache_miss_tokens": _numeric_average([run["cache_miss_tokens"] for run in runs]),
        "average_output_tokens": _numeric_average([run["output_tokens"] for run in runs]),
        "average_total_tokens": _numeric_average([run["total_tokens"] for run in runs]),
        "block_stability": _set_stability(block_sets),
        "quote_stability": _set_stability(quote_sets),
        "runs": runs,
    }
    return aggregate


def _build_comparison(
    *,
    whole: dict[str, Any],
    baselines: list[dict[str, Any]],
    gold: dict[str, Any] | None,
) -> dict[str, Any]:
    """比较整篇调用与最新逐块基线的覆盖、证据、用量和耗时。"""

    primary_baseline = baselines[-1]
    baseline_block_stability = _set_stability([set(run["evidence_blocks"]) for run in baselines])
    baseline_quote_stability = _set_stability(
        [set(run["evidence_quotes_normalized"]) for run in baselines]
    )
    baseline_average = {
        "call_count": _numeric_average([run["call_count"] for run in baselines]),
        "input_tokens": _numeric_average([run["input_tokens"] for run in baselines]),
        "cache_hit_tokens": _numeric_average([run["cache_hit_tokens"] for run in baselines]),
        "cache_miss_tokens": _numeric_average([run["cache_miss_tokens"] for run in baselines]),
        "output_tokens": _numeric_average([run["output_tokens"] for run in baselines]),
        "total_tokens": _numeric_average(
            [
                run["input_tokens"] + run["output_tokens"]
                for run in baselines
                if run["input_tokens"] is not None and run["output_tokens"] is not None
            ]
        ),
        "elapsed_seconds": _numeric_average([run["elapsed_seconds"] for run in baselines]),
        "request_elapsed_seconds": _numeric_average(
            [run["request_elapsed_seconds"] for run in baselines]
        ),
        "evidence_span_count": _numeric_average([run["evidence_span_count"] for run in baselines]),
    }

    primary_baseline_blocks = set(primary_baseline["evidence_blocks"])
    whole_vs_primary_runs: list[dict[str, Any]] = []
    baseline_quotes = set(primary_baseline["evidence_quotes_normalized"])
    for run in whole["runs"]:
        if run["response_class"] not in {"present", "absent"}:
            continue
        whole_blocks = set(run["evidence_blocks"])
        whole_payload = json.loads(
            (Path(whole["output_root"]) / f"run-{run['run_index']:02d}.json").read_text(
                encoding="utf-8"
            )
        )
        parsed = whole_payload["run"]["parsed"]
        whole_quotes = {_normalized_whitespace(span["quote"]) for span in _collect_evidence(parsed)}
        whole_vs_primary_runs.append(
            {
                "run_index": run["run_index"],
                "common_blocks": sorted(whole_blocks & primary_baseline_blocks),
                "whole_only_blocks": sorted(whole_blocks - primary_baseline_blocks),
                "baseline_only_blocks": sorted(primary_baseline_blocks - whole_blocks),
                "common_evidence_quotes": len(whole_quotes & baseline_quotes),
                "whole_only_evidence_quotes": len(whole_quotes - baseline_quotes),
                "baseline_only_evidence_quotes": len(baseline_quotes - whole_quotes),
                "whole_evidence_quote_count": len(whole_quotes),
                "baseline_evidence_quote_count": len(baseline_quotes),
            }
        )

    whole_total = whole["average_total_tokens"]
    baseline_total = baseline_average["total_tokens"]
    whole_input = whole["average_input_tokens"]
    baseline_input = baseline_average["input_tokens"]
    comparison = {
        "primary_baseline_directory": primary_baseline["directory"],
        "baseline_run_count": len(baselines),
        "baseline_average": baseline_average,
        "baseline_block_stability": baseline_block_stability,
        "baseline_quote_stability": baseline_quote_stability,
        "whole_average": {
            "call_count": whole["call_count"] / whole["run_count"] if whole["run_count"] else None,
            "input_tokens": whole_input,
            "output_tokens": whole["average_output_tokens"],
            "total_tokens": whole_total,
            "elapsed_seconds": whole["average_elapsed_seconds"],
            "evidence_span_count": _numeric_average(
                [run["evidence_span_count"] for run in whole["runs"]]
            ),
        },
        "average_input_token_saving": _safe_ratio(
            (baseline_input - whole_input)
            if baseline_input is not None and whole_input is not None
            else None,
            baseline_input,
        ),
        "average_total_token_saving": _safe_ratio(
            (baseline_total - whole_total)
            if baseline_total is not None and whole_total is not None
            else None,
            baseline_total,
        ),
        "whole_vs_primary_baseline_runs": whole_vs_primary_runs,
        "gold": gold,
        "primary_baseline_gold": _score_gold(primary_baseline["evidence_blocks"], gold=gold),
    }
    return comparison


def _parse_args() -> argparse.Namespace:
    """读取实验命令行参数。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--document", default=DEFAULT_DOCUMENT_LABEL)
    parser.add_argument("--relation", default=DEFAULT_RELATION_ID, choices=RELATION_CARDS)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--max-tokens", type=int, default=6000)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--theme-independent-candidates", action="store_true")
    parser.add_argument(
        "--structured-candidate-judgment",
        action="store_true",
        help="模型只输出结构化候选，由 runtime 派生关系是否成立。",
    )
    parser.add_argument(
        "--print-prompt",
        action="store_true",
        help="只生成本次将发送的完整提示词，不调用模型。",
    )
    parser.add_argument("--definition-override")
    parser.add_argument("--evidence-boundary-override")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--baseline-dir", type=Path, action="append", default=[])
    parser.add_argument("--runtime-root", type=Path, default=BACKEND_ROOT / ".runtime")
    return parser.parse_args()


async def main() -> None:
    """执行整篇文档探针，并保存逐次结果、汇总与逐块对照。"""

    args = _parse_args()
    if args.runs < 1:
        raise SystemExit("runs must be at least 1")
    relation_card = RELATION_CARDS[args.relation]
    relation_card_overrides: dict[str, str] = {}
    if args.definition_override is not None:
        relation_card_overrides["definition"] = args.definition_override
    if args.evidence_boundary_override is not None:
        relation_card_overrides["evidence_boundary"] = args.evidence_boundary_override
    if relation_card_overrides:
        relation_card = replace(relation_card, **relation_card_overrides)
    document, snapshot = _load_document(args.snapshot, args.document)
    prompt = (
        _build_structured_candidate_prompt(relation_card, document)
        if args.structured_candidate_judgment
        else _build_prompt(
            relation_card,
            document,
            include_theme_independent_candidate_rule=(args.theme_independent_candidates),
        )
    )
    if args.print_prompt:
        print(prompt)
        return

    gold = _load_gold(args.document)
    baseline_dirs = _discover_baseline_dirs(args.runtime_root, args.baseline_dir)
    baselines = [_load_baseline_run(path, document_label=args.document) for path in baseline_dirs]

    api_key = keyring.get_password(CREDENTIAL_SERVICE_NAME, CHAT_API_KEY_USERNAME)
    if not api_key:
        raise SystemExit("chat api key is missing")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = args.output_dir or (
        BACKEND_ROOT / ".runtime" / f"constrain_whole_document_probe_{stamp}"
    )
    output_root.mkdir(parents=True, exist_ok=False)
    (output_root / "prompt.txt").write_text(prompt, encoding="utf-8")
    experiment = {
        "snapshot": str(args.snapshot.resolve()),
        "snapshot_id": snapshot.snapshot_id,
        "document": args.document,
        "doc_id": document.doc_id,
        "doc_title": document.title,
        "block_count": len(document.blocks),
        "document_character_count": sum(len(block.text) for block in document.blocks),
        "relation_card": asdict(relation_card),
        "model": args.model,
        "runs": args.runs,
        "temperature": 0.0,
        "thinking": "disabled",
        "max_tokens": args.max_tokens,
        "include_theme_independent_candidate_rule": (args.theme_independent_candidates),
        "judgment_mode": (
            "structured-candidate-v1"
            if args.structured_candidate_judgment
            else "direct-block-judgment-v1"
        ),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "baseline_directories": [str(path) for path in baseline_dirs],
    }
    _write_json(output_root / "experiment.json", experiment)

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=DEFAULT_BASE_URL,
        timeout=args.timeout_seconds,
        max_retries=2,
    )
    payloads: list[dict[str, Any]] = []
    for run_index in range(1, args.runs + 1):
        payloads.append(
            await _run_once(
                client,
                relation_card=relation_card,
                document=document,
                model=args.model,
                max_tokens=args.max_tokens,
                prompt=prompt,
                run_index=run_index,
                output_root=output_root,
                structured_candidate_judgment=args.structured_candidate_judgment,
            )
        )

    whole = _summarize_whole_runs(payloads, document=document, gold=gold)
    whole["output_root"] = str(output_root)
    comparison = _build_comparison(whole=whole, baselines=baselines, gold=gold)
    result = {
        "experiment": experiment,
        "whole_document": whole,
        "block_level_baselines": baselines,
        "comparison": comparison,
    }
    _write_json(output_root / "result.json", result)
    print(
        json.dumps(
            {
                "output_root": str(output_root),
                "whole_average": comparison["whole_average"],
                "baseline_average": comparison["baseline_average"],
                "input_token_saving": comparison["average_input_token_saving"],
                "total_token_saving": comparison["average_total_token_saving"],
                "whole_block_stability": whole["block_stability"],
                "baseline_block_stability": comparison["baseline_block_stability"],
                "primary_baseline_gold": comparison["primary_baseline_gold"],
                "whole_run_gold": [run["gold"] for run in whole["runs"]],
                "format_or_validation_error_runs": whole["format_or_validation_error_runs"],
                "call_error_runs": whole["call_error_runs"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if whole["format_or_validation_error_runs"] or whole["call_error_runs"]:
        raise SystemExit("probe completed with failed whole-document runs")


if __name__ == "__main__":
    asyncio.run(main())
