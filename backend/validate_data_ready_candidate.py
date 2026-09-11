#!/usr/bin/env python3
"""Focused validation and small handoff outputs for the standalone candidate."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import pathlib
import sqlite3
import sys
import uuid
from collections import Counter

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ferclib import coverage as cov  # noqa: E402
from ferclib.publication import verify_receipt  # noqa: E402


REPRESENTATIVES = (
    ("interstate_gas_transco", "wmb-transco", "C000654",
     ("total_throughput", "gas_operating_revenues", "ioc_top5_shipper_concentration")),
    ("gas_storage_pine_prairie", "wmb-pine-prairie-storage", "C001058",
     ("cap_reported_capacity", "ioc_contracted_storage_quantity",
      "ioc_top5_shipper_concentration")),
    ("liquids_overland_pass", "wmb-overland-pass", "C000608",
     ("liq_operating_revenue", "liq_revenue_per_barrel",
      "p700_revenue_to_cost_ratio")),
    ("intrastate_arcadia", "wmb-arcadia-storage", "C001562",
     ("i311_affiliate_activity", "i311_storage_determinants",
      "i311_billed_transport_usage")),
    ("lng_sabine_pass_terminal", "lng-sabine-pass-lng-terminal",
     "NO-FERC-CID:Sabine Pass LNG, L.P.",
     ("lng_regas_sendout_capacity", "lng_operational_report", "lng_inspection")),
    ("oil_pipeline_index", None, "FERC-OIL-INDEX",
     ("liq_oil_pipeline_index_change", "liq_oil_pipeline_index_factor")),
)


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def atomic_json(path: pathlib.Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=True, default=str)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def csv_rows(path: pathlib.Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def date_key(observation: dict) -> str:
    period = observation["period"]
    return period.get("instant") or period.get("end") or period.get("start") or ""


def choose(observations: list[dict], metric_id: str) -> dict | None:
    rows = [row for row in observations if row.get("metric_id") == metric_id]
    if not rows:
        return None

    def rank(row):
        q = row["quality"]
        score = (3 if q.get("availability") == "present" and q.get("validation") == "pass"
                 and q.get("version_status") != "superseded" else
                 2 if q.get("availability") == "present" else 1)
        return score, date_key(row), row.get("observation_id") or ""

    return max(rows, key=rank)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=pathlib.Path, default=HERE)
    ap.add_argument("--db", type=pathlib.Path,
                    default=HERE / "staging" / "operating_assets.sqlite")
    ap.add_argument("--baseline-coverage", type=pathlib.Path,
                    default=HERE / "baseline_evidence" / "r6_coverage_statistics.json")
    ap.add_argument("--out", type=pathlib.Path, default=HERE / "deliverables")
    args = ap.parse_args(argv)
    root, exports, out = args.root.resolve(), args.root.resolve() / "exports", args.out.resolve()
    checks = []

    def check(name: str, passed: bool, evidence) -> None:
        checks.append({"name": name, "status": "pass" if passed else "fail",
                       "evidence": evidence})

    manifest = verify_receipt(root, root / "publication_receipt.json")
    check("publication_receipt", True, {"generation_id": manifest["generation_id"],
                                        "files": len(manifest["files"])})
    uri = f"file:{args.db.resolve()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("BEGIN")
    q = lambda sql, p=(): con.execute(sql, p).fetchall()

    quick = q("PRAGMA quick_check")
    fk = q("PRAGMA foreign_key_check")
    check("sqlite_quick_check", [row[0] for row in quick] == ["ok"], [row[0] for row in quick])
    check("sqlite_foreign_keys", not fk, {"violations": len(fk)})

    db_counts = {table: q(f'SELECT COUNT(*) FROM "{table}"')[0][0] for table in
                 ("entities", "assets", "asset_entity_map", "observations", "filings",
                  "documents", "events", "lineage_edges", "lineage_populations",
                  "observation_versions", "reviewed_source_annotations")}
    directory = json.loads((exports / "frontend_v1" / "assets.json").read_text())
    headline = json.loads((exports / "frontend_v1" /
                           "headline_availability.json").read_text())
    contract = json.loads((exports / "frontend_v1" / "contract.json").read_text())
    assets = directory["assets"]
    asset_by_id = {row["id"]: row for row in assets}
    universe = csv_rows(root / "config" / "universe.csv")
    db_asset_ids = {row[0] for row in q("SELECT asset_id FROM assets")}
    universe_ids = {row["asset_id"] for row in universe}
    exported_in_scope = {row["id"] for row in assets if row["inScope"]}
    check("complete_roster", len(assets) == 120 and {row["id"] for row in assets} == universe_ids,
          directory["counts"])
    check("operating_scope", exported_in_scope == db_asset_ids and len(db_asset_ids) == 109,
          {"database": len(db_asset_ids), "export": len(exported_in_scope)})

    map_counts = Counter(row[0] for row in q(
        "SELECT entity_key FROM asset_entity_map ORDER BY entity_key"))
    shared = {key for key, count in map_counts.items() if count > 1}
    shared_assets = [row for row in assets if row.get("entityMappings")
                     and row["entityMappings"][0]["entity_key"] in shared]
    check("shared_entity_comparison_gate",
          all(not row["comparisonEligible"] and row["scopeRelation"] ==
              "shared_filer_entity_context" for row in shared_assets),
          {"shared_entities": sorted(shared), "asset_rows": len(shared_assets)})

    population_rows = csv_rows(exports / "lineage_populations.csv")
    population_ids = {row["population_id"] for row in population_rows}
    edge_rows = csv_rows(exports / "lineage_edges.csv")
    referenced = {row["input_population_id"] for row in edge_rows
                  if row.get("input_population_id")}
    check("lineage_population_resolution",
          referenced <= population_ids and len(population_rows) == db_counts["lineage_populations"],
          {"referenced": len(referenced), "exported": len(population_ids),
           "database_rows": db_counts["lineage_populations"]})

    entity_payloads = {}
    observation_ids = set()
    annotation_count = 0
    eligible_count = 0
    source_url_count = 0
    for path in sorted((exports / "frontend_v1" / "entity_payloads").glob("*.json")):
        payload = json.loads(path.read_text())
        key = payload["entity"]["entity_key"]
        entity_payloads[key] = payload
        annotation_count += len(payload["reviewed_annotations"])
        for row in payload["observations"]:
            oid = row["observation_id"]
            if oid in observation_ids:
                check("unique_frontend_observation_ids", False, oid)
            observation_ids.add(oid)
            if row["comparison"]["eligible"]:
                eligible_count += 1
                ql = row["quality"]
                valid = (ql["availability"] == "present" and ql["validation"] == "pass"
                         and ql["version_status"] != "superseded"
                         and row["value"]["numeric"] is not None and row["scope"]["resolved"])
                if not valid:
                    check("comparison_gate_invariant", False, oid)
            source = row["source"]
            filing, document = source.get("filing") or {}, source.get("document") or {}
            if filing.get("source_url") or document.get("source_url"):
                source_url_count += 1
    check("frontend_observation_population", len(observation_ids) == db_counts["observations"],
          {"export": len(observation_ids), "database": db_counts["observations"]})
    check("comparison_gate_invariant", not any(
        row["name"] == "comparison_gate_invariant" and row["status"] == "fail"
        for row in checks), {"eligible_observations": eligible_count})
    check("source_links_exposed", source_url_count > 0,
          {"observations_with_filing_or_document_url": source_url_count})
    check("review_annotations", annotation_count == 7 == db_counts["reviewed_source_annotations"],
          {"frontend": annotation_count, "database": db_counts["reviewed_source_annotations"]})

    fayetteville = entity_payloads["C001012"]["observations"]
    warnings = [row for row in fayetteville
                if row["value"]["as_filed"] == "999999"
                and row["quality"]["validation"] == "source_anomaly_review"
                and row["quality"]["qa_flags"]]
    check("fayetteville_warnings", len(warnings) == 4,
          {"count": len(warnings), "observation_ids": [r["observation_id"] for r in warnings]})

    expected = [dict(row) for row in q("SELECT * FROM coverage_expected ORDER BY slot_id")]
    measured = [dict(row) for row in q("SELECT * FROM coverage_measured ORDER BY slot_id")]
    calculated_coverage = cov.summarise(expected, measured)
    check("coverage_recalculation", calculated_coverage == headline["coverage"],
          {k: calculated_coverage[k] for k in
           ("expected_core", "expected_core_due", "core_populated", "core_validated",
            "core_due_populated", "core_due_validated")})

    # The standalone history contract must not silently collapse to the recent
    # 2024--2026 coverage window.  Transco and Tennessee Gas Pipeline are the
    # two explicitly required older-history controls.
    historical_controls = {}
    for entity_key, label in (("C000654", "Transco"),
                              ("C000020", "Tennessee Gas Pipeline")):
        row = q(
            "SELECT MIN(COALESCE(instant_date,period_end,period_start)), "
            "MAX(COALESCE(instant_date,period_end,period_start)), COUNT(*) "
            "FROM observations WHERE entity_key=? AND availability='present'",
            (entity_key,),
        )[0]
        historical_controls[entity_key] = {
            "label": label, "history_from": row[0], "history_to": row[1],
            "present_observations": row[2],
        }
    check(
        "older_transco_tgp_history_preserved",
        all(row["history_from"] and row["history_from"] <= "2016-12-31"
            for row in historical_controls.values()),
        historical_controls,
    )

    document_scope = q(
        "SELECT COUNT(*), "
        "SUM(CASE WHEN TRIM(COALESCE(scope,''))<>'' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN TRIM(COALESCE(scope_rule,''))<>'' THEN 1 ELSE 0 END) "
        "FROM observations WHERE source_regime='eLibrary document' "
        "AND availability='present'"
    )[0]
    check(
        "document_scope_and_contract_separated",
        document_scope[0] > 0 and document_scope[0] == document_scope[1] == document_scope[2],
        {"present_document_observations": document_scope[0],
         "actual_scope_present": document_scope[1],
         "scope_contract_present": document_scope[2]},
    )

    entity_summary = {row["entity_key"]: row for row in csv_rows(
        exports / "coverage_by_entity.csv")}
    fanout_errors = []
    for key in shared:
        actual = q("SELECT COUNT(*) FROM observations WHERE entity_key=?", (key,))[0][0]
        exported = int(entity_summary[key]["observations"])
        if actual != exported:
            fanout_errors.append({"entity_key": key, "database": actual, "export": exported})
    check("entity_summary_no_asset_fanout", not fanout_errors,
          {"shared_entities_checked": len(shared), "errors": fanout_errors})

    registry = csv_rows(exports / "metric_registry.csv")
    unit_columns = {"canonical_unit", "unit_family", "display_unit", "display_scale",
                    "admissible_units", "admissible_units_evidence"}
    check("metric_unit_contract_export", len(registry) == 120 and
          unit_columns <= set(registry[0]),
          {"metrics": len(registry), "unit_columns": sorted(unit_columns)})

    accession_rows = {row[0]: row for row in q(
        "SELECT accession_number,entity_key,filing_id FROM filings "
        "WHERE accession_number IN ('20150930-5060','20231229-5212','20260527-5009')")}
    check("captured_supported_accessions_applied",
          set(accession_rows) == {"20150930-5060", "20231229-5212"},
          {key: {"entity_key": row[1], "filing_id": row[2]}
           for key, row in accession_rows.items()})
    wrong_body = (root / "inputs" / "official_ferc_recovery" / "list_responses" /
                  "20260527-5009.body")
    wrong_text = wrong_body.read_text(encoding="utf-8")
    check("tesoro_northwest_collision_rejected",
          "Tesoro Logistics Northwest Pipeline LLC" in wrong_text and
          "20260527-5009" not in accession_rows,
          {"captured_body_bytes": wrong_body.stat().st_size,
           "captured_body_sha256": sha256(wrong_body),
           "reason": "official description names Tesoro, not Williams Northwest Pipeline"})

    active_549d = [dict(row) for row in q(
        "SELECT blocker_id,scope,summary FROM blockers "
        "WHERE resolved_at IS NULL AND adapter='form549d' ORDER BY blocker_id")]
    check("549d_gates_retained", len(active_549d) == 5,
          {"live_blockers": len(active_549d),
           "interpretation": "four filer-year gates plus one consolidated policy gate; "
                             "the final R6 exception register retains six original exceptions"})

    current_events = q("SELECT COUNT(*) FROM events WHERE destination='investor_feed' "
                       "AND is_backfill=0")[0][0]
    check("current_change_feed_honest", current_events == 0,
          {"current_investor_feed_events": current_events,
           "note": "historical/archive/review events remain available; no current event invented"})
    check("related_project_gap_explicit",
          all(row.get("relatedProjectMappingStatus") == "not_supplied"
              and row.get("relatedProjectIds") == [] for row in assets),
          {"assets": len(assets)})
    check("contract_schema", contract.get("schema") == "ferc_operating_assets_frontend_v1",
          {"contract_version": contract.get("contract_version")})

    representative_dir = out / "representative_payloads"
    representative_index = []
    for label, asset_id, entity_key, metrics in REPRESENTATIVES:
        payload = entity_payloads[entity_key]
        selected = [row for metric in metrics
                    if (row := choose(payload["observations"], metric)) is not None]
        result = {
            "schema": "ferc_data_ready_representative_v1",
            "publication_generation_id": manifest["generation_id"],
            "as_of": directory["as_of"],
            "label": label,
            "asset": asset_by_id.get(asset_id) if asset_id else None,
            "entity": payload["entity"],
            "entity_scope_relation": payload["entity_scope_relation"],
            "selected_metrics": list(metrics),
            "observations": selected,
            "selection_rule": ("latest usable source-backed observation per requested metric; "
                               "otherwise latest retained gated/absent record"),
        }
        path = representative_dir / f"{label}.json"
        atomic_json(path, result)
        representative_index.append({"label": label, "path": str(path.relative_to(out)),
                                     "bytes": path.stat().st_size, "sha256": sha256(path),
                                     "observations": len(selected)})
    atomic_json(out / "REPRESENTATIVE_PAYLOAD_INDEX.json", {
        "schema": "ferc_data_ready_representative_index_v1",
        "publication_generation_id": manifest["generation_id"],
        "items": representative_index,
    })

    baseline = json.loads(args.baseline_coverage.read_text())
    bridge_fields = ("expected_core", "expected_core_due", "core_populated",
                     "core_validated", "core_due_populated", "core_due_validated",
                     "core_populated_pct", "core_validated_pct",
                     "core_due_populated_pct", "core_due_validated_pct")
    bridge = {
        "schema": "ferc_coverage_bridge_r6_to_data_ready_v1",
        "same_denominator": (baseline["expected_core"] == calculated_coverage["expected_core"]
                             and baseline["expected_core_due"] ==
                             calculated_coverage["expected_core_due"]),
        "r6": {key: baseline[key] for key in bridge_fields},
        "candidate": {key: calculated_coverage[key] for key in bridge_fields},
        "delta": {key: calculated_coverage[key] - baseline[key] for key in bridge_fields
                  if isinstance(baseline[key], (int, float))},
        "cause": ("R6 matched coverage slots against the observation's source-backed actual "
                  "scope, while expected slots store the independent registry scope contract. "
                  "The candidate matches on scope_rule and separately requires resolved actual "
                  "scope; observation population remains unchanged."),
    }
    atomic_json(out / "COVERAGE_BEFORE_AFTER.json", bridge)

    headline_csv = out / "HEADLINE_AVAILABILITY.csv"
    tmp_csv = headline_csv.with_name(f".{headline_csv.name}.{os.getpid()}.tmp")
    fields = ("group", "assets", "entities", "observations", "present_observations",
              "usable_observations", "review_observations", "history_from", "history_to",
              "core_slots", "core_due_slots", "populated", "validated", "in_review",
              "populated_pct", "validated_pct")
    with tmp_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in fields}
                         for row in headline["templates"])
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_csv, headline_csv)

    failures = [row for row in checks if row["status"] == "fail"]
    result = {
        "schema": "ferc_data_ready_validation_v1",
        "status": "pass" if not failures else "fail",
        "publication_generation_id": manifest["generation_id"],
        "publication_files": len(manifest["files"]),
        "database": {"path": str(args.db.resolve()), "bytes": args.db.stat().st_size,
                     "sha256": sha256(args.db), "counts": db_counts},
        "checks": checks,
        "failures": failures,
    }
    atomic_json(out / "CONTRACT_VALIDATION.json", result)
    con.rollback()
    con.close()
    print(json.dumps({"status": result["status"], "checks": len(checks),
                      "failures": len(failures), "generation_id": manifest["generation_id"],
                      "database_counts": db_counts}, indent=1, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
