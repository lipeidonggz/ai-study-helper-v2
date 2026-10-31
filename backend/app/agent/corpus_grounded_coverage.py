from __future__ import annotations

import asyncio
import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.providers.chat import ChatMessage, ChatProvider, ChatProviderError
from app.providers.embeddings import EmbeddingProvider
from app.retrieval.hybrid import (
    DEFAULT_HYBRID_RETRIEVAL_PROFILE,
    HybridRetrievalProfile,
)
from app.retrieval.models import RetrievalBlock, SearchBlocksResult
from app.retrieval.service import HybridSearchExecution, RetrievalSnapshotReader
from app.tracing.store import TraceSession

CORPUS_GROUNDED_STRATEGY_VERSION = "corpus-grounded-coverage-v10"
EXPLANATION_PROMPT_VERSION = "corpus-grounded-explanation-v1"
FACET_PROMPT_VERSION = "corpus-grounded-facet-planner-v6"
FACET_AUDIT_PROMPT_VERSION = "corpus-grounded-facet-audit-v3"
RERANK_PROMPT_VERSION = "corpus-grounded-reranker-v3"
SEED_TOP_K = 50
# 当前优先验证覆盖正确性；固定 Top-20 是临时基线，最终预算另行实验。
FACET_SHORTLIST_LIMIT = 20
RERANK_BATCH_SIZE = 8
SEARCH_CONCURRENCY = 3
RERANK_CONCURRENCY = 3
RERANK_MAX_ATTEMPTS = 2
SEED_OUTLINE_FIRST_LINE_CHARS = 200

QueryMode = Literal["dense", "keyword"]

EXPLANATION_PROMPT = """<role>
你负责为一个知识库检索主题生成完整解释。你不回答用户问题，也不生成查询。
</role>

<task>
请对 topic 做详细、完备、具体的解释，并用 Markdown 组织。
解释需要覆盖概念、目标、对象、机制、实现方式、边界、场景、风险和权衡，
并保留具体技术词与限制关系。
</task>

<rules>
1. explanation 字段必须是 Markdown 字符串。
2. 不假设当前知识库中一定包含某个答案。
3. 不输出检索查询或额外说明。
4. 只输出合法 JSON：
{"topic":"...","explanation":"# ..."}
</rules>
"""

FACET_PROMPT = """<role>
你是主题覆盖检索规划器。你的任务不是回答问题，而是为一个知识库主题
生成可执行、可验证的语义覆盖面（facets）和检索输入。
</role>

<task>
输入包含 topic、explanation，以及先用 topic 做 hybrid 检索后观察到的：
- seed_evidence：命中的完整正文 block；
- seed_documents：这些命中 block 所属文档的章节大纲和章节首句。

请同时利用主题解释、命中正文和文档结构生成 facets。seed_documents 用于发现
同一文档中与命中章节并列、相邻或上下位相关的证据面，不代表这些章节已经命中。
不能只按解释机械切段，也不能把查询生成限制在单一分类框架或少数常见主题。
</task>

<rules>
1. 每个 facet 表示一种可验证的证据义务，而不是一个泛化分类。
2. evidence_obligation 说明必须找到哪些对象、机制、状态、流程或风险。
3. coverage_question 是检索完成后应能明确回答的具体问题。
4. required_relation 说明证据必须表达的具体限制、机制、状态、因果或作用关系。
5. 优先保留知识库中出现或明确暗示的具体对象、机制、状态、流程和风险。
6. 解释中重要但 seed 未覆盖的语义面可以作为 hypothesis 保留。
7. origin 只能是 corpus、hypothesis 或 both。
8. corpus 和 both facet 的 seed_refs 必须非空且只能引用输入中的 seed id；
   hypothesis facet 可以没有 seed_refs。
9. semantic_queries 使用完整、自然、可独立理解的表达。
10. keyword_queries 使用短小、高信息量的词项组合；有检索价值时提供中英文。
11. 不同 facet 不得只是同义改写；不得用一个“所有方式”的总分类代替细节。
12. 只输出合法 JSON：
{
  "facets": [
    {
      "name": "...",
      "scope": "...",
      "evidence_obligation": "...",
      "coverage_question": "...",
      "required_relation": "...",
      "origin": "corpus|hypothesis|both",
      "seed_refs": ["S1"],
      "semantic_queries": ["..."],
      "keyword_queries": ["..."]
    }
  ]
}
</rules>
"""

FACET_AUDIT_PROMPT = """<role>
你是主题覆盖完整性审计器。你不回答用户问题，也不执行检索。你的任务是检查
初版 facets 是否存在缺项、过宽、重复或无法验证的问题，并输出最终 facets。
</role>

<task>
输入包含 topic、explanation、完整 seed_evidence、seed_documents 和初版 facets。
请逐项检查：
1. topic、explanation 或输入语料中出现的重要概念、机制、对象、状态、过程、
   风险和权衡，是否没有落入任何 facet；
2. 一个 facet 是否混合了多个可以独立检索和独立判断的证据义务；
3. 是否有多个 facet 只是同义改写或应当合并；
4. 是否存在泛化分类、无法指导检索或没有语料依据的 corpus facet；
5. 初始 facets 是否受单一解释、单一文档或单一分类框架影响，遗漏了 topic、
   explanation 或 seed evidence 直接支持的其他独立证据义务；
6. seed_documents 中是否存在尚未被覆盖、且根据输入可判断与主题有关的章节；
   只有能够说明该章节为何属于当前主题时才新增 facet。

最终 facets 是审计后的完整计划，必须保留初版中仍然有效、独立的证据义务。
新增或拆分 facet 只能由 topic、explanation 或输入语料自然支持，不得引入当前
输入之外的具体答案。
</task>

<rules>
1. 每个最终 facet 必须包含 name、scope、evidence_obligation、
   coverage_question、required_relation、origin、seed_refs、
   semantic_queries 和 keyword_queries。
2. corpus 和 both facet 的 seed_refs 必须非空，且只能引用 seed_evidence 中的 id。
3. 不得为了减少数量合并具有不同证据义务的 facet。
4. 不得为了增加数量制造同义 facet 或纯泛化分类。
5. changes 只记录本轮实际发生的 add、split、merge、rewrite、remove；
   没有变化时输出空数组。
6. changes 的 facet_names 必须来自初版或最终 facets。
7. 只输出合法 JSON：
{
  "facets": [
    {
      "name": "...",
      "scope": "...",
      "evidence_obligation": "...",
      "coverage_question": "...",
      "required_relation": "...",
      "origin": "corpus|hypothesis|both",
      "seed_refs": ["S1"],
      "semantic_queries": ["..."],
      "keyword_queries": ["..."]
    }
  ],
  "changes": [
    {
      "action": "add|split|merge|rewrite|remove",
      "facet_names": ["..."],
      "reason": "..."
    }
  ]
}
</rules>
"""

RERANK_PROMPT = """<role>
你是知识库证据相关性判断器。你判断候选 block 是否支持给定 facet，
不回答问题，也不补充候选之外的信息。
</role>

<rules>
1. 对每个 candidate 检查完整 facet_catalog；provenance_facets 只是召回提示。
2. 判断依据是 facet 的 evidence_obligation、coverage_question 和
   required_relation，不得只依据 name 或 scope。
3. direct：正文明确满足该 facet 的证据义务和必要关系。
4. partial：正文覆盖该 facet 的部分对象、机制或风险，但没有充分满足关系。
5. 不能因为出现相同词语就判 direct。
6. 一个 block 可以支持多个 facets；没有支持的 facets 不输出。
7. facet 和 block_id 必须逐字来自输入。
8. 只输出合法 JSON：
{
  "evaluations": [
    {
      "block_id": "...",
      "judgments": [
        {"facet": "...", "relevance": "direct|partial", "evidence": "..."}
      ]
    }
  ]
}
</rules>
"""

EXPLANATION_PROMPT_HASH = hashlib.sha256(EXPLANATION_PROMPT.encode("utf-8")).hexdigest()
FACET_PROMPT_HASH = hashlib.sha256(FACET_PROMPT.encode("utf-8")).hexdigest()
FACET_AUDIT_PROMPT_HASH = hashlib.sha256(FACET_AUDIT_PROMPT.encode("utf-8")).hexdigest()
RERANK_PROMPT_HASH = hashlib.sha256(RERANK_PROMPT.encode("utf-8")).hexdigest()


class ExplanationOutput(BaseModel):
    """主题解释。"""

    topic: str
    explanation: str = Field(min_length=1)


class GroundedFacet(BaseModel):
    """一个可检索、可判断覆盖的语义面。"""

    name: str
    scope: str
    evidence_obligation: str = Field(min_length=1)
    coverage_question: str = Field(min_length=1)
    required_relation: str = Field(min_length=1)
    origin: Literal["corpus", "hypothesis", "both"]
    seed_refs: list[str]
    semantic_queries: list[str] = Field(min_length=1)
    keyword_queries: list[str] = Field(min_length=1)


class FacetPlanOutput(BaseModel):
    """完整覆盖面计划。"""

    facets: list[GroundedFacet] = Field(min_length=1)


class FacetAuditChange(BaseModel):
    """facet 审计实际执行的一次结构变化。"""

    action: Literal["add", "split", "merge", "rewrite", "remove"]
    facet_names: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)


class FacetAuditOutput(BaseModel):
    """审计后的完整 facet 计划和变化记录。"""

    facets: list[GroundedFacet] = Field(min_length=1)
    changes: list[FacetAuditChange]


class FacetJudgment(BaseModel):
    """候选对一个 facet 的支持。"""

    facet: str
    relevance: Literal["direct", "partial"]
    evidence: str


class CandidateEvaluation(BaseModel):
    """一个候选的支持判断。"""

    block_id: str
    judgments: list[FacetJudgment]


class RerankOutput(BaseModel):
    """一批候选的支持判断。"""

    evaluations: list[CandidateEvaluation]


class TopicCoverageFailure(RuntimeError):
    """主题覆盖检索无法按合同完成。"""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        model_calls: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.model_calls = model_calls
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.diagnostics = diagnostics or {}


def _validate_facet_plan(
    facet_plan: FacetPlanOutput,
    *,
    seed_ids: set[str],
    usage: _Usage,
    stage: str,
) -> None:
    """校验 facet 唯一性和 corpus 证据引用，防止无效计划进入检索。"""

    facet_names = [facet.name for facet in facet_plan.facets]
    if len(set(facet_names)) != len(facet_names):
        raise TopicCoverageFailure(
            code="coverage_plan_invalid",
            message=f"{stage} returned duplicate facets",
            model_calls=usage.model_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
    for facet in facet_plan.facets:
        if not set(facet.seed_refs).issubset(seed_ids):
            raise TopicCoverageFailure(
                code="coverage_plan_invalid",
                message=f"{stage} facet '{facet.name}' references unknown seed ids",
                model_calls=usage.model_calls,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
        if facet.origin in {"corpus", "both"} and not facet.seed_refs:
            raise TopicCoverageFailure(
                code="coverage_plan_invalid",
                message=f"{stage} facet '{facet.name}' requires seed evidence",
                model_calls=usage.model_calls,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )


@dataclass(frozen=True, slots=True)
class CorpusGroundedCoverageExecution:
    """产品工具调用的结果、诊断和模型用量。"""

    payload: SearchBlocksResult
    diagnostics: dict[str, Any]
    model_calls: int
    input_tokens: int
    output_tokens: int


@dataclass(slots=True)
class _Usage:
    """累计模型调用数与非流式 token 用量。"""

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(slots=True)
class _Candidate:
    """跨查询合并后的候选及其 facet 局部支持。"""

    block: RetrievalBlock
    hits: list[dict[str, Any]] = field(default_factory=list)


def _seed_payload(blocks: list[RetrievalBlock]) -> list[dict[str, Any]]:
    """把种子结果整理为带标题和章节归属的完整正文证据。"""

    return [
        {
            "id": f"S{index}",
            "doc_id": block.doc_id,
            "doc_title": block.doc_title,
            "section_path": block.section_path,
            "text": block.text,
        }
        for index, block in enumerate(blocks, start=1)
    ]


def _seed_document_payload(
    reader: RetrievalSnapshotReader,
    blocks: list[RetrievalBlock],
) -> list[dict[str, Any]]:
    """读取 seed 文档的章节结构，补充尚未命中但结构相关的区域。"""

    doc_ids = list(dict.fromkeys(block.doc_id for block in blocks))
    if not doc_ids:
        return []
    outline = reader.get_outline(doc_ids)
    documents: list[dict[str, Any]] = []
    failed_doc_ids: list[str] = []
    for item in outline.documents:
        if item.document is None or item.error is not None:
            failed_doc_ids.append(item.doc_id)
            continue
        document = item.document
        documents.append(
            {
                "doc_id": document.doc_id,
                "doc_title": document.doc_title,
                "sections": [
                    {
                        "path": section.path,
                        "title": section.title,
                        "depth": section.depth,
                        "own_blocks": section.own_blocks,
                        "subtree_blocks": section.subtree_blocks,
                        "first_line": section.first_line[:SEED_OUTLINE_FIRST_LINE_CHARS],
                    }
                    for section in document.sections
                ],
            }
        )
    if failed_doc_ids:
        raise ValueError("seed document outlines unavailable: " + ", ".join(failed_doc_ids))
    return documents


async def _complete_json(
    chat_provider: ChatProvider,
    *,
    system_prompt: str,
    payload: dict[str, Any],
    usage: _Usage,
    error_code: str,
    trace_session: TraceSession | None = None,
    parent_span_id: str | None = None,
    call_purpose: str = "topic_coverage",
    prompt_version: str = "",
    prompt_hash: str = "",
) -> Any:
    """执行一次 JSON 模型调用并累计 usage。"""

    messages = (
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(
            role="user",
            content=json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ),
    )
    span = (
        trace_session.start_span(
            "llm.call",
            "LLM",
            parent_span_id=parent_span_id,
            attributes={
                "llm.provider": chat_provider.profile.provider,
                "llm.request.model": chat_provider.profile.model_id,
                "llm.invocation_parameters.temperature": (chat_provider.profile.temperature),
                "app.call_purpose": call_purpose,
                "app.prompt_version": prompt_version,
                "app.prompt_hash": prompt_hash,
            },
        )
        if trace_session is not None
        else None
    )
    if span is not None and trace_session is not None:
        prompt_artifact = trace_session.record_artifact(
            kind="llm.prompt",
            mime_type="application/json",
            content=[{"role": message.role, "content": message.content} for message in messages],
        )
        span.attributes["llm.prompt"] = prompt_artifact.model_dump(mode="json")
    try:
        result = await chat_provider.complete(
            messages,
            max_output_tokens=8_192,
        )
    except ChatProviderError as error:
        if span is not None and trace_session is not None:
            trace_session.end_span(
                span,
                status="error",
                error_code=error.code,
            )
        raise TopicCoverageFailure(
            code=error_code,
            message=f"{error_code}: {error.code}",
            model_calls=usage.model_calls + 1,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        ) from error
    usage.model_calls += 1
    usage.input_tokens += result.input_tokens
    usage.output_tokens += result.output_tokens
    if span is not None and trace_session is not None:
        output_artifact = trace_session.record_artifact(
            kind="llm.output",
            mime_type="text/plain; charset=utf-8",
            content=result.content,
        )
        span.attributes.update(
            {
                "llm.output": output_artifact.model_dump(mode="json"),
                "llm.finish_reason": result.finish_reason,
                "llm.token_count.prompt": result.input_tokens,
                "llm.token_count.completion": result.output_tokens,
                "llm.request.id": result.request_id,
                "app.usage_source": "provider",
            }
        )
        trace_session.end_span(span, status="ok")
    return result.content


def _normalized_scores(
    executions: Sequence[tuple[QueryMode, str, HybridSearchExecution]],
) -> dict[tuple[str, str, str], float]:
    """按单条查询归一化 dense distance 或 BM25 score。"""

    normalized: dict[tuple[str, str, str], float] = {}
    for mode, query, execution in executions:
        values = []
        for candidate in execution.fused_candidates:
            score = candidate.dense_score if mode == "dense" else candidate.keyword_score
            if score is not None:
                values.append((candidate.block_id, float(score)))
        if not values:
            continue
        low = min(score for _, score in values)
        high = max(score for _, score in values)
        for block_id, score in values:
            if high == low:
                value = 1.0
            elif mode == "dense":
                value = (high - score) / (high - low)
            else:
                value = (score - low) / (high - low)
            normalized[(mode, query.casefold(), block_id)] = value
    return normalized


async def execute_corpus_grounded_coverage(
    *,
    topic: str,
    reader: RetrievalSnapshotReader,
    embedding_provider: EmbeddingProvider,
    chat_provider: ChatProvider,
    search_profile: HybridRetrievalProfile = DEFAULT_HYBRID_RETRIEVAL_PROFILE,
    trace_session: TraceSession | None = None,
    parent_span_id: str | None = None,
) -> CorpusGroundedCoverageExecution:
    """执行 corpus-grounded 高召回检索，并返回全部有支持的相关块。"""

    usage = _Usage()
    normalized_topic = topic.strip()
    seed_profile = replace(
        search_profile,
        candidate_depth=max(search_profile.candidate_depth, 60),
        top_k=SEED_TOP_K,
    )
    seed_span = (
        trace_session.start_span(
            "retrieval.coverage_query",
            "INTERNAL",
            parent_span_id=parent_span_id,
            attributes={
                "app.query_kind": "seed",
                "app.search_mode": "hybrid",
                "app.query": normalized_topic,
            },
        )
        if trace_session is not None
        else None
    )
    try:
        seed_execution = await reader.execute_search_blocks(
            normalized_topic,
            provider=embedding_provider,
            doc_ids=None,
            profile=seed_profile,
            mode="hybrid",
        )
    except Exception as error:
        if seed_span is not None and trace_session is not None:
            trace_session.end_span(
                seed_span,
                status="error",
                error_code=str(getattr(error, "code", "coverage_seed_query_failed")),
            )
        code = str(getattr(error, "code", "coverage_seed_query_failed"))
        raise TopicCoverageFailure(
            code=code,
            message=str(getattr(error, "message", type(error).__name__)),
            model_calls=usage.model_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        ) from error
    if seed_span is not None and trace_session is not None:
        trace_session.end_span(
            seed_span,
            status="ok",
            attributes={"app.result_count": len(seed_execution.result.blocks)},
        )
    seed_evidence = _seed_payload(list(seed_execution.result.blocks))
    try:
        seed_documents = _seed_document_payload(
            reader,
            list(seed_execution.result.blocks),
        )
    except Exception as error:
        raise TopicCoverageFailure(
            code="coverage_seed_outline_failed",
            message=str(error),
            model_calls=usage.model_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        ) from error

    seed_ids = {item["id"] for item in seed_evidence}
    try:
        explanation_raw = await _complete_json(
            chat_provider,
            system_prompt=EXPLANATION_PROMPT,
            payload={
                "topic": normalized_topic,
            },
            usage=usage,
            error_code="coverage_explanation_failed",
            trace_session=trace_session,
            parent_span_id=parent_span_id,
            call_purpose="topic_coverage_explanation",
            prompt_version=EXPLANATION_PROMPT_VERSION,
            prompt_hash=EXPLANATION_PROMPT_HASH,
        )
        explanation = ExplanationOutput.model_validate_json(explanation_raw)
        facet_raw = await _complete_json(
            chat_provider,
            system_prompt=FACET_PROMPT,
            payload={
                "topic": normalized_topic,
                "explanation": explanation.explanation,
                "seed_evidence": seed_evidence,
                "seed_documents": seed_documents,
            },
            usage=usage,
            error_code="coverage_facet_planning_failed",
            trace_session=trace_session,
            parent_span_id=parent_span_id,
            call_purpose="topic_coverage_planning",
            prompt_version=FACET_PROMPT_VERSION,
            prompt_hash=FACET_PROMPT_HASH,
        )
        initial_facet_plan = FacetPlanOutput.model_validate_json(facet_raw)
        _validate_facet_plan(
            initial_facet_plan,
            seed_ids=seed_ids,
            usage=usage,
            stage="initial facet plan",
        )
        audit_raw = await _complete_json(
            chat_provider,
            system_prompt=FACET_AUDIT_PROMPT,
            payload={
                "topic": normalized_topic,
                "explanation": explanation.explanation,
                "seed_evidence": seed_evidence,
                "seed_documents": seed_documents,
                "initial_facets": [
                    facet.model_dump(mode="json") for facet in initial_facet_plan.facets
                ],
            },
            usage=usage,
            error_code="coverage_facet_audit_failed",
            trace_session=trace_session,
            parent_span_id=parent_span_id,
            call_purpose="topic_coverage_audit",
            prompt_version=FACET_AUDIT_PROMPT_VERSION,
            prompt_hash=FACET_AUDIT_PROMPT_HASH,
        )
        facet_audit = FacetAuditOutput.model_validate_json(audit_raw)
        facet_plan = FacetPlanOutput(facets=facet_audit.facets)
        _validate_facet_plan(
            facet_plan,
            seed_ids=seed_ids,
            usage=usage,
            stage="audited facet plan",
        )
    except ValidationError as error:
        raise TopicCoverageFailure(
            code="coverage_plan_invalid",
            message=f"corpus-grounded coverage returned invalid JSON: {error}",
            model_calls=usage.model_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        ) from error

    facet_by_name = {facet.name: facet for facet in facet_plan.facets}

    query_specs: dict[tuple[QueryMode, str], tuple[str, list[str]]] = {}
    for facet in facet_plan.facets:
        query_modes: tuple[tuple[QueryMode, list[str]], ...] = (
            ("dense", facet.semantic_queries),
            ("keyword", facet.keyword_queries),
        )
        for mode, queries in query_modes:
            for query in queries:
                normalized = query.strip()
                if not normalized:
                    continue
                key = (mode, normalized.casefold())
                existing = query_specs.get(key)
                if existing is None:
                    query_specs[key] = (normalized, [facet.name])
                elif facet.name not in existing[1]:
                    existing[1].append(facet.name)

    query_profile = replace(
        search_profile,
        candidate_depth=max(search_profile.candidate_depth, 60),
        top_k=20,
    )
    semaphore = asyncio.Semaphore(SEARCH_CONCURRENCY)

    async def run_query(
        mode: QueryMode,
        query: str,
        facets: list[str],
    ) -> tuple[QueryMode, str, HybridSearchExecution]:
        query_span = (
            trace_session.start_span(
                "retrieval.coverage_query",
                "INTERNAL",
                parent_span_id=parent_span_id,
                attributes={
                    "app.query_kind": "facet",
                    "app.search_mode": mode,
                    "app.query": query,
                    "app.facets": facets,
                },
            )
            if trace_session is not None
            else None
        )
        async with semaphore:
            try:
                execution = await reader.execute_search_blocks(
                    query,
                    provider=embedding_provider,
                    doc_ids=None,
                    profile=query_profile,
                    mode=mode,
                )
            except Exception as error:
                if query_span is not None and trace_session is not None:
                    trace_session.end_span(
                        query_span,
                        status="error",
                        error_code=str(getattr(error, "code", "coverage_query_failed")),
                    )
                raise
        if query_span is not None and trace_session is not None:
            trace_session.end_span(
                query_span,
                status="ok",
                attributes={
                    "app.result_count": len(execution.result.blocks),
                    "app.fused_candidate_count": len(execution.fused_candidates),
                },
            )
        return mode, query, execution

    try:
        executions = await asyncio.gather(
            *(run_query(mode, query, facets) for (mode, _), (query, facets) in query_specs.items())
        )
    except Exception as error:
        code = str(getattr(error, "code", "coverage_query_failed"))
        raise TopicCoverageFailure(
            code=code,
            message=str(getattr(error, "message", type(error).__name__)),
            model_calls=usage.model_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        ) from error

    candidates: dict[str, _Candidate] = {}
    for mode, query, execution in executions:
        memberships = query_specs[(mode, query.casefold())][1]
        for rank, block in enumerate(execution.result.blocks, start=1):
            candidate = candidates.setdefault(block.block_id, _Candidate(block=block))
            candidate.hits.extend(
                {
                    "mode": mode,
                    "query": query,
                    "rank": rank,
                    "facet": facet_name,
                }
                for facet_name in memberships
            )
    normalized_scores = _normalized_scores(executions)

    facet_entries: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for candidate in candidates.values():
        for hit in candidate.hits:
            normalized = normalized_scores.get(
                (hit["mode"], hit["query"].casefold(), candidate.block.block_id),
                0.0,
            )
            entry = facet_entries[hit["facet"]].setdefault(
                candidate.block.block_id,
                {
                    "candidate": candidate,
                    "best_normalized_score": 0.0,
                    "query_support": set(),
                    "modes": set(),
                },
            )
            entry["best_normalized_score"] = max(entry["best_normalized_score"], normalized)
            entry["query_support"].add((hit["mode"], hit["query"].casefold()))
            entry["modes"].add(hit["mode"])

    shortlist: dict[str, _Candidate] = {}
    provenance: dict[str, set[str]] = defaultdict(set)
    for facet_name, entries in facet_entries.items():
        ranked = sorted(
            entries.values(),
            key=lambda entry: (
                -entry["best_normalized_score"],
                -len(entry["query_support"]),
                -len(entry["modes"]),
                entry["candidate"].block.block_id,
            ),
        )
        for entry in ranked[:FACET_SHORTLIST_LIMIT]:
            candidate = entry["candidate"]
            shortlist[candidate.block.block_id] = candidate
            provenance[candidate.block.block_id].add(facet_name)

    shortlisted = list(shortlist.values())
    batches = [
        shortlisted[index : index + RERANK_BATCH_SIZE]
        for index in range(0, len(shortlisted), RERANK_BATCH_SIZE)
    ]
    rerank_semaphore = asyncio.Semaphore(RERANK_CONCURRENCY)

    async def rerank_batch(
        batch: list[_Candidate],
    ) -> dict[str, dict[str, str]]:
        async with rerank_semaphore:
            payload_candidates = [
                {
                    "block_id": candidate.block.block_id,
                    "text": candidate.block.text[:1_600],
                    "provenance_facets": [
                        {
                            "name": facet_name,
                            "scope": facet_by_name[facet_name].scope,
                            "evidence_obligation": facet_by_name[facet_name].evidence_obligation,
                            "coverage_question": facet_by_name[facet_name].coverage_question,
                            "required_relation": facet_by_name[facet_name].required_relation,
                        }
                        for facet_name in sorted(provenance[candidate.block.block_id])
                    ],
                }
                for candidate in batch
            ]
            facet_catalog = [
                {
                    "name": facet.name,
                    "scope": facet.scope,
                    "evidence_obligation": facet.evidence_obligation,
                    "coverage_question": facet.coverage_question,
                    "required_relation": facet.required_relation,
                }
                for facet in facet_plan.facets
            ]
            expected_ids = {candidate.block.block_id for candidate in batch}
            payload = {
                "topic": normalized_topic,
                "facet_catalog": facet_catalog,
                "candidates": payload_candidates,
            }
            for attempt in range(1, RERANK_MAX_ATTEMPTS + 1):
                raw = await _complete_json(
                    chat_provider,
                    system_prompt=RERANK_PROMPT,
                    payload=payload,
                    usage=usage,
                    error_code="coverage_rerank_failed",
                    trace_session=trace_session,
                    parent_span_id=parent_span_id,
                    call_purpose="topic_coverage_reranking",
                    prompt_version=RERANK_PROMPT_VERSION,
                    prompt_hash=RERANK_PROMPT_HASH,
                )
                try:
                    parsed = RerankOutput.model_validate_json(raw)
                    actual_ids = {item.block_id for item in parsed.evaluations}
                    if not actual_ids.issubset(expected_ids) or len(actual_ids) != len(
                        parsed.evaluations
                    ):
                        raise ValueError(
                            "corpus-grounded reranker returned unknown or duplicate candidate ids"
                        )
                except (ValidationError, ValueError) as error:
                    if attempt < RERANK_MAX_ATTEMPTS:
                        continue
                    raise TopicCoverageFailure(
                        code="coverage_rerank_invalid",
                        message=(
                            "corpus-grounded reranker returned invalid structured "
                            f"output after {attempt} attempts: {error}"
                        ),
                        model_calls=usage.model_calls,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        diagnostics={
                            "attempts": attempt,
                            "expected_candidate_count": len(expected_ids),
                        },
                    ) from error
                return {
                    evaluation.block_id: {
                        judgment.facet: judgment.relevance for judgment in evaluation.judgments
                    }
                    for evaluation in parsed.evaluations
                }
            raise AssertionError("reranker retry loop did not return or raise")

    try:
        rerank_results = await asyncio.gather(*(rerank_batch(batch) for batch in batches))
    except TopicCoverageFailure:
        raise
    support_by_block = {
        block_id: judgments
        for batch_result in rerank_results
        for block_id, judgments in batch_result.items()
    }
    supported = [
        candidate for block_id, candidate in shortlist.items() if support_by_block.get(block_id)
    ]
    supported.sort(
        key=lambda candidate: (
            -sum(
                relevance == "direct"
                for relevance in support_by_block[candidate.block.block_id].values()
            ),
            -len(support_by_block[candidate.block.block_id]),
            -facet_entries[next(iter(provenance[candidate.block.block_id]))][
                candidate.block.block_id
            ]["best_normalized_score"],
            candidate.block.block_id,
        )
    )

    return CorpusGroundedCoverageExecution(
        payload=SearchBlocksResult(
            blocks=[candidate.block for candidate in supported],
            empty_reason=None if supported else "no_match",
        ),
        diagnostics={
            "strategy": CORPUS_GROUNDED_STRATEGY_VERSION,
            "explanation_prompt_version": EXPLANATION_PROMPT_VERSION,
            "explanation_prompt_hash": EXPLANATION_PROMPT_HASH,
            "facet_prompt_version": FACET_PROMPT_VERSION,
            "facet_prompt_hash": FACET_PROMPT_HASH,
            "facet_audit_prompt_version": FACET_AUDIT_PROMPT_VERSION,
            "facet_audit_prompt_hash": FACET_AUDIT_PROMPT_HASH,
            "rerank_prompt_version": RERANK_PROMPT_VERSION,
            "rerank_prompt_hash": RERANK_PROMPT_HASH,
            "seed_query": normalized_topic,
            "seed_result_count": len(seed_evidence),
            "seed_document_count": len(seed_documents),
            "seed_outline_section_count": sum(
                len(document["sections"]) for document in seed_documents
            ),
            "seed_evidence_text_mode": "full_block",
            "coverage_input_scope": "topic_only",
            "initial_facet_count": len(initial_facet_plan.facets),
            "facet_count": len(facet_plan.facets),
            "facets": [facet.model_dump(mode="json") for facet in facet_plan.facets],
            "facet_audit_changes": [
                change.model_dump(mode="json") for change in facet_audit.changes
            ],
            "facet_shortlist_limit": FACET_SHORTLIST_LIMIT,
            "query_count": len(query_specs),
            "non_empty_query_count": sum(
                bool(execution.result.blocks) for _, _, execution in executions
            ),
            "candidate_count": len(candidates),
            "shortlist_count": len(shortlisted),
            "supported_count": len(supported),
            "support_counts": {
                "direct": sum(
                    relevance == "direct"
                    for judgments in support_by_block.values()
                    for relevance in judgments.values()
                ),
                "partial": sum(
                    relevance == "partial"
                    for judgments in support_by_block.values()
                    for relevance in judgments.values()
                ),
            },
        },
        model_calls=usage.model_calls,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )
