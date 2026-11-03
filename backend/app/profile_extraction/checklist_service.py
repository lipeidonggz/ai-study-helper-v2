from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select

from app.profile_extraction.errors import ProfileExtractionError
from app.profile_extraction.models import (
    ChecklistCreateRequest,
    ChecklistSaveRequest,
    ChecklistSuggestionPayload,
    ChecklistSuggestItem,
    ChecklistSuggestRequest,
    ChecklistSuggestResponse,
    DocumentChecklist,
    DocumentChecklistItem,
)
from app.profile_extraction.prompt_compiler import (
    build_checklist_suggestion_prompt,
    build_evidence_message,
)
from app.profile_extraction.util import (
    json_dumps,
    json_loads_object_list,
    new_identifier,
    normalize_name,
    ordered_unique,
    split_blocks,
    stable_hash,
)
from app.providers.chat import ChatMessage, ChatProvider, ChatProviderError, DeepSeekChatProvider
from app.retrieval.errors import RetrievalFailure
from app.retrieval.models import RetrievalBlock
from app.retrieval.service import RetrievalReadService, RetrievalSnapshotReader
from app.storage.credentials import CHAT_API_KEY_USERNAME, CredentialStore
from app.storage.database import StorageDatabase
from app.storage.models import utc_iso, utc_now
from app.storage.profile_extraction_models import (
    DocumentChecklistRow,
    ProfileComparisonRow,
)

CHECKLIST_GENERATOR_VERSION = "profile-checklist-generator-v1"


class ChecklistService:
    """创建、建议、编辑和冻结文档级判断清单。"""

    def __init__(
        self,
        data_root: Path,
        credentials: CredentialStore,
        *,
        chat_provider_factory: Callable[[str], ChatProvider] | None = None,
        database: StorageDatabase | None = None,
    ) -> None:
        self._database = database or StorageDatabase(data_root)
        self._retrieval = RetrievalReadService(data_root)
        self._credentials = credentials
        self._chat_provider_factory = chat_provider_factory

    def create(self, request: ChecklistCreateRequest) -> DocumentChecklist:
        """为指定 snapshot 文档创建空清单 draft。"""

        reader, doc_version, _blocks = self._resolve_document(
            request.snapshot_id,
            request.doc_id,
        )
        now = utc_now()
        row = DocumentChecklistRow(
            checklist_id=new_identifier("dc"),
            snapshot_id=reader.snapshot_id,
            doc_id=request.doc_id,
            doc_version=doc_version,
            status="draft",
            evaluation_note=request.evaluation_note,
            items_json="[]",
            checklist_hash=stable_hash(
                {
                    "snapshot_id": reader.snapshot_id,
                    "doc_id": request.doc_id,
                    "doc_version": doc_version,
                    "evaluation_note": request.evaluation_note,
                    "items": [],
                }
            ),
            created_at=now,
            updated_at=now,
        )
        with self._database.session() as session, session.begin():
            session.add(row)
            session.flush()
            return self._checklist(row)

    def save(self, checklist_id: str, request: ChecklistSaveRequest) -> DocumentChecklist:
        """全量保存未冻结清单，并校验全部 evidence 与 alias。"""

        with self._database.session() as session, session.begin():
            row = session.get(DocumentChecklistRow, checklist_id)
            if row is None:
                raise ProfileExtractionError(
                    "checklist_not_found",
                    "Document checklist does not exist",
                    404,
                )
            if row.status != "draft":
                raise ProfileExtractionError(
                    "checklist_immutable",
                    "Frozen checklists cannot be modified",
                    409,
                )
            reader, doc_version, blocks = self._resolve_document(
                row.snapshot_id,
                row.doc_id,
            )
            if doc_version != row.doc_version:
                raise ProfileExtractionError(
                    "checklist_document_changed",
                    "The document version no longer matches the checklist",
                    409,
                )
            self._validate_items(request.items, blocks)
            items = [item.model_dump(mode="json") for item in request.items]
            row.evaluation_note = request.evaluation_note
            row.items_json = json_dumps(items)
            row.checklist_hash = self._checklist_hash(row, items)
            row.updated_at = utc_now()
            session.flush()
            return self._checklist(row)

    def freeze(self, checklist_id: str) -> DocumentChecklist:
        """冻结至少包含一个项目的 draft 清单。"""

        with self._database.session() as session, session.begin():
            row = session.get(DocumentChecklistRow, checklist_id)
            if row is None:
                raise ProfileExtractionError(
                    "checklist_not_found",
                    "Document checklist does not exist",
                    404,
                )
            if row.status != "draft":
                raise ProfileExtractionError(
                    "checklist_already_frozen",
                    "Document checklist is already frozen",
                    409,
                )
            items = json_loads_object_list(row.items_json)
            parsed_items = [DocumentChecklistItem.model_validate(item) for item in items]
            if not parsed_items:
                raise ProfileExtractionError(
                    "checklist_empty",
                    "A checklist must contain at least one item before freezing",
                    422,
                )
            _reader, _doc_version, blocks = self._resolve_document(
                row.snapshot_id,
                row.doc_id,
            )
            self._validate_items(parsed_items, blocks)
            frozen_at = utc_now()
            row.status = "frozen"
            row.frozen_at = frozen_at
            row.updated_at = frozen_at
            row.checklist_hash = self._checklist_hash(
                row,
                [item.model_dump(mode="json") for item in parsed_items],
            )
            session.flush()
            return self._checklist(row)

    def get(self, checklist_id: str) -> DocumentChecklist:
        """读取清单。"""

        with self._database.session() as session:
            row = session.get(DocumentChecklistRow, checklist_id)
            if row is None:
                raise ProfileExtractionError(
                    "checklist_not_found",
                    "Document checklist does not exist",
                    404,
                )
            return self._checklist(row)

    def list(self) -> list[DocumentChecklist]:
        """按创建时间倒序返回清单历史。"""

        with self._database.session() as session:
            rows = session.scalars(
                select(DocumentChecklistRow).order_by(DocumentChecklistRow.created_at.desc())
            ).all()
            return [self._checklist(row) for row in rows]

    def document_blocks(
        self,
        snapshot_id: str,
        doc_id: str,
    ) -> list[dict[str, object]]:
        """按显式 snapshot 返回清单编辑所需的完整证据块。"""

        _reader, _doc_version, blocks = self._resolve_document(snapshot_id, doc_id)
        return [
            {
                "block_id": block.block_id,
                "section_path": block.section_path,
                "text": block.text,
            }
            for block in blocks
        ]

    def delete(self, checklist_id: str) -> None:
        """删除未冻结且未被比较引用的清单。"""

        with self._database.session() as session, session.begin():
            row = session.get(DocumentChecklistRow, checklist_id)
            if row is None:
                raise ProfileExtractionError(
                    "checklist_not_found",
                    "Document checklist does not exist",
                    404,
                )
            if row.status != "draft":
                raise ProfileExtractionError(
                    "checklist_delete_forbidden",
                    "Frozen checklists cannot be deleted",
                    409,
                )
            comparisons = session.scalars(select(ProfileComparisonRow)).all()
            if any(checklist_id in comparison.checklist_refs_json for comparison in comparisons):
                raise ProfileExtractionError(
                    "checklist_referenced",
                    "Checklist is referenced by a Profile comparison",
                    409,
                )
            session.delete(row)

    async def suggest(
        self,
        request: ChecklistSuggestRequest,
    ) -> ChecklistSuggestResponse:
        """用整篇文档生成非 Profile 专属的建议清单，不自动保存。"""

        key = self._credentials.get(CHAT_API_KEY_USERNAME)
        if not key:
            raise ProfileExtractionError(
                "chat_api_key_missing",
                "configure a chat API key before generating a checklist",
                409,
            )
        provider = (
            self._chat_provider_factory(key)
            if self._chat_provider_factory is not None
            else DeepSeekChatProvider(key)
        )
        reader, _doc_version, blocks = self._resolve_document(
            request.snapshot_id,
            request.doc_id,
        )
        suggestions: list[ChecklistSuggestItem] = []
        request_ids: list[str] = []
        system_prompt = build_checklist_suggestion_prompt()
        for window_index, window in enumerate(split_blocks(blocks)):
            message = build_evidence_message(
                doc_title=blocks[0].doc_title,
                source_label=blocks[0].source_label,
                blocks=window,
            )
            try:
                result = await provider.complete(
                    [
                        ChatMessage(role="system", content=system_prompt),
                        ChatMessage(
                            role="user",
                            content=json_dumps(
                                {
                                    "evaluation_note": request.evaluation_note,
                                    "window_index": window_index,
                                    "evidence": json.loads(message),
                                }
                            ),
                        ),
                    ],
                    max_output_tokens=8_192,
                )
            except ChatProviderError as error:
                raise ProfileExtractionError(
                    error.code,
                    error.message,
                    503 if error.retryable else 502,
                ) from error
            try:
                payload = ChecklistSuggestionPayload.model_validate_json(result.content)
            except Exception as error:
                raise ProfileExtractionError(
                    "checklist_suggestion_invalid",
                    "chat provider returned an invalid checklist suggestion",
                    502,
                ) from error
            self._validate_suggestions(payload.items, window)
            suggestions.extend(payload.items)
            request_ids.append(result.request_id)
        return ChecklistSuggestResponse(
            items=self._merge_suggestions(suggestions),
            request_ids=request_ids,
        )

    def _resolve_document(
        self,
        snapshot_id: str | None,
        doc_id: str,
    ) -> tuple[RetrievalSnapshotReader, int, list[RetrievalBlock]]:
        try:
            reader = (
                self._retrieval.bind_snapshot(snapshot_id)
                if snapshot_id is not None
                else self._retrieval.bind_current()
            )
        except RetrievalFailure as error:
            raise ProfileExtractionError(
                error.code,
                error.message,
                409,
            ) from error
        summaries = [
            document for document in reader.manifest.documents if document.doc_id == doc_id
        ]
        if not summaries:
            raise ProfileExtractionError(
                "checklist_document_not_found",
                "Document is not in the selected snapshot",
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
                "checklist_document_empty",
                "Document contains no extractable blocks",
                422,
            )
        return reader, summaries[0].doc_version, blocks

    @staticmethod
    def _validate_items(
        items: list[DocumentChecklistItem],
        blocks: list[RetrievalBlock],
    ) -> None:
        if len({item.item_id for item in items}) != len(items):
            raise ProfileExtractionError(
                "checklist_item_id_duplicate",
                "Checklist item IDs must be unique",
                422,
            )
        allowed = {block.block_id: block for block in blocks}
        for item in items:
            unknown = [block_id for block_id in item.source_block_ids if block_id not in allowed]
            if unknown:
                raise ProfileExtractionError(
                    "checklist_source_invalid",
                    "Checklist item references a source block outside its document",
                    422,
                )
            evidence = "\n".join(
                "\n".join(allowed[block_id].section_path) + "\n" + allowed[block_id].text
                for block_id in item.source_block_ids
            ).casefold()
            for alias in item.aliases:
                if normalize_name(alias) not in normalize_name(evidence):
                    raise ProfileExtractionError(
                        "checklist_alias_invalid",
                        "Checklist alias does not occur in its evidence",
                        422,
                    )

    @staticmethod
    def _validate_suggestions(
        items: list[ChecklistSuggestItem],
        blocks: list[RetrievalBlock],
    ) -> None:
        allowed = {block.block_id: block for block in blocks}
        for item in items:
            unknown = [block_id for block_id in item.source_block_ids if block_id not in allowed]
            if unknown:
                raise ProfileExtractionError(
                    "checklist_source_invalid",
                    "Suggested checklist item references a source block outside its window",
                    502,
                )
            evidence = "\n".join(
                "\n".join(allowed[block_id].section_path) + "\n" + allowed[block_id].text
                for block_id in item.source_block_ids
            ).casefold()
            for alias in item.aliases:
                if normalize_name(alias) not in normalize_name(evidence):
                    raise ProfileExtractionError(
                        "checklist_alias_invalid",
                        "Suggested checklist alias does not occur in its evidence",
                        502,
                    )

    @staticmethod
    def _merge_suggestions(
        suggestions: list[ChecklistSuggestItem],
    ) -> list[ChecklistSuggestItem]:
        priority_order = {"must": 0, "should": 1, "nice_to_have": 2, "noise": 3}
        grouped: dict[str, ChecklistSuggestItem] = {}
        for item in suggestions:
            key = normalize_name(item.name)
            current = grouped.get(key)
            if current is None:
                grouped[key] = item.model_copy(deep=True)
                continue
            if priority_order[item.priority] < priority_order[current.priority]:
                current.priority = item.priority
            current.aliases = ordered_unique([*current.aliases, *item.aliases])
            current.source_block_ids = ordered_unique(
                [*current.source_block_ids, *item.source_block_ids]
            )
        return sorted(grouped.values(), key=lambda item: normalize_name(item.name))

    @staticmethod
    def _checklist_hash(
        row: DocumentChecklistRow,
        items: list[dict[str, object]],
    ) -> str:
        return stable_hash(
            {
                "snapshot_id": row.snapshot_id,
                "doc_id": row.doc_id,
                "doc_version": row.doc_version,
                "evaluation_note": row.evaluation_note,
                "items": items,
            }
        )

    @staticmethod
    def _checklist(row: DocumentChecklistRow) -> DocumentChecklist:
        return DocumentChecklist(
            checklist_id=row.checklist_id,
            snapshot_id=row.snapshot_id,
            doc_id=row.doc_id,
            doc_version=row.doc_version,
            status=row.status,  # type: ignore[arg-type]
            evaluation_note=row.evaluation_note,
            items=[
                DocumentChecklistItem.model_validate(item)
                for item in json_loads_object_list(row.items_json)
            ],
            checklist_hash=row.checklist_hash,
            created_at=utc_iso(row.created_at),
            updated_at=utc_iso(row.updated_at),
            frozen_at=utc_iso(row.frozen_at) if row.frozen_at else None,
        )
