"""Run the product Agent once against a fixed snapshot with Wiki tools enabled."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.agent.factory import AgentExecutionService  # noqa: E402
from app.settings.models import RuntimeSettingsResponse  # noqa: E402
from app.storage.credentials import WindowsCredentialStore  # noqa: E402
from app.tracing.store import TraceStore  # noqa: E402


async def run(args: argparse.Namespace) -> dict[str, object]:
    """Run the Agent and return its answer plus product-observable diagnostics."""

    settings = RuntimeSettingsResponse(
        top_k=args.top_k,
        max_output_tokens=args.max_output_tokens,
        max_context_tokens=args.max_context_tokens,
        max_request_tokens=args.max_request_tokens,
        max_tool_calls=args.max_tool_calls,
        trace_mode=args.trace_mode,
    )
    trace_store = TraceStore(args.data_root)
    try:
        service = AgentExecutionService(
            collection_data_root=args.data_root,
            credentials=WindowsCredentialStore(),
            settings_provider=lambda: settings,
            trace_store=trace_store,
        )
        result = await service.create_runtime().run(
            args.question,
            snapshot_id=args.snapshot_id,
        )
    finally:
        trace_store.close()
    return {
        "status": result.status,
        "trace_id": result.trace_id,
        "trace_status": result.trace_status,
        "answer": result.answer,
        "answer_completeness": result.answer_completeness,
        "termination_reason": result.termination_reason,
        "prompt_bundle_version": result.prompt_bundle_version,
        "prompt_template_hash": result.prompt_template_hash,
        "agent_turns": result.agent_turns,
        "tool_calls": result.tool_calls,
        "model_calls": result.model_calls,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "limitations": [limitation.model_dump(mode="json") for limitation in result.limitations],
        "sources": result.sources,
        "events": [
            {"event_type": event.event_type, "payload": event.payload} for event in result.events
        ],
    }


def main() -> None:
    """Parse CLI arguments and print one Agent probe result."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--max-output-tokens", type=int, default=8_192)
    parser.add_argument("--max-context-tokens", type=int, default=100_000)
    parser.add_argument("--max-request-tokens", type=int, default=400_000)
    parser.add_argument("--max-tool-calls", type=int, default=8)
    parser.add_argument(
        "--trace-mode",
        choices=("off", "metadata", "full"),
        default="metadata",
    )
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
