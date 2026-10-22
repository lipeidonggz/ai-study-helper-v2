from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from langgraph.graph import END, StateGraph

from app.agent.actions import (
    ActionProtocolError,
    FinishAction,
    ParsedAction,
    ToolAction,
    parse_action,
)
from app.agent.models import AgentLimitation
from app.agent.prompt import PromptBundle, release_prompt_bundle
from app.agent.token_counter import (
    ConservativeObservationTokenCounter,
    ObservationTokenCounter,
)
from app.agent.tool_adapter import (
    RetrievalToolAdapter,
    ToolUnavailableError,
)
from app.providers.chat import (
    ChatCompletionResult,
    ChatMessage,
    ChatProvider,
    ChatProviderError,
)
from app.providers.embeddings import EmbeddingProvider
from app.retrieval.service import RetrievalFailure, RetrievalReadService, RetrievalSnapshotReader

AGENT_LOOP_VERSION = "react-langgraph-v1"
HISTORY_ASSISTANT_WRAPPER = "Assistant response:\n"


@dataclass(frozen=True, slots=True)
class AgentRunConfig:
    """单次 Agent 运行固定的预算与工具上限。"""

    max_tool_calls: int = 8
    max_context_tokens: int = 100_000
    max_request_tokens: int = 400_000
    finalization_reserve_tokens: int = 4_000

    def __post_init__(self) -> None:
        if self.max_tool_calls < 0:
            raise ValueError("max_tool_calls must not be negative")
        if self.max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive")
        if self.max_request_tokens < self.max_context_tokens:
            raise ValueError("max_request_tokens must be at least max_context_tokens")
        if not 0 <= self.finalization_reserve_tokens < self.max_request_tokens:
            raise ValueError("finalization reserve must be within request budget")


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """Agent Runtime 暴露给 API、trace 和测试的结构化事件。"""

    event_type: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """一次 Agent 请求的最终回答和审计信息。"""

    status: Literal["succeeded", "partial", "failed"]
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
    input_tokens: int
    output_tokens: int
    events: tuple[AgentEvent, ...]


class _AgentGraphState(TypedDict, total=False):
    question: str
    history: list[str]
    events: list[AgentEvent]
    pending_action: ParsedAction | None
    final_answer: str | None
    status: Literal["running", "succeeded", "partial", "failed"]
    answer_completeness: Literal["complete", "partial"]
    limitations: list[AgentLimitation]
    termination_reason: str | None
    agent_turns: int
    tool_calls: int
    model_calls: int
    input_tokens: int
    output_tokens: int
    tools_enabled: bool
    finalization_mode: bool
    current_system_prompt: str
    next_context_estimate: int | None
    context_estimate_source: Literal["provider_anchor", "estimated"]


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
    ) -> None:
        self._data_root = data_root.resolve()
        self._chat_provider = chat_provider
        self._embedding_provider = embedding_provider
        self._config = config or AgentRunConfig()
        self._prompt_bundle = prompt_bundle or release_prompt_bundle()
        self._token_counter = token_counter or ConservativeObservationTokenCounter()

    async def run(self, question: str) -> AgentRunResult:
        """在当前活动快照上执行一次单轮 Agent 请求。"""

        normalized_question = question.strip()
        if not normalized_question:
            raise ValueError("question must not be empty")
        try:
            reader = RetrievalReadService(self._data_root).bind_current()
        except RetrievalFailure as error:
            return self._failed_before_run(error.code)
        adapter = RetrievalToolAdapter(reader, self._embedding_provider)
        execution = _AgentGraphExecution(
            question=normalized_question,
            reader=reader,
            adapter=adapter,
            chat_provider=self._chat_provider,
            config=self._config,
            prompt_bundle=self._prompt_bundle,
            token_counter=self._token_counter,
        )
        return await execution.run()

    def _failed_before_run(self, reason: str) -> AgentRunResult:
        return AgentRunResult(
            status="failed",
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
            input_tokens=0,
            output_tokens=0,
            events=(AgentEvent(event_type="run_failed", payload={"reason": reason}),),
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
    ) -> None:
        self._question = question
        self._reader = reader
        self._adapter = adapter
        self._chat_provider = chat_provider
        self._config = config
        self._prompt_bundle = prompt_bundle
        self._token_counter = token_counter
        self._graph = self._build_graph()

    async def run(self) -> AgentRunResult:
        """启动 LangGraph 并把最终状态转换为稳定结果。"""

        initial: _AgentGraphState = {
            "question": self._question,
            "history": [],
            "events": [
                AgentEvent(
                    event_type="run_started",
                    payload={"snapshot_id": self._reader.snapshot_id},
                )
            ],
            "pending_action": None,
            "final_answer": None,
            "status": "running",
            "answer_completeness": self._initial_completeness(),
            "limitations": self._initial_limitations(),
            "termination_reason": None,
            "agent_turns": 0,
            "tool_calls": 0,
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "tools_enabled": True,
            "finalization_mode": False,
            "next_context_estimate": None,
            "context_estimate_source": "estimated",
        }
        final = await self._graph.ainvoke(
            initial,
            {"recursion_limit": self._config.max_tool_calls * 3 + 20},
        )
        status = final.get("status", "failed")
        if status == "running":
            status = "failed"
        limitations = tuple(final.get("limitations", []))
        answer_completeness = final.get("answer_completeness", "partial")
        termination_reason = final.get("termination_reason") or "harness_internal_error"
        return AgentRunResult(
            status=status,
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
            input_tokens=final.get("input_tokens", 0),
            output_tokens=final.get("output_tokens", 0),
            events=tuple(final.get("events", [])),
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

    async def _model_node(self, state: _AgentGraphState) -> _AgentGraphState:
        events = list(state.get("events", []))
        history = list(state.get("history", []))
        limitations = list(state.get("limitations", []))
        tool_calls = state.get("tool_calls", 0)
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
            events.append(
                AgentEvent(
                    event_type="budget_warning",
                    payload={"reason": "max_tool_calls", "limit": self._config.max_tool_calls},
                )
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
            events.append(
                AgentEvent(
                    event_type="budget_warning",
                    payload={"reason": "request_budget", "used_tokens": used_tokens},
                )
            )
        if used_tokens >= self._config.max_request_tokens:
            return self._terminal_state(
                state,
                events=events,
                status="failed",
                reason="request_budget_exceeded",
            )

        system_prompt = self._prompt_bundle.render(
            tools=self._adapter.descriptors if tools_enabled else [],
            collection_status=self._collection_status(),
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
            if not finalization_mode:
                finalization_mode = True
                tools_enabled = False
                system_prompt = self._prompt_bundle.render(
                    tools=[],
                    collection_status=self._collection_status(),
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
                return self._terminal_state(
                    state,
                    events=events,
                    status="failed",
                    reason="context_budget_exceeded",
                    extra={
                        "limitations": _with_limitation(
                            limitations,
                            code="context_budget_exceeded",
                            message=(
                                "完整上下文仍然超过单次上下文预算"
                                f"（max_context_tokens={self._config.max_context_tokens}）。"
                            ),
                            parameter="max_context_tokens",
                            limit=self._config.max_context_tokens,
                        )
                    },
                )

        messages = (
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_prompt),
        )
        result, error, attempts = await self._complete_with_retry(messages)
        model_calls = state.get("model_calls", 0) + attempts
        if result is None:
            reason = error.code if error is not None else "chat_failed"
            failed_input_tokens = attempts * context_estimate
            events.append(
                AgentEvent(
                    event_type="model_call_failed",
                    payload={
                        "attempts": attempts,
                        "error_code": reason,
                        "estimated_input_tokens": failed_input_tokens,
                        "usage_source": "estimated",
                    },
                )
            )
            return self._terminal_state(
                state,
                events=events,
                status="failed",
                reason=reason,
                extra={
                    "model_calls": model_calls,
                    "input_tokens": state.get("input_tokens", 0) + failed_input_tokens,
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
        assistant_wrapper_tokens = self._token_counter.count(HISTORY_ASSISTANT_WRAPPER)
        next_context_estimate = (
            current_input_tokens + current_output_tokens + assistant_wrapper_tokens
        )
        next_context_estimate_source = (
            "provider_anchor" if provider_usage_available else "estimated"
        )
        events.append(
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
            )
        )
        history.append(f"{HISTORY_ASSISTANT_WRAPPER}{result.content}")
        try:
            action = parse_action(result.content)
        except ActionProtocolError as protocol_error:
            observation = _observation(
                harness_status="protocol_error",
                tool=None,
                payload=None,
                message=str(protocol_error),
            )
            observation_text = f"Observation:\n{_canonical_json(observation)}"
            observation_tokens = self._token_counter.count(observation_text)
            history.append(observation_text)
            events.append(
                AgentEvent(
                    event_type="protocol_error",
                    payload={
                        "message": str(protocol_error),
                        "turn": agent_turns,
                        "result_tokens": observation_tokens,
                    },
                )
            )
            return {
                "history": history,
                "events": events,
                "pending_action": None,
                "agent_turns": agent_turns,
                "model_calls": model_calls,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "tools_enabled": tools_enabled,
                "finalization_mode": finalization_mode,
                "current_system_prompt": system_prompt,
                "next_context_estimate": next_context_estimate + observation_tokens,
                "context_estimate_source": next_context_estimate_source,
                "limitations": limitations,
            }

        if isinstance(action, FinishAction):
            answer = _render_answer_with_limitations(action.answer, limitations)
            status: Literal["succeeded", "partial"] = "partial" if limitations else "succeeded"
            events.append(
                AgentEvent(
                    event_type="answer_completed",
                    payload={
                        "turn": agent_turns,
                        "termination_reason": "finish",
                        "answer_completeness": "partial" if limitations else "complete",
                        "limitations": [limitation.model_dump() for limitation in limitations],
                    },
                )
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
                "current_system_prompt": system_prompt,
                "next_context_estimate": next_context_estimate,
                "context_estimate_source": next_context_estimate_source,
                "limitations": limitations,
            }

        if not tools_enabled:
            observation = _observation(
                harness_status="tool_unavailable",
                tool=action.tool_name,
                payload=None,
                message="tools are disabled during finalization",
            )
            observation_text = f"Observation:\n{_canonical_json(observation)}"
            observation_tokens = self._token_counter.count(observation_text)
            history.append(observation_text)
            events.append(
                AgentEvent(
                    event_type="tool_unavailable",
                    payload={
                        "tool": action.tool_name,
                        "reason": "finalization",
                        "result_tokens": observation_tokens,
                    },
                )
            )
            return {
                "history": history,
                "events": events,
                "pending_action": None,
                "agent_turns": agent_turns,
                "model_calls": model_calls,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "tools_enabled": False,
                "finalization_mode": True,
                "current_system_prompt": system_prompt,
                "next_context_estimate": next_context_estimate + observation_tokens,
                "context_estimate_source": next_context_estimate_source,
                "limitations": limitations,
            }
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
            "current_system_prompt": system_prompt,
            "next_context_estimate": next_context_estimate,
            "context_estimate_source": next_context_estimate_source,
            "limitations": limitations,
        }

    async def _tool_node(self, state: _AgentGraphState) -> _AgentGraphState:
        action = state.get("pending_action")
        if not isinstance(action, ToolAction):
            return {"pending_action": None}
        events = list(state.get("events", []))
        history = list(state.get("history", []))
        limitations = list(state.get("limitations", []))
        try:
            execution = await self._adapter.execute(action.tool_name, action.arguments)
        except ToolUnavailableError:
            observation = _observation(
                harness_status="tool_unavailable",
                tool=action.tool_name,
                payload=None,
                message="tool is not in the current whitelist",
            )
            observation_text = f"Observation:\n{_canonical_json(observation)}"
            observation_tokens = self._token_counter.count(observation_text)
            history.append(observation_text)
            events.append(
                AgentEvent(
                    event_type="tool_unavailable",
                    payload={
                        "tool": action.tool_name,
                        "result_tokens": observation_tokens,
                    },
                )
            )
            return {
                "history": history,
                "events": events,
                "pending_action": None,
                "next_context_estimate": (
                    (state.get("next_context_estimate") or 0) + observation_tokens
                ),
                "limitations": limitations,
            }

        observation = _observation(
            harness_status="ok",
            tool=action.tool_name,
            payload=execution.payload,
            message=None,
        )
        observation_text = f"Observation:\n{_canonical_json(observation)}"
        result_tokens = self._token_counter.count(observation_text)
        prospective = (state.get("next_context_estimate") or 0) + result_tokens
        events.append(
            AgentEvent(
                event_type="tool_call",
                payload={
                    "tool": action.tool_name,
                    "arguments": action.arguments,
                    "result_tokens": result_tokens,
                    "diagnostics": execution.diagnostics,
                    "admitted": prospective <= self._config.max_context_tokens,
                },
            )
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
            budget_text = f"Observation:\n{_canonical_json(budget_observation)}"
            budget_tokens = self._token_counter.count(budget_text)
            history.append(budget_text)
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
            }
        history.append(observation_text)
        return {
            "history": history,
            "events": events,
            "pending_action": None,
            "tool_calls": state.get("tool_calls", 0) + 1,
            "next_context_estimate": prospective,
            "limitations": limitations,
        }

    def _route_after_model(self, state: _AgentGraphState) -> str:
        if state.get("status") in {"succeeded", "partial", "failed"}:
            return END
        if state.get("pending_action") is not None:
            return "tools"
        return "model"

    async def _complete_with_retry(
        self,
        messages: tuple[ChatMessage, ...],
    ) -> tuple[ChatCompletionResult | None, ChatProviderError | None, int]:
        last_error: ChatProviderError | None = None
        for attempt in range(2):
            try:
                return await self._chat_provider.complete(messages), None, attempt + 1
            except ChatProviderError as error:
                last_error = error
                if not error.retryable or attempt == 1:
                    break
                await asyncio.sleep(0.1)
        return None, last_error, 2 if last_error is not None and last_error.retryable else 1

    def _terminal_state(
        self,
        state: _AgentGraphState,
        *,
        events: list[AgentEvent],
        status: Literal["succeeded", "partial", "failed"],
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> _AgentGraphState:
        events.append(
            AgentEvent(
                event_type="run_failed" if status == "failed" else "answer_completed",
                payload={"termination_reason": reason},
            )
        )
        return cast(
            _AgentGraphState,
            {
                **state,
                **(extra or {}),
                "events": events,
                "status": status,
                "answer_completeness": "complete" if status == "succeeded" else "partial",
                "termination_reason": reason,
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
    def _render_user_prompt(question: str, history: list[str]) -> str:
        history_text = "\n\n".join(history) if history else "（空）"
        return f"<question>\n{question}\n</question>\n\n<history>\n{history_text}\n</history>"


def _observation(
    *,
    harness_status: str,
    tool: str | None,
    payload: dict[str, Any] | None,
    message: str | None,
) -> dict[str, Any]:
    return {
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
