#!/usr/bin/env python3
"""Independent controls for eLibrary route coverage and occurrence quality."""
from __future__ import annotations

import csv
import datetime as dt
import pathlib
import tempfile
import types
import unittest

import run as pipeline
from ferclib import coverage, periods
from ferclib.applicability import EvidenceKind, SourceHealth
from ferclib.obligations import build_coverage_grid
from ferclib.registry import BY_ID
from ferclib.staging import slot_id


STAMP = "2026-09-09T18:02:08+00:00"
RUN = "SYNTHETIC-DOCUMENT-GRID"


class _Universe:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ferc-document-grid-")
        self.path = pathlib.Path(self.tmp.name) / "universe.csv"
        with self.path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=("asset_id", "entity_key", "status", "cod_group",
                            "template", "roster_forms"),
            )
            writer.writeheader()
            writer.writerow({
                "asset_id": "synthetic-doc-asset", "entity_key": "SYNTHETIC-DOC",
                "status": "operating", "cod_group": "post-cod",
                "template": "interstate_gas", "roster_forms": "[]",
            })

    def close(self):
        self.tmp.cleanup()


def _route_anchor(*, scope=None, asset_id="synthetic-doc-asset"):
    metric = BY_ID["rate_case_status"]
    actual_scope = metric.scope if scope is None else scope
    row = coverage.build_expected(
        "SYNTHETIC-DOC", asset_id, "interstate_gas", metric,
        "eLibrary document", periods.AS_OF, 2025, "Q4",
        coverage.REQUIRED, "independent route contract",
        "synthetic-document-v2", RUN, as_of=None, actual_scope=actual_scope,
        due_date=None, slot_state=coverage.SLOT_OPEN,
        obligation_form="eLibrary document route",
        obligation_authority="event-driven official FERC route",
        obligation_evidence_kind=EvidenceKind.NONE,
        source_health=SourceHealth.HEALTH_NOT_RECORDED,
        source_health_detail="pre-retrieval anchor",
        denominator_origin="document_adapter_anchor", docket="", facility="",
    )
    return row


class IndependentDocumentGrid(unittest.TestCase):
    def setUp(self):
        self.universe = _Universe()
        self.metric = BY_ID["rate_case_status"]

    def tearDown(self):
        self.universe.close()

    def _build(self, rows):
        return build_coverage_grid(
            universe_path=self.universe.path, year_from=2024, year_to=2026,
            as_of="2026-09-07", built_at=STAMP, run_id=RUN,
            metrics=(self.metric,), document_slots=rows,
        )

    def test_document_metric_cannot_disappear_from_denominator(self):
        with self.assertRaisesRegex(ValueError, "no independent route anchors"):
            self._build([])

    def test_exact_stable_anchor_is_measured_and_manifested(self):
        row = _route_anchor()
        grid = self._build([row])
        self.assertEqual(len(grid.slots), 1)
        self.assertEqual(grid.manifest()["document_slots"], 1)
        self.assertEqual(grid.slots[0]["scope"], self.metric.scope)
        self.assertIsNone(grid.slots[0]["instant_date"])
        self.assertEqual(grid.slots[0]["frozen_at"], STAMP)

    def test_input_order_is_deterministic_and_duplicate_pair_is_refused(self):
        row = _route_anchor()
        one = self._build([row])
        two = self._build(list(reversed([row])))
        self.assertEqual(one.input_digest, two.input_digest)
        self.assertEqual(one.slots, two.slots)
        with self.assertRaisesRegex(ValueError, "duplicate document slot"):
            self._build([row, dict(row)])

    def test_wrong_identity_registry_or_anchor_contract_is_refused(self):
        original = _route_anchor()
        cases = (
            ("slot_id", "wrong"), ("template", "liquids"),
            ("source_regime", "Form 2"), ("period_basis", periods.ANNUAL),
            ("unit_rule", "anything"), ("asset_id", "wrong-asset"),
            ("scope", "first parser result"),
            ("denominator_origin", "document_occurrence"),
            ("instant_date", "2025-03-14"),
        )
        for field, bad in cases:
            row = dict(original)
            row[field] = bad
            with self.subTest(field=field), self.assertRaises(ValueError):
                self._build([row])

    def test_explicit_empty_scope_remains_unresolved_and_is_not_guessed(self):
        row = _route_anchor(scope="")
        self.assertEqual(row["scope"], "")
        self.assertEqual(
            row["slot_id"],
            slot_id("SYNTHETIC-DOC", self.metric.id, "eLibrary document",
                    periods.AS_OF, "", "", "", ""),
        )
        with self.assertRaisesRegex(ValueError, "registry route contract"):
            self._build([row])


class _FakeStaging:
    run_id = "SYNTHETIC-PRIOR-RUN"

    def __init__(self, *, filing=True, filing_entity="C000020", canonical=True,
                 document_filing="20250314-3001", joint=False):
        self.filing = filing
        self.filing_entity = filing_entity
        self.canonical = canonical
        self.document_filing = document_filing
        self.joint = joint

    def query(self, sql):
        if "FROM filings" in sql:
            return ([{"source_system": "eLibrary", "filing_id": "20250314-3001",
                      "entity_key": self.filing_entity,
                      "is_canonical": 1 if self.canonical else 0}]
                    if self.filing else [])
        if "FROM documents" in sql:
            return ([{"document_id": "DOC-1", "source_system": "eLibrary",
                      "filing_id": self.document_filing, "availability": "retrieved"}]
                    if self.filing else [])
        if "FROM filing_dockets" in sql:
            return ([{"source_system": "eLibrary", "filing_id": "20250314-3001",
                      "docket": "RP25-1"}] if self.filing else [])
        if "FROM asset_entity_map" in sql:
            return [
                {"entity_key": "C000020", "asset_id": "kmi-tennessee-gas-pipeline"},
                {"entity_key": "C001087", "asset_id": "other-pipeline"},
            ]
        if "FROM assets" in sql:
            return [
                {"asset_id": "kmi-tennessee-gas-pipeline", "group_key": "synthetic"},
                {"asset_id": "other-pipeline", "group_key": "synthetic"},
            ]
        if "FROM asset_dockets" in sql:
            return ([
                {"asset_id": "kmi-tennessee-gas-pipeline", "docket": "RP25-1"},
                {"asset_id": "other-pipeline", "docket": "RP25-1"},
            ] if self.joint else [])
        if "FROM filing_entities" in sql:
            rows = [{
                "source_system": "eLibrary", "filing_id": "20250314-3001",
                "entity_key": self.filing_entity, "association_role": "named_filer",
                "facility_key": "synthetic", "evidence_ref": "synthetic reviewed fixture",
            }] if self.filing else []
            if self.joint and self.filing_entity != "C000020":
                rows.append({
                    "source_system": "eLibrary", "filing_id": "20250314-3001",
                    "entity_key": "C000020", "association_role": "named_filer",
                    "facility_key": "synthetic", "evidence_ref": "synthetic reviewed fixture",
                })
            return rows
        raise AssertionError(f"unexpected SQL: {sql}")


def _production_observation(**changes):
    row = {
        "observation_id": "SYNTHETIC-DOC-OBS", "entity_key": "C000020",
        "metric_id": "rate_case_status", "source_regime": "eLibrary document",
        "period_basis": periods.AS_OF, "period_start": None, "period_end": None,
        "instant_date": "2025-03-14", "reporting_year": 2025,
        "reporting_period": "as_of",
        "scope": "the named docket only | docket RP25-1",
        "value_text": "accepted", "value_num": None, "unit": "categorical",
        "availability": "present", "validation": "pass",
        "version_status": "original", "source_system": "eLibrary",
        "filing_id": "20250314-3001", "document_id": "DOC-1",
        "missing_reason": "", "qa_flags": "",
    }
    row.update(changes)
    return row


class ProductionMaterialisation(unittest.TestCase):
    def _ctx(self, **kwargs):
        return types.SimpleNamespace(
            args=types.SimpleNamespace(year_to=2026),
            as_of=dt.date(2026, 9, 7),
            staging=_FakeStaging(**kwargs),
        )

    def _materialise(self, observations=(), **kwargs):
        return pipeline._document_coverage_slots(
            self._ctx(**kwargs), RUN, STAMP, list(observations),
            year_from=2024, year_to=2026)

    def test_parser_rows_cannot_create_or_delete_denominator(self):
        with_row, with_quality = self._materialise([_production_observation()])
        without_row, without_quality = self._materialise([])
        self.assertEqual([r["slot_id"] for r in with_row],
                         [r["slot_id"] for r in without_row])
        self.assertEqual(len(with_quality["rows"]), 1)
        self.assertEqual(len(without_quality["rows"]), 0)

    def test_multiasset_entity_anchor_is_not_assigned_to_first_asset(self):
        rows, _ = self._materialise([])
        target = [r for r in rows if r["entity_key"] == "C000640"
                  and r["metric_id"] == "rate_case_status"]
        self.assertEqual(len(target), 1)
        self.assertEqual(target[0]["asset_id"], "")

    def test_missing_filing_wrong_filer_and_wrong_document_are_refused(self):
        with self.assertRaisesRegex(ValueError, "missing filing occurrence"):
            self._materialise([_production_observation()], filing=False)
        with self.assertRaisesRegex(ValueError, "no occurrence-specific"):
            self._materialise([_production_observation()],
                              filing_entity="C001087")
        with self.assertRaisesRegex(ValueError, "outside its filing occurrence"):
            self._materialise([_production_observation()],
                              document_filing="OTHER")

    def test_arbitrary_joint_docket_cannot_authorize_a_rate_metric(self):
        with self.assertRaisesRegex(ValueError, "metric-gated"):
            self._materialise([_production_observation()],
                              filing_entity="C001087", joint=True)

    def test_nonpublic_is_neither_ok_health_nor_filed_evidence(self):
        observation = _production_observation(
            availability="nonpublic", document_id="", unit="",
            value_text=None, validation="not_yet_validated")
        _, quality = self._materialise([observation])
        row = quality["rows"][0]
        self.assertNotEqual(row["source_health"], SourceHealth.OK)
        self.assertEqual(row["evidence_kind"], EvidenceKind.NONE)

    def test_out_of_window_and_superseded_rows_do_not_change_denominator(self):
        old = _production_observation(observation_id="OLD", instant_date="2020-01-01")
        superseded = _production_observation(
            observation_id="SUPERSEDED", version_status="superseded")
        anchors, quality = self._materialise([old, superseded])
        self.assertTrue(anchors)
        by_id = {r["observation_id"]: r for r in quality["rows"]}
        self.assertFalse(by_id["OLD"]["in_declared_window"])
        self.assertFalse(by_id["SUPERSEDED"]["current_version"])
        self.assertFalse(any(r["included_in_coverage_denominator"]
                             for r in quality["rows"]))

    def test_two_same_day_occurrences_survive_as_distinct_quality_rows(self):
        second = _production_observation(observation_id="SECOND")
        _, quality = self._materialise([_production_observation(), second])
        self.assertEqual([r["observation_id"] for r in quality["rows"]],
                         ["SECOND", "SYNTHETIC-DOC-OBS"])

    def test_lng_inspection_contract_accepts_status_and_date_families(self):
        families, open_rule = coverage.admissible_unit_families(
            BY_ID["lng_inspection"].unit_rule)
        self.assertFalse(open_rule)
        self.assertEqual(families, frozenset({"categorical", "date"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
