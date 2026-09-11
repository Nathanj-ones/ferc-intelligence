#!/usr/bin/env python3
"""Integrated routing and coverage controls for global FERC instruments."""
from __future__ import annotations

import csv
import pathlib
import tempfile
import types
import unittest

import run as pipeline
from adapters import oil_index
from ferclib.obligations import build_coverage_grid, measure_grid
from ferclib.registry import BY_ID


RUN_ID = "SYNTHETIC-OIL-GRID"
STAMP = "2026-09-09T18:02:08+00:00"


class _Universe:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ferc-oil-grid-")
        self.path = pathlib.Path(self.tmp.name) / "universe.csv"
        with self.path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=("asset_id", "entity_key", "status", "cod_group",
                            "template", "roster_forms"),
            )
            writer.writeheader()
            writer.writerow({
                "asset_id": "synthetic-carrier",
                "entity_key": "SYNTHETIC-CARRIER",
                "status": "operating",
                "cod_group": "post-cod",
                "template": "liquids",
                "roster_forms": "[]",
            })

    def close(self):
        self.tmp.cleanup()


class GlobalRouting(unittest.TestCase):
    def test_default_entity_run_and_explicit_instrument_are_distinct(self):
        self.assertIn("oil_index", pipeline.ADAPTERS)
        self.assertNotIn("oil_index", pipeline.ENTITY_ADAPTERS)
        args = types.SimpleNamespace(entity=[], template=[])
        carrier = {"entity_key": "CARRIER", "template": "liquids"}
        self.assertEqual(
            pipeline._eligible_adapter_entities(args, "oil_index", oil_index, [carrier]),
            [oil_index.GLOBAL_RUN_ENTITY],
        )

    def test_carrier_filter_cannot_fan_out_the_industry_index(self):
        carrier = {"entity_key": "CARRIER", "template": "liquids"}
        filtered = types.SimpleNamespace(entity=["CARRIER"], template=[])
        self.assertEqual(
            pipeline._eligible_adapter_entities(
                filtered, "oil_index", oil_index, [carrier]), [])
        explicit = types.SimpleNamespace(
            entity=[oil_index.INDUSTRY_KEY], template=["liquids"])
        self.assertEqual(
            pipeline._eligible_adapter_entities(explicit, "oil_index", oil_index, []),
            [oil_index.GLOBAL_RUN_ENTITY],
        )

    def test_database_seed_uses_only_the_declared_entity_contract(self):
        class Capture:
            rows = None

            def write_entities(self, rows):
                self.rows = rows

        ctx = types.SimpleNamespace(staging=Capture())
        pipeline._seed_global_adapter_entity(
            ctx, oil_index, oil_index.GLOBAL_RUN_ENTITY)
        self.assertEqual(ctx.staging.rows, [oil_index.INDUSTRY_ENTITY])
        self.assertNotIn("template", ctx.staging.rows[0])
        self.assertIsNone(ctx.staging.rows[0]["cid"])


class IndependentInstrumentGrid(unittest.TestCase):
    def setUp(self):
        self.universe = _Universe()
        self.metrics = (
            BY_ID[oil_index.METRIC],
            BY_ID[oil_index.METRIC_CHANGE],
        )

    def tearDown(self):
        self.universe.close()

    def _build(self, slots):
        return build_coverage_grid(
            universe_path=self.universe.path,
            year_from=2024,
            year_to=2026,
            as_of="2026-09-07",
            built_at=STAMP,
            run_id=RUN_ID,
            metrics=self.metrics,
            instrument_slots=slots,
        )

    def test_complete_instrument_history_is_part_of_the_measured_grid(self):
        slots = oil_index.expected_slots(RUN_ID, STAMP)
        grid = self._build(slots)
        self.assertEqual(len(grid.slots), 60)
        self.assertEqual(grid.manifest()["instrument_slots"], 60)
        self.assertEqual(grid.input_digest, self._build(list(reversed(slots))).input_digest)

        observations, edges = oil_index.observations_for(run_id=RUN_ID)
        canonical = {(row["source_system"], row["filing_id"]):
                     bool(row["is_canonical"])
                     for row in oil_index.source_records()}
        lineage = {}
        for edge in edges:
            lineage.setdefault(edge["observation_id"], []).append(edge)
        measured = measure_grid(
            grid, observations, canonical=canonical, lineage=lineage)
        self.assertEqual(len(measured.rows), 60)
        self.assertEqual(sum(row["populated"] for row in measured.rows), 60)
        self.assertEqual(sum(row["validated"] for row in measured.rows), 52)
        self.assertEqual(sum(row["in_review"] for row in measured.rows), 8)

    def test_registry_cannot_claim_oil_implementation_with_zero_slots(self):
        with self.assertRaisesRegex(ValueError, "no independent instrument slots"):
            self._build([])

    def test_wrong_scope_regime_basis_or_identity_is_refused(self):
        original = oil_index.expected_slots(RUN_ID, STAMP)
        for field, bad in (
                ("scope", "one carrier"),
                ("source_regime", "eLibrary document"),
                ("period_basis", "annual"),
                ("slot_id", "wrong")):
            rows = [dict(row) for row in original]
            rows[0][field] = bad
            with self.subTest(field=field), self.assertRaises(ValueError):
                self._build(rows)


if __name__ == "__main__":
    unittest.main(verbosity=2)
