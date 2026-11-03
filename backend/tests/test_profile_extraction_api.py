from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import httpx

from app.main import create_app
from app.profile_extraction.system_profiles import SYSTEM_TECHNICAL_PROFILE_ID
from app.providers.chat import (
    ChatCompletionResult,
    ChatMessage,
    ChatProviderProfile,
    ChatStreamChunk,
    FakeChatProvider,
)
from app.providers.embeddings import FakeEmbeddingProvider
from tests.helpers import fixture_bytes

TOKEN = "profile-extraction-api-token"


class _MemoryCredentials:
    """为 AI 建议 API 测试提供最小的内存凭证存储。"""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values or {}

    def get(self, username: str) -> str | None:
        return self.values.get(username)

    def set(self, username: str, value: str) -> None:
        self.values[username] = value

    def delete(self, username: str) -> None:
        self.values.pop(username, None)


class _SingleCandidateProvider:
    """为 API 集成测试返回当前窗口第一个 evidence block 的候选。"""

    def __init__(self) -> None:
        self.translation_request_count = 0
        self._profile = ChatProviderProfile(
            provider="fake",
            model_id="fake-profile-api",
            base_url="fake://chat",
            temperature=0.0,
            max_output_tokens=8192,
            thinking_enabled=False,
        )

    @property
    def profile(self) -> ChatProviderProfile:
        return self._profile

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> ChatCompletionResult:
        payload = json.loads(messages[-1].content)
        if "简体中文翻译器" in messages[0].content:
            self.translation_request_count += 1
            content = json.dumps(
                {
                    "translations": [
                        {
                            "block_id": block["block_id"],
                            "translated_text": f"中文翻译：{block['text']}",
                        }
                        for block in payload["evidence_blocks"]
                    ]
                },
                ensure_ascii=False,
            )
            return ChatCompletionResult(
                content=content,
                request_id=f"fake-translation-{self.translation_request_count}",
                actual_model_id=self.profile.model_id,
                finish_reason="stop",
                input_tokens=20,
                output_tokens=10,
            )
        block_id = payload["evidence_blocks"][0]["block_id"]
        content = json.dumps(
            {
                "candidates": [
                    {
                        "name": "Alpha",
                        "aliases": [],
                        "summary": "Alpha 是独立知识对象。",
                        "source_block_ids": [block_id],
                    }
                ]
            },
            ensure_ascii=False,
        )
        return ChatCompletionResult(
            content=content,
            request_id="fake-profile-api-1",
            actual_model_id=self.profile.model_id,
            finish_reason="stop",
            input_tokens=10,
            output_tokens=5,
        )

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> AsyncIterator[ChatStreamChunk]:
        result = await self.complete(messages, max_output_tokens=max_output_tokens)
        yield ChatStreamChunk(delta=result.content, result=result)


def _profile_config() -> dict[str, object]:
    return {
        "name": "API 测试 Profile",
        "description": "验证 Profile 管理 API。",
        "domain_context": "技术文档",
        "primary_uses": ["学习概念"],
        "representative_questions": ["这个机制是什么？"],
        "extraction_channels": [
            {
                "channel_id": "ec_0123456789abcdef",
                "name": "核心知识对象",
                "purpose": "发现独立知识对象。",
                "activation": "正文给出实质说明时。",
                "include_criteria": ["稳定名称"],
                "exclude_criteria": ["导航"],
                "granularity_rule": "一个对象一个候选。",
                "coverage_audit_enabled": False,
            }
        ],
        "exclude_criteria": [],
        "granularity": "standard",
        "additional_guidance": "",
        "language_policy": {
            "name": "preserve_source_term",
            "summary": "zh-CN",
        },
    }


def test_profile_management_api_is_protected_and_atomic(tmp_path: Path) -> None:
    async def run() -> None:
        app = create_app(
            TOKEN,
            data_root=tmp_path,
            embedding_provider_factory=FakeEmbeddingProvider,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            unauthorized = await client.get("/api/extraction-profiles")
            assert unauthorized.status_code == 401

            headers = {"x-launch-token": TOKEN}
            listed = await client.get("/api/extraction-profiles", headers=headers)
            listed.raise_for_status()
            system_ids = {profile["profile_id"] for profile in listed.json()["profiles"]}
            assert SYSTEM_TECHNICAL_PROFILE_ID in system_ids

            created = await client.post(
                "/api/extraction-profiles",
                headers=headers,
                json={"name": "API 测试 Profile"},
            )
            created.raise_for_status()
            profile_id = created.json()["profile_id"]

            saved = await client.put(
                f"/api/extraction-profiles/{profile_id}",
                headers=headers,
                json={"config": _profile_config()},
            )
            saved.raise_for_status()
            assert saved.json()["status"] == "draft"

            prompts = await client.get(
                f"/api/extraction-profiles/{profile_id}/compiled-prompts",
                headers=headers,
            )
            prompts.raise_for_status()
            assert len(prompts.json()) == 1
            assert "evidence_blocks" in prompts.json()[0]["prompt"]

            selected = await client.post(
                f"/api/extraction-profiles/{profile_id}/select",
                headers=headers,
                json={},
            )
            selected.raise_for_status()
            assert selected.json()["selected_profile_id"] == profile_id

            frozen_update = await client.put(
                f"/api/extraction-profiles/{profile_id}",
                headers=headers,
                json={"config": _profile_config()},
            )
            assert frozen_update.status_code == 409

            selection = await client.get(
                "/api/extraction-profile-selection",
                headers=headers,
            )
            selection.raise_for_status()
            assert selection.json()["selected_profile_id"] == profile_id

    asyncio.run(run())


def test_channel_attribute_suggestion_api_returns_unapplied_draft(tmp_path: Path) -> None:
    provider = FakeChatProvider(
        [
            json.dumps(
                {
                    "activation": "正文说明机制或边界时启用。",
                    "include_criteria": ["存在稳定名称"],
                    "exclude_criteria": ["只有导航提及"],
                    "granularity_rule": "一个独立对象对应一个 candidate。",
                    "coverage_audit_enabled": False,
                },
                ensure_ascii=False,
            )
        ]
    )

    async def run() -> dict[str, object]:
        app = create_app(
            TOKEN,
            data_root=tmp_path,
            credential_store=_MemoryCredentials({"chat-api-key": "test-key"}),
            embedding_provider_factory=FakeEmbeddingProvider,
            chat_provider_factory=lambda _key: provider,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/extraction-profiles/channel-attribute-suggestions",
                headers={"x-launch-token": TOKEN},
                json={
                    "profile_name": "技术学习",
                    "primary_uses": ["学习核心机制"],
                    "domain_context": "RAG",
                    "channel_name": "核心概念",
                    "channel_purpose": "发现可以独立检索的概念",
                },
            )
            response.raise_for_status()
            return response.json()

    body = asyncio.run(run())

    assert body["activation"] == "正文说明机制或边界时启用。"
    assert body["include_criteria"] == ["存在稳定名称"]
    assert body["request_id"]


def test_profile_preview_api_runs_and_paginates_candidates(tmp_path: Path) -> None:
    source = tmp_path / "profile-api.html"
    source.write_bytes(fixture_bytes("complex-article.html"))

    async def run() -> None:
        provider = _SingleCandidateProvider()
        app = create_app(
            TOKEN,
            data_root=tmp_path,
            credential_store=_MemoryCredentials({"chat-api-key": "test-key"}),
            embedding_provider_factory=FakeEmbeddingProvider,
            chat_provider_factory=lambda _key: provider,
        )
        headers = {"x-launch-token": TOKEN}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            created_task = await client.post(
                "/api/tasks/ingest",
                headers=headers,
                json={"sources": [{"source_kind": "file", "source_value": str(source)}]},
            )
            created_task.raise_for_status()
            task_id = created_task.json()["task_id"]
            for _ in range(300):
                task_response = await client.get(f"/api/tasks/{task_id}", headers=headers)
                task_response.raise_for_status()
                task = task_response.json()
                if task["status"] in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(0.02)
            assert task["status"] == "succeeded"

            profile = await client.post(
                "/api/extraction-profiles",
                headers=headers,
                json={"name": "Preview API Profile"},
            )
            profile.raise_for_status()
            profile_id = profile.json()["profile_id"]
            saved = await client.put(
                f"/api/extraction-profiles/{profile_id}",
                headers=headers,
                json={"config": _profile_config()},
            )
            saved.raise_for_status()

            collection = await client.get("/api/collections/current", headers=headers)
            collection.raise_for_status()
            snapshot_id = collection.json()["manifest"]["snapshot_id"]
            doc_id = collection.json()["manifest"]["documents"][0]["doc_id"]
            plan = await client.post(
                "/api/extraction-preview-runs/plan",
                headers=headers,
                json={
                    "profile_id": profile_id,
                    "snapshot_id": snapshot_id,
                    "document_ids": [doc_id],
                    "execution_mode": "reuse_if_exact",
                },
            )
            plan.raise_for_status()
            assert plan.json()["estimated_model_calls"] == 1
            assert plan.json()["estimated_total_tokens"] == (
                plan.json()["estimated_input_tokens"] + plan.json()["estimated_output_tokens"]
            )

            created_run = await client.post(
                "/api/extraction-preview-runs",
                headers=headers,
                json={
                    "profile_id": profile_id,
                    "snapshot_id": snapshot_id,
                    "document_ids": [doc_id],
                    "execution_mode": "force_new",
                },
            )
            created_run.raise_for_status()
            run_id = created_run.json()["run_id"]
            for _ in range(300):
                run_response = await client.get(
                    f"/api/extraction-preview-runs/{run_id}",
                    headers=headers,
                )
                run_response.raise_for_status()
                run = run_response.json()
                if run["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.02)
            assert run["status"] == "completed"
            candidates = await client.get(
                f"/api/extraction-preview-runs/{run_id}/candidates",
                headers=headers,
            )
            candidates.raise_for_status()
            assert candidates.json()["total"] == 1
            candidate = candidates.json()["candidates"][0]
            assert candidate["name"] == "Alpha"
            assert candidate["evidence_blocks"][0]["text"]
            block_id = candidate["evidence_blocks"][0]["block_id"]
            translated = await client.post(
                f"/api/extraction-preview-runs/{run_id}/translate-evidence",
                headers=headers,
                json={"block_ids": [block_id]},
            )
            translated.raise_for_status()
            assert translated.json()["translated_count"] == 1
            assert translated.json()["items"][0]["reused"] is False
            cached = await client.post(
                f"/api/extraction-preview-runs/{run_id}/translate-evidence",
                headers=headers,
                json={"block_ids": [block_id]},
            )
            cached.raise_for_status()
            assert cached.json()["reused_count"] == 1
            assert provider.translation_request_count == 1

            source_blocks = await client.get(
                "/api/document-checklists/blocks",
                headers=headers,
                params={"snapshot_id": snapshot_id, "doc_id": doc_id},
            )
            source_blocks.raise_for_status()
            source_only_block_id = next(
                block["block_id"] for block in source_blocks.json() if block["block_id"] != block_id
            )
            source_only_translation = await client.post(
                f"/api/extraction-preview-runs/{run_id}/translate-evidence",
                headers=headers,
                json={"block_ids": [source_only_block_id]},
            )
            source_only_translation.raise_for_status()
            assert source_only_translation.json()["items"][0]["reused"] is False
            assert provider.translation_request_count == 2

            second_run_response = await client.post(
                "/api/extraction-preview-runs",
                headers=headers,
                json={
                    "profile_id": profile_id,
                    "snapshot_id": snapshot_id,
                    "document_ids": [doc_id],
                    "execution_mode": "force_new",
                },
            )
            second_run_response.raise_for_status()
            second_run_id = second_run_response.json()["run_id"]
            for _ in range(300):
                second_run = await client.get(
                    f"/api/extraction-preview-runs/{second_run_id}",
                    headers=headers,
                )
                second_run.raise_for_status()
                if second_run.json()["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.02)
            assert second_run.json()["status"] == "completed"
            second_candidates = await client.get(
                f"/api/extraction-preview-runs/{second_run_id}/candidates",
                headers=headers,
            )
            second_candidates.raise_for_status()
            second_block_id = second_candidates.json()["candidates"][0]["evidence_blocks"][0][
                "block_id"
            ]
            reused_across_runs = await client.post(
                f"/api/extraction-preview-runs/{second_run_id}/translate-evidence",
                headers=headers,
                json={"block_ids": [second_block_id]},
            )
            reused_across_runs.raise_for_status()
            assert reused_across_runs.json()["reused_count"] == 1
            assert provider.translation_request_count == 2

    asyncio.run(run())
