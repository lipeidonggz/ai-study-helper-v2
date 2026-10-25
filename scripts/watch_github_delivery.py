from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GITHUB_API_ROOT = "https://api.github.com"
GITHUB_GRAPHQL_URL = f"{GITHUB_API_ROOT}/graphql"
DEFAULT_REPOSITORY = "lipeidonggz/ai-study-helper-v2"
REQUIRED_CHECKS = ("backend-and-frontend", "desktop")
FAILED_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "failure",
    "skipped",
    "stale",
    "startup_failure",
    "timed_out",
}


class GitHubApiError(RuntimeError):
    """GitHub API 请求失败；消息不包含认证凭证。"""


class GitHubClient:
    """使用 Windows Git 凭据访问 GitHub REST 与 GraphQL API。"""

    def __init__(self, token: str) -> None:
        self._token = token

    def get_json(self, url: str) -> dict[str, Any]:
        """读取一个 JSON object；失败时转换为稳定异常。"""

        payload = self._request(url, method="GET")
        if not isinstance(payload, dict):
            raise GitHubApiError("GitHub API returned a non-object payload")
        return payload

    def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """提交一个 JSON object；失败时转换为稳定异常。"""

        response = self._request(url, method="POST", payload=payload)
        if not isinstance(response, dict):
            raise GitHubApiError("GitHub API returned a non-object payload")
        return response

    def _request(
        self,
        url: str,
        *,
        method: str,
        payload: dict[str, Any] | None = None,
    ) -> object:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "ai-study-helper-v2-delivery-watcher",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as error:
            raise GitHubApiError(f"GitHub API returned HTTP {error.code}") from error
        except TimeoutError as error:
            raise GitHubApiError("GitHub API request timed out") from error
        except URLError as error:
            raise GitHubApiError(f"GitHub API is unreachable: {error.reason}") from error
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise GitHubApiError("GitHub API returned invalid JSON") from error


@dataclass(frozen=True, slots=True)
class RequiredCheck:
    """一个 required check 的最新可见状态。"""

    name: str
    status: str
    conclusion: str | None
    html_url: str | None


@dataclass(frozen=True, slots=True)
class DeliveryEvaluation:
    """一次 PR 轮询的确定性分类结果。"""

    state: str
    failure_code: str | None
    failure_message: str | None
    checks: tuple[RequiredCheck, ...]


def utc_now_iso() -> str:
    """返回带 UTC offset 的当前时间。"""

    return datetime.now(UTC).isoformat()


def load_github_token() -> str:
    """从 Git 凭据存储读取 GitHub token，不写入磁盘或日志。"""

    try:
        completed = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise GitHubApiError("GitHub credentials are unavailable") from error
    for line in completed.stdout.splitlines():
        if line.startswith("password="):
            token = line.partition("=")[2].strip()
            if token:
                return token
    raise GitHubApiError("GitHub credential does not contain a token")


def latest_required_checks(
    check_runs: list[dict[str, Any]],
    required_names: tuple[str, ...] = REQUIRED_CHECKS,
) -> tuple[RequiredCheck, ...]:
    """按检查名选取最新一次运行，避免 push／PR 重复触发时误用旧结果。"""

    latest: dict[str, dict[str, Any]] = {}
    for run in check_runs:
        name = run.get("name")
        if name not in required_names:
            continue
        current = latest.get(name)
        if current is None or _check_recency_key(run) >= _check_recency_key(current):
            latest[name] = run
    return tuple(
        RequiredCheck(
            name=name,
            status=str(latest[name].get("status") or "queued"),
            conclusion=latest[name].get("conclusion"),
            html_url=latest[name].get("html_url"),
        )
        for name in required_names
        if name in latest
    )


def evaluate_delivery(
    pull_request: dict[str, Any],
    check_runs: list[dict[str, Any]],
    *,
    required_names: tuple[str, ...] = REQUIRED_CHECKS,
    require_auto_merge: bool = True,
) -> DeliveryEvaluation:
    """判断 PR 已合并、仍在等待，或已经进入不可继续的失败状态。"""

    checks = latest_required_checks(check_runs, required_names)
    if pull_request.get("merged") is True:
        return DeliveryEvaluation("merged", None, None, checks)
    if pull_request.get("state") == "closed":
        return DeliveryEvaluation(
            "failed",
            "pr_closed",
            "pull request was closed without merging",
            checks,
        )
    if pull_request.get("mergeable") is False and pull_request.get("mergeable_state") == "dirty":
        return DeliveryEvaluation(
            "failed",
            "merge_conflict",
            "pull request has merge conflicts",
            checks,
        )
    failed = next(
        (
            check
            for check in checks
            if check.status == "completed" and check.conclusion in FAILED_CONCLUSIONS
        ),
        None,
    )
    if failed is not None:
        return DeliveryEvaluation(
            "failed",
            "checks_failed",
            f"required check failed: {failed.name} ({failed.conclusion})",
            checks,
        )
    if require_auto_merge and pull_request.get("auto_merge") is None:
        return DeliveryEvaluation(
            "failed",
            "auto_merge_disabled",
            "pull request does not have auto-merge enabled",
            checks,
        )
    return DeliveryEvaluation("pending", None, None, checks)


def enable_auto_merge(client: GitHubClient, pull_request: dict[str, Any]) -> None:
    """为尚未启用的 PR 开启 merge commit auto-merge。"""

    if pull_request.get("auto_merge") is not None:
        return
    payload = {
        "query": (
            "mutation EnableAutoMerge($pullRequestId: ID!) {"
            " enablePullRequestAutoMerge(input: {"
            " pullRequestId: $pullRequestId, mergeMethod: MERGE"
            " }) { pullRequest { number autoMergeRequest { enabledAt } } }"
            "}"
        ),
        "variables": {"pullRequestId": pull_request["node_id"]},
    }
    response = client.post_json(GITHUB_GRAPHQL_URL, payload)
    errors = response.get("errors")
    if errors:
        message = errors[0].get("message") if isinstance(errors[0], dict) else "unknown error"
        raise GitHubApiError(f"could not enable auto-merge: {message}")


def write_status(path: Path, status: dict[str, Any]) -> None:
    """原子更新 watcher 状态，避免读取到半写入 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def build_status(
    *,
    repository: str,
    pr_number: int,
    pull_request: dict[str, Any],
    evaluation: DeliveryEvaluation,
    started_at: str,
    attempt: int,
    last_error: str | None = None,
) -> dict[str, Any]:
    """构造供人工和后续 Agent 共同读取的状态快照。"""

    return {
        "schema_version": "github-delivery-watch-v1",
        "repository": repository,
        "pr_number": pr_number,
        "pr_url": pull_request.get("html_url"),
        "pr_state": pull_request.get("state"),
        "mergeable_state": pull_request.get("mergeable_state"),
        "auto_merge_enabled": pull_request.get("auto_merge") is not None,
        "head_sha": (pull_request.get("head") or {}).get("sha"),
        "merge_commit_sha": pull_request.get("merge_commit_sha"),
        "result": evaluation.state,
        "failure_code": evaluation.failure_code,
        "failure_message": evaluation.failure_message,
        "last_error": last_error,
        "checks": [asdict(check) for check in evaluation.checks],
        "started_at": started_at,
        "updated_at": utc_now_iso(),
        "attempt": attempt,
    }


def fetch_pull_request(client: GitHubClient, repository: str, pr_number: int) -> dict[str, Any]:
    """读取 PR 的合并、冲突与 auto-merge 状态。"""

    return client.get_json(f"{GITHUB_API_ROOT}/repos/{repository}/pulls/{pr_number}")


def fetch_check_runs(client: GitHubClient, repository: str, head_sha: str) -> list[dict[str, Any]]:
    """读取 PR head commit 的全部 GitHub Actions check runs。"""

    payload = client.get_json(
        f"{GITHUB_API_ROOT}/repos/{repository}/commits/{head_sha}/check-runs?per_page=100"
    )
    runs = payload.get("check_runs")
    if not isinstance(runs, list):
        raise GitHubApiError("GitHub checks response has no check_runs list")
    return [run for run in runs if isinstance(run, dict)]


def run_watcher(args: argparse.Namespace) -> int:
    """轮询 PR，持续落盘状态，并在终态或超时时退出。"""

    output_path = Path(args.output).resolve()
    started_at = utc_now_iso()
    deadline = time.monotonic() + args.timeout_minutes * 60
    consecutive_errors = 0
    pull_request: dict[str, Any] = {}
    initialization_failure: tuple[str, str] | None = None
    try:
        client = GitHubClient(load_github_token())
        pull_request = fetch_pull_request(client, args.repo, args.pr)
        if args.enable_auto_merge:
            try:
                enable_auto_merge(client, pull_request)
            except GitHubApiError as error:
                initialization_failure = ("auto_merge_enable_failed", str(error))
            else:
                pull_request = fetch_pull_request(client, args.repo, args.pr)
    except GitHubApiError as error:
        initialization_failure = ("watcher_start_failed", str(error))

    if initialization_failure is not None:
        failure_code, failure_message = initialization_failure
        status = build_status(
            repository=args.repo,
            pr_number=args.pr,
            pull_request=pull_request,
            evaluation=DeliveryEvaluation(
                "failed",
                failure_code,
                failure_message,
                (),
            ),
            started_at=started_at,
            attempt=0,
        )
        write_status(output_path, status)
        print(f"delivery watcher failed: {failure_message}", file=sys.stderr)
        return 1

    attempt = 0
    last_error: str | None = None
    while True:
        attempt += 1
        try:
            pull_request = fetch_pull_request(client, args.repo, args.pr)
            head_sha = (pull_request.get("head") or {}).get("sha")
            if not isinstance(head_sha, str) or not head_sha:
                raise GitHubApiError("pull request has no head commit")
            check_runs = fetch_check_runs(client, args.repo, head_sha)
            evaluation = evaluate_delivery(
                pull_request,
                check_runs,
                require_auto_merge=args.require_auto_merge,
            )
            last_error = None
            consecutive_errors = 0
        except GitHubApiError as error:
            consecutive_errors += 1
            last_error = str(error)
            evaluation = DeliveryEvaluation("pending", None, None, ())
            if consecutive_errors >= 3:
                evaluation = DeliveryEvaluation(
                    "failed",
                    "github_api_error",
                    last_error,
                    (),
                )

        status = build_status(
            repository=args.repo,
            pr_number=args.pr,
            pull_request=pull_request,
            evaluation=evaluation,
            started_at=started_at,
            attempt=attempt,
            last_error=last_error,
        )
        write_status(output_path, status)

        if evaluation.state in {"merged", "failed"}:
            return 0 if evaluation.state == "merged" else 1
        if args.once:
            return 0
        if time.monotonic() >= deadline:
            status["result"] = "timed_out"
            status["failure_code"] = "timeout"
            status["failure_message"] = "delivery watcher reached its timeout"
            status["updated_at"] = utc_now_iso()
            write_status(output_path, status)
            return 2
        time.sleep(min(args.interval, max(1, deadline - time.monotonic())))


def _check_recency_key(run: dict[str, Any]) -> tuple[str, int]:
    started_at = run.get("started_at")
    run_id = run.get("id")
    return (
        started_at if isinstance(started_at, str) else "",
        run_id if isinstance(run_id, int) else 0,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析 watcher 命令行参数。"""

    parser = argparse.ArgumentParser(description="Watch and auto-merge one GitHub pull request.")
    parser.add_argument("--pr", type=int, required=True, help="pull request number")
    parser.add_argument("--repo", default=DEFAULT_REPOSITORY, help="owner/repository")
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parents[1] / "backend/.runtime/delivery-status.json"),
        help="JSON status output path",
    )
    parser.add_argument("--interval", type=int, default=20, help="polling interval in seconds")
    parser.add_argument("--timeout-minutes", type=int, default=30, help="overall timeout")
    parser.add_argument(
        "--enable-auto-merge",
        action="store_true",
        help="enable auto-merge before polling",
    )
    parser.add_argument(
        "--no-require-auto-merge",
        action="store_false",
        dest="require_auto_merge",
        help="report pending instead of failing when auto-merge is absent",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="write one non-failing observation and exit",
    )
    parser.set_defaults(require_auto_merge=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """运行交付 watcher 并返回稳定退出码。"""

    try:
        return run_watcher(parse_args(argv))
    except (GitHubApiError, ValueError) as error:
        print(f"delivery watcher failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
