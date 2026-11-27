"""固定 15 篇叶节点挂接运行器的确定性回归。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

from app.providers.chat import ChatCompletionResult


def load_runner() -> ModuleType:
    """从 scripts 目录加载非包的实验运行器。"""

    script_path = Path(__file__).resolve().parents[2] / "scripts" / "wiki_leaf_attachment_run.py"
    spec = importlib.util.spec_from_file_location("wiki_leaf_attachment_run", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load wiki leaf attachment runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_inventory_and_framework_loaders_validate_fixture_contract(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runner = load_runner()
    snapshot_id = "snapshot-test"
    block_set_name = "doc#v0001.blocks.json"
    block_set = {
        "doc_id": "doc",
        "doc_version": "v0001",
        "blocks": [
            {
                "block_id": "doc#v0001#b00001",
                "section_path": ["section"],
                "text": "Fixture evidence.",
                "content_hash": "block-hash",
            }
        ],
    }
    block_set_bytes = (json.dumps(block_set, ensure_ascii=False) + "\n").encode()
    (tmp_path / block_set_name).write_bytes(block_set_bytes)
    manifest = {
        "snapshot_id": snapshot_id,
        "status": "ready",
        "completeness": "complete",
        "failed_documents": [],
        "totals": {"blocks": 1},
        "documents": [
            {
                "doc_id": "doc",
                "doc_version": "v0001",
                "block_count": 1,
                "artifact_refs": [
                    {
                        "kind": "block_set",
                        "snapshot_ref": block_set_name,
                        "sha256": runner.sha256_bytes(block_set_bytes),
                    }
                ],
            }
        ],
    }
    (tmp_path / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "SNAPSHOT_ID", snapshot_id)
    monkeypatch.setattr(runner, "EXPECTED_DOCUMENT_COUNT", 1)
    monkeypatch.setattr(runner, "EXPECTED_BLOCK_COUNT", 1)

    inventory = runner.load_inventory(tmp_path)
    framework = runner.load_framework()

    assert len(inventory.blocks) == 1
    assert inventory.blocks[0].block_id == "doc#v0001#b00001"
    assert len(framework.roots) == 24
    assert len(framework.leaves) == 620
    assert framework.revision_id == "kf_c1b79b163bfcb2a93844"


def test_quote_repair_maps_typography_back_to_original_text() -> None:
    runner = load_runner()
    block_text = "We found this useful.\nNothing more to clarify."
    payload = {
        "evidence": [
            {
                "quote": "we found this useful.",
                "support_types": ["effect"],
                "target": "target",
                "reason": "reason",
            },
            {
                "quote": "Nothing more to clarify.'",
                "support_types": ["definition"],
                "target": "target",
                "reason": "reason",
            },
        ]
    }

    repaired = runner.canonicalize_evidence_quotes(payload, block_text)

    assert repaired["evidence"][0]["quote"] == "We found this useful."
    assert repaired["evidence"][1]["quote"] == "Nothing more to clarify."


def test_stored_failed_output_can_be_repaired_without_new_call() -> None:
    runner = load_runner()
    block = runner.InputBlock(
        block_id="doc#v0001#b00001",
        document_id="doc",
        section_path=["section"],
        text="We found this useful.",
        content_hash="hash",
    )
    raw_content = json.dumps(
        {
            "evidence": [
                {
                    "quote": "we found this useful.",
                    "support_types": ["effect"],
                    "target": "target",
                    "reason": "reason",
                }
            ]
        }
    )
    record = {
        "block_id": block.block_id,
        "leaf_id": "K01.01",
        "status": "failed",
        "raw_content": raw_content,
        "errors": ["invalid_response"],
    }

    repaired = runner.repair_stored_leaf_record(record, block)

    assert repaired is not None
    assert repaired["status"] == "completed"
    assert repaired["supported"] is True
    assert repaired["evidence"][0]["quote"] == "We found this useful."
    assert repaired["repaired_from_stored_output"] is True


def test_invalid_root_preflight_falls_back_to_all_roots(monkeypatch) -> None:
    runner = load_runner()
    framework = runner.load_framework()
    block = runner.InputBlock(
        block_id="doc#v0001#b00001",
        document_id="doc",
        section_path=["section"],
        text="Fixture evidence.",
        content_hash="block-hash",
    )

    class InvalidRootProvider:
        async def complete(self, messages, *, max_output_tokens=None):
            return ChatCompletionResult(
                content='{"decisions":[]}',
                request_id="request-1",
                actual_model_id="fake",
                finish_reason="stop",
                input_tokens=10,
                output_tokens=2,
                cache_hit_tokens=6,
                cache_miss_tokens=4,
            )

    monkeypatch.setattr(runner, "MAX_REQUEST_ATTEMPTS", 1)
    result = asyncio.run(
        runner.run_root_preflight_one(
            InvalidRootProvider(),
            asyncio.Semaphore(1),
            block,
            framework.roots,
        )
    )

    assert result["status"] == "fallback_all_roots"
    assert result["retained_root_ids"] == sorted(root["node_id"] for root in framework.roots)
    assert result["cache_hit_tokens"] == 6
    assert result["cache_miss_tokens"] == 4


def test_leaf_verdict_preserves_cache_usage(monkeypatch) -> None:
    runner = load_runner()
    framework = runner.load_framework()
    block = runner.InputBlock(
        block_id="doc#v0001#b00001",
        document_id="doc",
        section_path=["section"],
        text="Fixture evidence.",
        content_hash="block-hash",
    )
    leaf = next(iter(framework.leaves.values()))

    class CachedLeafProvider:
        async def complete(self, messages, *, max_output_tokens=None):
            return ChatCompletionResult(
                content='{"evidence":[]}',
                request_id="request-leaf",
                actual_model_id="fake",
                finish_reason="stop",
                input_tokens=12,
                output_tokens=1,
                cache_hit_tokens=9,
                cache_miss_tokens=3,
            )

    monkeypatch.setattr(runner, "MAX_REQUEST_ATTEMPTS", 1)
    result = asyncio.run(
        runner.run_leaf_verdict_one(
            CachedLeafProvider(),
            asyncio.Semaphore(1),
            framework.revision_id,
            block,
            leaf,
        )
    )

    assert result["status"] == "completed"
    assert result["cache_hit_tokens"] == 9
    assert result["cache_miss_tokens"] == 3
