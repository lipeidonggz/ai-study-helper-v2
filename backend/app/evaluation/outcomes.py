from __future__ import annotations

from typing import Literal

AttemptOutcomeClass = Literal["product", "infrastructure", "canceled", "unknown"]

INFRASTRUCTURE_FAILURE_CODES = frozenset(
    {
        "embedding_api_key_missing",
        "embedding_upstream_unavailable",
        "evaluation_attempt_exception",
        "evaluation_attempt_timeout",
        "evaluation_process_restarted",
        "evaluation_worker_exception",
        "harness_internal_error",
    }
)


def classify_attempt_outcome(
    execution_status: str,
    error_code: str | None,
) -> AttemptOutcomeClass:
    """把执行结果区分为有效产品样本和基础设施故障。

    未识别的 failed 原因默认归为产品结果，避免自动替代掩盖真实失败。
    provider 的错误码统一归为基础设施故障，不计入有效 N。
    """

    if execution_status in {"succeeded", "partial"}:
        return "product"
    if execution_status == "canceled":
        return "canceled"
    if execution_status != "failed":
        return "unknown"
    if error_code in INFRASTRUCTURE_FAILURE_CODES:
        return "infrastructure"
    if error_code and error_code.startswith(("chat_", "embedding_")):
        return "infrastructure"
    return "product"


def is_product_sample(outcome_class: str) -> bool:
    """返回该 attempt 是否应计入有效 N 并进入判官。"""

    return outcome_class == "product"
