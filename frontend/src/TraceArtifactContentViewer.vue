<script setup lang="ts">
import { computed, ref, watch } from "vue";

import {
  parseHistoryEntries,
  parseJudgeInput,
  parseJudgeOutput,
  parseModelOutput,
  parsePromptMessages,
  parseUserPrompt,
  textStats,
  type PromptMessageView,
} from "./trace-artifact";

const props = defineProps<{
  artifactKind: string;
  content: string;
  error: string;
  isLoading: boolean;
}>();

const viewMode = ref<"structured" | "raw">("structured");
const copiedKey = ref("");

const promptMessages = computed(() =>
  props.artifactKind === "llm.prompt"
    ? parsePromptMessages(props.content)
    : null,
);

const modelOutput = computed(() =>
  props.artifactKind === "llm.output"
    ? parseModelOutput(props.content)
    : null,
);

const judgeInput = computed(() =>
  props.artifactKind === "judge.input"
    ? parseJudgeInput(props.content)
    : null,
);

const judgeOutput = computed(() =>
  props.artifactKind === "judge.output"
    ? parseJudgeOutput(props.content)
    : null,
);

const structuredAvailable = computed(
  () =>
    promptMessages.value !== null ||
    modelOutput.value !== null ||
    judgeInput.value !== null ||
    judgeOutput.value !== null,
);

watch(
  () => [props.artifactKind, props.content] as const,
  () => {
    viewMode.value = structuredAvailable.value ? "structured" : "raw";
  },
  { immediate: true },
);

/** 把模型消息角色转换成界面名称。 */
function roleLabel(role: string): string {
  const labels: Record<string, string> = {
    system: "系统规则",
    user: "本轮输入",
    assistant: "模型输出",
  };
  return labels[role] ?? role;
}

/** 把判官三态结论转换成中文。 */
function judgeVerdictLabel(verdict: string): string {
  const labels: Record<string, string> = {
    satisfied: "满足",
    unsatisfied: "未满足",
    violated: "违规",
    not_violated: "未违规",
    unknown: "不确定",
  };
  return labels[verdict] ?? verdict;
}

/** 返回用户消息中拆分后的题目和历史；无法识别时由模板回退原文。 */
function userPrompt(message: PromptMessageView): ReturnType<typeof parseUserPrompt> {
  return message.role === "user" ? parseUserPrompt(message.content) : null;
}

/** 复制文本并短暂显示复制状态。 */
async function copyText(content: string, key: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(content);
    copiedKey.value = key;
    window.setTimeout(() => {
      if (copiedKey.value === key) {
        copiedKey.value = "";
      }
    }, 1_500);
  } catch {
    copiedKey.value = `${key}:failed`;
  }
}

/** 显示复制按钮当前状态。 */
function copyLabel(key: string): string {
  if (copiedKey.value === key) {
    return "已复制";
  }
  if (copiedKey.value === `${key}:failed`) {
    return "复制失败";
  }
  return "复制";
}
</script>

<template>
  <div class="artifact-content-viewer">
    <div v-if="!isLoading && !error" class="artifact-viewer-toolbar">
      <div
        v-if="structuredAvailable"
        class="artifact-view-tabs"
        role="tablist"
        aria-label="运行内容查看方式"
      >
        <button
          type="button"
          role="tab"
          :aria-selected="viewMode === 'structured'"
          :class="{ active: viewMode === 'structured' }"
          @click="viewMode = 'structured'"
        >
          结构化
        </button>
        <button
          type="button"
          role="tab"
          :aria-selected="viewMode === 'raw'"
          :class="{ active: viewMode === 'raw' }"
          @click="viewMode = 'raw'"
        >
          原始内容
        </button>
      </div>
      <button
        type="button"
        class="artifact-copy-button"
        @click="copyText(content, 'all')"
      >
        {{ copyLabel("all") }}
      </button>
    </div>

    <p v-if="isLoading" class="artifact-status">正在读取运行内容…</p>
    <p v-else-if="error" class="artifact-error">{{ error }}</p>

    <template v-else-if="viewMode === 'raw' || !structuredAvailable">
      <p v-if="!structuredAvailable" class="artifact-fallback">
        当前内容无法解析为预期结构，已显示原文。
      </p>
      <pre class="artifact-content">{{ content }}</pre>
    </template>

    <div v-else-if="promptMessages" class="prompt-view">
      <details
        v-for="(message, index) in promptMessages"
        :key="`${message.role}-${index}`"
        class="prompt-message"
        :open="message.role !== 'system'"
      >
        <summary>
          <strong>{{ roleLabel(message.role) }}</strong>
          <span>{{ textStats(message.content) }}</span>
          <button
            type="button"
            @click.prevent.stop="
              copyText(message.content, `message-${index}`)
            "
          >
            {{ copyLabel(`message-${index}`) }}
          </button>
        </summary>
        <div
          v-if="userPrompt(message)"
          class="prompt-user-sections"
        >
          <section>
            <h4>用户问题</h4>
            <pre>{{ userPrompt(message)?.question }}</pre>
          </section>
          <section>
            <h4>运行历史</h4>
            <p
              v-if="!parseHistoryEntries(userPrompt(message)?.history ?? '').length"
              class="artifact-muted"
            >
              暂无历史。
            </p>
            <details
              v-for="(entry, entryIndex) in parseHistoryEntries(
                userPrompt(message)?.history ?? '',
              )"
              :key="entryIndex"
              class="history-entry"
            >
              <summary>
                {{
                  entry.kind === "assistant"
                    ? "Assistant 输出"
                    : entry.kind === "tool_result"
                      ? "Harness 工具结果"
                      : entry.kind === "protocol_error"
                        ? "Harness 协议错误"
                        : entry.kind === "observation"
                          ? "旧版工具 Observation"
                          : "其他历史内容"
                }}
              </summary>
              <template v-if="entry.output">
                <section
                  v-if="entry.output.decisionSummary"
                  class="output-section"
                >
                  <h4>决策摘要</h4>
                  <pre>{{ entry.output.decisionSummary }}</pre>
                </section>
                <section
                  v-if="entry.output.kind === 'finish'"
                  class="output-section"
                >
                  <h4>完成回答</h4>
                  <pre>{{ entry.output.answer }}</pre>
                </section>
                <section v-else class="output-section">
                  <h4>工具调用 · {{ entry.output.toolName }}</h4>
                  <pre>{{ entry.output.argumentsText }}</pre>
                </section>
              </template>
              <pre v-else>{{ entry.payloadText ?? entry.content }}</pre>
            </details>
          </section>
        </div>
        <pre v-else>{{ message.content }}</pre>
      </details>
    </div>

    <div v-else-if="judgeInput" class="judge-input-view">
      <div class="judge-input-meta">
        <span>提示词版本</span>
        <code>{{ judgeInput.promptVersion }}</code>
      </div>
      <details
        v-for="(message, index) in judgeInput.messages.filter(
          (item) => item.role === 'system',
        )"
        :key="index"
        class="prompt-message"
      >
        <summary>
          <strong>{{ roleLabel(message.role) }}</strong>
          <span>{{ textStats(message.content) }}</span>
          <button
            type="button"
            @click.prevent.stop="
              copyText(message.content, `judge-system-${index}`)
            "
          >
            {{ copyLabel(`judge-system-${index}`) }}
          </button>
        </summary>
        <pre>{{ message.content }}</pre>
      </details>

      <template v-if="judgeInput.packet">
        <section class="judge-packet-section">
          <h4>Agent 答案</h4>
          <pre>{{ judgeInput.packet.answer }}</pre>
        </section>
        <section class="judge-criterion">
          <div>
            <span>{{ judgeInput.packet.criterion.ruleType }}</span>
            <strong>当前判定项</strong>
          </div>
          <p>{{ judgeInput.packet.criterion.ruleText }}</p>
        </section>
      </template>

      <div
        v-else
        class="judge-message-fallback"
      >
        <details
          v-for="(message, index) in judgeInput.messages.filter(
            (item) => item.role !== 'system',
          )"
          :key="`payload-${index}`"
        >
          <summary>{{ roleLabel(message.role) }}</summary>
          <pre>{{ message.content }}</pre>
        </details>
      </div>
    </div>

    <div v-else-if="judgeOutput" class="judge-output-view">
      <div class="judge-verdict">
        <span>判定结论</span>
        <strong>{{ judgeVerdictLabel(judgeOutput.verdict) }}</strong>
      </div>
      <section
        v-if="modelOutput && modelOutput.decisionSummary"
        class="output-section"
      >
        <h4>判定原因</h4>
        <pre>{{ judgeOutput.reason }}</pre>
      </section>
    </div>

    <div v-else-if="modelOutput" class="model-output-view">
      <section class="output-section">
        <h4>决策摘要</h4>
        <pre>{{ modelOutput.decisionSummary }}</pre>
      </section>
      <section
        v-if="modelOutput.kind === 'finish'"
        class="output-section output-finish"
      >
        <h4>最终回答</h4>
        <pre>{{ modelOutput.answer }}</pre>
      </section>
      <section v-else class="output-section output-tool">
        <h4>工具调用 · {{ modelOutput.toolName }}</h4>
        <pre>{{ modelOutput.argumentsText }}</pre>
      </section>
    </div>
  </div>
</template>

<style scoped>
.artifact-content-viewer {
  display: grid;
  min-width: 0;
  gap: 10px;
}

.artifact-viewer-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.artifact-view-tabs {
  display: flex;
  gap: 4px;
  border-radius: 8px;
  padding: 3px;
  background: #eaf0e8;
}

.artifact-view-tabs button,
.artifact-copy-button {
  border: 0;
  border-radius: 6px;
  padding: 6px 9px;
  color: #53665a;
  background: transparent;
  cursor: pointer;
  font: inherit;
  font-size: 0.7rem;
}

.artifact-view-tabs button.active {
  color: #244c32;
  background: #fff;
  font-weight: 700;
  box-shadow: 0 1px 4px rgba(37, 71, 50, 0.12);
}

.artifact-copy-button {
  border: 1px solid #b8cbbd;
  background: #fff;
}

.artifact-content,
.prompt-message pre,
.output-section pre,
.history-entry pre,
.judge-packet-section pre,
.judge-message-fallback pre {
  max-height: 420px;
  overflow: auto;
  margin: 0;
  border-radius: 9px;
  padding: 11px;
  color: #2c4033;
  background: #fff;
  font-size: 0.7rem;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.artifact-status,
.artifact-fallback,
.artifact-error,
.artifact-muted {
  margin: 0;
  border-radius: 8px;
  padding: 9px 10px;
  font-size: 0.72rem;
  line-height: 1.55;
}

.artifact-status,
.artifact-muted {
  color: #66776c;
  background: #eef3ec;
}

.artifact-fallback {
  color: #75601f;
  background: #fff4d8;
}

.artifact-error {
  color: #8b3434;
  background: #fff1f1;
}

.prompt-view,
.judge-input-view,
.judge-output-view,
.model-output-view {
  display: grid;
  gap: 9px;
}

.prompt-message {
  overflow: hidden;
  border: 1px solid #dce6de;
  border-radius: 10px;
  background: #f7faf6;
}

.prompt-message > summary {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 9px 10px;
  cursor: pointer;
}

.prompt-message > summary strong {
  color: #294532;
  font-size: 0.76rem;
}

.prompt-message > summary span {
  color: #7a877d;
  font-size: 0.66rem;
}

.prompt-message > summary button {
  margin-left: auto;
  border: 0;
  padding: 0;
  color: #2b6b43;
  background: transparent;
  cursor: pointer;
  font: inherit;
  font-size: 0.68rem;
  text-decoration: underline;
}

.prompt-message > pre,
.prompt-user-sections,
.model-output-view {
  border-top: 1px solid #e1e9e2;
  padding: 10px;
}

.prompt-user-sections {
  display: grid;
  gap: 10px;
}

.prompt-user-sections section,
.output-section {
  display: grid;
  gap: 5px;
}

.prompt-user-sections h4,
.output-section h4 {
  margin: 0;
  color: #52675a;
  font-size: 0.7rem;
}

.judge-input-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 14px;
  border-radius: 8px;
  padding: 8px 10px;
  color: #64756a;
  background: #eef3ec;
  font-size: 0.68rem;
}

.judge-input-meta code {
  color: #294532;
}

.judge-packet-section,
.judge-criterion {
  display: grid;
  gap: 6px;
  border-radius: 9px;
  padding: 10px;
  background: #f4f7f3;
}

.judge-packet-section h4 {
  margin: 0;
  color: #52675a;
  font-size: 0.7rem;
}

.judge-criterion {
  border-left: 3px solid #4d8a62;
  background: #edf5ec;
}

.judge-criterion > div {
  display: flex;
  align-items: center;
  gap: 8px;
}

.judge-criterion > div span {
  border-radius: 5px;
  padding: 2px 5px;
  color: #fff;
  background: #4d8a62;
  font-size: 0.62rem;
  font-weight: 700;
}

.judge-criterion > div strong {
  color: #294532;
  font-size: 0.76rem;
}

.judge-criterion p {
  margin: 0;
  color: #52675a;
  font-size: 0.72rem;
  line-height: 1.55;
}

.judge-message-fallback {
  display: grid;
  gap: 7px;
}

.judge-message-fallback details {
  border: 1px solid #dfe8e0;
  border-radius: 8px;
  background: #f7faf6;
}

.judge-message-fallback summary {
  padding: 8px 9px;
  color: #52675a;
  cursor: pointer;
  font-size: 0.7rem;
  font-weight: 700;
}

.judge-message-fallback pre {
  margin: 0 9px 9px;
}

.judge-verdict {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  border-radius: 9px;
  padding: 10px 12px;
  color: #52675a;
  background: #edf5ec;
  font-size: 0.72rem;
}

.judge-verdict strong {
  color: #285f3b;
  font-size: 0.9rem;
}

.history-entry {
  border: 1px solid #e0e8e1;
  border-radius: 8px;
  background: #fff;
}

.history-entry > summary {
  padding: 7px 9px;
  color: #52675a;
  cursor: pointer;
  font-size: 0.68rem;
  font-weight: 700;
}

.history-entry > pre,
.history-entry > .output-section {
  margin: 0 9px 9px;
}

.history-entry .output-section + .output-section {
  margin-top: 8px;
}

.output-tool {
  border-left: 3px solid #4b88aa;
  padding-left: 9px;
}

.output-finish {
  border-left: 3px solid #4d8a62;
  padding-left: 9px;
}
</style>
