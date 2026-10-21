from __future__ import annotations

import logging
import os
import re
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.ingestion.errors import IngestFailure

MAX_MIGRATION_BACKUPS = 3
_BACKUP_NAME_PATTERN = re.compile(
    r"^application-before-(?P<revision>.+)-(?P<timestamp>\d{8}T\d{12}Z)\.db$"
)
_LOGGER = logging.getLogger(__name__)


def _migrations_directory() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and isinstance(bundle_root, str):
        return Path(bundle_root) / "migrations"
    return Path(__file__).resolve().parents[2] / "migrations"


def _migration_config(database_url: str) -> Config:
    migrations_directory = _migrations_directory()
    if not migrations_directory.is_dir():
        raise RuntimeError(f"database migrations are missing: {migrations_directory}")
    config = Config()
    config.set_main_option("script_location", migrations_directory.as_posix())
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _head_revision(config: Config) -> str:
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"database migrations must have exactly one head, got {heads}")
    return heads[0]


def _current_revision(database_path: Path) -> str | None:
    if not database_path.exists():
        return None
    connection = sqlite3.connect(database_path)
    try:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.OperationalError as error:
        if "no such table: alembic_version" in str(error).lower():
            return None
        raise
    finally:
        connection.close()
    return str(row[0]) if row else None


def _create_migration_backup(
    database_path: Path,
    backup_directory: Path,
    current_revision: str | None,
) -> Path:
    """用 SQLite 在线备份 API 创建包含 WAL 状态的一致快照。"""

    backup_directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    revision = re.sub(r"[^A-Za-z0-9._-]+", "_", current_revision or "base")
    target = backup_directory / f"application-before-{revision}-{timestamp}.db"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=backup_directory,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    source = sqlite3.connect(database_path)
    destination = sqlite3.connect(temporary_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    try:
        os.replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)
    return target


def _prune_migration_backups(backup_directory: Path) -> None:
    """按 UTC 时间只保留最新三份本系统迁移备份。"""

    candidates: list[tuple[datetime, Path]] = []
    if not backup_directory.is_dir():
        return
    for path in backup_directory.iterdir():
        if not path.is_file():
            continue
        match = _BACKUP_NAME_PATTERN.fullmatch(path.name)
        if match is None:
            continue
        created_at = datetime.strptime(
            match.group("timestamp"),
            "%Y%m%dT%H%M%S%fZ",
        ).replace(tzinfo=UTC)
        candidates.append((created_at, path))

    for _created_at, path in sorted(candidates, reverse=True)[MAX_MIGRATION_BACKUPS:]:
        try:
            path.unlink()
        except OSError as error:
            # 清理失败只允许留下额外备份，不能因此阻断数据库迁移。
            _LOGGER.warning("could not prune old database backup %s: %s", path, error)


def _restore_migration_backup(backup_path: Path, database_path: Path) -> None:
    """用迁移前快照覆盖主库，并 checkpoint WAL。"""

    source = sqlite3.connect(backup_path)
    destination = sqlite3.connect(database_path)
    try:
        source.backup(destination)
        destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        destination.close()
        source.close()


def _configure_sqlite_connection(engine: Engine) -> None:
    """为每个 SQLite 连接启用外键，并使用 WAL 支持单机并发读写。"""

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()


def run_migrations(
    database_url: str,
    database_path: Path,
    backup_directory: Path,
) -> None:
    """备份旧库、升级到打包的 Alembic head，失败时恢复原库。

    只有已存在且 revision 落后的数据库才备份；备份清理失败不阻断迁移。
    新库直接创建，不生成无意义的空库备份。
    """

    config = _migration_config(database_url)
    head = _head_revision(config)
    current = _current_revision(database_path)
    backup_path: Path | None = None
    if database_path.exists() and current != head:
        backup_path = _create_migration_backup(
            database_path,
            backup_directory,
            current,
        )
        _prune_migration_backups(backup_directory)

    try:
        command.upgrade(config, "head")
    except Exception as migration_error:
        if backup_path is not None:
            try:
                _restore_migration_backup(backup_path, database_path)
            except Exception as restore_error:
                raise RuntimeError(
                    "database migration failed and the pre-migration backup "
                    f"could not be restored: {restore_error}"
                ) from migration_error
        raise


class StorageDatabase:
    """持有单个 sidecar 的 SQLite 初始化、迁移和 session factory。"""

    def __init__(self, data_root: Path) -> None:
        database_directory = data_root.resolve() / "db"
        database_directory.mkdir(parents=True, exist_ok=True)
        self.database_path = database_directory / "application.db"
        self.database_url = f"sqlite:///{self.database_path.as_posix()}"
        self.backup_directory = data_root.resolve() / "backups" / "db"
        try:
            run_migrations(
                self.database_url,
                self.database_path,
                self.backup_directory,
            )
        except Exception as error:
            raise IngestFailure(
                "database_initialization_failed",
                f"could not initialize the application database: {error}",
                500,
            ) from error
        self.engine = create_engine(
            self.database_url,
            connect_args={"check_same_thread": False},
        )
        _configure_sqlite_connection(self.engine)
        self._session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
        )

    @contextmanager
    def session(self) -> Iterator[Session]:
        """创建一个调用方负责提交或回滚、退出时自动关闭的 session。"""

        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()
