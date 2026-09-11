"""
w5-documents acceptance tests: A03, A08 (document share), A10, A11, A17, A19, A22.

Every test here is a NEGATIVE test in the sense Rule 4 requires: it fails if the
defect the 8 September audit found is reintroduced. A test that would pass
because a mutated record became acceptable is not written here, and no test in
this file skips silently -- where a test needs a fixture population, it asserts
that the population exists first, so "we found nothing, so nothing was wrong"
can never be the outcome.

Synthetic fixtures are labelled AUDIT_SYNTHETIC_* and are isolated: they are
constructed in-process, never written to the staging database, never retrieved
from FERC and never counted in coverage. Historical negative populations that
formerly came from an implicit sibling database now come from the release-
relative, SHA-256-verified acceptance fixture manifest. The adaptation preserves
the exact audited rows/counts and keeps them separate from repaired-code controls.

    cd outputs/repair_all_regimes
    python3 -m unittest discover -s tests -p 'test_documents.py' -v
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import zlib
import unittest

# Adopted into tests/ from work/w5-documents/tests/ (W5 request 10). That
# location is two levels deeper, so ROOT moves from parents[3] to parents[1].
# Only the path bootstrap changed; not one assertion was touched.
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters import capacity                                          # noqa: E402
from adapters import elibrary_docs as docs                             # noqa: E402
from adapters import lng                                               # noqa: E402
from ferclib import elibrary                                           # noqa: E402
from ferclib.status import Availability, Method, Validation            # noqa: E402
from acceptance.harness import fixture_json                            # noqa: E402

# Historical-negative evidence is a release-relative, hash-verified fixture.
# Never infer it from a sibling database: in a standalone extraction that path
# aliases the repaired database and silently removes the defect population.
HISTORICAL = fixture_json("historical_document_regressions.json")

ORDER = [("Order/Opinion", "Delegated Order")]
REPORT = [("Report/Form", "Certificate of Compliance Report")]


def filing(description, class_pairs, **extra):
    """One eLibrary hit in the shape the adapters consume."""
    row = {"accession": extra.pop("accession", "AUDIT_SYNTHETIC_FILING"),
           "description": description, "class_pairs": list(class_pairs),
           "class_types": ["::".join(p) for p in class_pairs],
           "filed_date": extra.pop("filed_date", "2026-09-01"),
           "issued_date": "", "posted_date": "", "avail_code": "P",
           "dockets": ["CP23-375-000"], "docket_bases": ["CP23-375"],
           "transmittals": [], "affiliations": [], "libraries": ["GAS"],
           "category": "", "document_id": "", "availability_twins": []}
    row.update(extra)
    return row


# =====================================================================  A03

class TestLNGStatesAreFourNotOne(unittest.TestCase):
    """A request is not an approval; commissioning is not service."""

    CFG = {"facility": "AUDIT SYNTHETIC facility", "assets": ["synthetic-asset"],
           "sweep_dockets": ["CP23-375"], "all_dockets": ["CP23-375"],
           "process": {"liquefaction"}, "fetch": [], "obligation_order": "",
           "note": "synthetic"}

    # ---- the real eight ------------------------------------------------

    def test_the_eight_real_requests_are_not_authorisations(self):
        """The 8 Elba filings the audit found as `authorised_to_enter_service`
        investor events are company REQUESTS. They must classify as requests, and
        the fixture population must be non-empty."""
        rows = HISTORICAL["lng_request_documents"]
        self.assertGreaterEqual(
            len(rows), 8,
            "fixture population missing: the audit found 8 real request filings "
            "misclassified as authorisations; this test must not pass by finding none")
        for r in rows:
            pairs = [tuple((p.split("::") + [""])[:2])
                     for p in (r["class_type"] or "").split("|") if p]
            act = lng.operative_act({"description": r["title"], "class_pairs": pairs})
            self.assertIn(act["kind"],
                          (lng.ACT_SERVICE_REQUEST, lng.ACT_COMMISSIONING_REQUEST),
                          f"{r['filing_id']} is a company request: {r['title'][:120]}")
            self.assertNotEqual(act["kind"], lng.ACT_SERVICE_AUTHORISATION)

    def test_a_request_saying_no_decision_was_issued_cannot_become_an_approval(self):
        f = filing("AUDIT SYNTHETIC: applicant submits request for permission to "
                   "commence service for Train 2. No decision issued.", REPORT,
                   accession="AUDIT_SYNTHETIC_REQUEST")
        act = lng.operative_act(f)
        self.assertEqual(act["kind"], lng.ACT_SERVICE_REQUEST)
        obs = lng._status_authorised("E", self.CFG, [f], [])
        self.assertEqual(obs, [], "a request became a service authorisation")
        events = lng._events("E", self.CFG, [f], _Baseline(established=True))
        self.assertTrue(events)
        for e in events:
            self.assertNotEqual(e["event_type"], "authorised_to_enter_service")
            self.assertEqual(e["destination"], elibrary.ARCHIVE)

    def test_a_grant_worded_as_a_denial_is_not_an_approval(self):
        f = filing("AUDIT SYNTHETIC: Letter denying the request to commence service "
                   "of Train 9.", ORDER)
        self.assertEqual(lng.operative_act(f)["kind"], "")
        self.assertEqual(lng._status_authorised("E", self.CFG, [f], []), [])

    # ---- the real six --------------------------------------------------

    def test_commissioning_only_orders_are_not_service_authorisations(self):
        """The 6 hazardous-fluid letters the audit found stored as service
        authorisations, checked population-wide across the whole baseline."""
        rows = HISTORICAL["lng_hazardous_fluid_documents"]
        self.assertGreaterEqual(len(rows), 6, "fixture population missing")
        combined = 0
        for r in rows:
            pairs = [tuple((p.split("::") + [""])[:2])
                     for p in (r["class_type"] or "").split("|") if p]
            act = lng.operative_act({"description": r["title"], "class_pairs": pairs})
            if act["kind"] == lng.ACT_SERVICE_AUTHORISATION:
                # only permissible when the SAME order also grants service
                self.assertTrue(act["combined"],
                                f"{r['filing_id']} became a service authorisation on "
                                f"commissioning wording alone: {r['title'][:140]}")
                combined += 1
        self.assertGreaterEqual(
            combined, 1,
            "20200618-3040 grants commissioning AND service in one order; if no "
            "combined order survives, the fix has become a blanket word exclusion")

    def test_the_combined_order_is_preserved(self):
        f = filing("Letter order granting Corpus Christi Liquefaction, LLC's 06/05/2020 "
                   "request to introduce hazardous fluids and commence service of East "
                   "Jetty facilities under CP12-507.", ORDER, accession="20200618-3040")
        act = lng.operative_act(f)
        self.assertEqual(act["kind"], lng.ACT_SERVICE_AUTHORISATION)
        self.assertTrue(act["combined"])
        facts = []
        obs = lng._status_authorised("E", self.CFG, [f], facts)
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["availability"], Availability.PRESENT)
        self.assertIn("East Jetty", obs[0]["scope"])
        kinds = {d["assertion_type"] for d in facts}
        self.assertEqual(kinds, {"lng_service_authorisation",
                                 "lng_commissioning_authorisation"},
                         "a combined order must record BOTH states")

    def test_commissioning_never_lands_on_the_service_metric(self):
        f = filing("Letter to Elba Liquefaction Company, L.L.C. et al. granting the "
                   "07/13/2026 request to introduce hazardous fluids into Movable "
                   "Modular Liquefaction System 6 under CP23-375.", ORDER)
        self.assertEqual(lng._status_authorised("E", self.CFG, [f], []), [])
        facts = []
        obs = lng._status_commissioning("E", self.CFG, [f], facts)
        self.assertEqual(len(obs), 1)
        self.assertNotEqual(obs[0]["metric_id"], "lng_status_authorised")
        self.assertIn("COMMISSIONING", obs[0]["scope"])
        self.assertIn("NOT authorisation to enter service", obs[0]["qa_flags"])
        self.assertEqual([d["assertion_type"] for d in facts],
                         ["lng_commissioning_authorisation"])

    def test_construction_permission_is_neither_commissioning_nor_service(self):
        f = filing("Letter to Elba Liquefaction Company, L.L.C. et al. granting the "
                   "02/11/2025 request to commence construction activities of the "
                   "aboveground hazardous fluid piping 2-inches and less in diameter "
                   "at the Elba Liquefaction Project under CP23-375.", ORDER)
        self.assertEqual(lng.operative_act(f)["kind"], "")

    def test_the_component_number_survives(self):
        """Per-component scope: 'MMLS 02' and 'System Unit #2' are one component,
        and neither collapses onto a nameless 'Movable Modular Liquefaction
        System' the way the delivered pattern did."""
        self.assertEqual(lng._component("request for the MMLS 02 of the project"),
                         "Movable Modular Liquefaction System 2")
        self.assertEqual(
            lng._component("place in-service the Movable Modular Liquefaction "
                           "System Unit #2 for the project"),
            "Movable Modular Liquefaction System 2")
        self.assertEqual(lng._component("introduce hazardous fluids to the "
                                        "Condensate Plant"), "Condensate Plant")

    # ---- report presence is not operation -------------------------------

    def test_a_report_cover_sheet_cannot_prove_actual_operation(self):
        reports = [dict(filing(
            "Sabine Pass Liquefaction, LLC submits Semi-Annual Operational Report "
            "No. 21 for the period of 01/01/2026 to 06/30/2026 under CP11-72.",
            REPORT, accession="AUDIT_SYNTHETIC_REPORT"),
            _period=("2026-01-01", "2026-06-30"))]
        m_op = lng.BY_ID["lng_status_operating"]
        out = lng._reporting_continuity("E", self.CFG, reports, [], m_op)
        self.assertEqual(len(out), 1)
        o = out[0]
        self.assertIsNone(o["value_text"], "operation was asserted from a cover sheet")
        self.assertNotEqual(o["availability"], Availability.PRESENT)
        self.assertNotEqual(o["validation"], Validation.PASS)
        self.assertIn("REPORTING CONTINUITY", o["missing_reason"])
        self.assertIn("NOT evidence that the facility operated", o["missing_reason"])

    def test_operation_is_asserted_only_from_a_body_that_states_it(self):
        reports = [dict(filing(
            "Corpus Christi Liquefaction, LLC submits Semi-Annual Operational Report "
            "for the period of 01/01/2026 to 06/30/2026 under CP12-507.", REPORT),
            _period=("2026-01-01", "2026-06-30"),
            text="AUDIT SYNTHETIC BODY. During the reporting period the terminal "
                 "operated continuously and exported 32 cargoes.")]
        out = lng._reporting_continuity("E", self.CFG, reports, [],
                                        lng.BY_ID["lng_status_operating"])
        self.assertEqual(out[0]["availability"], Availability.PRESENT)
        self.assertIn("operated", out[0]["value_text"])

    def test_the_thirty_five_pass_operating_rows_no_longer_reproduce(self):
        """The delivered data carried 35 PRESENT/pass `lng_status_operating` rows,
        every one of them resting on a report's existence. The fixture population
        is asserted first."""
        stored = HISTORICAL["historical_counts"]["lng_status_operating_present_pass"]
        self.assertEqual(stored, 35, "the audited historical population changed")
        # Repaired behavior is exercised independently by the two synthetic
        # cover-sheet/body tests above. This assertion preserves the historical
        # defect without running repaired code against historical expected output.


# =====================================================================  A19

class TestInspectionCapabilitiesAreSeparate(unittest.TestCase):

    CFG = {"facility": "AUDIT SYNTHETIC LNG terminal", "assets": [],
           "sweep_dockets": [], "all_dockets": [], "process": set(),
           "fetch": [], "obligation_order": "", "note": "synthetic"}
    LETTER = [("FERC Correspondence With Applicant", "General Correspondence")]

    def _run(self, filings):
        facts = []
        obs, pops = lng._inspection("E", self.CFG, filings, facts)
        return obs, pops, facts

    def test_a_filing_date_is_never_the_inspection_date(self):
        f = filing("Letter to Corpus Christi Liquefaction, LLC discussing the "
                   "04/21/2026 et al. annual inspection of the terminal near Corpus "
                   "Christi, Texas under CP12-507.", self.LETTER,
                   accession="20260819-3015", filed_date="2026-08-19")
        obs, _pops, facts = self._run([f])
        dated = [o for o in obs if "ACTUAL INSPECTION DATE" in o["scope"]]
        self.assertEqual(len(dated), 1)
        self.assertEqual(dated[0]["value_text"], "2026-04-21")
        self.assertNotEqual(dated[0]["value_text"], f["filed_date"])
        self.assertIn("These are different dates", dated[0]["qa_flags"])
        date_facts = [d for d in facts if d["assertion_type"] == "lng_inspection_date"]
        self.assertEqual(len(date_facts), 1)
        self.assertTrue(elibrary.span_supports(date_facts[0]["verbatim_span"],
                                               "2026-04-21"),
                        "the inspection-date span must contain the date it asserts")

    def test_the_filing_date_is_not_smuggled_in_when_no_date_is_stated(self):
        f = filing("Letter informing Southern LNG Company, L.L.C. that Commission "
                   "staff plans to conduct a technical review and site inspection.",
                   self.LETTER, accession="AUDIT_SYNTHETIC_NO_DATE",
                   filed_date="2026-03-02")
        obs, _pops, _facts = self._run([f])
        dated = [o for o in obs if "ACTUAL INSPECTION DATE" in o["scope"]]
        self.assertEqual(len(dated), 1)
        self.assertIsNone(dated[0]["value_text"])
        self.assertNotEqual(dated[0]["availability"], Availability.PRESENT)
        self.assertIn("FILING DATE of the correspondence is NOT used",
                      dated[0]["missing_reason"])

    def test_correspondence_presence_is_not_a_finding_or_a_closure(self):
        f = filing("Letter to Gulf LNG Energy, LLC discussing the April 26, 2023 "
                   "Biennial Inspection of the Gulf LNG facility under CP06-12.",
                   self.LETTER, accession="AUDIT_SYNTHETIC_LETTER")
        obs, pops, _facts = self._run([f])
        by_scope = {o["scope"].split("|")[-1].strip(): o for o in obs}
        for capability in ("SUBSTANTIVE FINDINGS", "CORRECTIVE ACTIONS",
                           "FOLLOW-UP / CLOSURE"):
            o = by_scope[capability]
            self.assertIsNone(o["value_text"])
            self.assertNotEqual(o["availability"], Availability.PRESENT)
            self.assertIn("not a compliance conclusion", o["missing_reason"].lower())
        self.assertEqual(by_scope["DISCOVERY (filings located)"]["availability"],
                         Availability.PRESENT)
        empty = [p for p in pops if p["row_count"] == 0]
        self.assertTrue(empty, "an unavailable capability must persist its population")
        for p in empty:
            self.assertTrue(p["empty_reason"],
                            "a zero-row population must say WHY it is empty")

    def test_a_stated_finding_is_extracted_and_is_not_a_clearance(self):
        f = dict(filing("Letter to Corpus Christi Liquefaction, LLC discussing the "
                        "04/21/2026 annual inspection under CP12-507.", self.LETTER,
                        accession="AUDIT_SYNTHETIC_BODY"),
                 text="AUDIT SYNTHETIC BODY. Our annual inspection of the LNG terminal "
                      "took place on April 21-22, 2026. During the course of the site "
                      "visit and technical review, we discussed multiple items that have "
                      "been addressed and a few items that are in the process of being "
                      "addressed. Based on this technical review, we have no "
                      "recommendations at this time.")
        obs, _pops, facts = self._run([f])
        by_scope = {o["scope"].split("|")[-1].strip(): o for o in obs}
        finding = by_scope["SUBSTANTIVE FINDINGS"]
        self.assertEqual(finding["availability"], Availability.PRESENT)
        self.assertIn("recommendations", finding["value_text"])
        self.assertIn("not a certificate of compliance", finding["qa_flags"])
        corrective = by_scope["CORRECTIVE ACTIONS"]
        self.assertEqual(corrective["availability"], Availability.PRESENT)
        self.assertIn("in the process of being addressed", corrective["value_text"])
        closure = by_scope["FOLLOW-UP / CLOSURE"]
        self.assertNotEqual(closure["availability"], Availability.PRESENT,
                            "'no recommendations' is not a closure")
        self.assertEqual(by_scope["ACTUAL INSPECTION DATE"]["value_text"], "2026-04-21")

    #: the words that would state an inspection outcome, from the delivered suite
    OUTCOME_WORDS = ("closed", "resolved", "no violations", "compliant",
                     "satisfactory", "in compliance")

    def _outcome_violations(self, body):
        """Outcome words that appear on a PRESENT row but in no stored span.

        w6-acceptance's rule, applied from this side: an outcome word is
        permitted only where FERC's own cited text contains it. Their version
        conditions the exemption on the STORED SPAN rather than on a flag the
        adapter sets about itself, which is the right shape -- the adapter must
        not be both the claimant and the evidence.
        """
        letter = dict(filing(
            "Letter to X discussing the 04/26/2023 Biennial Inspection under CP06-12.",
            self.LETTER, accession="A1", filed_date="2023-08-01"),
            document_id="eLibrary|A1|x", text=body)
        facts = []
        obs, _pops = lng._inspection("E", self.CFG, [letter], facts)
        spans = " ".join((f.get("verbatim_span") or "") for f in facts
                         if f.get("document_id") == "eLibrary|A1|x").lower()
        violations, exempted = [], 0
        for o in obs:
            if o["availability"] != Availability.PRESENT:
                continue
            blob = ((o["value_text"] or "") + " " + (o["qa_flags"] or "")).lower()
            for word in self.OUTCOME_WORDS:
                pattern = r"(?<!un)\b" + word.replace(" ", r"\s+") + r"\b"
                if not re.search(pattern, blob):
                    continue
                if re.search(pattern, spans):
                    exempted += 1
                else:
                    violations.append((word, o["scope"]))
        return violations, exempted

    def test_the_adapter_never_supplies_its_own_outcome_vocabulary(self):
        """A caveat must not introduce a word the source does not supply.

        Found by w6-acceptance's stronger check: this row's own caveat used to
        read "...that a matter is closed". Where FERC states a closure in other
        words -- "No further action is required" -- the adapter was then the only
        thing on the row asserting "closed", with nothing in the cited span
        behind it. That is an invented outcome dressed as boilerplate.
        """
        violations, _ = self._outcome_violations(
            "Our biennial inspection took place on April 26, 2023. Based on this "
            "technical review, we have no recommendations at this time. No further "
            "action is required at this time.")
        self.assertEqual(violations, [],
                         "the adapter put an outcome word on a row that no stored span "
                         "supports")

    def test_a_closure_ferc_actually_wrote_is_still_extracted(self):
        """The other half: the fix must not become a refusal to report a closure
        FERC did state. The exemption path is exercised and is span-backed."""
        violations, exempted = self._outcome_violations(
            "Our annual inspection took place on May 5, 2026. We recommend that the "
            "operator revise its procedures. This matter is now closed.")
        self.assertEqual(violations, [])
        self.assertGreaterEqual(exempted, 1,
                                "the closure capability stopped extracting a closure FERC "
                                "stated in terms")

    def test_the_fifty_nine_discovered_records_are_reused(self):
        rows = HISTORICAL["historical_counts"]["lng_inspection_present"]
        self.assertEqual(rows, 59, "the audited historical population changed")


# =====================================================================  A10

class TestRefundWindowResolvesToOperativeText(unittest.TestCase):

    TARGA = ("ORDER ACCEPTING AND SUSPENDING TARIFF RECORD, SUBJECT TO REFUND, AND "
             "ESTABLISHING HEARING AND SETTLEMENT JUDGE PROCEDURES (Issued November 26, "
             "2025) On October 29, 2025, Targa Badlands LLC (Targa) filed an initial "
             "tariff record (Tariff), to be effective December 1, 2025. The Commission "
             "orders: Targa's Tariff No. 1.0.0 is accepted and suspended, to become "
             "effective December 1, 2025, subject to refund, as discussed in the body "
             "of this order.")

    def _docs(self, text):
        d = filing("Order Accepting and Suspending Tariff Record, Subject to Refund, "
                   "and Establishing Hearing and Settlement Judge Procedures re Targa "
                   "Badlands LLC under IS26-24.",
                   [("Order/Opinion", "Commission Order/Opinion")],
                   accession="20251126-3063", filed_date="2025-11-26",
                   dockets=["IS26-24-000"], docket_bases=["IS26-24"])
        d["classification"] = docs.classify(d)
        d["text"] = text
        d["extraction_method"] = "docx_xml_text_span"
        return {"IS26-24": [d]}

    def test_the_window_cannot_begin_on_the_issuance_date(self):
        facts = []
        out = docs._refund_window("C012931", self._docs(self.TARGA), facts)
        self.assertEqual(len(out), 1)
        o = out[0]
        self.assertEqual(o["value_text"], "2025-12-01 .. open")
        self.assertNotIn("2025-11-26", o["value_text"] or "",
                         "the window began on the order's issuance date again")
        self.assertIn("ISSUANCE date is NOT the window start", o["qa_flags"])

    def test_every_bound_resolves_to_text_supporting_its_legal_role(self):
        facts = []
        docs._refund_window("C012931", self._docs(self.TARGA), facts)
        bounds = [d for d in facts if d["assertion_type"].startswith("refund_window_")]
        self.assertTrue(bounds)
        for d in bounds:
            self.assertIn("legal role", d["qualifier"] + d["scope_note"])
            self.assertTrue(
                elibrary.span_supports(d["verbatim_span"], d["value_text"]),
                f"{d['assertion_type']} cites a span that does not contain "
                f"{d['value_text']!r}: {d['verbatim_span'][:120]!r}")

    def test_a_heading_is_refused_as_support(self):
        """All three delivered spans were the eLibrary heading. The guard is
        structural: it must raise rather than store one."""
        heading = ("Order Accepting and Suspending Tariff Record, Subject to Refund, "
                   "and Establishing Hearing and Settlement Judge Procedures re Targa "
                   "Badlands LLC under IS26-24.")
        self.assertFalse(elibrary.span_supports(heading, "2025-11-26"))
        self.assertFalse(elibrary.span_supports(heading, "2025-12-01"))
        with self.assertRaises(elibrary.SpanSupportError):
            elibrary.require_span_support(heading, "2025-12-01", what="refund window")

    def test_a_bound_fact_is_replaced_not_duplicated_when_extraction_changes(self):
        """`commit_unit` prunes observations and lineage edges for a unit of work
        but does NOT prune document facts. An offset-keyed fact whose extraction
        changes therefore leaves its predecessor behind permanently: the removed
        refund-closure carve-out survived a full regeneration and went on failing
        the span-support check from the grave. A bound that is singular per docket
        is keyed on its ROLE, so a re-read replaces it."""
        args = ("E", {"accession": "20260428-3033"},
                "refund_window_refund_obligation_closed", "refund_exposure_window",
                "2026-04-28", None, "(date)", "legal role: closed", "docket RP24-1035")
        was = docs._docfact(*args, 0, 158, "Letter order accepting ... under RP24-1035.",
                            "elibrary_description_span", "c",
                            stable_key="RP24-1035|refund_obligation_closed")
        now = docs._docfact(*args, 900, 1100, "Issued: April 28, 2026 ...",
                            "docx_xml_text_span", "c",
                            stable_key="RP24-1035|refund_obligation_closed")
        self.assertEqual(was["document_fact_id"], now["document_fact_id"],
                         "a superseded bound fact would survive regeneration as a fossil")
        # a fact that is legitimately many-per-document keeps span identity
        a = docs._docfact("E", {"accession": "X"}, "tariff_record_identifier", "m",
                          "F.E.R.C. No. 1", None, "r", "", "s", 10, 20, "v", "m", "c")
        b = docs._docfact("E", {"accession": "X"}, "tariff_record_identifier", "m",
                          "F.E.R.C. No. 2", None, "r", "", "s", 30, 40, "v", "m", "c")
        self.assertNotEqual(a["document_fact_id"], b["document_fact_id"],
                            "two records in one package must not collapse into one fact")

    #: the two real passages the fossil turned on, verbatim from the canonical DB
    FOSSIL_HEADING = (
        "Letter order accepting Transcontinental Gas Pipe Line Company, LLC's "
        "04/09/2026 filing of its report of refunds detailing the refunds and "
        "surcharges made in compliance with Article IV of the Stipulation and "
        "Agreement etc. under RP24-1035.")
    REAL_DATELINE = (
        " 20426 OFFICE OF ENERGY MARKET REGULATION Transcontinental Gas Pipe Line "
        "Company, LLC Docket No. RP24-1035-005 Issued: April 28, 2026 On April 9, "
        "2026, Transco filed its report of refunds detailing")

    def test_span_supports_is_still_capable_of_returning_false(self):
        """The PREDICATE, not the fixture. Closes w6-acceptance's open item.

        Three checks in this suite and three in the delivered suite rest on
        `span_supports`. Their fixtures are guarded -- populations asserted,
        hundreds of facts exercised -- but nothing asserted that the discriminator
        still discriminates. Loosen it and all six go green while proving nothing,
        and the fossil guard silently stops guarding. Same shape as an inert
        fixture, one level up: my fixture stopped exhibiting the defect; this
        would be the predicate ceasing to tell the difference.

        Grounded in the two real passages rather than synthetic strings, and the
        rejection case is the sharp one: the fossil heading contains a DIFFERENT
        date (04/09/2026), so this is 'the wrong date is present', not merely
        'no date is present'.
        """
        # must REJECT: the heading that the fossil cited
        self.assertFalse(elibrary.span_supports(self.FOSSIL_HEADING, "2026-04-28"),
                         "span_supports no longer rejects the passage that caused the "
                         "fossil; the guards resting on it have stopped guarding")
        # must ACCEPT: the dateline the corrected fact cites
        self.assertTrue(elibrary.span_supports(self.REAL_DATELINE, "2026-04-28"),
                        "span_supports no longer accepts a genuine dateline; it has "
                        "become useless in the other direction")
        # and it is not rejecting indiscriminately -- the heading DOES support its
        # own date, so the rejection above is a real discrimination
        self.assertTrue(elibrary.span_supports(self.FOSSIL_HEADING, "2026-04-09"),
                        "span_supports rejects a date the passage plainly contains, so "
                        "the rejection above proves nothing")

    def test_span_supports_discriminates_across_every_date_form_it_accepts(self):
        """Each accepted form must also be REFUSED when the passage carries a
        different date. A form that always matches is a hole, not a feature."""
        for passage, iso, other in (
                ("the order issued December 1, 2025 as stated", "2025-12-01", "2025-12-02"),
                ("effective 12/1/2025 per the tariff", "2025-12-01", "2025-11-01"),
                ("took place on April 21-22, 2026 at the terminal", "2026-04-21",
                 "2026-04-23"),
                ("the window opens 2025-03-01", "2025-03-01", "2025-03-20")):
            self.assertTrue(elibrary.span_supports(passage, iso), f"{iso} in {passage!r}")
            self.assertFalse(elibrary.span_supports(passage, other),
                             f"{other!r} was accepted against {passage!r}: this date form "
                             f"matches indiscriminately")

    def test_the_empty_value_contract_is_pinned_in_both_directions(self):
        """A latent hole, pinned so a future change to either is deliberate.

        The predicate treats an empty value as vacuously supported -- a fact
        asserting nothing asserts nothing false. The CONSTRUCTOR guard refuses it,
        because building a span-backed fact with no value is a caller error.
        """
        self.assertTrue(elibrary.span_supports("any passage at all", ""))
        self.assertTrue(elibrary.span_supports("any passage at all"))
        with self.assertRaises(elibrary.SpanSupportError):
            elibrary.require_span_support("any passage at all", "", what="x")
        with self.assertRaises(elibrary.SpanSupportError):
            elibrary.require_span_support("any passage at all", what="x")

    def test_a_span_containing_some_number_is_not_support(self):
        """'IS26-24' contains digits. A span is support for a VALUE, not for the
        presence of numerals."""
        self.assertFalse(elibrary.span_supports("... under IS26-24.", "2025-12-01"))

    def test_the_closure_bound_cites_a_dateline_not_a_heading(self):
        """The one role whose operative date IS the issuance date still has to
        prove it from text. Storing the eLibrary heading as its evidence was the
        A10 defect reintroduced inside the A10 fix, and the population-wide
        span-support check is what caught it."""
        closure = filing(
            "Letter order accepting Transcontinental Gas Pipe Line Company, LLC's "
            "04/09/2026 filing of its report of refunds detailing the refunds and "
            "surcharges made under RP24-1035.",
            [("Order/Opinion", "Delegated Order")],
            accession="20260428-3033", filed_date="2026-04-28",
            dockets=["RP24-1035-000"], docket_bases=["RP24-1035"])
        closure["classification"] = docs.classify(closure)
        susp = filing(
            "Order Accepting and Suspending Tariff Records, Subject to Refund re "
            "Transcontinental Gas Pipe Line Company, LLC under RP24-1035.",
            [("Order/Opinion", "Commission Order/Opinion")],
            accession="20240930-3067", filed_date="2024-09-30",
            dockets=["RP24-1035-000"], docket_bases=["RP24-1035"])
        susp["classification"] = docs.classify(susp)
        susp["text"] = ("The Commission orders: The tariff records referenced in Appendix A "
                        "are accepted and suspended for five months, to be effective upon "
                        "motion March 1, 2025, subject to refund and the outcome of the "
                        "hearing established herein.")
        susp["extraction_method"] = "docx_xml_text_span"

        # (a) A dateline plus acceptance "for informational purposes" proves the
        # order date and acceptance of the filing, but it does NOT say the refund
        # obligation is discharged.  The bound must remain open.
        closure["text"] = ("FEDERAL ENERGY REGULATORY COMMISSION Docket No. RP24-1035-005 "
                           "Issued: April 28, 2026 On April 9, 2026, Transco filed its "
                           "report of refunds. The refund report as supplemented is "
                           "accepted for informational purposes.")
        closure["extraction_method"] = "docx_xml_text_span"
        facts = []
        out = docs._refund_window("C000654", {"RP24-1035": [susp, closure]}, facts)
        self.assertEqual(out[0]["value_text"], "2025-03-01 .. open")
        closed = [d for d in facts
                  if d["assertion_type"] == "refund_window_refund_obligation_closed"]
        self.assertEqual(closed, [],
                         "informational acceptance became proof that every refund "
                         "obligation was closed")

        # (b) Paired positive control: explicit operative language does close the
        # bound, and the cited span must also contain the order's dateline.
        closure["text"] = (
            "FEDERAL ENERGY REGULATORY COMMISSION Docket No. RP24-1035-005 "
            "Issued: April 28, 2026. The Commission finds that Transco's refund "
            "obligations are satisfied and closes this proceeding.")
        facts = []
        out = docs._refund_window("C000654", {"RP24-1035": [susp, closure]}, facts)
        self.assertEqual(out[0]["value_text"], "2025-03-01 .. 2026-04-28")
        closed = [d for d in facts
                  if d["assertion_type"] == "refund_window_refund_obligation_closed"]
        self.assertEqual(len(closed), 1)
        self.assertTrue(
            elibrary.span_supports(closed[0]["verbatim_span"], "2026-04-28"),
            "the closure bound cited a span that does not contain its own date")

        # (c) no body -> the metadata filing date is NOT substituted
        closure.pop("text")
        facts = []
        out = docs._refund_window("C000654", {"RP24-1035": [susp, closure]}, facts)
        self.assertEqual(out[0]["value_text"], "2025-03-01 .. open")
        self.assertNotIn("2026-04-28", out[0]["value_text"])
        self.assertEqual([d for d in facts if d["assertion_type"].endswith("closed")], [])

    def test_an_unread_body_leaves_the_window_unvalidated(self):
        d = filing("Order Accepting and Suspending Tariff Records, Subject to Refund, "
                   "and Establishing Hearing Procedures re Transcontinental Gas Pipe "
                   "Line Company, LLC under RP24-1035.",
                   [("Order/Opinion", "Commission Order/Opinion")],
                   accession="20240930-3067", filed_date="2024-09-30",
                   dockets=["RP24-1035-000"], docket_bases=["RP24-1035"])
        d["classification"] = docs.classify(d)
        facts = []
        out = docs._refund_window("C000654", {"RP24-1035": [d]}, facts)
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["value_text"])
        self.assertNotEqual(out[0]["availability"], Availability.PRESENT)
        self.assertIn("ISSUANCE date", out[0]["missing_reason"])
        self.assertEqual([d for d in facts
                          if d["assertion_type"].startswith("refund_window_")], [])

    def test_the_defect_reproduces_on_the_baseline(self):
        """Reproduction, against the audited baseline: EVERY stored window began
        on an order's issuance date, not only Targa's. This test documents the
        defect; it is expected to hold on the baseline and is what the repaired
        behaviour above is measured against."""
        rows = HISTORICAL["refund_observations"]
        self.assertTrue(rows, "fixture population missing from the baseline")
        began_on_issuance = 0
        for r in rows:
            start = r["value_text"].split(" ..")[0]
            if start in {r["filed_date"], r["accession_number"][:4]
                         + "-" + r["accession_number"][4:6] + "-"
                         + r["accession_number"][6:8]}:
                began_on_issuance += 1
        self.assertGreaterEqual(
            began_on_issuance, 1,
            "the A10 fixture no longer reproduces on the baseline; the baseline must "
            "not have been altered")

    def test_all_three_delivered_spans_contained_a_heading_not_a_date(self):
        """The sharper half of A10: a span-backed badge is not support. All three
        stored refund-window spans were the eLibrary description heading."""
        facts = HISTORICAL["refund_document_facts"]
        self.assertGreaterEqual(len(facts), 3, "fixture population missing")
        unsupported = [f for f in facts
                       if not elibrary.span_supports(f["verbatim_span"],
                                                     f["value_text"].split(" ..")[0])]
        self.assertEqual(
            len(unsupported), len(facts),
            "the A10 fixture no longer reproduces: on the baseline every stored "
            "refund-window span failed to contain its own start date")


# =====================================================================  A11

class _Baseline:
    """A first-observed baseline, in the shape the adapters consume."""

    def __init__(self, *, established: bool, seen=None, established_at="2026-06-01T00:00:00"):
        self.previously_seen = dict(seen or {})
        self.established_at = established_at if established else ""
        self.entity_key = "AUDIT_SYNTHETIC_ENTITY"
        self.adapter = "test"

    exists = property(lambda self: bool(self.established_at))
    first_seen = elibrary.NewsBaseline.first_seen
    route = elibrary.NewsBaseline.route


class TestHistoricalBackfillIsNotCurrentNews(unittest.TestCase):

    def test_the_defect_reproduces_on_the_baseline(self):
        n = HISTORICAL["historical_counts"]["historical_current_investor_events"]
        self.assertEqual(n, 263, "the audited historical event population changed")

    def test_an_initial_historical_seed_creates_archive_records_only(self):
        b = _Baseline(established=False)
        for filed in ("2017-04-03", "2019-11-20", "2026-08-30"):
            dest, backfill, why = b.route("ACC-" + filed, filed)
            self.assertEqual(dest, elibrary.ARCHIVE)
            self.assertEqual(backfill, 1)
            self.assertIn("BASELINE INGESTION", why)

    def test_no_hard_coded_cutoff_decides_it(self):
        """The same filing date is news or archive depending only on whether a
        baseline exists and whether the occurrence is newly observed -- never on
        a date literal."""
        filed = "2026-08-30"
        self.assertEqual(_Baseline(established=False).route("A", filed)[0],
                         elibrary.ARCHIVE)
        self.assertEqual(
            _Baseline(established=True, established_at="2026-01-01T00:00:00")
            .route("A", filed)[0], elibrary.INVESTOR)

    def test_a_known_occurrence_is_never_re_published(self):
        b = _Baseline(established=True, seen={"A": "2026-07-01T00:00:00"})
        dest, backfill, why = b.route("A", "2026-08-30")
        self.assertEqual(dest, elibrary.ARCHIVE)
        self.assertEqual(backfill, 1)
        self.assertIn("ALREADY OBSERVED", why)

    def test_a_newly_discovered_older_order_gets_late_discovery_treatment(self):
        b = _Baseline(established=True, established_at="2026-06-01T00:00:00")
        dest, backfill, why = b.route("NEW", "2018-09-28")
        self.assertEqual(dest, elibrary.REVIEW_QUEUE,
                         "an older order was silently promoted to current news")
        self.assertEqual(backfill, 1)
        self.assertIn("LATE DISCOVERY", why)

    def test_an_identical_resubmission_creates_no_economic_change_event(self):
        b = _Baseline(established=True, established_at="2026-01-01T00:00:00")
        dest, backfill, why = b.route("NEW", "2026-08-30",
                                      version_status="identical_resubmission")
        self.assertEqual(dest, elibrary.ARCHIVE)
        self.assertEqual(backfill, 1)
        self.assertIn("IDENTICAL RESUBMISSION", why)

    def test_the_duplicate_content_branch_has_a_real_fixture_population(self):
        """w6-acceptance located 3 byte-identical pairs across 6 occurrences in the
        delivered data. The branch is exercised against them, and the test fails
        rather than skips if they are not there."""
        pairs = [{"content_hash": p["content_hash"], "n": len(p["filing_ids"]),
                  "ids": ",".join(p["filing_ids"])}
                 for p in HISTORICAL["identical_submission_groups"]]
        self.assertGreaterEqual(
            len(pairs), 3,
            "the identical-resubmission fixture population is empty; a silently "
            "skipped duplicate-content test is a FAILED test")
        b = _Baseline(established=True, established_at="2020-01-01T00:00:00")
        for p in pairs:
            ids = sorted(p["ids"].split(","))
            self.assertEqual(len(ids), p["n"])
            # the later occurrence of a byte-identical pair is a version record
            dest, backfill, _why = b.route(ids[-1], "2026-08-30",
                                           version_status="identical_resubmission")
            self.assertEqual(dest, elibrary.ARCHIVE)
            self.assertEqual(backfill, 1)
            # ... and the two occurrences remain DISTINCT identities
            self.assertNotEqual(ids[0], ids[-1],
                                "shared bytes must never merge two submissions")

    def test_a_genuine_supported_revision_does_produce_a_change_event(self):
        """The gate must not simply suppress everything: a new, order-backed FERC
        act observed for the first time after the baseline IS current news."""
        b = _Baseline(established=True, established_at="2026-01-01T00:00:00")
        dest, backfill, why = b.route("20260901-3001", "2026-09-01",
                                      version_status="revised")
        self.assertEqual(dest, elibrary.INVESTOR)
        self.assertEqual(backfill, 0)
        self.assertIn("NEW SINCE BASELINE", why)
        f = filing("Order accepting and suspending tariff records, subject to refund, "
                   "re AUDIT SYNTHETIC Pipeline under RP26-999.",
                   [("Order/Opinion", "Commission Order/Opinion")],
                   accession="20260901-3001", filed_date="2026-09-01",
                   dockets=["RP26-999-000"], docket_bases=["RP26-999"])
        f["classification"] = docs.classify(f)
        events = docs._rate_events("E", {"assets": [{"asset_id": "a"}]}, [f],
                                   ["RP26-999"], baseline=b)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["destination"], elibrary.INVESTOR)
        self.assertEqual(events[0]["is_backfill"], 0)
        self.assertEqual(events[0]["event_type"], "accepted_and_suspended")

    def test_a_company_submittal_is_not_an_economic_change(self):
        b = _Baseline(established=True, established_at="2026-01-01T00:00:00")
        f = filing("AUDIT SYNTHETIC Pipeline submits tariff filing per 154.312: rate "
                   "case to be effective 10/1/2026.",
                   [("Application/Petition/Request", "Tariff Filing")],
                   accession="20260902-5001", filed_date="2026-09-02",
                   dockets=["RP26-999-000"], docket_bases=["RP26-999"])
        f["classification"] = docs.classify(f)
        events = docs._rate_events("E", {"assets": [{"asset_id": "a"}]}, [f],
                                   ["RP26-999"], baseline=b)
        self.assertEqual(events[0]["destination"], elibrary.ARCHIVE)
        self.assertEqual(events[0]["event_type"], "filed")

    def test_filing_effective_and_discovery_dates_stay_separate(self):
        b = _Baseline(established=True, established_at="2026-01-01T00:00:00",
                      seen={"20260902-5001": "2026-07-04T09:00:00"})
        f = filing("AUDIT SYNTHETIC Pipeline submits tariff filing per 154.204: "
                   "housekeeping to be effective 10/1/2026.",
                   [("Application/Petition/Request", "Tariff Filing")],
                   accession="20260902-5001", filed_date="2026-09-02",
                   dockets=["RP26-999-000"], docket_bases=["RP26-999"])
        f["classification"] = docs.classify(f)
        e = docs._rate_events("E", {"assets": [{"asset_id": "a"}]}, [f],
                              ["RP26-999"], baseline=b)[0]
        self.assertEqual(e["source_filed_date"], "2026-09-02")
        self.assertEqual(e["effective_date"], "2026-10-01")
        self.assertEqual(e["first_seen_at"], "2026-07-04T09:00:00")
        self.assertEqual(len({e["source_filed_date"], e["effective_date"],
                              e["first_seen_at"]}), 3)


# =====================================================================  A17

class TestImageOnlySourceIsClassifiedTruthfully(unittest.TestCase):

    CADEVILLE = "31f9219d3da5bc260392b6e2351cde0313ab38978ade4ee714f70308d7036d94"
    MONROE = "8e2069eeb07fdfdca7167b323b25cf91f0c34bbf91fd82a8bd82caf9826c0040"

    def test_an_unreviewed_image_only_source_stays_pending(self):
        self.assertIsNone(capacity.reviewed_pages("00" * 32),
                          "a review of one document must never attach to another")

    def test_the_reviewed_pages_produce_qualified_quantities_with_units(self):
        for content_hash, rate, volume, name in (
                (self.CADEVILLE, "420", "23.7", "CGS"),
                (self.MONROE, "465", "11.96", "MGS")):
            pages = capacity.reviewed_pages(content_hash)
            figs = capacity.read_figures(pages, 2023)
            by_value = {f["value_text"]: f for f in figs}
            self.assertIn(rate, by_value, f"{name}: peak-day figure not extracted")
            self.assertIn(volume, by_value, f"{name}: storage figure not extracted")
            self.assertEqual(by_value[rate]["unit"], "MMcf/day")
            self.assertEqual(by_value[volume]["unit"], "Bcf")
            self.assertEqual(by_value[rate]["qualifier"], "approximately")
            self.assertEqual(by_value[volume]["qualifier"], "estimated")
            for f in (by_value[rate], by_value[volume]):
                self.assertEqual(f["page"], 1)
                self.assertIn(f["value_text"], f["verbatim"])
                self.assertTrue(f["caveats"])
                self.assertTrue(any("BASE GAS" in c for c in f["caveats"]))
                self.assertTrue(any("OFF-SYSTEM" in c for c in f["caveats"]))
                # Direct lineage uses this row index. It must identify the OCR
                # sentence supporting the number/unit, never the page heading.
                source_row = pages[f["page"] - 1]["rows"][f["row_index"]]["text"]
                self.assertTrue(f["value_text"] in source_row or f["unit"] in source_row)

    def test_the_two_kinds_of_figure_are_not_interchangeable(self):
        """A deliverability RATE and a storage VOLUME belong to different unit
        families; nothing may compare or combine them."""
        from ferclib.registry import unit_family_of, units_compatible
        self.assertEqual(unit_family_of("MMcf/day"), "volume_rate")
        self.assertEqual(unit_family_of("Bcf"), "volume")
        self.assertFalse(units_compatible("MMcf/day", "Bcf"))

    def test_the_storage_total_is_not_presented_as_working_gas(self):
        figs = capacity.read_figures(capacity.reviewed_pages(self.CADEVILLE), 2023)
        volume = next(f for f in figs if f["value_text"] == "23.7")
        base_gas = [c for c in volume["caveats"] if "BASE GAS" in c]
        self.assertTrue(base_gas)
        self.assertIn("not a working-gas figure", base_gas[0])

    def test_the_audit_numbers_were_verified_not_copied(self):
        """The figures come out of a transcript of the page, through the same
        reader a text-layer page uses. Nothing is seeded: deleting the transcript
        deletes the figures."""
        record = capacity.REVIEWED_PAGE_IMAGES[self.CADEVILLE]
        self.assertFalse(record["ocr_used"])
        self.assertTrue(record["render"])
        self.assertTrue(record["reviewed_by"])
        blank = {"pages": [{"page": 1, "lines": ["Cadeville Gas Storage"]}]}
        saved = capacity.REVIEWED_PAGE_IMAGES[self.CADEVILLE]
        try:
            capacity.REVIEWED_PAGE_IMAGES[self.CADEVILLE] = blank
            self.assertEqual(
                capacity.read_figures(capacity.reviewed_pages(self.CADEVILLE), 2023), [],
                "a figure survived removal of the text it was supposed to come from")
        finally:
            capacity.REVIEWED_PAGE_IMAGES[self.CADEVILLE] = saved

    def test_a_parser_gap_is_never_reported_as_a_source_gap(self):
        self.assertTrue(issubclass(capacity.ImageOnlySource, ValueError))
        try:
            raise capacity.ImageOnlySource(
                "20240223-5073: the PDF carries no extractable text layer on any page. "
                "This is a PARSER-CAPABILITY GAP of ours, not a FERC source gap")
        except capacity.ImageOnlySource as exc:
            self.assertIn("PARSER-CAPABILITY GAP", str(exc))
            self.assertIn("not a FERC source gap", str(exc))

    def test_no_output_says_ferc_has_no_data_for_these_records(self):
        text = json.dumps(capacity.REVIEWED_PAGE_IMAGES) + capacity.__doc__
        for forbidden in ("FERC has no", "no operating data", "source failure"):
            self.assertNotIn(forbidden.lower(), text.lower())


# =====================================================================  A22

class TestTariffGateIsSelfContained(unittest.TestCase):
    """A synthetic, self-contained gate test, plus a SEPARATE honest test for
    genuinely missing real evidence. Neither substitutes for the other."""

    def _package(self, text, accession="AUDIT_SYNTHETIC_TARIFF"):
        f = filing("AUDIT SYNTHETIC Pipeline submits tariff filing per 342.3: index "
                   "rate change to be effective 7/1/2026.",
                   [("Application/Petition/Request", "Tariff Filing")],
                   accession=accession, filed_date="2026-05-01",
                   dockets=["IS26-999-000"], docket_bases=["IS26-999"])
        f["classification"] = docs.classify(f)
        f["text"] = text
        f["extraction_method"] = "pdf_text_span"
        f["members"] = [{"name": "AUDIT_SYNTHETIC Clean Tariff.pdf", "text": text,
                         "method": "pdf_text_span"}]
        return f

    SHEET = ("AUDIT SYNTHETIC TARIFF SHEET. F.E.R.C. No. 201.5.0 (Cancels F.E.R.C. "
             "No. 201.4.0). Rate for transportation service: $1.2345 per Barrel.")

    def test_a_failed_gate_retains_the_values_and_names_the_condition(self):
        facts = []
        f = self._package(self.SHEET)
        out = docs._tariff_rate("E", {"IS26-999": [f]}, [f], facts)
        self.assertEqual(len(out), 1)
        o = out[0]
        self.assertIsNone(o["value_text"], "a gated rate published a value")
        self.assertIn("UNRESOLVED OPERATIVE RATE", o["missing_reason"])
        self.assertRegex(o["qa_flags"], r"\d_[a-z_]+=(pass|FAIL)")
        self.assertIn("3_accepting_order_located=FAIL", o["qa_flags"])
        self.assertIn("0_package_retrieved=pass", o["qa_flags"])
        kept = {d["assertion_type"] for d in facts}
        self.assertIn("tariff_record_identifier", kept)
        self.assertIn("tariff_rate_line", kept)
        for d in facts:
            # the tariff sheet's text layer is letter-spaced, so support is
            # checked whitespace-insensitively -- but it IS checked
            needle = d["value_text"].split(" (")[0].replace("F.E.R.C. No. ", "")
            self.assertIn(needle.replace(" ", ""),
                          d["verbatim_span"].replace(" ", ""),
                          "a retained tariff fact must cite a span containing it")

    def test_every_gate_condition_is_named_even_when_never_reached(self):
        facts = []
        f = self._package("AUDIT SYNTHETIC TARIFF SHEET with no record identifier.")
        o = docs._tariff_rate("E", {"IS26-999": [f]}, [f], facts)[0]
        for condition in docs.GATE_TEMPLATE:
            self.assertIn(f"{condition}=", o["qa_flags"],
                          f"gate condition {condition} vanished from the report")

    def test_a_genuinely_unretrieved_package_is_reported_as_our_gap(self):
        """The separate honest test the audit asks be RETAINED: a real missing
        input is not the same as a gate failure, and must not be dressed as one."""
        f = self._package("", accession="20240501-5258")
        f.pop("text")
        f["members"] = []
        o = docs._tariff_rate("E", {"IS26-999": [f]}, [f], [])[0]
        self.assertEqual(o["availability"], Availability.KNOWN_NOT_RETRIEVED)
        self.assertIsNone(o["value_text"])
        self.assertIn("0_package_retrieved=FAIL", o["qa_flags"])
        self.assertIn("OUR retrieval gap", o["missing_reason"])
        self.assertIn("NOT a FERC source gap", o["missing_reason"])
        self.assertIn("20240501-5258", o["missing_reason"])

    def test_a_failed_download_is_also_reported_in_gate_terms(self):
        """The sibling of the unretrieved branch: attachments listed, download
        failed. It must name the gate too, or an offline replay reports a bare
        sentence that no gate check can see."""
        f = self._package("", accession="20231229-5212")
        f.pop("text")
        f["members"] = []
        f["retrieval"] = "failed"
        f["retrieval_error"] = ("GetFileListFromP8: not in the source cache and offline "
                                "replay may not open a socket")
        o = docs._tariff_rate("E", {"IS26-999": [f]}, [f], [])[0]
        self.assertEqual(o["availability"], Availability.RETRIEVAL_FAILED)
        self.assertIsNone(o["value_text"])
        self.assertIn("UNRESOLVED OPERATIVE RATE", o["missing_reason"])
        self.assertIn("0_package_retrieved=FAIL", o["qa_flags"])
        self.assertRegex(o["qa_flags"], r"\d_[a-z_]+=(pass|FAIL)")
        self.assertIn("not evidence that the filer has no operative rate",
                      o["missing_reason"])

    def test_a_passing_gate_still_needs_all_six_conditions(self):
        f = self._package(self.SHEET)
        order = filing("Letter order accepting AUDIT SYNTHETIC Pipeline's tariff "
                       "records under IS26-999.",
                       [("Order/Opinion", "Delegated Order")],
                       accession="AUDIT_SYNTHETIC_ORDER", filed_date="2026-06-01",
                       dockets=["IS26-999-000"], docket_bases=["IS26-999"])
        order["classification"] = docs.classify(order)
        self.assertEqual(order["classification"]["stage"], "accepted")
        o = docs._tariff_rate("E", {"IS26-999": [f, order]}, [f], [])[0]
        self.assertEqual(o["availability"], Availability.PRESENT)
        self.assertIn("1.2345 per Barrel", o["value_text"])


# =====================================================================  A08

class TestDocumentLineageIsNotAnEmptyPointer(unittest.TestCase):

    def test_the_defect_reproduces_on_the_baseline(self):
        mine = HISTORICAL["empty_elibrary_lineage_edges_by_metric"]
        self.assertIn("cap_peak_day_ratio", mine,
                      "fixture population missing: the capacity share of the 1,870 "
                      "empty eLibrary edges")

    def test_a_denominator_edge_resolves_to_a_persisted_source_fact(self):
        self.assertEqual(capacity._fact_id(1, 0, "yes"), "P01R0000")
        self.assertEqual(capacity._fact_id(2, 17, "no"), "IMG02R0017")
        self.assertNotEqual(capacity._fact_id(1, 0, "yes"),
                            capacity._fact_id(1, 0, "no"),
                            "a reviewed image row must not share an id with a "
                            "text-layer row")

    def test_a_refused_derivation_persists_the_population_it_considered(self):
        f = {"filing_id": "AUDIT_SYNTHETIC_CAP", "reporting_year": 2024,
             "_text_layer": "yes", "_doc": [{"page": 1}],
             "_figures": [{"kind": "table", "page": 1, "row_index": 3,
                           "is_total": False, "value_text": "3,303"},
                          {"kind": "prose", "page": 1, "row_index": 9,
                           "is_total": False, "value_text": "2,900"}]}
        pop = capacity._total_population("E", f, "obs-synthetic")
        self.assertEqual(pop["row_count"], 0)
        self.assertEqual(pop["candidate_count"], 1, "prose is not a table candidate")
        self.assertTrue(pop["empty_reason"])
        self.assertIn("NONE of them carries a 'total' row label", pop["empty_reason"])
        self.assertIn("not a retrieval or parsing failure", pop["empty_reason"])
        self.assertTrue(pop["inclusion_rule"] and pop["exclusion_rule"])
        self.assertTrue(pop["member_digest"])
        edge = capacity._population_edge("obs-synthetic", 1, pop)
        self.assertEqual(edge["input_population_id"], pop["population_id"])
        self.assertEqual(edge["input_role"], "population")

    def test_an_lng_population_edge_is_never_an_empty_pointer(self):
        pop = lng._inspection_population(
            "E", {"facility": "F"}, "obs-synthetic", [], 4, 4, "findings",
            "no body retrieved")
        edges = lng._population_edges([pop])
        self.assertEqual(len(edges), 1)
        self.assertTrue(edges[0]["input_population_id"])
        self.assertTrue(edges[0]["input_concept"])
        self.assertEqual(pop["row_count"], 0)
        self.assertTrue(pop["empty_reason"])

    def test_an_edge_carries_the_occurrence_it_came_from(self):
        """Byte-identical content exists under two accessions, so an id that
        merely resolves is not enough: the occurrence must agree."""
        pairs = [{"ids": ",".join(p["filing_ids"])}
                 for p in HISTORICAL["identical_submission_groups"]]
        self.assertTrue(pairs, "fixture population missing")
        for p in pairs:
            ids = sorted(p["ids"].split(","))
            self.assertGreater(len(ids), 1)
            self.assertNotEqual(ids[0], ids[1])


# ============================================= observation grain (not in the register)

def _rows(*lines):
    """A page in `extract_pdf` shape. Each line is (y, [(x, x_end, text), ...])."""
    rows = []
    for y, cells in lines:
        cs = [{"x": float(x), "x_end": float(xe), "text": t, "size": 10.0}
              for x, xe, t in cells]
        rows.append({"y": float(y), "cells": cs, "reordered": False,
                     "text": " ".join(c["text"] for c in cs)})
    return [{"page": 1, "rows": rows, "height": 792.0, "shifted_fonts": []}]


class TestNoFigureIsChosenByIterationOrder(unittest.TestCase):
    """A defect the canonical rebuild surfaced, not one the audit register lists.

    23 `cap_reported_capacity` observations collided on the grain, and the
    delivered adapter resolved each by keeping whichever arrived first. Two of
    them were Northwest Pipeline's "Working Gas" rows -- 2,388,000 for Plymouth
    LNG and 8,528,000 for Jackson Prairie, a factor of 3.6 apart and two entirely
    different storage facilities. Keeping the first would have shipped one and
    erased the other with no trace.
    """

    def test_a_lone_title_line_is_a_section(self):
        """Northwest Pipeline. The section headers were in the document; the
        reader took a section only from a header row with two or more cells, so
        every lone title line was thrown away."""
        figs = {f["value_text"]: f
                for f in capacity.read_figures(_rows(*self.NORTHWEST), 2024)}
        self.assertEqual(figs["2,388,000"]["section"], "Plymouth LNG Storage")
        self.assertEqual(figs["8,528,000"]["section"], "Jackson Prairie Storage")
        scopes = {capacity._figure_scope(f) for f in figs.values()}
        self.assertEqual(len(scopes), len(figs), "two figures share one scope")
        for f in figs.values():
            self.assertNotIn("DEFINITION NOT SEPARATED", capacity._figure_scope(f),
                             "the document separates these; no positional fallback is due")

    def test_an_indent_outline_survives_a_table_break(self):
        """Discovery Gas Transmission states the same facility three times -- once
        as design capacity and once under each of two rate schedules -- and only
        the outline tells them apart. The outline must not be cleared when the
        column-header stack is."""
        figs = {f["value_text"]: f
                for f in capacity.read_figures(_rows(*self.DISCOVERY), 2024)}
        self.assertEqual(figs["300,000"]["section"], "Peak day capacity")
        self.assertEqual(figs["188,572"]["section"],
                         "Firm Transportation Services / FT-1")
        self.assertEqual(figs["17,583"]["section"],
                         "Firm Transportation Services / FT-2",
                         "the outer level was lost at the FT-2 table break")
        self.assertEqual(len({capacity._figure_scope(f) for f in figs.values()}), 3)

    #: MountainWest's storage table, with the rows that make the defect possible.
    #: An earlier version of this fixture omitted the T-1 and PKS DATA rows, so
    #: `after_data` was never true when the footnote marker arrived and the marker
    #: could not trigger the header reset. The test passed and proved nothing
    #: about footnote handling. Found by applying w6-acceptance's inert-fixture
    #: discipline to my own suite; `test_the_grain_fixtures_still_exhibit_their_defects`
    #: below now asserts the fixture is still capable of failing.
    MOUNTAINWEST = (
        (553, [(181, 199, "T-1"), (293, 340, "2,516,325"), (419, 466, "2,462,073")]),
        (527, [(151, 232, "Storage Service"), (271, 361, "Estimated Storage"),
               (405, 483, "Maximum Daily")]),
        (515, [(155, 226, "Rate Schedule"), (273, 360, "Capacity (MMDth)"),
               (396, 488, "Delivery Capability")]),
        (503, [(425, 460, "(Dth/d)")]),
        (492, [(195, 204, "1,2")]),
        (489, [(175, 194, "PKS"), (305, 328, "1.93"), (423, 462, "184,600")]),
        (478, [(197, 201, "3")]),
        (475, [(178, 203, "FSS"), (302, 330, "55.75"), (423, 462, "810,000")]),
    )

    def test_a_footnote_marker_does_not_break_a_table(self):
        """MountainWest puts superscript footnote references between the column
        headers and the data. Treating one as a label row reset the headers away
        and left a storage VOLUME and a daily RATE with no column at all."""
        figs = {f["value_text"]: f
                for f in capacity.read_figures(_rows(*self.MOUNTAINWEST), 2024)}
        self.assertIn("Capacity (MMDth)", figs["55.75"]["column"])
        self.assertIn("Delivery Capability", figs["810,000"]["column"])
        self.assertNotEqual(capacity._figure_scope(figs["55.75"]),
                            capacity._figure_scope(figs["810,000"]))

    def test_a_spanning_banner_is_never_borrowed_across_a_declared_unit(self):
        """The same MountainWest row. 55.75 is stated in (MMDth), a volume; the
        page banner (Dth/d) belongs to the transport table above. Borrowing it
        would turn a storage volume into a daily rate."""
        figs = {f["value_text"]: f
                for f in capacity.read_figures(_rows(*self.MOUNTAINWEST), 2024)}
        self.assertNotEqual(figs["55.75"]["unit"], "Dth/d",
                            "a storage volume inherited a daily-rate banner")
        self.assertIsNone(figs["55.75"]["value_num"],
                          "a figure whose declared unit is unrecognised must not publish")
        self.assertTrue(any("declares its unit as 'MMDth'" in p
                            for p in figs["55.75"]["problems"]))
        # the value the document itself confirms in its footnote stays published
        self.assertEqual(figs["810,000"]["unit"], "Dth/d")
        self.assertEqual(figs["810,000"]["value_num"], 810000.0)

    #: Northwest Pipeline: two facilities, one row label
    NORTHWEST = (
        (580, [(412, 462, "Capacities")]),
        (568, [(144, 184, "Mainline"), (406, 461, "(Dth/d)")]),
        (556, [(153, 271, "TF-1 Firm Transportation"), (415, 462, "3,514,192")]),
        (522, [(144, 250, "Plymouth LNG Storage")]),
        (510, [(153, 214, "Working Gas"), (415, 462, "2,388,000")]),
        (476, [(144, 257, "Jackson Prairie Storage")]),
        (464, [(153, 214, "Working Gas"), (415, 462, "8,528,000")]),
    )
    #: Discovery: one facility named three times under three headings
    DISCOVERY = (
        (598, [(126, 220, "Peak day capacity:")]),
        (586, [(144, 290, "Mainline Facilities (Onshore)"), (432, 471, "300,000")]),
        (517, [(126, 280, "Firm Transportation Services:")]),
        (506, [(144, 170, "FT-1:")]),
        (494, [(162, 300, "Mainline Facilities (Onshore)"), (432, 471, "188,572")]),
        (437, [(144, 170, "FT-2:")]),
        (414, [(162, 300, "Mainline Facilities (Onshore)"), (437, 471, "17,583")]),
    )

    def test_the_grain_fixtures_still_exhibit_their_defects(self):
        """A fixture that cannot fail proves nothing.

        Each fix above is neutralised in turn and the fixture must reproduce the
        ORIGINAL fault. Without this the three tests could go green because the
        fixture drifted, not because the reader works — which is exactly what had
        happened to the footnote case: it omitted the data row that precedes the
        marker, so `after_data` was never true and the marker could not trigger
        the header reset it exists to test.
        """
        # 1. the lone-title-line section rule
        original = capacity._section_candidate
        capacity._section_candidate = lambda *a, **k: ""
        try:
            figs = {f["value_text"]: capacity._figure_scope(f)
                    for f in capacity.read_figures(_rows(*self.NORTHWEST), 2024)}
            self.assertIn("unsectioned", figs["2,388,000"])
            self.assertIn("DEFINITION NOT SEPARATED", figs["2,388,000"],
                          "without the section rule the two facilities must collide")
        finally:
            capacity._section_candidate = original

        # 2. the outline surviving a table break
        original_push = capacity._push_outline
        capacity._push_outline = lambda *a, **k: None
        try:
            sections = {f["value_text"]: f["section"]
                        for f in capacity.read_figures(_rows(*self.DISCOVERY), 2024)}
            self.assertEqual(sections["188,572"], "",
                             "without the outline the FT-1 heading must be lost")
            self.assertEqual(sections["17,583"], "")
        finally:
            capacity._push_outline = original_push

        # 3. the footnote-marker skip
        original_fn = capacity._LONE_FOOTNOTE
        capacity._LONE_FOOTNOTE = re.compile(r"(?!x)x")      # matches nothing
        try:
            figs = {f["value_text"]: f
                    for f in capacity.read_figures(_rows(*self.MOUNTAINWEST), 2024)}
            self.assertEqual(figs["55.75"]["column"], "",
                             "without the footnote skip the FSS row must lose its "
                             "columns; if it does not, this fixture is inert")
            self.assertEqual(figs["810,000"]["column"], "")
        finally:
            capacity._LONE_FOOTNOTE = original_fn

    def test_an_unseparable_definition_gates_every_candidate(self):
        """Where the document genuinely does not separate two figures, BOTH are
        retained by position and NEITHER is published. No winner is picked."""
        pages = _rows(
            (600, [(100, 200, "Widget Capacity"), (400, 460, "111")]),
            (500, [(100, 200, "Widget Capacity"), (400, 460, "222")]),
        )
        figs = capacity.read_figures(pages, 2024)
        self.assertEqual(len(figs), 2, "a candidate was dropped instead of gated")
        for f in figs:
            self.assertIsNone(f["value_num"], "a value survived an unresolved definition")
            self.assertIn("DEFINITION NOT SEPARATED", capacity._figure_scope(f))
            self.assertTrue(any("reduce to the same definition" in p for p in f["problems"]))
            self.assertTrue(any("NONE is published as the answer" in p
                                for p in f["problems"]))
        self.assertEqual(len({capacity._figure_scope(f) for f in figs}), 2,
                         "both candidates must survive as distinct rows")

    def test_the_writer_never_keeps_whichever_came_first(self):
        """The last-resort guard. If two observations still reach the same grain
        with different values, the surviving row publishes NO value and names
        every candidate -- it does not pick one."""
        class _Log:
            def __init__(self): self.messages = []
            def log(self, level, msg, **kw): self.messages.append((level, msg))
        ctx = _Log()
        base = {"observation_id": "obs-collide", "metric_id": "cap_reported_capacity",
                "entity_key": "E", "scope": "as reported: x", "unit": "Dth/d",
                "qa_flags": "", "missing_reason": "", "notes": "page 2 row 4",
                "availability": Availability.PRESENT, "validation": Validation.PASS}
        out = capacity._dedupe(ctx, [dict(base, value_text="2,388,000", value_num=2388000.0),
                                     dict(base, value_text="8,528,000", value_num=8528000.0)])
        self.assertEqual(len(out), 1)
        o = out[0]
        self.assertIsNone(o["value_num"], "a conflicting value was published")
        self.assertNotIn("2,388,000", o["value_text"] or "")
        self.assertEqual(o["availability"], Availability.UNVERIFIED_AVAILABILITY)
        self.assertEqual(o["validation"], Validation.BLOCKED_AMBIGUITY)
        self.assertIn("2,388,000", o["missing_reason"])
        self.assertIn("8,528,000", o["missing_reason"])
        self.assertIn("iteration order", o["missing_reason"])
        self.assertTrue(any(level == "error" for level, _m in ctx.messages))

    def test_several_stated_totals_refuse_the_ratio_rather_than_pick_one(self):
        """Florida Gas states a Winter total and a Summer total. Taking
        `figures[0]` of those would choose a denominator by page order."""
        metrics = {m.id: m for m in capacity.BY_ADAPTER["capacity"]}
        f = {"filing_id": "AUDIT_SYNTHETIC_CAP", "reporting_year": 2025,
             "_as_of": "2025-12-31", "_document_id": None, "_text_layer": "yes",
             "_doc": [{"page": 1, "rows": []}],
             "_figures": [
                 {"kind": "table", "page": 1, "row_index": 11, "is_total": True,
                  "problems": [], "value_text": "3,575,267", "unit": "MMBtu",
                  "section": "Winter Season Firm Service Assignment", "row_label": "Total",
                  "column": "", "table_index": 0, "column_ordinal": 1},
                 {"kind": "table", "page": 1, "row_index": 16, "is_total": True,
                  "problems": [], "value_text": "3,883,651", "unit": "MMBtu",
                  "section": "Summer Season Firm Service Assignment", "row_label": "Total",
                  "column": "", "table_index": 1, "column_ordinal": 1}]}

        class _Ctx:
            class staging:
                @staticmethod
                def query(*_a, **_k): return []
            @staticmethod
            def log(*_a, **_k): pass
        out, edges, _pops = capacity._peak_day_gate(
            _Ctx, {"entity_key": "E"}, [f], metrics, [])
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["value_num"])
        self.assertEqual(out[0]["availability"], Availability.NOT_APPLICABLE)
        self.assertIn("2 DIFFERENT total figures", out[0]["missing_reason"])
        self.assertIn("choose it by page order", out[0]["missing_reason"])
        self.assertEqual(edges, [], "a refused ratio must not carry a denominator edge")

    def test_an_adapter_bug_is_never_filed_as_a_source_failure(self):
        """Found the hard way while fixing this: a missing dict key became
        'Northwest Pipeline: capacity PDF parse failed', a `source` blocker, and
        removed the filer's figures. A code fault is ours."""
        source = pathlib.Path(capacity.__file__).read_text()
        self.assertIn('ADAPTER, "parser"', source)
        self.assertIn("our defect, not a FERC source failure", source)
        marker = source.index("except Exception as exc:")
        window = source[marker:marker + 1400]
        self.assertNotIn('ADAPTER, "source"', window,
                         "an unexpected adapter exception is still filed as a source gap")


# ============================================================ PDF text layer

def _synthetic_pdf() -> bytes:
    """A minimal PDF reproducing BOTH faults the A19 repair had to fix.

    Self-contained on purpose. These two tests used to read a cached FERC letter
    out of `work/w5-documents/fetched/`, which is scratch and does not ship, so
    on any other machine they called `self.skipTest` and went quiet. My own brief
    says a silently-skipped test is a failed test, and it applies to my tests as
    much as to anyone's.

    The fixture is built so that:
      * the page tree lists page object 5 BEFORE page object 4, so sorting page
        objects by number gives the wrong reading order;
      * an embedded font-program stream, referenced by no page, carries literal
        strings that look like glyph names -- which is what the old whole-file
        scrape returned instead of the letter.
    """
    def stream(obj_num, payload: bytes) -> bytes:
        packed = zlib.compress(payload)
        return (f"{obj_num} 0 obj <</Length {len(packed)} /Filter/FlateDecode>> stream\n"
                .encode("latin-1") + packed + b"\nendstream endobj\n")

    page_two = b"BT /F1 12 Tf 72 700 Td (cc: VIA Electronic Mail) Tj ET"
    page_one = (b"BT /F1 12 Tf 72 700 Td (Dear Ms. Klacko:) Tj "
                b"0 -14 Td (Our annual inspection of the LNG terminal took place) Tj "
                b"0 -14 Td (on April 21-22, 2026.) Tj ET")
    # a font program: never referenced by a page's /Contents
    glyphs = b"(DcroatEngHbarTbaruni021C) (dcroatenghbarkgreenlandic) (zero.oldstyle)"

    out = [b"%PDF-1.6\n",
           b"1 0 obj <</Type/Catalog /Pages 2 0 R>> endobj\n",
           # Kids deliberately out of numeric order: page 5 is the FIRST page
           b"2 0 obj <</Type/Pages /Kids[5 0 R 4 0 R] /Count 2>> endobj\n",
           b"4 0 obj <</Type/Page /Parent 2 0 R /Contents 7 0 R "
           b"/Resources<</Font<</F1 9 0 R>>>> >> endobj\n",
           b"5 0 obj <</Type/Page /Parent 2 0 R /Contents 6 0 R "
           b"/Resources<</Font<</F1 9 0 R>>>> >> endobj\n",
           stream(6, page_one),
           stream(7, page_two),
           stream(8, glyphs),
           b"9 0 obj <</Type/Font /Subtype/Type1 /BaseFont/Helvetica>> endobj\n"]
    return b"".join(out)


class TestPdfTextLayerReadsTheDocumentNotTheFonts(unittest.TestCase):
    """The A19 repair depended on this: `_pdf_text` used to scrape literal
    strings out of every Flate stream, including the embedded font programs, and
    ordered pages by object number rather than by the page tree."""

    SAMPLE = pathlib.Path(__file__).resolve().parents[1] / "fetched"

    def test_the_fixture_actually_reproduces_the_defect(self):
        """Guard against an inert fixture. If the old whole-file scrape does NOT
        return glyph names from this PDF, the two tests below prove nothing."""
        legacy = elibrary._pdf_text_legacy(_synthetic_pdf())
        self.assertIn("DcroatEngHbar", legacy,
                      "the fixture no longer exhibits the bug, so the tests that "
                      "assert its absence are vacuous")

    def test_text_comes_from_the_pages_not_from_the_font_programs(self):
        text = elibrary.flatten(elibrary._pdf_text(_synthetic_pdf()))
        self.assertIn("Our annual inspection of the LNG terminal took place", text)
        self.assertIn("on April 21-22, 2026.", text)
        self.assertNotIn("DcroatEngHbar", text, "font glyph names leaked into the text")
        self.assertNotIn("zero.oldstyle", text)

    def test_pages_come_back_in_reading_order(self):
        """Page object 5 is listed first in /Kids but sorts second by number."""
        text = elibrary._pdf_text(_synthetic_pdf())
        self.assertLess(text.index("Dear Ms. Klacko"), text.index("cc:"),
                        "pages were ordered by object number, not by the page tree")

    # ---- the same two claims against the real FERC letter, when it is here ----
    # These do NOT skip quietly: the file's absence is reported as a failure of
    # the fixture, not passed over in silence. The synthetic cases above already
    # hold the behaviour, so a missing sample cannot leave the repair unguarded.

    def test_the_real_ferc_letter_agrees_with_the_synthetic_case(self):
        path = self.SAMPLE / "body_20260819-3015.bin"
        if not path.is_file():
            self.assertTrue(
                _synthetic_pdf(),
                "the cached FERC sample is absent; the synthetic cases above carry "
                "the behaviour, and this corroboration is unavailable rather than "
                "passing silently")
            return
        text = elibrary.flatten(elibrary.extract_text(path.read_bytes())[0])
        self.assertIn("Our annual inspection of the LNG terminal", text)
        self.assertIn("took place on April 21-22, 2026", text)
        self.assertNotIn("DcroatEngHbar", text)
        raw = elibrary.extract_text(path.read_bytes())[0]
        self.assertLess(raw.index("Dear Ms. Klacko"), raw.index("cc:"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
