"""Active interruption and abrupt-exit controls for the real unit writer.

The original M2.2/M2.4 acceptance fixtures patched ``write_observations`` even
though :func:`run.run_adapter` publishes through :meth:`Staging.commit_unit`.
These tests inject faults into the live call path and use subprocesses for the
cases where cleanup code cannot run at all.

All state lives in ``TemporaryDirectory`` databases, ledgers and caches.  The
subprocess trace is a deliberate proof that the requested fault point was
reached; a test may not infer that merely from an unusual return code.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run as pipeline  # noqa: E402
from ferclib import http as fhttp  # noqa: E402
from ferclib.ledger import Ledger, identity_digest  # noqa: E402
from ferclib.staging import Staging  # noqa: E402


ADAPTER = "fault_adapter"
ENTITY = "CID-FAULT-001"
METRIC = "fault_metric"
DERIVED_METRIC = "fault_derived_metric"
SCOPE = "legal entity CID-FAULT-001; synthetic interruption fixture"
OBSERVATION_ID = "obs-fault-2025"
DERIVED_OBSERVATION_ID = "obs-fault-dependent-2025"
TASK = f"{ADAPTER}:{ENTITY}"
IDENTITY = {
    "adapter": ADAPTER,
    "entity_key": ENTITY,
    "year_from": 2025,
    "year_to": 2025,
    "code_version": "interrupt-fixture-v1",
    "registry_version": "interrupt-fixture-v1",
    "config_digest": "config-interrupt-fixture-v1",
    "adapter_digest": "adapter-interrupt-fixture-v1",
}
SCOPE_KEY = Staging.scope_key_for(2025, 2025, identity_digest(IDENTITY))


def _observation(value: int, filing: str) -> dict:
    return {
        "observation_id": OBSERVATION_ID,
        "entity_key": ENTITY,
        "metric_id": METRIC,
        "source_regime": "synthetic_fault_fixture",
        "period_basis": "annual",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
        "reporting_year": 2025,
        "reporting_period": "annual",
        "scope": SCOPE,
        "unit": "USD",
        "value_text": str(value),
        "value_num": float(value),
        "availability": "available",
        "origin": "reported",
        "method": "direct",
        "derivation": None,
        "version_status": "original" if value == 100 else "revised",
        "validation": "validated",
        "source_system": "synthetic_fault_fixture",
        "filing_id": filing,
        "registry_version": "interrupt-fixture-v1",
        "qa_flags": "synthetic_fixture_only",
        "review_status": "",
    }


def _dependent_observation() -> dict:
    row = _observation(300, "filing-derived")
    row.update({
        "observation_id": DERIVED_OBSERVATION_ID,
        "metric_id": DERIVED_METRIC,
        "method": "derived",
        "derivation": "synthetic dependency on filing-last-good",
        "version_status": "original",
    })
    return row


def _seed_last_good(db_path: pathlib.Path) -> None:
    staging = Staging(db_path)
    staging.start_run("seed", {"fixture": True}, "fixture", "fixture")
    staging.checkpoint(ADAPTER, ENTITY, SCOPE_KEY, "in_progress")
    staging.record_unit_status(ADAPTER, ENTITY, SCOPE_KEY, "in_progress", "seed")
    staging.commit_unit(
        adapter=ADAPTER,
        entity_key=ENTITY,
        metric_ids=[METRIC, DERIVED_METRIC],
        year_from=2025,
        year_to=2025,
        observations=[_observation(100, "filing-last-good"),
                      _dependent_observation()],
        edges=[{
            "observation_id": DERIVED_OBSERVATION_ID,
            "input_order": 1,
            "input_role": "basis",
            "operator_sign": "+",
            "coefficient": 1.0,
            "input_source_system": "synthetic_fault_fixture",
            "input_filing_id": "filing-last-good",
            "input_value": "100",
            "input_unit": "USD",
            "input_version_status": "original",
        }],
        scope_key=SCOPE_KEY,
        input_digest="input-last-good",
        identity=IDENTITY,
    )
    staging.checkpoint(ADAPTER, ENTITY, SCOPE_KEY, "done")
    staging.record_unit_status(ADAPTER, ENTITY, SCOPE_KEY, "done", "seed committed")
    staging.finish_run("complete", "last-good fixture")
    staging.close()


def _durable_trace(path: pathlib.Path, message: str) -> None:
    """Write and fsync the injection marker before deliberately dying."""
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (message + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _crash_child(scenario: str, db_name: str, ledger_name: str,
                 trace_name: str) -> None:
    """Run one real commit and terminate without Python/SQLite cleanup."""
    db_path = pathlib.Path(db_name)
    ledger_path = pathlib.Path(ledger_name)
    trace_path = pathlib.Path(trace_name)
    staging = Staging(db_path)
    staging.start_run("refresh", {"fixture": True}, "fixture", "fixture")
    Ledger(ledger_path).set(TASK, "running")
    staging.checkpoint(ADAPTER, ENTITY, SCOPE_KEY, "in_progress")
    staging.record_unit_status(
        ADAPTER, ENTITY, SCOPE_KEY, "in_progress", f"armed:{scenario}")

    kwargs = {
        "adapter": ADAPTER,
        "entity_key": ENTITY,
        "metric_ids": [METRIC],
        "year_from": 2025,
        "year_to": 2025,
        "observations": [_observation(200, "filing-replacement")],
        "edges": [],
        "scope_key": SCOPE_KEY,
        "input_digest": "input-replacement",
        "identity": IDENTITY,
    }

    if scenario == "before_commit":
        real_upsert = staging._upsert

        def exit_from_observation_write(con, table, rows, keys):
            if table == "observations":
                _durable_trace(trace_path, "reached:commit_unit:observation_upsert")
                os._exit(71)
            return real_upsert(con, table, rows, keys)

        staging._upsert = exit_from_observation_write
        staging.commit_unit(**kwargs)
        raise AssertionError("before-commit injection did not terminate")

    if scenario == "during_commit":
        transaction_opcode = getattr(sqlite3, "SQLITE_TRANSACTION", 22)

        def authorizer(action, argument1, _argument2, _database, _trigger):
            if action == transaction_opcode and str(argument1).upper() == "COMMIT":
                _durable_trace(trace_path, "reached:sqlite:COMMIT-authorizer")
                os._exit(72)
            return sqlite3.SQLITE_OK

        staging.con.set_authorizer(authorizer)
        staging.commit_unit(**kwargs)
        raise AssertionError("during-commit injection did not terminate")

    if scenario == "after_commit_before_status":
        staging.commit_unit(**kwargs)
        _durable_trace(trace_path, "reached:commit_unit:return-before-status")
        os._exit(73)

    if scenario == "after_commit_before_dependency_update":
        # Exercise the repaired production ordering: revision invalidation is
        # inside commit_unit's BEGIN IMMEDIATE and precedes unit_commits.  Dying
        # on entry must roll back both the replacement and its absent marker.
        adapter = _FakeAdapter(supersedes=True)
        ctx = types.SimpleNamespace(
            args=types.SimpleNamespace(command="refresh", year_from=2025, year_to=2025),
            force=False,
            staging=staging,
            ledger=Ledger(ledger_path),
            client=types.SimpleNamespace(cache_misses=[]),
            failures=[],
            interrupted=False,
        )

        def log(level, message, *, adapter="", entity_cid=""):
            staging.log(level, message, adapter=adapter, entity_cid=entity_cid)

        def exit_at_dependency_phase(_con, _source_system, _filing_id):
            _durable_trace(trace_path, "reached:commit_unit:transactional_invalidation")
            os._exit(74)

        ctx.log = log
        with _fake_adapter_path(adapter), \
                mock.patch.object(staging, "_invalidate_dependents_in_transaction",
                                  side_effect=exit_at_dependency_phase):
            pipeline.run_adapter(ctx, ADAPTER, [_entity()])
        raise AssertionError("post-commit dependency injection did not terminate")

    raise AssertionError(f"unknown crash scenario: {scenario}")


class _FakeAdapter:
    def __init__(self, value: int = 200, supersedes: bool = False):
        self.value = value
        self.supersedes = supersedes
        self.retrieve_calls = 0

    def retrieve(self, _ctx, _entity, *, year_from, year_to):
        self.retrieve_calls += 1
        filing = {
            "source_system": "synthetic_fault_fixture",
            "filing_id": "filing-replacement",
            "content_hash": "2" * 64,
            "reporting_year": year_to,
            "reporting_period": "annual",
            "version_status": "revised",
            "is_canonical": 1,
        }
        if self.supersedes:
            filing["supersedes_filing_id"] = "filing-last-good"
        return [filing]

    def freeze_expected(self, _ctx, _entity, _filings, _assets):
        return []

    def canonicalise(self, _ctx, _entity, _filings, _expected):
        return [_observation(self.value, "filing-replacement")], []


class _RunContext:
    def __init__(self, db_path: pathlib.Path, ledger_path: pathlib.Path,
                 command: str = "refresh"):
        self.args = types.SimpleNamespace(
            command=command, year_from=2025, year_to=2025)
        self.force = False
        self.staging = Staging(db_path)
        self.ledger = Ledger(ledger_path)
        self.client = types.SimpleNamespace(cache_misses=[])
        self.failures = []
        self.interrupted = False
        self.messages = []
        self.staging.start_run(command, {"fixture": True}, "fixture", "fixture")

    def log(self, level, message, *, adapter="", entity_cid=""):
        self.messages.append((level, message, adapter, entity_cid))
        self.staging.log(level, message, adapter=adapter, entity_cid=entity_cid)

    def close(self, status="complete"):
        self.staging.finish_run(status, "synthetic interruption fixture")
        self.staging.close()


@contextlib.contextmanager
def _fake_adapter_path(adapter):
    metric = types.SimpleNamespace(id=METRIC, templates={"interstate_gas"})
    with mock.patch.object(pipeline, "load_adapter", return_value=adapter), \
            mock.patch.object(pipeline, "BY_ADAPTER", {ADAPTER: [metric]}), \
            mock.patch.object(pipeline, "unit_identity", return_value=dict(IDENTITY)):
        yield


def _entity() -> dict:
    return {
        "entity_key": ENTITY,
        "legal_name": "Synthetic Fault Entity",
        "template": "interstate_gas",
        "assets": [],
    }


class ActiveExceptionTests(unittest.TestCase):
    """Inject Exception/BaseException after pruning has begun in commit_unit."""

    def _fault_then_resume(self, fault):
        with tempfile.TemporaryDirectory(prefix="ferc-active-interrupt-") as td:
            root = pathlib.Path(td)
            db_path = root / "staging.sqlite"
            ledger_path = root / "ledger.json"
            _seed_last_good(db_path)

            ctx = _RunContext(db_path, ledger_path)
            adapter = _FakeAdapter()
            real_upsert = ctx.staging._upsert
            injection_count = {"n": 0}

            def inject(con, table, rows, keys):
                if table == "observations":
                    injection_count["n"] += 1
                    raise fault
                return real_upsert(con, table, rows, keys)

            with _fake_adapter_path(adapter), \
                    mock.patch.object(ctx.staging, "_upsert", side_effect=inject):
                if isinstance(fault, (KeyboardInterrupt, SystemExit)):
                    with self.assertRaises(type(fault)):
                        pipeline.run_adapter(ctx, ADAPTER, [_entity()])
                else:
                    result = pipeline.run_adapter(ctx, ADAPTER, [_entity()])
                    self.assertEqual("failed", result["status"])

            self.assertEqual(1, injection_count["n"], "fault point was not exercised")
            self.assertFalse(ctx.staging.con.in_transaction)
            row = ctx.staging.con.execute(
                "SELECT value_num,filing_id FROM observations WHERE observation_id=?",
                (OBSERVATION_ID,)).fetchone()
            self.assertEqual((100.0, "filing-last-good"), tuple(row))
            self.assertEqual(0, ctx.staging.con.execute(
                "SELECT COUNT(*) FROM observation_versions").fetchone()[0])
            self.assertEqual(0, ctx.staging.con.execute(
                "SELECT COUNT(*) FROM unit_commits WHERE run_id=?",
                (ctx.staging.run_id,)).fetchone()[0])
            checkpoint = ctx.staging.con.execute(
                "SELECT state,last_error FROM checkpoints WHERE adapter=? AND entity_cid=? "
                "AND scope_key=?", (ADAPTER, ENTITY, SCOPE_KEY)).fetchone()
            self.assertEqual("failed", checkpoint["state"])
            self.assertIn("interrupted" if isinstance(fault, (KeyboardInterrupt, SystemExit))
                          else str(fault), checkpoint["last_error"])
            self.assertEqual("failed", ctx.ledger.state(TASK))
            self.assertEqual("stale", ctx.ledger.freshness(TASK)["freshness"])

            # A later status write cannot accidentally commit the failed unit,
            # and a separate writer can acquire the database immediately.
            ctx.staging.log("info", "post-fault status durability probe")
            self.assertEqual(100.0, ctx.staging.con.execute(
                "SELECT value_num FROM observations WHERE observation_id=?",
                (OBSERVATION_ID,)).fetchone()[0])
            other = sqlite3.connect(str(db_path), isolation_level=None, timeout=1)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("ROLLBACK")
            finally:
                other.close()
            ctx.close("interrupted" if isinstance(fault, (KeyboardInterrupt, SystemExit))
                      else "failed")

            # A failed/stale unit is rerun on resume, commits once, then a
            # second resume skips it using the exact recorded identity.
            resumed = _RunContext(db_path, ledger_path, command="resume")
            adapter2 = _FakeAdapter()
            with _fake_adapter_path(adapter2):
                result = pipeline.run_adapter(resumed, ADAPTER, [_entity()])
                self.assertEqual("ok", result["status"])
                self.assertEqual(1, result["succeeded"])
                self.assertEqual(1, adapter2.retrieve_calls)
            resumed.close()

            skipped = _RunContext(db_path, ledger_path, command="resume")
            adapter3 = _FakeAdapter()
            with _fake_adapter_path(adapter3):
                result = pipeline.run_adapter(skipped, ADAPTER, [_entity()])
            self.assertEqual(1, result["resumed"])
            self.assertEqual(0, adapter3.retrieve_calls)
            self.assertEqual(1, skipped.staging.con.execute(
                "SELECT COUNT(*) FROM observations WHERE observation_id=?",
                (OBSERVATION_ID,)).fetchone()[0])
            self.assertEqual(200.0, skipped.staging.con.execute(
                "SELECT value_num FROM observations WHERE observation_id=?",
                (OBSERVATION_ID,)).fetchone()[0])
            skipped.close()

    def test_ordinary_exception_rolls_back_and_resumes(self):
        self._fault_then_resume(RuntimeError("injected ordinary write failure"))

    def test_keyboard_interrupt_rolls_back_propagates_and_resumes(self):
        self._fault_then_resume(KeyboardInterrupt())

    def test_system_exit_rolls_back_propagates_and_resumes(self):
        self._fault_then_resume(SystemExit(143))


class AbruptExitRecoveryTests(unittest.TestCase):
    """Kill a subprocess where no finally/rollback handler can execute."""

    def _run_crash(self, scenario: str, expected_rc: int, expected_trace: str,
                   committed: bool) -> None:
        with tempfile.TemporaryDirectory(prefix="ferc-abrupt-exit-") as td:
            root = pathlib.Path(td)
            db_path = root / "staging.sqlite"
            ledger_path = root / "ledger.json"
            trace_path = root / "fault.trace"
            cache_path = root / "cache"
            output_path = root / "output"
            _seed_last_good(db_path)

            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            proc = subprocess.run(
                [sys.executable, str(pathlib.Path(__file__).resolve()),
                 "--crash-child", scenario, str(db_path), str(ledger_path),
                 str(trace_path)],
                cwd=str(ROOT), env=env, capture_output=True, text=True,
                timeout=30, check=False)
            self.assertEqual(expected_rc, proc.returncode, (proc.stdout, proc.stderr))
            self.assertEqual(expected_trace, trace_path.read_text().strip())

            # Context.__init__ is the production startup bridge: it invokes
            # recover_incomplete_runs and reconciles the external JSON ledger
            # from the atomic unit marker before any resume decision is made.
            args = argparse.Namespace(force=False, offline=True, as_of="2026-09-07",
                                      budget=0, command="resume", year_from=2025,
                                      year_to=2025)
            with mock.patch.object(pipeline, "STAGING_DB", db_path), \
                    mock.patch.object(pipeline, "LEDGER", ledger_path), \
                    mock.patch.object(pipeline, "SOURCE_CACHE", cache_path), \
                    mock.patch.object(pipeline, "OUTPUT_DIR", output_path):
                ctx = pipeline.Context(args)
            self.addCleanup(fhttp.set_offline, False)

            self.assertEqual(1, len(ctx.recovery_events))
            event = ctx.recovery_events[0]
            self.assertEqual(committed, event["committed"])
            self.assertFalse(ctx.staging.con.in_transaction)
            prior_run = event["run_id"]
            run_row = ctx.staging.con.execute(
                "SELECT status,note FROM runs WHERE run_id=?", (prior_run,)).fetchone()
            self.assertEqual("interrupted", run_row["status"])
            self.assertIn("abrupt process exit", run_row["note"])
            checkpoint = ctx.staging.con.execute(
                "SELECT state,last_error FROM checkpoints WHERE adapter=? AND entity_cid=? "
                "AND scope_key=?", (ADAPTER, ENTITY, SCOPE_KEY)).fetchone()
            self.assertEqual("done" if committed else "failed", checkpoint["state"])
            recovered_state = ctx.staging.con.execute(
                "SELECT state FROM run_unit_status WHERE run_id=? AND adapter=? "
                "AND entity_cid=? AND scope_key=? ORDER BY seq DESC LIMIT 1",
                (prior_run, ADAPTER, ENTITY, SCOPE_KEY)).fetchone()[0]
            self.assertEqual("recovered_committed" if committed else "recovered_failed",
                             recovered_state)

            value = ctx.staging.con.execute(
                "SELECT value_num,filing_id FROM observations WHERE observation_id=?",
                (OBSERVATION_ID,)).fetchone()
            self.assertEqual((200.0, "filing-replacement") if committed
                             else (100.0, "filing-last-good"), tuple(value))
            markers = ctx.staging.con.execute(
                "SELECT COUNT(*) FROM unit_commits WHERE run_id=?",
                (prior_run,)).fetchone()[0]
            self.assertEqual(1 if committed else 0, markers)

            # Recovery itself writes durable status. It must not accidentally
            # commit a killed pre-commit transaction, and all OS/SQLite locks
            # must be gone so another process can write.
            ctx.staging.log("info", "post-recovery durability probe")
            again = ctx.staging.con.execute(
                "SELECT value_num FROM observations WHERE observation_id=?",
                (OBSERVATION_ID,)).fetchone()[0]
            self.assertEqual(200.0 if committed else 100.0, again)
            other = sqlite3.connect(str(db_path), isolation_level=None, timeout=1)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("ROLLBACK")
            finally:
                other.close()

            adapter = _FakeAdapter()
            with _fake_adapter_path(adapter):
                ctx.staging.start_run("resume", {"fixture": True}, "fixture", "fixture")
                result = pipeline.run_adapter(ctx, ADAPTER, [_entity()])
            if committed:
                self.assertEqual(1, result["resumed"])
                self.assertEqual(0, adapter.retrieve_calls)
            else:
                self.assertEqual(1, result["succeeded"])
                self.assertEqual(1, adapter.retrieve_calls)
            self.assertEqual(1, ctx.staging.con.execute(
                "SELECT COUNT(*) FROM observations WHERE observation_id=?",
                (OBSERVATION_ID,)).fetchone()[0])
            self.assertEqual(200.0, ctx.staging.con.execute(
                "SELECT value_num FROM observations WHERE observation_id=?",
                (OBSERVATION_ID,)).fetchone()[0])
            ctx.staging.finish_run("complete", "resume verified")
            ctx.close()

    def test_os_exit_before_commit_recovers_last_good_and_reruns(self):
        self._run_crash(
            "before_commit", 71, "reached:commit_unit:observation_upsert", False)

    def test_os_exit_during_sqlite_commit_recovers_last_good_and_reruns(self):
        self._run_crash(
            "during_commit", 72, "reached:sqlite:COMMIT-authorizer", False)

    def test_os_exit_after_commit_before_status_recovers_as_done(self):
        self._run_crash(
            "after_commit_before_status", 73,
            "reached:commit_unit:return-before-status", True)

    def test_os_exit_inside_dependency_invalidation_rolls_back_and_reruns(self):
        """Revision propagation and its unit marker share one transaction."""
        with tempfile.TemporaryDirectory(prefix="ferc-post-commit-dependency-") as td:
            root = pathlib.Path(td)
            db_path = root / "staging.sqlite"
            ledger_path = root / "ledger.json"
            trace_path = root / "fault.trace"
            _seed_last_good(db_path)
            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            proc = subprocess.run(
                [sys.executable, str(pathlib.Path(__file__).resolve()),
                 "--crash-child", "after_commit_before_dependency_update",
                 str(db_path), str(ledger_path), str(trace_path)],
                cwd=str(ROOT), env=env, capture_output=True, text=True,
                timeout=30, check=False)
            self.assertEqual(74, proc.returncode, (proc.stdout, proc.stderr))
            self.assertEqual("reached:commit_unit:transactional_invalidation",
                             trace_path.read_text().strip())

            args = argparse.Namespace(force=False, offline=True, as_of="2026-09-07",
                                      budget=0, command="resume", year_from=2025,
                                      year_to=2025)
            with mock.patch.object(pipeline, "STAGING_DB", db_path), \
                    mock.patch.object(pipeline, "LEDGER", ledger_path), \
                    mock.patch.object(pipeline, "SOURCE_CACHE", root / "cache"), \
                    mock.patch.object(pipeline, "OUTPUT_DIR", root / "output"):
                ctx = pipeline.Context(args)
            self.addCleanup(fhttp.set_offline, False)
            try:
                self.assertEqual(1, len(ctx.recovery_events))
                self.assertFalse(ctx.recovery_events[0]["committed"])
                prior_run = ctx.recovery_events[0]["run_id"]
                self.assertEqual(0, ctx.staging.con.execute(
                    "SELECT COUNT(*) FROM unit_commits WHERE run_id=?",
                    (prior_run,)).fetchone()[0])
                checkpoint = ctx.staging.con.execute(
                    "SELECT state FROM checkpoints WHERE adapter=? AND entity_cid=? "
                    "AND scope_key=?", (ADAPTER, ENTITY, SCOPE_KEY)).fetchone()[0]
                self.assertEqual("failed", checkpoint)
                self.assertEqual("failed", ctx.ledger.state(TASK))
                self.assertFalse(ctx.ledger.resumable(TASK, IDENTITY)[0])
                replacement_state = ctx.staging.con.execute(
                    "SELECT value_num,filing_id FROM observations WHERE observation_id=?",
                    (OBSERVATION_ID,)).fetchone()
                self.assertEqual((100.0, "filing-last-good"), tuple(replacement_state))
                dependency_state = ctx.staging.con.execute(
                    "SELECT version_status FROM observations WHERE observation_id=?",
                    (DERIVED_OBSERVATION_ID,)).fetchone()[0]
                self.assertEqual("original", dependency_state)

                # The failed/stale unit is eligible for a real resume.  The
                # successful replacement and transitive invalidation then cross
                # the atomic marker boundary together.
                adapter = _FakeAdapter(supersedes=True)
                with _fake_adapter_path(adapter):
                    ctx.staging.start_run(
                        "resume", {"fixture": True}, "fixture", "fixture")
                    result = pipeline.run_adapter(ctx, ADAPTER, [_entity()])
                self.assertEqual(1, result["succeeded"])
                self.assertEqual(1, adapter.retrieve_calls)
                self.assertEqual((200.0, "filing-replacement"), tuple(
                    ctx.staging.con.execute(
                        "SELECT value_num,filing_id FROM observations "
                        "WHERE observation_id=?", (OBSERVATION_ID,)).fetchone()))
                self.assertEqual("superseded", ctx.staging.con.execute(
                    "SELECT version_status FROM observations WHERE observation_id=?",
                    (DERIVED_OBSERVATION_ID,)).fetchone()[0])
            finally:
                ctx.close()


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--crash-child":
    _crash_child(*sys.argv[2:6])
elif __name__ == "__main__":
    unittest.main()
