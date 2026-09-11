"""Read-only invariants for persisted observation lineage.

This module deliberately validates the *persisted graph*, not an adapter's
in-memory intentions.  A lineage edge is useful only when it names an exact
source fact, another observation, or a reproducible source-row population.
An occurrence identifier by itself is metadata and cannot make a descriptive
row into evidence.

Multi-stage derivations are first-class.  In particular, an outer gas-margin
edge may point only to an intermediate observation: blank redundant source
fact fields are valid when that intermediate recursively terminates in exact
facts.  Conversely, a syntactically present edge does not pass if its branch
is cyclic, dangling, cross-entity, scope-incompatible, or never reaches source
evidence.

The public entry point is :func:`validate_persisted_lineage`.  It accepts either
a ``ferclib.staging.Staging`` instance (anything with ``query(sql, params)``)
or a ``sqlite3.Connection`` and never writes to it.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


_REQUIRED_TABLES = (
    "filings",
    "source_facts",
    "observations",
    "lineage_edges",
    "lineage_populations",
)


@dataclasses.dataclass(frozen=True)
class LineageProblem:
    """One reproducible persisted-lineage defect."""

    code: str
    message: str
    observation_id: str = ""
    input_order: Optional[int] = None
    related_id: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class LineageReport:
    """Structured result returned by :func:`validate_persisted_lineage`."""

    edge_count: int = 0
    present_derived_count: int = 0
    grounded_present_derived_count: int = 0
    exact_fact_references: int = 0
    observation_references: int = 0
    population_references: int = 0
    problems: List[LineageProblem] = dataclasses.field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def problem_counts(self) -> Dict[str, int]:
        counts = collections.Counter(problem.code for problem in self.problems)
        return dict(sorted(counts.items()))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "edge_count": self.edge_count,
            "present_derived_count": self.present_derived_count,
            "grounded_present_derived_count": self.grounded_present_derived_count,
            "exact_fact_references": self.exact_fact_references,
            "observation_references": self.observation_references,
            "population_references": self.population_references,
            "problem_counts": self.problem_counts,
            "problems": [problem.as_dict() for problem in self.problems],
        }

    def require_valid(self) -> "LineageReport":
        """Return this report or raise a compact error suitable for a CLI."""

        if not self.ok:
            raise LineageInvariantError(self)
        return self


class LineageInvariantError(AssertionError):
    """Raised by :meth:`LineageReport.require_valid` for an invalid graph."""

    def __init__(self, report: LineageReport):
        self.report = report
        examples = "; ".join(
            "{}: {}".format(problem.code, problem.message)
            for problem in report.problems[:5]
        )
        extra = len(report.problems) - min(5, len(report.problems))
        if extra:
            examples += "; ... and {} more".format(extra)
        super().__init__(
            "persisted lineage failed with {} problem(s): {}".format(
                len(report.problems), examples
            )
        )


def _query(store: Any, sql: str, params: Sequence[Any] = ()) -> List[Mapping[str, Any]]:
    if hasattr(store, "query"):
        rows = store.query(sql, params)
        column_names: Optional[List[str]] = None
    elif isinstance(store, sqlite3.Connection) or hasattr(store, "execute"):
        cursor = store.execute(sql, params)
        rows = cursor.fetchall()
        column_names = [str(item[0]) for item in (cursor.description or ())]
    else:
        raise TypeError("store must provide query(sql, params) or execute(sql, params)")

    answer: List[Mapping[str, Any]] = []
    for row in rows:
        if isinstance(row, sqlite3.Row):
            answer.append(dict(row))
        elif isinstance(row, Mapping):
            answer.append(dict(row))
        elif column_names and len(column_names) == len(row):
            answer.append(dict(zip(column_names, row)))
        else:
            # PRAGMA rows can be tuples when the caller did not install a row
            # factory.  Normal data queries below always provide explicit names,
            # so tuple rows are not silently guessed.
            raise TypeError("lineage validation requires named SQLite rows")
    return answer


def _table_names(store: Any) -> Set[str]:
    return {
        str(row["name"])
        for row in _query(
            store,
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        )
    }


def _columns(store: Any, table: str) -> Set[str]:
    # table is chosen only from the fixed constants in this module.
    return {str(row["name"]) for row in _query(store, "PRAGMA table_info(" + table + ")")}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _is_present(row: Mapping[str, Any]) -> bool:
    return _text(row.get("availability")).lower() == "present"


def _is_derived(row: Mapping[str, Any]) -> bool:
    return _text(row.get("method")).lower() == "derived"


def _scope_requires_matching_inputs(rule: str) -> bool:
    normalized = " ".join(_text(rule).lower().split())
    patterns = (
        r"identical\s+(?:regulatory\s+)?scope",
        r"same\s+(?:exact\s+)?(?:regulatory\s+)?scope",
        r"matching\s+(?:system\s+)?scope",
        r"scope\s+(?:must\s+)?match",
        r"identical\s+to\s+(?:both|all)\s+inputs",
        r"both\s+inputs[^.;]*\bscope\b",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def _scope_requires_output_identity(rule: str) -> bool:
    normalized = " ".join(_text(rule).lower().split())
    return bool(
        re.search(r"identical\s+to\s+(?:both|all)\s+inputs", normalized)
        or re.search(r"output[^.;]*identical[^.;]*scope", normalized)
    )


def _canonical_cycle(nodes: Iterable[str]) -> Tuple[str, ...]:
    values = list(nodes)
    if not values:
        return ()
    rotations = [tuple(values[index:] + values[:index]) for index in range(len(values))]
    return min(rotations)


def _graph_order_and_cycles(
    nodes: Iterable[str], adjacency: Mapping[str, Sequence[str]]
) -> Tuple[List[str], List[Tuple[str, ...]], Set[str]]:
    """Return child-first order and cycles without Python recursion."""

    colour: Dict[str, int] = {}
    path: List[str] = []
    path_position: Dict[str, int] = {}
    finished: List[str] = []
    cycles: List[Tuple[str, ...]] = []
    seen_cycles: Set[Tuple[str, ...]] = set()
    cyclic_nodes: Set[str] = set()

    for start in nodes:
        if colour.get(start, 0):
            continue
        colour[start] = 1
        path_position[start] = len(path)
        path.append(start)
        stack: List[Tuple[str, int]] = [(start, 0)]
        while stack:
            node, offset = stack[-1]
            children = adjacency.get(node, ())
            if offset < len(children):
                child = children[offset]
                stack[-1] = (node, offset + 1)
                state = colour.get(child, 0)
                if state == 0:
                    colour[child] = 1
                    path_position[child] = len(path)
                    path.append(child)
                    stack.append((child, 0))
                elif state == 1:
                    first = path_position[child]
                    cycle = _canonical_cycle(path[first:])
                    if cycle and cycle not in seen_cycles:
                        seen_cycles.add(cycle)
                        cycles.append(cycle)
                        cyclic_nodes.update(cycle)
            else:
                stack.pop()
                colour[node] = 2
                finished.append(node)
                path_position.pop(node, None)
                if path and path[-1] == node:
                    path.pop()

    return finished, cycles, cyclic_nodes


def validate_persisted_lineage(store: Any) -> LineageReport:
    """Validate the complete persisted lineage graph without changing it.

    The result is deliberately data rather than printed text, so the integrated
    validator, release builder, and regression suite can all apply the same
    acceptance decision and preserve exact affected identifiers.
    """

    report = LineageReport()
    seen_problem_keys: Set[Tuple[Any, ...]] = set()

    def problem(
        code: str,
        message: str,
        observation_id: str = "",
        input_order: Optional[int] = None,
        related_id: str = "",
    ) -> None:
        key = (code, observation_id, input_order, related_id, message)
        if key not in seen_problem_keys:
            seen_problem_keys.add(key)
            report.problems.append(
                LineageProblem(code, message, observation_id, input_order, related_id)
            )

    tables = _table_names(store)
    missing_tables = [table for table in _REQUIRED_TABLES if table not in tables]
    for table in missing_tables:
        problem("schema_missing_table", "required table {!r} is absent".format(table))
    if missing_tables:
        return report

    observation_columns = _columns(store, "observations")
    required_observation_columns = {
        "observation_id",
        "entity_key",
        "metric_id",
        "method",
        "availability",
        "scope",
        "source_system",
        "filing_id",
        "source_fact_id",
        "version_status",
    }
    missing_columns = sorted(required_observation_columns - observation_columns)
    for column in missing_columns:
        problem(
            "schema_missing_column",
            "observations.{!s} is required for lineage validation".format(column),
        )
    if missing_columns:
        return report

    optional_observation_columns = {
        "scope_rule": "''",
        "source_context_id": "''",
    }
    observation_select = sorted(required_observation_columns)
    for column, fallback in optional_observation_columns.items():
        if column in observation_columns:
            observation_select.append(column)
        else:
            observation_select.append("{} AS {}".format(fallback, column))

    observations = {
        _text(row["observation_id"]): row
        for row in _query(
            store,
            "SELECT {} FROM observations ORDER BY observation_id".format(
                ",".join(observation_select)
            ),
        )
    }
    edges = _query(
        store,
        """SELECT observation_id,input_order,input_role,
                  input_source_system,input_filing_id,input_source_fact_id,
                  input_observation_id,input_context_id,input_concept,
                  input_period,input_value,input_unit,input_version_status,
                  input_population_id
           FROM lineage_edges
           ORDER BY observation_id,input_order""",
    )
    report.edge_count = len(edges)

    facts = {
        (_text(row["source_system"]), _text(row["filing_id"]), _text(row["source_fact_id"])):
        row
        for row in _query(
            store,
            """SELECT f.source_system,f.filing_id,f.source_fact_id
               FROM source_facts f
               JOIN (
                 SELECT source_system,filing_id,source_fact_id
                 FROM observations
                 WHERE source_fact_id IS NOT NULL AND source_fact_id!=''
                 UNION
                 SELECT input_source_system,input_filing_id,input_source_fact_id
                 FROM lineage_edges
                 WHERE input_source_fact_id IS NOT NULL
                   AND input_source_fact_id!=''
               ) referenced
                 ON referenced.source_system=f.source_system
                AND referenced.filing_id=f.filing_id
                AND referenced.source_fact_id=f.source_fact_id""",
        )
    }
    filings = {
        (_text(row["source_system"]), _text(row["filing_id"])): row
        for row in _query(
            store,
            """SELECT source_system,filing_id,entity_key,version_status,
                      is_canonical,reporting_year,reporting_period,
                      period_start,period_end
               FROM filings""",
        )
    }
    populations = {
        _text(row["population_id"]): row
        for row in _query(
            store,
            """SELECT population_id,observation_id,source_system,source_table,
                      filing_ids,inclusion_rule,exclusion_rule,row_count,
                      candidate_count,excluded_count,member_key,member_digest,
                      aggregate_value,aggregate_unit,empty_reason
               FROM lineage_populations""",
        )
    }

    population_valid: Dict[str, bool] = {}
    population_filings: Dict[str, Set[str]] = {}
    populations_by_observation: Dict[str, List[str]] = collections.defaultdict(list)
    for population_id, population in populations.items():
        valid = True
        owner_id = _text(population.get("observation_id"))
        populations_by_observation[owner_id].append(population_id)
        if owner_id not in observations:
            valid = False
            problem(
                "population_observation_missing",
                "population {!r} belongs to absent observation {!r}".format(
                    population_id, owner_id
                ),
                owner_id,
                related_id=population_id,
            )

        definition_fields = (
            "source_system",
            "source_table",
            "inclusion_rule",
            "exclusion_rule",
            "member_key",
            "member_digest",
        )
        blank_definition_fields = [
            field for field in definition_fields if not _text(population.get(field))
        ]
        if blank_definition_fields:
            valid = False
            problem(
                "population_definition_incomplete",
                "population {!r} has blank definition field(s): {}".format(
                    population_id, ", ".join(blank_definition_fields)
                ),
                owner_id,
                related_id=population_id,
            )

        try:
            counts = (
                int(population.get("row_count")),
                int(population.get("candidate_count")),
                int(population.get("excluded_count")),
            )
        except (TypeError, ValueError):
            counts = (-1, -1, -1)
            valid = False
            problem(
                "population_counts_invalid",
                "population {!r} has non-integer row/candidate/excluded counts".format(
                    population_id
                ),
                owner_id,
                related_id=population_id,
            )
        if min(counts) < 0 or counts[0] + counts[2] != counts[1]:
            valid = False
            problem(
                "population_counts_invalid",
                "population {!r} counts {} do not satisfy row_count + excluded_count = candidate_count".format(
                    population_id, counts
                ),
                owner_id,
                related_id=population_id,
            )

        raw_filing_ids = population.get("filing_ids")
        parsed_filing_ids: List[Any]
        try:
            parsed = json.loads(_text(raw_filing_ids))
            if not isinstance(parsed, list):
                raise ValueError("expected a JSON array")
            parsed_filing_ids = parsed
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            parsed_filing_ids = []
            valid = False
            problem(
                "population_filing_ids_invalid",
                "population {!r} has invalid filing_ids: {}".format(population_id, exc),
                owner_id,
                related_id=population_id,
            )

        filing_id_set = {_text(value) for value in parsed_filing_ids if _text(value)}
        population_filings[population_id] = filing_id_set
        if not filing_id_set:
            if counts != (0, 0, 0) or not _text(population.get("empty_reason")):
                valid = False
                problem(
                    "population_empty_unsupported",
                    "population {!r} has no filing occurrences but does not document a genuine empty retrieval population".format(
                        population_id
                    ),
                    owner_id,
                    related_id=population_id,
                )
        source_system = _text(population.get("source_system"))
        for filing_id in sorted(filing_id_set):
            filing = filings.get((source_system, filing_id))
            if filing is None:
                valid = False
                problem(
                    "population_filing_missing",
                    "population {!r} names absent occurrence ({!r}, {!r})".format(
                        population_id, source_system, filing_id
                    ),
                    owner_id,
                    related_id=filing_id,
                )
            # A population can legitimately support an asset/facility
            # observation whose canonical entity key differs from the filing
            # entity (joint facilities and separately named LNG project owners
            # are examples).  That legal mapping belongs to the scope/asset
            # registry, not to a generic equality guess here.  The population
            # still has to name real filing occurrences and belong to this edge.
        population_valid[population_id] = valid

    facts_with_occurrence: Set[Tuple[str, str, str]] = set()
    for fact_key in facts:
        occurrence = (fact_key[0], fact_key[1])
        if occurrence in filings:
            facts_with_occurrence.add(fact_key)

    edges_by_output: Dict[str, List[Mapping[str, Any]]] = collections.defaultdict(list)
    adjacency: Dict[str, List[str]] = collections.defaultdict(list)
    edge_states: Dict[Tuple[str, int], Dict[str, Any]] = {}

    for edge in edges:
        output_id = _text(edge.get("observation_id"))
        input_order = int(edge.get("input_order"))
        edges_by_output[output_id].append(edge)
        output = observations.get(output_id)
        if output is None:
            problem(
                "edge_output_missing",
                "lineage edge belongs to absent observation {!r}".format(output_id),
                output_id,
                input_order,
            )

        fact_id = _text(edge.get("input_source_fact_id"))
        observation_id = _text(edge.get("input_observation_id"))
        population_id = _text(edge.get("input_population_id"))
        fact_requested = bool(fact_id)
        observation_requested = bool(observation_id)
        population_requested = bool(population_id)

        if not (fact_requested or observation_requested or population_requested):
            problem(
                "edge_no_referent",
                "edge has only descriptive/occurrence metadata and no fact, observation, or population referent",
                output_id,
                input_order,
            )

        fact_ok = False
        if fact_requested:
            report.exact_fact_references += 1
            fact_key = (
                _text(edge.get("input_source_system")),
                _text(edge.get("input_filing_id")),
                fact_id,
            )
            if not fact_key[0] or not fact_key[1]:
                problem(
                    "source_fact_identity_incomplete",
                    "source fact {!r} lacks source system or filing occurrence".format(fact_id),
                    output_id,
                    input_order,
                    fact_id,
                )
            elif fact_key not in facts:
                problem(
                    "source_fact_missing",
                    "exact source fact ({!r}, {!r}, {!r}) does not exist".format(*fact_key),
                    output_id,
                    input_order,
                    fact_id,
                )
            elif fact_key not in facts_with_occurrence:
                problem(
                    "source_fact_occurrence_missing",
                    "source fact ({!r}, {!r}, {!r}) has no filing occurrence".format(*fact_key),
                    output_id,
                    input_order,
                    fact_id,
                )
            else:
                fact_ok = True
                if output is not None:
                    fact_entity = _text(filings[(fact_key[0], fact_key[1])].get("entity_key"))
                    output_entity = _text(output.get("entity_key"))
                    if fact_entity != output_entity and not observation_requested:
                        fact_ok = False
                        problem(
                            "source_fact_entity_mismatch",
                            "output entity {!r} points directly to fact for entity {!r}".format(
                                output_entity, fact_entity
                            ),
                            output_id,
                            input_order,
                            fact_id,
                        )

        input_observation = observations.get(observation_id) if observation_requested else None
        observation_ok = input_observation is not None if observation_requested else False
        if observation_requested:
            report.observation_references += 1
            if input_observation is None:
                problem(
                    "input_observation_missing",
                    "input observation {!r} does not exist".format(observation_id),
                    output_id,
                    input_order,
                    observation_id,
                )
            else:
                adjacency[output_id].append(observation_id)
                if output is not None:
                    output_entity = _text(output.get("entity_key"))
                    input_entity = _text(input_observation.get("entity_key"))
                    if output_entity != input_entity:
                        observation_ok = False
                        problem(
                            "input_observation_entity_mismatch",
                            "output entity {!r} points to input observation for entity {!r}".format(
                                output_entity, input_entity
                            ),
                            output_id,
                            input_order,
                            observation_id,
                        )
                    if _is_present(output) and not _is_present(input_observation):
                        observation_ok = False
                        problem(
                            "input_observation_not_present",
                            "present derived output points to input with availability {!r}".format(
                                input_observation.get("availability")
                            ),
                            output_id,
                            input_order,
                            observation_id,
                        )

                redundant_fields = (
                    ("input_source_system", "source_system"),
                    ("input_filing_id", "filing_id"),
                    ("input_source_fact_id", "source_fact_id"),
                )
                for edge_field, observation_field in redundant_fields:
                    supplied = _text(edge.get(edge_field))
                    if supplied and supplied != _text(input_observation.get(observation_field)):
                        observation_ok = False
                        problem(
                            "redundant_occurrence_mismatch",
                            "{}={!r} disagrees with input observation {}={!r}".format(
                                edge_field,
                                supplied,
                                observation_field,
                                _text(input_observation.get(observation_field)),
                            ),
                            output_id,
                            input_order,
                            observation_id,
                        )
                supplied_context = _text(edge.get("input_context_id"))
                if supplied_context and supplied_context != _text(
                    input_observation.get("source_context_id")
                ):
                    observation_ok = False
                    problem(
                        "redundant_context_mismatch",
                        "input_context_id={!r} disagrees with linked observation".format(
                            supplied_context
                        ),
                        output_id,
                        input_order,
                        observation_id,
                    )
                supplied_version = _text(edge.get("input_version_status"))
                if supplied_version and supplied_version != _text(
                    input_observation.get("version_status")
                ):
                    observation_ok = False
                    problem(
                        "input_version_status_mismatch",
                        "stored input version {!r} disagrees with linked observation version {!r}".format(
                            supplied_version,
                            _text(input_observation.get("version_status")),
                        ),
                        output_id,
                        input_order,
                        observation_id,
                    )

        population_ok = False
        if population_requested:
            report.population_references += 1
            population = populations.get(population_id)
            if population is None:
                problem(
                    "input_population_missing",
                    "input population {!r} does not exist".format(population_id),
                    output_id,
                    input_order,
                    population_id,
                )
            else:
                population_ok = population_valid.get(population_id, False)
                allowed_owners = {output_id}
                if observation_requested:
                    allowed_owners.add(observation_id)
                owner_id = _text(population.get("observation_id"))
                if owner_id not in allowed_owners:
                    population_ok = False
                    problem(
                        "input_population_owner_mismatch",
                        "population {!r} belongs to observation {!r}, not this edge or its input".format(
                            population_id, owner_id
                        ),
                        output_id,
                        input_order,
                        population_id,
                    )
                supplied_system = _text(edge.get("input_source_system"))
                population_system = _text(population.get("source_system"))
                if supplied_system and supplied_system != population_system:
                    population_ok = False
                    problem(
                        "input_population_source_mismatch",
                        "edge source system {!r} disagrees with population source system {!r}".format(
                            supplied_system, population_system
                        ),
                        output_id,
                        input_order,
                        population_id,
                    )
                supplied_filing = _text(edge.get("input_filing_id"))
                if supplied_filing and supplied_filing not in population_filings.get(
                    population_id, set()
                ):
                    population_ok = False
                    problem(
                        "input_population_filing_mismatch",
                        "edge filing {!r} is absent from population {!r}".format(
                            supplied_filing, population_id
                        ),
                        output_id,
                        input_order,
                        population_id,
                    )

        edge_states[(output_id, input_order)] = {
            "fact_requested": fact_requested,
            "fact_ok": fact_ok,
            "observation_requested": observation_requested,
            "observation_ok": observation_ok,
            "observation_id": observation_id,
            "population_requested": population_requested,
            "population_ok": population_ok,
        }

    # Scope rules are checked across the full sibling input set, not one edge at
    # a time.  Different periods are intentionally allowed (for example annual
    # minus three filed quarters), while a rule demanding identical scope may
    # not combine two legal/facility bases.
    for output_id, output_edges in edges_by_output.items():
        output = observations.get(output_id)
        if output is None or not (_is_present(output) and _is_derived(output)):
            continue
        scope = _text(output.get("scope"))
        if re.search(r"\b(?:unresolved|incompatible)\b", scope, re.IGNORECASE):
            problem(
                "present_output_scope_unresolved",
                "present derived observation has unresolved/incompatible scope {!r}".format(scope),
                output_id,
            )
        rule = _text(output.get("scope_rule"))
        if not _scope_requires_matching_inputs(rule):
            continue
        linked = [
            observations[_text(edge.get("input_observation_id"))]
            for edge in output_edges
            if _text(edge.get("input_observation_id")) in observations
        ]
        input_scopes = {_text(row.get("scope")) for row in linked}
        if linked and ("" in input_scopes or len(input_scopes) != 1):
            problem(
                "input_scope_mismatch",
                "scope rule {!r} received input scopes {}".format(
                    rule, sorted(input_scopes)
                ),
                output_id,
            )
        elif linked and _scope_requires_output_identity(rule):
            common_scope = next(iter(input_scopes))
            if scope != common_scope:
                problem(
                    "output_scope_mismatch",
                    "scope rule {!r} requires output {!r} to equal input scope {!r}".format(
                        rule, scope, common_scope
                    ),
                    output_id,
                )

    graph_nodes = set(observations)
    finish_order, cycles, cyclic_nodes = _graph_order_and_cycles(graph_nodes, adjacency)
    for cycle in cycles:
        problem(
            "observation_cycle",
            "observation lineage cycle: {}".format(" -> ".join(cycle + (cycle[0],))),
            cycle[0],
            related_id=",".join(cycle),
        )

    linked_observation_ids = {
        _text(edge.get("input_observation_id"))
        for edge in edges
        if _text(edge.get("input_observation_id"))
    }
    own_terminal: Dict[str, bool] = {}
    for observation_id, observation in observations.items():
        source_fact_id = _text(observation.get("source_fact_id"))
        fact_ok = False
        if source_fact_id:
            fact_key = (
                _text(observation.get("source_system")),
                _text(observation.get("filing_id")),
                source_fact_id,
            )
            fact_ok = fact_key in facts_with_occurrence
            if not fact_ok and observation_id in linked_observation_ids:
                problem(
                    "input_observation_source_fact_missing",
                    "linked leaf observation does not resolve exact source fact {}".format(
                        fact_key
                    ),
                    observation_id,
                    related_id=source_fact_id,
                )
        population_ok = any(
            population_valid.get(population_id, False)
            for population_id in populations_by_observation.get(observation_id, ())
        )
        own_terminal[observation_id] = fact_ok or population_ok

    grounded: Dict[str, bool] = {}
    for observation_id in finish_order:
        if observation_id in cyclic_nodes:
            grounded[observation_id] = False
            continue
        output_edges = edges_by_output.get(observation_id, ())
        if not output_edges:
            grounded[observation_id] = own_terminal.get(observation_id, False)
            continue
        all_edges_grounded = True
        for edge in output_edges:
            input_order = int(edge.get("input_order"))
            state = edge_states[(observation_id, input_order)]
            requested_count = sum(
                1
                for key in (
                    "fact_requested",
                    "observation_requested",
                    "population_requested",
                )
                if state[key]
            )
            if requested_count == 0:
                all_edges_grounded = False
                continue
            if state["fact_requested"] and not state["fact_ok"]:
                all_edges_grounded = False
            if state["population_requested"] and not state["population_ok"]:
                all_edges_grounded = False
            if state["observation_requested"]:
                child_id = state["observation_id"]
                if not state["observation_ok"] or not grounded.get(child_id, False):
                    all_edges_grounded = False
            elif not (state["fact_ok"] or state["population_ok"]):
                all_edges_grounded = False
        grounded[observation_id] = all_edges_grounded

    present_derived = [
        (observation_id, observation)
        for observation_id, observation in observations.items()
        if _is_present(observation) and _is_derived(observation)
    ]
    report.present_derived_count = len(present_derived)
    report.grounded_present_derived_count = sum(
        1 for observation_id, _ in present_derived if grounded.get(observation_id, False)
    )
    for observation_id, _ in present_derived:
        if not edges_by_output.get(observation_id):
            problem(
                "derived_without_edges",
                "present derived observation has no persisted lineage edge",
                observation_id,
            )
        if not grounded.get(observation_id, False):
            problem(
                "derived_path_unterminated",
                "at least one lineage branch does not terminate in an exact source fact or valid population",
                observation_id,
            )

    report.problems.sort(
        key=lambda item: (
            item.code,
            item.observation_id,
            -1 if item.input_order is None else item.input_order,
            item.related_id,
            item.message,
        )
    )
    return report


__all__ = (
    "LineageInvariantError",
    "LineageProblem",
    "LineageReport",
    "validate_persisted_lineage",
)
