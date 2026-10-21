from __future__ import annotations

import asyncio
import hashlib

from app.indexing.embedding_inputs import build_embedding_inputs
from app.indexing.runner import EmbeddingRunFailure, EmbeddingRunner
from app.ingestion.models import BlockRecord, TableContext
from app.providers.embeddings import (
    EmbeddingBatchResult,
    EmbeddingProviderError,
    FakeEmbeddingProvider,
)


def _block(
    text: str,
    *,
    block_index: int = 1,
    section_path: list[str] | None = None,
    table_context: TableContext | None = None,
    embedding_hints: dict[str, str] | None = None,
) -> BlockRecord:
    return BlockRecord(
        block_id=f"doc_test#v0001#c1#b{block_index:05d}",
        section_path=section_path or ["安全边界"],
        block_index=block_index,
        language="zh",
        text=text,
        char_start=0,
        char_end=len(text),
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        source_node_ordinals=[block_index],
        table_context=table_context,
        embedding_hints=embedding_hints or {},
        context_token_estimate=10,
        estimator_version="cjk-ascii-v1",
    )


def test_embedding_input_is_versioned_and_preserves_block_text() -> None:
    block = _block(
        "只读数据库，禁止写入工作区外文件。",
        section_path=["隔离", "数据库"],
        table_context=TableContext(
            caption="权限表",
            headers=["机制", "边界"],
            group_index=2,
            group_count=3,
        ),
        embedding_hints={"code_language": "python"},
    )

    policy, inputs = build_embedding_inputs("Agent 隔离设计", [block])

    assert policy.version == "embedding-input-v1"
    assert len(policy.template_hash) == 64
    assert len(inputs) == 1
    assert inputs[0].block_id == block.block_id
    assert inputs[0].text.endswith(block.text)
    assert "文档标题：Agent 隔离设计" in inputs[0].text
    assert "章节：隔离 > 数据库" in inputs[0].text
    assert "表格：权限表" in inputs[0].text
    assert "表头：机制 | 边界" in inputs[0].text
    assert "代码语言：python" in inputs[0].text
    assert inputs[0].input_hash == hashlib.sha256(inputs[0].text.encode("utf-8")).hexdigest()


def test_embedding_input_rejects_empty_visible_text() -> None:
    block = _block("\u200b\u200c\u2060")

    try:
        build_embedding_inputs("空内容", [block])
    except Exception as error:
        assert getattr(error, "code", None) == "embedding_input_empty"
    else:
        raise AssertionError("invisible block text must be rejected")


def test_embedding_runner_batches_unique_inputs_without_exceeding_twenty() -> None:
    blocks = [_block(f"第 {index} 个唯一段落", block_index=index) for index in range(1, 2002)]
    _, inputs = build_embedding_inputs("批量测试", blocks)
    provider = FakeEmbeddingProvider()

    result = asyncio.run(EmbeddingRunner(provider).embed(inputs))

    assert result.usage_preview.block_count == 2001
    assert result.usage_preview.batch_count == 101
    assert len(provider.calls) == 101
    assert all(1 <= len(call) <= 20 for call in provider.calls)
    assert result.usage.request_count == 101
    assert len(result.vectors_by_block_id) == 2001


def test_embedding_runner_reuses_duplicate_input_hash() -> None:
    first = _block("完全相同的正文", block_index=1)
    second = _block("完全相同的正文", block_index=2)
    _, inputs = build_embedding_inputs("去重测试", [first, second])
    provider = FakeEmbeddingProvider()

    result = asyncio.run(EmbeddingRunner(provider).embed(inputs))

    assert inputs[0].input_hash == inputs[1].input_hash
    assert provider.calls == [("文档标题：去重测试\n章节：安全边界\n正文：\n完全相同的正文",)]
    assert result.vectors_by_block_id[first.block_id] == result.vectors_by_block_id[second.block_id]


class _RetryingProvider(FakeEmbeddingProvider):
    def __init__(self, failures: list[EmbeddingProviderError]) -> None:
        super().__init__()
        self._failures = failures
        self.delays: list[float] = []

    async def embed_documents(self, texts: list[str]) -> EmbeddingBatchResult:
        if self._failures:
            raise self._failures.pop(0)
        return await super().embed_documents(texts)


def test_embedding_runner_retries_retryable_errors_twice() -> None:
    _, inputs = build_embedding_inputs("重试测试", [_block("需要重试的正文")])
    provider = _RetryingProvider(
        [
            EmbeddingProviderError(
                code="embedding_rate_limited",
                message="rate limited",
                retryable=True,
                is_ambiguous=False,
            ),
            EmbeddingProviderError(
                code="embedding_upstream_unavailable",
                message="connection lost",
                retryable=True,
                is_ambiguous=True,
                retry_after_seconds=3.0,
            ),
        ]
    )

    async def record_sleep(delay: float) -> None:
        provider.delays.append(delay)

    result = asyncio.run(EmbeddingRunner(provider, sleep=record_sleep).embed(inputs))

    assert len(provider.delays) == 2
    assert all(delay >= 0 for delay in provider.delays)
    assert provider.delays[1] == 3.0
    assert result.usage.request_count == 3
    assert result.usage.ambiguous_retry_count == 1


def test_embedding_runner_stops_after_two_retries() -> None:
    _, inputs = build_embedding_inputs("失败测试", [_block("持续失败")])
    failure = EmbeddingProviderError(
        code="embedding_upstream_unavailable",
        message="unavailable",
        retryable=True,
        is_ambiguous=False,
    )
    provider = _RetryingProvider([failure, failure, failure])
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    try:
        asyncio.run(EmbeddingRunner(provider, sleep=record_sleep).embed(inputs))
    except EmbeddingRunFailure as error:
        assert error.code == "embedding_upstream_unavailable"
        assert error.usage.request_count == 3
    else:
        raise AssertionError("retry exhaustion must fail the build")
    assert len(sleeps) == 2


def test_embedding_runner_fails_fast_for_retry_after_over_sixty_seconds() -> None:
    _, inputs = build_embedding_inputs("长等待", [_block("不得休眠一小时")])
    provider = _RetryingProvider(
        [
            EmbeddingProviderError(
                code="embedding_rate_limited",
                message="rate limited",
                retryable=True,
                is_ambiguous=False,
                retry_after_seconds=3600.0,
            )
        ]
    )
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    try:
        asyncio.run(EmbeddingRunner(provider, sleep=record_sleep).embed(inputs))
    except EmbeddingRunFailure as error:
        assert error.code == "embedding_rate_limited"
        assert error.retryable is True
        assert error.usage.request_count == 1
    else:
        raise AssertionError("long Retry-After must fail without sleeping")

    assert sleeps == []


class _ZeroVectorProvider(FakeEmbeddingProvider):
    async def embed_documents(self, texts: list[str]) -> EmbeddingBatchResult:
        result = await super().embed_documents(texts)
        return EmbeddingBatchResult(
            vectors=[[0.0] * self.profile.dimension for _ in texts],
            request_id=result.request_id,
            input_tokens=result.input_tokens,
        )


def test_embedding_runner_rejects_zero_vectors() -> None:
    _, inputs = build_embedding_inputs("零向量", [_block("不能降级成零向量")])

    try:
        asyncio.run(EmbeddingRunner(_ZeroVectorProvider()).embed(inputs))
    except Exception as error:
        assert getattr(error, "code", None) == "embedding_vector_invalid"
    else:
        raise AssertionError("zero vectors must not be accepted")


class _ConcurrencyTrackingProvider(FakeEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.max_active = 0

    async def embed_documents(self, texts: list[str]) -> EmbeddingBatchResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            return await super().embed_documents(texts)
        finally:
            self.active -= 1


def test_embedding_runner_limits_batch_concurrency_to_three() -> None:
    blocks = [_block(f"并发段落 {index}", block_index=index) for index in range(1, 121)]
    _, inputs = build_embedding_inputs("并发测试", blocks)
    provider = _ConcurrencyTrackingProvider()

    asyncio.run(EmbeddingRunner(provider).embed(inputs))

    assert provider.max_active == 3
    assert len(provider.calls) == 6


class _FailingConcurrentProvider(FakeEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = 0

    async def embed_documents(self, texts: list[str]) -> EmbeddingBatchResult:
        self.started += 1
        start_index = self.started
        await asyncio.sleep(0.01)
        if start_index == 1:
            raise EmbeddingProviderError(
                code="embedding_authentication_failed",
                message="provider rejected credentials",
                retryable=False,
                is_ambiguous=False,
            )
        await asyncio.sleep(0.01)
        return await super().embed_documents(texts)


def test_embedding_failure_stops_queued_batches_and_keeps_usage() -> None:
    blocks = [_block(f"失败并发段落 {index}", block_index=index) for index in range(1, 201)]
    _, inputs = build_embedding_inputs("失败并发测试", blocks)
    provider = _FailingConcurrentProvider()

    try:
        asyncio.run(EmbeddingRunner(provider).embed(inputs))
    except EmbeddingRunFailure as error:
        assert error.code == "embedding_authentication_failed"
        assert error.usage.request_count == 3
    else:
        raise AssertionError("a terminal batch failure must fail the run")

    assert provider.started == 3
