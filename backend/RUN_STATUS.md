# RUN_STATUS

Run: `run-20260910T090350-711b35`  
Status: **complete** (exit 0)  
Generated: 2026-09-10T09:03:50+00:00  
As-of capture date: `2026-09-07`  Cache-only replay  
Window: 1997-2026  
Registry: `all-regime-2026-09-09-integrated2`  Code: `0.36.0`  Config: `da60ac1474a0b018`  Annotations: `1.1.0`

## Adapters

| Adapter | Eligible | Expected slots | Observations | ok | failed | unchanged | Status |
|---|---:|---:|---:|---:|---:|---:|---|
| oil_index | 1 | 60 | 60 | 1 | 0 | 0 | ok |

## Staging contents

| Table | Rows |
|---|---:|
| asset_entity_map | 109 |
| assets | 109 |
| blockers | 10 |
| checkpoints | 2,231 |
| coverage_expected | 26,279 |
| document_facts | 1,753 |
| documents | 5,283 |
| entities | 105 |
| events | 1,238 |
| filing_dockets | 5,559 |
| filing_entities | 6,354 |
| filings | 6,265 |
| lineage_edges | 137,338 |
| lineage_populations | 15,261 |
| observations | 33,282 |
| ownership | 104 |
| reviewed_source_annotations | 7 |
| run_input_inventory | 6,348 |
| run_log | 1,696 |
| run_unit_status | 572 |
| runs | 3 |
| source_contexts | 125,972 |
| source_dimensions | 139,831 |
| source_facts | 922,407 |
| source_units | 2,499 |
| unit_commits | 286 |

## Open blockers (5)

- **form549d** (semantic): Form 549D field 72 is transportation-only by definition, yet filers report Total_Rev on Storage rows; a display policy is required for the as-filed out-of-scope amounts
- **form549d** (semantic): Form 549D field 72 is transportation-only by definition, yet filers report Total_Rev on Storage rows; a display policy is required for the as-filed out-of-scope amounts
- **form549d** (semantic): Form 549D field 72 is transportation-only by definition, yet filers report Total_Rev on Storage rows; a display policy is required for the as-filed out-of-scope amounts
- **form549d** (semantic): Form 549D field 72 is transportation-only by definition, yet filers report Total_Rev on Storage rows; a display policy is required for the as-filed out-of-scope amounts
- **form549d** (semantic): Form 549D field 72 is transportation-only by definition, yet filers report Total_Rev on Storage rows; a display policy is required for the as-filed out-of-scope amounts

## Ledger

| Task | Milestone | Owner | State | Source coverage | Tests | Blocker | Next |
|---|---|---|---|---|---|---|---|
| capacity:C000020 | M3 | capacity | done | 2 filings, 4 slots | 52 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 population |  |  |
| capacity:C000021 | M3 | capacity | done | 2 filings, 4 slots | 28 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| capacity:C000087 | M3 | capacity | done | 2 filings, 4 slots | 12 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 population |  |  |
| capacity:C000147 | M3 | capacity | done | 2 filings, 4 slots | 16 observations, 4 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| capacity:C000226 | M3 | capacity | done | 2 filings, 4 slots | 9 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populations |  |  |
| capacity:C000231 | M3 | capacity | done | 2 filings, 4 slots | 6 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populations |  |  |
| capacity:C000235 | M3 | capacity | done | 2 filings, 4 slots | 10 observations, 4 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| capacity:C000236 | M3 | capacity | done | 2 filings, 4 slots | 8 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populations |  |  |
| capacity:C000255 | M3 | capacity | done | 2 filings, 2 slots | 10 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| capacity:C000584 | M3 | capacity | done | 2 filings, 4 slots | 28 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| capacity:C000596 | M3 | capacity | done | 1 filings, 2 slots | 4 observations, 1 edges, 0 superseded-by-rebuild, 0 prior versions archived, 1 populations |  |  |
| capacity:C000626 | M3 | capacity | done | 2 filings, 4 slots | 13 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 population |  |  |
| capacity:C000640 | M3 | capacity | done | 2 filings, 4 slots | 25 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 population |  |  |
| capacity:C000650 | M3 | capacity | done | 2 filings, 4 slots | 8 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populations |  |  |
| capacity:C000654 | M3 | capacity | done | 2 filings, 4 slots | 26 observations, 4 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| capacity:C000830 | M3 | capacity | done | 2 filings, 4 slots | 15 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 population |  |  |
| capacity:C000967 | M3 | capacity | done | 2 filings, 4 slots | 28 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 population |  |  |
| capacity:C000978 | M3 | capacity | done | 2 filings, 4 slots | 10 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 population |  |  |
| capacity:C000981 | M3 | capacity | done | 2 filings, 4 slots | 26 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 population |  |  |
| capacity:C000985 | M3 | capacity | done | 2 filings, 2 slots | 10 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| capacity:C000995 | M3 | capacity | done | 2 filings, 4 slots | 10 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 population |  |  |
| capacity:C001012 | M3 | capacity | done | 2 filings, 4 slots | 10 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 population |  |  |
| capacity:C001014 | M3 | capacity | done | 2 filings, 4 slots | 12 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 population |  |  |
| capacity:C001031 | M3 | capacity | done | 2 filings, 4 slots | 15 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 population |  |  |
| capacity:C001058 | M3 | capacity | done | 2 filings, 2 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| capacity:C001087 | M3 | capacity | done | 2 filings, 4 slots | 8 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populations |  |  |
| capacity:C001088 | M3 | capacity | done | 2 filings, 4 slots | 20 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 population |  |  |
| capacity:C001089 | M3 | capacity | done | 2 filings, 4 slots | 8 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populations |  |  |
| capacity:C001506 | M3 | capacity | done | 2 filings, 4 slots | 6 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populations |  |  |
| capacity:C001591 | M3 | capacity | done | 2 filings, 2 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| capacity:C001593 | M3 | capacity | done | 0 filings, 1 slots | 1 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| capacity:C001683 | M3 | capacity | done | 2 filings, 2 slots | 15 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| capacity:C001685 | M3 | capacity | done | 2 filings, 2 slots | 38 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| capacity:C002096 | M3 | capacity | done | 1 filings, 2 slots | 20 observations, 1 edges, 0 superseded-by-rebuild, 0 prior versions archived, 1 population |  |  |
| capacity:C003234 | M3 | capacity | done | 0 filings, 1 slots | 1 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| capacity:C003373 | M3 | capacity | done | 1 filings, 1 slots | 3 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| capacity:C003409 | M3 | capacity | done | 2 filings, 2 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| capacity:C004558 | M3 | capacity | done | 2 filings, 4 slots | 8 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populations |  |  |
| capacity:C007688 | M3 | capacity | done | 0 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| capacity:C011407 | M3 | capacity | done | 0 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| capacity:NO-FERC-CID:Targa Pipeline Mid-Continent WestTex LLC | M3 | capacity | done | 0 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| elibrary_docs:C000020 | M3 | elibrary_docs | done | 211 filings, 6 slots | 14 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 63 events |  |  |
| elibrary_docs:C000021 | M3 | elibrary_docs | done | 162 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 33 events |  |  |
| elibrary_docs:C000075 | M3 | elibrary_docs | done | 49 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C000087 | M3 | elibrary_docs | done | 67 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 24 events |  |  |
| elibrary_docs:C000147 | M3 | elibrary_docs | done | 92 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 28 events |  |  |
| elibrary_docs:C000226 | M3 | elibrary_docs | done | 63 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 34 events |  |  |
| elibrary_docs:C000231 | M3 | elibrary_docs | done | 94 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 34 events |  |  |
| elibrary_docs:C000235 | M3 | elibrary_docs | done | 132 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 30 events |  |  |
| elibrary_docs:C000236 | M3 | elibrary_docs | done | 49 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 34 events |  |  |
| elibrary_docs:C000255 | M3 | elibrary_docs | done | 147 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 49 events |  |  |
| elibrary_docs:C000433 | M3 | elibrary_docs | done | 10 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C000434 | M3 | elibrary_docs | done | 3 filings, 2 slots | 4 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 3 events |  |  |
| elibrary_docs:C000435 | M3 | elibrary_docs | done | 2 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C000436 | M3 | elibrary_docs | done | 0 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| elibrary_docs:C000584 | M3 | elibrary_docs | done | 56 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 31 events |  |  |
| elibrary_docs:C000585 | M3 | elibrary_docs | done | 3 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C000586 | M3 | elibrary_docs | done | 13 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C000587 | M3 | elibrary_docs | done | 0 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| elibrary_docs:C000588 | M3 | elibrary_docs | done | 7 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 4 events |  |  |
| elibrary_docs:C000589 | M3 | elibrary_docs | done | 4 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 3 events |  |  |
| elibrary_docs:C000596 | M3 | elibrary_docs | done | 74 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 25 events |  |  |
| elibrary_docs:C000604 | M3 | elibrary_docs | done | 8 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 4 events |  |  |
| elibrary_docs:C000605 | M3 | elibrary_docs | done | 23 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C000606 | M3 | elibrary_docs | done | 43 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C000607 | M3 | elibrary_docs | done | 58 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C000608 | M3 | elibrary_docs | done | 35 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C000626 | M3 | elibrary_docs | done | 121 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 28 events |  |  |
| elibrary_docs:C000629 | M3 | elibrary_docs | done | 23 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C000630 | M3 | elibrary_docs | done | 31 filings, 4 slots | 4 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C000633 | M3 | elibrary_docs | done | 27 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C000640 | M3 | elibrary_docs | done | 101 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 30 events |  |  |
| elibrary_docs:C000650 | M3 | elibrary_docs | done | 49 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 16 events |  |  |
| elibrary_docs:C000654 | M3 | elibrary_docs | done | 185 filings, 6 slots | 14 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 77 events |  |  |
| elibrary_docs:C000826 | M3 | elibrary_docs | done | 18 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 4 events |  |  |
| elibrary_docs:C000830 | M3 | elibrary_docs | done | 83 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 25 events |  |  |
| elibrary_docs:C000832 | M3 | elibrary_docs | done | 34 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C000951 | M3 | elibrary_docs | done | 10 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 3 events |  |  |
| elibrary_docs:C000967 | M3 | elibrary_docs | done | 126 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 25 events |  |  |
| elibrary_docs:C000978 | M3 | elibrary_docs | done | 143 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 25 events |  |  |
| elibrary_docs:C000981 | M3 | elibrary_docs | done | 148 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 26 events |  |  |
| elibrary_docs:C000985 | M3 | elibrary_docs | done | 69 filings, 3 slots | 3 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 5 events |  |  |
| elibrary_docs:C000995 | M3 | elibrary_docs | done | 47 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 11 events |  |  |
| elibrary_docs:C001012 | M3 | elibrary_docs | done | 82 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 36 events |  |  |
| elibrary_docs:C001014 | M3 | elibrary_docs | done | 50 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 24 events |  |  |
| elibrary_docs:C001031 | M3 | elibrary_docs | done | 70 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 27 events |  |  |
| elibrary_docs:C001049 | M3 | elibrary_docs | done | 142 filings, 4 slots | 9 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 4 events |  |  |
| elibrary_docs:C001058 | M3 | elibrary_docs | done | 50 filings, 3 slots | 3 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C001087 | M3 | elibrary_docs | done | 45 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 10 events |  |  |
| elibrary_docs:C001088 | M3 | elibrary_docs | done | 40 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 15 events |  |  |
| elibrary_docs:C001089 | M3 | elibrary_docs | done | 58 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 26 events |  |  |
| elibrary_docs:C001142 | M3 | elibrary_docs | done | 7 filings, 2 slots | 4 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 5 events |  |  |
| elibrary_docs:C001151 | M3 | elibrary_docs | done | 27 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C001202 | M3 | elibrary_docs | done | 0 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| elibrary_docs:C001422 | M3 | elibrary_docs | done | 15 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C001506 | M3 | elibrary_docs | done | 62 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 31 events |  |  |
| elibrary_docs:C001562 | M3 | elibrary_docs | done | 8 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C001591 | M3 | elibrary_docs | done | 42 filings, 3 slots | 3 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C001593 | M3 | elibrary_docs | done | 0 filings, 3 slots | 3 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| elibrary_docs:C001674 | M3 | elibrary_docs | done | 3 filings, 2 slots | 4 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 3 events |  |  |
| elibrary_docs:C001683 | M3 | elibrary_docs | done | 74 filings, 3 slots | 3 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C001685 | M3 | elibrary_docs | done | 42 filings, 3 slots | 3 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C001773 | M3 | elibrary_docs | done | 10 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 4 events |  |  |
| elibrary_docs:C002076 | M3 | elibrary_docs | done | 10 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 3 events |  |  |
| elibrary_docs:C002096 | M3 | elibrary_docs | done | 180 filings, 6 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 3 events |  |  |
| elibrary_docs:C002127 | M3 | elibrary_docs | done | 18 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C002792 | M3 | elibrary_docs | done | 0 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| elibrary_docs:C003132 | M3 | elibrary_docs | done | 100 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C003234 | M3 | elibrary_docs | done | 50 filings, 3 slots | 3 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C003337 | M3 | elibrary_docs | done | 7 filings, 2 slots | 4 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 4 events |  |  |
| elibrary_docs:C003338 | M3 | elibrary_docs | done | 24 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C003373 | M3 | elibrary_docs | done | 43 filings, 3 slots | 3 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C003409 | M3 | elibrary_docs | done | 42 filings, 3 slots | 3 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C003520 | M3 | elibrary_docs | done | 20 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C004558 | M3 | elibrary_docs | done | 76 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 27 events |  |  |
| elibrary_docs:C004609 | M3 | elibrary_docs | done | 50 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C004698 | M3 | elibrary_docs | done | 7 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 3 events |  |  |
| elibrary_docs:C005311 | M3 | elibrary_docs | done | 51 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C005518 | M3 | elibrary_docs | done | 23 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C007599 | M3 | elibrary_docs | done | 2 filings, 4 slots | 4 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| elibrary_docs:C007688 | M3 | elibrary_docs | done | 48 filings, 6 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C008788 | M3 | elibrary_docs | done | 27 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C008892 | M3 | elibrary_docs | done | 17 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C008985 | M3 | elibrary_docs | done | 12 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C010083 | M3 | elibrary_docs | done | 9 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C010084 | M3 | elibrary_docs | done | 17 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C010090 | M3 | elibrary_docs | done | 17 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C010176 | M3 | elibrary_docs | done | 17 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C010368 | M3 | elibrary_docs | done | 13 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C010798 | M3 | elibrary_docs | done | 11 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C010819 | M3 | elibrary_docs | done | 10 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:C010853 | M3 | elibrary_docs | done | 17 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 5 events |  |  |
| elibrary_docs:C011407 | M3 | elibrary_docs | done | 27 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 27 events |  |  |
| elibrary_docs:C011641 | M3 | elibrary_docs | done | 5 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 5 events |  |  |
| elibrary_docs:C012028 | M3 | elibrary_docs | done | 5 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 5 events |  |  |
| elibrary_docs:C012377 | M3 | elibrary_docs | done | 12 filings, 2 slots | 2 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 8 events |  |  |
| elibrary_docs:C012931 | M3 | elibrary_docs | done | 12 filings, 4 slots | 6 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 events |  |  |
| elibrary_docs:NO-FERC-CID:Targa Pipeline Mid-Continent WestTex LLC | M3 | elibrary_docs | done | 7 filings, 6 slots | 11 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived, 7 events |  |  |
| form549d:C000433 | M3 | form549d | done | 10 filings, 106 slots | 108 observations, 337 edges, 0 superseded-by-rebuild, 0 prior versions archived, 26 popula |  |  |
| form549d:C000434 | M3 | form549d | done | 10 filings, 106 slots | 107 observations, 355 edges, 0 superseded-by-rebuild, 0 prior versions archived, 24 popula |  |  |
| form549d:C000435 | M3 | form549d | done | 10 filings, 106 slots | 106 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populatio |  |  |
| form549d:C000436 | M3 | form549d | done | 10 filings, 106 slots | 106 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populatio |  |  |
| form549d:C000585 | M3 | form549d | done | 10 filings, 106 slots | 106 observations, 48 edges, 0 superseded-by-rebuild, 0 prior versions archived, 4 populati |  |  |
| form549d:C000586 | M3 | form549d | done | 10 filings, 106 slots | 106 observations, 2405 edges, 0 superseded-by-rebuild, 0 prior versions archived, 26 popul |  |  |
| form549d:C000587 | M3 | form549d | done | 10 filings, 106 slots | 106 observations, 48 edges, 0 superseded-by-rebuild, 0 prior versions archived, 4 populati |  |  |
| form549d:C000588 | M3 | form549d | done | 10 filings, 106 slots | 109 observations, 2507 edges, 0 superseded-by-rebuild, 0 prior versions archived, 23 popul |  |  |
| form549d:C000589 | M3 | form549d | done | 10 filings, 106 slots | 106 observations, 141 edges, 0 superseded-by-rebuild, 0 prior versions archived, 26 popula |  |  |
| form549d:C000604 | M3 | form549d | done | 10 filings, 106 slots | 106 observations, 124 edges, 0 superseded-by-rebuild, 0 prior versions archived, 26 popula |  |  |
| form549d:C000826 | M3 | form549d | done | 10 filings, 106 slots | 106 observations, 5290 edges, 0 superseded-by-rebuild, 0 prior versions archived, 25 popul |  |  |
| form549d:C000951 | M3 | form549d | done | 9 filings, 106 slots | 106 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populatio |  |  |
| form549d:C001142 | M3 | form549d | done | 10 filings, 106 slots | 106 observations, 1363 edges, 0 superseded-by-rebuild, 0 prior versions archived, 11 popul |  |  |
| form549d:C001202 | M3 | form549d | done | 10 filings, 106 slots | 106 observations, 58 edges, 0 superseded-by-rebuild, 0 prior versions archived, 4 populati |  |  |
| form549d:C001422 | M3 | form549d | done | 10 filings, 106 slots | 106 observations, 3640 edges, 0 superseded-by-rebuild, 0 prior versions archived, 24 popul |  |  |
| form549d:C001562 | M3 | form549d | done | 9 filings, 106 slots | 106 observations, 2666 edges, 0 superseded-by-rebuild, 0 prior versions archived, 22 popul |  |  |
| form549d:C001674 | M3 | form549d | done | 10 filings, 106 slots | 106 observations, 232 edges, 0 superseded-by-rebuild, 0 prior versions archived, 24 popula |  |  |
| form549d:C001773 | M3 | form549d | done | 10 filings, 106 slots | 106 observations, 3653 edges, 0 superseded-by-rebuild, 0 prior versions archived, 24 popul |  |  |
| form549d:C002076 | M3 | form549d | done | 10 filings, 106 slots | 106 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populatio |  |  |
| form549d:C002792 | M3 | form549d | done | 9 filings, 106 slots | 109 observations, 452 edges, 0 superseded-by-rebuild, 0 prior versions archived, 21 popula |  |  |
| form549d:C003337 | M3 | form549d | done | 9 filings, 106 slots | 106 observations, 128 edges, 0 superseded-by-rebuild, 0 prior versions archived, 23 popula |  |  |
| form549d:C004698 | M3 | form549d | done | 10 filings, 106 slots | 108 observations, 574 edges, 0 superseded-by-rebuild, 0 prior versions archived, 24 popula |  |  |
| form549d:C010083 | M3 | form549d | done | 10 filings, 106 slots | 106 observations, 197 edges, 0 superseded-by-rebuild, 0 prior versions archived, 4 populat |  |  |
| form549d:C010084 | M3 | form549d | done | 10 filings, 106 slots | 108 observations, 1155 edges, 0 superseded-by-rebuild, 0 prior versions archived, 26 popul |  |  |
| form549d:C010853 | M3 | form549d | done | 10 filings, 106 slots | 116 observations, 1273 edges, 0 superseded-by-rebuild, 0 prior versions archived, 26 popul |  |  |
| form549d:C011641 | M3 | form549d | done | 9 filings, 106 slots | 106 observations, 117 edges, 0 superseded-by-rebuild, 0 prior versions archived, 22 popula |  |  |
| form549d:C012028 | M3 | form549d | done | 9 filings, 106 slots | 106 observations, 112 edges, 0 superseded-by-rebuild, 0 prior versions archived, 24 popula |  |  |
| form549d:C012377 | M3 | form549d | done | 7 filings, 106 slots | 106 observations, 2086 edges, 0 superseded-by-rebuild, 0 prior versions archived, 20 popul |  |  |
| gas_xbrl:C000020 | M3 | gas_xbrl | done | 33 filings, 1208 slots | 1264 observations, 979 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000021 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 310 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000087 | M3 | gas_xbrl | done | 11 filings, 366 slots | 382 observations, 71 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000147 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 80 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000226 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 60 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000231 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 68 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000235 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 92 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000236 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 60 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000255 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 62 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000584 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 60 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000596 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 110 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000626 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 123 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000640 | M3 | gas_xbrl | done | 11 filings, 366 slots | 382 observations, 69 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000650 | M3 | gas_xbrl | done | 9 filings, 334 slots | 349 observations, 56 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000654 | M3 | gas_xbrl | done | 32 filings, 1208 slots | 1264 observations, 395 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000830 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 62 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000967 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 349 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000978 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 124 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000981 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 237 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000985 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 60 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C000995 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 78 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C001012 | M3 | gas_xbrl | done | 10 filings, 366 slots | 379 observations, 30 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C001014 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 68 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C001031 | M3 | gas_xbrl | done | 10 filings, 366 slots | 394 observations, 91 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C001058 | M3 | gas_xbrl | done | 2 filings, 110 slots | 110 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C001087 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 62 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C001088 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 79 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C001089 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 62 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C001506 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 68 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C001591 | M3 | gas_xbrl | done | 2 filings, 110 slots | 110 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C001593 | M3 | gas_xbrl | done | 2 filings, 110 slots | 110 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C001683 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 76 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C001685 | M3 | gas_xbrl | done | 2 filings, 110 slots | 110 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C002096 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 402 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C003234 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 71 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C003373 | M3 | gas_xbrl | done | 2 filings, 110 slots | 110 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C003409 | M3 | gas_xbrl | done | 2 filings, 110 slots | 110 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C004558 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 68 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C007688 | M3 | gas_xbrl | done | 10 filings, 366 slots | 382 observations, 68 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:C011407 | M3 | gas_xbrl | done | 2 filings, 110 slots | 110 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| gas_xbrl:NO-FERC-CID:Targa Pipeline Mid-Continent WestTex LLC | M3 | gas_xbrl | done | 0 filings, 0 slots | 0 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| ioc:C000020 | M3 | ioc | done | 13 filings, 88 slots | 319 observations, 2112 edges, 0 superseded-by-rebuild, 0 prior versions archived, 649 popu |  |  |
| ioc:C000021 | M3 | ioc | done | 11 filings, 88 slots | 319 observations, 2775 edges, 0 superseded-by-rebuild, 0 prior versions archived, 649 popu |  |  |
| ioc:C000087 | M3 | ioc | done | 11 filings, 88 slots | 187 observations, 4257 edges, 0 superseded-by-rebuild, 0 prior versions archived, 352 popu |  |  |
| ioc:C000147 | M3 | ioc | done | 10 filings, 80 slots | 170 observations, 5544 edges, 0 superseded-by-rebuild, 0 prior versions archived, 320 popu |  |  |
| ioc:C000226 | M3 | ioc | done | 11 filings, 80 slots | 170 observations, 692 edges, 0 superseded-by-rebuild, 0 prior versions archived, 282 popul |  |  |
| ioc:C000231 | M3 | ioc | done | 11 filings, 88 slots | 187 observations, 1351 edges, 0 superseded-by-rebuild, 0 prior versions archived, 325 popu |  |  |
| ioc:C000235 | M3 | ioc | done | 10 filings, 80 slots | 170 observations, 4816 edges, 0 superseded-by-rebuild, 0 prior versions archived, 320 popu |  |  |
| ioc:C000236 | M3 | ioc | done | 12 filings, 88 slots | 187 observations, 702 edges, 0 superseded-by-rebuild, 0 prior versions archived, 308 popul |  |  |
| ioc:C000255 | M3 | ioc | done | 13 filings, 80 slots | 171 observations, 1386 edges, 0 superseded-by-rebuild, 0 prior versions archived, 321 popu |  |  |
| ioc:C000584 | M3 | ioc | done | 11 filings, 88 slots | 187 observations, 1642 edges, 0 superseded-by-rebuild, 0 prior versions archived, 325 popu |  |  |
| ioc:C000596 | M3 | ioc | done | 11 filings, 88 slots | 187 observations, 2360 edges, 0 superseded-by-rebuild, 0 prior versions archived, 352 popu |  |  |
| ioc:C000626 | M3 | ioc | done | 11 filings, 88 slots | 187 observations, 2001 edges, 0 superseded-by-rebuild, 0 prior versions archived, 353 popu |  |  |
| ioc:C000640 | M3 | ioc | done | 10 filings, 80 slots | 270 observations, 5698 edges, 0 superseded-by-rebuild, 0 prior versions archived, 570 popu |  |  |
| ioc:C000650 | M3 | ioc | done | 11 filings, 88 slots | 176 observations, 1615 edges, 0 superseded-by-rebuild, 0 prior versions archived, 341 popu |  |  |
| ioc:C000654 | M3 | ioc | done | 12 filings, 96 slots | 336 observations, 2998 edges, 0 superseded-by-rebuild, 0 prior versions archived, 696 popu |  |  |
| ioc:C000830 | M3 | ioc | done | 10 filings, 72 slots | 153 observations, 1683 edges, 0 superseded-by-rebuild, 0 prior versions archived, 288 popu |  |  |
| ioc:C000967 | M3 | ioc | done | 12 filings, 88 slots | 187 observations, 1825 edges, 0 superseded-by-rebuild, 0 prior versions archived, 352 popu |  |  |
| ioc:C000978 | M3 | ioc | done | 11 filings, 88 slots | 187 observations, 1711 edges, 0 superseded-by-rebuild, 0 prior versions archived, 352 popu |  |  |
| ioc:C000981 | M3 | ioc | done | 11 filings, 88 slots | 310 observations, 5664 edges, 0 superseded-by-rebuild, 0 prior versions archived, 640 popu |  |  |
| ioc:C000985 | M3 | ioc | done | 11 filings, 88 slots | 187 observations, 966 edges, 0 superseded-by-rebuild, 0 prior versions archived, 319 popul |  |  |
| ioc:C000995 | M3 | ioc | done | 11 filings, 88 slots | 271 observations, 4733 edges, 0 superseded-by-rebuild, 0 prior versions archived, 513 popu |  |  |
| ioc:C001012 | M3 | ioc | done | 11 filings, 88 slots | 88 observations, 77 edges, 0 superseded-by-rebuild, 0 prior versions archived, 77 populati |  |  |
| ioc:C001014 | M3 | ioc | done | 11 filings, 88 slots | 187 observations, 1197 edges, 0 superseded-by-rebuild, 0 prior versions archived, 326 popu |  |  |
| ioc:C001031 | M3 | ioc | done | 12 filings, 88 slots | 187 observations, 742 edges, 0 superseded-by-rebuild, 0 prior versions archived, 353 popul |  |  |
| ioc:C001058 | M3 | ioc | done | 10 filings, 80 slots | 190 observations, 4456 edges, 0 superseded-by-rebuild, 0 prior versions archived, 364 popu |  |  |
| ioc:C001087 | M3 | ioc | done | 12 filings, 88 slots | 187 observations, 1816 edges, 0 superseded-by-rebuild, 0 prior versions archived, 352 popu |  |  |
| ioc:C001088 | M3 | ioc | done | 14 filings, 96 slots | 336 observations, 7593 edges, 0 superseded-by-rebuild, 0 prior versions archived, 708 popu |  |  |
| ioc:C001089 | M3 | ioc | done | 11 filings, 80 slots | 170 observations, 1402 edges, 0 superseded-by-rebuild, 0 prior versions archived, 308 popu |  |  |
| ioc:C001506 | M3 | ioc | done | 18 filings, 128 slots | 272 observations, 1070 edges, 0 superseded-by-rebuild, 0 prior versions archived, 448 popu |  |  |
| ioc:C001591 | M3 | ioc | done | 9 filings, 72 slots | 153 observations, 2658 edges, 0 superseded-by-rebuild, 0 prior versions archived, 288 popu |  |  |
| ioc:C001593 | M3 | ioc | done | 0 filings, 8 slots | 8 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| ioc:C001683 | M3 | ioc | done | 11 filings, 88 slots | 319 observations, 4046 edges, 0 superseded-by-rebuild, 0 prior versions archived, 649 popu |  |  |
| ioc:C001685 | M3 | ioc | done | 10 filings, 80 slots | 290 observations, 8885 edges, 0 superseded-by-rebuild, 0 prior versions archived, 590 popu |  |  |
| ioc:C002096 | M3 | ioc | done | 11 filings, 88 slots | 319 observations, 3233 edges, 0 superseded-by-rebuild, 0 prior versions archived, 649 popu |  |  |
| ioc:C003234 | M3 | ioc | done | 0 filings, 8 slots | 8 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| ioc:C003373 | M3 | ioc | done | 10 filings, 80 slots | 170 observations, 640 edges, 0 superseded-by-rebuild, 0 prior versions archived, 280 popul |  |  |
| ioc:C003409 | M3 | ioc | done | 10 filings, 80 slots | 170 observations, 3452 edges, 0 superseded-by-rebuild, 0 prior versions archived, 320 popu |  |  |
| ioc:C004558 | M3 | ioc | done | 11 filings, 88 slots | 187 observations, 680 edges, 0 superseded-by-rebuild, 0 prior versions archived, 308 popul |  |  |
| ioc:C007688 | M3 | ioc | done | 0 filings, 8 slots | 8 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| ioc:C011407 | M3 | ioc | done | 0 filings, 8 slots | 8 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| ioc:NO-FERC-CID:Targa Pipeline Mid-Continent WestTex LLC | M3 | ioc | done | 0 filings, 8 slots | 8 observations, 0 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
| liquids_xbrl:C000075 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 176 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C000605 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 166 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C000606 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 170 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C000607 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 168 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C000608 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 170 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C000629 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 174 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C000630 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 178 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C000633 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 169 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C000832 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 172 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C001049 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 176 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C001151 | M3 | liquids_xbrl | done | 9 filings, 147 slots | 176 observations, 154 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C002127 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 167 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C003132 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 172 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C003338 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 168 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C003520 | M3 | liquids_xbrl | done | 11 filings, 158 slots | 192 observations, 162 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C004609 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 182 observations, 150 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C005311 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 176 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C005518 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 158 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C007599 | M3 | liquids_xbrl | done | 9 filings, 147 slots | 174 observations, 148 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C008788 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 163 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C008892 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 166 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C008985 | M3 | liquids_xbrl | done | 11 filings, 158 slots | 192 observations, 168 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C010090 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 168 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C010176 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 158 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C010368 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 192 observations, 168 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C010798 | M3 | liquids_xbrl | done | 10 filings, 158 slots | 182 observations, 145 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C010819 | M3 | liquids_xbrl | done | 9 filings, 147 slots | 176 observations, 154 edges, 0 superseded-by-rebuild, 0 prior versions archived, 2 populat |  |  |
| liquids_xbrl:C012931 | M3 | liquids_xbrl | done | 2 filings, 46 slots | 49 observations, 48 edges, 0 superseded-by-rebuild, 0 prior versions archived, 1 populatio |  |  |
| lng:C000039 | M3 | lng | done | 37 filings, 10 slots | 38 observations, 4 edges, 0 superseded-by-rebuild, 0 prior versions archived, 4 population |  |  |
| lng:NO-FERC-CID:Corpus Christi Liquefaction, LLC | M3 | lng | done | 142 filings, 10 slots | 58 observations, 3 edges, 0 superseded-by-rebuild, 0 prior versions archived, 3 population |  |  |
| lng:NO-FERC-CID:Elba Liquefaction Company, L.L.C. | M3 | lng | done | 133 filings, 10 slots | 58 observations, 4 edges, 0 superseded-by-rebuild, 0 prior versions archived, 4 population |  |  |
| lng:NO-FERC-CID:Gulf LNG Energy, LLC | M3 | lng | done | 19 filings, 10 slots | 27 observations, 4 edges, 0 superseded-by-rebuild, 0 prior versions archived, 4 population |  |  |
| lng:NO-FERC-CID:Roadrunner Gas Transmission, LLC | M3 | lng | done | 26 filings, 10 slots | 12 observations, 1 edges, 0 superseded-by-rebuild, 0 prior versions archived, 1 population |  |  |
| lng:NO-FERC-CID:Sabine Pass LNG, L.P. | M3 | lng | done | 46 filings, 10 slots | 28 observations, 4 edges, 0 superseded-by-rebuild, 0 prior versions archived, 4 population |  |  |
| lng:NO-FERC-CID:Sabine Pass Liquefaction, LLC | M3 | lng | done | 55 filings, 10 slots | 35 observations, 4 edges, 0 superseded-by-rebuild, 0 prior versions archived, 4 population |  |  |
| oil_index:FERC-OIL-INDEX | M3 | oil_index | done | 31 filings, 60 slots | 60 observations, 2 edges, 0 superseded-by-rebuild, 0 prior versions archived |  |  |
