# FERC all-regime operating-asset staging pipeline

> Monorepo setup and operation: follow the root README. Use `npm run setup:backend`
> and `npm run backend -- <command>` for isolated working state. The original
> engine-level documentation below is preserved for reference; the raw commands
> use the backend's seed locations unless environment overrides are supplied.

One reusable pipeline that retrieves, parses, selects, validates and stages
FERC-only operating data for every asset type in the revised standard template:
interstate gas, non-major Form 2-A gas, gas storage, oil/refined-products/NGL
carriers, intrastate NGPA §311/Hinshaw reported services, and operating LNG
facilities.

**This is a staging implementation.** It does not deploy, does not touch the live
database or dashboard, and does not claim a finished website or investment
analytics.

## Run it

The release replay is the authoritative build interface. It preflights every
declared input—including the complete content-addressed cache—before it opens an
output database, then executes the dependency-ordered plan from
`config/run_plan.json`:

```bash
export FERC_STAGING_DB=/absolute/disposable/output/staging/operating_assets.sqlite
export FERC_OUTPUT_DIR=/absolute/disposable/output
export FERC_SOURCE_CACHE=/absolute/extracted/release/source_cache
export FERC_LEDGER=/absolute/disposable/output/staging/build.task_ledger.json

python3 run.py plan --check
python3 run.py replay
```

Replay is cache-only and credential-free. The declared full plan includes the
2024–2026 universe, the required 2016–2023 Transco/TGP history, reviewed
annotations, crosswalk generation, coverage, field readiness, one coherent
consumer publication, and integrated validation. The two image-only capacity
filings additionally require the external executables declared in
`RUNTIME_REQUIREMENTS.json`: Poppler `pdftoppm` and `/usr/bin/swift` with macOS
Vision. Their OCR output is accepted only when it agrees with the independent
hash-bound manual review.

Incremental refresh, resuming from checkpoints:

```bash
python3 run.py refresh
```

Filters compose on every command:

```bash
python3 run.py universe --template liquids --year-from 2024 --year-to 2026
python3 run.py universe --entity C000654 --adapter gas_xbrl
python3 run.py universe --adapter form549d --budget 200
python3 run.py status               # ledger, staging counts, open blockers
```

| Flag | Effect |
|---|---|
| `--entity CID` | repeatable; restrict to these filing entities |
| `--template` | `interstate_gas`, `gas_storage`, `liquids`, `intrastate_549d`, `lng` |
| `--adapter` | `gas_xbrl`, `liquids_xbrl`, `ioc`, `capacity`, `form549d`, `elibrary_docs`, `lng` |
| `--year-from` / `--year-to` | reporting-period window (default 2024–2026) |
| `--limit N` | first N filing entities |
| `--force` | ignore checkpoints and re-do completed work |
| `--budget N` | hard cap on HTTP requests for the run |

## Layout

```
ferclib/          shared engine -- schema, HTTP+cache, staging writer, XBRL parser,
                  applicability, registry, selectors, period grain, status, coverage
adapters/         one module per source regime, all against ADAPTER_CONTRACT.md
config/           frozen universe, metric registry, requirements crosswalk
staging/          operating_assets.sqlite -- the staged output
source_cache/     content-addressed source bytes, shared and immutable
exports/          consumer-facing CSVs
evidence/         discovery inventories
discovery/        live source research that the adapters were built from
verification/     regression, validation and integrity results
```

## The four things this pipeline is careful about

**1. Blank is not zero, and absent is not unavailable.** Thirteen distinct
availability states (`ferclib/status.py`). A source blank, a filed nil, a
schedule the form does not collect, a non-public document, a retrieval failure
and *our own unimplemented adapter* are six different things and never collapse
into "not available".

**2. Period grain is part of identity.** A year-to-date fact can never satisfy a
requested quarter. The only permitted equivalence is Q1, whose YTD interval
genuinely *is* the quarter. A blank filed quarter and a validly derived quarter
are separate observations that coexist — the derived one never overwrites the
evidence that the filed one was blank.

**3. Five independent status dimensions.** availability / origin / method /
version / validation. A value can be migrated + derived + revised + valid-with-a-
unit-warning at once. `exporters.py` refuses to write a flagged value whose flag
has been stripped, and proves it with a negative control.

**4. Applicability comes from FERC's own taxonomy, per version.** Whether a form
collects a concept is read from that form's entry point and presentation
linkbases for the exact taxonomy version the filing used. An incomplete taxonomy
yields `APPLICABILITY_UNKNOWN`, never `NOT_REQUIRED`.

## Reproducing the evidence

The versioned **full** ZIP contains the code, registry/configuration, immutable
captured source objects, candidate database, annotations, exports, validation,
final issue/exception/input records and machine-readable run plan needed for the
declared offline replay. Build acceptance keeps that clean extraction byte-for-
byte unchanged and writes the rebuilt database and generated outputs to a
separate, initially empty work root, without access to the live tree, worker
scratch, credentials or the network. The **lite** ZIP deliberately omits
`source_cache/`, `staging/` and other large work data and is not a standalone
rebuild package; its own manifest and receipt state those exclusions.

Every archive has its own embedded payload manifest. The archive's complete
hash/size and post-package Build-B result are issued afterwards as external,
hash-indexed receipts so writing the receipt cannot change the archive it
identifies.

## What this does not do

- No deployment, no live-database write, no dashboard change, no commits.
- No SEC, company-website, PHMSA or third-party operating data. FERC only — a
  company document filed *with* FERC is a FERC source.
- No FPA/electric rows. They are retained in the universe as `OUT_OF_TEMPLATE`
  for reconciliation and are never described as having no FERC data.
- No fabricated values to satisfy "all metrics". A gated metric stays gated with
  the exact evidence that would ungate it.
