from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select

from app.profile_extraction.errors import ProfileExtractionError
from app.profile_extraction.models import (
    ChannelAttributeSuggestionRequest,
    ChannelAttributeSuggestionResponse,
    ExtractionChannel,
    ExtractionProfile,
    ExtractionProfileConfig,
    ProfileCreateRequest,
    ProfileListResponse,
    ProfileSaveRequest,
    ProfileSelectionResponse,
    ProfileValidationIssue,
    ProfileValidationResponse,
    StrategySuggestionRequest,
    StrategySuggestionResponse,
    SuggestedChannelAttributes,
    SuggestedStrategyPayload,
)
from app.profile_extraction.prompt_compiler import (
    build_channel_attribute_suggestion_prompt,
    build_strategy_suggestion_prompt,
)
from app.profile_extraction.system_profiles import (
    SYSTEM_TECHNICAL_PROFILE_HASH,
    SYSTEM_TECHNICAL_PROFILE_ID,
    profile_hash,
    technical_profile_config,
)
from app.profile_extraction.util import (
    DEFAULT_PROFILE_ID,
    json_dumps,
    new_identifier,
    normalize_name,
)
from app.providers.chat import ChatMessage, ChatProvider, ChatProviderError
from app.storage.credentials import CHAT_API_KEY_USERNAME, CredentialStore
from app.storage.database import StorageDatabase
from app.storage.models import utc_iso, utc_now
from app.storage.profile_extraction_models import (
    ExtractionProfileRow,
    KnowledgeBaseProfileSelectionRow,
)


class ProfileService:
    """管理 Profile draft、冻结、确定性校验和知识库 selected 指针。"""

    def __init__(
        self,
        data_root: Path,
        credentials: CredentialStore,
        *,
        chat_provider_factory: Callable[[str], ChatProvider] | None = None,
        database: StorageDatabase | None = None,
    ) -> None:
        self._database = database or StorageDatabase(data_root)
        self._credentials = credentials
        self._chat_provider_factory = chat_provider_factory
        self._ensure_system_state()

    def list_profiles(self) -> ProfileListResponse:
        """列出系统与用户 Profile，并返回当前 selected 指针。"""

        with self._database.session() as session:
            rows = session.scalars(
                select(ExtractionProfileRow).order_by(
                    ExtractionProfileRow.is_system.desc(),
                    ExtractionProfileRow.created_at,
                )
            ).all()
            selection = session.get(KnowledgeBaseProfileSelectionRow, DEFAULT_PROFILE_ID)
            return ProfileListResponse(
                profiles=[self._profile(row) for row in rows],
                selected_profile_id=(
                    selection.selected_profile_id if selection is not None else None
                ),
            )

    def get_profile(self, profile_id: str) -> ExtractionProfile:
        """读取一个 Profile；不存在时返回稳定 404。"""

        with self._database.session() as session:
            row = session.get(ExtractionProfileRow, profile_id)
            if row is None:
                raise ProfileExtractionError(
                    "profile_not_found",
                    "Profile does not exist",
                    404,
                )
            return self._profile(row)

    def create_profile(self, request: ProfileCreateRequest) -> ExtractionProfile:
        """从空白或已有 Profile 创建新的可编辑 draft。"""

        now = utc_now()
        if request.clone_from_profile_id is None:
            config = ExtractionProfileConfig(name=request.name)
            created_from = None
        else:
            source = self.get_profile(request.clone_from_profile_id)
            config = source.config.model_copy(deep=True)
            config.name = request.name or f"{source.config.name} 副本"
            config.extraction_channels = [
                channel.model_copy(
                    update={"channel_id": new_identifier("ec")},
                    deep=True,
                )
                for channel in config.extraction_channels
            ]
            created_from = source.profile_id
        row = ExtractionProfileRow(
            profile_id=new_identifier("ep"),
            name=config.name,
            description=config.description,
            domain_context=config.domain_context,
            primary_uses_json=json_dumps(config.primary_uses),
            representative_questions_json=json_dumps(config.representative_questions),
            extraction_channels_json=json_dumps(
                [channel.model_dump(mode="json") for channel in config.extraction_channels]
            ),
            exclude_criteria_json=json_dumps(config.exclude_criteria),
            granularity=config.granularity,
            additional_guidance=config.additional_guidance,
            created_from_profile_id=created_from,
            is_system=False,
            status="draft",
            profile_hash=profile_hash(config),
            created_at=now,
            updated_at=now,
        )
        with self._database.session() as session, session.begin():
            session.add(row)
            session.flush()
            return self._profile(row)

    def save_draft(
        self,
        profile_id: str,
        request: ProfileSaveRequest,
    ) -> ExtractionProfile:
        """全量保存 draft；frozen 和系统 Profile 一律拒绝。"""

        config = request.config
        with self._database.session() as session, session.begin():
            row = session.get(ExtractionProfileRow, profile_id)
            if row is None:
                raise ProfileExtractionError(
                    "profile_not_found",
                    "Profile does not exist",
                    404,
                )
            if row.is_system or row.status == "frozen":
                raise ProfileExtractionError(
                    "profile_immutable",
                    "Frozen and system Profiles cannot be modified",
                    409,
                )
            issues = self._validation_issues(config)
            has_conflict = any(issue.code.startswith("conflict_") for issue in issues)
            row.name = config.name
            row.description = config.description
            row.domain_context = config.domain_context
            row.primary_uses_json = json_dumps(config.primary_uses)
            row.representative_questions_json = json_dumps(config.representative_questions)
            row.extraction_channels_json = json_dumps(
                [channel.model_dump(mode="json") for channel in config.extraction_channels]
            )
            row.exclude_criteria_json = json_dumps(config.exclude_criteria)
            row.granularity = config.granularity
            row.additional_guidance = config.additional_guidance
            row.status = "conflicted" if has_conflict else "draft"
            row.profile_hash = profile_hash(config)
            row.updated_at = utc_now()
            session.flush()
            return self._profile(row)

    def delete_draft(self, profile_id: str) -> None:
        """删除未被选为 selected 的 draft；冻结、系统 Profile 不允许删除。"""

        with self._database.session() as session, session.begin():
            row = session.get(ExtractionProfileRow, profile_id)
            if row is None:
                raise ProfileExtractionError(
                    "profile_not_found",
                    "Profile does not exist",
                    404,
                )
            if row.is_system or row.status != "draft":
                raise ProfileExtractionError(
                    "profile_delete_forbidden",
                    "Only non-system draft Profiles can be deleted",
                    409,
                )
            session.delete(row)

    def validate_profile(self, profile_id: str) -> ProfileValidationResponse:
        """执行冻结前的确定性 Profile 校验。"""

        profile = self.get_profile(profile_id)
        issues = self._validation_issues(profile.config)
        return ProfileValidationResponse(valid=not issues, issues=issues)

    def select_profile(self, profile_id: str) -> ProfileSelectionResponse:
        """原子校验、必要时冻结并更新知识库 selected 指针。"""

        with self._database.session() as session, session.begin():
            row = session.get(ExtractionProfileRow, profile_id)
            if row is None:
                raise ProfileExtractionError(
                    "profile_not_found",
                    "Profile does not exist",
                    404,
                )
            if row.status == "conflicted":
                raise ProfileExtractionError(
                    "profile_conflicted",
                    "Resolve Profile conflicts before selecting it",
                    409,
                )
            config = self._config(row)
            issues = self._validation_issues(config)
            if issues:
                raise ProfileExtractionError(
                    "profile_incomplete",
                    issues[0].message,
                    422,
                )
            now = utc_now()
            if row.status == "draft":
                row.status = "frozen"
                row.frozen_at = now
            final_hash = row.profile_hash
            selection = session.get(KnowledgeBaseProfileSelectionRow, DEFAULT_PROFILE_ID)
            if selection is None:
                selection = KnowledgeBaseProfileSelectionRow(
                    knowledge_base_id=DEFAULT_PROFILE_ID,
                    selected_profile_id=profile_id,
                    selected_profile_hash=final_hash,
                    selected_at=now,
                )
                session.add(selection)
            else:
                selection.selected_profile_id = profile_id
                selection.selected_profile_hash = final_hash
                selection.selected_at = now
            session.flush()
            return ProfileSelectionResponse(
                selected_profile_id=profile_id,
                selected_profile_hash=final_hash,
                selected_at=utc_iso(now),
            )

    def selection(self) -> ProfileSelectionResponse:
        """读取当前 selected Profile；未选择时返回空指针。"""

        with self._database.session() as session:
            row = session.get(KnowledgeBaseProfileSelectionRow, DEFAULT_PROFILE_ID)
            if row is None or row.selected_profile_id is None:
                return ProfileSelectionResponse()
            return ProfileSelectionResponse(
                selected_profile_id=row.selected_profile_id,
                selected_profile_hash=row.selected_profile_hash,
                selected_at=utc_iso(row.selected_at) if row.selected_at else None,
            )

    async def suggest_strategy(
        self,
        request: StrategySuggestionRequest,
    ) -> StrategySuggestionResponse:
        """调用模型生成可编辑通道草案，不持久化任何配置。"""

        key = self._credentials.get(CHAT_API_KEY_USERNAME)
        if not key:
            raise ProfileExtractionError(
                "chat_api_key_missing",
                "configure a chat API key before generating a strategy",
                409,
            )
        provider = (
            self._chat_provider_factory(key)
            if self._chat_provider_factory is not None
            else self._default_chat_provider(key)
        )
        messages = [
            ChatMessage(role="system", content=build_strategy_suggestion_prompt()),
            ChatMessage(
                role="user",
                content=json_dumps(request.model_dump(mode="json")),
            ),
        ]
        try:
            result = await provider.complete(messages, max_output_tokens=4_096)
        except ChatProviderError as error:
            raise ProfileExtractionError(
                error.code,
                error.message,
                503 if error.retryable else 502,
            ) from error
        try:
            payload = SuggestedStrategyPayload.model_validate_json(result.content)
        except Exception as error:
            raise ProfileExtractionError(
                "strategy_suggestion_invalid",
                "chat provider returned an invalid strategy suggestion",
                502,
            ) from error
        channels = [
            ExtractionChannel(
                channel_id=new_identifier("ec"),
                **channel.model_dump(mode="json"),
            )
            for channel in payload.channels
        ]
        return StrategySuggestionResponse(
            channels=channels,
            exclude_criteria=payload.exclude_criteria,
            request_id=result.request_id,
        )

    async def suggest_channel_attributes(
        self,
        request: ChannelAttributeSuggestionRequest,
    ) -> ChannelAttributeSuggestionResponse:
        """调用模型补全单个通道的其余属性，不持久化任何配置。"""

        key = self._credentials.get(CHAT_API_KEY_USERNAME)
        if not key:
            raise ProfileExtractionError(
                "chat_api_key_missing",
                "configure a chat API key before generating channel attributes",
                409,
            )
        provider = (
            self._chat_provider_factory(key)
            if self._chat_provider_factory is not None
            else self._default_chat_provider(key)
        )
        messages = [
            ChatMessage(role="system", content=build_channel_attribute_suggestion_prompt()),
            ChatMessage(
                role="user",
                content=json_dumps(request.model_dump(mode="json")),
            ),
        ]
        try:
            result = await provider.complete(messages, max_output_tokens=2_048)
        except ChatProviderError as error:
            raise ProfileExtractionError(
                error.code,
                error.message,
                503 if error.retryable else 502,
            ) from error
        try:
            payload = SuggestedChannelAttributes.model_validate_json(result.content)
        except Exception as error:
            raise ProfileExtractionError(
                "channel_attribute_suggestion_invalid",
                "chat provider returned an invalid channel attribute suggestion",
                502,
            ) from error
        return ChannelAttributeSuggestionResponse(
            **payload.model_dump(mode="json"),
            request_id=result.request_id,
        )

    def config_snapshot(self, profile_id: str) -> ExtractionProfile:
        """返回 Preview 启动时可冻结的 Profile 视图。"""

        return self.get_profile(profile_id)

    def _ensure_system_state(self) -> None:
        now = utc_now()
        config = technical_profile_config()
        with self._database.session() as session, session.begin():
            system = session.get(ExtractionProfileRow, SYSTEM_TECHNICAL_PROFILE_ID)
            if system is None:
                system = ExtractionProfileRow(
                    profile_id=SYSTEM_TECHNICAL_PROFILE_ID,
                    name=config.name,
                    description=config.description,
                    domain_context=config.domain_context,
                    primary_uses_json=json_dumps(config.primary_uses),
                    representative_questions_json=json_dumps(config.representative_questions),
                    extraction_channels_json=json_dumps(
                        [channel.model_dump(mode="json") for channel in config.extraction_channels]
                    ),
                    exclude_criteria_json=json_dumps(config.exclude_criteria),
                    granularity=config.granularity,
                    additional_guidance=config.additional_guidance,
                    created_from_profile_id=None,
                    is_system=True,
                    status="frozen",
                    profile_hash=SYSTEM_TECHNICAL_PROFILE_HASH,
                    created_at=now,
                    updated_at=now,
                    frozen_at=now,
                )
                session.add(system)
            selection = session.get(KnowledgeBaseProfileSelectionRow, DEFAULT_PROFILE_ID)
            if selection is None:
                session.add(
                    KnowledgeBaseProfileSelectionRow(
                        knowledge_base_id=DEFAULT_PROFILE_ID,
                        selected_profile_id=None,
                        selected_profile_hash=None,
                        selected_at=None,
                    )
                )

    @staticmethod
    def _config(row: ExtractionProfileRow) -> ExtractionProfileConfig:
        channels = [
            ExtractionChannel.model_validate(value)
            for value in json.loads(row.extraction_channels_json)
        ]
        return ExtractionProfileConfig(
            name=row.name,
            description=row.description,
            domain_context=row.domain_context,
            primary_uses=json.loads(row.primary_uses_json),
            representative_questions=json.loads(row.representative_questions_json),
            extraction_channels=channels,
            exclude_criteria=json.loads(row.exclude_criteria_json),
            granularity=row.granularity,  # type: ignore[arg-type]
            additional_guidance=row.additional_guidance,
        )

    @classmethod
    def _profile(cls, row: ExtractionProfileRow) -> ExtractionProfile:
        return ExtractionProfile(
            profile_id=row.profile_id,
            config=cls._config(row),
            status=row.status,  # type: ignore[arg-type]
            is_system=row.is_system,
            created_from_profile_id=row.created_from_profile_id,
            profile_hash=row.profile_hash,
            created_at=utc_iso(row.created_at),
            updated_at=utc_iso(row.updated_at),
            frozen_at=utc_iso(row.frozen_at) if row.frozen_at else None,
        )

    @staticmethod
    def _validation_issues(
        config: ExtractionProfileConfig,
    ) -> list[ProfileValidationIssue]:
        issues: list[ProfileValidationIssue] = []
        if not config.name.strip():
            issues.append(
                ProfileValidationIssue(code="profile_name_required", message="Profile 名称必填")
            )
        if not 1 <= len(config.extraction_channels) <= 8:
            issues.append(
                ProfileValidationIssue(
                    code="channel_count_invalid",
                    message="Profile 必须包含 1–8 个抽取通道",
                )
            )
        normalized_names: set[str] = set()
        global_excludes = {normalize_name(value) for value in config.exclude_criteria if value}
        for channel in config.extraction_channels:
            channel_name = normalize_name(channel.name)
            if channel_name and channel_name in normalized_names:
                issues.append(
                    ProfileValidationIssue(
                        code="channel_name_duplicate",
                        message="通道名称不能重复",
                        channel_id=channel.channel_id,
                        field="name",
                    )
                )
            normalized_names.add(channel_name)
            for field_name, value in (
                ("name", channel.name),
                ("purpose", channel.purpose),
                ("activation", channel.activation),
                ("granularity_rule", channel.granularity_rule),
            ):
                if not value.strip():
                    issues.append(
                        ProfileValidationIssue(
                            code="channel_field_required",
                            message=f"通道字段 {field_name} 必填",
                            channel_id=channel.channel_id,
                            field=field_name,
                        )
                    )
            channel_includes = {
                normalize_name(value) for value in channel.include_criteria if value
            }
            channel_excludes = {
                normalize_name(value) for value in channel.exclude_criteria if value
            }
            if channel_includes & channel_excludes:
                issues.append(
                    ProfileValidationIssue(
                        code="conflict_channel_include_exclude",
                        message="通道 include 与 exclude 条件直接冲突",
                        channel_id=channel.channel_id,
                    )
                )
            if channel_includes & global_excludes:
                issues.append(
                    ProfileValidationIssue(
                        code="conflict_channel_global_exclude",
                        message="通道 include 条件与全局排除规则直接冲突",
                        channel_id=channel.channel_id,
                    )
                )
        return issues

    @staticmethod
    def _default_chat_provider(key: str) -> ChatProvider:
        from app.providers.chat import DeepSeekChatProvider

        return DeepSeekChatProvider(key)
