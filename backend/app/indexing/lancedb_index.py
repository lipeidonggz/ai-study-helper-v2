from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import lancedb
import pyarrow as pa
from lancedb.index import FTS

from app.ingestion.errors import IngestFailure

BLOCKS_TABLE_NAME = "blocks"
FTS_FIELD_NAME = "search_terms"
# 记录实际运行版本，因为引擎升级可能改变 BM25 排序行为，即使建索引 API 不变。
LANCEDB_ENGINE_VERSION = str(getattr(lancedb, "__version__", "unknown"))


def write_candidate_lancedb(
    directory: Path,
    rows: list[dict[str, Any]],
    *,
    dimension: int,
) -> None:
    """创建 dense＋FTS 候选表；不创建 ANN 或活动 collection 指针。"""

    if not rows:
        raise IngestFailure(
            "index_empty_block_set",
            "cannot create a candidate index without blocks",
            422,
        )
    directory.mkdir(parents=True, exist_ok=False)
    schema = pa.schema(
        [
            pa.field("block_id", pa.string(), nullable=False),
            pa.field("doc_id", pa.string(), nullable=False),
            pa.field("doc_version", pa.int32(), nullable=False),
            pa.field("block_index", pa.int32(), nullable=False),
            pa.field("content_hash", pa.string(), nullable=False),
            pa.field("embedding_input_hash", pa.string(), nullable=False),
            pa.field("search_text", pa.string(), nullable=False),
            pa.field("search_terms", pa.string(), nullable=False),
            pa.field("vector_dimension", pa.int32(), nullable=False),
            pa.field("dense_and_keyword_present", pa.bool_(), nullable=False),
            pa.field("vector", pa.list_(pa.float32(), dimension), nullable=False),
        ]
    )
    try:
        database = lancedb.connect(str(directory))
        table = database.create_table(
            BLOCKS_TABLE_NAME,
            data=rows,
            schema=schema,
            mode="create",
            on_bad_vectors="error",
        )
        table.create_index(
            FTS_FIELD_NAME,
            config=FTS(
                base_tokenizer="whitespace",
                lower_case=False,
                stem=False,
                remove_stop_words=False,
                ascii_folding=False,
            ),
            replace=False,
            wait_timeout=timedelta(seconds=60),
        )
    except Exception as error:
        raise IngestFailure(
            "candidate_index_write_failed",
            f"could not write LanceDB candidate index: {error}",
            500,
        ) from error


def validate_candidate_lancedb(
    directory: Path,
    *,
    expected_block_ids: set[str],
    dimension: int,
) -> None:
    """逐行读取候选表，确认双索引覆盖同一 block 集且向量完整。"""

    try:
        table = lancedb.connect(str(directory)).open_table(BLOCKS_TABLE_NAME)
        rows = table.search().limit(len(expected_block_ids) + 1).to_list()
    except Exception as error:
        raise IngestFailure(
            "candidate_index_invalid",
            f"candidate LanceDB index cannot be opened: {error}",
            500,
        ) from error

    actual_block_ids = {str(row.get("block_id", "")) for row in rows}
    if actual_block_ids != expected_block_ids or len(rows) != len(expected_block_ids):
        raise IngestFailure(
            "index_integrity_failed",
            "dense and keyword indexes do not cover exactly the block set",
            500,
        )
    for row in rows:
        if not row.get("dense_and_keyword_present"):
            raise IngestFailure(
                "index_integrity_failed",
                "a block is missing one side of the dual index",
                500,
            )
        vector = row.get("vector")
        if not isinstance(vector, list) or len(vector) != dimension:
            raise IngestFailure(
                "index_integrity_failed",
                "candidate vector dimension does not match the embedding profile",
                500,
            )
        if not row.get("search_terms"):
            raise IngestFailure(
                "index_integrity_failed",
                "a block has no keyword terms",
                500,
            )
