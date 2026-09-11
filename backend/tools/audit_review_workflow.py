#!/usr/bin/env python3
"""Audit and normalize review-workflow semantics without changing source data.

The consumer-facing ``review`` count is a quality-flag count, not a count of
human review tasks.  This utility keeps those concepts separate and emits a
generation-bound audit record.  It opens the database read-only, never changes
filed values or statuses, and treats an explicit occurrence-bound final
annotation as the only mechanically closed observation review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import tempfile
from collections import Counter
from typing import Any, Iterable, Mapping


SCHEMA = "ferc_review_workflow_audit_v1"
MUST_PROPAGATE = frozenset({
    "source_anomaly_review",
    "blocked_ambiguity",
    "scope_incompatible",
    "unit_warning",
    "rounding_warning",
    "source_date_warning",
})
ALLOWED_REVIEW_STATUSES = frozenset({"", "open", "resolved"})
ANNOTATION_STATUSES = frozenset({
    "reviewed",
    "reviewed_open",
    "reviewed_final",
    "reviewed_resolved",
})
RESOLVED_ANNOTATION_STATUSES = frozenset({"reviewed_final", "reviewed_resolved"})
PRESENTATION_CLASSES = {
    "blocked_ambiguity": "interpretation_blocked",
    "scope_incompatible": "comparison_limited",
    "unit_warning": "unit_qualified",
    "rounding_warning": "reconciliation_qualified",
    "source_date_warning": "date_qualified",
    "source_anomaly_review": "anomaly_flagged",
}


def annotation_evidence_hash(row: Mapping[str, Any]) -> str:
    """Hash the exact occurrence identity using the annotation-bundle contract."""
    identity = {key: row.get(key) for key in (
        "entity_key",
        "filed_text",
        "filing_id",
        "metric_id",
        "source_fact_id",
        "source_system",
    )}
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def classify_observation(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return terminology safe for consumers; make no resolution decision."""
    validation = str(row.get("validation") or "")
    review_status = str(row.get("review_status") or "")
    return {
        "presentation_class": PRESENTATION_CLASSES.get(validation, "not_flagged"),
        "workflow_state": review_status or "none",
        "is_quality_flag": validation in MUST_PROPAGATE,
        "is_review_task": review_status == "open",
        "is_resolved_review": review_status == "resolved",
    }


def _rows(connection: sqlite3.Connection, sql: str,
          parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, parameters)]


def _duplicates(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"\s*(?:\|\||;)\s*", text or "")
             if part.strip()]
    counts = Counter(parts)
    return sorted(part for part, count in counts.items() if count > 1)


def _latest_publication(connection: sqlite3.Connection) -> dict[str, Any] | None:
    try:
        rows = _rows(connection, """
            SELECT generation_id,code_snapshot,input_snapshot,database_identity,
                   status,published_at
            FROM publication_generations
            WHERE status='published'
            ORDER BY published_at DESC,generation_id DESC LIMIT 1""")
    except sqlite3.OperationalError:
        return None
    return rows[0] if rows else None


def audit_connection(connection: sqlite3.Connection, *, database_path: str = "") -> dict:
    connection.row_factory = sqlite3.Row
    quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    observations = _rows(connection, """
        SELECT observation_id,source_system,entity_key,filing_id,source_fact_id,
               metric_id,value_text,availability,validation,review_status,qa_flags
        FROM observations
        WHERE availability='present' AND validation IN
          ('source_anomaly_review','blocked_ambiguity','scope_incompatible',
           'unit_warning','rounding_warning','source_date_warning')
        ORDER BY observation_id""")
    workflow_observations = _rows(connection, """
        SELECT observation_id,availability,validation,review_status
        FROM observations WHERE TRIM(COALESCE(review_status,''))<>''
        ORDER BY observation_id""")
    annotations = _rows(connection, """
        SELECT source_system,entity_key,filing_id,source_fact_id,metric_id,
               filed_text,review_status,rationale,evidence_ref,evidence_hash,
               reviewer,reviewed_at,applied_count
        FROM reviewed_source_annotations
        ORDER BY source_system,entity_key,filing_id,source_fact_id,metric_id""")
    document_states = _rows(connection, """
        SELECT COALESCE(NULLIF(review_state,''),'none') AS review_state,
               COALESCE(NULLIF(confidence,''),'none') AS confidence,COUNT(*) AS records
        FROM document_facts GROUP BY 1,2 ORDER BY 1,2""")
    blocker_states = _rows(connection, """
        SELECT kind,human_decision_needed,(resolved_at IS NULL) AS is_open,
               COUNT(*) AS records
        FROM blockers GROUP BY kind,human_decision_needed,(resolved_at IS NULL)
        ORDER BY kind,human_decision_needed,is_open""")
    review_queue_events = _rows(connection, """
        SELECT event_class,event_type,COUNT(*) AS records
        FROM events WHERE destination='data_review_queue'
        GROUP BY event_class,event_type ORDER BY event_class,event_type""")

    problems: list[dict[str, Any]] = []
    by_validation = Counter()
    by_workflow = Counter()
    by_presentation = Counter()
    duplicate_warning_records = []
    for row in observations:
        classification = classify_observation(row)
        by_validation[str(row["validation"])] += 1
        by_workflow[classification["workflow_state"]] += 1
        by_presentation[classification["presentation_class"]] += 1
        duplicates = _duplicates(str(row.get("qa_flags") or ""))
        if duplicates:
            duplicate_warning_records.append({
                "observation_id": row["observation_id"],
                "duplicate_fragments": duplicates,
            })

    annotation_applications = 0
    mechanically_closed = 0
    for annotation in annotations:
        key = {name: annotation.get(name) for name in (
            "source_system", "entity_key", "filing_id", "source_fact_id", "metric_id")}
        status = str(annotation.get("review_status") or "")
        if status not in ANNOTATION_STATUSES:
            problems.append({"code": "unknown_annotation_status", **key,
                             "review_status": status})
        expected_hash = annotation_evidence_hash(annotation)
        if str(annotation.get("evidence_hash") or "") != expected_hash:
            problems.append({"code": "annotation_evidence_hash_mismatch", **key,
                             "expected": expected_hash,
                             "actual": annotation.get("evidence_hash")})
        matches = _rows(connection, """
            SELECT observation_id,validation,review_status FROM observations
            WHERE source_system=? AND entity_key=? AND filing_id=?
              AND source_fact_id=? AND metric_id=? AND value_text=?
            ORDER BY observation_id""", (
                annotation["source_system"], annotation["entity_key"],
                annotation["filing_id"], annotation["source_fact_id"],
                annotation["metric_id"], annotation["filed_text"],
            ))
        annotation_applications += len(matches)
        if int(annotation.get("applied_count") or 0) != len(matches):
            problems.append({
                "code": "annotation_application_count_mismatch",
                **key,
                "stored": int(annotation.get("applied_count") or 0),
                "observed": len(matches),
            })
        expected_workflow = (
            "resolved" if status in RESOLVED_ANNOTATION_STATUSES else "open")
        for match in matches:
            if match["validation"] != "source_anomaly_review" \
                    or str(match.get("review_status") or "") != expected_workflow:
                problems.append({
                    "code": "annotation_observation_state_mismatch",
                    **key,
                    "observation_id": match["observation_id"],
                    "expected_validation": "source_anomaly_review",
                    "expected_review_status": expected_workflow,
                    "actual_validation": match["validation"],
                    "actual_review_status": match.get("review_status") or "",
                })
            elif expected_workflow == "resolved":
                mechanically_closed += 1

    workflow_by_status = Counter(
        str(row.get("review_status") or "none") for row in workflow_observations)
    workflow_by_validation = Counter(
        str(row.get("validation") or "none") for row in workflow_observations)
    workflow_by_availability = Counter(
        str(row.get("availability") or "none") for row in workflow_observations)
    for row in workflow_observations:
        status = str(row.get("review_status") or "")
        validation = str(row.get("validation") or "")
        if status not in ALLOWED_REVIEW_STATUSES:
            problems.append({
                "code": "unknown_observation_review_status",
                "observation_id": row["observation_id"],
                "review_status": status,
            })
        if validation not in MUST_PROPAGATE:
            problems.append({
                "code": "review_status_on_non_flagged_observation",
                "observation_id": row["observation_id"],
                "review_status": status,
                "validation": validation,
            })
    resolved_without_final_annotation = max(
        0, workflow_by_status["resolved"] - mechanically_closed)
    if resolved_without_final_annotation:
        problems.append({
            "code": "resolved_observation_without_exact_final_annotation",
            "records": resolved_without_final_annotation,
        })

    total = len(observations)
    open_reviews = by_workflow["open"]
    resolved_reviews = by_workflow["resolved"]
    non_workflow_flags = by_workflow["none"]
    report = {
        "schema": SCHEMA,
        "database": {
            "path": database_path,
            "quick_check": quick_check,
            "publication": _latest_publication(connection),
        },
        "definitions": {
            "quality_flag": (
                "A present observation carrying a must-propagate validation; it is not "
                "necessarily a human review task."),
            "review_task": "An observation whose independent review_status is open.",
            "resolved_review": (
                "An occurrence-bound review explicitly resolved by final evidence; the "
                "underlying warning remains attached."),
        },
        "observation_quality_flags": {
            "records": total,
            "by_validation": dict(sorted(by_validation.items())),
            "by_workflow_state": dict(sorted(by_workflow.items())),
            "by_presentation_class": dict(sorted(by_presentation.items())),
            "open_review_tasks": open_reviews,
            "resolved_review_tasks": resolved_reviews,
            "flags_without_workflow_state": non_workflow_flags,
            "duplicate_warning_records": len(duplicate_warning_records),
            "duplicate_warning_samples": duplicate_warning_records[:20],
        },
        "reviewed_source_annotations": {
            "records": len(annotations),
            "exact_applications": annotation_applications,
            "mechanically_closed_observations": mechanically_closed,
        },
        "observation_review_workflow": {
            "records": len(workflow_observations),
            "by_status": dict(sorted(workflow_by_status.items())),
            "by_validation": dict(sorted(workflow_by_validation.items())),
            "by_availability": dict(sorted(workflow_by_availability.items())),
        },
        "document_fact_review": document_states,
        "data_review_queue_events": review_queue_events,
        "blocker_lifecycle": blocker_states,
        "policy": {
            "mechanical_close": (
                "Only an exact source occurrence whose bundled annotation is explicitly "
                "reviewed_final or reviewed_resolved. Successful adapters may separately "
                "close only their own exact blockers."),
            "relabel_only": {
                "scope_incompatible": "comparison-limited observation",
                "unit_warning": "unit-qualified observation",
                "rounding_warning": "reconciliation-qualified observation",
                "source_date_warning": "date-qualified observation",
                "source_anomaly_review_without_open_status": "anomaly-flagged observation",
            },
            "never_auto_close": [
                "open source anomalies",
                "blocked interpretations",
                "unresolved units",
                "queued document assertions",
                "human-decision blockers",
            ],
            "safe_presentation_normalization": (
                "Deduplicate repeated warning fragments when rendering, while preserving "
                "the stored qa_flags and every validation state."),
        },
        "integrity": {
            "status": "pass" if quick_check == "ok" and not problems else "fail",
            "problems": problems,
        },
    }
    return report


def audit_database(database: pathlib.Path) -> dict:
    if not database.is_file():
        raise ValueError(f"database does not exist: {database}")
    uri = f"file:{database.resolve().as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return audit_connection(connection, database_path=str(database.resolve()))
    finally:
        connection.close()


def _write_atomic(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
           + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args(argv)
    report = audit_database(args.database)
    if args.output:
        _write_atomic(args.output, report)
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["integrity"]["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
