"""5.1 独立产物的原子发布、加载校验与人工检查报告。"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.dual_entry_index.models import (
    CompilationDiagnostics,
    CompileContract,
    MapPublicationRecord,
    ObjectStatus,
    ScanResult,
    SnapshotData,
    Stage51Artifact,
)
from app.dual_entry_index.normalization import normalize_lexical_key


class ArtifactError(RuntimeError):
    """产物无法加载、身份校验失败或原子发布冲突时抛出的异常。"""


ACTIVE_MAP_POINTER = "active-map.json"


def load_stage51_artifact(path: Path) -> Stage51Artifact:
    """加载并校验一个不可变的 5.1 地图产物。"""

    artifact_path = path / "artifact.json" if path.is_dir() else path
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ArtifactError(f"cannot read artifact: {artifact_path}") from error
    if not isinstance(payload, dict):
        raise ArtifactError("artifact root must be a JSON object")
    try:
        artifact = Stage51Artifact.model_validate(payload)
    except Exception as error:
        raise ArtifactError("artifact schema is invalid") from error
    _validate_stage51_artifact_identity(artifact)
    return artifact


def load_active_stage51_artifact(output_root: Path) -> Stage51Artifact:
    """加载唯一活动地图指针当前指向的地图。"""

    root = output_root.resolve()
    pointer_path = root / ACTIVE_MAP_POINTER
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ArtifactError(f"cannot read active map pointer: {pointer_path}") from error
    if not isinstance(pointer, dict):
        raise ArtifactError("active map publication record must be a JSON object")
    try:
        publication = MapPublicationRecord.model_validate(pointer)
    except Exception as error:
        raise ArtifactError("active map publication record is invalid") from error
    map_id = publication.map_id
    map_hash = publication.map_hash
    if not map_id.startswith("km51_") or "/" in map_id or "\\" in map_id:
        raise ArtifactError("active map publication record contains an invalid map ID")
    artifact = load_stage51_artifact(root / map_id / "artifact.json")
    if artifact.map_id != map_id or artifact.map_hash != map_hash:
        raise ArtifactError("active map publication record does not match artifact identity")
    if (
        artifact.compile_contract.contract_id != publication.compile_contract_id
        or artifact.compile_contract.contract_hash != publication.compile_contract_hash
        or artifact.base_map_id != publication.base_map_id
        or artifact.compilation_mode != publication.compilation_mode
    ):
        raise ArtifactError("active map publication record does not match compile identity")
    return artifact


def publish_scan_result(output_root: Path, scan_result: ScanResult) -> Path:
    """原子发布一次候选扫描结果和人工检查报告。"""

    destination = output_root.resolve() / f"scan_{scan_result.candidate_set_hash[:16]}"
    files = {
        "scan.json": _json_text(scan_result.model_dump(mode="json")),
        "report.md": render_scan_report(scan_result),
    }
    return _publish_directory(destination, files)


def publish_stage51_artifact(
    output_root: Path,
    artifact: Stage51Artifact,
    diagnostics: CompilationDiagnostics,
    snapshot: SnapshotData,
) -> Path:
    """原子发布地图，并在完整校验后切换唯一活动地图指针。"""

    _validate_stage51_artifact_identity(artifact)
    _validate_publication_inputs(artifact, diagnostics, snapshot)
    output_path = output_root.resolve()
    destination = output_path / artifact.map_id
    files = {
        "artifact.json": _json_text(artifact.model_dump(mode="json")),
        "diagnostics.json": _json_text(diagnostics.model_dump(mode="json")),
        "report.md": render_map_report(artifact, diagnostics, snapshot),
        "concepts.md": render_concept_catalog(artifact, snapshot),
    }
    if destination.exists():
        existing = load_stage51_artifact(destination)
        if existing.map_hash != artifact.map_hash:
            raise ArtifactError(f"immutable publication conflict: {destination}")
        _write_active_map_publication(output_path, artifact)
        return destination
    published = _publish_directory(destination, files)
    load_stage51_artifact(published)
    _write_active_map_publication(output_path, artifact)
    return published


def render_scan_report(scan_result: ScanResult) -> str:
    """生成候选扫描的数量、噪声和词面频次报告。"""

    counts = Counter(item.normalized_key for item in scan_result.occurrences)
    model_group_count = sum(
        group.model_judgement_required for group in scan_result.candidate_groups
    )
    lexically_excluded_group_count = len(scan_result.candidate_groups) - model_group_count
    display_by_key: dict[str, str] = {}
    for occurrence in scan_result.occurrences:
        display_by_key.setdefault(occurrence.normalized_key, occurrence.display_text)
    lines = [
        "# 5.1 候选扫描报告",
        "",
        f"- 扫描契约总哈希：`{scan_result.contract.contract_hash}`",
        f"- 候选集合哈希：`{scan_result.candidate_set_hash}`",
        f"- 原文块：{len(scan_result.block_statuses)}",
        f"- 候选出现：{len(scan_result.occurrences)}",
        f"- 候选组：{len(scan_result.candidate_groups)}",
        f"- 需要模型判断的候选组：{model_group_count}",
        f"- 确定性词法排除的候选组：{lexically_excluded_group_count}",
        f"- 不同规范化词面：{len(counts)}",
        "",
        "## 噪声屏蔽",
        "",
    ]
    if scan_result.excluded_noise_counts:
        lines.extend(
            f"- {reason}: {count}" for reason, count in scan_result.excluded_noise_counts.items()
        )
    else:
        lines.append("- 无")
    lines.extend(["", "### 候选级词法排除", ""])
    if scan_result.lexical_exclusion_counts:
        lines.extend(
            f"- {reason}: {count}" for reason, count in scan_result.lexical_exclusion_counts.items()
        )
    else:
        lines.append("- 无")
    lines.extend(
        [
            "",
            "## 高频候选",
            "",
            "| 规范化词面 | 展示样例 | 出现次数 |",
            "| --- | --- | ---: |",
        ]
    )
    for key, count in counts.most_common(50):
        lines.append(f"| `{key}` | {display_by_key[key]} | {count} |")
    return "\n".join(lines) + "\n"


def render_map_report(
    artifact: Stage51Artifact,
    diagnostics: CompilationDiagnostics,
    snapshot: SnapshotData,
) -> str:
    """生成供人工审核的 5.1 地图摘要。"""

    judgement_counts = Counter(
        f"{item.term_decision.value}/{item.sense_decision.value}"
        for item in diagnostics.candidate_group_judgements
    )
    blocks_by_id = {block.block_id: block for block in snapshot.blocks}
    lines = [
        "# 5.1 概念索引编译报告",
        "",
        f"- 地图：`{artifact.map_id}`",
        f"- 地图哈希：`{artifact.map_hash}`",
        f"- 快照：`{artifact.snapshot_id}`",
        f"- 模式：{artifact.compilation_mode}",
        f"- 请求模型标识：`{artifact.compile_contract.model_id}`",
        "- 地图实际模型来源："
        f"{', '.join(artifact.compile_contract.actual_model_ids) or '无模型调用'}",
        f"- 本次实际模型响应：{', '.join(diagnostics.actual_model_ids) or '无模型调用'}",
        f"- 原文块：{diagnostics.block_count}",
        f"- 新增原文块：{len(diagnostics.added_block_ids)}",
        f"- 删除原文块：{len(diagnostics.deleted_block_ids)}",
        f"- 影响闭包重扫原文块：{len(diagnostics.rescanned_block_ids)}",
        f"- 资源护栏跳过原文块：{len(diagnostics.skipped_block_ids)}",
        f"- 候选出现：{diagnostics.candidate_occurrence_count}",
        f"- 候选组：{diagnostics.candidate_group_count}",
        f"- 需要模型判断的候选组：{artifact.stats.get('model_judged_groups', '未知')}",
        f"- 确定性词法排除的候选组：{artifact.stats.get('lexically_excluded_groups', '未知')}",
        f"- 术语：{len(artifact.terms)}",
        f"- 概念义项：{len(artifact.concept_senses)}",
        f"- 术语—概念义项关联：{len(artifact.term_sense_links)}",
        f"- 模型请求：{diagnostics.llm_requests}",
        f"- 模型实际尝试：{diagnostics.llm_attempts}",
        f"- 失败尝试：{diagnostics.failed_llm_attempts}",
        f"- 重试尝试：{diagnostics.retry_attempts}",
        f"- 输入 token：{diagnostics.input_tokens}",
        f"- 输出 token：{diagnostics.output_tokens}",
        f"- 缓存命中 token：{diagnostics.cache_hit_tokens}",
        f"- 缓存未命中 token：{diagnostics.cache_miss_tokens}",
        "",
        "## 原文块工作集",
        "",
        f"- 新增：{_block_id_list(diagnostics.added_block_ids)}",
        f"- 删除：{_block_id_list(diagnostics.deleted_block_ids)}",
        f"- 重扫：{_block_id_list(diagnostics.rescanned_block_ids)}",
        f"- 跳过：{_block_id_list(diagnostics.skipped_block_ids)}",
        "",
        "## 阶段耗时与模型用量",
        "",
        "| 阶段 | 实际模型标识 | 墙钟秒数 | 成功请求 | 尝试 | 失败 | 重试 | "
        "输入 | 输出 | 缓存命中 | 缓存未命中 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    stage_labels = {
        "scan": "确定性扫描",
        "term_judgement": "术语与义项判断",
        "consolidation": "概念归并",
        "materialization": "产物物化与校验",
        "total_compile": "编译总计",
    }
    lines.extend(
        f"| {stage_labels.get(metric.stage, metric.stage)} "
        f"| {', '.join(metric.actual_model_ids) or '-'} "
        f"| {metric.wall_seconds:.2f} "
        f"| {metric.requests} "
        f"| {metric.attempts} "
        f"| {metric.failed_attempts} "
        f"| {metric.retry_attempts} "
        f"| {metric.input_tokens} "
        f"| {metric.output_tokens} "
        f"| {metric.cache_hit_tokens} "
        f"| {metric.cache_miss_tokens} |"
        for metric in diagnostics.stage_metrics
    )
    failure_details = tuple(
        detail for metric in diagnostics.stage_metrics for detail in metric.failure_details
    )
    if failure_details:
        lines.extend(["", "### 失败尝试明细", ""])
        lines.extend(f"- `{detail}`" for detail in failure_details)
    lines.extend(
        [
            "",
            "## 判断分布",
            "",
            "| 术语／义项 | 数量 |",
            "| --- | ---: |",
        ]
    )
    lines.extend(f"| {key} | {count} |" for key, count in sorted(judgement_counts.items()))
    lines.extend(
        [
            "",
            "## 结构统计",
            "",
            "| 指标 | 数值 |",
            "| --- | ---: |",
        ]
    )
    lines.extend(f"| {key} | {value} |" for key, value in sorted(artifact.stats.items()))
    lines.extend(
        [
            "",
            "## 一词多义",
            "",
            "| 术语 | 义项数 |",
            "| --- | ---: |",
        ]
    )
    senses_by_term: dict[str, set[str]] = defaultdict(set)
    for link in artifact.term_sense_links:
        senses_by_term[link.normalized_key].add(link.concept_sense_id)
    ambiguous_terms = [
        (key, sense_ids) for key, sense_ids in senses_by_term.items() if len(sense_ids) > 1
    ]
    if ambiguous_terms:
        lines.extend(
            f"| `{key}` | {len(sense_ids)} |" for key, sense_ids in sorted(ambiguous_terms)
        )
    else:
        lines.append("| 无 | 0 |")
    lines.extend(
        [
            "",
            "## 一义多词",
            "",
            "| 概念义项 | 术语数 |",
            "| --- | ---: |",
        ]
    )
    terms_by_sense: dict[str, set[str]] = defaultdict(set)
    for link in artifact.term_sense_links:
        terms_by_sense[link.concept_sense_id].add(link.normalized_key)
    multi_term = [(sense_id, keys) for sense_id, keys in terms_by_sense.items() if len(keys) > 1]
    if multi_term:
        lines.extend(f"| `{sense_id}` | {len(keys)} |" for sense_id, keys in sorted(multi_term))
    else:
        lines.append("| 无 | 0 |")
    lines.extend(["", "## 仅提及与不确定样本", ""])
    notable = [
        item
        for item in diagnostics.records
        if item.code
        in {
            "term_uncertain",
            "concept_sense_mention_only",
            "concept_sense_uncertain",
        }
    ]
    if notable:
        for item in notable[:80]:
            block_id = item.payload.get("block_id", "")
            occurrence_ids = item.payload.get("occurrence_ids", [])
            context = blocks_by_id.get(str(block_id))
            title = context.doc_title if context is not None else block_id
            occurrence_label = ", ".join(str(value) for value in occurrence_ids) or "无候选出现"
            lines.append(f"- `{item.code}` · {title} · `{occurrence_label}`：{item.message}")
    else:
        lines.append("- 无")
    return "\n".join(lines) + "\n"


def render_concept_catalog(artifact: Stage51Artifact, snapshot: SnapshotData) -> str:
    """为每个概念义项列出术语入口和可回源原文块。"""

    blocks_by_id = {block.block_id: block for block in snapshot.blocks}
    terms_by_sense: dict[str, list[str]] = defaultdict(list)
    for link in artifact.term_sense_links:
        if link.normalized_key not in terms_by_sense[link.concept_sense_id]:
            terms_by_sense[link.concept_sense_id].append(link.normalized_key)
    lines = ["# 5.1 概念义项目录", ""]
    for sense in artifact.concept_senses:
        lines.extend(
            [
                f"## {sense.summary}",
                "",
                f"- 义项 ID：`{sense.concept_sense_id}`",
                f"- 摘要依据：{sense.summary_basis.value}",
                "- 术语："
                + ", ".join(f"`{key}`" for key in sorted(terms_by_sense[sense.concept_sense_id])),
                "",
                "支持原文块：",
                "",
            ]
        )
        for block_id in sense.support_block_ids:
            block = blocks_by_id.get(block_id)
            if block is None:
                lines.append(f"- `{block_id}`：当前快照中缺失")
                continue
            excerpt = " ".join(block.text.split())[:260]
            lines.append(f"- {block.doc_title} / `{block_id}`：{excerpt}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _block_id_list(block_ids: tuple[str, ...]) -> str:
    """把块标识列表渲染为适合人工审核报告的单行 Markdown。"""

    return "、".join(f"`{block_id}`" for block_id in block_ids) or "无"


def _validate_stage51_artifact_identity(artifact: Stage51Artifact) -> None:
    _validate_compile_contract(artifact.compile_contract)
    if (
        artifact.compile_contract.snapshot_id != artifact.snapshot_id
        or artifact.compile_contract.snapshot_manifest_hash != artifact.snapshot_manifest_hash
    ):
        raise ArtifactError("artifact snapshot does not match compile contract")
    if artifact.calculate_map_hash() != artifact.map_hash:
        raise ArtifactError("artifact map_hash does not match content")
    if artifact.map_id != f"km51_{artifact.map_hash[:16]}":
        raise ArtifactError("artifact map_id does not match map_hash")
    _validate_stage51_relation_closure(artifact)


def _validate_stage51_relation_closure(artifact: Stage51Artifact) -> None:
    """校验地图只包含当前活动对象，且术语、义项和关联互相闭合。"""

    source_block_ids = {block.block_id for block in artifact.source_blocks}
    if len(source_block_ids) != len(artifact.source_blocks):
        raise ArtifactError("artifact contains duplicate source block IDs")

    terms = {term.normalized_key: term for term in artifact.terms}
    concepts = {sense.concept_sense_id: sense for sense in artifact.concept_senses}
    if len(terms) != len(artifact.terms) or len(concepts) != len(artifact.concept_senses):
        raise ArtifactError("artifact contains duplicate knowledge object IDs")
    for term in artifact.terms:
        if term.status is not ObjectStatus.ACTIVE or not term.support_block_ids:
            raise ArtifactError(f"term is not active or has no support: {term.normalized_key}")
        if normalize_lexical_key(term.normalized_key) != term.normalized_key:
            raise ArtifactError(f"term key is not normalized: {term.normalized_key}")
        if term.support_block_ids != tuple(sorted(set(term.support_block_ids))):
            raise ArtifactError(f"term support is not canonical: {term.normalized_key}")
        if not set(term.support_block_ids) <= source_block_ids:
            raise ArtifactError(
                f"term references a block outside the artifact: {term.normalized_key}"
            )
    for concept in artifact.concept_senses:
        if concept.status is not ObjectStatus.ACTIVE or not concept.support_block_ids:
            raise ArtifactError(
                f"concept sense is not active or has no support: {concept.concept_sense_id}"
            )
        if not concept.summary.strip():
            raise ArtifactError(f"concept sense has an empty summary: {concept.concept_sense_id}")
        if (
            concept.induction_contract_version
            != artifact.compile_contract.induction_contract_version
        ):
            raise ArtifactError(
                f"concept sense induction contract differs: {concept.concept_sense_id}"
            )
        if concept.support_block_ids != tuple(sorted(set(concept.support_block_ids))):
            raise ArtifactError(
                f"concept sense support is not canonical: {concept.concept_sense_id}"
            )
        if not set(concept.support_block_ids) <= source_block_ids:
            raise ArtifactError(
                f"concept sense references a block outside the artifact: {concept.concept_sense_id}"
            )

    seen_link_ids: set[tuple[str, str, str]] = set()
    support_by_concept: dict[str, set[str]] = {}
    for link in artifact.term_sense_links:
        identity = (link.normalized_key, link.concept_sense_id, link.basis_type.value)
        if identity in seen_link_ids:
            raise ArtifactError("artifact contains a duplicate term-sense link")
        seen_link_ids.add(identity)
        term = terms.get(link.normalized_key)
        if (
            link.status is not ObjectStatus.ACTIVE
            or term is None
            or link.concept_sense_id not in concepts
        ):
            raise ArtifactError("artifact contains an inactive or dangling term-sense link")
        if not link.support_block_ids or not set(link.support_block_ids) <= set(
            term.support_block_ids
        ):
            raise ArtifactError("term-sense link support exceeds term support")
        if link.support_block_ids != tuple(sorted(set(link.support_block_ids))):
            raise ArtifactError("term-sense link support is not canonical")
        support_by_concept.setdefault(link.concept_sense_id, set()).update(link.support_block_ids)
    for concept in artifact.concept_senses:
        if support_by_concept.get(concept.concept_sense_id) != set(concept.support_block_ids):
            raise ArtifactError(
                f"concept sense support does not match links: {concept.concept_sense_id}"
            )


def _validate_publication_inputs(
    artifact: Stage51Artifact,
    diagnostics: CompilationDiagnostics,
    snapshot: SnapshotData,
) -> None:
    """确保地图、编译诊断和发布报告绑定同一个快照与原文块清单。"""

    if (
        artifact.snapshot_id != snapshot.snapshot_id
        or artifact.snapshot_manifest_hash != snapshot.manifest_hash
    ):
        raise ArtifactError("artifact snapshot does not match publication snapshot")
    expected_blocks = tuple((block.block_id, block.content_hash) for block in snapshot.blocks)
    artifact_blocks = tuple(
        (block.block_id, block.content_hash) for block in artifact.source_blocks
    )
    if artifact_blocks != expected_blocks:
        raise ArtifactError("artifact source blocks do not match publication snapshot")
    if diagnostics.map_id != artifact.map_id or (diagnostics.snapshot_id != artifact.snapshot_id):
        raise ArtifactError("compilation diagnostics do not match artifact identity")
    if diagnostics.actual_model_ids != artifact.compile_contract.actual_model_ids:
        raise ArtifactError("compilation diagnostics actual models do not match compile contract")


def _validate_compile_contract(contract: CompileContract) -> None:
    if contract.calculate_compatibility_hash() != contract.compatibility_hash:
        raise ArtifactError("compile contract compatibility hash does not match content")
    if contract.calculate_contract_hash() != contract.contract_hash:
        raise ArtifactError("compile contract hash does not match content")
    if contract.contract_id != f"cc51_{contract.contract_hash[:16]}":
        raise ArtifactError("compile contract ID does not match hash")
    if len(contract.actual_model_ids) > 1:
        raise ArtifactError("compile contract contains multiple actual model IDs")


def _write_active_map_publication(output_root: Path, artifact: Stage51Artifact) -> None:
    publication = MapPublicationRecord(
        map_id=artifact.map_id,
        map_hash=artifact.map_hash,
        compile_contract_id=artifact.compile_contract.contract_id,
        compile_contract_hash=artifact.compile_contract.contract_hash,
        base_map_id=artifact.base_map_id,
        compilation_mode=artifact.compilation_mode,
        status="active",
        published_at=datetime.now(UTC),
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".active-map.",
        dir=output_root,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(_json_text(publication.model_dump(mode="json")))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output_root / ACTIVE_MAP_POINTER)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _publish_directory(destination: Path, files: dict[str, str]) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_dir():
            raise ArtifactError(f"publication path is not a directory: {destination}")
        for filename, expected in files.items():
            path = destination / filename
            if not path.is_file() or path.read_text(encoding="utf-8") != expected:
                raise ArtifactError(f"immutable publication conflict: {path}")
        return destination

    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        for filename, content in files.items():
            (staging / filename).write_text(content, encoding="utf-8", newline="\n")
        os.replace(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return destination
