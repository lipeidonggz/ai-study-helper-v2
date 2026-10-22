from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.tracing.models import (
    TraceDetailRecord,
    TraceListItem,
    TraceManifestRecord,
    TraceSpanRecord,
)

_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
TRACE_METADATA_RETENTION = timedelta(days=30)
TRACE_FULL_RETENTION = timedelta(days=7)
TRACE_DISK_LIMIT_BYTES = 512 * 1024 * 1024


class TraceNotFoundError(LookupError):
    """指定 trace 不存在或已被保留策略清理。"""


class TraceInvalidError(ValueError):
    """trace 文件、manifest 或 artifact 无法通过校验。"""


class TraceCatalog:
    """读取 trace 事实文件，并维护可重建的 SQLite 派生索引。"""

    def __init__(self, trace_root: Path) -> None:
        self._trace_root = trace_root.resolve()
        self._index_path = self._trace_root / "index.sqlite"

    def upsert_manifest(self, manifest_path: Path, manifest: TraceManifestRecord) -> None:
        """把新关闭的 manifest 写入派生索引。"""

        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO traces (
                    trace_id, mode, started_at, ended_at, status,
                    termination_reason, question_preview, duration_ms,
                    input_tokens, output_tokens, trace_status, replay_scope,
                    manifest_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trace_id) DO UPDATE SET
                    mode=excluded.mode,
                    started_at=excluded.started_at,
                    ended_at=excluded.ended_at,
                    status=excluded.status,
                    termination_reason=excluded.termination_reason,
                    question_preview=excluded.question_preview,
                    duration_ms=excluded.duration_ms,
                    input_tokens=excluded.input_tokens,
                    output_tokens=excluded.output_tokens,
                    trace_status=excluded.trace_status,
                    replay_scope=excluded.replay_scope,
                    manifest_path=excluded.manifest_path
                """,
                (
                    manifest.trace_id,
                    manifest.mode,
                    manifest.started_at,
                    manifest.ended_at,
                    manifest.status,
                    manifest.termination_reason,
                    manifest.question_preview,
                    manifest.duration_ms,
                    manifest.input_tokens,
                    manifest.output_tokens,
                    manifest.trace_status,
                    manifest.replay_scope,
                    str(manifest_path.resolve()),
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def list_recent(self, limit: int = 50) -> list[TraceListItem]:
        """按结束时间倒序返回最近 trace；索引损坏时自动重建。"""

        if not self._index_path.exists():
            self.rebuild()
        try:
            return self._list_from_index(limit)
        except sqlite3.DatabaseError:
            self.rebuild()
            return self._list_from_index(limit)

    def load(self, trace_id: str) -> TraceDetailRecord:
        """从事实文件读取 manifest 与全部 span。"""

        if not _TRACE_ID_PATTERN.fullmatch(trace_id):
            raise TraceNotFoundError(trace_id)
        matches = list(self._trace_root.glob(f"*/{trace_id}/manifest.json"))
        if len(matches) != 1:
            raise TraceNotFoundError(trace_id)
        trace_dir = matches[0].parent
        manifest = TraceManifestRecord.model_validate_json(matches[0].read_text("utf-8"))
        if manifest.trace_id != trace_id:
            raise TraceInvalidError("manifest trace_id does not match its directory")
        spans: list[TraceSpanRecord] = []
        trace_path = trace_dir / "trace.jsonl"
        if trace_path.exists():
            for line in trace_path.read_text("utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    spans.append(TraceSpanRecord.model_validate_json(line))
                except ValueError as error:
                    raise TraceInvalidError(f"invalid trace span: {error}") from error
        spans.sort(key=lambda span: (span.started_at, span.span_id))
        return TraceDetailRecord(manifest=manifest, spans=spans)

    def artifact_path(self, trace_id: str, artifact_id: str) -> Path:
        """解析并校验一个 stored artifact 的真实路径。"""

        detail = self.load(trace_id)
        artifact = next(
            (item for item in detail.manifest.artifacts if item.artifact_id == artifact_id),
            None,
        )
        if artifact is None or artifact.state != "stored" or artifact.relative_path is None:
            raise TraceNotFoundError(artifact_id)
        trace_dir = self._trace_root / detail.manifest.started_at[:10] / trace_id
        path = (trace_dir / artifact.relative_path).resolve()
        if not path.is_relative_to(trace_dir.resolve()) or not path.is_file():
            raise TraceNotFoundError(artifact_id)
        if hashlib.sha256(path.read_bytes()).hexdigest() != artifact.content_hash:
            raise TraceInvalidError(f"artifact checksum mismatch: {artifact_id}")
        return path

    def rebuild(self) -> int:
        """仅从 manifest 重建派生索引，不修改任何 trace 事实文件。"""

        self._trace_root.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        count = 0
        try:
            connection.execute("DELETE FROM traces")
            for manifest_path in sorted(self._trace_root.glob("*/*/manifest.json")):
                try:
                    manifest = TraceManifestRecord.model_validate_json(
                        manifest_path.read_text("utf-8")
                    )
                except (OSError, ValueError):
                    continue
                self._insert(connection, manifest_path, manifest)
                count += 1
            connection.commit()
        finally:
            connection.close()
        return count

    def cleanup(
        self,
        *,
        now: datetime | None = None,
        disk_limit_bytes: int = TRACE_DISK_LIMIT_BYTES,
    ) -> dict[str, int]:
        """按模式保留期与总磁盘上限清理已结束 trace。"""

        reference_time = now or datetime.now(UTC)
        entries: list[tuple[datetime, Path, int]] = []
        for manifest_path in self._trace_root.glob("*/*/manifest.json"):
            try:
                manifest = TraceManifestRecord.model_validate_json(manifest_path.read_text("utf-8"))
                ended_at = datetime.fromisoformat(manifest.ended_at)
            except (OSError, ValueError):
                continue
            if ended_at.tzinfo is None:
                ended_at = ended_at.replace(tzinfo=UTC)
            trace_dir = manifest_path.parent
            entries.append((ended_at, trace_dir, self._directory_size(trace_dir)))

        expired = [
            entry
            for entry in entries
            if reference_time - entry[0]
            > (
                TRACE_FULL_RETENTION
                if self._trace_mode(entry[1]) == "full"
                else TRACE_METADATA_RETENTION
            )
        ]
        expired_paths = {entry[1] for entry in expired}
        remaining = [entry for entry in entries if entry[1] not in expired_paths]
        total_bytes = sum(entry[2] for entry in remaining)
        oversized: list[tuple[datetime, Path, int]] = []
        for entry in sorted(remaining, key=lambda item: item[0]):
            if total_bytes <= disk_limit_bytes:
                break
            oversized.append(entry)
            total_bytes -= entry[2]

        removed = 0
        for _ended_at, trace_dir, _size in [*expired, *oversized]:
            if self._is_safe_trace_dir(trace_dir):
                try:
                    shutil.rmtree(trace_dir)
                except OSError:
                    continue
                removed += 1
        if removed:
            self.rebuild()
        return {
            "removed_by_age": len(expired),
            "removed_by_disk_limit": len(oversized),
            "removed": removed,
            "remaining_bytes": total_bytes,
        }

    def _list_from_index(self, limit: int) -> list[TraceListItem]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT trace_id, mode, started_at, ended_at, status,
                       termination_reason, question_preview, duration_ms,
                       input_tokens, output_tokens, trace_status, replay_scope
                FROM traces
                ORDER BY ended_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            connection.close()
        return [
            TraceListItem(
                trace_id=row[0],
                mode=row[1],
                started_at=row[2],
                ended_at=row[3],
                status=row[4],
                termination_reason=row[5],
                question_preview=row[6],
                duration_ms=row[7],
                input_tokens=row[8],
                output_tokens=row[9],
                trace_status=row[10],
                replay_scope=row[11],
            )
            for row in rows
        ]

    def _connect(self) -> sqlite3.Connection:
        self._trace_root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._index_path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS traces (
                trace_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                status TEXT NOT NULL,
                termination_reason TEXT NOT NULL,
                question_preview TEXT,
                duration_ms INTEGER NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                trace_status TEXT NOT NULL,
                replay_scope TEXT NOT NULL,
                manifest_path TEXT NOT NULL
            )
            """
        )
        return connection

    def _insert(
        self,
        connection: sqlite3.Connection,
        manifest_path: Path,
        manifest: TraceManifestRecord,
    ) -> None:
        connection.execute(
            """
            INSERT OR REPLACE INTO traces (
                trace_id, mode, started_at, ended_at, status,
                termination_reason, question_preview, duration_ms,
                input_tokens, output_tokens, trace_status, replay_scope,
                manifest_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                manifest.trace_id,
                manifest.mode,
                manifest.started_at,
                manifest.ended_at,
                manifest.status,
                manifest.termination_reason,
                manifest.question_preview,
                manifest.duration_ms,
                manifest.input_tokens,
                manifest.output_tokens,
                manifest.trace_status,
                manifest.replay_scope,
                str(manifest_path.resolve()),
            ),
        )

    @staticmethod
    def _directory_size(path: Path) -> int:
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())

    @staticmethod
    def _trace_mode(trace_dir: Path) -> str:
        try:
            manifest = json.loads((trace_dir / "manifest.json").read_text("utf-8"))
            return str(manifest.get("mode", "metadata"))
        except (OSError, ValueError):
            return "metadata"

    def _is_safe_trace_dir(self, trace_dir: Path) -> bool:
        resolved = trace_dir.resolve()
        return (
            resolved.is_relative_to(self._trace_root)
            and _TRACE_ID_PATTERN.fullmatch(resolved.name) is not None
        )
