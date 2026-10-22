from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.tracing.catalog import TraceCatalog
from app.tracing.models import TraceManifestRecord
from app.tracing.store import TraceStore

TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")


def _session(store: TraceStore, *, mode: str = "full"):
    return store.new_session(
        mode=mode,  # type: ignore[arg-type]
        question="trace 测试问题",
        app_version="0.1.0-test",
        versions={"prompt_bundle_version": "chat-v2"},
        budget={"max_request_tokens": 400_000},
    )


def test_full_trace_persists_manifest_spans_artifacts_and_index(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    session = _session(store)
    session.set_snapshot(
        snapshot_id="s_test",
        snapshot_status="ready",
        completeness="complete",
    )
    turn = session.start_span("agent.turn", "INTERNAL")
    llm = session.start_span("llm.call", "LLM", parent_span_id=turn.span_id)
    session.add_span_event(
        turn,
        "budget.check",
        {"app.decision": "admit", "app.limit_kind": "context"},
    )
    session.end_span(llm, status="ok")
    session.end_span(turn, status="ok")
    session.finish(
        status="succeeded",
        termination_reason="finish",
        answer_completeness="complete",
        agent_turns=1,
        tool_calls=0,
        model_calls=1,
        retry_count=0,
        input_tokens=20,
        output_tokens=8,
        answer="trace answer",
        limitations=[],
    )

    assert store.wait_for_idle(3)
    catalog = TraceCatalog(tmp_path / "traces")
    detail = catalog.load(session.trace_id)

    assert TRACE_ID_PATTERN.fullmatch(detail.manifest.trace_id)
    assert all(SPAN_ID_PATTERN.fullmatch(span.span_id) for span in detail.spans)
    assert {span.name for span in detail.spans} == {"agent.run", "agent.turn", "llm.call"}
    llm_span = next(span for span in detail.spans if span.name == "llm.call")
    assert llm_span.parent_span_id == next(
        span.span_id for span in detail.spans if span.name == "agent.turn"
    )
    assert detail.manifest.mode == "full"
    assert detail.manifest.question_preview == "trace 测试问题"
    assert detail.manifest.replay_scope == "retrieval"
    assert any(artifact.kind == "answer" for artifact in detail.manifest.artifacts)
    answer_artifact = next(
        artifact for artifact in detail.manifest.artifacts if artifact.kind == "answer"
    )
    artifact_path = catalog.artifact_path(session.trace_id, answer_artifact.artifact_id)
    assert artifact_path.read_text("utf-8") == "trace answer"
    assert catalog.list_recent()[0].trace_id == session.trace_id
    (tmp_path / "traces" / "index.sqlite").unlink()
    assert catalog.list_recent()[0].trace_id == session.trace_id
    store.close()


def test_metadata_trace_omits_content_but_keeps_hashes(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    session = _session(store, mode="metadata")
    session.finish(
        status="failed",
        termination_reason="chat_timeout",
        answer_completeness="partial",
        agent_turns=1,
        tool_calls=0,
        model_calls=1,
        retry_count=1,
        input_tokens=10,
        output_tokens=0,
        answer=None,
        limitations=[],
    )

    assert store.wait_for_idle(3)
    detail = TraceCatalog(tmp_path / "traces").load(session.trace_id)

    assert detail.manifest.question_preview == "trace 测试问题"
    assert detail.manifest.question_artifact is not None
    assert detail.manifest.question_artifact.state == "omitted"
    assert detail.manifest.question_artifact.content_hash
    assert detail.manifest.replay_scope == "metadata"
    store.close()


def test_cleanup_honors_mode_age_and_disk_limit(tmp_path: Path) -> None:
    trace_root = tmp_path / "traces"
    catalog = TraceCatalog(trace_root)
    now = datetime.now(UTC)
    for trace_id, mode, age_days, size in (
        ("a" * 32, "metadata", 31, 10),
        ("b" * 32, "full", 8, 10),
        ("c" * 32, "full", 1, 10),
        ("d" * 32, "full", 2, 10),
    ):
        trace_dir = trace_root / now.date().isoformat() / trace_id
        trace_dir.mkdir(parents=True)
        (trace_dir / "payload.bin").write_bytes(b"x" * size)
        manifest = TraceManifestRecord(
            trace_id=trace_id,
            mode=mode,  # type: ignore[arg-type]
            app_version="0.1.0",
            started_at=(now - timedelta(days=age_days)).isoformat(),
            ended_at=(now - timedelta(days=age_days)).isoformat(),
            duration_ms=1,
            status="succeeded",
            termination_reason="finish",
            answer_completeness="complete",
            agent_turns=1,
            tool_calls=0,
            model_calls=1,
            retry_count=0,
            input_tokens=1,
            output_tokens=1,
            replay_scope="retrieval" if mode == "full" else "metadata",
        )
        (trace_dir / "manifest.json").write_text(
            manifest.model_dump_json(),
            encoding="utf-8",
        )

    remaining_paths = [
        trace_root / now.date().isoformat() / (trace_id * 32) for trace_id in ("c", "d")
    ]
    disk_limit = (
        sum(
            item.stat().st_size
            for path in remaining_paths
            for item in path.rglob("*")
            if item.is_file()
        )
        - 1
    )
    result = catalog.cleanup(now=now, disk_limit_bytes=disk_limit)

    assert result["removed_by_age"] == 2
    assert result["removed_by_disk_limit"] == 1
    remaining = [path.parent.name for path in trace_root.glob("*/*/manifest.json")]
    assert remaining == ["c" * 32]


def test_full_queue_marks_trace_partial_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TraceStore(tmp_path, queue_capacity=1)
    monkeypatch.setattr(store, "_ensure_worker", lambda: None)
    session = _session(store)
    artifact = session.record_artifact(
        kind="answer",
        mime_type="text/plain",
        content="should be dropped",
    )
    turn = session.start_span("agent.turn", "INTERNAL")
    session.end_span(turn, status="ok")

    health = store.health(session.trace_id)

    assert artifact.state == "missing"
    assert health.status == "partial"
    assert health.dropped_events >= 2
