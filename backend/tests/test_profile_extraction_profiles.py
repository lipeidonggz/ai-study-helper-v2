from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.profile_extraction.errors import ProfileExtractionError
from app.profile_extraction.models import (
    ChannelAttributeSuggestionRequest,
    ExtractionChannel,
    ExtractionProfileConfig,
    ProfileCreateRequest,
    ProfileSaveRequest,
    StrategySuggestionRequest,
)
from app.profile_extraction.profile_service import ProfileService
from app.profile_extraction.system_profiles import SYSTEM_TECHNICAL_PROFILE_ID
from app.providers.chat import FakeChatProvider


class _MemoryCredentials:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = values or {}

    def get(self, username: str) -> str | None:
        return self._values.get(username)

    def set(self, username: str, value: str) -> None:
        self._values[username] = value

    def delete(self, username: str) -> None:
        self._values.pop(username, None)


def _valid_config(name: str = "技术学习测试") -> ExtractionProfileConfig:
    return ExtractionProfileConfig(
        name=name,
        description="用于验证 Profile 管理流程。",
        domain_context="软件技术文档",
        primary_uses=["学习技术概念"],
        representative_questions=["这个机制是什么？"],
        extraction_channels=[
            ExtractionChannel(
                channel_id="ec_0123456789abcdef",
                name="核心知识对象",
                purpose="发现可独立学习和检索的知识对象。",
                activation="正文给出定义、作用、条件或边界时启用。",
                include_criteria=["存在稳定名称", "存在独立事实"],
                exclude_criteria=["只有标题或顺带提及"],
                granularity_rule="一个可独立学习对象对应一个 candidate。",
            )
        ],
        exclude_criteria=["导航和页脚"],
    )


def test_system_profile_is_seeded_read_only(tmp_path: Path) -> None:
    service = ProfileService(tmp_path, _MemoryCredentials())

    listed = service.list_profiles()
    system = next(
        profile for profile in listed.profiles if profile.profile_id == SYSTEM_TECHNICAL_PROFILE_ID
    )

    assert system.is_system is True
    assert system.status == "frozen"
    assert len(system.config.extraction_channels) == 4
    with pytest.raises(ProfileExtractionError, match="cannot be modified"):
        service.save_draft(
            system.profile_id,
            ProfileSaveRequest(config=system.config),
        )


def test_draft_freezes_atomically_and_frozen_profile_cannot_be_modified(
    tmp_path: Path,
) -> None:
    service = ProfileService(tmp_path, _MemoryCredentials())
    draft = service.create_profile(ProfileCreateRequest(name="初始"))
    incomplete = ExtractionProfileConfig(
        name="初始",
        extraction_channels=[
            ExtractionChannel(
                channel_id="ec_0123456789abcdef",
                name="",
                purpose="",
                activation="",
                include_criteria=[],
                exclude_criteria=[],
                granularity_rule="",
            )
        ],
    )
    service.save_draft(draft.profile_id, ProfileSaveRequest(config=incomplete))
    assert service.validate_profile(draft.profile_id).valid is False
    with pytest.raises(ProfileExtractionError, match="必填"):
        service.select_profile(draft.profile_id)

    saved = service.save_draft(
        draft.profile_id,
        ProfileSaveRequest(config=_valid_config()),
    )
    selected = service.select_profile(draft.profile_id)
    frozen = service.get_profile(draft.profile_id)

    assert saved.status == "draft"
    assert selected.selected_profile_id == draft.profile_id
    assert frozen.status == "frozen"
    assert frozen.frozen_at is not None
    with pytest.raises(ProfileExtractionError, match="cannot be modified"):
        service.save_draft(
            draft.profile_id,
            ProfileSaveRequest(config=_valid_config("修改")),
        )


def test_clone_creates_new_profile_and_preserves_source(tmp_path: Path) -> None:
    service = ProfileService(tmp_path, _MemoryCredentials())
    draft = service.create_profile(ProfileCreateRequest(name="源 Profile"))
    source = service.save_draft(
        draft.profile_id,
        ProfileSaveRequest(config=_valid_config("源 Profile")),
    )

    clone = service.create_profile(
        ProfileCreateRequest(name="目标 Profile", clone_from_profile_id=source.profile_id)
    )

    assert clone.profile_id != source.profile_id
    assert clone.created_from_profile_id == source.profile_id
    assert clone.config.name == "目标 Profile"
    assert clone.config.extraction_channels[0].channel_id != (
        source.config.extraction_channels[0].channel_id
    )
    assert service.get_profile(source.profile_id).profile_hash == source.profile_hash


def test_failed_selection_keeps_previous_selected_profile(tmp_path: Path) -> None:
    service = ProfileService(tmp_path, _MemoryCredentials())
    first = service.create_profile(ProfileCreateRequest(name="第一个"))
    service.save_draft(first.profile_id, ProfileSaveRequest(config=_valid_config("第一个")))
    service.select_profile(first.profile_id)

    invalid = service.create_profile(ProfileCreateRequest(name="第二个"))
    service.save_draft(
        invalid.profile_id,
        ProfileSaveRequest(config=ExtractionProfileConfig(name="第二个")),
    )
    with pytest.raises(ProfileExtractionError):
        service.select_profile(invalid.profile_id)

    selected = service.selection()
    assert selected.selected_profile_id == first.profile_id
    assert service.get_profile(invalid.profile_id).status == "draft"


def test_strategy_suggestion_returns_editable_channel_draft(tmp_path: Path) -> None:
    response = (
        '{"channels":[{"name":"核心概念","purpose":"发现概念","activation":"出现定义时",'
        '"include_criteria":["有稳定名称"],"exclude_criteria":["导航"],'
        '"granularity_rule":"一个对象一个 candidate","coverage_audit_enabled":false}],'
        '"exclude_criteria":["页脚"]}'
    )
    provider = FakeChatProvider([response])
    service = ProfileService(
        tmp_path,
        _MemoryCredentials({"chat-api-key": "test-key"}),
        chat_provider_factory=lambda _key: provider,
    )

    suggestion = asyncio.run(
        service.suggest_strategy(
            StrategySuggestionRequest(
                profile_name="法律材料",
                primary_uses=["理解合同义务"],
                domain_context="合同与法规",
            )
        )
    )

    assert suggestion.channels[0].name == "核心概念"
    assert suggestion.exclude_criteria == ["页脚"]
    assert len(provider.requests) == 1


def test_channel_attribute_suggestion_uses_name_and_purpose(tmp_path: Path) -> None:
    response = (
        '{"activation":"正文定义概念、解释机制或说明边界时启用。",'
        '"include_criteria":["存在稳定名称","正文提供独立事实"],'
        '"exclude_criteria":["只有标题或导航提及"],'
        '"granularity_rule":"一个可独立学习和检索的对象对应一个 candidate。",'
        '"coverage_audit_enabled":false}'
    )
    provider = FakeChatProvider([response])
    service = ProfileService(
        tmp_path,
        _MemoryCredentials({"chat-api-key": "test-key"}),
        chat_provider_factory=lambda _key: provider,
    )

    suggestion = asyncio.run(
        service.suggest_channel_attributes(
            ChannelAttributeSuggestionRequest(
                profile_name="技术学习",
                domain_context="RAG 与 Agent",
                primary_uses=["学习核心机制"],
                channel_name="核心概念",
                channel_purpose="发现可以独立检索的概念",
            )
        )
    )

    request_payload = provider.requests[0][-1].content
    assert '"channel_name":"核心概念"' in request_payload
    assert '"channel_purpose":"发现可以独立检索的概念"' in request_payload
    assert suggestion.activation.startswith("正文定义概念")
    assert suggestion.include_criteria == ["存在稳定名称", "正文提供独立事实"]
    assert suggestion.coverage_audit_enabled is False
    assert suggestion.request_id
