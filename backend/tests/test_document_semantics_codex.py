"""Focused regressions for the independent re-audit's document semantics.

The quoted passages below are retained official FERC text identified by the
re-audit.  Tests are in-process only: they perform no network access, database
writes, cache reads, or subprocess calls.
"""

from __future__ import annotations

import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters import elibrary_docs as docs  # noqa: E402
from adapters import lng  # noqa: E402
from ferclib import elibrary  # noqa: E402
from ferclib.http import FetchError  # noqa: E402
from ferclib.status import Availability, Validation  # noqa: E402


FERC_LETTER = [("FERC Correspondence With Applicant", "General Correspondence")]
FERC_ORDER = [("Order/Opinion", "Delegated Order")]


def filing(description: str, class_pairs, **extra) -> dict:
    """A labelled in-memory filing in the shape the two adapters consume."""
    row = {
        "accession": extra.pop("accession", "AUDIT_SYNTHETIC_FILING"),
        "description": description,
        "class_pairs": list(class_pairs),
        "class_types": ["::".join(p) for p in class_pairs],
        "filed_date": extra.pop("filed_date", "2026-09-01"),
        "issued_date": "",
        "posted_date": "",
        "avail_code": "P",
        "dockets": extra.pop("dockets", ["CP06-12-000"]),
        "docket_bases": extra.pop("docket_bases", ["CP06-12"]),
        "transmittals": [],
        "affiliations": [],
        "libraries": ["GAS"],
        "category": "",
        "document_id": "AUDIT_IN_MEMORY_DOCUMENT",
        "availability_twins": [],
        "extraction_method": "captured_official_text",
    }
    row.update(extra)
    return row


class TestGulfLNGOperativeRecommendation(unittest.TestCase):
    """R06/A19: a filing instruction is not the inspection finding."""

    CFG = {
        "facility": "Gulf LNG / Clean Energy Project (Jackson County, MS)",
        "assets": ["kmi-gulf-lng-terminal"],
        "sweep_dockets": ["CP06-12"],
        "all_dockets": ["CP06-12", "CP06-13", "CP06-14"],
        "process": {"regas", "storage"},
        "fetch": [],
        "obligation_order": "",
        "note": "captured-evidence test",
    }
    DESCRIPTION = (
        "Letter to Gulf LNG Energy, LLC discussing the April 26, 2023 Biennial "
        "Inspection of the Gulf LNG facility under CP06-12."
    )
    ADMINISTRATIVE = (
        "We request that any anticipated extension requests be filed at least "
        "30 days prior to the recommendation due date."
    )
    RECOMMENDATION = (
        "By March 29, 2024, provide a plan and schedule to install a free-field "
        "seismic instrument consistent with seismic instrumentation criteria set "
        "forth in U.S. Nuclear Regulatory Commission (NRC) Regulatory Guide (RG) "
        "1.12, Rev. 3 October 2017."
    )
    BODY = (
        "Our biennial inspection of the Gulf LNG facility near Pascagoula, "
        "Mississippi took place on April 26, 2023. During the course of the site "
        "visit and technical review, we discussed items that are either in the "
        "process of being addressed or that need additional consideration and/or "
        "action by Gulf LNG. A list of recommendations resulting from the "
        "inspection is enclosed. "
        + ADMINISTRATIVE
        + " Inspection Recommendations: 1. "
        + RECOMMENDATION
        + " Note that U.S. NRC RG 1.12 is referenced in NBSIR 84-2833. The plan "
        "should address, at a minimum, the items listed below."
    )

    def _run(self, body: str):
        facts = []
        f = filing(self.DESCRIPTION, FERC_LETTER, accession="20230824-3057",
                   filed_date="2023-08-24", text=body)
        observations, populations = lng._inspection("NO-FERC-CID:Gulf LNG Energy, LLC",
                                                    self.CFG, [f], facts)
        return observations, populations, facts

    def test_numbered_recommendation_outranks_extension_procedure(self):
        observations, _populations, facts = self._run(self.BODY)
        finding = next(o for o in observations if "SUBSTANTIVE FINDINGS" in o["scope"])

        self.assertEqual(finding["availability"], Availability.PRESENT)
        self.assertEqual(finding["validation"], Validation.PASS)
        self.assertEqual(finding["value_text"], self.RECOMMENDATION)
        self.assertNotIn("extension requests", finding["value_text"])
        self.assertIn("Gulf LNG / Clean Energy Project", finding["scope"])
        self.assertEqual(finding["unit"], "categorical")
        self.assertIn("March 29, 2024", finding["value_text"])
        self.assertIn("free-field seismic instrument", finding["value_text"])
        self.assertIn("consistent with seismic instrumentation criteria",
                      finding["value_text"])

        fact = next(f for f in facts
                    if f["assertion_type"] == "lng_inspection_findings")
        self.assertEqual(fact["filing_id"], "20230824-3057")
        self.assertEqual(fact["value_text"], self.RECOMMENDATION)
        self.assertIn(self.RECOMMENDATION, fact["verbatim_span"])
        self.assertTrue(elibrary.span_supports(fact["verbatim_span"],
                                               fact["value_text"]))

    def test_administrative_extension_sentence_alone_is_not_a_finding(self):
        observations, populations, facts = self._run(
            "A list of recommendations resulting from the inspection is enclosed. "
            + self.ADMINISTRATIVE)
        finding = next(o for o in observations if "SUBSTANTIVE FINDINGS" in o["scope"])
        self.assertNotEqual(finding["availability"], Availability.PRESENT)
        self.assertIsNone(finding["value_text"])
        self.assertFalse(any(f["assertion_type"] == "lng_inspection_findings"
                             for f in facts))
        finding_population = next(
            p for p in populations if "'findings'" in p["note"])
        self.assertEqual(finding_population["row_count"], 1,
                         "the read body must remain in the examined population")
        self.assertIn("20230824-3057", finding_population["members_sample"])
        self.assertIn("none of them states", finding["missing_reason"])

    def test_narrative_no_recommendations_finding_remains_a_valid_control(self):
        body = (
            "Our annual inspection of the LNG terminal took place on May 5, 2026. "
            "Based on this technical review, we have no recommendations at this time."
        )
        observations, _populations, facts = self._run(body)
        finding = next(o for o in observations if "SUBSTANTIVE FINDINGS" in o["scope"])
        self.assertEqual(finding["availability"], Availability.PRESENT)
        self.assertEqual(
            finding["value_text"],
            "Based on this technical review, we have no recommendations at this time.")
        fact = next(f for f in facts
                    if f["assertion_type"] == "lng_inspection_findings")
        self.assertTrue(elibrary.span_supports(fact["verbatim_span"], fact["value_text"]))


class TestRefundRolesNeedTheirOwnOperativeText(unittest.TestCase):
    """A10: dates, subject-to-refund status and closure are separate facts."""

    TARGA_ORDER = (
        "ORDER ACCEPTING AND SUSPENDING TARIFF RECORD, SUBJECT TO REFUND. "
        "The Commission orders: Targa's Tariff No. 1.0.0 is accepted and "
        "suspended, to become effective December 1, 2025, subject to refund, as "
        "discussed in the body of this order."
    )
    TRANSCO_MOTION = (
        "On February 28, 2025, Transcontinental Gas Pipe Line Company, LLC filed "
        "revised tariff records to place into effect the rates suspended in the "
        "September 30, 2024 Suspension order in this proceeding. Pursuant to "
        "authority delegated to the Director, the tariff records listed in "
        "Appendix A are accepted, effective March 1, 2025, as requested."
    )
    TRANSCO_2019_MOTION = (
        "On February 28, 2019, Transcontinental Gas Pipe Line Company, LLC "
        "(Transco) filed revised tariff records in compliance with the "
        "Commission's order issued September 28, 2018. The September 28 Order "
        "accepted and suspended certain tariff records of Transco's general NGA "
        "Section 4 rate increase for five months, to be effective no earlier than "
        "March 1, 2019. The tariff records identified in Appendix A are accepted "
        "effective March 1, 2019."
    )
    TRANSCO_REFUND_ACCEPTANCE = (
        "Issued: April 28, 2026. On April 9, 2026, Transco filed its report of "
        "refunds detailing the refunds and surcharges made in compliance with "
        "Article IV. The refund report as supplemented is accepted for "
        "informational purposes."
    )

    @staticmethod
    def _classified(description: str, accession: str, filed_date: str, text: str,
                    docket: str) -> dict:
        f = filing(description, FERC_ORDER, accession=accession,
                   filed_date=filed_date, text=text,
                   dockets=[docket + "-000"], docket_bases=[docket])
        f["classification"] = docs.classify(f)
        return f

    def test_supported_targa_start_is_still_published(self):
        order = self._classified(
            "Order Accepting and Suspending Tariff Record, Subject to Refund re "
            "Targa Badlands LLC under IS26-24.",
            "20251126-3063", "2025-11-26", self.TARGA_ORDER, "IS26-24")
        facts = []
        result = docs._refund_window("C012931", {"IS26-24": [order]}, facts)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["value_text"], "2025-12-01 .. open")
        start = next(f for f in facts
                     if f["assertion_type"] ==
                     "refund_window_rates_effective_subject_to_refund")
        self.assertIn("subject to refund", start["verbatim_span"].lower())
        self.assertTrue(elibrary.span_supports(start["verbatim_span"], "2025-12-01"))

    def test_both_transco_motion_dates_without_refund_language_are_gated(self):
        cases = (
            ("RP18-1126", "20180928-3036", "2018-09-28", "20190318-3073",
             "2019-03-18", self.TRANSCO_2019_MOTION),
            ("RP24-1035", "20240930-3067", "2024-09-30", "20250320-3103",
             "2025-03-20", self.TRANSCO_MOTION),
        )
        for docket, suspension_acc, suspension_date, motion_acc, motion_date, text in cases:
            with self.subTest(docket=docket):
                suspension = self._classified(
                    "Order accepting and suspending tariff records, subject to refund, "
                    f"under {docket}.", suspension_acc, suspension_date, "", docket)
                motion = self._classified(
                    "Letter order accepting revised tariff records to place into effect "
                    f"the rates suspended in the prior order under {docket}.",
                    motion_acc, motion_date, text, docket)
                facts = []
                result = docs._refund_window(
                    "C000654", {docket: [suspension, motion]}, facts)
                self.assertEqual(len(result), 1)
                self.assertIsNone(result[0]["value_text"])
                self.assertNotEqual(result[0]["availability"], Availability.PRESENT)
                self.assertIn("subject to refund", result[0]["missing_reason"])
                self.assertFalse(any(
                    f["assertion_type"] ==
                    "refund_window_rates_effective_subject_to_refund" for f in facts))

    def test_report_accepted_for_information_does_not_close_obligation(self):
        closure = self._classified(
            "Letter order accepting Transco's report of refunds under RP24-1035.",
            "20260428-3033", "2026-04-28", self.TRANSCO_REFUND_ACCEPTANCE,
            "RP24-1035")
        self.assertIsNotNone(docs.RX_REFUND_ACCEPTED.search(closure["text"]),
                             "captured control must actually say the report is accepted")
        self.assertIsNone(docs.RX_REFUND_OBLIGATION_CLOSED.search(closure["text"]),
                          "acceptance for information was promoted to legal closure")
        facts = []
        bound = docs._boundary(
            [closure],
            [(docs.ROLE_REFUND_CLOSED, "refunds_accepted", [docs.RX_ISSUED_DATE],
              docs.RX_REFUND_OBLIGATION_CLOSED)],
            facts, "C000654", "RP24-1035", docs.BY_ID["refund_exposure_window"])
        self.assertIsNone(bound)
        self.assertEqual(facts, [])


class TestELibrarySearchStatusIsHonest(unittest.TestCase):
    """A failed retrieval must not be overwritten with a done checkpoint."""

    class Staging:
        def __init__(self):
            self.checkpoints = []
            self.blockers = []
            self.resolutions = []

        def checkpoint(self, *args, **kwargs):
            self.checkpoints.append((args, kwargs))

        def open_blocker(self, *args, **kwargs):
            self.blockers.append((args, kwargs))

        def resolve_blockers(self, *args, **kwargs):
            self.resolutions.append((args, kwargs))

    class Context:
        def __init__(self):
            self.staging = TestELibrarySearchStatusIsHonest.Staging()
            self.logs = []

        def log(self, *args, **kwargs):
            self.logs.append((args, kwargs))

    class Search:
        def __init__(self, result=None, error=None):
            self.result = result
            self.error = error

        def search(self, **_kwargs):
            if self.error:
                raise self.error
            return self.result

    def test_fetch_error_remains_failed(self):
        ctx = self.Context()
        error = FetchError("https://example.invalid/redacted", "captured failure",
                           status=503, attempts=3)
        result = docs._search(ctx, self.Search(error=error), "C000654", "docket:RP24")

        self.assertEqual(result, [])
        statuses = [args[3] for args, _kwargs in ctx.staging.checkpoints]
        self.assertEqual(statuses, ["failed"])
        self.assertNotIn("done", statuses,
                         "the finally block overwrote a real retrieval failure")
        self.assertEqual(len(ctx.staging.blockers), 1)
        self.assertEqual(ctx.staging.resolutions, [])
        self.assertEqual(len(ctx.logs), 1)

    def test_success_is_marked_done_once(self):
        ctx = self.Context()
        expected = [{"accession": "AUDIT_IN_MEMORY_SUCCESS"}]
        result = docs._search(ctx, self.Search(result=expected), "C000654", "census")

        self.assertIs(result, expected)
        statuses = [args[3] for args, _kwargs in ctx.staging.checkpoints]
        self.assertEqual(statuses, ["done"])
        self.assertEqual(ctx.staging.blockers, [])
        self.assertEqual(ctx.staging.resolutions,
                         [((docs.ADAPTER, "C000654:census"), {})])
        self.assertEqual(ctx.logs, [])


if __name__ == "__main__":
    unittest.main()
