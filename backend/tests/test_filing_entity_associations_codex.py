#!/usr/bin/env python3
"""Focused regressions for filing ownership and reviewed LNG associations.

The fixtures in this module are synthetic unless an accession is named.  The
named accessions are bounded, frozen eLibrary identities already present in the
candidate inputs; the tests do not call the network or open a shared database.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import tempfile
import types
import unittest
from unittest import mock

import run as pipeline
import validate as validation
from adapters import elibrary_docs, lng
from ferclib.registry import BY_ID
from ferclib.staging import FilingEntityConflict, Staging


ROOT = pathlib.Path(__file__).resolve().parents[1]
STAMP = "2026-09-09T18:02:08+00:00"
RUN_ID = "SYNTHETIC-FILING-ASSOCIATIONS"
ELBA_LIQUEFACTION = "NO-FERC-CID:Elba Liquefaction Company, L.L.C."
SOUTHERN_LNG = "C000039"
SABINE_LIQUEFACTION = "NO-FERC-CID:Sabine Pass Liquefaction, LLC"
SABINE_TERMINAL = "NO-FERC-CID:Sabine Pass LNG, L.P."


def _tariff_hit(description: str) -> dict:
    return {
        "description": description,
        "class_pairs": [("Application/Petition/Request", "Tariff Filing")],
    }


class TariffActorBoundary(unittest.TestCase):
    def test_tariff_actor_rejects_enlink_and_oneok_siblings(self):
        # These are the three source occurrences behind the five foreign-owner
        # observations caught during Build A v2.  Shared parent/name tokens do
        # not make one legal carrier the submitter for another carrier's tariff.
        cases = (
            (
                "20240903-5081",
                "EnLink Crude Pipeline, LLC",
                "EnLink Delaware Crude Pipeline, LLC submits tariff filing under IS24-802",
            ),
            (
                "20260522-5206",
                "ONEOK NGL Pipeline, L.L.C.",
                "ONEOK Southeast Texas NGL Pipeline, L.L.C. submits tariff filing under IS26-292",
            ),
            (
                "20260522-5237",
                "ONEOK NGL Pipeline, L.L.C.",
                "ONEOK West Texas NGL Pipeline, L.L.C. submits tariff filing under IS26-296",
            ),
        )
        for accession, legal_name, description in cases:
            with self.subTest(accession=accession):
                hit = _tariff_hit(description)
                self.assertFalse(
                    elibrary_docs._tariff_submitter_matches(legal_name, hit))
                self.assertFalse(elibrary_docs._target_owned_hit(legal_name, hit))

    def test_tariff_actor_accepts_exact_products_ngpl_and_reordered_mountainwest(self):
        cases = (
            (
                "Products (SE) Pipe Line Corporation",
                "Products (SE) Pipe Line Corporation submits tariff filing under IS26-1",
            ),
            (
                "Natural Gas Pipeline Company of America LLC",
                "Natural Gas Pipeline Company of America LLC submits tariff filing under RP26-1",
            ),
            (
                "MountainWest Overthrust Pipeline, LLC",
                "Overthrust Pipeline MountainWest, LLC submits tariff filing under RP26-2",
            ),
        )
        for legal_name, description in cases:
            with self.subTest(legal_name=legal_name):
                hit = _tariff_hit(description)
                self.assertTrue(
                    elibrary_docs._tariff_submitter_matches(legal_name, hit))
                self.assertTrue(elibrary_docs._target_owned_hit(legal_name, hit))


def _filing(owner: str) -> dict:
    return {
        "source_system": "eLibrary",
        "filing_id": "SYNTHETIC-OCCURRENCE",
        "entity_key": owner,
        "form": "eLibrary document",
        "accession_number": "SYNTHETIC-OCCURRENCE",
        "content_hash": "a" * 64,
    }


class FilingEntityPersistence(unittest.TestCase):
    @staticmethod
    def _store(root: pathlib.Path) -> Staging:
        store = Staging(root / "filing-associations.sqlite")
        store.write_entities([
            {"entity_key": "ENTITY-A", "legal_name": "Entity A, LLC"},
            {"entity_key": "ENTITY-B", "legal_name": "Entity B, LLC"},
        ])
        return store

    def test_exclusive_same_owner_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="ferc-filing-owner-") as td:
            store = self._store(pathlib.Path(td))
            try:
                store.write_filing_bundle(_filing("ENTITY-A"))
                store.write_filing_bundle(_filing("ENTITY-A"))
                filings = [dict(row) for row in store.query(
                    "SELECT source_system,filing_id,entity_key FROM filings")]
                relations = [dict(row) for row in store.query(
                    "SELECT source_system,filing_id,entity_key,association_role,"
                    "facility_key,evidence_ref FROM filing_entities")]
                self.assertEqual(filings, [{
                    "source_system": "eLibrary",
                    "filing_id": "SYNTHETIC-OCCURRENCE",
                    "entity_key": "ENTITY-A",
                }])
                self.assertEqual(len(relations), 1)
                self.assertEqual(relations[0]["entity_key"], "ENTITY-A")
                self.assertEqual(relations[0]["association_role"], "source_entity")
            finally:
                store.close()

    def test_exclusive_owner_conflict_is_rejected_without_mutation(self):
        with tempfile.TemporaryDirectory(prefix="ferc-filing-conflict-") as td:
            store = self._store(pathlib.Path(td))
            try:
                store.write_filing_bundle(_filing("ENTITY-A"))
                with self.assertRaisesRegex(
                        FilingEntityConflict, "existing owner.*attempted owner"):
                    store.write_filing_bundle(_filing("ENTITY-B"))
                owner = store.query(
                    "SELECT entity_key FROM filings WHERE source_system='eLibrary' "
                    "AND filing_id='SYNTHETIC-OCCURRENCE'")[0]["entity_key"]
                relations = [row["entity_key"] for row in store.query(
                    "SELECT entity_key FROM filing_entities ORDER BY entity_key")]
                self.assertEqual(owner, "ENTITY-A")
                self.assertEqual(relations, ["ENTITY-A"])
                self.assertFalse(store.con.in_transaction)
            finally:
                store.close()

    def test_shared_anchor_is_deterministic_under_reverse_replay(self):
        associations = [
            {
                "entity_key": "ENTITY-A", "association_role": "named_filer",
                "facility_key": "shared-facility", "evidence_ref": "source:A",
            },
            {
                "entity_key": "ENTITY-B", "association_role": "named_filer",
                "facility_key": "shared-facility", "evidence_ref": "source:B",
            },
        ]
        outcomes = []
        for replay_order in (("ENTITY-A", "ENTITY-B"),
                             ("ENTITY-B", "ENTITY-A")):
            with self.subTest(replay_order=replay_order), tempfile.TemporaryDirectory(
                    prefix="ferc-shared-owner-") as td:
                store = self._store(pathlib.Path(td))
                try:
                    for owner in replay_order:
                        supplied = (associations if owner == "ENTITY-A"
                                    else list(reversed(associations)))
                        store.write_filing_bundle(
                            _filing(owner), filing_entities=supplied,
                            allow_shared_entities=True)
                    owner = store.query(
                        "SELECT entity_key FROM filings WHERE source_system='eLibrary' "
                        "AND filing_id='SYNTHETIC-OCCURRENCE'")[0]["entity_key"]
                    relations = [tuple(row) for row in store.query(
                        "SELECT entity_key,association_role,facility_key,evidence_ref "
                        "FROM filing_entities ORDER BY entity_key")]
                    outcomes.append((owner, relations))
                finally:
                    store.close()
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0][0], "ENTITY-A")
        self.assertEqual([row[0] for row in outcomes[0][1]],
                         ["ENTITY-A", "ENTITY-B"])


class FinalValidationBoundary(unittest.TestCase):
    class _Store:
        def __init__(self, compatibility: int):
            self.compatibility = compatibility

        def query(self, sql):
            if "FROM filings f" in sql:
                return [{"n": 0}]
            if "LEFT JOIN filings f" in sql:
                return [{"n": 0}]
            if "association_role='compatibility_anchor'" in sql:
                return [{"n": self.compatibility}]
            if "FROM asset_dockets GROUP BY role" in sql:
                return [{"role": lng.AUTHORITY_ROLE, "n": 44, "blank": 0}]
            raise AssertionError(f"unexpected SQL: {sql}")

    def test_legacy_compatibility_anchor_is_not_release_ready(self):
        ctx = types.SimpleNamespace(staging=self._Store(1))
        with self.assertRaisesRegex(AssertionError, "require reviewed replay"):
            validation.t_filing_entity_associations(ctx)

    def test_reviewed_occurrence_relations_are_valid_control(self):
        ctx = types.SimpleNamespace(staging=self._Store(0))
        message = validation.t_filing_entity_associations(ctx)
        self.assertIn("44 reviewed LNG", message)


class ReviewedLNGInputs(unittest.TestCase):
    def test_lng_seed_has_exact_frozen_64_row_identity(self):
        self.assertEqual(
            lng.SEED_SHA256,
            "1d3bce4f7d9ecc0b0c40ad5d93f6aaf22b8eea2dcea0eb9da0fe64e285cc2e00",
        )
        self.assertEqual(
            hashlib.sha256(lng.SEED_CSV.read_bytes()).hexdigest(), lng.SEED_SHA256)
        rows = lng._reviewed_seed_rows()
        self.assertEqual(len(rows), 64)
        self.assertEqual(
            {"20260813-5004", "20260730-3052", "20260803-5167"}
            - {row["accession"] for row in rows},
            set(),
        )

    def test_lng_seed_missing_and_tampered_inputs_are_refused(self):
        with tempfile.TemporaryDirectory(prefix="ferc-lng-seed-") as td:
            root = pathlib.Path(td)
            missing = root / "missing.csv"
            with mock.patch.object(lng, "SEED_CSV", missing):
                with self.assertRaisesRegex(FileNotFoundError, "mandatory versioned"):
                    lng._reviewed_seed_rows()

            tampered = root / "tampered.csv"
            tampered.write_bytes(lng.SEED_CSV.read_bytes() + b"\n")
            with mock.patch.object(lng, "SEED_CSV", tampered):
                with self.assertRaisesRegex(ValueError, "hash mismatch"):
                    lng._reviewed_seed_rows()

    def test_reviewed_authority_has_exact_44_static_rows(self):
        rows = lng.reviewed_asset_docket_rows(ROOT / "config" / "universe.csv")
        pairs = [{"asset_id": row["asset_id"], "docket": row["docket"]}
                 for row in rows]
        digest = hashlib.sha256(json.dumps(
            pairs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(len(rows), 44)
        self.assertEqual(len({(row["asset_id"], row["docket"]) for row in rows}), 44)
        self.assertEqual(digest,
                         "163cbd2e8bdc2f354ecf3b11cef2658e8022f607e5897a5fb037b5a408fb8b57")
        self.assertEqual({row["role"] for row in rows}, {lng.AUTHORITY_ROLE})
        self.assertTrue(all(row["evidence_ref"] for row in rows))

    def test_dynamic_filing_docket_is_visible_but_never_promoted_to_authority(self):
        class StagingProbe:
            run_id = RUN_ID

            def __init__(self):
                self.published = None

            def query(self, sql):
                if "FROM filings" in sql:
                    return []
                if "FROM filing_dockets" in sql:
                    return [{"docket": "RP99-999"}]
                raise AssertionError(f"unexpected SQL: {sql}")

            def replace_reference_state(self, **kwargs):
                self.published = kwargs
                return {name: len(rows) for name, rows in kwargs.items()}

        staging = StagingProbe()
        ctx = types.SimpleNamespace(
            staging=staging,
            cache=types.SimpleNamespace(manifest_rows=lambda: []),
            applicability=object(),
            log=lambda *_args, **_kwargs: None,
        )
        pipeline._persist_reference_state(ctx)
        self.assertIsNotNone(staging.published)
        self.assertIn("RP99-999", {row["docket"] for row in staging.published["dockets"]})
        self.assertEqual(len(staging.published["asset_dockets"]), 44)
        self.assertNotIn(
            "RP99-999", {row["docket"] for row in staging.published["asset_dockets"]})
        self.assertEqual(
            {row["role"] for row in staging.published["asset_dockets"]},
            {lng.AUTHORITY_ROLE},
        )

    def test_occurrence_relations_are_source_and_facility_specific(self):
        cases = (
            (
                ELBA_LIQUEFACTION, "20260813-5004", "elba",
                {ELBA_LIQUEFACTION: "named_filer", SOUTHERN_LNG: "named_filer"},
            ),
            (
                SOUTHERN_LNG, "20260730-3052", "elba",
                {ELBA_LIQUEFACTION: "facility_subject", SOUTHERN_LNG: "named_filer"},
            ),
            (
                SABINE_LIQUEFACTION, "20260803-5167", "sabine-pass",
                {SABINE_LIQUEFACTION: "named_filer", SABINE_TERMINAL: "named_filer"},
            ),
            (
                SABINE_LIQUEFACTION, "20161012-3036", "sabine-pass",
                {SABINE_LIQUEFACTION: "named_filer", SABINE_TERMINAL: "named_filer"},
            ),
        )
        for entity_key, accession, facility_key, expected in cases:
            with self.subTest(accession=accession):
                filing = next(
                    row for row in lng._seed_rows(
                        entity_key, lng.FACILITY_ENTITY_NAMES[entity_key])
                    if row["accession"] == accession)
                relations = lng._filing_entity_associations(
                    {"entity_key": entity_key}, lng.FACILITIES[entity_key], filing)
                self.assertEqual(
                    {row["entity_key"]: row["association_role"] for row in relations},
                    expected,
                )
                self.assertEqual({row["facility_key"] for row in relations},
                                 {facility_key})
                self.assertTrue(all(
                    f"eLibrary:{accession}" in row["evidence_ref"]
                    and lng.SEED_SHA256 in row["evidence_ref"]
                    for row in relations))

    def test_live_elba_hit_keeps_reviewed_shared_filers_for_exact_accession(self):
        accession = "20241122-3097"
        seeds = lng._seed_rows(ELBA_LIQUEFACTION,
                               lng.FACILITY_ENTITY_NAMES[ELBA_LIQUEFACTION])
        live = [{
            "accession": accession,
            "description": ("Commission issued errata regarding Elba Liquefaction "
                            "Company, L.L.C. et al."),
            "filed_date": "2024-11-22",
            "dockets": ["CP23-375"],
            "docket_bases": ["CP23-375"],
            "shared_filers": ["Elba Liquefaction Company, L.L.C."],
            "source": "sweep",
        }]
        forward = lng._merge_reviewed_occurrence_metadata(live, seeds)
        reverse = lng._merge_reviewed_occurrence_metadata(live, list(reversed(seeds)))
        self.assertEqual(forward, reverse)
        self.assertEqual(forward[0]["description"], live[0]["description"])
        self.assertEqual(forward[0]["source"], "sweep")
        self.assertEqual(set(forward[0]["shared_filers"]), {
            "Elba Liquefaction Company, L.L.C.",
            "Southern LNG Company, L.L.C.",
        })
        relations = lng._filing_entity_associations(
            {"entity_key": ELBA_LIQUEFACTION},
            lng.FACILITIES[ELBA_LIQUEFACTION], forward[0])
        self.assertEqual(
            {row["entity_key"]: row["association_role"] for row in relations},
            {ELBA_LIQUEFACTION: "named_filer", SOUTHERN_LNG: "named_filer"},
        )

    def test_reviewed_metadata_never_moves_to_another_accession(self):
        seeds = lng._seed_rows(ELBA_LIQUEFACTION,
                               lng.FACILITY_ENTITY_NAMES[ELBA_LIQUEFACTION])
        live = [{"accession": "DIFFERENT", "description": "unrelated live hit"}]
        self.assertNotIn(
            "shared_filers",
            lng._merge_reviewed_occurrence_metadata(live, seeds)[0],
        )


class _CoverageStaging:
    run_id = RUN_ID

    def __init__(self, *, target_entity: str, filing_entity: str,
                 target_asset: str, filing_asset: str, target_group: str,
                 filing_group: str, facility_key: str, docket: str,
                 target_role: str = "named_filer"):
        self.target_entity = target_entity
        self.filing_entity = filing_entity
        self.target_asset = target_asset
        self.filing_asset = filing_asset
        self.target_group = target_group
        self.filing_group = filing_group
        self.facility_key = facility_key
        self.docket = docket
        self.target_role = target_role

    def query(self, sql):
        if "FROM filings" in sql:
            return [{
                "source_system": "eLibrary", "filing_id": "SHARED-FILING",
                "entity_key": self.filing_entity, "is_canonical": 1,
            }]
        if "FROM documents" in sql:
            return [{
                "document_id": "SHARED-DOCUMENT", "source_system": "eLibrary",
                "filing_id": "SHARED-FILING", "availability": "retrieved",
            }]
        if "FROM filing_dockets" in sql:
            return [{
                "source_system": "eLibrary", "filing_id": "SHARED-FILING",
                "docket": self.docket,
            }]
        if "FROM asset_entity_map" in sql:
            return [
                {"entity_key": self.target_entity, "asset_id": self.target_asset},
                {"entity_key": self.filing_entity, "asset_id": self.filing_asset},
            ]
        if "FROM assets" in sql:
            return [
                {"asset_id": self.target_asset, "group_key": self.target_group},
                {"asset_id": self.filing_asset, "group_key": self.filing_group},
            ]
        if "FROM asset_dockets" in sql:
            return [
                {"asset_id": self.target_asset, "docket": self.docket},
                {"asset_id": self.filing_asset, "docket": self.docket},
            ]
        if "FROM filing_entities" in sql:
            return [
                {
                    "source_system": "eLibrary", "filing_id": "SHARED-FILING",
                    "entity_key": self.filing_entity,
                    "association_role": "named_filer",
                    "facility_key": self.filing_group,
                    "evidence_ref": "synthetic anchor relation",
                },
                {
                    "source_system": "eLibrary", "filing_id": "SHARED-FILING",
                    "entity_key": self.target_entity,
                    "association_role": self.target_role,
                    "facility_key": self.facility_key,
                    "evidence_ref": "synthetic target relation",
                },
            ]
        raise AssertionError(f"unexpected SQL: {sql}")


def _coverage_observation(entity_key: str, metric_id: str, unit: str) -> dict:
    metric = BY_ID[metric_id]
    return {
        "observation_id": f"SYNTHETIC-{metric_id}",
        "entity_key": entity_key,
        "metric_id": metric_id,
        "source_regime": "eLibrary document",
        "period_basis": "as_of",
        "period_start": None,
        "period_end": None,
        "instant_date": "2026-08-13",
        "reporting_year": 2026,
        "reporting_period": "as_of",
        "scope": metric.scope,
        "value_text": "source-supported value",
        "value_num": None,
        "unit": unit,
        "availability": "present",
        "validation": "pass",
        "version_status": "original",
        "source_system": "eLibrary",
        "filing_id": "SHARED-FILING",
        "document_id": "SHARED-DOCUMENT",
        "missing_reason": "",
        "qa_flags": "",
    }


def _coverage_context(staging: _CoverageStaging):
    return types.SimpleNamespace(
        args=types.SimpleNamespace(year_to=2026),
        as_of=dt.date(2026, 9, 7),
        staging=staging,
    )


class CoverageAssociationGate(unittest.TestCase):
    def _materialise(self, staging, observation):
        return pipeline._document_coverage_slots(
            _coverage_context(staging), RUN_ID, STAMP, [observation],
            year_from=2024, year_to=2026)

    def test_shared_elba_and_sabine_operational_occurrences_are_accepted(self):
        cases = (
            _CoverageStaging(
                target_entity=ELBA_LIQUEFACTION, filing_entity=SOUTHERN_LNG,
                target_asset="kmi-elba-liquefaction",
                filing_asset="kmi-southern-lng-elba-island",
                target_group="elba", filing_group="elba",
                facility_key="elba", docket="CP14-103",
            ),
            _CoverageStaging(
                target_entity=SABINE_LIQUEFACTION, filing_entity=SABINE_TERMINAL,
                target_asset="lng-sabine-pass-liquefaction-trains-1-4",
                filing_asset="lng-sabine-pass-lng-terminal",
                target_group="sabine-pass", filing_group="sabine-pass",
                facility_key="sabine-pass", docket="CP11-72",
            ),
        )
        for staging in cases:
            with self.subTest(target=staging.target_entity):
                _, quality = self._materialise(
                    staging,
                    _coverage_observation(
                        staging.target_entity, "lng_operational_report", "(period)"),
                )
                row = quality["rows"][0]
                self.assertTrue(row["entity_association_ok"])
                self.assertEqual(
                    row["entity_association"],
                    f"reviewed_shared_lng_facility:named_filer:{staging.docket}",
                )

    def test_jointly_named_filer_application_order_and_capacity_are_accepted(self):
        staging = _CoverageStaging(
            target_entity=SABINE_LIQUEFACTION, filing_entity=SABINE_TERMINAL,
            target_asset="lng-sabine-pass-liquefaction-trains-1-4",
            filing_asset="lng-sabine-pass-lng-terminal",
            target_group="sabine-pass", filing_group="sabine-pass",
            facility_key="sabine-pass", docket="CP11-72",
        )
        for metric_id, unit in (
                ("lng_status_requested", "categorical"),
                ("lng_status_authorised", "categorical"),
                ("lng_material_order", "categorical"),
                ("lng_liquefaction_capacity", "MTPA")):
            with self.subTest(metric_id=metric_id):
                _, quality = self._materialise(
                    staging, _coverage_observation(
                        staging.target_entity, metric_id, unit))
                row = quality["rows"][0]
                self.assertTrue(row["entity_association_ok"])
                self.assertEqual(
                    row["entity_association"],
                    f"reviewed_shared_lng_facility:named_filer:{staging.docket}",
                )

    def test_rate_capacity_and_cross_facility_borrowing_are_rejected(self):
        cases = (
            (
                "rate",
                _CoverageStaging(
                    target_entity="C001087", filing_entity="C001088",
                    target_asset="wmb-mountainwest-overthrust",
                    filing_asset="wmb-mountainwest-pipeline",
                    target_group="mountainwest", filing_group="mountainwest",
                    facility_key="mountainwest", docket="RP26-1",
                ),
                _coverage_observation("C001087", "rate_case_status", "categorical"),
            ),
            (
                "capacity",
                _CoverageStaging(
                    target_entity=ELBA_LIQUEFACTION, filing_entity=SOUTHERN_LNG,
                    target_asset="kmi-elba-liquefaction",
                    filing_asset="kmi-southern-lng-elba-island",
                    target_group="elba", filing_group="elba",
                    facility_key="elba", docket="CP14-103",
                    target_role="facility_subject",
                ),
                _coverage_observation(
                    ELBA_LIQUEFACTION, "lng_liquefaction_capacity", "MTPA"),
            ),
            (
                "cross-facility",
                _CoverageStaging(
                    target_entity=ELBA_LIQUEFACTION, filing_entity=SABINE_TERMINAL,
                    target_asset="kmi-elba-liquefaction",
                    filing_asset="lng-sabine-pass-lng-terminal",
                    target_group="elba", filing_group="sabine-pass",
                    facility_key="sabine-pass", docket="CP11-72",
                ),
                _coverage_observation(
                    ELBA_LIQUEFACTION, "lng_operational_report", "(period)"),
            ),
        )
        for label, staging, observation in cases:
            with self.subTest(case=label), self.assertRaisesRegex(
                    ValueError,
                    "no occurrence-specific, metric-gated reviewed LNG association"):
                self._materialise(staging, observation)


if __name__ == "__main__":
    unittest.main(verbosity=2)
