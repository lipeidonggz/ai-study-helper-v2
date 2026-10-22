from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.main import create_app
from app.storage.document_registry import DocumentRegistry

TOKEN = "persistence-test-token"


def _assert_utc_timestamp(value: str) -> None:
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def _html(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body><article><h1>{title}</h1>"
        f"<h2>正文</h2><p>{body}</p>"
        "<p>本页面用于验证正式文档身份、不可变版本、提取 artifact 和章节块重建流程。</p>"
        "</article></body></html>"
    ).encode()


def _run_ingestion_flow(data_root: Path, html_path: Path) -> tuple[dict, dict, dict]:
    async def run() -> tuple[dict, dict, dict]:
        app = create_app(TOKEN, data_root=data_root)
        transport = httpx.ASGITransport(app=app)
        headers = {"x-launch-token": TOKEN}
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            preview_response = await client.post(
                "/api/ingest/preview",
                headers=headers,
                json={"source_kind": "file", "source_value": str(html_path)},
            )
            preview_response.raise_for_status()
            preview = preview_response.json()
            persist_response = await client.post(
                "/api/ingest/persist",
                headers=headers,
                json={"job_id": preview["job_id"]},
            )
            persist_response.raise_for_status()
            persisted = persist_response.json()
            duplicate_response = await client.post(
                "/api/ingest/persist",
                headers=headers,
                json={"job_id": preview["job_id"]},
            )
            duplicate_response.raise_for_status()
            return preview, persisted, duplicate_response.json()

    return asyncio.run(run())


def _run_preview_then_persist(
    data_root: Path,
    html_path: Path,
    *,
    mutate_preview: Callable[[Path], None] | None = None,
) -> tuple[dict, int, dict]:
    async def run() -> tuple[dict, int, dict]:
        app = create_app(TOKEN, data_root=data_root)
        transport = httpx.ASGITransport(app=app)
        headers = {"x-launch-token": TOKEN}
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            preview_response = await client.post(
                "/api/ingest/preview",
                headers=headers,
                json={"source_kind": "file", "source_value": str(html_path)},
            )
            preview_response.raise_for_status()
            preview = preview_response.json()
            job_directory = data_root / "jobs" / preview["job_id"]
            if mutate_preview is not None:
                mutate_preview(job_directory)
            persist_response = await client.post(
                "/api/ingest/persist",
                headers=headers,
                json={"job_id": preview["job_id"]},
            )
            return preview, persist_response.status_code, persist_response.json()

    return asyncio.run(run())


def test_persist_registers_stable_document_and_content_versions(tmp_path: Path) -> None:
    html_path = tmp_path / "document.html"
    html_path.write_bytes(
        _html(
            "持久化测试",
            "这是第一版正文。本文包含足够的中文内容，用于验证正文提取、文档身份和版本注册流程。",
        )
    )

    first_preview, first_persist, duplicate = _run_ingestion_flow(tmp_path, html_path)

    assert first_preview["section_tree"]["doc_id"].startswith("preview_")
    assert first_persist["is_new_document"] is True
    assert first_persist["is_new_version"] is True
    assert first_persist["document"]["doc_id"].startswith("doc_")
    assert first_persist["version"]["doc_version"] == 1
    _assert_utc_timestamp(first_persist["document"]["created_at"])
    _assert_utc_timestamp(first_persist["document"]["updated_at"])
    _assert_utc_timestamp(first_persist["version"]["created_at"])
    assert first_persist["section_tree"]["doc_id"] == first_persist["document"]["doc_id"]
    assert first_persist["section_tree"]["doc_version"] == 1
    assert first_persist["section_tree"]["tree_hash"] == first_preview["section_tree"]["tree_hash"]
    assert (
        first_persist["block_set"]["block_set_hash"] == first_preview["block_set"]["block_set_hash"]
    )
    assert first_persist["block_set"]["blocks"][0]["block_id"].startswith(
        f"{first_persist['document']['doc_id']}#v0001#c1#b"
    )
    for field in ("extracted_document_ref", "section_tree_ref", "block_set_ref"):
        assert (tmp_path / first_persist["version"][field]).is_file()

    assert duplicate["is_new_document"] is False
    assert duplicate["is_new_version"] is False
    assert duplicate["document"]["doc_id"] == first_persist["document"]["doc_id"]
    assert duplicate["version"]["doc_version"] == 1
    _assert_utc_timestamp(duplicate["document"]["created_at"])
    _assert_utc_timestamp(duplicate["document"]["updated_at"])
    _assert_utc_timestamp(duplicate["version"]["created_at"])

    html_path.write_bytes(
        _html(
            "持久化测试",
            "这是第二版正文。本文正文已经发生明确变化，但仍然包含足够内容用于版本递增验证。",
        )
    )
    _, second_persist, _ = _run_ingestion_flow(tmp_path, html_path)

    assert second_persist["is_new_document"] is False
    assert second_persist["is_new_version"] is True
    assert second_persist["document"]["doc_id"] == first_persist["document"]["doc_id"]
    assert second_persist["version"]["doc_version"] == 2

    with sqlite3.connect(tmp_path / "db" / "application.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM document_versions").fetchone() == (2,)
        assert connection.execute("SELECT current_doc_version FROM documents").fetchone() == (2,)
        assert connection.execute(
            "SELECT status FROM ingestion_jobs ORDER BY created_at"
        ).fetchall() == [("succeeded",), ("succeeded",)]


def test_different_source_key_creates_new_document(tmp_path: Path) -> None:
    first_path = tmp_path / "first.html"
    second_path = tmp_path / "second.html"
    first_path.write_bytes(
        _html(
            "第一篇",
            "第一篇文档包含足够正文，用于验证不同来源键会创建独立文档身份。"
            "这篇文档还包含额外的说明内容，以确保正文长度超过最低提取阈值。",
        )
    )
    second_path.write_bytes(
        _html(
            "第二篇",
            "第二篇文档包含足够正文，用于验证不同来源键会创建独立文档身份。"
            "这篇文档还包含额外的说明内容，以确保正文长度超过最低提取阈值。",
        )
    )

    _, first, _ = _run_ingestion_flow(tmp_path, first_path)
    _, second, _ = _run_ingestion_flow(tmp_path, second_path)

    assert first["document"]["doc_id"] != second["document"]["doc_id"]
    with sqlite3.connect(tmp_path / "db" / "application.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone() == (2,)


def test_persist_rejects_missing_preview_artifact(tmp_path: Path) -> None:
    html_path = tmp_path / "missing-artifact.html"
    html_path.write_bytes(
        _html(
            "缺失产物",
            "这篇文档用于验证预览 artifact 丢失时的保存失败语义和错误码。",
        )
    )

    async def run() -> tuple[int, dict]:
        app = create_app(TOKEN, data_root=tmp_path)
        transport = httpx.ASGITransport(app=app)
        headers = {"x-launch-token": TOKEN}
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            preview_response = await client.post(
                "/api/ingest/preview",
                headers=headers,
                json={"source_kind": "file", "source_value": str(html_path)},
            )
            preview_response.raise_for_status()
            job_id = preview_response.json()["job_id"]
            (tmp_path / "jobs" / job_id / "extracted.json").unlink()
            persist_response = await client.post(
                "/api/ingest/persist",
                headers=headers,
                json={"job_id": job_id},
            )
            return persist_response.status_code, persist_response.json()

    status_code, body = asyncio.run(run())

    assert status_code == 404
    assert body["error"]["code"] == "job_artifact_missing"


def test_duplicate_save_repairs_missing_derived_refs_without_new_version(
    tmp_path: Path,
) -> None:
    html_path = tmp_path / "repair.html"
    html_path.write_bytes(
        _html(
            "修复派生引用",
            "这篇文档用于验证相同内容重复保存时可以补齐缺失的章节和块 artifact。",
        )
    )
    _, first, _ = _run_ingestion_flow(tmp_path, html_path)

    with sqlite3.connect(tmp_path / "db" / "application.db") as connection:
        connection.execute("UPDATE document_versions SET section_tree_ref=NULL, block_set_ref=NULL")
        connection.commit()

    _, repaired, _ = _run_ingestion_flow(tmp_path, html_path)

    assert repaired["is_new_version"] is False
    assert repaired["version"]["section_tree_ref"] is not None
    assert repaired["version"]["block_set_ref"] is not None
    assert repaired["version"]["doc_version"] == first["version"]["doc_version"]
    assert "derived_artifacts_repaired" in repaired["warnings"]


@pytest.mark.parametrize("damage", ["missing", "invalid_json"])
def test_duplicate_save_rejects_invalid_existing_artifact(
    tmp_path: Path,
    damage: str,
) -> None:
    html_path = tmp_path / f"invalid-ref-{damage}.html"
    html_path.write_bytes(
        _html(
            "损坏引用",
            "这篇文档用于验证已有派生 artifact 缺失或损坏时不会静默覆盖历史引用。",
        )
    )
    _, first, _ = _run_ingestion_flow(tmp_path, html_path)
    artifact = tmp_path / first["version"]["section_tree_ref"]
    if damage == "missing":
        artifact.unlink()
    else:
        artifact.write_text("{broken", encoding="utf-8")

    _, status_code, body = _run_preview_then_persist(tmp_path, html_path)

    assert status_code == 409
    assert body["error"]["code"] == "document_version_artifact_invalid"


@pytest.mark.parametrize(
    ("artifact_name", "hash_field"),
    [
        ("section-tree.json", "tree_hash"),
        ("blocks.json", "block_set_hash"),
    ],
)
def test_preview_consistency_guard_rejects_tampered_artifact(
    tmp_path: Path,
    artifact_name: str,
    hash_field: str,
) -> None:
    html_path = tmp_path / f"tampered-{artifact_name}.html"
    html_path.write_bytes(
        _html(
            "预览一致性",
            "这篇文档用于验证预览 artifact 被篡改后保存会被一致性屏障拒绝。",
        )
    )

    def mutate(job_directory: Path) -> None:
        path = job_directory / artifact_name
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload[hash_field] = "0" * 64
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    _, status_code, body = _run_preview_then_persist(
        tmp_path,
        html_path,
        mutate_preview=mutate,
    )

    assert status_code == 409
    assert body["error"]["code"] == "preview_consistency_failed"
    with sqlite3.connect(tmp_path / "db" / "application.db") as connection:
        job_status = connection.execute("SELECT status, stage FROM ingestion_jobs").fetchone()
        refs = connection.execute(
            "SELECT section_tree_ref, block_set_ref FROM document_versions"
        ).fetchone()
    assert job_status == ("failed", "S4")
    assert refs == (None, None)


def test_s2_database_write_failure_preserves_stage_and_fails_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html_path = tmp_path / "write-failure.html"
    html_path.write_bytes(
        _html(
            "数据库写失败",
            "这篇文档用于验证 SQLite 写入失败时不会返回保存成功，并保留 S2 阶段。",
        )
    )

    def fail_register(self, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(DocumentRegistry, "register", fail_register)

    _, status_code, body = _run_preview_then_persist(tmp_path, html_path)

    assert status_code == 500
    assert body["error"]["code"] == "document_persistence_failed"
    with sqlite3.connect(tmp_path / "db" / "application.db") as connection:
        assert connection.execute(
            "SELECT status, stage, error_code FROM ingestion_jobs"
        ).fetchone() == ("failed", "S2", "document_persistence_failed")
