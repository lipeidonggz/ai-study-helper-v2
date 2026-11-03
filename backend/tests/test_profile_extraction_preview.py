from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import httpx

from app.main import create_app
from app.profile_extraction.models import (
    ExtractionChannel,
    ExtractionProfileConfig,
    PreviewRunRequest,
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

TOKEN = "profile-preview-test-token"


class _MemoryCredentials:
    def __init__(self) -> None:
        self._values = {"chat-api-key": "test-key"}

    def get(self, username: str) -> str | None:
        return self._values.get(username)

    def set(self, username: str, value: str) -> None:
        self._values[username] = value

    def delete(self, username: str) -> None:
        self._values.pop(username, None)


class _AliasCandidateProvider:
    """按通道返回两个名称不同、仅 alias 相连的候选。"""

    def __init__(
        self,
        *,
        invalid_source: bool = False,
        same_name: bool = False,
    ) -> None:
        self.invalid_source = invalid_source
        self.same_name = same_name
        self.requests: list[tuple[ChatMessage, ...]] = []
        self._profile = ChatProviderProfile(
            provider="fake",
            model_id="fake-profile-preview",
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
        block_id = payload["evidence_blocks"][0]["block_id"]
        source_id = "doc_missing#v0001#c1#b99999" if self.invalid_source else block_id
        if "ec_aaaaaaaaaaaaaaaa" in messages[0].content:
            candidates = [
                {
                    "name": "Alpha",
                    "aliases": ["Alias Alpha"],
                    "summary": "Alpha 是一个独立知识对象。",
                    "source_block_ids": [source_id],
                }
            ]
        elif "ec_bbbbbbbbbbbbbbbb" in messages[0].content:
            candidates = [
                {
                    "name": "Alpha" if self.same_name else "Alias Alpha",
                    "aliases": ["Alias Alpha"] if self.same_name else [],
                    "summary": "Alias Alpha 是另一个独立输入。",
                    "source_block_ids": [source_id],
                }
            ]
        else:
            candidates = []
        content = json.dumps({"candidates": candidates}, ensure_ascii=False)
        return ChatCompletionResult(
            content=content,
            request_id=f"fake-{len(self.requests)}",
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
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        "<title>Profile Preview Document</title></head><body><article>"
        "<h1>Profile Preview Document</h1>"
        "<p>Alpha 与 Alias Alpha 在本段中具有独立说明。</p>"
        "<p>第二段提供用于整篇抽取的补充证据。</p>"
        "</article></body></html>"
    ).encode()


async def _wait_for_task(
    client: httpx.AsyncClient,
    task_id: str,
) -> dict[str, object]:
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


def _ingest_paths(data_root: Path, sources: list[Path]) -> str:
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
                        {"source_kind": "file", "source_value": str(source)} for source in sources
                    ]
                },
            )
            created.raise_for_status()
            task = await _wait_for_task(client, created.json()["task_id"])
            assert task["status"] == "succeeded"
            return str(task["snapshot_id"])

    return asyncio.run(run())


def _ingest_document(data_root: Path) -> str:
    source = data_root / "profile-preview.html"
    source.write_bytes(_html())
    return _ingest_paths(data_root, [source])


def _two_alias_channels() -> ExtractionProfileConfig:
    return ExtractionProfileConfig(
        name="alias dedup probe",
        description="验证主名称精确去重。",
        extraction_channels=[
            ExtractionChannel(
                channel_id="ec_aaaaaaaaaaaaaaaa",
                name="通道 A",
                purpose="返回 Alpha。",
                activation="正文出现 Alpha 时。",
                include_criteria=["Alpha 有独立说明"],
                exclude_criteria=[],
                granularity_rule="一个对象一个 candidate。",
            ),
            ExtractionChannel(
                channel_id="ec_bbbbbbbbbbbbbbbb",
                name="通道 B",
                purpose="返回 Alias Alpha。",
                activation="正文出现 Alias Alpha 时。",
                include_criteria=["Alias Alpha 有独立说明"],
                exclude_criteria=[],
                granularity_rule="一个对象一个 candidate。",
            ),
        ],
    )


def test_preview_dedupes_primary_name_only_and_reuses_completed_run(
    tmp_path: Path,
) -> None:
    snapshot_id = _ingest_document(tmp_path)
    reader = RetrievalReadService(tmp_path).bind_snapshot(snapshot_id)
    doc = reader.list_documents().documents[0]
    credentials = _MemoryCredentials()
    provider = _AliasCandidateProvider()
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

    async def run() -> None:
        await previews.start()
        try:
            draft = profiles.create_profile(ProfileCreateRequest(name="alias probe"))
            saved = profiles.save_draft(
                draft.profile_id,
                ProfileSaveRequest(config=_two_alias_channels()),
            )
            request = PreviewRunRequest(
                profile_id=saved.profile_id,
                snapshot_id=snapshot_id,
                document_ids=[doc.doc_id],
                execution_mode="force_new",
            )
            created = await previews.create(request)
            completed = await previews.wait_for_terminal(created.run_id)
            assert completed.status == "completed"
            assert [candidate.name for candidate in completed.candidates] == [
                "Alias Alpha",
                "Alpha",
            ]
            calls_after_first = len(provider.requests)

            reused = await previews.create(
                request.model_copy(update={"execution_mode": "reuse_if_exact"})
            )
            assert reused.reused is True
            assert reused.run_id == completed.run_id
            assert len(provider.requests) == calls_after_first

            rerun = await previews.create(request)
            assert rerun.run_id != completed.run_id
            await previews.wait_for_terminal(rerun.run_id)
        finally:
            await previews.stop()

    asyncio.run(run())


def test_exact_primary_name_merges_channel_outputs(tmp_path: Path) -> None:
    snapshot_id = _ingest_document(tmp_path)
    doc = RetrievalReadService(tmp_path).bind_snapshot(snapshot_id).list_documents().documents[0]
    credentials = _MemoryCredentials()
    provider = _AliasCandidateProvider(same_name=True)
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

    async def run() -> None:
        await previews.start()
        try:
            draft = profiles.create_profile(ProfileCreateRequest(name="exact dedup"))
            saved = profiles.save_draft(
                draft.profile_id,
                ProfileSaveRequest(config=_two_alias_channels()),
            )
            created = await previews.create(
                PreviewRunRequest(
                    profile_id=saved.profile_id,
                    snapshot_id=snapshot_id,
                    document_ids=[doc.doc_id],
                    execution_mode="force_new",
                )
            )
            completed = await previews.wait_for_terminal(created.run_id)
            assert len(completed.candidates) == 1
            assert completed.candidates[0].name == "Alpha"
            assert completed.candidates[0].channel_ids == [
                "ec_aaaaaaaaaaaaaaaa",
                "ec_bbbbbbbbbbbbbbbb",
            ]
        finally:
            await previews.stop()

    asyncio.run(run())


def test_multi_document_preview_keeps_each_document_candidate_set_separate(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.html"
    second = tmp_path / "second.html"
    first.write_bytes(_html())
    second.write_bytes(_html())
    snapshot_id = _ingest_paths(tmp_path, [first, second])
    documents = RetrievalReadService(tmp_path).bind_snapshot(snapshot_id).list_documents()
    credentials = _MemoryCredentials()
    provider = _AliasCandidateProvider()
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

    async def run() -> None:
        await previews.start()
        try:
            draft = profiles.create_profile(ProfileCreateRequest(name="multi document"))
            saved = profiles.save_draft(
                draft.profile_id,
                ProfileSaveRequest(config=_two_alias_channels()),
            )
            created = await previews.create(
                PreviewRunRequest(
                    profile_id=saved.profile_id,
                    snapshot_id=snapshot_id,
                    document_ids=[document.doc_id for document in documents.documents],
                    execution_mode="force_new",
                )
            )
            completed = await previews.wait_for_terminal(created.run_id)
            assert completed.status == "completed"
            assert {candidate.doc_id for candidate in completed.candidates} == {
                document.doc_id for document in documents.documents
            }
            assert len(completed.candidates) == 4
            assert len({candidate.candidate_id for candidate in completed.candidates}) == 4
        finally:
            await previews.stop()

    asyncio.run(run())


def test_preview_with_invalid_source_fails_without_formal_candidates(tmp_path: Path) -> None:
    snapshot_id = _ingest_document(tmp_path)
    doc = RetrievalReadService(tmp_path).bind_snapshot(snapshot_id).list_documents().documents[0]
    credentials = _MemoryCredentials()
    provider = _AliasCandidateProvider(invalid_source=True)
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

    async def run() -> None:
        await previews.start()
        try:
            draft = profiles.create_profile(ProfileCreateRequest(name="invalid source"))
            saved = profiles.save_draft(
                draft.profile_id,
                ProfileSaveRequest(config=_two_alias_channels()),
            )
            created = await previews.create(
                PreviewRunRequest(
                    profile_id=saved.profile_id,
                    snapshot_id=snapshot_id,
                    document_ids=[doc.doc_id],
                    execution_mode="force_new",
                )
            )
            failed = await previews.wait_for_terminal(created.run_id)
            assert failed.status == "failed"
            assert failed.error_code == "candidate_source_invalid"
            assert failed.candidates == []
        finally:
            await previews.stop()

    asyncio.run(run())
