from __future__ import annotations

import pytest

from app.ingestion.errors import IngestFailure
from app.ingestion.extract import extract_html_document
from tests.helpers import fixture_bytes, source_artifact_for_bytes


def test_extracts_semantic_nodes_and_metadata() -> None:
    raw = fixture_bytes("article.html")
    extracted = extract_html_document(
        source_artifact_for_bytes("article.html", raw),
        raw.decode("utf-8"),
    )

    assert extracted.title_candidate == "Agent 隔离设计"
    assert extracted.site_name_candidate == "示例学习站"
    assert extracted.language == "zh"
    kinds = {node.kind for node in extracted.nodes}
    assert {"heading", "paragraph", "list", "code", "table", "figure"} <= kinds
    assert "站点导航不应进入正文" not in " ".join(node.text for node in extracted.nodes)
    assert "页脚不应进入正文" not in " ".join(node.text for node in extracted.nodes)

    code = next(node for node in extracted.nodes if node.kind == "code")
    assert "\n    return" in code.text
    assert code.code_language == "python"

    table = next(node for node in extracted.nodes if node.kind == "table")
    assert table.table_caption == "边界类型对照"
    assert table.table_rows[0] == ["机制", "边界"]


def test_rejects_javascript_shell_and_login_page() -> None:
    shell_raw = fixture_bytes("javascript-shell.html")
    with pytest.raises(IngestFailure) as shell_failure:
        extract_html_document(
            source_artifact_for_bytes("javascript-shell.html", shell_raw),
            shell_raw.decode("utf-8"),
        )
    assert shell_failure.value.code == "javascript_rendering_unsupported"

    login_raw = fixture_bytes("login.html")
    with pytest.raises(IngestFailure) as login_failure:
        extract_html_document(
            source_artifact_for_bytes("login.html", login_raw),
            login_raw.decode("utf-8"),
        )
    assert login_failure.value.code == "login_required"


def test_password_example_in_readable_tutorial_is_not_a_login_page() -> None:
    raw = fixture_bytes("login-tutorial.html")
    extracted = extract_html_document(
        source_artifact_for_bytes("login-tutorial.html", raw),
        raw.decode("utf-8"),
    )

    assert extracted.title_candidate == "HTML login form tutorial"
    assert any(node.kind == "paragraph" for node in extracted.nodes)


def test_empty_content_is_classified() -> None:
    raw = fixture_bytes("empty-content.html")
    with pytest.raises(IngestFailure) as failure:
        extract_html_document(
            source_artifact_for_bytes("empty-content.html", raw),
            raw.decode("utf-8"),
        )
    assert failure.value.code == "no_extractable_content"


def test_html_comments_inside_list_items_are_ignored() -> None:
    raw = (
        b"<!doctype html><html lang='en'><head><title>Comments in list</title></head>"
        b"<body><article><h1>Comments in list</h1><h2>Steps</h2>"
        b"<ul><li>First phrase<!-- editor note --> continues after comment"
        b"<strong> with emphasis</strong> and closing text."
        b"<!-- nested note --></li></ul>"
        b"<p>The surrounding article has enough readable text to pass extraction.</p>"
        b"</article></body></html>"
    )

    extracted = extract_html_document(
        source_artifact_for_bytes("comments-in-list.html", raw),
        raw.decode("utf-8"),
    )

    list_node = next(node for node in extracted.nodes if node.kind == "list")
    assert "editor note" not in list_node.text
    assert "nested note" not in list_node.text
    assert "First phrase continues after comment with emphasis and closing text." in list_node.text
