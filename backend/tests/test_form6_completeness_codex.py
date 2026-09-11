"""Form 6 full-versus-reduced schedule-census regressions.

All source rows are synthetic.  They exercise the liquids adapter's production
classifier; no database, cache or implementation-generated golden answer is
used.
"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from adapters import liquids_xbrl
from ferclib import registry
from ferclib.status import Availability, Method, Validation


SENTINELS = ("110", "114", "120", "212", "600")


def taxonomy(*, complete=True):
    schedules = [(p, f"sched-{p}") for p in ("1", "2", "110", "114", "120",
                                                   "212", "301", "600", "700")]
    concepts = {
        "FormType": ("1", "sched-1"),
        "ScheduleExemption": ("2", "sched-2"),
        "ScheduleWaiver": ("2", "sched-2"),
        "BalanceSheetAsset": ("110", "sched-110"),
        "IncomeStatementRevenue": ("114", "sched-114"),
        "CashFlowItem": ("120", "sched-120"),
        "CarrierProperty": ("212", "sched-212"),
        "InterstateRevenue301": ("301", "sched-301"),
        "MilesOperated": ("600", "sched-600"),
        "InterstateOperatingRevenues": ("700", "sched-700"),
    }
    return types.SimpleNamespace(
        complete=complete, schedules=schedules, concepts=concepts,
        entry_point_url="https://eCollection.ferc.gov/SYNTHETIC/form-6.xsd",
        sources=[{
            "artefact": "entry_point",
            "url": "https://eCollection.ferc.gov/SYNTHETIC/form-6.xsd",
            "content_hash": "a" * 64,
            "retrieved": 1,
        }])


def fact(concept, value="1", *, order=1, fact_id=None, nil=0):
    return {
        "source_system": "eCollection_XBRL",
        "filing_id": "SYNTHETIC-F6-2025",
        "source_fact_id": fact_id or f"SYNTHETIC-{concept}",
        "document_order": order,
        "concept_local": concept,
        "context_id": "SYNTHETIC-ANNUAL-CONTEXT",
        "value_as_filed": value,
        "is_nil": nil,
        "unit_text": "",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
        "instant": "",
    }


class ScheduleCensusUnitTests(unittest.TestCase):
    def test_full_requires_actual_full_only_schedule_evidence(self):
        census = liquids_xbrl._schedule_census([
            fact("FormType", "6", order=1),
            fact("InterstateOperatingRevenues", "0", order=2),
            fact("BalanceSheetAsset", "100", order=3),
        ], taxonomy())
        self.assertEqual(census["status"], "full")
        self.assertEqual(census["validation"], Validation.PASS)
        self.assertEqual(census["full_only_pages"], ["110"])
        self.assertIn("Page 700 was not used as proof", census["reason"])
        # Zero is a legitimate reported Page 700 fact, not an empty value.
        self.assertEqual(census["populated_schedule_counts"]["700"], 1)

    def test_complete_census_distinguishes_reduced_schedule_filing(self):
        census = liquids_xbrl._schedule_census([
            fact("FormType", "6"),
            fact("InterstateRevenue301", "400000", order=2),
            fact("InterstateOperatingRevenues", "400000", order=3),
        ], taxonomy())
        self.assertEqual(census["status"], "reduced_schedule")
        self.assertEqual(census["validation"], Validation.PASS)
        self.assertTrue(census["taxonomy_complete_for_rule"])
        self.assertEqual(census["full_only_pages"], [])

    def test_negative_page_700_alone_is_never_called_full(self):
        census = liquids_xbrl._schedule_census([
            fact("InterstateOperatingRevenues", "400000"),
        ], taxonomy())
        self.assertEqual(census["status"], "unresolved")
        self.assertEqual(census["validation"], Validation.BLOCKED_AMBIGUITY)
        self.assertIn("Page 700 alone is insufficient", census["reason"])

    def test_negative_incomplete_taxonomy_cannot_prove_reduced_by_absence(self):
        census = liquids_xbrl._schedule_census([
            fact("FormType", "6"),
            fact("InterstateOperatingRevenues", "400000", order=2),
        ], taxonomy(complete=False))
        self.assertEqual(census["status"], "unresolved")
        self.assertFalse(census["taxonomy_complete_for_rule"])
        self.assertIn("taxonomy schedule census is incomplete", census["reason"])

    def test_reported_exemption_is_positive_reduced_signal(self):
        census = liquids_xbrl._schedule_census([
            fact("ScheduleExemption", "X"),
            fact("InterstateOperatingRevenues", "350000", order=2),
        ], taxonomy(complete=False))
        self.assertEqual(census["status"], "reduced_schedule")
        self.assertEqual(census["reduced_signals"], ["ScheduleExemption"])

    def test_negative_conflicting_reduced_and_full_evidence_is_gated(self):
        census = liquids_xbrl._schedule_census([
            fact("FormType", "6"),
            fact("ScheduleWaiver", "X", order=2),
            fact("CashFlowItem", "10", order=3),
        ], taxonomy())
        self.assertEqual(census["status"], "unresolved")
        self.assertEqual(census["validation"], Validation.BLOCKED_AMBIGUITY)
        self.assertIn("contradictory filing evidence", census["reason"])

    def test_nil_and_empty_facts_do_not_populate_schedules(self):
        census = liquids_xbrl._schedule_census([
            fact("FormType", "6"),
            fact("BalanceSheetAsset", "", order=2),
            fact("CashFlowItem", "999", order=3, nil=1),
            fact("InterstateOperatingRevenues", "1", order=4),
        ], taxonomy())
        self.assertEqual(census["status"], "reduced_schedule")
        self.assertEqual(census["full_only_pages"], [])


class _Staging:
    def __init__(self, facts):
        self.facts = facts

    def query(self, sql, params=()):
        if "FROM source_facts" in sql:
            return list(self.facts)
        if "FROM filings" in sql:
            return [{
                "taxonomy_version": "2025-04-01",
                "schema_ref": "https://eCollection.ferc.gov/SYNTHETIC/form-6.xsd",
                "version_status": "revised",
                "data_origin": "native_xbrl",
            }]
        raise AssertionError(f"unexpected query: {sql}")


def slot():
    return {
        "metric_id": "liq_form6_filing_completeness",
        "source_regime": "Form 6",
        "period_basis": "annual_observation",
        "period_start": None,
        "period_end": None,
        "instant_date": "2025-12-31",
        "reporting_year": 2025,
        "reporting_period": "Q4",
        "requirement_evidence": "SYNTHETIC Form 6 obligation",
    }


class CompletenessObservationTests(unittest.TestCase):
    def setUp(self):
        self.taxonomy = taxonomy()
        self.facts = [
            fact("FormType", "6", order=1),
            fact("InterstateOperatingRevenues", "123", order=2),
            fact("MilesOperated", "500", order=3),
        ]
        self.ctx = types.SimpleNamespace(
            staging=_Staging(self.facts),
            applicability=types.SimpleNamespace(build=lambda *_a, **_k: self.taxonomy),
        )
        self.entity = {"entity_key": "SYNTHETIC-CID", "legal_name": "Synthetic Carrier"}
        self.filing = {
            "filing_id": "SYNTHETIC-F6-2025", "form": "Form 6",
            "reporting_year": 2025, "reporting_period": "Q4", "is_canonical": 1,
        }

    def test_observation_preserves_occurrence_taxonomy_scope_and_fact_lineage(self):
        metric = registry.BY_ID["liq_form6_filing_completeness"]
        obs, edges = liquids_xbrl._completeness_observation(
            self.ctx, self.entity, self.filing, slot(), metric)
        self.assertEqual(obs["value_text"], "full")
        self.assertEqual(obs["availability"], Availability.PRESENT)
        self.assertEqual(obs["method"], Method.DERIVED)
        self.assertEqual(obs["version_status"], "revised")
        self.assertEqual(obs["source_system"], "eCollection_XBRL")
        self.assertEqual(obs["filing_id"], "SYNTHETIC-F6-2025")
        self.assertIsNone(obs["source_fact_id"])
        self.assertEqual(obs["taxonomy_version"], "2025-04-01")
        self.assertIn("Synthetic Carrier (SYNTHETIC-CID)", obs["scope"])
        self.assertIn("filing occurrence SYNTHETIC-F6-2025", obs["scope"])
        self.assertNotEqual(obs["scope"], obs["scope_rule"])
        self.assertIn("taxonomy_source_set_sha256=", obs["notes"])
        self.assertIn("schedule_census=", obs["notes"])
        self.assertEqual({e["input_filing_id"] for e in edges}, {"SYNTHETIC-F6-2025"})
        self.assertEqual({e["input_source_fact_id"] for e in edges
                          if e["input_source_fact_id"]},
                         {"SYNTHETIC-FormType", "SYNTHETIC-InterstateOperatingRevenues",
                          "SYNTHETIC-MilesOperated"})
        self.assertTrue(all(e["observation_id"] == obs["observation_id"] for e in edges))
        populations = obs["_populations"]
        self.assertEqual(len(populations), 1)
        self.assertEqual(populations[0]["candidate_count"], 3)
        self.assertEqual(populations[0]["row_count"], 3)
        self.assertEqual(populations[0]["filing_ids"], '["SYNTHETIC-F6-2025"]')
        self.assertEqual(edges[0]["input_role"], "population")
        self.assertEqual(edges[0]["input_population_id"], populations[0]["population_id"])

    def test_adapter_replaces_generic_failure_but_preserves_other_output(self):
        generic_bad = {
            "observation_id": "SYNTHETIC-GENERIC-BAD",
            "metric_id": "liq_form6_filing_completeness",
            "reporting_year": 2025,
            "reporting_period": "Q4",
        }
        unrelated = {"observation_id": "SYNTHETIC-OTHER", "metric_id": "liq_net_income"}
        generic_edges = [{"observation_id": "SYNTHETIC-OTHER", "input_order": 1}]
        with mock.patch.object(liquids_xbrl.xbrl_adapter, "canonicalise",
                               return_value=([generic_bad, unrelated], generic_edges)):
            observations, edges = liquids_xbrl.canonicalise(
                self.ctx, self.entity, [self.filing], [slot()])
        by_metric = {o["metric_id"]: o for o in observations}
        self.assertEqual(by_metric["liq_form6_filing_completeness"]["value_text"], "full")
        self.assertIs(by_metric["liq_net_income"], unrelated)
        self.assertNotIn("SYNTHETIC-GENERIC-BAD",
                         {e["observation_id"] for e in edges})
        self.assertIn("SYNTHETIC-OTHER", {e["observation_id"] for e in edges})


if __name__ == "__main__":
    unittest.main()
