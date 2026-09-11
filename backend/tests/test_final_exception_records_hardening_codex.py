"""Paired synthetic controls for exact final 34-exception disposition gates.

All SQLite writes are to private ``:memory:`` fixtures.  No project database,
cache, draft, or generated record is opened for write.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import unittest
from decimal import Decimal

from implementation import build_final_release_records as records


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ExactExceptionFinalizerTests(unittest.TestCase):
    def _db(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript((ROOT / "ferclib" / "schema.sql").read_text(encoding="utf-8"))
        return con

    def _insert(self, con, table, row):
        columns = {value[1] for value in con.execute(f'PRAGMA table_info("{table}")')}
        row = {key: value for key, value in row.items() if key in columns}
        con.execute(
            f'INSERT INTO "{table}"({",".join(row)}) VALUES('
            + ",".join("?" for _ in row) + ")", tuple(row.values()))

    def _filing(self, con, filing_id, entity, *, snapshot, canonical=1,
                version="original", supersedes=None, acceptance="availCode=P",
                form="Form 549B IOC", source="eLibrary", year=2025, period="Q1",
                content_hash=None, period_start=None, period_end=None):
        self._insert(con, "filings", {
            "source_system": source, "filing_id": filing_id,
            "accession_number": filing_id if source == "eLibrary" else None,
            "entity_key": entity, "form": form, "reporting_year": year,
            "reporting_period": period, "period_start": period_start,
            "period_end": period_end, "snapshot_date": snapshot,
            "acceptance_status": acceptance,
            "content_hash": content_hash or ("a" * 64),
            "is_canonical": canonical, "version_status": version,
            "supersedes_filing_id": supersedes,
        })

    def _ioc_observations(self, con, filing_id, entity, count, version):
        for index in range(count):
            fact_id = f"R{index:05d}"
            self._insert(con, "source_facts", {
                "source_system": "eLibrary", "filing_id": filing_id,
                "source_fact_id": fact_id, "concept_local": "ioc_test_value",
                "value_as_filed": str(index + 1), "is_nil": 0,
            })
            observation_id = f"obs-{filing_id}-{index}"
            self._insert(con, "observations", {
                "observation_id": observation_id, "entity_key": entity,
                "metric_id": "ioc_mdq_change", "source_regime": "Form 549B IOC",
                "period_basis": "snapshot", "instant_date": "2025-01-01",
                "reporting_year": 2025, "reporting_period": "Q1",
                "scope": f"legal entity {index}", "unit": "Dth/day",
                "value_text": str(index + 1), "value_num": index + 1,
                "availability": "present", "origin": "source_native",
                "method": "reported", "version_status": version,
                "validation": "pass", "source_system": "eLibrary",
                "filing_id": filing_id, "source_fact_id": fact_id,
                "accession_number": filing_id,
            })
            self._insert(con, "lineage_edges", {
                "observation_id": observation_id, "input_order": 1,
                "input_role": "basis", "input_source_system": "eLibrary",
                "input_filing_id": filing_id, "input_source_fact_id": fact_id,
            })

    def _shifted_header_zero_fixture(self):
        con = self._db()
        exception_id = "blk-3baeb084500d53e2"
        expected = records.IOC_EXCEPTION_EXPECTATIONS[exception_id]
        (accession, _, entity, source_hash, source_bytes, encoding,
         rows_total, fact_count, _, _, _) = expected
        self._filing(con, accession, entity, snapshot="2025-01-01", canonical=0,
                     version="original", content_hash=source_hash)
        self._insert(con, "documents", {
            "document_id": "doc-old", "source_system": "eLibrary",
            "filing_id": accession, "accession_number": accession,
            "byte_size": source_bytes, "content_hash": source_hash,
            "availability": "retrieved",
        })
        provenance = {
            "content_sha256": source_hash, "byte_size": source_bytes,
            "encoding": encoding, "rows_total": rows_total,
            "replaced_characters": 0, "counts": {"H": 1, "D": fact_count - 1},
            "header_repair": {
                "applied": True, "transformation": "drop_empty_field_at_index_1",
                "original_bytes_retained": True,
                "scope": "the H (header) record only",
            },
        }
        header = {
            "pipeline_id_normalised": entity,
            "entity_gate": {"header_cid": entity, "requested_entity": entity,
                            "result": "accepted"},
            "uom_transport_state": "stated", "uom_transport": "Dth",
            "uom_storage_state": "blank", "uom_storage": "",
            "parse_provenance": provenance,
        }
        self._insert(con, "source_facts", {
            "source_system": "eLibrary", "filing_id": accession,
            "source_fact_id": "H00001", "concept_local": "ioc_header_record",
            "typed_dims_json": json.dumps(header), "value_as_filed": "H", "is_nil": 0,
        })
        for index in range(fact_count - 1):
            self._insert(con, "source_facts", {
                "source_system": "eLibrary", "filing_id": accession,
                "source_fact_id": f"D{index:05d}", "concept_local": "ioc_contract_field",
                "value_as_filed": str(index), "is_nil": 0,
            })
        self._filing(con, "20250114-5047", entity, snapshot="2025-01-01",
                     canonical=1, version="revised", supersedes=accession)
        self._ioc_observations(con, "20250114-5047", entity, 17, "revised")
        return con, exception_id

    def _access_fixture(self):
        con = self._db()
        exception_id = "blk-be0da85a84688d1a"
        accession, entity, replacement, snapshot, count = \
            records.IOC_ACCESS_EXPECTATIONS[exception_id]
        self._insert(con, "events", {
            "event_id": "metadata-event", "entity_key": entity,
            "event_class": "data_quality",
            "event_type": "metadata_declared_not_public",
            "headline": f"{accession}: no request was made and no HTTP status observed",
            "detail": "Erroneously filed; no access restriction is asserted.",
            "destination": "data_review_queue", "source_system": "eLibrary",
            "filing_id": accession, "accession_number": accession,
            "first_seen_at": "2026-09-09T00:00:00Z", "is_backfill": 1,
        })
        payload = {
            "metadata_availability_code": "N", "http_request_attempted": False,
            "http_status_observed": None, "confidentiality_established": False,
            "period_satisfied_by": [{"filing_id": replacement, "is_canonical": 1,
                                     "snapshot_date": snapshot}],
        }
        self._insert(con, "blockers", {
            "blocker_id": "new-scope-blocker", "adapter": "ioc",
            "scope": f"{entity}:{accession}", "kind": "access",
            "summary": "availCode N; no request was made and no HTTP status observed",
            "attempts": "0", "exact_error": json.dumps(payload) + "\nRESOLUTION: applied",
            "human_decision_needed": 0, "opened_at": "2026-09-09T00:00:00Z",
            "resolved_at": "2026-09-09T00:00:01Z",
        })
        self._filing(con, replacement, entity, snapshot=snapshot, canonical=1,
                     version="original", acceptance="availCode=P")
        self._ioc_observations(con, replacement, entity, count, "original")
        return con, exception_id, payload

    def _form549d_fixture(self):
        con = self._db()
        exception_id = "blk-f8a4430ac37587d6"
        entity, year, filing_id, storage_count, storage_revenue = \
            records.FORM549D_SEMANTIC_EXPECTATIONS[exception_id]
        self._filing(con, filing_id, entity, snapshot=None, canonical=1,
                     version="original", form="Form 549D", source="DataFERC",
                     year=year, period="Q4", period_start=f"{year}-10-01",
                     period_end=f"{year}-12-31")
        for index in range(storage_count):
            row = {
                "Filer_CID": entity, "Form549D_ID": filing_id,
                "Filing_Year__DASH__Quarter": f"{year}-Q4",
                "Sequence_Number": index + 1, "Service_Type": "Storage",
                "Total_Rev": int(storage_revenue) if index == 0 else 0,
            }
            self._insert(con, "source_facts", {
                "source_system": "DataFERC", "filing_id": filing_id,
                "source_fact_id": f"storage-{index}",
                "concept_local": "Form549D_ShipperContractRow",
                "value_as_filed": json.dumps(row), "is_nil": 0,
            })
        for index, amount in enumerate((10, 20), storage_count + 1):
            row = {
                "Filer_CID": entity, "Form549D_ID": filing_id,
                "Filing_Year__DASH__Quarter": f"{year}-Q4",
                "Sequence_Number": index, "Service_Type": "Transportation",
                "Total_Rev": amount,
            }
            self._insert(con, "source_facts", {
                "source_system": "DataFERC", "filing_id": filing_id,
                "source_fact_id": f"transport-{index}",
                "concept_local": "Form549D_ShipperContractRow",
                "value_as_filed": json.dumps(row), "is_nil": 0,
            })
        component_value = {
            "revenue_by_service_type_as_filed": {
                "Storage": int(storage_revenue), "Transportation": 30},
            "reported_total_transportation": 30,
            "order_735a_note": "source rows shown without applying that exclusion",
        }
        self._insert(con, "observations", {
            "observation_id": "components", "entity_key": entity,
            "metric_id": "i311_revenue_components", "source_regime": "Form 549D",
            "period_basis": "annual", "period_start": f"{year}-01-01",
            "period_end": f"{year}-12-31", "reporting_year": year,
            "reporting_period": "Q4", "scope": "same-year, same-service components",
            "unit": "as filed by component", "value_text": json.dumps(component_value),
            "availability": "present", "origin": "source_native",
            "method": "reported", "version_status": "unresolved",
            "validation": "pass", "source_system": "DataFERC",
            "filing_id": filing_id,
        })
        flags = ("[ORDER_735A_SCOPE_DIVERGENCE] OUT-OF-SCOPE REVENUE; "
                 "excluded from this transportation figure; "
                 f"Storage ${storage_revenue:,.0f}")
        self._insert(con, "observations", {
            "observation_id": "annual", "entity_key": entity,
            "metric_id": "i311_annual_transport_revenue",
            "source_regime": "Form 549D", "period_basis": "annual",
            "period_start": f"{year}-01-01", "period_end": f"{year}-12-31",
            "reporting_year": year, "reporting_period": "Q4",
            "scope": "FERC-reportable transportation services, excluding storage",
            "unit": "iso4217:USD", "value_text": "30", "value_num": 30,
            "availability": "present", "origin": "derived",
            "method": "derived", "version_status": "unresolved",
            "validation": "pass", "source_system": "DataFERC",
            "filing_id": filing_id, "qa_flags": flags,
        })
        self._insert(con, "lineage_populations", {
            "population_id": "annual-pop", "observation_id": "annual",
            "source_system": "DataFERC", "source_table": "d549_rows",
            "filing_ids": json.dumps([filing_id]),
            "inclusion_rule": "Service_Type Transportation",
            "exclusion_rule": "Storage and other services",
            "row_count": 2, "candidate_count": storage_count + 2,
            "excluded_count": storage_count,
            "member_key": "Form549D_ID:Sequence_Number",
            "member_digest": "b" * 64, "aggregate_value": "30",
            "aggregate_unit": "iso4217:USD", "created_at": "2026-09-09T00:00:00Z",
        })
        self._insert(con, "blockers", {
            "blocker_id": "new-549d-scope-blocker", "adapter": "form549d",
            "scope": f"{entity}:{year}", "kind": "semantic",
            "summary": "display decision remains",
            "exact_error": (f"{entity} {year}-Q4 reports ${storage_revenue:,.0f}; "
                            "this filer-year is NEVER consolidated with another"),
            "human_decision_needed": 1, "opened_at": "2026-09-09T00:00:00Z",
            "resolved_at": None,
        })
        return con, exception_id

    def test_parsed_noncanonical_zero_is_valid_but_wrong_native_cid_fails(self):
        con, exception_id = self._shifted_header_zero_fixture()
        result = records._verify_ioc_exception(con, exception_id)
        self.assertTrue(result["accepted"], result["failures"])
        self.assertIn("Zero observations is the expected result", result["counterevidence"])

        header = con.execute(
            "SELECT typed_dims_json FROM source_facts WHERE filing_id='20250102-5120' "
            "AND concept_local='ioc_header_record'").fetchone()[0]
        value = json.loads(header)
        value["entity_gate"]["header_cid"] = "C999999"
        con.execute(
            "UPDATE source_facts SET typed_dims_json=? WHERE filing_id='20250102-5120' "
            "AND concept_local='ioc_header_record'", (json.dumps(value),))
        bad = records._verify_ioc_exception(con, exception_id)
        self.assertFalse(bad["accepted"])
        self.assertTrue(any("native header CID" in failure for failure in bad["failures"]))
        con.close()

    def test_metadata_n_null_http_is_valid_but_invented_401_fails(self):
        con, exception_id, payload = self._access_fixture()
        result = records._verify_ioc_access_exception(con, exception_id)
        self.assertTrue(result["accepted"], result["failures"])
        self.assertIn("contradicted", result["counterevidence"])

        payload["http_status_observed"] = 401
        con.execute(
            "UPDATE blockers SET exact_error=? WHERE blocker_id='new-scope-blocker'",
            (json.dumps(payload) + "\nRESOLUTION: applied",))
        bad = records._verify_ioc_access_exception(con, exception_id)
        self.assertFalse(bad["accepted"])
        self.assertTrue(any("http_status_observed" in failure
                            for failure in bad["failures"]))
        con.close()

    def test_new_same_scope_unresolved_blocker_prevents_resolved_disposition(self):
        con, exception_id, _ = self._access_fixture()
        # The final-record builder consumes the implementation-owned draft.
        # Do not make this production-path regression depend on a duplicate
        # audit-working copy that the clean candidate materializer excludes.
        draft = json.loads((ROOT / "tests" / "fixtures" / "release_audit" /
                            "EXCEPTION_DISPOSITIONS_DRAFT.json").read_text())
        audit = {row["original_exception_id"]: {"blocker_id": row["original_exception_id"]}
                 for row in draft["rows"]}
        a17 = {"accessions": list(value[0] for value in
                                   records.CAPACITY_EXCEPTION_EXPECTATIONS.values()),
               "identity": {"path": "synthetic", "bytes": 1, "sha256": "a" * 64}}
        rows, _ = records._exception_records(con, draft, a17, audit)
        target = next(row for row in rows if row["original_exception_id"] == exception_id)
        self.assertEqual("resolved_with_public_replacement", target["disposition"])

        con.execute("UPDATE blockers SET resolved_at=NULL WHERE blocker_id='new-scope-blocker'")
        rows, _ = records._exception_records(con, draft, a17, audit)
        target = next(row for row in rows if row["original_exception_id"] == exception_id)
        self.assertEqual("data_repaired_but_persisted_blocker_open", target["disposition"])
        self.assertEqual("qualified_open", target["acceptance_result"])
        self.assertEqual(["new-scope-blocker"], [row["blocker_id"] for row in
                         target["current_output"]["matching_scope_blockers"]])
        con.close()

    def test_exact_549d_service_billing_period_passes_but_generic_value_fails(self):
        con, exception_id = self._form549d_fixture()
        result = records._verify_form549d_semantic_exception(con, exception_id)
        self.assertTrue(result["accepted"], result["failures"])
        self.assertEqual("10812326", result["checks"]["actual_storage_total_rev"])

        component = json.loads(con.execute(
            "SELECT value_text FROM observations WHERE observation_id='components'"
        ).fetchone()[0])
        component["reported_total_transportation"] = 31
        con.execute("UPDATE observations SET value_text=? WHERE observation_id='components'",
                    (json.dumps(component),))
        bad = records._verify_form549d_semantic_exception(con, exception_id)
        self.assertFalse(bad["accepted"])
        self.assertTrue(any("component output differs" in failure
                            for failure in bad["failures"]))
        con.close()

    def test_a15_and_readiness_ledger_states_are_derived_not_stale_targets(self):
        def exception(exception_id, issue_id="A15", accepted=True, checks=None):
            return {
                "original_exception_id": exception_id,
                "related_original_issue_ids": [issue_id],
                "acceptance_result": "accepted" if accepted else "qualified_open",
                "current_output": {"exact_exception_gate": {
                    "accepted": accepted, "checks": checks or {}}},
            }

        common = {
            "test_runs": {"issue_test_evidence": {
                "A15": ["tests.exact.test_access"],
                "R20-FIELD-READINESS-NOT-DATA-COMPLETENESS":
                    ["tests.exact.test_readiness"]}},
            "db_checks": {
                "identity": {"sha256": "a" * 64},
                "table_counts": {"field_status": 168},
                "field_readiness": {
                    "all_fields": {"rows": 168, "adapter_implemented": 168,
                                   "has_data_in_template": 152,
                                   "validated_in_template": 141},
                    "headline_fields": {"rows": 44, "adapter_implemented": 44,
                                        "has_data_in_template": 43,
                                        "validated_in_template": 40},
                    "basis": "synthetic exact rows",
                },
            },
            "publication": {"generation_id": "b" * 64},
            "validation": {"identity": {"sha256": "c" * 64}},
            "input_records": [],
            "a17": {"identity": {"sha256": "d" * 64}},
        }
        ledger_draft = {"rows": [{
            "issue_id": "A15", "source_row_type": "original_audit_A01_A22",
            "original_issue_ids": ["A15"], "severity": "P1",
            "root_cause": "metadata and HTTP observations were conflated",
            "targeted_test": "stale: retain two Gulfstream rows open",
            "current_disposition": "still_open", "audit_claim_preserved": True,
            "evidence": {"audit": [], "candidate": []},
        }, {
            "issue_id": "R20-FIELD-READINESS-NOT-DATA-COMPLETENESS",
            "source_row_type": "new_reaudit_finding", "original_issue_ids": ["A07"],
            "severity": "P2", "root_cause": "readiness dimensions were collapsed",
            "targeted_test": "stale: assert exact 168/151/140 and 44/43/40",
            "current_disposition": "fixed_pending_build", "audit_claim_preserved": True,
            "evidence": {"audit": [], "candidate": []},
        }]}
        access_records = [exception(exception_id)
                          for exception_id in records.IOC_ACCESS_EXPECTATIONS]
        con = self._db()
        try:
            output = records._repair_ledger_records(
                ROOT, con, ledger_draft, common["test_runs"], common["db_checks"],
                common["publication"], common["validation"], common["input_records"],
                access_records, common["a17"],
                {"A15": {"issue_id": "A15"},
                 "R20-FIELD-READINESS-NOT-DATA-COMPLETENESS": {"issue_id": "R20"}})
            by_id = {row["issue_id"]: row for row in output}
            self.assertEqual("accepted_at_build_a_boundary",
                             by_id["A15"]["acceptance_result"])
            self.assertIn("20250401-5098", by_id["A15"]["residual_limitation"])
            self.assertNotIn("retain two Gulfstream", by_id["A15"]["targeted_test"])
            readiness = by_id["R20-FIELD-READINESS-NOT-DATA-COMPLETENESS"]
            dynamic = readiness["evidence"]["dynamic_acceptance_gate"]
            self.assertEqual(152, dynamic["build_a"]["all_fields"]
                             ["has_data_in_template"])
            self.assertEqual(141, dynamic["build_a"]["all_fields"]
                             ["validated_in_template"])
            self.assertEqual(1, dynamic["bridge"]["all_data_change"])
            self.assertEqual(1, dynamic["bridge"]["all_validated_change"])
            self.assertNotIn("assert exact 168/151/140", readiness["targeted_test"])

            access_records[0] = exception(access_records[0]["original_exception_id"],
                                          accepted=False)
            output = records._repair_ledger_records(
                ROOT, con, {"rows": [ledger_draft["rows"][0]]},
                {"issue_test_evidence": {"A15": ["tests.exact.test_access"]}},
                common["db_checks"], common["publication"], common["validation"], [],
                access_records, common["a17"], {"A15": {"issue_id": "A15"}})
            self.assertEqual("qualified_at_build_a_boundary",
                             output[0]["acceptance_result"])
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
