"""
A13 -- the release builder approved a package missing a required export.

The audit removed `exports/quarterly_key_metrics.csv` in a disposable clone and
unmodified `build_release.py` returned PASS with an 84-file manifest, because the
manifest was an inventory of whatever files happened to exist: delete a required
output and the manifest simply has one fewer entry to verify against.

Everything here is asserted against a REAL candidate tree that the REAL builder
runs on, with `--root` pointed at a disposable copy. Nothing takes the builder's
own report as evidence. The positive control runs first, so a failure caused by
the mutation cannot be confused with a candidate that was broken to begin with.
"""

from __future__ import annotations

import json
import hashlib
import pathlib
import subprocess
import sys

from . import candidate, contract
from .harness import TREE, Tier, acceptance, must_reject, require, require_population

BUILDER = TREE / "build_release.py"


def _run_builder(root: pathlib.Path, *, payload: str = "lite") -> subprocess.CompletedProcess:
    """Run the real build_release.py against a disposable candidate tree."""
    return subprocess.run(
        [sys.executable, str(BUILDER), "--payload", payload,
         "--root", str(root), "--out-dir", str(root.parent)],
        capture_output=True, text=True, cwd=str(TREE), timeout=900)


def _table_counts(root: pathlib.Path) -> dict[str, int]:
    import sqlite3
    db = root / "staging" / "operating_assets.sqlite"
    if not db.is_file():
        return {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
           for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    return out


# ------------------------------------------------------------------ contract shape

@acceptance(issue="A13.0", group="release",
            mutation="a required-output list derived from whatever files exist")
def t_contract_is_declared_not_inventoried(env):
    """the required-output contract is a fixed declaration, not a tree walk"""
    src = contract.__file__
    text = pathlib.Path(src).read_text(encoding="utf-8")
    require_population(contract.REQUIRED_OUTPUTS, "declared required outputs", minimum=15)
    for bad in ("rglob", "glob(", "iterdir", "walk("):
        require(bad not in text,
                f"acceptance/contract.py calls {bad!r}: the contract must be a fixed "
                "declaration. A contract that enumerates the tree is the A13 defect "
                "wearing a different hat -- delete a file and it stops being required.")
    groups = {r.group for r in contract.REQUIRED_OUTPUTS}
    for required_group in contract.MANDATORY_GROUPS:
        require(required_group in groups,
                f"mandatory group {required_group!r} has no declared outputs")
    require("exports/quarterly_key_metrics.csv" in contract.REQUIRED_BY_PATH,
            "the artefact named by the A13 acceptance test is not declared required")
    return (f"{len(contract.REQUIRED_OUTPUTS)} outputs declared across "
            f"{len(groups)} groups; no filesystem enumeration in the contract")


# ------------------------------------------------------------------ the named artefact

@acceptance(issue="A13.1", group="release",
            mutation="removing exports/quarterly_key_metrics.csv before manifest generation")
def t_removing_quarterly_metrics_fails_the_release(env):
    """removing quarterly_key_metrics.csv makes release construction FAIL"""
    require(BUILDER.is_file(), f"build_release.py absent: {BUILDER}")
    root = candidate.build_candidate(env, "a13_quarterly")

    # POSITIVE CONTROL FIRST. If the intact candidate does not build, a failure
    # after the mutation would prove nothing about the mutation.
    good = _run_builder(root)
    require(good.returncode == 0,
            f"the INTACT candidate failed to build (rc={good.returncode}), so the "
            f"mutation below could not be attributed to the mutation.\n"
            f"stdout tail:\n{good.stdout[-1500:]}\nstderr tail:\n{good.stderr[-800:]}")
    manifest_path = root / "artifact_manifest.json"
    manifest_before_raw = manifest_path.read_bytes()
    manifest_before = json.loads(manifest_before_raw)
    n_before = len(manifest_before["files"])
    receipt_path = root / "release_receipt.json"
    require(receipt_path.is_file(), "the intact positive control issued no receipt")
    receipt_before_raw = receipt_path.read_bytes()
    receipt_before = json.loads(receipt_before_raw)
    archive_name = (receipt_before.get("archive") or {}).get("name")
    require(archive_name, "the intact positive-control receipt names no archive")
    archive_path = root.parent / pathlib.PurePosixPath(archive_name).name
    require(archive_path.is_file(), "the intact positive-control archive is absent")
    archive_before_hash = hashlib.sha256(archive_path.read_bytes()).hexdigest()

    # THE MUTATION -- exactly the audit's: remove it BEFORE manifest generation.
    removed = candidate.remove_output(root, "exports/quarterly_key_metrics.csv")
    require(not removed.is_file(), "the mutation did not take")

    bad = _run_builder(root)
    require(bad.returncode != 0,
            f"the release SUCCEEDED (rc=0) with a required export removed. "
            f"This is A13 unfixed.\nstdout tail:\n{bad.stdout[-2000:]}")

    # It must fail for the RIGHT reason, and before a manifest exists.
    require("quarterly_key_metrics" in bad.stdout,
            f"the release failed but never named the missing artefact:\n{bad.stdout[-1500:]}")
    manifest_after_raw = manifest_path.read_bytes()
    manifest_after = json.loads(manifest_after_raw)
    require(len(manifest_after["files"]) == n_before,
            "the manifest was REGENERATED during the failing build. A missing required "
            f"output must fail before the manifest is written; it went from {n_before} "
            f"to {len(manifest_after['files'])} entries, which is the shrinking-inventory "
            "defect itself.")

    require(manifest_after_raw == manifest_before_raw,
            "the failed attempt changed the last-good manifest")
    require(receipt_path.read_bytes() == receipt_before_raw,
            "the failed attempt overwrote the last-good receipt instead of preserving it")
    require(hashlib.sha256(archive_path.read_bytes()).hexdigest() == archive_before_hash,
            "the failed attempt changed the last-good archive")

    failure_path = root / "release_attempt_failure_lite.json"
    require(failure_path.is_file(),
            "the refused attempt preserved last-good output but wrote no failure sidecar")
    failure = json.loads(failure_path.read_bytes())
    require(failure.get("status") == "FAIL"
            and failure.get("failed_stage") == "freeze_payload",
            f"the attempt sidecar is not an exact freeze-stage failure: {failure}")
    hits = [row for row in failure.get("contract_violations") or []
            if row.get("path") == "exports/quarterly_key_metrics.csv"
            and row.get("kind") == "MISSING_REQUIRED_OUTPUT"]
    require(len(hits) == 1,
            f"the attempt sidecar does not carry the exact missing-output cause: {hits}")
    return (f"intact candidate built ({n_before} manifest entries); removing "
            f"quarterly_key_metrics.csv failed the release (rc={bad.returncode}) "
            "before any manifest was regenerated; last-good manifest/receipt/archive "
            "were preserved and the failed attempt was recorded separately")


# ------------------------------------------------------------------ mandatory groups

@acceptance(issue="A13.2", group="release",
            mutation="removing the mandatory annotations, status and coverage outputs")
def t_missing_mandatory_group_outputs_fail(env):
    """missing mandatory annotations/status/coverage outputs also FAIL"""
    named = {
        "annotations": "exports/reviewed_source_annotations.csv",
        "status": "exports/field_status.csv",
        "status_summary": "exports/field_status_summary.json",
        "coverage_slots": "exports/coverage_by_slot.csv",
        "coverage_statistics": "exports/coverage_statistics.json",
    }
    root = candidate.shared(env)
    counts = _table_counts(root)
    require_population(counts, "table counts from the candidate database")

    clean = contract.blocking(contract.check_required_outputs(root, counts))
    require(not clean,
            f"the INTACT candidate already violates the contract, so removals below "
            f"would prove nothing: {clean[:3]}")

    detected = {}
    for label, rel in named.items():
        with candidate.mutated(root, rel, "remove"):
            violations = contract.blocking(
                contract.check_required_outputs(root, counts))
            hits = [v for v in violations if v["path"] == rel
                    and v["kind"] == "MISSING_REQUIRED_OUTPUT"]
            require(hits,
                    f"removing the mandatory {label} output {rel} produced no blocking "
                    f"MISSING_REQUIRED_OUTPUT violation. Violations seen: {violations}")
            detected[label] = hits[0]["group"]
        # restored -- and the restoration must actually have happened
        require((root / rel).is_file(), f"{rel} was not restored after the mutation")
    return ("every mandatory group output fails the contract when removed: " +
            ", ".join(f"{k} ({v})" for k, v in detected.items()))


@acceptance(issue="A13.3", group="release",
            mutation="a required output present but truncated to zero bytes")
def t_blank_required_output_fails(env):
    """a present-but-empty required output over a populated table FAILS"""
    root = candidate.shared(env)
    counts = _table_counts(root)
    require(counts.get("observations", 0) > 0,
            "the candidate database has no observations, so an empty export would be "
            "legitimately empty and this mutation could not be presented")

    with candidate.mutated(root, "exports/quarterly_key_metrics.csv", "blank"):
        violations = contract.blocking(contract.check_required_outputs(root, counts))
    hits = [v for v in violations
            if v["path"] == "exports/quarterly_key_metrics.csv"
            and v["kind"] in ("EMPTY_OVER_POPULATED_TABLE", "MISSING_REQUIRED_COLUMNS")]
    require(hits,
            "a zero-byte quarterly_key_metrics.csv over a populated observations table "
            f"produced no blocking violation. An inventory-based manifest is perfectly "
            f"happy with an empty file, which is why the contract has to look inside it. "
            f"Violations: {violations}")
    return (f"zero-byte required output over {counts['observations']:,} observations "
            f"-> {hits[0]['kind']}")


@acceptance(issue="A13.4", group="release",
            mutation="dropping the validation column from a values export")
def t_dropping_a_status_column_fails(env):
    """dropping a status column from a required export FAILS"""
    root = candidate.shared(env)
    counts = _table_counts(root)
    with candidate.mutated(root, "exports/quarterly_key_metrics.csv",
                           "drop_column", column="validation"):
        violations = contract.blocking(contract.check_required_outputs(root, counts))
    hits = [v for v in violations if v["path"] == "exports/quarterly_key_metrics.csv"
            and v["kind"] == "MISSING_REQUIRED_COLUMNS"]
    require(hits,
            "removing the `validation` column from the quarterly table produced no "
            "violation. Rule 3.5 keeps the five status dimensions independent and "
            "Rule 3.6 forbids dropping a warning; the row count is unchanged, so only "
            "a column contract catches this.")
    return f"validation column dropped -> {hits[0]['kind']}: {hits[0]['detail'][:90]}"


@acceptance(issue="A13.5", group="release",
            mutation="an empty database making every emptiness rule vacuously true")
def t_empty_database_is_not_publishable(env):
    """an empty database cannot satisfy the contract vacuously"""
    root = candidate.build_candidate(env, "a13_emptydb", with_database=False)
    # Every NON_EMPTY_IF_TABLE rule is satisfied when the table has zero rows,
    # so without a floor a release of empty outputs passes.
    zeroed = {t: 0 for t in contract.MINIMUM_VIABLE_POPULATION}
    violations = contract.check_required_outputs(root, zeroed)
    hits = [v for v in violations if v["kind"] == "EMPTY_DATABASE_NOT_PUBLISHABLE"]
    require(hits,
            "an all-zero database produced no EMPTY_DATABASE_NOT_PUBLISHABLE violation. "
            "With zero rows expected, nothing can be missing -- the denominator went to "
            "zero and the number improved, which is Rule 3.6 exactly.")
    require(hits[0].get("severity") == contract.Severity.BLOCKING,
            f"the empty-database violation is not blocking: {hits[0]}")
    return f"all-zero table counts -> {hits[0]['kind']} (blocking)"


# ------------------------------------------------------------------ severity API

@acceptance(issue="A13.6", group="release",
            mutation="treating an unverifiable rule as a satisfied one")
def t_unverified_is_not_satisfied(env):
    """a rule that could not be checked is UNVERIFIED, never a pass"""
    root = candidate.shared(env)
    # No table counts: the non-empty-if-table rule cannot be re-derived.
    violations = contract.check_required_outputs(root, None)
    unver = contract.unverified(violations)
    require_population(unver, "unverified violations when no table counts are supplied")
    require(all(v["kind"] == "EMPTINESS_RULE_UNENFORCED" for v in unver),
            f"unexpected unverified kinds: {{v['kind'] for v in unver}}")
    require(not contract.blocking(violations),
            "an intact candidate produced BLOCKING violations when only the emptiness "
            f"rule was unverifiable: {contract.blocking(violations)[:3]}")

    # The evidence a lite receipt must carry, so the claim is re-derivable.
    evidence = contract.enforcement_evidence(root, _table_counts(root))
    require_population(evidence["outputs"], "per-output enforcement evidence")
    for rel, ev in evidence["outputs"].items():
        require(ev["table_rows_at_build"] is not None,
                f"{rel}: enforcement evidence carries no table row count, so a reader "
                "cannot re-derive the verdict and is back to trusting an assertion")
        require(ev["satisfied"] is not False,
                f"{rel}: enforcement evidence says the rule was NOT satisfied "
                f"({ev['rows_written']} rows written over {ev['table_rows_at_build']} "
                "table rows)")
    return (f"{len(unver)} rule(s) unverifiable without the database, none blocking; "
            f"{len(evidence['outputs'])} outputs carry re-derivable enforcement evidence")


# ------------------------------------------------------------------ clean room

@acceptance(issue="A13.7", group="release", tier=Tier.INTEGRATED,
            mutation="a receipt that certifies a rebuild it never performed")
def t_clean_room_tests_products_not_hashes(env):
    """the clean room verifies EXTRACTED outputs, not just archive hashes"""
    require(BUILDER.is_file(), f"build_release.py absent: {BUILDER}")
    root = candidate.build_candidate(env, "a13_cleanroom")
    proc = _run_builder(root)
    require(proc.returncode == 0,
            f"candidate build failed:\n{proc.stdout[-1500:]}\n{proc.stderr[-800:]}")

    receipt = json.loads((root / "release_receipt.json").read_bytes())
    verification = receipt.get("verification") or receipt.get("clean_room") or {}
    blob = json.dumps(receipt)

    require("OFFLINE_OK" not in blob or "contract" in blob.lower(),
            "the receipt still leans on an OFFLINE_OK line as its clean-room evidence; "
            "importing four modules and checking two period assertions rebuilds no data")
    # The extracted tree must have been checked against the contract, not just hashed.
    require(any(k in blob for k in ("contract_violations", "required_outputs",
                                    "unverified_from_this_payload")),
            f"the receipt records no contract check over the EXTRACTED tree: "
            f"{sorted(verification)[:12]}")
    return (f"receipt status {receipt.get('status')!r}; extracted-tree verification keys: "
            f"{sorted(verification)[:8]}")
