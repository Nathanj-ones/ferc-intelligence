"""
Shared eCollection client: the submission index and XBRL instance retrieval.

Used by both the gas and liquids adapters, so the 38k-row index is fetched ONCE
per refresh and shared, never once per asset or per worker.

Endpoints (all keyless and public; the /api/v1 surface 401s on every route and is
not used):
    /api/PublicSubmissionHistory        the whole submission index
    /api/SubmissionDetail/{filingID}    file list for one filing
    /api/DownloadDocument/{fileID}/1    the XBRL instance bytes
    /api/DownloadDocument/{fileID}/3    the HTML rendering
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re

from .http import Client, FetchError

BASE = "https://ecollection.ferc.gov/api"
SOURCE_SYSTEM = "eCollection_XBRL"

GAS_FORMS = {"Form 2", "Form 2A", "Form 3Q Gas"}
LIQUID_FORMS = {"Form 6", "Form 6Q"}
ANNUAL_FORMS = {"Form 2", "Form 2A", "Form 6"}

QUARTER_END = {"Q1": "-03-31", "Q2": "-06-30", "Q3": "-09-30", "Q4": "-12-31"}
QUARTER_START = {"Q1": "-01-01", "Q2": "-04-01", "Q3": "-07-01", "Q4": "-10-01"}


class Index:
    """The submission index, fetched once and queried many times."""

    def __init__(self, client: Client):
        self.client = client
        self._rows: list[dict] | None = None
        self.retrieved_at = ""
        self.content_hash = ""

    def load(self, *, use_cache: bool = True) -> list[dict]:
        if self._rows is not None:
            return self._rows
        body, entry = self.client.get(f"{BASE}/PublicSubmissionHistory",
                                      source_system=SOURCE_SYSTEM, use_cache=use_cache,
                                      accept="application/json")
        import json
        self._rows = json.loads(body.decode("utf-8"))
        self.retrieved_at = entry["last_seen_at"]
        self.content_hash = entry["content_hash"]
        return self._rows

    def form_names(self) -> dict[str, int]:
        from collections import Counter
        return dict(Counter(str(r.get("formName")) for r in self.load()))

    def for_cid(self, cid: str, forms: set[str] | None = None) -> list[dict]:
        return [r for r in self.load()
                if r.get("cid") == cid and (forms is None or r.get("formName") in forms)]

    def inventory(self, cid: str, legal_name: str, forms: set[str],
                  *, year_from: int, year_to: int,
                  filing_ids: set[str] | None = None) -> list[dict]:
        """Filing rows for one filer, with the canonical-version decision.

        Canonical rule: latest submittedOn per (cid, form, year, period). The
        index's `isSuperseded` field is undefined in practice and is not used.
        Every version is retained -- superseded rows carry the reason.
        """
        rows = []
        for r in self.for_cid(cid, forms):
            year, period = int(r["year"]), str(r.get("period") or "")
            if filing_ids is not None:
                if str(r.get("filingID")) not in filing_ids:
                    continue
            elif not (year_from <= year <= year_to):
                continue
            rows.append(r)

        groups: dict[tuple, list[dict]] = {}
        for r in rows:
            groups.setdefault((r["formName"], int(r["year"]), str(r["period"])), []).append(r)

        inv = []
        for (form, year, period), grp in sorted(groups.items(),
                                                key=lambda x: (x[0][1], x[0][2], x[0][0])):
            grp.sort(key=lambda r: str(r.get("submittedOn") or ""))
            newest = grp[-1]
            for r in grp:
                canonical = r["filingID"] == newest["filingID"]
                if len(grp) == 1:
                    reason = "only filing for this filer/form/period in the index"
                elif canonical:
                    reason = f"latest submittedOn of {len(grp)} versions (isSuperseded unusable)"
                else:
                    reason = f"superseded by filingID {newest['filingID']} (later submittedOn)"
                status = str(r.get("status") or "")
                inv.append({
                    "source_system": SOURCE_SYSTEM,
                    "filing_id": str(r["filingID"]),
                    "entity_key": cid,
                    "filer_legal_name": r.get("companyName") or legal_name,
                    "form": form,
                    "accession_number": r.get("accessionNumber") or None,
                    "reporting_year": year,
                    "reporting_period": period,
                    "period_start": (f"{year}-01-01" if form in ANNUAL_FORMS
                                     else f"{year}{QUARTER_START[period]}"),
                    "period_end": f"{year}{QUARTER_END[period]}",
                    "submitted_on": r.get("submittedOn") or "",
                    "acceptance_status": status,
                    "is_canonical": int(canonical),
                    "canonical_reason": reason,
                    "version_count": len(grp),
                    "data_origin": "ferc_migrated" if status == "Migrated" else "native_xbrl",
                })
        return inv


def attachments(client: Client, filing_id: str) -> list[dict]:
    """The filing's attachment list, from the proven `attachments` key."""
    detail = client.get_json(f"{BASE}/SubmissionDetail/{filing_id}",
                             source_system=SOURCE_SYSTEM)
    if isinstance(detail, list):
        return detail
    return detail.get("attachments") or detail.get("Attachments") or []


def attachment_url(att: dict, kind: int, filing_id: str) -> str:
    """`?filename=` is REQUIRED: the endpoint returns HTTP 400 without it.
    kind 1 = raw XBRL instance, 3 = rendered HTML."""
    name = att.get("fileName") or f"{filing_id}.{'xbrl' if kind == 1 else 'html'}"
    return f"{BASE}/DownloadDocument/{att['fileID']}/{kind}?filename={name}"


def instance_url(client: Client, filing_id: str) -> tuple[str, str]:
    """(download url, file id) for a filing's XBRL instance."""
    atts = attachments(client, filing_id)
    inst = next((a for a in atts if a.get("fileType") == "XBRL_INSTANCE_FILE"), None)
    if inst is None:
        # some migrated filings label the type differently; match on the name
        inst = next((a for a in atts
                     if "XBRL" in str(a.get("fileType", "")).upper()
                     or str(a.get("fileName", "")).lower().endswith((".xbrl", ".xml"))), None)
    if inst is None:
        raise FetchError(f"{BASE}/SubmissionDetail/{filing_id}",
                         f"no XBRL instance attachment listed "
                         f"(types: {sorted({str(a.get('fileType')) for a in atts})})")
    return attachment_url(inst, 1, filing_id), str(inst["fileID"])


def fetch_instance(client: Client, filing_id: str) -> tuple[bytes, dict, str]:
    """(bytes, cache entry, source url). Cached by content, so a re-run costs
    nothing and a byte-identical resubmission is visible as such."""
    url, _fid = instance_url(client, filing_id)
    body, entry = client.get(url, source_system=SOURCE_SYSTEM, accept="application/xml")
    if not body.lstrip().startswith(b"<"):
        raise FetchError(url, "instance body is not XML")
    return body, entry, entry["source_url"]


def fetch_rendering(client: Client, filing_id: str) -> tuple[bytes, dict] | None:
    """The official rendered HTML form, used as schedule-applicability evidence."""
    atts = attachments(client, filing_id)
    ren = next((a for a in atts if a.get("fileType") == "HTML_RENDERING"), None)
    if ren is None:
        return None
    return client.get(attachment_url(ren, 3, filing_id), source_system=SOURCE_SYSTEM,
                      accept="text/html")


def fact_id(file_sha: str, order: int, f: dict) -> str:
    """The verified source-fact identity algorithm, unchanged.

    sha256 over immutable source bytes plus in-document position, so it is
    stable across re-parses, unique within a filing, and independent of file
    paths and row order. Content identity REPEATS across byte-identical
    resubmissions by design -- that is why the occurrence key also carries the
    source system and filing ID.
    """
    s = "|".join([file_sha, str(order), f["qname"], f["context_ref"], f["unit_ref"],
                  f["decimals"], f["precision"], "nil" if f["nil"] else "value", f["value"]])
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def taxonomy_version(ns: str) -> str:
    m = re.search(r"/form/(\d{4}-\d{2}-\d{2})/ferc$", ns or "")
    return m.group(1) if m else ""


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
