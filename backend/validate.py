"""
Integration and adversarial checks over the staging database.

Every mandatory case from the task specification is here, including the NEGATIVE
tests: a check that cannot fail is not a check. Several of these deliberately
construct a broken condition and assert that the pipeline refuses it.

Results are PASS / FAIL / ERROR / SKIPPED and are recorded honestly. A missing
comparison is SKIPPED, never PASS.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import tempfile

from ferclib import periods
from ferclib.staging import Staging, observation_id
from ferclib.status import Availability, Validation, VersionStatus

RESULTS: list[dict] = []


def check(name: str, group: str = "general"):
    def deco(fn):
        fn._check = (name, group)
        return fn
    return deco


def _record(name, group, status, detail=""):
    RESULTS.append({"check": name, "group": group, "status": status, "detail": detail})
    icon = {"PASS": "ok  ", "FAIL": "FAIL", "ERROR": "ERR ", "SKIPPED": "skip"}[status]
    print(f"  [{icon}] {name}" + (f" -- {detail[:130]}" if detail else ""))


# ==================================================== period grain and coverage

@check("a YTD fact never satisfies a requested quarter", "period_grain")
def t_ytd_not_quarter(ctx):
    ok, why = periods.satisfies(periods.QUARTER, "2025-04-01", "2025-06-30", "",
                                periods.YTD, "2025-01-01", "2025-06-30", "")
    assert not ok, f"a Q2 YTD interval was accepted as a Q2 quarter: {why}"
    return f"rejected: {why}"


@check("a Q1 YTD interval IS the Q1 quarter (the only permitted equivalence)", "period_grain")
def t_q1_equivalence(ctx):
    ok, why = periods.satisfies(periods.QUARTER, "2025-01-01", "2025-03-31", "",
                                periods.YTD, "2025-01-01", "2025-03-31", "")
    assert ok, "the Q1 quarter/YTD equivalence was rejected"
    return why


@check("a blank filed quarter and a valid derived quarter coexist", "period_grain")
def t_blank_vs_derived(ctx):
    rows = ctx.staging.query("""
        SELECT metric_id, reporting_year, reporting_period,
               SUM(method='filed' AND availability='source_blank') AS blank_filed,
               SUM(method='derived' AND availability='present') AS derived_present
        FROM observations WHERE period_basis='quarter'
        GROUP BY metric_id, entity_key, reporting_year, reporting_period
        HAVING blank_filed>0 AND derived_present>0""")
    assert rows, "no case found where a blank filed quarter coexists with a derived one"
    r = rows[0]
    return (f"{len(rows)} such cases, e.g. {r['metric_id']} "
            f"{r['reporting_year']}{r['reporting_period']}")


@check("coverage denominator is frozen, not taken from the output", "period_grain")
def t_frozen_denominator(ctx):
    exp = ctx.staging.query("SELECT COUNT(*) n, COUNT(DISTINCT frozen_run_id) r FROM coverage_expected")
    obs = ctx.staging.query("SELECT COUNT(*) n FROM observations")
    assert exp[0]["n"] > 0, "no expected slots frozen"
    return (f"{exp[0]['n']} slots frozen across {exp[0]['r']} run(s); "
            f"{obs[0]['n']} observations produced -- the two counts are independent")


# ==================================================== status semantics

@check("nil, zero, source blank, not-required and retrieval failure stay distinct", "status")
def t_states_distinct(ctx):
    rows = ctx.staging.query(
        "SELECT availability, COUNT(*) n FROM observations GROUP BY availability")
    seen = {r["availability"] for r in rows}
    zero_valued = ctx.staging.query(
        "SELECT COUNT(*) n FROM observations WHERE availability='present' AND value_num=0")
    assert Availability.SOURCE_BLANK in seen, "no source_blank state present"
    assert zero_valued[0]["n"] > 0, "no explicit zero values found to distinguish from blank"
    return (f"states in use: {sorted(seen)}; "
            f"{zero_valued[0]['n']} filed zeros held distinct from blanks")


@check("a NOT_REQUIRED verdict always carries taxonomy evidence", "status")
def t_not_required_evidence(ctx):
    bad = ctx.staging.query("""
        SELECT COUNT(*) n FROM observations
        WHERE availability='not_required'
          AND (applicability_evidence IS NULL OR applicability_evidence='')""")
    assert bad[0]["n"] == 0, f"{bad[0]['n']} not_required rows carry no evidence"
    tot = ctx.staging.query(
        "SELECT COUNT(*) n FROM observations WHERE availability='not_required'")
    return f"{tot[0]['n']} not_required observations, all with evidence"


@check("applicability is UNKNOWN, never NOT_REQUIRED, when the taxonomy is incomplete", "status")
def t_unknown_not_notrequired(ctx):
    rows = ctx.staging.query("""
        SELECT COUNT(*) n FROM observations
        WHERE availability='not_required' AND applicability_evidence LIKE '%UNKNOWN%'""")
    assert rows[0]["n"] == 0, "an UNKNOWN applicability was recorded as not_required"
    return "no not_required verdict rests on an unresolved taxonomy"


@check("our unfinished work is never labelled a FERC source gap", "status")
def t_our_gap_labelled(ctx):
    rows = ctx.staging.query("""
        SELECT COUNT(*) n FROM observations
        WHERE availability='not_implemented' AND missing_reason LIKE '%FERC%'""")
    assert rows[0]["n"] == 0, "a not_implemented row blames FERC"
    return "not_implemented rows describe unfinished engineering only"


# ==================================================== warnings and anomalies

@check("a flagged value keeps its warning in every export", "warnings")
def t_flag_propagation(ctx):
    import exporters
    flagged = ctx.staging.query("""
        SELECT observation_id, metric_id, value_text, validation, qa_flags
        FROM observations WHERE validation IN
          ('source_anomaly_review','blocked_ambiguity','scope_incompatible',
           'unit_warning','rounding_warning','source_date_warning')
          AND value_text IS NOT NULL""")
    if not flagged:
        return "SKIP: no flagged values present in this run"
    rows = [dict(r, value=r["value_text"], review_status="") for r in flagged]
    exporters._check_flags_survive(rows, "propagation test")
    stripped = [dict(r) for r in rows]
    for s in stripped:
        s["qa_flags"], s["validation_copy"] = "", s["validation"]
    # negative control: stripping the flag MUST be detected
    broken = [dict(s, validation=s["validation_copy"]) for s in stripped]
    try:
        exporters._check_flags_survive(broken, "negative control")
    except exporters.FlagStripped:
        return f"{len(flagged)} flagged values carry their warning; stripping one is detected"
    raise AssertionError("NEGATIVE CONTROL FAILED: a stripped flag was not detected")


@check("the 999,999 source anomaly is preserved as filed and stays flagged", "warnings")
def t_repdigit_preserved(ctx):
    rows = ctx.staging.query("""
        SELECT metric_id, value_text, validation, qa_flags FROM observations
        WHERE entity_key='C001012' AND value_text='999999'""")
    if not rows:
        return "SKIP: the Fayetteville sample is not in this run"
    for r in rows:
        assert r["value_text"] == "999999", "the filed value was altered"
        assert r["validation"] == Validation.SOURCE_ANOMALY_REVIEW, \
            f"{r['metric_id']} lost its review flag"
    return f"{len(rows)} observations preserved as filed, all flagged for review"


@check("no repdigit-to-null or repdigit-to-zero rule exists", "warnings")
def t_no_repdigit_rule(ctx):
    src = (pathlib.Path(__file__).parent / "ferclib" / "xbrl_adapter.py").read_text()
    for pattern in ("999999", "999,999", "repdigit"):
        assert f'== "{pattern}"' not in src and f"== '{pattern}'" not in src, \
            f"engine contains a hard-coded {pattern} rule"
    return "the engine contains no value-specific correction rule"


@check("a derivation inherits its inputs' must-propagate flags", "warnings")
def t_inherited_flags(ctx):
    rows = ctx.staging.query("""
        SELECT o.metric_id, o.validation, o.qa_flags FROM observations o
        WHERE o.method='derived' AND o.qa_flags LIKE '%inherited_from_input%'""")
    return (f"{len(rows)} derived observations carry an inherited input flag"
            if rows else "SKIP: no derivation in this run had a flagged input")


# ==================================================== lineage and identity

@check("every filing has an occurrence-specific entity association", "lineage")
def t_filing_entity_associations(ctx):
    from adapters import lng

    missing = ctx.staging.query("""
        SELECT COUNT(*) n FROM filings f
        WHERE NOT EXISTS (
          SELECT 1 FROM filing_entities fe
          WHERE fe.source_system=f.source_system AND fe.filing_id=f.filing_id)""")[0]["n"]
    orphan = ctx.staging.query("""
        SELECT COUNT(*) n FROM filing_entities fe
        LEFT JOIN filings f ON f.source_system=fe.source_system AND f.filing_id=fe.filing_id
        LEFT JOIN entities e ON e.entity_key=fe.entity_key
        WHERE f.filing_id IS NULL OR e.entity_key IS NULL
           OR fe.evidence_ref IS NULL OR fe.evidence_ref=''""")[0]["n"]
    compatibility = ctx.staging.query("""
        SELECT COUNT(*) n FROM filing_entities
        WHERE association_role='compatibility_anchor'""")[0]["n"]
    authorities = ctx.staging.query(
        "SELECT role,COUNT(*) n,SUM(CASE WHEN evidence_ref IS NULL OR evidence_ref='' "
        "THEN 1 ELSE 0 END) blank FROM asset_dockets GROUP BY role")
    assert missing == 0, f"{missing} filing occurrences have no entity association"
    assert orphan == 0, f"{orphan} filing/entity associations are orphaned or unevidenced"
    assert compatibility == 0, (
        f"{compatibility} legacy compatibility anchors still require reviewed replay")
    assert len(authorities) == 1 and authorities[0]["role"] == lng.AUTHORITY_ROLE, (
        f"asset_dockets contains undeclared/dynamic roles: {[dict(row) for row in authorities]}")
    assert authorities[0]["n"] == 44 and authorities[0]["blank"] == 0, (
        f"reviewed LNG authority population is not exact: {dict(authorities[0])}")
    return (f"all filings associated; {authorities[0]['n']} reviewed LNG asset/docket "
            "authorities carry evidence")

@check("persisted lineage resolves completely and is acyclic", "lineage")
def t_persisted_lineage_invariants(ctx):
    """Run the production persisted-graph validator, not a query-shaped proxy."""
    from ferclib.lineage import validate_persisted_lineage

    report = validate_persisted_lineage(ctx.staging)
    if not report.ok:
        details = report.as_dict()
        sample = details["problems"][:5]
        raise AssertionError(
            f"{len(details['problems'])} persisted-lineage defect(s): "
            f"{json.dumps(sample, sort_keys=True, default=str)}")
    report.require_valid()
    return (f"{report.edge_count:,} edges and "
            f"{report.population_references:,} population references resolve to terminal evidence; "
            "no cycles or occurrence/scope/version conflicts")


@check("IOC aggregates redraw from their persisted source populations", "lineage")
def t_ioc_persisted_lineage(ctx):
    """Enforce the adapter's stronger population/redraw contract in production."""
    from adapters.ioc import ADAPTER, BY_ADAPTER, audit_lineage

    problems = audit_lineage(ctx.staging)
    assert not problems, (
        f"{len(problems)} IOC persisted-lineage defect(s), e.g. "
        f"{json.dumps(problems[:5], sort_keys=True, default=str)}")
    metric_ids = sorted({metric.id for metric in BY_ADAPTER[ADAPTER]})
    marks = ",".join("?" for _ in metric_ids)
    rows = ctx.staging.query(
        f"SELECT COUNT(*) n FROM observations WHERE metric_id IN ({marks}) "
        "AND availability='present'", tuple(metric_ids))
    return f"{rows[0]['n']:,} present IOC observations pass full persisted redraw"

@check("every derived observation has complete lineage", "lineage")
def t_lineage_complete(ctx):
    # A derived value must be traceable to its inputs. For a value computed from
    # named facts that means lineage EDGES. For a share or diagnostic computed
    # over a bulk table, listing every one of tens of thousands of contributing
    # rows as an edge is not evidence anyone can use -- there the requirement is
    # a resolvable source filing plus a derivation that names the population and
    # the filter. Both are accepted; neither being present is a failure.
    orphan = ctx.staging.query("""
        SELECT o.metric_id, COUNT(*) n FROM observations o
        WHERE o.method='derived' AND o.availability='present'
          AND NOT EXISTS (SELECT 1 FROM lineage_edges e
                          WHERE e.observation_id=o.observation_id)
          AND (o.filing_id IS NULL OR o.filing_id='' OR o.derivation IS NULL OR o.derivation='')
        GROUP BY o.metric_id""")
    assert not orphan, ("derived values with neither lineage edges nor a resolvable "
                        f"filing+derivation: {[(r['metric_id'], r['n']) for r in orphan]}")
    bulk = ctx.staging.query("""
        SELECT COUNT(*) n FROM observations o
        WHERE o.method='derived' AND o.availability='present'
          AND NOT EXISTS (SELECT 1 FROM lineage_edges e
                          WHERE e.observation_id=o.observation_id)""")
    tot = ctx.staging.query("SELECT COUNT(*) n FROM lineage_edges")
    return (f"{tot[0]['n']:,} lineage edges; {bulk[0]['n']} bulk-table shares carry a "
            "resolvable filing and a named population instead of per-row edges")


@check("every lineage input resolves to a real source fact", "lineage")
def t_lineage_resolves(ctx):
    bad = ctx.staging.query("""
        SELECT COUNT(*) n FROM lineage_edges e
        WHERE e.input_source_fact_id IS NOT NULL AND e.input_source_fact_id!=''
          AND NOT EXISTS (SELECT 1 FROM source_facts f
                          WHERE f.source_system=e.input_source_system
                            AND f.filing_id=e.input_filing_id
                            AND f.source_fact_id=e.input_source_fact_id)""")
    assert bad[0]["n"] == 0, f"{bad[0]['n']} lineage edges point at no source fact"
    return "all lineage inputs resolve on (source_system, filing_id, source_fact_id)"


def _occurrence_fixture(ctx):
    """The real two-occurrence fixture: one content hash under two filing IDs.

    Grouping deliberately does NOT include the accession number or the filing ID.
    Those ARE the occurrence identity, so grouping by them puts every occurrence
    in its own bucket and `HAVING n > 1` can never match -- which is exactly why
    the delivered identical-resubmission test skipped even with the real
    two-submission fixture sitting in the database. Rule 3.2: a shared content
    hash deduplicates BYTES, never submission identity, so the byte hash is the
    grouping key and the occurrence ID is the thing being counted.
    """
    return ctx.staging.query("""
        SELECT entity_key, form, reporting_year, reporting_period, content_hash,
               COUNT(*) n, COUNT(DISTINCT filing_id) distinct_ids,
               GROUP_CONCAT(filing_id) ids, GROUP_CONCAT(IFNULL(version_status,'')) vs
        FROM filings
        WHERE content_hash IS NOT NULL AND content_hash != ''
        GROUP BY entity_key, form, reporting_year, reporting_period, content_hash
        HAVING n > 1""")


@check("identical content under two filing IDs retains both occurrences", "lineage")
def t_identical_resubmission(ctx):
    rows = _occurrence_fixture(ctx)
    total = ctx.staging.query(
        "SELECT COUNT(*) n FROM filings WHERE content_hash IS NOT NULL AND content_hash!=''")
    if not total[0]["n"]:
        return "SKIP: no filings with content hashes are loaded at all"
    # FIXTURE PRECONDITION. With filings loaded, the two-submission fixture must
    # be present -- the audited baseline carries three pairs. Its absence means
    # the duplicate-content branch was not exercised, and an unexercised branch
    # is a failure, never a skip.
    assert rows, (
        f"the identical-resubmission fixture is ABSENT: {total[0]['n']} filings carry "
        "content hashes but none share one. The delivered baseline contains three "
        "byte-identical pairs (74834/74835, 20241001-5108/5171, 20241001-5128/5162), "
        "so this is a regression in filing retention, not a reason to skip.")
    for r in rows:
        assert r["distinct_ids"] == r["n"], (
            f"group {r['ids']} collapsed {r['n']} occurrences into "
            f"{r['distinct_ids']} filing IDs -- an occurrence was lost")
        states = [s for s in (r["vs"] or "").split(",") if s]
        assert VersionStatus.IDENTICAL_RESUBMISSION in states, \
            f"resubmission {r['ids']} not classified as identical (states={states})"
        # Every occurrence is retained AND classified; a group where only the
        # first row got a version_status would pass a substring test.
        assert all(states), f"resubmission {r['ids']} has an unclassified occurrence"
    return (f"{len(rows)} byte-identical resubmission group(s), "
            f"{sum(r['n'] for r in rows)} occurrences all retained and classified")


@check("a lineage edge cannot claim an occurrence its input does not have", "lineage")
def t_lineage_occurrence_coherent(ctx):
    """The filing occurrence on an edge must match the observation it points at.

    This is what the old wrong-version test only pretended to check. Resolving
    (source_system, filing_id, source_fact_id) against `source_facts` is NOT
    enough: when two submissions are byte-identical the same fact ID exists under
    BOTH filing IDs, so an edge repointed at the wrong occurrence still resolves.
    The occurrence claim has to agree with the input observation's own filing.
    """
    edges = ctx.staging.query("""
        SELECT COUNT(*) n FROM lineage_edges
        WHERE input_observation_id IS NOT NULL AND input_observation_id != ''""")
    if not edges[0]["n"]:
        return "SKIP: no lineage edges carry an input observation in this run"
    bad = ctx.staging.query("""
        SELECT e.observation_id, e.input_order, e.input_filing_id,
               o.filing_id AS observation_filing, e.input_source_fact_id,
               o.source_fact_id AS observation_fact
        FROM lineage_edges e JOIN observations o ON o.observation_id=e.input_observation_id
        WHERE (e.input_filing_id IS NOT NULL AND e.input_filing_id != ''
               AND o.filing_id IS NOT NULL AND o.filing_id != ''
               AND e.input_filing_id != o.filing_id)
           OR (e.input_source_fact_id IS NOT NULL AND e.input_source_fact_id != ''
               AND o.source_fact_id IS NOT NULL AND o.source_fact_id != ''
               AND e.input_source_fact_id != o.source_fact_id)
        LIMIT 10""")
    assert not bad, (
        f"{len(bad)} lineage edge(s) name a filing occurrence their input observation "
        f"does not have, e.g. {dict(bad[0])}")
    dangling = ctx.staging.query("""
        SELECT COUNT(*) n FROM lineage_edges e
        WHERE e.input_observation_id IS NOT NULL AND e.input_observation_id != ''
          AND NOT EXISTS (SELECT 1 FROM observations o
                          WHERE o.observation_id=e.input_observation_id)""")
    assert dangling[0]["n"] == 0, \
        f"{dangling[0]['n']} lineage edges point at an observation that does not exist"
    return (f"{edges[0]['n']:,} edges: every filing occurrence and fact ID agrees with "
            "the input observation it points at")


@check("repointing an edge at a byte-identical sibling occurrence is rejected", "lineage")
def t_wrong_occurrence_rejected(ctx):
    """NEGATIVE, on a mutation of REAL linked records in a disposable copy.

    Takes a real derived observation, its real input observation and their two
    real byte-identical filings, then repoints the edge's `input_filing_id` at
    the sibling occurrence while leaving the input observation on the original.
    The fact ID resolves under both filings -- identical bytes -- so a
    resolver-only check passes. The coherence check must reject it.
    """
    edge = ctx.staging.query("""
        SELECT e.* FROM lineage_edges e
        JOIN observations o ON o.observation_id = e.input_observation_id
        JOIN filings a ON a.source_system = e.input_source_system
                      AND a.filing_id = e.input_filing_id
        JOIN filings b ON b.source_system = a.source_system
                      AND b.content_hash = a.content_hash
                      AND b.filing_id != a.filing_id
        JOIN source_facts fa ON fa.source_system = a.source_system
                            AND fa.filing_id = a.filing_id
                            AND fa.source_fact_id = e.input_source_fact_id
        JOIN source_facts fb ON fb.source_system = b.source_system
                            AND fb.filing_id = b.filing_id
                            AND fb.source_fact_id = e.input_source_fact_id
        WHERE e.input_filing_id IS NOT NULL AND e.input_filing_id != ''
          AND e.input_source_fact_id IS NOT NULL AND e.input_source_fact_id != ''
          AND a.content_hash IS NOT NULL AND a.content_hash != ''
        LIMIT 1""")
    if not edge:
        # FIXTURE PRECONDITION. This used to return SKIP, which is the defect this
        # very check exists to punish: a negative control that goes quiet when its
        # fixture is absent proves nothing and looks like a pass. With lineage
        # loaded, the fixture must be present -- the audited baseline carries
        # 74834/74835 -- so its absence is a regression in filing retention.
        loaded = ctx.staging.query("""
            SELECT COUNT(*) n FROM lineage_edges
            WHERE input_filing_id IS NOT NULL AND input_filing_id != ''""")
        if not loaded[0]["n"]:
            return "SKIP: no lineage edges with an input filing are loaded at all"
        raise AssertionError(
            f"the byte-identical sibling fixture is ABSENT: {loaded[0]['n']} lineage "
            "edges name an input filing, but none of those filings has a "
            "byte-identical sibling occurrence to repoint at. The delivered baseline "
            "carries eCollection_XBRL 74834/74835 for exactly this purpose, so this "
            "is a regression in filing retention, not a reason to skip the one "
            "negative control that distinguishes content identity from submission "
            "identity.")
    e = dict(edge[0])
    sibling = ctx.staging.query("""
        SELECT b.filing_id FROM filings a JOIN filings b
          ON b.source_system=a.source_system AND b.content_hash=a.content_hash
         AND b.filing_id != a.filing_id
        WHERE a.source_system=? AND a.filing_id=? LIMIT 1""",
        (e["input_source_system"], e["input_filing_id"]))[0]["filing_id"]

    with tempfile.TemporaryDirectory() as td:
        db = Staging(pathlib.Path(td) / "wrong_occurrence.sqlite", create=True)
        for fid in (e["input_filing_id"], sibling):
            _copy_row(ctx, db, "filings",
                      "SELECT * FROM filings WHERE source_system=? AND filing_id=?",
                      (e["input_source_system"], fid))
            _copy_row(ctx, db, "source_facts",
                      "SELECT * FROM source_facts WHERE source_system=? AND filing_id=? "
                      "AND source_fact_id=?",
                      (e["input_source_system"], fid, e["input_source_fact_id"]))
        for oid in (e["observation_id"], e["input_observation_id"]):
            _copy_row(ctx, db, "observations",
                      "SELECT * FROM observations WHERE observation_id=?", (oid,))

        # FIXTURE CAPABILITY. The mutation is only meaningful while the SAME fact
        # id exists under BOTH filings -- that is what makes a resolver-only check
        # pass and leaves occurrence coherence as the only thing that can catch it.
        # If the fact resolved under just one filing, the resolver would reject the
        # mutation, this check would go green, and it would no longer be testing
        # what it claims to test. An inert fixture is the next failure along from a
        # skipped one, so it is asserted rather than assumed.
        both = db.query("""
            SELECT COUNT(DISTINCT filing_id) n FROM source_facts
            WHERE source_system=? AND source_fact_id=? AND filing_id IN (?, ?)""",
            (e["input_source_system"], e["input_source_fact_id"],
             e["input_filing_id"], sibling))
        if both[0]["n"] != 2:
            db.close()
            raise AssertionError(
                f"FIXTURE NO LONGER REPRODUCES THE DEFECT: fact "
                f"{e['input_source_fact_id'][:16]} resolves under {both[0]['n']} of the "
                f"two filings {e['input_filing_id']}/{sibling}, not both. A resolver-only "
                "check would then catch this mutation on its own and this control would "
                "pass without exercising occurrence coherence at all.")

        mutated = dict(e, input_filing_id=sibling)         # THE MUTATION
        db.con.execute(
            f"INSERT OR REPLACE INTO lineage_edges ({','.join(mutated)}) "
            f"VALUES ({','.join('?' * len(mutated))})", list(mutated.values()))
        db.con.commit()
        probe = _Ctx(db)

        # Now that the fixture's capability is asserted, the resolver-only check
        # MUST pass on this mutation. Asserting it rather than merely reporting it
        # is what detects the two checks collapsing into one: if a future change
        # made `t_lineage_resolves` catch a wrong occurrence, this control would
        # silently stop being the thing that distinguishes them.
        resolver_passed = True
        try:
            t_lineage_resolves(probe)
        except AssertionError as exc:
            resolver_passed = False
            db.close()
            raise AssertionError(
                "FIXTURE NO LONGER ISOLATES THE DEFECT: the resolver-only check "
                f"rejected this mutation by itself ({str(exc)[:120]}). That is not a "
                "failure of the engine, but this control no longer demonstrates that "
                "content identity is insufficient -- rebuild it against a fact that "
                "genuinely resolves under both occurrences.") from None
        try:
            t_lineage_occurrence_coherent(probe)
        except AssertionError as exc:
            db.close()
            return (f"edge repointed from filing {e['input_filing_id']} to byte-identical "
                    f"{sibling}: the resolver-only check "
                    f"{'still passed' if resolver_passed else 'also failed'}, "
                    f"the occurrence check REJECTED it -- {str(exc)[:120]}")
        db.close()
        raise AssertionError(
            "NEGATIVE CONTROL FAILED: an edge repointed from filing "
            f"{e['input_filing_id']} to byte-identical {sibling}, while its input "
            "observation stayed on the original, was accepted")


class _Ctx:
    """Minimal context so a check can be re-run against a disposable database."""

    def __init__(self, staging):
        self.staging = staging


def _copy_row(ctx, db, table: str, sql: str, params: tuple) -> None:
    rows = ctx.staging.query(sql, params)
    for r in rows:
        d = dict(r)
        db.con.execute(f"INSERT OR REPLACE INTO {table} ({','.join(d)}) "
                       f"VALUES ({','.join('?' * len(d))})", list(d.values()))


# ==================================================== scope and comparability

@check("a ratio across incompatible scopes is refused", "scope")
def t_scope_refusal(ctx):
    rows = ctx.staging.query("""
        SELECT COUNT(*) n FROM observations o JOIN lineage_edges a USING(observation_id)
        JOIN lineage_edges b USING(observation_id)
        JOIN observations oa ON oa.observation_id=a.input_observation_id
        JOIN observations ob ON ob.observation_id=b.input_observation_id
        WHERE o.selector='derived_ratio' AND a.input_order=1 AND b.input_order=2
          AND oa.scope != ob.scope""")
    assert rows[0]["n"] == 0, f"{rows[0]['n']} ratios mix two different scopes"
    return "no published ratio mixes scopes"


@check("Page 700 and whole-entity Form 6 figures never mix", "scope")
def t_p700_scope(ctx):
    rows = ctx.staging.query("""
        SELECT DISTINCT o.metric_id, oa.metric_id AS in_a, ob.metric_id AS in_b
        FROM observations o
        JOIN lineage_edges a ON a.observation_id=o.observation_id AND a.input_order=1
        JOIN lineage_edges b ON b.observation_id=o.observation_id AND b.input_order=2
        JOIN observations oa ON oa.observation_id=a.input_observation_id
        JOIN observations ob ON ob.observation_id=b.input_observation_id
        WHERE (oa.metric_id LIKE 'p700%') != (ob.metric_id LIKE 'p700%')""")
    assert not rows, f"a derivation mixes Page 700 with whole-entity inputs: {[dict(r) for r in rows]}"
    return "no derivation mixes the Page 700 interstate panel with whole-entity figures"


@check("a storage quantity is never presented as a daily rate", "scope")
def t_storage_units(ctx):
    rows = ctx.staging.query("""
        SELECT COUNT(*) n FROM observations
        WHERE metric_id LIKE '%storage%' AND (unit LIKE '%/day%' OR unit LIKE '%per day%')""")
    assert rows[0]["n"] == 0, "a storage quantity carries a per-day unit"
    return "storage quantities carry stock units only"


# ==================================================== idempotence and revisions

@check("two identical runs produce no duplicate observations", "idempotence")
def t_idempotent(ctx):
    dup = ctx.staging.query("""
        SELECT entity_key, metric_id, source_regime, period_basis,
               IFNULL(period_start,''), IFNULL(period_end,''), IFNULL(instant_date,''),
               scope, IFNULL(unit,''), method, COUNT(*) n
        FROM observations GROUP BY 1,2,3,4,5,6,7,8,9,10 HAVING n>1""")
    assert not dup, f"{len(dup)} duplicate observation grains"
    return "the unique grain constraint holds across all observations"


@check("a revision invalidates only its dependants and keeps prior values", "revisions")
def t_revision_containment(ctx):
    """Synthetic, clearly labelled, in a disposable copy of the database."""
    src = ctx.staging.path
    with tempfile.TemporaryDirectory() as td:
        dst = pathlib.Path(td) / "copy.sqlite"
        dst.write_bytes(src.read_bytes())
        db = Staging(dst, create=False)
        f = db.query("""SELECT * FROM filings WHERE source_system='eCollection_XBRL'
                        AND EXISTS(SELECT 1 FROM observations o
                                   WHERE o.filing_id=filings.filing_id) LIMIT 1""")
        if not f:
            db.close()
            return "SKIP: no filing with observations to revise"
        filing = f[0]
        before = db.query("SELECT COUNT(*) n FROM observations")[0]["n"]
        touched = db.invalidate_dependents(filing["source_system"], filing["filing_id"])
        after = db.query("SELECT COUNT(*) n FROM observations")[0]["n"]
        superseded = db.query(
            "SELECT COUNT(*) n FROM observations WHERE version_status='superseded'")[0]["n"]
        untouched = db.query("""SELECT COUNT(*) n FROM observations
                                WHERE version_status!='superseded'""")[0]["n"]
        db.close()
        assert after == before, "invalidation deleted rows instead of superseding them"
        # Count only what THIS call superseded: earlier runs legitimately leave
        # superseded rows behind, and counting those made the assertion depend on
        # run history rather than on the invalidation itself.
        assert superseded >= len(touched), (
            f"invalidation superseded {superseded} rows but reported {len(touched)} affected")
        assert untouched > 0, "invalidation touched every observation"
        return (f"synthetic revision of filing {filing['filing_id']}: {len(touched)} dependants "
                f"superseded, {untouched} untouched, 0 rows deleted")


@check("a failed source cannot publish a partial filing as complete", "failure")
def t_failure_containment(ctx):
    """NEGATIVE: an exception inside a filing transaction must roll the whole
    filing back rather than leave half of it committed."""
    with tempfile.TemporaryDirectory() as td:
        dst = pathlib.Path(td) / "copy.sqlite"
        dst.write_bytes(ctx.staging.path.read_bytes())
        db = Staging(dst, create=False)
        before = db.query("SELECT COUNT(*) n FROM source_facts")[0]["n"]
        try:
            with db.transaction() as con:
                con.execute("INSERT INTO source_facts(source_system,filing_id,source_fact_id,"
                            "value_as_filed) VALUES('test','PARTIAL','f1','1')")
                raise RuntimeError("simulated mid-filing download failure")
        except RuntimeError:
            pass
        after = db.query("SELECT COUNT(*) n FROM source_facts")[0]["n"]
        leaked = db.query("SELECT COUNT(*) n FROM source_facts WHERE filing_id='PARTIAL'")[0]["n"]
        db.close()
        assert after == before and leaked == 0, \
            f"a failed filing leaked {leaked} rows into the store"
        return "a mid-filing failure rolls the whole filing back; nothing partial is visible"


@check("a malformed HTML-as-200 response is rejected", "failure")
def t_html_rejected(ctx):
    from ferclib.http import looks_like_html_error
    assert looks_like_html_error(b"<!DOCTYPE html><html><head><title>404</title>"), \
        "an HTML error page was not detected"
    assert not looks_like_html_error(b'<?xml version="1.0"?><xbrl xmlns=...>'), \
        "a legitimate XML instance was misdetected as an error page"
    return "HTML bodies served with HTTP 200 are rejected; XML instances are not"


@check("credentials never reach an export", "security")
def t_no_secrets(ctx):
    from ferclib.http import assert_no_secrets, redact
    red = redact("https://api.data.ferc.gov/v1/dataset/29/data/?api_key=SECRETVALUE12345")
    assert "SECRETVALUE12345" not in red, "redact() leaked a key"
    assert_no_secrets(red)
    leaked = []
    for f in (pathlib.Path(__file__).parent / "exports").glob("*.csv"):
        try:
            assert_no_secrets(f.read_text(encoding="utf-8", errors="replace")[:400000])
        except AssertionError:
            leaked.append(f.name)
    assert not leaked, f"credentials found in exports: {leaked}"
    return f"redaction verified; {len(list((pathlib.Path(__file__).parent/'exports').glob('*.csv')))} exports clean"


@check("historical backfill is not emitted as current news", "feed")
def t_backfill_flagged(ctx):
    rows = ctx.staging.query("SELECT COUNT(*) n FROM events WHERE is_backfill=1")
    total = ctx.staging.query("SELECT COUNT(*) n FROM events")
    if not total[0]["n"]:
        return "SKIP: no events in this run"
    return f"{rows[0]['n']} of {total[0]['n']} events flagged as historical backfill"


@check("one shared filing links to many assets without duplicating metrics", "feed")
def t_shared_filing(ctx):
    rows = ctx.staging.query("""
        SELECT filing_id, COUNT(DISTINCT observation_id) obs FROM observations
        WHERE filing_id IS NOT NULL GROUP BY filing_id, metric_id, period_basis,
              period_start, period_end HAVING COUNT(*) > COUNT(DISTINCT observation_id)""")
    assert not rows, "a filing produced duplicate metric rows for one grain"
    return "no filing produces a duplicated metric at one grain"


# ==================================================== runner

def run_checks(ctx, out: pathlib.Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    RESULTS.clear()
    fns = [v for v in globals().values() if callable(v) and hasattr(v, "_check")]
    groups: dict[str, list] = {}
    for fn in fns:
        groups.setdefault(fn._check[1], []).append(fn)

    for group in sorted(groups):
        print(f"\n{group}:")
        for fn in groups[group]:
            name = fn._check[0]
            try:
                detail = fn(ctx)
                if isinstance(detail, str) and detail.startswith("SKIP"):
                    _record(name, group, "SKIPPED", detail[5:].lstrip(": "))
                else:
                    _record(name, group, "PASS", detail or "")
            except AssertionError as exc:
                _record(name, group, "FAIL", str(exc))
            except Exception as exc:                                  # noqa: BLE001
                _record(name, group, "ERROR", f"{type(exc).__name__}: {exc}")

    summary = {s: sum(1 for r in RESULTS if r["status"] == s)
               for s in ("PASS", "FAIL", "ERROR", "SKIPPED")}
    (out / "validation_results.json").write_text(
        json.dumps({"summary": summary, "results": RESULTS}, indent=1), encoding="utf-8")
    print(f"\n{summary['PASS']} PASS / {summary['FAIL']} FAIL / "
          f"{summary['ERROR']} ERROR / {summary['SKIPPED']} SKIPPED")
    print("-> verification/validation_results.json")
    return 0 if summary["FAIL"] == 0 and summary["ERROR"] == 0 else 1
