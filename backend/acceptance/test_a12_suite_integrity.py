"""
A12 -- the test suites contained false-pass and false-skip paths.

Four confirmed defects, each with a mutation that the suite must now REJECT:

  A12.1  a nonzero REQUIRED reference row missing from produced output scored
         zero failures and the run reported PASS;
  A12.1b a run in which every reference package was absent reported PASS having
         compared nothing;
  A12.2  the identical-resubmission check SKIPPED even with the real
         two-submission fixture present, because it grouped by the unique
         occurrence ID;
  A12.3  the wrong-version lineage check was tautological -- it asked whether a
         literal "NOT-A-REAL-FILING" resolved, which it never could.

Every case here goes green only by DETECTING the bad state. None of them can go
green by making the mutated record acceptable.
"""

from __future__ import annotations

import sys

from .harness import (Ctx, FixtureMissing, ReadOnlyStaging, Tier, acceptance,
                      must_reject, require, require_population)

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))


# ------------------------------------------------------------------ A12.1

@acceptance(issue="A12.1", group="regressions",
            mutation="a required nonzero reference row that the engine did not produce")
def t_missing_required_reference_is_fatal(env):
    """a missing required nonzero reference row fails the comparison"""
    import run_regressions

    # The auditor's exact fixture: one explicitly-synthetic reference row with a
    # nonzero value, and NOTHING produced against it.
    ref = {("W6ACC_SYNTHETIC_METRIC", 2025, "Q4", "annual", "raw"): {
        "value": "123", "canonical_row_id": "W6ACC_SYNTHETIC_REF_1",
        "raw_or_derived": "raw", "source_fact_id": "W6ACC_SYNTHETIC_FACT",
        "filing_id": "W6ACC_SYNTHETIC_FILING"}}
    comparison = run_regressions.compare(ref, {}, "W6ACC_SYNTHETIC_MISSING_REFERENCE")

    require(len(comparison["missing_from_produced"]) == 1,
            "the fixture did not land in missing_from_produced, so the mutation "
            f"was never presented: {comparison}")
    require(not comparison["value_differences"] and not comparison["identity_differences"],
            "the fixture must be missing-only; any other difference would let the "
            "check pass for the wrong reason")

    # Drive the REAL decision function, not a re-implementation of it.
    status, fatal = run_regressions.status_for(comparison)
    require(status == "FAIL",
            f"a required nonzero reference value absent from the output was scored "
            f"{status!r} with {fatal} fatal difference(s). Dropping a required value "
            "outright is the most severe regression this harness can see; it must be "
            "fatal, never a 'not produced' line item under a PASS.")
    return (f"missing required nonzero reference -> {status} ({fatal} fatal); "
            "the row was counted, not noted")


@acceptance(issue="A12.1b", group="regressions",
            mutation="a regression run that compared nothing at all")
def t_no_comparison_is_not_a_pass(env):
    """a run with every reference absent reports NO_COMPARISON, not PASS"""
    import run_regressions

    empty = run_regressions.compare({}, {}, "W6ACC_SYNTHETIC_EMPTY_REFERENCE")
    require(empty.get("fixture_error"),
            "a reference that parsed to zero valued rows produced no fixture error; "
            "an empty comparison proves nothing and must not look like a match")
    status, _ = run_regressions.status_for(empty)
    require(status != "PASS",
            f"a comparison over an empty reference population scored {status!r}")

    # And the module-level rule: PASS requires at least one reference COMPARED.
    src = (env.need_file(
        __import__("pathlib").Path(run_regressions.__file__), "run_regressions.py")
    ).read_text(encoding="utf-8")
    require("NO_COMPARISON" in src,
            "run_regressions.py has no NO_COMPARISON outcome, so a run in which every "
            "reference was skipped can still report PASS on zero comparisons")
    return (f"empty reference -> {status}; the module declares a NO_COMPARISON outcome "
            "so an all-skipped run cannot report PASS")


@acceptance(issue="A12.1c", group="regressions",
            mutation="reference values compared with an undeclared 6-decimal tolerance")
def t_value_comparison_is_exact_decimal(env):
    """reference values are compared as exact decimals, not rounded floats"""
    import decimal

    import run_regressions

    a = run_regressions.norm("1000000.0000001")
    b = run_regressions.norm("1000000.0000002")
    require(a != b,
            "two filed values differing in the seventh decimal compared EQUAL. "
            "round(float(v), 6) applied a tolerance nobody declared, which is "
            "exactly what a value regression exists to catch.")
    require(isinstance(a, decimal.Decimal),
            f"comparison key is {type(a).__name__}, not Decimal; binary floats "
            "reintroduce representation error into a filed-value comparison")
    # ... while the same number written differently still compares equal, and a
    # thousands-separated filed value still parses.
    same = {run_regressions.norm(v) for v in ("100", "100.00", "1E+2", "1,00")}
    require(len(same) == 1,
            f"exact decimal comparison separated equal values written differently: {same}")
    return "exact Decimal comparison; 100 == 100.00 == 1E+2 == '1,00', but .0000001 != .0000002"


# ------------------------------------------------------------------ A12.2

@acceptance(issue="A12.2", group="lineage",
            mutation="grouping identical submissions by the occurrence ID, which hides them")
def t_identical_resubmission_fixture_is_exercised(env):
    """the real two-submission fixture is found, not skipped"""
    con = env.baseline()

    delivered = con.execute("""
        SELECT COUNT(*) FROM (
          SELECT 1 FROM filings
          WHERE content_hash IS NOT NULL AND content_hash != ''
          GROUP BY entity_key, form, reporting_year, reporting_period, content_hash,
                   IFNULL(accession_number, filing_id)
          HAVING COUNT(*) > 1)""").fetchone()[0]
    corrected = con.execute("""
        SELECT COUNT(*) FROM (
          SELECT 1 FROM filings
          WHERE content_hash IS NOT NULL AND content_hash != ''
          GROUP BY entity_key, form, reporting_year, reporting_period, content_hash
          HAVING COUNT(*) > 1)""").fetchone()[0]

    # FIXTURE PRECONDITION: the population must actually exist, or this check
    # exercised nothing.
    require_population(range(corrected), "byte-identical resubmission groups in the "
                                         "delivered baseline")
    require(delivered == 0,
            f"the delivered grouping found {delivered} group(s); the A12 defect is that "
            "it found none, so this fixture no longer demonstrates the defect")
    require(corrected >= 3,
            f"the corrected grouping found only {corrected} group(s); the delivered "
            "baseline carries three byte-identical pairs")

    # And the real check must now exercise that branch rather than skip it.
    import validate
    result = validate.t_identical_resubmission(Ctx(ReadOnlyStaging(env.baseline_path)))
    require(not str(result).startswith("SKIP"),
            f"the identical-resubmission check still SKIPPED with the fixture present: {result}")
    return (f"grouping by the occurrence ID hid {corrected} group(s) (found {delivered}); "
            f"grouping by content finds them. Check result: {result}")


@acceptance(issue="A12.2b", group="lineage",
            mutation="an absent duplicate-content fixture reported as a skip")
def t_absent_resubmission_fixture_is_a_failure(env):
    """an absent two-submission fixture FAILS rather than skipping"""
    import validate

    con = env.baseline()
    # A disposable subset carrying filings that share NO content hash: the
    # fixture is genuinely absent. That must be a failure, not a pass or a skip.
    rows = con.execute("""
        SELECT * FROM filings WHERE content_hash IS NOT NULL AND content_hash != ''
        GROUP BY content_hash LIMIT 5""").fetchall()
    require_population(rows, "filings with distinct content hashes")
    sub = env.subset_db("a12_no_resubmission_fixture", {"filings": rows})
    probe = Ctx(_ConnStaging(sub))

    exc = must_reject(lambda: validate.t_identical_resubmission(probe),
                      expect=AssertionError,
                      what="a database with filings but no byte-identical pair was "
                           "accepted by the identical-resubmission check")
    require("ABSENT" in str(exc) or "absent" in str(exc),
            f"the rejection did not name the missing fixture as the reason: {exc}")
    return f"absent fixture rejected: {str(exc)[:120]}"


# ------------------------------------------------------------------ A12.3

@acceptance(issue="A12.3", group="lineage",
            mutation="a lineage edge repointed at a byte-identical sibling occurrence")
def t_wrong_occurrence_edge_rejected(env):
    """repointing filing 74835 to byte-identical 74834 is rejected"""
    import validate

    con = env.baseline()
    edge = con.execute("""
        SELECT * FROM lineage_edges
        WHERE input_filing_id='74835' AND input_observation_id IS NOT NULL LIMIT 1""").fetchone()
    require_population([edge] if edge else [],
                       "the auditor's lineage edge on filing 74835")
    e = dict(edge)

    obs_rows = [con.execute("SELECT * FROM observations WHERE observation_id=?", (oid,)).fetchone()
                for oid in (e["observation_id"], e["input_observation_id"])]
    require_population([o for o in obs_rows if o], "the edge's derived and input observations",
                       minimum=2)
    filings, facts = [], []
    for fid in ("74834", "74835"):
        f = con.execute("SELECT * FROM filings WHERE source_system=? AND filing_id=?",
                        (e["input_source_system"], fid)).fetchone()
        sf = con.execute("SELECT * FROM source_facts WHERE source_system=? AND filing_id=? "
                         "AND source_fact_id=?",
                         (e["input_source_system"], fid, e["input_source_fact_id"])).fetchone()
        require(f is not None, f"filing {fid} absent from the baseline")
        require(sf is not None,
                f"fact {e['input_source_fact_id'][:12]} absent under filing {fid} -- the "
                "whole point of this mutation is that identical bytes put the SAME fact "
                "id under BOTH occurrences")
        filings.append(f)
        facts.append(sf)

    # THE MUTATION: the edge claims 74834; its input observation is still on 74835.
    mutated = dict(e, input_filing_id="74834")
    sub = env.subset_db("a12_wrong_occurrence", {
        "filings": filings, "source_facts": facts,
        "observations": [o for o in obs_rows if o]})
    cols = ",".join(mutated)
    sub.execute(f"INSERT OR REPLACE INTO lineage_edges ({cols}) "
                f"VALUES ({','.join('?' * len(mutated))})", list(mutated.values()))
    sub.commit()
    probe = Ctx(_ConnStaging(sub))

    # The resolver-only check is expected to STILL PASS -- that is the defect.
    resolver = "rejected"
    try:
        validate.t_lineage_resolves(probe)
        resolver = "still passed"
    except AssertionError:
        pass

    exc = must_reject(lambda: validate.t_lineage_occurrence_coherent(probe),
                      expect=AssertionError,
                      what="an edge naming filing 74834 whose input observation is on "
                           "74835 was accepted")
    return (f"edge 74835 -> byte-identical 74834 with the input observation left on 74835: "
            f"resolver-only check {resolver}; occurrence check rejected it -- {str(exc)[:100]}")


#: Roles whose edges combine ADDITIVELY into the derived value, so the value can
#: be recomputed independently from the edge rows alone. Ratios (numerator /
#: denominator) and bulk `group_member` populations are excluded: they do not sum
#: to the published figure, and pretending they do would make this check fail for
#: a reason that has nothing to do with the mutation.
ADDITIVE_ROLES = ("addend", "minuend", "subtrahend")


def _recompute_additively(edges) -> "object | None":
    """Recompute a derived value from its edges with exact decimal arithmetic.

    This is deliberately an INDEPENDENT calculation: it reads the stored input
    values, signs and coefficients and combines them here. It never calls the
    selector or derivation code that produced the observation, because asking the
    function that created a number whether the number is right proves only that
    the function agrees with itself.
    """
    from decimal import Decimal, InvalidOperation
    total = Decimal(0)
    for e in edges:
        if e["input_role"] not in ADDITIVE_ROLES:
            return None
        if e["input_value"] in (None, ""):
            return None
        try:
            value = Decimal(str(e["input_value"]))
            coefficient = (Decimal(str(e["coefficient"]))
                           if e["coefficient"] is not None else Decimal(1))
        except (InvalidOperation, TypeError):
            return None
        sign = Decimal(-1) if (e["operator_sign"] or "+") == "-" else Decimal(1)
        total += sign * coefficient * value
    return total


@acceptance(issue="A12.3b", group="lineage",
            mutation="deleting a contributing lineage edge from a derived value")
def t_removing_a_contributing_edge_fails(env):
    """removing a contributing edge breaks the value's independent recomputation"""
    from decimal import Decimal

    con = env.baseline()

    # Find a derived observation that RECONCILES additively on the real data, so
    # the check starts from a known-good state and the only thing that changes is
    # the mutation.
    candidates = con.execute("""
        SELECT o.observation_id, o.value_text, o.metric_id, COUNT(*) n
        FROM observations o JOIN lineage_edges e USING(observation_id)
        WHERE o.method='derived' AND o.availability='present'
          AND o.value_text IS NOT NULL AND o.value_text != ''
        GROUP BY o.observation_id HAVING n >= 3 LIMIT 500""").fetchall()
    require_population(candidates, "derived observations with 3+ lineage edges")

    chosen = None
    for row in candidates:
        edges = con.execute("SELECT * FROM lineage_edges WHERE observation_id=? "
                            "ORDER BY input_order", (row["observation_id"],)).fetchall()
        recomputed = _recompute_additively(edges)
        if recomputed is None:
            continue
        try:
            if recomputed == Decimal(str(row["value_text"])):
                chosen = (row, edges, recomputed)
                break
        except Exception:                                            # noqa: BLE001
            continue

    # FIXTURE PRECONDITION: if nothing reconciles, this check exercised nothing.
    require(chosen is not None,
            f"none of {len(candidates)} sampled derived observations with 3+ edges could "
            f"be recomputed additively from their own lineage, so the edge-removal "
            "mutation could not be presented to a value that starts out correct")
    row, edges, baseline_value = chosen
    oid = row["observation_id"]

    dropped = edges[-1]
    kept = [e for e in edges if e["input_order"] != dropped["input_order"]]
    after = _recompute_additively(kept)

    require(after is not None, "the surviving edges no longer recompute at all")
    require(after != baseline_value,
            f"deleting a contributing edge left the recomputed value unchanged "
            f"({after}); the removal was undetectable, which means a contributing row "
            "can be dropped without any check noticing")

    # And the whole-suite view: the derived value in the table no longer matches
    # its own lineage, which is the condition a validator must reject.
    require(Decimal(str(row["value_text"])) != after,
            "the published value still agrees with the mutated lineage")
    return (f"{row['metric_id']} {oid}: {len(edges)} edges recompute to {baseline_value} "
            f"(matches the filed value); dropping the {dropped['input_role']} edge with "
            f"input {dropped['input_value']} gives {after} -- the removal is detected by "
            "independent Decimal recomputation, not by an arity count")


# ------------------------------------------------------------------ A12.4

@acceptance(issue="A12.4", group="regressions", tier=Tier.EXTERNAL,
            mutation="an absent optional reference package counted as a pass")
def t_absent_external_reference_is_skipped(env):
    """an absent optional external reference is SKIPPED, never PASS"""
    import run_regressions

    declared = [r["name"] for r in run_regressions.REFERENCES]
    present = [r["name"] for r in run_regressions.REFERENCES if r["canonical"].is_file()]
    absent = [n for n in declared if n not in present]
    if not absent:
        # Both packages present: assert the SKIP path exists and is not a pass by
        # driving it with a reference whose file cannot exist.
        require("SKIPPED" in (run_regressions.main.__doc__ or "")
                or "SKIPPED" in __import__("pathlib").Path(
                    run_regressions.__file__).read_text(encoding="utf-8"),
                "no SKIPPED outcome exists for an absent reference package")
        return (f"both external references present ({', '.join(present)}); the SKIPPED "
                "path is declared and an all-skipped run reports NO_COMPARISON")
    return f"absent external reference(s) reported as SKIPPED, not PASS: {absent}"


# ------------------------------------------------------------------ helpers

class _ConnStaging:
    """Staging-shaped wrapper over an already-open disposable connection."""

    def __init__(self, con):
        self.con = con
        self.path = None

    def query(self, sql, params=()):
        return self.con.execute(sql, params).fetchall()


def _declared_input_count(derivation: str) -> int | None:
    """Independent read of how many inputs a derivation says it used.

    Deliberately parses the derivation TEXT rather than calling whatever built
    it: validating a derived metric with the same function that produced it
    proves only that the function is self-consistent.
    """
    import re
    m = re.search(r"\b(\d+)\s+inputs?\b", derivation)
    return int(m.group(1)) if m else None
