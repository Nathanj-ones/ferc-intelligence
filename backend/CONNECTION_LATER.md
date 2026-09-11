# Connection later — not executed

No application connection was made in this run. The running frontend's database, WAL, configuration, fixtures, processes and active pointers remain unchanged.

When separately authorised:

1. Select one immutable candidate generation by reading and verifying `publication_receipt.json`; do not select files by modification time.
2. Build a read-only translation layer for the existing `/api/assets-view` and `/api/asset` routes over `exports/frontend_v1/`. Do not point the app directly at `staging/operating_assets.sqlite`.
3. Route assets by stable asset slug and reviewed entity mapping. Do not require a CID: nine in-scope asset rows legitimately have no FERC CID.
4. Preserve every observation's quality, actual scope, registry contract, periods, units and source dates. Resolve full provenance through `source_index.json` and the declared CSV dependencies.
5. Compare only 2–4 distinct subjects with an identical eligible comparison group. Never allocate one filing-entity total across shared asset aliases.
6. Add lazy loading or pagination before serving the largest monolithic histories in a browser; current entity payloads reach about 8 MB and the source index about 10 MB.
7. Keep historical/backfill events out of the current-news feed. `dataStatus: "live"` in the compatibility directory means stored usable data, not live-source freshness; expose the pinned generation/as-of state explicitly.
8. Leave related-project navigation disabled until a reviewed pre-COD/post-COD crosswalk is supplied.
9. Test the adapter against a disposable app configuration, then obtain explicit authorisation before changing any active path or restarting a service. Retain the prior pointer/configuration as the rollback point.

The current static contract is version `1.1.0`; any semantic change should increment it and republish an internally consistent generation before connection.
