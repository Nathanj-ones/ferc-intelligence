#!/usr/bin/env python3
"""Focused regressions for the industry-wide Oil Pipeline Index boundary."""
from __future__ import annotations

import collections
import pathlib
import sys
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from adapters import oil_index                                      # noqa: E402
from ferclib import coverage                                        # noqa: E402
from ferclib.applicability import EvidenceKind, SourceHealth        # noqa: E402


RUN_ID = "OIL-INSTRUMENT-TEST-RUN"
BUILT_AT = "2026-09-09T12:34:56+00:00"


class CaptureStaging:
    def __init__(self):
        self.run_id = RUN_ID
        self.filings = []
        self.facts = []

    def write_filing_bundle(self, filing, *, facts=None, **_kwargs):
        self.filings.append(dict(filing))
        self.facts.extend(dict(row) for row in (facts or ()))


class CaptureContext:
    def __init__(self):
        self.staging = CaptureStaging()
        self.args = types.SimpleNamespace(built_at=BUILT_AT)


class InstrumentIdentityAndSlots(unittest.TestCase):
    def test_global_identity_has_separate_database_and_runner_shapes(self):
        database_columns = {
            "entity_key", "cid", "local_key", "legal_name", "parent", "ticker",
            "jurisdiction", "note",
        }
        self.assertEqual(set(oil_index.INDUSTRY_ENTITY), database_columns)
        self.assertEqual(oil_index.INDUSTRY_ENTITY["entity_key"],
                         oil_index.INDUSTRY_KEY)
        self.assertIsNone(oil_index.INDUSTRY_ENTITY["cid"])
        self.assertEqual(oil_index.GLOBAL_RUN_ENTITY["entity_key"],
                         oil_index.INDUSTRY_KEY)
        self.assertEqual(oil_index.GLOBAL_RUN_ENTITY["template"], "liquids")
        self.assertEqual(oil_index.GLOBAL_RUN_ENTITY["assets"], [])

    def test_complete_expected_history_is_exactly_30_factors_and_30_changes(self):
        rows = oil_index.expected_slots(RUN_ID, BUILT_AT)
        counts = collections.Counter(row["metric_id"] for row in rows)
        self.assertEqual(counts, {
            oil_index.METRIC: 30,
            oil_index.METRIC_CHANGE: 30,
        })
        self.assertEqual(len(rows), 60)
        self.assertEqual(len({row["slot_id"] for row in rows}), 60)
        self.assertTrue(all(set(row) == set(coverage.EXPECTED_COLUMNS)
                            for row in rows))
        self.assertEqual({row["entity_key"] for row in rows},
                         {oil_index.INDUSTRY_KEY})
        self.assertEqual({row["source_regime"] for row in rows},
                         {oil_index.REGIME})
        self.assertEqual({row["period_basis"] for row in rows}, {"interval"})
        self.assertEqual({row["requirement"] for row in rows},
                         {coverage.CONDITIONAL})

    def test_slots_are_pure_deterministic_and_carry_honest_due_metadata(self):
        first = oil_index.expected_slots(RUN_ID, BUILT_AT)
        second = oil_index.expected_slots(RUN_ID, BUILT_AT)
        self.assertEqual(first, second)
        self.assertEqual({row["frozen_at"] for row in first}, {BUILT_AT})
        self.assertEqual({row["frozen_run_id"] for row in first}, {RUN_ID})
        self.assertTrue(all(row["due_date"] is None for row in first),
                        "a publication occurrence is not a carrier filing deadline")
        self.assertEqual({row["slot_state"] for row in first},
                         {coverage.SLOT_OPEN})
        self.assertEqual({row["obligation_evidence_kind"] for row in first},
                         {EvidenceKind.FILED_OCCURRENCE})
        self.assertEqual({row["source_health"] for row in first},
                         {SourceHealth.OK})
        self.assertEqual({row["denominator_origin"] for row in first},
                         {"regulatory_instrument_history"})
        with self.assertRaises(ValueError):
            oil_index.expected_slots("", BUILT_AT)
        with self.assertRaises(ValueError):
            oil_index.expected_slots(RUN_ID, "")

    def test_every_slot_names_its_exact_current_interval_and_source_fact(self):
        rows = {(row["period_start"], row["metric_id"]): row
                for row in oil_index.expected_slots(RUN_ID, BUILT_AT)}
        current = [oil_index.current_record(start) for start in
                   sorted({record["interval_start"]
                           for record in oil_index.INDEX_TABLE})]
        for record in current:
            factor = rows[(record["interval_start"], oil_index.METRIC)]
            self.assertEqual(factor["period_end"], record["interval_end"])
            self.assertEqual(factor["reporting_year"],
                             int(record["interval_start"][:4]))
            self.assertIn(record["fr_document"], factor["requirement_evidence"])
            self.assertIn(oil_index.fact_id(record, oil_index.METRIC),
                          factor["requirement_evidence"])
            self.assertIn(f"FR-{record['fr_document']}@{record['published']}",
                          factor["applicability_version"])
            change_key = (record["interval_start"], oil_index.METRIC_CHANGE)
            self.assertIn(change_key, rows,
                          "every captured notice explicitly publishes a change")
            self.assertIn(oil_index.fact_id(record, oil_index.METRIC_CHANGE),
                          rows[change_key]["requirement_evidence"])

    def test_current_2021_source_stands_while_vacated_revision_is_lineage(self):
        rows = {(row["period_start"], row["metric_id"]): row
                for row in oil_index.expected_slots(RUN_ID, BUILT_AT)}
        factor = rows[("2021-07-01", oil_index.METRIC)]
        self.assertIn("2021-10860", factor["requirement_evidence"])
        self.assertNotIn("2022-01521", factor["requirement_evidence"])
        change = rows[("2021-07-01", oil_index.METRIC_CHANGE)]
        self.assertIn("2021-10860", change["requirement_evidence"])
        self.assertNotIn("2022-01521", change["requirement_evidence"])

        observations, edges = oil_index.observations_for()
        actual = [row for row in observations
                  if row["period_start"] == "2021-07-01"]
        self.assertEqual([(row["metric_id"], row["filing_id"]) for row in actual],
                         [(oil_index.METRIC, "2021-10860"),
                          (oil_index.METRIC_CHANGE, "2021-10860")])
        self.assertEqual(len(edges), 2)
        self.assertEqual({edge["input_filing_id"] for edge in edges},
                         {"2022-01521"})
        self.assertEqual({edge["input_source_fact_id"] for edge in edges}, {
            f"2022-01521:{oil_index.METRIC}",
            f"2022-01521:{oil_index.METRIC_CHANGE}",
        })
        prior_facts = {(fact["filing_id"], fact["source_fact_id"])
                       for fact in oil_index.all_source_facts()}
        for edge in edges:
            self.assertIn((edge["input_filing_id"],
                           edge["input_source_fact_id"]), prior_facts)

    def test_expected_and_actual_full_history_match_at_the_complete_grain(self):
        expected = oil_index.expected_slots(RUN_ID, BUILT_AT)
        observations, edges = oil_index.observations_for(run_id=RUN_ID)
        canonical = {(row["source_system"], row["filing_id"]):
                     bool(row["is_canonical"])
                     for row in oil_index.source_records()}
        lineage = collections.defaultdict(list)
        for edge in edges:
            lineage[edge["observation_id"]].append(edge)
        measured = coverage.measure(
            expected, observations, RUN_ID,
            canonical=canonical, lineage=lineage)
        self.assertEqual(len(measured), 60)
        self.assertEqual(sum(row["populated"] for row in measured), 60)
        self.assertEqual(sum(row["source_matched"] for row in measured), 60)
        self.assertEqual(sum(row["validated"] for row in measured), 52)
        self.assertEqual(sum(row["in_review"] for row in measured), 8)
        self.assertEqual(sum(row["candidates_refused"] for row in measured), 0)


class WindowSafety(unittest.TestCase):
    def test_real_empty_retrieval_emits_no_uncited_history(self):
        ctx = CaptureContext()
        expected = oil_index.freeze_expected(
            ctx, oil_index.GLOBAL_RUN_ENTITY, [], [])
        observations, edges = oil_index.canonicalise(
            ctx, oil_index.GLOBAL_RUN_ENTITY, [], expected)
        self.assertEqual(expected, [])
        self.assertEqual(observations, [])
        self.assertEqual(edges, [])

    def test_full_history_retrieval_has_31_occurrences_and_60_observations(self):
        ctx = CaptureContext()
        filings = oil_index.retrieve(
            ctx, oil_index.GLOBAL_RUN_ENTITY, year_from=1997, year_to=2026)
        expected = oil_index.freeze_expected(
            ctx, oil_index.GLOBAL_RUN_ENTITY, filings, [])
        observations, edges = oil_index.canonicalise(
            ctx, oil_index.GLOBAL_RUN_ENTITY, filings, expected)
        self.assertEqual(len(filings), 31)
        self.assertEqual(len(ctx.staging.filings), 31)
        self.assertEqual(len(observations), 60)
        self.assertEqual(len(expected), 60)
        self.assertEqual(len(edges), 2)

    def test_narrow_window_has_no_outside_rows_or_dangling_references(self):
        ctx = CaptureContext()
        filings = oil_index.retrieve(
            ctx, oil_index.GLOBAL_RUN_ENTITY, year_from=2024, year_to=2024)
        expected = oil_index.freeze_expected(
            ctx, oil_index.GLOBAL_RUN_ENTITY, filings, [])
        observations, edges = oil_index.canonicalise(
            ctx, oil_index.GLOBAL_RUN_ENTITY, filings, expected)

        self.assertEqual([row["filing_id"] for row in filings], ["2024-11147"])
        self.assertEqual({row["reporting_year"] for row in observations}, {2024})
        self.assertEqual({row["reporting_year"] for row in expected}, {2024})
        self.assertEqual(len(observations), 2)  # factor and explicit index change
        self.assertEqual(edges, [])

        facts = {(row["source_system"], row["filing_id"], row["source_fact_id"])
                 for row in ctx.staging.facts}
        for row in observations:
            self.assertIn((row["source_system"], row["filing_id"],
                           row["source_fact_id"]), facts)
        for edge in edges:
            self.assertIn((edge["input_source_system"], edge["input_filing_id"],
                           edge["input_source_fact_id"]), facts)

    def test_revision_family_is_all_or_nothing(self):
        with self.assertRaisesRegex(RuntimeError, "incomplete source-version family"):
            oil_index.expected_slots(
                RUN_ID, BUILT_AT, filing_ids=["2021-10860"])
        with self.assertRaisesRegex(RuntimeError, "incomplete source-version family"):
            oil_index.observations_for(filing_ids=["2022-01521"])

        ctx = CaptureContext()
        filings = oil_index.retrieve(
            ctx, oil_index.GLOBAL_RUN_ENTITY, year_from=2021, year_to=2021)
        self.assertEqual({row["filing_id"] for row in filings},
                         {"2021-10860", "2022-01521"})
        observations, edges = oil_index.canonicalise(
            ctx, oil_index.GLOBAL_RUN_ENTITY, filings, [])
        self.assertEqual([(row["metric_id"], row["filing_id"])
                          for row in observations],
                         [(oil_index.METRIC, "2021-10860"),
                          (oil_index.METRIC_CHANGE, "2021-10860")])
        self.assertEqual(len(edges), 2)

    def test_carrier_routing_is_refused_before_retrieval(self):
        ctx = CaptureContext()
        with self.assertRaisesRegex(AssertionError, "industry-wide"):
            oil_index.retrieve(
                ctx, {"entity_key": "C001049", "assets": []},
                year_from=2024, year_to=2024)
        self.assertEqual(ctx.staging.filings, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
