#!/usr/bin/env python3
"""Regression controls for the occupied Oil Pipeline Index coverage defect.

These tests are database-free.  They exercise the real adapter observation and
lineage edge for the reinstated 2021-22 factor, then mutate one semantic
dimension at a time.  Rejecting every candidate therefore cannot look like a
repair.
"""
from __future__ import annotations

import copy
import unittest

from adapters import oil_index
from ferclib import coverage
from ferclib.registry import BY_ID
from ferclib.status import CoverageOutcome


RUN_ID = "SYNTHETIC-CODEX-OIL-COVERAGE"


def standing_factor() -> tuple[dict, list[dict]]:
    observations, edges = oil_index.observations_for()
    observation = next(
        row for row in observations
        if row["metric_id"] == oil_index.METRIC
        and row["period_start"] == "2021-07-01"
    )
    own_edges = [row for row in edges
                 if row["observation_id"] == observation["observation_id"]]
    return observation, own_edges


def slot_for(observation: dict) -> dict:
    metric = BY_ID[observation["metric_id"]]
    return {
        "slot_id": "SYNTHETIC-CODEX-CURRENT-OIL-SLOT",
        "entity_key": observation["entity_key"],
        "metric_id": observation["metric_id"],
        "source_regime": observation["source_regime"],
        "period_basis": observation["period_basis"],
        "period_start": observation["period_start"],
        "period_end": observation["period_end"],
        "instant_date": observation["instant_date"],
        "scope": observation["scope"],
        "unit_rule": metric.unit_rule,
        "requirement": coverage.REQUIRED,
        "requirement_evidence": "synthetic replay of the independently audited grain",
        "slot_state": coverage.SLOT_OPEN,
    }


def measured(slot: dict, observation: dict, lineage, *, canonical=None) -> dict:
    return coverage.measure(
        [slot], [observation], RUN_ID, canonical=canonical, lineage=lineage
    )[0]


class CurrentOilFactorCoverage(unittest.TestCase):
    def setUp(self):
        self.observation, self.edges = standing_factor()
        self.slot = slot_for(self.observation)
        self.assertEqual(self.observation["value_text"], "0.994188")
        self.assertEqual(self.observation["version_status"], "original")
        self.assertTrue(self.observation["source_fact_id"])
        self.assertEqual(
            [(edge["input_role"], edge["input_version_status"], edge["input_value"])
             for edge in self.edges],
            [("basis", "superseded", "0.984288")],
        )

    def test_current_direct_factor_accepts_role_aware_historical_basis(self):
        """A superseded basis preserves history; it does not replace the fact."""
        result = measured(
            self.slot,
            self.observation,
            {self.observation["observation_id"]: self.edges},
        )
        self.assertEqual((result["populated"], result["validated"]), (1, 1))
        self.assertEqual(result["observation_id"], self.observation["observation_id"])
        self.assertEqual(result["candidates_refused"], 0)

    def test_current_direct_factor_accepts_legacy_status_only_lineage(self):
        """Exercise the representation used by the production coverage command."""
        statuses = [edge["input_version_status"] for edge in self.edges]
        result = measured(
            self.slot,
            self.observation,
            {self.observation["observation_id"]: statuses},
        )
        self.assertEqual((result["populated"], result["validated"]), (1, 1))

    def test_currentness_guards_still_reject_the_observation_itself(self):
        superseded = dict(self.observation, version_status="superseded")
        result = measured(
            self.slot,
            superseded,
            {superseded["observation_id"]: self.edges},
        )
        self.assertEqual(result["populated"], 0)
        self.assertIn(coverage.Gate.SUPERSEDED, result["refusal_gates"])

        canonical = {(self.observation["source_system"],
                      str(self.observation["filing_id"])): False}
        result = measured(
            self.slot,
            self.observation,
            {self.observation["observation_id"]: self.edges},
            canonical=canonical,
        )
        self.assertEqual(result["populated"], 0)
        self.assertIn(coverage.Gate.NONCANONICAL, result["refusal_gates"])

    def test_unit_scope_and_period_guards_stay_closed(self):
        wrong_unit = dict(self.observation, unit="iso4217:USD")
        result = measured(
            self.slot,
            wrong_unit,
            {wrong_unit["observation_id"]: self.edges},
        )
        self.assertEqual(result["populated"], 0)
        self.assertEqual(result["outcome"], CoverageOutcome.SELECTOR_FAILED)
        self.assertIn(coverage.Gate.UNIT_FAMILY, result["refusal_gates"])

        for field, bad_value in (
            ("scope", "one carrier's actual tariff change"),
            ("period_end", "2023-06-30"),
        ):
            with self.subTest(field=field):
                wrong_grain = dict(self.observation, **{field: bad_value})
                result = measured(
                    self.slot,
                    wrong_grain,
                    {wrong_grain["observation_id"]: self.edges},
                )
                self.assertEqual(result["populated"], 0)
                self.assertIsNone(result["observation_id"])

    def test_only_nonoperative_direct_history_gets_the_exemption(self):
        """An operand, a derived row, or an untraced row remains refused."""
        operative = [dict(self.edges[0], input_role="addend")]
        result = measured(
            self.slot,
            self.observation,
            {self.observation["observation_id"]: operative},
        )
        self.assertEqual(result["populated"], 0)
        self.assertIn(coverage.Gate.SUPERSEDED, result["refusal_gates"])

        derived = copy.deepcopy(self.observation)
        derived.update(method="derived", source_fact_id=None)
        result = measured(
            self.slot,
            derived,
            {derived["observation_id"]: self.edges},
        )
        self.assertEqual(result["populated"], 0)
        self.assertIn(coverage.Gate.SUPERSEDED, result["refusal_gates"])

        untraced = dict(self.observation, source_fact_id=None, document_id=None)
        result = measured(
            self.slot,
            untraced,
            {untraced["observation_id"]: ["superseded"]},
        )
        self.assertEqual(result["populated"], 0)
        self.assertIn(coverage.Gate.SUPERSEDED, result["refusal_gates"])


if __name__ == "__main__":
    unittest.main()
