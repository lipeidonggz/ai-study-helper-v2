from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import httpx

from app.main import create_app
from app.providers.embeddings import EmbeddingBatchResult, FakeEmbeddingProvider
from app.storage.database import StorageDatabase
from app.storage.settings_models import ProviderSettingRow

TOKEN = "workspace-reset-test-token"


def _html(title: str) -> bytes:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body><article><h1>{title}</h1>"
        "<p>Workspace reset test content with enough text for extraction.</p>"
        "</article></body></html>"
    ).encode()


class _SlowProvider(FakeEmbeddingProvider):
    async def embed_documents(self, texts: list[str]) -> EmbeddingBatchResult:
        await asyncio.sleep(0.5)
        return await super().embed_documents(texts)


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


def test_reset_clears_knowledge_and_records_but_keeps_model_settings(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first.html"
    second_path = tmp_path / "second.html"
    first_path.write_bytes(_html("First reset document"))
    second_path.write_bytes(_html("Second reset document"))
    database = StorageDatabase(tmp_path)
    with database.session() as session, session.begin():
        session.add(
            ProviderSettingRow(
                provider="embedding",
                model_id="qwen3.7-text-embedding",
                dimension=1024,
            )
        )
    (tmp_path / "traces").mkdir()
    (tmp_path / "traces" / "old-trace.jsonl").write_text("{}", encoding="utf-8")
    (tmp_path / "backups" / "db").mkdir(parents=True)
    (tmp_path / "backups" / "db" / "old-backup.db").write_bytes(b"backup")

    async def run() -> dict:
        app = create_app(
            TOKEN,
            data_root=tmp_path,
            embedding_provider_factory=FakeEmbeddingProvider,
        )
        transport = httpx.ASGITransport(app=app)
        headers = {"x-launch-token": TOKEN}
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            created = await client.post(
                "/api/tasks/ingest",
                headers=headers,
                json={
                    "sources": [
                        {"source_kind": "file", "source_value": str(first_path)},
                        {"source_kind": "file", "source_value": str(second_path)},
                    ]
                },
            )
            created.raise_for_status()
            await _wait_for_terminal(client, created.json()["task_id"])

            wrong_confirmation = await client.post(
                "/api/workspace/reset",
                headers=headers,
                json={"confirmation": "reset"},
            )
            assert wrong_confirmation.status_code == 422

            reset = await client.post(
                "/api/workspace/reset",
                headers=headers,
                json={"confirmation": "清空知识库"},
            )
            reset.raise_for_status()
            return reset.json()

    result = asyncio.run(run())

    assert result["documents"] == 2
    assert result["ingestion_tasks"] == 1

    async def read_empty_state() -> tuple[int, object, list[dict]]:
        app = create_app(
            TOKEN,
            data_root=tmp_path,
            embedding_provider_factory=FakeEmbeddingProvider,
        )
        transport = httpx.ASGITransport(app=app)
        headers = {"x-launch-token": TOKEN}
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            current = await client.get("/api/collections/current", headers=headers)
            current.raise_for_status()
            history = await client.get("/api/tasks?limit=20", headers=headers)
            history.raise_for_status()
            return current.status_code, current.json(), history.json()

    current_status, current, history = asyncio.run(read_empty_state())
    assert current_status == 200
    assert current is None
    assert history == []

    with sqlite3.connect(tmp_path / "db" / "application.db") as connection:
        for table in (
            "documents",
            "document_versions",
            "source_artifacts",
            "ingestion_jobs",
            "ingestion_tasks",
            "active_collection_pointer",
            "collection_publish_records",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        assert connection.execute(
            "SELECT provider, model_id FROM provider_settings"
        ).fetchall() == [("embedding", "qwen3.7-text-embedding")]

    for directory in ("jobs", "collections", "traces", "backups/db"):
        assert list((tmp_path / directory).iterdir()) == []


def test_reset_is_rejected_while_background_task_is_active(tmp_path: Path) -> None:
    html_path = tmp_path / "running.html"
    html_path.write_bytes(_html("Running reset document"))

    async def run() -> int:
        app = create_app(
            TOKEN,
            data_root=tmp_path,
            embedding_provider_factory=_SlowProvider,
        )
        transport = httpx.ASGITransport(app=app)
        headers = {"x-launch-token": TOKEN}
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            created = await client.post(
                "/api/tasks/ingest",
                headers=headers,
                json={"sources": [{"source_kind": "file", "source_value": str(html_path)}]},
            )
            created.raise_for_status()
            reset = await client.post(
                "/api/workspace/reset",
                headers=headers,
                json={"confirmation": "清空知识库"},
            )
            await client.post(
                f"/api/tasks/{created.json()['task_id']}/cancel",
                headers=headers,
            )
            await _wait_for_terminal(client, created.json()["task_id"])
            return reset.status_code

    assert asyncio.run(run()) == 409
