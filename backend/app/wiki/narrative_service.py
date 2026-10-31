from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.providers.chat import ChatProvider, ChatProviderError
from app.retrieval.errors import RetrievalFailure
from app.retrieval.models import RetrievalBlock
from app.retrieval.service import RetrievalReadService
from app.storage.database import StorageDatabase
from app.storage.wiki_models import (
    WikiCoverageSlotRow,
    WikiPageLinkRow,
    WikiPageParagraphRow,
    WikiPageRow,
    WikiSlotEvidenceRow,
    WikiTopicAliasRow,
    WikiTopicEvidenceRow,
    WikiTopicRow,
)
from app.wiki.build_models import (
    WikiCoverageSlotView,
    WikiNarrativeMaterializeResult,
    WikiNarrativePage,
    WikiNarrativePageDraft,
    WikiNarrativeParagraph,
    WikiNarrativeSection,
    WikiPageReadResult,
)
from app.wiki.build_service import WikiBuildService
from app.wiki.errors import WikiCompilationError
from app.wiki.narrative_prompt import (
    NARRATIVE_PAGE_PROMPT_VERSION,
    build_narrative_page_messages,
)

WIKI_NARRATIVE_COMPILER_VERSION = "wiki-narrative-page-v1"


class WikiNarrativeService:
    """为达到 materialization 阈值的 topic 生成可回源叙述页。"""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root.resolve()
        self._database = StorageDatabase(self._data_root)
        self._builds = WikiBuildService(self._data_root)
        self._retrieval = RetrievalReadService(self._data_root)
        self._root = self._data_root / "wiki" / "page-builds"

    async def materialize(
        self,
        snapshot_id: str,
        *,
        provider: ChatProvider,
        topic_ids: list[str] | None = None,
    ) -> WikiNarrativeMaterializeResult:
        """生成全部目标页面；任页失败时不写数据库页面记录。"""

        build = self._builds.active_for_snapshot(snapshot_id)
        reader = self._retrieval.bind_snapshot(snapshot_id)
        with self._database.session() as session:
            topics = list(
                session.execute(
                    select(WikiTopicRow).where(WikiTopicRow.build_id == build.build_id)
                ).scalars()
            )
            aliases = list(
                session.execute(
                    select(WikiTopicAliasRow).where(WikiTopicAliasRow.build_id == build.build_id)
                ).scalars()
            )
            evidence = list(
                session.execute(
                    select(WikiTopicEvidenceRow).where(
                        WikiTopicEvidenceRow.build_id == build.build_id
                    )
                ).scalars()
            )
            slots = list(
                session.execute(
                    select(WikiCoverageSlotRow).where(
                        WikiCoverageSlotRow.build_id == build.build_id
                    )
                ).scalars()
            )
            slot_evidence = list(
                session.execute(
                    select(WikiSlotEvidenceRow).where(
                        WikiSlotEvidenceRow.build_id == build.build_id
                    )
                ).scalars()
            )
        requested_ids = None if topic_ids is None else set(topic_ids)
        if requested_ids is not None:
            existing_ids = {topic.topic_id for topic in topics}
            missing = requested_ids - existing_ids
            if missing:
                raise WikiCompilationError(
                    "wiki_topic_not_found",
                    f"topics do not exist in active build: {', '.join(sorted(missing))}",
                )
        targets = [
            topic
            for topic in topics
            if topic.content_level == "narrative"
            and (requested_ids is None or topic.topic_id in requested_ids)
        ]
        catalog_ids = sorted(topic.topic_id for topic in topics if topic.content_level == "catalog")
        aliases_by_topic: dict[str, list[str]] = {}
        for alias in aliases:
            aliases_by_topic.setdefault(alias.topic_id, []).append(alias.alias)
        evidence_by_topic: dict[str, list[str]] = {}
        for row in evidence:
            evidence_by_topic.setdefault(row.topic_id, []).append(row.block_id)
        slot_evidence_by_id: dict[str, list[str]] = {}
        for row in slot_evidence:
            slot_evidence_by_id.setdefault(row.slot_id, []).append(row.block_id)
        slots_by_topic: dict[str, list[WikiCoverageSlotView]] = {}
        for slot in slots:
            slots_by_topic.setdefault(slot.topic_id, []).append(
                WikiCoverageSlotView(
                    slot_id=slot.slot_id,
                    label=slot.label,
                    question=slot.question,
                    source_block_ids=sorted(slot_evidence_by_id.get(slot.slot_id, [])),
                )
            )
        all_topic_ids = {topic.topic_id for topic in topics}
        pages: list[WikiNarrativePage] = []
        for topic in targets:
            topic_evidence_ids = sorted(evidence_by_topic.get(topic.topic_id, []))
            try:
                blocks = reader.get_blocks(topic_evidence_ids).blocks
            except RetrievalFailure as error:
                raise WikiCompilationError(
                    "wiki_evidence_unavailable",
                    error.message,
                ) from error
            related = _related_topics(
                topic.topic_id,
                evidence_by_topic,
                {item.topic_id: item.title for item in topics},
            )
            draft = await _generate_page(
                provider=provider,
                topic=topic,
                aliases=sorted(set(aliases_by_topic.get(topic.topic_id, []))),
                slots=slots_by_topic.get(topic.topic_id, []),
                related=related,
                blocks=blocks,
            )
            pages.append(
                _validated_page(
                    topic=topic,
                    draft=draft,
                    aliases=sorted(set(aliases_by_topic.get(topic.topic_id, []))),
                    evidence_ids=set(topic_evidence_ids),
                    available_topic_ids=all_topic_ids,
                )
            )
        if not pages:
            return WikiNarrativeMaterializeResult(
                snapshot_id=snapshot_id,
                build_id=build.build_id,
                pages=[],
                catalog_only_topic_ids=catalog_ids,
            )
        pages = self._write_page_artifacts(
            snapshot_id=snapshot_id,
            build_id=build.build_id,
            pages=pages,
        )
        self._write_page_rows(
            build_id=build.build_id,
            pages=pages,
        )
        return WikiNarrativeMaterializeResult(
            snapshot_id=snapshot_id,
            build_id=build.build_id,
            pages=pages,
            catalog_only_topic_ids=catalog_ids,
        )

    def read_page(self, snapshot_id: str, topic_id: str) -> WikiPageReadResult:
        """读取 narrative 页面；catalog topic 返回显式 catalog 页。"""

        build = self._builds.active_for_snapshot(snapshot_id)
        topic = self._builds.get_topic(snapshot_id, topic_id)
        with self._database.session() as session:
            page_row = session.execute(
                select(WikiPageRow).where(
                    WikiPageRow.build_id == build.build_id,
                    WikiPageRow.topic_id == topic_id,
                )
            ).scalar_one_or_none()
            if page_row is None:
                if topic.content_level == "catalog":
                    return WikiPageReadResult(
                        snapshot_id=snapshot_id,
                        build_id=build.build_id,
                        page=WikiNarrativePage(
                            page_id=_page_id(topic.topic_id),
                            topic_id=topic.topic_id,
                            title=topic.title,
                            aliases=[],
                            summary=topic.summary,
                            content_level="catalog",
                            sections=[],
                            related_topic_ids=[],
                        ),
                    )
                raise WikiCompilationError(
                    "wiki_page_not_materialized",
                    f"narrative page is not materialized: {topic_id}",
                )
            artifact_path = self._data_root / page_row.artifact_ref
            try:
                page = WikiNarrativePage.model_validate_json(
                    artifact_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as error:
                raise WikiCompilationError(
                    "wiki_page_invalid",
                    f"narrative page artifact is unreadable: {page_row.page_id}",
                ) from error
            if (
                page.page_id != page_row.page_id
                or page.topic_id != topic_id
                or _page_hash(page) != page_row.artifact_hash
            ):
                raise WikiCompilationError(
                    "wiki_page_invalid",
                    f"narrative page identity or hash does not match: {page_row.page_id}",
                )
        return WikiPageReadResult(
            snapshot_id=snapshot_id,
            build_id=build.build_id,
            page=page,
        )

    def page_topic_ids(self, build_id: str) -> set[str]:
        """返回一个 build 已物化叙述页的 topic IDs。"""

        with self._database.session() as session:
            return {
                row.topic_id
                for row in session.execute(
                    select(WikiPageRow).where(WikiPageRow.build_id == build_id)
                ).scalars()
            }

    def copy_pages_from_base(
        self,
        *,
        base_build_id: str,
        target_build_id: str,
        topic_ids: list[str],
    ) -> list[str]:
        """把未变 topic 的页面投影复制到新 build，不重新调用模型。"""

        requested = set(topic_ids)
        if not requested:
            return []
        with self._database.session() as session:
            target_topic_ids = {
                row.topic_id
                for row in session.execute(
                    select(WikiTopicRow).where(WikiTopicRow.build_id == target_build_id)
                ).scalars()
            }
            base_pages = list(
                session.execute(
                    select(WikiPageRow).where(
                        WikiPageRow.build_id == base_build_id,
                        WikiPageRow.topic_id.in_(requested),
                    )
                ).scalars()
            )
            selected_topic_ids = {
                page.topic_id for page in base_pages if page.topic_id in target_topic_ids
            }
            base_page_ids = {
                page.page_id for page in base_pages if page.topic_id in selected_topic_ids
            }
            paragraphs = (
                list(
                    session.execute(
                        select(WikiPageParagraphRow).where(
                            WikiPageParagraphRow.build_id == base_build_id,
                            WikiPageParagraphRow.page_id.in_(base_page_ids),
                        )
                    ).scalars()
                )
                if base_page_ids
                else []
            )
            links = (
                list(
                    session.execute(
                        select(WikiPageLinkRow).where(
                            WikiPageLinkRow.build_id == base_build_id,
                            WikiPageLinkRow.from_topic_id.in_(selected_topic_ids),
                            WikiPageLinkRow.to_topic_id.in_(target_topic_ids),
                        )
                    ).scalars()
                )
                if selected_topic_ids
                else []
            )
        copied: list[str] = []
        try:
            with self._database.session() as session:
                for page in base_pages:
                    if page.topic_id not in selected_topic_ids:
                        continue
                    artifact_path = self._data_root / page.artifact_ref
                    try:
                        artifact = WikiNarrativePage.model_validate_json(
                            artifact_path.read_text(encoding="utf-8")
                        )
                    except (OSError, ValueError) as error:
                        raise WikiCompilationError(
                            "wiki_page_invalid",
                            f"base narrative page artifact is unreadable: {page.page_id}",
                        ) from error
                    if _page_hash(artifact) != page.artifact_hash:
                        raise WikiCompilationError(
                            "wiki_page_invalid",
                            f"base narrative page hash does not match: {page.page_id}",
                        )
                    session.add(
                        WikiPageRow(
                            build_id=target_build_id,
                            page_id=page.page_id,
                            topic_id=page.topic_id,
                            content_level=page.content_level,
                            summary=page.summary,
                            artifact_ref=page.artifact_ref,
                            artifact_hash=page.artifact_hash,
                            version=page.version,
                        )
                    )
                    copied.append(page.topic_id)
                for paragraph in paragraphs:
                    session.add(
                        WikiPageParagraphRow(
                            build_id=target_build_id,
                            page_id=paragraph.page_id,
                            ordinal=paragraph.ordinal,
                            text=paragraph.text,
                            source_block_ids_json=paragraph.source_block_ids_json,
                        )
                    )
                for link in links:
                    session.add(
                        WikiPageLinkRow(
                            build_id=target_build_id,
                            from_topic_id=link.from_topic_id,
                            to_topic_id=link.to_topic_id,
                            kind=link.kind,
                            source_block_ids_json=link.source_block_ids_json,
                        )
                    )
                for topic_id in copied:
                    page_paragraphs = [
                        paragraph
                        for paragraph in paragraphs
                        if any(
                            page.topic_id == topic_id and page.page_id == paragraph.page_id
                            for page in base_pages
                        )
                    ]
                    body = "\n".join(paragraph.text for paragraph in page_paragraphs)
                    session.execute(
                        text(
                            "UPDATE wiki_topic_fts SET body = :body "
                            "WHERE build_id = :build_id AND topic_id = :topic_id"
                        ),
                        {
                            "body": body,
                            "build_id": target_build_id,
                            "topic_id": topic_id,
                        },
                    )
                session.commit()
        except SQLAlchemyError as error:
            raise WikiCompilationError(
                "wiki_page_write_failed",
                f"could not copy base narrative pages: {type(error).__name__}",
            ) from error
        return sorted(copied)

    def _write_page_artifacts(
        self,
        *,
        snapshot_id: str,
        build_id: str,
        pages: list[WikiNarrativePage],
    ) -> list[WikiNarrativePage]:
        snapshot_root = self._root / snapshot_id / build_id
        snapshot_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".b-", dir=snapshot_root))
        page_build_id = f"pb_{os.urandom(8).hex()}"
        final_root = snapshot_root / page_build_id
        try:
            materialized: list[WikiNarrativePage] = []
            for page in pages:
                artifact_ref = (
                    f"wiki/page-builds/{snapshot_id}/{build_id}/{page_build_id}/"
                    f"pages/{page.page_id}.json"
                )
                page = page.model_copy(update={"artifact_ref": artifact_ref})
                target = staging / f"pages/{page.page_id}.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    page.model_dump_json(indent=2) + "\n",
                    encoding="utf-8",
                )
                materialized.append(page)
            staging.rename(final_root)
            return materialized
        except OSError as error:
            raise WikiCompilationError(
                "wiki_page_write_failed",
                "could not write narrative page artifacts",
            ) from error
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _write_page_rows(
        self,
        *,
        build_id: str,
        pages: list[WikiNarrativePage],
    ) -> None:
        page_ids = [page.page_id for page in pages]
        try:
            with self._database.session() as session:
                if page_ids:
                    session.execute(
                        delete(WikiPageParagraphRow).where(
                            WikiPageParagraphRow.build_id == build_id,
                            WikiPageParagraphRow.page_id.in_(page_ids),
                        )
                    )
                    session.execute(
                        delete(WikiPageLinkRow).where(
                            WikiPageLinkRow.build_id == build_id,
                            WikiPageLinkRow.from_topic_id.in_([page.topic_id for page in pages]),
                        )
                    )
                    session.execute(
                        delete(WikiPageRow).where(
                            WikiPageRow.build_id == build_id,
                            WikiPageRow.page_id.in_(page_ids),
                        )
                    )
                for page in pages:
                    session.add(
                        WikiPageRow(
                            build_id=build_id,
                            page_id=page.page_id,
                            topic_id=page.topic_id,
                            content_level=page.content_level,
                            summary=page.summary,
                            artifact_ref=page.artifact_ref,
                            artifact_hash=page.artifact_hash,
                            version=1,
                        )
                    )
                    ordinal = 0
                    body_parts: list[str] = []
                    for section in page.sections:
                        body_parts.append(section.heading)
                        for paragraph in section.paragraphs:
                            session.add(
                                WikiPageParagraphRow(
                                    build_id=build_id,
                                    page_id=page.page_id,
                                    ordinal=ordinal,
                                    text=paragraph.text,
                                    source_block_ids_json=json.dumps(
                                        paragraph.source_block_ids,
                                        ensure_ascii=False,
                                    ),
                                )
                            )
                            body_parts.append(paragraph.text)
                            ordinal += 1
                    for related_id in page.related_topic_ids:
                        session.add(
                            WikiPageLinkRow(
                                build_id=build_id,
                                from_topic_id=page.topic_id,
                                to_topic_id=related_id,
                                kind="explicit",
                                source_block_ids_json="[]",
                            )
                        )
                    session.execute(
                        text(
                            "UPDATE wiki_topic_fts SET body = :body "
                            "WHERE build_id = :build_id AND topic_id = :topic_id"
                        ),
                        {
                            "body": "\n".join(body_parts),
                            "build_id": build_id,
                            "topic_id": page.topic_id,
                        },
                    )
                session.commit()
        except SQLAlchemyError as error:
            raise WikiCompilationError(
                "wiki_page_write_failed",
                f"could not persist narrative pages: {type(error).__name__}",
            ) from error


async def _generate_page(
    *,
    provider: ChatProvider,
    topic: WikiTopicRow,
    aliases: list[str],
    slots: list[WikiCoverageSlotView],
    related: list[dict[str, str]],
    blocks: list[RetrievalBlock],
) -> WikiNarrativePageDraft:
    try:
        result = await provider.complete(
            build_narrative_page_messages(
                topic_id=topic.topic_id,
                title=topic.title,
                aliases=aliases,
                coverage_slots=slots,
                related_topics=related,
                blocks=blocks,
            ),
            max_output_tokens=provider.profile.max_output_tokens,
        )
    except ChatProviderError as error:
        raise WikiCompilationError(
            "wiki_page_provider_failed",
            f"narrative page generation failed: {error.code}",
            retryable=error.retryable,
        ) from error
    if result.finish_reason not in {None, "stop"}:
        raise WikiCompilationError(
            "wiki_page_truncated",
            f"narrative page generation did not finish cleanly: {result.finish_reason}",
            retryable=True,
        )
    try:
        return WikiNarrativePageDraft.model_validate_json(result.content)
    except ValidationError:
        try:
            return WikiNarrativePageDraft.model_validate(json.loads(result.content))
        except (json.JSONDecodeError, ValidationError) as error:
            raise WikiCompilationError(
                "wiki_page_response_invalid",
                f"narrative page generation returned invalid JSON: {topic.topic_id}",
            ) from error


def _validated_page(
    *,
    topic: WikiTopicRow,
    draft: WikiNarrativePageDraft,
    aliases: list[str],
    evidence_ids: set[str],
    available_topic_ids: set[str],
) -> WikiNarrativePage:
    summary = draft.summary.strip()
    if not summary or not draft.sections:
        raise WikiCompilationError(
            "wiki_page_unsupported",
            f"narrative page is empty or unsupported: {topic.topic_id}",
        )
    sections: list[WikiNarrativeSection] = []
    for section in draft.sections:
        heading = section.heading.strip()
        if not heading:
            raise WikiCompilationError(
                "wiki_page_response_invalid",
                f"narrative page contains an empty heading: {topic.topic_id}",
            )
        paragraphs: list[WikiNarrativeParagraph] = []
        for paragraph in section.paragraphs:
            text = paragraph.text.strip()
            source_ids = list(dict.fromkeys(paragraph.source_block_ids))
            if not text or not source_ids:
                raise WikiCompilationError(
                    "wiki_page_unsupported_paragraph",
                    f"narrative page contains an unsupported paragraph: {topic.topic_id}",
                )
            if not set(source_ids).issubset(evidence_ids):
                raise WikiCompilationError(
                    "wiki_page_cross_snapshot_ref",
                    f"narrative page references blocks outside topic evidence: {topic.topic_id}",
                )
            paragraphs.append(
                WikiNarrativeParagraph(
                    text=text,
                    source_block_ids=source_ids,
                )
            )
        sections.append(WikiNarrativeSection(heading=heading, paragraphs=paragraphs))
    related = [
        topic_id
        for topic_id in dict.fromkeys(draft.related_topic_ids)
        if topic_id in available_topic_ids and topic_id != topic.topic_id
    ]
    page_id = _page_id(topic.topic_id)
    page = WikiNarrativePage(
        page_id=page_id,
        topic_id=topic.topic_id,
        title=topic.title,
        aliases=aliases,
        summary=summary,
        content_level="narrative",
        sections=sections,
        related_topic_ids=related,
    )
    page = page.model_copy(
        update={
            "artifact_ref": "",
            "artifact_hash": _page_hash(page),
        }
    )
    return page


def _related_topics(
    topic_id: str,
    evidence_by_topic: dict[str, list[str]],
    titles: dict[str, str],
) -> list[dict[str, str]]:
    source_ids = set(evidence_by_topic.get(topic_id, []))
    related = []
    for other_id, other_evidence in evidence_by_topic.items():
        if other_id == topic_id or not source_ids.intersection(other_evidence):
            continue
        related.append({"topic_id": other_id, "title": titles[other_id]})
    return sorted(related, key=lambda item: (item["title"], item["topic_id"]))


def _page_id(topic_id: str) -> str:
    digest = hashlib.sha256(f"{topic_id}\0{NARRATIVE_PAGE_PROMPT_VERSION}".encode()).hexdigest()[
        :16
    ]
    return f"wp_{digest}"


def _page_hash(page: WikiNarrativePage) -> str:
    payload = page.model_dump(
        mode="json",
        exclude={"artifact_ref", "artifact_hash"},
    )
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
