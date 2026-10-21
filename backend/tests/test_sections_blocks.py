from __future__ import annotations

from app.ingestion.blocks import build_block_set, estimate_context_tokens
from app.ingestion.extract import extract_html_document
from app.ingestion.sections import build_section_tree
from tests.helpers import fixture_bytes, source_artifact_for_bytes


def _build_blocks(raw: bytes, filename: str):
    source = source_artifact_for_bytes(filename, raw)
    extracted = extract_html_document(source, raw.decode("utf-8"))
    sections = build_section_tree(extracted, doc_id="preview_test", doc_version=0)
    return build_block_set(
        doc_id="preview_test",
        doc_version=0,
        extracted=extracted,
        section_tree=sections,
        use_preview_block_ids=True,
    )


def test_section_paths_handle_duplicates_and_preamble() -> None:
    raw = fixture_bytes("article.html")
    extracted = extract_html_document(
        source_artifact_for_bytes("article.html", raw),
        raw.decode("utf-8"),
    )
    sections = build_section_tree(extracted, doc_id="preview_sections", doc_version=0)

    assert sections.sections[0].path == ["环境边界"]
    duplicate = next(
        section
        for section in sections.sections
        if section.title == "环境边界" and section.path[-1].endswith("(2)")
    )
    assert duplicate.path == ["环境边界 (2)"]
    assert "preamble_attached" in sections.warnings
    assert "duplicate_section_title" in sections.warnings


def test_no_heading_document_uses_root_section() -> None:
    raw = fixture_bytes("no-headings.html")
    extracted = extract_html_document(
        source_artifact_for_bytes("no-headings.html", raw),
        raw.decode("utf-8"),
    )
    sections = build_section_tree(extracted, doc_id="preview_no_headings", doc_version=0)

    assert len(sections.sections) == 1
    assert sections.sections[0].path == []
    assert sections.sections[0].depth == 0


def test_blocks_cover_every_body_node_without_gaps() -> None:
    raw = fixture_bytes("article.html")
    extracted = extract_html_document(
        source_artifact_for_bytes("article.html", raw),
        raw.decode("utf-8"),
    )
    sections = build_section_tree(extracted, doc_id="preview_coverage", doc_version=0)
    blocks = build_block_set(
        doc_id="preview_coverage",
        doc_version=0,
        extracted=extracted,
        section_tree=sections,
        use_preview_block_ids=True,
    )

    body_ordinals = {node.ordinal for node in extracted.nodes if node.kind != "heading"}
    covered = {ordinal for block in blocks.blocks for ordinal in block.source_node_ordinals}
    assert covered == body_ordinals
    assert [block.block_index for block in blocks.blocks] == list(range(1, len(blocks.blocks) + 1))
    assert blocks.blocks[0].char_start == 0
    assert blocks.blocks[-1].char_end == len(blocks.canonical_body_text)
    assert all(block.language in {"zh", "en", "mixed", "unknown"} for block in blocks.blocks)
    for previous, current in zip(blocks.blocks, blocks.blocks[1:], strict=False):
        assert previous.char_end == current.char_start


def test_long_paragraph_is_split_without_content_loss() -> None:
    paragraph = "这是用于测试边界的长句子。" * 160
    raw = (
        "<!doctype html><html lang='zh'><head><title>长段落</title></head>"
        f"<body><article><h1>长段落</h1><h2>正文</h2><p>{paragraph}</p>"
        "</article></body></html>"
    ).encode()
    source = source_artifact_for_bytes("long.html", raw)
    extracted = extract_html_document(source, raw.decode("utf-8"))
    sections = build_section_tree(extracted, doc_id="preview_long", doc_version=0)
    blocks = build_block_set(
        doc_id="preview_long",
        doc_version=0,
        extracted=extracted,
        section_tree=sections,
        use_preview_block_ids=True,
    )

    joined = "".join(block.text for block in blocks.blocks)
    assert paragraph in joined.replace("\n", "")
    assert len(blocks.blocks) > 1
    assert "text_semantic_split" in blocks.warnings
    assert "text_boundary_split" not in blocks.warnings
    assert all(block.text.endswith("。") for block in blocks.blocks)


def test_long_single_sentence_uses_clause_punctuation_before_characters() -> None:
    clause = "这是没有句号但仍应由分句标点拆开的长句" * 4
    paragraph = "，".join([clause] * 60)
    raw = (
        "<!doctype html><html lang='zh'><head><title>长分句</title></head>"
        f"<body><article><h1>长分句</h1><h2>正文</h2><p>{paragraph}</p>"
        "</article></body></html>"
    ).encode()
    blocks = _build_blocks(raw, "long-clause.html")

    assert len(blocks.blocks) > 1
    assert all(block.text.endswith("，") for block in blocks.blocks[:-1])
    assert "text_semantic_split" in blocks.warnings
    assert "text_boundary_split" not in blocks.warnings


def test_long_paragraph_without_soft_boundary_falls_back_to_characters() -> None:
    paragraph = "汉" * 1000
    raw = (
        "<!doctype html><html lang='zh'><head><title>字符降级</title></head>"
        f"<body><article><h1>字符降级</h1><h2>正文</h2><p>{paragraph}</p>"
        "</article></body></html>"
    ).encode()
    blocks = _build_blocks(raw, "character-fallback.html")

    assert len(blocks.blocks) > 1
    assert all(block.context_token_estimate <= 450 for block in blocks.blocks)
    assert "".join(block.text for block in blocks.blocks) == paragraph
    assert "text_boundary_split" in blocks.warnings


def test_long_list_prefers_item_boundaries() -> None:
    item = "步骤内容" * 40
    list_items = "".join(f"<li>{index}-{item}</li>" for index in range(1, 13))
    raw = (
        "<!doctype html><html lang='zh'><head><title>长列表</title></head>"
        f"<body><article><h1>长列表</h1><h2>步骤</h2><ol>{list_items}</ol>"
        "</article></body></html>"
    ).encode()
    blocks = _build_blocks(raw, "long-list.html")

    assert len(blocks.blocks) > 1
    for block in blocks.blocks:
        assert all(
            line.startswith(tuple(f"{index}. " for index in range(1, 13)))
            for line in block.text.splitlines()
        )
    assert "list_item_split" in blocks.warnings


def test_long_code_line_uses_character_limit_without_whitespace_shortcut() -> None:
    code_line = "A" * 100 + " " + "B" * 2400
    raw = (
        "<!doctype html><html lang='en'><head><title>Long code</title></head>"
        f"<body><article><h1>Long code</h1><h2>Example</h2><pre><code>{code_line}</code></pre>"
        "</article></body></html>"
    ).encode()
    blocks = _build_blocks(raw, "long-code.html")

    assert len(blocks.blocks) > 1
    assert len(blocks.blocks[0].text) > 500
    assert "".join(block.text for block in blocks.blocks) == code_line
    assert "code_line_split" in blocks.warnings


def test_large_table_is_split_into_contiguous_row_groups() -> None:
    rows = "".join(
        f"<tr><td>行{index}</td><td>{'数据' * 30}</td><td>{'约束' * 30}</td></tr>"
        for index in range(80)
    )
    raw = (
        "<!doctype html><html lang='zh'><head><title>大表格</title></head>"
        "<body><article><h1>大表格</h1><h2>表格</h2>"
        "<table><caption>测试表</caption><thead><tr><th>编号</th><th>数据</th><th>约束</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></article></body></html>"
    ).encode()
    source = source_artifact_for_bytes("table.html", raw)
    extracted = extract_html_document(source, raw.decode("utf-8"))
    sections = build_section_tree(extracted, doc_id="preview_table", doc_version=0)
    blocks = build_block_set(
        doc_id="preview_table",
        doc_version=0,
        extracted=extracted,
        section_tree=sections,
        use_preview_block_ids=True,
    )

    table_blocks = [block for block in blocks.blocks if block.table_context is not None]
    assert len(table_blocks) > 1
    assert table_blocks[0].table_context.headers == ["编号", "数据", "约束"]
    assert [block.table_context.group_index for block in table_blocks] == list(
        range(1, len(table_blocks) + 1)
    )
    assert "table_row_group_split" in blocks.warnings


def test_oversized_table_row_remains_atomic_and_warns() -> None:
    oversized_cell = "宽" * 4200
    other_rows = "".join(
        f"<tr><td>行{index}</td><td>{'数据' * 30}</td></tr>" for index in range(20)
    )
    raw = (
        "<!doctype html><html lang='zh'><head><title>超宽表格</title></head>"
        "<body><article><h1>超宽表格</h1><h2>表格</h2><table>"
        "<thead><tr><th>编号</th><th>数据</th></tr></thead>"
        f"<tbody><tr><td>超长行</td><td>{oversized_cell}</td></tr>{other_rows}</tbody>"
        "</table></article></body></html>"
    ).encode()
    blocks = _build_blocks(raw, "oversized-table-row.html")
    table_blocks = [block for block in blocks.blocks if block.table_context is not None]

    assert "oversized_atomic_table_row" in blocks.warnings
    assert sum(block.text.count("宽") for block in table_blocks) == len(oversized_cell)
    assert any(len(block.text) >= len(oversized_cell) for block in table_blocks)


def test_sizing_estimator_is_fixed_and_deterministic() -> None:
    assert estimate_context_tokens("中文") == 2
    assert estimate_context_tokens("abcd") == 1
    assert estimate_context_tokens("abcde") == 2
    assert estimate_context_tokens("，。！？") == 1
    assert estimate_context_tokens("") == 1


def test_block_language_uses_block_text_instead_of_copying_document_language() -> None:
    english = (
        "This English block contains enough letters to be classified independently "
        "from the declared Chinese document language."
    )
    raw = (
        "<!doctype html><html lang='zh'><head><title>Language boundary</title></head>"
        f"<body><article><h1>Language boundary</h1><h2>English section</h2><p>{english}</p>"
        "</article></body></html>"
    ).encode()
    blocks = _build_blocks(raw, "block-language.html")

    assert blocks.blocks[0].section_path == ["English section"]
    assert blocks.blocks[0].language == "en"


def test_complex_article_preserves_structures_and_excludes_page_chrome() -> None:
    raw = fixture_bytes("complex-article.html")
    extracted = extract_html_document(
        source_artifact_for_bytes("complex-article.html", raw),
        raw.decode("utf-8"),
    )
    sections = build_section_tree(extracted, doc_id="preview_complex", doc_version=0)
    blocks = build_block_set(
        doc_id="preview_complex",
        doc_version=0,
        extracted=extracted,
        section_tree=sections,
        use_preview_block_ids=True,
    )

    all_text = "\n".join(block.text for block in blocks.blocks)
    assert len(sections.sections) == 6
    assert len(blocks.blocks) == 7
    assert "全局页头" not in all_text
    assert "站点导航" not in all_text
    assert "推荐阅读" not in all_text
    assert "全局页脚" not in all_text
    assert "def in_workspace" in all_text
    assert "S4" in all_text
    assert "图片说明进入正文" in all_text
    assert "duplicate_section_title" in sections.warnings
