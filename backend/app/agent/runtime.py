from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, StateGraph

from app import __version__
from app.agent.actions import (
    ActionProtocolError,
    FinishAction,
    ParsedAction,
    ToolAction,
    parse_action,
)
from app.agent.citation_pipeline import CitationPipeline
from app.agent.citations import (
    CitationAnnotationArtifact,
    CitationEvidenceBlock,
    citation_annotation_output_token_cap,
    split_answer_spans,
)
from app.agent.finish_stream import FinishAnswerDeltaExtractor
from app.agent.models import AgentLimitation
from app.agent.prompt import PromptBundle, release_prompt_bundle
from app.agent.token_counter import (
    ConservativeObservationTokenCounter,
    ObservationTokenCounter,
)
from app.agent.tool_adapter import (
    TOOL_CONTRACT_VERSION,
    RetrievalToolAdapter,
    ToolUnavailableError,
)
from app.providers.chat import (
    ChatCompletionResult,
    ChatMessage,
    ChatProvider,
    ChatProviderError,
    ChatStreamChunk,
)
from app.providers.embeddings import EmbeddingProvider
from app.retrieval.hybrid import DEFAULT_HYBRID_RETRIEVAL_PROFILE, HybridRetrievalProfile
from app.retrieval.service import RetrievalFailure, RetrievalReadService, RetrievalSnapshotReader
from app.tracing.models import TraceMode, TraceStatus
from app.tracing.store import TraceSession, TraceSpanHandle, TraceStore, new_trace_id

AGENT_LOOP_VERSION = "react-structured-v4"
_PARTIAL_ANSWER_NOTICE = "**内容未完**：本次回答因运行预算限制提前结束，以上内容可能不完整。"
_TRUNCATION_NOTICE_MARKERS = (
    "内容未完",
    "尚未讲完",
    "回答未完成",
    "内容未完成",
)
TOOL_ACTIVITY_LABELS = {
    "list_documents": "正在查看文档列表",
    "search_topic": "正在构建主题高召回证据",
    "get_outline": "正在查看文档结构",
    "search_blocks": "正在检索相关段落",
    "list_blocks": "正在读取文档内容",
    "get_blocks": "正在补全原文",
    "scan_term": "正在核对原文",
}


@dataclass(frozen=True, slots=True)
class AgentRunConfig:
    """单次 Agent 运行固定的预算与工具上限。"""

    max_tool_calls: int = 8
    max_context_tokens: int = 100_000
    max_request_tokens: int = 400_000
    finalization_reserve_tokens: int = 4_000
    max_output_tokens: int = 8_192
    max_protocol_errors: int = 2

    def __post_init__(self) -> None:
        if self.max_tool_calls < 0:
            raise ValueError("max_tool_calls must not be negative")
        if self.max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive")
        if self.max_request_tokens < self.max_context_tokens:
            raise ValueError("max_request_tokens must be at least max_context_tokens")
        if not 0 <= self.finalization_reserve_tokens < self.max_request_tokens:
            raise ValueError("finalization reserve must be within request budget")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.max_protocol_errors <= 0:
            raise ValueError("max_protocol_errors must be positive")


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """Agent Runtime 暴露给 API、trace 和测试的结构化事件。"""

    event_type: str
    payload: dict[str, Any]


EventSink = Callable[[AgentEvent], None]


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """一次 Agent 请求的最终回答和审计信息。"""

    status: Literal["succeeded", "partial", "failed", "canceled"]
    trace_id: str
    answer: str | None
    answer_completeness: Literal["complete", "partial"]
    limitations: tuple[AgentLimitation, ...]
    termination_reason: str
    snapshot_id: str | None
    prompt_bundle_version: str
    prompt_template_hash: str
    observation_token_counter_version: str
    agent_turns: int
    tool_calls: int
    model_calls: int
    tool_model_calls: int
    input_tokens: int
    output_tokens: int
    sources: list[dict[str, Any]]
    events: tuple[AgentEvent, ...]
    trace_status: Literal["complete", "partial", "dropped", "missing"] = "missing"
    citation_annotations: CitationAnnotationArtifact | None = None
    citation_annotation_model_calls: int = 0


class _AgentGraphState(TypedDict, total=False):
    question: str
    history: list[str]
    events: list[AgentEvent]
    pending_action: ParsedAction | None
    final_answer: str | None
    status: Literal["running", "succeeded", "partial", "failed", "canceled"]
    answer_completeness: Literal["complete", "partial"]
    limitations: list[AgentLimitation]
    termination_reason: str | None
    agent_turns: int
    tool_calls: int
    model_calls: int
    tool_model_calls: int
    input_tokens: int
    output_tokens: int
    tools_enabled: bool
    finalization_mode: bool
    protocol_errors: int
    current_system_prompt: str
    next_context_estimate: int | None
    context_estimate_source: Literal["provider_anchor", "estimated"]
    sources: list[dict[str, Any]]
    evidence_blocks: list[dict[str, Any]]
    citation_annotations: CitationAnnotationArtifact | None
    citation_annotation_model_calls: int
    current_turn_span: TraceSpanHandle | None


class RunCancellationToken:
    """一次运行私有的协作式取消标志。"""

    def __init__(self) -> None:
        self._cancelled = False
        self._cancel_event = asyncio.Event()

    @property
    def is_cancelled(self) -> bool:
        """返回用户是否已经请求取消。"""

        return self._cancelled

    def cancel(self) -> None:
        """设置取消标志；后续模型和工具派发必须停止。"""

        self._cancelled = True
        self._cancel_event.set()

    async def wait(self) -> None:
        """等待用户取消，供长流读取与取消信号竞速。"""

        await self._cancel_event.wait()


@dataclass(frozen=True, slots=True)
class _ModelCallOutcome:
    """一次包含重试的流式模型调用结果。"""

    result: ChatCompletionResult | None
    error: ChatProviderError | None
    attempts: int
    raw_content: str
    emitted_answer: str
    extractor: FinishAnswerDeltaExtractor | None
    cancelled: bool


class AgentRuntime:
    """使用 LangGraph 推进 ReAct 状态，由 Harness 保持协议与预算事实源。"""

    def __init__(
        self,
        data_root: Path,
        *,
        chat_provider: ChatProvider,
        embedding_provider: EmbeddingProvider,
        config: AgentRunConfig | None = None,
        prompt_bundle: PromptBundle | None = None,
        token_counter: ObservationTokenCounter | None = None,
        retrieval_profile: HybridRetrievalProfile = DEFAULT_HYBRID_RETRIEVAL_PROFILE,
        trace_store: TraceStore | None = None,
        trace_mode: TraceMode = "off",
    ) -> None:
        self._data_root = data_root.resolve()
        self._chat_provider = chat_provider
        self._embedding_provider = embedding_provider
        self._config = config or AgentRunConfig()
        self._prompt_bundle = prompt_bundle or release_prompt_bundle()
        self._token_counter = token_counter or ConservativeObservationTokenCounter()
        self._retrieval_profile = retrieval_profile
        self._trace_store = trace_store
        self._trace_mode = trace_mode

    async def run(
        self,
        question: str,
        *,
        emit: EventSink | None = None,
        cancellation: RunCancellationToken | None = None,
        trace_session: TraceSession | None = None,
        trace_id: str | None = None,
        snapshot_id: str | None = None,
    ) -> AgentRunResult:
        """在当前活动快照上执行一次单轮 Agent 请求，并可实时回调事件。"""

        normalized_question = question.strip()
        if not normalized_question:
            raise ValueError("question must not be empty")
        if trace_session is None and trace_id is None:
            trace_id, trace_session = self.begin_trace(normalized_question)
        resolved_trace_id = (
            trace_session.trace_id if trace_session is not None else trace_id or new_trace_id()
        )
        cancellation = cancellation or RunCancellationToken()
        try:
            if cancellation.is_cancelled:
                result = self._canceled_before_run(emit, resolved_trace_id)
            else:
                try:
                    read_service = RetrievalReadService(self._data_root)
                    reader = (
                        read_service.bind_snapshot(snapshot_id)
                        if snapshot_id is not None
                        else read_service.bind_current()
                    )
                except RetrievalFailure as error:
                    result = self._failed_before_run(
                        error.code,
                        emit,
                        resolved_trace_id,
                    )
                else:
                    if trace_session is not None:
                        trace_session.set_snapshot(
                            snapshot_id=reader.snapshot_id,
                            snapshot_status=reader.manifest.status,
                            completeness=reader.completeness,
                        )
                    adapter = RetrievalToolAdapter(
                        reader,
                        self._embedding_provider,
                        chat_provider=self._chat_provider,
                        search_profile=self._retrieval_profile,
                    )
                    execution = _AgentGraphExecution(
                        question=normalized_question,
                        reader=reader,
                        adapter=adapter,
                        chat_provider=self._chat_provider,
                        config=self._config,
                        prompt_bundle=self._prompt_bundle,
                        token_counter=self._token_counter,
                        emit=emit,
                        cancellation=cancellation,
                        trace_session=trace_session,
                        trace_id=resolved_trace_id,
                    )
                    result = await execution.run()
        except Exception:
            self._finish_trace_failure(trace_session)
            raise
        trace_status = self._finish_trace(trace_session, result)
        return replace(result, trace_status=trace_status)

    def begin_trace(self, question: str) -> tuple[str, TraceSession | None]:
        """在模型和工具启动前创建运行级 trace 上下文。"""

        if self._trace_store is None or self._trace_mode == "off":
            return new_trace_id(), None
        profile = self._chat_provider.profile
        try:
            session = self._trace_store.new_session(
                mode=cast(TraceMode, self._trace_mode),
                question=question,
                app_version=__version__,
                versions={
                    "app_version": __version__,
                    "prompt_bundle_version": self._prompt_bundle.version,
                    "prompt_template_hash": self._prompt_bundle.template_hash,
                    "agent_loop_version": AGENT_LOOP_VERSION,
                    "tool_contract_version": TOOL_CONTRACT_VERSION,
                    "observation_token_counter_version": self._token_counter.version,
                    "chat_provider": profile.provider,
                    "chat_model_id": profile.model_id,
                    "chat_temperature": profile.temperature,
                    "thinking_enabled": profile.thinking_enabled,
                    "retrieval_profile": asdict(self._retrieval_profile),
                },
                budget={
                    "max_tool_calls": self._config.max_tool_calls,
                    "max_context_tokens": self._config.max_context_tokens,
                    "max_request_tokens": self._config.max_request_tokens,
                    "finalization_reserve_tokens": self._config.finalization_reserve_tokens,
                    "max_output_tokens": self._config.max_output_tokens,
                    "max_protocol_errors": self._config.max_protocol_errors,
                },
            )
        except Exception:
            return new_trace_id(), None
        return session.trace_id, session

    def _failed_before_run(
        self,
        reason: str,
        emit: EventSink | None,
        trace_id: str,
    ) -> AgentRunResult:
        event = AgentEvent(event_type="run_failed", payload={"reason": reason})
        if emit is not None:
            emit(event)
        return self._terminal_before_run(reason, event, trace_id=trace_id)

    def _canceled_before_run(
        self,
        emit: EventSink | None,
        trace_id: str,
    ) -> AgentRunResult:
        event = AgentEvent(
            event_type="run_canceled",
            payload={"reason": "canceled_by_user"},
        )
        if emit is not None:
            emit(event)
        return self._terminal_before_run(
            "canceled_by_user",
            event,
            trace_id=trace_id,
            status="canceled",
        )

    def _terminal_before_run(
        self,
        reason: str,
        event: AgentEvent,
        *,
        trace_id: str,
        status: Literal["failed", "canceled"] = "failed",
    ) -> AgentRunResult:
        return AgentRunResult(
            status=status,
            trace_id=trace_id,
            answer=None,
            answer_completeness="partial",
            limitations=(
                AgentLimitation(
                    code=reason,
                    message="Agent 运行未能开始，无法生成回答。",
                ),
            ),
            termination_reason=reason,
            snapshot_id=None,
            prompt_bundle_version=self._prompt_bundle.version,
            prompt_template_hash=self._prompt_bundle.template_hash,
            observation_token_counter_version=self._token_counter.version,
            agent_turns=0,
            tool_calls=0,
            model_calls=0,
            tool_model_calls=0,
            input_tokens=0,
            output_tokens=0,
            sources=[],
            events=(event,),
            citation_annotations=None,
            citation_annotation_model_calls=0,
        )

    def _finish_trace(
        self,
        trace_session: TraceSession | None,
        result: AgentRunResult,
    ) -> TraceStatus | Literal["missing"]:
        if trace_session is None:
            return "missing"
        try:
            trace_session.finish(
                status=result.status,
                termination_reason=result.termination_reason,
                answer_completeness=result.answer_completeness,
                agent_turns=result.agent_turns,
                tool_calls=result.tool_calls,
                model_calls=result.model_calls,
                retry_count=max(
                    0,
                    result.model_calls
                    - result.agent_turns
                    - result.tool_model_calls
                    - result.citation_annotation_model_calls,
                ),
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                answer=result.answer,
                limitations=[limitation.model_dump() for limitation in result.limitations],
                citation_annotations=(
                    result.citation_annotations.model_dump(mode="json")
                    if result.citation_annotations is not None
                    else None
                ),
            )
        except Exception:
            if self._trace_store is not None:
                self._trace_store.mark_failure(
                    trace_session.trace_id,
                    "trace finalization failed",
                )
        if self._trace_store is None:
            return "missing"
        return self._trace_store.health(trace_session.trace_id).status

    def _finish_trace_failure(self, trace_session: TraceSession | None) -> None:
        if trace_session is None or self._trace_store is None:
            return
        try:
            trace_session.finish(
                status="failed",
                termination_reason="harness_internal_error",
                answer_completeness="partial",
                agent_turns=0,
                tool_calls=0,
                model_calls=0,
                retry_count=0,
                input_tokens=0,
                output_tokens=0,
                answer=None,
                limitations=[],
                citation_annotations=None,
            )
        except Exception:
            self._trace_store.mark_failure(
                trace_session.trace_id,
                "agent run raised an unexpected exception",
            )


class _AgentGraphExecution:
    """一次运行私有的 LangGraph 节点集合。"""

    def __init__(
        self,
        *,
        question: str,
        reader: RetrievalSnapshotReader,
        adapter: RetrievalToolAdapter,
        chat_provider: ChatProvider,
        config: AgentRunConfig,
        prompt_bundle: PromptBundle,
        token_counter: ObservationTokenCounter,
        emit: EventSink | None,
        cancellation: RunCancellationToken,
        trace_session: TraceSession | None,
        trace_id: str,
    ) -> None:
        self._question = question
        self._reader = reader
        self._adapter = adapter
        self._chat_provider = chat_provider
        self._config = config
        self._prompt_bundle = prompt_bundle
        self._token_counter = token_counter
        self._emit = emit
        self._cancellation = cancellation
        self._trace_session = trace_session
        self._trace_id = trace_id
        self._graph = self._build_graph()

    async def run(self) -> AgentRunResult:
        """启动 LangGraph 并把最终状态转换为稳定结果。"""

        run_started = AgentEvent(
            event_type="run_started",
            payload={"snapshot_id": self._reader.snapshot_id},
        )
        snapshot_bound = AgentEvent(
            event_type="snapshot_bound",
            payload={"snapshot_id": self._reader.snapshot_id},
        )
        if self._emit is not None:
            self._emit(run_started)
            self._emit(snapshot_bound)
        initial: _AgentGraphState = {
            "question": self._question,
            "history": [],
            "events": [run_started, snapshot_bound],
            "pending_action": None,
            "final_answer": None,
            "status": "running",
            "answer_completeness": self._initial_completeness(),
            "limitations": self._initial_limitations(),
            "termination_reason": None,
            "agent_turns": 0,
            "tool_calls": 0,
            "model_calls": 0,
            "tool_model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "tools_enabled": True,
            "finalization_mode": False,
            "protocol_errors": 0,
            "next_context_estimate": None,
            "context_estimate_source": "estimated",
            "sources": [],
            "evidence_blocks": [],
            "citation_annotations": None,
            "citation_annotation_model_calls": 0,
            "current_turn_span": None,
        }
        final = initial
        try:
            async for state in self._graph.astream(
                initial,
                {"recursion_limit": self._config.max_tool_calls * 3 + 20},
                stream_mode="values",
            ):
                final = cast(_AgentGraphState, state)
        except GraphRecursionError:
            final = self._protocol_loop_state(final)
        final = await self._annotate_citations(final)
        status = final.get("status", "failed")
        if status == "running":
            status = "failed"
        limitations = tuple(final.get("limitations", []))
        answer_completeness = final.get("answer_completeness", "partial")
        termination_reason = final.get("termination_reason") or "harness_internal_error"
        return AgentRunResult(
            status=status,
            trace_id=self._trace_id,
            answer=final.get("final_answer"),
            answer_completeness=answer_completeness,
            limitations=limitations,
            termination_reason=termination_reason,
            snapshot_id=self._reader.snapshot_id,
            prompt_bundle_version=self._prompt_bundle.version,
            prompt_template_hash=self._prompt_bundle.template_hash,
            observation_token_counter_version=self._token_counter.version,
            agent_turns=final.get("agent_turns", 0),
            tool_calls=final.get("tool_calls", 0),
            model_calls=final.get("model_calls", 0),
            tool_model_calls=final.get("tool_model_calls", 0),
            input_tokens=final.get("input_tokens", 0),
            output_tokens=final.get("output_tokens", 0),
            sources=final.get("sources", []),
            events=tuple(final.get("events", [])),
            citation_annotations=final.get("citation_annotations"),
            citation_annotation_model_calls=final.get(
                "citation_annotation_model_calls",
                0,
            ),
        )

    async def _annotate_citations(
        self,
        state: _AgentGraphState,
    ) -> _AgentGraphState:
        """在冻结答案之后标注引用；失败时保留正文并显式进入 pending。"""

        if state.get("status") not in {"succeeded", "partial"}:
            return state
        answer = state.get("final_answer")
        if not answer:
            return state
        evidence_blocks = [
            CitationEvidenceBlock.model_validate(
                {key: value for key, value in item.items() if not key.startswith("_")}
            )
            for item in state.get("evidence_blocks", [])
        ]
        events = list(state.get("events", []))
        if self._cancellation.is_cancelled:
            return self._canceled_state(state, events=events, answer=answer)
        self._record_event(
            events,
            AgentEvent(
                event_type="activity_changed",
                payload={"stage": "citation_annotation", "label": "正在核对引用依据"},
            ),
        )
        self._record_event(
            events,
            AgentEvent(
                event_type="citation_annotation_started",
                payload={"evidence_count": len(evidence_blocks)},
            ),
        )

        current_input_tokens = state.get("input_tokens", 0)
        current_output_tokens = state.get("output_tokens", 0)
        annotation_span = (
            self._trace_session.start_span(
                "citation.annotation",
                "LLM",
                attributes={
                    "llm.provider": self._chat_provider.profile.provider,
                    "llm.request.model": self._chat_provider.profile.model_id,
                    "llm.invocation_parameters.max_tokens": (
                        citation_annotation_output_token_cap(len(split_answer_spans(answer)))
                    ),
                    "app.answer_span_count": len(split_answer_spans(answer)),
                    "app.evidence_block_count": len(evidence_blocks),
                },
            )
            if self._trace_session is not None
            else None
        )
        try:
            outcome = await CitationPipeline(
                self._chat_provider,
                token_counter=self._token_counter,
            ).annotate(
                question=self._question,
                answer=answer,
                evidence_blocks=evidence_blocks,
                spent_input_tokens=current_input_tokens,
                spent_output_tokens=current_output_tokens,
                max_context_tokens=self._config.max_context_tokens,
                max_request_tokens=self._config.max_request_tokens,
            )
        except asyncio.CancelledError:
            if annotation_span is not None and self._trace_session is not None:
                self._trace_session.end_span(
                    annotation_span,
                    status="error",
                    error_code="canceled_by_user",
                )
            raise
        except Exception:
            if annotation_span is not None and self._trace_session is not None:
                self._trace_session.end_span(
                    annotation_span,
                    status="error",
                    error_code="citation_annotation_internal_error",
                )
            raise

        cancelled = self._cancellation.is_cancelled
        if annotation_span is not None and self._trace_session is not None:
            if outcome.messages:
                prompt_artifact = self._trace_session.record_artifact(
                    kind="citation.annotation.prompt",
                    mime_type="application/json",
                    content=[
                        {"role": message.role, "content": message.content}
                        for message in outcome.messages
                    ],
                )
                annotation_span.attributes["llm.prompt"] = prompt_artifact.model_dump(mode="json")
            if outcome.raw_outputs:
                output_artifact = self._trace_session.record_artifact(
                    kind="citation.annotation.output",
                    mime_type="application/json",
                    content=list(outcome.raw_outputs),
                )
                annotation_span.attributes["llm.output"] = output_artifact.model_dump(mode="json")
            annotation_span.attributes.update(
                {
                    "app.citation_annotation_status": outcome.artifact.status,
                    "app.claim_span_version": outcome.artifact.span_version,
                    "app.citation_annotation_prompt_version": (outcome.artifact.prompt_version),
                    "app.citation_annotation_prompt_hash": outcome.artifact.prompt_hash,
                    "app.citation_source_meta_status": outcome.artifact.source_meta.status,
                    "app.citation_source_meta_rule_version": (
                        outcome.artifact.source_meta.rule_version
                    ),
                    "app.citation_source_meta_prompt_version": (
                        outcome.artifact.source_meta.prompt_version
                    ),
                    "app.citation_source_meta_prompt_hash": (
                        outcome.artifact.source_meta.prompt_hash
                    ),
                    "app.citation_source_meta_excluded_span_count": (
                        outcome.artifact.source_meta.excluded_span_count
                    ),
                    "app.citation_source_meta_fallback_to_all_spans": (
                        outcome.artifact.source_meta.fallback_to_all_spans
                    ),
                    "app.citation_source_meta_model_calls": (
                        outcome.artifact.source_meta.model_calls
                    ),
                    "app.annotation_batch_count": outcome.artifact.batch_count,
                    "app.annotation_batch_version": outcome.artifact.batch_version,
                    "app.model_calls": outcome.model_calls,
                    "llm.token_count.prompt": outcome.input_tokens,
                    "llm.token_count.completion": outcome.output_tokens,
                    "app.usage_source": ("provider" if outcome.model_calls > 0 else "not_called"),
                }
            )
            if outcome.model_calls > 1:
                self._trace_session.add_span_event(
                    annotation_span,
                    "retry",
                    {"app.attempts": outcome.model_calls},
                )
            self._trace_session.end_span(
                annotation_span,
                status=(
                    "ok" if outcome.artifact.status != "pending" and not cancelled else "error"
                ),
                error_code=("canceled_by_user" if cancelled else outcome.artifact.error_code),
            )

        model_calls = state.get("model_calls", 0) + outcome.model_calls
        input_tokens = current_input_tokens + outcome.input_tokens
        output_tokens = current_output_tokens + outcome.output_tokens
        limitations = list(state.get("limitations", []))
        if outcome.artifact.source_meta.status == "pending":
            source_meta_failure = (
                outcome.artifact.source_meta.error_message
                or outcome.artifact.source_meta.error_code
            )
            limitations = _with_limitation(
                limitations,
                code="citation_source_meta_fallback",
                message=(f"来源预筛未完成，已回退到全量正文引用标注：{source_meta_failure}"),
            )
        if outcome.artifact.status == "pending":
            limitations = _with_limitation(
                limitations,
                code="citation_annotation_pending",
                message=(
                    "引用标注未完成，正文已保留："
                    f"{outcome.artifact.error_message or outcome.artifact.error_code}"
                ),
            )
        if cancelled:
            return self._canceled_state(
                state,
                events=events,
                answer=answer,
                extra={
                    "model_calls": model_calls,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "limitations": limitations,
                    "citation_annotations": outcome.artifact,
                    "citation_annotation_model_calls": outcome.model_calls,
                },
            )

        artifact_payload = outcome.artifact.model_dump(mode="json")
        public_payload = {
            "status": artifact_payload["status"],
            "span_version": artifact_payload["span_version"],
            "batch_count": artifact_payload["batch_count"],
            "batch_version": artifact_payload["batch_version"],
            "annotations": artifact_payload["annotations"],
            "source_meta": {
                "status": artifact_payload["source_meta"]["status"],
                "rule_version": artifact_payload["source_meta"]["rule_version"],
                "prompt_version": artifact_payload["source_meta"]["prompt_version"],
                "prompt_hash": artifact_payload["source_meta"]["prompt_hash"],
                "model_calls": artifact_payload["source_meta"]["model_calls"],
                "excluded_span_count": artifact_payload["source_meta"]["excluded_span_count"],
                "fallback_to_all_spans": artifact_payload["source_meta"]["fallback_to_all_spans"],
                "error_code": artifact_payload["source_meta"]["error_code"],
            },
            "error_code": artifact_payload["error_code"],
            "message": artifact_payload["error_message"],
        }
        if outcome.model_calls:
            self._record_event(
                events,
                AgentEvent(
                    event_type="usage_update",
                    payload={
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "input_source": "estimated",
                        "output_source": "provider",
                    },
                ),
            )
        self._record_event(
            events,
            AgentEvent(
                event_type="citations_available",
                payload=public_payload,
            ),
        )
        if outcome.artifact.status == "pending":
            self._record_event(
                events,
                AgentEvent(
                    event_type="citation_annotation_failed",
                    payload=public_payload,
                ),
            )

        return cast(
            _AgentGraphState,
            {
                **state,
                "events": events,
                "model_calls": model_calls,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "limitations": limitations,
                "citation_annotations": outcome.artifact,
                "citation_annotation_model_calls": outcome.model_calls,
                "current_turn_span": None,
            },
        )

    def _protocol_loop_state(self, state: _AgentGraphState) -> _AgentGraphState:
        """把无法收敛的图循环转换为可判定的产品失败终态。"""

        events = list(state.get("events", []))
        limitations = _with_limitation(
            list(state.get("limitations", [])),
            code="agent_protocol_loop",
            message="Agent 协议多次无法收敛，已停止继续调用模型。",
            parameter="max_protocol_errors",
            limit=self._config.max_protocol_errors,
        )
        return self._terminal_state(
            state,
            events=events,
            status="failed",
            reason="agent_protocol_loop",
            extra={
                "limitations": limitations,
                "tools_enabled": False,
                "finalization_mode": True,
            },
        )

    def _build_graph(self):
        builder = StateGraph(_AgentGraphState)
        builder.add_node("model", self._model_node)
        builder.add_node("tools", self._tool_node)
        builder.set_entry_point("model")
        builder.add_conditional_edges(
            "model",
            self._route_after_model,
            {"model": "model", "tools": "tools", END: END},
        )
        builder.add_edge("tools", "model")
        return builder.compile()

    def _record_event(self, events: list[AgentEvent], event: AgentEvent) -> None:
        """同时写入最终审计事件和正在消费的实时事件槽。"""

        events.append(event)
        if self._emit is not None:
            self._emit(event)

    def _canceled_state(
        self,
        state: _AgentGraphState,
        *,
        events: list[AgentEvent],
        answer: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> _AgentGraphState:
        """把用户取消转换为保留已有正文的唯一终态。"""

        terminal_extra = dict(extra or {})
        if answer:
            terminal_extra["final_answer"] = answer
        return self._terminal_state(
            state,
            events=events,
            status="canceled",
            reason="canceled_by_user",
            extra=terminal_extra,
        )

    def _trace_turn_span(self, state: _AgentGraphState) -> TraceSpanHandle | None:
        """返回当前 ReAct 轮次 span；没有时创建并关联到本次运行根 span。"""

        existing = state.get("current_turn_span")
        if existing is not None or self._trace_session is None:
            return existing
        return self._trace_session.start_span(
            "agent.turn",
            "INTERNAL",
            attributes={
                "app.turn_index": state.get("agent_turns", 0) + 1,
            },
        )

    def _end_trace_turn(
        self,
        handle: TraceSpanHandle | None,
        *,
        status: str,
        error_code: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """结束一个轮次 span；trace 关闭时是空操作。"""

        if handle is None or self._trace_session is None:
            return
        self._trace_session.end_span(
            handle,
            status=status,
            error_code=error_code,
            attributes=attributes,
        )

    def _trace_budget_check(
        self,
        turn_span: TraceSpanHandle | None,
        *,
        phase: str,
        limit_kind: str,
        limit_tokens: int | None,
        observed_tokens: int | None,
        decision: str,
    ) -> None:
        """记录预算准入或拒绝，不参与实际预算判断。"""

        if turn_span is None or self._trace_session is None:
            return
        remaining = (
            max(0, limit_tokens - observed_tokens)
            if limit_tokens is not None and observed_tokens is not None
            else None
        )
        self._trace_session.add_span_event(
            turn_span,
            "budget.check",
            {
                "app.phase": phase,
                "app.limit_kind": limit_kind,
                "app.limit_tokens": limit_tokens,
                "app.observed_tokens": observed_tokens,
                "app.remaining_tokens": remaining,
                "app.decision": decision,
            },
        )

    async def _model_node(self, state: _AgentGraphState) -> _AgentGraphState:
        events = list(state.get("events", []))
        history = list(state.get("history", []))
        limitations = list(state.get("limitations", []))
        tool_calls = state.get("tool_calls", 0)
        trace_turn = self._trace_turn_span(state)
        state["current_turn_span"] = trace_turn
        finalization_mode = state.get("finalization_mode", False)
        tools_enabled = tool_calls < self._config.max_tool_calls

        if not tools_enabled and not finalization_mode:
            finalization_mode = True
            tools_enabled = False
            _append_limitation(
                limitations,
                code="max_tool_calls",
                message=(
                    f"达到了工具调用次数上限（max_tool_calls={self._config.max_tool_calls}）。"
                ),
                parameter="max_tool_calls",
                limit=self._config.max_tool_calls,
            )
            self._record_event(
                events,
                AgentEvent(
                    event_type="budget_warning",
                    payload={"reason": "max_tool_calls", "limit": self._config.max_tool_calls},
                ),
            )
            self._trace_budget_check(
                trace_turn,
                phase="pre_turn",
                limit_kind="max_tool_calls",
                limit_tokens=self._config.max_tool_calls,
                observed_tokens=tool_calls,
                decision="terminate",
            )

        used_tokens = state.get("input_tokens", 0) + state.get("output_tokens", 0)
        effective_request_limit = (
            self._config.max_request_tokens - self._config.finalization_reserve_tokens
        )
        if used_tokens >= effective_request_limit and not finalization_mode:
            finalization_mode = True
            tools_enabled = False
            _append_limitation(
                limitations,
                code="request_budget_finalization",
                message=(
                    "累计 token 预算触发最终回答阈值"
                    f"（max_request_tokens={self._config.max_request_tokens}，"
                    f"预留 {self._config.finalization_reserve_tokens}），停止继续检索。"
                ),
                parameter="max_request_tokens",
                limit=self._config.max_request_tokens,
            )
            self._record_event(
                events,
                AgentEvent(
                    event_type="budget_warning",
                    payload={"reason": "request_budget", "used_tokens": used_tokens},
                ),
            )
            self._trace_budget_check(
                trace_turn,
                phase="pre_turn",
                limit_kind="request",
                limit_tokens=self._config.max_request_tokens,
                observed_tokens=used_tokens,
                decision="terminate",
            )
        if used_tokens >= self._config.max_request_tokens:
            answer, _ = _partial_answer_with_notice(state.get("final_answer"))
            return self._terminal_state(
                state,
                events=events,
                status="partial",
                reason="request_budget_exceeded",
                extra={
                    "final_answer": answer,
                    "limitations": _with_limitation(
                        limitations,
                        code="request_budget_exceeded",
                        message="本次回答因累计 token 预算耗尽而提前结束。",
                        parameter="max_request_tokens",
                        limit=self._config.max_request_tokens,
                    ),
                    "tools_enabled": False,
                    "finalization_mode": True,
                },
            )

        system_prompt = self._prompt_bundle.render(
            tools=self._adapter.descriptors if tools_enabled else [],
            collection_status=self._collection_status(),
            finalization_mode=finalization_mode,
        )
        user_prompt = self._render_user_prompt(state.get("question", ""), history)
        prior_context_estimate = state.get("next_context_estimate")
        prior_system_prompt = state.get("current_system_prompt")
        if (
            prior_context_estimate is not None
            and state.get("context_estimate_source") == "provider_anchor"
            and prior_system_prompt is not None
        ):
            system_delta = self._token_counter.count(
                {"system": system_prompt}
            ) - self._token_counter.count({"system": prior_system_prompt})
            context_estimate = max(1, prior_context_estimate + system_delta)
            context_estimate_source = "provider_anchor"
        else:
            context_estimate = self._token_counter.count(
                {
                    "system": system_prompt,
                    "user": user_prompt,
                }
            )
            context_estimate_source = "estimated"
        if context_estimate > self._config.max_context_tokens:
            self._trace_budget_check(
                trace_turn,
                phase="pre_turn",
                limit_kind="context",
                limit_tokens=self._config.max_context_tokens,
                observed_tokens=context_estimate,
                decision="reject",
            )
            if not finalization_mode:
                finalization_mode = True
                tools_enabled = False
                system_prompt = self._prompt_bundle.render(
                    tools=[],
                    collection_status=self._collection_status(),
                    finalization_mode=True,
                )
                if prior_system_prompt is not None and prior_context_estimate is not None:
                    system_delta = self._token_counter.count(
                        {"system": system_prompt}
                    ) - self._token_counter.count({"system": prior_system_prompt})
                    context_estimate = max(1, prior_context_estimate + system_delta)
                    context_estimate_source = "provider_anchor"
                else:
                    context_estimate = self._token_counter.count(
                        {"system": system_prompt, "user": user_prompt}
                    )
                    context_estimate_source = "estimated"
            if context_estimate > self._config.max_context_tokens:
                answer, _ = _partial_answer_with_notice(state.get("final_answer"))
                return self._terminal_state(
                    state,
                    events=events,
                    status="partial",
                    reason="context_budget_exceeded",
                    extra={
                        "final_answer": answer,
                        "limitations": _with_limitation(
                            limitations,
                            code="context_budget_exceeded",
                            message=(
                                "完整上下文仍然超过单次上下文预算"
                                f"（max_context_tokens={self._config.max_context_tokens}）。"
                            ),
                            parameter="max_context_tokens",
                            limit=self._config.max_context_tokens,
                        ),
                        "tools_enabled": False,
                        "finalization_mode": True,
                    },
                )
        self._trace_budget_check(
            trace_turn,
            phase="pre_turn",
            limit_kind="context",
            limit_tokens=self._config.max_context_tokens,
            observed_tokens=context_estimate,
            decision="admit",
        )

        if self._cancellation.is_cancelled:
            return self._canceled_state(state, events=events)
        self._record_event(
            events,
            AgentEvent(
                event_type="activity_changed",
                payload={
                    "stage": "model",
                    "label": "正在组织回答" if finalization_mode else "正在分析问题",
                },
            ),
        )
        messages = (
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_prompt),
        )
        llm_span = (
            self._trace_session.start_span(
                "llm.call",
                "LLM",
                parent_span_id=(trace_turn.span_id if trace_turn is not None else None),
                attributes={
                    "llm.provider": self._chat_provider.profile.provider,
                    "llm.request.model": self._chat_provider.profile.model_id,
                    "llm.invocation_parameters.temperature": (
                        self._chat_provider.profile.temperature
                    ),
                    "llm.invocation_parameters.max_tokens": (self._config.max_output_tokens),
                    "app.turn_index": state.get("agent_turns", 0) + 1,
                },
            )
            if self._trace_session is not None
            else None
        )
        if llm_span is not None and self._trace_session is not None:
            prompt_artifact = self._trace_session.record_artifact(
                kind="llm.prompt",
                mime_type="application/json",
                content=[
                    {"role": message.role, "content": message.content} for message in messages
                ],
            )
            llm_span.attributes["llm.prompt"] = prompt_artifact.model_dump(mode="json")
        outcome = await self._complete_with_retry(
            messages,
            state,
            events,
            context_estimate,
        )
        result = outcome.result
        error = outcome.error
        attempts = outcome.attempts
        model_calls = state.get("model_calls", 0) + attempts
        if llm_span is not None and self._trace_session is not None:
            output_artifact = self._trace_session.record_artifact(
                kind="llm.output",
                mime_type="text/plain; charset=utf-8",
                content=outcome.raw_content,
            )
            llm_span.attributes.update(
                {
                    "llm.output": output_artifact.model_dump(mode="json"),
                    "app.attempts": attempts,
                    "llm.finish_reason": (
                        outcome.result.finish_reason if outcome.result is not None else None
                    ),
                    "llm.token_count.prompt": (
                        outcome.result.input_tokens if outcome.result is not None else 0
                    ),
                    "llm.token_count.completion": (
                        outcome.result.output_tokens
                        if outcome.result is not None
                        else self._token_counter.count(outcome.raw_content)
                    ),
                    "llm.request.id": (
                        outcome.result.request_id if outcome.result is not None else None
                    ),
                }
            )
            if attempts > 1:
                self._trace_session.add_span_event(
                    llm_span,
                    "retry",
                    {"app.attempts": attempts},
                )
        if outcome.cancelled:
            provider_input = outcome.result.input_tokens if outcome.result is not None else 0
            provider_output = outcome.result.output_tokens if outcome.result is not None else 0
            current_input = provider_input if provider_input > 0 else context_estimate
            current_output = (
                provider_output
                if provider_output > 0
                else self._token_counter.count(outcome.raw_content)
            )
            failed_attempt_input_tokens = max(0, attempts - 1) * context_estimate
            if llm_span is not None and self._trace_session is not None:
                self._trace_session.end_span(
                    llm_span,
                    status="error",
                    error_code="canceled_by_user",
                    attributes={
                        "app.cancelled": True,
                        "llm.token_count.prompt": current_input,
                        "llm.token_count.completion": current_output,
                    },
                )
            return self._canceled_state(
                state,
                events=events,
                answer=outcome.emitted_answer or None,
                extra={
                    "model_calls": model_calls,
                    "input_tokens": (
                        state.get("input_tokens", 0) + current_input + failed_attempt_input_tokens
                    ),
                    "output_tokens": state.get("output_tokens", 0) + current_output,
                },
            )
        if result is None:
            reason = error.code if error is not None else "chat_failed"
            failed_input_tokens = attempts * context_estimate
            failed_output_tokens = self._token_counter.count(outcome.raw_content)
            if llm_span is not None and self._trace_session is not None:
                self._trace_session.end_span(
                    llm_span,
                    status="error",
                    error_code=reason,
                    attributes={
                        "llm.token_count.prompt": failed_input_tokens,
                        "llm.token_count.completion": failed_output_tokens,
                        "app.usage_source": "estimated",
                    },
                )
            self._record_event(
                events,
                AgentEvent(
                    event_type="model_call_failed",
                    payload={
                        "attempts": attempts,
                        "error_code": reason,
                        "estimated_input_tokens": failed_input_tokens,
                        "estimated_output_tokens": failed_output_tokens,
                        "usage_source": "estimated",
                    },
                ),
            )
            return self._terminal_state(
                state,
                events=events,
                status="failed",
                reason=reason,
                extra={
                    "model_calls": model_calls,
                    "input_tokens": state.get("input_tokens", 0) + failed_input_tokens,
                    "output_tokens": (state.get("output_tokens", 0) + failed_output_tokens),
                    **({"final_answer": outcome.emitted_answer} if outcome.emitted_answer else {}),
                },
            )
        agent_turns = state.get("agent_turns", 0) + 1
        provider_usage_available = result.input_tokens > 0 and result.output_tokens > 0
        if provider_usage_available:
            current_input_tokens = result.input_tokens
            current_output_tokens = result.output_tokens
            usage_source = "provider"
        else:
            current_input_tokens = context_estimate
            current_output_tokens = self._token_counter.count(result.content)
            usage_source = "estimated"
        failed_attempt_input_tokens = max(0, attempts - 1) * context_estimate
        input_tokens = (
            state.get("input_tokens", 0) + current_input_tokens + failed_attempt_input_tokens
        )
        output_tokens = state.get("output_tokens", 0) + current_output_tokens
        next_context_estimate = current_input_tokens + current_output_tokens
        next_context_estimate_source = (
            "provider_anchor" if provider_usage_available else "estimated"
        )
        if llm_span is not None and self._trace_session is not None:
            self._trace_session.end_span(
                llm_span,
                status="ok",
                attributes={
                    "llm.token_count.prompt": current_input_tokens,
                    "llm.token_count.completion": current_output_tokens,
                    "app.usage_source": usage_source,
                },
            )
        self._record_event(
            events,
            AgentEvent(
                event_type="model_call",
                payload={
                    "turn": agent_turns,
                    "request_id": result.request_id,
                    "model_id": result.actual_model_id,
                    "finish_reason": result.finish_reason,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "context_tokens_estimate": context_estimate,
                    "context_estimate_source": context_estimate_source,
                    "usage_source": usage_source,
                    "estimated_failed_attempt_input_tokens": failed_attempt_input_tokens,
                },
            ),
        )
        try:
            action = parse_action(result.content)
        except ActionProtocolError as protocol_error:
            if result.finish_reason == "length":
                reason = (
                    "request_budget_exceeded" if finalization_mode else "answer_output_truncated"
                )
                answer, visible_suffix = _partial_answer_with_notice(outcome.emitted_answer)
                if visible_suffix:
                    self._record_event(
                        events,
                        AgentEvent(
                            event_type="answer_delta",
                            payload={"delta": visible_suffix},
                        ),
                    )
                return self._terminal_state(
                    state,
                    events=events,
                    status="partial",
                    reason=reason,
                    extra={
                        "history": history,
                        "final_answer": answer,
                        "limitations": _with_limitation(
                            limitations,
                            code=reason,
                            message=(
                                "模型最终答案未按协议闭合；已保留用户实际看到的部分正文，"
                                "并标记内容未完。"
                            ),
                        ),
                        "agent_turns": agent_turns,
                        "model_calls": model_calls,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "tools_enabled": False,
                        "finalization_mode": True,
                        "current_system_prompt": system_prompt,
                        "next_context_estimate": next_context_estimate,
                        "context_estimate_source": next_context_estimate_source,
                        "protocol_errors": state.get("protocol_errors", 0),
                    },
                )
            if finalization_mode:
                answer, visible_suffix = _partial_answer_with_notice(outcome.emitted_answer)
                if visible_suffix:
                    self._record_event(
                        events,
                        AgentEvent(
                            event_type="answer_delta",
                            payload={"delta": visible_suffix},
                        ),
                    )
                protocol_errors = state.get("protocol_errors", 0) + 1
                limitations = _with_limitation(
                    limitations,
                    code="agent_protocol_loop",
                    message="强制最终回答阶段仍未输出合法 finish JSON，Agent 已停止。",
                    parameter="max_protocol_errors",
                    limit=self._config.max_protocol_errors,
                )
                self._record_event(
                    events,
                    AgentEvent(
                        event_type="protocol_error",
                        payload={
                            "message": str(protocol_error),
                            "turn": agent_turns,
                            "count": protocol_errors,
                            "max_protocol_errors": self._config.max_protocol_errors,
                            "finalization_required": True,
                        },
                    ),
                )
                return self._terminal_state(
                    state,
                    events=events,
                    status="failed",
                    reason="agent_protocol_loop",
                    extra={
                        "history": history,
                        "final_answer": answer,
                        "limitations": limitations,
                        "agent_turns": agent_turns,
                        "model_calls": model_calls,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "tools_enabled": False,
                        "finalization_mode": True,
                        "protocol_errors": protocol_errors,
                        "current_system_prompt": system_prompt,
                        "next_context_estimate": next_context_estimate,
                        "context_estimate_source": next_context_estimate_source,
                    },
                )
            protocol_errors = state.get("protocol_errors", 0) + 1
            enter_finalization = protocol_errors >= self._config.max_protocol_errors
            protocol_message = {
                "type": "protocol_error",
                "message": str(protocol_error),
                "protocol_error_count": protocol_errors,
                "max_protocol_errors": self._config.max_protocol_errors,
                "tools_enabled": not enter_finalization,
            }
            protocol_text = _canonical_json(protocol_message)
            protocol_tokens = self._token_counter.count(protocol_text)
            history.append(protocol_text)
            self._record_event(
                events,
                AgentEvent(
                    event_type="protocol_error",
                    payload={
                        "message": str(protocol_error),
                        "turn": agent_turns,
                        "count": protocol_errors,
                        "max_protocol_errors": self._config.max_protocol_errors,
                        "finalization_required": enter_finalization,
                        "result_tokens": protocol_tokens,
                    },
                ),
            )
            self._end_trace_turn(
                trace_turn,
                status="error",
                error_code="protocol_error",
                attributes={
                    "app.protocol_error": str(protocol_error),
                    "app.protocol_error_count": protocol_errors,
                    "app.finalization_required": enter_finalization,
                },
            )
            if enter_finalization:
                limitations = _with_limitation(
                    limitations,
                    code="agent_protocol_error_finalization",
                    message="模型连续输出不符合结构化协议，已关闭工具并要求最终回答。",
                    parameter="max_protocol_errors",
                    limit=self._config.max_protocol_errors,
                )
            return {
                "history": history,
                "events": events,
                "pending_action": None,
                "agent_turns": agent_turns,
                "model_calls": model_calls,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "tools_enabled": not enter_finalization,
                "finalization_mode": enter_finalization,
                "protocol_errors": protocol_errors,
                "current_system_prompt": system_prompt,
                "next_context_estimate": next_context_estimate + protocol_tokens,
                "context_estimate_source": next_context_estimate_source,
                "limitations": limitations,
                "current_turn_span": None,
            }
        history.append(result.content)

        if isinstance(action, FinishAction):
            if outcome.extractor is not None:
                final_delta = outcome.extractor.finish(action.answer)
                if final_delta:
                    self._record_event(
                        events,
                        AgentEvent(
                            event_type="answer_delta",
                            payload={"delta": final_delta},
                        ),
                    )
            answer_text = action.answer
            if result.finish_reason == "length":
                limitations = _with_limitation(
                    limitations,
                    code="answer_output_truncated",
                    message="最终答案达到单次输出长度上限，部分内容未完。",
                )
                answer_text, visible_suffix = _partial_answer_with_notice(answer_text)
                if visible_suffix:
                    self._record_event(
                        events,
                        AgentEvent(
                            event_type="answer_delta",
                            payload={"delta": visible_suffix},
                        ),
                    )
            answer = _render_answer_with_limitations(answer_text, limitations)
            status: Literal["succeeded", "partial"] = "partial" if limitations else "succeeded"
            self._record_event(
                events,
                AgentEvent(
                    event_type="answer_completed",
                    payload={
                        "turn": agent_turns,
                        "answer": answer,
                        "termination_reason": "finish",
                        "answer_completeness": "partial" if limitations else "complete",
                        "limitations": [limitation.model_dump() for limitation in limitations],
                        "sources": state.get("sources", []),
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "input_source": usage_source,
                        "output_source": usage_source,
                    },
                ),
            )
            self._end_trace_turn(
                trace_turn,
                status="ok",
                attributes={
                    "app.termination_reason": "finish",
                    "app.answer_completeness": ("partial" if limitations else "complete"),
                    "app.decision_summary": action.decision_summary,
                },
            )
            return {
                "history": history,
                "events": events,
                "pending_action": None,
                "final_answer": answer,
                "status": status,
                "answer_completeness": "partial" if limitations else "complete",
                "termination_reason": "finish",
                "agent_turns": agent_turns,
                "model_calls": model_calls,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "tools_enabled": tools_enabled,
                "finalization_mode": finalization_mode,
                "protocol_errors": 0,
                "current_system_prompt": system_prompt,
                "next_context_estimate": next_context_estimate,
                "context_estimate_source": next_context_estimate_source,
                "limitations": limitations,
                "current_turn_span": None,
            }

        if not tools_enabled:
            answer, visible_suffix = _partial_answer_with_notice(outcome.emitted_answer)
            if visible_suffix:
                self._record_event(
                    events,
                    AgentEvent(
                        event_type="answer_delta",
                        payload={"delta": visible_suffix},
                    ),
                )
            limitations = _with_limitation(
                limitations,
                code="agent_protocol_loop",
                message="强制最终回答阶段仍输出工具调用，Agent 已停止。",
                parameter="max_protocol_errors",
                limit=self._config.max_protocol_errors,
            )
            return self._terminal_state(
                state,
                events=events,
                status="failed",
                reason="agent_protocol_loop",
                extra={
                    "history": history,
                    "final_answer": answer,
                    "limitations": limitations,
                    "agent_turns": agent_turns,
                    "model_calls": model_calls,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "tools_enabled": False,
                    "finalization_mode": True,
                    "protocol_errors": state.get("protocol_errors", 0) + 1,
                    "current_system_prompt": system_prompt,
                    "next_context_estimate": next_context_estimate,
                    "context_estimate_source": next_context_estimate_source,
                },
            )
        return {
            "history": history,
            "events": events,
            "pending_action": action,
            "agent_turns": agent_turns,
            "model_calls": model_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "tools_enabled": True,
            "finalization_mode": finalization_mode,
            "protocol_errors": 0,
            "current_system_prompt": system_prompt,
            "next_context_estimate": next_context_estimate,
            "context_estimate_source": next_context_estimate_source,
            "limitations": limitations,
            "current_turn_span": trace_turn,
        }

    async def _tool_node(self, state: _AgentGraphState) -> _AgentGraphState:
        action = state.get("pending_action")
        if not isinstance(action, ToolAction):
            return {"pending_action": None}
        events = list(state.get("events", []))
        history = list(state.get("history", []))
        limitations = list(state.get("limitations", []))
        if self._cancellation.is_cancelled:
            return self._canceled_state(state, events=events)
        activity_label = TOOL_ACTIVITY_LABELS.get(action.tool_name, "正在检索知识库")
        trace_turn = state.get("current_turn_span")
        tool_span = (
            self._trace_session.start_span(
                "tool.call",
                "TOOL",
                parent_span_id=(trace_turn.span_id if trace_turn is not None else None),
                attributes={
                    "tool.name": action.tool_name,
                },
            )
            if self._trace_session is not None
            else None
        )
        if tool_span is not None and self._trace_session is not None:
            arguments_artifact = self._trace_session.record_artifact(
                kind="tool.arguments",
                mime_type="application/json",
                content=action.arguments,
            )
            tool_span.attributes["app.arguments_artifact"] = arguments_artifact.model_dump(
                mode="json"
            )
        self._record_event(
            events,
            AgentEvent(
                event_type="activity_changed",
                payload={
                    "stage": "tool",
                    "tool": action.tool_name,
                    "label": activity_label,
                },
            ),
        )
        self._record_event(
            events,
            AgentEvent(
                event_type="tool_started",
                payload={"tool": action.tool_name, "label": activity_label},
            ),
        )
        try:
            execution = await self._adapter.execute(
                action.tool_name,
                action.arguments,
                trace_session=self._trace_session,
                parent_span_id=(tool_span.span_id if tool_span is not None else None),
            )
        except ToolUnavailableError:
            if tool_span is not None and self._trace_session is not None:
                self._trace_session.end_span(
                    tool_span,
                    status="error",
                    error_code="tool_unavailable",
                )
            observation = _observation(
                harness_status="tool_unavailable",
                tool=action.tool_name,
                payload=None,
                message="tool is not in the current whitelist",
            )
            observation_text = _canonical_json(observation)
            observation_tokens = self._token_counter.count(observation_text)
            history.append(observation_text)
            self._record_event(
                events,
                AgentEvent(
                    event_type="tool_unavailable",
                    payload={
                        "tool": action.tool_name,
                        "result_tokens": observation_tokens,
                    },
                ),
            )
            self._end_trace_turn(
                trace_turn,
                status="error",
                error_code="tool_unavailable",
                attributes={"app.tool": action.tool_name},
            )
            return {
                "history": history,
                "events": events,
                "pending_action": None,
                "next_context_estimate": (
                    (state.get("next_context_estimate") or 0) + observation_tokens
                ),
                "limitations": limitations,
                "current_turn_span": None,
            }

        state = cast(
            _AgentGraphState,
            {
                **state,
                "model_calls": state.get("model_calls", 0) + execution.model_calls,
                "tool_model_calls": (state.get("tool_model_calls", 0) + execution.model_calls),
                "input_tokens": state.get("input_tokens", 0) + execution.input_tokens,
                "output_tokens": state.get("output_tokens", 0) + execution.output_tokens,
            },
        )
        if self._cancellation.is_cancelled:
            if tool_span is not None and self._trace_session is not None:
                self._trace_session.end_span(
                    tool_span,
                    status="error",
                    error_code="canceled_by_user",
                )
            return self._canceled_state(state, events=events)
        source_refs = _source_refs(action.tool_name, execution.payload)
        merged_sources = _merge_sources(state.get("sources", []), source_refs)
        evidence_refs = _evidence_blocks(execution.payload)
        merged_evidence = _merge_evidence(
            state.get("evidence_blocks", []),
            evidence_refs,
        )
        if tool_span is not None and self._trace_session is not None:
            payload_artifact = self._trace_session.record_artifact(
                kind="tool.payload",
                mime_type="application/json",
                content=execution.payload,
            )
            tool_span.attributes.update(
                {
                    "app.payload_artifact": payload_artifact.model_dump(mode="json"),
                    "app.source_count": len(source_refs),
                    "app.tool_model_calls": execution.model_calls,
                    "app.tool_input_tokens": execution.input_tokens,
                    "app.tool_output_tokens": execution.output_tokens,
                }
            )
            if self._trace_session.mode == "full":
                tool_span.attributes.update(
                    {
                        "app.tool_arguments": action.arguments,
                        "app.diagnostics": execution.diagnostics,
                    }
                )
        self._record_event(
            events,
            AgentEvent(
                event_type="sources_available",
                payload={"sources": source_refs},
            ),
        )
        self._record_event(
            events,
            AgentEvent(
                event_type="tool_finished",
                payload={
                    "tool": action.tool_name,
                    "label": activity_label,
                    "source_count": len(source_refs),
                },
            ),
        )
        observation = _observation(
            harness_status="ok",
            tool=action.tool_name,
            payload=execution.payload,
            message=None,
        )
        observation_text = _canonical_json(observation)
        result_tokens = self._token_counter.count(observation_text)
        prospective = (state.get("next_context_estimate") or 0) + result_tokens
        if tool_span is not None and self._trace_session is not None:
            tool_span.attributes.update(
                {
                    "app.result_tokens": result_tokens,
                    "app.admitted": (prospective <= self._config.max_context_tokens),
                }
            )
            self._trace_session.end_span(tool_span, status="ok")
        self._trace_budget_check(
            trace_turn,
            phase="post_tool",
            limit_kind="context",
            limit_tokens=self._config.max_context_tokens,
            observed_tokens=prospective,
            decision=("admit" if prospective <= self._config.max_context_tokens else "reject"),
        )
        self._record_event(
            events,
            AgentEvent(
                event_type="tool_call",
                payload={
                    "tool": action.tool_name,
                    "arguments": action.arguments,
                    "result_tokens": result_tokens,
                    "diagnostics": execution.diagnostics,
                    "admitted": prospective <= self._config.max_context_tokens,
                    "model_calls": execution.model_calls,
                    "input_tokens": execution.input_tokens,
                    "output_tokens": execution.output_tokens,
                },
            ),
        )
        if prospective > self._config.max_context_tokens:
            limitations = _with_limitation(
                limitations,
                code="context_budget_exceeded",
                message=(
                    "工具结果超过单次上下文预算"
                    f"（max_context_tokens={self._config.max_context_tokens}），"
                    "完整结果未进入历史。"
                ),
                parameter="max_context_tokens",
                limit=self._config.max_context_tokens,
            )
            budget_observation = _observation(
                harness_status="context_budget_exceeded",
                tool=action.tool_name,
                payload=None,
                message="tool result would exceed the context budget",
            )
            budget_text = _canonical_json(budget_observation)
            budget_tokens = self._token_counter.count(budget_text)
            history.append(budget_text)
            self._end_trace_turn(
                trace_turn,
                status="error",
                error_code="context_budget_exceeded",
                attributes={"app.tool": action.tool_name},
            )
            return {
                "history": history,
                "events": events,
                "pending_action": None,
                "tool_calls": state.get("tool_calls", 0) + 1,
                "tools_enabled": False,
                "finalization_mode": True,
                "next_context_estimate": (
                    (state.get("next_context_estimate") or 0) + budget_tokens
                ),
                "limitations": limitations,
                "sources": state.get("sources", []),
                "evidence_blocks": state.get("evidence_blocks", []),
                "model_calls": state.get("model_calls", 0),
                "tool_model_calls": state.get("tool_model_calls", 0),
                "input_tokens": state.get("input_tokens", 0),
                "output_tokens": state.get("output_tokens", 0),
                "current_turn_span": None,
            }
        history.append(observation_text)
        self._end_trace_turn(
            trace_turn,
            status="ok",
            attributes={"app.tool": action.tool_name},
        )
        return {
            "history": history,
            "events": events,
            "pending_action": None,
            "tool_calls": state.get("tool_calls", 0) + 1,
            "next_context_estimate": prospective,
            "limitations": limitations,
            "sources": merged_sources,
            "evidence_blocks": merged_evidence,
            "model_calls": state.get("model_calls", 0),
            "tool_model_calls": state.get("tool_model_calls", 0),
            "input_tokens": state.get("input_tokens", 0),
            "output_tokens": state.get("output_tokens", 0),
            "current_turn_span": None,
        }

    def _route_after_model(self, state: _AgentGraphState) -> str:
        if state.get("status") in {"succeeded", "partial", "failed", "canceled"}:
            return END
        if state.get("pending_action") is not None:
            return "tools"
        return "model"

    async def _complete_with_retry(
        self,
        messages: tuple[ChatMessage, ...],
        state: _AgentGraphState,
        events: list[AgentEvent],
        context_estimate: int,
    ) -> _ModelCallOutcome:
        last_error: ChatProviderError | None = None
        for attempt in range(2):
            extractor = FinishAnswerDeltaExtractor()
            raw_parts: list[str] = []
            emitted_answer = ""
            result: ChatCompletionResult | None = None
            self._record_usage_update(
                state,
                events,
                context_estimate=context_estimate,
                raw_content="",
                failed_attempts=attempt,
            )
            stream = self._chat_provider.stream(
                messages,
                max_output_tokens=self._config.max_output_tokens,
            )
            try:
                while True:
                    next_chunk = asyncio.ensure_future(anext(stream))
                    cancel_wait = asyncio.create_task(self._cancellation.wait())
                    done, _pending = await asyncio.wait(
                        {next_chunk, cancel_wait},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if cancel_wait in done:
                        next_chunk.cancel()
                        await asyncio.gather(next_chunk, return_exceptions=True)
                        return _ModelCallOutcome(
                            result=result,
                            error=None,
                            attempts=attempt + 1,
                            raw_content="".join(raw_parts),
                            emitted_answer=emitted_answer,
                            extractor=extractor,
                            cancelled=True,
                        )
                    cancel_wait.cancel()
                    await asyncio.gather(cancel_wait, return_exceptions=True)
                    try:
                        chunk = next_chunk.result()
                    except StopAsyncIteration:
                        break
                    if chunk.delta:
                        raw_parts.append(chunk.delta)
                    visible = self._visible_stream_delta(chunk, extractor)
                    if visible:
                        if not emitted_answer:
                            self._record_event(
                                events,
                                AgentEvent(
                                    event_type="activity_changed",
                                    payload={
                                        "stage": "answer",
                                        "label": "正在生成回答",
                                    },
                                ),
                            )
                        self._record_event(
                            events,
                            AgentEvent(
                                event_type="answer_delta",
                                payload={"delta": visible},
                            ),
                        )
                        emitted_answer += visible
                    if chunk.delta:
                        self._record_usage_update(
                            state,
                            events,
                            context_estimate=context_estimate,
                            raw_content="".join(raw_parts),
                            failed_attempts=attempt,
                        )
                    if chunk.result is not None:
                        result = chunk.result
            except ChatProviderError as error:
                last_error = error
                raw_content = "".join(raw_parts)
                if raw_content:
                    return _ModelCallOutcome(
                        result=None,
                        error=error,
                        attempts=attempt + 1,
                        raw_content=raw_content,
                        emitted_answer=emitted_answer,
                        extractor=extractor,
                        cancelled=False,
                    )
                if not error.retryable or attempt == 1:
                    break
                await asyncio.sleep(0.1)
                continue
            finally:
                close_stream = getattr(stream, "aclose", None)
                if close_stream is not None:
                    await close_stream()
            if result is None:
                last_error = ChatProviderError(
                    code="chat_stream_invalid",
                    message="chat provider stream ended without a completion result",
                    retryable=False,
                )
                break
            raw_content = "".join(raw_parts)
            if result.content and raw_content != result.content:
                last_error = ChatProviderError(
                    code="chat_stream_invalid",
                    message="chat provider stream content does not match the completion result",
                    retryable=False,
                )
                break
            self._record_usage_update(
                state,
                events,
                context_estimate=context_estimate,
                raw_content=result.content,
                failed_attempts=attempt,
                result=result,
            )
            return _ModelCallOutcome(
                result=result,
                error=None,
                attempts=attempt + 1,
                raw_content=result.content,
                emitted_answer=emitted_answer,
                extractor=extractor,
                cancelled=False,
            )
        return _ModelCallOutcome(
            result=None,
            error=last_error,
            attempts=2 if last_error is not None and last_error.retryable else 1,
            raw_content="",
            emitted_answer="",
            extractor=None,
            cancelled=False,
        )

    def _visible_stream_delta(
        self,
        chunk: ChatStreamChunk,
        extractor: FinishAnswerDeltaExtractor,
    ) -> str:
        """从当前原始 chunk 中提取可展示的最终答案增量。"""

        if not chunk.delta:
            return ""
        return extractor.feed(chunk.delta)

    def _record_usage_update(
        self,
        state: _AgentGraphState,
        events: list[AgentEvent],
        *,
        context_estimate: int,
        raw_content: str,
        failed_attempts: int,
        result: ChatCompletionResult | None = None,
    ) -> None:
        """发布当前累计 token；供应商 usage 只替换它对应的一次调用。"""

        provider_input = result.input_tokens if result is not None else 0
        provider_output = result.output_tokens if result is not None else 0
        current_input = provider_input if provider_input > 0 else context_estimate
        current_output = (
            provider_output if provider_output > 0 else self._token_counter.count(raw_content)
        )
        input_tokens = (
            state.get("input_tokens", 0) + current_input + failed_attempts * context_estimate
        )
        output_tokens = state.get("output_tokens", 0) + current_output
        self._record_event(
            events,
            AgentEvent(
                event_type="usage_update",
                payload={
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "input_source": "provider" if provider_input > 0 else "estimated",
                    "output_source": "provider" if provider_output > 0 else "estimated",
                },
            ),
        )

    def _terminal_state(
        self,
        state: _AgentGraphState,
        *,
        events: list[AgentEvent],
        status: Literal["succeeded", "partial", "failed", "canceled"],
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> _AgentGraphState:
        self._end_trace_turn(
            state.get("current_turn_span"),
            status="ok" if status == "succeeded" else "error",
            error_code=None if status == "succeeded" else reason,
            attributes={"app.termination_reason": reason},
        )
        merged = cast(_AgentGraphState, {**state, **(extra or {})})
        event_type = {
            "failed": "run_failed",
            "canceled": "run_canceled",
        }.get(status, "answer_completed")
        self._record_event(
            events,
            AgentEvent(
                event_type=event_type,
                payload={
                    "termination_reason": reason,
                    "answer": merged.get("final_answer"),
                    "answer_completeness": ("complete" if status == "succeeded" else "partial"),
                    "sources": merged.get("sources", []),
                    "input_tokens": merged.get("input_tokens", 0),
                    "output_tokens": merged.get("output_tokens", 0),
                },
            ),
        )
        return cast(
            _AgentGraphState,
            {
                **merged,
                "events": events,
                "status": status,
                "answer_completeness": "complete" if status == "succeeded" else "partial",
                "termination_reason": reason,
                "current_turn_span": None,
            },
        )

    def _initial_completeness(self) -> Literal["complete", "partial"]:
        return "partial" if self._reader.completeness == "partial" else "complete"

    def _initial_limitations(self) -> list[AgentLimitation]:
        if self._reader.completeness != "partial":
            return []
        failed_count = len(self._reader.manifest.failed_documents)
        return [
            AgentLimitation(
                code="collection_partial",
                message=f"当前知识库有 {failed_count} 篇来源未成功入库。",
            )
        ]

    def _collection_status(self) -> dict[str, Any]:
        manifest = self._reader.manifest
        return {
            "snapshot_id": manifest.snapshot_id,
            "completeness": manifest.completeness,
            "failed_documents": [
                {
                    "doc_id": failure.doc_id,
                    "title": failure.doc_id,
                    "reason": failure.message,
                }
                for failure in manifest.failed_documents
            ],
        }

    @staticmethod
    def _render_user_prompt(
        question: str,
        history: list[str],
    ) -> str:
        history_text = "\n\n".join(history) if history else "（空）"
        return f"<question>\n{question}\n</question>\n\n<history>\n{history_text}\n</history>"


def _source_refs(tool: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """提取本次工具实际返回的块来源，并为前端生成安全摘要。"""

    candidates = payload.get("blocks")
    if not isinstance(candidates, list):
        candidates = payload.get("matches")
    if not isinstance(candidates, list):
        return []
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        doc_id = candidate.get("doc_id")
        block_id = candidate.get("block_id")
        if not isinstance(doc_id, str) or not isinstance(block_id, str):
            continue
        source_key = block_id
        if source_key in seen:
            continue
        seen.add(source_key)
        excerpt = candidate.get("text")
        if not isinstance(excerpt, str):
            excerpt = candidate.get("snippet")
        sources.append(
            {
                "source_key": source_key,
                "doc_id": doc_id,
                "block_id": block_id,
                "doc_title": str(candidate.get("doc_title") or doc_id),
                "source_label": str(candidate.get("source_label") or ""),
                "section_path": [str(value) for value in candidate.get("section_path", [])],
                "excerpt": (excerpt or "")[:240],
                "tool": tool,
            }
        )
    return sources


def _evidence_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """提取本次工具返回的可引用证据，并标记全文或摘录来源。"""

    candidates = payload.get("blocks")
    if not isinstance(candidates, list):
        candidates = payload.get("matches")
    if not isinstance(candidates, list):
        return []
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        block_id = candidate.get("block_id")
        text = candidate.get("text")
        text_is_complete = isinstance(text, str)
        if not isinstance(text, str):
            text = candidate.get("snippet")
        if (
            not isinstance(block_id, str)
            or not isinstance(text, str)
            or not text.strip()
            or block_id in seen
        ):
            continue
        seen.add(block_id)
        evidence.append(
            {
                "block_id": block_id,
                "doc_id": str(candidate.get("doc_id") or ""),
                "doc_title": str(candidate.get("doc_title") or ""),
                "source_label": str(candidate.get("source_label") or ""),
                "section_path": [str(value) for value in candidate.get("section_path", [])],
                "text": text,
                # list_blocks/get_blocks/search_blocks 的 text 是全文；
                # scan_term 的 snippet 只是命中窗口。
                "_evidence_text_priority": 2 if text_is_complete else 1,
            }
        )
    return evidence


def _merge_evidence(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 block_id 合并证据；全文必须覆盖先出现但被截断的 snippet。"""

    merged = list(existing)
    index_by_id = {
        item.get("block_id"): index for index, item in enumerate(merged) if isinstance(item, dict)
    }
    for item in incoming:
        block_id = item["block_id"]
        existing_index = index_by_id.get(block_id)
        if existing_index is None:
            merged.append(item)
            index_by_id[block_id] = len(merged) - 1
            continue
        current = merged[existing_index]
        current_priority = int(current.get("_evidence_text_priority", 0))
        incoming_priority = int(item.get("_evidence_text_priority", 0))
        if incoming_priority > current_priority:
            merged[existing_index] = item
    return merged


def _merge_sources(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 doc_id＋block_id 合并本次运行来源，保持首次出现顺序。"""

    merged = list(existing)
    seen = {source.get("source_key") for source in merged if isinstance(source, dict)}
    for source in incoming:
        if source["source_key"] not in seen:
            merged.append(source)
            seen.add(source["source_key"])
    return merged


def _observation(
    *,
    harness_status: str,
    tool: str | None,
    payload: dict[str, Any] | None,
    message: str | None,
) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "harness_status": harness_status,
        "tool": tool,
        "payload": payload,
        "message": message,
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _append_limitation(
    limitations: list[AgentLimitation],
    *,
    code: str,
    message: str,
    parameter: str | None = None,
    limit: int | None = None,
) -> None:
    if not any(limitation.code == code for limitation in limitations):
        limitations.append(
            AgentLimitation(
                code=code,
                message=message,
                parameter=parameter,
                limit=limit,
            )
        )


def _with_limitation(
    limitations: list[AgentLimitation],
    *,
    code: str,
    message: str,
    parameter: str | None = None,
    limit: int | None = None,
) -> list[AgentLimitation]:
    updated = list(limitations)
    _append_limitation(
        updated,
        code=code,
        message=message,
        parameter=parameter,
        limit=limit,
    )
    return updated


def _render_answer_with_limitations(
    answer: str,
    limitations: list[AgentLimitation],
) -> str:
    if not limitations:
        return answer
    details = "；".join(limitation.message.rstrip("。") for limitation in limitations)
    warning = f"注意：本次回答未完整完成。{details}。以下内容仅基于已经获取并进入上下文的证据。"
    return f"{warning}\n\n{answer}"


def _partial_answer_with_notice(answer: str | None) -> tuple[str, str]:
    """补齐用户可见的部分回答声明，并返回新增流式增量。

    模型可能被输出长度截断在结构化 finish JSON 的 answer 字符串中间。此时已经流出的正文仍然
    是用户实际看到的内容，不能因为协议未闭合而丢弃；追加固定声明可保证用户
    知道回答不完整，并让 checker 获得稳定、可核验的截断事实。
    """

    content = (answer or "").strip()
    if any(marker in content for marker in _TRUNCATION_NOTICE_MARKERS):
        return content, ""
    if not content:
        return _PARTIAL_ANSWER_NOTICE, _PARTIAL_ANSWER_NOTICE
    suffix = f"\n\n---\n\n{_PARTIAL_ANSWER_NOTICE}"
    return f"{content}{suffix}", suffix
