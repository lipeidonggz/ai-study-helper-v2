from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest
from scripts.github_async_delivery import (
    DeliveryError,
    build_tree_entries,
    list_changed_entries,
    read_delivery,
    refresh_delivery,
    require_merged,
    write_delivery,
)

DELIVERY_ID = "delivery_20261026T041122Z_d08dbfcc"


class _FakeGitHubClient:
    def __init__(
        self,
        *,
        check_conclusions: list[str | None],
        merged: bool = False,
        compare_status: str = "ahead",
        head_sha: str = "d" * 40,
    ) -> None:
        self._check_conclusions = check_conclusions
        self._merged = merged
        self._compare_status = compare_status
        self._head_sha = head_sha

    def get_json(self, url: str) -> dict[str, Any]:
        if "/pulls/" in url:
            return {
                "state": "closed" if self._merged else "open",
                "merged": self._merged,
                "mergeable": True,
                "mergeable_state": "unknown" if self._merged else "blocked",
                "auto_merge": {"enabled_by": "tester"},
                "html_url": "https://github.example/pr/7",
                "merge_commit_sha": "f" * 40 if self._merged else None,
                "head": {"sha": self._head_sha},
            }
        if "/check-runs" in url:
            return {
                "check_runs": [
                    {
                        "id": index,
                        "name": name,
                        "status": ("completed" if conclusion is not None else "in_progress"),
                        "conclusion": conclusion,
                        "started_at": f"2026-10-26T04:00:{index:02d}Z",
                        "html_url": f"https://github.example/check/{index}",
                    }
                    for index, (name, conclusion) in enumerate(
                        zip(
                            ("backend-and-frontend", "desktop"),
                            self._check_conclusions,
                            strict=True,
                        ),
                        start=1,
                    )
                ]
            }
        if "/compare/" in url:
            return {"status": self._compare_status}
        raise AssertionError(f"unexpected URL: {url}")


def _delivery_record() -> dict[str, Any]:
    return {
        "schema_version": "github-async-delivery-v1",
        "delivery_id": DELIVERY_ID,
        "repository": "owner/repository",
        "base_ref": "main",
        "base_sha": "a" * 40,
        "base_tree_sha": "b" * 40,
        "local_commit_sha": "c" * 40,
        "remote_head_sha": "d" * 40,
        "branch": "feat/async-delivery",
        "pr_number": 7,
        "pr_url": "https://github.example/pr/7",
        "state": "submitted",
        "created_at": "2026-10-26T04:11:22+00:00",
        "updated_at": "2026-10-26T04:11:22+00:00",
    }


def test_delivery_status_changes_from_checks_pending_to_merge_pending() -> None:
    pending = refresh_delivery(
        _FakeGitHubClient(check_conclusions=[None, "success"]),  # type: ignore[arg-type]
        _delivery_record(),
    )
    merge_pending = refresh_delivery(
        _FakeGitHubClient(check_conclusions=["success", "success"]),  # type: ignore[arg-type]
        _delivery_record(),
    )

    assert pending["state"] == "checks_pending"
    assert merge_pending["state"] == "merge_pending"


def test_merged_delivery_is_ready_only_when_merge_is_in_base() -> None:
    ready = refresh_delivery(
        _FakeGitHubClient(  # type: ignore[arg-type]
            check_conclusions=["success", "success"],
            merged=True,
        ),
        _delivery_record(),
    )
    diverged = refresh_delivery(
        _FakeGitHubClient(  # type: ignore[arg-type]
            check_conclusions=["success", "success"],
            merged=True,
            compare_status="diverged",
        ),
        _delivery_record(),
    )

    assert ready["state"] == "merged"
    assert ready["merge_commit_sha"] == "f" * 40
    assert diverged["state"] == "failed"
    assert diverged["failure_code"] == "merge_not_in_base"


def test_refresh_fails_when_pull_request_head_changed() -> None:
    result = refresh_delivery(
        _FakeGitHubClient(  # type: ignore[arg-type]
            check_conclusions=["success", "success"],
            head_sha="e" * 40,
        ),
        _delivery_record(),
    )

    assert result["state"] == "failed"
    assert result["failure_code"] == "remote_head_changed"


def test_delivery_status_file_has_latest_pointer_and_strict_id(
    tmp_path: Path,
) -> None:
    record = _delivery_record()

    written = write_delivery(record, tmp_path)
    _, latest = read_delivery(tmp_path, None)
    _, selected = read_delivery(tmp_path, DELIVERY_ID)

    assert written.name == f"{DELIVERY_ID}.json"
    assert latest["delivery_id"] == DELIVERY_ID
    assert selected["branch"] == "feat/async-delivery"
    assert (
        json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))["delivery_id"]
        == DELIVERY_ID
    )
    with pytest.raises(DeliveryError, match="delivery id is invalid"):
        read_delivery(tmp_path, "../../escape")


def test_changed_entries_handle_modify_rename_and_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "scripts.github_async_delivery.git",
        lambda *_args: b"M\0edited.py\0R100\0old.py\0new.py\0D\0gone.py\0",
    )

    assert list_changed_entries("base", "head") == [
        ("M", "edited.py"),
        ("D", "old.py"),
        ("M", "new.py"),
        ("D", "gone.py"),
    ]


def test_tree_entries_upload_changed_blob_with_repository_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploaded: list[tuple[str, dict[str, Any]]] = []

    class _BlobClient:
        def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, str]:
            uploaded.append((url, payload))
            return {"sha": "b" * 40}

    def fake_git(*args: str) -> bytes:
        if args[0] == "diff":
            return b"M\0src/example.py\0"
        if args[0] == "ls-tree":
            return b"100755 blob cccccccccccccccccccccccccccccccccccccccc\tsrc/example.py\0"
        if args[0] == "cat-file":
            return b"print('ok')\n"
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr("scripts.github_async_delivery.git", fake_git)

    entries = build_tree_entries(
        _BlobClient(),  # type: ignore[arg-type]
        repository="owner/repository",
        base_commit="a" * 40,
        head_commit="d" * 40,
    )

    assert entries == [
        {
            "path": "src/example.py",
            "mode": "100755",
            "type": "blob",
            "sha": "b" * 40,
        }
    ]
    assert uploaded == [
        (
            "https://api.github.com/repos/owner/repository/git/blobs",
            {
                "content": "cHJpbnQoJ29rJykK",
                "encoding": "base64",
            },
        )
    ]


@pytest.mark.parametrize(
    ("state", "expected_exit"),
    [
        ("checks_pending", 3),
        ("merged", 0),
        ("failed", 1),
    ],
)
def test_require_merged_maps_delivery_state_to_gate_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: str,
    expected_exit: int,
) -> None:
    record = {**_delivery_record(), "state": state, "merge_commit_sha": "f" * 40}
    status_path = tmp_path / f"{DELIVERY_ID}.json"

    monkeypatch.setattr(
        "scripts.github_async_delivery.read_delivery",
        lambda *_args: (status_path, record),
    )
    monkeypatch.setattr(
        "scripts.github_async_delivery.refresh_delivery",
        lambda _client, item: item,
    )
    monkeypatch.setattr(
        "scripts.github_async_delivery.write_delivery",
        lambda *_args: status_path,
    )
    monkeypatch.setattr(
        "scripts.github_async_delivery.load_github_token",
        lambda: "test-token",
    )

    exit_code = require_merged(
        Namespace(
            status_dir=tmp_path,
            delivery_id=DELIVERY_ID,
            wait=False,
            timeout_seconds=30,
            interval_seconds=1,
        )
    )

    assert exit_code == expected_exit
