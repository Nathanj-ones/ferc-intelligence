# Repair contract — 8 September 2026

Authority: the independent audit of 8 September 2026, `issues.json` A01–A22 and
`exceptions_34.json`. That register is the minimum backlog. This file governs how
six concurrent workstreams and one integrator share the tree without colliding.

## Baseline identity (verified, do not re-derive)

| Thing | Value |
|---|---|
| Audited lite archive | `outputs/operating_assets_all_regimes.zip` sha256 `dfee81e1…33ddc8e1` ✓ matches |
| Audited full archive | `outputs/operating_assets_all_regimes_FULL.zip` sha256 `3847d3d3…559abf83` ✓ matches |
| Delivered tree vs full archive | 3,565 files byte-identical; 0 missing; only additions are the 12 `ALL_REGIMES_AUDIT_BUNDLE_*.md` parts and `build_audit_bundle.py` (produced after the audit snapshot, not code) |
| This workspace vs delivered tree | byte-identical for all code, config, discovery, evidence and tests, at the moment of copy |
| `staging/operating_assets.sqlite` | drifted on 8 Sep and was **restored byte-exact**; see the incident note below. Do not cite the pre-restore on-disk file as audited bytes |

**Conclusion: the audited baseline IS this code.** No intervening-change
reconciliation is owed. Do not spend time re-establishing it.

Environment fact discovered on this machine, which affects release code:
`stat().st_size` on this volume returns a *stale placeholder size* for
cloud-evicted files until the file is actually read. `build_release.py` and
`build_full_bundle.py` both record `stat().st_size` without reading, so their
manifests can carry wrong byte counts. Size must be taken from the bytes read.

### Baseline DB incident, 8 September 12:09 — resolved

The delivered `staging/operating_assets.sqlite` drifted from the audited bytes
(707,723,264 vs 707,710,976; +12,288 from a journal-mode transition rewriting
header and freelist pages). **Content was never affected** — all 29 table counts,
`integrity_check` and `foreign_key_check` matched the audit's own
`evidence/reports/baseline.json` exactly.

Two writes caused it, and the first was the integrator's:

1. I ran `PRAGMA wal_checkpoint(TRUNCATE)` on the baseline while taking the
   initial snapshot. A checkpoint is a **write**. Taking a "safe baseline" by
   writing to the thing you are protecting is self-defeating.
2. w6-acceptance opened it via `ferclib.staging.Staging(...)`, whose
   `__init__` sets `PRAGMA journal_mode=WAL` — also a write.

**Restored** from `operating_assets_all_regimes_FULL.zip` and verified byte-exact:
sha256 `a19f10bd7b1a082c9033a3f75e5e7674940a7542f899b8a76f3931330994ad0a`,
707,710,976 bytes. It is now **mode 444**, so this cannot recur silently.

Two rules follow, both now binding:

- **Open the baseline read-only.** Never through plain `Staging()`, which writes a
  pragma before you do anything — use `Staging(path, readonly=True)`, which w1
  added for this and which issues no pragmas at all.

  Prefer `mode=ro&immutable=1`, but **do not hard-require it**. w1 measured that
  `immutable=1` fails with "disk I/O error" on this WAL-mode baseline whenever
  another process holds a connection to it, which happens routinely with six
  concurrent workstreams. `Staging(readonly=True)` tries immutable first, records
  the outcome in `self.readonly_immutable`, and falls back to plain `mode=ro`
  with a notice; `require_immutable=True` turns the fallback into an error.

  Plain `mode=ro` was verified safe for the file itself: sha256 and the
  707,710,976-byte length are unchanged across open-read-close. It only creates
  `-shm`/`-wal` sidecars beside the database, which are debris rather than
  damage and can be deleted.
- **Never hardlink `staging/`.** A hardlinked database is not a clone: it shares
  an inode, so every write through the "disposable" copy lands on the protected
  original. `work/a13_test/` was created this way by the integrator and did share
  inode 219733899 with the baseline. It has been removed and the link broken.
  `source_cache/` hardlinks remain correct and deliberate — those objects are
  content-addressed and only ever added, never rewritten in place.

## Workspace layout

```
outputs/operating_assets_all_regimes/    DELIVERED BASELINE — READ ONLY (mode 444)
    staging/operating_assets.sqlite      open ONLY as file:...?mode=ro&immutable=1
                                         never via Staging(); never hardlink it
outputs/repair_all_regimes/              THE WORKING TREE. All edits happen here.
    source_cache/                        hardlinked to the baseline cache (shared inodes,
                                         content-addressed, additive only — never delete)
                                         NOTE: the initial link pass was INCOMPLETE (209 of
                                         3,477) because the integrator's setup command timed
                                         out mid-loop against cloud-evicted files, and the
                                         shortfall was not verified. Offline replay fails on
                                         a missing object. Count the links before trusting
                                         the cache; w1's work/w1-runtime/link_cache_objects.py
                                         completes them and can link exactly what a failed
                                         offline run reported missing.
    source_cache/index.json              SHARED MUTABLE STATE — the one file here that is not
                                         content-addressed. It was destroyed once on 8 Sep by
                                         an external writer using a wrong path variable
                                         (adapters/form549d.py source landed on top of it),
                                         which makes every cache read fail and every adapter
                                         believe the cache is empty. Restored from the
                                         baseline. Never write this path from anything but
                                         SourceCache.
    staging/operating_assets.sqlite      canonical repaired DB — INTEGRATOR ONLY
    work/<worker>/                       your scratch: your own disposable DB, fixtures, logs
    reports/<worker>_report.md           your handoff
    requests/<worker>_shared_requests.md your asks for integrator-owned files
```

## Rule 1 — one owner per file

Edit only files you own. If you need a change in an integrator-owned file, write it
to `requests/<you>_shared_requests.md` with the exact patch and why; do not edit it.

| Owner | Files |
|---|---|
| **integrator** | `ferclib/registry.py`, `ferclib/schema.sql`, `ferclib/status.py`, `ferclib/periods.py`, `ferclib/selectors.py`, `ferclib/xbrl_adapter.py`, `ferclib/ecollection.py`, `ferclib/xbrl.py`, `exporters.py`, `adapters/gas_xbrl.py`, `build_release.py`, `build_full_bundle.py`, `migrations/`, all top-level reports |
| **w1-runtime** | `run.py`, `ferclib/staging.py`, `ferclib/ledger.py`, `ferclib/http.py`, `seed_annotations.py`, `seed_universe.py`, `config/annotations/`, `config/run_plan.json` |
| **w2-ioc** | `adapters/ioc.py` |
| **w3-financial** | `adapters/form549d.py`, `adapters/liquids_xbrl.py`, `adapters/oil_index.py` (new) |
| **w4-coverage** | `ferclib/coverage.py`, `ferclib/applicability.py`, `build_field_status.py`, `build_crosswalk.py` |
| **w5-documents** | `adapters/elibrary_docs.py`, `adapters/lng.py`, `adapters/capacity.py`, `ferclib/elibrary.py` |
| **w6-acceptance** | `validate.py`, `run_regressions.py`, `tests/`, `acceptance/` (new) |

`config/metric_registry.csv` is **generated** from `ferclib/registry.py` — never hand-edit.

## Rule 2 — never write the canonical DB

Only the integrator runs `run.py` against `repair_all_regimes/staging/`. Workers
build disposable DBs under `work/<you>/`, e.g.

```bash
cd outputs/repair_all_regimes
FERC_STAGING_DB=work/w2-ioc/test.sqlite python3 run.py universe --adapter ioc --entity C001087
```

If `FERC_STAGING_DB` is not yet honoured by `run.py`, w1-runtime adds it first
(it is required for A01 anyway); until then, copy the tree or point at your own
path explicitly. **Never delete a database to make a test pass** — if a rebuild
needs a fresh DB, create a new file with a new name.

## Rule 3 — data contracts that no workstream may weaken

1. **FERC sources only.** eCollection/XBRL, data.ferc.gov, eLibrary, eTariff,
   official FERC orders and instructions. A company document *filed with FERC* is
   fine. No SEC, no company websites, no PHMSA, no third-party datasets.
2. **Source identity** = source system + filing occurrence + source fact id.
   Shared content hashes deduplicate *bytes*, never *submission identity*.
3. **Scope** — filing entity, physical asset, terminal, train, storage module and
   ownership share are distinct and never interchangeable.
4. **Grain** — annual, discrete quarter, YTD, month, instant and snapshot are
   distinct. The only permitted equivalence remains Q1-YTD ≡ Q1.
5. **Five independent status dimensions**: availability, origin, method,
   version_status, validation. None may be collapsed into another.
6. **Never improve a number by weakening its basis.** No shrinking a denominator,
   no dropping a warning, no counting a skip as a pass, no zero-for-unknown, no
   publishing an ambiguous total. Qualified as-filed values stay separate from
   accepted analytical outputs.
7. A parser or retrieval failure is **not** legitimate non-applicability. Absence
   of a document is **not** proof of non-compliance.
8. **Do not fabricate.** If FERC evidence does not support a value, the correct
   outcome is an explicit gate, not a number. "Correctly gated because the
   evidence is unresolved" is a legitimate, complete outcome.

## Rule 4 — acceptance means bad states are *rejected*

Every fix ships with a **negative** test that fails when the defect is
reintroduced. A test that passes because the mutated record became acceptable is
a failed test. Synthetic fixtures must be labelled, isolated, and excluded from
production sources and coverage.

## Rule 5 — no network side effects

No deployment, no scheduled refresh, no investor-feed publication, no Slack/Make,
no remote push, no paid resources. Read-only official FERC retrieval is permitted
and encouraged where it closes a blocker. Never bypass authentication, CAPTCHA,
CEII or any access control. Never print an API key or write a credential-bearing
URL into any export.

## Rule 6 — handoff format

Write `reports/<you>_report.md` containing, per issue ID you own:

- **Reproduced?** what you ran, what you observed, against which snapshot.
  A finding may only be disputed with reproducible counterevidence against the
  correct baseline — not an assertion and not a pre-existing passing test.
- **Root cause** in one or two sentences.
- **Files changed** and what changed in each.
- **Data needing regeneration** by the integrator (entities, adapters, windows).
- **Tests written**, the exact command, and the actual result — including
  failures. Do not hide a failing test.
- **Remaining gates** and why the evidence does not permit closing them.

Also append machine-readable rows to `reports/<you>_issues.json`:
`{issue_id, reproduced, root_cause, files_changed[], data_repair, tests[],
final_disposition, outstanding}`.

Report honestly. A gated outcome reported accurately is worth more here than a
closed one that overstates the evidence.
