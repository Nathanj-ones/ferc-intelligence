"""
Adversarial matrix, points 1 and 2: refresh, checkpoints, windows and failure.

These drive the REAL shared runner (`run.run_adapter`) and the REAL staging
writer against a synthetic adapter module, in a disposable database under
work/w6-acceptance/. The adapter is synthetic; the runner, the ledger and the
writer are not -- a mock writer would prove only that the mock behaves.

Ported in substance from the auditor's `evidence/scripts/negative_controls.py`,
with its `/mnt/data/ferc_audit` sandbox paths replaced by this tree's. The
assertions are the auditor's; the paths and the fixture plumbing are this
workstream's.
"""

from __future__ import annotations

import copy
import pathlib
import types

from .harness import Tier, acceptance, fixture_json, require


def _seed(env):
    """A schema-complete real row shape, converted to an isolated synthetic row."""
    fixture = fixture_json("runtime_seed_observation.json")
    o = copy.deepcopy(fixture["observation"])
    o.update(observation_id="W6ACC_SYNTHETIC_SEED", run_id="",
             entity_key="W6ACC_SYNTHETIC_ENTITY",
             metric_id="gas_operating_revenues", source_regime="Form 3Q Gas",
             source_system="W6ACC_SYNTHETIC", filing_id="W6ACC_SYNTHETIC_FILING",
             source_fact_id=None, concept_local="OperatingRevenues",
             origin="synthetic_acceptance", selector="synthetic_fixture",
             scope="filing entity, whole entity", unit="iso4217:USD",
             availability="present", validation="pass", method="filed",
             value_text="100", value_num=100, qa_flags="AUDIT SYNTHETIC")
    slot = {"slot_id": "W6ACC_SYNTHETIC_SLOT"}
    return slot, o, "hash_verified_fixture"


class _Fixture:
    """A disposable staging DB + ledger + context, holding one prior-year row."""

    def __init__(self, env, name: str, seed_obs: dict):
        from ferclib.ledger import Ledger
        from ferclib.staging import Staging

        self.dir = env.scratch / "runtime"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / f"{name}.sqlite"
        self.staging = Staging(self.db_path)
        self.staging.start_run("w6acc_synthetic", {}, "acceptance", "acceptance")
        old = copy.deepcopy(seed_obs)
        old.update(observation_id="W6ACC_SYNTHETIC_OLD_2024",
                   period_start="2024-04-01", period_end="2024-06-30",
                   reporting_year=2024, run_id=self.staging.run_id)
        self.staging.write_observations([old], [])
        self.ledger = Ledger(self.dir / f"{name}.json")
        # Exactly the surface `run.run_adapter` uses: args, failures, force,
        # ledger, log, staging. `failures` is the list the runner appends each
        # failed unit to and computes the run status FROM, so a stub without it
        # would silently change how status is decided -- which is the thing
        # under test.
        self.ctx = types.SimpleNamespace(
            staging=self.staging, ledger=self.ledger,
            # run_adapter samples this before and after retrieval so a contained
            # OfflineCacheMiss cannot be laundered into a shorter "successful"
            # unit.  The synthetic adapter performs no I/O, so the valid control
            # starts and remains empty.
            client=types.SimpleNamespace(cache_misses=[]),
            args=types.SimpleNamespace(year_from=2025, year_to=2025, force=False,
                                       budget=0, offline=True, as_of=""),
            force=False, offline=True, failures=[], interrupted=False,
            as_of=__import__("datetime").date(2026, 9, 7),
            today=lambda: __import__("datetime").date(2026, 9, 7),
            log=lambda *a, **k: None)

    def observation_ids(self):
        return [r[0] for r in self.staging.con.execute(
            "SELECT observation_id FROM observations")]

    def count(self):
        return self.staging.con.execute("SELECT COUNT(*) FROM observations").fetchone()[0]

    def close(self):
        self.staging.close()


def _synthetic_adapter(new_obs, calls: list):
    return types.SimpleNamespace(
        retrieve=lambda *a, **kw: (calls.append(kw) or []),
        freeze_expected=lambda *a: [],
        canonicalise=lambda *a: ([copy.deepcopy(new_obs)], []))


def _entity(seed_obs):
    return {"entity_key": seed_obs["entity_key"],
            "legal_name": "W6ACC SYNTHETIC ENTITY",
            "template": "interstate_gas", "assets": []}


# ------------------------------------------------------------------ point 1

@acceptance(issue="M1.1", group="runtime", owner="w1-runtime",
            mutation="a done checkpoint used to skip a source that has a new filing")
def t_done_checkpoint_does_not_hide_a_new_filing(env):
    """a completed checkpoint does not hide a new filing"""
    import run

    slot, seed, which = _seed(env)
    fx = _Fixture(env, "done_ledger_refresh", seed)
    new = copy.deepcopy(seed)
    new.update(observation_id="W6ACC_SYNTHETIC_NEW_2025",
               period_start="2025-04-01", period_end="2025-06-30", reporting_year=2025)
    calls: list = []
    entity = _entity(seed)
    task = f"gas_xbrl:{entity['entity_key']}"
    fx.ledger.set(task, "done")

    real = run.load_adapter
    run.load_adapter = lambda _: _synthetic_adapter(new, calls)
    try:
        before = len(calls)
        result = run.run_adapter(fx.ctx, "gas_xbrl", [entity])
    finally:
        run.load_adapter = real
    state = fx.ledger.state(task)
    fx.close()

    require(len(calls) - before >= 1,
            f"a task marked done caused the source NOT to be checked at all "
            f"({len(calls) - before} retrieval calls). A completed checkpoint is a "
            "record of what was done, not permission to stop looking: a new filing "
            "published after it would never be seen.")
    return (f"[{which}] a done checkpoint still checked the source "
            f"({len(calls) - before} retrieval call(s)); task state now {state!r}, "
            f"run status {result.get('status')!r}")


@acceptance(issue="M1.2", group="runtime", owner="w1-runtime",
            mutation="a repeated no-change refresh duplicating rows")
def t_no_change_refresh_is_idempotent(env):
    """a repeated no-change refresh produces no duplicates"""
    import run

    slot, seed, which = _seed(env)
    fx = _Fixture(env, "idempotent_refresh", seed)
    new = copy.deepcopy(seed)
    new.update(observation_id="W6ACC_SYNTHETIC_NEW_2025",
               period_start="2025-04-01", period_end="2025-06-30", reporting_year=2025)
    calls: list = []
    entity = _entity(seed)

    real = run.load_adapter
    run.load_adapter = lambda _: _synthetic_adapter(new, calls)
    try:
        run.run_adapter(fx.ctx, "gas_xbrl", [entity])
        after_first = fx.count()
        run.run_adapter(fx.ctx, "gas_xbrl", [entity])
        after_second = fx.count()
    finally:
        run.load_adapter = real
    dupes = fx.staging.con.execute("""
        SELECT observation_id, COUNT(*) n FROM observations
        GROUP BY observation_id HAVING n > 1""").fetchall()
    fx.close()

    require(after_second == after_first,
            f"a second identical refresh changed the row count from {after_first} to "
            f"{after_second}; an unchanged source must be a no-op")
    require(not dupes, f"the second run duplicated observation ids: {dupes[:3]}")
    return (f"[{which}] two identical refreshes: {after_first} rows then "
            f"{after_second}, no duplicate observation ids")


# ------------------------------------------------------------------ point 2

@acceptance(issue="M2.1", group="runtime", owner="w1-runtime",
            mutation="a 2025-only refresh deleting 2024 history")
def t_narrow_window_preserves_earlier_years(env):
    """a 2025-only refresh preserves 2024 and earlier"""
    import run

    slot, seed, which = _seed(env)
    fx = _Fixture(env, "narrow_window", seed)
    require("W6ACC_SYNTHETIC_OLD_2024" in fx.observation_ids(),
            "the 2024 fixture row was not seeded, so its survival proves nothing")
    new = copy.deepcopy(seed)
    new.update(observation_id="W6ACC_SYNTHETIC_NEW_2025",
               period_start="2025-04-01", period_end="2025-06-30", reporting_year=2025)
    calls: list = []
    entity = _entity(seed)

    real = run.load_adapter
    run.load_adapter = lambda _: _synthetic_adapter(new, calls)
    try:
        result = run.run_adapter(fx.ctx, "gas_xbrl", [entity])
    finally:
        run.load_adapter = real
    ids = fx.observation_ids()
    fx.close()

    require("W6ACC_SYNTHETIC_OLD_2024" in ids,
            f"a refresh windowed to 2025 deleted the 2024 observation. Retained ids: "
            f"{ids}. An out-of-window row is not stale data; it was never in scope.")
    return (f"[{which}] 2025-only refresh: 2024 row retained, {len(ids)} row(s) total, "
            f"status {result.get('status')!r}")


@acceptance(issue="M2.2", group="runtime", owner="w1-runtime",
            mutation="an interruption after pruning but before commit losing last-good rows")
def t_interruption_preserves_last_good(env):
    """an interruption before commit preserves the last-good state"""
    import run

    slot, seed, which = _seed(env)
    fx = _Fixture(env, "interrupted_commit", seed)
    before_ids = fx.observation_ids()
    require(before_ids, "no prior good row to preserve")
    new = copy.deepcopy(seed)
    new.update(observation_id="W6ACC_SYNTHETIC_NEW_2025",
               period_start="2025-04-01", period_end="2025-06-30", reporting_year=2025)
    calls: list = []
    entity = _entity(seed)

    real_upsert = fx.ctx.staging._upsert
    fired = {"n": 0}

    def interrupt_during_commit(con, table, rows, keys):
        # commit_unit has already pruned stale rows when it reaches the
        # observations upsert. This is the actual transaction boundary that the
        # former write_observations monkeypatch never touched.
        if table == "observations" and fired["n"] == 0:
            fired["n"] += 1
            raise KeyboardInterrupt("W6ACC_SYNTHETIC_INTERRUPT_AFTER_PRUNE")
        return real_upsert(con, table, rows, keys)

    fx.ctx.staging._upsert = interrupt_during_commit
    real = run.load_adapter
    run.load_adapter = lambda _: _synthetic_adapter(new, calls)
    raised = None
    try:
        run.run_adapter(fx.ctx, "gas_xbrl", [entity])
    except BaseException as exc:  # KeyboardInterrupt is the required outcome
        raised = exc
    finally:
        run.load_adapter = real
        fx.ctx.staging._upsert = real_upsert
    after_ids = fx.observation_ids()
    state = fx.ledger.state(f"gas_xbrl:{entity['entity_key']}")
    checkpoint = fx.staging.con.execute(
        "SELECT state FROM checkpoints WHERE adapter=? AND entity_cid=? "
        "ORDER BY updated_at DESC LIMIT 1", ("gas_xbrl", entity["entity_key"])
    ).fetchone()
    in_transaction = fx.staging.con.in_transaction
    fx.close()

    require(fired["n"] == 1, "the negative fixture never reached commit_unit's upsert")
    require(isinstance(raised, KeyboardInterrupt),
            f"the original KeyboardInterrupt was replaced by {type(raised).__name__}: "
            f"{raised}")
    require(after_ids == before_ids,
            f"an interruption between prune and commit changed last-good ids from "
            f"{before_ids} to {after_ids}")
    require(not in_transaction,
            "the same connection remains in_transaction after KeyboardInterrupt")
    require(state != "done",
            f"the interrupted task is marked {state!r}; a task that did not commit "
            "must not be checkpointed as done, or the resume will skip it")
    require(checkpoint and checkpoint["state"] == "failed",
            f"the database checkpoint is not terminal failed: {dict(checkpoint) if checkpoint else None}")
    return (f"[{which}] real commit_unit interrupted after prune: last-good ids preserved, "
            f"connection rolled back, task {state!r}, checkpoint {checkpoint['state']!r}")


@acceptance(issue="M2.3", group="runtime", owner="w1-runtime",
            mutation="a fetch failure reported as a successful run")
def t_fetch_failure_is_reported_honestly(env):
    """a fetch failure produces an honest failed status and keeps prior rows"""
    import run

    slot, seed, which = _seed(env)
    fx = _Fixture(env, "failed_fetch", seed)
    before = fx.count()
    entity = _entity(seed)

    def boom(*a, **k):
        raise RuntimeError("W6ACC_SYNTHETIC_FETCH_ERROR")

    mod = types.SimpleNamespace(retrieve=boom, freeze_expected=lambda *a: [],
                                canonicalise=lambda *a: ([], []))
    real = run.load_adapter
    run.load_adapter = lambda _: mod
    try:
        result = run.run_adapter(fx.ctx, "gas_xbrl", [entity])
    finally:
        run.load_adapter = real
    after = fx.count()
    state = fx.ledger.state(f"gas_xbrl:{entity['entity_key']}")
    fx.close()

    require(after == before,
            f"a fetch exception cost {before - after} previously good row(s)")
    require(result.get("status") != "ok",
            f"a run whose only entity raised on retrieval reported "
            f"{result.get('status')!r}")
    require(state != "done",
            f"a failed fetch left the task checkpointed as {state!r}")
    return (f"[{which}] fetch exception: {after} prior row(s) intact, status "
            f"{result.get('status')!r}, task {state!r} -- a retrieval failure is not "
            "non-applicability")


@acceptance(issue="M2.4", group="runtime", owner="w1-runtime",
            mutation="a resume that duplicates rows it already wrote")
def t_resume_recovers_without_duplication(env):
    """resume recovers after a failure without duplicating rows"""
    import run

    slot, seed, which = _seed(env)
    fx = _Fixture(env, "resume", seed)
    new = copy.deepcopy(seed)
    new.update(observation_id="W6ACC_SYNTHETIC_NEW_2025",
               period_start="2025-04-01", period_end="2025-06-30", reporting_year=2025)
    calls: list = []
    entity = _entity(seed)

    real_upsert = fx.ctx.staging._upsert
    fault = {"n": 0}

    def fail_once(con, table, rows, keys):
        if table == "observations" and fault["n"] == 0:
            fault["n"] += 1
            raise RuntimeError("W6ACC_SYNTHETIC_INTERRUPT")
        return real_upsert(con, table, rows, keys)

    fx.ctx.staging._upsert = fail_once
    real = run.load_adapter
    run.load_adapter = lambda _: _synthetic_adapter(new, calls)
    try:
        first = run.run_adapter(fx.ctx, "gas_xbrl", [entity])
        after_fail = fx.count()
        fx.ctx.staging._upsert = real_upsert
        second = run.run_adapter(fx.ctx, "gas_xbrl", [entity])
        after_resume = fx.count()
    finally:
        run.load_adapter = real
        fx.ctx.staging._upsert = real_upsert
    dupes = fx.staging.con.execute("""
        SELECT observation_id, COUNT(*) n FROM observations
        GROUP BY observation_id HAVING n > 1""").fetchall()
    ids = fx.observation_ids()
    fx.close()

    require(fault["n"] == 1, "the negative fixture never reached commit_unit's upsert")
    require(first.get("status") != "ok", "the interrupted first run reported ok")
    require(second.get("status") == "ok", f"the valid resume reported {second!r}")
    require("W6ACC_SYNTHETIC_OLD_2024" in ids,
            f"the 2024 row did not survive the failure and resume: {ids}")
    require(not dupes, f"the resume duplicated rows: {dupes[:3]}")
    require(after_resume >= after_fail,
            f"the resume lost rows: {after_fail} -> {after_resume}")
    return (f"[{which}] failure then resume: {after_fail} -> {after_resume} row(s), "
            f"no duplicates, 2024 preserved; statuses {first.get('status')!r} then "
            f"{second.get('status')!r}")
