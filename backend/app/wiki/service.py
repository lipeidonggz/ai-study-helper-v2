from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from app.providers.chat import ChatProvider, ChatProviderError
from app.retrieval.errors import RetrievalFailure
from app.retrieval.models import RetrievalBlock
from app.retrieval.service import RetrievalReadService, RetrievalSnapshotReader
from app.wiki.errors import WikiCompilationError
from app.wiki.models import (
    WikiBuildManifest,
    WikiBuildPage,
    WikiCompileRequest,
    WikiCompileResult,
    WikiPageArtifact,
    WikiPageDraft,
    WikiPageSpec,
    WikiPageUsage,
    WikiProviderProfile,
)
from app.wiki.prompt import (
    WIKI_PAGE_PROMPT_HASH,
    WIKI_PAGE_PROMPT_VERSION,
    build_page_messages,
)

WIKI_COMPILER_VERSION = "llm-wiki-evidence-map-v1"
WIKI_SCHEMA_VERSION = "wiki-v1"
WIKI_BUILD_ID_PATTERN = re.compile(r"^w_[0-9a-f]{16}$")


class WikiCompiler:
    """在冻结 collection snapshot 上编译带 block 回源的不可变 Wiki artifact。"""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root.resolve()
        self._retrieval = RetrievalReadService(self._data_root)
        self._wiki_root = self._data_root / "wiki"

    async def compile(
        self,
        snapshot_id: str,
        request: WikiCompileRequest,
        *,
        provider: ChatProvider,
    ) -> WikiCompileResult:
        """编译全部页面；任一页面失败时不写出任何可读取的 build。"""

        normalized = _normalize_request(request)
        reader = self._bind_snapshot(snapshot_id)
        blocks_by_page = self._load_page_blocks(reader, normalized)
        provider_profile = WikiProviderProfile(
            provider=provider.profile.provider,
            model_id=provider.profile.model_id,
            temperature=provider.profile.temperature,
            max_output_tokens=provider.profile.max_output_tokens,
            thinking_enabled=provider.profile.thinking_enabled,
        )

        pages: list[WikiPageArtifact] = []
        usage: list[WikiPageUsage] = []
        for page, blocks in zip(normalized.pages, blocks_by_page, strict=True):
            page_id = _page_id(snapshot_id, page)
            draft, page_usage = await self._generate_page(
                page=page,
                page_id=page_id,
                blocks=blocks,
                provider=provider,
            )
            pages.append(_validated_page(page, page_id, draft, blocks))
            usage.append(page_usage)

        manifest = self._write_build(
            snapshot_id=snapshot_id,
            snapshot_manifest_hash=reader.manifest.manifest_hash,
            provider_profile=provider_profile,
            request_hash=_hash_payload(normalized.model_dump(mode="json")),
            page_specs=normalized.pages,
            pages=pages,
            usage=usage,
        )
        return WikiCompileResult(manifest=manifest, pages=pages)

    def load(self, snapshot_id: str, build_id: str) -> WikiCompileResult:
        """读取并重新校验 Wiki build、页面 hash 与每个来源 block。"""

        if not WIKI_BUILD_ID_PATTERN.fullmatch(build_id):
            raise WikiCompilationError(
                "wiki_artifact_invalid",
                f"invalid Wiki build id: {build_id}",
            )
        reader = self._bind_snapshot(snapshot_id)
        build_root = self._wiki_root / snapshot_id / build_id
        manifest_path = build_root / "manifest.json"
        try:
            manifest = WikiBuildManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            raise WikiCompilationError(
                "wiki_artifact_invalid",
                f"Wiki manifest is unreadable: {build_id}",
            ) from error
        if (
            manifest.build_id != build_id
            or manifest.snapshot_id != snapshot_id
            or manifest.snapshot_manifest_hash != reader.manifest.manifest_hash
            or manifest.manifest_hash != _manifest_hash(manifest)
        ):
            raise WikiCompilationError(
                "wiki_artifact_invalid",
                f"Wiki manifest identity or hash does not match: {build_id}",
            )

        pages: list[WikiPageArtifact] = []
        referenced_ids: list[str] = []
        page_ids: list[str] = []
        for page_summary in manifest.pages:
            artifact_path = _safe_artifact_path(build_root, page_summary.artifact_ref)
            try:
                content = artifact_path.read_bytes()
                page = WikiPageArtifact.model_validate_json(content)
            except (OSError, ValueError) as error:
                raise WikiCompilationError(
                    "wiki_artifact_invalid",
                    f"Wiki page artifact is unreadable: {page_summary.page_id}",
                ) from error
            if (
                hashlib.sha256(content).hexdigest() != page_summary.sha256
                or len(content) != page_summary.byte_length
                or page.page_id != page_summary.page_id
                or page.title != page_summary.title
                or page.page_id
                != _page_id(
                    snapshot_id,
                    WikiPageSpec(
                        title=page_summary.title,
                        source_block_ids=page_summary.candidate_block_ids,
                    ),
                )
            ):
                raise WikiCompilationError(
                    "wiki_artifact_invalid",
                    f"Wiki page identity or hash does not match: {page_summary.page_id}",
                )
            page_referenced_ids = _page_source_ids(page)
            candidates = set(page_summary.candidate_block_ids)
            if len(candidates) != len(page_summary.candidate_block_ids) or not set(
                page_referenced_ids
            ).issubset(candidates):
                raise WikiCompilationError(
                    "wiki_artifact_invalid",
                    f"Wiki page candidate identities are invalid: {page.page_id}",
                )
            expected_unreferenced = [
                block_id
                for block_id in page_summary.candidate_block_ids
                if block_id not in set(page_referenced_ids)
            ]
            if (
                len(page_summary.candidate_block_ids) != page_summary.candidate_block_count
                or len(set(page_referenced_ids)) != page_summary.referenced_block_count
                or expected_unreferenced != page_summary.unreferenced_block_ids
            ):
                raise WikiCompilationError(
                    "wiki_artifact_invalid",
                    f"Wiki page coverage metadata does not match: {page.page_id}",
                )
            pages.append(page)
            page_ids.append(page.page_id)
            referenced_ids.extend(page_referenced_ids)

        if (
            len(set(page_ids)) != len(page_ids)
            or [usage.page_id for usage in manifest.usage] != page_ids
        ):
            raise WikiCompilationError(
                "wiki_artifact_invalid",
                f"Wiki manifest page identities are inconsistent: {build_id}",
            )
        self._load_blocks(reader, referenced_ids, error_code="wiki_artifact_invalid")
        return WikiCompileResult(manifest=manifest, pages=pages)

    def _bind_snapshot(self, snapshot_id: str) -> RetrievalSnapshotReader:
        try:
            return self._retrieval.bind_snapshot(snapshot_id)
        except RetrievalFailure as error:
            raise WikiCompilationError(
                "wiki_snapshot_unavailable",
                error.message,
                retryable=error.retryable,
            ) from error

    def _load_page_blocks(
        self,
        reader: RetrievalSnapshotReader,
        request: WikiCompileRequest,
    ) -> list[list[RetrievalBlock]]:
        all_ids = [block_id for page in request.pages for block_id in page.source_block_ids]
        blocks = self._load_blocks(
            reader,
            all_ids,
            error_code="wiki_source_block_missing",
        )
        by_id = {block.block_id: block for block in blocks}
        return [[by_id[block_id] for block_id in page.source_block_ids] for page in request.pages]

    @staticmethod
    def _load_blocks(
        reader: RetrievalSnapshotReader,
        block_ids: list[str],
        *,
        error_code: str,
    ) -> list[RetrievalBlock]:
        if not block_ids:
            return []
        try:
            return reader.get_blocks(block_ids).blocks
        except RetrievalFailure as error:
            raise WikiCompilationError(
                error_code,
                error.message,
                retryable=error.retryable,
            ) from error

    @staticmethod
    async def _generate_page(
        *,
        page: WikiPageSpec,
        page_id: str,
        blocks: list[RetrievalBlock],
        provider: ChatProvider,
    ) -> tuple[WikiPageDraft, WikiPageUsage]:
        try:
            result = await provider.complete(
                build_page_messages(page, blocks),
                max_output_tokens=provider.profile.max_output_tokens,
            )
        except ChatProviderError as error:
            raise WikiCompilationError(
                "wiki_provider_failed",
                f"Wiki page generation failed: {error.code}",
                retryable=error.retryable,
            ) from error
        if result.finish_reason not in {None, "stop"}:
            raise WikiCompilationError(
                "wiki_response_truncated",
                f"Wiki page generation did not finish cleanly: {result.finish_reason}",
                retryable=True,
            )
        try:
            payload = json.loads(result.content)
            draft = WikiPageDraft.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as error:
            raise WikiCompilationError(
                "wiki_response_invalid",
                f"Wiki page generation returned an invalid JSON contract for {page_id}",
            ) from error
        return (
            draft,
            WikiPageUsage(
                page_id=page_id,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            ),
        )

    def _write_build(
        self,
        *,
        snapshot_id: str,
        snapshot_manifest_hash: str,
        provider_profile: WikiProviderProfile,
        request_hash: str,
        page_specs: list[WikiPageSpec],
        pages: list[WikiPageArtifact],
        usage: list[WikiPageUsage],
    ) -> WikiBuildManifest:
        snapshot_root = self._wiki_root / snapshot_id
        snapshot_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".b-", dir=snapshot_root))
        build_id = f"w_{os.urandom(8).hex()}"
        final_root = snapshot_root / build_id
        try:
            page_summaries: list[WikiBuildPage] = []
            for spec, page in zip(page_specs, pages, strict=True):
                artifact_ref = f"pages/{page.page_id}.json"
                artifact_path = staging / artifact_ref
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                content = (page.model_dump_json(indent=2, exclude_none=True) + "\n").encode("utf-8")
                artifact_path.write_bytes(content)
                referenced = set(_page_source_ids(page))
                page_summaries.append(
                    WikiBuildPage(
                        page_id=page.page_id,
                        title=page.title,
                        artifact_ref=artifact_ref,
                        sha256=hashlib.sha256(content).hexdigest(),
                        byte_length=len(content),
                        candidate_block_ids=spec.source_block_ids,
                        candidate_block_count=len(spec.source_block_ids),
                        referenced_block_count=len(referenced),
                        unreferenced_block_ids=[
                            block_id
                            for block_id in spec.source_block_ids
                            if block_id not in referenced
                        ],
                    )
                )
            manifest = WikiBuildManifest(
                schema_version=WIKI_SCHEMA_VERSION,
                build_id=build_id,
                snapshot_id=snapshot_id,
                snapshot_manifest_hash=snapshot_manifest_hash,
                compiler_version=WIKI_COMPILER_VERSION,
                prompt_version=WIKI_PAGE_PROMPT_VERSION,
                prompt_template_hash=WIKI_PAGE_PROMPT_HASH,
                request_hash=request_hash,
                provider=provider_profile,
                pages=page_summaries,
                usage=usage,
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
                f"could not write Wiki build for snapshot {snapshot_id}",
            ) from error
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def _normalize_request(request: WikiCompileRequest) -> WikiCompileRequest:
    titles: set[str] = set()
    pages: list[WikiPageSpec] = []
    for page in request.pages:
        title = page.title.strip()
        title_key = title.casefold()
        if title_key in titles:
            raise WikiCompilationError(
                "wiki_request_invalid",
                f"Wiki page titles must be unique: {title}",
            )
        titles.add(title_key)
        source_ids = _deduplicate_non_empty(
            page.source_block_ids,
            "source_block_ids",
        )
        pages.append(
            WikiPageSpec(
                title=title,
                aliases=_normalize_strings(page.aliases),
                coverage_questions=_normalize_strings(page.coverage_questions),
                source_block_ids=source_ids,
            )
        )
    return WikiCompileRequest(pages=pages)


def _validated_page(
    spec: WikiPageSpec,
    page_id: str,
    draft: WikiPageDraft,
    blocks: list[RetrievalBlock],
) -> WikiPageArtifact:
    allowed = {block.block_id for block in blocks}
    paragraphs = []
    for index, paragraph in enumerate(draft.paragraphs, start=1):
        text = paragraph.text.strip()
        if not text:
            raise WikiCompilationError(
                "wiki_response_invalid",
                f"Wiki page {page_id} contains an empty paragraph at position {index}",
            )
        source_ids = _deduplicate_non_empty(
            paragraph.source_block_ids,
            "source_block_ids",
        )
        out_of_scope = [block_id for block_id in source_ids if block_id not in allowed]
        if out_of_scope:
            raise WikiCompilationError(
                "wiki_source_out_of_scope",
                f"Wiki page {page_id} references blocks outside its evidence set: "
                f"{', '.join(out_of_scope)}",
            )
        paragraphs.append(
            paragraph.model_copy(
                update={
                    "text": text,
                    "source_block_ids": source_ids,
                }
            )
        )
    return WikiPageArtifact(
        page_id=page_id,
        title=spec.title,
        aliases=spec.aliases,
        coverage_questions=spec.coverage_questions,
        paragraphs=paragraphs,
    )


def _page_id(snapshot_id: str, page: WikiPageSpec) -> str:
    payload = {
        "snapshot_id": snapshot_id,
        "title": page.title.strip(),
        "source_block_ids": sorted(page.source_block_ids),
        "prompt_version": WIKI_PAGE_PROMPT_VERSION,
        "schema_version": WIKI_SCHEMA_VERSION,
    }
    digest = _hash_payload(payload)[:16]
    return f"wp_{digest}"


def _page_source_ids(page: WikiPageArtifact) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for paragraph in page.paragraphs:
        for block_id in paragraph.source_block_ids:
            if block_id not in seen:
                seen.add(block_id)
                result.append(block_id)
    return result


def _safe_artifact_path(build_root: Path, artifact_ref: str) -> Path:
    relative = Path(artifact_ref)
    if relative.is_absolute() or ".." in relative.parts:
        raise WikiCompilationError(
            "wiki_artifact_invalid",
            f"Wiki artifact path is unsafe: {artifact_ref}",
        )
    target = (build_root / relative).resolve()
    if not target.is_relative_to(build_root.resolve()):
        raise WikiCompilationError(
            "wiki_artifact_invalid",
            f"Wiki artifact path escapes build: {artifact_ref}",
        )
    return target


def _normalize_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _deduplicate_non_empty(values: list[str], field_name: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if not normalized:
            raise WikiCompilationError(
                "wiki_request_invalid",
                f"{field_name} must not contain an empty value",
            )
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    if not result:
        raise WikiCompilationError(
            "wiki_request_invalid",
            f"{field_name} must contain at least one value",
        )
    return result


def _manifest_hash(manifest: WikiBuildManifest) -> str:
    return _hash_payload(manifest.model_dump(mode="json", exclude={"manifest_hash"}))


def _hash_payload(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
