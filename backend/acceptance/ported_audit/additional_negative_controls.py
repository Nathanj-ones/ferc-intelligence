"""
Ported from the auditor's `evidence/scripts/additional_negative_controls.py`.

Only the sandbox paths changed (see `paths.py`) and two function names that the
A12 repair renamed. The assertions, the fixtures and the expected outcomes are
the auditor's. Their original verdicts, from
`evidence/reports/additional_negative_controls.json`, were:

    wrong real identical-submission lineage: pointer resolver ......... PASS  (should reject)
    wrong real identical-submission lineage: delivered negative test .. PASS  (should reject)
    two identical real submissions: delivered resubmission test ....... SKIP  (should exercise)
    reference nonzero value missing from produced output .............. PASS  (should FAIL)
    remove warning text but preserve warning status ................... REJECTED AS REQUIRED
    remove warning text and review status together .................... PASS  (cannot identify)

Re-running them against the REPAIRED code is the check: every verdict that was
wrong must now be different, and the one that was already right must stay right.

    python3 -m acceptance.ported_audit.additional_negative_controls
"""

from __future__ import annotations

import copy
import json
import sqlite3
import sys
import types

from . import paths

sys.path.insert(0, str(paths.CODE))


def main() -> int:
    paths.prepare()
    import exporters
    import run_regressions
    import validate

    con = sqlite3.connect(paths.audited_db_uri(), uri=True)
    con.row_factory = sqlite3.Row
    results: list[dict] = []

    # ---- the auditor's identity fixture, built in a disposable subset database
    edge = con.execute(
        "select * from lineage_edges where input_filing_id='74835' "
        "and input_observation_id is not null limit 1").fetchone()
    if edge is None:
        print("FIXTURE ABSENT: no lineage edge on filing 74835")
        return 2
    e = dict(edge)

    db_path = paths.WORK / "identity.sqlite"
    if db_path.exists():
        db_path = paths.WORK / f"identity_{len(list(paths.WORK.glob('*.sqlite')))}.sqlite"
    sub = sqlite3.connect(db_path)
    sub.row_factory = sqlite3.Row
    sub.executescript((paths.CODE / "ferclib" / "schema.sql").read_text(encoding="utf-8"))
    sub.execute("PRAGMA foreign_keys=OFF")

    def insert(table: str, row: dict) -> None:
        cols = ",".join(row)
        sub.execute(f"insert or replace into {table} ({cols}) "
                    f"values ({','.join('?' * len(row))})", list(row.values()))

    for fid in ("74834", "74835"):
        insert("filings", dict(con.execute(
            "select * from filings where source_system='eCollection_XBRL' and filing_id=?",
            (fid,)).fetchone()))
        insert("source_facts", dict(con.execute(
            "select * from source_facts where source_system='eCollection_XBRL' "
            "and filing_id=? and source_fact_id=?",
            (fid, e["input_source_fact_id"])).fetchone()))
    for ident in (e["observation_id"], e["input_observation_id"]):
        insert("observations", dict(con.execute(
            "select * from observations where observation_id=?", (ident,)).fetchone()))

    # THE AUDITOR'S MUTATION, unchanged: point the edge at the byte-identical
    # sibling occurrence while its input observation stays on the original.
    e["input_filing_id"] = "74834"
    insert("lineage_edges", e)
    sub.commit()

    class _Staging:
        def __init__(self, connection):
            self.con = connection
            self.path = db_path

        def query(self, sql, params=()):
            return self.con.execute(sql, params).fetchall()

    ctx = types.SimpleNamespace(staging=_Staging(sub))

    # The delivered `t_wrong_version_join` was replaced by
    # `t_lineage_occurrence_coherent`, which is the same question asked of real
    # linked records instead of a literal that could never resolve. Both names are
    # driven where they exist, so the port never quietly skips a control.
    probes = [("wrong real identical-submission lineage: pointer resolver",
               "t_lineage_resolves", "reject wrong occurrence"),
              ("wrong real identical-submission lineage: delivered negative-version test",
               "t_wrong_version_join", "reject wrong occurrence"),
              ("wrong real identical-submission lineage: occurrence coherence",
               "t_lineage_occurrence_coherent", "reject wrong occurrence"),
              ("two identical real submissions: delivered resubmission test",
               "t_identical_resubmission", "exercise identical submission, not skip")]
    for name, attr, expected in probes:
        fn = getattr(validate, attr, None)
        if fn is None:
            results.append({"test": name, "synthetic": True, "actual": "ABSENT",
                            "detail": f"{attr} no longer exists in validate.py",
                            "expected": expected})
            continue
        try:
            msg = fn(ctx)
            outcome = "SKIP" if str(msg).startswith("SKIP") else "PASS"
            results.append({"test": name, "synthetic": True, "actual": outcome,
                            "detail": str(msg), "expected": expected})
        except AssertionError as exc:
            results.append({"test": name, "synthetic": True, "actual": "REJECTED",
                            "detail": str(exc)[:400], "expected": expected})
        except Exception as exc:                                        # noqa: BLE001
            results.append({"test": name, "synthetic": True, "actual": "ERROR",
                            "detail": repr(exc)[:400], "expected": expected})

    results.append({
        "test": "independent mismatch check", "synthetic": True,
        "edge_filing": e["input_filing_id"],
        "referenced_input_observation_filing": dict(con.execute(
            "select * from observations where observation_id=?",
            (e["input_observation_id"],)).fetchone())["filing_id"],
        "expected_occurrence": "74835",
        "same_bytes_do_not_make_same_occurrence": True})
    sub.close()

    # ---- the auditor's flag-stripping controls, on a real flagged row
    row = con.execute(
        "select * from observations where entity_key='C001012' and value_text='999999' "
        "and validation='source_anomaly_review' limit 1").fetchone()
    if row is not None:
        row = dict(row)
        row["value"] = row["value_text"]
        row.update(qa_flags="", review_status="", missing_reason="")
        try:
            exporters._check_flags_survive([row], "AUDIT_FLAG_ONLY_STRIPPED")
            out = "FAILED TO REJECT"
        except exporters.FlagStripped:
            out = "REJECTED AS REQUIRED"
        results.append({"test": "remove warning text but preserve warning status",
                        "synthetic": True, "actual": out})

        both = dict(row, validation="pass")
        try:
            exporters._check_flags_survive([both], "AUDIT_WARNING_AND_STATUS_LOST")
            out2 = "PASS (cannot identify lost upstream annotation)"
        except exporters.FlagStripped:
            out2 = "REJECTED"
        # The auditor recorded PASS here and named its real counterpart: a clean
        # cached rebuild losing four Fayetteville flags. The export layer alone
        # still cannot see it -- nothing on the row contradicts anything -- which
        # is why the repair puts the witness OUTSIDE the row, in the declared
        # annotation input. See acceptance/test_matrix_values.py M6.1.
        from acceptance.test_matrix_values import (_declared_annotations,
                                                   check_annotated_facts_keep_their_flags)
        lost = check_annotated_facts_keep_their_flags([both], _declared_annotations())
        results.append({
            "test": "remove warning text and review status together",
            "synthetic": True, "actual": out2,
            "real_counterpart": "clean cached rebuild loses four Fayetteville flags",
            "detected_against_declared_annotation_input": bool(lost),
            "lost": lost[:2]})
    else:
        results.append({"test": "flag stripping controls", "synthetic": True,
                        "actual": "FIXTURE ABSENT",
                        "detail": "the 999999 anomaly row is not in this database"})

    # ---- the auditor's missing-reference control, driven through the real status rule
    ref = {("AUDIT_METRIC", 2025, "Q4", "annual", "raw"): {
        "value": "123", "canonical_row_id": "AUDIT_REF_1", "raw_or_derived": "raw",
        "source_fact_id": "AUDIT_FACT", "filing_id": "AUDIT_FILE"}}
    comparison = run_regressions.compare(ref, {}, "AUDIT_SYNTHETIC_MISSING_REFERENCE")
    status, fatal = run_regressions.status_for(comparison)
    results.append({
        "test": "reference nonzero value missing from produced output",
        "synthetic": True, "comparison": comparison,
        "delivered_main_status_formula": ("PASS" if (len(comparison["value_differences"]) +
                                                    len(comparison["identity_differences"])) == 0
                                          else "FAIL"),
        "repaired_status_rule": status, "fatal_differences": fatal,
        "expected": "FAIL"})

    con.close()
    out_path = paths.REPORTS / "additional_negative_controls.json"
    out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(json.dumps([{k: v for k, v in r.items() if k != "comparison"}
                      for r in results], indent=2, default=str))
    print(f"\n-> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
