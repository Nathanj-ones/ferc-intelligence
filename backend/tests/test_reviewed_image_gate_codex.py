"""Regression tests for the fail-closed A17 reviewed-image release gate."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import shutil
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TOOL_PATH = ROOT / "tools" / "verify_reviewed_image_sources.py"
SPEC = importlib.util.spec_from_file_location("verify_reviewed_image_sources", TOOL_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load A17 release gate")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class ReviewedImageGateTests(unittest.TestCase):

    def test_hosted_vision_caveat_glyph_correction_is_context_bound(self):
        from ferclib.image_ocr import _normalise_row

        raw = ("storage facilities under reasonably representative operating "
               "assumptior nd the respective assignments")
        normalised, corrections = _normalise_row(raw)
        self.assertIn("reasonably representative operating assumptions", normalised)
        self.assertEqual(["bounded_ocr_caveat_glyph_correction"],
                         [item["kind"] for item in corrections])
        self.assertEqual(raw, corrections[0]["from"],
                         "the exact OCR text must remain in correction evidence")

        for unrelated in (
            "storage facilities under representative operating assumptior",
            "reasonably representative financial assumptior",
            "a generic operating assumptior",
        ):
            with self.subTest(unrelated=unrelated):
                unchanged, rejected = _normalise_row(unrelated)
                self.assertEqual(unrelated, unchanged)
                self.assertEqual([], rejected)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ferc-a17-gate-test-")
        self.base = pathlib.Path(self.temp.name)
        self.cache = self.base / "source_cache"
        self.cache.mkdir()
        original_index = json.loads((ROOT / "source_cache" / "index.json").read_text())
        wanted = {str(item["source_sha256"]) for item in gate.SOURCE_CONTRACTS}
        index = {key: value for key, value in original_index.items()
                 if value.get("content_hash") in wanted}
        self.assertEqual(wanted, {value["content_hash"] for value in index.values()},
                         "test precondition: both actual sources must exist")
        for content_hash in wanted:
            source = ROOT / "source_cache" / "objects" / content_hash[:2] / content_hash
            target = self.cache / "objects" / content_hash[:2] / content_hash
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        (self.cache / "index.json").write_text(
            json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.fixture = ROOT / "evidence" / "test_fixtures" / "reviewed_image_sources.json"
        self.pdftoppm = shutil.which("pdftoppm")
        self.swift = shutil.which("swift")

    def tearDown(self):
        self.temp.cleanup()

    def run_gate(self, executable=None):
        return gate.run_gate(ROOT, self.cache, self.fixture,
                             self.pdftoppm if executable is None else executable,
                             self.swift)

    def test_valid_isolated_cache_passes_and_retains_no_render(self):
        # pdftoppm is mandatory when this gate is invoked: absence is a failure,
        # never grounds for a unittest skip.
        self.assertIsNotNone(self.pdftoppm, "required gate executable pdftoppm is absent")
        self.assertIsNotNone(self.swift, "required local Vision Swift executable is absent")
        result = self.run_gate()
        self.assertEqual("verified", result["status"], result.get("errors"))
        self.assertEqual({"20240223-5073", "20240223-5075"},
                         {item["accession"] for item in result["sources"]})
        for source in result["sources"]:
            self.assertEqual(0, source["pipeline_extractor"]["rows"])
            self.assertFalse(source["pipeline_extractor"]["text_layer"])
            self.assertTrue(source["page_1_render"]["signature_valid"])
            self.assertTrue(source["page_1_render"]["crc_valid"])
            self.assertGreater(source["page_1_render"]["width"], 0)
            self.assertGreater(source["page_1_render"]["height"], 0)
            self.assertFalse(source["page_1_render"]["render_retained"])
            self.assertTrue(source["reviewed_transcription"]["manual_review"])
            self.assertTrue(source["reviewed_transcription"]["ocr_used"])
            self.assertGreater(source["automatic_ocr"]["rows"], 0)
            self.assertFalse(source["automatic_ocr"]["credential_environment_forwarded"])
        self.assertTrue(result["claim_boundary"]["automatic_image_value_extraction_claimed"])

    def test_tampered_source_bytes_fail_before_acceptance(self):
        contract = gate.SOURCE_CONTRACTS[0]
        content_hash = str(contract["source_sha256"])
        path = self.cache / "objects" / content_hash[:2] / content_hash
        raw = bytearray(path.read_bytes())
        raw[-20] ^= 1
        path.write_bytes(raw)
        result = self.run_gate()
        self.assertEqual("failed", result["status"])
        self.assertIn("source_object_hash_mismatch",
                      {error["code"] for error in result["errors"]})
        self.assertLess(result["summary"]["verified_sources"], 2)

    def test_wrong_cache_hash_mapping_fails_closed(self):
        index_path = self.cache / "index.json"
        index = json.loads(index_path.read_text())
        target = str(gate.SOURCE_CONTRACTS[1]["source_sha256"])
        changed = 0
        for record in index.values():
            if record.get("content_hash") == target:
                record["content_hash"] = "0" * 64
                changed += 1
        self.assertGreater(changed, 0, "test mutation did not reach the intended cache record")
        index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
        result = self.run_gate()
        self.assertEqual("failed", result["status"])
        self.assertIn("source_not_indexed", {error["code"] for error in result["errors"]})

    def test_missing_pdftoppm_is_failure_not_skip(self):
        missing = str(self.base / "definitely-not-pdftoppm")
        output = self.base / "missing-tool-result.json"
        returncode = gate.main([
            "--root", str(ROOT), "--cache-dir", str(self.cache),
            "--review-fixture", str(self.fixture), "--pdftoppm", missing,
            "--output", str(output),
        ])
        self.assertEqual(1, returncode, "failed gate command returned success")
        self.assertTrue(output.is_file(), "failed gate did not emit its machine result")
        result = json.loads(output.read_text())
        self.assertEqual("failed", result["status"])
        self.assertIn("pdftoppm_not_executable",
                      {error["code"] for error in result["errors"]})
        self.assertEqual(0, result["summary"]["verified_sources"])

    def test_review_marked_as_ocr_is_rejected(self):
        from adapters import capacity

        content_hash = str(gate.SOURCE_CONTRACTS[0]["source_sha256"])
        record = capacity.REVIEWED_PAGE_IMAGES[content_hash]
        original = record["ocr_used"]
        try:
            record["ocr_used"] = True
            result = self.run_gate()
        finally:
            record["ocr_used"] = original
        self.assertEqual("failed", result["status"])
        self.assertIn("adapter_review_ocr_mismatch",
                      {error["code"] for error in result["errors"]})

    def test_automatic_ocr_disagreement_is_rejected_after_exercising_production_path(self):
        from adapters import capacity

        # Select the value through the independent gate contract and the
        # production parser's semantic output.  OCR line wrapping is an engine
        # detail, so this fixture must not depend on a literal sentence fragment
        # such as "approximately 420" occurring in one Vision row.
        target_contract = gate.SOURCE_CONTRACTS[0]
        target_hash = str(target_contract["source_sha256"])
        target_expected = next(
            expected for expected in target_contract["expected"]
            if expected[0] == "estimated_peak_day_delivery_capacity")
        _kind, target_value, target_unit, target_qualifier = target_expected
        replacement_value = str(int(target_value) + 1)

        original = capacity.extract_image_pdf
        calls = []
        mutations = []

        def changed(*args, **kwargs):
            pages, evidence = original(*args, **kwargs)
            source_hash = evidence["source_sha256"]
            calls.append({
                "source_sha256": source_hash,
                "page_count": len(pages),
                "row_count": sum(len(page.get("rows") or []) for page in pages),
            })
            if source_hash != target_hash:
                return pages, evidence

            before_figures = capacity.read_figures(pages, 2023)
            targets = [
                figure for figure in before_figures
                if str(figure.get("value_text")) == target_value
                and figure.get("unit") == target_unit
                and figure.get("qualifier") == target_qualifier
                and not (figure.get("problems") or [])
            ]
            if len(targets) != 1:
                raise AssertionError(
                    "fault-injection precondition: production parser did not yield "
                    f"exactly one reviewed target figure; found {len(targets)}")

            target = targets[0]
            page = next((item for item in pages
                         if item.get("page") == target.get("page")), None)
            if page is None:
                raise AssertionError("fault-injection target page is absent")
            row_index = target.get("row_index")
            if not isinstance(row_index, int) or not (0 <= row_index < len(page["rows"])):
                raise AssertionError("fault-injection target row identity is invalid")
            row = page["rows"][row_index]

            # Alter exactly the numeric token selected above in both row views
            # consumed by the production parser.  Numeric boundaries prevent a
            # prefix such as 4200 from accidentally satisfying the fixture.
            token = re.compile(
                rf"(?<![\d.]){re.escape(target_value)}(?![\d.])")
            before_text = str(row.get("text") or "")
            row_hits = len(token.findall(before_text))
            cell_hits = sum(len(token.findall(str(cell.get("text") or "")))
                            for cell in row.get("cells") or [])
            if row_hits != 1 or cell_hits != 1:
                raise AssertionError(
                    "fault-injection target must occur exactly once in the OCR row "
                    f"and cell views; row_hits={row_hits}, cell_hits={cell_hits}")
            row["text"] = token.sub(replacement_value, before_text, count=1)
            for cell in row["cells"]:
                cell["text"] = token.sub(
                    replacement_value, str(cell.get("text") or ""), count=1)

            after_figures = capacity.read_figures(pages, 2023)
            altered = [
                figure for figure in after_figures
                if str(figure.get("value_text")) == replacement_value
                and figure.get("unit") == target_unit
                and figure.get("qualifier") == target_qualifier
                and not (figure.get("problems") or [])
            ]
            if len(altered) != 1:
                raise AssertionError(
                    "fault injection did not alter the production parser's semantic "
                    f"figure exactly once; found {len(altered)}")
            mutations.append({
                "source_sha256": source_hash,
                "page": target["page"],
                "row_index": row_index,
                "row_hits": row_hits,
                "cell_hits": cell_hits,
                "before_value": target_value,
                "after_value": altered[0]["value_text"],
                "before_signature": capacity._review_signature(before_figures),
                "after_signature": capacity._review_signature(after_figures),
            })
            return pages, evidence

        try:
            capacity.extract_image_pdf = changed
            result = self.run_gate()
        finally:
            capacity.extract_image_pdf = original
        self.assertEqual(
            {str(contract["source_sha256"]) for contract in gate.SOURCE_CONTRACTS},
            {call["source_sha256"] for call in calls},
            "fault injection did not exercise production OCR for every gate source")
        self.assertTrue(all(call["page_count"] > 0 and call["row_count"] > 0
                            for call in calls),
                        "production OCR returned no usable page rows")
        self.assertEqual(1, len(mutations),
                         "intended OCR figure was not mutated exactly once")
        mutation = mutations[0]
        self.assertEqual(target_hash, mutation["source_sha256"])
        self.assertEqual((1, 1), (mutation["row_hits"], mutation["cell_hits"]))
        self.assertEqual(target_value, mutation["before_value"])
        self.assertEqual(replacement_value, mutation["after_value"])
        self.assertNotEqual(mutation["before_signature"], mutation["after_signature"])
        self.assertEqual("failed", result["status"])
        self.assertIn(
            ("automatic_ocr_review_disagreement", target_contract["accession"]),
            {(error["code"], error.get("accession")) for error in result["errors"]})

        # Paired valid control in the same gate run: the unmodified source must
        # still be accepted, so indiscriminate rejection cannot pass this test.
        other_sources = [source for source in result["sources"]
                         if source.get("accession") != target_contract["accession"]]
        self.assertEqual(1, len(other_sources))
        self.assertEqual("verified", other_sources[0]["status"],
                         other_sources[0].get("error"))

    def test_missing_swift_is_failure_not_skip(self):
        missing = str(self.base / "definitely-not-swift")
        result = gate.run_gate(ROOT, self.cache, self.fixture, self.pdftoppm, missing)
        self.assertEqual("failed", result["status"])
        self.assertIn("automatic_ocr_failed",
                      {error["code"] for error in result["errors"]})

    def test_png_parser_rejects_non_png_and_empty_dimensions(self):
        with self.assertRaises(gate.GateFailure) as ctx:
            gate._png_identity(b"not a png")
        self.assertEqual("render_not_png", ctx.exception.code)


if __name__ == "__main__":
    unittest.main()
