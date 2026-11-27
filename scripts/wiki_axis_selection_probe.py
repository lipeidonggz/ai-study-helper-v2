"""验证问题能否稳定映射到当前关系观察轴候选集。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import keyring

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.dual_entry_index.induction import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_ID,
    DeepSeekJsonClient,
    InductionFailure,
)

PROMPT_VERSION = "stage51-axis-selection-v7-current-21-with-connectivity-labels"
OUTPUT_PATH = BACKEND_ROOT / ".runtime" / "axis_selection_latest" / "results.json"
CREDENTIAL_SERVICE_NAME = "ai-study-helper"
CHAT_API_KEY_USERNAME = "chat-api-key"


@dataclass(frozen=True, slots=True)
class Axis:
    """交给模型选择的一条候选观察轴。"""

    axis_id: str
    name: str
    definition: str


@dataclass(frozen=True, slots=True)
class Question:
    """一个轴选择探针问题及其人工预期。"""

    question_id: str
    text: str
    expected_axis_ids: tuple[str, ...]


AXES = (
    Axis("C01", "类型归类", "一个知识对象是否被原文归入某个类型、类别或上位概念？"),
    Axis("C02", "组成与部分", "一个整体由哪些部分、成员、材料或子事件构成？"),
    Axis(
        "C03",
        "用途与目的",
        "一个对象、机制或行动被用于什么用途，或服务于什么目标、结果？",
    ),
    Axis("C04", "能力与可作用性", "一个对象通常能够执行什么行动，或能够承受什么行动？"),
    Axis(
        "C05",
        "位置与定位关系",
        "一个对象、主体、活动或事件位于何处、存放于哪里，或与哪个位置或对象邻近？",
    ),
    Axis(
        "C06",
        "作用与影响",
        "一个对象、事件或条件对另一个事件、状态、能力或倾向产生什么方向的作用？",
    ),
    Axis("C07", "时间与事件组织", "事件或子事件以什么先后、起止位置或时间顺序组织？"),
    Axis(
        "C08",
        "条件与依赖",
        "一个目标成立、发生或被接受前必须满足什么条件，或依赖什么对象、状态或过程？",
    ),
    Axis(
        "C09",
        "存在生成与终结",
        "哪个主体、活动或过程使一个对象、事件、活动或状态开始存在、开始发生、不再存在或终止？",
    ),
    Axis(
        "C10",
        "象征表示",
        "一个符号、颜色、图形、标记、动作或表达在特定语境中象征什么对象、性质或含义？",
    ),
    Axis(
        "C11",
        "同义、相似与对立",
        "两个对象或表达在哪个相关方面是同义或等价、相近或相反？",
    ),
    Axis(
        "C12",
        "拥有与归属",
        "一个对象或资源由谁持有、归谁所有，或属于哪个主体、组织或范围？",
    ),
    Axis(
        "C13",
        "有意识主体的有向态度",
        "一个有意识主体对某个对象、行动、状态或知识主张持有什么欲求、回避、偏好、反感、支持或反对态度？",
    ),
    Axis(
        "C14",
        "词源关系",
        "一个词或词形是否与另一个词共享历史来源，或由另一个词历史派生？",
    ),
    Axis(
        "C15",
        "来源、去向与流转",
        "一个对象、内容、材料、数据、知识、主张或信息来自哪个上游对象、过程或主体，"
        "或经过复制、转发、引用、传递等关系流向哪个下游对象、过程或主体？",
    ),
    Axis(
        "C16",
        "使用与被使用",
        "哪个活动、过程或执行者实际利用了哪些对象、材料、工具、数据、方法或资源，反向又如何？",
    ),
    Axis(
        "C17",
        "转化与派生",
        "一个对象、状态或版本是否由另一个既有对象、状态或版本经过转化、更新、修订或构造而形成，或形成了什么结果？",
    ),
    Axis(
        "C18",
        "责任与承担",
        "哪个主体对一个对象、活动或结果承担、负有或被赋予责任和角色？",
    ),
    Axis(
        "C19",
        "代理与代表",
        "哪个主体作为代理人或代表，为哪个主体执行活动、作出表达或承担角色？",
    ),
    Axis(
        "C21",
        "许可、禁止与义务",
        "某个主体、规则或机制对某个行动或资产规定了什么规范状态：允许或授权、禁止，还是要求或义务？",
    ),
    Axis(
        "C22",
        "分离、连通与可达",
        "原文是否明确表达两个对象、区域或作用域彼此隔开、存在通路，或从一侧能够到达、进入另一侧？",
    ),
)


SECONDARY_LABELS: dict[str, tuple[tuple[str, str], ...]] = {
    "C01": (
        ("实例归类", "具体对象、事件、行动或状态被原文明确归入某个类型或类别。"),
        (
            "子类型归类",
            "一个类型、行动类型、事件类型或关系类型被原文明确归入上位类型。",
        ),
    ),
    "C02": (
        (
            "结构或内容部分",
            "一个对象是整体、系统、文本或过程的结构部件、模块、章节、片段或阶段。",
        ),
        ("集合成员", "一个对象被原文明确列为某个集合、组或容器的成员。"),
        ("构成材料", "一个对象被原文明确说明由某种材料构成或制成。"),
        ("子事件", "一个事件是另一个事件或过程的明确子事件、阶段或步骤。"),
    ),
    "C03": (
        (
            "功能用途",
            "对象、机制或行动被设计、配置、推荐或说明用于某种功能、用途或结果。",
        ),
        ("目标动机", "行动或过程是为了某个目标、结果或动机而发生。"),
    ),
    "C04": (
        ("主动能力", "对象能够执行、承担或实现某个行动或功能。"),
        ("被动可作用性", "对象能够承受、接受或被某个行动作用、处理或影响。"),
    ),
    "C05": (
        (
            "所在位置",
            "对象位于、处于或落入某个地点、路径、目录、数据库、容器或逻辑环境。",
        ),
        ("存放位置", "对象被存储、托管、保存或驻留在某个位置或容器中。"),
        ("邻近关系", "两个对象相邻、接近或处于同一局部环境。"),
    ),
    "C06": (
        ("导致", "一个对象、事件或条件使另一个事件、状态或结果发生或成立。"),
        ("使能／促成", "一个对象、事件或条件使目标成为可能或提高其实现机会。"),
        ("增强", "一个对象、事件或条件提高目标的强度、频率、能力或程度。"),
        ("阻止", "一个对象、事件或条件使目标不能发生、成立或继续。"),
        ("削弱／抑制", "一个对象、事件或条件降低目标的强度、频率、能力或程度。"),
        ("损害／破坏", "一个对象、事件或条件使目标受到损伤、破坏或可用性下降。"),
    ),
    "C07": (
        ("先后顺序", "一个事件在另一个事件之前或之后发生。"),
        ("同时关系", "两个事件同时发生或在时间上重叠。"),
        (
            "期间关系",
            "一个事件发生在另一个事件的持续区间内，或其时间范围被另一事件覆盖。",
        ),
        (
            "起止／首末",
            "一个事件被明确表达为另一事件的开始、结束、第一或最后事件、阶段或子事件。",
        ),
    ),
    "C08": (
        ("必要条件", "某条件不成立时，目标不能发生、成立或被接受。"),
        ("前置条件", "某条件必须在目标开始前完成、发生或成立。"),
        ("持续条件", "某条件必须在目标持续、运行或有效期间保持成立。"),
    ),
    "C09": (
        ("生成／创建", "实体、内容或抽象知识对象开始存在。"),
        ("启动／触发", "活动或过程开始发生。"),
        ("终结／销毁", "实体不再存在、不可用或被销毁。"),
        ("结束／停止", "活动或过程结束、停止或被终止。"),
    ),
    "C10": (
        (
            "象征表示",
            "符号、颜色、图形、标记、动作或表达在特定语境中象征、标示或承载对象、性质、状态、身份或含义。",
        ),
    ),
    "C11": (
        ("同义／等价", "两个对象或表达在同一相关方面具有相同或可交换的指称或语义。"),
        ("相似／近似", "两个对象或表达在明确方面接近但不等同。"),
        ("对立／相反", "两个对象或表达在明确方面方向相反、互斥或构成反义。"),
    ),
    "C12": (
        ("所有权", "对象或资源在法律、制度或明确权属语义上归某个主体所有。"),
        ("持有／控制", "主体实际持有、保管或控制对象或资源，但不必然拥有所有权。"),
        (
            "归属范围",
            "对象或资源被明确归入主体、组织、账户、项目、工作区或其他归属范围。",
        ),
    ),
    "C13": (
        ("欲求", "有意识主体希望、想要或主动追求某个对象、行动、状态或结果。"),
        ("回避", "有意识主体不希望、拒绝或主动避免某个对象、行动、状态或结果。"),
        ("偏好", "有意识主体在两个或多个选项之间明确更喜欢或优先选择某些选项。"),
        ("反感", "有意识主体明确不喜欢、厌恶或排斥某个对象、行动或状态。"),
        ("支持", "有意识主体明确赞同、支持、维护或认可某个知识主张、行动或方案。"),
        ("反对", "有意识主体明确不赞同、反对或抵制某个知识主张、行动或方案。"),
    ),
    "C14": (
        ("同源", "两个词或词形在语言历史中共享来源，但一方不必然由另一方派生。"),
        ("历史派生", "一个词或词形在历史语言演变中由另一个词或词形派生。"),
    ),
    "C15": (
        (
            "直接来源",
            "目标内容直接来自一个上游对象、过程或主体，中间没有原文给出的来源节点。",
        ),
        ("间接来源", "目标内容经过至少一个原文明确的中间对象追溯到更上游来源。"),
        (
            "原始／最终来源",
            "来源链根端被原文明确认定为内容的产生者、采集者、创作者或初始出处。",
        ),
        (
            "直接去向",
            "目标内容直接流向、交给、发送给或传递给一个下游对象、过程或主体。",
        ),
        ("间接去向", "目标内容经过至少一个原文明确的中间对象到达更下游对象。"),
        ("最终去向", "接收方被原文明确认定为最终接收者、传递终点或不再继续下游传递。"),
    ),
    "C16": (
        (
            "实际使用",
            "活动、过程或执行者实际利用、调用、采用或投入某个对象、材料、工具、数据、方法或资源。",
        ),
    ),
    "C17": (
        (
            "转化",
            "既有对象或状态经过明确变化形成结果，结果保留可识别的连续性或对应关系。",
        ),
        ("更新／修订", "结果是对象、内容或版本的更新、修订、替换版本或后继版本。"),
        ("构造／派生", "结果基于一个或多个既有对象、内容或结构构造、生成或派生形成。"),
    ),
    "C18": (
        ("责任归属", "主体对对象、内容、活动或结果承担、负有或被赋予明确责任。"),
        ("活动角色", "主体在活动或过程中承担一个原文明确的责任角色或参与角色。"),
    ),
    "C19": (
        ("代表行动", "代理主体代表被代表主体执行活动。"),
        ("代表表达", "代理主体代表被代表主体作出表达、签署、陈述或提交。"),
        ("代表角色", "代理主体代表被代表主体承担某个角色、席位或身份。"),
    ),
    "C21": (
        (
            "允许／授权",
            "规则、主体或机制明确允许某项行动，或授予执行行动、访问资产或作出选择的资格。",
        ),
        ("禁止", "规则、主体或机制明确禁止某项行动、访问或状态。"),
        (
            "义务／要求",
            "规则、主体或机制明确要求某个主体执行行动、交付对象或保持状态。",
        ),
    ),
    "C22": (
        (
            "分离／隔断",
            "原文明确表达两个对象、区域或作用域彼此分开，或一个整体被划分为互不直接连通的部分。",
        ),
        (
            "连通／连接",
            "原文明确表达两端通过路径、连接器、通道或网络相连，并形成可通行、可交换或可作用的结构或技术通路。",
        ),
        (
            "可达／可进入",
            "原文明确表达从一侧能够沿通路到达、进入或访问另一侧；关系具有方向，不要求反向成立。",
        ),
        (
            "阻断／不可达",
            "原文明确表达屏障、分隔机制、连接缺失或不可通行状态使一侧无法到达、进入或访问另一侧。",
        ),
    ),
}


QUESTIONS = (
    Question(
        "c13-003",
        "在当前知识库中，除了沙箱、虚拟机和出口控制，AI Agent 还有哪些隔离或控制边界？"
        "请分别说明各限制什么。",
        (),
    ),
    Question(
        "isolation",
        "AI Agent 中的隔离都有哪些？",
        (),
    ),
)


SYSTEM_PROMPT = """你是知识库问答系统的查询路由器。

输入包含：
1. 当前可选的候选关系观察轴；
2. 每个候选轴可筛选出的候选二级关系数据及其定义；
3. 一个用户问题。

你的任务是判断：为了完整回答该问题，主 Agent 必须从哪些观察轴检索关系证据。

判断规则：

1. 只根据候选轴本身说明的语义选择，不要用外部本体或领域预设替换轴定义。
2. 选择的是问题要求的观察角度，不是与问题主题相关的一般领域。
3. 二级关系数据表示该轴实际可以筛出的数据形态；只有回答问题需要其中至少一种关系实例时，才选择对应轴。
4. 不能因为问题中出现某个领域词，就假定知识库已经存在同名分类或同名知识对象。
5. 可以选择多个轴；按对完整回答的必要性从高到低排序。
6. 如果没有任何候选轴直接对应问题的关系观察角度，selected_axes 返回空数组。
7. reason 只简短说明问题中的哪个语义要求或哪类二级关系数据使该轴入选，不写模型内部推理。
8. 只返回 JSON 对象，不要 Markdown 或额外说明。

返回结构：

{
  "selected_axes": [
    {
      "axis_id": "候选轴编号",
      "reason": "短理由"
    }
  ]
}
"""


def _load_chat_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key is None:
        key = keyring.get_password(CREDENTIAL_SERVICE_NAME, CHAT_API_KEY_USERNAME)
    if not key:
        raise RuntimeError("chat_api_key_missing")
    return key


def _user_prompt(question: Question) -> str:
    axis_blocks: list[str] = []
    for axis in AXES:
        labels = "\n".join(
            f"    - {name}：{definition}" for name, definition in SECONDARY_LABELS[axis.axis_id]
        )
        axis_blocks.append(
            f"- {axis.axis_id} {axis.name}：{axis.definition}\n  可筛选的二级关系数据：\n{labels}"
        )
    axis_text = "\n".join(axis_blocks)
    return f"""当前候选关系观察轴：

{axis_text}

用户问题：
{question.text}

按返回结构给出必须检索的观察轴及其必要性排序。"""


def _validate_selection(payload: dict[str, Any]) -> list[dict[str, str]]:
    raw_axes = payload.get("selected_axes")
    if not isinstance(raw_axes, list):
        raise TypeError("selected_axes_missing")
    valid_ids = {axis.axis_id for axis in AXES}
    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_axes):
        if not isinstance(raw, dict):
            raise TypeError(f"selected_axis_not_object:{index}")
        axis_id = raw.get("axis_id")
        reason = raw.get("reason")
        if axis_id not in valid_ids:
            raise TypeError(f"selected_axis_invalid:{axis_id}")
        if axis_id in seen:
            raise TypeError(f"selected_axis_duplicate:{axis_id}")
        if not isinstance(reason, str) or not reason.strip():
            raise TypeError(f"selected_axis_reason_missing:{axis_id}")
        seen.add(axis_id)
        selected.append({"axis_id": axis_id, "reason": reason.strip()})
    return selected


async def _run_once(
    client: DeepSeekJsonClient,
    question: Question,
    run_index: int,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    async with semaphore:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                result = await client.complete_json(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=_user_prompt(question),
                    max_output_tokens=1_200,
                )
                selected = _validate_selection(result.payload)
                selected_ids = [item["axis_id"] for item in selected]
                return {
                    "question_id": question.question_id,
                    "question": question.text,
                    "run": run_index,
                    "attempt": attempt,
                    "expected_axis_ids": list(question.expected_axis_ids),
                    "selected_axes": selected,
                    "selected_axis_ids": selected_ids,
                    "primary_axis_id": selected_ids[0] if selected_ids else None,
                    "expected_hit": all(
                        axis_id in selected_ids for axis_id in question.expected_axis_ids
                    ),
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "actual_model_id": result.actual_model_id,
                }
            except (InductionFailure, TypeError, ValueError) as error:
                last_error = error
                if attempt < 3:
                    await asyncio.sleep(0.5 * attempt)
        raise RuntimeError(f"axis_selection_failed:{question.question_id}:{run_index}:{last_error}")


def _summarize(results: list[dict[str, Any]], runs_per_question: int) -> dict[str, Any]:
    by_question: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        by_question[result["question_id"]].append(result)

    summary: dict[str, Any] = {
        "runs_per_question": runs_per_question,
        "questions": {},
        "total_input_tokens": sum(result["input_tokens"] for result in results),
        "total_output_tokens": sum(result["output_tokens"] for result in results),
    }
    for question in QUESTIONS:
        question_results = by_question[question.question_id]
        primary_counts = Counter(result["primary_axis_id"] for result in question_results)
        selected_set_counts = Counter(
            tuple(result["selected_axis_ids"]) for result in question_results
        )
        summary["questions"][question.question_id] = {
            "question": question.text,
            "expected_axis_ids": list(question.expected_axis_ids),
            "expected_hit_count": sum(1 for result in question_results if result["expected_hit"]),
            "primary_axis_counts": dict(primary_counts),
            "selected_sequence_counts": {
                ",".join(sequence) if sequence else "<empty>": count
                for sequence, count in selected_set_counts.items()
            },
            "runs": question_results,
        }
    return summary


async def run(args: argparse.Namespace) -> dict[str, Any]:
    client = DeepSeekJsonClient(
        _load_chat_key(),
        model_id=args.model,
        base_url=args.base_url,
        timeout_seconds=args.timeout_seconds,
    )
    semaphore = asyncio.Semaphore(args.concurrency)
    tasks = [
        _run_once(client, question, run_index, semaphore)
        for question in QUESTIONS
        for run_index in range(1, args.runs + 1)
    ]
    results = await asyncio.gather(*tasks)
    summary = _summarize(results, args.runs)
    return {
        "prompt_version": PROMPT_VERSION,
        "requested_model_id": args.model,
        "runs_per_question": args.runs,
        "axis_input_fields": [
            "axis_id",
            "name",
            "canonical_question",
            "secondary_labels[name,definition]",
        ],
        "axes": [
            {
                "axis_id": axis.axis_id,
                "name": axis.name,
                "canonical_question": axis.definition,
                "secondary_labels": [
                    {"name": name, "definition": definition}
                    for name, definition in SECONDARY_LABELS[axis.axis_id]
                ],
            }
            for axis in AXES
        ],
        "system_prompt": SYSTEM_PROMPT,
        "summary": summary,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--model", default=DEFAULT_MODEL_ID)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    report = asyncio.run(run(args))
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"\nraw_results={OUTPUT_PATH}")


if __name__ == "__main__":
    main()
