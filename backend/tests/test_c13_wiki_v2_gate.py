from __future__ import annotations

import json
from pathlib import Path

from scripts.c13_wiki_v2_gate import _evidence_requirement_checks, evaluate

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TARGETS_FILENAME = "c13_003_wiki_topic_v2_targets.json"
TARGETS_PATH = REPOSITORY_ROOT / "backend" / "tests" / "fixtures" / TARGETS_FILENAME

WORKSPACE_BLOCK = "doc_983ba4e2cbff4185873d525a69195fdf#v0001#c1#b00025"
CHECKPOINT_BLOCK = "doc_7f36f426b84f40eb9439acaad4949e32#v0001#c1#b00016"
PROGRESS_BLOCK = "doc_6c0a83dc425349d6ac08a20fba5ab94f#v0001#c1#b00013"


def _parallel_group() -> dict[str, object]:
    return {
        "label": "并行工作与持久状态控制边界",
        "evidence_requirements": [
            {
                "requirement_id": "workspace_isolation",
                "label": "并行工作现场隔离",
                "evidence_block_ids": [WORKSPACE_BLOCK],
            },
            {
                "requirement_id": "persistent_state_recovery",
                "label": "持久状态保存与恢复",
                "evidence_block_ids": [CHECKPOINT_BLOCK, PROGRESS_BLOCK],
            },
        ],
        "evidence_block_ids": [WORKSPACE_BLOCK, CHECKPOINT_BLOCK, PROGRESS_BLOCK],
    }


def test_parallel_work_state_requires_workspace_isolation_and_recovery() -> None:
    """Evidence requirements remain independently visible and jointly necessary."""

    group = _parallel_group()

    workspace_only = _evidence_requirement_checks(
        group_id="parallel_work_state",
        group=group,
        selected_ids={WORKSPACE_BLOCK},
    )
    recovery_only = _evidence_requirement_checks(
        group_id="parallel_work_state",
        group=group,
        selected_ids={CHECKPOINT_BLOCK},
    )
    complete = _evidence_requirement_checks(
        group_id="parallel_work_state",
        group=group,
        selected_ids={WORKSPACE_BLOCK, PROGRESS_BLOCK},
    )

    assert [check["hit"] for check in workspace_only] == [True, False]
    assert [check["hit"] for check in recovery_only] == [False, True]
    assert [check["hit"] for check in complete] == [True, True]


def test_c13_fixture_narrows_parallel_work_state_to_direct_evidence() -> None:
    """The frozen target keeps only evidence that directly supports both subconditions."""

    targets = json.loads(TARGETS_PATH.read_text(encoding="utf-8"))
    group = targets["cross_document"]["groups"]["parallel_work_state"]

    assert targets["case_revision_id"] is None
    assert targets["source_case_revision_id"] == "cr_c13_003_fff9fbc82a83"
    assert targets["target_case_revision_number"] == 6
    assert targets["target_case_revision_status"] == "draft"
    assert group["label"] == "并行工作与持久状态控制边界"
    assert group["evidence_block_ids"] == [
        WORKSPACE_BLOCK,
        CHECKPOINT_BLOCK,
        PROGRESS_BLOCK,
    ]
    assert {
        requirement["requirement_id"]: requirement["evidence_block_ids"]
        for requirement in group["evidence_requirements"]
    } == {
        "workspace_isolation": [WORKSPACE_BLOCK],
        "persistent_state_recovery": [CHECKPOINT_BLOCK, PROGRESS_BLOCK],
    }


def test_gate_rejects_parallel_work_state_with_workspace_evidence_only() -> None:
    """A worktree-only result cannot satisfy persistent-state recovery evidence."""

    targets = json.loads(TARGETS_PATH.read_text(encoding="utf-8"))
    selected_block = {
        "block_id": WORKSPACE_BLOCK,
        "text": "Run separate CLI sessions in isolated git checkouts so edits do not collide.",
    }
    result = {
        "search": {
            "result": {
                "topic": targets["topic"],
                "blocks": [selected_block],
                "empty_reason": None,
            },
            "candidate_block_count": 1,
            "matched_topic_ids": [],
            "group_matches": [{"group_id": "parallel_work_state"}],
            "group_selections": [],
            "topic_matches": [],
            "selected_evidence": [
                {
                    "block_id": WORKSPACE_BLOCK,
                    "reason": "Direct worktree isolation evidence.",
                }
            ],
            "matched_group_ids": [],
        },
        "map": {"groups": 224},
    }

    evaluation = evaluate(result=result, targets=targets, mode="cross")

    assert "parallel_work_state" not in evaluation["core_groups_hit"]
    assert evaluation["checks"]["required_groups_hit"] is False
    assert evaluation["checks"]["required_evidence_requirements_hit"] is False
    assert {
        requirement["requirement_id"]
        for requirement in evaluation["missing_core_evidence_requirements"]
    } == {"persistent_state_recovery"}
