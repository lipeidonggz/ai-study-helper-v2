from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from app.main import create_app
from app.providers.embeddings import FakeEmbeddingProvider

TOKEN = "document-detail-test-token"


def _assert_utc_timestamp(value: str) -> None:
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def _html(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body><article><h1>{title}</h1>"
        f"<h2>Overview</h2><p>{body}</p>"
        "<h2>Details</h2><p>Additional detail content for the section tree.</p>"
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


def test_document_detail_uses_active_snapshot_and_lists_versions(tmp_path: Path) -> None:
    html_path = tmp_path / "detail.html"
    html_path.write_bytes(_html("Detail document", "First version body content."))

    async def run() -> tuple[dict, dict, dict]:
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
                json={"sources": [{"source_kind": "file", "source_value": str(html_path)}]},
            )
            first.raise_for_status()
            await _wait_for_terminal(client, first.json()["task_id"])
            first_current = await client.get(
                "/api/collections/current",
                headers=headers,
            )
            first_current.raise_for_status()
            doc_id = first_current.json()["manifest"]["documents"][0]["doc_id"]

            first_detail = await client.get(
                f"/api/collections/current/documents/{doc_id}",
                headers=headers,
            )
            first_detail.raise_for_status()

            html_path.write_bytes(_html("Detail document", "Second version body content changed."))
            second = await client.post(
                "/api/tasks/ingest",
                headers=headers,
                json={"sources": [{"source_kind": "file", "source_value": str(html_path)}]},
            )
            second.raise_for_status()
            await _wait_for_terminal(client, second.json()["task_id"])
            second_detail = await client.get(
                f"/api/collections/current/documents/{doc_id}",
                headers=headers,
            )
            second_detail.raise_for_status()
            return first_detail.json(), second_detail.json(), first_current.json()

    first_detail, second_detail, first_current = asyncio.run(run())

    assert first_detail["snapshot_id"] == first_current["pointer"]["snapshot_id"]
    assert first_detail["doc_version"] == 1
    assert first_detail["title"] == "Detail document"
    assert len(first_detail["sections"]) == 2
    assert first_detail["blocks"]
    assert first_detail["versions"][0]["doc_version"] == 1
    assert first_detail["versions"][0]["is_active"] is True
    assert len(first_detail["activities"]) == 1
    assert first_detail["technical"]["extractor_version"] == "2"
    _assert_utc_timestamp(first_detail["published_at"])
    _assert_utc_timestamp(first_detail["versions"][0]["created_at"])
    _assert_utc_timestamp(first_detail["activities"][0]["created_at"])

    assert second_detail["doc_version"] == 2
    assert [version["doc_version"] for version in second_detail["versions"]] == [2, 1]
    assert second_detail["versions"][0]["is_active"] is True
    assert second_detail["versions"][1]["is_active"] is False
    assert len(second_detail["activities"]) == 2
    assert any("Second version" in block["text"] for block in second_detail["blocks"])
    _assert_utc_timestamp(second_detail["versions"][1]["created_at"])
    for activity in second_detail["activities"]:
        _assert_utc_timestamp(activity["created_at"])
        _assert_utc_timestamp(activity["started_at"])
        _assert_utc_timestamp(activity["finished_at"])


def test_document_detail_rejects_document_outside_active_snapshot(
    tmp_path: Path,
) -> None:
    async def run() -> httpx.Response:
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
            return await client.get(
                "/api/collections/current/documents/doc_missing",
                headers={"x-launch-token": TOKEN},
            )

    response = asyncio.run(run())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "collection_document_not_found"
