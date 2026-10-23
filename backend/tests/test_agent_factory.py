from __future__ import annotations

from pathlib import Path

from app.agent.factory import AgentExecutionService
from app.agent.prompt import release_prompt_bundle
from app.providers.chat import FakeChatProvider
from app.providers.embeddings import FakeEmbeddingProvider
from app.settings.models import RuntimeSettingsResponse
from app.storage.credentials import (
    CHAT_API_KEY_USERNAME,
    EMBEDDING_API_KEY_USERNAME,
)


class _MemoryCredentials:
    def __init__(self) -> None:
        self._values = {
            EMBEDDING_API_KEY_USERNAME: "embedding-key",
            CHAT_API_KEY_USERNAME: "chat-key",
        }

    def get(self, username: str) -> str | None:
        return self._values.get(username)

    def set(self, username: str, value: str) -> None:
        self._values[username] = value

    def delete(self, username: str) -> None:
        self._values.pop(username, None)


def test_agent_execution_service_accepts_frozen_evaluation_settings(
    tmp_path: Path,
) -> None:
    frozen_prompt = release_prompt_bundle()
    service = AgentExecutionService(
        collection_data_root=tmp_path,
        credentials=_MemoryCredentials(),
        settings_provider=lambda: RuntimeSettingsResponse(),
        trace_store=None,
        embedding_provider_factory=FakeEmbeddingProvider,
        chat_provider_factory=lambda _key: FakeChatProvider(["Thought: 完成\nAction: Finish[ok]"]),
    )
    frozen_settings = RuntimeSettingsResponse(
        top_k=9,
        max_tool_calls=3,
        max_output_tokens=4_096,
        max_context_tokens=20_000,
        max_request_tokens=30_000,
        trace_mode="full",
    )

    runtime = service.create_runtime(
        settings_override=frozen_settings,
        prompt_bundle_override=frozen_prompt,
    )

    assert runtime._config.max_tool_calls == 3
    assert runtime._config.max_context_tokens == 20_000
    assert runtime._retrieval_profile.top_k == 9
    assert runtime._prompt_bundle is frozen_prompt
