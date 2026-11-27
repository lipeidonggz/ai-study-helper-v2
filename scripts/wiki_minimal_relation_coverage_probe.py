"""用单关系极简提示词检查一篇文档的直接关系证据覆盖。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import keyring
from openai import AsyncOpenAI

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.dual_entry_index.snapshot import SnapshotLoader

DEFAULT_SNAPSHOT = (
    BACKEND_ROOT / ".runtime" / "dual_entry_index_stage51_091" / "s51_7f7e71a6b581c451"
)
DEFAULT_DOC_ID = "doc_fd4dec9976e041308e2b39ed797c75d1"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
CREDENTIAL_SERVICE_NAME = "ai-study-helper"
CHAT_API_KEY_USERNAME = "chat-api-key"

GOLD_BLOCK_SUFFIXES = frozenset(
    {
        "b00002",
        "b00003",
        "b00004",
        "b00005",
        "b00007",
        "b00008",
        "b00010",
        "b00011",
        "b00012",
        "b00013",
        "b00014",
        "b00015",
        "b00019",
        "b00020",
        "b00021",
        "b00023",
    }
)


@dataclass(frozen=True, slots=True)
class RelationSpec:
    """一种待检测关系的显示名称、定义和理由要求。"""

    display_name: str
    definition: str
    reason_instruction: str


RELATIONS = {
    "enable": RelationSpec(
        display_name="使能",
        definition="前者使后者成为可能或变得可行，但不保证后者实际发生。",
        reason_instruction=("哪个条件、工具或机制使哪个行动、状态或能力成为可能或更可行"),
    ),
    "require": RelationSpec(
        display_name="要求",
        definition=("某行动、状态、流程或资格，按规则、程序或明确约束，以另一条件为前置。"),
        reason_instruction=(
            "哪个行动、状态、流程或资格以另一个什么条件为前置，以及原文如何明确表达该前置要求"
        ),
    ),
    "constrain": RelationSpec(
        display_name="约束",
        definition=(
            "原文表示因为某个目标或者原因（可为显式的或者根据上下文自然推测的隐式表达的），"
            "通过明确的机制、条件或动作，把某个对象、行动或状态的可发生、可执行、可访问、"
            "可观察或可影响范围限定为更小的一部分。"
        ),
        reason_instruction=(
            "谁通过什么机制、条件或动作，因为什么目标或原因，把哪个对象、行动或状态的哪一"
            "可发生、可执行、可访问、可观察或可影响范围限定为更小的一部分；"
            "目标或原因可以是原句显式表达，也可以是根据当前完整段落能够自然推测的隐式表达，"
            "如果是隐式表达，请说明推断所依据的原文内容"
        ),
    ),
    "change": RelationSpec(
        display_name="改变",
        definition=(
            "已有对象、信息或状态的属性、形式、取值或组织方式发生变化；转换是改变的一种具体形式。"
        ),
        reason_instruction=(
            "哪个已有对象、信息或状态从什么情况变成了什么情况，以及原文中的哪一处表达表明这种变化"
        ),
    ),
    "compose": RelationSpec(
        display_name="组成",
        definition="一个整体、结构或明确集合由若干部分、成员或子项构成。",
        reason_instruction=(
            "哪个整体、结构或集合由哪些部分、成员或子项构成，以及原文如何明确表达这种组成关系"
        ),
    ),
    "continue": RelationSpec(
        display_name="持续",
        definition=("一个事件、过程或状态跨越一段时间并保持存在或继续进行。"),
        reason_instruction=("哪个事件、过程或状态跨越了什么时间段并保持存在或继续进行"),
    ),
    "separate": RelationSpec(
        display_name="分离",
        definition=(
            "两个对象、区域、状态或信息域之间存在原文明确的边界，并在某个维度上不共享、"
            "不可流通、不可见或不能相互进入。"
        ),
        reason_instruction=(
            "哪两个对象、区域、状态或信息域之间存在边界，由原文中的哪一处表达建立该边界，"
            "以及它们在哪个维度上不共享、不可流通、不可见或不能相互进入"
        ),
    ),
    "used_for": RelationSpec(
        display_name="用于",
        definition=("一个对象、机制、行动或设计被用来实现某目标、承担某功能或服务某场景。"),
        reason_instruction=(
            "哪个对象、机制、行动或设计用于实现什么目标、承担什么功能或服务什么场景"
        ),
    ),
    "compare": RelationSpec(
        display_name="比较",
        definition=(
            "两个或多个对象、方法、状态或方案之间被原文明确建立相似、差异、优劣、大小、取舍"
            "或其他相对关系。"
        ),
        reason_instruction=(
            "哪两个或多个对象、方法、状态或方案在哪个维度上形成相似、差异、优劣、大小或取舍"
        ),
    ),
    "sourced_from": RelationSpec(
        display_name="来源",
        definition=("信息、主张、作品、记录、数据或材料来自某个人、文件、机构、事件或存储出处。"),
        reason_instruction=(
            "哪个信息、主张、作品、记录、数据或材料来自哪里，以及原文如何明确表达该来源"
        ),
    ),
    "represent": RelationSpec(
        display_name="表示",
        definition=(
            "一个术语、符号、文字、图示、声音或编码形式表达、指称或标识某个含义、对象或状态。"
        ),
        reason_instruction=(
            "哪个术语、符号、文字、图示、声音或编码形式表达、指称或标识什么含义、对象或状态"
        ),
    ),
    "delegate": RelationSpec(
        display_name="委托",
        definition=("一个主体把任务、决策、上下文或执行职责交给另一个主体代为完成。"),
        reason_instruction=("哪个主体把哪项任务、决策、上下文或执行职责交给哪个主体完成"),
    ),
    "reset": RelationSpec(
        display_name="重置",
        definition=(
            "将对象、状态、计数或记录恢复到初始、默认或空状态，原有进展或当前取值不再保留。"
        ),
        reason_instruction=(
            "哪个对象、状态、计数或记录被重置，原文中的哪一处表达表明它恢复到了初始、默认"
            "或空状态，以及原文如何表明原有进展或当前取值不再保留"
        ),
    ),
}


def _block_suffix(block_id: str) -> str:
    """从快照块标识中取出稳定的块后缀。"""

    return block_id.rsplit("#", 1)[-1]


def _build_prompt(relation: RelationSpec, block_text: str) -> str:
    """构造只暴露单一关系定义的证据句判断提示词。"""

    return f"""你是原文关系判定器。

判断目标：

{relation.display_name}：{relation.definition}

下面是需要判断的完整原文段落：

\"\"\"
{block_text}
\"\"\"

任务：

1. 找出段落中明确表达上述“{relation.display_name}”含义的句子。
2. 不要求整段或整句的主要表达目的就是{relation.display_name}；只要句子中有一处明确表达了该含义，就可以列出。
3. 只列出确实存在该含义的句子，不要为了覆盖全文而补充。
4. 句子必须逐字复制原文，不得翻译或改写。
5. 每条理由说明：{relation.reason_instruction}。

按以下格式输出：

1. 原文句子：……
   理由：……

如果没有找到符合定义的句子，只输出：

未找到。"""


def _classify_response(response: str) -> str:
    """按响应是否包含证据句区分未找到、有效证据和格式异常。"""

    normalized = response.strip()
    if "原文句子" in normalized:
        return "evidence"
    if re.fullmatch(r"未找到[。.]?", normalized):
        return "not_found"
    return "unexpected"


def _extract_sentences(response: str) -> tuple[str, ...]:
    """提取响应中“原文句子”后的单行内容，供快速去重和人工复核。"""

    sentences: list[str] = []
    for line in response.splitlines():
        match = re.match(r"^\s*(?:\d+[.、]\s*)?原文句子[：:]\s*(.+?)\s*$", line)
        if match:
            sentences.append(match.group(1))
    return tuple(sentences)


def _write_json(path: Path, payload: Any) -> None:
    """以稳定 UTF-8 JSON 格式写入实验产物。"""

    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


async def _run_one(
    client: AsyncOpenAI,
    *,
    model: str,
    prompt: str,
    run_index: int,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """执行一次模型调用并保留可审计的响应与用量。"""

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
            return {
                "run": run_index,
                "started_at": started_at,
                "finish_reason": choice.finish_reason,
                "elapsed_seconds": elapsed_seconds,
                "input_tokens": int(getattr(response.usage, "prompt_tokens", 0) or 0),
                "output_tokens": int(getattr(response.usage, "completion_tokens", 0) or 0),
                "response": content,
                "response_class": _classify_response(content),
                "sentences": list(_extract_sentences(content)),
            }
        # 单次供应商调用失败必须记录后继续批量实验，不能中断其余原文块。
        except Exception as error:  # noqa: BLE001
            return {
                "run": run_index,
                "started_at": started_at,
                "elapsed_seconds": time.perf_counter() - started,
                "error": f"{type(error).__name__}: {error}",
                "response_class": "error",
                "sentences": [],
            }


async def _probe(
    *,
    blocks: list[Any],
    relation_id: str,
    relation: RelationSpec,
    client: AsyncOpenAI,
    model: str,
    runs: int,
    concurrency: int,
    output_root: Path,
) -> dict[str, Any]:
    """对一组原文块运行单一关系检测，并逐块落盘结果。"""

    semaphore = asyncio.Semaphore(concurrency)
    relation_root = output_root / relation_id
    relation_root.mkdir(parents=True, exist_ok=False)
    summaries: list[dict[str, Any]] = []

    async def probe_block(block: Any) -> dict[str, Any]:
        suffix = _block_suffix(block.block_id)
        prompt = _build_prompt(relation, block.text)
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        result_runs = await asyncio.gather(
            *(
                _run_one(
                    client,
                    model=model,
                    prompt=prompt,
                    run_index=index,
                    semaphore=semaphore,
                )
                for index in range(1, runs + 1)
            )
        )
        response_classes = Counter(run["response_class"] for run in result_runs)
        sentence_counts = Counter(
            sentence for run in result_runs for sentence in run.get("sentences", [])
        )
        payload = {
            "block_suffix": suffix,
            "block_id": block.block_id,
            "section_path": block.section_path,
            "relation_id": relation_id,
            "relation_name": relation.display_name,
            "relation_definition": relation.definition,
            "prompt_sha256": prompt_sha256,
            "prompt": prompt,
            "runs": result_runs,
        }
        _write_json(relation_root / f"{suffix}.json", payload)
        return {
            "block_suffix": suffix,
            "is_gold": suffix in GOLD_BLOCK_SUFFIXES,
            "successes": sum("error" not in run for run in result_runs),
            "errors": sum("error" in run for run in result_runs),
            "evidence_runs": response_classes["evidence"],
            "not_found_runs": response_classes["not_found"],
            "unexpected_runs": response_classes["unexpected"],
            "input_tokens": sum(run.get("input_tokens", 0) for run in result_runs),
            "output_tokens": sum(run.get("output_tokens", 0) for run in result_runs),
            "sentence_counts": dict(sentence_counts),
        }

    for block in blocks:
        summary = await probe_block(block)
        summaries.append(summary)
        print(
            f"{relation_id} {summary['block_suffix']}: "
            f"evidence={summary['evidence_runs']}/{runs}, "
            f"not_found={summary['not_found_runs']}/{runs}, "
            f"unexpected={summary['unexpected_runs']}",
            flush=True,
        )

    return {
        "relation_id": relation_id,
        "relation_name": relation.display_name,
        "blocks": summaries,
        "successes": sum(item["successes"] for item in summaries),
        "errors": sum(item["errors"] for item in summaries),
        "input_tokens": sum(item["input_tokens"] for item in summaries),
        "output_tokens": sum(item["output_tokens"] for item in summaries),
    }


def _write_block_summary(output_root: Path, relation_summaries: list[dict[str, Any]]) -> None:
    """写出逐块的关系命中摘要，便于直接检查金标准覆盖。"""

    by_relation = {
        summary["relation_id"]: {item["block_suffix"]: item for item in summary["blocks"]}
        for summary in relation_summaries
    }
    block_suffixes = sorted({suffix for items in by_relation.values() for suffix in items})
    rows: list[dict[str, Any]] = []
    for suffix in block_suffixes:
        row: dict[str, Any] = {
            "block_suffix": suffix,
            "is_gold": suffix in GOLD_BLOCK_SUFFIXES,
            "any_direct_evidence": False,
        }
        for relation_id in by_relation:
            block_summary = by_relation[relation_id][suffix]
            row[f"{relation_id}_evidence_runs"] = block_summary["evidence_runs"]
            row[f"{relation_id}_sentence_counts"] = block_summary["sentence_counts"]
            row["any_direct_evidence"] = bool(
                row["any_direct_evidence"] or block_summary["evidence_runs"]
            )
        rows.append(row)
    _write_json(output_root / "block_summary.json", rows)

    relation_ids = sorted(by_relation)
    lines = [
        ",".join(
            [
                "block",
                "is_gold",
                *(f"{relation_id}_hits" for relation_id in relation_ids),
                "any_direct_hit",
            ]
        ),
        *(
            ",".join(
                [
                    row["block_suffix"],
                    str(row["is_gold"]).lower(),
                    *(str(row[f"{relation_id}_evidence_runs"]) for relation_id in relation_ids),
                    str(row["any_direct_evidence"]).lower(),
                ]
            )
            for row in rows
        ),
    ]
    (output_root / "block_summary.csv").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


async def main() -> None:
    """解析参数、加载快照并执行指定的单关系覆盖实验。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--doc-id", default=DEFAULT_DOC_ID)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--relations",
        nargs="+",
        choices=tuple(RELATIONS),
        default=list(RELATIONS),
    )
    parser.add_argument("--block-suffixes", nargs="+", default=[])
    parser.add_argument("--exclude-block-suffixes", nargs="+", default=[])
    args = parser.parse_args()

    if args.runs < 1:
        raise SystemExit("--runs must be positive")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be positive")

    snapshot = SnapshotLoader().load(args.snapshot)
    blocks = sorted(
        (block for block in snapshot.blocks if block.doc_id == args.doc_id),
        key=lambda block: block.block_index,
    )
    if not blocks:
        raise SystemExit("document has no blocks in snapshot")
    included_suffixes = set(args.block_suffixes)
    excluded_suffixes = set(args.exclude_block_suffixes)
    available_suffixes = {_block_suffix(block.block_id) for block in blocks}
    unknown_suffixes = (included_suffixes | excluded_suffixes) - available_suffixes
    if unknown_suffixes:
        raise SystemExit(f"unknown block suffixes: {sorted(unknown_suffixes)}")
    if included_suffixes:
        blocks = [block for block in blocks if _block_suffix(block.block_id) in included_suffixes]
    if excluded_suffixes:
        blocks = [
            block for block in blocks if _block_suffix(block.block_id) not in excluded_suffixes
        ]
    if not blocks:
        raise SystemExit("block filters select no blocks")

    api_key = keyring.get_password(
        CREDENTIAL_SERVICE_NAME,
        CHAT_API_KEY_USERNAME,
    )
    if not api_key:
        raise SystemExit("chat api key is missing")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = args.output_dir or (
        BACKEND_ROOT / ".runtime" / f"minimal_relation_coverage_d04_{stamp}"
    )
    output_root.mkdir(parents=True, exist_ok=False)
    _write_json(
        output_root / "experiment.json",
        {
            "snapshot": str(args.snapshot.resolve()),
            "snapshot_id": snapshot.snapshot_id,
            "doc_id": args.doc_id,
            "block_count": len(blocks),
            "gold_block_suffixes": sorted(GOLD_BLOCK_SUFFIXES),
            "model": args.model,
            "runs_per_block_relation": args.runs,
            "concurrency": args.concurrency,
            "selected_block_suffixes": [_block_suffix(block.block_id) for block in blocks],
            "excluded_block_suffixes": sorted(excluded_suffixes),
            "temperature": 0.0,
            "thinking": "disabled",
            "relations": {
                relation_id: {
                    "display_name": relation.display_name,
                    "definition": relation.definition,
                    "reason_instruction": relation.reason_instruction,
                }
                for relation_id, relation in (
                    (relation_id, RELATIONS[relation_id]) for relation_id in args.relations
                )
            },
        },
    )

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=DEFAULT_BASE_URL,
        timeout=180.0,
        max_retries=2,
    )
    started = time.perf_counter()
    relation_summaries = [
        await _probe(
            blocks=blocks,
            relation_id=relation_id,
            relation=relation,
            client=client,
            model=args.model,
            runs=args.runs,
            concurrency=args.concurrency,
            output_root=output_root,
        )
        for relation_id, relation in (
            (relation_id, RELATIONS[relation_id]) for relation_id in args.relations
        )
    ]
    elapsed_seconds = time.perf_counter() - started

    aggregate = {
        "root": str(output_root),
        "elapsed_seconds": elapsed_seconds,
        "relations": relation_summaries,
    }
    _write_json(output_root / "aggregate.json", aggregate)
    _write_block_summary(output_root, relation_summaries)

    direct_gold = [
        suffix
        for suffix in sorted(GOLD_BLOCK_SUFFIXES)
        if any(
            summary["blocks"][index]["evidence_runs"] > 0
            for summary in relation_summaries
            for index, _ in enumerate(summary["blocks"])
            if summary["blocks"][index]["block_suffix"] == suffix
        )
    ]
    print(
        json.dumps(
            {
                "output_root": str(output_root),
                "elapsed_seconds": elapsed_seconds,
                "gold_direct_coverage": len(direct_gold),
                "gold_total": len(GOLD_BLOCK_SUFFIXES),
                "missing_gold": sorted(GOLD_BLOCK_SUFFIXES - set(direct_gold)),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
