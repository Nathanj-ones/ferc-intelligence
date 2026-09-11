"""The runner must persist the cause it actually observed, not ``source`` by default."""

from __future__ import annotations

import pathlib
import tempfile
import types
import unittest
from unittest import mock

from adapters import form549d
from ferclib import http
from ferclib.ledger import Ledger
from ferclib.staging import StagedCommitRejected, Staging
import run as pipeline


ADAPTER = "status_fixture"
ENTITY = "CID-STATUS-FIXTURE"
IDENTITY = {
    "adapter": ADAPTER,
    "entity_key": ENTITY,
    "year_from": 2025,
    "year_to": 2025,
    "code_version": "status-fixture-v1",
    "registry_version": "status-fixture-v1",
    "config_digest": "status-fixture-v1",
    "adapter_digest": "status-fixture-v1",
}


class _FailingAdapter:
    def __init__(self, exc: Exception, *, classify=False):
        self.exc = exc
        if classify:
            self.classify_retrieval_failure = form549d.classify_retrieval_failure

    def retrieve(self, _ctx, _entity, *, year_from, year_to):
        del year_from, year_to
        raise self.exc


class _Context:
    def __init__(self, root: pathlib.Path, *, offline: bool):
        self.args = types.SimpleNamespace(
            command="refresh", year_from=2025, year_to=2025)
        self.force = False
        self.offline = offline
        self.client = types.SimpleNamespace(cache_misses=[])
        self.staging = Staging(root / "status.sqlite")
        self.ledger = Ledger(root / "status.task_ledger.json")
        self.failures = []
        self.messages = []
        self.staging.start_run("refresh", {"fixture": True}, "fixture", "fixture")

    def log(self, level, message, *, adapter="", entity_cid=""):
        self.messages.append((level, message, adapter, entity_cid))

    def close(self):
        self.staging.close()


class PersistedRetrievalStatusTests(unittest.TestCase):
    def setUp(self):
        self._was_offline = http.is_offline()

    def tearDown(self):
        http.set_offline(self._was_offline)

    def _run(self, exc: Exception, *, offline=False, classify=False):
        with tempfile.TemporaryDirectory(prefix="ferc-status-fixture-") as td:
            ctx = _Context(pathlib.Path(td), offline=offline)
            adapter = _FailingAdapter(exc, classify=classify)
            metric = types.SimpleNamespace(id="status_metric",
                                           templates={"interstate_gas"})
            entity = {
                "entity_key": ENTITY,
                "legal_name": "Status Fixture",
                "template": "interstate_gas",
                "assets": [],
            }
            http.set_offline(offline)
            with mock.patch.object(pipeline, "load_adapter", return_value=adapter), \
                    mock.patch.object(pipeline, "BY_ADAPTER", {ADAPTER: [metric]}), \
                    mock.patch.object(pipeline, "unit_identity",
                                      return_value=dict(IDENTITY)):
                result = pipeline.run_adapter(ctx, ADAPTER, [entity])
            blocker = ctx.staging.con.execute(
                "SELECT kind,summary,exact_error FROM blockers "
                "WHERE adapter=? ORDER BY opened_at DESC LIMIT 1", (ADAPTER,)).fetchone()
            failures = list(ctx.failures)
            ctx.close()
        return result, blocker, failures

    def test_offline_cache_miss_persists_configuration_not_source(self):
        result, blocker, failures = self._run(
            http.OfflineCacheMiss("https://example.invalid/official"),
            offline=True, classify=True)
        self.assertEqual("failed", result["status"])
        self.assertEqual("configuration", blocker["kind"])
        self.assertIn("offline_replay_cache_miss", blocker["exact_error"])
        self.assertIn("not a ferc outage", blocker["exact_error"].lower())
        self.assertEqual("offline_replay_cache_miss", failures[0]["classification"])

    def test_actual_http_failure_is_the_positive_source_control(self):
        exc = http.FetchError("https://example.invalid/official", "HTTP 503",
                              status=503, attempts=1)
        _result, blocker, failures = self._run(exc, classify=True)
        self.assertEqual("source", blocker["kind"])
        self.assertIn("ferc_source_unavailable", blocker["exact_error"])
        self.assertEqual("ferc_source_unavailable", failures[0]["classification"])

    def test_unknown_execution_failure_is_not_promoted_to_source(self):
        _result, blocker, failures = self._run(RuntimeError("fixture code fault"))
        self.assertEqual("execution", blocker["kind"])
        self.assertIn("unclassified_execution_failure", blocker["exact_error"])
        self.assertEqual("unclassified_execution_failure",
                         failures[0]["classification"])

    def test_rejected_staged_unit_persists_validation(self):
        _result, blocker, failures = self._run(
            StagedCommitRejected("status-fixture", ["fixture malformed batch"]))
        self.assertEqual("validation", blocker["kind"])
        self.assertIn("staged_commit_rejected", blocker["exact_error"])
        self.assertEqual("staged_commit_rejected", failures[0]["classification"])


if __name__ == "__main__":
    unittest.main()
