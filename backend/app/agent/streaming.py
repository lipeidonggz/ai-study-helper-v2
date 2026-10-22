from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from app.agent.runtime import (
    AgentEvent,
    AgentRunResult,
    AgentRuntime,
    RunCancellationToken,
)

STREAM_SCHEMA_VERSION = "agent-stream-v1"
STREAM_RETENTION_SECONDS = 10 * 60


class AgentRunNotFoundError(LookupError):
    """请求的流式 Agent 运行不存在或已经超过内存保留期。"""


class AgentRunEventGapError(ValueError):
    """客户端请求的事件位置不在当前可恢复缓冲中。"""


@dataclass(frozen=True, slots=True)
class AgentStreamEnvelope:
    """发送给客户端的稳定 SSE 事件封装。"""

    trace_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any]

    def to_sse(self) -> str:
        """编码为带事件 ID 的单向 SSE 数据帧。"""

        body = json.dumps(
            {
                "schema_version": STREAM_SCHEMA_VERSION,
                "trace_id": self.trace_id,
                "sequence": self.sequence,
                "event_type": self.event_type,
                "payload": self.payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return f"id: {self.sequence}\nevent: {self.event_type}\ndata: {body}\n\n"


@dataclass(slots=True)
class _RunRecord:
    run_id: str
    trace_id: str
    cancellation: RunCancellationToken
    events: list[AgentStreamEnvelope] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    done: bool = False
    finished_at: float | None = None
    result: AgentRunResult | None = None
    failure: str | None = None

    def append(self, event: AgentEvent) -> None:
        """追加一个有序事件并唤醒所有订阅者。"""

        self.events.append(
            AgentStreamEnvelope(
                trace_id=self.trace_id,
                sequence=len(self.events) + 1,
                event_type=event.event_type,
                payload=dict(event.payload),
            )
        )
        self.changed.set()

    def update_run_started(self, payload: dict[str, Any]) -> None:
        """用 Runtime 的真实快照信息补全唯一的 run_started 事件。"""

        if not self.events or self.events[0].event_type != "run_started":
            raise RuntimeError("run_started must be the first streamed event")
        started = self.events[0]
        self.events[0] = AgentStreamEnvelope(
            trace_id=started.trace_id,
            sequence=started.sequence,
            event_type=started.event_type,
            payload={**started.payload, **payload, "run_id": self.run_id},
        )
        self.changed.set()


class AgentRunStreamService:
    """在单个 sidecar 进程内管理流式运行、事件续传和取消。"""

    def __init__(self, *, retention_seconds: int = STREAM_RETENTION_SECONDS) -> None:
        self._retention_seconds = retention_seconds
        self._runs: dict[str, _RunRecord] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self, runtime: AgentRuntime, question: str) -> AgentStreamEnvelope:
        """创建运行并立即返回 `run_started` 事件，模型工作转入后台任务。"""

        self._prune_expired()
        run_id = uuid4().hex
        trace_id = f"trace_{run_id}"
        record = _RunRecord(
            run_id=run_id,
            trace_id=trace_id,
            cancellation=RunCancellationToken(),
        )
        self._runs[run_id] = record
        record.append(
            AgentEvent(
                event_type="run_started",
                payload={"run_id": run_id, "snapshot_id": None},
            )
        )
        task = asyncio.create_task(self._execute(record, runtime, question))
        self._track_task(task)
        await asyncio.sleep(0)
        return record.events[0]

    def validate_cursor(self, run_id: str, after_sequence: int) -> None:
        """在开始 HTTP 流之前验证运行和事件位置，避免晚发现错误。"""

        record = self._get_record(run_id)
        latest = len(record.events)
        if after_sequence < 0 or after_sequence > latest:
            raise AgentRunEventGapError("requested event position is outside the buffer")

    async def subscribe(
        self,
        run_id: str,
        *,
        after_sequence: int,
    ) -> AsyncIterator[AgentStreamEnvelope]:
        """按事件序号回放并持续等待新事件，直到运行进入终态。"""

        record = self._get_record(run_id)
        cursor = after_sequence
        while True:
            record.changed.clear()
            if cursor < len(record.events):
                for envelope in record.events[cursor:]:
                    cursor = envelope.sequence
                    yield envelope
                continue
            if record.done:
                return
            await record.changed.wait()

    def cancel(self, run_id: str) -> None:
        """设置运行取消标志；实际终态由 Agent Runtime 发布。"""

        self._get_record(run_id).cancellation.cancel()

    def run_state(self, run_id: str) -> dict[str, Any]:
        """返回不含正文的诊断状态，供取消 API 响应和测试使用。"""

        record = self._get_record(run_id)
        return {
            "run_id": record.run_id,
            "trace_id": record.trace_id,
            "status": "completed" if record.done else "running",
            "last_sequence": len(record.events),
            "failure": record.failure,
        }

    async def shutdown(self) -> None:
        """取消所有仍在执行的运行，避免应用退出后遗留后台任务。"""

        for record in self._runs.values():
            if not record.done:
                record.cancellation.cancel()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        self._tasks.clear()
        self._runs.clear()

    def _get_record(self, run_id: str) -> _RunRecord:
        self._prune_expired()
        record = self._runs.get(run_id)
        if record is None:
            raise AgentRunNotFoundError(run_id)
        return record

    async def _execute(
        self,
        record: _RunRecord,
        runtime: AgentRuntime,
        question: str,
    ) -> None:
        def emit(event: AgentEvent) -> None:
            if event.event_type == "run_started":
                record.update_run_started(event.payload)
                return
            record.append(event)

        try:
            result = await runtime.run(
                question,
                emit=emit,
                cancellation=record.cancellation,
            )
            record.result = result
            if not any(
                event.event_type in {"answer_completed", "run_failed", "run_canceled"}
                for event in record.events
            ):
                record.append(
                    AgentEvent(
                        event_type="run_failed",
                        payload={
                            "termination_reason": "stream_terminal_event_missing",
                            "message": "Agent run ended without a terminal event",
                        },
                    )
                )
        except Exception as error:
            record.failure = type(error).__name__
            record.append(
                AgentEvent(
                    event_type="run_failed",
                    payload={
                        "termination_reason": "stream_internal_error",
                        "message": f"streaming run failed: {type(error).__name__}",
                    },
                )
            )
        finally:
            record.done = True
            record.finished_at = time.monotonic()
            record.changed.set()

    def _track_task(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _prune_expired(self) -> None:
        if self._retention_seconds <= 0:
            return
        now = time.monotonic()
        expired = [
            run_id
            for run_id, record in self._runs.items()
            if record.finished_at is not None
            and now - record.finished_at >= self._retention_seconds
        ]
        for run_id in expired:
            self._runs.pop(run_id, None)
