from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from sqlalchemy import delete, select

from app.profile_extraction.errors import ProfileExtractionError
from app.profile_extraction.models import (
    CandidateAuditFinding,
    CandidateAuditStatus,
    CandidateDocumentAnchorDecision,
    CandidateDocumentAnchorPayload,
    CandidateDraft,
    CandidateExtractionResponse,
    CandidateNameMeaningAuditPayload,
    CandidateNameScopeAuditPayload,
    CandidateSelectionReason,
    CandidateSemanticAuditPayload,
    DocumentAnchorStatus,
    EvidenceTranslationItem,
    EvidenceTranslationPayload,
    EvidenceTranslationRequest,
    EvidenceTranslationResponse,
    ExistingCandidateContext,
    ExtractionProfile,
    ExtractionProfileConfig,
    PreviewCandidate,
    PreviewCandidatePage,
    PreviewChannelDefinition,
    PreviewDocumentAnchorResult,
    PreviewRunDetail,
    PreviewRunPlan,
    PreviewRunPlanRequest,
    PreviewRunRequest,
    PreviewRunSummary,
)
from app.profile_extraction.profile_service import ProfileService
from app.profile_extraction.prompt_compiler import (
    CANDIDATE_AUDIT_PROMPT_VERSION,
    COMPILER_VERSION,
    DOCUMENT_ANCHOR_PROMPT_VERSION,
    EVIDENCE_TRANSLATION_PROMPT_VERSION,
    NAME_MEANING_AUDIT_PROMPT_VERSION,
    NAME_SCOPE_AUDIT_PROMPT_VERSION,
    PROMPT_VERSION,
    CompiledChannelPrompt,
    aggregate_prompt_hash,
    build_candidate_audit_prompt,
    build_candidate_document_anchor_prompt,
    build_candidate_name_meaning_audit_prompt,
    build_candidate_name_scope_audit_prompt,
    build_evidence_message,
    build_evidence_translation_prompt,
    build_missing_audit_prompt,
    compile_profile_prompts,
)
from app.profile_extraction.util import (
    BLOCK_WINDOW_CHARS,
    BLOCK_WINDOW_POLICY_VERSION,
    BLOCK_WINDOW_SIZE,
    NAME_NORMALIZATION_VERSION,
    estimate_tokens,
    json_dumps,
    json_loads_object,
    json_loads_object_list,
    json_loads_string_list,
    new_identifier,
    normalize_name,
    ordered_unique,
    ordered_unique_normalized_text,
    sha256_text,
    split_blocks,
    stable_hash,
)
from app.providers.chat import (
    ChatMessage,
    ChatProvider,
    ChatProviderError,
    DeepSeekChatProvider,
)
from app.retrieval.errors import RetrievalFailure
from app.retrieval.models import RetrievalBlock
from app.retrieval.service import RetrievalReadService, RetrievalSnapshotReader
from app.storage.credentials import CHAT_API_KEY_USERNAME, CredentialStore
from app.storage.database import StorageDatabase
from app.storage.models import utc_iso, utc_now
from app.storage.profile_extraction_models import (
    DocumentChecklistRow,
    EvidenceTranslationRow,
    PreviewCandidateRow,
    ProfileComparisonRow,
    ProfilePreviewRunRow,
)

DOCUMENT_ANCHOR_POLICY_VERSION = "document-anchor-32k-64k-v3"
DOCUMENT_ANCHOR_SOFT_INPUT_TOKENS = 32_000
DOCUMENT_ANCHOR_HARD_INPUT_TOKENS = 64_000
DOCUMENT_ANCHOR_SAFETY_RESERVE_TOKENS = 8_192
DOCUMENT_ANCHOR_MAX_REQUEST_TOKENS = 144_000
DOCUMENT_ANCHOR_MIN_OUTPUT_TOKENS = 32_768
DOCUMENT_ANCHOR_MAX_OUTPUT_TOKENS = 65_536
DOCUMENT_ANCHOR_BASE_OUTPUT_TOKENS = 4_096
DOCUMENT_ANCHOR_TOKENS_PER_CANDIDATE = 384
DOCUMENT_ANCHOR_MAX_CANDIDATES = 150
ESTIMATED_ANCHOR_CANDIDATES_PER_DOCUMENT = 96
ESTIMATED_ANCHOR_CANDIDATE_CHARS = 240
ESTIMATED_CANDIDATES_PER_CHANNEL_WINDOW = 6
ESTIMATED_ACCUMULATED_CANDIDATE_CHARS = 450
CANDIDATE_PIPELINE_VERSION = "profile-preview-pipeline-v9"
MAX_PARALLEL_CALLS = 2
PROMPT_ENVELOPE_CHARS = 512
ESTIMATED_OUTPUT_TOKENS_PER_CALL = 512
ESTIMATED_AUDIT_OUTPUT_TOKENS_PER_CALL = 1_024
ESTIMATED_AUDIT_CANDIDATE_CHARS_PER_WINDOW = 2_000
CANDIDATE_OUTPUT_MAX_ATTEMPTS = 2
CANDIDATE_OUTPUT_MAX_TOKENS = 16_384
CANDIDATE_OUTPUT_RETRY_MAX_TOKENS = 24_576
TRANSLATION_BATCH_MAX_BLOCKS = 20
TRANSLATION_BATCH_MAX_CHARS = 12_000


@dataclass(frozen=True, slots=True)
class _PreparedPreview:
    profile: ExtractionProfile
    config: ExtractionProfileConfig
    prompts: list[CompiledChannelPrompt]
    prompt_hash: str
    reader: RetrievalSnapshotReader
    blocks_by_doc: dict[str, list[RetrievalBlock]]
    document_versions: dict[str, int]
    document_hashes: dict[str, str]
    provider_profile: dict[str, object]
    fingerprint: str


@dataclass(slots=True)
class _ChannelWindowResult:
    channel_id: str
    operation: Literal["discovery", "omission_retry"]
    window_index: int
    candidates: list[CandidateDraft]
    usage: dict[str, object]
    diagnostics: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class _RawCandidate:
    name: str
    aliases: list[str]
    summaries: list[str]
    source_block_ids: list[str]
    channel_id: str
    window_index: int


@dataclass(slots=True)
class _DocumentCandidate:
    candidate_ref: str
    name: str
    normalized_name: str
    aliases: list[str]
    summaries: list[str]
    selection_reasons: list[CandidateSelectionReason]
    source_block_ids: list[str]
    channel_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _WindowAuditCandidate:
    candidate_ref: str
    candidate_id: str
    normalized_name: str
    name: str
    aliases: list[str]
    summaries: list[str]
    selection_reason: str
    source_block_ids: list[str]
    channel_ids: list[str]


@dataclass(slots=True)
class _KnowledgeBaseAuditCandidate:
    candidate_ref: str
    normalized_name: str
    name: str
    aliases: list[str]
    summaries: list[str]


@dataclass(slots=True)
class _DocumentAnchorCandidate:
    candidate_ref: str
    candidate_id: str
    name: str
    aliases: list[str]
    summaries: list[str]
    source_block_ids: list[str]
    channel_ids: list[str]


class ProfilePreviewService:
    """串行调度并执行不可变的 Profile Preview Run。"""

    def __init__(
        self,
        data_root: Path,
        profile_service: ProfileService,
        credentials: CredentialStore,
        *,
        chat_provider_factory: Callable[[str], ChatProvider] | None = None,
        database: StorageDatabase | None = None,
    ) -> None:
        self._database = database or StorageDatabase(data_root)
        self._profiles = profile_service
        self._credentials = credentials
        self._retrieval = RetrievalReadService(data_root)
        self._chat_provider_factory = chat_provider_factory
        self._dispatch_event = asyncio.Event()
        self._dispatcher: asyncio.Task[None] | None = None
        self._started = False
        self._cancel_requested: set[str] = set()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """恢复单 dispatcher；把上次进程遗留的 queued／running run 标为中断。"""

        if self._started:
            return
        self._started = True
        with self._database.session() as session, session.begin():
            rows = session.scalars(
                select(ProfilePreviewRunRow).where(
                    ProfilePreviewRunRow.status.in_(("queued", "running"))
                )
            ).all()
            now = utc_now()
            for row in rows:
                row.status = "interrupted"
                if row.semantic_audit_status in {"pending", "running"}:
                    row.semantic_audit_status = "interrupted"
                row.error_code = "process_restarted"
                row.error_message = "Preview Run was interrupted by a process restart"
                row.completed_at = now
        self._dispatcher = asyncio.create_task(self._dispatch_loop())
        self._dispatch_event.set()

    async def stop(self) -> None:
        """停止后台 dispatcher；当前 run 在下一次启动时标记为 interrupted。"""

        self._started = False
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            await asyncio.gather(self._dispatcher, return_exceptions=True)
            self._dispatcher = None

    def plan(self, request: PreviewRunPlanRequest) -> PreviewRunPlan:
        """计算 Preview 范围、调用量、粗略 token 和可复用 run。"""

        prepared = self._prepare(request)
        window_blocks_by_doc = {
            doc_id: split_blocks(blocks) for doc_id, blocks in prepared.blocks_by_doc.items()
        }
        windows = sum(len(groups) for groups in window_blocks_by_doc.values())
        enabled_channels = prepared.config.extraction_channels
        initial_calls = windows * len(enabled_channels)
        omission_retry_calls = windows * sum(
            1 for channel in enabled_channels if channel.omission_retry_enabled
        )
        content_audit_calls = windows if request.semantic_audit_enabled else 0
        name_scope_audit_calls = 1 if request.semantic_audit_enabled else 0
        name_meaning_audit_calls = 1 if request.semantic_audit_enabled else 0
        semantic_audit_calls = (
            content_audit_calls + name_scope_audit_calls + name_meaning_audit_calls
        )
        extraction_calls = initial_calls + omission_retry_calls
        document_anchor_calls = len(prepared.blocks_by_doc)
        estimated_model_calls = extraction_calls + document_anchor_calls + semantic_audit_calls
        input_chars = sum(
            len(prompt.system_prompt)
            + sum(len(block.text) for block in window)
            + PROMPT_ENVELOPE_CHARS
            for prompt in prepared.prompts
            for groups in window_blocks_by_doc.values()
            for window in groups
        )
        input_chars += sum(
            len(prompt.system_prompt)
            + sum(len(block.text) for block in window)
            + PROMPT_ENVELOPE_CHARS
            for prompt in prepared.prompts
            if prompt.channel.omission_retry_enabled
            for groups in window_blocks_by_doc.values()
            for window in groups
        )
        estimated_accumulated_attachment_chars = 0
        for groups in window_blocks_by_doc.values():
            accumulated_candidates = 0
            for _prompt in prepared.prompts:
                estimated_accumulated_attachment_chars += (
                    len(groups) * accumulated_candidates * ESTIMATED_ACCUMULATED_CANDIDATE_CHARS
                )
                accumulated_candidates += len(groups) * ESTIMATED_CANDIDATES_PER_CHANNEL_WINDOW
        input_chars += estimated_accumulated_attachment_chars
        document_anchor_prompt = build_candidate_document_anchor_prompt(prepared.config)
        document_anchor_input_chars = sum(
            len(document_anchor_prompt)
            + sum(len(block.text) for block in blocks)
            + ESTIMATED_ANCHOR_CANDIDATES_PER_DOCUMENT * ESTIMATED_ANCHOR_CANDIDATE_CHARS
            + PROMPT_ENVELOPE_CHARS
            for blocks in prepared.blocks_by_doc.values()
        )
        input_chars += document_anchor_input_chars
        if request.semantic_audit_enabled:
            audit_prompt = build_candidate_audit_prompt(prepared.config)
            name_scope_prompt = build_candidate_name_scope_audit_prompt(prepared.config)
            name_meaning_prompt = build_candidate_name_meaning_audit_prompt(prepared.config)
            input_chars += sum(
                len(audit_prompt)
                + sum(len(block.text) for block in window)
                + ESTIMATED_AUDIT_CANDIDATE_CHARS_PER_WINDOW
                + PROMPT_ENVELOPE_CHARS
                for groups in window_blocks_by_doc.values()
                for window in groups
            )
            input_chars += (
                len(name_scope_prompt)
                + ESTIMATED_AUDIT_CANDIDATE_CHARS_PER_WINDOW
                + PROMPT_ENVELOPE_CHARS
            )
            input_chars += (
                len(name_meaning_prompt)
                + ESTIMATED_AUDIT_CANDIDATE_CHARS_PER_WINDOW
                + PROMPT_ENVELOPE_CHARS
            )
        estimated_input_tokens = estimate_tokens("x" * input_chars)
        estimated_document_anchor_input_tokens = estimate_tokens("x" * document_anchor_input_chars)
        estimated_document_anchor_output_tokens = document_anchor_calls * (
            self._document_anchor_output_budget(ESTIMATED_ANCHOR_CANDIDATES_PER_DOCUMENT)
        )
        estimated_output_tokens = (
            extraction_calls * ESTIMATED_OUTPUT_TOKENS_PER_CALL
            + estimated_document_anchor_output_tokens
            + content_audit_calls * ESTIMATED_AUDIT_OUTPUT_TOKENS_PER_CALL
            + name_scope_audit_calls * ESTIMATED_AUDIT_OUTPUT_TOKENS_PER_CALL
            + name_meaning_audit_calls * ESTIMATED_AUDIT_OUTPUT_TOKENS_PER_CALL
        )
        existing = self._find_reusable(prepared.fingerprint)
        return PreviewRunPlan(
            snapshot_id=prepared.reader.snapshot_id,
            profile_id=prepared.profile.profile_id,
            profile_hash=prepared.profile.profile_hash,
            document_ids=list(prepared.blocks_by_doc),
            block_count=sum(len(blocks) for blocks in prepared.blocks_by_doc.values()),
            window_count=windows,
            estimated_extraction_calls=extraction_calls,
            estimated_document_anchor_calls=document_anchor_calls,
            estimated_document_anchor_input_tokens=(estimated_document_anchor_input_tokens),
            estimated_document_anchor_output_tokens=(estimated_document_anchor_output_tokens),
            estimated_semantic_audit_calls=semantic_audit_calls,
            estimated_name_scope_audit_calls=name_scope_audit_calls,
            estimated_name_meaning_audit_calls=name_meaning_audit_calls,
            estimated_model_calls=estimated_model_calls,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
            estimated_total_tokens=estimated_input_tokens + estimated_output_tokens,
            document_anchor_soft_input_tokens=DOCUMENT_ANCHOR_SOFT_INPUT_TOKENS,
            document_anchor_hard_input_tokens=DOCUMENT_ANCHOR_HARD_INPUT_TOKENS,
            document_anchor_max_candidates=DOCUMENT_ANCHOR_MAX_CANDIDATES,
            reusable_run_id=(
                existing.run_id
                if existing is not None and request.execution_mode == "reuse_if_exact"
                else None
            ),
            provider_ready=bool(self._credentials.get(CHAT_API_KEY_USERNAME)),
        )

    async def create(self, request: PreviewRunRequest) -> PreviewRunDetail:
        """创建或显式复用一次 Preview Run，并立即返回当前状态。"""

        await self.start()
        prepared = self._prepare(request)
        if request.execution_mode == "reuse_if_exact":
            existing = self._find_reusable(prepared.fingerprint)
            if existing is not None:
                return self.get_run(existing.run_id, reused=True)
        now = utc_now()
        row = ProfilePreviewRunRow(
            run_id=new_identifier("pr"),
            snapshot_id=prepared.reader.snapshot_id,
            snapshot_manifest_hash=prepared.reader.manifest.manifest_hash,
            profile_id=prepared.profile.profile_id,
            profile_hash=prepared.profile.profile_hash,
            profile_config_snapshot_json=json_dumps(prepared.config.model_dump(mode="json")),
            document_scope_json=json_dumps(list(prepared.blocks_by_doc)),
            document_versions_json=json_dumps(prepared.document_versions),
            document_hashes_json=json_dumps(prepared.document_hashes),
            candidate_pipeline_version=CANDIDATE_PIPELINE_VERSION,
            name_normalization_version=NAME_NORMALIZATION_VERSION,
            compiler_version=COMPILER_VERSION,
            prompt_version=PROMPT_VERSION,
            prompt_hash=prepared.prompt_hash,
            provider_profile_json=json_dumps(prepared.provider_profile),
            window_policy_version=BLOCK_WINDOW_POLICY_VERSION,
            window_policy_json=json_dumps(
                {"max_blocks": BLOCK_WINDOW_SIZE, "max_chars": BLOCK_WINDOW_CHARS}
            ),
            execution_policy_json=json_dumps({"max_parallel_calls": MAX_PARALLEL_CALLS}),
            preview_fingerprint=prepared.fingerprint,
            status="queued",
            semantic_audit_enabled=request.semantic_audit_enabled,
            semantic_audit_prompt_version=(
                CANDIDATE_AUDIT_PROMPT_VERSION if request.semantic_audit_enabled else None
            ),
            name_scope_audit_prompt_version=(
                NAME_SCOPE_AUDIT_PROMPT_VERSION if request.semantic_audit_enabled else None
            ),
            name_meaning_audit_prompt_version=(
                NAME_MEANING_AUDIT_PROMPT_VERSION if request.semantic_audit_enabled else None
            ),
            document_anchor_prompt_version=DOCUMENT_ANCHOR_PROMPT_VERSION,
            document_anchor_policy_version=DOCUMENT_ANCHOR_POLICY_VERSION,
            document_anchor_json="{}",
            semantic_audit_status="pending" if request.semantic_audit_enabled else "disabled",
            semantic_audit_error=None,
            semantic_audit_json="[]",
            usage_json="[]",
            diagnostics_json="[]",
            created_at=now,
        )
        with self._database.session() as session, session.begin():
            session.add(row)
        self._dispatch_event.set()
        return self.get_run(row.run_id)

    def list_runs(self) -> list[PreviewRunSummary]:
        """按创建时间倒序返回 Preview Run 历史。"""

        with self._database.session() as session:
            rows = session.scalars(
                select(ProfilePreviewRunRow).order_by(ProfilePreviewRunRow.created_at.desc())
            ).all()
            return [self._summary(row) for row in rows]

    def get_run(self, run_id: str, *, reused: bool = False) -> PreviewRunDetail:
        """读取一个 run；completed run 返回正式候选的第一页。"""

        with self._database.session() as session:
            row = session.get(ProfilePreviewRunRow, run_id)
            if row is None:
                raise ProfileExtractionError(
                    "preview_run_not_found", "Preview Run does not exist", 404
                )
            candidates = self._candidate_rows(session, run_id) if row.status == "completed" else []
            candidate_models = self._candidate_models(
                candidates,
                semantic_audit_json=row.semantic_audit_json,
                semantic_audit_enabled=row.semantic_audit_enabled,
                semantic_audit_status=row.semantic_audit_status,
            )
            return PreviewRunDetail(
                **self._summary(row).model_dump(),
                candidates=candidate_models,
                candidates_total=len(candidates),
                reused=reused,
            )

    def list_candidates(
        self,
        run_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> PreviewCandidatePage:
        """分页读取 completed run 的正式候选。"""

        with self._database.session() as session:
            row = session.get(ProfilePreviewRunRow, run_id)
            if row is None:
                raise ProfileExtractionError(
                    "preview_run_not_found", "Preview Run does not exist", 404
                )
            if row.status != "completed":
                raise ProfileExtractionError(
                    "preview_run_not_completed",
                    "Only completed Preview Runs expose formal candidates",
                    409,
                )
            all_rows = self._candidate_rows(session, run_id)
            page = all_rows[offset : offset + limit]
            models_by_id = {
                candidate.candidate_id: candidate
                for candidate in self._candidate_models(
                    all_rows,
                    semantic_audit_json=row.semantic_audit_json,
                    semantic_audit_enabled=row.semantic_audit_enabled,
                    semantic_audit_status=row.semantic_audit_status,
                )
            }
            return PreviewCandidatePage(
                candidates=[
                    models_by_id[candidate.candidate_id]
                    for candidate in page
                    if candidate.candidate_id in models_by_id
                ],
                total=len(all_rows),
                offset=offset,
                limit=limit,
            )

    async def translate_evidence(
        self,
        run_id: str,
        request: EvidenceTranslationRequest,
    ) -> EvidenceTranslationResponse:
        """翻译试抽取中的证据块，并按原文内容复用历史翻译。"""

        provider = self._provider()
        model_id = provider.profile.model_id
        requested_ids = ordered_unique(request.block_ids)
        with self._database.session() as session:
            run = session.get(ProfilePreviewRunRow, run_id)
            if run is None:
                raise ProfileExtractionError(
                    "preview_run_not_found",
                    "Preview Run does not exist",
                    404,
                )
            if run.status != "completed":
                raise ProfileExtractionError(
                    "preview_run_not_completed",
                    "Only completed Preview Runs expose evidence translation",
                    409,
                )
            snapshot_id = run.snapshot_id
            run_document_ids = json_loads_string_list(run.document_scope_json)
            evidence_by_id: dict[str, str] = {}
            for candidate in self._candidate_rows(session, run_id):
                for evidence in json_loads_object_list(candidate.evidence_blocks_json):
                    block_id = str(evidence.get("block_id", ""))
                    text = str(evidence.get("text", ""))
                    if block_id and text:
                        evidence_by_id[block_id] = text
            missing_ids = [block_id for block_id in requested_ids if block_id not in evidence_by_id]
            if missing_ids:
                reader = self._retrieval.bind_snapshot(snapshot_id)
                for document_id in run_document_ids:
                    try:
                        source_blocks = list(reader.list_blocks(document_id).blocks)
                    except RetrievalFailure as error:
                        raise ProfileExtractionError(
                            error.code,
                            error.message,
                            409,
                        ) from error
                    for block in source_blocks:
                        evidence_by_id.setdefault(block.block_id, block.text)
                missing_ids = [
                    block_id for block_id in requested_ids if block_id not in evidence_by_id
                ]
            if missing_ids:
                raise ProfileExtractionError(
                    "translation_block_not_found",
                    f"Evidence block is not part of this Preview Run: {missing_ids[0]}",
                    422,
                )
            translation_keys = {
                block_id: self._translation_key(model_id, evidence_by_id[block_id])
                for block_id in requested_ids
            }
            cached_rows = session.scalars(
                select(EvidenceTranslationRow).where(
                    EvidenceTranslationRow.translation_key.in_(translation_keys.values())
                )
            ).all()

        cached_by_key = {row.translation_key: row.translated_text for row in cached_rows}
        pending = [
            (block_id, evidence_by_id[block_id], translation_keys[block_id])
            for block_id in requested_ids
            if translation_keys[block_id] not in cached_by_key
        ]
        request_ids: list[str] = []
        new_translations: dict[str, tuple[str, str]] = {}
        if pending:
            key = self._credentials.get(CHAT_API_KEY_USERNAME)
            if not key:
                raise ProfileExtractionError(
                    "chat_api_key_missing",
                    "configure a chat API key before translating evidence",
                    409,
                )
            for batch in self._translation_batches(pending):
                try:
                    result = await provider.complete(
                        [
                            ChatMessage(
                                role="system",
                                content=build_evidence_translation_prompt(),
                            ),
                            ChatMessage(
                                role="user",
                                content=json_dumps(
                                    {
                                        "evidence_blocks": [
                                            {"block_id": block_id, "text": text}
                                            for block_id, text, _key in batch
                                        ]
                                    }
                                ),
                            ),
                        ],
                        max_output_tokens=4_096,
                    )
                except ChatProviderError as error:
                    raise ProfileExtractionError(
                        error.code,
                        error.message,
                        503 if error.retryable else 502,
                    ) from error
                try:
                    payload = EvidenceTranslationPayload.model_validate_json(result.content)
                except Exception as error:
                    raise ProfileExtractionError(
                        "evidence_translation_invalid",
                        "chat provider returned an invalid evidence translation",
                        502,
                    ) from error
                translated_by_id = {
                    item.block_id: item.translated_text for item in payload.translations
                }
                expected_ids = {block_id for block_id, _text, _key in batch}
                if set(translated_by_id) != expected_ids or len(translated_by_id) != len(batch):
                    raise ProfileExtractionError(
                        "evidence_translation_invalid",
                        "chat provider did not return exactly the requested evidence blocks",
                        502,
                    )
                for block_id, _text, translation_key in batch:
                    cached_by_key[translation_key] = translated_by_id[block_id]
                    new_translations[translation_key] = (
                        _text,
                        translated_by_id[block_id],
                    )
                request_ids.append(result.request_id)
            with self._database.session() as session, session.begin():
                for translation_key, (text, translated_text) in new_translations.items():
                    if session.get(EvidenceTranslationRow, translation_key) is not None:
                        continue
                    session.add(
                        EvidenceTranslationRow(
                            translation_key=translation_key,
                            source_text_hash=sha256_text(text),
                            provider_model_id=model_id,
                            prompt_version=EVIDENCE_TRANSLATION_PROMPT_VERSION,
                            translated_text=translated_text,
                        )
                    )

        items = [
            EvidenceTranslationItem(
                block_id=block_id,
                translated_text=cached_by_key[translation_keys[block_id]],
                reused=translation_keys[block_id] in {row.translation_key for row in cached_rows},
            )
            for block_id in requested_ids
        ]
        return EvidenceTranslationResponse(
            items=items,
            translated_count=len(items),
            reused_count=sum(1 for item in items if item.reused),
            request_ids=request_ids,
        )

    def cancel(self, run_id: str) -> PreviewRunSummary:
        """取消 queued run 或在 running run 的下一个 checkpoint 生效。"""

        with self._database.session() as session, session.begin():
            row = session.get(ProfilePreviewRunRow, run_id)
            if row is None:
                raise ProfileExtractionError(
                    "preview_run_not_found", "Preview Run does not exist", 404
                )
            if row.status == "queued":
                row.status = "cancelled"
                row.completed_at = utc_now()
            elif row.status == "running":
                self._cancel_requested.add(run_id)
            else:
                raise ProfileExtractionError(
                    "preview_run_not_cancellable",
                    "Preview Run is already terminal",
                    409,
                )
            session.flush()
            return self._summary(row)

    def delete(self, run_id: str) -> None:
        """删除未被清单或比较引用的终态 Preview Run。"""

        with self._database.session() as session, session.begin():
            row = session.get(ProfilePreviewRunRow, run_id)
            if row is None:
                raise ProfileExtractionError(
                    "preview_run_not_found", "Preview Run does not exist", 404
                )
            if row.status in {"queued", "running"}:
                raise ProfileExtractionError(
                    "preview_run_delete_forbidden",
                    "Cancel an active Preview Run before deleting it",
                    409,
                )
            comparison = session.scalar(
                select(ProfileComparisonRow.comparison_id)
                .where(
                    (ProfileComparisonRow.run_a_id == run_id)
                    | (ProfileComparisonRow.run_b_id == run_id)
                )
                .limit(1)
            )
            if comparison is not None:
                raise ProfileExtractionError(
                    "preview_run_referenced",
                    "Preview Run is referenced by a Profile comparison",
                    409,
                )
            checklists = session.scalars(select(DocumentChecklistRow)).all()
            if any(
                item.get("origin_preview_run_id") == run_id
                for checklist in checklists
                for item in json_loads_object_list(checklist.items_json)
            ):
                raise ProfileExtractionError(
                    "preview_run_referenced",
                    "Preview Run is referenced by a document checklist",
                    409,
                )
            session.execute(delete(PreviewCandidateRow).where(PreviewCandidateRow.run_id == run_id))
            session.delete(row)

    async def wait_for_terminal(
        self, run_id: str, timeout_seconds: float = 30.0
    ) -> PreviewRunDetail:
        """等待 run 进入终态；用于测试和受控调用方。"""

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            run = self.get_run(run_id)
            if run.status in {"completed", "failed", "cancelled", "interrupted"}:
                return run
            await asyncio.sleep(0.02)
        raise TimeoutError(f"Preview Run did not become terminal: {run_id}")

    async def _dispatch_loop(self) -> None:
        while self._started:
            await self._dispatch_event.wait()
            self._dispatch_event.clear()
            async with self._lock:
                row = self._next_queued()
                if row is None:
                    continue
                self._mark_running(row.run_id)
                try:
                    await self._execute_run(row.run_id)
                except asyncio.CancelledError:
                    raise
                except ProfileExtractionError as error:
                    self._mark_failed(row.run_id, error.code, error.message)
                except Exception as error:  # pragma: no cover - defensive boundary
                    self._mark_failed(
                        row.run_id,
                        "preview_run_failed",
                        f"Preview Run failed: {type(error).__name__}",
                    )
                finally:
                    self._cancel_requested.discard(row.run_id)
                self._dispatch_event.set()

    def _prepare(
        self,
        request: PreviewRunRequest | PreviewRunPlanRequest,
    ) -> _PreparedPreview:
        validation = self._profiles.validate_profile(request.profile_id)
        if not validation.valid:
            raise ProfileExtractionError(
                "profile_incomplete",
                validation.issues[0].message,
                422,
            )
        profile = self._profiles.config_snapshot(request.profile_id)
        config = profile.config
        prompts = compile_profile_prompts(config)
        prompt_hash = aggregate_prompt_hash(prompts)
        try:
            reader = (
                self._retrieval.bind_snapshot(request.snapshot_id)
                if request.snapshot_id is not None
                else self._retrieval.bind_current()
            )
        except RetrievalFailure as error:
            raise ProfileExtractionError(
                error.code,
                error.message,
                409,
            ) from error
        document_ids = ordered_unique(request.document_ids)
        if len(document_ids) != len(request.document_ids):
            raise ProfileExtractionError(
                "preview_document_duplicate",
                "Preview document IDs must be unique",
                422,
            )
        manifest_by_id = {document.doc_id: document for document in reader.manifest.documents}
        blocks_by_doc: dict[str, list[RetrievalBlock]] = {}
        document_versions: dict[str, int] = {}
        document_hashes: dict[str, str] = {}
        for doc_id in document_ids:
            summary = manifest_by_id.get(doc_id)
            if summary is None:
                raise ProfileExtractionError(
                    "preview_document_not_found",
                    f"Document is not in the selected snapshot: {doc_id}",
                    404,
                )
            try:
                blocks = list(reader.list_blocks(doc_id).blocks)
            except RetrievalFailure as error:
                raise ProfileExtractionError(
                    error.code,
                    error.message,
                    409,
                ) from error
            if not blocks:
                raise ProfileExtractionError(
                    "preview_document_empty",
                    f"Document contains no extractable blocks: {doc_id}",
                    422,
                )
            blocks_by_doc[doc_id] = blocks
            document_versions[doc_id] = summary.doc_version
            document_hashes[doc_id] = stable_hash(
                [(block.block_id, sha256_text(block.text)) for block in blocks]
            )
        provider = self._provider()
        provider_profile = dataclasses.asdict(provider.profile)
        fingerprint = stable_hash(
            {
                "profile_id": profile.profile_id,
                "profile_hash": profile.profile_hash,
                "snapshot_id": reader.snapshot_id,
                "snapshot_manifest_hash": reader.manifest.manifest_hash,
                "document_versions": document_versions,
                "document_hashes": document_hashes,
                "candidate_pipeline_version": CANDIDATE_PIPELINE_VERSION,
                "name_normalization_version": NAME_NORMALIZATION_VERSION,
                "compiler_version": COMPILER_VERSION,
                "prompt_version": PROMPT_VERSION,
                "prompt_hash": prompt_hash,
                "document_anchor_prompt_version": DOCUMENT_ANCHOR_PROMPT_VERSION,
                "document_anchor_policy_version": DOCUMENT_ANCHOR_POLICY_VERSION,
                "semantic_audit_enabled": request.semantic_audit_enabled,
                "candidate_audit_prompt_version": (
                    CANDIDATE_AUDIT_PROMPT_VERSION if request.semantic_audit_enabled else None
                ),
                "name_scope_audit_prompt_version": (
                    NAME_SCOPE_AUDIT_PROMPT_VERSION if request.semantic_audit_enabled else None
                ),
                "name_meaning_audit_prompt_version": (
                    NAME_MEANING_AUDIT_PROMPT_VERSION if request.semantic_audit_enabled else None
                ),
                "provider_profile": provider_profile,
                "window_policy": {
                    "max_blocks": BLOCK_WINDOW_SIZE,
                    "max_chars": BLOCK_WINDOW_CHARS,
                },
            }
        )
        return _PreparedPreview(
            profile=profile,
            config=config,
            prompts=prompts,
            prompt_hash=prompt_hash,
            reader=reader,
            blocks_by_doc=blocks_by_doc,
            document_versions=document_versions,
            document_hashes=document_hashes,
            provider_profile=provider_profile,
            fingerprint=fingerprint,
        )

    async def _execute_run(self, run_id: str) -> None:
        with self._database.session() as session:
            row = session.get(ProfilePreviewRunRow, run_id)
            if row is None:
                raise ProfileExtractionError(
                    "preview_run_not_found", "Preview Run does not exist", 404
                )
            config = ExtractionProfileConfig.model_validate_json(row.profile_config_snapshot_json)
            reader = self._retrieval.bind_snapshot(row.snapshot_id)
            document_ids = json_loads_string_list(row.document_scope_json)
            semantic_audit_enabled = row.semantic_audit_enabled
        prompts = compile_profile_prompts(config)
        provider = self._provider()
        if aggregate_prompt_hash(prompts) != row.prompt_hash:
            raise ProfileExtractionError(
                "preview_run_configuration_changed",
                "Prompt compiler changed after this Preview Run was queued",
                409,
            )
        if json_dumps(dataclasses.asdict(provider.profile)) != row.provider_profile_json:
            raise ProfileExtractionError(
                "preview_run_configuration_changed",
                "Provider configuration changed after this Preview Run was queued",
                409,
            )
        semaphore = asyncio.Semaphore(MAX_PARALLEL_CALLS)
        usage: list[dict[str, object]] = []
        diagnostics: list[dict[str, object]] = []
        semantic_audit_results: list[dict[str, object]] = []
        semantic_audit_successes = 0
        semantic_audit_failures = 0
        semantic_audit_errors: list[str] = []
        document_anchor_results: dict[str, PreviewDocumentAnchorResult] = {}
        candidates_by_doc: dict[str, list[PreviewCandidateRow]] = {}
        try:
            for doc_id in document_ids:
                self._raise_if_cancelled(run_id)
                blocks = list(reader.list_blocks(doc_id).blocks)
                window_groups = split_blocks(blocks)
                candidates_by_ref: dict[str, _DocumentCandidate] = {}
                refs_by_normalized: dict[str, str] = {}
                for prompt in prompts:
                    for window_index, window in enumerate(window_groups):
                        existing_attachments = self._accumulated_candidate_contexts(
                            candidates_by_ref
                        )
                        result = await self._run_channel(
                            provider=provider,
                            channel_prompt=prompt,
                            doc_id=doc_id,
                            doc_title=blocks[0].doc_title,
                            blocks=window,
                            window_index=window_index,
                            existing_candidates=existing_attachments,
                            operation="discovery",
                            semaphore=semaphore,
                        )
                        usage.append(result.usage)
                        diagnostics.extend(result.diagnostics)
                        self._accumulate_channel_result(
                            doc_id=doc_id,
                            candidates_by_ref=candidates_by_ref,
                            refs_by_normalized=refs_by_normalized,
                            result=result,
                        )
                    if prompt.channel.omission_retry_enabled:
                        for window_index, window in enumerate(window_groups):
                            existing_attachments = self._accumulated_candidate_contexts(
                                candidates_by_ref
                            )
                            result = await self._run_channel(
                                provider=provider,
                                channel_prompt=prompt,
                                doc_id=doc_id,
                                doc_title=blocks[0].doc_title,
                                blocks=window,
                                window_index=window_index,
                                existing_candidates=existing_attachments,
                                operation="omission_retry",
                                semaphore=semaphore,
                                audit_profile=config,
                            )
                            usage.append(result.usage)
                            diagnostics.extend(result.diagnostics)
                            self._accumulate_channel_result(
                                doc_id=doc_id,
                                candidates_by_ref=candidates_by_ref,
                                refs_by_normalized=refs_by_normalized,
                                result=result,
                            )
                merged = self._document_candidate_rows(
                    doc_id=doc_id,
                    doc_version=self._document_version(reader, doc_id),
                    run_id=run_id,
                    candidates=list(candidates_by_ref.values()),
                    evidence_by_id={block.block_id: block for block in blocks},
                )
                (
                    anchor_usage,
                    anchor_result,
                    anchor_diagnostics,
                ) = await self._anchor_document_candidates(
                    provider=provider,
                    profile=config,
                    doc_id=doc_id,
                    doc_title=blocks[0].doc_title,
                    blocks=blocks,
                    candidates=merged,
                    semaphore=semaphore,
                )
                document_anchor_results[doc_id] = anchor_result
                if anchor_usage is not None:
                    usage.append(anchor_usage)
                diagnostics.extend(anchor_diagnostics)
                if semantic_audit_enabled:
                    audit_jobs = []
                    for window_index, window in enumerate(window_groups):
                        audit_candidates = self._window_audit_candidates_from_rows(
                            doc_id=doc_id,
                            window_index=window_index,
                            window_blocks=window,
                            candidates=merged,
                        )
                        if audit_candidates:
                            audit_jobs.append(
                                self._run_semantic_audit(
                                    provider=provider,
                                    profile=config,
                                    doc_id=doc_id,
                                    doc_title=blocks[0].doc_title,
                                    blocks=window,
                                    window_index=window_index,
                                    candidates=audit_candidates,
                                    semaphore=semaphore,
                                )
                            )
                    if audit_jobs:
                        with self._database.session() as session, session.begin():
                            audit_row = session.get(ProfilePreviewRunRow, run_id)
                            if audit_row is not None:
                                audit_row.semantic_audit_status = "running"
                    audit_outcomes = await asyncio.gather(
                        *audit_jobs,
                        return_exceptions=True,
                    )
                    for outcome in audit_outcomes:
                        if isinstance(outcome, asyncio.CancelledError):
                            raise outcome
                        if isinstance(outcome, Exception):
                            semantic_audit_failures += 1
                            message = (
                                outcome.message
                                if isinstance(outcome, ProfileExtractionError)
                                else (
                                    f"Candidate audit failed: {type(outcome).__name__}: {outcome}"
                                )
                            )
                            semantic_audit_errors.append(message)
                            diagnostics.append(
                                {
                                    "code": "candidate_semantic_audit_failed",
                                    "message": message,
                                }
                            )
                            continue
                        audit_usage, audit_decisions = cast(
                            tuple[dict[str, object], list[dict[str, object]]],
                            outcome,
                        )
                        semantic_audit_successes += 1
                        usage.append(audit_usage)
                        semantic_audit_results.extend(audit_decisions)
                candidates_by_doc[doc_id] = merged
                self._raise_if_cancelled(run_id)
            if semantic_audit_enabled:
                knowledge_base_candidates = self._knowledge_base_audit_candidates(candidates_by_doc)
                if knowledge_base_candidates:
                    with self._database.session() as session, session.begin():
                        audit_row = session.get(ProfilePreviewRunRow, run_id)
                        if audit_row is not None:
                            audit_row.semantic_audit_status = "running"
                    try:
                        name_scope_usage, name_scope_decisions = await self._run_name_scope_audit(
                            provider=provider,
                            profile=config,
                            candidates=knowledge_base_candidates,
                            semaphore=semaphore,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        semantic_audit_failures += 1
                        message = (
                            error.message
                            if isinstance(error, ProfileExtractionError)
                            else (f"Name scope audit failed: {type(error).__name__}: {error}")
                        )
                        semantic_audit_errors.append(message)
                        diagnostics.append(
                            {
                                "code": "candidate_name_scope_audit_failed",
                                "message": message,
                            }
                        )
                    else:
                        semantic_audit_successes += 1
                        usage.append(name_scope_usage)
                        semantic_audit_results.extend(name_scope_decisions)
                    try:
                        (
                            name_meaning_usage,
                            name_meaning_decisions,
                        ) = await self._run_name_meaning_audit(
                            provider=provider,
                            profile=config,
                            candidates=knowledge_base_candidates,
                            semaphore=semaphore,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        semantic_audit_failures += 1
                        message = (
                            error.message
                            if isinstance(error, ProfileExtractionError)
                            else (f"Name meaning audit failed: {type(error).__name__}: {error}")
                        )
                        semantic_audit_errors.append(message)
                        diagnostics.append(
                            {
                                "code": "candidate_name_meaning_audit_failed",
                                "message": message,
                            }
                        )
                    else:
                        semantic_audit_successes += 1
                        usage.append(name_meaning_usage)
                        semantic_audit_results.extend(name_meaning_decisions)
            now = utc_now()
            with self._database.session() as session, session.begin():
                row = session.get(ProfilePreviewRunRow, run_id)
                if row is None:
                    raise ProfileExtractionError(
                        "preview_run_not_found",
                        "Preview Run disappeared during execution",
                        404,
                    )
                for candidate in (
                    candidate
                    for candidates in candidates_by_doc.values()
                    for candidate in candidates
                ):
                    session.add(candidate)
                row.status = "completed"
                if semantic_audit_enabled:
                    row.semantic_audit_status = (
                        "completed"
                        if semantic_audit_failures == 0
                        else "partial"
                        if semantic_audit_successes
                        else "failed"
                    )
                    row.semantic_audit_error = (
                        "；".join(ordered_unique(semantic_audit_errors))
                        if semantic_audit_errors
                        else None
                    )
                row.semantic_audit_json = json_dumps(semantic_audit_results)
                row.document_anchor_json = json_dumps(
                    {
                        document_id: result.model_dump(mode="json")
                        for document_id, result in document_anchor_results.items()
                    }
                )
                row.usage_json = json_dumps(usage)
                row.diagnostics_json = json_dumps(diagnostics)
                row.completed_at = now
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if run_id in self._cancel_requested:
                self._mark_terminal(
                    run_id,
                    "cancelled",
                    error_code=None,
                    error_message=None,
                    usage=usage,
                    diagnostics=diagnostics,
                )
                return
            code = error.code if isinstance(error, ProfileExtractionError) else "preview_run_failed"
            message = (
                error.message
                if isinstance(error, ProfileExtractionError)
                else f"Preview Run failed: {type(error).__name__}: {error}"
            )
            if isinstance(error, ProfileExtractionError) and error.diagnostics:
                diagnostics.append(
                    {
                        "code": code,
                        "message": message,
                        **error.diagnostics,
                    }
                )
            self._mark_terminal(
                run_id,
                "failed",
                error_code=code,
                error_message=message,
                usage=usage,
                diagnostics=diagnostics,
            )
            return

    @staticmethod
    def _document_anchor_output_budget(candidate_count: int) -> int:
        """按候选数估算文档级标题消歧的 JSON 输出预算。"""

        return min(
            DOCUMENT_ANCHOR_MAX_OUTPUT_TOKENS,
            max(
                DOCUMENT_ANCHOR_MIN_OUTPUT_TOKENS,
                DOCUMENT_ANCHOR_BASE_OUTPUT_TOKENS
                + candidate_count * DOCUMENT_ANCHOR_TOKENS_PER_CANDIDATE,
            ),
        )

    async def _anchor_document_candidates(
        self,
        *,
        provider: ChatProvider,
        profile: ExtractionProfileConfig,
        doc_id: str,
        doc_title: str,
        blocks: list[RetrievalBlock],
        candidates: list[PreviewCandidateRow],
        semaphore: asyncio.Semaphore,
    ) -> tuple[
        dict[str, object] | None,
        PreviewDocumentAnchorResult,
        list[dict[str, object]],
    ]:
        """用整篇文档 evidence 锚定名称；失败时保留 provisional candidates。"""

        candidate_count = len(candidates)
        output_budget = self._document_anchor_output_budget(candidate_count)
        if candidate_count == 0:
            return (
                None,
                PreviewDocumentAnchorResult(
                    status="not_needed",
                    candidate_count=0,
                    estimated_input_tokens=0,
                    output_token_budget=0,
                    soft_input_tokens=DOCUMENT_ANCHOR_SOFT_INPUT_TOKENS,
                    hard_input_tokens=DOCUMENT_ANCHOR_HARD_INPUT_TOKENS,
                ),
                [],
            )
        if candidate_count > DOCUMENT_ANCHOR_MAX_CANDIDATES:
            message = (
                f"文档级标题消歧需要处理 {candidate_count} 个候选，"
                f"超过上限 {DOCUMENT_ANCHOR_MAX_CANDIDATES}。"
            )
            return (
                None,
                PreviewDocumentAnchorResult(
                    status="candidate_limit_exceeded",
                    candidate_count=candidate_count,
                    estimated_input_tokens=0,
                    output_token_budget=output_budget,
                    soft_input_tokens=DOCUMENT_ANCHOR_SOFT_INPUT_TOKENS,
                    hard_input_tokens=DOCUMENT_ANCHOR_HARD_INPUT_TOKENS,
                    error=message,
                ),
                [{"code": "document_anchor_candidate_limit_exceeded", "message": message}],
            )

        anchor_candidates = [
            _DocumentAnchorCandidate(
                candidate_ref="ca_" + sha256_text(f"{doc_id}\0{candidate.candidate_id}")[:16],
                candidate_id=candidate.candidate_id,
                name=candidate.name,
                aliases=json_loads_string_list(candidate.aliases_json),
                summaries=json_loads_string_list(candidate.summaries_json),
                source_block_ids=json_loads_string_list(candidate.source_block_ids_json),
                channel_ids=json_loads_string_list(candidate.channel_ids_json),
            )
            for candidate in candidates
        ]
        system_prompt = build_candidate_document_anchor_prompt(profile)
        user_payload = json_dumps(
            {
                "document": {
                    "title": doc_title,
                    "evidence_blocks": [
                        {
                            "block_id": block.block_id,
                            "section_path": block.section_path,
                            "text": block.text,
                        }
                        for block in blocks
                    ],
                },
                "candidates": [
                    {
                        "candidate_ref": candidate.candidate_ref,
                        "provisional_name": candidate.name,
                        "aliases": candidate.aliases,
                        "summaries": candidate.summaries,
                        "source_block_ids": candidate.source_block_ids,
                        "channel_ids": candidate.channel_ids,
                    }
                    for candidate in anchor_candidates
                ],
            }
        )
        estimated_input_tokens = estimate_tokens(system_prompt) + estimate_tokens(user_payload)
        estimated_request_tokens = (
            estimated_input_tokens + output_budget + DOCUMENT_ANCHOR_SAFETY_RESERVE_TOKENS
        )
        if (
            estimated_input_tokens > DOCUMENT_ANCHOR_HARD_INPUT_TOKENS
            or estimated_request_tokens > DOCUMENT_ANCHOR_MAX_REQUEST_TOKENS
        ):
            message = (
                f"文档级标题消歧估算输入 {estimated_input_tokens} tokens，"
                f"超过硬上限 {DOCUMENT_ANCHOR_HARD_INPUT_TOKENS}。"
            )
            return (
                None,
                PreviewDocumentAnchorResult(
                    status="over_budget",
                    candidate_count=candidate_count,
                    estimated_input_tokens=estimated_input_tokens,
                    output_token_budget=output_budget,
                    soft_input_tokens=DOCUMENT_ANCHOR_SOFT_INPUT_TOKENS,
                    hard_input_tokens=DOCUMENT_ANCHOR_HARD_INPUT_TOKENS,
                    error=message,
                ),
                [{"code": "document_anchor_over_budget", "message": message}],
            )

        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_payload),
        ]
        async with semaphore:
            try:
                result = await provider.complete(messages, max_output_tokens=output_budget)
            except ChatProviderError as error:
                message = error.message
                return (
                    None,
                    PreviewDocumentAnchorResult(
                        status="failed",
                        candidate_count=candidate_count,
                        estimated_input_tokens=estimated_input_tokens,
                        output_token_budget=output_budget,
                        soft_input_tokens=DOCUMENT_ANCHOR_SOFT_INPUT_TOKENS,
                        hard_input_tokens=DOCUMENT_ANCHOR_HARD_INPUT_TOKENS,
                        error=message,
                    ),
                    [{"code": "document_anchor_failed", "message": message}],
                )
            call_usage: dict[str, object] = {
                "doc_id": doc_id,
                "channel_id": "candidate_document_anchor",
                "channel_name": "文档级标题消歧",
                "operation": "document_anchor",
                "request_id": result.request_id,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "finish_reason": result.finish_reason,
                "returned_chars": len(result.content),
            }

            def failed_anchor_result(
                status: DocumentAnchorStatus,
                message: str,
                diagnostic_code: str,
            ) -> tuple[
                dict[str, object],
                PreviewDocumentAnchorResult,
                list[dict[str, object]],
            ]:
                return (
                    call_usage,
                    PreviewDocumentAnchorResult(
                        status=status,
                        candidate_count=candidate_count,
                        estimated_input_tokens=estimated_input_tokens,
                        output_token_budget=output_budget,
                        soft_input_tokens=DOCUMENT_ANCHOR_SOFT_INPUT_TOKENS,
                        hard_input_tokens=DOCUMENT_ANCHOR_HARD_INPUT_TOKENS,
                        error=message,
                        request_id=result.request_id,
                        actual_input_tokens=result.input_tokens,
                        actual_output_tokens=result.output_tokens,
                        finish_reason=result.finish_reason,
                        returned_chars=len(result.content),
                    ),
                    [{"code": diagnostic_code, "message": message}],
                )

            if result.finish_reason == "length":
                return failed_anchor_result(
                    "truncated_output",
                    "文档级标题消歧输出达到 token 上限。",
                    "document_anchor_truncated_output",
                )
            try:
                parsed = json.loads(result.content)
            except json.JSONDecodeError:
                return failed_anchor_result(
                    "invalid_json",
                    "文档级标题消歧返回了非法 JSON。",
                    "document_anchor_invalid_json",
                )
            try:
                payload = CandidateDocumentAnchorPayload.model_validate(parsed)
            except ValueError:
                return failed_anchor_result(
                    "invalid_schema",
                    "文档级标题消歧 JSON 不符合输出 schema。",
                    "document_anchor_invalid_schema",
                )

        candidate_by_ref = {candidate.candidate_ref: candidate for candidate in anchor_candidates}
        returned_refs = [item.candidate_ref for item in payload.items]
        if len(returned_refs) != len(set(returned_refs)) or set(returned_refs) != set(
            candidate_by_ref
        ):
            message = "文档级标题消歧没有且仅没有覆盖全部候选。"
            return failed_anchor_result(
                "invalid_schema",
                message,
                "document_anchor_invalid_schema",
            )

        row_by_id = {candidate.candidate_id: candidate for candidate in candidates}
        block_by_id = {block.block_id: block for block in blocks}
        diagnostics: list[dict[str, object]] = []
        prepared_updates: dict[str, CandidateDocumentAnchorDecision] = {}
        for item in payload.items:
            anchor_candidate = candidate_by_ref[item.candidate_ref]
            if normalize_name(item.provisional_name) != normalize_name(anchor_candidate.name):
                message = "文档级标题消歧返回了与输入不一致的 provisional_name。"
                return failed_anchor_result(
                    "invalid_schema",
                    message,
                    "document_anchor_provisional_name_invalid",
                )
            unknown_anchor_blocks = [
                block_id for block_id in item.anchor_source_block_ids if block_id not in block_by_id
            ]
            if unknown_anchor_blocks:
                message = "文档级标题消歧引用了当前文档之外的 evidence block。"
                return failed_anchor_result(
                    "invalid_schema",
                    message,
                    "document_anchor_source_invalid",
                )
            row = row_by_id[anchor_candidate.candidate_id]
            local_evidence = normalize_name(
                "\n".join(
                    "\n".join(block_by_id[block_id].section_path)
                    + "\n"
                    + block_by_id[block_id].text
                    for block_id in json_loads_string_list(row.source_block_ids_json)
                )
            )
            aliases = ordered_unique(
                [
                    *json_loads_string_list(row.aliases_json),
                    *([row.name] if normalize_name(row.name) in local_evidence else []),
                    *item.aliases,
                ]
            )
            valid_aliases: list[str] = []
            for alias in aliases:
                normalized_alias = normalize_name(alias)
                if normalized_alias == normalize_name(item.final_name):
                    continue
                if normalized_alias in local_evidence:
                    valid_aliases.append(alias)
                    continue
                diagnostics.append(
                    {
                        "code": "document_anchor_alias_dropped",
                        "doc_id": doc_id,
                        "candidate_name": item.final_name,
                        "alias": alias,
                        "message": "文档级标题消歧 alias 不在候选自己的 evidence 中，已忽略。",
                    }
                )
            prepared_updates[row.candidate_id] = item.model_copy(update={"aliases": valid_aliases})

        for row in candidates:
            update = prepared_updates[row.candidate_id]
            row.name = update.final_name
            row.normalized_name = normalize_name(update.final_name)
            row.provisional_name = row.provisional_name or update.provisional_name
            row.title_kind = update.title_kind
            row.title_standalone_clear = update.standalone_clear
            row.missing_anchor_types_json = json_dumps(update.missing_anchor_types)
            row.name_changed = normalize_name(update.final_name) != normalize_name(
                update.provisional_name
            )
            row.aliases_json = json_dumps(update.aliases)
            row.name_anchor_source_block_ids_json = json_dumps(update.anchor_source_block_ids)
            row.name_anchor_reason = update.reason
            row.content_hash = self._candidate_content_hash(row)

        return (
            call_usage,
            PreviewDocumentAnchorResult(
                status="completed",
                candidate_count=candidate_count,
                estimated_input_tokens=estimated_input_tokens,
                output_token_budget=output_budget,
                soft_input_tokens=DOCUMENT_ANCHOR_SOFT_INPUT_TOKENS,
                hard_input_tokens=DOCUMENT_ANCHOR_HARD_INPUT_TOKENS,
                request_id=result.request_id,
                actual_input_tokens=result.input_tokens,
                actual_output_tokens=result.output_tokens,
                finish_reason=result.finish_reason,
                returned_chars=len(result.content),
            ),
            diagnostics,
        )

    @staticmethod
    def _candidate_content_hash(candidate: PreviewCandidateRow) -> str:
        """按候选可审核内容计算稳定 hash，包含文档级标题消歧结果。"""

        return stable_hash(
            {
                "name": candidate.name,
                "provisional_name": candidate.provisional_name,
                "title_kind": candidate.title_kind,
                "title_standalone_clear": candidate.title_standalone_clear,
                "missing_anchor_types": json_loads_string_list(candidate.missing_anchor_types_json),
                "name_changed": candidate.name_changed,
                "aliases": json_loads_string_list(candidate.aliases_json),
                "summaries": json_loads_string_list(candidate.summaries_json),
                "selection_reasons": json_loads_object_list(candidate.selection_reasons_json),
                "name_anchor_source_block_ids": json_loads_string_list(
                    candidate.name_anchor_source_block_ids_json
                ),
                "name_anchor_reason": candidate.name_anchor_reason,
                "source_block_ids": json_loads_string_list(candidate.source_block_ids_json),
                "channel_ids": json_loads_string_list(candidate.channel_ids_json),
            }
        )

    @staticmethod
    def _knowledge_base_audit_candidates(
        candidates_by_doc: dict[str, list[PreviewCandidateRow]],
    ) -> list[_KnowledgeBaseAuditCandidate]:
        """汇总当前 Run 的知识库级标题、别名和本地摘要，不包含文档正文。"""

        grouped: dict[str, _KnowledgeBaseAuditCandidate] = {}
        for candidates in candidates_by_doc.values():
            for candidate in candidates:
                current = grouped.get(candidate.normalized_name)
                if current is None:
                    current = _KnowledgeBaseAuditCandidate(
                        candidate_ref="ca_"
                        + sha256_text(f"name_scope\0{candidate.normalized_name}")[:16],
                        normalized_name=candidate.normalized_name,
                        name=candidate.name,
                        aliases=[],
                        summaries=[],
                    )
                    grouped[candidate.normalized_name] = current
                current.aliases = ordered_unique(
                    [
                        *current.aliases,
                        *json_loads_string_list(candidate.aliases_json),
                    ]
                )
                current.summaries = ordered_unique_normalized_text(
                    [
                        *current.summaries,
                        *json_loads_string_list(candidate.summaries_json),
                    ]
                )
        return sorted(grouped.values(), key=lambda candidate: candidate.normalized_name)

    async def _run_name_scope_audit(
        self,
        *,
        provider: ChatProvider,
        profile: ExtractionProfileConfig,
        candidates: list[_KnowledgeBaseAuditCandidate],
        semaphore: asyncio.Semaphore,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        """只把 Profile 与主标题送给模型，审查标题独立可定位性。"""

        messages = [
            ChatMessage(
                role="system",
                content=build_candidate_name_scope_audit_prompt(profile),
            ),
            ChatMessage(
                role="user",
                content=json_dumps(
                    {
                        "profile_name": profile.name,
                        "knowledge_base_scope": {
                            "domain_context": profile.domain_context,
                            "primary_uses": profile.primary_uses,
                        },
                        "candidate_titles": [
                            {
                                "candidate_ref": candidate.candidate_ref,
                                "name": candidate.name,
                            }
                            for candidate in candidates
                        ],
                    }
                ),
            ),
        ]
        async with semaphore:
            try:
                result = await provider.complete(messages, max_output_tokens=8_192)
            except ChatProviderError as error:
                raise ProfileExtractionError(
                    error.code,
                    error.message,
                    503 if error.retryable else 502,
                ) from error
            try:
                payload = CandidateNameScopeAuditPayload.model_validate_json(result.content)
            except Exception as error:
                raise ProfileExtractionError(
                    "candidate_name_scope_audit_invalid",
                    "candidate name scope audit returned invalid JSON",
                    502,
                ) from error

        candidate_by_ref = {candidate.candidate_ref: candidate for candidate in candidates}
        returned_refs = [item.candidate_ref for item in payload.items]
        if len(returned_refs) != len(set(returned_refs)):
            raise ProfileExtractionError(
                "candidate_name_scope_audit_invalid",
                "candidate name scope audit returned duplicate candidate references",
                502,
            )
        if set(returned_refs) != set(candidate_by_ref):
            raise ProfileExtractionError(
                "candidate_name_scope_audit_invalid",
                "candidate name scope audit did not cover every candidate name",
                502,
            )
        decisions = [
            {
                "audit_kind": "name_scope",
                "normalized_name": candidate_by_ref[item.candidate_ref].normalized_name,
                "name": candidate_by_ref[item.candidate_ref].name,
                "verdict": item.verdict,
                "scope_anchor": item.scope_anchor,
                "suggested_name": item.suggested_name,
                "reason": item.reason,
            }
            for item in payload.items
        ]
        usage = {
            "channel_id": "candidate_name_scope_audit",
            "channel_name": "名称范围审查",
            "operation": "name_scope_audit",
            "request_id": result.request_id,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        }
        return usage, decisions

    async def _run_name_meaning_audit(
        self,
        *,
        provider: ChatProvider,
        profile: ExtractionProfileConfig,
        candidates: list[_KnowledgeBaseAuditCandidate],
        semaphore: asyncio.Semaphore,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        """审查主标题及别名是否锚定本地 summary 的中心对象。"""

        messages = [
            ChatMessage(
                role="system",
                content=build_candidate_name_meaning_audit_prompt(profile),
            ),
            ChatMessage(
                role="user",
                content=json_dumps(
                    {
                        "profile_name": profile.name,
                        "knowledge_base_scope": {
                            "domain_context": profile.domain_context,
                            "primary_uses": profile.primary_uses,
                        },
                        "candidates": [
                            {
                                "candidate_ref": candidate.candidate_ref,
                                "name": candidate.name,
                                "aliases": candidate.aliases,
                                "summaries": candidate.summaries,
                            }
                            for candidate in candidates
                        ],
                    }
                ),
            ),
        ]
        async with semaphore:
            try:
                result = await provider.complete(messages, max_output_tokens=8_192)
            except ChatProviderError as error:
                raise ProfileExtractionError(
                    error.code,
                    error.message,
                    503 if error.retryable else 502,
                ) from error
            try:
                payload = CandidateNameMeaningAuditPayload.model_validate_json(result.content)
            except Exception as error:
                raise ProfileExtractionError(
                    "candidate_name_meaning_audit_invalid",
                    "candidate name meaning audit returned invalid JSON",
                    502,
                ) from error

        candidate_by_ref = {candidate.candidate_ref: candidate for candidate in candidates}
        returned_refs = [item.candidate_ref for item in payload.items]
        if len(returned_refs) != len(set(returned_refs)):
            raise ProfileExtractionError(
                "candidate_name_meaning_audit_invalid",
                "candidate name meaning audit returned duplicate candidate references",
                502,
            )
        if set(returned_refs) != set(candidate_by_ref):
            raise ProfileExtractionError(
                "candidate_name_meaning_audit_invalid",
                "candidate name meaning audit did not cover every candidate",
                502,
            )
        decisions = [
            {
                "audit_kind": "name_meaning",
                "normalized_name": candidate_by_ref[item.candidate_ref].normalized_name,
                "name": candidate_by_ref[item.candidate_ref].name,
                "verdict": item.verdict,
                "suggested_name": item.suggested_name,
                "reason": item.reason,
            }
            for item in payload.items
        ]
        usage = {
            "channel_id": "candidate_name_meaning_audit",
            "channel_name": "名称含义审查",
            "operation": "name_meaning_audit",
            "request_id": result.request_id,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        }
        return usage, decisions

    @staticmethod
    def _window_audit_candidates_from_rows(
        *,
        doc_id: str,
        window_index: int,
        window_blocks: list[RetrievalBlock],
        candidates: list[PreviewCandidateRow],
    ) -> list[_WindowAuditCandidate]:
        """从文档级标题消歧后的候选构造当前窗口内容审查输入。"""

        window_block_ids = {block.block_id for block in window_blocks}
        result: list[_WindowAuditCandidate] = []
        for candidate in candidates:
            source_block_ids = [
                block_id
                for block_id in json_loads_string_list(candidate.source_block_ids_json)
                if block_id in window_block_ids
            ]
            if not source_block_ids:
                continue
            selection_reasons = [
                CandidateSelectionReason.model_validate(value)
                for value in json_loads_object_list(candidate.selection_reasons_json)
            ]
            relevant_reasons = [
                reason
                for reason in selection_reasons
                if set(reason.source_block_ids).intersection(source_block_ids)
            ]
            result.append(
                _WindowAuditCandidate(
                    candidate_ref="ca_"
                    + sha256_text(f"{doc_id}\0{window_index}\0{candidate.candidate_id}")[:16],
                    candidate_id=candidate.candidate_id,
                    normalized_name=candidate.normalized_name,
                    name=candidate.name,
                    aliases=json_loads_string_list(candidate.aliases_json),
                    summaries=json_loads_string_list(candidate.summaries_json),
                    selection_reason="；".join(
                        ordered_unique(reason.reason for reason in relevant_reasons)
                    ),
                    source_block_ids=source_block_ids,
                    channel_ids=json_loads_string_list(candidate.channel_ids_json),
                )
            )
        return sorted(result, key=lambda item: item.normalized_name)

    async def _run_semantic_audit(
        self,
        *,
        provider: ChatProvider,
        profile: ExtractionProfileConfig,
        doc_id: str,
        doc_title: str,
        blocks: list[RetrievalBlock],
        window_index: int,
        candidates: list[_WindowAuditCandidate],
        semaphore: asyncio.Semaphore,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        """审查一个窗口内的去重候选，返回可持久化的逐候选结论。"""

        messages = [
            ChatMessage(role="system", content=build_candidate_audit_prompt(profile)),
            ChatMessage(
                role="user",
                content=json_dumps(
                    {
                        "document": {"title": doc_title},
                        "window_index": window_index,
                        "evidence_blocks": [
                            {
                                "block_id": block.block_id,
                                "section_path": block.section_path,
                                "text": block.text,
                            }
                            for block in blocks
                        ],
                        "candidates": [
                            {
                                "candidate_ref": candidate.candidate_ref,
                                "name": candidate.name,
                                "aliases": candidate.aliases,
                                "summaries": candidate.summaries,
                                "selection_reason": candidate.selection_reason,
                                "source_block_ids": candidate.source_block_ids,
                                "channel_ids": candidate.channel_ids,
                            }
                            for candidate in candidates
                        ],
                    }
                ),
            ),
        ]
        async with semaphore:
            try:
                result = await provider.complete(messages, max_output_tokens=8_192)
            except ChatProviderError as error:
                raise ProfileExtractionError(
                    error.code,
                    error.message,
                    503 if error.retryable else 502,
                ) from error
            try:
                payload = CandidateSemanticAuditPayload.model_validate_json(result.content)
            except Exception as error:
                raise ProfileExtractionError(
                    "candidate_audit_invalid",
                    "candidate audit returned invalid JSON",
                    502,
                ) from error

        candidate_by_ref = {candidate.candidate_ref: candidate for candidate in candidates}
        returned_refs = [item.candidate_ref for item in payload.items]
        if len(returned_refs) != len(set(returned_refs)):
            raise ProfileExtractionError(
                "candidate_audit_invalid",
                "candidate audit returned duplicate candidate references",
                502,
            )
        if set(returned_refs) != set(candidate_by_ref):
            raise ProfileExtractionError(
                "candidate_audit_invalid",
                "candidate audit did not return every requested candidate exactly once",
                502,
            )

        decisions: list[dict[str, object]] = []
        for item in payload.items:
            candidate = candidate_by_ref[item.candidate_ref]
            if any(
                block_id not in candidate.source_block_ids for block_id in item.evidence_block_ids
            ):
                raise ProfileExtractionError(
                    "candidate_audit_evidence_invalid",
                    "candidate audit referenced evidence outside the audited candidate",
                    502,
                )
            decisions.append(
                {
                    "audit_kind": "content",
                    "candidate_id": candidate.candidate_id,
                    "doc_id": doc_id,
                    "normalized_name": candidate.normalized_name,
                    "window_index": window_index,
                    "verdict": item.verdict,
                    "issues": [
                        {
                            "code": issue.code,
                            "message": issue.message,
                            "alias": issue.alias,
                        }
                        for issue in item.issues
                    ],
                    "reason": item.reason,
                    "evidence_block_ids": item.evidence_block_ids,
                }
            )
        usage = {
            "channel_id": "candidate_audit",
            "channel_name": "LLM 候选审查",
            "operation": "semantic_audit",
            "window_index": window_index,
            "request_id": result.request_id,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        }
        return usage, decisions

    async def _run_channel(
        self,
        *,
        provider: ChatProvider,
        channel_prompt: CompiledChannelPrompt,
        doc_id: str,
        doc_title: str,
        blocks: list[RetrievalBlock],
        window_index: int,
        existing_candidates: list[ExistingCandidateContext] | None,
        operation: Literal["discovery", "omission_retry"],
        semaphore: asyncio.Semaphore,
        audit_profile: ExtractionProfileConfig | None = None,
    ) -> _ChannelWindowResult:
        system_prompt = (
            build_missing_audit_prompt(
                profile=audit_profile,
                channel=channel_prompt.channel,
            )
            if audit_profile is not None
            else channel_prompt.system_prompt
        )
        user_content = build_evidence_message(
            doc_title=doc_title,
            source_label=blocks[0].source_label,
            blocks=blocks,
            existing_candidates=existing_candidates,
        )
        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=user_content),
        ]
        attempt_usage: list[dict[str, object]] = []
        retry_diagnostics: list[dict[str, object]] = []
        for attempt in range(1, CANDIDATE_OUTPUT_MAX_ATTEMPTS + 1):
            request_messages = messages
            if attempt > 1:
                previous = attempt_usage[-1]
                request_messages = [
                    *messages,
                    ChatMessage(role="assistant", content=str(previous["_content"])),
                    ChatMessage(
                        role="user",
                        content=(
                            "上一次输出未通过 candidate schema 校验。不要解释，也不要改变原任务；"
                            "重新只输出完整的候选 JSON 对象，逐字遵守原输出 schema。"
                        ),
                    ),
                ]
            output_budget = (
                CANDIDATE_OUTPUT_MAX_TOKENS if attempt == 1 else CANDIDATE_OUTPUT_RETRY_MAX_TOKENS
            )
            async with semaphore:
                try:
                    result = await provider.complete(
                        request_messages,
                        max_output_tokens=output_budget,
                    )
                except ChatProviderError as error:
                    raise ProfileExtractionError(
                        error.code,
                        error.message,
                        503 if error.retryable else 502,
                        diagnostics={
                            "doc_id": doc_id,
                            "channel_id": channel_prompt.channel.channel_id,
                            "operation": operation,
                            "window_index": window_index,
                            "attempt": attempt,
                        },
                    ) from error
            call_details: dict[str, object] = {
                "request_id": result.request_id,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "finish_reason": result.finish_reason,
                "returned_chars": len(result.content),
                "max_output_tokens": output_budget,
                "_content": result.content,
            }
            try:
                candidates, candidate_diagnostics = self._parse_candidates(
                    result.content,
                    blocks,
                    existing_candidates=existing_candidates,
                )
            except ProfileExtractionError as error:
                error.diagnostics = {
                    **error.diagnostics,
                    "doc_id": doc_id,
                    "channel_id": channel_prompt.channel.channel_id,
                    "operation": operation,
                    "window_index": window_index,
                    "attempt": attempt,
                    "max_attempts": CANDIDATE_OUTPUT_MAX_ATTEMPTS,
                    "request_id": result.request_id,
                    "finish_reason": result.finish_reason,
                    "returned_chars": len(result.content),
                    "max_output_tokens": output_budget,
                }
                if (
                    error.code != "candidate_output_invalid"
                    or attempt == CANDIDATE_OUTPUT_MAX_ATTEMPTS
                ):
                    if error.code == "candidate_output_invalid":
                        error.message = (
                            f"候选抽取输出连续 {attempt} 次结构不符合要求；"
                            f"文档「{doc_title}」、通道「{channel_prompt.channel.name}」、"
                            f"窗口 {window_index + 1}。"
                        )
                    error.diagnostics["attempts"] = [
                        {key: value for key, value in details.items() if key != "_content"}
                        for details in [*attempt_usage, call_details]
                    ]
                    raise
                attempt_usage.append(call_details)
                retry_diagnostics.append(
                    {
                        "code": "candidate_output_retry",
                        "message": "candidate extractor returned invalid output; retrying once",
                        "doc_id": doc_id,
                        "channel_id": channel_prompt.channel.channel_id,
                        "channel_name": channel_prompt.channel.name,
                        "operation": operation,
                        "window_index": window_index,
                        "attempt": attempt,
                        "request_id": result.request_id,
                        "finish_reason": result.finish_reason,
                        "returned_chars": len(result.content),
                        "max_output_tokens": output_budget,
                        "validation_error": error.diagnostics.get("validation_error"),
                    }
                )
                continue
            attempt_usage.append(call_details)
            usage_content = [
                {key: value for key, value in details.items() if key != "_content"}
                for details in attempt_usage
            ]
            usage = {
                "channel_id": channel_prompt.channel.channel_id,
                "channel_name": channel_prompt.channel.name,
                "operation": operation,
                "window_index": window_index,
                "doc_id": doc_id,
                "request_id": result.request_id,
                "input_tokens": sum(cast(int, item["input_tokens"]) for item in usage_content),
                "output_tokens": sum(cast(int, item["output_tokens"]) for item in usage_content),
                "attempts": attempt,
                "request_ids": [str(item["request_id"]) for item in usage_content],
                "finish_reasons": [item["finish_reason"] for item in usage_content],
                "returned_chars": sum(cast(int, item["returned_chars"]) for item in usage_content),
            }
            return _ChannelWindowResult(
                channel_id=channel_prompt.channel.channel_id,
                operation=operation,
                window_index=window_index,
                candidates=candidates,
                usage=usage,
                diagnostics=[
                    *retry_diagnostics,
                    *[
                        {
                            **diagnostic,
                            "doc_id": doc_id,
                            "channel_id": channel_prompt.channel.channel_id,
                            "channel_name": channel_prompt.channel.name,
                            "operation": operation,
                            "window_index": window_index,
                        }
                        for diagnostic in candidate_diagnostics
                    ],
                ],
            )
        raise AssertionError("candidate extraction attempt loop must return or raise")

    @staticmethod
    def _parse_candidates(
        content: str,
        blocks: list[RetrievalBlock],
        *,
        existing_candidates: list[ExistingCandidateContext] | None,
    ) -> tuple[list[CandidateDraft], list[dict[str, object]]]:
        try:
            response = CandidateExtractionResponse.model_validate_json(content)
        except Exception as error:
            raise ProfileExtractionError(
                "candidate_output_invalid",
                "candidate extractor returned invalid JSON",
                diagnostics={
                    "validation_error": f"{type(error).__name__}: {str(error)[:1_000]}".replace(
                        "\n", " "
                    )
                },
            ) from error
        allowed = {block.block_id: block for block in blocks}
        existing_by_ref = {
            candidate.candidate_ref: candidate for candidate in existing_candidates or []
        }
        diagnostics: list[dict[str, object]] = []
        accepted_candidates: list[CandidateDraft] = []
        for candidate in response.candidates:
            if candidate.existing_candidate_ref is not None:
                existing = existing_by_ref.get(candidate.existing_candidate_ref)
                if existing is None:
                    raise ProfileExtractionError(
                        "candidate_existing_ref_invalid",
                        "candidate referenced an existing candidate outside its attachment",
                    )
                if normalize_name(candidate.name) != normalize_name(existing.name):
                    diagnostics.append(
                        {
                            "code": "candidate_existing_name_replaced",
                            "candidate_name": existing.name,
                            "proposed_name": candidate.name,
                            "message": (
                                "existing candidate contribution proposed a different name; "
                                "the established name was retained"
                            ),
                        }
                    )
                    candidate.aliases = [*candidate.aliases, candidate.name]
                    candidate.name = existing.name
            outside_window = [
                block_id for block_id in candidate.source_block_ids if block_id not in allowed
            ]
            if outside_window:
                candidate.source_block_ids = [
                    block_id
                    for block_id in candidate.source_block_ids
                    if block_id not in outside_window
                ]
                diagnostics.append(
                    {
                        "code": (
                            "candidate_source_out_of_window_dropped"
                            if candidate.source_block_ids
                            else "candidate_without_current_source_dropped"
                        ),
                        "candidate_name": candidate.name,
                        "existing_candidate_ref": candidate.existing_candidate_ref,
                        "dropped_block_ids": outside_window,
                        "message": (
                            "candidate contribution referenced evidence outside the current "
                            "window; only current-window sources were retained"
                        ),
                    }
                )
                if not candidate.source_block_ids:
                    continue
            if not candidate.selection_reason.strip():
                diagnostics.append(
                    {
                        "code": "candidate_reason_missing",
                        "candidate_name": candidate.name,
                        "message": "candidate did not include a selection reason",
                    }
                )
            evidence = "\n".join(
                "\n".join(allowed[block_id].section_path) + "\n" + allowed[block_id].text
                for block_id in candidate.source_block_ids
            ).casefold()
            normalized_evidence = normalize_name(evidence)
            valid_aliases: list[str] = []
            for alias in candidate.aliases:
                if normalize_name(alias) in normalized_evidence:
                    valid_aliases.append(alias)
                    continue
                diagnostics.append(
                    {
                        "code": "candidate_alias_dropped",
                        "candidate_name": candidate.name,
                        "alias": alias,
                        "message": "candidate alias did not occur in its evidence and was ignored",
                    }
                )
            candidate.aliases = valid_aliases
            accepted_candidates.append(candidate)
        return accepted_candidates, diagnostics

    @staticmethod
    def _accumulated_candidate_contexts(
        candidates_by_ref: dict[str, _DocumentCandidate],
    ) -> list[ExistingCandidateContext]:
        """把顺序累积候选转为后续窗口或通道的只读附件。"""

        return [
            ExistingCandidateContext(
                candidate_ref=candidate.candidate_ref,
                name=candidate.name,
                aliases=candidate.aliases,
                summaries=candidate.summaries,
                source_block_ids=candidate.source_block_ids,
                channel_ids=candidate.channel_ids,
            )
            for candidate in sorted(
                candidates_by_ref.values(),
                key=lambda item: item.normalized_name,
            )
        ]

    @staticmethod
    def _accumulate_channel_result(
        *,
        doc_id: str,
        candidates_by_ref: dict[str, _DocumentCandidate],
        refs_by_normalized: dict[str, str],
        result: _ChannelWindowResult,
    ) -> None:
        """把顺序抽取批次结果合入文档候选账本。"""

        for draft in result.candidates:
            normalized = normalize_name(draft.name)
            candidate_ref = draft.existing_candidate_ref
            if candidate_ref is not None:
                current = candidates_by_ref.get(candidate_ref)
                if current is None:
                    raise ProfileExtractionError(
                        "candidate_existing_ref_invalid",
                        "candidate referenced an unknown existing candidate",
                    )
            else:
                candidate_ref = refs_by_normalized.get(normalized)
                current = candidates_by_ref.get(candidate_ref) if candidate_ref else None
            if current is None:
                candidate_ref = "ca_" + sha256_text(f"{doc_id}\0{normalized}")[:16]
                current = _DocumentCandidate(
                    candidate_ref=candidate_ref,
                    name=draft.name,
                    normalized_name=normalized,
                    aliases=[],
                    summaries=[],
                    selection_reasons=[],
                    source_block_ids=[],
                )
                candidates_by_ref[candidate_ref] = current
                refs_by_normalized[normalized] = candidate_ref
            current.aliases = ordered_unique([*current.aliases, *draft.aliases])
            current.summaries = ordered_unique_normalized_text(
                [*current.summaries, *draft.summaries]
            )
            if draft.selection_reason.strip():
                selection_reason = CandidateSelectionReason(
                    channel_id=result.channel_id,
                    operation=result.operation,
                    window_index=result.window_index,
                    reason=draft.selection_reason.strip(),
                    source_block_ids=draft.source_block_ids,
                )
                reason_key = (
                    selection_reason.channel_id,
                    selection_reason.operation,
                    selection_reason.window_index,
                    selection_reason.reason,
                    tuple(selection_reason.source_block_ids),
                )
                if all(
                    (
                        existing.channel_id,
                        existing.operation,
                        existing.window_index,
                        existing.reason,
                        tuple(existing.source_block_ids),
                    )
                    != reason_key
                    for existing in current.selection_reasons
                ):
                    current.selection_reasons.append(selection_reason)
            current.source_block_ids = ordered_unique(
                [*current.source_block_ids, *draft.source_block_ids]
            )
            current.channel_ids = ordered_unique([*current.channel_ids, result.channel_id])

    @staticmethod
    def _document_candidate_rows(
        *,
        doc_id: str,
        doc_version: int,
        run_id: str,
        candidates: list[_DocumentCandidate],
        evidence_by_id: dict[str, RetrievalBlock],
    ) -> list[PreviewCandidateRow]:
        """把累积候选账本转换为待持久化的文档候选。"""

        rows: list[PreviewCandidateRow] = []
        for ordinal, candidate in enumerate(
            sorted(candidates, key=lambda item: item.normalized_name)
        ):
            aliases = [
                alias
                for alias in ordered_unique(candidate.aliases)
                if normalize_name(alias) != candidate.normalized_name
            ]
            content_hash = stable_hash(
                {
                    "name": candidate.name,
                    "provisional_name": candidate.name,
                    "title_kind": None,
                    "title_standalone_clear": None,
                    "missing_anchor_types": [],
                    "name_changed": False,
                    "aliases": aliases,
                    "summaries": candidate.summaries,
                    "selection_reasons": [
                        selection_reason.model_dump(mode="json")
                        for selection_reason in candidate.selection_reasons
                    ],
                    "name_anchor_source_block_ids": [],
                    "name_anchor_reason": None,
                    "source_block_ids": candidate.source_block_ids,
                    "channel_ids": candidate.channel_ids,
                }
            )
            rows.append(
                PreviewCandidateRow(
                    candidate_id="wc_"
                    + sha256_text(f"{run_id}\0{doc_id}\0{candidate.normalized_name}")[:16],
                    run_id=run_id,
                    doc_id=doc_id,
                    doc_version=doc_version,
                    ordinal=ordinal,
                    name=candidate.name,
                    normalized_name=candidate.normalized_name,
                    provisional_name=candidate.name,
                    title_kind=None,
                    title_standalone_clear=None,
                    missing_anchor_types_json="[]",
                    name_changed=False,
                    aliases_json=json_dumps(aliases),
                    summaries_json=json_dumps(candidate.summaries),
                    selection_reasons_json=json_dumps(
                        [
                            selection_reason.model_dump(mode="json")
                            for selection_reason in candidate.selection_reasons
                        ]
                    ),
                    name_anchor_source_block_ids_json="[]",
                    name_anchor_reason=None,
                    source_block_ids_json=json_dumps(candidate.source_block_ids),
                    evidence_blocks_json=json_dumps(
                        [
                            {
                                "block_id": block_id,
                                "section_path": evidence_by_id[block_id].section_path,
                                "text": evidence_by_id[block_id].text,
                            }
                            for block_id in candidate.source_block_ids
                        ]
                    ),
                    channel_ids_json=json_dumps(candidate.channel_ids),
                    content_hash=content_hash,
                )
            )
        return rows

    def _provider(self) -> ChatProvider:
        key = self._credentials.get(CHAT_API_KEY_USERNAME) or ""
        if self._chat_provider_factory is not None:
            return self._chat_provider_factory(key)
        return DeepSeekChatProvider(key)

    @staticmethod
    def _translation_key(model_id: str, source_text: str) -> str:
        """生成跨 Run 稳定的翻译缓存键，文本变化或模型变化时自动失效。"""

        return sha256_text(f"{EVIDENCE_TRANSLATION_PROMPT_VERSION}\0{model_id}\0{source_text}")

    @staticmethod
    def _translation_batches(
        pending: list[tuple[str, str, str]],
    ) -> list[list[tuple[str, str, str]]]:
        """按块数和字符数切分翻译请求，避免单次模型输入无限增长。"""

        batches: list[list[tuple[str, str, str]]] = []
        current: list[tuple[str, str, str]] = []
        current_chars = 0
        for item in pending:
            item_chars = len(item[1])
            if current and (
                len(current) >= TRANSLATION_BATCH_MAX_BLOCKS
                or current_chars + item_chars > TRANSLATION_BATCH_MAX_CHARS
            ):
                batches.append(current)
                current = []
                current_chars = 0
            current.append(item)
            current_chars += item_chars
        if current:
            batches.append(current)
        return batches

    def _find_reusable(self, fingerprint: str) -> ProfilePreviewRunRow | None:
        with self._database.session() as session:
            return session.scalar(
                select(ProfilePreviewRunRow)
                .where(
                    ProfilePreviewRunRow.preview_fingerprint == fingerprint,
                    ProfilePreviewRunRow.status == "completed",
                )
                .order_by(ProfilePreviewRunRow.completed_at.desc())
                .limit(1)
            )

    def _next_queued(self) -> ProfilePreviewRunRow | None:
        with self._database.session() as session:
            return session.scalar(
                select(ProfilePreviewRunRow)
                .where(ProfilePreviewRunRow.status == "queued")
                .order_by(ProfilePreviewRunRow.created_at)
                .limit(1)
            )

    def _mark_running(self, run_id: str) -> None:
        with self._database.session() as session, session.begin():
            row = session.get(ProfilePreviewRunRow, run_id)
            if row is None or row.status != "queued":
                return
            row.status = "running"
            row.started_at = utc_now()

    def _mark_failed(self, run_id: str, code: str, message: str) -> None:
        self._mark_terminal(
            run_id,
            "failed",
            error_code=code,
            error_message=message,
            usage=[],
            diagnostics=[],
        )

    def _mark_terminal(
        self,
        run_id: str,
        status: str,
        *,
        error_code: str | None,
        error_message: str | None,
        usage: list[dict[str, object]],
        diagnostics: list[dict[str, object]],
    ) -> None:
        with self._database.session() as session, session.begin():
            row = session.get(ProfilePreviewRunRow, run_id)
            if row is None:
                return
            row.status = status
            if row.semantic_audit_enabled and row.semantic_audit_status in {
                "pending",
                "running",
            }:
                row.semantic_audit_status = "interrupted" if status == "cancelled" else "failed"
                row.semantic_audit_error = error_message
            row.error_code = error_code
            row.error_message = error_message
            row.usage_json = json_dumps(usage)
            row.diagnostics_json = json_dumps(diagnostics)
            row.completed_at = utc_now()

    def _raise_if_cancelled(self, run_id: str) -> None:
        if run_id in self._cancel_requested:
            raise ProfileExtractionError(
                "preview_run_cancelled",
                "Preview Run was cancelled",
                409,
            )

    @staticmethod
    def _document_version(reader: RetrievalSnapshotReader, doc_id: str) -> int:
        document = next(
            document for document in reader.manifest.documents if document.doc_id == doc_id
        )
        return document.doc_version

    @staticmethod
    def _candidate_rows(session: Any, run_id: str) -> list[PreviewCandidateRow]:
        return list(
            session.scalars(
                select(PreviewCandidateRow)
                .where(PreviewCandidateRow.run_id == run_id)
                .order_by(PreviewCandidateRow.doc_id, PreviewCandidateRow.ordinal)
            ).all()
        )

    @classmethod
    def _candidate_models(
        cls,
        rows: list[PreviewCandidateRow],
        *,
        semantic_audit_json: str,
        semantic_audit_enabled: bool,
        semantic_audit_status: str,
    ) -> list[PreviewCandidate]:
        """合并确定性别名冲突和持久化 LLM 结论为候选审查状态。"""

        primary_by_doc = {
            (row.doc_id, row.normalized_name): (row.candidate_id, row.name) for row in rows
        }
        semantic_by_candidate_id: dict[str, list[dict[str, object]]] = {}
        semantic_by_document_name: dict[tuple[str, str], list[dict[str, object]]] = {}
        name_scope_by_name: dict[str, dict[str, object]] = {}
        name_meaning_by_name: dict[str, dict[str, object]] = {}
        for decision in json_loads_object_list(semantic_audit_json):
            audit_kind = str(decision.get("audit_kind", "content"))
            candidate_id = str(decision.get("candidate_id", ""))
            document_id = str(decision.get("doc_id", ""))
            normalized_name = str(decision.get("normalized_name", ""))
            if audit_kind == "name_scope" and normalized_name:
                name_scope_by_name[normalized_name] = decision
            elif audit_kind == "name_meaning" and normalized_name:
                name_meaning_by_name[normalized_name] = decision
            elif candidate_id:
                semantic_by_candidate_id.setdefault(candidate_id, []).append(decision)
            elif document_id and normalized_name:
                semantic_by_document_name.setdefault(
                    (document_id, normalized_name),
                    [],
                ).append(decision)

        result: list[PreviewCandidate] = []
        for row in rows:
            findings: list[CandidateAuditFinding] = []
            for alias in json_loads_string_list(row.aliases_json):
                target = primary_by_doc.get((row.doc_id, normalize_name(alias)))
                if target is None or target[0] == row.candidate_id:
                    continue
                findings.append(
                    CandidateAuditFinding(
                        code="alias_matches_separate_candidate",
                        message=(
                            f"别名“{alias}”与同文档候选“{target[1]}”的主名称相同，"
                            "疑似成员、组成项或分类项被误作别名。"
                        ),
                        alias=alias,
                        conflicting_candidate_id=target[0],
                        evidence_block_ids=json_loads_string_list(row.source_block_ids_json),
                    )
                )

            semantic_decisions = semantic_by_candidate_id.get(row.candidate_id)
            if semantic_decisions is None:
                semantic_decisions = semantic_by_document_name.get(
                    (row.doc_id, row.normalized_name),
                    [],
                )
            has_content_audit = bool(semantic_decisions)
            name_scope_decision = name_scope_by_name.get(row.normalized_name)
            name_meaning_decision = name_meaning_by_name.get(row.normalized_name)
            semantic_verdicts: list[str] = []
            semantic_reasons: list[str] = []
            for decision in semantic_decisions:
                verdict = str(decision.get("verdict", "uncertain"))
                semantic_verdicts.append(verdict)
                reason = str(decision.get("reason", "")).strip()
                if reason:
                    semantic_reasons.append(reason)
                evidence_value = decision.get("evidence_block_ids", [])
                evidence_block_ids = (
                    [str(block_id) for block_id in evidence_value if isinstance(block_id, str)]
                    if isinstance(evidence_value, list)
                    else []
                )
                issues = decision.get("issues", [])
                if isinstance(issues, list) and issues:
                    for issue in issues:
                        if not isinstance(issue, dict):
                            continue
                        findings.append(
                            CandidateAuditFinding(
                                code=str(issue.get("code", "other")),
                                message=str(issue.get("message", reason or "LLM 审查发现疑点")),
                                alias=(
                                    str(issue["alias"])
                                    if isinstance(issue.get("alias"), str)
                                    else None
                                ),
                                evidence_block_ids=evidence_block_ids,
                            )
                        )
                elif verdict != "clean":
                    findings.append(
                        CandidateAuditFinding(
                            code="semantic_audit_uncertain"
                            if verdict == "uncertain"
                            else "semantic_audit_suspicious",
                            message=reason or "LLM 审查发现疑点但未提供具体类型。",
                            evidence_block_ids=evidence_block_ids,
                        )
                    )

            if name_scope_decision is not None:
                name_scope_verdict = str(name_scope_decision.get("verdict", "uncertain"))
                name_scope_reason = str(name_scope_decision.get("reason", "")).strip()
                if name_scope_reason:
                    semantic_reasons.append(name_scope_reason)
                if name_scope_verdict == "ambiguous":
                    semantic_verdicts.append("suspicious")
                    suggested_name = name_scope_decision.get("suggested_name")
                    suggested_suffix = (
                        f"；建议名称：{suggested_name}"
                        if isinstance(suggested_name, str) and suggested_name
                        else ""
                    )
                    findings.append(
                        CandidateAuditFinding(
                            code="name_ambiguous_in_knowledge_base",
                            message=(
                                f"名称在当前知识库范围内存在歧义{suggested_suffix}。"
                                f"{name_scope_reason}"
                            ),
                        )
                    )
                elif name_scope_verdict == "misaligned":
                    # 兼容 v2 历史结果；新标题审查不会再返回 misaligned。
                    semantic_verdicts.append("suspicious")
                    findings.append(
                        CandidateAuditFinding(
                            code="name_meaning_misaligned",
                            message=(f"名称与本地摘要的含义不一致。{name_scope_reason}"),
                        )
                    )
                elif name_scope_verdict == "uncertain":
                    semantic_verdicts.append("uncertain")
                    findings.append(
                        CandidateAuditFinding(
                            code="name_scope_uncertain",
                            message=name_scope_reason or "无法仅凭名称判断知识库内区分度。",
                        )
                    )
                else:
                    semantic_verdicts.append("clean")
            elif semantic_audit_enabled:
                semantic_verdicts.append("uncertain")
                findings.append(
                    CandidateAuditFinding(
                        code="name_scope_audit_missing",
                        message=(
                            "该候选没有对应的标题区分度审查结果"
                            f"（整体状态：{semantic_audit_status}）。"
                        ),
                    )
                )

            if name_meaning_decision is not None:
                name_meaning_verdict = str(name_meaning_decision.get("verdict", "uncertain"))
                name_meaning_reason = str(name_meaning_decision.get("reason", "")).strip()
                if name_meaning_reason:
                    semantic_reasons.append(name_meaning_reason)
                if name_meaning_verdict == "misaligned":
                    semantic_verdicts.append("suspicious")
                    suggested_name = name_meaning_decision.get("suggested_name")
                    suggested_suffix = (
                        f"；建议名称：{suggested_name}"
                        if isinstance(suggested_name, str) and suggested_name
                        else ""
                    )
                    findings.append(
                        CandidateAuditFinding(
                            code="name_meaning_misaligned",
                            message=(
                                f"名称与本地摘要的中心对象不一致{suggested_suffix}。"
                                f"{name_meaning_reason}"
                            ),
                        )
                    )
                elif name_meaning_verdict == "uncertain":
                    semantic_verdicts.append("uncertain")
                    findings.append(
                        CandidateAuditFinding(
                            code="name_meaning_uncertain",
                            message=name_meaning_reason or "无法判断名称与本地摘要是否同指。",
                        )
                    )
                else:
                    semantic_verdicts.append("clean")
            elif semantic_audit_enabled:
                semantic_verdicts.append("uncertain")
                findings.append(
                    CandidateAuditFinding(
                        code="name_meaning_audit_missing",
                        message=(
                            "该候选没有对应的名称含义审查结果"
                            f"（整体状态：{semantic_audit_status}）。"
                        ),
                    )
                )

            if semantic_audit_enabled and not has_content_audit:
                semantic_verdicts.append("uncertain")
                findings.append(
                    CandidateAuditFinding(
                        code="content_audit_missing",
                        message=(
                            f"该候选没有对应的内容审查结果（整体状态：{semantic_audit_status}）。"
                        ),
                    )
                )

            audit_status: CandidateAuditStatus
            if (
                any(finding.code == "alias_matches_separate_candidate" for finding in findings)
                or "suspicious" in semantic_verdicts
            ):
                audit_status = "suspicious"
            elif "uncertain" in semantic_verdicts:
                audit_status = "uncertain"
            elif semantic_verdicts:
                audit_status = "clean"
            else:
                audit_status = "not_requested"
            result.append(
                cls._candidate(
                    row,
                    audit_status=audit_status,
                    audit_reason="；".join(ordered_unique(semantic_reasons)) or None,
                    audit_findings=findings,
                )
            )
        return result

    @staticmethod
    def _candidate(
        row: PreviewCandidateRow,
        *,
        audit_status: CandidateAuditStatus,
        audit_reason: str | None,
        audit_findings: list[CandidateAuditFinding],
    ) -> PreviewCandidate:
        return PreviewCandidate(
            candidate_id=row.candidate_id,
            doc_id=row.doc_id,
            doc_version=row.doc_version,
            ordinal=row.ordinal,
            name=row.name,
            normalized_name=row.normalized_name,
            provisional_name=row.provisional_name,
            title_kind=row.title_kind,  # type: ignore[arg-type]
            title_standalone_clear=row.title_standalone_clear,
            missing_anchor_types=json_loads_string_list(  # type: ignore[arg-type]
                row.missing_anchor_types_json
            ),
            name_changed=row.name_changed,
            aliases=json_loads_string_list(row.aliases_json),
            summaries=json_loads_string_list(row.summaries_json),
            selection_reasons=[
                CandidateSelectionReason.model_validate(value)
                for value in json_loads_object_list(row.selection_reasons_json)
            ],
            name_anchor_source_block_ids=json_loads_string_list(
                row.name_anchor_source_block_ids_json
            ),
            name_anchor_reason=row.name_anchor_reason,
            audit_status=audit_status,
            audit_reason=audit_reason,
            audit_findings=audit_findings,
            source_block_ids=json_loads_string_list(row.source_block_ids_json),
            evidence_blocks=json_loads_object_list(row.evidence_blocks_json),
            channel_ids=json_loads_string_list(row.channel_ids_json),
            content_hash=row.content_hash,
        )

    @staticmethod
    def _summary(row: ProfilePreviewRunRow) -> PreviewRunSummary:
        profile_config = json_loads_object(row.profile_config_snapshot_json)
        profile_name = str(profile_config.get("name", ""))
        channel_names = {
            str(channel["channel_id"]): str(channel.get("name") or "未命名通道")
            for channel in profile_config.get("extraction_channels", [])
            if isinstance(channel, dict) and channel.get("channel_id")
        }
        channel_definitions = {
            str(channel["channel_id"]): PreviewChannelDefinition(
                name=str(channel.get("name") or "未命名通道"),
                activation=str(channel.get("activation") or ""),
                include_criteria=[
                    str(value)
                    for value in channel.get("include_criteria", [])
                    if isinstance(value, str) and value
                ],
                exclude_criteria=[
                    str(value)
                    for value in channel.get("exclude_criteria", [])
                    if isinstance(value, str) and value
                ],
            )
            for channel in profile_config.get("extraction_channels", [])
            if isinstance(channel, dict) and channel.get("channel_id")
        }
        return PreviewRunSummary(
            run_id=row.run_id,
            snapshot_id=row.snapshot_id,
            snapshot_manifest_hash=row.snapshot_manifest_hash,
            profile_id=row.profile_id,
            profile_hash=row.profile_hash,
            profile_name=profile_name,
            channel_names=channel_names,
            channel_definitions=channel_definitions,
            semantic_audit_enabled=row.semantic_audit_enabled,
            semantic_audit_prompt_version=row.semantic_audit_prompt_version,
            name_scope_audit_prompt_version=row.name_scope_audit_prompt_version,
            name_meaning_audit_prompt_version=row.name_meaning_audit_prompt_version,
            document_anchor_prompt_version=row.document_anchor_prompt_version,
            document_anchor_policy_version=row.document_anchor_policy_version,
            document_anchor_results={
                doc_id: PreviewDocumentAnchorResult.model_validate(value)
                for doc_id, value in json_loads_object(row.document_anchor_json).items()
                if isinstance(value, dict)
            },
            semantic_audit_status=row.semantic_audit_status,  # type: ignore[arg-type]
            semantic_audit_error=row.semantic_audit_error,
            document_scope=json_loads_string_list(row.document_scope_json),
            status=row.status,  # type: ignore[arg-type]
            preview_fingerprint=row.preview_fingerprint,
            created_at=utc_iso(row.created_at),
            started_at=utc_iso(row.started_at) if row.started_at else None,
            completed_at=utc_iso(row.completed_at) if row.completed_at else None,
            error_code=row.error_code,
            error_message=row.error_message,
            usage=json_loads_object_list(row.usage_json),
            diagnostics=json_loads_object_list(row.diagnostics_json),
        )
