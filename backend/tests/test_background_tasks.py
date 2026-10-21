from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from app.collections.service import CollectionSnapshotService
from app.ingestion.service import IngestionService
from app.main import create_app
from app.providers.embeddings import EmbeddingBatchResult, FakeEmbeddingProvider
from app.storage.database import StorageDatabase
from app.storage.task_models import IngestionTaskRow
from app.tasks.service import BackgroundTaskService

TOKEN = "background-task-test-token"


def _html(title: str) -> bytes:
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body><article><h1>{title}</h1>"
        "<h2>正文</h2><p>后台任务测试正文包含足够的文字，用于完成解析、章节、切块和索引。</p>"
        "<p>补充内容确保文档不会被提取器判定为空。</p>"
        "</article></body></html>"
    ).encode()


class _SlowProvider(FakeEmbeddingProvider):
    async def embed_documents(self, texts: list[str]) -> EmbeddingBatchResult:
        await asyncio.sleep(0.25)
        return await super().embed_documents(texts)


async def _wait_for_terminal(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    task_id: str,
    timeout_seconds: float = 15,
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/api/tasks/{task_id}", headers=headers)
        response.raise_for_status()
        task = response.json()
        if task["status"] in {"succeeded", "failed", "canceled", "interrupted"}:
            return task
        await asyncio.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach terminal state")


def _run_success_task(tmp_path: Path) -> tuple[dict, dict]:
    async def run() -> tuple[dict, dict]:
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
                        {
                            "source_kind": "file",
                            "source_value": str(tmp_path / "document.html"),
                        }
                    ]
                },
            )
            created.raise_for_status()
            task = await _wait_for_terminal(client, headers, created.json()["task_id"])
            current = await client.get("/api/collections/current", headers=headers)
            current.raise_for_status()
            return task, current.json()

    return asyncio.run(run())


def test_background_task_runs_pipeline_to_published_snapshot(tmp_path: Path) -> None:
    html_path = tmp_path / "document.html"
    html_path.write_bytes(_html("后台成功"))

    task, current = _run_success_task(tmp_path)

    assert task["status"] == "succeeded"
    assert task["stage"] == "S6"
    assert task["completed_sources"] == 1
    assert task["failed_sources"] == 0
    assert task["snapshot_id"] is not None
    assert current["pointer"]["snapshot_id"] == task["snapshot_id"]


def test_background_task_keeps_partial_success_when_one_source_fails(tmp_path: Path) -> None:
    html_path = tmp_path / "valid.html"
    html_path.write_bytes(_html("部分成功"))

    async def run() -> tuple[dict, dict]:
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
                        {"source_kind": "file", "source_value": str(html_path)},
                        {
                            "source_kind": "file",
                            "source_value": str(tmp_path / "missing.html"),
                        },
                    ]
                },
            )
            created.raise_for_status()
            task = await _wait_for_terminal(client, headers, created.json()["task_id"])
            current = await client.get("/api/collections/current", headers=headers)
            current.raise_for_status()
            return task, current.json()

    task, current = asyncio.run(run())

    assert task["status"] == "succeeded"
    assert task["completed_sources"] == 2
    assert task["failed_sources"] == 1
    assert current["pointer"] is not None
    assert len(current["manifest"]["failed_documents"]) == 1


def test_background_task_can_pause_and_resume(tmp_path: Path) -> None:
    html_path = tmp_path / "pause.html"
    html_path.write_bytes(_html("暂停恢复"))

    async def run() -> dict:
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
            task_id = created.json()["task_id"]
            await asyncio.sleep(0.05)
            paused = await client.post(f"/api/tasks/{task_id}/pause", headers=headers)
            paused.raise_for_status()
            await asyncio.sleep(0.3)
            assert (await client.get(f"/api/tasks/{task_id}", headers=headers)).json()[
                "status"
            ] == "paused"
            resumed = await client.post(f"/api/tasks/{task_id}/resume", headers=headers)
            resumed.raise_for_status()
            return await _wait_for_terminal(client, headers, task_id)

    task = asyncio.run(run())
    assert task["status"] == "succeeded"


def test_background_task_can_be_canceled(tmp_path: Path) -> None:
    html_path = tmp_path / "cancel.html"
    html_path.write_bytes(_html("取消"))

    async def run() -> dict:
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
            task_id = created.json()["task_id"]
            canceled = await client.post(f"/api/tasks/{task_id}/cancel", headers=headers)
            canceled.raise_for_status()
            return await _wait_for_terminal(client, headers, task_id)

    task = asyncio.run(run())
    assert task["status"] == "canceled"
    assert task["snapshot_id"] is None


def test_running_task_is_interrupted_on_start_and_can_resume(tmp_path: Path) -> None:
    html_path = tmp_path / "resume.html"
    html_path.write_bytes(_html("中断恢复"))
    task_id = "task_" + "a" * 26
    database = StorageDatabase(tmp_path)
    with database.session() as session, session.begin():
        session.add(
            IngestionTaskRow(
                task_id=task_id,
                status="running",
                stage="S0-S4",
                sources_json=(
                    '[{"source_kind":"file","source_value":"'
                    + str(html_path).replace("\\", "\\\\")
                    + '"}]'
                ),
                total_sources=1,
                next_source_index=0,
                completed_sources=0,
                failed_sources=0,
                successful_job_ids_json="[]",
                failures_json="[]",
                retryable=False,
            )
        )
    ingestion = IngestionService(tmp_path, embedding_provider_factory=FakeEmbeddingProvider)
    collection = CollectionSnapshotService(tmp_path)
    service = BackgroundTaskService(tmp_path, ingestion, collection)

    async def run() -> dict:
        await service.start()
        interrupted = service.get(task_id)
        assert interrupted.status == "interrupted"
        await service.resume(task_id)
        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline:
            record = service.get(task_id)
            if record.status in {"succeeded", "failed"}:
                await service.stop()
                return record.model_dump()
            await asyncio.sleep(0.05)
        raise AssertionError("resumed task did not finish")

    task = asyncio.run(run())
    assert task["status"] == "succeeded"
