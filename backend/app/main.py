from __future__ import annotations

import argparse
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import __version__
from app.collections.models import (
    CurrentCollectionResponse,
    PublishCollectionRequest,
    PublishCollectionResponse,
    RollbackCollectionRequest,
)
from app.collections.service import CollectionSnapshotService
from app.indexing.models import IndexBundle
from app.ingestion.errors import IngestFailure
from app.ingestion.models import (
    BuildIndexRequest,
    IngestDocumentResponse,
    IngestPreviewRequest,
    IngestPreviewResponse,
    PersistIngestRequest,
)
from app.ingestion.service import IngestionService
from app.ingestion.source import UrlFetcher
from app.providers.embeddings import EmbeddingProvider


class HealthResponse(BaseModel):
    """健康检查的最小响应契约。"""

    status: str
    service: str
    version: str


class RuntimeResponse(BaseModel):
    """已认证运行状态响应。"""

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
    embedding_provider_factory: Callable[[], EmbeddingProvider] | None = None,
) -> FastAPI:
    """创建只监听本机、使用启动令牌保护写接口的 FastAPI 应用。"""

    token = launch_token or os.environ.get("ASH_LAUNCH_TOKEN", "")
    if not token:
        raise RuntimeError("ASH_LAUNCH_TOKEN is required")
    resolved_data_root = data_root or (
        Path(os.environ["ASH_DATA_ROOT"]) if os.environ.get("ASH_DATA_ROOT") else None
    )
    ingestion_service = (
        IngestionService(
            resolved_data_root,
            url_fetcher=url_fetcher,
            embedding_provider_factory=embedding_provider_factory,
        )
        if resolved_data_root is not None
        else None
    )
    collection_service = (
        CollectionSnapshotService(resolved_data_root) if resolved_data_root is not None else None
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
        """把入库领域错误转换为稳定的 API 错误结构。"""

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
        """拒绝缺少或使用错误启动令牌的本地 API 请求。"""

        supplied = request.headers.get("x-launch-token", "")
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid launch token",
            )

    protected = [Depends(require_launch_token)]

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        """返回无需令牌的服务存活信息。"""

        return HealthResponse(
            status="ok",
            service="ai-study-helper-backend",
            version=__version__,
        )

    @app.get("/api/runtime", response_model=RuntimeResponse, dependencies=protected)
    async def runtime() -> RuntimeResponse:
        """返回需要启动令牌的进程与运行时长信息。"""

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
        """执行 S0-S4 并返回可保存的预览结果。"""

        if ingestion_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return await ingestion_service.preview(request)

    @app.post(
        "/api/ingest/persist",
        response_model=IngestDocumentResponse,
        dependencies=protected,
    )
    async def persist_ingested_document(
        request: PersistIngestRequest,
    ) -> IngestDocumentResponse:
        """把指定预览 job 保存为正式文档身份与版本。"""

        if ingestion_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return ingestion_service.persist(request)

    @app.post(
        "/api/ingest/index",
        response_model=IndexBundle,
        dependencies=protected,
    )
    async def build_candidate_index(request: BuildIndexRequest) -> IndexBundle:
        """为已保存 job 构建 S5 候选双索引，不发布 collection。"""

        if ingestion_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return await ingestion_service.build_index(request)

    @app.post(
        "/api/collections/publish",
        response_model=PublishCollectionResponse,
        dependencies=protected,
    )
    async def publish_collection(
        request: PublishCollectionRequest,
    ) -> PublishCollectionResponse:
        """把已有 S5 候选索引构建为快照并原子发布到活动指针。"""

        if collection_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return collection_service.publish(request.job_ids)

    @app.get(
        "/api/collections/current",
        response_model=CurrentCollectionResponse | None,
        dependencies=protected,
    )
    async def current_collection() -> CurrentCollectionResponse | None:
        """返回当前唯一的活动 collection；尚未发布时返回 None。"""

        if collection_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return collection_service.current()

    @app.post(
        "/api/collections/rollback",
        response_model=PublishCollectionResponse,
        dependencies=protected,
    )
    async def rollback_collection(
        request: RollbackCollectionRequest,
    ) -> PublishCollectionResponse:
        """切回当前 pointer 记录的上一个完整 snapshot。"""

        if collection_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return collection_service.rollback(request)

    return app


def parse_args() -> argparse.Namespace:
    """解析 sidecar 启动参数。"""

    parser = argparse.ArgumentParser(description="Run the ai-study-helper sidecar")
    parser.add_argument("--port", type=int, required=True)
    return parser.parse_args()


def main() -> None:
    """启动绑定回环地址的桌面 sidecar。"""

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
