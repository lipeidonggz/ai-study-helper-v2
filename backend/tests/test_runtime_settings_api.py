from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from app.main import create_app

TOKEN = "runtime-settings-test-token"


def _payload(**overrides: object) -> dict[str, object]:
    value = {
        "top_k": 5,
        "max_output_tokens": 8192,
        "max_context_tokens": 100_000,
        "max_request_tokens": 400_000,
        "max_tool_calls": 8,
        "trace_mode": "full",
    }
    value.update(overrides)
    return value


def test_runtime_settings_defaults_save_and_survive_restart(tmp_path: Path) -> None:
    app = create_app(TOKEN, data_root=tmp_path)

    async def run() -> tuple[dict, dict, dict, dict]:
        transport = httpx.ASGITransport(app=app)
        headers = {"x-launch-token": TOKEN}
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            defaults = await client.get("/api/settings/runtime", headers=headers)
            defaults.raise_for_status()
            saved = await client.put(
                "/api/settings/runtime",
                headers=headers,
                json=_payload(top_k=7, max_output_tokens=12_288),
            )
            saved.raise_for_status()
            loaded = await client.get("/api/settings/runtime", headers=headers)
            loaded.raise_for_status()
            reset = await client.post(
                "/api/workspace/reset",
                headers=headers,
                json={"confirmation": "清空知识库"},
            )
            reset.raise_for_status()
            after_reset = await client.get("/api/settings/runtime", headers=headers)
            after_reset.raise_for_status()
            return defaults.json(), saved.json(), loaded.json(), after_reset.json()

    defaults, saved, loaded, after_reset = asyncio.run(run())

    assert defaults["top_k"] == 5
    assert defaults["max_output_tokens"] == 8192
    assert defaults["trace_mode"] in {"off", "metadata", "full"}
    assert defaults["updated_at"] is None
    assert saved["top_k"] == 7
    assert saved["max_output_tokens"] == 12_288
    assert saved["trace_mode"] == "full"
    assert saved["updated_at"] is not None
    assert loaded == saved
    assert after_reset == saved

    restarted_app = create_app(TOKEN, data_root=tmp_path)

    async def read_restarted() -> dict:
        transport = httpx.ASGITransport(app=restarted_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                "/api/settings/runtime",
                headers={"x-launch-token": TOKEN},
            )
            response.raise_for_status()
            return response.json()

    assert asyncio.run(read_restarted()) == saved


def test_runtime_settings_reject_invalid_cross_field_limits(tmp_path: Path) -> None:
    app = create_app(TOKEN, data_root=tmp_path)

    async def run() -> tuple[int, dict, dict]:
        transport = httpx.ASGITransport(app=app)
        headers = {"x-launch-token": TOKEN}
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            invalid = await client.put(
                "/api/settings/runtime",
                headers=headers,
                json=_payload(
                    max_context_tokens=900_000,
                    max_output_tokens=200_000,
                    max_request_tokens=1_100_000,
                ),
            )
            current = await client.get("/api/settings/runtime", headers=headers)
            current.raise_for_status()
            return invalid.status_code, invalid.json(), current.json()

    status_code, invalid, current = asyncio.run(run())

    assert status_code == 422
    assert "max_context_tokens + max_output_tokens" in str(invalid)
    assert current["top_k"] == 5
    assert current["max_context_tokens"] == 100_000


def test_runtime_settings_put_is_allowed_by_browser_cors_preflight(tmp_path: Path) -> None:
    app = create_app(TOKEN, data_root=tmp_path)

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.options(
                "/api/settings/runtime",
                headers={
                    "origin": "http://127.0.0.1:1420",
                    "access-control-request-method": "PUT",
                    "access-control-request-headers": "content-type,x-launch-token",
                },
            )

    response = asyncio.run(run())

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:1420"
    assert "PUT" in response.headers["access-control-allow-methods"]
    assert "x-launch-token" in response.headers["access-control-allow-headers"].lower()


def test_agent_event_stream_headers_are_allowed_by_browser_cors_preflight(
    tmp_path: Path,
) -> None:
    app = create_app(TOKEN, data_root=tmp_path)

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.options(
                "/api/agent/runs/example/events",
                headers={
                    "origin": "http://127.0.0.1:1420",
                    "access-control-request-method": "GET",
                    "access-control-request-headers": "last-event-id,x-launch-token",
                },
            )

    response = asyncio.run(run())

    assert response.status_code == 200
    assert "last-event-id" in response.headers["access-control-allow-headers"].lower()
