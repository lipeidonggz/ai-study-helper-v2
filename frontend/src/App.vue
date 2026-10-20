<script setup lang="ts">
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { open } from "@tauri-apps/plugin-dialog";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

type ManagerStatus =
  | "starting"
  | "running"
  | "restarting"
  | "failed"
  | "stopped";
type SourceKind = "file" | "url";

interface BackendStatus {
  status: ManagerStatus;
  port: number;
  restartCount: number;
  message: string | null;
}

interface BackendConnection {
  baseUrl: string;
  token: string;
}

interface RuntimeInfo {
  status: string;
  version: string;
  pid: number;
  uptime_seconds: number;
}

interface SourceArtifact {
  source_kind: SourceKind;
  source_key: string;
  source_uri: string;
  final_url: string | null;
  media_type: string;
  content_hash: string;
  charset: string;
}

interface SemanticNode {
  ordinal: number;
  kind: string;
  text: string;
  heading_level: number | null;
}

interface ExtractedDocument {
  extraction_hash: string;
  title_candidate: string;
  language: "zh" | "en" | "unknown";
  nodes: SemanticNode[];
  stats: {
    node_count: number;
    character_count: number;
    heading_count: number;
  };
}

interface SectionRecord {
  path: string[];
  title: string;
  depth: number;
  ordinal: string;
  own_node_ordinals: number[];
  first_line: string;
}

interface BlockRecord {
  block_id: string;
  section_path: string[];
  block_index: number;
  text: string;
  context_token_estimate: number;
  source_node_ordinals: number[];
}

interface PreviewResponse {
  job_id: string;
  source: SourceArtifact;
  document: {
    preview_doc_id: string;
    title: string;
    source_label: string;
    language: "zh" | "en" | "unknown";
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

interface ApiErrorBody {
  detail?: string;
  error?: {
    code: string;
    message: string;
    retryable: boolean;
  };
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
const ingestError = ref("");
const ingesting = ref(false);

let unlisten: UnlistenFn | undefined;
let healthTimer: number | undefined;
let disposed = false;

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

const stateClass = computed(() => {
  const visualStatus =
    managerStatus.value === "running" && !runtime.value
      ? "starting"
      : managerStatus.value;
  return `status-${visualStatus}`;
});

const canIngest = computed(
  () =>
    managerStatus.value === "running" &&
    runtime.value !== null &&
    !ingesting.value,
);

const sourceValue = computed(() =>
  sourceKind.value === "file" ? selectedFilePath.value : urlValue.value.trim(),
);

const languageLabel = computed(() => {
  switch (preview.value?.document.language) {
    case "zh":
      return "中文";
    case "en":
      return "English";
    default:
      return "语言未知";
  }
});

function warningLabel(code: string): string {
  const labels: Record<string, string> = {
    source_container_preserved_structures:
      "已保留源文章容器中的表格和图片结构",
    preamble_attached: "首个章节前的内容已归入该章节",
    duplicate_section_title: "存在同名章节，路径已自动编号",
    dns_rebinding_not_pinned: "网页来源未固定 DNS 解析地址",
  };
  return labels[code] ?? "存在一条提取告警";
}

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
  };
  const label = labels[code];
  return label ? `${label}（错误码：${code}）` : `${detail}（错误码：${code}）`;
}

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
    }
  } catch (error) {
    runtime.value = null;
    if (managerStatus.value === "running") {
      message.value = `健康检查失败：${String(error)}`;
    }
  }
}

async function connect(): Promise<void> {
  try {
    connection.value = await invoke<BackendConnection>("backend_connection");
    const status = await invoke<BackendStatus>("backend_status");
    applyStatus(status);
    await checkRuntime();
  } catch (error) {
    managerStatus.value = "failed";
    message.value = `无法读取 Tauri 后端状态：${String(error)}`;
  }
}

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

async function ingest(): Promise<void> {
  if (!connection.value || !sourceValue.value || !canIngest.value) {
    return;
  }

  ingesting.value = true;
  ingestError.value = "";
  preview.value = null;
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
