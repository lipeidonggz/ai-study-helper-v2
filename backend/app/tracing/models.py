from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

TraceMode = Literal["off", "metadata", "full"]
TraceStatus = Literal["complete", "partial", "dropped"]
ReplayScope = Literal["metadata", "evidence", "retrieval"]
TRACE_SCHEMA_VERSION = "agent-trace-v1"


class TraceArtifactRecord(BaseModel):
    """trace 内按内容寻址的 artifact 引用。"""

    artifact_id: str
    kind: str
    mime_type: str
    size_bytes: int = Field(ge=0)
    content_hash: str
    state: Literal["stored", "omitted", "missing"]
    relative_path: str | None = None


class TraceSpanEvent(BaseModel):
    """挂在 span 上的不可变事件。"""

    name: str
    time: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class TraceSpanRecord(BaseModel):
    """遵循 OTel 字段口径的一条本地 span。"""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: Literal["INTERNAL", "LLM", "TOOL"]
    start_time_unix_nano: int
    end_time_unix_nano: int
    started_at: str
    ended_at: str
    status: Literal["ok", "error"]
    error_code: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    events: list[TraceSpanEvent] = Field(default_factory=list)


class TraceManifestRecord(BaseModel):
    """一次问答运行关闭后的本地 trace 摘要。"""

    schema_version: str = TRACE_SCHEMA_VERSION
    trace_id: str
    mode: TraceMode
    app_version: str
    started_at: str
    ended_at: str
    duration_ms: int = Field(ge=0)
    status: Literal["succeeded", "partial", "failed", "canceled"]
    termination_reason: str
    answer_completeness: Literal["complete", "partial"]
    question_preview: str | None = None
    question_artifact: TraceArtifactRecord | None = None
    snapshot_id: str | None = None
    snapshot_status: str | None = None
    completeness: str | None = None
    agent_turns: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    budget: dict[str, Any] = Field(default_factory=dict)
    versions: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[TraceArtifactRecord] = Field(default_factory=list)
    trace_status: TraceStatus = "complete"
    dropped_events: int = Field(default=0, ge=0)
    replay_scope: ReplayScope = "metadata"
    limitations: list[dict[str, Any]] = Field(default_factory=list)


class TraceListItem(BaseModel):
    """诊断列表中的一条运行摘要。"""

    trace_id: str
    mode: TraceMode
    started_at: str
    ended_at: str
    status: str
    termination_reason: str
    question_preview: str | None = None
    duration_ms: int
    input_tokens: int
    output_tokens: int
    trace_status: TraceStatus
    replay_scope: ReplayScope


class TraceDetailRecord(BaseModel):
    """一次 trace 的 manifest 与 span 树原始材料。"""

    manifest: TraceManifestRecord
    spans: list[TraceSpanRecord]
