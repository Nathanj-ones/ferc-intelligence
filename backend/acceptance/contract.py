"""
The FIXED required-output contract, and the release identity rules.

A13's root cause is that `build_release.py` builds its manifest by walking the
tree: the manifest is an INVENTORY OF WHATEVER HAPPENS TO EXIST. Delete a
required export and the manifest simply has one fewer entry, the clean room
verifies the archive against that shrunken manifest, and the release reports
PASS. A missing required output is silently redefined out of the release.

The fix is a declaration that does not move when the tree does. This module is
that declaration. It is data, not inventory: it is written down here, in advance,
independently of any build, and the builder must be measured AGAINST it.

Two things follow from that and both are load-bearing:

  * A release fails when a declared output is absent. It cannot pass by
    forgetting the output existed.
  * A release also fails when a declared output is present but structurally
    empty where the database says it must have rows -- otherwise "produce a
    zero-byte file" becomes the way to satisfy the contract.

Every size and hash in this module is taken from bytes that were actually read.
On this volume `stat().st_size` returns a stale placeholder size for a
cloud-evicted file until the bytes are read, so a size or hash taken from
`stat()` is unreliable by construction. See `acceptance.harness.sha256_read`.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import json
import pathlib


# ------------------------------------------------------------------ groups

class Group:
    """Reporting groups named by the A13 acceptance test."""

    VALUES = "values"                 # the human-facing metric tables
    COVERAGE = "coverage"             # mandatory coverage outputs
    STATUS = "status"                 # mandatory field-readiness outputs
    ANNOTATIONS = "annotations"       # mandatory reviewed-annotation outputs
    PROVENANCE = "provenance"         # lineage, filings, source manifest
    DOCUMENTS = "documents"           # document layer
    GATES = "gates"                   # blockers and applicability


class Emptiness:
    ALWAYS_NON_EMPTY = "always_non_empty"
    #: may be empty ONLY when the named table is itself empty; an empty file over
    #: a populated table is a silent data loss, not a legitimate empty result
    NON_EMPTY_IF_TABLE = "non_empty_if_table"
    #: legitimately empty in the delivered baseline; emptiness is tracked as an
    #: open gate elsewhere, not laundered into a passing release
    MAY_BE_EMPTY = "may_be_empty"


class Severity:
    """Whether a violation blocks a release, or records something unverifiable.

    These are DIFFERENT facts and collapsing them either way is a defect:

      BLOCKING    the contract was checked and the candidate failed it.
      UNVERIFIED  the contract could NOT be checked from this payload. Not a
                  pass. It must travel with the evidence that the rule WAS
                  enforced somewhere it could be, or it becomes the laundering
                  route the contract exists to close.

    A lite payload legitimately excludes the database, so the
    non-empty-if-table rule cannot be re-derived from the archive alone. That
    makes it UNVERIFIED there, never satisfied.
    """

    BLOCKING = "blocking"
    UNVERIFIED = "unverified"


#: Tables that must carry rows for a candidate to be a publishable release at
#: all. Without this floor an EMPTY database satisfies every
#: `NON_EMPTY_IF_TABLE` rule vacuously -- 0 rows expected, 0 rows found -- and a
#: release of eighteen empty CSVs passes the contract. That is the
#: "never improve a number by weakening its basis" failure in its purest form:
#: the denominator went to zero, so nothing could be missing.
MINIMUM_VIABLE_POPULATION = ("observations", "filings", "coverage_expected")


@dataclasses.dataclass(frozen=True)
class RequiredOutput:
    path: str
    group: str
    produced_by: str
    generator: str
    emptiness: str
    backing_table: str = ""
    required_columns: tuple[str, ...] = ()
    required_json_keys: tuple[str, ...] = ()
    rationale: str = ""


#: THE CONTRACT. Fixed, declared in advance, independent of any build.
#: Adding an output here is a deliberate act. A build may produce MORE than this;
#: it may never produce less.
REQUIRED_OUTPUTS: tuple[RequiredOutput, ...] = (
    # ---------------------------------------------------------- values
    RequiredOutput(
        "exports/quarterly_key_metrics.csv", Group.VALUES,
        "python3 run.py export", "exporters.write_all",
        Emptiness.NON_EMPTY_IF_TABLE, "observations",
        required_columns=("entity_key", "metric_id", "reporting_year",
                          "reporting_period", "period_basis", "value", "unit", "scope",
                          "availability", "origin", "method", "version_status",
                          "validation", "qa_flags", "review_status", "missing_reason",
                          "filing_id", "source_fact_id"),
        rationale="The named A13 artefact. It is the human-facing quarterly table; "
                  "a release without it has no quarterly values at all."),
    RequiredOutput(
        "exports/annual_key_metrics.csv", Group.VALUES,
        "python3 run.py export", "exporters.write_all",
        Emptiness.NON_EMPTY_IF_TABLE, "observations",
        required_columns=("entity_key", "metric_id", "reporting_year", "period_basis",
                          "value", "unit", "scope", "availability", "origin", "method",
                          "version_status", "validation", "qa_flags", "review_status",
                          "missing_reason", "filing_id", "source_fact_id"),
        rationale="The annual counterpart; same argument."),
    RequiredOutput(
        "exports/canonical_observations.csv", Group.VALUES,
        "python3 run.py export", "exporters.write_all",
        Emptiness.NON_EMPTY_IF_TABLE, "observations",
        required_columns=("observation_id", "entity_key", "metric_id", "source_regime",
                          "period_basis", "scope", "unit", "value", "availability",
                          "origin", "method", "version_status", "validation",
                          "qa_flags", "review_status", "missing_reason",
                          "source_system", "filing_id", "source_fact_id"),
        rationale="The canonical layer every other table is a view of. The five "
                  "status dimensions are required columns so none can be dropped."),

    # ---------------------------------------------------------- coverage
    RequiredOutput(
        "exports/coverage_by_slot.csv", Group.COVERAGE,
        "python3 run.py coverage", "ferclib.coverage.write_csv",
        Emptiness.NON_EMPTY_IF_TABLE, "coverage_measured",
        required_columns=("slot_id", "entity_key", "metric_id", "requirement",
                          "outcome", "populated", "source_matched", "validated"),
        rationale="The per-slot coverage record. Without it a coverage percentage "
                  "cannot be audited back to the frozen denominator."),
    RequiredOutput(
        "exports/coverage_by_entity.csv", Group.COVERAGE,
        "python3 run.py export", "exporters.write_all",
        Emptiness.NON_EMPTY_IF_TABLE, "observations",
        required_columns=("entity_key", "observations", "populated", "validated"),
        rationale="Mandatory coverage output named by the A13 acceptance test."),
    RequiredOutput(
        "exports/coverage_by_metric.csv", Group.COVERAGE,
        "python3 run.py export", "exporters.write_all",
        Emptiness.NON_EMPTY_IF_TABLE, "observations",
        required_columns=("metric_id", "source_regime", "observations",
                          "populated", "validated"),
        rationale="Mandatory coverage output named by the A13 acceptance test."),
    RequiredOutput(
        "exports/coverage_statistics.json", Group.COVERAGE,
        "python3 run.py coverage", "ferclib.coverage.summarise",
        Emptiness.ALWAYS_NON_EMPTY,
        required_json_keys=("core_populated", "core_validated", "core_outcomes",
                            "all_populated", "all_validated", "all_outcomes"),
        rationale="The headline coverage numbers. A release that ships values but "
                  "not the statistics that qualify them is the A13 failure shape."),

    # ---------------------------------------------------------- status
    RequiredOutput(
        "exports/field_status.csv", Group.STATUS,
        "python3 build_field_status.py", "build_field_status.main",
        Emptiness.ALWAYS_NON_EMPTY,
        required_columns=("template", "field_id", "metric_id", "adapter",
                          "implementation", "outcome", "evidence", "blocker"),
        rationale="Mandatory status output named by the A13 acceptance test. It is "
                  "the record of which requested fields are actually finished."),
    RequiredOutput(
        "exports/field_status_summary.json", Group.STATUS,
        "python3 build_field_status.py", "build_field_status.main",
        Emptiness.ALWAYS_NON_EMPTY,
        required_json_keys=("field_rows", "distinct_metrics", "by_outcome", "by_template"),
        rationale="Mandatory status output named by the A13 acceptance test."),

    # ---------------------------------------------------------- annotations
    RequiredOutput(
        "exports/reviewed_source_annotations.csv", Group.ANNOTATIONS,
        "python3 run.py export", "exporters.write_all",
        Emptiness.NON_EMPTY_IF_TABLE, "reviewed_source_annotations",
        required_columns=("source_system", "entity_key", "filing_id", "source_fact_id",
                          "metric_id", "filed_text", "review_status", "rationale",
                          "evidence_ref", "applied_count"),
        rationale="Mandatory annotations output named by the A13 acceptance test. "
                  "A09 is precisely the case where reviewed flags vanish on a clean "
                  "rebuild; shipping without this file makes that undetectable."),

    # ---------------------------------------------------------- provenance
    RequiredOutput(
        "exports/lineage_edges.csv", Group.PROVENANCE,
        "python3 run.py export", "exporters.write_all",
        Emptiness.NON_EMPTY_IF_TABLE, "lineage_edges",
        required_columns=("observation_id", "input_order", "input_role",
                          "input_source_system", "input_filing_id",
                          "input_source_fact_id", "input_observation_id"),
        rationale="Derived values are only defensible with their inputs. The filing "
                  "occurrence columns are required so A12's wrong-occurrence "
                  "mutation stays visible in the shipped artefact."),
    RequiredOutput(
        "exports/filing_inventory.csv", Group.PROVENANCE,
        "python3 run.py export", "exporters.write_all",
        Emptiness.NON_EMPTY_IF_TABLE, "filings",
        required_columns=("source_system", "filing_id", "entity_key", "form",
                          "content_hash", "version_status", "is_canonical"),
        rationale="Submission identity. Rule 3.2: shared content hashes deduplicate "
                  "bytes, never submission identity, so both columns must ship."),
    RequiredOutput(
        "exports/source_manifest.csv", Group.PROVENANCE,
        "python3 run.py export", "exporters.write_all",
        Emptiness.ALWAYS_NON_EMPTY,
        required_columns=("cache_key", "source_system", "source_url", "byte_size"),
        rationale="The reassembly contract: the release note tells a reader to "
                  "refetch and verify against this file. Without it the documented "
                  "reassembly command cannot be executed."),

    # ---------------------------------------------------------- documents
    RequiredOutput(
        "exports/documents.csv", Group.DOCUMENTS,
        "python3 run.py export", "exporters.write_all",
        Emptiness.NON_EMPTY_IF_TABLE, "documents",
        required_columns=("document_id", "source_system", "filing_id", "media_type",
                          "text_layer", "availability"),
        rationale="text_layer and availability are the columns that keep an "
                  "image-only PDF from being reported as a parse success (A17)."),
    RequiredOutput(
        "exports/document_facts.csv", Group.DOCUMENTS,
        "python3 run.py export", "exporters.write_all",
        Emptiness.NON_EMPTY_IF_TABLE, "document_facts",
        required_columns=("document_fact_id", "document_id", "entity_key",
                          "assertion_type", "verbatim_span", "extraction_method"),
        rationale="A document assertion without its verbatim span is unreviewable."),
    RequiredOutput(
        "exports/events.csv", Group.DOCUMENTS,
        "python3 run.py export", "exporters.write_all",
        Emptiness.NON_EMPTY_IF_TABLE, "events",
        required_columns=("event_id", "entity_key", "event_class", "event_type",
                          "headline", "destination", "is_backfill",
                          "comparison_basis", "confidence_note"),
        rationale="is_backfill is what keeps a historical seed out of the current "
                  "investor feed (A11); it must ship with the events."),

    # ---------------------------------------------------------- gates
    RequiredOutput(
        "exports/blockers.csv", Group.GATES,
        "python3 run.py export", "exporters.write_all",
        Emptiness.NON_EMPTY_IF_TABLE, "blockers",
        required_columns=("blocker_id", "adapter", "kind", "summary",
                          "exact_error", "human_decision_needed"),
        rationale="A correctly gated outcome is a complete outcome, but only if the "
                  "gate ships with the data."),
    RequiredOutput(
        "exports/applicability.csv", Group.GATES,
        "python3 run.py universe", "ferclib.applicability.ApplicabilityBuilder.rows",
        Emptiness.MAY_BE_EMPTY, "applicability",
        rationale="Empty in the delivered baseline because the taxonomy resolution "
                  "is an open gate. It must still SHIP, so the emptiness is visible "
                  "rather than absent -- an absent file reads as 'not part of this "
                  "release', an empty one reads as 'nothing resolved yet'."),
)

REQUIRED_BY_PATH = {r.path: r for r in REQUIRED_OUTPUTS}

#: groups the A13 acceptance test requires to fail the release when absent
MANDATORY_GROUPS = (Group.VALUES, Group.COVERAGE, Group.STATUS,
                    Group.ANNOTATIONS, Group.PROVENANCE)


# ------------------------------------------------------------------ checking

def _csv_header_and_rows(data: bytes) -> tuple[list[str], int]:
    controls = sorted({value for value in data
                       if value < 0x20 and value not in (0x09, 0x0A, 0x0D)})
    if controls:
        rendered = ", ".join(f"U+{value:04X}" for value in controls)
        raise ValueError(f"CSV contains binary C0 control character(s): {rendered}")
    text = data.decode("utf-8", errors="replace")
    if not text.strip():
        return [], 0
    rdr = csv.reader(io.StringIO(text))
    try:
        header = next(rdr)
    except StopIteration:
        return [], 0
    return header, sum(1 for _ in rdr)


def check_required_outputs(root: pathlib.Path,
                           table_counts: dict[str, int] | None = None) -> list[dict]:
    """Measure a candidate release tree against the fixed contract.

    Returns a list of VIOLATIONS. An empty list means the contract is satisfied.
    `table_counts` (optional) enables the NON_EMPTY_IF_TABLE rule; without it
    that rule is reported as unenforced rather than silently treated as passing.

    Sizes come from the bytes read, never from stat().
    """
    violations: list[dict] = []

    # The population floor, checked before anything else. An empty database
    # satisfies every non-empty-if-table rule vacuously, so without this a
    # release of empty files passes the contract.
    if table_counts is not None:
        starved = [t for t in MINIMUM_VIABLE_POPULATION if table_counts.get(t, 0) == 0]
        if starved:
            violations.append({
                "path": "(database)", "group": Group.VALUES,
                "kind": "EMPTY_DATABASE_NOT_PUBLISHABLE",
                "severity": Severity.BLOCKING,
                "detail": f"tables {starved} are empty, so every non-empty-if-table rule "
                          "would be satisfied vacuously and a release of empty outputs "
                          "would pass. A release is not publishable from an empty database."})

    for spec in REQUIRED_OUTPUTS:
        path = root / spec.path
        if not path.is_file():
            violations.append({
                "path": spec.path, "group": spec.group, "kind": "MISSING_REQUIRED_OUTPUT",
                "severity": Severity.BLOCKING,
                "detail": f"declared required output absent; produced by "
                          f"`{spec.produced_by}` ({spec.generator})",
                "rationale": spec.rationale})
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            violations.append({
                "path": spec.path, "group": spec.group, "kind": "UNREADABLE_REQUIRED_OUTPUT",
                "severity": Severity.BLOCKING,
                "detail": f"{type(exc).__name__}: {exc}. An unreadable file is never "
                          "an unchanged one."})
            continue

        if spec.path.endswith(".json"):
            if not data.strip():
                violations.append({"path": spec.path, "group": spec.group,
                                   "kind": "EMPTY_REQUIRED_OUTPUT",
                                   "severity": Severity.BLOCKING,
                                   "detail": "declared JSON output is zero-length"})
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError as exc:
                violations.append({"path": spec.path, "group": spec.group,
                                   "kind": "MALFORMED_REQUIRED_OUTPUT",
                                   "severity": Severity.BLOCKING,
                                   "detail": f"invalid JSON: {exc}"})
                continue
            missing = [k for k in spec.required_json_keys if k not in obj]
            if missing:
                violations.append({"path": spec.path, "group": spec.group,
                                   "kind": "MISSING_REQUIRED_KEYS",
                                   "severity": Severity.BLOCKING,
                                   "detail": f"required keys absent: {missing}"})
            continue

        try:
            header, nrows = _csv_header_and_rows(data)
        except (csv.Error, ValueError) as exc:
            violations.append({
                "path": spec.path, "group": spec.group,
                "kind": "MALFORMED_REQUIRED_OUTPUT",
                "severity": Severity.BLOCKING,
                "detail": f"invalid portable CSV: {exc}"})
            continue
        missing_cols = [c for c in spec.required_columns if c not in header]
        if missing_cols:
            violations.append({"path": spec.path, "group": spec.group,
                               "kind": "MISSING_REQUIRED_COLUMNS",
                               "severity": Severity.BLOCKING,
                               "detail": f"required columns absent: {missing_cols}",
                               "rationale": spec.rationale})
        if spec.emptiness == Emptiness.ALWAYS_NON_EMPTY and nrows == 0:
            violations.append({"path": spec.path, "group": spec.group,
                               "kind": "EMPTY_REQUIRED_OUTPUT",
                               "severity": Severity.BLOCKING,
                               "detail": "declared always-non-empty output has no data rows"})
        elif spec.emptiness == Emptiness.NON_EMPTY_IF_TABLE:
            if table_counts is None:
                violations.append({
                    "path": spec.path, "group": spec.group,
                    "kind": "EMPTINESS_RULE_UNENFORCED",
                    "severity": Severity.UNVERIFIED,
                    "backing_table": spec.backing_table,
                    "observed_rows": nrows,
                    "detail": "no table counts supplied, so the non-empty-if-table rule "
                              "could not be re-derived from this payload. UNVERIFIED is "
                              "not satisfied: it must travel with evidence that the rule "
                              "was enforced against the real database at build time "
                              "(see `enforcement_evidence`)."})
            elif table_counts.get(spec.backing_table, 0) > 0 and nrows == 0:
                violations.append({
                    "path": spec.path, "group": spec.group,
                    "kind": "EMPTY_OVER_POPULATED_TABLE",
                    "severity": Severity.BLOCKING,
                    "detail": f"file has 0 data rows but `{spec.backing_table}` holds "
                              f"{table_counts[spec.backing_table]:,} rows -- silent data loss"})
    return violations


def blocking(violations: list[dict]) -> list[dict]:
    """Only the violations that must stop a release."""
    return [v for v in violations
            if v.get("severity", Severity.BLOCKING) == Severity.BLOCKING]


def unverified(violations: list[dict]) -> list[dict]:
    """Violations that could not be checked from this payload. Never passes."""
    return [v for v in violations if v.get("severity") == Severity.UNVERIFIED]


def enforcement_evidence(root: pathlib.Path, table_counts: dict[str, int]) -> dict:
    """The FACTS a lite receipt must carry about rules it cannot re-verify.

    A lite payload excludes the database by declaration, so a verifier working
    from the archive alone cannot re-derive the non-empty-if-table rule. The
    honest way to close that is not a sentence in the receipt saying the rule was
    enforced -- a receipt that certifies its own correctness is the A21 cycle in
    miniature. It is to record, at build time, the numbers the rule was decided
    on, so an independent reader re-derives the verdict instead of trusting it:

        for each output, the backing table, its row count in the real database,
        and the data rows actually written to the file.

    Anyone can then recompute `rows_written > 0 whenever table_rows > 0` from the
    receipt without the database, and can challenge it against the full payload.
    """
    evidence = {}
    for spec in REQUIRED_OUTPUTS:
        if spec.emptiness != Emptiness.NON_EMPTY_IF_TABLE:
            continue
        path = root / spec.path
        rows_written = None
        if path.is_file() and not spec.path.endswith(".json"):
            _, rows_written = _csv_header_and_rows(path.read_bytes())
        table_rows = table_counts.get(spec.backing_table)
        evidence[spec.path] = {
            "backing_table": spec.backing_table,
            "table_rows_at_build": table_rows,
            "rows_written": rows_written,
            "rule": "rows_written > 0 whenever table_rows > 0",
            "satisfied": (None if table_rows is None or rows_written is None
                          else bool(rows_written > 0 or table_rows == 0)),
        }
    return {
        "rule": "non_empty_if_table",
        "enforced_against": "the real staging database at build stage 1",
        "note": ("Recorded so a reader of a lite payload can RE-DERIVE this verdict "
                 "from the numbers rather than accept an assertion. The full payload "
                 "carries the database and re-verifies it directly."),
        "outputs": evidence,
    }


# ------------------------------------------------------------------ A21

#: The release identity sequence, in order. Each stage may only reference bytes
#: frozen by an EARLIER stage. That is what makes the scheme acyclic.
RELEASE_STAGES = (
    ("freeze_payload", "every generated artefact exists and will not change again"),
    ("hash_payload", "hash each payload file FROM ITS BYTES; write the manifest"),
    ("create_archive", "build the archive from exactly the manifested payload"),
    ("issue_receipt", "write an EXTERNAL receipt naming the archive's hash"),
)

#: Files that are written after the manifest and therefore may never appear as
#: manifest entries: a manifest entry for one of these is stale the moment it
#: lands, which is exactly A21's three stale entries.
WRITTEN_AFTER_MANIFEST = (
    "artifact_manifest.json",     # cannot hash itself
    "release_receipt.json",       # written after the archive it certifies
)

#: Path suffixes whose content is not frozen at manifest time.
MUTABLE_AFTER_MANIFEST_PREFIXES = ("staging/",)


def manifest_cycle_violations(manifest: dict, *, archive_members: set[str] | None = None,
                              tree: pathlib.Path | None = None) -> list[dict]:
    """Check a manifest/receipt scheme is acyclic and coherent.

    Three defects, all present in the delivered artefact set:

      1. a manifest entry for a file written AFTER the manifest (stale by
         construction -- the acceptance doc, the receipt, the database);
      2. a receipt that hashes an archive which contains that same receipt
         (a self-hashing cycle: the receipt can never state its own truth);
      3. a manifest entry whose recorded size did not come from reading the
         bytes (unverifiable on a volume with cloud-evicted placeholders).
    """
    out: list[dict] = []
    files = manifest.get("files", {})
    for rel in files:
        if rel in WRITTEN_AFTER_MANIFEST:
            out.append({"kind": "STALE_BY_CONSTRUCTION", "path": rel,
                        "detail": "manifest records a file that is written after the "
                                  "manifest; the entry is stale the moment it lands"})
        if any(rel.startswith(p) for p in MUTABLE_AFTER_MANIFEST_PREFIXES):
            out.append({"kind": "MUTABLE_AFTER_MANIFEST", "path": rel,
                        "detail": "manifest records a file whose content is not frozen "
                                  "at manifest time"})
    if archive_members is not None:
        for rel in WRITTEN_AFTER_MANIFEST:
            if rel == "release_receipt.json" and rel in archive_members:
                out.append({"kind": "RECEIPT_INSIDE_ARCHIVE_CYCLE", "path": rel,
                            "detail": "the receipt certifying the archive is itself "
                                      "inside that archive; the hash it states can "
                                      "never be the hash of the bytes it ships in"})
    if tree is not None:
        for rel, meta in files.items():
            if meta.get("hash_source") == "content_addressed_filename":
                out.append({"kind": "SIZE_NOT_FROM_BYTES", "path": rel,
                            "detail": "hash taken from the filename and size from "
                                      "stat(); on this volume stat() returns a stale "
                                      "placeholder for an evicted file, so neither "
                                      "was verified against the bytes"})
    return out
