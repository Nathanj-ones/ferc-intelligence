"""Regression controls for non-destructive, output-scoped crosswalk builds."""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import pathlib
import sqlite3
import tempfile
import unittest

import build_crosswalk
import build_field_status
from ferclib.publication import verify_receipt


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _tree_identity(root: pathlib.Path) -> dict[str, tuple[int, str]]:
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            body = path.read_bytes()
            out[path.relative_to(root).as_posix()] = (
                len(body), hashlib.sha256(body).hexdigest())
    return out


class CrosswalkIsolationTests(unittest.TestCase):
    def _source_args(self) -> list[str]:
        return [
            "--matrix", str(ROOT / "inputs/day3/FERC_operating_assets_metric_decision_matrix.csv"),
            "--gap", str(ROOT / "inputs/day3/FERC_operating_assets_current_vs_target_gap.csv"),
            "--audited-fields", str(
                ROOT / "inputs/audit_baseline/field_status_all_166_audited.csv"),
        ]

    def test_valid_external_generation_does_not_touch_frozen_config(self):
        before = _tree_identity(ROOT / "config")
        with tempfile.TemporaryDirectory(prefix="ferc-crosswalk-output-") as td:
            out = pathlib.Path(td) / "config"
            args = self._source_args() + [
                "--out", str(out / "requirements_crosswalk.csv"),
                "--summary-out", str(out / "requirements_crosswalk_summary.json"),
                "--field-out", str(out / "field_crosswalk_166.csv"),
                "--receipt", str(out / "crosswalk_publication.json"),
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, build_crosswalk.main(args))
            manifest = verify_receipt(out, out / "crosswalk_publication.json")
            self.assertEqual(3, len(manifest["files"]))
            with (out / "requirements_crosswalk.csv").open(
                    newline="", encoding="utf-8-sig") as fh:
                self.assertEqual(146, len(list(csv.DictReader(fh))))
            identity = build_field_status._validate_field_crosswalk(
                out / "field_crosswalk_166.csv")
            self.assertEqual(
                {"rows": 167, "audited_rows": 166, "target_keys": 168}, identity)
        self.assertEqual(before, _tree_identity(ROOT / "config"))

    def test_bad_prerequisite_preserves_files_and_database(self):
        with tempfile.TemporaryDirectory(prefix="ferc-crosswalk-bad-") as td:
            root = pathlib.Path(td)
            bad_matrix = root / "bad.csv"
            bad_matrix.write_text("wrong,columns\n1,2\n", encoding="utf-8")
            outputs = [root / name for name in (
                "requirements_crosswalk.csv", "requirements_crosswalk_summary.json",
                "field_crosswalk_166.csv", "crosswalk_publication.json")]
            for path in outputs:
                path.write_bytes(("last-good:" + path.name).encode())
            db_path = root / "state.sqlite"
            con = sqlite3.connect(db_path)
            con.executescript("""
                CREATE TABLE requirements_crosswalk(
                  source_doc TEXT NOT NULL, source_row TEXT NOT NULL,
                  disposition TEXT NOT NULL, metric_id TEXT, template TEXT, note TEXT,
                  PRIMARY KEY(source_doc, source_row));
                INSERT INTO requirements_crosswalk VALUES
                  ('last-good', 'row', 'carried', 'metric', 'template', 'preserve');
            """)
            con.commit()
            con.close()
            before = {p: p.read_bytes() for p in outputs}
            args = self._source_args() + [
                "--matrix", str(bad_matrix),
                "--out", str(outputs[0]), "--summary-out", str(outputs[1]),
                "--field-out", str(outputs[2]), "--receipt", str(outputs[3]),
                "--db", str(db_path),
            ]
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(2, build_crosswalk.main(args))
            self.assertEqual(before, {p: p.read_bytes() for p in outputs})
            con = sqlite3.connect(db_path)
            self.assertEqual(
                [("last-good", "row", "preserve")],
                con.execute("SELECT source_doc,source_row,note "
                            "FROM requirements_crosswalk").fetchall())
            con.close()

    def test_field_status_rejects_bad_crosswalk_before_opening_outputs_or_db(self):
        with tempfile.TemporaryDirectory(prefix="ferc-field-crosswalk-bad-") as td:
            root = pathlib.Path(td)
            bad = root / "field.csv"
            bad.write_text("source_doc,source_row\na,b\n", encoding="utf-8")
            out = root / "exports"
            out.mkdir()
            sentinel = out / "field_status.csv"
            sentinel.write_bytes(b"last-good\n")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(2, build_field_status.main([
                    "--db", str(root / "must-not-be-created.sqlite"),
                    "--out", str(out), "--field-crosswalk", str(bad)]))
            self.assertFalse((root / "must-not-be-created.sqlite").exists())
            self.assertEqual(b"last-good\n", sentinel.read_bytes())


if __name__ == "__main__":
    unittest.main()
