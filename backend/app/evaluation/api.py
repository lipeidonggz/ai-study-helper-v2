from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agent.factory import AgentExecutionService
from app.collections.models import CurrentCollectionResponse
from app.collections.service import CollectionSnapshotService
from app.evaluation.database import EVALUATION_SCHEMA_VERSION, EvaluationDatabase
from app.evaluation.models import (
    CaseCatalogItem,
    CaseDraftView,
    CaseRevisionView,
    CreateCaseDraftRequest,
    EvaluationHealthResponse,
    ExecutionPlanView,
    PreflightRequest,
    PublishCaseDraftRequest,
    PublishSuiteDraftRequest,
    QuickPlanRequest,
    ReviewRevisionRequest,
    RunView,
    SaveCaseDraftRequest,
    SaveSuiteDraftRequest,
    StartDraftRequest,
    StartRunRequest,
    SuiteDraftView,
    SuiteRevisionView,
)
from app.evaluation.planner import EvaluationPlanner
from app.evaluation.repository import EvaluationRepository
from app.evaluation.runner import EvaluationRunner
from app.ingestion.errors import IngestFailure
from app.providers.chat import ChatProvider
from app.providers.embeddings import EmbeddingProvider
from app.settings.runtime_service import RuntimeSettingsService
from app.storage.credentials import CredentialStore, WindowsCredentialStore
from app.storage.database import StorageDatabase
from app.tracing.catalog import TraceCatalog, TraceInvalidError, TraceNotFoundError
from app.tracing.models import TraceDetailRecord, TraceListItem
from app.tracing.store import TraceStore

ALLOWED_ORIGINS = [
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://tauri.localhost",
    "https://tauri.localhost",
]


def create_eval_app(
    launch_token: str | None = None,
    *,
    product_data_root: Path | None = None,
    eval_data_root: Path | None = None,
    credential_store: CredentialStore | None = None,
    embedding_provider_factory: Callable[[], EmbeddingProvider] | None = None,
    chat_provider_factory: Callable[[str], ChatProvider] | None = None,
) -> FastAPI:
    """创建只监听回环地址、与产品 sidecar 进程隔离的评测应用。"""

    token = launch_token or os.environ.get("ASH_LAUNCH_TOKEN", "")
    if not token:
        raise RuntimeError("ASH_LAUNCH_TOKEN is required")
    resolved_product_root = product_data_root or _path_from_environment("ASH_DATA_ROOT")
    resolved_eval_root = eval_data_root or _path_from_environment("ASH_EVAL_DATA_ROOT")
    if resolved_product_root is None or resolved_eval_root is None:
        raise RuntimeError("ASH_DATA_ROOT and ASH_EVAL_DATA_ROOT are required")
    product_root = resolved_product_root.resolve()
    eval_root = resolved_eval_root.resolve()
    if product_root == eval_root:
        raise RuntimeError("evaluation data root must differ from product data root")

    database = EvaluationDatabase(eval_root)
    repository = EvaluationRepository(database)
    collection_service = CollectionSnapshotService(product_root)
    runtime_settings_service = RuntimeSettingsService(StorageDatabase(product_root))
    trace_store = TraceStore(eval_root)
    trace_catalog = TraceCatalog(eval_root / "traces")
    credentials = credential_store or WindowsCredentialStore()
    execution_service = AgentExecutionService(
        collection_data_root=product_root,
        credentials=credentials,
        settings_provider=runtime_settings_service.get,
        trace_store=trace_store,
        embedding_provider_factory=embedding_provider_factory,
        chat_provider_factory=chat_provider_factory,
    )
    planner = EvaluationPlanner(repository, collection_service)
    runner = EvaluationRunner(repository, execution_service)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            await runner.shutdown()
            trace_store.close()

    app = FastAPI(
        title="ai-study-helper evaluation console",
        version="0.1.0",
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
        allow_headers=["content-type", "x-launch-token"],
    )

    @app.exception_handler(IngestFailure)
    async def ingest_failure_handler(
        _request: Request,
        failure: IngestFailure,
    ) -> JSONResponse:
        return _error_response(failure.http_status, failure.code, failure.message)

    @app.exception_handler(KeyError)
    async def missing_record_handler(_request: Request, error: KeyError) -> JSONResponse:
        return _error_response(404, "evaluation_record_not_found", str(error.args[0]))

    @app.exception_handler(ValueError)
    async def invalid_state_handler(_request: Request, error: ValueError) -> JSONResponse:
        return _error_response(409, "evaluation_state_conflict", str(error))

    def require_launch_token(request: Request) -> None:
        supplied = request.headers.get("x-launch-token", "")
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid launch token",
            )

    protected = [Depends(require_launch_token)]

    @app.get("/api/eval/health", response_model=EvaluationHealthResponse)
    async def health() -> EvaluationHealthResponse:
        """返回评测 sidecar 和独立库的可用状态。"""

        return EvaluationHealthResponse(
            status="ok",
            service="ai-study-helper-evaluation",
            schema_version=EVALUATION_SCHEMA_VERSION,
        )

    @app.get(
        "/api/eval/suites",
        response_model=list[SuiteRevisionView],
        dependencies=protected,
    )
    async def list_suites() -> list[SuiteRevisionView]:
        """返回 suite revisions 及其固定用例现场。"""

        return repository.list_suite_revisions()

    @app.get(
        "/api/eval/cases",
        response_model=list[CaseCatalogItem],
        dependencies=protected,
    )
    async def list_cases() -> list[CaseCatalogItem]:
        """返回稳定用例、历史修订版和草稿状态。"""

        return repository.list_case_catalog()

    @app.post(
        "/api/eval/cases",
        response_model=CaseDraftView,
        status_code=status.HTTP_201_CREATED,
        dependencies=protected,
    )
    async def create_case(
        request: CreateCaseDraftRequest,
    ) -> CaseDraftView:
        """创建一个新的稳定用例身份和初始草稿。"""

        return repository.create_new_case_draft(request)

    @app.post(
        "/api/eval/cases/{case_id}/draft",
        response_model=CaseDraftView,
        status_code=status.HTTP_201_CREATED,
        dependencies=protected,
    )
    async def create_case_draft(
        case_id: str,
        request: StartDraftRequest,
    ) -> CaseDraftView:
        """从当前最新修订版派生可反复编辑的用例草稿。"""

        return repository.create_case_draft(
            case_id,
            updated_by=request.updated_by,
            base_revision_id=request.base_revision_id,
        )

    @app.get(
        "/api/eval/cases/{case_id}/draft",
        response_model=CaseDraftView,
        dependencies=protected,
    )
    async def get_case_draft(case_id: str) -> CaseDraftView:
        """读取当前用例草稿。"""

        return repository.get_case_draft(case_id)

    @app.put(
        "/api/eval/cases/{case_id}/draft",
        response_model=CaseDraftView,
        dependencies=protected,
    )
    async def save_case_draft(
        case_id: str,
        request: SaveCaseDraftRequest,
    ) -> CaseDraftView:
        """覆盖保存草稿字段和完整判定项集合。"""

        return repository.save_case_draft(case_id, request)

    @app.post(
        "/api/eval/cases/{case_id}/draft/publish",
        response_model=CaseRevisionView,
        dependencies=protected,
    )
    async def publish_case_draft(
        case_id: str,
        request: PublishCaseDraftRequest,
    ) -> CaseRevisionView:
        """原子生成 reviewed case revision，旧 active 不变。"""

        return repository.publish_case_draft(
            case_id,
            reviewer=request.reviewer,
        )

    @app.post(
        "/api/eval/cases/revisions/{revision_id}/review",
        response_model=CaseRevisionView,
        dependencies=protected,
    )
    async def review_case_revision(
        revision_id: str,
        request: ReviewRevisionRequest,
    ) -> CaseRevisionView:
        """推进 reviewed／active／retired 状态。"""

        return repository.review_case_revision(
            revision_id,
            review_status=request.review_status,
            reviewer=request.reviewer,
        )

    @app.post(
        "/api/eval/suites/{suite_id}/draft",
        response_model=SuiteDraftView,
        status_code=status.HTTP_201_CREATED,
        dependencies=protected,
    )
    async def create_suite_draft(
        suite_id: str,
        request: StartDraftRequest,
    ) -> SuiteDraftView:
        """从当前 suite 派生可编辑成员草稿。"""

        return repository.create_suite_draft(
            suite_id,
            updated_by=request.updated_by,
        )

    @app.get(
        "/api/eval/suites/{suite_id}/draft",
        response_model=SuiteDraftView,
        dependencies=protected,
    )
    async def get_suite_draft(suite_id: str) -> SuiteDraftView:
        """读取当前 suite draft。"""

        return repository.get_suite_draft(suite_id)

    @app.put(
        "/api/eval/suites/{suite_id}/draft",
        response_model=SuiteDraftView,
        dependencies=protected,
    )
    async def save_suite_draft(
        suite_id: str,
        request: SaveSuiteDraftRequest,
    ) -> SuiteDraftView:
        """保存 suite 元数据和精确 case revision 子集。"""

        return repository.save_suite_draft(suite_id, request)

    @app.post(
        "/api/eval/suites/{suite_id}/draft/publish",
        response_model=SuiteRevisionView,
        dependencies=protected,
    )
    async def publish_suite_draft(
        suite_id: str,
        request: PublishSuiteDraftRequest,
    ) -> SuiteRevisionView:
        """发布并激活 suite revision，旧 active 自动退休。"""

        return repository.publish_suite_draft(
            suite_id,
            reviewer=request.reviewer,
        )

    @app.get(
        "/api/eval/collections/current",
        response_model=CurrentCollectionResponse | None,
        dependencies=protected,
    )
    async def current_collection() -> CurrentCollectionResponse | None:
        """返回产品知识库当前活动快照，供运行前显式冻结。"""

        return collection_service.current()

    @app.post(
        "/api/eval/plans",
        response_model=ExecutionPlanView,
        dependencies=protected,
    )
    async def create_plan(request: PreflightRequest) -> ExecutionPlanView:
        """执行预检并保存不可变 ExecutionPlan。"""

        return planner.preflight(
            request,
            runtime_settings=runtime_settings_service.get(),
        )

    @app.post(
        "/api/eval/quick-plans",
        response_model=ExecutionPlanView,
        dependencies=protected,
    )
    async def create_quick_plan(request: QuickPlanRequest) -> ExecutionPlanView:
        """创建不依赖 suite 的探索性 ExecutionPlan。"""

        return planner.quick_preflight(
            request,
            runtime_settings=runtime_settings_service.get(),
        )

    @app.get(
        "/api/eval/plans/{plan_id}",
        response_model=ExecutionPlanView,
        dependencies=protected,
    )
    async def get_plan(plan_id: str) -> ExecutionPlanView:
        """读取一个 ExecutionPlan。"""

        return repository.get_plan(plan_id)

    @app.get(
        "/api/eval/runs",
        response_model=list[RunView],
        dependencies=protected,
    )
    async def list_runs() -> list[RunView]:
        """返回最近运行记录。"""

        return repository.list_runs()

    @app.get(
        "/api/eval/runs/active",
        response_model=RunView | None,
        dependencies=protected,
    )
    async def active_run() -> RunView | None:
        """返回当前未终止运行。"""

        return repository.active_run()

    @app.get(
        "/api/eval/runs/{run_id}",
        response_model=RunView,
        dependencies=protected,
    )
    async def get_run(run_id: str) -> RunView:
        """轮询运行进度与 attempts。"""

        return repository.get_run(run_id)

    @app.post(
        "/api/eval/runs",
        response_model=RunView,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=protected,
    )
    async def start_run(request: StartRunRequest) -> RunView:
        """从 planned ExecutionPlan 启动后台运行。"""

        return await runner.start(request.plan_id)

    @app.post(
        "/api/eval/runs/{run_id}/pause",
        response_model=RunView,
        dependencies=protected,
    )
    async def pause_run(run_id: str) -> RunView:
        """请求在 attempt 安全边界暂停。"""

        return await runner.pause(run_id)

    @app.post(
        "/api/eval/runs/{run_id}/resume",
        response_model=RunView,
        dependencies=protected,
    )
    async def resume_run(run_id: str) -> RunView:
        """继续缺少有效样本的 run。"""

        return await runner.resume(run_id)

    @app.post(
        "/api/eval/runs/{run_id}/cancel",
        response_model=RunView,
        dependencies=protected,
    )
    async def cancel_run(run_id: str) -> RunView:
        """请求在 attempt 安全边界取消。"""

        return await runner.cancel(run_id)

    @app.get(
        "/api/eval/traces",
        response_model=list[TraceListItem],
        dependencies=protected,
    )
    async def list_traces() -> list[TraceListItem]:
        """列出评测数据根中的 trace。"""

        return trace_catalog.list_recent(200)

    @app.get(
        "/api/eval/traces/{trace_id}",
        response_model=TraceDetailRecord,
        dependencies=protected,
    )
    async def get_trace(trace_id: str) -> TraceDetailRecord:
        """读取一次评测 attempt 的完整 trace。"""

        try:
            return trace_catalog.load(trace_id)
        except TraceNotFoundError as error:
            raise IngestFailure(
                "evaluation_trace_not_found",
                "evaluation trace does not exist",
                404,
            ) from error
        except TraceInvalidError as error:
            raise IngestFailure(
                "evaluation_trace_invalid",
                str(error),
                500,
            ) from error

    return app


def _path_from_environment(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": False,
            }
        },
    )
