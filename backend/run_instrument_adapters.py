#!/usr/bin/env python3
"""
Run the adapters whose subject is an INSTRUMENT, not a filing entity.

    python3 run_instrument_adapters.py --offline --as-of 2026-09-07

`run.py` iterates the operating universe, so every adapter it calls is called
once per eligible ENTITY. That is right for a form a carrier files and wrong for
the FERC Oil Pipeline Index, which is one industry-wide instrument published
once per index year under 18 CFR 342.3 in Docket RM93-11-000.

The delivered release got this wrong in the way the repair contract forbids: it
attached the single national index to 28 individual carriers as though each had
its own, which is a scope error (rule 3) and would let a consumer sum one index
28 times. Adding `oil_index` to `run.py`'s adapter list would repeat the shape of
that mistake -- the adapter would be invoked 28 times and would refuse 28 times,
and the run would be noisier without being more correct.

So the instrument gets its own entry point and its own synthetic entity key,
seeded from `config/instrument_entities.csv`. The key is deliberately NOT a FERC
CID and NOT a filing entity, and it is excluded from the asset roster, so the
109-asset / 104-entity reconciliation is unaffected.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import importlib
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run as runner                                                 # noqa: E402

#: adapter module -> the instrument key it publishes under
INSTRUMENT_ADAPTERS = {"oil_index": "FERC-OIL-INDEX"}
INSTRUMENTS_CSV = HERE / "config" / "instrument_entities.csv"


def load_instruments() -> dict[str, dict]:
    if not INSTRUMENTS_CSV.is_file():
        raise SystemExit(f"missing declared input: {INSTRUMENTS_CSV}")
    out = {}
    with INSTRUMENTS_CSV.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out[row["entity_key"]] = row
    return out


def _complete_slots(rows: list[dict], entity_key: str, run_id: str) -> list[dict]:
    """Fill the columns `coverage_expected` requires but an instrument adapter
    has no way to know.

    An adapter states the SUBSTANCE of an obligation -- which metric, over which
    interval, at which scope, on what published authority. The identity and the
    bookkeeping (`slot_id`, `entity_key`, `template`, `requirement`, `frozen_at`)
    belong to the runner, and computing them here rather than in the adapter is
    what keeps a slot id derived from one function everywhere.

    Nothing is invented: `requirement` is REQUIRED because 18 CFR 342.3(d)
    directs the ceiling adjustment for every index year, and the evidence the
    adapter supplied is the Commission notice that published it.
    """
    from ferclib.staging import slot_id
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    out = []
    for r in rows:
        basis = r.get("period_basis") or "interval"
        start = r.get("period_start") or ""
        end = r.get("period_end") or ""
        inst = r.get("instant_date") or ""
        metric = r["metric_id"]
        regime = r.get("source_regime") or "FERC Oil Pipeline Index"
        scope = r.get("scope") or ""
        out.append({
            "slot_id": slot_id(entity_key, metric, regime, basis, start, end,
                               inst, scope),
            "entity_key": entity_key,
            "asset_id": None,          # an instrument is not an asset
            "template": r.get("template") or "liquids",
            "metric_id": metric, "source_regime": regime, "period_basis": basis,
            "period_start": start or None, "period_end": end or None,
            "instant_date": inst or None,
            "reporting_year": int(start[:4]) if start[:4].isdigit() else None,
            "reporting_period": r.get("reporting_period") or "",
            "scope": scope, "unit_rule": r.get("unit_rule"),
            "requirement": r.get("requirement") or "REQUIRED",
            "requirement_evidence": r.get("requirement_evidence"),
            "applicability_version": r.get("applicability_version") or "18 CFR 342.3(d)",
            "frozen_at": stamp, "frozen_run_id": run_id,
            "obligation_form": "Notice of Annual Change in the PPI-FG",
            "obligation_authority": "18 CFR 342.3(d), Docket No. RM93-11-000",
            "obligation_evidence_kind": "published_commission_notice",
            "denominator_origin": "instrument_calendar",
            "docket": "RM93-11-000",
            "source_health": "ok",
        })
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--adapter", action="append", default=[])
    ap.add_argument("--year-from", type=int, default=1997)
    ap.add_argument("--year-to", type=int, default=2027)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--as-of", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--budget", type=int, default=None)
    args = ap.parse_args(argv)

    # Reuse run.py's Context so the single writer, the request budget, the
    # offline enforcement and the as-of date are all the same objects the
    # universe run uses. An instrument must not get its own private plumbing.
    ns = argparse.Namespace(
        command="universe", entity=[], template=[], adapter=[], limit=0,
        year_from=args.year_from, year_to=args.year_to, budget=args.budget,
        force=args.force, offline=args.offline, as_of=args.as_of)
    ctx = runner.Context(ns)
    instruments = load_instruments()
    rc = 0
    try:
        ctx.staging.start_run("instruments", vars(ns), runner.REGISTRY_VERSION,
                              runner.CODE_VERSION)
        for name in (args.adapter or list(INSTRUMENT_ADAPTERS)):
            key = INSTRUMENT_ADAPTERS.get(name)
            if key is None:
                ctx.log("error", f"{name} is not a declared instrument adapter")
                rc = 1
                continue
            meta = instruments.get(key)
            if meta is None:
                ctx.log("error", f"{key} is not declared in {INSTRUMENTS_CSV.name}; "
                                 "refusing to invent an entity")
                rc = 1
                continue
            mod = importlib.import_module(f"adapters.{name}")

            # seed the instrument entity -- never as an asset
            ctx.staging.write_entities([{
                "entity_key": key, "cid": None, "local_key": key,
                "legal_name": meta["legal_name"], "parent": None, "ticker": None,
                "jurisdiction": None, "note": meta.get("note")}])

            entity = {"entity_key": key, "legal_name": meta["legal_name"],
                      "template": "liquids", "assets": []}
            filings = mod.retrieve(ctx, entity, year_from=args.year_from,
                                   year_to=args.year_to)
            expected = _complete_slots(mod.freeze_expected(ctx, entity, filings, []),
                                       key, ctx.staging.run_id)
            ctx.staging.freeze_expected(expected)
            observations, edges = mod.canonicalise(ctx, entity, filings, expected)
            pops = [p for o in observations for p in (o.pop("_populations", []) or [])]
            events = [e for o in observations for e in (o.pop("_events", []) or [])]
            docfacts = [o.pop("_document_fact") for o in observations
                        if "_document_fact" in o]
            report = ctx.staging.commit_unit(
                adapter=name, entity_key=key,
                year_from=args.year_from, year_to=args.year_to,
                metric_ids=[m.id for m in runner.BY_ADAPTER.get(name, [])],
                observations=observations, edges=edges, qa_events=events,
                document_facts=docfacts, populations=pops)
            ctx.log("info", f"{name}: {len(filings)} source records, "
                            f"{len(expected)} slots, {len(observations)} observations, "
                            f"{len(edges)} edges, {len(pops)} populations "
                            f"({report})")
        ctx.staging.finish_run("complete" if rc == 0 else "partial")
    finally:
        ctx.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
