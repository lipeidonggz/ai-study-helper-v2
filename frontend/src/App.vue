<script setup lang="ts">
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
import {
  computed,
  defineAsyncComponent,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
} from "vue";

import { API_TIMEOUT_MS, fetchWithTimeout } from "./api";
import {
  normalizeChatSourceKey,
  renderChatMarkdown,
  type ChatCitationAnnotation,
} from "./chat-markdown";
import TraceDetailViewer from "./TraceDetailViewer.vue";
import {
  replayScopeLabel,
  traceModeLabel,
  traceStatusLabel,
} from "./trace";
import type {
  TraceArtifact,
  TraceDetail,
  TraceListItem,
  TraceMode,
} from "./trace";

/** Python sidecar 的 UI 生命周期状态。 */
type ManagerStatus =
  | "starting"
  | "running"
  | "restarting"
  | "failed"
  | "stopped";
/** 当前版本支持的文档来源类型。 */
type SourceKind = "file" | "url";
/** 主导航中的稳定工作区标识。 */
type AppSection =
  | "chat"
  | "knowledge"
  | "ingest"
  | "history"
  | "settings"
  | "diagnostics"
  | "evaluation";
/** 入库工作区的两个子页面。 */
type IngestMode = "batch" | "single";

const evalConsoleEnabled = import.meta.env.VITE_EVAL_CONSOLE === "true";
const EvaluationConsole = evalConsoleEnabled
  ? defineAsyncComponent(() => import("./EvaluationConsole.vue"))
  : null;

/** Tauri 返回的后端进程状态。 */
interface BackendStatus {
  status: ManagerStatus;
  port: number;
  restartCount: number;
  message: string | null;
}

/** 本地 sidecar 的随机回环地址和本次启动令牌。 */
interface BackendConnection {
  baseUrl: string;
  token: string;
}

/** 通过认证的运行状态响应。 */
interface RuntimeInfo {
  status: string;
  version: string;
  pid: number;
  uptime_seconds: number;
}

/** S0 留痕后的来源摘要。 */
interface SourceArtifact {
  source_kind: SourceKind;
  source_key: string;
  source_uri: string;
  final_url: string | null;
  media_type: string;
  content_hash: string;
  charset: string;
}

/** 预览中使用的简化语义节点。 */
interface SemanticNode {
  ordinal: number;
  kind: string;
  text: string;
  heading_level: number | null;
}

/** S1 提取结果及其统计。 */
interface ExtractedDocument {
  extraction_hash: string;
  title_candidate: string;
  language: "zh" | "en" | "mixed" | "unknown";
  nodes: SemanticNode[];
  stats: {
    node_count: number;
    character_count: number;
    heading_count: number;
  };
}

/** 预览章节树中的一条章节记录。 */
interface SectionRecord {
  path: string[];
  title: string;
  depth: number;
  ordinal: string;
  own_node_ordinals: number[];
  first_line: string;
}

/** 可引用证据块的预览字段。 */
interface BlockRecord {
  block_id: string;
  section_path: string[];
  block_index: number;
  text: string;
  context_token_estimate: number;
  source_node_ordinals: number[];
}

/** HTML 预览接口的完整响应。 */
interface PreviewResponse {
  job_id: string;
  source: SourceArtifact;
  document: {
    preview_doc_id: string;
    title: string;
    source_label: string;
    language: "zh" | "en" | "mixed" | "unknown";
    aliases: string[];
  };
  extracted: ExtractedDocument;
  section_tree: {
    tree_hash: string;
    sections: SectionRecord[];
    warnings: string[];
  };
  block_set: {
    blocks: BlockRecord[];
    warnings: string[];
  };
  warnings: string[];
}

/** 本地 API 的稳定错误结构。 */
interface ApiErrorBody {
  detail?: string;
  error?: {
    code: string;
    message: string;
    retryable: boolean;
  };
}

/** embedding 设置和最近一次连通性测试。 */
interface ModelSettingsResponse {
  embedding_configured: boolean;
  embedding_model_id: string;
  embedding_dimension: number;
  embedding_last_test: {
    status: "success" | "failed";
    model_id: string;
    dimension: number;
    request_id: string | null;
    input_tokens: number;
    message: string | null;
    tested_at: string;
  } | null;
  chat_configured: boolean;
  chat_model_id: string;
}

/** 对话模型连通性测试结果。 */
interface ChatProviderTestResult {
  status: "success" | "failed";
  model_id: string;
  request_id: string | null;
  input_tokens: number;
  output_tokens: number;
  message: string | null;
  tested_at: string;
}

/** 用户可调的检索与 Agent 运行参数。 */
interface RuntimeSettingsResponse {
  top_k: number;
  max_output_tokens: number;
  max_context_tokens: number;
  max_request_tokens: number;
  max_tool_calls: number;
  trace_mode: TraceMode;
  updated_at: string | null;
}

/** 运行设置表单中各字段的即时校验错误。 */
type RuntimeSettingsFieldErrors = Record<
  "top_k" | "max_output_tokens" | "max_context_tokens" | "max_request_tokens" | "max_tool_calls",
  string
>;

/** 本次问答运行对用户可见的生命周期状态。 */
type AgentChatStatus =
  | "idle"
  | "starting"
  | "running"
  | "reconnecting"
  | "succeeded"
  | "partial"
  | "failed"
  | "canceled"
  | "interrupted";

/** 流式问答返回的一条结构化来源。 */
interface AgentChatSource {
  source_key: string;
  doc_id: string;
  block_id: string;
  doc_title: string;
  source_label: string;
  section_path: string[];
  excerpt: string;
  tool: string;
}

/** 引用角标旁的来源悬浮卡及其视口锚点。 */
interface ChatCitationPopover {
  source: AgentChatSource;
  left: number;
  top: number;
  placement: "above" | "below";
}

/** 后置 citation annotator 返回的一条答案 span 引用。 */
interface AgentChatCitation extends ChatCitationAnnotation {}

/** SSE 事件 envelope 中前端会消费的载荷字段。 */
interface AgentStreamPayload {
  run_id?: string;
  snapshot_id?: string;
  label?: string;
  stage?: string;
  tool?: string;
  delta?: string;
  answer?: string | null;
  answer_completeness?: "complete" | "partial";
  termination_reason?: string;
  input_tokens?: number;
  output_tokens?: number;
  input_source?: "provider" | "estimated";
  output_source?: "provider" | "estimated";
  sources?: AgentChatSource[];
  annotations?: AgentChatCitation[];
  status?: "complete" | "pending" | "not_applicable";
  span_version?: string;
  batch_count?: number;
  batch_version?: string;
  source_meta?: {
    status: "complete" | "pending" | "not_applicable";
    rule_version: string;
    prompt_version: string;
    prompt_hash: string;
    model_calls: number;
    excluded_span_count: number;
    fallback_to_all_spans: boolean;
    error_code: string | null;
  };
  error_code?: string | null;
  source_count?: number;
  message?: string;
  reason?: string;
  limitations?: Array<{ code: string; message: string }>;
}

/** 流式 SSE 事件及其稳定序号。 */
interface AgentStreamEnvelope {
  schema_version: string;
  trace_id: string;
  sequence: number;
  event_type: string;
  payload: AgentStreamPayload;
}

/** 流式问答启动接口返回的运行标识。 */
interface AgentRunStartResponse {
  run_id: string;
  trace_id: string;
  status: "running";
}

/** 后台任务的可查询生命周期状态。 */
type BackgroundTaskStatus =
  | "queued"
  | "running"
  | "paused"
  | "succeeded"
  | "failed"
  | "canceled"
  | "interrupted";

/** 后台任务队列中的一个来源。 */
interface BackgroundSource {
  source_kind: "file" | "url";
  source_value: string;
}

/** 后台任务 API 返回的进度与失败信息。 */
interface BackgroundTaskRecord {
  task_id: string;
  status: BackgroundTaskStatus;
  stage: string;
  sources: BackgroundSource[];
  total_sources: number;
  next_source_index: number;
  completed_sources: number;
  failed_sources: number;
  successful_job_ids: string[];
  failures: Array<{
    source_index: number;
    source_kind: "file" | "url";
    job_id: string | null;
    stage: string;
    code: string;
    message: string;
    retryable: boolean;
  }>;
  snapshot_id: string | null;
  error_code: string | null;
  error_message: string | null;
  retryable: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

/** 当前活动 collection 的 pointer 与摘要。 */
interface CurrentCollectionResponse {
  pointer: {
    snapshot_id: string;
    manifest_hash: string;
    generation: number;
    previous_snapshot_id: string | null;
    published_at: string;
  };
  manifest: {
    status: "ready" | "ready_partial";
    completeness: "complete" | "partial";
    documents: Array<{
      doc_id: string;
      doc_version: number;
      title: string;
      source_label: string;
      section_count: number;
      block_count: number;
    }>;
    totals: {
      documents: number;
      sections: number;
      blocks: number;
    };
    failed_documents: Array<{
      job_id: string;
      code: string;
      message: string;
    }>;
  };
}

/** 全量清空本地知识库后返回的删除统计。 */
interface ResetWorkspaceResponse {
  status: "reset";
  documents: number;
  document_versions: number;
  source_artifacts: number;
  ingestion_jobs: number;
  ingestion_tasks: number;
  collection_publish_records: number;
  active_collection_pointer: number;
  cleanup_complete: boolean;
}

/** 当前活动文档的一个历史版本。 */
interface DocumentVersionDetail {
  doc_version: number;
  title: string;
  language: "zh" | "en" | "mixed" | "unknown";
  extractor_name: string;
  extractor_version: string;
  created_at: string;
  is_active: boolean;
}

/** 详情页折叠展示的解析与索引配置。 */
interface DocumentTechnicalDetail {
  extractor_name: string;
  extractor_version: string;
  section_builder_name: string;
  section_builder_version: string;
  chunker_name: string;
  chunker_version: string;
  embedding_model_id: string;
  embedding_dimension: number;
  extraction_hash: string;
}

/** 当前活动 collection 中一篇文档的完整只读详情。 */
interface DocumentDetailResponse {
  snapshot_id: string;
  published_at: string;
  doc_id: string;
  doc_version: number;
  title: string;
  source_label: string;
  language: "zh" | "en" | "mixed" | "unknown";
  source: {
    source_kind: "file" | "url";
    source_uri: string;
    final_url: string | null;
    content_hash: string;
    acquisition_mode: string;
    charset: string;
  };
  sections: SectionRecord[];
  blocks: BlockRecord[];
  versions: DocumentVersionDetail[];
  activities: BackgroundTaskRecord[];
  technical: DocumentTechnicalDetail;
  warnings: string[];
}

/** 详情页中的一个章节及其直属 blocks。 */
interface DocumentSectionContent {
  section: SectionRecord;
  blocks: BlockRecord[];
}

const managerStatus = ref<ManagerStatus>("starting");
const connection = ref<BackendConnection | null>(null);
const runtime = ref<RuntimeInfo | null>(null);
const restartCount = ref(0);
const message = ref("正在连接本地后端…");
const sourceKind = ref<SourceKind>("file");
const selectedFilePath = ref("");
const urlValue = ref("");
const preview = ref<PreviewResponse | null>(null);
const singleIngestTask = ref<BackgroundTaskRecord | null>(null);
const ingestError = ref("");
const saveError = ref("");
const ingesting = ref(false);
const saving = ref(false);
const modelSettings = ref<ModelSettingsResponse | null>(null);
const embeddingKey = ref("");
const chatKey = ref("");
const runtimeSettings = ref<RuntimeSettingsResponse | null>(null);
const runtimeSettingsForm = ref<RuntimeSettingsResponse>({
  top_k: 5,
  max_output_tokens: 8192,
  max_context_tokens: 100000,
  max_request_tokens: 400000,
  max_tool_calls: 8,
  trace_mode: "metadata",
  updated_at: null,
});
const runtimeSettingsFieldErrors = computed(() =>
  validateRuntimeSettings(runtimeSettingsForm.value),
);
const runtimeSettingsHasErrors = computed(() =>
  Object.values(runtimeSettingsFieldErrors.value).some(Boolean),
);
const embeddingSettingsBusy = ref(false);
const chatSettingsBusy = ref(false);
const runtimeSettingsBusy = ref(false);
const settingsLoading = ref(true);
const settingsError = ref("");
const settingsMessage = ref("");
const chatSettingsError = ref("");
const chatSettingsMessage = ref("");
const runtimeSettingsError = ref("");
const runtimeSettingsMessage = ref("");
const diagnosticsTraces = ref<TraceListItem[]>([]);
const diagnosticsLoading = ref(false);
const diagnosticsError = ref("");
const selectedTrace = ref<TraceDetail | null>(null);
const selectedTraceId = ref("");
const diagnosticsTraceModeBusy = ref(false);
const diagnosticsTraceModeMessage = ref("");
const diagnosticsTraceModeError = ref("");
const taskUrl = ref("");
const taskSources = ref<BackgroundSource[]>([]);
const backgroundTask = ref<BackgroundTaskRecord | null>(null);
const activeTasks = ref<BackgroundTaskRecord[]>([]);
const selectedTaskId = ref<string | null>(null);
const currentCollection = ref<CurrentCollectionResponse | null>(null);
const taskHistory = ref<BackgroundTaskRecord[]>([]);
const historyBusy = ref(false);
const historyError = ref("");
const taskBusy = ref(false);
const taskError = ref("");
const resetDialogOpen = ref(false);
const resetConfirmation = ref("");
const resetBusy = ref(false);
const resetError = ref("");
const resetMessage = ref("");
const activeSection = ref<AppSection>(readSectionFromHash());
const activeDocumentId = ref<string | null>(readDocumentIdFromHash());
const ingestMode = ref<IngestMode>("batch");
const documentDetail = ref<DocumentDetailResponse | null>(null);
const documentDetailLoading = ref(false);
const documentDetailError = ref("");
const documentDetailTab = ref<"content" | "versions" | "technical">("content");
const highlightedDocumentBlockId = ref("");
const chatQuestion = ref("");
const chatRunId = ref("");
const chatTraceId = ref("");
const chatTraceAvailable = ref(false);
const chatSnapshotId = ref("");
const chatStatus = ref<AgentChatStatus>("idle");
const chatActivity = ref("等待提问");
const chatAnswer = ref("");
const chatSources = ref<AgentChatSource[]>([]);
const chatCitations = ref<AgentChatCitation[]>([]);
const chatInputTokens = ref(0);
const chatOutputTokens = ref(0);
const chatInputSource = ref<"provider" | "estimated">("estimated");
const chatOutputSource = ref<"provider" | "estimated">("estimated");
const chatError = ref("");
const chatWarning = ref("");
const chatStopping = ref(false);
const chatLastEventId = ref(0);
const chatSourcesExpanded = ref(false);
const expandedChatSourceKey = ref("");
const chatSourceNotice = ref("");
const chatCitationPopover = ref<ChatCitationPopover | null>(null);
const pendingChatEvents: AgentStreamEnvelope[] = [];

let unlisten: UnlistenFn | undefined;
let healthTimer: number | undefined;
let taskTimer: number | undefined;
let taskRefreshInFlight = false;
let disposed = false;
let chatStreamController: AbortController | undefined;
let pendingDocumentBlockId = "";
let chatEventPump: Promise<void> | null = null;
let activeCitationElement: HTMLElement | null = null;

/** 从 URL hash 解析当前工作区；未知值回退到问答工作台。 */
function readSectionFromHash(): AppSection {
  const value = window.location.hash.replace(/^#/, "").split("/")[0];
  return value === "knowledge" ||
    value === "ingest" ||
    value === "history" ||
    value === "settings" ||
    value === "diagnostics" ||
    (evalConsoleEnabled && value === "evaluation")
    ? value
    : "chat";
}

/** 从知识库详情 URL 中读取活动文档 ID。 */
function readDocumentIdFromHash(): string | null {
  const parts = window.location.hash.replace(/^#/, "").split("/");
  if (parts[0] !== "knowledge" || !parts[1]) {
    return null;
  }
  try {
    return decodeURIComponent(parts[1]);
  } catch {
    return null;
  }
}

/** 从诊断页 URL 中读取选中的 trace ID。 */
function readTraceIdFromHash(): string | null {
  const parts = window.location.hash.replace(/^#/, "").split("/");
  if (parts[0] !== "diagnostics" || !parts[1]) {
    return null;
  }
  try {
    return decodeURIComponent(parts[1]);
  } catch {
    return null;
  }
}

/** 切换主导航工作区，并保留可刷新、可后退的 URL 位置。 */
function selectSection(section: AppSection): void {
  activeSection.value = section;
  activeDocumentId.value = null;
  documentDetail.value = null;
  window.location.hash = section;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/** 浏览器前进或修改 hash 时同步当前工作区。 */
function syncSectionFromHash(): void {
  activeSection.value = readSectionFromHash();
  activeDocumentId.value = readDocumentIdFromHash();
  if (activeDocumentId.value) {
    void loadAndFocusDocumentDetail(activeDocumentId.value);
  } else {
    documentDetail.value = null;
  }
  if (activeSection.value === "diagnostics") {
    void loadTraceDiagnostics(readTraceIdFromHash());
  }
}

/** 把后端状态转换成用户可读标签。 */
const stateLabel = computed(() => {
  switch (managerStatus.value) {
    case "starting":
      return "正在启动";
    case "running":
      return runtime.value ? "运行正常" : "等待健康检查";
    case "restarting":
      return "正在恢复";
    case "failed":
      return "启动失败";
    case "stopped":
      return "已停止";
  }
});

/** 选择状态指示点的视觉样式。 */
const stateClass = computed(() => {
  const visualStatus =
    managerStatus.value === "running" && !runtime.value
      ? "starting"
      : managerStatus.value;
  return `status-${visualStatus}`;
});

/** 只有后端健康且没有进行中的提取时才允许预览。 */
const canIngest = computed(
  () =>
    managerStatus.value === "running" &&
    runtime.value !== null &&
    !ingesting.value,
);

/** 单篇入库要求预览成功、embedding 可用且当前没有保存请求。 */
const canSave = computed(
  () =>
    managerStatus.value === "running" &&
    runtime.value !== null &&
    preview.value !== null &&
    modelSettings.value?.embedding_configured === true &&
    (!singleIngestTask.value ||
      ["failed", "canceled", "interrupted"].includes(singleIngestTask.value.status)) &&
    !saving.value,
);

/** 后台任务是否具备提交条件。 */
const canStartTask = computed(
  () =>
    managerStatus.value === "running" &&
    modelSettings.value?.embedding_configured === true &&
    taskSources.value.length > 0 &&
    !taskBusy.value,
);

/** 当前展开的任务是否仍可暂停或取消。 */
const selectedTaskCanControl = computed(
  () =>
    backgroundTask.value !== null &&
    ["queued", "running", "paused", "interrupted"].includes(backgroundTask.value.status),
);

/** 是否存在正在执行或等待执行的任务。 */
const hasExecutableTasks = computed(() =>
  activeTasks.value.some((task) =>
    ["queued", "running"].includes(task.status),
  ),
);

/** 入库导航中的队列摘要。 */
const taskQueueSummary = computed(() => {
  if (activeTasks.value.length === 0) {
    return "添加新文章";
  }
  const running = activeTasks.value.filter((task) => task.status === "running").length;
  const queued = activeTasks.value.filter((task) => task.status === "queued").length;
  const interrupted = activeTasks.value.filter(
    (task) => task.status === "interrupted",
  ).length;
  if (running || queued) {
    return `${running} 运行中 · ${queued} 排队`;
  }
  if (interrupted) {
    return `${interrupted} 个任务已中断`;
  }
  return `${activeTasks.value.length} 个任务待处理`;
});

/** 当前问答是否仍在接收事件。 */
const chatIsActive = computed(() =>
  ["starting", "running", "reconnecting"].includes(chatStatus.value),
);

/** 问答提交所需的服务、模型 Key 和知识库条件。 */
const canAskQuestion = computed(
  () =>
    managerStatus.value === "running" &&
    runtime.value !== null &&
    modelSettings.value?.embedding_configured === true &&
    modelSettings.value?.chat_configured === true &&
    currentCollection.value !== null &&
    !chatIsActive.value &&
    chatQuestion.value.trim().length > 0,
);

/** 把问答运行状态映射到用户可读标签。 */
const chatStatusLabel = computed(() => {
  const labels: Record<AgentChatStatus, string> = {
    idle: "等待提问",
    starting: "正在启动",
    running: "正在回答",
    reconnecting: "正在恢复连接",
    succeeded: "回答完成",
    partial: "部分完成",
    failed: "运行失败",
    canceled: "已停止",
    interrupted: "连接已中断",
  };
  return labels[chatStatus.value];
});

/** 按来源首次出现顺序生成稳定的引用编号。 */
const chatSourceNumbers = computed(() => {
  const numbers = new Map<string, number>();
  chatSources.value.forEach((source, index) => {
    numbers.set(normalizeChatSourceKey(source.source_key), index + 1);
  });
  return numbers;
});

/** 渲染安全 Markdown；来源数字随本次运行来源列表实时更新。 */
const chatAnswerHtml = computed(() =>
  renderChatMarkdown(
    chatAnswer.value,
    chatSourceNumbers.value,
    chatCitations.value,
  ),
);

/** 展开或收起答案下方的来源汇总。 */
function toggleChatSources(): void {
  chatSourcesExpanded.value = !chatSourcesExpanded.value;
}

/** 单独展开一条来源的摘要；同一时间只展开一个正文摘要。 */
function toggleChatSourceCard(sourceKey: string): void {
  expandedChatSourceKey.value =
    expandedChatSourceKey.value === sourceKey ? "" : sourceKey;
}

/** 关闭引用悬浮卡，并恢复触发角标的可访问状态。 */
function closeChatCitationPopover(): void {
  if (activeCitationElement) {
    activeCitationElement.classList.remove("active");
    activeCitationElement.setAttribute("aria-expanded", "false");
  }
  activeCitationElement = null;
  chatCitationPopover.value = null;
}

/** 在触发角标附近计算不会越出视口的悬浮卡位置。 */
function openChatCitationPopover(
  source: AgentChatSource,
  citation: HTMLElement,
): void {
  closeChatCitationPopover();
  const rect = citation.getBoundingClientRect();
  const margin = 12;
  const gap = 8;
  const estimatedHeight = 230;
  const width = Math.min(360, window.innerWidth - margin * 2);
  const left = Math.min(
    Math.max(rect.left, margin),
    Math.max(margin, window.innerWidth - width - margin),
  );
  const showAbove =
    window.innerHeight - rect.bottom < estimatedHeight &&
    rect.top >= estimatedHeight;

  chatCitationPopover.value = {
    source,
    left,
    top: showAbove ? rect.top - gap : rect.bottom + gap,
    placement: showAbove ? "above" : "below",
  };
  activeCitationElement = citation;
  citation.classList.add("active");
  citation.setAttribute("aria-expanded", "true");
}

/** 代理安全 Markdown 中的来源点击，就地展示引用内容而不滚动正文。 */
function handleChatAnswerClick(event: MouseEvent): void {
  const target = event.target;
  if (!(target instanceof Element)) {
    return;
  }
  const citation = target.closest<HTMLElement>("[data-chat-source-key]");
  if (!citation) {
    return;
  }
  event.preventDefault();
  const normalizedKey = normalizeChatSourceKey(
    decodeURIComponent(citation.dataset.chatSourceKey ?? ""),
  );
  const source = chatSources.value.find(
    (candidate) =>
      normalizeChatSourceKey(candidate.source_key) === normalizedKey,
  );
  if (!source) {
    chatSourceNotice.value = "模型引用的来源未在本次检索结果中找到。";
    chatSourcesExpanded.value = true;
    return;
  }
  chatSourceNotice.value = "";
  openChatCitationPopover(source, citation);
}

/** 点击悬浮卡和引用角标之外的区域时关闭卡片。 */
function handleDocumentPointerDown(event: PointerEvent): void {
  if (!chatCitationPopover.value || !(event.target instanceof Element)) {
    return;
  }
  if (
    event.target.closest("[data-chat-citation-popover]") ||
    event.target.closest("[data-chat-source-key]")
  ) {
    return;
  }
  closeChatCitationPopover();
}

/** Escape 或页面滚动时关闭悬浮卡，避免卡片与角标脱离。 */
function handleChatCitationDismiss(event: Event): void {
  if (
    event.type === "keydown" &&
    (event as KeyboardEvent).key !== "Escape"
  ) {
    return;
  }
  if (
    event.type === "scroll" &&
    event.target instanceof Element &&
    event.target.closest("[data-chat-citation-popover]")
  ) {
    return;
  }
  closeChatCitationPopover();
}

/** 从悬浮卡进入文档详情；这是用户显式选择，不替代默认就地阅读。 */
function openChatCitationSourceDocument(source: AgentChatSource): void {
  closeChatCitationPopover();
  openChatSourceDocument(source);
}

/** 只有当前活动快照与本次运行一致时才允许打开文档详情。 */
const canOpenChatSourceDocument = computed(
  () =>
    chatSnapshotId.value.length > 0 &&
    currentCollection.value?.pointer.snapshot_id === chatSnapshotId.value,
);

/** 只有没有 queued／running 任务且确认短语完全匹配时才允许执行全量清空。 */
const canConfirmReset = computed(
  () =>
    !hasExecutableTasks.value &&
    !resetBusy.value &&
    resetConfirmation.value.trim() === "清空知识库",
);

/** 提取当前来源类型对应的路径或 URL。 */
const sourceValue = computed(() =>
  sourceKind.value === "file" ? selectedFilePath.value : urlValue.value.trim(),
);

/** 把语言检测结果转换成界面标签。 */
const languageLabel = computed(() => {
  switch (preview.value?.document.language) {
    case "zh":
      return "中文";
    case "en":
      return "English";
    case "mixed":
      return "中英混排";
    default:
      return "语言未知";
  }
});

/** 把文档语言转换成详情页标签。 */
function documentLanguageLabel(language: DocumentDetailResponse["language"]): string {
  const labels: Record<DocumentDetailResponse["language"], string> = {
    zh: "中文",
    en: "English",
    mixed: "中英混排",
    unknown: "语言未知",
  };
  return labels[language];
}

/** 把一篇文档的 blocks 按当前快照章节树聚合，保持章节原始顺序。 */
const documentSections = computed<DocumentSectionContent[]>(() => {
  if (!documentDetail.value) {
    return [];
  }
  const blocksByPath = new Map<string, BlockRecord[]>();
  for (const block of documentDetail.value.blocks) {
    const key = block.section_path.join("\u0000");
    const values = blocksByPath.get(key) ?? [];
    values.push(block);
    blocksByPath.set(key, values);
  }
  return documentDetail.value.sections.map((section) => ({
    section,
    blocks: blocksByPath.get(section.path.join("\u0000")) ?? [],
  }));
});

/** 把稳定 warning code 转换成可理解文本。 */
function warningLabel(code: string): string {
  const labels: Record<string, string> = {
    source_container_preserved_structures:
      "已保留源文章容器中的表格和图片结构",
    preamble_attached: "首个章节前的内容已归入该章节",
    duplicate_section_title: "存在同名章节，路径已自动编号",
    dns_rebinding_not_pinned: "网页来源未固定 DNS 解析地址",
    derived_artifacts_repaired: "已有文档版本，但本次补齐了缺失的章节或块产物",
  };
  return labels[code] ?? "存在一条提取告警";
}

/** 把后端错误码和详情组合成用户可操作的提示。 */
function ingestErrorMessage(code: string, detail: string): string {
  const labels: Record<string, string> = {
    javascript_rendering_unsupported:
      "该页面需要执行 JavaScript 才能显示正文；当前版本只支持静态或 SSR HTML。",
    login_required: "该页面需要登录或密码验证，当前版本无法读取。",
    no_extractable_content: "页面中没有提取到足够的正文内容。",
    extractor_internal_error: "HTML 解析器遇到内部错误，该来源未入库，请保留文件用于排查。",
    unsupported_media_type: "该地址返回的不是 HTML 页面。",
    ssrf_blocked: "为保护本机安全，不能访问 localhost、私网或链路本地地址。",
    invalid_url: "请输入公开的 HTTP 或 HTTPS 页面地址。",
    source_not_found: "选择的本地文件不存在或已经被移动。",
    source_unreadable: "无法读取该文件，请检查访问权限。",
    source_too_large: "文件或网页内容超过 20 MiB 限制。",
    empty_source: "来源内容为空。",
    network_error: "无法连接该网页，请检查网络或稍后重试。",
    database_initialization_failed: "本地知识库数据库初始化失败。",
    document_persistence_failed: "文档已提取，但保存到本地知识库失败。",
    job_artifact_missing: "预览产物已失效，请重新解析后再保存。",
    preview_consistency_failed: "预览结果与保存结果不一致，请重新解析文档。",
    document_version_artifact_invalid:
      "已有文档版本引用的章节或块产物缺失、损坏或不一致，已停止保存。",
    snapshot_version_mismatch:
      "当前知识库由旧解析器版本生成，无法与新版本混用；请先清空知识库，再重新导入全部文档。",
  };
  const label = labels[code];
  return label ? `${label}（错误码：${code}）` : `${detail}（错误码：${code}）`;
}

/** 把设置 API 错误转换成用户可操作的提示。 */
function settingsErrorMessage(code: string, detail: string): string {
  const labels: Record<string, string> = {
    embedding_api_key_missing: "请先保存 embedding API Key，再进行连通性测试。",
    embedding_authentication_failed: "Key 无效或没有 embedding 权限。",
    embedding_rate_limited: "请求被限流，请稍后再测试。",
    embedding_upstream_unavailable: "暂时无法连接 embedding 服务，请检查网络后重试。",
    chat_api_key_missing: "请先保存对话 API Key，再进行连通性测试。",
    chat_authentication_failed: "对话 Key 无效或没有模型权限。",
    chat_rate_limited: "对话模型请求被限流，请稍后再测试。",
    chat_upstream_unavailable: "暂时无法连接对话模型，请检查网络后重试。",
    chat_request_rejected: "对话模型拒绝了测试请求，请检查 Key 和账户状态。",
  };
  return labels[code]
    ? `${labels[code]}（错误码：${code}）`
    : `${detail}（错误码：${code}）`;
}

/** 把知识库重置失败原因转换成普通用户可操作的中文提示。 */
function workspaceResetErrorMessage(code: string, detail: string): string {
  const labels: Record<string, string> = {
    workspace_reset_active_task: "后台入库任务仍在运行，请等待结束或取消任务后再清空。",
    workspace_reset_in_progress: "另一个清空操作正在执行，请稍后重试。",
    workspace_reset_failed: "清空知识库失败，原有数据已保持或恢复，请检查日志后重试。",
    data_root_unavailable: "本地数据目录不可用，无法清空知识库。",
  };
  return labels[code]
    ? `${labels[code]}（错误码：${code}）`
    : `${detail}（错误码：${code}）`;
}

/** 读取设置状态；响应不包含 Key 明文。 */
async function loadModelSettings(maxAttempts = 3): Promise<void> {
  if (!connection.value) {
    return;
  }
  settingsLoading.value = true;
  let lastError: unknown;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    try {
      const response = await fetchWithTimeout(
        `${connection.value.baseUrl}/api/settings/models`,
        {
          headers: { "x-launch-token": connection.value.token },
          cache: "no-store",
        },
        API_TIMEOUT_MS.quick,
      );
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      modelSettings.value = (await response.json()) as ModelSettingsResponse;
      settingsError.value = "";
      settingsLoading.value = false;
      return;
    } catch (error) {
      lastError = error;
      if (attempt < maxAttempts - 1) {
        await new Promise((resolve) =>
          window.setTimeout(resolve, 250 * (attempt + 1)),
        );
      }
    }
  }
  settingsLoading.value = false;
  settingsError.value = `读取模型设置失败：${String(lastError)}`;
}

/** 安全保存 embedding Key，成功后清空输入框。 */
async function saveEmbeddingKey(): Promise<void> {
  if (!connection.value || !embeddingKey.value.trim() || embeddingSettingsBusy.value) {
    return;
  }
  embeddingSettingsBusy.value = true;
  settingsError.value = "";
  settingsMessage.value = "";
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/settings/embedding-key`,
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-launch-token": connection.value.token,
        },
        body: JSON.stringify({ api_key: embeddingKey.value.trim() }),
      },
      API_TIMEOUT_MS.quick,
    );
    const body = (await response.json()) as ModelSettingsResponse | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      throw new Error(
        settingsErrorMessage(
          failure.error?.code ?? "request_failed",
          failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`,
        ),
      );
    }
    modelSettings.value = body as ModelSettingsResponse;
    embeddingKey.value = "";
    settingsMessage.value = "Key 已保存到 Windows Credential Manager。";
  } catch (error) {
    settingsError.value = String(error);
  } finally {
    embeddingSettingsBusy.value = false;
  }
}

/** 使用已保存 Key 做一次真实的短文本 embedding 检查。 */
async function testEmbeddingKey(): Promise<void> {
  if (!connection.value || embeddingSettingsBusy.value) {
    return;
  }
  embeddingSettingsBusy.value = true;
  settingsError.value = "";
  settingsMessage.value = "";
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/settings/embedding-key/test`,
      {
        method: "POST",
        headers: { "x-launch-token": connection.value.token },
      },
      API_TIMEOUT_MS.providerTest,
    );
    const body = (await response.json()) as
      | ModelSettingsResponse["embedding_last_test"]
      | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      throw new Error(
        settingsErrorMessage(
          failure.error?.code ?? "request_failed",
          failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`,
        ),
      );
    }
    const result = body as NonNullable<ModelSettingsResponse["embedding_last_test"]>;
    await loadModelSettings();
    settingsMessage.value =
      result.status === "success"
        ? `连接成功：${result.model_id}，${result.dimension} 维。`
        : `连接失败：${result.message ?? "未知错误"}`;
  } catch (error) {
    settingsError.value = String(error);
  } finally {
    embeddingSettingsBusy.value = false;
  }
}

/** 删除已保存 Key，并清除旧连通性结果。 */
async function deleteEmbeddingKey(): Promise<void> {
  if (!connection.value || embeddingSettingsBusy.value) {
    return;
  }
  embeddingSettingsBusy.value = true;
  settingsError.value = "";
  settingsMessage.value = "";
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/settings/embedding-key/delete`,
      {
        method: "POST",
        headers: { "x-launch-token": connection.value.token },
      },
      API_TIMEOUT_MS.quick,
    );
    const body = (await response.json()) as ModelSettingsResponse | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      throw new Error(
        settingsErrorMessage(
          failure.error?.code ?? "request_failed",
          failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`,
        ),
      );
    }
    modelSettings.value = body as ModelSettingsResponse;
    settingsMessage.value = "Key 已删除。";
  } catch (error) {
    settingsError.value = String(error);
  } finally {
    embeddingSettingsBusy.value = false;
  }
}

/** 安全保存独立的对话模型 Key。 */
async function saveChatKey(): Promise<void> {
  if (!connection.value || !chatKey.value.trim() || chatSettingsBusy.value) {
    return;
  }
  chatSettingsBusy.value = true;
  chatSettingsError.value = "";
  chatSettingsMessage.value = "";
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/settings/chat-key`,
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-launch-token": connection.value.token,
        },
        body: JSON.stringify({ api_key: chatKey.value.trim() }),
      },
      API_TIMEOUT_MS.quick,
    );
    const body = (await response.json()) as ModelSettingsResponse | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      throw new Error(
        settingsErrorMessage(
          failure.error?.code ?? "request_failed",
          failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`,
        ),
      );
    }
    modelSettings.value = body as ModelSettingsResponse;
    chatKey.value = "";
    chatSettingsMessage.value = "对话 Key 已保存到 Windows Credential Manager。";
  } catch (error) {
    chatSettingsError.value = String(error);
  } finally {
    chatSettingsBusy.value = false;
  }
}

/** 使用已保存的对话 Key 做一次最小真实补全检查。 */
async function testChatKey(): Promise<void> {
  if (!connection.value || chatSettingsBusy.value) {
    return;
  }
  chatSettingsBusy.value = true;
  chatSettingsError.value = "";
  chatSettingsMessage.value = "";
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/settings/chat-key/test`,
      {
        method: "POST",
        headers: { "x-launch-token": connection.value.token },
      },
      API_TIMEOUT_MS.providerTest,
    );
    const body = (await response.json()) as ChatProviderTestResult | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      throw new Error(
        settingsErrorMessage(
          failure.error?.code ?? "request_failed",
          failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`,
        ),
      );
    }
    const result = body as ChatProviderTestResult;
    await loadModelSettings();
    chatSettingsMessage.value =
      result.status === "success"
        ? `对话连接成功：${result.model_id}。`
        : `对话连接失败：${result.message ?? "未知错误"}`;
  } catch (error) {
    chatSettingsError.value = String(error);
  } finally {
    chatSettingsBusy.value = false;
  }
}

/** 删除已保存的对话 Key。 */
async function deleteChatKey(): Promise<void> {
  if (!connection.value || chatSettingsBusy.value) {
    return;
  }
  chatSettingsBusy.value = true;
  chatSettingsError.value = "";
  chatSettingsMessage.value = "";
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/settings/chat-key/delete`,
      {
        method: "POST",
        headers: { "x-launch-token": connection.value.token },
      },
      API_TIMEOUT_MS.quick,
    );
    const body = (await response.json()) as ModelSettingsResponse | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      throw new Error(
        settingsErrorMessage(
          failure.error?.code ?? "request_failed",
          failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`,
        ),
      );
    }
    modelSettings.value = body as ModelSettingsResponse;
    chatSettingsMessage.value = "对话 Key 已删除。";
  } catch (error) {
    chatSettingsError.value = String(error);
  } finally {
    chatSettingsBusy.value = false;
  }
}

/** 把后台任务状态转换成用户可读标签。 */
/** 打开高危清空确认；确认短语必须由用户重新输入。 */
function openResetDialog(): void {
  resetConfirmation.value = "";
  resetError.value = "";
  resetMessage.value = "";
  resetDialogOpen.value = true;
}

/** 关闭清空确认并清除刚才输入，避免后续误触直接执行。 */
function closeResetDialog(): void {
  if (resetBusy.value) {
    return;
  }
  resetDialogOpen.value = false;
  resetConfirmation.value = "";
}

/** 清空知识库、入库记录和派生文件，并重置页面中的运行时状态。 */
async function resetWorkspace(): Promise<void> {
  if (!connection.value || !canConfirmReset.value) {
    return;
  }
  resetBusy.value = true;
  resetError.value = "";
  resetMessage.value = "";
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/workspace/reset`,
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-launch-token": connection.value.token,
        },
        body: JSON.stringify({ confirmation: resetConfirmation.value.trim() }),
      },
      API_TIMEOUT_MS.maintenance,
    );
    const body = (await response.json()) as ResetWorkspaceResponse | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      throw new Error(
        workspaceResetErrorMessage(
          failure.error?.code ?? "request_failed",
          failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`,
        ),
      );
    }
    const result = body as ResetWorkspaceResponse;
    if (taskTimer !== undefined) {
      window.clearInterval(taskTimer);
      taskTimer = undefined;
    }
    currentCollection.value = null;
    activeDocumentId.value = null;
    documentDetail.value = null;
    documentDetailError.value = "";
    taskHistory.value = [];
    backgroundTask.value = null;
    activeTasks.value = [];
    selectedTaskId.value = null;
    taskSources.value = [];
    taskUrl.value = "";
    preview.value = null;
    singleIngestTask.value = null;
    resetDialogOpen.value = false;
    resetConfirmation.value = "";
    resetMessage.value = result.cleanup_complete
      ? `知识库已清空：删除 ${result.documents} 篇文档、${result.ingestion_tasks} 条入库记录。`
      : "知识库记录已清空，但隔离文件未能立即删除；应用会保留隔离目录用于后续清理。";
  } catch (error) {
    resetError.value = `清空知识库失败：${String(error)}`;
  } finally {
    resetBusy.value = false;
  }
}

/** 把后台任务状态转换成用户可读标签。 */
function taskStatusLabel(status: BackgroundTaskStatus): string {
  const labels: Record<BackgroundTaskStatus, string> = {
    queued: "排队中",
    running: "入库中",
    paused: "已暂停",
    succeeded: "已完成",
    failed: "失败",
    canceled: "已取消",
    interrupted: "已中断",
  };
  return labels[status];
}

/** 把后端运行设置复制到可编辑表单。 */
function applyRuntimeSettings(settings: RuntimeSettingsResponse): void {
  runtimeSettings.value = settings;
  runtimeSettingsForm.value = { ...settings };
}

/** 读取检索与 Agent 运行参数。 */
async function loadRuntimeSettings(maxAttempts = 3): Promise<void> {
  if (!connection.value) {
    return;
  }
  let lastError: unknown;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    try {
      const response = await fetchWithTimeout(
        `${connection.value.baseUrl}/api/settings/runtime`,
        {
          headers: { "x-launch-token": connection.value.token },
          cache: "no-store",
        },
        API_TIMEOUT_MS.quick,
      );
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      applyRuntimeSettings((await response.json()) as RuntimeSettingsResponse);
      runtimeSettingsError.value = "";
      return;
    } catch (error) {
      lastError = error;
      if (attempt < maxAttempts - 1) {
        await new Promise((resolve) =>
          window.setTimeout(resolve, 250 * (attempt + 1)),
        );
      }
    }
  }
  runtimeSettingsError.value = `读取运行设置失败：${String(lastError)}`;
}

/** 生成运行参数各字段的校验错误，并同步检查字段之间的预算关系。 */
function validateRuntimeSettings(value: RuntimeSettingsResponse): RuntimeSettingsFieldErrors {
  const errors: RuntimeSettingsFieldErrors = {
    top_k: "",
    max_output_tokens: "",
    max_context_tokens: "",
    max_request_tokens: "",
    max_tool_calls: "",
  };
  if (!Number.isInteger(value.top_k) || value.top_k < 3 || value.top_k > 10) {
    errors.top_k = "请输入 3–10 之间的整数。";
  }
  if (
    !Number.isInteger(value.max_output_tokens) ||
    value.max_output_tokens < 2048 ||
    value.max_output_tokens > 384000
  ) {
    errors.max_output_tokens = "请输入 2,048–384,000 之间的整数。";
  }
  if (
    !Number.isInteger(value.max_context_tokens) ||
    value.max_context_tokens < 10000 ||
    value.max_context_tokens > 1000000
  ) {
    errors.max_context_tokens = "请输入 10,000–1,000,000 之间的整数。";
  }
  if (
    !Number.isInteger(value.max_request_tokens) ||
    value.max_request_tokens < 10000
  ) {
    errors.max_request_tokens = "请输入不小于 10,000 的整数。";
  } else if (
    Number.isInteger(value.max_context_tokens) &&
    value.max_request_tokens < value.max_context_tokens
  ) {
    errors.max_request_tokens = "请求累计预算不能小于单次上下文预算。";
  }
  if (
    !Number.isInteger(value.max_tool_calls) ||
    value.max_tool_calls < 1 ||
    value.max_tool_calls > 20
  ) {
    errors.max_tool_calls = "请输入 1–20 之间的整数。";
  }
  if (
    !errors.max_context_tokens &&
    !errors.max_output_tokens &&
    value.max_context_tokens + value.max_output_tokens > 1000000
  ) {
    errors.max_output_tokens = "上下文预算与输出预算合计不能超过 1,000,000。";
  }
  return errors;
}

/** 清除上次保存或请求错误，让字段即时校验成为唯一当前反馈。 */
function clearRuntimeSettingsFeedback(): void {
  runtimeSettingsError.value = "";
  runtimeSettingsMessage.value = "";
}

/** 保存下一次问答使用的检索与 Agent 参数。 */
async function saveRuntimeSettings(): Promise<void> {
  if (!connection.value || runtimeSettingsBusy.value) {
    return;
  }
  const firstValidationError = Object.values(runtimeSettingsFieldErrors.value).find(Boolean);
  if (firstValidationError) {
    runtimeSettingsError.value = firstValidationError;
    runtimeSettingsMessage.value = "";
    return;
  }
  runtimeSettingsBusy.value = true;
  runtimeSettingsError.value = "";
  runtimeSettingsMessage.value = "";
  try {
    const payload = {
      top_k: runtimeSettingsForm.value.top_k,
      max_output_tokens: runtimeSettingsForm.value.max_output_tokens,
      max_context_tokens: runtimeSettingsForm.value.max_context_tokens,
      max_request_tokens: runtimeSettingsForm.value.max_request_tokens,
      max_tool_calls: runtimeSettingsForm.value.max_tool_calls,
      trace_mode: runtimeSettingsForm.value.trace_mode,
    };
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/settings/runtime`,
      {
        method: "PUT",
        headers: {
          "content-type": "application/json",
          "x-launch-token": connection.value.token,
        },
        body: JSON.stringify(payload),
      },
      API_TIMEOUT_MS.quick,
    );
    const body = (await response.json()) as RuntimeSettingsResponse | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      throw new Error(
        failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`,
      );
    }
    applyRuntimeSettings(body as RuntimeSettingsResponse);
    runtimeSettingsMessage.value = "运行设置已保存，将从下一次问答开始生效。";
  } catch (error) {
    runtimeSettingsError.value = String(error);
  } finally {
    runtimeSettingsBusy.value = false;
  }
}

/** 把表单恢复为推荐默认值，用户仍需点击保存。 */
function restoreRuntimeDefaults(): void {
  runtimeSettingsForm.value = {
    top_k: 5,
    max_output_tokens: 8192,
    max_context_tokens: 100000,
    max_request_tokens: 400000,
    max_tool_calls: 8,
    trace_mode: runtimeSettings.value?.trace_mode ?? "metadata",
    updated_at: null,
  };
  runtimeSettingsError.value = "";
  runtimeSettingsMessage.value = "已恢复推荐值，点击“保存运行设置”后生效。";
}

/** 保存全局 trace 记录模式，只影响之后创建的问答运行。 */
async function saveDiagnosticsTraceMode(): Promise<void> {
  if (!connection.value || diagnosticsTraceModeBusy.value) {
    return;
  }
  diagnosticsTraceModeBusy.value = true;
  diagnosticsTraceModeError.value = "";
  diagnosticsTraceModeMessage.value = "";
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/settings/runtime`,
      {
        method: "PUT",
        headers: {
          "content-type": "application/json",
          "x-launch-token": connection.value.token,
        },
        body: JSON.stringify({
          top_k: runtimeSettingsForm.value.top_k,
          max_output_tokens: runtimeSettingsForm.value.max_output_tokens,
          max_context_tokens: runtimeSettingsForm.value.max_context_tokens,
          max_request_tokens: runtimeSettingsForm.value.max_request_tokens,
          max_tool_calls: runtimeSettingsForm.value.max_tool_calls,
          trace_mode: runtimeSettingsForm.value.trace_mode,
        }),
      },
      API_TIMEOUT_MS.quick,
    );
    const body = (await response.json()) as RuntimeSettingsResponse | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      throw new Error(
        failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`,
      );
    }
    applyRuntimeSettings(body as RuntimeSettingsResponse);
    diagnosticsTraceModeMessage.value = "记录模式已保存，将从下一次问答开始生效。";
  } catch (error) {
    diagnosticsTraceModeError.value = String(error);
  } finally {
    diagnosticsTraceModeBusy.value = false;
  }
}

/** 读取最近 trace 摘要。 */
async function loadTraceList(): Promise<void> {
  if (!connection.value) {
    return;
  }
  diagnosticsLoading.value = true;
  diagnosticsError.value = "";
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/traces?limit=100`,
      {
        headers: { "x-launch-token": connection.value.token },
        cache: "no-store",
      },
      API_TIMEOUT_MS.quick,
    );
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    diagnosticsTraces.value = (await response.json()) as TraceListItem[];
  } catch (error) {
    diagnosticsError.value = `读取运行诊断失败：${String(error)}`;
  } finally {
    diagnosticsLoading.value = false;
  }
}

/** 读取一次 trace 的 manifest 与 span 树。 */
async function loadTraceDetail(traceId: string): Promise<void> {
  if (!connection.value) {
    return;
  }
  selectedTraceId.value = traceId;
  diagnosticsError.value = "";
  try {
    let body: TraceDetail | ApiErrorBody | null = null;
    for (let attempt = 0; attempt < 5; attempt += 1) {
      const response = await fetchWithTimeout(
        `${connection.value.baseUrl}/api/traces/${encodeURIComponent(traceId)}`,
        {
          headers: { "x-launch-token": connection.value.token },
          cache: "no-store",
        },
        API_TIMEOUT_MS.quick,
      );
      body = (await response.json()) as TraceDetail | ApiErrorBody;
      if (response.ok) {
        break;
      }
      if (response.status !== 404 || attempt === 4) {
        const failure = body as ApiErrorBody;
        const code = failure.error?.code ?? "request_failed";
        throw new Error(
          code === "trace_not_found"
            ? "本次运行没有可读取的 trace，可能未写入、已关闭或已被清理；问答结果不受影响。"
            : failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`,
        );
      }
      await new Promise((resolve) => window.setTimeout(resolve, 200));
    }
    selectedTrace.value = body as TraceDetail;
  } catch (error) {
    selectedTrace.value = null;
    diagnosticsError.value = `读取 trace 失败：${String(error)}`;
  }
}

/** 进入诊断工作区，并可直接打开指定 trace。 */
function openTraceDiagnostics(traceId = ""): void {
  selectedTrace.value = null;
  activeSection.value = "diagnostics";
  activeDocumentId.value = null;
  documentDetail.value = null;
  window.location.hash = traceId
    ? `diagnostics/${encodeURIComponent(traceId)}`
    : "diagnostics";
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/** 进入诊断页并同步列表与可选详情。 */
async function loadTraceDiagnostics(traceId: string | null): Promise<void> {
  await loadTraceList();
  if (traceId) {
    await loadTraceDetail(traceId);
  } else {
    selectedTraceId.value = "";
    selectedTrace.value = null;
  }
}

/** 读取 full trace artifact，并返回纯文本或格式化 JSON。 */
async function loadTraceArtifactContent(
  traceId: string,
  artifact: TraceArtifact,
): Promise<string> {
  if (!connection.value) {
    throw new Error("产品服务尚未连接");
  }
  if (artifact.state !== "stored") {
    return (
      artifact.state === "omitted"
        ? "当前 trace 模式未保存正文，仅保留内容 hash。"
        : "artifact 写入失败，正文不可用。"
    );
  }
  const response = await fetchWithTimeout(
    `${connection.value.baseUrl}/api/traces/${encodeURIComponent(traceId)}/artifacts/${artifact.artifact_id}`,
    {
      headers: { "x-launch-token": connection.value.token },
      cache: "no-store",
    },
    API_TIMEOUT_MS.quick,
  );
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  const text = await response.text();
  return artifact.mime_type.includes("json")
    ? JSON.stringify(JSON.parse(text) as unknown, null, 2)
    : text;
}

/** 把部分成功任务显示为独立语义，避免被“已完成”掩盖。 */
function taskDisplayStatus(task: BackgroundTaskRecord): string {
  return task.status === "succeeded" && task.failed_sources > 0
    ? "部分入库"
    : taskStatusLabel(task.status);
}

/** 为部分成功任务使用独立状态色，其余沿用任务状态色。 */
function taskStatusClass(task: BackgroundTaskRecord): string {
  return task.status === "succeeded" && task.failed_sources > 0
    ? "task-partial"
    : `task-${task.status}`;
}

/** 把任务控制失败转换成用户可操作的中文说明。 */
function taskControlErrorMessage(code: string, detail: string): string {
  const labels: Record<string, string> = {
    task_not_found: "任务不存在，可能已经被清理。",
    task_not_resumable: "任务当前状态不允许恢复。",
    task_control_invalid: "任务状态已经变化，请刷新操作记录后重试。",
  };
  return labels[code]
    ? `${labels[code]}（错误码：${code}）`
    : `${detail}（错误码：${code}）`;
}

/** 把后台阶段转换成用户可读标签。 */
function taskStageLabel(stage: string): string {
  const labels: Record<string, string> = {
    queued: "等待处理",
    "S0-S4": "获取、解析、切块",
    S5: "生成 embedding 与索引",
    S6: "验证并发布 collection",
  };
  return labels[stage] ?? stage;
}

/** 从当前活动快照加载一篇文档的完整只读详情。 */
async function loadDocumentDetail(docId: string): Promise<void> {
  if (!connection.value) {
    return;
  }
  documentDetailLoading.value = true;
  documentDetailError.value = "";
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/collections/current/documents/${encodeURIComponent(docId)}`,
      {
        headers: { "x-launch-token": connection.value.token },
        cache: "no-store",
      },
      API_TIMEOUT_MS.quick,
    );
    const body = (await response.json()) as DocumentDetailResponse | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      throw new Error(
        taskControlErrorMessage(
          failure.error?.code ?? "request_failed",
          failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`,
        ),
      );
    }
    documentDetail.value = body as DocumentDetailResponse;
    documentDetailTab.value = "content";
  } catch (error) {
    documentDetail.value = null;
    documentDetailError.value = `读取文档详情失败：${String(error)}`;
  } finally {
    documentDetailLoading.value = false;
  }
}

/** 打开知识库中的文档详情，并可定位到本次回答引用的 block。 */
function openDocumentDetail(docId: string, blockId = ""): void {
  pendingDocumentBlockId = blockId;
  highlightedDocumentBlockId.value = blockId;
  const location = `knowledge/${encodeURIComponent(docId)}`;
  if (window.location.hash === `#${location}`) {
    void loadAndFocusDocumentDetail(docId);
    return;
  }
  activeSection.value = "knowledge";
  activeDocumentId.value = docId;
  documentDetailTab.value = "content";
  window.location.hash = location;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/** 加载文档详情，并在渲染完成后滚动到待定位的 block。 */
async function loadAndFocusDocumentDetail(docId: string): Promise<void> {
  await loadDocumentDetail(docId);
  if (!pendingDocumentBlockId || documentDetail.value?.doc_id !== docId) {
    return;
  }
  const blockId = pendingDocumentBlockId;
  pendingDocumentBlockId = "";
  await nextTick();
  const element = document.getElementById(
    `document-block-${encodeURIComponent(blockId)}`,
  );
  if (!element) {
    documentDetailError.value = "文档已打开，但没有找到本次回答引用的原文片段。";
    return;
  }
  highlightedDocumentBlockId.value = blockId;
  element.scrollIntoView({ behavior: "smooth", block: "center" });
}

/** 从详情页返回知识库文章清单。 */
function closeDocumentDetail(): void {
  pendingDocumentBlockId = "";
  highlightedDocumentBlockId.value = "";
  selectSection("knowledge");
}

/** 把当前文档来源地址或本地路径复制到剪贴板。 */
async function copyDocumentSource(): Promise<void> {
  if (!documentDetail.value) {
    return;
  }
  const value =
    documentDetail.value.source.final_url ?? documentDetail.value.source.source_uri;
  try {
    await navigator.clipboard.writeText(value);
  } catch (error) {
    documentDetailError.value = `复制来源失败：${String(error)}`;
  }
}

/** 滚动到详情页的指定章节。 */
function scrollToDocumentSection(index: number): void {
  document.getElementById(`document-section-${index}`)?.scrollIntoView({
    behavior: "smooth",
    block: "start",
  });
}

/** 读取当前活动 collection；没有时返回 null。 */
async function loadCurrentCollection(): Promise<void> {
  if (!connection.value) {
    return;
  }
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/collections/current`,
      {
        headers: { "x-launch-token": connection.value.token },
        cache: "no-store",
      },
      API_TIMEOUT_MS.quick,
    );
    if (response.ok) {
      currentCollection.value = (await response.json()) as CurrentCollectionResponse | null;
    }
  } catch {
    // Collection 状态是辅助信息；不要阻断设置或导入主流程。
  }
}

/** 追加多个本地 HTML 文件到后台任务队列。 */
/** 读取最近入库任务，保留逐来源结果供操作历史展开查看。 */
async function loadTaskHistory(): Promise<void> {
  if (!connection.value) {
    return;
  }
  historyBusy.value = true;
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/tasks?limit=20`,
      {
        headers: { "x-launch-token": connection.value.token },
        cache: "no-store",
      },
      API_TIMEOUT_MS.quick,
    );
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    taskHistory.value = (await response.json()) as BackgroundTaskRecord[];
    historyError.value = "";
  } catch (error) {
    historyError.value = `读取入库记录失败：${String(error)}`;
  } finally {
    historyBusy.value = false;
  }
}

/** 把后端 UTC 时间转换成当前系统时区的本地时间。 */
function taskTimeLabel(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

/** 根据任务进度和失败清单还原每个来源当前的最终或进行中结果。 */
function taskSourceResult(task: BackgroundTaskRecord, sourceIndex: number): string {
  const failure = task.failures.find((item) => item.source_index === sourceIndex);
  if (failure) {
    return `失败：${ingestErrorMessage(failure.code, failure.message)}`;
  }
  if (sourceIndex < task.next_source_index) {
    return "成功";
  }
  if (
    sourceIndex === task.next_source_index &&
    ["queued", "running", "paused", "interrupted"].includes(task.status)
  ) {
    return task.status === "paused" || task.status === "interrupted"
      ? "等待继续"
      : "处理中";
  }
  return "未处理";
}

/** 标记一个来源的结果是否失败，供历史详情使用统一视觉样式。 */
function taskSourceResultClass(task: BackgroundTaskRecord, sourceIndex: number): string {
  if (task.failures.some((item) => item.source_index === sourceIndex)) {
    return "source-failed";
  }
  return sourceIndex < task.next_source_index ? "source-succeeded" : "";
}

/** 通过原生文件对话框把多个 HTML 来源加入后台任务队列。 */
async function addTaskFiles(): Promise<void> {
  const selected = await open({
    multiple: true,
    directory: false,
    filters: [{ name: "HTML", extensions: ["html", "htm"] }],
  });
  const paths = typeof selected === "string" ? [selected] : selected ?? [];
  for (const path of paths) {
    const source: BackgroundSource = { source_kind: "file", source_value: path };
    if (
      !taskSources.value.some(
        (item) =>
          item.source_kind === source.source_kind &&
          item.source_value === source.source_value,
      )
    ) {
      taskSources.value.push(source);
    }
  }
  taskError.value = "";
}

/** 把当前 URL 输入追加到后台任务队列。 */
function addTaskUrl(): void {
  const value = taskUrl.value.trim();
  if (!/^https?:\/\//i.test(value)) {
    taskError.value = "网页来源必须是公开的 HTTP 或 HTTPS URL。";
    return;
  }
  if (
    !taskSources.value.some(
      (item) => item.source_kind === "url" && item.source_value === value,
    )
  ) {
    taskSources.value.push({ source_kind: "url", source_value: value });
  }
  taskUrl.value = "";
  taskError.value = "";
}

/** 从队列移除一个来源。 */
function removeTaskSource(index: number): void {
  taskSources.value.splice(index, 1);
}

/** 提交多来源后台入库任务。 */
async function startBackgroundTask(): Promise<void> {
  if (!connection.value || !canStartTask.value) {
    return;
  }
  taskBusy.value = true;
  taskError.value = "";
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/tasks/ingest`,
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-launch-token": connection.value.token,
        },
        body: JSON.stringify({ sources: taskSources.value }),
      },
      API_TIMEOUT_MS.quick,
    );
    const body = (await response.json()) as BackgroundTaskRecord | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      throw new Error(
        taskControlErrorMessage(
          failure.error?.code ?? "request_failed",
          failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`,
        ),
      );
    }
    adoptBackgroundTask(body as BackgroundTaskRecord);
    taskSources.value = [];
    taskUrl.value = "";
    void loadTaskHistory();
  } catch (error) {
    taskError.value = `启动后台任务失败：${String(error)}`;
  } finally {
    taskBusy.value = false;
  }
}

/** 当前任务若来自单篇预览，同步同一记录的状态，避免预览页保留旧进度。 */
function syncSingleIngestTask(): void {
  if (
    backgroundTask.value &&
    singleIngestTask.value?.task_id === backgroundTask.value.task_id
  ) {
    singleIngestTask.value = backgroundTask.value;
  }
}

/** 判断任务是否仍属于持久队列。 */
function isActiveQueueTask(task: BackgroundTaskRecord): boolean {
  return ["queued", "running", "paused", "interrupted"].includes(task.status);
}

/** 在活动任务列表中新增或替换同 ID 记录。 */
function upsertActiveTask(task: BackgroundTaskRecord): void {
  const index = activeTasks.value.findIndex((item) => item.task_id === task.task_id);
  if (!isActiveQueueTask(task)) {
    if (index >= 0) {
      activeTasks.value.splice(index, 1);
    }
    return;
  }
  if (index >= 0) {
    activeTasks.value.splice(index, 1, task);
  } else {
    activeTasks.value.push(task);
  }
}

/** 选中队列中的一个任务，展示其详细进度和控制按钮。 */
function selectActiveTask(task: BackgroundTaskRecord): void {
  adoptBackgroundTask(task);
}

/** 读取持久任务队列；首次加载时优先选择 running，其次 queued。 */
async function loadActiveTasks(selectFallback = true): Promise<void> {
  if (!connection.value) {
    return;
  }
  const response = await fetchWithTimeout(
    `${connection.value.baseUrl}/api/tasks/active`,
    {
      headers: { "x-launch-token": connection.value.token },
      cache: "no-store",
    },
    API_TIMEOUT_MS.quick,
  );
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  const tasks = (await response.json()) as BackgroundTaskRecord[];
  activeTasks.value = tasks;
  if (selectedTaskId.value) {
    const selected = tasks.find((task) => task.task_id === selectedTaskId.value);
    if (selected) {
      backgroundTask.value = selected;
      syncSingleIngestTask();
      return;
    }
  }
  if (!selectFallback || tasks.length === 0) {
    return;
  }
  const preferred =
    tasks.find((task) => task.status === "running") ??
    tasks.find((task) => task.status === "queued") ??
    tasks[0];
  adoptBackgroundTask(preferred);
}

/** 将任务设为当前观察对象，并只对仍可能变化的非终态继续轮询。 */
function adoptBackgroundTask(task: BackgroundTaskRecord): void {
  backgroundTask.value = task;
  selectedTaskId.value = task.task_id;
  upsertActiveTask(task);
  syncSingleIngestTask();
  if (
    ["queued", "running"].includes(task.status) ||
    activeTasks.value.some((item) => ["queued", "running"].includes(item.status))
  ) {
    beginTaskPolling();
    return;
  }
  if (taskTimer !== undefined) {
    window.clearInterval(taskTimer);
    taskTimer = undefined;
  }
}

/** 轮询当前任务状态，完成或失败后自动停止。 */
async function refreshBackgroundTask(): Promise<void> {
  if (!connection.value || !backgroundTask.value || taskRefreshInFlight) {
    return;
  }
  taskRefreshInFlight = true;
  try {
    const previousStatus = backgroundTask.value.status;
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/tasks/${backgroundTask.value.task_id}`,
      {
        headers: { "x-launch-token": connection.value.token },
        cache: "no-store",
      },
      API_TIMEOUT_MS.quick,
    );
    if (!response.ok) {
      return;
    }
    const nextTask = (await response.json()) as BackgroundTaskRecord;
    backgroundTask.value = nextTask;
    upsertActiveTask(nextTask);
    syncSingleIngestTask();
    try {
      await loadActiveTasks(false);
    } catch {
      // 队列摘要的瞬时读取失败不覆盖已经成功读取的单任务状态。
    }
    const becameTerminal =
      !["succeeded", "failed", "canceled"].includes(previousStatus) &&
      ["succeeded", "failed", "canceled"].includes(nextTask.status);
    if (becameTerminal) {
      await loadTaskHistory();
      if (nextTask.status === "succeeded") {
        await Promise.all([
          loadCurrentCollection(),
          ...(activeDocumentId.value
            ? [loadDocumentDetail(activeDocumentId.value)]
            : []),
        ]);
      }
    }
    const mustContinuePolling = activeTasks.value.some((task) =>
      ["queued", "running"].includes(task.status),
    );
    if (
      !mustContinuePolling &&
      ["succeeded", "failed", "canceled"].includes(nextTask.status)
    ) {
      if (taskTimer !== undefined) {
        window.clearInterval(taskTimer);
        taskTimer = undefined;
      }
    }
  } catch {
    // 下一轮健康检查和轮询会继续尝试，不把瞬时网络错误变成任务失败。
  } finally {
    taskRefreshInFlight = false;
  }
}

/** 开始轮询当前后台任务。 */
function beginTaskPolling(): void {
  if (taskTimer !== undefined) {
    window.clearInterval(taskTimer);
  }
  taskTimer = window.setInterval(() => {
    void refreshBackgroundTask();
  }, 800);
}

/** 暂停当前后台任务。 */
async function pauseBackgroundTask(): Promise<void> {
  if (!connection.value || !backgroundTask.value) {
    return;
  }
  await controlBackgroundTask("pause");
}

/** 恢复当前后台任务。 */
async function resumeBackgroundTask(): Promise<void> {
  if (!connection.value || !backgroundTask.value) {
    return;
  }
  await controlBackgroundTask("resume");
}

/** 取消当前后台任务。 */
async function cancelBackgroundTask(): Promise<void> {
  if (!connection.value || !backgroundTask.value) {
    return;
  }
  await controlBackgroundTask("cancel");
}

/** 调用任务控制端点并立即刷新状态。 */
async function controlBackgroundTask(action: "pause" | "resume" | "cancel"): Promise<void> {
  if (!connection.value || !backgroundTask.value) {
    return;
  }
  taskBusy.value = true;
  taskError.value = "";
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/tasks/${backgroundTask.value.task_id}/${action}`,
      {
        method: "POST",
        headers: { "x-launch-token": connection.value.token },
      },
      API_TIMEOUT_MS.quick,
    );
    const body = (await response.json()) as BackgroundTaskRecord | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      throw new Error(
        failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`,
      );
    }
    adoptBackgroundTask(body as BackgroundTaskRecord);
    await loadTaskHistory();
  } catch (error) {
    taskError.value = `任务控制失败：${String(error)}`;
  } finally {
    taskBusy.value = false;
  }
}

/** 从操作记录恢复或取消指定任务，并把恢复任务设为当前观察对象。 */
async function controlHistoryTask(
  task: BackgroundTaskRecord,
  action: "resume" | "cancel",
): Promise<void> {
  if (!connection.value || taskBusy.value) {
    return;
  }
  taskBusy.value = true;
  taskError.value = "";
  historyError.value = "";
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/tasks/${task.task_id}/${action}`,
      {
        method: "POST",
        headers: { "x-launch-token": connection.value.token },
      },
      API_TIMEOUT_MS.quick,
    );
    const body = (await response.json()) as BackgroundTaskRecord | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      throw new Error(
        taskControlErrorMessage(
          failure.error?.code ?? "request_failed",
          failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`,
        ),
      );
    }
    adoptBackgroundTask(body as BackgroundTaskRecord);
    await loadTaskHistory();
    if (action === "resume") {
      ingestMode.value = "batch";
      selectSection("ingest");
    }
  } catch (error) {
    const message = `任务控制失败：${String(error)}`;
    taskError.value = message;
    historyError.value = message;
  } finally {
    taskBusy.value = false;
  }
}

/** 判断历史任务是否允许恢复。 */
function taskCanResume(task: BackgroundTaskRecord): boolean {
  return ["paused", "interrupted"].includes(task.status);
}

/** 判断任务是否允许暂停；queued 任务只需取消，不需要暂停。 */
function taskCanPause(task: BackgroundTaskRecord): boolean {
  return task.status === "running";
}

/** 判断历史任务是否允许取消。 */
function taskCanCancel(task: BackgroundTaskRecord): boolean {
  return ["queued", "running", "paused", "interrupted"].includes(task.status);
}

/** 返回任务在 queued 队列中的真实位置；非排队任务返回 null。 */
function taskQueuePosition(task: BackgroundTaskRecord): number | null {
  if (task.status !== "queued") {
    return null;
  }
  const queuedTasks = activeTasks.value.filter((item) => item.status === "queued");
  const index = queuedTasks.findIndex((item) => item.task_id === task.task_id);
  return index >= 0 ? index + 1 : null;
}

/** 返回当前任务正在处理的来源名，避免长时间阶段只显示一个比例。 */
function currentTaskSourceLabel(task: BackgroundTaskRecord): string {
  if (task.status !== "running" || task.next_source_index >= task.sources.length) {
    return "";
  }
  return task.sources[task.next_source_index].source_value;
}

/** 计算任务从开始到现在的本地耗时，不预测无依据的剩余时间。 */
function taskElapsedLabel(task: BackgroundTaskRecord): string {
  if (!task.started_at) {
    return "";
  }
  const startedAt = new Date(task.started_at).getTime();
  const endedAt = task.finished_at ? new Date(task.finished_at).getTime() : Date.now();
  if (Number.isNaN(startedAt) || Number.isNaN(endedAt) || endedAt < startedAt) {
    return "";
  }
  const totalSeconds = Math.floor((endedAt - startedAt) / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

/** 从任务队列直接执行暂停、恢复或取消，并选中该任务显示详情。 */
async function controlQueueTask(
  task: BackgroundTaskRecord,
  action: "pause" | "resume" | "cancel",
): Promise<void> {
  if (!connection.value || taskBusy.value) {
    return;
  }
  taskBusy.value = true;
  taskError.value = "";
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/tasks/${task.task_id}/${action}`,
      {
        method: "POST",
        headers: { "x-launch-token": connection.value.token },
      },
      API_TIMEOUT_MS.quick,
    );
    const body = (await response.json()) as BackgroundTaskRecord | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      throw new Error(
        taskControlErrorMessage(
          failure.error?.code ?? "request_failed",
          failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`,
        ),
      );
    }
    adoptBackgroundTask(body as BackgroundTaskRecord);
    await loadTaskHistory();
  } catch (error) {
    taskError.value = `任务控制失败：${String(error)}`;
  } finally {
    taskBusy.value = false;
  }
}

/** 定时检查已认证 sidecar 是否仍然可用。 */
async function checkRuntime(): Promise<void> {
  if (!connection.value || disposed) {
    return;
  }

  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/runtime`,
      {
        headers: {
          "x-launch-token": connection.value.token,
        },
        cache: "no-store",
      },
      API_TIMEOUT_MS.quick,
    );
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    runtime.value = (await response.json()) as RuntimeInfo;
    if (managerStatus.value === "running") {
      message.value = "Tauri 已连接本地 Python sidecar。";
      if (!modelSettings.value && !settingsLoading.value) {
        void loadModelSettings(2);
        void loadRuntimeSettings(2);
      }
    }
  } catch (error) {
    runtime.value = null;
    if (managerStatus.value === "running") {
      message.value = `健康检查失败：${String(error)}`;
    }
  }
}

/** 读取 Tauri 管理的后端连接信息并完成首次健康检查。 */
async function connect(): Promise<void> {
  try {
    connection.value = await invoke<BackendConnection>("backend_connection");
    const status = await invoke<BackendStatus>("backend_status");
    applyStatus(status);
    await checkRuntime();
    await loadModelSettings();
    await loadRuntimeSettings();
    await loadCurrentCollection();
    try {
      await loadActiveTasks();
    } catch (error) {
      taskError.value = `读取任务队列失败：${String(error)}`;
    }
    await loadTaskHistory();
    if (activeDocumentId.value) {
      await loadDocumentDetail(activeDocumentId.value);
    }
    if (activeSection.value === "diagnostics") {
      await loadTraceDiagnostics(readTraceIdFromHash());
    }
  } catch (error) {
    managerStatus.value = "failed";
    message.value = `无法读取 Tauri 后端状态：${String(error)}`;
  }
}

/** 把 Tauri 推送的进程状态同步到界面。 */
function applyStatus(status: BackendStatus): void {
  managerStatus.value = status.status;
  restartCount.value = status.restartCount;
  message.value =
    status.message ??
    (status.status === "restarting"
      ? `sidecar 意外退出，正在第 ${status.restartCount} 次重启。`
      : status.status === "running"
        ? "本地后端已启动，正在进行健康检查。"
        : "正在连接本地后端…");
}

/** 通过 Tauri 原生对话框选择单个本地 HTML 文件。 */
async function chooseFile(): Promise<void> {
  const selected = await open({
    multiple: false,
    directory: false,
    filters: [{ name: "HTML", extensions: ["html", "htm"] }],
  });
  if (typeof selected === "string") {
    selectedFilePath.value = selected;
    ingestError.value = "";
  }
}

/** 调用后端执行 S0-S4 并展示结构预览。 */
async function ingest(): Promise<void> {
  if (!connection.value || !sourceValue.value || !canIngest.value) {
    return;
  }

  ingesting.value = true;
  ingestError.value = "";
  preview.value = null;
  singleIngestTask.value = null;
  saveError.value = "";
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/ingest/preview`,
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-launch-token": connection.value.token,
        },
        body: JSON.stringify({
          source_kind: sourceKind.value,
          source_value: sourceValue.value,
        }),
      },
      API_TIMEOUT_MS.sourcePreview,
    );
    const body = (await response.json()) as PreviewResponse | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      const code = failure.error?.code ?? "request_failed";
      const detail = failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`;
      throw new Error(ingestErrorMessage(code, detail));
    }
    preview.value = body as PreviewResponse;
  } catch (error) {
    ingestError.value = String(error);
  } finally {
    ingesting.value = false;
  }
}

/** 把当前预览 job 提交为完整的 S2-S6 单篇入库任务。 */
async function saveDocument(): Promise<void> {
  if (!connection.value || !preview.value || !canSave.value) {
    return;
  }

  saving.value = true;
  saveError.value = "";
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/tasks/ingest/preview`,
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-launch-token": connection.value.token,
        },
        body: JSON.stringify({ job_id: preview.value.job_id }),
      },
      API_TIMEOUT_MS.quick,
    );
    const body = (await response.json()) as BackgroundTaskRecord | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      const code = failure.error?.code ?? "request_failed";
      const detail = failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`;
      throw new Error(ingestErrorMessage(code, detail));
    }
    singleIngestTask.value = body as BackgroundTaskRecord;
    adoptBackgroundTask(body as BackgroundTaskRecord);
    await loadTaskHistory();
    ingestMode.value = "batch";
  } catch (error) {
    saveError.value = String(error);
  } finally {
    saving.value = false;
  }
}

/** 把流式问答 API 错误转换为普通用户可操作的提示。 */
function agentChatErrorMessage(code: string, detail: string): string {
  const labels: Record<string, string> = {
    chat_api_key_missing: "请先在设置中配置对话 API Key。",
    embedding_api_key_missing: "请先在设置中配置 embedding API Key。",
    collection_unavailable: "当前没有可用的知识库快照，请先完成入库。",
    agent_run_not_found: "这次回答的运行现场已过期，请重新提问。",
    agent_event_gap: "事件恢复位置已失效，请重新提问。",
    agent_event_cursor_invalid: "事件恢复位置无效，请重新提问。",
    chat_authentication_failed: "对话模型鉴权失败，请检查 API Key。",
    chat_rate_limited: "对话模型请求被限流，请稍后重试。",
    chat_upstream_unavailable: "暂时无法连接对话模型，请检查网络后重试。",
  };
  return labels[code]
    ? `${labels[code]}（错误码：${code}）`
    : `${detail}（错误码：${code}）`;
}

/** 重置上一次问答的展示状态。 */
function resetChatState(): void {
  closeChatCitationPopover();
  pendingChatEvents.length = 0;
  chatRunId.value = "";
  chatTraceId.value = "";
  chatTraceAvailable.value = false;
  chatSnapshotId.value = "";
  chatStatus.value = "idle";
  chatActivity.value = "等待提问";
  chatAnswer.value = "";
  chatSources.value = [];
  chatCitations.value = [];
  chatInputTokens.value = 0;
  chatOutputTokens.value = 0;
  chatInputSource.value = "estimated";
  chatOutputSource.value = "estimated";
  chatError.value = "";
  chatWarning.value = "";
  chatStopping.value = false;
  chatLastEventId.value = 0;
  chatSourcesExpanded.value = false;
  expandedChatSourceKey.value = "";
  chatSourceNotice.value = "";
}

/** 按来源键合并实时来源，避免重连回放产生重复卡片。 */
function mergeChatSources(sources: AgentChatSource[]): void {
  const merged = new Map(
    chatSources.value.map((source) => [source.source_key, source]),
  );
  sources.forEach((source) => merged.set(source.source_key, source));
  chatSources.value = [...merged.values()];
}

/** 等待浏览器完成一个可见动画帧，避免同批事件被合成一次渲染。 */
function waitForAnimationFrame(): Promise<void> {
  return new Promise((resolve) => window.requestAnimationFrame(() => resolve()));
}

/** 把网络接收与界面消费解耦，保证真实分块按可见节奏进入页面。 */
function enqueueAgentStreamEvent(envelope: AgentStreamEnvelope): void {
  chatLastEventId.value = Math.max(chatLastEventId.value, envelope.sequence);
  pendingChatEvents.push(envelope);
  chatEventPump ??= pumpAgentStreamEvents().finally(() => {
    chatEventPump = null;
  });
}

/** 每帧最多消费约 12 个答案字符，同时保持事件原始顺序。 */
async function pumpAgentStreamEvents(): Promise<void> {
  while (pendingChatEvents.length > 0) {
    let answerCharacters = 0;
    let processed = 0;
    while (pendingChatEvents.length > 0 && processed < 40) {
      const next = pendingChatEvents[0];
      if (next.event_type === "answer_delta") {
        const deltaLength = next.payload.delta?.length ?? 0;
        if (answerCharacters > 0 && answerCharacters + deltaLength > 12) {
          break;
        }
        answerCharacters += deltaLength;
      }
      pendingChatEvents.shift();
      applyAgentStreamEvent(next);
      processed += 1;
      if (answerCharacters >= 12) {
        break;
      }
    }
    await waitForAnimationFrame();
  }
}

/** 等待当前事件队列全部渲染完成，再做终态和断线判断。 */
async function drainAgentStreamEvents(): Promise<void> {
  while (chatEventPump !== null) {
    await chatEventPump;
  }
}

/** 消费一个 SSE 事件并更新固定状态区、正文、token 和来源。 */
function applyAgentStreamEvent(envelope: AgentStreamEnvelope): void {
  const payload = envelope.payload;
  switch (envelope.event_type) {
    case "run_started":
      chatSnapshotId.value = payload.snapshot_id ?? "";
      chatStatus.value = "running";
      chatActivity.value = "正在分析问题";
      break;
    case "snapshot_bound":
      chatSnapshotId.value = payload.snapshot_id ?? chatSnapshotId.value;
      break;
    case "activity_changed":
      chatActivity.value = payload.label ?? chatActivity.value;
      break;
    case "tool_started":
      chatActivity.value = payload.label ?? "正在检索知识库";
      break;
    case "tool_finished":
      chatActivity.value =
        payload.source_count && payload.source_count > 0
          ? `检索完成，找到 ${payload.source_count} 个相关片段`
          : "检索完成，正在判断证据";
      break;
    case "sources_available":
      if (payload.sources) {
        mergeChatSources(payload.sources);
      }
      break;
    case "citation_annotation_started":
      chatActivity.value = "正在核对引用依据";
      break;
    case "citations_available":
      chatCitations.value = payload.annotations ?? [];
      if (payload.status === "pending") {
        const citationWarning =
          payload.message || "引用标注未完成，正文已保留。";
        chatWarning.value = chatWarning.value
          ? `${chatWarning.value}；${citationWarning}`
          : citationWarning;
        chatActivity.value = "回答完成，引用标注未完成";
      } else if (chatCitations.value.length > 0) {
        chatActivity.value =
          chatStatus.value === "partial"
            ? `回答已生成，但内容不完整，已标注 ${chatCitations.value.length} 处引用`
            : `回答完成，已标注 ${chatCitations.value.length} 处引用`;
      } else {
        chatActivity.value =
          chatStatus.value === "partial" ? "回答已生成，但内容不完整" : "回答完成";
      }
      break;
    case "answer_delta":
      if (payload.delta) {
        chatAnswer.value += payload.delta;
      }
      break;
    case "usage_update":
      chatInputTokens.value = payload.input_tokens ?? chatInputTokens.value;
      chatOutputTokens.value = payload.output_tokens ?? chatOutputTokens.value;
      chatInputSource.value = payload.input_source ?? "estimated";
      chatOutputSource.value = payload.output_source ?? "estimated";
      break;
    case "budget_warning":
      chatWarning.value =
        payload.reason === "max_tool_calls"
          ? "已达到最大工具调用数，回答可能不完整。"
          : "已达到运行预算，回答可能不完整。";
      break;
    case "answer_completed":
      if (!chatAnswer.value && payload.answer) {
        chatAnswer.value = payload.answer;
      }
      if (payload.sources) {
        mergeChatSources(payload.sources);
      }
      chatInputTokens.value = payload.input_tokens ?? chatInputTokens.value;
      chatOutputTokens.value = payload.output_tokens ?? chatOutputTokens.value;
      chatInputSource.value = payload.input_source ?? chatInputSource.value;
      chatOutputSource.value = payload.output_source ?? chatOutputSource.value;
      if (payload.limitations?.length) {
        chatWarning.value = payload.limitations
          .map((limitation) => limitation.message)
          .join("；");
      }
      chatStatus.value =
        payload.answer_completeness === "partial" ? "partial" : "succeeded";
      chatActivity.value =
        chatStatus.value === "partial" ? "回答已生成，但内容不完整" : "回答完成";
      chatStopping.value = false;
      break;
    case "run_failed":
      chatStatus.value = "failed";
      chatError.value =
        payload.message ??
        `运行失败：${payload.termination_reason ?? payload.reason ?? "未知原因"}`;
      chatActivity.value = "运行失败";
      chatStopping.value = false;
      break;
    case "run_canceled":
      chatStatus.value = "canceled";
      chatActivity.value = chatAnswer.value
        ? "已停止，保留已生成内容"
        : "已停止，尚未生成回答";
      chatStopping.value = false;
      break;
  }
}

/** 解析一个 SSE 文本帧；格式错误时保留明确错误而不是静默忽略。 */
function parseAgentSseFrame(frame: string): AgentStreamEnvelope | null {
  const data = frame
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data) {
    return null;
  }
  try {
    return JSON.parse(data) as AgentStreamEnvelope;
  } catch {
    throw new Error("收到无法解析的流式事件。");
  }
}

/** 连接同一个 run 的 SSE，并在断线后按最后事件序号恢复。 */
async function consumeAgentEventStream(
  runId: string,
  reconnectAttempt = 0,
): Promise<void> {
  if (!connection.value || disposed) {
    return;
  }
  const controller = new AbortController();
  chatStreamController = controller;
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/agent/runs/${runId}/events`,
      {
        headers: {
          accept: "text/event-stream",
          "x-launch-token": connection.value.token,
          "last-event-id": String(chatLastEventId.value),
        },
        cache: "no-store",
        signal: controller.signal,
      },
      API_TIMEOUT_MS.quick,
    );
    if (!response.ok) {
      const failure = (await response.json()) as ApiErrorBody;
      const code = failure.error?.code ?? "request_failed";
      const detail = failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`;
      throw new Error(agentChatErrorMessage(code, detail));
    }
    if (!response.body) {
      throw new Error("问答事件流没有可读取的响应正文。");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const envelope = parseAgentSseFrame(frame);
        if (envelope) {
          enqueueAgentStreamEvent(envelope);
        }
        boundary = buffer.indexOf("\n\n");
      }
    }
    await drainAgentStreamEvents();
    if (chatIsActive.value && !disposed && !chatStreamController?.signal.aborted) {
      throw new Error("问答事件流在终态前结束。");
    }
  } catch (error) {
    await drainAgentStreamEvents();
    if (disposed || controller.signal.aborted) {
      return;
    }
    if (chatIsActive.value && reconnectAttempt < 3) {
      chatStatus.value = "reconnecting";
      chatActivity.value = "连接中断，正在恢复本次回答";
      await new Promise((resolve) =>
        window.setTimeout(resolve, 500 * (reconnectAttempt + 1)),
      );
      await consumeAgentEventStream(runId, reconnectAttempt + 1);
      return;
    }
    if (chatIsActive.value) {
      chatStatus.value = "interrupted";
      chatActivity.value = "连接已中断";
    }
    chatError.value =
      error instanceof TypeError
        ? "无法连接本地问答服务，请检查应用状态后重试。"
        : String(error);
    chatStopping.value = false;
  } finally {
    if (chatStreamController === controller) {
      chatStreamController = undefined;
    }
  }
}

/** 启动一次流式问答并立即进入事件消费。 */
async function askAgentQuestion(): Promise<void> {
  if (!connection.value || !canAskQuestion.value) {
    return;
  }
  resetChatState();
  chatTraceAvailable.value = runtimeSettingsForm.value.trace_mode !== "off";
  chatStatus.value = "starting";
  chatActivity.value = "正在启动回答";
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/agent/runs`,
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-launch-token": connection.value.token,
        },
        body: JSON.stringify({ question: chatQuestion.value.trim() }),
      },
      API_TIMEOUT_MS.quick,
    );
    const body = (await response.json()) as AgentRunStartResponse | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      const code = failure.error?.code ?? "request_failed";
      const detail = failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`;
      throw new Error(agentChatErrorMessage(code, detail));
    }
    const started = body as AgentRunStartResponse;
    chatRunId.value = started.run_id;
    chatTraceId.value = started.trace_id;
    chatStatus.value = "running";
    chatActivity.value = "正在准备检索";
    await consumeAgentEventStream(started.run_id);
  } catch (error) {
    chatStatus.value = "failed";
    chatActivity.value = "无法开始回答";
    chatError.value =
      error instanceof TypeError
        ? "无法连接本地问答服务，请检查应用状态后重试。"
        : String(error);
    chatStopping.value = false;
  }
}

/** 请求后端停止当前问答；事件流仍会等待明确的取消终态。 */
async function stopAgentQuestion(): Promise<void> {
  if (!connection.value || !chatRunId.value || !chatIsActive.value) {
    return;
  }
  chatStopping.value = true;
  chatActivity.value = "正在停止回答";
  try {
    const response = await fetchWithTimeout(
      `${connection.value.baseUrl}/api/agent/runs/${chatRunId.value}/cancel`,
      {
        method: "POST",
        headers: { "x-launch-token": connection.value.token },
      },
      API_TIMEOUT_MS.quick,
    );
    if (!response.ok) {
      const failure = (await response.json()) as ApiErrorBody;
      const code = failure.error?.code ?? "request_failed";
      throw new Error(
        agentChatErrorMessage(
          code,
          failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`,
        ),
      );
    }
  } catch (error) {
    chatStopping.value = false;
    chatError.value = `停止请求失败：${String(error)}`;
  }
}

/** 用户可在三次自动续传失败后，手动恢复同一个 run 的事件流。 */
async function resumeAgentQuestionStream(): Promise<void> {
  if (!chatRunId.value || chatStatus.value !== "interrupted") {
    return;
  }
  chatError.value = "";
  chatStatus.value = "reconnecting";
  chatActivity.value = "正在恢复同一问答";
  await consumeAgentEventStream(chatRunId.value);
}

/** 从来源卡片打开仍与本次回答快照一致的文档详情。 */
function openChatSourceDocument(source: AgentChatSource): void {
  chatSourceNotice.value = "";
  if (!canOpenChatSourceDocument.value) {
    chatSourceNotice.value = "本次回答使用的知识库快照已切换，无法打开原文档版本。";
    return;
  }
  openDocumentDetail(source.doc_id, source.block_id);
}

onMounted(async () => {
  window.addEventListener("hashchange", syncSectionFromHash);
  document.addEventListener("pointerdown", handleDocumentPointerDown);
  document.addEventListener("keydown", handleChatCitationDismiss);
  window.addEventListener("scroll", handleChatCitationDismiss, true);
  window.addEventListener("resize", closeChatCitationPopover);
  unlisten = await listen<BackendStatus>("backend-status", (event) => {
    applyStatus(event.payload);
    if (event.payload.status === "running") {
      void checkRuntime();
    }
  });

  await connect();
  healthTimer = window.setInterval(() => {
    void checkRuntime();
  }, 1500);
});

onBeforeUnmount(() => {
  disposed = true;
  chatStreamController?.abort();
  window.removeEventListener("hashchange", syncSectionFromHash);
  document.removeEventListener("pointerdown", handleDocumentPointerDown);
  document.removeEventListener("keydown", handleChatCitationDismiss);
  window.removeEventListener("scroll", handleChatCitationDismiss, true);
  window.removeEventListener("resize", closeChatCitationPopover);
  unlisten?.();
  if (healthTimer !== undefined) {
    window.clearInterval(healthTimer);
  }
  if (taskTimer !== undefined) {
    window.clearInterval(taskTimer);
  }
});
</script>

<template>
  <main class="shell">
    <header class="hero">
      <div>
        <p class="eyebrow">AI STUDY HELPER V2</p>
        <h1>从文档出发，把问题学明白。</h1>
      </div>
      <div class="backend-chip">
        <span class="status-dot" :class="stateClass" aria-hidden="true"></span>
        <div>
          <strong>{{ stateLabel }}</strong>
          <small>{{ runtime ? `PID ${runtime.pid}` : message }}</small>
        </div>
      </div>
    </header>

    <nav
      class="main-nav"
      :class="{ 'with-eval': evalConsoleEnabled }"
      aria-label="主导航"
    >
      <button
        type="button"
        :class="{ active: activeSection === 'chat' }"
        :aria-current="activeSection === 'chat' ? 'page' : undefined"
        @click="selectSection('chat')"
      >
        <span class="nav-index">01</span>
        <span>
          <strong>问答</strong>
          <small>{{ chatIsActive ? chatStatusLabel : "向知识库提问" }}</small>
        </span>
      </button>
      <button
        type="button"
        :class="{ active: activeSection === 'knowledge' }"
        :aria-current="activeSection === 'knowledge' ? 'page' : undefined"
        @click="selectSection('knowledge')"
      >
        <span class="nav-index">02</span>
        <span>
          <strong>知识库</strong>
          <small>{{ currentCollection?.manifest.totals.documents ?? 0 }} 篇文章</small>
        </span>
      </button>
      <button
        type="button"
        :class="{ active: activeSection === 'ingest' }"
        :aria-current="activeSection === 'ingest' ? 'page' : undefined"
        @click="selectSection('ingest')"
      >
        <span class="nav-index">03</span>
        <span>
          <strong>入库</strong>
          <small>{{ taskQueueSummary }}</small>
        </span>
      </button>
      <button
        type="button"
        :class="{ active: activeSection === 'history' }"
        :aria-current="activeSection === 'history' ? 'page' : undefined"
        @click="selectSection('history')"
      >
        <span class="nav-index">04</span>
        <span>
          <strong>操作记录</strong>
          <small>{{ taskHistory.length }} 条记录</small>
        </span>
      </button>
      <button
        type="button"
        :class="{ active: activeSection === 'settings' }"
        :aria-current="activeSection === 'settings' ? 'page' : undefined"
        @click="selectSection('settings')"
      >
        <span class="nav-index">05</span>
        <span>
          <strong>设置</strong>
          <small>
            {{
              modelSettings?.embedding_configured && modelSettings?.chat_configured
                ? "模型与问答已配置"
                : "需要配置模型 Key"
            }}
          </small>
        </span>
      </button>
      <button
        v-if="evalConsoleEnabled"
        type="button"
        :class="{ active: activeSection === 'evaluation' }"
        :aria-current="activeSection === 'evaluation' ? 'page' : undefined"
        @click="selectSection('evaluation')"
      >
        <span class="nav-index">06</span>
        <span>
          <strong>评测</strong>
          <small>开发构建专用</small>
        </span>
      </button>
    </nav>

    <component
      :is="EvaluationConsole"
      v-if="evalConsoleEnabled && activeSection === 'evaluation'"
    />

    <section v-if="activeSection === 'chat'" class="chat-layout">
      <section class="chat-query-card">
        <div class="section-heading">
          <div>
            <p class="eyebrow">ASK YOUR LIBRARY</p>
            <h2>向知识库提问</h2>
          </div>
          <span class="settings-pill" :class="{ configured: chatRunId }">
            {{ chatStatusLabel }}
          </span>
        </div>
        <p class="settings-description">
          回答优先依据当前知识库；文档支持的结论可追溯，模型内置知识或推断不会伪装成文档证据。
        </p>
        <div
          v-if="!modelSettings?.chat_configured || !modelSettings?.embedding_configured"
          class="chat-prerequisite"
        >
          请先在设置中完成 embedding 与对话模型配置。
          <button type="button" class="inline-link" @click="selectSection('settings')">
            去设置
          </button>
        </div>
        <div v-else-if="!currentCollection" class="chat-prerequisite">
          当前知识库为空，请先入库文档。
          <button type="button" class="inline-link" @click="selectSection('ingest')">
            去入库
          </button>
        </div>

        <label class="chat-question-field">
          <span>你的问题</span>
          <textarea
            v-model="chatQuestion"
            rows="4"
            placeholder="例如：OpenAI 和 Anthropic 对人在回路中的角色有什么不同？"
            :disabled="chatIsActive"
            @keydown.enter.exact.prevent="askAgentQuestion"
          />
        </label>
        <div class="chat-query-actions">
          <button
            type="button"
            class="primary-button"
            :disabled="!canAskQuestion"
            @click="askAgentQuestion"
          >
            {{ chatIsActive ? "正在回答…" : "开始提问" }}
          </button>
          <button
            v-if="chatIsActive"
            type="button"
            class="secondary-button"
            :disabled="chatStopping"
            @click="stopAgentQuestion"
          >
            {{ chatStopping ? "正在停止…" : "停止回答" }}
          </button>
          <span class="chat-enter-hint">Enter 提交，Shift + Enter 换行</span>
        </div>
      </section>

      <section
        v-if="chatRunId || chatError || chatAnswer"
        class="chat-response-card"
        aria-live="polite"
      >
        <div class="chat-run-status">
          <div class="chat-activity">
            <span
              class="chat-activity-dot"
              :class="{ active: chatIsActive, failed: chatStatus === 'failed' }"
            />
            <div>
              <strong>{{ chatActivity }}</strong>
              <button
                v-if="chatTraceId && chatTraceAvailable"
                type="button"
                class="chat-trace-link"
                @click="openTraceDiagnostics(chatTraceId)"
              >
                查看运行 {{ chatTraceId.slice(-8) }}
              </button>
            </div>
          </div>
          <dl class="chat-token-meter">
            <div>
              <dt>输入 token</dt>
              <dd>
                {{ chatInputTokens.toLocaleString() }}
                <small>{{ chatInputSource === "provider" ? "实际" : "估算" }}</small>
              </dd>
            </div>
            <div>
              <dt>输出 token</dt>
              <dd>
                {{ chatOutputTokens.toLocaleString() }}
                <small>{{ chatOutputSource === "provider" ? "实际" : "估算" }}</small>
              </dd>
            </div>
          </dl>
        </div>

        <p v-if="chatWarning" class="chat-warning">{{ chatWarning }}</p>
        <p v-if="chatError" class="error-box">{{ chatError }}</p>
        <button
          v-if="chatStatus === 'interrupted'"
          type="button"
          class="secondary-button chat-resume-button"
          @click="resumeAgentQuestionStream"
        >
          重新连接本次回答
        </button>

        <article v-if="chatAnswer" class="chat-answer">
          <p class="eyebrow">ANSWER</p>
          <div
            class="chat-answer-text"
            @click="handleChatAnswerClick"
            v-html="chatAnswerHtml"
          />
        </article>
        <div v-else-if="chatIsActive" class="chat-answer-placeholder">
          正在检索和组织答案，正文会在证据足够后逐步显示。
        </div>

        <section
          v-if="chatSources.length || chatSourceNotice"
          class="chat-sources"
        >
          <div class="section-heading">
            <div>
              <p class="eyebrow">SOURCES</p>
              <h3>本次回答引用的资料</h3>
            </div>
            <button
              type="button"
              class="chat-sources-toggle"
              :aria-expanded="chatSourcesExpanded"
              @click="toggleChatSources"
            >
              {{
                chatSourcesExpanded
                  ? "收起来源"
                  : `查看 ${chatSources.length} 条来源`
              }}
            </button>
          </div>
          <template v-if="chatSourcesExpanded">
            <article
              v-for="(source, index) in chatSources"
              :id="`chat-source-${encodeURIComponent(source.source_key)}`"
              :key="source.source_key"
              class="chat-source-card"
            >
              <button
                type="button"
                class="chat-source-heading"
                :aria-expanded="expandedChatSourceKey === source.source_key"
                @click="toggleChatSourceCard(source.source_key)"
              >
                <span class="chat-source-index">{{ index + 1 }}</span>
                <div>
                  <strong>{{ source.doc_title }}</strong>
                  <small>
                    {{ source.source_label || "本地文档" }}
                    <template v-if="source.section_path.length">
                      · {{ source.section_path.join(" › ") }}
                    </template>
                  </small>
                </div>
                <span class="chat-source-chevron">
                  {{ expandedChatSourceKey === source.source_key ? "收起" : "展开" }}
                </span>
              </button>
              <template v-if="expandedChatSourceKey === source.source_key">
                <p>{{ source.excerpt || "该来源暂无正文摘录。" }}</p>
                <button
                  type="button"
                  class="inline-link"
                  @click="openChatSourceDocument(source)"
                >
                  查看文档详情
                </button>
              </template>
            </article>
          </template>
          <p v-if="chatSourceNotice" class="chat-source-notice">
            {{ chatSourceNotice }}
          </p>
        </section>
      </section>
      <Teleport to="body">
        <aside
          v-if="chatCitationPopover"
          class="chat-citation-popover"
          :class="chatCitationPopover.placement"
          :style="{
            left: `${chatCitationPopover.left}px`,
            top: `${chatCitationPopover.top}px`,
          }"
          role="dialog"
          aria-modal="false"
          aria-labelledby="chat-citation-popover-title"
          data-chat-citation-popover
        >
          <div class="chat-citation-popover-head">
            <span class="chat-source-index">
              {{
                chatSourceNumbers.get(
                  normalizeChatSourceKey(
                    chatCitationPopover.source.source_key,
                  ),
                )
              }}
            </span>
            <div>
              <strong id="chat-citation-popover-title">
                {{ chatCitationPopover.source.doc_title }}
              </strong>
              <small>
                {{ chatCitationPopover.source.source_label || "本地文档" }}
                <template v-if="chatCitationPopover.source.section_path.length">
                  ·
                  {{
                    chatCitationPopover.source.section_path.join(" › ")
                  }}
                </template>
              </small>
            </div>
            <button
              type="button"
              class="chat-citation-popover-close"
              aria-label="关闭引用内容"
              @click="closeChatCitationPopover"
            >
              ×
            </button>
          </div>
          <p>
            {{
              chatCitationPopover.source.excerpt ||
              "该来源暂无正文摘录。"
            }}
          </p>
          <button
            v-if="canOpenChatSourceDocument"
            type="button"
            class="inline-link"
            @click="
              openChatCitationSourceDocument(chatCitationPopover.source)
            "
          >
            查看文档详情
          </button>
        </aside>
      </Teleport>
    </section>

    <section v-if="activeSection === 'diagnostics'" class="diagnostics-page">
      <div class="section-heading diagnostics-page-heading">
        <div>
          <p class="eyebrow">LOCAL TRACE VIEWER</p>
          <h2>运行诊断</h2>
          <p class="settings-description">
            直接读取本机 trace 文件，不依赖导入、外部服务或 OTLP。
          </p>
        </div>
        <div class="diagnostics-heading-actions">
          <button
            type="button"
            class="secondary-button"
            :disabled="diagnosticsLoading"
            @click="loadTraceDiagnostics(selectedTraceId || null)"
          >
            {{ diagnosticsLoading ? "刷新中…" : "刷新" }}
          </button>
          <button
            type="button"
            class="secondary-button"
            @click="selectSection('settings')"
          >
            返回设置
          </button>
        </div>
      </div>
      <p v-if="diagnosticsError" class="error-box">{{ diagnosticsError }}</p>

      <div class="diagnostics-workspace">
        <aside class="trace-list-panel">
          <div class="panel-heading">
            <div>
              <p class="eyebrow">RECENT RUNS</p>
              <h3>最近运行</h3>
            </div>
            <span>{{ diagnosticsTraces.length }} 条</span>
          </div>
          <div v-if="!diagnosticsLoading && diagnosticsTraces.length === 0" class="empty-state">
            还没有可查看的 trace。
          </div>
          <button
            v-for="trace in diagnosticsTraces"
            :key="trace.trace_id"
            type="button"
            class="trace-list-item"
            :class="{ active: selectedTraceId === trace.trace_id }"
            @click="openTraceDiagnostics(trace.trace_id)"
          >
            <span class="trace-list-topline">
              <strong>{{ trace.question_preview || `运行 ${trace.trace_id.slice(-8)}` }}</strong>
              <span class="task-status-pill" :class="`task-${trace.status}`">
                {{ traceStatusLabel(trace.status) }}
              </span>
            </span>
            <small>
              {{ taskTimeLabel(trace.started_at) }} ·
              {{ traceModeLabel(trace.mode) }} ·
              {{ trace.duration_ms }} ms ·
              {{ trace.input_tokens + trace.output_tokens }} token
            </small>
            <span class="trace-list-health" :class="`trace-health-${trace.trace_status}`">
              轨迹{{ traceStatusLabel(trace.trace_status) }} ·
              {{ replayScopeLabel(trace.replay_scope) }}
            </span>
          </button>
        </aside>

        <TraceDetailViewer
          v-if="selectedTrace"
          :trace="selectedTrace"
          :load-artifact="loadTraceArtifactContent"
        />

        <div v-else class="trace-detail-placeholder">
          从左侧选择一次运行，查看轮次、模型、工具、预算和来源现场。
        </div>
      </div>
    </section>

    <section v-if="activeSection === 'settings'" class="settings-card">
      <div class="section-heading">
        <div>
          <p class="eyebrow">MODEL SETTINGS</p>
          <h2>连接 embedding 模型</h2>
        </div>
        <span
          class="settings-pill"
          :class="{
            configured: modelSettings?.embedding_configured,
            loading: settingsLoading,
          }"
        >
          {{
            settingsLoading
              ? "读取中"
              : modelSettings?.embedding_configured
                ? "已配置"
                : "未配置"
          }}
        </span>
      </div>
      <p class="settings-description">
        Key 会保存到 Windows Credential Manager；页面和日志不会保存或显示 Key 明文。
      </p>
      <div class="settings-form">
        <input
          v-model="embeddingKey"
          type="password"
          autocomplete="off"
          placeholder="输入新的 embedding API Key"
          @keyup.enter="saveEmbeddingKey"
        />
        <button
          type="button"
          class="primary-button"
          :disabled="embeddingSettingsBusy || !embeddingKey.trim()"
          @click="saveEmbeddingKey"
        >
          保存 Key
        </button>
        <button
          type="button"
          class="secondary-button settings-test-button"
          :disabled="embeddingSettingsBusy || !modelSettings?.embedding_configured"
          @click="testEmbeddingKey"
        >
          {{ embeddingSettingsBusy ? "检查中…" : "测试连接" }}
        </button>
        <button
          v-if="modelSettings?.embedding_configured"
          type="button"
          class="danger-button"
          :disabled="embeddingSettingsBusy"
          @click="deleteEmbeddingKey"
        >
          删除 Key
        </button>
      </div>
      <div v-if="modelSettings" class="settings-meta">
        <span>模型：{{ modelSettings.embedding_model_id }}</span>
        <span>维度：{{ modelSettings.embedding_dimension }}</span>
        <span v-if="modelSettings.embedding_last_test">
          最近测试：{{ modelSettings.embedding_last_test.status === "success" ? "成功" : "失败" }}
        </span>
      </div>
      <p v-if="settingsMessage" class="settings-message">{{ settingsMessage }}</p>
      <p v-if="settingsError" class="error-box">{{ settingsError }}</p>
    </section>

    <section v-if="activeSection === 'settings'" class="settings-card">
      <div class="section-heading">
        <div>
          <p class="eyebrow">CHAT MODEL</p>
          <h2>连接问答模型</h2>
        </div>
        <span
          class="settings-pill"
          :class="{
            configured: modelSettings?.chat_configured,
            loading: settingsLoading,
          }"
        >
          {{
            settingsLoading
              ? "读取中"
              : modelSettings?.chat_configured
                ? "已配置"
                : "未配置"
          }}
        </span>
      </div>
      <p class="settings-description">
        对话 Key 与 embedding Key 独立保存。它只发送问答上下文，不用于文档向量化。
      </p>
      <div class="settings-form">
        <input
          v-model="chatKey"
          type="password"
          autocomplete="off"
          placeholder="输入新的 DeepSeek API Key"
          @keyup.enter="saveChatKey"
        />
        <button
          type="button"
          class="primary-button"
          :disabled="chatSettingsBusy || !chatKey.trim()"
          @click="saveChatKey"
        >
          保存 Key
        </button>
        <button
          type="button"
          class="secondary-button settings-test-button"
          :disabled="chatSettingsBusy || !modelSettings?.chat_configured"
          @click="testChatKey"
        >
          {{ chatSettingsBusy ? "检查中…" : "测试连接" }}
        </button>
        <button
          v-if="modelSettings?.chat_configured"
          type="button"
          class="danger-button"
          :disabled="chatSettingsBusy"
          @click="deleteChatKey"
        >
          删除 Key
        </button>
      </div>
      <div v-if="modelSettings" class="settings-meta">
        <span>模型：{{ modelSettings.chat_model_id }}</span>
        <span>调用方式：OpenAI-compatible API</span>
      </div>
      <p v-if="chatSettingsMessage" class="settings-message">{{ chatSettingsMessage }}</p>
      <p v-if="chatSettingsError" class="error-box">{{ chatSettingsError }}</p>
    </section>

    <section
      v-if="activeSection === 'settings'"
      class="settings-card runtime-settings-card"
      @input="clearRuntimeSettingsFeedback"
    >
      <div class="section-heading">
        <div>
          <p class="eyebrow">ANSWER & RETRIEVAL</p>
          <h2>问答与检索参数</h2>
        </div>
        <span class="settings-pill configured">下一次问答生效</span>
      </div>
      <p class="settings-description">
        默认值适合大多数知识库。所有字段必须填写整数；上下文预算与最大输出合计不能超过
        1,000,000。
      </p>

      <div class="settings-grid settings-grid-primary">
        <label class="settings-field">
          <span>每次检索返回片段数</span>
          <input
            id="runtime-top-k"
            v-model.number="runtimeSettingsForm.top_k"
            type="number"
            min="3"
            max="10"
            step="1"
            :aria-invalid="Boolean(runtimeSettingsFieldErrors.top_k)"
            aria-describedby="runtime-top-k-range runtime-top-k-error"
          />
          <small id="runtime-top-k-range">
            允许 3–10，推荐 5。增加可能提升覆盖，也会增加延迟、成本和无关内容。
          </small>
          <small
            v-if="runtimeSettingsFieldErrors.top_k"
            id="runtime-top-k-error"
            class="settings-field-error"
            aria-live="polite"
          >
            {{ runtimeSettingsFieldErrors.top_k }}
          </small>
        </label>
        <label class="settings-field">
          <span>单次回答最大输出 token</span>
          <input
            id="runtime-max-output"
            v-model.number="runtimeSettingsForm.max_output_tokens"
            type="number"
            min="2048"
            max="384000"
            step="1024"
            :aria-invalid="Boolean(runtimeSettingsFieldErrors.max_output_tokens)"
            aria-describedby="runtime-max-output-range runtime-max-output-error"
          />
          <small id="runtime-max-output-range">
            允许 2,048–384,000，推荐 8,192。过大会增加费用和等待时间。
          </small>
          <small
            v-if="runtimeSettingsFieldErrors.max_output_tokens"
            id="runtime-max-output-error"
            class="settings-field-error"
            aria-live="polite"
          >
            {{ runtimeSettingsFieldErrors.max_output_tokens }}
          </small>
        </label>
      </div>

      <details class="settings-advanced">
        <summary>高级预算与工具限制</summary>
        <p>
          这些是保护性上限。调整前请确认理解上下文成本；保存后只影响下一次新问答。
        </p>
        <div class="settings-grid">
          <label class="settings-field">
            <span>单次上下文预算</span>
            <input
              id="runtime-max-context"
              v-model.number="runtimeSettingsForm.max_context_tokens"
              type="number"
              min="10000"
              max="1000000"
              step="10000"
              :aria-invalid="Boolean(runtimeSettingsFieldErrors.max_context_tokens)"
              aria-describedby="runtime-max-context-range runtime-max-context-error"
            />
            <small id="runtime-max-context-range">
              允许 10,000–1,000,000 的整数，推荐 100,000。
            </small>
            <small
              v-if="runtimeSettingsFieldErrors.max_context_tokens"
              id="runtime-max-context-error"
              class="settings-field-error"
              aria-live="polite"
            >
              {{ runtimeSettingsFieldErrors.max_context_tokens }}
            </small>
          </label>
          <label class="settings-field">
            <span>请求累计预算</span>
            <input
              id="runtime-max-request"
              v-model.number="runtimeSettingsForm.max_request_tokens"
              type="number"
              min="10000"
              step="10000"
              :aria-invalid="Boolean(runtimeSettingsFieldErrors.max_request_tokens)"
              aria-describedby="runtime-max-request-range runtime-max-request-error"
            />
            <small id="runtime-max-request-range">
              允许 10,000 以上，推荐 400,000；不能小于单次上下文预算。
            </small>
            <small
              v-if="runtimeSettingsFieldErrors.max_request_tokens"
              id="runtime-max-request-error"
              class="settings-field-error"
              aria-live="polite"
            >
              {{ runtimeSettingsFieldErrors.max_request_tokens }}
            </small>
          </label>
          <label class="settings-field">
            <span>最大工具调用数</span>
            <input
              id="runtime-max-tool-calls"
              v-model.number="runtimeSettingsForm.max_tool_calls"
              type="number"
              min="1"
              max="20"
              step="1"
              :aria-invalid="Boolean(runtimeSettingsFieldErrors.max_tool_calls)"
              aria-describedby="runtime-max-tool-calls-range runtime-max-tool-calls-error"
            />
            <small id="runtime-max-tool-calls-range">
              允许 1–20 的整数，推荐 8。达到上限后停止检索并生成部分回答。
            </small>
            <small
              v-if="runtimeSettingsFieldErrors.max_tool_calls"
              id="runtime-max-tool-calls-error"
              class="settings-field-error"
              aria-live="polite"
            >
              {{ runtimeSettingsFieldErrors.max_tool_calls }}
            </small>
          </label>
        </div>
      </details>

      <div class="settings-managed">
        <div>
          <strong>系统管理参数</strong>
          <small>这些实现常数影响检索语义，暂不开放普通设置。</small>
        </div>
        <span>混合检索：dense + FTS</span>
        <span>候选深度：20</span>
        <span>RRF k：60</span>
      </div>

      <div class="settings-actions">
        <button
          type="button"
          class="primary-button"
          :disabled="runtimeSettingsBusy || runtimeSettingsHasErrors"
          @click="saveRuntimeSettings"
        >
          {{ runtimeSettingsBusy ? "保存中…" : "保存运行设置" }}
        </button>
        <button type="button" class="secondary-button" :disabled="runtimeSettingsBusy" @click="restoreRuntimeDefaults">
          恢复推荐值
        </button>
        <span v-if="runtimeSettings?.updated_at" class="settings-saved-at">
          最近保存：{{ taskTimeLabel(runtimeSettings.updated_at) }}
        </span>
      </div>
      <p v-if="runtimeSettingsMessage" class="settings-message">{{ runtimeSettingsMessage }}</p>
      <p v-if="runtimeSettingsError" class="error-box">{{ runtimeSettingsError }}</p>
    </section>

    <section v-if="activeSection === 'settings'" class="settings-card diagnostics-settings-card">
      <div class="section-heading">
        <div>
          <p class="eyebrow">LOCAL DIAGNOSTICS</p>
          <h2>运行诊断</h2>
        </div>
        <span class="settings-pill configured">
          {{ traceModeLabel(runtimeSettingsForm.trace_mode) }}
        </span>
      </div>
      <p class="settings-description">
        保存每次问答的运行现场，便于解释检索、模型、预算和取消行为。该设置只影响之后的问答。
      </p>
      <label class="settings-field diagnostics-mode-field">
        <span>Trace 记录模式</span>
        <select v-model="runtimeSettingsForm.trace_mode">
          <option value="off">关闭：不生成 trace</option>
          <option value="metadata">元数据：保留状态、token、hash 和耗时</option>
          <option value="full">完整内容：额外保留提示词、工具结果和回答正文</option>
        </select>
        <small>
          开发环境推荐“完整内容”；分发环境默认“元数据”。正文只保存在本机。
        </small>
      </label>
      <div class="settings-actions">
        <button
          type="button"
          class="primary-button"
          :disabled="diagnosticsTraceModeBusy"
          @click="saveDiagnosticsTraceMode"
        >
          {{ diagnosticsTraceModeBusy ? "保存中…" : "保存记录模式" }}
        </button>
        <button
          type="button"
          class="secondary-button"
          @click="openTraceDiagnostics()"
        >
          打开运行诊断
        </button>
      </div>
      <p v-if="diagnosticsTraceModeMessage" class="settings-message">
        {{ diagnosticsTraceModeMessage }}
      </p>
      <p v-if="diagnosticsTraceModeError" class="error-box">
        {{ diagnosticsTraceModeError }}
      </p>
    </section>

    <section v-if="activeSection === 'settings'" class="reset-card">
      <div class="section-heading">
        <div>
          <p class="eyebrow">LOCAL DATA</p>
          <h2>本地数据管理</h2>
        </div>
      </div>
      <p class="settings-description">
        清空文章、文档版本、入库任务、操作记录、collection 快照、索引、job artifacts
        和数据库备份。embedding Key、模型设置和应用配置会保留。
      </p>
      <p v-if="hasExecutableTasks" class="reset-warning">
        当前有后台入库任务，请先等待任务结束或取消任务后再清空。
      </p>
      <button
        v-if="!resetDialogOpen"
        type="button"
        class="danger-button reset-open-button"
        :disabled="hasExecutableTasks"
        @click="openResetDialog"
      >
        清空知识库
      </button>
      <div v-else class="reset-confirmation">
        <label for="reset-confirmation">
          此操作不可撤销。请输入“清空知识库”确认：
        </label>
        <input
          id="reset-confirmation"
          v-model="resetConfirmation"
          type="text"
          autocomplete="off"
          :disabled="resetBusy"
          @keyup.enter="resetWorkspace"
        />
        <div class="reset-actions">
          <button
            type="button"
            class="secondary-button"
            :disabled="resetBusy"
            @click="closeResetDialog"
          >
            取消
          </button>
          <button
            type="button"
            class="danger-button"
            :disabled="!canConfirmReset"
            @click="resetWorkspace"
          >
            {{ resetBusy ? "正在清空…" : "确认清空全部数据" }}
          </button>
        </div>
      </div>
      <p v-if="resetError" class="error-box">{{ resetError }}</p>
      <p v-if="resetMessage" class="settings-message">{{ resetMessage }}</p>
    </section>

    <div v-if="activeSection === 'ingest'" class="workspace-tabs" role="tablist" aria-label="入库方式">
      <button
        type="button"
        :class="{ active: ingestMode === 'batch' }"
        @click="ingestMode = 'batch'"
      >
        批量入库
        <small>多个文件或 URL</small>
      </button>
      <button
        type="button"
        :class="{ active: ingestMode === 'single' }"
        @click="ingestMode = 'single'"
      >
        单篇预览
        <small>先检查结构再保存</small>
      </button>
    </div>

    <section
      v-if="activeSection === 'ingest' && ingestMode === 'batch'"
      class="task-card"
    >
      <div class="section-heading">
        <div>
          <p class="eyebrow">BACKGROUND INGESTION</p>
          <h2>批量入库工作台</h2>
        </div>
        <span v-if="activeTasks.length" class="task-status-pill task-queued">
          {{ taskQueueSummary }}
        </span>
      </div>
      <p class="settings-description">
        每个批次加入持久任务队列，后台按顺序执行解析、保存、embedding、建索引和 collection 发布。
      </p>

      <div class="task-workspace">
        <section class="task-composer">
          <div class="panel-heading">
            <div>
              <p class="eyebrow">NEW BATCH</p>
              <h3>新建批次</h3>
            </div>
            <span>{{ taskSources.length }} 个来源</span>
          </div>

          <div class="task-source-actions">
            <button type="button" class="secondary-button" @click="addTaskFiles">
              添加 HTML 文件
            </button>
            <div class="task-url-row">
              <input
                v-model="taskUrl"
                type="url"
                placeholder="添加公开网页 URL"
                @keyup.enter="addTaskUrl"
              />
              <button type="button" class="secondary-button" @click="addTaskUrl">
                添加 URL
              </button>
            </div>
          </div>

          <ol v-if="taskSources.length" class="task-source-list">
            <li
              v-for="(source, index) in taskSources"
              :key="`${source.source_kind}-${source.source_value}`"
            >
              <span class="source-kind">{{ source.source_kind === "file" ? "文件" : "URL" }}</span>
              <span class="task-source-value">{{ source.source_value }}</span>
              <button type="button" class="remove-source" @click="removeTaskSource(index)">
                移除
              </button>
            </li>
          </ol>
          <p v-else class="empty-queue">还没有待入库来源。</p>

          <div class="task-actions">
            <button
              type="button"
              class="primary-button"
              :disabled="!canStartTask"
              @click="startBackgroundTask"
            >
              {{ taskBusy ? "正在加入…" : "加入任务队列" }}
            </button>
            <span v-if="!modelSettings?.embedding_configured" class="task-hint">
              请先配置 embedding Key。
              <button type="button" class="inline-link" @click="selectSection('settings')">
                去设置
              </button>
            </span>
          </div>
          <p v-if="taskError" class="error-box">{{ taskError }}</p>
        </section>

        <section class="task-monitor">
          <div class="panel-heading">
            <div>
              <p class="eyebrow">TASK QUEUE</p>
              <h3>任务队列</h3>
            </div>
            <span>{{ activeTasks.length }} 个非终态任务</span>
          </div>

          <p v-if="!activeTasks.length && !backgroundTask" class="empty-queue">
            新提交的批次会显示在这里。
          </p>

          <ol v-if="activeTasks.length" class="active-task-queue">
            <li v-for="task in activeTasks" :key="task.task_id">
              <div
                class="active-task-queue-item"
                :class="{ selected: selectedTaskId === task.task_id }"
              >
                <button
                  type="button"
                  class="queue-task-select"
                  @click="selectActiveTask(task)"
                >
                  <span class="queue-position">
                    {{
                      task.status === "queued"
                        ? `Q${taskQueuePosition(task) ?? "-"}`
                        : task.status === "running"
                          ? "运行"
                          : task.status === "paused"
                            ? "暂停"
                            : "中断"
                    }}
                  </span>
                  <span class="queue-task-main">
                    <strong>
                      {{
                        currentTaskSourceLabel(task) ||
                        `${task.total_sources} 个来源`
                      }}
                    </strong>
                    <small>
                      {{ taskStageLabel(task.stage) }} ·
                      已完成 {{ task.completed_sources }} / {{ task.total_sources }}
                      <template v-if="taskElapsedLabel(task)">
                        · 已用 {{ taskElapsedLabel(task) }}
                      </template>
                    </small>
                  </span>
                  <span class="task-status-pill" :class="taskStatusClass(task)">
                    {{ taskDisplayStatus(task) }}
                  </span>
                </button>
                <div class="queue-task-actions">
                  <button
                    v-if="taskCanPause(task)"
                    type="button"
                    class="secondary-button"
                    :disabled="taskBusy"
                    @click="controlQueueTask(task, 'pause')"
                  >
                    暂停
                  </button>
                  <button
                    v-if="taskCanResume(task)"
                    type="button"
                    class="secondary-button"
                    :disabled="taskBusy"
                    @click="controlQueueTask(task, 'resume')"
                  >
                    恢复
                  </button>
                  <button
                    v-if="taskCanCancel(task)"
                    type="button"
                    class="danger-button"
                    :disabled="taskBusy"
                    @click="controlQueueTask(task, 'cancel')"
                  >
                    取消
                  </button>
                </div>
              </div>
            </li>
          </ol>

          <div v-if="backgroundTask" class="task-progress">
            <div class="task-progress-head">
              <strong>{{ taskDisplayStatus(backgroundTask) }}</strong>
              <span>
                {{ backgroundTask.completed_sources }} / {{ backgroundTask.total_sources }}
                {{ backgroundTask.failed_sources ? `· ${backgroundTask.failed_sources} 个失败` : "" }}
              </span>
            </div>
            <p v-if="currentTaskSourceLabel(backgroundTask)" class="task-current-source">
              正在处理：{{ currentTaskSourceLabel(backgroundTask) }}
              <template v-if="taskElapsedLabel(backgroundTask)">
                · 已用 {{ taskElapsedLabel(backgroundTask) }}
              </template>
            </p>
            <div
              class="task-progress-track"
              :class="{ 'has-failures': backgroundTask.failed_sources > 0 }"
            >
              <span
                :style="{
                  width: `${backgroundTask.total_sources
                    ? (backgroundTask.completed_sources / backgroundTask.total_sources) * 100
                    : 0}%`,
                }"
              ></span>
            </div>
            <div class="task-controls">
              <button
                v-if="backgroundTask.status === 'running'"
                type="button"
                class="secondary-button"
                :disabled="taskBusy"
                @click="pauseBackgroundTask"
              >
                暂停
              </button>
              <button
                v-if="['paused', 'interrupted'].includes(backgroundTask.status)"
                type="button"
                class="primary-button"
                :disabled="taskBusy"
                @click="resumeBackgroundTask"
              >
                恢复
              </button>
              <button
                v-if="selectedTaskCanControl"
                type="button"
                class="danger-button"
                :disabled="taskBusy"
                @click="cancelBackgroundTask"
              >
                取消
              </button>
              <span v-if="backgroundTask.snapshot_id" class="task-snapshot">
                已发布：{{ backgroundTask.snapshot_id }}
              </span>
            </div>
            <ul v-if="backgroundTask.failures.length" class="task-failures">
              <li v-for="failure in backgroundTask.failures" :key="failure.source_index">
                来源 {{ failure.source_index + 1 }}：{{
                  ingestErrorMessage(failure.code, failure.message)
                }}
              </li>
            </ul>
          </div>
        </section>
      </div>

      <div v-if="currentCollection" class="collection-summary">
        <strong>当前知识库</strong>
        <span>
          {{ currentCollection.manifest.totals.documents }} 篇 ·
          {{ currentCollection.manifest.totals.sections }} 节 ·
          {{ currentCollection.manifest.totals.blocks }} blocks
        </span>
        <span v-if="currentCollection.manifest.completeness === 'partial'" class="partial-label">
          部分入库
        </span>
      </div>
    </section>

    <section
      v-if="activeSection === 'knowledge' && !activeDocumentId"
      class="knowledge-card"
    >
      <div class="section-heading">
        <div>
          <p class="eyebrow">CURRENT KNOWLEDGE BASE</p>
          <h2>当前知识库</h2>
        </div>
        <span v-if="currentCollection" class="knowledge-total">
          {{ currentCollection.manifest.totals.documents }} 篇文章
        </span>
      </div>
      <p class="settings-description">
        这里显示下一次提问真正能够检索到的文章；重新导入同一文章会替换其旧版本。
      </p>
      <ul v-if="currentCollection?.manifest.documents.length" class="knowledge-documents">
        <li
          v-for="document in currentCollection.manifest.documents"
          :key="`${document.doc_id}-${document.doc_version}`"
        >
          <button
            type="button"
            class="knowledge-document-open"
            @click="openDocumentDetail(document.doc_id)"
          >
            <div>
              <strong>{{ document.title }}</strong>
              <span>{{ document.source_label }}</span>
            </div>
            <small>
              版本 {{ document.doc_version }} · {{ document.section_count }} 节 ·
              {{ document.block_count }} blocks
            </small>
            <span class="document-chevron" aria-hidden="true">›</span>
          </button>
        </li>
      </ul>
      <div v-else class="empty-state">
        <p>知识库中还没有文章。</p>
        <button
          type="button"
          class="primary-button"
          @click="
            ingestMode = 'batch';
            selectSection('ingest');
          "
        >
          去导入文章
        </button>
      </div>
    </section>

    <section
      v-if="activeSection === 'knowledge' && activeDocumentId"
      class="document-detail-card"
    >
      <div class="detail-breadcrumb">
        <button type="button" class="inline-link" @click="closeDocumentDetail">
          ← 返回知识库
        </button>
        <span>知识库 / {{ documentDetail?.title ?? activeDocumentId }}</span>
      </div>

      <p v-if="documentDetailLoading" class="detail-loading">正在读取文档详情…</p>
      <p v-if="documentDetailError" class="error-box">{{ documentDetailError }}</p>

      <template v-if="documentDetail">
        <header class="document-detail-head">
          <div>
            <p class="eyebrow">DOCUMENT DETAIL</p>
            <h2>{{ documentDetail.title }}</h2>
            <p class="document-detail-source">
              {{ documentDetail.source_label }} ·
              {{ documentLanguageLabel(documentDetail.language) }} ·
              当前版本 v{{ documentDetail.doc_version }}
            </p>
          </div>
          <button type="button" class="secondary-button" @click="copyDocumentSource">
            复制来源地址
          </button>
        </header>

        <dl class="document-detail-stats">
          <div><dt>章节</dt><dd>{{ documentDetail.sections.length }}</dd></div>
          <div><dt>Blocks</dt><dd>{{ documentDetail.blocks.length }}</dd></div>
          <div><dt>入库时间</dt><dd>{{ taskTimeLabel(documentDetail.published_at) }}</dd></div>
          <div><dt>状态</dt><dd>当前可检索</dd></div>
        </dl>

        <section v-if="documentDetail.warnings.length" class="detail-warning">
          <strong>解析提示</strong>
          <span v-for="warning in documentDetail.warnings" :key="warning">
            {{ warningLabel(warning) }}
          </span>
        </section>

        <nav class="detail-tabs" aria-label="文档详情">
          <button
            type="button"
            :class="{ active: documentDetailTab === 'content' }"
            @click="documentDetailTab = 'content'"
          >
            内容
          </button>
          <button
            type="button"
            :class="{ active: documentDetailTab === 'versions' }"
            @click="documentDetailTab = 'versions'"
          >
            版本与记录
          </button>
          <button
            type="button"
            :class="{ active: documentDetailTab === 'technical' }"
            @click="documentDetailTab = 'technical'"
          >
            技术信息
          </button>
        </nav>

        <div v-if="documentDetailTab === 'content'" class="document-reader">
          <aside class="document-outline">
            <p class="eyebrow">OUTLINE</p>
            <button
              v-for="(item, index) in documentSections"
              :key="`${item.section.ordinal}-${item.section.path.join('/')}`"
              type="button"
              :style="{ paddingLeft: `${10 + Math.max(0, item.section.depth - 1) * 12}px` }"
              @click="scrollToDocumentSection(index)"
            >
              <span>{{ item.section.ordinal || "0" }}</span>
              {{ item.section.title }}
              <small>{{ item.blocks.length }} blocks</small>
            </button>
          </aside>
          <article class="document-content">
            <section
              v-for="(item, index) in documentSections"
              :id="`document-section-${index}`"
              :key="`${item.section.ordinal}-${item.section.path.join('/')}`"
              class="document-section"
            >
              <header>
                <span>{{ item.section.ordinal || "0" }}</span>
                <h3>{{ item.section.title || "正文" }}</h3>
              </header>
              <pre
                v-for="block in item.blocks"
                :id="`document-block-${encodeURIComponent(block.block_id)}`"
                :key="block.block_id"
                class="document-block"
                :class="{
                  'citation-highlight':
                    highlightedDocumentBlockId === block.block_id,
                }"
              >{{ block.text }}</pre>
              <p v-if="!item.blocks.length" class="detail-empty">
                该章节没有直属内容。
              </p>
            </section>
          </article>
        </div>

        <div v-else-if="documentDetailTab === 'versions'" class="detail-stack">
          <section class="detail-panel">
            <h3>版本记录</h3>
            <ol class="version-list">
              <li v-for="version in documentDetail.versions" :key="version.doc_version">
                <div>
                  <strong>版本 {{ version.doc_version }}</strong>
                  <span>{{ version.title }}</span>
                </div>
                <span v-if="version.is_active" class="knowledge-total">当前活动版本</span>
                <small>
                  {{ taskTimeLabel(version.created_at) }} ·
                  {{ version.extractor_name }} v{{ version.extractor_version }}
                </small>
              </li>
            </ol>
          </section>
          <section class="detail-panel">
            <h3>相关入库记录</h3>
            <ol v-if="documentDetail.activities.length" class="history-list">
              <li v-for="task in documentDetail.activities" :key="task.task_id">
                <div class="history-row">
                  <div>
                    <strong>{{ taskTimeLabel(task.created_at) }}</strong>
                    <span>
                      {{ task.total_sources }} 个来源 · 成功
                      {{ task.completed_sources - task.failed_sources }} 个
                    </span>
                  </div>
                  <span class="task-status-pill" :class="`task-${task.status}`">
                    {{ taskStatusLabel(task.status) }}
                  </span>
                </div>
              </li>
            </ol>
            <p v-else class="detail-empty">没有找到相关入库记录。</p>
          </section>
        </div>

        <div v-else class="detail-stack">
          <section class="detail-panel">
            <h3>文档身份</h3>
            <dl class="technical-grid">
              <div><dt>doc_id</dt><dd>{{ documentDetail.doc_id }}</dd></div>
              <div><dt>source</dt><dd>{{ documentDetail.source.source_uri }}</dd></div>
              <div><dt>快照</dt><dd>{{ documentDetail.snapshot_id }}</dd></div>
              <div><dt>内容 hash</dt><dd>{{ documentDetail.source.content_hash }}</dd></div>
            </dl>
          </section>
          <section class="detail-panel">
            <h3>解析与索引配置</h3>
            <dl class="technical-grid">
              <div>
                <dt>Extractor</dt>
                <dd>
                  {{ documentDetail.technical.extractor_name }} v{{
                    documentDetail.technical.extractor_version
                  }}
                </dd>
              </div>
              <div>
                <dt>Section builder</dt>
                <dd>
                  {{ documentDetail.technical.section_builder_name }} v{{
                    documentDetail.technical.section_builder_version
                  }}
                </dd>
              </div>
              <div>
                <dt>Chunker</dt>
                <dd>
                  {{ documentDetail.technical.chunker_name }} v{{
                    documentDetail.technical.chunker_version
                  }}
                </dd>
              </div>
              <div>
                <dt>Embedding</dt>
                <dd>
                  {{ documentDetail.technical.embedding_model_id }} ·
                  {{ documentDetail.technical.embedding_dimension }} 维
                </dd>
              </div>
            </dl>
          </section>
          <section class="detail-panel">
            <h3>来源获取</h3>
            <dl class="technical-grid">
              <div><dt>类型</dt><dd>{{ documentDetail.source.source_kind }}</dd></div>
              <div><dt>获取方式</dt><dd>{{ documentDetail.source.acquisition_mode }}</dd></div>
              <div><dt>字符集</dt><dd>{{ documentDetail.source.charset }}</dd></div>
              <div><dt>Extraction hash</dt><dd>{{ documentDetail.technical.extraction_hash }}</dd></div>
            </dl>
          </section>
        </div>
      </template>
    </section>

    <section v-if="activeSection === 'history'" class="history-card">
      <div class="section-heading">
        <div>
          <p class="eyebrow">INGESTION HISTORY</p>
          <h2>最近入库记录</h2>
        </div>
        <button
          type="button"
          class="secondary-button history-refresh"
          :disabled="historyBusy"
          @click="loadTaskHistory"
        >
          {{ historyBusy ? "读取中…" : "刷新" }}
        </button>
      </div>
      <p class="settings-description">
        每次批量操作都会保留来源、结果和失败原因，应用重启后仍可查看。
      </p>
      <p v-if="historyError" class="error-box">{{ historyError }}</p>
      <ol v-if="taskHistory.length" class="history-list">
        <li v-for="task in taskHistory" :key="task.task_id">
          <div class="history-row">
            <div>
              <strong>{{ taskTimeLabel(task.created_at) }}</strong>
              <span>
                {{ task.total_sources }} 个来源 · 成功
                {{ task.completed_sources - task.failed_sources }} 个
                <template v-if="task.failed_sources"> · 失败 {{ task.failed_sources }} 个</template>
              </span>
            </div>
            <span class="task-status-pill" :class="taskStatusClass(task)">
              {{ taskDisplayStatus(task) }}
            </span>
          </div>
          <div
            v-if="taskCanResume(task) || taskCanCancel(task)"
            class="history-actions-row"
          >
            <button
              v-if="taskCanResume(task)"
              type="button"
              class="secondary-button"
              :disabled="taskBusy"
              @click="controlHistoryTask(task, 'resume')"
            >
              恢复
            </button>
            <button
              v-if="taskCanCancel(task)"
              type="button"
              class="danger-button"
              :disabled="taskBusy"
              @click="controlHistoryTask(task, 'cancel')"
            >
              取消
            </button>
            <span class="task-snapshot">{{ taskStageLabel(task.stage) }}</span>
          </div>
          <details>
            <summary>查看来源和处理结果</summary>
            <ul class="history-sources">
              <li
                v-for="(source, sourceIndex) in task.sources"
                :key="`${source.source_kind}-${source.source_value}`"
                :class="taskSourceResultClass(task, sourceIndex)"
              >
                <div>
                  <span class="source-kind">{{ source.source_kind === "file" ? "文件" : "URL" }}</span>
                  <span class="task-source-value">{{ source.source_value }}</span>
                </div>
                <small>{{ taskSourceResult(task, sourceIndex) }}</small>
              </li>
            </ul>
            <p v-if="task.snapshot_id" class="history-snapshot">
              snapshot：{{ task.snapshot_id }}
            </p>
            <p v-if="task.error_message" class="history-error">
              {{ ingestErrorMessage(task.error_code ?? "task_failed", task.error_message) }}
            </p>
          </details>
        </li>
      </ol>
      <p v-else-if="!historyBusy" class="empty-history">还没有入库操作记录。</p>
    </section>

    <section
      v-if="activeSection === 'ingest' && ingestMode === 'single'"
      class="import-card"
    >
      <div class="section-heading">
        <div>
          <p class="eyebrow">STEP 01</p>
          <h2>导入一份 HTML</h2>
        </div>
        <p>首版只读取服务器返回的静态／SSR HTML，不执行页面脚本。</p>
      </div>

      <div class="source-switch" role="tablist" aria-label="文档来源">
        <button
          type="button"
          :class="{ active: sourceKind === 'file' }"
          @click="sourceKind = 'file'"
        >
          本地文件
        </button>
        <button
          type="button"
          :class="{ active: sourceKind === 'url' }"
          @click="sourceKind = 'url'"
        >
          网页 URL
        </button>
      </div>

      <div v-if="sourceKind === 'file'" class="source-row">
        <button type="button" class="secondary-button" @click="chooseFile">
          选择 HTML 文件
        </button>
        <span class="source-path">{{ selectedFilePath || "尚未选择文件" }}</span>
      </div>
      <div v-else class="source-row">
        <input
          v-model="urlValue"
          type="url"
          placeholder="https://example.com/article"
          @keyup.enter="ingest"
        />
      </div>

      <button
        type="button"
        class="primary-button"
        :disabled="!canIngest || !sourceValue"
        @click="ingest"
      >
        {{ ingesting ? "正在提取…" : "解析文档结构" }}
      </button>

      <p v-if="ingestError" class="error-box">{{ ingestError }}</p>
    </section>

    <template v-if="activeSection === 'ingest' && ingestMode === 'single' && preview">
      <section class="preview-head">
        <div>
          <p class="eyebrow">DOCUMENT PREVIEW</p>
          <h2>{{ preview.document.title }}</h2>
          <p class="source-meta">
            {{ preview.document.source_label }} ·
            {{ languageLabel }} ·
            已生成结构预览
          </p>
        </div>
        <dl class="preview-stats">
          <div><dt>章节</dt><dd>{{ preview.section_tree.sections.length }}</dd></div>
          <div><dt>Blocks</dt><dd>{{ preview.block_set.blocks.length }}</dd></div>
          <div><dt>字符</dt><dd>{{ preview.extracted.stats.character_count }}</dd></div>
        </dl>
      </section>

      <section class="save-strip">
        <div>
          <strong>保存并入知识库</strong>
          <p v-if="!singleIngestTask">
            将复用当前预览内容，依次完成文档保存、embedding、双索引和 collection 发布。
          </p>
          <p v-else-if="singleIngestTask.status === 'succeeded'" class="save-result">
            已进入知识库，发布快照 {{ singleIngestTask.snapshot_id ?? "已提交" }}。
          </p>
          <p v-else-if="['queued', 'running', 'paused'].includes(singleIngestTask.status)">
            已提交单篇入库：{{ taskStatusLabel(singleIngestTask.status) }} ·
            {{ taskStageLabel(singleIngestTask.stage) }}
          </p>
          <p v-else class="save-result">
            上次单篇入库未完成：{{ taskStatusLabel(singleIngestTask.status) }}。可以重新提交。
          </p>
        </div>
        <button
          type="button"
          class="primary-button"
          :disabled="!canSave"
          @click="saveDocument"
        >
          {{
            saving
              ? "正在提交…"
              : singleIngestTask?.status === "succeeded"
                ? "已进入知识库"
                : singleIngestTask &&
                    ["failed", "canceled", "interrupted"].includes(singleIngestTask.status)
                  ? "重新提交入库"
                  : "开始入库"
          }}
        </button>
      </section>
      <p v-if="!modelSettings?.embedding_configured" class="task-hint">
        单篇入库需要 embedding Key。
        <button type="button" class="inline-link" @click="selectSection('settings')">
          去设置
        </button>
      </p>
      <p v-if="saveError" class="error-box">{{ saveError }}</p>

      <section v-if="preview.warnings.length" class="warning-list">
        <strong>提取警告</strong>
        <span
          v-for="warning in preview.warnings"
          :key="warning"
          :title="warning"
        >
          {{ warningLabel(warning) }}
        </span>
      </section>

      <div class="preview-grid">
        <aside class="outline-card">
          <p class="eyebrow">OUTLINE</p>
          <h3>章节结构</h3>
          <ol class="outline-list">
            <li
              v-for="section in preview.section_tree.sections"
              :key="`${section.ordinal}-${section.path.join('/')}`"
              :style="{ paddingLeft: `${Math.max(0, section.depth - 1) * 14}px` }"
            >
              <span class="ordinal">{{ section.ordinal }}</span>
              <div>
                <strong>{{ section.title }}</strong>
                <small>{{ section.first_line }}</small>
              </div>
            </li>
          </ol>
        </aside>

        <section class="blocks-panel">
          <div class="blocks-heading">
            <div>
              <p class="eyebrow">EVIDENCE BLOCKS</p>
              <h3>可引用内容</h3>
            </div>
            <span>按原文顺序</span>
          </div>
          <article
            v-for="block in preview.block_set.blocks"
            :key="block.block_id"
            class="block-card"
          >
            <div class="block-meta">
              <strong>Block {{ block.block_index }}</strong>
              <span>
                {{ block.section_path.join(" / ") || "根节" }}
                · ~{{ block.context_token_estimate }} tokens
              </span>
            </div>
            <pre>{{ block.text }}</pre>
          </article>
        </section>
      </div>
    </template>
  </main>
</template>
