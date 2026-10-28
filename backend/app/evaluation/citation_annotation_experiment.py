from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.agent.citation_annotation_prompt import (
    DEFAULT_CITATION_ANNOTATION_PROMPT,
    CitationAnnotationPrompt,
)
from app.agent.citations import (
    CitationAnnotation,
    CitationAnnotationInput,
    CitationAnnotator,
    CitationEvidenceBlock,
)
from app.agent.token_counter import ConservativeObservationTokenCounter
from app.evaluation.citation_prompt import CitationEvidenceInput
from app.evaluation.database import EvaluationDatabase
from app.evaluation.evidence import EvidenceBlock, _extract_evidence
from app.evaluation.judgment import (
    EvaluationJudgmentService,
    _build_citation_task,
    _JudgeBudget,
)
from app.evaluation.repository import EvaluationRepository
from app.providers.chat import DeepSeekChatProvider
from app.storage.credentials import CHAT_API_KEY_USERNAME, WindowsCredentialStore
from app.tracing.catalog import TraceCatalog

PRODUCT_ROOT = Path.home() / "AppData" / "Local" / "com.lipeidong.aistudyhelper"
EVAL_ROOT = PRODUCT_ROOT / "eval-data"
DEFAULT_CONCURRENCY = 2


def extract_frozen_packets(
    *,
    trace_catalog: TraceCatalog,
    trace_id: str,
) -> list[CitationAnnotationInput]:
    """从冻结 trace 提取原始 citation annotation 输入包。"""

    detail = trace_catalog.load(trace_id)
    packets: list[CitationAnnotationInput] = []
    prompt_artifacts = [
        artifact
        for artifact in detail.manifest.artifacts
        if artifact.kind == "citation.annotation.prompt"
    ]
    evidence_blocks = _extract_evidence(detail, trace_catalog, [])
    packets_by_artifact: list[CitationAnnotationInput] = []
    for artifact in prompt_artifacts:
        artifact_packets: list[CitationAnnotationInput] = []
        path = trace_catalog.artifact_path(trace_id, artifact.artifact_id)
        messages = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(messages, list):
            raise ValueError("citation annotation prompt artifact must contain a message list")
        for index, message in enumerate(messages):
            content = message.get("content") if isinstance(message, dict) else None
            if (
                isinstance(content, str)
                and message.get("role") == "system"
                and "后置引用标注器" in content
                and index + 1 < len(messages)
            ):
                artifact_packets.append(
                    CitationAnnotationInput.model_validate_json(messages[index + 1]["content"])
                )
        if not artifact_packets:
            continue
        base = artifact_packets[0]
        if any(
            packet.question != base.question
            or packet.answer != base.answer
            or packet.spans != base.spans
            for packet in artifact_packets[1:]
        ):
            raise ValueError(f"annotation batches disagree on frozen answer: {trace_id}")
        merged_evidence = list(
            {
                block.block_id: block
                for packet in artifact_packets
                for block in packet.evidence_blocks
            }.values()
        )
        packets_by_artifact.append(base.model_copy(update={"evidence_blocks": merged_evidence}))

    if not packets_by_artifact:
        raise ValueError(f"trace has no citation annotation packet: {trace_id}")
    packets.extend(
        restore_frozen_evidence_ids(packet, evidence_blocks) for packet in packets_by_artifact
    )
    return packets


def restore_frozen_evidence_ids(
    packet: CitationAnnotationInput,
    evidence_blocks: list[EvidenceBlock],
) -> CitationAnnotationInput:
    """把 prompt 中的短 evidence 引用恢复成 trace 的真实 block_id。"""

    evidence_by_signature: dict[tuple[str, tuple[str, ...], str], list[EvidenceBlock]] = (
        defaultdict(list)
    )
    for block in evidence_blocks:
        evidence_by_signature[(block.doc_id, tuple(block.section_path), block.text)].append(block)
    restored_blocks: list[CitationEvidenceBlock] = []
    for block in packet.evidence_blocks:
        signature = (block.doc_id, tuple(block.section_path), block.text)
        candidates = evidence_by_signature.get(signature, [])
        if not candidates:
            raise ValueError(f"frozen evidence block cannot be mapped to trace: {block.block_id}")
        candidate = candidates.pop(0)
        restored_blocks.append(
            CitationEvidenceBlock(
                block_id=candidate.block_id,
                doc_id=block.doc_id,
                doc_title=block.doc_title,
                source_label=block.source_label,
                section_path=block.section_path,
                text=block.text,
            )
        )
    return packet.model_copy(update={"evidence_blocks": restored_blocks})


def summarize_records(
    *,
    records: list[dict[str, Any]],
    repeats: int,
) -> dict[str, Any]:
    """汇总 reason 覆盖率、协议稳定性和指定 span 的选证分布。"""

    protocol_errors = sum(record["status"] != "complete" for record in records)
    annotation_count = 0
    empty_reason_count = 0
    reasons_by_span: dict[str, Counter[str]] = defaultdict(Counter)
    blocks_by_span: dict[str, Counter[tuple[str, ...]]] = defaultdict(Counter)
    for record in records:
        for annotation in record["annotations"]:
            annotation_count += 1
            key = f"{record['attempt_id']}:{annotation['span_id']}"
            reasons_by_span[key][annotation["reason"]] += 1
            blocks_by_span[key][tuple(annotation["block_ids"])] += 1
            if not annotation["reason"].strip():
                empty_reason_count += 1
    return {
        "attempt_count": len({record["attempt_id"] for record in records}),
        "repeat_count": len(records),
        "expected_repeat_count_per_attempt": repeats,
        "protocol_errors": protocol_errors,
        "annotation_count": annotation_count,
        "empty_reason_count": empty_reason_count,
        "reason_coverage": (
            0.0
            if annotation_count == 0
            else (annotation_count - empty_reason_count) / annotation_count
        ),
        "reasons_by_span": {
            span_key: dict(counter) for span_key, counter in sorted(reasons_by_span.items())
        },
        "blocks_by_span": {
            span_key: [
                {"block_ids": list(block_ids), "count": count}
                for block_ids, count in counter.most_common()
            ]
            for span_key, counter in sorted(blocks_by_span.items())
        },
    }


def _parse_args() -> argparse.Namespace:
    """读取冻结 run、候选提示词和输出路径。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--attempt-id", action="append", default=[])
    parser.add_argument("--judge-target-span", action="append", default=[])
    parser.add_argument("--judge-citations", action="store_true")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--prompt-version", default="candidate")
    return parser.parse_args()


def _load_prompt(path: Path | None, version: str) -> CitationAnnotationPrompt:
    """读取候选 annotation 提示词；未提供时使用产品默认提示词。"""

    if path is None:
        return DEFAULT_CITATION_ANNOTATION_PROMPT
    return CitationAnnotationPrompt(
        version=version,
        system_prompt=path.read_text(encoding="utf-8"),
    )


async def _run(args: argparse.Namespace) -> None:
    """在冻结 answer 和 evidence 上重复执行产品同源 annotation。"""

    database = EvaluationDatabase(EVAL_ROOT)
    repository = EvaluationRepository(database)
    run = repository.get_run(args.run_id)
    case_revisions = {
        revision.revision_id: revision
        for revision in repository.get_case_revisions(
            list({attempt.case_revision_id for attempt in run.attempts})
        )
    }
    attempts = [
        attempt
        for attempt in run.attempts
        if attempt.outcome_class == "product"
        and attempt.trace_id is not None
        and (not args.attempt_id or attempt.attempt_id in args.attempt_id)
        and (not args.case_id or case_revisions[attempt.case_revision_id].case_id in args.case_id)
    ]
    api_key = WindowsCredentialStore().get(CHAT_API_KEY_USERNAME)
    if not api_key:
        raise RuntimeError("chat API key is not configured")
    prompt = _load_prompt(args.prompt_file, args.prompt_version)
    annotator = CitationAnnotator(
        DeepSeekChatProvider(api_key),
        token_counter=ConservativeObservationTokenCounter(),
        prompt=prompt,
    )
    trace_catalog = TraceCatalog(EVAL_ROOT / "traces")
    judgment_service = (
        EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=trace_catalog,
            credentials=WindowsCredentialStore(),
            chat_provider_factory=lambda _key: DeepSeekChatProvider(api_key),
        )
        if args.judge_target_span or args.judge_citations
        else None
    )
    semaphore = asyncio.Semaphore(args.concurrency)
    records: list[dict[str, Any]] = []

    async def annotate_attempt(attempt: Any) -> None:
        packets = extract_frozen_packets(
            trace_catalog=trace_catalog,
            trace_id=attempt.trace_id,
        )
        for repeat in range(1, args.repeats + 1):
            batch_records: list[dict[str, Any]] = []
            annotations: list[CitationAnnotation] = []
            evidence_by_id = {}
            for batch_index, packet in enumerate(packets, start=1):
                evidence_by_id.update({block.block_id: block for block in packet.evidence_blocks})
                async with semaphore:
                    outcome = await annotator.annotate(
                        question=packet.question,
                        answer=packet.answer,
                        evidence_blocks=packet.evidence_blocks,
                        spans=packet.spans,
                        spent_input_tokens=0,
                        spent_output_tokens=0,
                        max_context_tokens=100_000,
                        max_request_tokens=400_000,
                    )
                annotations.extend(outcome.artifact.annotations)
                batch_records.append(
                    {
                        "batch_index": batch_index,
                        "status": outcome.artifact.status,
                        "error_code": outcome.artifact.error_code,
                        "error_message": outcome.artifact.error_message,
                        "model_calls": outcome.model_calls,
                        "input_tokens": outcome.input_tokens,
                        "output_tokens": outcome.output_tokens,
                        "raw_outputs": list(outcome.raw_outputs),
                        "annotations": [
                            annotation.model_dump(mode="json")
                            for annotation in outcome.artifact.annotations
                        ],
                    }
                )
            judge_result: dict[str, Any] | None = None
            if judgment_service is not None:
                target_annotations = (
                    annotations
                    if args.judge_citations
                    else [
                        annotation
                        for annotation in annotations
                        if annotation.span_id in args.judge_target_span
                    ]
                )
                citations = [
                    CitationEvidenceInput(
                        block_id=block_id,
                        doc_title=evidence_by_id[block_id].doc_title,
                        section_path=evidence_by_id[block_id].section_path,
                        text=evidence_by_id[block_id].text,
                    )
                    for block_id in dict.fromkeys(
                        block_id
                        for annotation in target_annotations
                        for block_id in annotation.block_ids
                    )
                    if block_id in evidence_by_id
                ]
                citation_task = _build_citation_task(
                    attempt_id=f"{attempt.attempt_id}:repeat-{repeat}",
                    answer=packets[0].answer,
                    citations=citations,
                    annotations=target_annotations,
                )
                if citation_task is None:
                    judge_result = {
                        "result": "missing",
                        "error_code": "citation_target_missing",
                        "payload": {},
                    }
                else:
                    judge_outcome = await judgment_service._judge_citation_task(
                        citation_task,
                        budget=_JudgeBudget(
                            token_budget=2_000_000,
                            request_budget=200,
                        ),
                        timeout_seconds=120,
                    )
                    judge_result = {
                        "result": judge_outcome.result,
                        "error_code": judge_outcome.error_code,
                        "payload": judge_outcome.payload,
                    }
            records.append(
                {
                    "attempt_id": attempt.attempt_id,
                    "case_id": case_revisions[attempt.case_revision_id].case_id,
                    "repeat": repeat,
                    "status": (
                        "complete"
                        if all(batch["status"] == "complete" for batch in batch_records)
                        else "pending"
                    ),
                    "batches": batch_records,
                    "annotations": [
                        annotation for batch in batch_records for annotation in batch["annotations"]
                    ],
                    "judge": judge_result,
                }
            )
        print(
            json.dumps(
                {"case_id": case_revisions[attempt.case_revision_id].case_id},
                ensure_ascii=False,
            ),
            flush=True,
        )

    await asyncio.gather(*(annotate_attempt(attempt) for attempt in attempts))
    summary = summarize_records(records=records, repeats=args.repeats)
    judge_summary = Counter(
        record["judge"]["result"] for record in records if record["judge"] is not None
    )
    result = {
        "run_id": args.run_id,
        "prompt_version": prompt.version,
        "prompt_hash": prompt.content_hash,
        "repeats": args.repeats,
        **summary,
        "judge_results": dict(judge_summary),
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "records"},
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


def main() -> None:
    """命令行入口。"""

    asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    main()
