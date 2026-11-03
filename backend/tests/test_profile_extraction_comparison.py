from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import httpx
import pytest

from app.main import create_app
from app.profile_extraction.checklist_service import ChecklistService
from app.profile_extraction.comparison_service import ComparisonService
from app.profile_extraction.errors import ProfileExtractionError
from app.profile_extraction.models import (
    ChecklistCreateRequest,
    ChecklistSaveRequest,
    DocumentChecklist,
    DocumentChecklistItem,
    ExtractionChannel,
    ExtractionProfileConfig,
    PreviewRunRequest,
    ProfileComparisonConcludeRequest,
    ProfileComparisonCreateRequest,
    ProfileCreateRequest,
    ProfileSaveRequest,
)
from app.profile_extraction.preview_service import ProfilePreviewService
from app.profile_extraction.profile_service import ProfileService
from app.providers.chat import (
    ChatCompletionResult,
    ChatMessage,
    ChatProviderProfile,
    ChatStreamChunk,
)
from app.providers.embeddings import FakeEmbeddingProvider
from app.retrieval.service import RetrievalReadService
from app.storage.database import StorageDatabase
from tests.helpers import fixture_bytes

TOKEN = "profile-comparison-test-token"


class _MemoryCredentials:
    def __init__(self) -> None:
        self._values = {"chat-api-key": "test-key"}

    def get(self, username: str) -> str | None:
        return self._values.get(username)

    def set(self, username: str, value: str) -> None:
        self._values[username] = value

    def delete(self, username: str) -> None:
        self._values.pop(username, None)


class _ComparisonProvider:
    """根据 Profile 名称返回 A/B 有差异的确定候选。"""

    def __init__(self) -> None:
        self.requests: list[tuple[ChatMessage, ...]] = []
        self._profile = ChatProviderProfile(
            provider="fake",
            model_id="fake-profile-comparison",
            base_url="fake://chat",
            temperature=0.0,
            max_output_tokens=8192,
            thinking_enabled=False,
        )

    @property
    def profile(self) -> ChatProviderProfile:
        return self._profile

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> ChatCompletionResult:
        self.requests.append(tuple(messages))
        payload = json.loads(messages[-1].content)
        system = messages[0].content
        names = ["Shared", "Only A"] if "Profile A" in system else ["Shared", "Only B"]
        block_ids = [block["block_id"] for block in payload["evidence_blocks"]]
        name_offsets = {"Shared": 0, "Only A": 1, "Only B": 2}
        content = json.dumps(
            {
                "candidates": [
                    {
                        "name": name,
                        "aliases": [],
                        "summary": f"{name} 的独立说明。",
                        "source_block_ids": [block_ids[name_offsets[name] % len(block_ids)]],
                    }
                    for name in names
                ]
            },
            ensure_ascii=False,
        )
        return ChatCompletionResult(
            content=content,
            request_id=f"comparison-{len(self.requests)}",
            actual_model_id=self.profile.model_id,
            finish_reason="stop",
            input_tokens=10,
            output_tokens=5,
        )

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[ChatStreamChunk]:
        result = await self.complete(messages, max_output_tokens=max_output_tokens)
        yield ChatStreamChunk(delta=result.content, result=result)


def _html() -> bytes:
    return fixture_bytes("complex-article.html")


async def _wait_for_task(client: httpx.AsyncClient, task_id: str) -> dict[str, object]:
    for _ in range(300):
        response = await client.get(
            f"/api/tasks/{task_id}",
            headers={"x-launch-token": TOKEN},
        )
        response.raise_for_status()
        task = response.json()
        if task["status"] in {"succeeded", "failed", "canceled", "interrupted"}:
            return task
        await asyncio.sleep(0.02)
    raise AssertionError("ingestion task did not finish")


def _ingest_document(data_root: Path) -> str:
    first_source = data_root / "comparison-first.html"
    second_source = data_root / "comparison-second.html"
    first_source.write_bytes(_html())
    second_source.write_bytes(_html())

    async def run() -> str:
        app = create_app(
            TOKEN,
            data_root=data_root,
            embedding_provider_factory=FakeEmbeddingProvider,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            created = await client.post(
                "/api/tasks/ingest",
                headers={"x-launch-token": TOKEN},
                json={
                    "sources": [
                        {"source_kind": "file", "source_value": str(first_source)},
                        {"source_kind": "file", "source_value": str(second_source)},
                    ]
                },
            )
            created.raise_for_status()
            task = await _wait_for_task(client, created.json()["task_id"])
            assert task["status"] == "succeeded"
            return str(task["snapshot_id"])

    return asyncio.run(run())


def _profile_config(name: str, channel_id: str) -> ExtractionProfileConfig:
    return ExtractionProfileConfig(
        name=name,
        description="用户定义的比较 Profile。",
        extraction_channels=[
            ExtractionChannel(
                channel_id=channel_id,
                name="独立知识对象",
                purpose="发现正文中的独立知识对象。",
                activation="正文给出独立说明时。",
                include_criteria=["有稳定名称", "有独立事实"],
                exclude_criteria=["只有导航文字"],
                granularity_rule="一个对象一个 candidate。",
            )
        ],
    )


def test_compare_requires_must_override_and_freezes_conclusion(tmp_path: Path) -> None:
    snapshot_id = _ingest_document(tmp_path)
    reader = RetrievalReadService(tmp_path).bind_snapshot(snapshot_id)
    docs = reader.list_documents().documents
    assert len(docs) == 2
    blocks = reader.list_blocks(docs[0].doc_id).blocks
    assert len(blocks) >= 4
    credentials = _MemoryCredentials()
    provider = _ComparisonProvider()
    database = StorageDatabase(tmp_path)
    profiles = ProfileService(
        tmp_path,
        credentials,
        chat_provider_factory=lambda _key: provider,
        database=database,
    )
    previews = ProfilePreviewService(
        tmp_path,
        profiles,
        credentials,
        chat_provider_factory=lambda _key: provider,
        database=database,
    )
    checklists = ChecklistService(
        tmp_path,
        credentials,
        chat_provider_factory=lambda _key: provider,
        database=database,
    )
    comparisons = ComparisonService(tmp_path, database=database)

    async def run() -> None:
        await previews.start()
        try:
            profile_a = profiles.create_profile(ProfileCreateRequest(name="Profile A"))
            profile_a = profiles.save_draft(
                profile_a.profile_id,
                ProfileSaveRequest(config=_profile_config("Profile A", "ec_aaaaaaaaaaaaaaaa")),
            )
            profile_b = profiles.create_profile(ProfileCreateRequest(name="Profile B"))
            profile_b = profiles.save_draft(
                profile_b.profile_id,
                ProfileSaveRequest(config=_profile_config("Profile B", "ec_bbbbbbbbbbbbbbbb")),
            )
            run_a = await previews.create(
                PreviewRunRequest(
                    profile_id=profile_a.profile_id,
                    snapshot_id=snapshot_id,
                    document_ids=[document.doc_id for document in docs],
                    execution_mode="force_new",
                )
            )
            run_a = await previews.wait_for_terminal(run_a.run_id)
            run_b = await previews.create(
                PreviewRunRequest(
                    profile_id=profile_b.profile_id,
                    snapshot_id=snapshot_id,
                    document_ids=[document.doc_id for document in docs],
                    execution_mode="force_new",
                )
            )
            run_b = await previews.wait_for_terminal(run_b.run_id)
            assert run_a.status == run_b.status == "completed"

            frozen_checklists: list[DocumentChecklist] = []
            for document in docs:
                document_blocks = reader.list_blocks(document.doc_id).blocks
                shared_block_id = document_blocks[0].block_id
                only_a_block_id = document_blocks[1].block_id
                missing_block_id = document_blocks[3].block_id
                checklist = checklists.create(
                    ChecklistCreateRequest(
                        snapshot_id=snapshot_id,
                        doc_id=document.doc_id,
                        evaluation_note="功能性比较",
                    )
                )
                checklist = checklists.save(
                    checklist.checklist_id,
                    ChecklistSaveRequest(
                        evaluation_note="功能性比较",
                        items=[
                            DocumentChecklistItem(
                                item_id="dci_0000000000000001",
                                name="Shared",
                                priority="must",
                                source_block_ids=[shared_block_id],
                                origin="manual",
                            ),
                            DocumentChecklistItem(
                                item_id="dci_0000000000000002",
                                name="Missing from both",
                                priority="must",
                                source_block_ids=[missing_block_id],
                                origin="manual",
                            ),
                            DocumentChecklistItem(
                                item_id="dci_0000000000000003",
                                name="Only A",
                                priority="should",
                                source_block_ids=[only_a_block_id],
                                origin="manual",
                            ),
                        ],
                    ),
                )
                frozen_checklists.append(checklists.freeze(checklist.checklist_id))

            comparison = comparisons.create(
                ProfileComparisonCreateRequest(
                    run_a_id=run_a.run_id,
                    run_b_id=run_b.run_id,
                    checklist_ids=[checklist.checklist_id for checklist in frozen_checklists],
                )
            )
            assert len(comparison.document_results) == 2
            result = comparison.document_results[0]
            assert result["profile_a"]["must"] == {
                "matched": 1,
                "missing": 1,
                "uncertain": 0,
            }
            assert result["profile_a"]["should"]["matched"] == 1
            assert result["profile_b"]["should"]["missing"] == 1
            assert result["profile_b"]["extra_candidate_count"] == 1

            with pytest.raises(ProfileExtractionError, match="override reason"):
                comparisons.conclude(
                    comparison.comparison_id,
                    ProfileComparisonConcludeRequest(outcome="a_better"),
                )

            concluded = comparisons.conclude(
                comparison.comparison_id,
                ProfileComparisonConcludeRequest(
                    outcome="a_better",
                    outcome_note="接受两份 Profile 共同缺少的 must 项。",
                    must_override_reasons={
                        document.doc_id: "测试文档中该对象不适用。" for document in docs
                    },
                    document_outcomes={document.doc_id: "a_better" for document in docs},
                ),
            )
            assert concluded.status == "concluded"
            assert concluded.outcome == "a_better"
            assert concluded.document_results[0]["must_override_reason"] == (
                "测试文档中该对象不适用。"
            )
            with pytest.raises(ProfileExtractionError, match="already concluded"):
                comparisons.conclude(
                    comparison.comparison_id,
                    ProfileComparisonConcludeRequest(outcome="equivalent"),
                )
        finally:
            await previews.stop()

    asyncio.run(run())
