from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.token_counter import ObservationTokenCounter
from app.providers.chat import ChatMessage, ChatProvider, ChatProviderError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.agent.citations import CitationSpan

CITATION_SOURCE_META_MAX_OUTPUT_TOKENS = 8_192
MAX_CITATION_SOURCE_META_ATTEMPTS = 2
CITATION_SOURCE_META_RULE_VERSION = "citation-source-meta-rules-v4"

CITATION_SOURCE_META_SYSTEM_PROMPT = """<role>
你是RAG问答中答案引用标注流程中的来源元描述门禁。你不选择证据，也不判断引用是否成立。
你的职责只是高置信度地找出不能依靠普通内容 block 直接支持的来源元描述。
无法确定时必须放行到正文引用标注。
</role>

<input>
你会收到 question、基于检索增强生成的完整 answer，以及已通过结构过滤的 spans。
span.text 是唯一判断对象；answer 只用于解释指代和范围，不是判定对象。
</input>

<output>
只输出一个 JSON 对象，不得输出 Markdown、代码围栏或额外说明：
{"assessments":[{"span_id":"span_0001","has_content_claim":false,"source_relation":"none","answer_relation":"none","reason":""}]}
</output>

<assessment_fields>
has_content_claim：span 本身是否包含一个可由普通内容 block 支持、脱离当前回答仍可核验的内容命题。
- 外部事实、观点、方法、分类、原则、比较、来源之间的关系、建议或结论都算内容命题。
- 即使命题带有“文档指出”“该段提到”“本文认为”等来源归因外壳，只要 X 是可引用内容，
  都应判断为 true。
- 执行删除检验：删去“文档指出”“以上来自”“补充说明”“在结尾表格中”等来源或回答外壳。
  删除后仍剩下事实、定义、分类、关系、数值或结论时，has_content_claim=true。
- 来源覆盖数量、某个说法是否存在、来源章节位置、当前 answer 的完成状态或生成过程，
  不算普通内容命题。
- 若 span 的主结论是“来源没有建立某关系/该说法只是答案补充”，
  其中为论证而复述的来源内容不单独把主结论改判为内容命题。

source_relation：
- none：没有把来源覆盖或组织方式本身作为核心命题；
- coverage_stat：命题是对全部或部分来源的出现次数、覆盖数量或覆盖程度作统计；
- coverage_not_appear：命题是某个说法、主题或对象在来源中不存在、未出现或未被覆盖；
- location：核心命题是给定内容在来源中的章节、段落、前后位置或阅读顺序。
  location 只适用于删除位置信息后不再剩下独立内容命题的 span。
  说明某章节或表格“包含哪些模式、类别、代价、数值或边界”属于内容摘要；
  只有“见下表”“位于后文”“出现在某段”这类纯指针才是 location。

answer_relation：
- none：没有把当前 answer 自身作为核心命题；
- answer_process：命题只涉及当前 answer 的完成状态、证据是否充分、如何生成或归纳，
  或明确指导当前 answer 中某句话应如何标注来源与推断。
  普通开场、过渡、清单引导、回答提纲、能力边界、缺少用户输入和请求用户补充信息
  都不属于 answer_process。
  来源归属外壳后跟术语定义、机制或数据说明时，仍属于内容命题，不属于 answer_process。
</assessment_fields>

<rules>
1. 必须为每个输入 span_id 返回且只返回一次结果，不得增加、遗漏或重复。
2. 先判断 span 的核心命题；不能只按“文档”“本文”“回答”等表面词语分类。
3. 来源覆盖统计或来源缺失是最高优先级：只要核心命题是它们，
   就标记 coverage_stat 或 coverage_not_appear。
   当主结论是来源未建立某关系时，列举来源确实提到了 A、B 等支持性事实仍属于
   coverage_not_appear，不能因为这些事实而判成普通内容。
4. 除规则 3 外，只要 span 含有一个实质内容命题，has_content_claim=true；
   source_relation 和 answer_relation 的附带外壳不得把该 span 变成来源元描述。
5. 同时包含“位置/回答组织”和实质内容时，has_content_claim=true。
6. “文档指出 X”“原文称 X”中的 X 是实质内容时，
   has_content_claim=true、source_relation=none。
7. “字面核对结果：”“下面分三点说明：”等纯标签、提纲或过渡语可以
   has_content_claim=false，但 answer_relation=none，让它们默认进入正文标注。
   如果主句在指导某句话应标成原文引用、推论或模型补充，从句给出的替代原文只是
   标注依据，仍应标记 answer_process。
8. “我没有收到文档”“可以告诉我文档名吗”“我能帮你梳理以下内容”等能力边界、
   缺少输入或追问内容属于普通回答正文，answer_relation=none。
9. 不能确定时按正文处理：has_content_claim=true、source_relation=none、
   answer_relation=none。
</rules>
"""


@dataclass(frozen=True, slots=True)
class CitationSourceMetaPrompt:
    """一次来源元分类实验使用的不可变提示词版本。"""

    version: str
    system_prompt: str

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("source meta prompt version must not be empty")
        if not self.system_prompt.strip():
            raise ValueError("source meta prompt must not be empty")

    @property
    def content_hash(self) -> str:
        """返回系统提示词内容的稳定审计 hash。"""

        return hashlib.sha256(self.system_prompt.encode("utf-8")).hexdigest()


DEFAULT_CITATION_SOURCE_META_PROMPT = CitationSourceMetaPrompt(
    version="citation-source-meta-v8",
    system_prompt=CITATION_SOURCE_META_SYSTEM_PROMPT,
)
CITATION_SOURCE_META_PROMPT_VERSION = DEFAULT_CITATION_SOURCE_META_PROMPT.version
CITATION_SOURCE_META_PROMPT_HASH = DEFAULT_CITATION_SOURCE_META_PROMPT.content_hash

SourceMetaCategory = Literal[
    "source_coverage_stat",
    "source_coverage_not_appear",
    "source_structure",
    "answer_provenance",
]


class CitationSourceMetaInput(BaseModel):
    """来源元分类器收到的完整输入契约。"""

    model_config = ConfigDict(extra="forbid")

    question: str
    answer: str
    spans: list[dict[str, str]]


SourceRelation = Literal[
    "none",
    "coverage_stat",
    "coverage_not_appear",
    "location",
]
AnswerRelation = Literal[
    "none",
    "answer_process",
]


class CitationSourceMetaAssessment(BaseModel):
    """模型给出的三个正交语义信号。"""

    model_config = ConfigDict(extra="forbid")

    span_id: str
    has_content_claim: bool
    source_relation: SourceRelation
    answer_relation: AnswerRelation
    reason: str = ""


class CitationSourceMetaDecision(BaseModel):
    """由 Harness 根据语义信号推导出的门禁决定。"""

    model_config = ConfigDict(extra="forbid")

    span_id: str
    source_meta: bool
    category: SourceMetaCategory | Literal[""] = ""
    reason: str = ""


class CitationSourceMetaArtifact(BaseModel):
    """随引用标注 artifact 持久化的来源门禁版本、决定与调用摘要。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["complete", "pending", "not_applicable"] = "not_applicable"
    rule_version: str = CITATION_SOURCE_META_RULE_VERSION
    prompt_version: str = ""
    prompt_hash: str = ""
    model_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    decisions: list[CitationSourceMetaDecision] = Field(default_factory=list)
    excluded_span_count: int = Field(default=0, ge=0)
    fallback_to_all_spans: bool = False
    error_code: str | None = None
    error_message: str | None = None


class CitationSourceMetaOutput(BaseModel):
    """模型对一批 span 的完整语义评估。"""

    model_config = ConfigDict(extra="forbid")

    assessments: list[CitationSourceMetaAssessment] = Field(default_factory=list)


class CitationSourceMetaError(ValueError):
    """来源元分类协议错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CitationSourceMetaOutcome:
    """来源元分类的协议结果和调用审计。"""

    status: Literal["complete", "pending"]
    decisions: tuple[CitationSourceMetaDecision, ...]
    model_calls: int
    input_tokens: int
    output_tokens: int
    prompt_version: str
    prompt_hash: str
    messages: tuple[ChatMessage, ...]
    raw_outputs: tuple[str, ...]
    error_code: str | None = None
    error_message: str | None = None

    def to_artifact(self) -> CitationSourceMetaArtifact:
        """把协议调用结果压缩成适合 trace 与评测消费的持久化摘要。"""

        return CitationSourceMetaArtifact(
            status=self.status,
            prompt_version=self.prompt_version,
            prompt_hash=self.prompt_hash,
            model_calls=self.model_calls,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            decisions=list(self.decisions),
            excluded_span_count=sum(decision.source_meta for decision in self.decisions),
            fallback_to_all_spans=False,
            error_code=self.error_code,
            error_message=self.error_message,
        )


def render_citation_source_meta_messages(
    *,
    question: str,
    answer: str,
    spans: Sequence[CitationSpan],
    prompt: CitationSourceMetaPrompt = DEFAULT_CITATION_SOURCE_META_PROMPT,
) -> list[ChatMessage]:
    """构造来源元分类器的最小消息集。"""

    packet = CitationSourceMetaInput(
        question=question,
        answer=answer,
        spans=[
            {
                "span_id": span.span_id,
                "structure_role": span.structure_role,
                "text": span.text,
            }
            for span in spans
        ],
    )
    return [
        ChatMessage(role="system", content=prompt.system_prompt),
        ChatMessage(
            role="user",
            content=json.dumps(
                packet.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    ]


def parse_citation_source_meta_output(
    raw_output: str,
    *,
    spans: Sequence[CitationSpan],
) -> tuple[CitationSourceMetaDecision, ...]:
    """校验模型语义评估，并由 Harness 按固定优先级推导门禁决定。"""

    try:
        parsed = CitationSourceMetaOutput.model_validate_json(raw_output)
    except ValidationError as error:
        raise CitationSourceMetaError(
            "citation_source_meta_output_invalid",
            "source meta output does not match its schema",
        ) from error

    assessments_by_id = {item.span_id: item for item in parsed.assessments}
    expected_ids = {span.span_id for span in spans}
    if len(assessments_by_id) != len(parsed.assessments) or set(assessments_by_id) != expected_ids:
        raise CitationSourceMetaError(
            "citation_source_meta_span_set_invalid",
            "source meta classifier must classify every input span exactly once",
        )
    return tuple(
        _derive_citation_source_meta_decision(
            assessments_by_id[span.span_id],
            text=span.text,
        )
        for span in spans
    )


def _derive_citation_source_meta_decision(
    assessment: CitationSourceMetaAssessment,
    *,
    text: str,
) -> CitationSourceMetaDecision:
    """按固定优先级把语义信号合成为高精度门禁决定。"""

    category: SourceMetaCategory | Literal[""] = ""
    if assessment.source_relation == "coverage_stat":
        category = "source_coverage_stat"
    elif assessment.source_relation == "coverage_not_appear":
        category = "source_coverage_not_appear"
    elif assessment.has_content_claim:
        # 正文优先是门禁的核心不变量。位置、组织或回答过程外壳不能覆盖已有的内容命题，
        # 否则会绕过后续引用标注与判官，形成静默漏引。
        category = ""
    elif assessment.answer_relation == "answer_process" and _is_high_confidence_answer_process(
        text
    ):
        category = "answer_provenance"
    elif assessment.source_relation == "location" and _is_pure_source_location(text):
        category = "source_structure"
    return CitationSourceMetaDecision(
        span_id=assessment.span_id,
        source_meta=bool(category),
        category=category,
        reason=assessment.reason,
    )


_SOURCE_LOCATION_CUE_PATTERN = re.compile(
    r"出现在|位于|见(?:上文|下文|后文|前文|第[一二三四五六七八九十\d]+章)|"
    r"参(?:见|考)|上文|下文|前文|后文|开篇|结尾|第[一二三四五六七八九十\d]+章|"
    r"章节|段落|目录|阅读顺序"
)
_CONTENT_BEARING_PATTERN = re.compile(
    r"包含|给出|分为|分成|对应|提到|说明|指出|强调|解释|总结|比较|列出|介绍|"
    r"讨论|描述|显示|支持|限制|控制|风险|代价|边界|方法|机制|数据|模式|类别|"
    r"数值|事实|观点|原则|方案|建议|结论"
)
_ANSWER_PROCESS_PATTERN = re.compile(
    r"本次回答|回答(?:未|已经|仅)|内容(?:未|仅)|证据(?:是否|不足|充分|范围|仅)|"
    r"模型(?:连续输出|输出|层面)?|结构化协议|无法生成|如何生成|"
    r"标注为|原文引用|基于文档的推论|来源之外|模型补充|"
    r"以上内容均来自|内容均来自|另行核对"
)


def _is_pure_source_location(text: str) -> bool:
    """只放行不含实质内容命题的明确来源位置指针。"""

    return (
        _SOURCE_LOCATION_CUE_PATTERN.search(text) is not None
        and _CONTENT_BEARING_PATTERN.search(text) is None
    )


def _is_high_confidence_answer_process(text: str) -> bool:
    """只排除带明确回答过程信号的窄类别，普通提纲仍进入正文。"""

    return _ANSWER_PROCESS_PATTERN.search(text) is not None


class CitationSourceMetaClassifier:
    """使用独立模型调用执行来源元分类，并保留完整协议审计。"""

    def __init__(
        self,
        provider: ChatProvider,
        *,
        token_counter: ObservationTokenCounter,
        prompt: CitationSourceMetaPrompt = DEFAULT_CITATION_SOURCE_META_PROMPT,
    ) -> None:
        self._provider = provider
        self._token_counter = token_counter
        self._prompt = prompt

    async def classify(
        self,
        *,
        question: str,
        answer: str,
        spans: Sequence[CitationSpan],
        spent_input_tokens: int,
        spent_output_tokens: int,
        max_context_tokens: int,
        max_request_tokens: int,
    ) -> CitationSourceMetaOutcome:
        """分类全部输入 span；协议失败会重试一次并保留调用审计。"""

        messages = tuple(
            render_citation_source_meta_messages(
                question=question,
                answer=answer,
                spans=spans,
                prompt=self._prompt,
            )
        )
        estimated_input_tokens = sum(
            self._token_counter.count(message.content) for message in messages
        )
        if estimated_input_tokens > max_context_tokens:
            return self._pending(
                messages=messages,
                code="citation_source_meta_context_exceeded",
                message=(
                    "source meta input exceeds max_context_tokens: "
                    f"{estimated_input_tokens}>{max_context_tokens}"
                ),
            )

        model_calls = 0
        input_tokens = 0
        output_tokens = 0
        raw_outputs: list[str] = []
        last_code = "citation_source_meta_failed"
        last_message = "source meta classification failed"
        for attempt in range(1, MAX_CITATION_SOURCE_META_ATTEMPTS + 1):
            if (
                spent_input_tokens
                + spent_output_tokens
                + input_tokens
                + output_tokens
                + estimated_input_tokens
                + CITATION_SOURCE_META_MAX_OUTPUT_TOKENS
                > max_request_tokens
            ):
                return self._pending(
                    messages=messages,
                    code="citation_source_meta_request_budget_exceeded",
                    message="source meta classification exceeds the request budget",
                    model_calls=model_calls,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    raw_outputs=tuple(raw_outputs),
                )
            try:
                result = await self._provider.complete(
                    messages,
                    max_output_tokens=CITATION_SOURCE_META_MAX_OUTPUT_TOKENS,
                )
            except ChatProviderError as error:
                model_calls += 1
                input_tokens += estimated_input_tokens
                last_code = "citation_source_meta_model_failed"
                last_message = error.message
                if not error.retryable or attempt == MAX_CITATION_SOURCE_META_ATTEMPTS:
                    return self._pending(
                        messages=messages,
                        code=last_code,
                        message=last_message,
                        model_calls=model_calls,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        raw_outputs=tuple(raw_outputs),
                    )
                continue

            model_calls += 1
            input_tokens += result.input_tokens
            output_tokens += result.output_tokens
            raw_outputs.append(result.content)
            try:
                if result.finish_reason == "length":
                    raise CitationSourceMetaError(
                        "citation_source_meta_output_truncated",
                        "source meta output reached the token limit",
                    )
                decisions = parse_citation_source_meta_output(
                    result.content,
                    spans=spans,
                )
            except CitationSourceMetaError as error:
                last_code = error.code
                last_message = str(error)
                if attempt == MAX_CITATION_SOURCE_META_ATTEMPTS:
                    return self._pending(
                        messages=messages,
                        code=last_code,
                        message=last_message,
                        model_calls=model_calls,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        raw_outputs=tuple(raw_outputs),
                    )
                continue
            return CitationSourceMetaOutcome(
                status="complete",
                decisions=decisions,
                model_calls=model_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                messages=messages,
                raw_outputs=tuple(raw_outputs),
                prompt_version=self._prompt.version,
                prompt_hash=self._prompt.content_hash,
            )
        raise RuntimeError("source meta classifier exhausted without a result")

    def _pending(
        self,
        *,
        messages: tuple[ChatMessage, ...],
        code: str,
        message: str,
        model_calls: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        raw_outputs: tuple[str, ...] = (),
    ) -> CitationSourceMetaOutcome:
        """构造保留原始输出与调用审计的 pending 结果。"""

        return CitationSourceMetaOutcome(
            status="pending",
            decisions=(),
            model_calls=model_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            prompt_version=self._prompt.version,
            prompt_hash=self._prompt.content_hash,
            messages=messages,
            raw_outputs=raw_outputs,
            error_code=code,
            error_message=message,
        )
