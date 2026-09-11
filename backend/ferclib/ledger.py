"""
The resumable task ledger.

Resume state, not a substitute for implementation: a context limit, a crash or
an interrupted download must not lose completed work or restart a successful
backfill. Every task carries owner, dependency, code location, source coverage,
test result, blocker and next action, so a fresh process can pick the run up
exactly where it stopped.

CHECKPOINT IDENTITY (audit A01 clause 1)
----------------------------------------
"Done" is not a fact about a task name, it is a fact about a task name *under a
particular set of conditions*. A completed checkpoint may only suppress work
when every one of those conditions still holds:

    entity + adapter + window (year_from..year_to)
    + code version + registry version
    + a digest of the adapter module and the frozen configuration
    + the input state -- the set of filing occurrences the source presented

The first group is the RESUME identity: it is known before any retrieval, and
it is what `resumable()` compares. Change the code, the registry, the config or
the window and every stored checkpoint stops matching, so stale completions
cannot survive a change that would have altered their result.

The input digest is the FRESHNESS identity: it is only known after the source
has actually been consulted, so it can never be used to decide whether to
consult the source. It answers a different question -- "did anything change?" --
and it is what makes a no-change refresh idempotent without ever letting a
completed checkpoint hide a new filing.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import threading

PENDING, RUNNING, DONE, FAILED, BLOCKED, SKIPPED = (
    "pending", "running", "done", "failed", "blocked", "skipped")

#: the identity fields knowable BEFORE the source is consulted
RESUME_IDENTITY_FIELDS = ("adapter", "entity_key", "year_from", "year_to",
                          "code_version", "registry_version", "config_digest",
                          "adapter_digest")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def identity_digest(identity: dict) -> str:
    """Stable digest of the resume identity. Any field changing changes this."""
    payload = {k: identity.get(k) for k in RESUME_IDENTITY_FIELDS}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:32]


def input_digest(filings) -> str:
    """Digest of the filing occurrences a retrieval presented.

    Deliberately over the SUBMISSION IDENTITY of each occurrence, not over
    content hashes alone: rule 3 clause 2 says a shared content hash
    deduplicates bytes, never submission identity, so two byte-identical
    resubmissions are two occurrences here and a refresh sees them both.
    """
    keys = ("source_system", "filing_id", "accession_number", "content_hash",
            "submitted_on", "form", "reporting_year", "reporting_period",
            "snapshot_date", "version_status", "is_canonical")
    rows = []
    for f in filings or []:
        if not isinstance(f, dict):
            rows.append(repr(f))
            continue
        rows.append({k: f[k] for k in keys if k in f})
    return hashlib.sha256(
        json.dumps(sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, default=str)),
                   sort_keys=True, default=str).encode("utf-8")).hexdigest()[:32]


class Ledger:
    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        self._lock = threading.Lock()
        self.data: dict = {"created_at": now(), "updated_at": now(), "tasks": {}}
        if self.path.is_file():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        self.data.setdefault("tasks", {})

    def add(self, task_id: str, *, owner: str, milestone: str, description: str,
            depends_on: list[str] | None = None, code: str = "",
            next_action: str = "") -> None:
        with self._lock:
            t = self.data["tasks"].setdefault(task_id, {})
            if t.get("state") in (DONE, SKIPPED):
                return                       # completed work is never reset
            t.update({"task_id": task_id, "owner": owner, "milestone": milestone,
                      "description": description, "depends_on": depends_on or [],
                      "code": code, "state": t.get("state", PENDING),
                      "source_coverage": t.get("source_coverage", ""),
                      "test_result": t.get("test_result", ""),
                      "blocker": t.get("blocker", ""),
                      "next_action": next_action or t.get("next_action", ""),
                      "updated_at": now()})
            self._flush()

    def set(self, task_id: str, state: str, **fields) -> None:
        with self._lock:
            t = self.data["tasks"].setdefault(task_id, {"task_id": task_id})
            t["state"] = state
            t.update({k: v for k, v in fields.items() if v is not None})
            t["updated_at"] = now()
            self._flush()

    def mark_done(self, task_id: str, *, identity: dict, input_digest: str,
                  run_id: str = "", **fields) -> None:
        """Complete a unit of work and stamp what made it complete.

        `last_success_at` / `last_good_run_id` are the freshness anchors: a
        later failed refresh sets freshness=stale but leaves these alone, so the
        honest answer to "when was this last confirmed against the source" is
        always available and is never the time of the failure.
        """
        with self._lock:
            t = self.data["tasks"].setdefault(task_id, {"task_id": task_id})
            t["state"] = DONE
            t["identity"] = dict(identity)
            t["identity_digest"] = identity_digest(identity)
            t["input_digest"] = input_digest
            t["last_success_at"] = now()
            t["last_good_run_id"] = run_id
            t["freshness"] = "current"
            t["stale_reason"] = ""
            t["stale_since"] = ""
            t["blocker"] = ""
            t.update({k: v for k, v in fields.items() if v is not None})
            t["updated_at"] = now()
            self._flush()

    def state(self, task_id: str) -> str:
        return self.data["tasks"].get(task_id, {}).get("state", PENDING)

    def is_done(self, task_id: str) -> bool:
        return self.state(task_id) == DONE

    # --------------------------------------------------------- identity

    def record_identity(self, task_id: str, identity: dict) -> None:
        """Stamp the conditions this task is about to run under."""
        with self._lock:
            t = self.data["tasks"].setdefault(task_id, {"task_id": task_id})
            t["identity"] = dict(identity)
            t["identity_digest"] = identity_digest(identity)
            t["updated_at"] = now()
            self._flush()

    def stored_identity(self, task_id: str) -> dict:
        return dict(self.data["tasks"].get(task_id, {}).get("identity") or {})

    def resumable(self, task_id: str, identity: dict) -> tuple[bool, str]:
        """(may this completed task be skipped, why not).

        Only ever consulted in `resume` mode. A task is resumable when it
        completed AND the stored resume identity is byte-for-byte the identity
        we are about to run under. Anything else -- a new window, a new registry
        version, edited adapter code, edited config, or a checkpoint written
        before identities were recorded -- is not resumable, and the correct
        answer is to do the work rather than to trust a stale 'done'.
        """
        t = self.data["tasks"].get(task_id) or {}
        if t.get("state") != DONE:
            return False, f"state={t.get('state', PENDING)}"
        stored = t.get("identity_digest")
        if not stored:
            return False, "checkpoint carries no recorded identity"
        want = identity_digest(identity)
        if stored != want:
            old = t.get("identity") or {}
            changed = [k for k in RESUME_IDENTITY_FIELDS
                       if old.get(k) != identity.get(k)]
            return False, "identity changed: " + ",".join(changed or ["unknown"])
        return True, ""

    def success_digest(self, task_id: str) -> str:
        """The input digest the last SUCCESSFUL run of this task committed."""
        return (self.data["tasks"].get(task_id) or {}).get("input_digest") or ""

    def unchanged_since_last_success(self, task_id: str, digest: str,
                                     *, was_done: bool | None = None) -> bool:
        """True when the source presented exactly the occurrences that the last
        successful run of this task already committed.

        Only meaningful AFTER retrieval, and never used to decide whether to
        retrieve. `was_done` must be sampled BEFORE the task is marked running,
        because the runner sets it running the moment it starts work -- reading
        the live state here would always see `running` and the shortcut would
        never fire. It defaults to reading the state so an out-of-tree caller
        still gets the safe answer.

        A previously FAILED attempt is never 'unchanged': the last thing that
        happened to this unit was not a success, so the work must be redone even
        if the source looks the same.
        """
        t = self.data["tasks"].get(task_id) or {}
        done = t.get("state") == DONE if was_done is None else bool(was_done)
        return bool(digest) and done and t.get("input_digest") == digest

    def mark_stale(self, task_id: str, reason: str) -> None:
        """A refresh that failed leaves the stored rows as LAST GOOD, not fresh.

        Recorded explicitly so a consumer asking "how current is this?" is told
        the truth: the newest successful observation of the source is the one
        stamped in `last_success_at`, not the wall clock of the failed run.
        """
        with self._lock:
            t = self.data["tasks"].setdefault(task_id, {"task_id": task_id})
            t["freshness"] = "stale"
            t["stale_reason"] = reason[:300]
            t["stale_since"] = now()
            t["updated_at"] = now()
            self._flush()

    def freshness(self, task_id: str) -> dict:
        t = self.data["tasks"].get(task_id) or {}
        return {"task_id": task_id, "state": t.get("state", PENDING),
                "freshness": t.get("freshness", "unknown"),
                "last_success_at": t.get("last_success_at", ""),
                "last_good_run_id": t.get("last_good_run_id", ""),
                "stale_since": t.get("stale_since", ""),
                "stale_reason": t.get("stale_reason", "")}

    def stale_tasks(self) -> list[dict]:
        return [self.freshness(k) for k, t in self.data["tasks"].items()
                if t.get("freshness") == "stale"]

    def ready(self) -> list[dict]:
        """Tasks whose dependencies are all done."""
        out = []
        for t in self.data["tasks"].values():
            if t.get("state") not in (PENDING, FAILED):
                continue
            if all(self.is_done(d) for d in t.get("depends_on", [])):
                out.append(t)
        return out

    def by_state(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in self.data["tasks"].values():
            counts[t.get("state", PENDING)] = counts.get(t.get("state", PENDING), 0) + 1
        return counts

    def blockers(self) -> list[dict]:
        return [t for t in self.data["tasks"].values() if t.get("blocker")]

    def _flush(self) -> None:
        self.data["updated_at"] = now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def markdown(self) -> str:
        rows = ["| Task | Milestone | Owner | State | Source coverage | Tests | Blocker | Next |",
                "|---|---|---|---|---|---|---|---|"]
        for t in sorted(self.data["tasks"].values(),
                        key=lambda x: (x.get("milestone", ""), x.get("task_id", ""))):
            rows.append("| {task_id} | {milestone} | {owner} | {state} | {source_coverage} | "
                        "{test_result} | {blocker} | {next_action} |".format(
                            **{k: str(t.get(k, ""))[:90] for k in
                               ("task_id", "milestone", "owner", "state", "source_coverage",
                                "test_result", "blocker", "next_action")}))
        return "\n".join(rows)
