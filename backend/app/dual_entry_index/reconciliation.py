"""5.1 仅存在于单次编译内的显式对账计划。

计划不持久化，也不允许从半成品续跑。它把基础地图、目标快照和最终状态之间的差异展开成
完整操作集，并在发布前实际应用一次；只有应用结果与待发布状态完全一致才允许继续。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from app.dual_entry_index.models import (
    ConceptSense,
    LinkBasis,
    Stage51Artifact,
    TermRecord,
    TermSenseLink,
)


class ReconciliationError(RuntimeError):
    """内存对账计划无法完整、确定地重建目标状态。"""


class ReconciliationObjectKind(StrEnum):
    """对账计划能够操作的知识对象类型。"""

    TERM = "term"
    TERM_SENSE_LINK = "term_sense_link"
    CONCEPT_SENSE = "concept_sense"


class ReconciliationAction(StrEnum):
    """一次内存对账对对象采取的动作。"""

    REUSE = "reuse"
    CREATE = "create"
    UPDATE = "update"
    MERGE = "merge"
    RETIRE = "retire"


type KnowledgeObject = TermRecord | TermSenseLink | ConceptSense


@dataclass(frozen=True, slots=True)
class ReconciliationOperation:
    """完整操作集中的一项，包含目标对象和证据增删。"""

    object_kind: ReconciliationObjectKind
    action: ReconciliationAction
    object_id: str
    source_object_ids: tuple[str, ...]
    evidence_added: tuple[str, ...]
    evidence_removed: tuple[str, ...]
    reason: str
    induction_contract_version: str
    result: KnowledgeObject | None


@dataclass(frozen=True, slots=True)
class ReconciliationPlan:
    """基础地图到目标快照的完整内存操作集。"""

    base_map_id: str | None
    target_snapshot_id: str
    induction_contract_version: str
    operations: tuple[ReconciliationOperation, ...]

    def apply(
        self, base_artifact: Stage51Artifact | None
    ) -> tuple[
        tuple[TermRecord, ...],
        tuple[TermSenseLink, ...],
        tuple[ConceptSense, ...],
    ]:
        """把计划依次应用到基础地图副本，并返回新地图状态。"""

        terms = {
            item.normalized_key: item
            for item in (() if base_artifact is None else base_artifact.terms)
        }
        links = {
            _link_id(item): item
            for item in (() if base_artifact is None else base_artifact.term_sense_links)
        }
        senses = {
            item.concept_sense_id: item
            for item in (() if base_artifact is None else base_artifact.concept_senses)
        }

        for operation in self.operations:
            target: dict[str, KnowledgeObject]
            if operation.object_kind is ReconciliationObjectKind.TERM:
                target = cast(dict[str, KnowledgeObject], terms)
            elif operation.object_kind is ReconciliationObjectKind.TERM_SENSE_LINK:
                target = cast(dict[str, KnowledgeObject], links)
            else:
                target = cast(dict[str, KnowledgeObject], senses)

            if operation.action is ReconciliationAction.RETIRE:
                if operation.object_id not in target:
                    raise ReconciliationError(
                        f"retire_target_missing:{operation.object_kind}:{operation.object_id}"
                    )
                del target[operation.object_id]
                continue
            if operation.result is None:
                raise ReconciliationError("non_retire_operation_has_no_result")
            if not _operation_result_matches(operation):
                raise ReconciliationError(
                    "operation_result_identity_mismatch:"
                    f"{operation.object_kind}:{operation.object_id}"
                )
            if operation.action is ReconciliationAction.CREATE:
                if operation.object_id in target:
                    raise ReconciliationError("create_target_already_exists")
            elif operation.action is ReconciliationAction.REUSE:
                if operation.object_id not in target:
                    raise ReconciliationError("reuse_target_missing")
            elif operation.action is ReconciliationAction.UPDATE:
                if not operation.source_object_ids and operation.object_id not in target:
                    raise ReconciliationError("update_target_missing")
                if any(
                    source_id not in target
                    for source_id in operation.source_object_ids
                    if source_id != operation.object_id
                ):
                    raise ReconciliationError("update_source_missing")
                for source_id in operation.source_object_ids:
                    if source_id != operation.object_id:
                        target.pop(source_id, None)
            elif operation.action is ReconciliationAction.MERGE:
                if any(item not in target for item in operation.source_object_ids):
                    raise ReconciliationError("merge_source_missing")
                for source_id in operation.source_object_ids:
                    target.pop(source_id, None)
            target[operation.object_id] = operation.result

        return (
            tuple(terms[key] for key in sorted(terms)),
            tuple(sorted(links.values(), key=_link_sort_key)),
            tuple(senses[key] for key in sorted(senses)),
        )


def build_reconciliation_plan(
    *,
    base_artifact: Stage51Artifact | None,
    target_snapshot_id: str,
    induction_contract_version: str,
    terms: tuple[TermRecord, ...],
    links: tuple[TermSenseLink, ...],
    concepts: tuple[ConceptSense, ...],
    sense_lineage: dict[str, tuple[str, ...]],
    affected_term_keys: set[str],
) -> ReconciliationPlan:
    """构造并校验基础地图到目标状态的完整操作集。"""

    _validate_target_state(terms, links, concepts)
    operations: list[ReconciliationOperation] = []
    operations.extend(
        _term_operations(
            base_artifact,
            terms,
            induction_contract_version,
        )
    )
    operations.extend(
        _concept_operations(
            base_artifact,
            concepts,
            sense_lineage,
            induction_contract_version,
        )
    )
    operations.extend(
        _link_operations(
            base_artifact,
            links,
            induction_contract_version,
        )
    )
    operations.extend(
        _retire_operations(
            base_artifact,
            terms,
            links,
            concepts,
            sense_lineage,
            induction_contract_version,
        )
    )
    plan = ReconciliationPlan(
        base_map_id=None if base_artifact is None else base_artifact.map_id,
        target_snapshot_id=target_snapshot_id,
        induction_contract_version=induction_contract_version,
        operations=tuple(operations),
    )
    _validate_affected_scope(plan, base_artifact, affected_term_keys, links)
    actual_terms, actual_links, actual_concepts = plan.apply(base_artifact)
    if actual_terms != terms or actual_links != links or actual_concepts != concepts:
        raise ReconciliationError(
            "reconciliation_plan_does_not_reproduce_target_state:"
            f"terms={len(actual_terms)}/{len(terms)}:"
            f"links={len(actual_links)}/{len(links)}:"
            f"concepts={len(actual_concepts)}/{len(concepts)}"
        )
    return plan


def _validate_target_state(
    terms: tuple[TermRecord, ...],
    links: tuple[TermSenseLink, ...],
    concepts: tuple[ConceptSense, ...],
) -> None:
    """拒绝无法由对象身份唯一回放的目标状态。"""

    term_ids = [item.normalized_key for item in terms]
    link_ids = [_link_id(item) for item in links]
    concept_ids = [item.concept_sense_id for item in concepts]
    if len(term_ids) != len(set(term_ids)):
        raise ReconciliationError("target_terms_contain_duplicate_identity")
    if len(link_ids) != len(set(link_ids)):
        raise ReconciliationError("target_links_contain_duplicate_identity")
    if len(concept_ids) != len(set(concept_ids)):
        raise ReconciliationError("target_concepts_contain_duplicate_identity")


def _validate_affected_scope(
    plan: ReconciliationPlan,
    base_artifact: Stage51Artifact | None,
    affected_term_keys: set[str],
    target_links: tuple[TermSenseLink, ...],
) -> None:
    if base_artifact is None:
        return
    for operation in plan.operations:
        if operation.object_kind is ReconciliationObjectKind.CONCEPT_SENSE:
            continue
        normalized_key = _operation_term_key(operation, base_artifact)
        if normalized_key in affected_term_keys:
            continue
        if operation.action is not ReconciliationAction.REUSE:
            raise ReconciliationError(
                "reconciliation_modified_object_outside_affected_scope:"
                f"{operation.object_kind}:{normalized_key}"
            )
    affected_sense_ids = {
        link.concept_sense_id
        for link in base_artifact.term_sense_links
        if link.normalized_key in affected_term_keys
    }
    target_keys_by_sense: dict[str, set[str]] = {}
    for link in target_links:
        target_keys_by_sense.setdefault(link.concept_sense_id, set()).add(link.normalized_key)
    for operation in plan.operations:
        if operation.object_kind is not ReconciliationObjectKind.CONCEPT_SENSE:
            continue
        if operation.action is ReconciliationAction.REUSE:
            continue
        if operation.action is ReconciliationAction.RETIRE:
            if operation.object_id not in affected_sense_ids:
                raise ReconciliationError(
                    "reconciliation_retired_object_outside_affected_scope:"
                    f"concept_sense:{operation.object_id}"
                )
            continue
        touches_affected_sense = bool(set(operation.source_object_ids) & affected_sense_ids)
        touches_affected_key = bool(
            target_keys_by_sense.get(operation.object_id, set()) & affected_term_keys
        )
        if not touches_affected_sense and not touches_affected_key:
            raise ReconciliationError(
                "reconciliation_modified_object_outside_affected_scope:"
                f"concept_sense:{operation.object_id}"
            )


def _term_operations(
    base_artifact: Stage51Artifact | None,
    terms: tuple[TermRecord, ...],
    induction_contract_version: str,
) -> list[ReconciliationOperation]:
    base = (
        {} if base_artifact is None else {item.normalized_key: item for item in base_artifact.terms}
    )
    result: list[ReconciliationOperation] = []
    for term in terms:
        previous = base.get(term.normalized_key)
        action = _object_action(previous, term)
        result.append(
            ReconciliationOperation(
                object_kind=ReconciliationObjectKind.TERM,
                action=action,
                object_id=term.normalized_key,
                source_object_ids=() if previous is None else (term.normalized_key,),
                evidence_added=tuple(sorted(set(term.support_block_ids) - set(_support(previous)))),
                evidence_removed=tuple(
                    sorted(set(_support(previous)) - set(term.support_block_ids))
                ),
                reason=_action_reason(action),
                induction_contract_version=induction_contract_version,
                result=term,
            )
        )
    return result


def _concept_operations(
    base_artifact: Stage51Artifact | None,
    concepts: tuple[ConceptSense, ...],
    sense_lineage: dict[str, tuple[str, ...]],
    induction_contract_version: str,
) -> list[ReconciliationOperation]:
    base = (
        {}
        if base_artifact is None
        else {item.concept_sense_id: item for item in base_artifact.concept_senses}
    )
    result: list[ReconciliationOperation] = []
    for concept in concepts:
        lineage = tuple(sorted(sense_lineage.get(concept.concept_sense_id, ())))
        previous = base.get(concept.concept_sense_id)
        source_senses = tuple(base[item] for item in lineage if item in base)
        if len(source_senses) > 1:
            action = ReconciliationAction.MERGE
        elif not source_senses and previous is None:
            action = ReconciliationAction.CREATE
        elif source_senses:
            action = (
                ReconciliationAction.REUSE
                if source_senses[0].concept_sense_id == concept.concept_sense_id
                and source_senses[0] == concept
                else ReconciliationAction.UPDATE
            )
        else:
            action = _object_action(previous, concept)
        previous_support = {
            block_id for item in source_senses for block_id in item.support_block_ids
        }
        if not source_senses and previous is not None:
            previous_support = set(previous.support_block_ids)
        result.append(
            ReconciliationOperation(
                object_kind=ReconciliationObjectKind.CONCEPT_SENSE,
                action=action,
                object_id=concept.concept_sense_id,
                source_object_ids=lineage,
                evidence_added=tuple(sorted(set(concept.support_block_ids) - previous_support)),
                evidence_removed=tuple(sorted(previous_support - set(concept.support_block_ids))),
                reason=_action_reason(action),
                induction_contract_version=induction_contract_version,
                result=concept,
            )
        )
    return result


def _link_operations(
    base_artifact: Stage51Artifact | None,
    links: tuple[TermSenseLink, ...],
    induction_contract_version: str,
) -> list[ReconciliationOperation]:
    base = (
        {}
        if base_artifact is None
        else {_link_id(item): item for item in base_artifact.term_sense_links}
    )
    result: list[ReconciliationOperation] = []
    for link in links:
        identity = _link_id(link)
        previous = base.get(identity)
        action = _object_action(previous, link)
        result.append(
            ReconciliationOperation(
                object_kind=ReconciliationObjectKind.TERM_SENSE_LINK,
                action=action,
                object_id=identity,
                source_object_ids=() if previous is None else (identity,),
                evidence_added=tuple(sorted(set(link.support_block_ids) - set(_support(previous)))),
                evidence_removed=tuple(
                    sorted(set(_support(previous)) - set(link.support_block_ids))
                ),
                reason=_action_reason(action),
                induction_contract_version=induction_contract_version,
                result=link,
            )
        )
    return result


def _retire_operations(
    base_artifact: Stage51Artifact | None,
    terms: tuple[TermRecord, ...],
    links: tuple[TermSenseLink, ...],
    concepts: tuple[ConceptSense, ...],
    sense_lineage: dict[str, tuple[str, ...]],
    induction_contract_version: str,
) -> list[ReconciliationOperation]:
    if base_artifact is None:
        return []
    term_ids = {item.normalized_key for item in terms}
    link_ids = {_link_id(item) for item in links}
    concept_ids = {item.concept_sense_id for item in concepts}
    consumed_source_sense_ids = {
        source_id
        for target_id, source_ids in sense_lineage.items()
        if target_id in concept_ids
        for source_id in source_ids
        if source_id != target_id
    }
    result: list[ReconciliationOperation] = []
    for item in base_artifact.concept_senses:
        if (
            item.concept_sense_id not in concept_ids
            and item.concept_sense_id not in consumed_source_sense_ids
        ):
            result.append(
                _retire(
                    item.concept_sense_id,
                    ReconciliationObjectKind.CONCEPT_SENSE,
                    item.support_block_ids,
                    induction_contract_version,
                )
            )
    for item in base_artifact.term_sense_links:
        if _link_id(item) not in link_ids:
            result.append(
                _retire(
                    _link_id(item),
                    ReconciliationObjectKind.TERM_SENSE_LINK,
                    item.support_block_ids,
                    induction_contract_version,
                )
            )
    for item in base_artifact.terms:
        if item.normalized_key not in term_ids:
            result.append(
                _retire(
                    item.normalized_key,
                    ReconciliationObjectKind.TERM,
                    item.support_block_ids,
                    induction_contract_version,
                )
            )
    return result


def _retire(
    object_id: str,
    object_kind: ReconciliationObjectKind,
    removed_support: tuple[str, ...],
    induction_contract_version: str,
) -> ReconciliationOperation:
    return ReconciliationOperation(
        object_kind=object_kind,
        action=ReconciliationAction.RETIRE,
        object_id=object_id,
        source_object_ids=(object_id,),
        evidence_added=(),
        evidence_removed=tuple(sorted(removed_support)),
        reason="目标快照已无有效支持，退役对象",
        induction_contract_version=induction_contract_version,
        result=None,
    )


def _operation_term_key(
    operation: ReconciliationOperation,
    base_artifact: Stage51Artifact | None,
) -> str:
    """返回操作涉及的规范化词面键，不解析包含分隔符的自由文本身份。"""

    if operation.object_kind is ReconciliationObjectKind.TERM:
        return operation.object_id
    if isinstance(operation.result, TermSenseLink):
        return operation.result.normalized_key
    if base_artifact is not None:
        for link in base_artifact.term_sense_links:
            if _link_id(link) == operation.object_id:
                return link.normalized_key
    raise ReconciliationError(
        f"operation_term_key_unresolved:{operation.object_kind}:{operation.object_id}"
    )


def _operation_result_matches(operation: ReconciliationOperation) -> bool:
    """确认操作声明的对象类型和标识与回放结果完全一致。"""

    if operation.object_kind is ReconciliationObjectKind.TERM:
        return (
            isinstance(operation.result, TermRecord)
            and operation.result.normalized_key == operation.object_id
        )
    if operation.object_kind is ReconciliationObjectKind.TERM_SENSE_LINK:
        return (
            isinstance(operation.result, TermSenseLink)
            and _link_id(operation.result) == operation.object_id
        )
    return (
        isinstance(operation.result, ConceptSense)
        and operation.result.concept_sense_id == operation.object_id
    )


def _object_action(
    previous: KnowledgeObject | None, current: KnowledgeObject
) -> ReconciliationAction:
    if previous is None:
        return ReconciliationAction.CREATE
    if previous == current:
        return ReconciliationAction.REUSE
    return ReconciliationAction.UPDATE


def _action_reason(action: ReconciliationAction) -> str:
    return {
        ReconciliationAction.REUSE: "基础对象与目标状态一致，直接复用",
        ReconciliationAction.CREATE: "目标状态中存在新对象",
        ReconciliationAction.UPDATE: "目标状态更新对象内容或证据",
        ReconciliationAction.MERGE: "多个基础对象归并为一个目标对象",
        ReconciliationAction.RETIRE: "目标状态不再保留该对象",
    }[action]


def _support(value: KnowledgeObject | None) -> tuple[str, ...]:
    return () if value is None else value.support_block_ids


def _link_id(value: TermSenseLink) -> str:
    """构造仅在当前地图内有意义的关联操作标识。"""

    return f"{value.normalized_key}|{value.concept_sense_id}|{LinkBasis(value.basis_type).value}"


def _link_sort_key(value: TermSenseLink) -> tuple[str, str, str]:
    """使用与目标地图一致的结构化顺序，避免拼接分隔符改变排序边界。"""

    return (
        value.normalized_key,
        value.concept_sense_id,
        LinkBasis(value.basis_type).value,
    )
