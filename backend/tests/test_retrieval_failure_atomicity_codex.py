"""Entity-unit atomicity from retrieval writes through canonical publication.

Some real adapters persist filing/fact/document rows while ``retrieve()`` walks
the source inventory.  A later required-subitem or cache-miss gate can reject
the entity.  These controls prove that such provisional raw rows do not leak
into the last-good database and that cancellation remains BaseException-safe.

All fixtures use a disposable SQLite store and drive :func:`run.run_adapter`,
the production orchestration function.  Marker counters prove each injected
failure point was actually reached; absence of a row alone is not proof.
"""

from __future__ import annotations

import contextlib
import io
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
from ferclib.ledger import Ledger, identity_digest  # noqa: E402
from ferclib.staging import Staging  # noqa: E402


ADAPTER = "retrieval_atomicity_fixture"
ENTITY = "CID-RETRIEVAL-ATOMICITY"
METRIC = "retrieval_atomicity_metric"
SOURCE = "synthetic_retrieval_fixture"
OLD_FILING = "filing-last-good"
NEW_FILING = "filing-provisional"
OLD_DOCUMENT = "document-last-good"
NEW_DOCUMENT = "document-provisional"
OLD_FACT = "fact-last-good"
NEW_FACT = "fact-provisional"
OBSERVATION = "obs-retrieval-atomicity"
OLD_SLOT = "slot-last-good"
NEW_SLOT = "slot-provisional"
SCOPE = "legal entity CID-RETRIEVAL-ATOMICITY; synthetic fixture"
TASK = f"{ADAPTER}:{ENTITY}"
IDENTITY = {
    "adapter": ADAPTER,
    "entity_key": ENTITY,
    "year_from": 2025,
    "year_to": 2025,
    "code_version": "retrieval-atomicity-fixture-v1",
    "registry_version": "retrieval-atomicity-fixture-v1",
    "config_digest": "retrieval-atomicity-config-v1",
    "adapter_digest": "retrieval-atomicity-adapter-v1",
}
SCOPE_KEY = Staging.scope_key_for(2025, 2025, identity_digest(IDENTITY))


def _entity() -> dict:
    return {
        "entity_key": ENTITY,
        "legal_name": "Synthetic Retrieval Atomicity Entity",
        "template": "interstate_gas",
        "assets": [],
    }


def _filing(filing_id: str) -> dict:
    old = filing_id == OLD_FILING
    return {
        "source_system": SOURCE,
        "filing_id": filing_id,
        "entity_key": ENTITY,
        "form": "Synthetic retrieval fixture",
        "accession_number": "old-accession" if old else "new-accession",
        "reporting_year": 2025,
        "reporting_period": "annual",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
        "filed_date": "2026-01-01" if old else "2026-02-01",
        "submitted_on": "2026-01-01" if old else "2026-02-01",
        "content_hash": ("1" if old else "2") * 64,
        "is_canonical": 1,
        "canonical_reason": "synthetic fixture",
        "version_status": "original" if old else "revised",
        "supersedes_filing_id": None if old else OLD_FILING,
        "data_origin": "document",
        "retrieved_at": "2026-09-10T00:00:00+00:00",
        "first_seen_at": "2026-09-10T00:00:00+00:00",
        "source_url": f"https://www.ferc.gov/synthetic/{filing_id}",
    }


def _fact(filing_id: str) -> dict:
    old = filing_id == OLD_FILING
    return {
        "source_system": SOURCE,
        "filing_id": filing_id,
        "source_fact_id": OLD_FACT if old else NEW_FACT,
        "document_order": 1,
        "concept_local": "SyntheticValue",
        "value_as_filed": "100" if old else "200",
        "is_nil": 0,
        "period_class": "annual",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
        "current_or_prior": "current",
    }


def _document(filing_id: str) -> dict:
    old = filing_id == OLD_FILING
    document_id = OLD_DOCUMENT if old else NEW_DOCUMENT
    return {
        "document_id": document_id,
        "source_system": SOURCE,
        "filing_id": filing_id,
        "accession_number": "old-accession" if old else "new-accession",
        "attachment_id": "1",
        "title": "last good" if old else "provisional",
        "media_type": "text/plain",
        "byte_size": 3,
        "content_hash": ("a" if old else "b") * 64,
        "cache_path": f"objects/{document_id}",
        "text_layer": "yes",
        "availability": "retrieved",
        "retrieved_at": "2026-09-10T00:00:00+00:00",
        "source_url": f"https://www.ferc.gov/synthetic/{document_id}",
    }


def _observation(value: int, filing_id: str) -> dict:
    return {
        "observation_id": OBSERVATION,
        "entity_key": ENTITY,
        "metric_id": METRIC,
        "source_regime": SOURCE,
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
        "version_status": "original" if value == 100 else "revised",
        "validation": "validated",
        "source_system": SOURCE,
        "filing_id": filing_id,
        "source_fact_id": OLD_FACT if value == 100 else NEW_FACT,
        "document_id": OLD_DOCUMENT if value == 100 else NEW_DOCUMENT,
        "registry_version": "retrieval-atomicity-fixture-v1",
        "qa_flags": "synthetic_fixture_only",
        "review_status": "",
    }


def _expected(slot_id: str) -> dict:
    return {
        "slot_id": slot_id,
        "entity_key": ENTITY,
        "template": "interstate_gas",
        "metric_id": METRIC,
        "source_regime": SOURCE,
        "period_basis": "annual",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
        "reporting_year": 2025,
        "reporting_period": "annual",
        "scope": SCOPE,
        "requirement": "REQUIRED",
        "requirement_evidence": "synthetic fixture",
        "frozen_at": "2026-09-10T00:00:00+00:00",
    }


def _seed_last_good(db_path: pathlib.Path) -> None:
    store = Staging(db_path)
    try:
        store.write_entities([{
            "entity_key": ENTITY,
            "cid": ENTITY,
            "legal_name": "Synthetic Retrieval Atomicity Entity",
        }])
        store.start_run("seed", {"fixture": True}, "fixture", "fixture")
        store.write_filing_bundle(
            _filing(OLD_FILING), facts=[_fact(OLD_FILING)],
            documents=[_document(OLD_FILING)])
        store.freeze_expected([_expected(OLD_SLOT)])
        store.commit_unit(
            adapter=ADAPTER,
            entity_key=ENTITY,
            metric_ids=[METRIC],
            year_from=2025,
            year_to=2025,
            observations=[_observation(100, OLD_FILING)],
            edges=[],
            scope_key=SCOPE_KEY,
            input_digest="last-good-digest",
            identity=IDENTITY,
        )
        store.finish_run("complete", "last-good fixture")
    finally:
        store.close()


class _FixtureAdapter:
    """Writes raw rows exactly as the production adapters do during retrieve."""

    def __init__(self, fault: str = ""):
        self.fault = fault
        self.reached = {"retrieve_write": 0, "canonicalise": 0}

    def retrieve(self, ctx, _entity_row, *, year_from, year_to):
        del year_from, year_to
        ctx.staging.write_filing_bundle(
            _filing(NEW_FILING), facts=[_fact(NEW_FILING)],
            documents=[_document(NEW_FILING)])
        self.reached["retrieve_write"] += 1
        if self.fault == "subitem":
            ctx.staging.checkpoint(
                ADAPTER, ENTITY, "required-subitem", "failed",
                error="injected required subitem failure")
        elif self.fault == "cache":
            ctx.client.cache_misses.append("synthetic://required-cache-object")
        elif self.fault == "ordinary":
            raise RuntimeError("injected retrieve failure")
        elif self.fault == "keyboard":
            raise KeyboardInterrupt("injected retrieve cancellation")
        elif self.fault == "system_exit":
            raise SystemExit(143)
        return [_filing(NEW_FILING)]

    def freeze_expected(self, _ctx, _entity_row, _filings, _assets):
        return [_expected(NEW_SLOT)]

    def canonicalise(self, _ctx, _entity_row, _filings, _expected_rows):
        self.reached["canonicalise"] += 1
        if self.fault == "canonical":
            raise ValueError("injected canonicalisation failure")
        return [_observation(200, NEW_FILING)], []


def _durable_trace(path: pathlib.Path, message: str) -> None:
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (message + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


class _RunContext:
    def __init__(self, db_path: pathlib.Path, ledger_path: pathlib.Path, *,
                 command: str = "refresh", force: bool = True):
        self.args = types.SimpleNamespace(
            command=command, year_from=2025, year_to=2025,
            entity=[], template=[])
        self.force = force
        self.offline = True
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
        self.staging.finish_run(status, "retrieval atomicity fixture")
        self.staging.close()


@contextlib.contextmanager
def _production_adapter_path(adapter, identity=IDENTITY):
    metric = types.SimpleNamespace(id=METRIC, templates={"interstate_gas"})
    with mock.patch.object(pipeline, "load_adapter", return_value=adapter), \
            mock.patch.object(pipeline, "BY_ADAPTER", {ADAPTER: [metric]}), \
            mock.patch.object(pipeline, "unit_identity", return_value=dict(identity)):
        yield


def _raw_snapshot(store: Staging) -> dict:
    return {
        "filings": [tuple(r) for r in store.con.execute(
            "SELECT filing_id,entity_key FROM filings ORDER BY filing_id")],
        "facts": [tuple(r) for r in store.con.execute(
            "SELECT filing_id,source_fact_id,value_as_filed FROM source_facts "
            "ORDER BY filing_id,source_fact_id")],
        "documents": [tuple(r) for r in store.con.execute(
            "SELECT filing_id,document_id,title FROM documents ORDER BY filing_id,document_id")],
        "observations": [tuple(r) for r in store.con.execute(
            "SELECT observation_id,value_num,filing_id,source_fact_id,document_id "
            "FROM observations ORDER BY observation_id")],
        "expected": [r[0] for r in store.con.execute(
            "SELECT slot_id FROM coverage_expected ORDER BY slot_id")],
    }


def _crash_child(scenario: str, db_name: str, ledger_name: str,
                 trace_name: str) -> None:
    """Terminate the process at the real run_adapter outer-unit boundary."""
    db_path = pathlib.Path(db_name)
    ledger_path = pathlib.Path(ledger_name)
    trace_path = pathlib.Path(trace_name)
    ctx = _RunContext(db_path, ledger_path)
    adapter = _FixtureAdapter()
    real_retrieve = adapter.retrieve
    if scenario == "before_outer_commit":
        def exit_after_raw_write(*args, **kwargs):
            result = real_retrieve(*args, **kwargs)
            _durable_trace(trace_path, "reached:run_adapter:retrieval-raw-write")
            os._exit(81)

        adapter.retrieve = exit_after_raw_write

    elif scenario == "during_outer_commit":
        transaction_opcode = getattr(sqlite3, "SQLITE_TRANSACTION", 22)

        def authorizer(action, argument1, _argument2, _database, _trigger):
            if action == transaction_opcode and str(argument1).upper() == "COMMIT":
                _durable_trace(trace_path, "reached:run_adapter:outer-COMMIT")
                os._exit(82)
            return sqlite3.SQLITE_OK

        def arm_after_retrieve(*args, **kwargs):
            result = real_retrieve(*args, **kwargs)
            # Arm only after run_adapter's durable in-progress status and all
            # retrieval-time savepoints. Otherwise the authorizer would stop an
            # earlier status COMMIT rather than the outer unit COMMIT under test.
            ctx.staging.con.set_authorizer(authorizer)
            return result

        adapter.retrieve = arm_after_retrieve

    elif scenario != "after_outer_commit_before_status":
        raise AssertionError(f"unknown crash scenario {scenario!r}")

    with contextlib.ExitStack() as stack:
        stack.enter_context(_production_adapter_path(adapter))
        if scenario == "after_outer_commit_before_status":
            real_unit = pipeline._retrieve_and_commit_unit

            def exit_after_unit_commit(*args, **kwargs):
                result = real_unit(*args, **kwargs)
                _durable_trace(
                    trace_path, "reached:run_adapter:outer-commit-before-status")
                os._exit(83)

            stack.enter_context(mock.patch.object(
                pipeline, "_retrieve_and_commit_unit", side_effect=exit_after_unit_commit))
        pipeline.run_adapter(ctx, ADAPTER, [_entity()])
    raise AssertionError("abrupt-exit injection did not terminate the child")


class NestedTransactionTests(unittest.TestCase):
    def test_caught_ordinary_nested_failure_rolls_back_only_its_savepoint(self):
        with tempfile.TemporaryDirectory(prefix="ferc-nested-savepoint-") as td:
            store = Staging(pathlib.Path(td) / "nested.sqlite")
            try:
                with store.transaction():
                    store.write_entities([{
                        "entity_key": "A", "legal_name": "outer before",
                    }])
                    with self.assertRaisesRegex(RuntimeError, "inner ordinary"):
                        with store.transaction():
                            store.write_entities([{
                                "entity_key": "B", "legal_name": "inner rollback",
                            }])
                            raise RuntimeError("inner ordinary")
                    store.write_entities([{
                        "entity_key": "C", "legal_name": "outer after",
                    }])
                self.assertEqual(
                    ["A", "C"],
                    [r[0] for r in store.con.execute(
                        "SELECT entity_key FROM entities ORDER BY entity_key")],
                )
                self.assertFalse(store.con.in_transaction)
            finally:
                store.close()

    def test_keyboard_interrupt_unwinds_nested_and_outer_transactions(self):
        with tempfile.TemporaryDirectory(prefix="ferc-nested-keyboard-") as td:
            store = Staging(pathlib.Path(td) / "nested.sqlite")
            try:
                with self.assertRaises(KeyboardInterrupt):
                    with store.transaction():
                        store.write_entities([{
                            "entity_key": "A", "legal_name": "outer rollback",
                        }])
                        with store.transaction():
                            store.write_entities([{
                                "entity_key": "B", "legal_name": "inner rollback",
                            }])
                            raise KeyboardInterrupt("nested cancellation")
                self.assertEqual(0, store.con.execute(
                    "SELECT COUNT(*) FROM entities").fetchone()[0])
                self.assertFalse(store.con.in_transaction)
            finally:
                store.close()


class AbruptOuterBoundaryTests(unittest.TestCase):
    """Process death around the actual outer COMMIT used by run_adapter."""

    def _run_crash(self, scenario: str, expected_rc: int, expected_trace: str,
                   committed: bool) -> None:
        with tempfile.TemporaryDirectory(prefix="ferc-retrieval-abrupt-") as td:
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
                 "--crash-child", scenario, str(db_path), str(ledger_path),
                 str(trace_path)],
                cwd=str(ROOT), env=env, capture_output=True, text=True,
                timeout=30, check=False)
            self.assertEqual(expected_rc, proc.returncode, (proc.stdout, proc.stderr))
            self.assertEqual(expected_trace, trace_path.read_text().strip())

            store = Staging(db_path)
            try:
                prior_run = store.con.execute(
                    "SELECT run_id FROM runs WHERE status='running' "
                    "ORDER BY started_at DESC LIMIT 1").fetchone()[0]
                self.assertFalse(store.con.in_transaction)
                self.assertEqual(1 if committed else 0, store.con.execute(
                    "SELECT COUNT(*) FROM unit_commits WHERE run_id=?",
                    (prior_run,)).fetchone()[0])
                self.assertEqual(1 if committed else 0, store.con.execute(
                    "SELECT COUNT(*) FROM run_input_inventory WHERE run_id=?",
                    (prior_run,)).fetchone()[0])
                self.assertEqual({OLD_FILING, NEW_FILING} if committed else {OLD_FILING}, {
                    r[0] for r in store.con.execute("SELECT filing_id FROM filings")})
                value = tuple(store.con.execute(
                    "SELECT value_num,filing_id FROM observations WHERE observation_id=?",
                    (OBSERVATION,)).fetchone())
                self.assertEqual((200.0, NEW_FILING) if committed
                                 else (100.0, OLD_FILING), value)

                events = store.recover_incomplete_runs()
                self.assertEqual(1, len(events))
                self.assertEqual(committed, events[0]["committed"])
                checkpoint = store.con.execute(
                    "SELECT state FROM checkpoints WHERE adapter=? AND entity_cid=? "
                    "AND scope_key=?", (ADAPTER, ENTITY, SCOPE_KEY)).fetchone()[0]
                self.assertEqual("done" if committed else "failed", checkpoint)
                recovered = store.con.execute(
                    "SELECT state FROM run_unit_status WHERE run_id=? AND adapter=? "
                    "AND entity_cid=? AND scope_key=? ORDER BY seq DESC LIMIT 1",
                    (prior_run, ADAPTER, ENTITY, SCOPE_KEY)).fetchone()[0]
                self.assertEqual(
                    "recovered_committed" if committed else "recovered_failed",
                    recovered)

                other = sqlite3.connect(str(db_path), isolation_level=None, timeout=1)
                try:
                    other.execute("BEGIN IMMEDIATE")
                    other.execute("ROLLBACK")
                finally:
                    other.close()
            finally:
                store.close()

            # Whether recovery found the old or new generation, another real
            # unit run can acquire the database and finish deterministically.
            restarted = _RunContext(db_path, ledger_path)
            adapter = _FixtureAdapter()
            try:
                with _production_adapter_path(adapter):
                    result = pipeline.run_adapter(restarted, ADAPTER, [_entity()])
                self.assertEqual("ok", result["status"])
                self.assertEqual(1, result["succeeded"])
                self.assertEqual((200.0, NEW_FILING), tuple(
                    restarted.staging.con.execute(
                        "SELECT value_num,filing_id FROM observations "
                        "WHERE observation_id=?", (OBSERVATION,)).fetchone()))
            finally:
                restarted.close("complete")

    def test_process_exit_before_outer_commit_keeps_last_good_generation(self):
        self._run_crash(
            "before_outer_commit", 81,
            "reached:run_adapter:retrieval-raw-write", False)

    def test_process_exit_during_outer_commit_keeps_last_good_generation(self):
        self._run_crash(
            "during_outer_commit", 82,
            "reached:run_adapter:outer-COMMIT", False)

    def test_process_exit_after_outer_commit_recovers_committed_generation(self):
        self._run_crash(
            "after_outer_commit_before_status", 83,
            "reached:run_adapter:outer-commit-before-status", True)


class RetrievalUnitAtomicityTests(unittest.TestCase):
    def _new_fixture(self):
        temp = tempfile.TemporaryDirectory(prefix="ferc-retrieval-unit-")
        self.addCleanup(temp.cleanup)
        root = pathlib.Path(temp.name)
        db_path = root / "staging.sqlite"
        ledger_path = root / "ledger.json"
        _seed_last_good(db_path)
        return db_path, ledger_path

    def _run_fault(self, fault: str):
        db_path, ledger_path = self._new_fixture()
        ctx = _RunContext(db_path, ledger_path)
        adapter = _FixtureAdapter(fault)
        before = _raw_snapshot(ctx.staging)
        with _production_adapter_path(adapter), contextlib.redirect_stderr(io.StringIO()):
            if fault == "keyboard":
                with self.assertRaises(KeyboardInterrupt):
                    pipeline.run_adapter(ctx, ADAPTER, [_entity()])
                result = None
                final_state = "interrupted"
            elif fault == "system_exit":
                with self.assertRaises(SystemExit) as raised:
                    pipeline.run_adapter(ctx, ADAPTER, [_entity()])
                self.assertEqual(143, raised.exception.code)
                result = None
                final_state = "interrupted"
            else:
                result = pipeline.run_adapter(ctx, ADAPTER, [_entity()])
                self.assertEqual("failed", result["status"])
                self.assertEqual(1, result["failed"])
                final_state = "failed"

        self.assertEqual(1, adapter.reached["retrieve_write"],
                         "retrieval raw-write fault point was not exercised")
        self.assertEqual(before, _raw_snapshot(ctx.staging),
                         "a rejected unit changed last-good raw/canonical state")
        self.assertFalse(ctx.staging.con.in_transaction)
        self.assertEqual(0, ctx.staging.con.execute(
            "SELECT COUNT(*) FROM run_input_inventory WHERE run_id=?",
            (ctx.staging.run_id,)).fetchone()[0])
        self.assertEqual(0, ctx.staging.con.execute(
            "SELECT COUNT(*) FROM unit_commits WHERE run_id=?",
            (ctx.staging.run_id,)).fetchone()[0])
        checkpoint = ctx.staging.con.execute(
            "SELECT state,last_error FROM checkpoints WHERE adapter=? AND entity_cid=? "
            "AND scope_key=?", (ADAPTER, ENTITY, SCOPE_KEY)).fetchone()
        self.assertEqual("failed", checkpoint["state"])
        states = [r[0] for r in ctx.staging.con.execute(
            "SELECT state FROM run_unit_status WHERE run_id=? AND adapter=? "
            "AND entity_cid=? AND scope_key=? ORDER BY seq",
            (ctx.staging.run_id, ADAPTER, ENTITY, SCOPE_KEY))]
        self.assertEqual(["in_progress", final_state], states)
        self.assertEqual("failed", ctx.ledger.state(TASK))
        self.assertEqual("stale", ctx.ledger.freshness(TASK)["freshness"])

        # A later status write cannot commit provisional rows, and the failed
        # path releases its writer lock for another process immediately.
        ctx.staging.log("info", "post-fault lock probe")
        other = sqlite3.connect(str(db_path), isolation_level=None, timeout=1)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
        finally:
            other.close()
        return db_path, ledger_path, ctx, adapter, result

    def test_required_subitem_failure_rolls_back_then_restart_and_unchanged_succeed(self):
        db_path, ledger_path, failed_ctx, adapter, _ = self._run_fault("subitem")
        self.assertEqual(0, adapter.reached["canonicalise"])
        failed_detail = failed_ctx.staging.con.execute(
            "SELECT detail FROM run_unit_status WHERE run_id=? AND state='failed' "
            "AND scope_key=?",
            (failed_ctx.staging.run_id, SCOPE_KEY)).fetchone()[0]
        self.assertIn("required-subitem:failed", failed_detail)
        self.assertIn("injected required subitem failure", failed_detail)
        # The retrieval-time child checkpoint rolled back with the raw data,
        # then run_adapter re-emitted its identity and exact error as STATUS
        # ONLY. No stale in-progress child pointer or partial input row leaks.
        child = failed_ctx.staging.con.execute(
            "SELECT state,last_error FROM checkpoints "
            "WHERE scope_key='required-subitem'").fetchone()
        self.assertEqual("failed", child["state"])
        self.assertIn("child state=failed", child["last_error"])
        self.assertIn("injected required subitem failure", child["last_error"])
        child_status = failed_ctx.staging.con.execute(
            "SELECT state,detail FROM run_unit_status WHERE run_id=? "
            "AND scope_key='required-subitem' ORDER BY seq DESC LIMIT 1",
            (failed_ctx.staging.run_id,)).fetchone()
        self.assertEqual("failed", child_status["state"])
        self.assertIn("injected required subitem failure", child_status["detail"])

        # The runner owns one keyed entity-level failure.  Older releases used
        # a classification-qualified key, while adapters can independently own
        # a source blocker at the same entity scope.  A successful retry must
        # retire both runner identities without touching the adapter sibling.
        runner_id = Staging.blocker_id(ADAPTER, ENTITY, "unit-failure")
        self.assertIsNone(failed_ctx.staging.con.execute(
            "SELECT resolved_at FROM blockers WHERE blocker_id=?", (runner_id,)
        ).fetchone()[0])
        legacy_id = failed_ctx.staging.open_blocker(
            ADAPTER, "execution", "legacy runner failure", scope=ENTITY,
            key="unit-failure:unclassified_execution_failure",
            exact_error="[unclassified_execution_failure] RuntimeError: legacy")
        adapter_id = failed_ctx.staging.open_blocker(
            ADAPTER, "source", "current adapter search failure", scope=ENTITY,
            key="adapter-search",
            exact_error="[unclassified_execution_failure] source-owned fixture")
        failed_ctx.close("failed")

        # Fresh process, same database and ledger: the failed unit reruns and
        # publishes raw evidence + canonical state together.
        success_ctx = _RunContext(db_path, ledger_path)
        success = _FixtureAdapter()
        with _production_adapter_path(success):
            result = pipeline.run_adapter(success_ctx, ADAPTER, [_entity()])
        self.assertEqual("ok", result["status"])
        self.assertEqual(1, result["succeeded"])
        self.assertEqual({OLD_FILING, NEW_FILING}, {
            r[0] for r in success_ctx.staging.con.execute("SELECT filing_id FROM filings")})
        self.assertEqual((200.0, NEW_FILING, NEW_FACT, NEW_DOCUMENT), tuple(
            success_ctx.staging.con.execute(
                "SELECT value_num,filing_id,source_fact_id,document_id FROM observations "
                "WHERE observation_id=?", (OBSERVATION,)).fetchone()))
        self.assertEqual(1, success_ctx.staging.con.execute(
            "SELECT COUNT(*) FROM coverage_expected WHERE slot_id=?", (NEW_SLOT,)
        ).fetchone()[0])
        self.assertEqual(1, success_ctx.staging.con.execute(
            "SELECT COUNT(*) FROM unit_commits WHERE run_id=?",
            (success_ctx.staging.run_id,)).fetchone()[0])
        self.assertEqual(1, success_ctx.staging.con.execute(
            "SELECT COUNT(*) FROM run_input_inventory WHERE run_id=?",
            (success_ctx.staging.run_id,)).fetchone()[0])
        resolutions = {row["blocker_id"]: row["resolved_at"] for row in
                       success_ctx.staging.query(
                           "SELECT blocker_id,resolved_at FROM blockers")}
        self.assertIsNotNone(resolutions[runner_id])
        self.assertIsNotNone(resolutions[legacy_id])
        self.assertIsNone(resolutions[adapter_id])

        # Recreate both runner identities while the ledger still records a
        # successful unit.  The following run genuinely takes the unchanged
        # branch and must perform the same lifecycle transition.
        success_ctx.staging.open_blocker(
            ADAPTER, "execution", "current runner failure", scope=ENTITY,
            key="unit-failure",
            exact_error="[unclassified_execution_failure] RuntimeError: current")
        success_ctx.staging.open_blocker(
            ADAPTER, "execution", "legacy runner failure", scope=ENTITY,
            key="unit-failure:unclassified_execution_failure",
            exact_error="[unclassified_execution_failure] RuntimeError: legacy")
        success_ctx.close("complete")

        # A no-change refresh still consults retrieve, commits its append-only
        # input inventory, and does not archive another observation version.
        unchanged_ctx = _RunContext(db_path, ledger_path, force=False)
        unchanged_adapter = _FixtureAdapter()
        prior_versions = unchanged_ctx.staging.con.execute(
            "SELECT COUNT(*) FROM observation_versions").fetchone()[0]
        with _production_adapter_path(unchanged_adapter):
            unchanged = pipeline.run_adapter(unchanged_ctx, ADAPTER, [_entity()])
        self.assertEqual("ok", unchanged["status"])
        self.assertEqual(1, unchanged["unchanged"])
        self.assertEqual(0, unchanged["succeeded"])
        self.assertEqual(1, unchanged_adapter.reached["retrieve_write"])
        self.assertEqual(prior_versions, unchanged_ctx.staging.con.execute(
            "SELECT COUNT(*) FROM observation_versions").fetchone()[0])
        self.assertEqual(1, unchanged_ctx.staging.con.execute(
            "SELECT COUNT(*) FROM run_input_inventory WHERE run_id=?",
            (unchanged_ctx.staging.run_id,)).fetchone()[0])
        resolutions = {row["blocker_id"]: row["resolved_at"] for row in
                       unchanged_ctx.staging.query(
                           "SELECT blocker_id,resolved_at FROM blockers")}
        self.assertIsNotNone(resolutions[runner_id])
        self.assertIsNotNone(resolutions[legacy_id])
        self.assertIsNone(resolutions[adapter_id])
        unchanged_ctx.close("complete")

    def test_same_source_recanonicalises_when_execution_identity_changes(self):
        db_path, ledger_path = self._new_fixture()

        first_ctx = _RunContext(db_path, ledger_path)
        first_adapter = _FixtureAdapter()
        with _production_adapter_path(first_adapter):
            first = pipeline.run_adapter(first_ctx, ADAPTER, [_entity()])
        self.assertEqual(1, first["succeeded"])
        self.assertEqual(1, first_adapter.reached["canonicalise"])
        first_input_digest = first_ctx.ledger.success_digest(TASK)
        first_ctx.close("complete")

        changed_identity = {
            **IDENTITY,
            "adapter_digest": "retrieval-atomicity-adapter-v2",
        }
        changed_ctx = _RunContext(db_path, ledger_path, force=False)
        changed_adapter = _FixtureAdapter()
        with _production_adapter_path(changed_adapter, changed_identity):
            changed = pipeline.run_adapter(changed_ctx, ADAPTER, [_entity()])

        self.assertEqual("ok", changed["status"])
        self.assertEqual(0, changed["unchanged"],
                         "matching source bytes must not hide changed parser code")
        self.assertEqual(1, changed["succeeded"])
        self.assertEqual(1, changed_adapter.reached["canonicalise"])
        self.assertEqual(first_input_digest, changed_ctx.ledger.success_digest(TASK))
        self.assertEqual(identity_digest(changed_identity),
                         identity_digest(changed_ctx.ledger.stored_identity(TASK)))
        changed_ctx.close("complete")

    def test_later_unit_failure_rolls_back_provisional_blocker_resolution(self):
        db_path, ledger_path = self._new_fixture()
        ctx = _RunContext(db_path, ledger_path)
        legacy_id = ctx.staging.open_blocker(
            ADAPTER, "execution", "prior runner failure", scope=ENTITY,
            key="unit-failure:prior_failure",
            exact_error="[prior_failure] RuntimeError: prior")
        adapter = _FixtureAdapter("canonical")
        with _production_adapter_path(adapter), contextlib.redirect_stderr(io.StringIO()):
            result = pipeline.run_adapter(ctx, ADAPTER, [_entity()])

        self.assertEqual("failed", result["status"])
        self.assertIsNone(ctx.staging.con.execute(
            "SELECT resolved_at FROM blockers WHERE blocker_id=?", (legacy_id,)
        ).fetchone()[0])
        ctx.close("failed")

    def test_offline_cache_miss_rolls_back_retrieval_rows(self):
        _db, _ledger, ctx, adapter, _ = self._run_fault("cache")
        self.assertEqual(0, adapter.reached["canonicalise"])
        self.assertIn("offline_cache_misses=synthetic://required-cache-object",
                      ctx.staging.con.execute(
                          "SELECT detail FROM run_unit_status WHERE run_id=? "
                          "AND state='failed'", (ctx.staging.run_id,)).fetchone()[0])
        ctx.close("failed")

    def test_ordinary_retrieve_exception_rolls_back_retrieval_rows(self):
        _db, _ledger, ctx, adapter, _ = self._run_fault("ordinary")
        self.assertEqual(0, adapter.reached["canonicalise"])
        ctx.close("failed")

    def test_canonicalisation_exception_rolls_back_raw_and_expected_rows(self):
        _db, _ledger, ctx, adapter, _ = self._run_fault("canonical")
        self.assertEqual(1, adapter.reached["canonicalise"])
        self.assertEqual(0, ctx.staging.con.execute(
            "SELECT COUNT(*) FROM coverage_expected WHERE slot_id=?", (NEW_SLOT,)
        ).fetchone()[0])
        ctx.close("failed")

    def test_keyboard_interrupt_rolls_back_and_propagates(self):
        _db, _ledger, ctx, adapter, _ = self._run_fault("keyboard")
        self.assertEqual(0, adapter.reached["canonicalise"])
        ctx.close("interrupted")

    def test_system_exit_rolls_back_and_propagates(self):
        _db, _ledger, ctx, adapter, _ = self._run_fault("system_exit")
        self.assertEqual(0, adapter.reached["canonicalise"])
        ctx.close("interrupted")


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--crash-child":
    _crash_child(*sys.argv[2:6])
elif __name__ == "__main__":
    unittest.main()
