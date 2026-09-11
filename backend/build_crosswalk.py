#!/usr/bin/env python3
"""
Requirements crosswalk: every original Day 2 / Day 3 row -> a revised metric ID.

Nothing is allowed to fall off the back of the specification. Each source row
gets exactly one disposition:

    carried      implemented under the same intent
    renamed      same requirement, new metric ID
    merged       folded into another metric (the target is named)
    gated        implemented, but publication is conditional on evidence
    superseded   deliberately replaced by the revised template, with the reason
    unmapped     NOT matched -- reported, never silently discarded

`unmapped` is the important one. A crosswalk that maps everything is usually a
crosswalk that stopped looking, so unmapped rows are printed and counted rather
than quietly dropped.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import pathlib
import re
import sys
from collections import Counter

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from ferclib.registry import BY_ID, REGISTRY                        # noqa: E402
from ferclib.publication import publish_generation                  # noqa: E402

DAY3 = HERE / "inputs" / "day3"
MATRIX = DAY3 / "FERC_operating_assets_metric_decision_matrix.csv"
GAP = DAY3 / "FERC_operating_assets_current_vs_target_gap.csv"
OUT = HERE / "config" / "requirements_crosswalk.csv"
SUMMARY_OUT = HERE / "config" / "requirements_crosswalk_summary.json"

REGIME_TEMPLATE = {
    "Interstate gas": "interstate_gas", "Liquids": "liquids",
    "Intrastate §311/Hinshaw": "intrastate_549d", "LNG (NGA §3)": "lng",
}

#: Explicit dispositions where a keyword match would be wrong or ambiguous.
#: Each names its target metric or its reason, so nothing rests on a fuzzy match.
EXPLICIT = {
    # superseded by the revised template, with the reason recorded
    "return on rate base": ("superseded", "p700_revenue_less_cost_of_service",
                            "The revised template replaces a standalone 'return on rate base' "
                            "headline with the Page 700 revenue-versus-cost panel. The filed "
                            "return is an ALLOWANCE inside the cost-of-service calculation and "
                            "cannot establish what the carrier actually earned."),
    "project-level revenue": ("superseded", "",
                              "Retained as a future candidate only. Revenue over plant is not "
                              "EBITDA yield, ROIC or IRR, and no complete profitability "
                              "numerator has been established. Explicitly out of scope."),
    "earnings at risk": ("superseded", "ioc_expiry_profile",
                         "Replaced by an expiry profile on a NAMED weight. No 'earnings at "
                         "risk' figure is inferred from usage alone."),
}

KEYWORDS = [
    (r"gas operating revenue|operating revenue.*gas", "gas_operating_revenues"),
    (r"net utility operating income|nuoi", "net_utility_operating_income"),
    (r"utility operating expense", "utility_operating_expenses"),
    (r"\bmargin\b", "operating_margin_pct"),
    (r"net income", "net_income"),
    (r"gas plant in service", "gas_plant_in_service"),
    (r"transmission plant", "transmission_plant"),
    (r"accumulated (provision for )?depreciation", "accum_prov_depreciation_gas_plant"),
    (r"depreciation expense", "depreciation_expense"),
    (r"operation expense|o&m", "operation_expense"),
    (r"maintenance expense", "maintenance_expense"),
    (r"total (volume of )?throughput|quarterly throughput", "total_throughput"),
    (r"forwardhaul", "forwardhaul_throughput"),
    (r"backhaul", "backhaul_throughput"),
    (r"gas received", "gas_received_by_utility"),
    (r"gas delivered|deliveries of gas", "gas_delivered_by_utility"),
    (r"losses and unaccounted|unaccounted[- ]for", "gas_losses_and_unaccounted_for"),
    (r"gas account", "gas_account_balancing_total"),
    (r"miles of transmission|transmission miles", "transmission_miles"),
    (r"compressor unit|number of units", "compressor_units"),
    (r"horsepower", "certificated_horsepower"),
    (r"compressor fuel", "compressor_fuel"),
    (r"storage capacity|certificated storage", "storage_capacity"),
    (r"maximum day.?s withdrawal|max.*withdrawal", "max_day_withdrawal"),
    (r"working gas", "storage_capacity"),
    (r"injection|withdrawal detail", "storage_injections"),
    (r"single[- ]day peak deliver", "single_day_peak_deliveries"),
    (r"three[- ]day peak deliver", "three_day_peak_deliveries"),
    (r"peak.*date|date.*peak", "single_day_peak_date"),
    (r"auxiliary peaking", "aux_peaking_capacity"),
    (r"negotiated[- ]rate", "negotiated_rate_volumes"),
    (r"discounted[- ]rate", "discounted_rate_volumes"),
    (r"peak[- ]day (deliverability|capacity)", "cap_reported_capacity"),
    (r"peak[- ]day.*(ratio|utilis)", "cap_peak_day_ratio"),
    # contracts / IOC
    (r"firm.*mdq|mdq.*firm|transportation mdq", "ioc_firm_transport_mdq"),
    (r"contracted storage", "ioc_contracted_storage_quantity"),
    (r"top[- ]five|top 5|concentration", "ioc_top5_shipper_concentration"),
    (r"expiry|primary[- ]term", "ioc_expiry_profile"),
    (r"affiliate", "ioc_affiliate_share"),
    (r"shipper identit|identity coverage", "ioc_identity_coverage"),
    (r"mdq change|change in mdq", "ioc_mdq_change"),
    (r"point|segment", "ioc_points"),
    # liquids
    (r"barrels delivered|delivered barrels", "liq_barrels_delivered"),
    (r"barrels received", "liq_barrels_received"),
    (r"barrel[- ]mile", "liq_barrel_miles"),
    (r"net carrier operating income|ncoi", "liq_net_carrier_operating_income"),
    (r"carrier property", "liq_carrier_property"),
    (r"gross addition", "liq_gross_additions"),
    (r"miles of pipeline", "liq_miles_of_pipeline"),
    (r"trunk revenue", "liq_trunk_revenue"),
    (r"delivery revenue", "liq_delivery_revenue"),
    (r"allowance", "liq_allowance_revenue"),
    (r"incidental", "liq_incidental_revenue"),
    (r"cost of service", "p700_total_cost_of_service"),
    (r"rate base", "p700_original_cost_rate_base"),
    (r"wacc|cost of capital|capital structure", "p700_wacc"),
    (r"interstate operating revenue|page 700 revenue", "p700_interstate_operating_revenue"),
    (r"revenue.*(less|minus|versus|vs).*cost", "p700_revenue_less_cost_of_service"),
    (r"revenue per barrel|per[- ]barrel revenue", "liq_revenue_per_barrel"),
    (r"(operating )?(expense|cost) per barrel", "liq_opex_per_barrel"),
    # `liq_oil_price_index` was split into a published index LEVEL and the
    # year-on-year CHANGE; a bare "index" no longer resolves to one metric, and
    # "index of customers" is a different thing entirely from the oil index.
    (r"index of customers", "ioc_identity_coverage"),
    (r"oil pipeline index|index year|index multiplier|price index",
     "liq_oil_pipeline_index_factor"),
    (r"reduced (form 6|schedule)|full form 6", "liq_form6_filing_completeness"),
    # 549D
    (r"billed.*usage|usage.*billed|quarterly billed", "i311_billed_transport_usage"),
    (r"firm share|firm/interruptible|firm vs", "i311_firm_share"),
    (r"reservation|determinant", "i311_storage_determinants"),
    (r"annual.*(transportation )?revenue", "i311_annual_transport_revenue"),
    (r"revenue component", "i311_revenue_components"),
    (r"component rate|reported rate", "i311_component_rates"),
    (r"discount|rate schedule|pr docket", "i311_discounts_and_schedules"),
    (r"no reportable activity|reporting state", "i311_reporting_state"),
    # regulation / LNG / events
    (r"rate case|general rate", "rate_case_status"),
    (r"effective date|rate effective", "rate_effective_date"),
    (r"refund", "refund_exposure_window"),
    (r"tariff", "tariff_operative_rate"),
    (r"liquefaction", "lng_liquefaction_capacity"),
    (r"regas|send[- ]?out|vaporiz", "lng_regas_sendout_capacity"),
    (r"lng storage|authorised storage", "lng_storage_capacity"),
    (r"in[- ]service|enter service|authoris", "lng_status_authorised"),
    (r"operational report|operating report", "lng_operational_report"),
    (r"inspection", "lng_inspection"),
    (r"order|amendment|restriction", "lng_material_order"),
    (r"interruption|576", "event_reportable_interruption"),
    (r"replacement", "event_replacement_report"),
    (r"textblock|narrative|profile disclosure", "profile_miles_narrative"),
    # regime-specific phrasings that the generic patterns above miss
    (r"operating revenues? \(annual\)|operating revenue.*form 6", "liq_operating_revenue"),
    (r"operating expenses? \(annual\)|operating expense.*form 6", "liq_operating_expenses"),
    (r"trunk[- ]line miles|miles.*p\.602", "liq_miles_of_pipeline"),
    (r"return components?", "p700_return_component"),
    (r"revenue[- ]account breakdown|gas-revenue account|p\.300-series", "gas_operating_revenues"),
    (r"storage subscription", "ioc_contracted_storage_quantity"),
    (r"distinct shipper count|contract identity", "ioc_identity_coverage"),
    (r"continuation state|days until next possible contract expiration", "ioc_expiry_profile"),
    (r"zone[- ]level mdq|zone aggregation", "ioc_points"),
    (r"moratorium|next[- ]case deadline", "rate_case_status"),
    (r"revenue coverage", "i311_revenue_grain_diagnostic"),
]


#: Rows that are not metrics at all. They are requirements on the SOURCE LAYER or
#: on the shared infrastructure, and they are satisfied by an adapter or by the
#: staging schema rather than by a registry entry. Classifying them as "unmapped"
#: would misreport working implementation as a missing requirement.
SOURCE_LAYER = [
    (r"form 2 ?/ ?2-a annual xbrl|form 2 / 2-a annual", "adapters/gas_xbrl.py + ferclib/xbrl_adapter.py"),
    (r"form 3-q gas", "adapters/gas_xbrl.py (Form 3Q Gas regime)"),
    (r"form 6 annual xbrl", "adapters/liquids_xbrl.py (Form 6 regime)"),
    (r"form 6-q", "adapters/liquids_xbrl.py (Form 6Q regime)"),
    (r"form 549d datasets", "adapters/form549d.py"),
    (r"549b capacity report", "adapters/capacity.py"),
    (r"compressor stations \(p\.508", "gas registry: compressor_units / certificated_horsepower / compressor_fuel"),
    (r"q4 ?/ ?quarterly derivation|ytd differencing", "ferclib/periods.py + xbrl_adapter derivations"),
    (r"pr-docket rate election", "adapters/elibrary_docs.py (rate_case_status)"),
    (r"rate-case stage and clock", "adapters/elibrary_docs.py (rate_case_status, rate_effective_date)"),
    (r"settlement economic terms", "adapters/elibrary_docs.py (gated on order text)"),
    (r"form 73|audit / enforcement|audit/enforcement", "adapters/elibrary_docs.py (event route; retrieval verified, classification gated)"),
    (r"major certificate and regulatory event", "adapters/lng.py (lng_material_order)"),
    (r"contract begin ?/ ?end dates", "ioc_expiry_profile + i311_contract_expiry"),
    (r"component-level rates", "i311_component_rates"),
]
INFRASTRUCTURE = [
    (r"provenance layer", "staging schema: source_facts occurrence key + source_manifest + lineage_edges"),
    (r"canonical filing/version|restatement handling", "staging.classify_version + invalidate_dependents"),
    (r"migrated-history flagging", "status Origin.FERC_MIGRATED"),
    (r"missing-data states", "ferclib/status.py Availability (13 distinct states)"),
    (r"asset mapping classes", "staging schema: asset_entity_map with mapping_scope"),
    (r"common output envelope|operating event feed", "exporters.py + events table"),
]


def _scan(table, low):
    for pattern, target in table:
        if re.search(pattern, low):
            return target
    return None


def match(text: str, regime: str) -> tuple[str, str, str]:
    low = text.lower()
    if "unavailable_from_ferc" in low:
        return ("documented_source_limit", "",
                "Recorded in the revised template as a source limitation with its reason. "
                "It is a documented absence of a FERC source, not unfinished engineering, "
                "and no value is fabricated to fill it.")
    hit = _scan(SOURCE_LAYER, low)
    if hit:
        return ("carried_as_source_layer", "",
                f"a source/retrieval requirement rather than a metric; satisfied by {hit}")
    hit = _scan(INFRASTRUCTURE, low)
    if hit:
        return ("carried_as_infrastructure", "",
                f"a cross-cutting requirement satisfied by shared infrastructure: {hit}")
    for phrase, (disp, target, note) in EXPLICIT.items():
        if phrase in low:
            return disp, target, note
    template = REGIME_TEMPLATE.get(regime, "")
    hits = []
    for pattern, mid in KEYWORDS:
        if re.search(pattern, low):
            m = BY_ID.get(mid)
            if m and (not template or template in m.templates):
                hits.append(mid)
    if not hits:
        # try again ignoring the template, then record the regime mismatch
        for pattern, mid in KEYWORDS:
            if re.search(pattern, low) and mid in BY_ID:
                return ("carried", mid,
                        f"matched on intent; the revised registry places it under "
                        f"{'/'.join(BY_ID[mid].templates)} rather than {regime}")
        return "unmapped", "", "no revised metric matched this row"
    if len(hits) == 1:
        return "carried", hits[0], ""
    return "merged", hits[0], f"folded together with {', '.join(hits[1:])}"


# ==================================================================== fields
#
# The 166 audited template-field rows, and where each one is now.
#
# The audit reconciled 166 rows: 144 implemented/retrieved/validated, 16
# interpretation-blocked, 3 source-blank-or-not-required, 1 retrieval failure and
# 2 unfinished. Every one of them must still be findable after the repair, and a
# row is not allowed to disappear because it was renamed or split -- which is
# exactly what happened to `liq_oil_price_index`. So the audited list is joined
# to the current registry by (template, field_id), and anything that does not
# join is resolved through an explicit successor map rather than dropped.

AUDITED_166 = HERE / "inputs" / "audit_baseline" / "field_status_all_166_audited.csv"
FIELD_OUT = HERE / "config" / "field_crosswalk_166.csv"

#: Deliberate renames and splits since the audited snapshot. Each names its
#: successors and why; an audited row with no successor here and no direct match
#: is reported as `lost`, never silently absent.
FIELD_SUCCESSORS: dict[tuple[str, str], tuple[str, tuple[str, ...], str]] = {
    ("liquids", "liq_oil_price_index"): (
        "split", ("liq_oil_pipeline_index_factor", "liq_oil_pipeline_index_change"),
        "The single 'oil price index' row conflated two different published FERC "
        "figures: the annual index LEVEL the Commission sets under 18 CFR 342.3 and "
        "the year-on-year CHANGE applied to a carrier's ceiling rate. They have "
        "different units and different uses, so they are separate fields; neither "
        "inherits the other's status."),
}


def field_crosswalk(audited_path: pathlib.Path) -> list[dict]:
    from ferclib.registry import REGISTRY as _REG
    current = {(t, m.id): m for m in _REG for t in m.templates}
    audited = _read_csv(audited_path, {"template", "field_id", "outcome"},
                        "audited 166-field baseline")
    if len(audited) != 166:
        raise ValueError(f"audited field baseline must contain 166 rows, found {len(audited)}")
    rows, claimed = [], set()
    for a in audited:
        key = (a["template"], a["field_id"])
        if key in current:
            m = current[key]
            claimed.add(key)
            rows.append({
                "source_doc": "audit 2026-09-08 field_status_all_166",
                "source_row": f"{key[0]}::{key[1]}", "template": key[0],
                "audited_outcome": a["outcome"], "disposition": "carried",
                "successors": a["field_id"], "adapter": m.adapter, "role": m.role,
                "note": ""})
            continue
        disp, succ, why = FIELD_SUCCESSORS.get(key, ("lost", (), ""))
        present = [s for s in succ if (key[0], s) in current]
        missing = [s for s in succ if (key[0], s) not in current]
        if disp == "lost":
            why = ("no current registry row and no declared successor; this audited "
                   "field would otherwise have vanished from the report")
        elif missing:
            disp, why = "partially_lost", (
                f"{why} DECLARED SUCCESSOR(S) ABSENT FROM THE REGISTRY: "
                f"{', '.join(missing)}")
        claimed.update((key[0], s) for s in present)
        rows.append({
            "source_doc": "audit 2026-09-08 field_status_all_166",
            "source_row": f"{key[0]}::{key[1]}", "template": key[0],
            "audited_outcome": a["outcome"], "disposition": disp,
            "successors": ";".join(succ), "adapter": "", "role": "", "note": why})
    for key, m in sorted(current.items()):
        if key in claimed:
            continue
        rows.append({
            "source_doc": "current registry", "source_row": f"{key[0]}::{key[1]}",
            "template": key[0], "audited_outcome": "", "disposition": "new",
            "successors": key[1], "adapter": m.adapter, "role": m.role,
            "note": "not present in the audited 166; introduced by the repair"})
    return rows


def _read_csv(path: pathlib.Path, required: set[str], label: str) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(f"missing required {label}: {path}")
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fields = set(reader.fieldnames or ())
        missing = sorted(required - fields)
        if missing:
            raise ValueError(f"{label} has wrong schema; missing columns {missing}: {path}")
        rows = list(reader)
    if not rows:
        raise ValueError(f"{label} contains zero data rows: {path}")
    return rows


def _csv_bytes(rows: list[dict]) -> bytes:
    if not rows:
        raise ValueError("refusing to serialize a zero-row crosswalk")
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--matrix", type=pathlib.Path, default=MATRIX)
    ap.add_argument("--gap", type=pathlib.Path, default=GAP)
    ap.add_argument("--audited-fields", type=pathlib.Path, default=AUDITED_166)
    ap.add_argument("--out", type=pathlib.Path, default=OUT)
    ap.add_argument("--summary-out", type=pathlib.Path, default=SUMMARY_OUT)
    ap.add_argument("--field-out", type=pathlib.Path, default=FIELD_OUT)
    ap.add_argument("--receipt", type=pathlib.Path,
                    default=HERE / "config" / "crosswalk_publication.json")
    ap.add_argument("--db", type=pathlib.Path,
                    default=pathlib.Path(os.environ["FERC_STAGING_DB"])
                    if os.environ.get("FERC_STAGING_DB") else None,
                    help="optional staging database whose requirements_crosswalk table is "
                         "replaced only after the complete crosswalk validates")
    args = ap.parse_args(argv)

    # Preflight every source and build every output in memory before any
    # published path is opened. Missing/empty/wrong-schema inputs therefore
    # leave the last-good crosswalk byte-for-byte intact.
    try:
        matrix_rows = _read_csv(
            args.matrix, {"Metric ID", "Metric", "Investor question", "FERC source",
                          "Asset regime"}, "Day-3 metric decision matrix")
        gap_rows = _read_csv(
            args.gap, {"Metric/source", "Target treatment", "Asset regime"},
            "Day-3 current-vs-target gap")
        fields = field_crosswalk(args.audited_fields)
    except (OSError, ValueError) as exc:
        print(f"crosswalk preflight failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    rows = []
    for src, path, source_rows, id_col, text_cols in (
            ("Day 3 metric decision matrix", args.matrix, matrix_rows, "Metric ID",
             ("Metric", "Investor question", "FERC source")),
            ("Day 3 current-vs-target gap", args.gap, gap_rows, "Metric/source",
             ("Metric/source", "Target treatment"))):
        for r in source_rows:
            regime = r.get("Asset regime", "")
            text = " ".join(r.get(c, "") or "" for c in text_cols)
            disp, target, note = match(text, regime)
            metric = BY_ID.get(target)
            rows.append({
                "source_doc": src,
                "source_row": (r.get(id_col) or text[:60]).strip(),
                "source_regime": regime,
                "source_text": text.strip()[:160],
                "disposition": disp,
                "metric_id": target,
                "template": "/".join(metric.templates) if metric else "",
                "adapter": metric.adapter if metric else "",
                "implementation": metric.implementation if metric else "not_implemented",
                "role": metric.role if metric else "",
                "note": note,
            })
        print(f"  read {path.name}: {len(rows)} cumulative rows")

    disp = Counter(r["disposition"] for r in rows)
    covered = {r["metric_id"] for r in rows if r["metric_id"]}
    uncovered = [m.id for m in REGISTRY if m.id not in covered]
    summary = {
        "source_rows": len(rows),
        "dispositions": dict(disp),
        "distinct_metrics_mapped": len(covered),
        "registry_metrics": len(REGISTRY),
        "registry_metrics_with_no_source_row": uncovered,
        "note": ("Metrics with no source row are NEW requirements introduced by the "
                 "revised template (narrative profile fallback, status dimensions, "
                 "grain diagnostics). They are additions, not unmapped legacy rows."),
    }
    # ------------------------------------------------ the 166 audited fields
    fdisp = Counter(r["disposition"] for r in fields)
    audited_rows = sum(1 for r in fields
                       if r["source_doc"].startswith("audit 2026-09-08"))
    target_keys = {(r["template"], successor)
                   for r in fields for successor in r["successors"].split(";")
                   if successor}
    summary["field_crosswalk"] = {
        "audited_rows_accounted_for": audited_rows,
        "current_registry_rows": sum(len(m.templates) for m in REGISTRY),
        "target_keys_accounted_for": len(target_keys),
        "dispositions": dict(fdisp),
        "lost": [r["source_row"] for r in fields
                 if r["disposition"] in ("lost", "partially_lost")],
    }
    expected_targets = sum(len(m.templates) for m in REGISTRY)
    if audited_rows != 166 or len(target_keys) != expected_targets or summary["field_crosswalk"]["lost"]:
        print("crosswalk validation failed before publication: "
              f"audited={audited_rows}, targets={len(target_keys)}/{expected_targets}, "
              f"lost={summary['field_crosswalk']['lost']}", file=sys.stderr)
        return 2

    payload = {
        "requirements_crosswalk.csv": _csv_bytes(rows),
        "requirements_crosswalk_summary.json":
            (json.dumps(summary, indent=1, sort_keys=True) + "\n").encode("utf-8"),
        "field_crosswalk_166.csv": _csv_bytes(fields),
    }
    # The receipt defines the publication boundary.  In a replay it lives
    # below FERC_OUTPUT_DIR, so using its parent also keeps the immutable
    # generation out of the frozen source/configuration tree.  The defaults
    # still resolve to HERE/config for the legacy direct command.
    manifest = publish_generation(
        args.receipt.parent, args.receipt, "crosswalk", payload,
        metadata={"matrix": str(args.matrix), "gap": str(args.gap),
                  "audited_fields": str(args.audited_fields),
                  "source_rows": len(rows), "audited_rows": audited_rows,
                  "target_keys": len(target_keys)},
        compatibility_targets={
            "requirements_crosswalk.csv": args.out,
            "requirements_crosswalk_summary.json": args.summary_out,
            "field_crosswalk_166.csv": args.field_out})

    if args.db is not None:
        from ferclib.staging import Staging
        db = Staging(args.db, create=False)
        try:
            db_rows = [{k: r.get(k, "") for k in
                        ("source_doc", "source_row", "disposition", "metric_id",
                         "template", "note")} for r in rows]
            db.replace_snapshot("requirements_crosswalk", db_rows,
                                ["source_doc", "source_row"])
        finally:
            db.close()

    print(json.dumps({k: v for k, v in summary.items()
                      if k != "registry_metrics_with_no_source_row"}, indent=1))
    unmapped = [r for r in rows if r["disposition"] == "unmapped"]
    if unmapped:
        print(f"\nUNMAPPED source rows ({len(unmapped)}) -- reported, not discarded:")
        for r in unmapped:
            print(f"  [{r['source_regime']}] {r['source_row']}: {r['source_text'][:88]}")
    print(f"\n166 audited field rows: {audited_rows} accounted for, {dict(fdisp)}")
    print(f"published generation {manifest['generation_id']} -> {args.receipt}")
    if args.db is not None:
        print(f"database requirements_crosswalk: {len(rows)} rows -> {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
