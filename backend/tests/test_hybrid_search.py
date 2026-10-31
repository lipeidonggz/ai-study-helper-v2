from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app.main import create_app
from app.providers.embeddings import FakeEmbeddingProvider
from app.retrieval.hybrid import reciprocal_rank_fusion
from app.retrieval.service import RetrievalFailure, RetrievalReadService

TOKEN = "hybrid-search-test-token"


def _html(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body><article><h1>{title}</h1>"
        "<h2>Boundary</h2><p>Alpha 根章节包含 MCP 和沙箱边界说明。</p>"
        "<h3>Details</h3><p>补充说明完整的隔离边界和本地进程限制。</p>"
        "<h2>Other</h2>"
        f"<p>{body}</p>"
        "</article></body></html>"
    ).encode()


async def _wait_for_terminal(
    client: httpx.AsyncClient,
    task_id: str,
    *,
    timeout_seconds: float = 15,
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(
            f"/api/tasks/{task_id}",
            headers={"x-launch-token": TOKEN},
        )
        response.raise_for_status()
        task = response.json()
        if task["status"] in {"succeeded", "failed", "canceled", "interrupted"}:
            return task
        await asyncio.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach a terminal state")


def _ingest(data_root: Path, paths: list[Path]) -> None:
    async def run() -> None:
        app = create_app(
            TOKEN,
            data_root=data_root,
            embedding_provider_factory=FakeEmbeddingProvider,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            created = await client.post(
                "/api/tasks/ingest",
                headers={"x-launch-token": TOKEN},
                json={
                    "sources": [
                        {"source_kind": "file", "source_value": str(path)} for path in paths
                    ]
                },
            )
            created.raise_for_status()
            task = await _wait_for_terminal(client, created.json()["task_id"])
            assert task["status"] == "succeeded"

    asyncio.run(run())


def test_search_blocks_uses_query_embedding_fts_and_returns_top_five(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "alpha.html"
    second_path = tmp_path / "beta.html"
    first_path.write_bytes(_html("Alpha document", "第一篇文档的普通正文。"))
    second_path.write_bytes(_html("Beta document", "第二篇文档的普通正文。"))
    _ingest(tmp_path, [first_path, second_path])
    reader = RetrievalReadService(tmp_path).bind_current()
    provider = FakeEmbeddingProvider()

    result = asyncio.run(reader.search_blocks("MCP", provider=provider))

    assert 0 < len(result.blocks) <= 5
    assert any("MCP" in block.text for block in result.blocks)
    assert provider.query_calls == ["MCP"]
    payload = result.model_dump()
    assert "result_tokens" not in payload
    assert all("score" not in block for block in payload["blocks"])


def test_search_blocks_is_stable_and_applies_document_scope_before_fusion(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "alpha.html"
    second_path = tmp_path / "beta.html"
    first_path.write_bytes(_html("Alpha document", "Alpha 文档正文。"))
    second_path.write_bytes(_html("Beta document", "Beta 文档正文。"))
    _ingest(tmp_path, [first_path, second_path])
    reader = RetrievalReadService(tmp_path).bind_current()
    provider = FakeEmbeddingProvider()
    documents = reader.list_documents().documents
    beta = next(document for document in documents if document.title == "Beta document")

    first = asyncio.run(reader.search_blocks("MCP", provider=provider, doc_ids=[beta.doc_id]))
    second = asyncio.run(reader.search_blocks("MCP", provider=provider, doc_ids=[beta.doc_id]))

    assert [block.block_id for block in first.blocks] == [block.block_id for block in second.blocks]
    assert {block.doc_id for block in first.blocks} == {beta.doc_id}


def test_dense_route_skips_keyword_candidates(tmp_path: Path) -> None:
    path = tmp_path / "dense.html"
    path.write_bytes(_html("Dense document", "Dense 路线正文。"))
    _ingest(tmp_path, [path])
    reader = RetrievalReadService(tmp_path).bind_current()
    provider = FakeEmbeddingProvider()

    execution = asyncio.run(
        reader.execute_search_blocks(
            "MCP",
            provider=provider,
            mode="dense",
        )
    )

    assert execution.result.blocks
    assert execution.dense_block_ids
    assert execution.keyword_block_ids == ()
    assert provider.query_calls == ["MCP"]


def test_keyword_route_skips_query_embedding(tmp_path: Path) -> None:
    path = tmp_path / "keyword.html"
    path.write_bytes(_html("Keyword document", "MCP 关键词检索正文。"))
    _ingest(tmp_path, [path])
    reader = RetrievalReadService(tmp_path).bind_current()
    provider = FakeEmbeddingProvider()

    execution = asyncio.run(
        reader.execute_search_blocks(
            "MCP",
            provider=provider,
            mode="keyword",
        )
    )

    assert execution.result.blocks
    assert execution.dense_block_ids == ()
    assert execution.keyword_block_ids
    assert provider.query_calls == []


def test_invalid_scope_and_empty_query_fail_before_query_embedding(
    tmp_path: Path,
) -> None:
    path = tmp_path / "scope.html"
    path.write_bytes(_html("Scope document", "范围校验正文。"))
    _ingest(tmp_path, [path])
    reader = RetrievalReadService(tmp_path).bind_current()
    provider = FakeEmbeddingProvider()

    with pytest.raises(RetrievalFailure) as missing_scope:
        asyncio.run(
            reader.search_blocks(
                "MCP",
                provider=provider,
                doc_ids=["doc_missing"],
            )
        )
    assert missing_scope.value.code == "scope_not_found"
    assert provider.query_calls == []

    with pytest.raises(RetrievalFailure) as empty_query:
        asyncio.run(reader.search_blocks("   ", provider=provider))
    assert empty_query.value.code == "invalid_argument"
    assert provider.query_calls == []


def test_query_embedding_retries_once_and_does_not_fall_back_to_fts(
    tmp_path: Path,
) -> None:
    class RetryOnceProvider(FakeEmbeddingProvider):
        attempts = 0

        async def embed_query(self, query: str):
            type(self).attempts += 1
            if type(self).attempts == 1:
                from app.providers.embeddings import EmbeddingProviderError

                raise EmbeddingProviderError(
                    code="embedding_rate_limited",
                    message="rate limited",
                    retryable=True,
                    is_ambiguous=False,
                    retry_after_seconds=0,
                )
            return await super().embed_query(query)

    path = tmp_path / "retry.html"
    path.write_bytes(_html("Retry document", "重试正文。"))
    _ingest(tmp_path, [path])
    reader = RetrievalReadService(tmp_path).bind_current()
    provider = RetryOnceProvider()
    RetryOnceProvider.attempts = 0

    result = asyncio.run(reader.search_blocks("MCP", provider=provider))

    assert result.blocks
    assert RetryOnceProvider.attempts == 2


def test_query_embedding_failure_is_upstream_unavailable(tmp_path: Path) -> None:
    class FailingProvider(FakeEmbeddingProvider):
        async def embed_query(self, query: str):
            from app.providers.embeddings import EmbeddingProviderError

            raise EmbeddingProviderError(
                code="embedding_authentication_failed",
                message="invalid key",
                retryable=False,
                is_ambiguous=False,
            )

    path = tmp_path / "failure.html"
    path.write_bytes(_html("Failure document", "失败正文。"))
    _ingest(tmp_path, [path])
    reader = RetrievalReadService(tmp_path).bind_current()

    with pytest.raises(RetrievalFailure) as failure:
        asyncio.run(reader.search_blocks("MCP", provider=FailingProvider()))

    assert failure.value.code == "upstream_unavailable"
    assert failure.value.retryable is False


def test_reciprocal_rank_fusion_applies_score_and_stable_tie_order() -> None:
    dense_rows = [
        {"block_id": "a", "doc_id": "doc_z", "block_index": 1, "_distance": 0.2},
        {"block_id": "b", "doc_id": "doc_a", "block_index": 2, "_distance": 0.4},
    ]
    keyword_rows = [
        {"block_id": "c", "doc_id": "doc_a", "block_index": 1, "_score": 0.7},
        {"block_id": "b", "doc_id": "doc_a", "block_index": 2, "_score": 0.9},
    ]

    fused = reciprocal_rank_fusion(dense_rows, keyword_rows, rrf_k=60)

    assert [candidate.block_id for candidate in fused] == ["b", "c", "a"]
    assert fused[0].dense_rank == 2
    assert fused[0].keyword_rank == 2
    assert fused[0].dense_score == 0.4
    assert fused[0].keyword_score == 0.9
