"""
Retained positive controls.

The brief is explicit that a working control must not be deleted to simplify a
suite, so these are kept as first-class cases rather than folded into the
mutation checks. They assert the behaviours that were already correct and that a
repair could plausibly break:

  * XML parsing produces no typed-domain pseudo-facts;
  * typed dimensions are retained;
  * a valid dimensioned total is still published;
  * total throughput and forwardhaul stay distinct;
  * period bases stay distinct, with Q1-YTD == Q1 the only equivalence;
  * ownership is a label and never multiplies a filed figure;
  * a source-specific anomaly review stays out of the generic selectors.

Each one names what breaking it would look like, so a future failure reads as a
regression rather than a puzzle.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

from .harness import (TREE, FixtureIntegrityError, TierUnavailable, acceptance,
                      code_only, must_reject, require, require_population)


@acceptance(issue="A12.1d", group="positive_controls", owner="w6-acceptance",
            mutation="a missing, aliased or hash-mismatched required fixture")
def t_required_fixture_integrity_fails_closed(env):
    """required fixtures accept exact bytes and reject missing, aliased or changed bytes"""
    from acceptance import harness

    root = env.scratch / "fixture_integrity"
    root.mkdir(parents=True, exist_ok=True)
    fixture = root / "required.json"
    manifest = root / "MANIFEST.json"
    payload = b'{"population":["positive-control"]}\n'
    fixture.write_bytes(payload)
    manifest_text = json.dumps({
        "schema": "fixture-integrity-probe-v1",
        "files": {"required.json": {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }},
    }, indent=1) + "\n"
    manifest.write_text(manifest_text, encoding="utf-8")

    prior_dir, prior_manifest = harness.FIXTURE_DIR, harness.FIXTURE_MANIFEST
    harness.FIXTURE_DIR, harness.FIXTURE_MANIFEST = root, manifest
    try:
        require(harness.fixture_json("required.json") ==
                {"population": ["positive-control"]},
                "the exact manifested fixture was not accepted")

        manifest.unlink()
        no_manifest = must_reject(
            lambda: harness.fixture_json("required.json"),
            expect=FixtureIntegrityError,
            what="required fixture manifest removed")
        require("manifest is absent or aliased" in str(no_manifest),
                f"missing manifest failed for the wrong reason: {no_manifest}")
        manifest.write_text(manifest_text, encoding="utf-8")

        fixture.unlink()
        missing = must_reject(
            lambda: harness.fixture_json("required.json"),
            expect=FixtureIntegrityError,
            what="required fixture removed after manifest creation")
        require("absent or aliased" in str(missing),
                f"missing fixture failed for the wrong reason: {missing}")

        target = root / "aliased-target.json"
        target.write_bytes(payload)
        fixture.symlink_to(target.name)
        aliased = must_reject(
            lambda: harness.fixture_json("required.json"),
            expect=FixtureIntegrityError,
            what="required fixture replaced with a symlink")
        require("absent or aliased" in str(aliased),
                f"aliased fixture failed for the wrong reason: {aliased}")

        fixture.unlink()
        fixture.write_bytes(b'{"population":["changed"]}\n')
        changed = must_reject(
            lambda: harness.fixture_json("required.json"),
            expect=FixtureIntegrityError,
            what="required fixture bytes changed without updating the manifest")
        require("integrity failure" in str(changed),
                f"hash-mismatched fixture failed for the wrong reason: {changed}")

        traversed = must_reject(
            lambda: harness.fixture_json("../required.json"),
            expect=FixtureIntegrityError,
            what="fixture name escaping the release-relative directory")
        require("invalid fixture name" in str(traversed),
                f"path-escape fixture failed for the wrong reason: {traversed}")
    finally:
        harness.FIXTURE_DIR, harness.FIXTURE_MANIFEST = prior_dir, prior_manifest
    return ("exact manifested fixture accepted; removed manifest, removed fixture, "
            "symlinked fixture, byte-changed fixture and path escape all rejected by "
            "the intended integrity guard")


@acceptance(issue="P1", group="positive_controls", owner="integrator",
            mutation="a typed-domain member emitted as though it were a filed fact")
def t_no_typed_domain_pseudo_facts(env):
    """XML parsing produces no typed-domain pseudo-facts"""
    con, which = env.any_db()
    total = con.execute("SELECT COUNT(*) n FROM source_facts").fetchone()["n"]
    require_population(range(total), "source facts")

    # A typed-dimension MEMBER is part of a fact's address, not a fact. If the
    # parser emits one as a fact it acquires a value it never had.
    suspects = con.execute("""
        SELECT COUNT(*) n FROM source_facts
        WHERE (value_as_filed IS NULL OR TRIM(value_as_filed)='')
          AND source_fact_id IS NOT NULL AND source_fact_id != ''""").fetchone()["n"]
    ratio = suspects / total if total else 0
    require(ratio < 0.05,
            f"[{which}] {suspects:,} of {total:,} source facts ({ratio:.1%}) carry no "
            "filed value at all, which is what a typed-domain member emitted as a fact "
            "looks like")
    return (f"[{which}] {total:,} source facts, {suspects:,} valueless ({ratio:.2%}) -- "
            "no typed-domain pseudo-fact population")


@acceptance(issue="P2", group="positive_controls", owner="integrator",
            mutation="dropping typed dimensions so a dimensioned fact loses its address")
def t_typed_dimensions_retained(env):
    """typed dimensions are retained on the facts that carry them"""
    con, which = env.any_db()
    dims = con.execute("SELECT COUNT(*) n FROM source_dimensions").fetchone()["n"]
    require_population(range(dims), "source dimensions", minimum=1)
    typed = con.execute("""
        SELECT COUNT(*) n FROM source_dimensions WHERE dim_kind='typed'""").fetchone()["n"]
    explicit = con.execute("""
        SELECT COUNT(*) n FROM source_dimensions WHERE dim_kind='explicit'""").fetchone()["n"]
    require(typed > 0,
            f"[{which}] {dims:,} dimensions and NONE is typed. A typed dimension carries "
            "its member as a value rather than a QName, so losing the kind loses the "
            "distinction between a typed member and an explicit one.")
    with_value = con.execute("""
        SELECT COUNT(*) n FROM source_dimensions
        WHERE dim_kind='typed' AND typed_value IS NOT NULL AND TRIM(typed_value)!=''"""
        ).fetchone()["n"]
    require(with_value == typed,
            f"[{which}] {typed - with_value} of {typed} typed dimensions carry no "
            "typed_value, so the member that addresses the fact was dropped")
    contexts = con.execute("SELECT COUNT(*) n FROM source_contexts").fetchone()["n"]
    require(dims > contexts * 0.5,
            f"[{which}] only {dims:,} dimensions across {contexts:,} contexts; a "
            "dimensioned taxonomy should carry roughly one or more per context")
    return (f"[{which}] {dims:,} dimensions across {contexts:,} contexts: {typed:,} "
            f"typed (all carrying a typed_value) and {explicit:,} explicit")


@acceptance(issue="P3", group="positive_controls", owner="integrator",
            mutation="refusing every dimensioned total, not just the ambiguous ones")
def t_valid_dimensioned_totals_still_publish(env):
    """a valid dimensioned total is still published"""
    con, which = env.any_db()
    published = con.execute("""
        SELECT COUNT(*) n FROM observations
        WHERE availability='present' AND value_num IS NOT NULL""").fetchone()["n"]
    require_population(range(published), "published values", minimum=100)
    blocked = con.execute("""
        SELECT COUNT(*) n FROM observations
        WHERE availability='interpretation_blocked'""").fetchone()["n"]
    # Gating is correct where the evidence is unresolved, but gating EVERYTHING
    # is not a repair -- it is a refusal to answer.
    require(published > blocked,
            f"[{which}] {blocked:,} gated observations against {published:,} published; "
            "a repair that gates more than it publishes has stopped answering")
    return (f"[{which}] {published:,} values published alongside {blocked:,} correctly "
            "gated ones")


@acceptance(issue="P4", group="positive_controls", owner="integrator",
            mutation="collapsing total throughput and forwardhaul into one metric")
def t_throughput_and_forwardhaul_stay_distinct(env):
    """total throughput and forwardhaul remain distinct metrics"""
    from ferclib.registry import REGISTRY

    by_id = {m.id: m for m in REGISTRY}
    throughput = {i for i in by_id if "throughput" in i}
    forward = {i for i in throughput if "forwardhaul" in i or "forward_haul" in i}
    total = throughput - forward
    require_population(throughput, "throughput metrics")
    if not forward:
        raise TierUnavailable(
            "no forwardhaul metric is declared in the registry, so the distinction "
            "cannot be checked here")
    require_population(total, "whole-system throughput metrics distinct from forwardhaul")

    # Directional throughput metrics legitimately share scope='system': they are
    # all system-level figures, distinguished by DIRECTION, not by scope. An
    # earlier version of this check demanded distinct scopes and failed on
    # forwardhaul vs backhaul, which was the check being wrong about the domain.
    #
    # What must actually hold is that they are separate metrics with separate
    # meanings, so coverage matching -- which keys on metric_id -- can never let
    # one stand in for another.
    directional = {i for i in throughput
                   if any(w in i for w in ("forwardhaul", "forward_haul", "backhaul"))}
    whole = throughput - directional
    require_population(directional, "directional throughput metrics")
    require_population(whole, "whole-system throughput metrics")

    meanings = {}
    for i in sorted(throughput):
        m = by_id[i]
        text = (m.meaning or m.display or "").strip().lower()
        require(text, f"{i} declares no meaning, so nothing distinguishes it")
        require(text not in meanings,
                f"{i} and {meanings.get(text)} declare the SAME meaning; two throughput "
                "metrics that mean the same thing are one metric with two names, and "
                "either could be published for the other")
        meanings[text] = i

    # And a data-level check: where a whole-system total and a directional figure
    # exist for the same entity and period, the total must not simply BE the
    # directional figure -- that is what dropping the other direction looks like.
    from decimal import Decimal
    con, which = env.any_db()
    collapsed = []
    for w in sorted(whole):
        for d in sorted(directional):
            for r in con.execute("""
                    SELECT a.entity_key, a.reporting_year, a.reporting_period,
                           a.value_num AS total, b.value_num AS directional
                    FROM observations a JOIN observations b
                      ON a.entity_key=b.entity_key AND a.reporting_year=b.reporting_year
                     AND IFNULL(a.reporting_period,'')=IFNULL(b.reporting_period,'')
                     AND a.period_basis=b.period_basis
                    WHERE a.metric_id=? AND b.metric_id=?
                      AND a.availability='present' AND b.availability='present'
                      AND a.value_num IS NOT NULL AND b.value_num IS NOT NULL
                    LIMIT 200""", (w, d)).fetchall():
                if Decimal(str(r["total"])) < Decimal(str(r["directional"])):
                    collapsed.append({"entity": r["entity_key"],
                                      "year": r["reporting_year"], "total_metric": w,
                                      "directional_metric": d, "total": r["total"],
                                      "directional": r["directional"]})
    require(not collapsed,
            f"[{which}] {len(collapsed)} case(s) where a whole-system throughput total is "
            f"SMALLER than one direction of it, e.g. {collapsed[0] if collapsed else None}")
    return (f"{len(whole)} whole-system metric(s) {sorted(whole)} and {len(directional)} "
            f"directional metric(s) {sorted(directional)}: distinct ids, distinct "
            f"meanings, and no total smaller than one of its directions [{which}]")


@acceptance(issue="P5", group="positive_controls", owner="integrator",
            mutation="treating any YTD interval as its trailing quarter")
def t_period_bases_stay_distinct(env):
    """period bases stay distinct, with Q1-YTD == Q1 the only equivalence"""
    from ferclib import periods

    ok, why = periods.satisfies(periods.QUARTER, "2025-01-01", "2025-03-31", "",
                                periods.YTD, "2025-01-01", "2025-03-31", "")
    require(ok, f"the permitted Q1 equivalence was refused: {why}")
    for start, end, label in (("2025-04-01", "2025-06-30", "Q2"),
                              ("2025-07-01", "2025-09-30", "Q3"),
                              ("2025-10-01", "2025-12-31", "Q4")):
        bad, why2 = periods.satisfies(periods.QUARTER, start, end, "",
                                      periods.YTD, "2025-01-01", end, "")
        require(not bad,
                f"a {label} year-to-date interval satisfied the discrete {label} "
                f"quarter: {why2}")
    return ("Q1-YTD satisfies Q1; Q2, Q3 and Q4 year-to-date intervals are all "
            "refused for their discrete quarters")


@acceptance(issue="P6", group="positive_controls", owner="integrator",
            mutation="multiplying a filed entity figure by an ownership percentage")
def t_ownership_is_a_label_not_a_multiplier(env):
    """ownership is a label and never scales a filed figure"""
    import exporters

    src = pathlib.Path(exporters.__file__).read_text(encoding="utf-8")
    require("never" in src and "ownership" in src.lower(),
            "the export layer states no ownership policy")
    require("apportion" in src or "multiplied by a percentage" in src,
            "the export layer does not say that filed figures are never apportioned")

    con, which = env.any_db()
    own = con.execute("SELECT COUNT(*) n FROM ownership").fetchone()["n"]
    if not own:
        raise TierUnavailable("no ownership rows are staged in this database")
    # The mutation to catch is an OWNERSHIP share scaling a filed figure. Note
    # that "share of the stated denominator only" and "share of the stated billed
    # activity" are RATIO labels, not ownership -- an earlier version of this
    # check matched them on the word "share" and reported 814 false positives.
    # What must not exist is a published figure apportioned by an ownership
    # percentage.
    percents = [dict(r) for r in con.execute("""
        SELECT o.observation_id, o.metric_id, o.scope FROM observations o
        WHERE o.availability='present'
          AND (o.scope LIKE '%ownership share%' OR o.scope LIKE '%working interest%'
               OR o.scope LIKE '%net to parent%' OR o.scope LIKE '%apportion%')
        LIMIT 10""")]
    require(not percents,
            f"[{which}] {len(percents)} published observation(s) are scoped to an "
            f"ownership share, e.g. {percents[0] if percents else None}. Reported "
            "figures are the filing entity's own 100% system data and are never "
            "multiplied by a percentage.")
    pct_values = con.execute("""
        SELECT COUNT(*) n FROM ownership
        WHERE percent IS NOT NULL AND percent NOT IN ('', '0')""").fetchone()["n"] \
        if "percent" in {r[1] for r in con.execute("PRAGMA table_info(ownership)")} else 0
    return (f"[{which}] {own} ownership row(s) held as labels ({pct_values} carrying a "
            "percentage); no published observation is apportioned by one")


@acceptance(issue="P7", group="positive_controls", owner="integrator",
            mutation="a source-specific anomaly rule leaking into a generic selector")
def t_anomaly_reviews_stay_out_of_generic_selectors(env):
    """source-specific anomaly reviews stay out of the generic selectors"""
    for module in ("ferclib/selectors.py", "ferclib/xbrl_adapter.py",
                   "ferclib/coverage.py", "ferclib/periods.py"):
        path = TREE / module
        if not path.is_file():
            continue
        # Comments and docstrings are stripped first. These modules DOCUMENT the
        # Fayetteville anomaly at length to explain why no rule exists for it,
        # and a raw grep reads that explanation as the defect -- the same mistake
        # A21.7 made on its first pass.
        code = code_only(path.read_text(encoding="utf-8"))
        for literal in ("999999", "999,999", "C001012", "Fayetteville"):
            require(literal not in code,
                    f"{module} contains the source-specific literal {literal!r} in "
                    "EXECUTABLE code. A value- or filer-specific correction inside a "
                    "generic selector silently rewrites every other filer's data too.")
    con, which = env.any_db()
    preserved = con.execute("""
        SELECT COUNT(*) n FROM observations
        WHERE value_text='999999' AND validation='source_anomaly_review'""").fetchone()["n"]
    if not preserved:
        raise TierUnavailable(
            "the 999,999 anomaly sample is not staged in this database, so its "
            "preservation cannot be confirmed here")
    return (f"[{which}] no generic selector carries a value- or filer-specific rule; "
            f"{preserved} anomalous value(s) preserved as filed and flagged for review")
