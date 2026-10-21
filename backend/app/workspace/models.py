from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ResetWorkspaceRequest(BaseModel):
    """清空知识库前要求用户输入的固定确认短语。"""

    confirmation: Literal["清空知识库"]


class ResetWorkspaceResponse(BaseModel):
    """一次知识库重置清除的数据库记录数量。"""

    status: Literal["reset"] = "reset"
    documents: int = Field(ge=0)
    document_versions: int = Field(ge=0)
    source_artifacts: int = Field(ge=0)
    ingestion_jobs: int = Field(ge=0)
    ingestion_tasks: int = Field(ge=0)
    collection_publish_records: int = Field(ge=0)
    active_collection_pointer: int = Field(ge=0)
    cleanup_complete: bool
