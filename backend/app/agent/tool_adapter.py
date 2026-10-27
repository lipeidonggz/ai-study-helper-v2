from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from app.ingestion.models import DocumentLanguage
from app.providers.embeddings import EmbeddingProvider
from app.retrieval.errors import RetrievalFailure
from app.retrieval.hybrid import DEFAULT_HYBRID_RETRIEVAL_PROFILE, HybridRetrievalProfile
from app.retrieval.models import (
    GetBlocksResult,
    GetOutlinePayload,
    ListBlocksResult,
    ListDocumentsResult,
    RetrievalErrorPayload,
    RetrievalOutlineErrorItem,
    RetrievalToolErrorPayload,
    ScanTermResult,
    SearchBlocksResult,
)
from app.retrieval.service import RetrievalSnapshotReader

TOOL_CONTRACT_VERSION = "retrieval-tools-v5"
TOOL_WHITELIST = (
    "list_documents",
    "get_outline",
    "search_blocks",
    "list_blocks",
    "get_blocks",
    "scan_term",
)


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """一个工具对模型展示的说明与输入 schema。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]

    def render_for_prompt(self) -> str:
        """渲染为模型可读的工具说明。"""

        input_schema = json.dumps(
            self.input_schema,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        output_schema = json.dumps(
            self.output_schema,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return (
            f"- {self.name}: {self.description}\n"
            f"  输入 schema: {input_schema}\n"
            f"  输出 schema: {output_schema}"
        )


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
    include_descendants: bool | None = None

    @model_validator(mode="after")
    def validate_scope_contract(self) -> ListBlocksArgs:
        """要求整篇与章节范围使用互斥且显式的参数组合。"""

        has_sections = bool(self.section_paths)
        if has_sections and self.include_descendants is None:
            raise ValueError("include_descendants is required when section_paths is not empty")
        if not has_sections and self.include_descendants is not None:
            raise ValueError("include_descendants is only valid with non-empty section_paths")
        return self


class GetBlocksArgs(_ToolArgs):
    """`get_blocks` 参数。"""

    block_ids: list[str] = Field(min_length=1)


class ScanTermArgs(_ToolArgs):
    """`scan_term` 参数。"""

    term: str
    doc_ids: list[str] | None = None


def tool_descriptors() -> list[ToolDescriptor]:
    """返回当前版本冻结的六个工具描述。"""

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
            output_schema=_output_schema(ListDocumentsResult),
        ),
        ToolDescriptor(
            name="get_outline",
            description=(
                "查看一篇或多篇文档的章节结构，返回章节路径、规模与首句。"
                "需要建立整体图景、换一种形态整理，或先确定后续读取哪些章节时调用；"
                "返回结果是结构信息，不是文档内容的完整证据。"
                "若文档未识别出章节标题，返回 path=[]、title=文档标题、depth=0 的默认根节；"
                "这是解析 fallback，不表示原文在语义上只有一个章节。"
            ),
            input_schema={
                "type": "object",
                "required": ["doc_ids"],
                "properties": {
                    "doc_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1}
                },
                "additionalProperties": False,
            },
            output_schema=_output_schema(GetOutlinePayload),
        ),
        ToolDescriptor(
            name="search_blocks",
            description=(
                "需要按语义查找候选证据时调用；按语义与关键词混合检索相关块，"
                "可按文档限定范围，返回排序后的前 5 条候选（块引用＋完整正文）。"
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
            output_schema=_output_schema(SearchBlocksResult),
        ),
        ToolDescriptor(
            name="list_blocks",
            description=(
                "需要完整读取某一篇文档或显式选择章节时调用。"
                "省略 section_paths 表示整篇；传入章节时必须明确 include_descendants："
                "true 返回该节及全部后代，false 只返回该节直属块。"
                "工具返回范围内全部块，不做上下文预算判断；跨多篇时按篇分别调用。"
            ),
            input_schema={
                "type": "object",
                "required": ["doc_id"],
                "properties": {
                    "doc_id": {"type": "string"},
                    "section_paths": {
                        "type": ["array", "null"],
                        "items": {"type": "array", "items": {"type": "string"}},
                        "description": ("省略或空数组表示整篇；非空时每项是稳定 section_path"),
                    },
                    "include_descendants": {
                        "type": ["boolean", "null"],
                        "description": (
                            "section_paths 非空时必传；true=本章节及后代，"
                            "false=仅直属块。整篇请求不得传此字段。"
                        ),
                    },
                },
                "additionalProperties": False,
            },
            output_schema=_output_schema(ListBlocksResult),
        ),
        ToolDescriptor(
            name="get_blocks",
            description=(
                "需要按已知 block_id 精确补全完整正文时调用。"
                "scan_term 返回 snippet；若该 snippet 足以定位且需要完整证据，"
                "应使用本工具读取原 block。"
                "search_blocks 已返回完整正文，不需要再次调用本工具。"
                "任一 block_id 不存在时整次失败，不返回部分结果。"
            ),
            input_schema={
                "type": "object",
                "required": ["block_ids"],
                "properties": {
                    "block_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    }
                },
                "additionalProperties": False,
            },
            output_schema=_output_schema(GetBlocksResult),
        ),
        ToolDescriptor(
            name="scan_term",
            description=(
                "需要确认某个具体字符串在指定范围内是否存在，或核对字面归属时调用；"
                "按原文字符串扫描，大小写不敏感，不做分词、词干或同义扩展。"
                "工具返回当前范围的全部命中块、命中数及句界完整上下文，不做上下文预算判断。"
                "snippet 只用于定位；需要完整 block 时使用 get_blocks。"
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
            output_schema=_output_schema(ScanTermResult),
        ),
    ]


class RetrievalToolAdapter:
    """把模型 Action 参数映射到固定快照的六个检索能力。"""

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
                    payload=_validated_payload(result),
                    diagnostics={"result_count": result.total},
                )
            if tool_name == "get_outline":
                parsed = GetOutlineArgs.model_validate(arguments)
                result = self._reader.get_outline(parsed.doc_ids)
                documents = []
                for item in result.documents:
                    if item.error is not None:
                        documents.append(
                            RetrievalOutlineErrorItem(
                                doc_id=item.doc_id,
                                error=item.error,
                            )
                        )
                    elif item.document is not None:
                        documents.append(item.document)
                payload = GetOutlinePayload(documents=documents)
                return ToolExecution(
                    payload=_validated_payload(payload),
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
                    payload=_validated_payload(execution.result),
                    diagnostics=diagnostics,
                )
            if tool_name == "list_blocks":
                parsed = ListBlocksArgs.model_validate(arguments)
                result = self._reader.list_blocks(
                    parsed.doc_id,
                    parsed.section_paths,
                    include_descendants=parsed.include_descendants,
                )
                return ToolExecution(
                    payload=_validated_payload(result),
                    diagnostics={
                        "result_count": result.total,
                        "scope": result.scope,
                        "requested_section_count": len(parsed.section_paths or []),
                        "include_descendants": parsed.include_descendants,
                    },
                )
            if tool_name == "get_blocks":
                parsed = GetBlocksArgs.model_validate(arguments)
                result = self._reader.get_blocks(parsed.block_ids)
                return ToolExecution(
                    payload=_validated_payload(result),
                    diagnostics={
                        "requested_block_ids": parsed.block_ids,
                        "result_block_ids": [block.block_id for block in result.blocks],
                        "result_count": result.total,
                    },
                )
            if tool_name == "scan_term":
                parsed = ScanTermArgs.model_validate(arguments)
                result = self._reader.scan_term(parsed.term, parsed.doc_ids)
                return ToolExecution(
                    payload=_validated_payload(result),
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
    return _validated_payload(
        RetrievalToolErrorPayload(
            error=RetrievalErrorPayload(
                code=code,
                message=message,
                retryable=retryable,
            )
        )
    )


def _validated_payload(model: BaseModel) -> dict[str, Any]:
    """把已构造的 adapter payload 冻结为经过 Pydantic 校验的 JSON 对象。"""

    return model.model_dump(mode="json")


def _output_schema(success_model: type[BaseModel]) -> dict[str, Any]:
    """生成成功 payload 与公共错误 payload 的合并输出 schema。"""

    success_schema = success_model.model_json_schema()
    error_schema = RetrievalToolErrorPayload.model_json_schema()
    definitions = {
        **error_schema.pop("$defs", {}),
        **success_schema.pop("$defs", {}),
        "RetrievalToolErrorPayload": error_schema,
    }
    schema = {
        "$defs": definitions,
        "anyOf": [
            success_schema,
            {"$ref": "#/$defs/RetrievalToolErrorPayload"},
        ],
    }
    return _drop_schema_metadata(schema)


def _drop_schema_metadata(value: Any) -> Any:
    """移除不影响字段契约的标题和说明，控制系统提示词大小。"""

    if isinstance(value, dict):
        return {
            key: _drop_schema_metadata(item)
            for key, item in value.items()
            if key not in {"description", "title"}
        }
    if isinstance(value, list):
        return [_drop_schema_metadata(item) for item in value]
    return value
