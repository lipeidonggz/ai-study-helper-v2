from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command

from app.storage.database import (
    MAX_MIGRATION_BACKUPS,
    StorageDatabase,
    _create_migration_backup,
    _migration_config,
    _prune_migration_backups,
    run_migrations,
)


def test_new_database_migrates_without_creating_backup(tmp_path: Path) -> None:
    database = StorageDatabase(tmp_path)

    assert database.database_path.is_file()
    assert list(database.backup_directory.glob("application-before-*.db")) == []
    database.engine.dispose()


def test_migration_creates_backup_before_upgrading_old_schema(tmp_path: Path) -> None:
    database = StorageDatabase(tmp_path)
    config = _migration_config(database.database_url)
    command.downgrade(config, "base")
    database.engine.dispose()

    upgraded = StorageDatabase(tmp_path)

    backups = list(upgraded.backup_directory.glob("application-before-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(upgraded.database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "documents" in tables
    upgraded.engine.dispose()


def test_migration_failure_restores_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database_path = tmp_path / "db" / "application.db"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES ('before')")

    database_url = f"sqlite:///{database_path.as_posix()}"
    backup_directory = tmp_path / "backups" / "db"

    def fail_upgrade(_config, _revision: str) -> None:
        with sqlite3.connect(database_path) as connection:
            connection.execute("CREATE TABLE broken_migration (value TEXT)")
        raise RuntimeError("simulated migration failure")

    monkeypatch.setattr(command, "upgrade", fail_upgrade)

    with pytest.raises(RuntimeError, match="simulated migration failure"):
        run_migrations(database_url, database_path, backup_directory)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchone() == ("before",)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "broken_migration" not in tables
    assert len(list(backup_directory.glob("application-before-*.db"))) == 1


def test_backup_retention_keeps_latest_three_and_ignores_other_files(
    tmp_path: Path,
) -> None:
    backup_directory = tmp_path / "backups" / "db"
    backup_directory.mkdir(parents=True)
    timestamps = [
        "20261021T010000000000Z",
        "20261021T020000000000Z",
        "20261021T030000000000Z",
        "20261021T040000000000Z",
        "20261021T050000000000Z",
    ]
    for timestamp in timestamps:
        (backup_directory / f"application-before-0001-{timestamp}.db").write_bytes(b"db")
    unrelated = backup_directory / "user-backup.db"
    unrelated.write_bytes(b"keep")

    _prune_migration_backups(backup_directory)

    remaining = sorted(path.name for path in backup_directory.glob("application-before-*.db"))
    assert len(remaining) == MAX_MIGRATION_BACKUPS
    assert remaining == [f"application-before-0001-{timestamp}.db" for timestamp in timestamps[2:]]
    assert unrelated.read_bytes() == b"keep"


def test_migration_backup_uses_sqlite_snapshot_with_wal_data(tmp_path: Path) -> None:
    database_path = tmp_path / "db" / "application.db"
    database_path.parent.mkdir(parents=True)
    source = sqlite3.connect(database_path)
    try:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("PRAGMA wal_autocheckpoint=0")
        source.execute("CREATE TABLE wal_data (value TEXT NOT NULL)")
        source.execute("INSERT INTO wal_data VALUES ('not-checkpointed')")
        source.commit()

        backup_path = _create_migration_backup(
            database_path,
            tmp_path / "backups" / "db",
            "base",
        )
    finally:
        source.close()

    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("SELECT value FROM wal_data").fetchone() == ("not-checkpointed",)


def test_backup_prune_failure_does_not_block_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_directory = tmp_path / "backups" / "db"
    backup_directory.mkdir(parents=True)
    for hour in range(1, 5):
        (backup_directory / f"application-before-0001-20261021T0{hour}0000000000Z.db").write_bytes(
            b"db"
        )

    def fail_unlink(_path: Path, *args, **kwargs) -> None:
        raise PermissionError("simulated delete failure")

    monkeypatch.setattr(Path, "unlink", fail_unlink)

    _prune_migration_backups(backup_directory)

    assert len(list(backup_directory.glob("application-before-*.db"))) == 4
