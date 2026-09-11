#!/usr/bin/env python3
"""Build the hash-bound test manifest consumed by final release records.

The input is a small JSON run specification.  Counts and test identifiers are
never accepted from that specification: they are reconstructed from complete
``unittest -v`` logs, checked against each log's terminal summary and bound to
the current acceptance/test/tool tree.  For example::

    {
      "schema": "ferc-final-test-run-spec-v1",
      "issue_test_evidence": {"A01": ["tests.test_x.Case.test_control"]},
      "exception_test_evidence": {"blk-1": ["tests.test_x.Case.test_control"]},
      "runs": [{
        "run_id": "python314-complete",
        "kind": "complete_suite",
        "acceptance_role": "acceptance",
        "complete_suite": true,
        "log": "full_python314.log",
        "command": ["python3.14", "-B", "-m", "unittest", "discover", "-v"],
        "interpreter": "CPython 3.14.7",
        "exit_code": 0,
        "skip_details": [],
        "xfail_details": []
      }]
    }

An intentionally defective historical baseline may be included with
``acceptance_role=historical_negative_control``.  It must have a non-zero exit
code and a genuinely failing log.  Such results are evidence of the negative
control only and cannot satisfy an issue or exception evidence citation.

This program only reads the candidate tree/specification/logs and atomically
writes the requested JSON file.  It does not execute tests or import pipeline
production modules.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile
from typing import Any, Dict, List, Mapping, Sequence, Tuple

try:
    from implementation.unittest_log_parser import (
        UnittestLogRefused, parse_unittest_log as _parse_shared_unittest_log)
except ModuleNotFoundError:  # Direct script execution from implementation/.
    from unittest_log_parser import (  # type: ignore
        UnittestLogRefused, parse_unittest_log as _parse_shared_unittest_log)


SPEC_SCHEMA = "ferc-final-test-run-spec-v1"
MANIFEST_SCHEMA = "ferc-final-test-run-manifest-v2"
MIN_COMPLETE_SUITE_CASES = 100
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
COUNT_KEYS = ("collected", "executed", "pass", "fail", "error", "skip",
              "xfail", "xpass")


class ManifestRefused(RuntimeError):
    """The supplied logs/specification cannot support a final test claim."""


def _read_regular(path: pathlib.Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ManifestRefused(f"{label} is absent, non-regular, or a symlink: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ManifestRefused(
            f"cannot read {label}: {type(exc).__name__}: {exc}") from None


def _load_json(path: pathlib.Path, label: str) -> Any:
    raw = _read_regular(path, label)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestRefused(f"{label} is not valid UTF-8 JSON: {exc}") from None


def _safe_relative(value: Any, label: str) -> pathlib.PurePosixPath:
    if not isinstance(value, str):
        raise ManifestRefused(f"{label} must be a relative POSIX path")
    pure = pathlib.PurePosixPath(value)
    if (not value or pure.is_absolute() or ".." in pure.parts or "." in pure.parts
            or "\\" in value or any(not part for part in pure.parts)):
        raise ManifestRefused(f"{label} is not a safe relative POSIX path: {value!r}")
    return pure


def _under(root: pathlib.Path, relative: Any, label: str) -> Tuple[pathlib.Path, str]:
    pure = _safe_relative(relative, label)
    candidate = root
    for part in pure.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ManifestRefused(f"{label} traverses a symlink")
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        raise ManifestRefused(f"{label} escapes the logs directory") from None
    return resolved, pure.as_posix()


def parse_unittest_log(raw: bytes, label: str) -> Dict[str, Any]:
    """Parse one complete verbose unittest log or fail closed."""
    try:
        return _parse_shared_unittest_log(raw, label)
    except UnittestLogRefused as exc:
        raise ManifestRefused(str(exc)) from None


def _detail_rows(value: Any, expected: int, status: str,
                 results: Mapping[str, str], run_id: str) -> List[dict]:
    if not isinstance(value, list) or len(value) != expected:
        raise ManifestRefused(
            f"run {run_id} must classify all {expected} {status} result(s)")
    clean: List[dict] = []
    seen = set()
    for number, row in enumerate(value, 1):
        if not isinstance(row, dict):
            raise ManifestRefused(f"run {run_id} {status} detail {number} is not an object")
        test_id = str(row.get("test") or "").strip()
        classification = str(row.get("classification") or "").strip()
        reason = str(row.get("reason") or "").strip()
        if not test_id or not classification or not reason:
            raise ManifestRefused(f"run {run_id} has incomplete {status} detail")
        if row.get("required") is not False:
            raise ManifestRefused(f"run {run_id} leaves a required capability {status}")
        if test_id in seen or results.get(test_id) != status:
            raise ManifestRefused(
                f"run {run_id} {status} detail does not uniquely name an observed result")
        seen.add(test_id)
        clean.append({"test": test_id, "classification": classification,
                      "reason": reason, "required": False})
    return clean


def _snapshot_digest(root: pathlib.Path, paths: Sequence[pathlib.Path]) -> str:
    root = root.resolve()
    files: List[pathlib.Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(item for item in path.rglob("*")
                         if item.is_file() and "__pycache__" not in item.parts
                         and item.suffix != ".pyc")
        elif path.is_file():
            files.append(path)
    digest = hashlib.sha256()
    for path in sorted(set(files), key=lambda item: str(item)):
        if path.is_symlink():
            raise ManifestRefused(f"test snapshot includes symlink: {path}")
        resolved = path.resolve()
        try:
            label = str(resolved.relative_to(root))
        except ValueError:
            raise ManifestRefused(f"test snapshot path escapes candidate root: {path}") from None
        digest.update(label.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(_read_regular(resolved, label)).hexdigest().encode("ascii")
                      + b"\n")
    return digest.hexdigest()


def test_tool_snapshot(root: pathlib.Path) -> str:
    """Mirror the final-record consumer's deliberately narrow test snapshot."""
    return _snapshot_digest(root, [
        root / "tests", root / "acceptance", root / "tools",
        root / "build_release.py",
        root / "implementation" / "build_final_release_records.py",
        root / "implementation" / "build_test_run_manifest.py",
        root / "implementation" / "unittest_log_parser.py",
        root / "implementation" / "run_build_b_acceptance.py",
        root / "implementation" / "finalize_external_release_evidence.py",
        root / "implementation" / "import_bounded_elibrary_search_capture.py",
    ])


def _evidence_map(value: Any, label: str, successful_ids: set) -> Dict[str, List[str]]:
    if not isinstance(value, dict):
        raise ManifestRefused(f"{label} must be an object")
    clean: Dict[str, List[str]] = {}
    for record_id, cited in value.items():
        if not isinstance(record_id, str) or not record_id.strip():
            raise ManifestRefused(f"{label} has an invalid record id")
        if not isinstance(cited, list) or not cited:
            raise ManifestRefused(f"{label}.{record_id} must cite at least one passing test")
        if (not all(isinstance(test_id, str) and test_id.strip() for test_id in cited)
                or len(cited) != len(set(cited))):
            raise ManifestRefused(f"{label}.{record_id} has invalid or duplicate test ids")
        absent = sorted(set(cited) - successful_ids)
        if absent:
            raise ManifestRefused(
                f"{label}.{record_id} cites tests not observed passing in an "
                f"acceptance run: {absent}")
        clean[record_id] = sorted(cited)
    return {key: clean[key] for key in sorted(clean)}


def build_manifest(root: pathlib.Path, logs_dir: pathlib.Path, spec: Any) -> dict:
    if not isinstance(spec, dict) or spec.get("schema") != SPEC_SCHEMA:
        raise ManifestRefused(f"test run specification schema must be {SPEC_SCHEMA}")
    runs = spec.get("runs")
    if not isinstance(runs, list) or not runs:
        raise ManifestRefused("test run specification has no runs")

    seen = set()
    successful_ids = set()
    complete_success = 0
    records = []
    for number, run in enumerate(runs, 1):
        if not isinstance(run, dict):
            raise ManifestRefused(f"test run {number} is not an object")
        run_id = str(run.get("run_id") or "").strip()
        if not SAFE_RUN_ID.fullmatch(run_id) or run_id in seen:
            raise ManifestRefused(f"test run {number} has invalid or duplicate run_id")
        seen.add(run_id)
        role = run.get("acceptance_role")
        if role not in ("acceptance", "historical_negative_control"):
            raise ManifestRefused(f"run {run_id} has invalid acceptance_role")
        kind = str(run.get("kind") or "").strip()
        if not kind:
            raise ManifestRefused(f"run {run_id} has no kind")
        log_path, log_rel = _under(logs_dir, run.get("log"), f"run {run_id} log")
        log_raw = _read_regular(log_path, f"run {run_id} complete log")
        parsed = parse_unittest_log(log_raw, f"run {run_id} complete log")

        exit_code = run.get("exit_code")
        if not isinstance(exit_code, int):
            raise ManifestRefused(f"run {run_id} has no exact integer exit code")
        if (exit_code == 0) != parsed["successful"]:
            raise ManifestRefused(
                f"run {run_id} exit code {exit_code} disagrees with its unittest result")
        if role == "acceptance" and not parsed["successful"]:
            raise ManifestRefused(f"acceptance run {run_id} did not pass")
        if role == "historical_negative_control" and parsed["successful"]:
            raise ManifestRefused(
                f"historical negative control {run_id} did not demonstrate failure")

        command = run.get("command")
        if not ((isinstance(command, str) and command.strip()) or
                (isinstance(command, list) and command
                 and all(isinstance(item, str) and item for item in command))):
            raise ManifestRefused(f"run {run_id} has no exact command")
        interpreter = run.get("interpreter")
        if not isinstance(interpreter, str) or not interpreter.strip():
            raise ManifestRefused(f"run {run_id} has no interpreter")

        skip_details = _detail_rows(
            run.get("skip_details", []), parsed["counts"]["skip"], "skip",
            parsed["results"], run_id)
        xfail_details = _detail_rows(
            run.get("xfail_details", []), parsed["counts"]["xfail"], "xfail",
            parsed["results"], run_id)
        complete_suite = run.get("complete_suite") is True
        if role == "acceptance":
            successful_ids.update(test_id for test_id, status in parsed["results"].items()
                                  if status == "pass")
            if complete_suite and kind == "complete_suite":
                command_text = " ".join(command) if isinstance(command, list) else command
                if (parsed["counts"]["executed"] < MIN_COMPLETE_SUITE_CASES
                        or "unittest" not in command_text or "discover" not in command_text
                        or not (" -v" in " " + command_text or "--verbose" in command_text)):
                    raise ManifestRefused(
                        f"run {run_id} is not a verbose complete-suite invocation with at "
                        f"least {MIN_COMPLETE_SUITE_CASES} cases")
                complete_success += 1

        records.append({
            "run_id": run_id, "kind": kind, "acceptance_role": role,
            "complete_suite": complete_suite, "log": log_rel,
            "bytes": len(log_raw), "sha256": hashlib.sha256(log_raw).hexdigest(),
            "command": command, "interpreter": interpreter, "exit_code": exit_code,
            "counts": parsed["counts"], "skip_details": skip_details,
            "xfail_details": xfail_details,
        })
    if not complete_success:
        raise ManifestRefused("no successful non-zero complete candidate suite is recorded")

    return {
        "schema": MANIFEST_SCHEMA,
        "tested_tree_snapshot": test_tool_snapshot(root),
        "issue_test_evidence": _evidence_map(
            spec.get("issue_test_evidence"), "issue_test_evidence", successful_ids),
        "exception_test_evidence": _evidence_map(
            spec.get("exception_test_evidence"), "exception_test_evidence", successful_ids),
        "runs": records,
    }


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
            + "\n").encode("utf-8")


def _atomic_write(path: pathlib.Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ManifestRefused(f"output is a symlink: {path}")
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True,
                        help="candidate root used for the test/tool snapshot")
    parser.add_argument("--logs-dir", required=True,
                        help="directory containing complete logs named by the specification")
    parser.add_argument("--run-spec", required=True,
                        help=f"UTF-8 JSON {SPEC_SCHEMA} input")
    parser.add_argument("--out", required=True,
                        help=f"output {MANIFEST_SCHEMA} JSON path")
    args = parser.parse_args(argv)
    try:
        root = pathlib.Path(args.root).resolve()
        logs_dir = pathlib.Path(args.logs_dir).resolve()
        if not root.is_dir() or root.is_symlink():
            raise ManifestRefused("candidate root is absent, non-directory, or a symlink")
        if not logs_dir.is_dir() or logs_dir.is_symlink():
            raise ManifestRefused("logs directory is absent, non-directory, or a symlink")
        spec = _load_json(pathlib.Path(args.run_spec).resolve(), "test run specification")
        manifest = build_manifest(root, logs_dir, spec)
        raw = _json_bytes(manifest)
        _atomic_write(pathlib.Path(args.out).resolve(), raw)
        print(json.dumps({
            "status": "PASS", "schema": MANIFEST_SCHEMA,
            "output": str(pathlib.Path(args.out).resolve()),
            "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "runs": len(manifest["runs"]),
            "executed": sum(run["counts"]["executed"] for run in manifest["runs"]),
        }, sort_keys=True))
        return 0
    except ManifestRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
