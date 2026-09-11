"""Build-A database regressions for the 34 original audit exceptions.

This module is deliberately database-only and non-mutating.  It does not import
the implementation under test, does not derive expected values with production
selectors, and refuses to fall back to a repository database.  The caller must
set ``FERC_STAGING_DB`` to the completed disposable Build-A database.

The fixed expectations below are transcribed from the independent audit's
``exceptions_34.json`` and record-level reconciliations (audit evidence SHA-256
``c7e3626ffa2d2d79333668f06dfb3f7aa23b8a84469653670293ecedf866c4ee``).
The source register shipped in ``inputs/audit_baseline`` is itself pinned below,
so a changed or incomplete exception population fails rather than silently
changing the test oracle.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sqlite3
import unittest
from decimal import Decimal
from urllib.parse import quote


ROOT = pathlib.Path(__file__).resolve().parents[1]
EXCEPTIONS_PATH = ROOT / "inputs" / "audit_baseline" / "exceptions_34.json"
EXCEPTIONS_SHA256 = "3e0c9584690469df611bbcce30993cdd5b0ebb0269d2b2b1a1da6f55e667e35f"


# accession: original exception, defect, native CID, raw-source identity,
# decoded row/source-fact count, current observation count, canonical state.
IOC_PARSER_19 = {
    "20240102-5300": ("blk-14a56caaab90cb00", "utf8_bom", "C000626", "ae32aa82f0ffab9b902a9e3aa4b13a67640ec5ebb732f082a706043d8cbb1f2a", 32316, "utf-8-sig", 488, 488, 16, 1),
    "20240401-5299": ("blk-1fdc86170bf76cff", "utf8_bom", "C000626", "31e5221abc98902d4eb08c9f9bb4ff3f5d1605b1b6a1aa5658e44240adea5a5a", 30839, "utf-8-sig", 468, 468, 17, 1),
    "20240701-5096": ("blk-05a8dedba157934e", "utf8_bom", "C000626", "47fcad9455bea5e15edc736bec1142209f1892b41a58e503735ccc4472934ab1", 32289, "utf-8-sig", 488, 488, 17, 1),
    "20241001-5148": ("blk-65725cb603ec902d", "utf8_bom", "C000626", "70641eedd9862c122016f15cfd78a4d7394540c64b27e78d4d6a9bf5a1fbee75", 32236, "utf-8-sig", 487, 487, 17, 1),
    "20250102-5086": ("blk-eb9f11f46f600233", "utf8_bom", "C000626", "7ea9423a5daaaad469113b872c22cabc90e34dd2578ec2e796e75f73b2cd6827", 36189, "utf-8-sig", 548, 548, 17, 1),
    "20250401-5141": ("blk-75cce911464fd9f7", "utf8_bom", "C000626", "3bf5b7cd435c23b1bd34da3f5804d6cbb23897a043e703027694da125bf3fb12", 36578, "utf-8-sig", 555, 555, 17, 1),
    "20250701-5124": ("blk-acacbee7fb646a01", "utf8_bom", "C000626", "c7ad9df1342a5d36572819ce0305f0186e2509480b85f1bea80ad42833edc80f", 32815, "utf-8-sig", 498, 498, 17, 1),
    "20251001-5109": ("blk-23902889f085cfa0", "utf8_bom", "C000626", "1c950c6f32076218f939c58c76cd5f0e3ac5212645259d0c501a8c53894a002a", 32756, "utf-8-sig", 499, 499, 17, 1),
    "20260102-5087": ("blk-ccd4f5d8651f2179", "utf8_bom", "C000626", "c2e56d01a711d8e3b143a98aceae703e5ff5dca4c1152ef31cc0bc2383499c4c", 40743, "utf-8-sig", 619, 619, 17, 1),
    "20260401-5124": ("blk-c0c35027d5a35025", "utf8_bom", "C000626", "d7ad830c922c28be8fd5b3bdea6ae01cfdd58f617959d1412fbeda70b82baf42", 36273, "utf-8-sig", 552, 552, 17, 1),
    "20260701-5124": ("blk-010f11ba7ab3f034", "utf8_bom", "C000626", "8175a80fae1035f64c4234b29e2be2984599e6e0f58d6fd4628e0dbba3a54cc5", 40153, "utf-8-sig", 610, 610, 17, 1),
    "20250331-5099": ("blk-813db4fdd559d280", "preamble", "C000830", "34401d32354da79d52ef68500731e7433a6018ea719a9ad593300779a76d5af8", 1470, "utf-8", 29, 24, 17, 1),
    "20240102-5281": ("blk-283ff9fe93424302", "shifted_header", "C001031", "774680cc97e80821d082213b53ab6a5b47d75e21d8af18f9b4827ad4780387a4", 20313, "utf-8", 311, 307, 16, 1),
    "20240401-5372": ("blk-7a2d5583942ea884", "shifted_header", "C001031", "d7034a6589f8d9eeb00e99b5b24750ab320d04e04ea7aeba7971f186bfb094c0", 20335, "utf-8", 312, 308, 17, 1),
    "20240701-5084": ("blk-bcf7aaefcfb67a22", "shifted_header", "C001031", "1b12103baf52041fb25d8d4177b35e468f37868a1e2d386cabf098afc06396a2", 20996, "utf-8", 321, 317, 17, 1),
    "20241001-5033": ("blk-951b30d9725b40b2", "shifted_header", "C001031", "ee4bc424615630558f680a0870d0cf252f54d166ea182c8fed7ed3ddcc218d80", 23351, "utf-8", 356, 352, 17, 1),
    "20250102-5120": ("blk-3baeb084500d53e2", "shifted_header", "C001031", "bf6d6096e51b29a60f27261cf41e99cfdf4de68219ddaa399091b57cc7469aa4", 23290, "utf-8", 356, 352, 0, 0),
    "20250114-5047": ("blk-4e70dc991829dbe4", "shifted_header", "C001031", "9114decfb80e9f3f073b591aa2036a7cb6ae1469838ddf70d6a2666b09302571", 23284, "utf-8", 356, 352, 17, 1),
    "20241001-5327": ("blk-0e1bef5b42c6c2c8", "utf16_le", "C000087", "15b3b65168f5dd51730253e8caa4c9d85fa9a25ed95169ebcbc2444933846934", 23588, "utf-16", 202, 199, 17, 1),
}


# metadata-only accession: exception, native entity, public same-period filing.
# The audit identified 20250401-5098 as the public Gulfstream candidate; Build A
# must contain it before either formerly unresolved Gulfstream row can close.
IOC_ACCESS_5 = {
    "20250701-5067": ("blk-fda56f7e4db97a5f", "C000654", "20250709-5064", "2025-07-01"),
    "20250401-5132": ("blk-be0da85a84688d1a", "C000236", "20250401-5144", "2025-04-01"),
    "20250401-5093": ("blk-61aa163f18d6bf88", "C000087", "20250401-5098", "2025-04-01"),
    "20250401-5205": ("blk-d5a42d025ee876fa", "C000087", "20250401-5098", "2025-04-01"),
    "20260701-5096": ("blk-dd6dc6d6d74b49c4", "C001058", "20260701-5117", "2026-07-01"),
}


# (entity, year): exception, Q4 filing, Storage row count and as-filed Total_Rev.
FORM549D_FILER_YEARS = {
    ("C000826", 2024): ("blk-f8a4430ac37587d6", "18901", 27, Decimal("10812326")),
    ("C000826", 2025): ("blk-11652f800db8da1f", "20443", 19, Decimal("13346064")),
    ("C001773", 2024): ("blk-b93bb12170552fd3", "18903", 81, Decimal("8090679")),
    ("C001773", 2025): ("blk-86a5207f2d741448", "20445", 70, Decimal("7049958")),
}
FORM549D_POLICY_IDS = {"blk-c29e1a69a09275d1", "blk-df2155105afc957e"}
FORM549D_CREDENTIAL_IDS = {
    "C000435": "blk-f9fc2cc2c28a2f14",
    "C004698": "blk-6c1ae60577ce89ca",
}


# accession: exception, entity, raw source hash/bytes, delivery and storage values.
CAPACITY_2 = {
    "20240223-5073": ("blk-433ea58ae2a4a07c", "C003373", "31f9219d3da5bc260392b6e2351cde0313ab38978ade4ee714f70308d7036d94", 337391, Decimal("420"), Decimal("23.7")),
    "20240223-5075": ("blk-187aa7808a87cf34", "C001591", "8e2069eeb07fdfdca7167b323b25cf91f0c34bbf91fd82a8bd82caf9826c0040", 339731, Decimal("465"), Decimal("11.96")),
}


EXPECTED_EXCEPTION_IDS = (
    {v[0] for v in IOC_PARSER_19.values()}
    | {v[0] for v in IOC_ACCESS_5.values()}
    | {v[0] for v in FORM549D_FILER_YEARS.values()}
    | FORM549D_POLICY_IDS
    | set(FORM549D_CREDENTIAL_IDS.values())
    | {v[0] for v in CAPACITY_2.values()}
)


def _money(value) -> Decimal:
    if value in (None, ""):
        return Decimal(0)
    return Decimal(str(value))


class OriginalException34BuildA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = EXCEPTIONS_PATH.read_bytes()
        if hashlib.sha256(raw).hexdigest() != EXCEPTIONS_SHA256:
            raise AssertionError("inputs/audit_baseline/exceptions_34.json identity changed")
        cls.exceptions = json.loads(raw)
        ids = [r["blocker_id"] for r in cls.exceptions]
        if len(ids) != 34 or len(set(ids)) != 34 or set(ids) != EXPECTED_EXCEPTION_IDS:
            raise AssertionError("the exact 34-row original exception population is required")

        db_text = os.environ.get("FERC_STAGING_DB", "").strip()
        if not db_text:
            raise AssertionError("FERC_STAGING_DB is required; no repository fallback is allowed")
        db = pathlib.Path(db_text).expanduser().resolve()
        if not db.is_file() or db.stat().st_size == 0:
            raise AssertionError(f"FERC_STAGING_DB is absent or empty: {db}")
        # A non-empty WAL can contain committed pages absent from the main DB,
        # so immutable mode must refuse it.  The SHM file is only the transient
        # WAL index/lock map and contains no database pages; SQLite may leave a
        # non-empty SHM beside a fully checkpointed, zero-byte WAL.  Treating
        # that harmless index as data made this consistency guard reject stable
        # snapshots while proving nothing stronger.
        wal = pathlib.Path(str(db) + "-wal")
        if wal.exists() and wal.stat().st_size:
            raise AssertionError(
                f"refusing immutable read while a non-empty SQLite WAL exists: {wal}"
            )
        uri = f"file:{quote(str(db), safe='/')}?mode=ro&immutable=1"
        cls.db = sqlite3.connect(uri, uri=True, timeout=1)
        cls.db.row_factory = sqlite3.Row
        cls.db.execute("PRAGMA query_only=ON")

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    @classmethod
    def _one(cls, sql, params=()):
        rows = cls.db.execute(sql, params).fetchall()
        if len(rows) != 1:
            raise AssertionError(f"expected one row, got {len(rows)} for {params!r}")
        return rows[0]

    def _assert_lineage_resolves(self, observation_id):
        edges = self.db.execute(
            "SELECT * FROM lineage_edges WHERE observation_id=? ORDER BY input_order",
            (observation_id,),
        ).fetchall()
        self.assertGreater(len(edges), 0, f"{observation_id}: no lineage edges")
        for edge in edges:
            if edge["input_source_fact_id"]:
                n = self.db.execute(
                    "SELECT count(*) FROM source_facts "
                    "WHERE source_system=? AND filing_id=? AND source_fact_id=?",
                    (edge["input_source_system"], edge["input_filing_id"], edge["input_source_fact_id"]),
                ).fetchone()[0]
                self.assertEqual(n, 1, f"{observation_id}: dangling source-fact edge")
            elif edge["input_population_id"]:
                n = self.db.execute(
                    "SELECT count(*) FROM lineage_populations WHERE population_id=?",
                    (edge["input_population_id"],),
                ).fetchone()[0]
                self.assertEqual(n, 1, f"{observation_id}: dangling population edge")
            elif edge["input_observation_id"]:
                n = self.db.execute(
                    "SELECT count(*) FROM observations WHERE observation_id=?",
                    (edge["input_observation_id"],),
                ).fetchone()[0]
                self.assertEqual(n, 1, f"{observation_id}: dangling observation edge")
            else:
                self.fail(f"{observation_id}: lineage edge has no resolvable input identity")

    def test_a14_ioc_parser_19_exact_occurrences_and_lineage(self):
        for accession, expected in IOC_PARSER_19.items():
            (exception_id, defect, entity, source_hash, source_bytes, encoding,
             rows_total, fact_count, observation_count, is_canonical) = expected
            with self.subTest(exception_id=exception_id, accession=accession):
                filing = self._one(
                    "SELECT * FROM filings WHERE source_system='eLibrary' AND filing_id=?",
                    (accession,),
                )
                self.assertEqual(filing["accession_number"], accession)
                self.assertEqual(filing["entity_key"], entity)
                self.assertEqual(filing["form"], "Form 549B IOC")
                self.assertEqual(filing["content_hash"], source_hash)
                self.assertEqual(filing["is_canonical"], is_canonical)

                document = self._one(
                    "SELECT * FROM documents WHERE source_system='eLibrary' "
                    "AND filing_id=? AND content_hash=?",
                    (accession, source_hash),
                )
                self.assertEqual(document["byte_size"], source_bytes)
                self.assertEqual(document["availability"], "retrieved")

                header = self._one(
                    "SELECT * FROM source_facts WHERE source_system='eLibrary' "
                    "AND filing_id=? AND concept_local='ioc_header_record'",
                    (accession,),
                )
                meta = json.loads(header["typed_dims_json"])
                provenance = meta["parse_provenance"]
                self.assertEqual(meta["pipeline_id_normalised"], entity)
                self.assertEqual(meta["entity_gate"]["header_cid"], entity)
                self.assertEqual(meta["entity_gate"]["requested_entity"], entity)
                self.assertEqual(meta["entity_gate"]["result"], "accepted")
                self.assertEqual(provenance["content_sha256"], source_hash)
                self.assertEqual(provenance["byte_size"], source_bytes)
                self.assertEqual(provenance["encoding"], encoding)
                self.assertEqual(provenance["rows_total"], rows_total)
                self.assertEqual(provenance["replaced_characters"], 0)
                self.assertEqual(sum(provenance["counts"].values()), fact_count)
                if defect == "utf8_bom":
                    self.assertEqual(provenance["bom"], "UTF-8")
                    self.assertFalse(provenance["header_repair"]["applied"])
                elif defect == "utf16_le":
                    self.assertEqual(provenance["bom"], "UTF-16 LE")
                    self.assertFalse(provenance["header_repair"]["applied"])
                elif defect == "preamble":
                    self.assertEqual(provenance["rows_blank"], 5)
                    self.assertEqual(provenance["counts"]["?"], 1)
                    self.assertFalse(provenance["header_repair"]["applied"])
                else:
                    repair = provenance["header_repair"]
                    self.assertTrue(repair["applied"])
                    self.assertEqual(repair["transformation"], "drop_empty_field_at_index_1")
                    self.assertTrue(repair["original_bytes_retained"])
                    self.assertIn("H (header) record only", repair["scope"])

                actual_facts = self.db.execute(
                    "SELECT count(*) FROM source_facts WHERE source_system='eLibrary' AND filing_id=?",
                    (accession,),
                ).fetchone()[0]
                self.assertEqual(actual_facts, fact_count)
                observations = self.db.execute(
                    "SELECT * FROM observations WHERE filing_id=? ORDER BY observation_id",
                    (accession,),
                ).fetchall()
                self.assertEqual(len(observations), observation_count)

                if accession == "20250102-5120":
                    # A valid parsed occurrence is not a missing observation.  It
                    # is noncanonical because the later public occurrence is the
                    # source-supported replacement for the same snapshot.
                    self.assertEqual(observations, [])
                    replacement = self._one(
                        "SELECT * FROM filings WHERE filing_id='20250114-5047'"
                    )
                    self.assertEqual(replacement["entity_key"], entity)
                    self.assertEqual(replacement["snapshot_date"], filing["snapshot_date"])
                    self.assertEqual(replacement["is_canonical"], 1)
                    self.assertEqual(replacement["version_status"], "revised")
                    self.assertEqual(replacement["supersedes_filing_id"], accession)
                    self.assertEqual(
                        self.db.execute(
                            "SELECT count(*) FROM lineage_edges WHERE input_filing_id=?",
                            (accession,),
                        ).fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        self.db.execute(
                            "SELECT count(*) FROM lineage_populations "
                            "WHERE EXISTS (SELECT 1 FROM json_each(filing_ids) WHERE value=?)",
                            (accession,),
                        ).fetchone()[0],
                        0,
                    )
                else:
                    for observation in observations:
                        self.assertEqual(observation["entity_key"], entity)
                        self.assertEqual(observation["source_system"], "eLibrary")
                        self.assertEqual(observation["version_status"], filing["version_status"])
                        self._assert_lineage_resolves(observation["observation_id"])
                        if observation["metric_id"] != "ioc_mdq_change":
                            populations = self.db.execute(
                                "SELECT filing_ids, candidate_count, row_count, excluded_count "
                                "FROM lineage_populations WHERE observation_id=?",
                                (observation["observation_id"],),
                            ).fetchall()
                            self.assertGreater(len(populations), 0)
                            for population in populations:
                                self.assertIn(accession, json.loads(population["filing_ids"]))
                                self.assertGreaterEqual(population["candidate_count"], population["row_count"])
                                self.assertEqual(
                                    population["candidate_count"] - population["row_count"],
                                    population["excluded_count"],
                                )

    def test_a15_ioc_access_5_metadata_and_replacements(self):
        for accession, expected in IOC_ACCESS_5.items():
            exception_id, entity, replacement_id, snapshot_date = expected
            with self.subTest(exception_id=exception_id, accession=accession):
                # Metadata-N is retained as its own occurrence, never invented as
                # a downloaded filing/document/observation.
                self.assertEqual(
                    self.db.execute(
                        "SELECT count(*) FROM filings WHERE accession_number=?", (accession,)
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    self.db.execute(
                        "SELECT count(*) FROM documents WHERE accession_number=?", (accession,)
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    self.db.execute(
                        "SELECT count(*) FROM observations WHERE accession_number=?", (accession,)
                    ).fetchone()[0],
                    0,
                )

                event = self._one(
                    "SELECT * FROM events WHERE accession_number=? "
                    "AND event_type='metadata_declared_not_public'",
                    (accession,),
                )
                self.assertEqual(event["entity_key"], entity)
                self.assertIn("no request was made", event["headline"].lower())
                self.assertIn("no http status observed", event["headline"].lower())
                self.assertIn("erroneously filed", event["detail"].lower())
                event_text = (event["headline"] + " " + event["detail"]).casefold()
                self.assertNotIn("http 401", event_text)
                self.assertNotIn("status 401", event_text)

                blocker = self._one(
                    "SELECT * FROM blockers WHERE adapter='ioc' AND scope=?",
                    (f"{entity}:{accession}",),
                )
                self.assertEqual(blocker["kind"], "access")
                self.assertEqual(blocker["attempts"], "0")
                payload = json.loads(blocker["exact_error"].split("\nRESOLUTION:", 1)[0])
                self.assertEqual(payload["metadata_availability_code"], "N")
                self.assertFalse(payload["http_request_attempted"])
                self.assertIsNone(payload["http_status_observed"])
                self.assertFalse(payload["confidentiality_established"])
                blocker_text = (blocker["summary"] + " " + blocker["exact_error"]).casefold()
                self.assertNotIn("http 401", blocker_text)
                self.assertNotIn("status 401", blocker_text)

                satisfied = payload["period_satisfied_by"]
                self.assertEqual([x["filing_id"] for x in satisfied], [replacement_id])
                replacement = self._one(
                    "SELECT * FROM filings WHERE filing_id=?", (replacement_id,)
                )
                self.assertEqual(replacement["entity_key"], entity)
                self.assertEqual(replacement["snapshot_date"], snapshot_date)
                self.assertEqual(replacement["acceptance_status"], "availCode=P")
                self.assertEqual(replacement["is_canonical"], 1)
                self.assertTrue(replacement["content_hash"])
                self.assertGreater(
                    self.db.execute(
                        "SELECT count(*) FROM observations WHERE filing_id=?", (replacement_id,)
                    ).fetchone()[0],
                    0,
                )
                self.assertIsNotNone(blocker["resolved_at"])

    def test_a16_form549d_8_storage_grain_and_cache_semantics(self):
        # Four exact filer-year exceptions: independently recompute the source
        # rows, then compare the components, annual output/gate and warning.
        for key, expected in FORM549D_FILER_YEARS.items():
            entity, year = key
            exception_id, filing_id, storage_count, storage_revenue = expected
            with self.subTest(exception_id=exception_id, entity=entity, year=year):
                filing = self._one(
                    "SELECT * FROM filings WHERE source_system='DataFERC' "
                    "AND form='Form 549D' AND entity_key=? AND reporting_year=? "
                    "AND reporting_period='Q4' AND is_canonical=1",
                    (entity, year),
                )
                self.assertEqual(filing["filing_id"], filing_id)
                source_rows = [
                    json.loads(r[0])
                    for r in self.db.execute(
                        "SELECT value_as_filed FROM source_facts "
                        "WHERE source_system='DataFERC' AND filing_id=? "
                        "AND concept_local='Form549D_ShipperContractRow'",
                        (filing_id,),
                    )
                ]
                storage = [
                    r for r in source_rows
                    if str(r.get("Service_Type") or "").strip().casefold() == "storage"
                ]
                transport = [
                    r for r in source_rows
                    if str(r.get("Service_Type") or "").strip().casefold() == "transportation"
                ]
                self.assertEqual(len(storage), storage_count)
                self.assertEqual(sum((_money(r.get("Total_Rev")) for r in storage), Decimal(0)), storage_revenue)
                transport_revenue = sum((_money(r.get("Total_Rev")) for r in transport), Decimal(0))

                components = self._one(
                    "SELECT * FROM observations WHERE entity_key=? AND reporting_year=? "
                    "AND reporting_period='Q4' AND metric_id='i311_revenue_components'",
                    (entity, year),
                )
                detail = json.loads(components["value_text"])
                self.assertEqual(_money(detail["revenue_by_service_type_as_filed"]["Storage"]), storage_revenue)
                self.assertEqual(_money(detail["reported_total_transportation"]), transport_revenue)
                self.assertIn("without applying that exclusion", detail["order_735a_note"])

                annual = self._one(
                    "SELECT * FROM observations WHERE entity_key=? AND reporting_year=? "
                    "AND metric_id='i311_annual_transport_revenue'",
                    (entity, year),
                )
                warning = annual["qa_flags"] or ""
                self.assertIn("[ORDER_735A_SCOPE_DIVERGENCE]", warning)
                self.assertIn("OUT-OF-SCOPE REVENUE", warning)
                self.assertIn("excluded from this transportation figure", warning)
                self.assertIn(f"Storage ${storage_revenue:,.0f}", warning)
                if entity == "C000826" and year == 2025:
                    self.assertEqual(annual["availability"], "interpretation_blocked")
                    self.assertEqual(annual["validation"], "blocked_ambiguity")
                    self.assertIsNone(annual["value_num"])
                    self.assertIn("ambiguous contract group", annual["missing_reason"])
                    self.assertIn("GRAIN BLOCKED", warning)
                elif not transport:
                    self.assertEqual(annual["availability"], "not_applicable")
                    self.assertEqual(annual["validation"], "pass")
                    self.assertIsNone(annual["value_num"])
                    self.assertIn("no transportation-service rows", annual["missing_reason"])
                    self.assertIn("NOT substituted", annual["missing_reason"])
                else:
                    self.assertEqual(annual["availability"], "present")
                    self.assertEqual(_money(annual["value_text"]), transport_revenue)
                    population = self._one(
                        "SELECT * FROM lineage_populations WHERE observation_id=?",
                        (annual["observation_id"],),
                    )
                    self.assertEqual(population["row_count"], len(transport))
                    self.assertIn("Transportation", population["inclusion_rule"])
                    self.assertIn("Storage", population["exclusion_rule"])
                    self.assertEqual(_money(population["aggregate_value"]), transport_revenue)

                scope_blocker = self._one(
                    "SELECT * FROM blockers WHERE adapter='form549d' AND scope=?",
                    (f"{entity}:{year}",),
                )
                self.assertIn(f"${storage_revenue:,.0f}", scope_blocker["exact_error"])
                self.assertIn("NEVER consolidated", scope_blocker["exact_error"])

        # The two duplicate policy IDs consolidate to one current policy record;
        # source occurrences remain separate.  The DB only persists the included
        # universe, so independently reproduce that nested population (179), and
        # require the 544-row historical audit baseline to remain distinctly
        # labelled rather than presented as a DB recomputation.
        policy = self._one(
            "SELECT * FROM blockers WHERE adapter='form549d' "
            "AND scope='549D annual revenue scope'"
        )
        self.assertEqual(policy["human_decision_needed"], 1)
        for exception_id in FORM549D_POLICY_IDS:
            self.assertIn(exception_id, policy["exact_error"])
            self.assertEqual(
                self.db.execute(
                    "SELECT count(*) FROM blockers WHERE blocker_id=?", (exception_id,)
                ).fetchone()[0],
                0,
            )
        self.assertIn('"historical_all_occurrences"', policy["exact_error"])
        self.assertIn('"rows": 544', policy["exact_error"])
        self.assertIn('"revenue": "237017275"', policy["exact_error"])
        self.assertIn('"included_universe_2024_2025"', policy["exact_error"])

        included_count = 0
        included_revenue = Decimal(0)
        query = (
            "SELECT sf.value_as_filed FROM source_facts sf JOIN filings f "
            "ON f.source_system=sf.source_system AND f.filing_id=sf.filing_id "
            "WHERE f.source_system='DataFERC' AND f.form='Form 549D' "
            "AND f.reporting_period='Q4' AND f.reporting_year BETWEEN 2024 AND 2025 "
            "AND sf.concept_local='Form549D_ShipperContractRow'"
        )
        for row in self.db.execute(query):
            source = json.loads(row[0])
            if str(source.get("Service_Type") or "").strip().casefold() != "storage":
                continue
            amount = _money(source.get("Total_Rev"))
            if amount:
                included_count += 1
                included_revenue += amount
        self.assertEqual(included_count, 179)
        self.assertEqual(included_revenue, Decimal("39299027"))

        # The two old credential exceptions must now be satisfied from declared
        # cached Data.FERC inputs, with no false FERC-source/configuration blocker.
        expected_periods = {
            (2024, "Q1"), (2024, "Q2"), (2024, "Q3"), (2024, "Q4"),
            (2025, "Q1"), (2025, "Q2"), (2025, "Q3"), (2025, "Q4"),
            (2026, "Q1"), (2026, "Q2"),
        }
        for entity, exception_id in FORM549D_CREDENTIAL_IDS.items():
            with self.subTest(exception_id=exception_id, entity=entity):
                filings = self.db.execute(
                    "SELECT reporting_year, reporting_period, content_hash FROM filings "
                    "WHERE source_system='DataFERC' AND form='Form 549D' "
                    "AND entity_key=? AND is_canonical=1",
                    (entity,),
                ).fetchall()
                self.assertEqual({(r[0], r[1]) for r in filings}, expected_periods)
                self.assertTrue(all(r[2] for r in filings))
                self.assertGreater(
                    self.db.execute(
                        "SELECT count(*) FROM observations WHERE entity_key=? "
                        "AND source_regime='Form 549D' AND availability='present'",
                        (entity,),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    self.db.execute(
                        "SELECT count(*) FROM blockers WHERE adapter='form549d' "
                        "AND scope LIKE ? AND kind IN ('source','configuration') "
                        "AND resolved_at IS NULL",
                        (entity + "%",),
                    ).fetchone()[0],
                    0,
                )

    def test_a17_capacity_2_ocr_values_qualifiers_and_lineage(self):
        for accession, expected in CAPACITY_2.items():
            (exception_id, entity, source_hash, source_bytes,
             delivery_value, storage_value) = expected
            with self.subTest(exception_id=exception_id, accession=accession):
                filing = self._one(
                    "SELECT * FROM filings WHERE source_system='eLibrary' AND filing_id=?",
                    (accession,),
                )
                self.assertEqual(filing["entity_key"], entity)
                self.assertEqual(filing["form"], "Form 549B Capacity")
                self.assertEqual(filing["content_hash"], source_hash)
                self.assertEqual(filing["is_canonical"], 1)
                document = self._one(
                    "SELECT * FROM documents WHERE filing_id=? AND content_hash=?",
                    (accession, source_hash),
                )
                self.assertEqual(document["byte_size"], source_bytes)
                self.assertEqual(document["availability"], "retrieved")
                self.assertEqual(document["media_type"], "application/pdf")
                self.assertEqual(document["text_layer"], "no")

                facts = self.db.execute(
                    "SELECT source_fact_id, document_order, value_as_filed, typed_dims_json "
                    "FROM source_facts WHERE source_system='eLibrary' AND filing_id=? "
                    "AND concept_local='ocr_page_image_row' ORDER BY document_order",
                    (accession,),
                ).fetchall()
                self.assertGreater(len(facts), 20)
                passage = " ".join(r["value_as_filed"] for r in facts)
                compact_passage = passage.replace("off system", "off-system")
                self.assertIn(str(delivery_value), passage)
                self.assertIn("MMcf/day", passage)
                self.assertIn(str(storage_value), passage)
                self.assertIn("Bcf", passage)
                self.assertIn("base gas requirements", passage)
                self.assertIn("does not include any off-system capacity", compact_passage)
                self.assertIn("reasonably representative operating assumptions", passage)
                for fact in facts:
                    meta = json.loads(fact["typed_dims_json"])
                    self.assertIn("ocr_raw_text", meta)
                    self.assertIn("ocr_confidence", meta)
                    self.assertEqual(meta["page"], 1)

                observations = self.db.execute(
                    "SELECT * FROM observations WHERE accession_number=? "
                    "AND metric_id='cap_reported_capacity' AND value_num IS NOT NULL",
                    (accession,),
                ).fetchall()
                self.assertEqual(len(observations), 2)
                by_unit = {r["unit"]: r for r in observations}
                self.assertEqual(set(by_unit), {"MMcf/day", "Bcf"})
                self.assertEqual(_money(by_unit["MMcf/day"]["value_text"]), delivery_value)
                self.assertEqual(_money(by_unit["Bcf"]["value_text"]), storage_value)
                source_fact_text = {r["source_fact_id"]: r["value_as_filed"] for r in facts}
                for unit, observation in by_unit.items():
                    self.assertEqual(observation["entity_key"], entity)
                    self.assertEqual(observation["filing_id"], accession)
                    self.assertEqual(observation["document_id"], document["document_id"])
                    self.assertEqual(observation["method"], "document_extracted")
                    self.assertEqual(observation["validation"], "pass")
                    flags = observation["qa_flags"] or ""
                    self.assertIn("BASE GAS", flags)
                    self.assertIn("OFF-SYSTEM", flags)
                    self.assertIn("OPERATING ASSUMPTIONS", flags)
                    self.assertIn("IMAGE-ONLY SOURCE, OCR EXTRACTED AND REVIEW-CHECKED", flags)
                    self.assertIn("no unit conversion", flags)
                    # Occurrence/document identity alone is not enough: the
                    # direct source-fact link must name an OCR row in the
                    # operative value sentence, not an unrelated page heading.
                    self.assertIn(observation["source_fact_id"], source_fact_text)
                    linked_text = source_fact_text[observation["source_fact_id"]]
                    expected_value = delivery_value if unit == "MMcf/day" else storage_value
                    self.assertTrue(
                        str(expected_value) in linked_text or unit in linked_text,
                        f"{accession} {unit}: direct lineage points outside the value sentence",
                    )

                summary = self._one(
                    "SELECT * FROM observations WHERE accession_number=? "
                    "AND metric_id='cap_reported_capacity' AND value_num IS NULL",
                    (accession,),
                )
                self.assertEqual(summary["value_text"], "2 reported figures, no system total stated")
                self.assertIn("NO system-wide total", summary["qa_flags"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
