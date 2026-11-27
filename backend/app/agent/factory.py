from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from app.agent.prompt import PromptBundle
from app.agent.runtime import AgentRunConfig, AgentRuntime
from app.ingestion.errors import IngestFailure
from app.providers.chat import ChatProvider, DeepSeekChatProvider
from app.providers.embeddings import DashScopeEmbeddingProvider, EmbeddingProvider
from app.retrieval.hybrid import DEFAULT_HYBRID_RETRIEVAL_PROFILE
from app.settings.models import RuntimeSettingsResponse
from app.storage.credentials import (
    CHAT_API_KEY_USERNAME,
    EMBEDDING_API_KEY_USERNAME,
    CredentialStore,
)
from app.tracing.store import TraceStore


class AgentExecutionService:
    """产品 API 与评测 runner 共用的唯一 Agent 执行组装入口。

    两者必须通过本服务创建 Runtime，不能各自复制 provider、预算、prompt、
    工具或 trace 组装逻辑。评测调用方只替换 collection 数据源读取方式和
    trace 数据根；Agent 行为契约保持一致。
    """

    def __init__(
        self,
        *,
        collection_data_root: Path,
        credentials: CredentialStore,
        settings_provider: Callable[[], RuntimeSettingsResponse],
        trace_store: TraceStore | None,
        embedding_provider_factory: Callable[[], EmbeddingProvider] | None = None,
        chat_provider_factory: Callable[[str], ChatProvider] | None = None,
        prompt_bundle: PromptBundle | None = None,
    ) -> None:
        self._collection_data_root = collection_data_root.resolve()
        self._credentials = credentials
        self._settings_provider = settings_provider
        self._trace_store = trace_store
        self._embedding_provider_factory = embedding_provider_factory
        self._chat_provider_factory = chat_provider_factory
        self._prompt_bundle = prompt_bundle

    def create_runtime(
        self,
        *,
        settings_override: RuntimeSettingsResponse | None = None,
        prompt_bundle_override: PromptBundle | None = None,
    ) -> AgentRuntime:
        """按请求开始时的凭证与设置创建独立 Agent Runtime。

        评测 runner 会传入 ExecutionPlan 冻结的设置和 prompt bundle；产品 API
        不传覆盖值，因此两类调用仍复用同一套 provider、预算、工具与 trace
        组装逻辑。
        """

        embedding_key = self._credentials.get(EMBEDDING_API_KEY_USERNAME)
        if not embedding_key:
            raise IngestFailure(
                "embedding_api_key_missing",
                "configure an embedding API key before running the agent",
                409,
            )
        chat_key = self._credentials.get(CHAT_API_KEY_USERNAME)
        if not chat_key:
            raise IngestFailure(
                "chat_api_key_missing",
                "configure a chat API key before running the agent",
                409,
            )
        settings = settings_override or self._settings_provider()
        embedding_provider = (
            self._embedding_provider_factory()
            if self._embedding_provider_factory is not None
            else DashScopeEmbeddingProvider(embedding_key)
        )
        chat_provider = (
            self._chat_provider_factory(chat_key)
            if self._chat_provider_factory is not None
            else DeepSeekChatProvider(chat_key)
        )
        return AgentRuntime(
            self._collection_data_root,
            chat_provider=chat_provider,
            embedding_provider=embedding_provider,
            config=AgentRunConfig(
                max_tool_calls=settings.max_tool_calls,
                max_context_tokens=settings.max_context_tokens,
                max_request_tokens=settings.max_request_tokens,
                max_output_tokens=settings.max_output_tokens,
            ),
            prompt_bundle=prompt_bundle_override or self._prompt_bundle,
            retrieval_profile=replace(
                DEFAULT_HYBRID_RETRIEVAL_PROFILE,
                top_k=settings.top_k,
            ),
            trace_store=self._trace_store,
            trace_mode=settings.trace_mode,
        )
