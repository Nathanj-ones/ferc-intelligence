"""Focused regressions for the external clean-extraction Build-B runner."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import tempfile
import unittest
import zipfile

import exporters
from implementation import run_build_b_acceptance as build_b


class DatabaseSemanticComparisonTests(unittest.TestCase):
    @staticmethod
    def _single_row_database(path: pathlib.Path, table: str, ddl: str,
                             columns: tuple[str, ...], values: tuple) -> None:
        connection = sqlite3.connect(path)
        connection.execute(f"CREATE TABLE {table}({ddl})")
        marks = ",".join("?" for _ in columns)
        names = ",".join(columns)
        connection.execute(
            f"INSERT INTO {table}({names}) VALUES({marks})", values)
        connection.commit()
        connection.close()

    def _database(self, path: pathlib.Path, *, run_id: str, stamp: str,
                  scope: str = "whole legal entity", warning: str = "retained warning") -> None:
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE observations("
            "observation_id TEXT PRIMARY KEY,run_id TEXT,scope TEXT,qa_flags TEXT,"
            "first_seen_at TEXT,updated_at TEXT)")
        connection.execute(
            "INSERT INTO observations VALUES(?,?,?,?,?,?)",
            ("obs-1", run_id, scope, warning, stamp, stamp))
        connection.commit()
        connection.close()

    def test_only_explicit_run_identity_and_timestamps_are_normalised(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            left, right = root / "a.sqlite", root / "b.sqlite"
            self._database(left, run_id="run-a", stamp="2026-09-09T01:00:00Z")
            self._database(right, run_id="run-b", stamp="2026-09-10T09:00:00Z")
            a = build_b.database_snapshot(left, tables=("observations",))
            b = build_b.database_snapshot(right, tables=("observations",))
            result = build_b.compare_database_snapshots(
                a, b, "a", "b", tables=("observations",))
            self.assertTrue(result["match"])
            self.assertNotEqual(a["raw_identity"], b["raw_identity"])
            self.assertEqual(a["normalized_identity"], b["normalized_identity"])

    def test_scope_and_warning_changes_remain_blocking(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            left, right = root / "a.sqlite", root / "b.sqlite"
            self._database(left, run_id="run-a", stamp="a")
            self._database(right, run_id="run-b", stamp="b",
                           scope="guessed facility", warning="")
            a = build_b.database_snapshot(left, tables=("observations",))
            b = build_b.database_snapshot(right, tables=("observations",))
            result = build_b.compare_database_snapshots(
                a, b, "a", "b", tables=("observations",))
            self.assertFalse(result["match"])
            self.assertEqual("observations", result["mismatches"][0]["table"])

    def test_version_row_json_only_normalises_explicit_run_envelope(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            paths = [root / name for name in ("a.sqlite", "b.sqlite", "bad.sqlite")]
            for path, run_id, stamp, scope in (
                    (paths[0], "run-a", "2026-09-09T01:00:00Z", "system"),
                    (paths[1], "run-b", "2026-09-10T02:00:00Z", "system"),
                    (paths[2], "run-b", "2026-09-10T02:00:00Z", "guessed")):
                connection = sqlite3.connect(path)
                connection.execute(
                    "CREATE TABLE observation_versions("
                    "observation_id TEXT,version_seq INTEGER,superseded_at TEXT,"
                    "superseded_by_run_id TEXT,row_json TEXT,"
                    "PRIMARY KEY(observation_id,version_seq))")
                row_json = json.dumps({
                    "run_id": run_id, "first_seen_at": stamp, "updated_at": stamp,
                    "scope": scope, "unit": "Dth", "value_text": "12",
                    "qa_flags": "retained warning"})
                connection.execute("INSERT INTO observation_versions VALUES(?,?,?,?,?)", (
                    "obs-1", 1, stamp, run_id, row_json))
                connection.commit()
                connection.close()
            snapshots = [build_b.database_snapshot(
                path, tables=("observation_versions",)) for path in paths]
            self.assertTrue(build_b.compare_database_snapshots(
                snapshots[0], snapshots[1], "a", "b",
                tables=("observation_versions",))["match"])
            self.assertFalse(build_b.compare_database_snapshots(
                snapshots[0], snapshots[2], "a", "bad",
                tables=("observation_versions",))["match"])

    def test_filing_source_capture_and_first_observation_times_are_exact(self):
        """R50: both timestamps come from frozen evidence and drive routing."""
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            paths = [root / name for name in
                     ("a.sqlite", "same.sqlite", "bad-first.sqlite",
                      "bad-retrieval.sqlite", "null-a.sqlite", "null-b.sqlite")]
            ddl = ("source_system TEXT,filing_id TEXT,retrieved_at TEXT,"
                   "first_seen_at TEXT,PRIMARY KEY(source_system,filing_id)")
            columns = ("source_system", "filing_id", "retrieved_at", "first_seen_at")
            rows = (
                ("eLibrary", "20260901-5001", "2026-09-07T11:00:00Z",
                 "2026-09-09T01:00:00Z"),
                ("eLibrary", "20260901-5001", "2026-09-07T11:00:00Z",
                 "2026-09-09T01:00:00Z"),
                ("eLibrary", "20260901-5001", "2026-09-07T11:00:00Z",
                 "2026-09-10T02:00:00Z"),
                ("eLibrary", "20260901-5001", "2026-09-08T12:00:00Z",
                 "2026-09-09T01:00:00Z"),
                ("eLibrary", "20260901-5001", None, None),
                ("eLibrary", "20260901-5001", None, None),
            )
            for path, row in zip(paths, rows):
                self._single_row_database(path, "filings", ddl, columns, row)
            snapshots = [build_b.database_snapshot(path, tables=("filings",))
                         for path in paths]
            self.assertTrue(build_b.compare_database_snapshots(
                snapshots[0], snapshots[1], "a", "same",
                tables=("filings",))["match"])
            self.assertFalse(build_b.compare_database_snapshots(
                snapshots[0], snapshots[2], "a", "bad-first",
                tables=("filings",))["match"],
                "a changed first-observed timestamp was normalised away")
            self.assertFalse(build_b.compare_database_snapshots(
                snapshots[0], snapshots[3], "a", "bad-retrieval",
                tables=("filings",))["match"],
                "a changed source retrieval timestamp was normalised away")
            self.assertTrue(build_b.compare_database_snapshots(
                snapshots[4], snapshots[5], "null-a", "null-b",
                tables=("filings",))["match"],
                "the same explicitly unknown timestamps should remain reproducible")
            self.assertFalse(build_b.compare_database_snapshots(
                snapshots[0], snapshots[4], "a", "null",
                tables=("filings",))["match"],
                "unknown source timestamps equalled known source timestamps")

    def test_document_retrieval_time_remains_exact(self):
        """R50: a document's captured-source timestamp is not run metadata."""
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            paths = [root / name for name in ("a.sqlite", "same.sqlite", "bad.sqlite")]
            ddl = "document_id TEXT PRIMARY KEY,retrieved_at TEXT,availability TEXT"
            columns = ("document_id", "retrieved_at", "availability")
            rows = (
                ("eLibrary|20260901-5001|listing", "2026-09-07T11:00:00Z", "retrieved"),
                ("eLibrary|20260901-5001|listing", "2026-09-07T11:00:00Z", "retrieved"),
                ("eLibrary|20260901-5001|listing", "2026-09-08T12:00:00Z", "retrieved"),
            )
            for path, row in zip(paths, rows):
                self._single_row_database(path, "documents", ddl, columns, row)
            snapshots = [build_b.database_snapshot(path, tables=("documents",))
                         for path in paths]
            self.assertTrue(build_b.compare_database_snapshots(
                snapshots[0], snapshots[1], "a", "same",
                tables=("documents",))["match"])
            self.assertFalse(build_b.compare_database_snapshots(
                snapshots[0], snapshots[2], "a", "bad",
                tables=("documents",))["match"])

    def test_blocker_lifecycle_values_may_vary_but_state_and_summary_do_not(self):
        """R51: times are operational, while blocker meaning and state are exact."""
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            paths = [root / name for name in
                     ("a.sqlite", "b.sqlite", "bad-summary.sqlite",
                      "bad-state.sqlite", "unresolved.sqlite")]
            ddl = ("blocker_id TEXT PRIMARY KEY,summary TEXT,kind TEXT,"
                   "human_decision_needed INTEGER,opened_at TEXT,resolved_at TEXT")
            columns = ("blocker_id", "summary", "kind", "human_decision_needed",
                       "opened_at", "resolved_at")
            rows = (
                ("blk-1", "official input unavailable", "source", 0,
                 "2026-09-09T01:00:00Z", "2026-09-09T01:01:00Z"),
                ("blk-1", "official input unavailable", "source", 0,
                 "2026-09-10T02:00:00Z", "2026-09-10T02:01:00Z"),
                ("blk-1", "different asserted condition", "source", 0,
                 "2026-09-10T02:00:00Z", "2026-09-10T02:01:00Z"),
                ("blk-1", "official input unavailable", "source", 1,
                 "2026-09-10T02:00:00Z", "2026-09-10T02:01:00Z"),
                ("blk-1", "official input unavailable", "source", 0,
                 "2026-09-10T02:00:00Z", None),
            )
            for path, row in zip(paths, rows):
                self._single_row_database(path, "blockers", ddl, columns, row)
            snapshots = [build_b.database_snapshot(path, tables=("blockers",))
                         for path in paths]
            self.assertTrue(build_b.compare_database_snapshots(
                snapshots[0], snapshots[1], "a", "b", tables=("blockers",))["match"])
            for index, label in ((2, "bad-summary"), (3, "bad-state"),
                                 (4, "unresolved")):
                self.assertFalse(build_b.compare_database_snapshots(
                    snapshots[0], snapshots[index], "a", label,
                    tables=("blockers",))["match"])


class ExportSemanticComparisonTests(unittest.TestCase):
    @staticmethod
    def _csv(path: pathlib.Path, header: str, row: str) -> None:
        path.write_text(header + "\n" + row + "\n", encoding="utf-8")

    def test_event_timestamp_is_allowed_but_headline_is_not(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            paths = [root / name for name in ("a.csv", "b.csv", "bad.csv")]
            header = "event_id,headline,first_seen_at\n"
            paths[0].write_text(header + "evt-1,Supported,2026-09-09T01:00:00Z\n")
            paths[1].write_text(header + "evt-1,Supported,2026-09-10T02:00:00Z\n")
            paths[2].write_text(header + "evt-1,Changed,2026-09-10T02:00:00Z\n")
            first = build_b._normalised_csv(paths[0], ("first_seen_at",))
            second = build_b._normalised_csv(paths[1], ("first_seen_at",))
            bad = build_b._normalised_csv(paths[2], ("first_seen_at",))
            self.assertEqual(first["normalized_sha256"], second["normalized_sha256"])
            self.assertNotEqual(first["normalized_sha256"], bad["normalized_sha256"])

    def test_filing_export_keeps_discovery_and_source_retrieval_times_exact(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            paths = [root / name for name in
                     ("a.csv", "same.csv", "bad-first.csv", "bad-retrieval.csv",
                      "null-a.csv", "null-b.csv")]
            header = "filing_id,retrieved_at,first_seen_at"
            self._csv(paths[0], header,
                      "F1,2026-09-07T11:00:00Z,2026-09-09T01:00:00Z")
            self._csv(paths[1], header,
                      "F1,2026-09-07T11:00:00Z,2026-09-09T01:00:00Z")
            self._csv(paths[2], header,
                      "F1,2026-09-07T11:00:00Z,2026-09-10T02:00:00Z")
            self._csv(paths[3], header,
                      "F1,2026-09-08T12:00:00Z,2026-09-09T01:00:00Z")
            self._csv(paths[4], header, "F1,,")
            self._csv(paths[5], header, "F1,,")
            values = [build_b._normalised_export(path, "filing_inventory.csv")
                      for path in paths]
            self.assertEqual(values[0]["normalized_sha256"],
                             values[1]["normalized_sha256"])
            self.assertNotEqual(values[0]["normalized_sha256"],
                                values[2]["normalized_sha256"])
            self.assertNotEqual(values[0]["normalized_sha256"],
                                values[3]["normalized_sha256"])
            self.assertEqual(values[4]["normalized_sha256"],
                             values[5]["normalized_sha256"])
            self.assertNotEqual(values[0]["normalized_sha256"],
                                values[4]["normalized_sha256"])

    def test_document_export_retrieval_time_is_not_allowlisted(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            paths = [root / name for name in ("a.csv", "bad.csv")]
            header = "document_id,retrieved_at,availability"
            self._csv(paths[0], header, "D1,2026-09-07T11:00:00Z,retrieved")
            self._csv(paths[1], header, "D1,2026-09-08T12:00:00Z,retrieved")
            values = [build_b._normalised_export(path, "documents.csv")
                      for path in paths]
            self.assertNotEqual(values[0]["normalized_sha256"],
                                values[1]["normalized_sha256"])

    def test_blocker_export_normalises_times_but_not_meaning_or_resolution_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            paths = [root / name for name in
                     ("a.csv", "b.csv", "bad.csv", "unresolved.csv")]
            header = "blocker_id,summary,human_decision_needed,opened_at,resolved_at"
            self._csv(paths[0], header,
                      "B1,official input unavailable,0,2026-09-09T01:00:00Z,"
                      "2026-09-09T01:01:00Z")
            self._csv(paths[1], header,
                      "B1,official input unavailable,0,2026-09-10T02:00:00Z,"
                      "2026-09-10T02:01:00Z")
            self._csv(paths[2], header,
                      "B1,different asserted condition,1,2026-09-10T02:00:00Z,"
                      "2026-09-10T02:01:00Z")
            self._csv(paths[3], header,
                      "B1,official input unavailable,0,2026-09-10T02:00:00Z,")
            values = [build_b._normalised_export(path, "blockers.csv")
                      for path in paths]
            self.assertEqual(values[0]["normalized_sha256"],
                             values[1]["normalized_sha256"])
            self.assertNotEqual(values[0]["normalized_sha256"],
                                values[2]["normalized_sha256"])
            self.assertNotEqual(values[0]["normalized_sha256"],
                                values[3]["normalized_sha256"])

    def test_coverage_export_normalises_measurement_time_but_not_outcome(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            paths = [root / name for name in ("a.csv", "b.csv", "bad.csv")]
            header = "slot_id,outcome,populated,measured_at"
            self._csv(paths[0], header,
                      "S1,populated_validated,1,2026-09-09T01:00:00Z")
            self._csv(paths[1], header,
                      "S1,populated_validated,1,2026-09-10T02:00:00Z")
            self._csv(paths[2], header,
                      "S1,not_implemented,0,2026-09-10T02:00:00Z")
            values = [build_b._normalised_export(path, "coverage_by_slot.csv")
                      for path in paths]
            self.assertEqual(values[0]["normalized_sha256"],
                             values[1]["normalized_sha256"])
            self.assertNotEqual(values[0]["normalized_sha256"],
                                values[2]["normalized_sha256"])


class EventExportOrderingTests(unittest.TestCase):
    class _Staging:
        def __init__(self, first_seen: dict[str, str]):
            self.connection = sqlite3.connect(":memory:")
            self.connection.row_factory = sqlite3.Row
            self.connection.execute(
                "CREATE TABLE events(event_id TEXT PRIMARY KEY,source_filed_date TEXT,"
                "reporting_date TEXT,headline TEXT,first_seen_at TEXT)")
            self.connection.executemany(
                "INSERT INTO events VALUES(?,?,?,?,?)", [
                    ("evt-a", "2026-09-01", "2026-09-01", "A", first_seen["evt-a"]),
                    ("evt-b", "2026-09-02", "2026-09-02", "B", first_seen["evt-b"]),
                ])

        def query(self, sql, params=()):
            if "FROM events" in sql:
                return self.connection.execute(sql, params).fetchall()
            return []

        def close(self):
            self.connection.close()

    class _Cache:
        @staticmethod
        def manifest_rows():
            return []

    class _Context:
        def __init__(self, first_seen):
            self.staging = EventExportOrderingTests._Staging(first_seen)
            self.cache = EventExportOrderingTests._Cache()

    def test_event_export_order_does_not_depend_on_volatile_discovery_time(self):
        """R51: normalising a column cannot repair rows sorted by that column."""
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            first = self._Context({
                "evt-a": "2026-09-10T02:00:00Z",
                "evt-b": "2026-09-09T01:00:00Z",
            })
            second = self._Context({
                "evt-a": "2026-09-09T01:00:00Z",
                "evt-b": "2026-09-10T02:00:00Z",
            })
            try:
                exporters.write_all(first, root / "a")
                exporters.write_all(second, root / "b")
            finally:
                first.staging.close()
                second.staging.close()
            left = build_b._normalised_export(root / "a" / "events.csv", "events.csv")
            right = build_b._normalised_export(root / "b" / "events.csv", "events.csv")
            self.assertEqual(left["rows"], 2)
            self.assertEqual(left["normalized_sha256"], right["normalized_sha256"])


class ComparisonEvidenceRetentionTests(unittest.TestCase):
    def test_failed_comparison_is_recorded_before_the_gate_refuses(self):
        """R50: a refusal must retain the exact mismatches that caused it."""
        record = {"comparisons": {}}
        result = {
            "match": False,
            "reference": "build_a",
            "candidate": "build_b",
            "mismatches": [{
                "table": "documents",
                "field": "normalized_sha256",
                "build_a": "a" * 64,
                "build_b": "b" * 64,
            }],
        }
        with self.assertRaisesRegex(
                build_b.BuildBRefused, "semantic database records differ"):
            build_b._require_recorded_comparison(
                record, "database_build_a_build_b", result,
                "Build A and Build B semantic database records differ")
        self.assertIs(
            result, record["comparisons"]["database_build_a_build_b"])
        self.assertEqual(
            "documents",
            record["comparisons"]["database_build_a_build_b"]
            ["mismatches"][0]["table"])


class ArchiveBindingTests(unittest.TestCase):
    def _fixture(self, root: pathlib.Path):
        candidate = root / build_b.PACKAGE
        candidate.mkdir()
        body = b"immutable input\n"
        (candidate / "input.txt").write_bytes(body)
        entries = {
            "input.txt": {"sha256": build_b._sha_bytes(body), "bytes": len(body),
                          "class": "frozen_evidence_input"}}
        manifest = {"package": build_b.PACKAGE, "payload": "full",
                    "identity_is_content_derived": True,
                    "files": entries, "file_count": 1,
                    "payload_digest": build_b._payload_digest(entries)}
        manifest_bytes = build_b._json_bytes(manifest)
        (candidate / build_b.MANIFEST).write_bytes(manifest_bytes)
        archive = root / "candidate.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr(f"{build_b.PACKAGE}/input.txt", body)
            zipped.writestr(f"{build_b.PACKAGE}/{build_b.MANIFEST}", manifest_bytes)
        return candidate, archive

    def test_archive_and_clean_extraction_must_both_match_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            candidate, archive = self._fixture(root)
            result = build_b.verify_archive_and_extraction(archive, candidate)
            self.assertEqual(1, result["verified_payload_files"])
            (candidate / "input.txt").write_bytes(b"changed bytes\n")
            with self.assertRaisesRegex(build_b.BuildBRefused, "clean extraction"):
                build_b.verify_archive_and_extraction(archive, candidate)


class AcceptanceMirrorDependencyTests(unittest.TestCase):
    def _candidate(self, root: pathlib.Path, *, include_regressions: bool):
        candidate = root / "candidate"
        files = {
            "acceptance/run_acceptance.py": b"# acceptance\n",
            "ferclib/schema.sql": b"-- schema\n",
            "run.py": b"# runner\n",
        }
        if include_regressions:
            files["run_regressions.py"] = b"# exact comparison dependency\n"
        entries = {}
        for relative, body in files.items():
            path = candidate / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            entries[relative] = {
                "sha256": build_b._sha_bytes(body), "bytes": len(body),
                "class": "engine_script",
            }
        return candidate, {"files": entries}

    def test_mirror_copies_the_a12_regression_dependency(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            candidate, manifest = self._candidate(
                root, include_regressions=True)
            mirror = root / "mirror"
            result = build_b.materialize_acceptance_mirror(
                candidate, mirror, manifest)
            self.assertTrue((mirror / "run_regressions.py").is_file())
            self.assertEqual(4, result["files"])

    def test_missing_a12_regression_dependency_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            candidate, manifest = self._candidate(
                root, include_regressions=False)
            with self.assertRaisesRegex(
                    build_b.BuildBRefused, "run_regressions.py"):
                build_b.materialize_acceptance_mirror(
                    candidate, root / "mirror", manifest)


class PlanAndAcceptancePolicyTests(unittest.TestCase):
    def _plan(self, root: pathlib.Path, argv):
        path = root / "config" / "run_plan.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema": "ferc_operating_assets_run_plan_v2", "cache_only": True,
            "as_of": "2026-09-07", "plan_id": "fixture", "plan_version": "1",
            "steps": [{"id": "one", "argv": argv, "inputs": [], "outputs": [{}]}],
        }))

    def test_plan_refuses_online_or_external_path_commands(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._plan(root, ["run.py", "validate"])
            with self.assertRaisesRegex(build_b.BuildBRefused, "--offline"):
                build_b.verify_plan_policy(root)
            self._plan(root, ["python3", "/outside/tool.py"])
            with self.assertRaisesRegex(build_b.BuildBRefused, "external path"):
                build_b.verify_plan_policy(root)

    def _acceptance(self, path: pathlib.Path, worker=None, integrated=None,
                    external=None):
        keys = ("PASS", "FAIL", "ERROR", "SKIPPED", "XFAIL", "XPASS")
        worker = worker or {"PASS": 2}
        integrated = integrated or {"PASS": 3}
        external = external or {"SKIPPED": 1}
        def full(value):
            return {key: int(value.get(key, 0)) for key in keys}
        tiers = {"worker": {"totals": full(worker)},
                 "integrated": {"totals": full(integrated)},
                 "external_optional": {"totals": full(external)}}
        combined = {key: sum(item["totals"][key] for item in tiers.values())
                    for key in keys}
        path.write_text(json.dumps({"combined": {"totals": combined},
                                    "by_tier_report": tiers}))

    def test_external_skip_is_qualified_but_mandatory_skip_or_xfail_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            report = root / "acceptance.json"
            self._acceptance(report)
            self.assertTrue(build_b.acceptance_result(report, 0)["pass"])
            self._acceptance(report, integrated={"PASS": 2, "SKIPPED": 1})
            self.assertFalse(build_b.acceptance_result(report, 0)["pass"])
            self._acceptance(report, worker={"PASS": 1, "XFAIL": 1})
            self.assertFalse(build_b.acceptance_result(report, 0)["pass"])
            self._acceptance(report, worker={"PASS": 0})
            self.assertFalse(build_b.acceptance_result(report, 0)["pass"])


class ReplayLedgerPolicyTests(unittest.TestCase):
    def _write_ledger(self, path: pathlib.Path, rows):
        # Canonical JSON sorts object keys, as the production replay writer
        # does.  Acceptance must use explicit ordinals rather than key order.
        path.write_text(json.dumps({"steps": dict(rows)}, sort_keys=True))

    def test_canonical_key_sort_does_not_reject_completed_plan_order(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "replay.json"
            self._write_ledger(path, [
                ("z_first", {"step": 1, "status": "complete"}),
                ("a_second", {"step": 2, "status": "complete"}),
            ])
            result = build_b.replay_result(
                path, {"step_ids": ["z_first", "a_second"]}, 0)
            self.assertTrue(result["pass"])
            self.assertEqual(["z_first", "a_second"],
                             result["actual_steps_by_ordinal"])
            self.assertEqual([1, 2], result["ordinals"])

    def test_wrong_or_duplicate_ordinals_remain_blocking(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "replay.json"
            plan = {"step_ids": ["first", "second"]}
            self._write_ledger(path, [
                ("first", {"step": 2, "status": "complete"}),
                ("second", {"step": 1, "status": "complete"}),
            ])
            self.assertFalse(build_b.replay_result(path, plan, 0)["pass"])
            self._write_ledger(path, [
                ("first", {"step": 1, "status": "complete"}),
                ("second", {"step": 1, "status": "complete"}),
            ])
            self.assertFalse(build_b.replay_result(path, plan, 0)["pass"])

    def test_nonzero_exit_or_incomplete_step_remains_blocking(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "replay.json"
            plan = {"step_ids": ["first"]}
            self._write_ledger(
                path, [("first", {"step": 1, "status": "failed"})])
            self.assertFalse(build_b.replay_result(path, plan, 0)["pass"])
            self._write_ledger(
                path, [("first", {"step": 1, "status": "complete"})])
            self.assertFalse(build_b.replay_result(path, plan, 1)["pass"])


class IsolationProfileTests(unittest.TestCase):
    def test_guard_probe_denies_network_candidate_writes_and_hidden_reads(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            candidate, build_a = root / "candidate", root / "build_a"
            work, mirror = root / "work", root / "mirror"
            logs, output = work / "logs", work / "output"
            for path in (candidate / "staging", candidate / "exports",
                         build_a / "staging", work, mirror, logs, output):
                path.mkdir(parents=True, exist_ok=True)
            (candidate / "staging" / "operating_assets.sqlite").write_bytes(b"package")
            (candidate / "exports" / "answer.csv").write_text("answer\n")
            (build_a / "staging" / "operating_assets.sqlite").write_bytes(b"build-a")
            home, temporary = work / "home", work / "tmp"
            home.mkdir()
            temporary.mkdir()
            python = pathlib.Path(__import__("sys").executable).resolve()
            guard = build_b.install_python_guard(
                candidate, build_a, work, mirror, [], python)
            env = build_b._command_environment(
                candidate, output, home, temporary, guard, python)
            result = build_b.run_command(
                "probe", [str(guard["probe"])], guard["root"], env, guard, logs, 30)
            self.assertEqual(0, result["returncode"])
            nested = build_b.run_command(
                "nested", ["-I", "-S", "-c", "print('GUARDED_NESTED_OK')"],
                guard["root"], env, guard, logs, 30)
            self.assertEqual(0, nested["returncode"])
            self.assertFalse((candidate / ".build-b-write-probe").exists())
            self.assertGreaterEqual(len(list(guard["markers"].iterdir())), 2)


if __name__ == "__main__":
    unittest.main()
