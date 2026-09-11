#!/usr/bin/env python3
"""
The all-regime staging pipeline.

One command runs the configured operating universe across every supported
adapter, writing canonical observations, source lineage, document facts,
statuses and coverage into the shared staging database.

    python3 run.py universe                  # everything, every adapter
    python3 run.py universe --template liquids --limit 5
    python3 run.py discover                  # index only: what exists, no parsing
    python3 run.py backfill --entity C000654 --adapter gas_xbrl
    python3 run.py refresh                   # ALWAYS re-checks the source
    python3 run.py resume                    # ONLY mode that skips completed work
    python3 run.py plan --check              # validate the declared replay plan
    python3 run.py replay                    # execute the declared plan, cache-only
    python3 run.py validate                  # re-run checks over stored data
    python3 run.py coverage                  # measure against the frozen grid
    python3 run.py export                    # consumer-facing CSVs
    python3 run.py status                    # ledger, freshness and blockers

Filters: --entity CID (repeatable), --template, --adapter, --year-from, --year-to,
--limit, --force (ignore checkpoints), --budget N (request cap), --offline.

REFRESH IS NOT RESUME (audit A01 clause 1)
-------------------------------------------
`universe`, `backfill` and `refresh` always consult the source for every
in-scope entity. A completed checkpoint can never suppress that, so it can never
hide a filing or a revision that appeared after it was written. What a checkpoint
CAN do is make the commit a no-op: once the source has been consulted, a run
that finds exactly the occurrences the last successful run committed writes
nothing, which is what makes a no-change refresh idempotent.

`resume` is the one mode that skips work without consulting the source, and it
skips only where the recorded checkpoint identity -- entity, adapter, window,
code version, registry version, config digest, adapter digest -- still matches
exactly. Change any of them and the checkpoint no longer answers for this run.

EXIT STATUS IS HONEST (clause 5)
---------------------------------
    0  every in-scope unit succeeded
    1  the run completed but some units failed (status `partial`)
    2  the run could not complete: budget exhausted, missing required input,
       or an unhandled failure (status `failed`)
    130 interrupted

A run that stopped early is never recorded as `complete`, and a failed refresh
marks its units `stale` rather than restamping them as fresh.
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import uuid

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ferclib import coverage as cov                                  # noqa: E402
from ferclib import http as fhttp                                    # noqa: E402
from ferclib.applicability import (ApplicabilityBuilder, EvidenceKind,
                                   SourceHealth)                     # noqa: E402
from ferclib.ecollection import Index                                # noqa: E402
from ferclib.http import BudgetExhausted, Client, SourceCache        # noqa: E402
from ferclib.ledger import Ledger, identity_digest, input_digest     # noqa: E402
from ferclib.registry import BY_ADAPTER, BY_ID, REGISTRY, REGISTRY_VERSION, to_rows  # noqa: E402
from ferclib.staging import Staging, StagedCommitRejected            # noqa: E402
from ferclib.status import Availability                              # noqa: E402

CODE_VERSION = "0.37.4"


def _env_path(name: str, default: pathlib.Path) -> pathlib.Path:
    v = os.environ.get(name, "").strip()
    return pathlib.Path(v).expanduser() if v else default


#: Every path a run writes is overridable, so a worker can build a disposable
#: database and its exports without any possibility of touching the canonical
#: tree (repair contract rule 2). FERC_STAGING_DB is the one other workstreams
#: depend on; FERC_OUTPUT_DIR keeps a worker's exports and RUN_STATUS out of the
#: shared tree, and the ledger follows the database it describes.
STAGING_DB = _env_path("FERC_STAGING_DB", HERE / "staging" / "operating_assets.sqlite")
OUTPUT_DIR = _env_path("FERC_OUTPUT_DIR", HERE)
SOURCE_CACHE = _env_path("FERC_SOURCE_CACHE", HERE / "source_cache")
UNIVERSE = _env_path("FERC_UNIVERSE", HERE / "config" / "universe.csv")
ANNOTATIONS_DIR = _env_path("FERC_ANNOTATIONS_DIR", HERE / "config" / "annotations")
RUN_PLAN = _env_path("FERC_RUN_PLAN", HERE / "config" / "run_plan.json")
TAXONOMY_PINS = _env_path("FERC_TAXONOMY_PINS", HERE / "config" / "taxonomy_pins.json")
EXPORTS = OUTPUT_DIR / "exports"
EVIDENCE = OUTPUT_DIR / "evidence"
VERIFICATION = OUTPUT_DIR / "verification"
LEDGER = _env_path(
    "FERC_LEDGER",
    (STAGING_DB.parent / f"{STAGING_DB.stem}.task_ledger.json")
    if os.environ.get("FERC_STAGING_DB", "").strip() else HERE / "task_ledger.json")

ENTITY_ADAPTERS = ["gas_xbrl", "liquids_xbrl", "ioc", "capacity", "form549d",
                   "elibrary_docs", "lng"]
INSTRUMENT_ADAPTERS = ["oil_index"]
ADAPTERS = ENTITY_ADAPTERS + INSTRUMENT_ADAPTERS
_COVERAGE_SNAPSHOT_FORMS = frozenset({"Form 549B IOC", "Form 549B Capacity"})

# One recovered official-response body is deliberately carried in a FULL
# package only at its content-addressed cache path.  The capture-tree path is a
# byte-identical alias which would add 109 MB without adding source evidence.
# Input identity must therefore canonicalise that one path in exactly the same
# way as packaging does.  This declaration is deliberately exact: an alias is
# excluded only after the retained cache object, cache-index relation and both
# retained provenance records have all been verified.  No name/suffix rule can
# make another missing input invisible.
_REDUNDANT_INPUT_ALIAS = {
    "alias_path": (
        "inputs/official_ferc_recovery/individual_responses/20231229-5212/"
        "07_F19520DA-2360-C302-8619-8CB6CB100000.body"),
    "cache_path": (
        "objects/da/"
        "dae325d243bb7ecb3ac6db58c7163e197ffcc593b73e583330372f0fe03c400b"),
    "sha256": "dae325d243bb7ecb3ac6db58c7163e197ffcc593b73e583330372f0fe03c400b",
    "bytes": 109287608,
    "accession": "20231229-5212",
    "attachment_id": "F19520DA-2360-C302-8619-8CB6CB100000",
    "source_url": (
        "https://elibrary.ferc.gov/eLibraryWebAPI/api/File/DownloadP8File"
        "#body=3322ba94850c5522a19cdba79dcb6a52"),
    "results_path": (
        "implementation_logs/input_recovery/"
        "OFFICIAL_FERC_INDIVIDUAL_20231229-5212.json"),
    "results_sha256": "54acae8440cff3e9547e643b19e3b723f0113387f0ee114517123a482fefd43f",
    "results_bytes": 16735,
    "ledger_path": "implementation_logs/input_recovery/cache_import_runs.jsonl",
    "ledger_sha256": "fd8ac93a2e00c528d8d57f2e990e8cd46ed3b88fe2349d0c3a6a77a0eeb225d4",
    "ledger_bytes": 26862,
}

#: modes that always consult the source, versus the single resume mode
BUILD_COMMANDS = ("universe", "backfill", "refresh", "resume", "replay")


class Context:
    """Everything an adapter needs. Adapters never open the database or the
    network on their own -- they go through this, so the request budget, the
    shared index and the single writer are all enforced centrally."""

    def __init__(self, args):
        self.args = args
        self.force = bool(args.force)
        self.offline = bool(getattr(args, "offline", False))
        # Enforced, not merely recorded: set_offline() makes the shared client
        # refuse to open a socket and makes api_key() return a non-credential
        # sentinel, so cache-only replay needs no key at all (audit A01/A20).
        fhttp.set_offline(self.offline)
        # The as-of capture date. Date-sensitive retrieval must read this rather
        # than the wall clock, so a replay is pinned by a DECLARED input instead
        # of by a monkeypatch around date.today() (audit A20).
        self.as_of = _parse_as_of(getattr(args, "as_of", "") or
                                  os.environ.get("FERC_AS_OF", ""))
        self.as_of_iso = self.as_of.isoformat()
        self.output_dir = OUTPUT_DIR
        self.universe_path = UNIVERSE
        self.workdir = STAGING_DB.parent / "work"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.cache = SourceCache(SOURCE_CACHE)
        self.staging = Staging(STAGING_DB)
        self.client = Client(self.cache, min_interval=0.25,
                             budget=args.budget, logger=self._client_log)
        self.index = Index(self.client)
        self.applicability = ApplicabilityBuilder(self.client, logger=self._client_log)
        self.ledger = Ledger(LEDGER)
        # Reconcile a prior abrupt exit before a new run can consult resume
        # state. A unit_commits row was committed atomically with its data; its
        # absence proves the old generation stayed in place.
        self.recovery_events = self.staging.recover_incomplete_runs()
        for event in self.recovery_events:
            task = f"{event['adapter']}:{event['entity_cid']}"
            if event["committed"] and event.get("identity"):
                self.ledger.mark_done(
                    task, identity=event["identity"],
                    input_digest=event.get("input_digest") or "",
                    run_id=event["run_id"],
                    test_result="startup recovery verified the atomic unit-commit marker")
            else:
                self.ledger.set(task, "failed", blocker=event["detail"])
                self.ledger.mark_stale(task, event["detail"])
        self.applied_annotations = 0
        self.annotation_manifest: dict = {}
        #: every unit that failed, with its exact blocker -- the run status is
        #: computed from this, never assumed
        self.failures: list[dict] = []
        self.interrupted = False

    def today(self) -> dt.date:
        """The date a run should treat as 'now'. Adapters must call this instead
        of dt.date.today() so a declared as-of replay is reproducible."""
        return self.as_of

    def _client_log(self, level, msg):
        self.staging.log(level, msg, adapter="http")

    def log(self, level, msg, *, adapter="", entity_cid=""):
        print(f"[{dt.datetime.now():%H:%M:%S}] {level:5s} {msg}", flush=True)
        self.staging.log(level, msg, adapter=adapter, entity_cid=entity_cid)

    def close(self):
        self.staging.close()


def _parse_as_of(value: str) -> dt.date:
    if not value:
        return dt.date.today()
    try:
        return dt.date.fromisoformat(value.strip())
    except ValueError as exc:
        raise SystemExit(f"--as-of must be an ISO date (YYYY-MM-DD): {exc}") from None


# ---------------------------------------------------------------- universe

def load_universe(args) -> list[dict]:
    if not UNIVERSE.is_file():
        raise SystemExit(f"universe not frozen: {UNIVERSE} is missing. Run seed_universe.py first.")
    rows = list(csv.DictReader(UNIVERSE.open(newline="", encoding="utf-8")))
    if args.template:
        rows = [r for r in rows if r["template"] in args.template]
    if args.entity:
        rows = [r for r in rows if r["entity_key"] in args.entity]
    rows = [r for r in rows if r["template"] != "OUT_OF_TEMPLATE"]
    if args.limit:
        seen, out = set(), []
        for r in rows:
            if r["entity_key"] not in seen:
                seen.add(r["entity_key"])
            if len(seen) > args.limit:
                break
            out.append(r)
        rows = out
    return rows


def entities_from(rows: list[dict]) -> list[dict]:
    """One record per filing entity, carrying the assets it serves."""
    by_key: dict[str, dict] = {}
    for r in rows:
        e = by_key.setdefault(r["entity_key"], {
            "entity_key": r["entity_key"], "legal_name": r["entity_name"],
            "template": r["template"], "parent": r.get("parent", ""),
            "ticker": r.get("ticker", ""), "assets": []})
        e["assets"].append({"asset_id": r["asset_id"], "display_name": r["display_name"],
                            "template": r["template"], "group_key": r.get("group_key", ""),
                            "interest_display": r.get("interest_display", "")})
    return list(by_key.values())


# ---------------------------------------------------------------- adapters

def load_adapter(name: str):
    import importlib
    try:
        return importlib.import_module(f"adapters.{name}")
    except ModuleNotFoundError:
        return None


def _classify_adapter_failure(ctx, mod, exc: Exception) -> dict:
    """Classify a failed unit from evidence about the failure point.

    Only an HTTP failure after a request was actually attempted is a source
    condition.  Offline cache misses, absent local prerequisites and production
    assertion/commit failures are deliberately separate: calling all of them
    ``source`` made the stored blocker register contradict its own exact_error.
    An adapter-specific classifier may add a more precise taxonomy, but it must
    return a usable mapping; a classifier defect never masks the original
    exception.
    """
    if isinstance(exc, StagedCommitRejected):
        return {
            "classification": "staged_commit_rejected",
            "kind": "validation",
            "summary": "the staged unit failed pre-commit validation",
            "explicitly_not": "No source failure is established.",
            "remedy": "Correct the staged data or transformation and retry.",
        }

    classifier = getattr(mod, "classify_retrieval_failure", None)
    classifier_error = ""
    if callable(classifier):
        try:
            result = classifier(exc)
            if (isinstance(result, dict) and result.get("kind")
                    and result.get("classification")):
                return result
            classifier_error = "adapter classifier returned no kind/classification"
        except Exception as classify_exc:                         # noqa: BLE001
            classifier_error = (f"adapter classifier failed: {type(classify_exc).__name__}: "
                                f"{str(classify_exc)[:160]}")

    text = str(exc)
    low = text.lower()
    if isinstance(exc, fhttp.OfflineCacheMiss) or (
            bool(getattr(ctx, "offline", False))
            and ("offline_cache_misses=" in low or "source cache" in low)):
        return {
            "classification": "offline_replay_cache_miss",
            "kind": "configuration",
            "summary": "offline replay lacks a required captured response",
            "explicitly_not": "FERC was not contacted; this is not a FERC outage.",
            "remedy": "Capture the declared official response before freezing inputs.",
        }

    if isinstance(exc, fhttp.FetchError):
        url = str(getattr(exc, "url", ""))
        attempted = not (url.startswith("<") and url.endswith(">"))
        if not attempted or "not set in the environment" in low:
            return {
                "classification": "local_configuration_missing_credential",
                "kind": "configuration",
                "summary": "a local retrieval prerequisite failed before any request",
                "explicitly_not": "No FERC source condition was observed.",
                "remedy": "Supply the prerequisite for live retrieval or use captured replay.",
            }
        if getattr(exc, "status", None) == 403:
            return {
                "classification": "host_access_blocked_by_publisher",
                "kind": "access",
                "summary": "the publisher refused this host's request",
                "explicitly_not": "This is not evidence that the document is unpublished.",
                "remedy": "Use a permitted official retrieval route and preserve the response.",
            }
        return {
            "classification": "ferc_source_unavailable",
            "kind": "source",
            "summary": "an attempted official-source request failed",
            "explicitly_not": "",
            "remedy": "Retain the exact HTTP result and retry within policy.",
        }

    if isinstance(exc, (FileNotFoundError, PermissionError)):
        kind, classification = "configuration", "local_prerequisite_failure"
        summary = "a required local file or executable was unavailable"
    elif isinstance(exc, (AssertionError, KeyError, TypeError, ValueError, sqlite3.Error)):
        kind, classification = "validation", "transformation_or_validation_failure"
        summary = "the retrieved unit failed transformation or validation"
    else:
        kind, classification = "execution", "unclassified_execution_failure"
        summary = "the adapter failed outside a demonstrated source condition"
    return {
        "classification": classification,
        "kind": kind,
        "summary": summary,
        "explicitly_not": "No FERC source failure is established.",
        "remedy": "Inspect and correct the local execution failure before retrying.",
        "classifier_error": classifier_error,
    }
def _digest_file(path: pathlib.Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "absent"


_CONFIG_DIGEST: str | None = None


def config_digest() -> str:
    """One digest over everything that decides what a run produces.

    The registry module (not the CSV it generates), the frozen universe, the
    bundled annotation manifest and both versioned eLibrary routing seeds. If
    any of them changes, every stored checkpoint identity changes with it, so a
    completed checkpoint written under the old configuration stops being
    resumable automatically rather than by anyone remembering to pass --force
    (audit A01 clause 1).
    """
    global _CONFIG_DIGEST
    if _CONFIG_DIGEST is None:
        parts = [_digest_file(HERE / "ferclib" / "registry.py"),
                 _digest_file(UNIVERSE),
                 _digest_file(ANNOTATIONS_DIR / "MANIFEST.json"),
                 _digest_file(HERE / "inputs" / "official_ferc" / "elibrary" /
                              "rate_proceedings_v1.csv"),
                 _digest_file(HERE / "inputs" / "official_ferc" / "elibrary" /
                              "lng_facility_sources_v1.csv")]
        _CONFIG_DIGEST = hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
    return _CONFIG_DIGEST


def unit_identity(ctx, adapter: str, entity_key: str) -> dict:
    return {"adapter": adapter, "entity_key": entity_key,
            "year_from": int(ctx.args.year_from), "year_to": int(ctx.args.year_to),
            "code_version": CODE_VERSION, "registry_version": REGISTRY_VERSION,
            "config_digest": config_digest(),
            "adapter_digest": _digest_file(HERE / "adapters" / f"{adapter}.py")}


def _foreign_metric_ids(adapter: str) -> set[str]:
    """Metrics that belong to some OTHER adapter. A unit of work writing one of
    these would be writing rows its own prune scope cannot see, which is how
    orphans survive a rebuild."""
    mine = {m.id for m in BY_ADAPTER.get(adapter, [])}
    others: set[str] = set()
    for name, metrics in BY_ADAPTER.items():
        if name != adapter:
            others |= {m.id for m in metrics}
    return others - mine


def _prune_unwindowed_ok(ctx, task: str) -> bool:
    """May this run remove rows that carry no date at all?

    Only when its window covers at least as much as the window of the run that
    last committed this unit. A narrow refresh must not delete an undated
    snapshot a wider run produced, because it has no way of knowing whether it
    would have produced it too. When nothing was recorded before, the current
    run is the only authority and the answer is yes.
    """
    prev = ctx.ledger.stored_identity(task)
    if not prev or prev.get("year_from") is None:
        return True
    try:
        return (int(ctx.args.year_from) <= int(prev["year_from"])
                and int(ctx.args.year_to) >= int(prev["year_to"]))
    except (TypeError, ValueError):
        return True


def _eligible_adapter_entities(args, name: str, mod, entities: list[dict]) -> list[dict]:
    """Route entity adapters and global regulatory instruments separately.

    A global instrument must never be fanned out across filing entities merely
    because its metrics use the liquids template.  Conversely, an explicit
    carrier filter must not be silently reinterpreted as a request for the
    industry-wide index.
    """
    declared = getattr(mod, "GLOBAL_RUN_ENTITY", None)
    if declared is None:
        return [e for e in entities
                if any(m.templates and e["template"] in m.templates
                       for m in BY_ADAPTER.get(name, []))]
    entity = dict(declared)
    required = {"entity_key", "legal_name", "template"}
    missing = sorted(required - set(entity))
    if missing:
        raise ValueError(f"global adapter entity is missing {missing}")
    entity.setdefault("assets", [])
    if args.entity and entity["entity_key"] not in args.entity:
        return []
    if args.template and entity["template"] not in args.template:
        return []
    return [entity]


def _seed_global_adapter_entity(ctx, mod, entity: dict) -> None:
    """Persist only the legal identity of a synthetic regulatory instrument."""
    database_identity = getattr(mod, "INDUSTRY_ENTITY", None)
    row = dict(database_identity) if database_identity is not None else {
        "entity_key": entity["entity_key"],
        "cid": entity.get("cid"),
        "local_key": entity.get("local_key") or entity["entity_key"],
        "legal_name": entity["legal_name"],
        "parent": entity.get("parent"),
        "ticker": entity.get("ticker"),
        "jurisdiction": entity.get("jurisdiction"),
        "note": entity.get("note") or entity.get("notes"),
    }
    allowed = {"entity_key", "cid", "local_key", "legal_name", "parent",
               "ticker", "jurisdiction", "note"}
    if set(row) != allowed or row.get("entity_key") != entity["entity_key"]:
        raise ValueError("global adapter database identity has an invalid entity contract")
    ctx.staging.write_entities([row])


class _RetrievalIncomplete(RuntimeError):
    """A retrieval returned only a prefix of its required unit inputs."""

    def __init__(self, detail: str, *, subfailures: list[dict],
                 cache_misses: list[str]):
        self.subfailures = [dict(row) for row in subfailures]
        self.cache_misses = list(cache_misses)
        super().__init__(detail)


def _persist_rejected_subunit_status(ctx, exc: Exception) -> list[str]:
    """Re-emit child failure evidence after the unit data rollback.

    Retrieval-time child checkpoints participate in the outer transaction so
    they cannot accidentally commit raw data. Once rollback has completed, the
    child identities and exact errors are safe status evidence and are written
    back as explicit failures. Errors here are reported to the caller rather
    than masking the original retrieval failure.
    """
    if not isinstance(exc, _RetrievalIncomplete):
        return []
    errors = []
    for row in exc.subfailures:
        scope_key = str(row.get("scope_key") or "")
        if not scope_key:
            continue
        prior_state = str(row.get("state") or "unknown")
        exact = re.sub(r"\s+", " ", str(row.get("last_error") or "")).strip()
        if not exact:
            exact = f"child was still {prior_state} when the completeness gate ran"
        detail = f"outer unit rejected; child state={prior_state}; {exact}"
        try:
            ctx.staging.checkpoint(
                str(row.get("adapter") or ""), str(row.get("entity_cid") or ""),
                scope_key, "failed", error=detail[:300])
            ctx.staging.record_unit_status(
                str(row.get("adapter") or ""), str(row.get("entity_cid") or ""),
                scope_key, "failed", detail[:1000])
        except Exception as status_exc:                              # noqa: BLE001
            errors.append(
                f"{scope_key}: {type(status_exc).__name__}: {str(status_exc)[:160]}")
    return errors


def _retrieve_and_commit_unit(ctx, mod, name: str, entity: dict, *,
                              identity: dict, scope_key: str, task: str,
                              metric_ids: list[str], foreign: set[str],
                              mode: str, was_done: bool) -> dict:
    """Retrieve and publish one entity/adapter/window under one DB boundary.

    Several adapters persist occurrence-level filing, fact and document rows as
    they parse each response. Those rows are useful raw evidence, but they are
    still part of this unit: if a later required subitem is absent, publishing
    the early occurrences would mix a new partial filing archive with the old
    canonical observations. The outer transaction makes all nested staging
    writes provisional until the completeness gate and canonical swap succeed.

    Cache capture is intentionally outside SQLite and therefore may survive a
    rollback. It is immutable input evidence for a later retry, not published
    database state.
    """
    with ctx.staging.transaction():
        miss_at_start = len(ctx.client.cache_misses)
        filings = mod.retrieve(
            ctx, entity, year_from=ctx.args.year_from, year_to=ctx.args.year_to)
        digest = input_digest(filings)
        # This inventory is append-only across SUCCESSFUL units/runs, but it is
        # deliberately inside the unit transaction. A rejected attempt may
        # have returned only a prefix of the required source universe; retaining
        # that prefix as though it were the run's input inventory would be as
        # misleading as retaining its partial filing rows. The durable failed
        # status below identifies the missing subitem/cache object instead.
        ctx.staging.record_run_inputs(name, entity["entity_key"], filings)

        # Retrieval helpers contain failures to one filing/accession so
        # siblings can still be captured. That containment must not be
        # mistaken for permission to publish a shorter canonical unit: a
        # failed required subitem rolls back ALL database rows from this attempt
        # and makes the entity/run explicitly partial.
        subfailures = ctx.staging.failed_subunits(
            name, entity["entity_key"], scope_key)
        new_misses = ctx.client.cache_misses[miss_at_start:]
        if subfailures or new_misses:
            detail = []
            if subfailures:
                detail.append("subitems=" + ",".join(
                    "{}:{}:{}".format(
                        r["scope_key"], r["state"],
                        re.sub(r"\s+", " ", str(r.get("last_error") or ""))[:160])
                    for r in subfailures[:20]))
            if new_misses:
                detail.append("offline_cache_misses=" + ",".join(new_misses[:20]))
            raise _RetrievalIncomplete(
                "required retrieval/capture incomplete; canonical publication refused; "
                + "; ".join(detail),
                subfailures=subfailures, cache_misses=new_misses)

        # Idempotence is decided only AFTER the source has been consulted. A
        # completed checkpoint never suppresses retrieval. The raw occurrence
        # upserts and append-only input inventory, if any, commit before the
        # caller records the durable `unchanged` status.
        if (not ctx.force and mode in ("refresh", "resume")
                and ctx.ledger.unchanged_since_last_success(
                    task, digest, was_done=was_done)):
            return {"unchanged": True, "filings": filings, "digest": digest}

        expected = mod.freeze_expected(ctx, entity, filings, entity["assets"])
        ctx.staging.freeze_expected(expected)
        observations, edges = mod.canonicalise(ctx, entity, filings, expected)
        # Adapters may attach a document fact and/or feed events to an
        # observation. Both are unpacked here so the single writer stays the
        # only thing that touches the database.
        docfacts = [o.pop("_document_fact") for o in observations
                    if "_document_fact" in o]
        events = [e for o in observations for e in (o.pop("_events", []) or [])]
        # Set-based lineage: an aggregate over thousands of rows carries ONE
        # edge with input_role='population' pointing at a row that defines the
        # set exactly -- inclusion rule, exclusion rule, counts and a digest
        # over the member keys.
        populations = [p for o in observations
                       for p in (o.pop("_populations", []) or [])]
        # IOC buffers populations per entity because unrecognised observation
        # keys would otherwise enter the INSERT column list. Pull them into the
        # same unit transaction here.
        pending = getattr(mod, "pending_populations", None)
        if callable(pending):
            populations.extend(pending(entity["entity_key"]) or [])

        # STAGED SWAP. Retrieval-time rows above are visible only on this
        # connection until the outer transaction commits. commit_unit validates
        # and replaces this unit within a nested savepoint; an exception or
        # cancellation anywhere still rolls the outer transaction back.
        report = ctx.staging.commit_unit(
            adapter=name, entity_key=entity["entity_key"], metric_ids=metric_ids,
            year_from=identity["year_from"], year_to=identity["year_to"],
            observations=observations, edges=edges, qa_events=events,
            document_facts=docfacts, populations=populations,
            revisions=filings,
            prune_unwindowed=_prune_unwindowed_ok(ctx, task),
            foreign_metric_ids=foreign, scope_key=scope_key,
            input_digest=digest, identity=identity)
        return {
            "unchanged": False, "filings": filings, "digest": digest,
            "expected": expected, "observations": observations,
            "events": events, "report": report,
        }


def run_adapter(ctx, name: str, entities: list[dict]) -> dict:
    mod = load_adapter(name)
    if mod is None or not hasattr(mod, "retrieve"):
        ctx.log("warn", f"adapter {name} is not implemented; its metrics are counted as "
                        "unfinished work, not as a FERC data gap")
        ctx.staging.open_blocker(name, "unimplemented",
                                 f"adapter {name} is not implemented")
        return {"adapter": name, "entities": 0, "observations": 0, "expected": 0,
                "succeeded": 0, "failed": 0, "unchanged": 0, "resumed": 0,
                "status": "not_implemented"}

    eligible = _eligible_adapter_entities(ctx.args, name, mod, entities)
    if getattr(mod, "GLOBAL_RUN_ENTITY", None) is not None and eligible:
        _seed_global_adapter_entity(ctx, mod, eligible[0])
    mode = getattr(ctx.args, "command", "universe")
    ctx.log("info", f"=== {name}: {len(eligible)} eligible entities ({mode}) ===")

    metric_ids = [m.id for m in BY_ADAPTER.get(name, [])]
    foreign = _foreign_metric_ids(name)
    n_obs = n_exp = 0
    succeeded = failed = unchanged = resumed = 0
    fatal: Exception | None = None

    for i, entity in enumerate(eligible, 1):
        task = f"{name}:{entity['entity_key']}"
        identity = unit_identity(ctx, name, entity["entity_key"])
        scope_key = ctx.staging.scope_key_for(identity["year_from"], identity["year_to"],
                                              identity_digest(identity))
        ctx.ledger.add(task, owner=name, milestone="M3",
                       description=f"{name} for {entity['legal_name']}",
                       code=f"adapters/{name}.py")

        # RESUME is the only path that skips without consulting the source, and
        # only when the recorded conditions still hold exactly.
        if mode == "resume" and not ctx.force:
            ok, why = ctx.ledger.resumable(task, identity)
            if ok:
                resumed += 1
                continue
            if ctx.ledger.is_done(task):
                ctx.log("info", f"{entity['entity_key']}: not resumable ({why}); re-running")

        # Sampled BEFORE the task is marked running -- the idempotence check
        # below asks "did the LAST attempt succeed with these same inputs", and
        # once this unit is running the live state can no longer answer that.
        was_done = ctx.ledger.is_done(task)
        ctx.ledger.set(task, "running")
        ctx.staging.checkpoint(name, entity["entity_key"], scope_key, "in_progress")
        ctx.staging.record_unit_status(name, entity["entity_key"], scope_key,
                                       "in_progress", "entity/adapter/window started")
        try:
            unit_result = _retrieve_and_commit_unit(
                ctx, mod, name, entity, identity=identity, scope_key=scope_key,
                task=task, metric_ids=metric_ids, foreign=foreign,
                mode=mode, was_done=was_done)
            filings = unit_result["filings"]
            digest = unit_result["digest"]
            if unit_result["unchanged"]:
                unchanged += 1
                ctx.ledger.mark_done(task, identity=identity, input_digest=digest,
                                     run_id=ctx.staging.run_id or "",
                                     test_result="unchanged: source presented the same "
                                                 "filing occurrences as the last success")
                ctx.staging.checkpoint(name, entity["entity_key"], scope_key, "done")
                ctx.staging.record_unit_status(
                    name, entity["entity_key"], scope_key, "unchanged",
                    "source occurrence inventory matches the last successful input digest")
                continue

            expected = unit_result["expected"]
            observations = unit_result["observations"]
            events = unit_result["events"]
            report = unit_result["report"]

            # A revision moves its dependants with it, once, through an explicit
            # edge walk -- including derivations outside this run's window. The
            # walk now commits INSIDE commit_unit before its recovery marker, so
            # an abrupt process exit cannot make stale dependants look complete.
            invalidated = report["invalidated_dependents"]
            for detail in report["revision_invalidation"]:
                if detail["transitions"]:
                    ctx.log(
                        "info", f"revision {detail['revising_filing_id']} supersedes "
                        f"{detail['superseded_filing_id']}: {detail['transitions']} "
                        f"dependent observation(s) invalidated "
                        f"({len(detail['derived'])} via lineage)", adapter=name)

            n_obs += len(observations)
            n_exp += len(expected)
            succeeded += 1
            ctx.ledger.mark_done(
                task, identity=identity, input_digest=digest,
                run_id=ctx.staging.run_id or "",
                source_coverage=f"{len(filings)} filings, {len(expected)} slots",
                test_result=f"{report['committed']} observations, {report['edges']} edges, "
                            f"{report['pruned']} superseded-by-rebuild, "
                            f"{report['archived_prior_versions']} prior versions archived"
                            + (f", {report['populations']} populations"
                               if report["populations"] else "")
                            + (f", {len(events)} events" if events else "")
                            + (f", {invalidated} dependants invalidated" if invalidated else ""))
            ctx.staging.checkpoint(name, entity["entity_key"], scope_key, "done")
            ctx.staging.record_unit_status(
                name, entity["entity_key"], scope_key, "done",
                f"atomic commit: {report['committed']} observations, "
                f"{report['edges']} edges")
            ctx.log("info", f"[{i}/{len(eligible)}] {entity['entity_key']} "
                            f"{entity['legal_name'][:38]}: {len(filings)} filings, "
                            f"{len(expected)} slots, {report['committed']} observations, "
                            f"{report['pruned']} pruned, "
                            f"{report['retained_out_of_window']} out-of-window retained")
        except (KeyboardInterrupt, SystemExit) as original:
            # Each status sink is best-effort, but no status failure may replace
            # the cancellation that caused it. transaction() has already rolled
            # back and released the SQLite write lock at this point.
            status_errors = []
            for label, action in (
                    ("ledger failed", lambda: ctx.ledger.set(
                        task, "failed", blocker="interrupted before durable unit status")),
                    ("ledger stale", lambda: ctx.ledger.mark_stale(
                        task, "run interrupted; stored rows are last-good unless an atomic "
                              "unit-commit marker proves the commit completed")),
                    ("database checkpoint", lambda: ctx.staging.checkpoint(
                        name, entity["entity_key"], scope_key, "failed", error="interrupted")),
                    ("append-only unit status", lambda: ctx.staging.record_unit_status(
                        name, entity["entity_key"], scope_key, "interrupted",
                        "cancellation propagated; transaction rolled back"))):
                try:
                    action()
                except Exception as status_exc:                       # noqa: BLE001
                    status_errors.append(f"{label}: {type(status_exc).__name__}: {status_exc}")
            if status_errors:
                try:
                    original.add_note("; ".join(status_errors))
                except AttributeError:
                    pass
            raise
        except BudgetExhausted as exc:
            # A budget stop is a BOUNDED run, not a complete one. It aborts the
            # adapter rather than marching on producing an apparently complete
            # result from an unconsulted source.
            failed += 1
            ctx.failures.append({"adapter": name, "entity": entity["entity_key"],
                                 "kind": "budget_exhausted", "error": str(exc)[:300]})
            ctx.ledger.set(task, "failed", blocker=f"BudgetExhausted: {str(exc)[:160]}")
            ctx.ledger.mark_stale(task, "request budget exhausted before this unit ran")
            ctx.staging.checkpoint(name, entity["entity_key"], scope_key, "failed",
                                   error=str(exc)[:300])
            ctx.staging.record_unit_status(name, entity["entity_key"], scope_key,
                                           "failed", str(exc)[:1000])
            ctx.staging.open_blocker(name, "environment",
                                     f"{entity['entity_key']}: request budget exhausted",
                                     scope=entity["entity_key"], exact_error=str(exc)[:500])
            ctx.log("error", f"{entity['entity_key']} budget exhausted; stopping {name}")
            fatal = exc
            break
        except Exception as exc:                                        # noqa: BLE001
            import traceback
            failed += 1
            # Raw/expected/canonical rows have rolled back. Preserve the child
            # failure identities and exact errors as STATUS ONLY, in fresh
            # transactions, so diagnostics remain durable without leaking a
            # partial filing archive or incomplete input inventory.
            subunit_status_errors = _persist_rejected_subunit_status(ctx, exc)
            classification = _classify_adapter_failure(ctx, mod, exc)
            kind = classification["kind"]
            class_id = classification["classification"]
            ctx.failures.append({"adapter": name, "entity": entity["entity_key"],
                                 "kind": f"{kind}:{class_id}:{type(exc).__name__}",
                                 "classification": class_id,
                                 "error": str(exc)[:300]})
            ctx.ledger.set(task, "failed",
                           blocker=f"{type(exc).__name__}: {str(exc)[:160]}")
            # The stored rows are still the last GOOD rows -- commit_unit either
            # swapped completely or not at all -- so freshness, not correctness,
            # is what degraded. Say so instead of restamping the unit as current.
            ctx.ledger.mark_stale(task, f"{type(exc).__name__}: {str(exc)[:200]}")
            ctx.staging.checkpoint(name, entity["entity_key"], scope_key, "failed",
                                   error=str(exc)[:300])
            ctx.staging.record_unit_status(
                name, entity["entity_key"], scope_key, "failed",
                f"{type(exc).__name__}: {str(exc)[:900]}"
                + ("; child status persistence errors="
                   + ",".join(subunit_status_errors) if subunit_status_errors else ""))
            exact_error = f"[{class_id}] {type(exc).__name__}: {str(exc)[:360]}"
            if classification.get("explicitly_not"):
                exact_error += f" || {classification['explicitly_not']}"
            if classification.get("remedy"):
                exact_error += f" || remedy: {classification['remedy']}"
            if classification.get("classifier_error"):
                exact_error += f" || {classification['classifier_error']}"
            ctx.staging.open_blocker(
                name, kind,
                f"{entity['entity_key']}: {classification['summary']}",
                scope=entity["entity_key"], exact_error=exact_error[:1000],
                key=f"unit-failure:{class_id}")
            ctx.log("error", f"{entity['entity_key']} FAILED [{class_id}]: "
                            f"{type(exc).__name__}: {exc}")
            traceback.print_exc(limit=3)
            # failure containment: one entity's blocker must not stop the rest

    status = ("budget_exhausted" if isinstance(fatal, BudgetExhausted)
              else "failed" if failed and not succeeded
              else "partial" if failed
              else "ok")
    return {"adapter": name, "entities": len(eligible), "observations": n_obs,
            "expected": n_exp, "succeeded": succeeded, "failed": failed,
            "unchanged": unchanged, "resumed": resumed, "status": status}


def _invalidate_revisions(ctx, adapter: str, filings) -> int:
    """Walk the dependency closure for every filing this run saw superseded.

    `classify_version` records the superseded occurrence on the filing it
    revises; this turns that into the explicit invalidation of everything that
    depends on it. Idempotent: a second identical refresh finds every dependant
    already superseded and reports 0 transitions, so 'exactly once' is a
    property of the data rather than of how many times the run was started.
    """
    total = 0
    for f in filings or []:
        if not isinstance(f, dict):
            continue
        prior = f.get("supersedes_filing_id") or f.get("superseded_filing_id")
        if not prior or f.get("version_status") != "revised":
            continue
        detail = ctx.staging.invalidate_dependents_detailed(
            f.get("source_system") or "", str(prior))
        total += detail["transitions"]
        if detail["transitions"]:
            ctx.log("info", f"revision {f.get('filing_id')} supersedes {prior}: "
                            f"{detail['transitions']} dependent observation(s) invalidated "
                            f"({len(detail['derived'])} via lineage)", adapter=adapter)
    return total


# ---------------------------------------------------------------- commands

def cmd_universe(ctx, args) -> int:
    rows = load_universe(args)
    entities = entities_from(rows)
    scope = vars(args) | {"entities": len(entities), "as_of": ctx.as_of_iso,
                          "offline": ctx.offline, "config_digest": config_digest()}
    ctx.staging.start_run(args.command, scope, REGISTRY_VERSION, CODE_VERSION)
    ctx.log("info", f"universe: {len(rows)} asset rows, {len(entities)} filing entities "
                    f"({args.command}, as-of {ctx.as_of_iso}"
                    + (", cache-only" if ctx.offline else "") + ")")
    _seed_identity(ctx, rows)
    # A required input, loaded before any adapter runs. Absent or altered, the
    # run stops here rather than producing observations whose reviewed warnings
    # have quietly become passes (audit A09).
    _seed_annotations(ctx, args)

    results, status, note = [], "complete", ""
    try:
        for name in (args.adapter or ENTITY_ADAPTERS):
            results.append(run_adapter(ctx, name, entities))
    except KeyboardInterrupt:
        ctx.interrupted = True
        status, note = "interrupted", "run interrupted by signal"
        ctx.log("error", "interrupted: stored rows are last-good; freshness marked stale")

    if not ctx.interrupted:
        if any(r["status"] == "budget_exhausted" for r in results):
            status = "budget_exhausted"
            note = ("the request budget ran out before every in-scope unit was "
                    "consulted; this run is bounded, not complete")
        elif ctx.failures:
            ok = sum(r.get("succeeded", 0) for r in results)
            status = "partial" if ok else "failed"
            note = f"{len(ctx.failures)} unit(s) failed; {ok} succeeded"

    ctx.staging.finish_run(status, note)
    _write_run_status(ctx, results, status, note)
    for r in results:
        print(f"  {r['adapter']:14s} entities={r['entities']:4d} "
              f"observations={r.get('observations', 0):6d} "
              f"ok={r.get('succeeded', 0):4d} failed={r.get('failed', 0):3d} "
              f"unchanged={r.get('unchanged', 0):4d} {r['status']}")
    print(f"\nrun status: {status}" + (f" -- {note}" if note else ""))
    if ctx.failures:
        print(f"failed units ({len(ctx.failures)}):")
        for f in ctx.failures[:20]:
            print(f"  {f['adapter']:14s} {f['entity']:24s} {f['kind']}: {f['error'][:90]}")
    return _exit_code(status)


def _exit_code(status: str) -> int:
    return {"complete": 0, "partial": 1, "failed": 2, "budget_exhausted": 2,
            "interrupted": 130}.get(status, 2)


# ------------------------------------------------------------- annotations

class MissingRequiredInput(SystemExit):
    """A declared, required input is absent or does not match its manifest."""


def _load_annotation_bundle(strict: bool = True) -> tuple[list[dict], dict]:
    """Read the bundled reviewed annotations and prove they are intact.

    An annotation is a reviewed conclusion about one exact source observation.
    Rebuilding without it does not merely lose a note: the observation's
    `validation` reverts from source_anomaly_review to pass, so a suspected
    placeholder is silently promoted to apparently validated data. That is
    exactly the "improve a number by weakening its basis" the contract forbids,
    which is why the bundle is a REQUIRED input and a missing or altered one is
    fatal rather than a warning (audit A09).

    Three separate failures are distinguished, because they mean different
    things: the file is gone, the file no longer matches its recorded checksum,
    or a row has lost the review status or the rationale that made it an
    annotation at all.
    """
    manifest_path = ANNOTATIONS_DIR / "MANIFEST.json"
    if not manifest_path.is_file():
        if not strict:
            return [], {}
        raise MissingRequiredInput(
            f"required annotation manifest is missing: {manifest_path}\n"
            "  Reviewed source annotations are a required build input. Without them a\n"
            "  rebuild silently upgrades reviewed warnings to 'pass'.\n"
            "  Regenerate with:  python3 seed_annotations.py --export\n"
            "  or run with --without-annotations to record an explicitly degraded run.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows: list[dict] = []
    for decl in manifest.get("files", []):
        path = ANNOTATIONS_DIR / decl["path"]
        if not path.is_file():
            raise MissingRequiredInput(
                f"required annotation input declared in MANIFEST.json is missing: {path}")
        raw = path.read_bytes()
        got = hashlib.sha256(raw).hexdigest()
        if decl.get("sha256") and got != decl["sha256"]:
            raise MissingRequiredInput(
                f"annotation input {path.name} does not match its manifest checksum\n"
                f"  declared {decl['sha256']}\n  found    {got}\n"
                "  An annotation set is versioned evidence. Edit it through\n"
                "  seed_annotations.py --export so the manifest is regenerated with it.")
        body = json.loads(raw.decode("utf-8"))
        file_rows = body.get("annotations", [])
        if decl.get("row_count") is not None and len(file_rows) != decl["row_count"]:
            raise MissingRequiredInput(
                f"annotation input {path.name} declares {decl['row_count']} rows "
                f"but carries {len(file_rows)}")
        problems = _validate_annotation_rows(file_rows, path.name)
        if problems:
            raise MissingRequiredInput(
                f"annotation input {path.name} is structurally invalid:\n  "
                + "\n  ".join(problems[:10]))
        rows.extend(file_rows)
    return rows, manifest


#: an annotation without these is not an annotation
_ANNOTATION_REQUIRED = ("source_system", "entity_key", "filing_id", "source_fact_id",
                        "metric_id", "filed_text", "review_status", "rationale")
_ANNOTATION_STATUSES = {"reviewed", "reviewed_open", "reviewed_final", "reviewed_resolved"}


def _validate_annotation_rows(rows, label: str) -> list[str]:
    problems = []
    for i, a in enumerate(rows):
        missing = [k for k in _ANNOTATION_REQUIRED if not str(a.get(k) or "").strip()]
        if missing:
            problems.append(f"row {i} ({a.get('metric_id', '?')}) is missing or blank: "
                            f"{','.join(missing)}")
            continue
        if a["review_status"] not in _ANNOTATION_STATUSES:
            problems.append(f"row {i} review_status {a['review_status']!r} is not one of "
                            f"{sorted(_ANNOTATION_STATUSES)}")
    return problems


#: the columns reviewed_source_annotations actually has
_ANNOTATION_COLUMNS = ("source_system", "entity_key", "filing_id", "source_fact_id",
                       "metric_id", "filed_text", "review_status", "rationale",
                       "evidence_ref", "evidence_hash", "reviewer", "reviewed_at",
                       "applied_count")


def _seed_annotations(ctx, args) -> int:
    if getattr(args, "without_annotations", False):
        # Explicit, recorded, and never silent: the run is degraded and says so.
        ctx.staging.open_blocker(
            "annotations", "environment",
            "run executed with --without-annotations: reviewed source annotations were "
            "NOT loaded, so reviewed warnings are absent and any observation they "
            "qualify is unreviewed rather than validated",
            human_decision=True)
        ctx.failures.append({"adapter": "annotations", "entity": "-",
                             "kind": "required_input_skipped",
                             "error": "--without-annotations was passed"})
        ctx.log("warn", "reviewed source annotations SKIPPED by explicit flag; this run "
                        "is not releasable")
        return 0
    rows, manifest = _load_annotation_bundle(strict=True)
    ctx.annotation_manifest = manifest
    payload = [{k: a.get(k, "" if k != "applied_count" else 0)
                for k in _ANNOTATION_COLUMNS} for a in rows]
    n = ctx.staging.write_reviewed_annotations(payload)
    ctx.log("info", f"reviewed source annotations loaded: {n} rows, set version "
                    f"{manifest.get('annotation_set_version', '?')} "
                    f"({len({r['entity_key'] for r in rows})} entities)")
    return n


def cmd_discover(ctx, args) -> int:
    """Index only: what exists for each entity, with no parsing or selection."""
    rows = load_universe(args)
    entities = entities_from(rows)
    ctx.staging.start_run("discover", vars(args), REGISTRY_VERSION, CODE_VERSION)
    idx = ctx.index
    forms = idx.form_names()
    ctx.log("info", f"submission index: {sum(forms.values()):,} rows, {len(forms)} form names")
    out = []
    for e in entities:
        mine = idx.for_cid(e["entity_key"])
        by_form: dict[str, int] = {}
        for r in mine:
            by_form[str(r.get("formName"))] = by_form.get(str(r.get("formName")), 0) + 1
        years = sorted({int(r["year"]) for r in mine}) if mine else []
        out.append({"entity_key": e["entity_key"], "legal_name": e["legal_name"],
                    "template": e["template"], "filings": len(mine),
                    "forms": ";".join(f"{k}={v}" for k, v in sorted(by_form.items())),
                    "year_min": years[0] if years else "", "year_max": years[-1] if years else ""})
    cov.write_csv(EVIDENCE / "discovery_inventory.csv", out)
    have = sum(1 for r in out if r["filings"])
    ctx.log("info", f"{have}/{len(out)} entities have eCollection filings "
                    f"-> evidence/discovery_inventory.csv")
    ctx.staging.finish_run("complete")
    return 0



def _json_safe(obj):
    """Make a nested structure sortable and serialisable without losing None.

    A None key becomes the explicit string "(unset)" so a reader can see that
    the run did not establish that value, which is different from an empty
    string and different from the key being absent.
    """
    if isinstance(obj, dict):
        return {("(unset)" if k is None else str(k)): _json_safe(v)
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _persist_reference_state(ctx) -> dict:
    """Materialise the executable alternatives that previously shipped empty."""
    from adapters import lng

    applicability: dict[tuple, dict] = {}
    taxonomy_sources: dict[tuple, dict] = {}
    filings = ctx.staging.query(
        "SELECT DISTINCT form,taxonomy_version,schema_ref FROM filings "
        "WHERE taxonomy_version IS NOT NULL AND taxonomy_version!='' "
        "ORDER BY form,taxonomy_version")
    for filing in filings:
        form, version = filing["form"], filing["taxonomy_version"]
        concepts = sorted({m.concept for m in REGISTRY if m.concept and
                           any(regime == form for regime, _basis in m.regimes)})
        if not concepts:
            continue
        for row in ctx.applicability.rows(
                form, version, concepts, schema_ref=filing["schema_ref"] or ""):
            applicability[(row["form"], row["taxonomy_version"],
                           row["concept_local"])] = row
        for row in ctx.applicability.taxonomy_source_rows(form, version):
            taxonomy_sources[(row["form"], row["taxonomy_version"],
                              row["artefact"], row["url"])] = row

    docket_rows: dict[str, dict] = {}
    associations = ctx.staging.query(
        "SELECT DISTINCT docket FROM filing_dockets "
        "WHERE TRIM(docket)!='' ORDER BY docket")
    for row in associations:
        docket = row["docket"].strip()
        prefix = "".join(ch for ch in docket.split("-", 1)[0] if ch.isalpha()).upper()
        docket_rows[docket] = {
            "docket": docket, "prefix": prefix,
            "note": ("observed on a retained FERC filing occurrence; this dynamic "
                     "association is not a claim that the docket is the asset's "
                     "standing or exclusive legal authority")}
    # Filing occurrence history is not standing facility authority.  The only
    # asset/docket rows published here are the reviewed, versioned LNG routing
    # map; arbitrary observed dockets remain visible in ``dockets`` and
    # ``filing_dockets`` but cannot authorize cross-entity attribution.
    authority_rows = lng.reviewed_asset_docket_rows(UNIVERSE)
    for row in authority_rows:
        docket_rows.setdefault(row["docket"], {
            "docket": row["docket"],
            "prefix": "".join(ch for ch in row["docket"].split("-", 1)[0]
                              if ch.isalpha()).upper(),
            "note": "reviewed LNG facility authority from versioned routing evidence",
        })

    result = ctx.staging.replace_reference_state(
        applicability=list(applicability.values()),
        taxonomy_sources=list(taxonomy_sources.values()),
        source_manifest=ctx.cache.manifest_rows(),
        dockets=list(docket_rows.values()), asset_dockets=authority_rows)
    ctx.log("info", "reference snapshots: " + ", ".join(
        f"{name}={count:,}" for name, count in sorted(result.items())))
    return result


def _validated_taxonomy_pins(cache_index: dict) -> list[dict]:
    """Validate the pinned taxonomy routes against one exact cache index.

    Coverage is too late to discover that a required configuration snapshot
    was frozen against a different cache.  This helper is deliberately usable
    by the read-only plan preflight as well as the runtime coverage path.
    """
    if not TAXONOMY_PINS.is_file():
        raise MissingRequiredInput(f"required taxonomy pins are missing: {TAXONOMY_PINS}")
    body = json.loads(TAXONOMY_PINS.read_text(encoding="utf-8"))
    if body.get("schema") != "ferc_taxonomy_pins_v1":
        raise MissingRequiredInput("taxonomy pin file has an unsupported schema")
    declared_index = body.get("source_cache_index_sha256_at_freeze")
    actual_index = _sha256_file(SOURCE_CACHE / "index.json")
    if declared_index != actual_index:
        raise MissingRequiredInput(
            "taxonomy pins were not frozen against this source-cache index: "
            f"declared {declared_index}, current {actual_index}")
    pins = body.get("pins") or []
    if not pins:
        raise MissingRequiredInput("taxonomy pin file contains no pins")
    for pin in pins:
        cache_key = pin.get("cache_key", "")
        entry = cache_index.get(cache_key)  # exact frozen URL-key evidence
        evidence = pin.get("evidence_ref", "")
        if "#sha256=" not in evidence:
            raise MissingRequiredInput(f"taxonomy pin has no content hash: {pin}")
        url, content_hash = evidence.rsplit("#sha256=", 1)
        if not entry or entry.get("source_url") != url \
                or entry.get("content_hash") != content_hash:
            raise MissingRequiredInput(
                f"taxonomy pin evidence does not resolve in the frozen cache: "
                f"{pin.get('form')} {pin.get('reporting_year')}")
        object_path = SOURCE_CACHE / entry["cache_path"]
        if not object_path.is_file() or _sha256_file(object_path) != content_hash:
            raise MissingRequiredInput(f"taxonomy pin object is absent or corrupt: {object_path}")
    return pins


def _verify_taxonomy_pin_inputs() -> list[dict]:
    """Read-only plan-preflight validation for taxonomy/cache consistency."""
    index_path = SOURCE_CACHE / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MissingRequiredInput(
            f"taxonomy pin cache index cannot be read: {exc}") from exc
    if not isinstance(index, dict):
        raise MissingRequiredInput("taxonomy pin cache index is not an object")
    return _validated_taxonomy_pins(index)


def _load_taxonomy_pins(ctx) -> list[dict]:
    return _validated_taxonomy_pins(ctx.cache._index)


def _coverage_occurrences(ctx) -> list[dict]:
    from ferclib.obligations import FORM_ALIASES

    # Only the two genuine snapshot filings have a regulatory as-of date.
    # Form 549D's legacy snapshot_date value was Data.FERC's dataset-update
    # timestamp, while XBRL forms are keyed by their reporting interval.  A
    # generic timestamp-to-date conversion would silently invent semantics.
    rows = []
    for filing in ctx.staging.query(
            "SELECT source_system,filing_id,entity_key,form,reporting_year,"
            "reporting_period,snapshot_date FROM filings "
            "WHERE reporting_year IS NOT NULL ORDER BY source_system,filing_id"):
        form = (filing["form"] or "").strip()
        if form not in FORM_ALIASES:
            continue
        period = (filing["reporting_period"] or "").strip()
        if form in ("Form 2", "Form 2A", "Form 2-A", "Form 6",
                    "Form 549B Capacity"):
            period = "Q4"
        rows.append({
            "entity_key": filing["entity_key"], "form": form,
            "reporting_year": filing["reporting_year"], "reporting_period": period,
            "source_health": "ok", "detail": "retained parsed filing occurrence",
            "indexed_only": False,
            "as_of": ((filing["snapshot_date"] or "")
                      if form in _COVERAGE_SNAPSHOT_FORMS else ""),
            "occurrence_id": str(filing["filing_id"]),
            "source_system": filing["source_system"]})
    return rows


def _coverage_source_health(ctx, built_at: str) -> list[dict]:
    """Derive health labels without letting them create/delete obligations."""
    mapping = {
        "parse_failed": "parse_failed", "retrieval_failed": "retrieval_failed",
        "known_not_retrieved": "not_retrieved",
        "expected_not_located": "not_indexed"}
    rank = {"not_indexed": 1, "not_retrieved": 2,
            "retrieval_failed": 3, "parse_failed": 4}
    chosen: dict[tuple, dict] = {}
    for row in ctx.staging.query(
            "SELECT observation_id,entity_key,source_regime,reporting_year,"
            "reporting_period,instant_date,availability,missing_reason "
            "FROM observations WHERE availability IN "
            "('parse_failed','retrieval_failed','known_not_retrieved',"
            "'expected_not_located') ORDER BY observation_id"):
        form = (row["source_regime"] or "").strip()
        if form not in ("Form 2", "Form 2A", "Form 2-A", "Form 3Q Gas",
                        "Form 6", "Form 6Q", "Form 549B IOC",
                        "Form 549B Capacity", "Form 549D") or not row["reporting_year"]:
            continue
        period = (row["reporting_period"] or "").strip()
        if form in ("Form 2", "Form 2A", "Form 2-A", "Form 6",
                    "Form 549B Capacity"):
            period = "Q4"
        if period not in ("Q1", "Q2", "Q3", "Q4"):
            continue
        state = mapping[row["availability"]]
        key = (row["entity_key"], form, int(row["reporting_year"]), period)
        candidate = {"key": key, "state": state, "ids": [row["observation_id"]],
                     "reasons": [row["missing_reason"] or ""],
                     "as_of": ((row["instant_date"] or "")
                               if form in _COVERAGE_SNAPSHOT_FORMS else "")}
        prior = chosen.get(key)
        if prior is None:
            chosen[key] = candidate
        elif rank[state] > rank[prior["state"]]:
            chosen[key] = candidate
        elif state == prior["state"]:
            prior["ids"].append(row["observation_id"])
            prior["reasons"].append(row["missing_reason"] or "")
    identity_rows = [{"key": list(v["key"]), "state": v["state"],
                      "ids": sorted(v["ids"])} for v in chosen.values()]
    generation = "coverage-health-" + hashlib.sha256(json.dumps(
        sorted(identity_rows, key=lambda r: r["key"]), sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()[:24]
    out = []
    for value in chosen.values():
        entity, form, year, period = value["key"]
        material = f"{entity}|{form}|{year}|{period}|{value['state']}|{'|'.join(sorted(value['ids']))}"
        out.append({
            "record_id": "health-" + hashlib.sha256(material.encode()).hexdigest()[:24],
            "generation_id": generation, "entity_key": entity, "form": form,
            "reporting_year": year, "reporting_period": period,
            "source_health": value["state"],
            "detail": (f"observations={','.join(sorted(value['ids']))}; "
                       + "; ".join(sorted(set(value["reasons"])))[:1200]),
            "recorded_at": built_at, "as_of": value["as_of"]})
    return out


def _document_coverage_slots(ctx, coverage_run_id: str, built_at: str,
                             observations: list[dict], *, year_from: int,
                             year_to: int) -> tuple[list[dict], dict]:
    """Return stable route anchors and a separate occurrence-quality census.

    Event-driven eLibrary records do not have a defensible ``N per year``
    calendar.  A prior implementation replaced each independently frozen route
    anchor with one coverage slot per *observation*.  Removing a parser row then
    removed its own denominator row, and two legitimate same-day occurrences
    collided because provenance is not part of the business-value grain.

    The denominator is now exactly one pre-retrieval route anchor per eligible
    entity/metric.  Every persisted observation is still checked below, but in
    a separately labelled quality census: source occurrence, exact attachment,
    filing-entity/joint-docket association, current version, declared window
    and unit contract.  That census cannot be mistaken for an assertion that a
    fixed number of orders, inspections or amendments was legally due.
    """
    from adapters import elibrary_docs, lng
    from ferclib import elibrary
    from ferclib.obligations import load_universe as load_coverage_universe

    universe = load_coverage_universe(UNIVERSE)
    document_metrics = {
        m.id: m for m in REGISTRY
        if m.adapter != "oil_index"
        and any(regime == "eLibrary document" for regime, _basis in m.regimes)
    }
    module_for_template = {
        "interstate_gas": elibrary_docs,
        "gas_storage": elibrary_docs,
        "liquids": elibrary_docs,
        "intrastate_549d": elibrary_docs,
        "lng": lng,
    }
    anchors: list[dict] = []
    expected_pairs: set[tuple[str, str]] = set()
    for eligible in universe.entities:
        entity = {"entity_key": eligible.entity_key, "template": eligible.template,
                  "legal_name": eligible.entity_key}
        # One legal filer can cover several physical assets.  An arbitrary
        # first asset is not evidence that an entity-level route belongs to that
        # facility.  Keep the anchor unresolved unless attribution is unique.
        assets = ([{"asset_id": eligible.asset_ids[0],
                    "template": eligible.template}]
                  if len(eligible.asset_ids) == 1 else [])
        module = module_for_template[eligible.template]
        frozen = module.freeze_expected(ctx, entity, (), assets)
        anchors.extend(frozen)
        expected_pairs.update(
            (eligible.entity_key, metric.id) for metric in document_metrics.values()
            if eligible.template in metric.templates and metric.adapter == module.ADAPTER)

    anchors_by_pair: dict[tuple[str, str], dict] = {}
    for anchor in anchors:
        pair = (str(anchor.get("entity_key") or ""),
                str(anchor.get("metric_id") or ""))
        if pair in anchors_by_pair:
            raise ValueError(f"duplicate independent document anchor for {pair}")
        anchors_by_pair[pair] = dict(anchor)
    actual_pairs = set(anchors_by_pair)
    if actual_pairs != expected_pairs:
        missing = sorted(expected_pairs - actual_pairs)
        extra = sorted(actual_pairs - expected_pairs)
        raise ValueError(
            "document adapters did not freeze the complete eligible metric set: "
            f"missing={missing[:20]} extra={extra[:20]}")

    document_observations = [
        dict(row) for row in observations
        if row.get("source_regime") == "eLibrary document"
    ]
    by_pair: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for row in document_observations:
        pair = (str(row.get("entity_key") or ""), str(row.get("metric_id") or ""))
        if pair not in anchors_by_pair:
            raise ValueError(
                f"document observation {row.get('observation_id')!r} has no independent "
                f"eligible anchor for {pair}")
        by_pair[pair].append(row)

    known_filings = {
        (str(row["source_system"]), str(row["filing_id"])): {
            "entity_key": str(row["entity_key"] or ""),
            "is_canonical": bool(row["is_canonical"]),
        }
        for row in ctx.staging.query(
            "SELECT source_system,filing_id,entity_key,is_canonical FROM filings")
    }
    known_documents = {
        str(row["document_id"]): {
            "source_system": str(row["source_system"] or ""),
            "filing_id": str(row["filing_id"] or ""),
            "availability": str(row["availability"] or ""),
        }
        for row in ctx.staging.query(
            "SELECT document_id,source_system,filing_id,availability FROM documents")
    }
    dockets_by_filing: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for row in ctx.staging.query(
            "SELECT source_system,filing_id,docket FROM filing_dockets "
            "ORDER BY source_system,filing_id,docket"):
        dockets_by_filing[(str(row["source_system"]), str(row["filing_id"]))].append(
            str(row["docket"] or ""))
    entity_assets: dict[str, set[str]] = collections.defaultdict(set)
    for row in ctx.staging.query(
            "SELECT entity_key,asset_id FROM asset_entity_map ORDER BY entity_key,asset_id"):
        entity_assets[str(row["entity_key"])].add(str(row["asset_id"]))
    asset_groups = {
        str(row["asset_id"]): str(row["group_key"] or "")
        for row in ctx.staging.query(
            "SELECT asset_id,group_key FROM assets ORDER BY asset_id")
    }
    assets_by_docket: dict[str, set[str]] = collections.defaultdict(set)
    for row in ctx.staging.query(
            "SELECT asset_id,docket FROM asset_dockets "
            f"WHERE role='{lng.AUTHORITY_ROLE}' ORDER BY asset_id,docket"):
        assets_by_docket[elibrary.docket_base(str(row["docket"] or ""))].add(
            str(row["asset_id"]))
    filing_entities: dict[tuple[str, str], dict[str, dict]] = collections.defaultdict(dict)
    for row in ctx.staging.query(
            "SELECT source_system,filing_id,entity_key,association_role,facility_key,"
            "evidence_ref FROM filing_entities ORDER BY source_system,filing_id,entity_key"):
        filing_entities[(str(row["source_system"]), str(row["filing_id"]))][
            str(row["entity_key"])] = dict(row)
    # A facility-subject association is intentionally narrower than a named-
    # filer association.  The former can support only observations that truly
    # describe the shared facility as a whole; the latter is occurrence-
    # specific evidence that the target legal entity is itself a filer.  Do
    # not make jointly filed applications, orders or amendments fail merely
    # because deterministic occurrence ownership selected the other named
    # filer as ``filings.entity_key``.
    facility_subject_lng_metrics = {
        "lng_operational_report", "lng_inspection", "lng_status_operating"
    }
    named_filer_lng_metrics = {
        metric.id for metric in document_metrics.values()
        if metric.adapter == lng.ADAPTER
    }

    health_for = {
        Availability.PRESENT: SourceHealth.OK,
        Availability.FILED_NIL: SourceHealth.OK,
        Availability.INTERPRETATION_BLOCKED: SourceHealth.OK,
        Availability.SOURCE_BLANK: SourceHealth.NOT_INDEXED,
        Availability.EXPECTED_NOT_LOCATED: SourceHealth.NOT_INDEXED,
        Availability.KNOWN_NOT_RETRIEVED: SourceHealth.NOT_RETRIEVED,
        Availability.PARSE_FAILED: SourceHealth.PARSE_FAILED,
        Availability.RETRIEVAL_FAILED: SourceHealth.RETRIEVAL_FAILED,
        Availability.UNVERIFIED_AVAILABILITY: SourceHealth.HEALTH_NOT_RECORDED,
        Availability.NOT_IMPLEMENTED: SourceHealth.HEALTH_NOT_RECORDED,
        Availability.NOT_YET_DUE: SourceHealth.HEALTH_NOT_RECORDED,
        # These are legal/source states, not proof that an artefact was indexed
        # and parsed.  In particular NONPUBLIC is not SourceHealth.OK.
        Availability.NONPUBLIC: SourceHealth.HEALTH_NOT_RECORDED,
        Availability.NOT_REQUIRED: SourceHealth.HEALTH_NOT_RECORDED,
        Availability.NOT_APPLICABLE: SourceHealth.HEALTH_NOT_RECORDED,
    }

    quality_rows: list[dict] = []
    for observation in sorted(
            document_observations,
            key=lambda row: str(row.get("observation_id") or "")):
        pair = (str(observation.get("entity_key") or ""),
                str(observation.get("metric_id") or ""))
        anchor = anchors_by_pair[pair]
        availability = str(observation.get("availability") or "")
        source_health = health_for.get(
            availability, SourceHealth.HEALTH_NOT_RECORDED)
        filing_id = str(observation.get("filing_id") or "")
        source_system = str(observation.get("source_system") or "")
        filing_key = (source_system, filing_id)
        filing = known_filings.get(filing_key) if filing_id else None
        if filing_id and filing is None:
            raise ValueError(
                f"document observation {observation.get('observation_id')!r} names "
                f"missing filing occurrence {filing_key}")

        document_id = str(observation.get("document_id") or "")
        document = known_documents.get(document_id) if document_id else None
        if document_id and (document is None or
                            (document["source_system"], document["filing_id"]) != filing_key):
            raise ValueError(
                f"document observation {observation.get('observation_id')!r} names "
                "an attachment outside its filing occurrence")

        association = "no_filing_status"
        association_ok = not filing_id
        if filing is not None:
            relation = filing_entities.get(filing_key, {}).get(pair[0])
            if relation is None or relation.get("association_role") == "compatibility_anchor":
                raise ValueError(
                    f"document observation {observation.get('observation_id')!r} has no "
                    f"occurrence-specific reviewed entity association for {pair[0]!r}")
            if filing["entity_key"] == pair[0]:
                association, association_ok = "same_legal_entity", True
            else:
                target_assets = entity_assets.get(pair[0], set())
                anchor_assets = entity_assets.get(filing["entity_key"], set())
                common_authorities = []
                for docket in dockets_by_filing.get(filing_key, ()):
                    base = elibrary.docket_base(docket)
                    authorised = assets_by_docket.get(base, set())
                    target_matches = authorised & target_assets
                    anchor_matches = authorised & anchor_assets
                    if any(asset_groups.get(left) and
                           asset_groups.get(left) == asset_groups.get(right)
                           for left in target_matches for right in anchor_matches):
                        common_authorities.append(base)
                target_groups = {asset_groups.get(asset) for asset in target_assets
                                 if asset_groups.get(asset)}
                association_role = str(
                    relation.get("association_role") if relation else "")
                role_ok = association_role in {"named_filer", "facility_subject"}
                facility_ok = bool(relation and relation.get("facility_key") in target_groups)
                metric_ok = (
                    (association_role == "named_filer"
                     and pair[1] in named_filer_lng_metrics)
                    or (association_role == "facility_subject"
                        and pair[1] in facility_subject_lng_metrics)
                )
                if relation and role_ok and facility_ok and metric_ok and common_authorities:
                    association = (f"reviewed_shared_lng_facility:"
                                   f"{relation['association_role']}:"
                                   f"{','.join(sorted(set(common_authorities)))}")
                    association_ok = True
                else:
                    raise ValueError(
                        f"document observation {observation.get('observation_id')!r} belongs "
                        f"to filing entity {filing['entity_key']!r}, not {pair[0]!r}, and "
                        "has no occurrence-specific, metric-gated reviewed LNG association")

        evidence_kind = EvidenceKind.NONE
        if filing_id:
            evidence_kind = EvidenceKind.INDEXED_OCCURRENCE
        if document and document["availability"] == "retrieved":
            evidence_kind = EvidenceKind.FILED_OCCURRENCE
        # A nonpublic or not-applicable row cannot become filed evidence merely
        # because the adapter persisted a placeholder/listing document id.
        if availability in {Availability.NONPUBLIC, Availability.NOT_REQUIRED,
                            Availability.NOT_APPLICABLE}:
            evidence_kind = EvidenceKind.NONE

        instant = str(observation.get("instant_date") or "")
        in_window = True
        if instant:
            try:
                instant_year = dt.date.fromisoformat(instant).year
            except ValueError:
                raise ValueError(
                    f"document observation {observation.get('observation_id')!r} has "
                    f"invalid instant_date {instant!r}") from None
            in_window = year_from <= instant_year <= year_to
        is_current = (str(observation.get("version_status") or "") != "superseded"
                      and (filing is None or filing["is_canonical"]))
        unit_ok, unit_gate = cov.admissible(
            anchor, observation,
            canonical={k: v["is_canonical"] for k, v in known_filings.items()},
            lineage={})
        quality_rows.append({
            "observation_id": str(observation.get("observation_id") or ""),
            "entity_key": pair[0], "metric_id": pair[1],
            "source_system": source_system, "filing_id": filing_id,
            "document_id": document_id, "instant_date": instant,
            "scope": "" if observation.get("scope") is None else str(observation["scope"]),
            "unit": str(observation.get("unit") or ""),
            "availability": availability,
            "validation": str(observation.get("validation") or ""),
            "source_health": source_health,
            "evidence_kind": evidence_kind,
            "entity_association": association,
            "entity_association_ok": association_ok,
            "document_filing_association_ok": not document_id or document is not None,
            "in_declared_window": in_window,
            "current_version": is_current,
            "unit_contract_ok": bool(unit_ok),
            "unit_gate": unit_gate,
            "included_in_coverage_denominator": False,
            "classification": (
                "current_occurrence_quality" if is_current and in_window
                else "historical_or_noncurrent_occurrence_quality"),
        })

    # Source health on the route anchor is deliberately conservative.  It says
    # only whether a current in-window result exercised the route; it does not
    # borrow an occurrence's value, date or scope to satisfy the anchor.
    quality_by_pair: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for row in quality_rows:
        quality_by_pair[(row["entity_key"], row["metric_id"])].append(row)
    out: list[dict] = []
    for pair in sorted(expected_pairs):
        anchor = dict(anchors_by_pair[pair])
        current = [row for row in quality_by_pair.get(pair, ())
                   if row["current_version"] and row["in_declared_window"]]
        if any(row["source_health"] == SourceHealth.OK for row in current):
            route_health = SourceHealth.OK
        else:
            defects = [row["source_health"] for row in current
                       if row["source_health"] in SourceHealth.DEFECT]
            route_health = (defects[0] if defects
                            else SourceHealth.HEALTH_NOT_RECORDED)
        strongest = EvidenceKind.NONE
        if any(row["evidence_kind"] == EvidenceKind.FILED_OCCURRENCE for row in current):
            strongest = EvidenceKind.FILED_OCCURRENCE
        elif any(row["evidence_kind"] == EvidenceKind.INDEXED_OCCURRENCE for row in current):
            strongest = EvidenceKind.INDEXED_OCCURRENCE
        anchor.update({
            "asset_id": anchor.get("asset_id") or "",
            "frozen_at": built_at, "frozen_run_id": coverage_run_id,
            "due_date": None,
            "slot_state": (cov.TECHNICAL_FAILURE
                           if route_health in SourceHealth.DEFECT
                           else cov.UNKNOWN_APPLICABILITY
                           if anchor.get("requirement") == cov.UNKNOWN
                           else cov.SLOT_OPEN),
            "obligation_form": "eLibrary document route",
            "obligation_authority": (
                "event-driven official FERC eLibrary route; no fixed occurrence count "
                "or periodic deadline is asserted by this anchor"),
            "obligation_evidence_kind": strongest,
            "source_health": route_health,
            "source_health_detail": (
                f"independent route anchor; {len(current)} current in-window result "
                f"row(s), {len(quality_by_pair.get(pair, ()))} total occurrence/status "
                "row(s); value-level quality is reported separately and never changes "
                "this denominator"),
            "denominator_origin": "document_adapter_anchor",
            "docket": "", "facility": "",
        })
        out.append(anchor)

    counts = collections.Counter(row["classification"] for row in quality_rows)
    quality = {
        "schema": "ferc-document-occurrence-quality-v1",
        "coverage_window": [int(year_from), int(year_to)],
        "denominator_policy": (
            "occurrence/status rows are quality evidence only; the coverage denominator "
            "uses one independent route anchor per eligible entity/metric"),
        "summary": {
            "route_anchors": len(out),
            "occurrence_or_status_rows": len(quality_rows),
            "current_in_window": counts.get("current_occurrence_quality", 0),
            "historical_or_noncurrent": counts.get(
                "historical_or_noncurrent_occurrence_quality", 0),
            "unit_contract_failures": sum(
                1 for row in quality_rows
                if row["current_version"] and row["in_declared_window"]
                and row["availability"] == Availability.PRESENT
                and not row["unit_contract_ok"]),
            "joint_docket_asset_associations": sum(
                row["entity_association"] == "reviewed_joint_docket_asset"
                for row in quality_rows),
            "nonpublic_rows": sum(
                row["availability"] == Availability.NONPUBLIC for row in quality_rows),
        },
        "rows": quality_rows,
    }
    return out, quality

def cmd_coverage(ctx, args) -> int:
    from ferclib.obligations import FrozenApplicability, build_coverage_grid, measure_grid
    from adapters import oil_index

    built_at = (args.built_at or
                dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    _persist_reference_state(ctx)
    applicability_rows = [dict(r) for r in ctx.staging.query(
        "SELECT * FROM applicability ORDER BY form,taxonomy_version,concept_local")]
    applicability_identity = hashlib.sha256(json.dumps(
        applicability_rows, sort_keys=True, separators=(",", ":"),
        default=str).encode()).hexdigest()
    resolver = FrozenApplicability(applicability_rows,
                                   snapshot_id=applicability_identity)
    occurrences = _coverage_occurrences(ctx)
    health = _coverage_source_health(ctx, built_at)
    coverage_run_id = (f"coverage-{ctx.as_of_iso}-{config_digest()}-"
                       f"{_digest_file(TAXONOMY_PINS)}")
    instrument_slots = oil_index.expected_slots(coverage_run_id, built_at)
    observations = [dict(r) for r in ctx.staging.query("SELECT * FROM observations")]
    document_slots, document_quality = _document_coverage_slots(
        ctx, coverage_run_id, built_at, observations,
        year_from=args.year_from, year_to=args.year_to)
    grid = build_coverage_grid(
        year_from=args.year_from, year_to=args.year_to, as_of=ctx.as_of,
        built_at=built_at, run_id=coverage_run_id, universe_path=UNIVERSE,
        metrics=REGISTRY, occurrences=occurrences, source_health=health,
        taxonomy_pins=_load_taxonomy_pins(ctx), instrument_slots=instrument_slots,
        document_slots=document_slots,
        applicability=resolver)

    # PASS THE CANONICAL MAP AND THE LINEAGE MAP (w4-coverage R2).
    #
    # Omitting them was not merely weaker, it was wrong in one direction.
    # `measure()` read `bool(lineage is None or lineage.get(...))`, so with no
    # map supplied an unlineaged derived value counted as SOURCE-MATCHED --
    # "we did not check, therefore it passes". 703 derived observations have
    # neither a source fact id nor a single lineage edge, and every one of them
    # would have been reported as source-matched by a production run.
    #
    # w4 found this by auditing their own file after w2-ioc applied their
    # permissive-default test to `adapters/ioc.py`. The figures they published
    # were unaffected only because their harness happened to supply the map;
    # this call site is the one that did not.
    canonical = {(r["source_system"], str(r["filing_id"])): bool(r["is_canonical"])
                 for r in ctx.staging.query(
                     "SELECT source_system, filing_id, is_canonical FROM filings")}
    lineage: dict[str, list[str]] = {}
    for r in ctx.staging.query("SELECT observation_id, input_role, input_version_status "
                               "FROM lineage_edges"):
        lineage.setdefault(r["observation_id"], []).append({
            "input_role": r["input_role"] or "",
            "input_version_status": r["input_version_status"] or ""})
    ctx.log("info", f"coverage inputs: {len(canonical):,} canonical filing flags, "
                    f"{len(lineage):,} observations with lineage")
    measurement = measure_grid(grid, observations, canonical=canonical, lineage=lineage)
    ctx.staging.replace_coverage(list(grid.slots), list(measurement.rows))
    stats = measurement.statistics
    generation = {"grid": grid.manifest(), "statistics": stats,
                  "document_occurrence_quality": document_quality,
                  "separated_occurrences": list(grid.separated_occurrences),
                  "unused_source_health": [r.__dict__ for r in grid.unused_source_health],
                  "zero_slot_obligations": list(grid.zero_slot_obligations),
                  "offline_cache_misses": list(ctx.client.cache_misses),
                  "satisfied_offline_cache_probes":
                      list(ctx.client.satisfied_cache_misses)}
    _atomic_json(VERIFICATION / "coverage_generation.json", _json_safe(generation))
    print(json.dumps({k: v for k, v in stats.items() if not k.endswith("outcomes")}, indent=1))
    print("\ncore outcomes:", json.dumps(stats.get("core_outcomes", {}), indent=1))
    print(f"coverage input digest: {grid.input_digest}")
    print(f"coverage evidence: {VERIFICATION / 'coverage_generation.json'}")
    return 0


def cmd_status(ctx, args) -> int:
    print("staging tables:")
    for t, n in ctx.staging.counts().items():
        if n:
            print(f"  {t:32s} {n:>8,}")
    print("\nledger:", json.dumps(ctx.ledger.by_state()))
    stale = ctx.ledger.stale_tasks()
    if stale:
        print(f"\nstale units ({len(stale)}) -- these hold LAST GOOD rows; the freshness "
              "claim degraded, not the data:")
        for s in stale[:20]:
            print(f"  {s['task_id']:34s} last success {s['last_success_at'] or 'never':20s} "
                  f"{s['stale_reason'][:70]}")
    ann = ctx.staging.query("SELECT COUNT(*) n FROM reviewed_source_annotations")
    print(f"\nreviewed source annotations in database: {ann[0]['n'] if ann else 0}")
    blockers = ctx.staging.query(
        "SELECT adapter,kind,summary,scope FROM blockers WHERE resolved_at IS NULL")
    if blockers:
        print(f"\nopen blockers: {len(blockers)}")
        for b in blockers[:20]:
            print(f"  [{b['kind']:14s}] {b['adapter']:13s} {b['summary'][:90]}")
    return 0


def _snapshot_digest(paths: list[pathlib.Path], *,
                     excluded_files: set[pathlib.Path] | None = None) -> str:
    h = hashlib.sha256()
    excluded = {path.absolute() for path in (excluded_files or set())}
    files: list[pathlib.Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(p for p in path.rglob("*") if p.is_file()
                         and "__pycache__" not in p.parts and p.suffix != ".pyc"
                         and p.absolute() not in excluded)
        elif path.is_file():
            if path.absolute() not in excluded:
                files.append(path)
    for path in sorted(set(files), key=lambda p: str(p)):
        try:
            label = str(path.relative_to(HERE))
        except ValueError:
            label = str(path)
        h.update(label.encode("utf-8") + b"\0")
        h.update(_sha256_file(path).encode("ascii") + b"\n")
    return h.hexdigest()


def _code_snapshot() -> str:
    return _snapshot_digest([
        HERE / "run.py", HERE / "build_crosswalk.py", HERE / "build_field_status.py",
        HERE / "exporters.py", HERE / "validate.py", HERE / "ferclib",
        HERE / "adapters", HERE / "migrations"])


def _verified_redundant_input_alias() -> tuple[pathlib.Path, ...]:
    """Return the sole packaging exclusion after verifying its retained identity.

    The capture alias is present in Build A but intentionally absent from a
    clean package extraction.  Both states receive the same input snapshot only
    because the exact official response is retained at its content-addressed
    cache path and two immutable provenance records bind that response to the
    official request and cache import.  Any mismatch is fatal; this function
    never searches for a similar file and never excludes a second path.
    """
    declaration = _REDUNDANT_INPUT_ALIAS
    alias = HERE / declaration["alias_path"]
    index_path = SOURCE_CACHE / "index.json"
    canonical = SOURCE_CACHE / declaration["cache_path"]
    results_path = HERE / declaration["results_path"]
    ledger_path = HERE / declaration["ledger_path"]

    def exact_file(path: pathlib.Path, *, sha256: str, byte_size: int,
                   label: str) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"{label} is absent or is a symlink: {path}")
        raw = path.read_bytes()
        actual = hashlib.sha256(raw).hexdigest()
        if len(raw) != byte_size or actual != sha256:
            raise RuntimeError(
                f"{label} identity mismatch: expected {byte_size}/{sha256}, "
                f"read {len(raw)}/{actual}")
        return raw

    expected_hash = str(declaration["sha256"])
    expected_bytes = int(declaration["bytes"])
    exact_file(canonical, sha256=expected_hash, byte_size=expected_bytes,
               label="retained canonical cache object for redundant input")

    if index_path.is_symlink() or not index_path.is_file():
        raise RuntimeError(
            f"source-cache index for redundant input is absent or a symlink: {index_path}")
    try:
        cache_index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"source-cache index for redundant input is invalid: {exc}") \
            from None
    source_url = str(declaration["source_url"])
    cache_key = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    entry = cache_index.get(cache_key) if isinstance(cache_index, dict) else None
    if not isinstance(entry, dict) or {
            "content_hash": entry.get("content_hash"),
            "cache_path": entry.get("cache_path"),
            "byte_size": entry.get("byte_size"),
            "source_system": entry.get("source_system"),
            "source_url": entry.get("source_url"),
    } != {
            "content_hash": expected_hash,
            "cache_path": declaration["cache_path"],
            "byte_size": expected_bytes,
            "source_system": "eLibrary",
            "source_url": source_url,
    }:
        raise RuntimeError(
            "source-cache index does not bind the redundant input to its exact "
            "retained official-FERC object")

    results_raw = exact_file(
        results_path, sha256=str(declaration["results_sha256"]),
        byte_size=int(declaration["results_bytes"]),
        label="official-FERC individual-response provenance")
    try:
        results = json.loads(results_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"official-FERC individual-response provenance is invalid: {exc}") \
            from None
    result_matches = []
    for row in results.get("results", []) if isinstance(results, dict) else []:
        response = row.get("response") if isinstance(row, dict) else None
        request = row.get("request") if isinstance(row, dict) else None
        if (row.get("accession_number") == declaration["accession"]
                and row.get("attachment_id") == declaration["attachment_id"]
                and isinstance(response, dict) and isinstance(request, dict)
                and response.get("http_status") == 200
                and response.get("complete_body") is True
                and response.get("bytes") == expected_bytes
                and response.get("sha256") == expected_hash
                and request.get("cache_url") == source_url):
            result_matches.append(row)
    if len(result_matches) != 1:
        raise RuntimeError(
            "official-FERC response provenance does not contain exactly one matching "
            "successful capture for the redundant input")

    ledger_raw = exact_file(
        ledger_path, sha256=str(declaration["ledger_sha256"]),
        byte_size=int(declaration["ledger_bytes"]),
        label="append-only cache-import provenance")
    ledger_matches = []
    try:
        for line in ledger_raw.decode("utf-8").splitlines():
            record = json.loads(line)
            if not isinstance(record, dict) or record.get("status") != "complete":
                continue
            result_identity = record.get("results_identity") or {}
            if (result_identity.get("bytes") != declaration["results_bytes"]
                    or result_identity.get("sha256") != declaration["results_sha256"]):
                continue
            for entry_row in record.get("entries", []):
                if (isinstance(entry_row, dict)
                        and entry_row.get("accession") == declaration["accession"]
                        and entry_row.get("attachment_ids") == [declaration["attachment_id"]]
                        and entry_row.get("action") in ("imported", "already_present_identical")
                        and entry_row.get("http_status") == 200
                        and entry_row.get("cache_url") == source_url
                        and entry_row.get("byte_size") == expected_bytes
                        and entry_row.get("content_sha256") == expected_hash):
                    ledger_matches.append(entry_row)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"append-only cache-import provenance is invalid: {exc}") from None
    if len(ledger_matches) != 1:
        raise RuntimeError(
            "append-only cache-import provenance does not contain exactly one matching "
            "successful import for the redundant input")

    if alias.exists() or alias.is_symlink():
        exact_file(alias, sha256=expected_hash, byte_size=expected_bytes,
                   label="redundant official-response capture alias")
    return (alias,)


def _input_snapshot() -> str:
    excluded = set(_verified_redundant_input_alias())
    return _snapshot_digest([
        UNIVERSE, ANNOTATIONS_DIR, RUN_PLAN, SOURCE_CACHE / "index.json",
        HERE / "inputs" / "day3", HERE / "inputs" / "audit_baseline",
        HERE / "inputs" / "official_ferc",
        HERE / "inputs" / "official_ferc_recovery",
        HERE / "inputs" / "official_ferc_capacity_20260226_5162",
        HERE / "inputs" / "official_ferc_elibrary_search_recovery_20260909",
        HERE / "inputs" / "official_ferc_elibrary_dependency_recovery_20260910",
        HERE / "inputs" / "reference_regressions",
        HERE / _REDUNDANT_INPUT_ALIAS["results_path"],
        HERE / _REDUNDANT_INPUT_ALIAS["ledger_path"],
        HERE / "implementation_logs" / "input_recovery" /
            "build_a_v4_dependency_capture.log",
    ], excluded_files=excluded)


def _unit_commit_snapshot(ctx) -> dict:
    """Stable identity of data-bearing unit commits, excluding volatile run IDs."""
    rows = [dict(r) for r in ctx.staging.query(
        "SELECT adapter,entity_cid,scope_key,identity_json,input_digest,"
        "observation_count,edge_count,document_fact_count FROM unit_commits "
        "ORDER BY adapter,entity_cid,scope_key,input_digest,identity_json")]
    # Repeated no-change runs can create equivalent markers under new run IDs.
    # Collapse exact semantic duplicates so Build A and Build B keep one stable
    # data identity even though their operational run UUIDs legitimately differ.
    canonical = []
    seen = set()
    for row in rows:
        key = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
        if key not in seen:
            seen.add(key)
            canonical.append(row)
    body = json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")
    return {"unit_commit_count": len(canonical),
            "unit_commit_snapshot": hashlib.sha256(body).hexdigest()}


def _database_semantic_identity(ctx) -> str:
    """Digest the consumer/provenance state, excluding run/publication journals.

    The digest is independent of SQLite page layout. It intentionally includes
    warnings, scope, units, source facts, version links and coverage; consumers
    may not obtain parity by ignoring those fields.
    """
    tables = (
        "entities", "assets", "asset_entity_map", "ownership", "dockets",
        "asset_dockets", "filings", "filing_entities", "filing_dockets",
        "documents", "source_facts",
        "source_contexts", "source_dimensions", "source_units", "source_manifest",
        "observations", "observation_versions", "lineage_populations", "lineage_edges",
        "document_facts", "events", "coverage_expected", "coverage_measured",
        "field_status", "requirements_crosswalk", "applicability", "taxonomy_sources",
        "blockers", "reviewed_source_annotations")
    h = hashlib.sha256()
    for table in tables:
        cols = [r[1] for r in ctx.staging.query(f'PRAGMA table_info("{table}")')]
        if not cols:
            raise RuntimeError(f"publication identity table absent: {table}")
        pk = [r[1] for r in sorted(
            ctx.staging.query(f'PRAGMA table_info("{table}")'), key=lambda r: r[5])
              if r[5]]
        order = pk or cols
        h.update((table + "\0" + "\0".join(cols) + "\n").encode("utf-8"))
        sql = (f'SELECT {",".join(chr(34)+c+chr(34) for c in cols)} FROM "{table}" '
               f'ORDER BY {",".join(chr(34)+c+chr(34) for c in order)}')
        # source_facts alone is close to one million rows.  Materialising it in
        # Staging.query used several GB and forced routine validation into swap.
        # Streaming preserves the same declared order and byte-for-byte digest
        # algorithm while keeping memory bounded.  Test doubles without the
        # streaming interface retain the old list-based fallback.
        iterator = (ctx.staging.iter_query(sql)
                    if hasattr(ctx.staging, "iter_query") else ctx.staging.query(sql))
        for row in iterator:
            h.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":"),
                                default=str).encode("utf-8") + b"\n")
    return h.hexdigest()


def _coverage_export_files(ctx, out: pathlib.Path) -> dict:
    expected = [dict(r) for r in ctx.staging.query(
        "SELECT * FROM coverage_expected ORDER BY slot_id")]
    measured = {r["slot_id"]: dict(r) for r in ctx.staging.query(
        "SELECT * FROM coverage_measured ORDER BY slot_id")}
    if not expected or set(measured) != {r["slot_id"] for r in expected}:
        raise RuntimeError("coverage expected/measured generation is absent or inconsistent")
    combined = []
    for e in expected:
        row = dict(e)
        row.update({k: v for k, v in measured[e["slot_id"]].items() if k != "slot_id"})
        combined.append(row)
    cov.write_csv(out / "coverage_by_slot.csv", combined)
    stats = cov.summarise(expected, list(measured.values()))
    groups = {
        "by_template": cov.group_summary(expected, list(measured.values()), "template"),
        "by_adapter": cov.group_summary(
            expected, list(measured.values()), "adapter",
            keyer=lambda s: BY_ID[s["metric_id"]].adapter if s.get("metric_id") in BY_ID
            else "unregistered"),
        "by_metric": cov.group_summary(expected, list(measured.values()), "metric_id"),
    }
    (out / "coverage_statistics.json").write_text(
        json.dumps(_json_safe(stats), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    (out / "coverage_groups.json").write_text(
        json.dumps(_json_safe(groups), indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return stats


def _field_status_export_files(ctx, out: pathlib.Path) -> dict:
    rows = [dict(r) for r in ctx.staging.query(
        "SELECT * FROM field_status ORDER BY template,field_id")]
    if not rows:
        raise RuntimeError("field_status is empty")
    by_outcome = collections.Counter(r["outcome"] for r in rows)
    by_template: dict[str, collections.Counter] = {}
    for row in rows:
        by_template.setdefault(row["template"], collections.Counter())[row["outcome"]] += 1
    summary = {
        "field_rows": len(rows),
        "distinct_metrics": len({r["metric_id"] for r in rows}),
        "by_outcome": dict(by_outcome),
        "by_template": {k: dict(v) for k, v in sorted(by_template.items())},
        "rows_with_implemented_adapter": sum(bool(r["adapter_implemented"]) for r in rows),
        "rows_with_local_data": sum(bool(r["has_data_in_template"]) for r in rows),
        "rows_validated_locally": sum(bool(r["validated_in_template"]) for r in rows),
        "readiness_basis": "within template, adapter, eligibility, period and quality gate",
    }
    (out / "field_status_summary.json").write_text(
        json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def cmd_export(ctx, args) -> int:
    from exporters import write_all
    from ferclib.frontend_exports import (
        validate_frontend_contract_files, write_frontend_contract)
    from ferclib.publication import publish_generation, stage_generation

    # These are executable release inputs, not decorative tables. A successful
    # export may not paper over a run that never integrated them.
    required_tables = ("source_manifest", "applicability", "taxonomy_sources",
                       "requirements_crosswalk")
    missing = [t for t in required_tables
               if not ctx.staging.query(f'SELECT 1 FROM "{t}" LIMIT 1')]
    if missing:
        raise RuntimeError(f"required release reference stores are empty: {missing}")

    with tempfile.TemporaryDirectory(prefix="ferc-export-", dir=OUTPUT_DIR) as tmp_name:
        tmp = pathlib.Path(tmp_name)
        n = write_all(ctx, tmp)
        # Keep the general CSV writer usable by narrow test/diagnostic contexts;
        # the production export command owns the frontend contract and its
        # reviewed-universe dependency explicitly.
        frontend_write_stats = write_frontend_contract(ctx, tmp, ctx.universe_path)
        n += bool(frontend_write_stats["files"])
        coverage_stats = _coverage_export_files(ctx, tmp)
        field_summary = _field_status_export_files(ctx, tmp)
        files = {str(path.relative_to(tmp)): path.read_bytes()
                 for path in sorted(tmp.rglob("*")) if path.is_file()}

    mandatory = {
        "canonical_observations.csv", "lineage_edges.csv", "filing_inventory.csv",
        "lineage_populations.csv", "observation_versions.csv", "entities.csv",
        "assets.csv", "asset_entity_map.csv", "ownership.csv", "asset_dockets.csv",
        "metric_registry.csv", "frontend_v1/contract.json", "frontend_v1/assets.json",
        "frontend_v1/instruments.json", "frontend_v1/headline_availability.json",
        "frontend_v1/source_index.json", "documents.csv", "document_facts.csv",
        "source_manifest.csv", "applicability.csv", "reviewed_source_annotations.csv",
        "coverage_by_slot.csv", "coverage_statistics.json", "coverage_groups.json",
        "field_status.csv", "field_status_summary.json", "blockers.csv"}
    absent = sorted(mandatory - set(files))
    if absent:
        raise RuntimeError(f"mandatory export(s) absent before manifest generation: {absent}")
    frontend_contract_stats = validate_frontend_contract_files(files)

    # frontend_v1 is a managed compatibility namespace.  Never issue a new
    # receipt while an old dynamic route would remain beside the new payload.
    # The roster is stable for this candidate, so failing closed is safer than
    # deleting an unexpected file during publication.
    expected_frontend = {name for name in files if name.startswith("frontend_v1/")}
    compatibility_frontend = EXPORTS / "frontend_v1"
    existing_frontend = set()
    if compatibility_frontend.exists():
        for path in compatibility_frontend.rglob("*"):
            if path.is_file() or path.is_symlink():
                existing_frontend.add(str(path.relative_to(EXPORTS)))
    stale_frontend = sorted(existing_frontend - expected_frontend)
    if stale_frontend:
        raise RuntimeError(
            "stale managed frontend compatibility route(s) must be reviewed before "
            f"publication: {stale_frontend[:20]}")

    code_snapshot = _code_snapshot()
    input_snapshot = _input_snapshot()
    database_identity = _database_semantic_identity(ctx)
    commit_snapshot = _unit_commit_snapshot(ctx)
    metadata = {
        "schema": "ferc_consumer_publication_v1", "run_id": ctx.staging.run_id or "",
        "code_snapshot": code_snapshot, "input_snapshot": input_snapshot,
        "database_identity": database_identity, "registry_version": REGISTRY_VERSION,
        "code_version": CODE_VERSION, "as_of": ctx.as_of_iso,
        **commit_snapshot,
        "coverage": coverage_stats, "field_status": field_summary,
        "frontend_write": frontend_write_stats,
        "frontend_contract": frontend_contract_stats,
        "activation_rule": ("current only when publication_receipt.json names this generation "
                            "and publication_generations contains the same published row")}
    manifest = stage_generation(OUTPUT_DIR, "consumer_exports", files, metadata=metadata)
    publication_row = {
        "generation_id": manifest["generation_id"], "run_id": ctx.staging.run_id,
        "code_snapshot": code_snapshot, "input_snapshot": input_snapshot,
        "database_identity": database_identity,
        "manifest_json": json.dumps(manifest, sort_keys=True), "status": "staged",
        "published_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    ctx.staging.record_publication(publication_row)
    manifest = publish_generation(
        OUTPUT_DIR, OUTPUT_DIR / "publication_receipt.json", "consumer_exports", files,
        metadata=metadata,
        compatibility_targets={name: EXPORTS / name for name in files})
    publication_row.update({
        "manifest_json": json.dumps(manifest, sort_keys=True), "status": "published",
        "published_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
    ctx.staging.record_publication(publication_row)
    ctx.log("info", f"published {n} base export groups / {len(files)} files as "
                    f"generation {manifest['generation_id']}")
    return 0


def cmd_validate(ctx, args) -> int:
    from validate import run_checks
    from ferclib.frontend_exports import validate_frontend_contract_files
    from ferclib.publication import PublicationError, verify_receipt
    rc = run_checks(ctx, VERIFICATION)
    receipt = OUTPUT_DIR / "publication_receipt.json"
    try:
        manifest = verify_receipt(OUTPUT_DIR, receipt)
        rows = ctx.staging.query(
            "SELECT * FROM publication_generations WHERE generation_id=? AND status='published'",
            (manifest["generation_id"],))
        if len(rows) != 1:
            raise RuntimeError("receipt has no matching published database record")
        meta = manifest.get("metadata", {})
        row = rows[0]
        try:
            stored_manifest = json.loads(row["manifest_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"database publication manifest is invalid: {exc}") from None
        if stored_manifest != manifest:
            raise RuntimeError("receipt differs from the exact manifest stored in the database")
        for key in ("code_snapshot", "input_snapshot", "database_identity"):
            if row[key] != meta.get(key):
                raise RuntimeError(f"publication {key} differs between receipt and database")
        expected_snapshots = {
            "code_snapshot": _code_snapshot(),
            "input_snapshot": _input_snapshot(),
            **_unit_commit_snapshot(ctx),
        }
        for key, current_value in expected_snapshots.items():
            if meta.get(key) != current_value:
                raise RuntimeError(
                    f"publication {key} does not match the current candidate/input/data state")
        current = _database_semantic_identity(ctx)
        if current != meta.get("database_identity"):
            raise RuntimeError(
                "database semantic state changed after the consumer generation was published")
        frontend_bodies = {}
        for name, expected in manifest.get("files", {}).items():
            relative = pathlib.PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name:
                raise RuntimeError(f"unsafe compatibility export name in receipt: {name!r}")
            path = EXPORTS.joinpath(*relative.parts)
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"compatibility export is missing or a symlink: {name}")
            if path.stat().st_size != expected.get("bytes") \
                    or _sha256_file(path) != expected.get("sha256"):
                raise RuntimeError(f"compatibility export differs from generation: {name}")
            # The closure validator needs JSON bodies and only the existence of
            # large CSV dependencies; avoid loading all CSVs into memory again.
            frontend_bodies[name] = (path.read_bytes()
                                     if name.startswith("frontend_v1/") else b"")
        expected_frontend = {
            name for name in manifest.get("files", {})
            if name.startswith("frontend_v1/")}
        if expected_frontend:
            actual_frontend = set()
            root = EXPORTS / "frontend_v1"
            if root.exists():
                for path in root.rglob("*"):
                    if path.is_file() or path.is_symlink():
                        actual_frontend.add(str(path.relative_to(EXPORTS)))
            if actual_frontend != expected_frontend:
                raise RuntimeError(
                    "managed frontend compatibility files differ from receipt; "
                    f"missing={sorted(expected_frontend-actual_frontend)[:20]} "
                    f"extra={sorted(actual_frontend-expected_frontend)[:20]}")
            frontend_stats = validate_frontend_contract_files(frontend_bodies)
            print("frontend contract closure: PASS "
                  f"({frontend_stats['asset_payloads']} assets, "
                  f"{frontend_stats['entity_payloads']} entity histories)")
        print(f"publication boundary: PASS ({manifest['generation_id']})")
    except (OSError, ValueError, KeyError, RuntimeError, PublicationError) as exc:
        print(f"publication boundary: FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        rc = max(rc, 1)
    return rc


# ---------------------------------------------------------------- identity

def _seed_identity(ctx, rows: list[dict]) -> None:
    ents, assets, maps, own, dockets = {}, {}, [], [], []
    for r in rows:
        ents[r["entity_key"]] = {
            "entity_key": r["entity_key"],
            "cid": r["entity_key"] if r["entity_key"].startswith("C") else None,
            "local_key": None if r["entity_key"].startswith("C") else r["entity_key"],
            "legal_name": r["entity_name"], "parent": r.get("parent"),
            "ticker": r.get("ticker"), "jurisdiction": r.get("jurisdiction"),
            "note": r.get("note")}
        assets[r["asset_id"]] = {
            "asset_id": r["asset_id"], "ticker": r.get("ticker"),
            "display_name": r["display_name"], "template": r["template"],
            "authority": r.get("authority"), "cod_group": r.get("cod_group"),
            "group_key": r.get("group_key"), "status": r.get("status"),
            "note": r.get("note")}
        maps.append({"asset_id": r["asset_id"], "entity_key": r["entity_key"],
                     "mapping_scope": r.get("mapping_scope") or "whole_entity",
                     "effective_from": None, "effective_to": None,
                     "note": r.get("interest_display")})
        if r.get("parent"):
            own.append({"entity_key": r["entity_key"], "parent": r["parent"],
                        "ticker": r.get("ticker"),
                        "pct": float(r["ownership_pct"]) if r.get("ownership_pct") else None,
                        "basis": "jv" if r.get("via_jv") in ("1", "True", "true") else "direct",
                        "qualifier": r.get("interest_display"),
                        "effective_from": None, "effective_to": None})
    ctx.staging.write_entities(list(ents.values()))
    ctx.staging.write_assets(list(assets.values()), maps, own, dockets)
    ctx.log("info", f"identity seeded: {len(ents)} entities, {len(assets)} assets, "
                    f"{len(maps)} asset-entity mappings")


def _write_run_status(ctx, results, status: str = "complete", note: str = "") -> None:
    counts = ctx.staging.counts()
    blockers = ctx.staging.query(
        "SELECT adapter,kind,summary FROM blockers WHERE resolved_at IS NULL")
    stale = ctx.ledger.stale_tasks()

    # Machine-readable first: a caller must be able to tell a complete run from a
    # bounded or failed one without parsing prose (audit A01 clause 5).
    payload = {
        "run_id": ctx.staging.run_id,
        "command": getattr(ctx.args, "command", ""),
        "status": status,
        "exit_code": _exit_code(status),
        "note": note,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "as_of": ctx.as_of_iso,
        "offline": ctx.offline,
        "registry_version": REGISTRY_VERSION,
        "code_version": CODE_VERSION,
        "config_digest": config_digest(),
        "annotation_set_version": ctx.annotation_manifest.get("annotation_set_version", ""),
        "window": {"year_from": ctx.args.year_from, "year_to": ctx.args.year_to},
        "filters": {k: getattr(ctx.args, k, None) for k in
                    ("entity", "template", "adapter", "limit", "budget", "force")},
        "requests_made": ctx.client.requests_made,
        "request_budget": ctx.client.budget,
        "budget_exhausted": ctx.client.budget_exhausted,
        "offline_cache_misses": ctx.client.cache_misses[:200],
        "offline_cache_miss_count": len(ctx.client.cache_misses),
        "satisfied_offline_cache_probes": ctx.client.satisfied_cache_misses[:200],
        "satisfied_offline_cache_probe_count": len(ctx.client.satisfied_cache_misses),
        "adapters": results,
        "failed_units": ctx.failures,
        "stale_units": stale,
        "applied_annotations": ctx.applied_annotations,
        "open_blockers": len(blockers),
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # A certified extraction may preserve read-only payload modes. Publishing
    # by replacement, rather than truncating the old inode, works in a writable
    # preparation directory and guarantees that a reader sees either the prior
    # complete status or the new complete status -- never partial JSON.
    _atomic_json(OUTPUT_DIR / "run_status.json", payload)

    lines = [
        "# RUN_STATUS", "",
        f"Run: `{ctx.staging.run_id}`  ",
        f"Status: **{status}** (exit {_exit_code(status)})"
        + (f" -- {note}" if note else "") + "  ",
        f"Generated: {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}  ",
        f"As-of capture date: `{ctx.as_of_iso}`  "
        + ("Cache-only replay  " if ctx.offline else ""),
        f"Window: {ctx.args.year_from}-{ctx.args.year_to}  ",
        f"Registry: `{REGISTRY_VERSION}`  Code: `{CODE_VERSION}`  "
        f"Config: `{config_digest()}`  "
        f"Annotations: `{ctx.annotation_manifest.get('annotation_set_version', 'NOT LOADED')}`", "",
        "## Adapters", "",
        "| Adapter | Eligible | Expected slots | Observations | ok | failed | unchanged | Status |",
        "|---|---:|---:|---:|---:|---:|---:|---|"]
    for r in results:
        lines.append(f"| {r['adapter']} | {r['entities']} | {r.get('expected', 0)} | "
                     f"{r.get('observations', 0)} | {r.get('succeeded', 0)} | "
                     f"{r.get('failed', 0)} | {r.get('unchanged', 0)} | {r['status']} |")
    if ctx.failures:
        lines += ["", f"## Failed units ({len(ctx.failures)})", "",
                  "| Adapter | Entity | Kind | Error |", "|---|---|---|---|"]
        for f in ctx.failures:
            lines.append(f"| {f['adapter']} | {f['entity']} | {f['kind']} | "
                         f"{f['error'][:150].replace('|', '/')} |")
    if stale:
        lines += ["", f"## Stale units ({len(stale)})", "",
                  "These retain their LAST GOOD rows. The freshness claim, not the data,",
                  "is what degraded: `last_success_at` is when the source was last",
                  "successfully consulted.", "",
                  "| Task | Last success | Stale since | Reason |", "|---|---|---|---|"]
        for s in stale:
            lines.append(f"| {s['task_id']} | {s['last_success_at'] or 'never'} | "
                         f"{s['stale_since']} | {s['stale_reason'][:120].replace('|', '/')} |")
    if ctx.client.cache_misses:
        lines += ["", f"## Offline cache misses ({len(ctx.client.cache_misses)})", "",
                  "Evidence this replay needed and does not have. Absence of a cached",
                  "object is a missing input, never a FERC data gap.", ""]
        lines += [f"- `{u}`" for u in ctx.client.cache_misses[:50]]
    if ctx.client.satisfied_cache_misses:
        lines += ["", f"## Satisfied offline cache probes "
                  f"({len(ctx.client.satisfied_cache_misses)})", "",
                  "These request shapes were absent, but a different cached official",
                  "response was validated as the complete requested population. They are",
                  "retained as probes and are not counted as missing inputs.", ""]
        lines += [f"- `{row['url']}` -> `{row['satisfied_by']['content_hash']}`"
                  for row in ctx.client.satisfied_cache_misses[:50]]
    lines += ["", "## Staging contents", "", "| Table | Rows |", "|---|---:|"]
    for t, n in counts.items():
        if n:
            lines.append(f"| {t} | {n:,} |")
    lines += ["", f"## Open blockers ({len(blockers)})", ""]
    for b in blockers:
        lines.append(f"- **{b['adapter']}** ({b['kind']}): {b['summary']}")
    lines += ["", "## Ledger", "", ctx.ledger.markdown()]
    _atomic_text(OUTPUT_DIR / "RUN_STATUS.md", "\n".join(lines) + "\n")


# ---------------------------------------------------------------- run plan

def _sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _plan_path(value: str) -> pathlib.Path:
    """Resolve the small, explicit path vocabulary used by the replay plan."""
    if value == "@staging_db":
        return STAGING_DB
    if value == "@ledger":
        return LEDGER
    if value == "@output":
        return OUTPUT_DIR
    if value == "@cache":
        return SOURCE_CACHE
    if value.startswith("@output/"):
        return OUTPUT_DIR / value.removeprefix("@output/")
    if value.startswith("@cache/"):
        return SOURCE_CACHE / value.removeprefix("@cache/")
    return HERE / value


def _plan_argv(argv: list[str]) -> list[str]:
    out = []
    for value in argv:
        if value in ("@staging_db", "@ledger", "@output", "@cache") \
                or value.startswith(("@output/", "@cache/")):
            out.append(str(_plan_path(value)))
        else:
            out.append(value)
    return out


def _recorded_plan_path(value: str) -> str:
    """Return a portable path for a resolved run-plan input or output.

    Replay ledgers are shipped and replayed from a clean extraction.  Recording
    the temporary Build-A directory in every identity makes an otherwise
    identical Build B differ and, more importantly, prevents a downstream gate
    from proving that a declared logical output is the file it inspected.
    Resolve the plan token first, then record its path relative to either the
    output boundary or the immutable candidate tree.  A plan path outside both
    declared roots is not portable and is refused.
    """
    resolved = _plan_path(value).resolve()
    for base in (OUTPUT_DIR.resolve(), HERE.resolve()):
        try:
            relative = resolved.relative_to(base)
        except ValueError:
            continue
        return relative.as_posix() if relative.parts else "."
    raise ValueError(f"run-plan path is outside the candidate/output boundaries: {value}")


def _path_identity(path: pathlib.Path, *, recorded_path: str | None = None) -> dict:
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return {"path": recorded_path if recorded_path is not None else str(path),
            "bytes": path.stat().st_size,
            "sha256": _sha256_file(path)}


def _sqlite_query_identity(spec: dict) -> dict:
    database_decl = spec.get("database", "@staging_db")
    db = _plan_path(database_decl)
    if not db.is_file():
        raise FileNotFoundError(str(db))
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        quick = con.execute("PRAGMA quick_check").fetchone()[0]
        if quick != "ok":
            raise RuntimeError(f"SQLite quick_check failed for {db}: {quick}")
        rows = [dict(r) for r in con.execute(spec["sql"], spec.get("params", []))]
        body = json.dumps(rows, sort_keys=True, separators=(",", ":"),
                          default=str).encode("utf-8")
        minimum = int(spec.get("minimum_rows", 0))
        if len(rows) < minimum:
            raise RuntimeError(
                f"{spec.get('name', 'SQLite output')} has {len(rows)} rows; "
                f"minimum is {minimum}")
        return {"database": _recorded_plan_path(database_decl),
                "query": spec["sql"], "rows": len(rows),
                "sha256": hashlib.sha256(body).hexdigest(), "quick_check": quick}
    finally:
        con.close()


def _step_output_identities(step: dict) -> list[dict]:
    identities = []
    for spec in step.get("outputs", []):
        if "publication_receipt" in spec:
            from ferclib.publication import PublicationError, verify_receipt

            rule = spec["publication_receipt"]
            base = _plan_path(rule["base"])
            receipt_path = _plan_path(rule["path"])
            compatibility_root = _plan_path(rule["compatibility_root"])
            try:
                manifest = verify_receipt(base, receipt_path)
            except PublicationError as exc:
                raise RuntimeError(f"invalid publication receipt {receipt_path}: {exc}") \
                    from None
            compatibility = []
            compatibility_recorded = _recorded_plan_path(rule["compatibility_root"])
            for logical, expected in sorted(manifest["files"].items()):
                path = compatibility_root / logical
                if path.is_symlink() or not path.is_file():
                    raise RuntimeError(
                        f"publication compatibility file absent or symlink: {path}")
                recorded = (pathlib.PurePosixPath(compatibility_recorded) / logical).as_posix()
                actual = _path_identity(path, recorded_path=recorded)
                if (actual["bytes"] != expected["bytes"]
                        or actual["sha256"] != expected["sha256"]):
                    raise RuntimeError(
                        f"publication compatibility identity mismatch: {path}")
                compatibility.append({"logical_path": logical, **actual})
            item = {
                "receipt": _path_identity(
                    receipt_path, recorded_path=_recorded_plan_path(rule["path"])),
                "base": _recorded_plan_path(rule["base"]),
                "compatibility_root": compatibility_recorded,
                "generation_id": manifest["generation_id"],
                "kind": manifest["kind"],
                "files": compatibility,
            }
            item["name"] = spec.get("name", rule["path"])
        elif "path" in spec:
            item = _path_identity(
                _plan_path(spec["path"]),
                recorded_path=_recorded_plan_path(spec["path"]))
            item["name"] = spec.get("name", spec["path"])
        elif "sqlite_query" in spec:
            item = _sqlite_query_identity(spec["sqlite_query"])
            item["name"] = spec.get("name", spec["sqlite_query"].get("name", "sqlite"))
        else:
            raise ValueError(f"step output has no supported identity rule: {spec}")
        identities.append(item)
    if not identities:
        raise ValueError(f"step {step.get('id')} declares no verifiable outputs")
    return identities


def _atomic_json(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _atomic_text(path: pathlib.Path, value: str) -> None:
    """Durably replace a UTF-8 text artifact without truncating last-good."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(value)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _read_replay_ledger(path: pathlib.Path, plan_hash: str) -> dict:
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema") == "ferc_replay_step_status_v2":
                payload.setdefault("steps", {})
                payload["active_plan_sha256"] = plan_hash
                return payload
        except (OSError, json.JSONDecodeError):
            pass
    return {"schema": "ferc_replay_step_status_v2", "active_plan_sha256": plan_hash,
            "steps": {}}


def _step_input_identity(step: dict, ledger: dict) -> dict:
    inputs = []
    for decl in step.get("inputs", []):
        value = decl if isinstance(decl, str) else decl["path"]
        item = _path_identity(
            _plan_path(value), recorded_path=_recorded_plan_path(value))
        item["name"] = value
        inputs.append(item)
    dependencies = {}
    for dep in step.get("depends_on", []):
        prior = ledger.get("steps", {}).get(dep, {})
        if prior.get("status") != "complete":
            raise RuntimeError(f"dependency {dep!r} is not complete")
        dependencies[dep] = prior.get("output_digest", "")
    # Resume is invalidated by production-code changes even when a step's data
    # inputs and command text are unchanged. Otherwise a repaired adapter could
    # be silently skipped behind an old green step cursor.
    body = {"argv": step.get("argv", []), "inputs": inputs,
            "dependencies": dependencies, "code_snapshot": _code_snapshot()}
    digest = hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"digest": digest, **body}


def _outputs_digest(outputs: list[dict]) -> str:
    return hashlib.sha256(json.dumps(
        outputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _load_plan() -> tuple[dict, str]:
    if not RUN_PLAN.is_file():
        raise FileNotFoundError(f"no run plan at {RUN_PLAN}")
    body = RUN_PLAN.read_bytes()
    return json.loads(body.decode("utf-8")), hashlib.sha256(body).hexdigest()


def _verify_source_cache_index() -> dict:
    """Verify the complete frozen cache once, before a replay can write.

    The index is a URL-key map, while objects are content addressed.  We hash
    each unique object once and validate every URL mapping against it.  This is
    deliberately a replay preflight boundary: a missing uncaptured request may
    still become an honest per-unit offline miss, but a declared cached object
    can never disappear or change and be mistaken for a source result.
    """
    index_path = SOURCE_CACHE / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"source cache index cannot be read: {exc}") from exc
    if not isinstance(index, dict) or not index:
        raise ValueError("source cache index is empty or not an object")
    verified: dict[str, dict] = {}
    for cache_key, entry in sorted(index.items()):
        if not re.fullmatch(r"[0-9a-f]{64}", cache_key):
            raise ValueError(f"invalid cache URL key: {cache_key!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"cache entry {cache_key} is not an object")
        digest = str(entry.get("content_hash", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"cache entry {cache_key} has invalid content hash")
        expected_rel = pathlib.Path("objects") / digest[:2] / digest
        if pathlib.PurePosixPath(str(entry.get("cache_path", ""))) \
                != pathlib.PurePosixPath(expected_rel.as_posix()):
            raise ValueError(f"cache entry {cache_key} has non-canonical object path")
        path = SOURCE_CACHE / expected_rel
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"cache object is missing or is a symlink: {expected_rel}")
        size = path.stat().st_size
        if size != int(entry.get("byte_size", -1)):
            raise ValueError(f"cache object size mismatch: {expected_rel}")
        if digest not in verified:
            got = _sha256_file(path)
            if got != digest:
                raise ValueError(f"cache object hash mismatch: {expected_rel}")
            verified[digest] = {"bytes": size}
    total = sum(v["bytes"] for v in verified.values())
    return {"url_entries": len(index), "unique_objects": len(verified),
            "unique_bytes": total, "index_sha256": _sha256_file(index_path)}

def cmd_plan(ctx, args) -> int:
    """Validate the declared replay plan, and print the exact command sequence.

    A20 in one command. The plan names everything a reproduction needs and this
    proves each piece is actually present before a replay starts, so missing
    setup is explicit and fatal rather than an empty table discovered afterwards.
    """
    try:
        plan, plan_hash = _load_plan()
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read run plan {RUN_PLAN}: {exc}", file=sys.stderr)
        return 2
    problems: list[str] = []

    for decl in plan.get("required_inputs", []):
        path = _plan_path(decl["path"])
        if not path.exists():
            problems.append(f"MISSING required input: {decl['path']} -- {decl.get('why', '')}")
            continue
        if path.is_file() and not decl.get("sha256"):
            problems.append(f"MISSING declared checksum: {decl['path']}")
        elif decl.get("sha256"):
            got = _sha256_file(path)
            if got != decl["sha256"]:
                problems.append(f"CHECKSUM MISMATCH {decl['path']}: declared "
                                f"{decl['sha256'][:16]}, found {got[:16]}")
    for decl in plan.get("optional_inputs", []):
        if not _plan_path(decl["path"]).exists():
            print(f"  optional, absent (recorded, not fabricated): {decl['path']}"
                  f" -- {decl.get('why', '')}")

    try:
        cache_check = _verify_source_cache_index()
        print("  source cache: "
              f"{cache_check['url_entries']:,} URL entries, "
              f"{cache_check['unique_objects']:,} unique hash-verified objects, "
              f"{cache_check['unique_bytes']:,} bytes")
    except (OSError, ValueError) as exc:
        problems.append(f"SOURCE CACHE INVALID: {exc}")

    try:
        taxonomy_pins = _verify_taxonomy_pin_inputs()
        print(f"  taxonomy pins: {len(taxonomy_pins)} form/year routes match this cache")
    except (OSError, ValueError, MissingRequiredInput) as exc:
        problems.append(f"TAXONOMY PINS INVALID: {exc}")

    seen_steps: set[str] = set()
    for i, step in enumerate(plan.get("steps", []), 1):
        sid = step.get("id", "")
        if not sid or sid in seen_steps:
            problems.append(f"step {i} has a missing or duplicate id: {sid!r}")
        for dep in step.get("depends_on", []):
            if dep not in seen_steps:
                problems.append(f"step {sid or i} depends on unknown/later step {dep!r}")
        seen_steps.add(sid)
        if not step.get("outputs"):
            problems.append(f"step {sid or i} declares no verifiable outputs")
        for decl in step.get("inputs", []):
            if not isinstance(decl, (str, dict)):
                problems.append(f"step {sid or i} has invalid input declaration {decl!r}")

    try:
        rows, manifest = _load_annotation_bundle(strict=True)
        print(f"  annotations: {len(rows)} rows, set version "
              f"{manifest.get('annotation_set_version')}")
    except SystemExit as exc:
        problems.append(str(exc).splitlines()[0])

    print(f"\nrun plan `{plan.get('plan_id')}` version {plan.get('plan_version')}")
    print(f"  plan SHA-256      : {plan_hash}")
    print(f"  as-of capture date : {plan.get('as_of')}")
    print(f"  cache-only         : {plan.get('cache_only')}")
    print(f"  declared windows   : "
          f"{', '.join(str(w['label']) for w in plan.get('windows', []))}")
    print("\nsteps:")
    for i, step in enumerate(plan.get("steps", []), 1):
        print(f"  {i:2d}. [{step.get('id', '?')}] {' '.join(step['argv'])}")
        if step.get("depends_on"):
            print(f"      depends on: {', '.join(step['depends_on'])}")
        if step.get("why"):
            print(f"      # {step['why']}")

    if problems:
        print(f"\n{len(problems)} problem(s) -- this plan cannot be replayed as declared:",
              file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 2
    print("\nevery declared required input is present and matches its checksum.")
    return 0


# ---------------------------------------------------------------- cli

def cmd_replay(ctx, args) -> int:
    """Execute the declared plan, in order, cache-only, stopping at the first
    unmet prerequisite. Nothing is implicit: the plan states the windows, the
    as-of date, the required inputs and the build steps, and this runs exactly
    those (audit A20)."""
    if cmd_plan(ctx, args) != 0:
        return 2
    plan, plan_hash = _load_plan()
    status_path = VERIFICATION / "replay_step_status.json"
    ledger = _read_replay_ledger(status_path, plan_hash)
    worst = 0
    for i, step in enumerate(plan.get("steps", []), 1):
        sid = step["id"]
        try:
            input_identity = _step_input_identity(step, ledger)
        except (OSError, RuntimeError, ValueError) as exc:
            ledger["steps"][sid] = {
                "status": "failed_precondition", "step": i,
                "error": f"{type(exc).__name__}: {exc}",
                "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
            _atomic_json(status_path, ledger)
            print(f"replay stopped before step {i} [{sid}]: {exc}", file=sys.stderr)
            return 2

        previous = ledger["steps"].get(sid, {})
        if args.resume_plan and previous.get("status") == "complete" \
                and previous.get("input_digest") == input_identity["digest"]:
            try:
                current_outputs = _step_output_identities(step)
            # Any ordinary validation failure makes the prior output stale and
            # therefore forces the producer to run again.  In particular,
            # SQLite raises OperationalError for a stale/incorrect declared
            # query; letting that escape would strand the durable ledger in
            # ``running`` even though no producer is active.
            except Exception:
                current_outputs = []
            if current_outputs and _outputs_digest(current_outputs) == previous.get("output_digest"):
                print(f"\n=== step {i} [{sid}] resume: verified complete; skipped ===",
                      flush=True)
                continue

        argv = _plan_argv(list(step["argv"]))
        print(f"\n=== step {i} [{sid}]: {' '.join(argv)} ===", flush=True)
        ledger["steps"][sid] = {
            "status": "running", "step": i, "argv": argv,
            "input_digest": input_identity["digest"],
            "input_identity": input_identity,
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        _atomic_json(status_path, ledger)
        command_error = ""
        command_status = ""
        try:
            if argv[:1] == ["run.py"]:
                rc = main(argv[1:])
            else:
                if argv and argv[0] in ("python", "python3"):
                    argv = [sys.executable, *argv[1:]]
                rc = subprocess.run(argv, cwd=str(HERE), check=False).returncode
        except KeyboardInterrupt:
            rc = 130
            command_status = "interrupted"
            command_error = "KeyboardInterrupt during command execution"
        except SystemExit as exc:
            raw_code = int(exc.code) if isinstance(exc.code, int) else 2
            rc = raw_code if raw_code else 2
            command_status = "terminated"
            command_error = f"SystemExit during command execution: {exc.code!r}"
        except Exception as exc:
            rc = 2
            command_status = "failed"
            command_error = f"{type(exc).__name__}: {exc}"
        print(f"=== step {i} [{sid}] exit {rc} ===", flush=True)
        if rc != 0 and not step.get("tolerate_partial"):
            failure = {
                "status": command_status or ("interrupted" if rc == 130 else "failed"),
                "exit_code": rc,
                "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
            if command_error:
                failure["error"] = command_error
            ledger["steps"][sid].update(failure)
            _atomic_json(status_path, ledger)
            print(f"replay stopped: step {i} [{sid}] ({' '.join(argv)}) exited {rc}",
                  file=sys.stderr)
            return rc
        try:
            outputs = _step_output_identities(step)
        except KeyboardInterrupt:
            ledger["steps"][sid].update({
                "status": "interrupted", "exit_code": 130,
                "error": "KeyboardInterrupt during output validation",
                "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
            _atomic_json(status_path, ledger)
            print(f"replay interrupted after step {i} [{sid}] before output validation "
                  "completed", file=sys.stderr)
            return 130
        except SystemExit as exc:
            raw_code = exc.code if isinstance(exc.code, int) else 1
            exit_code = raw_code if raw_code else 2
            ledger["steps"][sid].update({
                "status": "terminated", "exit_code": exit_code,
                "error": f"SystemExit during output validation: {exc.code!r}",
                "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
            _atomic_json(status_path, ledger)
            print(f"replay terminated after step {i} [{sid}] before output validation "
                  "completed", file=sys.stderr)
            return exit_code
        # Persist every ordinary output-validation failure.  Cancellation is
        # intentionally excluded because KeyboardInterrupt/SystemExit do not
        # derive from Exception and must retain their signal semantics.
        except Exception as exc:
            ledger["steps"][sid].update({
                "status": "failed_output_validation", "exit_code": rc,
                "error": f"{type(exc).__name__}: {exc}",
                "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
            _atomic_json(status_path, ledger)
            print(f"replay stopped: step {i} [{sid}] output validation failed: {exc}",
                  file=sys.stderr)
            return 2
        ledger["steps"][sid].update({
            "status": "complete", "exit_code": rc, "outputs": outputs,
            "output_digest": _outputs_digest(outputs),
            "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
        _atomic_json(status_path, ledger)
        worst = max(worst, rc)
    return worst


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["universe", "discover", "backfill", "refresh",
                                       "resume", "validate", "coverage", "export",
                                       "status", "registry", "plan", "replay",
                                       "annotations"])
    p.add_argument("--entity", action="append", default=[])
    p.add_argument("--template", action="append", default=[])
    p.add_argument("--adapter", action="append", default=[])
    p.add_argument("--year-from", type=int, default=2024)
    p.add_argument("--year-to", type=int, default=2026)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--budget", type=int, default=None)
    p.add_argument("--force", action="store_true",
                   help="re-run even where a checkpoint is resumable")
    p.add_argument("--offline", action="store_true",
                   help="cache-only replay: never open a socket, never need a credential")
    p.add_argument("--as-of", default="",
                   help="the capture date date-sensitive retrieval should treat as today")
    p.add_argument("--built-at", default="",
                   help="fixed ISO timestamp for deterministic generated metadata; "
                        "defaults to the current UTC time")
    p.add_argument("--without-annotations", action="store_true",
                   help="run without the required reviewed annotations; the run is "
                        "recorded as degraded and is not releasable")
    p.add_argument("--check", action="store_true", help="plan: validate only")
    p.add_argument("--resume-plan", action="store_true",
                   help="replay: skip a completed step only after its declared inputs, "
                        "dependencies and output identities are re-verified")
    args = p.parse_args(argv)

    # Filters are bounded and honest: an unknown adapter or a negative window is
    # a mistake, not a silently empty run (audit A01 clause 6).
    unknown = [a for a in args.adapter if a not in ADAPTERS]
    if unknown:
        print(f"unknown adapter(s) {unknown}; known adapters are {ADAPTERS}",
              file=sys.stderr)
        return 2
    if args.year_from > args.year_to:
        print(f"--year-from {args.year_from} is after --year-to {args.year_to}",
              file=sys.stderr)
        return 2
    if args.limit < 0 or (args.budget is not None and args.budget < 0):
        print("--limit and --budget must not be negative", file=sys.stderr)
        return 2

    if args.command == "registry":
        cov.write_csv(HERE / "config" / "metric_registry.csv", to_rows())
        print(f"metric registry: {len(to_rows())} metrics -> config/metric_registry.csv")
        return 0

    # Plan inspection is genuinely read-only. Constructing Context opens the
    # database writer, creates a work directory and performs startup recovery;
    # none of those mutations is acceptable for `plan --check`. Replay also
    # preflights before it creates the first output or staging database.
    if args.command == "plan":
        return cmd_plan(None, args)
    if args.command == "replay":
        return cmd_replay(None, args)

    for d in (EXPORTS, EVIDENCE, VERIFICATION):
        d.mkdir(parents=True, exist_ok=True)
    ctx = Context(args)
    try:
        if args.command in ("universe", "backfill", "refresh", "resume"):
            return cmd_universe(ctx, args)
        return {"discover": cmd_discover, "coverage": cmd_coverage,
                "status": cmd_status, "export": cmd_export,
                "annotations": cmd_annotations,
                "validate": cmd_validate}[args.command](ctx, args)
    except KeyboardInterrupt:
        ctx.staging.finish_run("interrupted", "interrupted by signal")
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        ctx.close()


def cmd_annotations(ctx, args) -> int:
    """Load the required reviewed annotations into this staging database and
    report exactly what was loaded, without running any adapter."""
    n = _seed_annotations(ctx, args)
    rows = ctx.staging.query(
        "SELECT entity_key, filing_id, metric_id, review_status, filed_text, reviewer"
        " FROM reviewed_source_annotations ORDER BY entity_key, filing_id, metric_id")
    for r in rows:
        attributed = "named reviewer" if _looks_attributed(r["reviewer"]) \
            else "UNATTRIBUTED prior annotation"
        print(f"  {r['entity_key']:10s} filing {r['filing_id']:>7s} "
              f"{r['metric_id'][:38]:38s} {r['review_status']:16s} "
              f"filed={r['filed_text'][:12]:12s} {attributed}")
    print(f"\n{n} annotation(s) loaded; {len(rows)} present in this database")
    return 0


def _looks_attributed(reviewer: str) -> bool:
    """Whether an annotation records WHO reviewed it.

    The delivered annotations record a process and a date -- "Transco reference
    package review 2026-09-04", "Form 2-A bounded sample test, 2026-09-07" --
    and no person. That is provenance, not attribution, and it is reported as
    such rather than dressed up as a human sign-off (audit A09)."""
    return bool(reviewer) and "@" in reviewer


if __name__ == "__main__":
    sys.exit(main())
