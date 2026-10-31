from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.agent.corpus_grounded_coverage import (
    TopicCoverageFailure,
    execute_corpus_grounded_coverage,
)
from app.providers.chat import FakeChatProvider
from app.providers.embeddings import FakeEmbeddingProvider
from app.retrieval.hybrid import DEFAULT_HYBRID_RETRIEVAL_PROFILE, FusedCandidate
from app.retrieval.models import (
    GetOutlineResult,
    RetrievalBlock,
    RetrievalDocumentOutline,
    RetrievalOutlineItem,
    RetrievalOutlineSection,
    RetrievalSectionContext,
    SearchBlocksResult,
)
from app.retrieval.service import HybridSearchExecution


class _Reader:
    """为 corpus-grounded 流程返回固定 seed 和双通道查询结果。"""

    def __init__(
        self,
        *,
        topic: str,
        seed: list[RetrievalBlock],
        dense: dict[str, list[RetrievalBlock]],
        keyword: dict[str, list[RetrievalBlock]],
    ) -> None:
        self._topic = topic
        self._seed = seed
        self._dense = dense
        self._keyword = keyword
        self.calls: list[str] = []

    async def execute_search_blocks(
        self,
        query: str,
        *,
        provider: Any,
        doc_ids: list[str] | None = None,
        profile: Any = DEFAULT_HYBRID_RETRIEVAL_PROFILE,
        mode: str = "hybrid",
    ) -> HybridSearchExecution:
        """返回固定结果，并保留排序所需的通道分数。"""

        del provider, doc_ids
        self.calls.append(f"{mode}:{query}")
        blocks = (
            self._seed
            if mode == "hybrid" and query == self._topic
            else self._dense[query]
            if mode == "dense"
            else self._keyword[query]
        )
        candidates = tuple(
            FusedCandidate(
                block_id=block.block_id,
                doc_id=block.doc_id,
                block_index=index,
                score=1.0 / (profile.rrf_k + index),
                dense_rank=index if mode == "dense" else None,
                keyword_rank=index if mode == "keyword" else None,
                dense_score=float(index) if mode == "dense" else None,
                keyword_score=float(10 - index) if mode == "keyword" else None,
            )
            for index, block in enumerate(blocks, start=1)
        )
        return HybridSearchExecution(
            result=SearchBlocksResult(blocks=blocks),
            profile=profile,
            query_request_id=f"request-{len(self.calls)}",
            query_input_tokens=10 if mode == "dense" else 0,
            dense_block_ids=tuple(block.block_id for block in blocks) if mode == "dense" else (),
            keyword_block_ids=tuple(block.block_id for block in blocks)
            if mode == "keyword"
            else (),
            fused_candidates=candidates,
        )

    def get_outline(self, doc_ids: list[str]) -> GetOutlineResult:
        """返回 seed block 所属文档的最小章节大纲。"""

        blocks_by_doc: dict[str, RetrievalBlock] = {block.doc_id: block for block in self._seed}
        documents = []
        for doc_id in doc_ids:
            block = blocks_by_doc.get(doc_id)
            if block is None:
                raise AssertionError(f"unexpected outline doc_id: {doc_id}")
            section = RetrievalOutlineSection(
                path=block.section_path,
                ordinal="1",
                title=block.section_path[-1],
                depth=0,
                own_blocks=1,
                own_context_tokens=10,
                subtree_blocks=1,
                subtree_context_tokens=10,
                first_line=block.text[:80],
            )
            documents.append(
                RetrievalOutlineItem(
                    doc_id=doc_id,
                    document=RetrievalDocumentOutline(
                        doc_id=doc_id,
                        doc_title=block.doc_title,
                        source_label=block.source_label,
                        sections=[section],
                    ),
                )
            )
        return GetOutlineResult(documents=documents)


def _block(block_id: str, *, text: str) -> RetrievalBlock:
    """构造一个完整检索块。"""

    return RetrievalBlock(
        block_id=block_id,
        doc_id=f"doc-{block_id}",
        doc_title=f"Document {block_id}",
        source_label="test",
        section_path=["Section"],
        section_context=RetrievalSectionContext(
            section_depth=1,
            has_child_sections=False,
            document_section_count=1,
        ),
        text=text,
    )


def test_corpus_grounded_coverage_returns_all_supported_blocks() -> None:
    topic = "Agent isolation"
    retained_seed_tail = "The final seed sentence is intentionally retained."
    direct_block = _block(
        "direct",
        text="Runtime isolation uses a sandbox and limits filesystem access.",
    )
    partial_block = _block(
        "partial",
        text="A separate workspace prevents parallel sessions from colliding.",
    )
    seed_block = _block(
        "seed",
        text=("Seed evidence about agent isolation. " * 20) + retained_seed_tail,
    )
    reader = _Reader(
        topic=topic,
        seed=[seed_block],
        dense={
            "runtime isolation": [direct_block, partial_block],
        },
        keyword={
            "sandbox isolation": [partial_block],
        },
    )
    chat = FakeChatProvider(
        [
            json.dumps(
                {
                    "topic": topic,
                    "explanation": "# Agent isolation\n\nUse runtime and workspace boundaries.",
                }
            ),
            json.dumps(
                {
                    "facets": [
                        {
                            "name": "Runtime isolation",
                            "scope": "Restrict runtime and filesystem access.",
                            "evidence_obligation": (
                                "Find mechanisms that restrict filesystem access."
                            ),
                            "coverage_question": ("How does runtime isolation limit file access?"),
                            "required_relation": (
                                "The evidence must connect a runtime mechanism "
                                "to a file-access restriction."
                            ),
                            "origin": "both",
                            "seed_refs": ["S1"],
                            "semantic_queries": ["runtime isolation"],
                            "keyword_queries": ["sandbox isolation"],
                        }
                    ]
                }
            ),
            json.dumps(
                {
                    "facets": [
                        {
                            "name": "Runtime isolation",
                            "scope": "Restrict runtime and filesystem access.",
                            "evidence_obligation": (
                                "Find mechanisms that restrict filesystem access."
                            ),
                            "coverage_question": ("How does runtime isolation limit file access?"),
                            "required_relation": (
                                "The evidence must connect a runtime mechanism "
                                "to a file-access restriction."
                            ),
                            "origin": "both",
                            "seed_refs": ["S1"],
                            "semantic_queries": ["runtime isolation"],
                            "keyword_queries": ["sandbox isolation"],
                        }
                    ],
                    "changes": [],
                }
            ),
            json.dumps(
                {
                    "evaluations": [
                        {
                            "block_id": "direct",
                        }
                    ]
                }
            ),
            json.dumps(
                {
                    "evaluations": [
                        {
                            "block_id": "direct",
                            "judgments": [
                                {
                                    "facet": "Runtime isolation",
                                    "relevance": "direct",
                                    "evidence": "Describes sandbox and filesystem limits.",
                                }
                            ],
                        },
                        {
                            "block_id": "partial",
                            "judgments": [
                                {
                                    "facet": "Runtime isolation",
                                    "relevance": "partial",
                                    "evidence": "Describes workspace separation.",
                                }
                            ],
                        },
                    ]
                }
            ),
        ]
    )

    execution = asyncio.run(
        execute_corpus_grounded_coverage(
            topic=topic,
            reader=reader,
            embedding_provider=FakeEmbeddingProvider(),
            chat_provider=chat,
        )
    )

    assert execution.model_calls == 5
    assert {block.block_id for block in execution.payload.blocks} == {
        "direct",
        "partial",
    }
    assert execution.diagnostics["strategy"] == "corpus-grounded-coverage-v10"
    assert execution.diagnostics["facet_shortlist_limit"] == 20
    assert execution.diagnostics["initial_facet_count"] == 1
    assert execution.diagnostics["facet_audit_changes"] == []
    assert execution.diagnostics["seed_evidence_text_mode"] == "full_block"
    assert execution.diagnostics["seed_document_count"] == 1
    assert execution.diagnostics["seed_outline_section_count"] == 1
    assert execution.diagnostics["coverage_input_scope"] == "topic_only"
    assert execution.diagnostics["shortlist_count"] == 2
    assert execution.diagnostics["supported_count"] == 2
    assert execution.diagnostics["support_counts"] == {
        "direct": 1,
        "partial": 1,
    }
    assert reader.calls == [
        f"hybrid:{topic}",
        "dense:runtime isolation",
        "keyword:sandbox isolation",
    ]
    explanation_payload = json.loads(chat.requests[0][-1].content)
    facet_payload = json.loads(chat.requests[1][-1].content)
    audit_payload = json.loads(chat.requests[2][-1].content)
    rerank_payload = json.loads(chat.requests[4][-1].content)
    assert explanation_payload == {"topic": topic}
    assert facet_payload["topic"] == topic
    assert facet_payload["seed_evidence"][0]["text"].endswith(retained_seed_tail)
    assert facet_payload["seed_documents"][0]["sections"][0]["path"] == ["Section"]
    assert audit_payload["initial_facets"][0]["name"] == "Runtime isolation"
    assert rerank_payload["facet_catalog"][0]["evidence_obligation"] == (
        "Find mechanisms that restrict filesystem access."
    )
    assert rerank_payload["facet_catalog"][0]["coverage_question"] == (
        "How does runtime isolation limit file access?"
    )


def test_corpus_grounded_coverage_allows_reranker_to_omit_unsupported_candidate() -> None:
    topic = "Agent isolation"
    supported_block = _block(
        "supported",
        text="Runtime isolation uses a sandbox and filesystem boundary.",
    )
    unsupported_block = _block(
        "unsupported",
        text="A general introduction to agents.",
    )
    reader = _Reader(
        topic=topic,
        seed=[supported_block],
        dense={"runtime isolation": [supported_block, unsupported_block]},
        keyword={"sandbox isolation": [supported_block]},
    )
    chat = FakeChatProvider(
        [
            json.dumps(
                {
                    "topic": topic,
                    "explanation": "# Agent isolation\n\nRuntime boundaries.",
                }
            ),
            json.dumps(
                {
                    "facets": [
                        {
                            "name": "Runtime isolation",
                            "scope": "Restrict runtime and filesystem access.",
                            "evidence_obligation": (
                                "Find mechanisms that restrict filesystem access."
                            ),
                            "coverage_question": ("How does runtime isolation limit file access?"),
                            "required_relation": (
                                "The evidence must connect a runtime mechanism "
                                "to a file-access restriction."
                            ),
                            "origin": "both",
                            "seed_refs": ["S1"],
                            "semantic_queries": ["runtime isolation"],
                            "keyword_queries": ["sandbox isolation"],
                        }
                    ]
                }
            ),
            json.dumps(
                {
                    "facets": [
                        {
                            "name": "Runtime isolation",
                            "scope": "Restrict runtime and filesystem access.",
                            "evidence_obligation": (
                                "Find mechanisms that restrict filesystem access."
                            ),
                            "coverage_question": ("How does runtime isolation limit file access?"),
                            "required_relation": (
                                "The evidence must connect a runtime mechanism "
                                "to a file-access restriction."
                            ),
                            "origin": "both",
                            "seed_refs": ["S1"],
                            "semantic_queries": ["runtime isolation"],
                            "keyword_queries": ["sandbox isolation"],
                        }
                    ],
                    "changes": [],
                }
            ),
            json.dumps(
                {
                    "evaluations": [
                        {
                            "block_id": "supported",
                            "judgments": [
                                {
                                    "facet": "Runtime isolation",
                                    "relevance": "direct",
                                    "evidence": "Describes a runtime boundary.",
                                }
                            ],
                        }
                    ]
                }
            ),
        ]
    )

    execution = asyncio.run(
        execute_corpus_grounded_coverage(
            topic=topic,
            reader=reader,
            embedding_provider=FakeEmbeddingProvider(),
            chat_provider=chat,
        )
    )

    assert [block.block_id for block in execution.payload.blocks] == ["supported"]
    assert execution.diagnostics["shortlist_count"] == 2
    assert execution.diagnostics["supported_count"] == 1


@pytest.mark.parametrize("seed_refs", [[], ["S99"]])
def test_corpus_grounded_coverage_rejects_invalid_corpus_seed_refs(
    seed_refs: list[str],
) -> None:
    topic = "Agent isolation"
    seed_block = _block("seed", text="Seed evidence about agent isolation.")
    reader = _Reader(
        topic=topic,
        seed=[seed_block],
        dense={},
        keyword={},
    )
    chat = FakeChatProvider(
        [
            json.dumps(
                {
                    "topic": topic,
                    "explanation": "# Agent isolation",
                }
            ),
            json.dumps(
                {
                    "facets": [
                        {
                            "name": "Runtime isolation",
                            "scope": "Restrict runtime access.",
                            "evidence_obligation": "Find a runtime restriction.",
                            "coverage_question": "What restricts runtime access?",
                            "required_relation": ("The mechanism must restrict runtime access."),
                            "origin": "corpus",
                            "seed_refs": seed_refs,
                            "semantic_queries": ["runtime isolation"],
                            "keyword_queries": ["sandbox isolation"],
                        }
                    ]
                }
            ),
        ]
    )

    with pytest.raises(TopicCoverageFailure) as raised:
        asyncio.run(
            execute_corpus_grounded_coverage(
                topic=topic,
                reader=reader,
                embedding_provider=FakeEmbeddingProvider(),
                chat_provider=chat,
            )
        )

    assert raised.value.code == "coverage_plan_invalid"
