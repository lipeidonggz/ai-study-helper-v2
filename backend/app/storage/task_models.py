from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.models import Base, utc_now


class IngestionTaskRow(Base):
    """后台批量入库任务及其可恢复进度。"""

    __tablename__ = "ingestion_tasks"

    task_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    sources_json: Mapped[str] = mapped_column(Text, nullable=False)
    total_sources: Mapped[int] = mapped_column(Integer, nullable=False)
    next_source_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_sources: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_sources: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    successful_job_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    failures_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    preview_job_id: Mapped[str | None] = mapped_column(String(32))
    snapshot_id: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
