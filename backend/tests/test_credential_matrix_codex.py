"""A16-CREDENTIAL: exercise the real 549D retrieval preconditions offline.

The earlier tests called the failure classifier with hand-written strings.  That
left a gap: the actual ``OfflineCacheMiss`` message did not contain the word
``miss``, and therefore took a different branch.  These cases start at
``form549d._resolve`` with the production ``Client`` and ``SourceCache``.  Every
socket opener is replaced by a rejecting fixture, so the suite itself never
uses the network.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock

from adapters import form549d
from adapters import oil_index
from ferclib import http


CATALOG = {
    "data-assets": [
        {
            "title": "FERC Form 549D: Quarterly 311 Transaction Report",
            "point_of_contact_email": "official.fixture@example.invalid",
            "data-sets": [
                {"id": 28, "title": "Respondent Information"},
                {"id": 29, "title": "Shipper and Contract Information"},
            ],
        },
        {
            "title": "Company Registration",
            "data-sets": [{"id": 26, "title": "FERC Identifier Listing"}],
        },
    ]
}


class _Context:
    def __init__(self, client):
        self.client = client


class CredentialAndRetrievalMatrix(unittest.TestCase):
    def setUp(self):
        self._was_offline = http.is_offline()

    def tearDown(self):
        http.set_offline(self._was_offline)
        form549d._TABLES = None

    @staticmethod
    def _client(root: pathlib.Path, *, offline: bool, max_attempts: int = 1):
        return http.Client(
            http.SourceCache(root),
            offline=offline,
            min_interval=0,
            max_attempts=max_attempts,
            timeout=1,
        )

    @staticmethod
    def _socket_forbidden(*_args, **_kwargs):
        raise AssertionError("credential-matrix test attempted a real network call")

    def test_cached_success_is_credential_free_and_never_opens_a_socket(self):
        """A cached official response is a success, not a missing-key failure."""
        with tempfile.TemporaryDirectory(prefix="ferc-a16-cache-") as td:
            root = pathlib.Path(td)
            client = self._client(root, offline=False)
            seeded_key = "matrix-cache-seed-not-a-real-credential"
            seeded_url = f"{form549d.BASE}/data-assets/?api_key={seeded_key}"
            client.cache.put(
                seeded_url,
                json.dumps(CATALOG).encode("utf-8"),
                "application/json",
                form549d.SOURCE_SYSTEM,
            )

            http.set_offline(True)
            with mock.patch("urllib.request.urlopen", side_effect=self._socket_forbidden):
                resolved = form549d._resolve(_Context(client))

            self.assertEqual(resolved["respondent_id"], 28)
            self.assertEqual(resolved["contract_id"], 29)
            self.assertEqual(resolved["registration_id"], 26)
            self.assertEqual(client.requests_made, 0)
            self.assertEqual(client.cache_misses, [])
            # URL identity is credential-independent and the seed is not stored.
            index_text = client.cache.index_path.read_text(encoding="utf-8")
            self.assertNotIn(seeded_key, index_text)
            self.assertIn("%3CREDACTED%3E", index_text)

    def test_offline_cache_miss_is_not_a_source_or_ferc_failure(self):
        """The production exception object, not a classifier-shaped string."""
        with tempfile.TemporaryDirectory(prefix="ferc-a16-miss-") as td:
            client = self._client(pathlib.Path(td), offline=True)
            with mock.patch("urllib.request.urlopen", side_effect=self._socket_forbidden):
                with self.assertRaises(http.OfflineCacheMiss) as raised:
                    form549d._resolve(_Context(client))

            status = form549d.classify_retrieval_failure(raised.exception)
            self.assertEqual(status["classification"], oil_index.CLASS_CACHE_MISS)
            self.assertEqual(status["kind"], "configuration")
            self.assertFalse(status["is_ferc_condition"])
            self.assertIn("FERC was never contacted", status["explicitly_not"])
            self.assertEqual(client.requests_made, 0)
            self.assertEqual(len(client.cache_misses), 1)

    def test_missing_key_is_local_configuration_before_any_request(self):
        """Live mode without a key says nothing about FERC availability."""
        with tempfile.TemporaryDirectory(prefix="ferc-a16-key-") as td:
            root = pathlib.Path(td)
            client = self._client(root / "cache", offline=False)

            def actual_missing_key():
                return http.api_key(env_path=root / "intentionally-missing.env")

            with mock.patch.dict(os.environ, {"FERC_API_KEY": ""}, clear=False):
                with mock.patch.object(form549d, "api_key", side_effect=actual_missing_key):
                    with mock.patch(
                        "urllib.request.urlopen", side_effect=self._socket_forbidden
                    ):
                        with self.assertRaises(http.FetchError) as raised:
                            form549d._resolve(_Context(client))

            status = form549d.classify_retrieval_failure(raised.exception)
            self.assertEqual(
                status["classification"], oil_index.CLASS_MISSING_CREDENTIAL
            )
            self.assertEqual(status["kind"], "configuration")
            self.assertFalse(status["is_ferc_condition"])
            self.assertIn("NOT a FERC outage", status["explicitly_not"])
            self.assertEqual(raised.exception.url, "<FERC_API_KEY>")
            self.assertEqual(client.requests_made, 0)
            self.assertEqual(client.cache_misses, [])

    def test_actual_http_failure_is_the_valid_source_failure_control(self):
        """Do not make the classifier so cautious that it rejects every source fault."""
        with tempfile.TemporaryDirectory(prefix="ferc-a16-http-") as td:
            client = self._client(pathlib.Path(td), offline=False)

            test_key = "matrix-http-control-not-a-real-credential"
            http_error = urllib.error.HTTPError(
                "https://api.data.ferc.gov/fixture",
                503,
                "fixture service unavailable",
                {},
                io.BytesIO(b"fixture response body"),
            )
            try:
                with mock.patch.object(form549d, "api_key", return_value=test_key):
                    with mock.patch("urllib.request.urlopen", side_effect=http_error):
                        with self.assertRaises(http.FetchError) as raised:
                            form549d._resolve(_Context(client))
            finally:
                http_error.close()

            status = form549d.classify_retrieval_failure(raised.exception)
            self.assertEqual(status["classification"], oil_index.CLASS_FERC_UNAVAILABLE)
            self.assertEqual(status["kind"], "source")
            self.assertTrue(status["is_ferc_condition"])
            self.assertEqual(raised.exception.status, 503)
            self.assertEqual(raised.exception.attempts, 1)
            self.assertEqual(client.requests_made, 1)
            self.assertNotIn(test_key, raised.exception.url)
            self.assertIn("%3CREDACTED%3E", raised.exception.url)


if __name__ == "__main__":
    unittest.main()
