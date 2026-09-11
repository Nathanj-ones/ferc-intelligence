"""Focused production tests for IOC top-five source-row lineage.

The fixture is deliberately synthetic and in-memory, but every population and
edge is built by the production ``_Lineage`` helper and every assertion calls
the production ``audit_lineage`` validator. The 47-row contract book (46 rows
with a weight and one explicitly blank) makes both the denominator and
collective top-five population exceed the per-row edge limit, so mutations
outside the stored 25-member sample cannot hide behind a small-fixture pass.
"""

from __future__ import annotations

import json
import sqlite3
import unittest

from adapters import ioc


class _Staging:
    def __init__(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(
            """
            CREATE TABLE observations (
              observation_id TEXT PRIMARY KEY,
              metric_id TEXT NOT NULL,
              entity_key TEXT NOT NULL,
              method TEXT NOT NULL,
              availability TEXT NOT NULL,
              missing_reason TEXT,
              filing_id TEXT,
              value_text TEXT,
              value_num REAL,
              unit TEXT,
              scope TEXT NOT NULL,
              candidate_count INTEGER
            );
            CREATE TABLE source_facts (
              source_system TEXT NOT NULL,
              filing_id TEXT NOT NULL,
              source_fact_id TEXT NOT NULL,
              typed_dims_json TEXT,
              PRIMARY KEY (source_system, filing_id, source_fact_id)
            );
            CREATE TABLE lineage_populations (
              population_id TEXT PRIMARY KEY,
              observation_id TEXT NOT NULL,
              source_system TEXT,
              source_table TEXT,
              filing_ids TEXT,
              inclusion_rule TEXT,
              exclusion_rule TEXT,
              row_count INTEGER,
              candidate_count INTEGER,
              excluded_count INTEGER,
              member_key TEXT,
              member_digest TEXT,
              members_sample TEXT,
              aggregate_value TEXT,
              aggregate_unit TEXT,
              empty_reason TEXT,
              note TEXT,
              created_at TEXT
            );
            CREATE TABLE lineage_edges (
              observation_id TEXT NOT NULL,
              input_order INTEGER NOT NULL,
              input_role TEXT NOT NULL,
              operator_sign TEXT,
              coefficient REAL,
              input_source_system TEXT,
              input_filing_id TEXT,
              input_source_fact_id TEXT,
              input_observation_id TEXT,
              input_context_id TEXT,
              input_concept TEXT,
              input_period TEXT,
              input_value TEXT,
              input_unit TEXT,
              input_version_status TEXT,
              input_population_id TEXT,
              PRIMARY KEY (observation_id, input_order)
            );
            """
        )

    def query(self, sql, params=()):
        return self.con.execute(sql, params).fetchall()

    def insert(self, table: str, row: dict) -> None:
        columns = list(row)
        marks = ",".join("?" for _ in columns)
        self.con.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({marks})",
            tuple(row[column] for column in columns),
        )


def _observation(observation_id, metric_id, value, unit, *, value_text=None,
                 candidate_count=None):
    return {
        "observation_id": observation_id,
        "metric_id": metric_id,
        "entity_key": "C009999",
        "method": "derived" if metric_id == "ioc_top5_shipper_concentration" else "filed",
        "availability": "present",
        "filing_id": "20260101-9999",
        "value_text": str(value) if value_text is None else value_text,
        "value_num": value,
        "unit": unit,
        "scope": "one synthetic Form 549B filing occurrence",
        "candidate_count": candidate_count,
    }


def _contract_rows():
    # Top five: A=41 over 41 rows, B=30, C=20, D=10, E=5. F=1 is excluded.
    layout = [("SHIPPER A", 1.0)] * 41 + [
        ("SHIPPER B", 30.0),
        ("SHIPPER C", 20.0),
        ("SHIPPER D", 10.0),
        ("SHIPPER E", 5.0),
        ("SHIPPER F", 1.0),
        ("SHIPPER WITH BLANK WEIGHT", None),
    ]
    return [
        {"line": number, "_group": number, "shipper_name": name,
         "shipper_id": str(number), "transport_mdq": weight,
         "storage_quantity": None, "primary_term_expiry": None,
         "rollover_days": None, "points": []}
        for number, (name, weight) in enumerate(layout, 1)
    ]


def _fixture(weight_field="transport_mdq"):
    staging = _Staging()
    filing_id = "20260101-9999"
    contracts = _contract_rows()
    if weight_field == "transport_mdq":
        label, unit, denominator_metric = (
            "transportation MDQ (D item o)", "Dth/day", "filed_transport_mdq")
    elif weight_field == "storage_quantity":
        label, unit, denominator_metric = (
            "contracted storage quantity (D item p)", "Dth", "filed_storage_quantity")
        for row in contracts:
            row["storage_quantity"] = row["transport_mdq"]
            row["transport_mdq"] = None
    else:  # fixture author error, not a product behavior
        raise ValueError(weight_field)
    weighted = [row for row in contracts if row[weight_field] is not None]
    denominator = sum(row[weight_field] for row in weighted)
    top_names = ("SHIPPER A", "SHIPPER B", "SHIPPER C", "SHIPPER D", "SHIPPER E")
    numerator = sum(row[weight_field] for row in weighted
                    if row["shipper_name"] in top_names)
    output = _observation(
        "obs-top5", "ioc_top5_shipper_concentration",
        100.0 * numerator / denominator, "percent",
        value_text=f"{100.0 * numerator / denominator:.4f}",
        candidate_count=len({row["shipper_name"] for row in weighted}))
    denominator_obs = _observation(
        "obs-denominator", denominator_metric, denominator, unit)
    staging.insert("observations", output)
    staging.insert("observations", denominator_obs)
    for row in contracts:
        staging.insert("source_facts", {
            "source_system": ioc.SOURCE_SYSTEM,
            "filing_id": filing_id,
            "source_fact_id": f"D{row['line']:05d}",
            "typed_dims_json": json.dumps({
                "shipper_name": row["shipper_name"],
                "transport_mdq": row["transport_mdq"],
                "storage_quantity": row["storage_quantity"],
            }, sort_keys=True),
        })

    lineage = ioc._Lineage(output["entity_key"])
    lineage.edge(
        output, "denominator", "/", filing_id, denominator, unit,
        concept=denominator_metric, observation_id_=denominator_obs["observation_id"],
        version="original")
    lineage.contributions(
        output, f"denominator_rows::{label}", weighted,
        filing_id=filing_id,
        inclusion=f"D records contributing to the {label} denominator",
        exclusion=f"D records with no {label} are excluded",
        value_of=lambda row: row[weight_field], unit=unit,
        aggregate_value=denominator, candidate_count=len(contracts),
        concept=denominator_metric)

    grouped = {}
    for row in weighted:
        state = grouped.setdefault(row["shipper_name"], {"weight": 0.0, "rows": []})
        state["weight"] += row[weight_field]
        state["rows"].append(row)
    ranked = sorted(grouped.items(), key=lambda item: -item[1]["weight"])
    for name, state in ranked[:5]:
        lineage.contributions(
            output, f"top5_shipper_member::{label}::{name}",
            state["rows"], filing_id=filing_id,
            inclusion=(f"contracts in the {label} denominator whose legal "
                       f"shipper name equals {name!r}"),
            exclusion="all contracts held by other legal shipper names are excluded",
            value_of=lambda row: row[weight_field], unit=unit,
            aggregate_value=state["weight"], candidate_count=len(weighted),
            concept=name, edge_role="group_member", version="original")
    top_rows = [row for row in weighted if row["shipper_name"] in top_names]
    collective = lineage.contributions(
        output, f"top5_shipper_contracts::{label}", top_rows,
        filing_id=filing_id,
        inclusion=(f"contracts in the {label} denominator held by the five "
                   "legal shipper names with the largest aggregated weight"),
        exclusion="contracts held by all other legal shipper names are excluded",
        value_of=lambda row: row[weight_field], unit=unit,
        aggregate_value=numerator, candidate_count=len(weighted),
        concept="ioc_top5_shipper_concentration")

    for population in lineage.populations:
        staging.insert("lineage_populations", population)
    for edge in lineage.edges:
        staging.insert("lineage_edges", edge)
    staging.con.commit()
    return staging, collective["population_id"]


class IOCTopFiveLineageTests(unittest.TestCase):
    def setUp(self):
        self.staging, self.collective_id = _fixture()

    def tearDown(self):
        self.staging.con.close()

    def problems(self):
        return ioc.audit_lineage(self.staging, "C009999")

    def test_valid_large_population_passes_and_each_summary_has_a_specific_set(self):
        group_edges = self.staging.query(
            "SELECT input_population_id FROM lineage_edges "
            "WHERE observation_id='obs-top5' AND input_role='group_member' "
            "ORDER BY input_order")
        self.assertEqual(len(group_edges), 5)
        self.assertTrue(all(row["input_population_id"] for row in group_edges))
        self.assertEqual(len({row["input_population_id"] for row in group_edges}), 5)
        self.assertEqual(self.problems(), [])

    def test_storage_quantity_weight_uses_the_same_full_redraw(self):
        storage, _collective_id = _fixture("storage_quantity")
        try:
            self.assertEqual(ioc.audit_lineage(storage, "C009999"), [])
        finally:
            storage.con.close()

    def test_standard_and_three_character_point_codes_redraw_exactly(self):
        for number, code in ((5, "130"), (6, "M2")):
            observation = _observation(
                f"obs-point-{code}", "ioc_points", 1, "count", candidate_count=1)
            self.staging.insert("observations", observation)
            self.staging.insert("source_facts", {
                "source_system": ioc.SOURCE_SYSTEM,
                "filing_id": observation["filing_id"],
                "source_fact_id": f"P{number:05d}",
                "typed_dims_json": json.dumps({"point_code": code}, sort_keys=True),
            })
            lineage = ioc._Lineage(observation["entity_key"])
            lineage.contributions(
                observation, f"point_records::{code}",
                [{"line": number, "_kind": "P", "point_code": code}],
                filing_id=observation["filing_id"],
                inclusion=("P records of this filing occurrence whose item yh "
                           f"(point code) is {code} -- exact filed code"),
                exclusion="all P records with a different item yh code are excluded",
                value_of=lambda _row: 1, unit="count", aggregate_value=1,
                candidate_count=1, concept="ioc_points")
            for population in lineage.populations:
                self.staging.insert("lineage_populations", population)
            for edge in lineage.edges:
                self.staging.insert("lineage_edges", edge)
        self.staging.con.commit()
        self.staging.con.execute(
            "UPDATE lineage_populations SET inclusion_rule=? "
            "WHERE observation_id='obs-point-130'",
            ("all filing-occurrence P rows carrying exact typed code 130",))
        self.staging.con.commit()
        self.assertEqual(self.problems(), [])

        # A prefix is not the filed code. Mutating 130 to 13 must make the exact
        # population fail rather than silently satisfy a truncated rule.
        self.staging.con.execute(
            "UPDATE source_facts SET typed_dims_json=? WHERE source_fact_id='P00005'",
            (json.dumps({"point_code": "13"}, sort_keys=True),))
        problems = self.problems()
        self.assertTrue(any(
            row["problem"] == "population_cannot_be_redrawn_from_source_facts"
            and row.get("purpose") == "point_records::130" for row in problems))

    def test_blank_point_code_cannot_be_promoted_as_present(self):
        observation = _observation(
            "obs-point-blank", "ioc_points", 1, "codes", candidate_count=1)
        self.staging.insert("observations", observation)
        self.staging.insert("source_facts", {
            "source_system": ioc.SOURCE_SYSTEM,
            "filing_id": observation["filing_id"],
            "source_fact_id": "P00007",
            "typed_dims_json": json.dumps({"point_code": ""}, sort_keys=True),
        })
        lineage = ioc._Lineage(observation["entity_key"])
        lineage.contributions(
            observation, "point_records::",
            [{"line": 7, "_kind": "P", "point_code": ""}],
            filing_id=observation["filing_id"],
            inclusion=("P records of this filing occurrence whose item yh "
                       "(point code) is  -- blank filed code"),
            exclusion="all P records with a nonblank item yh code are excluded",
            value_of=lambda _row: "", unit="codes", aggregate_value=None,
            candidate_count=1, concept="ioc_points")
        for population in lineage.populations:
            self.staging.insert("lineage_populations", population)
        for edge in lineage.edges:
            self.staging.insert("lineage_edges", edge)
        self.staging.con.commit()
        codes = {row["problem"] for row in self.problems()}
        self.assertIn("blank_point_code_promoted_as_present", codes)

    def test_blank_point_code_rejection_does_not_depend_on_rule_prose(self):
        observation = _observation(
            "obs-point-blank-alternate", "ioc_points", 1, "codes", candidate_count=1)
        self.staging.insert("observations", observation)
        self.staging.insert("source_facts", {
            "source_system": ioc.SOURCE_SYSTEM,
            "filing_id": observation["filing_id"],
            "source_fact_id": "P00008",
            "typed_dims_json": json.dumps({"point_code": ""}, sort_keys=True),
        })
        lineage = ioc._Lineage(observation["entity_key"])
        lineage.contributions(
            observation, "point_records::",
            [{"line": 8, "_kind": "P", "point_code": ""}],
            filing_id=observation["filing_id"],
            inclusion="one categorical location member selected from this occurrence",
            exclusion="all other categorical location members are excluded",
            value_of=lambda _row: "", unit="codes", aggregate_value=None,
            candidate_count=1, concept="ioc_points")
        for population in lineage.populations:
            population["note"] = "legacy human explanation without a machine purpose"
            self.staging.insert("lineage_populations", population)
        for edge in lineage.edges:
            self.staging.insert("lineage_edges", edge)
        self.staging.con.commit()
        codes = {row["problem"] for row in self.problems()}
        self.assertIn("blank_point_code_promoted_as_present", codes)
        self.assertIn("ioc_population_purpose_missing", codes)

    def test_blank_point_code_cannot_masquerade_as_file_census(self):
        observation = _observation(
            "obs-point-blank-mislabeled", "ioc_points", 1, "codes", candidate_count=1)
        observation["scope"] = "location detail only | point code blank"
        self.staging.insert("observations", observation)
        self.staging.insert("source_facts", {
            "source_system": ioc.SOURCE_SYSTEM,
            "filing_id": observation["filing_id"],
            "source_fact_id": "P00009",
            "typed_dims_json": json.dumps({"point_code": ""}, sort_keys=True),
        })
        lineage = ioc._Lineage(observation["entity_key"])
        lineage.contributions(
            observation, "point_records",
            [{"line": 9, "_kind": "P", "point_code": ""}],
            filing_id=observation["filing_id"],
            inclusion="every P record in the occurrence",
            exclusion="all non-P rows are excluded",
            value_of=lambda _row: "", unit="codes", aggregate_value=None,
            candidate_count=1, concept="ioc_points")
        for population in lineage.populations:
            self.staging.insert("lineage_populations", population)
        for edge in lineage.edges:
            self.staging.insert("lineage_edges", edge)
        self.staging.con.commit()
        codes = {row["problem"] for row in self.problems()}
        self.assertIn("point_file_census_output_disagrees", codes)

    def test_zero_point_file_census_records_and_redraws_zero_candidate_count(self):
        point_metric = next(metric for metric in ioc.BY_ADAPTER[ioc.ADAPTER]
                            if metric.id == "ioc_points")
        observation = _observation(
            "obs-point-none", "ioc_points", None, None,
            value_text=None, candidate_count=0)
        observation.update({
            "availability": "source_blank",
            "value_text": None,
            "value_num": None,
            "scope": point_metric.scope,
            "missing_reason": "this snapshot carries no P (point) records",
        })
        self.staging.insert("observations", observation)
        lineage = ioc._Lineage(observation["entity_key"])
        lineage.contributions(
            observation, "point_records", [],
            filing_id=observation["filing_id"],
            inclusion="P (point) records of this filing occurrence",
            exclusion="no row was excluded; there was nothing to exclude",
            value_of=lambda _row: None, unit=None, aggregate_value=None,
            candidate_count=0,
            empty_reason=("the filing was retrieved and parsed; it carries no "
                          "P record at all"),
            concept="ioc_points")
        for population in lineage.populations:
            self.staging.insert("lineage_populations", population)
        for edge in lineage.edges:
            self.staging.insert("lineage_edges", edge)
        self.staging.con.commit()
        self.assertEqual(self.problems(), [])

        for invalid in (None, 1):
            self.staging.con.execute(
                "UPDATE observations SET candidate_count=? "
                "WHERE observation_id='obs-point-none'", (invalid,))
            problems = self.problems()
            defect = next(row for row in problems
                          if row.get("observation_id") == "obs-point-none")
            self.assertEqual(defect["problem"], "point_file_census_output_disagrees")
            self.assertIn("candidate_count", defect["detail"])
            self.staging.con.execute(
                "UPDATE observations SET candidate_count=0 "
                "WHERE observation_id='obs-point-none'")

    def test_foreign_adapter_population_is_outside_ioc_validator(self):
        foreign = (
            ("i311_firm_share", "FERC Form 549D", "d549_rows", "R0001"),
            ("lng_inspection", "eLibrary", "filings", "20260101-8888"),
            # Prefix similarity is intentionally irrelevant. This metric is
            # not declared for the IOC adapter and must remain outside it.
            ("ioc_foreign_probe", "eLibrary", "filings", "20260101-9998"),
        )
        for number, (metric, source, table, member) in enumerate(foreign, 1):
            observation = _observation(
                f"obs-foreign-adapter-{number}", metric, 50, "percent")
            self.staging.insert("observations", observation)
            self.staging.insert("lineage_populations", {
                "population_id": f"pop-foreign-adapter-{number}",
                "observation_id": observation["observation_id"],
                "source_system": source,
                "source_table": table,
                "filing_ids": json.dumps([observation["filing_id"]]),
                "inclusion_rule": "foreign rows selected under their adapter contract",
                "exclusion_rule": "all other foreign-adapter rows are excluded",
                "row_count": 1,
                "candidate_count": 1,
                "excluded_count": 0,
                "member_key": "source_fact_id",
                "member_digest": ioc.member_digest([member]),
                "members_sample": json.dumps([member]),
                "aggregate_value": "50",
                "aggregate_unit": "percent",
                "empty_reason": None,
                "note": "foreign-adapter negative control",
                "created_at": "2026-09-10T00:00:00+00:00",
            })
        self.staging.con.commit()
        # Exercise the no-entity production path which exposed the v7 defect.
        self.assertEqual(ioc.audit_lineage(self.staging), [])
        import validate
        metric_ids = sorted(metric.id for metric in ioc.BY_ADAPTER[ioc.ADAPTER])
        marks = ",".join("?" for _ in metric_ids)
        expected = self.staging.query(
            f"SELECT COUNT(*) n FROM observations WHERE metric_id IN ({marks}) "
            "AND availability='present'", tuple(metric_ids))[0]["n"]
        detail = validate.t_ioc_persisted_lineage(
            type("ValidationContext", (), {"staging": self.staging})())
        self.assertTrue(detail.startswith(
            f"{expected:,} present IOC observations pass full persisted redraw"))
        self.assertNotIn("ioc_foreign_probe", metric_ids)

        # Ignoring foreign populations cannot mean ignoring IOC populations.
        self.staging.con.execute(
            "DELETE FROM lineage_edges WHERE rowid IN ("
            "SELECT rowid FROM lineage_edges WHERE observation_id='obs-top5' "
            "AND input_role='contributing_row' LIMIT 1)")
        codes = {row["problem"] for row in ioc.audit_lineage(self.staging)}
        self.assertIn("contributing_edges_do_not_match_population", codes)

    def test_snapshot_builder_emits_shipper_specific_populations(self):
        metric = next(metric for metric in ioc.BY_ADAPTER["ioc"]
                      if metric.id == "ioc_top5_shipper_concentration")
        filing = {
            "filing_id": "20260101-9999", "filed_date": "2026-01-01",
            "reporting_year": 2026, "reporting_period": "Q1",
            "version_status": "original", "_document_id": "synthetic-ioc",
            "_parsed": {
                "header": {
                    "snapshot_date": "2026-01-01", "report_date": "2026-01-01",
                    "original_revised": "O", "uom_transport": "Dth",
                    "uom_storage": "", "uom_transport_state": "recognised",
                    "uom_storage_state": "blank", "uom_transport_code": "T",
                    "uom_storage_code": "",
                },
                "contracts": _contract_rows(),
                "provenance": {
                    "bom": "", "decode_fallback": "",
                    "preamble_rows_skipped": 0, "unclassified_note": "",
                    "header_repair": {"applied": False},
                },
            },
        }
        lineage = ioc._Lineage("C009999")
        observations = ioc._snapshot_observations(
            None, {"entity_key": "C009999"}, filing, {metric.id: metric}, lineage)
        output = next(row for row in observations if row["metric_id"] == metric.id)
        groups = [edge for edge in lineage.edges
                  if edge["observation_id"] == output["observation_id"]
                  and edge["input_role"] == "group_member"]
        populations = {row["population_id"]: row for row in lineage.populations}
        self.assertAlmostEqual(output["value_num"], 100.0 * 106.0 / 107.0)
        self.assertEqual(len(groups), 5)
        self.assertEqual(len({edge["input_population_id"] for edge in groups}), 5)
        self.assertTrue(all(edge["input_population_id"] in populations for edge in groups))
        self.assertEqual(sorted(populations[edge["input_population_id"]]["row_count"]
                                for edge in groups), [1, 1, 1, 1, 41])
        collective = next(edge for edge in lineage.edges
                          if edge["observation_id"] == output["observation_id"]
                          and edge["input_role"] == "population"
                          and edge["input_concept"] == metric.id)
        self.assertEqual(populations[collective["input_population_id"]]["aggregate_value"],
                         "106")

    def test_snapshot_builder_retains_blank_point_rows_without_promoting_a_code(self):
        metric = next(metric for metric in ioc.BY_ADAPTER["ioc"]
                      if metric.id == "ioc_points")

        def point(code, line):
            return {
                "line": line, "_kind": "P", "_group": 1,
                "point_code": code, "point_name": "synthetic point",
                "qualifier": "", "point_id": "", "zone": "",
                "transport_qty_text": "0", "storage_qty_text": "0",
                "transport_qty": 0.0, "storage_qty": 0.0, "footnote_ids": "",
            }

        def filing(contracts):
            return {
                "filing_id": "20260101-9999", "filed_date": "2026-01-01",
                "reporting_year": 2026, "reporting_period": "Q1",
                "version_status": "original", "_document_id": "synthetic-ioc",
                "_parsed": {
                    "header": {
                        "snapshot_date": "2026-01-01", "report_date": "2026-01-01",
                        "original_revised": "O", "uom_transport": "Dth",
                        "uom_storage": "", "uom_transport_state": "recognised",
                        "uom_storage_state": "blank", "uom_transport_code": "T",
                        "uom_storage_code": "",
                    },
                    "contracts": contracts,
                    "provenance": {
                        "bom": "", "decode_fallback": "",
                        "preamble_rows_skipped": 0, "unclassified_note": "",
                        "header_repair": {"applied": False},
                    },
                },
            }

        mixed = _contract_rows()
        mixed[0]["points"] = [point("", 100), point("M2", 101)]
        mixed_lineage = ioc._Lineage("C009999")
        mixed_rows = ioc._snapshot_observations(
            None, {"entity_key": "C009999"}, filing(mixed),
            {metric.id: metric}, mixed_lineage)
        self.assertEqual(len(mixed_rows), 2)  # file census plus M2, never blank
        self.assertEqual({row["availability"] for row in mixed_rows}, {"present"})
        self.assertTrue(all((row["value_text"] or "").strip() for row in mixed_rows))
        self.assertFalse(any("point code  (" in row["scope"] for row in mixed_rows))
        head = next(row for row in mixed_rows if row["scope"] == metric.scope)
        self.assertIn("1 P record(s) have blank item yh", head["qa_flags"])
        self.assertEqual(sum(pop["row_count"] for pop in mixed_lineage.populations
                             if pop["observation_id"] == head["observation_id"]), 2)

        blank_only = _contract_rows()
        blank_only[0]["points"] = [point("", 102)]
        blank_lineage = ioc._Lineage("C009999")
        blank_rows = ioc._snapshot_observations(
            None, {"entity_key": "C009999"}, filing(blank_only),
            {metric.id: metric}, blank_lineage)
        self.assertEqual(len(blank_rows), 1)
        self.assertEqual(blank_rows[0]["availability"], "source_blank")
        self.assertIsNone(blank_rows[0]["value_text"])
        self.assertIn("item yh (point code) is blank throughout",
                      blank_rows[0]["missing_reason"])
        self.assertEqual(blank_lineage.populations[0]["row_count"], 1)

        no_points = _contract_rows()
        no_point_lineage = ioc._Lineage("C009999")
        no_point_rows = ioc._snapshot_observations(
            None, {"entity_key": "C009999"}, filing(no_points),
            {metric.id: metric}, no_point_lineage)
        self.assertEqual(len(no_point_rows), 1)
        self.assertEqual(no_point_rows[0]["availability"], "source_blank")
        self.assertEqual(no_point_rows[0]["candidate_count"], 0)
        self.assertIsNone(no_point_rows[0]["value_text"])
        self.assertIn("no P (point) records", no_point_rows[0]["missing_reason"])
        self.assertEqual(no_point_lineage.populations[0]["row_count"], 0)
        self.assertEqual(no_point_lineage.populations[0]["candidate_count"], 0)

    def test_targetless_group_summary_is_rejected(self):
        self.staging.con.execute(
            "UPDATE lineage_edges SET input_population_id=NULL "
            "WHERE observation_id='obs-top5' AND input_role='group_member'")
        codes = {problem["problem"] for problem in self.problems()}
        self.assertIn("top5_group_population_missing", codes)

    def test_shared_collective_population_is_not_accepted_as_shipper_specific(self):
        self.staging.con.execute(
            "UPDATE lineage_edges SET input_population_id=? "
            "WHERE observation_id='obs-top5' AND input_role='group_member'",
            (self.collective_id,))
        codes = {problem["problem"] for problem in self.problems()}
        self.assertIn("top5_group_summary_disagrees", codes)

    def test_missing_collective_population_is_rejected(self):
        self.staging.con.execute(
            "DELETE FROM lineage_populations WHERE population_id=?",
            (self.collective_id,))
        codes = {problem["problem"] for problem in self.problems()}
        self.assertIn("top5_collective_population_missing", codes)

    def test_mutated_summary_weight_is_rejected(self):
        self.staging.con.execute(
            "UPDATE lineage_edges SET input_value='999' "
            "WHERE observation_id='obs-top5' AND input_role='group_member' "
            "AND input_concept='SHIPPER B'")
        codes = {problem["problem"] for problem in self.problems()}
        self.assertIn("top5_group_summary_disagrees", codes)

    def test_non_sampled_member_mutation_in_a_large_population_is_rejected(self):
        population = self.staging.query(
            "SELECT members_sample FROM lineage_populations WHERE population_id=?",
            (self.collective_id,))[0]
        self.assertNotIn("20260101-9999:D00041", json.loads(population["members_sample"]))
        self.staging.con.execute(
            "UPDATE source_facts SET typed_dims_json=? "
            "WHERE filing_id='20260101-9999' AND source_fact_id='D00041'",
            (json.dumps({"shipper_name": "SHIPPER F", "transport_mdq": 1.0,
                         "storage_quantity": None}, sort_keys=True),))
        problems = self.problems()
        codes = {problem["problem"] for problem in problems}
        self.assertIn("top5_collective_population_disagrees", codes)
        collective = next(problem for problem in problems
                          if problem["problem"] == "top5_collective_population_disagrees")
        self.assertIn("member_digest", collective["detail"])

    def test_denominator_mutation_is_rejected(self):
        self.staging.con.execute(
            "UPDATE observations SET value_num=value_num+1 "
            "WHERE observation_id='obs-denominator'")
        codes = {problem["problem"] for problem in self.problems()}
        self.assertIn("top5_denominator_disagrees", codes)

    def test_published_text_and_candidate_count_are_redrawn(self):
        self.staging.con.execute(
            "UPDATE observations SET value_text='99.9999', candidate_count=99 "
            "WHERE observation_id='obs-top5'")
        problems = self.problems()
        output = next(problem for problem in problems
                      if problem["problem"] == "top5_output_disagrees")
        self.assertIn("value_text", output["detail"])
        self.assertIn("candidate_count", output["detail"])


if __name__ == "__main__":
    unittest.main()
