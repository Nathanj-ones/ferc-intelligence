# Provenance and lineage repair scope

Recorded: 2026-09-09 (Europe/London)

## Outcome

The repaired/live database fixes the broad historical A08 absence: it has 123,380 lineage edges, 12,978 persisted populations, no dangling nonblank fact/observation/population identifiers, and no present derived observation without an edge. Two narrower issues remain and must not be conflated:

1. The IOC top-five output is collectively traceable and independently recomputes correctly, but 2,004 `group_member` rows are still descriptive records rather than resolvable lineage edges. Each carries a valid filing ID, but all 2,004 have blank fact, input-observation and population IDs.
2. The 81 gas operating-margin outputs have blank *direct* filing/fact IDs on their 162 outer edges, but every edge points to a real intermediate observation and every intermediate terminates in source facts. This is valid multi-stage lineage, not missing source data. The defect in that population is scope metadata: all 162 intermediate/output pairs use different stored scopes in the audited database.

Accordingly, the minimum repair is not “fill every blank source-fact field.” IOC summary edges need a real referent or a non-lineage home; gas multi-source paths need recursive validation and the candidate scope migration/rebuild. Inventing one terminal filing for a multi-filing intermediate would make provenance less accurate.

## Evidence boundary and reproducibility

Both database reads used `mode=ro&immutable=1`; the independent IOC redraw imports no project module and performs no network access. No production source, database or export was modified.

| State | Database | Bytes | SHA-256 | Integrity |
|---|---|---:|---|---|
| Audited repaired/live | `../baseline/databases/live_operating_assets_20260909.sqlite` | 774,959,104 | `773fe35600031c461166f89bba860bbcedaee04e16a80029f2d950cde080a4d4` | `quick_check=ok`; 0 FK violations |
| Historical defective evidence | `../baseline/databases/historical_defect_snapshot.sqlite` | 707,710,976 | `a19f10bd7b1a082c9033a3f75e5e7674940a7542f899b8a76f3931330994ad0a` | `quick_check=ok`; 0 FK violations |

The selected repaired snapshot had no unapplied WAL content. At the final read a zero-byte WAL and a 32,768-byte SHM sidecar were present; the base-file hash still matched the selected snapshot, and the immutable read ignored sidecars. Exact SQL is in `implementation_logs/lineage_analysis/lineage_queries.sql` and `historical_lineage_queries.sql`. Full affected record IDs—not abbreviated samples—are in the CSV extracts named below.

## Historical A08 population recovered exactly

The historical-defect snapshot contains 42,863 lineage edges. Exactly 1,870 have neither a fact nor an input-observation ID:

| Adapter / metric / role | Edges | Outputs | Entities | Filings |
|---|---:|---:|---:|---:|
| `ioc` / `ioc_top5_shipper_concentration` / `group_member` | 1,854 | 449 | 33 | 341 |
| `gas_xbrl` / `cap_peak_day_ratio` / `denominator` | 16 | 16 | 9 | 16 |

It also contains 1,662 present IOC observations with no lineage edge: 1,191 `ioc_points`, 313 `ioc_firm_transport_mdq`, and 158 `ioc_contracted_storage_quantity`. Separately, 52 present Form 549D derived observations have no edge: 25 `i311_affiliate_activity`, 11 `i311_firm_share`, eight `i311_revenue_components`, and eight `i311_revenue_grain_diagnostic`.

The complete IDs and values are preserved in:

- `implementation_logs/lineage_analysis/historical_targetless_edges.csv` (1,870 records)
- `implementation_logs/lineage_analysis/historical_edgeless_ioc_observations.csv` (1,662 records)
- `implementation_logs/lineage_analysis/historical_edgeless_derived_observations.csv` (52 records)

These are historical-negative populations only; they must not be used as expected repaired output.

## Current IOC `group_member` issue

### Exact pattern

All 2,004 current edges without a resolving fact, observation or population have one shape:

- adapter/source: `ioc`, eLibrary, Form 549B IOC;
- output metric: `ioc_top5_shipper_concentration`;
- role: `group_member`;
- 479 output observations, 35 entities and 371 distinct filing occurrences;
- all 2,004 filing IDs exist and belong to the same entity as the output;
- all 2,004 source-fact, input-observation and population IDs are blank.

The number of summaries per output is supported by the filed population: 358 outputs have five group members, eight have four, 21 have three, 27 have two, and 65 have one. Full edge/output/entity identifiers are in `unresolved_group_member_edges.csv`, `group_member_support_by_observation.csv`, and `group_member_affected_entities.csv`.

### What is and is not source-supported

`adapters/ioc.py:2760-2817` aggregates contracts to a normalized legal shipper, ranks the shippers, emits the targetless summaries at lines 2794-2796, and then persists one collective population of all contracts held by the top five at lines 2800-2817. That collective population is real evidence for the output, but it is not a specific referent for any one descriptive summary.

The independent stdlib-only redraw checked every affected output directly against persisted IOC D-row `typed_dims_json`, using only whitespace/case normalization of the filed legal name. Results:

- 479/479 output observations and 479/479 collective populations checked;
- 8,564 contributing source-fact members checked;
- summary names and weights, candidate/row/excluded counts, member digests and samples, aggregate totals, denominator links and published percentages all agreed;
- zero mismatches.

Therefore the aggregate values and collective membership are verified. The claim that every `group_member` row is itself a resolvable edge is contradicted.

### Validator gap

The adapter self-audit does not fully protect this path:

- `adapters/ioc.py:2053-2059` rejects roles consisting only of `denominator`; leftover targetless `group_member` roles prevent that condition from firing even if the collective population is removed.
- `adapters/ioc.py:2143-2173` fully compares per-row edges only when a population has at most 40 members.
- `adapters/ioc.py:1982-2007` and `2204-2223` define redraw rules for denominator/point populations, but not `top5_shipper_contracts`. Consequently, 60 top-five populations containing 3,523 members exceed the per-row limit and receive only the 25-member sample existence check, not a full source-fact redraw.
- `tests/test_ioc.py:413-429` asks only whether any denominator and any `group_member` exist; it does not require a referent, the collective population, or recomputation.
- `ferclib/staging.py:592-612` checks that a nonblank population link names a batch population, but does not reject an edge with no fact, observation or population referent.
- `validate.py:187-224` checks that derived observations have *some* edge and that nonblank fact IDs resolve. Blank identifiers on an existing edge are outside both assertions.

### Minimally sufficient production repair

Every retained `lineage_edges` row should resolve at least one of `(source_system, filing_id, source_fact_id)`, `input_observation_id`, or `input_population_id`. A filing ID by itself is occurrence metadata, not a row/set referent.

For IOC, use one of two honest representations:

1. If consumers require structured group-member rows, give each summary a shipper-specific population of the source D rows that sum to that shipper's displayed weight. Keep the existing union population for the overall top-five numerator. Each summary then has a specific, redrawable referent.
2. If summaries are display metadata only, remove them from `lineage_edges` and persist them in an explicitly non-lineage structured summary. Do not point all five summaries at the same union population; that pointer would resolve syntactically while remaining semantically non-specific.

Whichever representation is chosen, redraw every top-five population from source facts—including sets above 40 rows—and compare normalized legal names, weights, complete membership digest, aggregate numerator, denominator and output percentage.

## Current gas-margin paths

Exactly 81 `operating_margin_pct` outputs have 162 outer lineage edges whose direct filing/fact fields are blank: 68 Form 2 outputs, 11 Form 2A outputs and two Form 3Q Gas outputs. All 162 edges point to distinct intermediate revenue/income observations. Those intermediates have 550 terminal lineage edges, and all 550 resolve to real source facts under same-entity filings:

| Regime | Margin outputs | Intermediate inputs | Terminal facts |
|---|---:|---:|---:|
| Form 2 | 68 | 136 | 476 |
| Form 2A | 11 | 22 | 66 |
| Form 3Q Gas | 2 | 4 | 8 |
| **Total** | **81** | **162** | **550** |

The intermediate calculations include two- and four-filing Q4 derivations (`q4_monthly_sum`, `q4_minus_quarters`, `q4_annual_minus_q3_ytd`, `sequential_ytd`). A singular direct source ID would be false for those nodes. `ferclib/xbrl_adapter.py:1311-1327` correctly records every filed term on the intermediate; `ferclib/xbrl_adapter.py:1443-1458` correctly points each ratio edge to the intermediate. Acceptance should traverse that graph recursively, reject cycles/dangling nodes, and require every terminal to resolve—not demand redundant singular IDs at every layer.

The remaining applied-data defect is scope. Every one of the 162 current outer paths has an intermediate scope different from the output scope. The audited database stores the registry instruction as output scope and has no `observations.scope_rule` column. Candidate code now separates actual `scope` from `scope_rule` at `ferclib/xbrl_adapter.py:354-388`, carries `common_scope` into derived intermediates at lines 1293-1306, and uses the numerator's supported scope at lines 1426-1441; candidate schema declares the new column at `ferclib/schema.sql:374-384`. This repair exists in code/schema but is not applied to the audited database. It needs a disposable migration or full rebuild plus regenerated exports.

Complete output, intermediate and terminal identifiers are in `gas_margin_missing_direct_source_paths.csv` and `gas_margin_terminal_fact_edges.csv`; the six derivation/regime aggregates are in `gas_margin_path_summary.csv`.

## Global production invariant

Add one post-build validator (and preferably a schema `CHECK` after migration compatibility is established) with these rules:

1. Every edge has at least one resolving referent: exact source fact, input observation, or population. A nonblank filing alone does not pass.
2. Every nonblank source fact resolves on source system + filing occurrence + fact ID; every nonblank observation and population resolves.
3. Observation references form an acyclic graph and every path ends in one or more exact facts or reproducible populations.
4. Redundant direct IDs, when supplied beside an input observation, agree with that observation/occurrence. Blank redundant IDs are allowed for a genuinely multi-source intermediate.
5. Edge/output/input period, unit, regulatory scope and filing-version semantics agree with the declared operation.

The current schema gives only `input_population_id` an FK (`ferclib/schema.sql:433-459`). `validate.py:187-224` and `ferclib/staging.py:551-613` do not yet implement the full invariant. `build_resolution.py:124-166` describes the 2,004 rows as harmless summaries while `_DATA_CHECKS` at lines 342-352 still requires the raw no-input count to be zero; those two acceptance interpretations must be made consistent.

## Targeted acceptance tests

The following cases should be added after the production representation is chosen. Each negative needs the unchanged positive fixture alongside it so a validator that rejects everything cannot pass.

1. **IOC direct referent:** build a top-five observation and assert every retained `group_member` resolves to its shipper-specific set; a row with only filing/name/value fails.
2. **IOC population deletion:** remove the collective population and contributing rows while leaving denominator and descriptions; validation must fail.
3. **IOC summary mutation:** alter one shipper name or weight without changing source facts; redraw must fail.
4. **IOC large-set mutation:** use more than 40 contributing rows and alter a member outside the stored 25-member sample; full redraw/digest validation must fail.
5. **IOC positive redraw:** the unmodified fixture passes names, weights, complete digest, numerator, denominator and percentage.
6. **Gas recursive positive:** two filed quarters feed a derived quarter, which feeds a margin; blank redundant outer fact IDs pass because all terminal facts resolve.
7. **Gas missing terminal:** delete one terminal edge/fact from that graph; recursive validation fails.
8. **Gas cycle/dangling node:** a missing or cyclic `input_observation_id` fails.
9. **Gas false singular source:** attach one arbitrary filing to a four-filing intermediate and require the occurrence/coherence validator to reject it.
10. **Scope application:** after migration/rebuild, actual common scope is stored in `scope`, the registry instruction is stored in `scope_rule`, and an incompatible pair is gated with no lineage/output presented as valid.

## Files produced

- `implementation_logs/lineage_analysis/LINEAGE_ANALYSIS.json`: machine-readable conclusion and counts.
- `implementation_logs/lineage_analysis/ioc_top5_independent_redraw.json`: per-output independent redraw and mismatch list.
- `implementation_logs/lineage_analysis/recompute_ioc_top5_lineage.py`: runnable independent checker.
- `implementation_logs/lineage_analysis/lineage_queries.sql` and `historical_lineage_queries.sql`: exact read-only SQL.
- CSV files in the same directory: complete record IDs plus summary counts for every population described above.

No production files or tests were edited by this lineage work. A deliberately failing regression was not added before the production representation was selected; the ten cases above are the acceptance contract for that change.
