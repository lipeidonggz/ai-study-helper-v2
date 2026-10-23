<script setup lang="ts">
import { invoke } from "@tauri-apps/api/core";
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { fetchWithTimeout } from "./api";

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
  trace_id: string | null;
  trace_status: "complete" | "partial" | "missing";
  execution_status: string;
  termination_reason: string | null;
  error_code: string | null;
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
  suite_id: string | null;
  suite_name: string | null;
  suite_revision_id: string | null;
  suite_revision_number: number | null;
  status: string;
  pause_reason: string | null;
  created_at: string;
  started_at: string | null;
  sealed_at: string | null;
  totals: Record<string, number>;
  attempts: AttemptView[];
}

/** 评测 trace 的摘要与 span 时间线。 */
interface TraceDetail {
  manifest: {
    trace_id: string;
    status: string;
    termination_reason: string | null;
    input_tokens: number;
    output_tokens: number;
    question_preview: string;
  };
  spans: Array<{
    span_id: string;
    name: string;
    kind: string;
    status: string;
    started_at: string;
    ended_at: string;
    attributes: Record<string, unknown>;
  }>;
}

/** 评测 API 的稳定错误响应。 */
interface ApiErrorBody {
  error?: { code: string; message: string };
  detail?: string;
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
const traceDetail = ref<TraceDetail | null>(null);
const loading = ref(true);
const busy = ref(false);
const error = ref("");
const message = ref("");
let pollTimer: number | undefined;

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
    traceDetail.value = null;
    stopPolling();
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
    missing: "缺失",
    ok: "正常",
    error: "错误",
    dropped: "已丢弃",
  };
  return labels[status] ?? status;
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

/** 把 trace span 类型转换成中文名称。 */
function spanKindText(kind: string): string {
  const labels: Record<string, string> = {
    LLM: "模型调用",
    TOOL: "工具调用",
    INTERNAL: "内部步骤",
  };
  return labels[kind] ?? kind;
}

const runStatusLabel = computed(() => statusText(selectedRun.value?.status ?? ""));

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
    throw new Error(
      body.error?.message ?? body.detail ?? `请求失败（HTTP ${response.status}）`,
    );
  }
  return (await response.json()) as T;
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
    const [loadedSuites, loadedCases, collection, recentRuns] = await Promise.all([
      api<SuiteRevisionView[]>("/api/eval/suites"),
      api<CaseCatalogItem[]>("/api/eval/cases"),
      api<CurrentCollection | null>("/api/eval/collections/current"),
      api<RunView[]>("/api/eval/runs"),
    ]);
    suites.value = loadedSuites;
    caseCatalog.value = loadedCases;
    selectedSuiteRevisionId.value =
      loadedSuites[0]?.revision_id ?? selectedSuiteRevisionId.value;
    currentCollection.value = collection;
    snapshotId.value =
      collection?.manifest.snapshot_id ?? snapshotId.value;
    runs.value = recentRuns;
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
    }
  } catch (reason) {
    error.value = `刷新运行进度失败：${String(reason)}`;
    stopPolling();
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
  error.value = "";
  try {
    traceDetail.value = await api<TraceDetail>(
      `/api/eval/traces/${encodeURIComponent(traceId)}`,
    );
  } catch (reason) {
    error.value = `读取运行轨迹失败：${String(reason)}`;
  }
}

/** 切换查看的 ExecutionRun。 */
function selectRun(run: RunView): void {
  selectedRun.value = run;
  traceDetail.value = null;
  schedulePolling();
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

onMounted(() => {
  void connectAndLoad();
});

onBeforeUnmount(stopPolling);
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
            <span>{{ item.run_id.slice(0, 18) }}…</span>
            <strong>{{ statusText(item.status) }}</strong>
            <small>
              {{
                item.plan_mode === "exploratory"
                  ? "快速试跑"
                  : `${item.suite_name ?? "未知用例集"} · 第 ${
                      item.suite_revision_number ?? "?"
                    } 版`
              }}
              · {{ item.totals.attempts }} 个执行样本
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
                      {{ criterion.criterion_id }} · 修订 {{ criterion.revision_id }}
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
              <h3>{{ selectedRun.run_id }}</h3>
              <small class="eval-run-source">
                {{
                  selectedRun.plan_mode === "exploratory"
                    ? "快速试跑"
                    : `${selectedRun.suite_name ?? "未知用例集"} · 第 ${
                        selectedRun.suite_revision_number ?? "?"
                      } 版 · ${selectedRun.suite_revision_id ?? ""}`
                }}
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
              <span>有效答案</span>
              <strong>
                {{ selectedRun.totals.succeeded + selectedRun.totals.partial }}
              </strong>
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
        </section>

        <section
          v-if="traceDetail"
          v-show="['execute', 'history'].includes(evaluationTab)"
          class="eval-panel eval-trace-panel"
        >
          <div class="eval-panel-heading">
            <div>
              <span class="eval-kicker">运行轨迹</span>
              <h3>{{ traceDetail.manifest.trace_id }}</h3>
            </div>
            <span class="eval-count">{{ statusText(traceDetail.manifest.status) }}</span>
          </div>
          <p class="eval-trace-question">
            {{ traceDetail.manifest.question_preview }}
          </p>
          <ol class="eval-span-list">
            <li v-for="span in traceDetail.spans" :key="span.span_id">
              <strong>{{ span.name }}</strong>
              <span>{{ spanKindText(span.kind) }} · {{ statusText(span.status) }}</span>
              <small>{{ span.started_at }} → {{ span.ended_at }}</small>
            </li>
          </ol>
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

.eval-form-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0 12px;
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
.eval-primary:disabled {
  cursor: not-allowed;
  opacity: 0.5;
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
  border-radius: 999px;
  padding: 3px 7px;
  color: #315d3d;
  background: #e8f4ea;
  font-weight: 700;
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

.eval-trace-question {
  color: #56695c;
}

.eval-span-list {
  display: grid;
  gap: 7px;
  margin: 0;
  padding-left: 20px;
}

.eval-span-list li {
  display: grid;
  grid-template-columns: minmax(160px, 1fr) auto;
  gap: 2px 10px;
  color: #53655a;
  font-size: 0.76rem;
}

.eval-span-list small {
  grid-column: 1 / -1;
  color: #839087;
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

  .eval-field-wide {
    grid-column: 1 / -1;
  }
}
</style>
