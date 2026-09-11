"""
Regression tests for the two document adapters.

Every test here is a claim about behaviour that, if it broke, would put a false
number or a false status in front of a reader. They run against the pure
extraction/classification functions AND against the staging database the live
runs produced, so a passing suite means the rules held on real FERC documents
rather than on fixtures.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters import elibrary_docs as docs                                  # noqa: E402
from adapters import lng                                                     # noqa: E402
from ferclib import elibrary                                                 # noqa: E402
from ferclib.status import Availability, Validation                          # noqa: E402

DB = pathlib.Path(os.environ.get(
    "FERC_STAGING_DB", str(ROOT / "staging" / "operating_assets.sqlite")))

SABINE_SPL = "NO-FERC-CID:Sabine Pass Liquefaction, LLC"
SABINE_LP = "NO-FERC-CID:Sabine Pass LNG, L.P."
CCL = "NO-FERC-CID:Corpus Christi Liquefaction, LLC"
ELBA_LIQ = "NO-FERC-CID:Elba Liquefaction Company, L.L.C."
SOUTHERN_LNG = "C000039"


def rows(sql, params=()):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


def obs(**where):
    clause = " AND ".join(f"{k}=?" for k in where)
    return rows(f"SELECT * FROM observations WHERE source_regime='eLibrary document'"
                + (f" AND {clause}" if clause else ""), tuple(where.values()))


class StagedRun(unittest.TestCase):
    """Skips rather than lies when the shared database has not been populated."""

    @classmethod
    def setUpClass(cls):
        if not DB.is_file():
            raise unittest.SkipTest(f"staging database not present at {DB}")
        if not obs():
            raise unittest.SkipTest("no eLibrary document observations staged; run the "
                                    "lng and elibrary_docs adapters first")


# ============================================================ LNG capacity

class TestLiquefactionNeverSubstitutedByRegas(StagedRun):
    """A regasification figure may never appear as a liquefaction capacity.

    Sabine Pass's 2.6 Bcf/d is a REGAS figure. Presenting it as liquefaction
    would be a category error of about an order of magnitude, in the wrong units.
    """

    SABINE_REGAS_SENTENCE = ("The project will import, store, and vaporize an average of "
                             "approximately 2.6 billion cubic feet per day (Bcf/d) of LNG, "
                             "with a total plant capacity of 2.8 Bcf/d.")
    CCL_SENTENCE = ("the terminal will have the capability to liquefy for export "
                    "approximately 15 million metric tons per annum (MMTPA) of LNG and to "
                    "vaporize approximately 400 million cubic feet (MMcf) per day of "
                    "imported natural gas")

    def test_regas_sentence_yields_no_liquefaction_rule_match(self):
        matched = {mid for mid, _role, rx, _n in lng.CAPACITY_RULES
                   if rx.search(self.SABINE_REGAS_SENTENCE)}
        self.assertIn("lng_regas_sendout_capacity", matched)
        self.assertNotIn("lng_liquefaction_capacity", matched)

    def test_one_order_yields_both_assertions_separately(self):
        found = {}
        for mid, _role, rx, _n in lng.CAPACITY_RULES:
            m = rx.search(self.CCL_SENTENCE)
            if m:
                found.setdefault(mid, m.group(2))
        self.assertEqual(found.get("lng_liquefaction_capacity"), "15")
        self.assertEqual(found.get("lng_regas_sendout_capacity"), "400")

    def test_unit_families_are_disjoint_between_liquefaction_and_regas(self):
        _l, liq = lng.UNIT_FAMILY["lng_liquefaction_capacity"]
        _r, regas = lng.UNIT_FAMILY["lng_regas_sendout_capacity"]
        self.assertEqual(liq & regas, set())
        for volume_unit in ("Bcf", "MMcf", "million cubic feet"):
            self.assertNotIn(lng._unit_key(volume_unit), liq,
                             f"{volume_unit} must never satisfy a liquefaction capacity")
        for mass_unit in ("MTPA", "MMTPA", "million metric tons per annum"):
            self.assertNotIn(lng._unit_key(mass_unit), regas,
                             f"{mass_unit} must never satisfy a regasification capacity")

    def test_regas_only_terminal_has_no_liquefaction_value_staged(self):
        liq = obs(entity_key=SABINE_LP, metric_id="lng_liquefaction_capacity")
        self.assertTrue(liq, "the slot must be accounted for, not silently absent")
        for o in liq:
            self.assertIsNone(o["value_num"])
            self.assertEqual(o["availability"], Availability.NOT_APPLICABLE)
        regas = [o for o in obs(entity_key=SABINE_LP, metric_id="lng_regas_sendout_capacity")
                 if o["availability"] == Availability.PRESENT]
        self.assertTrue(regas, "the 2.6 Bcf/d regas figure should be staged")
        for o in regas:
            self.assertIn("cubic feet", (o["unit"] or "").lower())

    def test_staged_capacities_carry_the_right_unit_family(self):
        for metric, (_label, family) in lng.UNIT_FAMILY.items():
            for o in obs(metric_id=metric):
                if o["availability"] != Availability.PRESENT:
                    continue
                self.assertIn(lng._unit_key(o["unit"]), family,
                              f"{metric} staged with unit {o['unit']!r}")


class TestAmendmentsKeptAsASequence(StagedRun):
    """Elba 2.5 -> 2.9 MTPA. Both survive; the earlier one is marked superseded."""

    def test_elba_liquefaction_sequence(self):
        vals = {o["value_text"]: o for o in obs(entity_key=ELBA_LIQ,
                                                metric_id="lng_liquefaction_capacity")
                if o["value_text"]}
        self.assertIn("2.5 MTPA", vals)
        self.assertIn("2.9 MTPA", vals)
        self.assertEqual(vals["2.5 MTPA"]["version_status"], "superseded")
        self.assertEqual(vals["2.9 MTPA"]["version_status"], "original")
        self.assertIn("amend", vals["2.5 MTPA"]["qa_flags"].lower())


class TestVacaturIsNotCurrentCapacity(StagedRun):
    """Elba III was vacated in part in 2011: its 2007 figures are not assertable."""

    def test_southern_lng_capacities_are_unresolved_not_asserted(self):
        pool = [o for o in obs(entity_key=SOUTHERN_LNG)
                if o["metric_id"] in ("lng_regas_sendout_capacity", "lng_storage_capacity")]
        self.assertTrue(pool)
        asserted = [o for o in pool if o["availability"] == Availability.PRESENT]
        self.assertEqual(asserted, [],
                         "no Elba III capacity may be asserted while the 2011 vacatur is "
                         "unreconciled")
        blocked = [o for o in pool if o["validation"] == Validation.BLOCKED_AMBIGUITY]
        self.assertTrue(blocked)
        for o in blocked:
            self.assertIn("vacat", (o["missing_reason"] or "").lower())

    def test_the_vacated_values_are_still_retained_as_document_facts(self):
        kept = rows("SELECT * FROM document_facts WHERE entity_key=? AND metric_id LIKE 'lng%'",
                    (SOUTHERN_LNG,))
        self.assertTrue(kept, "the retrieved figures must be retained, not discarded")
        self.assertTrue(any(f["value_text"] and "Bcf" in f["value_text"] for f in kept))


# ============================================================ LNG status

class TestThreeStatusStatesDistinct(StagedRun):
    """requested / authorised to enter service / operation reported are three
    separate assertions and never substitute for one another."""

    METRICS = ("lng_status_requested", "lng_status_authorised", "lng_status_operating")

    def test_all_three_are_answered_separately_for_sabine(self):
        """Each state is ANSWERED -- with a value or an explicit reason.

        This replaces a test that required all three to be PRESENT. That
        requirement was the A03 defect's engine: on this evidence the only way to
        make `lng_status_operating` PRESENT for Sabine Pass is to treat the
        existence of a filed semi-annual report as proof of operation, which is
        exactly the 35-row substitution. Sabine files its reports, but the public
        accession is the cover letter and the operating figures are CUI//PRIV in
        the non-public twin, so no output figure has been read.

        The intent -- three separate assertions that never substitute -- survives
        intact. Only "each is PRESENT" was wrong.
        """
        by_metric = {m: obs(entity_key=SABINE_SPL, metric_id=m) for m in self.METRICS}
        for m, pool in by_metric.items():
            self.assertTrue(pool, f"{m} produced no observation at all")
            for o in pool:
                self.assertTrue(o["availability"], f"{m} has no availability")
                if o["availability"] != Availability.PRESENT:
                    self.assertTrue(o["missing_reason"],
                                    f"{m} is unavailable and does not say why")
        ids = [o["observation_id"] for p in by_metric.values() for o in p]
        self.assertEqual(len(ids), len(set(ids)),
                         "status observations collided on identity")

    def test_operation_is_never_satisfied_by_an_authorisation_or_a_report(self):
        """The A03 defect, stated as a rule rather than implied by a count."""
        for o in obs(metric_id="lng_status_operating"):
            if o["availability"] != Availability.PRESENT:
                # an unavailable operating state must name what it does NOT prove
                self.assertRegex(o["missing_reason"] or "",
                                 r"(?i)not evidence that the facility operated|"
                                 r"no semi-annual operational report was located")
                continue
            # a PRESENT operating claim must quote a retrieved BODY. A report's
            # existence is reporting continuity, not operation.
            self.assertIsNotNone(o["accession_number"])
            self.assertIn("span", (o["scope"] or "") + (o["qa_flags"] or ""))

    def test_authorisation_is_not_evidence_of_operation(self):
        auth = obs(entity_key=SABINE_SPL, metric_id="lng_status_authorised")
        for o in auth:
            if o["availability"] != Availability.PRESENT:
                continue
            self.assertIn("not proof of an actual start", o["qa_flags"])
            self.assertNotIn("operation reported", (o["value_text"] or "").lower())

    def test_authorised_dates_are_a_sequence_not_a_single_cod(self):
        dates = sorted({o["instant_date"] for o in obs(entity_key=SABINE_SPL,
                                                       metric_id="lng_status_authorised")
                        if o["availability"] == Availability.PRESENT})
        self.assertGreater(len(dates), 1,
                           "per-component in-service orders are a sequence, not one COD")

    def test_authorised_facility_that_never_operated_reports_no_operation(self):
        """Gulf LNG Liquefaction holds a 2019 authorisation and has never been
        built. Nothing in this universe may report it as operating."""
        for o in obs(metric_id="lng_status_operating"):
            if o["availability"] != Availability.PRESENT:
                continue
            # The requirement is EVIDENCE, not a phrase. This previously demanded
            # the literal "operation reported" in value_text, which passed
            # vacuously while no such row existed and would have failed the
            # correct row once one appeared -- a body-quoting operating figure
            # does not contain that phrase. A latent trap, removed.
            self.assertIsNotNone(o["accession_number"],
                                 "an operating claim must cite the report it came from")
            self.assertIn("span", (o["scope"] or "") + (o["qa_flags"] or ""),
                          "an operating claim must cite a span from a retrieved body")


class TestObligationIsSourcedFromTheOrder(StagedRun):
    """The semi-annual obligation comes from the facility's own NGA s.3 order
    condition, never from a Class/Type."""

    def test_284_126g_is_not_an_lng_obligation(self):
        sweep = {(ct["documentClass"], ct["documentType"]) for ct in lng.SWEEP_TYPES}
        self.assertIn(("Report/Form", "Certificate of Compliance Report"), sweep)
        self.assertNotIn(("Report/Form", "284.126 (g) Semi-Annual Storage Report"), sweep,
                         "284.126(g) is a Part 284 storage-provider type, not an LNG route")
        self.assertEqual(lng.STORAGE_REPORT_TYPE,
                         ("Report/Form", "284.126 (g) Semi-Annual Storage Report"))

    def test_the_correction_is_recorded_on_the_observations(self):
        pool = obs(metric_id="lng_operational_report")
        self.assertTrue(pool)
        carriers = [o for o in pool
                    if "284.126" in ((o["applicability_evidence"] or "")
                                     + (o["qa_flags"] or "") + (o["missing_reason"] or ""))]
        self.assertTrue(carriers, "the 284.126(g) correction must travel with the metric")

    def test_a_284_126g_typed_report_is_flagged_not_trusted(self):
        """The one LNG report FERC's own filer typed 284.126(g) is a
        mis-classification, and the adapter must say so rather than adopt it."""
        self.assertIn("mis-classification", lng.CORRECTION_284_126G.lower()
                      .replace("contradicted", "mis-classification"))
        self.assertIn("20210212-5147", lng.CORRECTION_284_126G)
        self.assertIn("20210212-5148", lng.CORRECTION_284_126G)

    def test_obligation_is_quoted_verbatim_with_a_span(self):
        quoted = [o for o in obs(metric_id="lng_operational_report")
                  if o["unit"] == "(order condition)"
                  and o["availability"] == Availability.PRESENT]
        self.assertTrue(quoted, "at least one facility's order condition must be quoted")
        for o in quoted:
            self.assertIn("semi-annual", (o["value_text"] or "").lower())
            self.assertIsNotNone(o["accession_number"])
        facts = rows("SELECT * FROM document_facts WHERE assertion_type='lng_reporting_obligation'")
        self.assertTrue(facts)
        for f in facts:
            self.assertTrue(f["verbatim_span"])
            self.assertIsNotNone(f["char_start"])
            self.assertLess(f["char_start"], f["char_end"])

    def test_an_unretrieved_obligation_says_so_rather_than_inferring_one(self):
        for o in obs(metric_id="lng_operational_report"):
            if o["unit"] != "(order condition)" or o["availability"] == Availability.PRESENT:
                continue
            self.assertIn(o["availability"],
                          {Availability.KNOWN_NOT_RETRIEVED, Availability.RETRIEVAL_FAILED})
            self.assertIn("284.126", o["missing_reason"] or "")


class TestInspectionOutcomeIsNotInvented(StagedRun):

    #: outcome capabilities: the row read the letter body and quotes it
    OUTCOME_SCOPES = ("SUBSTANTIVE FINDINGS", "CORRECTIVE ACTIONS", "FOLLOW-UP / CLOSURE")
    #: words that would state an outcome
    OUTCOME_WORDS = ("closed", "resolved", "no violations", "compliant",
                     "satisfactory", "in compliance")

    def _spans_for(self, o) -> str:
        """Every verbatim span stored for this observation's document.

        The exemption below is granted by EVIDENCE, so the evidence has to be
        fetched rather than taken on trust.
        """
        if not o["document_id"]:
            return ""
        facts = rows("SELECT verbatim_span FROM document_facts WHERE document_id=?",
                     (o["document_id"],))
        return " ".join((f["verbatim_span"] or "") for f in facts)

    def test_metadata_only_inspections_report_outcome_not_extracted(self):
        """After the A19 split there are two kinds of PRESENT inspection row.

        A discovery or inspection-date row carries metadata and no outcome, and
        must say so. A findings, corrective-actions or closure row read the letter
        body and quotes it with its span -- demanding NOT EXTRACTED there would
        make the row lie about its own evidence.
        """
        pool = [o for o in obs(metric_id="lng_inspection")
                if o["availability"] == Availability.PRESENT]
        self.assertTrue(pool)
        extracted = 0
        for o in pool:
            if any(k in (o["scope"] or "") for k in self.OUTCOME_SCOPES):
                self.assertIn("quoted VERBATIM from the body", o["qa_flags"] or "")
                extracted += 1
            else:
                self.assertIn("NOT EXTRACTED", (o["missing_reason"] or "").upper())
        self.assertTrue(extracted,
                        "no inspection outcome was extracted from any body; the A19 "
                        "capability is not exercised and this check proves nothing")

    def test_no_inspection_outcome_is_invented(self):
        """An outcome word is permitted only where FERC's own span contains it.

        w5-documents proposed exempting a row that carries the flag
        "quoted VERBATIM from the body" in its own qa_flags. That would let the
        adapter grant itself the exemption by asserting it -- the same shape as a
        receipt certifying its own correctness (A21) and a manifest that is its
        own inventory (A13). So the exemption is conditioned on the STORED SPAN
        actually containing the word, checked here against document_facts. The
        adapter cannot talk its way past this; only FERC's text can.

        The judgement itself is w5's and it is right: inventing these words is the
        defect, quoting them from a cited span is the A19 capability. Forbidding
        them outright would mean refusing to report a closure FERC actually
        stated, which trades one kind of dishonesty for another.
        """
        pool = [o for o in obs(metric_id="lng_inspection")
                if o["availability"] == Availability.PRESENT]
        self.assertTrue(pool)
        exempted = 0
        for o in pool:
            blob = ((o["value_text"] or "") + " " + (o["qa_flags"] or "")).lower()
            spans = self._spans_for(o).lower()
            for word in self.OUTCOME_WORDS:
                pattern = r"(?<!un)\b" + word.replace(" ", r"\s+") + r"\b"
                if not re.search(pattern, blob):
                    continue
                # the word IS present -- it is only allowed if FERC wrote it
                self.assertRegex(
                    spans, pattern,
                    f"inspection outcome {word!r} appears in observation "
                    f"{o['observation_id']} but is NOT present in any verbatim span "
                    "stored for its document, so nothing FERC wrote supports it")
                exempted += 1
        # Recorded, not asserted: zero is legitimate (no closure letter in the
        # window). A non-zero count means the exemption path was exercised and
        # every use of it was backed by a real span.
        self.assertGreaterEqual(exempted, 0)


class TestNonpublicIsNotMissing(StagedRun):
    """The public copy of an operational report is the cover letter; the
    operating data is CUI//PRIV in the N/C twin. That is a source condition."""

    def test_nonpublic_rows_exist_and_are_classified_as_a_source_condition(self):
        pool = [o for o in obs() if o["availability"] == Availability.NONPUBLIC]
        self.assertTrue(pool, "no nonpublic observation staged")
        for o in pool:
            self.assertIn(o["availability"], Availability.SOURCE_CONDITION)
            self.assertNotIn(o["availability"], Availability.OUR_GAP)

    def test_nonpublic_names_the_withheld_accession_and_band(self):
        for o in [x for x in obs() if x["availability"] == Availability.NONPUBLIC]:
            reason = o["missing_reason"] or ""
            self.assertRegex(reason, r"\d{8}-\d{4}",
                             "a nonpublic row must name the accession that holds the data")
            self.assertRegex(reason, r"CUI//PRIV|availability band")
            self.assertNotIn("no source", reason.lower())

    def test_the_twin_is_stored_and_excluded_from_canonical(self):
        twins = rows("SELECT * FROM filings WHERE source_system='eLibrary' "
                     "AND is_canonical=0 AND canonical_reason LIKE '%twin%'")
        self.assertTrue(twins, "availability twins must be retained as evidence")

    def test_nonpublic_is_distinct_from_our_own_gaps(self):
        avail = {o["availability"] for o in obs()}
        self.assertIn(Availability.NONPUBLIC, avail)
        self.assertTrue(avail & {Availability.KNOWN_NOT_RETRIEVED,
                                 Availability.NOT_APPLICABLE},
                        "the four outcomes must remain distinguishable in one run")


class TestSharedFilingLinksManyAssets(StagedRun):
    """One filing, many assets, one event. Elba files jointly for Elba
    Liquefaction and Southern LNG across nine dockets."""

    JOINT = "20260813-5004"

    def test_event_ids_are_unique(self):
        dupes = rows("SELECT event_id, COUNT(*) n FROM events GROUP BY 1 HAVING n>1")
        self.assertEqual(dupes, [], f"duplicate events: {dupes}")

    def test_a_shared_filing_produces_one_event_carrying_several_assets(self):
        evs = rows("SELECT * FROM events WHERE accession_number=?", (self.JOINT,))
        self.assertTrue(evs, f"{self.JOINT} produced no event")
        for e in evs:
            assets = json.loads(e["asset_ids"])
            self.assertGreater(len(assets), 1, "the joint report must link both assets")
            self.assertEqual(len(assets), len(set(assets)))
        by_type = {}
        for e in evs:
            by_type[e["event_type"]] = by_type.get(e["event_type"], 0) + 1
        self.assertTrue(all(n == 1 for n in by_type.values()),
                        f"one filing produced duplicate events per type: {by_type}")

    def test_both_entities_report_the_filing_without_duplicating_the_event(self):
        seen = obs(metric_id="lng_operational_report")
        entities = {o["entity_key"] for o in seen if o["accession_number"] == self.JOINT}
        self.assertGreaterEqual(len(entities), 2,
                                "both joint filers should carry the report observation")
        evs = rows("SELECT COUNT(*) n FROM events WHERE accession_number=? "
                   "AND event_type='operational_report'", (self.JOINT,))
        self.assertEqual(evs[0]["n"], 1, "the event must not be duplicated per entity")

    def test_procedural_motions_never_link_assets(self):
        hit = {"class_pairs": [("Pleading/Motion", "Procedural Motion")],
               "docket_bases": ["RP23-863"] + [f"CP{i:02d}-1" for i in range(50)]}
        self.assertFalse(elibrary.links_assets(hit))
        self.assertEqual(lng._linked_assets(hit), [])
        self.assertTrue(elibrary.links_assets(
            {"class_pairs": [("Order/Opinion", "Delegated Order")], "docket_bases": []}))

    def test_capacities_are_not_duplicated_across_the_joint_filers(self):
        """The joint report links both assets; a capacity still belongs to the
        order that names it."""
        for metric in ("lng_liquefaction_capacity", "lng_regas_sendout_capacity"):
            for o in obs(metric_id=metric):
                if o["availability"] != Availability.PRESENT:
                    continue
                self.assertNotEqual(o["accession_number"], self.JOINT,
                                    "a capacity must never come from a joint status report")


# ============================================================ rates

class TestDocketStageNotInferredFromRecency(StagedRun):

    def test_the_newest_document_does_not_set_the_stage(self):
        pool = [o for o in obs(metric_id="rate_case_status")
                if o["availability"] == Availability.PRESENT]
        self.assertTrue(pool)
        for o in pool:
            self.assertIn("NOT the newest document", o["qa_flags"])
            self.assertIn("newest document in", o["qa_flags"])

    def test_transco_rp24_1035_reports_acceptance_without_inventing_closure(self):
        pool = [o for o in obs(entity_key="C000654", metric_id="rate_case_status")
                if o["value_text"] and o["value_text"].startswith("RP24-1035")]
        self.assertEqual(len(pool), 1, "one stage per docket")
        o = pool[0]
        self.assertEqual(o["accession_number"], "20260428-3033")
        self.assertIn("refund report ACCEPTED", o["value_text"])
        self.assertIn("closure NOT established", o["value_text"])
        self.assertIn("20260428-5001", o["qa_flags"])
        self.assertIn("does NOT supersede", o["qa_flags"])

    def test_tgp_rates_run_off_the_2019_settlement_not_a_recent_compliance_letter(self):
        pool = [o for o in obs(entity_key="C000020", metric_id="rate_case_status")
                if o["value_text"] and o["value_text"].startswith("RP19-351")]
        self.assertTrue(pool, "RP19-351 must be reported for TGP")
        o = pool[0]
        self.assertTrue((o["instant_date"] or "").startswith("2019"),
                        f"the legal date is 2019, got {o['instant_date']}")
        self.assertEqual(o["accession_number"], "20190524-3047")

    def test_tgp_has_no_general_rate_case_and_that_is_a_valid_state(self):
        neg = [o for o in obs(entity_key="C000020", metric_id="rate_case_status")
               if o["availability"] == Availability.NOT_APPLICABLE
               and "general_rate_case" in (o["value_text"] or "")]
        self.assertTrue(neg, "the affirmative 'no s.4 rate case' finding must be staged")
        self.assertIn(neg[0]["availability"], Availability.SOURCE_CONDITION)
        self.assertIn("154.312", neg[0]["missing_reason"])

    def test_tejas_pr18_59_has_no_order_and_says_so(self):
        pool = obs(entity_key="C000434", metric_id="rate_case_status")
        self.assertTrue(pool)
        filed = [o for o in pool if (o["value_text"] or "").startswith("PR18-59")]
        self.assertTrue(filed)
        self.assertIn("no Commission action", filed[0]["missing_reason"])
        unresolved = [o for o in pool if "NONE LOCATED" in (o["value_text"] or "")]
        self.assertTrue(unresolved, "the honest unresolved must be recorded, not omitted")

    def test_stage_of_ignores_a_later_non_advancing_document(self):
        def hit(acc, date, stage, issuance):
            return {"accession": acc, "filed_date": date, "description": "",
                    "classification": {"stage": stage, "is_issuance": issuance,
                                       "advancing": stage in docs.RANK,
                                       "evidence_class": docs.SOURCED, "why": "",
                                       "label": docs.LABEL.get(stage, stage)}}
        docket = [hit("A", "2025-12-30", "settlement_approved", True),
                  hit("B", "2026-08-07", "under_judicial_review", False),
                  hit("C", "2026-04-28", "refund_report_supplement", False)]
        stage, src, newest = docs._stage_of(docket)
        self.assertEqual(stage, "settlement_approved")
        self.assertEqual(src["accession"], "A")
        self.assertEqual(newest["accession"], "B")

    def test_stage_of_prefers_the_earliest_order_at_equal_rank(self):
        def hit(acc, date):
            return {"accession": acc, "filed_date": date, "description": "",
                    "classification": {"stage": "settlement_approved", "is_issuance": True,
                                       "advancing": True, "evidence_class": docs.SOURCED,
                                       "why": "", "label": ""}}
        stage, src, _ = docs._stage_of([hit("late", "2022-10-06"), hit("early", "2019-05-24")])
        self.assertEqual(src["accession"], "early")


class TestEvidenceClassSeparatesSourcedFromInferred(StagedRun):

    def test_a_filer_stated_effective_date_is_never_published_as_a_legal_date(self):
        for o in obs(metric_id="rate_effective_date"):
            if o["availability"] == Availability.PRESENT:
                self.assertIn("ORDER TEXT", o["qa_flags"])
                self.assertIn(docs.SOURCED, o["qa_flags"])
            else:
                self.assertIsNone(o["value_text"])
                if "filer-stated" in (o["qa_flags"] or ""):
                    self.assertIn(docs.INFERRED, o["qa_flags"])
                    self.assertIn("NOT published as a legal effective date",
                                  o["missing_reason"])

    def test_the_filer_stated_date_is_retained_as_a_document_fact(self):
        kept = rows("SELECT * FROM document_facts WHERE assertion_type="
                    "'filer_stated_effective_date'")
        for f in kept:
            self.assertIn("not adjudicated", f["qualifier"])


class TestSpanSupportsCanStillSayNo(unittest.TestCase):
    """The predicate behind every span-backed check must still discriminate.

    Three shipped checks rest on `elibrary.span_supports`:
    `test_every_refund_window_bound_cites_a_span_containing_its_date` below, and
    `M-STAGING.2` / `M-STAGING.2i` in `acceptance/test_matrix_identity.py`. All
    three CONSUME the predicate's result — they filter for facts it rejects — so
    if it ever returned `True` unconditionally, every one of them would find
    nothing, report success and prove nothing. Nothing in the shipped tree
    asserted it could still return `False`; that gap is what this class closes.

    Deliberately a plain `TestCase` rather than a `StagedRun`. A guard that skips
    when the database is absent is the exact failure mode it exists to prevent,
    and it needs no database: both passages are literal FERC text.

    The two spans are real. The heading is the one that produced the fossil
    document fact `dfact-138d7329…`, which asserted the RP24-1035 closure bound
    `2026-04-28` while citing words that do not contain it. The dateline is from
    the order body that replaced it.
    """

    #: the eLibrary description heading — note it carries a DIFFERENT date,
    #: 04/09/2026, so this is "the wrong date is present", not "no date is
    #: present". That is the harder discrimination and the one a fossil needs.
    FOSSIL_HEADING = ("Letter order accepting Transcontinental Gas Pipe Line "
                      "Company, LLC's 04/09/2026 filing of its report of refunds "
                      "detailing the refunds and surcharges made in compliance "
                      "with Article IV of the Stipulation and Agreement")
    #: the dateline in the order body, which genuinely supports the bound
    ORDER_DATELINE = "Issued: April 28, 2026"

    def test_it_rejects_a_heading_that_lacks_the_date_it_is_cited_for(self):
        self.assertFalse(
            elibrary.span_supports(self.FOSSIL_HEADING, "2026-04-28"),
            "span_supports no longer rejects the passage that produced the "
            "fossil. Every check resting on it is now vacuous: they filter for "
            "what it rejects, so a permissive predicate makes them all pass "
            "while finding nothing.")

    def test_it_still_accepts_the_same_heading_for_its_own_date(self):
        # Without this, a predicate returning False for EVERYTHING satisfies the
        # rejection above. A rejection only means something if the same passage
        # is shown to be capable of acceptance.
        self.assertTrue(
            elibrary.span_supports(self.FOSSIL_HEADING, "2026-04-09"),
            "span_supports no longer accepts a passage for the date it actually "
            "contains; it is now useless in the other direction, and the "
            "rejection test above proves nothing on its own.")

    def test_it_accepts_a_genuine_dateline_in_the_long_american_form(self):
        self.assertTrue(
            elibrary.span_supports(self.ORDER_DATELINE, "2026-04-28"),
            "span_supports no longer matches the long American date form an "
            "order actually writes against the ISO form a fact actually stores")


class TestRefundWindowPublishesNoAmount(StagedRun):

    def test_no_dollar_amount_anywhere_in_the_metric(self):
        for o in obs(metric_id="refund_exposure_window"):
            for field in ("value_text", "qa_flags", "missing_reason"):
                text = o[field] or ""
                self.assertNotRegex(text, r"\$\s*[\d,]+",
                                    f"refund exposure published an amount in {field}")

    def test_the_guard_refuses_a_monetary_string(self):
        with self.assertRaises(ValueError):
            docs._refuse_dollars("2025-03-20 .. 2026-04-28, about $12,000,000")
        self.assertEqual(docs._refuse_dollars("2025-03-20 .. 2026-04-28"),
                         "2025-03-20 .. 2026-04-28")

    def test_transco_window_starts_at_effectiveness_and_stays_open_without_closure(self):
        # This assertion USED TO ENCODE THE DEFECT (audit A10). It required
        # "2025-03-20 .. 2026-04-28", where 2025-03-20 is the ISSUANCE date of
        # letter order 20250320-3103. That order's own text accepts the tariff
        # records "effective March 1, 2025", and the suspension order
        # 20240930-3067 says they take effect "upon motion March 1, 2025, subject
        # to refund". The refund clock starts when the rates became collectable
        # subject to refund, not when the paperwork issued -- so the old test
        # would have failed correct data and passed the wrong window.
        pool = [o for o in obs(entity_key="C000654", metric_id="refund_exposure_window")
                if o["value_text"] and "RP24-1035" in (o["scope"] or "")]
        self.assertTrue(pool, "the RP24-1035 refund window must be staged")
        o = pool[0]
        # The April 28 letter says only that the refund report "as supplemented
        # is accepted for informational purposes."  It does not state that the
        # refund obligation is satisfied, discharged or closed.  The final audit
        # explicitly found that the former closed-window assertion outran this
        # operative passage, so the conservative current value remains open.
        self.assertEqual(o["value_text"], "2025-03-01 .. open")
        self.assertIn("20250320-3103", o["qa_flags"])      # the order that says so
        self.assertIn("20260428-3033", o["qa_flags"])      # report acceptance, not closure
        self.assertIn("legal role rates_effective_subject_to_refund", o["qa_flags"])
        self.assertIn("ISSUANCE date is NOT the window start", o["qa_flags"])
        self.assertIn("END open", o["qa_flags"])
        closed = rows(
            "SELECT * FROM document_facts WHERE filing_id='20260428-3033' "
            "AND assertion_type='refund_window_refund_obligation_closed'")
        self.assertEqual(closed, [],
                         "acceptance for informational purposes was promoted to closure")

    def test_every_refund_window_bound_cites_a_span_containing_its_date(self):
        """The companion check that would have caught the original defect.

        A date is only defensible if the cited span actually contains it. The
        issuance-date window passed every other test precisely because nothing
        compared the published bound against the words it claimed to come from.
        """
        facts = rows("SELECT * FROM document_facts WHERE assertion_type "
                     "LIKE 'refund_window_%'")
        self.assertTrue(facts, "refund-window bounds must be stored as document facts")
        for f in facts:
            self.assertTrue(
                elibrary.span_supports(f["verbatim_span"], f["value_text"]),
                f"{f['assertion_type']} cites a span that does not contain "
                f"{f['value_text']!r}")

    def test_an_outright_acceptance_starts_no_refund_clock(self):
        na = [o for o in obs(metric_id="refund_exposure_window")
              if o["availability"] == Availability.NOT_APPLICABLE]
        for o in na:
            self.assertIn("no order accepting and SUSPENDING", o["missing_reason"])


class TestTariffRateIsGatedNotDeferred(StagedRun):

    def test_the_tariff_sheets_were_actually_retrieved(self):
        recs = rows("SELECT * FROM document_facts WHERE assertion_type="
                    "'tariff_record_identifier'")
        self.assertTrue(recs, "tariff records must be retrieved, not permanently deferred")
        for r in recs:
            self.assertTrue(r["verbatim_span"])
            self.assertIsNotNone(r["char_start"])

    def test_a_failed_gate_retains_the_values_and_says_which_condition_failed(self):
        """Two distinct ways the package is never read, both gated.

        w5-documents confirmed both branches are legitimate and must both stay
        sayable, so the disjunction is asserted with each branch named:

          KNOWN_NOT_RETRIEVED  the package was identified and never attempted;
          RETRIEVAL_FAILED     attachments were listed and the download failed.

        "UNRESOLVED OPERATIVE RATE" is required on BOTH because it is the claim
        about the OUTPUT -- no operative rate is published -- which is true
        however we failed to get there. The specific diagnosis is a claim about
        the CAUSE. Keeping only the more precise one would leave a gate outcome
        that no gate check can see, which is how this got through the first time.
        """
        seen = {Availability.RETRIEVAL_FAILED: 0, Availability.KNOWN_NOT_RETRIEVED: 0}
        gated = 0
        for o in obs(metric_id="tariff_operative_rate"):
            if o["availability"] in (Availability.PRESENT,
                                     Availability.EXPECTED_NOT_LOCATED):
                continue
            self.assertIn("UNRESOLVED OPERATIVE RATE", o["missing_reason"] or "")
            self.assertRegex(o["qa_flags"] or "", r"\d_[a-z_]+=(pass|FAIL)")
            self.assertIsNone(o["value_text"])
            gated += 1
            if o["availability"] == Availability.RETRIEVAL_FAILED:
                self.assertIn("0_package_retrieved=FAIL", o["qa_flags"])
                self.assertIn("RETRIEVAL failure", o["missing_reason"])
                self.assertIn("not a nonpublic record", o["missing_reason"])
                seen[Availability.RETRIEVAL_FAILED] += 1
            elif o["availability"] == Availability.KNOWN_NOT_RETRIEVED:
                self.assertIn("0_package_retrieved=FAIL", o["qa_flags"])
                self.assertIn("not downloaded in this run", o["missing_reason"])
                seen[Availability.KNOWN_NOT_RETRIEVED] += 1
        # A complete cache should eliminate live retrieval failures.  Requiring
        # one here made data recovery fail the suite.  The two failure branches
        # remain exercised with fault-injected production-function fixtures in
        # test_documents; this real-state assertion proves that at least one
        # semantic gate (rather than a blanket success) is still evaluated.
        self.assertTrue(gated, "no live operative-rate gate was examined")

    def test_magellan_gate_fails_on_the_unordered_successor(self):
        pool = obs(entity_key="C001049", metric_id="tariff_operative_rate")
        self.assertTrue(pool)
        self.assertIn("3_accepting_order_located=FAIL", pool[0]["qa_flags"])
        self.assertIn("F.E.R.C. No.", pool[0]["qa_flags"])


class TestClassTypeFilterShapeIsEnforced(unittest.TestCase):
    """A string classTypes filter is silently ignored by FERC and returns
    unfiltered hits. It must never reach the wire."""

    def test_string_form_raises(self):
        with self.assertRaises(elibrary.ClassTypeShapeError):
            elibrary.validate_class_types("Order/Opinion|Commission Order/Opinion")
        with self.assertRaises(elibrary.ClassTypeShapeError):
            elibrary.validate_class_types(["Order/Opinion|Commission Order/Opinion"])

    def test_getclasstypes_row_shape_raises(self):
        with self.assertRaises(elibrary.ClassTypeShapeError):
            elibrary.validate_class_types([{"Class": "Order/Opinion",
                                            "Type": "Commission Order/Opinion",
                                            "Library": "G", "Category": "Issuance"}])

    def test_correct_shape_survives_and_reaches_the_body(self):
        ok = elibrary.validate_class_types([elibrary.class_type("Order/Opinion",
                                                                "Delegated Order")])
        self.assertEqual(ok, [{"documentClass": "Order/Opinion",
                               "documentType": "Delegated Order"}])
        body = elibrary.search_body(docket="CP11-72", class_types=ok)
        self.assertEqual(body["classTypes"], ok)
        self.assertFalse(body["searchDescription"], "a docket query sets no description flag")

    def test_docket_base_does_not_mangle_a_three_digit_docket(self):
        self.assertEqual(elibrary.docket_base("IS24-810"), "IS24-810")
        self.assertEqual(elibrary.docket_base("CP12-507"), "CP12-507")
        self.assertEqual(elibrary.docket_base("CP12-507-000"), "CP12-507")
        self.assertEqual(elibrary.docket_base("RP24-1035-005"), "RP24-1035")

    def test_availability_dedupe_prefers_the_public_copy(self):
        hits = [{"accession": "20260803-5168", "avail_code": "N", "filed_date": "2026-08-03",
                 "description": "Semi-Annual Operational Report 01/01/2026 to 06/30/2026"},
                {"accession": "20260803-5167", "avail_code": "P", "filed_date": "2026-08-03",
                 "description": "Semi-Annual Operational Report 01/01/2026 to 06/30/2026"}]
        canonical, twins = elibrary.dedupe_availability(hits)
        self.assertEqual([c["accession"] for c in canonical], ["20260803-5167"])
        self.assertEqual([t["accession"] for t in twins], ["20260803-5168"])
        self.assertEqual(twins[0]["twin_of"], "20260803-5167")

    def test_the_same_accession_twice_is_not_its_own_twin(self):
        one = {"accession": "20240530-3079", "avail_code": "P", "filed_date": "2024-05-30",
               "description": "Letter order approving a stipulation and agreement"}
        canonical, twins = elibrary.dedupe_availability([dict(one), dict(one)])
        self.assertEqual(len(canonical), 1)
        self.assertEqual(twins, [])


class TestEveryFrozenSlotEndsMeasured(StagedRun):

    def test_no_document_slot_is_silently_empty(self):
        expected = rows("SELECT * FROM coverage_expected WHERE source_regime="
                        "'eLibrary document'")
        self.assertTrue(expected)
        have = {(o["entity_key"], o["metric_id"]) for o in obs()}
        missing = [(s["entity_key"], s["metric_id"]) for s in expected
                   if (s["entity_key"], s["metric_id"]) not in have]
        self.assertEqual(missing, [], f"frozen slots with no observation at all: {missing}")

    def test_every_absence_carries_a_reason(self):
        for o in obs():
            if o["availability"] == Availability.PRESENT:
                continue
            self.assertTrue((o["missing_reason"] or "").strip() or (o["qa_flags"] or "").strip(),
                            f"{o['entity_key']}/{o['metric_id']} is blank without a reason")

    def test_our_gaps_are_labelled_as_ours_and_source_conditions_as_the_sources(self):
        for o in obs():
            avail = o["availability"]
            reason = (o["missing_reason"] or "").lower()
            if avail == Availability.KNOWN_NOT_RETRIEVED:
                self.assertTrue("not retrieved" in reason or "not download" in reason
                                or "our retrieval gap" in reason or "not yet" in reason,
                                f"{o['metric_id']}: {reason[:120]}")
            if avail == Availability.NONPUBLIC:
                self.assertNotIn("not implemented", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
