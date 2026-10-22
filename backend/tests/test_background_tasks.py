from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
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


def _assert_utc_timestamp(value: str) -> None:
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


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


class _TrackingProvider(FakeEmbeddingProvider):
    active_calls = 0
    max_active_calls = 0

    async def embed_documents(self, texts: list[str]) -> EmbeddingBatchResult:
        type(self).active_calls += 1
        type(self).max_active_calls = max(
            type(self).max_active_calls,
            type(self).active_calls,
        )
        try:
            await asyncio.sleep(0.2)
            return await super().embed_documents(texts)
        finally:
            type(self).active_calls -= 1


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
    _assert_utc_timestamp(current["pointer"]["published_at"])


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


def test_successive_background_tasks_keep_both_batches_in_active_collection(
    tmp_path: Path,
) -> None:
    first_paths = [tmp_path / f"first-{index}.html" for index in range(3)]
    second_paths = [tmp_path / f"second-{index}.html" for index in range(4)]
    for index, path in enumerate([*first_paths, *second_paths]):
        path.write_bytes(_html(f"Document {index + 1}"))

    async def run() -> list[dict]:
        app = create_app(
            TOKEN,
            data_root=tmp_path,
            embedding_provider_factory=FakeEmbeddingProvider,
        )
        transport = httpx.ASGITransport(app=app)
        headers = {"x-launch-token": TOKEN}
        result: list[dict] = []
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            for paths in (first_paths, second_paths):
                created = await client.post(
                    "/api/tasks/ingest",
                    headers=headers,
                    json={
                        "sources": [
                            {"source_kind": "file", "source_value": str(path)} for path in paths
                        ]
                    },
                )
                created.raise_for_status()
                result.append(await _wait_for_terminal(client, headers, created.json()["task_id"]))
            current = await client.get("/api/collections/current", headers=headers)
            current.raise_for_status()
            result.append(current.json())
            history = await client.get("/api/tasks?limit=20", headers=headers)
            history.raise_for_status()
            result.append({"history": history.json()})
            return result

    first_task, second_task, current, history = asyncio.run(run())

    assert first_task["status"] == "succeeded"
    assert second_task["status"] == "succeeded"
    assert current["pointer"]["previous_snapshot_id"] == first_task["snapshot_id"]
    assert current["manifest"]["totals"]["documents"] == 7
    assert len(current["manifest"]["documents"]) == 7
    assert [task["task_id"] for task in history["history"]] == [
        second_task["task_id"],
        first_task["task_id"],
    ]
    for task in history["history"]:
        for field in ("created_at", "started_at", "finished_at"):
            _assert_utc_timestamp(task[field])

    async def read_history_after_restart() -> list[dict]:
        app = create_app(
            TOKEN,
            data_root=tmp_path,
            embedding_provider_factory=FakeEmbeddingProvider,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                "/api/tasks?limit=20",
                headers={"x-launch-token": TOKEN},
            )
            response.raise_for_status()
            return response.json()

    restarted_history = asyncio.run(read_history_after_restart())
    assert [task["task_id"] for task in restarted_history] == [
        second_task["task_id"],
        first_task["task_id"],
    ]


def test_concurrent_submissions_are_executed_by_one_serial_worker(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "serial-first.html"
    second_path = tmp_path / "serial-second.html"
    first_path.write_bytes(_html("Serial first"))
    second_path.write_bytes(_html("Serial second"))
    _TrackingProvider.active_calls = 0
    _TrackingProvider.max_active_calls = 0

    async def run() -> tuple[dict, dict, list[set[str]]]:
        app = create_app(
            TOKEN,
            data_root=tmp_path,
            embedding_provider_factory=_TrackingProvider,
        )
        transport = httpx.ASGITransport(app=app)
        headers = {"x-launch-token": TOKEN}
        observed_running: list[set[str]] = []
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            tasks: list[dict] = []
            for path in (first_path, second_path):
                created = await client.post(
                    "/api/tasks/ingest",
                    headers=headers,
                    json={"sources": [{"source_kind": "file", "source_value": str(path)}]},
                )
                created.raise_for_status()
                tasks.append(created.json())

            first_task_id = tasks[0]["task_id"]
            second_task_id = tasks[1]["task_id"]
            assert tasks[1]["status"] == "queued"
            active = await client.get("/api/tasks/active", headers=headers)
            active.raise_for_status()
            assert {task["task_id"] for task in active.json()} == {
                first_task_id,
                second_task_id,
            }
            while True:
                listed = await client.get("/api/tasks?limit=20", headers=headers)
                listed.raise_for_status()
                history = listed.json()
                running_ids = {task["task_id"] for task in history if task["status"] == "running"}
                observed_running.append(running_ids)
                statuses = {task["task_id"]: task["status"] for task in history}
                if statuses.get(first_task_id) == "queued":
                    await asyncio.sleep(0.02)
                    continue
                if statuses.get(second_task_id) == "running":
                    break
                if statuses.get(first_task_id) in {"succeeded", "failed"} and statuses.get(
                    second_task_id
                ) in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(0.02)

            first = await _wait_for_terminal(client, headers, first_task_id)
            second = await _wait_for_terminal(client, headers, second_task_id)
            active_after = await client.get("/api/tasks/active", headers=headers)
            active_after.raise_for_status()
            assert active_after.json() == []
            return first, second, observed_running

    first, second, observed_running = asyncio.run(run())

    assert first["status"] == "succeeded"
    assert second["status"] == "succeeded"
    assert first["started_at"] <= second["started_at"]
    assert _TrackingProvider.max_active_calls == 1
    assert all(len(running_ids) <= 1 for running_ids in observed_running)


def test_preview_job_can_be_published_through_task_history(tmp_path: Path) -> None:
    html_path = tmp_path / "preview-single.html"
    html_path.write_bytes(_html("Single preview publication"))

    async def run() -> tuple[dict, dict, list[dict]]:
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
            preview = await client.post(
                "/api/ingest/preview",
                headers=headers,
                json={"source_kind": "file", "source_value": str(html_path)},
            )
            preview.raise_for_status()
            before = await client.get("/api/collections/current", headers=headers)
            before.raise_for_status()
            assert before.json() is None

            created = await client.post(
                "/api/tasks/ingest/preview",
                headers=headers,
                json={"job_id": preview.json()["job_id"]},
            )
            created.raise_for_status()
            task = await _wait_for_terminal(client, headers, created.json()["task_id"])
            current = await client.get("/api/collections/current", headers=headers)
            current.raise_for_status()
            history = await client.get("/api/tasks?limit=20", headers=headers)
            history.raise_for_status()
            return task, current.json(), history.json()

    task, current, history = asyncio.run(run())

    assert task["status"] == "succeeded"
    assert task["snapshot_id"] == current["pointer"]["snapshot_id"]
    assert current["manifest"]["totals"]["documents"] == 1
    assert current["manifest"]["documents"][0]["title"] == "Single preview publication"
    assert [item["task_id"] for item in history] == [task["task_id"]]


def test_second_partial_task_keeps_previous_active_documents(tmp_path: Path) -> None:
    first_path = tmp_path / "first-kept.html"
    valid_path = tmp_path / "valid-added.html"
    first_path.write_bytes(_html("First kept"))
    valid_path.write_bytes(_html("Valid added"))

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
            first = await client.post(
                "/api/tasks/ingest",
                headers=headers,
                json={"sources": [{"source_kind": "file", "source_value": str(first_path)}]},
            )
            first.raise_for_status()
            first_task = await _wait_for_terminal(
                client,
                headers,
                first.json()["task_id"],
            )
            second = await client.post(
                "/api/tasks/ingest",
                headers=headers,
                json={
                    "sources": [
                        {"source_kind": "file", "source_value": str(valid_path)},
                        {
                            "source_kind": "file",
                            "source_value": str(tmp_path / "missing.html"),
                        },
                    ]
                },
            )
            second.raise_for_status()
            await _wait_for_terminal(client, headers, second.json()["task_id"])
            current = await client.get("/api/collections/current", headers=headers)
            current.raise_for_status()
            return first_task, current.json()

    first_task, current = asyncio.run(run())

    assert current["manifest"]["status"] == "ready_partial"
    assert current["manifest"]["totals"]["documents"] == 2
    assert len(current["manifest"]["failed_documents"]) == 1
    assert current["pointer"]["previous_snapshot_id"] == first_task["snapshot_id"]


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
