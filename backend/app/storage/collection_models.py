from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.models import Base, utc_now


class ActiveCollectionPointerRow(Base):
    """单行 activity pointer；generation 提供发布 CAS。"""

    __tablename__ = "active_collection_pointer"

    pointer_id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_snapshot_id: Mapped[str | None] = mapped_column(String(64))
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class CollectionPublishRecordRow(Base):
    """发布意图和结果的持久审计记录。"""

    __tablename__ = "collection_publish_records"

    publish_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    from_snapshot_id: Mapped[str | None] = mapped_column(String(64))
    to_snapshot_id: Mapped[str | None] = mapped_column(String(64))
    expected_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
