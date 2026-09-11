#!/usr/bin/env python3
"""Verify and import one bounded official eLibrary search capture.

This importer is deliberately independent of the application package.  It
accepts only the three pinned docket searches defined below, proves the source
ledger, source index and response bytes agree, preflights the complete target
cache, and then publishes a merged index without changing existing entries.

The capture schema used here records only successful HTTP-200 search bodies;
newer captures may also carry an explicit ``http_status`` field, which must be
200.  That schema contract is recorded in every run inventory row rather than
silently converting a failed or non-JSON response into cached evidence.
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
from typing import Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple


OFFICIAL_SEARCH_URL = (
    "https://elibrary.ferc.gov/eLibraryWebAPI/api/Search/AdvancedSearch"
)
EXPECTED_DOCKETS = frozenset({"IS26-587", "RP26-1091", "RP26-981"})
CAPTURE_SCHEMA = "bounded_official_elibrary_search_capture_v1"
SOURCE_SYSTEM = "eLibrary"
MEDIA_TYPE = "application/json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ACCESSION_RE = re.compile(r"^[0-9]{8}-[0-9]+$")
DOCKET_RE = re.compile(r"^[A-Z]+[0-9]+-[0-9]+$")


class BoundedSearchImportError(RuntimeError):
    """Base class for a fail-closed bounded-search import."""


class EvidenceVerificationError(BoundedSearchImportError):
    """The capture, source index, or response object is inconsistent."""


class CacheConflictError(BoundedSearchImportError):
    """The complete target cache did not pass conflict preflight."""


@dataclass(frozen=True)
class SearchItem:
    docket: str
    cache_key: str
    cache_url: str
    content_hash: str
    byte_size: int
    body: bytes
    source_entry: Mapping[str, object]
    accessions: Tuple[str, ...]
    request_payload_sha256: str
    http_status_basis: str

    def inventory_row(self, action: str) -> dict:
        return {
            "docket": self.docket,
            "cache_key": self.cache_key,
            "cache_url": self.cache_url,
            "content_hash": self.content_hash,
            "byte_size": self.byte_size,
            "media_type": MEDIA_TYPE,
            "source_system": SOURCE_SYSTEM,
            "http_status": 200,
            "http_status_basis": self.http_status_basis,
            "success": True,
            "accessions": list(self.accessions),
            "request_payload_sha256": self.request_payload_sha256,
            "action": action,
        }


@dataclass(frozen=True)
class VerifiedCapture:
    capture_identity: Mapping[str, object]
    source_index_identity: Mapping[str, object]
    retrieved_utc: str
    items: Tuple[SearchItem, ...]


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
    # Derive both fields from one complete content read.  In particular, do
    # not pair File Provider metadata from stat() with bytes read later.
    value = path.read_bytes()
    return {
        "path": str(path.resolve(strict=True)),
        "bytes": len(value),
        "sha256": _sha256_bytes(value),
    }


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(str(path)))


def _require_sha256(value: str, label: str) -> None:
    if not SHA256_RE.fullmatch(value):
        raise EvidenceVerificationError(
            "%s must be a lowercase 64-character SHA-256" % label)


def _require_int(value: object, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EvidenceVerificationError("%s is not an integer >= %d" % (label, minimum))
    return value


def _require_no_symlink_path(path: Path, root: Path, label: str,
                             require_file: bool = True) -> Path:
    """Require a lexical descendant with no symlink in its in-root path."""
    lexical_root = _absolute(root)
    lexical_path = _absolute(path)
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError:
        raise EvidenceVerificationError(
            "%s escapes its declared root: %s" % (label, path)) from None
    if lexical_root.is_symlink():
        raise EvidenceVerificationError("%s root must not be a symlink: %s" % (label, root))
    current = lexical_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise EvidenceVerificationError("%s traverses a symlink: %s" % (label, current))
    try:
        root_resolved = lexical_root.resolve(strict=True)
        resolved = lexical_path.resolve(strict=True)
    except OSError as exc:
        raise EvidenceVerificationError("missing %s: %s" % (label, exc)) from None
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise EvidenceVerificationError("%s resolves outside its declared root" % label)
    if require_file and not lexical_path.is_file():
        raise EvidenceVerificationError("%s is not a regular file: %s" % (label, path))
    if not require_file and not lexical_path.is_dir():
        raise EvidenceVerificationError("%s is not a directory: %s" % (label, path))
    return lexical_path


def _reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceVerificationError("JSON contains duplicate key %r" % key)
        result[key] = value
    return result


def _load_json_bytes(value: bytes, label: str):
    try:
        return json.loads(value.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs)
    except EvidenceVerificationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceVerificationError("%s is not valid duplicate-free UTF-8 JSON: %s" %
                                        (label, exc)) from None


def _expected_payload(docket: str) -> dict:
    return {
        "accessionNumber": None,
        "affiliations": [],
        "allDates": False,
        "availability": None,
        "categories": [],
        "classTypes": [
            {"documentClass": "Order/Opinion",
             "documentType": "Commission Order/Opinion"},
            {"documentClass": "Order/Opinion", "documentType": "Delegated Order"},
            {"documentClass": "ALJ Issuance",
             "documentType": "Certification of Settlement"},
            {"documentClass": "ALJ Issuance",
             "documentType": "Report to the Commission"},
            {"documentClass": "Pleading/Motion",
             "documentType": "Petition for Review"},
            {"documentClass": "Application/Petition/Request",
             "documentType": "Tariff Filing"},
            {"documentClass": "Application/Petition/Request",
             "documentType": "Complaints"},
        ],
        "curPage": 0,
        "dateSearches": [{
            "dateType": "filed_date",
            "endDate": "2026-12-31",
            "startDate": "2015-01-01",
        }],
        "docketSearches": [{"docketNumber": docket, "subDocketNumbers": []}],
        "eFiling": False,
        "groupBy": "NONE",
        "idolResultID": "",
        "libraries": [],
        "resultsPerPage": 100,
        "searchDescription": False,
        "searchFullText": False,
        "searchText": "",
        "sortBy": "",
    }


def _require_official_search_url(value: object, label: str) -> str:
    url = str(value or "")
    if url != OFFICIAL_SEARCH_URL:
        raise EvidenceVerificationError("%s is not the exact AdvancedSearch URL" % label)
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "https" or parts.hostname != "elibrary.ferc.gov"
            or parts.username or parts.password or parts.query or parts.fragment):
        raise EvidenceVerificationError("%s is not the public official FERC route" % label)
    return url


def _base_dockets(values: object, label: str) -> FrozenSet[str]:
    if not isinstance(values, list) or not values:
        raise EvidenceVerificationError("%s has no docketNumbers" % label)
    result = set()
    for value in values:
        text = str(value or "")
        match = re.fullmatch(r"([A-Z]+[0-9]+-[0-9]+)(?:-[0-9]+)?", text)
        if not match:
            raise EvidenceVerificationError("%s has malformed docket number %r" % (label, value))
        result.add(match.group(1))
    return frozenset(result)


def _verify_response(row: dict, body: bytes, docket: str) -> Tuple[Tuple[str, ...], str]:
    explicit_status = row.get("http_status")
    if explicit_status is not None:
        if _require_int(explicit_status, "%s http_status" % docket, 100) != 200:
            raise EvidenceVerificationError("%s response is not HTTP 200" % docket)
        status_basis = "explicit_capture_http_status"
    else:
        # This v1 capture ledger was emitted only after its capture routine
        # accepted HTTP 200. Preserve that limitation explicitly in inventory.
        status_basis = "bounded_capture_v1_successful_response_contract"
    if row.get("success") is not True:
        raise EvidenceVerificationError("%s capture row is not successful" % docket)
    if row.get("media_type") != MEDIA_TYPE:
        raise EvidenceVerificationError("%s capture media type is not application/json" % docket)
    response = _load_json_bytes(body, "%s response object" % docket)
    if not isinstance(response, dict) or response.get("success") is not True:
        raise EvidenceVerificationError("%s response JSON does not report success=true" % docket)
    if response.get("errorMessage") not in (None, ""):
        raise EvidenceVerificationError("%s successful response carries an error" % docket)
    hits = response.get("searchHits")
    if not isinstance(hits, list):
        raise EvidenceVerificationError("%s response has no searchHits array" % docket)
    num_hits = _require_int(row.get("num_hits"), "%s capture num_hits" % docket)
    total_hits = _require_int(row.get("total_hits"), "%s capture total_hits" % docket)
    if (_require_int(response.get("numHits"), "%s response numHits" % docket) != num_hits
            or _require_int(response.get("totalHits"),
                            "%s response totalHits" % docket) != total_hits
            or len(hits) != num_hits or total_hits < num_hits):
        raise EvidenceVerificationError("%s hit counts disagree across capture and response" % docket)
    accessions = row.get("accessions")
    if not isinstance(accessions, list) or len(accessions) != len(set(accessions)):
        raise EvidenceVerificationError("%s has malformed or duplicate capture accessions" % docket)
    actual_accessions: List[str] = []
    for position, hit in enumerate(hits):
        if not isinstance(hit, dict):
            raise EvidenceVerificationError("%s hit %d is not an object" % (docket, position))
        accession = str(hit.get("acesssionNumber") or "")
        if not ACCESSION_RE.fullmatch(accession):
            raise EvidenceVerificationError("%s hit %d has malformed accession" %
                                            (docket, position))
        if _base_dockets(hit.get("docketNumbers"), "%s hit %d" % (docket, position)) \
                != frozenset({docket}):
            raise EvidenceVerificationError("%s hit %d belongs to another docket" %
                                            (docket, position))
        actual_accessions.append(accession)
    if tuple(str(value) for value in accessions) != tuple(actual_accessions):
        raise EvidenceVerificationError("%s accessions disagree with the response" % docket)
    return tuple(actual_accessions), status_basis


def load_verified_capture(capture_path: Path, source_index_path: Path,
                          source_cache_root: Path, recovery_root: Path,
                          expected_capture_sha256: str,
                          expected_source_index_sha256: str) -> VerifiedCapture:
    """Verify the full capture and return exactly three importable objects."""
    _require_sha256(expected_capture_sha256, "expected capture hash")
    _require_sha256(expected_source_index_sha256, "expected source-index hash")
    recovery_root = _require_no_symlink_path(
        recovery_root, recovery_root, "recovery root", require_file=False)
    capture_path = _require_no_symlink_path(
        capture_path, recovery_root, "capture ledger")
    source_cache_root = _require_no_symlink_path(
        source_cache_root, recovery_root, "source cache", require_file=False)
    source_index_path = _require_no_symlink_path(
        source_index_path, source_cache_root, "source-cache index")
    capture_bytes = capture_path.read_bytes()
    index_bytes = source_index_path.read_bytes()
    if _sha256_bytes(capture_bytes) != expected_capture_sha256:
        raise EvidenceVerificationError("capture ledger SHA-256 mismatch")
    if _sha256_bytes(index_bytes) != expected_source_index_sha256:
        raise EvidenceVerificationError("source-cache index SHA-256 mismatch")
    capture = _load_json_bytes(capture_bytes, "capture ledger")
    source_index = _load_json_bytes(index_bytes, "source-cache index")
    if not isinstance(capture, dict) or capture.get("schema") != CAPTURE_SCHEMA:
        raise EvidenceVerificationError("unexpected capture schema")
    if not isinstance(source_index, dict):
        raise EvidenceVerificationError("source-cache index must be a JSON object")
    rows = capture.get("rows")
    if not isinstance(rows, list) or len(rows) != len(EXPECTED_DOCKETS):
        raise EvidenceVerificationError("capture must contain exactly three rows")
    if capture.get("requests_made") != len(rows):
        raise EvidenceVerificationError("requests_made does not equal the capture row count")
    dockets = [str(row.get("docket") or "") if isinstance(row, dict) else ""
               for row in rows]
    if (frozenset(dockets) != EXPECTED_DOCKETS or len(set(dockets)) != len(dockets)
            or any(not DOCKET_RE.fullmatch(value) for value in dockets)):
        raise EvidenceVerificationError(
            "capture docket population must be exactly %s" % sorted(EXPECTED_DOCKETS))

    items: List[SearchItem] = []
    expected_keys = set()
    for row in rows:
        docket = str(row["docket"])
        if row.get("method") != "POST":
            raise EvidenceVerificationError("%s method is not POST" % docket)
        official_url = _require_official_search_url(
            row.get("official_url"), "%s official_url" % docket)
        payload = row.get("request_payload")
        if payload != _expected_payload(docket):
            raise EvidenceVerificationError("%s request payload is outside the bounded contract" % docket)
        payload_blob = json.dumps(payload, sort_keys=True).encode("utf-8")
        payload_hash = _sha256_bytes(payload_blob)
        if (_require_int(row.get("request_payload_bytes"),
                         "%s request_payload_bytes" % docket) != len(payload_blob)
                or row.get("request_payload_sha256") != payload_hash):
            raise EvidenceVerificationError("%s canonical request identity disagrees" % docket)
        cache_url = "%s#body=%s" % (official_url, payload_hash[:32])
        if row.get("cache_url") != cache_url:
            raise EvidenceVerificationError("%s cache URL is not derived from its canonical payload" % docket)
        cache_key = _sha256_bytes(cache_url.encode("utf-8"))
        expected_keys.add(cache_key)
        entry = source_index.get(cache_key)
        if not isinstance(entry, dict):
            raise EvidenceVerificationError("%s source-cache entry is missing" % docket)
        content_hash = str(row.get("response_sha256") or "")
        _require_sha256(content_hash, "%s response hash" % docket)
        byte_size = _require_int(row.get("response_bytes"), "%s response bytes" % docket, 1)
        expected_cache_path = "objects/%s/%s" % (content_hash[:2], content_hash)
        stable_expected = {
            "source_url": cache_url,
            "source_system": SOURCE_SYSTEM,
            "media_type": MEDIA_TYPE,
            "content_hash": content_hash,
            "byte_size": byte_size,
            "cache_path": expected_cache_path,
        }
        for field, expected in stable_expected.items():
            if entry.get(field) != expected:
                raise EvidenceVerificationError(
                    "%s source-cache %s disagrees with capture" % (docket, field))
        object_path = source_cache_root / expected_cache_path
        object_path = _require_no_symlink_path(
            object_path, source_cache_root, "%s response object" % docket)
        body = object_path.read_bytes()
        if len(body) != byte_size or _sha256_bytes(body) != content_hash:
            raise EvidenceVerificationError("%s response object identity mismatch" % docket)
        accessions, status_basis = _verify_response(row, body, docket)
        items.append(SearchItem(
            docket=docket,
            cache_key=cache_key,
            cache_url=cache_url,
            content_hash=content_hash,
            byte_size=byte_size,
            body=body,
            source_entry=dict(entry),
            accessions=accessions,
            request_payload_sha256=payload_hash,
            http_status_basis=status_basis,
        ))
    if set(source_index) != expected_keys:
        raise EvidenceVerificationError("source-cache index is not the exact three-response population")
    return VerifiedCapture(
        capture_identity=_identity(capture_path),
        source_index_identity=_identity(source_index_path),
        retrieved_utc=str(capture.get("retrieved_utc") or ""),
        items=tuple(sorted(items, key=lambda item: item.docket)),
    )


def _read_target_index(cache_root: Path) -> Tuple[dict, Optional[bytes]]:
    index_path = cache_root / "index.json"
    if not index_path.exists():
        return {}, None
    if index_path.is_symlink() or not index_path.is_file():
        raise CacheConflictError("target index must be a regular non-symlink file")
    try:
        raw = index_path.read_bytes()
        value = _load_json_bytes(raw, "target cache index")
    except EvidenceVerificationError as exc:
        raise CacheConflictError(str(exc)) from None
    if not isinstance(value, dict):
        raise CacheConflictError("target cache index must be a JSON object")
    return value, raw


def _verify_target_object(path: Path, cache_root: Path, digest: str,
                          byte_size: Optional[int], label: str) -> None:
    try:
        checked = _require_no_symlink_path(path, cache_root, label)
    except EvidenceVerificationError as exc:
        raise CacheConflictError(str(exc)) from None
    actual_size = checked.stat().st_size
    if (byte_size is not None and actual_size != byte_size) or _sha256_file(checked) != digest:
        raise CacheConflictError("%s has conflicting content-addressed bytes" % label)


def _preflight_complete_target(cache_root: Path, desired: VerifiedCapture):
    """Validate every indexed and resident target object before any import."""
    if cache_root.is_symlink() or not cache_root.is_dir():
        raise CacheConflictError("target cache root must be a non-symlink directory")
    objects_root = cache_root / "objects"
    if objects_root.exists() and (objects_root.is_symlink() or not objects_root.is_dir()):
        raise CacheConflictError("target objects root must be a non-symlink directory")
    index, raw_index = _read_target_index(cache_root)
    referenced = set()
    for key, entry in index.items():
        if not isinstance(key, str) or not SHA256_RE.fullmatch(key) or not isinstance(entry, dict):
            raise CacheConflictError("target cache has malformed index key or entry")
        url = entry.get("source_url")
        digest = entry.get("content_hash")
        if not isinstance(url, str) or _sha256_bytes(url.encode("utf-8")) != key:
            raise CacheConflictError("target cache key does not match source_url")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise CacheConflictError("target cache entry has malformed content_hash")
        byte_size = entry.get("byte_size")
        if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 0:
            raise CacheConflictError("target cache entry has malformed byte_size")
        expected_rel = "objects/%s/%s" % (digest[:2], digest)
        if entry.get("cache_path") != expected_rel:
            raise CacheConflictError("target cache entry has noncanonical cache_path")
        path = cache_root / expected_rel
        _verify_target_object(path, cache_root, digest, byte_size,
                              "target indexed object %s" % digest)
        referenced.add(expected_rel)

    resident = set()
    if objects_root.exists():
        for directory, dirnames, filenames in os.walk(str(objects_root), followlinks=False):
            directory_path = Path(directory)
            for name in list(dirnames):
                candidate = directory_path / name
                if candidate.is_symlink():
                    raise CacheConflictError("target objects tree contains a symlink")
            for name in filenames:
                candidate = directory_path / name
                if candidate.is_symlink() or not candidate.is_file():
                    raise CacheConflictError("target objects tree contains a non-regular file")
                relative = candidate.relative_to(cache_root).as_posix()
                parts = relative.split("/")
                if (len(parts) != 3 or parts[0] != "objects"
                        or not re.fullmatch(r"[0-9a-f]{2}", parts[1])
                        or not SHA256_RE.fullmatch(parts[2]) or parts[1] != parts[2][:2]):
                    raise CacheConflictError("target contains a noncanonical object path: %s" % relative)
                _verify_target_object(candidate, cache_root, parts[2], None,
                                      "target resident object %s" % parts[2])
                resident.add(relative)

    actions = []
    conflicts = []
    for item in desired.items:
        existing = index.get(item.cache_key)
        if existing is not None:
            stable_fields = ("source_url", "source_system", "media_type", "content_hash",
                             "byte_size", "cache_path")
            disagreements = [field for field in stable_fields
                             if existing.get(field) != item.source_entry.get(field)]
            if disagreements:
                conflicts.append({
                    "docket": item.docket,
                    "cache_url": item.cache_url,
                    "fields": disagreements,
                    "detail": "existing URL entry differs from verified source capture",
                })
            else:
                actions.append((item, "already_present_identical"))
        else:
            actions.append((item, "import"))
    if conflicts:
        raise CacheConflictError(json.dumps(conflicts, sort_keys=True))
    snapshot = {
        "index_present": raw_index is not None,
        "index_bytes": len(raw_index) if raw_index is not None else 0,
        "index_sha256": _sha256_bytes(raw_index) if raw_index is not None else None,
        "indexed_entries": len(index),
        "resident_objects": len(resident),
        "unreferenced_valid_objects": len(resident - referenced),
    }
    return index, raw_index, actions, snapshot


def _append_jsonl(path: Path, value: dict) -> None:
    path = _absolute(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise BoundedSearchImportError("run inventory must be a regular non-symlink file")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        payload = (json.dumps(value, separators=(",", ":"), sort_keys=True)
                   + "\n").encode("utf-8")
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("zero-byte append to run inventory")
            offset += written
        os.fsync(fd)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _install_object(item: SearchItem, cache_root: Path) -> None:
    target = cache_root / str(item.source_entry["cache_path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        _verify_target_object(target, cache_root, item.content_hash, item.byte_size,
                              "planned target object %s" % item.content_hash)
        return
    temporary = target.with_name(".%s.%d.%s.tmp" %
                                 (item.content_hash, os.getpid(), uuid.uuid4().hex))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(temporary), flags, 0o600)
    try:
        offset = 0
        while offset < len(item.body):
            written = os.write(fd, item.body[offset:])
            if written <= 0:
                raise OSError("zero-byte write to staged cache object")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        if _sha256_file(temporary) != item.content_hash:
            raise CacheConflictError("staged object failed content verification")
        temporary.chmod(0o444)
        try:
            os.link(str(temporary), str(target))
        except FileExistsError:
            _verify_target_object(target, cache_root, item.content_hash, item.byte_size,
                                  "concurrent planned target object")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _publish_index(cache_root: Path, index: dict, original_raw: Optional[bytes]) -> bytes:
    index_path = cache_root / "index.json"
    current_raw = None
    if index_path.exists():
        if index_path.is_symlink() or not index_path.is_file():
            raise CacheConflictError("target index changed to a non-regular file")
        current_raw = index_path.read_bytes()
    if current_raw != original_raw:
        raise CacheConflictError("target index changed after complete-cache preflight")
    encoded = json.dumps(index, indent=0, sort_keys=True).encode("utf-8")
    temporary = index_path.with_name(".index.%d.%s.tmp" % (os.getpid(), uuid.uuid4().hex))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(temporary), flags, 0o644)
    try:
        offset = 0
        while offset < len(encoded):
            written = os.write(fd, encoded[offset:])
            if written <= 0:
                raise OSError("zero-byte write to staged target index")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(str(temporary), str(index_path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return encoded


def _separate_paths(recovery_root: Path, cache_root: Path, inventory: Path) -> None:
    recovery = recovery_root.resolve(strict=True)
    cache = cache_root.resolve(strict=False)
    inventory_path = inventory.resolve(strict=False)
    if (cache == recovery or cache in recovery.parents or recovery in cache.parents):
        raise BoundedSearchImportError(
            "target cache and immutable recovery capture must be separate trees")
    if (inventory_path == recovery or recovery in inventory_path.parents
            or inventory_path == cache or cache in inventory_path.parents):
        raise BoundedSearchImportError(
            "append-only inventory must be outside source and target cache trees")


def execute_import(capture_path: Path, source_index_path: Path,
                   source_cache_root: Path, recovery_root: Path,
                   target_cache_root: Path, run_inventory: Path,
                   expected_capture_sha256: str,
                   expected_source_index_sha256: str) -> dict:
    """Verify, preflight, import, and append an outcome inventory record."""
    recovery_root = Path(recovery_root)
    target_cache_root = Path(target_cache_root)
    run_inventory = Path(run_inventory)
    _separate_paths(recovery_root, target_cache_root, run_inventory)
    run_id = str(uuid.uuid4())
    started = _utc_now()
    base = {
        "schema": "bounded_elibrary_search_cache_import_run_v1",
        "run_id": run_id,
        "started_utc": started,
        "expected_capture_sha256": expected_capture_sha256,
        "expected_source_index_sha256": expected_source_index_sha256,
        "expected_dockets": sorted(EXPECTED_DOCKETS),
        "recovery_root": str(recovery_root.resolve(strict=True)),
        "target_cache_root": str(target_cache_root.resolve(strict=False)),
        "append_only_inventory": str(run_inventory.resolve(strict=False)),
    }
    try:
        verified = load_verified_capture(
            capture_path=Path(capture_path),
            source_index_path=Path(source_index_path),
            source_cache_root=Path(source_cache_root),
            recovery_root=recovery_root,
            expected_capture_sha256=expected_capture_sha256,
            expected_source_index_sha256=expected_source_index_sha256,
        )
    except Exception as exc:
        record = dict(base)
        record.update({
            "ended_utc": _utc_now(),
            "status": "failed_evidence_verification",
            "imported": 0,
            "already_present_identical": 0,
            "entries": [],
            "failure": {"type": type(exc).__name__, "detail": str(exc)},
        })
        _append_jsonl(run_inventory, record)
        raise

    target_cache_root.mkdir(parents=True, exist_ok=True)
    if target_cache_root.is_symlink() or not target_cache_root.is_dir():
        raise CacheConflictError("target cache root must be a non-symlink directory")
    lock_path = target_cache_root / ".bounded_elibrary_search_import.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_fd = os.open(str(lock_path), flags, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            index, original_raw, actions, target_before = _preflight_complete_target(
                target_cache_root, verified)
        except Exception as exc:
            record = dict(base)
            record.update({
                "capture_identity": dict(verified.capture_identity),
                "source_index_identity": dict(verified.source_index_identity),
                "ended_utc": _utc_now(),
                "status": "blocked_cache_conflict",
                "imported": 0,
                "already_present_identical": 0,
                "entries": [],
                "failure": {"type": type(exc).__name__, "detail": str(exc)},
            })
            _append_jsonl(run_inventory, record)
            raise

        imported = 0
        already = 0
        inventory_entries = []
        try:
            merged = dict(index)
            for item, action in actions:
                if action == "already_present_identical":
                    already += 1
                    inventory_entries.append(item.inventory_row(action))
                    continue
                _install_object(item, target_cache_root)
                merged[item.cache_key] = dict(item.source_entry)
                imported += 1
                inventory_entries.append(item.inventory_row("imported"))
            if imported:
                final_raw = _publish_index(target_cache_root, merged, original_raw)
            else:
                final_raw = original_raw if original_raw is not None else b""
            final_index, _, final_actions, target_after = _preflight_complete_target(
                target_cache_root, verified)
            if len(final_index) != len(merged) or any(action != "already_present_identical"
                                                      for _item, action in final_actions):
                raise CacheConflictError("post-import complete-cache verification failed")
            if imported and _sha256_bytes(final_raw) != target_after["index_sha256"]:
                raise CacheConflictError("published target index identity changed unexpectedly")
        except Exception as exc:
            record = dict(base)
            record.update({
                "capture_identity": dict(verified.capture_identity),
                "source_index_identity": dict(verified.source_index_identity),
                "ended_utc": _utc_now(),
                "status": "failed_during_import",
                "imported_objects_before_index_failure": imported,
                "imported": 0,
                "already_present_identical": already,
                "target_before": target_before,
                "entries": inventory_entries,
                "failure": {"type": type(exc).__name__, "detail": str(exc)},
            })
            _append_jsonl(run_inventory, record)
            raise

        record = dict(base)
        record.update({
            "capture_identity": dict(verified.capture_identity),
            "source_index_identity": dict(verified.source_index_identity),
            "capture_retrieved_utc": verified.retrieved_utc,
            "ended_utc": _utc_now(),
            "status": "complete",
            "planned": len(verified.items),
            "imported": imported,
            "already_present_identical": already,
            "target_before": target_before,
            "target_after": target_after,
            "entries": inventory_entries,
            "conflicts": [],
        })
        _append_jsonl(run_inventory, record)
        return record
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True,
                        help="Exact bounded CAPTURE.json")
    parser.add_argument("--source-index", type=Path, required=True,
                        help="Exact captured source_cache/index.json")
    parser.add_argument("--source-cache-root", type=Path, required=True,
                        help="Read-only captured source_cache directory")
    parser.add_argument("--recovery-root", type=Path, required=True,
                        help="Read-only root containing the complete capture")
    parser.add_argument("--target-cache-root", type=Path, required=True,
                        help="Target content-addressed SourceCache directory")
    parser.add_argument("--run-inventory", type=Path, required=True,
                        help="Append-only JSONL record outside both cache trees")
    parser.add_argument("--expected-capture-sha256", required=True,
                        help="Exact SHA-256 of CAPTURE.json")
    parser.add_argument("--expected-source-index-sha256", required=True,
                        help="Exact SHA-256 of captured source_cache/index.json")
    parser.add_argument("--apply", action="store_true",
                        help="Required acknowledgement that target cache may be updated")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.apply:
        parser.error("--apply is required; the importer never selects a target implicitly")
    try:
        record = execute_import(
            capture_path=args.capture,
            source_index_path=args.source_index,
            source_cache_root=args.source_cache_root,
            recovery_root=args.recovery_root,
            target_cache_root=args.target_cache_root,
            run_inventory=args.run_inventory,
            expected_capture_sha256=args.expected_capture_sha256,
            expected_source_index_sha256=args.expected_source_index_sha256,
        )
    except BoundedSearchImportError as exc:
        print(json.dumps({"status": "failed", "type": type(exc).__name__,
                          "detail": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps({
        "status": record["status"],
        "run_id": record["run_id"],
        "imported": record["imported"],
        "already_present_identical": record["already_present_identical"],
        "target_after": record["target_after"],
        "run_inventory": record["append_only_inventory"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
