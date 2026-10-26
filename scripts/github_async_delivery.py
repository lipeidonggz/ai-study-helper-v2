from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from scripts.watch_github_delivery import (
    DEFAULT_REPOSITORY,
    GITHUB_API_ROOT,
    REQUIRED_CHECKS,
    GitHubApiError,
    GitHubClient,
    enable_auto_merge,
    evaluate_delivery,
    fetch_check_runs,
    fetch_pull_request,
    load_github_token,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATUS_DIR = ROOT / "backend" / ".runtime" / "delivery"
DELIVERY_SCHEMA_VERSION = "github-async-delivery-v1"
DELIVERY_ID_PATTERN = re.compile(r"^delivery_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")


class DeliveryError(RuntimeError):
    """异步交付命令的稳定失败。"""


def utc_now_iso() -> str:
    """返回带 UTC offset 的时间戳。"""

    return datetime.now(UTC).isoformat()


def git(*args: str) -> bytes:
    """运行本地只读 Git 命令并返回原始标准输出。"""

    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise DeliveryError(message or f"git {' '.join(args)} failed")
    return completed.stdout


def git_text(*args: str) -> str:
    """运行本地只读 Git 命令并返回 UTF-8 文本。"""

    return git(*args).decode("utf-8").strip()


def write_delivery(record: dict[str, Any], status_dir: Path) -> Path:
    """原子写入一个交付记录，并更新 latest 指针。"""

    delivery_id = record.get("delivery_id")
    if not isinstance(delivery_id, str) or not DELIVERY_ID_PATTERN.fullmatch(delivery_id):
        raise DeliveryError("delivery id is invalid")
    status_dir.mkdir(parents=True, exist_ok=True)
    target = status_dir / f"{delivery_id}.json"
    temporary = target.with_name(f"{target.name}.{time.time_ns()}.tmp")
    payload = json.dumps(record, ensure_ascii=False, indent=2)
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(target)
    latest = status_dir / "latest.json"
    latest_temporary = latest.with_name(f"{latest.name}.{time.time_ns()}.tmp")
    latest_temporary.write_text(payload, encoding="utf-8")
    latest_temporary.replace(latest)
    return target


def read_delivery(
    status_dir: Path,
    delivery_id: str | None,
) -> tuple[Path, dict[str, Any]]:
    """读取指定交付；未指定时读取 latest 指针。"""

    if delivery_id is None:
        path = status_dir / "latest.json"
    else:
        if not DELIVERY_ID_PATTERN.fullmatch(delivery_id):
            raise DeliveryError("delivery id is invalid")
        path = status_dir / f"{delivery_id}.json"
    if not path.is_file():
        raise DeliveryError(f"delivery status does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DeliveryError(f"delivery status is unreadable: {path}") from error
    if not isinstance(payload, dict):
        raise DeliveryError(f"delivery status is not an object: {path}")
    return path, payload


def validate_branch_name(branch: str) -> None:
    """拒绝可能被解释为参数或非法 Git ref 的分支名。"""

    if not branch or branch.startswith("-") or branch.endswith("/"):
        raise DeliveryError("branch name is invalid")
    completed = subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise DeliveryError("branch name is invalid")


def list_changed_entries(base_commit: str, head_commit: str) -> list[tuple[str, str]]:
    """把 git diff 的 NUL 输出解析为需要写入或删除的路径。"""

    raw = git("diff", "--name-status", "-z", base_commit, head_commit)
    tokens = raw.decode("utf-8", errors="strict").split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        if not status:
            raise DeliveryError("git diff contains an empty status")
        if status[0] in {"R", "C"}:
            if index + 1 >= len(tokens):
                raise DeliveryError("git diff rename entry is incomplete")
            source_path = tokens[index]
            target_path = tokens[index + 1]
            index += 2
            if status[0] == "R":
                entries.append(("D", source_path))
            entries.append(("M", target_path))
            continue
        if index >= len(tokens):
            raise DeliveryError("git diff path entry is incomplete")
        path = tokens[index]
        index += 1
        entries.append(("D" if status == "D" else "M", path))
    return entries


def build_tree_entries(
    client: GitHubClient,
    *,
    repository: str,
    base_commit: str,
    head_commit: str,
) -> list[dict[str, Any]]:
    """把本地 commit 相对基线的变化上传为 GitHub tree entries。"""

    entries: list[dict[str, Any]] = []
    for status, path in list_changed_entries(base_commit, head_commit):
        if status == "D":
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            continue
        tree_line = git("ls-tree", "-z", head_commit, "--", path).decode("utf-8")
        if not tree_line:
            raise DeliveryError(f"changed path is missing from commit: {path}")
        metadata, observed_path = tree_line.rstrip("\0").split("\t", 1)
        mode, object_type, object_sha = metadata.split()
        if observed_path != path or object_type != "blob":
            raise DeliveryError(f"unsupported changed path type: {path}")
        content = git("cat-file", "blob", object_sha)
        blob = client.post_json(
            f"{GITHUB_API_ROOT}/repos/{repository}/git/blobs",
            {
                "content": base64.b64encode(content).decode("ascii"),
                "encoding": "base64",
            },
        )
        blob_sha = blob.get("sha")
        if not isinstance(blob_sha, str) or not blob_sha:
            raise DeliveryError(f"GitHub did not return a blob for: {path}")
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob_sha})
    return entries


def refresh_delivery(
    client: GitHubClient,
    record: dict[str, Any],
) -> dict[str, Any]:
    """从 GitHub 刷新一个交付记录；只有依赖点需要调用。"""

    repository = str(record["repository"])
    pr_number = int(record["pr_number"])
    pull_request = fetch_pull_request(client, repository, pr_number)
    head_sha = (pull_request.get("head") or {}).get("sha")
    if not isinstance(head_sha, str) or not head_sha:
        raise DeliveryError("pull request has no head commit")
    if head_sha != record.get("remote_head_sha"):
        return {
            **record,
            "state": "failed",
            "pr_state": pull_request.get("state"),
            "remote_head_sha": head_sha,
            "failure_code": "remote_head_changed",
            "failure_message": "pull request head no longer matches the delivered commit",
            "updated_at": utc_now_iso(),
        }
    check_runs = fetch_check_runs(client, repository, head_sha)
    evaluation = evaluate_delivery(pull_request, check_runs)
    checks = [asdict(check) for check in evaluation.checks]
    required_checks_passed = {check["name"] for check in checks} == set(REQUIRED_CHECKS) and all(
        check["status"] == "completed" and check["conclusion"] == "success" for check in checks
    )
    state = {
        "pending": "merge_pending" if required_checks_passed else "checks_pending",
        "merged": "merged",
        "failed": "failed",
    }[evaluation.state]
    result = {
        **record,
        "state": state,
        "pr_state": pull_request.get("state"),
        "mergeable_state": pull_request.get("mergeable_state"),
        "auto_merge_enabled": pull_request.get("auto_merge") is not None,
        "remote_head_sha": head_sha,
        "merge_commit_sha": pull_request.get("merge_commit_sha"),
        "checks": checks,
        "failure_code": evaluation.failure_code,
        "failure_message": evaluation.failure_message,
        "updated_at": utc_now_iso(),
    }
    if state == "merged":
        merge_commit_sha = pull_request.get("merge_commit_sha")
        if not isinstance(merge_commit_sha, str) or not merge_commit_sha:
            raise DeliveryError("merged pull request has no merge commit")
        compare = client.get_json(
            f"{GITHUB_API_ROOT}/repos/{repository}/compare/"
            f"{quote(merge_commit_sha, safe='')}...{quote(str(record['base_ref']), safe='')}"
        )
        if compare.get("status") not in {"ahead", "identical"}:
            result["state"] = "failed"
            result["failure_code"] = "merge_not_in_base"
            result["failure_message"] = "merge commit is not contained by the base branch"
    return result


def start_delivery(args: argparse.Namespace) -> int:
    """创建远端提交、分支、PR 和 auto-merge，然后立即返回。"""

    validate_branch_name(args.branch)
    commit = git_text("rev-parse", f"{args.commit}^{{commit}}")
    base_commit = git_text("rev-parse", f"{args.base_commit}^{{commit}}")
    client = GitHubClient(load_github_token())

    base_ref = client.get_json(
        f"{GITHUB_API_ROOT}/repos/{args.repo}/git/ref/heads/{quote(args.base_ref, safe='')}"
    )
    base_sha = ((base_ref.get("object") or {}).get("sha")) if isinstance(base_ref, dict) else None
    if not isinstance(base_sha, str) or not base_sha:
        raise DeliveryError("base branch has no commit")
    base_commit_payload = client.get_json(
        f"{GITHUB_API_ROOT}/repos/{args.repo}/git/commits/{base_sha}"
    )
    base_tree_sha = (base_commit_payload.get("tree") or {}).get("sha")
    if not isinstance(base_tree_sha, str) or not base_tree_sha:
        raise DeliveryError("base branch has no tree")
    if git_text("rev-parse", f"{base_commit}^{{tree}}") != base_tree_sha:
        raise DeliveryError(
            "local base tree does not match the current remote base; rebase or update first"
        )

    tree_entries = build_tree_entries(
        client,
        repository=args.repo,
        base_commit=base_commit,
        head_commit=commit,
    )
    tree = client.post_json(
        f"{GITHUB_API_ROOT}/repos/{args.repo}/git/trees",
        {"base_tree": base_tree_sha, "tree": tree_entries},
    )
    tree_sha = tree.get("sha")
    if not isinstance(tree_sha, str) or not tree_sha:
        raise DeliveryError("GitHub did not return a tree")
    message = git_text("show", "-s", "--format=%B", commit).strip()
    if not message:
        raise DeliveryError("commit message is empty")
    identity_fields = git_text(
        "show",
        "-s",
        "--format=%an%n%ae%n%aI",
        commit,
    ).splitlines()
    if len(identity_fields) != 3:
        raise DeliveryError("commit identity is unavailable")
    identity = {
        "name": identity_fields[0],
        "email": identity_fields[1],
        "date": identity_fields[2],
    }
    remote_commit = client.post_json(
        f"{GITHUB_API_ROOT}/repos/{args.repo}/git/commits",
        {
            "message": message,
            "tree": tree_sha,
            "parents": [base_sha],
            "author": identity,
            "committer": identity,
        },
    )
    remote_sha = remote_commit.get("sha")
    if not isinstance(remote_sha, str) or not remote_sha:
        raise DeliveryError("GitHub did not return a commit")
    client.post_json(
        f"{GITHUB_API_ROOT}/repos/{args.repo}/git/refs",
        {"ref": f"refs/heads/{args.branch}", "sha": remote_sha},
    )
    title = message.splitlines()[0]
    body = "\n".join(message.splitlines()[1:]).strip()
    pull_request = client.post_json(
        f"{GITHUB_API_ROOT}/repos/{args.repo}/pulls",
        {"title": title, "head": args.branch, "base": args.base_ref, "body": body},
    )
    enable_auto_merge(client, pull_request)
    pr_number = pull_request.get("number")
    if not isinstance(pr_number, int):
        raise DeliveryError("GitHub did not return a pull request number")
    started_at = utc_now_iso()
    delivery_id = "delivery_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + remote_sha[:8]
    record: dict[str, Any] = {
        "schema_version": DELIVERY_SCHEMA_VERSION,
        "delivery_id": delivery_id,
        "repository": args.repo,
        "base_ref": args.base_ref,
        "base_sha": base_sha,
        "base_tree_sha": base_tree_sha,
        "local_commit_sha": commit,
        "remote_head_sha": remote_sha,
        "branch": args.branch,
        "pr_number": pr_number,
        "pr_url": pull_request.get("html_url"),
        "state": "submitted",
        "created_at": started_at,
        "updated_at": started_at,
    }
    record = refresh_delivery(client, record)
    path = write_delivery(record, args.status_dir)
    print(json.dumps({"status_path": str(path), **record}, ensure_ascii=False, indent=2))
    return 0


def show_status(args: argparse.Namespace) -> int:
    """查询并刷新一次交付状态，供主对话或外部脚本读取。"""

    path, record = read_delivery(args.status_dir, args.delivery_id)
    record = refresh_delivery(GitHubClient(load_github_token()), record)
    write_delivery(record, args.status_dir)
    print(json.dumps({"status_path": str(path), **record}, ensure_ascii=False, indent=2))
    return 0


def require_merged(args: argparse.Namespace) -> int:
    """依赖门禁：确认合并已进入目标分支后才允许后续工作。"""

    deadline = time.monotonic() + args.timeout_seconds
    while True:
        path, record = read_delivery(args.status_dir, args.delivery_id)
        record = refresh_delivery(GitHubClient(load_github_token()), record)
        write_delivery(record, args.status_dir)
        state = record.get("state")
        if state == "merged":
            print(
                json.dumps(
                    {
                        "result": "ready",
                        "status_path": str(path),
                        "delivery_id": record["delivery_id"],
                        "merge_commit_sha": record["merge_commit_sha"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if state == "failed":
            print(
                json.dumps(
                    {
                        "result": "failed",
                        "status_path": str(path),
                        **record,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 1
        if not args.wait:
            print(
                json.dumps(
                    {
                        "result": "blocked",
                        "status_path": str(path),
                        "delivery_id": record["delivery_id"],
                        "state": state,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 3
        if time.monotonic() >= deadline:
            print(
                json.dumps(
                    {
                        "result": "timed_out",
                        "status_path": str(path),
                        "delivery_id": record["delivery_id"],
                        "state": state,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 2
        time.sleep(args.interval_seconds)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析异步交付命令。"""

    parser = argparse.ArgumentParser(description="Manage asynchronous GitHub delivery.")
    parser.add_argument(
        "--status-dir",
        type=Path,
        default=DEFAULT_STATUS_DIR,
        help="directory containing delivery status files",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="publish HEAD and enable auto-merge")
    start.add_argument("--repo", default=DEFAULT_REPOSITORY)
    start.add_argument("--base-ref", default="main")
    start.add_argument("--base-commit", default="HEAD^")
    start.add_argument("--commit", default="HEAD")
    start.add_argument("--branch", required=True)

    status = subparsers.add_parser("status", help="refresh one delivery status")
    status.add_argument("--delivery-id")

    require = subparsers.add_parser(
        "require-merged",
        help="gate dependent work on a merged delivery",
    )
    require.add_argument("--delivery-id")
    require.add_argument("--wait", action="store_true")
    require.add_argument("--timeout-seconds", type=int, default=1_800)
    require.add_argument("--interval-seconds", type=int, default=20)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """执行异步交付命令并返回稳定退出码。"""

    args = parse_args(argv)
    args.status_dir = args.status_dir.resolve()
    try:
        if args.command == "start":
            return start_delivery(args)
        if args.command == "status":
            return show_status(args)
        if args.command == "require-merged":
            if args.timeout_seconds <= 0 or args.interval_seconds <= 0:
                raise DeliveryError("wait intervals must be positive")
            return require_merged(args)
        raise DeliveryError(f"unsupported command: {args.command}")
    except (DeliveryError, GitHubApiError) as error:
        print(json.dumps({"result": "error", "message": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
