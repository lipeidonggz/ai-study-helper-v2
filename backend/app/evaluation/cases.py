from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast

from app.evaluation.models import CaseSeed, CriterionSeed

_CASE_ROW_PATTERN = re.compile(r"^\|\s*`(?P<case_id>c\d+-001|cf-\d+)`")
_CRITERION_PATTERN = re.compile(
    r"·\s+\*\*(?P<id>[RX]\d+)\s+(?P<title>.*?)\*\*(?:——|\s+)"
    r"(?P<body>.*?)(?=<br>\s*·\s+\*\*[RX]\d+\s+|$)"
)
_TARGET_TITLES: dict[str, tuple[str, ...]] = {
    "c1-001": ("How we contain Claude",),
    "c2-001": ("How we contain Claude",),
    "c3-001": ("How we contain Claude",),
    "c4-001": ("How we contain Claude",),
    "c5-001": ("How we contain Claude",),
    "c6-001": ("How we contain Claude",),
    "c7-001": ("How we contain Claude",),
    "c8-001": ("Harness engineering", "How we contain Claude"),
    "c9-001": ("Harness engineering", "How we contain Claude"),
    "c10-001": ("Harness engineering",),
    "c11-001": ("Harness engineering", "How we contain Claude"),
    "c12-001": ("How we contain Claude",),
    "c13-001": (),
    "c14-001": ("How we contain Claude",),
    "c15-001": ("How we contain Claude",),
    "cf-001": (),
    "cf-002": (),
    "cf-003": (
        "Building Effective",
        "New tools for building agents",
        "Harness engineering",
        "How we contain Claude",
    ),
}
_COMPLEX_CASE_IDS = {
    "c6-001",
    "c7-001",
    "c8-001",
    "c9-001",
    "c11-001",
    "c13-001",
    "cf-003",
}


def default_case_source_path() -> Path:
    """返回设计 03 基线路径；允许开发脚本显式覆盖。"""

    override = os.environ.get("ASH_EVAL_CASE_SOURCE")
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[3] / "docs" / "design" / "03-cases-and-gold-standard.md"


def load_design_case_seeds(source_path: Path | None = None) -> list[CaseSeed]:
    """解析设计 03 的两张用例表，得到 18 条确定性种子。

    解析结果固定为每条 case 的 ``r1`` active revision。问题、R／X 判定项
    和来源内容 hash 同属 revision；后续编辑只能追加新 revision。
    """

    path = (source_path or default_case_source_path()).resolve()
    lines = path.read_text(encoding="utf-8").splitlines()
    seeds: list[CaseSeed] = []
    for line_number, line in enumerate(lines, start=1):
        match = _CASE_ROW_PATTERN.match(line)
        if match is None:
            continue
        columns = _split_markdown_row(line)
        if len(columns) < 4:
            raise ValueError(f"invalid case row at {path}:{line_number}")
        case_id = match.group("case_id")
        category = _plain_text(columns[1])
        question = _normalize_question(columns[2])
        criterion_text = columns[3]
        criteria = _parse_criteria(case_id, criterion_text)
        if not criteria:
            raise ValueError(f"case {case_id} has no R/X criteria")
        targets = [
            {
                "title_contains": title,
                "section_path": [],
            }
            for title in _TARGET_TITLES[case_id]
        ]
        source_ref = f"docs/design/03-cases-and-gold-standard.md#L{line_number}"
        execution_policy = {
            "latency_class": "complex" if case_id in _COMPLEX_CASE_IDS else "simple",
        }
        content_hash = _case_content_hash(
            case_id=case_id,
            category=category,
            question=question,
            target_bindings=targets,
            execution_policy=execution_policy,
            criteria=criteria,
        )
        seeds.append(
            CaseSeed(
                case_id=case_id,
                revision_id=f"cr_{case_id.replace('-', '_')}_r1",
                category=category,
                question=question,
                target_bindings=targets,
                execution_policy=execution_policy,
                source_ref=source_ref,
                content_hash=content_hash,
                criteria=criteria,
            )
        )
    if len(seeds) != 18:
        raise ValueError(f"design 03 must yield 18 cases, got {len(seeds)}")
    return seeds


def _split_markdown_row(line: str) -> list[str]:
    return [column.strip() for column in line.strip().strip("|").split("|")]


def _plain_text(value: str) -> str:
    text = value.replace("<br>", " ")
    text = re.sub(r"[*`]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_question(value: str) -> str:
    question = value.strip()
    if len(question) >= 2 and question[0] == question[-1] == '"':
        question = question[1:-1]
    return question.strip()


def _parse_criteria(case_id: str, value: str) -> list[CriterionSeed]:
    criteria: list[CriterionSeed] = []
    for match in _CRITERION_PATTERN.finditer(value):
        code = match.group("id")
        rule_type = code[0]
        if rule_type not in {"R", "X"}:
            continue
        ordinal = int(code[1:])
        criterion_slug = case_id.replace("-", "_")
        criteria.append(
            CriterionSeed(
                criterion_id=f"{case_id}-{code.lower()}",
                revision_id=f"ccr_{criterion_slug}_{code.lower()}_r1",
                rule_type=cast(Literal["R", "X"], rule_type),
                ordinal=ordinal,
                title=_plain_text(match.group("title")),
                rule_text=_plain_text(match.group("body")),
            )
        )
    return criteria


def _case_content_hash(
    *,
    case_id: str,
    category: str,
    question: str,
    target_bindings: list[dict[str, object]],
    execution_policy: Mapping[str, object],
    criteria: list[CriterionSeed],
) -> str:
    payload = {
        "case_id": case_id,
        "category": category,
        "question": question,
        "target_bindings": target_bindings,
        "execution_policy": execution_policy,
        "criteria": [
            {
                "criterion_id": criterion.criterion_id,
                "rule_type": criterion.rule_type,
                "ordinal": criterion.ordinal,
                "title": criterion.title,
                "rule_text": criterion.rule_text,
            }
            for criterion in criteria
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
