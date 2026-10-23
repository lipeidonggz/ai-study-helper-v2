/** 运行时 trace 的记录级别。 */
export type TraceMode = "off" | "metadata" | "full";

/** trace 内按内容寻址的 artifact 引用。 */
export interface TraceArtifact {
  artifact_id: string;
  kind: string;
  mime_type: string;
  size_bytes: number;
  content_hash: string;
  state: "stored" | "omitted" | "missing";
  relative_path: string | null;
}

/** 挂在 trace span 上的确定性事件。 */
export interface TraceSpanEvent {
  name: string;
  time: string;
  attributes: Record<string, unknown>;
}

/** 本地 OTel 风格 trace span。 */
export interface TraceSpan {
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  name: string;
  kind: "INTERNAL" | "LLM" | "TOOL";
  start_time_unix_nano: number;
  end_time_unix_nano: number;
  started_at: string;
  ended_at: string;
  status: "ok" | "error";
  error_code: string | null;
  attributes: Record<string, unknown>;
  events: TraceSpanEvent[];
}

/** 一次运行 trace 的最终摘要。 */
export interface TraceManifest {
  schema_version: string;
  trace_id: string;
  mode: TraceMode;
  app_version: string;
  started_at: string;
  ended_at: string;
  duration_ms: number;
  status: string;
  termination_reason: string;
  answer_completeness: "complete" | "partial";
  question_preview: string | null;
  question_artifact: TraceArtifact | null;
  snapshot_id: string | null;
  snapshot_status: string | null;
  completeness: string | null;
  agent_turns: number;
  tool_calls: number;
  model_calls: number;
  retry_count: number;
  input_tokens: number;
  output_tokens: number;
  budget: Record<string, unknown>;
  versions: Record<string, unknown>;
  artifacts: TraceArtifact[];
  trace_status: "complete" | "partial" | "dropped";
  dropped_events: number;
  replay_scope: "metadata" | "evidence" | "retrieval";
  limitations: Array<{ code: string; message: string }>;
}

/** 诊断列表中的一次 trace 摘要。 */
export interface TraceListItem {
  trace_id: string;
  mode: TraceMode;
  started_at: string;
  ended_at: string;
  status: string;
  termination_reason: string;
  question_preview: string | null;
  duration_ms: number;
  input_tokens: number;
  output_tokens: number;
  trace_status: "complete" | "partial" | "dropped";
  replay_scope: "metadata" | "evidence" | "retrieval";
}

/** trace 详情接口返回的 manifest 与 span。 */
export interface TraceDetail {
  manifest: TraceManifest;
  spans: TraceSpan[];
}

/** 把 trace 和运行状态转换成统一的中文标签。 */
export function traceStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    succeeded: "成功",
    partial: "部分完成",
    failed: "失败",
    canceled: "已取消",
    complete: "完整",
    dropped: "已丢失",
  };
  return labels[status] ?? status;
}

/** 把 span 状态转换成统一的中文标签。 */
export function traceSpanStatusLabel(span: TraceSpan): string {
  return span.status === "ok"
    ? "成功"
    : `错误${span.error_code ? `（${span.error_code}）` : ""}`;
}

/** 把 trace 记录模式转换成统一的中文标签。 */
export function traceModeLabel(mode: string): string {
  const labels: Record<string, string> = {
    off: "关闭",
    metadata: "仅元数据",
    full: "完整内容",
  };
  return labels[mode] ?? mode;
}

/** 把 span 类型转换成统一的中文标签。 */
export function traceSpanKindLabel(kind: TraceSpan["kind"]): string {
  const labels: Record<TraceSpan["kind"], string> = {
    INTERNAL: "内部步骤",
    LLM: "模型调用",
    TOOL: "工具调用",
  };
  return labels[kind];
}

/** 把可重放范围转换成统一的中文标签。 */
export function replayScopeLabel(scope: string): string {
  const labels: Record<string, string> = {
    metadata: "仅元数据",
    evidence: "证据",
    retrieval: "检索",
  };
  return labels[scope] ?? scope;
}

/** 把 artifact 状态转换成统一的中文标签。 */
export function artifactStateLabel(state: TraceArtifact["state"]): string {
  const labels: Record<TraceArtifact["state"], string> = {
    stored: "已保存",
    omitted: "已省略",
    missing: "已缺失",
  };
  return labels[state];
}

/** 把 artifact 类型转换成统一的业务名称。 */
export function artifactKindLabel(kind: string): string {
  const labels: Record<string, string> = {
    question: "用户问题",
    answer: "最终回答",
    "llm.prompt": "模型输入",
    "llm.output": "模型原始输出",
    "tool.arguments": "工具参数",
    "tool.payload": "工具返回内容",
  };
  return labels[kind] ?? kind;
}
