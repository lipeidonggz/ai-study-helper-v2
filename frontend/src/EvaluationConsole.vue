<script setup lang="ts">
import { invoke } from "@tauri-apps/api/core";
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { fetchWithTimeout } from "./api";
import TraceArtifactContentViewer from "./TraceArtifactContentViewer.vue";
import TraceDetailViewer from "./TraceDetailViewer.vue";
import type { TraceArtifact, TraceDetail } from "./trace";

/** 评测 sidecar 的独立回环地址与启动令牌。 */
interface EvaluationConnection {
  baseUrl: string;
  token: string;
}

/** 设计 03 中的一条有序 R／X 判定项。 */
interface CriterionView {
  criterion_id: string;
  revision_id: string;
  rule_type: "R" | "X";
  ordinal: number;
  title: string;
  rule_text: string;
  evaluator: string;
  evaluator_ref: string | null;
  change_note: string;
}

/** 一个不可变用例 revision 及其判定项。 */
interface CaseRevisionView {
  case_id: string;
  revision_id: string;
  revision_number: number;
  category: string;
  question: string;
  target_bindings: Array<{ title_contains?: string; section_path?: string[] }>;
  distractor_metadata: Record<string, unknown>;
  n: number;
  k: number;
  execution_policy: Record<string, unknown>;
  review_status: string;
  reviewer: string | null;
  source_ref: string;
  content_hash: string;
  change_note: string;
  criteria: CriterionView[];
}

/** 一条可编辑的草稿判定项。 */
interface CaseDraftCriterionView {
  criterion_id: string;
  base_revision_id: string | null;
  rule_type: "R" | "X";
  ordinal: number;
  title: string;
  rule_text: string;
  evaluator: "checker" | "llm_judge" | "human";
  evaluator_ref: string | null;
}

/** 可反复编辑的用例草稿。 */
interface CaseDraftView {
  case_id: string;
  base_revision_id: string | null;
  next_revision_number: number;
  category: string;
  question: string;
  target_bindings: Array<{ title_contains?: string; section_path?: string[] }>;
  distractor_metadata: Record<string, unknown>;
  n: number;
  k: number;
  execution_policy: Record<string, unknown>;
  change_note: string;
  criteria: CaseDraftCriterionView[];
  updated_by: string;
  updated_at: string;
}

/** 一个字段或判定项在草稿与基准修订版之间的差异。 */
interface CaseDraftDiffItem {
  label: string;
  kind: "field" | "added" | "removed" | "changed";
  before?: string;
  after?: string;
}

/** 用例库中的稳定身份、当前草稿和历史修订版。 */
interface CaseCatalogItem {
  case_id: string;
  active_revision_id: string | null;
  latest_revision_id: string | null;
  has_draft: boolean;
  revisions: CaseRevisionView[];
}

/** 可选择的 suite revision 和固定 case revisions。 */
interface SuiteRevisionView {
  suite_id: string;
  revision_id: string;
  revision_number: number;
  name: string;
  description: string;
  scope_id: string;
  review_status: string;
  change_note: string;
  stale: boolean;
  stale_reasons: string[];
  case_count: number;
  cases: CaseRevisionView[];
}

/** 可编辑的 suite 草稿与精确成员。 */
interface SuiteDraftView {
  suite_id: string;
  base_revision_id: string | null;
  next_revision_number: number;
  name: string;
  description: string;
  scope_id: string;
  change_note: string;
  cases: CaseRevisionView[];
  updated_by: string;
  updated_at: string;
}

/** 产品知识库当前活动快照的只读投影。 */
interface CurrentCollection {
  pointer: { snapshot_id: string };
  manifest: {
    snapshot_id: string;
    completeness: string;
    documents: Array<{ doc_id: string; title: string }>;
  };
}

/** 预检中一个可操作的通过或失败项。 */
interface PreflightCheck {
  code: string;
  status: "passed" | "failed";
  message: string;
}

/** 已将 suite、snapshot、版本和预算冻结的执行计划。 */
interface ExecutionPlanView {
  plan_id: string;
  status: "planned" | "invalid_plan";
  suite_revision_id: string | null;
  collection_snapshot_id: string;
  run_type: "quality" | "latency";
  mode: "formal" | "exploratory";
  concurrency: number;
  checks: PreflightCheck[];
  cases: Array<{
    case_id: string;
    case_revision_id: string;
    n: number;
    k: number;
    source_n: number;
    source_k: number;
    resolved_targets: Array<{ doc_id: string; title: string }>;
  }>;
  run_policy: Record<string, unknown>;
}

/** 一次全新 Agent 会话的持久化结果。 */
interface AttemptView {
  attempt_id: string;
  case_revision_id: string;
  replacement_for_attempt_id: string | null;
  trace_id: string | null;
  trace_status: "complete" | "partial" | "dropped" | "missing";
  execution_status: string;
  outcome_class: "product" | "infrastructure" | "canceled" | "unknown";
  termination_reason: string | null;
  error_code: string | null;
  answer: string | null;
  input_tokens: number;
  output_tokens: number;
  model_calls: number;
  tool_calls: number;
  started_at: string;
  first_token_at: string | null;
  finished_at: string | null;
}

/** 一个可恢复 ExecutionRun 的状态与 attempts。 */
interface RunView {
  run_id: string;
  plan_id: string;
  plan_mode: "formal" | "exploratory";
  run_type: "quality" | "latency";
  suite_id: string | null;
  suite_name: string | null;
  suite_revision_id: string | null;
  suite_revision_number: number | null;
  status: string;
  pause_reason: string | null;
  created_at: string;
  started_at: string | null;
  sealed_at: string | null;
  updated_at: string;
  totals: Record<string, number>;
  attempts: AttemptView[];
}

/** 一次 Judge provider 调用及 usage。 */
interface JudgeInvocationView {
  invocation_id: string;
  attempt_number: number;
  prompt_version: string;
  model_id: string;
  status: string;
  error_code: string | null;
  input_tokens: number;
  output_tokens: number;
  request_id: string | null;
  input_json: string | null;
  raw_output: string | null;
  started_at: string;
  finished_at: string | null;
}

/** 一条 attempt×criterion 判定及 invocation 历史。 */
interface JudgmentView {
  judgment_id: string;
  attempt_id: string;
  criterion_revision_id: string;
  executor: string;
  executor_ref: string | null;
  verdict: string;
  normalized_result: "pass" | "fail" | "pending";
  reason: string | null;
  evidence: {
    block_ids?: string[];
    quotes?: string[];
  };
  error_code: string | null;
  created_at: string;
  invocations: JudgeInvocationView[];
}

/** 一条 Judgment 的 append-only 生效绑定。 */
interface JudgmentBindingView {
  binding_id: string;
  attempt_id: string;
  criterion_revision_id: string;
  judgment_id: string;
  executor: string;
  verdict: string;
  normalized_result: "pass" | "fail" | "pending";
  is_effective: boolean;
  bound_at: string;
}

/** 按 R／X 分组的 Judgment 及界面说明。 */
interface JudgmentGroup {
  ruleType: "R" | "X";
  title: string;
  description: string;
  items: JudgmentView[];
}

/** 一个 attempt 下用于诊断的 Judgment 集合。 */
interface AttemptJudgmentGroup {
  attemptId: string;
  caseId: string;
  caseRevisionId: string;
  sampleLabel: string;
  result: AttemptAggregateView["result"];
  aggregate: AttemptAggregateView | undefined;
  judgments: JudgmentView[];
  answer: string | null;
  traceId: string | null;
  traceStatus: AttemptView["trace_status"];
}

/** 当前打开的人工 LLM Judgment 改判目标。 */
interface JudgeOverrideTarget {
  attemptId: string;
  criterionRevisionId: string;
  ruleType: "R" | "X";
}

/** 逐 block 引用支持人工复核草稿。 */
interface CitationReviewDraft {
  blockId: string;
  verdict: "supported" | "unsupported" | "unknown";
  reason: string;
}

/** 当前打开的 LLM CheckResult 人工复核目标。 */
interface CheckReviewTarget {
  check: CheckResultView;
  items: CitationReviewDraft[];
}

/** 一条通用跨判定项确定性检查结果。 */
interface CheckResultView {
  check_result_id: string;
  attempt_id: string;
  checker_id: string;
  checker_version: string;
  result: "pass" | "fail" | "pending";
  payload: Record<string, unknown>;
  error_code: string | null;
  created_at: string;
}

/** 一条人工追加的 LLM CheckResult 复核。 */
interface CheckReviewView {
  review_id: string;
  attempt_id: string;
  checker_id: string;
  source_check_result_id: string;
  result: "pass" | "fail" | "pending";
  payload: Record<string, unknown>;
  error_code: string | null;
  reason: string;
  reviewer: string;
  created_at: string;
}

/** 单次 attempt 的聚合结果。 */
interface AttemptAggregateView {
  attempt_id: string;
  case_id: string;
  case_revision_id: string;
  valid_sample: boolean;
  result: "pass" | "fail" | "pending" | "excluded";
  r_pass: number;
  r_fail: number;
  r_pending: number;
  x_not_violated: number;
  x_violated: number;
  x_pending: number;
  missing_binding_count: number;
  check_failures: string[];
  check_pending: string[];
  reasons: string[];
}

/** 一个 case 在冻结 N/K 下的聚合结果。 */
interface CaseAggregateView {
  case_id: string;
  case_revision_id: string;
  n: number;
  k: number;
  valid_samples: number;
  pass_count: number;
  fail_count: number;
  pending_count: number;
  x_violation_count: number;
  check_failure_count: number;
  redline_violation_count: number;
  result: "pass" | "fail" | "pending" | "exploratory";
  reasons: string[];
}

/** 结果矩阵中的一次执行样本及其成本和证据入口。 */
interface ResultMatrixAttemptView {
  attempt_id: string;
  sample_ordinal: number;
  execution_status: string;
  outcome_class: AttemptView["outcome_class"];
  valid_sample: boolean;
  result: AttemptAggregateView["result"];
  redline_violated: boolean;
  x_violated: number;
  check_failures: string[];
  trace_id: string | null;
  trace_status: AttemptView["trace_status"];
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  input_tokens: number;
  output_tokens: number;
  model_calls: number;
  tool_calls: number;
  reasons: string[];
}

/** 结果矩阵中的一个 case 及其 attempt 明细。 */
interface ResultMatrixCaseView {
  case_id: string;
  case_revision_id: string;
  category: string;
  question: string;
  n: number;
  k: number;
  attempt_count: number;
  valid_samples: number;
  excluded_count: number;
  pass_count: number;
  fail_count: number;
  pending_count: number;
  x_violation_count: number;
  check_failure_count: number;
  redline_violation_count: number;
  result: CaseAggregateView["result"];
  input_tokens: number;
  output_tokens: number;
  model_calls: number;
  tool_calls: number;
  duration_ms: number;
  reasons: string[];
  attempts: ResultMatrixAttemptView[];
}

/** 结果矩阵中类别或整个 run 的汇总。 */
interface ResultMatrixTotalsView {
  case_count: number;
  case_pass_count: number;
  case_fail_count: number;
  case_pending_count: number;
  case_exploratory_count: number;
  attempt_count: number;
  valid_samples: number;
  excluded_attempts: number;
  infrastructure_failures: number;
  canceled_attempts: number;
  unknown_attempts: number;
  x_violation_count: number;
  check_failure_count: number;
  redline_violation_count: number;
  input_tokens: number;
  output_tokens: number;
  model_calls: number;
  tool_calls: number;
  duration_ms: number;
}

/** 按冻结 category 分组的结果矩阵。 */
interface ResultMatrixCategoryView {
  category: string;
  totals: ResultMatrixTotalsView;
  cases: ResultMatrixCaseView[];
}

/** 从执行与判定事实重建的四层只读矩阵。 */
interface EvaluationResultMatrixView {
  run_id: string;
  run_status: RunView["status"];
  plan_mode: RunView["plan_mode"];
  suite_name: string | null;
  suite_revision_id: string | null;
  suite_revision_number: number | null;
  totals: ResultMatrixTotalsView;
  categories: ResultMatrixCategoryView[];
}

/** 一个人工确认且冻结结果投影的评测基线。 */
interface BaselineView {
  baseline_id: string;
  suite_id: string;
  suite_name: string;
  scope_id: string;
  run_type: "quality" | "latency";
  suite_revision_id: string;
  collection_snapshot_id: string;
  execution_run_id: string;
  evaluation_revision_id: string;
  case_revision_ids: string[];
  aggregate_rule_version: string;
  totals: ResultMatrixTotalsView;
  run_started_at: string | null;
  run_sealed_at: string | null;
  reviewer: string;
  reason: string;
  created_at: string;
  is_current: boolean;
}

/** 一个 case 在基线与目标 run 之间的差异。 */
interface BaselineCaseDeltaView {
  case_id: string;
  category: string;
  baseline_case_revision_id: string | null;
  target_case_revision_id: string | null;
  baseline_result: "pass" | "fail" | "pending" | "exploratory" | null;
  target_result: "pass" | "fail" | "pending" | "exploratory" | null;
  change: "new" | "removed" | "improved" | "regressed" | "changed" | "unchanged";
  revision_changed: boolean;
  valid_samples_delta: number;
  pass_count_delta: number;
  fail_count_delta: number;
  pending_count_delta: number;
  x_violation_count_delta: number;
  check_failure_count_delta: number;
  redline_violation_count_delta: number;
  input_tokens_delta: number;
  output_tokens_delta: number;
  model_calls_delta: number;
  tool_calls_delta: number;
  duration_ms_delta: number;
}

/** 目标 run 相对基线的 run 级差异。 */
interface BaselineComparisonSummaryView {
  case_pass_count_delta: number;
  case_fail_count_delta: number;
  case_pending_count_delta: number;
  case_exploratory_count_delta: number;
  valid_samples_delta: number;
  x_violation_count_delta: number;
  check_failure_count_delta: number;
  redline_violation_count_delta: number;
  input_tokens_delta: number;
  output_tokens_delta: number;
  model_calls_delta: number;
  tool_calls_delta: number;
  duration_ms_delta: number;
  newly_passing_count: number;
  newly_failing_count: number;
}

/** 一个完整 run 与当前基线的比较投影。 */
interface BaselineComparisonView {
  baseline: BaselineView;
  target_run_id: string;
  target_evaluation_revision_id: string;
  target_suite_revision_id: string;
  target_collection_snapshot_id: string;
  target_totals: ResultMatrixTotalsView;
  comparable: boolean;
  incompatibility_reasons: string[];
  summary: BaselineComparisonSummaryView;
  cases: BaselineCaseDeltaView[];
}

/** 一次判定修订版及其逐项结果。 */
interface EvaluationRevisionView {
  revision_id: string;
  execution_run_id: string;
  status: "running" | "complete" | "incomplete" | "canceled";
  checker_version_set: Record<string, unknown>;
  judge_version_set: Record<string, unknown>;
  config: Record<string, unknown>;
  summary: Record<string, number>;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  judgments: JudgmentView[];
  judgment_bindings: JudgmentBindingView[];
  effective_bindings: JudgmentBindingView[];
  checks: CheckResultView[];
  check_reviews: CheckReviewView[];
  attempt_aggregates: AttemptAggregateView[];
  case_aggregates: CaseAggregateView[];
  result_matrix: EvaluationResultMatrixView | null;
}

/** 判官 prompt 原文与版本。 */
interface JudgePromptView {
  version: string;
  content_hash: string;
  content: string;
}

/** 评测 API 的稳定错误响应。 */
interface ApiErrorBody {
  error?: { code: string; message: string };
  detail?: unknown;
}

const connection = ref<EvaluationConnection | null>(null);
const evaluationTab = ref<"cases" | "suites" | "execute" | "history">("cases");
const suites = ref<SuiteRevisionView[]>([]);
const caseCatalog = ref<CaseCatalogItem[]>([]);
const selectedCaseId = ref("");
const selectedCaseRevisionId = ref("");
const selectedSuiteRevisionId = ref("");
const snapshotId = ref("");
const currentCollection = ref<CurrentCollection | null>(null);
const caseDraft = ref<CaseDraftView | null>(null);
const caseDraftDirty = ref(false);
const caseDraftSaveState = ref("");
const caseDraftSaveError = ref("");
const caseTargetTitles = ref("");
const caseDistractorMetadata = ref("{}");
const caseExecutionPolicy = ref('{"latency_class":"simple"}');
const suiteDraft = ref<SuiteDraftView | null>(null);
const suiteDraftCaseIds = ref<string[]>([]);
const reviewerName = ref("开发者");
const executionMode = ref<"formal" | "exploratory">("formal");
const quickCaseRevisionIds = ref<string[]>([]);
const quickN = ref(1);
const runType = ref<"quality" | "latency">("quality");
const concurrency = ref(8);
const totalTokenBudget = ref(5_000_000);
const modelRequestBudget = ref(2_000);
const replacementAttemptBudget = ref(20);
const attemptTimeoutSeconds = ref(600);
const plan = ref<ExecutionPlanView | null>(null);
const runs = ref<RunView[]>([]);
const historySuiteFilter = ref("all");
const selectedRun = ref<RunView | null>(null);
const evaluationRevisions = ref<EvaluationRevisionView[]>([]);
const evaluationRevision = ref<EvaluationRevisionView | null>(null);
const isJudgmentFormOpen = ref(false);
const judgePrompt = ref<JudgePromptView | null>(null);
const citationPrompt = ref<JudgePromptView | null>(null);
const judgeReviewConfirmed = ref(false);
const judgeConcurrency = ref(2);
const judgeTokenBudget = ref(500_000);
const judgeRequestBudget = ref(500);
const judgeTimeoutSeconds = ref(60);
const judgingBusy = ref(false);
const judgingError = ref("");
const judgmentViewMode = ref<"attempt" | "criterion">("attempt");
const judgmentAttemptFilter = ref<"all" | "failed">("all");
const focusedAttemptId = ref("");
const judgeOverrideTarget = ref<JudgeOverrideTarget | null>(null);
const judgeOverrideVerdict = ref("");
const judgeOverrideReason = ref("");
const judgeOverrideReviewer = ref("");
const judgeOverrideBusy = ref(false);
const judgeOverrideError = ref("");
const checkReviewTarget = ref<CheckReviewTarget | null>(null);
const checkReviewReason = ref("");
const checkReviewReviewer = ref("");
const checkReviewBusy = ref(false);
const checkReviewError = ref("");
const traceDetail = ref<TraceDetail | null>(null);
const matrixJudgmentAttemptId = ref("");
const matrixTraceAttempt = ref<ResultMatrixAttemptView | null>(null);
const matrixTraceDetail = ref<TraceDetail | null>(null);
const matrixTraceLoading = ref(false);
const matrixTraceError = ref("");
const baselineHistory = ref<BaselineView[]>([]);
const currentBaseline = ref<BaselineView | null>(null);
const baselineComparison = ref<BaselineComparisonView | null>(null);
const baselineLoading = ref(false);
const baselineError = ref("");
const isBaselineFormOpen = ref(false);
const baselineReason = ref("");
const baselineBusy = ref(false);
const historyDetailTab = ref<"matrix" | "baseline" | "judgment" | "trace">("matrix");
const loading = ref(true);
const busy = ref(false);
const error = ref("");
const message = ref("");
let pollTimer: number | undefined;
let judgmentPollTimer: number | undefined;
let matrixTraceRequestSequence = 0;

const selectedSuite = computed(
  () =>
    suites.value.find(
      (suite) => suite.revision_id === selectedSuiteRevisionId.value,
    ) ?? null,
);

const selectableFormalSuites = computed(() =>
  suites.value.filter(
    (suite) => suite.review_status === "active" && !suite.stale,
  ),
);

const selectedCaseCatalog = computed(
  () =>
    caseCatalog.value.find((item) => item.case_id === selectedCaseId.value) ??
    null,
);

const selectedCaseRevision = computed(() => {
  const catalog = selectedCaseCatalog.value;
  if (!catalog) {
    return null;
  }
  return (
    catalog.revisions.find(
      (revision) => revision.revision_id === selectedCaseRevisionId.value,
    ) ??
    catalog.revisions.find((revision) => revision.review_status === "active") ??
    catalog.revisions[0] ??
    null
  );
});

const caseDraftBaseRevision = computed(() => {
  if (!caseDraft.value?.base_revision_id) {
    return null;
  }
  return (
    selectedCaseCatalog.value?.revisions.find(
      (revision) => revision.revision_id === caseDraft.value?.base_revision_id,
    ) ?? null
  );
});

const caseDraftDiff = computed<CaseDraftDiffItem[]>(() => {
  const draft = caseDraft.value;
  if (!draft) {
    return [];
  }
  const base = caseDraftBaseRevision.value;
  const differences: CaseDraftDiffItem[] = [];
  const addField = (
    label: string,
    before: unknown,
    after: unknown,
  ): void => {
    const beforeText = _diffText(before);
    const afterText = _diffText(after);
    if (beforeText !== afterText) {
      differences.push({
        label,
        kind: "field",
        before: beforeText,
        after: afterText,
      });
    }
  };
  addField("类别", base?.category, draft.category);
  addField("问题", base?.question, draft.question);
  addField("目标文档与章节", base?.target_bindings, draft.target_bindings);
  addField("干扰元数据", base?.distractor_metadata, draft.distractor_metadata);
  addField("执行策略", base?.execution_policy, draft.execution_policy);
  addField("N", base?.n, draft.n);
  addField("K", base?.k, draft.k);
  addField("升级原因／修订备注", base?.change_note, draft.change_note);

  const baseCriteria = new Map(
    (base?.criteria ?? []).map((criterion) => [
      criterion.criterion_id,
      criterion,
    ]),
  );
  const draftCriteria = new Map(
    draft.criteria.map((criterion) => [criterion.criterion_id, criterion]),
  );
  for (const criterion of draft.criteria) {
    const before = baseCriteria.get(criterion.criterion_id);
    if (!before) {
      differences.push({
        label: `${criterion.rule_type}${criterion.ordinal} ${criterion.title}`,
        kind: "added",
        after: criterion.rule_text,
      });
      continue;
    }
    const beforeText = _criterionDiffText(before);
    const afterText = _criterionDiffText(criterion);
    if (beforeText !== afterText) {
      differences.push({
        label: `${criterion.rule_type}${criterion.ordinal} ${criterion.title}`,
        kind: "changed",
        before: beforeText,
        after: afterText,
      });
    }
  }
  for (const before of base?.criteria ?? []) {
    if (!draftCriteria.has(before.criterion_id)) {
      differences.push({
        label: `${before.rule_type}${before.ordinal} ${before.title}`,
        kind: "removed",
        before: before.rule_text,
      });
    }
  }
  return differences;
});

/** 把 diff 字段值转换为稳定、可读的文本。 */
function _diffText(value: unknown): string {
  if (value === undefined || value === null || value === "") {
    return "未设置";
  }
  if (typeof value === "string") {
    return value;
  }
  return JSON.stringify(value, null, 2);
}

/** 生成判定项内容比较文本。 */
function _criterionDiffText(
  criterion: CriterionView | CaseDraftCriterionView,
): string {
  return JSON.stringify(
    {
      rule_type: criterion.rule_type,
      ordinal: criterion.ordinal,
      title: criterion.title,
      rule_text: criterion.rule_text,
      evaluator: criterion.evaluator,
      evaluator_ref: criterion.evaluator_ref,
    },
    null,
    2,
  );
}

/** 把 diff 类型转换为审核界面文字。 */
function diffKindText(kind: CaseDraftDiffItem["kind"]): string {
  const labels = {
    field: "字段修改",
    added: "新增判定项",
    removed: "删除判定项",
    changed: "修改判定项",
  };
  return labels[kind];
}

const quickCaseGroups = computed(() =>
  caseCatalog.value.flatMap((item) => {
    const revisions = item.revisions.filter((candidate) =>
      ["reviewed", "active"].includes(candidate.review_status),
    );
    return revisions.length ? [{ caseId: item.case_id, revisions }] : [];
  }),
);

const suiteActiveCaseOptions = computed(() =>
  caseCatalog.value.flatMap((item) => {
    const revision = item.revisions.find(
      (candidate) => candidate.review_status === "active",
    );
    return revision ? [revision] : [];
  }),
);

const plannedAttempts = computed(() =>
  (plan.value?.cases ?? []).reduce((total, item) => total + item.n, 0),
);

const failedChecks = computed(
  () => plan.value?.checks.filter((check) => check.status === "failed") ?? [],
);

const activeRun = computed(() =>
  ["planned", "running", "paused"].includes(selectedRun.value?.status ?? ""),
);

const selectedRunIsCurrentBaseline = computed(
  () =>
    currentBaseline.value?.execution_run_id === selectedRun.value?.run_id &&
    currentBaseline.value?.evaluation_revision_id ===
      evaluationRevision.value?.revision_id,
);

const baselineEligibility = computed<{ eligible: boolean; reason: string }>(() => {
  const run = selectedRun.value;
  const revision = evaluationRevision.value;
  if (!run || !run.suite_id) {
    return { eligible: false, reason: "当前 run 没有 suite 身份，不能设置基线。" };
  }
  if (run.plan_mode !== "formal") {
    return { eligible: false, reason: "探索性快速试跑不能成为基线。" };
  }
  if (run.status !== "sealed_complete") {
    return { eligible: false, reason: "只有完整封存的 run 可以成为基线。" };
  }
  if (!revision || revision.status !== "complete" || !revision.result_matrix) {
    return { eligible: false, reason: "判定尚未完整结束，不能成为基线。" };
  }
  const incomplete = revision.result_matrix.categories
    .flatMap((category) => category.cases)
    .find(
      (item) =>
        item.result === "pending" ||
        item.result === "exploratory" ||
        item.pending_count > 0 ||
        item.valid_samples !== item.n,
    );
  if (incomplete) {
    return {
      eligible: false,
      reason: `${incomplete.case_id} 仍缺少有效样本或待定判定，不能成为基线。`,
    };
  }
  if (selectedRunIsCurrentBaseline.value) {
    return { eligible: false, reason: "当前 run 已是这个范围的基线。" };
  }
  return { eligible: true, reason: "" };
});

const historyRuns = computed(() =>
  runs.value.filter(
    (run) => !["planned", "running", "paused"].includes(run.status),
  ),
);

const historySuiteOptions = computed(() => {
  const options = new Map<string, string>();
  for (const run of historyRuns.value) {
    if (run.suite_id) {
      options.set(run.suite_id, run.suite_name ?? run.suite_id);
    }
  }
  return [...options.entries()].map(([suiteId, name]) => ({ suiteId, name }));
});

const filteredHistoryRuns = computed(() => {
  if (historySuiteFilter.value === "all") {
    return historyRuns.value;
  }
  if (historySuiteFilter.value === "quick") {
    return historyRuns.value.filter((run) => run.plan_mode === "exploratory");
  }
  return historyRuns.value.filter(
    (run) => run.suite_id === historySuiteFilter.value,
  );
});

/** 切换评测页签；历史页不继续展示当前活动批次。 */
function selectEvaluationTab(
  tab: "cases" | "suites" | "execute" | "history",
): void {
  evaluationTab.value = tab;
  if (
    tab === "execute" &&
    selectedSuite.value &&
    !selectableFormalSuites.value.some(
      (suite) => suite.revision_id === selectedSuite.value?.revision_id,
    )
  ) {
    selectedSuiteRevisionId.value =
      selectableFormalSuites.value[0]?.revision_id ??
      selectedSuiteRevisionId.value;
  }
  if (tab === "history" && activeRun.value) {
    selectedRun.value = null;
    evaluationRevisions.value = [];
    evaluationRevision.value = null;
    isJudgmentFormOpen.value = false;
    traceDetail.value = null;
    stopPolling();
    stopJudgmentPolling();
  }
}

watch(
  [executionMode, selectableFormalSuites],
  () => {
    if (
      executionMode.value === "formal" &&
      !selectableFormalSuites.value.some(
        (suite) => suite.revision_id === selectedSuiteRevisionId.value,
      )
    ) {
      selectedSuiteRevisionId.value =
        selectableFormalSuites.value[0]?.revision_id ?? "";
      invalidatePlan();
    }
  },
  { deep: true, immediate: true },
);

/** 配置变化后作废旧预检计划，避免展示与当前表单不一致的结果。 */
function invalidatePlan(): void {
  if (plan.value !== null) {
    plan.value = null;
    message.value = "执行配置已变化，请重新生成预检计划。";
  }
}

watch(
  [
    executionMode,
    selectedSuiteRevisionId,
    snapshotId,
    runType,
    concurrency,
    totalTokenBudget,
    modelRequestBudget,
    replacementAttemptBudget,
    attemptTimeoutSeconds,
    quickN,
    quickCaseRevisionIds,
  ],
  invalidatePlan,
  { deep: true },
);

/** 把后端稳定状态码转换成面向用户的中文标签。 */
function statusText(status: string): string {
  const labels: Record<string, string> = {
    planned: "已计划",
    running: "运行中",
    paused: "已暂停",
    canceled: "已取消",
    sealed_complete: "封存完成",
    sealed_partial: "封存残缺",
    draft: "草稿",
    reviewed: "已审核",
    active: "生效中",
    retired: "已停用",
    succeeded: "成功",
    partial: "部分完成",
    failed: "失败",
    outcome_unknown: "结果未知",
    complete: "完整",
    incomplete: "不完整",
    missing: "缺失",
    ok: "正常",
    error: "错误",
    dropped: "已丢弃",
    pass: "通过",
    fail: "不通过",
    pending: "待定",
    exploratory: "探索性",
    excluded: "不计样本",
    satisfied: "满足",
    unsatisfied: "未满足",
    violated: "违规",
    not_violated: "未违规",
    supported: "有证据支持",
    unsupported: "无证据支持",
    unknown: "不确定",
  };
  return labels[status] ?? status;
}

/** 为结果矩阵中的 case 提供仅用于扫读的背景提示，不改变 case 聚合状态。 */
function matrixCaseAttentionClass(matrixCase: ResultMatrixCaseView): string {
  if (matrixCase.attempts.some((attempt) => attempt.result === "fail")) {
    return "matrix-case-attention-fail";
  }
  if (matrixCase.attempts.some((attempt) => attempt.result === "pending")) {
    return "matrix-case-attention-pending";
  }
  return "";
}

/** 解释 attempt 是否计入有效评测样本。 */
function outcomeClassText(outcomeClass: AttemptView["outcome_class"]): string {
  const labels: Record<AttemptView["outcome_class"], string> = {
    product: "有效样本",
    infrastructure: "基础设施失败，不计样本",
    canceled: "已取消，不计样本",
    unknown: "等待结果",
  };
  return labels[outcomeClass];
}

/** 把判定或检查失败码转换成可操作的中文说明。 */
function diagnosticErrorText(code: string): string {
  const labels: Record<string, string> = {
    judge_budget_exhausted: "判定预算不足，未发起模型调用",
    judge_evidence_missing: "缺少可判定的检索证据",
    judge_trace_incomplete: "运行轨迹不完整，无法构建判定证据",
    judge_trace_missing: "运行轨迹不存在，无法构建判定证据",
    judge_trace_invalid: "运行轨迹损坏，无法构建判定证据",
    judge_evidence_artifact_missing: "检索证据文件缺失",
    judge_evidence_artifact_invalid: "检索证据文件损坏",
    judge_output_invalid: "模型输出不符合结构化格式",
    judge_verdict_invalid: "模型结论超出允许的三态范围",
    judge_timeout: "模型调用超时",
    judge_provider_exception: "模型服务调用异常",
    judge_upstream_unavailable: "模型服务暂时不可用",
    judge_manual_required: "该判定项需要人工判定",
    judge_prompt_version_mismatch: "当前判官提示词版本与 criterion 不匹配",
    citation_block_missing: "答案引用的知识块不在本次检索证据中",
    citation_not_supported: "答案引用存在，但证据原文不支持对应主张",
    citation_support_unknown: "现有证据不足以判断引用是否支持答案",
    citation_support_input_too_large: "本次引用证据超过支持检查输入上限",
    citation_support_unit_too_large: "单个引用上下文或证据超过安全输入上限",
    citation_support_context_missing: "无法从答案中定位该引用对应的语义上下文",
    citation_support_occurrence_missing: "引用支持检查遗漏了部分引用 occurrence",
    citation_support_failed: "引用支持检查未完成",
    citation_output_invalid: "引用支持检查输出格式非法",
    citation_result_block_set_invalid: "引用支持检查的 block 结果不完整或重复",
    citation_occurrence_set_invalid: "引用支持检查的 occurrence 结果不完整或重复",
    citation_evidence_quote_invalid: "引用支持检查引用了不存在的证据原文",
  };
  return `${labels[code] ?? "执行未完成"}（${code}）`;
}

/** 把程序检查器标识转换成业务名称。 */
function checkerText(checkerId: string): string {
  const labels: Record<string, string> = {
    citation_integrity: "引用完整性",
    citation_support: "引用支持",
    truncation_declared: "截断声明",
  };
  return labels[checkerId] ?? checkerId;
}

/** 按执行者类型展示 CheckResult 的版本来源。 */
function checkExecutorVersionText(check: CheckResultView): string {
  if (check.checker_id !== "citation_support") {
    return `检查器版本 ${check.checker_version}`;
  }
  const promptVersion = check.payload.prompt_version;
  return typeof promptVersion === "string" && promptVersion
    ? `引用判官 ${promptVersion}`
    : `引用判官版本 ${check.checker_version}`;
}

/** 汇总引用支持分批检查的覆盖范围与批次数。 */
function citationBatchSummary(check: CheckResultView): string {
  if (check.checker_id !== "citation_support") {
    return "";
  }
  const batching = check.payload.batching;
  if (typeof batching !== "object" || batching === null) {
    return "";
  }
  const values = batching as Record<string, unknown>;
  const contextCount = Number(values.context_count ?? 0);
  const occurrenceCount = Number(values.occurrence_count ?? 0);
  const blockCount = Number(values.block_count ?? 0);
  const batchCount = Number(values.batch_count ?? 0);
  const preflightFailureCount = Number(values.preflight_failure_count ?? 0);
  const failureText = preflightFailureCount
    ? ` · ${preflightFailureCount} 个超限项`
    : "";
  return `${batchCount} 批 · ${contextCount} 个上下文 · ${occurrenceCount} 个引用关系 · ${blockCount} 个 block${failureText}`;
}

/** 读取检查 payload 中的字符串数组。 */
function checkStringList(check: CheckResultView, field: string): string[] {
  const value = check.payload[field];
  return Array.isArray(value) && value.every((item) => typeof item === "string")
    ? value
    : [];
}

/** 读取引用判官的逐 block 结论。 */
function checkCitationResults(
  check: CheckResultView,
): Array<{
  blockId: string;
  verdict: string;
  reason: string;
  evidenceQuotes: string[];
}> {
  return citationResultsFromPayload(check.payload);
}

/** 从检查或人工复核 payload 中读取逐 block 引用结论。 */
function citationResultsFromPayload(
  payload: Record<string, unknown>,
): Array<{
  blockId: string;
  verdict: string;
  reason: string;
  evidenceQuotes: string[];
}> {
  const value = payload.citations;
  if (!Array.isArray(value)) {
    return [];
  }
  return value.flatMap((item) => {
    if (
      !item ||
      typeof item !== "object" ||
      !("block_id" in item) ||
      !("verdict" in item) ||
      !("reason" in item) ||
      typeof item.block_id !== "string" ||
      typeof item.verdict !== "string" ||
      typeof item.reason !== "string"
    ) {
      return [];
    }
    const evidenceQuotes =
      "evidence_quotes" in item &&
      Array.isArray(item.evidence_quotes) &&
      item.evidence_quotes.every((quote: unknown) => typeof quote === "string")
        ? item.evidence_quotes
        : [];
    return [
      {
        blockId: item.block_id,
        verdict: item.verdict,
        reason: item.reason,
        evidenceQuotes,
      },
    ];
  });
}

/** 读取引用支持检查的调用审计。 */
function checkInvocations(check: CheckResultView): Array<{
  attemptNumber: number;
  status: string;
  errorCode: string | null;
  inputTokens: number;
  outputTokens: number;
  rawOutput: string | null;
}> {
  const value = check.payload.invocations;
  if (!Array.isArray(value)) {
    return [];
  }
  return value.flatMap((item) => {
    if (
      !item ||
      typeof item !== "object" ||
      !("attempt_number" in item) ||
      !("status" in item) ||
      typeof item.attempt_number !== "number" ||
      typeof item.status !== "string"
    ) {
      return [];
    }
    return [
      {
        attemptNumber: item.attempt_number,
        status: item.status,
        errorCode:
          "error_code" in item && typeof item.error_code === "string"
            ? item.error_code
            : null,
        inputTokens:
          "input_tokens" in item && typeof item.input_tokens === "number"
            ? item.input_tokens
            : 0,
        outputTokens:
          "output_tokens" in item && typeof item.output_tokens === "number"
            ? item.output_tokens
            : 0,
        rawOutput:
          "raw_output" in item && typeof item.raw_output === "string"
            ? item.raw_output
            : null,
      },
    ];
  });
}

/** 把评测类型代码转换成中文名称。 */
function runTypeText(runType: string): string {
  return runType === "latency" ? "首字延迟评测" : "质量评测";
}

/** 把暂停原因代码转换成可操作的中文说明。 */
function pauseReasonText(reason: string): string {
  const labels: Record<string, string> = {
    user_paused: "用户请求暂停",
    total_budget_exhausted: "累计执行预算不足",
    replacement_attempt_budget_exhausted: "替代执行样本次数已用完",
    upstream_rate_limited: "上游模型触发限流",
    continuous_upstream_failure: "上游模型连续失败",
    evaluation_process_restarted: "评测进程重启，未知结果不会自动重发",
    evaluation_runner_failure: "评测执行器内部失败",
  };
  return labels[reason] ?? reason;
}

/** 把 suite stale 原因转换成可操作的中文说明。 */
function staleReasonText(reason: string): string {
  if (reason === "suite_revision_not_active") {
    return "用例集修订版已停用";
  }
  if (reason === "duplicate_case_revision") {
    return "同一用例包含多个修订版";
  }
  if (reason.startsWith("case_revision_not_active:")) {
    const [, caseId, revisionId] = reason.split(":", 3);
    return `用例 ${caseId} 的修订版 ${revisionId} 已停用`;
  }
  return reason;
}

/** 把 Judgment executor 转换成中文名称。 */
function executorText(executor: string): string {
  const labels: Record<string, string> = {
    checker: "程序检查",
    llm_judge: "LLM 判官",
    human: "人工判定",
  };
  return labels[executor] ?? executor;
}

const runStatusLabel = computed(() => statusText(selectedRun.value?.status ?? ""));
const resultMatrix = computed(
  () => evaluationRevision.value?.result_matrix ?? null,
);

const caseById = computed(() => {
  const values = new Map<string, CaseRevisionView>();
  for (const catalogItem of caseCatalog.value) {
    for (const revision of catalogItem.revisions) {
      values.set(revision.revision_id, revision);
    }
  }
  for (const item of selectedSuite.value?.cases ?? []) {
    values.set(item.revision_id, item);
  }
  return values;
});

const criterionById = computed(() => {
  const values = new Map<string, CriterionView>();
  for (const catalogItem of caseCatalog.value) {
    for (const revision of catalogItem.revisions) {
      for (const criterion of revision.criteria) {
        values.set(criterion.revision_id, criterion);
      }
    }
  }
  return values;
});

const judgmentById = computed(() => {
  const values = new Map<string, JudgmentView>();
  for (const judgment of evaluationRevision.value?.judgments ?? []) {
    values.set(judgment.judgment_id, judgment);
  }
  return values;
});

const effectiveJudgments = computed(() => {
  const values: JudgmentView[] = [];
  for (const binding of evaluationRevision.value?.effective_bindings ?? []) {
    const judgment = judgmentById.value.get(binding.judgment_id);
    if (judgment) {
      values.push(judgment);
    }
  }
  return values;
});

const attemptOrdinalById = computed(() => {
  const values = new Map<string, number>();
  const byCase = new Map<string, AttemptView[]>();
  for (const attempt of selectedRun.value?.attempts ?? []) {
    const attempts = byCase.get(attempt.case_revision_id) ?? [];
    attempts.push(attempt);
    byCase.set(attempt.case_revision_id, attempts);
  }
  for (const attempts of byCase.values()) {
    attempts
      .sort((left, right) => left.started_at.localeCompare(right.started_at))
      .forEach((attempt, index) => values.set(attempt.attempt_id, index + 1));
  }
  return values;
});

const attemptJudgmentGroups = computed<AttemptJudgmentGroup[]>(() => {
  const aggregateByAttempt = new Map(
    (evaluationRevision.value?.attempt_aggregates ?? []).map((item) => [
      item.attempt_id,
      item,
    ]),
  );
  const judgmentsByAttempt = new Map<string, JudgmentView[]>();
  for (const judgment of effectiveJudgments.value) {
    const judgments = judgmentsByAttempt.get(judgment.attempt_id) ?? [];
    judgments.push(judgment);
    judgmentsByAttempt.set(judgment.attempt_id, judgments);
  }
  const attemptIds = new Set([
    ...judgmentsByAttempt.keys(),
    ...aggregateByAttempt.keys(),
  ]);
  const groups = [...attemptIds].map((attemptId) => {
    const attempt = selectedRun.value?.attempts.find(
      (item) => item.attempt_id === attemptId,
    );
    const aggregate = aggregateByAttempt.get(attemptId);
    const ordinal = attemptOrdinalById.value.get(attemptId) ?? 0;
    return {
      attemptId,
      caseId: caseIdForAttempt(attemptId),
      caseRevisionId: attempt?.case_revision_id ?? "",
      sampleLabel: ordinal > 0 ? `样本 #${String(ordinal).padStart(2, "0")}` : "样本",
      result: aggregate?.result ?? "pending",
      aggregate,
      judgments: judgmentsByAttempt.get(attemptId) ?? [],
      answer: attempt?.answer ?? null,
      traceId: attempt?.trace_id ?? null,
      traceStatus: attempt?.trace_status ?? "missing",
    } satisfies AttemptJudgmentGroup;
  });
  const priority: Record<AttemptAggregateView["result"], number> = {
    fail: 0,
    pending: 1,
    excluded: 2,
    pass: 3,
  };
  return groups.sort(
    (left, right) =>
      priority[left.result] - priority[right.result] ||
      left.sampleLabel.localeCompare(right.sampleLabel),
  );
});

const matrixJudgmentGroup = computed(
  () =>
    attemptJudgmentGroups.value.find(
      (group) => group.attemptId === matrixJudgmentAttemptId.value,
    ) ?? null,
);

const matrixJudgmentQuestion = computed(() => {
  const group = matrixJudgmentGroup.value;
  if (!group) {
    return "";
  }
  const matrixCase = resultMatrix.value?.categories
    .flatMap((category) => category.cases)
    .find((item) => item.case_revision_id === group.caseRevisionId);
  return (
    matrixCase?.question ??
    caseById.value.get(group.caseRevisionId)?.question ??
    "问题原文不可用"
  );
});

const hasOpenMatrixModal = computed(
  () => Boolean(matrixJudgmentGroup.value || matrixTraceAttempt.value),
);

const filteredAttemptJudgmentGroups = computed(() =>
  judgmentAttemptFilter.value === "failed"
    ? attemptJudgmentGroups.value.filter((group) => group.result === "fail")
    : attemptJudgmentGroups.value,
);

const failedAttemptCount = computed(
  () =>
    attemptJudgmentGroups.value.filter((group) => group.result === "fail").length,
);

const judgmentGroups = computed<JudgmentGroup[]>(() => {
  const judgments = effectiveJudgments.value;
  return [
    {
      ruleType: "R",
      title: "必选判定项",
      description:
        "对同一执行样本的答案进行检查。全部 R 都通过才算满足；任一 R 不通过则整体失败；存在待定项时不能得出通过结论。",
      items: judgments.filter(
        (judgment) =>
          criterionById.value.get(judgment.criterion_revision_id)?.rule_type ===
          "R",
      ),
    },
    {
      ruleType: "X",
      title: "边界判定项",
      description:
        "对同一执行样本的答案进行检查，确认是否触碰明确禁止的边界。全部 X 都合规才算满足；任一 X 违规则整体失败；存在待定项时不能得出通过结论。",
      items: judgments.filter(
        (judgment) =>
          criterionById.value.get(judgment.criterion_revision_id)?.rule_type ===
          "X",
      ),
    },
  ];
});

const judgeOverrideVerdicts = computed(() => {
  if (judgeOverrideTarget.value?.ruleType === "X") {
    return [
      { value: "not_violated", label: "无违规" },
      { value: "violated", label: "违规" },
      { value: "unknown", label: "不确定" },
    ];
  }
  return [
    { value: "satisfied", label: "满足" },
    { value: "unsatisfied", label: "未满足" },
    { value: "unknown", label: "不确定" },
  ];
});

/** 返回指定 criterion 的当前生效绑定。 */
function effectiveBindingFor(judgment: JudgmentView): JudgmentBindingView | undefined {
  return evaluationRevision.value?.effective_bindings.find(
    (binding) =>
      binding.attempt_id === judgment.attempt_id &&
      binding.criterion_revision_id === judgment.criterion_revision_id,
  );
}

/** 返回指定 criterion 的全部绑定历史。 */
function judgmentBindingHistory(judgment: JudgmentView): JudgmentBindingView[] {
  return (
    evaluationRevision.value?.judgment_bindings.filter(
      (binding) =>
        binding.attempt_id === judgment.attempt_id &&
        binding.criterion_revision_id === judgment.criterion_revision_id,
    ) ?? []
  );
}

/** 判断指定 Judgment 是否为当前生效项。 */
function isJudgmentEffective(judgment: JudgmentView): boolean {
  return (
    evaluationRevision.value?.effective_bindings.some(
      (binding) =>
        binding.judgment_id === judgment.judgment_id &&
        binding.attempt_id === judgment.attempt_id &&
        binding.criterion_revision_id === judgment.criterion_revision_id,
    ) ?? false
  );
}

/** 返回同一 attempt×criterion 的全部 Judgment。 */
function judgmentHistory(judgment: JudgmentView): JudgmentView[] {
  return (
    evaluationRevision.value?.judgments.filter(
      (item) =>
        item.attempt_id === judgment.attempt_id &&
        item.criterion_revision_id === judgment.criterion_revision_id,
    ) ?? []
  );
}

/** 返回一个 case revision 下的 attempt 聚合。 */
function attemptAggregatesForCase(caseRevisionId: string): AttemptAggregateView[] {
  return (
    evaluationRevision.value?.attempt_aggregates.filter(
      (item) => item.case_revision_id === caseRevisionId,
    ) ?? []
  );
}

/** 返回指定 attempt 的全部跨判定项检查。 */
function checksForAttempt(attemptId: string): CheckResultView[] {
  return (
    evaluationRevision.value?.checks.filter(
      (check) => check.attempt_id === attemptId,
    ) ?? []
  );
}

/** 在结果矩阵中打开样本判定弹窗，不切换当前分析上下文。 */
function openMatrixJudgment(attempt: ResultMatrixAttemptView): void {
  matrixJudgmentAttemptId.value = attempt.attempt_id;
}

/** 关闭结果矩阵中的样本判定弹窗。 */
function closeMatrixJudgment(): void {
  matrixJudgmentAttemptId.value = "";
}

/** 在结果矩阵中加载并打开指定 attempt 的 trace 弹窗。 */
async function openMatrixTrace(attempt: ResultMatrixAttemptView): Promise<void> {
  matrixTraceAttempt.value = attempt;
  matrixTraceDetail.value = null;
  matrixTraceError.value = "";
  if (!attempt.trace_id) {
    matrixTraceError.value = "该样本没有可用的运行轨迹。";
    matrixTraceLoading.value = false;
    return;
  }
  const requestSequence = ++matrixTraceRequestSequence;
  matrixTraceLoading.value = true;
  try {
    const detail = await api<TraceDetail>(
      `/api/eval/traces/${encodeURIComponent(attempt.trace_id)}`,
    );
    if (requestSequence !== matrixTraceRequestSequence) {
      return;
    }
    matrixTraceDetail.value = detail;
  } catch (reason) {
    if (requestSequence === matrixTraceRequestSequence) {
      matrixTraceError.value = `读取运行轨迹失败：${String(reason)}`;
    }
  } finally {
    if (requestSequence === matrixTraceRequestSequence) {
      matrixTraceLoading.value = false;
    }
  }
}

/** 关闭结果矩阵中的 trace 弹窗，并让未完成请求失效。 */
function closeMatrixTrace(): void {
  matrixTraceRequestSequence += 1;
  matrixTraceAttempt.value = null;
  matrixTraceDetail.value = null;
  matrixTraceLoading.value = false;
  matrixTraceError.value = "";
}

let previousBodyOverflow = "";
let previousDocumentOverflow = "";

watch(hasOpenMatrixModal, (isOpen) => {
  if (isOpen) {
    previousBodyOverflow = document.body.style.overflow;
    previousDocumentOverflow = document.documentElement.style.overflow;
    document.body.style.overflow = "hidden";
    document.documentElement.style.overflow = "hidden";
    return;
  }
  document.body.style.overflow = previousBodyOverflow;
  document.documentElement.style.overflow = previousDocumentOverflow;
});

/** 返回同一 case 内按开始时间排序的样本序号。 */
function attemptSampleLabel(attemptId: string): string {
  const ordinal = attemptOrdinalById.value.get(attemptId);
  return ordinal ? `样本 #${String(ordinal).padStart(2, "0")}` : "样本";
}

/** 返回 attempt 的短 ID，完整值保留在页面标题上。 */
function attemptShortId(attemptId: string): string {
  return `${attemptId.slice(0, 18)}…`;
}

/** 切到按样本视图并定位指定 attempt。 */
async function focusAttemptJudgments(attemptId: string): Promise<void> {
  historyDetailTab.value = "judgment";
  judgmentViewMode.value = "attempt";
  judgmentAttemptFilter.value = "all";
  focusedAttemptId.value = attemptId;
  await nextTick();
  document
    .getElementById(`attempt-judgment-${attemptId}`)
    ?.scrollIntoView({ behavior: "smooth", block: "start" });
}

/** 只有 LLM 语义 criterion 允许人工纠错。 */
function canOverrideJudgment(judgment: JudgmentView): boolean {
  return (
    evaluationRevision.value?.status !== "running" &&
    criterionById.value.get(judgment.criterion_revision_id)?.evaluator ===
      "llm_judge"
  );
}

/** 打开指定 criterion 的人工改判表单。 */
function openJudgeOverride(judgment: JudgmentView): void {
  const criterion = criterionById.value.get(judgment.criterion_revision_id);
  if (!criterion || criterion.evaluator !== "llm_judge") {
    return;
  }
  judgeOverrideTarget.value = {
    attemptId: judgment.attempt_id,
    criterionRevisionId: judgment.criterion_revision_id,
    ruleType: criterion.rule_type,
  };
  judgeOverrideVerdict.value = "";
  judgeOverrideReason.value = "";
  judgeOverrideReviewer.value = reviewerName.value || "开发者";
  judgeOverrideError.value = "";
}

/** 关闭人工改判表单。 */
function closeJudgeOverride(): void {
  judgeOverrideTarget.value = null;
  judgeOverrideError.value = "";
}

/** 提交人工 LLM Judgment，并切换到新生效绑定。 */
async function submitJudgeOverride(): Promise<void> {
  const target = judgeOverrideTarget.value;
  const revision = evaluationRevision.value;
  if (!target || !revision) {
    return;
  }
  judgeOverrideBusy.value = true;
  judgeOverrideError.value = "";
  try {
    const updated = await api<EvaluationRevisionView>(
      `/api/eval/evaluations/${encodeURIComponent(revision.revision_id)}/overrides`,
      {
        method: "POST",
        body: JSON.stringify({
          attempt_id: target.attemptId,
          criterion_revision_id: target.criterionRevisionId,
          verdict: judgeOverrideVerdict.value,
          reason: judgeOverrideReason.value.trim(),
          reviewer: judgeOverrideReviewer.value.trim(),
        }),
      },
    );
    setCurrentEvaluationRevision(updated);
    closeJudgeOverride();
    await refreshBaselineContext();
  } catch (reason) {
    judgeOverrideError.value = `人工改判失败：${String(reason)}`;
  } finally {
    judgeOverrideBusy.value = false;
  }
}

/** 返回指定 CheckResult 的最新人工复核。 */
function checkReviewFor(check: CheckResultView): CheckReviewView | undefined {
  const reviews =
    evaluationRevision.value?.check_reviews.filter(
      (review) =>
        review.attempt_id === check.attempt_id &&
        review.checker_id === check.checker_id,
    ) ?? [];
  return reviews[reviews.length - 1];
}

/** 返回人工 CheckResult 复核人。 */
function checkReviewReviewerText(check: CheckResultView): string {
  return checkReviewFor(check)?.reviewer ?? "";
}

/** 返回人工 CheckResult 复核说明。 */
function checkReviewReasonText(check: CheckResultView): string {
  return checkReviewFor(check)?.reason ?? "";
}

/** 返回当前生效的 CheckResult 状态。 */
function effectiveCheckResult(check: CheckResultView): "pass" | "fail" | "pending" {
  return checkReviewFor(check)?.result ?? check.result;
}

/** 返回当前生效的逐 block 引用支持结论。 */
function effectiveCitationResults(
  check: CheckResultView,
): ReturnType<typeof citationResultsFromPayload> {
  const review = checkReviewFor(check);
  return review
    ? citationResultsFromPayload(review.payload)
    : citationResultsFromPayload(check.payload);
}

/** 打开 LLM 引用支持检查的逐 block 人工复核表单。 */
function openCheckReview(check: CheckResultView): void {
  const integrity = evaluationRevision.value?.checks.find(
    (item) =>
      item.attempt_id === check.attempt_id &&
      item.checker_id === "citation_integrity",
  );
  if (!integrity) {
    checkReviewError.value = "缺少引用完整性事实，无法人工复核。";
    return;
  }
  const blockIds = checkStringList(integrity, "cited_block_ids");
  if (blockIds.length === 0) {
    checkReviewError.value = "本次答案没有可复核的显式引用。";
    return;
  }
  const current = new Map(
    effectiveCitationResults(check).map((item) => [item.blockId, item]),
  );
  checkReviewTarget.value = {
    check,
    items: blockIds.map((blockId) => ({
      blockId,
      verdict:
        current.get(blockId)?.verdict === "unsupported"
          ? "unsupported"
          : current.get(blockId)?.verdict === "unknown"
            ? "unknown"
            : "supported",
      reason: current.get(blockId)?.reason ?? "",
    })),
  };
  checkReviewReason.value = "";
  checkReviewReviewer.value = reviewerName.value || "开发者";
  checkReviewError.value = "";
}

/** 判断 LLM CheckResult 是否具备逐 block 人工复核条件。 */
function canReviewLlmCheck(check: CheckResultView): boolean {
  if (
    evaluationRevision.value?.status === "running" ||
    check.checker_id !== "citation_support"
  ) {
    return false;
  }
  const integrity = evaluationRevision.value?.checks.find(
    (item) =>
      item.attempt_id === check.attempt_id &&
      item.checker_id === "citation_integrity",
  );
  return (
    integrity?.result === "pass" &&
    checkStringList(integrity, "cited_block_ids").length > 0
  );
}

/** 关闭引用支持人工复核表单。 */
function closeCheckReview(): void {
  checkReviewTarget.value = null;
  checkReviewError.value = "";
}

/** 提交逐 block 人工引用支持复核。 */
async function submitCheckReview(): Promise<void> {
  const target = checkReviewTarget.value;
  const revision = evaluationRevision.value;
  if (!target || !revision) {
    return;
  }
  if (target.items.some((item) => !item.reason.trim())) {
    checkReviewError.value = "每个引用 block 都必须填写复核原因。";
    return;
  }
  checkReviewBusy.value = true;
  checkReviewError.value = "";
  try {
    const updated = await api<EvaluationRevisionView>(
      `/api/eval/evaluations/${encodeURIComponent(revision.revision_id)}/check-reviews`,
      {
        method: "POST",
        body: JSON.stringify({
          attempt_id: target.check.attempt_id,
          checker_id: target.check.checker_id,
          citations: target.items.map((item) => ({
            block_id: item.blockId,
            verdict: item.verdict,
            reason: item.reason.trim(),
            evidence_quotes: [],
          })),
          reason: checkReviewReason.value.trim(),
          reviewer: checkReviewReviewer.value.trim(),
        }),
      },
    );
    setCurrentEvaluationRevision(updated);
    closeCheckReview();
    await refreshBaselineContext();
  } catch (reason) {
    checkReviewError.value = `引用支持复核失败：${String(reason)}`;
  } finally {
    checkReviewBusy.value = false;
  }
}

const canStartJudgment = computed(() => {
  if (!selectedRun.value || !judgePrompt.value) {
    return false;
  }
  return (
    ["sealed_complete", "sealed_partial", "canceled"].includes(
      selectedRun.value.status,
    ) &&
    !evaluationRevisions.value.some((revision) => revision.status === "running")
  );
});

const citationIntegrityCheck = computed(() =>
  evaluationRevision.value?.checks.find(
    (check) => check.checker_id === "citation_integrity",
  ),
);

const citationSupportCheck = computed(() =>
  evaluationRevision.value?.checks.find(
    (check) => check.checker_id === "citation_support",
  ),
);

const citationJudgeNotice = computed(() => {
  if (!evaluationRevision.value || citationSupportCheck.value) {
    return "";
  }
  if (!citationIntegrityCheck.value) {
    return "尚未生成引用完整性检查。";
  }
  if (citationIntegrityCheck.value.result === "pending") {
    return "运行轨迹或证据不可用，未执行引用支持判官。";
  }
  if (
    checkStringList(citationIntegrityCheck.value, "cited_block_ids").length === 0
  ) {
    return "本次答案没有显式引用，引用支持判官未触发。";
  }
  return "本次存在无效引用，引用支持判官未执行。";
});

/** 当前选中的判定修订版；切换时同步轮询状态。 */
const selectedEvaluationRevisionId = computed({
  get: () => evaluationRevision.value?.revision_id ?? "",
  set: (revisionId: string) => {
    const selected = evaluationRevisions.value.find(
      (revision) => revision.revision_id === revisionId,
    );
    setCurrentEvaluationRevision(selected ?? null);
  },
});

/** 构造带启动令牌的评测 API 请求头。 */
function headers(): Record<string, string> {
  return connection.value
    ? {
        "content-type": "application/json",
        "x-launch-token": connection.value.token,
      }
    : { "content-type": "application/json" };
}

/** 调用评测 sidecar，并把稳定错误结构转换成界面消息。 */
async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (!connection.value) {
    throw new Error("评测服务尚未连接");
  }
  const response = await fetchWithTimeout(
    `${connection.value.baseUrl}${path}`,
    {
      ...init,
      cache: "no-store",
      headers: {
        ...headers(),
        ...(init.headers ?? {}),
      },
    },
    path.includes("/plans") || path.includes("/runs") ? 30_000 : 15_000,
  );
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiErrorBody;
    throw new Error(apiErrorText(body, response.status));
  }
  return (await response.json()) as T;
}

/** 把 FastAPI 字符串或结构化校验错误转换成单行可读文本。 */
function apiErrorText(body: ApiErrorBody, status: number): string {
  if (body.error?.message) {
    return body.error.message;
  }
  if (typeof body.detail === "string") {
    return body.detail;
  }
  if (Array.isArray(body.detail)) {
    const messages = body.detail.flatMap((item) => {
      if (!item || typeof item !== "object") {
        return [];
      }
      const record = item as Record<string, unknown>;
      const location = Array.isArray(record.loc)
        ? record.loc.map(String).join(".")
        : "请求参数";
      const message =
        typeof record.msg === "string" ? record.msg : "参数校验失败";
      return [`${location}：${message}`];
    });
    if (messages.length) {
      return messages.join("；");
    }
  }
  if (body.detail && typeof body.detail === "object") {
    return JSON.stringify(body.detail);
  }
  return `请求失败（HTTP ${status}）`;
}

/** 按需连接评测 sidecar，并加载 suite、快照和最近运行。 */
async function connectAndLoad(): Promise<void> {
  loading.value = true;
  error.value = "";
  try {
    connection.value = await invoke<EvaluationConnection>(
      "eval_backend_connection",
    );
    await waitForHealth();
    const [
      loadedSuites,
      loadedCases,
      collection,
      recentRuns,
      loadedJudgePrompt,
      loadedCitationPrompt,
    ] = await Promise.all([
      api<SuiteRevisionView[]>("/api/eval/suites"),
      api<CaseCatalogItem[]>("/api/eval/cases"),
      api<CurrentCollection | null>("/api/eval/collections/current"),
      api<RunView[]>("/api/eval/runs"),
      api<JudgePromptView>("/api/eval/judge-prompt"),
      api<JudgePromptView>("/api/eval/citation-prompt"),
    ]);
    suites.value = loadedSuites;
    caseCatalog.value = loadedCases;
    selectedSuiteRevisionId.value =
      loadedSuites[0]?.revision_id ?? selectedSuiteRevisionId.value;
    currentCollection.value = collection;
    snapshotId.value =
      collection?.manifest.snapshot_id ?? snapshotId.value;
    runs.value = recentRuns;
    judgePrompt.value = loadedJudgePrompt;
    citationPrompt.value = loadedCitationPrompt;
    const active = recentRuns.find((run) =>
      ["planned", "running", "paused"].includes(run.status),
    );
    if (active) {
      selectedRun.value = active;
      schedulePolling();
    }
  } catch (reason) {
    error.value = `评测服务连接失败：${String(reason)}`;
  } finally {
    loading.value = false;
  }
}

/** 在 sidecar 首次启动期间轮询健康检查，避免把启动窗口当失败。 */
async function waitForHealth(): Promise<void> {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const response = await fetchWithTimeout(
        `${connection.value?.baseUrl}/api/eval/health`,
        {},
        1_500,
      );
      if (response.ok) {
        return;
      }
    } catch {
      // sidecar 首次按需启动需要数秒，继续等待而不把中间失败暴露给用户。
    }
    await new Promise((resolve) => window.setTimeout(resolve, 250));
  }
  throw new Error("本地评测服务在 15 秒内未就绪");
}

/** 重新读取用例目录和 suite revisions。 */
async function reloadCatalog(): Promise<void> {
  const [loadedSuites, loadedCases] = await Promise.all([
    api<SuiteRevisionView[]>("/api/eval/suites"),
    api<CaseCatalogItem[]>("/api/eval/cases"),
  ]);
  suites.value = loadedSuites;
  caseCatalog.value = loadedCases;
  const selected = loadedCases.find((item) => item.case_id === selectedCaseId.value);
  if (selected) {
    const revisionStillExists = selected.revisions.some(
      (item) => item.revision_id === selectedCaseRevisionId.value,
    );
    if (!revisionStillExists) {
      selectedCaseRevisionId.value =
        selected.active_revision_id ?? selected.latest_revision_id ?? "";
    }
  }
  if (!loadedSuites.some((suite) => suite.revision_id === selectedSuiteRevisionId.value)) {
    selectedSuiteRevisionId.value = loadedSuites[0]?.revision_id ?? "";
  }
}

/** 选择左侧用例，并在右侧展示只读修订版详情。 */
function selectCase(item: CaseCatalogItem): void {
  selectedCaseId.value = item.case_id;
  selectedCaseRevisionId.value =
    item.active_revision_id ?? item.latest_revision_id ?? "";
  caseDraft.value = null;
}

/** 把草稿对象同步到可编辑表单。 */
function applyCaseDraft(draft: CaseDraftView): void {
  caseDraft.value = draft;
  caseDraftDirty.value = false;
  caseDraftSaveState.value = `已保存 · ${new Date(draft.updated_at).toLocaleString()}`;
  caseDraftSaveError.value = "";
  caseTargetTitles.value = draft.target_bindings
    .map((item) => {
      const title = item.title_contains ?? "";
      const section = item.section_path?.join(" / ") ?? "";
      return section ? `${title} | ${section}` : title;
    })
    .filter(Boolean)
    .join("\n");
  caseDistractorMetadata.value = JSON.stringify(draft.distractor_metadata, null, 2);
  caseExecutionPolicy.value = JSON.stringify(draft.execution_policy, null, 2);
}

/** 标记草稿表单存在尚未保存的修改。 */
function markCaseDraftDirty(): void {
  caseDraftDirty.value = true;
  caseDraftSaveState.value = "有未保存修改";
  caseDraftSaveError.value = "";
}

/** 创建或重新打开一条用例草稿。 */
async function beginCaseDraft(
  caseId: string,
  baseRevisionId: string | null = null,
): Promise<void> {
  busy.value = true;
  error.value = "";
  try {
    const draft = await api<CaseDraftView>(
      `/api/eval/cases/${encodeURIComponent(caseId)}/draft`,
      {
        method: "POST",
        body: JSON.stringify({
          updated_by: reviewerName.value.trim() || "开发者",
          base_revision_id: baseRevisionId,
        }),
      },
    );
    selectedCaseId.value = caseId;
    applyCaseDraft(draft);
    await reloadCatalog();
    message.value = `${caseId} 草稿已打开，可反复修改。`;
  } catch (reason) {
    error.value = String(reason);
  } finally {
    busy.value = false;
  }
}

/** 通过简单输入创建新的稳定用例与初始草稿。 */
async function createNewCase(): Promise<void> {
  const caseId = window.prompt("新用例 ID，例如 c16-001：")?.trim();
  if (!caseId) {
    return;
  }
  const category = window.prompt("问题类别：", "16 新类型")?.trim() ?? "";
  if (!category) {
    return;
  }
  const question = window.prompt("初始问题：")?.trim() ?? "";
  if (!question) {
    return;
  }
  busy.value = true;
  error.value = "";
  try {
    const draft = await api<CaseDraftView>("/api/eval/cases", {
      method: "POST",
      body: JSON.stringify({
        case_id: caseId,
        category,
        question,
        updated_by: reviewerName.value.trim() || "开发者",
      }),
    });
    selectedCaseId.value = caseId;
    applyCaseDraft(draft);
    await reloadCatalog();
    message.value = `${caseId} 已创建草稿，请添加判定项后提交审核。`;
  } catch (reason) {
    error.value = String(reason);
  } finally {
    busy.value = false;
  }
}

/** 保存当前用例草稿；返回是否成功。 */
async function saveCaseDraft(showMessage = true): Promise<boolean> {
  if (!caseDraft.value) {
    return false;
  }
  error.value = "";
  caseDraftSaveError.value = "";
  if (!caseDraft.value.question.trim()) {
    caseDraftSaveError.value = "问题不能为空。";
    return false;
  }
  if (caseDraft.value.n < 1 || caseDraft.value.k < 1 || caseDraft.value.k > caseDraft.value.n) {
    caseDraftSaveError.value = "N 和 K 必须是正整数，且 K 不能大于 N。";
    return false;
  }
  caseDraftSaveState.value = "正在保存…";
  try {
    const existingTargets = caseDraft.value.target_bindings;
    const targetBindings = caseTargetTitles.value
      .split("\n")
      .map((value) => value.trim())
      .filter(Boolean)
      .map((line) => {
        const [titleValue, sectionValue = ""] = line.split("|", 2);
        const title = titleValue.trim();
        const matching = existingTargets.find(
          (item) => item.title_contains === title,
        );
        const sectionPath = sectionValue
          .split(/\s*\/\s*/)
          .map((item) => item.trim())
          .filter(Boolean);
        return {
          title_contains: title,
          section_path:
            sectionPath.length > 0 ? sectionPath : (matching?.section_path ?? []),
        };
      });
    await api<CaseDraftView>(
      `/api/eval/cases/${encodeURIComponent(caseDraft.value.case_id)}/draft`,
      {
        method: "PUT",
        body: JSON.stringify({
          category: caseDraft.value.category,
          question: caseDraft.value.question,
          target_bindings: targetBindings,
          distractor_metadata: JSON.parse(caseDistractorMetadata.value),
          n: caseDraft.value.n,
          k: caseDraft.value.k,
          execution_policy: JSON.parse(caseExecutionPolicy.value),
          change_note: caseDraft.value.change_note,
          criteria: caseDraft.value.criteria.map((criterion) => ({ ...criterion })),
          updated_by: reviewerName.value.trim() || "开发者",
        }),
      },
    );
    const persisted = await api<CaseDraftView>(
      `/api/eval/cases/${encodeURIComponent(caseDraft.value.case_id)}/draft`,
    );
    applyCaseDraft(persisted);
    await reloadCatalog();
    if (showMessage) {
      message.value = "用例草稿已保存，尚未生成新修订版。";
    }
    return true;
  } catch (reason) {
    caseDraftSaveError.value = `保存草稿失败：${String(reason)}`;
    caseDraftSaveState.value = "保存失败";
    return false;
  }
}

/** 保存并原子发布 reviewed case revision。 */
async function publishCaseDraft(): Promise<void> {
  if (!caseDraft.value || !(await saveCaseDraft(false))) {
    return;
  }
  busy.value = true;
  try {
    const revision = await api<CaseRevisionView>(
      `/api/eval/cases/${encodeURIComponent(caseDraft.value.case_id)}/draft/publish`,
      {
        method: "POST",
        body: JSON.stringify({
          reviewer: reviewerName.value.trim() || "开发者",
        }),
      },
    );
    caseDraft.value = null;
    selectedCaseRevisionId.value = revision.revision_id;
    await reloadCatalog();
    selectedCaseRevisionId.value = revision.revision_id;
    message.value = `${revision.case_id} 第 ${revision.revision_number} 版已生成，状态为已审核。`;
  } catch (reason) {
    error.value = `发布修订包失败：${String(reason)}`;
  } finally {
    busy.value = false;
  }
}

/** 激活 reviewed case revision。 */
async function activateCaseRevision(revision: CaseRevisionView): Promise<void> {
  busy.value = true;
  error.value = "";
  try {
    await api<CaseRevisionView>(
      `/api/eval/cases/revisions/${encodeURIComponent(revision.revision_id)}/review`,
      {
        method: "POST",
        body: JSON.stringify({
          review_status: "active",
          reviewer: reviewerName.value.trim() || "开发者",
        }),
      },
    );
    await reloadCatalog();
    message.value = `${revision.case_id} 第 ${revision.revision_number} 版已激活。`;
  } catch (reason) {
    error.value = String(reason);
  } finally {
    busy.value = false;
  }
}

/** 停用一个已审核或已激活的用例修订版。 */
async function retireCaseRevision(revision: CaseRevisionView): Promise<void> {
  busy.value = true;
  error.value = "";
  try {
    await api<CaseRevisionView>(
      `/api/eval/cases/revisions/${encodeURIComponent(revision.revision_id)}/review`,
      {
        method: "POST",
        body: JSON.stringify({
          review_status: "retired",
          reviewer: reviewerName.value.trim() || "开发者",
        }),
      },
    );
    await reloadCatalog();
    message.value = `${revision.case_id} 第 ${revision.revision_number} 版已停用。`;
  } catch (reason) {
    error.value = String(reason);
  } finally {
    busy.value = false;
  }
}

/** 向草稿追加一条 R／X 判定项。 */
function addDraftCriterion(ruleType: "R" | "X"): void {
  if (!caseDraft.value) {
    return;
  }
  const ordinal =
    Math.max(
      0,
      ...caseDraft.value.criteria
        .filter((criterion) => criterion.rule_type === ruleType)
        .map((criterion) => criterion.ordinal),
    ) + 1;
  caseDraft.value.criteria.push({
    criterion_id: `criterion_${crypto.randomUUID()}`,
    base_revision_id: null,
    rule_type: ruleType,
    ordinal,
    title: "",
    rule_text: "",
    evaluator: "human",
    evaluator_ref: null,
  });
  markCaseDraftDirty();
}

/** 从草稿移除一条判定项。 */
function removeDraftCriterion(index: number): void {
  caseDraft.value?.criteria.splice(index, 1);
  markCaseDraftDirty();
}

/** 打开或创建 suite draft。 */
async function beginSuiteDraft(suiteId?: string): Promise<void> {
  const resolvedSuiteId =
    suiteId ??
    selectedSuite.value?.suite_id ??
    window.prompt("输入新用例集名称，例如 smoke：")?.trim();
  if (!resolvedSuiteId) {
    return;
  }
  busy.value = true;
  error.value = "";
  try {
    const draft = await api<SuiteDraftView>(
      `/api/eval/suites/${encodeURIComponent(resolvedSuiteId)}/draft`,
      {
        method: "POST",
        body: JSON.stringify({ updated_by: reviewerName.value.trim() || "开发者" }),
      },
    );
    suiteDraft.value = draft;
    suiteDraftCaseIds.value = draft.cases.flatMap((item) => {
      const active = caseCatalog.value
        .find((catalog) => catalog.case_id === item.case_id)
        ?.revisions.find((candidate) => candidate.review_status === "active");
      return active ? [active.revision_id] : [];
    });
    await reloadCatalog();
    message.value = `用例集 ${resolvedSuiteId} 草稿已打开。`;
  } catch (reason) {
    error.value = String(reason);
  } finally {
    busy.value = false;
  }
}

/** 输入名称后创建新的用例集草稿。 */
async function beginNewSuiteDraft(): Promise<void> {
  const suiteId = window.prompt("输入新用例集名称，例如 smoke：")?.trim();
  if (suiteId) {
    await beginSuiteDraft(suiteId);
  }
}

/** 在 suite draft 中选中一条 case revision，并保证同一 case 唯一。 */
function toggleSuiteDraftCase(revision: CaseRevisionView): void {
  if (!suiteDraft.value) {
    return;
  }
  const selected = suiteDraftCaseIds.value.includes(revision.revision_id);
  if (selected) {
    suiteDraftCaseIds.value = suiteDraftCaseIds.value.filter(
      (item) => item !== revision.revision_id,
    );
    return;
  }
  const sameCaseIds = new Set(
    caseCatalog.value
      .find((item) => item.case_id === revision.case_id)
      ?.revisions.map((item) => item.revision_id) ?? [],
  );
  suiteDraftCaseIds.value = [
    ...suiteDraftCaseIds.value.filter((item) => !sameCaseIds.has(item)),
    revision.revision_id,
  ];
}

/** 保存 suite draft。 */
async function saveSuiteDraft(showMessage = true): Promise<boolean> {
  if (!suiteDraft.value) {
    return false;
  }
  busy.value = true;
  error.value = "";
  try {
    const draft = await api<SuiteDraftView>(
      `/api/eval/suites/${encodeURIComponent(suiteDraft.value.suite_id)}/draft`,
      {
        method: "PUT",
        body: JSON.stringify({
          name: suiteDraft.value.name,
          description: suiteDraft.value.description,
          scope_id: suiteDraft.value.scope_id,
          case_revision_ids: suiteDraftCaseIds.value,
          change_note: suiteDraft.value.change_note,
          updated_by: reviewerName.value.trim() || "开发者",
        }),
      },
    );
    suiteDraft.value = draft;
    await reloadCatalog();
    if (showMessage) {
      message.value = "用例集草稿已保存。";
    }
    return true;
  } catch (reason) {
    error.value = `保存用例集失败：${String(reason)}`;
    return false;
  } finally {
    busy.value = false;
  }
}

/** 发布 suite draft 并启用新 revision。 */
async function publishSuiteDraft(): Promise<void> {
  if (!suiteDraft.value || !(await saveSuiteDraft(false))) {
    return;
  }
  busy.value = true;
  try {
    const suite = await api<SuiteRevisionView>(
      `/api/eval/suites/${encodeURIComponent(suiteDraft.value.suite_id)}/draft/publish`,
      {
        method: "POST",
        body: JSON.stringify({
          reviewer: reviewerName.value.trim() || "开发者",
        }),
      },
    );
    suiteDraft.value = null;
    suiteDraftCaseIds.value = [];
    selectedSuiteRevisionId.value = suite.revision_id;
    await reloadCatalog();
    message.value = `用例集 ${suite.name} 已启用修订版 ${suite.revision_id}。`;
  } catch (reason) {
    error.value = `发布用例集失败：${String(reason)}`;
  } finally {
    busy.value = false;
  }
}

/** 选择或取消快速试跑中的唯一 case revision。 */
function toggleQuickCase(revision: CaseRevisionView): void {
  if (quickCaseRevisionIds.value.includes(revision.revision_id)) {
    quickCaseRevisionIds.value = quickCaseRevisionIds.value.filter(
      (item) => item !== revision.revision_id,
    );
    return;
  }
  quickCaseRevisionIds.value = [
    ...quickCaseRevisionIds.value.filter(
      (item) =>
        !caseCatalog.value
          .find((catalog) => catalog.case_id === revision.case_id)
          ?.revisions.some((candidate) => candidate.revision_id === item),
    ),
    revision.revision_id,
  ];
}

/** 返回某用例在快速试跑选择中的修订版号。 */
function quickSelectedRevisionNumber(
  revisions: CaseRevisionView[],
): number | null {
  return (
    revisions.find((revision) =>
      quickCaseRevisionIds.value.includes(revision.revision_id),
    )?.revision_number ?? null
  );
}

/** 根据 attempt 查找对应的稳定 case ID。 */
function caseIdForAttempt(attemptId: string): string {
  const attempt = selectedRun.value?.attempts.find(
    (item) => item.attempt_id === attemptId,
  );
  if (!attempt) {
    return attemptId;
  }
  return (
    caseById.value.get(attempt.case_revision_id)?.case_id ??
    attempt.case_revision_id
  );
}

/** 请求后端验证现场并生成不可变 ExecutionPlan。 */
async function runPreflight(): Promise<void> {
  busy.value = true;
  error.value = "";
  message.value = "";
  traceDetail.value = null;
  try {
    if (
      executionMode.value === "exploratory" &&
      quickCaseRevisionIds.value.length === 0
    ) {
      throw new Error("快速试跑至少选择一条已审核或生效中用例修订版");
    }
    const commonPolicy = {
      collection_snapshot_id: snapshotId.value.trim(),
      run_type: runType.value,
      concurrency: runType.value === "latency" ? 1 : concurrency.value,
      total_token_budget: totalTokenBudget.value,
      model_request_budget: modelRequestBudget.value,
      replacement_attempt_budget: replacementAttemptBudget.value,
      attempt_timeout_seconds: attemptTimeoutSeconds.value,
    };
    plan.value =
      executionMode.value === "formal"
        ? await api<ExecutionPlanView>("/api/eval/plans", {
            method: "POST",
            body: JSON.stringify({
              ...commonPolicy,
              suite_revision_id: selectedSuiteRevisionId.value,
            }),
          })
        : await api<ExecutionPlanView>("/api/eval/quick-plans", {
            method: "POST",
            body: JSON.stringify({
              ...commonPolicy,
              case_revision_ids: quickCaseRevisionIds.value,
              n_override: quickN.value,
            }),
          });
    message.value =
      plan.value.status === "planned"
        ? `${plan.value.mode === "exploratory" ? "探索性试跑" : "正式评测"}预检通过：${plan.value.cases.length} 条用例，预计 ${plannedAttempts.value} 个执行样本。`
        : "预检失败，请依据失败项修正现场后重新生成计划。";
  } catch (reason) {
    error.value = String(reason);
  } finally {
    busy.value = false;
  }
}

/** 从已通过预检的计划启动后台 ExecutionRun。 */
async function startRun(): Promise<void> {
  if (!plan.value || plan.value.status !== "planned") {
    return;
  }
  busy.value = true;
  error.value = "";
  try {
    selectedRun.value = await api<RunView>("/api/eval/runs", {
      method: "POST",
      body: JSON.stringify({ plan_id: plan.value.plan_id }),
    });
    message.value = "评测运行已启动。";
    schedulePolling();
    await refreshRuns();
  } catch (reason) {
    error.value = String(reason);
  } finally {
    busy.value = false;
  }
}

/** 在 attempt 安全边界暂停、继续或取消当前运行。 */
async function controlRun(action: "pause" | "resume" | "cancel"): Promise<void> {
  if (!selectedRun.value) {
    return;
  }
  busy.value = true;
  error.value = "";
  try {
    selectedRun.value = await api<RunView>(
      `/api/eval/runs/${encodeURIComponent(selectedRun.value.run_id)}/${action}`,
      { method: "POST" },
    );
    message.value =
      action === "pause"
        ? "已请求在当前执行样本结束后暂停。"
        : action === "resume"
          ? "已继续运行缺少的有效样本。"
          : "已请求在当前执行样本结束后取消。";
    schedulePolling();
  } catch (reason) {
    error.value = String(reason);
  } finally {
    busy.value = false;
  }
}

/** 轮询当前运行进度，并在封存后停止定时刷新。 */
async function refreshRun(): Promise<void> {
  if (!selectedRun.value) {
    return;
  }
  try {
    selectedRun.value = await api<RunView>(
      `/api/eval/runs/${encodeURIComponent(selectedRun.value.run_id)}`,
    );
    if (!activeRun.value) {
      stopPolling();
      await refreshRuns();
      await loadEvaluations(selectedRun.value.run_id);
    }
  } catch (reason) {
    error.value = `刷新运行进度失败：${String(reason)}`;
    stopPolling();
  }
}

/** 切换当前查看的判定修订版，并让轮询只跟随当前修订版。 */
function setCurrentEvaluationRevision(
  revision: EvaluationRevisionView | null,
): void {
  if (evaluationRevision.value?.revision_id !== revision?.revision_id) {
    closeJudgeOverride();
    closeCheckReview();
  }
  evaluationRevision.value = revision;
  if (revision) {
    const index = evaluationRevisions.value.findIndex(
      (item) => item.revision_id === revision.revision_id,
    );
    if (index === -1) {
      evaluationRevisions.value = [revision, ...evaluationRevisions.value];
    } else {
      evaluationRevisions.value.splice(index, 1, revision);
    }
  }
  if (revision?.status === "running") {
    scheduleJudgmentPolling();
  } else {
    stopJudgmentPolling();
  }
}

/** 展开或收起重新判定表单，避免在模板中直接修改响应式状态。 */
function toggleJudgmentForm(): void {
  isJudgmentFormOpen.value = !isJudgmentFormOpen.value;
}

/** 加载一个历史 run 的全部 EvaluationRevision，并优先保留当前选择。 */
async function loadEvaluations(runId: string): Promise<void> {
  judgingError.value = "";
  try {
    const revisions = await api<EvaluationRevisionView[]>(
      `/api/eval/runs/${encodeURIComponent(runId)}/evaluations`,
    );
    const selectedRevisionId = evaluationRevision.value?.revision_id;
    evaluationRevisions.value = revisions;
    setCurrentEvaluationRevision(
      revisions.find((item) => item.revision_id === selectedRevisionId) ??
        revisions[0] ??
        null,
    );
  } catch (reason) {
    judgingError.value = `读取判定记录失败：${String(reason)}`;
  }
}

/** 读取当前 run 的基线历史和同场比较结果。 */
async function refreshBaselineContext(): Promise<void> {
  const run = selectedRun.value;
  if (!run?.suite_id) {
    baselineHistory.value = [];
    currentBaseline.value = null;
    baselineComparison.value = null;
    return;
  }
  baselineLoading.value = true;
  baselineError.value = "";
  const query = `suite_id=${encodeURIComponent(run.suite_id)}&run_type=${encodeURIComponent(run.run_type)}`;
  try {
    const [history, current] = await Promise.all([
      api<BaselineView[]>(`/api/eval/baselines?${query}`),
      api<BaselineView | null>(`/api/eval/baselines/current?${query}`),
    ]);
    baselineHistory.value = history;
    currentBaseline.value = current;
    const revision = evaluationRevision.value;
    baselineComparison.value =
      current &&
      revision?.status === "complete" &&
      (baselineEligibility.value.eligible || selectedRunIsCurrentBaseline.value)
        ? await api<BaselineComparisonView | null>(
            `/api/eval/runs/${encodeURIComponent(run.run_id)}/comparison?revision_id=${encodeURIComponent(revision.revision_id)}`,
          )
        : null;
  } catch (reason) {
    baselineError.value = `读取基线失败：${String(reason)}`;
    baselineHistory.value = [];
    currentBaseline.value = null;
    baselineComparison.value = null;
  } finally {
    baselineLoading.value = false;
  }
}

/** 打开基线设置表单。 */
function openBaselineForm(): void {
  baselineReason.value = "";
  baselineError.value = "";
  isBaselineFormOpen.value = true;
}

/** 关闭基线设置表单。 */
function closeBaselineForm(): void {
  isBaselineFormOpen.value = false;
  baselineReason.value = "";
}

/** 人工把当前完整正式 run 设为 scope 的当前基线。 */
async function submitBaseline(): Promise<void> {
  const run = selectedRun.value;
  const revision = evaluationRevision.value;
  const reason = baselineReason.value.trim();
  if (!run || !revision || !reason) {
    return;
  }
  baselineBusy.value = true;
  baselineError.value = "";
  try {
    currentBaseline.value = await api<BaselineView>(
      `/api/eval/runs/${encodeURIComponent(run.run_id)}/baseline`,
      {
        method: "POST",
        body: JSON.stringify({
          evaluation_revision_id: revision.revision_id,
          reviewer: reviewerName.value.trim() || "开发者",
          reason,
        }),
      },
    );
    closeBaselineForm();
    message.value = "当前基线已更新。";
    await refreshBaselineContext();
  } catch (errorValue) {
    baselineError.value = `设置基线失败：${String(errorValue)}`;
  } finally {
    baselineBusy.value = false;
  }
}

/** 把整数变化格式化为带符号文本。 */
function signedNumber(value: number): string {
  return value > 0 ? `+${formatTokens(value)}` : formatTokens(value);
}

/** 把比较中的稳定性字段转换成中文说明。 */
function baselineChangeText(change: BaselineCaseDeltaView["change"]): string {
  const labels: Record<BaselineCaseDeltaView["change"], string> = {
    new: "新增 case",
    removed: "移除 case",
    improved: "转好",
    regressed: "转差",
    changed: "其他变化",
    unchanged: "未变化",
  };
  return labels[change];
}

/** 把比较现场不一致代码转换成可操作说明。 */
function baselineIncompatibilityText(reason: string): string {
  const labels: Record<string, string> = {
    suite_revision_changed: "suite revision 不同",
    case_revision_set_changed: "case revision 集合不同",
    collection_snapshot_changed: "collection snapshot 不同",
    run_policy_changed: "运行策略不同",
    aggregate_rule_changed: "聚合规则版本不同",
  };
  return labels[reason] ?? reason;
}

watch(
  () => [
    selectedRun.value?.run_id,
    evaluationRevision.value?.revision_id,
    evaluationRevision.value?.status,
  ],
  () => {
    if (selectedRun.value?.suite_id) {
      void refreshBaselineContext();
    }
  },
);

/** 启动逐 criterion 判定。 */
async function startJudgment(): Promise<void> {
  if (!selectedRun.value || !judgeReviewConfirmed.value) {
    return;
  }
  judgingBusy.value = true;
  judgingError.value = "";
  try {
    const normalizedConcurrency = Math.min(
      8,
      Math.max(1, Number(judgeConcurrency.value) || 2),
    );
    judgeConcurrency.value = normalizedConcurrency;
    const revision = await api<EvaluationRevisionView>(
      `/api/eval/runs/${encodeURIComponent(selectedRun.value.run_id)}/judging`,
      {
        method: "POST",
        body: JSON.stringify({
          concurrency: normalizedConcurrency,
          token_budget: judgeTokenBudget.value,
          request_budget: judgeRequestBudget.value,
          attempt_timeout_seconds: judgeTimeoutSeconds.value,
          judge_review_confirmed: true,
        }),
      },
    );
    isJudgmentFormOpen.value = false;
    judgeReviewConfirmed.value = false;
    setCurrentEvaluationRevision(revision);
  } catch (reason) {
    judgingError.value = `启动判定失败：${
      reason instanceof Error ? reason.message : String(reason)
    }`;
  } finally {
    judgingBusy.value = false;
  }
}

/** 刷新正在运行的 EvaluationRevision。 */
async function refreshJudgment(): Promise<void> {
  if (!evaluationRevision.value) {
    return;
  }
  try {
    const revision = await api<EvaluationRevisionView>(
      `/api/eval/evaluations/${encodeURIComponent(evaluationRevision.value.revision_id)}`,
    );
    setCurrentEvaluationRevision(revision);
  } catch (reason) {
    judgingError.value = `刷新判定进度失败：${String(reason)}`;
    stopJudgmentPolling();
  }
}

/** 为 running EvaluationRevision 建立轮询。 */
function scheduleJudgmentPolling(): void {
  stopJudgmentPolling();
  if (evaluationRevision.value?.status === "running") {
    judgmentPollTimer = window.setInterval(
      () => void refreshJudgment(),
      800,
    );
  }
}

/** 停止判定进度轮询。 */
function stopJudgmentPolling(): void {
  if (judgmentPollTimer !== undefined) {
    window.clearInterval(judgmentPollTimer);
    judgmentPollTimer = undefined;
  }
}

/** 刷新辅助用的最近运行列表。 */
async function refreshRuns(): Promise<void> {
  try {
    runs.value = await api<RunView[]>("/api/eval/runs");
  } catch {
    // 最近运行只是辅助面板，不覆盖主状态栏中的关键错误。
  }
}

/** 为仍在活动的 run 建立低频轮询。 */
function schedulePolling(): void {
  stopPolling();
  if (selectedRun.value && activeRun.value) {
    pollTimer = window.setInterval(() => void refreshRun(), 800);
  }
}

/** 清理当前组件的轮询定时器。 */
function stopPolling(): void {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer);
    pollTimer = undefined;
  }
}

/** 读取并展示一个评测 attempt 关联的 trace。 */
async function inspectTrace(traceId: string | null): Promise<void> {
  if (!traceId) {
    return;
  }
  historyDetailTab.value = "trace";
  error.value = "";
  traceDetail.value = null;
  try {
    traceDetail.value = await api<TraceDetail>(
      `/api/eval/traces/${encodeURIComponent(traceId)}`,
    );
  } catch (reason) {
    error.value = `读取运行轨迹失败：${String(reason)}`;
  }
}

/** 切换到历史详情页签；首次进入轨迹时自动打开第一条可用 trace。 */
function selectHistoryDetailTab(
  tab: "matrix" | "baseline" | "judgment" | "trace",
): void {
  historyDetailTab.value = tab;
  if (tab === "trace" && !traceDetail.value) {
    const attempt = selectedRun.value?.attempts.find((item) => item.trace_id);
    void inspectTrace(attempt?.trace_id ?? null);
  }
}

/** 读取评测 trace 的 artifact，并返回文本或格式化 JSON。 */
async function loadTraceArtifactContent(
  traceId: string,
  artifact: TraceArtifact,
): Promise<string> {
  if (!connection.value) {
    throw new Error("评测服务尚未连接");
  }
  if (artifact.state !== "stored") {
    return artifact.state === "omitted"
      ? "当前 trace 模式未保存正文，仅保留内容 hash。"
      : "运行内容写入失败，正文不可用。";
  }
  const response = await fetchWithTimeout(
    `${connection.value.baseUrl}/api/eval/traces/${encodeURIComponent(traceId)}/artifacts/${encodeURIComponent(artifact.artifact_id)}`,
    {
      headers: headers(),
      cache: "no-store",
    },
  );
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  const text = await response.text();
  return artifact.mime_type.includes("json")
    ? JSON.stringify(JSON.parse(text) as unknown, null, 2)
    : text;
}

/** 切换查看的 ExecutionRun。 */
function selectRun(run: RunView): void {
  selectedRun.value = run;
  evaluationRevisions.value = [];
  evaluationRevision.value = null;
  isJudgmentFormOpen.value = false;
  judgeReviewConfirmed.value = false;
  traceDetail.value = null;
  baselineHistory.value = [];
  currentBaseline.value = null;
  baselineComparison.value = null;
  baselineError.value = "";
  isBaselineFormOpen.value = false;
  closeMatrixJudgment();
  closeMatrixTrace();
  historyDetailTab.value = "matrix";
  stopJudgmentPolling();
  schedulePolling();
  void loadEvaluations(run.run_id);
}

/** 把 attempt 起止时间格式化成秒级耗时。 */
function durationLabel(attempt: AttemptView): string {
  if (!attempt.finished_at) {
    return "运行中";
  }
  const milliseconds =
    new Date(attempt.finished_at).getTime() -
    new Date(attempt.started_at).getTime();
  return `${(milliseconds / 1000).toFixed(1)}s`;
}

/** 把结果矩阵记录的毫秒耗时格式化为紧凑文本。 */
function durationMsLabel(milliseconds: number | null): string {
  if (milliseconds === null) {
    return "运行中";
  }
  if (milliseconds < 1_000) {
    return `${milliseconds}ms`;
  }
  return `${(milliseconds / 1_000).toFixed(1)}s`;
}

/** 把首个正文 token 与请求开始时间之差格式化。 */
function firstTokenLabel(attempt: AttemptView): string {
  if (!attempt.first_token_at) {
    return "—";
  }
  const milliseconds =
    new Date(attempt.first_token_at).getTime() -
    new Date(attempt.started_at).getTime();
  return `${(milliseconds / 1000).toFixed(1)}s`;
}

/** 使用用户本地格式展示 token 数量。 */
function formatTokens(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(value);
}

/** 把 run 时间按浏览器本地时区格式化为年月日和时分秒。 */
function formatRunDate(value: string | null): string {
  if (!value) {
    return "—";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

/** 把两个 ISO 时间之间的差值格式化为易读耗时。 */
function durationBetweenText(start: string, end: string): string {
  const elapsed = Math.max(
    0,
    new Date(end).getTime() - new Date(start).getTime(),
  );
  if (elapsed < 1_000) {
    return `${elapsed} 毫秒`;
  }
  const seconds = Math.floor(elapsed / 1_000);
  if (seconds < 60) {
    return `${seconds} 秒`;
  }
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  if (minutes < 60) {
    return `${minutes} 分 ${remainingSeconds} 秒`;
  }
  const hours = Math.floor(minutes / 60);
  return `${hours} 小时 ${minutes % 60} 分`;
}

/** 计算历史 run 从开始到封存／最终状态更新的耗时。 */
function runDurationText(run: RunView): string {
  if (["planned", "running", "paused"].includes(run.status)) {
    return "进行中";
  }
  const startedAt = run.started_at ?? run.created_at;
  const finishedAt = run.sealed_at ?? run.updated_at;
  return durationBetweenText(startedAt, finishedAt);
}

onMounted(() => {
  void connectAndLoad();
});

onBeforeUnmount(() => {
  stopPolling();
  stopJudgmentPolling();
  document.body.style.overflow = previousBodyOverflow;
  document.documentElement.style.overflow = previousDocumentOverflow;
});
</script>

<template>
  <section class="eval-page">
    <header class="eval-heading">
      <div>
        <p class="eval-eyebrow">仅开发构建</p>
        <h2>评测执行台</h2>
        <p class="eval-lede">
          固定用例集修订版、知识库快照和 Agent 版本，直接复用产品 Agent 内核执行全新会话。
        </p>
      </div>
      <span class="eval-build-badge">开发构建</span>
    </header>

    <p v-if="loading" class="eval-notice">正在启动独立评测服务…</p>
    <p v-if="error" class="eval-error">{{ error }}</p>
    <p v-if="message" class="eval-success">{{ message }}</p>

    <nav class="eval-workspace-tabs" aria-label="评测工作区">
      <button
        type="button"
        :class="{ active: evaluationTab === 'cases' }"
        @click="selectEvaluationTab('cases')"
      >
        <strong>用例</strong>
        <small>编辑草稿、审核并激活修订版</small>
      </button>
      <button
        type="button"
        :class="{ active: evaluationTab === 'suites' }"
        @click="selectEvaluationTab('suites')"
      >
        <strong>用例集</strong>
        <small>选择用例子集并发布套件</small>
      </button>
      <button
        type="button"
        :class="{ active: evaluationTab === 'execute' }"
        @click="selectEvaluationTab('execute')"
      >
        <strong>执行</strong>
        <small>正式评测、快速试跑和当前运行</small>
      </button>
      <button
        type="button"
        :class="{ active: evaluationTab === 'history' }"
        @click="selectEvaluationTab('history')"
      >
        <strong>历史结果</strong>
        <small>已完成批次、样本和运行轨迹</small>
      </button>
    </nav>

    <div
      v-if="!loading && connection"
      class="eval-layout"
      :class="{ 'eval-layout-wide': evaluationTab === 'execute' }"
    >
      <aside v-show="evaluationTab !== 'execute'" class="eval-sidebar">
        <section v-show="evaluationTab === 'suites'" class="eval-panel">
          <div class="eval-panel-heading">
            <div>
              <span class="eval-kicker">评测输入</span>
              <h3>用例与金标准</h3>
            </div>
            <span class="eval-count">{{ selectedSuite?.case_count ?? 0 }}</span>
          </div>
          <label class="eval-field">
            <span>用例集修订版</span>
            <select v-model="selectedSuiteRevisionId">
              <option
                v-for="suite in suites"
                :key="suite.revision_id"
                :value="suite.revision_id"
              >
                {{ suite.name }} · 第 {{ suite.revision_number }} 版 ·
                {{ suite.revision_id }}{{ suite.stale ? " · 已过期" : "" }}
              </option>
            </select>
          </label>
          <div class="eval-suite-meta">
            <span>{{ selectedSuite?.case_count }} 条用例</span>
            <span>{{ selectedSuite?.scope_id }}</span>
            <span>{{ statusText(selectedSuite?.review_status ?? "") }}</span>
          </div>
          <p v-if="selectedSuite?.stale" class="eval-stale-notice">
            用例集已过期：{{
              selectedSuite.stale_reasons.map(staleReasonText).join("；")
            }}。请派生新修订版后启用。
          </p>
          <div class="eval-actions">
            <button
              type="button"
              :disabled="busy || !selectedSuite"
              @click="beginSuiteDraft()"
            >
              编辑当前用例集
            </button>
            <button type="button" :disabled="busy" @click="beginNewSuiteDraft">
              新建用例集
            </button>
          </div>
          <details
            v-for="item in selectedSuite?.cases ?? []"
            :key="item.revision_id"
            class="eval-case"
          >
            <summary>
              <span class="eval-case-id">{{ item.case_id }}</span>
              <span>{{ item.question }}</span>
              <span class="eval-case-count">{{ item.criteria.length }} 项</span>
            </summary>
            <div class="eval-case-body">
              <p>{{ item.category }} · N={{ item.n }} / K={{ item.k }}</p>
              <ul>
                <li
                  v-for="criterion in item.criteria"
                  :key="criterion.criterion_id"
                  :class="`criterion-${criterion.rule_type.toLowerCase()}`"
                >
                  <strong>{{ criterion.rule_type }} · {{ criterion.title }}</strong>
                  <span>{{ criterion.rule_text }}</span>
                </li>
              </ul>
            </div>
          </details>
        </section>

        <section v-show="evaluationTab === 'cases'" class="eval-panel">
          <div class="eval-panel-heading">
            <div>
              <span class="eval-kicker">草稿与审核</span>
              <h3>用例修订</h3>
            </div>
          </div>
          <label class="eval-field">
            <span>操作人／审核人</span>
            <input v-model="reviewerName" maxlength="128" />
          </label>
          <button
            type="button"
            class="eval-primary eval-full-button"
            :disabled="busy"
            @click="createNewCase"
          >
            新建用例
          </button>
          <button
            v-for="item in caseCatalog"
            :key="item.case_id"
            type="button"
            class="eval-case-list-row"
            :class="{ active: selectedCaseId === item.case_id }"
            @click="selectCase(item)"
          >
            <span class="eval-case-id">{{ item.case_id }}</span>
            <span class="eval-case-list-question">
              {{ item.revisions[0]?.question }}
            </span>
            <span class="eval-case-list-status">
              {{
                item.has_draft
                  ? "有草稿"
                  : statusText(item.revisions[0]?.review_status ?? "")
              }}
            </span>
          </button>
        </section>

        <section v-show="evaluationTab === 'history'" class="eval-panel">
          <div class="eval-panel-heading">
            <div>
              <span class="eval-kicker">最近记录</span>
              <h3>最近执行批次</h3>
            </div>
          </div>
          <label class="eval-field">
            <span>按用例集筛选</span>
            <select v-model="historySuiteFilter">
              <option value="all">全部历史批次</option>
              <option value="quick">快速试跑</option>
              <option
                v-for="option in historySuiteOptions"
                :key="option.suiteId"
                :value="option.suiteId"
              >
                {{ option.name }}
              </option>
            </select>
          </label>
          <p v-if="!filteredHistoryRuns.length" class="eval-empty">
            当前筛选下没有历史运行记录。
          </p>
          <button
            v-for="item in filteredHistoryRuns"
            :key="item.run_id"
            type="button"
            class="eval-run-link"
            :class="{ active: selectedRun?.run_id === item.run_id }"
            @click="selectRun(item)"
          >
            <span class="eval-run-title">
              {{
                item.suite_name ??
                (item.plan_mode === "exploratory" ? "快速试跑" : "未知用例集")
              }}
            </span>
            <strong>{{ statusText(item.status) }}</strong>
            <small>
              {{ item.run_id }}
              ·
              {{
                item.plan_mode === "exploratory"
                  ? "探索性"
                  : `第 ${item.suite_revision_number ?? "?"} 版`
              }}
              · {{ item.totals.attempts }} 个执行样本 / 有效
              {{ item.totals.valid_samples ?? 0 }}
            </small>
            <small>
              {{ formatRunDate(item.started_at ?? item.created_at) }}
              <template v-if="item.sealed_at">
                → {{ formatRunDate(item.sealed_at) }}
              </template>
              · 耗时 {{ runDurationText(item) }}
            </small>
          </button>
        </section>
      </aside>

      <div class="eval-main">
        <section
          v-if="caseDraft"
          v-show="evaluationTab === 'cases'"
          class="eval-panel eval-editor-panel"
          @input="markCaseDraftDirty"
          @change="markCaseDraftDirty"
        >
          <div class="eval-panel-heading">
            <div>
              <span class="eval-kicker">用例草稿</span>
              <h3>
                {{ caseDraft.case_id }} · 即将生成第
                {{ caseDraft.next_revision_number }} 版
              </h3>
              <small
                class="eval-save-state"
                :class="{ error: caseDraftSaveError, dirty: caseDraftDirty }"
              >
                {{ caseDraftSaveState }}
              </small>
            </div>
            <div class="eval-heading-actions">
              <button
                type="button"
                :disabled="busy"
                @click="saveCaseDraft()"
              >
                保存草稿
              </button>
              <button
                type="button"
                class="eval-primary"
                :disabled="busy || !caseDraft.criteria.length"
                @click="publishCaseDraft"
              >
                提交审核并生成修订版
              </button>
              <button type="button" class="eval-close-button" @click="caseDraft = null">
                收起
              </button>
            </div>
          </div>
          <p v-if="caseDraftSaveError" class="eval-inline-error">
            {{ caseDraftSaveError }}
          </p>
          <details open class="eval-diff">
            <summary>
              <strong>提交审核前差异</strong>
              <span>
                {{
                  caseDraftDiff.length
                    ? `${caseDraftDiff.length} 项变化`
                    : "暂无字段或判定项变化"
                }}
              </span>
            </summary>
            <p v-if="!caseDraftDiff.length" class="eval-empty">
              当前草稿与基准修订版内容一致。
            </p>
            <article
              v-for="item in caseDraftDiff"
              :key="`${item.kind}-${item.label}`"
              class="eval-diff-item"
            >
              <div>
                <strong>{{ item.label }}</strong>
                <span>{{ diffKindText(item.kind) }}</span>
              </div>
              <div v-if="item.before !== undefined" class="eval-diff-before">
                <small>修改前</small>
                <pre>{{ item.before }}</pre>
              </div>
              <div v-if="item.after !== undefined" class="eval-diff-after">
                <small>修改后</small>
                <pre>{{ item.after }}</pre>
              </div>
            </article>
          </details>
          <div class="eval-form-grid">
            <label class="eval-field">
              <span>类别</span>
              <input v-model="caseDraft.category" />
            </label>
            <label class="eval-field">
              <span>N</span>
              <input v-model.number="caseDraft.n" type="number" min="1" max="100" />
            </label>
            <label class="eval-field">
              <span>K</span>
              <input v-model.number="caseDraft.k" type="number" min="1" max="100" />
            </label>
            <label class="eval-field eval-field-wide">
              <span>问题</span>
              <textarea v-model="caseDraft.question" rows="3"></textarea>
            </label>
            <label class="eval-field eval-field-wide">
              <span>目标文档与章节（每行“标题 | 章节／子章节”，可省略章节）</span>
              <textarea v-model="caseTargetTitles" rows="3"></textarea>
            </label>
            <label class="eval-field eval-field-wide">
              <span>干扰元数据（JSON）</span>
              <textarea v-model="caseDistractorMetadata" rows="3"></textarea>
            </label>
            <label class="eval-field eval-field-wide">
              <span>执行策略（JSON）</span>
              <textarea v-model="caseExecutionPolicy" rows="3"></textarea>
            </label>
            <label class="eval-field eval-field-wide">
              <span>升级原因／修订备注</span>
              <textarea
                v-model="caseDraft.change_note"
                rows="3"
                placeholder="说明为什么升级，以及本次修改了哪些内容、原因是什么。"
              ></textarea>
            </label>
          </div>

          <div class="eval-criterion-editor">
            <div class="eval-panel-heading">
              <h3>R／X 判定项</h3>
              <div class="eval-actions">
                <button type="button" @click="addDraftCriterion('R')">添加 R</button>
                <button type="button" @click="addDraftCriterion('X')">添加 X</button>
              </div>
            </div>
            <p v-if="!caseDraft.criteria.length" class="eval-empty">
              至少添加一条判定项后才能提交审核。
            </p>
            <article
              v-for="(criterion, index) in caseDraft.criteria"
              :key="criterion.criterion_id"
              class="eval-criterion-editor-row"
            >
              <div class="eval-criterion-row-head">
                <select v-model="criterion.rule_type">
                  <option value="R">R 必选</option>
                  <option value="X">X 边界</option>
                </select>
                <input v-model="criterion.title" placeholder="判定项标题" />
                <button type="button" class="danger" @click="removeDraftCriterion(index)">
                  删除
                </button>
              </div>
              <textarea
                v-model="criterion.rule_text"
                rows="3"
                placeholder="判定标准或判定动作"
              ></textarea>
              <div class="eval-criterion-row-foot">
                <select v-model="criterion.evaluator">
                  <option value="human">人工判定</option>
                  <option value="checker">程序判定器</option>
                  <option value="llm_judge">模型判官</option>
                </select>
                <code>{{ criterion.criterion_id }}</code>
              </div>
            </article>
          </div>
        </section>

        <section
          v-if="suiteDraft"
          v-show="evaluationTab === 'suites'"
          class="eval-panel eval-editor-panel"
        >
          <div class="eval-panel-heading">
            <div>
              <span class="eval-kicker">用例集草稿</span>
              <h3>
                {{ suiteDraft.name }} · 即将生成第
                {{ suiteDraft.next_revision_number }} 版
              </h3>
            </div>
            <button type="button" class="eval-close-button" @click="suiteDraft = null">
              收起
            </button>
          </div>
          <div class="eval-form-grid">
            <label class="eval-field">
              <span>名称</span>
              <input v-model="suiteDraft.name" />
            </label>
            <label class="eval-field">
              <span>范围</span>
              <input v-model="suiteDraft.scope_id" />
            </label>
            <label class="eval-field">
              <span>修订说明</span>
              <input v-model="suiteDraft.change_note" />
            </label>
            <label class="eval-field eval-field-wide">
              <span>描述</span>
              <textarea v-model="suiteDraft.description" rows="2"></textarea>
            </label>
          </div>
          <div class="eval-suite-case-picker">
            <p>
              已选择 {{ suiteDraftCaseIds.length }} 条用例，
              预计
              {{
                suiteActiveCaseOptions
                  .filter((item) => suiteDraftCaseIds.includes(item.revision_id))
                  .reduce((total, item) => total + item.n, 0)
              }}
              个执行样本
            </p>
            <label
              v-for="revision in suiteActiveCaseOptions"
              :key="revision.revision_id"
              class="eval-select-row"
            >
              <input
                type="checkbox"
                :checked="suiteDraftCaseIds.includes(revision.revision_id)"
                @change="toggleSuiteDraftCase(revision)"
              />
              <span><strong>{{ revision.case_id }}</strong> {{ revision.question }}</span>
              <small>N={{ revision.n }} / K={{ revision.k }}</small>
            </label>
          </div>
          <div class="eval-actions">
            <button type="button" :disabled="busy" @click="saveSuiteDraft()">
              保存草稿
            </button>
            <button
              type="button"
              class="eval-primary"
              :disabled="busy || !suiteDraftCaseIds.length"
              @click="publishSuiteDraft"
            >
              发布并启用用例集
            </button>
          </div>
        </section>

        <section
          v-if="evaluationTab === 'cases' && !caseDraft"
          class="eval-panel"
        >
          <template v-if="selectedCaseCatalog">
            <div class="eval-panel-heading">
              <div>
                <span class="eval-kicker">用例详情</span>
                <h3>{{ selectedCaseCatalog.case_id }}</h3>
              </div>
              <div class="eval-heading-actions">
                <span class="eval-count">
                  {{
                    statusText(selectedCaseRevision?.review_status ?? "无修订版")
                  }}
                </span>
                <button
                  v-if="selectedCaseCatalog.has_draft"
                  type="button"
                  class="eval-primary"
                  :disabled="busy"
                  @click="beginCaseDraft(selectedCaseCatalog.case_id)"
                >
                  继续编辑草稿
                </button>
                <button
                  v-else-if="selectedCaseRevision"
                  type="button"
                  class="eval-primary"
                  :disabled="busy"
                  @click="
                    beginCaseDraft(
                      selectedCaseCatalog.case_id,
                      selectedCaseRevision.revision_id,
                    )
                  "
                >
                  从此修订版派生草稿
                </button>
                <button
                  v-if="selectedCaseRevision?.review_status === 'reviewed'"
                  type="button"
                  :disabled="busy"
                  @click="activateCaseRevision(selectedCaseRevision)"
                >
                  激活此修订版
                </button>
                <button
                  v-if="selectedCaseRevision?.review_status === 'reviewed'"
                  type="button"
                  class="danger"
                  :disabled="busy"
                  @click="retireCaseRevision(selectedCaseRevision)"
                >
                  停用此修订版
                </button>
              </div>
            </div>
            <label class="eval-field eval-field-wide">
              <span>查看修订版</span>
              <select v-model="selectedCaseRevisionId">
                <option
                  v-for="revision in selectedCaseCatalog.revisions"
                  :key="revision.revision_id"
                  :value="revision.revision_id"
                >
                  第 {{ revision.revision_number }} 版 ·
                  {{ statusText(revision.review_status) }} ·
                  {{ revision.revision_id }}
                </option>
              </select>
            </label>
            <template v-if="selectedCaseRevision">
              <div class="eval-detail-meta">
                <span>{{ selectedCaseRevision.category }}</span>
                <span>N={{ selectedCaseRevision.n }}</span>
                <span>K={{ selectedCaseRevision.k }}</span>
                <span>修订版 {{ selectedCaseRevision.revision_id }}</span>
              </div>
              <section class="eval-readonly-section">
                <span>问题</span>
                <p>{{ selectedCaseRevision.question }}</p>
              </section>
              <section class="eval-readonly-section">
                <span>目标文档与章节</span>
                <ul>
                  <li
                    v-for="(target, index) in selectedCaseRevision.target_bindings"
                    :key="`${target.title_contains}-${index}`"
                  >
                    {{ target.title_contains }}
                    <template v-if="target.section_path?.length">
                      · {{ target.section_path.join(" / ") }}
                    </template>
                  </li>
                </ul>
                <p v-if="!selectedCaseRevision.target_bindings.length">
                  全库检索，不限定目标文档。
                </p>
              </section>
              <section class="eval-readonly-section">
                <span>R／X 判定项</span>
                <ul class="eval-readonly-criteria">
                  <li
                    v-for="criterion in selectedCaseRevision.criteria"
                    :key="criterion.criterion_id"
                  >
                    <strong>{{ criterion.rule_type }} · {{ criterion.title }}</strong>
                    <p>{{ criterion.rule_text }}</p>
                    <small>
                      {{ criterion.criterion_id }} ·
                      {{ executorText(criterion.evaluator) }} ·
                      修订 {{ criterion.revision_id }}
                    </small>
                  </li>
                </ul>
              </section>
              <p class="eval-change-note">
                升级原因／修订备注：{{
                  selectedCaseRevision.change_note || "未填写"
                }}
              </p>
            </template>
            <p v-else class="eval-empty">该用例还没有可查看的修订版。</p>
          </template>
          <template v-else>
            <span class="eval-kicker">用例详情</span>
            <h3>从左侧选择一个用例</h3>
            <p class="eval-placeholder-copy">
              右侧将先展示当前修订版的只读内容，再由用户选择派生草稿或继续编辑。
            </p>
          </template>
        </section>

        <section
          v-if="evaluationTab === 'suites' && !suiteDraft"
          class="eval-panel eval-placeholder"
        >
          <span class="eval-kicker">用例集编辑</span>
          <h3>选择当前用例集或新建一个用例集</h3>
          <p>用例集只引用生效中的修订版，并在发布后冻结成员。</p>
          <div class="eval-actions">
            <button
              type="button"
              :disabled="busy || !selectedSuite"
              @click="beginSuiteDraft()"
            >
              编辑当前用例集
            </button>
            <button type="button" :disabled="busy" @click="beginNewSuiteDraft">
              新建用例集
            </button>
          </div>
        </section>

        <section v-show="evaluationTab === 'execute'" class="eval-panel">
          <div class="eval-panel-heading">
            <div>
              <span class="eval-kicker">执行准备</span>
              <h3>冻结现场并预检</h3>
            </div>
            <span class="eval-count">{{ plannedAttempts }} 个样本</span>
          </div>
          <div class="eval-form-grid">
            <label class="eval-field eval-field-wide">
              <span>执行方式</span>
              <select v-model="executionMode">
                <option value="formal">正式评测（必须使用生效中用例集）</option>
                <option value="exploratory">快速试跑（不生成正式结论）</option>
              </select>
            </label>
            <label
              v-if="executionMode === 'formal'"
              class="eval-field eval-field-wide"
            >
              <span>正式评测用例集</span>
              <select v-model="selectedSuiteRevisionId">
                <option v-if="!selectableFormalSuites.length" value="" disabled>
                  暂无可用的生效中用例集
                </option>
                <option
                  v-for="suite in selectableFormalSuites"
                  :key="suite.revision_id"
                  :value="suite.revision_id"
                >
                  {{ suite.name }} · 第 {{ suite.revision_number }} 版 ·
                  {{ suite.revision_id }}
                </option>
              </select>
              <small v-if="!selectableFormalSuites.length">
                请先在“用例集”页发布可用的生效中用例集。
              </small>
            </label>
            <label class="eval-field eval-field-wide">
              <span>知识库快照</span>
              <input
                v-model="snapshotId"
                placeholder="s_0123456789abcdef"
                spellcheck="false"
              />
              <small v-if="currentCollection">
                当前活动快照：{{ currentCollection.manifest.snapshot_id }} ·
                {{ currentCollection.manifest.documents.length }} 篇文档
              </small>
            </label>
            <label class="eval-field">
              <span>运行类型</span>
              <select v-model="runType">
                <option value="quality">质量评测</option>
                <option value="latency">首字延迟评测</option>
              </select>
            </label>
            <label class="eval-field">
              <span>并发执行样本</span>
              <input
                v-model.number="concurrency"
                type="number"
                min="1"
                max="32"
                :disabled="runType === 'latency'"
              />
            </label>
            <label v-if="executionMode === 'exploratory'" class="eval-field">
              <span>试跑 N（不改变 K）</span>
              <input v-model.number="quickN" type="number" min="1" max="100" />
            </label>
            <label class="eval-field">
              <span>累计 token 预算</span>
              <input v-model.number="totalTokenBudget" type="number" min="1" />
            </label>
            <label class="eval-field">
              <span>模型请求预算</span>
              <input v-model.number="modelRequestBudget" type="number" min="1" />
            </label>
            <label class="eval-field">
              <span>替代执行样本上限</span>
              <input
                v-model.number="replacementAttemptBudget"
                type="number"
                min="0"
                max="500"
              />
            </label>
            <label class="eval-field">
              <span>单个执行样本超时（秒）</span>
              <input
                v-model.number="attemptTimeoutSeconds"
                type="number"
                min="30"
                max="3600"
              />
            </label>
          </div>
          <div v-if="executionMode === 'exploratory'" class="eval-quick-picker">
            <p>
              每个用例可选择任意一条已审核或生效中的修订版；草稿与已停用版本不可选。
            </p>
            <details
              v-for="group in quickCaseGroups"
              :key="group.caseId"
              class="eval-quick-case-group"
            >
              <summary>
                <span class="eval-case-id">{{ group.caseId }}</span>
                <span>{{ group.revisions[0]?.question }}</span>
                <small>
                  <template
                    v-if="quickSelectedRevisionNumber(group.revisions) !== null"
                  >
                    已选第
                    {{ quickSelectedRevisionNumber(group.revisions) }} 版
                  </template>
                  <template v-else>未选择</template>
                </small>
              </summary>
              <label
                v-for="revision in group.revisions"
                :key="revision.revision_id"
                class="eval-select-row"
              >
                <input
                  type="checkbox"
                  :checked="quickCaseRevisionIds.includes(revision.revision_id)"
                  @change="toggleQuickCase(revision)"
                />
                <span>
                  第 {{ revision.revision_number }} 版 ·
                  {{ statusText(revision.review_status) }}
                </span>
                <small>
                  源 N={{ revision.n }} / K={{ revision.k }} · {{ revision.revision_id }}
                </small>
              </label>
            </details>
          </div>
          <p
            v-if="executionMode === 'formal' && selectedSuite?.stale"
            class="eval-stale-notice"
          >
            当前用例集已过期，不能启动新的正式评测。请先发布新的用例集修订版。
          </p>
          <div class="eval-actions">
            <button
              type="button"
              class="eval-primary"
              :disabled="
                busy ||
                !snapshotId ||
                (executionMode === 'formal' &&
                  (!selectedSuiteRevisionId || selectedSuite?.stale)) ||
                (executionMode === 'exploratory' &&
                  quickCaseRevisionIds.length === 0)
              "
              @click="runPreflight"
            >
              {{ busy ? "检查中…" : "生成预检计划" }}
            </button>
          </div>

          <template v-if="plan">
            <div
              class="eval-plan-status"
              :class="plan.status === 'planned' ? 'passed' : 'failed'"
            >
              <strong>{{ plan.status === "planned" ? "预检通过" : "预检失败" }}</strong>
              <span>{{ plan.plan_id }}</span>
              <span>{{ plan.collection_snapshot_id }}</span>
              <span>
                {{ plan.mode === "exploratory" ? "快速试跑" : "正式评测" }} ·
                {{ runTypeText(plan.run_type) }} · 并发 {{ plan.concurrency }}
              </span>
              <span>{{ plan.cases.length }} 条用例</span>
              <span>预计 {{ plannedAttempts }} 个执行样本</span>
            </div>
            <ul class="eval-checks">
              <li
                v-for="check in plan.checks"
                :key="check.code"
                :class="check.status"
              >
                <strong>{{ check.code }}</strong>
                <span>{{ check.message }}</span>
              </li>
            </ul>
            <div class="eval-plan-cases">
              <strong class="eval-plan-cases-heading">
                计划用例 · 有效 N/K 与源 N/K
              </strong>
              <article
                v-for="planCase in plan.cases"
                :key="planCase.case_revision_id"
              >
                <div>
                  <strong>{{ planCase.case_id }}</strong>
                  <span>
                    有效 N={{ planCase.n }} / K={{ planCase.k }} · 源 N={{
                      planCase.source_n
                    }} / K={{ planCase.source_k }}
                  </span>
                </div>
                <small v-if="plan.mode === 'exploratory' && planCase.n < planCase.k">
                  探索性执行：N 小于 K，不产生正式通过结论
                </small>
                <small v-else-if="plan.mode === 'exploratory'">
                  快速试跑：结果不进入正式基线
                </small>
              </article>
            </div>
            <button
              type="button"
              class="eval-primary"
              :disabled="busy || plan.status !== 'planned' || Boolean(activeRun)"
              @click="startRun"
            >
              启动执行批次（{{ plannedAttempts }} 个样本）
            </button>
          </template>
          <p v-else-if="failedChecks.length" class="eval-error">
            {{ failedChecks.length }} 个预检项失败。
          </p>
        </section>

        <section
          v-if="selectedRun"
          v-show="['execute', 'history'].includes(evaluationTab)"
          class="eval-panel eval-live-panel"
        >
          <div class="eval-live-head">
            <div>
              <span class="eval-kicker">执行批次</span>
              <h3 class="eval-live-title">
                <strong>
                  {{
                    selectedRun.suite_name ??
                    (selectedRun.plan_mode === "exploratory"
                      ? "快速试跑"
                      : "未知用例集")
                  }}
                </strong>
                <span>
                  {{ formatRunDate(selectedRun.started_at ?? selectedRun.created_at) }}
                  <template v-if="selectedRun.sealed_at">
                    → {{ formatRunDate(selectedRun.sealed_at) }}
                  </template>
                </span>
              </h3>
              <small class="eval-run-source">
                {{ selectedRun.run_id }}
                ·
                {{
                  selectedRun.plan_mode === "exploratory"
                    ? "探索性"
                    : `第 ${selectedRun.suite_revision_number ?? "?"} 版 · ${
                        selectedRun.suite_revision_id ?? ""
                      }`
                }}
              </small>
              <small class="eval-run-time">
                耗时 {{ runDurationText(selectedRun) }}
              </small>
            </div>
            <span class="eval-run-state" :class="selectedRun.status">
              {{ runStatusLabel }}
            </span>
          </div>
          <p v-if="selectedRun.pause_reason" class="eval-pause-reason">
            暂停原因：{{ pauseReasonText(selectedRun.pause_reason) }}
          </p>
          <div class="eval-metrics">
            <div>
              <span>执行样本</span>
              <strong>{{ selectedRun.totals.attempts }}</strong>
            </div>
            <div>
              <span>有效样本</span>
              <strong>{{ selectedRun.totals.valid_samples ?? 0 }}</strong>
            </div>
            <div>
              <span>输入 token</span>
              <strong>{{ formatTokens(selectedRun.totals.input_tokens) }}</strong>
            </div>
            <div>
              <span>输出 token</span>
              <strong>{{ formatTokens(selectedRun.totals.output_tokens) }}</strong>
            </div>
            <div>
              <span>模型调用</span>
              <strong>{{ selectedRun.totals.model_calls }}</strong>
            </div>
          </div>
          <div class="eval-actions">
            <button
              v-if="['planned', 'running'].includes(selectedRun.status)"
              type="button"
              :disabled="busy"
              @click="controlRun('pause')"
            >
              暂停
            </button>
            <button
              v-if="selectedRun.status === 'paused'"
              type="button"
              :disabled="busy"
              @click="controlRun('resume')"
            >
              继续
            </button>
            <button
              v-if="['planned', 'running', 'paused'].includes(selectedRun.status)"
              type="button"
              class="danger"
              :disabled="busy"
              @click="controlRun('cancel')"
            >
              取消
            </button>
          </div>

          <div class="eval-attempts">
            <div
              v-for="attempt in selectedRun.attempts"
              :key="attempt.attempt_id"
              class="eval-attempt-row"
            >
              <div>
                <strong>
                  {{ caseById.get(attempt.case_revision_id)?.case_id ?? attempt.case_revision_id }}
                </strong>
                <small>{{ attempt.attempt_id.slice(0, 22) }}…</small>
              </div>
              <span class="eval-attempt-status" :class="attempt.execution_status">
                {{ statusText(attempt.execution_status) }}
                <small>{{ outcomeClassText(attempt.outcome_class) }}</small>
              </span>
              <span>首字 {{ firstTokenLabel(attempt) }}</span>
              <span>耗时 {{ durationLabel(attempt) }}</span>
              <span>
                {{ formatTokens(attempt.input_tokens + attempt.output_tokens) }} token
              </span>
              <button
                type="button"
                class="eval-trace-link"
                :disabled="!attempt.trace_id"
                @click="inspectTrace(attempt.trace_id)"
              >
                运行轨迹 · {{ statusText(attempt.trace_status) }}
              </button>
            </div>
          </div>
          <nav
            v-if="evaluationTab === 'history'"
            class="eval-history-tabs"
            aria-label="历史结果详情"
          >
            <button
              type="button"
              :class="{ active: historyDetailTab === 'matrix' }"
              @click="selectHistoryDetailTab('matrix')"
            >
              结果矩阵
            </button>
            <button
              type="button"
              :class="{ active: historyDetailTab === 'baseline' }"
              @click="selectHistoryDetailTab('baseline')"
            >
              基线比较
            </button>
            <button
              type="button"
              :class="{ active: historyDetailTab === 'judgment' }"
              @click="selectHistoryDetailTab('judgment')"
            >
              判定结果
            </button>
            <button
              type="button"
              :class="{ active: historyDetailTab === 'trace' }"
              @click="selectHistoryDetailTab('trace')"
            >
              运行轨迹
            </button>
          </nav>
        </section>

        <section
          v-show="
            evaluationTab === 'history' &&
            selectedRun &&
            historyDetailTab === 'matrix'
          "
          class="eval-panel eval-result-matrix"
        >
          <div class="eval-panel-heading">
            <div>
              <span class="eval-kicker">结果矩阵</span>
              <h3>从批次、类别和样本定位结果</h3>
            </div>
            <span class="eval-count">
              {{ evaluationRevision ? statusText(evaluationRevision.status) : "尚未判定" }}
            </span>
          </div>

          <p v-if="!evaluationRevision" class="eval-empty">
            当前运行尚未生成判定结果。先完成判定，才能查看 case 与 attempt 的质量矩阵；
            token、工具和耗时仍可在上方执行信息中查看。
          </p>
          <p v-else-if="!resultMatrix" class="eval-empty">
            结果矩阵正在生成。
          </p>
          <template v-else>
            <div class="eval-matrix-summary">
              <article>
                <span>Run 结论</span>
                <strong>
                  {{ resultMatrix.totals.case_pass_count }} 通过 /
                  {{ resultMatrix.totals.case_fail_count }} 失败 /
                  {{ resultMatrix.totals.case_pending_count }} 待定
                </strong>
                <small>
                  {{ resultMatrix.totals.case_exploratory_count }} 条探索性 ·
                  共 {{ resultMatrix.totals.case_count }} 个 case
                </small>
              </article>
              <article>
                <span>有效样本</span>
                <strong>
                  {{ resultMatrix.totals.valid_samples }} /
                  {{ resultMatrix.totals.attempt_count }}
                </strong>
                <small>
                  基础设施失败 {{ resultMatrix.totals.infrastructure_failures }} ·
                  未知 {{ resultMatrix.totals.unknown_attempts }} ·
                  取消 {{ resultMatrix.totals.canceled_attempts }}
                </small>
              </article>
              <article :class="{ warning: resultMatrix.totals.redline_violation_count > 0 }">
                <span>红线失败样本</span>
                <strong>{{ resultMatrix.totals.redline_violation_count }}</strong>
                <small>
                  X 违规 {{ resultMatrix.totals.x_violation_count }} ·
                  跨项失败 {{ resultMatrix.totals.check_failure_count }}
                </small>
              </article>
              <article>
                <span>Token</span>
                <strong>
                  {{ formatTokens(resultMatrix.totals.input_tokens + resultMatrix.totals.output_tokens) }}
                </strong>
                <small>
                  输入 {{ formatTokens(resultMatrix.totals.input_tokens) }} ·
                  输出 {{ formatTokens(resultMatrix.totals.output_tokens) }}
                </small>
              </article>
              <article>
                <span>模型 / 工具调用</span>
                <strong>
                  {{ resultMatrix.totals.model_calls }} /
                  {{ resultMatrix.totals.tool_calls }}
                </strong>
                <small>按全部 attempt 统计，不混入有效 N</small>
              </article>
              <article>
                <span>样本累计耗时</span>
                <strong>{{ durationMsLabel(resultMatrix.totals.duration_ms) }}</strong>
                <small>各 attempt 起止时间之和</small>
              </article>
            </div>

            <p v-if="!resultMatrix.categories.length" class="eval-empty">
              当前运行没有可展示的用例。
            </p>
            <section
              v-for="category in resultMatrix.categories"
              :key="category.category"
              class="eval-matrix-category"
            >
              <header>
                <div>
                  <span class="eval-kicker">问题类别</span>
                  <h4>{{ category.category }}</h4>
                </div>
                <div class="eval-matrix-category-counts">
                  <span>{{ category.totals.case_count }} 个 case</span>
                  <span>通过 {{ category.totals.case_pass_count }}</span>
                  <span>失败 {{ category.totals.case_fail_count }}</span>
                  <span>待定 {{ category.totals.case_pending_count }}</span>
                  <span>探索性 {{ category.totals.case_exploratory_count }}</span>
                  <span>
                    X 违规 {{ category.totals.x_violation_count }} ·
                    跨项失败 {{ category.totals.check_failure_count }} ·
                    红线失败 {{ category.totals.redline_violation_count }}
                  </span>
                  <span>
                    token
                    {{
                      formatTokens(
                        category.totals.input_tokens + category.totals.output_tokens,
                      )
                    }}
                  </span>
                  <span>
                    模型 / 工具 {{ category.totals.model_calls }} /
                    {{ category.totals.tool_calls }}
                  </span>
                  <span>耗时 {{ durationMsLabel(category.totals.duration_ms) }}</span>
                </div>
              </header>
              <div class="eval-matrix-cases">
                <details
                  v-for="matrixCase in category.cases"
                  :key="matrixCase.case_revision_id"
                  class="eval-matrix-case"
                  :class="[
                    `matrix-result-${matrixCase.result}`,
                    matrixCaseAttentionClass(matrixCase),
                  ]"
                >
                  <summary>
                    <div class="eval-matrix-case-title">
                      <strong>{{ matrixCase.case_id }}</strong>
                      <small :title="matrixCase.question">{{ matrixCase.question }}</small>
                    </div>
                    <span :class="`result-${matrixCase.result}`">
                      {{ statusText(matrixCase.result) }}
                    </span>
                    <span>有效样本 {{ matrixCase.valid_samples }} / {{ matrixCase.n }}</span>
                    <span>
                      通过 {{ matrixCase.pass_count }} · 失败 {{ matrixCase.fail_count }} ·
                      待定 {{ matrixCase.pending_count }}
                    </span>
                    <span>
                      X 违规 {{ matrixCase.x_violation_count }} ·
                      跨项失败 {{ matrixCase.check_failure_count }} ·
                      红线失败 {{ matrixCase.redline_violation_count }}
                    </span>
                    <span>
                      {{ formatTokens(matrixCase.input_tokens + matrixCase.output_tokens) }} token
                    </span>
                    <span>
                      模型 / 工具 {{ matrixCase.model_calls }} /
                      {{ matrixCase.tool_calls }}
                    </span>
                    <span>{{ durationMsLabel(matrixCase.duration_ms) }}</span>
                  </summary>
                  <div class="eval-matrix-case-detail">
                    <p
                      v-for="reason in matrixCase.reasons"
                      :key="reason"
                      class="eval-matrix-reason"
                    >
                      {{ reason }}
                    </p>
                    <p v-if="!matrixCase.attempts.length" class="eval-empty">
                      该 case 没有执行样本。
                    </p>
                    <article
                      v-for="attempt in matrixCase.attempts"
                      :key="attempt.attempt_id"
                      class="eval-matrix-attempt"
                      :class="`matrix-result-${attempt.result}`"
                    >
                      <div class="eval-matrix-attempt-title">
                        <strong>样本 #{{ String(attempt.sample_ordinal).padStart(2, "0") }}</strong>
                        <small :title="attempt.attempt_id">{{ attemptShortId(attempt.attempt_id) }}</small>
                      </div>
                      <span :class="`result-${attempt.result}`">
                        {{ statusText(attempt.result) }}
                      </span>
                      <span>
                        {{ statusText(attempt.execution_status) }} ·
                        {{ outcomeClassText(attempt.outcome_class) }}
                      </span>
                      <template
                        v-if="
                          attempt.x_violated > 0 ||
                          attempt.check_failures.length
                        "
                      >
                        <span
                          v-if="attempt.x_violated > 0"
                          class="eval-matrix-redline"
                        >
                          X 违规 {{ attempt.x_violated }}
                        </span>
                        <span
                          v-if="attempt.check_failures.length"
                          class="eval-matrix-redline"
                        >
                          跨项失败 {{ attempt.check_failures.join("、") }}
                        </span>
                      </template>
                      <span v-else>无红线</span>
                      <span>
                        {{ formatTokens(attempt.input_tokens + attempt.output_tokens) }} token
                      </span>
                      <span>模型 {{ attempt.model_calls }} · 工具 {{ attempt.tool_calls }}</span>
                      <span>{{ durationMsLabel(attempt.duration_ms) }}</span>
                      <div class="eval-matrix-attempt-actions">
                        <button
                          type="button"
                          class="eval-secondary-button"
                          @click="openMatrixJudgment(attempt)"
                        >
                          查看判定
                        </button>
                        <button
                          type="button"
                          class="eval-secondary-button"
                          :disabled="!attempt.trace_id"
                          @click="openMatrixTrace(attempt)"
                        >
                          运行轨迹 · {{ statusText(attempt.trace_status) }}
                        </button>
                      </div>
                    </article>
                  </div>
                </details>
              </div>
            </section>
          </template>
        </section>

        <section
          v-show="
            evaluationTab === 'history' &&
            selectedRun &&
            historyDetailTab === 'baseline'
          "
          class="eval-panel eval-baseline-panel"
        >
          <div class="eval-panel-heading">
            <div>
              <span class="eval-kicker">基线与比较</span>
              <h3>确认稳定现场并比较版本变化</h3>
            </div>
            <span class="eval-count">
              {{ baselineLoading ? "读取中" : currentBaseline ? "已有基线" : "尚无基线" }}
            </span>
          </div>
          <p v-if="baselineError" class="eval-error">{{ baselineError }}</p>

          <section v-if="currentBaseline" class="eval-baseline-current">
            <header>
              <div>
                <span class="eval-kicker">当前基线</span>
                <h4>
                  {{ currentBaseline.suite_name }}
                  · {{ runTypeText(currentBaseline.run_type) }}
                </h4>
              </div>
              <span class="eval-baseline-current-badge">当前</span>
            </header>
            <dl>
              <div>
                <dt>Run</dt>
                <dd :title="currentBaseline.execution_run_id">
                  {{ currentBaseline.execution_run_id.slice(0, 22) }}…
                </dd>
              </div>
              <div>
                <dt>判定修订版</dt>
                <dd :title="currentBaseline.evaluation_revision_id">
                  {{ currentBaseline.evaluation_revision_id.slice(-12) }}
                </dd>
              </div>
              <div>
                <dt>Snapshot</dt>
                <dd>{{ currentBaseline.collection_snapshot_id }}</dd>
              </div>
              <div>
                <dt>设置人 / 时间</dt>
                <dd>
                  {{ currentBaseline.reviewer }} ·
                  {{ formatRunDate(currentBaseline.created_at) }}
                </dd>
              </div>
              <div class="eval-baseline-reason">
                <dt>原因</dt>
                <dd>{{ currentBaseline.reason }}</dd>
              </div>
            </dl>
            <div class="eval-baseline-metrics">
              <span>通过 {{ currentBaseline.totals.case_pass_count }}</span>
              <span>失败 {{ currentBaseline.totals.case_fail_count }}</span>
              <span>
                红线失败 {{ currentBaseline.totals.redline_violation_count }}
                （X {{ currentBaseline.totals.x_violation_count }} ·
                跨项 {{ currentBaseline.totals.check_failure_count }}）
              </span>
              <span>
                Token
                {{
                  formatTokens(
                    currentBaseline.totals.input_tokens +
                      currentBaseline.totals.output_tokens,
                  )
                }}
              </span>
              <span>
                模型 / 工具 {{ currentBaseline.totals.model_calls }} /
                {{ currentBaseline.totals.tool_calls }}
              </span>
            </div>
          </section>
          <p v-else-if="!baselineLoading" class="eval-empty">
            {{ selectedRun?.suite_name || selectedRun?.suite_id || "当前范围" }} 还没有当前基线。
          </p>

          <section class="eval-baseline-action">
            <template v-if="selectedRunIsCurrentBaseline">
              <strong>当前 run 就是这个范围的基线</strong>
              <small>基线结果已在设置时冻结，后续改判不会反写这份快照。</small>
            </template>
            <template v-else-if="isBaselineFormOpen">
              <label class="eval-field">
                <span>设置人</span>
                <input v-model="reviewerName" maxlength="128" />
              </label>
              <label class="eval-field">
                <span>设置原因</span>
                <textarea
                  v-model="baselineReason"
                  rows="3"
                  maxlength="4000"
                  placeholder="说明为什么选择这次 run 作为新基线"
                />
              </label>
              <div class="eval-baseline-form-actions">
                <button
                  type="button"
                  class="eval-primary"
                  :disabled="baselineBusy || !baselineReason.trim()"
                  @click="submitBaseline"
                >
                  {{ baselineBusy ? "设置中…" : "确认设为当前基线" }}
                </button>
                <button type="button" :disabled="baselineBusy" @click="closeBaselineForm">
                  取消
                </button>
              </div>
            </template>
            <template v-else>
              <button
                type="button"
                class="eval-primary"
                :disabled="!baselineEligibility.eligible"
                @click="openBaselineForm"
              >
                设为当前基线
              </button>
              <small :class="{ 'eval-error-text': !baselineEligibility.eligible }">
                {{
                  baselineEligibility.eligible
                    ? "设置后冻结当前结果，后续比较以这份快照为基准。"
                    : baselineEligibility.reason
                }}
              </small>
            </template>
          </section>

          <details v-if="baselineHistory.length" class="eval-baseline-history">
            <summary>基线历史 · {{ baselineHistory.length }}</summary>
            <article
              v-for="item in baselineHistory"
              :key="item.baseline_id"
              :class="{ current: item.is_current }"
            >
              <div>
                <strong>{{ formatRunDate(item.created_at) }}</strong>
                <span v-if="item.is_current">当前</span>
              </div>
              <small :title="item.execution_run_id">{{ item.execution_run_id }}</small>
              <p>{{ item.reviewer }} · {{ item.reason }}</p>
            </article>
          </details>

          <section v-if="baselineComparison" class="eval-comparison">
            <header>
              <div>
                <span class="eval-kicker">目标相对基线</span>
                <h4>
                  {{
                    baselineComparison.comparable
                      ? "同场比较：差异可用于版本判断"
                      : "环境已变化：仅供排查，不可直接归因"
                  }}
                </h4>
              </div>
              <span :class="baselineComparison.comparable ? 'result-pass' : 'result-pending'">
                {{ baselineComparison.comparable ? "可直接比较" : "不可直接归因" }}
              </span>
            </header>
            <ul
              v-if="baselineComparison.incompatibility_reasons.length"
              class="eval-comparison-warning"
            >
              <li
                v-for="reason in baselineComparison.incompatibility_reasons"
                :key="reason"
              >
                {{ baselineIncompatibilityText(reason) }}
              </li>
            </ul>
            <p v-if="!baselineComparison.comparable" class="eval-comparison-note">
              目标 run 与基线的 suite revision、case 集合、知识库快照、运行策略或聚合规则至少有一项不同。
              页面仍展示差异用于排查，但不能据此判断“是模型或 Agent 改动导致了变化”。
            </p>
            <div class="eval-comparison-summary">
              <article>
                <span>Case 通过</span>
                <strong>{{ signedNumber(baselineComparison.summary.case_pass_count_delta) }}</strong>
                <small>
                  {{ baselineComparison.summary.newly_passing_count }} 个转好 ·
                  {{ baselineComparison.summary.newly_failing_count }} 个转差
                </small>
              </article>
              <article>
                <span>Case 失败</span>
                <strong>{{ signedNumber(baselineComparison.summary.case_fail_count_delta) }}</strong>
                <small>正数表示失败数增加</small>
              </article>
              <article>
                <span>红线失败</span>
                <strong>
                  {{ signedNumber(baselineComparison.summary.redline_violation_count_delta) }}
                </strong>
                <small>
                  X {{ signedNumber(baselineComparison.summary.x_violation_count_delta) }} ·
                  跨项 {{ signedNumber(baselineComparison.summary.check_failure_count_delta) }}
                </small>
              </article>
              <article>
                <span>Token</span>
                <strong>
                  {{
                    signedNumber(
                      baselineComparison.summary.input_tokens_delta +
                        baselineComparison.summary.output_tokens_delta,
                    )
                  }}
                </strong>
                <small>目标减基线</small>
              </article>
              <article>
                <span>模型 / 工具</span>
                <strong>
                  {{ signedNumber(baselineComparison.summary.model_calls_delta) }} /
                  {{ signedNumber(baselineComparison.summary.tool_calls_delta) }}
                </strong>
                <small>目标减基线</small>
              </article>
              <article>
                <span>耗时</span>
                <strong>
                  {{ signedNumber(baselineComparison.summary.duration_ms_delta) }} ms
                </strong>
                <small>目标减基线</small>
              </article>
            </div>
            <div class="eval-comparison-cases">
              <p
                v-if="
                  !baselineComparison.cases.some((item) => item.change !== 'unchanged')
                "
                class="eval-empty"
              >
                所有 case 结果与基线一致。
              </p>
              <article
                v-for="item in baselineComparison.cases.filter(
                  (candidate) => candidate.change !== 'unchanged',
                )"
                :key="item.case_id"
                class="eval-comparison-case"
                :class="`comparison-${item.change}`"
              >
                <div>
                  <strong>{{ item.case_id }}</strong>
                  <small>{{ item.category }}</small>
                </div>
                <span>
                  {{ item.baseline_result ? statusText(item.baseline_result) : "不存在" }}
                </span>
                <span>→</span>
                <span>
                  {{ item.target_result ? statusText(item.target_result) : "不存在" }}
                </span>
                <strong>{{ baselineChangeText(item.change) }}</strong>
                <small>
                  pass {{ signedNumber(item.pass_count_delta) }} ·
                  fail {{ signedNumber(item.fail_count_delta) }} ·
                  X {{ signedNumber(item.x_violation_count_delta) }} ·
                  跨项 {{ signedNumber(item.check_failure_count_delta) }} ·
                  红线 {{ signedNumber(item.redline_violation_count_delta) }}
                </small>
                <small>
                  token
                  {{ signedNumber(item.input_tokens_delta + item.output_tokens_delta) }}
                  · 模型 {{ signedNumber(item.model_calls_delta) }}
                  · 工具 {{ signedNumber(item.tool_calls_delta) }}
                </small>
              </article>
            </div>
          </section>
          <p
            v-else-if="currentBaseline && !baselineLoading && !baselineEligibility.eligible"
            class="eval-empty"
          >
            当前目标尚不能比较：{{ baselineEligibility.reason }}
          </p>
          <p v-else-if="!currentBaseline && !baselineLoading" class="eval-empty">
            设置当前基线后，才能比较其他 run。
          </p>
        </section>

        <section
          v-show="
            evaluationTab === 'history' &&
            selectedRun &&
            historyDetailTab === 'judgment'
          "
          class="eval-panel eval-judgment-panel"
        >
          <div class="eval-panel-heading">
            <div>
              <span class="eval-kicker">判官</span>
              <h3>逐项判定</h3>
            </div>
            <span class="eval-count">
              {{
                evaluationRevision
                  ? statusText(evaluationRevision.status)
                  : "尚未判定"
              }}
            </span>
          </div>

          <section class="eval-prompt-review">
            <div class="eval-prompt-review-heading">
              <strong>判官提示词</strong>
              <span>启动判定前必须共同审阅</span>
            </div>
            <details v-if="judgePrompt" class="eval-judge-prompt">
              <summary>
                <span>
                  <strong>主判官 · {{ judgePrompt.version }}</strong>
                  <small>
                    只接收答案和当前 criterion 的 rule_type／rule_text，逐条判断
                    R／X 是否满足。
                  </small>
                </span>
                <code>{{ judgePrompt.content_hash.slice(0, 12) }}</code>
              </summary>
              <pre>{{ judgePrompt.content }}</pre>
            </details>
            <details v-if="citationPrompt" class="eval-judge-prompt">
              <summary>
                <span>
                  <strong>引用支持判官 · {{ citationPrompt.version }}</strong>
                  <small>
                    只在答案存在合法显式引用时触发，逐 block 检查证据是否支持对应主张。
                  </small>
                </span>
                <code>{{ citationPrompt.content_hash.slice(0, 12) }}</code>
              </summary>
              <pre>{{ citationPrompt.content }}</pre>
            </details>
          </section>

          <div v-if="evaluationRevisions.length" class="eval-revision-picker">
            <label class="eval-field">
              <span>判定修订版</span>
              <select v-model="selectedEvaluationRevisionId">
                <option
                  v-for="revision in evaluationRevisions"
                  :key="revision.revision_id"
                  :value="revision.revision_id"
                >
                  {{ formatRunDate(revision.created_at) }} ·
                  {{ statusText(revision.status) }} ·
                  通过 {{ revision.summary.pass ?? 0 }}/{{ revision.summary.total ?? 0 }} ·
                  {{ revision.revision_id.slice(-8) }}
                </option>
              </select>
            </label>
            <button
              type="button"
              class="eval-secondary"
              :disabled="
                judgingBusy ||
                !canStartJudgment ||
                evaluationRevision?.status === 'running'
              "
              @click="toggleJudgmentForm"
            >
              {{ isJudgmentFormOpen ? "收起重新判定" : "重新判定" }}
            </button>
          </div>

          <template v-if="!evaluationRevision || isJudgmentFormOpen">
            <label class="eval-confirm-row">
              <input v-model="judgeReviewConfirmed" type="checkbox" />
              <span>
                我已人工审阅主判官和引用支持判官提示词，并确认可以调用云端模型。
              </span>
            </label>
            <div class="eval-form-grid eval-judge-config-grid">
              <label class="eval-field">
                <span>判定并发</span>
                <input
                  v-model.number="judgeConcurrency"
                  type="number"
                  min="1"
                  max="8"
                />
                <small class="eval-field-help">
                  同时处理多少个“样本 × 判定项”任务。默认 2。
                </small>
              </label>
              <label class="eval-field">
                <span>Token 消耗上限</span>
                <input
                  v-model.number="judgeTokenBudget"
                  type="number"
                  min="1"
                />
                <small class="eval-field-help">
                  整次判定最多消耗多少 token，包含判官提示词、题目、答案、证据等输入和模型回复。
                </small>
              </label>
              <label class="eval-field">
                <span>模型调用次数上限</span>
                <input
                  v-model.number="judgeRequestBudget"
                  type="number"
                  min="1"
                />
                <small class="eval-field-help">
                  每个任务通常调用 1 次、最多重试 1 次；系统启动任务前按 2
                  次预留，因此设得过小会让任务保持待定。
                </small>
              </label>
              <label class="eval-field">
                <span>单次超时（秒）</span>
                <input
                  v-model.number="judgeTimeoutSeconds"
                  type="number"
                  min="10"
                  max="300"
                />
                <small class="eval-field-help">
                  任意一次模型请求超过该时长即中止，并将判定保持为待定。
                </small>
              </label>
            </div>
            <p v-if="judgingError" class="eval-error">{{ judgingError }}</p>
            <div class="eval-actions">
              <button
                type="button"
                class="eval-primary"
                :disabled="
                  judgingBusy ||
                  !judgeReviewConfirmed ||
                  !canStartJudgment
                "
                @click="startJudgment"
              >
                {{
                  judgingBusy
                    ? "正在启动…"
                    : evaluationRevision
                      ? "创建新判定"
                      : "开始判定"
                }}
              </button>
              <button
                v-if="isJudgmentFormOpen"
                type="button"
                :disabled="judgingBusy"
                @click="toggleJudgmentForm"
              >
                取消
              </button>
            </div>
          </template>

          <template v-if="evaluationRevision">
            <div class="eval-judgment-summary">
              <span>总任务 {{ evaluationRevision.summary.total ?? 0 }}</span>
              <span>通过 {{ evaluationRevision.summary.pass ?? 0 }}</span>
              <span>不通过 {{ evaluationRevision.summary.fail ?? 0 }}</span>
              <span>待定 {{ evaluationRevision.summary.pending ?? 0 }}</span>
              <span>{{ evaluationRevision.revision_id }}</span>
            </div>
            <p v-if="judgingError || evaluationRevision.error_message" class="eval-error">
              {{ judgingError || evaluationRevision.error_message }}
            </p>
            <section
              v-if="evaluationRevision.case_aggregates.length"
              class="eval-aggregate-board"
            >
              <header>
                <strong>生效绑定与 N／K 聚合</strong>
                <span>
                  {{ evaluationRevision.effective_bindings.length }} 条生效绑定 ·
                  {{ evaluationRevision.check_reviews.length }} 条人工检查复核
                </span>
              </header>
              <article
                v-for="caseAggregate in evaluationRevision.case_aggregates"
                :key="caseAggregate.case_revision_id"
                class="eval-case-aggregate"
              >
                <div class="eval-case-aggregate-head">
                  <div>
                    <strong>{{ caseAggregate.case_id }}</strong>
                    <small>
                      N={{ caseAggregate.n }} · K={{ caseAggregate.k }} ·
                      有效样本 {{ caseAggregate.valid_samples }}
                    </small>
                  </div>
                  <span :class="`result-${caseAggregate.result}`">
                    {{ statusText(caseAggregate.result) }}
                  </span>
                </div>
                <div class="eval-case-aggregate-counts">
                  <span>通过 {{ caseAggregate.pass_count }}</span>
                  <span>失败 {{ caseAggregate.fail_count }}</span>
                  <span>待定 {{ caseAggregate.pending_count }}</span>
                  <span>
                    X 违规 {{ caseAggregate.x_violation_count }} ·
                    跨项失败 {{ caseAggregate.check_failure_count }} ·
                    红线失败 {{ caseAggregate.redline_violation_count }}
                  </span>
                </div>
                <p v-for="reason in caseAggregate.reasons" :key="reason">
                  {{ reason }}
                </p>
                <details
                  v-if="attemptAggregatesForCase(caseAggregate.case_revision_id).length"
                >
                  <summary>查看 attempt 聚合</summary>
                  <div class="eval-attempt-aggregates">
                    <article
                      v-for="attemptAggregate in attemptAggregatesForCase(
                        caseAggregate.case_revision_id,
                      )"
                      :key="attemptAggregate.attempt_id"
                    >
                      <div>
                        <strong>
                          {{ attemptAggregate.case_id }} ·
                          {{ attemptSampleLabel(attemptAggregate.attempt_id) }}
                        </strong>
                        <span :class="`result-${attemptAggregate.result}`">
                          {{ statusText(attemptAggregate.result) }}
                        </span>
                      </div>
                      <small
                        :title="attemptAggregate.attempt_id"
                        class="eval-attempt-id"
                      >
                        {{ attemptShortId(attemptAggregate.attempt_id) }}
                      </small>
                      <small>
                        R {{ attemptAggregate.r_pass }} 通过 /
                        {{ attemptAggregate.r_fail }} 失败 /
                        {{ attemptAggregate.r_pending }} 待定 · X
                        {{ attemptAggregate.x_not_violated }} 合规 /
                        {{ attemptAggregate.x_violated }} 违规 /
                        {{ attemptAggregate.x_pending }} 待定
                      </small>
                      <p v-for="reason in attemptAggregate.reasons" :key="reason">
                        {{ reason }}
                      </p>
                      <button
                        type="button"
                        class="eval-secondary-button"
                        @click="focusAttemptJudgments(attemptAggregate.attempt_id)"
                      >
                        查看该样本判定
                      </button>
                    </article>
                  </div>
                </details>
              </article>
            </section>
            <div class="eval-check-guide">
              <article>
                <strong>引用完整性</strong>
                <p>
                  检查引用有没有指错地方。程序会逐个核对答案中的知识块编号（block_id），确认它们都来自本次检索工具实际返回的证据。
                </p>
              </article>
              <article>
                <strong>引用支持</strong>
                <p>
                  检查引用内容能不能证明答案。模型判官会逐个查看被引用的证据原文，判断它是否支持答案中的对应说法。只有引用完整且答案包含显式引用时才会执行。
                </p>
              </article>
            </div>
            <details class="eval-check-results">
              <summary>通用跨判定项检查 · {{ evaluationRevision.checks.length }}</summary>
              <details
                v-for="check in evaluationRevision.checks"
                :key="check.check_result_id"
                class="eval-check-result"
              >
                <summary>
                  <strong :title="check.checker_id">
                    {{ checkerText(check.checker_id) }}
                  </strong>
                  <span :class="`result-${effectiveCheckResult(check)}`">
                    {{ statusText(effectiveCheckResult(check)) }}
                  </span>
                  <small>
                    {{ caseIdForAttempt(check.attempt_id) }} ·
                    {{
                      check.error_code
                        ? diagnosticErrorText(check.error_code)
                        : "无错误"
                    }}
                    · {{ checkExecutorVersionText(check) }}
                    <template v-if="checkReviewFor(check)">
                      · 人工复核：{{ checkReviewReviewerText(check) }}
                    </template>
                  </small>
                </summary>
                <div class="eval-check-detail">
                  <p v-if="checkReviewFor(check)">
                    人工复核原因：{{ checkReviewReasonText(check) }}
                  </p>
                  <p
                    v-if="
                      typeof check.payload.reason === 'string' &&
                      check.payload.reason
                    "
                  >
                    {{ check.payload.reason }}
                  </p>
                  <p
                    v-if="
                      typeof check.payload.model_id === 'string' &&
                      check.payload.model_id
                    "
                  >
                    模型：{{ check.payload.model_id }}
                  </p>
                  <template v-if="check.checker_id === 'citation_integrity'">
                    <p>
                      已引用：
                      {{ checkStringList(check, "cited_block_ids").join("、") || "无" }}
                    </p>
                    <p>
                      缺失 block：
                      {{ checkStringList(check, "missing_block_ids").join("、") || "无" }}
                    </p>
                    <p>
                      本次工具证据块：
                      {{ check.payload.evidence_block_count ?? 0 }}
                    </p>
                  </template>
                  <template v-if="check.checker_id === 'citation_support'">
                    <article
                      v-for="citation in effectiveCitationResults(check)"
                      :key="citation.blockId"
                      class="eval-citation-result"
                    >
                      <header>
                        <code>{{ citation.blockId }}</code>
                        <span>{{ statusText(citation.verdict) }}</span>
                      </header>
                      <p>{{ citation.reason }}</p>
                      <blockquote
                        v-for="(quote, index) in citation.evidenceQuotes"
                        :key="index"
                      >
                        {{ quote }}
                      </blockquote>
                    </article>
                    <details
                      v-for="invocation in checkInvocations(check)"
                      :key="invocation.attemptNumber"
                      class="eval-check-invocation"
                    >
                      <summary>
                        第 {{ invocation.attemptNumber }} 次 ·
                        {{ statusText(invocation.status) }} ·
                        输入 {{ invocation.inputTokens }} / 输出
                        {{ invocation.outputTokens }} token
                      </summary>
                      <p v-if="invocation.errorCode">
                        {{ diagnosticErrorText(invocation.errorCode) }}
                      </p>
                      <pre v-if="invocation.rawOutput">{{ invocation.rawOutput }}</pre>
                    </details>
                    <button
                      v-if="canReviewLlmCheck(check)"
                      type="button"
                      class="eval-secondary-button"
                      @click="openCheckReview(check)"
                    >
                      人工复核逐 block 结论
                    </button>
                    <form
                      v-if="
                        checkReviewTarget?.check.check_result_id ===
                        check.check_result_id
                      "
                      class="eval-review-form"
                      @submit.prevent="submitCheckReview"
                    >
                      <label class="eval-field">
                        <span>复核说明</span>
                        <textarea
                          v-model="checkReviewReason"
                          rows="2"
                          placeholder="说明本次人工复核的依据"
                          required
                        />
                      </label>
                      <label class="eval-field">
                        <span>复核人</span>
                        <input v-model="checkReviewReviewer" type="text" required />
                      </label>
                      <article
                        v-for="item in checkReviewTarget.items"
                        :key="item.blockId"
                        class="eval-review-item"
                      >
                        <code>{{ item.blockId }}</code>
                        <select v-model="item.verdict">
                          <option value="supported">有证据支持</option>
                          <option value="unsupported">无证据支持</option>
                          <option value="unknown">不确定</option>
                        </select>
                        <input
                          v-model="item.reason"
                          type="text"
                          placeholder="该 block 的复核原因"
                          required
                        />
                      </article>
                      <p v-if="checkReviewError" class="eval-error">
                        {{ checkReviewError }}
                      </p>
                      <div class="eval-actions">
                        <button type="submit" :disabled="checkReviewBusy">
                          {{ checkReviewBusy ? "提交中…" : "提交人工复核" }}
                        </button>
                        <button
                          type="button"
                          :disabled="checkReviewBusy"
                          @click="closeCheckReview"
                        >
                          取消
                        </button>
                      </div>
                    </form>
                  </template>
                </div>
              </details>
              <p v-if="citationJudgeNotice" class="eval-check-notice">
                {{ citationJudgeNotice }}
              </p>
            </details>
            <div class="eval-judgment-viewbar">
              <div role="tablist" aria-label="判定结果分组方式">
                <button
                  type="button"
                  :class="{ active: judgmentViewMode === 'attempt' }"
                  @click="judgmentViewMode = 'attempt'"
                >
                  按样本
                </button>
                <button
                  type="button"
                  :class="{ active: judgmentViewMode === 'criterion' }"
                  @click="judgmentViewMode = 'criterion'"
                >
                  按判定项
                </button>
              </div>
              <div v-if="judgmentViewMode === 'attempt'" class="eval-attempt-filter">
                <button
                  type="button"
                  :class="{ active: judgmentAttemptFilter === 'all' }"
                  @click="judgmentAttemptFilter = 'all'"
                >
                  全部 {{ attemptJudgmentGroups.length }}
                </button>
                <button
                  type="button"
                  :class="{ active: judgmentAttemptFilter === 'failed' }"
                  @click="judgmentAttemptFilter = 'failed'"
                >
                  失败 {{ failedAttemptCount }}
                </button>
              </div>
            </div>
            <div
              v-if="judgmentViewMode === 'attempt'"
              class="eval-attempt-judgment-groups"
            >
              <p v-if="!filteredAttemptJudgmentGroups.length" class="eval-empty">
                当前筛选下没有样本。
              </p>
              <details
                v-for="group in filteredAttemptJudgmentGroups"
                :id="`attempt-judgment-${group.attemptId}`"
                :key="group.attemptId"
                class="eval-attempt-judgment-group"
                :class="{
                  focused: focusedAttemptId === group.attemptId,
                  failed: group.result === 'fail',
                  pending: group.result === 'pending',
                }"
                :open="
                  focusedAttemptId === group.attemptId || group.result !== 'pass'
                "
              >
                <summary>
                  <div>
                    <strong>{{ group.caseId }} · {{ group.sampleLabel }}</strong>
                    <small :title="group.attemptId">
                      {{ attemptShortId(group.attemptId) }}
                    </small>
                  </div>
                  <span :class="`result-${group.result}`">
                    {{ statusText(group.result) }}
                  </span>
                </summary>
                <div class="eval-attempt-judgment-summary">
                  <span>
                    R {{ group.aggregate?.r_pass ?? 0 }} 通过 /
                    {{ group.aggregate?.r_fail ?? 0 }} 失败 /
                    {{ group.aggregate?.r_pending ?? 0 }} 待定
                  </span>
                  <span>
                    X {{ group.aggregate?.x_not_violated ?? 0 }} 合规 /
                    {{ group.aggregate?.x_violated ?? 0 }} 违规 /
                    {{ group.aggregate?.x_pending ?? 0 }} 待定
                  </span>
                </div>
                <p
                  v-for="reason in group.aggregate?.reasons ?? []"
                  :key="reason"
                  class="eval-attempt-reason"
                >
                  {{ reason }}
                </p>
                <div class="eval-attempt-detail-actions">
                  <details class="eval-answer-view">
                    <summary>查看答案</summary>
                    <pre>{{ group.answer || "无答案" }}</pre>
                  </details>
                  <button
                    v-if="group.traceId"
                    type="button"
                    class="eval-attempt-trace-action"
                    :title="group.traceId"
                    @click="inspectTrace(group.traceId)"
                  >
                    查看本样本运行轨迹 · {{ statusText(group.traceStatus) }}
                  </button>
                </div>
                <div class="eval-attempt-criterion-list">
                  <article
                    v-for="judgment in group.judgments"
                    :key="judgment.judgment_id"
                  >
                    <header>
                      <span class="eval-rule-badge">
                        {{
                          criterionById.get(judgment.criterion_revision_id)?.rule_type ??
                          "?"
                        }}
                      </span>
                      <div>
                        <strong>
                          {{
                            criterionById.get(judgment.criterion_revision_id)?.title ??
                            judgment.criterion_revision_id
                          }}
                        </strong>
                        <small>
                          {{ executorText(judgment.executor) }} · {{ group.sampleLabel }}
                        </small>
                      </div>
                      <span :class="`result-${judgment.normalized_result}`">
                        {{ statusText(judgment.normalized_result) }}
                      </span>
                    </header>
                    <p>
                      {{
                        judgment.error_code
                          ? diagnosticErrorText(judgment.error_code)
                          : judgment.reason ?? "无判定说明"
                      }}
                    </p>
                    <p
                      v-if="judgment.evidence.quotes?.length"
                      class="eval-judge-quote"
                    >
                      “{{ judgment.evidence.quotes[0] }}”
                    </p>
                    <details v-if="judgment.invocations.length">
                      <summary>
                        判官调用 · {{ judgment.invocations.length }} 次
                      </summary>
                      <article
                        v-for="invocation in judgment.invocations"
                        :key="invocation.invocation_id"
                        class="eval-invocation"
                      >
                        <span>
                          第 {{ invocation.attempt_number }} 次 ·
                          {{ statusText(invocation.status) }} ·
                          {{ invocation.model_id }}
                        </span>
                        <small>
                          输入 {{ invocation.input_tokens }} / 输出
                          {{ invocation.output_tokens }} token
                        </small>
                        <details
                          v-if="invocation.input_json"
                          class="eval-invocation-payload"
                        >
                          <summary>查看判官输入</summary>
                          <TraceArtifactContentViewer
                            artifact-kind="judge.input"
                            :content="invocation.input_json"
                            error=""
                            :is-loading="false"
                          />
                        </details>
                        <details
                          v-if="invocation.raw_output"
                          class="eval-invocation-payload"
                        >
                          <summary>查看判官输出</summary>
                          <TraceArtifactContentViewer
                            artifact-kind="judge.output"
                            :content="invocation.raw_output"
                            error=""
                            :is-loading="false"
                          />
                        </details>
                      </article>
                    </details>
                  </article>
                </div>
              </details>
            </div>
            <div v-else class="eval-judgment-groups">
              <section
                v-for="group in judgmentGroups"
                v-show="group.items.length"
                :key="group.ruleType"
                class="eval-judgment-group"
                :class="`eval-judgment-group-${group.ruleType.toLowerCase()}`"
              >
                <header class="eval-judgment-group-guide">
                  <span class="eval-rule-badge">{{ group.ruleType }}</span>
                  <div>
                    <h4>{{ group.title }}</h4>
                    <p>{{ group.description }}</p>
                  </div>
                </header>
                <div class="eval-judgment-list">
                  <article
                    v-for="judgment in group.items"
                    :key="judgment.judgment_id"
                  >
                    <div class="eval-judgment-head">
                      <div>
                        <strong>{{ caseIdForAttempt(judgment.attempt_id) }}</strong>
                        <span>
                          {{
                            criterionById.get(judgment.criterion_revision_id)?.title ??
                            judgment.criterion_revision_id
                          }}
                        </span>
                      </div>
                      <span :class="`result-${judgment.normalized_result}`">
                        {{ statusText(judgment.normalized_result) }}
                      </span>
                    </div>
                    <p>
                      {{ executorText(judgment.executor) }}：
                      {{ statusText(judgment.verdict) }} ·
                      {{
                        judgment.error_code
                          ? diagnosticErrorText(judgment.error_code)
                          : judgment.reason ?? "无判定说明"
                      }}
                    </p>
                    <p v-if="judgment.evidence.quotes?.length" class="eval-judge-quote">
                      “{{ judgment.evidence.quotes[0] }}”
                    </p>
                    <p v-if="effectiveBindingFor(judgment)" class="eval-binding-note">
                      当前生效：
                      {{ executorText(effectiveBindingFor(judgment)?.executor ?? "") }} ·
                      {{ formatRunDate(effectiveBindingFor(judgment)?.bound_at ?? "") }}
                    </p>
                    <button
                      v-if="canOverrideJudgment(judgment)"
                      type="button"
                      class="eval-secondary-button"
                      @click="openJudgeOverride(judgment)"
                    >
                      人工改判
                    </button>
                    <form
                      v-if="
                        judgeOverrideTarget?.attemptId === judgment.attempt_id &&
                        judgeOverrideTarget.criterionRevisionId ===
                          judgment.criterion_revision_id
                      "
                      class="eval-review-form"
                      @submit.prevent="submitJudgeOverride"
                    >
                      <label class="eval-field">
                        <span>人工结论</span>
                        <select v-model="judgeOverrideVerdict" required>
                          <option value="" disabled>请选择</option>
                          <option
                            v-for="option in judgeOverrideVerdicts"
                            :key="option.value"
                            :value="option.value"
                          >
                            {{ option.label }}
                          </option>
                        </select>
                      </label>
                      <label class="eval-field">
                        <span>判定原因</span>
                        <textarea
                          v-model="judgeOverrideReason"
                          rows="3"
                          required
                          placeholder="说明为什么不采用当前 LLM Judge 结论"
                        />
                      </label>
                      <label class="eval-field">
                        <span>复核人</span>
                        <input v-model="judgeOverrideReviewer" type="text" required />
                      </label>
                      <p v-if="judgeOverrideError" class="eval-error">
                        {{ judgeOverrideError }}
                      </p>
                      <div class="eval-actions">
                        <button type="submit" :disabled="judgeOverrideBusy">
                          {{ judgeOverrideBusy ? "提交中…" : "提交人工判定" }}
                        </button>
                        <button
                          type="button"
                          :disabled="judgeOverrideBusy"
                          @click="closeJudgeOverride"
                        >
                          取消
                        </button>
                      </div>
                    </form>
                    <details
                      v-if="judgmentHistory(judgment).length > 1"
                      class="eval-binding-history"
                    >
                      <summary>
                        判定历史 · {{ judgmentHistory(judgment).length }} 条
                      </summary>
                      <article
                        v-for="historyItem in judgmentHistory(judgment)"
                        :key="historyItem.judgment_id"
                      >
                        <div>
                          <strong>{{ executorText(historyItem.executor) }}</strong>
                          <span :class="`result-${historyItem.normalized_result}`">
                            {{ statusText(historyItem.normalized_result) }}
                          </span>
                        </div>
                        <p>{{ historyItem.reason ?? "无判定说明" }}</p>
                        <small>
                          {{ formatRunDate(historyItem.created_at) }} ·
                          {{ isJudgmentEffective(historyItem) ? "当前生效" : "历史" }}
                        </small>
                      </article>
                    </details>
                    <details v-if="judgment.invocations.length">
                      <summary>
                        判官调用 · {{ judgment.invocations.length }} 次
                      </summary>
                      <article
                        v-for="invocation in judgment.invocations"
                        :key="invocation.invocation_id"
                        class="eval-invocation"
                      >
                        <span>
                          第 {{ invocation.attempt_number }} 次 ·
                          {{ statusText(invocation.status) }} · {{ invocation.model_id }}
                        </span>
                        <small>
                          输入 {{ invocation.input_tokens }} / 输出
                          {{ invocation.output_tokens }} token
                        </small>
                        <details
                          v-if="invocation.input_json"
                          class="eval-invocation-payload"
                        >
                          <summary>查看判官输入</summary>
                          <TraceArtifactContentViewer
                            artifact-kind="judge.input"
                            :content="invocation.input_json"
                            error=""
                            :is-loading="false"
                          />
                        </details>
                        <details
                          v-if="invocation.raw_output"
                          class="eval-invocation-payload"
                        >
                          <summary>查看判官输出</summary>
                          <TraceArtifactContentViewer
                            artifact-kind="judge.output"
                            :content="invocation.raw_output"
                            error=""
                            :is-loading="false"
                          />
                        </details>
                      </article>
                    </details>
                  </article>
                </div>
              </section>
            </div>
          </template>
        </section>

        <section
          v-if="
            (evaluationTab === 'history' &&
              selectedRun &&
              historyDetailTab === 'trace') ||
            (evaluationTab === 'execute' && traceDetail)
          "
          class="eval-trace-shell"
        >
          <TraceDetailViewer
            v-if="traceDetail"
            :trace="traceDetail"
            :load-artifact="loadTraceArtifactContent"
          />
          <div v-else class="eval-panel eval-placeholder">
            <span class="eval-kicker">运行轨迹</span>
            <h3>选择一个执行样本的运行轨迹</h3>
            <p>在上方样本列表点击“运行轨迹”后，这里会展示调用树和运行内容。</p>
          </div>
        </section>

        <section
          v-if="evaluationTab === 'history' && !selectedRun"
          class="eval-panel eval-placeholder"
        >
          <span class="eval-kicker">历史结果</span>
          <h3>从左侧选择一个历史执行批次</h3>
          <p>历史页展示已完成、已取消或已封存批次及其运行轨迹。</p>
        </section>
      </div>

      <div
        v-if="matrixJudgmentGroup"
        class="eval-modal-backdrop"
        @click.self="closeMatrixJudgment"
      >
        <section
          role="dialog"
          aria-modal="true"
          aria-labelledby="matrix-judgment-modal-title"
          class="eval-modal eval-modal-wide"
        >
          <header class="eval-modal-header">
            <div>
              <span class="eval-kicker">样本判定</span>
              <h3 id="matrix-judgment-modal-title">
                {{ matrixJudgmentGroup.caseId }} ·
                {{ matrixJudgmentGroup.sampleLabel }}
                <span :class="`result-${matrixJudgmentGroup.result}`">
                  {{ statusText(matrixJudgmentGroup.result) }}
                </span>
              </h3>
              <small :title="matrixJudgmentGroup.attemptId">
                {{ matrixJudgmentGroup.attemptId }}
              </small>
            </div>
            <button
              type="button"
              class="eval-modal-close"
              aria-label="关闭样本判定"
              @click="closeMatrixJudgment"
            >
              关闭
            </button>
          </header>
          <div
            class="eval-modal-body"
            @wheel.stop
            @touchmove.stop
          >
            <section class="eval-modal-question">
              <span>本次问题</span>
              <p>{{ matrixJudgmentQuestion }}</p>
            </section>
            <div class="eval-attempt-judgment-summary">
              <span>
                R {{ matrixJudgmentGroup.aggregate?.r_pass ?? 0 }} 通过 /
                {{ matrixJudgmentGroup.aggregate?.r_fail ?? 0 }} 失败 /
                {{ matrixJudgmentGroup.aggregate?.r_pending ?? 0 }} 待定
              </span>
              <span>
                X {{ matrixJudgmentGroup.aggregate?.x_not_violated ?? 0 }} 合规 /
                {{ matrixJudgmentGroup.aggregate?.x_violated ?? 0 }} 违规 /
                {{ matrixJudgmentGroup.aggregate?.x_pending ?? 0 }} 待定
              </span>
            </div>
            <p
              v-for="reason in matrixJudgmentGroup.aggregate?.reasons ?? []"
              :key="reason"
              class="eval-attempt-reason"
            >
              {{ reason }}
            </p>
            <details class="eval-answer-view">
              <summary>查看答案</summary>
              <pre>{{ matrixJudgmentGroup.answer || "无答案" }}</pre>
            </details>
            <section class="eval-modal-section">
              <h4>R／X 判定</h4>
              <p v-if="!matrixJudgmentGroup.judgments.length" class="eval-empty">
                当前样本尚无 criterion 判定。
              </p>
              <article
                v-for="judgment in matrixJudgmentGroup.judgments"
                :key="judgment.judgment_id"
                class="eval-modal-judgment"
              >
                <header>
                  <span class="eval-rule-badge">
                    {{
                      criterionById.get(judgment.criterion_revision_id)?.rule_type ??
                      "?"
                    }}
                  </span>
                  <div>
                    <strong>
                      {{
                        criterionById.get(judgment.criterion_revision_id)?.title ??
                        judgment.criterion_revision_id
                      }}
                    </strong>
                    <small>
                      {{ executorText(judgment.executor) }} ·
                      {{ statusText(judgment.verdict) }}
                    </small>
                  </div>
                  <span :class="`result-${judgment.normalized_result}`">
                    {{ statusText(judgment.normalized_result) }}
                  </span>
                </header>
                <section class="eval-modal-criterion-rule">
                  <span>金标准</span>
                  <p>
                    {{
                      criterionById.get(judgment.criterion_revision_id)?.rule_text ??
                      "当前 revision 的 criterion 原文不可用"
                    }}
                  </p>
                </section>
                <p>
                  {{
                    judgment.error_code
                      ? diagnosticErrorText(judgment.error_code)
                      : judgment.reason ?? "无判定说明"
                  }}
                </p>
                <p v-if="judgment.evidence.quotes?.length" class="eval-judge-quote">
                  “{{ judgment.evidence.quotes[0] }}”
                </p>
                <details v-if="judgment.invocations.length">
                  <summary>判官调用 · {{ judgment.invocations.length }} 次</summary>
                  <article
                    v-for="invocation in judgment.invocations"
                    :key="invocation.invocation_id"
                    class="eval-invocation"
                  >
                    <span>
                      第 {{ invocation.attempt_number }} 次 ·
                      {{ statusText(invocation.status) }} ·
                      {{ invocation.model_id }}
                    </span>
                    <small>
                      输入 {{ invocation.input_tokens }} / 输出
                      {{ invocation.output_tokens }} token
                    </small>
                    <details
                      v-if="invocation.input_json"
                      class="eval-invocation-payload"
                    >
                      <summary>查看判官输入</summary>
                      <TraceArtifactContentViewer
                        artifact-kind="judge.input"
                        :content="invocation.input_json"
                        error=""
                        :is-loading="false"
                      />
                    </details>
                    <details
                      v-if="invocation.raw_output"
                      class="eval-invocation-payload"
                    >
                      <summary>查看判官输出</summary>
                      <TraceArtifactContentViewer
                        artifact-kind="judge.output"
                        :content="invocation.raw_output"
                        error=""
                        :is-loading="false"
                      />
                    </details>
                  </article>
                </details>
              </article>
            </section>
            <section class="eval-modal-section">
              <h4>通用跨判定项检查</h4>
              <p v-if="!checksForAttempt(matrixJudgmentGroup.attemptId).length" class="eval-empty">
                当前样本没有跨判定项检查。
              </p>
              <article
                v-for="check in checksForAttempt(matrixJudgmentGroup.attemptId)"
                :key="check.check_result_id"
                class="eval-modal-check"
              >
                <div>
                  <strong>{{ checkerText(check.checker_id) }}</strong>
                  <span :class="`result-${effectiveCheckResult(check)}`">
                    {{ statusText(effectiveCheckResult(check)) }}
                  </span>
                </div>
                <small>
                  {{ check.error_code ? diagnosticErrorText(check.error_code) : "无错误" }}
                  · {{ checkExecutorVersionText(check) }}
                </small>
                <small v-if="citationBatchSummary(check)">
                  {{ citationBatchSummary(check) }}
                </small>
                <p v-if="checkReviewFor(check)">
                  人工复核：{{ checkReviewReviewerText(check) }} ·
                  {{ checkReviewReasonText(check) }}
                </p>
                <p
                  v-else-if="typeof check.payload.reason === 'string' && check.payload.reason"
                >
                  {{ check.payload.reason }}
                </p>
              </article>
            </section>
          </div>
        </section>
      </div>

      <div
        v-if="matrixTraceAttempt"
        class="eval-modal-backdrop"
        @click.self="closeMatrixTrace"
      >
        <section
          role="dialog"
          aria-modal="true"
          aria-labelledby="matrix-trace-modal-title"
          class="eval-modal eval-modal-trace"
        >
          <header class="eval-modal-header">
            <div>
              <span class="eval-kicker">样本运行轨迹</span>
              <h3 id="matrix-trace-modal-title">
                样本 #{{ String(matrixTraceAttempt.sample_ordinal).padStart(2, "0") }}
                · {{ matrixTraceAttempt.attempt_id.slice(0, 18) }}…
              </h3>
              <small>
                {{ statusText(matrixTraceAttempt.trace_status) }}
                <template v-if="matrixTraceAttempt.trace_id">
                  · {{ matrixTraceAttempt.trace_id }}
                </template>
              </small>
            </div>
            <button
              type="button"
              class="eval-modal-close"
              aria-label="关闭运行轨迹"
              @click="closeMatrixTrace"
            >
              关闭
            </button>
          </header>
          <div
            class="eval-modal-body"
            @wheel.stop
            @touchmove.stop
          >
            <p v-if="matrixTraceLoading" class="eval-empty">正在读取运行轨迹…</p>
            <p v-else-if="matrixTraceError" class="eval-error">
              {{ matrixTraceError }}
            </p>
            <TraceDetailViewer
              v-else-if="matrixTraceDetail"
              :trace="matrixTraceDetail"
              :load-artifact="loadTraceArtifactContent"
            />
          </div>
        </section>
      </div>
    </div>
  </section>
</template>

<style scoped>
.eval-page {
  display: grid;
  gap: 18px;
  padding-top: 28px;
}

.eval-heading,
.eval-live-head,
.eval-panel-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}

.eval-heading h2,
.eval-panel h3 {
  margin: 4px 0 0;
  color: #173723;
}

.eval-heading h2 {
  font-size: clamp(1.7rem, 4vw, 2.5rem);
}

.eval-eyebrow,
.eval-kicker {
  margin: 0;
  color: #55705f;
  font-size: 0.7rem;
  font-weight: 800;
  letter-spacing: 0.08em;
}

.eval-lede {
  max-width: 760px;
  margin: 8px 0 0;
  color: #5a6d60;
  line-height: 1.65;
}

.eval-build-badge,
.eval-count,
.eval-run-state {
  border-radius: 999px;
  padding: 6px 11px;
  color: #345441;
  background: #e0ebe1;
  font-size: 0.74rem;
  font-weight: 700;
}

.eval-workspace-tabs {
  position: sticky;
  z-index: 8;
  top: 10px;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 6px;
  border: 1px solid rgba(37, 71, 50, 0.14);
  border-radius: 15px;
  padding: 6px;
  background: rgba(245, 249, 244, 0.94);
  box-shadow: 0 10px 28px rgba(42, 66, 50, 0.08);
  backdrop-filter: blur(14px);
}

.eval-workspace-tabs button {
  display: grid;
  gap: 2px;
  border: 0;
  border-radius: 10px;
  padding: 10px 12px;
  color: #52675a;
  background: transparent;
  cursor: pointer;
  text-align: left;
}

.eval-workspace-tabs button.active {
  color: #fff;
  background: #245b38;
}

.eval-workspace-tabs small {
  overflow: hidden;
  color: #7a887e;
  font-size: 0.68rem;
  font-weight: 400;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.eval-workspace-tabs button.active small {
  color: rgba(255, 255, 255, 0.72);
}

.eval-layout {
  display: grid;
  grid-template-columns: minmax(280px, 0.82fr) minmax(0, 1.6fr);
  align-items: start;
  gap: 18px;
}

.eval-layout-wide {
  grid-template-columns: minmax(0, 1fr);
}

.eval-layout-wide .eval-main {
  width: 100%;
}

.eval-placeholder {
  display: grid;
  align-content: start;
  min-height: 250px;
}

.eval-placeholder h3 {
  margin-top: 12px;
}

.eval-placeholder p {
  max-width: 620px;
  color: #65756a;
  line-height: 1.7;
}

.eval-sidebar,
.eval-main {
  display: grid;
  gap: 18px;
}

.eval-panel {
  border: 1px solid rgba(37, 71, 50, 0.14);
  border-radius: 18px;
  padding: 20px;
  background: rgba(255, 255, 255, 0.8);
  box-shadow: 0 14px 38px rgba(42, 66, 50, 0.07);
}

.eval-panel-heading h3,
.eval-live-head h3 {
  font-size: 1.12rem;
}

.eval-heading-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}

.eval-save-state {
  display: block;
  margin-top: 5px;
  color: #5e7565;
  font-size: 0.7rem;
  font-weight: 500;
}

.eval-save-state.dirty {
  color: #8a5c20;
}

.eval-save-state.error {
  color: #9b3737;
}

.eval-inline-error {
  margin: 12px 0 0;
  border-radius: 9px;
  padding: 9px 11px;
  color: #8b3434;
  background: #fff0f0;
  font-size: 0.78rem;
}

.eval-diff {
  margin-top: 12px;
  border: 1px solid #d8e3da;
  border-radius: 11px;
  padding: 10px 12px;
  background: #f9fbf9;
}

.eval-diff summary {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  color: #435b49;
  cursor: pointer;
  font-size: 0.78rem;
}

.eval-diff summary span {
  color: #748278;
}

.eval-diff-item {
  display: grid;
  gap: 7px;
  margin-top: 10px;
  border-top: 1px solid #e2e9e3;
  padding-top: 9px;
}

.eval-diff-item > div:first-child {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  color: #3e5647;
  font-size: 0.76rem;
}

.eval-diff-item > div:first-child span {
  color: #886126;
}

.eval-diff-before,
.eval-diff-after {
  border-left: 3px solid #c68b6b;
  padding-left: 8px;
}

.eval-diff-after {
  border-left-color: #5f986e;
}

.eval-diff-before small,
.eval-diff-after small {
  color: #758479;
  font-size: 0.68rem;
}

.eval-diff pre {
  max-height: 180px;
  overflow: auto;
  margin: 3px 0 0;
  color: #44554a;
  font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
  font-size: 0.74rem;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
}

.eval-field {
  display: grid;
  gap: 6px;
  min-width: 0;
  margin-top: 14px;
  color: #4f6657;
  font-size: 0.78rem;
  font-weight: 700;
}

.eval-field input,
.eval-field select,
.eval-field textarea {
  width: 100%;
  border: 1px solid #cbd8cd;
  border-radius: 10px;
  padding: 9px 10px;
  color: #213b2a;
  background: #fbfdfb;
}

.eval-field textarea,
.eval-criterion-editor-row textarea {
  resize: vertical;
  font: inherit;
  line-height: 1.55;
}

.eval-field small,
.eval-suite-meta {
  color: #75847a;
  font-size: 0.7rem;
  font-weight: 400;
}

.eval-field-help {
  line-height: 1.5;
}

.eval-stale-notice {
  margin: 12px 0 0;
  border-radius: 10px;
  padding: 9px 11px;
  color: #8b4e1d;
  background: #fff4df;
  font-size: 0.76rem;
  line-height: 1.55;
}

.eval-full-button {
  width: 100%;
  margin-top: 12px;
}

.eval-case-list-row {
  display: grid;
  width: 100%;
  grid-template-columns: 72px minmax(0, 1fr) auto;
  gap: 8px;
  margin-top: 7px;
  border: 1px solid #e0e8e1;
  border-radius: 10px;
  padding: 9px 10px;
  color: #405747;
  background: #fbfdfb;
  cursor: pointer;
  font-size: 0.76rem;
  line-height: 1.45;
  text-align: left;
}

.eval-case-list-row:hover {
  border-color: #9eb9a5;
  background: #f1f7f2;
}

.eval-case-list-row.active {
  border-color: #3f7952;
  background: #e8f3ea;
  box-shadow: inset 3px 0 0 #2c6840;
}

.eval-case-list-question {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.eval-case-list-status {
  color: #748278;
  white-space: nowrap;
}

.eval-detail-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 7px 14px;
  margin-top: 14px;
  color: #65756a;
  font-size: 0.74rem;
}

.eval-readonly-section {
  margin-top: 16px;
  border-top: 1px solid #e4ebe5;
  padding-top: 12px;
}

.eval-readonly-section > span {
  color: #52675a;
  font-size: 0.72rem;
  font-weight: 800;
  letter-spacing: 0.06em;
}

.eval-readonly-section > p {
  margin: 8px 0 0;
  color: #2f4235;
  line-height: 1.7;
  white-space: pre-wrap;
}

.eval-readonly-section > ul {
  display: grid;
  gap: 6px;
  margin: 8px 0 0;
  padding-left: 20px;
  color: #55685b;
  font-size: 0.78rem;
}

.eval-readonly-criteria {
  padding-left: 0 !important;
  list-style: none;
}

.eval-readonly-criteria li {
  display: grid;
  gap: 4px;
  border-left: 3px solid #7ca989;
  padding-left: 9px;
}

.eval-readonly-criteria p {
  margin: 0;
  line-height: 1.6;
}

.eval-readonly-criteria small {
  color: #819087;
  font-size: 0.68rem;
}

.eval-change-note {
  margin-top: 14px;
  border-radius: 9px;
  padding: 9px 11px;
  color: #5b695f;
  background: #f0f5f0;
  font-size: 0.76rem;
}

.eval-placeholder-copy {
  max-width: 620px;
  color: #65756a;
  line-height: 1.7;
}

.eval-editor-panel {
  border-color: rgba(59, 110, 76, 0.32);
}

.eval-close-button {
  border: 0;
  color: #637269;
  background: transparent;
  cursor: pointer;
  font-size: 0.76rem;
  text-decoration: underline;
}

.eval-heading-actions .eval-close-button {
  border: 0;
  padding: 4px;
  background: transparent;
}

.eval-criterion-editor {
  margin-top: 18px;
  border-top: 1px solid #e0e8e1;
  padding-top: 14px;
}

.eval-criterion-editor-row {
  display: grid;
  gap: 8px;
  margin-top: 9px;
  border: 1px solid #dfe8e0;
  border-radius: 11px;
  padding: 10px;
  background: #fbfdfb;
}

.eval-criterion-row-head {
  display: grid;
  grid-template-columns: 110px minmax(0, 1fr) auto;
  gap: 8px;
}

.eval-criterion-row-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.eval-criterion-row-head input,
.eval-criterion-row-head select,
.eval-criterion-row-foot select,
.eval-criterion-editor-row textarea {
  border: 1px solid #cad7cc;
  border-radius: 8px;
  padding: 7px 8px;
  color: #2e4737;
  background: #fff;
}

.eval-criterion-row-head button {
  border: 1px solid #d9aaaa;
  border-radius: 8px;
  padding: 7px 9px;
  color: #8b3434;
  background: #fff7f7;
  cursor: pointer;
}

.eval-criterion-row-foot code {
  overflow: hidden;
  color: #738177;
  font-size: 0.68rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.eval-suite-case-picker,
.eval-quick-picker {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
  max-height: 330px;
  overflow: auto;
  margin-top: 15px;
  border: 1px solid #e0e8e1;
  border-radius: 11px;
  padding: 10px;
}

.eval-suite-case-picker > p,
.eval-quick-picker > p {
  grid-column: 1 / -1;
  margin: 0 0 4px;
  color: #627168;
  font-size: 0.74rem;
}

.eval-quick-case-group {
  border-top: 1px solid #e4ebe5;
  padding: 7px 0;
}

.eval-quick-picker .eval-quick-case-group {
  align-self: start;
  border: 1px solid #e4ebe5;
  border-radius: 9px;
  padding: 8px 9px;
}

.eval-quick-case-group summary {
  display: grid;
  grid-template-columns: 74px minmax(0, 1fr) auto;
  gap: 8px;
  color: #405747;
  cursor: pointer;
  font-size: 0.75rem;
  line-height: 1.45;
}

.eval-quick-case-group summary small {
  color: #77867b;
  white-space: nowrap;
}

.eval-quick-case-group .eval-select-row {
  margin-top: 5px;
  background: #f8fbf8;
}

.eval-select-row {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: start;
  gap: 8px;
  border-radius: 8px;
  padding: 7px;
  color: #43594a;
  cursor: pointer;
  font-size: 0.75rem;
  line-height: 1.45;
}

.eval-select-row:hover {
  background: #edf5ee;
}

.eval-select-row small {
  color: #7b897e;
  white-space: nowrap;
}

.eval-suite-meta {
  display: flex;
  gap: 12px;
  margin-top: 10px;
}

.eval-case {
  margin-top: 10px;
  border-top: 1px solid #e3ebe4;
  padding-top: 10px;
}

.eval-case summary {
  display: grid;
  grid-template-columns: 74px minmax(0, 1fr) auto;
  gap: 8px;
  color: #344c3b;
  cursor: pointer;
  font-size: 0.8rem;
  line-height: 1.5;
}

.eval-case-id {
  color: #28603b;
  font-family: Consolas, monospace;
  font-weight: 700;
}

.eval-case-count {
  color: #76867b;
  white-space: nowrap;
}

.eval-case-body {
  color: #5a6c60;
  font-size: 0.76rem;
}

.eval-case-body ul {
  display: grid;
  gap: 8px;
  margin: 8px 0 0;
  padding: 0;
  list-style: none;
}

.eval-case-body li {
  display: grid;
  gap: 3px;
  border-left: 3px solid #7ca989;
  padding-left: 8px;
}

.eval-case-body li.criterion-x {
  border-left-color: #c18151;
}

.eval-run-link {
  display: grid;
  width: 100%;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 3px 8px;
  margin-top: 8px;
  border: 1px solid #dbe5dc;
  border-radius: 11px;
  padding: 9px 10px;
  color: #42594a;
  background: #f8fbf8;
  cursor: pointer;
  text-align: left;
}

.eval-run-link.active {
  border-color: #5d8c6b;
  background: #edf6ee;
}

.eval-run-link small {
  grid-column: 1 / -1;
  color: #7b897e;
}

.eval-run-title {
  overflow: hidden;
  font-size: 0.82rem;
  font-weight: 800;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.eval-live-title {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 6px 12px;
}

.eval-live-title span {
  color: #6d7b71;
  font-size: 0.72rem;
  font-weight: 500;
}

.eval-form-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0 12px;
}

.eval-judge-config-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr));
  column-gap: 14px;
  row-gap: 18px;
  align-items: start;
}

.eval-judge-config-grid .eval-field {
  grid-template-rows: 22px 44px auto;
  align-items: start;
  margin-top: 0;
}

.eval-judge-config-grid .eval-field > span {
  min-height: 22px;
  line-height: 22px;
}

.eval-judge-config-grid .eval-field > input {
  box-sizing: border-box;
  height: 44px;
}

.eval-field-wide {
  grid-column: 1 / -1;
}

.eval-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 16px;
}

.eval-actions button,
.eval-heading-actions button,
.eval-secondary,
.eval-primary {
  border: 1px solid #b8cbbd;
  border-radius: 10px;
  padding: 9px 14px;
  color: #284b34;
  background: #f9fcf9;
  cursor: pointer;
}

.eval-primary {
  border-color: #245b38;
  color: #fff;
  background: #245b38;
}

.eval-actions button.danger,
.eval-heading-actions button.danger {
  border-color: #d4a5a5;
  color: #8b3030;
  background: #fff7f7;
}

.eval-actions button:disabled,
.eval-heading-actions button:disabled,
.eval-secondary:disabled,
.eval-primary:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.eval-revision-picker {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: end;
  gap: 12px;
  margin-top: 16px;
  padding: 12px;
  border: 1px solid #dce6de;
  border-radius: 10px;
  background: #f7faf7;
}

.eval-revision-picker .eval-field {
  margin: 0;
}

.eval-plan-status {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
  margin-top: 18px;
  border-radius: 12px;
  padding: 11px 13px;
  font-size: 0.76rem;
}

.eval-plan-status.passed {
  color: #28613d;
  background: #eaf5ec;
}

.eval-plan-status.failed {
  color: #8b3434;
  background: #fff1f1;
}

.eval-checks {
  display: grid;
  gap: 6px;
  max-height: 210px;
  overflow: auto;
  margin: 12px 0 0;
  padding: 0;
  list-style: none;
}

.eval-checks li {
  display: grid;
  grid-template-columns: 150px minmax(0, 1fr);
  gap: 8px;
  border-left: 3px solid #7ca989;
  padding: 5px 9px;
  color: #5c6c61;
  font-size: 0.74rem;
}

.eval-checks li.failed {
  border-left-color: #b44e4e;
}

.eval-plan-cases {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
  max-height: 260px;
  overflow: auto;
  margin-top: 12px;
}

.eval-plan-cases-heading {
  grid-column: 1 / -1;
  color: #445e4b;
  font-size: 0.76rem;
}

.eval-plan-cases article {
  display: grid;
  gap: 4px;
  border: 1px solid #e0e8e1;
  border-radius: 9px;
  padding: 8px 10px;
  color: #53665a;
  font-size: 0.73rem;
}

.eval-plan-cases article > div {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 5px 12px;
}

.eval-plan-cases small {
  color: #886126;
}

.eval-live-panel {
  border-color: rgba(36, 91, 56, 0.28);
}

.eval-run-source {
  display: block;
  margin-top: 4px;
  color: #6f7f74;
  font-size: 0.7rem;
}

.eval-run-time {
  display: block;
  margin-top: 3px;
  color: #7b897f;
  font-size: 0.68rem;
}

.eval-judgment-panel {
  border-color: rgba(65, 103, 79, 0.24);
}

.eval-prompt-review {
  display: grid;
  gap: 8px;
  margin-top: 12px;
}

.eval-prompt-review-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  color: #52675a;
  font-size: 0.72rem;
}

.eval-prompt-review-heading span {
  color: #7b897f;
  font-size: 0.67rem;
}

.eval-judge-prompt {
  border: 1px solid #dce6de;
  border-radius: 10px;
  padding: 9px 11px;
  color: #53665a;
  font-size: 0.75rem;
}

.eval-judge-prompt > summary {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  cursor: pointer;
}

.eval-judge-prompt > summary > span {
  display: grid;
  gap: 3px;
}

.eval-judge-prompt > summary small {
  color: #728178;
  font-size: 0.68rem;
  font-weight: 400;
  line-height: 1.5;
}

.eval-judge-prompt > summary code {
  color: #718077;
  font-size: 0.65rem;
}

.eval-judge-prompt pre {
  max-height: 320px;
  overflow: auto;
  margin: 10px 0 0;
  color: #405249;
  font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
  font-size: 0.72rem;
  line-height: 1.6;
  white-space: pre-wrap;
}

.eval-confirm-row {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  margin-top: 12px;
  border-radius: 10px;
  padding: 10px;
  color: #53665a;
  background: #f0f5f0;
  font-size: 0.76rem;
  line-height: 1.55;
}

.eval-judgment-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin-top: 12px;
  border-radius: 10px;
  padding: 10px;
  color: #53665a;
  background: #eef5ef;
  font-size: 0.74rem;
}

.eval-aggregate-board {
  display: grid;
  gap: 9px;
  margin-top: 12px;
}

.eval-aggregate-board > header,
.eval-case-aggregate-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.eval-aggregate-board > header span,
.eval-case-aggregate-head small {
  color: #728178;
  font-size: 0.68rem;
}

.eval-case-aggregate {
  display: grid;
  gap: 7px;
  border: 1px solid #dfe8e0;
  border-radius: 11px;
  padding: 10px;
  color: #52645a;
  background: #fbfdfb;
  font-size: 0.72rem;
}

.eval-case-aggregate-head > div {
  display: grid;
  gap: 2px;
}

.eval-case-aggregate-counts {
  display: flex;
  flex-wrap: wrap;
  gap: 7px 14px;
}

.eval-case-aggregate p,
.eval-attempt-aggregates p {
  margin: 0;
}

.eval-attempt-aggregates {
  display: grid;
  gap: 7px;
  margin-top: 7px;
}

.eval-attempt-aggregates > article {
  display: grid;
  gap: 4px;
  border-left: 3px solid #8aa894;
  padding: 7px 9px;
  background: #f4f8f4;
}

.eval-attempt-aggregates > article > div {
  display: flex;
  justify-content: space-between;
  gap: 8px;
}

.eval-attempt-id {
  overflow: hidden;
  color: #7b897f;
  font-family: Consolas, monospace;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.eval-binding-note {
  border-radius: 7px;
  padding: 6px 8px;
  color: #4f6557;
  background: #eef5ef;
}

.eval-binding-history {
  border-radius: 8px;
  padding: 7px 8px;
  background: #f6f9f6;
}

.eval-binding-history > article {
  display: grid;
  gap: 3px;
  margin-top: 7px;
  border-top: 1px solid #dfe8e0;
  padding-top: 7px;
}

.eval-binding-history > article > div {
  display: flex;
  justify-content: space-between;
  gap: 8px;
}

.eval-secondary-button {
  justify-self: start;
  border: 1px solid #9eb6a5;
  border-radius: 8px;
  padding: 6px 9px;
  color: #315d3d;
  background: #f4f9f4;
  cursor: pointer;
  font: inherit;
  font-weight: 700;
}

.eval-review-form {
  display: grid;
  gap: 8px;
  border: 1px solid #cbd9ce;
  border-radius: 10px;
  padding: 10px;
  background: #f5f9f5;
}

.eval-review-form textarea,
.eval-review-form input,
.eval-review-form select {
  width: 100%;
  box-sizing: border-box;
}

.eval-review-item {
  display: grid;
  grid-template-columns: minmax(150px, 1fr) minmax(130px, auto) minmax(180px, 1.5fr);
  gap: 7px;
  align-items: center;
  border-radius: 8px;
  padding: 7px;
  background: #fff;
}

.eval-check-guide {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 12px;
}

.eval-check-guide article {
  display: grid;
  gap: 4px;
  border: 1px solid #dfe8e0;
  border-radius: 10px;
  padding: 9px 11px;
  color: #53665a;
  background: #f8fbf8;
  font-size: 0.72rem;
}

.eval-check-guide strong {
  color: #345a42;
}

.eval-check-guide p {
  margin: 0;
  line-height: 1.55;
}

.eval-check-results {
  margin-top: 12px;
  border: 1px solid #dfe8e0;
  border-radius: 10px;
  padding: 9px 11px;
}

.eval-check-result {
  margin-top: 7px;
  border-top: 1px solid #e3eae4;
}

.eval-check-result > summary {
  display: grid;
  grid-template-columns: minmax(150px, 1fr) auto;
  gap: 3px 10px;
  padding: 7px 0;
  color: #53665a;
  cursor: pointer;
  font-size: 0.74rem;
}

.eval-check-result > summary small {
  grid-column: 1 / -1;
  color: #7b897f;
}

.eval-check-detail {
  display: grid;
  gap: 8px;
  padding: 8px 0 9px;
  color: #53665a;
  font-size: 0.72rem;
}

.eval-check-detail p,
.eval-check-detail blockquote,
.eval-check-detail ul {
  margin: 0;
}

.eval-check-notice {
  margin: 8px 0 0;
  border-radius: 8px;
  padding: 8px 9px;
  color: #66776c;
  background: #eef3ec;
  font-size: 0.7rem;
}

.eval-check-detail blockquote {
  border-left: 3px solid #72a17f;
  padding-left: 9px;
  color: #637269;
}

.eval-citation-result {
  display: grid;
  gap: 6px;
  border: 1px solid #e0e8e1;
  border-radius: 8px;
  padding: 8px;
  background: #f8fbf8;
}

.eval-citation-result header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.eval-check-invocation {
  border-radius: 8px;
  padding: 7px 8px;
  background: #f4f7f4;
}

.eval-check-invocation > summary {
  cursor: pointer;
  color: #52675a;
  font-size: 0.7rem;
}

.eval-check-invocation pre {
  max-height: 220px;
  overflow: auto;
  margin: 7px 0 0;
  border-radius: 7px;
  padding: 8px;
  background: #fff;
  white-space: pre-wrap;
}

.result-pass {
  color: #2c6b42;
  font-weight: 700;
}

.result-fail {
  color: #9a3636;
  font-weight: 700;
}

.result-pending {
  color: #895e20;
  font-weight: 700;
}

.result-exploratory {
  color: #6b5b2a;
  font-weight: 700;
}

.result-excluded {
  color: #777;
  font-weight: 700;
}

.eval-judgment-viewbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px 12px;
  margin-top: 12px;
}

.eval-judgment-viewbar > div {
  display: flex;
  gap: 6px;
}

.eval-judgment-viewbar button {
  border: 1px solid #d4e0d7;
  border-radius: 999px;
  padding: 5px 10px;
  color: #64746a;
  background: #fff;
  cursor: pointer;
  font: inherit;
  font-size: 0.7rem;
  font-weight: 700;
}

.eval-judgment-viewbar button.active {
  border-color: #4f805d;
  color: #fff;
  background: #3f7450;
}

.eval-attempt-judgment-groups {
  display: grid;
  gap: 9px;
  margin-top: 9px;
}

.eval-attempt-judgment-group {
  border: 1px solid #dfe8e0;
  border-left: 4px solid #6d9878;
  border-radius: 11px;
  padding: 9px 10px;
  background: #fbfdfb;
}

.eval-attempt-judgment-group.failed {
  border-left-color: #b04a4a;
  background: #fffafa;
}

.eval-attempt-judgment-group.pending {
  border-left-color: #b48a45;
  background: #fffdf7;
}

.eval-attempt-judgment-group.focused {
  outline: 2px solid #7da489;
  outline-offset: 2px;
}

.eval-attempt-judgment-group > summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  cursor: pointer;
  color: #3e5747;
}

.eval-attempt-judgment-group > summary > div {
  display: grid;
  gap: 2px;
}

.eval-attempt-judgment-group > summary small {
  color: #79867d;
  font-size: 0.65rem;
}

.eval-attempt-judgment-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 7px 16px;
  margin-top: 8px;
  color: #5f6f64;
  font-size: 0.7rem;
}

.eval-attempt-reason {
  margin: 7px 0 0;
  color: #7a4c4c;
  font-size: 0.7rem;
}

.eval-attempt-detail-actions {
  display: flex;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 9px;
}

.eval-attempt-trace-action {
  border: 1px solid #b8cbbb;
  border-radius: 8px;
  padding: 6px 9px;
  color: #315d3d;
  background: #f6faf6;
  cursor: pointer;
  font: inherit;
  font-size: 0.7rem;
  font-weight: 700;
}

.eval-answer-view {
  flex: 1 1 100%;
  border-radius: 8px;
  padding: 7px 8px;
  background: #f4f8f4;
}

.eval-answer-view > summary {
  cursor: pointer;
  color: #52675a;
  font-size: 0.7rem;
  font-weight: 700;
}

.eval-answer-view pre {
  max-height: 240px;
  overflow: auto;
  margin: 7px 0 0;
  border-radius: 7px;
  padding: 8px;
  color: #48574d;
  background: #fff;
  font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
  font-size: 0.7rem;
  line-height: 1.55;
  white-space: pre-wrap;
}

.eval-attempt-criterion-list {
  display: grid;
  gap: 7px;
  margin-top: 9px;
}

.eval-attempt-criterion-list > article {
  display: grid;
  gap: 5px;
  border: 1px solid #e1e9e2;
  border-radius: 9px;
  padding: 8px;
  color: #52645a;
  background: #fff;
  font-size: 0.72rem;
}

.eval-attempt-criterion-list > article > header {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
}

.eval-attempt-criterion-list > article > header > div {
  display: grid;
  gap: 2px;
}

.eval-attempt-criterion-list > article > header small {
  color: #78877d;
  font-size: 0.64rem;
}

.eval-judgment-groups {
  display: grid;
  gap: 12px;
  margin-top: 12px;
}

.eval-judgment-group {
  display: grid;
  gap: 9px;
  border: 1px solid #dfe8e0;
  border-radius: 12px;
  padding: 10px;
  background: #fbfdfb;
}

.eval-judgment-group-r {
  border-left: 4px solid #5d8f6b;
}

.eval-judgment-group-x {
  border-left: 4px solid #b48a45;
}

.eval-judgment-group-guide {
  display: flex;
  align-items: flex-start;
  gap: 9px;
}

.eval-judgment-group-guide > div {
  display: grid;
  gap: 3px;
}

.eval-judgment-group-guide h4 {
  margin: 0;
  color: #345a42;
  font-size: 0.82rem;
}

.eval-judgment-group-guide p {
  margin: 0;
  color: #66776c;
  font-size: 0.72rem;
  line-height: 1.55;
}

.eval-rule-badge {
  display: inline-grid;
  flex: 0 0 26px;
  place-items: center;
  width: 26px;
  height: 26px;
  border-radius: 999px;
  color: #fff;
  background: #5d8f6b;
  font-size: 0.72rem;
  font-weight: 800;
}

.eval-judgment-group-x .eval-rule-badge {
  background: #a77b36;
}

.eval-judgment-list {
  display: grid;
  gap: 8px;
  max-height: 360px;
  overflow: auto;
}

.eval-judgment-list > article {
  display: grid;
  gap: 7px;
  border: 1px solid #e0e8e1;
  border-radius: 10px;
  padding: 10px;
  color: #52645a;
  background: #fff;
  font-size: 0.75rem;
}

.eval-judgment-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.eval-judgment-head > div {
  display: grid;
  gap: 2px;
}

.eval-judgment-list p {
  margin: 0;
  line-height: 1.55;
}

.eval-judge-quote {
  border-left: 3px solid #72a17f;
  padding-left: 9px;
  color: #637269;
}

.eval-invocation {
  display: grid;
  gap: 4px;
  margin-top: 7px;
  border-top: 1px solid #e1e9e2;
  padding-top: 7px;
  font-size: 0.7rem;
}

.eval-invocation small {
  color: #7e8b82;
}

.eval-invocation-payload {
  border-radius: 7px;
  padding: 6px 8px;
  background: #f7faf7;
}

.eval-invocation-payload summary {
  color: #4f6657;
  cursor: pointer;
  font-size: 0.7rem;
}

.eval-invocation pre {
  max-height: 180px;
  overflow: auto;
  margin: 2px 0 0;
  border-radius: 7px;
  padding: 7px;
  color: #46564c;
  background: #f4f7f4;
  font-family: Consolas, monospace;
  white-space: pre-wrap;
}

.eval-run-state.running {
  color: #fff;
  background: #28603b;
}

.eval-run-state.paused {
  color: #785319;
  background: #f5e9ce;
}

.eval-metrics {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 8px;
  margin-top: 16px;
}

.eval-metrics div {
  display: grid;
  gap: 3px;
  border-radius: 11px;
  padding: 10px;
  background: #f0f6f0;
}

.eval-metrics span {
  color: #718077;
  font-size: 0.68rem;
}

.eval-metrics strong {
  color: #294c35;
  font-size: 0.94rem;
}

.eval-pause-reason,
.eval-notice,
.eval-success,
.eval-error {
  border-radius: 10px;
  padding: 10px 12px;
  font-size: 0.8rem;
}

.eval-pause-reason {
  color: #785319;
  background: #fff7e7;
}

.eval-notice {
  color: #536a5a;
  background: #edf4ed;
}

.eval-success {
  color: #285f3b;
  background: #e9f5eb;
}

.eval-error {
  color: #8b3434;
  background: #fff1f1;
}

.eval-modal-backdrop {
  position: fixed;
  z-index: 900;
  inset: 0;
  display: grid;
  place-items: center;
  box-sizing: border-box;
  padding: 28px;
  background: rgba(22, 38, 28, 0.42);
  backdrop-filter: blur(5px);
}

.eval-modal {
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  width: min(1050px, 100%);
  max-height: min(88vh, 920px);
  overflow: hidden;
  border: 1px solid #cddbcf;
  border-radius: 17px;
  background: #fbfdfb;
  box-shadow: 0 26px 70px rgba(17, 39, 24, 0.25);
}

.eval-modal-wide {
  width: min(1120px, 100%);
}

.eval-modal-trace {
  width: min(1320px, 100%);
}

.eval-modal-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  border-bottom: 1px solid #dce7de;
  padding: 16px 18px;
  background: #f4f8f4;
}

.eval-modal-header h3 {
  margin: 3px 0 4px;
  color: #203f2b;
  font-size: 1rem;
}

.eval-modal-header small {
  display: block;
  overflow: hidden;
  color: #728177;
  font-family: Consolas, monospace;
  font-size: 0.67rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.eval-modal-close {
  flex: 0 0 auto;
  border: 1px solid #b9cbbd;
  border-radius: 8px;
  padding: 6px 11px;
  color: #35553f;
  background: #fff;
  cursor: pointer;
  font: inherit;
  font-size: 0.72rem;
  font-weight: 700;
}

.eval-modal-body {
  display: grid;
  align-content: start;
  gap: 10px;
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
  padding: 16px 18px 20px;
}

.eval-modal-question,
.eval-modal-criterion-rule {
  display: grid;
  gap: 4px;
  border-radius: 10px;
  padding: 9px 11px;
  color: #4d6254;
  background: #eef5ef;
  font-size: 0.72rem;
}

.eval-modal-question > span,
.eval-modal-criterion-rule > span {
  color: #6a7c70;
  font-size: 0.65rem;
  font-weight: 800;
  letter-spacing: 0.04em;
}

.eval-modal-question p,
.eval-modal-criterion-rule p {
  margin: 0;
  line-height: 1.6;
  white-space: pre-wrap;
}

.eval-modal-criterion-rule {
  background: #f7f4ea;
}

.eval-modal-section {
  display: grid;
  gap: 8px;
}

.eval-modal-section h4 {
  margin: 4px 0 0;
  color: #335440;
  font-size: 0.84rem;
}

.eval-modal-judgment,
.eval-modal-check {
  display: grid;
  gap: 6px;
  border: 1px solid #dfe8e0;
  border-radius: 10px;
  padding: 9px 11px;
  color: #56685b;
  background: #fff;
  font-size: 0.72rem;
}

.eval-modal-judgment > header,
.eval-modal-check > div {
  display: flex;
  align-items: center;
  gap: 8px;
}

.eval-modal-judgment > header > div {
  display: grid;
  flex: 1;
  gap: 2px;
}

.eval-modal-judgment p,
.eval-modal-check p {
  margin: 0;
  line-height: 1.55;
}

.eval-modal-judgment details > summary {
  color: #52705b;
  cursor: pointer;
  font-size: 0.68rem;
}

.eval-result-matrix {
  display: grid;
  gap: 12px;
}

.eval-baseline-panel {
  display: grid;
  gap: 13px;
}

.eval-baseline-current,
.eval-baseline-action,
.eval-comparison {
  display: grid;
  gap: 10px;
  border: 1px solid #d9e5db;
  border-radius: 12px;
  padding: 12px;
  background: #f7faf7;
}

.eval-baseline-current > header,
.eval-comparison > header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.eval-baseline-current h4,
.eval-comparison h4 {
  margin: 3px 0 0;
  color: #294c35;
}

.eval-baseline-current-badge {
  border-radius: 999px;
  padding: 4px 9px;
  color: #fff;
  background: #326947;
  font-size: 0.68rem;
  font-weight: 700;
}

.eval-baseline-current dl {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 7px 14px;
  margin: 0;
}

.eval-baseline-current dl > div {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.eval-baseline-current dt {
  color: #75847a;
  font-size: 0.66rem;
  font-weight: 700;
}

.eval-baseline-current dd {
  overflow: hidden;
  margin: 0;
  color: #42574a;
  font-size: 0.72rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.eval-baseline-current .eval-baseline-reason {
  grid-column: 1 / -1;
}

.eval-baseline-current .eval-baseline-reason dd {
  white-space: normal;
}

.eval-baseline-metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 5px 14px;
  color: #5d7064;
  font-size: 0.69rem;
}

.eval-baseline-action {
  justify-items: start;
}

.eval-baseline-action small {
  color: #6f7e74;
  line-height: 1.5;
}

.eval-baseline-action .eval-field {
  width: min(620px, 100%);
}

.eval-baseline-form-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}

.eval-error-text {
  color: #8f4141 !important;
}

.eval-baseline-history {
  border: 1px solid #dce7de;
  border-radius: 10px;
  padding: 9px 11px;
}

.eval-baseline-history > summary {
  color: #486050;
  cursor: pointer;
  font-size: 0.74rem;
  font-weight: 700;
}

.eval-baseline-history > article {
  display: grid;
  gap: 3px;
  margin-top: 8px;
  border-top: 1px solid #e1e9e2;
  padding-top: 8px;
  color: #607166;
  font-size: 0.69rem;
}

.eval-baseline-history > article.current {
  border-left: 3px solid #4f8660;
  padding-left: 8px;
}

.eval-baseline-history > article > div {
  display: flex;
  align-items: center;
  gap: 8px;
}

.eval-baseline-history > article > div span {
  border-radius: 999px;
  padding: 2px 6px;
  color: #fff;
  background: #3d7350;
  font-size: 0.61rem;
}

.eval-baseline-history p {
  margin: 0;
}

.eval-comparison {
  border-color: #cfdfd2;
  background: #fbfdfb;
}

.eval-comparison-warning {
  margin: 0;
  border-radius: 9px;
  padding: 8px 10px 8px 28px;
  color: #805d20;
  background: #fff7e2;
  font-size: 0.71rem;
}

.eval-comparison-note {
  margin: 0;
  border-radius: 9px;
  padding: 9px 11px;
  color: #586b5e;
  background: #eef4ef;
  font-size: 0.71rem;
  line-height: 1.6;
}

.eval-comparison-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}

.eval-comparison-summary article {
  display: grid;
  gap: 2px;
  border-radius: 9px;
  padding: 9px 10px;
  background: #f0f6f1;
}

.eval-comparison-summary span,
.eval-comparison-summary small {
  color: #6b7c70;
  font-size: 0.67rem;
}

.eval-comparison-summary strong {
  color: #2d543a;
  font-size: 0.92rem;
}

.eval-comparison-cases {
  display: grid;
  gap: 6px;
}

.eval-comparison-case {
  display: grid;
  grid-template-columns: minmax(150px, 1.4fr) auto auto auto auto minmax(180px, 1fr);
  align-items: center;
  gap: 6px 9px;
  border-left: 3px solid #93a397;
  border-radius: 8px;
  padding: 8px 9px;
  color: #617066;
  background: #f6f9f6;
  font-size: 0.68rem;
}

.eval-comparison-case.comparison-improved {
  border-left-color: #4e8b61;
  background: #f2f8f3;
}

.eval-comparison-case.comparison-regressed {
  border-left-color: #a94343;
  background: #fff7f7;
}

.eval-comparison-case.comparison-changed,
.eval-comparison-case.comparison-new,
.eval-comparison-case.comparison-removed {
  border-left-color: #b28742;
  background: #fffaf1;
}

.eval-comparison-case > div {
  display: grid;
  gap: 2px;
}

.eval-comparison-case small {
  color: #78867c;
}

.eval-matrix-summary {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}

.eval-matrix-summary article {
  display: grid;
  gap: 3px;
  border: 1px solid #dce7de;
  border-radius: 11px;
  padding: 10px 12px;
  background: #f7faf7;
}

.eval-matrix-summary article.warning {
  border-color: #deb9b9;
  background: #fff7f7;
}

.eval-matrix-summary span,
.eval-matrix-summary small {
  color: #718077;
  font-size: 0.68rem;
}

.eval-matrix-summary strong {
  color: #294c35;
  font-size: 0.94rem;
}

.eval-matrix-summary article.warning strong {
  color: #963f3f;
}

.eval-matrix-category {
  display: grid;
  gap: 8px;
  border: 1px solid #d8e5da;
  border-radius: 13px;
  padding: 12px;
  background: #f1f7f2;
}

.eval-matrix-category > header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px 14px;
}

.eval-matrix-category h4 {
  margin: 2px 0 0;
  color: #294c35;
  font-size: 0.95rem;
}

.eval-matrix-category-counts {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 11px;
  color: #607266;
  font-size: 0.68rem;
}

.eval-matrix-cases {
  display: grid;
  gap: 7px;
}

.eval-matrix-case {
  border: 1px solid #dce6de;
  border-left-width: 4px;
  border-radius: 10px;
  background: #fff;
}

.eval-matrix-case.matrix-result-pass {
  border-left-color: #4f8a60;
}

.eval-matrix-case.matrix-result-fail {
  border-left-color: #aa4646;
}

.eval-matrix-case.matrix-result-pending {
  border-left-color: #b48a45;
}

.eval-matrix-case.matrix-result-exploratory {
  border-left-color: #927d42;
}

.eval-matrix-case.matrix-case-attention-fail {
  border-left-color: #b23c3c;
  background: #fff1f0;
}

.eval-matrix-case.matrix-case-attention-pending {
  border-left-color: #b07820;
  background: #fff7e5;
}

.eval-matrix-case > summary {
  display: grid;
  grid-template-columns: minmax(210px, 1.6fr) repeat(6, minmax(86px, auto));
  align-items: center;
  gap: 8px 12px;
  padding: 10px 11px;
  color: #607266;
  cursor: pointer;
  font-size: 0.7rem;
}

.eval-matrix-case-title,
.eval-matrix-attempt-title {
  display: grid;
  min-width: 0;
  gap: 2px;
}

.eval-matrix-case-title small,
.eval-matrix-attempt-title small {
  overflow: hidden;
  color: #7b897f;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.eval-matrix-case-detail {
  display: grid;
  gap: 7px;
  border-top: 1px solid #e4ebe5;
  padding: 9px 11px 11px;
}

.eval-matrix-reason {
  margin: 0;
  color: #6c5862;
  font-size: 0.7rem;
}

.eval-matrix-attempt {
  display: grid;
  grid-template-columns: minmax(145px, 1.2fr) auto minmax(120px, 1fr) repeat(4, auto);
  align-items: center;
  gap: 7px 10px;
  border-radius: 9px;
  padding: 8px 9px;
  color: #617065;
  background: #f6f9f6;
  font-size: 0.68rem;
}

.eval-matrix-attempt.matrix-result-fail {
  background: #fdebea;
}

.eval-matrix-attempt.matrix-result-pending {
  background: #fff4d9;
}

.eval-matrix-attempt.matrix-result-excluded {
  background: #f2f3f2;
  color: #7b817c;
}

.eval-matrix-redline {
  color: #9a3636;
  font-weight: 700;
}

.eval-matrix-attempt-actions {
  grid-column: 1 / -1;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.eval-attempts {
  display: grid;
  gap: 6px;
  max-height: 380px;
  overflow: auto;
  margin-top: 16px;
}

.eval-attempt-row {
  display: grid;
  grid-template-columns: minmax(145px, 1.3fr) auto repeat(3, auto) auto;
  align-items: center;
  gap: 10px;
  border: 1px solid #e0e9e1;
  border-radius: 11px;
  padding: 9px 10px;
  color: #617065;
  font-size: 0.7rem;
}

.eval-attempt-row > div {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.eval-attempt-row small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.eval-attempt-status {
  display: grid;
  gap: 1px;
  justify-items: center;
  border-radius: 999px;
  padding: 3px 7px;
  color: #315d3d;
  background: #e8f4ea;
  font-weight: 700;
}

.eval-attempt-status small {
  font-size: 0.58rem;
  font-weight: 500;
}

.eval-attempt-status.failed,
.eval-attempt-status.outcome_unknown {
  color: #8a3535;
  background: #fff0f0;
}

.eval-attempt-status.running {
  color: #72511c;
  background: #fff3d8;
}

.eval-trace-link {
  border: 0;
  padding: 0;
  color: #2b6b43;
  background: transparent;
  cursor: pointer;
  font: inherit;
  text-decoration: underline;
}

.eval-history-tabs {
  display: flex;
  gap: 6px;
  margin-top: 18px;
  border-bottom: 1px solid #dce6de;
}

.eval-history-tabs button {
  border: 0;
  border-bottom: 2px solid transparent;
  padding: 8px 12px;
  color: #65766b;
  background: transparent;
  cursor: pointer;
  font: inherit;
  font-size: 0.76rem;
  font-weight: 700;
}

.eval-history-tabs button.active {
  border-bottom-color: #2d6942;
  color: #245b38;
}

.eval-trace-shell {
  min-width: 0;
}

.eval-empty {
  color: #7b897e;
  font-size: 0.78rem;
}

@media (max-width: 980px) {
  .eval-layout {
    grid-template-columns: 1fr;
  }

  .eval-metrics {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .eval-quick-picker,
  .eval-plan-cases {
    grid-template-columns: 1fr;
  }

  .eval-attempt-row {
    grid-template-columns: minmax(130px, 1fr) auto auto;
  }

  .eval-matrix-summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .eval-comparison-summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .eval-comparison-case {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .eval-matrix-case > summary,
  .eval-matrix-attempt {
    grid-template-columns: minmax(160px, 1fr) repeat(2, auto);
  }
}

@media (max-width: 680px) {
  .eval-workspace-tabs small {
    display: none;
  }

  .eval-workspace-tabs button {
    justify-items: center;
    text-align: center;
  }

  .eval-form-grid,
  .eval-metrics {
    grid-template-columns: 1fr 1fr;
  }

  .eval-judge-config-grid {
    grid-template-columns: 1fr;
  }

  .eval-check-guide {
    grid-template-columns: 1fr;
  }

  .eval-matrix-summary,
  .eval-matrix-case > summary,
  .eval-matrix-attempt,
  .eval-baseline-current dl,
  .eval-comparison-summary,
  .eval-comparison-case {
    grid-template-columns: 1fr;
  }

  .eval-modal-backdrop {
    padding: 10px;
  }

  .eval-modal {
    max-height: 94vh;
    border-radius: 12px;
  }

  .eval-modal-header,
  .eval-modal-body {
    padding: 12px;
  }

  .eval-review-item {
    grid-template-columns: 1fr;
  }

  .eval-revision-picker {
    grid-template-columns: 1fr;
  }

  .eval-field-wide {
    grid-column: 1 / -1;
  }
}
</style>
