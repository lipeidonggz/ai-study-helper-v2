from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path

from sqlalchemy import delete, func, select

from app.ingestion.errors import IngestFailure
from app.storage.collection_models import (
    ActiveCollectionPointerRow,
    CollectionPublishRecordRow,
)
from app.storage.database import StorageDatabase
from app.storage.models import (
    DocumentRow,
    DocumentVersionRow,
    IngestionJobRow,
    SourceArtifactRow,
)
from app.storage.task_models import IngestionTaskRow
from app.tasks.service import BackgroundTaskService
from app.workspace.models import ResetWorkspaceResponse

_LOGGER = logging.getLogger(__name__)
_RESET_DIRECTORIES = ("jobs", "collections", "backups/db", "traces")


class WorkspaceResetService:
    """停止新的破坏性操作并清空所有知识库与入库记录。"""

    def __init__(
        self,
        data_root: Path,
        task_service: BackgroundTaskService,
    ) -> None:
        self._data_root = data_root.resolve()
        self._database = StorageDatabase(self._data_root)
        self._tasks = task_service
        self._reset_lock = threading.Lock()

    def reset(self) -> ResetWorkspaceResponse:
        """清除知识库事实源及派生文件，同时保留模型设置和应用配置。

        如果后台 worker 尚未结束则拒绝操作，避免解析或 embedding 线程在清理期间
        继续写入 job、数据库或 collection。重置通过整体移动目录先隔离旧数据，
        再在单一 SQLite 事务中删除记录；事务失败时把目录移回。
        """

        if not self._reset_lock.acquire(blocking=False):
            raise IngestFailure(
                "workspace_reset_in_progress",
                "another workspace reset is already running",
                409,
            )
        try:
            if self._tasks.has_live_workers():
                raise IngestFailure(
                    "workspace_reset_active_task",
                    "cancel or finish the running ingestion task before resetting",
                    409,
                )

            quarantine_root = Path(tempfile.mkdtemp(prefix=".reset-", dir=self._data_root))
            moved: list[tuple[Path, Path]] = []
            counts: dict[str, int] = {}
            try:
                moved = self._quarantine_directories(quarantine_root)
                counts = self._clear_database()
            except Exception as error:
                self._restore_directories(moved)
                shutil.rmtree(quarantine_root, ignore_errors=True)
                if isinstance(error, IngestFailure):
                    raise
                raise IngestFailure(
                    "workspace_reset_failed",
                    f"could not reset the local knowledge base: {error}",
                    500,
                ) from error

            for relative in _RESET_DIRECTORIES:
                (self._data_root / relative).mkdir(parents=True, exist_ok=True)
            cleanup_complete = True
            try:
                shutil.rmtree(quarantine_root)
            except OSError as error:
                # 数据记录已经全部失效；删除失败只留下隔离目录，不能把操作伪装成未执行。
                cleanup_complete = False
                _LOGGER.warning(
                    "workspace reset could not remove quarantine %s: %s",
                    quarantine_root,
                    error,
                )
            return ResetWorkspaceResponse(
                documents=counts["documents"],
                document_versions=counts["document_versions"],
                source_artifacts=counts["source_artifacts"],
                ingestion_jobs=counts["ingestion_jobs"],
                ingestion_tasks=counts["ingestion_tasks"],
                collection_publish_records=counts["collection_publish_records"],
                active_collection_pointer=counts["active_collection_pointer"],
                cleanup_complete=cleanup_complete,
            )
        finally:
            self._reset_lock.release()

    def _quarantine_directories(self, quarantine_root: Path) -> list[tuple[Path, Path]]:
        """把旧 artifacts 原子移出活动目录，确保清理失败时仍可恢复。"""

        moved: list[tuple[Path, Path]] = []
        for relative in _RESET_DIRECTORIES:
            source = self._data_root / relative
            if not source.exists():
                continue
            target = quarantine_root / relative.replace("/", "-")
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            moved.append((source, target))
        return moved

    @staticmethod
    def _restore_directories(moved: list[tuple[Path, Path]]) -> None:
        """在数据库事务失败时按相反顺序恢复刚隔离的目录。"""

        for source, target in reversed(moved):
            if source.exists():
                continue
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, source)

    def _clear_database(self) -> dict[str, int]:
        """按外键顺序删除知识库和操作记录，但保留 provider settings。"""

        models = {
            "documents": DocumentRow,
            "document_versions": DocumentVersionRow,
            "source_artifacts": SourceArtifactRow,
            "ingestion_jobs": IngestionJobRow,
            "ingestion_tasks": IngestionTaskRow,
            "collection_publish_records": CollectionPublishRecordRow,
            "active_collection_pointer": ActiveCollectionPointerRow,
        }
        with self._database.session() as session, session.begin():
            counts = {
                name: int(session.scalar(select(func.count()).select_from(model)) or 0)
                for name, model in models.items()
            }
            for name in (
                "ingestion_tasks",
                "ingestion_jobs",
                "document_versions",
                "documents",
                "source_artifacts",
                "collection_publish_records",
                "active_collection_pointer",
            ):
                session.execute(delete(models[name]))
        return counts
