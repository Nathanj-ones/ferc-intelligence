#!/usr/bin/env python3
"""Negative acceptance tests for A05, A06 and A07.

Every test here asserts that a BAD state is REJECTED. A test that starts passing
because the mutated record became acceptable is a failed test, not a fixed one
(contract rule 4). All fixtures are synthetic, are labelled SYNTHETIC in their
identifiers, and never touch production sources or coverage.

Authored by w4-coverage and adopted into `tests/` (W4-R8), which w6-acceptance
owns. The assertions are w4's and are unchanged; only the path bootstrap, this
note and the run command below are w6's.

Run:  python3 -m unittest discover -s tests -p 'test_coverage.py' -v
      python3 tests/mutation_check_coverage.py     # the six-defect mutation check
      (from outputs/repair_all_regimes)
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys
import unittest
from collections import Counter

HERE = pathlib.Path(__file__).resolve().parent
# Adopted into tests/ from work/w4-coverage/ (W4-R8), which is one level
# shallower, so the root is the parent rather than the grandparent. Only the
# path bootstrap changed; not one assertion was touched.
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from ferclib import coverage as cov                                     # noqa: E402
from ferclib import periods                                            # noqa: E402
from ferclib.applicability import (EvidenceKind, ObligationRegister,    # noqa: E402
                                   SourceHealth)
from ferclib.registry import BY_ID                                      # noqa: E402
from ferclib.status import Availability, CoverageOutcome, Validation    # noqa: E402

TODAY = dt.date(2026, 9, 8)
SYN = "SYNTHETIC-w4"


def slot_for(metric_id, *, regime="Form 2", basis=periods.QUARTER, year=2025,
             period="Q4", entity="SYNTHETIC-E1", template="interstate_gas", as_of=""):
    m = BY_ID[metric_id]
    return cov.build_expected(entity, "synthetic-asset", template, m, regime, basis,
                              year, period, cov.REQUIRED, "synthetic fixture",
                              "synthetic", SYN, as_of=as_of, slot_state=cov.SLOT_OPEN,
                              source_health=SourceHealth.OK)


def obs_for(slot, *, unit, value="100", availability=Availability.PRESENT,
            validation=Validation.PASS, method="filed", version_status="original",
            scope=None, obs_id=None, filing_id="SYNTHETIC-F1",
            source_system="eCollection_XBRL", source_fact_id="SYNTHETIC-FACT"):
    return {
        "observation_id": obs_id or f"{SYN}-obs-{unit}-{method}-{availability}",
        "entity_key": slot["entity_key"], "metric_id": slot["metric_id"],
        "source_regime": slot["source_regime"], "period_basis": slot["period_basis"],
        "period_start": slot["period_start"], "period_end": slot["period_end"],
        "instant_date": slot["instant_date"], "reporting_year": slot["reporting_year"],
        "reporting_period": slot["reporting_period"],
        "scope": slot["scope"] if scope is None else scope,
        "unit": unit, "value_text": value, "value_num": None,
        "availability": availability, "origin": "native_xbrl", "method": method,
        "version_status": version_status, "validation": validation,
        "source_system": source_system, "filing_id": filing_id,
        "source_fact_id": source_fact_id, "document_id": None,
        "qa_flags": "", "missing_reason": "",
    }


def measured(slot, observations, **kw):
    return cov.measure([slot], observations, SYN, **kw)[0]


# ------------------------------------------------------------------ A06 gates

class TestAdmissibility(unittest.TestCase):
    """A06: a same-grain candidate must still be refused when it is wrong."""

    def test_wrong_unit_family_cannot_satisfy_a_usd_slot(self):
        slot = slot_for("gas_operating_revenues")               # iso4217:USD
        wrong = obs_for(slot, unit="utr:dth")
        r = measured(slot, [wrong])
        self.assertEqual(r["populated"], 0, "a dekatherm figure satisfied a dollar slot")
        self.assertEqual(r["validated"], 0)
        self.assertIn(cov.Gate.UNIT_FAMILY, r["refusal_gates"])
        self.assertEqual(r["outcome"], CoverageOutcome.SELECTOR_FAILED)

    def test_right_unit_family_still_satisfies(self):
        """The guard must not be a blanket refusal."""
        slot = slot_for("gas_operating_revenues")
        r = measured(slot, [obs_for(slot, unit="iso4217:USD")])
        self.assertEqual((r["populated"], r["validated"]), (1, 1))

    def test_scope_rule_does_not_admit_an_arbitrary_narrower_actual_scope(self):
        slot = slot_for("gas_operating_revenues", entity="C000020")
        valid = obs_for(slot, unit="iso4217:USD")
        valid["scope_rule"] = slot["scope"]
        valid["scope"] = (
            "FERC filing entity Tennessee Gas Pipeline Company, L.L.C. (C000020); "
            "XBRL entity identifier C000020; regulatory subset filing entity "
            "represented by this XBRL context; consolidated source context "
            "(no explicit/typed dimensions)")
        self.assertEqual(measured(slot, [valid])["populated"], 1)

        narrower = dict(valid, scope="synthetic train 2")
        result = measured(slot, [narrower])
        self.assertEqual(result["populated"], 0)
        self.assertIn(cov.Gate.SCOPE_CONTRACT, result["refusal_gates"])

    def test_migrated_era_unit_spelling_is_accepted(self):
        """`ferc:dth` and `utr:dth` are the same dekatherm; refusing the migrated
        spelling would delete real history rather than reject a wrong value."""
        slot = slot_for("total_throughput")
        r = measured(slot, [obs_for(slot, unit="ferc:dth")])
        self.assertEqual(r["populated"], 1)

    def test_rendered_dimensionless_alternative_is_accepted(self):
        """storage_capacity's rule reads 'utr:dth | pure (rendered p.512-513
        supplies Dth)'. Both alternatives are admissible; taking only the first
        would refuse the rendered figures FERC actually files."""
        fams, _open = cov._slot_unit_contract({"metric_id": "storage_capacity",
                                               "unit_rule": BY_ID["storage_capacity"].unit_rule})
        self.assertIn("energy", fams)
        self.assertIn("fraction", fams)

    def test_metric_unit_contract_beats_an_ambiguous_rule(self):
        """p700_wacc has unit_rule 'percent' but canonical_unit 'fraction' --
        FERC files 0.0913. Reading the rule would refuse the filer's own number."""
        fams, _open = cov._slot_unit_contract({"metric_id": "p700_wacc",
                                               "unit_rule": BY_ID["p700_wacc"].unit_rule})
        self.assertEqual(fams, frozenset({"fraction"}))

    def test_superseded_version_cannot_satisfy_a_current_slot(self):
        slot = slot_for("gas_operating_revenues")
        r = measured(slot, [obs_for(slot, unit="iso4217:USD", version_status="superseded")])
        self.assertEqual(r["populated"], 0, "a superseded filing satisfied a current slot")
        self.assertIn(cov.Gate.SUPERSEDED, r["refusal_gates"])

    def test_non_canonical_filing_cannot_satisfy_a_slot(self):
        slot = slot_for("gas_operating_revenues")
        o = obs_for(slot, unit="iso4217:USD", filing_id="SYNTHETIC-OLD")
        r = measured(slot, [o],
                     canonical={("eCollection_XBRL", "SYNTHETIC-OLD"): False})
        self.assertEqual(r["populated"], 0)
        self.assertIn(cov.Gate.NONCANONICAL, r["refusal_gates"])

    def test_derived_value_whose_input_is_superseded_is_refused(self):
        slot = slot_for("gas_operating_revenues")
        o = obs_for(slot, unit="iso4217:USD", method="derived", source_fact_id=None,
                    obs_id=f"{SYN}-derived-from-superseded")
        r = measured(slot, [o], lineage={o["observation_id"]: ["original", "superseded"]})
        self.assertEqual(r["populated"], 0)
        self.assertIn(cov.Gate.SUPERSEDED, r["refusal_gates"])

    def test_untyped_unit_is_gated_not_counted_and_not_called_missing(self):
        """A value whose unit we cannot place is neither usable nor absent."""
        slot = slot_for("ioc_firm_transport_mdq", regime="Form 549B IOC",
                        basis=periods.SNAPSHOT, as_of="2025-01-01")
        r = measured(slot, [obs_for(slot, unit=None)])
        self.assertEqual(r["populated"], 0)
        self.assertEqual(r["outcome"], CoverageOutcome.INTERPRETATION_BLOCKED)


class TestGrain(unittest.TestCase):
    """A06: grain substitutions the shipped matcher already refused, kept refused."""

    def test_q2_ytd_cannot_satisfy_a_discrete_q2_quarter(self):
        slot = slot_for("gas_operating_revenues", regime="Form 3Q Gas", period="Q2")
        ytd = dict(obs_for(slot, unit="iso4217:USD"),
                   period_basis=periods.YTD,
                   period_start="2025-01-01", period_end="2025-06-30")
        r = measured(slot, [ytd])
        self.assertEqual(r["populated"], 0, "a Q2 year-to-date fact satisfied a Q2 quarter")

    def test_q1_ytd_does_satisfy_q1_because_the_interval_coincides(self):
        slot = slot_for("gas_operating_revenues", regime="Form 3Q Gas", period="Q1")
        ytd = dict(obs_for(slot, unit="iso4217:USD"), period_basis=periods.YTD,
                   period_start="2025-01-01", period_end="2025-03-31")
        r = measured(slot, [ytd])
        self.assertEqual(r["populated"], 1)
        self.assertIn("Q1 interval coincides", r["reason"])

    def test_train_scope_cannot_satisfy_an_entity_scope_slot(self):
        slot = slot_for("gas_operating_revenues")
        train = obs_for(slot, unit="iso4217:USD", scope="train 1 only",
                        obs_id=f"{SYN}-train")
        r = measured(slot, [train])
        self.assertEqual(r["populated"], 0, "a train-level figure satisfied an entity slot")

    def test_a_dated_snapshot_cannot_satisfy_another_date(self):
        slot = slot_for("ioc_firm_transport_mdq", regime="Form 549B IOC",
                        basis=periods.SNAPSHOT, as_of="2025-01-01")
        other = dict(obs_for(slot, unit="Dth/day"), instant_date="2026-01-01")
        r = measured(slot, [other])
        self.assertEqual(r["populated"], 0,
                         "a 2026 snapshot answered a 2025 snapshot slot")


class TestRanking(unittest.TestCase):
    """A06: present admissible values outrank blanks, gates still bind."""

    def test_present_derived_value_beats_blank_filed_placeholder(self):
        slot = slot_for("gas_operating_revenues")
        blank = obs_for(slot, unit=None, value=None, method="filed",
                        availability=Availability.SOURCE_BLANK,
                        validation=Validation.NOT_YET_VALIDATED,
                        obs_id=f"{SYN}-blank-filed")
        real = obs_for(slot, unit="iso4217:USD", method="derived",
                       source_fact_id=None, obs_id=f"{SYN}-derived-present")
        r = measured(slot, [blank, real],
                     lineage={real["observation_id"]: ["original", "original"]})
        self.assertEqual(r["observation_id"], real["observation_id"],
                         "a blank filed placeholder outranked a valid derived value")
        self.assertEqual((r["populated"], r["validated"]), (1, 1))

    def test_a_review_gated_value_beats_a_blank_but_never_counts_as_validated(self):
        slot = slot_for("gas_operating_revenues")
        blank = obs_for(slot, unit=None, value=None,
                        availability=Availability.SOURCE_BLANK,
                        validation=Validation.NOT_YET_VALIDATED,
                        obs_id=f"{SYN}-blank2")
        flagged = obs_for(slot, unit="iso4217:USD",
                          validation=Validation.SOURCE_ANOMALY_REVIEW,
                          obs_id=f"{SYN}-flagged")
        r = measured(slot, [blank, flagged])
        self.assertEqual(r["observation_id"], flagged["observation_id"])
        self.assertEqual(r["populated"], 1)
        self.assertEqual(r["validated"], 0, "a review-gated value was counted as validated")
        self.assertEqual(r["in_review"], 1)

    def test_a_filed_value_still_beats_a_derived_one_at_the_same_tier(self):
        slot = slot_for("gas_operating_revenues")
        filed = obs_for(slot, unit="iso4217:USD", method="filed", obs_id=f"{SYN}-filed")
        derived = obs_for(slot, unit="iso4217:USD", method="derived",
                          obs_id=f"{SYN}-derived")
        r = measured(slot, [derived, filed])
        self.assertEqual(r["observation_id"], filed["observation_id"])

    def test_an_inadmissible_present_value_does_not_displace_the_blank_record(self):
        """Refusing a wrong-unit candidate must not invent data either."""
        slot = slot_for("gas_operating_revenues")
        blank = obs_for(slot, unit=None, value=None,
                        availability=Availability.SOURCE_BLANK,
                        validation=Validation.NOT_YET_VALIDATED, obs_id=f"{SYN}-b3")
        wrong = obs_for(slot, unit="utr:dth", obs_id=f"{SYN}-wrongunit")
        r = measured(slot, [blank, wrong])
        self.assertEqual(r["observation_id"], blank["observation_id"])
        self.assertEqual(r["populated"], 0)
        self.assertIn(cov.Gate.UNIT_FAMILY, r["refusal_gates"])

    def test_derived_without_lineage_is_not_called_source_matched(self):
        slot = slot_for("gas_operating_revenues")
        d = obs_for(slot, unit="iso4217:USD", method="derived", source_fact_id=None,
                    obs_id=f"{SYN}-derived-nolineage")
        r = measured(slot, [d], lineage={})
        self.assertEqual(r["populated"], 1)
        self.assertEqual(r["source_matched"], 0,
                         "a derived value with no lineage was called source-matched")


# ------------------------------------------------------- A05 obligation calendar

def _register(**kw):
    return ObligationRegister(2024, 2026, TODAY, **kw)


class TestObligationCalendar(unittest.TestCase):
    """A05: obligations are independent of whether retrieval succeeded."""

    def test_a_parse_failure_does_not_remove_its_dated_obligations(self):
        healthy = _register()
        for q in ("Q1", "Q2", "Q3", "Q4"):
            healthy.note_occurrence("SYNTHETIC-NB", "Form 549B IOC", 2025, q)
        broken = _register()
        for q in ("Q1", "Q2", "Q3", "Q4"):
            broken.note_occurrence("SYNTHETIC-NB", "Form 549B IOC", 2025, q,
                                   health=SourceHealth.PARSE_FAILED,
                                   detail="SYNTHETIC parser failure", indexed_only=True)
        h = [o for o in healthy.obligations() if o.year == 2025]
        b = [o for o in broken.obligations() if o.year == 2025]
        self.assertEqual(len(h), len(b),
                         "breaking the parser changed how many obligations exist")
        self.assertTrue(all(o.as_of for o in b), "an obligation lost its as-of date")
        self.assertEqual({o.source_health for o in b}, {SourceHealth.PARSE_FAILED})

    def test_breaking_ingestion_cannot_raise_coverage(self):
        """The whole of A05 in one assertion: delete the filing, keep the slot,
        and watch the percentage FALL rather than the denominator shrink."""
        reg = _register()
        for q in ("Q1", "Q2", "Q3", "Q4"):
            reg.note_occurrence("SYNTHETIC-NB", "Form 549B IOC", 2025, q)
        obligations = reg.obligations()
        metrics = [BY_ID["ioc_firm_transport_mdq"]]
        kw = dict(entity_template={"SYNTHETIC-NB": "interstate_gas"},
                  entity_asset={"SYNTHETIC-NB": "synthetic-asset"}, run_id=SYN)
        slots = cov.calendar_slots(obligations, metrics, **kw)
        self.assertTrue(slots)
        good = [obs_for(s, unit="Dth/day", obs_id=f"{SYN}-{s['slot_id']}") for s in slots]
        full = cov.summarise(slots, cov.measure(slots, good, SYN))

        # now break ingestion for one quarter: the bytes were fetched, the parser
        # failed, nothing was produced
        broken_reg = _register()
        for q in ("Q1", "Q2", "Q3", "Q4"):
            broken_reg.note_occurrence(
                "SYNTHETIC-NB", "Form 549B IOC", 2025, q,
                **({"health": SourceHealth.PARSE_FAILED, "indexed_only": True}
                   if q == "Q3" else {}))
        broken_slots = cov.calendar_slots(broken_reg.obligations(), metrics, **kw)
        broken_obs = [o for o, s in zip(good, slots) if s["reporting_period"] != "Q3"]
        broken = cov.summarise(broken_slots, cov.measure(broken_slots, broken_obs, SYN))

        self.assertEqual(broken["expected_core"], full["expected_core"],
                         "a parse failure shrank the denominator")
        self.assertLess(broken["core_validated"], full["core_validated"])
        self.assertLess(broken["core_validated_pct"], full["core_validated_pct"],
                        "breaking ingestion improved the coverage percentage")

    def test_form2_to_form2a_transition_is_a_transition_not_two_duties(self):
        """Fayetteville Express filed Form 2 to FY2023 and Form 2-A from FY2024
        after crossing the 18 CFR 260.1 major threshold downwards."""
        reg = _register()
        for y in range(2016, 2024):
            reg.note_occurrence("SYNTHETIC-FEP", "Form 2", y, "Q4")
        for y in (2024, 2025):
            reg.note_occurrence("SYNTHETIC-FEP", "Form 2A", y, "Q4")
        annual = {(o.year, o.form) for o in reg.obligations()
                  if o.family == "gas_annual"}
        self.assertEqual(annual, {(2024, "Form 2A"), (2025, "Form 2A"), (2026, "Form 2A")})
        self.assertNotIn((2024, "Form 2"), annual,
                         "a Form 2 duty was still expected after the filer moved to 2-A")

    def test_a_future_obligation_is_not_yet_due_and_is_not_a_miss(self):
        reg = _register()
        reg.note_occurrence("SYNTHETIC-FEP", "Form 2A", 2025, "Q4")
        fut = [o for o in reg.obligations() if o.year == 2026 and o.form == "Form 2A"]
        self.assertTrue(fut)
        self.assertEqual(fut[0].state, "future_not_due")
        self.assertEqual(fut[0].due_date, "2027-04-19")

    def test_a_nonmajor_filer_gets_the_seventy_day_quarterly_deadline(self):
        reg = _register()
        reg.note_occurrence("SYNTHETIC-FEP", "Form 2A", 2024, "Q4")
        reg.note_occurrence("SYNTHETIC-FEP", "Form 3Q Gas", 2024, "Q1")
        q1 = [o for o in reg.obligations()
              if o.form == "Form 3Q Gas" and o.year == 2024 and o.period == "Q1"][0]
        self.assertEqual(q1.due_date, "2024-06-10")     # 31 Mar + 70 days, weekend-adjusted

    def test_a_quarterly_form_has_no_fourth_quarter(self):
        reg = _register()
        reg.note_occurrence("SYNTHETIC-X", "Form 3Q Gas", 2025, "Q1")
        qs = {o.period for o in reg.obligations() if o.form == "Form 3Q Gas"}
        self.assertEqual(qs, {"Q1", "Q2", "Q3"},
                         "a Q4 quarterly slot was invented; the annual report covers Q4")

    def test_an_unevidenced_year_is_unknown_not_absent_and_not_required(self):
        reg = _register()
        reg.note_roster("SYNTHETIC-R", ["Form 2"])
        obs = [o for o in reg.obligations() if o.entity_key == "SYNTHETIC-R"]
        self.assertTrue(obs, "a roster-declared filer produced no obligation at all")
        self.assertEqual({o.evidence_kind for o in obs},
                         {EvidenceKind.ROSTER_DECLARED})
        slots = cov.calendar_slots(
            obs, [BY_ID["gas_operating_revenues"]],
            entity_template={"SYNTHETIC-R": "interstate_gas"},
            entity_asset={"SYNTHETIC-R": "a"}, run_id=SYN)
        self.assertTrue(slots)
        self.assertEqual({s["requirement"] for s in slots}, {cov.UNKNOWN})

    def test_an_undated_snapshot_slot_is_never_emitted(self):
        """An undated snapshot slot is satisfiable by any date, so it measures
        nothing and must not exist. It is reported as an unresolved obligation."""
        reg = _register()
        reg.note_occurrence("SYNTHETIC-NOAS", "Form 549B Capacity", 2025, "Q4")
        obs = reg.obligations()
        self.assertTrue(all(not o.as_of for o in obs))
        slots = cov.calendar_slots(
            obs, [BY_ID["cap_reported_capacity"]],
            entity_template={"SYNTHETIC-NOAS": "interstate_gas"},
            entity_asset={"SYNTHETIC-NOAS": "a"}, run_id=SYN)
        snapshot = [s for s in slots
                    if s["period_basis"] in (periods.SNAPSHOT, periods.AS_OF)]
        self.assertEqual(snapshot, [], "an undated snapshot slot was emitted")

    def test_reconcile_never_deletes_a_shipped_slot(self):
        shipped = [slot_for("gas_operating_revenues", entity="SYNTHETIC-OLD", year=2016)]
        merged, bridge = cov.reconcile_expected([], shipped)
        self.assertEqual(len(merged), 1, "reconciliation dropped a shipped slot")
        self.assertEqual(bridge[0]["disposition"], "retained_outside_calendar")


class TestIOCUnitContracts(unittest.TestCase):
    """Cases w2-ioc raised. Each asserts the gate treats like causes alike."""

    def test_categorical_point_census_is_admitted_but_never_validates(self):
        """ioc_points now files unit='codes' with the count moved to
        candidate_count. The family gate must admit it -- and scope_incompatible
        must still keep it out of the validated count."""
        slot = slot_for("ioc_points", regime="Form 549B IOC",
                        basis=periods.SNAPSHOT, as_of="2025-01-01")
        o = obs_for(slot, unit="codes", value="M2=310, MQ=310",
                    validation=Validation.SCOPE_INCOMPATIBLE)
        r = measured(slot, [o])
        self.assertEqual(r["populated"], 1, "a categorical census was refused on units")
        self.assertEqual(r["validated"], 0,
                         "a scope-incompatible value was counted as validated")
        self.assertEqual(r["in_review"], 1)

    def test_declared_admissible_units_widen_the_gate_but_only_as_declared(self):
        """`admissible_units` is a factual claim about what FERC files, not a
        wildcard. ioc_expiry_profile legitimately carries a per-day rate and a
        quantity; a dollar figure is still refused."""
        slot = slot_for("ioc_expiry_profile", regime="Form 549B IOC",
                        basis=periods.SNAPSHOT, as_of="2025-01-01")
        fams, open_rule = cov._slot_unit_contract(slot)
        self.assertFalse(open_rule)
        self.assertEqual(fams, frozenset({"energy_rate", "energy"}))
        for unit in ("Dth/day", "Dth", "MMBtu/day"):
            with self.subTest(unit=unit):
                self.assertEqual(measured(slot, [obs_for(slot, unit=unit)])["populated"], 1)
        self.assertEqual(measured(slot, [obs_for(slot, unit="iso4217:USD")])["populated"], 0,
                         "a declared-units entry became a wildcard")

    def test_a_rate_cannot_answer_a_storage_quantity_slot(self):
        """Admitting both dimensions is only safe because the weight is named in
        scope, and scope is part of the slot key. If that ever stopped being
        true, a per-day rate could satisfy a quantity slot -- so assert it."""
        storage = slot_for("ioc_expiry_profile", regime="Form 549B IOC",
                           basis=periods.SNAPSHOT, as_of="2025-01-01")
        storage["scope"] = ("matched contracts only | remaining primary term bucket 0-1y "
                            "| weight: contracted storage quantity (D item p)")
        rate = obs_for(storage, unit="Dth/day")
        rate["scope"] = ("matched contracts only | remaining primary term bucket 0-1y "
                         "| weight: transportation MDQ (D item o)")
        self.assertEqual(measured(storage, [rate])["populated"], 0,
                         "a transportation rate satisfied a storage quantity slot")

    def test_a_volume_against_an_energy_metric_is_gated_not_called_a_defect(self):
        """w2-ioc's new live case: a filer reporting IOC header item `g` as `F`
        files a storage quantity in Mcf against a metric declaring dekatherms.
        Relating a gas volume to a gas energy needs a heat content that is not on
        the record, so the value is refused -- but it is a real quantity in a unit
        we cannot convert, not a wrong value and not our selector failing."""
        slot = slot_for("ioc_expiry_profile", regime="Form 549B IOC",
                        basis=periods.SNAPSHOT, as_of="2025-01-01")
        r = measured(slot, [obs_for(slot, unit="Mcf")])
        self.assertEqual(r["populated"], 0)
        self.assertIn(cov.Gate.UNIT_UNRELATABLE, r["refusal_gates"])
        self.assertEqual(r["outcome"], CoverageOutcome.INTERPRETATION_BLOCKED,
                         "a filer's unit declaration was reported as our defect")

    def test_an_unrelated_dimension_is_still_a_defect(self):
        """The distinction must not become a blanket excuse: no constant relates
        dollars to dekatherms, so that stays a defect."""
        slot = slot_for("ioc_expiry_profile", regime="Form 549B IOC",
                        basis=periods.SNAPSHOT, as_of="2025-01-01")
        r = measured(slot, [obs_for(slot, unit="iso4217:USD")])
        self.assertEqual(r["populated"], 0)
        self.assertIn(cov.Gate.UNIT_FAMILY, r["refusal_gates"])
        self.assertEqual(r["outcome"], CoverageOutcome.SELECTOR_FAILED)

    def test_no_conversion_is_ever_invented_to_satisfy_a_slot(self):
        """The whole point of the gate: an Mcf value must not become a Dth value."""
        slot = slot_for("ioc_expiry_profile", regime="Form 549B IOC",
                        basis=periods.SNAPSHOT, as_of="2025-01-01")
        r = measured(slot, [obs_for(slot, unit="Mcf", value="1000")])
        self.assertEqual(r["observation_id"], None,
                         "a volume was converted into an energy to fill a slot")
        self.assertEqual(r["validated"], 0)

    def test_a_bucketing_qualifier_does_not_disable_the_unit_check(self):
        """'Dth/day by bucket' is dekatherms per day, reported per bucket. Reading
        the qualifier as an open rule removed unit checking from the metric, so
        ioc_expiry_profile admitted unitless values while ioc_firm_transport_mdq
        refused them for exactly the same reason."""
        # Tested on the RULE PARSER alone, deliberately: `_slot_unit_contract`
        # also folds in the registry's declared `admissible_units`, and this
        # property must hold independently of any such declaration.
        fams, open_rule = cov.admissible_unit_families(
            BY_ID["ioc_expiry_profile"].unit_rule)
        self.assertFalse(open_rule, "a bucketing qualifier made the rule open")
        self.assertEqual(fams, frozenset({"energy_rate"}))

    def test_unitless_values_are_gated_the_same_way_across_ioc_metrics(self):
        """The filer left the UOM header blank. Same cause, same treatment, on
        every metric it touches."""
        for mid in ("ioc_firm_transport_mdq", "ioc_mdq_change",
                    "ioc_contracted_storage_quantity", "ioc_expiry_profile"):
            with self.subTest(metric=mid):
                slot = slot_for(mid, regime="Form 549B IOC",
                                basis=periods.SNAPSHOT, as_of="2025-01-01")
                r = measured(slot, [obs_for(slot, unit=None)])
                self.assertEqual(r["populated"], 0)
                self.assertEqual(r["outcome"], CoverageOutcome.INTERPRETATION_BLOCKED,
                                 f"{mid} did not gate an unstated unit")

    def test_a_corrupt_cache_object_is_not_a_parse_failure_and_not_a_ferc_gap(self):
        reg = _register()
        reg.note_occurrence("SYNTHETIC-CC", "Form 549B IOC", 2025, "Q4",
                            health=SourceHealth.CACHE_CORRUPT, indexed_only=True,
                            detail="SYNTHETIC: cached object's bytes do not hash to its name")
        slots = cov.calendar_slots(
            reg.obligations(), [BY_ID["ioc_firm_transport_mdq"]],
            entity_template={"SYNTHETIC-CC": "interstate_gas"},
            entity_asset={"SYNTHETIC-CC": "a"}, run_id=SYN)
        q4 = [s for s in slots if s["reporting_period"] == "Q4"]
        self.assertTrue(q4, "a corrupt cache object removed its own obligation")
        self.assertEqual(q4[0]["source_health"], SourceHealth.CACHE_CORRUPT)
        r = measured(q4[0], [])
        self.assertNotEqual(r["outcome"], CoverageOutcome.PARSE_FAILED,
                            "our storage failure was blamed on the parser")
        self.assertNotEqual(r["outcome"], CoverageOutcome.SOURCE_BLANK,
                            "our storage failure was blamed on FERC")
        self.assertEqual(r["outcome"], CoverageOutcome.RETRIEVAL_FAILED)


class TestBarrelMileUnitRule(unittest.TestCase):
    """w3-financial's R10 rule, locked in against a later tidy-up.

    The Form 6 taxonomy declares no barrel-mile unit, so every filer reports the
    concept untyped -- that is a vocabulary fact and the rule should admit it.
    But two observations carry `utr:bbl` on a barrel-MILE concept with
    barrel-mile magnitudes: a filer's wrong unit declaration, not a spelling
    variant. Widening the rule to accept every unit observed would turn coverage
    green by cementing a dimensional error as validated.
    """

    PROPOSED = ("barrel-miles | xbrli:pure | pure (the Form 6 taxonomy declares no "
                "barrel-mile unit, so every filer reports these concepts untyped)")

    def _admit(self, unit):
        slot = {"metric_id": "p700_barrel_miles", "unit_rule": self.PROPOSED}
        return cov.admissible(slot, {
            "availability": Availability.PRESENT, "unit": unit,
            "validation": Validation.PASS, "version_status": "original",
            "method": "filed", "observation_id": "x", "filing_id": None})

    def test_untyped_barrel_miles_are_admitted(self):
        for unit in ("xbrli:pure", "pure", "barrel-miles"):
            with self.subTest(unit=unit):
                self.assertTrue(self._admit(unit)[0])

    def test_a_barrels_unit_on_a_barrel_mile_concept_stays_refused(self):
        ok, gate = self._admit("utr:bbl")
        self.assertFalse(ok, "a barrels-declared barrel-mile value was admitted; "
                             "a barrel is not a barrel-mile")
        self.assertEqual(gate, cov.Gate.UNIT_FAMILY)


class TestPersistence(unittest.TestCase):
    """The repaired schema persists every calendar/evidence column explicitly."""

    def _slots(self):
        reg = _register()
        reg.note_occurrence("SYNTHETIC-P", "Form 549B IOC", 2025, "Q1",
                            health=SourceHealth.PARSE_FAILED, indexed_only=True)
        return cov.calendar_slots(
            reg.obligations(), [BY_ID["ioc_firm_transport_mdq"]],
            entity_template={"SYNTHETIC-P": "interstate_gas"},
            entity_asset={"SYNTHETIC-P": "a"}, run_id=SYN)

    def test_projection_fits_the_repaired_schema_without_evidence_loss(self):
        import sqlite3
        slots = self._slots()
        rows, dropped = cov.project(slots, cov.EXPECTED_COLUMNS)
        con = sqlite3.connect(":memory:")
        con.executescript((ROOT / "ferclib" / "schema.sql").read_text())
        cols = list(rows[0])
        con.executemany(
            f"INSERT INTO coverage_expected ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})",
            [tuple(r[c] for c in cols) for r in rows])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM coverage_expected").fetchone()[0],
                         len(rows))
        self.assertEqual([], dropped)
        persisted = con.execute(
            "SELECT source_health,due_date,slot_state FROM coverage_expected").fetchone()
        self.assertEqual("parse_failed", persisted[0])
        self.assertEqual(slots[0]["due_date"], persisted[1])
        self.assertEqual(slots[0]["slot_state"], persisted[2])

    def test_projection_retains_calendar_columns_and_reports_unknown_keys(self):
        slots = self._slots()
        slots[0]["unexpected_future_column"] = "must not disappear silently"
        rows, dropped = cov.project(slots, cov.EXPECTED_COLUMNS)
        self.assertEqual("parse_failed", rows[0]["source_health"])
        self.assertIn("due_date", rows[0])
        self.assertIn("slot_state", rows[0])
        self.assertEqual(["unexpected_future_column"], dropped,
                         "only an actually unknown column may be projected away")

    def test_measured_row_keeps_failure_and_refusal_evidence(self):
        slots = self._slots()
        m = cov.measure(slots, [], SYN)
        rows, _dropped = cov.project(m, cov.MEASURED_COLUMNS)
        self.assertEqual(rows[0]["outcome"], CoverageOutcome.PARSE_FAILED)
        self.assertEqual(rows[0]["validated"], 0)
        self.assertIn("candidates_refused", rows[0])
        self.assertIn("refusal_gates", rows[0])


class TestDocumentSlots(unittest.TestCase):
    """A05/A06: document expectations are evidenced and dated, never invented."""

    def test_no_slot_is_invented_for_an_undated_occurrence(self):
        occ = [{"entity_key": "SYNTHETIC-D", "template": "lng", "metric_id": "lng_inspection",
                "as_of": "", "docket": "CP15-88-000"}]
        self.assertEqual(cov.document_slots(occ, BY_ID, run_id=SYN), [])

    def test_a_dated_document_slot_matches_only_its_own_dated_fact(self):
        occ = [{"entity_key": "SYNTHETIC-D", "template": "lng",
                "metric_id": "lng_material_order", "as_of": "2025-03-14",
                "docket": "CP15-88-000", "authority": "NGA section 3 order"}]
        slots = cov.document_slots(occ, BY_ID, run_id=SYN)
        self.assertEqual(len(slots), 1)
        slot = slots[0]
        self.assertEqual(slot["instant_date"], "2025-03-14")
        other = dict(obs_for(slot, unit="categorical"), instant_date="2025-06-01")
        self.assertEqual(measured(slot, [other])["populated"], 0,
                         "a different date's order satisfied this document slot")
        self.assertEqual(measured(slot, [obs_for(slot, unit="categorical")])["populated"], 1)

    def test_a_fixed_number_of_documents_is_never_required(self):
        """Three orders in one year and none in the next is not 100% then 0%:
        there is no FERC duty to file N orders, so there are N slots, evidenced."""
        occ = [{"entity_key": "SYNTHETIC-D", "template": "lng",
                "metric_id": "lng_material_order", "as_of": d,
                "docket": "CP15-88-000"} for d in ("2025-01-05", "2025-06-06", "2025-09-09")]
        slots = cov.document_slots(occ, BY_ID, run_id=SYN)
        self.assertEqual(len(slots), 3)
        self.assertEqual({s["requirement"] for s in slots}, {cov.CONDITIONAL})


# --------------------------------------------------------------- A07 readiness

class TestFieldReadiness(unittest.TestCase):
    """A07: readiness is computed inside the template that requests the field."""

    def setUp(self):
        sys.path.insert(0, str(ROOT))
        import build_field_status
        self.bfs = build_field_status

    def test_cross_template_passes_cannot_make_a_template_ready(self):
        m = BY_ID["storage_capacity"]
        counts = Counter()                       # nothing present in gas_storage
        counts[(Availability.PRESENT, Validation.SOURCE_ANOMALY_REVIEW)] = 2
        outcome, impl, evidence, blocker = self.bfs.classify(
            m, "gas_storage", counts, 9, Counter({cov.REQUIRED: 7}), [])
        self.assertNotEqual(outcome, "implemented_retrieved_validated",
                            "a review-only storage field was marked ready")
        self.assertEqual(outcome, "source_exists_interpretation_blocked")

    def test_a_local_pass_does_mark_the_template_ready(self):
        m = BY_ID["storage_capacity"]
        counts = Counter({(Availability.PRESENT, Validation.PASS): 3})
        outcome, *_ = self.bfs.classify(m, "gas_storage", counts, 9,
                                        Counter({cov.REQUIRED: 7}), [])
        self.assertEqual(outcome, "implemented_retrieved_validated")

    def test_no_local_observation_is_unfinished_work_not_a_ferc_gap(self):
        m = BY_ID["storage_injections_own"]
        outcome, impl, evidence, blocker = self.bfs.classify(
            m, "gas_storage", Counter(), 0, Counter({cov.REQUIRED: 7}), [])
        self.assertEqual(outcome, "not_implemented_unfinished_work")
        self.assertIn("7 slots are requested", evidence)

    def test_a_field_the_form_does_not_collect_is_not_unfinished_work(self):
        m = BY_ID["storage_capacity"]
        outcome, impl, *_ = self.bfs.classify(
            m, "gas_storage", Counter(), 0, Counter({cov.NOT_REQUIRED: 4}), [])
        self.assertEqual(outcome, "implemented_source_blank_or_not_required")
        self.assertEqual(impl, "implemented")

    def test_expected_not_located_is_not_reported_as_a_source_blank(self):
        m = BY_ID["refund_exposure_window"]
        counts = Counter({(Availability.EXPECTED_NOT_LOCATED,
                           Validation.NOT_YET_VALIDATED): 9})
        outcome, impl, evidence, _b = self.bfs.classify(
            m, "gas_storage", counts, 9, Counter({cov.REQUIRED: 9}), [])
        self.assertEqual(outcome, "retrieval_or_access_failure")
        self.assertIn("not proof of non-compliance", evidence)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPermissiveDefaults(unittest.TestCase):
    """Six defaults audited with w2-ioc's tell: permissive, or undetermined?

    Each of these asserts that a default now expresses "not established" rather
    than quietly asserting a fact nobody checked.
    """

    def test_unlineaged_derived_value_is_not_source_matched_when_unchecked(self):
        """The occupied one. `lineage is None` used to mean "passes"; the
        docstring already said it should mean "unchecked"."""
        slot = slot_for("gas_operating_revenues")
        d = obs_for(slot, unit="iso4217:USD", method="derived", source_fact_id=None,
                    obs_id=f"{SYN}-derived-unchecked")
        r = measured(slot, [d])                      # no lineage map supplied
        self.assertEqual(r["populated"], 1)
        self.assertEqual(r["source_matched"], 0,
                         "an unchecked derived value was counted as source-matched")
        self.assertIn("lineage NOT CHECKED", r["evidence"])

    def test_an_unclassified_entity_is_refused_not_guessed_and_not_dropped(self):
        reg = _register()
        reg.note_occurrence("SYNTHETIC-NOTEMPLATE", "Form 2", 2025, "Q4")
        with self.assertRaises(ValueError) as ctx:
            cov.calendar_slots(reg.obligations(), [BY_ID["gas_operating_revenues"]],
                               entity_template={}, entity_asset={}, run_id=SYN)
        self.assertIn("no template mapping", str(ctx.exception))

    def test_a_form_with_no_declared_calendar_is_refused_not_silently_empty(self):
        from ferclib import applicability as ap
        reg = _register()
        reg.note_occurrence("SYNTHETIC-NP", "Form 2", 2025, "Q4")
        saved = ap.FORM_PERIODS.pop("Form 2")
        try:
            with self.assertRaises(ValueError) as ctx:
                reg.obligations()
            self.assertIn("no\nFORM_PERIODS".replace("\n", " "), str(ctx.exception))
        finally:
            ap.FORM_PERIODS["Form 2"] = saved

    def test_unrecorded_retrieval_health_is_not_reported_as_an_empty_index(self):
        reg = _register()
        reg.note_occurrence("SYNTHETIC-H", "Form 2", 2025, "Q4")   # no health given
        ob = [o for o in reg.obligations() if o.year == 2025][0]
        self.assertEqual(ob.source_health, SourceHealth.HEALTH_NOT_RECORDED,
                         "a missing dictionary key became a finding about FERC's index")
        self.assertIn(SourceHealth.HEALTH_NOT_RECORDED, SourceHealth.UNDETERMINED)
        self.assertNotIn(SourceHealth.HEALTH_NOT_RECORDED, SourceHealth.DEFECT)

    def test_a_document_slot_asserts_no_authority_it_was_not_given(self):
        occ = [{"entity_key": "SYNTHETIC-D", "template": "lng",
                "metric_id": "lng_material_order", "as_of": "2025-03-14"}]
        slot = cov.document_slots(occ, BY_ID, run_id=SYN)[0]
        self.assertEqual(slot["obligation_authority"], "authority unresolved")
        self.assertEqual(slot["source_health"], SourceHealth.HEALTH_NOT_RECORDED,
                         "an unstated retrieval outcome was reported as OK")

    def test_occurrences_with_no_periodic_family_are_counted_not_dropped(self):
        reg = _register()
        reg.note_occurrence("SYNTHETIC-I", "eLibrary document", 2025, "as_of")
        reg.note_occurrence("SYNTHETIC-I", "Form 404 Nonexistent", 2025, "Q4")
        self.assertEqual(dict(reg.ignored_occurrences),
                         {"eLibrary document": 1, "Form 404 Nonexistent": 1})


class TestAggregationBasisIsNotAUnit(unittest.TestCase):
    """`i311_contract_expiry`'s rule is `"by weight"`, which names the aggregation
    basis of a distribution, not the unit of its values. Treating it as an open
    rule disarmed the unit check for the metric entirely -- the same permissive
    default this suite exists to catch, in the regex written to catch them.

    This test also guards a restore hazard: the fix was overwritten once by a
    release-tree restore, so it needs an assertion, not a comment.
    """

    def test_by_weight_is_not_treated_as_an_open_unit_rule(self):
        fams, is_open = cov._slot_unit_contract(
            {"metric_id": "i311_contract_expiry",
             "unit_rule": BY_ID["i311_contract_expiry"].unit_rule})
        self.assertFalse(is_open, "'by weight' switched the unit check off again")
        self.assertEqual(fams, frozenset())

    def test_its_values_are_gated_as_an_unresolved_contract(self):
        slot = slot_for("i311_contract_expiry", regime="Form 549D",
                        basis=periods.QUARTER, template="intrastate_549d")
        r = measured(slot, [obs_for(slot, unit="contract rows and Usage_BU, both stated")])
        self.assertEqual(r["populated"], 0)
        self.assertIn(cov.Gate.UNIT_RULE_UNRESOLVED, r["refusal_gates"])
        self.assertEqual(r["outcome"], CoverageOutcome.INTERPRETATION_BLOCKED)

    def test_genuinely_open_rules_are_still_open(self):
        """The narrowing must not disarm the deliberate cases."""
        for mid in ("profile_miles_narrative", "tariff_operative_rate",
                    "single_day_peak_date"):
            with self.subTest(metric=mid):
                _f, is_open = cov._slot_unit_contract(
                    {"metric_id": mid, "unit_rule": BY_ID[mid].unit_rule})
                self.assertTrue(is_open, f"{mid} lost its deliberate open rule")
