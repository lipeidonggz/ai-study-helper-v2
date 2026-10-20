from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

from lxml import html
from lxml.html import HtmlElement
from readability import Document

from app.ingestion.errors import IngestFailure
from app.ingestion.language import detect_document_language, normalize_declared_language
from app.ingestion.models import (
    ComponentVersion,
    ExtractedDocument,
    ExtractionStats,
    ListNode,
    SemanticNode,
    SourceArtifact,
    TitleSource,
)

EXTRACTOR_VERSION = "1"
MIN_CONTENT_CHARACTERS = 40
JS_SHELL_VISIBLE_CHARACTERS = 80
LOGIN_PAGE_MAX_VISIBLE_CHARACTERS = 800
LOGIN_PAGE_MAX_OUTSIDE_FORM_CHARACTERS = 200
LOGIN_FORM_SIGNALS = (
    "login",
    "log in",
    "signin",
    "sign in",
    "sign-in",
    "password",
    "登录",
    "登陆",
    "密码",
)

_BLOCK_CONTAINERS = {"article", "body", "div", "main", "section"}
_SKIPPED_ELEMENTS = {
    "aside",
    "canvas",
    "footer",
    "form",
    "header",
    "nav",
    "noscript",
    "script",
    "style",
    "svg",
}


def normalize_text(value: str) -> str:
    """Normalize visible prose without changing its evidence semantics."""

    normalized = unicodedata.normalize("NFC", value.replace("\xa0", " "))
    return re.sub(r"\s+", " ", normalized).strip()


def normalize_code(value: str) -> str:
    """Preserve code line structure and indentation."""

    normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    return normalized.strip("\n")


def _meta_content(
    root: HtmlElement, *, name: str | None = None, property_name: str | None = None
) -> str:
    selector_parts: list[str] = []
    if name:
        selector_parts.append(f'@name="{name}"')
    if property_name:
        selector_parts.append(f'@property="{property_name}"')
    if not selector_parts:
        return ""
    values = root.xpath(f"//meta[{' or '.join(selector_parts)}]/@content")
    return normalize_text(str(values[0])) if values else ""


def _declared_language(root: HtmlElement) -> str | None:
    """Return an explicit language tag from the document or reliable metadata."""

    candidates = [
        root.get("lang"),
        _meta_content(root, name="language"),
    ]
    content_language = root.xpath(
        "//meta[translate(@http-equiv, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz')='content-language']/@content"
    )
    candidates.extend(str(value) for value in content_language)
    return next(
        (
            candidate
            for candidate in candidates
            if candidate and normalize_declared_language(candidate) is not None
        ),
        None,
    )


def _is_login_page(root: HtmlElement, body_text: str) -> bool:
    """Recognize short password forms without rejecting readable tutorials."""

    if len(body_text) > LOGIN_PAGE_MAX_VISIBLE_CHARACTERS:
        return False
    password_fields = root.xpath(
        "//input[translate(@type, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz')='password']"
    )
    for field in password_fields:
        forms = field.xpath("ancestor::form[1]")
        if not forms:
            continue
        form = forms[0]
        signal_parts = [
            form.get("id", ""),
            form.get("class", ""),
            form.get("action", ""),
            normalize_text(form.text_content()),
        ]
        signal_parts.extend(
            value
            for element in form.xpath(".//input|.//button")
            for value in (
                element.get("name", ""),
                element.get("placeholder", ""),
                element.get("autocomplete", ""),
                element.get("value", ""),
            )
        )
        signal_text = " ".join(signal_parts).casefold()
        if not any(signal in signal_text for signal in LOGIN_FORM_SIGNALS):
            continue
        containers = field.xpath("ancestor::article[1]|ancestor::main[1]")
        container = containers[0] if containers else root
        outside_form_text = normalize_text(
            " ".join(
                container.xpath(
                    ".//text()[not(ancestor::form) "
                    "and not(ancestor::script) and not(ancestor::style)]"
                )
            )
        )
        if len(outside_form_text) <= LOGIN_PAGE_MAX_OUTSIDE_FORM_CHARACTERS:
            return True
    return False


def _locator(element: HtmlElement) -> str:
    tree = element.getroottree()
    return tree.getpath(element)


def _class_language(element: HtmlElement) -> str | None:
    for class_name in element.get("class", "").split():
        for prefix in ("language-", "lang-"):
            if class_name.startswith(prefix) and len(class_name) > len(prefix):
                return class_name[len(prefix) :]
    return None


def _list_items(list_element: HtmlElement, level: int = 0) -> list[ListNode]:
    items: list[ListNode] = []
    for item in list_element.xpath("./li"):
        nested_lists = item.xpath("./ul|./ol")
        own_text_parts: list[str] = []
        if item.text:
            own_text_parts.append(item.text)
        for child in item:
            if child.tag in {"ul", "ol"}:
                continue
            own_text_parts.append(child.text_content())
        own_text = normalize_text(" ".join(own_text_parts))
        if own_text:
            items.append(ListNode(text=own_text, level=level))
        for nested in nested_lists:
            items.extend(_list_items(nested, level + 1))
    return items


def _render_list(items: list[ListNode], ordered: bool) -> str:
    lines: list[str] = []
    item_number = 0
    for item in items:
        if ordered and item.level == 0:
            item_number += 1
            marker = f"{item_number}."
        else:
            marker = "-"
        lines.append(f"{'  ' * item.level}{marker} {item.text}")
    return "\n".join(lines)


def _table_rows(table: HtmlElement) -> tuple[list[list[str]], str]:
    caption_values = table.xpath("./caption")
    caption = normalize_text(caption_values[0].text_content()) if caption_values else ""
    rows: list[list[str]] = []
    for row in table.xpath(".//tr"):
        cells = [normalize_text(cell.text_content()) for cell in row.xpath("./th|./td")]
        if cells:
            rows.append(cells)
    return rows, caption


def _render_table(rows: list[list[str]]) -> str:
    return "\n".join(" | ".join(cells) for cells in rows)


def _figure_text(figure: HtmlElement) -> tuple[str, str | None]:
    alt_values = [
        normalize_text(value) for value in figure.xpath(".//img/@alt") if normalize_text(value)
    ]
    caption_values = [
        normalize_text(value)
        for value in figure.xpath("./figcaption//text()")
        if normalize_text(value)
    ]
    alt = " ".join(alt_values) or None
    caption = normalize_text(" ".join(caption_values))
    return normalize_text(" ".join(part for part in (alt, caption) if part)), alt


def _walk_nodes(container: HtmlElement, output: list[SemanticNode]) -> None:
    for child in container:
        if not isinstance(child.tag, str):
            continue
        tag = child.tag.lower()
        if tag in _SKIPPED_ELEMENTS:
            continue
        if re.fullmatch(r"h[1-6]", tag):
            value = normalize_text(child.text_content())
            if value:
                output.append(
                    SemanticNode(
                        ordinal=len(output) + 1,
                        kind="heading",
                        text=value,
                        heading_level=int(tag[1]),
                        source_locator=_locator(child),
                    )
                )
            continue
        if tag == "p":
            value = normalize_text(child.text_content())
            if value:
                output.append(
                    SemanticNode(
                        ordinal=len(output) + 1,
                        kind="paragraph",
                        text=value,
                        source_locator=_locator(child),
                    )
                )
            continue
        if tag in {"ul", "ol"}:
            items = _list_items(child)
            if items:
                ordered = tag == "ol"
                output.append(
                    SemanticNode(
                        ordinal=len(output) + 1,
                        kind="list",
                        text=_render_list(items, ordered),
                        list_ordered=ordered,
                        list_items=items,
                        source_locator=_locator(child),
                    )
                )
            continue
        if tag == "blockquote":
            value = normalize_text(child.text_content())
            if value:
                output.append(
                    SemanticNode(
                        ordinal=len(output) + 1,
                        kind="quote",
                        text=value,
                        source_locator=_locator(child),
                    )
                )
            continue
        if tag == "pre":
            value = normalize_code(child.text_content())
            if value:
                code = child.xpath(".//code")
                language = _class_language(code[0] if code else child)
                output.append(
                    SemanticNode(
                        ordinal=len(output) + 1,
                        kind="code",
                        text=value,
                        code_language=language,
                        source_locator=_locator(child),
                    )
                )
            continue
        if tag == "table":
            rows, caption = _table_rows(child)
            if rows:
                output.append(
                    SemanticNode(
                        ordinal=len(output) + 1,
                        kind="table",
                        text=_render_table(rows),
                        table_rows=rows,
                        table_caption=caption or None,
                        source_locator=_locator(child),
                    )
                )
            continue
        if tag == "figure":
            value, alt = _figure_text(child)
            if value:
                output.append(
                    SemanticNode(
                        ordinal=len(output) + 1,
                        kind="figure",
                        text=value,
                        image_alt=alt,
                        source_locator=_locator(child),
                    )
                )
            continue
        if tag == "img":
            alt = normalize_text(child.get("alt", ""))
            if alt:
                output.append(
                    SemanticNode(
                        ordinal=len(output) + 1,
                        kind="figure",
                        text=alt,
                        image_alt=alt,
                        source_locator=_locator(child),
                    )
                )
            continue
        if tag in _BLOCK_CONTAINERS:
            _walk_nodes(child, output)


def _choose_title(
    *,
    root: HtmlElement,
    nodes: list[SemanticNode],
    source: SourceArtifact,
    readability_document: Document,
) -> tuple[str, TitleSource, list[str]]:
    warnings: list[str] = []
    og_title = _meta_content(root, property_name="og:title")
    if og_title:
        return og_title, TitleSource(kind="metadata"), warnings

    page_title = normalize_text(readability_document.title())
    short_title = normalize_text(readability_document.short_title())
    headings = [node for node in nodes if node.kind == "heading"]
    first_h1 = next((node for node in headings if node.heading_level == 1), None)
    if first_h1 and (
        not page_title
        or normalize_text(first_h1.text).casefold() in normalize_text(page_title).casefold()
        or normalize_text(short_title).casefold() in normalize_text(first_h1.text).casefold()
    ):
        return first_h1.text, TitleSource(kind="heading", node_ordinal=first_h1.ordinal), warnings
    if short_title:
        return short_title, TitleSource(kind="metadata"), warnings
    if page_title:
        return page_title, TitleSource(kind="metadata"), warnings

    filename = Path(urlsplit(source.source_key).path).stem or "Untitled"
    warnings.append("title_fallback_filename")
    return filename, TitleSource(kind="filename"), warnings


def _select_content_root(
    root: HtmlElement,
    summary_root: HtmlElement,
    warnings: list[str],
) -> HtmlElement:
    """Prefer a source article/main container when it preserves more structure.

    Readability occasionally drops semantically important tables or figures as
    low-scoring content. If its extracted prose is contained in an article/main
    element, walking that narrower source container keeps those evidence
    structures without reintroducing site navigation or footers.
    """

    summary_text = normalize_text(summary_root.text_content())
    probe = summary_text[:160]
    candidates = root.xpath("//article|//main")
    if not probe or not candidates:
        return summary_root
    for candidate in sorted(
        candidates,
        key=lambda element: len(normalize_text(element.text_content())),
        reverse=True,
    ):
        candidate_text = normalize_text(candidate.text_content())
        if probe in candidate_text and len(candidate_text) > len(summary_text):
            warnings.append("source_container_preserved_structures")
            return candidate
    return summary_root


def extract_html_document(source: SourceArtifact, decoded_html: str) -> ExtractedDocument:
    """Extract a format-neutral semantic document from one HTML artifact."""

    try:
        root = html.document_fromstring(decoded_html)
    except (ValueError, TypeError) as error:
        raise IngestFailure("html_parse_failed", f"HTML parser failed: {error}", 422) from error

    body_text = normalize_text(root.text_content())
    script_count = len(root.xpath("//script"))
    if _is_login_page(root, body_text):
        raise IngestFailure(
            "login_required",
            "login or password-protected pages are not supported",
            422,
        )
    if len(body_text) < JS_SHELL_VISIBLE_CHARACTERS and script_count:
        raise IngestFailure(
            "javascript_rendering_unsupported",
            "the page appears to require JavaScript rendering",
            422,
        )

    readability_document = Document(decoded_html)
    warnings: list[str] = []
    try:
        summary_html = readability_document.summary(html_partial=True)
        summary_root = html.fromstring(summary_html)
        content_root = _select_content_root(root, summary_root, warnings)
    except (ValueError, TypeError, IndexError):
        content_root = root.find("body")
        if content_root is None:
            content_root = root
        warnings.append("readability_fallback_body")

    nodes: list[SemanticNode] = []
    _walk_nodes(content_root, nodes)
    body_nodes = [node for node in nodes if node.kind != "heading"]
    if not body_nodes or sum(len(node.text) for node in body_nodes) < MIN_CONTENT_CHARACTERS:
        raise IngestFailure(
            "no_extractable_content",
            "the HTML does not contain enough readable body content",
            422,
        )

    title, title_source, title_warnings = _choose_title(
        root=root,
        nodes=nodes,
        source=source,
        readability_document=readability_document,
    )
    warnings.extend(title_warnings)
    language = detect_document_language(
        _declared_language(root),
        "\n".join(node.text for node in body_nodes),
    )
    site_name = _meta_content(root, property_name="og:site_name") or None
    if not site_name and source.source_kind == "url" and source.final_url:
        site_name = urlsplit(source.final_url).hostname

    canonical_payload = {
        "title": title,
        "site_name": site_name,
        "language": language,
        "nodes": [node.model_dump(mode="json", exclude={"source_locator"}) for node in nodes],
    }
    extraction_hash = hashlib.sha256(
        json.dumps(
            canonical_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    return ExtractedDocument(
        source_artifact_ref=source.raw_artifact_ref,
        effective_content_ref=source.raw_artifact_ref,
        extractor=ComponentVersion(name="html-readability-lxml", version=EXTRACTOR_VERSION),
        extraction_hash=extraction_hash,
        title_candidate=title,
        title_source=title_source,
        site_name_candidate=site_name,
        language=language,
        nodes=nodes,
        warnings=warnings,
        stats=ExtractionStats(
            node_count=len(nodes),
            character_count=sum(len(node.text) for node in nodes),
            heading_count=sum(1 for node in nodes if node.kind == "heading"),
        ),
    )
