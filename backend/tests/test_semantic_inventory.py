from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from app.dual_entry_index.induction import JsonChatResult
from app.dual_entry_index.models import DocumentVersionIdentity, SnapshotData, SourceBlock
from app.dual_entry_index.scanner import CandidateScanner
from app.dual_entry_index.semantic_inventory import (
    SEMANTIC_INVENTORY_SYSTEM_PROMPT,
    SemanticInventoryCompiler,
)


class _InventoryClient:
    model_id = "fake-semantic-inventory-model"

    def __init__(self) -> None:
        self.user_prompts: list[str] = []

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> JsonChatResult:
        assert system_prompt == SEMANTIC_INVENTORY_SYSTEM_PROMPT
        assert max_output_tokens == 2_000
        self.user_prompts.append(user_prompt)
        payload = json.loads(user_prompt)
        candidates = payload["block"]["candidates"]
        first = int(candidates[0]["i"])
        second = int(candidates[1]["i"])
        return JsonChatResult(
            payload={
                "terms": [
                    {
                        "i": candidate["i"],
                        "term": "accepted",
                        "sense": "resolved",
                        "sense_hint": f"{candidate['display']} 的局部含义",
                        "standalone": candidate["i"] == first,
                        "reason": "独立名词性指称",
                    }
                    for candidate in candidates
                ],
                "relations": [
                    {
                        "pattern": "separate",
                        "proposition": "第一个对象把第二个对象与共享状态分离。",
                        "participants": [
                            {"i": first, "sense_hint": "第一个局部对象"},
                            {"i": second, "sense_hint": "第二个局部对象"},
                        ],
                        "reason": "原文明确表达彼此隔离",
                    }
                ],
            },
            actual_model_id=self.model_id,
            input_tokens=100,
            output_tokens=50,
        )


class _InvalidParticipantClient:
    model_id = "fake-invalid-inventory-model"

    def __init__(self) -> None:
        self.requests = 0

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> JsonChatResult:
        del system_prompt, max_output_tokens
        self.requests += 1
        payload = json.loads(user_prompt)
        candidates = payload["block"]["candidates"]
        return JsonChatResult(
            payload={
                "terms": [
                    {
                        "i": candidate["i"],
                        "term": "accepted",
                        "sense": "resolved",
                        "sense_hint": "局部含义",
                        "standalone": False,
                        "reason": "测试",
                    }
                    for candidate in candidates
                ],
                "relations": [
                    {
                        "pattern": "separate",
                        "proposition": "测试关系",
                        "participants": [{"i": 999, "sense_hint": "越界对象"}],
                        "reason": "测试",
                    }
                ],
            },
            actual_model_id=self.model_id,
            input_tokens=10,
            output_tokens=5,
        )


class _SingleParticipantClient:
    model_id = "fake-single-participant-model"

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> JsonChatResult:
        del system_prompt, max_output_tokens
        payload = json.loads(user_prompt)
        candidates = payload["block"]["candidates"]
        return JsonChatResult(
            payload={
                "terms": [
                    {
                        "i": candidate["i"],
                        "term": "accepted",
                        "sense": "resolved",
                        "sense_hint": "局部含义",
                        "standalone": False,
                        "reason": "测试",
                    }
                    for candidate in candidates
                ],
                "relations": [
                    {
                        "pattern": "protect",
                        "proposition": "测试关系",
                        "participants": [{"i": 0, "sense_hint": "唯一端点"}],
                        "reason": "测试",
                    }
                ],
            },
            actual_model_id=self.model_id,
            input_tokens=10,
            output_tokens=5,
        )


class _ContainInventoryClient:
    model_id = "fake-contain-inventory-model"

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> JsonChatResult:
        del system_prompt, max_output_tokens
        payload = json.loads(user_prompt)
        candidates = payload["block"]["candidates"]
        return JsonChatResult(
            payload={
                "terms": [
                    {
                        "i": candidate["i"],
                        "term": "accepted",
                        "sense": "resolved",
                        "sense_hint": f"{candidate['display']} 的局部含义",
                        "standalone": False,
                        "reason": "独立名词性指称",
                    }
                    for candidate in candidates
                ],
                "relations": [
                    {
                        "pattern": "contain",
                        "proposition": "三个组件包含模型、环境和外部内容。",
                        "participants": [
                            {"i": candidate["i"], "sense_hint": candidate["display"]}
                            for candidate in candidates[:4]
                        ],
                        "reason": "原文明确列出集合及其三个成员",
                    }
                ],
            },
            actual_model_id=self.model_id,
            input_tokens=100,
            output_tokens=50,
        )


def _snapshot(text: str) -> SnapshotData:
    block = SourceBlock(
        snapshot_id="snapshot_semantic_inventory",
        doc_id="doc_semantic_inventory",
        doc_version=1,
        doc_title="Agent execution",
        block_id="block_semantic_inventory",
        block_index=1,
        block_ordinal=0,
        text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        section_path=["Execution"],
    )
    return SnapshotData(
        snapshot_id=block.snapshot_id,
        manifest_hash="a" * 64,
        root=str(Path.cwd()),
        documents=(DocumentVersionIdentity(doc_id=block.doc_id, doc_version=1),),
        blocks=(block,),
    )


def test_joint_inventory_materializes_only_standalone_or_relation_endpoints() -> None:
    client = _InventoryClient()
    compiler = SemanticInventoryCompiler(
        client,
        max_input_chars=60_000,
        max_output_tokens=2_000,
    )

    result = asyncio.run(
        compiler.compile(
            snapshot=_snapshot(
                "Worktree isolates each Agent session while checkpoint persists execution state."
            ),
            scanner=CandidateScanner(),
            concurrency=1,
        )
    )

    assert result.stats["accepted_terms"] == result.stats["resolved_terms"]
    assert 2 <= result.stats["materialized_concepts"] < result.stats["accepted_terms"]
    assert result.stats["term_only"] > 0
    assert result.stats["functional_assertions"] == 1
    assert result.assertions[0].pattern.value == "separate"
    assert set(result.assertions[0].participant_concept_ids) == {
        concept.concept_id for concept in result.concepts
    }
    assert result.calculate_inventory_hash() == result.inventory_hash
    assert "standalone=true" in SEMANTIC_INVENTORY_SYSTEM_PROMPT
    assert "只是词面直译、同义重复、上位类别" in SEMANTIC_INVENTORY_SYSTEM_PROMPT
    assert "每个候选序号必须且只能在 terms 中出现一次" in SEMANTIC_INVENTORY_SYSTEM_PROMPT
    assert "不要因为词面在句法上修饰了另一个词" in SEMANTIC_INVENTORY_SYSTEM_PROMPT
    assert "不能为了使用已有名词而制造关系" in SEMANTIC_INVENTORY_SYSTEM_PROMPT
    assert "pattern 单独表达关系类别" in SEMANTIC_INVENTORY_SYSTEM_PROMPT
    assert "原文使用监督、允许、提供、记录、" in SEMANTIC_INVENTORY_SYSTEM_PROMPT


def test_contain_relation_links_a_set_to_multiple_members() -> None:
    compiler = SemanticInventoryCompiler(
        _ContainInventoryClient(),
        max_input_chars=60_000,
        max_output_tokens=2_000,
        max_retries=0,
    )

    result = asyncio.run(
        compiler.compile(
            snapshot=_snapshot(
                "Three components contain the model, the environment, and external content."
            ),
            scanner=CandidateScanner(),
            concurrency=1,
        )
    )

    assert result.stats["functional_assertions"] == 1
    relation = result.assertions[0]
    assert relation.pattern.value == "contain"
    assert len(relation.participant_concept_ids) == 4
    assert "contain（包含）" in SEMANTIC_INVENTORY_SYSTEM_PROMPT
    assert "方向由整体、集合指向成员或内容" in SEMANTIC_INVENTORY_SYSTEM_PROMPT
    assert "在容器中运行、存放于某位置" in SEMANTIC_INVENTORY_SYSTEM_PROMPT


def test_inconsistent_relation_candidate_is_rejected_without_failing_block() -> None:
    client = _InvalidParticipantClient()
    compiler = SemanticInventoryCompiler(
        client,
        max_input_chars=60_000,
        max_output_tokens=2_000,
        max_retries=0,
    )

    result = asyncio.run(
        compiler.compile(
            snapshot=_snapshot("Worktree isolates each Agent session."),
            scanner=CandidateScanner(),
            concurrency=1,
        )
    )
    assert client.requests == 1
    assert result.stats["functional_assertions"] == 0
    assert result.stats["rejected_relation_candidates"] == 1


def test_relation_with_fewer_than_two_endpoints_is_rejected() -> None:
    compiler = SemanticInventoryCompiler(
        _SingleParticipantClient(),
        max_input_chars=60_000,
        max_output_tokens=2_000,
        max_retries=0,
    )

    result = asyncio.run(
        compiler.compile(
            snapshot=_snapshot("Sandbox protects Agent execution."),
            scanner=CandidateScanner(),
            concurrency=1,
        )
    )

    assert result.stats["functional_assertions"] == 0
    assert result.stats["rejected_relation_candidates"] == 1
