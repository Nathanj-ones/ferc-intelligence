"""Focused contract controls for the standalone frontend export adapter."""

from __future__ import annotations

import unittest
import sqlite3
import json
import pathlib
import tempfile

import exporters
from adapters import elibrary_docs, lng
from ferclib.frontend_exports import (
    _asset_detail_mappings,
    _asset_comparison_decision,
    _contract,
    _display,
    _document_assertion_catalog,
    _document_assertion_refs,
    _enrich_event_source,
    _entity_filename,
    _source_index_payload,
    comparison_decision,
    comparison_selection_decision,
    pair_comparison_decision,
    FrontendContractViolation,
    validate_frontend_contract_files,
    write_frontend_contract,
)
from ferclib.registry import to_rows
from ferclib.registry import BY_ID


def observation(**changes):
    row = {
        "entity_key": "C000654",
        "metric_id": "gas_operating_revenues",
        "source_regime": "Form 2",
        "period_basis": "annual",
        "scope": "Transcontinental Gas Pipe Line Company, LLC consolidated gas system",
        "scope_rule": "legal-entity gas system; no ownership apportionment",
        "unit": "iso4217:USD",
        "value_num": 10.0,
        "availability": "present",
        "validation": "pass",
        "version_status": "revised",
        "review_status": "",
    }
    row.update(changes)
    return row


class FrontendContractTests(unittest.TestCase):
    def test_valid_same_type_rows_share_an_exact_comparison_key(self) -> None:
        left = comparison_decision(observation(), template="interstate_gas")
        right = comparison_decision(observation(
            entity_key="C000020",
            scope="Tennessee Gas Pipeline Company, L.L.C. consolidated gas system"),
            template="interstate_gas")
        self.assertTrue(left["eligible"])
        self.assertTrue(right["eligible"])
        self.assertTrue(pair_comparison_decision(left, right)["eligible"])
        self.assertEqual(left["group_id"], right["group_id"])
        self.assertNotEqual(left["series_id"], right["series_id"])

    def test_unregistered_metric_is_never_comparison_eligible(self) -> None:
        decision = comparison_decision(
            observation(metric_id="gas_operating_revenue"),
            template="interstate_gas")
        self.assertFalse(decision["eligible"])
        self.assertIn("metric_unregistered", decision["reasons"])

    def test_comparison_value_is_normalised_to_unit_family_base(self) -> None:
        metric = BY_ID["ioc_firm_transport_mdq"]
        common = {
            "metric_id": metric.id,
            "source_regime": "Form 549B IOC",
            "period_basis": "snapshot",
            "scope_rule": metric.scope,
        }
        dth = comparison_decision(observation(
            **common, entity_key="C000654", scope="Transco contracted firm transport",
            unit="Dth/day", value_num=1000), template="interstate_gas")
        mdth = comparison_decision(observation(
            **common, entity_key="C000020", scope="TGP contracted firm transport",
            unit="MDth/day", value_num=1), template="interstate_gas")
        self.assertTrue(dth["eligible"])
        self.assertTrue(mdth["eligible"])
        self.assertEqual(1000.0, dth["comparison_value_base"])
        self.assertEqual(1000.0, mdth["comparison_value_base"])
        self.assertEqual(dth["group_id"], mdth["group_id"])
        self.assertTrue(pair_comparison_decision(dth, mdth)["eligible"])

    def test_selection_enforces_distinct_subjects_and_two_to_four_series(self) -> None:
        decisions = [comparison_decision(observation(
            entity_key=f"C00000{i}", scope=f"Entity {i} consolidated gas system"),
            template="interstate_gas") for i in range(1, 6)]
        self.assertTrue(comparison_selection_decision(decisions[:4])["eligible"])
        too_many = comparison_selection_decision(decisions)
        self.assertFalse(too_many["eligible"])
        self.assertIn("more_than_4_series", too_many["reasons"])

        same_subject = [
            comparison_decision(observation(scope="Entity system - north"),
                                template="interstate_gas"),
            comparison_decision(observation(scope="Entity system - south"),
                                template="interstate_gas"),
        ]
        duplicate = comparison_selection_decision(same_subject)
        self.assertFalse(duplicate["eligible"])
        self.assertIn("duplicate_subject", duplicate["reasons"])

    def test_wrong_period_or_asset_type_is_not_comparable(self) -> None:
        annual = comparison_decision(observation(), template="interstate_gas")
        quarter = comparison_decision(
            observation(period_basis="quarter"), template="interstate_gas")
        liquid = comparison_decision(observation(), template="liquids")
        self.assertFalse(pair_comparison_decision(annual, quarter)["eligible"])
        self.assertFalse(pair_comparison_decision(annual, liquid)["eligible"])

    def test_review_value_and_shared_filer_mapping_remain_visible_but_gated(self) -> None:
        decision = comparison_decision(
            observation(validation="source_anomaly_review"),
            template="interstate_gas", unique_asset_mapping=False)
        self.assertFalse(decision["eligible"])
        self.assertIn("filing_entity_maps_to_multiple_asset_rows", decision["reasons"])
        self.assertIn("validation:source_anomaly_review", decision["reasons"])

    def test_unknown_scope_and_unit_do_not_become_plausible_values(self) -> None:
        decision = comparison_decision(observation(
            scope="actual regulatory/facility subset unresolved", unit=""),
            template="interstate_gas")
        self.assertFalse(decision["eligible"])
        self.assertIn("actual_scope_unresolved", decision["reasons"])
        self.assertIn("unit_family_unresolved", decision["reasons"])

    def test_open_review_is_not_comparison_eligible(self) -> None:
        decision = comparison_decision(
            observation(review_status="open"), template="interstate_gas")
        self.assertFalse(decision["eligible"])
        self.assertIn("review_status:open", decision["reasons"])

    def test_normalized_date_value_is_not_confused_with_reporting_instant(self) -> None:
        value = _display(observation(
            metric_id="single_day_peak_date", unit="(date)", value_num=None,
            value_text="Date: January 21, 2025", normalized_iso="2025-01-21",
            instant_date="2024-12-31"))
        self.assertEqual("2025-01-21", value["normalized_iso"])

    def test_asset_comparison_flag_requires_an_eligible_series(self) -> None:
        empty = _asset_comparison_decision(
            unique_asset_mapping=True, comparison_eligible_series=0)
        self.assertFalse(empty["eligible"])
        self.assertEqual("no_comparison_eligible_series", empty["reason"])
        present = _asset_comparison_decision(
            unique_asset_mapping=True, comparison_eligible_series=1)
        self.assertTrue(present["eligible"])

    def test_local_entity_filename_is_safe_and_stable(self) -> None:
        key = "NO-FERC-CID:Example Entity, L.L.C."
        self.assertEqual(_entity_filename(key), _entity_filename(key))
        self.assertNotIn(":", _entity_filename(key))
        self.assertNotIn("/", _entity_filename(key))

    def test_asset_detail_entity_paths_are_document_relative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "frontend_v1"
            entity = root / "entity_payloads" / "C000654.json"
            entity.parent.mkdir(parents=True)
            entity.write_text("{}\n", encoding="utf-8")
            asset_document = root / "asset_payloads" / "wmb-transco.json"
            asset_document.parent.mkdir()
            mappings = _asset_detail_mappings([{
                "entity_key": "C000654",
                "entity_payload_path": "entity_payloads/C000654.json",
            }])
            self.assertEqual(
                "../entity_payloads/C000654.json",
                mappings[0]["entity_payload_path"])
            self.assertEqual(
                entity.resolve(),
                (asset_document.parent / mappings[0]["entity_payload_path"]).resolve())

    def test_registry_export_carries_stored_and_display_unit_contracts(self) -> None:
        rows = to_rows()
        self.assertTrue(rows)
        for field in ("canonical_unit", "unit_family", "display_unit", "display_scale",
                      "admissible_units", "admissible_units_evidence"):
            self.assertIn(field, rows[0])
        margin = next(row for row in rows if row["metric_id"] == "operating_margin_pct")
        self.assertEqual("percent", margin["canonical_unit"])

    def test_source_index_resolves_event_and_preserves_operative_assertion(self) -> None:
        filing = {
            "source_system": "eLibrary", "filing_id": "20260102-5233",
            "entity_key": "C000826", "form": "eLibrary document",
            "accession_number": "20260102-5233",
            "source_url": "https://elibrary.ferc.gov/eLibrary/filelist?accession_num=20260102-5233",
        }
        document = {
            "document_id": "eLibrary|20260102-5233|attachment-1",
            "source_system": "eLibrary", "filing_id": "20260102-5233",
            "title": "FERC order", "source_url": "https://elibrary.ferc.gov/idmws/file/1",
        }
        assertion = {
            "document_fact_id": "dfact-example",
            "document_id": document["document_id"], "source_system": "eLibrary",
            "filing_id": "20260102-5233", "source_fact_id": None,
            "entity_key": "C000826", "assertion_type": "rate_effective_date",
            "metric_id": "rate_effective_date", "value_text": "November 1, 2022",
            "value_num": None, "unit": "(date)", "qualifier": "subject to refund",
            "scope_note": "docket RP26-24", "page": "3", "paragraph": "12",
            "char_start": 10, "char_end": 62,
            "verbatim_span": "rates are accepted effective November 1, 2022, subject to refund",
            "extraction_method": "pdf_text_span", "content_hash": "a" * 64,
            "confidence": "verified_span", "review_state": "reviewed",
            "reviewer_note": "", "first_seen_at": "2026-09-10T00:00:00Z",
        }
        catalog, lookup = _document_assertion_catalog([assertion])
        refs = _document_assertion_refs({
            "document_id": document["document_id"], "source_system": "eLibrary",
            "filing_id": "20260102-5233", "source_fact_id": None,
            "entity_key": "C000826", "metric_id": "rate_effective_date",
        }, lookup)
        self.assertEqual(["dfact-example"], refs)
        source_index = _source_index_payload(
            [filing], [document], catalog, as_of="2026-09-10", run_id="run-1")
        self.assertEqual(
            assertion["verbatim_span"],
            source_index["document_assertions"]["dfact-example"]["verbatim_span"])

        event = _enrich_event_source({
            "event_id": "evt-1", "source_system": "eLibrary",
            "filing_id": "20260102-5233", "document_id": document["document_id"],
            "accession_number": "20260102-5233",
        }, {("eLibrary", "20260102-5233"): filing},
            {document["document_id"]: document})
        self.assertEqual(document["source_url"], event["source_url"])
        self.assertEqual("resolved", event["source_resolution_status"])

    def test_assertion_refs_use_most_specific_available_identity(self) -> None:
        common = {
            "source_system": "eLibrary", "filing_id": "20260102-5233",
            "entity_key": "C000826", "metric_id": "rate_effective_date",
        }
        rows = [
            {**common, "document_fact_id": "exact-fact", "document_id": "doc-a",
             "source_fact_id": "fact-1"},
            {**common, "document_fact_id": "same-document", "document_id": "doc-a",
             "source_fact_id": "fact-2"},
            {**common, "document_fact_id": "sibling-document", "document_id": "doc-b",
             "source_fact_id": "fact-3"},
        ]
        _catalog, lookup = _document_assertion_catalog(rows)

        exact = _document_assertion_refs(
            {**common, "document_id": "doc-a", "source_fact_id": "fact-1"}, lookup)
        self.assertEqual(["exact-fact"], exact)

        document_fallback = _document_assertion_refs(
            {**common, "document_id": "doc-a", "source_fact_id": "unknown"}, lookup)
        self.assertEqual(["exact-fact", "same-document"], document_fallback)

        filing_fallback = _document_assertion_refs(common, lookup)
        self.assertEqual(
            ["exact-fact", "same-document", "sibling-document"], filing_fallback)

    def test_contract_declares_complete_provenance_dependencies(self) -> None:
        contract = _contract()
        self.assertEqual("source_index.json", contract["files"]["source_index"])
        dependencies = contract["external_csv_dependencies"]
        for key in ("metric_registry", "filing_inventory", "documents",
                    "document_facts", "lineage_edges", "lineage_populations",
                    "observation_versions", "reviewed_source_annotations",
                    "source_manifest"):
            self.assertIn(key, dependencies)

    def test_generated_contract_links_comparison_dates_events_and_assertions(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        schema = pathlib.Path(__file__).parents[1] / "ferclib" / "schema.sql"
        con.executescript(schema.read_text(encoding="utf-8"))
        con.executescript("""
            INSERT INTO entities(entity_key,cid,legal_name,parent,ticker)
              VALUES('C000654','C000654','Transcontinental Gas Pipe Line Company, LLC',
                     'The Williams Companies, Inc.','WMB');
            INSERT INTO assets(asset_id,ticker,display_name,template,cod_group,status)
              VALUES('wmb-transco','WMB','Transco','interstate_gas','post-cod','operating');
            INSERT INTO asset_entity_map(asset_id,entity_key,mapping_scope)
              VALUES('wmb-transco','C000654','whole_entity');
            INSERT INTO filings(
              source_system,filing_id,entity_key,form,accession_number,filed_date,
              is_canonical,version_status,source_url)
              VALUES('eLibrary','20260102-5233','C000654','eLibrary document',
                     '20260102-5233','2026-01-02',1,'original',
                     'https://elibrary.ferc.gov/eLibrary/filelist?accession_num=20260102-5233');
            INSERT INTO documents(
              document_id,source_system,filing_id,title,availability,source_url)
              VALUES('eLibrary|20260102-5233|attachment-1','eLibrary','20260102-5233',
                     'FERC order','retrieved','https://elibrary.ferc.gov/idmws/file/1');
            INSERT INTO observations(
              observation_id,entity_key,metric_id,source_regime,period_basis,
              instant_date,reporting_year,reporting_period,period_label,scope,scope_rule,
              unit,value_text,value_num,normalized_iso,availability,origin,method,version_status,
              validation,source_system,filing_id,document_id)
              VALUES(
                'obs-revenue','C000654','gas_operating_revenues','Form 2','annual',
                '2025-12-31',2025,'Q4','FY2025',
                'Transco consolidated gas system',
                'legal-entity gas system; no ownership apportionment','iso4217:USD',
                '1000',1000,NULL,'present','document','document_extracted','original','pass',
                'eLibrary','20260102-5233','eLibrary|20260102-5233|attachment-1'),
                ('obs-date','C000654','rate_effective_date','eLibrary document','as_of',
                '2026-01-02',2026,'Q1','as of 2026-01-02','Transco docket RP26-24',
                'the operative rate/order scope','(date)','November 1, 2022',NULL,
                '2022-11-01','present','document','document_extracted','original','pass',
                'eLibrary','20260102-5233','eLibrary|20260102-5233|attachment-1');
            INSERT INTO document_facts(
              document_fact_id,document_id,source_system,filing_id,entity_key,
              assertion_type,metric_id,value_text,unit,scope_note,page,paragraph,
              char_start,char_end,verbatim_span,extraction_method,confidence,review_state)
              VALUES('dfact-example','eLibrary|20260102-5233|attachment-1','eLibrary',
                     '20260102-5233','C000654','rate_effective_date','rate_effective_date',
                     'November 1, 2022','(date)','docket RP26-24','3','12',10,62,
                     'rates are accepted effective November 1, 2022, subject to refund',
                     'pdf_text_span','verified_span','reviewed');
            INSERT INTO events(
              event_id,entity_key,asset_ids,event_class,event_type,headline,destination,
              source_system,filing_id,accession_number,document_id,reporting_date,
              first_seen_at,is_backfill)
              VALUES('evt-1','C000654','["wmb-transco"]','rate','filed','Rate filing',
                     'investor_feed','eLibrary','20260102-5233','20260102-5233',
                     'eLibrary|20260102-5233|attachment-1','2026-01-02',
                     '2026-09-10T00:00:00Z',0);
        """)

        class Staging:
            run_id = "run-1"

            @staticmethod
            def query(sql, params=()):
                return con.execute(sql, params).fetchall()

        class Ctx:
            staging = Staging()
            as_of_iso = "2026-09-10"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            universe = tmp_path / "universe.csv"
            universe.write_text(
                "asset_id,template,entity_key,display_name,ticker,cod_group\n"
                "wmb-transco,interstate_gas,C000654,Transco,WMB,post-cod\n",
                encoding="utf-8")
            result = write_frontend_contract(Ctx(), tmp_path, universe)
            root = tmp_path / "frontend_v1"
            entity = json.loads(
                (root / "entity_payloads" / "C000654.json").read_text(encoding="utf-8"))
            by_id = {row["observation_id"]: row for row in entity["observations"]}
            self.assertEqual("2022-11-01", by_id["obs-date"]["value"]["normalized_iso"])
            self.assertEqual(
                ["dfact-example"],
                by_id["obs-date"]["source"]["document_assertion_refs"])
            self.assertTrue(by_id["obs-revenue"]["comparison"]["eligible"])
            self.assertEqual(
                "https://elibrary.ferc.gov/idmws/file/1",
                entity["events"][0]["source_url"])
            directory = json.loads((root / "assets.json").read_text(encoding="utf-8"))
            self.assertTrue(directory["assets"][0]["comparisonEligible"])
            source_index = json.loads(
                (root / "source_index.json").read_text(encoding="utf-8"))
            self.assertIn("dfact-example", source_index["document_assertions"])
            asset_path = root / directory["assets"][0]["detailPath"]
            asset_detail = json.loads(asset_path.read_text(encoding="utf-8"))
            self.assertEqual(
                ["../entity_payloads/C000654.json"], asset_detail["entity_payloads"])
            self.assertEqual(
                asset_detail["entity_payloads"][0],
                asset_detail["asset"]["entityMappings"][0]["entity_payload_path"])
            self.assertTrue(
                (asset_path.parent / asset_detail["entity_payloads"][0]).resolve().is_file())
            self.assertEqual(
                "entity_payloads/C000654.json",
                directory["assets"][0]["entityMappings"][0]["entity_payload_path"])
            self.assertEqual(7, result["files"])
            # Validate the generation as one closed publication, including the
            # CSVs that the JSON contract declares rather than embeds.
            for target in _contract()["external_csv_dependencies"].values():
                (root / target).resolve().write_text("fixture\n", encoding="utf-8")
            files = {
                str(path.relative_to(tmp_path)): path.read_bytes()
                for path in tmp_path.rglob("*") if path.is_file()
            }
            stats = validate_frontend_contract_files(files)
            self.assertEqual(1, stats["asset_payloads"])
            self.assertEqual(1, stats["entity_payloads"])

            missing = dict(files)
            missing.pop("frontend_v1/source_index.json")
            with self.assertRaisesRegex(FrontendContractViolation, "absent"):
                validate_frontend_contract_files(missing)

            stale = dict(files)
            stale["frontend_v1/asset_payloads/obsolete.json"] = b"{}\n"
            with self.assertRaisesRegex(FrontendContractViolation, "closure mismatch"):
                validate_frontend_contract_files(stale)
        con.close()

    def test_entity_summary_does_not_fan_out_across_two_asset_aliases(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE entities(entity_key TEXT PRIMARY KEY,legal_name TEXT);
            CREATE TABLE assets(asset_id TEXT PRIMARY KEY,template TEXT);
            CREATE TABLE asset_entity_map(asset_id TEXT,entity_key TEXT);
            CREATE TABLE observations(
              entity_key TEXT,availability TEXT,validation TEXT);
            INSERT INTO entities VALUES('C000608','Overland Pass Pipeline Company LLC');
            INSERT INTO assets VALUES('oke-overland-pass','liquids');
            INSERT INTO assets VALUES('wmb-overland-pass','liquids');
            INSERT INTO asset_entity_map VALUES('oke-overland-pass','C000608');
            INSERT INTO asset_entity_map VALUES('wmb-overland-pass','C000608');
            INSERT INTO observations VALUES('C000608','present','pass');
            INSERT INTO observations VALUES('C000608','source_blank','not_yet_validated');
        """)

        class Staging:
            @staticmethod
            def query(sql, params=()):
                return con.execute(sql, params).fetchall()

        class Ctx:
            staging = Staging()

        rows = exporters.entity_summary_rows(Ctx())
        self.assertEqual(1, len(rows))
        self.assertEqual(2, rows[0]["observations"])
        self.assertEqual(1, rows[0]["populated"])
        con.close()

    def test_document_adapters_separate_actual_scope_from_registry_contract(self) -> None:
        rate = BY_ID["rate_case_status"]
        rate_row = elibrary_docs._obs(
            "C000654", rate, instant="2026-01-01", value_text="accepted",
            scope=f"{rate.scope} | docket RP26-1")
        self.assertEqual(rate.scope, rate_row["scope_rule"])
        self.assertIn("RP26-1", rate_row["scope"])

        inspection = BY_ID["lng_inspection"]
        lng_row = lng._obs(
            "NO-FERC-CID:Example LNG", inspection, instant="2026-01-01",
            value_text="inspection located", value_num=None, unit="categorical",
            availability="present", scope=f"{inspection.scope} | Example LNG | findings")
        self.assertEqual(inspection.scope, lng_row["scope_rule"])
        self.assertIn("Example LNG", lng_row["scope"])


if __name__ == "__main__":
    unittest.main()
