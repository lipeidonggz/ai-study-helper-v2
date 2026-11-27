"""对固定 collection 快照执行可恢复的全量叶节点证据挂接。"""

# 运行器必须先设置 backend 与 scripts 搜索路径，之后才能导入项目模块。
# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = SCRIPT_ROOT.parent / "backend"
sys.path.insert(0, str(SCRIPT_ROOT))
sys.path.insert(0, str(BACKEND_ROOT))

from app.providers.chat import (
    DEEPSEEK_BASE_URL,
    ChatCompletionResult,
    ChatMessage,
    ChatProviderError,
    ChatProviderProfile,
    DeepSeekChatProvider,
)
from app.storage.credentials import CHAT_API_KEY_USERNAME, WindowsCredentialStore
from app.wiki.leaf_coverage import framework_content_sha256
from wiki_leaf_support_probe import (
    MAX_OUTPUT_TOKENS,
    MODEL_ID,
    SUPPORT_TYPES,
    load_leaves,
    parse_and_validate,
)
from wiki_leaf_support_probe import (
    SYSTEM_PROMPT as LEAF_SYSTEM_PROMPT,
)
from wiki_leaf_support_probe import (
    build_messages as build_leaf_messages,
)
from wiki_root_exclusion_probe import (
    SYSTEM_PROMPT as ROOT_SYSTEM_PROMPT,
)
from wiki_root_exclusion_probe import (
    build_messages as build_root_messages,
)
from wiki_root_exclusion_probe import (
    parse_and_validate as parse_root_response,
)

SNAPSHOT_ID = "s_6b787258ba7d700c"
EXPECTED_DOCUMENT_COUNT = 15
EXPECTED_BLOCK_COUNT = 526
EXPECTED_ROOT_COUNT = 24
EXPECTED_LEAF_COUNT = 620
ATTACHMENT_SCHEMA_VERSION = "block-leaf-attachment-v1"
ROOT_PROMPT_VERSION = "root-skeleton-l3-v1"
LEAF_PROMPT_VERSION = "leaf-evidence-presence-v1"
MODEL_TEMPERATURE = 0.0
DEFAULT_ROOT_CONCURRENCY = 25
DEFAULT_LEAF_CONCURRENCY = 20
MAX_REQUEST_ATTEMPTS = 3
RETRY_BASE_SECONDS = 2.0

LOCAL_APP_DATA_ROOT = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
DATA_ROOT = LOCAL_APP_DATA_ROOT / "com.lipeidong.aistudyhelper"
SNAPSHOT_ROOT = DATA_ROOT / "collections" / SNAPSHOT_ID
ATTACHMENT_ROOT = DATA_ROOT / "wiki-leaf-attachments" / SNAPSHOT_ID
FOREST_PATH = BACKEND_ROOT / "resources" / "role_knowledge_forest.json"


@dataclass(frozen=True, slots=True)
class InputBlock:
    """一个已校验的原始 block 及其来源文档。"""

    block_id: str
    document_id: str
    section_path: list[str]
    text: str
    content_hash: str

    def model_payload(self) -> dict[str, Any]:
        """返回挂接模型所需的 block 字段。"""

        return {
            "block_id": self.block_id,
            "section_path": self.section_path,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class Inventory:
    """固定快照的确定性输入清单。"""

    manifest_sha256: str
    blocks: tuple[InputBlock, ...]
    inventory_sha256: str


@dataclass(frozen=True, slots=True)
class Framework:
    """运行所需的角色知识框架视图。"""

    forest_sha256: str
    revision_id: str
    roots: tuple[dict[str, Any], ...]
    leaves: dict[str, dict[str, Any]]


def utc_now() -> str:
    """返回 UTC ISO 时间戳。"""

    return datetime.now(UTC).isoformat()


def sha256_bytes(payload: bytes) -> str:
    """计算字节内容 SHA-256。"""

    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    """流式计算文件 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    """生成用于稳定 hash 的规范 JSON。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(prefix: str, *parts: str) -> str:
    """按逻辑键生成稳定短 ID。"""

    digest = sha256_bytes("\0".join(parts).encode("utf-8"))[:24]
    return f"{prefix}_{digest}"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """以同目录临时文件原子替换 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL；中断留下的截断末行不作为已完成记录。"""

    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


class JsonlAppender:
    """串行 flush 阶段结果，避免并发任务交错写入。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle = path.open("a", encoding="utf-8", newline="\n")
        self._pending_sync = 0

    def append(self, payload: dict[str, Any]) -> None:
        """追加一条完整 JSON 记录。"""

        self._handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._handle.flush()
        self._pending_sync += 1
        if self._pending_sync >= 50:
            os.fsync(self._handle.fileno())
            self._pending_sync = 0

    def close(self) -> None:
        """关闭文件前确保已经落盘。"""

        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()


def load_inventory(snapshot_root: Path = SNAPSHOT_ROOT) -> Inventory:
    """严格校验 manifest、15 篇文档、526 个 block 和文件 hash。"""

    manifest_path = snapshot_root / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("snapshot_id") != SNAPSHOT_ID:
        raise RuntimeError("snapshot manifest ID mismatch")
    if manifest.get("status") != "ready" or manifest.get("completeness") != "complete":
        raise RuntimeError("snapshot is not ready and complete")
    documents = manifest.get("documents")
    if not isinstance(documents, list) or len(documents) != EXPECTED_DOCUMENT_COUNT:
        raise RuntimeError("snapshot must contain exactly 15 documents")
    if manifest.get("failed_documents") != []:
        raise RuntimeError("snapshot contains failed documents")
    if manifest.get("totals", {}).get("blocks") != EXPECTED_BLOCK_COUNT:
        raise RuntimeError("snapshot manifest block total mismatch")

    blocks: list[InputBlock] = []
    seen_block_ids: set[str] = set()
    seen_doc_ids: set[str] = set()
    inventory_entries: list[dict[str, Any]] = []
    for document in documents:
        document_id = document["doc_id"]
        document_version = document["doc_version"]
        if document_id in seen_doc_ids:
            raise RuntimeError(f"duplicate document ID: {document_id}")
        seen_doc_ids.add(document_id)
        block_artifact = next(
            (
                artifact
                for artifact in document.get("artifact_refs", [])
                if artifact.get("kind") == "block_set"
            ),
            None,
        )
        if not isinstance(block_artifact, dict):
            raise TypeError(f"document has no block_set artifact: {document_id}")
        block_path = snapshot_root / block_artifact["snapshot_ref"]
        actual_hash = sha256_file(block_path)
        if actual_hash != block_artifact["sha256"]:
            raise RuntimeError(f"block_set hash mismatch: {document_id}")
        block_set = json.loads(block_path.read_text(encoding="utf-8"))
        if block_set.get("doc_id") != document_id:
            raise RuntimeError(f"block_set document ID mismatch: {document_id}")
        if block_set.get("doc_version") != document_version:
            raise RuntimeError(f"block_set document version mismatch: {document_id}")
        document_blocks = block_set.get("blocks")
        if not isinstance(document_blocks, list) or len(document_blocks) != document["block_count"]:
            raise RuntimeError(f"block count mismatch: {document_id}")

        for block in document_blocks:
            block_id = block.get("block_id")
            section_path = block.get("section_path")
            text = block.get("text")
            content_hash = block.get("content_hash")
            if not isinstance(block_id, str) or not block_id or block_id in seen_block_ids:
                raise RuntimeError(f"invalid or duplicate block ID: {block_id}")
            if (
                not isinstance(section_path, list)
                or not all(isinstance(part, str) for part in section_path)
                or not isinstance(text, str)
                or not text
                or not isinstance(content_hash, str)
            ):
                raise RuntimeError(f"invalid block payload: {block_id}")
            seen_block_ids.add(block_id)
            blocks.append(
                InputBlock(
                    block_id=block_id,
                    document_id=document_id,
                    section_path=section_path,
                    text=text,
                    content_hash=content_hash,
                )
            )
            inventory_entries.append(
                {
                    "document_id": document_id,
                    "document_version": document_version,
                    "block_id": block_id,
                    "content_hash": content_hash,
                }
            )

    if len(blocks) != EXPECTED_BLOCK_COUNT:
        raise RuntimeError("enumerated block count does not match 526")
    inventory_hash = sha256_bytes(canonical_json(inventory_entries).encode("utf-8"))
    return Inventory(
        manifest_sha256=sha256_bytes(manifest_bytes),
        blocks=tuple(blocks),
        inventory_sha256=inventory_hash,
    )


def load_framework(forest_path: Path = FOREST_PATH) -> Framework:
    """加载并校验 24 个一级分支和 620 个叶节点。"""

    forest_bytes = forest_path.read_bytes()
    forest = json.loads(forest_bytes)
    if forest.get("status") != "complete" or forest.get("missing_node_ids") != []:
        raise RuntimeError("role knowledge forest is not complete")
    roots = forest.get("roots")
    if not isinstance(roots, list) or len(roots) != EXPECTED_ROOT_COUNT:
        raise RuntimeError("role knowledge forest must contain 24 roots")
    root_ids = [root.get("node_id") for root in roots]
    if len(root_ids) != len(set(root_ids)) or not all(isinstance(item, str) for item in root_ids):
        raise RuntimeError("role knowledge forest has invalid root IDs")
    leaves = load_leaves(forest)
    if len(leaves) != EXPECTED_LEAF_COUNT:
        raise RuntimeError(f"role knowledge forest leaf count mismatch: {len(leaves)}")
    forest_hash = framework_content_sha256(forest_bytes)
    return Framework(
        forest_sha256=forest_hash,
        revision_id=f"kf_{forest_hash[:20]}",
        roots=tuple(roots),
        leaves=leaves,
    )


def create_provider() -> DeepSeekChatProvider:
    """创建工作目录中固定参数的 DeepSeek provider。"""

    api_key = WindowsCredentialStore().get(CHAT_API_KEY_USERNAME)
    if not api_key:
        raise RuntimeError("chat API key is not configured in Windows Credential Manager")
    return DeepSeekChatProvider(
        api_key,
        profile=ChatProviderProfile(
            provider="deepseek",
            model_id=MODEL_ID,
            base_url=DEEPSEEK_BASE_URL,
            temperature=MODEL_TEMPERATURE,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            thinking_enabled=False,
        ),
    )


async def complete_once(
    provider: DeepSeekChatProvider,
    semaphore: asyncio.Semaphore,
    messages: tuple[ChatMessage, ...],
) -> tuple[ChatCompletionResult | None, str | None, bool]:
    """执行一次模型调用，并返回错误是否值得重试。"""

    try:
        async with semaphore:
            result = await provider.complete(messages, max_output_tokens=MAX_OUTPUT_TOKENS)
        return result, None, False
    except ChatProviderError as error:
        return None, f"{error.code}:{error.message}", error.retryable
    except Exception as error:  # noqa: BLE001 - 未知失败也必须显式保留，不能伪装成不支持。
        return None, f"{type(error).__name__}:{error}", True


def root_ids_for_leaf(leaf_id: str) -> str:
    """从稳定叶节点编号提取所属一级分支。"""

    return leaf_id.split(".", 1)[0]


def normalize_quote_with_offsets(text: str) -> tuple[str, list[int]]:
    """归一化 quote 排版，并让每个归一化字符可映射回原下标。"""

    characters: list[str] = []
    offsets: list[int] = []
    for index, character in enumerate(text):
        normalized = unicodedata.normalize("NFKC", character).casefold()
        if normalized.isspace():
            if characters and characters[-1] != " ":
                characters.append(" ")
                offsets.append(index)
            continue
        for normalized_character in normalized:
            characters.append(normalized_character)
            offsets.append(index)
    while characters and characters[-1] == " ":
        characters.pop()
        offsets.pop()
    return "".join(characters), offsets


def canonicalize_evidence_quotes(parsed: dict[str, Any], block_text: str) -> dict[str, Any]:
    """把仅排版不同的 quote 无损映射回 block 中的连续原文。"""

    evidence = parsed.get("evidence")
    if not isinstance(evidence, list):
        return parsed
    normalized_block, block_offsets = normalize_quote_with_offsets(block_text)
    canonical_evidence: list[Any] = []
    for item in evidence:
        if not isinstance(item, dict) or not isinstance(item.get("quote"), str):
            canonical_evidence.append(item)
            continue
        quote = item["quote"]
        if quote and quote not in block_text:
            quote_for_matching = re.sub(r"\\[nrt]", " ", quote)
            normalized_quote, _ = normalize_quote_with_offsets(quote_for_matching)
            match_start = normalized_block.find(normalized_quote) if normalized_quote else -1
            while match_start < 0 and normalized_quote:
                trimmed_quote = normalized_quote[:-1].rstrip()
                if trimmed_quote == normalized_quote:
                    break
                normalized_quote = trimmed_quote
                match_start = normalized_block.find(normalized_quote)
            if match_start >= 0:
                match_end = match_start + len(normalized_quote)
                source_start = block_offsets[match_start]
                source_end = block_offsets[match_end - 1] + 1
                item = {**item, "quote": block_text[source_start:source_end]}
        canonical_evidence.append(item)
    return {**parsed, "evidence": canonical_evidence}


async def run_root_preflight_one(
    provider: DeepSeekChatProvider,
    semaphore: asyncio.Semaphore,
    block: InputBlock,
    roots: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """对一个 block 执行一级树初筛；失败时保守保留全部一级树。"""

    expected_root_ids = frozenset(root["node_id"] for root in roots)
    messages = build_root_messages(block.model_payload(), list(roots), detail_mode="skeleton-l3")
    started_at = utc_now()
    errors: list[str] = []
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_hit_tokens = 0
    total_cache_miss_tokens = 0
    last_result: ChatCompletionResult | None = None
    for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
        result, request_error, retryable = await complete_once(provider, semaphore, messages)
        if result is None:
            errors.append(f"attempt={attempt}:{request_error}")
            if not retryable:
                break
        else:
            last_result = result
            total_input_tokens += result.input_tokens
            total_output_tokens += result.output_tokens
            total_cache_hit_tokens += result.cache_hit_tokens
            total_cache_miss_tokens += result.cache_miss_tokens
            validation = parse_root_response(
                result.content,
                expected_root_ids=expected_root_ids,
                expected_retained_root_ids=expected_root_ids,
            )
            retained_root_ids = validation.get("retained_ids", [])
            if validation.get("valid") is True:
                return {
                    "block_id": block.block_id,
                    "status": "valid",
                    "attempt": attempt,
                    "started_at": started_at,
                    "finished_at": utc_now(),
                    "retained_root_ids": retained_root_ids,
                    "raw_content": result.content,
                    "request_id": result.request_id,
                    "actual_model_id": result.actual_model_id,
                    "finish_reason": result.finish_reason,
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                    "cache_hit_tokens": total_cache_hit_tokens,
                    "cache_miss_tokens": total_cache_miss_tokens,
                    "errors": errors,
                }
            errors.append(
                f"attempt={attempt}:invalid_response:{validation.get('parse_error', 'contract')}"
            )
        if attempt < MAX_REQUEST_ATTEMPTS:
            await asyncio.sleep(RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
    return {
        "block_id": block.block_id,
        "status": "fallback_all_roots",
        "attempt": len(errors),
        "started_at": started_at,
        "finished_at": utc_now(),
        "retained_root_ids": sorted(expected_root_ids),
        "raw_content": last_result.content if last_result is not None else None,
        "request_id": last_result.request_id if last_result is not None else None,
        "actual_model_id": last_result.actual_model_id if last_result is not None else None,
        "finish_reason": last_result.finish_reason if last_result is not None else None,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "cache_hit_tokens": total_cache_hit_tokens,
        "cache_miss_tokens": total_cache_miss_tokens,
        "errors": errors,
    }


async def run_leaf_verdict_one(
    provider: DeepSeekChatProvider,
    semaphore: asyncio.Semaphore,
    framework_revision_id: str,
    block: InputBlock,
    leaf: dict[str, Any],
) -> dict[str, Any]:
    """判定一个 block 对一个叶节点的支持关系，并校验 evidence。"""

    messages = build_leaf_messages(block.model_payload(), leaf, LEAF_SYSTEM_PROMPT)
    started_at = utc_now()
    errors: list[str] = []
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_hit_tokens = 0
    total_cache_miss_tokens = 0
    last_result: ChatCompletionResult | None = None
    for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
        result, request_error, retryable = await complete_once(provider, semaphore, messages)
        if result is None:
            errors.append(f"attempt={attempt}:{request_error}")
            if not retryable:
                break
        else:
            last_result = result
            total_input_tokens += result.input_tokens
            total_output_tokens += result.output_tokens
            total_cache_hit_tokens += result.cache_hit_tokens
            total_cache_miss_tokens += result.cache_miss_tokens
            try:
                model_payload = json.loads(result.content)
            except json.JSONDecodeError:
                model_payload = None
            if isinstance(model_payload, dict):
                result_for_validation = ChatCompletionResult(
                    content=json.dumps(
                        canonicalize_evidence_quotes(model_payload, block.text),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    request_id=result.request_id,
                    actual_model_id=result.actual_model_id,
                    finish_reason=result.finish_reason,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cache_hit_tokens=result.cache_hit_tokens,
                    cache_miss_tokens=result.cache_miss_tokens,
                )
            else:
                result_for_validation = result
            validation = parse_and_validate(
                result_for_validation.content,
                block_text=block.text,
                expected_supported=False,
                required_types=(),
                allowed_extra_types=(),
                allowed_support_types=SUPPORT_TYPES,
                require_quote_grounding=True,
            )
            if (
                validation.get("valid") is True
                and validation.get("response_contract") == "evidence_presence"
            ):
                evidence = validation["parsed"]["evidence"]
                augmented_evidence = [
                    {
                        **item,
                        "quote_sha256": sha256_bytes(item["quote"].encode("utf-8")),
                    }
                    for item in evidence
                ]
                return {
                    "framework_revision_id": framework_revision_id,
                    "block_id": block.block_id,
                    "leaf_id": leaf["leaf_id"],
                    "status": "completed",
                    "attempt": attempt,
                    "started_at": started_at,
                    "finished_at": utc_now(),
                    "supported": bool(evidence),
                    "evidence": augmented_evidence,
                    "raw_content": result.content,
                    "request_id": result.request_id,
                    "actual_model_id": result.actual_model_id,
                    "finish_reason": result.finish_reason,
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                    "cache_hit_tokens": total_cache_hit_tokens,
                    "cache_miss_tokens": total_cache_miss_tokens,
                    "errors": errors,
                }
            errors.append(
                f"attempt={attempt}:invalid_response:{validation.get('parse_error', 'contract')}"
            )
        if attempt < MAX_REQUEST_ATTEMPTS:
            await asyncio.sleep(RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
    return {
        "framework_revision_id": framework_revision_id,
        "block_id": block.block_id,
        "leaf_id": leaf["leaf_id"],
        "status": "failed",
        "attempt": len(errors),
        "started_at": started_at,
        "finished_at": utc_now(),
        "supported": None,
        "evidence": [],
        "raw_content": last_result.content if last_result is not None else None,
        "request_id": last_result.request_id if last_result is not None else None,
        "actual_model_id": last_result.actual_model_id if last_result is not None else None,
        "finish_reason": last_result.finish_reason if last_result is not None else None,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "cache_hit_tokens": total_cache_hit_tokens,
        "cache_miss_tokens": total_cache_miss_tokens,
        "errors": errors,
    }


def latest_by_key(
    records: Iterable[dict[str, Any]], keys: tuple[str, ...]
) -> dict[tuple[Any, ...], dict[str, Any]]:
    """按逻辑键保留最后一条 stage 记录。"""

    latest: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in records:
        latest[tuple(record.get(key) for key in keys)] = record
    return latest


def repair_stored_leaf_record(record: dict[str, Any], block: InputBlock) -> dict[str, Any] | None:
    """用已保存的原始模型输出重新校验可无损修复的证据。"""

    raw_content = record.get("raw_content")
    if record.get("status") == "completed" or not isinstance(raw_content, str):
        return None
    try:
        payload = json.loads(raw_content)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    repaired_content = json.dumps(
        canonicalize_evidence_quotes(payload, block.text),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    validation = parse_and_validate(
        repaired_content,
        block_text=block.text,
        expected_supported=False,
        required_types=(),
        allowed_extra_types=(),
        allowed_support_types=SUPPORT_TYPES,
        require_quote_grounding=True,
    )
    if (
        validation.get("valid") is not True
        or validation.get("response_contract") != "evidence_presence"
    ):
        return None
    evidence = validation["parsed"]["evidence"]
    return {
        **record,
        "status": "completed",
        "supported": bool(evidence),
        "evidence": [
            {
                **item,
                "quote_sha256": sha256_bytes(item["quote"].encode("utf-8")),
            }
            for item in evidence
        ],
        "repaired_from_stored_output": True,
        "finished_at": utc_now(),
    }


def run_manifest_base(
    attachment_run_id: str,
    inventory: Inventory,
    framework: Framework,
) -> dict[str, Any]:
    """构造运行 manifest 的固定绑定字段。"""

    prompt_dir = ATTACHMENT_ROOT / attachment_run_id / "prompts"
    return {
        "schema_version": ATTACHMENT_SCHEMA_VERSION,
        "attachment_run_id": attachment_run_id,
        "status": "building",
        "created_at": utc_now(),
        "sealed_at": None,
        "snapshot_id": SNAPSHOT_ID,
        "snapshot_manifest_sha256": inventory.manifest_sha256,
        "document_count": EXPECTED_DOCUMENT_COUNT,
        "block_count": len(inventory.blocks),
        "input_inventory_sha256": inventory.inventory_sha256,
        "framework_path": str(FOREST_PATH),
        "framework_revision_id": framework.revision_id,
        "framework_sha256": framework.forest_sha256,
        "leaf_count": len(framework.leaves),
        "root_count": len(framework.roots),
        "leaf_prompt_version": LEAF_PROMPT_VERSION,
        "leaf_prompt_sha256": sha256_bytes(LEAF_SYSTEM_PROMPT.encode("utf-8")),
        "root_prompt_version": ROOT_PROMPT_VERSION,
        "root_prompt_sha256": sha256_bytes(ROOT_SYSTEM_PROMPT.encode("utf-8")),
        "root_prompt_mode": "skeleton-l3",
        "model_id": MODEL_ID,
        "temperature": MODEL_TEMPERATURE,
        "thinking_enabled": False,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "root_concurrency": DEFAULT_ROOT_CONCURRENCY,
        "leaf_concurrency": DEFAULT_LEAF_CONCURRENCY,
        "max_request_attempts": MAX_REQUEST_ATTEMPTS,
        "prompt_dir": str(prompt_dir),
        "counts": {},
        "ledger_files": {},
        "diagnostic_files": {},
    }


def create_run_dir(attachment_run_id: str) -> Path:
    """创建运行目录和只写一次的 prompt 诊断文件。"""

    run_dir = ATTACHMENT_ROOT / attachment_run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    prompt_dir = run_dir / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "leaf-system.txt").write_text(LEAF_SYSTEM_PROMPT, encoding="utf-8")
    (prompt_dir / "root-prefilter-system.txt").write_text(ROOT_SYSTEM_PROMPT, encoding="utf-8")
    (run_dir / "staging").mkdir()
    return run_dir


def open_existing_run(
    attachment_run_id: str,
    inventory: Inventory,
    framework: Framework,
) -> tuple[Path, dict[str, Any]]:
    """打开已有 building／partial 运行用于续跑。"""

    run_dir = ATTACHMENT_ROOT / attachment_run_id
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists() or not (run_dir / "staging").exists():
        raise RuntimeError(f"resumable attachment run not found: {run_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") == "sealed_complete":
        raise RuntimeError("sealed attachment run is immutable and cannot resume")
    expected_bindings = {
        "schema_version": ATTACHMENT_SCHEMA_VERSION,
        "snapshot_id": SNAPSHOT_ID,
        "snapshot_manifest_sha256": inventory.manifest_sha256,
        "input_inventory_sha256": inventory.inventory_sha256,
        "framework_revision_id": framework.revision_id,
        "framework_sha256": framework.forest_sha256,
        "leaf_prompt_sha256": sha256_bytes(LEAF_SYSTEM_PROMPT.encode("utf-8")),
        "root_prompt_sha256": sha256_bytes(ROOT_SYSTEM_PROMPT.encode("utf-8")),
        "model_id": MODEL_ID,
        "temperature": MODEL_TEMPERATURE,
        "thinking_enabled": False,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }
    mismatches = {
        key: {"expected": expected, "actual": manifest.get(key)}
        for key, expected in expected_bindings.items()
        if manifest.get(key) != expected
    }
    if mismatches:
        raise RuntimeError(f"attachment run binding mismatch: {mismatches}")
    return run_dir, manifest


async def execute_root_stage(
    run_dir: Path,
    provider: DeepSeekChatProvider,
    inventory: Inventory,
    framework: Framework,
) -> dict[str, Any]:
    """执行或续跑全部 block 的一级树初筛。"""

    path = run_dir / "staging" / "root-preflight.jsonl"
    latest = latest_by_key(load_jsonl(path), ("block_id",))
    completed = {
        block_id[0]: record
        for block_id, record in latest.items()
        if record.get("status") == "valid"
    }
    pending = [block for block in inventory.blocks if block.block_id not in completed]
    semaphore = asyncio.Semaphore(DEFAULT_ROOT_CONCURRENCY)
    appender = JsonlAppender(path)
    try:
        tasks = [
            asyncio.create_task(run_root_preflight_one(provider, semaphore, block, framework.roots))
            for block in pending
        ]
        completed_count = len(completed)
        for task in asyncio.as_completed(tasks):
            record = await task
            appender.append(record)
            completed_count += 1
            if (
                completed_count == 1
                or completed_count % 25 == 0
                or completed_count == len(inventory.blocks)
            ):
                print(
                    f"root preflight {completed_count}/{len(inventory.blocks)}",
                    flush=True,
                )
    finally:
        appender.close()

    latest = latest_by_key(load_jsonl(path), ("block_id",))
    if len(latest) != len(inventory.blocks):
        raise RuntimeError("root preflight did not cover every block")
    root_counts = Counter(len(record.get("retained_root_ids", [])) for record in latest.values())
    fallback_count = sum(record.get("status") == "fallback_all_roots" for record in latest.values())
    estimated_leaf_calls = sum(
        sum(leaf_id.startswith(root_id + ".") for leaf_id in framework.leaves)
        for record in latest.values()
        for root_id in record.get("retained_root_ids", [])
    )
    return {
        "block_count": len(latest),
        "fallback_block_count": fallback_count,
        "retained_root_count_distribution": {
            str(key): value for key, value in sorted(root_counts.items())
        },
        "estimated_candidate_leaf_calls": estimated_leaf_calls,
        "input_tokens": sum(record.get("input_tokens", 0) for record in latest.values()),
        "output_tokens": sum(record.get("output_tokens", 0) for record in latest.values()),
        "cache_hit_tokens": sum(record.get("cache_hit_tokens", 0) for record in latest.values()),
        "cache_miss_tokens": sum(record.get("cache_miss_tokens", 0) for record in latest.values()),
    }


def build_candidate_pairs(
    inventory: Inventory,
    framework: Framework,
    root_records: dict[tuple[Any, ...], dict[str, Any]],
) -> list[tuple[InputBlock, dict[str, Any]]]:
    """根据一级初筛结果生成全部 block × leaf 候选对。"""

    leaves_by_root: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for leaf in framework.leaves.values():
        leaves_by_root[root_ids_for_leaf(leaf["leaf_id"])].append(leaf)
    pairs: list[tuple[InputBlock, dict[str, Any]]] = []
    for block in inventory.blocks:
        root_record = root_records.get((block.block_id,))
        if root_record is None:
            raise RuntimeError(f"missing root preflight: {block.block_id}")
        for root_id in sorted(root_record["retained_root_ids"]):
            for leaf in sorted(leaves_by_root[root_id], key=lambda item: item["leaf_id"]):
                pairs.append((block, leaf))
    return pairs


async def execute_leaf_stage(
    run_dir: Path,
    provider: DeepSeekChatProvider,
    inventory: Inventory,
    framework: Framework,
) -> dict[str, Any]:
    """执行或续跑全部候选 block × leaf 判定。"""

    root_path = run_dir / "staging" / "root-preflight.jsonl"
    root_records = latest_by_key(load_jsonl(root_path), ("block_id",))
    pairs = build_candidate_pairs(inventory, framework, root_records)
    leaf_path = run_dir / "staging" / "leaf-verdicts.jsonl"
    semaphore = asyncio.Semaphore(DEFAULT_LEAF_CONCURRENCY)
    appender = JsonlAppender(leaf_path)
    try:
        blocks_by_id = {block.block_id: block for block in inventory.blocks}
        latest = latest_by_key(load_jsonl(leaf_path), ("block_id", "leaf_id"))
        repaired_count = 0
        for record in latest.values():
            repaired = repair_stored_leaf_record(record, blocks_by_id[record["block_id"]])
            if repaired is not None:
                appender.append(repaired)
                repaired_count += 1
        if repaired_count:
            print(f"repaired stored leaf verdicts {repaired_count}", flush=True)
        latest = latest_by_key(load_jsonl(leaf_path), ("block_id", "leaf_id"))
        completed = {key for key, record in latest.items() if record.get("status") == "completed"}
        pending = [
            (block, leaf)
            for block, leaf in pairs
            if (block.block_id, leaf["leaf_id"]) not in completed
        ]
        completed_count = len(completed)
        batch_size = DEFAULT_LEAF_CONCURRENCY * 4
        for start in range(0, len(pending), batch_size):
            batch = pending[start : start + batch_size]
            records = await asyncio.gather(
                *(
                    run_leaf_verdict_one(provider, semaphore, framework.revision_id, block, leaf)
                    for block, leaf in batch
                )
            )
            for record in records:
                appender.append(record)
                completed_count += 1
            if completed_count % 100 < len(records) or completed_count == len(pairs):
                print(f"leaf verdict {completed_count}/{len(pairs)}", flush=True)
    finally:
        appender.close()

    latest = latest_by_key(load_jsonl(leaf_path), ("block_id", "leaf_id"))
    expected_keys = {(block.block_id, leaf["leaf_id"]) for block, leaf in pairs}
    failed = [key for key in expected_keys if latest.get(key, {}).get("status") != "completed"]
    completed_records = [
        latest.get(key) for key in expected_keys if latest.get(key, {}).get("status") == "completed"
    ]
    return {
        "candidate_leaf_call_count": len(pairs),
        "completed_candidate_leaf_call_count": len(pairs) - len(failed),
        "failed_candidate_leaf_call_count": len(failed),
        "supported_relationship_count": sum(
            record.get("supported") is True for record in completed_records if record is not None
        ),
        "input_tokens": sum(
            record.get("input_tokens", 0) for record in completed_records if record
        ),
        "output_tokens": sum(
            record.get("output_tokens", 0) for record in completed_records if record
        ),
        "cache_hit_tokens": sum(
            record.get("cache_hit_tokens", 0) for record in completed_records if record
        ),
        "cache_miss_tokens": sum(
            record.get("cache_miss_tokens", 0) for record in completed_records if record
        ),
    }


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> str:
    """稳定写入 JSONL 并返回内容 hash。"""

    content = "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows
    )
    path.write_text(content, encoding="utf-8", newline="\n")
    return sha256_bytes(content.encode("utf-8"))


def finalize_run(
    run_dir: Path,
    inventory: Inventory,
    framework: Framework,
    attachment_run_id: str,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """校验覆盖并封存账本；只有完整时发布活动指针。"""

    root_records = latest_by_key(
        load_jsonl(run_dir / "staging" / "root-preflight.jsonl"), ("block_id",)
    )
    leaf_records = latest_by_key(
        load_jsonl(run_dir / "staging" / "leaf-verdicts.jsonl"), ("block_id", "leaf_id")
    )
    pairs = build_candidate_pairs(inventory, framework, root_records)
    expected_pair_keys = {(block.block_id, leaf["leaf_id"]) for block, leaf in pairs}
    failed_pairs = [
        key for key in expected_pair_keys if leaf_records.get(key, {}).get("status") != "completed"
    ]

    conflicts: list[dict[str, Any]] = []
    for block_key, record in sorted(root_records.items()):
        if record.get("status") != "valid":
            block_id = block_key[0]
            conflicts.append(
                {
                    "conflict_id": stable_id("blc", "root_fallback", block_id),
                    "kind": "root_preflight_fallback",
                    "block_id": block_id,
                    "detail": record.get("errors", []),
                }
            )
    for block_id, leaf_id in sorted(failed_pairs):
        record = leaf_records.get((block_id, leaf_id), {})
        conflicts.append(
            {
                "conflict_id": stable_id("blc", "leaf_failed", block_id, leaf_id),
                "kind": "leaf_verdict_unresolved",
                "block_id": block_id,
                "leaf_id": leaf_id,
                "detail": record.get("errors", ["missing staged record"]),
            }
        )

    supports: list[dict[str, Any]] = []
    supported_by_block: Counter[str] = Counter()
    supported_by_leaf: Counter[str] = Counter()
    for (block_id, leaf_id), record in sorted(leaf_records.items()):
        if record.get("status") != "completed" or record.get("supported") is not True:
            continue
        evidence = record["evidence"]
        if not evidence:
            raise RuntimeError(f"supported verdict has empty evidence: {block_id} / {leaf_id}")
        supports.append(
            {
                "support_id": stable_id("bls", framework.revision_id, block_id, leaf_id),
                "snapshot_id": SNAPSHOT_ID,
                "framework_revision_id": framework.revision_id,
                "block_id": block_id,
                "leaf_id": leaf_id,
                "evidence": evidence,
            }
        )
        supported_by_block[block_id] += 1
        supported_by_leaf[leaf_id] += 1

    candidate_by_block: Counter[str] = Counter()
    candidate_by_leaf: Counter[str] = Counter()
    for block, leaf in pairs:
        candidate_by_block[block.block_id] += 1
        candidate_by_leaf[leaf["leaf_id"]] += 1

    block_coverage = []
    for block in inventory.blocks:
        root_record = root_records[(block.block_id,)]
        retained_root_ids = root_record["retained_root_ids"]
        evaluated = sum(
            leaf_records.get((block.block_id, leaf_id), {}).get("status") == "completed"
            for leaf_id in framework.leaves
            if root_ids_for_leaf(leaf_id) in retained_root_ids
        )
        candidate_count = candidate_by_block[block.block_id]
        block_coverage.append(
            {
                "block_id": block.block_id,
                "document_id": block.document_id,
                "retained_root_ids": retained_root_ids,
                "root_preflight_status": root_record["status"],
                "candidate_leaf_count": candidate_count,
                "evaluated_leaf_count": evaluated,
                "supported_leaf_count": supported_by_block[block.block_id],
                "is_no_support": evaluated == candidate_count
                and supported_by_block[block.block_id] == 0,
                "is_complete": evaluated == candidate_count,
            }
        )

    leaf_coverage = []
    for leaf_id, leaf in sorted(framework.leaves.items()):
        candidate_count = candidate_by_leaf[leaf_id]
        evaluated = sum(
            leaf_records.get((block.block_id, leaf_id), {}).get("status") == "completed"
            for block in inventory.blocks
            if root_ids_for_leaf(leaf_id) in root_records[(block.block_id,)]["retained_root_ids"]
        )
        leaf_coverage.append(
            {
                "leaf_id": leaf_id,
                "path": leaf["path"],
                "candidate_block_count": candidate_count,
                "evaluated_block_count": evaluated,
                "supporting_block_count": supported_by_leaf[leaf_id],
                "is_empty": evaluated == candidate_count and supported_by_leaf[leaf_id] == 0,
                "is_complete": evaluated == candidate_count,
            }
        )

    ledger_hashes = {
        "supports.jsonl": write_jsonl(run_dir / "supports.jsonl", supports),
        "block-coverage.jsonl": write_jsonl(run_dir / "block-coverage.jsonl", block_coverage),
        "leaf-coverage.jsonl": write_jsonl(run_dir / "leaf-coverage.jsonl", leaf_coverage),
        "conflicts.jsonl": write_jsonl(run_dir / "conflicts.jsonl", conflicts),
    }
    diagnostic_hashes = {
        "staging/root-preflight.jsonl": sha256_file(run_dir / "staging" / "root-preflight.jsonl"),
        "staging/leaf-verdicts.jsonl": sha256_file(run_dir / "staging" / "leaf-verdicts.jsonl"),
    }
    counts = {
        "block_coverage_count": len(block_coverage),
        "leaf_coverage_count": len(leaf_coverage),
        "candidate_leaf_call_count": len(pairs),
        "completed_candidate_leaf_call_count": len(pairs) - len(failed_pairs),
        "supported_relationship_count": len(supports),
        "no_support_block_count": sum(row["is_no_support"] for row in block_coverage),
        "empty_leaf_count": sum(row["is_empty"] for row in leaf_coverage),
        "root_fallback_block_count": sum(
            record.get("status") == "fallback_all_roots" for record in root_records.values()
        ),
        "conflict_count": len(conflicts),
        "root_input_tokens": sum(record.get("input_tokens", 0) for record in root_records.values()),
        "root_output_tokens": sum(
            record.get("output_tokens", 0) for record in root_records.values()
        ),
        "root_cache_hit_tokens": sum(
            record.get("cache_hit_tokens", 0) for record in root_records.values()
        ),
        "root_cache_miss_tokens": sum(
            record.get("cache_miss_tokens", 0) for record in root_records.values()
        ),
        "leaf_input_tokens": sum(record.get("input_tokens", 0) for record in leaf_records.values()),
        "leaf_output_tokens": sum(
            record.get("output_tokens", 0) for record in leaf_records.values()
        ),
        "leaf_cache_hit_tokens": sum(
            record.get("cache_hit_tokens", 0) for record in leaf_records.values()
        ),
        "leaf_cache_miss_tokens": sum(
            record.get("cache_miss_tokens", 0) for record in leaf_records.values()
        ),
    }
    is_sealed = not failed_pairs
    sealed_manifest = {
        **manifest,
        "status": "sealed_complete" if is_sealed else "partial",
        "sealed_at": utc_now() if is_sealed else None,
        "counts": counts,
        "ledger_files": ledger_hashes,
        "diagnostic_files": diagnostic_hashes,
    }
    manifest_path = run_dir / "manifest.json"
    atomic_write_json(manifest_path, sealed_manifest)
    if is_sealed:
        active_pointer = {
            "snapshot_id": SNAPSHOT_ID,
            "attachment_run_id": attachment_run_id,
            "framework_revision_id": framework.revision_id,
            "manifest_sha256": sha256_file(manifest_path),
            "published_at": utc_now(),
        }
        atomic_write_json(ATTACHMENT_ROOT / "active-attachment.json", active_pointer)
    return sealed_manifest


def parse_args() -> argparse.Namespace:
    """解析运行阶段参数。"""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--stage",
        choices=("inventory", "roots", "leaves", "seal", "all"),
        default="all",
    )
    parser.add_argument("--run-id", help="续跑已有 attachment_run_id")
    parser.add_argument("--root-concurrency", type=int, default=DEFAULT_ROOT_CONCURRENCY)
    parser.add_argument("--leaf-concurrency", type=int, default=DEFAULT_LEAF_CONCURRENCY)
    args = parser.parse_args()
    if args.root_concurrency <= 0 or args.leaf_concurrency <= 0:
        parser.error("concurrency values must be positive")
    return args


async def main() -> None:
    """执行指定的全量挂接阶段。"""

    global DEFAULT_ROOT_CONCURRENCY, DEFAULT_LEAF_CONCURRENCY
    args = parse_args()
    DEFAULT_ROOT_CONCURRENCY = args.root_concurrency
    DEFAULT_LEAF_CONCURRENCY = args.leaf_concurrency
    inventory = load_inventory()
    framework = load_framework()

    if args.stage == "inventory":
        print(
            json.dumps(
                {
                    "snapshot_id": SNAPSHOT_ID,
                    "document_count": EXPECTED_DOCUMENT_COUNT,
                    "block_count": len(inventory.blocks),
                    "manifest_sha256": inventory.manifest_sha256,
                    "inventory_sha256": inventory.inventory_sha256,
                    "framework_revision_id": framework.revision_id,
                    "root_count": len(framework.roots),
                    "leaf_count": len(framework.leaves),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.run_id:
        run_dir, manifest = open_existing_run(args.run_id, inventory, framework)
        attachment_run_id = args.run_id
    else:
        attachment_run_id = datetime.now(UTC).strftime("att_%Y%m%dT%H%M%S%fZ")
        run_dir = create_run_dir(attachment_run_id)
        manifest = run_manifest_base(attachment_run_id, inventory, framework)
        atomic_write_json(run_dir / "manifest.json", manifest)

    provider = create_provider()
    if args.stage in {"roots", "all"}:
        summary = await execute_root_stage(run_dir, provider, inventory, framework)
        manifest["root_stage_summary"] = summary
        atomic_write_json(run_dir / "manifest.json", manifest)
        print(
            json.dumps({"root_stage": summary}, ensure_ascii=False, indent=2),
            flush=True,
        )
    if args.stage in {"leaves", "all"}:
        summary = await execute_leaf_stage(run_dir, provider, inventory, framework)
        manifest["leaf_stage_summary"] = summary
        atomic_write_json(run_dir / "manifest.json", manifest)
        print(
            json.dumps({"leaf_stage": summary}, ensure_ascii=False, indent=2),
            flush=True,
        )
    if args.stage in {"seal", "all"}:
        sealed_manifest = finalize_run(
            run_dir,
            inventory,
            framework,
            attachment_run_id,
            manifest,
        )
        print(
            json.dumps(
                {
                    "attachment_run_id": attachment_run_id,
                    "run_dir": str(run_dir),
                    "status": sealed_manifest["status"],
                    "counts": sealed_manifest["counts"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
