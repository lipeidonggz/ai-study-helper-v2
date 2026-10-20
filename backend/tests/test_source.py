from __future__ import annotations

import asyncio

import httpx
import pytest

from app.ingestion.errors import IngestFailure
from app.ingestion.source import HttpUrlFetcher, _validate_public_url
from tests.helpers import fixture_bytes

PUBLIC_RESOLVER = lambda _host, _port: ["93.184.216.34"]  # noqa: E731
PRIVATE_RESOLVER = lambda _host, _port: ["127.0.0.1"]  # noqa: E731


def test_private_url_is_blocked_before_network_request() -> None:
    def fail_transport(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("transport must not be called for a private address")

    fetcher = HttpUrlFetcher(
        transport=httpx.MockTransport(fail_transport),
        resolver=PRIVATE_RESOLVER,
    )
    with pytest.raises(IngestFailure) as failure:
        asyncio.run(fetcher.fetch("https://private.example/article"))
    assert failure.value.code == "ssrf_blocked"


def test_url_fetcher_revalidates_redirect_target() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.example":
            return httpx.Response(
                302,
                headers={"location": "http://127.0.0.1/private"},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    fetcher = HttpUrlFetcher(
        transport=httpx.MockTransport(handler),
        resolver=PUBLIC_RESOLVER,
    )
    with pytest.raises(IngestFailure) as failure:
        asyncio.run(fetcher.fetch("https://public.example/article"))
    assert failure.value.code == "ssrf_blocked"


def test_url_fetcher_reads_html_without_script_execution() -> None:
    body = fixture_bytes("no-headings.html")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=body,
        )

    fetcher = HttpUrlFetcher(
        transport=httpx.MockTransport(handler),
        resolver=PUBLIC_RESOLVER,
    )
    response = asyncio.run(fetcher.fetch("https://public.example/article"))
    assert response.status_code == 200
    assert response.body == body


def test_validate_public_url_rejects_credentials() -> None:
    with pytest.raises(IngestFailure) as failure:
        asyncio.run(_validate_public_url("https://user:pass@example.com/", PUBLIC_RESOLVER))
    assert failure.value.code == "invalid_url"
