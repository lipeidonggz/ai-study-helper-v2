from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx

from app.main import create_app
from app.providers.embeddings import FakeEmbeddingProvider

TOKEN = "model-settings-test-token"


class _MemoryCredentials:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, username: str) -> str | None:
        return self.values.get(username)

    def set(self, username: str, value: str) -> None:
        self.values[username] = value

    def delete(self, username: str) -> None:
        self.values.pop(username, None)


def _app(tmp_path: Path, credentials: _MemoryCredentials):
    return create_app(
        TOKEN,
        data_root=tmp_path,
        credential_store=credentials,
        embedding_provider_factory=FakeEmbeddingProvider,
        embedding_test_provider_factory=lambda _key: FakeEmbeddingProvider(),
    )


def test_settings_save_test_and_delete_embedding_key(tmp_path: Path) -> None:
    credentials = _MemoryCredentials()
    app = _app(tmp_path, credentials)

    async def run() -> tuple[dict, dict, dict, dict]:
        transport = httpx.ASGITransport(app=app)
        headers = {"x-launch-token": TOKEN}
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            initial = await client.get("/api/settings/models", headers=headers)
            initial.raise_for_status()
            saved = await client.post(
                "/api/settings/embedding-key",
                headers=headers,
                json={"api_key": "sk-test-not-persisted-in-sqlite"},
            )
            saved.raise_for_status()
            tested = await client.post(
                "/api/settings/embedding-key/test",
                headers=headers,
            )
            tested.raise_for_status()
            deleted = await client.post(
                "/api/settings/embedding-key/delete",
                headers=headers,
            )
            deleted.raise_for_status()
            return (
                initial.json(),
                saved.json(),
                tested.json(),
                deleted.json(),
            )

    initial, saved, tested, deleted = asyncio.run(run())

    assert initial["embedding_configured"] is False
    assert initial["embedding_model_id"] == "qwen3.7-text-embedding"
    assert saved["embedding_configured"] is True
    assert "sk-test-not-persisted-in-sqlite" not in json.dumps(saved)
    assert tested["status"] == "success"
    assert tested["dimension"] == 1024
    assert deleted["embedding_configured"] is False
    assert deleted["embedding_last_test"] is None
    assert credentials.values == {}
    with sqlite3.connect(tmp_path / "db" / "application.db") as connection:
        row = connection.execute(
            "SELECT last_test_status FROM provider_settings WHERE provider='embedding'"
        ).fetchone()
    assert row == (None,)


def test_testing_without_key_returns_stable_error(tmp_path: Path) -> None:
    credentials = _MemoryCredentials()
    app = _app(tmp_path, credentials)

    async def run() -> tuple[int, dict]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/settings/embedding-key/test",
                headers={"x-launch-token": TOKEN},
            )
            return response.status_code, response.json()

    status_code, body = asyncio.run(run())
    assert status_code == 409
    assert body["error"]["code"] == "embedding_api_key_missing"
