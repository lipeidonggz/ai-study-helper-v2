from __future__ import annotations

from fastapi import APIRouter, Query, Response, status

from app.profile_extraction.checklist_service import ChecklistService
from app.profile_extraction.comparison_service import ComparisonService
from app.profile_extraction.models import (
    ChannelAttributeSuggestionRequest,
    ChannelAttributeSuggestionResponse,
    ChecklistCreateRequest,
    ChecklistSaveRequest,
    ChecklistSuggestRequest,
    ChecklistSuggestResponse,
    DocumentChecklist,
    EvidenceTranslationRequest,
    EvidenceTranslationResponse,
    ExtractionProfile,
    PreviewCandidatePage,
    PreviewRunDetail,
    PreviewRunPlan,
    PreviewRunPlanRequest,
    PreviewRunRequest,
    PreviewRunSummary,
    ProfileCloneRequest,
    ProfileComparison,
    ProfileComparisonConcludeRequest,
    ProfileComparisonCreateRequest,
    ProfileComparisonItemDecisionRequest,
    ProfileCreateRequest,
    ProfileListResponse,
    ProfileSaveRequest,
    ProfileSelectionResponse,
    ProfileValidationResponse,
    StrategySuggestionRequest,
    StrategySuggestionResponse,
)
from app.profile_extraction.preview_service import ProfilePreviewService
from app.profile_extraction.profile_service import ProfileService
from app.profile_extraction.prompt_compiler import compile_profile_prompts


def create_profile_extraction_router(
    *,
    profiles: ProfileService,
    previews: ProfilePreviewService,
    checklists: ChecklistService,
    comparisons: ComparisonService,
) -> APIRouter:
    """构造 Profile 管理、Preview、Checklist 和比较 API。"""

    router = APIRouter(prefix="/api")

    @router.get("/extraction-profile-selection", response_model=ProfileSelectionResponse)
    def get_profile_selection() -> ProfileSelectionResponse:
        """返回知识库当前 selected Profile。"""

        return profiles.selection()

    @router.get("/extraction-profiles", response_model=ProfileListResponse)
    def list_profiles() -> ProfileListResponse:
        """返回系统与用户 Profile 列表。"""

        return profiles.list_profiles()

    @router.post(
        "/extraction-profiles/strategy-suggestions",
        response_model=StrategySuggestionResponse,
    )
    async def suggest_profile_strategy(
        request: StrategySuggestionRequest,
    ) -> StrategySuggestionResponse:
        """生成不自动保存的抽取通道草案。"""

        return await profiles.suggest_strategy(request)

    @router.post(
        "/extraction-profiles/channel-attribute-suggestions",
        response_model=ChannelAttributeSuggestionResponse,
    )
    async def suggest_channel_attributes(
        request: ChannelAttributeSuggestionRequest,
    ) -> ChannelAttributeSuggestionResponse:
        """生成不自动保存的单通道属性草案。"""

        return await profiles.suggest_channel_attributes(request)

    @router.post("/extraction-profiles", response_model=ExtractionProfile)
    def create_profile(request: ProfileCreateRequest) -> ExtractionProfile:
        """创建空白 Profile draft，或从已有 Profile 克隆。"""

        return profiles.create_profile(request)

    @router.get("/extraction-profiles/{profile_id}", response_model=ExtractionProfile)
    def get_profile(profile_id: str) -> ExtractionProfile:
        """读取一个 Profile。"""

        return profiles.get_profile(profile_id)

    @router.put("/extraction-profiles/{profile_id}", response_model=ExtractionProfile)
    def save_profile(
        profile_id: str,
        request: ProfileSaveRequest,
    ) -> ExtractionProfile:
        """全量保存一个 draft Profile。"""

        return profiles.save_draft(profile_id, request)

    @router.delete(
        "/extraction-profiles/{profile_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_profile(profile_id: str) -> Response:
        """删除未冻结的非系统 draft Profile。"""

        profiles.delete_draft(profile_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/extraction-profiles/{profile_id}/clone",
        response_model=ExtractionProfile,
    )
    def clone_profile(
        profile_id: str,
        request: ProfileCloneRequest,
    ) -> ExtractionProfile:
        """从已有 Profile 创建新的 draft。"""

        return profiles.create_profile(
            ProfileCreateRequest(
                name=request.name or "",
                clone_from_profile_id=profile_id,
            )
        )

    @router.post(
        "/extraction-profiles/{profile_id}/validate",
        response_model=ProfileValidationResponse,
    )
    def validate_profile(profile_id: str) -> ProfileValidationResponse:
        """校验 Profile 是否满足冻结条件。"""

        return profiles.validate_profile(profile_id)

    @router.post(
        "/extraction-profiles/{profile_id}/select",
        response_model=ProfileSelectionResponse,
    )
    def select_profile(profile_id: str) -> ProfileSelectionResponse:
        """校验、必要时冻结并设置 selected Profile。"""

        return profiles.select_profile(profile_id)

    @router.get("/extraction-profiles/{profile_id}/compiled-prompts")
    def compiled_prompts(profile_id: str) -> list[dict[str, str]]:
        """返回 Profile 各通道的只读编译 Prompt。"""

        profile = profiles.get_profile(profile_id)
        return [
            {
                "channel_id": compiled.channel.channel_id,
                "channel_name": compiled.channel.name,
                "prompt": compiled.system_prompt,
                "prompt_hash": compiled.prompt_hash,
            }
            for compiled in compile_profile_prompts(profile.config)
        ]

    @router.post("/extraction-preview-runs/plan", response_model=PreviewRunPlan)
    def plan_preview_run(request: PreviewRunPlanRequest) -> PreviewRunPlan:
        """计算命中文档、窗口、调用量和粗略 token 估算。"""

        return previews.plan(request)

    @router.post("/extraction-preview-runs", response_model=PreviewRunDetail)
    async def create_preview_run(request: PreviewRunRequest) -> PreviewRunDetail:
        """创建、排队或复用一次 Preview Run。"""

        return await previews.create(request)

    @router.get("/extraction-preview-runs", response_model=list[PreviewRunSummary])
    def list_preview_runs() -> list[PreviewRunSummary]:
        """列出 Preview Run 历史。"""

        return previews.list_runs()

    @router.get(
        "/extraction-preview-runs/{run_id}",
        response_model=PreviewRunDetail,
    )
    def get_preview_run(run_id: str) -> PreviewRunDetail:
        """读取 Preview Run 状态和第一页候选。"""

        return previews.get_run(run_id)

    @router.get(
        "/extraction-preview-runs/{run_id}/candidates",
        response_model=PreviewCandidatePage,
    )
    def list_preview_candidates(
        run_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> PreviewCandidatePage:
        """分页读取正式 Preview 候选。"""

        return previews.list_candidates(run_id, offset=offset, limit=limit)

    @router.post(
        "/extraction-preview-runs/{run_id}/translate-evidence",
        response_model=EvidenceTranslationResponse,
    )
    async def translate_preview_evidence(
        run_id: str,
        request: EvidenceTranslationRequest,
    ) -> EvidenceTranslationResponse:
        """翻译试抽取中的证据块，并复用已缓存结果。"""

        return await previews.translate_evidence(run_id, request)

    @router.post(
        "/extraction-preview-runs/{run_id}/cancel",
        response_model=PreviewRunSummary,
    )
    def cancel_preview_run(run_id: str) -> PreviewRunSummary:
        """取消 queued run 或请求 running run 在 checkpoint 停止。"""

        return previews.cancel(run_id)

    @router.delete(
        "/extraction-preview-runs/{run_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_preview_run(run_id: str) -> Response:
        """删除未被引用的终态 Preview Run。"""

        previews.delete(run_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post("/document-checklists/suggest", response_model=ChecklistSuggestResponse)
    async def suggest_checklist(
        request: ChecklistSuggestRequest,
    ) -> ChecklistSuggestResponse:
        """生成不自动保存的文档清单建议。"""

        return await checklists.suggest(request)

    @router.post("/document-checklists", response_model=DocumentChecklist)
    def create_checklist(request: ChecklistCreateRequest) -> DocumentChecklist:
        """创建空清单 draft。"""

        return checklists.create(request)

    @router.get("/document-checklists/blocks")
    def list_checklist_blocks(
        snapshot_id: str,
        doc_id: str,
    ) -> list[dict[str, object]]:
        """读取清单绑定 snapshot 的完整 evidence blocks。"""

        return checklists.document_blocks(snapshot_id, doc_id)

    @router.get("/document-checklists", response_model=list[DocumentChecklist])
    def list_checklists() -> list[DocumentChecklist]:
        """列出文档清单历史。"""

        return checklists.list()

    @router.get(
        "/document-checklists/{checklist_id}",
        response_model=DocumentChecklist,
    )
    def get_checklist(checklist_id: str) -> DocumentChecklist:
        """读取一个文档清单。"""

        return checklists.get(checklist_id)

    @router.put(
        "/document-checklists/{checklist_id}",
        response_model=DocumentChecklist,
    )
    def save_checklist(
        checklist_id: str,
        request: ChecklistSaveRequest,
    ) -> DocumentChecklist:
        """全量保存 draft 清单。"""

        return checklists.save(checklist_id, request)

    @router.delete(
        "/document-checklists/{checklist_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_checklist(checklist_id: str) -> Response:
        """删除未冻结且未被比较引用的清单。"""

        checklists.delete(checklist_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.post(
        "/document-checklists/{checklist_id}/freeze",
        response_model=DocumentChecklist,
    )
    def freeze_checklist(checklist_id: str) -> DocumentChecklist:
        """冻结文档清单。"""

        return checklists.freeze(checklist_id)

    @router.post("/profile-comparisons", response_model=ProfileComparison)
    def create_profile_comparison(
        request: ProfileComparisonCreateRequest,
    ) -> ProfileComparison:
        """创建逐文档 Profile 比较 draft。"""

        return comparisons.create(request)

    @router.get("/profile-comparisons", response_model=list[ProfileComparison])
    def list_profile_comparisons() -> list[ProfileComparison]:
        """列出历史 Profile Comparison。"""

        return comparisons.list()

    @router.get(
        "/profile-comparisons/{comparison_id}",
        response_model=ProfileComparison,
    )
    def get_profile_comparison(comparison_id: str) -> ProfileComparison:
        """读取一个 Profile Comparison。"""

        return comparisons.get(comparison_id)

    @router.post(
        "/profile-comparisons/{comparison_id}/item-decisions",
        response_model=ProfileComparison,
    )
    def apply_comparison_item_decisions(
        comparison_id: str,
        request: ProfileComparisonItemDecisionRequest,
    ) -> ProfileComparison:
        """应用用户对 uncertain 项的 matched／missing 决定。"""

        return comparisons.apply_item_decisions(comparison_id, request)

    @router.post(
        "/profile-comparisons/{comparison_id}/conclude",
        response_model=ProfileComparison,
    )
    def conclude_profile_comparison(
        comparison_id: str,
        request: ProfileComparisonConcludeRequest,
    ) -> ProfileComparison:
        """提交最终比较结论并冻结比较。"""

        return comparisons.conclude(comparison_id, request)

    return router
