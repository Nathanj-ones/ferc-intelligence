#!/usr/bin/env python3
"""
Real assertions for the Form 549D adapter, against live-retrieved data.

These are not shape tests. Each one encodes a semantic finding that would be a
reporting error if the adapter regressed, and each is checked against the actual
bulk tables (served from the content-addressed cache, so the suite costs no
requests after the first run).

Nothing here writes to the shared staging database: the adapter is driven
through a stub `ctx` whose staging discards writes.

    python3 tests/test_549d.py            # or: python3 -m pytest tests/test_549d.py
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from adapters import form549d as d549                                       # noqa: E402
from ferclib import periods                                                 # noqa: E402
from ferclib.http import (Client, FetchError, SourceCache,                  # noqa: E402
                          assert_no_secrets, redact)
from ferclib.status import Availability, Validation                         # noqa: E402


# ---------------------------------------------------------------- stub context

class StubStaging:
    """Accepts everything the adapter writes and keeps none of it."""

    run_id = "test-run"

    def __init__(self):
        self.blockers, self.filings, self.checkpoints = [], [], {}

    def is_done(self, *a, **k):
        return False

    def checkpoint(self, adapter, cid, scope, state, **k):
        self.checkpoints[(adapter, cid, scope)] = state

    def open_blocker(self, adapter, kind, summary, **k):
        self.blockers.append({"adapter": adapter, "kind": kind, "summary": summary, **k})
        return "blk-test"

    def classify_version(self, *a, **k):
        return "original", None

    def write_filing_bundle(self, filing, **k):
        self.filings.append((filing, k))

    def freeze_expected(self, rows):
        return len(rows)

    def query(self, sql, params=()):
        return []

    def log(self, *a, **k):
        pass


class Ctx:
    def __init__(self, year_from=2024, year_to=2026):
        self.cache = SourceCache(HERE / "source_cache")
        self.client = Client(self.cache, min_interval=0.25, logger=lambda *_: None)
        self.staging = StubStaging()
        self.force = True
        self.offline = False
        # Generated resolution evidence belongs to disposable test output, not
        # the candidate/input tree.  Keep the source cache read-only and route
        # adapter diagnostics through the same declared boundary as production.
        self._output_tmp = tempfile.TemporaryDirectory(
            prefix="ferc-test-549d-output-")
        self.output_dir = pathlib.Path(self._output_tmp.name)
        self.workdir = HERE / "staging" / "work"
        self.args = types.SimpleNamespace(year_from=year_from, year_to=year_to)
        self.logs = []

    def log(self, level, msg, **k):
        self.logs.append((level, msg))


CTX = Ctx()
ENTITY = {"C000434": "Kinder Morgan Tejas Pipeline LLC",
          "C000826": "Bridgeline Holdings, L.P.",
          "C001422": "Kinder Morgan Keystone Gas Storage LLC",
          "C001773": "Jefferson Island Storage & Hub, L.L.C.",
          "C010084": "Gulf Coast Express Pipeline LLC",
          "C000435": "Kinder Morgan Border Pipeline LLC",
          "C000588": "ONEOK Gas Transportation, L.L.C."}
_RUN_CACHE: dict = {}


def entity(cid):
    return {"entity_key": cid, "legal_name": ENTITY.get(cid, cid),
            "template": "intrastate_549d", "parent": "", "ticker": "",
            "assets": [{"asset_id": f"test-{cid.lower()}", "display_name": cid,
                        "template": "intrastate_549d"}]}


def run(cid, year_from=2024, year_to=2026):
    """Drive the full adapter contract for one entity. Cached per test session."""
    key = (cid, year_from, year_to)
    if key not in _RUN_CACHE:
        ctx = Ctx(year_from, year_to)
        e = entity(cid)
        filings = d549.retrieve(ctx, e, year_from=year_from, year_to=year_to)
        expected = d549.freeze_expected(ctx, e, filings, e["assets"])
        obs, edges = d549.canonicalise(ctx, e, filings, expected)
        _RUN_CACHE[key] = {"ctx": ctx, "filings": filings, "expected": expected,
                           "observations": obs, "edges": edges}
    return _RUN_CACHE[key]


def pick(obs, metric, year=None, period=None, basis=None):
    out = [o for o in obs if o["metric_id"] == metric
           and (year is None or o["reporting_year"] == year)
           and (period is None or o["reporting_period"] == period)
           and (basis is None or o["period_basis"] == basis)]
    return out


def rows_of(filings, year, quarter):
    for f in filings:
        if f["reporting_year"] == year and f["reporting_period"] == quarter and f["is_canonical"]:
            return f["_rows"]
    return []


# ================================================================ source facts

class TestSourceSemantics(unittest.TestCase):
    """Findings about the source itself, asserted against the whole live table."""

    @classmethod
    def setUpClass(cls):
        cls.t = d549._tables(CTX)
        cls.ds29 = [r for v in cls.t["rows_by_filing"].values() for r in v]

    def test_shipper_id_is_a_row_key_not_a_shipper_identifier(self):
        """`Shipper_ID` is unique per ROW, so it can never identify a shipper."""
        ids = [d549._s(r.get("Shipper_ID")) for r in self.ds29]
        self.assertEqual(len(set(ids)), len(ids),
                         "Shipper_ID stopped being one-per-row; re-verify before using it")
        # and it never repeats across a shipper's several contracts, which is what
        # an identifier would have to do
        by_name = {}
        for r in self.ds29:
            k = (d549._s(r.get("Filer_CID")).upper(), d549._s(r.get("Shipper_Name")).upper())
            by_name.setdefault(k, set()).add(d549._s(r.get("Shipper_ID")))
        multi = [k for k, v in by_name.items() if len(v) > 1]
        self.assertTrue(multi, "expected shippers holding several Shipper_IDs")

    def test_natural_key_is_filing_plus_sequence(self):
        keys = [d549.natural_key(r) for r in self.ds29]
        self.assertEqual(len(set(keys)), len(keys),
                         "(Form549D_ID, Sequence_Number) is no longer unique")

    def test_shipper_key_groups_by_legal_name_not_by_shipper_id(self):
        a = {"Shipper_Name": "Occidental Energy Marketing, Inc.", "Shipper_ID": "1"}
        b = {"Shipper_Name": "OCCIDENTAL ENERGY MARKETING INC", "Shipper_ID": "999999"}
        self.assertEqual(d549.shipper_key(a), d549.shipper_key(b))
        self.assertTrue(d549.shipper_key(a))

    def test_unknown_affiliate_is_the_empty_string(self):
        """There is no 'Unknown' token: blank IS unknown, and it is not rare."""
        vals = [d549._s(r.get("Affiliate_Status")) for r in self.ds29]
        blanks = sum(1 for v in vals if not v)
        self.assertGreater(blanks, 0, "expected blank Affiliate_Status rows")
        self.assertNotIn("UNKNOWN", {v.upper() for v in vals},
                         "an explicit Unknown token appeared; re-verify the blank convention")
        self.assertEqual(d549.norm_affiliate(""), "unknown")
        self.assertEqual(d549.norm_affiliate(None), "unknown")
        self.assertNotEqual(d549.norm_affiliate(""), d549.norm_affiliate("No"))
        for token in ("N", "no", "NO", "No"):
            self.assertEqual(d549.norm_affiliate(token), "no")
        for token in ("Y", "yes", "YES", "Yes"):
            self.assertEqual(d549.norm_affiliate(token), "yes")

    def test_pagination_completeness_is_proven_not_assumed(self):
        for name in ("respondent", "contract"):
            ev = self.t["evidence"][name]
            self.assertTrue(ev["complete"])
            self.assertFalse(ev["has_more"], f"{name}: has_more must be false when unpaged")
            self.assertEqual(ev["fetched_rows"], ev["declared_row_count"],
                             f"{name}: fetched rows must equal details/ row_count")
            self.assertTrue(ev["boundary_probe_matches_last_row"],
                            f"{name}: the offset probe must reproduce the last unpaged row")

    def test_no_credential_reaches_any_recorded_url(self):
        p = HERE / "discovery" / "549d_resolution.json"
        self.assertTrue(p.is_file())
        assert_no_secrets(p.read_text(encoding="utf-8"))
        u = redact("https://api.data.ferc.gov/v1/dataset/29/data/?limit=1&api_key=SECRETVALUE1")
        self.assertNotIn("SECRETVALUE1", u)
        self.assertIn("REDACTED", u)
        assert_no_secrets(u)
        key = d549.api_key()
        for f, _k in run("C000434")["ctx"].staging.filings:
            url = f["source_url"] or ""
            assert_no_secrets(url)
            self.assertNotIn(key, url, "a live credential reached a persisted filing URL")
            self.assertIn("REDACTED", url)
        for cid in ("C000434", "C000826"):
            for o in run(cid)["observations"]:
                blob = " ".join(str(o.get(k) or "") for k in
                                ("qa_flags", "notes", "missing_reason", "value_text"))
                assert_no_secrets(blob)
                self.assertNotIn(key, blob)

    def test_resolution_evidence_uses_declared_output_root(self):
        minimal = {
            "resolution": {}, "evidence": {}, "torn_snapshot": False,
            "collected_periods": [],
        }
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            candidate = root / "immutable-candidate"
            output = root / "work-output"
            candidate.mkdir()
            with mock.patch.object(d549, "HERE", candidate):
                path = d549._write_resolution_evidence(
                    types.SimpleNamespace(output_dir=output), minimal)
            self.assertEqual(output / "discovery" / "549d_resolution.json", path)
            self.assertTrue(path.is_file())
            self.assertFalse((candidate / "discovery" / "549d_resolution.json").exists())
            with self.assertRaises(AttributeError):
                d549._write_resolution_evidence(types.SimpleNamespace(), minimal)

    def test_unit_families_never_merge_incompatible_units(self):
        self.assertEqual(d549.unit_family("MMBtu"), d549.unit_family("Dth"))
        self.assertNotEqual(d549.unit_family("MMBtu"), d549.unit_family("Mcf"))
        self.assertNotEqual(d549.unit_family("MMBtu"), d549.unit_family("th"))
        self.assertNotEqual(d549.unit_family("MMBtu"), d549.unit_family("MMBtu-day"))
        self.assertNotEqual(d549.unit_family("Dth-mo."), d549.unit_family("Dth"))
        self.assertEqual(d549.unit_family(""), "(unit not reported)")


# ================================================================ grain

class TestGrain(unittest.TestCase):

    def test_bridgeline_2025q4_ambiguous_contracts_are_detected(self):
        r = run("C000826")
        g = d549.probe_grain(rows_of(r["filings"], 2025, "Q4"))
        self.assertFalse(g["resolved"])
        found = {x["contract"] for x in g["ambiguous_groups"]}
        self.assertEqual(found, {"TRN00593", "TRN00676"})

    def test_grain_ambiguity_blocks_the_annual_aggregate(self):
        r = run("C000826")
        o = pick(r["observations"], "i311_annual_transport_revenue", 2025,
                 basis=periods.ANNUAL)
        self.assertEqual(len(o), 1)
        o = o[0]
        self.assertEqual(o["validation"], Validation.BLOCKED_AMBIGUITY)
        self.assertIsNone(o["value_num"], "a blocked aggregate must assert no value")
        self.assertIsNone(o["value_text"])
        self.assertIn("scenario_B_row_sum", o["qa_flags"])
        self.assertIn("scenario_A_one_value_per_ambiguous_contract", o["qa_flags"])
        self.assertIn("TRN00593", o["qa_flags"])

    def test_the_resolved_prior_year_is_not_blocked(self):
        """Blocking is per (filer, period): 2024-Q4 is clean and must publish."""
        r = run("C000826")
        o = pick(r["observations"], "i311_annual_transport_revenue", 2024,
                 basis=periods.ANNUAL)[0]
        self.assertEqual(o["availability"], Availability.PRESENT)
        self.assertIsNotNone(o["value_num"])

    def test_identical_amounts_block_rather_than_collapse(self):
        """The forbidden 'same amount means duplicate' rule would COLLAPSE these.

        Two rows of one contract, identical non-zero revenue, differing only in
        shipper name: the adapter must treat that as unresolved and block, which
        is the opposite of collapsing them as a duplicate.
        """
        rows = [{"Form549D_ID": "1", "Sequence_Number": 1, "Contract_Number": "X1",
                 "Shipper_Name": "ALPHA", "Service_Type": "Transportation",
                 "Total_Rev": 100.0},
                {"Form549D_ID": "1", "Sequence_Number": 2, "Contract_Number": "X1",
                 "Shipper_Name": "BETA", "Service_Type": "Transportation",
                 "Total_Rev": 100.0}]
        g = d549.probe_grain(rows)
        self.assertFalse(g["resolved"])
        sc = d549.grain_scenarios(g, rows, "Total_Rev")
        self.assertFalse(sc["treatments_coincide"])
        self.assertEqual(sc["scenario_B_row_sum"], 200.0)
        self.assertEqual(sc["scenario_A_one_value_per_ambiguous_contract"], 100.0)

    def test_a_tiny_divergence_still_blocks(self):
        """No percentage threshold may buy permission to publish an uncertain sum."""
        rows = [{"Form549D_ID": "1", "Sequence_Number": 1, "Contract_Number": "X1",
                 "Shipper_Name": "ALPHA", "Service_Type": "Transportation",
                 "Total_Rev": 1_000_000.0},
                {"Form549D_ID": "1", "Sequence_Number": 2, "Contract_Number": "X1",
                 "Shipper_Name": "BETA", "Service_Type": "Transportation",
                 "Total_Rev": 1.0}]
        g = d549.probe_grain(rows)
        sc = d549.grain_scenarios(g, rows, "Total_Rev")
        self.assertFalse(sc["treatments_coincide"],
                         "a 0.0001% gap must still block; thresholds are forbidden")

    def test_service_legs_are_not_treated_as_ambiguous(self):
        """ONEOK WesTex reports one contract over several delivery points."""
        rows = [{"Form549D_ID": "1", "Sequence_Number": 1, "Contract_Number": "X1",
                 "Shipper_Name": "ALPHA", "Service_Type": "Transportation",
                 "Delivery_Point_Name": "POINT A", "Total_Rev": 10.0},
                {"Form549D_ID": "1", "Sequence_Number": 2, "Contract_Number": "X1",
                 "Shipper_Name": "ALPHA", "Service_Type": "Transportation",
                 "Delivery_Point_Name": "POINT B", "Total_Rev": 20.0}]
        g = d549.probe_grain(rows)
        self.assertTrue(g["resolved"])
        self.assertEqual(g["grain_class"], "contract_x_service_leg")

    def test_ambiguity_that_cannot_change_the_total_does_not_block_that_total(self):
        """Bridgeline 2025-Q4 usage: the ambiguous groups carry no usage at all."""
        r = run("C000826")
        o = [x for x in pick(r["observations"], "i311_billed_transport_usage", 2025, "Q4")
             if x["availability"] == Availability.PRESENT]
        self.assertTrue(o, "usage must publish when the grain question cannot move it")
        self.assertIn("UNRESOLVED GRAIN", o[0]["qa_flags"])
        self.assertIn("treatments_coincide", o[0]["qa_flags"])

    def test_attribution_ambiguity_blocks_the_concentration(self):
        """Tejas 2024-Q2: the ambiguous contracts carry the weight AND two shipper
        names, so the concentration ranking cannot be asserted."""
        r = run("C000434")
        o = pick(r["observations"], "i311_top5_shipper_share", 2024, "Q2")[0]
        self.assertEqual(o["validation"], Validation.BLOCKED_AMBIGUITY)
        self.assertIsNone(o["value_num"])
        self.assertEqual(pick(r["observations"], "i311_affiliate_activity",
                              2024, "Q2")[0]["validation"], Validation.BLOCKED_AMBIGUITY)

    def test_attribution_ambiguity_is_separate_from_total_ambiguity(self):
        """One non-zero row and one zero row: the TOTAL is identical under both
        readings, but WHICH shipper holds it is not, so a share must still stop."""
        rows = [{"Form549D_ID": "1", "Sequence_Number": 1, "Contract_Number": "X1",
                 "Shipper_Name": "ALPHA", "Service_Type": "Transportation",
                 "Usage_BU": 500.0},
                {"Form549D_ID": "1", "Sequence_Number": 2, "Contract_Number": "X1",
                 "Shipper_Name": "BETA", "Service_Type": "Transportation",
                 "Usage_BU": 0.0}]
        g = d549.probe_grain(rows)
        self.assertFalse(g["resolved"])
        self.assertTrue(d549.grain_scenarios(g, rows, "Usage_BU")["treatments_coincide"],
                        "the total cannot differ between the two readings here")
        amb = d549.attribution_ambiguous(g, "Usage_BU", "Shipper_Name")
        self.assertEqual(len(amb), 1)
        self.assertEqual(sorted(amb[0]["competing_values"]), ["ALPHA", "BETA"])

    def test_an_ambiguous_group_that_carries_no_weight_does_not_block_a_share(self):
        """Bridgeline 2025-Q4: TRN00593/TRN00676 hold revenue but zero usage, so a
        usage-weighted concentration is untouched -- and says so."""
        r = run("C000826")
        o = pick(r["observations"], "i311_top5_shipper_share", 2025, "Q4")[0]
        self.assertEqual(o["availability"], Availability.PRESENT)
        self.assertIn("UNRESOLVED GRAIN", o["qa_flags"])

    def test_tejas_2024q2_usage_is_blocked(self):
        """Even the 'clean' filer has one quarter the grain cannot carry."""
        r = run("C000434")
        g = d549.probe_grain(rows_of(r["filings"], 2024, "Q2"))
        self.assertFalse(g["resolved"])
        o = pick(r["observations"], "i311_billed_transport_usage", 2024, "Q2")
        self.assertTrue(o)
        self.assertTrue(all(x["validation"] == Validation.BLOCKED_AMBIGUITY for x in o))
        self.assertTrue(all(x["value_num"] is None for x in o))


# ================================================================ revenue

class TestAnnualRevenue(unittest.TestCase):

    def test_storage_revenue_is_excluded_from_the_transportation_figure(self):
        r = run("C000826")
        o = pick(r["observations"], "i311_annual_transport_revenue", 2024,
                 basis=periods.ANNUAL)[0]
        rows = rows_of(r["filings"], 2024, "Q4")
        transport = sum(d549._f0(x.get("Total_Rev")) for x in rows
                        if d549.norm_service(x.get("Service_Type")) == "Transportation")
        everything = sum(d549._f0(x.get("Total_Rev")) for x in rows)
        self.assertAlmostEqual(o["value_num"], transport, places=2)
        self.assertNotAlmostEqual(o["value_num"], everything, places=2)
        # Marker AND substance, which are different guarantees.
        #
        # The marker is a cheap, stable machine hook, imported from the adapter
        # rather than hardcoded so it survives any rewording. The substance
        # assertions stop the marker becoming a rubber stamp on prose that later
        # drifts away from what it claims. This test previously pinned only the
        # literal "ORDER 735-A SCOPE DIVERGENCE", which w3-financial's rewrite
        # replaced with fuller text -- so it failed on strictly better prose,
        # which is a test measuring the wrong thing.
        flags = o["qa_flags"]
        self.assertIn(d549.SCOPE_DIVERGENCE_MARKER, flags,
                      "the flag carries no machine-readable scope-divergence marker")
        self.assertIn("735-A", flags,
                      "the flag does not cite Order No. 735-A, which is the authority "
                      "for excluding storage revenue from this field")
        self.assertRegex(flags, r"(?i)storage",
                         "the flag never mentions the storage revenue it excludes")
        self.assertRegex(flags, r"(?i)exclud",
                         "the flag cites the order but never says the revenue is excluded")
        # And the divergence is still raised as an event, not silently absorbed.
        self.assertTrue(any(e["event_type"] == "order_735a_scope_divergence"
                            for e in o.get("_events", [])),
                        "the scope divergence is not reported as an event")

    def test_the_order_735a_divergence_is_reported_not_resolved(self):
        r = run("C000826")
        o = pick(r["observations"], "i311_annual_transport_revenue", 2024,
                 basis=periods.ANNUAL)[0]
        evs = o.get("_events", [])
        self.assertTrue(any(e["event_type"] == "order_735a_scope_divergence" for e in evs))
        comp = pick(r["observations"], "i311_revenue_components", 2024, basis=periods.ANNUAL)[0]
        payload = json.loads(comp["value_text"])
        self.assertIn("Storage", payload["revenue_by_service_type_as_filed"])
        self.assertGreater(payload["revenue_by_service_type_as_filed"]["Storage"], 0)

    def test_missing_storage_revenue_never_depresses_transportation_coverage(self):
        """A storage-only filer gets NOT_APPLICABLE, not a blank or a zero."""
        r = run("C001773")
        o = pick(r["observations"], "i311_annual_transport_revenue", 2024,
                 basis=periods.ANNUAL)[0]
        self.assertEqual(o["availability"], Availability.NOT_APPLICABLE)
        self.assertIsNone(o["value_num"])
        self.assertIn("no transportation-service rows", o["missing_reason"])
        self.assertNotEqual(o["availability"], Availability.SOURCE_BLANK)
        comp = pick(r["observations"], "i311_revenue_components", 2024, basis=periods.ANNUAL)[0]
        self.assertGreater(
            json.loads(comp["value_text"])["revenue_by_service_type_as_filed"]["Storage"], 0,
            "the storage revenue must still be reported, just not as transportation revenue")

    def test_q4_usage_coverage_is_separate_from_annual_revenue_coverage(self):
        """Gulf Coast Express 2024: full annual revenue, zero Q4 usage."""
        r = run("C010084")
        rows = rows_of(r["filings"], 2024, "Q4")
        transport = [x for x in rows
                     if d549.norm_service(x.get("Service_Type")) == "Transportation"]
        rev_rows = [x for x in transport if d549._f0(x.get("Total_Rev"))]
        usage_rows = [x for x in transport if d549._f0(x.get("Usage_BU"))]
        self.assertTrue(rev_rows, "expected revenue-bearing rows")
        self.assertEqual(usage_rows, [], "expected zero Q4 usage on this filer")
        o = pick(r["observations"], "i311_annual_transport_revenue", 2024,
                 basis=periods.ANNUAL)[0]
        self.assertEqual(o["availability"], Availability.PRESENT)
        self.assertGreater(o["value_num"], 0,
                           "annual revenue must survive a quarter with no usage")
        self.assertIn("ANNUAL REVENUE COVERAGE", o["qa_flags"])
        self.assertIn("Q4 USAGE COVERAGE", o["qa_flags"])
        self.assertIn("THESE TWO ARE INDEPENDENT", o["qa_flags"])

    def test_components_are_reconciled_to_their_own_total_not_summed_into_it(self):
        r = run("C000826")
        rev = pick(r["observations"], "i311_annual_transport_revenue", 2024,
                   basis=periods.ANNUAL)[0]
        comp = pick(r["observations"], "i311_revenue_components", 2024, basis=periods.ANNUAL)[0]
        payload = json.loads(comp["value_text"])
        self.assertAlmostEqual(payload["reported_total_transportation"], rev["value_num"],
                               places=2)
        self.assertIn("component_sum_transportation", payload)
        self.assertIn("rows_where_components_disagree_with_their_own_total", payload)
        self.assertIsNone(comp["value_num"],
                          "the components observation must not publish a single number")

    def test_annual_revenue_is_never_divided_by_quarterly_usage(self):
        """No emitted value may equal annual revenue over a quarterly usage figure."""
        for cid in ("C000826", "C000434", "C010084"):
            r = run(cid)
            for year in (2024, 2025):
                rev = pick(r["observations"], "i311_annual_transport_revenue", year,
                           basis=periods.ANNUAL)
                if not rev or rev[0]["value_num"] is None:
                    continue
                total = rev[0]["value_num"]
                for q in ("Q1", "Q2", "Q3", "Q4"):
                    for u in pick(r["observations"], "i311_billed_transport_usage", year, q):
                        if not u["value_num"]:
                            continue
                        implied = total / u["value_num"]
                        for o in r["observations"]:
                            if o["value_num"] is None:
                                continue
                            self.assertNotAlmostEqual(
                                o["value_num"], implied, places=6,
                                msg=(f"{cid} {year}{q}: {o['metric_id']} equals annual revenue "
                                     "divided by quarterly usage"))
        # and the rates metric says so explicitly
        o = pick(run("C000826")["observations"], "i311_component_rates", 2025, "Q2")[0]
        self.assertIn("ANNUAL REVENUE IS NEVER DIVIDED BY QUARTERLY USAGE", o["qa_flags"])

    def test_off_quarter_revenue_is_surfaced_and_not_summed(self):
        """Tejas repeats its 2025 annual revenue in the 2026-Q1 filing."""
        r = run("C000434")
        st = pick(r["observations"], "i311_reporting_state", 2026, "Q1")[0]
        self.assertIn("FILER ERROR TO SURFACE", st["qa_flags"])
        self.assertTrue(any(e["event_type"] == "off_quarter_annual_revenue"
                            for e in st.get("_events", [])))
        self.assertEqual(pick(r["observations"], "i311_annual_transport_revenue", 2026), [],
                         "no annual slot may exist for a year with no collected Q4")


# ================================================================ conventions

class TestReportingConventions(unittest.TestCase):

    def test_no_reportable_activity_is_a_valid_filing_state(self):
        r = run("C000435")
        for q in ("Q1", "Q2", "Q3", "Q4"):
            st = pick(r["observations"], "i311_reporting_state", 2025, q)[0]
            self.assertEqual(st["availability"], Availability.PRESENT)
            self.assertEqual(st["value_text"], "no_reportable_activity_declared")
            self.assertIn("VALID FILING STATE", st["qa_flags"])
            usage = pick(r["observations"], "i311_billed_transport_usage", 2025, q)[0]
            self.assertEqual(usage["availability"], Availability.NOT_APPLICABLE)
            self.assertNotEqual(usage["availability"], Availability.EXPECTED_NOT_LOCATED)
            self.assertIn("missing rows do not prove contracts terminated", usage["qa_flags"])

    def test_zero_is_never_published_as_a_measured_zero(self):
        r = run("C010084")
        o = [x for x in pick(r["observations"], "i311_billed_transport_usage", 2024, "Q4")]
        self.assertTrue(o)
        for x in o:
            self.assertIsNone(x["value_num"])
            self.assertNotEqual(x["availability"], Availability.PRESENT)
            self.assertIn("0-fills", x["missing_reason"] + x["qa_flags"])

    def test_every_observation_is_labelled_with_the_311_scope(self):
        for cid in ("C000434", "C000826", "C001422"):
            for o in run(cid)["observations"]:
                self.assertIn("not necessarily the whole intrastate pipeline", o["qa_flags"])

    def test_determinants_stay_separately_scoped(self):
        r = run("C001422")
        o = pick(r["observations"], "i311_storage_determinants", 2025, "Q4")[0]
        payload = json.loads(o["value_text"])
        self.assertIn("Injection_BU", payload)
        self.assertIn("Withdrawal_BU", payload)
        self.assertNotIn("Usage_BU", payload,
                         "transportation usage must never appear among storage determinants")
        self.assertIn("spans both", payload["Reservation_BU"]["scope"].lower())
        for bucket in payload["Injection_BU"]["by_service_and_unit"]:
            self.assertIn("|", bucket, "each bucket must name its service type and unit")

    def test_point_codes_are_never_given_invented_names(self):
        r = run("C000826")
        o = pick(r["observations"], "i311_points_decoded", 2025, "Q2")[0]
        payload = json.loads(o["value_text"])
        rows = rows_of(r["filings"], 2025, "Q2")
        filed = {d549._s(x.get("Receipt_Point_Common_Code")) for x in rows}
        for p in payload["receipt"]:
            if p["common_code"]:
                self.assertIn(p["common_code"], filed,
                              "a point code was altered or invented")
        self.assertIn("REMAIN CODES", o["qa_flags"])

    def test_dockets_are_parsed_as_a_list(self):
        self.assertEqual(d549.parse_dockets("PR03-17-000, PR10-13-000"),
                         ["PR03-17-000", "PR10-13-000"])
        self.assertEqual(d549.parse_dockets("Docket No. PR24-95-000"), ["PR24-95-000"])
        self.assertEqual(d549.parse_dockets("PR-09-15-000"), ["PR09-15-000"])
        self.assertEqual(d549.parse_dockets(""), [])
        self.assertEqual(d549.parse_dockets("WHEELPOOL"), ["WHEELPOOL"])

    def test_signed_columns_are_not_netted_into_a_share_denominator(self):
        """Park/loan carries both directions in one column; a share must stay <= 100%."""
        r = run("C001422")
        for o in r["observations"]:
            if o["unit"] == "percent" and o["value_num"] is not None:
                self.assertGreaterEqual(o["value_num"], 0)
                self.assertLessEqual(o["value_num"], 100.0001,
                                     f"{o['metric_id']} exceeded 100%: opposite directions "
                                     "were netted into the denominator")
        top = pick(r["observations"], "i311_top5_shipper_share", 2025, "Q4")[0]
        self.assertIn("SIGNED COLUMN", top["qa_flags"])
        self.assertEqual(top["validation"], Validation.SOURCE_ANOMALY_REVIEW)

    def test_a_storage_only_filer_gets_a_named_weight_not_transport_usage(self):
        r = run("C001422")
        o = pick(r["observations"], "i311_top5_shipper_share", 2025, "Q4")[0]
        notes = json.loads(o["notes"])
        self.assertNotEqual(notes["weight_column"], "Usage_BU")
        self.assertIn(notes["weight_column"], {c for c, _d, _n in d549.DETERMINANTS})
        self.assertIn("identity_coverage_pct", notes)
        self.assertLessEqual(notes["identity_coverage_pct"], 100.0001)

    def test_absent_filing_is_not_called_overdue(self):
        """C003337 has no 2026-Q1 filing and no 549D deadline rule exists."""
        r = run("C003337") if "C003337" in ENTITY else None
        if r is None:
            ENTITY["C003337"] = "TPL SouthTex Transmission Company LP"
            r = run("C003337")
        o = pick(r["observations"], "i311_reporting_state", 2026, "Q1")[0]
        # an absent filing in a completely-fetched table is a SOURCE condition,
        # never our retrieval defect
        self.assertEqual(o["availability"], Availability.SOURCE_BLANK)
        self.assertNotEqual(o["availability"], Availability.RETRIEVAL_FAILED)
        self.assertNotEqual(o["availability"], Availability.NOT_IMPLEMENTED)
        self.assertIn("not a retrieval failure of ours", o["missing_reason"])
        self.assertIn("not proof that contracts terminated", o["missing_reason"])
        self.assertIn("no filing-due determination is made", o["qa_flags"])
        self.assertIn("'overdue' is not asserted", o["qa_flags"])
        self.assertNotIn("is overdue", o["qa_flags"].lower())
        self.assertNotIn("past due", o["qa_flags"].lower())


# ================================================================ contract

class TestAdapterContract(unittest.TestCase):

    def test_every_frozen_slot_ends_in_a_measured_status(self):
        for cid in ("C000434", "C000826", "C001422", "C000435"):
            r = run(cid)
            have = {(o["metric_id"], o["period_basis"], o.get("period_start"),
                     o.get("period_end")) for o in r["observations"]}
            for s in r["expected"]:
                self.assertIn((s["metric_id"], s["period_basis"], s.get("period_start"),
                               s.get("period_end")), have,
                              f"{cid}: frozen slot {s['metric_id']} produced nothing")
            for o in r["observations"]:
                if o["availability"] != Availability.PRESENT:
                    self.assertTrue(o["missing_reason"],
                                    f"{cid}: {o['metric_id']} is absent without a reason")

    def test_recording_provenance_never_requires_the_credential(self):
        """A per-filing api_key() lookup turns a transient read of the shared
        .env into a spurious entity failure. Once the shared tables are loaded,
        the whole adapter contract must run without touching the key at all."""
        d549._tables(CTX)                       # tables resolved: no network left to do
        real = d549.api_key
        calls = []

        def exploding(*a, **k):
            calls.append(a)
            raise FetchError("<FERC_API_KEY>", "FERC_API_KEY is not set (simulated)")

        d549.api_key = exploding
        try:
            ctx = Ctx()
            e = entity("C000435")
            filings = d549.retrieve(ctx, e, year_from=2024, year_to=2026)
            expected = d549.freeze_expected(ctx, e, filings, e["assets"])
            obs, _edges = d549.canonicalise(ctx, e, filings, expected)
        finally:
            d549.api_key = real
        self.assertEqual(calls, [], "the credential was read on a non-network path")
        self.assertTrue(filings)
        self.assertTrue(obs)
        for f in filings:
            self.assertIn("REDACTED", f["source_url"])

    def test_semantic_gate_and_indeterminacy_are_never_confused(self):
        """The one invariant that keeps the two open questions apart.

        A grain gate means "retrieved, complete, MEANING unresolved"; the 0-fill
        gate means "we cannot tell what the source SAYS". Collapsing them tells
        the reader the wrong story about which question is open, so the mapping
        is exact in both directions and is asserted here rather than trusted.
        """
        for cid in ("C000434", "C000826", "C000588", "C010853", "C010084", "C001422"):
            if cid not in ENTITY:
                ENTITY[cid] = cid
            for o in run(cid)["observations"]:
                blocked = o["validation"] == Validation.BLOCKED_AMBIGUITY
                gated = o["availability"] == Availability.INTERPRETATION_BLOCKED
                self.assertEqual(
                    blocked, gated,
                    f"{cid} {o['metric_id']} {o['period_label']}: "
                    f"validation={o['validation']} availability={o['availability']}")
                if gated:
                    self.assertIsNone(o["value_num"])
                    self.assertIn("grain", o["missing_reason"].lower())
                    self.assertNotIn("0-fills", o["missing_reason"],
                                     "a 0-fill indeterminacy is not a semantic gate")
                if o["availability"] == Availability.UNVERIFIED_AVAILABILITY:
                    self.assertNotEqual(o["validation"], Validation.BLOCKED_AMBIGUITY)

    def test_a_semantic_gate_is_neither_a_defect_nor_a_usable_value(self):
        from ferclib.status import CoverageOutcome, coverage_outcome
        out = coverage_outcome(Availability.INTERPRETATION_BLOCKED,
                               Validation.BLOCKED_AMBIGUITY)
        self.assertEqual(out, CoverageOutcome.INTERPRETATION_BLOCKED)
        self.assertNotIn(out, CoverageOutcome.DEFECT,
                         "a semantic gate is not unfinished engineering")
        self.assertNotIn(out, CoverageOutcome.USABLE,
                         "a semantic gate must not count as a usable value")
        self.assertIn(Availability.INTERPRETATION_BLOCKED, Availability.SEMANTIC_GATE)
        self.assertNotIn(Availability.INTERPRETATION_BLOCKED, Availability.OUR_GAP)
        self.assertNotIn(Availability.INTERPRETATION_BLOCKED, Availability.POPULATED)

    def test_no_slot_is_labelled_as_our_unfinished_engineering(self):
        for cid in ("C000434", "C000826", "C001422"):
            bad = [o["metric_id"] for o in run(cid)["observations"]
                   if o["availability"] == Availability.NOT_IMPLEMENTED]
            self.assertEqual(bad, [], f"{cid}: unimplemented metrics {bad}")

    def test_derived_observations_carry_lineage(self):
        r = run("C000826")
        by_obs = {}
        for e in r["edges"]:
            by_obs.setdefault(e["observation_id"], []).append(e)
        for o in r["observations"]:
            if o["method"] == "derived" and o["availability"] == Availability.PRESENT:
                self.assertIn(o["observation_id"], by_obs,
                              f"{o['metric_id']} {o['period_label']} is derived with no lineage")
        # a share must carry its denominator
        share = pick(r["observations"], "i311_firm_share", 2024, "Q1")[0]
        roles = {e["input_role"] for e in by_obs[share["observation_id"]]}
        self.assertIn("denominator", roles)
        self.assertIn("numerator", roles)

    def test_observation_ids_are_deterministic(self):
        e = entity("C000434")
        ctx = Ctx()
        f = d549.retrieve(ctx, e, year_from=2024, year_to=2026)
        exp = d549.freeze_expected(ctx, e, f, e["assets"])
        a, _ = d549.canonicalise(ctx, e, f, exp)
        b, _ = d549.canonicalise(ctx, e, f, exp)
        self.assertEqual([x["observation_id"] for x in a], [x["observation_id"] for x in b])
        self.assertEqual([x["value_text"] for x in a], [x["value_text"] for x in b])
        self.assertEqual(len({x["observation_id"] for x in a}), len(a),
                         "observation ids collided within one entity")

    def test_period_grain_is_respected(self):
        r = run("C000826")
        for o in r["observations"]:
            if o["period_basis"] == periods.QUARTER:
                self.assertEqual(
                    (o["period_start"], o["period_end"]),
                    periods.quarter_interval(o["reporting_year"], o["reporting_period"]))
            elif o["period_basis"] == periods.ANNUAL:
                self.assertEqual((o["period_start"], o["period_end"]),
                                 periods.annual_interval(o["reporting_year"]))

    def test_resubmissions_are_deduped_on_submission_date(self):
        """Occurrence history is retained; canonicality follows Submission_Date."""
        r = run("C000434")
        by_period = {}
        for f in r["filings"]:
            by_period.setdefault((f["reporting_year"], f["reporting_period"]), []).append(f)
        for k, group in by_period.items():
            canon = [f for f in group if f["is_canonical"]]
            self.assertEqual(len(canon), 1, f"{k}: exactly one canonical filing expected")
            latest = max(group, key=lambda f: (f["submitted_on"] or "", f["filing_id"]))
            self.assertEqual(canon[0]["filing_id"], latest["filing_id"])
        resub = [f for f in r["filings"] if f["acceptance_status"] == "Resubmission"]
        self.assertTrue(resub, "expected at least one resubmission in 2024-2026")
        for f in resub:
            self.assertTrue(f["is_canonical"],
                            "a resubmission REPLACES the prior filing and is the current record")


if __name__ == "__main__":
    unittest.main(verbosity=2)
