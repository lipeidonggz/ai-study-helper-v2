from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from app.ingestion.models import DocumentLanguage
from app.providers.embeddings import EmbeddingProvider
from app.retrieval.errors import RetrievalFailure
from app.retrieval.hybrid import DEFAULT_HYBRID_RETRIEVAL_PROFILE, HybridRetrievalProfile
from app.retrieval.service import RetrievalSnapshotReader

TOOL_CONTRACT_VERSION = "retrieval-tools-v1"
TOOL_WHITELIST = (
    "list_documents",
    "get_outline",
    "search_blocks",
    "list_blocks",
    "scan_term",
)


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """一个工具对模型展示的说明与输入 schema。"""

    name: str
    description: str
    input_schema: dict[str, Any]

    def render_for_prompt(self) -> str:
        """渲染为模型可读的工具说明。"""

        schema = json.dumps(
            self.input_schema,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return f"- {self.name}: {self.description}\n  输入 schema: {schema}"


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """一次工具执行的 Agent payload 与内部诊断。"""

    payload: dict[str, Any]
    diagnostics: dict[str, Any] = field(default_factory=dict)


class ToolUnavailableError(Exception):
    """模型请求了白名单之外的工具。"""


class _ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListDocumentsArgs(_ToolArgs):
    """`list_documents` 参数。"""

    source: str | None = None
    lang: DocumentLanguage | None = None
    title_contains: str | None = None


class GetOutlineArgs(_ToolArgs):
    """`get_outline` 参数。"""

    doc_ids: list[str]


class SearchBlocksArgs(_ToolArgs):
    """`search_blocks` 参数。"""

    query: str
    doc_ids: list[str] | None = None


class ListBlocksArgs(_ToolArgs):
    """`list_blocks` 参数。"""

    doc_id: str
    section_paths: list[list[str]] | None = None


class ScanTermArgs(_ToolArgs):
    """`scan_term` 参数。"""

    term: str
    doc_ids: list[str] | None = None


def tool_descriptors() -> list[ToolDescriptor]:
    """返回当前版本冻结的五个工具描述。"""

    return [
        ToolDescriptor(
            name="list_documents",
            description=(
                "列出知识库中的文档，可按来源、语言、标题片段筛选。"
                "当用户所指文档不明确，或需要先了解知识库有哪些文档时调用。"
                "来源支持用户常用别名；标题筛选只匹配规范标题子串。"
                "别名只用于定位，不改变文档的规范身份。"
                "返回文档清单和总数；零结果不能推断正文中不存在相关内容。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source": {"type": ["string", "null"]},
                    "lang": {
                        "type": ["string", "null"],
                        "enum": ["zh", "en", None],
                    },
                    "title_contains": {"type": ["string", "null"]},
                },
                "additionalProperties": False,
            },
        ),
        ToolDescriptor(
            name="get_outline",
            description=(
                "查看一篇或多篇文档的章节结构，返回章节路径、规模与首句。"
                "需要建立整体图景、换一种形态整理，或先确定后续读取哪些章节时调用；"
                "返回结果是结构信息，不是文档内容的完整证据。"
            ),
            input_schema={
                "type": "object",
                "required": ["doc_ids"],
                "properties": {
                    "doc_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1}
                },
                "additionalProperties": False,
            },
        ),
        ToolDescriptor(
            name="search_blocks",
            description=(
                "需要按语义查找候选证据时调用；按语义与关键词混合检索相关块，"
                "可按文档限定范围，返回排序后的前 5 条候选（块引用＋全文）。"
                "它是候选召回，不是完整枚举；确认精确字符串是否存在应使用 scan_term。"
            ),
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "doc_ids": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                },
                "additionalProperties": False,
            },
        ),
        ToolDescriptor(
            name="list_blocks",
            description=(
                "需要完整读取或按章节取全某一篇文档时调用；范围可以是整篇，"
                "也可以是若干章节。工具返回该范围的全部块，不做上下文预算判断；"
                "跨多篇时按篇分别调用。一个 section_path 选择该节及其全部后代。"
            ),
            input_schema={
                "type": "object",
                "required": ["doc_id"],
                "properties": {
                    "doc_id": {"type": "string"},
                    "section_paths": {
                        "type": ["array", "null"],
                        "items": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "additionalProperties": False,
            },
        ),
        ToolDescriptor(
            name="scan_term",
            description=(
                "需要确认某个具体字符串在指定范围内是否存在，或核对字面归属时调用；"
                "按原文字符串扫描，大小写不敏感，不做分词、词干或同义扩展。"
                "工具返回当前范围的全部命中块与命中数，不做上下文预算判断。"
                "需要概念或语义匹配时使用 search_blocks；count=0 只表示该字符串没有出现。"
            ),
            input_schema={
                "type": "object",
                "required": ["term"],
                "properties": {
                    "term": {"type": "string"},
                    "doc_ids": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                },
                "additionalProperties": False,
            },
        ),
    ]


class RetrievalToolAdapter:
    """把模型 Action 参数映射到固定快照的五个检索能力。"""

    def __init__(
        self,
        reader: RetrievalSnapshotReader,
        embedding_provider: EmbeddingProvider,
        *,
        search_profile: HybridRetrievalProfile = DEFAULT_HYBRID_RETRIEVAL_PROFILE,
    ) -> None:
        self._reader = reader
        self._embedding_provider = embedding_provider
        self._search_profile = search_profile
        self._descriptors = {descriptor.name: descriptor for descriptor in tool_descriptors()}

    @property
    def descriptors(self) -> list[ToolDescriptor]:
        """返回模型可使用的工具描述。"""

        return list(self._descriptors.values())

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> ToolExecution:
        """校验参数并执行一个工具；业务失败作为公共 error payload 返回。"""

        if tool_name not in self._descriptors:
            raise ToolUnavailableError(tool_name)
        try:
            if tool_name == "list_documents":
                parsed = ListDocumentsArgs.model_validate(arguments)
                result = self._reader.list_documents(
                    source=parsed.source,
                    lang=parsed.lang,
                    title_contains=parsed.title_contains,
                )
                return ToolExecution(
                    payload=result.model_dump(mode="json"),
                    diagnostics={"result_count": result.total},
                )
            if tool_name == "get_outline":
                parsed = GetOutlineArgs.model_validate(arguments)
                result = self._reader.get_outline(parsed.doc_ids)
                documents = []
                for item in result.documents:
                    if item.error is not None:
                        documents.append(
                            {
                                "doc_id": item.doc_id,
                                "error": item.error.model_dump(mode="json"),
                            }
                        )
                    elif item.document is not None:
                        documents.append(item.document.model_dump(mode="json"))
                return ToolExecution(
                    payload={"documents": documents},
                    diagnostics={"result_count": len(documents)},
                )
            if tool_name == "search_blocks":
                parsed = SearchBlocksArgs.model_validate(arguments)
                execution = await self._reader.execute_search_blocks(
                    parsed.query,
                    provider=self._embedding_provider,
                    doc_ids=parsed.doc_ids,
                    profile=self._search_profile,
                )
                diagnostics = {
                    "search_profile": asdict(execution.profile),
                    "query_request_id": execution.query_request_id,
                    "query_input_tokens": execution.query_input_tokens,
                    "dense_candidates": list(execution.dense_block_ids),
                    "keyword_candidates": list(execution.keyword_block_ids),
                    "fused_candidates": [
                        asdict(candidate) for candidate in execution.fused_candidates
                    ],
                    "result_count": len(execution.result.blocks),
                    "empty_reason": execution.result.empty_reason,
                }
                return ToolExecution(
                    payload=execution.result.model_dump(mode="json"),
                    diagnostics=diagnostics,
                )
            if tool_name == "list_blocks":
                parsed = ListBlocksArgs.model_validate(arguments)
                result = self._reader.list_blocks(
                    parsed.doc_id,
                    parsed.section_paths,
                )
                return ToolExecution(
                    payload=result.model_dump(mode="json"),
                    diagnostics={"result_count": result.total},
                )
            if tool_name == "scan_term":
                parsed = ScanTermArgs.model_validate(arguments)
                result = self._reader.scan_term(parsed.term, parsed.doc_ids)
                return ToolExecution(
                    payload=result.model_dump(mode="json"),
                    diagnostics={"result_count": result.count},
                )
        except ValidationError as error:
            return ToolExecution(payload=_public_error("invalid_argument", str(error)))
        except RetrievalFailure as error:
            return ToolExecution(
                payload=_public_error(
                    error.code,
                    error.message,
                    retryable=error.retryable,
                )
            )
        except Exception as error:
            return ToolExecution(
                payload=_public_error(
                    "tool_internal_error",
                    f"tool failed: {type(error).__name__}",
                )
            )
        raise AssertionError("known tool dispatch must return")


def _public_error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        }
    }
