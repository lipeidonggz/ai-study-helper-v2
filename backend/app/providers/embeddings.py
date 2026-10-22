from __future__ import annotations

import asyncio
import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol

import requests
from dashscope import TextEmbedding

from app.indexing.models import EmbeddingProfile

QWEN_EMBEDDING_MODEL_ID = "qwen3.7-text-embedding"
QWEN_EMBEDDING_MODEL_VERSION = "provider-managed"
QWEN_EMBEDDING_DIMENSION = 1024
QWEN_MAX_BATCH_SIZE = 20
QWEN_REQUEST_TIMEOUT_SECONDS = 60

QWEN_EMBEDDING_PROFILE = EmbeddingProfile(
    provider="dashscope",
    model_id=QWEN_EMBEDDING_MODEL_ID,
    model_version=QWEN_EMBEDDING_MODEL_VERSION,
    dimension=QWEN_EMBEDDING_DIMENSION,
    output_type="dense",
    text_type="document",
)


@dataclass(slots=True)
class EmbeddingBatchResult:
    """一个 embedding 批次的向量、request ID 和供应商 token usage。"""

    vectors: list[list[float]]
    request_id: str
    input_tokens: int


@dataclass(slots=True)
class EmbeddingQueryResult:
    """一次 query embedding 的向量、request ID 和供应商 token usage。"""

    vector: list[float]
    request_id: str
    input_tokens: int


@dataclass(slots=True)
class EmbeddingProviderError(Exception):
    """可分类的 embedding 上游失败，供构建器决定是否重试。"""

    code: str
    message: str
    retryable: bool
    is_ambiguous: bool
    retry_after_seconds: float | None = None

    def __str__(self) -> str:
        """返回不包含凭证与正文的上游错误摘要。"""

        return self.message


class EmbeddingProvider(Protocol):
    """单批 document embedding 的最小供应商协议。"""

    @property
    def profile(self) -> EmbeddingProfile:
        """返回构建必须固定记录的模型与向量契约。"""
        ...

    @property
    def batch_size(self) -> int:
        """返回供应商允许的单批最大输入数。"""
        ...

    async def embed_documents(self, texts: list[str]) -> EmbeddingBatchResult:
        """为一个非空批次生成与输入顺序一致的 dense 向量。"""
        ...

    async def embed_query(self, query: str) -> EmbeddingQueryResult:
        """按供应商 query 输入类型生成一个 dense 查询向量。"""
        ...


class FakeEmbeddingProvider:
    """用于自动测试的确定性 1024 维 embedding provider。"""

    def __init__(self, dimension: int = QWEN_EMBEDDING_DIMENSION) -> None:
        self._profile = EmbeddingProfile(
            provider="fake",
            model_id="deterministic-test-embedding",
            model_version="1",
            dimension=dimension,
            output_type="dense",
            text_type="document",
        )
        self.calls: list[tuple[str, ...]] = []
        self.query_calls: list[str] = []

    @property
    def profile(self) -> EmbeddingProfile:
        """返回 fake provider 的固定 profile。"""

        return self._profile

    @property
    def batch_size(self) -> int:
        """返回与 Qwen 相同的最大批大小。"""

        return QWEN_MAX_BATCH_SIZE

    async def embed_documents(self, texts: list[str]) -> EmbeddingBatchResult:
        """由输入 hash 确定性地生成单位向量，并记录调用批次。"""

        if not texts or len(texts) > self.batch_size:
            raise EmbeddingProviderError(
                code="embedding_batch_invalid",
                message="embedding batch must contain between 1 and 20 inputs",
                retryable=False,
                is_ambiguous=False,
            )
        self.calls.append(tuple(texts))
        vectors = [self._vector_for(text) for text in texts]
        return EmbeddingBatchResult(
            vectors=vectors,
            request_id=f"fake-{len(self.calls):06d}",
            input_tokens=sum(max(1, len(text) // 4) for text in texts),
        )

    async def embed_query(self, query: str) -> EmbeddingQueryResult:
        """由查询文本确定性地生成一个 query 向量。"""

        normalized = query.strip()
        if not normalized:
            raise EmbeddingProviderError(
                code="embedding_query_invalid",
                message="embedding query must not be empty",
                retryable=False,
                is_ambiguous=False,
            )
        self.query_calls.append(normalized)
        return EmbeddingQueryResult(
            vector=self._vector_for(normalized),
            request_id=f"fake-query-{len(self.query_calls):06d}",
            input_tokens=max(1, len(normalized) // 4),
        )

    def _vector_for(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values: list[float] = []
        counter = 0
        while len(values) < self.profile.dimension:
            block = hashlib.sha256(digest + counter.to_bytes(4, "big")).digest()
            values.extend(byte / 127.5 - 1.0 for byte in block)
            counter += 1
        selected = values[: self.profile.dimension]
        norm = math.sqrt(sum(value * value for value in selected))
        if norm == 0:
            selected[0] = 1.0
            norm = 1.0
        return [value / norm for value in selected]


class DashScopeEmbeddingProvider:
    """通过 DashScope Python SDK 调用 Qwen document embedding。"""

    def __init__(
        self,
        api_key: str | None,
        *,
        profile: EmbeddingProfile = QWEN_EMBEDDING_PROFILE,
    ) -> None:
        self._api_key = api_key or ""
        self._profile = profile

    @property
    def profile(self) -> EmbeddingProfile:
        """返回 Qwen embedding profile。"""

        return self._profile

    @property
    def batch_size(self) -> int:
        """返回 Qwen 单批 20 条上限。"""

        return QWEN_MAX_BATCH_SIZE

    async def embed_documents(self, texts: list[str]) -> EmbeddingBatchResult:
        """调用一次 Qwen 同步 SDK，并把协议失败映射为稳定领域错误。"""

        return await self._embed(texts, text_type=self.profile.text_type)

    async def embed_query(self, query: str) -> EmbeddingQueryResult:
        """以 Qwen query 输入类型生成一个检索查询向量。"""

        normalized = query.strip()
        if not normalized:
            raise EmbeddingProviderError(
                code="embedding_query_invalid",
                message="embedding query must not be empty",
                retryable=False,
                is_ambiguous=False,
            )
        result = await self._embed([normalized], text_type="query")
        if len(result.vectors) != 1:
            raise _invalid_provider_response("query embedding must return exactly one vector")
        return EmbeddingQueryResult(
            vector=result.vectors[0],
            request_id=result.request_id,
            input_tokens=result.input_tokens,
        )

    async def _embed(self, texts: list[str], *, text_type: str) -> EmbeddingBatchResult:
        """以指定 query／document 输入类型调用一次 Qwen SDK。"""

        if not self._api_key.strip():
            raise EmbeddingProviderError(
                code="embedding_api_key_missing",
                message="embedding API key is not configured",
                retryable=False,
                is_ambiguous=False,
            )
        if not texts or len(texts) > self.batch_size:
            raise EmbeddingProviderError(
                code="embedding_batch_invalid",
                message="embedding batch must contain between 1 and 20 inputs",
                retryable=False,
                is_ambiguous=False,
            )
        try:
            response = await asyncio.to_thread(
                TextEmbedding.call,
                model=self.profile.model_id,
                input=texts,
                api_key=self._api_key,
                text_type=text_type,
                dimension=self.profile.dimension,
                output_type=self.profile.output_type,
                request_timeout=QWEN_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            raise EmbeddingProviderError(
                code="embedding_upstream_unavailable",
                message=f"embedding request failed: {type(error).__name__}",
                retryable=True,
                is_ambiguous=True,
            ) from error

        status_code = int(getattr(response, "status_code", 0))
        if status_code != 200:
            raise _provider_status_error(
                status_code,
                getattr(response, "headers", None),
            )

        output = getattr(response, "output", None)
        if not isinstance(output, dict):
            raise _invalid_provider_response("embedding response has no output object")
        raw_embeddings = output.get("embeddings")
        if not isinstance(raw_embeddings, list) or len(raw_embeddings) != len(texts):
            raise _invalid_provider_response("embedding response count does not match the request")
        ordered = sorted(
            raw_embeddings,
            key=lambda item: int(item.get("text_index", 0)) if isinstance(item, dict) else -1,
        )
        vectors: list[list[float]] = []
        for item in ordered:
            if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                raise _invalid_provider_response("embedding item is malformed")
            vector = item["embedding"]
            if not all(isinstance(value, int | float) for value in vector):
                raise _invalid_provider_response("embedding vector contains non-numeric values")
            vectors.append([float(value) for value in vector])

        usage = getattr(response, "usage", None)
        input_tokens = 0
        if isinstance(usage, dict):
            raw_tokens = usage.get("input_tokens", usage.get("total_tokens", 0))
            if isinstance(raw_tokens, int | float):
                input_tokens = int(raw_tokens)
        request_id = str(getattr(response, "request_id", "") or "")
        return EmbeddingBatchResult(
            vectors=vectors,
            request_id=request_id,
            input_tokens=input_tokens,
        )


def _provider_status_error(
    status_code: int,
    headers: object = None,
) -> EmbeddingProviderError:
    """把 HTTP 状态映射为可重试或终止的稳定错误。"""

    retry_after_seconds = _retry_after_seconds(headers)
    if status_code in {401, 403}:
        return EmbeddingProviderError(
            code="embedding_authentication_failed",
            message=f"embedding provider rejected credentials with HTTP {status_code}",
            retryable=False,
            is_ambiguous=False,
        )
    if status_code == 429:
        return EmbeddingProviderError(
            code="embedding_rate_limited",
            message="embedding provider rate limited the request",
            retryable=True,
            is_ambiguous=False,
            retry_after_seconds=retry_after_seconds,
        )
    if status_code in {408, 409} or status_code >= 500:
        return EmbeddingProviderError(
            code="embedding_upstream_unavailable",
            message=f"embedding provider returned HTTP {status_code}",
            retryable=True,
            is_ambiguous=status_code in {408, 409},
            retry_after_seconds=retry_after_seconds,
        )
    return EmbeddingProviderError(
        code="embedding_request_rejected",
        message=f"embedding provider returned HTTP {status_code}",
        retryable=False,
        is_ambiguous=False,
    )


def _invalid_provider_response(message: str) -> EmbeddingProviderError:
    return EmbeddingProviderError(
        code="embedding_response_invalid",
        message=message,
        retryable=False,
        is_ambiguous=False,
    )


def _retry_after_seconds(headers: object) -> float | None:
    """解析标准 Retry-After 秒数或 HTTP 日期，无法识别时返回 None。"""

    if not isinstance(headers, dict):
        return None
    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    raw_value = normalized.get("retry-after")
    if raw_value is None:
        return None
    try:
        return max(0.0, float(raw_value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(raw_value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
