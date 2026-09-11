# Operating-assets standalone data candidate

## Result

This directory is the completed standalone backend candidate. It is useful for operating-asset directories, histories, same-type comparisons, change/source inspection and explicit data-quality states. It is **not connected to the frontend or running application**.

| Identity | Value |
|---|---|
| Candidate root | `/Users/nathanjones/Desktop/ferc_reaudit_handoff/operating_assets_backend_ready_20260910/operating_assets_all_regimes` |
| Publication generation | `0dccbd426f15372f1330737537f9aac58bc9547a2eaf81ef6f4b655f10cf2824` |
| Code / registry | `0.37.4` / `all-regime-2026-09-09-integrated2` |
| Data as of | `2026-09-07` |
| Database | `staging/operating_assets.sqlite`; 814,530,560 bytes; SHA-256 `7f7b5e17570003eac67e1cfcceee2152d335d883cd57cc68a35cd196e908cfa8` |
| Publication set | 257 receipt-bound files: 27 base exports plus 230 frontend-contract files |
| Frontend contract | `ferc_operating_assets_frontend_v1` version `1.1.0` |

The R6 starting archive was verified once at 836,955,442 bytes and SHA-256 `d28b8ff9ab117677112339a05f49cf11aab6c166bc96661efc85a17769ec92f3`. The candidate was moved to this fully materialised Desktop handoff location after the Documents workspace exhibited macOS File Provider eviction. The original archive, audits, frontend tree and live application database were not changed.

## What changed after R6

- Corrected the dominant coverage matcher defect. Expected slots carry the independent registry scope contract, while observations carry source-backed actual scope. Matching now uses `scope_rule` and separately requires a resolved, supported actual scope. It does not replace actual scope with a registry instruction or admit arbitrary narrower facility labels.
- Added a detached frontend export contract with stable asset/entity identities, exact periods and units, comparison IDs and gates, quality/review state, events, source links, distinct dates and complete lineage dependencies.
- Added generation-level route and dependency closure. Publication fails if a required file, dynamic asset/entity payload, source index, compatibility export or database/generation identity is absent, stale or inconsistent.
- Prevented entity-summary fan-out where several asset aliases share one filing entity.
- Preserved source occurrence identity and exact actual/document scope through the migration and exports. The migration is idempotent and changed no actual-scope bytes.
- Recovered and independently validated the immutable captured source cache: 3,620 URL entries, 3,587 objects and 1,198,822,748 logical bytes; no missing, mismatched or unreferenced object.
- Applied two captured eLibrary accessions where their entity assignment was supported, while keeping tariff interpretation gated; rejected the third because the official filing names a different company.
- Kept the fixed 166-row audited field crosswalk reconciled to 168 current field rows. Of those, 142 are locally validated, 14 interpretation-gated, 10 blank/not-required and 2 retrieval/access failures; all 168 have an implemented adapter.

## Stored population

| Population | Rows |
|---|---:|
| Assets in operating scope | 109 |
| Filing entities / exported entity histories | 104 / 105 including the Oil Pipeline Index instrument |
| Observations | 33,282 |
| Filings / documents / document assertions | 6,265 / 5,283 / 1,753 |
| Source facts | 922,407 |
| Lineage edges / populations | 137,338 / 15,261 |
| Events | 1,238, all explicitly historical/backfill |
| Reviewed annotations | 7 |
| Fayetteville `999999` source anomalies | 4, preserved as filed and warning-gated |

## Headline availability

“Usable” means present, locally validated and not superseded. Coverage percentages use the same frozen 24,607 core-slot denominator throughout; they are not percentages of observations.

| Template | Assets | Entities | Observations | Usable | Review values | History | Core populated | Core validated |
|---|---:|---:|---:|---:|---:|---|---:|---:|
| Interstate gas | 33 | 32 | 21,167 | 17,110 | 1,712 | 2015-09-30 to 2026-09-04 | 11,894 / 14,587 (81.54%) | 11,506 / 14,587 (78.88%) |
| Gas storage | 9 | 9 | 3,414 | 1,565 | 758 | 2022-08-02 to 2026-07-01 | 1,246 / 1,981 (62.90%) | 1,120 / 1,981 (56.54%) |
| Intrastate / 549D | 28 | 28 | 3,055 | 2,032 | 79 | 2017-11-06 to 2026-07-15 | 1,870 / 3,668 (50.98%) | 1,791 / 3,668 (48.83%) |
| Liquids | 30 | 28 | 5,390 | 4,667 | 10 | 1998-06-30 to 2027-06-30 | 3,385 / 4,311 (78.52%) | 3,377 / 4,311 (78.33%) |
| LNG | 9 | 7 | 256 | 172 | 1 | 2004-12-21 to 2026-08-20 | 0 / 60 | 0 / 60 |

The LNG zero in the periodic coverage columns does not mean the source-backed history is empty: 174 present LNG observations remain available. It means the fixed obligation grid does not treat event-driven filings as a guaranteed number of annual events, and the remaining LNG core slots were not satisfied. The underlying event records remain visible and source-linked.

Machine-readable detail is in:

- `exports/frontend_v1/headline_availability.json`: five template rows, 184 template/metric rows and 75 grouped unmatched-core buckets.
- `deliverables/HEADLINE_AVAILABILITY.csv`: compact template table.
- `exports/coverage_by_entity.csv`, `exports/coverage_by_metric.csv` and `exports/coverage_by_slot.csv`: exact underlying extracts.

## Why coverage changed from R6

| Measure | R6 | Candidate | Change |
|---|---:|---:|---:|
| Core populated | 4,617 / 24,607 (18.7630%) | 18,395 / 24,607 (74.7552%) | +13,778 slots |
| Core validated | 4,056 / 24,607 (16.4831%) | 17,794 / 24,607 (72.3128%) | +13,738 slots |
| Due core populated | 4,617 / 22,551 (20.4736%) | 18,001 / 22,551 (79.8235%) | +13,384 slots |
| Due core validated | 4,056 / 22,551 (17.9859%) | 17,400 / 22,551 (77.1584%) | +13,344 slots |

Observation count, denominator and due denominator did not change. The increase is the result of correcting scope-contract matching, not re-ingestion, relaxed units/dates, counting superseded rows or removing obligations. The stricter actual-scope guard still refuses 5,209 core candidates.

The remaining 24,607 core outcomes are: 17,794 validated; 601 populated/review; 1,636 not yet due; 978 not applicable; 1,242 not implemented; 1,577 source blank; 172 retrieval failed; 310 applicability unknown; 19 parse failed; and 278 interpretation blocked. Large practical gaps include unsupported/gated 549D contract-expiry and zero-filled usage semantics, 549D periods with no observation, IOC metrics for entities/snapshots without applicable captured data, some liquids revenue-per-barrel derivations whose gates are unmet, and tariff/date assertions for which no qualifying document was located. These states remain explicit rather than being relabelled to raise coverage.

## Representative consumer payloads

`deliverables/representative_payloads/` contains small real payloads for Transco, Pine Prairie storage, Overland Pass, Arcadia, Sabine Pass LNG and the FERC Oil Pipeline Index. They demonstrate:

- quarterly versus YTD financial/activity periods;
- IOC quantities, shipper concentration and snapshot dates;
- storage capacity versus storage quantity units;
- Form 6 revenue/barrel and the separately scoped annual Page 700 panel;
- a valid not-applicable 549D transportation result alongside retained storage data;
- LNG capacity, operating-report and inspection records without promoting inspection timing to findings; and
- Oil Pipeline Index change, factor, effective interval and industry-instrument identity.

The index with exact paths, sizes and hashes is `deliverables/REPRESENTATIVE_PAYLOAD_INDEX.json`.

## Remaining semantic/source boundaries

- Five active 549D blocker rows remain: four filer-year gates plus one consolidated policy gate. The final R6 exception history refers to six original exceptions because it also retains a duplicate policy record. Storage revenue is not leaked into transportation totals, zero-filled cells are not asserted to be measured zeros, and ambiguous contract grain is not forced.
- `20150930-5060` is assigned to Sabine Pipe Line (C000830). Its official 19,759,596-byte package and eight source rate lines are retained, but the operative numeric rate stays blocked because record/scope/accepting-order conditions are unresolved.
- `20231229-5212` is assigned to Northern Border Pipeline (C000626). Its official 109,287,608-byte ten-member package is retained; the best tariff member has a text layer, but the operative rate remains ambiguity-gated.
- `20260527-5009` is deliberately absent from canonical data. The official filing names Tesoro Logistics Northwest Pipeline, not Williams Northwest Pipeline/C000640, so applying it to Williams would be an entity-identity error.
- All 1,238 events in this captured generation are historical/backfill; there is no invented current investor-feed event.
- Related-project IDs are not supplied. Related assets remain visible, but no asset/docket/company relation is presented as a pre-COD project mapping.
- No new tariff-quote engine, generic OCR system, regime expansion or utilisation methodology was introduced.

## Safety, refresh and validation

A forced bounded v0.36 eLibrary replay committed the three detached entity units but then failed while publishing a compatibility status sidecar to the old Documents clone. The immediate non-force repeat completed cleanly and recorded all three units unchanged. Final v0.37.4 atomic-status and abrupt-exit controls pass, but a new manual refresh was deliberately not run after final publication. This distinction is retained in the logs; database `complete` state alone is not presented as proof that the first CLI invocation succeeded.

Final checks:

| Check | Result |
|---|---|
| Changed frontend/publication/coverage controls | 98 / 98 pass |
| Integrated candidate validation | 30 pass, 0 fail/error/skip; frontend closure 120 assets / 105 histories; publication boundary pass |
| Independent contract/database validation | 23 / 23 pass; SQLite quick check and foreign keys pass |
| Source-cache validation | Pass; 3,587 objects, zero missing/mismatch/unreferenced |
| macOS Vision reviewed-image gate | 8 / 8 pass when run with the required local OS framework |
| Full repository discovery | 855 executed: 853 pass, 0 failures, 2 errors, 0 skips |

The two full-suite errors are unchanged missing inputs to the superseded elaborate release-record workflow: `implementation/FINAL_TEST_RUN_SPEC_DRAFT.json` and `implementation/EXCEPTION_DISPOSITIONS_DRAFT.json`. They are not shipped by the R6 artifact contract. They were not reconstructed, converted to skips or hidden. The equivalent self-contained “open blocker cannot be called resolved” regression passes, and the final discovery command itself uses package-qualified `-s tests -t .`. Accordingly, the relevant runtime/data/export/contract checks are green, while the whole repository suite is truthfully **not entirely green** because those two legacy record-generation tests error.

Primary logs:

- `implementation_logs/data_ready/final_changed_controls_tests.log`
- `implementation_logs/data_ready/final_export_publication_v4.log`
- `implementation_logs/data_ready/final_candidate_validate_v2.log`
- `implementation_logs/data_ready/final_data_ready_contract_validation_post_tests.log`
- `implementation_logs/data_ready/final_source_cache_validation.log`
- `implementation_logs/data_ready/final_full_backend_suite_cpython314.log`
- `implementation_logs/data_ready/open_blocker_self_contained_regression.log`
- `implementation_logs/data_ready/targeted_elibrary_refresh.log`
- `implementation_logs/data_ready/incremental_no_change_refresh.log`

## Commands used for the final candidate

```bash
PYTHONDONTWRITEBYTECODE=1 FERC_OFFLINE=1 FERC_STAGING_DB="$PWD/staging/operating_assets.sqlite" \
  python3 -B run.py export --offline --as-of 2026-09-07 \
  --built-at 2026-09-10T15:00:00+00:00

PYTHONDONTWRITEBYTECODE=1 FERC_OFFLINE=1 FERC_STAGING_DB="$PWD/staging/operating_assets.sqlite" \
  python3 -B run.py validate --offline --as-of 2026-09-07 \
  --built-at 2026-09-10T15:00:00+00:00

PYTHONDONTWRITEBYTECODE=1 FERC_OFFLINE=1 FERC_STAGING_DB="$PWD/staging/operating_assets.sqlite" \
  python3 -B validate_data_ready_candidate.py

python3 -B tools/validate_source_cache.py --cache-root source_cache \
  --report implementation_logs/data_ready/SOURCE_CACHE_FULL_VALIDATION_FINAL.json

PYTHONDONTWRITEBYTECODE=1 FERC_OFFLINE=1 FERC_STAGING_DB="$PWD/staging/operating_assets.sqlite" \
  python3 -B -m unittest discover -s tests -t . -p "test*.py" -v
```

The tested runtime for new changes was CPython 3.14.7. The source tree is standard-library-only; reviewed-image verification additionally requires the declared local `swift`, macOS Vision and `pdftoppm` tools. R6's prior runtime evidence was reused for unchanged components but does not certify these edits on a second Python runtime.

## Deliverable map and rollback

- `publication_receipt.json`: generation receipt and exact hashes for every published file.
- `staging/operating_assets.sqlite`: detached candidate database.
- `exports/frontend_v1/`: later-adapter contract, directory, histories, source index and representative routes.
- `exports/`: canonical observations, entities/assets, coverage, status, events, filings/documents, annotations, registry and lineage.
- `deliverables/`: compact coverage bridge, headline summary, validation result and representative payloads.
- `rollback/operating_assets_pre_data_ready_scope_migration.sqlite`: pre-migration rollback database, 814,530,560 bytes, SHA-256 `70012ab708372c8ea6a5df263cfc580f53df7050e01c3c66228cdaccbc824893`.
- `FRONTEND_BACKEND_CONTRACT.md`: requirement mapping and compatibility boundary.
- `CONNECTION_LATER.md`: deliberately unexecuted later connection steps.

Contract testing was detached and static: schema shape, route closure, cross-file references, source/lineage resolution, comparison gates and representative real payloads. The frontend was not run, no API adapter was installed, and live end-to-end integration remains deliberately untested.
