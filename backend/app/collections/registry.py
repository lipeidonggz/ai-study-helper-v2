from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.engine import CursorResult

from app.collections.models import (
    ActiveCollectionPointer,
    CandidateSnapshotManifest,
    PublishRecord,
)
from app.ingestion.errors import IngestFailure
from app.storage.collection_models import (
    ActiveCollectionPointerRow,
    CollectionPublishRecordRow,
)
from app.storage.database import StorageDatabase


class CollectionRegistry:
    """使用 SQLite generation CAS 管理活动 collection 指针和发布记录。"""

    def __init__(self, database: StorageDatabase) -> None:
        self._database = database

    def current(self) -> ActiveCollectionPointer | None:
        """返回当前活动指针；首个 collection 发布前返回 None。"""

        with self._database.session() as session:
            row = session.get(ActiveCollectionPointerRow, 1)
            return None if row is None else self._pointer_from_row(row)

    def publish(
        self,
        manifest: CandidateSnapshotManifest,
        *,
        snapshot_path: str,
        expected_generation: int | None = None,
        reason: str | None = None,
    ) -> tuple[ActiveCollectionPointer, PublishRecord]:
        """以 generation CAS 提交 pointer，并返回完整发布审计记录。

        候选快照必须已经在最终目录中且由调用方完成验证。发布记录先写
        intent，再切换 pointer；pointer 是当前活动的唯一事实源。
        """

        if manifest.status not in {"ready", "ready_partial"}:
            raise IngestFailure(
                "snapshot_not_publishable",
                "only ready or ready_partial snapshots can be published",
                409,
            )
        current = self.current()
        if (
            current is not None
            and current.snapshot_id == manifest.snapshot_id
            and current.manifest_hash == manifest.manifest_hash
        ):
            return current, self._idempotent_record(current, manifest, snapshot_path)

        actual_generation = current.generation if current is not None else 0
        requested_generation = (
            actual_generation if expected_generation is None else expected_generation
        )
        expected_generation = requested_generation
        publish_id = uuid4().hex
        started_at = datetime.now(UTC)
        verification: dict[str, object] = {
            "snapshot_path": snapshot_path,
            "manifest_hash": manifest.manifest_hash,
            "status": manifest.status,
            "completeness": manifest.completeness,
            "documents": len(manifest.documents),
            "failed_documents": len(manifest.failed_documents),
        }
        self._create_intent(
            publish_id=publish_id,
            manifest=manifest,
            from_snapshot_id=current.snapshot_id if current is not None else None,
            expected_generation=expected_generation,
            started_at=started_at,
            verification=verification,
        )
        if requested_generation != actual_generation:
            self._finish_record(
                publish_id,
                result="aborted",
                reason="publish_generation_conflict",
                finished_at=datetime.now(UTC),
            )
            raise IngestFailure(
                "publish_generation_conflict",
                "active collection pointer changed before publish",
                409,
                retryable=True,
            )

        try:
            pointer = self._commit_pointer(
                manifest=manifest,
                expected_generation=expected_generation,
            )
        except Exception as error:
            self._finish_record(
                publish_id,
                result="aborted",
                reason=str(error),
                finished_at=datetime.now(UTC),
            )
            if isinstance(error, IngestFailure):
                raise
            raise IngestFailure(
                "publish_failed",
                f"could not commit collection pointer: {error}",
                500,
            ) from error

        record = self._finish_record(
            publish_id,
            result="committed",
            reason=reason,
            finished_at=datetime.now(UTC),
        )
        return pointer, record

    def record_aborted(
        self,
        manifest: CandidateSnapshotManifest,
        *,
        snapshot_path: str,
        reason: str,
    ) -> PublishRecord:
        """记录不改变 activity pointer 的失败发布尝试。"""

        current = self.current()
        expected_generation = current.generation if current is not None else 0
        publish_id = uuid4().hex
        started_at = datetime.now(UTC)
        verification: dict[str, object] = {
            "snapshot_path": snapshot_path,
            "manifest_hash": manifest.manifest_hash,
            "status": manifest.status,
            "reason": reason,
        }
        self._create_intent(
            publish_id=publish_id,
            manifest=manifest,
            from_snapshot_id=current.snapshot_id if current is not None else None,
            expected_generation=expected_generation,
            started_at=started_at,
            verification=verification,
        )
        return self._finish_record(
            publish_id,
            result="aborted",
            reason=reason,
            finished_at=datetime.now(UTC),
        )

    def _commit_pointer(
        self,
        *,
        manifest: CandidateSnapshotManifest,
        expected_generation: int,
    ) -> ActiveCollectionPointer:
        now = datetime.now(UTC)
        with self._database.session() as session, session.begin():
            current = session.get(ActiveCollectionPointerRow, 1)
            if current is None:
                if expected_generation != 0:
                    raise IngestFailure(
                        "publish_generation_conflict",
                        "active collection pointer changed during publish",
                        409,
                        retryable=True,
                    )
                row = ActiveCollectionPointerRow(
                    pointer_id=1,
                    snapshot_id=manifest.snapshot_id,
                    manifest_hash=manifest.manifest_hash,
                    generation=1,
                    previous_snapshot_id=None,
                    published_at=now,
                )
                session.add(row)
                session.flush()
                return self._pointer_from_row(row)

            if current.generation != expected_generation:
                raise IngestFailure(
                    "publish_generation_conflict",
                    "active collection pointer changed during publish",
                    409,
                    retryable=True,
                )
            result = session.execute(
                update(ActiveCollectionPointerRow)
                .where(
                    ActiveCollectionPointerRow.pointer_id == 1,
                    ActiveCollectionPointerRow.generation == expected_generation,
                )
                .values(
                    snapshot_id=manifest.snapshot_id,
                    manifest_hash=manifest.manifest_hash,
                    generation=expected_generation + 1,
                    previous_snapshot_id=current.snapshot_id,
                    published_at=now,
                )
            )
            if cast(CursorResult[object], result).rowcount != 1:
                raise IngestFailure(
                    "publish_generation_conflict",
                    "active collection pointer CAS failed",
                    409,
                    retryable=True,
                )
            updated = session.get(ActiveCollectionPointerRow, 1)
            if updated is None:
                raise IngestFailure(
                    "publish_pointer_missing",
                    "active collection pointer disappeared after CAS",
                    500,
                )
            return self._pointer_from_row(updated)

    def _create_intent(
        self,
        *,
        publish_id: str,
        manifest: CandidateSnapshotManifest,
        from_snapshot_id: str | None,
        expected_generation: int,
        started_at: datetime,
        verification: dict[str, object],
    ) -> None:
        with self._database.session() as session, session.begin():
            session.add(
                CollectionPublishRecordRow(
                    publish_id=publish_id,
                    from_snapshot_id=from_snapshot_id,
                    to_snapshot_id=manifest.snapshot_id,
                    expected_generation=expected_generation,
                    result="aborted",
                    reason="publish_intent",
                    started_at=started_at,
                    finished_at=None,
                    verification_json=json.dumps(
                        verification,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                )
            )

    def _finish_record(
        self,
        publish_id: str,
        *,
        result: Literal["committed", "aborted"],
        reason: str | None,
        finished_at: datetime,
    ) -> PublishRecord:
        with self._database.session() as session, session.begin():
            row = session.get(CollectionPublishRecordRow, publish_id)
            if row is None:
                raise IngestFailure(
                    "publish_record_missing",
                    "publish audit record disappeared",
                    500,
                )
            row.result = result
            row.reason = reason
            row.finished_at = finished_at
            return PublishRecord(
                publish_id=row.publish_id,
                from_snapshot_id=row.from_snapshot_id,
                to_snapshot_id=row.to_snapshot_id,
                expected_generation=row.expected_generation,
                result=result,
                reason=reason,
                started_at=row.started_at.isoformat(),
                finished_at=finished_at.isoformat(),
                verification=json.loads(row.verification_json),
            )

    def _idempotent_record(
        self,
        pointer: ActiveCollectionPointer,
        manifest: CandidateSnapshotManifest,
        snapshot_path: str,
    ) -> PublishRecord:
        publish_id = uuid4().hex
        now = datetime.now(UTC)
        verification: dict[str, object] = {
            "snapshot_path": snapshot_path,
            "manifest_hash": manifest.manifest_hash,
            "reason": "idempotent",
        }
        self._create_intent(
            publish_id=publish_id,
            manifest=manifest,
            from_snapshot_id=pointer.snapshot_id,
            expected_generation=pointer.generation,
            started_at=now,
            verification=verification,
        )
        return self._finish_record(
            publish_id,
            result="committed",
            reason="idempotent",
            finished_at=datetime.now(UTC),
        )

    @staticmethod
    def _pointer_from_row(row: ActiveCollectionPointerRow) -> ActiveCollectionPointer:
        return ActiveCollectionPointer(
            snapshot_id=row.snapshot_id,
            manifest_hash=row.manifest_hash,
            generation=row.generation,
            previous_snapshot_id=row.previous_snapshot_id,
            published_at=row.published_at.isoformat(),
        )
