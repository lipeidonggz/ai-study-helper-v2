from __future__ import annotations

import asyncio
import math
import random
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from app.indexing.embedding_inputs import EmbeddingInput
from app.indexing.models import EmbeddingUsage, UsagePreview
from app.ingestion.errors import IngestFailure
from app.providers.embeddings import (
    QWEN_MAX_BATCH_SIZE,
    EmbeddingBatchResult,
    EmbeddingProvider,
    EmbeddingProviderError,
)

RETRY_DELAYS_SECONDS = (1.0, 4.0)
MAX_RETRIES = 2
MAX_JITTER_RATIO = 0.2
MAX_RETRY_AFTER_SECONDS = 60.0
DEFAULT_BATCH_CONCURRENCY = 3
# 默认限制为 3 个批次在途，避免把供应商限流风险和 usage 对账复杂度放大。


@dataclass(slots=True)
class EmbeddingRunFailure(Exception):
    """失败构建保留已完成批次和失败尝试的 usage。"""

    code: str
    message: str
    http_status: int
    retryable: bool
    usage_preview: UsagePreview
    usage: EmbeddingUsage

    def __str__(self) -> str:
        """返回不包含凭证与正文的失败摘要。"""

        return self.message


@dataclass(slots=True)
class _BatchFailure(Exception):
    """内部记录一个失败批次已消耗的尝试与已知 usage。"""

    cause: Exception
    attempts: int
    ambiguous_retry_count: int
    input_tokens: int = 0
    request_id: str = ""


@dataclass(slots=True)
class EmbeddingRunResult:
    """一次 embedding 编排后的逐 block 向量与 usage。"""

    vectors_by_block_id: dict[str, list[float]]
    usage_preview: UsagePreview
    usage: EmbeddingUsage


class _Sleep(Protocol):
    def __call__(self, delay: float) -> Awaitable[None]: ...


class EmbeddingRunner:
    """按唯一输入分批、去重、重试并校验 embedding 响应。"""

    def __init__(
        self,
        provider: EmbeddingProvider,
        *,
        batch_concurrency: int = DEFAULT_BATCH_CONCURRENCY,
        sleep: _Sleep = asyncio.sleep,
        jitter_source: Callable[[], float] = random.random,
    ) -> None:
        if batch_concurrency <= 0:
            raise ValueError("batch_concurrency must be positive")
        self._provider = provider
        self._batch_concurrency = batch_concurrency
        self._sleep = sleep
        self._jitter_source = jitter_source

    async def embed(self, inputs: list[EmbeddingInput]) -> EmbeddingRunResult:
        """执行完整批次，并把向量严格绑定回原始 block 身份。"""

        if not inputs:
            raise IngestFailure(
                "embedding_input_empty",
                "no embedding inputs were produced for the block set",
                422,
            )
        unique_inputs = self._unique_inputs(inputs)
        batch_size = min(self._provider.batch_size, QWEN_MAX_BATCH_SIZE)
        if batch_size <= 0:
            raise IngestFailure(
                "embedding_batch_invalid",
                "embedding provider returned an invalid batch size",
                500,
            )
        batches = [
            unique_inputs[index : index + batch_size]
            for index in range(0, len(unique_inputs), batch_size)
        ]

        completed: dict[int, tuple[EmbeddingBatchResult, int, int]] = {}
        failures: dict[int, _BatchFailure] = {}
        pending = deque(range(len(batches)))
        stop_scheduling = asyncio.Event()

        async def worker() -> None:
            """工作协程从共享队列取批次，失败后阻止后续批次启动。"""

            while not stop_scheduling.is_set():
                try:
                    batch_index = pending.popleft()
                except IndexError:
                    return
                batch = batches[batch_index]
                try:
                    result, attempts, ambiguous = await self._embed_batch_with_retry(batch)
                except _BatchFailure as failure:
                    failures[batch_index] = failure
                    stop_scheduling.set()
                    return
                except Exception as error:
                    failures[batch_index] = _BatchFailure(
                        cause=error,
                        attempts=1,
                        ambiguous_retry_count=0,
                    )
                    stop_scheduling.set()
                    return
                try:
                    self._validate_batch_result(batch, result)
                except Exception as error:
                    failures[batch_index] = _BatchFailure(
                        cause=error,
                        attempts=attempts,
                        ambiguous_retry_count=ambiguous,
                        input_tokens=result.input_tokens,
                        request_id=result.request_id,
                    )
                    stop_scheduling.set()
                    return
                completed[batch_index] = (result, attempts, ambiguous)

        workers = [
            asyncio.create_task(worker()) for _ in range(min(self._batch_concurrency, len(batches)))
        ]
        await asyncio.gather(*workers)

        usage_preview = UsagePreview(
            block_count=len(inputs),
            unique_input_count=len(unique_inputs),
            batch_count=len(batches),
        )
        if failures:
            first_failure = failures[min(failures)]
            usage = self._failure_usage(completed, failures)
            code, message, http_status, retryable = self._failure_fields(first_failure.cause)
            raise EmbeddingRunFailure(
                code=code,
                message=message,
                http_status=http_status,
                retryable=retryable,
                usage_preview=usage_preview,
                usage=usage,
            ) from first_failure.cause

        vectors_by_hash: dict[str, list[float]] = {}
        request_count = 0
        input_tokens = 0
        ambiguous_retry_count = 0
        request_ids: list[str] = []
        for batch_index in range(len(batches)):
            result, attempts, ambiguous = completed[batch_index]
            request_count += attempts
            ambiguous_retry_count += ambiguous
            input_tokens += result.input_tokens
            if result.request_id:
                request_ids.append(result.request_id)
            for embedding_input, vector in zip(
                batches[batch_index],
                result.vectors,
                strict=True,
            ):
                vectors_by_hash[embedding_input.input_hash] = vector

        vectors_by_block_id = {
            embedding_input.block_id: vectors_by_hash[embedding_input.input_hash]
            for embedding_input in inputs
        }
        return EmbeddingRunResult(
            vectors_by_block_id=vectors_by_block_id,
            usage_preview=usage_preview,
            usage=EmbeddingUsage(
                request_count=request_count,
                input_tokens=input_tokens,
                ambiguous_retry_count=ambiguous_retry_count,
                request_ids=request_ids,
            ),
        )

    @staticmethod
    def _unique_inputs(inputs: list[EmbeddingInput]) -> list[EmbeddingInput]:
        by_hash: dict[str, EmbeddingInput] = {}
        order: deque[str] = deque()
        for embedding_input in inputs:
            if embedding_input.input_hash not in by_hash:
                by_hash[embedding_input.input_hash] = embedding_input
                order.append(embedding_input.input_hash)
        return [by_hash[input_hash] for input_hash in order]

    async def _embed_batch_with_retry(
        self,
        batch: list[EmbeddingInput],
    ) -> tuple[EmbeddingBatchResult, int, int]:
        attempts = 0
        ambiguous_retries = 0
        texts = [embedding_input.text for embedding_input in batch]
        while True:
            attempts += 1
            try:
                result = await self._provider.embed_documents(texts)
                return result, attempts, ambiguous_retries
            except EmbeddingProviderError as error:
                retries_used = attempts - 1
                if not error.retryable or retries_used >= MAX_RETRIES:
                    raise _BatchFailure(
                        cause=error,
                        attempts=attempts,
                        ambiguous_retry_count=ambiguous_retries,
                    ) from error
                if error.is_ambiguous:
                    ambiguous_retries += 1
                delay = self._retry_delay(error, retries_used)
                if delay > MAX_RETRY_AFTER_SECONDS:
                    # 同步验证入口不能因服务端长等待阻塞一小时；交给后续后台任务重试。
                    raise _BatchFailure(
                        cause=error,
                        attempts=attempts,
                        ambiguous_retry_count=ambiguous_retries,
                    ) from error
                await self._sleep(delay)

    def _retry_delay(self, error: EmbeddingProviderError, retry_index: int) -> float:
        """优先使用 Retry-After，否则采用约 1s／4s 抖动退避。"""

        if error.retry_after_seconds is not None:
            return max(0.0, error.retry_after_seconds)
        base = RETRY_DELAYS_SECONDS[retry_index]
        jitter = max(0.0, min(1.0, self._jitter_source())) * MAX_JITTER_RATIO
        return base * (1.0 + jitter)

    def _validate_batch_result(
        self,
        batch: list[EmbeddingInput],
        result: EmbeddingBatchResult,
    ) -> None:
        """拒绝数量、维度、非有限值和零向量等破坏索引完整性的响应。"""

        if len(result.vectors) != len(batch):
            raise IngestFailure(
                "embedding_response_invalid",
                "embedding response count does not match the request",
                502,
            )
        dimension = self._provider.profile.dimension
        for vector in result.vectors:
            if len(vector) != dimension:
                raise IngestFailure(
                    "embedding_dimension_mismatch",
                    f"embedding vector dimension must be {dimension}",
                    502,
                )
            if not all(math.isfinite(value) for value in vector):
                raise IngestFailure(
                    "embedding_vector_invalid",
                    "embedding vector contains a non-finite value",
                    502,
                )
            if not any(value != 0.0 for value in vector):
                raise IngestFailure(
                    "embedding_vector_invalid",
                    "embedding provider returned a zero vector",
                    502,
                )

    @staticmethod
    def _failure_usage(
        completed: dict[int, tuple[EmbeddingBatchResult, int, int]],
        failures: dict[int, _BatchFailure],
    ) -> EmbeddingUsage:
        """汇总已完成响应和失败尝试，避免并发失败后隐藏真实请求量。"""

        request_count = 0
        input_tokens = 0
        ambiguous_retry_count = 0
        request_ids: list[str] = []
        for batch_index in sorted(completed):
            result, attempts, ambiguous = completed[batch_index]
            request_count += attempts
            ambiguous_retry_count += ambiguous
            input_tokens += result.input_tokens
            if result.request_id:
                request_ids.append(result.request_id)
        for batch_index in sorted(failures):
            failure = failures[batch_index]
            request_count += failure.attempts
            ambiguous_retry_count += failure.ambiguous_retry_count
            input_tokens += failure.input_tokens
            if failure.request_id:
                request_ids.append(failure.request_id)
        return EmbeddingUsage(
            request_count=request_count,
            input_tokens=input_tokens,
            ambiguous_retry_count=ambiguous_retry_count,
            request_ids=request_ids,
        )

    @staticmethod
    def _failure_fields(cause: Exception) -> tuple[str, str, int, bool]:
        """把 runner 内已知失败转换为稳定错误字段。"""

        if isinstance(cause, EmbeddingProviderError):
            return (
                cause.code,
                cause.message,
                502 if cause.retryable else 409,
                cause.retryable,
            )
        if isinstance(cause, IngestFailure):
            return cause.code, cause.message, cause.http_status, cause.retryable
        return (
            "embedding_runner_failed",
            f"embedding runner failed with {type(cause).__name__}",
            500,
            False,
        )
