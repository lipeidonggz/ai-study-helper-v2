import MarkdownIt from "markdown-it";

/** Markdown 与来源编号集合的渲染环境。 */
interface ChatMarkdownEnvironment {
  sourceNumbers?: Map<string, number>;
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
          return `<a href="#chat-source-${encodedKey}" class="chat-citation${number ? "" : " unresolved"}" data-chat-source-key="${encodedKey}" title="${title}">${number ?? "来源"}</a>`;
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
): string {
  return markdown.render(value, { sourceNumbers });
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
