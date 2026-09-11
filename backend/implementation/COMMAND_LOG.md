# Implementation command log

This log records material baseline and implementation commands. Full test/build output is retained under `implementation_logs/` and final external sidecars.

## Baseline

```text
df -k /Users/nathanjones/Desktop/ferc
# available: 3,811,384 KiB; filesystem reported 100% capacity after rounding

lsof <live operating DB> <live operating DB WAL>
# no open handles (lsof exit 1 with no output)

stat -f '%N %z %m %Sm' <live DB> <live WAL>
# live DB: 774,959,104 bytes, mtime 2026-09-08 15:48:17
# WAL: 0 bytes, mtime 2026-09-08 16:21:21

unzip -oq operating_assets_all_regimes_REPAIRED.zip -d baseline/lite_extracted
# exit 0

cp -c <live operating DB> baseline/databases/live_operating_assets_20260909.sqlite
cp -c <historical operating DB> baseline/databases/historical_defect_snapshot.sqlite
# exit 0; sources had zero-byte WALs and no open handles

sqlite3 'file:<snapshot>?mode=ro&immutable=1' 'PRAGMA quick_check; PRAGMA foreign_key_check;'
# `ok` for each snapshot; no foreign-key rows

git init candidate
git checkout -b codex/integrated-repair-20260909
git commit -m 'chore: freeze audited repaired release baseline'
# root commit 83eb6ee; 145 files
```

The first attempted `git init --initial-branch=...` was rejected by the installed older Git (exit 129); the two-command compatible form above succeeded.

## Integrated repair continuation

```text
env PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.14 -B \
  implementation_logs/input_recovery/capture_capacity_20260226_5162.py
# exit 0; one official-FERC GET and one POST; both HTTP 200; exact list and
# 173,287-byte PDF validated; no credential, cookie, proxy, redirect or retry

/opt/homebrew/bin/python3.14 -B tools/import_official_recovery.py \
  --results <relative-results> --hash-index <relative-hash-index> ... --apply
# exit 2 before opening the cache: the immutable evidence index uses absolute
# paths and therefore rejected the relative result identity

/opt/homebrew/bin/python3.14 -B tools/import_official_recovery.py \
  --results <absolute-results> --hash-index <absolute-hash-index> \
  --recovery-root <absolute-recovery-root> --cache-root <candidate-cache> \
  --run-inventory <append-only-cache-import-inventory> \
  --expected-results-sha256 32e0e37e7b24b9079f98794d317dbb42f37d262b2195dcd1c564d6877b3619d6 \
  --expected-hash-index-sha256 998423f51a667b0a1ec5ce96347926537d4cc0bb13eccb69069ac29b100aadee \
  --expected-accession 20260226-5162 --apply
# exit 0; 2 exact responses imported; 0 blockers/conflicts
# resulting source_cache/index.json: 2,019,887 bytes, 3,606 entries,
# SHA-256 bf57edec7ea9476482b3127fdfdcccb1650264620965c5811c4017d876c6d675
```

An accidental `--help` invocation of the legacy top-level
`evidence/repair_records/build_repair_records.py` began regenerating its six
disposable duplicate outputs because that evidence helper has no argument
guard. It was interrupted while reading a cloud placeholder; no implementation
draft, database, cache, input, audit source, or supplied tree was changed.
Attempts to hydrate six duplicate copies were stopped after the targets had
already been truncated. The clean candidate deliberately excludes
`evidence/repair_records/`, the affected production regression now reads the
implementation-owned draft actually consumed by final-record generation, and
the branch source copies are left untouched. This incident is not represented
as a successful repair step.

The final-record boundary was then extended without changing the completed
audit populations: `FINAL_REPAIR_LEDGER` retains 64 audited rows and adds two
explicitly labelled repair-time findings; `FINAL_INPUT_INVENTORY_13` remains 13
rows and a separate one-row additional-input inventory carries accession
`20260226-5162`. Packaging now verifies the final-record receipt, immutable
generation and compatibility copies before payload hashing.

```text
/opt/homebrew/bin/python3.14 -B -m unittest -v <10 focused modules>
/usr/bin/python3 -B -m unittest -v <10 focused modules>
# 89 tests executed, 89 passed on each interpreter

/opt/homebrew/bin/python3.14 -B -m unittest discover -s tests -t . -v
# exit 1 before collection: tests/ is not an importable package; no tests ran

FERC_OFFLINE=1 /opt/homebrew/bin/python3.14 -B -m unittest discover -s tests -v
# 580 executed: 563 pass, 3 fail, 1 setUpClass error, 13 skip
# the error is the deliberately fail-closed Build-A test requiring
# FERC_STAGING_DB; the three failures are the mandatory local Vision path being
# denied by the sandbox. Both are rerun only against Build A with the required
# database and macOS-system access; this preflight is not an acceptance run.
```

After the final test-tool snapshot was expanded, the focused freeze boundary
was rerun from the candidate root with `PYTHONPATH` and `PYTHONHOME` removed:

```text
env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.14 -B -m unittest -v tests.test_build_b_acceptance_runner_codex tests.test_build_test_run_manifest_codex tests.test_capacity_20260226_5162_capture tests.test_capacity_filer_identity_codex tests.test_external_release_evidence_codex tests.test_final_exception_records_hardening_codex tests.test_final_release_records_codex tests.test_import_official_recovery tests.test_release_builder_codex tests.test_runtime_requirements_codex
# exit 0; 89 executed, 89 passed; log 31,787 bytes;
# SHA-256 c40717bfda7b6b748fdc66a5491a8101694c8af7decc2b2c9821e6b74f8d9bb9

env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -B -m unittest -v tests.test_build_b_acceptance_runner_codex tests.test_build_test_run_manifest_codex tests.test_capacity_20260226_5162_capture tests.test_capacity_filer_identity_codex tests.test_external_release_evidence_codex tests.test_final_exception_records_hardening_codex tests.test_final_release_records_codex tests.test_import_official_recovery tests.test_release_builder_codex tests.test_runtime_requirements_codex
# exit 0; 89 executed, 89 passed; log 26,491 bytes;
# SHA-256 9abe8c5982423c0a0746404cad71ca4011e179ccce66d378736dfdfb2c8dbef2
```
