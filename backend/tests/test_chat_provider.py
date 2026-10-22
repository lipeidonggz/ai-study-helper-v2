from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.providers.chat import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL_ID,
    ChatMessage,
    ChatProviderError,
    DeepSeekChatProvider,
)


class _FakeAsyncStream:
    def __init__(self, chunks: list[SimpleNamespace]) -> None:
        self._chunks = iter(chunks)
        self.closed = False

    def __aiter__(self) -> _FakeAsyncStream:
        return self

    async def __anext__(self) -> SimpleNamespace:
        try:
            return next(self._chunks)
        except StopIteration as error:
            raise StopAsyncIteration from error

    async def close(self) -> None:
        self.closed = True


def test_deepseek_chat_provider_uses_openai_compatible_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                model=DEEPSEEK_MODEL_ID,
                _request_id="request-1",
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="OK"),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=11, completion_tokens=2),
            )

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            captured["client"] = kwargs
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("app.providers.chat.AsyncOpenAI", FakeClient)

    result = asyncio.run(
        DeepSeekChatProvider("test-key").complete(
            [ChatMessage(role="user", content="reply OK")],
            max_output_tokens=8,
        )
    )

    assert captured["client"] == {
        "api_key": "test-key",
        "base_url": DEEPSEEK_BASE_URL,
        "timeout": 60,
        "max_retries": 0,
    }
    assert captured["model"] == DEEPSEEK_MODEL_ID
    assert captured["temperature"] == 0.0
    assert captured["max_tokens"] == 8
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
    assert result.content == "OK"
    assert result.request_id == "request-1"
    assert result.input_tokens == 11
    assert result.output_tokens == 2


def test_deepseek_chat_provider_requires_key() -> None:
    with pytest.raises(ChatProviderError) as failure:
        asyncio.run(
            DeepSeekChatProvider(None).complete([ChatMessage(role="user", content="hello")])
        )

    assert failure.value.code == "chat_api_key_missing"


def test_deepseek_chat_provider_streams_deltas_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    stream = _FakeAsyncStream(
        [
            SimpleNamespace(
                model=DEEPSEEK_MODEL_ID,
                _request_id="stream-request",
                choices=[SimpleNamespace(delta=SimpleNamespace(content="答"), finish_reason=None)],
                usage=None,
            ),
            SimpleNamespace(
                model=DEEPSEEK_MODEL_ID,
                _request_id="stream-request",
                choices=[
                    SimpleNamespace(delta=SimpleNamespace(content="案"), finish_reason="stop")
                ],
                usage=None,
            ),
            SimpleNamespace(
                model=DEEPSEEK_MODEL_ID,
                _request_id="stream-request",
                choices=[],
                usage=SimpleNamespace(prompt_tokens=9, completion_tokens=2),
            ),
        ]
    )

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return stream

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr("app.providers.chat.AsyncOpenAI", FakeClient)

    async def collect():
        return [
            chunk
            async for chunk in DeepSeekChatProvider("test-key").stream(
                [ChatMessage(role="user", content="stream")],
                max_output_tokens=16,
            )
        ]

    chunks = asyncio.run(collect())

    assert captured["stream"] is True
    assert captured["stream_options"] == {"include_usage": True}
    assert "".join(chunk.delta for chunk in chunks) == "答案"
    assert chunks[-1].result is not None
    assert chunks[-1].result.input_tokens == 9
    assert chunks[-1].result.output_tokens == 2
    assert chunks[-1].result.finish_reason == "stop"
    assert stream.closed is True
