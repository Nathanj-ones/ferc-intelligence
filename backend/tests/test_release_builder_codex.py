"""Focused release-builder controls for A13/A21 and Build-A identity.

All archives, databases and mutations are synthetic and live in temporary
directories.  The production candidate is imported for code only.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import sqlite3
import stat
import tempfile
import types
import unittest
import warnings
import zipfile
from unittest import mock

import build_release as release
import build_full_bundle as full_bundle
from acceptance.contract import REQUIRED_OUTPUTS


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _identity(data: bytes) -> dict:
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
            "class": "generated_data"}


def _contract_tree(root: pathlib.Path) -> None:
    """Small positive fixture for the fixed required-output contract."""
    backing = {spec.backing_table for spec in REQUIRED_OUTPUTS if spec.backing_table}
    db = root / "staging" / "operating_assets.sqlite"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    for table in sorted(backing | {"observations", "filings", "coverage_expected"}):
        con.execute(f'CREATE TABLE "{table}"(x INTEGER)')
        con.execute(f'INSERT INTO "{table}" VALUES(1)')
    con.commit()
    con.close()

    for spec in REQUIRED_OUTPUTS:
        path = root / spec.path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".json":
            payload = {key: {} for key in spec.required_json_keys}
            path.write_text(json.dumps(payload), encoding="utf-8")
        else:
            columns = list(spec.required_columns) or ["status"]
            path.write_text(",".join(columns) + "\n" + ",".join("x" for _ in columns)
                            + "\n", encoding="utf-8")


def _build_a_tree(root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    db = root / "staging" / "operating_assets.sqlite"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.executescript((ROOT / "ferclib" / "schema.sql").read_text(encoding="utf-8"))
    con.commit()
    con.close()

    semantic = release.database_semantic_identity(db)
    metadata = {"code_snapshot": "c" * 64, "input_snapshot": "i" * 64,
                "database_identity": semantic}
    files: dict[str, dict] = {}
    for spec in REQUIRED_OUTPUTS:
        logical = spec.path[len("exports/"):]
        exported = f"fixture,{logical}\n".encode()
        files[logical] = _identity(exported)
        files[logical].pop("class")
    identity_payload = {"kind": "consumer_exports", "files": files,
                        "metadata": metadata}
    generation_id = hashlib.sha256(json.dumps(
        identity_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    generation_rel = f".generations/consumer_exports/{generation_id}"
    for logical, expected in files.items():
        exported = f"fixture,{logical}\n".encode()
        generated = root / generation_rel / logical
        compatible = root / "exports" / logical
        generated.parent.mkdir(parents=True, exist_ok=True)
        compatible.parent.mkdir(parents=True, exist_ok=True)
        generated.write_bytes(exported)
        compatible.write_bytes(exported)
    receipt = {"generation_id": generation_id, "kind": "consumer_exports",
               "generation_path": generation_rel, "files": files,
               "metadata": metadata}
    (root / release.PUBLICATION_RECEIPT_NAME).write_text(
        json.dumps(receipt), encoding="utf-8")
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO publication_generations VALUES(?,?,?,?,?,?,?,?)",
        (generation_id, "run-test", metadata["code_snapshot"],
         metadata["input_snapshot"], semantic, json.dumps(receipt),
         "published", "2026-09-09T00:00:00+00:00"))
    con.commit()
    con.close()
    return db, root / "exports" / "canonical_observations.csv"


class ReleaseBuilderTests(unittest.TestCase):
    def test_fixed_contract_rejects_removed_required_export(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            _contract_tree(root)
            self.assertEqual([], release.stage1_freeze(root)["violations"])
            target = root / "exports" / "quarterly_key_metrics.csv"
            target.unlink()
            violations = release.stage1_freeze(root)["violations"]
            self.assertTrue(any(v["kind"] == "MISSING_REQUIRED_OUTPUT"
                                and v["path"] == "exports/quarterly_key_metrics.csv"
                                for v in violations))

    def test_fixed_contract_rejects_binary_controls_as_malformed_csv(self) -> None:
        """A NUL-bearing CSV is not publishable on every declared runtime."""
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            _contract_tree(root)
            self.assertEqual([], release.stage1_freeze(root)["violations"])
            target = root / "exports" / "document_facts.csv"
            target.write_bytes(target.read_bytes().replace(b"x\n", b"x\x00\n", 1))
            violations = release.stage1_freeze(root)["violations"]
            malformed = [v for v in violations
                         if v["path"] == "exports/document_facts.csv"
                         and v["kind"] == "MALFORMED_REQUIRED_OUTPUT"]
            self.assertEqual(1, len(malformed), violations)
            self.assertIn("U+0000", malformed[0]["detail"])

    def test_lite_full_names_and_identity_sidecars_are_distinct(self) -> None:
        self.assertNotEqual(release.archive_name(False), release.archive_name(True))
        self.assertNotEqual(release.manifest_for(False), release.manifest_for(True))
        self.assertNotEqual(release.receipt_for(False), release.receipt_for(True))
        self.assertIn("REPAIRED", release.archive_name(False))
        self.assertTrue(release.archive_name(True).endswith("_FULL.zip"))
        self.assertEqual(
            "operating_assets_all_regimes_REPAIRED_v1_FULL.zip",
            release.archive_name(True, "v1"))

    def test_full_release_requires_deferred_build_b_and_pending_is_not_ignored(self):
        self.assertEqual(2, release.main(["--payload", "full"]))
        self.assertEqual(2, release.main([
            "--payload", "lite", "--pending-sidecar", "must-not-be-ignored.json",
        ]))
        self.assertIn(
            "python3 build_release.py --payload full --defer-build-b",
            release.__doc__,
        )

    def test_full_bundle_shim_and_docs_preserve_the_clean_extraction(self):
        self.assertEqual(
            ["--payload", "full", "--defer-build-b", "--version", "v1"],
            full_bundle._forwarded_args(["--version", "v1"]),
        )
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        decisions = (ROOT / "DECISIONS.md").read_text(encoding="utf-8")
        self.assertIn("keeps that clean extraction byte-for-", readme)
        self.assertIn("separate, initially empty work root", readme)
        self.assertNotIn("Build acceptance removes the packaged database", readme)
        self.assertIn("full manifest includes\nthe database", decisions)
        self.assertIn("lite manifest omits\n`staging/`", decisions)

    def test_full_payload_contract_is_fixed_not_inventoried(self) -> None:
        self.assertTrue({
            "implementation/import_bounded_elibrary_search_capture.py",
            "inputs/official_ferc/elibrary/rate_proceedings_v1.csv",
            "inputs/official_ferc_elibrary_search_recovery_20260909/CAPTURE.json",
            "implementation_logs/input_recovery/bounded_elibrary_search_import_runs.jsonl",
            "inputs/official_ferc_elibrary_dependency_recovery_20260910/CAPTURE.json",
            "inputs/official_ferc_elibrary_dependency_recovery_20260910/source_cache_index_before.json",
            "implementation_logs/input_recovery/build_a_v4_dependency_capture.log",
        }.issubset(set(release.FULL_MANDATORY_ARTIFACTS)))
        self.assertIn("config/universe.csv", release.FULL_MANDATORY_ARTIFACTS)
        self.assertNotIn("config/universe_draft.csv", release.FULL_MANDATORY_ARTIFACTS)
        self.assertNotIn("data_side_facts.json", release.FULL_MANDATORY_ARTIFACTS)
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            for rel in release.FULL_MANDATORY_ARTIFACTS:
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"present")
            self.assertEqual([], release.full_payload_contract(root))
            (root / "source_cache" / "index.json").unlink()
            violations = release.full_payload_contract(root)
            self.assertEqual(["source_cache/index.json"],
                             [v["path"] for v in violations])
            self.assertEqual("MISSING_FULL_PAYLOAD_ARTIFACT", violations[0]["kind"])

    def test_final_record_receipt_binds_generation_and_compatibility_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            records_root = root / "final_records"
            identities = {}
            for number, name in enumerate(release.FINAL_RECORD_FILES):
                raw = ("final-%d-%s" % (number, name)).encode("utf-8")
                path = records_root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                identities[name] = _identity(raw)
                identities[name].pop("class")
            generation_id = hashlib.sha256(json.dumps(
                {"kind": "final_implementation_records", "files": identities},
                sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            generation = records_root / ".final_records" / generation_id
            generation.mkdir(parents=True)
            for name in release.FINAL_RECORD_FILES:
                (generation / name).write_bytes((records_root / name).read_bytes())
            receipt = {
                "schema": "ferc-final-records-receipt-v1", "status": "published",
                "kind": "final_implementation_records", "generation_id": generation_id,
                "generation_path": ".final_records/" + generation_id,
                "files": identities,
            }
            (records_root / release.FINAL_RECORD_RECEIPT).write_text(
                json.dumps(receipt), encoding="utf-8")
            self.assertEqual("verified", release.verify_final_records_boundary(
                root, required=True)["status"])
            (records_root / release.FINAL_RECORD_FILES[0]).write_bytes(b"tampered")
            with self.assertRaisesRegex(release.ReleaseRefused, "identity mismatch"):
                release.verify_final_records_boundary(root, required=True)

    def test_lite_main_publishes_only_after_verification_and_preserves_last_good(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            root = base / "tree"
            root.mkdir()
            _contract_tree(root)
            shutil.copytree(ROOT / "ferclib", root / "ferclib",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            rc = release.main(["--payload", "lite", "--root", str(root),
                               "--out-dir", str(base), "--version", "test"])
            self.assertEqual(0, rc)
            archive = base / release.archive_name(False, "test")
            receipt = root / release.receipt_for(False)
            manifest = root / release.manifest_for(False)
            self.assertTrue(archive.is_file())
            self.assertTrue(receipt.is_file())
            self.assertTrue(manifest.is_file())
            lite_receipt = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertIn(
                "python3 build_release.py --payload full --defer-build-b",
                lite_receipt["reassembly"],
            )
            archive_before = archive.read_bytes()
            receipt_before = receipt.read_bytes()
            with zipfile.ZipFile(archive) as z:
                basenames = {pathlib.PurePosixPath(n).name for n in z.namelist()}
                self.assertNotIn(release.receipt_for(False), basenames)
                self.assertEqual("lite", json.loads(
                    z.read(f"{release.NAME}/{release.MANIFEST_NAME}"))["payload"])

            (root / "exports" / "quarterly_key_metrics.csv").unlink()
            failed = release.main(["--payload", "lite", "--root", str(root),
                                   "--out-dir", str(base), "--version", "test"])
            self.assertEqual(1, failed)
            self.assertEqual(archive_before, archive.read_bytes())
            self.assertEqual(receipt_before, receipt.read_bytes())

    def test_payload_inventory_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            (root / "real.txt").write_text("real", encoding="utf-8")
            (root / "link.txt").symlink_to(root / "real.txt")
            with self.assertRaisesRegex(release.ReleaseRefused, "symlink"):
                release.stage2_hash(root, full=True)

    def test_payload_excludes_draft_claims_and_unselected_worker_logs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            files = {
                "implementation/REPAIR_LEDGER_DRAFT.json": b"pending_build_a",
                "implementation/EXCEPTION_DISPOSITIONS_DRAFT.csv": b"unclaimed",
                "implementation/RELATED_REPAIR_FINDINGS_DRAFT.json": b"draft",
                "implementation/ADDITIONAL_INPUTS_DISCOVERED_DRAFT.json": b"draft",
                "implementation/FINAL_TEST_RUN_SPEC_DRAFT.json": b"pending",
                "config/universe.csv": b"final roster",
                "config/universe_draft.csv": b"stale draft roster",
                "config/universe_draft.csv.notes": b"retained positive control",
                "data_side_facts.json": b"stale provisional output",
                "evidence/data_side_facts.json": b"retained evidence copy",
                "implementation_logs/worker/debug.log": b"debug",
                "implementation_logs/input_recovery/provenance.json": b"official",
                "final_records/FINAL_REPAIR_LEDGER.json": b"final",
            }
            for rel, body in files.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
            for full in (True, False):
                with self.subTest(full=full):
                    included = {rel for _path, rel in release.payload_paths(root, full=full)}
                    self.assertNotIn("implementation/REPAIR_LEDGER_DRAFT.json", included)
                    self.assertNotIn("implementation/EXCEPTION_DISPOSITIONS_DRAFT.csv", included)
                    self.assertNotIn("implementation/RELATED_REPAIR_FINDINGS_DRAFT.json", included)
                    self.assertNotIn("implementation/ADDITIONAL_INPUTS_DISCOVERED_DRAFT.json", included)
                    self.assertNotIn("implementation/FINAL_TEST_RUN_SPEC_DRAFT.json", included)
                    self.assertNotIn("config/universe_draft.csv", included)
                    self.assertNotIn("data_side_facts.json", included)
                    self.assertIn("config/universe.csv", included)
                    self.assertIn("config/universe_draft.csv.notes", included)
                    self.assertIn("evidence/data_side_facts.json", included)
                    self.assertNotIn("implementation_logs/worker/debug.log", included)
                    self.assertIn("implementation_logs/input_recovery/provenance.json", included)
                    self.assertIn("final_records/FINAL_REPAIR_LEDGER.json", included)

    def test_archive_is_exact_crc_clean_safe_and_receipt_external(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "tree"
            root.mkdir()
            (root / "a.txt").write_bytes(b"alpha")
            entries = {"a.txt": _identity(b"alpha")}
            manifest = release.build_manifest(
                entries, full=False,
                extra={"contract_check": {"required_outputs": 1, "violations": 0}})
            archive = pathlib.Path(td) / "release.zip"
            release.stage3_archive(root, entries, manifest, archive, full=False)
            repeated = pathlib.Path(td) / "release-repeat.zip"
            release.stage3_archive(root, entries, manifest, repeated, full=False)
            self.assertEqual(archive.read_bytes(), repeated.read_bytes())
            check = release.inspect_archive(archive, manifest)
            self.assertEqual([], release._archive_safety_failures(check))
            self.assertEqual(2, check["members"])
            with zipfile.ZipFile(archive) as z:
                names = z.namelist()
                self.assertEqual(len(names), len(set(names)))
                self.assertNotIn(release.receipt_for(False),
                                 {pathlib.PurePosixPath(n).name for n in names})
                self.assertEqual(b"alpha", z.read(f"{release.NAME}/a.txt"))

            streamed = release.stream_verify_archive(archive, manifest, full=False)
            self.assertEqual(1, streamed["verified_files"])
            self.assertTrue(streamed["payload_digest_matches"])
            self.assertEqual([], streamed["hash_mismatches"])

    def test_stream_verifier_rejects_member_whose_bytes_disagree_with_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            entries = {"a.txt": _identity(b"alpha")}
            manifest = release.build_manifest(entries, full=False, extra={})
            archive = root / "wrong.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr(f"{release.NAME}/a.txt", b"beta")
                z.writestr(f"{release.NAME}/{release.MANIFEST_NAME}",
                           release._json_bytes(manifest))
            result = release.stream_verify_archive(archive, manifest, full=False)
            self.assertFalse(result["payload_digest_matches"])
            self.assertTrue(any("a.txt" in item for item in result["hash_mismatches"]))

    def test_exact_redundant_alias_is_declared_and_only_canonical_bytes_ship(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            body = b"same official response"
            digest = hashlib.sha256(body).hexdigest()
            declaration = ({
                "omitted_path": "inputs/raw.body",
                "canonical_path": f"source_cache/objects/{digest[:2]}/{digest}",
                "sha256": digest, "bytes": len(body),
                "provenance_paths": ["evidence/request.json"],
                "reason": "test exact alias",
            },)
            omitted = root / declaration[0]["omitted_path"]
            canonical = root / declaration[0]["canonical_path"]
            omitted.parent.mkdir(parents=True)
            canonical.parent.mkdir(parents=True)
            omitted.write_bytes(body)
            canonical.write_bytes(body)
            provenance = root / "evidence/request.json"
            provenance.parent.mkdir()
            provenance.write_text("{}", encoding="utf-8")
            index = {hashlib.sha256(b"https://example.test/source").hexdigest(): {
                            "content_hash": digest, "byte_size": len(body),
                            "cache_path": declaration[0]["canonical_path"].removeprefix(
                                "source_cache/")}}
            (root / "source_cache/index.json").write_text(json.dumps(index))
            with mock.patch.object(release, "REDUNDANT_PAYLOAD_ALIASES", declaration):
                staged = release.stage2_hash(root, full=True)
                self.assertNotIn("inputs/raw.body", staged["entries"])
                self.assertIn(declaration[0]["canonical_path"], staged["entries"])
                self.assertEqual("verified_exact_alias_omitted",
                                 staged["redundant_aliases"][0]["status"])
                omitted.write_bytes(b"changed")
                with self.assertRaisesRegex(release.ReleaseRefused, "not both the exact"):
                    release.stage2_hash(root, full=True)

    def test_stage2_reuses_the_cache_contract_byte_stream_identity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            body = b"official cached response"
            digest = hashlib.sha256(body).hexdigest()
            object_path = root / "source_cache" / "objects" / digest[:2] / digest
            object_path.parent.mkdir(parents=True)
            object_path.write_bytes(body)
            index = {hashlib.sha256(b"https://example.test/reuse").hexdigest(): {
                "content_hash": digest,
                "byte_size": len(body),
                "cache_path": f"objects/{digest[:2]}/{digest}",
            }}
            (root / "source_cache" / "index.json").write_text(
                json.dumps(index), encoding="utf-8")
            original = release.sha_and_size
            with mock.patch.object(release, "REDUNDANT_PAYLOAD_ALIASES", ()), \
                    mock.patch.object(release, "sha_and_size",
                                      wraps=original) as hashed:
                staged = release.stage2_hash(root, full=True)
            object_reads = [call for call in hashed.call_args_list
                            if pathlib.Path(call.args[0]) == object_path]
            self.assertEqual(1, len(object_reads),
                             "stage2 immediately re-read an already verified cache object")
            rel = f"source_cache/objects/{digest[:2]}/{digest}"
            self.assertEqual({"sha256": digest, "bytes": len(body),
                              "class": release.classify(rel)},
                             staged["entries"][rel])

    def test_archive_inspector_detects_traversal_duplicates_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            archive = pathlib.Path(td) / "bad.zip"
            body = b"alpha"
            entries = {"a.txt": _identity(body)}
            manifest = release.build_manifest(entries, full=False, extra={})
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(archive, "w") as z:
                    z.writestr(f"{release.NAME}/a.txt", body)
                    z.writestr(f"{release.NAME}/a.txt", body)
                    z.writestr(f"{release.NAME}/A.TXT", body)
                    z.writestr(f"{release.NAME}/../escape.txt", b"bad")
                    link = zipfile.ZipInfo(f"{release.NAME}/link")
                    link.create_system = 3
                    link.external_attr = (stat.S_IFLNK | 0o777) << 16
                    z.writestr(link, b"a.txt")
                    z.writestr(f"{release.NAME}/{release.MANIFEST_NAME}",
                               release._json_bytes(manifest))
            check = release.inspect_archive(archive, manifest)
            self.assertTrue(check["duplicates"])
            self.assertTrue(check["case_collisions"])
            self.assertTrue(check["unsafe_members"])
            self.assertTrue(check["symlinks"])
            self.assertTrue(check["unexpected"])
            self.assertTrue(release._archive_safety_failures(check))

    def test_archive_inspector_reports_missing_or_invalid_embedded_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            entries = {"a.txt": _identity(b"alpha")}
            manifest = release.build_manifest(entries, full=False, extra={})
            for name, embedded in (("missing.zip", None), ("invalid.zip", b"not-json")):
                archive = root / name
                with zipfile.ZipFile(archive, "w") as z:
                    z.writestr(f"{release.NAME}/a.txt", b"alpha")
                    if embedded is not None:
                        z.writestr(f"{release.NAME}/{release.MANIFEST_NAME}", embedded)
                check = release.inspect_archive(archive, manifest)
                self.assertFalse(check["embedded_manifest_matches"])
                self.assertTrue(check["embedded_manifest_error"])
                self.assertTrue(release._archive_safety_failures(check))

    def test_changed_source_never_replaces_last_good_archive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td) / "tree"
            root.mkdir()
            source = root / "a.txt"
            source.write_bytes(b"before")
            entries = {"a.txt": _identity(b"before")}
            manifest = release.build_manifest(entries, full=False, extra={})
            source.write_bytes(b"after")
            archive = pathlib.Path(td) / "release.zip"
            archive.write_bytes(b"LAST_GOOD")
            with self.assertRaisesRegex(release.ReleaseRefused, "changed"):
                release.stage3_archive(root, entries, manifest, archive, full=False)
            self.assertEqual(b"LAST_GOOD", archive.read_bytes())

    def test_build_a_boundary_ties_database_generation_and_exports(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            db, compatible = _build_a_tree(root)
            result = release.verify_build_a_boundary(root, required=True)
            self.assertEqual("verified", result["status"])
            self.assertEqual(len(REQUIRED_OUTPUTS), result["files"])

            full = release.stage2_hash(root, full=True)["entries"]
            self.assertIn("staging/operating_assets.sqlite", full)
            self.assertIn("exports/canonical_observations.csv", full)
            self.assertIn(release.PUBLICATION_RECEIPT_NAME, full)

            compatible.write_bytes(b"different")
            with self.assertRaisesRegex(release.ReleaseRefused,
                                        "compatibility export identity mismatch"):
                release.verify_build_a_boundary(root, required=True)
            compatible.write_bytes(b"fixture,canonical_observations.csv\n")
            db.with_name(db.name + "-wal").write_bytes(b"not checkpointed")
            with self.assertRaisesRegex(release.ReleaseRefused, "WAL is non-empty"):
                release.verify_build_a_boundary(root, required=True)

    def test_extracted_offline_suite_uses_isolated_interpreter_and_local_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            temp = pathlib.Path(td)
            pkg = temp / "pkg"
            (pkg / "config").mkdir(parents=True)
            (pkg / "config" / "run_plan.json").write_text(
                json.dumps({"as_of": "2026-09-07"}), encoding="utf-8")
            completed = types.SimpleNamespace(returncode=0, stdout="VALID", stderr="")
            with mock.patch.object(release.subprocess, "run", return_value=completed) as run:
                result = release.run_extracted_offline_suite(
                    pkg, full=True, temp_root=temp)
            self.assertEqual(2, run.call_count)
            first_args, first_kwargs = run.call_args_list[0]
            second_args, kwargs = run.call_args_list[1]
            self.assertEqual([release.sys.executable, "-I", "-S", "run.py", "plan",
                              "--check"], first_args[0])
            command = second_args[0]
            self.assertEqual([release.sys.executable, "-I", "-S", "run.py", "validate"],
                             command[:5])
            self.assertIn("--offline", command)
            self.assertEqual("1", kwargs["env"]["FERC_OFFLINE"])
            self.assertEqual(str(pkg / "staging" / "operating_assets.sqlite"),
                             kwargs["env"]["FERC_STAGING_DB"])
            self.assertEqual(str(pkg), kwargs["cwd"])
            self.assertEqual(str(pkg), first_kwargs["cwd"])
            self.assertEqual(0, result["returncode"])
            self.assertTrue(result["offline_enforced"])

    def test_pending_full_candidate_has_no_pass_receipt_and_finalizes_only_from_bound_b(self):
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            root = base / "tree"
            out = base / "releases"
            root.mkdir()
            (root / "a.txt").write_bytes(b"alpha")
            entries = {"a.txt": _identity(b"alpha")}
            build_a = {"status": "verified", "required": True,
                       "database_identity": "d" * 64}
            with mock.patch.object(release, "stage1_freeze", return_value={
                    "table_counts_available": True,
                    "required_outputs_declared": len(REQUIRED_OUTPUTS),
                    "violations": []}), \
                    mock.patch.object(release, "full_payload_contract", return_value=[]), \
                    mock.patch.object(release, "verify_build_a_boundary",
                                      return_value=build_a), \
                    mock.patch.object(release, "verify_final_records_boundary",
                                      return_value={"status": "verified",
                                                    "required": True}), \
                    mock.patch.object(release, "stage2_hash", return_value={
                        "entries": entries, "unreadable": [], "redundant_aliases": [],
                        "source_cache_contract": {"status": "test"},
                        "referenced_cache_objects": None}), \
                    mock.patch.object(release, "payload_paths",
                                      return_value=[(root / "a.txt", "a.txt")]), \
                    mock.patch.object(release, "validate_redundant_aliases", return_value=[]), \
                    mock.patch.object(release, "source_cache_contract",
                                      return_value=({"status": "test"}, None)):
                rc = release.main(["--payload", "full", "--defer-build-b",
                                   "--root", str(root), "--out-dir", str(out),
                                   "--version", "pending-test"])
            self.assertEqual(0, rc)
            archive = out / release.archive_name(True, "pending-test")
            pending_path = archive.with_suffix(archive.suffix + ".pending.json")
            self.assertTrue(archive.is_file())
            self.assertTrue(pending_path.is_file())
            self.assertFalse((root / release.FULL_RECEIPT_NAME).exists())
            pending = json.loads(pending_path.read_text())
            self.assertEqual("CANDIDATE_PENDING_BUILD_B", pending["status"])
            action = pending["next_required_action"]
            self.assertIn("extraction", action)
            self.assertIn("byte-for-byte unchanged", action)
            self.assertIn("separate empty --work-root", action)
            self.assertNotIn("remove packaged", action)

            manifest = release.load_embedded_manifest(archive)
            archive_sha, archive_size = release.sha_and_size(archive)
            evidence = {
                "schema": "ferc-build-b-acceptance-v1", "status": "PASS",
                "checks": {name: True for name in release.FINAL_BUILD_B_CHECKS},
                "identities": {
                    "archive_sha256": archive_sha, "archive_bytes": archive_size,
                    "payload_digest": manifest["payload_digest"],
                    "manifest_sha256": hashlib.sha256(
                        release._json_bytes(manifest)).hexdigest(),
                    "build_a_database_identity": "d" * 64,
                    "build_b_database_identity": "d" * 64,
                    "packaged_database_identity": "d" * 64,
                },
            }
            evidence_path = base / "build_b.json"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            receipt_path = base / "release_receipt_full.json"
            receipt = release.finalize_build_b_receipt(
                archive, pending_path, evidence_path, receipt_path)
            self.assertEqual("PASS", receipt["status"])
            self.assertTrue(receipt_path.is_file())

            evidence["checks"]["acceptance_pass"] = False
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            with self.assertRaisesRegex(release.ReleaseRefused, "failed/missing"):
                release.finalize_build_b_receipt(
                    archive, pending_path, evidence_path, base / "must-not-exist.json")
            self.assertFalse((base / "must-not-exist.json").exists())


if __name__ == "__main__":
    unittest.main()
