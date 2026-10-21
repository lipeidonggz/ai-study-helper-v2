from __future__ import annotations

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.models import Base


class ProviderSettingRow(Base):
    """只保存模型设置的非敏感状态和最近一次连通性结果。"""

    __tablename__ = "provider_settings"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    last_test_status: Mapped[str | None] = mapped_column(String(16))
    last_test_at: Mapped[str | None] = mapped_column(String(40))
    last_test_message: Mapped[str | None] = mapped_column(Text)
    last_test_request_id: Mapped[str | None] = mapped_column(String(128))
    last_test_input_tokens: Mapped[int | None] = mapped_column(Integer)
