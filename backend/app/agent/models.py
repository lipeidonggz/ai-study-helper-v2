from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentRunRequest(BaseModel):
    """内部 Agent 单轮问答请求。"""

    question: str = Field(min_length=1)


class AgentLimitation(BaseModel):
    """运行限制对最终回答完整性的确定性说明。"""

    code: str
    message: str
    parameter: str | None = None
    limit: int | None = None


class AgentRunEvent(BaseModel):
    """Agent 运行事件的外部表示。"""

    event_type: str
    payload: dict[str, Any]


class AgentRunResponse(BaseModel):
    """Agent 单轮运行的最终结果与审计事件。"""

    status: Literal["succeeded", "partial", "failed", "canceled"]
    answer: str | None
    answer_completeness: Literal["complete", "partial"]
    limitations: list[AgentLimitation]
    termination_reason: str
    snapshot_id: str | None
    prompt_bundle_version: str
    prompt_template_hash: str
    observation_token_counter_version: str
    agent_turns: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    events: list[AgentRunEvent]


class AgentRunStartResponse(BaseModel):
    """异步流式 Agent 运行的启动句柄。"""

    run_id: str
    trace_id: str
    status: Literal["running"] = "running"
