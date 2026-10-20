import asyncio

import httpx

from app.main import create_app


TOKEN = "test-launch-token"


def get(path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=create_app(TOKEN))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get(path, headers=headers)

    return asyncio.run(request())


def test_health_is_available_without_launch_token() -> None:
    response = get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["service"] == "ai-study-helper-backend"


def test_runtime_requires_launch_token() -> None:
    response = get("/api/runtime")

    assert response.status_code == 401


def test_runtime_rejects_wrong_launch_token() -> None:
    response = get(
        "/api/runtime",
        headers={"x-launch-token": "wrong-token"},
    )

    assert response.status_code == 401


def test_runtime_accepts_launch_token() -> None:
    response = get(
        "/api/runtime",
        headers={"x-launch-token": TOKEN},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert response.json()["pid"] > 0
