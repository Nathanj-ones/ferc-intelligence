"""Regression controls for portable, evidence-preserving consumer text.

The raw official-FERC objects remain immutable in ``source_cache``.  These
checks exercise the production normaliser and writer, then verify the integrated
publication without weakening row-count or legitimate-zero requirements.
"""

from __future__ import annotations

import csv
import io
import json
import pathlib
import sqlite3
import tempfile
import unittest

import exporters
from adapters import capacity
from ferclib import elibrary


ROOT = pathlib.Path(__file__).resolve().parents[1]
FORBIDDEN = set(range(0x00, 0x09)) | {0x0B, 0x0C} | set(range(0x0E, 0x20))


class ExtractedTextControlTests(unittest.TestCase):
    def test_production_extractor_replaces_binary_controls_without_dropping_zero(self) -> None:
        raw = (b"Official FERC text: value 0, capacity 380000 MMBtu/day; "
               b"alpha\x00beta\x1agamma.\nSecond line remains readable.")
        text, method, layer = elibrary.extract_text(raw, "control_fixture.txt")
        self.assertEqual("plain_text_span", method)
        self.assertIn(layer, {"partial", "yes"})
        self.assertIn("value 0", text)
        self.assertIn("380000 MMBtu/day", text)
        self.assertIn("alpha beta gamma", text)
        self.assertIn("\nSecond line", text)
        self.assertFalse(any(ord(ch) in FORBIDDEN for ch in text), repr(text))

    @staticmethod
    def _glyph(seq: int, y: float, x: float, x_end: float, text: str) -> dict:
        return {"seq": seq, "y": y, "x": x, "x_end": x_end,
                "text": text, "size": 10.0}

    def test_capacity_coordinate_reader_normalises_but_gates_affected_scope(self) -> None:
        glyph_rows = [
            self._glyph(0, 620, 100, 175, "Service"),
            self._glyph(1, 620, 300, 395, "Capacity (Dth/d)"),
            self._glyph(2, 600, 100, 220, "Mainline\x12Facilities"),
            self._glyph(3, 600, 310, 350, "380,000"),
        ]
        rows = capacity.rows_of(glyph_rows)

        # Raw/source-derived glyph evidence is not rewritten.  Only the row text
        # that can be persisted and exported is made portable.
        self.assertEqual("Mainline\x12Facilities", glyph_rows[2]["text"])
        dirty_row = next(row for row in rows if "380,000" in row["text"])
        self.assertEqual(["U+0012"], dirty_row["binary_control_codes"])
        self.assertIn("Mainline Facilities", dirty_row["text"])
        self.assertFalse(any(ord(ch) in FORBIDDEN for ch in dirty_row["text"]))

        figures = capacity.read_figures(
            [{"page": 1, "height": 792.0, "rows": rows}], 2024)
        figure = next(item for item in figures if item["value_text"] == "380,000")
        self.assertIsNone(figure["value_num"])
        self.assertEqual(["U+0012"], figure["binary_control_codes"])
        self.assertTrue(any("binary glyph codes" in problem
                            for problem in figure["problems"]))
        confidence, validation, _ = capacity._confidence(figure)
        self.assertIn("source_text_undecodable", confidence)
        self.assertEqual("blocked_ambiguity", validation)

        # Paired valid control: rejecting every capacity figure cannot pass.
        clean_rows = capacity.rows_of([
            self._glyph(0, 620, 100, 175, "Service"),
            self._glyph(1, 620, 300, 395, "Capacity (Dth/d)"),
            self._glyph(2, 600, 100, 220, "Mainline Facilities"),
            self._glyph(3, 600, 310, 350, "0"),
        ])
        clean = capacity.read_figures(
            [{"page": 1, "height": 792.0, "rows": clean_rows}], 2024)
        zero = next(item for item in clean if item["value_text"] == "0")
        self.assertEqual(0.0, zero["value_num"])
        self.assertEqual([], zero["problems"])
        self.assertEqual(("verified_span", "pass", []), capacity._confidence(zero))

    def test_capacity_common_boundary_covers_direct_ocr_rows(self) -> None:
        pages = [{"page": 1, "height": 792.0, "shifted_fonts": [], "rows": [{
            "y": 600.0,
            "cells": [{"x": 90.0, "x_end": 200.0,
                       "text": "Storage capacity is 0 Dth/day\x00", "size": 11.0}],
            "reordered": False,
            "text": "Storage capacity is 0 Dth/day\x00",
            "ocr_raw_text": "Storage capacity is 0 Dth/day\x00",
        }]}]
        got = capacity._normalise_capacity_doc_rows(pages)
        row = got[0]["rows"][0]
        self.assertEqual("Storage capacity is 0 Dth/day", row["text"])
        self.assertEqual("Storage capacity is 0 Dth/day", row["cells"][0]["text"])
        self.assertEqual(["U+0000"], row["binary_control_codes"])
        self.assertEqual(["U+0000"], row["cells"][0]["binary_control_codes"])
        self.assertEqual("Storage capacity is 0 Dth/day\x00", row["ocr_raw_text"],
                         "diagnostic raw OCR evidence must remain exact")
        prose = capacity.read_figures(got, 2024)
        self.assertEqual(1, len(prose))
        self.assertEqual("0", prose[0]["value_text"])
        self.assertIsNone(prose[0]["value_num"],
                          "a C0-affected prose number must remain value-withheld")
        self.assertTrue(any("binary glyph codes" in problem
                            for problem in prose[0]["problems"]))

    def test_writer_rejects_an_unsanitised_bad_row_and_accepts_valid_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            bad = root / "bad.csv"
            with self.assertRaises(exporters.CSVControlCharacterViolation):
                exporters._write(bad, [{"id": "bad", "value": "12\x003"}])
            self.assertFalse(bad.exists(), "the writer partially published a rejected file")

            good = root / "good.csv"
            self.assertEqual(1, exporters._write(
                good, [{"id": "zero", "value": "0", "note": "line 1\nline 2"}]))
            rows = list(csv.DictReader(io.StringIO(good.read_text(encoding="utf-8"))))
            self.assertEqual([{"id": "zero", "value": "0",
                               "note": "line 1\nline 2"}], rows)


class IntegratedPublicationControlTests(unittest.TestCase):
    def test_complete_but_ambiguous_capacity_is_not_applicability_unknown(self) -> None:
        db_path = ROOT / "staging" / "operating_assets.sqlite"
        self.assertTrue(db_path.is_file(), db_path)
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            affected = ("20250225-5101", "20250227-5034", "20260202-5047")
            marks = ",".join("?" for _ in affected)
            rows = con.execute(
                f"SELECT accession_number,availability,validation,value_num "
                f"FROM observations WHERE accession_number IN ({marks}) "
                "AND metric_id='cap_reported_capacity' "
                "AND source_regime='Form 549B Capacity' "
                "ORDER BY accession_number,observation_id", affected).fetchall()
            self.assertEqual(34, len(rows))
            gated_null = [row for row in rows
                          if row[2] == "blocked_ambiguity" and row[3] is None]
            self.assertEqual(22, len(gated_null))
            self.assertEqual({"interpretation_blocked"},
                             {row[1] for row in gated_null})
            self.assertFalse(any(row[1] == "unverified_availability" for row in rows))

            # Exact Arlington dirty-source population: one report head plus 18
            # occurrence-specific facts.  Nothing is discarded to improve the
            # result, and every value remains withheld behind the semantic gate.
            arlington = [row for row in rows if row[0] == "20250225-5101"]
            self.assertEqual(19, len(arlington))
            self.assertTrue(all(row[1] == "interpretation_blocked"
                                and row[2] == "blocked_ambiguity"
                                and row[3] is None for row in arlington))
            self.assertEqual(18, con.execute(
                "SELECT COUNT(*) FROM document_facts WHERE filing_id='20250225-5101'"
            ).fetchone()[0])

            measured = set(con.execute(
                "SELECT o.accession_number,m.outcome FROM coverage_measured m "
                "JOIN observations o ON o.observation_id=m.observation_id "
                "WHERE o.accession_number IN ('20250225-5101','20260202-5047') "
                "AND o.availability='interpretation_blocked'"
            ).fetchall())
            self.assertEqual({
                ("20250225-5101", "interpretation_blocked"),
                ("20260202-5047", "interpretation_blocked"),
            }, measured)

            # Paired valid production control: the same adapter continues to
            # retain clean, numeric capacity facts as present/pass.
            self.assertGreater(con.execute(
                "SELECT COUNT(*) FROM observations WHERE source_regime="
                "'Form 549B Capacity' AND availability='present' "
                "AND validation='pass' AND value_num IS NOT NULL"
            ).fetchone()[0], 0)
        finally:
            con.close()

    def test_published_csvs_are_portable_and_keep_database_populations(self) -> None:
        receipt_path = ROOT / "publication_receipt.json"
        db_path = ROOT / "staging" / "operating_assets.sqlite"
        self.assertTrue(receipt_path.is_file(), receipt_path)
        self.assertTrue(db_path.is_file(), db_path)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        generation = ROOT / receipt["generation_path"]

        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        expected = {
            "canonical_observations.csv": con.execute(
                "SELECT COUNT(*) FROM observations").fetchone()[0],
            "document_facts.csv": con.execute(
                "SELECT COUNT(*) FROM document_facts").fetchone()[0],
        }
        con.close()

        for name, table_rows in expected.items():
            with self.subTest(name=name):
                compatible = ROOT / "exports" / name
                frozen = generation / name
                self.assertEqual(compatible.read_bytes(), frozen.read_bytes())
                data = compatible.read_bytes()
                bad = sorted({value for value in data if value in FORBIDDEN})
                self.assertEqual([], bad, f"{name} contains C0 controls {bad}")
                with compatible.open("r", encoding="utf-8", newline="") as fh:
                    self.assertEqual(table_rows, sum(1 for _ in csv.DictReader(fh)))


if __name__ == "__main__":
    unittest.main()
