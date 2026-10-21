from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import requests
from dashscope import TextEmbedding

from app.providers.embeddings import (
    QWEN_EMBEDDING_DIMENSION,
    DashScopeEmbeddingProvider,
    EmbeddingProviderError,
)


def test_dashscope_provider_sends_document_contract_and_orders_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_call(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            status_code=200,
            request_id="request-123",
            usage={"input_tokens": 42},
            output={
                "embeddings": [
                    {"text_index": 1, "embedding": [0.0, 2.0]},
                    {"text_index": 0, "embedding": [1.0, 0.0]},
                ]
            },
        )

    monkeypatch.setattr(TextEmbedding, "call", fake_call)
    provider = DashScopeEmbeddingProvider("test-key")

    result = asyncio.run(provider.embed_documents(["first", "second"]))

    assert captured["model"] == "qwen3.7-text-embedding"
    assert captured["text_type"] == "document"
    assert captured["dimension"] == QWEN_EMBEDDING_DIMENSION
    assert captured["output_type"] == "dense"
    assert captured["request_timeout"] == 60
    assert result.vectors == [[1.0, 0.0], [0.0, 2.0]]
    assert result.request_id == "request-123"
    assert result.input_tokens == 42


@pytest.mark.parametrize(
    ("status_code", "expected_code", "retryable"),
    [
        (401, "embedding_authentication_failed", False),
        (429, "embedding_rate_limited", True),
        (500, "embedding_upstream_unavailable", True),
        (422, "embedding_request_rejected", False),
    ],
)
def test_dashscope_provider_classifies_status_codes(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_code: str,
    retryable: bool,
) -> None:
    monkeypatch.setattr(
        TextEmbedding,
        "call",
        lambda **_kwargs: SimpleNamespace(status_code=status_code),
    )

    try:
        asyncio.run(DashScopeEmbeddingProvider("test-key").embed_documents(["text"]))
    except EmbeddingProviderError as error:
        assert error.code == expected_code
        assert error.retryable is retryable
    else:
        raise AssertionError("non-200 provider responses must fail")


def test_dashscope_transport_failure_is_ambiguous_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(**_kwargs):
        raise requests.Timeout("timed out")

    monkeypatch.setattr(TextEmbedding, "call", fail)

    try:
        asyncio.run(DashScopeEmbeddingProvider("test-key").embed_documents(["text"]))
    except EmbeddingProviderError as error:
        assert error.code == "embedding_upstream_unavailable"
        assert error.retryable is True
        assert error.is_ambiguous is True
    else:
        raise AssertionError("transport errors must be classified")


def test_dashscope_rate_limit_uses_retry_after_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        TextEmbedding,
        "call",
        lambda **_kwargs: SimpleNamespace(
            status_code=429,
            headers={"Retry-After": "7"},
        ),
    )

    try:
        asyncio.run(DashScopeEmbeddingProvider("test-key").embed_documents(["text"]))
    except EmbeddingProviderError as error:
        assert error.code == "embedding_rate_limited"
        assert error.retry_after_seconds == 7.0
    else:
        raise AssertionError("rate limits must expose Retry-After")


def test_dashscope_provider_reports_missing_key_only_when_embedding() -> None:
    provider = DashScopeEmbeddingProvider(None)

    assert provider.profile.model_id == "qwen3.7-text-embedding"
    try:
        asyncio.run(provider.embed_documents(["text"]))
    except EmbeddingProviderError as error:
        assert error.code == "embedding_api_key_missing"
        assert error.retryable is False
    else:
        raise AssertionError("missing embedding key must fail before a provider request")
