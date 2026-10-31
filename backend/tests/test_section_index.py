from __future__ import annotations

import asyncio
from typing import cast

import pytest

from app.providers.embeddings import FakeEmbeddingProvider
from app.retrieval.errors import RetrievalFailure
from app.retrieval.models import (
    ListBlocksResult,
    RetrievalBlock,
    RetrievalDocumentOutline,
    RetrievalOutlineSection,
    RetrievalSectionContext,
)
from app.retrieval.section_index import (
    SectionIndex,
    build_section_vector_index,
    expand_section_blocks,
)


def _section(
    title: str,
    *,
    depth: int = 2,
    subtree_blocks: int = 1,
    first_line: str = "",
) -> RetrievalOutlineSection:
    path = ["Document title", title]
    return RetrievalOutlineSection(
        path=path,
        ordinal=f"1.{depth}",
        title=title,
        depth=depth,
        own_blocks=subtree_blocks,
        own_context_tokens=subtree_blocks * 20,
        subtree_blocks=subtree_blocks,
        subtree_context_tokens=subtree_blocks * 20,
        first_line=first_line,
    )


def _document() -> RetrievalDocumentOutline:
    return RetrievalDocumentOutline(
        doc_id="doc_alpha",
        doc_title="Document title",
        source_label="test",
        sections=[
            RetrievalOutlineSection(
                path=["Document title"],
                ordinal="1",
                title="Document title",
                depth=1,
                own_blocks=1,
                own_context_tokens=20,
                subtree_blocks=8,
                subtree_context_tokens=160,
                first_line="Document introduction.",
            ),
            _section(
                "Context isolation",
                first_line="Separate context windows prevent state contamination.",
            ),
            _section("Acknowledgements"),
            _section("​JavaScript"),
            _section("Conclusion", first_line="The conclusion summarizes the design."),
            _section("Empty section", subtree_blocks=0),
            _section("MCP servers#", first_line="MCP configuration controls tool access."),
        ],
    )


def test_section_index_classifies_roots_noise_and_empty_sections() -> None:
    index = SectionIndex.from_documents("s_0123456789abcdef", [_document()])

    by_title = {entry.title: entry for entry in index.entries}

    assert by_title["Document title"].kind == "document_root"
    assert by_title["Document title"].routing_eligible is False
    assert by_title["Context isolation"].kind == "facet"
    assert by_title["Context isolation"].routing_eligible is True
    assert by_title["Acknowledgements"].kind == "boilerplate"
    assert by_title["JavaScript"].kind == "code_example"
    assert by_title["Conclusion"].kind == "generic"
    assert by_title["Conclusion"].routing_eligible is True
    assert by_title["Empty section"].routing_eligible is False
    assert by_title["MCP servers"].title == "MCP servers"
    assert len({entry.section_id for entry in index.entries}) == len(index.entries)
    assert index.stats.documents == 1
    assert index.stats.routing_eligible == 3


def test_section_lexical_search_is_stable_and_favours_specific_headings() -> None:
    index = SectionIndex.from_documents("s_0123456789abcdef", [_document()])

    first = index.search_lexical("context isolation state", top_k=3)
    second = index.search_lexical("context isolation state", top_k=3)

    assert [(item.entry.section_id, item.score) for item in first] == [
        (item.entry.section_id, item.score) for item in second
    ]
    assert first[0].entry.title == "Context isolation"
    assert first[0].lexical_rank == 1


def test_section_vector_index_is_snapshot_bound() -> None:
    index = SectionIndex.from_documents("s_0123456789abcdef", [_document()])
    provider = FakeEmbeddingProvider()

    vector_index = asyncio.run(build_section_vector_index(index, provider=provider))

    assert len(vector_index.vectors) == index.stats.routing_eligible
    assert vector_index.section_index.snapshot_id == index.snapshot_id
    assert all(
        len(vector) == provider.profile.dimension for vector in vector_index.vectors.values()
    )

    other = SectionIndex.from_documents("s_ffffffffffffffff", [_document()])
    with pytest.raises(RetrievalFailure) as mismatch:
        asyncio.run(
            other.search_dense(
                "context",
                provider=provider,
                vector_index=vector_index,
                top_k=3,
            )
        )
    assert mismatch.value.code == "snapshot_integrity_error"


def test_section_hybrid_search_keeps_both_route_provenance() -> None:
    index = SectionIndex.from_documents("s_0123456789abcdef", [_document()])
    provider = FakeEmbeddingProvider()
    vector_index = asyncio.run(build_section_vector_index(index, provider=provider))
    query = next(entry.search_text for entry in index.entries if entry.title == "Context isolation")

    hits = asyncio.run(
        index.search_hybrid(
            query,
            provider=provider,
            vector_index=vector_index,
            top_k=3,
        )
    )

    assert hits[0].entry.title == "Context isolation"
    assert hits[0].lexical_rank is not None
    assert hits[0].dense_rank is not None


class _FakeReader:
    def list_blocks(
        self,
        doc_id: str,
        section_paths: list[list[str]] | None = None,
        *,
        include_descendants: bool | None = None,
    ) -> ListBlocksResult:
        assert doc_id == "doc_alpha"
        assert section_paths == [["Document title", "Context isolation"]]
        assert include_descendants is True
        return ListBlocksResult(
            scope="subtree",
            total=2,
            blocks=[
                RetrievalBlock(
                    block_id="doc_alpha#b1",
                    doc_id=doc_id,
                    doc_title="Document title",
                    source_label="test",
                    section_path=["Document title", "Context isolation"],
                    section_context=RetrievalSectionContext(
                        section_depth=2,
                        has_child_sections=False,
                        document_section_count=7,
                    ),
                    text="Context isolation separates state.",
                ),
                RetrievalBlock(
                    block_id="doc_alpha#b2",
                    doc_id=doc_id,
                    doc_title="Document title",
                    source_label="test",
                    section_path=["Document title", "Context isolation"],
                    section_context=RetrievalSectionContext(
                        section_depth=2,
                        has_child_sections=False,
                        document_section_count=7,
                    ),
                    text="Each agent receives a separate context window.",
                ),
            ],
        )


def test_expand_section_blocks_preserves_provenance_and_budget() -> None:
    index = SectionIndex.from_documents("s_0123456789abcdef", [_document()])
    candidate = next(
        item
        for item in index.search_lexical("context isolation", top_k=3)
        if item.entry.title == "Context isolation"
    )
    reader = cast("object", _FakeReader())

    expanded = expand_section_blocks(
        reader,  # type: ignore[arg-type]
        [candidate, candidate],
        max_blocks=2,
        max_tokens=10_000,
    )

    assert [item.block.block_id for item in expanded] == [
        "doc_alpha#b1",
        "doc_alpha#b2",
    ]
    assert expanded[0].section_id == candidate.entry.section_id
    assert expanded[0].section_rank == 1


def test_empty_query_and_invalid_budget_are_explicit_errors() -> None:
    index = SectionIndex.from_documents("s_0123456789abcdef", [_document()])

    with pytest.raises(RetrievalFailure) as empty_query:
        index.search_lexical("   ")
    assert empty_query.value.code == "invalid_argument"

    with pytest.raises(RetrievalFailure) as invalid_budget:
        expand_section_blocks(_FakeReader(), [], max_blocks=0)  # type: ignore[arg-type]
    assert invalid_budget.value.code == "invalid_argument"


def test_empty_section_index_is_not_reported_as_successful_search() -> None:
    index = SectionIndex.from_documents(
        "s_0123456789abcdef",
        [
            RetrievalDocumentOutline(
                doc_id="doc_empty",
                doc_title="Empty document",
                source_label="test",
                sections=[],
            )
        ],
    )

    with pytest.raises(RetrievalFailure) as unavailable:
        index.search_lexical("topic")
    assert unavailable.value.code == "no_semantic_sections"
