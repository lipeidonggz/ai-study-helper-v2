from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.tracing.models import TraceMode

DEEPSEEK_CONTEXT_WINDOW_TOKENS = 1_000_000
DEEPSEEK_MAX_OUTPUT_TOKENS = 384_000
DEFAULT_TOP_K = 5
DEFAULT_MAX_OUTPUT_TOKENS = 8_192
DEFAULT_MAX_CONTEXT_TOKENS = 100_000
DEFAULT_MAX_REQUEST_TOKENS = 400_000
DEFAULT_MAX_TOOL_CALLS = 8


def _default_trace_mode() -> TraceMode:
    value = os.environ.get("ASH_TRACE_MODE", "off")
    return value if value in {"off", "metadata", "full"} else "off"  # type: ignore[return-value]


DEFAULT_TRACE_MODE = _default_trace_mode()


class SetEmbeddingKeyRequest(BaseModel):
    """设置 embedding Key；请求体不会出现在响应中。"""

    api_key: str = Field(min_length=1)


class SetChatKeyRequest(BaseModel):
    """设置 DeepSeek 对话 Key；请求体不会出现在响应中。"""

    api_key: str = Field(min_length=1)


class ProviderTestResult(BaseModel):
    """一次模型连通性检查的结果。"""

    status: Literal["success", "failed"]
    model_id: str
    dimension: int
    request_id: str | None = None
    input_tokens: int = 0
    message: str | None = None
    tested_at: str


class ChatProviderTestResult(BaseModel):
    """一次 DeepSeek 对话连通性检查的结果。"""

    status: Literal["success", "failed"]
    model_id: str
    request_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    message: str | None = None
    tested_at: str


class ModelSettingsResponse(BaseModel):
    """设置页展示的状态，不包含任何 Key 明文。"""

    embedding_configured: bool
    embedding_model_id: str
    embedding_dimension: int
    embedding_last_test: ProviderTestResult | None = None
    chat_configured: bool = False
    chat_model_id: str = "deepseek-flash"


class RuntimeSettingsPayload(BaseModel):
    """用户可调的检索和 Agent 运行参数。"""

    top_k: int = Field(default=DEFAULT_TOP_K, ge=3, le=10)
    max_output_tokens: int = Field(
        default=DEFAULT_MAX_OUTPUT_TOKENS,
        ge=2_048,
        le=DEEPSEEK_MAX_OUTPUT_TOKENS,
    )
    max_context_tokens: int = Field(
        default=DEFAULT_MAX_CONTEXT_TOKENS,
        ge=10_000,
        le=DEEPSEEK_CONTEXT_WINDOW_TOKENS,
    )
    max_request_tokens: int = Field(default=DEFAULT_MAX_REQUEST_TOKENS, ge=10_000)
    max_tool_calls: int = Field(default=DEFAULT_MAX_TOOL_CALLS, ge=1, le=20)
    trace_mode: TraceMode = DEFAULT_TRACE_MODE

    @model_validator(mode="after")
    def validate_limits(self) -> RuntimeSettingsPayload:
        """校验跨字段预算关系，避免保存必然不可执行的配置。"""

        if self.max_context_tokens + self.max_output_tokens > DEEPSEEK_CONTEXT_WINDOW_TOKENS:
            raise ValueError("max_context_tokens + max_output_tokens must not exceed 1000000")
        if self.max_request_tokens < self.max_context_tokens:
            raise ValueError("max_request_tokens must be at least max_context_tokens")
        return self


class RuntimeSettingsResponse(RuntimeSettingsPayload):
    """包含最近更新时间的运行参数响应。"""

    updated_at: str | None = None
