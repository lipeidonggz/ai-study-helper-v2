from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from app.ingestion.errors import IngestFailure
from app.ingestion.models import (
    ComponentVersion,
    ExtractedDocument,
    SectionRecord,
    SectionTree,
)

SECTION_BUILDER_VERSION = "2"


@dataclass(slots=True)
class _OpenSection:
    path: list[str]
    heading_level: int
    record_index: int


@dataclass(slots=True)
class _SectionDraft:
    path: list[str]
    parent_path: list[str]
    title: str
    heading_level: int | None
    depth: int
    ordinal: str
    heading_node_ordinal: int | None
    own_node_ordinals: list[int] = field(default_factory=list)


def _first_line(text: str, limit: int = 120) -> str:
    first = text.splitlines()[0] if text else ""
    return first[:limit]


def build_section_tree(
    extracted: ExtractedDocument,
    *,
    doc_id: str,
    doc_version: int,
) -> SectionTree:
    """从提取后的语义节点确定性地重建章节树，不调用模型猜章节。"""

    warnings = list(extracted.warnings)
    nodes = {node.ordinal: node for node in extracted.nodes}
    title_heading_ordinal = (
        extracted.title_source.node_ordinal if extracted.title_source.kind == "heading" else None
    )
    drafts: list[_SectionDraft] = []
    open_sections: list[_OpenSection] = []
    pending_preamble: list[int] = []
    path_segment_counts: dict[tuple[tuple[str, ...], str], int] = {}
    top_level_count = 0
    child_counts: dict[tuple[str, ...], int] = {}

    for node in extracted.nodes:
        if node.ordinal == title_heading_ordinal:
            continue
        if node.kind != "heading":
            if open_sections:
                drafts[open_sections[-1].record_index].own_node_ordinals.append(node.ordinal)
            else:
                pending_preamble.append(node.ordinal)
            continue

        section_level = node.heading_level or 1
        while open_sections and open_sections[-1].heading_level >= section_level:
            open_sections.pop()
        parent_path = open_sections[-1].path if open_sections else []
        if open_sections and section_level > open_sections[-1].heading_level + 1:
            warnings.append("heading_level_jump")

        count_key = (tuple(parent_path), node.text)
        occurrence = path_segment_counts.get(count_key, 0) + 1
        path_segment_counts[count_key] = occurrence
        segment = node.text if occurrence == 1 else f"{node.text} ({occurrence})"
        if occurrence > 1:
            warnings.append("duplicate_section_title")

        path = [*parent_path, segment]
        depth = len(open_sections) + 1 if open_sections else 1
        parent_key = tuple(parent_path)
        child_counts[parent_key] = child_counts.get(parent_key, 0) + 1
        if parent_path:
            ordinal = f"{drafts[open_sections[-1].record_index].ordinal}.{child_counts[parent_key]}"
        else:
            top_level_count += 1
            ordinal = str(top_level_count)

        draft = _SectionDraft(
            path=path,
            parent_path=parent_path,
            title=node.text,
            heading_level=node.heading_level,
            depth=depth,
            ordinal=ordinal,
            heading_node_ordinal=node.ordinal,
        )
        drafts.append(draft)
        record_index = len(drafts) - 1
        open_sections.append(
            _OpenSection(
                path=path,
                heading_level=section_level,
                record_index=record_index,
            )
        )

        if pending_preamble:
            if len(drafts) == 1:
                draft.own_node_ordinals.extend(pending_preamble)
                warnings.append("preamble_attached")
            pending_preamble.clear()

    if not drafts:
        body_ordinals = [node.ordinal for node in extracted.nodes if node.kind != "heading"]
        if not body_ordinals:
            raise IngestFailure(
                "no_extractable_content",
                "no body nodes are available for section construction",
                422,
            )
        drafts.append(
            _SectionDraft(
                path=[],
                parent_path=[],
                title=extracted.title_candidate,
                heading_level=None,
                depth=0,
                ordinal="0",
                heading_node_ordinal=None,
                own_node_ordinals=body_ordinals,
            )
        )

    assigned_ordinals = [ordinal for draft in drafts for ordinal in draft.own_node_ordinals]
    expected_body_ordinals = {node.ordinal for node in extracted.nodes if node.kind != "heading"}
    if len(assigned_ordinals) != len(set(assigned_ordinals)):
        raise IngestFailure(
            "section_integrity_failed",
            "at least one body node was assigned to multiple sections",
            500,
        )
    if set(assigned_ordinals) != expected_body_ordinals:
        raise IngestFailure(
            "section_integrity_failed",
            "section construction did not cover every body node",
            500,
        )
    paths = [tuple(draft.path) for draft in drafts]
    if len(paths) != len(set(paths)):
        raise IngestFailure("section_integrity_failed", "duplicate section paths", 500)
    for draft in drafts:
        if draft.parent_path and tuple(draft.parent_path) not in paths:
            raise IngestFailure(
                "section_integrity_failed",
                "section references a missing parent path",
                500,
            )

    records: list[SectionRecord] = []
    for index, draft in enumerate(drafts):
        first_line = ""
        for ordinal in draft.own_node_ordinals:
            first_line = _first_line(nodes[ordinal].text)
            if first_line:
                break
        if not first_line:
            prefix = tuple(draft.path)
            for candidate in drafts[index + 1 :]:
                candidate_path = tuple(candidate.path)
                if (
                    len(candidate.path) <= len(draft.path)
                    or candidate_path[: len(prefix)] != prefix
                ):
                    continue
                if candidate.own_node_ordinals:
                    first_line = _first_line(nodes[candidate.own_node_ordinals[0]].text)
                    break
        records.append(
            SectionRecord(
                path=draft.path,
                parent_path=draft.parent_path,
                title=draft.title,
                heading_level=draft.heading_level,
                depth=draft.depth,
                ordinal=draft.ordinal,
                order_index=index + 1,
                heading_node_ordinal=draft.heading_node_ordinal,
                own_node_ordinals=draft.own_node_ordinals,
                first_line=first_line,
            )
        )

    tree_payload = [
        {
            "path": record.path,
            "parent_path": record.parent_path,
            "title": record.title,
            "heading_level": record.heading_level,
            "depth": record.depth,
            "ordinal": record.ordinal,
            "order_index": record.order_index,
            "heading_node_ordinal": record.heading_node_ordinal,
            "own_node_ordinals": record.own_node_ordinals,
            "first_line": record.first_line,
        }
        for record in records
    ]
    unique_warnings = list(dict.fromkeys(warnings))
    tree_hash = hashlib.sha256(
        json.dumps(
            {
                "builder": {
                    "name": "semantic-section-builder",
                    "version": SECTION_BUILDER_VERSION,
                },
                "sections": tree_payload,
                "warnings": unique_warnings,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return SectionTree(
        doc_id=doc_id,
        doc_version=doc_version,
        builder=ComponentVersion(name="semantic-section-builder", version=SECTION_BUILDER_VERSION),
        tree_hash=tree_hash,
        sections=records,
        warnings=unique_warnings,
    )
