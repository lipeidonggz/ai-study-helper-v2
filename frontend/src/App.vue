<script setup lang="ts">
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

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
type AppSection = "knowledge" | "ingest" | "history" | "settings";
/** 入库工作区的两个子页面。 */
type IngestMode = "batch" | "single";

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
const settingsBusy = ref(false);
const settingsLoading = ref(true);
const settingsError = ref("");
const settingsMessage = ref("");
const taskUrl = ref("");
const taskSources = ref<BackgroundSource[]>([]);
const backgroundTask = ref<BackgroundTaskRecord | null>(null);
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

let unlisten: UnlistenFn | undefined;
let healthTimer: number | undefined;
let taskTimer: number | undefined;
let disposed = false;

/** 从 URL hash 解析当前工作区；未知值回退到知识库。 */
function readSectionFromHash(): AppSection {
  const value = window.location.hash.replace(/^#/, "").split("/")[0];
  return value === "ingest" || value === "history" || value === "settings"
    ? value
    : "knowledge";
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
    void loadDocumentDetail(activeDocumentId.value);
  } else {
    documentDetail.value = null;
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

/** 单篇入库要求预览成功、embedding 可用且当前没有活动任务。 */
const canSave = computed(
  () =>
    managerStatus.value === "running" &&
    runtime.value !== null &&
    preview.value !== null &&
    modelSettings.value?.embedding_configured === true &&
    (!singleIngestTask.value ||
      ["failed", "canceled", "interrupted"].includes(singleIngestTask.value.status)) &&
    !(
      backgroundTask.value &&
      ["queued", "running", "paused"].includes(backgroundTask.value.status)
    ) &&
    !saving.value,
);

/** 后台任务是否具备提交条件。 */
const canStartTask = computed(
  () =>
    managerStatus.value === "running" &&
    modelSettings.value?.embedding_configured === true &&
    taskSources.value.length > 0 &&
    !taskBusy.value &&
    (!backgroundTask.value ||
      ["succeeded", "failed", "canceled"].includes(backgroundTask.value.status)),
);

/** 后台任务是否正在运行或等待暂停。 */
const taskIsActive = computed(
  () =>
    backgroundTask.value !== null &&
    ["queued", "running", "paused"].includes(backgroundTask.value.status),
);

/** 只有没有活动任务且确认短语完全匹配时才允许执行全量清空。 */
const canConfirmReset = computed(
  () =>
    !taskIsActive.value &&
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
      const response = await fetch(`${connection.value.baseUrl}/api/settings/models`, {
        headers: { "x-launch-token": connection.value.token },
        cache: "no-store",
      });
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
  if (!connection.value || !embeddingKey.value.trim() || settingsBusy.value) {
    return;
  }
  settingsBusy.value = true;
  settingsError.value = "";
  settingsMessage.value = "";
  try {
    const response = await fetch(`${connection.value.baseUrl}/api/settings/embedding-key`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-launch-token": connection.value.token,
      },
      body: JSON.stringify({ api_key: embeddingKey.value.trim() }),
    });
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
    settingsBusy.value = false;
  }
}

/** 使用已保存 Key 做一次真实的短文本 embedding 检查。 */
async function testEmbeddingKey(): Promise<void> {
  if (!connection.value || settingsBusy.value) {
    return;
  }
  settingsBusy.value = true;
  settingsError.value = "";
  settingsMessage.value = "";
  try {
    const response = await fetch(
      `${connection.value.baseUrl}/api/settings/embedding-key/test`,
      {
        method: "POST",
        headers: { "x-launch-token": connection.value.token },
      },
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
    settingsBusy.value = false;
  }
}

/** 删除已保存 Key，并清除旧连通性结果。 */
async function deleteEmbeddingKey(): Promise<void> {
  if (!connection.value || settingsBusy.value) {
    return;
  }
  settingsBusy.value = true;
  settingsError.value = "";
  settingsMessage.value = "";
  try {
    const response = await fetch(
      `${connection.value.baseUrl}/api/settings/embedding-key/delete`,
      {
        method: "POST",
        headers: { "x-launch-token": connection.value.token },
      },
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
    settingsBusy.value = false;
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
    const response = await fetch(`${connection.value.baseUrl}/api/workspace/reset`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-launch-token": connection.value.token,
      },
      body: JSON.stringify({ confirmation: resetConfirmation.value.trim() }),
    });
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
    const response = await fetch(
      `${connection.value.baseUrl}/api/collections/current/documents/${encodeURIComponent(docId)}`,
      {
        headers: { "x-launch-token": connection.value.token },
        cache: "no-store",
      },
    );
    const body = (await response.json()) as DocumentDetailResponse | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      throw new Error(
        failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`,
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

/** 打开知识库中的文档详情，并用 URL hash 保留当前位置。 */
function openDocumentDetail(docId: string): void {
  const location = `knowledge/${encodeURIComponent(docId)}`;
  if (window.location.hash === `#${location}`) {
    void loadDocumentDetail(docId);
    return;
  }
  activeSection.value = "knowledge";
  activeDocumentId.value = docId;
  documentDetailTab.value = "content";
  window.location.hash = location;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/** 从详情页返回知识库文章清单。 */
function closeDocumentDetail(): void {
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
    const response = await fetch(`${connection.value.baseUrl}/api/collections/current`, {
      headers: { "x-launch-token": connection.value.token },
      cache: "no-store",
    });
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
    const response = await fetch(`${connection.value.baseUrl}/api/tasks?limit=20`, {
      headers: { "x-launch-token": connection.value.token },
      cache: "no-store",
    });
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
    const response = await fetch(`${connection.value.baseUrl}/api/tasks/ingest`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-launch-token": connection.value.token,
      },
      body: JSON.stringify({ sources: taskSources.value }),
    });
    const body = (await response.json()) as BackgroundTaskRecord | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      throw new Error(
        failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`,
      );
    }
    backgroundTask.value = body as BackgroundTaskRecord;
    beginTaskPolling();
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

/** 轮询当前任务状态，完成或失败后自动停止。 */
async function refreshBackgroundTask(): Promise<void> {
  if (!connection.value || !backgroundTask.value) {
    return;
  }
  try {
    const response = await fetch(
      `${connection.value.baseUrl}/api/tasks/${backgroundTask.value.task_id}`,
      {
        headers: { "x-launch-token": connection.value.token },
        cache: "no-store",
      },
    );
    if (!response.ok) {
      return;
    }
    backgroundTask.value = (await response.json()) as BackgroundTaskRecord;
    syncSingleIngestTask();
    if (["succeeded", "failed", "canceled"].includes(backgroundTask.value.status)) {
      if (taskTimer !== undefined) {
        window.clearInterval(taskTimer);
        taskTimer = undefined;
      }
      if (backgroundTask.value.status === "succeeded") {
        await Promise.all([
          loadCurrentCollection(),
          loadTaskHistory(),
          ...(activeDocumentId.value
            ? [loadDocumentDetail(activeDocumentId.value)]
            : []),
        ]);
      } else {
        await loadTaskHistory();
      }
    }
  } catch {
    // 下一轮健康检查和轮询会继续尝试，不把瞬时网络错误变成任务失败。
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
    const response = await fetch(
      `${connection.value.baseUrl}/api/tasks/${backgroundTask.value.task_id}/${action}`,
      {
        method: "POST",
        headers: { "x-launch-token": connection.value.token },
      },
    );
    const body = (await response.json()) as BackgroundTaskRecord | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      throw new Error(
        failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`,
      );
    }
    backgroundTask.value = body as BackgroundTaskRecord;
    syncSingleIngestTask();
    beginTaskPolling();
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
    const response = await fetch(`${connection.value.baseUrl}/api/runtime`, {
      headers: {
        "x-launch-token": connection.value.token,
      },
      cache: "no-store",
    });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    runtime.value = (await response.json()) as RuntimeInfo;
    if (managerStatus.value === "running") {
      message.value = "Tauri 已连接本地 Python sidecar。";
      if (!modelSettings.value && !settingsLoading.value) {
        void loadModelSettings(2);
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
    await loadCurrentCollection();
    await loadTaskHistory();
    if (activeDocumentId.value) {
      await loadDocumentDetail(activeDocumentId.value);
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
    const response = await fetch(`${connection.value.baseUrl}/api/ingest/preview`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-launch-token": connection.value.token,
      },
      body: JSON.stringify({
        source_kind: sourceKind.value,
        source_value: sourceValue.value,
      }),
    });
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
    const response = await fetch(`${connection.value.baseUrl}/api/tasks/ingest/preview`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-launch-token": connection.value.token,
      },
      body: JSON.stringify({ job_id: preview.value.job_id }),
    });
    const body = (await response.json()) as BackgroundTaskRecord | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      const code = failure.error?.code ?? "request_failed";
      const detail = failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`;
      throw new Error(ingestErrorMessage(code, detail));
    }
    singleIngestTask.value = body as BackgroundTaskRecord;
    backgroundTask.value = body as BackgroundTaskRecord;
    beginTaskPolling();
    await loadTaskHistory();
    ingestMode.value = "batch";
  } catch (error) {
    saveError.value = String(error);
  } finally {
    saving.value = false;
  }
}

onMounted(async () => {
  window.addEventListener("hashchange", syncSectionFromHash);
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
  window.removeEventListener("hashchange", syncSectionFromHash);
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
        <h1>从文档出发，<br />把问题学明白。</h1>
      </div>
      <div class="backend-chip">
        <span class="status-dot" :class="stateClass" aria-hidden="true"></span>
        <div>
          <strong>{{ stateLabel }}</strong>
          <small>{{ runtime ? `PID ${runtime.pid}` : message }}</small>
        </div>
      </div>
    </header>

    <nav class="main-nav" aria-label="主导航">
      <button
        type="button"
        :class="{ active: activeSection === 'knowledge' }"
        :aria-current="activeSection === 'knowledge' ? 'page' : undefined"
        @click="selectSection('knowledge')"
      >
        <span class="nav-index">01</span>
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
        <span class="nav-index">02</span>
        <span>
          <strong>入库</strong>
          <small>{{ taskIsActive ? "任务进行中" : "添加新文章" }}</small>
        </span>
      </button>
      <button
        type="button"
        :class="{ active: activeSection === 'history' }"
        :aria-current="activeSection === 'history' ? 'page' : undefined"
        @click="selectSection('history')"
      >
        <span class="nav-index">03</span>
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
        <span class="nav-index">04</span>
        <span>
          <strong>设置</strong>
          <small>
            {{
              modelSettings?.embedding_configured
                ? "embedding 已配置"
                : "需要配置 embedding"
            }}
          </small>
        </span>
      </button>
    </nav>

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
          :disabled="settingsBusy || !embeddingKey.trim()"
          @click="saveEmbeddingKey"
        >
          保存 Key
        </button>
        <button
          type="button"
          class="secondary-button settings-test-button"
          :disabled="settingsBusy || !modelSettings?.embedding_configured"
          @click="testEmbeddingKey"
        >
          {{ settingsBusy ? "检查中…" : "测试连接" }}
        </button>
        <button
          v-if="modelSettings?.embedding_configured"
          type="button"
          class="danger-button"
          :disabled="settingsBusy"
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
      <p v-if="taskIsActive" class="reset-warning">
        当前有后台入库任务，请先等待任务结束或取消任务后再清空。
      </p>
      <button
        v-if="!resetDialogOpen"
        type="button"
        class="danger-button reset-open-button"
        :disabled="taskIsActive"
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
        <span v-if="backgroundTask" class="task-status-pill" :class="`task-${backgroundTask.status}`">
          {{ taskStatusLabel(backgroundTask.status) }}
        </span>
      </div>
      <p class="settings-description">
        将多个来源加入队列，后台会依次执行解析、保存、embedding、建索引，并在完成后自动发布 collection。
      </p>

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
        <li v-for="(source, index) in taskSources" :key="`${source.source_kind}-${source.source_value}`">
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
          {{ taskBusy && !taskIsActive ? "正在启动…" : "开始后台入库" }}
        </button>
        <span v-if="!modelSettings?.embedding_configured" class="task-hint">
          请先配置 embedding Key。
          <button type="button" class="inline-link" @click="selectSection('settings')">
            去设置
          </button>
        </span>
      </div>
      <p v-if="taskError" class="error-box">{{ taskError }}</p>

      <div v-if="backgroundTask" class="task-progress">
        <div class="task-progress-head">
          <strong>{{ taskStageLabel(backgroundTask.stage) }}</strong>
          <span>
            {{ backgroundTask.completed_sources }} / {{ backgroundTask.total_sources }}
            {{ backgroundTask.failed_sources ? `· ${backgroundTask.failed_sources} 个失败` : "" }}
          </span>
        </div>
        <div class="task-progress-track">
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
            v-if="['queued', 'running'].includes(backgroundTask.status)"
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
            v-if="taskIsActive"
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
                :key="block.block_id"
                class="document-block"
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
            <span class="task-status-pill" :class="`task-${task.status}`">
              {{
                task.failed_sources && task.status === "succeeded"
                  ? "部分入库"
                  : taskStatusLabel(task.status)
              }}
            </span>
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
