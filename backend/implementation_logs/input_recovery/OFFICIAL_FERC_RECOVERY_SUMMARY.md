# Official FERC recovery pass

Completed: `2026-09-09T16:29:56.843708+00:00`

This is new official-FERC evidence collected after the audit. It does not change the finding that these inputs were absent from the frozen and checked local cache indexes.

Exact command: `/usr/bin/python3 -B /Users/nathanjones/Desktop/ferc/outputs/codex_integrated_repair_20260909_lokqmf/candidate/implementation_logs/input_recovery/recover_official_ferc.py`

The helper was inspected before execution (AST valid; 17,046 bytes; SHA-256 `bc47a846e6daa5b0e510ea39ca505415faa8c9db3e95a5aff2404d78c1a4140e`; no project imports or subprocesses; official host and authorized write roots only).

- Exact accessions/list GETs: 13 / 13 HTTP 200
- Attachment POSTs: 13 attempted once; 12 HTTP 200
- Raw official attachment ZIPs captured and independently CRC/hash checked: 12
- Metadata-only unresolved attachment failures: 1
- Consumer replay/database/export application: not performed
- Implementation status: all 13 remain pending

## Unresolved retrieval

`20231229-5212` returned a public 10-item list, then the one permitted batch attachment request returned HTTP 500 with a 61-byte official JSON error body. This is not evidence that the source is unavailable, and no identical retry was made.
