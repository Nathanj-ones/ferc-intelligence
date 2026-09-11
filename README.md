# FERC Intelligence — frontend and backend

One repository for the website, Python ingestion pipeline, reviewed source inputs,
validation, and local live-refresh integration. No other checkout or Desktop folder
is required.

## Quick start

Requirements: Node.js 22.13+ (24 recommended), Python 3.9–3.14, and GitHub access
to this private repository. The frontend works on its checked-in snapshot without
a FERC key. Live pulls require your own `FERC_API_KEY`.

```bash
npm ci
npm run setup:backend
cp .env.example .env
# Set FERC_API_KEY in .env if you want live source pulls.
npm run dev:live
```

Open [localhost:5173](http://localhost:5173/?view=assets). Both the website and
the local refresh endpoint start with that command.

`setup:backend` downloads the pinned starting-data bundle from **this repository's
private GitHub Release**, verifies the archive and every extracted file, checks
SQLite integrity and row counts, then installs it under ignored backend folders.
Install the GitHub CLI and run `gh auth login` first, or download both release assets
into the same folder and run `npm run setup:backend -- --archive /path/to/ferc-backend-data-2026-09-10-v1.zip`.
Allow 8 GB of free disk space for the seed, downloads, and separate working copies.
Setup is idempotent and refuses to overwrite an unrelated database.

For full image-only capacity extraction/replay, the existing backend requires
**macOS, Swift/macOS Vision, and Poppler (`pdftoppm`)**. On macOS, install Poppler
with `brew install poppler` and the Apple command-line tools with `xcode-select --install`
if needed. The website, seeded data verification, and text-based adapters do not
require those OCR tools. Unavailable OCR is reported as a failure, never skipped
as a successful full refresh. See `backend/RUNTIME_REQUIREMENTS.json`.

## Repository layout

- `app/`, `components/`, `lib/`: frontend and verified browser-data loader.
- `backend/`: Python engine, all regime adapters, SQL schema/migrations, reviewed
  inputs/configuration, tests, and original backend contracts.
- `scripts/`: setup, local API, backend commands, and frontend export integration.
- `public/data/ferc/`: the ready-to-view, hash-verified reviewed browser snapshot.
- `backend/bootstrap.json`: pinned release asset identity and database counts.
- `.ferc-local/`: ignored generated working state; never committed or served.

Credentials, caches and SQLite files are not in Git history. The reviewed seed
database and official-source cache are in the checksum-pinned private release;
newly generated databases and refresh results stay local.

## Backend commands

```bash
npm run backend:check       # All declared inputs + complete source cache verified
npm run backend:verify      # Seed -> export -> validation -> frontend, offline
npm run backend:test        # Python unit/regression suite
npm run backend -- status  # Work on a separate CLI database/cache
npm run backend -- refresh --budget 2000
```

The website's **Refresh live data** button orchestrates collection, coverage,
field statuses, export, validation, and frontend activation. The raw backend CLI
is for individual pipeline operations and does not update the website on its own.
The seed is preserved; the button and CLI each use separate working databases.
Before using those copies, the repository applies its idempotent data-only
migrations, including the accession-package document identity correction.
See [local refresh details](LOCAL-LIVE-DATA.md).

The existing private hosted site is a separately published, pinned snapshot.
Pushing to GitHub does not redeploy it or make the Python process run on Sites.

## Checks

```bash
npm test
npm run backend:verify
npx tsc --noEmit
npm run lint
npm run build
```

## Data integration

The operating-asset UI reads a compact, hash-verified browser projection from
`public/data/ferc`. The snapshot is pinned to one backend publication receipt,
and `lib/ferc/backend.ts` verifies both stored-file and decoded-content hashes
before accepting it. Asset details, historical changes, and complete lineage
shards load on demand. The contracted industry-wide oil-index instrument is
published separately from carrier assets and also loads on demand. The source
generation is immutable; frontend presentation fixes can revise its projection.
An open tab detecting revised hashes verifies the new payload then reloads the
same URL, so it never combines old directory eligibility with new detail data.
A bad cached response gets one revalidated retry; persistent corruption fails
closed.

Regenerate the snapshot from a validated backend export with its receipt generation:

```bash
FERC_BACKEND_ROOT=/path/to/export-output FERC_EXPECTED_GENERATION=<receipt-generation-id> npm run sync:ferc
```

The sync fails closed if the generation, frontend contract, dependency hashes,
route closure, or cross-references differ from the pinned release. It does not
read the staging database, raw source cache, credentials, or environment files.
The directory projection also carries compact, receipt-derived comparison
metadata so the picker can reject ambiguous series and selections with no exact
period in common before loading an asset comparison.

Directory cards use the same regime-specific safe selection as asset detail
cards, including the exact point and reporting period. Form 549D
assets with no scalar headline may use the exact filed reporting-state metric;
structured records are never promoted. “Latest available period” is derived
from the latest present, validated occurrence rather than the expected-slot
calendar, so an unfilled future slot cannot make the visible history look newer
than its data.

Asset detail pages lead with up to six regime-specific key metrics before the
complete metric explorer. Quarterly performance cards select only a present,
validated quarter from one resolved scope-and-unit series and show the same
quarter from the prior year when available; they never substitute an annual or
YTD value. Annual, snapshot, and event metrics keep their own period labels, and
ambiguous or structured values fail closed instead of being promoted.

The backend's `summary.review` is retained for contract provenance but is not
presented as a human-review queue. The browser projection separately counts
must-propagate quality flags, explicitly open review tasks, and resolved review
tasks. Unit and scope qualifications stay visible without being mislabeled as
records awaiting a reviewer; only an explicit `review_status=open` is described
as needing review.

Charts collapse duplicate occurrences only when the source fact, filing identity
and every substantive field agree (the observation ID and source-regime label
may differ). Raw records remain available. Conflicting same-period records
prevent a trend or comparison. Shipper-concentration comparison also requires
the same evidenced denominator concept and unit: storage inventory quantity
cannot be compared with daily transport capacity. The directory contract carries
this extra semantic discriminator, while original backend comparison IDs remain
intact in observation evidence.

Raw filed evidence is separate from the stored interpretation. For example,
Form 549D reporting-state records retain both the categorical interpretation
and the underlying structured filing header. Roadrunner's border-crossing
pipeline is labeled by its physical asset type; its backend Section 3/LNG
processing template is unchanged.

To regenerate outside the working snapshot (for validation or to preserve
untracked files), set `FERC_FRONTEND_DATA_ROOT` to a dedicated temporary output
directory. No backend source files are edited by the sync.

Presentation follows the backend's display value and scale without inferring a
conversion. A small set of contract-1.1.0 metric-specific unit shims covers
documented source-tag anomalies (counts, barrel-miles, migrated Page 313 Dth,
and the Form 2 horsepower column). Those shims leave the stored value and raw
unit visible in evidence, never change the numeric magnitude, and fail closed
on warned barrel/barrel-mile conflicts. Certificated horsepower remains excluded
from cross-asset comparison until the backend propagates its source-unit warning
consistently. URLs containing placeholder or redacted credentials are omitted
from the browser projection.

`lib/ferc/data.ts` still contains the separately supplied regulatory-project
fixtures. The backend contract does not currently provide related-project
mappings, so the UI does not infer links between operating assets and projects.
