from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from app.collections.models import SnapshotDocument
from app.providers.chat import ChatProvider, ChatProviderError
from app.retrieval.errors import RetrievalFailure
from app.retrieval.models import RetrievalBlock
from app.retrieval.service import RetrievalReadService, RetrievalSnapshotReader
from app.wiki.errors import WikiCompilationError
from app.wiki.models import WikiProviderProfile
from app.wiki.topic_models import (
    TopicCandidateDraft,
    TopicCoverageSlot,
    TopicExtractionResponse,
    TopicMapDocument,
    TopicMapUsage,
    WikiCandidate,
    WikiTopicMapManifest,
    WikiTopicMapResult,
)
from app.wiki.topic_prompt import (
    TOPIC_MAP_PROMPT_HASH,
    TOPIC_MAP_PROMPT_VERSION,
    build_topic_map_messages,
)

TOPIC_MAP_COMPILER_VERSION = "snapshot-topic-map-v1"
TOPIC_MAP_ID_PATTERN = re.compile(r"^tm_[0-9a-f]{16}$")
DEFAULT_BLOCK_WINDOW_SIZE = 16
DEFAULT_BLOCK_WINDOW_CHARS = 12_000


class TopicMapCompiler:
    """把一个冻结 collection snapshot 编译成文档局部主题候选地图。"""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root.resolve()
        self._retrieval = RetrievalReadService(self._data_root)
        self._root = self._data_root / "wiki" / "topic-maps"

    async def compile(
        self,
        snapshot_id: str,
        *,
        provider: ChatProvider,
        granularity: Literal["focused", "standard", "exhaustive"] = "standard",
        block_window_size: int = DEFAULT_BLOCK_WINDOW_SIZE,
        block_window_chars: int = DEFAULT_BLOCK_WINDOW_CHARS,
        map_parallel: int = 1,
        document_ids: list[str] | None = None,
    ) -> WikiTopicMapResult:
        """编译全部或指定文档；任一窗口失败时不发布部分 map。"""

        if granularity not in {"focused", "standard", "exhaustive"}:
            raise WikiCompilationError(
                "wiki_request_invalid",
                f"unsupported topic extraction granularity: {granularity}",
            )
        if block_window_size <= 0 or block_window_chars <= 0 or map_parallel <= 0:
            raise WikiCompilationError(
                "wiki_request_invalid",
                "topic map window size and character budget must be positive",
            )
        reader = self._bind_snapshot(snapshot_id)
        selected_documents = self._select_documents(reader, document_ids)
        provider_profile = _provider_profile(provider)
        documents: list[TopicMapDocument] = []
        usage: list[TopicMapUsage] = []
        for document in selected_documents:
            try:
                document_blocks = reader.list_blocks(document.doc_id).blocks
            except RetrievalFailure as error:
                raise WikiCompilationError(
                    "wiki_snapshot_unavailable",
                    error.message,
                    retryable=error.retryable,
                ) from error
            document_map, document_usage = await self._compile_document(
                snapshot_id=snapshot_id,
                document=document,
                blocks=document_blocks,
                provider=provider,
                granularity=granularity,
                block_window_size=block_window_size,
                block_window_chars=block_window_chars,
                map_parallel=map_parallel,
            )
            documents.append(document_map)
            usage.extend(document_usage)

        expected_total_blocks = (
            reader.manifest.totals.blocks
            if document_ids is None
            else sum(document.block_count for document in selected_documents)
        )
        actual_total_blocks = sum(len(document.processed_block_ids) for document in documents)
        if actual_total_blocks != expected_total_blocks:
            raise WikiCompilationError(
                "topic_map_block_gap",
                f"topic map covered {actual_total_blocks} blocks, expected {expected_total_blocks}",
            )
        manifest = self._write_map(
            reader=reader,
            provider_profile=provider_profile,
            granularity=granularity,
            block_window_size=block_window_size,
            block_window_chars=block_window_chars,
            documents=documents,
            usage=usage,
        )
        return WikiTopicMapResult(manifest=manifest, documents=documents)

    def load(self, snapshot_id: str, map_id: str) -> WikiTopicMapResult:
        """读取并重新校验一个 topic map。"""

        if not TOPIC_MAP_ID_PATTERN.fullmatch(map_id):
            raise WikiCompilationError("wiki_artifact_invalid", f"invalid topic map id: {map_id}")
        manifest_path = self._root / snapshot_id / map_id / "manifest.json"
        try:
            manifest = WikiTopicMapManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            raise WikiCompilationError(
                "wiki_artifact_invalid",
                f"topic map is unreadable: {map_id}",
            ) from error
        if (
            manifest.map_id != map_id
            or manifest.snapshot_id != snapshot_id
            or manifest.manifest_hash != _manifest_hash(manifest)
        ):
            raise WikiCompilationError(
                "wiki_artifact_invalid",
                f"topic map identity or hash does not match: {map_id}",
            )
        return WikiTopicMapResult(manifest=manifest, documents=manifest.documents)

    def adopt_documents(
        self,
        snapshot_id: str,
        *,
        source_snapshot_id: str,
        source_map_id: str,
        document_ids: list[str],
    ) -> WikiTopicMapResult | None:
        """把未变文档的既有 topic map 重新绑定到新 snapshot，不调用 LLM。"""

        requested = list(dict.fromkeys(document_ids))
        if not requested:
            return None
        source = self.load(source_snapshot_id, source_map_id)
        source_docs = {document.doc_id: document for document in source.documents}
        missing = [doc_id for doc_id in requested if doc_id not in source_docs]
        if missing:
            raise WikiCompilationError(
                "wiki_artifact_invalid",
                f"source topic map lacks requested documents: {', '.join(missing)}",
            )
        reader = self._bind_snapshot(snapshot_id)
        selected_documents: list[TopicMapDocument] = []
        for doc_id in requested:
            source_document = source_docs[doc_id]
            summary = next(
                (document for document in reader.manifest.documents if document.doc_id == doc_id),
                None,
            )
            if summary is None or summary.doc_version != source_document.doc_version:
                raise WikiCompilationError(
                    "wiki_snapshot_mismatch",
                    f"document version changed while adopting topic map: {doc_id}",
                )
            try:
                current_blocks = reader.list_blocks(doc_id).blocks
            except RetrievalFailure as error:
                raise WikiCompilationError(
                    "wiki_snapshot_unavailable",
                    error.message,
                ) from error
            if [block.block_id for block in current_blocks] != source_document.processed_block_ids:
                raise WikiCompilationError(
                    "wiki_snapshot_mismatch",
                    f"document blocks changed while adopting topic map: {doc_id}",
                )
            selected_documents.append(source_document)
        manifest = self._write_map(
            reader=reader,
            provider_profile=source.manifest.provider,
            granularity=source.manifest.granularity,
            block_window_size=source.manifest.block_window_size,
            block_window_chars=source.manifest.block_window_chars,
            documents=selected_documents,
            usage=[],
            reused_from_map_id=source_map_id,
        )
        return WikiTopicMapResult(manifest=manifest, documents=selected_documents)

    async def _compile_document(
        self,
        *,
        snapshot_id: str,
        document: SnapshotDocument,
        blocks: list[RetrievalBlock],
        provider: ChatProvider,
        granularity: Literal["focused", "standard", "exhaustive"],
        block_window_size: int,
        block_window_chars: int,
        map_parallel: int,
    ) -> tuple[TopicMapDocument, list[TopicMapUsage]]:
        doc_id = document.doc_id
        doc_version = document.doc_version
        windows = _split_blocks(
            blocks,
            max_blocks=block_window_size,
            max_chars=block_window_chars,
        )
        semaphore = asyncio.Semaphore(map_parallel)

        async def generate(
            window_index: int,
            window: list[RetrievalBlock],
        ) -> tuple[int, TopicExtractionResponse, TopicMapUsage]:
            async with semaphore:
                draft, window_usage = await _generate_window(
                    provider=provider,
                    doc_id=doc_id,
                    doc_title=document.title,
                    source_label=document.source_label,
                    granularity=granularity,
                    window_index=window_index,
                    blocks=window,
                )
                return window_index, draft, window_usage

        generated = await asyncio.gather(
            *(generate(index, window) for index, window in enumerate(windows))
        )
        merged: dict[str, WikiCandidate] = {}
        usage: list[TopicMapUsage] = []
        for window_index, draft, window_usage in sorted(
            generated,
            key=lambda item: item[0],
        ):
            window = windows[window_index]
            usage.append(window_usage)
            for candidate in draft.candidates:
                normalized = _validated_candidate(
                    snapshot_id=snapshot_id,
                    doc_id=doc_id,
                    doc_version=doc_version,
                    doc_title=document.title,
                    candidate=candidate,
                    allowed_blocks={block.block_id for block in window},
                )
                current = merged.get(normalized.candidate_id)
                merged[normalized.candidate_id] = (
                    normalized if current is None else _merge_local_candidate(current, normalized)
                )
        ordered_candidates = sorted(
            merged.values(),
            key=lambda candidate: (candidate.normalized_name, candidate.candidate_id),
        )
        return (
            TopicMapDocument(
                doc_id=doc_id,
                doc_version=doc_version,
                doc_title=document.title,
                source_label=document.source_label,
                windows=len(windows),
                processed_block_ids=[block.block_id for block in blocks],
                candidates=ordered_candidates,
            ),
            usage,
        )

    def _bind_snapshot(self, snapshot_id: str) -> RetrievalSnapshotReader:
        try:
            return self._retrieval.bind_snapshot(snapshot_id)
        except RetrievalFailure as error:
            raise WikiCompilationError(
                "wiki_snapshot_unavailable",
                error.message,
                retryable=error.retryable,
            ) from error

    @staticmethod
    def _select_documents(
        reader: RetrievalSnapshotReader,
        document_ids: list[str] | None,
    ) -> list[SnapshotDocument]:
        documents = reader.manifest.documents
        if document_ids is None:
            return list(documents)
        requested = list(dict.fromkeys(document_ids))
        by_id = {document.doc_id: document for document in documents}
        missing = [doc_id for doc_id in requested if doc_id not in by_id]
        if missing:
            raise WikiCompilationError(
                "scope_not_found",
                f"documents do not exist in the fixed snapshot: {', '.join(missing)}",
            )
        return [by_id[doc_id] for doc_id in requested]

    def _write_map(
        self,
        *,
        reader: RetrievalSnapshotReader,
        provider_profile: WikiProviderProfile,
        granularity: Literal["focused", "standard", "exhaustive"],
        block_window_size: int,
        block_window_chars: int,
        documents: list[TopicMapDocument],
        usage: list[TopicMapUsage],
        reused_from_map_id: str | None = None,
    ) -> WikiTopicMapManifest:
        snapshot_id = reader.snapshot_id
        snapshot_root = self._root / snapshot_id
        snapshot_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".b-", dir=snapshot_root))
        map_id = f"tm_{os.urandom(8).hex()}"
        final_root = snapshot_root / map_id
        try:
            manifest = WikiTopicMapManifest(
                map_id=map_id,
                snapshot_id=snapshot_id,
                snapshot_manifest_hash=reader.manifest.manifest_hash,
                compiler_version=TOPIC_MAP_COMPILER_VERSION,
                prompt_version=TOPIC_MAP_PROMPT_VERSION,
                prompt_template_hash=TOPIC_MAP_PROMPT_HASH,
                provider=provider_profile,
                granularity=granularity,
                block_window_size=block_window_size,
                block_window_chars=block_window_chars,
                reused_from_map_id=reused_from_map_id,
                documents=documents,
                usage=usage,
                candidate_count=sum(len(document.candidates) for document in documents),
                block_count=sum(len(document.processed_block_ids) for document in documents),
                created_at=datetime.now(UTC).isoformat(),
            )
            manifest = manifest.model_copy(update={"manifest_hash": _manifest_hash(manifest)})
            (staging / "manifest.json").write_text(
                manifest.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
            staging.rename(final_root)
            return manifest
        except OSError as error:
            raise WikiCompilationError(
                "wiki_artifact_write_failed",
                f"could not write topic map for snapshot {snapshot_id}",
            ) from error
        finally:
            if staging.exists():
                shutil.rmtree(staging)


async def _generate_window(
    *,
    provider: ChatProvider,
    doc_id: str,
    doc_title: str,
    source_label: str,
    granularity: str,
    window_index: int,
    blocks: list[RetrievalBlock],
) -> tuple[TopicExtractionResponse, TopicMapUsage]:
    try:
        result = await provider.complete(
            build_topic_map_messages(
                doc_title=doc_title,
                source_label=source_label,
                granularity=granularity,
                blocks=blocks,
            ),
            max_output_tokens=provider.profile.max_output_tokens,
        )
    except ChatProviderError as error:
        raise WikiCompilationError(
            "topic_map_provider_failed",
            f"topic map generation failed: {error.code}",
            retryable=error.retryable,
        ) from error
    if result.finish_reason not in {None, "stop"}:
        raise WikiCompilationError(
            "topic_map_response_truncated",
            f"topic map generation did not finish cleanly: {result.finish_reason}",
            retryable=True,
        )
    try:
        direct = TopicExtractionResponse.model_validate_json(result.content)
    except ValidationError:
        try:
            payload = json.loads(result.content)
            direct = TopicExtractionResponse.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as error:
            raise WikiCompilationError(
                "topic_map_response_invalid",
                f"topic map generation returned an invalid JSON contract for {doc_id}",
            ) from error
    return (
        direct,
        TopicMapUsage(
            doc_id=doc_id,
            window_index=window_index,
            request_id=result.request_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        ),
    )


def _validated_candidate(
    *,
    snapshot_id: str,
    doc_id: str,
    doc_version: int,
    doc_title: str,
    candidate: TopicCandidateDraft,
    allowed_blocks: set[str],
) -> WikiCandidate:
    name = candidate.name.strip()
    summary = candidate.summary.strip()
    if not name or not summary:
        raise WikiCompilationError(
            "topic_map_response_invalid",
            "candidate name and summary must not be empty",
        )
    source_ids = _deduplicate_non_empty(candidate.source_block_ids)
    out_of_scope = [block_id for block_id in source_ids if block_id not in allowed_blocks]
    if out_of_scope:
        raise WikiCompilationError(
            "topic_map_source_out_of_scope",
            f"candidate {name} references blocks outside its window: {', '.join(out_of_scope)}",
        )
    semantic_tags = [
        tag
        for tag in _normalize_strings(candidate.semantic_tags)
        if normalize_topic_name(tag) not in {"topic", "主题"}
    ]
    normalized_name = normalize_topic_name(name)
    candidate_id = _candidate_id(snapshot_id, doc_id, normalized_name)
    coverage_slots: list[TopicCoverageSlot] = []
    for slot in candidate.coverage_slots:
        label = slot.label.strip()
        question = slot.question.strip()
        slot_source_ids = _deduplicate_non_empty(slot.source_block_ids)
        if not label or not question:
            raise WikiCompilationError(
                "topic_map_response_invalid",
                f"candidate {name} contains an empty coverage slot",
            )
        slot_out_of_scope = [
            block_id for block_id in slot_source_ids if block_id not in allowed_blocks
        ]
        if slot_out_of_scope:
            raise WikiCompilationError(
                "topic_map_source_out_of_scope",
                f"coverage slot for {name} references blocks outside its window: "
                f"{', '.join(slot_out_of_scope)}",
            )
        coverage_slots.append(
            TopicCoverageSlot(
                slot_id=_slot_id(candidate_id, label, question),
                label=label,
                question=question,
                source_block_ids=slot_source_ids,
            )
        )
    return WikiCandidate(
        candidate_id=candidate_id,
        doc_id=doc_id,
        doc_version=doc_version,
        doc_title=doc_title,
        name=name,
        normalized_name=normalized_name,
        aliases=_normalize_strings(candidate.aliases),
        summary=summary,
        semantic_tags=semantic_tags,
        source_block_ids=source_ids,
        coverage_slots=coverage_slots,
    )


def _merge_local_candidate(first: WikiCandidate, second: WikiCandidate) -> WikiCandidate:
    slots_by_id = {slot.slot_id: slot for slot in first.coverage_slots}
    for slot in second.coverage_slots:
        existing = slots_by_id.get(slot.slot_id)
        if existing is None:
            slots_by_id[slot.slot_id] = slot
        else:
            slots_by_id[slot.slot_id] = existing.model_copy(
                update={
                    "source_block_ids": _ordered_union(
                        existing.source_block_ids,
                        slot.source_block_ids,
                    )
                }
            )
    return first.model_copy(
        update={
            "aliases": _ordered_union(first.aliases, second.aliases),
            "semantic_tags": _ordered_union(first.semantic_tags, second.semantic_tags),
            "source_block_ids": _ordered_union(
                first.source_block_ids,
                second.source_block_ids,
            ),
            "coverage_slots": sorted(slots_by_id.values(), key=lambda slot: slot.slot_id),
        }
    )


def _split_blocks(
    blocks: list[RetrievalBlock],
    *,
    max_blocks: int,
    max_chars: int,
) -> list[list[RetrievalBlock]]:
    if not blocks:
        return []
    windows: list[list[RetrievalBlock]] = []
    current: list[RetrievalBlock] = []
    current_chars = 0
    for block in blocks:
        block_chars = len(block.text)
        if current and (len(current) >= max_blocks or current_chars + block_chars > max_chars):
            windows.append(current)
            current = []
            current_chars = 0
        current.append(block)
        current_chars += block_chars
    if current:
        windows.append(current)
    return windows


def normalize_topic_name(value: str) -> str:
    """把主题名称归一为身份比较用的稳定形式。"""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _candidate_id(snapshot_id: str, doc_id: str, normalized_name: str) -> str:
    digest = hashlib.sha256(f"{snapshot_id}\0{doc_id}\0{normalized_name}".encode()).hexdigest()[:16]
    return f"wc_{digest}"


def _slot_id(candidate_id: str, label: str, question: str) -> str:
    payload = json.dumps(
        {
            "candidate_id": candidate_id,
            "label": normalize_topic_name(label),
            "question": normalize_topic_name(question),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"ws_{hashlib.sha256(payload).hexdigest()[:16]}"


def _normalize_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        key = normalize_topic_name(normalized)
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def _deduplicate_non_empty(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if not normalized:
            raise WikiCompilationError(
                "topic_map_response_invalid",
                "block id must not be empty",
            )
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    if not result:
        raise WikiCompilationError(
            "topic_map_candidate_unsupported",
            "candidate or coverage slot has no source block",
        )
    return result


def _ordered_union(first: list[str], second: list[str]) -> list[str]:
    result = list(first)
    seen = set(first)
    for value in second:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _provider_profile(provider: ChatProvider) -> WikiProviderProfile:
    profile = provider.profile
    return WikiProviderProfile(
        provider=profile.provider,
        model_id=profile.model_id,
        temperature=profile.temperature,
        max_output_tokens=profile.max_output_tokens,
        thinking_enabled=profile.thinking_enabled,
    )


def _manifest_hash(manifest: WikiTopicMapManifest) -> str:
    payload = manifest.model_dump(mode="json", exclude={"manifest_hash"})
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
