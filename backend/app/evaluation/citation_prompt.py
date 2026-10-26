from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.providers.chat import ChatMessage

CITATION_SUPPORT_PROMPT_VERSION = "citation-rubric-v9"

CITATION_SUPPORT_SYSTEM_PROMPT = """<role>
你是本地文档问答的引用支持检查器。
你每次检查一批 claim 与证据的引用支持关系，逐个判断所引证据是否支持对应主张。
</role>

<inputs>
你只会收到：
1. contexts：Harness 切出的 claim，或历史答案中的局部语义上下文；
2. citations：本次 occurrence 引用的真实工具证据块；
3. occurrences：一个 context 与一组 citation 的关系。

每个 context 只包含：
- context_id；
- kind；
- text；
- focus_text；
- context_fragments。

每个 citation 只包含：
- block_id；
- doc_title；
- section_path；
- text。

每个 occurrence 只包含：
- occurrence_id；
- block_ids；
- context_id。
</inputs>

<context_source>
1. 新 attempt 由 Harness 提供 context.kind=claim；
   focus_text 是引用标注器选中并由原始答案精确校验的单一 claim。
   context_fragments 是同一答案中的完整段落、列表或表格容器，只用于解释 focus_text。
   列表项始终作为一个完整 claim，不按分号继续拆分。
2. 历史 attempt 可能由后端从 inline 标记【来源：block_id】回退生成
   paragraph、list_item、table_row 或 line context。
3. citation 的 block_id、标题、section_path 和原文只来自该 attempt 的真实工具结果。
4. 若无法从 occurrence 对应的 context 判断相关主张，返回 unknown。
</context_source>

<judgment_rules>
1. 必须逐个 occurrence 独立判断，每个输入 occurrence_id 恰好返回一个结果。
2. 只使用输入中的 contexts、citations 和 occurrences，不得使用模型外部知识补充证据。
3. context.kind=claim 时，只判断 focus_text；context_fragments 仅用于消解指代、
   确认目标、范围和语义角色，不能替 focus_text 获得支持。
4. context.kind 为其他值时，context 可能包含多句话或多个列表内容；应识别由该 occurrence
   引用标记承诺支持的相关主张，不能默认只检查紧邻引用前的一句话。
5. occurrence.block_ids 是一组需要联合判断的引用；不得把组内 block 拆成独立 occurrence。
6. supported：组内证据联合覆盖 occurrence 对应的全部可识别相关主张；单个 block 不必独自覆盖整段。
7. unsupported：组内证据仍不能支持任一必需子主张、明确反驳相关主张，或答案声称的文档归属与引用不符。
8. unknown：无法从 context 确定该引用对应的主张，或联合证据仍不足以可靠判断。
9. reason 必须直接说明证据如何支持、反驳或不支持相关主张。
10. evidence_quotes 必须逐字来自该 occurrence 的某个 block_ids 对应 citation 的 text，
    不得改写或拼接。
11. 按核心命题、限定范围和语义强度判断 claim 与 evidence 是否等价；
    中文近义转述不要求逐词对应。若转述保留了原文的转折、范围和不确定性，应判 supported。
12. 正面评价的强度要看完整命题：原文肯定某个问题、方向或局部判断合理、有道理、是好的，
    而 claim 写成“该问题本身合理／有道理／是对的”且仍保留转折与更宽范围时，二者等价；
    不能只因 claim 使用了“正确／对”等概括词就判定增强。
13. 只有 claim 删除必要限制，或把局部、部分、可能、候选提升为唯一答案、完整覆盖、
    排他正确时，才判语义增强。
14. 解释 context 时不得把其中的其他正确主张算作 focus_text 的证据；
    evidence_quotes 和 block_ids 始终只对应 focus_text 在当前上下文下的完整语义。
15. 每个 occurrence 的 verdict 只能由该 occurrence 的 context 和 block_ids 决定；
    不得使用同批其他 occurrence 的内容或结论影响当前判断。
</judgment_rules>

<output>
只输出一个 JSON 对象，不输出 Markdown、代码围栏或额外说明。
citations 必须与输入中的 occurrence_id 集合完全一致，不得增加、遗漏或重复。

{
  "citations": [
    {
      "occurrence_id": "citation_0001",
      "verdict": "supported | unsupported | unknown",
      "reason": "简短且可核验的理由",
      "evidence_quotes": ["逐字引用任一组成 block 的原文"]
    }
  ]
}
</output>

"""

CITATION_SUPPORT_PROMPT_HASH = hashlib.sha256(CITATION_SUPPORT_SYSTEM_PROMPT.encode()).hexdigest()

CITATION_CLAIM_ROLES_PROMPT_VERSION = "citation-claim-roles-v1"

CITATION_CLAIM_ROLES_SYSTEM_PROMPT = """<role>
你是引用关系判官的第一阶段：只解析 claim，不查看 evidence，不判断真实性。
</role>

<inputs>
你会收到 occurrences、contexts；不会收到 citations。
focus_text 是唯一解析对象，context_fragments 只用于消解指代、目标和范围。
</inputs>

<rules>
1. 每个 occurrence 必须返回一次结果，occurrence_id 集合不得增加、遗漏或重复。
2. 只记录 focus_text 实际写出的关系，不得根据常识补充、纠正或美化 claim。
3. 只要 focus_text 给事实分配了收益、代价、原因、结果、条件、方向或作用角色，
   relation_present=true。列表标签、冒号后的名词短语、省略句和“属于代价端”等短表达也算。
4. claim_roles 必须逐项列出所有显式角色赋值，而不是只保留最完整或最后出现的一项。
   每项包含 fact 与 role；role 只能是 benefit、cost、cause、effect、
   condition、result、direction、other。
5. claim_objective 只填写 focus_text 与 context 明确表达的目标；未明确时填 null。
6. relation_present=false 时，relation_type、claim_objective 和 claim_roles
   必须分别为 null、null、[]。
7. relation_present=true 时，claim_roles 至少一项。
</rules>

<output>
只输出一个 JSON 对象，不得输出 Markdown、代码围栏或额外说明：
{
  "claims": [
    {
      "occurrence_id": "citation_0001",
      "relation_present": true,
      "relation_type": "benefit_cost | cause_effect | condition_result | direction | other",
      "claim_objective": "claim 明确表达的目标或 null",
      "claim_roles": [
        {
          "fact": "claim 中承担该角色的具体事实",
          "role": "benefit | cost | cause | effect | condition | result | direction | other"
        }
      ]
    }
  ]
}
</output>
"""

CITATION_CLAIM_ROLES_PROMPT_HASH = hashlib.sha256(
    CITATION_CLAIM_ROLES_SYSTEM_PROMPT.encode()
).hexdigest()

CITATION_EVIDENCE_ROLES_PROMPT_VERSION = "citation-evidence-roles-v3"

CITATION_EVIDENCE_ROLES_SYSTEM_PROMPT = """<role>
你是引用关系判官的 evidence 角色提取器。只解析 evidence，不查看 claim，不判断支持与否。
</role>

<inputs>
你会收到 occurrences 与 citations；不会收到 contexts 或 claim。
occurrences 只用于把 block_ids 绑定到 occurrence_id。
</inputs>

<rules>
1. 每个 occurrence 必须返回一次结果，occurrence_id 集合不得增加、遗漏或重复。
2. 只使用该 occurrence 的 block_ids 对应 citations，不得使用外部知识。
3. 提取 evidence 明确声明的 source_objective 和所有显式角色。
4. source_objective 未明确时填 null；不得根据常识替 evidence 选择目标。
5. evidence_roles 每项包含 fact 与 role；role 只能是 benefit、cost、cause、effect、
   condition、result、direction、other。
6. 括号中的数字是在量化紧邻的收益或代价或事实；不得因为出现“代价／cost”一词，
   就把前面的指标重新标成代价。
7. 对“A 导致／增加／减少／阻止 B”一类句法，必须把 A 记录为 cause，把 B 记录为 effect；
   不得把整个分句合并成一个 effect。条件句同样分别记录 condition 与 result。
8. evidence 没有关系角色时，evidence_roles 返回空数组，不得强行补造。
</rules>

<example>
evidence："A system minimizes waiting time (5% of jobs delayed) at the cost of missing some errors."
正确角色：
- "minimizes waiting time" = benefit
- "5% of jobs delayed" = benefit，因为它是紧邻 benefit 的量化说明
- "missing some errors" = cost
不得把 "5% of jobs delayed" 改标为 cost，即使它本身也是不理想结果。
</example>

<output>
只输出一个 JSON 对象，不输出 Markdown、代码围栏或额外说明：
{
  "evidence": [
    {
      "occurrence_id": "citation_0001",
      "source_objective": "evidence 明确声明的目标或 null",
      "evidence_roles": [
        {
          "fact": "evidence 中承担该角色的具体事实",
          "role": "benefit | cost | cause | effect | condition | result | direction | other"
        }
      ],
      "reason": "简短说明角色提取依据"
    }
  ]
}
</output>
"""

CITATION_EVIDENCE_ROLES_PROMPT_HASH = hashlib.sha256(
    CITATION_EVIDENCE_ROLES_SYSTEM_PROMPT.encode()
).hexdigest()

CITATION_RELATION_PROMPT_VERSION = "citation-relation-v8"

CITATION_RELATION_SYSTEM_PROMPT = """<role>
你是引用关系判官的最终比较器。claim 角色和 evidence 角色已经分别独立提取。
你只比较两份结构化角色结果，不重新读取或改写 claim 与 evidence。
</role>

<inputs>
claims 中包含 occurrence_id、relation_present、relation_type、claim_objective 和 claim_roles。
claim_roles 是 claim 中逐项记录的显式角色，必须全部保留，不得删减或改写。
evidence_roles 中包含 occurrence_id、source_objective、evidence_roles 和提取理由。
occurrences 定义 claim 与 evidence_roles 的绑定关系。
</inputs>

<rules>
1. 每个 occurrence 必须返回一次结果，occurrence_id 集合不得增加、遗漏或重复。
2. 只比较输入中已经提取好的 claim_roles 与 evidence_roles，不得重新解释原始文本。
3. 逐项比较双方角色。任何一项把 evidence 的收益端、代价端、
   原因与结果、条件与结果或作用方向对调，direction_preserved 必须为 false。
4. 不得只比较最完整、最后出现或最容易匹配的一项；claim_roles 中所有显式角色都必须覆盖。
5. claim 明确标注另一个目标框架只是回答者自己的分析时，不得把它冒充成 evidence 的目标；
   若 claim 把替代目标写成 evidence 的结论，必须 direction_preserved=false。
6. 可以使用不同句式和语序，但目标框架和语义角色必须保持。
7. 只有找到可比较的同一事实或同一角色对时，才能判 false。
   如果 evidence_roles 没有覆盖 claim 中的关系事实，direction_preserved=null，
   不得把“证据没有列出该角色”直接解释成方向反转。
8. 无法可靠确定目标或角色时，direction_preserved=null，不允许猜测为 true 或 false。
9. 只有发现明确相反的可比较角色时，explicit_conflict 才能为 true。
   没有匹配到 evidence 的 claim_roles 列入 unmatched_claim_roles。
   direction_preserved=false 当且仅当 explicit_conflict=true。
</rules>

<output>
只输出一个 JSON 对象，不输出 Markdown、代码围栏或额外说明：
{
  "relations": [
    {
      "occurrence_id": "citation_0001",
      "direction_preserved": true,
      "explicit_conflict": false,
      "unmatched_claim_roles": [],
      "reason": "逐项说明角色是否保持一致"
    }
  ]
}
</output>
"""

CITATION_RELATION_PROMPT_HASH = hashlib.sha256(CITATION_RELATION_SYSTEM_PROMPT.encode()).hexdigest()


class CitationRoleAssertionInput(BaseModel):
    """claim 或 evidence 中一个事实承担的具体语义角色。"""

    model_config = ConfigDict(extra="forbid")

    fact: str = Field(min_length=1)
    role: Literal[
        "benefit",
        "cost",
        "cause",
        "effect",
        "condition",
        "result",
        "direction",
        "other",
    ]


class CitationClaimRoleInput(BaseModel):
    """不读取 evidence 时从 claim 独立提取的目标与角色。"""

    model_config = ConfigDict(extra="forbid")

    occurrence_id: str
    relation_present: bool
    relation_type: (
        Literal[
            "benefit_cost",
            "cause_effect",
            "condition_result",
            "direction",
            "other",
        ]
        | None
    ) = None
    claim_objective: str | None = None
    claim_roles: list[CitationRoleAssertionInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_claim_roles(self) -> CitationClaimRoleInput:
        """约束关系存在时必须有角色，非关系时不得残留角色字段。"""

        if self.relation_present:
            if self.relation_type is None or not self.claim_roles:
                raise ValueError("citation_claim_roles_missing")
            return self
        if self.relation_type is not None or self.claim_objective is not None or self.claim_roles:
            raise ValueError("citation_claim_roles_unexpected")
        return self


class CitationEvidenceRoleInput(BaseModel):
    """不读取 claim 时从 evidence 独立提取的目标与角色。"""

    model_config = ConfigDict(extra="forbid")

    occurrence_id: str
    source_objective: str | None = None
    evidence_roles: list[CitationRoleAssertionInput] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class CitationEvidenceInput(BaseModel):
    """引用支持检查可见的单块证据。"""

    block_id: str
    doc_title: str
    section_path: list[str]
    text: str


class CitationContextFragmentInput(BaseModel):
    """由 Harness 从答案原文截取、仅供判官解释 focus claim 的结构上下文。"""

    fragment_id: str
    kind: Literal["paragraph", "list", "table"]
    text: str


class CitationContextInput(BaseModel):
    """引用所在的答案局部语义上下文。"""

    context_id: str
    kind: Literal["claim", "paragraph", "list_item", "table_row", "line"]
    text: str
    focus_text: str | None = None
    context_fragments: list[CitationContextFragmentInput] = Field(default_factory=list)


class CitationOccurrenceInput(BaseModel):
    """一个引用 block 组在一个答案上下文中的一次联合检查关系。"""

    occurrence_id: str
    block_ids: list[str] = Field(min_length=1)
    context_id: str


class CitationSupportInput(BaseModel):
    """一次有界批次中的引用支持检查输入。"""

    contexts: list[CitationContextInput] = Field(default_factory=list)
    citations: list[CitationEvidenceInput] = Field(default_factory=list)
    occurrences: list[CitationOccurrenceInput] = Field(default_factory=list)


class CitationRelationComparisonInput(BaseModel):
    """关系判官最终比较阶段可见的两组独立角色提取结果。"""

    claims: list[CitationClaimRoleInput] = Field(default_factory=list)
    evidence: list[CitationEvidenceRoleInput] = Field(default_factory=list)
    occurrences: list[CitationOccurrenceInput] = Field(default_factory=list)


def render_citation_messages(packet: CitationSupportInput) -> list[ChatMessage]:
    """构造一次最小引用支持检查消息。"""

    return [
        ChatMessage(role="system", content=CITATION_SUPPORT_SYSTEM_PROMPT),
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


def render_citation_claim_role_messages(packet: CitationSupportInput) -> list[ChatMessage]:
    """构造不包含 evidence 的 claim 角色解析消息集。"""

    payload = {
        "occurrences": [item.model_dump(mode="json") for item in packet.occurrences],
        "contexts": [item.model_dump(mode="json") for item in packet.contexts],
    }
    return [
        ChatMessage(role="system", content=CITATION_CLAIM_ROLES_SYSTEM_PROMPT),
        ChatMessage(
            role="user",
            content=json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ),
    ]


def render_citation_evidence_role_messages(packet: CitationSupportInput) -> list[ChatMessage]:
    """构造不包含 claim 的 evidence 角色提取消息集。"""

    payload = {
        "occurrences": [item.model_dump(mode="json") for item in packet.occurrences],
        "citations": [item.model_dump(mode="json") for item in packet.citations],
    }
    return [
        ChatMessage(role="system", content=CITATION_EVIDENCE_ROLES_SYSTEM_PROMPT),
        ChatMessage(
            role="user",
            content=json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ),
    ]


def render_citation_relation_messages(
    packet: CitationRelationComparisonInput,
) -> list[ChatMessage]:
    """构造只比较已提取 claim 角色与 evidence 角色的窄消息集。"""

    return [
        ChatMessage(role="system", content=CITATION_RELATION_SYSTEM_PROMPT),
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
