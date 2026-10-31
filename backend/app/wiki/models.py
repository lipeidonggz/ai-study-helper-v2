from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WikiParagraph(BaseModel):
    """带原文 block 回源的一页中的一个段落。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    source_block_ids: list[str] = Field(min_length=1)


class WikiPageDraft(BaseModel):
    """模型为一页返回的唯一允许输出结构。"""

    model_config = ConfigDict(extra="forbid")

    paragraphs: list[WikiParagraph] = Field(min_length=1)


class WikiPageSpec(BaseModel):
    """调用方明确指定的一页主题及其可用证据边界。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    coverage_questions: list[str] = Field(default_factory=list)
    source_block_ids: list[str] = Field(min_length=1)


class WikiCompileRequest(BaseModel):
    """一次全有或全无的 Wiki 页面集合编译请求。"""

    model_config = ConfigDict(extra="forbid")

    pages: list[WikiPageSpec] = Field(min_length=1)


class WikiPageArtifact(BaseModel):
    """可持久化并可由 block 回源验证的一页 Wiki。"""

    model_config = ConfigDict(extra="forbid")

    page_id: str = Field(pattern=r"^wp_[0-9a-f]{16}$")
    title: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    coverage_questions: list[str] = Field(default_factory=list)
    paragraphs: list[WikiParagraph] = Field(min_length=1)


class WikiProviderProfile(BaseModel):
    """一次 Wiki build 使用的对话模型固定参数。"""

    provider: str
    model_id: str
    temperature: float
    max_output_tokens: int
    thinking_enabled: bool


class WikiPageUsage(BaseModel):
    """一页 Wiki 生成实际消耗的供应商 usage。"""

    page_id: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class WikiBuildPage(BaseModel):
    """manifest 中一页 artifact、候选证据和覆盖统计。"""

    page_id: str = Field(pattern=r"^wp_[0-9a-f]{16}$")
    title: str
    artifact_ref: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=0)
    candidate_block_ids: list[str] = Field(min_length=1)
    candidate_block_count: int = Field(ge=1)
    referenced_block_count: int = Field(ge=1)
    unreferenced_block_ids: list[str]


class WikiBuildManifest(BaseModel):
    """一个不可变 Wiki build 的完整来源、版本与产物清单。"""

    schema_version: Literal["wiki-v1"] = "wiki-v1"
    build_id: str = Field(pattern=r"^w_[0-9a-f]{16}$")
    snapshot_id: str
    snapshot_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiler_version: str
    prompt_version: str
    prompt_template_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: WikiProviderProfile
    pages: list[WikiBuildPage] = Field(min_length=1)
    usage: list[WikiPageUsage] = Field(min_length=1)
    created_at: str
    manifest_hash: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")


class WikiCompileResult(BaseModel):
    """成功编译的 manifest 与可直接读取的页面。"""

    manifest: WikiBuildManifest
    pages: list[WikiPageArtifact]
