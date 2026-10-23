/** llm.prompt artifact 中的一条模型消息。 */
export interface PromptMessageView {
  role: string;
  content: string;
}

/** 用户消息中可识别的题目和运行历史。 */
export interface UserPromptView {
  question: string;
  history: string;
}

/** Agent 输出中的一次工具调用。 */
export interface ToolActionView {
  kind: "tool";
  thought: string;
  toolName: string;
  argumentsText: string;
}

/** Agent 输出中的最终回答。 */
export interface FinishActionView {
  kind: "finish";
  thought: string;
  answer: string;
}

/** 一次合法 Agent 输出的工具调用或最终回答。 */
export type ModelOutputView = ToolActionView | FinishActionView;

/** 运行历史中的一条 Assistant 输出或 Observation。 */
export interface HistoryEntryView {
  kind: "assistant" | "observation" | "other";
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

/** 解析运行历史中的 Assistant 输出与 Observation。 */
export function parseHistoryEntries(history: string): HistoryEntryView[] {
  if (!history || history === "（空）") {
    return [];
  }
  return history
    .split(/\n{2,}(?=Assistant response:\n|Observation:\n)/)
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
      return {
        kind: "other",
        content: part,
        output: null,
        payloadText: formatJson(part),
      };
    });
}

/** 严格解析 Agent 的 Thought／Action 输出；规则与后端 actions.py 保持一致。 */
export function parseModelOutput(content: string): ModelOutputView | null {
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
    return { kind: "finish", thought, answer };
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
    thought,
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

/** 判断值是否为可索引对象。 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
