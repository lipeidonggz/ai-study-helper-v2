/** llm.prompt artifact 中的一条模型消息。 */
export interface PromptMessageView {
  role: string;
  content: string;
}

/** 系统提示词中一个工具的名称、说明和输入／输出契约。 */
export interface PromptToolView {
  name: string;
  description: string;
  inputSchemaText: string | null;
  outputSchemaText: string | null;
}

/** 系统提示词中结构化拆分出的工具定义与其余内容。 */
export interface PromptSystemView {
  tools: PromptToolView[];
  remainder: string;
}

/** 用户消息中可识别的题目和运行历史。 */
export interface UserPromptView {
  question: string;
  history: string;
}

/** Agent 输出中的一次工具调用。 */
export interface ToolActionView {
  kind: "tool";
  decisionSummary: string;
  toolName: string;
  argumentsText: string;
}

/** Agent 输出中的最终回答。 */
export interface FinishActionView {
  kind: "finish";
  decisionSummary: string;
  answer: string;
}

/** 一次合法 Agent 输出的工具调用或最终回答。 */
export type ModelOutputView = ToolActionView | FinishActionView;

/** 运行历史中的一条 Assistant 输出或 Observation。 */
export interface HistoryEntryView {
  kind: "assistant" | "tool_result" | "protocol_error" | "observation" | "other";
  content: string;
  output: ModelOutputView | null;
  payloadText: string | null;
}

/** 判官输入 user 消息中的业务载荷。 */
export interface JudgePacketView {
  answer: string;
  criterion: {
    ruleType: string;
    ruleText: string;
  };
}

/** 判官调用完整输入。 */
export interface JudgeInputView {
  promptVersion: string;
  messages: PromptMessageView[];
  packet: JudgePacketView | null;
}

/** 判官严格 JSON 输出。 */
export interface JudgeOutputView {
  verdict: string;
  reason: string;
}

/** 解析 llm.prompt 中的消息数组；结构不符合白名单时返回 null。 */
export function parsePromptMessages(content: string): PromptMessageView[] | null {
  try {
    const value = JSON.parse(content) as unknown;
    return parseMessageArray(value);
  } catch {
    return null;
  }
}

/** 从系统提示词中拆出工具定义，并把输入／输出 schema 格式化为可读 JSON。 */
export function parsePromptSystem(content: string): PromptSystemView | null {
  const match = content.match(/<tools>\s*([\s\S]*?)\s*<\/tools>/i);
  if (!match) {
    return null;
  }
  const tools = parseToolDefinitions(match[1]);
  if (!tools.length) {
    return null;
  }
  return {
    tools,
    remainder: content.replace(match[0], "").trim(),
  };
}

/** 解析判官调用的 prompt 包装和内部业务载荷。 */
export function parseJudgeInput(content: string): JudgeInputView | null {
  try {
    const value = JSON.parse(content) as unknown;
    if (!isRecord(value) || typeof value.prompt_version !== "string") {
      return null;
    }
    const messages = parseMessageArray(value.messages);
    if (!messages) {
      return null;
    }
    const userContent = messages.find((message) => message.role === "user")?.content;
    return {
      promptVersion: value.prompt_version,
      messages,
      packet: userContent ? parseJudgePacket(userContent) : null,
    };
  } catch {
    return null;
  }
}

/** 解析判官返回的严格 JSON；字段缺失或类型不符时返回 null。 */
export function parseJudgeOutput(content: string): JudgeOutputView | null {
  try {
    const value = JSON.parse(content) as unknown;
    if (
      !isRecord(value) ||
      typeof value.verdict !== "string" ||
      typeof value.reason !== "string"
    ) {
      return null;
    }
    return {
      verdict: value.verdict,
      reason: value.reason,
    };
  } catch {
    return null;
  }
}

/** 从用户消息中拆出正式问题和 Agent 运行历史。 */
export function parseUserPrompt(content: string): UserPromptView | null {
  const match = content.match(
    /<question>\s*([\s\S]*?)\s*<\/question>\s*<history>\s*([\s\S]*?)\s*<\/history>/i,
  );
  if (!match) {
    return null;
  }
  return {
    question: match[1].trim(),
    history: match[2].trim(),
  };
}

/** 解析运行历史中的结构化模型输出和 Harness 消息；兼容旧 trace。 */
export function parseHistoryEntries(history: string): HistoryEntryView[] {
  if (!history || history === "（空）") {
    return [];
  }
  return history
    .split(/\n{2,}(?=Assistant response:\n|Observation:\n|\{)/)
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      if (part.startsWith("Assistant response:\n")) {
        const content = part.slice("Assistant response:\n".length);
        return {
          kind: "assistant",
          content,
          output: parseModelOutput(content),
          payloadText: null,
        };
      }
      if (part.startsWith("Observation:\n")) {
        const content = part.slice("Observation:\n".length);
        return {
          kind: "observation",
          content,
          output: null,
          payloadText: formatJson(content),
        };
      }
      const structured = parseStructuredHistoryEntry(part);
      if (structured) {
        return structured;
      }
      return {
        kind: "other",
        content: part,
        output: null,
        payloadText: formatJson(part),
      };
    });
}

/** 严格解析 Agent 的结构化 JSON 输出；旧 trace 继续按 Thought／Action 读取。 */
export function parseModelOutput(content: string): ModelOutputView | null {
  const structured = parseStructuredModelOutput(content);
  if (structured) {
    return structured;
  }
  return parseLegacyModelOutput(content);
}

/** 解析结构化 tool_call／finish JSON，并验证展示所需字段。 */
function parseStructuredModelOutput(content: string): ModelOutputView | null {
  try {
    const value = parseModelJson(content);
    if (!isRecord(value)) {
      return null;
    }
    if (
      value.type === "tool_call" &&
      typeof value.decision_summary === "string" &&
      typeof value.tool === "string" &&
      isRecord(value.arguments) &&
      value.decision_summary.trim()
    ) {
      return {
        kind: "tool",
        decisionSummary: value.decision_summary,
        toolName: value.tool,
        argumentsText: JSON.stringify(value.arguments, null, 2),
      };
    }
    if (
      value.type === "finish" &&
      typeof value.answer === "string" &&
      value.answer.trim()
    ) {
      return {
        kind: "finish",
        decisionSummary:
          typeof value.decision_summary === "string" ? value.decision_summary : "",
        answer: value.answer,
      };
    }
    return null;
  } catch {
    return null;
  }
}

/** 解析一条结构化 Agent 或 Harness 历史消息；旧格式返回 null。 */
function parseStructuredHistoryEntry(part: string): HistoryEntryView | null {
  try {
    const value = parseModelJson(part);
    if (!isRecord(value) || typeof value.type !== "string") {
      return null;
    }
    if (value.type === "tool_call" || value.type === "finish") {
      return {
        kind: "assistant",
        content: part,
        output: parseStructuredModelOutput(part),
        payloadText: null,
      };
    }
    if (value.type === "tool_result") {
      return {
        kind: "tool_result",
        content: part,
        output: null,
        payloadText: formatJson(part),
      };
    }
    if (value.type === "protocol_error") {
      return {
        kind: "protocol_error",
        content: part,
        output: null,
        payloadText: formatJson(part),
      };
    }
  } catch {
    return null;
  }
  return null;
}

/** 解析模型 JSON，并兼容供应商漏转义字符串控制字符的行为。 */
function parseModelJson(content: string): unknown {
  try {
    return JSON.parse(content) as unknown;
  } catch {
    return JSON.parse(escapeControlCharactersInStrings(content)) as unknown;
  }
}

/** 只修复 JSON 字符串内的原始控制字符，不改变对象结构。 */
function escapeControlCharactersInStrings(content: string): string {
  let result = "";
  let inString = false;
  let afterBackslash = false;
  for (const char of content) {
    if (inString && afterBackslash) {
      result += char;
      afterBackslash = false;
      continue;
    }
    if (char === "\\" && inString) {
      result += char;
      afterBackslash = true;
      continue;
    }
    if (char === '"') {
      inString = !inString;
      result += char;
      continue;
    }
    if (inString && char.charCodeAt(0) < 0x20) {
      const replacements: Record<string, string> = {
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
      };
      result += replacements[char] ?? `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`;
      continue;
    }
    result += char;
  }
  return result;
}

/** 解析 026 之前自由文本 Thought／Action 格式，仅用于历史 trace 兼容。 */
function parseLegacyModelOutput(content: string): ModelOutputView | null {
  const lines = content.split(/\r?\n/);
  const nonEmpty = lines
    .map((line, index) => ({ line, index }))
    .filter((item) => item.line.trim());
  const thoughtIndexes = nonEmpty
    .filter((item) => item.line.trimStart().startsWith("Thought:"))
    .map((item) => item.index);
  const actionIndexes = nonEmpty
    .filter((item) => item.line.trimStart().startsWith("Action:"))
    .map((item) => item.index);
  if (
    thoughtIndexes.length !== 1 ||
    actionIndexes.length !== 1 ||
    nonEmpty[0]?.index !== thoughtIndexes[0] ||
    thoughtIndexes[0] >= actionIndexes[0]
  ) {
    return null;
  }

  const thoughtText = lines
    .slice(thoughtIndexes[0], actionIndexes[0])
    .map((line) => line.trim())
    .join("\n");
  const thought = thoughtText.slice("Thought:".length).trim();
  if (!thought) {
    return null;
  }

  const actionIndex = actionIndexes[0];
  const actionText = lines[actionIndex].trimStart().slice("Action:".length).trim();
  if (actionText.startsWith("Finish[")) {
    const finishText = [actionText, ...lines.slice(actionIndex + 1)].join("\n");
    const closeIndex = lastNonWhitespaceIndex(finishText);
    if (closeIndex < 0 || finishText[closeIndex] !== "]") {
      return null;
    }
    if (finishText.slice(closeIndex + 1).trim()) {
      return null;
    }
    const answer = finishText.slice("Finish[".length, closeIndex);
    if (!answer.trim()) {
      return null;
    }
    return { kind: "finish", decisionSummary: thought, answer };
  }

  if (lines.slice(actionIndex + 1).some((line) => line.trim())) {
    return null;
  }
  const match = actionText.match(/^([A-Za-z_][A-Za-z0-9_]*)\[([\s\S]*)\]$/);
  if (!match) {
    return null;
  }
  const argumentsText = formatJson(match[2]);
  let argumentsValue: unknown;
  try {
    argumentsValue = JSON.parse(match[2]) as unknown;
  } catch {
    return null;
  }
  if (!argumentsValue || typeof argumentsValue !== "object" || Array.isArray(argumentsValue)) {
    return null;
  }
  return {
    kind: "tool",
    decisionSummary: thought,
    toolName: match[1],
    argumentsText,
  };
}

/** 把 JSON 文本格式化；非法 JSON 保持原文。 */
export function formatJson(content: string): string {
  try {
    return JSON.stringify(JSON.parse(content) as unknown, null, 2);
  } catch {
    return content;
  }
}

/** 统计内容行数和字符数，供长文本摘要展示。 */
export function textStats(content: string): string {
  const lines = content ? content.split(/\r?\n/).length : 0;
  return `${lines} 行 · ${content.length} 字符`;
}

/** 返回最后一段非空白字符的位置。 */
function lastNonWhitespaceIndex(value: string): number {
  for (let index = value.length - 1; index >= 0; index -= 1) {
    if (!/\s/.test(value[index])) {
      return index;
    }
  }
  return -1;
}

/** 把 JSON 中的判官输入载荷转换成便于展示的领域对象。 */
function parseJudgePacket(content: string): JudgePacketView | null {
  try {
    const value = JSON.parse(content) as unknown;
    if (
      !isRecord(value) ||
      typeof value.answer !== "string" ||
      !isRecord(value.criterion) ||
      typeof value.criterion.rule_type !== "string" ||
      typeof value.criterion.rule_text !== "string"
    ) {
      return null;
    }
    const criterion = value.criterion;
    return {
      answer: value.answer as string,
      criterion: {
        ruleType: criterion.rule_type as string,
        ruleText: criterion.rule_text as string,
      },
    };
  } catch {
    return null;
  }
}

/** 校验并转换消息数组。 */
function parseMessageArray(value: unknown): PromptMessageView[] | null {
  if (!Array.isArray(value)) {
    return null;
  }
  const messages: PromptMessageView[] = [];
  for (const item of value) {
    if (
      !isRecord(item) ||
      typeof item.role !== "string" ||
      typeof item.content !== "string"
    ) {
      return null;
    }
    messages.push({ role: item.role, content: item.content });
  }
  return messages;
}

/** 解析工具段落；新 trace 同时包含输入和输出 schema，旧 trace 允许仅有输入。 */
function parseToolDefinitions(content: string): PromptToolView[] {
  const tools: PromptToolView[] = [];
  let current: PromptToolView | null = null;
  for (const line of content.split(/\r?\n/)) {
    const heading = line.match(/^\s*-\s*([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$/);
    if (heading) {
      if (current) {
        tools.push(current);
      }
      current = {
        name: heading[1],
        description: heading[2].trim(),
        inputSchemaText: null,
        outputSchemaText: null,
      };
      continue;
    }
    if (!current) {
      continue;
    }
    const inputSchema = line.match(/^\s*输入 schema:\s*(.*)$/);
    if (inputSchema) {
      current.inputSchemaText = formatJson(inputSchema[1]);
      continue;
    }
    const outputSchema = line.match(/^\s*输出 schema:\s*(.*)$/);
    if (outputSchema) {
      current.outputSchemaText = formatJson(outputSchema[1]);
      continue;
    }
    if (line.trim()) {
      current.description = `${current.description}\n${line.trim()}`.trim();
    }
  }
  if (current) {
    tools.push(current);
  }
  return tools;
}

/** 判断值是否为可索引对象。 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
