#!/usr/bin/env python3
"""Create non-circular post-package test and release evidence sidecars.

This tool is intentionally downstream of packaging and Build-B validation.  It
does not import the implementation being certified, execute tests, alter the
archive, or write into the candidate tree.  It consumes complete verbose test
logs, an already-finished archive, and a hash-bound package-validation record;
then atomically publishes a new external directory containing:

* ``FINAL_TEST_RUN_MANIFEST.json`` -- exact commands, interpreter/dependency
  declarations, independently parsed counts, and complete-log identities;
* ``FINAL_EVIDENCE_INDEX.json`` -- identities of every bundled member and every
  explicitly unbundled large item;
* ``CODEX_REPAIR_EVIDENCE.zip`` -- a bounded, deterministic evidence bundle;
* ``EXTERNAL_RELEASE_RECEIPT.json`` -- the final leaf binding all of the above
  to the immutable archive.

The evidence index and receipt are deliberately not embedded in either archive.
That ordering is the consistency boundary: package -> validation/test evidence
-> evidence ZIP/index -> external receipt.  Any package mutation requires a new
archive identity and therefore a new sidecar set.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

try:
    from implementation.unittest_log_parser import (
        UnittestLogRefused, parse_unittest_log as _parse_shared_unittest_log)
except ModuleNotFoundError:  # Direct script execution from implementation/.
    from unittest_log_parser import (  # type: ignore
        UnittestLogRefused, parse_unittest_log as _parse_shared_unittest_log)


TEST_MANIFEST_NAME = "FINAL_TEST_RUN_MANIFEST.json"
EVIDENCE_INDEX_NAME = "FINAL_EVIDENCE_INDEX.json"
EVIDENCE_ZIP_NAME = "CODEX_REPAIR_EVIDENCE.zip"
RECEIPT_NAME = "EXTERNAL_RELEASE_RECEIPT.json"
MAX_EVIDENCE_ZIP_BYTES = 25_000_000
MIN_COMPLETE_SUITE_CASES = 100
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SECRET_VALUE = re.compile(
    rb"(?i)(?:api[_-]?key|access[_-]?token|authorization|password|secret)"
    rb"\s*(?:=|:)\s*(?!<?redacted>?|none\b|null\b|false\b|true\b)"
    rb"[^\s,;]{8,}")
LOCAL_PATH = re.compile(
    rb"(?:/Users/[^/\s]+/|/home/[^/\s]+/|/private/(?:tmp|var)/|/tmp/|"
    rb"[A-Za-z]:\\Users\\[^\\\s]+\\)")

BUILD_B_CHECKS = (
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


class EvidenceRefused(RuntimeError):
    """An input cannot support an external PASS receipt."""


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n").encode("utf-8")


def _sha_and_size(path: pathlib.Path) -> Tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _identity(path: pathlib.Path, logical_name: str) -> Dict[str, Any]:
    sha, size = _sha_and_size(path)
    return {"name": logical_name, "bytes": size, "sha256": sha}


def _read_regular(path: pathlib.Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise EvidenceRefused(f"{label} is absent, non-regular, or a symlink: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise EvidenceRefused(f"cannot read {label}: {type(exc).__name__}: {exc}") from None


def _load_json(path: pathlib.Path, label: str) -> Tuple[bytes, Any]:
    raw = _read_regular(path, label)
    try:
        return raw, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceRefused(f"{label} is not valid UTF-8 JSON: {exc}") from None


def _safe_relative(value: Any, label: str) -> pathlib.PurePosixPath:
    text = str(value or "")
    pure = pathlib.PurePosixPath(text)
    if (not text or pure.is_absolute() or ".." in pure.parts or "." in pure.parts
            or "\\" in text or any(not part for part in pure.parts)):
        raise EvidenceRefused(f"{label} is not a safe relative POSIX path: {text!r}")
    return pure


def _under(root: pathlib.Path, relative: Any, label: str) -> pathlib.Path:
    pure = _safe_relative(relative, label)
    candidate = root
    for part in pure.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise EvidenceRefused(f"{label} traverses a symlink")
    path = candidate.resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        raise EvidenceRefused(f"{label} escapes its declared root") from None
    if path.is_symlink():
        raise EvidenceRefused(f"{label} resolves to a symlink")
    return path


def _hygiene_reasons(raw: bytes) -> List[str]:
    reasons = []
    if SECRET_VALUE.search(raw):
        reasons.append("credential-shaped value")
    if LOCAL_PATH.search(raw):
        reasons.append("local absolute path")
    return reasons


def _require_hygiene(raw: bytes, label: str) -> None:
    reasons = _hygiene_reasons(raw)
    if reasons:
        raise EvidenceRefused(f"{label} fails bundle hygiene: {', '.join(reasons)}")


def _parse_unittest_log(raw: bytes, label: str) -> Dict[str, Any]:
    try:
        parsed = _parse_shared_unittest_log(raw, label)
    except UnittestLogRefused as exc:
        raise EvidenceRefused(str(exc)) from None
    return {"counts": parsed["counts"], "results": parsed["results"],
            "successful": parsed["successful"]}


def _validate_detail_rows(rows: Any, expected: int, status_name: str,
                          results: Mapping[str, str], run_id: str) -> List[dict]:
    if not isinstance(rows, list) or len(rows) != expected:
        raise EvidenceRefused(
            f"run {run_id} must classify all {expected} {status_name} result(s)")
    clean = []
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise EvidenceRefused(f"run {run_id} {status_name} row {number} is not an object")
        test = str(row.get("test") or "").strip()
        classification = str(row.get("classification") or "").strip()
        reason = str(row.get("reason") or "").strip()
        if not test or not classification or not reason:
            raise EvidenceRefused(f"run {run_id} has incomplete {status_name} detail")
        if row.get("required") is not False:
            raise EvidenceRefused(f"run {run_id} leaves a required capability {status_name}")
        if results.get(test) != status_name:
            raise EvidenceRefused(
                f"run {run_id} {status_name} detail does not name an observed result")
        clean.append({"test": test, "classification": classification,
                      "reason": reason, "required": False})
    return clean


def _runtime_dependencies(runtime_path: pathlib.Path) -> Tuple[bytes, dict]:
    raw, body = _load_json(runtime_path, "runtime requirements")
    if not isinstance(body, dict) or not isinstance(body.get("python"), dict):
        raise EvidenceRefused("runtime requirements lacks a python declaration")
    python = body["python"]
    runtimes = python.get("tested_runtimes")
    if not isinstance(runtimes, list) or not runtimes:
        raise EvidenceRefused("runtime requirements has no tested runtimes")
    if not isinstance(python.get("third_party_packages"), list):
        raise EvidenceRefused("runtime requirements omits third_party_packages")
    external = body.get("external_executables")
    if not isinstance(external, list):
        raise EvidenceRefused("runtime requirements omits external_executables")
    return raw, {
        "schema_version": body.get("schema_version"),
        "identity": {"name": runtime_path.name, "bytes": len(raw),
                     "sha256": hashlib.sha256(raw).hexdigest()},
        "third_party_packages": python["third_party_packages"],
        "dependency_install_command": python.get("dependency_install_command"),
        "external_executables": external,
        "tested_runtimes": runtimes,
    }


def _match_interpreter(dependencies: Mapping[str, Any], declared: Any,
                       run_id: str) -> dict:
    needed = ("executable", "implementation", "python_version", "sqlite_version",
              "platform")
    if not isinstance(declared, dict) or any(not str(declared.get(k) or "").strip()
                                             for k in needed):
        raise EvidenceRefused(
            f"run {run_id} interpreter must declare {', '.join(needed)}")
    executable = str(declared["executable"])
    if LOCAL_PATH.search(executable.encode("utf-8")):
        raise EvidenceRefused(f"run {run_id} interpreter exposes a local workspace path")
    candidates = dependencies["tested_runtimes"]
    matches = [profile for profile in candidates
               if profile.get("executable") == executable
               and profile.get("python_version") == declared.get("python_version")
               and profile.get("sqlite_version") == declared.get("sqlite_version")
               and profile.get("platform") == declared.get("platform")]
    if len(matches) != 1:
        raise EvidenceRefused(
            f"run {run_id} interpreter does not exactly match one tested runtime")
    if declared.get("implementation") != "CPython":
        raise EvidenceRefused(f"run {run_id} interpreter implementation is not CPython")
    return {key: declared[key] for key in needed}


def _test_manifest(run_spec_path: pathlib.Path, logs_dir: pathlib.Path,
                   dependencies: Mapping[str, Any]) -> Tuple[dict, Dict[str, bytes], List[dict]]:
    _raw, spec = _load_json(run_spec_path, "test run specification")
    if not isinstance(spec, dict) or spec.get("schema") != "ferc-test-run-spec-v1":
        raise EvidenceRefused("test run specification schema is not v1")
    runs = spec.get("runs")
    if not isinstance(runs, list) or not runs:
        raise EvidenceRefused("test run specification has no runs")
    seen = set()
    records = []
    bundled_logs: Dict[str, bytes] = {}
    excluded_logs: List[dict] = []
    complete_acceptance = 0
    for number, declared in enumerate(runs, 1):
        if not isinstance(declared, dict):
            raise EvidenceRefused(f"test run {number} is not an object")
        run_id = str(declared.get("run_id") or "").strip()
        if not SAFE_ID.fullmatch(run_id) or run_id in seen:
            raise EvidenceRefused(f"test run {number} has invalid or duplicate run_id")
        seen.add(run_id)
        role = declared.get("acceptance_role")
        if role not in ("acceptance", "historical_negative_control"):
            raise EvidenceRefused(f"run {run_id} has invalid acceptance_role")
        kind = str(declared.get("kind") or "").strip()
        if not kind:
            raise EvidenceRefused(f"run {run_id} has no kind")
        log_rel = _safe_relative(declared.get("log"), f"run {run_id} log")
        log_path = _under(logs_dir, log_rel.as_posix(), f"run {run_id} log")
        log_raw = _read_regular(log_path, f"run {run_id} complete log")
        parsed = _parse_unittest_log(log_raw, f"run {run_id} complete log")
        exit_code = declared.get("exit_code")
        if not isinstance(exit_code, int):
            raise EvidenceRefused(f"run {run_id} has no exact integer exit code")
        command = declared.get("command")
        if not (isinstance(command, list) and command
                and all(isinstance(item, str) and item for item in command)):
            raise EvidenceRefused(f"run {run_id} command must be a non-empty argv list")
        command_raw = "\0".join(command).encode("utf-8")
        if SECRET_VALUE.search(command_raw) or LOCAL_PATH.search(command_raw):
            raise EvidenceRefused(f"run {run_id} command fails secret/path hygiene")
        interpreter = _match_interpreter(dependencies, declared.get("interpreter"), run_id)
        counts = parsed["counts"]
        if not counts["executed"]:
            raise EvidenceRefused(f"run {run_id} is a zero-case run")
        skip_details = _validate_detail_rows(
            declared.get("skip_details", []), counts["skip"], "skip",
            parsed["results"], run_id)
        xfail_details = _validate_detail_rows(
            declared.get("xfail_details", []), counts["xfail"], "xfail",
            parsed["results"], run_id)
        if role == "acceptance":
            if exit_code != 0 or not parsed["successful"]:
                raise EvidenceRefused(f"acceptance run {run_id} did not pass")
            if declared.get("complete_suite") is True and kind == "complete_suite":
                command_text = " ".join(command)
                if (counts["executed"] < MIN_COMPLETE_SUITE_CASES
                        or "unittest" not in command_text or "discover" not in command_text
                        or "-v" not in command):
                    raise EvidenceRefused(
                        f"run {run_id} is not a verbose complete suite with at least "
                        f"{MIN_COMPLETE_SUITE_CASES} cases")
                complete_acceptance += 1
        elif exit_code == 0 and parsed["successful"]:
            raise EvidenceRefused(
                f"historical negative control {run_id} did not demonstrate failure")

        log_member = f"logs/{log_rel.as_posix()}"
        hygiene = _hygiene_reasons(log_raw)
        bundle = {"status": "bundled", "member": log_member}
        bundle_log = declared.get("bundle_log", True)
        if bundle_log not in (True, False):
            raise EvidenceRefused(f"run {run_id} bundle_log must be boolean")
        declared_reason = str(declared.get("unbundled_reason") or "").strip()
        if bundle_log is False and not declared_reason:
            raise EvidenceRefused(
                f"run {run_id} explicitly unbundles its log without a reason")
        if hygiene:
            bundle = {"status": "unbundled_for_hygiene",
                      "reason": ", ".join(hygiene)}
            excluded_logs.append({
                "name": log_rel.as_posix(), "bytes": len(log_raw),
                "sha256": hashlib.sha256(log_raw).hexdigest(),
                "reason": bundle["reason"],
            })
        elif bundle_log is False:
            bundle = {"status": "unbundled_by_declaration",
                      "reason": declared_reason}
            excluded_logs.append({
                "name": log_rel.as_posix(), "bytes": len(log_raw),
                "sha256": hashlib.sha256(log_raw).hexdigest(),
                "reason": declared_reason,
            })
        else:
            if log_member in bundled_logs:
                raise EvidenceRefused(f"duplicate bundled log member {log_member}")
            bundled_logs[log_member] = log_raw
        records.append({
            "run_id": run_id, "kind": kind, "acceptance_role": role,
            "complete_suite": declared.get("complete_suite") is True,
            "command": command, "interpreter": interpreter,
            "dependency_profile": dependencies["identity"],
            "exit_code": exit_code, "counts": counts,
            "skip_details": skip_details, "xfail_details": xfail_details,
            "complete_log": {"name": log_rel.as_posix(), "bytes": len(log_raw),
                             "sha256": hashlib.sha256(log_raw).hexdigest(),
                             "bundle": bundle},
        })
    if not complete_acceptance:
        raise EvidenceRefused("no successful complete acceptance suite is recorded")
    manifest = {
        "schema": "ferc-final-test-run-manifest-v3-external",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "counts_basis": ("collected equals executed for unittest; every count is parsed "
                         "from one complete verbose log and checked against its summary"),
        "dependencies": dependencies,
        "runs": records,
        "totals": {name: sum(run["counts"][name] for run in records)
                   for name in ("collected", "executed", "pass", "fail", "error",
                                "skip", "xfail", "xpass")},
        "successful_complete_acceptance_suites": complete_acceptance,
    }
    return manifest, bundled_logs, excluded_logs


def _validate_archive(path: pathlib.Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise EvidenceRefused("release archive is absent, non-regular, or a symlink")
    archive_sha, archive_size = _sha_and_size(path)
    names = set()
    uncompressed = 0
    compressed = 0
    try:
        with zipfile.ZipFile(path, "r") as archive:
            for info in archive.infolist():
                pure = _safe_relative(info.filename, "archive member")
                name = pure.as_posix()
                if name in names:
                    raise EvidenceRefused(f"archive has duplicate member {name}")
                names.add(name)
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise EvidenceRefused(f"archive contains symlink {name}")
                if info.flag_bits & 0x1:
                    raise EvidenceRefused(f"archive contains encrypted member {name}")
                uncompressed += info.file_size
                compressed += info.compress_size
            bad = archive.testzip()
            if bad:
                raise EvidenceRefused(f"archive CRC failed at {bad}")
    except zipfile.BadZipFile as exc:
        raise EvidenceRefused(f"release archive is not a valid ZIP: {exc}") from None
    forbidden = {EVIDENCE_INDEX_NAME, RECEIPT_NAME}
    embedded = sorted(name for name in names if pathlib.PurePosixPath(name).name in forbidden)
    if embedded:
        raise EvidenceRefused(f"external receipt/index already embedded: {embedded}")
    return {"name": path.name, "bytes": archive_size, "sha256": archive_sha,
            "members": len(names), "uncompressed_bytes": uncompressed,
            "compressed_member_bytes": compressed, "crc": "PASS",
            "safe_paths": True, "duplicate_members": 0, "symlinks": 0}


def _validate_package_record(path: pathlib.Path, archive: Mapping[str, Any]) -> Tuple[bytes, dict]:
    raw, body = _load_json(path, "package-validation record")
    if not isinstance(body, dict) or body.get("status") != "PASS":
        raise EvidenceRefused("package-validation record is not PASS")
    schema = body.get("schema")
    if schema == "ferc-build-b-acceptance-v1":
        checks = body.get("checks") or {}
        missing = [name for name in BUILD_B_CHECKS if checks.get(name) is not True]
        if missing:
            raise EvidenceRefused(f"Build-B record failed or omitted checks: {missing}")
        identities = body.get("identities") or {}
        declared_sha = identities.get("archive_sha256")
        declared_size = identities.get("archive_bytes")
        check_summary = {name: True for name in BUILD_B_CHECKS}
    elif schema == "ferc-full-release-receipt-v2":
        declared_archive = body.get("archive") or {}
        declared_sha = declared_archive.get("sha256")
        declared_size = declared_archive.get("bytes")
        checks = (body.get("build_b_evidence") or {}).get("checks") or {}
        missing = [name for name in BUILD_B_CHECKS if checks.get(name) is not True]
        if missing:
            raise EvidenceRefused(f"full release receipt omits Build-B checks: {missing}")
        check_summary = {name: True for name in BUILD_B_CHECKS}
    else:
        raise EvidenceRefused(f"unsupported package-validation schema {schema!r}")
    if declared_sha != archive["sha256"] or declared_size != archive["bytes"]:
        raise EvidenceRefused("package-validation record names different archive bytes")
    return raw, {"schema": schema, "status": "PASS", "checks": check_summary,
                 "identity": {"name": path.name, "bytes": len(raw),
                              "sha256": hashlib.sha256(raw).hexdigest()}}


def _evidence_selection(spec_path: Optional[pathlib.Path], evidence_root: pathlib.Path,
                        members: Dict[str, bytes]) -> Tuple[List[dict], List[dict]]:
    if spec_path is None:
        return [], []
    _raw, spec = _load_json(spec_path, "evidence selection")
    if not isinstance(spec, dict) or spec.get("schema") != "ferc-evidence-selection-v1":
        raise EvidenceRefused("evidence selection schema is not v1")
    included = []
    unbundled = []
    for number, item in enumerate(spec.get("include", []), 1):
        if not isinstance(item, dict):
            raise EvidenceRefused(f"included evidence row {number} is not an object")
        path = _under(evidence_root, item.get("path"), f"included evidence row {number}")
        logical = _safe_relative(item.get("name"),
                                 f"included evidence row {number} name").as_posix()
        category = str(item.get("category") or "").strip()
        if not category:
            raise EvidenceRefused(f"included evidence row {number} has no category")
        raw = _read_regular(path, f"included evidence {logical}")
        _require_hygiene(raw, f"included evidence {logical}")
        member = "evidence/" + logical
        if member in members:
            raise EvidenceRefused(f"duplicate evidence ZIP member {member}")
        members[member] = raw
        included.append({"member": member, "category": category,
                         "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    rows = spec.get("unbundled_large_evidence", [])
    if not isinstance(rows, list):
        raise EvidenceRefused("unbundled_large_evidence must be a list")
    for number, item in enumerate(rows, 1):
        if not isinstance(item, dict):
            raise EvidenceRefused(f"unbundled evidence row {number} is not an object")
        path = _under(evidence_root, item.get("path"), f"unbundled evidence row {number}")
        logical = _safe_relative(item.get("name"),
                                 f"unbundled evidence row {number} name").as_posix()
        category = str(item.get("category") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if not category or not reason:
            raise EvidenceRefused(
                f"unbundled evidence row {number} needs category and explicit reason")
        ident = _identity(path, logical)
        ident.update({"category": category, "reason": reason})
        unbundled.append(ident)
    return included, unbundled


def _zip_bytes(path: pathlib.Path, members: Mapping[str, bytes]) -> dict:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9, allowZip64=True) as archive:
        for name in sorted(members):
            pure = _safe_relative(name, "evidence ZIP member")
            info = zipfile.ZipInfo(pure.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100644 & 0xFFFF) << 16
            info.create_system = 3
            archive.writestr(info, members[name])
    sha, size = _sha_and_size(path)
    if size >= MAX_EVIDENCE_ZIP_BYTES:
        raise EvidenceRefused(
            f"evidence ZIP is {size} bytes; target requires less than "
            f"{MAX_EVIDENCE_ZIP_BYTES}. Move large evidence to the explicit unbundled list")
    with zipfile.ZipFile(path, "r") as archive:
        bad = archive.testzip()
        if bad:
            raise EvidenceRefused(f"evidence ZIP CRC failed at {bad}")
    return {"name": EVIDENCE_ZIP_NAME, "bytes": size, "sha256": sha,
            "members": len(members), "target_bytes_exclusive": MAX_EVIDENCE_ZIP_BYTES,
            "within_target": True}


def build(args: argparse.Namespace) -> dict:
    archive_path = pathlib.Path(args.archive).resolve()
    validation_path = pathlib.Path(args.package_validation).resolve()
    run_spec_path = pathlib.Path(args.run_spec).resolve()
    logs_dir = pathlib.Path(args.logs_dir).resolve()
    runtime_path = pathlib.Path(args.runtime_requirements).resolve()
    out_dir = pathlib.Path(args.out_dir).resolve()
    if out_dir.exists():
        raise EvidenceRefused(f"output directory already exists; refusing overwrite: {out_dir}")
    if not out_dir.parent.is_dir():
        raise EvidenceRefused(f"output parent does not exist: {out_dir.parent}")
    evidence_root = (pathlib.Path(args.evidence_root).resolve()
                     if args.evidence_root else run_spec_path.parent)
    evidence_spec = pathlib.Path(args.evidence_spec).resolve() if args.evidence_spec else None

    archive = _validate_archive(archive_path)
    validation_raw, validation = _validate_package_record(validation_path, archive)
    runtime_raw, dependencies = _runtime_dependencies(runtime_path)
    _require_hygiene(runtime_raw, "runtime requirements")
    test_manifest, log_members, excluded_logs = _test_manifest(
        run_spec_path, logs_dir, dependencies)
    test_raw = _json_bytes(test_manifest)
    _require_hygiene(test_raw, "generated test manifest")

    members: Dict[str, bytes] = dict(log_members)
    members[TEST_MANIFEST_NAME] = test_raw
    members["runtime/" + runtime_path.name] = runtime_raw
    validation_hygiene = _hygiene_reasons(validation_raw)
    validation_bundle = "package_validation/" + validation_path.name
    if validation_hygiene:
        validation["bundle"] = {"status": "unbundled_for_hygiene",
                                "reason": ", ".join(validation_hygiene)}
    else:
        members[validation_bundle] = validation_raw
        validation["bundle"] = {"status": "bundled", "member": validation_bundle}

    included, unbundled_large = _evidence_selection(
        evidence_spec, evidence_root, members)
    member_index = [{"member": name, "bytes": len(raw),
                     "sha256": hashlib.sha256(raw).hexdigest()}
                    for name, raw in sorted(members.items())]

    stage = pathlib.Path(tempfile.mkdtemp(prefix=".ferc-external-evidence-",
                                         dir=str(out_dir.parent)))
    try:
        (stage / TEST_MANIFEST_NAME).write_bytes(test_raw)
        zip_identity = _zip_bytes(stage / EVIDENCE_ZIP_NAME, members)
        index = {
            "schema": "ferc-external-evidence-index-v1",
            "archive": archive,
            "package_validation": validation,
            "test_manifest": {"name": TEST_MANIFEST_NAME, "bytes": len(test_raw),
                              "sha256": hashlib.sha256(test_raw).hexdigest()},
            "evidence_zip": zip_identity,
            "bundled_members": member_index,
            "selected_evidence": included,
            "unbundled_large_evidence": unbundled_large,
            "unbundled_test_logs": excluded_logs,
            "explicit_large_evidence_inventory": True,
            "non_circular": {
                "release_archive_modified": False,
                "index_inside_evidence_zip": False,
                "receipt_inside_release_archive": False,
                "receipt_inside_evidence_zip": False,
            },
        }
        index_raw = _json_bytes(index)
        _require_hygiene(index_raw, "generated evidence index")
        (stage / EVIDENCE_INDEX_NAME).write_bytes(index_raw)

        receipt = {
            "schema": "ferc-external-release-evidence-receipt-v1",
            "status": "PASS",
            "issued_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "release_archive": archive,
            "package_validation": validation,
            "test_manifest": {"name": TEST_MANIFEST_NAME, "bytes": len(test_raw),
                              "sha256": hashlib.sha256(test_raw).hexdigest(),
                              "totals": test_manifest["totals"]},
            "runtime_requirements": dependencies["identity"],
            "evidence_zip": zip_identity,
            "evidence_index": {"name": EVIDENCE_INDEX_NAME, "bytes": len(index_raw),
                               "sha256": hashlib.sha256(index_raw).hexdigest()},
            "chain": [
                "immutable release archive -> hash/CRC/safe-member verification",
                "archive-bound PASS package validation + complete test-log parsing",
                "bounded evidence ZIP + external evidence index",
                "this external leaf receipt",
            ],
            "scope_note": ("The receipt binds package identity, declared package validation, "
                           "test evidence and the evidence bundle. It does not convert "
                           "documented unavailable/review source states into recovered facts."),
            "non_circular": ("This receipt and its evidence index were written after archive "
                             "validation and are absent from both the release archive and "
                             "evidence ZIP."),
        }
        receipt_raw = _json_bytes(receipt)
        _require_hygiene(receipt_raw, "generated external receipt")
        (stage / RECEIPT_NAME).write_bytes(receipt_raw)
        os.replace(stage, out_dir)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return receipt


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True,
                        help="finished immutable full-release ZIP")
    parser.add_argument("--package-validation", required=True,
                        help="archive-bound PASS Build-B evidence or v2 full receipt")
    parser.add_argument("--run-spec", required=True,
                        help="ferc-test-run-spec-v1 input naming complete logs")
    parser.add_argument("--logs-dir", required=True,
                        help="root for relative complete-log paths")
    parser.add_argument("--runtime-requirements", required=True,
                        help="exact RUNTIME_REQUIREMENTS.json used by the test runs")
    parser.add_argument("--evidence-spec",
                        help="optional ferc-evidence-selection-v1 input")
    parser.add_argument("--evidence-root",
                        help="root for evidence-spec paths (defaults to run-spec directory)")
    parser.add_argument("--out-dir", required=True,
                        help="new, absent directory for the four external outputs")
    args = parser.parse_args(argv)
    try:
        receipt = build(args)
    except (EvidenceRefused, OSError, ValueError, KeyError) as exc:
        print(f"EXTERNAL EVIDENCE: FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("EXTERNAL EVIDENCE: PASS")
    print(f"  archive {receipt['release_archive']['bytes']} bytes "
          f"{receipt['release_archive']['sha256']}")
    print(f"  outputs {pathlib.Path(args.out_dir).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
