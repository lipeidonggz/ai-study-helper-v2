from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import httpx
import pytest

from app.main import create_app
from app.profile_extraction.errors import ProfileExtractionError
from app.profile_extraction.models import (
    ExtractionChannel,
    ExtractionProfileConfig,
    PreviewRunPlanRequest,
    PreviewRunRequest,
    ProfileCreateRequest,
    ProfileSaveRequest,
)
from app.profile_extraction.preview_service import ProfilePreviewService
from app.profile_extraction.profile_service import ProfileService
from app.profile_extraction.prompt_compiler import compile_profile_prompts
from app.providers.chat import (
    ChatCompletionResult,
    ChatMessage,
    ChatProviderError,
    ChatProviderProfile,
    ChatStreamChunk,
)
from app.providers.embeddings import FakeEmbeddingProvider
from app.retrieval.models import RetrievalBlock, RetrievalSectionContext
from app.retrieval.service import RetrievalReadService
from app.storage.database import StorageDatabase
from app.storage.profile_extraction_models import PreviewCandidateRow

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
        audit_issue_candidate: str | None = None,
        ambiguous_name_candidate: str | None = None,
        document_anchor_mode: str | None = None,
        document_anchor_name: str | None = None,
        enrich_existing_candidate: bool = False,
        enrich_existing_proposed_name: str | None = None,
        enrich_existing_same_channel: bool = False,
        fail_audit: bool = False,
        fail_document_anchor: bool = False,
        invalid_alias: bool = False,
        invalid_document_anchor_alias: bool = False,
        invalid_document_anchor_source: bool = False,
        invalid_source: bool = False,
        misaligned_name_candidate: str | None = None,
        same_name: bool = False,
    ) -> None:
        self.audit_issue_candidate = audit_issue_candidate
        self.ambiguous_name_candidate = ambiguous_name_candidate
        self.document_anchor_mode = document_anchor_mode
        self.document_anchor_name = document_anchor_name
        self.enrich_existing_candidate = enrich_existing_candidate
        self.enrich_existing_proposed_name = enrich_existing_proposed_name
        self.enrich_existing_same_channel = enrich_existing_same_channel
        self.fail_audit = fail_audit
        self.fail_document_anchor = fail_document_anchor
        self.invalid_alias = invalid_alias
        self.invalid_document_anchor_alias = invalid_document_anchor_alias
        self.invalid_document_anchor_source = invalid_document_anchor_source
        self.invalid_source = invalid_source
        self.misaligned_name_candidate = misaligned_name_candidate
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
        if "文档级标题消歧器" in messages[0].content:
            if self.fail_document_anchor:
                raise ChatProviderError(
                    code="document_anchor_unavailable",
                    message="document anchor unavailable",
                    retryable=False,
                )
            if self.document_anchor_mode == "truncated":
                return ChatCompletionResult(
                    content='{"items":[',
                    request_id=f"document-anchor-{len(self.requests)}",
                    actual_model_id=self.profile.model_id,
                    finish_reason="length",
                    input_tokens=30,
                    output_tokens=12,
                )
            if self.document_anchor_mode == "invalid_json":
                return ChatCompletionResult(
                    content="not valid json",
                    request_id=f"document-anchor-{len(self.requests)}",
                    actual_model_id=self.profile.model_id,
                    finish_reason="stop",
                    input_tokens=30,
                    output_tokens=12,
                )
            if self.document_anchor_mode == "invalid_schema":
                return ChatCompletionResult(
                    content='{"items":[{"candidate_ref":"bad"}]}',
                    request_id=f"document-anchor-{len(self.requests)}",
                    actual_model_id=self.profile.model_id,
                    finish_reason="stop",
                    input_tokens=30,
                    output_tokens=12,
                )
            items = []
            for candidate in payload["candidates"]:
                name = (
                    self.document_anchor_name
                    if candidate["provisional_name"] == "Alpha"
                    and self.document_anchor_name is not None
                    else candidate["provisional_name"]
                )
                aliases = list(candidate["aliases"])
                if name != candidate["provisional_name"]:
                    aliases.append(candidate["provisional_name"])
                if self.invalid_document_anchor_alias:
                    aliases.append("ghost anchor alias")
                anchor_source_block_ids = candidate["source_block_ids"][:1]
                if self.invalid_document_anchor_source:
                    anchor_source_block_ids = ["doc_missing#v0001#c1#b99999"]
                items.append(
                    {
                        "candidate_ref": candidate["candidate_ref"],
                        "provisional_name": candidate["provisional_name"],
                        "title_kind": (
                            "common_concept"
                            if name != candidate["provisional_name"]
                            else "proper_name"
                        ),
                        "standalone_clear": name == candidate["provisional_name"],
                        "missing_anchor_types": (
                            [] if name == candidate["provisional_name"] else ["actor", "scope"]
                        ),
                        "final_name": name,
                        "aliases": aliases,
                        "anchor_source_block_ids": anchor_source_block_ids,
                        "reason": "测试文档上下文提供了名称锚点。",
                    }
                )
            return ChatCompletionResult(
                content=json.dumps({"items": items}, ensure_ascii=False),
                request_id=f"document-anchor-{len(self.requests)}",
                actual_model_id=self.profile.model_id,
                finish_reason="stop",
                input_tokens=30,
                output_tokens=12,
            )
        if "候选标题区分度审查器" in messages[0].content:
            if self.fail_audit:
                raise ChatProviderError(
                    code="audit_unavailable",
                    message="semantic audit unavailable",
                    retryable=False,
                )
            items = []
            for candidate in payload["candidate_titles"]:
                ambiguous = candidate["name"] == self.ambiguous_name_candidate
                items.append(
                    {
                        "candidate_ref": candidate["candidate_ref"],
                        "verdict": "ambiguous" if ambiguous else "clear",
                        "scope_anchor": None if ambiguous else "测试知识库",
                        "suggested_name": "Alpha 知识对象" if ambiguous else None,
                        "reason": (
                            "只看到标题时无法确定对象范围"
                            if ambiguous
                            else "标题在当前测试知识库内可区分"
                        ),
                    }
                )
            return ChatCompletionResult(
                content=json.dumps({"items": items}, ensure_ascii=False),
                request_id=f"name-scope-{len(self.requests)}",
                actual_model_id=self.profile.model_id,
                finish_reason="stop",
                input_tokens=10,
                output_tokens=5,
            )
        if "候选名称含义审查器" in messages[0].content:
            if self.fail_audit:
                raise ChatProviderError(
                    code="audit_unavailable",
                    message="semantic audit unavailable",
                    retryable=False,
                )
            items = []
            for candidate in payload["candidates"]:
                misaligned = candidate["name"] == self.misaligned_name_candidate
                items.append(
                    {
                        "candidate_ref": candidate["candidate_ref"],
                        "verdict": "misaligned" if misaligned else "aligned",
                        "suggested_name": "Alpha 对象" if misaligned else None,
                        "reason": (
                            "标题与本地摘要的中心对象不一致"
                            if misaligned
                            else "标题与本地摘要指向同一对象"
                        ),
                    }
                )
            return ChatCompletionResult(
                content=json.dumps({"items": items}, ensure_ascii=False),
                request_id=f"name-meaning-{len(self.requests)}",
                actual_model_id=self.profile.model_id,
                finish_reason="stop",
                input_tokens=12,
                output_tokens=6,
            )
        if "候选审查器" in messages[0].content:
            if self.fail_audit:
                raise ChatProviderError(
                    code="audit_unavailable",
                    message="semantic audit unavailable",
                    retryable=False,
                )
            items = []
            for candidate in payload["candidates"]:
                suspicious = candidate["name"] == self.audit_issue_candidate
                items.append(
                    {
                        "candidate_ref": candidate["candidate_ref"],
                        "verdict": "suspicious" if suspicious else "clean",
                        "issues": (
                            [
                                {
                                    "code": "alias_not_same_object",
                                    "message": "测试审查疑点",
                                }
                            ]
                            if suspicious
                            else []
                        ),
                        "reason": "测试审查意见",
                        "evidence_block_ids": candidate["source_block_ids"][:1],
                    }
                )
            return ChatCompletionResult(
                content=json.dumps({"items": items}, ensure_ascii=False),
                request_id=f"audit-{len(self.requests)}",
                actual_model_id=self.profile.model_id,
                finish_reason="stop",
                input_tokens=20,
                output_tokens=10,
            )
        block_id = payload["evidence_blocks"][0]["block_id"]
        source_id = "doc_missing#v0001#c1#b99999" if self.invalid_source else block_id
        if "ec_aaaaaaaaaaaaaaaa" in messages[0].content:
            existing_candidates = payload.get("existing_candidates", [])
            if self.enrich_existing_same_channel and existing_candidates:
                existing = existing_candidates[0]
                candidates = [
                    {
                        "name": existing["name"],
                        "aliases": [],
                        "summaries": ["后续窗口补充了同一对象的证据。"],
                        "selection_reason": "后续窗口提供同一对象的补充事实。",
                        "source_block_ids": [source_id],
                        "existing_candidate_ref": existing["candidate_ref"],
                    }
                ]
            else:
                candidates = [
                    {
                        "name": "Alpha",
                        "aliases": (
                            ["Alias missing from evidence"]
                            if self.invalid_alias
                            else ["Alias Alpha"]
                        ),
                        "summaries": ["Alpha 是一个独立知识对象。"],
                        "selection_reason": "正文同时提供 Alpha 的稳定名称和独立说明。",
                        "source_block_ids": [source_id],
                    }
                ]
        elif "ec_bbbbbbbbbbbbbbbb" in messages[0].content:
            existing_candidates = payload.get("existing_candidates", [])
            if self.enrich_existing_candidate and existing_candidates:
                existing = existing_candidates[0]
                candidates = [
                    {
                        "name": self.enrich_existing_proposed_name or existing["name"],
                        "aliases": ["Alias Alpha"],
                        "summaries": ["Alias Alpha 补充了该对象的另一种称呼。"],
                        "selection_reason": "正文补充了同一对象的稳定别名。",
                        "source_block_ids": [source_id],
                        "existing_candidate_ref": existing["candidate_ref"],
                    }
                ]
            else:
                candidates = [
                    {
                        "name": "Alpha" if self.same_name else "Alias Alpha",
                        "aliases": ["Alias Alpha"] if self.same_name else [],
                        "summaries": ["Alias Alpha 是另一个独立输入。"],
                        "selection_reason": "正文把 Alias Alpha 作为独立对象说明。",
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


class _InvalidThenValidCandidateProvider:
    """首次返回损坏候选 JSON，第二次返回合法候选。"""

    def __init__(self, *, always_invalid: bool = False) -> None:
        self.always_invalid = always_invalid
        self.requests: list[tuple[ChatMessage, ...]] = []
        self.max_output_requests: list[int | None] = []
        self._profile = ChatProviderProfile(
            provider="fake",
            model_id="fake-invalid-candidate",
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
        self.max_output_requests.append(max_output_tokens)
        if self.always_invalid or len(self.requests) == 1:
            content = '{"candidates":['
            finish_reason = "length"
        else:
            payload = json.loads(messages[1].content)
            content = json.dumps(
                {
                    "candidates": [
                        {
                            "name": "Alpha",
                            "aliases": ["Alias Alpha"],
                            "summaries": ["Alpha 是一个独立知识对象。"],
                            "selection_reason": "正文提供 Alpha 的稳定名称和独立说明。",
                            "source_block_ids": [payload["evidence_blocks"][0]["block_id"]],
                        }
                    ]
                },
                ensure_ascii=False,
            )
            finish_reason = "stop"
        return ChatCompletionResult(
            content=content,
            request_id=f"invalid-candidate-{len(self.requests)}",
            actual_model_id=self.profile.model_id,
            finish_reason=finish_reason,
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


def _html_two_window_document() -> bytes:
    first = (
        "Alpha 与 Alias Alpha 在第一窗口中具有独立说明。"
        "Alpha 提供稳定的行为边界、适用条件和可验证的补充事实。"
    ) * 24
    second = (
        "第二窗口继续说明 Alpha 的运行状态与边界。"
        "这些内容用于验证后续抽取窗口能够看到前面的候选账本。"
    ) * 24
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        "<title>Two Window Document</title></head><body><article>"
        "<h1>Two Window Document</h1>"
        f"<p>{first}</p><p>{second}</p>"
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
            assert task["status"] == "succeeded", task.get("error_message")
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
            alpha = next(
                candidate for candidate in completed.candidates if candidate.name == "Alpha"
            )
            assert alpha.audit_status == "suspicious"
            assert any(
                finding.code == "alias_matches_separate_candidate"
                and finding.alias == "Alias Alpha"
                for finding in alpha.audit_findings
            )
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
            assert len(completed.candidates[0].selection_reasons) == 2
            assert {reason.channel_id for reason in completed.candidates[0].selection_reasons} == {
                "ec_aaaaaaaaaaaaaaaa",
                "ec_bbbbbbbbbbbbbbbb",
            }
            assert completed.channel_names == {
                "ec_aaaaaaaaaaaaaaaa": "通道 A",
                "ec_bbbbbbbbbbbbbbbb": "通道 B",
            }
            assert completed.channel_definitions["ec_aaaaaaaaaaaaaaaa"].activation == (
                "正文出现 Alpha 时。"
            )
            assert completed.channel_definitions["ec_aaaaaaaaaaaaaaaa"].include_criteria == [
                "Alpha 有独立说明"
            ]
        finally:
            await previews.stop()

    asyncio.run(run())


def test_later_channel_enriches_accumulated_candidate_without_duplicate(
    tmp_path: Path,
) -> None:
    snapshot_id = _ingest_document(tmp_path)
    doc = RetrievalReadService(tmp_path).bind_snapshot(snapshot_id).list_documents().documents[0]
    credentials = _MemoryCredentials()
    provider = _AliasCandidateProvider(
        enrich_existing_candidate=True,
        enrich_existing_proposed_name="Alias Alpha",
    )
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
            draft = profiles.create_profile(ProfileCreateRequest(name="channel accumulation"))
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

            assert completed.status == "completed"
            assert len(completed.candidates) == 1
            candidate = completed.candidates[0]
            assert candidate.name == "Alpha"
            assert candidate.summaries == [
                "Alpha 是一个独立知识对象。",
                "Alias Alpha 补充了该对象的另一种称呼。",
            ]
            assert candidate.aliases == ["Alias Alpha"]
            assert any(
                diagnostic["code"] == "candidate_existing_name_replaced"
                and diagnostic["proposed_name"] == "Alias Alpha"
                for diagnostic in completed.diagnostics
            )
            assert candidate.channel_ids == [
                "ec_aaaaaaaaaaaaaaaa",
                "ec_bbbbbbbbbbbbbbbb",
            ]
            assert {reason.channel_id for reason in candidate.selection_reasons} == {
                "ec_aaaaaaaaaaaaaaaa",
                "ec_bbbbbbbbbbbbbbbb",
            }

            channel_b_request = next(
                request
                for request in provider.requests
                if "ec_bbbbbbbbbbbbbbbb" in request[0].content
            )
            attachment = json.loads(channel_b_request[-1].content)["existing_candidates"]
            assert attachment[0]["name"] == "Alpha"
            assert attachment[0]["summaries"] == ["Alpha 是一个独立知识对象。"]
        finally:
            await previews.stop()

    asyncio.run(run())


def test_later_window_in_same_channel_enriches_accumulated_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "two-window-profile-preview.html"
    source.write_bytes(_html_two_window_document())
    snapshot_id = _ingest_paths(tmp_path, [source])
    reader = RetrievalReadService(tmp_path).bind_snapshot(snapshot_id)
    doc = reader.list_documents().documents[0]
    assert len(reader.list_blocks(doc.doc_id).blocks) >= 2
    credentials = _MemoryCredentials()
    provider = _AliasCandidateProvider(enrich_existing_same_channel=True)
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
    monkeypatch.setattr(
        "app.profile_extraction.preview_service.split_blocks",
        lambda blocks: [blocks[:1], blocks[1:]],
    )

    async def run() -> None:
        await previews.start()
        try:
            draft = profiles.create_profile(ProfileCreateRequest(name="window accumulation"))
            saved = profiles.save_draft(
                draft.profile_id,
                ProfileSaveRequest(
                    config=ExtractionProfileConfig(
                        name="window accumulation",
                        description="验证同通道后续窗口能看到累积候选。",
                        extraction_channels=[_two_alias_channels().extraction_channels[0]],
                    )
                ),
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

            assert completed.status == "completed", completed.error_message
            assert len(completed.candidates) == 1
            candidate = completed.candidates[0]
            assert candidate.name == "Alpha"
            assert candidate.summaries == [
                "Alpha 是一个独立知识对象。",
                "后续窗口补充了同一对象的证据。",
            ]
            assert len(candidate.selection_reasons) == 2
            assert len(candidate.source_block_ids) == 2

            channel_requests = [
                request
                for request in provider.requests
                if "ec_aaaaaaaaaaaaaaaa" in request[0].content
                and "文档级标题消歧器" not in request[0].content
            ]
            assert len(channel_requests) == 2
            first_attachment = json.loads(channel_requests[0][-1].content).get(
                "existing_candidates",
                [],
            )
            second_attachment = json.loads(channel_requests[1][-1].content)["existing_candidates"]
            assert first_attachment == []
            assert second_attachment[0]["name"] == "Alpha"
            assert second_attachment[0]["summaries"] == ["Alpha 是一个独立知识对象。"]
        finally:
            await previews.stop()

    asyncio.run(run())


def test_channel_contribution_rejects_unknown_existing_candidate_ref() -> None:
    block = _anchor_block("doc_ref", "b1", "Alpha 是一个独立对象。")
    content = json.dumps(
        {
            "candidates": [
                {
                    "name": "Alpha",
                    "aliases": [],
                    "summaries": ["Alpha 的补充说明。"],
                    "selection_reason": "正文提供补充说明。",
                    "source_block_ids": [block.block_id],
                    "existing_candidate_ref": "ca_0000000000000001",
                }
            ]
        },
        ensure_ascii=False,
    )

    with pytest.raises(ProfileExtractionError, match="outside its attachment"):
        ProfilePreviewService._parse_candidates(
            content,
            [block],
            existing_candidates=[],
        )


def test_candidate_output_invalid_is_retried_once_with_diagnostics(tmp_path: Path) -> None:
    provider = _InvalidThenValidCandidateProvider()
    credentials = _MemoryCredentials()
    database = StorageDatabase(tmp_path)
    profiles = ProfileService(
        tmp_path,
        credentials,
        chat_provider_factory=lambda _key: provider,
        database=database,
    )
    service = ProfilePreviewService(
        tmp_path,
        profiles,
        credentials,
        chat_provider_factory=lambda _key: provider,
        database=database,
    )
    prompt = compile_profile_prompts(_two_alias_channels())[0]
    block = _anchor_block("doc_retry", "b1", "Alpha 是独立对象，也叫 Alias Alpha。")

    result = asyncio.run(
        service._run_channel(
            provider=provider,
            channel_prompt=prompt,
            doc_id="doc_retry",
            doc_title="Retry",
            blocks=[block],
            window_index=0,
            existing_candidates=None,
            operation="discovery",
            semaphore=asyncio.Semaphore(1),
        )
    )

    assert len(provider.requests) == 2
    assert provider.max_output_requests == [16_384, 24_576]
    assert result.usage["attempts"] == 2
    assert result.usage["input_tokens"] == 20
    assert result.diagnostics[0]["code"] == "candidate_output_retry"
    assert result.diagnostics[0]["finish_reason"] == "length"


def test_candidate_output_invalid_stops_after_one_retry(tmp_path: Path) -> None:
    provider = _InvalidThenValidCandidateProvider(always_invalid=True)
    credentials = _MemoryCredentials()
    database = StorageDatabase(tmp_path)
    profiles = ProfileService(
        tmp_path,
        credentials,
        chat_provider_factory=lambda _key: provider,
        database=database,
    )
    service = ProfilePreviewService(
        tmp_path,
        profiles,
        credentials,
        chat_provider_factory=lambda _key: provider,
        database=database,
    )
    prompt = compile_profile_prompts(_two_alias_channels())[0]
    block = _anchor_block("doc_retry", "b1", "Alpha 是独立对象。")

    with pytest.raises(ProfileExtractionError) as raised:
        asyncio.run(
            service._run_channel(
                provider=provider,
                channel_prompt=prompt,
                doc_id="doc_retry",
                doc_title="Retry",
                blocks=[block],
                window_index=0,
                existing_candidates=None,
                operation="discovery",
                semaphore=asyncio.Semaphore(1),
            )
        )

    assert len(provider.requests) == 2
    assert raised.value.code == "candidate_output_invalid"
    assert raised.value.diagnostics["doc_id"] == "doc_retry"
    assert raised.value.diagnostics["window_index"] == 0
    assert len(raised.value.diagnostics["attempts"]) == 2


def test_candidate_source_outside_current_window_is_dropped_without_failing_parse() -> None:
    block = _anchor_block("doc_ref", "b1", "Alpha 是一个独立对象。")
    content = json.dumps(
        {
            "candidates": [
                {
                    "name": "Alpha",
                    "aliases": [],
                    "summaries": ["Alpha 的补充说明。"],
                    "selection_reason": "当前窗口提供补充说明。",
                    "source_block_ids": [
                        block.block_id,
                        "doc_ref#v0001#c1#b99999",
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )

    candidates, diagnostics = ProfilePreviewService._parse_candidates(
        content,
        [block],
        existing_candidates=[],
    )

    assert [candidate.source_block_ids for candidate in candidates] == [[block.block_id]]
    assert diagnostics[0]["code"] == "candidate_source_out_of_window_dropped"
    assert diagnostics[0]["dropped_block_ids"] == ["doc_ref#v0001#c1#b99999"]


def test_preview_drops_alias_missing_from_evidence_and_keeps_candidate(
    tmp_path: Path,
) -> None:
    snapshot_id = _ingest_document(tmp_path)
    doc = RetrievalReadService(tmp_path).bind_snapshot(snapshot_id).list_documents().documents[0]
    credentials = _MemoryCredentials()
    provider = _AliasCandidateProvider(invalid_alias=True)
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
            draft = profiles.create_profile(ProfileCreateRequest(name="alias validation"))
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
            assert completed.status == "completed"
            alpha = next(
                candidate for candidate in completed.candidates if candidate.name == "Alpha"
            )
            assert alpha.aliases == []
            assert any(
                diagnostic["code"] == "candidate_alias_dropped"
                and diagnostic["candidate_name"] == "Alpha"
                and diagnostic["alias"] == "Alias missing from evidence"
                for diagnostic in completed.diagnostics
            )
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


def test_preview_drops_contribution_without_current_window_source(tmp_path: Path) -> None:
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
            completed = await previews.wait_for_terminal(created.run_id)
            assert completed.status == "completed"
            assert completed.error_code is None
            assert completed.candidates == []
            assert (
                sum(
                    diagnostic["code"] == "candidate_without_current_source_dropped"
                    for diagnostic in completed.diagnostics
                )
                == 2
            )
        finally:
            await previews.stop()

    asyncio.run(run())


def test_preview_can_persist_optional_semantic_candidate_audit(tmp_path: Path) -> None:
    snapshot_id = _ingest_document(tmp_path)
    doc = RetrievalReadService(tmp_path).bind_snapshot(snapshot_id).list_documents().documents[0]
    credentials = _MemoryCredentials()
    provider = _AliasCandidateProvider(
        same_name=True,
        audit_issue_candidate="Alpha",
    )
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
            draft = profiles.create_profile(ProfileCreateRequest(name="semantic audit"))
            saved = profiles.save_draft(
                draft.profile_id,
                ProfileSaveRequest(config=_two_alias_channels()),
            )
            plan = previews.plan(
                PreviewRunPlanRequest(
                    profile_id=saved.profile_id,
                    snapshot_id=snapshot_id,
                    document_ids=[doc.doc_id],
                    semantic_audit_enabled=True,
                )
            )
            assert plan.estimated_extraction_calls == 2
            assert plan.estimated_semantic_audit_calls == 3
            assert plan.estimated_name_scope_audit_calls == 1
            assert plan.estimated_name_meaning_audit_calls == 1
            created = await previews.create(
                PreviewRunRequest(
                    profile_id=saved.profile_id,
                    snapshot_id=snapshot_id,
                    document_ids=[doc.doc_id],
                    execution_mode="force_new",
                    semantic_audit_enabled=True,
                )
            )
            completed = await previews.wait_for_terminal(created.run_id)
            assert completed.status == "completed"
            assert completed.semantic_audit_status == "completed"
            assert completed.name_scope_audit_prompt_version == "candidate-name-scope-audit-v3"
            assert completed.name_meaning_audit_prompt_version == (
                "candidate-name-meaning-audit-v2"
            )
            assert completed.candidates[0].audit_status == "suspicious"
            assert completed.candidates[0].audit_findings[0].code == "alias_not_same_object"
            assert any(usage["operation"] == "semantic_audit" for usage in completed.usage)
            assert any(usage["operation"] == "name_scope_audit" for usage in completed.usage)
            assert any(usage["operation"] == "name_meaning_audit" for usage in completed.usage)
        finally:
            await previews.stop()

    asyncio.run(run())


def test_title_scope_audit_rejects_summary_and_alias_context(tmp_path: Path) -> None:
    snapshot_id = _ingest_document(tmp_path)
    doc = RetrievalReadService(tmp_path).bind_snapshot(snapshot_id).list_documents().documents[0]
    credentials = _MemoryCredentials()
    provider = _AliasCandidateProvider(
        same_name=True,
        ambiguous_name_candidate="Alpha",
    )
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
            draft = profiles.create_profile(ProfileCreateRequest(name="title-only audit"))
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
                    semantic_audit_enabled=True,
                )
            )
            completed = await previews.wait_for_terminal(created.run_id)

            title_request = next(
                request
                for request in provider.requests
                if "候选标题区分度审查器" in request[0].content
            )
            title_payload = json.loads(title_request[-1].content)
            assert title_payload["candidate_titles"] == [
                {
                    "candidate_ref": title_payload["candidate_titles"][0]["candidate_ref"],
                    "name": "Alpha",
                }
            ]
            assert "summaries" not in title_request[-1].content
            assert "Alias Alpha" not in title_request[-1].content

            alpha = completed.candidates[0]
            assert alpha.audit_status == "suspicious"
            assert any(
                finding.code == "name_ambiguous_in_knowledge_base"
                and "Alpha 知识对象" in finding.message
                for finding in alpha.audit_findings
            )
        finally:
            await previews.stop()

    asyncio.run(run())


def test_name_meaning_audit_reports_title_summary_mismatch(tmp_path: Path) -> None:
    snapshot_id = _ingest_document(tmp_path)
    doc = RetrievalReadService(tmp_path).bind_snapshot(snapshot_id).list_documents().documents[0]
    credentials = _MemoryCredentials()
    provider = _AliasCandidateProvider(
        same_name=True,
        misaligned_name_candidate="Alpha",
    )
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
            draft = profiles.create_profile(ProfileCreateRequest(name="name meaning audit"))
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
                    semantic_audit_enabled=True,
                )
            )
            completed = await previews.wait_for_terminal(created.run_id)

            alpha = completed.candidates[0]
            assert alpha.audit_status == "suspicious"
            assert any(
                finding.code == "name_meaning_misaligned" and "Alpha 对象" in finding.message
                for finding in alpha.audit_findings
            )
        finally:
            await previews.stop()

    asyncio.run(run())


def test_semantic_audit_failure_does_not_discard_completed_extraction(
    tmp_path: Path,
) -> None:
    snapshot_id = _ingest_document(tmp_path)
    doc = RetrievalReadService(tmp_path).bind_snapshot(snapshot_id).list_documents().documents[0]
    credentials = _MemoryCredentials()
    provider = _AliasCandidateProvider(same_name=True, fail_audit=True)
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
            draft = profiles.create_profile(ProfileCreateRequest(name="audit failure"))
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
                    semantic_audit_enabled=True,
                )
            )
            completed = await previews.wait_for_terminal(created.run_id)
            assert completed.status == "completed"
            assert completed.semantic_audit_status == "failed"
            assert completed.semantic_audit_error
            assert completed.candidates
        finally:
            await previews.stop()

    asyncio.run(run())


def test_document_anchor_updates_name_without_rewriting_candidate_facts(
    tmp_path: Path,
) -> None:
    snapshot_id = _ingest_document(tmp_path)
    doc = RetrievalReadService(tmp_path).bind_snapshot(snapshot_id).list_documents().documents[0]
    credentials = _MemoryCredentials()
    provider = _AliasCandidateProvider(
        same_name=True,
        document_anchor_name="Agent Alpha",
    )
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
            draft = profiles.create_profile(ProfileCreateRequest(name="document anchor"))
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
            candidate = completed.candidates[0]

            assert completed.status == "completed"
            assert completed.document_anchor_results[doc.doc_id].status == "completed"
            assert completed.document_anchor_prompt_version == "candidate-document-anchor-v3"
            assert completed.document_anchor_policy_version == "document-anchor-32k-64k-v3"
            assert candidate.name == "Agent Alpha"
            assert candidate.provisional_name == "Alpha"
            assert candidate.title_kind == "common_concept"
            assert candidate.title_standalone_clear is False
            assert candidate.missing_anchor_types == ["actor", "scope"]
            assert candidate.name_changed is True
            assert candidate.summaries == [
                "Alpha 是一个独立知识对象。",
                "Alias Alpha 是另一个独立输入。",
            ]
            assert candidate.name_anchor_source_block_ids
            assert candidate.name_anchor_reason == "测试文档上下文提供了名称锚点。"
            assert candidate.source_block_ids == [
                block["block_id"] for block in candidate.evidence_blocks
            ]
            assert "Alpha" in candidate.aliases
            assert any(usage["operation"] == "document_anchor" for usage in completed.usage)
        finally:
            await previews.stop()

    asyncio.run(run())


def test_document_anchor_failure_preserves_provisional_candidates(tmp_path: Path) -> None:
    snapshot_id = _ingest_document(tmp_path)
    doc = RetrievalReadService(tmp_path).bind_snapshot(snapshot_id).list_documents().documents[0]
    credentials = _MemoryCredentials()
    provider = _AliasCandidateProvider(
        same_name=True,
        fail_document_anchor=True,
    )
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
            draft = profiles.create_profile(ProfileCreateRequest(name="anchor failure"))
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

            assert completed.status == "completed"
            assert completed.document_anchor_results[doc.doc_id].status == "failed"
            assert completed.document_anchor_results[doc.doc_id].error
            assert completed.candidates[0].name == "Alpha"
            assert completed.candidates[0].name_anchor_source_block_ids == []
            assert any(
                diagnostic["code"] == "document_anchor_failed"
                for diagnostic in completed.diagnostics
            )
        finally:
            await previews.stop()

    asyncio.run(run())


def test_document_anchor_budget_skips_over_limit_inputs(tmp_path: Path) -> None:
    credentials = _MemoryCredentials()
    provider = _AliasCandidateProvider()
    profiles = ProfileService(
        tmp_path,
        credentials,
        chat_provider_factory=lambda _key: provider,
    )
    previews = ProfilePreviewService(
        tmp_path,
        profiles,
        credentials,
        chat_provider_factory=lambda _key: provider,
    )
    block = _anchor_block("doc_large", "b1", "x" * 200_000)
    candidate = _anchor_candidate_row(
        run_id="pr_budget",
        doc_id="doc_large",
        candidate_id="wc_0000000000000001",
        name="Alpha",
        source_block_ids=[block.block_id],
    )

    async def run() -> None:
        usage, result, diagnostics = await previews._anchor_document_candidates(
            provider=provider,
            profile=_two_alias_channels(),
            doc_id="doc_large",
            doc_title="Large",
            blocks=[block],
            candidates=[candidate],
            semaphore=asyncio.Semaphore(1),
        )
        assert usage is None
        assert result.status == "over_budget"
        assert result.estimated_input_tokens > result.hard_input_tokens
        assert diagnostics[0]["code"] == "document_anchor_over_budget"
        assert provider.requests == []

    asyncio.run(run())


def test_document_anchor_output_budget_has_generous_structured_output_room() -> None:
    assert ProfilePreviewService._document_anchor_output_budget(54) == 32_768
    assert ProfilePreviewService._document_anchor_output_budget(150) == 61_696
    assert ProfilePreviewService._document_anchor_output_budget(200) == 65_536


def test_document_anchor_candidate_limit_skips_without_provider_call(
    tmp_path: Path,
) -> None:
    credentials = _MemoryCredentials()
    provider = _AliasCandidateProvider()
    profiles = ProfileService(
        tmp_path,
        credentials,
        chat_provider_factory=lambda _key: provider,
    )
    previews = ProfilePreviewService(
        tmp_path,
        profiles,
        credentials,
        chat_provider_factory=lambda _key: provider,
    )
    block = _anchor_block("doc_many", "b1", "Alpha")
    candidates = [
        _anchor_candidate_row(
            run_id="pr_many",
            doc_id="doc_many",
            candidate_id=f"wc_{index:016x}",
            name=f"Alpha {index}",
            source_block_ids=[block.block_id],
        )
        for index in range(151)
    ]

    async def run() -> None:
        usage, result, diagnostics = await previews._anchor_document_candidates(
            provider=provider,
            profile=_two_alias_channels(),
            doc_id="doc_many",
            doc_title="Many",
            blocks=[block],
            candidates=candidates,
            semaphore=asyncio.Semaphore(1),
        )
        assert usage is None
        assert result.status == "candidate_limit_exceeded"
        assert diagnostics[0]["code"] == "document_anchor_candidate_limit_exceeded"
        assert provider.requests == []

    asyncio.run(run())


def test_document_anchor_classifies_invalid_output_and_records_usage(
    tmp_path: Path,
) -> None:
    credentials = _MemoryCredentials()
    profiles = ProfileService(
        tmp_path,
        credentials,
        chat_provider_factory=lambda _key: _AliasCandidateProvider(),
    )
    previews = ProfilePreviewService(
        tmp_path,
        profiles,
        credentials,
        chat_provider_factory=lambda _key: _AliasCandidateProvider(),
    )
    block = _anchor_block("doc_invalid", "b1", "Alpha 是一个独立对象。")

    async def run() -> None:
        for mode, expected_status, expected_code in [
            ("truncated", "truncated_output", "document_anchor_truncated_output"),
            ("invalid_json", "invalid_json", "document_anchor_invalid_json"),
            ("invalid_schema", "invalid_schema", "document_anchor_invalid_schema"),
        ]:
            provider = _AliasCandidateProvider(document_anchor_mode=mode)
            candidate = _anchor_candidate_row(
                run_id=f"pr_{mode}",
                doc_id=f"doc_{mode}",
                candidate_id=f"wc_{mode:0>16}"[:19],
                name="Alpha",
                source_block_ids=[block.block_id],
            )
            usage, result, diagnostics = await previews._anchor_document_candidates(
                provider=provider,
                profile=_two_alias_channels(),
                doc_id=f"doc_{mode}",
                doc_title="Invalid output",
                blocks=[block],
                candidates=[candidate],
                semaphore=asyncio.Semaphore(1),
            )
            assert usage is not None
            assert result.status == expected_status
            assert result.actual_input_tokens == 30
            assert result.actual_output_tokens == 12
            assert result.returned_chars > 0
            assert usage["output_tokens"] == 12
            assert diagnostics[0]["code"] == expected_code
            assert candidate.name == "Alpha"

    asyncio.run(run())


def test_document_anchor_drops_alias_outside_candidate_evidence(tmp_path: Path) -> None:
    credentials = _MemoryCredentials()
    provider = _AliasCandidateProvider(invalid_document_anchor_alias=True)
    profiles = ProfileService(
        tmp_path,
        credentials,
        chat_provider_factory=lambda _key: provider,
    )
    previews = ProfilePreviewService(
        tmp_path,
        profiles,
        credentials,
        chat_provider_factory=lambda _key: provider,
    )
    block = _anchor_block("doc_alias", "b1", "Alpha 是一个独立对象。")
    candidate = _anchor_candidate_row(
        run_id="pr_alias",
        doc_id="doc_alias",
        candidate_id="wc_0000000000000001",
        name="Alpha",
        source_block_ids=[block.block_id],
    )

    async def run() -> None:
        usage, result, diagnostics = await previews._anchor_document_candidates(
            provider=provider,
            profile=_two_alias_channels(),
            doc_id="doc_alias",
            doc_title="Alias",
            blocks=[block],
            candidates=[candidate],
            semaphore=asyncio.Semaphore(1),
        )
        assert usage is not None
        assert result.status == "completed"
        assert json.loads(candidate.aliases_json) == []
        assert any(
            diagnostic["code"] == "document_anchor_alias_dropped"
            and diagnostic["alias"] == "ghost anchor alias"
            for diagnostic in diagnostics
        )

    asyncio.run(run())


def test_document_anchor_rejects_source_outside_document(tmp_path: Path) -> None:
    credentials = _MemoryCredentials()
    provider = _AliasCandidateProvider(invalid_document_anchor_source=True)
    profiles = ProfileService(
        tmp_path,
        credentials,
        chat_provider_factory=lambda _key: provider,
    )
    previews = ProfilePreviewService(
        tmp_path,
        profiles,
        credentials,
        chat_provider_factory=lambda _key: provider,
    )
    block = _anchor_block("doc_source", "b1", "Alpha 是一个独立对象。")
    candidate = _anchor_candidate_row(
        run_id="pr_source",
        doc_id="doc_source",
        candidate_id="wc_0000000000000001",
        name="Alpha",
        source_block_ids=[block.block_id],
    )

    async def run() -> None:
        usage, result, diagnostics = await previews._anchor_document_candidates(
            provider=provider,
            profile=_two_alias_channels(),
            doc_id="doc_source",
            doc_title="Source",
            blocks=[block],
            candidates=[candidate],
            semaphore=asyncio.Semaphore(1),
        )
        assert usage is not None
        assert result.status == "invalid_schema"
        assert result.actual_output_tokens == 12
        assert diagnostics[0]["code"] == "document_anchor_source_invalid"
        assert candidate.name == "Alpha"

    asyncio.run(run())


def _anchor_block(doc_id: str, block_id: str, text: str) -> RetrievalBlock:
    return RetrievalBlock(
        block_id=block_id,
        doc_id=doc_id,
        doc_title="Anchor document",
        source_label=f"{doc_id}.html",
        section_path=["Anchor"],
        section_context=RetrievalSectionContext(
            section_depth=1,
            has_child_sections=False,
            document_section_count=1,
        ),
        text=text,
    )


def _anchor_candidate_row(
    *,
    run_id: str,
    doc_id: str,
    candidate_id: str,
    name: str,
    source_block_ids: list[str],
) -> PreviewCandidateRow:
    return PreviewCandidateRow(
        candidate_id=candidate_id,
        run_id=run_id,
        doc_id=doc_id,
        doc_version=1,
        ordinal=0,
        name=name,
        normalized_name=name.casefold(),
        aliases_json="[]",
        summaries_json=json.dumps([f"{name} 的独立说明。"], ensure_ascii=False),
        selection_reasons_json="[]",
        name_anchor_source_block_ids_json="[]",
        name_anchor_reason=None,
        source_block_ids_json=json.dumps(source_block_ids),
        evidence_blocks_json="[]",
        channel_ids_json="[]",
        content_hash="0" * 64,
    )
