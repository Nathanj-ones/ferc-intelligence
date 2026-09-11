# Integrated operating-assets repair report

Record boundary: Build A candidate generated at `2026-09-09T18:02:08+00:00`. This is an implementation-owner self-test record, not a new independent audit and not a post-package receipt.

## Outcome

The Build A database passed SQLite integrity and foreign-key checks, the consumer exports matched one hash-bound published generation, integrated validation had no failures or errors, and every final-audit issue plus all 34 exception IDs was mapped to successful non-zero test evidence. Open source/application states remain explicit.

| Verdict area | Build A verdict | Basis |
|---|---:|---|
| Runtime safety | accepted for candidate | Successful complete suite(s), validation, no running run/checkpoint state |
| Semantic validity | accepted or explicitly gated | Database/export agreement, coverage recomputation, seven annotations and four Fayetteville warnings |
| Source completeness | qualified | 10 of 13 recovered inputs applied; 3 remain explicit; 1 repair-discovered accession package applied; 1 mandatory eLibrary dependency bundle applied; 3 captured search responses accepted as diagnostic evidence rather than final-plan execution prerequisites; 0 remain qualified |
| Offline reproduction | not tested at this boundary | Clean-extraction Build B is a post-package gate |
| Release integrity | not tested at this boundary | Archive/receipt do not yet exist |
| Local integration | not promoted by this generator | Promotion requires a separate active-writer check and rollback boundary |

## Exact database and publication state

- Database: `814325760` bytes, SHA-256 `7ea0d0dbdf505f67dd4d0b18b61c310f28ee84ef51a3b453c6bdc0f74862b13c`.
- Publication generation: `c44f6f6d772e4c3f8173867e221ee113b372e3259b48e87376113f11c5418cc6`; code snapshot `b5bcb12f99c231eac40537af64aa712ccd1044bd553cc9d3f18785ebfe068ea9`; input snapshot `4d8cf2c176ac6c8b75586eb2a545c612a45eb5853d3e9d92321b9a0464eaf7fc`; database semantic identity `003c240db38acf65f2c236cdaf010d9371a0e6dcaa7206567b304f182ef0d437`.
- Integrated validation: 30 pass, 0 fail, 0 error, 0 skipped.

## Coverage bridge

| Measure | Audited frozen baseline | Build A candidate | Change |
|---|---:|---:|---:|
| All slots | 26191 | 29458 | +3267 |
| Core slots | 25304 | 24607 | -697 |
| Core populated | 21183 | 4617 | -16566 |
| Core validated | 20563 | 4056 | -16507 |

Full-plan core populated coverage is 4617 / 24607 = 18.7630%; validated coverage is 4056 / 24607 = 16.4831%. Due-to-date core populated coverage is 4617 / 22551 = 20.4736%; validated is 4056 / 22551 = 17.9859%. Excluded future slots: 2056.

## Field reconciliation

The generated crosswalk accounts for 166 original audit rows through 167 mapping rows and exactly 168 current template/field targets. The requirements crosswalk contains 146 source rows. No lost or partially-lost disposition is accepted.

## Findings, exceptions and inputs

- Repair ledger: 64 audited rows plus 33 related repair-time findings (97 total). Audited-row dispositions: `{"contradicted_with_reproducible_counterevidence": 1, "fixed_and_verified_cached_failure_classification": 1, "fixed_and_verified_exact_ioc_header_unit_population": 1, "fixed_and_verified_in_build_a_candidate": 45, "fixed_and_verified_with_bounded_counterevidence": 1, "fixed_with_build_a_readiness_dimensions": 1, "fixed_with_reproducible_metadata_http_counterevidence": 1, "implementation_claim_remains_open_pending_specific_evidence": 1, "implemented_with_explicit_open_data_or_source_limit": 10, "resolved_by_same_population_reconciliation": 1, "safely_gated_hash_bound_offline_ocr": 1}`.
- Original exceptions: 34 unique rows; 28 accepted as resolved or safely gated and 6 retained as explicit open limitations.
- Final input inventory: 13 unique official-FERC request identities; 10 applied and 3 captured but unapplied.
- Additional repair-time input inventory: 5 separately labelled identities; 1 accession package applied to Build A, 1 mandatory eLibrary dependency bundle applied to Build A, 3 bounded search responses retained as exact diagnostic evidence but not selected by the versioned proceeding seed, and 0 remain qualified.

A safely gated item is limited to its demonstrated capability. In particular, the A17 gate binds two exact image-only FERC PDFs and page renders, runs the local Vision OCR production path, and requires the extracted values, units, subjects and caveats to agree with the preserved independent manual review.

## Test execution

| Run | Role | Interpreter | Executed | Pass | Fail | Error | Skip | Xfail | Exit |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| build-a-cpython314-complete | acceptance | CPython 3.14.7 (/opt/homebrew/bin/python3.14) | 831 | 831 | 0 | 0 | 0 | 0 | 0 |
| build-a-cpython39-complete | acceptance | CPython 3.9.6 (/usr/bin/python3) | 831 | 831 | 0 | 0 | 0 | 0 | 0 |

Counts are per execution and are not added into a purported unique-test total. Each command, interpreter, exit code, full-log identity and skip/xfail classification is in `FINAL_TEST_EVIDENCE_INDEX.json`.

## Remaining boundaries

This record does not certify archive construction, clean-extraction Build B, semantic A/B/package equality, or local promotion. Those later operations must consume these unchanged candidate bytes and issue external hash-bound receipts; changing the payload requires regeneration.
