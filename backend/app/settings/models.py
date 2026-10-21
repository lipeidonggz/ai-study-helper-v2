from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SetEmbeddingKeyRequest(BaseModel):
    """设置 embedding Key；请求体不会出现在响应中。"""

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


class ModelSettingsResponse(BaseModel):
    """设置页展示的状态，不包含任何 Key 明文。"""

    embedding_configured: bool
    embedding_model_id: str
    embedding_dimension: int
    embedding_last_test: ProviderTestResult | None = None
    chat_configured: bool = False
