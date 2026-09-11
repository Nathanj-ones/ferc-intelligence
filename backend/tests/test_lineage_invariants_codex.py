"""Regression tests for the generic persisted-lineage invariant.

Every rejecting mutation is paired with an unchanged positive fixture.  The
fixtures use real production table/column names but stay wholly in memory; no
project database, cache, or configuration is read.
"""

from __future__ import annotations

import json
import sqlite3
import unittest

from ferclib.lineage import LineageInvariantError, validate_persisted_lineage


def _connection(row_factory=True):
    con = sqlite3.connect(":memory:")
    if row_factory:
        con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE filings (
          source_system TEXT NOT NULL,
          filing_id TEXT NOT NULL,
          entity_key TEXT NOT NULL,
          version_status TEXT,
          is_canonical INTEGER,
          reporting_year INTEGER,
          reporting_period TEXT,
          period_start TEXT,
          period_end TEXT,
          PRIMARY KEY (source_system, filing_id)
        );
        CREATE TABLE source_facts (
          source_system TEXT NOT NULL,
          filing_id TEXT NOT NULL,
          source_fact_id TEXT NOT NULL,
          context_id TEXT,
          concept_qname TEXT,
          concept_local TEXT,
          unit_text TEXT,
          value_as_filed TEXT,
          period_class TEXT,
          instant TEXT,
          period_start TEXT,
          period_end TEXT,
          PRIMARY KEY (source_system, filing_id, source_fact_id)
        );
        CREATE TABLE observations (
          observation_id TEXT PRIMARY KEY,
          entity_key TEXT NOT NULL,
          metric_id TEXT NOT NULL,
          method TEXT NOT NULL,
          availability TEXT NOT NULL,
          scope TEXT NOT NULL,
          scope_rule TEXT,
          source_system TEXT,
          filing_id TEXT,
          source_fact_id TEXT,
          source_context_id TEXT,
          version_status TEXT NOT NULL
        );
        CREATE TABLE lineage_populations (
          population_id TEXT PRIMARY KEY,
          observation_id TEXT NOT NULL,
          source_system TEXT NOT NULL,
          source_table TEXT NOT NULL,
          filing_ids TEXT NOT NULL,
          inclusion_rule TEXT NOT NULL,
          exclusion_rule TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          candidate_count INTEGER NOT NULL,
          excluded_count INTEGER NOT NULL,
          member_key TEXT NOT NULL,
          member_digest TEXT NOT NULL,
          aggregate_value TEXT,
          aggregate_unit TEXT,
          empty_reason TEXT
        );
        CREATE TABLE lineage_edges (
          observation_id TEXT NOT NULL,
          input_order INTEGER NOT NULL,
          input_role TEXT NOT NULL,
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
    return con


def _insert(con, table, **row):
    columns = list(row)
    con.execute(
        "INSERT INTO {} ({}) VALUES ({})".format(
            table, ",".join(columns), ",".join("?" for _ in columns)
        ),
        tuple(row[column] for column in columns),
    )


def _filing(con, filing_id, entity="C1", version="original"):
    _insert(
        con,
        "filings",
        source_system="eCollection_XBRL",
        filing_id=filing_id,
        entity_key=entity,
        version_status=version,
        is_canonical=1,
        reporting_year=2025,
        reporting_period="annual",
        period_start="2025-01-01",
        period_end="2025-12-31",
    )


def _fact(con, filing_id, fact_id, value, start, end):
    _insert(
        con,
        "source_facts",
        source_system="eCollection_XBRL",
        filing_id=filing_id,
        source_fact_id=fact_id,
        context_id="ctx-" + fact_id,
        concept_qname="ferc:" + fact_id,
        concept_local=fact_id,
        unit_text="USD",
        value_as_filed=str(value),
        period_class="quarter",
        instant=None,
        period_start=start,
        period_end=end,
    )


def _observation(
    con,
    observation_id,
    metric,
    *,
    method,
    scope="FERC filing entity C1; whole-entity gas operations",
    scope_rule="",
    filing_id=None,
    fact_id=None,
    entity="C1",
    availability="present",
    version="original",
):
    _insert(
        con,
        "observations",
        observation_id=observation_id,
        entity_key=entity,
        metric_id=metric,
        method=method,
        availability=availability,
        scope=scope,
        scope_rule=scope_rule,
        source_system="eCollection_XBRL" if filing_id else None,
        filing_id=filing_id,
        source_fact_id=fact_id,
        source_context_id=("ctx-" + fact_id) if fact_id else None,
        version_status=version,
    )


def _edge(
    con,
    output,
    order,
    role,
    *,
    input_observation=None,
    source_system=None,
    filing_id=None,
    fact_id=None,
    population_id=None,
    context_id=None,
    version_status=None,
    period=None,
):
    _insert(
        con,
        "lineage_edges",
        observation_id=output,
        input_order=order,
        input_role=role,
        input_source_system=source_system,
        input_filing_id=filing_id,
        input_source_fact_id=fact_id,
        input_observation_id=input_observation,
        input_context_id=context_id,
        input_concept=None,
        input_period=period,
        input_value=None,
        input_unit=None,
        input_version_status=version_status,
        input_population_id=population_id,
    )


def _gas_chain(row_factory=True):
    """Two different quarters -> intermediate -> margin, all one actual scope."""

    con = _connection(row_factory=row_factory)
    facts = (
        ("f-q1", "revenue-q1", 100, "2025-01-01", "2025-03-31"),
        ("f-q2", "revenue-q2", 120, "2025-04-01", "2025-06-30"),
        ("f-inc", "income-h1", 55, "2025-01-01", "2025-06-30"),
    )
    for filing_id, fact_id, value, start, end in facts:
        _filing(con, filing_id)
        _fact(con, filing_id, fact_id, value, start, end)
        _observation(
            con,
            "obs-" + fact_id,
            fact_id,
            method="filed",
            filing_id=filing_id,
            fact_id=fact_id,
        )

    _observation(
        con,
        "obs-revenue-h1",
        "gas_operating_revenues",
        method="derived",
        scope_rule="all inputs must have identical scope",
        version="current",
    )
    _edge(
        con,
        "obs-revenue-h1",
        0,
        "addend",
        input_observation="obs-revenue-q1",
        period="2025-01-01..2025-03-31",
    )
    _edge(
        con,
        "obs-revenue-h1",
        1,
        "addend",
        input_observation="obs-revenue-q2",
        period="2025-04-01..2025-06-30",
    )
    _observation(
        con,
        "obs-margin",
        "operating_margin_pct",
        method="derived",
        scope_rule="identical to both inputs; never mixes scopes",
        version="current",
    )
    # These outer edges intentionally carry no singular source occurrence/fact:
    # revenue-h1 is supported by two distinct filings.
    _edge(
        con,
        "obs-margin",
        0,
        "denominator",
        input_observation="obs-revenue-h1",
    )
    _edge(
        con,
        "obs-margin",
        1,
        "numerator",
        input_observation="obs-income-h1",
    )
    return con


def _population_fixture():
    con = _connection()
    _filing(con, "f-pop")
    _observation(
        con,
        "obs-pop",
        "ioc_top5_shipper_concentration",
        method="derived",
        version="current",
    )
    _insert(
        con,
        "lineage_populations",
        population_id="pop-top5",
        observation_id="obs-pop",
        source_system="eCollection_XBRL",
        source_table="source_facts",
        filing_ids=json.dumps(["f-pop"]),
        inclusion_rule="all valid contract rows for the five named shippers",
        exclusion_rule="blank or nonnumeric contract weights",
        row_count=45,
        candidate_count=47,
        excluded_count=2,
        member_key="source_fact_id",
        member_digest="0" * 64,
        aggregate_value="106",
        aggregate_unit="Dth/day",
        empty_reason=None,
    )
    _edge(
        con,
        "obs-pop",
        0,
        "population",
        source_system="eCollection_XBRL",
        filing_id="f-pop",
        population_id="pop-top5",
    )
    return con


def _codes(report):
    return {problem.code for problem in report.problems}


class PersistedLineageInvariantTests(unittest.TestCase):
    def assertValid(self, con):
        self.addCleanup(con.close)
        report = validate_persisted_lineage(con)
        self.assertTrue(report.ok, report.as_dict())
        return report

    def assertMutationRejected(self, con, code):
        self.addCleanup(con.close)
        report = validate_persisted_lineage(con)
        self.assertFalse(report.ok)
        self.assertIn(code, _codes(report), report.as_dict())
        return report

    def test_recursive_gas_chain_accepts_blank_outer_direct_ids_and_mixed_dates(self):
        report = self.assertValid(_gas_chain())
        self.assertEqual(report.edge_count, 4)
        self.assertEqual(report.present_derived_count, 2)
        self.assertEqual(report.grounded_present_derived_count, 2)
        self.assertEqual(report.observation_references, 4)
        self.assertEqual(report.exact_fact_references, 0)

    def test_plain_sqlite_connection_without_row_factory_is_supported(self):
        self.assertValid(_gas_chain(row_factory=False))

    def test_occurrence_only_edge_is_rejected_but_unchanged_chain_passes(self):
        self.assertValid(_gas_chain())
        con = _gas_chain()
        _edge(
            con,
            "obs-margin",
            2,
            "group_member",
            source_system="eCollection_XBRL",
            filing_id="f-q1",
        )
        report = self.assertMutationRejected(con, "edge_no_referent")
        self.assertIn("derived_path_unterminated", _codes(report))

    def test_dangling_observation_is_rejected_but_unchanged_chain_passes(self):
        self.assertValid(_gas_chain())
        con = _gas_chain()
        con.execute(
            "UPDATE lineage_edges SET input_observation_id='obs-absent' "
            "WHERE observation_id='obs-margin' AND input_order=1"
        )
        self.assertMutationRejected(con, "input_observation_missing")

    def test_deleted_terminal_fact_is_rejected_but_unchanged_chain_passes(self):
        self.assertValid(_gas_chain())
        con = _gas_chain()
        con.execute(
            "DELETE FROM source_facts WHERE source_system='eCollection_XBRL' "
            "AND filing_id='f-q2' AND source_fact_id='revenue-q2'"
        )
        report = self.assertMutationRejected(
            con, "input_observation_source_fact_missing"
        )
        self.assertIn("derived_path_unterminated", _codes(report))

    def test_observation_cycle_is_rejected_but_unchanged_chain_passes(self):
        self.assertValid(_gas_chain())
        con = _gas_chain()
        _edge(
            con,
            "obs-revenue-q1",
            0,
            "basis",
            input_observation="obs-margin",
        )
        report = self.assertMutationRejected(con, "observation_cycle")
        self.assertIn("derived_path_unterminated", _codes(report))

    def test_redundant_occurrence_must_agree_with_linked_observation(self):
        con = _gas_chain()
        con.execute(
            "UPDATE lineage_edges SET input_source_system='eCollection_XBRL', "
            "input_filing_id='f-q1', input_source_fact_id='revenue-q1', "
            "input_context_id='ctx-revenue-q1', input_version_status='original' "
            "WHERE observation_id='obs-revenue-h1' AND input_order=0"
        )
        self.assertValid(con)

        con.execute(
            "UPDATE lineage_edges SET input_filing_id='f-q2', "
            "input_source_fact_id='revenue-q2' "
            "WHERE observation_id='obs-revenue-h1' AND input_order=0"
        )
        self.assertMutationRejected(con, "redundant_occurrence_mismatch")

    def test_stale_linked_version_is_rejected_but_current_version_passes(self):
        con = _gas_chain()
        con.execute(
            "UPDATE lineage_edges SET input_version_status='original' "
            "WHERE observation_id='obs-revenue-h1' AND input_order=0"
        )
        self.assertValid(con)
        con.execute(
            "UPDATE lineage_edges SET input_version_status='superseded' "
            "WHERE observation_id='obs-revenue-h1' AND input_order=0"
        )
        self.assertMutationRejected(con, "input_version_status_mismatch")

    def test_cross_entity_input_is_rejected_but_same_entity_passes(self):
        self.assertValid(_gas_chain())
        con = _gas_chain()
        con.execute(
            "UPDATE observations SET entity_key='C2' "
            "WHERE observation_id='obs-revenue-q2'"
        )
        self.assertMutationRejected(con, "input_observation_entity_mismatch")

    def test_explicit_identical_scope_rule_rejects_input_scope_mix(self):
        self.assertValid(_gas_chain())
        con = _gas_chain()
        con.execute(
            "UPDATE observations SET scope='FERC filing entity C1; transmission only' "
            "WHERE observation_id='obs-revenue-q2'"
        )
        self.assertMutationRejected(con, "input_scope_mismatch")

    def test_explicit_identical_scope_rule_rejects_false_output_scope(self):
        self.assertValid(_gas_chain())
        con = _gas_chain()
        con.execute(
            "UPDATE observations SET scope='registry instruction copied as scope' "
            "WHERE observation_id='obs-margin'"
        )
        self.assertMutationRejected(con, "output_scope_mismatch")

    def test_present_output_cannot_claim_unresolved_scope(self):
        self.assertValid(_gas_chain())
        con = _gas_chain()
        con.execute(
            "UPDATE observations SET scope='scope unresolved because inputs differ', "
            "scope_rule='' WHERE observation_id='obs-margin'"
        )
        self.assertMutationRejected(con, "present_output_scope_unresolved")

    def test_valid_source_population_is_a_terminal(self):
        report = self.assertValid(_population_fixture())
        self.assertEqual(report.population_references, 1)
        self.assertEqual(report.grounded_present_derived_count, 1)

    def test_documented_zero_population_is_valid_but_unsupported_empty_is_not(self):
        con = _population_fixture()
        con.execute(
            "UPDATE lineage_populations SET filing_ids='[]', row_count=0, "
            "candidate_count=0, excluded_count=0, "
            "empty_reason='official query returned no qualifying rows' "
            "WHERE population_id='pop-top5'"
        )
        con.execute(
            "UPDATE lineage_edges SET input_filing_id=NULL "
            "WHERE observation_id='obs-pop'"
        )
        self.assertValid(con)
        con.execute(
            "UPDATE lineage_populations SET empty_reason=NULL "
            "WHERE population_id='pop-top5'"
        )
        self.assertMutationRejected(con, "population_empty_unsupported")

    def test_population_owner_mutation_is_rejected_but_original_passes(self):
        self.assertValid(_population_fixture())
        con = _population_fixture()
        _observation(
            con,
            "obs-unrelated",
            "unrelated",
            method="filed",
            filing_id="f-pop",
            fact_id=None,
        )
        con.execute(
            "UPDATE lineage_populations SET observation_id='obs-unrelated' "
            "WHERE population_id='pop-top5'"
        )
        self.assertMutationRejected(con, "input_population_owner_mismatch")

    def test_population_occurrence_mutation_is_rejected_but_original_passes(self):
        self.assertValid(_population_fixture())
        con = _population_fixture()
        con.execute(
            "UPDATE lineage_populations SET filing_ids='[\"f-absent\"]' "
            "WHERE population_id='pop-top5'"
        )
        report = self.assertMutationRejected(con, "population_filing_missing")
        self.assertIn("input_population_filing_mismatch", _codes(report))

    def test_population_definition_and_counts_are_evidence_not_placeholders(self):
        self.assertValid(_population_fixture())
        con = _population_fixture()
        con.execute(
            "UPDATE lineage_populations SET inclusion_rule='', row_count=44 "
            "WHERE population_id='pop-top5'"
        )
        report = self.assertMutationRejected(con, "population_definition_incomplete")
        self.assertIn("population_counts_invalid", _codes(report))

    def test_missing_direct_fact_is_rejected_but_real_fact_passes(self):
        con = _connection()
        _filing(con, "f-one")
        _fact(con, "f-one", "fact-one", 10, "2025-01-01", "2025-12-31")
        _observation(con, "obs-direct", "derived-total", method="derived")
        _edge(
            con,
            "obs-direct",
            0,
            "basis",
            source_system="eCollection_XBRL",
            filing_id="f-one",
            fact_id="fact-one",
        )
        self.assertValid(con)
        con.execute(
            "UPDATE lineage_edges SET input_source_fact_id='fact-absent' "
            "WHERE observation_id='obs-direct'"
        )
        self.assertMutationRejected(con, "source_fact_missing")

    def test_present_derived_without_edges_is_rejected_and_exception_is_structured(self):
        con = _connection()
        _observation(con, "obs-empty", "derived-total", method="derived")
        report = self.assertMutationRejected(con, "derived_without_edges")
        with self.assertRaises(LineageInvariantError) as caught:
            report.require_valid()
        self.assertIs(caught.exception.report, report)
        self.assertEqual(report.problem_counts["derived_path_unterminated"], 1)

    def test_present_output_cannot_depend_on_unavailable_input(self):
        self.assertValid(_gas_chain())
        con = _gas_chain()
        con.execute(
            "UPDATE observations SET availability='source_missing' "
            "WHERE observation_id='obs-income-h1'"
        )
        self.assertMutationRejected(con, "input_observation_not_present")


if __name__ == "__main__":
    unittest.main()
