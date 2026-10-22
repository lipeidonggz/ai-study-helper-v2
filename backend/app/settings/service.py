from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime

from app.ingestion.errors import IngestFailure
from app.providers.chat import (
    ChatMessage,
    ChatProvider,
    ChatProviderError,
    DeepSeekChatProvider,
)
from app.providers.embeddings import (
    QWEN_EMBEDDING_DIMENSION,
    QWEN_EMBEDDING_MODEL_ID,
    DashScopeEmbeddingProvider,
    EmbeddingProvider,
    EmbeddingProviderError,
)
from app.settings.models import (
    ChatProviderTestResult,
    ModelSettingsResponse,
    ProviderTestResult,
)
from app.storage.credentials import (
    CHAT_API_KEY_USERNAME,
    EMBEDDING_API_KEY_USERNAME,
    CredentialStore,
)
from app.storage.database import StorageDatabase
from app.storage.settings_models import ProviderSettingRow


class ModelSettingsService:
    """管理非敏感模型状态，并把 Key 操作限制在 CredentialStore。"""

    def __init__(
        self,
        database: StorageDatabase,
        credential_store: CredentialStore,
        *,
        embedding_test_provider_factory: Callable[[str], EmbeddingProvider] | None = None,
        chat_provider_factory: Callable[[str], ChatProvider] | None = None,
    ) -> None:
        self._database = database
        self._credentials = credential_store
        self._provider_factory = embedding_test_provider_factory or (
            lambda key: DashScopeEmbeddingProvider(key)
        )
        self._chat_provider_factory = chat_provider_factory or (
            lambda key: DeepSeekChatProvider(key)
        )

    def status(self) -> ModelSettingsResponse:
        """返回设置状态；绝不读取或返回 Key 明文。"""

        row = self._get_row()
        last_test = self._last_test_from_row(row)
        return ModelSettingsResponse(
            embedding_configured=bool(self._credentials.get(EMBEDDING_API_KEY_USERNAME)),
            embedding_model_id=(row.model_id if row is not None else QWEN_EMBEDDING_MODEL_ID),
            embedding_dimension=(row.dimension if row is not None else QWEN_EMBEDDING_DIMENSION),
            embedding_last_test=last_test,
            chat_configured=bool(self._credentials.get(CHAT_API_KEY_USERNAME)),
        )

    def set_embedding_key(self, api_key: str) -> ModelSettingsResponse:
        """写入 embedding Key，并清除旧 Key 的连通性结果。"""

        self._credentials.set(EMBEDDING_API_KEY_USERNAME, api_key)
        self._clear_last_test()
        return self.status()

    def delete_embedding_key(self) -> ModelSettingsResponse:
        """删除 embedding Key，并清除对应连通性结果。"""

        self._credentials.delete(EMBEDDING_API_KEY_USERNAME)
        self._clear_last_test()
        return self.status()

    def set_chat_key(self, api_key: str) -> ModelSettingsResponse:
        """写入独立对话 Key；响应不回传 Key。"""

        self._credentials.set(CHAT_API_KEY_USERNAME, api_key)
        return self.status()

    def delete_chat_key(self) -> ModelSettingsResponse:
        """删除独立对话 Key。"""

        self._credentials.delete(CHAT_API_KEY_USERNAME)
        return self.status()

    async def test_embedding(self) -> ProviderTestResult:
        """使用已保存 Key 发起一次最小真实 embedding 检查。"""

        api_key = self._credentials.get(EMBEDDING_API_KEY_USERNAME)
        if not api_key:
            raise IngestFailure(
                "embedding_api_key_missing",
                "configure an embedding API key before testing connectivity",
                409,
            )
        provider = self._provider_factory(api_key)
        tested_at = datetime.now(UTC).isoformat()
        try:
            result = await provider.embed_documents(
                ["真实连通性验证：确认 embedding 模型、维度和响应格式。"]
            )
            self._validate_test_vector(result.vectors, provider.profile.dimension)
        except EmbeddingProviderError as error:
            return self._record_test(
                status="failed",
                model_id=provider.profile.model_id,
                dimension=provider.profile.dimension,
                request_id=None,
                input_tokens=0,
                message=error.message,
                tested_at=tested_at,
            )
        except Exception as error:
            return self._record_test(
                status="failed",
                model_id=provider.profile.model_id,
                dimension=provider.profile.dimension,
                request_id=None,
                input_tokens=0,
                message=f"embedding test failed: {type(error).__name__}",
                tested_at=tested_at,
            )
        return self._record_test(
            status="success",
            model_id=provider.profile.model_id,
            dimension=provider.profile.dimension,
            request_id=result.request_id,
            input_tokens=result.input_tokens,
            message=None,
            tested_at=tested_at,
        )

    async def test_chat(self) -> ChatProviderTestResult:
        """使用已保存对话 Key 发起一次最小真实补全检查。"""

        api_key = self._credentials.get(CHAT_API_KEY_USERNAME)
        if not api_key:
            raise IngestFailure(
                "chat_api_key_missing",
                "configure a chat API key before testing connectivity",
                409,
            )
        provider = self._chat_provider_factory(api_key)
        tested_at = datetime.now(UTC).isoformat()
        try:
            result = await provider.complete(
                [
                    ChatMessage(
                        role="user",
                        content="这是连通性测试。只回复 OK，不要输出其他内容。",
                    )
                ],
                max_output_tokens=256,
            )
        except ChatProviderError as error:
            return ChatProviderTestResult(
                status="failed",
                model_id=provider.profile.model_id,
                message=error.message,
                tested_at=tested_at,
            )
        except Exception as error:
            return ChatProviderTestResult(
                status="failed",
                model_id=provider.profile.model_id,
                message=f"chat test failed: {type(error).__name__}",
                tested_at=tested_at,
            )
        return ChatProviderTestResult(
            status="success",
            model_id=result.actual_model_id,
            request_id=result.request_id or None,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            message=None,
            tested_at=tested_at,
        )

    @staticmethod
    def _validate_test_vector(vectors: list[list[float]], dimension: int) -> None:
        if len(vectors) != 1 or len(vectors[0]) != dimension:
            raise IngestFailure(
                "embedding_response_invalid",
                "connectivity test returned an unexpected vector shape",
                502,
            )
        if not all(math.isfinite(value) for value in vectors[0]) or not any(
            value != 0 for value in vectors[0]
        ):
            raise IngestFailure(
                "embedding_vector_invalid",
                "connectivity test returned an invalid vector",
                502,
            )

    def _get_row(self) -> ProviderSettingRow | None:
        with self._database.session() as session:
            return session.get(ProviderSettingRow, "embedding")

    def _clear_last_test(self) -> None:
        with self._database.session() as session, session.begin():
            row = session.get(ProviderSettingRow, "embedding")
            if row is None:
                row = ProviderSettingRow(
                    provider="embedding",
                    model_id=QWEN_EMBEDDING_MODEL_ID,
                    dimension=QWEN_EMBEDDING_DIMENSION,
                )
                session.add(row)
            row.last_test_status = None
            row.last_test_at = None
            row.last_test_message = None
            row.last_test_request_id = None
            row.last_test_input_tokens = None

    def _record_test(
        self,
        *,
        status: str,
        model_id: str,
        dimension: int,
        request_id: str | None,
        input_tokens: int,
        message: str | None,
        tested_at: str,
    ) -> ProviderTestResult:
        with self._database.session() as session, session.begin():
            row = session.get(ProviderSettingRow, "embedding")
            if row is None:
                row = ProviderSettingRow(
                    provider="embedding",
                    model_id=model_id,
                    dimension=dimension,
                )
                session.add(row)
            row.model_id = model_id
            row.dimension = dimension
            row.last_test_status = status
            row.last_test_at = tested_at
            row.last_test_message = message
            row.last_test_request_id = request_id
            row.last_test_input_tokens = input_tokens
        return ProviderTestResult(
            status=status,  # type: ignore[arg-type]
            model_id=model_id,
            dimension=dimension,
            request_id=request_id,
            input_tokens=input_tokens,
            message=message,
            tested_at=tested_at,
        )

    @staticmethod
    def _last_test_from_row(row: ProviderSettingRow | None) -> ProviderTestResult | None:
        if (
            row is None
            or row.last_test_status not in {"success", "failed"}
            or row.last_test_at is None
        ):
            return None
        return ProviderTestResult(
            status=row.last_test_status,  # type: ignore[arg-type]
            model_id=row.model_id,
            dimension=row.dimension,
            request_id=row.last_test_request_id,
            input_tokens=row.last_test_input_tokens or 0,
            message=row.last_test_message,
            tested_at=row.last_test_at,
        )
