from __future__ import annotations

import argparse
import os
import secrets
import time

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app import __version__


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


def create_app(launch_token: str | None = None) -> FastAPI:
    token = launch_token or os.environ.get("ASH_LAUNCH_TOKEN", "")
    if not token:
        raise RuntimeError("ASH_LAUNCH_TOKEN is required")

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
        allow_methods=["GET"],
        allow_headers=["x-launch-token"],
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
