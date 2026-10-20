from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from app.ingestion.source import FetchedResponse
from app.main import create_app
from tests.helpers import fixture_bytes

TOKEN = "preview-test-token"


class StaticUrlFetcher:
    async def fetch(self, body_url: str) -> FetchedResponse:
        return FetchedResponse(
            url=body_url,
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=fixture_bytes("no-headings.html"),
            warnings=[],
        )


def request_preview(
    data_root: Path,
    payload: dict[str, str],
) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(
            app=create_app(
                TOKEN,
                data_root=data_root,
                url_fetcher=StaticUrlFetcher(),
            )
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                "/api/ingest/preview",
                headers={"x-launch-token": TOKEN},
                json=payload,
            )

    return asyncio.run(request())


def test_local_preview_endpoint_persists_job_artifacts(tmp_path: Path) -> None:
    html_path = tmp_path / "article.html"
    html_path.write_bytes(fixture_bytes("article.html"))

    response = request_preview(
        tmp_path,
        {"source_kind": "file", "source_value": str(html_path)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["document"]["title"] == "Agent 隔离设计"
    job_dir = tmp_path / "jobs" / body["job_id"]
    assert (job_dir / "source.html").exists()
    assert (job_dir / "extracted.json").exists()
    assert (job_dir / "section-tree.json").exists()
    assert (job_dir / "blocks.json").exists()
    assert (job_dir / "preview.json").exists()


def test_url_preview_uses_the_same_pipeline(tmp_path: Path) -> None:
    response = request_preview(
        tmp_path,
        {
            "source_kind": "url",
            "source_value": "https://example.com/article",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source"]["source_kind"] == "url"
    assert body["source"]["final_url"] == "https://example.com/article"
    assert body["block_set"]["blocks"]


def test_local_path_with_chinese_and_spaces(tmp_path: Path) -> None:
    response = request_preview(
        tmp_path,
        {
            "source_kind": "file",
            "source_value": str(
                Path(__file__).parent / "fixtures" / "复杂 路径" / "带 空格的文档.html"
            ),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["document"]["title"] == "中文与空格路径"
    assert len(body["section_tree"]["sections"]) == 1
    assert len(body["block_set"]["blocks"]) == 1


def test_preview_requires_launch_token(tmp_path: Path) -> None:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=create_app(TOKEN, data_root=tmp_path))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.post(
                "/api/ingest/preview",
                json={"source_kind": "file", "source_value": "missing.html"},
            )

    response = asyncio.run(request())
    assert response.status_code == 401
