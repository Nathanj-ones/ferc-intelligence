#!/usr/bin/env python3
"""
Freeze the operating universe from the production roster, read-only.

The roster is the authorised source of asset/company mapping metadata. It is
opened read-only and never written. Every post-COD asset row is carried across,
INCLUDING the FPA rows that fall outside the pipeline/LNG templates: they are
labelled OUT_OF_TEMPLATE and retained so universe reconciliation stays complete.
They are never silently dropped and never forced into a gas template.
"""

from __future__ import annotations

import csv
import json
import pathlib
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
PROD_DB = HERE.parent.parent / "data" / "ferc.db"
OUT = HERE / "config" / "universe.csv"

#: authority -> template. Jurisdiction refines nga-7 into the storage template.
TEMPLATE = {"nga-7": "interstate_gas", "ngpa-311": "intrastate_549d",
            "ica": "liquids", "nga-3": "lng",
            "fpa-mbr": "OUT_OF_TEMPLATE", "fpa-qf": "OUT_OF_TEMPLATE"}


def main() -> int:
    if not PROD_DB.is_file():
        print(f"production roster not found at {PROD_DB}", file=sys.stderr)
        return 1
    con = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = []
    for r in con.execute("""
            SELECT a.asset_id, a.ticker, a.display_name, a.cid, a.authority, a.status,
                   a.status_note, a.cod_group, a.group_key, a.interest_display, a.capacity,
                   a.note, e.name AS entity_name, e.parent, e.jurisdiction, e.forms,
                   e.ownership_pct, e.via_jv
            FROM assets a LEFT JOIN entities e ON e.cid = a.cid
            WHERE a.cod_group = 'post-cod'
            ORDER BY a.authority, a.ticker, a.asset_id"""):
        d = dict(r)
        template = TEMPLATE.get(d["authority"], "UNKNOWN")
        if d["authority"] == "nga-7" and d["jurisdiction"] == "nga-storage":
            template = "gas_storage"
        d["template"] = template
        d["entity_key"] = d.pop("cid") or ""
        d["entity_name"] = d["entity_name"] or d["display_name"]
        # A JV interest is a LABEL. The filing entity still reports 100% of its
        # own system, so mapping_scope records what the figures cover and the
        # ownership percentage never multiplies a metric.
        d["mapping_scope"] = "whole_entity"
        d["roster_forms"] = d.pop("forms") or ""
        rows.append(d)
    con.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    cols = list(rows[0])
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    from collections import Counter
    by_t = Counter(r["template"] for r in rows)
    summary = {
        "asset_rows_post_cod": len(rows),
        "in_template_rows": sum(v for k, v in by_t.items() if k != "OUT_OF_TEMPLATE"),
        "out_of_template_rows": by_t.get("OUT_OF_TEMPLATE", 0),
        "by_template": dict(by_t),
        "distinct_filing_entities": len({r["entity_key"] for r in rows if r["entity_key"]}),
        "in_template_entities": len({r["entity_key"] for r in rows
                                     if r["entity_key"] and r["template"] != "OUT_OF_TEMPLATE"}),
        "local_key_entities": sorted({r["entity_key"] for r in rows
                                      if r["entity_key"].startswith("NO-FERC-CID")}),
        "rows_without_entity": sum(1 for r in rows if not r["entity_key"]),
        "note": ("OUT_OF_TEMPLATE rows are FPA/electric authority and have no approved "
                 "operating template in this contract. They are retained for universe "
                 "reconciliation and are never described as having no FERC data."),
    }
    (HERE / "config" / "universe_summary.json").write_text(
        json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    print(f"\nfrozen -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
