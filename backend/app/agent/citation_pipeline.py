from __future__ import annotations

from app.agent.citation_source_meta import (
    CitationSourceMetaClassifier,
)
from app.agent.citations import (
    CLAIM_SPAN_VERSION,
    CitationAnnotationArtifact,
    CitationAnnotationOutcome,
    CitationAnnotator,
    CitationEvidenceBlock,
    split_answer_spans,
)
from app.agent.token_counter import ObservationTokenCounter
from app.providers.chat import ChatProvider


class CitationPipeline:
    """串联来源门禁与 claim annotator，并返回一次完整引用管线审计。"""

    def __init__(
        self,
        provider: ChatProvider,
        *,
        token_counter: ObservationTokenCounter,
    ) -> None:
        self._provider = provider
        self._token_counter = token_counter
        self._source_meta = CitationSourceMetaClassifier(
            provider,
            token_counter=token_counter,
        )
        self._annotator = CitationAnnotator(
            provider,
            token_counter=token_counter,
        )

    async def annotate(
        self,
        *,
        question: str,
        answer: str,
        evidence_blocks: list[CitationEvidenceBlock],
        spent_input_tokens: int,
        spent_output_tokens: int,
        max_context_tokens: int,
        max_request_tokens: int,
    ) -> CitationAnnotationOutcome:
        """先高精度排除来源元描述，再只对放行正文生成 citations。"""

        spans = split_answer_spans(answer)
        if not spans or not evidence_blocks or "\u3010\u6765\u6e90\uff1a" in answer:
            return await self._annotator.annotate(
                question=question,
                answer=answer,
                evidence_blocks=evidence_blocks,
                spent_input_tokens=spent_input_tokens,
                spent_output_tokens=spent_output_tokens,
                max_context_tokens=max_context_tokens,
                max_request_tokens=max_request_tokens,
            )

        source_meta = await self._source_meta.classify(
            question=question,
            answer=answer,
            spans=spans,
            spent_input_tokens=spent_input_tokens,
            spent_output_tokens=spent_output_tokens,
            max_context_tokens=max_context_tokens,
            max_request_tokens=max_request_tokens,
        )
        source_meta_artifact = source_meta.to_artifact()
        if source_meta.status == "pending":
            # 来源门禁只是降低人工复核量的优化步骤。失败时应回退到旧的全量
            # annotation 行为，避免普通用户因为预筛故障而丢失全部引用。
            source_meta_artifact = source_meta_artifact.model_copy(
                update={"fallback_to_all_spans": True}
            )

        excluded_ids = (
            set()
            if source_meta_artifact.fallback_to_all_spans
            else {decision.span_id for decision in source_meta.decisions if decision.source_meta}
        )
        eligible_spans = [span for span in spans if span.span_id not in excluded_ids]
        if not eligible_spans:
            return CitationAnnotationOutcome(
                artifact=CitationAnnotationArtifact(
                    status="complete",
                    span_version=CLAIM_SPAN_VERSION,
                    model_id=self._provider.profile.model_id,
                    span_count=len(spans),
                    evidence_count=len(evidence_blocks),
                    source_meta=source_meta_artifact,
                ),
                model_calls=source_meta.model_calls,
                input_tokens=source_meta.input_tokens,
                output_tokens=source_meta.output_tokens,
                messages=source_meta.messages,
                raw_outputs=source_meta.raw_outputs,
            )

        annotation = await self._annotator.annotate(
            question=question,
            answer=answer,
            evidence_blocks=evidence_blocks,
            spans=eligible_spans,
            spent_input_tokens=spent_input_tokens + source_meta.input_tokens,
            spent_output_tokens=spent_output_tokens + source_meta.output_tokens,
            max_context_tokens=max_context_tokens,
            max_request_tokens=max_request_tokens,
        )
        return CitationAnnotationOutcome(
            artifact=annotation.artifact.model_copy(
                update={
                    "span_count": len(spans),
                    "source_meta": source_meta_artifact,
                }
            ),
            model_calls=source_meta.model_calls + annotation.model_calls,
            input_tokens=source_meta.input_tokens + annotation.input_tokens,
            output_tokens=source_meta.output_tokens + annotation.output_tokens,
            messages=source_meta.messages + annotation.messages,
            raw_outputs=source_meta.raw_outputs + annotation.raw_outputs,
        )
