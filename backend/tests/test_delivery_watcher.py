from __future__ import annotations

from scripts.watch_github_delivery import evaluate_delivery, latest_required_checks


def _check(name: str, *, conclusion: str = "success", run_id: int = 1) -> dict:
    return {
        "id": run_id,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "started_at": f"2026-10-22T03:00:{run_id:02d}Z",
        "html_url": f"https://github.example/{name}/{run_id}",
    }


def _pr(*, auto_merge: bool = True) -> dict:
    return {
        "state": "open",
        "merged": False,
        "mergeable": True,
        "mergeable_state": "blocked",
        "auto_merge": {"enabled_by": "tester"} if auto_merge else None,
        "html_url": "https://github.example/pr/1",
        "merge_commit_sha": None,
        "head": {"sha": "abc123"},
    }


def test_delivery_is_merged_when_pull_request_reports_merged() -> None:
    pull_request = _pr()
    pull_request["merged"] = True

    evaluation = evaluate_delivery(pull_request, [])

    assert evaluation.state == "merged"
    assert evaluation.failure_code is None


def test_delivery_is_pending_when_required_checks_succeed_and_auto_merge_is_enabled() -> None:
    evaluation = evaluate_delivery(
        _pr(),
        [_check("backend-and-frontend"), _check("desktop")],
    )

    assert evaluation.state == "pending"
    assert {check.conclusion for check in evaluation.checks} == {"success"}


def test_delivery_fails_when_latest_required_check_fails() -> None:
    evaluation = evaluate_delivery(
        _pr(),
        [
            _check("backend-and-frontend"),
            _check("desktop", conclusion="failure", run_id=2),
        ],
    )

    assert evaluation.state == "failed"
    assert evaluation.failure_code == "checks_failed"
    assert evaluation.failure_message == "required check failed: desktop (failure)"


def test_latest_check_run_wins_when_push_and_pr_trigger_duplicates() -> None:
    checks = latest_required_checks(
        [
            _check("desktop", conclusion="failure", run_id=1),
            _check("desktop", conclusion="success", run_id=2),
        ]
    )

    assert len(checks) == 1
    assert checks[0].conclusion == "success"


def test_delivery_fails_when_pull_request_has_conflicts() -> None:
    pull_request = _pr()
    pull_request["mergeable"] = False
    pull_request["mergeable_state"] = "dirty"

    evaluation = evaluate_delivery(pull_request, [])

    assert evaluation.state == "failed"
    assert evaluation.failure_code == "merge_conflict"


def test_delivery_fails_when_auto_merge_was_not_enabled() -> None:
    evaluation = evaluate_delivery(
        _pr(auto_merge=False),
        [_check("backend-and-frontend"), _check("desktop")],
    )

    assert evaluation.state == "failed"
    assert evaluation.failure_code == "auto_merge_disabled"


def test_delivery_waits_when_required_checks_are_not_complete() -> None:
    evaluation = evaluate_delivery(
        _pr(),
        [
            {
                **_check("backend-and-frontend"),
                "status": "in_progress",
                "conclusion": None,
            },
            _check("desktop"),
        ],
    )

    assert evaluation.state == "pending"
