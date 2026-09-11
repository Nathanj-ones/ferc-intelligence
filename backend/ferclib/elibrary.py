"""
The single accession-led eLibrary client. Both document adapters use this and
nothing else; neither opens a socket of its own.

Everything here was verified live against elibrary.ferc.gov on 7 September 2026
(see discovery/docs_discovery.md). Four behaviours are load-bearing and are
enforced in code rather than left to caller discipline:

  1. ``classTypes`` MUST be a list of ``{"documentClass": ..., "documentType": ...}``
     objects. The string form ``"Order/Opinion|Commission Order/Opinion"`` is
     SILENTLY IGNORED and returns an unfiltered result set wearing a filter's
     clothes (1,161 hits instead of 2 on CP11-72). ``validate_class_types``
     raises rather than let that reach the wire.

  2. ``totalHits`` IS NOT A STOP CONDITION. RP24-1035 reports 262 and yields 200
     distinct accessions over three overlapping pages; RP18-1126's single most
     important order appears only on page 2 of a search reporting 180 hits.
     Pagination stops on a short page or on a page contributing no new
     accession, never on a count.

  3. ``acesssionNumber`` really is spelled with three s's, ``classTypes`` is an
     ARRAY OF OBJECTS and a filing routinely carries two or more pairs.

  4. THE SAME FILING EXISTS UNDER TWO OR THREE ACCESSIONS, one per availability
     band (P public / C CEII / N non-public CUI//PRIV). ``dedupe_availability``
     collapses them to the most-public copy and keeps the others as recorded
     twins, because counting them separately double-counts every event.

Download responses are always ``application/octet-stream``; the real type is in
the magic bytes. A single file comes back raw, several come back as a ZIP, and
FERC orders are usually Word (.DOCX, or legacy binary .DOC) rather than PDF.
``extract_text`` handles all four with the standard library only, and reports an
honest ``text_layer`` rather than guessing.

Extracted text is a normalised view of the immutable source bytes.  PDF font
programs sometimes emit C0 control codes (including NUL) where a missing glyph
or bullet appeared.  Those codes are not readable source text, break the
declared CPython 3.9 CSV reader, and make SQLite string functions stop at the
first NUL.  ``extract_text`` therefore replaces every non-whitespace C0 control
with a space before spans or observations are built.  The original FERC bytes
remain unchanged in the content-addressed source cache.

Two corrections made on 8 September 2026 while repairing audit issues A10/A19:

  * ``_pdf_text`` used to scrape literal ``(...)`` strings out of EVERY Flate
    stream in the file. Embedded font programs contain such strings, so a modern
    FERC letter came back as several thousand glyph names
    ("DcroatEngHbarTbar...") and nothing else, and the adapter above concluded
    the letter said nothing. Text is now read only from the streams a page
    actually names in ``/Contents``, hex ``<...>`` operands are decoded as well
    as literal ones, and where the producer supplies a ``/ToUnicode`` CMap that
    map is used verbatim. Verified on 20260819-3015: 0 usable characters before,
    the full post-inspection letter after.
  * ``span_supports`` is a shared structural guard. A03/A10 both failed the same
    way -- a stored assertion cited a span that contained a heading, not the
    value. Nothing in this codebase may build a document fact whose verbatim span
    does not contain what the fact asserts.
"""

from __future__ import annotations

import io
import json
import pathlib
import re
import stat
import zipfile
import zlib

from .http import FetchError

SOURCE_SYSTEM = "eLibrary"
BASE = "https://elibrary.ferc.gov/eLibraryWebAPI/api"
SEARCH_URL = f"{BASE}/Search/AdvancedSearch"
FILELIST_URL = f"{BASE}/File/GetFileListFromP8"
DOWNLOAD_URL = f"{BASE}/File/DownloadP8File"

#: citation URLs -- what a reader clicks to see the same record
def docinfo_url(accession: str) -> str:
    return f"https://elibrary.ferc.gov/eLibrary/docinfo?accession_num={accession}"


def filelist_url(accession: str) -> str:
    return f"https://elibrary.ferc.gov/eLibrary/filelist?accession_num={accession}"


# ---------------------------------------------------------------- availability

class Avail:
    PUBLIC = "P"
    CEII = "C"
    NONPUBLIC = "N"
    #: most-public first; the winner of a twin group
    RANK = {PUBLIC: 0, CEII: 1, NONPUBLIC: 2}
    LABEL = {PUBLIC: "public",
             CEII: "CEII (critical energy infrastructure information)",
             NONPUBLIC: "non-public / privileged (CUI//PRIV)"}


#: Class/type pairs whose docket lists are NOT a membership signal. One
#: Procedural Motion service-list row in RP23-863 carried 300+ dockets spanning
#: IS, CP, RP, PR, PL and RM. Linking assets through those fully connects the
#: asset graph, so they are excluded from docket-linkage inference. They are
#: still retrieved and stored -- only the linkage inference is refused.
NO_LINKAGE_TYPES = {
    ("Pleading/Motion", "Procedural Motion"),
    ("Intervention", ""),                       # every type in the class
}


def class_type(document_class: str, document_type: str = "") -> dict:
    """One filter pair. An empty documentType means 'every type in this class',
    which is verified to work (CP12-507 + Application/Petition/Request -> 11
    hits, all of that class) and is the cheapest way to sweep a docket."""
    return {"documentClass": document_class, "documentType": document_type}


class ClassTypeShapeError(ValueError):
    """Raised instead of sending a filter shape the API ignores in silence."""


def validate_class_types(class_types) -> list[dict]:
    """Refuse every shape that does not filter.

    Measured on CP11-72:
      [{"Class":..,"Type":..}]                 -> success:false, null-parameter error
      ["Order/Opinion|Commission Order/Opinion"] -> 1161 hits, filter SILENTLY IGNORED
      [{"documentClass":..,"documentType":..}]  -> 2 hits, correct
    """
    if not class_types:
        return []
    if isinstance(class_types, (str, bytes)):
        raise ClassTypeShapeError(
            "classTypes was given as a string; the API accepts it, ignores it and "
            "returns an UNFILTERED result set. Use class_type(cls, typ).")
    out = []
    for ct in class_types:
        if not isinstance(ct, dict):
            raise ClassTypeShapeError(
                f"classTypes entry {ct!r} is {type(ct).__name__}, not a dict. The string "
                "form is silently ignored by the API and returns unfiltered hits.")
        if "documentClass" not in ct:
            raise ClassTypeShapeError(
                f"classTypes entry {sorted(ct)} lacks 'documentClass'. The GetClassTypes "
                "row shape {'Class','Type','Library','Category'} makes the API return "
                "success:false with 'Value cannot be null. Parameter name: stringToEscape'.")
        if not str(ct["documentClass"]).strip():
            raise ClassTypeShapeError("documentClass may not be blank; that is an unfiltered search.")
        out.append({"documentClass": str(ct["documentClass"]),
                    "documentType": str(ct.get("documentType") or "")})
    return out


def search_body(*, text: str = "", docket: str = "", start: str = "1990-01-01",
                end: str = "2026-12-31", page: int = 0, per_page: int = 100,
                class_types=None, availability=None) -> dict:
    """The exact verified request body.

    ``searchDescription`` must be true only when ``searchText`` is non-empty; a
    docket-scoped query is searchText="" + searchDescription=false. The docket
    number is given WITHOUT its sub-docket suffix ("CP11-72"), which matches
    every sub-docket.
    """
    return {
        "searchText": text,
        "searchFullText": False,
        "searchDescription": bool(text),
        "dateSearches": [{"dateType": "filed_date", "startDate": start, "endDate": end}],
        "availability": availability,
        "affiliations": [], "categories": [], "libraries": [],
        "accessionNumber": None, "eFiling": False,
        "docketSearches": [{"docketNumber": docket, "subDocketNumbers": []}],
        "resultsPerPage": per_page, "curPage": page,
        "classTypes": validate_class_types(class_types),
        "sortBy": "", "groupBy": "NONE", "idolResultID": "", "allDates": False,
    }


# ---------------------------------------------------------------- hit shape

def docket_base(docket: str) -> str:
    """'CP11-72-000' -> 'CP11-72', and 'IS24-810' -> 'IS24-810'.

    The sub-docket suffix is the THIRD hyphen-separated segment, never the
    second. Stripping any trailing three digits turns 'IS24-810' into 'IS24' and
    'CP12-507' into 'CP12', which silently merges unrelated proceedings.
    """
    d = (docket or "").strip().upper()
    parts = d.split("-")
    if len(parts) >= 3 and re.fullmatch(r"\d{3}", parts[-1]):
        return "-".join(parts[:-1])
    return d


def normalise_hit(h: dict) -> dict:
    """One search hit, in the shape the adapters use.

    ``classTypes`` is flattened to a SET of "Class::Type" strings because a
    single filing routinely carries two or more pairs and the second pair is
    sometimes the whole signal (a FERC inspection letter that also carries
    ``FERC Correspondence With Applicant::Compliance Directives`` is an
    inspection with unresolved conditions).
    """
    pairs = [(str(c.get("documentClass") or ""), str(c.get("documentType") or ""))
             for c in (h.get("classTypes") or []) if isinstance(c, dict)]
    dockets = [str(d) for d in (h.get("docketNumbers") or [])]
    return {
        "accession": str(h.get("acesssionNumber") or ""),       # three s's, verbatim
        "document_id": str(h.get("documentId") or ""),
        "description": (h.get("description") or "").strip(),
        "filed_date": _iso(h.get("filedDate")),
        "issued_date": _iso(h.get("issuedDate")),
        "posted_date": _iso(h.get("postedDate")),
        "class_pairs": pairs,
        "class_types": ["::".join(p) for p in pairs],
        "avail_code": str(h.get("availCode") or ""),
        "category": str(h.get("category") or ""),
        "libraries": [str(x) for x in (h.get("libraries") or [])],
        "dockets": dockets,
        "docket_bases": sorted({docket_base(d) for d in dockets if d}),
        "transmittals": [
            {"file_id": str(t.get("fileId") or ""), "file_name": str(t.get("fileName") or ""),
             "file_type": str(t.get("fileType") or ""), "byte_size": int(t.get("fileSize") or 0)}
            for t in (h.get("transmittals") or []) if isinstance(t, dict)],
        "affiliations": [str(a.get("affiliation") or "")
                         for a in (h.get("affiliations") or []) if isinstance(a, dict)],
    }


def _iso(v) -> str:
    """FERC returns 'MM/DD/YYYY' here and ISO elsewhere. Never guess: return ''
    when the shape is not one of the two known ones."""
    s = str(v or "").strip()
    if not s:
        return ""
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    return m.group(0) if m else ""


def links_assets(hit: dict) -> bool:
    """Whether this filing's docket list may be used to link assets."""
    for cls, typ in hit["class_pairs"]:
        if (cls, typ) in NO_LINKAGE_TYPES or (cls, "") in NO_LINKAGE_TYPES:
            return False
    return True


def _twin_key(hit: dict) -> tuple:
    """Filings that are the same submission under different availability bands.

    Keyed on filed date plus a normalised description prefix: 20260803-5167 (P)
    and 20260803-5168 (N) are one Sabine Pass semi-annual report, and counting
    both doubles every event derived from it.
    """
    desc = re.sub(r"\s+", " ", hit["description"].lower()).strip()
    desc = re.sub(r"\(?non-?public( twin)?\)?", "", desc).strip()
    return (hit["filed_date"], desc[:140])


def _merge_occurrence_capture_times(left: dict, right: dict) -> dict:
    """Return ``left`` with occurrence-capture bounds merged from ``right``.

    One accession can appear on overlapping result pages and in several docket
    searches.  Those are repeated sightings of the same filing occurrence, not
    competing filing versions.  Its first observation is therefore the
    earliest captured ``first_seen_at`` and its retrieval boundary is the
    latest captured ``retrieved_at``.  Both values come from the persisted
    source-cache index; no replay clock participates.  Keeping every other
    field from ``left`` preserves the established source-result precedence.

    Cache timestamps are emitted as fixed-width UTC ISO-8601 strings, so their
    lexical and chronological order are identical.  Blank or absent values are
    unknown and never outrank a captured value.
    """
    merged = dict(left)
    first_values = [str(value).strip() for value in (
        left.get("first_seen_at"), right.get("first_seen_at")) if value]
    retrieved_values = [str(value).strip() for value in (
        left.get("retrieved_at"), right.get("retrieved_at")) if value]
    if "first_seen_at" in left or "first_seen_at" in right:
        merged["first_seen_at"] = min(first_values) if first_values else None
    if "retrieved_at" in left or "retrieved_at" in right:
        merged["retrieved_at"] = max(retrieved_values) if retrieved_values else None
    return merged


def dedupe_availability(hits: list[dict]) -> tuple[list[dict], list[dict]]:
    """(canonical, twins). The most-public copy of each submission wins.

    Nothing is discarded: the twins are returned so the adapter can record the
    non-public accession explicitly as ``Availability.NONPUBLIC`` -- which is a
    source condition, not a missing source and not a retrieval failure.
    """
    # Collapse EXACT accession duplicates first. `search` dedupes within one
    # call, but the same filing legitimately appears in two docket sweeps, and
    # without this it would end up both canonical and its own availability twin.
    by_accession: dict[str, dict] = {}
    for h in hits:
        accession = h["accession"]
        if accession in by_accession:
            by_accession[accession] = _merge_occurrence_capture_times(
                by_accession[accession], h)
        else:
            by_accession[accession] = dict(h)
    groups: dict[tuple, list[dict]] = {}
    for h in by_accession.values():
        groups.setdefault(_twin_key(h), []).append(h)
    canonical, twins = [], []
    for key, group in groups.items():
        group = sorted(group, key=lambda x: (Avail.RANK.get(x["avail_code"], 3), x["accession"]))
        best = dict(group[0])
        others = group[1:]
        best["availability_twins"] = [
            {"accession": o["accession"], "avail_code": o["avail_code"]} for o in others]
        canonical.append(best)
        for o in others:
            o = dict(o)
            o["twin_of"] = best["accession"]
            twins.append(o)
    canonical.sort(key=lambda x: (x["filed_date"], x["accession"]), reverse=True)
    twins.sort(key=lambda x: (x["filed_date"], x["accession"]), reverse=True)
    return canonical, twins


# ---------------------------------------------------------------- the client

def cache_first_seen_at(entry: dict) -> str | None:
    """Return when this immutable cache entry was first captured, if known."""
    value = str((entry or {}).get("first_seen_at") or "").strip()
    return value or None


def cache_retrieved_at(entry: dict) -> str | None:
    """Return the persisted capture time for one immutable cache object.

    ``last_seen_at`` records the most recent real retrieval represented by the
    frozen cache index.  Falling back to ``first_seen_at`` keeps older valid
    indexes usable.  Missing metadata remains unknown; an offline replay must
    never manufacture a source retrieval time from its wall clock.
    """
    for field in ("last_seen_at", "first_seen_at"):
        value = str((entry or {}).get(field) or "").strip()
        if value:
            return value
    return None


class Elibrary:
    """Accession-led. Every method takes or returns an accession number.

    All traffic goes through ``ferclib.http.Client``: retries, Retry-After,
    HTML-as-200 rejection, the shared request budget, content-addressed caching
    and credential redaction are handled there and are not re-implemented.
    """

    def __init__(self, client, log=None):
        self.client = client
        self._log = log or (lambda level, msg: None)
        self.searches = 0
        self.filelists = 0
        self.downloads = 0

    # ------------------------------------------------------------ search

    def search(self, *, text: str = "", docket: str = "", start: str = "1990-01-01",
               end: str = "2026-12-31", class_types=None, per_page: int = 100,
               max_pages: int = 3, use_cache: bool = True) -> list[dict]:
        """Paginate until a short page or a page with no new accession.

        ``totalHits`` is deliberately not consulted as a stop condition. Pages
        overlap: RP24-1035 gives 100+100+100 rows and only 200 distinct
        accessions.
        """
        seen: dict[str, int] = {}
        out: list[dict] = []
        for page in range(max_pages):
            body = search_body(text=text, docket=docket, start=start, end=end,
                               page=page, per_page=per_page, class_types=class_types)
            raw, entry = self.client.post_json(SEARCH_URL, body,
                                               source_system=SOURCE_SYSTEM,
                                               use_cache=use_cache)
            # ``retrieved_at`` is source-capture metadata, not the time an
            # offline replay happened to read the immutable cache.  Carry the
            # cache entry's persisted observation time with every occurrence
            # so downstream filing/document rows reproduce exactly.
            first_seen_at = cache_first_seen_at(entry)
            retrieved_at = cache_retrieved_at(entry)
            self.searches += 1
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FetchError(SEARCH_URL, f"search body is not JSON: {exc}") from None
            if not data.get("success", True):
                raise FetchError(SEARCH_URL,
                                 f"search rejected: {data.get('errorMessage')!r} "
                                 f"(docket={docket!r} text={text!r})")
            rows = data.get("searchHits") or []
            fresh = 0
            for h in rows:
                hit = normalise_hit(h)
                hit["first_seen_at"] = first_seen_at
                hit["retrieved_at"] = retrieved_at
                accession = hit["accession"]
                if not accession:
                    continue
                if accession in seen:
                    index = seen[accession]
                    out[index] = _merge_occurrence_capture_times(out[index], hit)
                    continue
                seen[accession] = len(out)
                out.append(hit)
                fresh += 1
            if len(rows) < per_page or fresh == 0:
                break
        return out

    # ------------------------------------------------------------ attachments

    @staticmethod
    def browser_headers(accession: str) -> dict:
        """Origin/Referer exactly as the eLibrary UI sends them.

        Verified live 2026-09-07 that DownloadP8File currently succeeds WITHOUT
        them; they are sent anyway so the adapter keeps working if FERC tightens
        CSRF checking. ``ferclib.http.Client`` merges them over its defaults.
        """
        return {"Origin": "https://elibrary.ferc.gov",
                "Referer": filelist_url(accession)}

    def file_list(self, accession: str, *, use_cache: bool = True) -> list[dict]:
        """``GET /api/File/GetFileListFromP8/{accession}`` -> ``DataList``.

        ``ID`` is the download key. ``Description`` is the FILING's description
        repeated on every row and is not a per-file name.  Current P8 responses
        also expose ``Orig_File_Name``/``FileDescription``; keep those when
        present, while still treating ZIP entry names as the authoritative name
        of each byte stream.
        """
        url = f"{FILELIST_URL}/{accession}"
        raw, _entry = self.client.get(url, source_system=SOURCE_SYSTEM, use_cache=use_cache,
                                      accept="application/json",
                                      headers=self.browser_headers(accession))
        self.filelists += 1
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FetchError(url, f"GetFileListFromP8 body is not JSON: {exc}") from None
        rows = data.get("DataList") or []
        errors = data.get("ErrorList") or []
        if errors and not rows:
            raise FetchError(url, f"GetFileListFromP8 returned ErrorList={errors!r}")
        return [{"attachment_id": str(r.get("ID") or ""),
                 "media_type": str(r.get("MimeType") or ""),
                 "avail_code": str(r.get("Availability_Mode") or r.get("Availability_Code") or ""),
                 "transmittal_type": str(r.get("TransmittalType") or ""),
                 "file_name": str(r.get("Orig_File_Name") or ""),
                 "file_description": str(r.get("FileDescription") or ""),
                 "listed_byte_size": int(r.get("File_Size_Num") or 0),
                 "filing_description": str(r.get("Description") or ""),
                 "accession": str(r.get("Accession_Number") or accession)}
                for r in rows if r.get("ID")]

    def download(self, accession: str, attachment_ids: list[str], *,
                 use_cache: bool = True,
                 expected_files: list[dict] | None = None) -> tuple[bytes, dict]:
        """``POST /api/File/DownloadP8File``. One file returns raw bytes, several
        return a ZIP; the Content-Type is always application/octet-stream, so the
        caller must sniff. Raises FetchError with the exact status.

        Origin and Referer are sent as the browser sends them. They were verified
        live to be unnecessary today (20141230-3043 downloaded, 154,293 bytes,
        ZIP, with neither header) and are sent defensively so the adapter survives
        FERC tightening CSRF checking.
        """
        def request(ids: list[str]):
            payload = {"FileType": "", "accession": accession, "fileid": 0,
                       "FileIDAll": "", "fileidLst": list(ids), "Islegacy": False}
            return self.client.post_json(
                DOWNLOAD_URL, payload, source_system=SOURCE_SYSTEM,
                use_cache=use_cache, headers=self.browser_headers(accession))

        ids = list(dict.fromkeys(str(value) for value in attachment_ids if value))
        if not ids:
            raise FetchError(DOWNLOAD_URL, f"{accession}: no attachment IDs supplied")
        # Only misses created by this bounded bulk/single-ID strategy are
        # eligible to be satisfied by a verified complete-accession response.
        # Earlier search, file-list or filing misses remain fatal.
        miss_checkpoint = len(getattr(self.client, "cache_misses", []))
        try:
            blob, entry = request(ids)
            self.downloads += 1
            return blob, entry
        except FetchError as exc:
            if len(ids) == 1:
                raise
            bulk_error = exc

        # FERC's file-list UI sends one ID for an individual-file click.  The
        # P8 backend sometimes ignores that subset and returns the complete
        # accession ZIP even when the equivalent all-ID request fails (observed
        # on public accession 20231229-5212).  This is a distinct official route,
        # not an identical retry.  Accept it only when the returned archive
        # proves that it carries at least the entire declared attachment set.
        individual_errors = []
        for attachment_id in ids:
            try:
                candidate, candidate_entry = request([attachment_id])
            except FetchError as exc:
                individual_errors.append(
                    f"{attachment_id}:{exc.detail} status={exc.status}")
                continue
            if sniff(candidate) != "zip":
                individual_errors.append(
                    f"{attachment_id}:response was {sniff(candidate)}, not a complete ZIP")
                continue
            try:
                with zipfile.ZipFile(io.BytesIO(candidate)) as zf:
                    infos = [info for info in zf.infolist() if not info.is_dir()]
                    unsafe = [info.filename for info in infos
                              if pathlib.PurePosixPath(info.filename).is_absolute()
                              or ".." in pathlib.PurePosixPath(info.filename).parts
                              or "\\" in info.filename
                              or len(pathlib.PurePosixPath(info.filename).parts) != 1
                              or stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)
                              or bool(info.flag_bits & 0x1)]
                    names = [info.filename for info in infos]
                    duplicate = (len(names) != len(set(names))
                                 or len(names) != len({name.casefold() for name in names}))
                    if unsafe or duplicate or len(names) < len(ids):
                        individual_errors.append(
                            f"{attachment_id}:ZIP membership does not represent the "
                            f"declared accession ({len(names)} members, unsafe={bool(unsafe)})")
                        continue
                    declared = [row for row in (expected_files or [])
                                if row.get("attachment_id") in ids]
                    if declared and all(row.get("file_name") and row.get("listed_byte_size")
                                        for row in declared):
                        expected = {str(row["file_name"]): int(row["listed_byte_size"])
                                    for row in declared}
                        actual = {}
                        prefix = f"{accession}_"
                        for info in infos:
                            name = info.filename[len(prefix):] if info.filename.startswith(prefix) \
                                else info.filename
                            actual[name] = info.file_size
                        if len(expected) != len(declared) or actual != expected:
                            individual_errors.append(
                                f"{attachment_id}:ZIP names/sizes disagree with the official "
                                f"file list (expected {len(expected)}, got {len(actual)})")
                            continue
                    # Reading each member is the CRC check.  No extracted file
                    # touches disk and the raw response stays the source object.
                    for info in infos:
                        zf.read(info)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                individual_errors.append(
                    f"{attachment_id}:invalid ZIP ({type(exc).__name__}: {exc})")
                continue
            self.downloads += 1
            entry = dict(candidate_entry)
            entry["download_strategy"] = "single-ID request returned complete accession ZIP"
            entry["requested_attachment_id"] = attachment_id
            entry["declared_attachment_count"] = len(ids)
            resolver = getattr(self.client, "satisfy_cache_misses_since", None)
            if callable(resolver):
                resolver(miss_checkpoint, satisfied_by={
                    "reason": ("verified complete accession ZIP matched every official "
                               "file-list member name and byte size"),
                    "source_url": entry.get("source_url") or DOWNLOAD_URL,
                    "content_hash": entry.get("content_hash") or "",
                })
            return candidate, entry

        detail = (f"{accession}: all-ID DownloadP8File failed ({bulk_error.detail}; "
                  f"status={bulk_error.status}); no individual-ID response supplied a "
                  f"complete {len(ids)}-attachment archive: "
                  + "; ".join(individual_errors[:6]))
        raise FetchError(DOWNLOAD_URL, detail, status=bulk_error.status,
                         attempts=bulk_error.attempts)


# ---------------------------------------------------------------- file sniffing

MAGIC = [
    (b"%PDF", "pdf"),
    (b"PK\x03\x04", "zip"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole2"),
    (b"{\\rt", "rtf"),
]


def sniff(blob: bytes) -> str:
    """Magic bytes only. Content-Type is always application/octet-stream and
    tells you nothing; the extension in Content-Disposition lies often enough
    (a 'zip' that is a raw .docx) that it is not trusted either."""
    head = blob[:16]
    for magic, kind in MAGIC:
        if head.startswith(magic):
            if kind == "zip":
                try:
                    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
                except zipfile.BadZipFile:
                    return "zip_unreadable"
                if "word/document.xml" in names:
                    return "docx"
                if any(n.startswith("xl/") for n in names):
                    return "xlsx"
                return "zip"
            return kind
    if not blob:
        return "empty"
    sample = blob[:4096]
    printable = sum(1 for b in sample if 9 <= b <= 13 or 32 <= b <= 126)
    return "text" if printable > 0.90 * len(sample) else "unknown"


def zip_entries(blob: bytes) -> list[tuple[str, bytes]]:
    """Named members of a ZIP container, largest first. Entry names carry the
    real per-file names ({accession}_{filename}); the file list does not."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        return []
    out = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        try:
            out.append((info.filename, zf.read(info)))
        except (RuntimeError, zipfile.BadZipFile, OSError):
            continue                                  # encrypted or corrupt member
    out.sort(key=lambda kv: len(kv[1]), reverse=True)
    return out


def members(blob: bytes, hint: str = "") -> list[tuple[str, bytes, str]]:
    """(name, bytes, kind) for everything inside a download.

    A single-file download is one member; a multi-file download is a ZIP whose
    members are named; a legacy single order arrives as a ZIP wrapping one .DOC.
    """
    kind = sniff(blob)
    if kind == "zip":
        return [(name, data, sniff(data)) for name, data in zip_entries(blob)]
    return [(hint or "(single file)", blob, kind)]


# ---------------------------------------------------------------- text layer

_PDF_STREAM = re.compile(rb"stream\r?\n")
_PDF_STRING = re.compile(rb"\((?:\\.|[^\\()])*\)", re.S)

# ---------------------------------------------------------------- PDF objects
# Minimal object graph, enough to find the pages and the fonts they use. This is
# deliberately NOT the coordinate-aware reader in adapters/capacity.py: that one
# exists to keep table cells apart by x position, this one exists to recover a
# letter's prose in reading order. They share the same primitives conceptually
# and are kept apart on purpose -- a geometry bug in one must not silently move
# a legal date in the other.


def _pdf_objects(raw: bytes) -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    for m in re.finditer(rb"(\d+)\s+(\d+)\s+obj\b", raw):
        end = raw.find(b"endobj", m.end())
        out[int(m.group(1))] = raw[m.end():end if end > 0 else len(raw)]
    for num, body in list(out.items()):                     # PDF 1.5 object streams
        if b"/ObjStm" not in body:
            continue
        data = _pdf_stream_data(body)
        n, first = _pdf_int(body, b"/N"), _pdf_int(body, b"/First")
        if not data or n is None or first is None:
            continue
        head = data[:first].split()
        for i in range(0, min(len(head) - 1, 2 * n), 2):
            try:
                onum, off = int(head[i]), int(head[i + 1])
            except ValueError:
                continue
            nxt = first + int(head[i + 3]) if i + 3 < len(head) else len(data)
            out.setdefault(onum, data[first + off:nxt])
    return out


def _pdf_int(body: bytes, key: bytes):
    m = re.search(key + rb"\s+(\d+)", body)
    return int(m.group(1)) if m else None


def _pdf_stream_data(body: bytes) -> bytes:
    m = _PDF_STREAM.search(body)
    if not m:
        return b""
    end = body.find(b"endstream", m.end())
    raw = body[m.end():end if end > 0 else len(body)]
    if b"/FlateDecode" in body[:m.start()]:
        try:
            return zlib.decompressobj().decompress(raw)
        except zlib.error:
            return b""
    return raw


def _pdf_refs(body: bytes, key: bytes) -> list[int]:
    m = re.search(key + rb"\s*(\[[^\]]*\]|\d+\s+\d+\s+R)", body)
    return [int(x) for x in re.findall(rb"(\d+)\s+\d+\s+R", m.group(1))] if m else []


def _pdf_tounicode(obj: bytes) -> dict[int, str]:
    """The producer's own code -> character mapping, when it supplies one."""
    data = _pdf_stream_data(obj)
    if not data:
        return {}
    cmap: dict[int, str] = {}
    for blk in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            s = dst.decode("latin-1")
            cmap[int(src, 16)] = "".join(chr(int(s[i:i + 4], 16))
                                         for i in range(0, len(s) - 3, 4))
    for blk in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for lo, hi, dst in re.findall(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            base, start = int(dst, 16), int(lo, 16)
            for i in range(start, min(int(hi, 16), start + 65535) + 1):
                cmap[i] = chr(base + i - start)
    return cmap


def _pdf_page_fonts(objs: dict[int, bytes], resources: bytes) -> dict[str, dict]:
    m = re.fullmatch(rb"\s*(\d+)\s+\d+\s+R\s*", resources or b"")
    res = objs.get(int(m.group(1)), b"") if m else (resources or b"")
    fm = re.search(rb"/Font\s*(<<.*?>>|\d+\s+\d+\s+R)", res, re.S)
    if not fm:
        return {}
    fdict = fm.group(1)
    rm = re.fullmatch(rb"\s*(\d+)\s+\d+\s+R\s*", fdict)
    if rm:
        fdict = objs.get(int(rm.group(1)), b"")
    out: dict[str, dict] = {}
    for fmatch in re.finditer(rb"/([A-Za-z0-9#+._-]+)\s+(\d+)\s+\d+\s+R", fdict):
        body = objs.get(int(fmatch.group(2)), b"")
        tu = re.search(rb"/ToUnicode\s+(\d+)\s+\d+\s+R", body)
        out["/" + fmatch.group(1).decode("latin-1")] = {
            "two_byte": b"/Identity-H" in body or b"/Type0" in body,
            "cmap": _pdf_tounicode(objs.get(int(tu.group(1)), b"")) if tu else {}}
    return out


_PDF_TOKEN = re.compile(rb"""
    (?P<str>\((?:\\.|[^()\\]|\((?:\\.|[^()\\])*\))*\))
  | (?P<hex><[0-9A-Fa-f\s]*>)
  | (?P<name>/[^\s/\[\]<>(){}]+)
  | (?P<op>T[Jjfd*DLcwz]|'|"|BT|ET)
""", re.VERBOSE)


def _pdf_unescape(s: bytes) -> bytes:
    out, body, i = bytearray(), s[1:-1], 0
    esc = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}
    while i < len(body):
        c = body[i]
        if c == 0x5C and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in esc:
                out.append(esc[nxt]); i += 2
            elif 0x30 <= nxt <= 0x37:
                oct_ = body[i + 1:i + 4]
                k = 0
                while k < 3 and k < len(oct_) and 0x30 <= oct_[k] <= 0x37:
                    k += 1
                out.append(int(oct_[:k], 8) & 0xFF); i += 1 + k
            elif nxt in (10, 13):
                i += 2
            else:
                out.append(nxt); i += 2
        else:
            out.append(c); i += 1
    return bytes(out)


def _pdf_decode(raw: bytes, font: dict) -> str:
    cmap = font.get("cmap") or {}
    if font.get("two_byte"):
        if len(raw) % 2:
            raw += b"\x00"
        codes = [raw[i] << 8 | raw[i + 1] for i in range(0, len(raw), 2)]
    else:
        codes = list(raw)
    return "".join(cmap.get(c) or (chr(c) if c < 0x100 else "") for c in codes)


def _pdf_page_order(objs: dict[int, bytes]) -> list[int]:
    """Page object numbers in READING order, from the catalogue's page tree.

    Object number order is not page order: 20260819-3015 stores its cc-list page
    first, so a numeric sort put the second page of the letter above the first
    and cut the salutation away from the paragraph that answers the question.
    """
    root = next((n for n, b in objs.items() if b"/Catalog" in b), None)
    order: list[int] = []
    seen: set[int] = set()

    def walk(num: int) -> None:
        if num in seen:
            return
        seen.add(num)
        body = objs.get(num, b"")
        if re.search(rb"/Type\s*/Pages", body):
            for k in _pdf_refs(body, b"/Kids"):
                walk(k)
        elif re.search(rb"/Type\s*/Page[^s]", body):
            order.append(num)

    for t in (_pdf_refs(objs.get(root, b""), b"/Pages") if root is not None else []):
        walk(t)
    if order:
        return order
    return sorted(n for n, b in objs.items() if re.search(rb"/Type\s*/Page[^s]", b))


def _pdf_text(blob: bytes) -> str:
    """Text from the streams the PAGES name, in emission order.

    The previous implementation scraped literal strings out of every Flate
    stream in the file, including the embedded font programs, which is why
    20260819-3015 (a two-page FERC post-inspection letter) produced 60,240
    characters of glyph names and not one sentence of the letter.
    """
    objs = _pdf_objects(blob)
    pages = _pdf_page_order(objs)
    if not pages:
        return _pdf_text_legacy(blob)
    out: list[str] = []
    for numb in pages:
        body = objs.get(numb, b"")
        content = b"".join(_pdf_stream_data(objs.get(c, b"")) + b"\n"
                           for c in _pdf_refs(body, b"/Contents"))
        if not content:
            continue
        rm = re.search(rb"/Resources\s*(<<.*?>>|\d+\s+\d+\s+R)", body, re.S)
        fonts = _pdf_page_fonts(objs, rm.group(1) if rm else b"")
        cur: dict = {}
        buf: list[str] = []
        pending_name: bytes | None = None
        for t in _PDF_TOKEN.finditer(content):
            kind, val = t.lastgroup, t.group()
            if kind == "name":
                pending_name = val
                continue
            if kind == "op":
                op = val.decode("latin-1")
                if op == "Tf" and pending_name is not None:
                    cur = fonts.get(pending_name.decode("latin-1"), {})
                elif op in ("Td", "TD", "T*", "TL"):
                    buf.append("\n")
                pending_name = None
                continue
            if kind == "str":
                buf.append(_pdf_decode(_pdf_unescape(val), cur))
            elif kind == "hex":
                hx = re.sub(rb"[^0-9A-Fa-f]", b"", val)
                if hx and len(hx) % 2 == 0:
                    buf.append(_pdf_decode(bytes.fromhex(hx.decode()), cur))
        out.append("".join(buf))
    text = "\n".join(out)
    # A page whose fonts carry no /ToUnicode map and no simple encoding can still
    # come back as noise. Falling back is better than returning nothing, but the
    # fallback is only taken when the page-scoped read produced almost no letters.
    letters = sum(ch.isalpha() for ch in text)
    return text if letters >= 40 else (_pdf_text_legacy(blob) or text)


def _pdf_text_legacy(blob: bytes) -> str:
    """The pre-repair whole-file scrape, kept only as a last resort for producers
    whose page tree this reader cannot follow. Its output is noisy by design and
    it is never preferred over a page-scoped read."""
    chunks = []
    for m in _PDF_STREAM.finditer(blob):
        s = m.end()
        e = blob.find(b"endstream", s)
        if e < 0:
            continue
        raw = blob[s:e]
        try:
            data = zlib.decompress(raw)
        except zlib.error:
            data = raw if raw[:1] in (b"B", b"/", b"q", b"1", b"0") else b""
        if not data:
            continue
        strings = [t.group()[1:-1] for t in _PDF_STRING.finditer(data)]
        if strings:
            chunks.append(b" ".join(strings))
    text = b"\n".join(chunks).decode("latin-1", "replace")
    return re.sub(r"\\([()\\])", r"\1", text)


def _docx_text(blob: bytes) -> str:
    """word/document.xml with paragraph and tab boundaries preserved."""
    try:
        xml = zipfile.ZipFile(io.BytesIO(blob)).read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError):
        return ""
    xml = re.sub(r"<w:tab\b[^>]*/>", "\t", xml)
    xml = re.sub(r"<w:br\b[^>]*/>", "\n", xml)
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    for ent, ch in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                    ("&quot;", '"'), ("&apos;", "'"), ("&#8217;", "'"), ("&#8220;", '"'),
                    ("&#8221;", '"'), ("&#8211;", "-"), ("&#8212;", "--")):
        xml = xml.replace(ent, ch)
    return xml


_RUN = re.compile(rb"[\x20-\x7e\t]{5,}")


def _ole2_text(blob: bytes) -> str:
    """Legacy binary .DOC (OLE2, magic D0CF11E0) has no public stdlib reader.

    A printable-run scrape of the compound file recovers the WordDocument
    stream's text -- verified on the 2004 Sabine Pass order (20041221-3094),
    94,321 characters, including the semi-annual reporting condition quoted in
    the discovery note. Both the single-byte and the UTF-16LE encodings are
    tried and the longer result wins; nothing is repaired or reflowed.
    """
    single = b" ".join(m.group() for m in _RUN.finditer(blob)).decode("cp1252", "replace")
    try:
        wide = blob.decode("utf-16-le", "ignore")
    except UnicodeDecodeError:
        wide = ""
    wide = " ".join(m for m in re.findall(r"[\x20-\x7e\t]{8,}", wide))
    return single if len(single) >= len(wide) else wide


def _rtf_text(blob: bytes) -> str:
    s = blob.decode("latin-1", "replace")
    s = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", s)
    return re.sub(r"[{}]", " ", s)


_NON_TEXT_C0 = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _normalise_extracted_text(text: str) -> str:
    """Return readable text without binary C0 controls.

    Newline, carriage return and tab are intentionally excluded from the
    replacement set.  Decimal digit ``0`` is ordinary text and is likewise
    untouched.  Replacing with one space, instead of deleting the byte, keeps
    neighbouring tokens from being silently joined; the existing whitespace
    normalisation below then applies the same policy as every other extracted
    passage.
    """
    return _NON_TEXT_C0.sub(" ", text)


def extract_text(blob: bytes, name: str = "") -> tuple[str, str, str]:
    """(normalised_text, extraction_method, text_layer).

    ``text_layer`` is one of yes / partial / no, and 'no' is an honest statement
    about the document -- a scanned legacy order with no text layer is not a
    parse failure and not a missing source.
    """
    kind = sniff(blob)
    method = {"pdf": "pdf_flate_text_span", "docx": "docx_xml_text_span",
              "ole2": "ole2_printable_run_span", "rtf": "rtf_control_strip_span",
              "text": "plain_text_span"}.get(kind, "")
    if kind == "pdf":
        text = _pdf_text(blob)
    elif kind == "docx":
        text = _docx_text(blob)
    elif kind == "ole2":
        text = _ole2_text(blob)
    elif kind == "rtf":
        text = _rtf_text(blob)
    elif kind == "text":
        text = blob.decode("utf-8", "replace")
    else:
        return "", f"unsupported:{kind}", "no"
    text = _normalise_extracted_text(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    layer = "yes" if len(text) >= 400 else "partial" if len(text) >= 60 else "no"
    return text, method, layer


def squeeze(text: str) -> tuple[str, list[int]]:
    """(whitespace-free text, index map back to the original).

    PDF text layers extracted from a tariff sheet arrive letter-spaced --
    Magellan's own tariff reads "F .E .R. C. N o . 2 0 1 . 5 . 0" -- so a
    pattern written against readable text never matches. Squeezing lets the
    pattern work while the index map keeps the stored char offsets pointing at
    the real span in the real text, which is what makes a document fact
    reproducible.
    """
    out = []
    idx = []
    for i, ch in enumerate(text):
        if ch.isspace():
            continue
        out.append(ch)
        idx.append(i)
    return "".join(out), idx


def map_span(index_map: list[int], a: int, b: int) -> tuple[int, int]:
    """Squeezed [a,b) -> original [start,end)."""
    if not index_map:
        return a, b
    a = max(0, min(a, len(index_map) - 1))
    b = max(a + 1, min(b, len(index_map)))
    return index_map[a], index_map[b - 1] + 1


def flatten(text: str) -> str:
    """One-line normalisation used for span offsets, so a stored char_start is
    reproducible from the stored content hash."""
    return re.sub(r"\s+", " ", text).strip()


def span(text: str, start: int, end: int, pad: int = 90) -> tuple[int, int, str]:
    """(char_start, char_end, verbatim) with context, clipped to the text."""
    a = max(0, start - pad)
    b = min(len(text), end + pad)
    return a, b, text[a:b]


# ================================================================== A11
# FIRST-OBSERVED BASELINE.
#
# The 8 September audit found 263 investor-feed events with is_backfill=0 whose
# source filed date predated the run, back to 2017. Both document adapters were
# emitting an event for every filing they classified, and deciding "backfill"
# either by hard-coding `filed_date < <window start>` (lng) or not at all
# (elibrary_docs, which set is_backfill=0 unconditionally). A first ingestion of
# a decade of dockets therefore arrived as a decade of "current news".
#
# The rule implemented here needs no cutoff date and no configuration:
#
#   * A filing occurrence has a FIRST-OBSERVED time, which is when THIS system
#     first saw it. That is not its filing date and not its effective date.
#   * An entity has a BASELINE: the moment this system finished its first
#     ingestion of that entity. Everything present at that moment is history.
#   * Before the baseline exists, nothing is current news. The first run
#     ARCHIVES and establishes the baseline.
#   * After it exists, only a filing first observed in this run can be news.
#   * A filing first observed now but filed before the baseline was established
#     is a LATE DISCOVERY. It is routed for explicit review, never promoted
#     silently, because "we only just noticed a 2019 order" is a different fact
#     from "FERC issued an order today".
#   * An identical resubmission is a version record, never an economic change.
#
# Filing date, effective date and discovery date stay three separate columns
# throughout; nothing here overwrites any of them.

ARCHIVE = "filing_archive"
INVESTOR = "investor_feed"
REVIEW_QUEUE = "data_review_queue"

BASELINE_SCOPE = "news_baseline"


class NewsBaseline:
    """Decides, per filing occurrence, whether an event may be current news.

    Constructed BEFORE the adapter writes anything for the entity, because the
    question it answers -- "had we seen this occurrence before this run?" -- is
    destroyed by the write.
    """

    def __init__(self, staging, adapter: str, entity_key: str, source_system: str):
        self.adapter = adapter
        self.entity_key = entity_key
        self.staging = staging
        rows = staging.query(
            "SELECT filing_id, first_seen_at FROM filings "
            "WHERE source_system=? AND entity_key=?", (source_system, entity_key))
        #: occurrence -> when THIS system first saw it, from before this run
        self.previously_seen = {r["filing_id"]: (r["first_seen_at"] or "")
                                for r in rows}
        marker = staging.query(
            "SELECT state, updated_at FROM checkpoints "
            "WHERE adapter=? AND entity_cid=? AND scope_key=?",
            (adapter, entity_key, BASELINE_SCOPE))
        self.established_at = (marker[0]["updated_at"] or "") if (
            marker and marker[0]["state"] == "done") else ""

    # -------------------------------------------------------------- queries

    @property
    def exists(self) -> bool:
        return bool(self.established_at)

    def first_seen(self, filing_id: str, captured_at: str | None) -> str | None:
        """The earliest durable first-observed time, without a replay clock.

        ``captured_at`` comes from the immutable source-cache index.  A prior
        row can legitimately predate that cache capture, while an audited row
        may contain the later wall-clock stamp produced by the historical bug;
        taking the earliest nonblank value preserves the former and repairs the
        latter on a supported replay.  A seed-only occurrence with no captured
        evidence remains ``None``.  Membership in ``previously_seen`` still
        routes that occurrence as already observed even when its time is null.
        """
        values = [str(value).strip() for value in (
            self.previously_seen.get(filing_id), captured_at) if value]
        return min(values) if values else None

    def route(self, filing_id: str, filed_date: str, *,
              version_status: str = "") -> tuple[str, int, str]:
        """(destination, is_backfill, why).

        `why` is stored on the event so a reader can see which branch decided it.
        """
        if version_status == VersionStatus_IDENTICAL:
            return (ARCHIVE, 1,
                    "IDENTICAL RESUBMISSION: byte-identical to an earlier occurrence of the "
                    "same submission. It is recorded as a version/archive record because the "
                    "occurrence is real, but it changes nothing economically and is never an "
                    "economic-change event.")
        if not self.exists:
            return (ARCHIVE, 1,
                    "BASELINE INGESTION: this is the first time this system has ingested "
                    f"{self.entity_key} through the {self.adapter} adapter. Everything found "
                    "on a first pass is history by construction, so it is archived and the "
                    "baseline is established. Nothing from a first ingestion is current news.")
        if filing_id in self.previously_seen:
            return (ARCHIVE, 1,
                    "ALREADY OBSERVED: this occurrence was first seen at "
                    f"{self.previously_seen[filing_id] or 'an earlier run'}. Re-reading a "
                    "filing is not a new event.")
        if filed_date and filed_date < self.established_at[:10]:
            return (REVIEW_QUEUE, 1,
                    f"LATE DISCOVERY: filed {filed_date}, but first observed only now, after "
                    f"the baseline was established on {self.established_at[:10]}. A newly "
                    "discovered older order may well be material, but it is not today's news "
                    "and is not promoted silently. It is queued for an explicit decision.")
        return (INVESTOR, 0,
                f"NEW SINCE BASELINE: first observed in this run, filed {filed_date}, after "
                f"the baseline of {self.established_at[:10]}.")

    def establish(self) -> None:
        """Mark the entity as baselined. Call once the entity's filings have been
        written successfully; it is idempotent and never moves backwards."""
        if not self.exists:
            self.staging.checkpoint(self.adapter, self.entity_key, BASELINE_SCOPE, "done")


#: imported lazily to keep this module free of a ferclib.status dependency cycle
VersionStatus_IDENTICAL = "identical_resubmission"


# ---------------------------------------------------------------- span support

class SpanSupportError(ValueError):
    """A document fact was built whose span does not contain what it asserts."""


def _loose(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


_NUMERIC_ASSERTION = re.compile(
    r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")
_NUMBER_IN_SPAN = re.compile(
    r"(?<![\w.,])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\w,]|\.\d)")
_NUMBER_WITH_ATTACHED_UNIT = re.compile(
    r"(?<![\w.,])(?P<number>[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    r"(?=(?:MMBTU|MDTH|DTH|MMSCF|MMCF|MCF|BCF)(?:/(?:D|DAY))?(?:\b|$))",
    re.I,
)


def _asserted_text(value) -> str:
    """Normalise caller values without turning numeric zero into an empty value."""
    return "" if value is None else str(value).strip()


def _span_has_exact_number(verbatim: str, asserted: str) -> bool:
    """Match a complete numeric token, with comma formatting treated as equivalent.

    The old loose-string comparison accepted ``0`` anywhere in ``10`` or
    ``2026``.  Converting complete tokens through Decimal retains legitimate
    filed zeroes while rejecting substrings and identifiers that merely contain
    the asserted digits.
    """
    from decimal import Decimal, InvalidOperation

    try:
        wanted = Decimal(asserted.replace(",", ""))
    except InvalidOperation:
        return False
    for pattern in (_NUMBER_IN_SPAN, _NUMBER_WITH_ATTACHED_UNIT):
        for match in pattern.finditer(verbatim or ""):
            candidate = (match.groupdict().get("number")
                         if match.groupdict() else match.group(0))
            try:
                if Decimal(candidate.replace(",", "")) == wanted:
                    return True
            except InvalidOperation:
                continue
    return False


def span_supports(verbatim: str, *values: str) -> bool:
    """Does this passage actually contain the value(s) being asserted?

    The 8 September audit found the same defect twice. All three stored
    refund-window spans were the eLibrary DESCRIPTION HEADING -- "Order Accepting
    and Suspending Tariff Record, Subject to Refund ... under IS26-24." -- while
    the fact asserted "2025-11-26 .. open". The heading contains neither date. A
    span that merely sits near a claim is not evidence for it.

    Dates are matched in both the ISO and the long American form, because an
    order writes "December 1, 2025" and the fact stores "2025-12-01". A date
    range written as one span -- "April 21-22, 2026" for a two-day inspection --
    supports its FIRST day, which is how such a date is stored.
    """
    hay = verbatim or ""
    loose = _loose(hay)
    for v in values:
        v = _asserted_text(v)
        if not v:
            continue
        forms = {v}
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", v)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            name = _MONTH_NAMES[mo - 1]
            forms |= {f"{name} {d}, {y}", f"{name} {d} {y}", f"{mo}/{d}/{y}",
                      f"{mo:02d}/{d:02d}/{y}"}
            if any(_written_dates_include(hay, y, mo, d) for _ in (0,)):
                continue
        if _NUMERIC_ASSERTION.fullmatch(v):
            if not _span_has_exact_number(hay, v):
                return False
            continue
        if not any(f in hay or _loose(f) in loose for f in forms):
            return False
    return True


_MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]
_WRITTEN_DATE = re.compile(
    r"\b(" + "|".join(_MONTH_NAMES) + r")\s+(\d{1,2})(?:\s*[-–]\s*\d{1,2})?,?\s*(\d{4})",
    re.I)


def _written_dates_include(text: str, year: int, month: int, day: int) -> bool:
    for m in _WRITTEN_DATE.finditer(text or ""):
        if (_MONTH_NAMES.index(m.group(1).capitalize()) + 1 == month
                and int(m.group(2)) == day and int(m.group(3)) == year):
            return True
    return False


def require_span_support(verbatim: str, *values: str, what: str = "assertion") -> str:
    """The structural form. Raises rather than store an unsupported span.

    Stricter than ``span_supports`` in one respect, deliberately. The predicate
    treats an empty value as vacuously supported, because a fact asserting
    nothing asserts nothing false. This is a CONSTRUCTOR guard, and building a
    document fact with no value and calling it span-backed is a caller error, so
    an empty value is refused here rather than waved through.
    """
    if not values or not any(_asserted_text(v) for v in values):
        raise SpanSupportError(
            f"{what}: no value was given to check. A span cannot support an "
            f"assertion that states nothing; this is a caller error, not a source "
            f"condition.")
    if not span_supports(verbatim, *values):
        raise SpanSupportError(
            f"{what}: the stored passage does not contain "
            f"{', '.join(repr(v) for v in values if v)}. A span containing a heading, "
            f"or merely containing some number, is not support for this value. "
            f"passage={verbatim[:180]!r}")
    return verbatim
