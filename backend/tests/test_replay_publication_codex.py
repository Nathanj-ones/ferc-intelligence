"""Regression coverage for replay resume and the consumer publication boundary.

These tests exercise the production functions with isolated files only.  Bad
states are paired with valid controls so a control that rejects every run cannot
appear correct.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
import tempfile
import types
import unittest
from unittest import mock

import exporters
import ferclib.frontend_exports as frontend_exports
import ferclib.publication as publication
import run as pipeline
import validate


class ReplayIdentityTests(unittest.TestCase):
    def _plan(self):
        return {
            "steps": [{
                "id": "produce",
                "argv": ["python3", "producer.py"],
                "inputs": ["@output/input.txt"],
                "depends_on": [],
                "outputs": [{"name": "product", "path": "@output/output.txt"}],
            }]
        }

    def test_resume_skips_only_while_input_and_output_identities_hold(self):
        with tempfile.TemporaryDirectory(prefix="ferc-replay-test-") as td:
            root = pathlib.Path(td)
            verification = root / "verification"
            (root / "input.txt").write_text("input-v1", encoding="utf-8")
            calls = []

            def execute(_argv, **_kwargs):
                calls.append(1)
                body = (root / "input.txt").read_text(encoding="utf-8")
                (root / "output.txt").write_text("built:" + body, encoding="utf-8")
                return types.SimpleNamespace(returncode=0)

            args = types.SimpleNamespace(resume_plan=True)
            with mock.patch.object(pipeline, "OUTPUT_DIR", root), \
                    mock.patch.object(pipeline, "VERIFICATION", verification), \
                    mock.patch.object(pipeline, "cmd_plan", return_value=0), \
                    mock.patch.object(pipeline, "_load_plan", return_value=(self._plan(), "p1")), \
                    mock.patch.object(pipeline.subprocess, "run", side_effect=execute):
                self.assertEqual(0, pipeline.cmd_replay(None, args))
                first = json.loads(
                    (verification / "replay_step_status.json").read_text())
                recorded = first["steps"]["produce"]
                self.assertEqual(
                    "input.txt", recorded["input_identity"]["inputs"][0]["path"])
                self.assertEqual("output.txt", recorded["outputs"][0]["path"])
                self.assertNotIn(
                    str(root), json.dumps(recorded),
                    "replay identities must not retain the disposable build path")
                self.assertEqual(0, pipeline.cmd_replay(None, args))
                self.assertEqual(1, len(calls), "verified replay output should be skipped")

                (root / "output.txt").write_text("tampered", encoding="utf-8")
                self.assertEqual(0, pipeline.cmd_replay(None, args))
                self.assertEqual(2, len(calls), "stale output must be rebuilt")

                (root / "input.txt").write_text("input-v2", encoding="utf-8")
                self.assertEqual(0, pipeline.cmd_replay(None, args))
                self.assertEqual(3, len(calls), "changed input must invalidate resume")
                self.assertEqual("built:input-v2", (root / "output.txt").read_text())

    def test_keyboard_interrupt_is_durable_and_not_reported_complete(self):
        with tempfile.TemporaryDirectory(prefix="ferc-replay-interrupt-") as td:
            root = pathlib.Path(td)
            (root / "input.txt").write_text("input", encoding="utf-8")
            args = types.SimpleNamespace(resume_plan=False)
            with mock.patch.object(pipeline, "OUTPUT_DIR", root), \
                    mock.patch.object(pipeline, "VERIFICATION", root / "verification"), \
                    mock.patch.object(pipeline, "cmd_plan", return_value=0), \
                    mock.patch.object(pipeline, "_load_plan", return_value=(self._plan(), "p1")), \
                    mock.patch.object(pipeline.subprocess, "run", side_effect=KeyboardInterrupt):
                self.assertEqual(130, pipeline.cmd_replay(None, args))
            saved = json.loads((root / "verification/replay_step_status.json").read_text())
            self.assertEqual("interrupted", saved["steps"]["produce"]["status"])
            self.assertEqual(130, saved["steps"]["produce"]["exit_code"])

    def test_sqlite_output_validation_failure_is_durable_and_reruns(self):
        with tempfile.TemporaryDirectory(prefix="ferc-replay-output-failure-") as td:
            root = pathlib.Path(td)
            (root / "input.txt").write_text("input", encoding="utf-8")
            calls = []

            def execute(_argv, **_kwargs):
                calls.append(1)
                return types.SimpleNamespace(returncode=0)

            identities = [
                sqlite3.OperationalError("declared output uses a missing column"),
                [{"name": "product", "sha256": "a" * 64}],
            ]
            args = types.SimpleNamespace(resume_plan=True)
            with mock.patch.object(pipeline, "OUTPUT_DIR", root), \
                    mock.patch.object(pipeline, "VERIFICATION", root / "verification"), \
                    mock.patch.object(pipeline, "cmd_plan", return_value=0), \
                    mock.patch.object(pipeline, "_load_plan", return_value=(self._plan(), "p1")), \
                    mock.patch.object(pipeline, "_step_output_identities",
                                      side_effect=identities), \
                    mock.patch.object(pipeline.subprocess, "run", side_effect=execute):
                self.assertEqual(2, pipeline.cmd_replay(None, args))
                failed = json.loads(
                    (root / "verification/replay_step_status.json").read_text())
                self.assertEqual(
                    "failed_output_validation", failed["steps"]["produce"]["status"])
                self.assertIn("OperationalError", failed["steps"]["produce"]["error"])

                self.assertEqual(0, pipeline.cmd_replay(None, args))
                self.assertEqual(2, len(calls), "failed validation must rerun producer")
                recovered = json.loads(
                    (root / "verification/replay_step_status.json").read_text())
                self.assertEqual("complete", recovered["steps"]["produce"]["status"])

    def test_real_sqlite_wrong_column_reruns_then_verified_resume_skips(self):
        with tempfile.TemporaryDirectory(prefix="ferc-replay-real-sqlite-") as td:
            root = pathlib.Path(td)
            db = root / "state.sqlite"
            (root / "input.txt").write_text("input", encoding="utf-8")
            plan = self._plan()
            plan["steps"][0]["outputs"] = [{
                "name": "schema-bound output",
                "sqlite_query": {
                    "database": "@staging_db",
                    "sql": "SELECT required_value FROM product",
                    "minimum_rows": 1,
                },
            }]
            calls = []

            def execute(_argv, **_kwargs):
                calls.append(1)
                con = sqlite3.connect(db)
                try:
                    if len(calls) == 1:
                        con.execute("CREATE TABLE product(actual_value TEXT)")
                        con.execute("INSERT INTO product VALUES ('first')")
                    else:
                        con.execute("ALTER TABLE product ADD COLUMN required_value TEXT")
                        con.execute("UPDATE product SET required_value='second'")
                    con.commit()
                finally:
                    con.close()
                return types.SimpleNamespace(returncode=0)

            args = types.SimpleNamespace(resume_plan=True)
            with mock.patch.object(pipeline, "STAGING_DB", db), \
                    mock.patch.object(pipeline, "OUTPUT_DIR", root), \
                    mock.patch.object(pipeline, "VERIFICATION", root / "verification"), \
                    mock.patch.object(pipeline, "cmd_plan", return_value=0), \
                    mock.patch.object(pipeline, "_load_plan", return_value=(plan, "p1")), \
                    mock.patch.object(pipeline, "_code_snapshot", return_value="c" * 64), \
                    mock.patch.object(pipeline.subprocess, "run", side_effect=execute):
                self.assertEqual(2, pipeline.cmd_replay(None, args))
                failed = json.loads(
                    (root / "verification/replay_step_status.json").read_text())
                self.assertEqual(
                    "failed_output_validation", failed["steps"]["produce"]["status"])
                self.assertEqual(0, failed["steps"]["produce"]["exit_code"])
                self.assertIn("OperationalError", failed["steps"]["produce"]["error"])

                self.assertEqual(0, pipeline.cmd_replay(None, args))
                self.assertEqual(0, pipeline.cmd_replay(None, args))
                self.assertEqual(2, len(calls))

    def test_interruptions_during_post_command_validation_are_durable(self):
        for raised, expected_status, expected_exit in (
                (KeyboardInterrupt(), "interrupted", 130),
                (SystemExit(0), "terminated", 2),
                (SystemExit(7), "terminated", 7)):
            with self.subTest(raised=type(raised).__name__, code=getattr(raised, "code", None)), \
                    tempfile.TemporaryDirectory(prefix="ferc-replay-validation-stop-") as td:
                root = pathlib.Path(td)
                (root / "input.txt").write_text("input", encoding="utf-8")
                args = types.SimpleNamespace(resume_plan=False)
                with mock.patch.object(pipeline, "OUTPUT_DIR", root), \
                        mock.patch.object(pipeline, "VERIFICATION", root / "verification"), \
                        mock.patch.object(pipeline, "cmd_plan", return_value=0), \
                        mock.patch.object(pipeline, "_load_plan",
                                          return_value=(self._plan(), "p1")), \
                        mock.patch.object(pipeline, "_code_snapshot", return_value="c" * 64), \
                        mock.patch.object(pipeline.subprocess, "run",
                                          return_value=types.SimpleNamespace(returncode=0)), \
                        mock.patch.object(pipeline, "_step_output_identities",
                                          side_effect=raised):
                    self.assertEqual(expected_exit, pipeline.cmd_replay(None, args))
                saved = json.loads(
                    (root / "verification/replay_step_status.json").read_text())
                self.assertEqual(expected_status, saved["steps"]["produce"]["status"])
                self.assertEqual(expected_exit, saved["steps"]["produce"]["exit_code"])
                self.assertIn("finished_at", saved["steps"]["produce"])

    def test_in_process_exception_is_durable_then_reruns(self):
        """An exception from a run.py plan step must not strand `running`."""
        with tempfile.TemporaryDirectory(prefix="ferc-replay-command-failure-") as td:
            root = pathlib.Path(td)
            (root / "input.txt").write_text("input", encoding="utf-8")
            plan = self._plan()
            plan["steps"][0]["argv"] = ["run.py", "coverage"]
            args = types.SimpleNamespace(resume_plan=True)
            with mock.patch.object(pipeline, "OUTPUT_DIR", root), \
                    mock.patch.object(pipeline, "VERIFICATION", root / "verification"), \
                    mock.patch.object(pipeline, "cmd_plan", return_value=0), \
                    mock.patch.object(pipeline, "_load_plan", return_value=(plan, "p1")), \
                    mock.patch.object(pipeline, "_code_snapshot", return_value="c" * 64), \
                    mock.patch.object(pipeline, "main",
                                      side_effect=[ValueError("bad coverage input"), 0]), \
                    mock.patch.object(pipeline, "_step_output_identities",
                                      return_value=[{"name": "product", "sha256": "a" * 64}]):
                self.assertEqual(2, pipeline.cmd_replay(None, args))
                failed = json.loads(
                    (root / "verification/replay_step_status.json").read_text())
                self.assertEqual("failed", failed["steps"]["produce"]["status"])
                self.assertEqual(2, failed["steps"]["produce"]["exit_code"])
                self.assertIn("ValueError: bad coverage input",
                              failed["steps"]["produce"]["error"])
                self.assertEqual(0, pipeline.cmd_replay(None, args))

    def test_in_process_interrupt_and_termination_are_durable(self):
        for raised, expected_status, expected_exit in (
                (KeyboardInterrupt(), "interrupted", 130),
                (SystemExit(0), "terminated", 2),
                (SystemExit(7), "terminated", 7)):
            with self.subTest(raised=type(raised).__name__, code=getattr(raised, "code", None)), \
                    tempfile.TemporaryDirectory(prefix="ferc-replay-command-stop-") as td:
                root = pathlib.Path(td)
                (root / "input.txt").write_text("input", encoding="utf-8")
                plan = self._plan()
                plan["steps"][0]["argv"] = ["run.py", "coverage"]
                args = types.SimpleNamespace(resume_plan=False)
                with mock.patch.object(pipeline, "OUTPUT_DIR", root), \
                        mock.patch.object(pipeline, "VERIFICATION", root / "verification"), \
                        mock.patch.object(pipeline, "cmd_plan", return_value=0), \
                        mock.patch.object(pipeline, "_load_plan", return_value=(plan, "p1")), \
                        mock.patch.object(pipeline, "_code_snapshot", return_value="c" * 64), \
                        mock.patch.object(pipeline, "main", side_effect=raised):
                    self.assertEqual(expected_exit, pipeline.cmd_replay(None, args))
                saved = json.loads(
                    (root / "verification/replay_step_status.json").read_text())
                self.assertEqual(expected_status, saved["steps"]["produce"]["status"])
                self.assertEqual(expected_exit, saved["steps"]["produce"]["exit_code"])
                self.assertIn("error", saved["steps"]["produce"])


class CoverageAsOfSemanticsTests(unittest.TestCase):
    class _Staging:
        def query(self, sql, params=()):
            if "FROM filings" in sql:
                return [
                    {"source_system": "DataFERC", "filing_id": "549D-1",
                     "entity_key": "C000001", "form": "Form 549D",
                     "reporting_year": 2025, "reporting_period": "Q2",
                     "snapshot_date": "2026-09-07T06:36:03.232Z"},
                    {"source_system": "eLibrary IOC", "filing_id": "IOC-1",
                     "entity_key": "C000002", "form": "Form 549B IOC",
                     "reporting_year": 2025, "reporting_period": "Q3",
                     "snapshot_date": "2025-07-01"},
                ]
            if "FROM observations" in sql:
                return [
                    {"observation_id": "obs-549d", "entity_key": "C000001",
                     "source_regime": "Form 549D", "reporting_year": 2025,
                     "reporting_period": "Q2",
                     "instant_date": "2026-09-07T06:36:03.232Z",
                     "availability": "parse_failed", "missing_reason": "synthetic"},
                    {"observation_id": "obs-ioc", "entity_key": "C000002",
                     "source_regime": "Form 549B IOC", "reporting_year": 2025,
                     "reporting_period": "Q3", "instant_date": "2025-07-01",
                     "availability": "parse_failed", "missing_reason": "synthetic"},
                ]
            raise AssertionError(sql)

    def setUp(self):
        self.ctx = types.SimpleNamespace(staging=self._Staging())

    def test_only_snapshot_forms_supply_occurrence_as_of(self):
        from ferclib.obligations import OccurrenceEvidence

        rows = pipeline._coverage_occurrences(self.ctx)
        by_form = {row["form"]: row for row in rows}
        self.assertEqual("", by_form["Form 549D"]["as_of"])
        self.assertEqual("2025-07-01", by_form["Form 549B IOC"]["as_of"])
        for row in rows:
            OccurrenceEvidence.from_value(row)
        malformed = dict(by_form["Form 549B IOC"])
        malformed["as_of"] = "2025-07-01T12:00:00Z"
        with self.assertRaisesRegex(ValueError, "occurrence as_of"):
            OccurrenceEvidence.from_value(malformed)

    def test_only_snapshot_forms_supply_health_as_of(self):
        from ferclib.obligations import SourceHealthEvidence

        rows = pipeline._coverage_source_health(
            self.ctx, "2026-09-09T12:00:00+00:00")
        by_form = {row["form"]: row for row in rows}
        self.assertEqual("", by_form["Form 549D"]["as_of"])
        self.assertEqual("2025-07-01", by_form["Form 549B IOC"]["as_of"])
        for row in rows:
            SourceHealthEvidence.from_value(row)


class CachePreflightTests(unittest.TestCase):
    def test_valid_cache_passes_and_corrupt_object_fails(self):
        with tempfile.TemporaryDirectory(prefix="ferc-cache-test-") as td:
            root = pathlib.Path(td)
            body = b"official response"
            digest = hashlib.sha256(body).hexdigest()
            path = root / "objects" / digest[:2] / digest
            path.parent.mkdir(parents=True)
            path.write_bytes(body)
            url_key = hashlib.sha256(b"https://example.test/redacted").hexdigest()
            index = {url_key: {"content_hash": digest, "cache_path":
                               f"objects/{digest[:2]}/{digest}", "byte_size": len(body)}}
            (root / "index.json").write_text(json.dumps(index), encoding="utf-8")
            with mock.patch.object(pipeline, "SOURCE_CACHE", root):
                result = pipeline._verify_source_cache_index()
                self.assertEqual(1, result["unique_objects"])
                path.write_bytes(b"corrupt response")
                with self.assertRaisesRegex(ValueError, "size mismatch|hash mismatch"):
                    pipeline._verify_source_cache_index()

    def test_taxonomy_pin_preflight_is_strict_but_live_cache_may_append(self):
        with tempfile.TemporaryDirectory(prefix="ferc-taxonomy-pin-preflight-") as td:
            root = pathlib.Path(td)
            cache = root / "source_cache"
            url = "https://eCollection.ferc.gov/taxonomy/example.xsd"
            body = b"official taxonomy entry point"
            digest = hashlib.sha256(body).hexdigest()
            cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
            object_path = cache / "objects" / digest[:2] / digest
            object_path.parent.mkdir(parents=True)
            object_path.write_bytes(body)
            index = {
                cache_key: {
                    "source_url": url,
                    "content_hash": digest,
                    "cache_path": f"objects/{digest[:2]}/{digest}",
                    "byte_size": len(body),
                }
            }
            index_path = cache / "index.json"
            index_path.write_text(json.dumps(index), encoding="utf-8")
            pins_path = root / "taxonomy_pins.json"
            pins = {
                "schema": "ferc_taxonomy_pins_v1",
                "source_cache_index_sha256_at_freeze":
                    hashlib.sha256(index_path.read_bytes()).hexdigest(),
                "pins": [{
                    "form": "Form 2", "reporting_year": 2026,
                    "taxonomy_version": "2026-04-01",
                    "cache_key": cache_key,
                    "evidence_ref": f"{url}#sha256={digest}",
                }],
            }
            pins_path.write_text(json.dumps(pins), encoding="utf-8")
            with mock.patch.object(pipeline, "SOURCE_CACHE", cache), \
                    mock.patch.object(pipeline, "TAXONOMY_PINS", pins_path):
                self.assertEqual(1, len(pipeline._verify_taxonomy_pin_inputs()))
                index["f" * 64] = {
                    "source_url": "https://example.test/new-live-source",
                    "content_hash": digest,
                    "cache_path": f"objects/{digest[:2]}/{digest}",
                    "byte_size": len(body),
                }
                index_path.write_text(json.dumps(index), encoding="utf-8")
                with self.assertRaisesRegex(
                        pipeline.MissingRequiredInput,
                        "not frozen against this source-cache index"):
                    pipeline._verify_taxonomy_pin_inputs()
                self.assertEqual(1, len(pipeline._validated_taxonomy_pins(
                    index, require_frozen_index=False)))
                index[cache_key]["content_hash"] = "0" * 64
                with self.assertRaisesRegex(
                        pipeline.MissingRequiredInput,
                        "evidence does not resolve"):
                    pipeline._validated_taxonomy_pins(
                        index, require_frozen_index=False)

    def test_plan_preflight_reports_taxonomy_cache_mismatch_before_replay(self):
        plan = {
            "plan_id": "synthetic", "plan_version": "1", "as_of": "2026-09-07",
            "cache_only": True, "required_inputs": [], "optional_inputs": [],
            "windows": [],
            "steps": [{
                "id": "never-run", "argv": ["run.py", "coverage"],
                "depends_on": [], "inputs": [],
                "outputs": [{"name": "never", "path": "@output/never"}],
            }],
        }
        with mock.patch.object(pipeline, "_load_plan", return_value=(plan, "p" * 64)), \
                mock.patch.object(pipeline, "_verify_source_cache_index", return_value={
                    "url_entries": 1, "unique_objects": 1, "unique_bytes": 1,
                    "index_sha256": "i" * 64,
                }), \
                mock.patch.object(pipeline, "_verify_taxonomy_pin_inputs",
                                  side_effect=pipeline.MissingRequiredInput(
                                      "declared old, current new")), \
                mock.patch.object(pipeline, "_load_annotation_bundle",
                                  return_value=([], {"annotation_set_version": "test"})):
            self.assertEqual(2, pipeline.cmd_plan(None, types.SimpleNamespace(check=True)))


class PublicationTests(unittest.TestCase):
    def test_mid_publish_exception_restores_legacy_files_and_old_receipt(self):
        with tempfile.TemporaryDirectory(prefix="ferc-publication-test-") as td:
            root = pathlib.Path(td)
            receipt = root / "publication_receipt.json"
            targets = {"a.csv": root / "exports/a.csv", "b.csv": root / "exports/b.csv"}
            old = {"a.csv": b"old-a\n", "b.csv": b"old-b\n"}
            publication.publish_generation(root, receipt, "consumer", old,
                                           compatibility_targets=targets)
            old_receipt = receipt.read_bytes()
            real_replace = publication.os.replace
            failed = {"done": False}

            def fail_second(src, dst):
                if pathlib.Path(dst) == targets["b.csv"] and not failed["done"]:
                    failed["done"] = True
                    raise OSError("injected compatibility publication failure")
                return real_replace(src, dst)

            new = {"a.csv": b"new-a\n", "b.csv": b"new-b\n"}
            with mock.patch.object(publication.os, "replace", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "injected"):
                    publication.publish_generation(root, receipt, "consumer", new,
                                                   compatibility_targets=targets)
            self.assertEqual(old_receipt, receipt.read_bytes())
            self.assertEqual(old["a.csv"], targets["a.csv"].read_bytes())
            self.assertEqual(old["b.csv"], targets["b.csv"].read_bytes())
            self.assertEqual(old, {name: (root / manifest["generation_path"] / name).read_bytes()
                                   for name in old
                                   for manifest in [publication.verify_receipt(root, receipt)]})

    def test_valid_publish_moves_receipt_to_complete_new_generation(self):
        with tempfile.TemporaryDirectory(prefix="ferc-publication-valid-") as td:
            root = pathlib.Path(td)
            receipt = root / "publication_receipt.json"
            targets = {"a.csv": root / "exports/a.csv", "b.csv": root / "exports/b.csv"}
            files = {"a.csv": b"a\n", "b.csv": b"b\n"}
            manifest = publication.publish_generation(
                root, receipt, "consumer", files, metadata={"snapshot": "fixed"},
                compatibility_targets=targets)
            self.assertEqual(manifest["generation_id"],
                             publication.verify_receipt(root, receipt)["generation_id"])
            self.assertEqual(files, {name: target.read_bytes() for name, target in targets.items()})

    def test_receipt_cannot_relabel_or_diverge_from_embedded_manifest(self):
        with tempfile.TemporaryDirectory(prefix="ferc-publication-tamper-") as td:
            root = pathlib.Path(td)
            receipt = root / "publication_receipt.json"
            manifest = publication.publish_generation(
                root, receipt, "consumer", {"a.csv": b"a\n"}, metadata={"snapshot": "fixed"})
            tampered = dict(manifest, generation_id="f" * 64)
            receipt.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(publication.PublicationError,
                                        "does not bind|path does not match"):
                publication.verify_receipt(root, receipt)

            receipt.write_text(json.dumps(manifest), encoding="utf-8")
            embedded = root / manifest["generation_path"] / "GENERATION_MANIFEST.json"
            changed = dict(manifest, metadata={"snapshot": "changed"})
            embedded.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(publication.PublicationError,
                                        "receipt and embedded"):
                publication.verify_receipt(root, receipt)


class ReplayPublicationReceiptTests(unittest.TestCase):
    def test_output_identity_covers_receipt_generation_and_every_compatibility_file(self):
        with tempfile.TemporaryDirectory(prefix="ferc-replay-receipt-") as td:
            root = pathlib.Path(td)
            receipt = root / "publication_receipt.json"
            files = {"named.csv": b"named\n", "otherwise-unlisted.json": b"{}\n"}
            publication.publish_generation(
                root, receipt, "consumer", files,
                compatibility_targets={name: root / "exports" / name for name in files})
            step = {"outputs": [{
                "name": "consumer boundary",
                "publication_receipt": {
                    "base": "@output", "path": "@output/publication_receipt.json",
                    "compatibility_root": "@output/exports"},
            }]}
            with mock.patch.object(pipeline, "OUTPUT_DIR", root):
                identities = pipeline._step_output_identities(step)
                self.assertEqual(1, len(identities))
                self.assertEqual(set(files), {
                    item["logical_path"] for item in identities[0]["files"]})

                (root / "exports/otherwise-unlisted.json").unlink()
                with self.assertRaisesRegex(RuntimeError, "compatibility file absent"):
                    pipeline._step_output_identities(step)

                (root / "exports/otherwise-unlisted.json").write_bytes(
                    files["otherwise-unlisted.json"])
                generation = root / identities[0]["receipt"]["path"]
                self.assertTrue(generation.is_file())
                manifest = json.loads(receipt.read_text())
                (root / manifest["generation_path"] / "named.csv").unlink()
                with self.assertRaisesRegex(RuntimeError, "invalid publication receipt"):
                    pipeline._step_output_identities(step)


class MandatoryExportTests(unittest.TestCase):
    MANDATORY = {
        "canonical_observations.csv", "lineage_edges.csv", "filing_inventory.csv",
        "lineage_populations.csv", "observation_versions.csv", "entities.csv",
        "assets.csv", "asset_entity_map.csv", "ownership.csv", "asset_dockets.csv",
        "metric_registry.csv", "documents.csv", "document_facts.csv",
        "frontend_v1/contract.json", "frontend_v1/assets.json",
        "frontend_v1/instruments.json", "frontend_v1/headline_availability.json",
        "frontend_v1/source_index.json",
        "source_manifest.csv", "applicability.csv", "reviewed_source_annotations.csv",
        "coverage_by_slot.csv", "coverage_statistics.json", "coverage_groups.json",
        "field_status.csv", "field_status_summary.json", "blockers.csv",
    }

    class _Staging:
        run_id = "test-run"

        def __init__(self):
            self.publications = []

        def query(self, _sql, _params=()):
            if "FROM unit_commits" in _sql:
                return [{"adapter": "test", "entity_cid": "C1", "scope_key": "all",
                         "identity_json": "{}", "input_digest": "i" * 64,
                         "observation_count": 1, "edge_count": 0,
                         "document_fact_count": 0}]
            return [types.SimpleNamespace()]

        def record_publication(self, row):
            self.publications.append(dict(row))

    class _Context:
        def __init__(self):
            self.staging = MandatoryExportTests._Staging()
            self.as_of_iso = "2026-09-07"
            self.universe_path = pathlib.Path("fixture-universe.csv")
            self.messages = []

        def log(self, level, message):
            self.messages.append((level, message))

    @staticmethod
    def _write_names(out, names):
        out.mkdir(parents=True, exist_ok=True)
        for name in names:
            (out / name).parent.mkdir(parents=True, exist_ok=True)
            (out / name).write_bytes((name + "\n").encode())

    def _patch_helpers(self, root):
        return (mock.patch.object(pipeline, "OUTPUT_DIR", root),
                mock.patch.object(pipeline, "EXPORTS", root / "exports"),
                mock.patch.object(pipeline, "_snapshot_digest", return_value="c" * 64),
                mock.patch.object(pipeline, "_database_semantic_identity", return_value="d" * 64))

    def test_missing_required_export_is_rejected_before_staging(self):
        with tempfile.TemporaryDirectory(prefix="ferc-export-negative-") as td:
            root = pathlib.Path(td)
            ctx = self._Context()

            def incomplete(_ctx, out):
                self._write_names(out, {"blockers.csv"})
                return 1

            def coverage(_ctx, out):
                self._write_names(out, {"coverage_by_slot.csv", "coverage_statistics.json",
                                        "coverage_groups.json"})
                return {}

            def fields(_ctx, out):
                self._write_names(out, {"field_status.csv", "field_status_summary.json"})
                return {}

            patches = self._patch_helpers(root)
            with patches[0], patches[1], patches[2], patches[3], \
                    mock.patch.object(exporters, "write_all", side_effect=incomplete), \
                    mock.patch.object(frontend_exports, "write_frontend_contract",
                                      return_value={"files": 0}), \
                    mock.patch.object(pipeline, "_coverage_export_files", side_effect=coverage), \
                    mock.patch.object(pipeline, "_field_status_export_files", side_effect=fields), \
                    mock.patch.object(publication, "stage_generation") as stage:
                with self.assertRaisesRegex(RuntimeError, "mandatory export"):
                    pipeline.cmd_export(ctx, types.SimpleNamespace())
                stage.assert_not_called()

    def test_complete_required_export_reaches_publication_boundary(self):
        with tempfile.TemporaryDirectory(prefix="ferc-export-positive-") as td:
            root = pathlib.Path(td)
            ctx = self._Context()

            def complete(_ctx, out):
                self._write_names(out, self.MANDATORY)
                return 12

            manifest = {"generation_id": "g1", "kind": "consumer_exports",
                        "generation_path": ".generations/consumer_exports/g1",
                        "files": {}, "metadata": {}}
            patches = self._patch_helpers(root)
            with patches[0], patches[1], patches[2], patches[3], \
                    mock.patch.object(exporters, "write_all", side_effect=complete), \
                    mock.patch.object(frontend_exports, "write_frontend_contract",
                                      return_value={"files": 5}), \
                    mock.patch.object(frontend_exports, "validate_frontend_contract_files",
                                      return_value={"fixture": True}), \
                    mock.patch.object(pipeline, "_coverage_export_files", return_value={}), \
                    mock.patch.object(pipeline, "_field_status_export_files", return_value={}), \
                    mock.patch.object(publication, "stage_generation", return_value=manifest) as stage, \
                    mock.patch.object(publication, "publish_generation", return_value=manifest) as publish:
                self.assertEqual(0, pipeline.cmd_export(ctx, types.SimpleNamespace()))
            stage.assert_called_once()
            publish.assert_called_once()
            self.assertEqual(2, len(ctx.staging.publications))
            self.assertEqual(["staged", "published"],
                             [row["status"] for row in ctx.staging.publications])


class PublicationBoundaryValidationTests(unittest.TestCase):
    class _Staging:
        def __init__(self, row):
            self.row = row

        def query(self, sql, params=()):
            if "FROM publication_generations" in sql:
                return [self.row]
            raise AssertionError(f"unexpected SQL: {sql}")

    class _Context:
        def __init__(self, row):
            self.staging = PublicationBoundaryValidationTests._Staging(row)

    def _fixture(self, root):
        body = b"coherent\n"
        expected = {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
        manifest = {
            "generation_id": "a" * 64,
            "kind": "consumer_exports",
            "generation_path": ".generations/consumer_exports/" + "a" * 64,
            "files": {"canonical_observations.csv": expected},
            "metadata": {
                "code_snapshot": "c" * 64,
                "input_snapshot": "i" * 64,
                "database_identity": "d" * 64,
                "unit_commit_count": 1,
                "unit_commit_snapshot": "u" * 64,
            },
        }
        exports = root / "exports"
        exports.mkdir()
        (exports / "canonical_observations.csv").write_bytes(body)
        row = {
            "generation_id": manifest["generation_id"],
            "code_snapshot": "c" * 64,
            "input_snapshot": "i" * 64,
            "database_identity": "d" * 64,
            "manifest_json": json.dumps(manifest, sort_keys=True),
            "status": "published",
        }
        return manifest, row, exports

    def _run(self, root, manifest, row):
        with mock.patch.object(pipeline, "OUTPUT_DIR", root), \
                mock.patch.object(pipeline, "EXPORTS", root / "exports"), \
                mock.patch.object(pipeline, "VERIFICATION", root / "verification"), \
                mock.patch.object(validate, "run_checks", return_value=0), \
                mock.patch.object(publication, "verify_receipt", return_value=manifest), \
                mock.patch.object(pipeline, "_code_snapshot", return_value="c" * 64), \
                mock.patch.object(pipeline, "_input_snapshot", return_value="i" * 64), \
                mock.patch.object(pipeline, "_unit_commit_snapshot", return_value={
                    "unit_commit_count": 1, "unit_commit_snapshot": "u" * 64}), \
                mock.patch.object(pipeline, "_database_semantic_identity",
                                  return_value="d" * 64):
            return pipeline.cmd_validate(self._Context(row), types.SimpleNamespace())

    def test_valid_boundary_accepts_matching_database_generation_and_compatibility_file(self):
        with tempfile.TemporaryDirectory(prefix="ferc-boundary-valid-") as td:
            root = pathlib.Path(td)
            manifest, row, _exports = self._fixture(root)
            self.assertEqual(0, self._run(root, manifest, row))

    def test_database_manifest_compatibility_and_snapshot_mismatches_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="ferc-boundary-bad-") as td:
            root = pathlib.Path(td)
            manifest, row, exports = self._fixture(root)

            bad_row = dict(row, manifest_json=json.dumps(dict(manifest, kind="other")))
            self.assertEqual(1, self._run(root, manifest, bad_row))

            (exports / "canonical_observations.csv").write_bytes(b"stale\n")
            self.assertEqual(1, self._run(root, manifest, row))

            (exports / "canonical_observations.csv").write_bytes(b"coherent\n")
            bad_manifest = json.loads(json.dumps(manifest))
            bad_manifest["metadata"]["unit_commit_snapshot"] = "x" * 64
            bad_row = dict(row, manifest_json=json.dumps(bad_manifest, sort_keys=True))
            self.assertEqual(1, self._run(root, bad_manifest, bad_row))


if __name__ == "__main__":
    unittest.main()
