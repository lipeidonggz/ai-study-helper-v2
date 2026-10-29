from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app.evaluation.api import create_eval_app
from app.tracing.store import TraceStore

TOKEN = "evaluation-api-test-token"


def test_evaluation_api_exposes_seeded_suite_without_product_routes(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        product_root = tmp_path / "product"
        eval_root = tmp_path / "eval"
        app = create_eval_app(
            TOKEN,
            product_data_root=product_root,
            eval_data_root=eval_root,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://eval",
        ) as client:
            health = await client.get("/api/eval/health")
            assert health.status_code == 200
            assert health.json()["schema_version"] == 7
            assert (await client.get("/api/eval/suites")).status_code == 401
            preflight = await client.options(
                "/api/eval/cases/c1-001/draft",
                headers={
                    "origin": "http://127.0.0.1:1420",
                    "access-control-request-method": "PUT",
                    "access-control-request-headers": "content-type,x-launch-token",
                },
            )
            assert preflight.status_code == 200
            assert "PUT" in preflight.headers["access-control-allow-methods"]

            suites = await client.get(
                "/api/eval/suites",
                headers={"x-launch-token": TOKEN},
            )
            assert suites.status_code == 200
            body = suites.json()
            assert len(body) == 4
            body = [
                suite for suite in body if suite["review_status"] == "active" and not suite["stale"]
            ]
            assert len(body) == 1
            assert body[0]["case_count"] == 18
            assert sum(len(case["criteria"]) for case in body[0]["cases"]) == 69
            prompt = await client.get(
                "/api/eval/judge-prompt",
                headers={"x-launch-token": TOKEN},
            )
            assert prompt.status_code == 200
            assert prompt.json()["version"] == "judge-rubric-v3"
            citation_prompt = await client.get(
                "/api/eval/citation-prompt",
                headers={"x-launch-token": TOKEN},
            )
            assert citation_prompt.status_code == 200
            assert citation_prompt.json()["version"] == "citation-rubric-v11"
            current_baseline = await client.get(
                "/api/eval/baselines/current?suite_id=design03&run_type=quality",
                headers={"x-launch-token": TOKEN},
            )
            assert current_baseline.status_code == 200
            assert current_baseline.json() is None
            baseline_history = await client.get(
                "/api/eval/baselines?suite_id=design03&run_type=quality",
                headers={"x-launch-token": TOKEN},
            )
            assert baseline_history.status_code == 200
            assert baseline_history.json() == []

            assert (await client.get("/health")).status_code == 404
            assert (await client.get("/api/agent/run")).status_code == 404

    asyncio.run(run())


def test_evaluation_api_requires_distinct_data_roots(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="must differ"):
        create_eval_app(
            TOKEN,
            product_data_root=tmp_path,
            eval_data_root=tmp_path,
        )


def test_invalid_plan_does_not_start_run(tmp_path: Path) -> None:
    async def run() -> None:
        app = create_eval_app(
            TOKEN,
            product_data_root=tmp_path / "product",
            eval_data_root=tmp_path / "eval",
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://eval",
        ) as client:
            headers = {"x-launch-token": TOKEN}
            response = await client.post(
                "/api/eval/plans",
                headers=headers,
                json={
                    "suite_revision_id": "sr_design03_r1",
                    "collection_snapshot_id": "s_0123456789abcdef",
                    "run_type": "quality",
                    "concurrency": 8,
                    "total_token_budget": 1_000_000,
                    "model_request_budget": 1_000,
                    "replacement_attempt_budget": 10,
                    "attempt_timeout_seconds": 600,
                },
            )
            assert response.status_code == 200
            plan = response.json()
            assert plan["status"] == "invalid_plan"

            started = await client.post(
                "/api/eval/runs",
                headers=headers,
                json={"plan_id": plan["plan_id"]},
            )
            assert started.status_code == 409
            assert started.json()["error"]["code"] == "evaluation_state_conflict"

    asyncio.run(run())


def test_evaluation_api_reads_trace_artifacts(tmp_path: Path) -> None:
    eval_root = tmp_path / "eval"
    store = TraceStore(eval_root)
    session = store.new_session(
        mode="full",
        question="评测 trace artifact 测试",
        app_version="0.1.0-test",
        versions={"prompt_bundle_version": "test"},
        budget={"max_request_tokens": 1000},
    )
    session.finish(
        status="succeeded",
        termination_reason="finish",
        answer_completeness="complete",
        agent_turns=0,
        tool_calls=0,
        model_calls=0,
        retry_count=0,
        input_tokens=0,
        output_tokens=0,
        answer="评测运行答案",
        limitations=[],
    )
    assert store.wait_for_idle(3)
    store.close()

    async def run() -> None:
        app = create_eval_app(
            TOKEN,
            product_data_root=tmp_path / "product",
            eval_data_root=eval_root,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://eval",
        ) as client:
            headers = {"x-launch-token": TOKEN}
            detail = await client.get(
                f"/api/eval/traces/{session.trace_id}",
                headers=headers,
            )
            assert detail.status_code == 200
            answer_artifact = next(
                artifact
                for artifact in detail.json()["manifest"]["artifacts"]
                if artifact["kind"] == "answer"
            )
            artifact = await client.get(
                f"/api/eval/traces/{session.trace_id}/artifacts/{answer_artifact['artifact_id']}",
                headers=headers,
            )
            assert artifact.status_code == 200
            assert artifact.content.decode("utf-8") == "评测运行答案"

    asyncio.run(run())


def test_case_and_suite_authoring_api_flow(tmp_path: Path) -> None:
    async def run() -> None:
        app = create_eval_app(
            TOKEN,
            product_data_root=tmp_path / "product",
            eval_data_root=tmp_path / "eval",
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://eval",
        ) as client:
            headers = {"x-launch-token": TOKEN}
            catalog = await client.get("/api/eval/cases", headers=headers)
            assert catalog.status_code == 200
            case = next(item for item in catalog.json() if item["case_id"] == "c1-001")
            created = await client.post(
                "/api/eval/cases/c1-001/draft",
                headers=headers,
                json={"updated_by": "api-test"},
            )
            assert created.status_code == 201
            draft = created.json()
            draft["question"] = "API 修改后的问题"
            saved = await client.put(
                "/api/eval/cases/c1-001/draft",
                headers=headers,
                json=draft,
            )
            assert saved.status_code == 200
            published = await client.post(
                "/api/eval/cases/c1-001/draft/publish",
                headers=headers,
                json={"reviewer": "api-test"},
            )
            assert published.status_code == 200
            reviewed = published.json()
            assert reviewed["review_status"] == "reviewed"
            activated = await client.post(
                f"/api/eval/cases/revisions/{reviewed['revision_id']}/review",
                headers=headers,
                json={"review_status": "active", "reviewer": "api-test"},
            )
            assert activated.status_code == 200

            suite_draft = await client.post(
                "/api/eval/suites/design03/draft",
                headers=headers,
                json={"updated_by": "api-test"},
            )
            assert suite_draft.status_code == 201
            saved_suite = await client.put(
                "/api/eval/suites/design03/draft",
                headers=headers,
                json={
                    "name": "API 小套件",
                    "description": "两条用例",
                    "scope_id": "api-smoke",
                    "case_revision_ids": [
                        reviewed["revision_id"],
                        "cr_c2_001_minimal_judge_v2",
                    ],
                    "change_note": "API 验收",
                    "updated_by": "api-test",
                },
            )
            assert saved_suite.status_code == 200
            suite = await client.post(
                "/api/eval/suites/design03/draft/publish",
                headers=headers,
                json={"reviewer": "api-test"},
            )
            assert suite.status_code == 200
            published_suite = suite.json()
            assert published_suite["case_count"] == 2
            assert published_suite["stale"] is False

            quick_plan = await client.post(
                "/api/eval/quick-plans",
                headers=headers,
                json={
                    "case_revision_ids": [reviewed["revision_id"]],
                    "collection_snapshot_id": "s_0123456789abcdef",
                    "n_override": 1,
                    "total_token_budget": 1_000_000,
                    "model_request_budget": 100,
                },
            )
            assert quick_plan.status_code == 200
            quick_body = quick_plan.json()
            assert quick_body["mode"] == "exploratory"
            assert quick_body["suite_revision_id"] is None
            assert quick_body["cases"][0]["n"] == 1
            assert quick_body["cases"][0]["source_n"] == 10

            assert case["active_revision_id"] == "cr_c1_001_minimal_judge_v2"

    asyncio.run(run())
