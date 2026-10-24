<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";

import {
  artifactKindLabel,
  artifactStateLabel,
  replayScopeLabel,
  traceModeLabel,
  traceSpanKindLabel,
  traceSpanStatusLabel,
  traceStatusLabel,
} from "./trace";
import type { TraceArtifact, TraceDetail, TraceSpan } from "./trace";
import TraceArtifactContentViewer from "./TraceArtifactContentViewer.vue";

const props = defineProps<{
  trace: TraceDetail;
  loadArtifact: (
    traceId: string,
    artifact: TraceArtifact,
  ) => Promise<string>;
}>();

const selectedSpanId = ref("");
const activeArtifact = ref<TraceArtifact | null>(null);
const artifactContent = ref("");
const artifactError = ref("");
const artifactIsLoading = ref(false);
let artifactRequestSequence = 0;

/** 按浏览器本地时区格式化 trace 时间。 */
function formatTraceTime(value: string): string {
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

/** 优先使用运行时记录耗时，缺失时从起止时间推算。 */
function spanDuration(span: TraceSpan): string {
  const recorded = Number(span.attributes["app.duration_ms"]);
  const duration = Number.isFinite(recorded)
    ? recorded
    : Math.max(0, new Date(span.ended_at).getTime() - new Date(span.started_at).getTime());
  return `${Math.round(duration)} ms`;
}

/** 根据父 span 计算调用树缩进层级。 */
function spanDepth(span: TraceSpan, byId: Map<string, TraceSpan>): number {
  let current = span;
  let depth = 0;
  const seen = new Set<string>();
  while (current.parent_span_id && !seen.has(current.span_id)) {
    seen.add(current.span_id);
    const parent = byId.get(current.parent_span_id);
    if (!parent) {
      break;
    }
    depth += 1;
    current = parent;
  }
  return depth;
}

/** 展开或收起一个调用步骤。 */
function toggleSpan(spanId: string): void {
  selectedSpanId.value =
    selectedSpanId.value === spanId ? "" : spanId;
}

const spanRows = computed(() => {
  const byId = new Map(props.trace.spans.map((span) => [span.span_id, span]));
  return props.trace.spans.map((span) => ({
    span,
    depth: spanDepth(span, byId),
  }));
});

/** 把工具调用 span 转成调用树标题中的工具名与参数摘要。 */
function toolCallSummary(
  span: TraceSpan,
): { label: string; title: string } | null {
  if (span.kind !== "TOOL" || span.name !== "tool.call") {
    return null;
  }
  const toolName =
    typeof span.attributes["tool.name"] === "string"
      ? span.attributes["tool.name"]
      : "unknown";
  const argumentsValue = span.attributes["app.tool_arguments"];
  if (!argumentsValue || typeof argumentsValue !== "object") {
    return {
      label: `${toolName} · 参数未记录`,
      title: `${toolName}\n当前 trace 模式未记录参数原文。`,
    };
  }
  try {
    const serialized = JSON.stringify(argumentsValue);
    return {
      label: `${toolName} ${serialized}`,
      title: `${toolName}\n${JSON.stringify(argumentsValue, null, 2)}`,
    };
  } catch {
    return {
      label: `${toolName} · 参数无法展开`,
      title: `${toolName}\n参数对象无法序列化。`,
    };
  }
}

const artifactById = computed(
  () =>
    new Map(
      props.trace.manifest.artifacts.map((artifact) => [
        artifact.artifact_id,
        artifact,
      ]),
    ),
);

const artifactByPath = computed(
  () =>
    new Map(
      props.trace.manifest.artifacts
        .filter((artifact) => artifact.relative_path)
        .map((artifact) => [artifact.relative_path as string, artifact]),
    ),
);

const visibleArtifacts = computed(() =>
  props.trace.manifest.artifacts.filter(
    (artifact) => artifact.state !== "omitted",
  ),
);

const omittedArtifactCount = computed(
  () =>
    props.trace.manifest.artifacts.filter(
      (artifact) => artifact.state === "omitted",
    ).length,
);

/** 从 span 属性和事件中收集可关联到 manifest 的 artifact 引用。 */
function artifactsForSpan(span: TraceSpan): TraceArtifact[] {
  const found = new Map<string, TraceArtifact>();
  const visit = (value: unknown, key = ""): void => {
    if (typeof value === "string") {
      const artifact =
        key.endsWith("artifact_id")
          ? artifactById.value.get(value)
          : key.endsWith("relative_path")
            ? artifactByPath.value.get(value)
            : undefined;
      if (artifact) {
        found.set(artifact.artifact_id, artifact);
      }
      return;
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        visit(item, key);
      }
      return;
    }
    if (value && typeof value === "object") {
      for (const [childKey, childValue] of Object.entries(value)) {
        visit(childValue, childKey);
      }
    }
  };
  visit(span.attributes);
  for (const event of span.events) {
    visit(event.attributes);
  }
  return [...found.values()];
}

/** 打开 artifact 弹框并按需读取内容。 */
async function openArtifactModal(artifact: TraceArtifact): Promise<void> {
  const requestSequence = ++artifactRequestSequence;
  activeArtifact.value = artifact;
  artifactContent.value = "";
  artifactError.value = "";
  artifactIsLoading.value = true;
  try {
    const content = await props.loadArtifact(props.trace.manifest.trace_id, artifact);
    if (requestSequence !== artifactRequestSequence) {
      return;
    }
    artifactContent.value = content;
  } catch (error) {
    if (requestSequence !== artifactRequestSequence) {
      return;
    }
    artifactError.value = `读取运行内容失败：${String(error)}`;
  } finally {
    if (requestSequence === artifactRequestSequence) {
      artifactIsLoading.value = false;
    }
  }
}

/** 关闭 artifact 弹框并让未完成的旧请求失效。 */
function closeArtifactModal(): void {
  artifactRequestSequence += 1;
  activeArtifact.value = null;
  artifactContent.value = "";
  artifactError.value = "";
  artifactIsLoading.value = false;
}

/** 弹框打开时支持 Escape 关闭。 */
function handleArtifactModalKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape" && activeArtifact.value) {
    closeArtifactModal();
  }
}

watch(
  () => activeArtifact.value?.artifact_id,
  (artifactId) => {
    window.removeEventListener("keydown", handleArtifactModalKeydown);
    if (artifactId) {
      window.addEventListener("keydown", handleArtifactModalKeydown);
    }
  },
);

watch(
  () => props.trace.manifest.trace_id,
  () => {
    selectedSpanId.value = "";
    closeArtifactModal();
  },
);

onBeforeUnmount(() => {
  window.removeEventListener("keydown", handleArtifactModalKeydown);
});
</script>

<template>
  <article class="trace-detail-panel">
    <div class="trace-detail-header">
      <div>
        <p class="trace-eyebrow">
          运行轨迹 · {{ trace.manifest.trace_id.slice(-8) }}
        </p>
        <h3>{{ trace.manifest.question_preview || "未保存问题正文" }}</h3>
      </div>
      <span
        class="trace-health-badge"
        :class="`trace-health-${trace.manifest.trace_status}`"
      >
        {{ traceStatusLabel(trace.manifest.trace_status) }}
      </span>
    </div>

    <dl class="trace-summary-grid">
      <div>
        <dt>最终状态</dt>
        <dd>{{ traceStatusLabel(trace.manifest.status) }}</dd>
      </div>
      <div>
        <dt>终止原因</dt>
        <dd>{{ trace.manifest.termination_reason }}</dd>
      </div>
      <div>
        <dt>耗时</dt>
        <dd>{{ trace.manifest.duration_ms }} ms</dd>
      </div>
      <div>
        <dt>轮次 / 工具 / 模型</dt>
        <dd>
          {{ trace.manifest.agent_turns }} /
          {{ trace.manifest.tool_calls }} /
          {{ trace.manifest.model_calls }}
        </dd>
      </div>
      <div>
        <dt>输入 token</dt>
        <dd>{{ trace.manifest.input_tokens }}</dd>
      </div>
      <div>
        <dt>输出 token</dt>
        <dd>{{ trace.manifest.output_tokens }}</dd>
      </div>
      <div>
        <dt>记录模式</dt>
        <dd>{{ traceModeLabel(trace.manifest.mode) }}</dd>
      </div>
      <div>
        <dt>可重放范围</dt>
        <dd>{{ replayScopeLabel(trace.manifest.replay_scope) }}</dd>
      </div>
    </dl>

    <details class="trace-metadata-block">
      <summary>版本与运行配置</summary>
      <pre>{{ JSON.stringify(trace.manifest.versions, null, 2) }}</pre>
      <pre>{{ JSON.stringify(trace.manifest.budget, null, 2) }}</pre>
    </details>

    <section class="trace-timeline">
      <div class="trace-section-heading">
        <div>
          <p class="trace-eyebrow">调用时间线</p>
          <h3>调用树</h3>
        </div>
        <span>{{ trace.spans.length }} 个步骤</span>
      </div>
      <p v-if="!spanRows.length" class="trace-empty">本次运行没有记录调用步骤。</p>
      <article
        v-for="row in spanRows"
        :key="row.span.span_id"
        class="trace-span-card"
        :class="[
          `trace-depth-${row.depth % 5}`,
          {
            selected: selectedSpanId === row.span.span_id,
            'trace-span-error': row.span.status === 'error',
          },
        ]"
        :style="{
          marginLeft: `${row.depth * 18}px`,
          width: `calc(100% - ${row.depth * 18}px)`,
        }"
      >
        <button
          type="button"
          class="trace-span-toggle"
          @click="toggleSpan(row.span.span_id)"
        >
          <span class="trace-span-topline">
            <span class="trace-span-kind">
              {{ traceSpanKindLabel(row.span.kind) }}
            </span>
            <strong>{{ row.span.name }}</strong>
            <span
              v-if="toolCallSummary(row.span)"
              class="trace-tool-call-summary"
              :title="toolCallSummary(row.span)?.title"
            >
              {{ toolCallSummary(row.span)?.label }}
            </span>
            <span>{{ traceSpanStatusLabel(row.span) }}</span>
          </span>
          <small>
            {{ formatTraceTime(row.span.started_at) }} ·
            耗时 {{ spanDuration(row.span) }}
          </small>
        </button>
        <span
          v-if="selectedSpanId === row.span.span_id"
          class="trace-span-detail"
        >
          <span
            v-if="artifactsForSpan(row.span).length"
            class="trace-span-artifacts"
          >
            <strong>关联运行内容</strong>
            <button
              v-for="artifact in artifactsForSpan(row.span)"
              :key="artifact.artifact_id"
              type="button"
              class="trace-artifact-link"
              @click.stop="openArtifactModal(artifact)"
            >
              <span>{{ artifactKindLabel(artifact.kind) }}</span>
              <code>{{ artifact.relative_path ?? artifact.artifact_id }}</code>
            </button>
          </span>
          <span class="trace-detail-label">技术属性</span>
          <pre>{{ JSON.stringify(row.span.attributes, null, 2) }}</pre>
          <span v-if="row.span.events.length" class="trace-span-events">
            <strong>事件 · {{ row.span.events.length }}</strong>
            <pre>{{ JSON.stringify(row.span.events, null, 2) }}</pre>
          </span>
        </span>
      </article>
    </section>

    <section v-if="trace.manifest.artifacts.length" class="trace-artifacts">
      <div class="trace-section-heading">
        <div>
          <p class="trace-eyebrow">文件与内容</p>
          <h3>运行内容</h3>
        </div>
        <span>{{ visibleArtifacts.length }} 个可查看</span>
      </div>
      <p v-if="omittedArtifactCount" class="trace-artifacts-omitted">
        {{ traceModeLabel(trace.manifest.mode) }}模式已省略
        {{ omittedArtifactCount }} 个正文内容，仅保留内容哈希。
      </p>
      <button
        v-for="artifact in visibleArtifacts"
        :key="artifact.artifact_id"
        type="button"
        class="trace-artifact-item"
        @click="openArtifactModal(artifact)"
      >
        <span class="trace-artifact-heading">
          <strong :title="artifact.kind">{{ artifactKindLabel(artifact.kind) }}</strong>
          <small>{{ artifact.relative_path ?? artifact.artifact_id }}</small>
          <small>
            {{ artifact.mime_type }} · {{ artifact.size_bytes }} bytes
          </small>
        </span>
        <span class="trace-artifact-state">
          {{ artifactStateLabel(artifact.state) }}
        </span>
      </button>
    </section>

    <Teleport to="body">
      <div
        v-if="activeArtifact"
        class="trace-modal-backdrop"
        role="presentation"
        @click.self="closeArtifactModal"
      >
        <section
          class="trace-modal"
          role="dialog"
          aria-modal="true"
          aria-labelledby="trace-artifact-modal-title"
        >
          <header class="trace-modal-header">
            <div>
              <p class="trace-eyebrow">运行内容</p>
              <h3 id="trace-artifact-modal-title">
                {{ artifactKindLabel(activeArtifact.kind) }}
              </h3>
            </div>
            <button
              type="button"
              class="trace-modal-close"
              aria-label="关闭运行内容弹框"
              @click="closeArtifactModal"
            >
              关闭
            </button>
          </header>
          <dl class="trace-artifact-meta">
            <div>
              <dt>artifact_id</dt>
              <dd>{{ activeArtifact.artifact_id }}</dd>
            </div>
            <div>
              <dt>relative_path</dt>
              <dd>{{ activeArtifact.relative_path ?? "未保存路径" }}</dd>
            </div>
            <div>
              <dt>类型 / 大小</dt>
              <dd>
                {{ activeArtifact.mime_type }} ·
                {{ activeArtifact.size_bytes }} bytes
              </dd>
            </div>
          </dl>
          <TraceArtifactContentViewer
            :artifact-kind="activeArtifact.kind"
            :content="artifactContent"
            :error="artifactError"
            :is-loading="artifactIsLoading"
          />
        </section>
      </div>
    </Teleport>
  </article>
</template>

<style scoped>
.trace-detail-panel {
  min-width: 0;
  border: 1px solid rgba(37, 71, 50, 0.11);
  border-radius: 14px;
  padding: 16px;
  background: #f7faf6;
}

.trace-detail-header,
.trace-section-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.trace-detail-header h3 {
  margin: 5px 0 0;
  color: #24402d;
  font-size: 1.05rem;
}

.trace-eyebrow {
  margin: 0;
  color: #7a877d;
  font-size: 0.64rem;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.trace-health-badge,
.trace-list-health {
  width: fit-content;
  border-radius: 999px;
  padding: 3px 7px;
  font-size: 0.66rem;
}

.trace-health-complete {
  color: #24623c;
  background: #ddf0e1;
}

.trace-health-partial {
  color: #7c5b12;
  background: #fff0c9;
}

.trace-health-dropped {
  color: #8c302d;
  background: #ffe5e3;
}

.trace-summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  margin: 16px 0;
}

.trace-summary-grid > div {
  min-width: 0;
  border-radius: 9px;
  padding: 8px 9px;
  background: #eef3ec;
}

.trace-summary-grid dt {
  color: #78857b;
  font-size: 0.65rem;
}

.trace-summary-grid dd {
  overflow: hidden;
  margin: 3px 0 0;
  color: #2b4834;
  font-family: Consolas, monospace;
  font-size: 0.74rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trace-metadata-block {
  border: 1px solid rgba(37, 71, 50, 0.1);
  border-radius: 10px;
  padding: 10px 12px;
  background: #f1f5ef;
}

.trace-metadata-block summary {
  color: #3d5c47;
  cursor: pointer;
  font-size: 0.78rem;
  font-weight: 700;
}

.trace-metadata-block pre,
.trace-span-detail pre {
  max-height: 360px;
  overflow: auto;
  border-radius: 9px;
  padding: 10px;
  color: #2c4033;
  background: #fff;
  font-size: 0.7rem;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
}

.trace-timeline,
.trace-artifacts {
  margin-top: 20px;
  border-top: 1px solid rgba(37, 71, 50, 0.1);
  padding-top: 14px;
}

.trace-section-heading {
  align-items: center;
}

.trace-section-heading h3 {
  margin: 3px 0 0;
  color: #294532;
  font-size: 0.9rem;
}

.trace-section-heading > span {
  color: #78857b;
  font-size: 0.68rem;
}

.trace-span-card {
  display: block;
  box-sizing: border-box;
  width: 100%;
  margin-top: 7px;
  border: 1px solid rgba(37, 71, 50, 0.11);
  border-left: 3px solid var(--trace-accent, #7ca189);
  border-radius: 10px;
  color: #294532;
  background: #fff;
}

.trace-span-toggle {
  display: block;
  width: 100%;
  border: 0;
  padding: 9px 11px;
  color: inherit;
  background: transparent;
  cursor: pointer;
  text-align: left;
}

.trace-span-toggle:focus-visible {
  outline: 2px solid var(--trace-accent, #4c8963);
  outline-offset: -3px;
}

.trace-depth-0 {
  --trace-accent: #3f7c5a;
  --trace-accent-bg: #e4f1e8;
}

.trace-depth-1 {
  --trace-accent: #3974a5;
  --trace-accent-bg: #e4eef7;
}

.trace-depth-2 {
  --trace-accent: #a06a2b;
  --trace-accent-bg: #f7ecdc;
}

.trace-depth-3 {
  --trace-accent: #7a55a3;
  --trace-accent-bg: #eee7f6;
}

.trace-depth-4 {
  --trace-accent: #4b7f86;
  --trace-accent-bg: #e3f0f1;
}

.trace-span-card.selected {
  border-top-color: #4c8963;
  border-right-color: #4c8963;
  border-bottom-color: #4c8963;
  background: #f0f7ef;
}

.trace-span-card.trace-span-error {
  border-left-color: #b4453e !important;
}

.trace-span-topline {
  display: flex;
  align-items: center;
  gap: 8px;
}

.trace-span-topline strong {
  color: #294532;
  font-size: 0.78rem;
}

.trace-tool-call-summary {
  min-width: 0;
  overflow: hidden;
  color: #536b5b;
  font-family: Consolas, "Courier New", monospace;
  font-size: 0.69rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trace-span-topline > span:last-child {
  margin-left: auto;
  color: #6e7d73;
  font-size: 0.68rem;
}

.trace-span-kind {
  border-radius: 5px;
  padding: 2px 5px;
  color: var(--trace-accent, #4d6655);
  background: var(--trace-accent-bg, #e6eee4);
  font-size: 0.61rem;
}

.trace-span-toggle > small {
  display: block;
  margin-top: 5px;
  color: #7c8980;
  font-size: 0.67rem;
}

.trace-span-detail,
.trace-span-events {
  display: block;
  margin-top: 8px;
}

.trace-span-detail {
  padding: 0 11px 10px;
}

.trace-detail-label {
  display: block;
  margin-bottom: 5px;
  color: #52675a;
  font-size: 0.7rem;
  font-weight: 700;
}

.trace-span-events > strong {
  color: #52675a;
  font-size: 0.72rem;
}

.trace-span-artifacts {
  display: grid;
  gap: 5px;
  margin-bottom: 9px;
  border-radius: 8px;
  padding: 8px;
  background: #f4f7f3;
}

.trace-span-artifacts > strong {
  color: #52675a;
  font-size: 0.7rem;
}

.trace-artifact-link {
  display: grid;
  gap: 2px;
  width: 100%;
  border: 0;
  border-radius: 6px;
  padding: 6px 8px;
  color: #275b3b;
  background: #fff;
  cursor: pointer;
  text-align: left;
}

.trace-artifact-link:hover,
.trace-artifact-link:focus-visible {
  background: #e8f2e9;
}

.trace-artifact-link span {
  font-size: 0.7rem;
  font-weight: 700;
}

.trace-artifact-link code {
  overflow: hidden;
  color: #69796f;
  font-size: 0.64rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trace-artifact-item {
  display: flex;
  box-sizing: border-box;
  width: 100%;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 7px;
  border: 1px solid rgba(37, 71, 50, 0.1);
  border-radius: 9px;
  padding: 9px 11px;
  color: #294532;
  background: #fff;
  cursor: pointer;
  text-align: left;
}

.trace-artifact-heading {
  display: grid;
  gap: 2px;
}

.trace-artifact-heading strong {
  font-size: 0.76rem;
}

.trace-artifact-heading small,
.trace-artifact-state {
  color: #78857b;
  font-size: 0.67rem;
}

.trace-artifacts-omitted,
.trace-empty {
  margin: 8px 0;
  border-radius: 9px;
  padding: 9px 11px;
  font-size: 0.74rem;
  line-height: 1.55;
}

.trace-artifacts-omitted {
  color: #75601f;
  background: #fff4d8;
}

.trace-empty {
  color: #75847a;
  background: #eef3ec;
}

.trace-modal-backdrop {
  position: fixed;
  z-index: 1000;
  inset: 0;
  display: grid;
  place-items: center;
  padding: 28px;
  background: rgba(20, 35, 25, 0.5);
  backdrop-filter: blur(2px);
}

.trace-modal {
  display: grid;
  width: min(900px, 100%);
  max-height: min(82vh, 760px);
  overflow: auto;
  gap: 14px;
  border: 1px solid rgba(37, 71, 50, 0.18);
  border-radius: 16px;
  padding: 18px;
  color: #294532;
  background: #f9fcf8;
  box-shadow: 0 24px 70px rgba(20, 42, 27, 0.28);
}

.trace-modal-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.trace-modal-header h3 {
  margin: 4px 0 0;
  color: #24402d;
  font-size: 1rem;
}

.trace-modal-close {
  border: 1px solid #b8cbbd;
  border-radius: 8px;
  padding: 6px 10px;
  color: #284b34;
  background: #fff;
  cursor: pointer;
  font: inherit;
  font-size: 0.72rem;
}

.trace-artifact-meta {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin: 0;
}

.trace-artifact-meta > div {
  min-width: 0;
  border-radius: 8px;
  padding: 8px 9px;
  background: #eef3ec;
}

.trace-artifact-meta dt {
  color: #78857b;
  font-size: 0.64rem;
}

.trace-artifact-meta dd {
  overflow-wrap: anywhere;
  margin: 3px 0 0;
  color: #2b4834;
  font-family: Consolas, monospace;
  font-size: 0.68rem;
}

@media (max-width: 900px) {
  .trace-summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .trace-artifact-meta {
    grid-template-columns: 1fr;
  }

  .trace-modal-backdrop {
    padding: 14px;
  }
}
</style>
