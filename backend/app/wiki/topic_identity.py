from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path

from pydantic import ValidationError

from app.providers.chat import ChatProvider, ChatProviderError
from app.wiki.errors import WikiCompilationError
from app.wiki.models import WikiProviderProfile
from app.wiki.topic_map import TopicMapCompiler, normalize_topic_name
from app.wiki.topic_models import (
    TopicCoverageSlot,
    TopicIdentityCompareGroup,
    TopicIdentityGroup,
    TopicIdentityMergeResponse,
    WikiCandidate,
    WikiTopicLedgerEntry,
    WikiTopicLedgerManifest,
    WikiTopicLedgerResult,
    WikiTopicMapResult,
)
from app.wiki.topic_prompt import (
    TOPIC_IDENTITY_PROMPT_HASH,
    TOPIC_IDENTITY_PROMPT_VERSION,
    build_topic_identity_messages,
)

TOPIC_IDENTITY_RESOLVER_VERSION = "wiki-topic-identity-v1"
TOPIC_LEDGER_ID_PATTERN = re.compile(r"^tl_[0-9a-f]{16}$")
IDENTITY_BATCH_SIZE = 20
IDENTITY_NEIGHBOR_LIMIT = 5
IDENTITY_MIN_SIMILARITY = 0.55


@dataclass(slots=True)
class _CandidateRef:
    map_id: str
    candidate: WikiCandidate


@dataclass(slots=True)
class _CandidateGroup:
    group_id: str
    refs: list[_CandidateRef]
    names: list[str]
    aliases: list[str]
    summaries: list[str]
    evidence_count: int
    base_topic_id: str | None = None


class TopicIdentityCompiler:
    """把多个文档局部候选归一为稳定、可审计的 Wiki topic ledger。"""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root.resolve()
        self._topic_maps = TopicMapCompiler(self._data_root)
        self._root = self._data_root / "wiki" / "topic-ledgers"

    async def compile(
        self,
        snapshot_id: str,
        *,
        topic_map_ids: list[str],
        provider: ChatProvider,
        base_ledger_id: str | None = None,
    ) -> WikiTopicLedgerResult:
        """合并 topic maps；任何非法 merge 或丢失候选均使整次失败。"""

        if not topic_map_ids:
            raise WikiCompilationError(
                "wiki_request_invalid",
                "at least one topic map id is required",
            )
        maps = [self._topic_maps.load(snapshot_id, map_id) for map_id in topic_map_ids]
        snapshot_hashes = {item.manifest.snapshot_manifest_hash for item in maps}
        if len(snapshot_hashes) != 1:
            raise WikiCompilationError(
                "wiki_snapshot_mismatch",
                "topic maps belong to different snapshot manifests",
            )
        base = self.load(base_ledger_id) if base_ledger_id is not None else None
        refs = _candidate_refs(maps)
        groups, _ = _group_by_exact_identity(refs)
        groups = _merge_groups_for_base_identity(groups, base)
        groups, unresolved_merge_count = await _resolve_fuzzy_groups(groups, provider)
        topics = [_ledger_entry(group) for group in sorted(groups, key=lambda item: item.group_id)]
        _verify_candidate_coverage(refs, topics)
        manifest = self._write_ledger(
            snapshot_id=snapshot_id,
            snapshot_manifest_hash=next(iter(snapshot_hashes)),
            topic_map_ids=topic_map_ids,
            base_ledger_id=base_ledger_id,
            provider_profile=_provider_profile(provider),
            topics=topics,
            candidate_count=len(refs),
            unresolved_merge_count=unresolved_merge_count,
        )
        return WikiTopicLedgerResult(manifest=manifest, topics=topics)

    def load(self, ledger_id: str) -> WikiTopicLedgerResult:
        """按 ID 读取 ledger；base ledger 只提供稳定身份，不提供当前证据。"""

        if not TOPIC_LEDGER_ID_PATTERN.fullmatch(ledger_id):
            raise WikiCompilationError(
                "wiki_artifact_invalid",
                f"invalid topic ledger id: {ledger_id}",
            )
        matches = list(self._root.glob(f"*/{ledger_id}/manifest.json"))
        if len(matches) != 1:
            raise WikiCompilationError(
                "wiki_artifact_invalid",
                f"topic ledger does not exist uniquely: {ledger_id}",
            )
        try:
            manifest = WikiTopicLedgerManifest.model_validate_json(
                matches[0].read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            raise WikiCompilationError(
                "wiki_artifact_invalid",
                f"topic ledger is unreadable: {ledger_id}",
            ) from error
        if manifest.ledger_id != ledger_id or manifest.manifest_hash != _manifest_hash(manifest):
            raise WikiCompilationError(
                "wiki_artifact_invalid",
                f"topic ledger identity or hash does not match: {ledger_id}",
            )
        return WikiTopicLedgerResult(manifest=manifest, topics=manifest.topics)

    def _write_ledger(
        self,
        *,
        snapshot_id: str,
        snapshot_manifest_hash: str,
        topic_map_ids: list[str],
        base_ledger_id: str | None,
        provider_profile: WikiProviderProfile,
        topics: list[WikiTopicLedgerEntry],
        candidate_count: int,
        unresolved_merge_count: int,
    ) -> WikiTopicLedgerManifest:
        snapshot_root = self._root / snapshot_id
        snapshot_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".b-", dir=snapshot_root))
        ledger_id = f"tl_{os.urandom(8).hex()}"
        final_root = snapshot_root / ledger_id
        try:
            manifest = WikiTopicLedgerManifest(
                ledger_id=ledger_id,
                snapshot_id=snapshot_id,
                snapshot_manifest_hash=snapshot_manifest_hash,
                topic_map_ids=topic_map_ids,
                base_ledger_id=base_ledger_id,
                resolver_version=TOPIC_IDENTITY_RESOLVER_VERSION,
                prompt_version=TOPIC_IDENTITY_PROMPT_VERSION,
                prompt_template_hash=TOPIC_IDENTITY_PROMPT_HASH,
                provider=provider_profile,
                topics=topics,
                candidate_count=candidate_count,
                unresolved_merge_count=unresolved_merge_count,
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
                f"could not write topic ledger for snapshot {snapshot_id}",
            ) from error
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def _candidate_refs(maps: list[WikiTopicMapResult]) -> list[_CandidateRef]:
    refs: list[_CandidateRef] = []
    for topic_map in maps:
        for document in topic_map.documents:
            refs.extend(
                _CandidateRef(map_id=topic_map.manifest.map_id, candidate=candidate)
                for candidate in document.candidates
            )
    if len({ref.candidate.candidate_id for ref in refs}) != len(refs):
        raise WikiCompilationError(
            "topic_identity_candidate_duplicate",
            "topic maps contain duplicate candidate IDs",
        )
    return refs


def _group_by_exact_identity(
    refs: list[_CandidateRef],
) -> tuple[list[_CandidateGroup], dict[str, _CandidateGroup]]:
    parent = list(range(len(refs)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parent[second_root] = first_root

    first_by_name: dict[str, int] = {}
    for index, ref in enumerate(refs):
        for name in [ref.candidate.name, *ref.candidate.aliases]:
            normalized = normalize_topic_name(name)
            if not normalized:
                continue
            existing = first_by_name.get(normalized)
            if existing is None:
                first_by_name[normalized] = index
            else:
                union(index, existing)
    grouped: dict[int, list[_CandidateRef]] = {}
    for index, ref in enumerate(refs):
        grouped.setdefault(find(index), []).append(ref)
    groups = [_make_group(items) for items in grouped.values()]
    return groups, _group_index(groups)


def _make_group(refs: list[_CandidateRef]) -> _CandidateGroup:
    names: list[str] = []
    aliases: list[str] = []
    summaries: list[str] = []
    evidence_ids: set[str] = set()
    for ref in refs:
        names.append(ref.candidate.name)
        aliases.extend(ref.candidate.aliases)
        summaries.append(ref.candidate.summary)
        evidence_ids.update(ref.candidate.source_block_ids)
    group_digest = hashlib.sha256(
        "|".join(sorted(ref.candidate.candidate_id for ref in refs)).encode()
    ).hexdigest()
    group_id = f"wg_{group_digest[:16]}"
    return _CandidateGroup(
        group_id=group_id,
        refs=refs,
        names=_ordered_union([], names),
        aliases=_ordered_union([], aliases),
        summaries=_ordered_union([], summaries),
        evidence_count=len(evidence_ids),
    )


def _merge_groups_for_base_identity(
    groups: list[_CandidateGroup],
    base: WikiTopicLedgerResult | None,
) -> list[_CandidateGroup]:
    if base is None:
        return groups
    variants_to_topic: dict[str, str] = {}
    for topic in base.topics:
        for value in [topic.title, topic.canonical_name, *topic.aliases]:
            normalized = normalize_topic_name(value)
            if normalized:
                variants_to_topic.setdefault(normalized, topic.topic_id)
    by_topic_id: dict[str, list[_CandidateGroup]] = {}
    unmatched: list[_CandidateGroup] = []
    for group in groups:
        matched_ids = {
            variants_to_topic[normalized]
            for value in [*group.names, *group.aliases]
            if (normalized := normalize_topic_name(value)) in variants_to_topic
        }
        if len(matched_ids) == 1:
            topic_id = next(iter(matched_ids))
            by_topic_id.setdefault(topic_id, []).append(group)
        else:
            unmatched.append(group)
    result = unmatched
    for topic_id, matched_groups in by_topic_id.items():
        merged = _merge_group_list(matched_groups)
        merged.base_topic_id = topic_id
        result.append(merged)
    return result


async def _resolve_fuzzy_groups(
    groups: list[_CandidateGroup],
    provider: ChatProvider,
) -> tuple[list[_CandidateGroup], int]:
    if len(groups) < 2:
        return groups, 0
    request_groups: list[TopicIdentityGroup] = []
    neighbor_map: dict[str, set[str]] = {}
    for group in groups:
        neighbors = _identity_neighbors(group, groups)
        if not neighbors:
            continue
        neighbor_map[group.group_id] = {neighbor.group_id for neighbor in neighbors}
        request_groups.append(
            TopicIdentityGroup(
                group_id=group.group_id,
                names=group.names,
                aliases=group.aliases,
                summary=group.summaries[0],
                evidence_count=group.evidence_count,
                candidate_compare_groups=[
                    TopicIdentityCompareGroup(
                        group_id=neighbor.group_id,
                        names=neighbor.names,
                        aliases=neighbor.aliases,
                        summary=neighbor.summaries[0],
                    )
                    for neighbor in neighbors
                ],
            )
        )
    if not request_groups:
        return groups, 0
    parent = {group.group_id: group.group_id for group in groups}

    def find(group_id: str) -> str:
        while parent[group_id] != group_id:
            parent[group_id] = parent[parent[group_id]]
            group_id = parent[group_id]
        return group_id

    def union(source: str, target: str) -> None:
        source_root = find(source)
        target_root = find(target)
        if source_root != target_root:
            parent[source_root] = target_root

    merge_count = 0
    for offset in range(0, len(request_groups), IDENTITY_BATCH_SIZE):
        batch = request_groups[offset : offset + IDENTITY_BATCH_SIZE]
        response = await _generate_identity_merges(provider, batch)
        batch_ids = {group.group_id for group in batch}
        for source, target in response.merges.items():
            if source not in batch_ids or source not in neighbor_map:
                raise WikiCompilationError(
                    "topic_identity_merge_invalid",
                    f"identity response invented or reused an invalid source group: {source}",
                )
            if target not in neighbor_map[source]:
                raise WikiCompilationError(
                    "topic_identity_merge_invalid",
                    f"identity response merged into a group that was not a candidate: {target}",
                )
            union(source, target)
            merge_count += 1
    grouped: dict[str, list[_CandidateGroup]] = {}
    for group in groups:
        grouped.setdefault(find(group.group_id), []).append(group)
    merged_groups: list[_CandidateGroup] = []
    for items in grouped.values():
        merged = _merge_group_list(items)
        base_ids = {item.base_topic_id for item in items if item.base_topic_id is not None}
        if len(base_ids) > 1:
            raise WikiCompilationError(
                "topic_identity_ambiguous",
                "two existing topic identities were merged",
            )
        if base_ids:
            merged.base_topic_id = next(iter(base_ids))
        merged_groups.append(merged)
    unresolved = max(0, len(request_groups) - merge_count)
    return merged_groups, unresolved


async def _generate_identity_merges(
    provider: ChatProvider,
    groups: list[TopicIdentityGroup],
) -> TopicIdentityMergeResponse:
    try:
        result = await provider.complete(
            build_topic_identity_messages(groups),
            max_output_tokens=min(provider.profile.max_output_tokens, 4096),
        )
    except ChatProviderError as error:
        raise WikiCompilationError(
            "topic_identity_provider_failed",
            f"topic identity generation failed: {error.code}",
            retryable=error.retryable,
        ) from error
    if result.finish_reason not in {None, "stop"}:
        raise WikiCompilationError(
            "topic_identity_response_truncated",
            f"topic identity generation did not finish cleanly: {result.finish_reason}",
            retryable=True,
        )
    try:
        return TopicIdentityMergeResponse.model_validate_json(result.content)
    except ValidationError:
        try:
            payload = json.loads(result.content)
            return TopicIdentityMergeResponse.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as error:
            raise WikiCompilationError(
                "topic_identity_response_invalid",
                "topic identity generation returned an invalid JSON contract",
            ) from error


def _identity_neighbors(
    group: _CandidateGroup,
    groups: list[_CandidateGroup],
) -> list[_CandidateGroup]:
    scored: list[tuple[float, _CandidateGroup]] = []
    for candidate in groups:
        if candidate.group_id == group.group_id:
            continue
        score = _identity_similarity(group, candidate)
        if score >= IDENTITY_MIN_SIMILARITY:
            scored.append((score, candidate))
    scored.sort(key=lambda item: (-item[0], item[1].group_id))
    return [candidate for _, candidate in scored[:IDENTITY_NEIGHBOR_LIMIT]]


def _identity_similarity(first: _CandidateGroup, second: _CandidateGroup) -> float:
    first_variants = {normalize_topic_name(value) for value in [*first.names, *first.aliases]}
    second_variants = {normalize_topic_name(value) for value in [*second.names, *second.aliases]}
    if first_variants & second_variants:
        return 1.0
    best = 0.0
    for left in first_variants:
        for right in second_variants:
            if not left or not right:
                continue
            if left in right or right in left:
                best = max(best, min(len(left), len(right)) / max(len(left), len(right)))
            else:
                best = max(best, SequenceMatcher(None, left, right).ratio())
    return best


def _merge_group_list(groups: list[_CandidateGroup]) -> _CandidateGroup:
    refs = [ref for group in groups for ref in group.refs]
    merged = _make_group(refs)
    base_ids = {group.base_topic_id for group in groups if group.base_topic_id}
    if len(base_ids) == 1:
        merged.base_topic_id = next(iter(base_ids))
    return merged


def _ledger_entry(group: _CandidateGroup) -> WikiTopicLedgerEntry:
    primary = max(
        group.refs,
        key=lambda ref: (len(ref.candidate.source_block_ids), ref.candidate.candidate_id),
    ).candidate
    title = primary.name
    topic_id = group.base_topic_id or _topic_id(
        [*group.names, *group.aliases],
        [ref.candidate.candidate_id for ref in group.refs],
    )
    variants = _ordered_union([], [*group.names, *group.aliases])
    aliases = [
        value for value in variants if normalize_topic_name(value) != normalize_topic_name(title)
    ]
    evidence_ids = sorted(
        {block_id for ref in group.refs for block_id in ref.candidate.source_block_ids}
    )
    semantic_tags = _ordered_union(
        [],
        [tag for ref in group.refs for tag in ref.candidate.semantic_tags],
    )
    slots_by_key: dict[tuple[str, str], tuple[str, str, set[str]]] = {}
    for ref in group.refs:
        for slot in ref.candidate.coverage_slots:
            key = (normalize_topic_name(slot.label), normalize_topic_name(slot.question))
            existing = slots_by_key.get(key)
            if existing is None:
                slots_by_key[key] = (
                    slot.label,
                    slot.question,
                    set(slot.source_block_ids),
                )
            else:
                existing[2].update(slot.source_block_ids)
    coverage_slots = [
        TopicCoverageSlot(
            slot_id=_global_slot_id(topic_id, label, question),
            label=label,
            question=question,
            source_block_ids=sorted(block_ids),
        )
        for label, question, block_ids in slots_by_key.values()
    ]
    coverage_slots.sort(key=lambda slot: (slot.label, slot.question, slot.slot_id))
    return WikiTopicLedgerEntry(
        topic_id=topic_id,
        title=title,
        canonical_name=normalize_topic_name(title),
        normalized_name=normalize_topic_name(title),
        slug=f"topic/{_slugify(title, topic_id)}",
        aliases=aliases,
        summary=max(
            (ref.candidate.summary for ref in group.refs),
            key=lambda value: (len(value), value),
        ),
        semantic_tags=semantic_tags,
        source_doc_ids=sorted({ref.candidate.doc_id for ref in group.refs}),
        evidence_block_ids=evidence_ids,
        coverage_slots=coverage_slots,
        candidate_ids=sorted(ref.candidate.candidate_id for ref in group.refs),
    )


def _verify_candidate_coverage(
    refs: list[_CandidateRef],
    topics: list[WikiTopicLedgerEntry],
) -> None:
    expected = {ref.candidate.candidate_id for ref in refs}
    actual = {candidate_id for topic in topics for candidate_id in topic.candidate_ids}
    if expected != actual:
        raise WikiCompilationError(
            "topic_identity_candidate_loss",
            "topic ledger lost or invented candidate identities",
        )


def _group_index(groups: list[_CandidateGroup]) -> dict[str, _CandidateGroup]:
    result: dict[str, _CandidateGroup] = {}
    for group in groups:
        for ref in group.refs:
            result[ref.candidate.candidate_id] = group
    return result


def _topic_id(variants: list[str], candidate_ids: list[str]) -> str:
    payload = json.dumps(
        {
            "variants": sorted({normalize_topic_name(value) for value in variants if value}),
            "candidate_ids": sorted(candidate_ids),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"wt_{hashlib.sha256(payload).hexdigest()[:16]}"


def _global_slot_id(topic_id: str, label: str, question: str) -> str:
    payload = json.dumps(
        [topic_id, normalize_topic_name(label), normalize_topic_name(question)],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"ws_{hashlib.sha256(payload).hexdigest()[:16]}"


def _slugify(title: str, topic_id: str) -> str:
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
    if ascii_slug:
        return ascii_slug[:120]
    return topic_id.removeprefix("wt_")


def _ordered_union(first: list[str], second: list[str]) -> list[str]:
    result = list(first)
    seen = {normalize_topic_name(value) for value in first}
    for value in second:
        key = normalize_topic_name(value)
        if value and key and key not in seen:
            seen.add(key)
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


def _manifest_hash(manifest: WikiTopicLedgerManifest) -> str:
    payload = manifest.model_dump(mode="json", exclude={"manifest_hash"})
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
