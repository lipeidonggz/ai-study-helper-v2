from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)
from openai.types.chat import ChatCompletionMessageParam

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL_ID = "deepseek-flash"
DEFAULT_CHAT_TEMPERATURE = 0.0
DEFAULT_MAX_OUTPUT_TOKENS = 8192
DEEPSEEK_REQUEST_TIMEOUT_SECONDS = 60
DEFAULT_THINKING_ENABLED = False


@dataclass(frozen=True, slots=True)
class ChatProviderProfile:
    """一次对话模型调用固定的供应商与模型契约。"""

    provider: str
    model_id: str
    base_url: str
    temperature: float
    max_output_tokens: int
    thinking_enabled: bool


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """发送给对话模型的一条消息。"""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ChatCompletionResult:
    """一次对话补全的文本与供应商 usage。"""

    content: str
    request_id: str
    actual_model_id: str
    finish_reason: str | None
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class ChatStreamChunk:
    """流式对话中的一段正文；最后一项携带完整调用结果。"""

    delta: str
    result: ChatCompletionResult | None = None


@dataclass(frozen=True, slots=True)
class ChatProviderError(Exception):
    """可分类的对话模型失败。"""

    code: str
    message: str
    retryable: bool

    def __str__(self) -> str:
        """返回不包含凭证和用户正文的错误摘要。"""

        return self.message


class ChatProvider(Protocol):
    """Agent Runtime 使用的最小对话模型协议。"""

    @property
    def profile(self) -> ChatProviderProfile:
        """返回本次运行固定的模型与采样参数。"""
        ...

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> ChatCompletionResult:
        """执行一次非流式对话补全。"""
        ...

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[ChatStreamChunk]:
        """执行一次流式对话补全，并在结束时返回供应商 usage。"""
        ...


class FakeChatProvider:
    """按预设内容依次返回的确定性 chat provider。"""

    def __init__(
        self,
        responses: Sequence[str],
        *,
        stream_chunk_size: int = 7,
        auto_empty_citation_annotations: bool = True,
        auto_source_meta_assertions: bool = True,
    ) -> None:
        if not responses:
            raise ValueError("fake chat provider requires at least one response")
        if stream_chunk_size <= 0:
            raise ValueError("stream_chunk_size must be positive")
        self._responses = list(responses)
        self._stream_chunk_size = stream_chunk_size
        self._auto_empty_citation_annotations = auto_empty_citation_annotations
        self._auto_source_meta_assertions = auto_source_meta_assertions
        self._profile = ChatProviderProfile(
            provider="fake",
            model_id="fake-agent-chat",
            base_url="fake://chat",
            temperature=DEFAULT_CHAT_TEMPERATURE,
            max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            thinking_enabled=DEFAULT_THINKING_ENABLED,
        )
        self.requests: list[tuple[ChatMessage, ...]] = []
        self.max_output_requests: list[int | None] = []
        self.stream_requests = 0

    @property
    def profile(self) -> ChatProviderProfile:
        """返回 fake provider 的固定 profile。"""

        return self._profile

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> ChatCompletionResult:
        """返回下一条预设响应，并记录本次输入。"""

        return self._take_response(messages, max_output_tokens)

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[ChatStreamChunk]:
        """按固定长度切分预设响应，便于确定性验证流式协议。"""

        result = await self.complete(messages, max_output_tokens=max_output_tokens)
        self.stream_requests += 1
        for offset in range(0, len(result.content), self._stream_chunk_size):
            yield ChatStreamChunk(delta=result.content[offset : offset + self._stream_chunk_size])
        yield ChatStreamChunk(delta="", result=result)

    def _take_response(
        self,
        messages: Sequence[ChatMessage],
        max_output_tokens: int | None,
    ) -> ChatCompletionResult:
        if (
            self._auto_empty_citation_annotations
            and messages
            and messages[0].role == "system"
            and "后置引用标注器" in messages[0].content
        ):
            content = '{"annotations":[]}'
            self.requests.append(tuple(messages))
            self.max_output_requests.append(max_output_tokens)
            input_tokens = sum(max(1, len(message.content) // 4) for message in messages)
            return ChatCompletionResult(
                content=content,
                request_id=f"fake-chat-{len(self.requests):04d}",
                actual_model_id=self.profile.model_id,
                finish_reason="stop",
                input_tokens=input_tokens,
                output_tokens=max(1, len(content) // 4),
            )
        if (
            self._auto_source_meta_assertions
            and messages
            and messages[0].role == "system"
            and "来源元描述门禁" in messages[0].content
        ):
            packet = json.loads(messages[-1].content)
            content = json.dumps(
                {
                    "assessments": [
                        {
                            "span_id": span["span_id"],
                            "has_content_claim": True,
                            "source_relation": "none",
                            "answer_relation": "none",
                            "reason": "fake provider 默认放行正文",
                        }
                        for span in packet["spans"]
                    ]
                },
                ensure_ascii=False,
            )
            self.requests.append(tuple(messages))
            self.max_output_requests.append(max_output_tokens)
            input_tokens = sum(max(1, len(message.content) // 4) for message in messages)
            return ChatCompletionResult(
                content=content,
                request_id=f"fake-chat-{len(self.requests):04d}",
                actual_model_id=self.profile.model_id,
                finish_reason="stop",
                input_tokens=input_tokens,
                output_tokens=max(1, len(content) // 4),
            )
        if not self._responses:
            raise ChatProviderError(
                code="chat_fake_exhausted",
                message="fake chat provider has no remaining responses",
                retryable=False,
            )
        self.requests.append(tuple(messages))
        self.max_output_requests.append(max_output_tokens)
        content = self._responses.pop(0)
        input_tokens = sum(max(1, len(message.content) // 4) for message in messages)
        return ChatCompletionResult(
            content=content,
            request_id=f"fake-chat-{len(self.requests):04d}",
            actual_model_id=self.profile.model_id,
            finish_reason="stop",
            input_tokens=input_tokens,
            output_tokens=max(1, len(content) // 4),
        )


class DeepSeekChatProvider:
    """通过 OpenAI Python SDK 调用 DeepSeek OpenAI-compatible API。"""

    def __init__(
        self,
        api_key: str | None,
        *,
        profile: ChatProviderProfile | None = None,
    ) -> None:
        self._api_key = api_key or ""
        self._profile = profile or ChatProviderProfile(
            provider="deepseek",
            model_id=DEEPSEEK_MODEL_ID,
            base_url=DEEPSEEK_BASE_URL,
            temperature=DEFAULT_CHAT_TEMPERATURE,
            max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            thinking_enabled=DEFAULT_THINKING_ENABLED,
        )
        self._client: AsyncOpenAI | None = (
            AsyncOpenAI(
                api_key=self._api_key,
                base_url=self.profile.base_url,
                timeout=DEEPSEEK_REQUEST_TIMEOUT_SECONDS,
                max_retries=0,
            )
            if self._api_key.strip()
            else None
        )

    @property
    def profile(self) -> ChatProviderProfile:
        """返回 DeepSeek 对话 profile。"""

        return self._profile

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> ChatCompletionResult:
        """调用 DeepSeek，并把供应商异常映射为稳定领域错误。"""

        if not self._api_key.strip():
            raise ChatProviderError(
                code="chat_api_key_missing",
                message="chat API key is not configured",
                retryable=False,
            )
        if self._client is None:
            raise ChatProviderError(
                code="chat_api_key_missing",
                message="chat API key is not configured",
                retryable=False,
            )
        if not messages or any(
            message.role not in {"system", "user", "assistant"} for message in messages
        ):
            raise ChatProviderError(
                code="chat_messages_invalid",
                message="chat messages are empty or contain an unsupported role",
                retryable=False,
            )
        message_payload: list[ChatCompletionMessageParam] = [
            {"role": message.role, "content": message.content}  # type: ignore[typeddict-item]
            for message in messages
        ]
        try:
            response = await self._client.chat.completions.create(
                model=self.profile.model_id,
                messages=message_payload,
                temperature=self.profile.temperature,
                max_tokens=max_output_tokens or self.profile.max_output_tokens,
                response_format={"type": "json_object"},
                extra_body={
                    "thinking": {"type": "enabled" if self.profile.thinking_enabled else "disabled"}
                },
            )
        except AuthenticationError as error:
            raise ChatProviderError(
                code="chat_authentication_failed",
                message="chat provider rejected credentials",
                retryable=False,
            ) from error
        except RateLimitError as error:
            raise ChatProviderError(
                code="chat_rate_limited",
                message="chat provider rate limited the request",
                retryable=True,
            ) from error
        except BadRequestError as error:
            raise ChatProviderError(
                code="chat_request_rejected",
                message="chat provider rejected the request",
                retryable=False,
            ) from error
        except (APIConnectionError, APITimeoutError, APIStatusError) as error:
            raise ChatProviderError(
                code="chat_upstream_unavailable",
                message=f"chat provider request failed: {type(error).__name__}",
                retryable=True,
            ) from error

        if not response.choices:
            raise ChatProviderError(
                code="chat_response_invalid",
                message="chat provider returned no choices",
                retryable=False,
            )
        content = response.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            finish_reason = response.choices[0].finish_reason
            raise ChatProviderError(
                code="chat_response_invalid",
                message=(
                    f"chat provider returned empty final content (finish_reason={finish_reason})"
                ),
                retryable=False,
            )
        usage = response.usage
        response_headers = getattr(getattr(response, "response", None), "headers", None)
        request_id = str(getattr(response, "_request_id", "") or "")
        if not request_id and response_headers is not None:
            request_id = str(
                response_headers.get("x-request-id") or response_headers.get("x-ds-trace-id") or ""
            )
        return ChatCompletionResult(
            content=content,
            request_id=request_id,
            actual_model_id=response.model,
            finish_reason=response.choices[0].finish_reason,
            input_tokens=usage.prompt_tokens if usage is not None else 0,
            output_tokens=usage.completion_tokens if usage is not None else 0,
        )

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[ChatStreamChunk]:
        """流式调用 DeepSeek，只产出正文增量，并保留最终 usage。"""

        if not self._api_key.strip() or self._client is None:
            raise ChatProviderError(
                code="chat_api_key_missing",
                message="chat API key is not configured",
                retryable=False,
            )
        if not messages or any(
            message.role not in {"system", "user", "assistant"} for message in messages
        ):
            raise ChatProviderError(
                code="chat_messages_invalid",
                message="chat messages are empty or contain an unsupported role",
                retryable=False,
            )
        message_payload: list[ChatCompletionMessageParam] = [
            {"role": message.role, "content": message.content}  # type: ignore[typeddict-item]
            for message in messages
        ]
        response = None
        content_parts: list[str] = []
        finish_reason: str | None = None
        actual_model_id = self.profile.model_id
        request_id = ""
        input_tokens = 0
        output_tokens = 0
        try:
            response = await self._client.chat.completions.create(
                model=self.profile.model_id,
                messages=message_payload,
                temperature=self.profile.temperature,
                max_tokens=max_output_tokens or self.profile.max_output_tokens,
                response_format={"type": "json_object"},
                stream=True,
                stream_options={"include_usage": True},
                extra_body={
                    "thinking": {"type": "enabled" if self.profile.thinking_enabled else "disabled"}
                },
            )
            async for chunk in response:
                actual_model_id = chunk.model or actual_model_id
                request_id = str(getattr(chunk, "_request_id", "") or request_id)
                usage = chunk.usage
                if usage is not None:
                    input_tokens = usage.prompt_tokens
                    output_tokens = usage.completion_tokens
                for choice in chunk.choices:
                    finish_reason = choice.finish_reason or finish_reason
                    delta = choice.delta.content
                    if isinstance(delta, str) and delta:
                        content_parts.append(delta)
                        yield ChatStreamChunk(delta=delta)
        except AuthenticationError as error:
            raise ChatProviderError(
                code="chat_authentication_failed",
                message="chat provider rejected credentials",
                retryable=False,
            ) from error
        except RateLimitError as error:
            raise ChatProviderError(
                code="chat_rate_limited",
                message="chat provider rate limited the request",
                retryable=True,
            ) from error
        except BadRequestError as error:
            raise ChatProviderError(
                code="chat_request_rejected",
                message="chat provider rejected the request",
                retryable=False,
            ) from error
        except (APIConnectionError, APITimeoutError, APIStatusError) as error:
            raise ChatProviderError(
                code="chat_upstream_unavailable",
                message=f"chat provider stream failed: {type(error).__name__}",
                retryable=True,
            ) from error
        finally:
            if response is not None:
                await response.close()

        content = "".join(content_parts)
        if not content.strip():
            raise ChatProviderError(
                code="chat_response_invalid",
                message=(
                    f"chat provider returned empty streamed content (finish_reason={finish_reason})"
                ),
                retryable=False,
            )
        yield ChatStreamChunk(
            delta="",
            result=ChatCompletionResult(
                content=content,
                request_id=request_id,
                actual_model_id=actual_model_id,
                finish_reason=finish_reason,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
        )
