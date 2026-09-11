# Integrated repair baseline

Recorded: 2026-09-09 (Europe/London)

## Deliberate baseline

- Code/configuration/export baseline: the exact payload extracted from `operating_assets_all_regimes_REPAIRED.zip` (12,188,872 bytes; SHA-256 `815a412b390a40be51a127d640ce2d1501f60ede4691f70131fc2439dbc49574`).
- Immutable full-release reference: `operating_assets_all_regimes_REPAIRED_FULL.zip` (646,424,761 bytes; audited SHA-256 `2567244938a7645ba1fe09b81486cf3780cd1a769b61b59afee25a6309f382d5`).
- The source tree has no `.git` repository. A new local repository was initialized only in this isolated candidate and the frozen release payload was committed as `83eb6ee` on `codex/integrated-repair-20260909`.
- The four live-tree/release divergences reported by the audit are report/identity/manifest artifacts, not newer core implementation. They were not overlaid onto this baseline.

## Recoverable database baselines

Immediately before copying, both operating-asset SQLite databases had zero-byte WAL files and no open file handle. Copy-on-write snapshots were taken into the parent repair workspace; both passed `PRAGMA quick_check` and `PRAGMA foreign_key_check` in immutable read-only mode.

| Role | Snapshot | Bytes | SHA-256 |
|---|---|---:|---|
| Audited repaired/live state | `../baseline/databases/live_operating_assets_20260909.sqlite` | 774,959,104 | `773fe35600031c461166f89bba860bbcedaee04e16a80029f2d950cde080a4d4` |
| Historical defective state used only as regression evidence | `../baseline/databases/historical_defect_snapshot.sqlite` | 707,710,976 | `a19f10bd7b1a082c9033a3f75e5e7674940a7542f899b8a76f3931330994ad0a` |

The historical database is evidence, not a production input and will not be bundled wholesale. Required historical negative-test populations must be reduced to explicit, hashed evidence extracts.

## Explicit supplemental inputs

The following locally preserved inputs were intentionally imported; their presence does not make them part of the audited frozen release:

| Path | SHA-256 | Purpose |
|---|---|---|
| `inputs/day3/FERC_operating_assets_metric_decision_matrix.csv` | `7a5396f040e053594c0706d58d6b977a4bfca68e3b4d754d56f97e08e17aece3` | Crosswalk source |
| `inputs/day3/FERC_operating_assets_current_vs_target_gap.csv` | `70cd484c0c49a4194092b5f520fe3cf7d87aa90364871d7d40568e65c4386175` | Crosswalk source |
| `evidence/supplemental_w4/recompute.py` | `49a5fb4f70b9a869e32692afc734fed64b250f855964556b2497dcfe3eca15ad` | Audited W4 implementation candidate, review before integration |
| `evidence/supplemental_w4/build_applicability_store.py` | `4a19a893c372b6297b135364b62b0ada77f7a6f4a7196b0175f2c7732c1dff66` | Audited W4 implementation candidate, review before integration |
| `evidence/supplemental_w4/test_coverage_w4.py` | `12ee908f03e58d04486fbaf64ab90c4cbb97a5ceec0c05c5579c2b9b232a6723` | W4 regression evidence |

The final audit registers copied under `evidence/audit_baseline/` remain evidence inputs and are never rewritten.

## Active-writer boundary

No process held either operating-assets database when the snapshots were made. A separate dashboard process held `/Users/nathanjones/Desktop/ferc/data/ferc.db` and its WAL/SHM read-write. That database belongs to a different application boundary and is excluded from this repair. No process was killed and no source/live database was modified.

## Space constraint

The data volume had 3,811,384 KiB available at the start of implementation. Large snapshots use APFS clone semantics where available; full source-cache/database copies are not duplicated casually. Build A and Build B must use distinct versioned output paths and must pass a disk-space preflight before materialization.
