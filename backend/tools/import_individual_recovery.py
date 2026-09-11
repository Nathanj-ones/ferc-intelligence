#!/usr/bin/env python3
"""Verify and import the complete one-ID eLibrary recovery response.

The public eLibrary file-list UI issues one ``DownloadP8File`` request per
selected file.  For accession 20231229-5212, one of those one-ID requests
returned a ZIP containing the complete ten-file accession after the equivalent
all-ID request failed.  This importer is deliberately narrow: it accepts only
the pinned capture, proves the request/cache identity and exact ZIP membership,
and imports only the successful HTTP-200 response into a separate SourceCache.
The nine retained non-200 observations remain evidence; they are never cached.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional, Sequence


HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from tools.import_official_recovery import (  # noqa: E402
    CacheConflictError,
    EvidenceVerificationError,
    ImportItem,
    RecoveryImportError,
    RecoveryPlan,
    canonical_download_request,
    import_plan,
)


ACCESSION = "20231229-5212"
PINNED_LEDGER_SHA256 = "54acae8440cff3e9547e643b19e3b723f0113387f0ee114517123a482fefd43f"
PINNED_COMPLETE_ZIP_SHA256 = "dae325d243bb7ecb3ac6db58c7163e197ffcc593b73e583330372f0fe03c400b"
PINNED_COMPLETE_ZIP_BYTES = 109_287_608
DEFAULT_LEDGER = HERE / "implementation_logs/input_recovery/OFFICIAL_FERC_INDIVIDUAL_20231229-5212.json"
DEFAULT_RECOVERY_ROOT = HERE / "inputs/official_ferc_recovery"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(path: Path) -> dict:
    return {"path": str(path.resolve()), "bytes": path.stat().st_size,
            "sha256": _sha256_file(path)}


def _require_immutable_file(path: Path, root: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        root_resolved = root.resolve(strict=True)
    except OSError as exc:
        raise EvidenceVerificationError(f"missing {label}: {path}: {exc}") from None
    if path.is_symlink() or not path.is_file():
        raise EvidenceVerificationError(f"{label} is not a regular file: {path}")
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise EvidenceVerificationError(f"{label} escapes recovery root: {path}")
    if stat.S_IMODE(path.stat().st_mode) & 0o222:
        raise EvidenceVerificationError(f"{label} is writable, not immutable: {path}")
    return resolved


def _safe_member_name(name: str) -> bool:
    pure = PurePosixPath(name)
    return bool(name and not pure.is_absolute() and ".." not in pure.parts
                and "\\" not in name and len(pure.parts) == 1)


def _verify_complete_zip(body: bytes, listed_rows: list[dict]) -> list[dict]:
    expected = {
        f"{ACCESSION}_{str(row.get('Orig_File_Name') or '')}":
            int(row.get("File_Size_Num") or 0)
        for row in listed_rows
    }
    if len(expected) != len(listed_rows) or any(not name or size <= 0
                                                for name, size in expected.items()):
        raise EvidenceVerificationError("file list has duplicate/blank names or invalid sizes")
    member_rows = []
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if any(info.is_dir() for info in infos):
                raise EvidenceVerificationError("complete response contains a directory member")
            if any(not _safe_member_name(name) for name in names):
                raise EvidenceVerificationError("complete response contains an unsafe member path")
            if len(names) != len(set(names)) or len(names) != len({n.casefold() for n in names}):
                raise EvidenceVerificationError("complete response has duplicate member names")
            if set(names) != set(expected):
                missing = sorted(set(expected) - set(names))
                extra = sorted(set(names) - set(expected))
                raise EvidenceVerificationError(
                    f"ZIP membership disagrees with official file list: missing={missing}, extra={extra}")
            for info in infos:
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode) or info.flag_bits & 0x1:
                    raise EvidenceVerificationError(
                        f"ZIP member is a symlink or encrypted: {info.filename}")
                if info.file_size != expected[info.filename]:
                    raise EvidenceVerificationError(
                        f"ZIP member size mismatch for {info.filename}: "
                        f"expected {expected[info.filename]}, got {info.file_size}")
                digest = hashlib.sha256()
                total = 0
                with archive.open(info, "r") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        total += len(chunk)
                        digest.update(chunk)
                if total != info.file_size:
                    raise EvidenceVerificationError(
                        f"short ZIP member read for {info.filename}: {total}/{info.file_size}")
                member_rows.append({"name": info.filename, "bytes": total,
                                    "sha256": digest.hexdigest(), "crc32": f"{info.CRC:08x}"})
    except zipfile.BadZipFile as exc:
        raise EvidenceVerificationError(f"HTTP-200 response is not a valid ZIP: {exc}") from None
    return sorted(member_rows, key=lambda row: row["name"])


def load_verified_plan(ledger_path: Path, recovery_root: Path,
                       expected_ledger_sha256: str = PINNED_LEDGER_SHA256) -> tuple[RecoveryPlan, dict]:
    ledger_path = Path(ledger_path)
    recovery_root = Path(recovery_root)
    if ledger_path.is_symlink() or not ledger_path.is_file():
        raise EvidenceVerificationError(f"individual recovery ledger is missing: {ledger_path}")
    ledger_identity = _identity(ledger_path)
    if ledger_identity["sha256"] != expected_ledger_sha256:
        raise EvidenceVerificationError(
            f"individual recovery ledger SHA-256 mismatch: expected {expected_ledger_sha256}, "
            f"got {ledger_identity['sha256']}")
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceVerificationError(f"individual recovery ledger is invalid JSON: {exc}") from None
    if ledger.get("schema") != "official_ferc_individual_recovery_v1":
        raise EvidenceVerificationError("unexpected individual recovery ledger schema")
    if ledger.get("accession_number") != ACCESSION:
        raise EvidenceVerificationError("individual recovery ledger accession mismatch")
    if (ledger.get("network_policy")
            != "one request per public attachment; no retries/auth/cookies/bypass"):
        raise EvidenceVerificationError("individual recovery network policy is not the pinned policy")

    list_meta = ledger.get("list_response") or {}
    list_path = _require_immutable_file(Path(str(list_meta.get("path") or "")), recovery_root,
                                        "official file-list body")
    list_identity = _identity(list_path)
    if (list_identity["bytes"] != list_meta.get("bytes")
            or list_identity["sha256"] != list_meta.get("sha256")):
        raise EvidenceVerificationError("official file-list body disagrees with the recovery ledger")
    try:
        file_list_doc = json.loads(list_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceVerificationError(f"official file-list body is invalid JSON: {exc}") from None
    listed_rows = file_list_doc.get("DataList") if isinstance(file_list_doc, dict) else None
    if not isinstance(listed_rows, list) or len(listed_rows) != 10 or file_list_doc.get("ErrorList"):
        raise EvidenceVerificationError("official file list is not the expected error-free ten-file population")
    ids = [str(row.get("ID") or "") for row in listed_rows]
    if len(set(ids)) != 10 or any(not value for value in ids):
        raise EvidenceVerificationError("official file list has blank or duplicate attachment IDs")
    if any(str(row.get("Accession_Number") or ACCESSION) != ACCESSION for row in listed_rows):
        raise EvidenceVerificationError("official file list crosses accession boundaries")

    rows = ledger.get("results")
    if not isinstance(rows, list) or len(rows) != 10:
        raise EvidenceVerificationError("individual recovery ledger must contain ten response rows")
    orders = [row.get("attachment_order") for row in rows]
    if orders != list(range(1, 11)):
        raise EvidenceVerificationError("individual recovery rows are not in exact file-list order")
    observed_ids = [str(row.get("attachment_id") or "") for row in rows]
    if observed_ids != ids:
        raise EvidenceVerificationError("individual recovery IDs disagree with official file-list order")
    successes = []
    failed_observations = []
    for index, (row, listed) in enumerate(zip(rows, listed_rows), start=1):
        if (row.get("schema") != "official_ferc_individual_response_v1"
                or row.get("accession_number") != ACCESSION
                or row.get("availability_mode") != "P"
                or row.get("file_name") != listed.get("Orig_File_Name")
                or row.get("listed_bytes") != listed.get("File_Size_Num")):
            raise EvidenceVerificationError(f"individual response row {index} disagrees with file list")
        request = row.get("request") or {}
        payload, canonical_blob, cache_url = canonical_download_request(
            ACCESSION, [str(row["attachment_id"])])
        del payload
        if (request.get("method") != "POST"
                or request.get("official_url")
                    != "https://elibrary.ferc.gov/eLibraryWebAPI/api/File/DownloadP8File"
                or request.get("credentials_or_cookies_sent") is not False
                or request.get("retry_count") != 0
                or request.get("payload_bytes") != len(canonical_blob)
                or request.get("payload_sha256") != _sha256_bytes(canonical_blob)
                or request.get("cache_url") != cache_url):
            raise EvidenceVerificationError(f"individual response row {index} request identity mismatch")
        response = row.get("response") or {}
        body_path = _require_immutable_file(Path(str(response.get("body_path") or "")),
                                            recovery_root, f"response body {index}")
        body_identity = _identity(body_path)
        if (response.get("complete_body") is not True
                or response.get("network_error") is not None
                or response.get("bytes") != body_identity["bytes"]
                or response.get("sha256") != body_identity["sha256"]):
            raise EvidenceVerificationError(f"individual response body {index} identity mismatch")
        status = response.get("http_status")
        if status == 200:
            successes.append((row, body_path, body_identity, cache_url))
        else:
            if not isinstance(status, int) or status < 400:
                raise EvidenceVerificationError(f"individual response row {index} has invalid status")
            failed_observations.append({
                "attachment_id": row["attachment_id"], "http_status": status,
                "response_bytes": body_identity["bytes"],
                "response_sha256": body_identity["sha256"],
                "disposition": "retained HTTP observation; not imported and not source unavailability",
            })
    if len(successes) != 1 or len(failed_observations) != 9:
        raise EvidenceVerificationError("pinned capture must contain one success and nine non-200 observations")

    success, body_path, body_identity, cache_url = successes[0]
    if (body_identity["bytes"] != PINNED_COMPLETE_ZIP_BYTES
            or body_identity["sha256"] != PINNED_COMPLETE_ZIP_SHA256):
        raise EvidenceVerificationError("complete ZIP does not have the pinned size and SHA-256")
    body = body_path.read_bytes()
    if len(body) != body_identity["bytes"] or _sha256_bytes(body) != body_identity["sha256"]:
        raise EvidenceVerificationError("complete ZIP changed while it was being verified")
    members = _verify_complete_zip(body, listed_rows)
    requested_id = str(success["attachment_id"])
    item = ImportItem(
        accession=ACCESSION,
        response_kind="single_attachment_request_complete_accession_zip",
        cache_url=cache_url,
        body=body,
        content_sha256=body_identity["sha256"],
        byte_size=body_identity["bytes"],
        media_type="application/octet-stream",
        raw_body_path=body_identity["path"],
        http_status=200,
        canonical_payload_sha256=str(success["request"]["payload_sha256"]),
        attachment_ids=(requested_id,),
    )
    plan = RecoveryPlan(
        results_identity=ledger_identity,
        hash_index_identity={"not_applicable": True,
                             "reason": "the signed ledger records every complete body identity"},
        entries=(item,), blockers=(), accessions=(ACCESSION,),
    )
    verification = {
        "schema": "official_ferc_individual_import_verification_v1",
        "accession_number": ACCESSION,
        "ledger_identity": ledger_identity,
        "list_identity": list_identity,
        "successful_request_attachment_id": requested_id,
        "complete_zip_identity": body_identity,
        "members": members,
        "failed_observations": failed_observations,
    }
    return plan, verification


def execute_import(ledger_path: Path, recovery_root: Path, cache_root: Path,
                   run_inventory: Path,
                   expected_ledger_sha256: str = PINNED_LEDGER_SHA256) -> dict:
    recovery_resolved = Path(recovery_root).resolve(strict=True)
    cache_resolved = Path(cache_root).resolve()
    inventory_resolved = Path(run_inventory).resolve()
    if (cache_resolved == recovery_resolved or cache_resolved in recovery_resolved.parents
            or recovery_resolved in cache_resolved.parents):
        raise RecoveryImportError("cache and immutable recovery evidence must be separate trees")
    if inventory_resolved == recovery_resolved or recovery_resolved in inventory_resolved.parents:
        raise RecoveryImportError("run inventory must be outside immutable recovery evidence")
    plan, verification = load_verified_plan(
        ledger_path, recovery_root, expected_ledger_sha256=expected_ledger_sha256)
    record = import_plan(plan, cache_root=cache_root, run_inventory=run_inventory)
    record["verification"] = verification
    return record


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--recovery-root", type=Path, default=DEFAULT_RECOVERY_ROOT)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--run-inventory", type=Path, required=True)
    parser.add_argument("--apply", action="store_true",
                        help="Required acknowledgement that the target cache may be updated")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.apply:
        parser.error("--apply is required; no cache is selected or modified implicitly")
    try:
        record = execute_import(args.ledger, args.recovery_root, args.cache_root,
                                args.run_inventory)
    except RecoveryImportError as exc:
        print(json.dumps({"status": "failed", "type": type(exc).__name__,
                          "detail": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps({
        "status": record["status"], "run_id": record["run_id"],
        "imported": record["imported"],
        "already_present_identical": record["already_present_identical"],
        "complete_zip": record["verification"]["complete_zip_identity"],
        "member_count": len(record["verification"]["members"]),
        "retained_non_200_observations": len(record["verification"]["failed_observations"]),
        "run_inventory": record["append_only_inventory"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
