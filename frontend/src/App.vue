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

/** 正式保存接口的文档、版本与语义 hash 响应。 */
interface PersistResponse {
  job_id: string;
  document: {
    doc_id: string;
    current_doc_version: number;
    title: string;
    language: "zh" | "en" | "mixed" | "unknown";
  };
  version: {
    doc_id: string;
    doc_version: number;
    extraction_hash: string;
  };
  section_tree: PreviewResponse["section_tree"];
  block_set: PreviewResponse["block_set"];
  warnings: string[];
  is_new_document: boolean;
  is_new_version: boolean;
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

const managerStatus = ref<ManagerStatus>("starting");
const connection = ref<BackendConnection | null>(null);
const runtime = ref<RuntimeInfo | null>(null);
const restartCount = ref(0);
const message = ref("正在连接本地后端…");
const sourceKind = ref<SourceKind>("file");
const selectedFilePath = ref("");
const urlValue = ref("");
const preview = ref<PreviewResponse | null>(null);
const persisted = ref<PersistResponse | null>(null);
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

let unlisten: UnlistenFn | undefined;
let healthTimer: number | undefined;
let disposed = false;

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

/** 保存要求预览成功、尚未保存且当前没有保存请求。 */
const canSave = computed(
  () =>
    managerStatus.value === "running" &&
    runtime.value !== null &&
    preview.value !== null &&
    persisted.value === null &&
    !saving.value,
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
  persisted.value = null;
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

/** 把当前预览 job 保存为正式文档版本。 */
async function saveDocument(): Promise<void> {
  if (!connection.value || !preview.value || !canSave.value) {
    return;
  }

  saving.value = true;
  saveError.value = "";
  try {
    const response = await fetch(`${connection.value.baseUrl}/api/ingest/persist`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-launch-token": connection.value.token,
      },
      body: JSON.stringify({ job_id: preview.value.job_id }),
    });
    const body = (await response.json()) as PersistResponse | ApiErrorBody;
    if (!response.ok) {
      const failure = body as ApiErrorBody;
      const code = failure.error?.code ?? "request_failed";
      const detail = failure.error?.message ?? failure.detail ?? `HTTP ${response.status}`;
      throw new Error(ingestErrorMessage(code, detail));
    }
    persisted.value = body as PersistResponse;
  } catch (error) {
    saveError.value = String(error);
  } finally {
    saving.value = false;
  }
}

onMounted(async () => {
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
  unlisten?.();
  if (healthTimer !== undefined) {
    window.clearInterval(healthTimer);
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

    <section class="settings-card">
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

    <section class="import-card">
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

    <template v-if="preview">
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
          <strong>保存到知识库</strong>
          <p v-if="!persisted">
            保存后会分配稳定的文档身份，并记录当前内容版本；相同正文重复保存不会新增版本。
          </p>
          <p v-else class="save-result">
            已保存为 {{ persisted.document.doc_id }} · 版本
            {{ persisted.version.doc_version }}
            · {{ persisted.is_new_document ? "新文档" : persisted.is_new_version ? "新版本" : "已有版本" }}
          </p>
        </div>
        <button
          type="button"
          class="primary-button"
          :disabled="!canSave"
          @click="saveDocument"
        >
          {{ saving ? "正在保存…" : persisted ? "已保存" : "保存到知识库" }}
        </button>
      </section>
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
