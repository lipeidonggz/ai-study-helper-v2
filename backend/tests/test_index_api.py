from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx

from app.main import create_app
from app.providers.embeddings import (
    EmbeddingBatchResult,
    EmbeddingProviderError,
    FakeEmbeddingProvider,
)

TOKEN = "index-api-test-token"


def _html() -> bytes:
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        "<title>候选索引测试</title></head><body><article><h1>候选索引测试</h1>"
        "<h2>安全边界</h2><p>沙箱负责进程边界，网络控制负责出口边界。</p>"
        "<p>这段正文用于验证 S5 候选索引不会创建活动 collection。</p>"
        "</article></body></html>"
    ).encode()


def _save_document(client_headers: dict[str, str], app, html_path: Path) -> dict:
    async def run() -> dict:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            preview_response = await client.post(
                "/api/ingest/preview",
                headers=client_headers,
                json={"source_kind": "file", "source_value": str(html_path)},
            )
            preview_response.raise_for_status()
            persisted_response = await client.post(
                "/api/ingest/persist",
                headers=client_headers,
                json={"job_id": preview_response.json()["job_id"]},
            )
            persisted_response.raise_for_status()
            return persisted_response.json()

    return asyncio.run(run())


def test_index_api_builds_candidate_without_publishing(tmp_path: Path) -> None:
    html_path = tmp_path / "index.html"
    html_path.write_bytes(_html())
    provider = FakeEmbeddingProvider()
    app = create_app(
        TOKEN,
        data_root=tmp_path,
        embedding_provider_factory=lambda: provider,
    )
    headers = {"x-launch-token": TOKEN}
    persisted = _save_document(headers, app, html_path)

    async def run() -> tuple[int, dict]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/ingest/index",
                headers=headers,
                json={"job_id": persisted["job_id"]},
            )
            return response.status_code, response.json()

    status_code, bundle = asyncio.run(run())

    assert status_code == 200
    assert bundle["status"] == "candidate"
    assert bundle["doc_id"] == persisted["document"]["doc_id"]
    assert bundle["embedding_profile"]["dimension"] == 1024
    assert len(bundle["block_indexes"]) == len(persisted["block_set"]["blocks"])
    assert (tmp_path / bundle["artifact_ref"]).is_file()
    assert (tmp_path / bundle["lancedb_ref"]).is_dir()
    assert not (tmp_path / "collections").exists()
    with sqlite3.connect(tmp_path / "db" / "application.db") as connection:
        assert connection.execute(
            "SELECT status, stage FROM ingestion_jobs WHERE job_id = ?",
            (persisted["job_id"],),
        ).fetchone() == ("succeeded", "S5")


class _RejectedProvider:
    @property
    def profile(self):
        return FakeEmbeddingProvider().profile

    @property
    def batch_size(self) -> int:
        return 20

    async def embed_documents(self, _texts: list[str]) -> EmbeddingBatchResult:
        raise EmbeddingProviderError(
            code="embedding_authentication_failed",
            message="provider rejected credentials",
            retryable=False,
            is_ambiguous=False,
        )


def test_index_api_records_s5_failure_without_partial_bundle(tmp_path: Path) -> None:
    html_path = tmp_path / "rejected.html"
    html_path.write_bytes(_html())
    app = create_app(
        TOKEN,
        data_root=tmp_path,
        embedding_provider_factory=_RejectedProvider,
    )
    headers = {"x-launch-token": TOKEN}
    persisted = _save_document(headers, app, html_path)

    async def run() -> tuple[int, dict]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/ingest/index",
                headers=headers,
                json={"job_id": persisted["job_id"]},
            )
            return response.status_code, response.json()

    status_code, body = asyncio.run(run())

    assert status_code == 409
    assert body["error"]["code"] == "embedding_authentication_failed"
    assert not list((tmp_path / "jobs" / persisted["job_id"]).glob("idx/idx_*"))
    failure_path = tmp_path / "jobs" / persisted["job_id"] / "index-failure.json"
    failure = json.loads(failure_path.read_text(encoding="utf-8"))
    assert failure["code"] == "embedding_authentication_failed"
    assert failure["usage"]["request_count"] == 1
    with sqlite3.connect(tmp_path / "db" / "application.db") as connection:
        assert connection.execute(
            "SELECT status, stage, error_code FROM ingestion_jobs WHERE job_id = ?",
            (persisted["job_id"],),
        ).fetchone() == (
            "failed",
            "S5",
            "embedding_authentication_failed",
        )
