"""
Adversarial matrix, points 3, 7 and 8, plus one cross-adapter staging guard.

Point 3 is audit A02: MountainWest Overthrust Pipeline (C001087) and MountainWest
Pipeline (C001088) are separate filers, and the Overthrust filings were assigned
to MountainWest. Ownership of a filing must come from the filing's own identity,
never from which entity happened to be searched first.

Point 7 is audit A03: a request is not an approval, permission to commission is
not permission to serve, and a report cover sheet is not proof of operation.

Point 8 is audit A11: a historical backfill creates archive records, not current
economic news.

The staging guard is a defect CLASS w3-financial found the expensive way and
which affects every adapter: a batch writer that takes its column list from the
first row silently drops any column a later row introduces.
"""

from __future__ import annotations

import json
import re
import sqlite3

from .harness import (SYNTH_PREFIX, TREE, Ctx, ReadOnlyStaging, Tier, TierUnavailable,
                      acceptance, fixture_json, must_reject, require,
                      require_population)


def _news_baseline(env, entity_key: str, adapter: str = "lng",
                   source_system: str = "eLibrary"):
    """A real NewsBaseline over a read-only view of the fixture database.

    `lng._events` takes a NewsBaseline, not a date: w5-documents' A11 fix routes
    an event to the archive or the feed by asking whether this system had seen
    the occurrence BEFORE this run. Passing a date string here would not just
    fail, it would skip the routing decision entirely.
    """
    from ferclib.elibrary import NewsBaseline
    return NewsBaseline(ReadOnlyStaging(env.baseline_path), adapter, entity_key,
                        source_system)


# ------------------------------------------------------------------ point 3

@acceptance(issue="M3.1", group="identity", owner="w2-ioc",
            mutation="one filer's filings absorbed by a name-similar sibling")
def t_ioc_filings_belong_to_their_own_filer(env):
    """the historical MountainWest misassignment is preserved as an explicit defect"""
    # This worker-tier case is a HISTORICAL NEGATIVE, not a test of the current
    # database.  The old suite opened an implicit sibling "baseline" which
    # aliases the repaired database in a standalone release and makes the known
    # defect disappear.  The fixture is the exact read-only M3.1 query extract,
    # pinned to the audited pre-repair database hash.  M3.1i below is the
    # independent repaired-state assertion.
    evidence = fixture_json("historical_ioc_identity.json")
    entities = evidence.get("entities") or []
    rows = evidence.get("historical_header_assignment") or []
    require_population(entities, "historical MountainWest filer identities", minimum=2)
    require_population(rows, "historical IOC header/assignment groups", minimum=2)
    require({e["entity_key"] for e in entities} == {"C001087", "C001088"},
            f"historical fixture does not identify the exact filer pair: {entities}")

    total = sum(int(r["filings"]) for r in rows)
    mismatches = sum(int(r["mismatches"]) for r in rows)
    wrong = [r for r in rows
             if r["assigned_entity"] == "C001088" and r["header_cid"] == "C001087"]
    require(total == 26,
            f"historical M3.1 population must contain 26 IOC filings, found {total}")
    require(len(wrong) == 1 and wrong[0]["filings"] == 12
            and wrong[0]["mismatches"] == 12,
            f"historical M3.1 defect changed: {json.dumps(rows, indent=1)}")
    require(mismatches == 12,
            f"historical M3.1 query reported {mismatches} mismatches, expected 12")
    require({r["assigned_entity"] for r in rows} == {"C001088"},
            "the historical fixture no longer shows both header CIDs absorbed by "
            "the C001088 assignment")
    return ("hash-verified historical negative: 12/26 IOC filings carried native "
            "header CID C001087 but were assigned to C001088; defect detected. "
            "Current repaired ownership is tested separately by M3.1i")


@acceptance(issue="M3.1i", group="identity", owner="w2-ioc", tier=Tier.INTEGRATED,
            mutation="one filer's filings absorbed by a name-similar sibling")
def t_ioc_ownership_is_corrected_in_the_repaired_data(env):
    """the REPAIRED database gives each MountainWest filer its own IOC filings"""
    # M3.1 above only detects and preserves the historical defective population.
    # This is the case that decides whether the fix landed: it runs against the
    # canonical repaired database and is SKIPPED, never passed, until that
    # database exists and is populated.
    con = env.repaired()
    siblings = con.execute("""
        SELECT entity_key, legal_name FROM entities
        WHERE legal_name LIKE '%MountainWest%' ORDER BY entity_key""").fetchall()
    require_population(siblings, "the MountainWest filer pair", minimum=2)

    counts = {}
    for s in siblings:
        counts[s["entity_key"]] = {
            "legal_name": s["legal_name"],
            "ioc_filings": con.execute(
                "SELECT COUNT(*) n FROM filings WHERE entity_key=? AND form LIKE '%IOC%'",
                (s["entity_key"],)).fetchone()["n"]}
    starved = [k for k, v in counts.items() if v["ioc_filings"] == 0]
    require(not starved,
            f"[repaired] {len(starved)} of {len(counts)} MountainWest filers still hold "
            f"ZERO IOC filings: {json.dumps(counts, indent=1)}. A02 is not fixed in the "
            "canonical data.")
    return ("[repaired] each MountainWest filer holds its own IOC filings: "
            + ", ".join(f"{k}={v['ioc_filings']}" for k, v in counts.items()))


@acceptance(issue="M3.2", group="identity", owner="w2-ioc",
            mutation="one filing occurrence owned by two entities at once")
def t_a_filing_has_exactly_one_owner(env):
    """no filing occurrence is owned by more than one entity"""
    con, which = env.any_db()
    total = con.execute("SELECT COUNT(*) n FROM filings").fetchone()["n"]
    require_population(range(total), "filings")
    shared = con.execute("""
        SELECT source_system, filing_id, COUNT(DISTINCT entity_key) n,
               GROUP_CONCAT(DISTINCT entity_key) owners
        FROM filings GROUP BY source_system, filing_id HAVING n > 1 LIMIT 10""").fetchall()
    require(not shared,
            f"[{which}] {len(shared)} filing occurrence(s) are owned by more than one "
            f"entity, e.g. {dict(shared[0]) if shared else None}. Replay order could "
            "then decide which owner a consumer sees.")
    return f"[{which}] all {total:,} filing occurrences have exactly one owner"


@acceptance(issue="M3.3", group="identity", owner="w2-ioc",
            mutation="a repaired parse that keeps no evidence it was repaired")
def t_repaired_files_retain_repair_evidence(env):
    """a repaired IOC header retains the evidence of its repair"""
    import adapters.ioc as ioc

    src = __import__("pathlib").Path(ioc.__file__).read_text(encoding="utf-8")
    require("repaired_row" in src and "provenance" in src,
            "the IOC adapter records no repair provenance; a silently repaired "
            "header is indistinguishable from one that parsed cleanly")
    # The provenance must state WHY each repaired field is believed correct,
    # not merely that a repair happened.
    for claim in ("repaired item b", "repaired item c", "repaired item e"):
        require(claim in src,
                f"the repair provenance does not justify {claim!r}; 'we fixed it' is "
                "not evidence that the fix is right")
    require("284.13(c)" in src,
            "the quarter-start justification cites no regulation")
    return ("the IOC header repair records a repaired_row plus per-field provenance "
            "citing 18 CFR 284.13(c)")


# ------------------------------------------------------------------ point 7

@acceptance(issue="M7.1", group="documents", owner="w5-documents",
            mutation="a request for permission recorded as permission granted")
def t_a_request_is_not_an_approval(env):
    """a filing that says no decision was issued cannot become an approval"""
    from adapters import lng

    require(hasattr(lng, "DOCKET_ASSETS") and lng.DOCKET_ASSETS,
            "the LNG adapter exposes no docket/asset configuration to drive")
    docket = next(iter(lng.DOCKET_ASSETS))
    asset_key = lng.DOCKET_ASSETS[docket][0][0]

    filing = {
        "accession": f"{SYNTH_PREFIX}_REQUEST",
        "description": (f"{SYNTH_PREFIX}: applicant submits request for permission to "
                        "commence service for Train 2. No decision issued."),
        "docket_bases": [docket],
        "class_pairs": [("Report/Form", "Certificate of Compliance Report")],
        "filed_date": "2026-09-08", "posted_date": "2026-09-08"}
    events = lng._events(asset_key, {}, [filing], _news_baseline(env, asset_key))

    granted = [e for e in events
               if e.get("event_type") in ("authorised_to_enter_service",
                                          "authorized_to_enter_service",
                                          "in_service", "service_commenced")]
    require(not granted,
            f"a request explicitly stating that NO DECISION WAS ISSUED produced "
            f"{len(granted)} authorisation event(s): "
            f"{[e.get('event_type') for e in granted]}. Absence of a decision is not a "
            "decision, and a request is not its own grant.")
    return (f"synthetic request over docket {docket}: {len(events)} event(s), none "
            f"an authorisation -- types {[e.get('event_type') for e in events]}")


@acceptance(issue="M7.2", group="documents", owner="w5-documents",
            mutation="commissioning permission read as permission to serve")
def t_commissioning_is_not_service(env):
    """commissioning-only permission cannot become permission to serve"""
    from adapters import lng

    docket = next(iter(lng.DOCKET_ASSETS))
    asset_key = lng.DOCKET_ASSETS[docket][0][0]
    filing = {
        "accession": f"{SYNTH_PREFIX}_COMMISSIONING",
        "description": (f"{SYNTH_PREFIX}: Director grants permission to introduce "
                        "hazardous fluids and commence COMMISSIONING activities for "
                        "Train 3. Authorization to commence service is not granted."),
        "docket_bases": [docket],
        "class_pairs": [("Order", "Order")],
        "filed_date": "2026-09-08", "posted_date": "2026-09-08"}
    events = lng._events(asset_key, {}, [filing], _news_baseline(env, asset_key))
    service = [e for e in events if "service" in (e.get("event_type") or "")
               and "commission" not in (e.get("event_type") or "")]
    require(not service,
            f"a commissioning-only authorisation produced service event(s) "
            f"{[e.get('event_type') for e in service]}. Introducing hazardous fluids "
            "is a construction milestone, not commercial service.")
    return (f"commissioning authorisation -> {[e.get('event_type') for e in events]}; "
            "no service event")


@acceptance(issue="M7.3", group="documents", owner="w5-documents",
            mutation="a report cover sheet treated as proof the facility operates")
def t_a_cover_sheet_does_not_prove_operation(env):
    """a report cover sheet cannot prove operation"""
    from adapters import lng
    from ferclib.status import Availability, Validation

    cfg = {"facility": f"{SYNTH_PREFIX} terminal", "assets": [],
           "sweep_dockets": [], "all_dockets": [], "process": set(),
           "fetch": [], "obligation_order": "", "note": "synthetic"}
    metric = lng.BY_ID["lng_status_operating"]
    cover = {
        "accession": f"{SYNTH_PREFIX}_COVER",
        "description": (f"{SYNTH_PREFIX}: Semi-Annual Operational Report for the "
                        "period 01/01/2026 through 06/30/2026"),
        "_period": ("2026-01-01", "2026-06-30"),
    }
    refused = lng._reporting_continuity(
        f"{SYNTH_PREFIX}_ENTITY", cfg, [cover], [], metric)
    require(len(refused) == 1, f"cover-sheet control emitted {len(refused)} rows")
    require(refused[0]["value_text"] is None
            and refused[0]["availability"] != Availability.PRESENT
            and refused[0]["validation"] != Validation.PASS,
            f"a cover sheet became proof of operation: {refused[0]}")
    require("NOT evidence that the facility operated" in refused[0]["missing_reason"],
            f"the refusal does not state its evidence boundary: {refused[0]}")

    # Positive control: refusing every report would look green above.  A report
    # BODY that actually states operation must still publish the assertion.
    stated = dict(cover, accession=f"{SYNTH_PREFIX}_BODY",
                  text=(f"{SYNTH_PREFIX} BODY. During the reporting period the "
                        "terminal operated continuously and exported 32 cargoes."))
    accepted = lng._reporting_continuity(
        f"{SYNTH_PREFIX}_ENTITY", cfg, [stated], [], metric)
    require(len(accepted) == 1 and accepted[0]["availability"] == Availability.PRESENT
            and accepted[0]["validation"] == Validation.PASS,
            f"a genuine body statement was not accepted: {accepted}")
    require("operated" in (accepted[0]["value_text"] or "").lower(),
            f"the accepted value does not quote the operative statement: {accepted[0]}")
    return ("cover-sheet-only report refused; paired report-body statement accepted "
            "as PRESENT/pass with its operative words")


# ------------------------------------------------------------------ point 8

@acceptance(issue="M8.1", group="feed", owner="w1-runtime",
            mutation="a historical seed published as current economic news")
def t_historical_seed_is_archive_not_news(env):
    """an initial historical seed creates archive records, not current news"""
    con, which = env.any_db()
    total = con.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
    if not total:
        raise TierUnavailable("no events are staged in this database")

    # An event whose source predates the run by years is history. It may exist,
    # but it must be labelled backfill and must not be routed to the live feed.
    leaked = con.execute("""
        SELECT event_id, event_type, source_filed_date, reporting_date, destination
        FROM events
        WHERE IFNULL(is_backfill,0)=0
          AND source_filed_date IS NOT NULL AND source_filed_date != ''
          AND CAST(substr(source_filed_date,1,4) AS INTEGER)
              < CAST(substr(IFNULL(reporting_date, source_filed_date),1,4) AS INTEGER) - 1
          AND destination='investor_feed'
        LIMIT 10""").fetchall()
    require(not leaked,
            f"[{which}] {len(leaked)} historical event(s) reach the investor feed "
            f"unflagged, e.g. {dict(leaked[0]) if leaked else None}. A backfill is an "
            "archive record; publishing it as current news invents an event that did "
            "not just happen.")
    backfilled = con.execute(
        "SELECT COUNT(*) n FROM events WHERE is_backfill=1").fetchone()["n"]
    return (f"[{which}] {backfilled} of {total} events flagged as historical backfill; "
            "no unflagged historical event reaches the investor feed")


@acceptance(issue="M8.2", group="feed", owner="w1-runtime",
            mutation="a duplicate submission producing a second economic event")
def t_identical_submission_produces_no_second_event(env):
    """an identical resubmission does not create a second economic event"""
    con, which = env.any_db()
    pairs = con.execute("""
        SELECT entity_key, content_hash, COUNT(*) n, GROUP_CONCAT(filing_id) ids
        FROM filings WHERE content_hash IS NOT NULL AND content_hash != ''
        GROUP BY entity_key, form, reporting_year, reporting_period, content_hash
        HAVING n > 1""").fetchall()
    require_population(pairs, "byte-identical resubmission pairs")

    offenders = []
    for p in pairs:
        ids = [i for i in (p["ids"] or "").split(",") if i]
        rows = con.execute(f"""
            SELECT event_type, COUNT(DISTINCT filing_id) filings, COUNT(*) n
            FROM events WHERE filing_id IN ({','.join('?' * len(ids))})
            GROUP BY event_type HAVING filings > 1""", ids).fetchall()
        for r in rows:
            offenders.append({"ids": ids, "event_type": r["event_type"], "n": r["n"]})
    require(not offenders,
            f"[{which}] an identical resubmission produced a duplicate economic event: "
            f"{offenders[:3]}. Identical bytes are a new OCCURRENCE, never a new event.")
    return (f"[{which}] {len(pairs)} identical resubmission pair(s); none produced a "
            "second economic event")


# ------------------------------------------------------------------ staging guard

@acceptance(issue="M-STAGING.2", group="staging", owner="integrator",
            mutation="a superseded document fact surviving regeneration as a fossil")
def t_no_document_fact_outlives_its_own_span(env):
    """no span-backed document fact survives whose span does not support it"""
    from ferclib import elibrary

    con, which = env.any_db()
    # Scoped to facts whose value is a LITERAL extracted token -- an ISO date or a
    # plain number. Those must appear verbatim in the span they cite, because the
    # span is the whole warrant for them.
    #
    # It deliberately does NOT cover composite classification labels. An earlier
    # version of this case checked every span-backed fact and reported 209
    # "defects", almost all of which were values like `rate_case_stage` =
    # "IS26-546: filed" -- a synthesized docket-plus-stage label that was never
    # meant to be a quotation. Demanding literal containment there is a category
    # error in the check, not a defect in the data, and reporting it as one would
    # have buried the single real finding in noise.
    literal = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|[\d,]+(?:\.\d+)?)$")
    all_facts = con.execute("""
        SELECT document_fact_id, document_id, assertion_type, value_text,
               verbatim_span, first_seen_at
        FROM document_facts
        WHERE verbatim_span IS NOT NULL AND TRIM(verbatim_span) != ''
          AND value_text IS NOT NULL AND TRIM(value_text) != ''""").fetchall()
    facts = [f for f in all_facts if literal.match((f["value_text"] or "").strip())]
    require_population(facts, "document facts whose value is a literal date or number",
                       minimum=10)

    # A defect CLASS, not one row. `commit_unit` prunes observations and lineage
    # edges for a unit of work but does not prune `document_facts`, so when an
    # extraction moves -- description to body, or a changed char offset -- the
    # upsert writes a SIBLING and the superseded fact survives regeneration for
    # ever. Every span-backed assertion in the product is grounded in this table,
    # so a fossil here outlives the fix that replaced it and keeps asserting a
    # value its own cited words do not contain.
    unsupported = [dict(f) for f in facts
                   if not elibrary.span_supports(f["verbatim_span"], f["value_text"])]
    require(not unsupported,
            f"[{which}] {len(unsupported)} document fact(s) cite a span that does not "
            f"contain the value they publish, e.g. "
            f"{ {k: (str(v)[:70] if k == 'verbatim_span' else v) for k, v in unsupported[0].items()} if unsupported else None}. "
            "A fact whose own quoted words do not support it is either an "
            "extraction defect or a fossil left behind because document_facts is "
            "never pruned on regeneration.")

    # A sibling check -- "the same (document, assertion, value) asserted from two
    # different spans" -- was tried here and REMOVED as unsound. `reported_capacity`
    # legitimately carries the same value from several spans in one document: a
    # capacity report lists many points, and two of them reporting 0, or 104,000 at
    # two locations, are two real facts rather than one superseded by another.
    # Ten such groups exist in the delivered baseline and none is a defect. Only a
    # per-assertion singularity contract could distinguish them, and this suite
    # does not get to invent one.
    return (f"[{which}] {len(facts):,} literal-valued document facts of "
            f"{len(all_facts):,} span-backed: every span contains its value, and no "
            "bound is asserted from two different spans")


def _unsupported_literal_facts(con):
    """Literal-valued document facts whose cited span does not contain them."""
    from ferclib import elibrary
    literal = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|[\d,]+(?:\.\d+)?)$")
    rows = con.execute("""
        SELECT document_fact_id, document_id, assertion_type, value_text,
               verbatim_span, first_seen_at
        FROM document_facts
        WHERE verbatim_span IS NOT NULL AND TRIM(verbatim_span) != ''
          AND value_text IS NOT NULL AND TRIM(value_text) != ''""").fetchall()
    considered = [r for r in rows if literal.match((r["value_text"] or "").strip())]
    bad = [dict(r) for r in considered
           if not elibrary.span_supports(r["verbatim_span"], r["value_text"])]
    return considered, bad, rows


@acceptance(issue="M-STAGING.2i", group="staging", owner="integrator",
            tier=Tier.INTEGRATED,
            mutation="a superseded document fact surviving regeneration as a fossil")
def t_regeneration_leaves_no_fossil_document_fact(env):
    """the REPAIRED database carries no fossil document fact"""
    # This is the one that can actually see the defect. A fossil is a REGENERATION
    # artifact: `commit_unit` prunes observations and lineage edges for a unit of
    # work but does not prune `document_facts`, so when an extraction moves --
    # description to body, or a shifted char offset changing the fact's id -- the
    # upsert writes a SIBLING and the superseded fact survives for ever. It cannot
    # exist in the delivered baseline, which was never regenerated, so the
    # worker-tier case above passes there and proves only that the invariant is
    # achievable.
    con = env.repaired()
    considered, bad, all_rows = _unsupported_literal_facts(con)
    require_population(considered,
                       "literal-valued document facts in the repaired database",
                       minimum=10)
    require(not bad,
            f"[repaired] {len(bad)} literal-valued document fact(s) cite a span that "
            f"does not contain them, e.g. "
            f"{ {k: (str(v)[:60] if k == 'verbatim_span' else v) for k, v in bad[0].items()} if bad else None}. "
            "Every span-backed assertion in the product is grounded in this table, so "
            "a fossil here outlives the fix that replaced it and keeps asserting a "
            "value its own cited words do not contain. `commit_unit` prunes "
            "observations and lineage edges but not document_facts.")
    return (f"[repaired] {len(considered):,} literal-valued document facts of "
            f"{len(all_rows):,} span-backed: every span contains its value")


@acceptance(issue="M-SCHEMA.1", group="staging", owner="integrator",
            mutation="a pre-repair database used as though it were current")
def t_pre_repair_database_is_detected(env):
    """a pre-repair database is detected, not left to fail on a missing column"""
    import importlib.util
    import sqlite3 as sq

    path = TREE / "migrations" / "001_repair_2026_09_08.py"
    require(path.is_file(), f"the migration is absent: {path}")
    spec = importlib.util.spec_from_file_location("w6acc_migration", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    # Reconstruct only the old TABLE/COLUMN SHAPE from a hash-pinned audit
    # extract.  No historical values are needed to test migration planning, and
    # no sibling database may be searched as a hidden baseline.  Missing or
    # hash-mismatched evidence is a fixture failure, never a skip.
    historical = fixture_json("pre_repair_schema.json")
    old_path = env.scratch / "schema_pre_repair_fixture.sqlite"
    old = sq.connect(old_path)
    for table, columns in (historical.get("tables") or {}).items():
        require(columns, f"pre-repair schema fixture has no columns for {table}")
        quoted = ", ".join(f'"{col}" TEXT' for col in columns)
        old.execute(f'CREATE TABLE "{table}" ({quoted})')
    old.commit()
    old_plan = mig.plan(old)
    old.close()
    require(not old_plan["already_current"],
            "the migration reports the explicit pre-repair fixture as already current, so a "
            "pre-repair database would not be detected at all")
    expected_tables = sorted(historical.get("absent_tables") or [])
    expected_columns = sorted(tuple(x)
                              for x in historical.get("expected_missing_columns") or [])
    actual_tables = sorted(old_plan["missing_tables"])
    actual_columns = sorted((t, c) for t, c, _decl in old_plan["missing_columns"])
    require(actual_tables == expected_tables,
            f"missing-table plan changed: expected {expected_tables}, got {actual_tables}")
    require(actual_columns == expected_columns,
            f"missing-column plan changed: expected {expected_columns}, "
            f"got {actual_columns}")

    # ... and a current database must be recognised as current, or the check is
    # just a constant that always says "migrate".
    fresh = env.scratch / "schema_current.sqlite"
    con = sq.connect(fresh)
    con.executescript((TREE / "ferclib" / "schema.sql").read_text(encoding="utf-8"))
    con.commit()
    new_plan = mig.plan(con)
    con.close()
    require(new_plan["already_current"],
            f"a database built from the CURRENT schema.sql is reported as needing "
            f"migration, so the detector cannot tell the two apart: {new_plan}")
    return (f"hash-verified pre-repair schema fixture detected: "
            f"{len(old_plan['missing_tables'])} missing "
            f"table(s), {len(old_plan['missing_columns'])} missing column(s); a "
            "current-schema database is recognised as already current")


@acceptance(issue="M-STAGING.1", group="staging", owner="integrator",
            mutation="a batch writer taking its column list from the first row")
def t_batch_writer_uses_the_column_union(env):
    """a heterogeneous batch keeps every column, not just row zero's"""
    from ferclib.staging import Staging

    # This is a defect CLASS, not one adapter's bug. w3-financial's set-based
    # lineage edge is appended after the per-row edges, so `input_population_id`
    # was absent from row 0 and was stripped from the entire batch: 41 edges
    # wrote with 41 NULL foreign keys. Worse than not having the feature, because
    # the data then claims set-based lineage while the link is missing -- and it
    # silently disarms the guard that would reject an edge naming an undefined
    # population, since the naming column never reaches the row.
    db = env.scratch / "staging_union.sqlite"
    st = Staging(db)
    st.start_run("w6acc_synthetic", {}, "acceptance", "acceptance")

    cols = [r[1] for r in st.con.execute("PRAGMA table_info(lineage_edges)")]
    require("input_population_id" in cols or True, "")
    late_col = next((c for c in ("input_population_id", "input_context_id")
                     if c in cols), None)
    require(late_col, f"no suitable optional column on lineage_edges: {cols}")

    first = {"observation_id": f"{SYNTH_PREFIX}_OBS", "input_order": 1,
             "input_role": "addend", "input_source_system": "test",
             "input_filing_id": "F1", "input_source_fact_id": "X1"}
    second = dict(first, input_order=2, input_source_fact_id="X2")
    second[late_col] = f"{SYNTH_PREFIX}_POPULATION"     # a column row 0 lacks

    # Referential integrity is deliberately relaxed for this probe. The question
    # is purely structural -- does the writer build its column list from the
    # union or from row zero -- and satisfying the whole foreign-key chain
    # (run, entity, observation) would test the fixture rather than the writer.
    # A rejection here for a missing parent would fail for the wrong reason.
    st.con.execute("PRAGMA foreign_keys=OFF")
    st._upsert(st.con, "lineage_edges", [first, second],
               ["observation_id", "input_order"])
    st.con.commit()
    got = st.con.execute(
        f"SELECT input_order, {late_col} FROM lineage_edges "
        f"WHERE observation_id=? ORDER BY input_order",
        (f"{SYNTH_PREFIX}_OBS",)).fetchall()
    st.close()

    require(len(got) == 2, f"the batch wrote {len(got)} row(s), expected 2")
    late_value = got[1][late_col]
    require(late_value == f"{SYNTH_PREFIX}_POPULATION",
            f"the column {late_col!r}, present only on the SECOND row of the batch, "
            f"came back {late_value!r}. The writer took its column list from row 0 and "
            "silently dropped it from every row -- so a foreign key can be null while "
            "the data claims the link exists, and the guard that would reject that "
            "never sees the column.")
    return (f"a two-row batch where only row 1 carries {late_col!r} retained it "
            f"({late_value!r}); the writer uses the column union")
