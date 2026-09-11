#!/usr/bin/env python3
"""Run and attest the clean-extraction Build-B release boundary.

This is deliberately an external orchestrator.  It never edits the extracted
candidate or the Build-A tree.  Pipeline output, acceptance scratch and logs go
to a caller-supplied, initially empty work directory.  Every Python process
enters through an isolated ``-I -S`` launcher whose pre-import audit hook denies
network access, hidden-state reads and protected-tree writes.  The only
non-Python child program names permitted are the two declared local OCR tools.

The final JSON uses ``ferc-build-b-acceptance-v1``, the schema consumed by
``build_release.py --finalize-build-b``.  A PASS is issued only after:

* the exact archive and clean extraction agree with the embedded manifest;
* the declared offline plan succeeds from an absent output database;
* the adversarial acceptance suite has no mandatory skip, xfail or xpass;
* Build A, packaged state and Build B agree on every non-volatile semantic
  database value and every published export; and
* all seven reviewed annotations agree exactly.

Only the precise run identifiers/timestamps in ``VOLATILE_DB_COLUMNS``,
``VOLATILE_DB_JSON_FIELDS`` and ``VOLATILE_EXPORT_COLUMNS`` are normalised.
Scope, units, periods, values, warnings, review state, source dates and filing
identities are never ignored.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
import zipfile


SCHEMA = "ferc-build-b-acceptance-v1"
PACKAGE = "operating_assets_all_regimes"
MANIFEST = "artifact_manifest.json"
HEX64 = re.compile(r"^[0-9a-f]{64}$")

FINAL_CHECKS = (
    "build_b_from_empty",
    "offline_network_denied",
    "live_tree_access_denied",
    "replay_pass",
    "acceptance_pass",
    "build_a_build_b_semantic_match",
    "build_a_packaged_semantic_match",
    "required_exports_match",
    "annotations_match",
)

# Kept independent of run.py/build_release.py so the candidate cannot weaken
# the comparison by changing the set it asks the acceptance tool to inspect.
SEMANTIC_TABLES = (
    "entities", "assets", "asset_entity_map", "ownership", "dockets",
    "asset_dockets", "filings", "filing_entities", "filing_dockets", "documents", "source_facts",
    "source_contexts", "source_dimensions", "source_units", "source_manifest",
    "observations", "observation_versions", "lineage_populations", "lineage_edges",
    "document_facts", "events", "coverage_expected", "coverage_measured",
    "field_status", "requirements_crosswalk", "applicability", "taxonomy_sources",
    "blockers", "reviewed_source_annotations",
)

# Every permitted difference is named at table+column granularity.  Values are
# reduced to a presence token, so a missing timestamp/run id cannot equal a
# present one.  Source retrieval/filed/effective/review dates are intentionally
# absent from this list and therefore compare exactly.
VOLATILE_DB_COLUMNS = {
    "observations": ("run_id", "first_seen_at", "updated_at"),
    "observation_versions": ("superseded_at", "superseded_by_run_id"),
    "lineage_populations": ("created_at",),
    "document_facts": ("first_seen_at",),
    "events": ("first_seen_at",),
    "coverage_expected": ("frozen_run_id",),
    "coverage_measured": ("run_id", "measured_at"),
    "blockers": ("opened_at", "resolved_at"),
}

# ``observation_versions.row_json`` is the prior ``observations`` row encoded
# as JSON.  Its embedded run envelope must follow the same narrow policy as the
# corresponding columns above; treating the whole JSON value as volatile would
# also hide scope, period, unit, value, warning and provenance regressions.
VOLATILE_DB_JSON_FIELDS = {
    "observation_versions": {
        "row_json": ("run_id", "first_seen_at", "updated_at"),
    },
}

VOLATILE_EXPORT_COLUMNS = {
    "document_facts.csv": ("first_seen_at",),
    "events.csv": ("first_seen_at",),
    "blockers.csv": ("opened_at", "resolved_at"),
    "coverage_by_slot.csv": ("measured_at",),
}

# Fixed release contract, repeated rather than imported from the candidate.
REQUIRED_EXPORTS = (
    "quarterly_key_metrics.csv", "annual_key_metrics.csv",
    "canonical_observations.csv", "coverage_by_slot.csv",
    "coverage_by_entity.csv", "coverage_by_metric.csv",
    "coverage_statistics.json", "field_status.csv",
    "field_status_summary.json", "reviewed_source_annotations.csv",
    "lineage_edges.csv", "filing_inventory.csv", "source_manifest.csv",
    "documents.csv", "document_facts.csv", "events.csv", "blockers.csv",
    "applicability.csv",
)

# A writable acceptance mirror contains shipped code and immutable fixtures,
# never Build-A/package answers.  The pipeline itself executes in the verified
# extraction and directs every output through FERC_OUTPUT_DIR/FERC_STAGING_DB.
ACCEPTANCE_MIRROR_TOP = {
    "acceptance", "adapters", "evidence", "ferclib", "inputs", "migrations",
    "tests", "tools",
}
ACCEPTANCE_MIRROR_FILES = {
    "RUNTIME_REQUIREMENTS.json", "build_crosswalk.py", "build_field_status.py",
    "build_release.py", "exporters.py", "run.py", "run_regressions.py",
    "validate.py",
    "config/universe.csv", "config/taxonomy_pins.json",
    "config/metric_registry.csv", "config/run_plan.json",
    "config/annotations/MANIFEST.json",
    "config/annotations/reviewed_source_annotations.json",
}

PACKAGED_GENERATED_PATHS = (
    "staging", "exports", ".generations", "verification", "final_records",
    "publication_receipt.json", "run_status.json", "task_ledger.json",
    "RUN_STATUS.md",
)


# Loaded by ``python -I -S`` before the requested shipped script.  The audit
# hook remains active for that process.  ``sys.executable`` is then rebound to
# the generated wrapper, so every Python subprocess launched by replay or the
# acceptance suite enters through this same hook instead of escaping it.
GUARD_LAUNCHER = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import runpy
import sys

policy_path = os.environ.get("FERC_BUILD_B_GUARD_POLICY", "")
if not policy_path:
    raise SystemExit("FERC Build-B guard policy is not configured")
with open(policy_path, "r", encoding="utf-8") as handle:
    POLICY = json.load(handle)

DENY_READ = tuple(os.path.realpath(path) for path in POLICY["deny_read"])
DENY_WRITE = tuple(os.path.realpath(path) for path in POLICY["deny_write"])
SCRIPT_ROOTS = tuple(os.path.realpath(path) for path in POLICY["script_roots"])
ALLOWED_PROGRAMS = frozenset(POLICY["allowed_subprocess_basenames"])

def normal_path(value):
    if isinstance(value, int) or value is None:
        return None
    try:
        return os.path.realpath(os.path.abspath(os.fspath(value)))
    except (TypeError, ValueError):
        return None

def within(path, roots):
    return any(path == root or path.startswith(root + os.sep) for root in roots)

def writing(mode, flags):
    if isinstance(mode, str) and any(char in mode for char in "wax+"):
        return True
    if isinstance(flags, int):
        mask = (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        return bool(flags & mask)
    return False

def audit(event, args):
    if event.startswith("socket."):
        raise PermissionError("FERC Build-B guard denies all socket/network operations")
    if event == "os.system":
        raise PermissionError("FERC Build-B guard denies uninspected process launch")
    if event == "subprocess.Popen":
        executable = normal_path(args[0])
        basename = os.path.basename(executable or str(args[0]))
        if basename not in ALLOWED_PROGRAMS:
            raise PermissionError("FERC Build-B guard denies subprocess: " + basename)
    if event == "open":
        path = normal_path(args[0] if args else None)
        if path is None:
            return
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else None
        if writing(mode, flags):
            if within(path, DENY_WRITE):
                raise PermissionError("FERC Build-B guard denies write: " + path)
        elif within(path, DENY_READ):
            raise PermissionError("FERC Build-B guard denies hidden read: " + path)
    if event == "sqlite3.connect":
        path = normal_path(args[0] if args else None)
        if path and within(path, DENY_READ):
            raise PermissionError("FERC Build-B guard denies hidden SQLite read: " + path)
    if event in ("os.listdir", "os.scandir"):
        path = normal_path(args[0] if args else ".")
        if path and within(path, DENY_READ):
            raise PermissionError("FERC Build-B guard denies hidden directory read: " + path)
    if event in ("os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.chown",
                 "os.truncate", "os.symlink"):
        path = normal_path(args[0] if args else None)
        if path and within(path, DENY_WRITE):
            raise PermissionError("FERC Build-B guard denies mutation: " + path)
    if event in ("os.rename", "os.replace", "os.link"):
        for value in args[:2]:
            path = normal_path(value)
            if path and within(path, DENY_WRITE):
                raise PermissionError("FERC Build-B guard denies mutation: " + path)

sys.addaudithook(audit)
marker = Path(POLICY["marker_dir"]) / ("guard-active-" + str(os.getpid()))
marker.write_text("active\n", encoding="utf-8")

sys.executable = POLICY["python_wrapper"]
arguments = list(sys.argv[1:])
# Nested shipped code sometimes invokes sys.executable with isolation flags.
# The wrapper already used -I -S before this launcher loaded, so these are
# redundant but accepted for compatibility rather than mistaken for a path.
while arguments and arguments[0] in ("-I", "-S", "-B", "-s", "-E", "-P", "-u"):
    arguments.pop(0)
if not arguments:
    raise SystemExit("guard launcher requires a shipped script/module/command")
if arguments[0] == "-c":
    if len(arguments) < 2:
        raise SystemExit("guard launcher -c requires code")
    sys.argv = ["-c", *arguments[2:]]
    exec(compile(arguments[1], "<guarded -c>", "exec"), {"__name__": "__main__"})
elif arguments[0] == "-m":
    if len(arguments) < 2 or arguments[1] not in POLICY["allowed_python_modules"]:
        raise SystemExit("guard launcher module is not explicitly allowed")
    module = arguments[1]
    sys.argv = [module, *arguments[2:]]
    runpy.run_module(module, run_name="__main__", alter_sys=True)
else:
    target = normal_path(arguments[0])
    if not target or not within(target, SCRIPT_ROOTS):
        raise SystemExit("guard launcher target is outside the approved script roots")
    sys.argv = [target, *arguments[1:]]
    sys.path.insert(0, os.path.dirname(target))
    runpy.run_path(target, run_name="__main__")
'''


GUARD_PROBE = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import socket
import subprocess

policy = json.loads(Path(os.environ["FERC_BUILD_B_GUARD_POLICY"]).read_text())
checks = {}
try:
    socket.socket()
except PermissionError:
    checks["network"] = True
else:
    checks["network"] = False

try:
    subprocess.run(["/bin/echo", "must not run"], check=False)
except PermissionError:
    checks["unapproved_subprocess"] = True
else:
    checks["unapproved_subprocess"] = False

for label, path in policy["probe_denied_reads"].items():
    try:
        Path(path).read_bytes()
    except PermissionError:
        checks["read:" + label] = True
    else:
        checks["read:" + label] = False

try:
    (Path(policy["candidate_root"]) / ".build-b-write-probe").write_bytes(b"forbidden")
except PermissionError:
    checks["candidate_write"] = True
else:
    checks["candidate_write"] = False

print(json.dumps(checks, sort_keys=True))
raise SystemExit(0 if checks and all(checks.values()) else 3)
'''


class BuildBRefused(RuntimeError):
    """The candidate cannot receive a Build-B PASS record."""


def _utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _json_bytes(value) -> bytes:
    return (json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False)
            + "\n").encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def _identity(path: Path, logical_path: str | None = None) -> dict:
    if path.is_symlink() or not path.is_file():
        raise BuildBRefused(f"required regular file is absent or a symlink: {path}")
    digest, size = _sha_file(path)
    return {"path": logical_path or str(path), "sha256": digest, "bytes": size}


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _safe_relative(value: str) -> bool:
    if not value or "\\" in value or "\x00" in value:
        return False
    pure = PurePosixPath(value)
    return not pure.is_absolute() and all(part not in ("", ".", "..")
                                          for part in pure.parts)


def _payload_digest(entries: dict[str, dict]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(entries):
        item = entries[relative]
        digest.update(
            f"{relative}\0{item['sha256']}\0{item['bytes']}\n".encode("utf-8"))
    return digest.hexdigest()


def candidate_inventory(candidate: Path) -> dict:
    """Reject extraction extras/symlinks and bind cheap before/after metadata."""
    files = []
    metadata = hashlib.sha256()
    for path in sorted(candidate.rglob("*")):
        relative = path.relative_to(candidate).as_posix()
        if path.is_symlink():
            raise BuildBRefused(f"clean extraction contains a symlink: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise BuildBRefused(f"clean extraction contains a special node: {relative}")
        state = path.stat()
        files.append(relative)
        metadata.update(
            f"{relative}\0{state.st_size}\0{state.st_mtime_ns}\0"
            f"{stat.S_IMODE(state.st_mode):04o}\n".encode("utf-8"))
    return {"files": files, "file_count": len(files),
            "metadata_sha256": metadata.hexdigest()}


def _read_json(path: Path, label: str) -> tuple[bytes, dict]:
    if path.is_symlink() or not path.is_file():
        raise BuildBRefused(f"{label} is absent or a symlink: {path}")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildBRefused(f"{label} is not valid UTF-8 JSON: {exc}") from None
    if not isinstance(value, dict):
        raise BuildBRefused(f"{label} is not a JSON object")
    return raw, value


def verify_archive_and_extraction(archive: Path, candidate: Path) -> dict:
    """Stream-verify the exact ZIP and its already-clean extraction."""
    archive_identity = _identity(archive, archive.name)
    if candidate.is_symlink() or not candidate.is_dir():
        raise BuildBRefused(f"candidate root is absent or a symlink: {candidate}")
    try:
        handle = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BuildBRefused(f"archive is unreadable: {exc}") from None
    with handle as zipped:
        infos = zipped.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)):
            raise BuildBRefused("archive contains duplicate member names")
        folded = {}
        for name in names:
            prior = folded.setdefault(name.casefold(), name)
            if prior != name:
                raise BuildBRefused(f"archive contains case-colliding members: {prior}, {name}")
            if not _safe_relative(name):
                raise BuildBRefused(f"archive contains an unsafe member: {name!r}")
            info = zipped.getinfo(name)
            mode = (info.external_attr >> 16) & 0o170000
            if info.flag_bits & 0x1:
                raise BuildBRefused(f"archive contains an encrypted member: {name}")
            if mode == stat.S_IFLNK or mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise BuildBRefused(f"archive contains a non-regular member: {name}")

        manifest_name = f"{PACKAGE}/{MANIFEST}"
        if names.count(manifest_name) != 1:
            raise BuildBRefused(f"archive does not contain exactly one {manifest_name}")
        try:
            manifest_raw = zipped.read(manifest_name)
            manifest = json.loads(manifest_raw.decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError,
                zipfile.BadZipFile) as exc:
            raise BuildBRefused(f"embedded manifest is unreadable: {exc}") from None
        if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
            raise BuildBRefused("embedded manifest has no file identity object")
        if (manifest.get("package") != PACKAGE or manifest.get("payload") != "full"
                or manifest.get("identity_is_content_derived") is not True):
            raise BuildBRefused("embedded manifest does not declare the expected full payload")
        canonical_manifest = _json_bytes(manifest)
        if manifest_raw != canonical_manifest:
            raise BuildBRefused("embedded manifest is not the canonical JSON bytes")
        entries = manifest["files"]
        if manifest.get("file_count") != len(entries):
            raise BuildBRefused("embedded manifest file_count disagrees with its entries")
        expected_names = {manifest_name} | {f"{PACKAGE}/{name}" for name in entries}
        actual_files = {name for name in names if not name.endswith("/")}
        if actual_files != expected_names:
            raise BuildBRefused(
                "archive membership differs from its manifest "
                f"(unexpected={sorted(actual_files-expected_names)[:5]}, "
                f"missing={sorted(expected_names-actual_files)[:5]})")

        extracted_inventory = candidate_inventory(candidate)
        expected_extracted = set(entries) | {MANIFEST}
        actual_extracted = set(extracted_inventory["files"])
        if actual_extracted != expected_extracted:
            raise BuildBRefused(
                "candidate is not a clean exact extraction "
                f"(unexpected={sorted(actual_extracted-expected_extracted)[:5]}, "
                f"missing={sorted(expected_extracted-actual_extracted)[:5]})")

        recomputed = {}
        candidate_manifest = candidate / MANIFEST
        if candidate_manifest.is_symlink() or not candidate_manifest.is_file():
            raise BuildBRefused("clean extraction's manifest is absent or a symlink")
        if candidate_manifest.read_bytes() != manifest_raw:
            raise BuildBRefused("clean extraction's manifest differs from the archive")
        for relative, expected in sorted(entries.items()):
            if not _safe_relative(relative) or not isinstance(expected, dict):
                raise BuildBRefused(f"manifest has an unsafe/invalid entry: {relative!r}")
            member = f"{PACKAGE}/{relative}"
            digest = hashlib.sha256()
            size = 0
            try:
                with zipped.open(member) as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                        size += len(chunk)
            except (OSError, EOFError, zipfile.BadZipFile) as exc:
                raise BuildBRefused(f"archive member {relative} failed CRC/stream read: {exc}") \
                    from None
            actual = {"sha256": digest.hexdigest(), "bytes": size,
                      "class": expected.get("class")}
            if (actual["sha256"], actual["bytes"]) != (
                    expected.get("sha256"), expected.get("bytes")):
                raise BuildBRefused(f"archive member identity mismatch: {relative}")
            extracted = candidate / Path(*PurePosixPath(relative).parts)
            extracted_identity = _identity(extracted, relative)
            if (extracted_identity["sha256"], extracted_identity["bytes"]) != (
                    actual["sha256"], actual["bytes"]):
                raise BuildBRefused(f"clean extraction differs from archive: {relative}")
            recomputed[relative] = actual
        payload = _payload_digest(recomputed)
        if payload != manifest.get("payload_digest"):
            raise BuildBRefused("payload digest does not recompute from streamed members")
    return {
        "archive": archive_identity,
        "manifest": {"path": manifest_name, "sha256": _sha_bytes(canonical_manifest),
                     "bytes": len(canonical_manifest),
                     "file_count": len(entries), "payload_digest": payload},
        "manifest_value": manifest,
        "verified_payload_files": len(entries),
        "candidate_inventory": extracted_inventory,
    }


def verify_pending(pending_path: Path, package: dict) -> dict:
    raw, pending = _read_json(pending_path, "pending candidate sidecar")
    if (pending.get("schema") != "ferc-full-release-pending-build-b-v1"
            or pending.get("package") != PACKAGE or pending.get("payload") != "full"
            or pending.get("status") != "CANDIDATE_PENDING_BUILD_B"):
        raise BuildBRefused("pending candidate sidecar has the wrong identity/status schema")
    archive = pending.get("archive") or {}
    manifest = pending.get("manifest") or {}
    if (archive.get("sha256"), archive.get("bytes")) != (
            package["archive"]["sha256"], package["archive"]["bytes"]):
        raise BuildBRefused("pending sidecar does not bind the supplied archive")
    if (pending.get("payload_digest") != package["manifest"]["payload_digest"]
            or manifest.get("sha256") != package["manifest"]["sha256"]
            or manifest.get("file_count") != package["manifest"]["file_count"]):
        raise BuildBRefused("pending sidecar does not bind the embedded manifest")
    build_a = pending.get("build_a_publication") or {}
    database_identity = str(build_a.get("database_identity") or "")
    if build_a.get("status") != "verified" or not HEX64.fullmatch(database_identity):
        raise BuildBRefused("pending sidecar lacks a verified Build-A database identity")
    return {"identity": {"path": pending_path.name, "sha256": _sha_bytes(raw),
                         "bytes": len(raw)}, "value": pending,
            "build_a_database_identity": database_identity}


def verify_plan_policy(candidate: Path) -> dict:
    raw, plan = _read_json(candidate / "config" / "run_plan.json", "run plan")
    if plan.get("schema") != "ferc_operating_assets_run_plan_v2":
        raise BuildBRefused("run plan has the wrong schema")
    if plan.get("cache_only") is not True:
        raise BuildBRefused("run plan is not explicitly cache-only")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(plan.get("as_of") or "")):
        raise BuildBRefused("run plan has no pinned ISO as-of date")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise BuildBRefused("run plan contains no steps")
    ids = []
    for number, step in enumerate(steps, 1):
        if not isinstance(step, dict) or not isinstance(step.get("argv"), list):
            raise BuildBRefused(f"run-plan step {number} has no argv")
        sid = str(step.get("id") or "")
        if not sid or sid in ids:
            raise BuildBRefused(f"run-plan step {number} has a missing/duplicate id")
        ids.append(sid)
        argv = [str(value) for value in step["argv"]]
        for token in argv:
            if token.startswith("/") or token.startswith("~") or "\\" in token:
                raise BuildBRefused(f"run-plan step {sid} contains an external path: {token}")
        if argv[:1] == ["run.py"] and "--offline" not in argv:
            raise BuildBRefused(f"run-plan step {sid} does not enforce --offline")
        if "--resume-plan" in argv:
            raise BuildBRefused(f"run-plan step {sid} attempts a non-empty resume")
        for declaration in step.get("inputs", []):
            value = declaration if isinstance(declaration, str) else declaration.get("path", "")
            if str(value).startswith(("/", "~")) or "\\" in str(value):
                raise BuildBRefused(f"run-plan step {sid} declares an external input")
    return {"path": "config/run_plan.json", "sha256": _sha_bytes(raw),
            "bytes": len(raw), "plan_id": plan.get("plan_id"),
            "plan_version": plan.get("plan_version"), "as_of": plan["as_of"],
            "cache_only": True, "step_ids": ids, "step_count": len(ids)}


def _presence_token(value):
    if value is None:
        return None
    if isinstance(value, str) and value == "":
        return ""
    return "<volatile-present>"


def _normalise_json_fields(value, fields: tuple[str, ...], label: str):
    if not isinstance(value, str):
        raise BuildBRefused(f"{label} is not a JSON string")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise BuildBRefused(f"{label} is invalid JSON: {exc}") from None
    if not isinstance(decoded, dict):
        raise BuildBRefused(f"{label} is not a JSON object")
    missing = sorted(set(fields) - set(decoded))
    if missing:
        raise BuildBRefused(f"{label} lacks allowlisted run fields: {missing}")
    for field in fields:
        decoded[field] = _presence_token(decoded[field])
    return json.dumps(decoded, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _open_database(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file():
        raise BuildBRefused(f"database is absent or a symlink: {path}")
    wal = Path(str(path) + "-wal")
    if wal.is_file() and wal.stat().st_size:
        raise BuildBRefused(f"database has a non-empty WAL: {wal}")
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro&immutable=1",
                                 uri=True)
    connection.row_factory = sqlite3.Row
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        connection.close()
        raise BuildBRefused(f"SQLite quick_check failed: {path}")
    foreign = connection.execute("PRAGMA foreign_key_check").fetchmany(10)
    if foreign:
        connection.close()
        raise BuildBRefused(f"SQLite foreign-key violations in {path}: {foreign}")
    return connection


def database_snapshot(path: Path, tables=SEMANTIC_TABLES,
                      volatile_columns=VOLATILE_DB_COLUMNS,
                      volatile_json_fields=VOLATILE_DB_JSON_FIELDS) -> dict:
    """Hash raw and explicitly normalised semantic rows without loading them."""
    connection = _open_database(path)
    raw_all = hashlib.sha256()
    normal_all = hashlib.sha256()
    details = {}
    try:
        for table in tables:
            info = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            columns = [str(row[1]) for row in info]
            if not columns:
                raise BuildBRefused(f"semantic table is absent: {table}")
            primary = [str(row[1]) for row in sorted(info, key=lambda item: item[5])
                       if row[5]]
            order = primary or columns
            allowed = tuple(volatile_columns.get(table, ()))
            json_allowed = volatile_json_fields.get(table, {})
            missing_allowed = sorted(set(allowed) - set(columns))
            if missing_allowed:
                raise BuildBRefused(
                    f"volatile allowlist names missing columns in {table}: {missing_allowed}")
            missing_json_columns = sorted(set(json_allowed) - set(columns))
            if missing_json_columns:
                raise BuildBRefused(
                    f"JSON allowlist names missing columns in {table}: "
                    f"{missing_json_columns}")
            header = (table + "\0" + "\0".join(columns) + "\n").encode("utf-8")
            raw_all.update(header)
            normal_all.update(header)
            raw_table = hashlib.sha256(header)
            normal_table = hashlib.sha256(header)
            select = ",".join(f'"{column}"' for column in columns)
            ordering = ",".join(f'"{column}"' for column in order)
            count = 0
            for row in connection.execute(
                    f'SELECT {select} FROM "{table}" ORDER BY {ordering}'):
                values = list(row)
                normal = [_presence_token(value) if column in allowed else value
                          for column, value in zip(columns, values)]
                for column, fields in json_allowed.items():
                    index = columns.index(column)
                    normal[index] = _normalise_json_fields(
                        normal[index], tuple(fields), f"{table}.{column}")
                raw_line = (json.dumps(values, ensure_ascii=False, separators=(",", ":"),
                                       default=str).encode("utf-8") + b"\n")
                normal_line = (json.dumps(normal, ensure_ascii=False, separators=(",", ":"),
                                          default=str).encode("utf-8") + b"\n")
                raw_all.update(raw_line)
                normal_all.update(normal_line)
                raw_table.update(raw_line)
                normal_table.update(normal_line)
                count += 1
            details[table] = {
                "rows": count, "columns": columns, "primary_key": primary,
                "volatile_columns": list(allowed),
                "volatile_json_fields": {
                    key: list(value) for key, value in json_allowed.items()},
                "raw_sha256": raw_table.hexdigest(),
                "normalized_sha256": normal_table.hexdigest(),
            }
        schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()
    return {"path": str(path), "raw_identity": raw_all.hexdigest(),
            "normalized_identity": normal_all.hexdigest(),
            "schema_version": schema_version, "user_version": user_version,
            "quick_check": "ok", "foreign_key_violations": 0, "tables": details}


def compare_database_snapshots(reference: dict, candidate: dict,
                               reference_label: str, candidate_label: str,
                               tables=SEMANTIC_TABLES) -> dict:
    mismatches = []
    for table in tables:
        left = reference["tables"].get(table)
        right = candidate["tables"].get(table)
        if left is None or right is None:
            mismatches.append({"table": table, "reason": "table absent"})
            continue
        for field in ("columns", "primary_key", "rows", "normalized_sha256"):
            if left.get(field) != right.get(field):
                mismatches.append({"table": table, "field": field,
                                   reference_label: left.get(field),
                                   candidate_label: right.get(field)})
    return {"reference": reference_label, "candidate": candidate_label,
            "match": not mismatches, "mismatches": mismatches,
            "reference_normalized_identity": reference["normalized_identity"],
            "candidate_normalized_identity": candidate["normalized_identity"],
            "reference_raw_identity": reference["raw_identity"],
            "candidate_raw_identity": candidate["raw_identity"]}


def _require_recorded_comparison(record: dict, name: str, result: dict,
                                 failure: str) -> dict:
    """Persist a comparison before enforcing its fail-closed gate.

    A refused candidate is evidence, not merely an exit code.  Recording only
    after every comparison passed made the R5 result omit the exact table/file
    mismatch that caused refusal.  This helper makes storage precede the gate,
    so a first failure remains independently diagnosable in the external JSON.
    """
    record.setdefault("comparisons", {})[name] = result
    if not result.get("match"):
        raise BuildBRefused(failure)
    return result


def _normalised_csv(path: Path, volatile: tuple[str, ...]) -> dict:
    digest = hashlib.sha256()
    raw_digest, raw_bytes = _sha_file(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            header = []
        if len(header) != len(set(header)):
            raise BuildBRefused(f"CSV has duplicate columns: {path}")
        missing = sorted(set(volatile) - set(header))
        if missing:
            raise BuildBRefused(f"export allowlist names absent columns in {path.name}: {missing}")
        digest.update(json.dumps(header, ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8") + b"\n")
        indices = {header.index(column) for column in volatile}
        rows = 0
        for number, row in enumerate(reader, 2):
            if len(row) != len(header):
                raise BuildBRefused(
                    f"CSV row width differs from header: {path}:{number}")
            normal = [_presence_token(value) if index in indices else value
                      for index, value in enumerate(row)]
            digest.update(json.dumps(normal, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8") + b"\n")
            rows += 1
    return {"path": str(path), "kind": "csv", "columns": header, "rows": rows,
            "raw_sha256": raw_digest, "raw_bytes": raw_bytes,
            "normalized_sha256": digest.hexdigest(),
            "volatile_columns": list(volatile)}


def _normalised_export(path: Path, name: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise BuildBRefused(f"published export is absent or a symlink: {path}")
    if name.endswith(".csv"):
        return _normalised_csv(path, tuple(VOLATILE_EXPORT_COLUMNS.get(name, ())))
    raw = path.read_bytes()
    if name.endswith(".json"):
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BuildBRefused(f"published JSON is invalid ({name}): {exc}") from None
        normalized = _json_bytes(value)
        return {"path": str(path), "kind": "json", "raw_sha256": _sha_bytes(raw),
                "raw_bytes": len(raw), "normalized_sha256": _sha_bytes(normalized)}
    return {"path": str(path), "kind": "bytes", "raw_sha256": _sha_bytes(raw),
            "raw_bytes": len(raw), "normalized_sha256": _sha_bytes(raw)}


def verify_publication(root: Path, database_raw_identity: str) -> dict:
    receipt_raw, receipt = _read_json(root / "publication_receipt.json",
                                      "consumer publication receipt")
    if receipt.get("kind") != "consumer_exports":
        raise BuildBRefused(f"publication receipt has wrong kind under {root}")
    generation_id = str(receipt.get("generation_id") or "")
    generation_path = str(receipt.get("generation_path") or "")
    files = receipt.get("files")
    metadata = receipt.get("metadata")
    if (not HEX64.fullmatch(generation_id) or not _safe_relative(generation_path)
            or not isinstance(files, dict) or not files or not isinstance(metadata, dict)):
        raise BuildBRefused(f"publication receipt is structurally invalid under {root}")
    expected_generation = _sha_bytes(json.dumps(
        {"kind": "consumer_exports", "files": files, "metadata": metadata},
        sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if generation_id != expected_generation:
        raise BuildBRefused(f"publication generation id is not content-bound under {root}")
    if metadata.get("database_identity") != database_raw_identity:
        raise BuildBRefused(f"publication/database semantic identity differs under {root}")
    generation = root / Path(*PurePosixPath(generation_path).parts)
    embedded_raw, embedded = _read_json(
        generation / "GENERATION_MANIFEST.json", "embedded generation manifest")
    if embedded != receipt:
        raise BuildBRefused(f"publication/embedded generation manifests differ under {root}")
    exports = {}
    for name, expected in sorted(files.items()):
        if not _safe_relative(name) or not isinstance(expected, dict):
            raise BuildBRefused(f"unsafe publication member under {root}: {name!r}")
        immutable = _identity(generation / Path(*PurePosixPath(name).parts), name)
        compatible = _identity(root / "exports" / Path(*PurePosixPath(name).parts), name)
        pair = (expected.get("sha256"), expected.get("bytes"))
        if ((immutable["sha256"], immutable["bytes"]) != pair
                or (compatible["sha256"], compatible["bytes"]) != pair):
            raise BuildBRefused(f"publication member identity mismatch under {root}: {name}")
        exports[name] = _normalised_export(root / "exports" / name, name)
    missing_required = sorted(set(REQUIRED_EXPORTS) - set(files))
    if missing_required:
        raise BuildBRefused(f"publication omits required exports: {missing_required}")
    database = root / "staging" / "operating_assets.sqlite"
    connection = _open_database(database)
    try:
        rows = connection.execute(
            "SELECT manifest_json,status FROM publication_generations "
            "WHERE generation_id=?", (generation_id,)).fetchall()
    finally:
        connection.close()
    if len(rows) != 1 or rows[0]["status"] != "published":
        raise BuildBRefused(f"database lacks one published generation under {root}")
    try:
        stored = json.loads(rows[0]["manifest_json"])
    except json.JSONDecodeError:
        raise BuildBRefused(f"database publication manifest is invalid under {root}") from None
    if stored != receipt:
        raise BuildBRefused(f"database/publication receipt differs under {root}")
    return {"receipt": {"path": str(root / "publication_receipt.json"),
                        "sha256": _sha_bytes(receipt_raw), "bytes": len(receipt_raw)},
            "embedded_manifest": {"sha256": _sha_bytes(embedded_raw),
                                  "bytes": len(embedded_raw)},
            "generation_id": generation_id, "metadata": metadata,
            "files": exports}


def compare_publications(reference: dict, candidate: dict,
                         reference_label: str, candidate_label: str) -> dict:
    left_names = set(reference["files"])
    right_names = set(candidate["files"])
    mismatches = []
    if left_names != right_names:
        mismatches.append({"field": "file_set",
                           f"{reference_label}_only": sorted(left_names-right_names),
                           f"{candidate_label}_only": sorted(right_names-left_names)})
    for name in sorted(left_names & right_names):
        left = reference["files"][name]
        right = candidate["files"][name]
        for field in ("kind", "columns", "rows", "normalized_sha256"):
            if left.get(field) != right.get(field):
                mismatches.append({"file": name, "field": field,
                                   reference_label: left.get(field),
                                   candidate_label: right.get(field)})
    # These are the only publication-envelope values expected to differ after
    # a genuine empty rebuild. All other metadata (code/input snapshots,
    # coverage, field status, registry, as-of and commit population) is exact.
    volatile_metadata = {"run_id", "database_identity"}
    left_meta = {key: value for key, value in reference["metadata"].items()
                 if key not in volatile_metadata}
    right_meta = {key: value for key, value in candidate["metadata"].items()
                  if key not in volatile_metadata}
    if left_meta != right_meta:
        mismatches.append({"field": "publication_metadata_nonvolatile",
                           reference_label: left_meta, candidate_label: right_meta})
    return {"reference": reference_label, "candidate": candidate_label,
            "match": not mismatches, "mismatches": mismatches,
            "volatile_envelope_allowlist": [
                "generation_id", "generation_path", "metadata.run_id",
                "metadata.database_identity"],
            "files_compared": len(left_names & right_names)}


def annotation_check(database_snapshots: dict, publications: dict) -> dict:
    table = {label: snap["tables"]["reviewed_source_annotations"]
             for label, snap in database_snapshots.items()}
    export = {label: pub["files"]["reviewed_source_annotations.csv"]
              for label, pub in publications.items()}
    table_counts = {label: value["rows"] for label, value in table.items()}
    table_hashes = {label: value["normalized_sha256"] for label, value in table.items()}
    export_counts = {label: value["rows"] for label, value in export.items()}
    export_hashes = {label: value["normalized_sha256"] for label, value in export.items()}
    matched = (set(table_counts.values()) == {7} and set(export_counts.values()) == {7}
               and len(set(table_hashes.values())) == 1
               and len(set(export_hashes.values())) == 1)
    return {"match": matched, "required_rows": 7, "database_rows": table_counts,
            "database_sha256": table_hashes, "export_rows": export_counts,
            "export_sha256": export_hashes, "volatile_columns": []}


def acceptance_result(path: Path, returncode: int) -> dict:
    _raw, report = _read_json(path, "acceptance result")
    combined = report.get("combined") or {}
    by_tier = report.get("by_tier_report") or {}
    totals = combined.get("totals") or {}
    required_statuses = ("PASS", "FAIL", "ERROR", "SKIPPED", "XFAIL", "XPASS")
    if any(not isinstance(totals.get(key), int) for key in required_statuses):
        raise BuildBRefused("acceptance report has incomplete/non-integer totals")
    mandatory_tiers = ("worker", "integrated")
    mandatory = {}
    for tier in mandatory_tiers:
        tier_totals = (by_tier.get(tier) or {}).get("totals") or {}
        if not tier_totals:
            raise BuildBRefused(f"acceptance report omitted mandatory tier: {tier}")
        mandatory[tier] = {key: int(tier_totals.get(key, 0)) for key in required_statuses}
    mandatory_case_counts = {
        tier: sum(values.values()) for tier, values in mandatory.items()}
    disallowed_mandatory = sum(
        values[key] for values in mandatory.values()
        for key in ("FAIL", "ERROR", "SKIPPED", "XFAIL", "XPASS"))
    global_disallowed = sum(totals[key] for key in ("FAIL", "ERROR", "XFAIL", "XPASS"))
    passed = (returncode == 0 and all(value > 0 for value in mandatory_case_counts.values())
              and disallowed_mandatory == 0
              and global_disallowed == 0)
    return {"pass": passed, "returncode": returncode,
            "totals": {key: totals[key] for key in required_statuses},
            "mandatory_tiers": mandatory,
            "mandatory_case_counts": mandatory_case_counts,
            "policy": ("worker and integrated must execute nonzero cases with zero fail, "
                       "error, skip, xfail or xpass; only external_optional skips are allowed")}


def replay_result(path: Path, plan: dict, returncode: int) -> dict:
    _raw, ledger = _read_json(path, "replay step status")
    steps = ledger.get("steps") or {}
    expected = plan["step_ids"]
    states = {step: (steps.get(step) or {}).get("status") for step in expected}
    # The replay ledger is written as canonical JSON with sorted object keys.
    # Object member order therefore cannot prove execution order.  Each step
    # record carries the durable one-based ordinal written by run.py; use that
    # explicit field and reject missing, duplicate or reordered ordinals.
    actual_by_ordinal = sorted(
        steps,
        key=lambda step: (
            (steps.get(step) or {}).get("step")
            if isinstance((steps.get(step) or {}).get("step"), int)
            else len(expected) + 1,
            step,
        ),
    )
    ordinals = [(steps.get(step) or {}).get("step") for step in actual_by_ordinal]
    passed = (returncode == 0 and actual_by_ordinal == expected
              and ordinals == list(range(1, len(expected) + 1))
              and all(state == "complete" for state in states.values()))
    return {"pass": passed, "returncode": returncode, "states": states,
            "expected_steps": expected, "actual_steps_by_ordinal": actual_by_ordinal,
            "ordinals": ordinals,
            "unexpected_steps": sorted(set(steps)-set(expected))}


def install_python_guard(candidate: Path, build_a: Path, work: Path,
                         mirror: Path, extra_denied: list[Path],
                         python: Path) -> dict:
    """Create the isolated launcher and its explicit access policy."""
    guard = work / "python_guard"
    markers = guard / "markers"
    guard.mkdir()
    markers.mkdir()
    launcher = guard / "guard_launcher.py"
    wrapper = guard / "python_guard_wrapper"
    probe = guard / "guard_probe.py"
    policy_path = guard / "policy.json"
    launcher.write_text(GUARD_LAUNCHER, encoding="utf-8")
    probe.write_text(GUARD_PROBE, encoding="utf-8")
    wrapper.write_text(
        "#!/bin/sh\nexport FERC_BUILD_B_GUARD_POLICY="
        + shlex.quote(str(policy_path)) + "\nexec " + shlex.quote(str(python)) + " -I -S "
        + shlex.quote(str(launcher)) + " \"$@\"\n", encoding="utf-8")
    wrapper.chmod(0o500)
    packaged = [candidate / relative for relative in PACKAGED_GENERATED_PATHS]
    denied_read = [build_a, *extra_denied, *packaged]
    mirror_immutable = [mirror / top for top in ACCEPTANCE_MIRROR_TOP]
    mirror_immutable.extend(mirror / Path(*PurePosixPath(name).parts)
                            for name in ACCEPTANCE_MIRROR_FILES)
    denied_write = [candidate, build_a, *extra_denied, *mirror_immutable,
                    launcher, wrapper, probe, policy_path]
    probe_reads = {
        "build_a_database": str(build_a / "staging" / "operating_assets.sqlite"),
        "packaged_database": str(candidate / "staging" / "operating_assets.sqlite"),
        "packaged_exports": str(candidate / "exports"),
    }
    for number, path in enumerate(extra_denied, 1):
        probe_reads[f"extra_denied_{number}"] = str(path)
    policy = {
        "schema": "ferc-build-b-python-guard-v1",
        "candidate_root": str(candidate), "build_a_root": str(build_a),
        "work_root": str(work), "deny_read": [str(path) for path in denied_read],
        "deny_write": [str(path) for path in denied_write],
        "script_roots": [str(candidate), str(mirror), str(guard), str(work)],
        "allowed_subprocess_basenames": [
            wrapper.name, "pdftoppm", "swift"],
        "allowed_python_modules": ["unittest"],
        "python_wrapper": str(wrapper), "marker_dir": str(markers),
        "probe_denied_reads": probe_reads,
        "normalization_note": "No credentials or inherited Python/user paths are present.",
    }
    _atomic_json(policy_path, policy)
    return {"root": guard, "launcher": launcher, "wrapper": wrapper,
            "probe": probe, "policy": policy_path, "markers": markers,
            "policy_value": policy}


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_topology(candidate: Path, build_a: Path, work: Path, evidence: Path,
                      denied: list[Path]) -> None:
    candidate = candidate.resolve(strict=True)
    build_a = build_a.resolve(strict=True)
    work = work.resolve()
    evidence = evidence.resolve()
    if candidate == build_a or _is_within(candidate, build_a) or _is_within(build_a, candidate):
        raise BuildBRefused("candidate extraction and Build-A roots must be disjoint")
    for protected in (candidate, build_a):
        if (_is_within(work, protected) or _is_within(protected, work)
                or _is_within(evidence, protected)):
            raise BuildBRefused("work/evidence paths must be outside protected input roots")
    for path in denied:
        resolved = path.resolve(strict=True)
        if (_is_within(candidate, resolved) or _is_within(work, resolved)
                or _is_within(resolved, candidate) or _is_within(resolved, work)):
            raise BuildBRefused("an extra denied path overlaps candidate or Build-B work")


def materialize_acceptance_mirror(candidate: Path, mirror: Path,
                                  manifest: dict) -> dict:
    if mirror.exists():
        raise BuildBRefused(f"acceptance mirror already exists: {mirror}")
    selected = []
    for relative, expected in sorted(manifest.get("files", {}).items()):
        parts = PurePosixPath(relative).parts
        keep = (relative in ACCEPTANCE_MIRROR_FILES
                or (parts and parts[0] in ACCEPTANCE_MIRROR_TOP))
        if not keep or relative.startswith(("evidence/audit_baseline/",
                                             "evidence/repair_records/")):
            continue
        source = candidate / Path(*parts)
        if source.is_symlink() or not source.is_file():
            raise BuildBRefused(f"acceptance mirror source is absent/aliased: {relative}")
        digest, size = _sha_file(source)
        if (digest, size) != (expected.get("sha256"), expected.get("bytes")):
            raise BuildBRefused(f"acceptance mirror source differs from manifest: {relative}")
        target = mirror / Path(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied_digest, copied_size = _sha_file(target)
        if (copied_digest, copied_size) != (digest, size):
            raise BuildBRefused(f"acceptance mirror copy failed verification: {relative}")
        selected.append({"path": relative, "sha256": digest, "bytes": size})
    required = {"acceptance/run_acceptance.py", "ferclib/schema.sql", "run.py",
                "run_regressions.py"}
    missing = sorted(required - {row["path"] for row in selected})
    if missing:
        raise BuildBRefused(f"acceptance mirror lacks required shipped code: {missing}")
    return {"path": str(mirror), "files": len(selected),
            "bytes": sum(row["bytes"] for row in selected), "identities": selected}


def _command_environment(candidate: Path, output: Path, home: Path,
                         temporary: Path, guard: dict, python: Path) -> dict:
    python_bin = str(python.parent)
    search = [python_bin, "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    env = {
        "PATH": ":".join(dict.fromkeys(search)), "HOME": str(home),
        "TMPDIR": str(temporary), "LANG": "C", "LC_ALL": "C",
        "PYTHONNOUSERSITE": "1", "PYTHONPATH": "", "PYTHONSAFEPATH": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "FERC_OFFLINE": "1",
        "FERC_STAGING_DB": str(output / "staging" / "operating_assets.sqlite"),
        "FERC_OUTPUT_DIR": str(output),
        "FERC_SOURCE_CACHE": str(candidate / "source_cache"),
        "FERC_UNIVERSE": str(candidate / "config" / "universe.csv"),
        "FERC_ANNOTATIONS_DIR": str(candidate / "config" / "annotations"),
        "FERC_RUN_PLAN": str(candidate / "config" / "run_plan.json"),
        "FERC_TAXONOMY_PINS": str(candidate / "config" / "taxonomy_pins.json"),
        "FERC_LEDGER": str(output / "staging" / "build.task_ledger.json"),
        "FERC_ACCEPTANCE_REFERENCE_TREE": str(output),
        "FERC_ACCEPTANCE_REFERENCE_DB": str(
            output / "staging" / "operating_assets.sqlite"),
        "FERC_BUILD_B_GUARD_POLICY": str(guard["policy"]),
    }
    # No API key, credential path, user Python setting or ambient FERC path is
    # inherited. The keys are recorded, while values that could be credentials
    # never enter evidence or logs.
    return env


def run_command(label: str, logical_argv: list[str], cwd: Path, env: dict,
                guard: dict, logs: Path, timeout: int) -> dict:
    stdout_path = logs / f"{label}.stdout.log"
    stderr_path = logs / f"{label}.stderr.log"
    actual = [str(guard["wrapper"]), *logical_argv]
    started = time.monotonic()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
            completed = subprocess.run(actual, cwd=str(cwd), env=env, stdout=stdout,
                                       stderr=stderr, check=False, timeout=timeout)
            returncode = completed.returncode
            timeout_error = False
        except subprocess.TimeoutExpired:
            returncode = 124
            timeout_error = True
    return {"label": label, "argv": logical_argv,
            "guarded_argv_prefix": actual[:2],
            "cwd": str(cwd), "returncode": returncode,
            "timed_out": timeout_error,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": _identity(stdout_path), "stderr": _identity(stderr_path)}


def _directory_empty(path: Path) -> bool:
    return not path.exists() or (path.is_dir() and not any(path.iterdir()))


def execute(args) -> tuple[int, dict]:
    checks = {name: False for name in FINAL_CHECKS}
    record = {"schema": SCHEMA, "status": "FAIL", "started_at_utc": _utcnow(),
              "checks": checks, "identities": {}, "commands": [], "comparisons": {}}
    output = args.work_root.resolve() / "output"
    try:
        candidate = args.candidate_root.resolve(strict=True)
        build_a = args.build_a_root.resolve(strict=True)
        archive = args.archive.resolve(strict=True)
        pending_path = args.pending_sidecar.resolve(strict=True)
        evidence_out = args.out.resolve()
        denied = [path.resolve(strict=True) for path in args.deny_read]
        validate_topology(candidate, build_a, args.work_root, evidence_out, denied)
        if not _directory_empty(args.work_root):
            raise BuildBRefused("Build-B work root must be absent or empty")
        args.work_root.mkdir(parents=True, exist_ok=True)
        if not _directory_empty(output):
            raise BuildBRefused("Build-B output directory is not empty")
        output.mkdir()
        initial_output_entries = list(output.iterdir())
        if initial_output_entries or (output / "staging" / "operating_assets.sqlite").exists():
            raise BuildBRefused("Build-B did not start from empty outputs/an absent database")
        checks["build_b_from_empty"] = True

        package = verify_archive_and_extraction(archive, candidate)
        pending = verify_pending(pending_path, package)
        plan = verify_plan_policy(candidate)
        record["inputs"] = {
            "archive": package["archive"], "manifest": package["manifest"],
            "pending_sidecar": pending["identity"], "run_plan": plan,
            "candidate_root": str(candidate), "build_a_root": str(build_a),
        }

        db_paths = {
            "build_a": build_a / "staging" / "operating_assets.sqlite",
            "packaged": candidate / "staging" / "operating_assets.sqlite",
        }
        snapshots = {label: database_snapshot(path) for label, path in db_paths.items()}
        record["comparisons"]["database_snapshots"] = snapshots
        expected_identity = pending["build_a_database_identity"]
        if snapshots["build_a"]["raw_identity"] != expected_identity:
            raise BuildBRefused("Build-A database no longer matches the pending identity")
        if snapshots["packaged"]["raw_identity"] != expected_identity:
            raise BuildBRefused("packaged database does not match the pending Build-A identity")
        db_a_package = compare_database_snapshots(
            snapshots["build_a"], snapshots["packaged"], "build_a", "packaged")
        _require_recorded_comparison(
            record, "database_build_a_packaged", db_a_package,
            "Build-A and packaged semantic database records differ")

        publications = {
            "build_a": verify_publication(build_a, snapshots["build_a"]["raw_identity"]),
            "packaged": verify_publication(
                candidate, snapshots["packaged"]["raw_identity"]),
        }
        pub_a_package = compare_publications(
            publications["build_a"], publications["packaged"], "build_a", "packaged")
        _require_recorded_comparison(
            record, "publication_build_a_packaged", pub_a_package,
            "Build-A and packaged published exports differ")
        checks["build_a_packaged_semantic_match"] = True

        logs = args.work_root / "logs"
        home = args.work_root / "runtime_home"
        temporary = args.work_root / "runtime_tmp"
        mirror = args.work_root / "acceptance_tree"
        for path in (logs, home, temporary):
            path.mkdir()
        mirror_result = materialize_acceptance_mirror(
            candidate, mirror, package["manifest_value"])
        python_path = args.python.resolve(strict=True)
        if not python_path.is_file() or not os.access(python_path, os.X_OK):
            raise BuildBRefused(f"Python interpreter is absent/not executable: {python_path}")
        guard = install_python_guard(
            candidate, build_a, args.work_root.resolve(), mirror, denied, python_path)
        env = _command_environment(
            candidate, output, home, temporary, guard, python_path)

        probe_result = run_command(
            "isolation_probe", [str(guard["probe"])], guard["root"], env,
            guard, logs, args.timeout)
        record["commands"].append(probe_result)
        if probe_result["returncode"] != 0:
            raise BuildBRefused(
                f"network/hidden-input/write isolation probe exited "
                f"{probe_result['returncode']}")
        checks["offline_network_denied"] = True
        checks["live_tree_access_denied"] = True

        command_specs = [
            ("plan_check", [str(candidate / "run.py"), "plan", "--check"],
             candidate),
            ("replay", [str(candidate / "run.py"), "replay", "--offline"],
             candidate),
        ]
        for label, argv, cwd in command_specs:
            result = run_command(label, argv, cwd, env, guard, logs, args.timeout)
            record["commands"].append(result)
            if result["returncode"] != 0:
                raise BuildBRefused(f"{label} exited {result['returncode']}")

        replay = replay_result(output / "verification" / "replay_step_status.json",
                               plan, record["commands"][-1]["returncode"])
        record["replay"] = replay
        if not replay["pass"]:
            raise BuildBRefused("declared replay did not complete every exact plan step")
        checks["replay_pass"] = True

        acceptance_path = output / "verification" / "acceptance_results.json"
        acceptance_command = [
            str(mirror / "acceptance" / "run_acceptance.py"),
            "--out", str(acceptance_path),
        ]
        accepted_command = run_command(
            "acceptance", acceptance_command, mirror, env, guard,
            logs, args.timeout)
        record["commands"].append(accepted_command)
        acceptance = acceptance_result(acceptance_path, accepted_command["returncode"])
        record["acceptance"] = acceptance
        if not acceptance["pass"]:
            raise BuildBRefused("acceptance result violates the mandatory-tier policy")
        checks["acceptance_pass"] = True

        db_b = output / "staging" / "operating_assets.sqlite"
        snapshots["build_b"] = database_snapshot(db_b)
        publications["build_b"] = verify_publication(
            output, snapshots["build_b"]["raw_identity"])
        db_a_b = compare_database_snapshots(
            snapshots["build_a"], snapshots["build_b"], "build_a", "build_b")
        _require_recorded_comparison(
            record, "database_build_a_build_b", db_a_b,
            "Build A and Build B differ outside the volatile allowlist")
        checks["build_a_build_b_semantic_match"] = True
        pub_a_b = compare_publications(
            publications["build_a"], publications["build_b"], "build_a", "build_b")
        pub_package_b = compare_publications(
            publications["packaged"], publications["build_b"], "packaged", "build_b")
        record["comparisons"]["publication_build_a_build_b"] = pub_a_b
        record["comparisons"]["publication_packaged_build_b"] = pub_package_b
        if not pub_a_b["match"] or not pub_package_b["match"]:
            raise BuildBRefused("required/published exports differ outside the allowlist")
        checks["required_exports_match"] = True

        annotations = annotation_check(snapshots, publications)
        record["comparisons"]["annotations"] = annotations
        if not annotations["match"]:
            raise BuildBRefused("seven reviewed annotations do not agree across A/package/B")
        checks["annotations_match"] = True

        # The launcher installed the audit hook before each target and rebound
        # sys.executable to itself for descendant Python commands. The negative
        # probe separately proved socket, hidden-read and candidate-write denial.
        if not record["commands"] or any(
                item["guarded_argv_prefix"][:1] != [str(guard["wrapper"])]
                for item in record["commands"]):
            raise BuildBRefused("not every Build-B command used the isolated launcher")
        final_candidate_inventory = candidate_inventory(candidate)
        if final_candidate_inventory != package["candidate_inventory"]:
            raise BuildBRefused("candidate extraction metadata changed during Build B")

        # The finalizer requires one identity token for all three states. Build
        # B's raw digest legitimately differs because run UUIDs/timestamps are
        # embedded in otherwise identical rows. This certified value means:
        # raw Build-A identity + exact equality after the declared, presence-
        # preserving normalization. Both raw identities remain in the evidence.
        record["identities"] = {
            "archive_sha256": package["archive"]["sha256"],
            "archive_bytes": package["archive"]["bytes"],
            "payload_digest": package["manifest"]["payload_digest"],
            "manifest_sha256": package["manifest"]["sha256"],
            "build_a_database_identity": expected_identity,
            "build_b_database_identity": expected_identity,
            "packaged_database_identity": expected_identity,
        }
        record["isolation"] = {
            "mechanism": "pre-import Python audit hook propagated via sys.executable",
            "launcher": _identity(guard["launcher"]),
            "wrapper": _identity(guard["wrapper"]),
            "policy": _identity(guard["policy"]),
            "probe": _identity(guard["probe"]),
            "guard_process_markers": len(list(guard["markers"].glob("guard-active-*"))),
            "network_policy": "all socket.* audit events denied before shipped code loads",
            "candidate_write_denied": True, "build_a_read_write_denied": True,
            "packaged_generated_read_denied": list(PACKAGED_GENERATED_PATHS),
            "additional_read_write_denied": [str(path) for path in denied],
            "ambient_environment_inherited": False,
            "environment_keys": sorted(env), "api_key_present": False,
            "acceptance_mirror": mirror_result,
            "candidate_inventory_before_after": {
                "unchanged": True, **final_candidate_inventory},
        }
        record["comparison_policy"] = {
            "semantic_tables": list(SEMANTIC_TABLES),
            "volatile_db_columns": {
                key: list(value) for key, value in VOLATILE_DB_COLUMNS.items()},
            "volatile_db_json_fields": {
                table: {column: list(fields) for column, fields in columns.items()}
                for table, columns in VOLATILE_DB_JSON_FIELDS.items()},
            "volatile_export_columns": {
                key: list(value) for key, value in VOLATILE_EXPORT_COLUMNS.items()},
            "normalization": ("only non-null/non-empty values in the named columns "
                              "become <volatile-present>; null and empty remain distinct"),
            "not_ignored": [
                "scope", "unit", "period", "value", "qa_flags", "review_status",
                "source filing/fact/version identity", "filed/effective/retrieval dates"],
            "operational_journals_outside_semantic_identity": [
                "runs", "run_log", "checkpoints", "run_unit_status",
                "run_input_inventory", "unit_commits", "publication_generations"],
        }
        record["comparisons"].update({
            "raw_build_b_database_identity": snapshots["build_b"]["raw_identity"],
            "certified_identity_basis": (
                "pending Build-A raw identity after exact Build-A/package agreement and "
                "Build-B equality outside the explicit volatile-column allowlist"),
        })
        if not all(value is True for value in checks.values()):
            raise BuildBRefused("one or more mandatory Build-B checks did not become true")
        record["status"] = "PASS"
        return 0, record
    except Exception as exc:  # noqa: BLE001 - every ordinary failure becomes FAIL evidence
        record["failure"] = {"type": type(exc).__name__, "detail": str(exc)}
        return 1, record
    finally:
        record["finished_at_utc"] = _utcnow()
        record["output_root"] = str(output)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", type=Path, required=True,
                        help="clean extracted operating_assets_all_regimes root")
    parser.add_argument("--archive", type=Path, required=True,
                        help="exact full archive from which candidate-root was extracted")
    parser.add_argument("--pending-sidecar", type=Path, required=True)
    parser.add_argument("--build-a-root", type=Path, required=True,
                        help="frozen Build-A payload root, read only")
    parser.add_argument("--work-root", type=Path, required=True,
                        help="new absent/empty directory for all Build-B writes")
    parser.add_argument("--out", type=Path, required=True,
                        help="external Build-B JSON evidence path")
    parser.add_argument("--deny-read", type=Path, action="append", default=[],
                        help="additional live/worker root denied to every child (repeatable)")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--timeout", type=int, default=21600,
                        help="per-command timeout seconds (default: 6 hours)")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    code, record = execute(args)
    try:
        _atomic_json(args.out.resolve(), record)
    except OSError as exc:
        print(f"BUILD B: FAIL: cannot write evidence: {exc}", file=sys.stderr)
        return 2
    print(f"BUILD B: {record['status']} -> {args.out.resolve()}")
    if code:
        failure = record.get("failure") or {}
        print(f"{failure.get('type', 'BuildBRefused')}: "
              f"{failure.get('detail', 'unspecified failure')}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
