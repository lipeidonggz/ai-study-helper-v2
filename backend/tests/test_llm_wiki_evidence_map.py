from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from app.main import create_app
from app.providers.chat import (
    FakeChatProvider,
)
from app.providers.embeddings import FakeEmbeddingProvider
from app.wiki.errors import WikiCompilationError
from app.wiki.models import WikiCompileRequest, WikiPageSpec
from app.wiki.service import WikiCompiler

TOKEN = "wiki-test-token"


@dataclass(frozen=True, slots=True)
class _PublishedCollection:
    snapshot_id: str
    snapshot_root: Path
    block_ids: tuple[str, ...]


def _html(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body><article><h1>{title}</h1>"
        f"<h2>{title}机制</h2><p>{body}</p>"
        "<p>补充正文用于确保提取、章节、切块和候选索引流程具有足够内容。</p>"
        "</article></body></html>"
    ).encode()


def _publish_collection(tmp_path: Path) -> _PublishedCollection:
    async def run() -> _PublishedCollection:
        first = tmp_path / "first.html"
        second = tmp_path / "second.html"
        first.write_bytes(_html("工作区隔离", "并行任务使用独立工作区，避免相互覆盖状态。"))
        second.write_bytes(_html("检查点恢复", "任务从中断前的检查点恢复并继续执行。"))
        app = create_app(
            TOKEN,
            data_root=tmp_path,
            embedding_provider_factory=FakeEmbeddingProvider,
        )
        headers = {"x-launch-token": TOKEN}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            job_ids: list[str] = []
            block_ids: list[str] = []
            for source in (first, second):
                preview = await client.post(
                    "/api/ingest/preview",
                    headers=headers,
                    json={"source_kind": "file", "source_value": str(source)},
                )
                preview.raise_for_status()
                job_id = preview.json()["job_id"]
                job_ids.append(job_id)
                persisted = await client.post(
                    "/api/ingest/persist",
                    headers=headers,
                    json={"job_id": job_id},
                )
                persisted.raise_for_status()
                block_ids.append(persisted.json()["block_set"]["blocks"][0]["block_id"])
                indexed = await client.post(
                    "/api/ingest/index",
                    headers=headers,
                    json={"job_id": job_id},
                )
                indexed.raise_for_status()
            published = await client.post(
                "/api/collections/publish",
                headers=headers,
                json={"job_ids": job_ids},
            )
            published.raise_for_status()
            snapshot_id = published.json()["manifest"]["snapshot_id"]
            return _PublishedCollection(
                snapshot_id=snapshot_id,
                snapshot_root=tmp_path / "collections" / snapshot_id,
                block_ids=tuple(block_ids),
            )

    return asyncio.run(run())


def _request(block_ids: tuple[str, ...]) -> WikiCompileRequest:
    return WikiCompileRequest(
        pages=[
            WikiPageSpec(
                title="任务状态控制面",
                aliases=["任务边界"],
                coverage_questions=["任务执行时有哪些状态隔离与恢复边界？"],
                source_block_ids=list(block_ids),
            )
        ]
    )


def _response(paragraphs: list[dict[str, object]]) -> str:
    return json.dumps({"paragraphs": paragraphs}, ensure_ascii=False)


def _build_directories(tmp_path: Path, snapshot_id: str) -> list[Path]:
    root = tmp_path / "wiki" / snapshot_id
    if not root.exists():
        return []
    return sorted(
        path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")
    )


def _snapshot_tree_hash(snapshot_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(snapshot_root.rglob("*")):
        if not path.is_file():
            continue
        digest.update(path.relative_to(snapshot_root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_compile_writes_grounded_page_outside_collection_and_verifies_on_load(
    tmp_path: Path,
) -> None:
    collection = _publish_collection(tmp_path)
    before = _snapshot_tree_hash(collection.snapshot_root)
    provider = FakeChatProvider(
        [
            _response(
                [
                    {
                        "text": "并行任务通过独立工作区隔离彼此状态。",
                        "source_block_ids": [collection.block_ids[0]],
                    },
                    {
                        "text": "被中断的任务可从检查点恢复。",
                        "source_block_ids": [collection.block_ids[1]],
                    },
                ]
            )
        ]
    )

    result = asyncio.run(
        WikiCompiler(tmp_path).compile(
            collection.snapshot_id,
            _request(collection.block_ids),
            provider=provider,
        )
    )

    assert _snapshot_tree_hash(collection.snapshot_root) == before
    build_root = tmp_path / "wiki" / collection.snapshot_id / result.manifest.build_id
    assert (build_root / "manifest.json").is_file()
    page = result.pages[0]
    assert page.paragraphs[0].source_block_ids == [collection.block_ids[0]]
    assert result.manifest.pages[0].referenced_block_count == 2
    assert result.manifest.pages[0].unreferenced_block_ids == []

    loaded = WikiCompiler(tmp_path).load(collection.snapshot_id, result.manifest.build_id)
    assert loaded.pages == result.pages
    assert loaded.manifest.snapshot_manifest_hash == result.manifest.snapshot_manifest_hash


def test_page_identity_is_stable_across_builds(tmp_path: Path) -> None:
    collection = _publish_collection(tmp_path)
    request = _request(collection.block_ids)
    response = _response(
        [
            {
                "text": "工作区与检查点共同限制任务状态。",
                "source_block_ids": list(collection.block_ids),
            }
        ]
    )

    first = asyncio.run(
        WikiCompiler(tmp_path).compile(
            collection.snapshot_id,
            request,
            provider=FakeChatProvider([response]),
        )
    )
    second = asyncio.run(
        WikiCompiler(tmp_path).compile(
            collection.snapshot_id,
            request,
            provider=FakeChatProvider([response]),
        )
    )

    assert first.pages[0].page_id == second.pages[0].page_id
    assert first.manifest.build_id != second.manifest.build_id


def test_unknown_candidate_block_fails_before_provider_call(tmp_path: Path) -> None:
    collection = _publish_collection(tmp_path)
    request = WikiCompileRequest(
        pages=[
            WikiPageSpec(
                title="不存在的证据",
                source_block_ids=["missing#block"],
            )
        ]
    )

    with pytest.raises(WikiCompilationError) as failure:
        asyncio.run(
            WikiCompiler(tmp_path).compile(
                collection.snapshot_id,
                request,
                provider=FakeChatProvider(["unused"]),
            )
        )

    assert failure.value.code == "wiki_source_block_missing"
    assert _build_directories(tmp_path, collection.snapshot_id) == []


def test_model_cannot_reference_another_pages_candidate_block(tmp_path: Path) -> None:
    collection = _publish_collection(tmp_path)
    request = WikiCompileRequest(
        pages=[
            WikiPageSpec(
                title="工作区",
                source_block_ids=[collection.block_ids[0]],
            ),
            WikiPageSpec(
                title="恢复",
                source_block_ids=[collection.block_ids[1]],
            ),
        ]
    )
    provider = FakeChatProvider(
        [
            _response(
                [
                    {
                        "text": "越界引用另一个页面的证据。",
                        "source_block_ids": [collection.block_ids[1]],
                    }
                ]
            )
        ]
    )

    with pytest.raises(WikiCompilationError) as failure:
        asyncio.run(
            WikiCompiler(tmp_path).compile(
                collection.snapshot_id,
                request,
                provider=provider,
            )
        )

    assert failure.value.code == "wiki_source_out_of_scope"
    assert _build_directories(tmp_path, collection.snapshot_id) == []


@pytest.mark.parametrize(
    "invalid_paragraph",
    [
        {"text": "   ", "source_block_ids": ["source"]},
        {"text": "没有来源的段落", "source_block_ids": []},
    ],
)
def test_invalid_paragraph_contract_fails_without_partial_build(
    tmp_path: Path,
    invalid_paragraph: dict[str, object],
) -> None:
    collection = _publish_collection(tmp_path)
    if invalid_paragraph["source_block_ids"] == ["source"]:
        invalid_paragraph["source_block_ids"] = [collection.block_ids[0]]
    provider = FakeChatProvider(
        [
            _response(
                [
                    invalid_paragraph,
                ]
            )
        ]
    )

    with pytest.raises(WikiCompilationError) as failure:
        asyncio.run(
            WikiCompiler(tmp_path).compile(
                collection.snapshot_id,
                _request((collection.block_ids[0],)),
                provider=provider,
            )
        )

    assert failure.value.code == "wiki_response_invalid"
    assert _build_directories(tmp_path, collection.snapshot_id) == []


def test_later_page_failure_does_not_publish_earlier_page(tmp_path: Path) -> None:
    collection = _publish_collection(tmp_path)
    request = WikiCompileRequest(
        pages=[
            WikiPageSpec(title="工作区", source_block_ids=[collection.block_ids[0]]),
            WikiPageSpec(title="恢复", source_block_ids=[collection.block_ids[1]]),
        ]
    )
    provider = FakeChatProvider(
        [
            _response(
                [
                    {
                        "text": "第一页本可成功。",
                        "source_block_ids": [collection.block_ids[0]],
                    }
                ]
            )
        ]
    )

    with pytest.raises(WikiCompilationError) as failure:
        asyncio.run(
            WikiCompiler(tmp_path).compile(
                collection.snapshot_id,
                request,
                provider=provider,
            )
        )

    assert failure.value.code == "wiki_provider_failed"
    assert _build_directories(tmp_path, collection.snapshot_id) == []


def test_modified_page_artifact_is_rejected_on_load(tmp_path: Path) -> None:
    collection = _publish_collection(tmp_path)
    response = _response(
        [
            {
                "text": "来自原文的工作区说明。",
                "source_block_ids": [collection.block_ids[0]],
            }
        ]
    )
    result = asyncio.run(
        WikiCompiler(tmp_path).compile(
            collection.snapshot_id,
            _request((collection.block_ids[0],)),
            provider=FakeChatProvider([response]),
        )
    )
    page_path = (
        tmp_path
        / "wiki"
        / collection.snapshot_id
        / result.manifest.build_id
        / result.manifest.pages[0].artifact_ref
    )
    page_path.write_bytes(page_path.read_bytes() + b" ")

    with pytest.raises(WikiCompilationError) as failure:
        WikiCompiler(tmp_path).load(collection.snapshot_id, result.manifest.build_id)

    assert failure.value.code == "wiki_artifact_invalid"
