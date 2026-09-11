#!/usr/bin/env python3
"""Focused controls for the production obligation/coverage-grid boundary."""
from __future__ import annotations

import csv
import pathlib
import tempfile
import unittest

from ferclib import coverage, periods
from ferclib.applicability import SourceHealth
from ferclib.obligations import (DEFAULT_UNIVERSE, FrozenApplicability,
                                 OccurrenceEvidence, SourceHealthEvidence,
                                 TaxonomyPin, build_coverage_grid, load_universe,
                                 measure_grid)
from ferclib.registry import BY_ID
from ferclib.status import (Availability, CoverageOutcome, Method, Origin,
                            Validation, VersionStatus)


STAMP = "2026-09-09T12:00:00+00:00"
AS_OF = "2026-09-07"
RUN = "SYNTHETIC-CODEX-OBLIGATIONS"


class SyntheticUniverse:
    def __init__(self, *, template="interstate_gas", forms=None,
                 entity="SYNTHETIC-E1", asset="synthetic-asset"):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "universe.csv"
        with self.path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=("asset_id", "entity_key", "status", "cod_group",
                            "template", "roster_forms"),
            )
            writer.writeheader()
            writer.writerow({
                "asset_id": asset,
                "entity_key": entity,
                "status": "operating",
                "cod_group": "post-cod",
                "template": template,
                "roster_forms": forms or "[]",
            })

    def close(self):
        self.tmp.cleanup()


def occurrence(form, year, period="Q4", *, health=SourceHealth.OK,
               entity="SYNTHETIC-E1", suffix="1", as_of="", indexed_only=False):
    return OccurrenceEvidence(
        entity_key=entity,
        form=form,
        reporting_year=year,
        reporting_period=period,
        source_health=health,
        detail=f"synthetic {form} occurrence",
        indexed_only=indexed_only,
        as_of=as_of,
        occurrence_id=f"SYN-{year}-{period}-{suffix}",
        source_system="SYNTHETIC-FERC",
    )


def pin(form, year, version="2024-01-01"):
    return TaxonomyPin(form, year, version, "SYNTHETIC frozen taxonomy manifest")


def build(universe, *, years=(2024, 2024), metrics=(), occurrences=(),
          source_health=(), pins=(), applicability=None):
    return build_coverage_grid(
        universe_path=universe.path,
        year_from=years[0],
        year_to=years[1],
        as_of=AS_OF,
        built_at=STAMP,
        run_id=RUN,
        metrics=metrics,
        occurrences=occurrences,
        source_health=source_health,
        taxonomy_pins=pins,
        applicability=applicability,
    )


def obs_for(slot, *, validation=Validation.PASS):
    return {
        "observation_id": f"SYN-OBS-{slot['slot_id']}",
        "entity_key": slot["entity_key"],
        "metric_id": slot["metric_id"],
        "source_regime": slot["source_regime"],
        "period_basis": slot["period_basis"],
        "period_start": slot.get("period_start"),
        "period_end": slot.get("period_end"),
        "instant_date": slot.get("instant_date"),
        "reporting_year": slot["reporting_year"],
        "reporting_period": slot["reporting_period"],
        "scope": slot["scope"],
        "unit": "iso4217:USD",
        "value_text": "100",
        "value_num": 100.0,
        "availability": Availability.PRESENT,
        "origin": Origin.NATIVE_XBRL,
        "method": Method.FILED,
        "version_status": VersionStatus.ORIGINAL,
        "validation": validation,
        "source_system": "SYNTHETIC-FERC",
        "filing_id": "SYN-FILING",
        "source_fact_id": "SYN-FACT",
        "document_id": None,
        "qa_flags": "SYNTHETIC warning" if validation != Validation.PASS else "",
        "missing_reason": "",
    }


class UniverseEligibility(unittest.TestCase):
    def test_actual_roster_reconciles_assets_entities_and_exclusions(self):
        universe = load_universe(DEFAULT_UNIVERSE)
        self.assertEqual((len(universe.assets), len(universe.entities), len(universe.exclusions)),
                         (109, 104, 11))
        overland = next(row for row in universe.entities if row.entity_key == "C000608")
        self.assertEqual(overland.asset_ids,
                         ("oke-overland-pass", "wmb-overland-pass"))
        self.assertEqual(overland.primary_asset_id, "oke-overland-pass")
        southtex = next(row for row in universe.entities if row.entity_key == "C003337")
        self.assertIn("Form 549D", southtex.eligibility_forms)

    def test_template_eligibility_seeds_unknown_slots_without_claiming_an_occurrence(self):
        universe = SyntheticUniverse(template="intrastate_549d")
        try:
            grid = build(universe, metrics=(BY_ID["i311_reporting_state"],))
        finally:
            universe.close()
        self.assertEqual(len(grid.slots), 4)
        self.assertEqual({row["requirement"] for row in grid.slots}, {coverage.UNKNOWN})
        self.assertEqual({row["source_health"] for row in grid.slots},
                         {SourceHealth.HEALTH_NOT_RECORDED})
        self.assertEqual({row["obligation_evidence_kind"] for row in grid.slots},
                         {"roster_declared"})


class FrozenInputsAndHistory(unittest.TestCase):
    def setUp(self):
        self.universe = SyntheticUniverse(forms='["Form 2", "Form 3-Q"]')

    def tearDown(self):
        self.universe.close()

    def test_prior_occurrence_informs_continuity_but_never_adds_historical_slots(self):
        rows = (
            occurrence("Form 2", 2023, suffix="prior"),
            occurrence("eLibrary document", 2018, "as_of", suffix="document"),
            occurrence("Form 2", 2027, suffix="future"),
        )
        grid = build(
            self.universe,
            years=(2024, 2026),
            metrics=(BY_ID["gas_operating_revenues"],),
            occurrences=rows,
        )
        self.assertEqual(len(grid.prior_eligibility_occurrences), 1)
        self.assertEqual(len(grid.window_occurrences), 0)
        self.assertEqual(
            {row["reason"] for row in grid.separated_occurrences},
            {"event_or_nonperiodic_feed", "after_coverage_window"},
        )
        self.assertNotIn(2023, {row["reporting_year"] for row in grid.slots})
        annual = [row for row in grid.obligations if row.family == "gas_annual"]
        self.assertEqual({row.evidence_kind for row in annual}, {"carried_forward"})

    def test_as_of_and_build_time_are_distinct_and_deterministic(self):
        kwargs = dict(
            universe_path=self.universe.path,
            year_from=2024,
            year_to=2026,
            as_of=AS_OF,
            built_at=STAMP,
            run_id=RUN,
            metrics=(BY_ID["gas_operating_revenues"],),
            occurrences=(occurrence("Form 2", 2023),),
        )
        first = build_coverage_grid(**kwargs)
        second = build_coverage_grid(**dict(kwargs, occurrences=reversed(kwargs["occurrences"])))
        self.assertEqual(first.input_digest, second.input_digest)
        self.assertEqual(first.slots, second.slots)
        self.assertEqual({row["frozen_at"] for row in first.slots}, {STAMP})
        future = [row for row in first.obligations
                  if row.form == "Form 2" and row.year == 2026]
        self.assertEqual((future[0].due_date, future[0].state),
                         ("2027-04-19", "future_not_due"))

    def test_time_varying_major_status_controls_each_years_quarterly_deadline(self):
        grid = build(
            self.universe,
            years=(2023, 2024),
            metrics=(BY_ID["gas_operating_revenues"],),
            occurrences=(
                occurrence("Form 2", 2023, suffix="f2"),
                occurrence("Form 3-Q", 2023, "Q1", suffix="3q-major"),
                occurrence("Form 2-A", 2024, suffix="f2a"),
                occurrence("Form 3-Q", 2024, "Q1", suffix="3q-nonmajor"),
            ),
        )
        q1 = {(row.year, row.form): row.due_date for row in grid.obligations
              if row.form == "Form 3Q Gas" and row.period == "Q1"}
        self.assertEqual(q1[(2023, "Form 3Q Gas")], "2023-05-30")
        self.assertEqual(q1[(2024, "Form 3Q Gas")], "2024-06-10")

    def test_mixed_source_health_generations_are_refused(self):
        base = dict(entity_key="SYNTHETIC-E1", form="Form 2", reporting_year=2024,
                    reporting_period="Q4", source_health=SourceHealth.OK,
                    detail="synthetic", recorded_at=STAMP)
        rows = [
            SourceHealthEvidence(record_id="H1", generation_id="G1", **base),
            SourceHealthEvidence(record_id="H2", generation_id="G2", **base),
        ]
        with self.assertRaisesRegex(ValueError, "mix generations"):
            build(self.universe, metrics=(BY_ID["gas_operating_revenues"],),
                  source_health=rows)


class RequirementAndOutcomeSeparation(unittest.TestCase):
    def setUp(self):
        self.universe = SyntheticUniverse(forms='["Form 2"]')
        self.metrics = (
            BY_ID["gas_operating_revenues"],
            BY_ID["total_throughput"],
            BY_ID["utility_operating_expenses"],
        )
        self.applicability = FrozenApplicability([
            {"form": "Form 2", "taxonomy_version": "2024-01-01",
             "concept_local": "OperatingRevenues", "in_form": "yes",
             "schedule_page": "114", "evidence": "SYNTHETIC present linkbase locator"},
            {"form": "Form 2", "taxonomy_version": "2024-01-01",
             "concept_local": "TotalVolumeOfThroughput", "in_form": "no",
             "schedule_page": "", "evidence": "SYNTHETIC complete-linkbase absence"},
        ], snapshot_id="SYNTHETIC-APPLICABILITY-V1")

    def tearDown(self):
        self.universe.close()

    def test_required_not_required_and_unknown_are_three_states(self):
        grid = build(
            self.universe,
            metrics=self.metrics,
            occurrences=(occurrence("Form 2", 2024),),
            pins=(pin("Form 2", 2024),),
            applicability=self.applicability,
        )
        by_metric = {}
        for row in grid.slots:
            by_metric.setdefault(row["metric_id"], set()).add(row["requirement"])
        self.assertEqual(by_metric["gas_operating_revenues"], {coverage.REQUIRED})
        self.assertEqual(by_metric["total_throughput"], {coverage.NOT_REQUIRED})
        self.assertEqual(by_metric["utility_operating_expenses"], {coverage.UNKNOWN})

        result = measure_grid(grid, [])
        outcomes = {}
        for expected, measured in zip(grid.slots, result.rows):
            outcomes.setdefault(expected["metric_id"], set()).add(measured["outcome"])
        self.assertEqual(outcomes["total_throughput"], {CoverageOutcome.NOT_REQUIRED})
        self.assertEqual(outcomes["utility_operating_expenses"],
                         {CoverageOutcome.APPLICABILITY_UNKNOWN})

    def test_durable_retrieval_failure_survives_into_slot_and_measurement(self):
        health = SourceHealthEvidence(
            record_id="HEALTH-2024-F2",
            generation_id="GENERATION-A",
            entity_key="SYNTHETIC-E1",
            form="Form 2",
            reporting_year=2024,
            reporting_period="Q4",
            source_health=SourceHealth.RETRIEVAL_FAILED,
            detail="SYNTHETIC captured request failed",
            recorded_at=STAMP,
        )
        grid = build(
            self.universe,
            metrics=(BY_ID["gas_operating_revenues"],),
            occurrences=(occurrence("Form 2", 2024),),
            source_health=(health,),
            pins=(pin("Form 2", 2024),),
            applicability=self.applicability,
        )
        self.assertEqual({row["source_health"] for row in grid.slots},
                         {SourceHealth.RETRIEVAL_FAILED})
        self.assertEqual({row["slot_state"] for row in grid.slots},
                         {coverage.TECHNICAL_FAILURE})
        self.assertTrue(all("HEALTH-2024-F2" in row["source_health_detail"]
                            for row in grid.slots))
        result = measure_grid(grid, [])
        self.assertEqual({row["outcome"] for row in result.rows},
                         {CoverageOutcome.RETRIEVAL_FAILED})

    def test_review_value_is_populated_but_never_validated(self):
        grid = build(
            self.universe,
            metrics=(BY_ID["gas_operating_revenues"],),
            occurrences=(occurrence("Form 2", 2024),),
            pins=(pin("Form 2", 2024),),
            applicability=self.applicability,
        )
        target = next(row for row in grid.slots if row["period_basis"] == periods.ANNUAL)
        result = measure_grid(
            grid, [obs_for(target, validation=Validation.SOURCE_ANOMALY_REVIEW)]
        )
        measured = next(row for row in result.rows if row["slot_id"] == target["slot_id"])
        self.assertEqual(measured["outcome"], CoverageOutcome.POPULATED_REVIEW)
        self.assertEqual((measured["populated"], measured["validated"], measured["in_review"]),
                         (1, 0, 1))

    def test_taxonomy_version_is_never_inferred_from_successful_ingestion(self):
        grid = build(
            self.universe,
            metrics=(BY_ID["gas_operating_revenues"],),
            occurrences=(occurrence("Form 2", 2024),),
            applicability=self.applicability,
            pins=(),
        )
        self.assertEqual({row["requirement"] for row in grid.slots}, {coverage.UNKNOWN})
        self.assertTrue(all("no taxonomy version is pinned" in row["requirement_evidence"]
                            for row in grid.slots))


class FailedIngestionCannotShrinkGrid(unittest.TestCase):
    def test_parse_failure_and_success_generate_the_same_ioc_denominator(self):
        universe = SyntheticUniverse(template="interstate_gas", forms='["Form 2-A"]')
        metric = (BY_ID["ioc_firm_transport_mdq"],)
        try:
            good = build(
                universe,
                years=(2025, 2026),
                metrics=metric,
                occurrences=(occurrence("Form 549B IOC", 2025, "Q3", suffix="good",
                                        as_of="2025-07-01"),),
            )
            broken = build(
                universe,
                years=(2025, 2026),
                metrics=metric,
                occurrences=(occurrence(
                    "Form 549B IOC", 2025, "Q3", suffix="broken",
                    health=SourceHealth.PARSE_FAILED, indexed_only=True,
                    as_of="2025-07-01"),),
            )
        finally:
            universe.close()
        self.assertEqual([row["slot_id"] for row in good.slots],
                         [row["slot_id"] for row in broken.slots])
        failed = [row for row in broken.slots if row["reporting_period"] == "Q3"
                  and row["reporting_year"] == 2025]
        self.assertEqual({row["source_health"] for row in failed},
                         {SourceHealth.PARSE_FAILED})
        self.assertEqual({row["slot_state"] for row in failed},
                         {coverage.TECHNICAL_FAILURE})


if __name__ == "__main__":
    unittest.main()
