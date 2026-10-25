from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from app.tracing.models import (
    ReplayScope,
    TraceArtifactRecord,
    TraceManifestRecord,
    TraceMode,
    TraceSpanEvent,
    TraceSpanRecord,
    TraceStatus,
)

logger = logging.getLogger(__name__)

TRACE_QUEUE_CAPACITY = 4_096
TRACE_WRITE_BATCH_SIZE = 100
TRACE_WRITE_INTERVAL_SECONDS = 0.25
TRACE_SHUTDOWN_FLUSH_SECONDS = 2.0
TRACE_QUESTION_PREVIEW_CHARS = 80


def utc_now_iso() -> str:
    """返回带 UTC offset、适合 trace 持久化的 ISO 时间。"""

    return datetime.now(UTC).isoformat()


def new_trace_id() -> str:
    """生成 32 位小写十六进制 W3C trace ID。"""

    return secrets.token_hex(16)


def new_span_id() -> str:
    """生成 16 位小写十六进制 span ID。"""

    return secrets.token_hex(8)


@dataclass(slots=True)
class TraceSpanHandle:
    """尚未结束的 span，由 Runtime 或 provider 边界持有。"""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: str
    start_time_unix_nano: int
    started_at: str
    started_monotonic: float
    events: list[TraceSpanEvent] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        """向 span 追加一个有序事件。"""

        self.events.append(
            TraceSpanEvent(
                name=name,
                time=utc_now_iso(),
                attributes=attributes or {},
            )
        )


@dataclass(frozen=True, slots=True)
class _ArtifactWrite:
    trace_id: str
    trace_dir: Path
    artifact: TraceArtifactRecord
    content: bytes


@dataclass(frozen=True, slots=True)
class _SpanWrite:
    trace_id: str
    trace_dir: Path
    span: TraceSpanRecord


@dataclass(frozen=True, slots=True)
class _ManifestWrite:
    trace_id: str
    trace_dir: Path
    manifest: TraceManifestRecord


@dataclass(slots=True)
class _TraceHealth:
    dropped_events: int = 0
    failure: str | None = None

    @property
    def status(self) -> TraceStatus:
        """根据丢弃和写入失败情况返回最终 trace 完整度。"""

        if self.failure and self.dropped_events == 0:
            return "dropped"
        if self.failure or self.dropped_events > 0:
            return "partial"
        return "complete"


class TraceStore:
    """使用单一后台线程非阻塞写入本地 trace 文件与派生索引。"""

    def __init__(
        self,
        data_root: Path,
        *,
        queue_capacity: int = TRACE_QUEUE_CAPACITY,
    ) -> None:
        self._trace_root = data_root.resolve() / "traces"
        self._queue: queue.Queue[_ArtifactWrite | _SpanWrite | _ManifestWrite] = queue.Queue(
            maxsize=queue_capacity
        )
        self._capacity = queue_capacity
        self._health: dict[str, _TraceHealth] = {}
        self._health_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._closed = False

    def new_session(
        self,
        *,
        mode: TraceMode,
        question: str,
        app_version: str,
        versions: dict[str, Any],
        budget: dict[str, Any],
    ) -> TraceSession:
        """创建一次问答运行的 trace 会话；`off` 模式返回不落盘的会话。"""

        root_span = None
        session = TraceSession(
            store=self,
            mode=cast(TraceMode, mode),
            question=question,
            app_version=app_version,
            versions=versions,
            budget=budget,
        )
        if mode != "off":
            root_span = session.start_span(
                "agent.run",
                "INTERNAL",
                attributes={"app.operation": "agent_run"},
            )
        session._root_span = root_span
        return session

    def enqueue_span(self, trace_dir: Path, span: TraceSpanRecord) -> bool:
        """尝试提交一个 span；队列满时只标记降级，不阻塞调用方。"""

        return self._enqueue(
            _SpanWrite(trace_id=span.trace_id, trace_dir=trace_dir, span=span),
            trace_id=span.trace_id,
            reserve_manifest_slot=True,
        )

    def enqueue_artifact(
        self,
        trace_dir: Path,
        trace_id: str,
        artifact: TraceArtifactRecord,
        content: bytes,
    ) -> bool:
        """尝试提交一个 artifact；队列满时把引用标成 missing。"""

        accepted = self._enqueue(
            _ArtifactWrite(
                trace_id=trace_id,
                trace_dir=trace_dir,
                artifact=artifact,
                content=content,
            ),
            trace_id=trace_id,
            reserve_manifest_slot=True,
        )
        if not accepted:
            artifact.state = "missing"
        return accepted

    def enqueue_manifest(self, trace_dir: Path, manifest: TraceManifestRecord) -> bool:
        """提交 trace manifest；必须尽量为最终摘要保留队列空间。"""

        return self._enqueue(
            _ManifestWrite(
                trace_id=manifest.trace_id,
                trace_dir=trace_dir,
                manifest=manifest,
            ),
            trace_id=manifest.trace_id,
            reserve_manifest_slot=False,
        )

    def mark_failure(self, trace_id: str, message: str) -> None:
        """记录 writer 失败，使最终 manifest 如实标记 partial／dropped。"""

        with self._health_lock:
            health = self._health.setdefault(trace_id, _TraceHealth())
            should_log = health.failure is None
            health.failure = message
        if should_log:
            logger.warning("trace writer degraded for %s: %s", trace_id, message)

    def health(self, trace_id: str) -> _TraceHealth:
        """返回当前运行的 trace 写入健康状态。"""

        with self._health_lock:
            health = self._health.get(trace_id)
            if health is None:
                health = _TraceHealth()
                self._health[trace_id] = health
            return _TraceHealth(
                dropped_events=health.dropped_events,
                failure=health.failure,
            )

    def wait_for_idle(self, timeout_seconds: float = TRACE_SHUTDOWN_FLUSH_SECONDS) -> bool:
        """等待队列排空，供测试和受时限的退出 flush 使用。"""

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def close(self) -> None:
        """停止接收新任务，并在固定时限内 best-effort 写完已有事件。"""

        self._closed = True
        self.wait_for_idle(TRACE_SHUTDOWN_FLUSH_SECONDS)
        worker = self._worker
        if worker is not None:
            worker.join(timeout=1)

    def _enqueue(
        self,
        command: _ArtifactWrite | _SpanWrite | _ManifestWrite,
        *,
        trace_id: str,
        reserve_manifest_slot: bool,
    ) -> bool:
        self._ensure_worker()
        limit = max(1, self._capacity - 1) if reserve_manifest_slot else self._capacity
        if self._queue.qsize() >= limit:
            with self._health_lock:
                health = self._health.setdefault(trace_id, _TraceHealth())
                health.dropped_events += 1
            return False
        try:
            self._queue.put_nowait(command)
        except queue.Full:
            with self._health_lock:
                health = self._health.setdefault(trace_id, _TraceHealth())
                should_log = health.dropped_events == 0
                health.dropped_events += 1
            if should_log:
                logger.warning("trace queue full; dropping events for %s", trace_id)
            return False
        return True

    def _ensure_worker(self) -> None:
        if self._closed:
            return
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._writer_loop,
                name="trace-writer",
                daemon=True,
            )
            self._worker.start()

    def _writer_loop(self) -> None:
        while True:
            try:
                first = self._queue.get(timeout=TRACE_WRITE_INTERVAL_SECONDS)
            except queue.Empty:
                if self._closed:
                    return
                continue
            batch = [first]
            while len(batch) < TRACE_WRITE_BATCH_SIZE:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            for command in batch:
                try:
                    self._write(command)
                finally:
                    self._queue.task_done()

    def _write(self, command: _ArtifactWrite | _SpanWrite | _ManifestWrite) -> None:
        try:
            if isinstance(command, _ArtifactWrite):
                self._write_artifact(command)
            elif isinstance(command, _SpanWrite):
                self._append_span(command)
            else:
                self._write_manifest(command)
        except Exception as error:
            self.mark_failure(command.trace_id, f"{type(error).__name__}: {error}")

    def _write_artifact(self, command: _ArtifactWrite) -> None:
        relative = command.artifact.relative_path
        if relative is None:
            raise ValueError("stored artifact has no relative path")
        path = command.trace_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temporary.write_bytes(command.content)
        os.replace(temporary, path)

    def _append_span(self, command: _SpanWrite) -> None:
        path = command.trace_dir / "trace.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(
                    command.span.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            stream.write("\n")

    def _write_manifest(self, command: _ManifestWrite) -> None:
        health = self.health(command.trace_id)
        manifest = command.manifest.model_copy(
            update={
                "trace_status": health.status,
                "dropped_events": health.dropped_events,
            }
        )
        path = command.trace_dir / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temporary.write_text(
            manifest.model_dump_json(indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
        from app.tracing.catalog import TraceCatalog

        try:
            TraceCatalog(self._trace_root).upsert_manifest(path, manifest)
        except Exception:
            # manifest 是事实源；索引损坏时下次读取会从 manifest 重建。
            logger.warning("trace index update failed for %s", manifest.trace_id)
            return


class TraceSession:
    """一次运行私有的 trace 写入接口；不直接操作文件。"""

    def __init__(
        self,
        *,
        store: TraceStore,
        mode: TraceMode,
        question: str,
        app_version: str,
        versions: dict[str, Any],
        budget: dict[str, Any],
    ) -> None:
        self._store = store
        self.mode: TraceMode = mode
        self.trace_id = new_trace_id()
        self.started_at = utc_now_iso()
        self._started_monotonic = time.monotonic()
        self._trace_dir = (
            store._trace_root  # pyright: ignore[reportPrivateUsage]
            / self.started_at[:10]
            / self.trace_id
        )
        self._question = question
        self._app_version = app_version
        self._versions = dict(versions)
        self._budget = dict(budget)
        self._root_span: TraceSpanHandle | None = None
        self._artifacts: list[TraceArtifactRecord] = []
        self._snapshot_id: str | None = None
        self._snapshot_status: str | None = None
        self._completeness: str | None = None
        self.question_artifact: TraceArtifactRecord | None = None
        if mode != "off":
            self.question_artifact = self.record_artifact(
                kind="question",
                mime_type="text/plain; charset=utf-8",
                content=question,
            )

    def start_span(
        self,
        name: str,
        kind: str,
        *,
        parent_span_id: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> TraceSpanHandle:
        """开始一个 span；父节点缺省使用 trace 根 span。"""

        parent = parent_span_id
        if parent is None and self._root_span is not None:
            parent = self._root_span.span_id
        return TraceSpanHandle(
            trace_id=self.trace_id,
            span_id=new_span_id(),
            parent_span_id=parent,
            name=name,
            kind=kind,
            start_time_unix_nano=time.time_ns(),
            started_at=utc_now_iso(),
            started_monotonic=time.monotonic(),
            attributes=attributes or {},
        )

    def end_span(
        self,
        handle: TraceSpanHandle,
        *,
        status: str,
        error_code: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """结束一个 span，并异步提交给 writer。"""

        handle.attributes.update(attributes or {})
        if self.mode == "off":
            return
        span = TraceSpanRecord(
            trace_id=handle.trace_id,
            span_id=handle.span_id,
            parent_span_id=handle.parent_span_id,
            name=handle.name,
            kind=handle.kind,  # type: ignore[arg-type]
            start_time_unix_nano=handle.start_time_unix_nano,
            end_time_unix_nano=time.time_ns(),
            started_at=handle.started_at,
            ended_at=utc_now_iso(),
            status="ok" if status == "ok" else "error",
            error_code=error_code,
            attributes={
                **handle.attributes,
                "app.duration_ms": round(
                    (time.monotonic() - handle.started_monotonic) * 1_000,
                    3,
                ),
            },
            events=handle.events,
        )
        self._store.enqueue_span(self._trace_dir, span)

    def add_span_event(
        self,
        handle: TraceSpanHandle,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """在 span 上追加预算、重试等事件。"""

        handle.add_event(name, attributes)

    def record_artifact(
        self,
        *,
        kind: str,
        mime_type: str,
        content: str | bytes | dict[str, Any] | list[Any],
    ) -> TraceArtifactRecord:
        """按内容 hash 生成 artifact 引用；metadata 模式不落正文。"""

        encoded = self._encode_content(content, mime_type)
        content_hash = hashlib.sha256(encoded).hexdigest()
        suffix = ".json" if "json" in mime_type else ".txt"
        relative_path = f"artifacts/{content_hash}{suffix}" if self.mode == "full" else None
        artifact = TraceArtifactRecord(
            artifact_id=content_hash,
            kind=kind,
            mime_type=mime_type,
            size_bytes=len(encoded),
            content_hash=content_hash,
            state="stored" if self.mode == "full" else "omitted",
            relative_path=relative_path,
        )
        self._artifacts.append(artifact)
        if self.mode == "full":
            self._store.enqueue_artifact(
                self._trace_dir,
                self.trace_id,
                artifact,
                encoded,
            )
        return artifact

    def set_snapshot(
        self,
        *,
        snapshot_id: str,
        snapshot_status: str,
        completeness: str,
    ) -> None:
        """固化本次运行使用的 collection 快照。"""

        self._snapshot_id = snapshot_id
        self._snapshot_status = snapshot_status
        self._completeness = completeness

    def finish(
        self,
        *,
        status: str,
        termination_reason: str,
        answer_completeness: str,
        agent_turns: int,
        tool_calls: int,
        model_calls: int,
        retry_count: int,
        input_tokens: int,
        output_tokens: int,
        answer: str | None,
        limitations: list[dict[str, Any]],
        citation_annotations: dict[str, Any] | None = None,
    ) -> None:
        """关闭根 span，并提交不可变 manifest。"""

        if self.mode == "off":
            return
        if answer is not None:
            answer_artifact = self.record_artifact(
                kind="answer",
                mime_type="text/markdown; charset=utf-8",
                content=answer,
            )
        else:
            answer_artifact = None
        citation_artifact = (
            self.record_artifact(
                kind="citations",
                mime_type="application/json",
                content=citation_annotations,
            )
            if citation_annotations is not None
            else None
        )
        root = self._root_span
        if root is None:
            raise RuntimeError("trace session has no root span")
        root.add_event(
            "trace.summary",
            {
                "app.status": status,
                "app.termination_reason": termination_reason,
                "app.answer_artifact": (
                    answer_artifact.model_dump(mode="json") if answer_artifact is not None else None
                ),
                "app.citations_artifact": (
                    citation_artifact.model_dump(mode="json")
                    if citation_artifact is not None
                    else None
                ),
            },
        )
        self.end_span(
            root,
            status="ok" if status in {"succeeded", "partial"} else "error",
            error_code=None if status in {"succeeded", "partial"} else termination_reason,
        )
        health = self._store.health(self.trace_id)
        replay_scope: ReplayScope = (
            "retrieval"
            if self.mode == "full" and self._snapshot_id is not None
            else "evidence"
            if self.mode == "full"
            else "metadata"
        )
        ended_at = utc_now_iso()
        manifest = TraceManifestRecord(
            trace_id=self.trace_id,
            mode=self.mode,
            app_version=self._app_version,
            started_at=self.started_at,
            ended_at=ended_at,
            duration_ms=round((time.monotonic() - self._started_monotonic) * 1_000),
            status=status,  # type: ignore[arg-type]
            termination_reason=termination_reason,
            answer_completeness=answer_completeness,  # type: ignore[arg-type]
            question_preview=" ".join(self._question.split())[:TRACE_QUESTION_PREVIEW_CHARS],
            question_artifact=self.question_artifact,
            snapshot_id=self._snapshot_id,
            snapshot_status=self._snapshot_status,
            completeness=self._completeness,
            agent_turns=agent_turns,
            tool_calls=tool_calls,
            model_calls=model_calls,
            retry_count=retry_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            budget=self._budget,
            versions=self._versions,
            artifacts=self._artifacts,
            trace_status=health.status,
            dropped_events=health.dropped_events,
            replay_scope=replay_scope,
            limitations=limitations,
        )
        self._store.enqueue_manifest(self._trace_dir, manifest)

    @staticmethod
    def _encode_content(
        content: str | bytes | dict[str, Any] | list[Any],
        mime_type: str,
    ) -> bytes:
        if isinstance(content, bytes):
            return content
        if isinstance(content, str):
            return content.encode("utf-8")
        return json.dumps(
            content,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
