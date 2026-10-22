from __future__ import annotations

from typing import cast

from app.settings.models import (
    DEFAULT_TRACE_MODE,
    RuntimeSettingsPayload,
    RuntimeSettingsResponse,
)
from app.storage.database import StorageDatabase
from app.storage.models import utc_iso
from app.storage.settings_models import RuntimeSettingsRow
from app.tracing.models import TraceMode


class RuntimeSettingsService:
    """持久化用户可调的检索与 Agent 运行参数。"""

    def __init__(self, database: StorageDatabase) -> None:
        self._database = database

    def get(self) -> RuntimeSettingsResponse:
        """读取设置；首次使用返回设计默认值。"""

        with self._database.session() as session:
            row = session.get(RuntimeSettingsRow, 1)
            if row is None:
                return RuntimeSettingsResponse()
            return self._response(row)

    def save(self, settings: RuntimeSettingsPayload) -> RuntimeSettingsResponse:
        """原子写入单行设置，并返回规范化结果。"""

        with self._database.session() as session, session.begin():
            row = session.get(RuntimeSettingsRow, 1)
            if row is None:
                row = RuntimeSettingsRow(
                    settings_id=1,
                    top_k=settings.top_k,
                    max_output_tokens=settings.max_output_tokens,
                    max_context_tokens=settings.max_context_tokens,
                    max_request_tokens=settings.max_request_tokens,
                    max_tool_calls=settings.max_tool_calls,
                    trace_mode=settings.trace_mode,
                )
                session.add(row)
            else:
                row.top_k = settings.top_k
                row.max_output_tokens = settings.max_output_tokens
                row.max_context_tokens = settings.max_context_tokens
                row.max_request_tokens = settings.max_request_tokens
                row.max_tool_calls = settings.max_tool_calls
                row.trace_mode = settings.trace_mode
            session.flush()
            return self._response(row)

    @staticmethod
    def _response(row: RuntimeSettingsRow) -> RuntimeSettingsResponse:
        return RuntimeSettingsResponse(
            top_k=row.top_k,
            max_output_tokens=row.max_output_tokens,
            max_context_tokens=row.max_context_tokens,
            max_request_tokens=row.max_request_tokens,
            max_tool_calls=row.max_tool_calls,
            trace_mode=cast(
                TraceMode,
                (
                    row.trace_mode
                    if row.trace_mode in {"off", "metadata", "full"}
                    else DEFAULT_TRACE_MODE
                ),
            ),
            updated_at=utc_iso(row.updated_at),
        )
