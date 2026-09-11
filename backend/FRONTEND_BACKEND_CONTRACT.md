# Frontend-to-backend contract

Status: standalone contract only. The frontend and running application are unchanged and are not connected to this candidate.

The frontend was inspected read-only at `/Users/nathanjones/Desktop/ferc`. Its implemented backend routes are `/api/assets-view` and `/api/asset`; its chart primitives exist, but the user-selected, up-to-four comparison workflow and richer source/derivation views are not implemented. The candidate publishes a versioned static contract under `exports/frontend_v1/`. It is suitable for a later read-only route adapter, not as a byte-for-byte replacement for either live route.

| Frontend requirement | Existing frontend surface | Candidate field/output | Disposition |
|---|---|---|---|
| Company and asset directory | `AssetView` aliases: ID, ticker, display name, CID | `assets.json`: 120 stable asset slugs, 109 in-scope assets, reviewed company interests, 104 mapped filing entities, explicit exclusions | Mapped through a later adapter |
| Stable filer and asset identity | Primarily asset ID/CID | Asset slug plus `entityMappings[].entity_key`; safe entity filenames; 9 in-scope assets legitimately use reviewed `NO-FERC-CID:` keys | Mapped; never invent a CID |
| Single-asset history | `/api/asset`, CID-level `factsSeries` | `asset_payloads/<asset>.json` resolves to `entity_payloads/<entity>.json`, with complete observations, events and annotations | Mapped with an entity-scope gate |
| Up to four same-type comparisons | Chart primitives; no selection route/cap | Exact `comparison.group_id`, stable series ID, base-unit value and 2–4 distinct-subject validation | Data contract ready; UI/route not implemented |
| Period ranges and units | Period/year/unit, with some presentation inference | Exact basis/start/end/instant, reporting year/period, filed unit, declared display unit/scale and dimensional base value | Mapped; future adapter must not infer missing units |
| Regulatory scope | Not represented fully | `scope.actual`, `scope.contract_rule`, `scope.resolved`; filing-entity and facility/subset context remain distinct | New adapter/UI fields required |
| Missing, review and version state | Not represented fully | Availability, validation, version, origin, method, QA flags, review status, missing reason and applicability evidence travel with every observation | New adapter/UI fields required |
| Changes | Company-filtered filing feed | Entity events retain event class/type, destination, backfill flag, relevant dates and source identity | Mapped; adapter required |
| Source links and dates | Mainly accession links | Filing/fact/document identity; reporting, filed, posted, issued, effective, retrieved and first-seen dates; `source_index.json` | Source-detail adapter required |
| Derivation inputs | Not implemented | Inline bounded lineage plus complete `lineage_edges.csv` and `lineage_populations.csv` | New source-detail component required |
| Related projects | Group/entity/docket affordances | Related asset context is retained; every project mapping is explicitly `not_supplied` | Genuine gap; do not relabel an asset or docket as a project |
| Oil Pipeline Index | No dedicated instrument route | `instruments.json` keeps the industry-wide index separate from carrier assets | Optional new adapter surface |

## Consumer invariants

- Filter company groups by reviewed `ticker`, not the free-text parent field. The exported groups are KMI, LNG, OKE, TRGP and WMB; TC Energy appears in ownership/entity context, while NEXT and National Grid are not separate operating-roster groups in this reviewed universe.
- Compare only observations that are eligible and have an identical comparison group. The group fixes template, metric, period basis, unit family and registry scope contract. The series also fixes the filing-entity subject and actual scope.
- Ten asset aliases share five filing entities. Their entity totals are useful context but may not be allocated to, or compared as, independent assets.
- A quality object must be displayed with every value. Open review, unresolved scope, wrong/unknown units and superseded versions are not comparison-eligible.
- Preserve source system, occurrence and fact/document identity together. Do not reduce provenance to a generic URL.
- Keep reporting, filing, effective, retrieval and first-seen dates distinct.
- `dataStatus: "live"` is a compatibility label for usable stored data, not a claim of live-source freshness. Freshness must come from the dated source and publication fields.
- Historical/backfill events are not current news. This generation intentionally has zero current `investor_feed` events.
- The publication boundary is the external `publication_receipt.json`, generation `0dccbd426f15372f1330737537f9aac58bc9547a2eaf81ef6f4b655f10cf2824`. Payload `run_id` is blank and must not be used as a generation identifier.

## Practical limits

- 88 asset rows have at least one comparison-eligible series. The other 32 are explicitly gated: 11 out-of-template, 10 shared-filer aliases and 11 with no eligible series.
- Entity histories are monolithic compatibility payloads; the largest are about 8 MB. `source_index.json` is about 10 MB. Lazy loading, pagination or sharding is a later performance task.
- Rich comparison selection, scope/quality presentation, derivation inspection and related-project navigation remain frontend work.
- Contract testing in this run is detached schema, route-closure, referential and representative-payload testing. No live frontend, API or end-to-end application test was run.

The complete machine-readable contract is `exports/frontend_v1/contract.json`.
