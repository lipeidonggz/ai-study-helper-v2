"""运行原文块级联合语义清单实验。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

import keyring

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.dual_entry_index.artifacts import load_stage51_artifact
from app.dual_entry_index.induction import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_ID,
    DeepSeekJsonClient,
)
from app.dual_entry_index.scanner import CandidateScanner
from app.dual_entry_index.semantic_inventory import (
    PATTERN_DEFINITIONS,
    PATTERN_LABELS,
    SEMANTIC_INVENTORY_SYSTEM_PROMPT,
    SemanticInventoryCompiler,
)
from app.dual_entry_index.snapshot import (
    SnapshotLoader,
    find_active_snapshot,
    select_snapshot_documents,
)

CREDENTIAL_SERVICE_NAME = "ai-study-helper"
CHAT_API_KEY_USERNAME = "chat-api-key"


def _load_chat_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key is None:
        key = keyring.get_password(CREDENTIAL_SERVICE_NAME, CHAT_API_KEY_USERNAME)
    if not key:
        raise RuntimeError("chat_api_key_missing")
    return key


def _snapshot_path(args: argparse.Namespace) -> Path:
    if args.snapshot is not None:
        return args.snapshot
    if args.data_root is None:
        raise RuntimeError("snapshot_or_data_root_required")
    return find_active_snapshot(args.data_root)


def _progress_printer() -> Callable[[str], None]:
    started = time.monotonic()

    def print_progress(message: str) -> None:
        elapsed = int(time.monotonic() - started)
        minutes, seconds = divmod(elapsed, 60)
        print(f"[+{minutes:02d}:{seconds:02d}] {message}", flush=True)

    return print_progress


def _render_report(payload: dict[str, object]) -> str:
    stats = payload["stats"]
    if not isinstance(stats, dict):
        raise TypeError("semantic_inventory_stats_missing")
    usage = payload["usage"]
    if not isinstance(usage, dict):
        raise TypeError("semantic_inventory_usage_missing")
    reference = payload.get("reference")
    return _render_full_report(payload, stats, usage, reference)


def _short_block_id(block_id: str) -> str:
    return block_id.rsplit("#", 1)[-1]


def _table_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _render_full_report(
    payload: dict[str, object],
    stats: dict[str, object],
    usage: dict[str, object],
    reference: object,
) -> str:
    block_inventories = payload["block_inventories"]
    concepts = payload["concepts"]
    assertions = payload["assertions"]
    if not all(isinstance(value, list) for value in (block_inventories, concepts, assertions)):
        raise TypeError("semantic_inventory_output_shape_invalid")

    term_rows = [
        term
        for block in block_inventories
        for term in block["terms"]  # type: ignore[index]
    ]
    term_decision_counts = Counter(str(row["term_decision"]) for row in term_rows)
    sense_decision_counts = Counter(str(row["sense_decision"]) for row in term_rows)
    accepted_keys = {
        str(row["normalized_key"]) for row in term_rows if row["term_decision"] == "accepted"
    }
    resolved_occurrences = sum(row["sense_decision"] == "resolved" for row in term_rows)
    standalone_occurrences = sum(bool(row["standalone_concept"]) for row in term_rows)
    pattern_counts = json.loads(str(stats["assertion_pattern_counts"]))
    if not isinstance(pattern_counts, dict):
        raise TypeError("semantic_inventory_pattern_counts_invalid")
    materialization_counts = Counter(
        "+".join(concept["materialization_reasons"]) for concept in concepts
    )
    prompt_hash = hashlib.sha256(SEMANTIC_INVENTORY_SYSTEM_PROMPT.encode("utf-8")).hexdigest()

    lines = [
        "# 原文块级联合语义清单实验",
        "",
        "## 1. 实验目的",
        "",
        (
            "验证“术语先行、概念与功能关系共同识别、概念选择性物化”的原型是否可行。"
            "本实验只评估入库侧的术语、概念和功能关系产出，不包含跨文档概念对账，"
            "也不包含最终地图检索与问答。"
        ),
        "",
        "## 2. 实际实现（由当前代码反推）",
        "",
        "### 2.1 执行链",
        "",
        "1. 从固定快照加载原文块。本次快照只选择 1 篇文档，共 26 个待处理原文块。",
        (
            "2. 复用现有确定性候选扫描器扫描整个快照。只有 "
            "`model_judgement_required=true` 的候选组进入模型输入，并按原文块分组。"
        ),
        ("3. 每个含候选的原文块只发起一次联合模型调用；原文块之间按配置并发，本次并发度为 8。"),
        (
            "4. 模型在同一次响应中返回该块全部候选的术语判断、局部指称判断、"
            "是否具备独立物化说明，以及候选功能关系。"
        ),
        (
            "5. Runtime 校验模型响应。每个候选序号必须恰好出现一次；关系端点必须是本块"
            "已接受术语，且至少有两个不同端点；每个端点必须提供局部含义。"
        ),
        (
            "6. 校验失败时最多重试 2 次，并把 `validation_feedback` 加入下一次请求。"
            "单个非法关系候选会被拒绝并计数，不会使整个原文块失败。"
        ),
        (
            "7. 按规范化词面投影原型概念。只有两类词面会物化：当前原文提供了独立说明，"
            "或该词面被至少一条已接受功能关系作为端点。"
        ),
        "8. 每条关系投影为一条功能断言，并通过规范化词面连接到物化概念。",
        "",
        "### 2.2 模型输入结构",
        "",
        (
            "系统提示词见第 3 节。用户输入是压缩 JSON，包含原文块身份、章节路径、"
            "块类型、表格信息、完整原文，以及候选数组。候选只暴露局部序号、规范化词面、"
            "展示词面和出现次数，不提供预先判断的术语或概念。扫描得到 2,958 个候选组，"
            "其中 2,699 个要求模型判断；其余候选由确定性扫描结果直接保留。"
        ),
        "",
        "```json",
        "{",
        '  "task": "build_block_semantic_inventory",',
        '  "block": {',
        '    "block_id": "...",',
        '    "doc_title": "...",',
        '    "section_path": ["..."],',
        '    "source_node_types": ["..."],',
        '    "table_caption": null,',
        '    "table_headers": [],',
        '    "text": "原文块全文",',
        '    "candidates": [',
        '      {"i": 0, "key": "规范化词面", "display": "原文词面", "occurrences": 1}',
        "    ]",
        "  }",
        "}",
        "```",
        "",
        "### 2.3 Runtime 校验与物化规则",
        "",
        "- 非 `accepted` 术语不得携带 `resolved` 局部指称或独立物化标记。",
        "- `resolved` 必须提供非空 `sense_hint`；`mention_only` 和 `uncertain` 不提供。",
        "- `standalone=true` 只在原文本身提供了功能、作用、组成、状态、边界或行为信息时成立。",
        "- 功能关系端点必须指向本块的 `accepted` 候选，端点数量至少为 2，且不能重复。",
        (
            "- 原型的“概念”暂时按 `normalized_key` 聚合，不执行跨文档义项合并。"
            "概念摘要在局部 `sense_hint` 中取出现次数最多者，平票时取较短词面。"
        ),
        "- 概念支持块只来自独立物化出现的块，或该概念作为关系端点出现的块。",
        "- `orphan_concepts` 表示只有独立说明、没有进入任何功能关系的概念。",
        "- 清单哈希排除 `usage`、`stats` 和自身哈希，用于标识语义产物。",
        "",
        "### 2.4 本次运行参数",
        "",
        f"- 模型：`{(usage.get('actual_model_ids') or ['未知'])[0]}`",
        "- 最大输入字符：60,000",
        "- 最大输出 token：8,000",
        "- 最大重试次数：2",
        "- 请求超时：180 秒",
        "- 并发度：8",
        "",
        "## 3. 实际系统提示词",
        "",
        "```text",
        SEMANTIC_INVENTORY_SYSTEM_PROMPT.rstrip(),
        "```",
        "",
        "## 4. 功能关系模式定义",
        "",
        "| 模式 | 中文名 | 定义 |",
        "| --- | --- | --- |",
    ]
    for pattern, definition in PATTERN_DEFINITIONS.items():
        lines.append(
            f"| `{pattern.value}` | {PATTERN_LABELS[pattern]} | {_table_cell(definition)} |"
        )

    lines.extend(
        [
            "",
            "## 5. 总体结果",
            "",
            "| 指标 | 结果 |",
            "| --- | ---: |",
            f"| 快照 | `{payload['snapshot_id']}` |",
            f"| 编译器版本 | `{payload['compiler_version']}` |",
            f"| 提示词版本 | `{payload['prompt_version']}` |",
            f"| 系统提示词 SHA-256 | `{prompt_hash}` |",
            f"| 清单哈希 | `{payload['inventory_hash']}` |",
            f"| 原文块 | {stats['blocks_with_inventory']} |",
            f"| 候选组 | {stats['candidate_groups']} |",
            f"| 术语判断记录 | {len(term_rows)} |",
            f"| 接受术语（唯一词面） | {stats['accepted_terms']} |",
            f"| 已解析局部指称（唯一词面） | {stats['resolved_terms']} |",
            f"| 物化概念 | {stats['materialized_concepts']} |",
            f"| 仅术语、未物化 | {stats['term_only']} |",
            f"| 已解析但未物化 | {stats['resolved_not_materialized']} |",
            f"| 孤立概念 | {stats['orphan_concepts']} |",
            f"| 孤立概念比例 | {stats['orphan_concept_ratio']:.3f} |",
            f"| 功能断言 | {stats['functional_assertions']} |",
            f"| 被拒绝关系候选 | {stats['rejected_relation_candidates']} |",
            f"| 总耗时 | {stats['elapsed_seconds']:.2f}s |",
            f"| 模型请求 | {usage['requests']} |",
            f"| 失败尝试 | {usage['failed_attempts']} |",
            f"| 输入 token | {usage['input_tokens']} |",
            f"| 输出 token | {usage['output_tokens']} |",
            f"| 缓存命中 token | {usage['cache_hit_tokens']} |",
            f"| 缓存未命中 token | {usage['cache_miss_tokens']} |",
            "",
            "### 5.1 术语与义项判断分布",
            "",
            "| 维度 | 取值 | 出现次数 | 占比 |",
            "| --- | --- | ---: | ---: |",
        ]
    )
    for decision, count in sorted(term_decision_counts.items()):
        lines.append(f"| 术语判断 | `{decision}` | {count} | {count / len(term_rows):.3f} |")
    for decision, count in sorted(sense_decision_counts.items()):
        lines.append(f"| 义项判断 | `{decision}` | {count} | {count / len(term_rows):.3f} |")
    lines.extend(
        [
            (
                f"| 独立物化标记 | `true` | {standalone_occurrences} | "
                f"{standalone_occurrences / len(term_rows):.3f} |"
            ),
            f"| 接受术语唯一词面 | `accepted` | {len(accepted_keys)} | - |",
            f"| 已解析局部指称出现次数 | `resolved` | {resolved_occurrences} | - |",
            "",
            "### 5.2 概念物化原因",
            "",
            "| 物化原因 | 概念数 |",
            "| --- | ---: |",
        ]
    )
    for reason, count in sorted(materialization_counts.items()):
        lines.append(f"| `{reason}` | {count} |")

    lines.extend(
        [
            "",
            "### 5.3 各原文块产出",
            "",
            (
                "| 块 | 送入判断候选 | 接受术语记录 | 已解析记录 | 独立物化记录 | "
                "功能关系 | 拒绝关系 |"
            ),
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for block in block_inventories:
        terms = block["terms"]
        lines.append(
            f"| `{_short_block_id(block['block_id'])}` | {len(terms)} | "
            f"{sum(term['term_decision'] == 'accepted' for term in terms)} | "
            f"{sum(term['sense_decision'] == 'resolved' for term in terms)} | "
            f"{sum(bool(term['standalone_concept']) for term in terms)} | "
            f"{len(block['relations'])} | {len(block['rejected_relations'])} |"
        )

    lines.extend(
        [
            "",
            "### 5.4 功能关系模式分布",
            "",
            "| 模式 | 中文名 | 数量 |",
            "| --- | --- | ---: |",
        ]
    )
    pattern_by_value = {pattern.value: pattern for pattern in PATTERN_DEFINITIONS}
    for pattern_value, count in sorted(pattern_counts.items()):
        pattern = pattern_by_value[pattern_value]
        lines.append(f"| `{pattern_value}` | {PATTERN_LABELS[pattern]} | {count} |")

    lines.extend(_render_concepts_and_assertions(block_inventories, concepts, assertions))
    lines.extend(_render_report_limits(reference, stats))
    return "\n".join(lines) + "\n"


def _render_concepts_and_assertions(
    block_inventories: list[dict[str, object]],
    concepts: list[dict[str, object]],
    assertions: list[dict[str, object]],
) -> list[str]:
    term_by_group_id = {
        term["candidate_group_id"]: term for block in block_inventories for term in block["terms"]
    }
    relation_by_key = {
        (block["block_id"], relation["proposition"]): relation
        for block in block_inventories
        for relation in block["relations"]
    }
    pattern_by_value = {pattern.value: pattern for pattern in PATTERN_DEFINITIONS}

    accepted_terms_by_key: dict[str, list[tuple[str, dict[str, object]]]] = {}
    for block in block_inventories:
        for term in block["terms"]:
            if term["term_decision"] != "accepted":
                continue
            accepted_terms_by_key.setdefault(term["normalized_key"], []).append(
                (block["block_id"], term)
            )

    relations_by_key: dict[str, list[dict[str, object]]] = {}
    for assertion in assertions:
        block_id = assertion["support_block_ids"][0]
        relation = relation_by_key[(block_id, assertion["proposition"])]
        endpoints = []
        endpoint_hints: dict[str, str] = {}
        for group_id, hint in zip(
            relation["participant_group_ids"],
            relation["participant_sense_hints"],
            strict=True,
        ):
            key = term_by_group_id[group_id]["normalized_key"]
            endpoints.append(f"`{key}`：{hint}")
            endpoint_hints[key] = hint
        relation_entry = {
            "assertion": assertion,
            "relation": relation,
            "endpoints": endpoints,
            "endpoint_hints": endpoint_hints,
        }
        for key in assertion["participant_keys"]:
            relations_by_key.setdefault(key, []).append(relation_entry)

    lines = [
        "",
        "## 6. 义项判断与关联功能关系明细",
        "",
        (
            "本节按物化义项逐个展开。每个义项先列出它与原文各局部指称判断的关系，"
            "再列出所有引用该义项的功能关系。一个关系会重复出现在其每个端点义项下，"
            "重复是为了从任一义项出发都能看到完整关系。"
        ),
    ]
    sorted_concepts = sorted(concepts, key=lambda item: str(item["normalized_key"]))
    for concept_index, concept in enumerate(sorted_concepts, start=1):
        key = str(concept["normalized_key"])
        support_block_ids = concept["support_block_ids"]
        support_blocks = "、".join(
            f"`{_short_block_id(block_id)}`" for block_id in support_block_ids
        )
        reasons = [str(reason) for reason in concept["materialization_reasons"]]
        related_relations = relations_by_key.get(key, [])
        lines.extend(
            [
                "",
                f"### 6.{concept_index} 义项：`{key}`",
                "",
                f"- 概念 ID：`{concept['concept_id']}`",
                f"- 概念摘要：{concept['summary']}",
                f"- 物化原因代码：`{'+'.join(reasons)}`",
                f"- 支持原文块：{support_blocks}",
            ]
        )
        reason_sentences = []
        if "standalone_reference" in reasons:
            reason_sentences.append("至少一个原文块为它提供了独立的局部说明，满足单独物化条件。")
        if "relation_participant" in reasons:
            reason_sentences.append(f"它被 {len(related_relations)} 条已接受功能关系作为端点引用。")
        lines.append(f"- 为什么产生：{' '.join(reason_sentences)}")
        lines.extend(
            [
                "",
                "#### 义项判断来源",
                "",
                "| 原文块 | 术语判断 | 局部义项判断 | 局部含义 | 独立物化 | 模型判断依据 |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        observations = sorted(
            accepted_terms_by_key.get(key, []),
            key=lambda item: (item[0], item[1]["candidate_group_id"]),
        )
        for block_id, term in observations:
            hint = term["sense_hint"] or "—"
            lines.append(
                f"| `{_short_block_id(block_id)}` | `{term['term_decision']}` | "
                f"`{term['sense_decision']}` | {_table_cell(hint)} | "
                f"{'是' if term['standalone_concept'] else '否'} | "
                f"{_table_cell(term['reason'])} |"
            )

        lines.extend(["", "#### 关联功能关系", ""])
        if not related_relations:
            lines.append("未发现引用该义项的功能关系。")
        else:
            for relation_index, relation_entry in enumerate(related_relations, start=1):
                assertion = relation_entry["assertion"]
                relation = relation_entry["relation"]
                pattern = pattern_by_value[str(assertion["pattern"])]
                local_hint = relation_entry["endpoint_hints"][key]
                lines.extend(
                    [
                        (
                            f"- **关系 {relation_index}：{PATTERN_LABELS[pattern]}"
                            f"（`{pattern.value}`）** {assertion['proposition']}"
                        ),
                        f"  - 原文块：`{_short_block_id(assertion['support_block_ids'][0])}`",
                        f"  - 该义项在此关系中的局部含义：{local_hint}",
                        f"  - 全部端点：{'；'.join(relation_entry['endpoints'])}",
                        f"  - 关系抽取依据：{relation['reason']}",
                        f"  - 断言 ID：`{assertion['assertion_id']}`",
                    ]
                )

    rejected_rows = [
        (block["block_id"], rejection)
        for block in block_inventories
        for rejection in block["rejected_relations"]
    ]
    lines.extend(["", "## 7. 被拒绝的关系候选", ""])
    if rejected_rows:
        lines.extend(
            [
                "| 原文块 | Runtime 拒绝原因 |",
                "| --- | --- |",
            ]
        )
        lines.extend(
            f"| `{_short_block_id(block_id)}` | `{reason}` |" for block_id, reason in rejected_rows
        )
    else:
        lines.append("本次没有关系候选被 Runtime 拒绝。")
    return lines


def _render_report_limits(
    reference: object,
    stats: dict[str, object],
) -> list[str]:
    lines = [
        "",
        "## 8. 当前原型的边界",
        "",
        (
            "- 概念按 `normalized_key` 聚合，没有完成跨文档的术语消歧和概念义项对账，"
            "同形异义仍会暂时合并。"
        ),
        (
            "- 功能关系只做了块内结构校验，没有在概念对账后执行第二轮语义校验；"
            "因此“阻止却归为启用”等模式误判仍可能存在。"
        ),
        "- `accepted_terms` 是术语账本规模，不等于物化概念规模；大量术语保持 `term_only`。",
        (
            "- 本报告只证明入库产物的结构、成本与可观察结果，不能替代最终召回率、"
            "答案覆盖度和噪声比例的端到端验收。"
        ),
        "",
        "## 9. 追溯入口",
        "",
        "- 原始产物：同目录下的 `inventory.json`。",
        "- Runtime 实现：`backend/app/dual_entry_index/semantic_inventory.py`。",
        "- 实验入口：`scripts/wiki_semantic_inventory_probe.py`。",
        "",
        "## 10. 与现有地图对比",
        "",
    ]
    if not isinstance(reference, dict):
        lines.append("本次运行没有提供参照地图。")
        return lines

    reference_terms = int(reference["terms"])
    reference_concepts = int(reference["concept_senses"])
    materialized_concepts = int(stats["materialized_concepts"])
    accepted_terms = int(stats["accepted_terms"])
    term_change = (accepted_terms - reference_terms) / reference_terms
    concept_change = (materialized_concepts - reference_concepts) / reference_concepts
    lines.extend(
        [
            "| 指标 | 新原型 | 现有地图 | 变化 |",
            "| --- | ---: | ---: | ---: |",
            f"| 术语 | {accepted_terms} | {reference_terms} | {term_change:+.1%} |",
            (
                f"| 概念义项 | {materialized_concepts} | {reference_concepts} | "
                f"{concept_change:+.1%} |"
            ),
        ]
    )
    return lines


async def run(args: argparse.Namespace) -> dict[str, object]:
    """执行联合语义清单并写入实验产物。"""

    snapshot = SnapshotLoader().load(_snapshot_path(args))
    snapshot = select_snapshot_documents(snapshot, args.doc_id)
    client = DeepSeekJsonClient(
        _load_chat_key(),
        model_id=args.model,
        base_url=args.base_url,
        timeout_seconds=args.timeout_seconds,
    )
    compiler = SemanticInventoryCompiler(
        client,
        max_input_chars=args.max_input_chars,
        max_output_tokens=args.max_output_tokens,
        max_retries=args.max_retries,
    )
    progress = _progress_printer()
    progress(f"已加载快照：{snapshot.snapshot_id} blocks={len(snapshot.blocks)}")
    result = await compiler.compile(
        snapshot=snapshot,
        scanner=CandidateScanner(),
        concurrency=args.concurrency,
        progress=progress,
    )
    payload = result.model_dump(mode="json")
    if args.reference_map is not None:
        reference = load_stage51_artifact(args.reference_map)
        payload["reference"] = {
            "map_id": reference.map_id,
            "terms": len(reference.terms),
            "concept_senses": len(reference.concept_senses),
            "term_sense_links": len(reference.term_sense_links),
        }
    output_path = args.output_dir / f"inv_{result.inventory_hash[:16]}"
    output_path.mkdir(parents=True, exist_ok=False)
    (output_path / "inventory.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_path / "report.md").write_text(_render_report(payload), encoding="utf-8")
    return {
        "snapshot_id": result.snapshot_id,
        "inventory_hash": result.inventory_hash,
        "stats": result.stats,
        "usage": asdict(result.usage),
        "reference": payload.get("reference"),
        "output": str(output_path),
    }


def main() -> None:
    """解析命令行参数并运行实验。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--doc-id", action="append", default=[])
    parser.add_argument("--reference-map", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, choices=range(1, 17), default=8)
    parser.add_argument("--max-input-chars", type=int, default=60_000)
    parser.add_argument("--max-output-tokens", type=int, default=6_000)
    parser.add_argument("--max-retries", type=int, choices=range(5), default=2)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--model", default=DEFAULT_MODEL_ID)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
