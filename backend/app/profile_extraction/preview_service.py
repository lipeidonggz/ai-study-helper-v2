from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from app.profile_extraction.errors import ProfileExtractionError
from app.profile_extraction.models import (
    CandidateDraft,
    CandidateExtractionResponse,
    EvidenceTranslationItem,
    EvidenceTranslationPayload,
    EvidenceTranslationRequest,
    EvidenceTranslationResponse,
    ExtractionProfile,
    ExtractionProfileConfig,
    PreviewCandidate,
    PreviewCandidatePage,
    PreviewRunDetail,
    PreviewRunPlan,
    PreviewRunPlanRequest,
    PreviewRunRequest,
    PreviewRunSummary,
)
from app.profile_extraction.profile_service import ProfileService
from app.profile_extraction.prompt_compiler import (
    COMPILER_VERSION,
    EVIDENCE_TRANSLATION_PROMPT_VERSION,
    PROMPT_VERSION,
    CompiledChannelPrompt,
    aggregate_prompt_hash,
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

CANDIDATE_PIPELINE_VERSION = "profile-preview-pipeline-v1"
MAX_PARALLEL_CALLS = 2
PROMPT_ENVELOPE_CHARS = 512
ESTIMATED_OUTPUT_TOKENS_PER_CALL = 512
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
    window_index: int
    candidates: list[CandidateDraft]
    usage: dict[str, object]


@dataclass(slots=True)
class _RawCandidate:
    name: str
    aliases: list[str]
    summary: str
    source_block_ids: list[str]
    channel_id: str
    window_index: int


@dataclass(slots=True)
class _DocumentCandidate:
    name: str
    normalized_name: str
    aliases: list[str]
    summary: str
    summary_variants: list[str]
    source_block_ids: list[str]
    channel_ids: list[str] = field(default_factory=list)


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
        audit_calls = windows * sum(
            1 for channel in enabled_channels if channel.coverage_audit_enabled
        )
        estimated_model_calls = initial_calls + audit_calls
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
            if prompt.channel.coverage_audit_enabled
            for groups in window_blocks_by_doc.values()
            for window in groups
        )
        estimated_input_tokens = estimate_tokens("x" * input_chars)
        estimated_output_tokens = estimated_model_calls * ESTIMATED_OUTPUT_TOKENS_PER_CALL
        existing = self._find_reusable(prepared.fingerprint)
        return PreviewRunPlan(
            snapshot_id=prepared.reader.snapshot_id,
            profile_id=prepared.profile.profile_id,
            profile_hash=prepared.profile.profile_hash,
            document_ids=list(prepared.blocks_by_doc),
            block_count=sum(len(blocks) for blocks in prepared.blocks_by_doc.values()),
            window_count=windows,
            estimated_model_calls=estimated_model_calls,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
            estimated_total_tokens=estimated_input_tokens + estimated_output_tokens,
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
            return PreviewRunDetail(
                **self._summary(row).model_dump(),
                candidates=[self._candidate(candidate) for candidate in candidates],
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
            return PreviewCandidatePage(
                candidates=[self._candidate(candidate) for candidate in page],
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
        candidates_by_doc: dict[str, list[PreviewCandidateRow]] = {}
        try:
            for doc_id in document_ids:
                self._raise_if_cancelled(run_id)
                blocks = list(reader.list_blocks(doc_id).blocks)
                window_groups = split_blocks(blocks)
                initial_tasks = [
                    self._run_channel(
                        provider=provider,
                        channel_prompt=prompt,
                        doc_title=blocks[0].doc_title,
                        blocks=window,
                        window_index=window_index,
                        existing_candidates=None,
                        operation="discovery",
                        semaphore=semaphore,
                    )
                    for prompt in prompts
                    for window_index, window in enumerate(window_groups)
                ]
                initial_results = await asyncio.gather(*initial_tasks)
                audit_tasks = []
                for prompt in prompts:
                    if not prompt.channel.coverage_audit_enabled:
                        continue
                    for window_index, window in enumerate(window_groups):
                        existing = [
                            candidate
                            for result in initial_results
                            if result.channel_id == prompt.channel.channel_id
                            and result.window_index == window_index
                            for candidate in result.candidates
                        ]
                        audit_tasks.append(
                            self._run_channel(
                                provider=provider,
                                channel_prompt=prompt,
                                doc_title=blocks[0].doc_title,
                                blocks=window,
                                window_index=window_index,
                                existing_candidates=existing,
                                operation="coverage_audit",
                                semaphore=semaphore,
                                audit_profile=config,
                            )
                        )
                audit_results = await asyncio.gather(*audit_tasks)
                results = [*initial_results, *audit_results]
                usage.extend(result.usage for result in results if result.usage)
                merged = self._merge_document_candidates(
                    doc_id=doc_id,
                    doc_version=self._document_version(reader, doc_id),
                    results=results,
                    channel_order=[prompt.channel.channel_id for prompt in prompts],
                    run_id=run_id,
                    evidence_by_id={block.block_id: block for block in blocks},
                )
                candidates_by_doc[doc_id] = merged
                self._raise_if_cancelled(run_id)
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
                else f"Preview Run failed: {type(error).__name__}"
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

    async def _run_channel(
        self,
        *,
        provider: ChatProvider,
        channel_prompt: CompiledChannelPrompt,
        doc_title: str,
        blocks: list[RetrievalBlock],
        window_index: int,
        existing_candidates: list[CandidateDraft] | None,
        operation: str,
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
        async with semaphore:
            try:
                result = await provider.complete(messages, max_output_tokens=8_192)
            except ChatProviderError as error:
                raise ProfileExtractionError(
                    error.code,
                    error.message,
                    503 if error.retryable else 502,
                ) from error
            candidates = self._parse_candidates(result.content, blocks)
            usage = {
                "channel_id": channel_prompt.channel.channel_id,
                "channel_name": channel_prompt.channel.name,
                "operation": operation,
                "window_index": window_index,
                "request_id": result.request_id,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            }
            return _ChannelWindowResult(
                channel_id=channel_prompt.channel.channel_id,
                window_index=window_index,
                candidates=candidates,
                usage=usage,
            )

    @staticmethod
    def _parse_candidates(
        content: str,
        blocks: list[RetrievalBlock],
    ) -> list[CandidateDraft]:
        try:
            response = CandidateExtractionResponse.model_validate_json(content)
        except Exception as error:
            raise ProfileExtractionError(
                "candidate_output_invalid",
                "candidate extractor returned invalid JSON",
            ) from error
        allowed = {block.block_id: block for block in blocks}
        for candidate in response.candidates:
            unknown = [
                block_id for block_id in candidate.source_block_ids if block_id not in allowed
            ]
            if unknown:
                raise ProfileExtractionError(
                    "candidate_source_invalid",
                    "candidate referenced a source block outside its window",
                )
            evidence = "\n".join(
                "\n".join(allowed[block_id].section_path) + "\n" + allowed[block_id].text
                for block_id in candidate.source_block_ids
            ).casefold()
            for alias in candidate.aliases:
                if normalize_name(alias) not in normalize_name(evidence):
                    raise ProfileExtractionError(
                        "candidate_alias_invalid",
                        "candidate alias does not occur in its evidence",
                    )
        return response.candidates

    @staticmethod
    def _merge_document_candidates(
        *,
        doc_id: str,
        doc_version: int,
        results: list[_ChannelWindowResult],
        channel_order: list[str],
        run_id: str,
        evidence_by_id: dict[str, RetrievalBlock],
    ) -> list[PreviewCandidateRow]:
        grouped: dict[str, _DocumentCandidate] = {}
        for result in sorted(
            results,
            key=lambda item: (
                channel_order.index(item.channel_id),
                item.window_index,
            ),
        ):
            for draft in result.candidates:
                normalized = normalize_name(draft.name)
                current = grouped.get(normalized)
                if current is None:
                    current = _DocumentCandidate(
                        name=draft.name,
                        normalized_name=normalized,
                        aliases=[],
                        summary=draft.summary,
                        summary_variants=[],
                        source_block_ids=[],
                    )
                    grouped[normalized] = current
                current.aliases = ordered_unique([*current.aliases, *draft.aliases])
                current.summary_variants = ordered_unique(
                    [*current.summary_variants, draft.summary]
                )
                current.source_block_ids = ordered_unique(
                    [*current.source_block_ids, *draft.source_block_ids]
                )
                current.channel_ids = ordered_unique([*current.channel_ids, result.channel_id])
        rows: list[PreviewCandidateRow] = []
        for ordinal, candidate in enumerate(
            sorted(grouped.values(), key=lambda item: item.normalized_name)
        ):
            aliases = [
                alias
                for alias in ordered_unique(candidate.aliases)
                if normalize_name(alias) != candidate.normalized_name
            ]
            content_hash = stable_hash(
                {
                    "name": candidate.name,
                    "aliases": aliases,
                    "summary_variants": candidate.summary_variants,
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
                    aliases_json=json_dumps(aliases),
                    summary=candidate.summary,
                    summary_variants_json=json_dumps(candidate.summary_variants),
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

    @staticmethod
    def _candidate(row: PreviewCandidateRow) -> PreviewCandidate:
        return PreviewCandidate(
            candidate_id=row.candidate_id,
            doc_id=row.doc_id,
            doc_version=row.doc_version,
            ordinal=row.ordinal,
            name=row.name,
            normalized_name=row.normalized_name,
            aliases=json_loads_string_list(row.aliases_json),
            summary=row.summary,
            summary_variants=json_loads_string_list(row.summary_variants_json),
            source_block_ids=json_loads_string_list(row.source_block_ids_json),
            evidence_blocks=json_loads_object_list(row.evidence_blocks_json),
            channel_ids=json_loads_string_list(row.channel_ids_json),
            content_hash=row.content_hash,
        )

    @staticmethod
    def _summary(row: ProfilePreviewRunRow) -> PreviewRunSummary:
        profile_name = str(json_loads_object(row.profile_config_snapshot_json).get("name", ""))
        return PreviewRunSummary(
            run_id=row.run_id,
            snapshot_id=row.snapshot_id,
            snapshot_manifest_hash=row.snapshot_manifest_hash,
            profile_id=row.profile_id,
            profile_hash=row.profile_hash,
            profile_name=profile_name,
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
