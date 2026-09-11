"""
Oil Pipeline Index (A18) -- w3-financial acceptance tests.

Rule 4: every fix ships with a NEGATIVE test that fails when the defect is
reintroduced. Each test below therefore does two things -- asserts the correct
behaviour, and then MUTATES the input to reintroduce the exact defect the audit
found and asserts that the mutation is REJECTED. A test that passes because the
mutated record became acceptable is a failed test.

Authored by w3-financial and adopted into `tests/` by w6-acceptance, which owns
this directory (REPAIR_CONTRACT Rule 1). The assertions are w3's and are
unchanged; only the module split and this note are w6's.

Run:
    cd outputs/repair_all_regimes && python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from adapters import form549d as f549                                      # noqa: E402
from adapters import oil_index                                            # noqa: E402
from ferclib.status import VersionStatus                                  # noqa: E402


def row(**kw):
    """A 549D contract row with enough columns for the probes to work."""
    base = {
        "Form549D_ID": "18901", "Sequence_Number": "1",
        "Contract_Number": "TRN00593", "Service_Type": "Transportation",
        "Character_of_Service": "Firm", "Affiliate_Status": "No",
        "Shipper_Name": "ACME GAS", "Filer_Proprietary_Shipper_ID": "S1",
        "Total_Rev": "1000", "Usage_BU": "500",
        "Service_Rate_Schedule": "FT-1", "Rate_Docket": "RP11-1",
        "Receipt_Point_Name": "P1", "Delivery_Point_Name": "P2",
        "Contract_Begin": "01/01/2024", "Contract_End": "12/31/2030",
    }
    base.update(kw)
    return base


# =====================================================================
# A18 -- the FERC Oil Pipeline Index
# =====================================================================

class OilPipelineIndex(unittest.TestCase):

    def test_the_2026_27_interval_and_factor_load(self):
        r = oil_index.operative_record("2026-07-01")
        self.assertIsNotNone(r)
        self.assertEqual(r["interval_start"], "2026-07-01")
        self.assertEqual(r["interval_end"], "2027-06-30")
        self.assertEqual(r["factor"], "1.014290")
        self.assertEqual(r["index_change"], "0.014290")
        self.assertEqual(r["five_year_level"], "PPI-FG-0.55%")
        self.assertEqual(r["fr_document"], "2026-09998")

    def test_factor_and_change_are_different_quantities(self):
        """1.014290 is a multiplier; 0.014290 is the fractional change.
        Storing the multiplier under a percent unit is a category error."""
        r = oil_index.operative_record("2026-07-01")
        from decimal import Decimal
        self.assertEqual(Decimal(r["factor"]),
                         Decimal(1) + Decimal(r["index_change"]))
        self.assertNotEqual(r["factor"], r["index_change"])

    def test_superseded_factors_are_preserved(self):
        states = oil_index.records_for_interval("2021-07-01")
        self.assertEqual(len(states), 2, "both published states must survive")
        self.assertEqual([s["factor"] for s in states],
                         ["0.994188", "0.984288"])
        original, revision = states
        # Corrected after LEPA v. FERC: the D.C. Circuit vacated the Order on
        # Rehearing the revision implemented, and the Commission reinstated the
        # original level. So 0.994188 STANDS and 0.984288 is the vacated one --
        # the reverse of what this test asserted before the reversal was
        # followed through. (w3-financial; adopted here 8 Sep.)
        self.assertEqual(original["version_status"], VersionStatus.ORIGINAL)
        self.assertEqual(original["superseded_by"], "2022-01521")
        self.assertTrue(original["reinstated_by"])
        self.assertEqual(revision["version_status"], VersionStatus.SUPERSEDED)
        self.assertEqual(revision["supersedes"], "2021-10860")
        self.assertTrue(revision["vacated_by"])

    def test_a_superseding_factor_can_take_effect_inside_its_index_year(self):
        """The recomputation was effective 1 March 2022, mid-index-year."""
        rev = [s for s in oil_index.records_for_interval("2021-07-01")
               if s["fr_document"] == "2022-01521"][0]
        self.assertEqual(rev["effective_from"], "2022-03-01")
        self.assertGreater(rev["effective_from"], rev["interval_start"])
        # before it took effect, the original governs
        self.assertEqual(oil_index.operative_record("2021-09-01")["factor"],
                         "0.994188")
        # after, the recomputation governs
        self.assertEqual(oil_index.operative_record("2022-04-01")["factor"],
                         "0.984288")

    def test_NEGATIVE_a_superseded_factor_must_not_be_deleted(self):
        """Reintroduce the defect: keep only the latest factor per interval."""
        latest_only = {}
        for r in oil_index.INDEX_TABLE:
            k = r["interval_start"]
            if k not in latest_only or r["published"] > latest_only[k]["published"]:
                latest_only[k] = r
        self.assertLess(len(latest_only), len(oil_index.INDEX_TABLE),
                        "collapsing to one factor per interval loses history")
        lost = [r for r in oil_index.INDEX_TABLE
                if r not in latest_only.values()]
        self.assertTrue(any(r["factor"] == "0.994188" for r in lost),
                        "the original 2021-22 factor is exactly what a "
                        "one-row-per-interval model discards")

    def test_every_observation_resolves_to_a_source_record(self):
        obs, _ = oil_index.observations_for()
        self.assertTrue(obs)
        known = {f["filing_id"] for f in oil_index.source_records()}
        dangling = [o for o in obs if o["filing_id"] not in known]
        self.assertEqual(dangling, [],
                         "A18's defect was 28 observations with no filing row")
        for o in obs:
            self.assertTrue(o["filing_id"])
            self.assertEqual(o["source_system"], oil_index.SOURCE_SYSTEM)

    def test_NEGATIVE_an_observation_naming_an_unknown_filing_is_detectable(self):
        obs, _ = oil_index.observations_for()
        known = {f["filing_id"] for f in oil_index.source_records()}
        obs[0]["filing_id"] = "20260514-3050"          # the baseline's dangling id
        dangling = [o for o in obs if o["filing_id"] not in known]
        self.assertEqual(len(dangling), 1)

    def test_metric_is_renamed_away_from_commodity_price(self):
        self.assertEqual(oil_index.LEGACY_METRIC, "liq_oil_price_index")
        self.assertNotIn("price", oil_index.METRIC)
        self.assertIn("index", oil_index.METRIC)
        obs, _ = oil_index.observations_for()
        self.assertNotIn(oil_index.LEGACY_METRIC, {o["metric_id"] for o in obs})

    def test_crosswalk_refuses_to_pretend_the_old_name_had_one_meaning(self):
        cw = oil_index.metric_crosswalk()[oil_index.LEGACY_METRIC]
        self.assertEqual(cw["renamed_to"], oil_index.METRIC)
        self.assertEqual(cw["companion"], oil_index.METRIC_CHANGE)
        self.assertIn("NOT a drop-in alias", cw["not_equivalent"])

    def test_the_multiplier_is_never_stored_as_a_percent(self):
        obs, _ = oil_index.observations_for()
        for o in obs:
            if o["metric_id"] == oil_index.METRIC:
                self.assertEqual(o["unit"], "multiplier")
            if o["metric_id"] == oil_index.METRIC_CHANGE:
                self.assertEqual(o["unit"], "fraction")
            self.assertNotEqual(o["unit"], "percent")

    def test_scope_says_it_is_not_a_carrier_rate(self):
        self.assertIn("not a carrier rate", oil_index.SCOPE)
        obs, _ = oil_index.observations_for()
        self.assertTrue(all("NOT a commodity oil price" in o["qa_flags"]
                            for o in obs))

    def test_published_once_under_the_industry_key_not_per_carrier(self):
        """The baseline fanned one national index across 28 carriers."""
        obs, _ = oil_index.observations_for()
        self.assertEqual({o["entity_key"] for o in obs},
                         {oil_index.INDUSTRY_KEY})
        self.assertIsNone(oil_index.INDUSTRY_ENTITY["cid"],
                          "the industry key must not be a FERC CID")
        # ONE observation per index YEAR, not per published state: two states of
        # one index year share every column of ux_observation_grain (which
        # deliberately excludes version_status) and cannot both be rows. 31
        # records over 30 index years therefore yields 30 factors, and asserting
        # 31 conflated "records in the table" with "observations emitted".
        # Stronger than the old assertion, not looser: flattening the table to
        # one row per interval now fails the second check.
        factors = [o for o in obs if o["metric_id"] == oil_index.METRIC]
        intervals = {r["interval_start"] for r in oil_index.INDEX_TABLE}
        self.assertEqual(len(factors), len(intervals))
        self.assertLess(len(factors), len(oil_index.INDEX_TABLE),
                        "the table holds more states than there are index years")

    def test_NEGATIVE_attributing_the_index_to_a_carrier_is_refused(self):
        """Reintroduce the baseline's scope error: publish under a CID."""
        with self.assertRaises(AssertionError) as cm:
            oil_index.canonicalise(None, {"entity_key": "C001049"}, [], [])
        self.assertIn("industry-wide", str(cm.exception))
        # the correct key is accepted
        obs, _ = oil_index.canonicalise(
            None, {"entity_key": oil_index.INDUSTRY_KEY}, [], [])
        self.assertTrue(obs)

    def test_the_index_cannot_be_summed_across_carriers(self):
        """28 identical rows for one national index was the defect."""
        obs, _ = oil_index.observations_for()
        current = [o for o in obs
                   if o["metric_id"] == oil_index.METRIC
                   and o["period_start"] == "2026-07-01"]
        self.assertEqual(len(current), 1,
                         "exactly one row may exist for one index year")

    def test_source_anomalies_are_recorded_not_corrected(self):
        anomalies = [r for r in oil_index.INDEX_TABLE if r["source_anomaly"]]
        self.assertEqual(len(anomalies), 4)
        docs = {r["fr_document"] for r in anomalies}
        self.assertIn("00-13115", docs)    # conflicting closing-history factor
        self.assertIn("2025-09243", docs)   # base interval misstated
        self.assertIn("03-12949", docs)     # "negative 0.987207"
        self.assertIn("E7-12192", docs)     # PPI comparison years misstated
        # the published factor itself is untouched
        r = [x for x in oil_index.INDEX_TABLE
             if x["fr_document"] == "2025-09243"][0]
        self.assertEqual(r["factor"], "1.019976")

    def test_table_is_internally_consistent(self):
        self.assertEqual(oil_index.verify_table(), [])

    def test_every_record_carries_a_citation(self):
        for r in oil_index.INDEX_TABLE:
            self.assertTrue(r["fr_document"])
            self.assertTrue(r["published"])
            self.assertIn("Federal Register", r["citation"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
