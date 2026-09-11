# Adapter contract

Every adapter is a module in `adapters/` exposing exactly three functions. The
lead integrator owns `ferclib/` and the staging database; adapters never edit
shared code, never open the database directly and never open a socket except
through `ctx.client`.

```python
def retrieve(ctx, entity, *, year_from: int, year_to: int) -> list[dict]:
    """Fetch + persist raw source records. Return a list of filing dicts."""

def freeze_expected(ctx, entity, filings: list[dict], assets: list[dict]) -> list[dict]:
    """Return frozen expected coverage slots, BEFORE looking at any result."""

def canonicalise(ctx, entity, filings, expected) -> tuple[list[dict], list[dict]]:
    """Return (observations, lineage_edges)."""
```

`adapters/gas_xbrl.py` is the worked reference. Read it before writing a new one.

## What `ctx` gives you

| Attribute | Use |
|---|---|
| `ctx.client` | `.get(url, source_system=…)`, `.get_json(…)`, `.post_json(url, payload, source_system=…)`. Retries, backoff, `Retry-After`, HTML-as-200 rejection, content-addressed caching and credential redaction are already handled. Never use `urllib` directly. |
| `ctx.staging` | `.write_filing_bundle(...)`, `.checkpoint(...)`, `.is_done(...)`, `.open_blocker(...)`, `.classify_version(...)`, `.query(sql, params)`. Do NOT write `observations` yourself — return them. |
| `ctx.applicability` | `.status(form, taxonomy_version, concept)` → `("yes"/"no"/"unknown", page, evidence)` |
| `ctx.index` | the shared eCollection submission index, fetched once per run |
| `ctx.log(level, msg, adapter=…, entity_cid=…)` | |
| `ctx.force`, `ctx.workdir` | ignore checkpoints; scratch directory |

`entity` is `{"entity_key", "legal_name", "template", "parent", "ticker", "assets": [...]}`.
`entity_key` is a FERC CID, or a reviewed local key beginning `NO-FERC-CID:`.

## Non-negotiable rules

1. **Blank is not zero.** A missing value gets an `Availability` that says which
   kind of missing it is. Never substitute 0, and never substitute a component
   for a total.
2. **Period grain is identity.** Use `ferclib.periods`. A year-to-date fact can
   never satisfy a requested quarter. A Q1 YTD interval *is* the Q1 quarter and
   that is the only permitted equivalence.
3. **A blank filed quarter and a valid derived quarter are different
   observations.** Emit both; never let one overwrite the other.
4. **Five status dimensions**, from `ferclib.status`: `availability`, `origin`,
   `method`, `version_status`, `validation`. Never collapse them.
5. **Warnings propagate.** If an input carries a validation in
   `Validation.MUST_PROPAGATE`, anything derived from it inherits that flag.
6. **Derived values need lineage.** Every derived observation returns edges to
   every input, including denominators.
7. **Exact occurrence key** is `(source_system, filing_id, source_fact_id)`.
   Content hashes do not replace filing IDs.
8. **Scope is part of the value.** Never build a ratio across two different
   scopes — refuse it and say why.
9. **Our gaps are ours.** `Availability.NOT_IMPLEMENTED` for unfinished
   engineering; a source condition is never labelled as one, and vice versa.
10. **Every frozen slot ends in a measured status with a reason.** See
    `_account_for_remainder` in the gas adapter.
11. **Failure containment.** One entity's blocker must not stop the others.
    Catch, `ctx.staging.open_blocker(...)`, continue.
12. **Idempotence.** Running twice must produce identical rows. Observation IDs
    come from `ferclib.staging.observation_id(...)` over the full grain.
13. **Never print or persist a credential.** Use `ferclib.http.api_key(...)` at
    the point of use only; every recorded URL goes through `redact()`.

## Verified source facts (from live discovery, 7 September 2026)

**eCollection** — `?filename=` is REQUIRED on `DownloadDocument` (HTTP 400
without it). Attachments are under `detail["attachments"]`, instance is
`fileType == "XBRL_INSTANCE_FILE"`, rendered form is `HTML_RENDERING`.

**eLibrary** — search `POST /eLibraryWebAPI/api/Search/AdvancedSearch`;
attachments `GET /api/File/GetFileListFromP8/{accession}` → `DataList`; download
`POST /api/File/DownloadP8File` with Origin+Referer headers. Single file returns
raw bytes, multiple returns a ZIP — sniff magic bytes. `classTypes` filtering
requires `[{"documentClass":…,"documentType":…}]`; a string form is SILENTLY
IGNORED and returns unfiltered hits wearing a filter. `acesssionNumber` really
does have three s's. `availCode` N → 401 (non-public); record and move on.

**data.ferc.gov** — base `https://api.data.ferc.gov/v1`, needs the key.
549D datasets are 28 (respondent, 6,808 rows) and 29 (shipper/contract, 113,255
rows), re-verified live. A single unpaged `/dataset/{id}/data/` call returns
everything (`has_more=false`, matches `row_count`). Fetch each table ONCE per
refresh and share it — never per asset.
