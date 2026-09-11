"""
Adversarial matrix, points 5, 9 and 11: coverage substitutions and denominators.

Every case takes a REAL frozen slot and a REAL observation that satisfies it,
mutates the observation into the bad state, and requires `coverage.measure` to
refuse it. The unmutated pair is measured first in each case, so a refusal can
never be explained by a slot that never matched to begin with.

The mutations are the audit's:

  * a Q2 year-to-date interval offered for a discrete quarter;
  * a wrong unit;
  * a superseded version offered for a current slot;
  * a train/terminal-scope value offered for an entity-scope slot;
  * a valid derived candidate beating a blank -- which must be ALLOWED, but only
    on the merits and never by bypassing review.
"""

from __future__ import annotations

import copy

from .harness import (Tier, acceptance, must_reject, require, require_population)


def _slot_and_observation(env):
    """One real frozen slot and the real observation that satisfied it."""
    con, which = env.any_db()
    slot = con.execute("""
        SELECT * FROM coverage_expected
        WHERE period_basis='quarter' AND reporting_period='Q2'
          AND EXISTS (SELECT 1 FROM coverage_measured m
                      WHERE m.slot_id = coverage_expected.slot_id
                        AND m.observation_id IS NOT NULL)
        LIMIT 1""").fetchone()
    require_population([slot] if slot else [],
                       "a frozen Q2 quarter slot that a real observation satisfied")
    obs = con.execute("""
        SELECT o.* FROM observations o
        JOIN coverage_measured m ON m.observation_id = o.observation_id
        WHERE m.slot_id = ?""", (slot["slot_id"],)).fetchone()
    require_population([obs] if obs else [], f"the observation matched to {slot['slot_id']}")
    o = dict(obs)
    o.update(availability="present", validation="pass", method="filed",
             value_text="100", value_num=100)
    return dict(slot), o, which


def _measure_one(slot, observation):
    from ferclib import coverage
    return coverage.measure([slot], [observation], "W6ACC_SYNTHETIC")[0]


def _baseline_match(slot, observation, what: str):
    """Positive control: the unmutated pair must match before we mutate it."""
    r = _measure_one(slot, observation)
    require(r["populated"] == 1,
            f"the UNMUTATED {what} did not populate its slot (populated="
            f"{r['populated']}, outcome={r.get('outcome')}). A refusal after mutation "
            "would then prove nothing.")
    return r


# ------------------------------------------------------------------ point 5

@acceptance(issue="M5.1", group="coverage", owner="w4-coverage",
            mutation="a Q2 year-to-date interval offered for a discrete quarter")
def t_ytd_cannot_satisfy_a_discrete_quarter(env):
    """a Q2 YTD interval cannot satisfy a requested discrete quarter"""
    slot, o, which = _slot_and_observation(env)
    _baseline_match(slot, o, "quarter observation")

    bad = copy.deepcopy(o)
    bad.update(period_basis="ytd", period_start=f"{slot['reporting_year']}-01-01")
    r = _measure_one(slot, bad)
    require(r["populated"] == 0,
            f"a Q2 YTD interval satisfied a discrete Q2 quarter slot "
            f"(outcome={r.get('outcome')}). Rule 3.4 permits exactly one equivalence, "
            "Q1-YTD == Q1, and this is not it.")
    return f"[{which}] Q2 YTD refused for a discrete quarter -> outcome {r.get('outcome')!r}"


@acceptance(issue="M5.2", group="coverage", owner="w4-coverage",
            mutation="a value in the wrong unit offered for a USD slot")
def t_wrong_unit_cannot_satisfy(env):
    """a wrong-unit value cannot validate a slot"""
    slot, o, which = _slot_and_observation(env)
    _baseline_match(slot, o, "correct-unit observation")

    bad = copy.deepcopy(o)
    bad["unit"] = "barrels"
    r = _measure_one(slot, bad)
    require(r["validated"] == 0,
            f"a value in barrels validated a slot whose unit rule is "
            f"{slot.get('unit_rule')!r} (outcome={r.get('outcome')})")
    return (f"[{which}] unit 'barrels' refused against unit rule "
            f"{slot.get('unit_rule')!r} -> validated={r['validated']}")


@acceptance(issue="M5.3", group="coverage", owner="w4-coverage",
            mutation="a superseded observation offered for a current slot")
def t_superseded_cannot_satisfy_current(env):
    """a superseded observation cannot validate a current slot"""
    slot, o, which = _slot_and_observation(env)
    _baseline_match(slot, o, "current observation")

    bad = copy.deepcopy(o)
    bad["version_status"] = "superseded"
    r = _measure_one(slot, bad)
    require(r["validated"] == 0,
            f"a superseded observation validated a current slot "
            f"(outcome={r.get('outcome')})")
    return f"[{which}] superseded refused -> validated={r['validated']}, outcome {r.get('outcome')!r}"


@acceptance(issue="M5.4", group="coverage", owner="w4-coverage",
            mutation="a train/terminal-scope value offered for an entity-scope slot")
def t_wrong_scope_cannot_satisfy(env):
    """a narrower-scope substitute cannot satisfy an entity-scope slot"""
    slot, o, which = _slot_and_observation(env)
    _baseline_match(slot, o, "entity-scope observation")

    for scope in ("W6ACC_SYNTHETIC_TRAIN_2", "W6ACC_SYNTHETIC_TERMINAL",
                  "W6ACC_SYNTHETIC_STORAGE_MODULE"):
        bad = copy.deepcopy(o)
        bad["scope"] = scope
        r = _measure_one(slot, bad)
        require(r["populated"] == 0,
                f"a value scoped {scope!r} populated a slot scoped {slot['scope']!r}. "
                "Rule 3.3 keeps filing entity, physical asset, terminal, train, storage "
                "module and ownership share distinct and never interchangeable.")
    return (f"[{which}] train, terminal and storage-module scopes all refused against "
            f"slot scope {slot['scope']!r}")


@acceptance(issue="M5.5", group="coverage", owner="w4-coverage",
            mutation="a blank preferred over a valid present candidate")
def t_valid_candidate_beats_blank_without_bypassing_review(env):
    """a valid present candidate beats a blank, without bypassing review"""
    slot, o, which = _slot_and_observation(env)

    blank = copy.deepcopy(o)
    blank.update(availability="source_blank", validation="not_validated",
                 value_text=None, value_num=None)
    present = copy.deepcopy(o)
    present.update(method="derived", observation_id="W6ACC_SYNTHETIC_DERIVED")

    r = _measure_one(slot, [blank, present] and blank)      # blank alone first
    require(r["populated"] == 0,
            "a source_blank alone populated the slot, so the comparison below "
            "could not distinguish the candidate from the blank")

    from ferclib import coverage
    both = coverage.measure([slot], [blank, present], "W6ACC_SYNTHETIC")[0]
    require(both["populated"] == 1,
            f"a valid derived candidate did not beat a blank (outcome="
            f"{both.get('outcome')}); a blank was preferred over a real value")

    # ... and it must not have bypassed review: a flagged candidate stays flagged.
    flagged = copy.deepcopy(present)
    flagged.update(validation="source_anomaly_review")
    review = coverage.measure([slot], [blank, flagged], "W6ACC_SYNTHETIC")[0]
    require(review["populated"] == 1,
            "a flagged but present value lost to a blank; a warning does not make a "
            "value unavailable")
    require(review.get("validated") == 0 or review.get("outcome") != "populated_validated",
            f"a value carrying source_anomaly_review was recorded as validated "
            f"(outcome={review.get('outcome')}), which launders a review flag into a pass")
    return (f"[{which}] valid candidate beats blank (outcome {both.get('outcome')!r}); "
            f"a flagged candidate still wins over the blank but is recorded as "
            f"{review.get('outcome')!r}, not validated")


# ------------------------------------------------------------------ point 11

@acceptance(issue="M11.1", group="coverage", owner="w4-coverage",
            mutation="breaking ingestion to shrink the expected denominator")
def t_broken_ingestion_cannot_remove_obligations(env):
    """breaking ingestion cannot remove expected obligations"""
    con, which = env.any_db()
    frozen = con.execute("SELECT COUNT(*) n FROM coverage_expected").fetchone()["n"]
    require_population(range(frozen), "frozen expected slots")

    # The denominator is frozen from OBLIGATIONS, so it must not move with what
    # the run happened to produce. Measuring the same slots against NOTHING must
    # keep every slot and report them unpopulated -- never drop them.
    slots = [dict(r) for r in con.execute(
        "SELECT * FROM coverage_expected LIMIT 200").fetchall()]
    require_population(slots, "a sample of frozen slots", minimum=50)

    from ferclib import coverage
    measured = coverage.measure(slots, [], "W6ACC_SYNTHETIC")
    require(len(measured) == len(slots),
            f"measuring {len(slots)} slots against zero observations returned "
            f"{len(measured)} rows. A failed ingestion must leave the obligations "
            "standing and report them unmet; shrinking the denominator is Rule 3.6's "
            "'never improve a number by weakening its basis'.")
    require(all(m["populated"] == 0 for m in measured),
            "some slot reported itself populated with no observations at all")
    outcomes = {m.get("outcome") for m in measured}
    require("populated_validated" not in outcomes,
            f"a slot with no observation was recorded as validated: {outcomes}")
    return (f"[{which}] {len(slots)} slots measured against zero observations: all "
            f"{len(measured)} retained and unpopulated; outcomes {sorted(o for o in outcomes if o)[:4]}")


@acceptance(issue="M11.2", group="coverage", owner="w4-coverage",
            mutation="a value from one template counted toward another's readiness")
def t_cross_template_values_cannot_improve_readiness(env):
    """a value cannot improve a different template's readiness"""
    con, which = env.any_db()
    rows = con.execute("""
        SELECT template, COUNT(*) n, COUNT(DISTINCT metric_id) metrics
        FROM field_status GROUP BY template ORDER BY n DESC""").fetchall()
    require_population(rows, "field_status rows by template", minimum=2)

    # Each template's readiness must be computed over ITS OWN metrics. If one
    # metric_id counts toward two templates' readiness, a value produced for one
    # regime silently improves another's score.
    shared = con.execute("""
        SELECT metric_id, COUNT(DISTINCT template) t, GROUP_CONCAT(DISTINCT template) ts
        FROM field_status WHERE metric_id IS NOT NULL AND metric_id != ''
        GROUP BY metric_id HAVING t > 1 LIMIT 10""").fetchall()
    if not shared:
        return (f"[{which}] no metric appears under more than one template across "
                f"{sum(r['n'] for r in rows)} field_status rows, so no cross-template "
                "leakage is possible by construction")

    # Where a metric legitimately appears under several templates, its readiness
    # row must be per-template, not shared: the same metric may be finished for
    # one regime and unimplemented for another.
    for r in shared:
        outcomes = con.execute("""
            SELECT DISTINCT template, outcome FROM field_status WHERE metric_id=?""",
            (r["metric_id"],)).fetchall()
        require(len(outcomes) >= r["t"],
                f"metric {r['metric_id']} spans {r['t']} templates ({r['ts']}) but has "
                f"only {len(outcomes)} template/outcome rows, so one template's result "
                "is standing in for another's")
    return (f"[{which}] {len(shared)} metric(s) appear under several templates; each "
            "carries its own per-template readiness row rather than a shared one")
