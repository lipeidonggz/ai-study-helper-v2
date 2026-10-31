from __future__ import annotations

from app.agent import corpus_grounded_coverage


def test_corpus_grounded_prompts_do_not_embed_evaluation_case_terms() -> None:
    prompt_text = "\n".join(
        (
            corpus_grounded_coverage.EXPLANATION_PROMPT,
            corpus_grounded_coverage.FACET_PROMPT,
            corpus_grounded_coverage.FACET_AUDIT_PROMPT,
            corpus_grounded_coverage.RERANK_PROMPT,
        )
    )
    forbidden_terms = (
        "worktree",
        "checkpoint",
        "revert",
        "sandbox",
        "virtual machine",
        "egress",
        "ai agent 隔离与控制边界",
        "沙箱",
        "虚拟机",
        "出口控制",
        "检查点",
        "回滚",
        "并行任务",
        "生命周期",
        "状态变更",
        "红队",
    )

    leaked_terms = [term for term in forbidden_terms if term.casefold() in prompt_text.casefold()]

    assert leaked_terms == []
