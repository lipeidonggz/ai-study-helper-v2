"""Evaluate one search_wiki_topic_v2 probe result against the frozen c13 targets."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGETS = (
    REPOSITORY_ROOT / "backend" / "tests" / "fixtures" / "c13_003_wiki_topic_v2_targets.json"
)


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _token_estimate(text: str) -> int:
    return max(1, math.ceil(len(text) / 3))


def _evidence_requirement_checks(
    *,
    group_id: str,
    group: dict[str, object],
    selected_ids: set[str],
) -> list[dict[str, object]]:
    """按组内语义子条件返回实际命中的 evidence block。

    一个语义边界可能由两个必须同时成立的子关系组成。例如并行工作现场隔离不能由状态恢复
    证据替代。未声明子条件的旧组继续使用组内任一相关 block 命中的兼容语义。
    """

    raw_requirements = group.get("evidence_requirements")
    if raw_requirements is None:
        return []
    if not isinstance(raw_requirements, list):
        raise ValueError(f"{group_id}.evidence_requirements must be a list")

    checks: list[dict[str, object]] = []
    for raw_requirement in raw_requirements:
        if not isinstance(raw_requirement, dict):
            raise ValueError(f"{group_id}.evidence_requirements entries must be objects")
        requirement_id = raw_requirement.get("requirement_id")
        label = raw_requirement.get("label")
        raw_block_ids = raw_requirement.get("evidence_block_ids")
        if not isinstance(requirement_id, str) or not requirement_id:
            raise ValueError(f"{group_id} has an evidence requirement without requirement_id")
        if not isinstance(label, str) or not label:
            raise ValueError(f"{group_id}.{requirement_id} has no label")
        if not isinstance(raw_block_ids, list) or not all(
            isinstance(block_id, str) and block_id for block_id in raw_block_ids
        ):
            raise ValueError(f"{group_id}.{requirement_id}.evidence_block_ids must contain strings")
        requirement_block_ids = set(raw_block_ids)
        hit_block_ids = sorted(selected_ids.intersection(requirement_block_ids))
        checks.append(
            {
                "group_id": group_id,
                "requirement_id": requirement_id,
                "label": label,
                "hit": bool(hit_block_ids),
                "hit_block_ids": hit_block_ids,
            }
        )
    return checks


def evaluate(
    *,
    result: dict[str, object],
    targets: dict[str, object],
    mode: str,
) -> dict[str, object]:
    """Check evidence coverage, precision, provenance schema and cost guards."""

    target_scope = targets["single_document"] if mode == "single" else targets["cross_document"]
    assert isinstance(target_scope, dict)
    groups = target_scope["groups"]
    assert isinstance(groups, dict)
    if mode == "single":
        required_group_ids = list(target_scope["required_group_ids"])
        known_relevant_ids = set(target_scope["known_relevant_block_ids"])
        extension_group_ids: list[str] = []
    else:
        required_group_ids = list(target_scope["required_core_group_ids"])
        extension_group_ids = list(target_scope["extension_group_ids"])
        known_relevant_ids = {
            block_id
            for group in groups.values()
            if isinstance(group, dict)
            for block_id in group["evidence_block_ids"]
        } | set(target_scope["known_relevant_block_ids"])

    search = result["search"]
    assert isinstance(search, dict)
    public_result = search["result"]
    assert isinstance(public_result, dict)
    blocks = public_result["blocks"]
    assert isinstance(blocks, list)
    selected_ids = [str(block["block_id"]) for block in blocks]
    selected_token_count = sum(
        _token_estimate(str(block["text"])) for block in blocks if isinstance(block, dict)
    )
    relevant_tokens = sum(
        _token_estimate(str(block["text"]))
        for block in blocks
        if isinstance(block, dict) and str(block["block_id"]) in known_relevant_ids
    )
    precision = (
        sum(block_id in known_relevant_ids for block_id in selected_ids) / len(selected_ids)
        if selected_ids
        else 0.0
    )
    irrelevant_token_ratio = (
        (selected_token_count - relevant_tokens) / selected_token_count
        if selected_token_count
        else 1.0
    )

    selected_id_set = set(selected_ids)

    def hit_group_ids(group_ids: list[str]) -> list[str]:
        hits: list[str] = []
        for group_id in group_ids:
            group = groups[group_id]
            assert isinstance(group, dict)
            requirement_checks = _evidence_requirement_checks(
                group_id=group_id,
                group=group,
                selected_ids=selected_id_set,
            )
            if requirement_checks:
                if all(bool(check["hit"]) for check in requirement_checks):
                    hits.append(group_id)
                continue
            if known_relevant_ids.intersection(selected_ids).intersection(
                group["evidence_block_ids"]
            ):
                hits.append(group_id)
        return hits

    core_hits = hit_group_ids(required_group_ids)
    extension_hits = hit_group_ids(extension_group_ids)
    missing_core = sorted(set(required_group_ids) - set(core_hits))
    core_evidence_requirement_checks = [
        check
        for group_id in required_group_ids
        for check in _evidence_requirement_checks(
            group_id=group_id,
            group=groups[group_id],
            selected_ids=selected_id_set,
        )
    ]
    missing_core_evidence_requirements = [
        check for check in core_evidence_requirement_checks if not bool(check["hit"])
    ]
    candidate_count = int(search["candidate_block_count"])
    matched_topic_count = len(search["matched_topic_ids"])
    group_matches = search.get("group_matches", [])
    group_selections = search.get("group_selections", [])
    topic_matches = search.get("topic_matches", [])
    selection_diagnostics = search.get("selected_evidence", [])
    assert isinstance(group_matches, list)
    assert isinstance(group_selections, list)
    assert isinstance(topic_matches, list)
    assert isinstance(selection_diagnostics, list)
    map_summary = result["map"]
    assert isinstance(map_summary, dict)
    reported_group_count = map_summary.get("groups")
    group_count = (
        int(reported_group_count) if reported_group_count is not None else len(group_matches)
    )
    minimum_extensions = int(target_scope.get("minimum_extension_groups", 0))

    checks = {
        "topic_matches": public_result["topic"] == targets["topic"],
        "public_payload_only_has_expected_fields": set(public_result)
        == {
            "topic",
            "blocks",
            "empty_reason",
        },
        "selected_blocks_unique": len(selected_ids) == len(set(selected_ids)),
        "required_groups_hit": not missing_core,
        "required_evidence_requirements_hit": not missing_core_evidence_requirements,
        "extension_groups_hit": len(extension_hits) >= minimum_extensions,
        "precision_meets_threshold": precision >= 0.80,
        "irrelevant_tokens_meet_threshold": irrelevant_token_ratio <= 0.20,
        "candidate_guard_has_two_times_margin": candidate_count <= 80,
        "selected_guard_has_two_times_margin": len(selected_ids) <= 48,
        "token_guard_has_two_times_margin": selected_token_count <= 25_000,
        "topic_guard_has_two_times_margin": matched_topic_count <= 2_000,
        "map_group_guard_has_two_times_margin": group_count <= 10_000,
        "retrieved_group_guard_has_two_times_margin": 0 < len(group_matches) <= 64,
        "diagnostics_include_group_scores": bool(group_matches),
        "diagnostics_include_group_selection_reasons": (
            len(group_selections) == len(search["matched_group_ids"])
        ),
        "diagnostics_include_topic_scores": len(topic_matches) == matched_topic_count,
        "diagnostics_include_selection_reasons": (len(selection_diagnostics) == len(selected_ids)),
    }
    return {
        "mode": mode,
        "passed": all(checks.values()),
        "checks": checks,
        "core_groups_hit": core_hits,
        "missing_core_groups": missing_core,
        "core_evidence_requirement_checks": core_evidence_requirement_checks,
        "missing_core_evidence_requirements": missing_core_evidence_requirements,
        "extension_groups_hit": extension_hits,
        "selected_block_count": len(selected_ids),
        "candidate_block_count": candidate_count,
        "matched_topic_count": matched_topic_count,
        "group_count": group_count,
        "selected_token_estimate": selected_token_count,
        "precision": precision,
        "irrelevant_token_ratio": irrelevant_token_ratio,
        "noise_block_ids": [
            block_id for block_id in selected_ids if block_id not in known_relevant_ids
        ],
    }


def main() -> None:
    """Parse the probe artifact, print the gate result and return a failing exit code."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("single", "cross"), required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = evaluate(
        result=_load_json(args.result),
        targets=_load_json(args.targets),
        mode=args.mode,
    )
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if not payload["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
