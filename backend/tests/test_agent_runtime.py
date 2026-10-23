from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

from app.agent.runtime import AgentRunConfig, AgentRuntime, RunCancellationToken
from app.agent.tool_adapter import RetrievalToolAdapter, ToolUnavailableError
from app.main import create_app
from app.providers.chat import (
    ChatMessage,
    ChatProviderError,
    ChatStreamChunk,
    FakeChatProvider,
)
from app.providers.embeddings import FakeEmbeddingProvider
from app.retrieval.service import RetrievalReadService
from app.tracing.catalog import TraceCatalog
from app.tracing.store import TraceStore

TOKEN = "agent-runtime-test-token"
TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _html() -> bytes:
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        "<title>Agent 文档</title></head><body><article><h1>Agent 文档</h1>"
        "<h2>边界</h2><p>MCP 沙箱边界是这里最重要的原文证据。"
        "这段正文还说明本地进程限制、工具白名单和云端请求边界，"
        "用于保证提取器能够识别完整文章内容。</p>"
        "<h2>补充</h2><p>其他补充正文介绍回答引用、证据核对和未完成事项披露，"
        "确保文档拥有足够长度的可读学习材料。</p>"
        "</article></body></html>"
    ).encode()


async def _wait_for_terminal(client: httpx.AsyncClient, task_id: str) -> None:
    for _ in range(300):
        response = await client.get(
            f"/api/tasks/{task_id}",
            headers={"x-launch-token": TOKEN},
        )
        response.raise_for_status()
        task = response.json()
        if task["status"] in {"succeeded", "failed", "canceled", "interrupted"}:
            assert task["status"] == "succeeded", {
                "status": task["status"],
                "stage": task["stage"],
                "error_code": task.get("error_code"),
                "error_message": task.get("error_message"),
                "failures": json.dumps(task.get("failures"), ensure_ascii=False),
            }
            return
        await asyncio.sleep(0.05)
    raise AssertionError("ingestion task did not finish")


def _prepare_snapshot(tmp_path: Path) -> FakeEmbeddingProvider:
    path = tmp_path / "agent.html"
    path.write_bytes(_html())
    embedding_provider = FakeEmbeddingProvider()

    async def run() -> None:
        app = create_app(
            TOKEN,
            data_root=tmp_path,
            embedding_provider_factory=lambda: embedding_provider,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            created = await client.post(
                "/api/tasks/ingest",
                headers={"x-launch-token": TOKEN},
                json={"sources": [{"source_kind": "file", "source_value": str(path)}]},
            )
            created.raise_for_status()
            await _wait_for_terminal(client, created.json()["task_id"])

    asyncio.run(run())
    return FakeEmbeddingProvider()


class _MemoryCredentials:
    def __init__(self) -> None:
        self.values = {
            "embedding-api-key": "fake-embedding-key",
            "chat-api-key": "fake-chat-key",
        }

    def get(self, username: str) -> str | None:
        return self.values.get(username)

    def set(self, username: str, value: str) -> None:
        self.values[username] = value

    def delete(self, username: str) -> None:
        self.values.pop(username, None)


class _MemoryCredentialsWithoutKeys:
    def get(self, username: str) -> str | None:
        return None

    def set(self, username: str, value: str) -> None:
        raise AssertionError("set must not be called")

    def delete(self, username: str) -> None:
        raise AssertionError("delete must not be called")


def test_agent_runtime_executes_tool_and_finishes(tmp_path: Path) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)
    chat_provider = FakeChatProvider(
        [
            'Thought: 需要查找 MCP。\nAction: search_blocks[{"query":"MCP"}]',
            "Thought: 已取得证据。\nAction: Finish[MCP 位于沙箱边界章节。]",
        ]
    )
    runtime = AgentRuntime(
        tmp_path,
        chat_provider=chat_provider,
        embedding_provider=embedding_provider,
    )

    result = asyncio.run(runtime.run("MCP 在哪个章节？"))

    assert result.status == "succeeded"
    assert result.answer == "MCP 位于沙箱边界章节。"
    assert result.agent_turns == 2
    assert result.tool_calls == 1
    assert result.model_calls == 2
    tool_event = next(event for event in result.events if event.event_type == "tool_call")
    assert tool_event.payload["result_tokens"] > 0
    assert "result_tokens" not in tool_event.payload["diagnostics"]
    model_events = [event for event in result.events if event.event_type == "model_call"]
    assert model_events[0].payload["context_estimate_source"] == "estimated"
    assert model_events[1].payload["context_estimate_source"] == "provider_anchor"
    assert model_events[1].payload["usage_source"] == "provider"
    assert "harness_status" in chat_provider.requests[1][1].content
    assert "result_tokens" not in chat_provider.requests[1][1].content


def test_agent_runtime_emits_safe_answer_activity_usage_and_sources(
    tmp_path: Path,
) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)
    chat_provider = FakeChatProvider(
        [
            'Thought: 需要查找 MCP。\nAction: search_blocks[{"query":"MCP"}]',
            "Thought: 已取得证据。\nAction: Finish[MCP 位于沙箱边界章节。]",
        ],
        stream_chunk_size=4,
    )
    runtime = AgentRuntime(
        tmp_path,
        chat_provider=chat_provider,
        embedding_provider=embedding_provider,
    )
    emitted = []

    result = asyncio.run(runtime.run("MCP 在哪个章节？", emit=emitted.append))

    event_types = [event.event_type for event in emitted]
    assert "activity_changed" in event_types
    assert "tool_started" in event_types
    assert "tool_finished" in event_types
    assert "sources_available" in event_types
    assert "answer_delta" in event_types
    assert "usage_update" in event_types
    assert (
        "".join(event.payload["delta"] for event in emitted if event.event_type == "answer_delta")
        == result.answer
    )
    assert result.sources
    assert result.sources[0]["source_key"] == result.sources[0]["block_id"]
    assert result.sources[0]["source_key"].startswith(result.sources[0]["doc_id"])
    assert all(
        "Thought:" not in event.payload.get("delta", "")
        and "Observation:" not in event.payload.get("delta", "")
        for event in emitted
        if event.event_type == "answer_delta"
    )


def test_agent_runtime_cancel_interrupts_model_stream_without_later_calls(
    tmp_path: Path,
) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)

    class WaitingProvider(FakeChatProvider):
        started = asyncio.Event()

        async def stream(self, messages, *, max_output_tokens=None):
            yield ChatStreamChunk(delta="Thought: 尚未完成。\nAction: Finish[已生成的部分答案")
            self.started.set()
            await asyncio.Event().wait()

    provider = WaitingProvider(["unused"])
    token = RunCancellationToken()
    runtime = AgentRuntime(
        tmp_path,
        chat_provider=provider,
        embedding_provider=embedding_provider,
    )

    async def run() -> tuple:
        task = asyncio.create_task(runtime.run("取消测试", cancellation=token))
        await provider.started.wait()
        token.cancel()
        return await asyncio.wait_for(task, timeout=3)

    result = asyncio.run(run())

    assert result.status == "canceled"
    assert result.termination_reason == "canceled_by_user"
    assert result.answer == "已生成的部分答案"
    assert result.model_calls == 1
    assert result.input_tokens > 0
    assert result.output_tokens > 0
    canceled_event = next(event for event in result.events if event.event_type == "run_canceled")
    assert canceled_event.payload["input_tokens"] == result.input_tokens
    assert canceled_event.payload["output_tokens"] == result.output_tokens


def test_agent_runtime_persists_parented_trace_with_tool_and_budget_data(
    tmp_path: Path,
) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)
    chat_provider = FakeChatProvider(
        [
            'Thought: 需要查找 MCP。\nAction: search_blocks[{"query":"MCP"}]',
            "Thought: 已取得证据。\nAction: Finish[MCP 位于沙箱边界章节。]",
        ],
        stream_chunk_size=5,
    )
    store = TraceStore(tmp_path)
    runtime = AgentRuntime(
        tmp_path,
        chat_provider=chat_provider,
        embedding_provider=embedding_provider,
        trace_store=store,
        trace_mode="full",
    )

    result = asyncio.run(runtime.run("MCP 在哪个章节？"))

    assert result.trace_id and TRACE_ID_PATTERN.fullmatch(result.trace_id)
    assert result.trace_status == "complete"
    assert store.wait_for_idle(3)
    detail = TraceCatalog(tmp_path / "traces").load(result.trace_id)
    store.close()

    names = [span.name for span in detail.spans]
    assert names.count("agent.run") == 1
    assert names.count("agent.turn") == 2
    assert names.count("llm.call") == 2
    assert names.count("tool.call") == 1
    root = next(span for span in detail.spans if span.name == "agent.run")
    turns = [span for span in detail.spans if span.name == "agent.turn"]
    assert all(turn.parent_span_id == root.span_id for turn in turns)
    tool_span = next(span for span in detail.spans if span.name == "tool.call")
    assert tool_span.parent_span_id in {turn.span_id for turn in turns}
    assert tool_span.attributes["app.result_tokens"] > 0
    assert tool_span.attributes["app.diagnostics"]
    assert any(event.name == "budget.check" for turn in turns for event in turn.events)
    assert detail.manifest.trace_status == "complete"
    assert detail.manifest.replay_scope == "retrieval"
    assert detail.manifest.versions["agent_loop_version"] == "react-langgraph-v2"
    assert {artifact.kind for artifact in detail.manifest.artifacts} >= {
        "question",
        "llm.prompt",
        "llm.output",
        "tool.arguments",
        "tool.payload",
        "answer",
    }


def test_trace_queue_failure_does_not_break_agent_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)
    chat_provider = FakeChatProvider(["Thought: 完成。\nAction: Finish[回答仍应成功]"])
    store = TraceStore(tmp_path, queue_capacity=1)
    monkeypatch.setattr(store, "_ensure_worker", lambda: None)
    runtime = AgentRuntime(
        tmp_path,
        chat_provider=chat_provider,
        embedding_provider=embedding_provider,
        trace_store=store,
        trace_mode="full",
    )

    result = asyncio.run(runtime.run("trace 队列满"))

    assert result.status == "succeeded"
    assert result.answer == "回答仍应成功"
    assert store.health(result.trace_id).status == "partial"


def test_metadata_trace_omits_prompt_tool_body_and_answer_content(
    tmp_path: Path,
) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)
    chat_provider = FakeChatProvider(
        [
            'Thought: 检索。\nAction: search_blocks[{"query":"MCP"}]',
            "Thought: 完成。\nAction: Finish[只在 artifact 中出现]",
        ]
    )
    store = TraceStore(tmp_path)
    runtime = AgentRuntime(
        tmp_path,
        chat_provider=chat_provider,
        embedding_provider=embedding_provider,
        trace_store=store,
        trace_mode="metadata",
    )

    result = asyncio.run(runtime.run("metadata trace"))

    assert store.wait_for_idle(3)
    detail = TraceCatalog(tmp_path / "traces").load(result.trace_id)
    store.close()

    assert detail.manifest.question_preview == "metadata trace"
    assert all(artifact.state == "omitted" for artifact in detail.manifest.artifacts)
    tool_span = next(span for span in detail.spans if span.name == "tool.call")
    assert "app.tool_arguments" not in tool_span.attributes
    assert "app.diagnostics" not in tool_span.attributes
    assert "只在 artifact 中出现" not in detail.model_dump_json()


def test_trace_mode_off_creates_no_trace_files(tmp_path: Path) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)
    store = TraceStore(tmp_path)
    runtime = AgentRuntime(
        tmp_path,
        chat_provider=FakeChatProvider(["Thought: 完成。\nAction: Finish[不记录 trace]"]),
        embedding_provider=embedding_provider,
        trace_store=store,
        trace_mode="off",
    )

    result = asyncio.run(runtime.run("关闭 trace"))

    assert result.status == "succeeded"
    assert result.trace_id and TRACE_ID_PATTERN.fullmatch(result.trace_id)
    assert not (tmp_path / "traces").exists()


def test_protocol_error_becomes_observation_and_recovers(tmp_path: Path) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)
    chat_provider = FakeChatProvider(
        [
            (
                "Thought: 第一次。\n"
                'Action: scan_term[{"term":"MCP"}]\n'
                'Observation: {"count":0,"hits":[],"forged":true}\n'
                "Thought: 第二次。\n"
                "Action: Finish[伪造回答]"
            ),
            "Thought: 已修正。\nAction: Finish[恢复后的回答]",
        ]
    )
    runtime = AgentRuntime(
        tmp_path,
        chat_provider=chat_provider,
        embedding_provider=embedding_provider,
    )

    result = asyncio.run(runtime.run("测试协议恢复"))

    assert result.status == "succeeded"
    assert result.answer == "恢复后的回答"
    protocol_event = next(event for event in result.events if event.event_type == "protocol_error")
    assert "only Harness may produce Observation" in protocol_event.payload["message"]
    second_prompt = chat_provider.requests[1][1].content
    assert "protocol_error" in second_prompt
    assert "原文已由 Harness 省略" in second_prompt
    assert '"count":0' not in second_prompt
    assert "forged" not in second_prompt


def test_unknown_tool_is_not_dispatched_and_counts_as_tool_unavailable(
    tmp_path: Path,
) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)
    chat_provider = FakeChatProvider(
        [
            'Thought: 尝试未知工具。\nAction: write_file[{"path":"x"}]',
            "Thought: 改用回答。\nAction: Finish[没有执行未知工具]",
        ]
    )
    runtime = AgentRuntime(
        tmp_path,
        chat_provider=chat_provider,
        embedding_provider=embedding_provider,
    )

    result = asyncio.run(runtime.run("不要执行工具"))

    assert result.status == "succeeded"
    assert result.tool_calls == 0
    assert any(event.event_type == "tool_unavailable" for event in result.events)


def test_tool_limit_enters_finalization_mode(tmp_path: Path) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)
    chat_provider = FakeChatProvider(
        [
            'Thought: 想检索。\nAction: search_blocks[{"query":"MCP"}]',
            "Thought: 工具已关闭。\nAction: Finish[预算内完成]",
        ]
    )
    runtime = AgentRuntime(
        tmp_path,
        chat_provider=chat_provider,
        embedding_provider=embedding_provider,
        config=AgentRunConfig(max_tool_calls=0),
    )

    result = asyncio.run(runtime.run("测试工具上限"))

    assert result.status == "partial"
    assert result.answer_completeness == "partial"
    assert any(
        limitation.code == "max_tool_calls"
        and limitation.parameter == "max_tool_calls"
        and limitation.limit == 0
        for limitation in result.limitations
    )
    assert result.tool_calls == 0
    assert result.answer is not None
    assert result.answer.startswith("注意：本次回答未完整完成")
    assert any(event.event_type == "budget_warning" for event in result.events)
    assert "<finalization_mode>" in chat_provider.requests[0][0].content
    assert (
        "工具" not in chat_provider.requests[0][0].content.split("<tools>")[1].split("</tools>")[0]
    )


def test_truncated_finish_keeps_streamed_partial_answer_and_declares_incomplete(
    tmp_path: Path,
) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)
    chat_provider = FakeChatProvider(
        ["Thought: 输出过长。\nAction: Finish[已经生成但尚未闭合的部分正文"]
    )
    runtime = AgentRuntime(
        tmp_path,
        chat_provider=chat_provider,
        embedding_provider=embedding_provider,
        config=AgentRunConfig(max_tool_calls=0),
    )

    result = asyncio.run(runtime.run("测试截断 Finish"))

    assert result.status == "partial"
    assert result.answer_completeness == "partial"
    assert result.termination_reason == "request_budget_exceeded"
    assert result.answer is not None
    assert "已经生成但尚未闭合的部分正文" in result.answer
    assert "内容未完" in result.answer
    assert any(limitation.code == "request_budget_exceeded" for limitation in result.limitations)


def test_tool_attempt_after_limit_is_blocked_until_finish(tmp_path: Path) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)
    chat_provider = FakeChatProvider(
        [
            'Thought: 第一次检索。\nAction: search_blocks[{"query":"MCP"}]',
            'Thought: 再检索一次。\nAction: search_blocks[{"query":"Agent"}]',
            "Thought: 工具已关闭，直接回答。\nAction: Finish[基于已有证据回答]",
        ]
    )
    runtime = AgentRuntime(
        tmp_path,
        chat_provider=chat_provider,
        embedding_provider=embedding_provider,
        config=AgentRunConfig(max_tool_calls=1),
    )

    result = asyncio.run(runtime.run("测试达到工具上限后继续调用"))

    assert result.status == "partial"
    assert result.tool_calls == 1
    assert any(
        limitation.code == "max_tool_calls"
        and limitation.parameter == "max_tool_calls"
        and limitation.limit == 1
        for limitation in result.limitations
    )
    assert any(event.event_type == "tool_unavailable" for event in result.events)
    second_system_prompt = chat_provider.requests[1][0].content
    tools_text = second_system_prompt.split("<tools>")[1].split("</tools>")[0]
    assert tools_text.strip() == ""
    assert result.answer is not None
    assert result.answer.startswith("注意：本次回答未完整完成")


def test_context_budget_failure_is_explicit(tmp_path: Path) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)
    runtime = AgentRuntime(
        tmp_path,
        chat_provider=FakeChatProvider(["Thought: unused\nAction: Finish[unused]"]),
        embedding_provider=embedding_provider,
        config=AgentRunConfig(
            max_context_tokens=10,
            max_request_tokens=10,
            finalization_reserve_tokens=0,
        ),
    )

    result = asyncio.run(runtime.run("上下文超界"))

    assert result.status == "partial"
    assert result.termination_reason == "context_budget_exceeded"
    assert result.answer_completeness == "partial"
    assert result.answer is not None
    assert "内容未完" in result.answer


def test_oversized_tool_result_is_dropped_and_answer_is_marked_partial(
    tmp_path: Path,
) -> None:
    class ToolResultBudgetCounter:
        @property
        def version(self) -> str:
            return "test-tool-budget"

        def count(self, value) -> int:
            serialized = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            return 1_000 if '"harness_status":"ok"' in serialized else 10

    class SmallUsageProvider(FakeChatProvider):
        async def complete(self, messages, *, max_output_tokens=None):
            result = await super().complete(messages, max_output_tokens=max_output_tokens)
            from app.providers.chat import ChatCompletionResult

            return ChatCompletionResult(
                content=result.content,
                request_id=result.request_id,
                actual_model_id=result.actual_model_id,
                finish_reason=result.finish_reason,
                input_tokens=5,
                output_tokens=2,
            )

    embedding_provider = _prepare_snapshot(tmp_path)
    chat_provider = SmallUsageProvider(
        [
            'Thought: 检索。\nAction: search_blocks[{"query":"MCP"}]',
            "Thought: 预算不足。\nAction: Finish[部分回答]",
        ]
    )
    runtime = AgentRuntime(
        tmp_path,
        chat_provider=chat_provider,
        embedding_provider=embedding_provider,
        config=AgentRunConfig(
            max_context_tokens=100,
            max_request_tokens=1_000,
            finalization_reserve_tokens=0,
        ),
        token_counter=ToolResultBudgetCounter(),
    )

    result = asyncio.run(runtime.run("测试工具结果超界"))

    assert result.status == "partial"
    assert result.answer_completeness == "partial"
    assert any(
        limitation.code == "context_budget_exceeded"
        and limitation.parameter == "max_context_tokens"
        and limitation.limit == 100
        for limitation in result.limitations
    )
    assert result.answer is not None
    assert result.answer.startswith("注意：本次回答未完整完成")
    tool_event = next(event for event in result.events if event.event_type == "tool_call")
    assert tool_event.payload["admitted"] is False
    assert "context_budget_exceeded" in chat_provider.requests[1][1].content
    assert "MCP 沙箱边界" not in chat_provider.requests[1][1].content


def test_tool_adapter_covers_all_deterministic_tools_and_rejects_invalid_input(
    tmp_path: Path,
) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)
    reader = RetrievalReadService(tmp_path).bind_current()
    adapter = RetrievalToolAdapter(reader, embedding_provider)

    async def run() -> tuple[dict, dict, dict, dict]:
        documents = await adapter.execute("list_documents", {})
        doc_id = documents.payload["documents"][0]["doc_id"]
        outline = await adapter.execute("get_outline", {"doc_ids": [doc_id]})
        blocks = await adapter.execute("list_blocks", {"doc_id": doc_id})
        scanned = await adapter.execute("scan_term", {"term": "MCP", "doc_ids": [doc_id]})
        invalid = await adapter.execute("scan_term", {"term": "MCP", "extra": True})
        assert invalid.payload["error"]["code"] == "invalid_argument"
        with pytest.raises(ToolUnavailableError):
            await adapter.execute("write_file", {})
        return documents.payload, outline.payload, blocks.payload, scanned.payload

    documents, outline, blocks, scanned = asyncio.run(run())

    assert documents["total"] == 1
    assert outline["documents"][0]["doc_title"] == "Agent 文档"
    assert blocks["total"] > 0
    assert scanned["count"] == 1
    assert "result_tokens" not in documents


def test_chat_retry_is_bounded_to_one_retry(tmp_path: Path) -> None:
    class RetryChatProvider(FakeChatProvider):
        attempts = 0

        async def complete(
            self,
            messages: tuple[ChatMessage, ...],
            *,
            max_output_tokens: int | None = None,
        ):
            type(self).attempts += 1
            if type(self).attempts == 1:
                from app.providers.chat import ChatProviderError

                raise ChatProviderError(
                    code="chat_rate_limited",
                    message="rate limited",
                    retryable=True,
                )
            return await super().complete(messages, max_output_tokens=max_output_tokens)

    embedding_provider = _prepare_snapshot(tmp_path)
    provider = RetryChatProvider(["Thought: 完成\nAction: Finish[重试成功]"])
    RetryChatProvider.attempts = 0
    runtime = AgentRuntime(
        tmp_path,
        chat_provider=provider,
        embedding_provider=embedding_provider,
    )

    result = asyncio.run(runtime.run("测试重试"))

    assert result.status == "succeeded"
    assert result.answer == "重试成功"
    assert RetryChatProvider.attempts == 2
    assert result.model_calls == 2
    model_event = next(event for event in result.events if event.event_type == "model_call")
    assert model_event.payload["estimated_failed_attempt_input_tokens"] > 0
    assert result.input_tokens > model_event.payload["input_tokens"]


def test_missing_provider_usage_uses_conservative_token_estimate(tmp_path: Path) -> None:
    class MissingUsageProvider(FakeChatProvider):
        async def complete(self, messages, *, max_output_tokens=None):
            result = await super().complete(messages, max_output_tokens=max_output_tokens)
            from app.providers.chat import ChatCompletionResult

            return ChatCompletionResult(
                content=result.content,
                request_id=result.request_id,
                actual_model_id=result.actual_model_id,
                finish_reason=result.finish_reason,
                input_tokens=0,
                output_tokens=0,
            )

    embedding_provider = _prepare_snapshot(tmp_path)
    runtime = AgentRuntime(
        tmp_path,
        chat_provider=MissingUsageProvider(
            ["Thought: 完成\nAction: Finish[估算 usage 时仍可完成]"]
        ),
        embedding_provider=embedding_provider,
    )

    result = asyncio.run(runtime.run("测试缺失 usage"))

    model_event = next(event for event in result.events if event.event_type == "model_call")
    assert model_event.payload["usage_source"] == "estimated"
    assert model_event.payload["input_tokens"] == 0
    assert result.input_tokens > 0
    assert result.output_tokens > 0


def test_protected_agent_api_runs_with_injected_providers(tmp_path: Path) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)
    chat_provider = FakeChatProvider(["Thought: 直接回答。\nAction: Finish[API 运行成功]"])
    app = create_app(
        TOKEN,
        data_root=tmp_path,
        credential_store=_MemoryCredentials(),
        embedding_provider_factory=lambda: embedding_provider,
        chat_provider_factory=lambda _key: chat_provider,
    )

    async def run() -> dict:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/agent/run",
                headers={"x-launch-token": TOKEN},
                json={"question": "测试 API"},
            )
            response.raise_for_status()
            return response.json()

    result = asyncio.run(run())

    assert result["status"] == "succeeded"
    assert result["answer"] == "API 运行成功"
    assert result["answer_completeness"] == "complete"
    assert result["limitations"] == []
    assert result["prompt_bundle_version"] == "chat-v3"
    assert result["observation_token_counter_version"] == "chat-budget-conservative-v1"


def test_agent_api_uses_saved_runtime_settings(tmp_path: Path) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)
    chat_provider = FakeChatProvider(
        [
            'Thought: 检索。\nAction: search_blocks[{"query":"MCP"}]',
            "Thought: 完成。\nAction: Finish[设置已生效]",
        ]
    )
    app = create_app(
        TOKEN,
        data_root=tmp_path,
        credential_store=_MemoryCredentials(),
        embedding_provider_factory=lambda: embedding_provider,
        chat_provider_factory=lambda _key: chat_provider,
    )

    async def run() -> dict:
        transport = httpx.ASGITransport(app=app)
        headers = {"x-launch-token": TOKEN}
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            saved = await client.put(
                "/api/settings/runtime",
                headers=headers,
                json={
                    "top_k": 3,
                    "max_output_tokens": 4096,
                    "max_context_tokens": 80_000,
                    "max_request_tokens": 300_000,
                    "max_tool_calls": 2,
                },
            )
            saved.raise_for_status()
            response = await client.post(
                "/api/agent/run",
                headers=headers,
                json={"question": "验证设置注入"},
            )
            response.raise_for_status()
            return response.json()

    result = asyncio.run(run())

    assert result["status"] == "succeeded"
    tool_event = next(event for event in result["events"] if event["event_type"] == "tool_call")
    assert tool_event["payload"]["diagnostics"]["search_profile"]["top_k"] == 3
    assert chat_provider.max_output_requests == [4096, 4096]


def test_runtime_settings_do_not_change_an_inflight_request(tmp_path: Path) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)

    class BlockingChatProvider(FakeChatProvider):
        started: asyncio.Event
        release: asyncio.Event

        async def complete(self, messages, *, max_output_tokens=None):
            from app.providers.chat import ChatCompletionResult

            self.requests.append(tuple(messages))
            self.max_output_requests.append(max_output_tokens)
            self.started.set()
            await self.release.wait()
            content = self._responses.pop(0)
            return ChatCompletionResult(
                content=content,
                request_id=f"blocking-{len(self.requests)}",
                actual_model_id=self.profile.model_id,
                finish_reason="stop",
                input_tokens=20,
                output_tokens=5,
            )

    chat_provider = BlockingChatProvider(
        [
            "Thought: 第一请求。\nAction: Finish[旧配置]",
            "Thought: 第二请求。\nAction: Finish[新配置]",
        ]
    )
    chat_provider.started = asyncio.Event()
    chat_provider.release = asyncio.Event()
    app = create_app(
        TOKEN,
        data_root=tmp_path,
        credential_store=_MemoryCredentials(),
        embedding_provider_factory=lambda: embedding_provider,
        chat_provider_factory=lambda _key: chat_provider,
    )

    async def run() -> tuple[dict, dict]:
        transport = httpx.ASGITransport(app=app)
        headers = {"x-launch-token": TOKEN}
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            saved = await client.put(
                "/api/settings/runtime",
                headers=headers,
                json={
                    "top_k": 5,
                    "max_output_tokens": 4096,
                    "max_context_tokens": 100_000,
                    "max_request_tokens": 400_000,
                    "max_tool_calls": 8,
                },
            )
            saved.raise_for_status()
            first_request = asyncio.create_task(
                client.post(
                    "/api/agent/run",
                    headers=headers,
                    json={"question": "正在运行的请求"},
                )
            )
            await asyncio.wait_for(chat_provider.started.wait(), timeout=3)
            updated = await client.put(
                "/api/settings/runtime",
                headers=headers,
                json={
                    "top_k": 5,
                    "max_output_tokens": 6144,
                    "max_context_tokens": 100_000,
                    "max_request_tokens": 400_000,
                    "max_tool_calls": 8,
                },
            )
            updated.raise_for_status()
            chat_provider.release.set()
            first = await first_request
            first.raise_for_status()
            second = await client.post(
                "/api/agent/run",
                headers=headers,
                json={"question": "下一个请求"},
            )
            second.raise_for_status()
            return first.json(), second.json()

    first, second = asyncio.run(run())

    assert first["answer"] == "旧配置"
    assert second["answer"] == "新配置"
    assert chat_provider.max_output_requests == [4096, 6144]


def test_agent_api_exposes_persisted_trace_detail_and_artifact(tmp_path: Path) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)
    chat_provider = FakeChatProvider(
        ["Thought: 完成。\nAction: Finish[可诊断回答]"],
        stream_chunk_size=3,
    )
    app = create_app(
        TOKEN,
        data_root=tmp_path,
        credential_store=_MemoryCredentials(),
        embedding_provider_factory=lambda: embedding_provider,
        chat_provider_factory=lambda _key: chat_provider,
    )

    async def run() -> tuple[dict, dict, bytes]:
        transport = httpx.ASGITransport(app=app)
        headers = {"x-launch-token": TOKEN}
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            saved = await client.put(
                "/api/settings/runtime",
                headers=headers,
                json={
                    "top_k": 5,
                    "max_output_tokens": 4096,
                    "max_context_tokens": 100_000,
                    "max_request_tokens": 400_000,
                    "max_tool_calls": 8,
                    "trace_mode": "full",
                },
            )
            saved.raise_for_status()
            response = await client.post(
                "/api/agent/run",
                headers=headers,
                json={"question": "生成可诊断 trace"},
            )
            response.raise_for_status()
            trace_id = response.json()["trace_id"]
            for _ in range(100):
                listed = await client.get("/api/traces", headers=headers)
                listed.raise_for_status()
                if any(item["trace_id"] == trace_id for item in listed.json()):
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("trace did not appear in index")
            detail = await client.get(f"/api/traces/{trace_id}", headers=headers)
            detail.raise_for_status()
            answer_artifact = next(
                artifact
                for artifact in detail.json()["manifest"]["artifacts"]
                if artifact["kind"] == "answer"
            )
            artifact = await client.get(
                f"/api/traces/{trace_id}/artifacts/{answer_artifact['artifact_id']}",
                headers=headers,
            )
            artifact.raise_for_status()
            return response.json(), detail.json(), artifact.content

    response, detail, artifact_content = asyncio.run(run())

    assert response["trace_id"]
    assert detail["manifest"]["trace_id"] == response["trace_id"]
    assert detail["manifest"]["mode"] == "full"
    assert detail["spans"]
    assert artifact_content.decode("utf-8") == "可诊断回答"


def test_agent_api_runtime_failure_returns_structured_http_200(tmp_path: Path) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)

    class FailingChatProvider(FakeChatProvider):
        async def complete(self, messages, *, max_output_tokens=None):
            raise ChatProviderError(
                code="chat_authentication_failed",
                message="invalid key",
                retryable=False,
            )

    app = create_app(
        TOKEN,
        data_root=tmp_path,
        credential_store=_MemoryCredentials(),
        embedding_provider_factory=lambda: embedding_provider,
        chat_provider_factory=lambda _key: FailingChatProvider(["unused"]),
    )

    async def run() -> tuple[int, dict]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/agent/run",
                headers={"x-launch-token": TOKEN},
                json={"question": "触发模型失败"},
            )
            return response.status_code, response.json()

    status_code, body = asyncio.run(run())

    assert status_code == 200
    assert body["status"] == "failed"
    assert body["answer_completeness"] == "partial"
    assert body["termination_reason"] == "chat_authentication_failed"
    assert body["events"][-1]["event_type"] == "run_failed"


def test_streaming_agent_api_replays_events_without_duplicate_text(
    tmp_path: Path,
) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)
    chat_provider = FakeChatProvider(
        ["Thought: 完成。\nAction: Finish[流式回答]"],
        stream_chunk_size=2,
    )
    app = create_app(
        TOKEN,
        data_root=tmp_path,
        credential_store=_MemoryCredentials(),
        embedding_provider_factory=lambda: embedding_provider,
        chat_provider_factory=lambda _key: chat_provider,
    )

    async def run() -> tuple[str, str, int]:
        transport = httpx.ASGITransport(app=app)
        headers = {"x-launch-token": TOKEN}
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            started = await client.post(
                "/api/agent/runs",
                headers=headers,
                json={"question": "流式输出测试"},
            )
            started.raise_for_status()
            run_id = started.json()["run_id"]
            async with client.stream(
                "GET",
                f"/api/agent/runs/{run_id}/events",
                headers=headers,
            ) as response:
                response.raise_for_status()
                event_text = (await response.aread()).decode()
            last_event_id = [
                int(line.removeprefix("id: "))
                for line in event_text.splitlines()
                if line.startswith("id: ")
            ][-1]
            resumed = await client.get(
                f"/api/agent/runs/{run_id}/events",
                headers={
                    **headers,
                    "Last-Event-ID": str(last_event_id),
                },
            )
            resumed.raise_for_status()
            gap = await client.get(
                f"/api/agent/runs/{run_id}/events",
                headers=headers,
                params={"after": last_event_id + 1},
            )
            return event_text, resumed.text, gap.status_code

    event_text, resumed_text, gap_status = asyncio.run(run())

    assert "event: run_started" in event_text
    assert "event: usage_update" in event_text
    assert "event: answer_delta" in event_text
    assert "event: answer_completed" in event_text
    assert "流式回答" in event_text
    assert resumed_text == ""
    assert gap_status == 409


def test_streaming_agent_api_cancel_preserves_canceled_terminal_state(
    tmp_path: Path,
) -> None:
    embedding_provider = _prepare_snapshot(tmp_path)

    class WaitingProvider(FakeChatProvider):
        started = asyncio.Event()

        async def stream(self, messages, *, max_output_tokens=None):
            yield ChatStreamChunk(delta="Thought: 尚未完成。\n")
            self.started.set()
            await asyncio.Event().wait()

    provider = WaitingProvider(["unused"])
    app = create_app(
        TOKEN,
        data_root=tmp_path,
        credential_store=_MemoryCredentials(),
        embedding_provider_factory=lambda: embedding_provider,
        chat_provider_factory=lambda _key: provider,
    )

    async def run() -> str:
        transport = httpx.ASGITransport(app=app)
        headers = {"x-launch-token": TOKEN}
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            started = await client.post(
                "/api/agent/runs",
                headers=headers,
                json={"question": "取消流式输出"},
            )
            started.raise_for_status()
            run_id = started.json()["run_id"]
            await asyncio.wait_for(provider.started.wait(), timeout=3)
            canceled = await client.post(
                f"/api/agent/runs/{run_id}/cancel",
                headers=headers,
            )
            canceled.raise_for_status()
            async with client.stream(
                "GET",
                f"/api/agent/runs/{run_id}/events",
                headers=headers,
                params={"after": 0},
            ) as response:
                response.raise_for_status()
                return (await response.aread()).decode()

    event_text = asyncio.run(run())

    assert "event: run_canceled" in event_text
    assert "event: answer_completed" not in event_text
    assert "event: answer_delta" not in event_text


def test_agent_api_missing_key_is_rejected_before_run(tmp_path: Path) -> None:
    app = create_app(
        TOKEN,
        data_root=tmp_path,
        credential_store=_MemoryCredentialsWithoutKeys(),
    )

    async def run() -> tuple[int, dict]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/agent/run",
                headers={"x-launch-token": TOKEN},
                json={"question": "缺少 Key"},
            )
            return response.status_code, response.json()

    status_code, body = asyncio.run(run())

    assert status_code == 409
    assert body["error"]["code"] in {"embedding_api_key_missing", "chat_api_key_missing"}
