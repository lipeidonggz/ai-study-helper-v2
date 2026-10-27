from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.main import create_app
from app.providers.embeddings import FakeEmbeddingProvider
from app.retrieval.service import RetrievalFailure, RetrievalReadService, _snippet
from tests.helpers import fixture_bytes

TOKEN = "retrieval-read-test-token"


def _html(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body><article><h1>{title}</h1>"
        "<h2>Alpha</h2><p>Alpha 根章节包含 MCP 和沙箱边界说明。</p>"
        "<h3>Alpha child</h3><p>Alpha 子章节包含需要完整枚举的补充证据。</p>"
        "<h2>Beta</h2>"
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


def _ingest(data_root: Path, paths: list[Path]) -> str:
    async def run() -> str:
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
            assert task["snapshot_id"] is not None
            return task["snapshot_id"]

    return asyncio.run(run())


def test_reader_keeps_its_snapshot_after_a_new_collection_is_published(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "alpha-doc.html"
    second_path = tmp_path / "beta-doc.html"
    first_path.write_bytes(_html("Alpha document", "第一篇文档的第二章节正文。"))
    second_path.write_bytes(_html("Beta document", "第二篇文档的第二章节正文。"))

    first_snapshot = _ingest(tmp_path, [first_path])
    service = RetrievalReadService(tmp_path)
    bound_reader = service.bind_current()

    second_snapshot = _ingest(tmp_path, [second_path])
    fresh_reader = service.bind_current()

    assert bound_reader.snapshot_id == first_snapshot
    assert second_snapshot != first_snapshot
    assert [document.title for document in bound_reader.list_documents().documents] == [
        "Alpha document"
    ]
    assert {document.title for document in fresh_reader.list_documents().documents} == {
        "Alpha document",
        "Beta document",
    }


def test_explicit_snapshot_binding_does_not_follow_active_pointer(tmp_path: Path) -> None:
    first_path = tmp_path / "alpha-doc.html"
    second_path = tmp_path / "beta-doc.html"
    first_path.write_bytes(_html("Alpha document", "第一篇文档的第二章节正文。"))
    second_path.write_bytes(_html("Beta document", "第二篇文档的第二章节正文。"))

    first_snapshot = _ingest(tmp_path, [first_path])
    second_snapshot = _ingest(tmp_path, [second_path])
    reader = RetrievalReadService(tmp_path).bind_snapshot(first_snapshot)

    assert second_snapshot != first_snapshot
    assert RetrievalReadService(tmp_path).bind_current().snapshot_id == second_snapshot
    assert [document.title for document in reader.list_documents().documents] == ["Alpha document"]


def test_list_documents_applies_filters_and_stable_sorting(tmp_path: Path) -> None:
    paths = [tmp_path / f"document-{index}.html" for index in range(3)]
    for index, path in enumerate(paths):
        path.write_bytes(_html(f"Document {index}", f"第 {index} 篇正文。"))
    _ingest(tmp_path, paths)
    reader = RetrievalReadService(tmp_path).bind_current()

    all_documents = reader.list_documents()
    filtered = reader.list_documents(title_contains="Document 1")
    source_filtered = reader.list_documents(
        source=all_documents.documents[0].source_label,
        lang="zh",
    )

    assert all_documents.total == 3
    assert [document.title for document in all_documents.documents] == [
        "Document 0",
        "Document 1",
        "Document 2",
    ]
    assert [document.title for document in filtered.documents] == ["Document 1"]
    assert filtered.total == 1
    assert source_filtered.total == 3


def test_outline_and_section_enumeration_include_descendant_blocks(
    tmp_path: Path,
) -> None:
    path = tmp_path / "outline.html"
    path.write_bytes(_html("Outline document", "Beta 章节正文。"))
    _ingest(tmp_path, [path])
    reader = RetrievalReadService(tmp_path).bind_current()
    document = reader.list_documents().documents[0]

    outline = reader.get_outline([document.doc_id]).documents[0].document
    assert outline is not None
    alpha = next(section for section in outline.sections if section.title == "Alpha")
    alpha_own_blocks = reader.list_blocks(
        document.doc_id,
        [alpha.path],
        include_descendants=False,
    )
    alpha_subtree_blocks = reader.list_blocks(
        document.doc_id,
        [alpha.path],
        include_descendants=True,
    )
    beta = next(section for section in outline.sections if section.title == "Beta")
    combined_blocks = reader.list_blocks(
        document.doc_id,
        [alpha.path, beta.path, alpha.path],
        include_descendants=True,
    )
    all_blocks = reader.list_blocks(document.doc_id)

    assert alpha.subtree_blocks >= 2
    assert alpha.subtree_context_tokens >= alpha.own_context_tokens
    assert any("MCP" in block.text for block in alpha_own_blocks.blocks)
    assert all("子章节" not in block.text for block in alpha_own_blocks.blocks)
    assert any("子章节" in block.text for block in alpha_subtree_blocks.blocks)
    alpha_own_context = alpha_own_blocks.blocks[0].section_context
    assert alpha_own_context.section_depth == alpha.depth
    assert alpha_own_context.has_child_sections is True
    assert alpha_own_context.document_section_count == len(outline.sections)
    assert alpha_own_blocks.scope == "section"
    assert alpha_subtree_blocks.scope == "subtree"
    assert all_blocks.scope == "document"
    assert alpha_own_blocks.total < alpha_subtree_blocks.total
    assert len({block.block_id for block in combined_blocks.blocks}) == len(combined_blocks.blocks)
    assert combined_blocks.total > alpha_subtree_blocks.total
    assert [block.block_id for block in all_blocks.blocks] == sorted(
        (block.block_id for block in all_blocks.blocks),
    )


def test_get_blocks_returns_complete_requested_blocks_in_request_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "exact-blocks.html"
    path.write_bytes(_html("Exact document", "MCP 精确补全正文。"))
    _ingest(tmp_path, [path])
    reader = RetrievalReadService(tmp_path).bind_current()
    document = reader.list_documents().documents[0]
    matches = reader.scan_term("MCP", [document.doc_id]).matches
    block_id = matches[0].block_id

    exact = reader.get_blocks([block_id, block_id])

    assert exact.total == 1
    assert exact.blocks[0].block_id == block_id
    assert "MCP" in exact.blocks[0].text
    assert exact.blocks[0].section_context.has_child_sections is True
    assert exact.blocks[0].section_context.document_section_count > 1
    with pytest.raises(RetrievalFailure) as missing:
        reader.get_blocks([block_id, "doc_missing#v0001#c1#b00001"])
    assert missing.value.code == "scope_not_found"
    assert "doc_missing#v0001#c1#b00001" in missing.value.message


def test_no_heading_document_reports_single_root_section_context(
    tmp_path: Path,
) -> None:
    path = tmp_path / "no-heading-context.html"
    path.write_bytes(fixture_bytes("no-headings.html"))
    _ingest(tmp_path, [path])
    reader = RetrievalReadService(tmp_path).bind_current()
    document = reader.list_documents().documents[0]

    blocks = reader.list_blocks(document.doc_id)

    assert blocks.total == 1
    assert blocks.blocks[0].section_path == []
    assert blocks.blocks[0].section_context.section_depth == 0
    assert blocks.blocks[0].section_context.has_child_sections is False
    assert blocks.blocks[0].section_context.document_section_count == 1


def test_get_outline_returns_per_document_scope_errors(tmp_path: Path) -> None:
    path = tmp_path / "outline-error.html"
    path.write_bytes(_html("Outline error document", "用于验证同一请求中的部分文档错误。"))
    _ingest(tmp_path, [path])
    reader = RetrievalReadService(tmp_path).bind_current()
    document = reader.list_documents().documents[0]

    outline = reader.get_outline([document.doc_id, "doc_missing"])

    assert outline.documents[0].document is not None
    assert outline.documents[1].error is not None
    assert outline.documents[1].error.code == "scope_not_found"


def test_scan_term_distinguishes_zero_matches_from_missing_scope(
    tmp_path: Path,
) -> None:
    path = tmp_path / "scan.html"
    path.write_bytes(_html("Scan document", "只有普通正文，没有那个缩写。"))
    _ingest(tmp_path, [path])
    reader = RetrievalReadService(tmp_path).bind_current()
    document = reader.list_documents().documents[0]

    matches = reader.scan_term("mcp", [document.doc_id])
    no_matches = reader.scan_term("SENTINEL_ABSENT", [document.doc_id])

    assert matches.count == 1
    assert "MCP" in matches.matches[0].snippet
    assert matches.matches[0].section_context.section_depth == 1
    assert matches.matches[0].section_context.has_child_sections is True
    assert matches.matches[0].section_context.document_section_count > 1
    assert no_matches.count == 0
    assert no_matches.matches == []
    with pytest.raises(RetrievalFailure) as missing_scope:
        reader.scan_term("MCP", ["doc_missing"])
    assert missing_scope.value.code == "scope_not_found"


def test_scan_snippet_expands_to_complete_sentence_boundaries() -> None:
    text = (
        "Enterprises often ask us how to secure MCP connections. "
        "It's a good question, but "
        + "the surrounding discussion remains relevant "
        * 4
        + "the right one is broader than MCP specifically. "
        "This following sentence provides additional context for the reader."
    )
    start = text.rindex("MCP")

    snippet = _snippet(text, start, start + len("MCP"))

    assert snippet.startswith("…It's a good question")
    assert "broader than MCP specifically." in snippet
    assert "This following sentence provides additional context for the reader." in snippet
    assert snippet.endswith(".")


def test_scan_snippet_keeps_opening_and_closing_sentence_punctuation() -> None:
    text = "First context sentence. “The target MCP answer.” Final context sentence."
    start = text.index("MCP")

    snippet = _snippet(text, start, start + len("MCP"))

    assert "“The target MCP answer.”" in snippet


def test_empty_arguments_and_missing_sections_return_stable_errors(
    tmp_path: Path,
) -> None:
    path = tmp_path / "errors.html"
    path.write_bytes(_html("Error document", "用于验证参数和范围错误。"))
    _ingest(tmp_path, [path])
    reader = RetrievalReadService(tmp_path).bind_current()
    document = reader.list_documents().documents[0]

    with pytest.raises(RetrievalFailure) as empty_term:
        reader.scan_term("   ")
    assert empty_term.value.code == "invalid_argument"

    with pytest.raises(RetrievalFailure) as invalid_scope:
        reader.list_blocks(
            document.doc_id,
            [["Missing section"]],
            include_descendants=False,
        )
    assert invalid_scope.value.code == "scope_not_found"

    with pytest.raises(RetrievalFailure) as missing_scope_mode:
        reader.list_blocks(document.doc_id, [["Alpha"]])
    assert missing_scope_mode.value.code == "invalid_argument"


def test_missing_active_collection_is_not_an_empty_library(tmp_path: Path) -> None:
    with pytest.raises(RetrievalFailure) as unavailable:
        RetrievalReadService(tmp_path).bind_current()

    assert unavailable.value.code == "collection_unavailable"


def test_tampered_snapshot_document_checksum_is_not_silently_ignored(
    tmp_path: Path,
) -> None:
    path = tmp_path / "corrupt.html"
    path.write_bytes(_html("Corrupt document", "用于验证快照文档损坏。"))
    snapshot_id = _ingest(tmp_path, [path])
    manifest_path = tmp_path / "collections" / snapshot_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = next(
        item for item in manifest["documents"][0]["artifact_refs"] if item["kind"] == "document"
    )
    artifact_path = tmp_path / "collections" / snapshot_id / artifact["snapshot_ref"]
    persisted = json.loads(artifact_path.read_text(encoding="utf-8"))
    persisted["document"]["title"] = "Tampered title"
    artifact_path.write_text(
        json.dumps(persisted, ensure_ascii=False),
        encoding="utf-8",
    )

    reader = RetrievalReadService(tmp_path).bind_current()
    with pytest.raises(RetrievalFailure) as corrupted:
        reader.list_documents()

    assert corrupted.value.code == "snapshot_integrity_error"
