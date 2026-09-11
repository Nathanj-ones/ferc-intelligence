"""Regression controls for clean Build-A materialization and package hygiene."""

from __future__ import annotations

import csv
import json
import pathlib
import tempfile
import unittest

import build_release
from acceptance import candidate as acceptance_candidate
from implementation import materialize_candidate_tree


class ReleaseScratchExclusionTests(unittest.TestCase):
    def test_full_and_lite_payloads_exclude_all_work_scratch(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "work" / "w6-acceptance" / "run-1").mkdir(parents=True)
            (root / "work" / "w6-acceptance" / "run-1" / "mutated.sqlite").write_bytes(
                b"disposable database")
            (root / "keep.py").write_text("VALUE = 1\n", encoding="utf-8")

            for full in (False, True):
                paths = {rel for _path, rel in build_release.payload_paths(root, full=full)}
                self.assertEqual({"keep.py"}, paths)
                self.assertFalse(any(rel.startswith("work/") for rel in paths))


class AcceptanceCandidateCSVTests(unittest.TestCase):
    def test_slim_copy_counts_csv_records_and_preserves_quoted_newlines(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            source = root / "source.csv"
            target = root / "target.csv"
            with source.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["id", "passage"])
                for number in range(acceptance_candidate.SLIM_ROWS + 5):
                    passage = "line one\nline two" if number == 0 else f"row {number}"
                    writer.writerow([number, passage])
            acceptance_candidate._slim_copy(source, target)
            with target.open("r", encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(acceptance_candidate.SLIM_ROWS, len(rows))
            self.assertEqual("line one\nline two", rows[0]["passage"])
            self.assertEqual(str(acceptance_candidate.SLIM_ROWS - 1), rows[-1]["id"])


class CleanCandidateMaterializationTests(unittest.TestCase):
    def _write(self, root: pathlib.Path, relative: str, body: str = "x\n") -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_selected_files_retain_inputs_code_and_evidence_but_not_generated_state(self):
        with tempfile.TemporaryDirectory() as td:
            source = pathlib.Path(td)
            retained = {
                "run.py",
                "ferclib/schema.sql",
                "inputs/frozen/source.json",
                "evidence/test_fixtures/fixture.json",
                "config/universe.csv",
                "config/universe_draft.csv.notes",
                "evidence/data_side_facts.json",
            }
            generated = {
                "exports/canonical_observations.csv",
                ".generations/consumer_exports/old/data.csv",
                "final_records/FINAL_REPAIR_LEDGER.json",
                "publication_receipt.json",
                "artifact_manifest_full.json",
                "artifact_manifest_lite.json",
                "release_receipt_full.json",
                "release_receipt_lite.json",
                "staging/operating_assets.sqlite",
                "staging/operating_assets.sqlite-wal",
                "staging/build.task_ledger.json",
                "verification/validation_results.json",
                "work/w6-acceptance/run-1/extracted.sqlite",
                "config/universe_draft.csv",
                "data_side_facts.json",
            }
            for relative in retained | generated:
                self._write(source, relative)

            selected = {
                relative for relative, _path
                in materialize_candidate_tree.selected_files(source, [])
            }
            self.assertTrue(retained <= selected)
            self.assertFalse(generated & selected)

    def test_exact_stale_snapshots_are_excluded_but_lookalikes_survive(self):
        with tempfile.TemporaryDirectory() as td:
            source = pathlib.Path(td)
            excluded = {"config/universe_draft.csv", "data_side_facts.json"}
            retained = {
                "config/universe.csv",
                "config/universe_draft.csv.notes",
                "evidence/data_side_facts.json",
            }
            for relative in excluded | retained:
                self._write(source, relative)
            selected = {
                relative for relative, _path
                in materialize_candidate_tree.selected_files(source, [])
            }
            self.assertFalse(excluded & selected)
            self.assertTrue(retained <= selected)

    def test_explicit_include_cannot_reintroduce_generated_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            source = pathlib.Path(td)
            self._write(source, "exports/stale.csv")
            with self.assertRaisesRegex(
                    SystemExit, "not an approved immutable-input subtree"):
                materialize_candidate_tree.selected_files(source, ["exports"])

    def test_apply_starts_with_no_generated_outputs_and_keeps_declared_recovery_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            source = base / "source"
            destination = base / "candidate"
            ledger = base / "materialization.json"
            self._write(source, "run.py", "print('candidate')\n")
            self._write(source, "inputs/frozen/source.json", "{}\n")
            self._write(source, "evidence/test_fixtures/fixture.json", "{}\n")
            self._write(source, "implementation_logs/input_recovery/capture.json", "{}\n")
            self._write(source, "implementation_logs/worker/debug.log")
            self._write(source, "exports/stale.csv")
            self._write(source, ".generations/consumer_exports/stale.csv")
            self._write(source, "final_records/stale.json")
            self._write(source, "publication_receipt.json", "{}\n")
            self._write(source, "staging/operating_assets.sqlite")
            self._write(source, "verification/old_test.log")

            rc = materialize_candidate_tree.main([
                "--source", str(source),
                "--destination", str(destination),
                "--ledger", str(ledger),
                "--include-subtree", "implementation_logs/input_recovery",
                "--apply",
            ])
            self.assertEqual(0, rc)
            self.assertTrue((destination / "run.py").is_file())
            self.assertTrue((destination / "inputs/frozen/source.json").is_file())
            self.assertTrue((destination / "evidence/test_fixtures/fixture.json").is_file())
            self.assertTrue(
                (destination / "implementation_logs/input_recovery/capture.json").is_file())
            self.assertFalse((destination / "implementation_logs/worker/debug.log").exists())
            for relative in (
                    "exports", ".generations", "final_records", "publication_receipt.json",
                    "staging", "verification", "work"):
                self.assertFalse((destination / relative).exists(), relative)

            record = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual("complete", record["status"])
            copied = {row["path"] for row in record["files"]}
            self.assertIn("implementation_logs/input_recovery/capture.json", copied)
            self.assertFalse(any(path.startswith("exports/") for path in copied))

    def test_apply_refuses_nonempty_destination_instead_of_retaining_hidden_state(self):
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            source = base / "source"
            destination = base / "candidate"
            ledger = base / "materialization.json"
            self._write(source, "run.py", "print('candidate')\n")
            self._write(destination, "work/w6-acceptance/stale.sqlite", "stale\n")

            with self.assertRaisesRegex(SystemExit, "absent or empty"):
                materialize_candidate_tree.main([
                    "--source", str(source),
                    "--destination", str(destination),
                    "--ledger", str(ledger),
                    "--apply",
                ])
            self.assertEqual(
                "stale\n",
                (destination / "work/w6-acceptance/stale.sqlite").read_text())
            self.assertFalse(ledger.exists())


if __name__ == "__main__":
    unittest.main()
