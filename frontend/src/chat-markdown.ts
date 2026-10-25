import MarkdownIt from "markdown-it";

/** Markdown 与来源编号集合的渲染环境。 */
interface ChatMarkdownEnvironment {
  sourceNumbers?: Map<string, number>;
}

/** 后端结构化的答案 span 引用关系。 */
export interface ChatCitationAnnotation {
  annotation_id: string;
  span_id: string;
  kind: string;
  start_index: number;
  end_index: number;
  text: string;
  block_ids: string[];
}

const markdown = new MarkdownIt({
  html: false,
  linkify: false,
  breaks: true,
  typographer: false,
});

markdown.renderer.rules.text = (tokens, index, _options, environment) => {
  const sourceNumbers =
    (environment as ChatMarkdownEnvironment | undefined)?.sourceNumbers ??
    new Map<string, number>();
  return escapeHtmlText(tokens[index].content).replace(
    /【来源：([^】]+)】/g,
    (_match: string, sourceList: string) =>
      sourceList
        .split(/[、,，;\s]+/u)
        .filter(Boolean)
        .map((sourceKey) => {
          const normalizedKey = normalizeChatSourceKey(sourceKey);
          const number = sourceNumbers.get(normalizedKey);
          const encodedKey = encodeURIComponent(normalizedKey);
          const title = number
            ? `查看来源 ${number}`
            : "模型引用未在本次检索结果中找到";
          return `<a href="#chat-source-${encodedKey}" class="chat-citation${number ? "" : " unresolved"}" data-chat-source-key="${encodedKey}" aria-haspopup="dialog" aria-expanded="false" title="${title}">${number ?? "来源"}</a>`;
        })
        .join(""),
  );
};

markdown.renderer.rules.link_open = (tokens, index, options, _environment, renderer) => {
  tokens[index].attrSet("target", "_blank");
  tokens[index].attrSet("rel", "noopener noreferrer");
  return renderer.renderToken(tokens, index, options);
};

/** 渲染安全 Markdown，并把来源标记替换成结构化引用链接。 */
export function renderChatMarkdown(
  value: string,
  sourceNumbers: Map<string, number>,
  citations: ChatCitationAnnotation[] = [],
): string {
  return markdown.render(applyChatCitationMarkers(value, citations), {
    sourceNumbers,
  });
}

/** 把结构化 annotation 注入渲染副本；原始答案字符串保持不变。 */
export function applyChatCitationMarkers(
  value: string,
  citations: ChatCitationAnnotation[],
): string {
  const characters = Array.from(value);
  const insertions = citations
    .filter(
      (citation) =>
        Number.isInteger(citation.start_index) &&
        Number.isInteger(citation.end_index) &&
        citation.start_index >= 0 &&
        citation.end_index > citation.start_index &&
        citation.end_index <= characters.length &&
        characters
          .slice(citation.start_index, citation.end_index)
          .join("") === citation.text &&
        citation.block_ids.length > 0,
    )
    .sort((left, right) => right.end_index - left.end_index);

  let rendered = value;
  let previousStart = value.length + 1;
  for (const citation of insertions) {
    if (citation.end_index > previousStart) {
      continue;
    }
    const citationEndIndex = citationInsertionCodePointIndex(
      characters,
      citation,
    );
    const insertionIndex = codePointIndexToCodeUnitIndex(
      value,
      citationEndIndex,
    );
    rendered =
      rendered.slice(0, insertionIndex) +
      `【来源：${citation.block_ids.join("，")}】` +
      rendered.slice(insertionIndex);
    previousStart = citation.start_index;
  }
  return rendered;
}

/** 表格行引用必须写进最后一个单元格，不能落在行尾竖线之后。 */
function citationInsertionCodePointIndex(
  characters: string[],
  citation: ChatCitationAnnotation,
): number {
  if (citation.kind !== "table_row") {
    return citation.end_index;
  }
  let index = Math.min(citation.end_index, characters.length) - 1;
  while (index >= citation.start_index && characters[index].trim() === "") {
    index -= 1;
  }
  return characters[index] === "|" ? index : citation.end_index;
}

/** 把后端使用的 Unicode code point offset 转成 JavaScript 的 UTF-16 offset。 */
function codePointIndexToCodeUnitIndex(
  value: string,
  codePointIndex: number,
): number {
  let codeUnitIndex = 0;
  let currentCodePointIndex = 0;
  for (const character of value) {
    if (currentCodePointIndex === codePointIndex) {
      return codeUnitIndex;
    }
    codeUnitIndex += character.length;
    currentCodePointIndex += 1;
  }
  return value.length;
}

/** 去除来源键首尾空白；除此之外必须与工具返回的 block_id 精确一致。 */
export function normalizeChatSourceKey(sourceKey: string): string {
  return sourceKey.trim();
}

/** 转义文本节点，避免 Markdown 渲染器输出任何未声明 HTML。 */
function escapeHtmlText(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
