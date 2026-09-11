#!/usr/bin/env python3
"""Capacity filer ownership and population-identity regressions."""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from adapters import capacity  # noqa: E402


class CapacityFilerDescriptionBoundary(unittest.TestCase):
    def test_mountainwest_and_overthrust_are_not_interchangeable(self):
        mwp = "MountainWest Pipeline, LLC"
        mwo = "MountainWest Overthrust Pipeline, LLC"
        own_mwp = ("Annual Peak Day Capacity Report of MountainWest Pipeline, LLC "
                   "for 2025.")
        own_mwo = ("Annual Peak Day Capacity Report of MountainWest Overthrust "
                   "Pipeline, LLC for 2025.")
        self.assertTrue(capacity._description_names_filer(mwp, own_mwp))
        self.assertTrue(capacity._description_names_filer(mwo, own_mwo))
        self.assertFalse(capacity._description_names_filer(mwp, own_mwo))
        self.assertFalse(capacity._description_names_filer(mwo, own_mwp))

    def test_valid_punctuation_and_revised_prefix_do_not_break_boundary(self):
        self.assertTrue(capacity._description_names_filer(
            "Tennessee Gas Pipeline Company, L.L.C.",
            "Revised Annual Peak Day Capacity Report of Tennessee Gas Pipeline "
            "Company, L.L.C. for 2025."))
        self.assertFalse(capacity._description_names_filer(
            "Tennessee Gas Pipeline Company, L.L.C.",
            "Annual Peak Day Capacity Report of Another Tennessee Pipeline for 2025."))


class CapacityPopulationIdentity(unittest.TestCase):
    @staticmethod
    def _filing(filing_id="F-1"):
        return {
            "filing_id": filing_id, "reporting_year": 2025,
            "_text_layer": "yes", "_doc": [{"page": 1}],
            "_figures": [{"kind": "table", "page": 1, "row_index": 3,
                           "is_total": False, "value_text": "100"}],
        }

    def test_population_id_binds_owner_observation_and_occurrence(self):
        filing = self._filing()
        one = capacity._total_population("C001087", filing, "obs-one")
        repeat = capacity._total_population("C001087", filing, "obs-one")
        other_entity = capacity._total_population("C001088", filing, "obs-two")
        other_observation = capacity._total_population("C001087", filing, "obs-three")
        other_filing = capacity._total_population(
            "C001087", self._filing("F-2"), "obs-one")
        self.assertEqual(one["population_id"], repeat["population_id"])
        self.assertEqual(len({one["population_id"], other_entity["population_id"],
                              other_observation["population_id"],
                              other_filing["population_id"]}), 4)
        self.assertEqual(one["observation_id"], "obs-one")
        self.assertEqual(one["filing_ids"], '["F-1"]')


def _report_pages(*texts: str) -> list[dict]:
    return [{
        "page": 1,
        "rows": [{"text": text} for text in texts],
    }]


def _candidate(description_year: int | None, filename: str, *,
               accession: str = "SYNTHETIC") -> dict:
    return {
        "accession": accession,
        "report_year": description_year,
        "pdfs": [{"fileName": filename}],
    }


class CapacityReportYearEvidence(unittest.TestCase):
    FGT_2026_HASH = (
        "05bd59b0837447be42bba68b4f764e8192063c76c7539072d88c2ff90f1ad00a"
    )
    GULFSTREAM_2026_HASH = (
        "b24a4437cf4a94dcd9ffbeee05a6f54555bca0b60a9afe54b18e69fb9ccb31d2"
    )

    def test_fgt_2026_correction_is_bound_to_accession_and_exact_pdf_hash(self):
        year, basis, disagreements = capacity._report_year_from_document(
            _report_pages("Peak Day Capacity Report – 2026"),
            _candidate(
                2025,
                "PeakDayCapacityRpt_Y2026_FGT.pdf",
                accession="20260113-5139",
            ),
            content_hash=self.FGT_2026_HASH,
        )
        self.assertEqual(year, 2026)
        self.assertIn("accession 20260113-5139", basis)
        self.assertIn(self.FGT_2026_HASH, basis)
        self.assertEqual(disagreements, [
            "eLibrary description says 2025, but resolved report year is 2026",
        ])

    def test_reviewed_accession_with_different_bytes_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "SHA-256 does not match"):
            capacity._report_year_from_document(
                _report_pages("Peak Day Capacity Report – 2026"),
                _candidate(
                    2025,
                    "PeakDayCapacityRpt_Y2026_FGT.pdf",
                    accession="20260113-5139",
                ),
                content_hash="0" * 64,
            )

    def test_gulfstream_2026_correction_is_bound_to_exact_filed_bytes(self):
        year, basis, disagreements = capacity._report_year_from_document(
            _report_pages(
                "Gulfstream Natural Gas System Capacity",
                "MARCH 2026",
                "System capacity as of March 1, 2026.",
            ),
            _candidate(
                2025,
                "Gulfstream Peak Day Capacity Report 2026.pdf",
                accession="20260226-5099",
            ),
            content_hash=self.GULFSTREAM_2026_HASH,
        )
        self.assertEqual(year, 2026)
        self.assertIn("accession 20260226-5099", basis)
        self.assertIn(self.GULFSTREAM_2026_HASH, basis)
        self.assertEqual(disagreements, [
            "eLibrary description says 2025, but resolved report year is 2026",
        ])

    def test_correction_does_not_attach_to_a_different_accession(self):
        year, basis, disagreements = capacity._report_year_from_document(
            _report_pages("Peak Day Capacity Report – 2026"),
            _candidate(2025, "PeakDayCapacityRpt_Y2026_FGT.pdf"),
            content_hash=self.FGT_2026_HASH,
        )
        self.assertEqual(year, 2025)
        self.assertEqual(basis, "eLibrary description")
        self.assertEqual(disagreements, [
            "filed report heading says 2026, but resolved report year is 2025",
            "report-specific attachment filename says 2026, but resolved report year is 2025",
        ])

    def test_filed_2025_description_is_not_outvoted_by_attachment_name(self):
        year, basis, disagreements = capacity._report_year_from_document(
            _report_pages("Peak Day Capacity Report ± 2025"),
            _candidate(2025, "PeakDayCapacityRpt_Y2024_FGT.pdf"),
        )
        self.assertEqual(year, 2025)
        self.assertEqual(basis, "eLibrary description")
        self.assertEqual(disagreements, [
            "report-specific attachment filename says 2024, but resolved report year is 2025",
        ])

    def test_capacity_as_of_date_does_not_relabel_the_annual_cycle(self):
        year, basis, disagreements = capacity._report_year_from_document(
            _report_pages(
                "ANNUAL PEAK DAY CAPACITY REPORT – 2025",
                "pipeline system as of March 1, 2026.",
            ),
            _candidate(2025, "Gulfstream Peak Day Capacity Report.pdf"),
        )
        self.assertEqual(year, 2025)
        self.assertIn("eLibrary description", basis)
        self.assertEqual(disagreements, [])

    def test_matching_filename_and_as_of_date_do_not_outvote_description(self):
        year, basis, disagreements = capacity._report_year_from_document(
            _report_pages(
                "ANNUAL PEAK DAY CAPACITY REPORT",
                "pipeline system as of March 1, 2026.",
            ),
            _candidate(2025, "Gulfstream Peak Day Capacity Report 2026.pdf"),
        )
        self.assertEqual(year, 2025)
        self.assertEqual(basis, "eLibrary description")
        self.assertEqual(disagreements, [
            "report-specific attachment filename says 2026, but resolved report year is 2025",
        ])

    def test_exact_value_period_language_controls_generically(self):
        year, basis, disagreements = capacity._report_year_from_document(
            _report_pages(
                "2025 Annual Peak Day Capacity Report",
                "LNG storage facility for 2024 were 4 Bcf and 400 MMcf/d.",
            ),
            _candidate(2024, "Capacity 2024-Pine Needle.pdf"),
        )
        self.assertEqual(year, 2024)
        self.assertIn("reported capacity value", basis)
        self.assertEqual(disagreements, [
            "filed report heading says 2025, but resolved report year is 2024",
        ])

    def test_sabine_description_cycles_remain_2024_and_2025(self):
        years = [
            capacity._report_year_from_document(
                _report_pages("Annual Peak Day Capacity Report – 2025"),
                _candidate(2024, "Sabine Peak Day Capacity Report (2025).pdf"),
            )[0],
            capacity._report_year_from_document(
                _report_pages("Annual Peak Day Capacity Report – 2026"),
                _candidate(2025, "Sabine Peak Day Capacity Report (2026).pdf"),
            )[0],
        ]
        self.assertEqual(years, [2024, 2025])

    def test_northwest_description_cycles_remain_consecutive(self):
        years = [
            capacity._report_year_from_document(
                _report_pages("Annual Peak Day Capacity Report for 2025"),
                _candidate(2024, "Capacity 2024-NWP.pdf"),
            )[0],
            capacity._report_year_from_document(
                _report_pages("Annual Peak Day Capacity Report for 2026"),
                _candidate(2025, "Capacity 2025-NWP.pdf"),
            )[0],
        ]
        self.assertEqual(years, [2024, 2025])

    def test_conflicting_document_years_are_refused(self):
        with self.assertRaisesRegex(ValueError, "conflicting reporting years"):
            capacity._report_year_from_document(
                _report_pages(
                    "Annual Peak Day Capacity Report for 2025",
                    "Annual Peak Day Capacity Report for 2026",
                ),
                _candidate(2025, "capacity.pdf"),
            )

    def test_filing_date_is_never_used_as_an_implicit_report_year(self):
        with self.assertRaisesRegex(ValueError, "has no report year"):
            capacity._report_year_from_document(
                _report_pages("Filed February 20, 2026"),
                _candidate(None, "attachment.pdf"),
            )

    def test_filename_alone_never_establishes_a_cycle(self):
        with self.assertRaisesRegex(ValueError, "only an attachment-filename"):
            capacity._report_year_from_document(
                _report_pages("Annual capacity filing"),
                _candidate(None, "Capacity 2025.pdf"),
            )

    def test_image_only_year_requires_ocr_and_review_agreement(self):
        with self.assertRaisesRegex(
                capacity.ImageOnlySource, "disagrees between OCR"):
            capacity._reviewed_image_report_year(
                _report_pages(
                    "The system capacity for calendar year 2025 was 100 MDth/d."),
                _report_pages(
                    "The system capacity for calendar year 2024 was 100 MDth/d."),
                _candidate(2025, "capacity.pdf"),
                content_hash="a" * 64,
            )

    def test_image_only_matching_year_is_accepted(self):
        resolution = capacity._reviewed_image_report_year(
            _report_pages(
                "The system capacity for calendar year 2025 was 100 MDth/d."),
            _report_pages(
                "The system capacity for calendar year 2025 was 100 MDth/d."),
            _candidate(2024, "capacity.pdf"),
            content_hash="a" * 64,
        )
        self.assertEqual(resolution[0], 2025)


class GulfstreamInlineUnitTableRegression(unittest.TestCase):
    """Exact row shapes from accession 20260226-5099's filed PDF."""

    @staticmethod
    def _row(y: float, *cells: tuple[float, float, str]) -> dict:
        values = [{"x": x, "x_end": x_end, "text": text,
                   "binary_control_codes": []}
                  for x, x_end, text in cells]
        return {
            "y": y,
            "cells": values,
            "text": " ".join(cell["text"] for cell in values),
            "reordered": False,
            "binary_control_codes": [],
        }

    def test_inline_units_stay_in_table_and_definition_footnote_is_not_a_value(self):
        rows = [
            self._row(527.16, (79.38, 160.09, "RATE SCHEDULE"),
                      (424.62, 563.31, "ASSIGNMENT OF CAPACITY")),
            self._row(516.66, (552.06, 558.05, "1)")),
            self._row(514.14, (79.38, 96.26, "FTS"),
                      (485.28, 552.07, "1,388,000 Dth")),
            self._row(488.88,
                      (79.38, 303.44, "TOTAL ESTIMATED FIRM PEAK DAY OBLIGATION"),
                      (485.04, 551.84, "1,388,000 Dth")),
            self._row(458.28, (79.38, 182.42, "PEAK DAY CAPACITY")),
            self._row(455.70, (485.04, 551.83, "1,388,000 Dth")),
            self._row(
                259.14,
                (79.38, 91.41, "1)"),
                (97.38, 454.11,
                 "The term Dth or dekatherm is the quantity of heat energy "
                 "that is 1 MMBtu."),
            ),
        ]
        figures = capacity.read_figures(
            [{"page": 2, "height": 792.0, "rows": rows}], 2026)

        self.assertEqual(3, len(figures))
        self.assertEqual({"table"}, {figure["kind"] for figure in figures})
        self.assertEqual({"1,388,000"},
                         {figure["value_text"] for figure in figures})
        self.assertEqual({1388000.0},
                         {figure["value_num"] for figure in figures})
        self.assertEqual({"Dth"}, {figure["unit"] for figure in figures})
        self.assertFalse(any("The term" in figure["verbatim"]
                             for figure in figures))
        by_label = {figure["row_label"]: figure for figure in figures}
        self.assertFalse(by_label["FTS"]["is_total"])
        self.assertFalse(
            by_label["TOTAL ESTIMATED FIRM PEAK DAY OBLIGATION"]["is_total"])
        self.assertTrue(by_label["PEAK DAY CAPACITY"]["is_total"])
        scopes = {capacity._figure_scope(figure) for figure in figures}
        self.assertTrue(any("FTS" in scope for scope in scopes))
        self.assertTrue(any("TOTAL ESTIMATED FIRM PEAK DAY OBLIGATION" in scope
                            for scope in scopes))
        self.assertTrue(any("PEAK DAY CAPACITY" in scope for scope in scopes))

        metric = next(
            item for item in capacity.BY_ADAPTER["capacity"]
            if item.id == "cap_reported_capacity"
        )
        observations = capacity._capacity_observations(
            None,
            {"entity_key": "C000087"},
            {
                "filing_id": "20260226-5099",
                "reporting_year": 2026,
                "filed_date": "2026-02-26",
                "_as_of": "2026-03-01",
                "_as_of_basis": "stated on page 2",
                "_figures": figures,
                "_shifted": [],
                "_text_layer": "yes",
                "_document_id": "synthetic-gulfstream-document",
                "content_hash": "synthetic-gulfstream-hash",
            },
            {metric.id: metric},
        )
        headline = next(item for item in observations
                        if item["scope"] == metric.scope)
        self.assertEqual("1,388,000", headline["value_text"])
        self.assertEqual(1388000.0, headline["value_num"])
        self.assertEqual("Dth", headline["unit"])
        self.assertIn(
            "the filer's own stated system total: "
            "PEAK DAY CAPACITY / PEAK DAY CAPACITY",
            headline["qa_flags"],
        )

    def test_numbered_quantity_row_is_not_mistaken_for_a_definition_footnote(self):
        row = self._row(
            500.0,
            (79.0, 91.0, "1)"),
            (97.0, 250.0, "Maximum Daily Quantity"),
            (485.0, 552.0, "100 Dth"),
        )
        self.assertFalse(capacity._is_unit_definition_footnote(row))

        figures = capacity.read_figures(
            [{"page": 2, "height": 792.0, "rows": [
                self._row(525.0, (79.0, 250.0, "SERVICE"),
                          (424.0, 563.0, "ASSIGNMENT OF CAPACITY")),
                row,
            ]}],
            2026,
        )
        self.assertTrue(any(
            figure["value_text"] == "100"
            and figure["value_num"] == 100.0
            and figure["unit"] == "Dth"
            for figure in figures
        ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
