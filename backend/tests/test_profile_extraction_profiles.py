from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.profile_extraction.errors import ProfileExtractionError
from app.profile_extraction.models import (
    CandidateDocumentAnchorDecision,
    CandidateDraft,
    CandidateSelectionReason,
    ChannelAttributeSuggestionRequest,
    ExtractionChannel,
    ExtractionProfileConfig,
    ProfileCreateRequest,
    ProfileSaveRequest,
    StrategySuggestionRequest,
)
from app.profile_extraction.profile_service import ProfileService
from app.profile_extraction.prompt_compiler import (
    CANDIDATE_AUDIT_PROMPT_VERSION,
    DOCUMENT_ANCHOR_PROMPT_VERSION,
    NAME_MEANING_AUDIT_PROMPT_VERSION,
    NAME_SCOPE_AUDIT_PROMPT_VERSION,
    PROMPT_VERSION,
    build_candidate_audit_prompt,
    build_candidate_document_anchor_prompt,
    build_candidate_name_meaning_audit_prompt,
    build_candidate_name_scope_audit_prompt,
    compile_profile_prompts,
)
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


def test_candidate_draft_flattens_and_dedupes_summaries() -> None:
    candidate = CandidateDraft(
        name="Alpha",
        aliases=[],
        summaries=["  Alpha 的定义。 ", "alpha 的定义。", "Alpha 的机制。"],
        selection_reason="正文提供了定义和机制。",
        source_block_ids=["b1"],
    )

    assert candidate.summaries == ["Alpha 的定义。", "Alpha 的机制。"]


def test_candidate_draft_rejects_empty_summary_set() -> None:
    with pytest.raises(ValidationError, match="at least one non-empty summary"):
        CandidateDraft(
            name="Alpha",
            aliases=[],
            summaries=["   "],
            selection_reason="正文提供了定义。",
            source_block_ids=["b1"],
        )


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


def test_extraction_channel_accepts_legacy_omission_field_name() -> None:
    channel = ExtractionChannel.model_validate(
        {
            "channel_id": "ec_0123456789abcdef",
            "name": "核心知识对象",
            "purpose": "发现独立知识对象。",
            "activation": "出现定义时。",
            "include_criteria": ["有稳定名称"],
            "exclude_criteria": ["导航"],
            "granularity_rule": "一个对象一个 candidate。",
            "coverage_audit_enabled": True,
        }
    )

    assert channel.omission_retry_enabled is True
    dumped = channel.model_dump(mode="json")
    assert dumped["omission_retry_enabled"] is True
    assert "coverage_audit_enabled" not in dumped


def test_candidate_selection_reason_normalizes_legacy_operation() -> None:
    reason = CandidateSelectionReason(
        channel_id="ec_0123456789abcdef",
        operation="coverage_audit",
        window_index=0,
        reason="补抽遗漏对象。",
        source_block_ids=["b1"],
    )

    assert reason.operation == "omission_retry"


def test_candidate_audit_prompt_checks_name_specificity_not_local_definition() -> None:
    prompt = build_candidate_audit_prompt(_valid_config())

    assert CANDIDATE_AUDIT_PROMPT_VERSION == "candidate-semantic-audit-v16"
    assert "name_mismatches_evidence" not in prompt
    assert "不得要求任意一条摘要独立给出完整定义" in prompt
    assert "summary_missing_object_definition" not in prompt


def test_discovery_prompt_requires_standalone_candidate_titles() -> None:
    prompt = compile_profile_prompts(_valid_config())[0].system_prompt
    compact_prompt = "".join(prompt.split())

    assert PROMPT_VERSION == "profile-candidate-v6"
    assert "候选列表中的独立定位标识" in compact_prompt
    assert "不能直接使用完整句、祈使句或建议句" in compact_prompt
    assert "使用evidence已支持的最小对象、系统或场景锚定" in compact_prompt
    assert "不得机械添加领域前缀" in compact_prompt
    assert "优先使用可独立理解的完整名称" in compact_prompt
    assert "原文中的原短语与name同指且实际出现，应保留为alias" in compact_prompt
    assert "existing_candidates是前面通道已经积累的候选账本" in compact_prompt
    assert "必须返回existing_candidate_ref" in compact_prompt
    assert "summaries只写当前evidence新增的信息贡献" in compact_prompt


def test_document_anchor_prompt_uses_full_document_without_rewriting_candidates() -> None:
    prompt = build_candidate_document_anchor_prompt(_valid_config())
    compact_prompt = "".join(prompt.split())

    assert DOCUMENT_ANCHOR_PROMPT_VERSION == "candidate-document-anchor-v3"
    assert "使用整篇文档evidence" in compact_prompt
    assert "不得修改候选summaries" in compact_prompt
    assert "不能增加、遗漏、合并、拆分或重复" in compact_prompt
    assert "中英文互译、同义改写、括号释义、缩写展开本身不构成消歧锚点" in compact_prompt
    assert "PatternN" in compact_prompt
    assert "anchor_source_block_ids" in prompt
    assert "standalone_clear" in prompt
    assert "missing_anchor_types" in prompt
    assert "final_name" in prompt
    assert "title_kind" in prompt


def test_document_anchor_rejects_unclear_title_that_is_not_renamed() -> None:
    with pytest.raises(ValidationError, match="unclear provisional title must be renamed"):
        CandidateDocumentAnchorDecision(
            candidate_ref="ca_0000000000000001",
            provisional_name="blast radius",
            title_kind="common_concept",
            standalone_clear=False,
            missing_anchor_types=["actor", "scope"],
            final_name="blast radius",
            aliases=["blast radius"],
            anchor_source_block_ids=["b1"],
            reason="缺少 Actor 和作用范围。",
        )


def test_name_scope_prompt_uses_only_profile_and_knowledge_base_names() -> None:
    prompt = build_candidate_name_scope_audit_prompt(_valid_config())

    assert NAME_SCOPE_AUDIT_PROMPT_VERSION == "candidate-name-scope-audit-v3"
    assert "当前知识库命名空间" in prompt
    assert "不要求全球或全互联网唯一" in prompt
    assert "输入不会提供、也不得推断 alias、summaries、文档标题、正文或 evidence" in prompt
    assert "只看主标题" in prompt
    assert "blast radius" in prompt
    assert "misaligned" not in prompt
    assert "suggested_name" in prompt


def test_name_meaning_prompt_compares_title_with_local_summary() -> None:
    prompt = build_candidate_name_meaning_audit_prompt(_valid_config())
    compact_prompt = "".join(prompt.split())

    assert NAME_MEANING_AUDIT_PROMPT_VERSION == "candidate-name-meaning-audit-v2"
    assert "主标题、别名和本地 summaries 集合" in prompt
    assert "不要求任何一条单独构成完整定义" in prompt
    assert "顾名而比较" in compact_prompt
    assert "aligned|misaligned|uncertain" in prompt


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
        '"granularity_rule":"一个对象一个 candidate","omission_retry_enabled":false}],'
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
        '"omission_retry_enabled":false}'
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
    assert suggestion.omission_retry_enabled is False
    assert suggestion.request_id
