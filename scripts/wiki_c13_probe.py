"""Run the 068 Wiki evidence-map probe against the fixed c13-003 snapshot."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.providers.chat import DeepSeekChatProvider  # noqa: E402
from app.retrieval.models import RetrievalBlock  # noqa: E402
from app.retrieval.service import RetrievalReadService  # noqa: E402
from app.storage.credentials import (  # noqa: E402
    CHAT_API_KEY_USERNAME,
    WindowsCredentialStore,
)
from app.wiki.models import WikiCompileRequest, WikiPageSpec  # noqa: E402
from app.wiki.service import WikiCompiler  # noqa: E402

DEFAULT_DATA_ROOT = BACKEND_ROOT / ".runtime" / "wiki-c13-068"
DEFAULT_SNAPSHOT_ID = "s_6b787258ba7d700c"
DEFAULT_SUMMARY = BACKEND_ROOT / ".runtime" / "wiki-c13-068-summary.json"
TARGET_BLOCKS = {
    "worktree": "doc_983ba4e2cbff4185873d525a69195fdf#v0001#c1#b00025",
    "checkpoint_resume": "doc_7f36f426b84f40eb9439acaad4949e32#v0001#c1#b00016",
    "checkpoint_evaluation": "doc_7f36f426b84f40eb9439acaad4949e32#v0001#c1#b00022",
}
TASK_STATE_DOC_IDS = (
    "doc_7f36f426b84f40eb9439acaad4949e32",
    "doc_6c0a83dc425349d6ac08a20fba5ab94f",
    "doc_983ba4e2cbff4185873d525a69195fdf",
    "doc_a0ffdc255fe64a00b41f69a95dfbbf1b",
    "doc_23f78d4aa7c1444ab4d4a48c96bf6736",
)


def _snapshot_blocks(
    data_root: Path,
    snapshot_id: str,
    *,
    mode: str,
) -> list[RetrievalBlock]:
    """读取固定快照指定范围的 block；full 模式再与 manifest 总数对账。"""

    reader = RetrievalReadService(data_root).bind_snapshot(snapshot_id)
    blocks: list[RetrievalBlock] = []
    for document in reader.list_documents().documents:
        if mode == "task-state" and document.doc_id not in TASK_STATE_DOC_IDS:
            continue
        blocks.extend(reader.list_blocks(document.doc_id).blocks)
    if mode == "full" and len(blocks) != reader.manifest.totals.blocks:
        raise RuntimeError(
            f"snapshot block count mismatch: {len(blocks)} != {reader.manifest.totals.blocks}"
        )
    return blocks


def _page_spec(mode: str, blocks: list[RetrievalBlock]) -> WikiPageSpec:
    """为完整主题或任务状态子面构造显式证据输入。"""

    if mode == "full":
        return WikiPageSpec(
            title="AI Agent 隔离与控制边界",
            aliases=["Agent 隔离", "Agent 控制边界"],
            coverage_questions=[
                "除了沙箱、虚拟机和出口控制，AI Agent 还有哪些隔离或控制边界？",
                "每类边界分别限制什么？",
                "并行工作、任务状态与恢复过程受到哪些边界约束？",
            ],
            source_block_ids=[block.block_id for block in blocks],
        )
    return WikiPageSpec(
        title="任务并行、状态与恢复边界",
        aliases=["任务状态隔离", "Agent 恢复边界"],
        coverage_questions=[
            "并行任务和工作区之间如何保持状态隔离？",
            "检查点、断点续跑和回退分别控制什么？",
            "如何验证恢复后的终态正确，而不是只检查过程？",
            "上下文、记忆和子 Agent 如何限制状态污染？",
        ],
        source_block_ids=[block.block_id for block in blocks],
    )


def _target_occurrences(
    paragraphs: list[Any],
) -> dict[str, list[dict[str, object]]]:
    """列出每个已知探针被哪些页面段落直接引用。"""

    result: dict[str, list[dict[str, object]]] = {}
    for name, target_id in TARGET_BLOCKS.items():
        occurrences = []
        for index, paragraph in enumerate(paragraphs):
            if target_id in paragraph.source_block_ids:
                occurrences.append(
                    {
                        "paragraph_index": index,
                        "text": paragraph.text,
                        "source_block_ids": paragraph.source_block_ids,
                    }
                )
        result[name] = occurrences
    return result


async def run(args: argparse.Namespace) -> dict[str, object]:
    """执行单页全语料证据地图编译并返回可审查摘要。"""

    api_key = WindowsCredentialStore().get(CHAT_API_KEY_USERNAME)
    if not api_key:
        raise RuntimeError("chat API key is required")
    blocks = _snapshot_blocks(args.data_root, args.snapshot_id, mode=args.mode)
    request = WikiCompileRequest(pages=[_page_spec(args.mode, blocks)])
    result = await WikiCompiler(args.data_root).compile(
        args.snapshot_id,
        request,
        provider=DeepSeekChatProvider(api_key),
    )
    page = result.pages[0]
    page_summary = result.manifest.pages[0]
    targets = _target_occurrences(page.paragraphs)
    summary: dict[str, object] = {
        "snapshot_id": args.snapshot_id,
        "mode": args.mode,
        "build_id": result.manifest.build_id,
        "page_id": page.page_id,
        "provider": result.manifest.provider.model_dump(mode="json"),
        "usage": [item.model_dump(mode="json") for item in result.manifest.usage],
        "candidate_block_count": page_summary.candidate_block_count,
        "referenced_block_count": page_summary.referenced_block_count,
        "unreferenced_block_count": len(page_summary.unreferenced_block_ids),
        "paragraph_count": len(page.paragraphs),
        "targets": targets,
        "targets_directly_cited": {
            name: bool(occurrences) for name, occurrences in targets.items()
        },
        "paragraphs": [
            {
                "text": paragraph.text,
                "source_block_ids": paragraph.source_block_ids,
            }
            for paragraph in page.paragraphs
        ],
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    """解析参数并打印不含模型正文的顶层结果。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--snapshot-id", default=DEFAULT_SNAPSHOT_ID)
    parser.add_argument("--mode", choices=("full", "task-state"), default="full")
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    summary = asyncio.run(run(args))
    print(
        json.dumps(
            {
                "build_id": summary["build_id"],
                "page_id": summary["page_id"],
                "mode": summary["mode"],
                "candidate_block_count": summary["candidate_block_count"],
                "referenced_block_count": summary["referenced_block_count"],
                "unreferenced_block_count": summary["unreferenced_block_count"],
                "paragraph_count": summary["paragraph_count"],
                "targets_directly_cited": summary["targets_directly_cited"],
                "summary_path": str(args.summary),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
