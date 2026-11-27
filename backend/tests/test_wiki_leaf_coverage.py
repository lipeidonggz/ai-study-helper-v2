from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from app.agent.token_counter import ConservativeObservationTokenCounter
from app.agent.tool_adapter import ToolExecutionContext
from app.providers.chat import (
    ChatCompletionResult,
    ChatMessage,
    ChatProviderError,
    ChatProviderProfile,
    ChatStreamChunk,
)
from app.retrieval.models import (
    GetBlocksResult,
    RetrievalBlock,
    RetrievalSectionContext,
)
from app.retrieval.service import RetrievalSnapshotReader
from app.tracing.models import TraceArtifactRecord
from app.wiki.leaf_coverage import (
    COVERAGE_PROMPT_VERSION,
    CURRENT_LEAF_COUNT,
    CURRENT_ROOT_COUNT,
    LEAF_SELECTION_SYSTEM_PROMPT,
    CoverageBudgetExceededError,
    CoverageCancelledError,
    CoverageEvidenceService,
    CoverageLedger,
    CoverageNotReadyError,
    FrozenForest,
    load_active_coverage_ledger,
    load_frozen_forest,
)


class _RoutingChatProvider:
    """按 root ID 返回固定选择结果，便于验证分支隔离和 usage 上卷。"""

    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses
        self._profile = ChatProviderProfile(
            provider="fake",
            model_id="fake-coverage-selector",
            base_url="fake://chat",
            temperature=0.0,
            max_output_tokens=1_024,
            thinking_enabled=False,
        )
        self.calls = 0

    @property
    def profile(self) -> ChatProviderProfile:
        return self._profile

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> ChatCompletionResult:
        del max_output_tokens
        self.calls += 1
        system_prompt = messages[0].content
        content = next(
            (
                response
                for root_id, response in self._responses.items()
                if f'"node_id":"{root_id}"' in system_prompt
            ),
            '{"leaf_ids":[]}',
        )
        return ChatCompletionResult(
            content=content,
            request_id=f"coverage-{self.calls}",
            actual_model_id=self.profile.model_id,
            finish_reason="stop",
            input_tokens=100,
            output_tokens=10,
            cache_hit_tokens=30,
            cache_miss_tokens=70,
        )

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[ChatStreamChunk]:
        del messages, max_output_tokens
        raise NotImplementedError


class _PartialFailureProvider(_RoutingChatProvider):
    """让指定分支失败，其余分支正常返回。"""

    def __init__(self, responses: dict[str, str], failed_root_id: str) -> None:
        super().__init__(responses)
        self._failed_root_id = failed_root_id

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> ChatCompletionResult:
        if f'"node_id":"{self._failed_root_id}"' in messages[0].content:
            self.calls += 1
            raise ChatProviderError(
                code="chat_rate_limited",
                message="rate limited",
                retryable=False,
            )
        return await super().complete(messages, max_output_tokens=max_output_tokens)


class _TruncationProvider(_RoutingChatProvider):
    """让指定分支返回供应商截断结果。"""

    def __init__(self, responses: dict[str, str], truncated_root_id: str) -> None:
        super().__init__(responses)
        self._truncated_root_id = truncated_root_id

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> ChatCompletionResult:
        result = await super().complete(messages, max_output_tokens=max_output_tokens)
        if f'"node_id":"{self._truncated_root_id}"' in messages[0].content:
            return replace(result, finish_reason="length")
        return result


class _ExcessUsageProvider(_RoutingChatProvider):
    """模拟供应商实际输入 usage 高于保守预估值。"""

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> ChatCompletionResult:
        result = await super().complete(messages, max_output_tokens=max_output_tokens)
        if '"node_id":"root-a"' in messages[0].content:
            return replace(result, input_tokens=5_000)
        return result


class _CancelDuringSearchProvider(_RoutingChatProvider):
    """让一个分支先完成、另一个分支保持未完成以验证取消上卷。"""

    def __init__(self, responses: dict[str, str]) -> None:
        super().__init__(responses)
        self.first_completed = asyncio.Event()
        self.blocked_started = asyncio.Event()

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> ChatCompletionResult:
        if '"node_id":"root-a"' in messages[0].content:
            result = await super().complete(messages, max_output_tokens=max_output_tokens)
            self.first_completed.set()
            return result
        if '"node_id":"root-b"' in messages[0].content:
            self.blocked_started.set()
            await asyncio.sleep(60)
        return await super().complete(messages, max_output_tokens=max_output_tokens)


class _RecordingReader:
    """只实现覆盖服务使用的完整 block 读取接口。"""

    def __init__(self, blocks: dict[str, RetrievalBlock]) -> None:
        self._blocks = blocks
        self.requested_ids: list[str] = []

    def get_blocks(self, block_ids: list[str]) -> GetBlocksResult:
        self.requested_ids = list(block_ids)
        return GetBlocksResult(
            blocks=[self._blocks[block_id] for block_id in block_ids],
            total=len(block_ids),
        )


class _RecordingSpan:
    def __init__(self, span_id: str, name: str, kind: str, parent_span_id: str | None) -> None:
        self.span_id = span_id
        self.name = name
        self.kind = kind
        self.parent_span_id = parent_span_id
        self.attributes: dict[str, Any] = {}
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.end_status: str | None = None


class _RecordingTraceSession:
    mode = "full"

    def __init__(self) -> None:
        self.spans: list[_RecordingSpan] = []

    def start_span(
        self,
        name: str,
        kind: str,
        *,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> _RecordingSpan:
        span = _RecordingSpan(
            span_id=f"span-{len(self.spans) + 1}",
            name=name,
            kind=kind,
            parent_span_id=parent_span_id,
        )
        span.attributes.update(attributes or {})
        self.spans.append(span)
        return span

    def end_span(
        self,
        handle: _RecordingSpan,
        *,
        status: str,
        error_code: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        handle.end_status = status
        handle.attributes.update(attributes or {})
        if error_code is not None:
            handle.attributes["error_code"] = error_code

    def add_span_event(
        self,
        handle: _RecordingSpan,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        handle.events.append((name, attributes or {}))

    def record_artifact(
        self,
        *,
        kind: str,
        mime_type: str,
        content: str | bytes | dict[str, Any] | list[Any],
    ) -> TraceArtifactRecord:
        digest = hashlib.sha256(repr(content).encode()).hexdigest()
        return TraceArtifactRecord(
            artifact_id=digest,
            kind=kind,
            mime_type=mime_type,
            size_bytes=1,
            content_hash=digest,
            state="stored",
            relative_path=f"artifacts/{digest}.json",
        )


def _small_forest() -> FrozenForest:
    root_a = {
        "node_id": "root-a",
        "children": [
            {"node_id": "leaf-a1", "children": []},
            {"node_id": "leaf-a2", "children": []},
        ],
    }
    root_b = {
        "node_id": "root-b",
        "children": [{"node_id": "leaf-b1", "children": []}],
    }
    return FrozenForest(
        revision_id="test-revision",
        sha256="test-sha256",
        roots=(root_a, root_b),
        leaves_by_root={
            "root-a": frozenset({"leaf-a1", "leaf-a2"}),
            "root-b": frozenset({"leaf-b1"}),
        },
        all_leaf_ids=frozenset({"leaf-a1", "leaf-a2", "leaf-b1"}),
    )


def _small_ledger() -> CoverageLedger:
    return CoverageLedger(
        attachment_run_id="att_test",
        snapshot_id="snapshot-test",
        framework_revision_id="test-revision",
        framework_sha256="test-sha256",
        block_ids_by_leaf={
            "leaf-a1": ("doc#b1",),
            "leaf-a2": ("doc#b1", "doc#b3"),
            "leaf-b1": ("doc#b2",),
        },
        support_count=4,
    )


def _block(block_id: str, text: str) -> RetrievalBlock:
    return RetrievalBlock(
        block_id=block_id,
        doc_id="doc",
        doc_title="Document",
        source_label="fixture.html",
        section_path=["Section"],
        section_context=RetrievalSectionContext(
            section_depth=1,
            has_child_sections=False,
            document_section_count=1,
        ),
        text=text,
    )


def _service(
    provider: _RoutingChatProvider,
    *,
    trace_session: _RecordingTraceSession | None = None,
) -> tuple[CoverageEvidenceService, _RecordingReader, _RecordingTraceSession]:
    session = trace_session or _RecordingTraceSession()
    reader = _RecordingReader(
        {
            "doc#b1": _block("doc#b1", "first full block"),
            "doc#b2": _block("doc#b2", "second full block"),
            "doc#b3": _block("doc#b3", "third full block"),
        }
    )
    service = CoverageEvidenceService(
        reader=cast(RetrievalSnapshotReader, reader),
        provider=provider,
        token_counter=ConservativeObservationTokenCounter(),
        forest=_small_forest(),
        ledger=_small_ledger(),
    )
    return service, reader, session


def _context(
    session: _RecordingTraceSession,
    *,
    remaining_tokens: int = 100_000,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        remaining_request_tokens=remaining_tokens,
        max_context_tokens=20_000,
        max_output_tokens=1_024,
        trace_session=session,
    )


def _write_active_ledger_fixture(data_root: Path, leaf_id: str) -> None:
    snapshot_id = "snapshot-test"
    attachment_run_id = "att_20261127T000000000000Z"
    attachment_root = data_root / "wiki-leaf-attachments" / snapshot_id
    run_dir = attachment_root / attachment_run_id
    run_dir.mkdir(parents=True)
    collections_dir = data_root / "collections" / snapshot_id
    collections_dir.mkdir(parents=True)
    snapshot_manifest = b'{"snapshot_id":"snapshot-test"}\n'
    (collections_dir / "manifest.json").write_bytes(snapshot_manifest)
    support_line = json.dumps(
        {
            "snapshot_id": snapshot_id,
            "framework_revision_id": "test-revision",
            "leaf_id": leaf_id,
            "block_id": "doc#b1",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    supports_bytes = f"{support_line}\n".encode()
    (run_dir / "supports.jsonl").write_bytes(supports_bytes)
    manifest = {
        "status": "sealed_complete",
        "snapshot_id": snapshot_id,
        "framework_revision_id": "test-revision",
        "framework_sha256": "test-sha256",
        "block_count": 1,
        "root_count": 2,
        "leaf_count": 3,
        "snapshot_manifest_sha256": hashlib.sha256(snapshot_manifest).hexdigest(),
        "ledger_files": {
            "supports.jsonl": hashlib.sha256(supports_bytes).hexdigest(),
        },
    }
    manifest_bytes = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    (run_dir / "manifest.json").write_bytes(manifest_bytes)
    pointer = {
        "snapshot_id": snapshot_id,
        "attachment_run_id": attachment_run_id,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    (attachment_root / "active-attachment.json").write_text(
        json.dumps(pointer, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def test_frozen_forest_resource_matches_current_profile() -> None:
    forest = load_frozen_forest()

    assert len(forest.roots) == CURRENT_ROOT_COUNT
    assert len(forest.all_leaf_ids) == CURRENT_LEAF_COUNT
    assert forest.revision_id == "kf_c1b79b163bfcb2a93844"
    assert "K01.01" in forest.leaves_by_root["K01"]


def test_leaf_selection_prompt_is_compatible_with_json_object_mode() -> None:
    assert "json" in LEAF_SELECTION_SYSTEM_PROMPT
    assert "顶层必须且只能包含 leaf_ids 字段" in LEAF_SELECTION_SYSTEM_PROMPT
    assert "不得输出 selected_nodes" in LEAF_SELECTION_SYSTEM_PROMPT


def test_active_coverage_ledger_builds_leaf_to_block_mapping(tmp_path: Path) -> None:
    forest = _small_forest()
    _write_active_ledger_fixture(tmp_path, "leaf-a1")

    ledger = load_active_coverage_ledger(
        tmp_path,
        "snapshot-test",
        forest,
        expected_block_count=1,
    )

    assert ledger.attachment_run_id == "att_20261127T000000000000Z"
    assert ledger.block_ids_by_leaf["leaf-a1"] == ("doc#b1",)


def test_active_coverage_ledger_rejects_modified_support_file(tmp_path: Path) -> None:
    forest = _small_forest()
    _write_active_ledger_fixture(tmp_path, "leaf-a1")
    support_path = (
        tmp_path
        / "wiki-leaf-attachments"
        / "snapshot-test"
        / "att_20261127T000000000000Z"
        / "supports.jsonl"
    )
    support_path.write_text("modified\n", encoding="utf-8")

    with pytest.raises(CoverageNotReadyError, match="hash mismatch"):
        load_active_coverage_ledger(
            tmp_path,
            "snapshot-test",
            forest,
            expected_block_count=1,
        )


def test_coverage_search_merges_leaf_and_block_duplicates_with_usage_and_trace() -> None:
    provider = _RoutingChatProvider(
        {
            "root-a": '{"leaf_ids":["leaf-a1","leaf-a2","leaf-a1"]}',
            "root-b": '{"leaf_ids":["leaf-b1"]}',
        }
    )
    service, reader, session = _service(provider)

    result = asyncio.run(service.search("compare all aspects", execution_context=_context(session)))

    assert result.payload is not None
    assert result.payload["coverage_status"] == "complete"
    assert [block["block_id"] for block in result.payload["blocks"]] == [
        "doc#b1",
        "doc#b3",
        "doc#b2",
    ]
    assert reader.requested_ids == ["doc#b1", "doc#b3", "doc#b2"]
    assert result.model_calls == 2
    assert result.input_tokens == 200
    assert result.output_tokens == 20
    assert result.cache_hit_tokens == 60
    assert result.cache_miss_tokens == 140
    assert result.trace_attributes["app.coverage.branch_count"] == 2
    assert result.trace_attributes["app.coverage.selected_leaf_count"] == 3
    assert result.trace_attributes["app.coverage.selected_block_count"] == 3
    assert [span.name for span in session.spans] == [
        "coverage.branch",
        "coverage.branch",
        "llm.call",
        "llm.call",
        "coverage.materialize",
    ]


def test_coverage_search_marks_cross_subtree_leaf_as_partial() -> None:
    provider = _RoutingChatProvider(
        {
            "root-a": '{"leaf_ids":["leaf-b1"]}',
            "root-b": '{"leaf_ids":["leaf-b1"]}',
        }
    )
    service, _reader, session = _service(provider)

    result = asyncio.run(service.search("question", execution_context=_context(session)))

    assert result.payload is not None
    assert result.payload["coverage_status"] == "partial"
    assert [block["block_id"] for block in result.payload["blocks"]] == ["doc#b2"]
    assert result.diagnostics["failed_branch_count"] == 1
    assert result.diagnostics["failed_branches"][0]["error_code"] == (
        "coverage_selector_leaf_outside_subtree"
    )


def test_coverage_search_marks_invalid_json_as_partial() -> None:
    provider = _RoutingChatProvider(
        {
            "root-a": '{"leaf_ids":',
            "root-b": '{"leaf_ids":["leaf-b1"]}',
        }
    )
    service, _reader, session = _service(provider)

    result = asyncio.run(service.search("question", execution_context=_context(session)))

    assert result.payload is not None
    assert result.payload["coverage_status"] == "partial"
    assert [block["block_id"] for block in result.payload["blocks"]] == ["doc#b2"]
    assert result.diagnostics["failed_branches"][0]["error_code"] == (
        "coverage_selector_invalid_output"
    )


def test_coverage_search_marks_truncated_branch_as_partial() -> None:
    provider = _TruncationProvider(
        {
            "root-a": '{"leaf_ids":["leaf-a1"]}',
            "root-b": '{"leaf_ids":["leaf-b1"]}',
        },
        truncated_root_id="root-a",
    )
    service, _reader, session = _service(provider)

    result = asyncio.run(service.search("question", execution_context=_context(session)))

    assert result.payload is not None
    assert result.payload["coverage_status"] == "partial"
    assert [block["block_id"] for block in result.payload["blocks"]] == ["doc#b2"]
    assert result.diagnostics["failed_branches"][0]["error_code"] == ("coverage_selector_truncated")


def test_coverage_search_records_actual_usage_above_reservation() -> None:
    provider = _ExcessUsageProvider(
        {
            "root-a": '{"leaf_ids":["leaf-a1"]}',
            "root-b": '{"leaf_ids":["leaf-b1"]}',
        }
    )
    service, _reader, session = _service(provider)

    result = asyncio.run(service.search("question", execution_context=_context(session)))

    assert result.payload is not None
    assert result.payload["coverage_status"] == "complete"
    assert result.input_tokens == 5_100
    assert result.diagnostics["budget_overshoot_tokens"] > 0
    assert result.trace_attributes["app.coverage.request_budget_overshoot"] > 0


def test_coverage_search_finishes_with_an_active_uncanceled_token() -> None:
    provider = _RoutingChatProvider(
        {
            "root-a": '{"leaf_ids":["leaf-a1"]}',
            "root-b": '{"leaf_ids":["leaf-b1"]}',
        }
    )
    service, _reader, session = _service(provider)
    cancellation = asyncio.Event()

    class CancellationToken:
        @property
        def is_cancelled(self) -> bool:
            return cancellation.is_set()

        async def wait(self) -> None:
            await cancellation.wait()

    context = ToolExecutionContext(
        remaining_request_tokens=100_000,
        max_context_tokens=20_000,
        max_output_tokens=1_024,
        cancellation=CancellationToken(),
        trace_session=session,
    )

    result = asyncio.run(
        asyncio.wait_for(
            service.search("question", execution_context=context),
            timeout=1,
        )
    )

    assert result.payload is not None
    assert result.payload["coverage_status"] == "complete"


def test_coverage_search_preserves_usage_when_canceled_mid_flight() -> None:
    provider = _CancelDuringSearchProvider(
        {
            "root-a": '{"leaf_ids":["leaf-a1"]}',
            "root-b": '{"leaf_ids":["leaf-b1"]}',
        }
    )
    service, _reader, session = _service(provider)
    cancellation = asyncio.Event()

    class CancellationToken:
        @property
        def is_cancelled(self) -> bool:
            return cancellation.is_set()

        async def wait(self) -> None:
            await cancellation.wait()

    context = ToolExecutionContext(
        remaining_request_tokens=100_000,
        max_context_tokens=20_000,
        max_output_tokens=1_024,
        cancellation=CancellationToken(),
        trace_session=session,
    )

    async def run_until_canceled() -> CoverageCancelledError:
        task = asyncio.create_task(service.search("question", execution_context=context))
        await asyncio.wait_for(provider.blocked_started.wait(), timeout=1)
        await asyncio.wait_for(provider.first_completed.wait(), timeout=1)
        cancellation.set()
        with pytest.raises(CoverageCancelledError) as captured:
            await asyncio.wait_for(task, timeout=1)
        return captured.value

    error = asyncio.run(run_until_canceled())

    assert error.model_calls == 2
    assert error.input_tokens == 100
    assert error.output_tokens == 10
    assert error.cache_hit_tokens == 30
    assert error.cache_miss_tokens == 70
    assert error.trace_attributes["app.coverage.canceled_branch_count"] == 1


def test_coverage_search_preserves_successful_blocks_when_one_branch_fails() -> None:
    provider = _PartialFailureProvider(
        {"root-b": '{"leaf_ids":["leaf-b1"]}'},
        failed_root_id="root-a",
    )
    service, _reader, session = _service(provider)

    result = asyncio.run(service.search("question", execution_context=_context(session)))

    assert result.payload is not None
    assert result.payload["coverage_status"] == "partial"
    assert [block["block_id"] for block in result.payload["blocks"]] == ["doc#b2"]
    assert result.diagnostics["successful_branch_count"] == 1
    assert result.diagnostics["failed_branch_count"] == 1


def test_coverage_search_returns_explicit_failure_when_all_branches_fail() -> None:
    provider = _PartialFailureProvider({}, failed_root_id="root-a")
    provider._failed_root_id = "root-a"
    service, reader, session = _service(provider)
    provider._responses = {}

    original_complete = provider.complete

    async def fail_all(
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> ChatCompletionResult:
        del messages, max_output_tokens
        provider.calls += 1
        raise ChatProviderError(
            code="chat_unavailable",
            message="unavailable",
            retryable=False,
        )

    provider.complete = fail_all  # type: ignore[method-assign]
    result = asyncio.run(service.search("question", execution_context=_context(session)))
    provider.complete = original_complete  # type: ignore[method-assign]

    assert result.payload is None
    assert result.error == (
        "coverage_all_branches_failed",
        "all coverage selector branches failed",
        False,
    )
    assert result.model_calls == 2
    assert reader.requested_ids == []


def test_coverage_search_rejects_insufficient_budget_before_provider_call() -> None:
    provider = _RoutingChatProvider({})
    service, _reader, session = _service(provider)

    with pytest.raises(CoverageBudgetExceededError):
        asyncio.run(
            service.search(
                "question",
                execution_context=_context(session, remaining_tokens=1),
            )
        )

    assert provider.calls == 0


def test_coverage_search_stops_before_calls_when_already_cancelled() -> None:
    provider = _RoutingChatProvider({})
    service, _reader, session = _service(provider)

    class CancelledToken:
        is_cancelled = True

        async def wait(self) -> None:
            return None

    context = ToolExecutionContext(
        remaining_request_tokens=100_000,
        max_context_tokens=20_000,
        max_output_tokens=1_024,
        cancellation=CancelledToken(),
        trace_session=session,
    )

    with pytest.raises(CoverageCancelledError):
        asyncio.run(service.search("question", execution_context=context))

    assert provider.calls == 0
    assert COVERAGE_PROMPT_VERSION == "coverage-leaf-selection-v2"
