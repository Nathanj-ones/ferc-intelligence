# FERC Intelligence frontend

Frontend for reviewing source-backed operating-asset results and FERC project developments.

## Run locally

```bash
npm ci
npm run build
npm start
```

Open `http://localhost:8787/`.

## Checks

```bash
npm test
npx tsc --noEmit
npm run lint
npm run build
```

## Data integration

The operating-asset UI reads a compact, immutable browser snapshot from
`public/data/ferc`. The snapshot is pinned to one backend publication receipt,
and `lib/ferc/backend.ts` verifies both stored-file and decoded-content hashes
before accepting it. Asset details, historical changes, and complete lineage
shards load on demand. The contracted industry-wide oil-index instrument is
published separately from carrier assets and also loads on demand.

Regenerate the snapshot from the authoritative backend export with:

```bash
FERC_BACKEND_ROOT=/path/to/operating_assets_all_regimes npm run sync:ferc
```

The sync fails closed if the generation, frontend contract, dependency hashes,
route closure, or cross-references differ from the pinned release. It does not
read the staging database, raw source cache, credentials, or environment files.
The directory projection also carries compact, receipt-derived comparison
metadata so the picker can reject ambiguous series and selections with no exact
period in common before loading an asset comparison.

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
