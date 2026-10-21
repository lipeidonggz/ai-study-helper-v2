from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

TaskStatus = Literal[
    "queued",
    "running",
    "paused",
    "succeeded",
    "failed",
    "canceled",
    "interrupted",
]


class IngestTaskSource(BaseModel):
    """后台任务中的一个本地文件或 URL 来源。"""

    source_kind: Literal["file", "url"]
    source_value: str = Field(min_length=1)


class BackgroundIngestRequest(BaseModel):
    """启动一次后台批量入库。"""

    sources: list[IngestTaskSource] = Field(min_length=1, max_length=1000)


class PreviewIngestTaskRequest(BaseModel):
    """把已完成 S0-S4 的单篇预览 job 提交为完整入库任务。"""

    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class BackgroundSourceFailure(BaseModel):
    """后台任务中一个来源的失败摘要。"""

    source_index: int = Field(ge=0)
    source_kind: Literal["file", "url"]
    job_id: str | None = None
    stage: str
    code: str
    message: str
    retryable: bool = False


class BackgroundTaskRecord(BaseModel):
    """后台入库任务的可查询状态。"""

    task_id: str
    status: TaskStatus
    stage: str
    sources: list[IngestTaskSource]
    total_sources: int = Field(ge=0)
    next_source_index: int = Field(ge=0)
    completed_sources: int = Field(ge=0)
    failed_sources: int = Field(ge=0)
    successful_job_ids: list[str] = Field(default_factory=list)
    failures: list[BackgroundSourceFailure] = Field(default_factory=list)
    snapshot_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None


class BackgroundTaskControlResponse(BaseModel):
    """暂停、恢复或取消操作后的任务状态。"""

    task: BackgroundTaskRecord
