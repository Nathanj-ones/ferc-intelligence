#!/usr/bin/env python3
"""Import the pinned official-FERC recovery capture into a ``SourceCache``.

The recovery files are evidence, not a mutable download staging area.  This
tool therefore fails closed unless the recovery result and hash index have the
exact pinned identities, verifies every referenced body independently, derives
the same POST cache key as :meth:`ferclib.http.Client.post_json`, and refuses a
URL whose cache entry already points at different bytes.

Only HTTP-200 responses are imported.  The retained HTTP-500 response for
accession 20231229-5212 is recorded as a blocker in an append-only JSONL run
inventory.  Running the importer again is idempotent for the cache and appends
a second run record instead of replacing the first.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple


HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from ferclib.http import SourceCache  # noqa: E402


DEFAULT_RECOVERY_ROOT = HERE / "inputs/official_ferc_recovery"
DEFAULT_RESULTS = HERE / "implementation_logs/input_recovery/OFFICIAL_FERC_RECOVERY_RESULTS.json"
DEFAULT_HASH_INDEX = HERE / "implementation_logs/input_recovery/OFFICIAL_FERC_RECOVERY_HASHES.json"

PINNED_RESULTS_SHA256 = "cc27ccb4fa2b210808ab7c89345b1fd1059f7d8b5138bb678f21a4125e144fdd"
PINNED_HASH_INDEX_SHA256 = "6b0c767579c1b61f612482cdd7026356049ea54ef333aab4fc86dc198d5fff5a"
PINNED_ACCESSIONS = frozenset({
    "20150930-5060",
    "20151215-5229",
    "20171010-5329",
    "20191231-5075",
    "20231229-5212",
    "20250102-5121",
    "20250401-5098",
    "20250701-5150",
    "20251001-5165",
    "20260102-5146",
    "20260401-5141",
    "20260527-5009",
    "20260701-5109",
})

OFFICIAL_ORIGIN = "https://elibrary.ferc.gov"
OFFICIAL_LIST_PREFIX = f"{OFFICIAL_ORIGIN}/eLibraryWebAPI/api/File/GetFileListFromP8/"
OFFICIAL_DOWNLOAD_URL = f"{OFFICIAL_ORIGIN}/eLibraryWebAPI/api/File/DownloadP8File"
SOURCE_SYSTEM = "eLibrary"
ACCESSION_RE = re.compile(r"^[0-9]{8}-[0-9]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RecoveryImportError(RuntimeError):
    """Base class for a fail-closed recovery import."""


class EvidenceVerificationError(RecoveryImportError):
    """The pinned evidence or one of its referenced bodies is inconsistent."""


class CacheConflictError(RecoveryImportError):
    """A target URL is already indexed to different or corrupt bytes."""


@dataclass(frozen=True)
class ImportItem:
    accession: str
    response_kind: str
    cache_url: str
    body: bytes
    content_sha256: str
    byte_size: int
    media_type: str
    raw_body_path: str
    http_status: int
    canonical_payload_sha256: Optional[str] = None
    attachment_ids: Tuple[str, ...] = ()

    def inventory_row(self, action: str) -> dict:
        return {
            "accession": self.accession,
            "response_kind": self.response_kind,
            "cache_url": self.cache_url,
            "content_sha256": self.content_sha256,
            "byte_size": self.byte_size,
            "media_type": self.media_type,
            "raw_body_path": self.raw_body_path,
            "http_status": self.http_status,
            "canonical_payload_sha256": self.canonical_payload_sha256,
            "attachment_ids": list(self.attachment_ids),
            "action": action,
        }


@dataclass(frozen=True)
class RecoveryPlan:
    results_identity: dict
    hash_index_identity: dict
    entries: Tuple[ImportItem, ...]
    blockers: Tuple[dict, ...]
    accessions: Tuple[str, ...]


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(path: Path) -> dict:
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": _sha256_file(path)}


def _require_expected_hash(label: str, actual: str, expected: str) -> None:
    if not SHA256_RE.fullmatch(expected):
        raise EvidenceVerificationError(f"{label} expected SHA-256 is malformed: {expected!r}")
    if actual != expected:
        raise EvidenceVerificationError(f"{label} SHA-256 mismatch: expected {expected}, got {actual}")


def _require_regular_immutable(path: Path, root: Path, label: str) -> None:
    try:
        resolved = path.resolve(strict=True)
        root_resolved = root.resolve(strict=True)
    except OSError as exc:
        raise EvidenceVerificationError(f"missing {label}: {path}: {exc}") from None
    if path.is_symlink() or not path.is_file():
        raise EvidenceVerificationError(f"{label} must be a regular non-symlink file: {path}")
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise EvidenceVerificationError(f"{label} escapes recovery root: {path}")
    if stat.S_IMODE(path.stat().st_mode) & 0o222:
        raise EvidenceVerificationError(f"{label} is writable, not immutable: {path}")


def _require_index_identity(hash_index: dict, path: Path, label: str) -> dict:
    entry = (hash_index.get("files") or {}).get(str(path))
    if not isinstance(entry, dict):
        raise EvidenceVerificationError(f"{label} is not indexed by exact path: {path}")
    actual = _identity(path)
    if entry.get("bytes") != actual["bytes"] or entry.get("sha256") != actual["sha256"]:
        raise EvidenceVerificationError(
            f"{label} disagrees with hash index: indexed={entry!r}, actual={actual!r}")
    return actual


def _require_official_url(url: str, expected: str, label: str) -> None:
    if url != expected:
        raise EvidenceVerificationError(f"{label} URL mismatch: expected {expected}, got {url!r}")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != "elibrary.ferc.gov" or parts.username or parts.password:
        raise EvidenceVerificationError(f"{label} is not the public official FERC route: {url!r}")


def _normalise_media_type(value: object) -> str:
    # Client._request strips parameters before SourceCache.put; mirror it.
    return str(value or "").split(";", 1)[0].strip()


def _http_status(response: dict, label: str) -> int:
    value = response.get("http_status")
    if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599:
        raise EvidenceVerificationError(f"{label} has invalid HTTP status {value!r}")
    return value


def _verified_body(response: dict, hash_index: dict, recovery_root: Path,
                   expected_method: str, expected_url: str, label: str) -> Tuple[bytes, dict]:
    if response.get("attempted_this_run") is not True:
        raise EvidenceVerificationError(f"{label} was not an attempted response")
    if response.get("method") != expected_method:
        raise EvidenceVerificationError(f"{label} method is not {expected_method}")
    _require_official_url(str(response.get("official_url") or ""), expected_url, label)
    if response.get("network_error") is not None:
        raise EvidenceVerificationError(f"{label} carries a network error and cannot be imported")
    if response.get("body_capture_limitation") is not None or response.get("response_sha256_is_complete") is not True:
        raise EvidenceVerificationError(f"{label} body was not captured completely")

    body_path = Path(str(response.get("raw_body_path") or ""))
    headers_path = Path(str(response.get("raw_headers_path") or ""))
    _require_regular_immutable(body_path, recovery_root, f"{label} body")
    _require_regular_immutable(headers_path, recovery_root, f"{label} headers")
    body_identity = _require_index_identity(hash_index, body_path, f"{label} body")
    _require_index_identity(hash_index, headers_path, f"{label} headers")
    if body_identity["bytes"] != response.get("response_bytes"):
        raise EvidenceVerificationError(f"{label} body size disagrees with result ledger")
    if body_identity["sha256"] != response.get("response_sha256"):
        raise EvidenceVerificationError(f"{label} body SHA-256 disagrees with result ledger")
    body = body_path.read_bytes()
    if len(body) != body_identity["bytes"] or _sha256_bytes(body) != body_identity["sha256"]:
        raise EvidenceVerificationError(f"{label} body changed while it was being verified")
    if response.get("http_status") == 200 and not body:
        raise EvidenceVerificationError(f"{label} is an empty HTTP-200 body")
    return body, body_identity


def canonical_download_request(accession: str, attachment_ids: Sequence[str]) -> Tuple[dict, bytes, str]:
    """Return the exact payload/blob/cache URL used by ``Client.post_json``."""
    payload = {
        "FileType": "",
        "accession": accession,
        "fileid": 0,
        "FileIDAll": "",
        "fileidLst": list(attachment_ids),
        "Islegacy": False,
    }
    canonical_blob = json.dumps(payload, sort_keys=True).encode("utf-8")
    payload_sha256 = _sha256_bytes(canonical_blob)
    cache_url = f"{OFFICIAL_DOWNLOAD_URL}#body={payload_sha256[:32]}"
    return payload, canonical_blob, cache_url


def load_verified_plan(results_path: Path, hash_index_path: Path, recovery_root: Path,
                       expected_results_sha256: str, expected_hash_index_sha256: str,
                       expected_accessions: Optional[FrozenSet[str]] = None) -> RecoveryPlan:
    """Verify immutable recovery evidence and construct successful cache writes."""
    results_path = Path(results_path)
    hash_index_path = Path(hash_index_path)
    recovery_root = Path(recovery_root)
    for path, label in ((results_path, "recovery results"), (hash_index_path, "recovery hash index")):
        if path.is_symlink() or not path.is_file():
            raise EvidenceVerificationError(f"{label} must be a regular non-symlink file: {path}")

    try:
        index_bytes = hash_index_path.read_bytes()
        results_bytes = results_path.read_bytes()
    except OSError as exc:
        raise EvidenceVerificationError(f"cannot read recovery ledgers: {type(exc).__name__}: {exc}") from None
    index_actual = _sha256_bytes(index_bytes)
    results_actual = _sha256_bytes(results_bytes)
    _require_expected_hash("recovery hash index", index_actual, expected_hash_index_sha256)
    _require_expected_hash("recovery results", results_actual, expected_results_sha256)
    try:
        hash_index = json.loads(index_bytes.decode("utf-8"))
        results = json.loads(results_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceVerificationError(f"recovery ledger is not valid UTF-8 JSON: {exc}") from None
    if hash_index.get("schema") != "official_ferc_recovery_hash_index_v1" or hash_index.get("self_excluded") is not True:
        raise EvidenceVerificationError("unexpected recovery hash-index schema or self-inclusion policy")
    results_identity = _require_index_identity(hash_index, results_path, "recovery results")
    if results.get("schema") != "official_ferc_recovery_results_v1":
        raise EvidenceVerificationError("unexpected recovery-results schema")

    result_rows = results.get("results")
    if not isinstance(result_rows, list) or not result_rows:
        raise EvidenceVerificationError("recovery results contain no rows")
    accessions = [str(row.get("accession_number") or "") for row in result_rows]
    if any(not ACCESSION_RE.fullmatch(accession) for accession in accessions):
        raise EvidenceVerificationError("recovery results contain a malformed accession")
    if len(set(accessions)) != len(accessions):
        raise EvidenceVerificationError("recovery results contain duplicate accessions")
    if expected_accessions is not None and frozenset(accessions) != expected_accessions:
        raise EvidenceVerificationError(
            f"recovery accession population mismatch: expected {sorted(expected_accessions)}, got {sorted(accessions)}")

    entries: List[ImportItem] = []
    blockers: List[dict] = []
    for row in result_rows:
        accession = str(row["accession_number"])
        list_url = OFFICIAL_LIST_PREFIX + accession
        if row.get("inventory_url") != list_url:
            raise EvidenceVerificationError(f"{accession} inventory/list URL is not exact")
        list_response = row.get("list_response")
        if not isinstance(list_response, dict):
            raise EvidenceVerificationError(f"{accession} has no list response ledger")
        list_body, list_identity = _verified_body(
            list_response, hash_index, recovery_root, "GET", list_url, f"{accession} list response")
        list_status = _http_status(list_response, f"{accession} list response")
        if list_status != 200:
            blockers.append({
                "accession": accession,
                "response_kind": "list",
                "http_status": list_status,
                "http_reason": list_response.get("http_reason"),
                "official_url": list_url,
                "response_bytes": list_identity["bytes"],
                "response_sha256": list_identity["sha256"],
                "disposition": "not imported; explicit source blocker",
            })
            continue
        try:
            list_doc = json.loads(list_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceVerificationError(f"{accession} HTTP-200 list body is not JSON: {exc}") from None
        data_list = list_doc.get("DataList") if isinstance(list_doc, dict) else None
        if not isinstance(data_list, list):
            raise EvidenceVerificationError(f"{accession} list JSON has no DataList array")
        attachment_ids = tuple(str(item.get("ID") or "") for item in data_list if item.get("ID"))
        if not attachment_ids or len(set(attachment_ids)) != len(attachment_ids):
            raise EvidenceVerificationError(f"{accession} list has no attachment IDs or duplicate IDs")
        recorded_ids = tuple((row.get("parsed_file_list") or {}).get("attachment_ids") or ())
        if attachment_ids != recorded_ids:
            raise EvidenceVerificationError(f"{accession} actual list IDs disagree with result ledger")
        for item in data_list:
            listed_accession = str(item.get("Accession_Number") or accession)
            if listed_accession != accession:
                raise EvidenceVerificationError(
                    f"{accession} list contains attachment assigned to {listed_accession}")

        entries.append(ImportItem(
            accession=accession,
            response_kind="list",
            cache_url=list_url,
            body=list_body,
            content_sha256=list_identity["sha256"],
            byte_size=list_identity["bytes"],
            media_type=_normalise_media_type(list_response.get("response_media_type")),
            raw_body_path=list_identity["path"],
            http_status=list_status,
            attachment_ids=attachment_ids,
        ))

        _payload, canonical_blob, download_cache_url = canonical_download_request(accession, attachment_ids)
        attachment_response = row.get("attachment_response")
        if not isinstance(attachment_response, dict):
            blockers.append({
                "accession": accession,
                "response_kind": "attachment",
                "http_status": None,
                "official_url": OFFICIAL_DOWNLOAD_URL,
                "cache_url": download_cache_url,
                "attachment_ids": list(attachment_ids),
                "canonical_payload_sha256": _sha256_bytes(canonical_blob),
                "disposition": "not imported; attachment response absent",
            })
            continue
        attachment_body, attachment_identity = _verified_body(
            attachment_response, hash_index, recovery_root, "POST", OFFICIAL_DOWNLOAD_URL,
            f"{accession} attachment response")
        attachment_status = _http_status(attachment_response, f"{accession} attachment response")
        if attachment_status != 200:
            blockers.append({
                "accession": accession,
                "response_kind": "attachment",
                "http_status": attachment_status,
                "http_reason": attachment_response.get("http_reason"),
                "official_url": OFFICIAL_DOWNLOAD_URL,
                "cache_url": download_cache_url,
                "attachment_ids": list(attachment_ids),
                "canonical_payload_sha256": _sha256_bytes(canonical_blob),
                "response_bytes": attachment_identity["bytes"],
                "response_sha256": attachment_identity["sha256"],
                "raw_body_path": attachment_identity["path"],
                "disposition": "not imported; explicit source blocker; one response is not proof of unavailability",
            })
            continue
        entries.append(ImportItem(
            accession=accession,
            response_kind="attachment",
            cache_url=download_cache_url,
            body=attachment_body,
            content_sha256=attachment_identity["sha256"],
            byte_size=attachment_identity["bytes"],
            media_type=_normalise_media_type(attachment_response.get("response_media_type")),
            raw_body_path=attachment_identity["path"],
            http_status=attachment_status,
            canonical_payload_sha256=_sha256_bytes(canonical_blob),
            attachment_ids=attachment_ids,
        ))

    cache_urls: Dict[str, ImportItem] = {}
    for item in entries:
        previous = cache_urls.get(item.cache_url)
        if previous is not None:
            raise EvidenceVerificationError(f"duplicate recovery cache URL: {item.cache_url}")
        cache_urls[item.cache_url] = item
    return RecoveryPlan(
        results_identity=results_identity,
        hash_index_identity=_identity(hash_index_path),
        entries=tuple(entries),
        blockers=tuple(blockers),
        accessions=tuple(accessions),
    )


def _existing_action(cache: SourceCache, item: ImportItem) -> str:
    key = cache.url_key(item.cache_url)
    entry = cache._index.get(key)  # preflight the exact URL before SourceCache.put
    object_path = cache._object_path(item.content_sha256)
    if entry is not None:
        existing_hash = str(entry.get("content_hash") or "")
        if existing_hash != item.content_sha256:
            raise CacheConflictError(
                f"cache URL conflict for {item.cache_url}: existing {existing_hash}, recovery {item.content_sha256}")
        existing_path = cache._object_path(existing_hash)
        if existing_path.is_symlink() or not existing_path.is_file():
            raise CacheConflictError(f"cache URL {item.cache_url} points to missing object {existing_hash}")
        actual = _sha256_file(existing_path)
        if actual != existing_hash or existing_path.stat().st_size != item.byte_size:
            raise CacheConflictError(f"cache URL {item.cache_url} points to corrupt/conflicting object bytes")
        if entry.get("byte_size") != item.byte_size:
            raise CacheConflictError(f"cache URL {item.cache_url} has inconsistent byte_size metadata")
        return "already_present_identical"
    if object_path.exists():
        if not object_path.is_file() or object_path.is_symlink():
            raise CacheConflictError(f"content-addressed target is not a regular file: {object_path}")
        actual = _sha256_file(object_path)
        if actual != item.content_sha256 or object_path.stat().st_size != item.byte_size:
            raise CacheConflictError(f"content-addressed target has conflicting bytes: {object_path}")
    return "import"


def _append_jsonl(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RecoveryImportError(f"run inventory must not be a symlink: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        payload = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        position = 0
        while position < len(payload):
            written = os.write(fd, payload[position:])
            if written <= 0:
                raise OSError("zero-byte append to run inventory")
            position += written
        os.fsync(fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _separate_roots(cache_root: Path, recovery_root: Path) -> None:
    try:
        cache_resolved = cache_root.resolve()
        recovery_resolved = recovery_root.resolve(strict=True)
    except OSError as exc:
        raise RecoveryImportError(f"cannot resolve cache/recovery roots: {exc}") from None
    if cache_root.is_symlink():
        raise RecoveryImportError(f"cache root must not be a symlink: {cache_root}")
    if (cache_resolved == recovery_resolved or cache_resolved in recovery_resolved.parents
            or recovery_resolved in cache_resolved.parents):
        raise RecoveryImportError("cache root and immutable recovery root must be separate directory trees")


def _source_cache_checked(cache_root: Path) -> SourceCache:
    """Open SourceCache without allowing its malformed-index fallback to erase state."""
    index_path = cache_root / "index.json"
    expected = None
    if index_path.exists():
        if index_path.is_symlink() or not index_path.is_file():
            raise CacheConflictError(f"cache index must be a regular non-symlink file: {index_path}")
        try:
            expected = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CacheConflictError(
                f"refusing to import over unreadable/malformed cache index: {type(exc).__name__}: {exc}") from None
        if not isinstance(expected, dict):
            raise CacheConflictError("refusing to import over non-object cache index")
    cache = SourceCache(cache_root)
    if expected is not None and cache._index != expected:
        raise CacheConflictError("SourceCache did not load the checked cache index exactly")
    return cache


def import_plan(plan: RecoveryPlan, cache_root: Path, run_inventory: Path) -> dict:
    """Preflight all cache entries, import new ones, and append one run record."""
    cache_root = Path(cache_root)
    run_inventory = Path(run_inventory)
    recovery_paths = {Path(item.raw_body_path).resolve() for item in plan.entries}
    if run_inventory.resolve() in recovery_paths:
        raise RecoveryImportError("run inventory cannot replace an immutable response body")
    cache_root.mkdir(parents=True, exist_ok=True)
    lock_path = cache_root / ".official_recovery_import.lock"
    started = _utc_now()
    run_id = str(uuid.uuid4())
    base_record = {
        "schema": "official_recovery_cache_import_run_v1",
        "run_id": run_id,
        "started_utc": started,
        "cache_root": str(cache_root.resolve()),
        "results_identity": plan.results_identity,
        "hash_index_identity": plan.hash_index_identity,
        "accessions": list(plan.accessions),
        "blockers": list(plan.blockers),
        "append_only_inventory": str(run_inventory.resolve()),
    }

    lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            cache = _source_cache_checked(cache_root)
        except CacheConflictError as exc:
            record = dict(base_record)
            record.update({
                "ended_utc": _utc_now(),
                "status": "blocked_cache_conflict",
                "planned_successful_responses": len(plan.entries),
                "imported": 0,
                "already_present_identical": 0,
                "conflicts": [{"cache_url": None, "detail": str(exc)}],
                "entries": [],
            })
            _append_jsonl(run_inventory, record)
            raise
        actions: List[Tuple[ImportItem, str]] = []
        conflicts = []
        for item in plan.entries:
            try:
                actions.append((item, _existing_action(cache, item)))
            except CacheConflictError as exc:
                conflicts.append({
                    "accession": item.accession,
                    "response_kind": item.response_kind,
                    "cache_url": item.cache_url,
                    "detail": str(exc),
                })
        if conflicts:
            record = dict(base_record)
            record.update({
                "ended_utc": _utc_now(),
                "status": "blocked_cache_conflict",
                "planned_successful_responses": len(plan.entries),
                "imported": 0,
                "already_present_identical": 0,
                "conflicts": conflicts,
                "entries": [item.inventory_row(action) for item, action in actions],
            })
            _append_jsonl(run_inventory, record)
            raise CacheConflictError(f"cache preflight found {len(conflicts)} conflicting URL(s)")

        imported = 0
        already = 0
        inventory_entries = []
        try:
            for item, action in actions:
                if action == "already_present_identical":
                    already += 1
                    inventory_entries.append(item.inventory_row(action))
                    continue
                # Re-check immediately before the only mutating operation.
                immediate = _existing_action(cache, item)
                if immediate == "already_present_identical":
                    already += 1
                    inventory_entries.append(item.inventory_row(immediate))
                    continue
                entry = cache.put(item.cache_url, item.body, item.media_type, SOURCE_SYSTEM)
                if entry.get("content_hash") != item.content_sha256:
                    raise CacheConflictError(f"SourceCache returned the wrong content hash for {item.cache_url}")
                object_path = cache._object_path(item.content_sha256)
                if _sha256_file(object_path) != item.content_sha256:
                    raise CacheConflictError(f"post-import object verification failed for {item.cache_url}")
                object_path.chmod(0o444)
                imported += 1
                inventory_entries.append(item.inventory_row("imported"))
            # Re-open the completed index and prove every planned successful
            # response resolves to the intended bytes before recording success.
            final_cache = _source_cache_checked(cache_root)
            for item in plan.entries:
                hit = final_cache.get(item.cache_url)
                if hit is None:
                    raise CacheConflictError(f"post-import cache entry is missing: {item.cache_url}")
                cached_body, cached_entry = hit
                if (_sha256_bytes(cached_body) != item.content_sha256
                        or len(cached_body) != item.byte_size
                        or cached_entry.get("content_hash") != item.content_sha256):
                    raise CacheConflictError(f"post-import cache entry has conflicting bytes: {item.cache_url}")
        except Exception as exc:
            record = dict(base_record)
            record.update({
                "ended_utc": _utc_now(),
                "status": "failed_during_import",
                "planned_successful_responses": len(plan.entries),
                "imported": imported,
                "already_present_identical": already,
                "failure": {"type": type(exc).__name__, "detail": str(exc)},
                "entries": inventory_entries,
            })
            _append_jsonl(run_inventory, record)
            raise

        record = dict(base_record)
        record.update({
            "ended_utc": _utc_now(),
            "status": "complete_with_blockers" if plan.blockers else "complete",
            "planned_successful_responses": len(plan.entries),
            "imported": imported,
            "already_present_identical": already,
            "conflicts": [],
            "entries": inventory_entries,
        })
        _append_jsonl(run_inventory, record)
        return record
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def execute_import(results_path: Path, hash_index_path: Path, recovery_root: Path,
                   cache_root: Path, run_inventory: Path,
                   expected_results_sha256: str = PINNED_RESULTS_SHA256,
                   expected_hash_index_sha256: str = PINNED_HASH_INDEX_SHA256,
                   expected_accessions: Optional[FrozenSet[str]] = PINNED_ACCESSIONS) -> dict:
    recovery_root = Path(recovery_root)
    cache_root = Path(cache_root)
    run_inventory = Path(run_inventory)
    _separate_roots(cache_root, recovery_root)
    recovery_resolved = recovery_root.resolve(strict=True)
    cache_resolved = cache_root.resolve()
    inventory_resolved = run_inventory.resolve()
    if inventory_resolved == recovery_resolved or recovery_resolved in inventory_resolved.parents:
        raise RecoveryImportError("append-only run inventory must not be written inside immutable recovery evidence")
    if inventory_resolved == cache_resolved or cache_resolved in inventory_resolved.parents:
        raise RecoveryImportError("append-only run inventory must be outside the SourceCache root")
    for protected in (Path(results_path).resolve(strict=True), Path(hash_index_path).resolve(strict=True)):
        if inventory_resolved == protected:
            raise RecoveryImportError(f"run inventory would overwrite protected evidence: {protected}")
        if cache_resolved == protected or cache_resolved in protected.parents:
            raise RecoveryImportError(f"cache root would contain protected evidence: {protected}")
    plan = load_verified_plan(
        results_path=Path(results_path),
        hash_index_path=Path(hash_index_path),
        recovery_root=recovery_root,
        expected_results_sha256=expected_results_sha256,
        expected_hash_index_sha256=expected_hash_index_sha256,
        expected_accessions=expected_accessions,
    )
    if expected_accessions == PINNED_ACCESSIONS:
        if len(plan.entries) != 25:
            raise EvidenceVerificationError(f"pinned recovery should contain 25 HTTP-200 responses, got {len(plan.entries)}")
        failed = [b for b in plan.blockers if b.get("accession") == "20231229-5212"
                  and b.get("response_kind") == "attachment" and b.get("http_status") == 500]
        if len(plan.blockers) != 1 or len(failed) != 1:
            raise EvidenceVerificationError("pinned recovery must retain exactly the 20231229-5212 HTTP-500 blocker")
    return import_plan(plan, cache_root=cache_root, run_inventory=run_inventory)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS,
                        help="Pinned OFFICIAL_FERC_RECOVERY_RESULTS.json")
    parser.add_argument("--hash-index", type=Path, default=DEFAULT_HASH_INDEX,
                        help="Pinned OFFICIAL_FERC_RECOVERY_HASHES.json")
    parser.add_argument("--recovery-root", type=Path, default=DEFAULT_RECOVERY_ROOT,
                        help="Immutable raw official-response root")
    parser.add_argument("--cache-root", type=Path, required=True,
                        help="Target SourceCache root; must be separate from recovery evidence")
    parser.add_argument("--run-inventory", type=Path, required=True,
                        help="Append-only JSONL inventory of every importer run")
    parser.add_argument(
        "--expected-results-sha256",
        help=("Exact result-ledger SHA-256 for a separately captured recovery set. "
              "Must be supplied together with --expected-hash-index-sha256 and "
              "at least one --expected-accession; omission retains the pinned "
              "historical 13-input contract."),
    )
    parser.add_argument(
        "--expected-hash-index-sha256",
        help="Exact hash-index SHA-256 for a separately captured recovery set.",
    )
    parser.add_argument(
        "--expected-accession", action="append", default=[],
        help="Exact accession in the separately captured set; may be repeated.",
    )
    parser.add_argument("--apply", action="store_true",
                        help="Required acknowledgement that the target cache may be updated")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.apply:
        parser.error("--apply is required; no cache is selected or modified implicitly")
    custom_contract = bool(
        args.expected_results_sha256
        or args.expected_hash_index_sha256
        or args.expected_accession
    )
    if custom_contract:
        if not (
            args.expected_results_sha256
            and args.expected_hash_index_sha256
            and args.expected_accession
        ):
            parser.error(
                "custom recovery imports require both expected SHA-256 values and "
                "at least one --expected-accession"
            )
        expected_accessions = frozenset(args.expected_accession)
        if len(expected_accessions) != len(args.expected_accession):
            parser.error("custom recovery accession contract contains duplicates")
        expected_results_sha256 = args.expected_results_sha256
        expected_hash_index_sha256 = args.expected_hash_index_sha256
    else:
        expected_accessions = PINNED_ACCESSIONS
        expected_results_sha256 = PINNED_RESULTS_SHA256
        expected_hash_index_sha256 = PINNED_HASH_INDEX_SHA256
    try:
        record = execute_import(
            results_path=args.results,
            hash_index_path=args.hash_index,
            recovery_root=args.recovery_root,
            cache_root=args.cache_root,
            run_inventory=args.run_inventory,
            expected_results_sha256=expected_results_sha256,
            expected_hash_index_sha256=expected_hash_index_sha256,
            expected_accessions=expected_accessions,
        )
    except RecoveryImportError as exc:
        print(json.dumps({"status": "failed", "type": type(exc).__name__, "detail": str(exc)},
                         sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps({
        "status": record["status"],
        "run_id": record["run_id"],
        "imported": record["imported"],
        "already_present_identical": record["already_present_identical"],
        "blockers": record["blockers"],
        "run_inventory": record["append_only_inventory"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
