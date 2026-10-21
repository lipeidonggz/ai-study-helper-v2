from __future__ import annotations

import asyncio
from pathlib import Path

import lancedb

from app.indexing.service import IndexBuildService
from app.ingestion.blocks import build_block_set
from app.ingestion.extract import extract_html_document
from app.ingestion.sections import build_section_tree
from app.providers.embeddings import FakeEmbeddingProvider
from tests.helpers import fixture_bytes, source_artifact_for_bytes


def _build_document() -> tuple[object, object]:
    raw = fixture_bytes("article.html")
    source = source_artifact_for_bytes("article.html", raw)
    extracted = extract_html_document(source, raw.decode("utf-8"))
    section_tree = build_section_tree(
        extracted,
        doc_id="doc_0123456789abcdef0123456789abcdef",
        doc_version=1,
    )
    block_set = build_block_set(
        doc_id="doc_0123456789abcdef0123456789abcdef",
        doc_version=1,
        extracted=extracted,
        section_tree=section_tree,
    )
    return section_tree, block_set


def test_candidate_index_has_same_dense_and_keyword_block_coverage(tmp_path: Path) -> None:
    section_tree, block_set = _build_document()
    job_id = "a" * 32
    (tmp_path / "jobs" / job_id).mkdir(parents=True)
    provider = FakeEmbeddingProvider()
    service = IndexBuildService(tmp_path)

    bundle = asyncio.run(
        service.build_candidate(
            job_id=job_id,
            document_title="Agent 隔离设计",
            section_tree=section_tree,
            block_set=block_set,
            provider=provider,
        )
    )

    expected_ids = [block.block_id for block in block_set.blocks]
    assert bundle.status == "candidate"
    assert [record.block_id for record in bundle.block_indexes] == expected_ids
    assert bundle.usage_preview.block_count == len(expected_ids)
    assert bundle.usage.request_count >= 1
    assert bundle.embedding_profile.dimension == 1024
    assert bundle.embedding_profile.output_type == "dense"
    assert bundle.embedding_profile.text_type == "document"
    assert bundle.block_set_hash == block_set.block_set_hash

    lancedb_directory = tmp_path / bundle.lancedb_ref
    table = lancedb.connect(str(lancedb_directory)).open_table("blocks")
    rows = table.search().limit(1000).to_list()
    assert {row["block_id"] for row in rows} == set(expected_ids)
    assert all(row["vector_dimension"] == 1024 for row in rows)
    assert all(row["dense_and_keyword_present"] is True for row in rows)

    fts_rows = table.search("沙箱 网络", query_type="fts").limit(5).to_list()
    assert fts_rows
    assert any("沙箱" in row["search_text"] for row in fts_rows)

    assert (tmp_path / bundle.artifact_ref).is_file()
    assert not (tmp_path / "collections").exists()


def test_repeated_candidate_build_reuses_immutable_index(tmp_path: Path) -> None:
    section_tree, block_set = _build_document()
    job_id = "b" * 32
    (tmp_path / "jobs" / job_id).mkdir(parents=True)
    provider = FakeEmbeddingProvider()
    service = IndexBuildService(tmp_path)

    first = asyncio.run(
        service.build_candidate(
            job_id=job_id,
            document_title="Agent 隔离设计",
            section_tree=section_tree,
            block_set=block_set,
            provider=provider,
        )
    )
    calls_after_first = len(provider.calls)
    second = asyncio.run(
        service.build_candidate(
            job_id=job_id,
            document_title="Agent 隔离设计",
            section_tree=section_tree,
            block_set=block_set,
            provider=provider,
        )
    )

    assert second.index_id == first.index_id
    assert second.artifact_ref == first.artifact_ref
    assert len(provider.calls) == calls_after_first


def test_candidate_build_rejects_empty_block_set(tmp_path: Path) -> None:
    section_tree, block_set = _build_document()
    job_id = "c" * 32
    (tmp_path / "jobs" / job_id).mkdir(parents=True)
    empty_blocks = block_set.model_copy(update={"blocks": []})

    try:
        asyncio.run(
            IndexBuildService(tmp_path).build_candidate(
                job_id=job_id,
                document_title="空文档",
                section_tree=section_tree,
                block_set=empty_blocks,
                provider=FakeEmbeddingProvider(),
            )
        )
    except Exception as error:
        assert getattr(error, "code", None) == "index_empty_block_set"
    else:
        raise AssertionError("empty block sets must fail instead of building an empty index")


def test_candidate_build_rejects_section_and_block_identity_mismatch(tmp_path: Path) -> None:
    section_tree, block_set = _build_document()
    job_id = "d" * 32
    (tmp_path / "jobs" / job_id).mkdir(parents=True)
    mismatched = block_set.model_copy(update={"doc_version": 2})

    try:
        asyncio.run(
            IndexBuildService(tmp_path).build_candidate(
                job_id=job_id,
                document_title="身份错配",
                section_tree=section_tree,
                block_set=mismatched,
                provider=FakeEmbeddingProvider(),
            )
        )
    except Exception as error:
        assert getattr(error, "code", None) == "index_document_mismatch"
    else:
        raise AssertionError("doc version mismatch must fail before embedding")
