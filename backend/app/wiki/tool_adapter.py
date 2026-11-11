from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.tool_adapter import (
    ToolDescriptor,
    ToolExecution,
    ToolUnavailableError,
    _output_schema,
)
from app.providers.chat import ChatProvider
from app.providers.embeddings import EmbeddingProvider
from app.retrieval.models import RetrievalErrorPayload, RetrievalToolErrorPayload
from app.wiki.build_models import (
    WikiPagesResult,
    WikiTopicEvidenceResult,
    WikiTopicSearchResult,
)
from app.wiki.build_service import WikiBuildService
from app.wiki.errors import WikiCompilationError
from app.wiki.narrative_service import WikiNarrativeService
from app.wiki.profile_knowledge_map import (
    ProfileKnowledgeMapError,
    SearchWikiTopicV2Service,
)
from app.wiki.profile_knowledge_map_models import SearchWikiTopicV2Result

WIKI_TOOL_CONTRACT_VERSION = "wiki-tools-v2"


class _WikiToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchWikiTopicsArgs(_WikiToolArgs):
    """`search_wiki_topics` 参数。"""

    query: str = Field(min_length=1)
    semantic_tag: str | None = None


class ReadWikiPageArgs(_WikiToolArgs):
    """`read_wiki_page` 参数。"""

    topic_ids: list[str] = Field(min_length=1)


class GetTopicEvidenceArgs(_WikiToolArgs):
    """`get_topic_evidence` 参数。"""

    topic_ids: list[str] = Field(min_length=1)


class SearchWikiTopicV2Args(_WikiToolArgs):
    """`search_wiki_topic_v2` 参数。"""

    topic: str = Field(min_length=1)


def wiki_tool_descriptors() -> list[ToolDescriptor]:
    """返回与具体用例无关的 Wiki 工具描述。"""

    return [
        ToolDescriptor(
            name="search_wiki_topics",
            description=(
                "从当前 collection snapshot 的预编译 Wiki 主题台账中查找主题入口。"
                "按标题、别名、摘要、正文和独立语义索引召回；结果是一个不截断的全量轻量目录，"
                "给出 topic_id、证据数量与覆盖标签，不直接回答用户问题。"
                "当前知识库范围内的主题枚举、开放找全或跨来源问题应先用它确定候选入口；"
                "应浏览整个目录并按语义分组选择多个入口，再按需读取完整证据。"
                "具体文档问题不必先调用。没有结果不等于知识库不存在相关内容。"
            ),
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "semantic_tag": {"type": ["string", "null"]},
                },
                "additionalProperties": False,
            },
            output_schema=_output_schema(WikiTopicSearchResult),
        ),
        ToolDescriptor(
            name="read_wiki_page",
            description=(
                "按 topic_id 读取已预编译的 Wiki 页面。叙述页包含段落级 source_block_ids；"
                "catalog 页面明确表示只有元数据和证据入口，不是完整正文。"
            ),
            input_schema={
                "type": "object",
                "required": ["topic_ids"],
                "properties": {
                    "topic_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    }
                },
                "additionalProperties": False,
            },
            output_schema=_output_schema(WikiPagesResult),
        ),
        ToolDescriptor(
            name="get_topic_evidence",
            description=(
                "按 topic_id 返回当前 snapshot 中该主题的全部直接证据块和完整正文。"
                "需要事实回答或精确引用时必须使用本工具回到原始 block。"
            ),
            input_schema={
                "type": "object",
                "required": ["topic_ids"],
                "properties": {
                    "topic_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    }
                },
                "additionalProperties": False,
            },
            output_schema=_output_schema(WikiTopicEvidenceResult),
        ),
        ToolDescriptor(
            name="search_wiki_topic_v2",
            description=(
                "在 Profile-driven Wiki 知识地图中按 topic 检索证据。"
                "输入应是主题短语而不是完整问题；只返回当前 snapshot 的完整原始 evidence blocks，"
                "不返回 topic 目录、summary 或最终答案。"
            ),
            input_schema={
                "type": "object",
                "required": ["topic"],
                "properties": {"topic": {"type": "string"}},
                "additionalProperties": False,
            },
            output_schema=_output_schema(SearchWikiTopicV2Result),
        ),
    ]


class WikiToolAdapter:
    """把 active Wiki build 和 Profile-driven 知识地图暴露为只读 Agent 工具。"""

    def __init__(
        self,
        data_root: Path,
        *,
        snapshot_id: str,
        embedding_provider: EmbeddingProvider,
        chat_provider: ChatProvider,
        knowledge_map_id: str | None = None,
        enable_legacy_tools: bool = False,
    ) -> None:
        self._builds = WikiBuildService(data_root)
        self._narratives = WikiNarrativeService(data_root)
        self._snapshot_id = snapshot_id
        self._embedding_provider = embedding_provider
        self._chat_provider = chat_provider
        self._topic_v2 = SearchWikiTopicV2Service(data_root)
        self._knowledge_map_id = knowledge_map_id
        available_names: set[str] = set()
        if enable_legacy_tools:
            available_names.update({"search_wiki_topics", "read_wiki_page", "get_topic_evidence"})
        if knowledge_map_id is not None:
            available_names.add("search_wiki_topic_v2")
        self._descriptors = {
            descriptor.name: descriptor
            for descriptor in wiki_tool_descriptors()
            if descriptor.name in available_names
        }

    @property
    def descriptors(self) -> list[ToolDescriptor]:
        """返回当前 Wiki 工具集。"""

        return list(self._descriptors.values())

    @property
    def contract_version(self) -> str:
        """返回 Wiki 工具契约版本。"""

        return WIKI_TOOL_CONTRACT_VERSION

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> ToolExecution:
        """执行 Wiki 工具，业务失败转换为公共 error payload。"""

        if tool_name not in self._descriptors:
            raise ToolUnavailableError(tool_name)
        try:
            if tool_name == "search_wiki_topics":
                parsed = SearchWikiTopicsArgs.model_validate(arguments)
                result = await self._builds.search_topics(
                    self._snapshot_id,
                    parsed.query,
                    semantic_tag=parsed.semantic_tag,
                    embedding_provider=self._embedding_provider,
                )
                return ToolExecution(
                    payload=result.model_dump(mode="json"),
                    diagnostics={
                        "result_count": result.total,
                        "build_id": result.build_id,
                        "topic_ids": [topic.topic_id for topic in result.topics],
                    },
                )
            if tool_name == "read_wiki_page":
                parsed = ReadWikiPageArgs.model_validate(arguments)
                pages = [
                    self._narratives.read_page(self._snapshot_id, topic_id).page
                    for topic_id in dict.fromkeys(parsed.topic_ids)
                ]
                result = WikiPagesResult(
                    snapshot_id=self._snapshot_id,
                    build_id=self._builds.active_for_snapshot(self._snapshot_id).build_id,
                    pages=pages,
                )
                return ToolExecution(
                    payload=result.model_dump(mode="json"),
                    diagnostics={
                        "topic_ids": [page.topic_id for page in pages],
                        "page_ids": [page.page_id for page in pages],
                    },
                )
            if tool_name == "get_topic_evidence":
                parsed = GetTopicEvidenceArgs.model_validate(arguments)
                result = self._builds.get_topic_evidence(
                    self._snapshot_id,
                    parsed.topic_ids,
                )
                return ToolExecution(
                    payload=result.model_dump(mode="json"),
                    diagnostics={
                        "topic_ids": [topic.topic_id for topic in result.topics],
                        "block_ids": [
                            block.block_id for topic in result.topics for block in topic.blocks
                        ],
                    },
                )
            if tool_name == "search_wiki_topic_v2":
                parsed = SearchWikiTopicV2Args.model_validate(arguments)
                if self._knowledge_map_id is None:
                    return ToolExecution(
                        payload=_public_error(
                            "wiki_topic_v2_not_ready",
                            "no Profile-driven knowledge map is ready for the snapshot",
                        )
                    )
                execution = await self._topic_v2.search(
                    snapshot_id=self._snapshot_id,
                    map_id=self._knowledge_map_id,
                    topic=parsed.topic,
                    embedding_provider=self._embedding_provider,
                    provider=self._chat_provider,
                )
                return ToolExecution(
                    payload=execution.result.model_dump(mode="json"),
                    diagnostics={
                        "map_id": self._knowledge_map_id,
                        **execution.model_dump(mode="json", exclude={"result"}),
                    },
                )
        except ValidationError as error:
            return ToolExecution(payload=_public_error("invalid_argument", str(error)))
        except WikiCompilationError as error:
            return ToolExecution(
                payload=_public_error(
                    error.code,
                    error.message,
                    retryable=error.retryable,
                )
            )
        except ProfileKnowledgeMapError as error:
            return ToolExecution(
                payload=_public_error(error.code, error.message),
                diagnostics=error.diagnostics,
            )
        except Exception as error:
            return ToolExecution(
                payload=_public_error(
                    "wiki_tool_internal_error",
                    f"wiki tool failed: {type(error).__name__}",
                )
            )
        raise AssertionError("known Wiki tool dispatch must return")


def _public_error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> dict[str, Any]:
    return RetrievalToolErrorPayload(
        error=RetrievalErrorPayload(
            code=code,
            message=message,
            retryable=retryable,
        )
    ).model_dump(mode="json")
