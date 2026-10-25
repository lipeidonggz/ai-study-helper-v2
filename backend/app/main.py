from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from app import __version__
from app.agent.factory import AgentExecutionService
from app.agent.models import (
    AgentRunEvent,
    AgentRunRequest,
    AgentRunResponse,
    AgentRunStartResponse,
)
from app.agent.streaming import (
    AgentRunEventGapError,
    AgentRunNotFoundError,
    AgentRunStreamService,
)
from app.collections.models import (
    CurrentCollectionResponse,
    PublishCollectionRequest,
    PublishCollectionResponse,
    RollbackCollectionRequest,
)
from app.collections.service import CollectionSnapshotService
from app.documents.models import DocumentDetailResponse
from app.documents.service import DocumentDetailService
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
from app.providers.chat import ChatProvider
from app.providers.embeddings import EmbeddingProvider
from app.settings.models import (
    ChatProviderTestResult,
    ModelSettingsResponse,
    ProviderTestResult,
    RuntimeSettingsPayload,
    RuntimeSettingsResponse,
    SetChatKeyRequest,
    SetEmbeddingKeyRequest,
)
from app.settings.runtime_service import RuntimeSettingsService
from app.settings.service import ModelSettingsService
from app.storage.credentials import (
    CredentialStore,
    WindowsCredentialStore,
)
from app.storage.database import StorageDatabase
from app.tasks.models import (
    BackgroundIngestRequest,
    BackgroundTaskRecord,
    PreviewIngestTaskRequest,
)
from app.tasks.service import BackgroundTaskService
from app.tracing.catalog import TraceCatalog, TraceInvalidError, TraceNotFoundError
from app.tracing.models import TraceDetailRecord, TraceListItem
from app.tracing.store import TraceStore
from app.workspace.models import ResetWorkspaceRequest, ResetWorkspaceResponse
from app.workspace.service import WorkspaceResetService


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
    credential_store: CredentialStore | None = None,
    embedding_test_provider_factory: Callable[[str], EmbeddingProvider] | None = None,
    chat_provider_factory: Callable[[str], ChatProvider] | None = None,
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
    resolved_credentials = credential_store or WindowsCredentialStore()
    model_settings_service = (
        ModelSettingsService(
            StorageDatabase(resolved_data_root),
            resolved_credentials,
            embedding_test_provider_factory=embedding_test_provider_factory,
            chat_provider_factory=chat_provider_factory,
        )
        if resolved_data_root is not None
        else None
    )
    task_service = (
        BackgroundTaskService(
            resolved_data_root,
            ingestion_service,
            collection_service,
        )
        if resolved_data_root is not None
        and ingestion_service is not None
        and collection_service is not None
        else None
    )
    workspace_reset_service = (
        WorkspaceResetService(resolved_data_root, task_service)
        if resolved_data_root is not None and task_service is not None
        else None
    )
    runtime_settings_service = (
        RuntimeSettingsService(StorageDatabase(resolved_data_root))
        if resolved_data_root is not None
        else None
    )
    agent_stream_service = AgentRunStreamService()
    trace_store = TraceStore(resolved_data_root) if resolved_data_root is not None else None
    trace_catalog = (
        TraceCatalog(resolved_data_root / "traces") if resolved_data_root is not None else None
    )
    agent_execution_service = (
        AgentExecutionService(
            collection_data_root=resolved_data_root,
            credentials=resolved_credentials,
            settings_provider=runtime_settings_service.get,
            trace_store=trace_store,
            embedding_provider_factory=embedding_provider_factory,
            chat_provider_factory=chat_provider_factory,
        )
        if resolved_data_root is not None and runtime_settings_service is not None
        else None
    )

    async def trace_cleanup_loop() -> None:
        """启动时及每日一次清理过期 trace，不影响主服务启动。"""

        while True:
            if trace_catalog is not None:
                with suppress(Exception):
                    await asyncio.to_thread(trace_catalog.cleanup)
            await asyncio.sleep(24 * 60 * 60)

    document_detail_service = (
        DocumentDetailService(resolved_data_root, collection_service, task_service)
        if resolved_data_root is not None
        and collection_service is not None
        and task_service is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if task_service is not None:
            await task_service.start()
        trace_cleanup_task = asyncio.create_task(trace_cleanup_loop())
        try:
            yield
        finally:
            trace_cleanup_task.cancel()
            await asyncio.gather(trace_cleanup_task, return_exceptions=True)
            await agent_stream_service.shutdown()
            if trace_store is not None:
                trace_store.close()
            if task_service is not None:
                await task_service.stop()

    started_at = time.monotonic()
    app = FastAPI(
        title="ai-study-helper backend",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["content-type", "x-launch-token", "last-event-id"],
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

    def build_agent_runtime():
        """通过产品与评测共享的工厂创建一次独立 Agent Runtime。"""

        if agent_execution_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return agent_execution_service.create_runtime()

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

    @app.get(
        "/api/collections/current/documents/{doc_id}",
        response_model=DocumentDetailResponse,
        dependencies=protected,
    )
    async def current_document_detail(doc_id: str) -> DocumentDetailResponse:
        """读取活动快照中一篇文档的正文、版本和相关操作。"""

        if document_detail_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return document_detail_service.get(doc_id)

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

    @app.get(
        "/api/settings/models",
        response_model=ModelSettingsResponse,
        dependencies=protected,
    )
    async def get_model_settings() -> ModelSettingsResponse:
        """返回不含 Key 明文的模型设置状态。"""

        if model_settings_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return model_settings_service.status()

    @app.get(
        "/api/settings/runtime",
        response_model=RuntimeSettingsResponse,
        dependencies=protected,
    )
    async def get_runtime_settings() -> RuntimeSettingsResponse:
        """返回检索与 Agent 的可调运行参数。"""

        if runtime_settings_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return runtime_settings_service.get()

    @app.put(
        "/api/settings/runtime",
        response_model=RuntimeSettingsResponse,
        dependencies=protected,
    )
    async def save_runtime_settings(
        request: RuntimeSettingsPayload,
    ) -> RuntimeSettingsResponse:
        """校验并原子保存下一次问答使用的运行参数。"""

        if runtime_settings_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return runtime_settings_service.save(request)

    @app.post(
        "/api/settings/embedding-key",
        response_model=ModelSettingsResponse,
        dependencies=protected,
    )
    async def set_embedding_key(
        request: SetEmbeddingKeyRequest,
    ) -> ModelSettingsResponse:
        """安全保存 embedding Key；响应不回传 Key。"""

        if model_settings_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return model_settings_service.set_embedding_key(request.api_key)

    @app.post(
        "/api/settings/embedding-key/delete",
        response_model=ModelSettingsResponse,
        dependencies=protected,
    )
    async def delete_embedding_key() -> ModelSettingsResponse:
        """删除 embedding Key 和旧连通性结果。"""

        if model_settings_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return model_settings_service.delete_embedding_key()

    @app.post(
        "/api/settings/embedding-key/test",
        response_model=ProviderTestResult,
        dependencies=protected,
    )
    async def test_embedding_key() -> ProviderTestResult:
        """使用已保存 Key 做一次最小 embedding 连通性验证。"""

        if model_settings_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return await model_settings_service.test_embedding()

    @app.post(
        "/api/settings/chat-key",
        response_model=ModelSettingsResponse,
        dependencies=protected,
    )
    async def set_chat_key(
        request: SetChatKeyRequest,
    ) -> ModelSettingsResponse:
        """安全保存独立对话 Key；响应不回传 Key。"""

        if model_settings_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return model_settings_service.set_chat_key(request.api_key)

    @app.post(
        "/api/settings/chat-key/delete",
        response_model=ModelSettingsResponse,
        dependencies=protected,
    )
    async def delete_chat_key() -> ModelSettingsResponse:
        """删除独立对话 Key。"""

        if model_settings_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return model_settings_service.delete_chat_key()

    @app.post(
        "/api/settings/chat-key/test",
        response_model=ChatProviderTestResult,
        dependencies=protected,
    )
    async def test_chat_key() -> ChatProviderTestResult:
        """使用已保存对话 Key 做一次最小真实补全检查。"""

        if model_settings_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return await model_settings_service.test_chat()

    @app.post(
        "/api/agent/runs",
        response_model=AgentRunStartResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=protected,
    )
    async def start_agent_run(request: AgentRunRequest) -> AgentRunStartResponse:
        """启动一次后台流式问答并返回运行与事件定位标识。"""

        runtime = build_agent_runtime()
        started = await agent_stream_service.start(runtime, request.question)
        run_id = str(started.payload["run_id"])
        return AgentRunStartResponse(run_id=run_id, trace_id=started.trace_id)

    @app.get(
        "/api/agent/runs/{run_id}/events",
        dependencies=protected,
    )
    async def stream_agent_run(run_id: str, request: Request) -> StreamingResponse:
        """按 Last-Event-ID 回放并持续发送本次问答事件。"""

        raw_cursor = request.headers.get("last-event-id") or request.query_params.get(
            "after",
            "0",
        )
        try:
            after_sequence = int(raw_cursor)
        except ValueError as error:
            raise IngestFailure(
                "agent_event_cursor_invalid",
                "Last-Event-ID must be a non-negative integer",
                400,
            ) from error
        try:
            agent_stream_service.validate_cursor(run_id, after_sequence)
        except AgentRunNotFoundError as error:
            raise IngestFailure(
                "agent_run_not_found",
                "agent run is no longer available for reconnect",
                404,
            ) from error
        except AgentRunEventGapError as error:
            raise IngestFailure(
                "agent_event_gap",
                "requested event position is no longer available",
                409,
            ) from error
        return StreamingResponse(
            (
                envelope.to_sse()
                async for envelope in agent_stream_service.subscribe(
                    run_id,
                    after_sequence=after_sequence,
                )
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(
        "/api/agent/runs/{run_id}/cancel",
        dependencies=protected,
    )
    async def cancel_agent_run(run_id: str) -> dict[str, object]:
        """请求取消一次仍在执行的流式问答。"""

        try:
            agent_stream_service.cancel(run_id)
            return agent_stream_service.run_state(run_id)
        except AgentRunNotFoundError as error:
            raise IngestFailure(
                "agent_run_not_found",
                "agent run is no longer available",
                404,
            ) from error

    @app.get(
        "/api/traces",
        response_model=list[TraceListItem],
        dependencies=protected,
    )
    async def list_traces(limit: int = Query(default=50, ge=1, le=200)) -> list[TraceListItem]:
        """返回最近本地 Agent trace 摘要。"""

        if trace_catalog is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return trace_catalog.list_recent(limit)

    @app.get(
        "/api/traces/{trace_id}",
        response_model=TraceDetailRecord,
        dependencies=protected,
    )
    async def get_trace(trace_id: str) -> TraceDetailRecord:
        """从本地事实文件读取一次完整 trace。"""

        if trace_catalog is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        try:
            return trace_catalog.load(trace_id)
        except TraceNotFoundError as error:
            raise IngestFailure(
                "trace_not_found",
                "trace does not exist or has been cleaned up",
                404,
            ) from error
        except TraceInvalidError as error:
            raise IngestFailure(
                "trace_invalid",
                str(error),
                500,
            ) from error

    @app.get(
        "/api/traces/{trace_id}/artifacts/{artifact_id}",
        dependencies=protected,
    )
    async def get_trace_artifact(trace_id: str, artifact_id: str) -> FileResponse:
        """读取 full trace 中通过 checksum 校验的 artifact。"""

        if trace_catalog is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        try:
            detail = trace_catalog.load(trace_id)
            artifact = next(
                (item for item in detail.manifest.artifacts if item.artifact_id == artifact_id),
                None,
            )
            path = trace_catalog.artifact_path(trace_id, artifact_id)
        except TraceNotFoundError as error:
            raise IngestFailure(
                "trace_artifact_not_found",
                "trace artifact does not exist or is omitted",
                404,
            ) from error
        except TraceInvalidError as error:
            raise IngestFailure(
                "trace_artifact_invalid",
                str(error),
                500,
            ) from error
        return FileResponse(
            path,
            media_type=artifact.mime_type if artifact is not None else "application/octet-stream",
        )

    @app.post(
        "/api/traces/index/rebuild",
        dependencies=protected,
    )
    async def rebuild_trace_index() -> dict[str, int]:
        """从 trace manifest 重建派生索引。"""

        if trace_catalog is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return {"indexed": trace_catalog.rebuild()}

    @app.post(
        "/api/agent/run",
        response_model=AgentRunResponse,
        dependencies=protected,
    )
    async def run_agent(request: AgentRunRequest) -> AgentRunResponse:
        """执行一次非流式 Agent 单轮问答。"""

        result = await build_agent_runtime().run(request.question)
        return AgentRunResponse(
            status=result.status,
            trace_id=result.trace_id,
            answer=result.answer,
            answer_completeness=result.answer_completeness,
            limitations=list(result.limitations),
            termination_reason=result.termination_reason,
            snapshot_id=result.snapshot_id,
            prompt_bundle_version=result.prompt_bundle_version,
            prompt_template_hash=result.prompt_template_hash,
            observation_token_counter_version=result.observation_token_counter_version,
            agent_turns=result.agent_turns,
            tool_calls=result.tool_calls,
            model_calls=result.model_calls,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            sources=result.sources,
            citation_annotations=result.citation_annotations,
            citation_annotation_model_calls=result.citation_annotation_model_calls,
            events=[
                AgentRunEvent(event_type=event.event_type, payload=event.payload)
                for event in result.events
            ],
        )

    @app.post(
        "/api/workspace/reset",
        response_model=ResetWorkspaceResponse,
        dependencies=protected,
    )
    async def reset_workspace(
        _request: ResetWorkspaceRequest,
    ) -> ResetWorkspaceResponse:
        """清空知识库、入库任务和派生文件，保留模型设置与本地凭证。"""

        if workspace_reset_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return await asyncio.to_thread(workspace_reset_service.reset)

    @app.post(
        "/api/tasks/ingest/preview",
        response_model=BackgroundTaskRecord,
        dependencies=protected,
    )
    async def create_preview_ingest_task(
        request: PreviewIngestTaskRequest,
    ) -> BackgroundTaskRecord:
        """复用单篇预览产物启动完整 S2-S6 入库并写入操作历史。"""

        if task_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return await task_service.create_from_preview(request)

    @app.post(
        "/api/tasks/ingest",
        response_model=BackgroundTaskRecord,
        dependencies=protected,
    )
    async def create_background_ingest_task(
        request: BackgroundIngestRequest,
    ) -> BackgroundTaskRecord:
        """启动 S0-S6 后台入库任务并立即返回任务 ID。"""

        if task_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return await task_service.create(request)

    @app.get(
        "/api/tasks/active",
        response_model=list[BackgroundTaskRecord],
        dependencies=protected,
    )
    async def list_active_background_tasks(
        limit: int = Query(default=100, ge=1, le=100),
    ) -> list[BackgroundTaskRecord]:
        """返回 queued、running、paused 和 interrupted 的持久任务队列。"""

        if task_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return task_service.list_active(limit=limit)

    @app.get(
        "/api/tasks",
        response_model=list[BackgroundTaskRecord],
        dependencies=protected,
    )
    async def list_background_tasks(
        limit: int = Query(default=20, ge=1, le=100),
    ) -> list[BackgroundTaskRecord]:
        """返回最近入库任务，包含逐来源成功或失败结果。"""

        if task_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return task_service.list_recent(limit=limit)

    @app.get(
        "/api/tasks/{task_id}",
        response_model=BackgroundTaskRecord,
        dependencies=protected,
    )
    async def get_background_task(task_id: str) -> BackgroundTaskRecord:
        """查询后台任务状态和进度。"""

        if task_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return task_service.get(task_id)

    @app.post(
        "/api/tasks/{task_id}/pause",
        response_model=BackgroundTaskRecord,
        dependencies=protected,
    )
    async def pause_background_task(task_id: str) -> BackgroundTaskRecord:
        """请求后台任务在下一个阶段边界暂停。"""

        if task_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return task_service.pause(task_id)

    @app.post(
        "/api/tasks/{task_id}/resume",
        response_model=BackgroundTaskRecord,
        dependencies=protected,
    )
    async def resume_background_task(task_id: str) -> BackgroundTaskRecord:
        """从暂停或中断状态恢复后台任务。"""

        if task_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return await task_service.resume(task_id)

    @app.post(
        "/api/tasks/{task_id}/cancel",
        response_model=BackgroundTaskRecord,
        dependencies=protected,
    )
    async def cancel_background_task(task_id: str) -> BackgroundTaskRecord:
        """请求后台任务在下一个阶段边界取消。"""

        if task_service is None:
            raise IngestFailure(
                "data_root_unavailable",
                "application data root is not configured",
                503,
            )
        return task_service.cancel(task_id)

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
