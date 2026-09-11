import copy
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from implementation import build_final_release_records as final_records
from implementation import build_test_run_manifest as builder


def _log(rows, summary, terminal):
    return (("\n".join(rows) + f"\n\nRan {len(rows)} tests in 0.010s\n\n"
             + terminal + "\n").encode("utf-8"))


def _passing_log(count=100):
    ids = [f"tests.synthetic.CompleteSuite.test_case_{number:03d}"
           for number in range(count)]
    rows = [f"test_case_{number:03d} ({test_id}) ... ok"
            for number, test_id in enumerate(ids)]
    return _log(rows, count, "OK"), ids


class BuildTestRunManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.temp.name)
        self.logs = self.base / "logs"
        self.logs.mkdir()
        self.output = self.base / "manifest.json"
        raw, ids = _passing_log()
        (self.logs / "complete.log").write_bytes(raw)
        self.test_ids = ids
        self.spec = {
            "schema": builder.SPEC_SCHEMA,
            "issue_test_evidence": {"A01": [ids[0]]},
            "exception_test_evidence": {"blk-one": [ids[1]]},
            "runs": [{
                "run_id": "python314-complete", "kind": "complete_suite",
                "acceptance_role": "acceptance", "complete_suite": True,
                "log": "complete.log",
                "command": ["python3.14", "-B", "-m", "unittest", "discover", "-v"],
                "interpreter": "CPython 3.14.7", "exit_code": 0,
                "skip_details": [], "xfail_details": [],
            }],
        }

    def tearDown(self):
        self.temp.cleanup()

    def _build(self, spec=None):
        return builder.build_manifest(ROOT, self.logs, spec or self.spec)

    def test_valid_manifest_is_deterministic_and_accepted_by_final_consumer(self):
        first = self._build()
        second = self._build()
        self.assertEqual(first, second)
        (ROOT / "staging").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "staging") as temp:
            manifest_path = pathlib.Path(temp) / "manifest.json"
            manifest_path.write_text(json.dumps(first, indent=2, sort_keys=True) + "\n")
            checked = final_records._validate_test_runs(
                manifest_path, self.logs.resolve(), {"A01"}, {"blk-one"}, ROOT)
        self.assertEqual(1, checked["successful_complete_suites"])
        self.assertEqual(100, checked["runs"][0]["counts"]["pass"])
        self.assertEqual(builder.test_tool_snapshot(ROOT), first["tested_tree_snapshot"])

    def test_bounded_search_importer_bytes_are_inside_test_tool_snapshot(self):
        candidate = self.base / "snapshot-candidate"
        importer = (candidate / "implementation" /
                    "import_bounded_elibrary_search_capture.py")
        importer.parent.mkdir(parents=True)
        importer.write_bytes(b"first verified importer\n")
        first = builder.test_tool_snapshot(candidate)
        self.assertEqual(first, final_records._test_tool_snapshot(candidate))
        importer.write_bytes(b"second changed importer\n")
        second = builder.test_tool_snapshot(candidate)
        self.assertEqual(second, final_records._test_tool_snapshot(candidate))
        self.assertNotEqual(first, second)

    def test_cli_writes_exact_hash_and_counts(self):
        spec_path = self.base / "spec.json"
        spec_path.write_text(json.dumps(self.spec))
        command = [sys.executable, str(ROOT / "implementation/build_test_run_manifest.py"),
                   "--root", str(ROOT), "--logs-dir", str(self.logs),
                   "--run-spec", str(spec_path), "--out", str(self.output)]
        proc = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        report = json.loads(proc.stdout)
        raw = self.output.read_bytes()
        self.assertEqual(len(raw), report["bytes"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), report["sha256"])
        self.assertEqual(100, report["executed"])

    def test_intentional_historical_negative_control_is_recorded_but_not_evidence(self):
        bad_id = "tests.synthetic.Historical.test_bad_baseline"
        (self.logs / "historical.log").write_bytes(_log(
            [f"test_bad_baseline ({bad_id}) ... FAIL"], 1, "FAILED (failures=1)"))
        spec = copy.deepcopy(self.spec)
        spec["runs"].append({
            "run_id": "historical-bad", "kind": "historical_fixture",
            "acceptance_role": "historical_negative_control", "complete_suite": False,
            "log": "historical.log", "command": ["python3", "-m", "unittest", "-v"],
            "interpreter": "CPython 3.14.7", "exit_code": 1,
            "skip_details": [], "xfail_details": [],
        })
        manifest = self._build(spec)
        self.assertEqual("historical_negative_control",
                         manifest["runs"][1]["acceptance_role"])
        self.assertEqual(1, manifest["runs"][1]["counts"]["fail"])
        spec["issue_test_evidence"]["A01"] = [bad_id]
        with self.assertRaisesRegex(builder.ManifestRefused, "not observed passing"):
            self._build(spec)

    def test_exit_code_and_result_must_agree_in_both_directions(self):
        spec = copy.deepcopy(self.spec)
        spec["runs"][0]["exit_code"] = 1
        with self.assertRaisesRegex(builder.ManifestRefused, "disagrees"):
            self._build(spec)

        bad_id = "tests.synthetic.Bad.test_failure"
        (self.logs / "bad.log").write_bytes(_log(
            [f"test_failure ({bad_id}) ... FAIL"], 1, "FAILED (failures=1)"))
        spec = copy.deepcopy(self.spec)
        spec["runs"][0].update({"log": "bad.log", "exit_code": 0,
                                "complete_suite": False, "kind": "targeted"})
        with self.assertRaisesRegex(builder.ManifestRefused, "disagrees"):
            self._build(spec)

    def test_truncated_nonverbose_and_zero_case_logs_are_rejected(self):
        cases = {
            "truncated.log": b"test_x (tests.synthetic.Case.test_x) ... ok\n",
            "nonverbose.log": b".\nRan 1 test in 0.001s\n\nOK\n",
            "zero.log": b"\nRan 0 tests in 0.001s\n\nOK\n",
        }
        patterns = {"truncated.log": "exactly one", "nonverbose.log": "non-verbose",
                    "zero.log": "zero-case"}
        for name, raw in cases.items():
            with self.subTest(name=name):
                (self.logs / name).write_bytes(raw)
                spec = copy.deepcopy(self.spec)
                spec["runs"][0]["log"] = name
                with self.assertRaisesRegex(builder.ManifestRefused, patterns[name]):
                    self._build(spec)

    def test_summary_counts_must_match_verbose_rows(self):
        test_id = "tests.synthetic.Case.test_skipped"
        (self.logs / "mismatch.log").write_bytes(_log(
            [f"test_skipped ({test_id}) ... skipped 'optional'"], 1, "OK"))
        spec = copy.deepcopy(self.spec)
        spec["runs"][0].update({"log": "mismatch.log", "complete_suite": False,
                                "kind": "targeted"})
        with self.assertRaisesRegex(builder.ManifestRefused, "disagrees"):
            self._build(spec)

    def test_python39_short_parenthetical_ids_normalise_without_hiding_mismatch(self):
        raw = _log(
            ["test_value (tests.synthetic.CompatibilityCase) ... ok"],
            1, "OK")
        parsed = builder.parse_unittest_log(raw, "python39.log")
        self.assertEqual(
            {"tests.synthetic.CompatibilityCase.test_value": "pass"},
            parsed["results"],
        )

        mismatched = _log(
            ["test_value (tests.synthetic.CompatibilityCase.test_other) ... ok"],
            1, "OK")
        with self.assertRaisesRegex(builder.ManifestRefused, "inconsistent verbose test id"):
            builder.parse_unittest_log(mismatched, "mismatch.log")

    def test_multiline_and_interleaved_verbose_results_are_complete_and_fail_closed(self):
        raw = (
            "test_documented (tests.synthetic.RuntimeCase.test_documented)\n"
            "A short description. ... ok\n"
            "test_noisy (tests.synthetic.RuntimeCase)\n"
            "Captured output is expected. ... pipeline output\n"
            "Traceback from an exercised negative fixture\n"
            "ok\n"
            "test_failure (tests.synthetic.RuntimeCase.test_failure) ... FAIL\n"
            "\n======================================================================\n"
            "FAIL: test_failure (tests.synthetic.RuntimeCase.test_failure)\n"
            "Traceback (most recent call last):\nAssertionError: exercised\n"
            "\n----------------------------------------------------------------------\n"
            "Ran 3 tests in 0.010s\n\nFAILED (failures=1)\n"
        ).encode("utf-8")
        parsed = builder.parse_unittest_log(raw, "multiline.log")
        self.assertEqual({
            "tests.synthetic.RuntimeCase.test_documented": "pass",
            "tests.synthetic.RuntimeCase.test_noisy": "pass",
            "tests.synthetic.RuntimeCase.test_failure": "fail",
        }, parsed["results"])
        self.assertEqual(3, parsed["counts"]["executed"])

        ambiguous = (
            "test_noisy (tests.synthetic.RuntimeCase.test_noisy) ... ok\n"
            "ok\n\nRan 1 test in 0.001s\n\nOK\n"
        ).encode("utf-8")
        with self.assertRaisesRegex(builder.ManifestRefused,
                                    "2 unambiguous per-test outcome"):
            builder.parse_unittest_log(ambiguous, "ambiguous.log")

        missing = (
            "test_noisy (tests.synthetic.RuntimeCase.test_noisy) ... output only\n"
            "\nRan 1 test in 0.001s\n\nOK\n"
        ).encode("utf-8")
        with self.assertRaisesRegex(builder.ManifestRefused,
                                    "0 unambiguous per-test outcome"):
            builder.parse_unittest_log(missing, "missing.log")

    def test_terminal_counts_encoding_and_trailing_material_are_strict(self):
        base = (
            "test_bad (tests.synthetic.RuntimeCase.test_bad) ... FAIL\n\n"
            "Ran 1 test in 0.001s\n\n{terminal}\n"
        )
        for terminal, pattern in (
                ("FAILED (failures=1, mystery=1)", "unknown or malformed"),
                ("FAILED (failures=1, failures=1)", "duplicate"),
                ("FAILED (failures=0)", "unknown or malformed"),
                ("OK (failures=1)", "terminal OK contradicts")):
            with self.subTest(terminal=terminal):
                with self.assertRaisesRegex(builder.ManifestRefused, pattern):
                    builder.parse_unittest_log(
                        base.format(terminal=terminal).encode("utf-8"), terminal)

        trailing = (base.format(terminal="FAILED (failures=1)")
                    + "unexpected trailing bytes\n").encode("utf-8")
        with self.assertRaisesRegex(builder.ManifestRefused,
                                    "nonblank material outside"):
            builder.parse_unittest_log(trailing, "trailing")

        with self.assertRaisesRegex(builder.ManifestRefused, "not valid UTF-8"):
            builder.parse_unittest_log(b"\xff", "invalid")

    def test_final_suite_spec_uses_package_qualified_discovery(self):
        """Mapped ``tests.*`` IDs must be the identities discovery emits."""
        package_marker = ROOT / "tests" / "__init__.py"
        self.assertTrue(package_marker.is_file(), package_marker)
        # The released handoff omits its mutable draft. Retain the actually
        # executed historical spec as a repository-contained regression fixture.
        final_spec = json.loads((ROOT / "tests" / "fixtures" / "release_audit" /
                                 "FINAL_TEST_RUN_SPEC_EXECUTED.json").read_text())
        complete = [row for row in final_spec["runs"] if row.get("complete_suite")]
        self.assertEqual(2, len(complete))
        for run in complete:
            command = run["command"]
            self.assertIn("discover", command)
            start = command.index("-s")
            top = command.index("-t")
            self.assertEqual("tests", command[start + 1])
            self.assertEqual(".", command[top + 1])
        mapped = [test_id for table in (final_spec["issue_test_evidence"],
                                        final_spec["exception_test_evidence"])
                  for tests in table.values() for test_id in tests]
        self.assertTrue(mapped)
        self.assertTrue(all(test_id.startswith("tests.") for test_id in mapped))

    def test_all_skip_and_xfail_results_need_optional_classification(self):
        skipped = "tests.synthetic.Case.test_optional"
        xfailed = "tests.synthetic.Case.test_expected"
        passed = "tests.synthetic.Case.test_valid"
        rows = [f"test_optional ({skipped}) ... skipped 'unavailable'",
                f"test_expected ({xfailed}) ... expected failure",
                f"test_valid ({passed}) ... ok"]
        (self.logs / "classified.log").write_bytes(_log(
            rows, 3, "OK (skipped=1, expected failures=1)"))
        spec = copy.deepcopy(self.spec)
        spec["runs"].append({
            "run_id": "classified-targeted", "acceptance_role": "acceptance",
            "log": "classified.log", "complete_suite": False, "kind": "targeted",
            "command": ["python3.14", "-B", "-m", "unittest", "-v"],
            "interpreter": "CPython 3.14.7", "exit_code": 0,
            "skip_details": [{"test": skipped, "classification": "external_optional",
                              "reason": "reference is optional", "required": False}],
            "xfail_details": [{"test": xfailed, "classification": "external_optional",
                               "reason": "reference is optional", "required": False}],
        })
        spec["issue_test_evidence"]["A01"] = [passed]
        spec["exception_test_evidence"]["blk-one"] = [passed]
        manifest = self._build(spec)
        self.assertEqual(1, manifest["runs"][1]["counts"]["skip"])
        self.assertEqual(1, manifest["runs"][1]["counts"]["xfail"])

        missing = copy.deepcopy(spec)
        missing["runs"][1]["skip_details"] = []
        with self.assertRaisesRegex(builder.ManifestRefused, "classify all 1 skip"):
            self._build(missing)
        required = copy.deepcopy(spec)
        required["runs"][1]["xfail_details"][0]["required"] = True
        with self.assertRaisesRegex(builder.ManifestRefused, "required capability"):
            self._build(required)

    def test_evidence_maps_require_unique_observed_acceptance_passes(self):
        for value, pattern in (({"A01": ["tests.none.Case.test_absent"]},
                                "not observed passing"),
                               ({"A01": [self.test_ids[0], self.test_ids[0]]},
                                "invalid or duplicate"),
                               ({"A01": []}, "at least one")):
            with self.subTest(value=value):
                spec = copy.deepcopy(self.spec)
                spec["issue_test_evidence"] = value
                with self.assertRaisesRegex(builder.ManifestRefused, pattern):
                    self._build(spec)

    def test_log_paths_cannot_escape_or_traverse_symlinks(self):
        spec = copy.deepcopy(self.spec)
        spec["runs"][0]["log"] = "../outside.log"
        with self.assertRaisesRegex(builder.ManifestRefused, "safe relative"):
            self._build(spec)
        link = self.logs / "linked.log"
        try:
            link.symlink_to(self.logs / "complete.log")
        except OSError:
            self.skipTest("symlinks unavailable")
        spec["runs"][0]["log"] = "linked.log"
        with self.assertRaisesRegex(builder.ManifestRefused, "symlink"):
            self._build(spec)


if __name__ == "__main__":
    unittest.main()
