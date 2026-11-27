"""用完整原文块和关系卡判断关系并保存可复核的连续证据段。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import keyring
from openai import AsyncOpenAI

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.dual_entry_index.snapshot import SnapshotLoader

DEFAULT_SNAPSHOT = Path(
    r"C:\Users\lida\AppData\Local\com.lipeidong.aistudyhelper\collections\s_6b787258ba7d700c"
)
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
CREDENTIAL_SERVICE_NAME = "ai-study-helper"
CHAT_API_KEY_USERNAME = "chat-api-key"


@dataclass(frozen=True, slots=True)
class RelationCard:
    """描述一次单关系判断所需的稳定语义契约。"""

    relation_id: str
    display_name: str
    definition: str
    evidence_boundary: str


RELATION_CARDS = {
    "constrain": RelationCard(
        relation_id="constrain",
        display_name="约束",
        definition=(
            "原文明确表达某个机制、规则、条件、动作或配置，使某个对象、行动、状态、输入或"
            "结果原本可行、可选、可发生或被接受的情形集合，缩小为该集合中更小的一个部分。"
        ),
        evidence_boundary=(
            "限制来源、被限制对象及至少一种被排除的情形必须能从原文确定；被排除情形可以由\n"
            "   限制语义直接推出，不要求出现“范围”或“缩小”等词。约束机制或规则可以是被描述\n"
            "   为已经存在、已被采用、被允许配置或作为系统／流程组成部分的限制；是否属于强制\n"
            "   要求、规范、可选项或最佳实践，不影响关系是否成立。但原文仅提出一般建议、价值\n"
            "   判断、适用场景、分类、能力、分工或共同出现，而没有描述具体限制机制及其排除\n"
            "   情形时，不能判定为约束。"
        ),
    ),
}
DEFAULT_RELATION_CARD_ID = "constrain"
PROMPT_LAYOUT_LEGACY = "legacy"
PROMPT_LAYOUT_STABLE_PREFIX = "stable-prefix-v2"
DEFAULT_PROMPT_LAYOUT = PROMPT_LAYOUT_STABLE_PREFIX
PROMPT_LAYOUTS = (PROMPT_LAYOUT_LEGACY, PROMPT_LAYOUT_STABLE_PREFIX)


def _build_prompt(
    relation_card: RelationCard,
    block_text: str,
    *,
    prompt_layout: str = DEFAULT_PROMPT_LAYOUT,
) -> str:
    """构造块级判断提示词；默认把原文块放在最后以扩大可缓存公共前缀。"""

    if prompt_layout == PROMPT_LAYOUT_LEGACY:
        return f"""你是原文块关系判定器。

判断目标：

{relation_card.display_name}：{relation_card.definition}

下面是需要判断的完整原文块：

\"\"\"
{block_text}
\"\"\"

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

    if prompt_layout == PROMPT_LAYOUT_STABLE_PREFIX:
        return f"""你是原文块关系判定器。

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
}}

下面是需要判断的完整原文块：

\"\"\"
{block_text}
\"\"\""""

    raise ValueError(f"unknown prompt layout: {prompt_layout}")


def _write_json(path: Path, payload: Any) -> None:
    """写入实验产物 JSON。"""

    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _parse_model_output(
    content: str, block_text: str, relation_card: RelationCard
) -> tuple[dict[str, Any] | None, str | None]:
    """解析并校验块级结果，不隐式修复模型输出。"""

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
    present = payload.get("present")
    if not isinstance(present, bool):
        return None, "present must be boolean"
    evidence_spans = payload.get("evidence_spans")
    if not isinstance(evidence_spans, list):
        return None, "evidence_spans must be a list"
    if present and not evidence_spans:
        return None, "present=true requires evidence_spans"
    if not present and evidence_spans:
        return None, "present=false must not contain evidence_spans"
    for index, span in enumerate(evidence_spans, 1):
        if not isinstance(span, dict):
            return None, f"evidence span {index} must be an object"
        quote = span.get("quote")
        reason = span.get("reason")
        if not isinstance(quote, str) or not quote.strip():
            return None, f"evidence span {index} quote is missing"
        if quote in block_text:
            span["quote_match"] = "exact"
        elif " ".join(quote.split()) in " ".join(block_text.split()):
            # 模型偶尔只把原文换行改成空格；记录差异，便于区分真实改写与空白归一化。
            span["quote_match"] = "whitespace_normalized"
        else:
            return None, f"evidence span {index} quote is not a block substring"
        if not isinstance(reason, str) or not reason.strip():
            return None, f"evidence span {index} reason is missing"
    if not present:
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return None, "present=false requires reason"
    return payload, None


def _optional_usage_tokens(usage: Any, field_name: str) -> int | None:
    """读取供应商可能不返回的 token 计数，不用零值伪造缺失指标。"""

    value = getattr(usage, field_name, None)
    return None if value is None else int(value)


async def _run_block(
    client: AsyncOpenAI,
    *,
    relation_card: RelationCard,
    prompt_layout: str,
    cache_probe_prefix: str | None,
    model: str,
    doc_label: str,
    doc_id: str,
    block: Any,
    block_suffix: str,
    semaphore: asyncio.Semaphore,
    output_root: Path,
) -> dict[str, Any]:
    """对一个原文块执行一次指定关系卡判断。"""

    prompt = _build_prompt(
        relation_card,
        block.text,
        prompt_layout=prompt_layout,
    )
    if cache_probe_prefix:
        # 缓存对照批次只用于制造独立、可重复的冷缓存前缀，不进入产品提示词。
        prompt = f"缓存对照批次：{cache_probe_prefix}\n\n{prompt}"
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    async with semaphore:
        started_at = datetime.now().isoformat(timespec="seconds")
        started = time.perf_counter()
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=2000,
                extra_body={"thinking": {"type": "disabled"}},
            )
            elapsed_seconds = time.perf_counter() - started
            choice = response.choices[0]
            content = choice.message.content or ""
            parsed, validation_error = _parse_model_output(content, block.text, relation_card)
            cache_hit_tokens = _optional_usage_tokens(response.usage, "prompt_cache_hit_tokens")
            cache_miss_tokens = _optional_usage_tokens(response.usage, "prompt_cache_miss_tokens")
            input_tokens = _optional_usage_tokens(response.usage, "prompt_tokens")
            response_class = (
                "error"
                if validation_error is not None
                else ("present" if parsed and parsed["present"] else "absent")
            )
            payload = {
                "doc_label": doc_label,
                "doc_id": doc_id,
                "doc_title": block.doc_title,
                "block_suffix": block_suffix,
                "block_id": block.block_id,
                "section_path": block.section_path,
                "relation_card": asdict(relation_card),
                "prompt_layout": prompt_layout,
                "cache_probe_prefix": cache_probe_prefix,
                "prompt_sha256": prompt_sha256,
                "prompt": prompt,
                "run": {
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
                },
            }
        # 单块调用失败必须落盘，避免批量任务静默遗漏。
        except Exception as error:  # noqa: BLE001
            elapsed_seconds = time.perf_counter() - started
            payload = {
                "doc_label": doc_label,
                "doc_id": doc_id,
                "doc_title": block.doc_title,
                "block_suffix": block_suffix,
                "block_id": block.block_id,
                "section_path": block.section_path,
                "relation_card": asdict(relation_card),
                "prompt_layout": prompt_layout,
                "cache_probe_prefix": cache_probe_prefix,
                "prompt_sha256": prompt_sha256,
                "prompt": prompt,
                "run": {
                    "started_at": started_at,
                    "elapsed_seconds": elapsed_seconds,
                    "input_tokens": 0,
                    "cache_hit_tokens": None,
                    "cache_miss_tokens": None,
                    "cache_tokens_consistent": None,
                    "output_tokens": 0,
                    "response": "",
                    "parsed": None,
                    "validation_error": f"{type(error).__name__}: {error}",
                    "response_class": "call_error",
                },
            }
    _write_json(output_root / doc_label / f"{block_suffix}.json", payload)
    return payload


def _block_suffix(block_id: str) -> str:
    """从块标识中取出稳定后缀。"""

    return block_id.rsplit("#", 1)[-1]


def _summarize_document(
    *,
    doc_label: str,
    doc_id: str,
    doc_title: str,
    elapsed_seconds: float,
    payloads: list[dict[str, Any]],
) -> dict[str, Any]:
    """汇总单篇文档的块级关系判断结果。"""

    response_classes = Counter(payload["run"]["response_class"] for payload in payloads)
    evidence_spans = [
        span["quote"]
        for payload in payloads
        if payload["run"]["parsed"] is not None
        for span in payload["run"]["parsed"]["evidence_spans"]
    ]
    span_counts = Counter(
        len(payload["run"]["parsed"]["evidence_spans"])
        for payload in payloads
        if payload["run"]["parsed"] is not None
    )
    cache_hit_tokens = sum(payload["run"]["cache_hit_tokens"] or 0 for payload in payloads)
    cache_miss_tokens = sum(payload["run"]["cache_miss_tokens"] or 0 for payload in payloads)
    cache_metric_calls = sum(
        payload["run"]["cache_hit_tokens"] is not None
        and payload["run"]["cache_miss_tokens"] is not None
        for payload in payloads
    )
    return {
        "doc_label": doc_label,
        "doc_id": doc_id,
        "doc_title": doc_title,
        "block_count": len(payloads),
        "calls": len(payloads),
        "present_blocks": response_classes["present"],
        "absent_blocks": response_classes["absent"],
        "format_or_validation_errors": response_classes["error"],
        "call_errors": response_classes["call_error"],
        "evidence_spans": len(evidence_spans),
        "unique_evidence_spans": len(set(evidence_spans)),
        "evidence_chars": sum(len(quote) for quote in evidence_spans),
        "span_count_distribution": {str(key): value for key, value in sorted(span_counts.items())},
        "input_tokens": sum(payload["run"]["input_tokens"] or 0 for payload in payloads),
        "cache_hit_tokens": cache_hit_tokens,
        "cache_miss_tokens": cache_miss_tokens,
        "cache_hit_rate": (
            cache_hit_tokens / (cache_hit_tokens + cache_miss_tokens)
            if cache_hit_tokens + cache_miss_tokens
            else None
        ),
        "cache_metric_missing_calls": len(payloads) - cache_metric_calls,
        "cache_token_sum_mismatch_calls": sum(
            payload["run"]["cache_tokens_consistent"] is False for payload in payloads
        ),
        "output_tokens": sum(payload["run"]["output_tokens"] for payload in payloads),
        "request_elapsed_seconds": sum(payload["run"]["elapsed_seconds"] for payload in payloads),
        "document_elapsed_seconds": elapsed_seconds,
    }


async def _probe_document(
    *,
    client: AsyncOpenAI,
    relation_card: RelationCard,
    prompt_layout: str,
    cache_probe_prefix: str | None,
    model: str,
    concurrency: int,
    doc_label: str,
    doc_id: str,
    blocks: list[Any],
    output_root: Path,
) -> dict[str, Any]:
    """并发执行一篇文档全部 block 的指定关系卡判断。"""

    (output_root / doc_label).mkdir(parents=True, exist_ok=False)
    semaphore = asyncio.Semaphore(concurrency)
    started = time.perf_counter()
    payloads = await asyncio.gather(
        *(
            _run_block(
                client,
                relation_card=relation_card,
                prompt_layout=prompt_layout,
                cache_probe_prefix=cache_probe_prefix,
                model=model,
                doc_label=doc_label,
                doc_id=doc_id,
                block=block,
                block_suffix=_block_suffix(block.block_id),
                semaphore=semaphore,
                output_root=output_root,
            )
            for block in blocks
        )
    )
    elapsed_seconds = time.perf_counter() - started
    summary = _summarize_document(
        doc_label=doc_label,
        doc_id=doc_id,
        doc_title=blocks[0].doc_title,
        elapsed_seconds=elapsed_seconds,
        payloads=payloads,
    )
    _write_json(output_root / doc_label / "aggregate.json", summary)
    print(
        json.dumps(summary, ensure_ascii=False),
        flush=True,
    )
    return summary


async def main() -> None:
    """按指定关系卡和支持范围执行块级关系探针。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--relation-card",
        choices=sorted(RELATION_CARDS),
        default=DEFAULT_RELATION_CARD_ID,
    )
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument(
        "--prompt-layout",
        choices=PROMPT_LAYOUTS,
        default=DEFAULT_PROMPT_LAYOUT,
    )
    parser.add_argument(
        "--cache-probe-prefix",
        help="仅用于缓存对照：在提示词最前面加入固定批次标识以隔离已有缓存",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="只运行指定块，格式为 D05:b00007；可重复传入",
    )
    args = parser.parse_args()
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be positive")
    relation_card = RELATION_CARDS[args.relation_card]

    snapshot = SnapshotLoader().load(args.snapshot)
    blocks_by_doc: dict[str, list[Any]] = {document.doc_id: [] for document in snapshot.documents}
    for block in snapshot.blocks:
        blocks_by_doc[block.doc_id].append(block)
    for blocks in blocks_by_doc.values():
        blocks.sort(key=lambda block: block.block_index)

    selected_by_label: dict[str, set[str]] = {}
    if args.include:
        labels_by_doc_id = {
            document.doc_id: f"D{index:02d}" for index, document in enumerate(snapshot.documents, 1)
        }
        for selector in args.include:
            doc_label, separator, block_suffix = selector.partition(":")
            if not separator or not doc_label or not block_suffix:
                raise SystemExit(f"invalid --include selector: {selector}")
            selected_by_label.setdefault(doc_label, set()).add(block_suffix)
        known_labels = set(labels_by_doc_id.values())
        unknown_labels = set(selected_by_label) - known_labels
        if unknown_labels:
            raise SystemExit(f"unknown document labels: {sorted(unknown_labels)}")
        for doc_id, doc_label in labels_by_doc_id.items():
            requested = selected_by_label.get(doc_label)
            if requested is None:
                blocks_by_doc[doc_id] = []
                continue
            filtered = [
                block
                for block in blocks_by_doc[doc_id]
                if _block_suffix(block.block_id) in requested
            ]
            available = {_block_suffix(block.block_id) for block in filtered}
            missing = requested - available
            if missing:
                raise SystemExit(f"unknown block suffixes for {doc_label}: {sorted(missing)}")
            blocks_by_doc[doc_id] = filtered

    api_key = keyring.get_password(CREDENTIAL_SERVICE_NAME, CHAT_API_KEY_USERNAME)
    if not api_key:
        raise SystemExit("chat api key is missing")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = args.output_dir or (BACKEND_ROOT / ".runtime" / f"relation_card_probe_{stamp}")
    output_root.mkdir(parents=True, exist_ok=False)
    _write_json(
        output_root / "experiment.json",
        {
            "snapshot": str(args.snapshot.resolve()),
            "snapshot_id": snapshot.snapshot_id,
            "model": args.model,
            "concurrency_per_document": args.concurrency,
            "runs_per_block": 1,
            "temperature": 0.0,
            "thinking": "disabled",
            "relation_card": asdict(relation_card),
            "prompt_layout": args.prompt_layout,
            "cache_probe_prefix": args.cache_probe_prefix,
            "document_count": len(snapshot.documents),
            "block_count": len(snapshot.blocks),
        },
    )

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=DEFAULT_BASE_URL,
        timeout=180.0,
        max_retries=2,
    )
    document_summaries: list[dict[str, Any]] = []
    started = time.perf_counter()
    for index, document in enumerate(snapshot.documents, 1):
        doc_label = f"D{index:02d}"
        blocks = blocks_by_doc[document.doc_id]
        if not blocks:
            continue
        document_summaries.append(
            await _probe_document(
                client=client,
                relation_card=relation_card,
                prompt_layout=args.prompt_layout,
                cache_probe_prefix=args.cache_probe_prefix,
                model=args.model,
                concurrency=args.concurrency,
                doc_label=doc_label,
                doc_id=document.doc_id,
                blocks=blocks,
                output_root=output_root,
            )
        )
    elapsed_seconds = time.perf_counter() - started

    response_classes = Counter(
        payload["run"]["response_class"]
        for path in output_root.glob("D*/b*.json")
        for payload in [json.loads(path.read_text(encoding="utf-8"))]
    )
    aggregate = {
        "output_root": str(output_root),
        "elapsed_seconds": elapsed_seconds,
        "document_count": len(document_summaries),
        "block_count": sum(item["block_count"] for item in document_summaries),
        "present_blocks": response_classes["present"],
        "absent_blocks": response_classes["absent"],
        "format_or_validation_errors": response_classes["error"],
        "call_errors": response_classes["call_error"],
        "input_tokens": sum(item["input_tokens"] for item in document_summaries),
        "cache_hit_tokens": sum(item["cache_hit_tokens"] for item in document_summaries),
        "cache_miss_tokens": sum(item["cache_miss_tokens"] for item in document_summaries),
        "cache_hit_rate": (
            sum(item["cache_hit_tokens"] for item in document_summaries)
            / (
                sum(item["cache_hit_tokens"] for item in document_summaries)
                + sum(item["cache_miss_tokens"] for item in document_summaries)
            )
            if (
                sum(item["cache_hit_tokens"] for item in document_summaries)
                + sum(item["cache_miss_tokens"] for item in document_summaries)
            )
            else None
        ),
        "cache_metric_missing_calls": sum(
            item["cache_metric_missing_calls"] for item in document_summaries
        ),
        "cache_token_sum_mismatch_calls": sum(
            item["cache_token_sum_mismatch_calls"] for item in document_summaries
        ),
        "output_tokens": sum(item["output_tokens"] for item in document_summaries),
        "evidence_spans": sum(item["evidence_spans"] for item in document_summaries),
        "documents": document_summaries,
    }
    _write_json(output_root / "aggregate.json", aggregate)
    print(json.dumps(aggregate, ensure_ascii=False), flush=True)
    if aggregate["format_or_validation_errors"] or aggregate["call_errors"]:
        raise SystemExit("probe completed with failed blocks")


if __name__ == "__main__":
    asyncio.run(main())
