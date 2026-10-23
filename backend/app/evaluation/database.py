from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from app.evaluation.storage_models import EvalSchemaVersionRow

EVALUATION_SCHEMA_VERSION = 5


def _migrations_directory() -> Path:
    """定位开发环境或显式评测包中的独立 Alembic 目录。"""

    bundle_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and isinstance(bundle_root, str):
        return Path(bundle_root) / "eval_migrations"
    return Path(__file__).resolve().parents[2] / "eval_migrations"


def _migration_config(database_url: str) -> Config:
    migrations_directory = _migrations_directory()
    if not migrations_directory.is_dir():
        raise RuntimeError(f"evaluation migrations are missing: {migrations_directory}")
    config = Config()
    config.set_main_option("script_location", migrations_directory.as_posix())
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _run_migrations(database_url: str) -> None:
    """使用独立 revision 链把 eval.db 升级到唯一 head。"""

    config = _migration_config(database_url)
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"evaluation migrations must have exactly one head, got {heads}")
    command.upgrade(config, "head")


def _configure_sqlite_connection(engine: Engine) -> None:
    """为评测库启用外键与 WAL；评测并发只发生在独立 sidecar 内。"""

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


class EvaluationDatabase:
    """持有独立 ``eval.db`` 的 schema、session factory 与初始化边界。"""

    def __init__(self, data_root: Path) -> None:
        database_directory = data_root.resolve() / "db"
        database_directory.mkdir(parents=True, exist_ok=True)
        self.database_path = database_directory / "eval.db"
        self.database_url = f"sqlite:///{self.database_path.as_posix()}"
        self.engine = create_engine(
            self.database_url,
            connect_args={"check_same_thread": False},
        )
        _run_migrations(self.database_url)
        _configure_sqlite_connection(self.engine)
        self._ensure_schema_version()
        self._create_immutability_triggers()
        self._session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
        )

    def _ensure_schema_version(self) -> None:
        """把当前代码支持的首版 schema 幂等登记到独立评测库。"""

        with Session(self.engine) as session, session.begin():
            existing = session.get(EvalSchemaVersionRow, EVALUATION_SCHEMA_VERSION)
            if existing is None:
                session.add(EvalSchemaVersionRow(version=EVALUATION_SCHEMA_VERSION))

    def _create_immutability_triggers(self) -> None:
        """在 SQL 边界阻止内容 revision 被原地改写或删除。"""

        statements = (
            """
            CREATE TRIGGER IF NOT EXISTS eval_case_revision_content_immutable
            BEFORE UPDATE OF
                case_id, revision_number, question, target_bindings_json,
                distractor_metadata_json, n, k, execution_policy_json,
                category, source_ref, content_hash, change_note
            ON eval_case_revisions
            BEGIN
                SELECT RAISE(ABORT, 'case revision content is immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS eval_case_revision_delete_immutable
            BEFORE DELETE ON eval_case_revisions
            BEGIN
                SELECT RAISE(ABORT, 'case revisions cannot be deleted');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS eval_criterion_revision_content_immutable
            BEFORE UPDATE OF
                criterion_id, revision_number, rule_type, ordinal, title,
                rule_text, evaluator, evaluator_ref, source_ref, content_hash,
                change_note
            ON eval_criterion_revisions
            BEGIN
                SELECT RAISE(ABORT, 'criterion revision content is immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS eval_criterion_revision_delete_immutable
            BEFORE DELETE ON eval_criterion_revisions
            BEGIN
                SELECT RAISE(ABORT, 'criterion revisions cannot be deleted');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS eval_suite_revision_content_immutable
            BEFORE UPDATE OF
                suite_id, revision_number, name, description, scope_id,
                content_hash, change_note
            ON eval_suite_revisions
            BEGIN
                SELECT RAISE(ABORT, 'suite revision content is immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS eval_suite_revision_delete_immutable
            BEFORE DELETE ON eval_suite_revisions
            BEGIN
                SELECT RAISE(ABORT, 'suite revisions cannot be deleted');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS eval_suite_case_active_only
            BEFORE INSERT ON eval_suite_revision_cases
            BEGIN
                SELECT CASE
                    WHEN (
                        SELECT review_status
                        FROM eval_case_revisions
                        WHERE revision_id = NEW.case_revision_id
                    ) != 'active'
                    THEN RAISE(ABORT, 'suite may only reference active case revisions')
                END;
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS eval_execution_plan_content_immutable
            BEFORE UPDATE OF
                suite_revision_id, collection_snapshot_id, run_type,
                agent_version_set_json, model_config_json, run_policy_json,
                experiment_variable_json, preflight_result_json, mode
            ON eval_execution_plans
            BEGIN
                SELECT RAISE(ABORT, 'execution plan content is immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS eval_plan_case_content_immutable
            BEFORE UPDATE ON eval_plan_cases
            BEGIN
                SELECT RAISE(ABORT, 'execution plan cases are immutable');
            END
            """,
        )
        with self.engine.begin() as connection:
            for statement in statements:
                connection.exec_driver_sql(statement)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """创建由调用方控制事务的 session。"""

        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()

    def schema_version(self) -> int:
        """返回评测库已应用的 schema 版本。"""

        with self.session() as session:
            versions = session.scalars(
                select(EvalSchemaVersionRow.version).order_by(EvalSchemaVersionRow.version)
            ).all()
        return max(versions, default=0)
