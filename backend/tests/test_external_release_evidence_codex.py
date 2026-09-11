"""Synthetic tests for the post-package external-evidence finalizer."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from implementation import finalize_external_release_evidence as finalizer  # noqa: E402


def write_json(path: pathlib.Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def identity(path: pathlib.Path) -> tuple[str, int]:
    raw = path.read_bytes()
    return hashlib.sha256(raw).hexdigest(), len(raw)


class Fixture:
    def __init__(self, directory: str):
        self.base = pathlib.Path(directory)
        self.logs = self.base / "logs"
        self.evidence = self.base / "evidence"
        self.logs.mkdir()
        self.evidence.mkdir()
        self.archive = self.base / "candidate-full.zip"
        with zipfile.ZipFile(self.archive, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("payload/readme.txt", b"immutable candidate\n")
        archive_sha, archive_bytes = identity(self.archive)

        self.validation = self.base / "build_b_acceptance.json"
        write_json(self.validation, {
            "schema": "ferc-build-b-acceptance-v1",
            "status": "PASS",
            "checks": {name: True for name in finalizer.BUILD_B_CHECKS},
            "identities": {"archive_sha256": archive_sha,
                           "archive_bytes": archive_bytes},
        })

        executable = sys.executable
        version = ".".join(str(v) for v in sys.version_info[:3])
        runtime = {
            "executable": executable,
            "python_version": version,
            "sqlite_version": sqlite3.sqlite_version,
            "platform": "synthetic-test-platform",
        }
        self.runtime = self.base / "RUNTIME_REQUIREMENTS.json"
        write_json(self.runtime, {
            "schema_version": "1.0.0",
            "python": {"tested_runtimes": [runtime], "third_party_packages": [],
                       "dependency_install_command": None},
            "external_executables": [],
        })

        lines = []
        for number in range(100):
            method = f"test_case_{number:03d}"
            test_id = f"tests.synthetic.CompleteSuite.{method}"
            if number == 98:
                result = "skipped 'optional unavailable reference'"
            elif number == 99:
                result = "expected failure"
            else:
                result = "ok"
            lines.append(f"{method} ({test_id}) ... {result}")
        lines.extend(["", "Ran 100 tests in 0.100s", "",
                      "OK (skipped=1, expected failures=1)"])
        self.log = self.logs / "complete_py.log"
        self.log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        self.run_spec = self.base / "run_spec.json"
        write_json(self.run_spec, {
            "schema": "ferc-test-run-spec-v1",
            "runs": [{
                "run_id": "complete-synthetic",
                "kind": "complete_suite",
                "acceptance_role": "acceptance",
                "complete_suite": True,
                "log": self.log.name,
                "command": [executable, "-B", "-m", "unittest", "discover", "-v"],
                "interpreter": {"executable": executable,
                                "implementation": "CPython", **runtime},
                "exit_code": 0,
                "skip_details": [{
                    "test": "tests.synthetic.CompleteSuite.test_case_098",
                    "classification": "optional unavailable reference",
                    "reason": "not a required release capability",
                    "required": False,
                }],
                "xfail_details": [{
                    "test": "tests.synthetic.CompleteSuite.test_case_099",
                    "classification": "historical negative fixture",
                    "reason": "intentionally defective baseline",
                    "required": False,
                }],
            }],
        })

        (self.evidence / "small.txt").write_text("small safe evidence\n",
                                                  encoding="utf-8")
        (self.evidence / "large.bin").write_bytes(b"L" * 4096)
        self.evidence_spec = self.base / "evidence_spec.json"
        self._write_evidence_spec()
        self.output = self.base / "final-sidecars"

    def _write_evidence_spec(self, *, include_path="small.txt", include_name="notes/small.txt",
                             unbundled=True):
        large = ([{"path": "large.bin", "name": "large/full-debug.bin",
                   "category": "complete_local_debug_evidence",
                   "reason": "kept outside the compact upload bundle by size policy"}]
                 if unbundled else [])
        write_json(self.evidence_spec, {
            "schema": "ferc-evidence-selection-v1",
            "include": [{"path": include_path, "name": include_name,
                         "category": "focused_test_evidence"}],
            "unbundled_large_evidence": large,
        })

    def argv(self):
        return [
            "--archive", str(self.archive),
            "--package-validation", str(self.validation),
            "--run-spec", str(self.run_spec),
            "--logs-dir", str(self.logs),
            "--runtime-requirements", str(self.runtime),
            "--evidence-spec", str(self.evidence_spec),
            "--evidence-root", str(self.evidence),
            "--out-dir", str(self.output),
        ]


class TestExternalReleaseEvidence(unittest.TestCase):

    def test_verbose_test_ids_normalize_cpython314_and_cpython39_formats(self):
        expected = "tests.synthetic.CompleteSuite.test_case_000"
        for runtime, parenthetical in (
                ("CPython 3.14", expected),
                ("CPython 3.9", "tests.synthetic.CompleteSuite")):
            with self.subTest(runtime=runtime):
                raw = (
                    f"test_case_000 ({parenthetical}) ... ok\n\n"
                    "Ran 1 test in 0.001s\n\nOK\n"
                ).encode("utf-8")
                parsed = finalizer._parse_unittest_log(raw, runtime)
                self.assertEqual(parsed["results"], {expected: "pass"})
                self.assertEqual(parsed["counts"]["executed"], 1)
                self.assertTrue(parsed["successful"])

    def test_verbose_test_id_with_different_method_is_refused(self):
        raw = (
            "test_expected (tests.synthetic.CompleteSuite.test_other) ... ok\n\n"
            "Ran 1 test in 0.001s\n\nOK\n"
        ).encode("utf-8")
        with self.assertRaisesRegex(finalizer.EvidenceRefused,
                                    "inconsistent verbose test id"):
            finalizer._parse_unittest_log(raw, "mismatched method")

    def test_verbose_parser_accepts_multiline_capture_and_rejects_ambiguous_status(self):
        raw = (
            "test_documented (tests.synthetic.CompleteSuite.test_documented)\n"
            "A short description. ... ok\n"
            "test_capture (tests.synthetic.CompleteSuite) ... diagnostic line\n"
            "negative-fixture traceback\n"
            "ok\n\n----------------------------------------------------------------------\n"
            "Ran 2 tests in 0.002s\n\nOK\n"
        ).encode("utf-8")
        parsed = finalizer._parse_unittest_log(raw, "multiline")
        self.assertEqual(2, parsed["counts"]["executed"])
        self.assertEqual(2, parsed["counts"]["pass"])
        self.assertTrue(parsed["successful"])

        ambiguous = raw.replace(
            b"negative-fixture traceback\nok\n",
            b"ok\nnegative-fixture traceback\nok\n")
        with self.assertRaisesRegex(finalizer.EvidenceRefused,
                                    "2 unambiguous per-test outcome"):
            finalizer._parse_unittest_log(ambiguous, "ambiguous")

    def test_success_binds_archive_logs_dependencies_and_unbundled_evidence(self):
        with tempfile.TemporaryDirectory(prefix="ferc-sidecar-success-") as td:
            fixture = Fixture(td)
            before = identity(fixture.archive)
            self.assertEqual(finalizer.main(fixture.argv()), 0)
            self.assertEqual(identity(fixture.archive), before,
                             "post-package finalization modified the release archive")

            expected = {finalizer.TEST_MANIFEST_NAME, finalizer.EVIDENCE_INDEX_NAME,
                        finalizer.EVIDENCE_ZIP_NAME, finalizer.RECEIPT_NAME}
            self.assertEqual({p.name for p in fixture.output.iterdir()}, expected)
            manifest = json.loads(
                (fixture.output / finalizer.TEST_MANIFEST_NAME).read_text())
            self.assertEqual(manifest["totals"], {
                "collected": 100, "executed": 100, "pass": 98,
                "fail": 0, "error": 0, "skip": 1, "xfail": 1, "xpass": 0,
            })
            run = manifest["runs"][0]
            self.assertEqual(run["command"],
                             [sys.executable, "-B", "-m", "unittest", "discover", "-v"])
            self.assertEqual(run["complete_log"]["sha256"], identity(fixture.log)[0])
            self.assertEqual(run["interpreter"]["sqlite_version"], sqlite3.sqlite_version)

            index = json.loads(
                (fixture.output / finalizer.EVIDENCE_INDEX_NAME).read_text())
            self.assertEqual(index["archive"]["sha256"], before[0])
            self.assertTrue(index["explicit_large_evidence_inventory"])
            self.assertEqual(index["unbundled_large_evidence"][0]["name"],
                             "large/full-debug.bin")
            self.assertEqual(index["unbundled_large_evidence"][0]["bytes"], 4096)
            self.assertTrue(index["evidence_zip"]["within_target"])

            with zipfile.ZipFile(fixture.output / finalizer.EVIDENCE_ZIP_NAME) as bundle:
                names = set(bundle.namelist())
                self.assertIn(finalizer.TEST_MANIFEST_NAME, names)
                self.assertIn("logs/complete_py.log", names)
                self.assertIn("evidence/notes/small.txt", names)
                self.assertNotIn(finalizer.RECEIPT_NAME, names)
                self.assertNotIn(finalizer.EVIDENCE_INDEX_NAME, names)
                self.assertIsNone(bundle.testzip())

            receipt = json.loads(
                (fixture.output / finalizer.RECEIPT_NAME).read_text())
            self.assertEqual(receipt["status"], "PASS")
            self.assertEqual(receipt["release_archive"]["sha256"], before[0])
            self.assertEqual(receipt["test_manifest"]["totals"], manifest["totals"])
            for output in fixture.output.iterdir():
                if output.suffix == ".json":
                    self.assertNotIn(str(fixture.base), output.read_text(),
                                     "a generated upload sidecar exposed a local path")

    def test_archive_identity_mismatch_or_failed_build_b_check_is_refused(self):
        for mutation in ("identity", "check"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-sidecar-validation-") as td:
                fixture = Fixture(td)
                body = json.loads(fixture.validation.read_text())
                if mutation == "identity":
                    body["identities"]["archive_sha256"] = "0" * 64
                else:
                    body["checks"]["acceptance_pass"] = False
                write_json(fixture.validation, body)
                self.assertEqual(finalizer.main(fixture.argv()), 1)
                self.assertFalse(fixture.output.exists())

    def test_large_complete_log_can_be_explicitly_unbundled_but_remains_hash_bound(self):
        with tempfile.TemporaryDirectory(prefix="ferc-sidecar-unbundled-log-") as td:
            fixture = Fixture(td)
            spec = json.loads(fixture.run_spec.read_text())
            spec["runs"][0]["bundle_log"] = False
            spec["runs"][0]["unbundled_reason"] = (
                "complete local log retained outside the sub-25MB upload bundle")
            write_json(fixture.run_spec, spec)
            self.assertEqual(finalizer.main(fixture.argv()), 0)
            manifest = json.loads(
                (fixture.output / finalizer.TEST_MANIFEST_NAME).read_text())
            self.assertEqual(manifest["runs"][0]["complete_log"]["sha256"],
                             identity(fixture.log)[0])
            self.assertEqual(manifest["runs"][0]["complete_log"]["bundle"]["status"],
                             "unbundled_by_declaration")
            index = json.loads(
                (fixture.output / finalizer.EVIDENCE_INDEX_NAME).read_text())
            self.assertEqual(index["unbundled_test_logs"][0]["sha256"],
                             identity(fixture.log)[0])
            with zipfile.ZipFile(fixture.output / finalizer.EVIDENCE_ZIP_NAME) as bundle:
                self.assertNotIn("logs/complete_py.log", bundle.namelist())

    def test_incomplete_or_zero_case_log_is_refused(self):
        for mutation in ("incomplete", "zero"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-sidecar-log-") as td:
                fixture = Fixture(td)
                if mutation == "incomplete":
                    fixture.log.write_text(
                        "test_one (tests.synthetic.Case.test_one) ... ok\n\n"
                        "Ran 2 tests in 0.1s\n\nOK\n", encoding="utf-8")
                else:
                    fixture.log.write_text("Ran 0 tests in 0.0s\n\nOK\n",
                                           encoding="utf-8")
                self.assertEqual(finalizer.main(fixture.argv()), 1)
                self.assertFalse(fixture.output.exists())

    def test_required_skip_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="ferc-sidecar-skip-") as td:
            fixture = Fixture(td)
            spec = json.loads(fixture.run_spec.read_text())
            spec["runs"][0]["skip_details"][0]["required"] = True
            write_json(fixture.run_spec, spec)
            self.assertEqual(finalizer.main(fixture.argv()), 1)
            self.assertFalse(fixture.output.exists())

    def test_unsafe_release_member_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="ferc-sidecar-member-") as td:
            fixture = Fixture(td)
            with zipfile.ZipFile(fixture.archive, "w") as archive:
                archive.writestr("../escape.txt", "bad")
            archive_sha, archive_bytes = identity(fixture.archive)
            body = json.loads(fixture.validation.read_text())
            body["identities"].update(archive_sha256=archive_sha,
                                      archive_bytes=archive_bytes)
            write_json(fixture.validation, body)
            self.assertEqual(finalizer.main(fixture.argv()), 1)
            self.assertFalse(fixture.output.exists())

    def test_secret_or_local_path_in_included_evidence_is_refused(self):
        for mutation, content in (
                ("secret", "api_key=REALLOOKINGSECRET12345\n"),
                ("path", "trace at /Users/example/private/project/test.py:10\n")):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    prefix="ferc-sidecar-hygiene-") as td:
                fixture = Fixture(td)
                (fixture.evidence / "small.txt").write_text(content, encoding="utf-8")
                self.assertEqual(finalizer.main(fixture.argv()), 1)
                self.assertFalse(fixture.output.exists())

    def test_symlinked_selected_evidence_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="ferc-sidecar-symlink-") as td:
            fixture = Fixture(td)
            selected = fixture.evidence / "small.txt"
            target = fixture.evidence / "target.txt"
            target.write_text("target bytes\n", encoding="utf-8")
            selected.unlink()
            selected.symlink_to(target)
            self.assertEqual(finalizer.main(fixture.argv()), 1)
            self.assertFalse(fixture.output.exists())

    def test_size_limit_refuses_instead_of_silently_dropping_evidence(self):
        with tempfile.TemporaryDirectory(prefix="ferc-sidecar-size-") as td:
            fixture = Fixture(td)
            (fixture.evidence / "small.txt").write_bytes(os.urandom(4096))
            with mock.patch.object(finalizer, "MAX_EVIDENCE_ZIP_BYTES", 1024):
                self.assertEqual(finalizer.main(fixture.argv()), 1)
            self.assertFalse(fixture.output.exists())

    def test_existing_output_directory_is_never_overwritten(self):
        with tempfile.TemporaryDirectory(prefix="ferc-sidecar-output-") as td:
            fixture = Fixture(td)
            fixture.output.mkdir()
            marker = fixture.output / "owner.txt"
            marker.write_text("pre-existing\n", encoding="utf-8")
            self.assertEqual(finalizer.main(fixture.argv()), 1)
            self.assertEqual(marker.read_text(), "pre-existing\n")


if __name__ == "__main__":
    unittest.main()
