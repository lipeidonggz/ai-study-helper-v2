import { API_TIMEOUT_MS, fetchWithTimeout } from "./api";

/** 一个用户可编辑的职责单一抽取通道。 */
export interface ExtractionChannel {
  channel_id: string;
  name: string;
  purpose: string;
  activation: string;
  include_criteria: string[];
  exclude_criteria: string[];
  granularity_rule: string;
  omission_retry_enabled: boolean;
}

/** 模型为单个通道生成的其余属性，不包含名称、目的或通道 ID。 */
export interface ChannelAttributeSuggestion {
  activation: string;
  include_criteria: string[];
  exclude_criteria: string[];
  granularity_rule: string;
  omission_retry_enabled: boolean;
  request_id: string;
}

/** 由确定性编译器消费的 Profile 配置。 */
export interface ExtractionProfileConfig {
  name: string;
  description: string;
  domain_context: string;
  primary_uses: string[];
  representative_questions: string[];
  extraction_channels: ExtractionChannel[];
  exclude_criteria: string[];
  granularity: "standard" | "fine" | "coarse";
  additional_guidance: string;
  language_policy: {
    name: "preserve_source_term";
    summary: "zh-CN";
  };
}

/** Profile 的持久化视图。 */
export interface ExtractionProfile {
  profile_id: string;
  config: ExtractionProfileConfig;
  status: "draft" | "frozen" | "conflicted";
  is_system: boolean;
  created_from_profile_id: string | null;
  profile_hash: string;
  created_at: string;
  updated_at: string;
  frozen_at: string | null;
}

/** Profile 列表及其当前 selected 指针。 */
export interface ProfileListResponse {
  profiles: ExtractionProfile[];
  selected_profile_id: string | null;
}

/** 一个可定位到通道或字段的 Profile 校验问题。 */
export interface ProfileValidationIssue {
  code: string;
  message: string;
  channel_id: string | null;
  field: string | null;
}

/** Profile 冻结前的确定性校验结果。 */
export interface ProfileValidationResponse {
  valid: boolean;
  issues: ProfileValidationIssue[];
}

/** 知识库当前选择的冻结 Profile。 */
export interface ProfileSelection {
  selected_profile_id: string | null;
  selected_profile_hash: string | null;
  selected_at: string | null;
}

/** 启动预览前的范围与粗略成本估算。 */
export interface PreviewRunPlan {
  snapshot_id: string;
  profile_id: string;
  profile_hash: string;
  document_ids: string[];
  block_count: number;
  window_count: number;
  estimated_extraction_calls: number;
  estimated_document_anchor_calls: number;
  estimated_document_anchor_input_tokens: number;
  estimated_document_anchor_output_tokens: number;
  estimated_semantic_audit_calls: number;
  estimated_name_scope_audit_calls: number;
  estimated_name_meaning_audit_calls: number;
  estimated_model_calls: number;
  estimated_input_tokens: number;
  estimated_output_tokens: number;
  estimated_total_tokens: number;
  document_anchor_soft_input_tokens: number;
  document_anchor_hard_input_tokens: number;
  document_anchor_max_candidates: number;
  reusable_run_id: string | null;
  provider_ready: boolean;
}

/** Preview Run 的终端或运行中状态。 */
export type PreviewRunStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

/** 一个通道窗口对候选为何被提取的简短、可追溯说明。 */
export interface CandidateSelectionReason {
  channel_id: string;
  operation: "discovery" | "omission_retry";
  window_index: number;
  reason: string;
  source_block_ids: string[];
}

/** 一个候选的确定性或 LLM 审查疑点。 */
export interface CandidateAuditFinding {
  code: string;
  message: string;
  alias: string | null;
  conflicting_candidate_id: string | null;
  evidence_block_ids: string[];
}

/** 文档级 Profile 比较候选及其直接证据。 */
export interface PreviewCandidate {
  candidate_id: string;
  doc_id: string;
  doc_version: number;
  ordinal: number;
  name: string;
  normalized_name: string;
  provisional_name: string | null;
  title_kind:
    | "proper_name"
    | "acronym"
    | "common_concept"
    | "relation"
    | "principle"
    | "method"
    | "other"
    | null;
  title_standalone_clear: boolean | null;
  missing_anchor_types: Array<"actor" | "system" | "product" | "scope">;
  name_changed: boolean;
  aliases: string[];
  summaries: string[];
  selection_reasons?: CandidateSelectionReason[];
  name_anchor_source_block_ids: string[];
  name_anchor_reason: string | null;
  audit_status: "not_requested" | "clean" | "suspicious" | "uncertain";
  audit_reason: string | null;
  audit_findings: CandidateAuditFinding[];
  source_block_ids: string[];
  evidence_blocks: Array<{
    block_id: string;
    section_path: string[];
    text: string;
  }>;
  channel_ids: string[];
  content_hash: string;
}

/** 单个证据块的中文翻译及缓存复用状态。 */
export interface EvidenceTranslationItem {
  block_id: string;
  translated_text: string;
  reused: boolean;
}

/** 一次证据批量翻译结果。 */
export interface EvidenceTranslationResponse {
  items: EvidenceTranslationItem[];
  translated_count: number;
  reused_count: number;
  request_ids: string[];
}

/** Run 创建时冻结的通道判断标准。 */
export interface PreviewChannelDefinition {
  name: string;
  activation: string;
  include_criteria: string[];
  exclude_criteria: string[];
}

/** 一个文档的整篇上下文名称锚定状态。 */
export interface PreviewDocumentAnchorResult {
  status:
    | "not_needed"
    | "completed"
    | "over_budget"
    | "candidate_limit_exceeded"
    | "truncated_output"
    | "invalid_json"
    | "invalid_schema"
    | "failed";
  candidate_count: number;
  estimated_input_tokens: number;
  output_token_budget: number;
  soft_input_tokens: number;
  hard_input_tokens: number;
  error: string | null;
  request_id: string | null;
  actual_input_tokens: number;
  actual_output_tokens: number;
  finish_reason: string | null;
  returned_chars: number;
}

/** Preview Run 的可追溯状态摘要。 */
export interface PreviewRunSummary {
  run_id: string;
  snapshot_id: string;
  snapshot_manifest_hash: string;
  profile_id: string;
  profile_hash: string;
  profile_name: string;
  channel_names: Record<string, string>;
  channel_definitions: Record<string, PreviewChannelDefinition>;
  semantic_audit_enabled: boolean;
  semantic_audit_prompt_version: string | null;
  name_scope_audit_prompt_version: string | null;
  name_meaning_audit_prompt_version: string | null;
  document_anchor_prompt_version: string | null;
  document_anchor_policy_version: string | null;
  document_anchor_results: Record<string, PreviewDocumentAnchorResult>;
  semantic_audit_status:
    | "disabled"
    | "pending"
    | "running"
    | "completed"
    | "partial"
    | "failed"
    | "interrupted";
  semantic_audit_error: string | null;
  document_scope: string[];
  status: PreviewRunStatus;
  preview_fingerprint: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  error_code: string | null;
  error_message: string | null;
  usage: Array<Record<string, unknown>>;
  diagnostics: Array<Record<string, unknown>>;
}

/** Preview Run 摘要及第一页正式候选。 */
export interface PreviewRunDetail extends PreviewRunSummary {
  candidates: PreviewCandidate[];
  candidates_total: number;
  reused: boolean;
}

/** 文档清单项的重要度或独立噪声类型。 */
export type ChecklistPriority = "must" | "should" | "nice_to_have" | "noise";

/** 一个绑定文档 evidence 的期望项或噪声项。 */
export interface DocumentChecklistItem {
  item_id: string;
  name: string;
  aliases: string[];
  priority: ChecklistPriority;
  source_block_ids: string[];
  origin: "system_suggested" | "from_preview" | "manual";
  origin_profile_id: string | null;
  origin_preview_run_id: string | null;
}

/** 绑定文档版本和 snapshot 的可编辑或冻结清单。 */
export interface DocumentChecklist {
  checklist_id: string;
  snapshot_id: string;
  doc_id: string;
  doc_version: number;
  status: "draft" | "frozen";
  evaluation_note: string;
  items: DocumentChecklistItem[];
  checklist_hash: string;
  created_at: string;
  updated_at: string;
  frozen_at: string | null;
}

/** 用户对两个 Profile 作出的显式比较结论。 */
export type ComparisonOutcome =
  | "a_better"
  | "b_better"
  | "equivalent"
  | "both_insufficient";

/** 单一优先级的命中、缺失和不确定数量。 */
export interface ComparisonPriorityCounts {
  matched: number;
  missing: number;
  uncertain: number;
}

/** 一个 Profile 在一篇文档上的逐优先级比较指标。 */
export interface ComparisonProfileMetrics {
  must: ComparisonPriorityCounts;
  should: ComparisonPriorityCounts;
  nice_to_have: ComparisonPriorityCounts;
  noise_hits: number;
  extra_candidate_count: number;
}

/** 一个清单项在某一 Profile 侧的匹配结果。 */
export interface ComparisonItemSide {
  status: "matched" | "missing" | "uncertain_match";
  candidate_ids: string[];
  evidence_block_ids: string[];
  user_override: Record<string, unknown> | null;
}

/** 一个清单项在 A/B 两侧的临时比较结果。 */
export interface ComparisonItemResult {
  item_id: string;
  name: string;
  priority: ChecklistPriority;
  profile_a: ComparisonItemSide;
  profile_b: ComparisonItemSide;
}

/** 一篇文档的完整 Profile 比较结果。 */
export interface ComparisonDocumentResult {
  doc_id: string;
  doc_version: number;
  checklist_id: string;
  checklist_hash: string;
  profile_a: ComparisonProfileMetrics;
  profile_b: ComparisonProfileMetrics;
  item_results: ComparisonItemResult[];
  profile_a_extra_candidate_ids: string[];
  profile_b_extra_candidate_ids: string[];
  user_outcome: ComparisonOutcome | null;
  must_override_reason: string | null;
  decisions: Array<Record<string, unknown>>;
}

/** 一个不可变的多文档 Profile 比较记录。 */
export interface ProfileComparison {
  comparison_id: string;
  snapshot_id: string;
  run_a_id: string;
  run_b_id: string;
  profile_a_id: string;
  profile_b_id: string;
  checklist_refs: Array<Record<string, string>>;
  status: "draft" | "concluded";
  document_results: ComparisonDocumentResult[];
  outcome: ComparisonOutcome | null;
  outcome_note: string;
  comparison_hash: string;
  created_at: string;
  completed_at: string | null;
}

/** 当前 collection snapshot 中可供预览的文档摘要。 */
export interface CollectionDocument {
  doc_id: string;
  doc_version: number;
  title: string;
  source_label: string;
  block_count: number;
}

/** 当前活动 collection 的 snapshot 和文档列表。 */
export interface CurrentCollectionResponse {
  manifest: {
    snapshot_id: string;
    documents: CollectionDocument[];
  };
}

/** 文档详情接口返回值中的一个原文证据块。 */
export interface DocumentDetailBlock {
  block_id: string;
  text: string;
  section_path: string[];
}

/** 当前活动快照中一篇文档的详情最小子集。 */
export interface DocumentDetailResponse {
  snapshot_id: string;
  doc_id: string;
  doc_version: number;
  title: string;
  blocks: DocumentDetailBlock[];
}

/** 生成与后端正则兼容的 16 位十六进制清单项 ID。 */
export function newChecklistItemId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(8));
  return `dci_${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
}

/** 生成与后端正则兼容的 16 位十六进制通道 ID。 */
export function newChannelId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(8));
  return `ec_${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
}

/** 访问 Profile 抽取相关本地 API 的轻量客户端。 */
export class ProfileExtractionClient {
  private readonly baseUrl: string;
  private readonly token: string;

  constructor(baseUrl: string, token: string) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.token = token;
  }

  async currentCollection(): Promise<CurrentCollectionResponse | null> {
    return this.request("/api/collections/current");
  }

  async documentDetail(docId: string): Promise<DocumentDetailResponse> {
    return this.request(`/api/collections/current/documents/${docId}`);
  }

  async listProfiles(): Promise<ProfileListResponse> {
    return this.request("/api/extraction-profiles");
  }

  async selection(): Promise<ProfileSelection> {
    return this.request("/api/extraction-profile-selection");
  }

  async createProfile(name: string): Promise<ExtractionProfile> {
    return this.request("/api/extraction-profiles", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
  }

  async cloneProfile(profileId: string, name?: string): Promise<ExtractionProfile> {
    return this.request(`/api/extraction-profiles/${profileId}/clone`, {
      method: "POST",
      body: JSON.stringify({ name: name ?? null }),
    });
  }

  async saveProfile(
    profileId: string,
    config: ExtractionProfileConfig,
  ): Promise<ExtractionProfile> {
    return this.request(`/api/extraction-profiles/${profileId}`, {
      method: "PUT",
      body: JSON.stringify({ config }),
    });
  }

  async deleteProfile(profileId: string): Promise<void> {
    await this.request(`/api/extraction-profiles/${profileId}`, { method: "DELETE" });
  }

  async validateProfile(profileId: string): Promise<ProfileValidationResponse> {
    return this.request(`/api/extraction-profiles/${profileId}/validate`, {
      method: "POST",
      body: "{}",
    });
  }

  async selectProfile(profileId: string): Promise<ProfileSelection> {
    return this.request(`/api/extraction-profiles/${profileId}/select`, {
      method: "POST",
      body: "{}",
    });
  }

  async compiledPrompts(profileId: string): Promise<Array<Record<string, string>>> {
    return this.request(`/api/extraction-profiles/${profileId}/compiled-prompts`);
  }

  async suggestStrategy(
    profileName: string,
    primaryUses: string[],
    representativeQuestions: string[],
    domainContext: string,
  ): Promise<{ channels: ExtractionChannel[]; exclude_criteria: string[]; request_id: string }> {
    return this.request(
      "/api/extraction-profiles/strategy-suggestions",
      {
        method: "POST",
        body: JSON.stringify({
          profile_name: profileName,
          primary_uses: primaryUses,
          representative_questions: representativeQuestions,
          domain_context: domainContext,
        }),
      },
      API_TIMEOUT_MS.sourcePreview,
    );
  }

  async suggestChannelAttributes(
    profileName: string,
    primaryUses: string[],
    representativeQuestions: string[],
    domainContext: string,
    channelName: string,
    channelPurpose: string,
  ): Promise<ChannelAttributeSuggestion> {
    return this.request(
      "/api/extraction-profiles/channel-attribute-suggestions",
      {
        method: "POST",
        body: JSON.stringify({
          profile_name: profileName,
          primary_uses: primaryUses,
          representative_questions: representativeQuestions,
          domain_context: domainContext,
          channel_name: channelName,
          channel_purpose: channelPurpose,
        }),
      },
      API_TIMEOUT_MS.sourcePreview,
    );
  }

  async planPreview(
    profileId: string,
    snapshotId: string,
    documentIds: string[],
    semanticAuditEnabled: boolean,
  ): Promise<PreviewRunPlan> {
    return this.request("/api/extraction-preview-runs/plan", {
      method: "POST",
      body: JSON.stringify({
        profile_id: profileId,
        snapshot_id: snapshotId,
        document_ids: documentIds,
        execution_mode: "reuse_if_exact",
        semantic_audit_enabled: semanticAuditEnabled,
      }),
    });
  }

  async createPreview(
    profileId: string,
    snapshotId: string,
    documentIds: string[],
    forceNew: boolean,
    semanticAuditEnabled: boolean,
  ): Promise<PreviewRunDetail> {
    return this.request("/api/extraction-preview-runs", {
      method: "POST",
      body: JSON.stringify({
        profile_id: profileId,
        snapshot_id: snapshotId,
        document_ids: documentIds,
        execution_mode: forceNew ? "force_new" : "reuse_if_exact",
        semantic_audit_enabled: semanticAuditEnabled,
      }),
    });
  }

  async listPreviewRuns(): Promise<PreviewRunSummary[]> {
    return this.request("/api/extraction-preview-runs");
  }

  async getPreviewRun(runId: string): Promise<PreviewRunDetail> {
    return this.request(`/api/extraction-preview-runs/${runId}`);
  }

  async previewCandidates(
    runId: string,
  ): Promise<{ candidates: PreviewCandidate[]; total: number; offset: number; limit: number }> {
    return this.request(`/api/extraction-preview-runs/${runId}/candidates?offset=0&limit=500`);
  }

  async translateEvidence(
    runId: string,
    blockIds: string[],
  ): Promise<EvidenceTranslationResponse> {
    return this.request(
      `/api/extraction-preview-runs/${runId}/translate-evidence`,
      {
        method: "POST",
        body: JSON.stringify({ block_ids: blockIds }),
      },
      API_TIMEOUT_MS.sourcePreview,
    );
  }

  async cancelPreview(runId: string): Promise<PreviewRunSummary> {
    return this.request(`/api/extraction-preview-runs/${runId}/cancel`, {
      method: "POST",
      body: "{}",
    });
  }

  async deletePreview(runId: string): Promise<void> {
    await this.request(`/api/extraction-preview-runs/${runId}`, { method: "DELETE" });
  }

  async listChecklists(): Promise<DocumentChecklist[]> {
    return this.request("/api/document-checklists");
  }

  async checklistBlocks(
    snapshotId: string,
    docId: string,
  ): Promise<DocumentDetailBlock[]> {
    return this.request(
      `/api/document-checklists/blocks?snapshot_id=${encodeURIComponent(snapshotId)}&doc_id=${encodeURIComponent(docId)}`,
    );
  }

  async createChecklist(
    snapshotId: string,
    docId: string,
    evaluationNote: string,
  ): Promise<DocumentChecklist> {
    return this.request("/api/document-checklists", {
      method: "POST",
      body: JSON.stringify({
        snapshot_id: snapshotId,
        doc_id: docId,
        evaluation_note: evaluationNote,
      }),
    });
  }

  async saveChecklist(
    checklistId: string,
    evaluationNote: string,
    items: DocumentChecklistItem[],
  ): Promise<DocumentChecklist> {
    return this.request(`/api/document-checklists/${checklistId}`, {
      method: "PUT",
      body: JSON.stringify({ evaluation_note: evaluationNote, items }),
    });
  }

  async freezeChecklist(checklistId: string): Promise<DocumentChecklist> {
    return this.request(`/api/document-checklists/${checklistId}/freeze`, {
      method: "POST",
      body: "{}",
    });
  }

  async suggestChecklist(
    snapshotId: string,
    docId: string,
    evaluationNote: string,
  ): Promise<{ items: Array<Omit<DocumentChecklistItem, "item_id" | "origin" | "origin_profile_id" | "origin_preview_run_id">>; request_ids: string[] }> {
    return this.request(
      "/api/document-checklists/suggest",
      {
        method: "POST",
        body: JSON.stringify({
          snapshot_id: snapshotId,
          doc_id: docId,
          evaluation_note: evaluationNote,
        }),
      },
      API_TIMEOUT_MS.sourcePreview,
    );
  }

  async deleteChecklist(checklistId: string): Promise<void> {
    await this.request(`/api/document-checklists/${checklistId}`, { method: "DELETE" });
  }

  async createComparison(
    runAId: string,
    runBId: string,
    checklistIds: string[],
  ): Promise<ProfileComparison> {
    return this.request("/api/profile-comparisons", {
      method: "POST",
      body: JSON.stringify({
        run_a_id: runAId,
        run_b_id: runBId,
        checklist_ids: checklistIds,
      }),
    });
  }

  async listComparisons(): Promise<ProfileComparison[]> {
    return this.request("/api/profile-comparisons");
  }

  async applyComparisonDecision(
    comparisonId: string,
    itemId: string,
    profileSide: "a" | "b",
    decision: "matched" | "missing",
    candidateId: string | null,
  ): Promise<ProfileComparison> {
    return this.request(`/api/profile-comparisons/${comparisonId}/item-decisions`, {
      method: "POST",
      body: JSON.stringify({
        decisions: [
          {
            item_id: itemId,
            profile_side: profileSide,
            decision,
            candidate_id: candidateId,
            reason: "",
          },
        ],
      }),
    });
  }

  async concludeComparison(
    comparisonId: string,
    outcome: ComparisonOutcome,
    outcomeNote: string,
    mustOverrideReasons: Record<string, string>,
    documentOutcomes: Record<string, ComparisonOutcome>,
  ): Promise<ProfileComparison> {
    return this.request(`/api/profile-comparisons/${comparisonId}/conclude`, {
      method: "POST",
      body: JSON.stringify({
        outcome,
        outcome_note: outcomeNote,
        must_override_reasons: mustOverrideReasons,
        document_outcomes: documentOutcomes,
      }),
    });
  }

  private async request<T>(
    path: string,
    init: RequestInit = {},
    timeoutMs: number = API_TIMEOUT_MS.quick,
  ): Promise<T> {
    const response = await fetchWithTimeout(
      `${this.baseUrl}${path}`,
      {
        ...init,
        headers: {
          "content-type": "application/json",
          "x-launch-token": this.token,
          ...(init.headers ?? {}),
        },
      },
      timeoutMs,
    );
    if (!response.ok) {
      let message = `请求失败（HTTP ${response.status}）`;
      try {
        const body = (await response.json()) as {
          detail?: string;
          error?: { message?: string };
        };
        message = body.error?.message ?? body.detail ?? message;
      } catch {
        // 保留基于状态码的可操作错误。
      }
      throw new Error(message);
    }
    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }
}
