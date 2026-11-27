"""对照关系循环顺序对 DeepSeek 前缀缓存命中与抽取行为的实验探针。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

import keyring
from app.dual_entry_index.snapshot import SnapshotLoader
from openai import AsyncOpenAI
from wiki_constrain_block_level_probe import (
    CHAT_API_KEY_USERNAME,
    CREDENTIAL_SERVICE_NAME,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_SNAPSHOT,
    PROMPT_LAYOUT_STABLE_PREFIX,
    RELATION_CARDS,
    RelationCard,
    _block_suffix,
    _build_prompt,
)

EXECUTION_RELATION_OUTER = "relation-outer"
EXECUTION_BLOCK_OUTER_RELATION_PREFIX = "block-outer-relation-prefix"
EXECUTION_BLOCK_OUTER_BLOCK_PREFIX = "block-outer-block-prefix"

EXPERIMENT_CARDS = {
    **RELATION_CARDS,
    "enable": RelationCard(
        relation_id="enable",
        display_name="使能",
        definition="前者使后者成为可能或变得可行，但不保证后者实际发生。",
        evidence_boundary=(
            "原文明确表达某能力、行动或状态因前者而成为可能、可行或可执行。"
            "组件存在、功能开关被启用或单纯前置条件不等于使能；"
            "实际发生并被造成时优先归“导致”。"
        ),
    ),
    "require": RelationCard(
        relation_id="require",
        display_name="要求",
        definition=("某行动、状态、流程或资格，按规则、程序或明确约束，以另一条件为前置。"),
        evidence_boundary=(
            "原文明确表达规则、程序、资格或操作约束中的“必须、须、需要”等前置要求。"
            "“允许、禁止、限制”等规范判断归“规范控制”；无法脱离某条件运行归“依赖”；"
            "一般条件从句不自动成为要求。"
        ),
    ),
}

DEFAULT_RELATIONS = ("constrain", "enable", "require")
DEFAULT_BLOCKS = (
    "b00001",
    "b00003",
    "b00005",
    "b00008",
    "b00010",
    "b00015",
    "b00018",
    "b00021",
    "b00023",
    "b00026",
)


def _build_block_first_prompt(relation_card: RelationCard, block_text: str) -> str:
    """把原文块放到关系专用内容之前，同时保留现有判断要求和输出契约。"""

    return f"""你是原文块关系判定器。

下面是需要判断的完整原文块：

\"\"\"
{block_text}
\"\"\"

判断目标：

{relation_card.display_name}：{relation_card.definition}

任务：

1. 判断这个完整原文块是否表达了上述“{relation_card.display_name}”含义。
2. 不要求整个块的主要表达目的就是{relation_card.display_name}；只要块中有一处明确表达了该含义，就可以判定为存在。
3. 可以使用原文块内部的上下文。一个{relation_card.display_name}可以由一句表达，也可以由相邻多句共同表达。
4. 只依据原文块判断，不为覆盖全文而补充关系。
5. 如果存在{relation_card.display_name}，返回支持该判断的最小连续证据段。一个证据段可以包含一句或多句；
   一句话已经足够时，不要返回整个块。
6. 如果同一个块中存在多个彼此独立的{relation_card.display_name}，可以返回多个证据段；
   如果多个句子共同表达同一个{relation_card.display_name}，应合并为一个连续证据段。
7. evidence_spans 中的 quote 必须逐字复制原文，携带原文标点和语序，不得翻译、概括或改写。
8. 每个证据段的 reason 只说明该连续证据段如何表达上述{relation_card.display_name}定义，不新增定义之外的判断要求。
9. {relation_card.evidence_boundary}

只输出一个 JSON 对象。

存在{relation_card.display_name}时：

{{
  "relation": "{relation_card.relation_id}",
  "present": true,
  "evidence_spans": [
    {{
      "quote": "逐字复制的原文证据段",
      "reason": "该连续证据段如何表达{relation_card.display_name}"
    }}
  ]
}}

不存在{relation_card.display_name}时：

{{
  "relation": "{relation_card.relation_id}",
  "present": false,
  "evidence_spans": [],
  "reason": "原文块为什么没有表达该关系"
}}"""


def _parse_present(content: str) -> tuple[bool | None, str | None]:
    """从模型返回的 JSON 中读取存在性；只用于实验行为计数。"""

    normalized = content.strip()
    if normalized.startswith("```"):
        normalized = normalized.removeprefix("```json").removeprefix("```")
        normalized = normalized.removesuffix("```").strip()
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError as error:
        return None, str(error)
    present = payload.get("present")
    if not isinstance(present, bool):
        return None, "present must be boolean"
    return present, None


async def _run_prompt(
    client: AsyncOpenAI,
    *,
    relation_card: RelationCard,
    prompt: str,
    cache_probe_prefix: str,
    model: str,
    doc_label: str,
    block: Any,
    output_root: Path,
) -> dict[str, Any]:
    """执行单次提示词并保存原探针提供的统一用量字段。"""

    prompt = f"缓存拓扑对照批次：{cache_probe_prefix}\n\n{prompt}"
    started_at = datetime.now().isoformat(timespec="seconds")
    started = time.perf_counter()
    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=2000,
        extra_body={"thinking": {"type": "disabled"}},
    )
    elapsed_seconds = time.perf_counter() - started
    content = response.choices[0].message.content or ""
    parsed_present, parse_error = _parse_present(content)
    usage = response.usage
    input_tokens = getattr(usage, "prompt_tokens", None)
    cache_hit_tokens = getattr(usage, "prompt_cache_hit_tokens", None)
    cache_miss_tokens = getattr(usage, "prompt_cache_miss_tokens", None)
    payload = {
        "execution_order": output_root.name,
        "relation_id": relation_card.relation_id,
        "doc_label": doc_label,
        "block_suffix": _block_suffix(block.block_id),
        "started_at": started_at,
        "elapsed_seconds": elapsed_seconds,
        "input_tokens": input_tokens,
        "cache_hit_tokens": cache_hit_tokens,
        "cache_miss_tokens": cache_miss_tokens,
        "output_tokens": getattr(usage, "completion_tokens", None),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "json_parse_ok": parse_error is None,
        "parse_error": parse_error,
        "present": parsed_present,
        "content": content,
    }
    relation_root = output_root / relation_card.relation_id / doc_label
    relation_root.mkdir(parents=True, exist_ok=True)
    result_path = relation_root / f"{_block_suffix(block.block_id)}.json"
    result_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def _summarize(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总总体及按关系的缓存指标。"""

    def aggregate(items: list[dict[str, Any]]) -> dict[str, Any]:
        input_tokens = sum(item["input_tokens"] or 0 for item in items)
        hit_tokens = sum(item["cache_hit_tokens"] or 0 for item in items)
        miss_tokens = sum(item["cache_miss_tokens"] or 0 for item in items)
        return {
            "calls": len(items),
            "input_tokens": input_tokens,
            "cache_hit_tokens": hit_tokens,
            "cache_miss_tokens": miss_tokens,
            "cache_hit_rate": (
                hit_tokens / (hit_tokens + miss_tokens) if hit_tokens + miss_tokens else None
            ),
            "output_tokens": sum(item["output_tokens"] or 0 for item in items),
            "json_parse_errors": sum(not item["json_parse_ok"] for item in items),
            "present_relations": sum(item["present"] is True for item in items),
            "request_elapsed_seconds": sum(item["elapsed_seconds"] for item in items),
        }

    by_relation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for payload in payloads:
        by_relation[payload["relation_id"]].append(payload)
    return {
        **aggregate(payloads),
        "by_relation": {
            relation_id: aggregate(items) for relation_id, items in sorted(by_relation.items())
        },
    }


async def main() -> None:
    """执行三种循环顺序的固定样本缓存对照。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--relations",
        default=",".join(DEFAULT_RELATIONS),
        help="逗号分隔的关系标识",
    )
    parser.add_argument(
        "--blocks",
        default=",".join(DEFAULT_BLOCKS),
        help="逗号分隔的 D04 块后缀",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--batch-id")
    args = parser.parse_args()

    relation_ids = tuple(item.strip() for item in args.relations.split(","))
    unknown_relations = set(relation_ids) - set(EXPERIMENT_CARDS)
    if unknown_relations:
        raise SystemExit(f"unknown relations: {sorted(unknown_relations)}")
    block_suffixes = tuple(item.strip() for item in args.blocks.split(","))

    snapshot = SnapshotLoader().load(args.snapshot)
    document = snapshot.documents[3]
    blocks = sorted(
        (
            block
            for block in snapshot.blocks
            if block.doc_id == document.doc_id and _block_suffix(block.block_id) in block_suffixes
        ),
        key=lambda block: block.block_index,
    )
    found_suffixes = {_block_suffix(block.block_id) for block in blocks}
    missing = set(block_suffixes) - found_suffixes
    if missing:
        raise SystemExit(f"unknown D04 blocks: {sorted(missing)}")

    api_key = keyring.get_password(CREDENTIAL_SERVICE_NAME, CHAT_API_KEY_USERNAME)
    if not api_key:
        raise SystemExit("chat api key is missing")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_id = args.batch_id or stamp
    output_root = args.output_dir or (
        Path(__file__).resolve().parents[1]
        / "backend"
        / ".runtime"
        / f"prompt_cache_topology_{stamp}"
    )
    output_root.mkdir(parents=True, exist_ok=False)
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=DEFAULT_BASE_URL,
        timeout=180.0,
        max_retries=2,
    )

    prompts = {
        (relation_id, block.block_id): {
            "relation-prefix": _build_prompt(
                EXPERIMENT_CARDS[relation_id],
                block.text,
                prompt_layout=PROMPT_LAYOUT_STABLE_PREFIX,
            ),
            "block-prefix": _build_block_first_prompt(EXPERIMENT_CARDS[relation_id], block.text),
        }
        for relation_id in relation_ids
        for block in blocks
    }

    configs = (
        (
            EXECUTION_RELATION_OUTER,
            [
                (relation_id, block, "relation-prefix")
                for relation_id in relation_ids
                for block in blocks
            ],
        ),
        (
            EXECUTION_BLOCK_OUTER_RELATION_PREFIX,
            [
                (relation_id, block, "relation-prefix")
                for block in blocks
                for relation_id in relation_ids
            ],
        ),
        (
            EXECUTION_BLOCK_OUTER_BLOCK_PREFIX,
            [
                (relation_id, block, "block-prefix")
                for block in blocks
                for relation_id in relation_ids
            ],
        ),
    )

    config_summaries: dict[str, Any] = {}
    for config_name, calls in configs:
        config_root = output_root / config_name
        config_root.mkdir(parents=True)
        payloads = []
        started = time.perf_counter()
        for relation_id, block, prompt_kind in calls:
            payloads.append(
                await _run_prompt(
                    client,
                    relation_card=EXPERIMENT_CARDS[relation_id],
                    prompt=prompts[(relation_id, block.block_id)][prompt_kind],
                    cache_probe_prefix=f"{batch_id}:{config_name}",
                    model=args.model,
                    doc_label="D04",
                    block=block,
                    output_root=config_root,
                )
            )
        summary = _summarize(payloads)
        summary["wall_elapsed_seconds"] = time.perf_counter() - started
        config_summaries[config_name] = summary
        (config_root / "aggregate.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({config_name: summary}, ensure_ascii=False), flush=True)

    experiment = {
        "snapshot": str(args.snapshot.resolve()),
        "snapshot_id": snapshot.snapshot_id,
        "model": args.model,
        "document": "D04",
        "blocks": list(block_suffixes),
        "relations": {
            relation_id: asdict(EXPERIMENT_CARDS[relation_id]) for relation_id in relation_ids
        },
        "configs": config_summaries,
    }
    (output_root / "aggregate.json").write_text(
        json.dumps(experiment, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(experiment, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
