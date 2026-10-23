from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.tracing.catalog import TraceCatalog, TraceInvalidError, TraceNotFoundError
from app.tracing.models import TraceArtifactRecord, TraceDetailRecord

CITATION_PATTERN = re.compile(r"【来源：([^】]+)】")
MAX_JUDGE_EVIDENCE_CHARS = 60_000


class JudgeInputError(RuntimeError):
    """JudgeInput 无法可靠构建时的稳定错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvidenceBlock(BaseModel):
    """发送给 Judge 的一块可核验证据。"""

    block_id: str
    doc_id: str
    doc_title: str
    source_label: str
    section_path: list[str]
    text: str
    cited_in_answer: bool


class GenerationState(BaseModel):
    """回答生成完整度和运行限制。"""

    answer_completeness: Literal["complete", "partial"]
    limitations: list[dict[str, Any]] = Field(default_factory=list)
    termination_reason: str


class JudgeCriterionInput(BaseModel):
    """单次 Judge 调用唯一针对的 criterion。"""

    rule_type: Literal["R", "X"]
    rule_text: str


class JudgeInputPacket(BaseModel):
    """发送到主判官的最小输入：答案与当前 criterion。"""

    answer: str
    criterion: JudgeCriterionInput


class CheckerPayload(BaseModel):
    """本 attempt 的通用跨判定项确定性检查。"""

    generation_state: GenerationState
    cited_block_ids: list[str]
    missing_block_ids: list[str]
    evidence_block_count: int
    truncation_declared: bool


@dataclass(frozen=True, slots=True)
class BuiltJudgeInput:
    """checker 证据投影和 trace 现场。"""

    checker_payload: CheckerPayload
    all_evidence: list[EvidenceBlock]
    trace: TraceDetailRecord


def build_judge_input(
    *,
    catalog: TraceCatalog,
    trace_id: str | None,
    trace_status: str,
    answer: str | None,
) -> BuiltJudgeInput:
    """从一次完整 attempt trace 构造 checker 事实。

    evidence 只来自该 attempt 实际工具返回值；不会重新检索 collection，
    也不会用其他 attempt 的块补齐。
    """

    if trace_id is None or trace_status != "complete":
        raise JudgeInputError(
            "judge_trace_incomplete",
            "attempt trace is missing or incomplete",
        )
    try:
        trace = catalog.load(trace_id)
    except TraceNotFoundError as error:
        raise JudgeInputError(
            "judge_trace_missing",
            "attempt trace does not exist",
        ) from error
    except TraceInvalidError as error:
        raise JudgeInputError(
            "judge_trace_invalid",
            str(error),
        ) from error

    normalized_answer = answer or ""
    cited_ids = _cited_block_ids(normalized_answer)
    evidence = _extract_evidence(trace, catalog, cited_ids)
    evidence_ids = {item.block_id for item in evidence}
    missing_ids = [block_id for block_id in cited_ids if block_id not in evidence_ids]
    limitations = list(trace.manifest.limitations)
    checker_payload = CheckerPayload(
        generation_state=GenerationState(
            answer_completeness=trace.manifest.answer_completeness,
            limitations=limitations,
            termination_reason=trace.manifest.termination_reason,
        ),
        cited_block_ids=cited_ids,
        missing_block_ids=missing_ids,
        evidence_block_count=len(evidence),
        truncation_declared=_declares_truncation(normalized_answer),
    )
    return BuiltJudgeInput(
        checker_payload=checker_payload,
        all_evidence=evidence,
        trace=trace,
    )


def _extract_evidence(
    trace: TraceDetailRecord,
    catalog: TraceCatalog,
    cited_ids: list[str],
) -> list[EvidenceBlock]:
    cited_set = set(cited_ids)
    by_block_id: dict[str, EvidenceBlock] = {}
    for span in trace.spans:
        if span.kind != "TOOL":
            continue
        artifact = _artifact_ref(span.attributes.get("app.payload_artifact"))
        if artifact is None:
            continue
        if artifact.state != "stored":
            raise JudgeInputError(
                "judge_evidence_artifact_missing",
                f"tool payload artifact is {artifact.state}",
            )
        try:
            payload = json.loads(
                catalog.artifact_path(trace.manifest.trace_id, artifact.artifact_id).read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError, TraceNotFoundError, TraceInvalidError) as error:
            raise JudgeInputError(
                "judge_evidence_artifact_invalid",
                f"tool payload artifact is invalid: {error}",
            ) from error
        candidates = payload.get("blocks")
        if not isinstance(candidates, list):
            candidates = payload.get("matches")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            block_id = candidate.get("block_id")
            text = candidate.get("text")
            if not isinstance(text, str):
                text = candidate.get("snippet")
            if not isinstance(block_id, str) or not isinstance(text, str):
                continue
            block = EvidenceBlock(
                block_id=block_id,
                doc_id=str(candidate.get("doc_id") or ""),
                doc_title=str(candidate.get("doc_title") or ""),
                source_label=str(candidate.get("source_label") or ""),
                section_path=[str(item) for item in candidate.get("section_path", [])],
                text=text,
                cited_in_answer=block_id in cited_set,
            )
            existing = by_block_id.get(block_id)
            if existing is None or (not existing.cited_in_answer and block.cited_in_answer):
                by_block_id[block_id] = block
    return list(by_block_id.values())


def _artifact_ref(value: object) -> TraceArtifactRecord | None:
    if not isinstance(value, dict):
        return None
    try:
        return TraceArtifactRecord.model_validate(value)
    except ValueError:
        return None


def _cited_block_ids(answer: str) -> list[str]:
    result: list[str] = []
    for match in CITATION_PATTERN.finditer(answer):
        for value in re.split(r"[,，、\s]+", match.group(1).strip()):
            if value and value not in result:
                result.append(value)
    return result


def _declares_truncation(answer: str) -> bool:
    normalized = answer.replace(" ", "")
    return any(
        marker in normalized for marker in ("内容未完", "尚未讲完", "回答未完成", "内容未完成")
    )
