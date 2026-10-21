from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select

from app.collections.models import SnapshotFailure
from app.collections.service import CollectionSnapshotService
from app.ingestion.errors import IngestFailure
from app.ingestion.models import (
    BuildIndexRequest,
    IngestPreviewRequest,
    PersistIngestRequest,
)
from app.ingestion.service import IngestionService
from app.storage.database import StorageDatabase
from app.storage.models import utc_now
from app.storage.task_models import IngestionTaskRow
from app.tasks.models import (
    BackgroundIngestRequest,
    BackgroundSourceFailure,
    BackgroundTaskRecord,
    IngestTaskSource,
    TaskStatus,
)


class TaskNotFoundError(IngestFailure):
    """任务 ID 不存在时使用的稳定错误。"""

    def __init__(self, task_id: str) -> None:
        super().__init__(
            "task_not_found",
            f"background task does not exist: {task_id}",
            404,
        )


class TaskCanceled(Exception):
    """worker 在 checkpoint 发现任务已取消。"""


class BackgroundTaskService:
    """执行并持久化 S0–S6 后台入库任务。"""

    def __init__(
        self,
        data_root: Path,
        ingestion_service: IngestionService,
        collection_service: CollectionSnapshotService,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._data_root = data_root.resolve()
        self._ingestion = ingestion_service
        self._collections = collection_service
        self._database = StorageDatabase(self._data_root)
        self._sleep = sleep
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._started = False

    async def start(self) -> None:
        """启动服务；把崩溃前遗留的 running 任务标为 interrupted。"""

        if self._started:
            return
        self._started = True
        with self._database.session() as session, session.begin():
            rows = session.scalars(
                select(IngestionTaskRow).where(IngestionTaskRow.status == "running")
            ).all()
            for row in rows:
                row.status = "interrupted"
                row.error_code = "process_restarted"
                row.error_message = "background task was interrupted by a process restart"
                row.retryable = True

    async def stop(self) -> None:
        """停止当前进程内的 worker，不删除持久任务记录。"""

        workers = list(self._workers.values())
        self._workers.clear()
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    async def create(self, request: BackgroundIngestRequest) -> BackgroundTaskRecord:
        """持久化任务并立即启动后台 worker。"""

        await self.start()
        task_id = f"task_{uuid4().hex[:26]}"
        now = utc_now()
        row = IngestionTaskRow(
            task_id=task_id,
            status="queued",
            stage="queued",
            sources_json=_encode_model_list(request.sources),
            total_sources=len(request.sources),
            next_source_index=0,
            completed_sources=0,
            failed_sources=0,
            successful_job_ids_json="[]",
            failures_json="[]",
            snapshot_id=None,
            error_code=None,
            error_message=None,
            retryable=False,
            created_at=now,
            started_at=None,
            finished_at=None,
        )
        with self._database.session() as session, session.begin():
            session.add(row)
        record = self._record(row)
        self._spawn(task_id)
        return record

    def get(self, task_id: str) -> BackgroundTaskRecord:
        """读取任务状态。"""

        with self._database.session() as session:
            row = session.get(IngestionTaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            return self._record(row)

    def pause(self, task_id: str) -> BackgroundTaskRecord:
        """请求任务在下一个 checkpoint 暂停。"""

        return self._change_control_status(task_id, allowed={"queued", "running"}, target="paused")

    def cancel(self, task_id: str) -> BackgroundTaskRecord:
        """请求任务在下一个 checkpoint 退出。"""

        return self._change_control_status(
            task_id,
            allowed={"queued", "running", "paused", "interrupted"},
            target="canceled",
        )

    async def resume(self, task_id: str) -> BackgroundTaskRecord:
        """从 paused／interrupted 任务的下一个来源继续执行。"""

        with self._database.session() as session, session.begin():
            row = session.get(IngestionTaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            if row.status not in {"paused", "interrupted"}:
                raise IngestFailure(
                    "task_not_resumable",
                    f"task cannot be resumed from status {row.status}",
                    409,
                )
            row.status = "queued"
            row.error_code = None
            row.error_message = None
            row.retryable = False
            result = self._record(row)
        self._spawn(task_id)
        return result

    def _change_control_status(
        self,
        task_id: str,
        *,
        allowed: set[str],
        target: str,
    ) -> BackgroundTaskRecord:
        with self._database.session() as session, session.begin():
            row = session.get(IngestionTaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            if row.status not in allowed:
                raise IngestFailure(
                    "task_control_invalid",
                    f"cannot change task from {row.status} to {target}",
                    409,
                )
            row.status = target
            if target == "canceled":
                row.finished_at = utc_now()
                row.error_code = "task_canceled"
                row.error_message = "background task was canceled by the user"
            return self._record(row)

    def _spawn(self, task_id: str) -> None:
        worker = self._workers.get(task_id)
        if worker is not None and not worker.done():
            return
        self._workers[task_id] = asyncio.create_task(self._run(task_id))

    async def _run(self, task_id: str) -> None:
        """执行一个任务；暂停、取消和进程停止都在阶段边界可观察。"""

        try:
            while True:
                row = self._read_row(task_id)
                if row.status in {"paused", "canceled"}:
                    return
                if row.next_source_index >= row.total_sources:
                    await self._publish(task_id)
                    return
                await self._checkpoint(task_id)
                row = self._mark_running(task_id, stage="S0-S4")
                source = _decode_sources(row.sources_json)[row.next_source_index]
                job_id: str | None = None
                try:
                    preview = await self._ingestion.preview(
                        IngestPreviewRequest(
                            source_kind=source.source_kind,
                            source_value=source.source_value,
                        )
                    )
                    job_id = preview.job_id
                    await self._checkpoint(task_id)
                    await asyncio.to_thread(
                        self._ingestion.persist,
                        PersistIngestRequest(job_id=job_id),
                    )
                    await self._checkpoint(task_id)
                    self._set_stage(task_id, "S5")
                    await self._ingestion.build_index(BuildIndexRequest(job_id=job_id))
                    self._record_success(task_id, job_id)
                except TaskCanceled:
                    return
                except Exception as error:
                    self._record_failure(task_id, source, row.next_source_index, job_id, error)
            await self._publish(task_id)
        except asyncio.CancelledError:
            self._mark_interrupted(task_id)
            raise
        except TaskCanceled:
            return
        except Exception as error:
            self._mark_failed(task_id, error)
        finally:
            self._workers.pop(task_id, None)

    async def _publish(self, task_id: str) -> None:
        row = self._read_row(task_id)
        if row.status == "canceled":
            return
        await self._checkpoint(task_id)
        self._set_stage(task_id, "S6")
        successful_ids = _decode_strings(row.successful_job_ids_json)
        failures = _decode_failures(row.failures_json)
        extra_failures = [
            SnapshotFailure(
                job_id=failure.job_id or task_id,
                stage=failure.stage,
                code=failure.code,
                message=failure.message,
                retryable=failure.retryable,
            )
            for failure in failures
        ]
        try:
            result = await asyncio.to_thread(
                self._collections.publish,
                successful_ids,
                extra_failures=extra_failures,
            )
        except Exception as error:
            self._mark_failed(task_id, error)
            return
        if result.pointer is None:
            self._mark_failed(
                task_id,
                IngestFailure(
                    "collection_not_published",
                    "collection snapshot was not published",
                    409,
                ),
                snapshot_id=result.manifest.snapshot_id,
            )
            return
        self._finish_success(task_id, result.manifest.snapshot_id)

    async def _checkpoint(self, task_id: str) -> None:
        """在阶段边界等待暂停，并让取消立即结束当前 worker。"""

        while True:
            status = self._read_row(task_id).status
            if status == "canceled":
                raise TaskCanceled()
            if status != "paused":
                return
            await self._sleep(0.2)

    def _mark_running(self, task_id: str, *, stage: str) -> IngestionTaskRow:
        with self._database.session() as session, session.begin():
            row = session.get(IngestionTaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            if row.status in {"paused", "canceled"}:
                raise TaskCanceled()
            row.status = "running"
            row.stage = stage
            if row.started_at is None:
                row.started_at = utc_now()
            return row

    def _set_stage(self, task_id: str, stage: str) -> None:
        with self._database.session() as session, session.begin():
            row = session.get(IngestionTaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            if row.status == "canceled":
                raise TaskCanceled()
            if row.status == "paused":
                raise TaskCanceled()
            row.stage = stage

    def _record_success(self, task_id: str, job_id: str) -> None:
        with self._database.session() as session, session.begin():
            row = session.get(IngestionTaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            job_ids = [*_decode_strings(row.successful_job_ids_json), job_id]
            row.successful_job_ids_json = json.dumps(job_ids)
            row.next_source_index += 1
            row.completed_sources += 1
            row.stage = "S5"

    def _record_failure(
        self,
        task_id: str,
        source: IngestTaskSource,
        source_index: int,
        job_id: str | None,
        error: Exception,
    ) -> None:
        failure = BackgroundSourceFailure(
            source_index=source_index,
            source_kind=source.source_kind,
            job_id=job_id,
            stage="S0-S5",
            code=getattr(error, "code", "source_ingestion_failed"),
            message=getattr(error, "message", str(error)),
            retryable=getattr(error, "retryable", False),
        )
        with self._database.session() as session, session.begin():
            row = session.get(IngestionTaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            failures = [*_decode_failures(row.failures_json), failure]
            row.failures_json = _encode_model_list(failures)
            row.next_source_index += 1
            row.completed_sources += 1
            row.failed_sources += 1
            row.stage = "S5"

    def _finish_success(self, task_id: str, snapshot_id: str) -> None:
        with self._database.session() as session, session.begin():
            row = session.get(IngestionTaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            row.status = "succeeded"
            row.stage = "S6"
            row.snapshot_id = snapshot_id
            row.error_code = None
            row.error_message = None
            row.retryable = False
            row.finished_at = utc_now()

    def _mark_failed(
        self,
        task_id: str,
        error: Exception,
        *,
        snapshot_id: str | None = None,
    ) -> None:
        with self._database.session() as session, session.begin():
            row = session.get(IngestionTaskRow, task_id)
            if row is None:
                return
            row.status = "failed"
            row.stage = "S6"
            row.snapshot_id = snapshot_id
            row.error_code = getattr(error, "code", "background_task_failed")
            row.error_message = getattr(error, "message", str(error))
            row.retryable = getattr(error, "retryable", False)
            row.finished_at = utc_now()

    def _mark_interrupted(self, task_id: str) -> None:
        with self._database.session() as session, session.begin():
            row = session.get(IngestionTaskRow, task_id)
            if row is None or row.status in {"succeeded", "failed", "canceled"}:
                return
            row.status = "interrupted"
            row.error_code = "process_stopped"
            row.error_message = "background worker stopped before task completion"
            row.retryable = True

    def _read_row(self, task_id: str) -> IngestionTaskRow:
        with self._database.session() as session:
            row = session.get(IngestionTaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            return row

    @staticmethod
    def _record(row: IngestionTaskRow) -> BackgroundTaskRecord:
        return BackgroundTaskRecord(
            task_id=row.task_id,
            status=cast(TaskStatus, row.status),
            stage=row.stage,
            sources=_decode_sources(row.sources_json),
            total_sources=row.total_sources,
            next_source_index=row.next_source_index,
            completed_sources=row.completed_sources,
            failed_sources=row.failed_sources,
            successful_job_ids=_decode_strings(row.successful_job_ids_json),
            failures=_decode_failures(row.failures_json),
            snapshot_id=row.snapshot_id,
            error_code=row.error_code,
            error_message=row.error_message,
            retryable=row.retryable,
            created_at=row.created_at.isoformat(),
            started_at=row.started_at.isoformat() if row.started_at else None,
            finished_at=row.finished_at.isoformat() if row.finished_at else None,
        )


def _encode_model_list(values: list[Any]) -> str:
    return json.dumps(
        [
            value.model_dump(mode="json") if hasattr(value, "model_dump") else value
            for value in values
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _decode_sources(value: str) -> list[IngestTaskSource]:
    return [IngestTaskSource.model_validate(item) for item in json.loads(value)]


def _decode_failures(value: str) -> list[BackgroundSourceFailure]:
    return [BackgroundSourceFailure.model_validate(item) for item in json.loads(value)]


def _decode_strings(value: str) -> list[str]:
    decoded = json.loads(value)
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise ValueError("task string list is invalid")
    return decoded
