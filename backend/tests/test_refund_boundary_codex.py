"""Focused regressions for refund-boundary and eLibrary filer ownership.

The fixtures are synthetic, labelled and entirely in memory.  They exercise the
production selectors without network, cache or database access.
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters import elibrary_docs as docs  # noqa: E402
from ferclib.status import Availability  # noqa: E402


def order(stage: str, accession: str, filed_date: str, text: str, *,
          is_issuance: bool = True) -> dict:
    """A minimal labelled order in the shape the production selector consumes."""
    return {
        "accession": accession,
        "filed_date": filed_date,
        "document_id": "SYNTHETIC-" + accession,
        "description": "SYNTHETIC FERC ORDER FIXTURE",
        "text": text,
        "extraction_method": "synthetic_in_memory_order_text",
        "classification": {
            "stage": stage,
            "is_issuance": is_issuance,
            "advancing": True,
            "label": stage,
            "evidence_class": "synthetic_test_evidence",
            "why": "explicit synthetic classification",
        },
    }


SUSPENSION_WITHOUT_DATE = (
    "The Commission orders: the tariff records are accepted and suspended for "
    "five months, to be effective upon motion, subject to refund and the outcome "
    "of the hearing established herein."
)
MOTION_ORDER = (
    "The company moved to place into effect the rates suspended by the prior "
    "Commission order. The Commission orders: the tariff records listed in "
    "Appendix A are accepted, effective March 1, 2025, as requested."
)


class TestRefundBoundaryOrderChain(unittest.TestCase):
    """A suspension supplies legal status; a later order can supply the date."""

    def _chain(self, reverse: bool = False):
        suspension = order("accepted_and_suspended", "20240930-3067",
                           "2024-09-30", SUSPENSION_WITHOUT_DATE)
        motion_order = order("rates_effective_subject_to_refund", "20250320-3103",
                             "2025-03-20", MOTION_ORDER)
        population = [suspension, motion_order]
        if reverse:
            population.reverse()
        facts = []
        observations = docs._refund_window(
            "C000654", {"RP24-1035": population}, facts)
        return observations, facts

    def test_suspension_plus_later_motion_order_establishes_start(self):
        observations, facts = self._chain()
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["value_text"], "2025-03-01 .. open")
        self.assertEqual(observations[0]["accession_number"], "20250320-3103")
        self.assertIn("20240930-3067", observations[0]["qa_flags"])
        self.assertIn("20250320-3103", observations[0]["qa_flags"])

        date_fact = next(f for f in facts if f["assertion_type"] ==
                         "refund_window_rates_effective_subject_to_refund")
        status_fact = next(f for f in facts if f["assertion_type"] ==
                           "refund_window_suspension_subject_to_refund")
        self.assertEqual(date_fact["filing_id"], "20250320-3103")
        self.assertEqual(date_fact["value_text"], "2025-03-01")
        self.assertEqual(status_fact["filing_id"], "20240930-3067")
        self.assertEqual(status_fact["value_text"], "subject to refund")

    def test_replay_order_does_not_change_selected_boundary(self):
        forward_obs, forward_facts = self._chain(False)
        reverse_obs, reverse_facts = self._chain(True)
        self.assertEqual(forward_obs[0]["value_text"], reverse_obs[0]["value_text"])
        self.assertEqual(forward_obs[0]["accession_number"],
                         reverse_obs[0]["accession_number"])
        fact_key = lambda f: (f["assertion_type"], f["filing_id"], f["value_text"])
        self.assertEqual(sorted(map(fact_key, forward_facts)),
                         sorted(map(fact_key, reverse_facts)))

    def test_all_eligible_orders_are_inspected_and_body_date_governs(self):
        unread_earliest = order("rates_effective_subject_to_refund", "20250101-3000",
                                "2025-01-01", "This order contains no effective date.")
        later_with_date = order(
            "rates_effective_subject_to_refund", "20250320-3103", "2025-03-20",
            MOTION_ORDER)
        facts = []
        boundary = docs._boundary(
            [unread_earliest, later_with_date],
            [(docs.ROLE_REFUND_EFFECTIVE, "rates_effective_subject_to_refund",
              [docs.RX_MOTION_EFFECTIVE], docs.RX_SUSPENDED_RATE_REFERENCE)],
            facts, "C000654", "RP24-1035",
            docs.BY_ID["refund_exposure_window"])
        self.assertIsNotNone(boundary)
        self.assertEqual(boundary["accession"], "20250320-3103")
        self.assertEqual(boundary["iso"], "2025-03-01")
        self.assertEqual(len(facts), 1)

    def test_supported_operative_date_not_earliest_filed_date_breaks_tie(self):
        earlier_filed_later_boundary = order(
            "rates_effective_subject_to_refund", "20250101-3000", "2025-01-01",
            "The rates suspended by the prior order remain at issue. The tariff "
            "records are accepted, effective April 1, 2025.")
        later_filed_earlier_boundary = order(
            "rates_effective_subject_to_refund", "20250320-3103", "2025-03-20",
            MOTION_ORDER)
        boundary = docs._boundary(
            [earlier_filed_later_boundary, later_filed_earlier_boundary],
            [(docs.ROLE_REFUND_EFFECTIVE, "rates_effective_subject_to_refund",
              [docs.RX_MOTION_EFFECTIVE], docs.RX_SUSPENDED_RATE_REFERENCE)],
            [], "C000654", "RP24-1035",
            docs.BY_ID["refund_exposure_window"])
        self.assertEqual(boundary["iso"], "2025-03-01")
        self.assertEqual(boundary["accession"], "20250320-3103")
        self.assertEqual(boundary["candidate_count"], 2)

    def test_motion_date_without_sourced_suspension_is_not_promoted(self):
        no_body = order("accepted_and_suspended", "20240930-3067",
                        "2024-09-30", "")
        motion_order = order("rates_effective_subject_to_refund", "20250320-3103",
                             "2025-03-20", MOTION_ORDER)
        facts = []
        observations = docs._refund_window(
            "C000654", {"RP24-1035": [no_body, motion_order]}, facts)
        self.assertEqual(len(observations), 1)
        self.assertIsNone(observations[0]["value_text"])
        self.assertNotEqual(observations[0]["availability"], Availability.PRESENT)
        self.assertFalse(any(f["assertion_type"].startswith("refund_window_")
                             for f in facts))

    def test_nonissuance_cannot_establish_the_later_boundary(self):
        suspension = order("accepted_and_suspended", "20240930-3067",
                           "2024-09-30", SUSPENSION_WITHOUT_DATE)
        filer_motion = order("rates_effective_subject_to_refund", "20250301-5000",
                             "2025-03-01", MOTION_ORDER, is_issuance=False)
        observations = docs._refund_window(
            "C000654", {"RP24-1035": [suspension, filer_motion]}, [])
        self.assertIsNone(observations[0]["value_text"])

    def test_report_acceptance_is_not_closure_but_explicit_order_is(self):
        suspension = order(
            "accepted_and_suspended", "20251126-3063", "2025-11-26",
            "The Commission orders: the tariff is accepted and suspended, to "
            "become effective December 1, 2025, subject to refund.")
        informational = order(
            "refunds_accepted", "20260428-3033", "2026-04-28",
            "Issued: April 28, 2026. The refund report is accepted for "
            "informational purposes.")
        open_obs = docs._refund_window(
            "C000654", {"RP24-1035": [suspension, informational]}, [])
        self.assertEqual(open_obs[0]["value_text"], "2025-12-01 .. open")

        closure = dict(informational)
        closure["text"] = (
            "Issued: April 28, 2026. The Commission finds that the refund "
            "obligations are satisfied and closes this proceeding.")
        closed_obs = docs._refund_window(
            "C000654", {"RP24-1035": [suspension, closure]}, [])
        self.assertEqual(closed_obs[0]["value_text"],
                         "2025-12-01 .. 2026-04-28")


class TestELibraryLegalNameBoundary(unittest.TestCase):

    def test_overthrust_requires_its_distinctive_token(self):
        legal = "MountainWest Overthrust Pipeline, LLC"
        self.assertFalse(docs._mentions(
            legal, "MountainWest Pipeline, LLC files its annual replacement report."))
        self.assertTrue(docs._mentions(
            legal, "MountainWest Overthrust Pipeline, LLC files its annual report."))
        self.assertTrue(docs._mentions(
            legal, "Overthrust Pipeline (MountainWest) submits the report."),
            "the valid control must not depend on token adjacency")

    def test_shorter_mountainwest_name_does_not_claim_overthrust(self):
        legal = "MountainWest Pipeline, LLC"
        self.assertFalse(docs._mentions(
            legal, "MountainWest Overthrust Pipeline, LLC files its annual report."))
        self.assertTrue(docs._mentions(
            legal, "MountainWest Pipeline, LLC files its annual report."))

    def test_step_three_filters_foreign_report_before_persistence(self):
        foreign = {
            "accession": "FOREIGN-MWP", "filed_date": "2026-04-01",
            "description": "MountainWest Pipeline, LLC replacement facilities report",
            "class_pairs": [("Report/Form", "2.55 Annual Construction Report")],
        }
        own = {
            "accession": "OWN-MWO", "filed_date": "2026-04-02",
            "description": ("MountainWest Overthrust Pipeline, LLC replacement "
                            "facilities report"),
            "class_pairs": [("Report/Form", "2.55 Annual Construction Report")],
        }

        def search(_ctx, _elib, _key, tag, **_kwargs):
            return [foreign, own] if tag == "interruption" else []

        class Baseline:
            def __init__(self, *_args, **_kwargs):
                pass

            def establish(self):
                pass

        class Context:
            client = object()
            staging = object()

            @staticmethod
            def log(*_args, **_kwargs):
                pass

        persisted = []
        entity = {
            "entity_key": "C001087", "template": "interstate_gas",
            "legal_name": "MountainWest Overthrust Pipeline, LLC",
        }
        with (mock.patch.object(docs, "_seed_dockets", return_value=([], [])),
              mock.patch.object(docs, "_search", side_effect=search),
              mock.patch.object(docs.elibrary, "Elibrary", return_value=object()),
              mock.patch.object(docs.elibrary, "dedupe_availability",
                                side_effect=lambda rows: (list(rows), [])),
              mock.patch.object(docs.elibrary, "NewsBaseline", Baseline),
              mock.patch.object(docs, "_documents_to_read", return_value=[]),
              mock.patch.object(docs, "_persist",
                                side_effect=lambda _ctx, _entity, filing, **_kw:
                                persisted.append(filing["accession"]))):
            filings = docs.retrieve(Context(), entity, year_from=2024, year_to=2026)

        self.assertEqual([f["accession"] for f in filings], ["OWN-MWO"])
        self.assertEqual(persisted, ["OWN-MWO"])

    def test_point_in_time_replay_keeps_valid_prior_filing_and_rejects_future_hit(self):
        prior = {
            "accession": "20260825-5125", "filed_date": "2026-08-25",
            "issued_date": "2026-08-25", "posted_date": "2026-08-25",
            "description": ("Transcontinental Gas Pipe Line Company, LLC submits "
                            "tariff filing under RP26-1091"),
            "class_pairs": [("Application/Petition/Request", "Tariff Filing")],
            "docket_bases": [],
        }
        future = {
            "accession": "20260909-5053", "filed_date": "2026-09-09",
            "issued_date": "2026-09-09", "posted_date": "2026-09-09",
            "description": ("Transcontinental Gas Pipe Line Company, LLC filing "
                            "under RP26-1091"),
            "class_pairs": [("Order/Opinion", "Delegated Order")],
            "docket_bases": [],
        }

        def search(_ctx, _elib, _key, tag, **_kwargs):
            return [prior, future] if tag.startswith("census:") else []

        class Baseline:
            def __init__(self, *_args, **_kwargs):
                pass

            def establish(self):
                pass

        class Context:
            client = object()
            staging = object()
            as_of_iso = "2026-09-07"

            @staticmethod
            def log(*_args, **_kwargs):
                pass

        persisted = []
        entity = {
            "entity_key": "C000654", "template": "liquids",
            "legal_name": "Transcontinental Gas Pipe Line Company, LLC",
        }
        with (mock.patch.object(docs, "_seed_dockets", return_value=([], [])),
              mock.patch.object(docs, "_search", side_effect=search),
              mock.patch.object(docs.elibrary, "Elibrary", return_value=object()),
              mock.patch.object(docs.elibrary, "dedupe_availability",
                                side_effect=lambda rows: (list(rows), [])),
              mock.patch.object(docs.elibrary, "NewsBaseline", Baseline),
              mock.patch.object(docs, "_documents_to_read", return_value=[]),
              mock.patch.object(docs, "_persist",
                                side_effect=lambda _ctx, _entity, filing, **_kw:
                                persisted.append(filing["accession"]))):
            filings = docs.retrieve(Context(), entity, year_from=2024, year_to=2026)

        self.assertEqual([f["accession"] for f in filings], ["20260825-5125"])
        self.assertEqual(persisted, ["20260825-5125"])
        self.assertFalse(docs._after_as_of(prior, "2026-09-07"))
        self.assertTrue(docs._after_as_of(future, "2026-09-07"))


if __name__ == "__main__":
    unittest.main()
