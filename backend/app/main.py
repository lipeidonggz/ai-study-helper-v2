from __future__ import annotations

import argparse
import os
import secrets
import time
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import __version__
from app.ingestion.errors import IngestFailure
from app.ingestion.models import IngestPreviewRequest, IngestPreviewResponse
from app.ingestion.service import IngestionPreviewService
from app.ingestion.source import UrlFetcher


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


class RuntimeResponse(BaseModel):
    status: str
    version: str
    pid: int
    uptime_seconds: float


ALLOWED_ORIGINS = [
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://tauri.localhost",
    "https://tauri.localhost",
]


def create_app(
    launch_token: str | None = None,
    data_root: Path | None = None,
    url_fetcher: UrlFetcher | None = None,
) -> FastAPI:
    token = launch_token or os.environ.get("ASH_LAUNCH_TOKEN", "")
    if not token:
        raise RuntimeError("ASH_LAUNCH_TOKEN is required")
    resolved_data_root = data_root or (
        Path(os.environ["ASH_DATA_ROOT"]) if os.environ.get("ASH_DATA_ROOT") else None
    )
    ingestion_service = (
        IngestionPreviewService(resolved_data_root, url_fetcher=url_fetcher)
        if resolved_data_root is not None
        else None
    )

    started_at = time.monotonic()
    app = FastAPI(
        title="ai-study-helper backend",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["content-type", "x-launch-token"],
    )

    @app.exception_handler(IngestFailure)
    async def ingest_failure_handler(
        _request: Request,
        failure: IngestFailure,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=failure.http_status,
            content={
                "error": {
                    "code": failure.code,
                    "message": failure.message,
                    "retryable": failure.retryable,
                }
            },
        )

    def require_launch_token(request: Request) -> None:
        supplied = request.headers.get("x-launch-token", "")
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid launch token",
            )

    protected = [Depends(require_launch_token)]

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service="ai-study-helper-backend",
            version=__version__,
        )

    @app.get("/api/runtime", response_model=RuntimeResponse, dependencies=protected)
    async def runtime() -> RuntimeResponse:
        return RuntimeResponse(
            status="running",
            version=__version__,
            pid=os.getpid(),
            uptime_seconds=round(time.monotonic() - started_at, 3),
        )

    @app.post(
        "/api/ingest/preview",
        response_model=IngestPreviewResponse,
        dependencies=protected,
    )
    async def ingest_preview(request: IngestPreviewRequest) -> IngestPreviewResponse:
        if ingestion_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return await ingestion_service.preview(request)

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the ai-study-helper sidecar")
    parser.add_argument("--port", type=int, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app = create_app()
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
