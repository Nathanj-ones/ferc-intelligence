"""
Form 549B Index of Customers (IOC) adapter.

18 CFR 284.13(c): every quarter an interstate pipeline files an index of all of
its firm transportation and storage customers **under contract as of the first
day of the calendar quarter**. The filing is a tab-delimited text file carried as
an eLibrary attachment under Class/Type `Report/Form` / `Index of Customer`.

Everything below is bound to what `discovery/ioc_discovery.md` established live
against FERC on 7 September 2026. The five things that adapter code most easily
gets wrong, and how this module handles each:

  1. TWO DATES, NOT ONE. Header item `c` (column 4) is the date the pipeline
     *expects to file*; header item `e` (column 6) is the first day of the
     calendar quarter and is the as-of date of every quantity in the file. The
     observation instant is item `e`. Proved live: TGP Q3-2026 was refiled as
     `20260707-5148` with report date 7/7/2026 and indicator R while the
     snapshot date stayed 7/1/2026.

  2. CONTRACTED STORAGE QUANTITY IS A STOCK. D item `p` is captioned "For
     Storage, max Daily Quantity" but the manual's own instruction is "the
     largest quantity of natural gas the pipeline is obligated to store". The
     caption is legacy and wrong; the field is a quantity, not a rate, and is
     never added to transportation MDQ, inventory capacity or daily withdrawal.

  3. THE CONTINUATION FIELD IS A ROLLOVER PERIOD IN DAYS. D item `n` holds
     365/1095/730/180/279 -- a duration, populated only once the primary term has
     already passed. It is not a date, not a notice period and not a guaranteed
     termination, so no termination date is ever computed from `m` + `n`.

  4. P RECORDS ARE LOCATION DETAIL. M2 = receipt, MQ = delivery, S8 and S9 are
     SEGMENT ENDPOINTS -- S8 is not a receipt code. The "P sum = 2 x D total"
     rule is FALSE: measured 91.8% on Transco and 87.8% on TGP. No allocation is
     published from P records; the contract's capacity is D item `o`.

  5. A CONTRACT MISSING FROM A SNAPSHOT IS NOT A TERMINATION. Snapshot diffs
     publish the change in matched MDQ only, and say so; the arrival of a revised
     file is recorded as a revision event, separately from any economic change.

Retrieval is the shared eLibrary route through `ctx.client`. Server-side
`classTypes` filtering is broken (it returns
`"Value cannot be null. Parameter name: stringToEscape"` for every body shape
tried), so the Class/Type filter is applied client-side over
`hit.classTypes[].documentType`.

  6. THE NATIVE HEADER CID IS THE ONLY IDENTITY GATE (audit A02). The eLibrary
     description search is a RETRIEVAL key, not an identity test: searching
     "MountainWest Pipeline, LLC" also returns "MountainWest Overthrust
     Pipeline, LLC". Header item `b` (column 3) carries the filer's own company
     ID and must equal the intended entity before a single row is written.
     Name similarity never establishes ownership. A document that names another
     entity is recorded as an ASSOCIATION (see `_record_association`); ownership
     stays single-valued and is decided by the file's own header.

  7. A METADATA AVAILABILITY CODE IS NOT AN OBSERVED HTTP STATUS (audit A15).
     `availCode` comes out of the search hit; when it is not `P` this adapter
     makes NO request at all, so it reports no status, infers no
     confidentiality, and says which public accession (if any) carries the same
     snapshot. The erroneous occurrence is retained in its own right.

  8. RECOVERABLE BYTES ARE NOT AN IRREDUCIBLE SOURCE GAP (audit A14). Encoding
     is detected (UTF-8/16/32 BOM, BOM-less UTF-16, UTF-8, latin-1 fallback), a
     bounded title preamble is skipped verbatim, and exactly one malformed
     header shape -- a single empty field inserted at index 1 -- is repaired
     under CID and date validation with the original bytes retained. Anything
     else is quarantined, never guessed at.
"""

from __future__ import annotations

import codecs
import csv
import datetime as dt
import hashlib
import io
import json
import pathlib
import re
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ferclib import coverage, periods                                       # noqa: E402
from ferclib.http import FetchError, redact                                 # noqa: E402
from ferclib.registry import (BY_ADAPTER, REGISTRY_VERSION,                 # noqa: E402
                              known_unit, unit_family_of, units_compatible)
from ferclib.staging import observation_id                                  # noqa: E402
from ferclib.status import (Availability, Method, Origin, Validation,       # noqa: E402
                            VersionStatus)

ADAPTER = "ioc"
SOURCE_SYSTEM = "eLibrary"
REGIME = "Form 549B IOC"
FORM = REGIME

#: the file format carries NO version or schema stamp anywhere -- the only
#: proxies are header item d (O/R) and the .TAB/.TA1 extension, so schema drift
#: would be silent. Recorded as such rather than invented.
FORMAT_VERSION = "no_version_indicator_in_format"

#: THE PINNED OFFICIAL INSTRUCTIONS. Every semantic decision in this module is
#: traceable to one of these two, and to nothing else. They are quoted, not
#: paraphrased, wherever a caption and an instruction disagree.
INSTRUCTIONS = {
    "rule": "18 CFR 284.13(c)",
    "rule_text": ("each calendar quarter an interstate pipeline must file an index of "
                  "all of its firm transportation and storage customers under contract "
                  "as of the first day of the calendar quarter"),
    "manual": ("FERC, Instruction Manual, Form 549B (Index of Customers), "
               "OMB control number 1902-0169, revised 2017-02-23"),
    "manual_url": "https://www.ferc.gov/sites/default/files/2020-05/elec-inst_2.pdf",
    "manual_sections": ("Electronic Filing Guide, pp. 6-11: record layouts for the H, D, "
                        "A, P and F records and the item-letter vocabulary"),
    # the four semantics an IOC adapter most easily breaks, each tied to the manual
    "preserved": (
        "D item o is the CONTRACTED TRANSPORT MDQ (a daily rate); D item p is the "
        "CONTRACTED STORAGE QUANTITY, and the manual's instruction -- 'the largest "
        "quantity of natural gas the pipeline is obligated to store' -- governs over its "
        "legacy caption 'For Storage, max Daily Quantity'. The two are different unit "
        "families and are never added.",
        "Storage units come from header item g and transport units from header item f, "
        "each B=MMBtu, T=Dth, F=Mcf. Manual section 5.A says quantities must be reported "
        "in MMBtu absent a waiver, but filers report T (Dth); the header is read, never "
        "assumed.",
        "Concentration aggregates a shipper's CONTRACTS to the legal shipper name FIRST "
        "and ranks afterwards; ranking contracts would understate concentration.",
        "Item m is the PRIMARY TERM expiry, not the contract end date; item n is a "
        "rollover period in DAYS, so no termination date is derived from m + n.",
        "P records are location detail. M2 = receipt, MQ = delivery, S8 and S9 are "
        "SEGMENT ENDPOINTS -- S8 is not a receipt-point code -- and no 'P total = 2 x D "
        "total' identity is assumed.",
    ),
}
APPLICABILITY_BASIS = (f"{INSTRUCTIONS['rule']} and the Form 549B instruction manual "
                       "(OMB 1902-0169, rev. 2017-02-23); the file format itself "
                       "carries no version indicator")

ELIB = "https://elibrary.ferc.gov/eLibraryWebAPI/api"
DOCINFO = "https://elibrary.ferc.gov/eLibrary/docinfo?accession_num={}"
FILELIST = "https://elibrary.ferc.gov/eLibrary/filelist?accession_num={}"

#: the Class/Type vocabulary, verified against GetClassTypes. Note the singular
#: "Index of Customer" -- the filing DESCRIPTIONS all say "Index of Customers".
CLASS_TYPE = ("Report/Form", "Index of Customer")
SEARCH_PHRASE = "Index of Customers Report of"

#: Transco files .txt for every quarter including its refilings and never uses
#: .TA1; match on the extension set, never on ".TAB", and never identify the
#: pipeline from the filename (TGP's embedded code flips 000020 -> 000231).
DATA_EXTENSIONS = {"txt", "tab", "ta1", "ta2", "ta3"}

QUARTER_OF_MONTH = {1: "Q1", 2: "Q1", 3: "Q1", 4: "Q2", 5: "Q2", 6: "Q2",
                    7: "Q3", 8: "Q3", 9: "Q3", 10: "Q4", 11: "Q4", 12: "Q4"}

#: header item f / g vocabulary
UOM = {"B": "MMBtu", "T": "Dth", "F": "Mcf"}

#: NAESB WGQ point-identifier codes, verbatim from the Form 549B manual.
#: S8/S9 are SEGMENT endpoints. S8 is NOT a receipt code.
POINT_CODES = {
    "M2": "receipt point",
    "MQ": "delivery point",
    "MV": "mainline",
    "S8": "pipeline segment endpoint (second of 2 point records)",
    "S9": "pipeline segment endpoint (1 point record, or first of 2)",
    "SB": "storage area",
    "IJ": "injection point (daily rate)",
    "WR": "withdrawal point (daily rate)",
}

EXPIRY_BUCKETS = ["0-1y", "1-2y", "2-5y", "5y+", "continuing_after_primary_term",
                  "unknown"]

#: A FERC company ID as it appears in header item b. Northern Border pads it to
#: 17 characters; the value is compared on the normalised form only.
CID_RE = re.compile(r"^C\d{6}$")

#: the five record types the manual defines. Anything else in column 1 is an
#: UNCLASSIFIED row: retained verbatim, never reinterpreted (audit A14).
RECORD_TYPES = ("H", "D", "A", "P", "F")

#: How many leading rows may be skipped as a title preamble before the H record.
#: Sabine Pipe Line writes a two-line "Index of Customers" title block. The bound
#: exists so that a file which is simply not an IOC file cannot be walked into.
MAX_PREAMBLE_ROWS = 5

#: Below this many contributing rows an aggregate carries one lineage edge PER
#: ROW, which is what a reviewer can actually read. At or above it the aggregate
#: carries a `lineage_populations` set instead (schema: the only permitted
#: alternative to per-row edges). Both are emitted for set aggregates.
PER_ROW_EDGE_LIMIT = 40

#: Filing-level events are discovered during retrieval, before any observation
#: exists. They are buffered here and handed to run.py on an observation's
#: `_events` key, so every write still goes through the single integrating writer.
_PENDING_EVENTS: dict[str, list[dict]] = {}

#: Set-based lineage rows for the observations this adapter produced, buffered
#: per entity. They are NOT attached to the observation dicts, because an
#: unrecognised key inside an observation row would be handed straight to the
#: column list of an INSERT. `lineage_populations.observation_id` is a foreign
#: key onto `observations`, so these can only be written AFTER the observations
#: land and BEFORE the edges that reference them: the runner calls
#: `write_pending_populations()` between those two steps.
_PENDING_POPULATIONS: dict[str, list[dict]] = {}

#: why an entity ended a retrieval with nothing. OUR failures and FERC's
#: silences are different states and must never be reported as one another.
_RETRIEVAL_STATE: dict[str, tuple[str, str]] = {}


class IOCIdentityError(ValueError):
    """The file's own header does not establish that it belongs to this entity.

    Deliberately NOT a subclass of the parse errors: a file that parses
    perfectly but names another filer is not a broken source, and must not be
    reported as one.
    """

    def __init__(self, message: str, *, expected: str, found: str, found_name: str = "",
                 accession: str = ""):
        super().__init__(message)
        self.expected = expected
        self.found = found
        self.found_name = found_name
        self.accession = accession


class IOCEntityMismatch(IOCIdentityError):
    """Header item b names a DIFFERENT company ID than the entity being built."""


class IOCIdentityUnverifiable(IOCIdentityError):
    """No usable company ID on one of the two sides, so ownership cannot be
    established at all. Refused rather than guessed (contract rule 3.8)."""


# ================================================================= eLibrary
# Shared by adapters/capacity.py. Every request goes through ctx.client, which
# owns retry, backoff, the request budget, HTML-as-200 rejection, credential
# redaction and the content-addressed cache.

def search(ctx, legal_name: str, phrase: str, *, start: str, end: str,
           document_type: str, max_pages: int = 5) -> list[dict]:
    """Description-phrase search, then a CLIENT-SIDE Class/Type filter.

    The description convention is machine-generated and stable
    (`[Revised ]Index of Customers Report of <LEGAL NAME> for Q<n> of <YYYY>.`),
    which makes the phrase a far better retrieval key than the bare company
    name -- 14/14 precision on Transco versus hundreds of unrelated hits.
    Pagination completion is checked explicitly.
    """
    out: list[dict] = []
    page = 0
    total = None
    while page < max_pages:
        payload = {
            "searchText": f"{phrase} {legal_name}", "searchFullText": False,
            "searchDescription": True,
            "dateSearches": [{"dateType": "filed_date", "startDate": start, "endDate": end}],
            "availability": None, "affiliations": [], "categories": [], "libraries": [],
            "accessionNumber": None, "eFiling": False,
            "docketSearches": [{"docketNumber": "", "subDocketNumbers": []}],
            "resultsPerPage": 100, "curPage": page, "classTypes": [], "sortBy": "",
            "groupBy": "NONE", "idolResultID": "", "allDates": False}
        body, _entry = ctx.client.post_json(f"{ELIB}/Search/AdvancedSearch", payload,
                                            source_system=SOURCE_SYSTEM,
                                            use_cache=not ctx.force)
        data = json.loads(body.decode("utf-8"))
        if not data.get("success"):
            raise FetchError(f"{ELIB}/Search/AdvancedSearch",
                             f"search rejected: {data.get('errorMessage')!r}")
        total = data.get("totalHits") if total is None else total
        hits = data.get("searchHits") or []
        out.extend(hits)
        if len(hits) < 100 or len(out) >= (total or 0):
            break
        page += 1
    if total is not None and len(out) < total and page >= max_pages - 1:
        ctx.log("warn", f"{legal_name}: pagination incomplete -- {len(out)} of {total} hits "
                        f"retrieved after {max_pages} pages", adapter=ADAPTER)
    # server-side classTypes filtering is broken; filter here
    return [h for h in out
            if any(c.get("documentType") == document_type
                   for c in (h.get("classTypes") or []))]


def file_list(ctx, accession: str) -> list[dict]:
    data = ctx.client.get_json(f"{ELIB}/File/GetFileListFromP8/{accession}",
                               source_system=SOURCE_SYSTEM, use_cache=True)
    return data.get("DataList") or []


def download(ctx, accession: str, ids: list[str]) -> tuple[bytes, dict]:
    """POST-only download. `fileidLst` subsetting is accepted but IGNORED by the
    server -- the whole filing always comes back -- so every attachment ID is
    sent and the members are separated locally."""
    return ctx.client.post_json(
        f"{ELIB}/File/DownloadP8File",
        {"FileType": "", "accession": accession, "fileid": 0, "FileIDAll": "",
         "fileidLst": ids, "Islegacy": False},
        source_system=SOURCE_SYSTEM, use_cache=True,
        headers={"Origin": "https://elibrary.ferc.gov",
                 "Referer": FILELIST.format(accession)})


def members(blob: bytes, accession: str, fallback_name: str) -> dict[str, bytes]:
    """A filing with >=2 attachments returns a ZIP; exactly one returns the RAW
    FILE. Sniff the magic bytes -- never assume ZIP."""
    if blob[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            return {n.split(f"{accession}_", 1)[-1]: z.read(n) for n in z.namelist()}
    return {fallback_name: blob}


def resolve_blockers(ctx, adapter: str, scope: str) -> int:
    """Close this adapter's own open blockers for a scope that has now succeeded.

    A blocker left open after the condition clears makes `run.py status` lie
    about what is actually wrong. Only blockers belonging to the calling adapter
    and to the exact scope are touched; a genuine standing condition (an
    availCode N attachment, say) keeps its own accession-scoped blocker open.
    """
    rows = ctx.staging.query(
        "SELECT * FROM blockers WHERE adapter=? AND scope=? AND resolved_at IS NULL",
        (adapter, scope))
    if not rows:
        return 0
    ctx.staging.upsert("blockers",
                       [dict(r) | {"resolved_at": utcnow()} for r in rows], ["blocker_id"])
    return len(rows)


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def iso_date(text: str) -> str:
    """M/D/YYYY or MM/DD/YYYY -> ISO. Transco zero-pads, TGP does not."""
    t = (text or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", t)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    try:
        return dt.date.fromisoformat(t).isoformat()
    except ValueError:
        return ""


def num(v):
    try:
        s = str(v).replace(",", "").strip()
        return float(s) if s not in ("", "-") else None
    except (TypeError, ValueError):
        return None


def normalise_cid(value: str) -> str:
    """A FERC company ID compared on its normalised form only.

    Northern Border writes `C000626` padded to 17 characters and Sabine writes
    it clean; both are the same company. Whitespace and case are the ONLY things
    normalised away -- no prefixing, no zero-fill, no numeric coercion, because
    each of those would silently make two different IDs compare equal.
    """
    return re.sub(r"\s+", "", (value or "")).upper()


def is_quarter_start(iso: str) -> bool:
    """18 CFR 284.13(c): the index is as of the FIRST DAY of a calendar quarter."""
    return bool(iso) and iso[5:7] in ("01", "04", "07", "10") and iso[8:10] == "01"


# ================================================================= decoding

#: Byte-order marks, longest first: the UTF-32 LE mark begins with the UTF-16 LE
#: mark, so testing UTF-16 first would decode a UTF-32 file as UTF-16.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32", "UTF-32 LE"),
    (codecs.BOM_UTF32_BE, "utf-32", "UTF-32 BE"),
    (codecs.BOM_UTF8, "utf-8-sig", "UTF-8"),
    (codecs.BOM_UTF16_LE, "utf-16", "UTF-16 LE"),
    (codecs.BOM_UTF16_BE, "utf-16", "UTF-16 BE"),
)


def decode_ioc(raw: bytes) -> tuple[str, dict]:
    """Decode the bytes of one IOC attachment, and say exactly how.

    FERC accepts whatever the filer uploads, and filers upload whatever their
    spreadsheet exported. Four encodings occur in the delivered window: plain
    ASCII/UTF-8, UTF-8 with a byte-order mark (Northern Border, 11 quarters),
    UTF-16 LE with a mark (Gulfstream Q4-2024) and, in principle, anything
    latin-1 can absorb.

    Decoding UTF-8-with-BOM as latin-1 turns the first record into `\\xef\\xbb\\xbfH`,
    which is not `H`; decoding UTF-16 as latin-1 leaves a NUL between every
    character and makes `csv` raise "new-line character seen in unquoted field".
    Both were reported as irreducible source gaps (audit A14). They are neither.

    Returns (text, provenance). The provenance is retained on the filing so that
    the transformation applied to the bytes is auditable, and the ORIGINAL bytes
    are still the ones hashed and cached.
    """
    meta = {"byte_size": len(raw), "content_sha256": sha256(raw),
            "bom": "", "encoding": "", "decode_fallback": "",
            "replaced_characters": 0}
    for mark, encoding, label in _BOMS:
        if raw.startswith(mark):
            meta["bom"] = label
            meta["encoding"] = encoding
            text = raw.decode(encoding)
            break
    else:
        encoding = _sniff_bomless(raw)
        if encoding:
            meta["encoding"] = encoding
            meta["decode_fallback"] = ("no byte-order mark; wide encoding inferred from the "
                                       "NUL-byte pattern")
            text = raw.decode(encoding)
        else:
            try:
                text = raw.decode("utf-8")
                meta["encoding"] = "utf-8"
            except UnicodeDecodeError as exc:
                # latin-1 cannot fail, so it is the terminal fallback -- but it
                # is RECORDED, because it reinterprets every byte >= 0x80 and a
                # silent reinterpretation is not a successful decode.
                text = raw.decode("latin-1")
                meta["encoding"] = "latin-1"
                meta["decode_fallback"] = (f"not valid UTF-8 ({exc.reason} at byte "
                                           f"{exc.start}); decoded as latin-1")
                meta["replaced_characters"] = sum(1 for b in raw if b >= 0x80)
    # A byte-order mark in the middle of a file -- two exports concatenated --
    # would reach csv as data. It is a framing artefact, not a field, so it is
    # removed and the removal is recorded.
    if "﻿" in text:
        meta["stripped_inner_bom"] = text.count("﻿")
        text = text.replace("﻿", "")
    # A NUL that survives a correct decode is NOT removed: it would be the
    # filer's own bytes, and quietly deleting content is not decoding it. It is
    # counted so that a downstream oddity has an explanation.
    if "\x00" in text:
        meta["nul_characters_retained"] = text.count("\x00")
    meta["line_terminator"] = ("CRLF" if "\r\n" in text
                               else "CR" if "\r" in text
                               else "LF" if "\n" in text else "none")
    return text, meta


def _sniff_bomless(raw: bytes) -> str:
    """UTF-16 with no byte-order mark, decided on the NUL pattern.

    Only claimed when the evidence is overwhelming: a tab-delimited ASCII report
    encoded as UTF-16 has a NUL in every other byte. Anything less certain falls
    through to the UTF-8/latin-1 path rather than being guessed at.
    """
    head = raw[:4096]
    if len(head) < 8 or b"\x00" not in head:
        return ""
    even = sum(1 for b in head[0::2] if b == 0)
    odd = sum(1 for b in head[1::2] if b == 0)
    half = len(head) // 2
    if odd > half * 0.8 and even < half * 0.1:
        return "utf-16-le"
    if even > half * 0.8 and odd < half * 0.1:
        return "utf-16-be"
    return ""


# ================================================================= parsing

def _cell(row, i):
    return row[i].strip() if i < len(row) else ""


def _read_header(row: list[str]) -> dict:
    """Read an H row by position. Returns the header dict; never raises."""
    header = {
        "pipeline_name": _cell(row, 1),
        "pipeline_id": _cell(row, 2),
        "pipeline_id_normalised": normalise_cid(_cell(row, 2)),
        "report_date": iso_date(_cell(row, 3)),          # item c -- NOT the as-of date
        "original_revised": _cell(row, 4).upper(),       # item d
        "snapshot_date": iso_date(_cell(row, 5)),        # item e -- THE as-of date
        "uom_transport_code": _cell(row, 6).upper(),     # item f
        "uom_storage_code": _cell(row, 7).upper(),       # item g
        "contact": _cell(row, 8),
        "footnote_ids": _cell(row, 9),
    }
    header["uom_transport"] = UOM.get(header["uom_transport_code"], "")
    header["uom_storage"] = UOM.get(header["uom_storage_code"], "")
    # A code we do not recognise is NOT the same fact as no code at all. Both
    # leave the figure without a unit, but one is the filer stating nothing and
    # the other is the filer stating something we cannot interpret, and claiming
    # the first when the second happened would be a false statement about the
    # filing. Silence is not a value; an unreadable value is not silence.
    header["uom_transport_state"] = _uom_state(header["uom_transport_code"])
    header["uom_storage_state"] = _uom_state(header["uom_storage_code"])
    return header


def _template_of(entity: dict, assets: list[dict]) -> str:
    """The template whose metric set applies, REFUSED rather than assumed.

    The template decides which metrics exist for an entity and therefore which
    coverage slots are frozen, so guessing it invents a scope. The delivered code
    defaulted to `interstate_gas`, which would have silently built a gas
    pipeline's whole metric grid for an entity nobody had classified -- and
    contract rule 3.3 makes scope distinctions non-interchangeable. A missing
    template is a universe-configuration defect, and refusing it surfaces that as
    one blocker instead of a page of confidently scoped observations.
    """
    template = (assets[0]["template"] if assets and assets[0].get("template")
                else (entity.get("template") or ""))
    if not template:
        raise ValueError(
            f"{entity.get('entity_key', '?')}: no template on the entity or its assets, so "
            f"which metric set applies is unestablished. It is not assumed: the template "
            f"decides the coverage grid, and defaulting it would invent a scope for an "
            f"entity nobody classified")
    return template


def _uom_state(code: str) -> str:
    """`stated` | `blank` | `unrecognised`, for one header unit-of-measure item.

    The manual's vocabulary for items f and g is B = MMBtu, T = Dth, F = Mcf.
    Anything else is a statement this adapter cannot read, and it is reported as
    that rather than folded into "no unit was stated".
    """
    if not (code or "").strip():
        return "blank"
    return "stated" if code in UOM else "unrecognised"


def _header_defects(header: dict) -> list[str]:
    """What is wrong with a candidate header's FORMAT, one defect at a time.

    Company identity is deliberately not judged here. A header that reads
    perfectly but names the wrong filer, or names no filer, is an IDENTITY
    question, not a malformed file, and the two produce different records: a
    parse failure opens a source blocker, an identity failure is recorded as a
    document association. `_identity_gate` owns that decision.
    """
    bad = []
    if not header["pipeline_name"]:
        bad.append("item b (pipeline name) is blank")
    if not header["report_date"]:
        bad.append("item c (report date) is blank or unparseable")
    if not header["snapshot_date"]:
        bad.append("item e (first day of the calendar quarter) is blank or unparseable; "
                   "without it the file has no as-of date")
    return bad


def _repair_header(row: list[str], expect_cid: str = "") -> tuple[list[str], dict] | None:
    """The ONE malformed header shape this adapter repairs, and nothing else.

    Six Discovery Gas Transmission files (2024-Q1 through 2025-Q1) carry an
    extra EMPTY field between the record-type code and the pipeline name, so
    every header item sits one column to the right:

        H <tab> <tab> Discovery Gas Transmission LLC <tab> C001031 <tab> ...

    The repair drops that one empty field, and is applied only when ALL of these
    hold, so it cannot generalise into permissive shifting of arbitrary rows:

      * the row is an H row that FAILS to read at the documented layout;
      * the field being dropped is at index 1 and is empty after stripping;
      * dropping it produces a header with NO remaining defects, i.e. a real
        C-number in item b, and item c and item e both parsing as dates;
      * item e of the repaired header is the first day of a calendar quarter,
        as 18 CFR 284.13(c) requires;
      * when the caller knows which entity it asked for, the repaired company id
        equals it -- so a repair can never invent a different filer.

    Returns (repaired_row, provenance) or None. D, A, P and F rows are never
    shifted: Discovery's own detail rows sit at the documented positions, which
    is the evidence that the defect is confined to the header.
    """
    if len(row) < 2 or row[1].strip() != "":
        return None
    candidate = [row[0]] + list(row[2:])
    header = _read_header(candidate)
    if _header_defects(header):
        return None
    # a repair must land on a real company id -- this check is the reason the
    # transformation cannot quietly turn one filer's file into another's
    if not CID_RE.match(header["pipeline_id_normalised"]):
        return None
    if not is_quarter_start(header["snapshot_date"]):
        return None
    if expect_cid and header["pipeline_id_normalised"] != normalise_cid(expect_cid):
        return None
    return candidate, {
        "applied": True,
        "transformation": "drop_empty_field_at_index_1",
        "scope": "the H (header) record only; no D, A, P or F row was altered",
        "original_row": list(row),
        "repaired_row": list(candidate),
        "checks_passed": [
            "the dropped field was empty after stripping",
            f"repaired item b is a FERC C-number ({header['pipeline_id_normalised']})",
            f"repaired item c parses as a date ({header['report_date']})",
            f"repaired item e parses as a date ({header['snapshot_date']})",
            "repaired item e is the first day of a calendar quarter, per 18 CFR 284.13(c)",
        ] + ([f"repaired item b equals the requested entity {normalise_cid(expect_cid)}"]
             if expect_cid else []),
        "original_bytes_retained": True,
    }


def parse_ioc(raw: bytes, *, expect_cid: str = "", accession: str = "") -> dict:
    """Parse one Index of Customers file.

    Tab-delimited. Encoding is DETECTED, not assumed (see `decode_ioc`). Parsed
    with a real CSV reader because Transco quotes values containing commas and
    TGP quotes nothing; `newline=""` is the csv-documented form and keeps a
    newline inside a quoted field where the filer put it. Row length is NOT a
    record-type signal (Transco pads every row to 14 columns, TGP emits the
    natural length), so every field is read by position with a bounds guard.

    Grouping is POSITIONAL: A and P records carry no contract key and belong to
    the most recent D record.

    `expect_cid` is the entity the caller is building. When given, the parse
    REFUSES a file whose header item b names a different company: name-based
    retrieval is a search key and never an identity test (audit A02).
    """
    text, meta = decode_ioc(raw)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter="\t", quotechar='"')
    all_rows = list(reader)
    rows = [r for r in all_rows if any(c.strip() for c in r)]
    meta["rows_total"] = len(all_rows)
    meta["rows_blank"] = len(all_rows) - len(rows)
    if not rows:
        raise ValueError("file contains no records")

    # ---- bounded title preamble -------------------------------------------
    header_index = next((i for i, r in enumerate(rows)
                         if _cell(r, 0).upper() == "H"), None)
    if header_index is None:
        raise ValueError(f"no H header record in {len(rows)} non-blank rows; first record "
                         f"is {_cell(rows[0], 0)!r}")
    if header_index > 0:
        preamble = rows[:header_index]
        if header_index > MAX_PREAMBLE_ROWS:
            raise ValueError(f"the H header record is at row {header_index + 1}, beyond the "
                             f"{MAX_PREAMBLE_ROWS}-row preamble bound; the file is not "
                             f"recognisably an Index of Customers and is not guessed at")
        for r in preamble:
            if _cell(r, 0).upper() in RECORD_TYPES:
                raise ValueError(f"a {_cell(r, 0)!r} record precedes the H header record; "
                                 f"the file's record order is not the documented one and is "
                                 f"not repaired")
            if sum(1 for c in r if c.strip()) > 2:
                raise ValueError(f"row {rows.index(r) + 1} before the H header carries "
                                 f"{sum(1 for c in r if c.strip())} populated fields, so it "
                                 f"is data of an unknown shape rather than a title line; "
                                 f"the file is quarantined rather than guessed at")
        meta["preamble_rows_skipped"] = [list(r) for r in preamble]
        meta["preamble_note"] = (f"{header_index} title row(s) precede the H record; they are "
                                 f"retained verbatim as unclassified records and contribute "
                                 f"to nothing")

    # ---- header, with the one audited repair -------------------------------
    h_row = rows[header_index]
    header = _read_header(h_row)
    defects = _header_defects(header)
    header_repair = {"applied": False}
    if defects:
        repaired = _repair_header(h_row, expect_cid)
        if repaired is None:
            raise ValueError("; ".join(defects))
        h_row_repaired, header_repair = repaired
        header = _read_header(h_row_repaired)
        header_repair["defects_before_repair"] = defects
    header["raw"] = list(h_row)                 # ALWAYS the bytes as filed
    header["raw_repaired"] = header_repair.get("repaired_row")
    header["repair"] = header_repair
    meta["header_repair"] = header_repair

    # ---- identity gate: the header decides ownership, never the search key --
    header["entity_gate"] = _identity_gate(header, expect_cid, accession)

    contracts: list[dict] = []
    footnotes: dict[str, list[str]] = {}
    records: list[dict] = []
    unclassified: list[dict] = []
    group = -1
    for line_no, row in enumerate(rows, start=1):
        kind = _cell(row, 0).upper()
        if kind not in RECORD_TYPES:
            # NOT reinterpreted. Discovery emits its footnote text with an empty
            # record-type column and other filers emit a bare footnote number
            # there; either could be a footnote, and "could be" is not evidence.
            # The row is retained verbatim and contributes to nothing.
            rec = {"line": line_no, "kind": "?", "group": -1, "raw": row,
                   "unclassified_code": _cell(row, 0)}
            records.append(rec)
            unclassified.append({"line": line_no, "column_1": _cell(row, 0),
                                 "raw": list(row)})
            continue
        rec = {"line": line_no, "kind": kind, "group": group, "raw": row}
        if kind == "H":
            rec["group"] = -1
        elif kind == "D":
            group = len(contracts)
            rec["group"] = group
            d = {
                "line": line_no, "_kind": "D", "_group": group,
                "shipper_name": _cell(row, 1),                       # item j
                "shipper_id": _cell(row, 2),                         # item ya (filer-local)
                "affiliate_flag": _cell(row, 3).upper(),             # item yb
                "rate_schedule": _cell(row, 4),                      # item k
                "contract_number": _cell(row, 5),                    # item yc
                "effective_date": iso_date(_cell(row, 6)),           # item l
                "primary_term_expiry": iso_date(_cell(row, 7)),      # item m
                "primary_term_expiry_raw": _cell(row, 7),
                "rollover_days": _cell(row, 8),                      # item n -- DURATION
                "negotiated_rate_flag": _cell(row, 9).upper(),       # item yd
                "transport_mdq_text": _cell(row, 10),                # item o
                "storage_quantity_text": _cell(row, 11),             # item p -- a STOCK
                "footnote_ids": _cell(row, 12),                      # item q
                "agents": [], "points": [],
            }
            d["transport_mdq"] = num(d["transport_mdq_text"])
            d["storage_quantity"] = num(d["storage_quantity_text"])
            contracts.append(d)
        elif kind == "A" and contracts:
            contracts[-1]["agents"].append({
                "line": line_no,
                "agent_name": _cell(row, 1),                          # item ye
                # the manual's instruction text says "enter the D-U-N-S number"
                # but its own format column says Y or N, and every live record
                # carries Y/N. The format column is followed.
                "affiliate_flag": _cell(row, 2).upper(),              # item yf
                "footnote_ids": _cell(row, 3)})
        elif kind == "P" and contracts:
            p = {
                "line": line_no, "_kind": "P", "_group": group,
                "point_code": _cell(row, 1).upper(),                  # item yh
                "point_name": _cell(row, 2),                          # item yi
                "qualifier": _cell(row, 3),                           # item yj (95 = TSP LOC)
                "point_id": _cell(row, 4),                            # item yk
                "zone": _cell(row, 5),                                # item yl
                "transport_qty_text": _cell(row, 6),                  # item ym
                "storage_qty_text": _cell(row, 7),                    # item yn (DAILY rate)
                "footnote_ids": _cell(row, 8)}
            p["transport_qty"] = num(p["transport_qty_text"])
            p["storage_qty"] = num(p["storage_qty_text"])
            contracts[-1]["points"].append(p)
        elif kind == "F":
            # F records are FILE-scoped, not group-scoped, and a single footnote
            # is split into 255-character segments that share a number. Reading
            # only the first segment silently truncates the meaning.
            number = _cell(row, 1)
            footnotes.setdefault(number, []).append(_cell(row, 2))
            rec["group"] = -1
        records.append(rec)

    if unclassified:
        meta["unclassified_rows"] = unclassified[:50]
        meta["unclassified_note"] = (
            f"{len(unclassified)} row(s) carry a column-1 value that is not one of the five "
            f"record types the Form 549B manual defines "
            f"({', '.join(sorted({u['column_1'] or '(blank)' for u in unclassified}))}). "
            f"They are retained verbatim as source facts and interpreted as nothing: "
            f"Discovery Gas Transmission writes its footnote text with an empty record-type "
            f"column and other filers write a bare footnote number there, so reading them as "
            f"F records would be a guess about which filer meant what")
    meta["counts"] = {k: sum(1 for r in records if r["kind"] == k)
                      for k in (*RECORD_TYPES, "?")}
    return {"header": header, "contracts": contracts, "records": records,
            "footnotes": {k: " ".join(v) for k, v in footnotes.items()},
            "unclassified": unclassified,
            "provenance": meta,
            "counts": meta["counts"]}


def _identity_gate(header: dict, expect_cid: str, accession: str) -> dict:
    """Decide, from the FILE'S OWN HEADER, whether this file belongs to the
    entity being built -- and raise if it does not (audit A02).

    The eLibrary description search is a retrieval key. `"Index of Customers
    Report of MountainWest Pipeline, LLC"` matches `"MountainWest Overthrust
    Pipeline, LLC"` too, and in the delivered run twelve Overthrust (C001087)
    indexes were stored as MountainWest Pipeline (C001088) filings -- nine of
    them as the CANONICAL file for their quarter, so MountainWest Pipeline's own
    contract book was superseded by another pipeline's. Which of the two won
    depended only on which accession number sorted last.

    Header item b is the filer's own statement of who filed. It is the gate.
    """
    found = header["pipeline_id_normalised"]
    gate = {"header_cid": found, "header_name": header["pipeline_name"],
            "requested_entity": normalise_cid(expect_cid), "accession": accession}
    if not expect_cid:
        gate["result"] = "not_gated"
        gate["basis"] = "no entity was supplied; the parse is being used for inspection only"
        return gate
    want = normalise_cid(expect_cid)
    if not CID_RE.match(want):
        raise IOCIdentityUnverifiable(
            f"the requested entity key {expect_cid!r} is not a FERC C-number, so the native "
            f"header cannot be checked against it; a filing is not accepted on a name match",
            expected=want, found=found, found_name=header["pipeline_name"],
            accession=accession)
    if not CID_RE.match(found):
        raise IOCIdentityUnverifiable(
            f"header item b carries {header['pipeline_id']!r}, which is not a FERC C-number, "
            f"so the file states no company id to check against {want}; ownership is left "
            f"unestablished rather than inferred from the filer name "
            f"{header['pipeline_name']!r}",
            expected=want, found=found, found_name=header["pipeline_name"],
            accession=accession)
    if found != want:
        raise IOCEntityMismatch(
            f"header item b names {found} ({header['pipeline_name']}), not the requested "
            f"entity {want}; the eLibrary description search matched on company NAME, which "
            f"is a retrieval key and never an identity test",
            expected=want, found=found, found_name=header["pipeline_name"],
            accession=accession)
    gate["result"] = "accepted"
    gate["basis"] = (f"header item b ({found}) equals the requested entity; ownership is "
                     f"established by the file's own header, not by the search phrase")
    return gate


def footnote_numbers(ids: str) -> list[str]:
    """`o23`, `x35/n1` -> ['23', '35', '1'].

    The leading letters are ITEM ids saying WHICH FIELD the note qualifies
    ('x' = the whole record); they are not part of the footnote number.
    """
    out = []
    for part in (ids or "").split("/"):
        m = re.search(r"(\d+)\s*$", part.strip())
        if m:
            out.append(m.group(1))
    return out


def normalise_shipper(name: str) -> str:
    """Legal-name key used to aggregate a shipper's contracts before ranking.

    Whitespace and case only -- TGP writes 'ATMOS  ENERGY  CORPORATION' with
    double spaces. No corporate-family merging is attempted: 'ATMOS ENERGY (KY),
    A DIVISION OF ATMOS ENERGY CORPORATION' stays a distinct legal name, because
    treating it as the parent would be an unsourced identity claim.
    """
    return re.sub(r"\s+", " ", (name or "").strip().upper())


# ================================================================= retrieval

def retrieve(ctx, entity, *, year_from: int, year_to: int) -> list[dict]:
    """Fetch, persist and parse every public IOC filing for one entity.

    One accession is one atomic unit: a download or parse failure leaves that
    accession absent with a blocker, and the other quarters continue.
    """
    _PENDING_EVENTS.pop(entity["entity_key"], None)
    _PENDING_POPULATIONS.pop(entity["entity_key"], None)
    _RETRIEVAL_STATE[entity["entity_key"]] = ("ok", "")
    start = f"{year_from:04d}-01-01"
    # THE QUERY BOUND IS THE DECLARED CAPTURE DATE, NOT THE WALL CLOCK.
    # The window's end date goes into the search payload and therefore into the
    # content-addressed cache key, so a wall-clock bound asks a cached capture a
    # question it cannot answer: replayed a day later, every search misses and
    # the entity silently produces no filings at all (audit A20). Record-keeping
    # timestamps stay on the real clock; only bounds move to ctx.today().
    end = min(dt.date(year_to, 12, 31), ctx.today()).isoformat()
    try:
        hits = search(ctx, entity["legal_name"], SEARCH_PHRASE, start=start, end=end,
                      document_type=CLASS_TYPE[1])
    except FetchError as exc:
        ctx.staging.open_blocker(ADAPTER, "access",
                                 f"{entity['entity_key']}: IOC search failed",
                                 scope=entity["entity_key"], attempts=str(exc.attempts),
                                 exact_error=exc.detail)
        ctx.log("error", f"{entity['entity_key']}: IOC search failed: {exc.detail}",
                adapter=ADAPTER, entity_cid=entity["entity_key"])
        _RETRIEVAL_STATE[entity["entity_key"]] = (
            "search_failed", f"the eLibrary search itself failed: {exc.detail}")
        return []

    if not hits:
        ctx.log("info", f"{entity['entity_key']} {entity['legal_name'][:40]}: no "
                        f"'{CLASS_TYPE[1]}' filings in eLibrary {start}..{end}",
                adapter=ADAPTER, entity_cid=entity["entity_key"])
        _RETRIEVAL_STATE[entity["entity_key"]] = ("no_hits", "")
        return []

    candidates = []
    for h in hits:
        acc = h.get("acesssionNumber")            # three s's -- the source's own spelling
        if not acc:
            continue
        desc = h.get("description") or ""
        snap_year, snap_quarter = _quarter_from_description(desc, h.get("filedDate"))
        data_files = [t for t in (h.get("transmittals") or [])
                      if _extension(t.get("fileName", "")) in DATA_EXTENSIONS]
        candidates.append({
            "accession": acc, "description": desc, "hit": h,
            "filed_date": iso_date(h.get("filedDate", "")),
            "posted_date": iso_date(h.get("postedDate", "")),
            "issued_date": iso_date(h.get("issuedDate", "")),
            "avail_code": (h.get("availCode") or "").upper(),
            "snapshot_year": snap_year, "snapshot_quarter": snap_quarter,
            "data_files": data_files,
            "revised_in_description": bool(re.match(r"\s*revised", desc, re.I))})

    # ORDER-INDEPENDENCE. The candidate order decides nothing about ownership --
    # that is settled by each file's own header (audit A02) -- but a stable order
    # keeps the ATTEMPT sequence, and therefore the blocker and checkpoint
    # history, reproducible between runs.
    candidates.sort(key=lambda c: (c["filed_date"], c["accession"]))
    usable, skipped = [], []
    for c in candidates:
        if c["avail_code"] != "P":
            # A METADATA AVAILABILITY CODE IS NOT AN OBSERVED HTTP STATUS.
            # availCode comes out of the search hit. Nothing is requested for
            # this accession, so nothing is observed: no status, no
            # confidentiality determination, no inference about the period.
            # All five such records in the delivered window say "Erroneously
            # filed" and every one of them has a public accession carrying the
            # same snapshot (audit A15).
            skipped.append((c, "metadata_not_public"))
            continue
        if not c["data_files"]:
            # a transmittal-letter-only accession (Transco 20251001-5142)
            skipped.append((c, "letter_only"))
            continue
        usable.append(c)

    filings: list[dict] = []
    rejected: list[tuple[dict, IOCIdentityError]] = []
    for c in usable:
        scope_key = f"ioc:{c['accession']}"
        # The identity gate runs BEFORE any write, so a file belonging to another
        # filer never reaches the database at all -- and, because nothing was
        # staged for it, it leaves no checkpoint claiming it as this entity's
        # unit of work.
        try:
            bundle = _fetch_and_parse(ctx, entity, c)
        except IOCIdentityError as exc:
            rejected.append((c, exc))
            ctx.log("info", f"{c['accession']}: not {entity['entity_key']}'s filing -- {exc}",
                    adapter=ADAPTER, entity_cid=entity["entity_key"])
            continue
        except FetchError as exc:
            ctx.staging.checkpoint(ADAPTER, entity["entity_key"], scope_key, "failed",
                                   error=exc.detail)
            ctx.staging.open_blocker(
                ADAPTER, "source", f"{entity['entity_key']} {c['accession']}: IOC file "
                                   f"not retrieved", scope=scope_key,
                attempts=str(exc.attempts), exact_error=exc.detail)
            ctx.log("error", f"{c['accession']}: {exc.detail}", adapter=ADAPTER,
                    entity_cid=entity["entity_key"])
            continue
        except (ValueError, csv.Error, zipfile.BadZipFile, UnicodeError,
                LookupError) as exc:
            # csv.Error and LookupError are here because their absence is what
            # let ONE malformed Gulfstream attachment abort that entity's whole
            # task in the delivered run, taking eight later quarters and every
            # coverage slot with it (audit A14). A parse failure is contained to
            # its own accession, always.
            ctx.staging.checkpoint(ADAPTER, entity["entity_key"], scope_key, "failed",
                                   error=f"parse: {type(exc).__name__}: {exc}")
            ctx.staging.open_blocker(ADAPTER, "source",
                                     f"{c['accession']}: IOC parse failed",
                                     scope=scope_key,
                                     exact_error=f"{type(exc).__name__}: {exc}"[:400])
            ctx.log("error", f"{c['accession']}: parse failed: {type(exc).__name__}: {exc}",
                    adapter=ADAPTER, entity_cid=entity["entity_key"])
            continue
        ctx.staging.checkpoint(ADAPTER, entity["entity_key"], scope_key, "in_progress")
        try:
            row = _persist(ctx, entity, c, bundle)
        except Exception as exc:                                        # noqa: BLE001
            ctx.staging.checkpoint(ADAPTER, entity["entity_key"], scope_key, "failed",
                                   error=f"persist: {type(exc).__name__}: {exc}")
            ctx.staging.open_blocker(ADAPTER, "source",
                                     f"{c['accession']}: IOC filing not persisted",
                                     scope=scope_key,
                                     exact_error=f"{type(exc).__name__}: {exc}"[:400])
            ctx.log("error", f"{c['accession']}: persist failed: {type(exc).__name__}: {exc}",
                    adapter=ADAPTER, entity_cid=entity["entity_key"])
            continue
        filings.append(row)
        ctx.staging.checkpoint(ADAPTER, entity["entity_key"], scope_key, "done")
        resolve_blockers(ctx, ADAPTER, scope_key)

    if filings:
        resolve_blockers(ctx, ADAPTER, entity["entity_key"])
    accepted_candidates = [c for c in usable
                           if any(f["filing_id"] == c["accession"] for f in filings)]
    if usable and not filings and len(rejected) < len(usable):
        _RETRIEVAL_STATE[entity["entity_key"]] = (
            "download_failed",
            f"{len(usable) - len(rejected)} public data-carrying accessions belonging to this "
            f"entity were located but none could be retrieved or parsed; see the open "
            f"blockers for the exact errors")
    elif usable and not filings and rejected:
        _RETRIEVAL_STATE[entity["entity_key"]] = (
            "no_owned_filings",
            f"all {len(rejected)} data-carrying accessions returned by the description search "
            f"name a different filer in their own header record, so none of them is this "
            f"entity's index; no Index of Customers filed BY this entity was located")
    _mark_canonical(ctx, entity, filings)
    _record_associations(ctx, entity, rejected)
    _record_skipped(ctx, entity, skipped, filings, candidates)
    ctx.log("info", f"{entity['entity_key']} {entity['legal_name'][:34]}: "
                    f"{len(filings)} IOC files parsed over "
                    f"{len({f['snapshot_date'] for f in filings})} "
                    f"snapshots ({len(skipped)} accessions skipped, "
                    f"{len(rejected)} rejected on the header identity gate)",
            adapter=ADAPTER, entity_cid=entity["entity_key"])
    return filings


def _extension(name: str) -> str:
    return pathlib.PurePath(name or "").suffix.lstrip(".").lower()


def _quarter_from_description(desc: str, filed: str) -> tuple[int, str]:
    """Snapshot quarter from the machine-generated description; the header's own
    item `e` overrides this once the file is parsed."""
    m = re.search(r"for\s+Q(\d)\s+of\s+(\d{4})", desc or "", re.I)
    if m:
        return int(m.group(2)), f"Q{m.group(1)}"
    d = iso_date(filed or "")
    if d:
        y, mo = int(d[:4]), int(d[5:7])
        return y, QUARTER_OF_MONTH[mo]
    return 0, ""


def _fetch_and_parse(ctx, entity, c: dict) -> dict:
    """Retrieve and parse one accession. WRITES NOTHING.

    Every reason to refuse a file -- a download failure, a malformed file, and
    above all a header naming another company -- is reached here, before a
    single row is staged. That ordering is what makes the identity gate real
    rather than a correction applied afterwards.
    """
    acc = c["accession"]
    listing = file_list(ctx, acc)
    ids = [d["ID"] for d in listing if d.get("ID")]
    if not ids:
        raise FetchError(f"{ELIB}/File/GetFileListFromP8/{acc}", "no attachments listed")
    blob, entry = download(ctx, acc, ids)
    want = c["data_files"][0]["fileName"]
    parts = members(blob, acc, want)
    data_name, data_bytes = None, None
    for name, body in parts.items():
        if _extension(name) in DATA_EXTENSIONS:
            data_name, data_bytes = name, body
            break
    if data_bytes is None:
        raise ValueError(f"downloaded payload carries no {sorted(DATA_EXTENSIONS)} member "
                         f"(members: {sorted(parts)})")

    # expect_cid makes the gate part of the parse: a mismatch raises here, and
    # the caller never gets a bundle it could accidentally persist.
    parsed = parse_ioc(data_bytes, expect_cid=entity["entity_key"], accession=acc)
    header = parsed["header"]
    snapshot = header["snapshot_date"]
    if (c["snapshot_year"], c["snapshot_quarter"]) != (
            int(snapshot[:4]), QUARTER_OF_MONTH[int(snapshot[5:7])]):
        ctx.log("warn", f"{acc}: description says {c['snapshot_quarter']} "
                        f"{c['snapshot_year']} but header item e says {snapshot}; "
                        f"the header wins",
                adapter=ADAPTER, entity_cid=entity["entity_key"])
    if parsed["provenance"]["header_repair"].get("applied"):
        ctx.log("warn", f"{acc}: header record repaired "
                        f"({parsed['provenance']['header_repair']['transformation']}); the "
                        f"original bytes are retained and the repair is recorded on the "
                        f"filing", adapter=ADAPTER, entity_cid=entity["entity_key"])
    return {"parsed": parsed, "data_name": data_name, "data_bytes": data_bytes,
            "entry": entry, "listing": listing}


def _persist(ctx, entity, c: dict, bundle: dict) -> dict:
    """Stage one accepted filing. Only ever reached after the identity gate."""
    acc = c["accession"]
    parsed = bundle["parsed"]
    data_name, data_bytes, entry = (bundle["data_name"], bundle["data_bytes"],
                                    bundle["entry"])
    header = parsed["header"]
    snapshot = header["snapshot_date"]
    year = int(snapshot[:4])
    quarter = QUARTER_OF_MONTH[int(snapshot[5:7])]

    content_hash = sha256(data_bytes)
    # snapshot_date= makes the shared classifier group on the header as-of date
    # instead of (form, year, period): two indexes filed in one quarter with
    # different item e values are two indexes, not a revision of one.
    version_status, supersedes = ctx.staging.classify_version(
        SOURCE_SYSTEM, entity["entity_key"], FORM, year, quarter, acc, content_hash,
        snapshot_date=snapshot, submitted_on=c["filed_date"])

    doc_id = f"{SOURCE_SYSTEM}|{acc}|{c['data_files'][0].get('fileId') or data_name}"
    filing = {
        "source_system": SOURCE_SYSTEM, "filing_id": acc,
        "entity_key": entity["entity_key"], "form": FORM, "accession_number": acc,
        "reporting_year": year, "reporting_period": quarter,
        # a snapshot is an instant, not an interval: period_start/end stay NULL
        "period_start": None, "period_end": None,
        "filed_date": c["filed_date"], "posted_date": c["posted_date"],
        "issued_date": c["issued_date"],
        "effective_date": snapshot, "submitted_on": c["filed_date"],
        "snapshot_date": snapshot,
        "acceptance_status": f"availCode={c['avail_code']}",
        "taxonomy_version": FORMAT_VERSION,
        "schema_ref": "https://www.ferc.gov/sites/default/files/2020-05/elec-inst_2.pdf",
        "content_hash": content_hash,
        "is_canonical": 0, "canonical_reason": "",
        "version_status": version_status, "supersedes_filing_id": supersedes,
        "data_origin": "document",
        "retrieved_at": entry["last_seen_at"], "first_seen_at": entry["first_seen_at"],
        "source_url": redact(DOCINFO.format(acc)),
    }
    document = {
        "document_id": doc_id, "source_system": SOURCE_SYSTEM, "filing_id": acc,
        "accession_number": acc,
        "attachment_id": str(c["data_files"][0].get("fileId") or ""),
        "title": data_name, "class_type": " / ".join(CLASS_TYPE),
        "media_type": _media_type(parsed["provenance"]),
        "byte_size": len(data_bytes),
        "content_hash": content_hash, "cache_path": entry.get("cache_path"),
        "text_layer": "yes", "availability": "retrieved",
        "retrieved_at": entry["last_seen_at"],
        "source_url": redact(FILELIST.format(acc))}

    facts = _facts(parsed, acc, snapshot)
    ctx.staging.write_filing_bundle(filing, facts=facts, documents=[document])
    # OWNERSHIP is single-valued and now recorded as such: exactly one owner
    # link per filing, established by the file's own header record.
    _link_filing_entity(ctx, acc, entity["entity_key"], "owner",
                        header["entity_gate"]["basis"])

    filing["_parsed"] = parsed
    filing["_document_id"] = doc_id
    filing["_data_file"] = data_name
    filing["_hit"] = c
    filing["_original_revised"] = header["original_revised"]
    return filing


def _media_type(provenance: dict) -> str:
    """The attachment's own encoding, not a guess.

    `text/plain` with no charset told a downstream reader nothing, and is what
    made a UTF-16 attachment look like a corrupt one.
    """
    enc = provenance.get("encoding") or ""
    charset = {"utf-8-sig": "utf-8", "utf-16-le": "utf-16le",
               "utf-16-be": "utf-16be"}.get(enc, enc)
    return f"text/plain; charset={charset}" if charset else "text/plain"


def _link_filing_entity(ctx, filing_id: str, entity_key: str, link_type: str,
                        basis: str) -> bool:
    """Record a filing-to-entity link.

    A DOCUMENT may be connected to several entities -- it names them, it was
    returned by their search, it discusses their system -- while a FILING has
    exactly one owner. Collapsing those two into the single `filings.entity_key`
    column is what allowed a search hit to silently rewrite ownership (audit
    A02). `filing_entity_links` keeps them apart, with `link_type='owner'`
    unique per filing.

    Written only when the integrator's table exists; the association is ALSO
    recorded as an event, so nothing is lost while that lands. See
    `requests/w2-ioc_shared_requests.md`.
    """
    if not _has_table(ctx, "filing_entity_links"):
        return False
    ctx.staging.upsert("filing_entity_links", [{
        "source_system": SOURCE_SYSTEM, "filing_id": filing_id,
        "entity_key": entity_key, "link_type": link_type, "basis": basis,
        "established_by": ADAPTER, "first_seen_at": utcnow()}],
        ["source_system", "filing_id", "entity_key", "link_type"])
    return True


_TABLE_CACHE: dict[int, dict[str, bool]] = {}


def _has_table(ctx, name: str) -> bool:
    cache = _TABLE_CACHE.setdefault(id(ctx.staging), {})
    if name not in cache:
        cache[name] = bool(ctx.staging.query(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))
    return cache[name]


def _facts(parsed: dict, accession: str, snapshot: str) -> list[dict]:
    """Every record retained verbatim, with its parsed fields alongside.

    P records are kept in full for location detail; nothing is summarised away
    at this layer. `context_id` carries the positional contract group, which is
    the ONLY link A and P records have to their D record.
    """
    out = []
    by_line = {}
    for c in parsed["contracts"]:
        by_line[c["line"]] = c
        for a in c["agents"]:
            by_line[a["line"]] = a
        for p in c["points"]:
            by_line[p["line"]] = p
    for order, rec in enumerate(parsed["records"]):
        kind = rec["kind"]
        payload = {k: v for k, v in (by_line.get(rec["line"]) or {}).items()
                   if k not in ("agents", "points", "raw") and not k.startswith("_")}
        if kind == "H":
            # The header fact carries the whole PARSE PROVENANCE: how the bytes
            # were decoded, what preamble was skipped, whether the one audited
            # header repair fired and on what evidence, and what the identity
            # gate concluded. `value_as_filed` below stays the ORIGINAL row, so
            # the repair can always be checked against the bytes it came from.
            payload = {k: v for k, v in parsed["header"].items() if k != "raw"}
            payload["parse_provenance"] = parsed["provenance"]
        elif kind == "F":
            number = rec["raw"][1].strip() if len(rec["raw"]) > 1 else ""
            payload = {"footnote_number": number,
                       "segment_text": rec["raw"][2] if len(rec["raw"]) > 2 else ""}
        elif kind == "?":
            payload = {"unclassified_column_1": rec.get("unclassified_code", ""),
                       "note": ("column 1 is not one of the five record types the Form 549B "
                                "manual defines; the row is retained exactly as filed and "
                                "is interpreted as nothing")}
        out.append({
            "source_system": SOURCE_SYSTEM, "filing_id": accession,
            "source_fact_id": f"{'X' if kind == '?' else kind}{rec['line']:05d}",
            "document_order": order,
            "concept_qname": f"ferc549b:{kind}",
            "concept_local": {"H": "ioc_header_record", "D": "ioc_detail_record",
                              "A": "ioc_agent_record", "P": "ioc_point_record",
                              "F": "ioc_footnote_record",
                              "?": "ioc_unclassified_record"}.get(kind, "ioc_record"),
            "context_id": f"contract{rec['group']:04d}" if rec["group"] >= 0 else "file",
            "unit_id": parsed["header"]["uom_transport_code"],
            "unit_text": parsed["header"]["uom_transport"],
            "decimals": "", "precision": "",
            "value_as_filed": "\t".join(rec["raw"]),
            "is_nil": 0, "period_class": periods.SNAPSHOT, "instant": snapshot,
            "period_start": None, "period_end": None, "duration_days": "",
            "current_or_prior": "current",
            "explicit_dims_json": "", "typed_dims_json": json.dumps(payload, sort_keys=True),
            "taxonomy_version": FORMAT_VERSION})
    return out


def _mark_canonical(ctx, entity, filings: list[dict]) -> None:
    """One canonical file per (filer, snapshot quarter): the greatest filedDate
    that actually carries a data file and is public.

    That single rule handles all four anomaly classes observed -- a withdrawn
    401ing accession, a letter-only accession, a silent refile with the same
    filename and different content, and a proper .TA1 revision.
    """
    groups: dict[str, list[dict]] = {}
    for f in filings:
        # grouped on the header SNAPSHOT date: two files are alternatives for the
        # same index only when they describe the same as-of date. A file with a
        # different item e is a different snapshot and supersedes nothing.
        groups.setdefault(f["snapshot_date"], []).append(f)
    for key, grp in groups.items():
        # Reconcile against EVERY persisted occurrence for this native-header
        # owner and snapshot, not just the order in which this invocation
        # happened to replay them. This also repairs an existing audited DB whose
        # older classifier created reciprocal/forward supersession pointers.
        normal = ctx.staging.normalize_revision_group(
            SOURCE_SYSTEM, entity["entity_key"], FORM,
            snapshot_date=key, set_canonical=True)
        by_id = {f["filing_id"]: f for f in normal}
        for f in grp:
            fixed = by_id[f["filing_id"]]
            for field in ("version_status", "supersedes_filing_id",
                          "is_canonical", "canonical_reason"):
                f[field] = fixed[field]
        if len(normal) > 1:
            _revision_events(ctx, entity, normal, normal[-1])


def _revision_events(ctx, entity, grp: list[dict], newest: dict) -> None:
    """"A revised file arrived" is recorded as its own event, separately from
    any economic change. An identical resubmission is never an economic change."""
    rows = []
    for f in grp[:-1]:
        identical = f["content_hash"] == newest["content_hash"]
        rows.append({
            "event_id": "evt-" + hashlib.sha256(
                f"ioc-revision|{newest['filing_id']}|{f['filing_id']}"
                .encode()).hexdigest()[:24],
            "entity_key": entity["entity_key"],
            "asset_ids": json.dumps(sorted(a["asset_id"] for a in entity["assets"])),
            "event_class": "revision",
            "event_type": "identical_resubmission" if identical else "revised_filing",
            "headline": (f"Index of Customers for {newest['reporting_period']} "
                         f"{newest['reporting_year']} refiled as {newest['filing_id']}"),
            "detail": ("byte-identical content under a new accession: a restatement of the "
                       "same snapshot, not an economic change"
                       if identical else
                       f"accession {newest['filing_id']} (filed {newest['filed_date']}, "
                       f"header indicator {newest.get('_original_revised', '?')}) restates "
                       f"the {newest['snapshot_date']} snapshot filed as {f['filing_id']}; "
                       "the snapshot date is unchanged and any dependent comparison is "
                       "recomputed on the restated file"),
            "destination": "filing_archive",
            "source_system": SOURCE_SYSTEM, "filing_id": newest["filing_id"],
            "accession_number": newest["filing_id"], "document_id": None, "docket": None,
            "reporting_date": newest["snapshot_date"],
            "source_filed_date": newest["filed_date"],
            "source_posted_date": newest["posted_date"],
            "effective_date": newest["snapshot_date"],
            "first_seen_at": utcnow(), "is_backfill": 1,
            "comparison_basis": f"{f['filing_id']} -> {newest['filing_id']}",
            "confidence_note": "version classified on the sha256 of the data file bytes"})
    if rows:
        _PENDING_EVENTS.setdefault(entity["entity_key"], []).extend(rows)


def _record_associations(ctx, entity, rejected: list) -> None:
    """A document the search returned that belongs to ANOTHER filer.

    Refusing it is not enough: the connection is real and useful -- it is why a
    name search finds it, and it is the trace that explains an otherwise
    unexplained absence. So the link is kept as an ASSOCIATION, on the entity
    that searched, naming the entity whose header owns the file. Ownership stays
    where the header put it.
    """
    rows = []
    for c, exc in rejected:
        kind = ("other_entity" if isinstance(exc, IOCEntityMismatch)
                else "identity_unverifiable")
        _link_filing_entity(
            ctx, c["accession"], entity["entity_key"],
            "named_in_search_not_owned" if kind == "other_entity" else "identity_unverifiable",
            f"returned by the description search for {entity['legal_name']!r}; "
            f"header item b names {exc.found or '(no company id)'} "
            f"({exc.found_name or 'unnamed'})")
        rows.append({
            "event_id": "evt-" + hashlib.sha256(
                f"ioc-identity|{entity['entity_key']}|{c['accession']}".encode()
            ).hexdigest()[:24],
            "entity_key": entity["entity_key"],
            "asset_ids": json.dumps(sorted(a["asset_id"] for a in entity["assets"])),
            "event_class": "identity",
            "event_type": ("filing_owned_by_another_entity" if kind == "other_entity"
                           else "filing_ownership_unverifiable"),
            "headline": (f"{c['accession']}: Index of Customers filed by "
                         f"{exc.found} ({exc.found_name}), not by "
                         f"{entity['entity_key']}"
                         if kind == "other_entity" else
                         f"{c['accession']}: Index of Customers ownership could not be "
                         f"established from the file's own header"),
            "detail": (f"{exc}. The eLibrary description search for "
                       f"{entity['legal_name']!r} returned this accession because the "
                       f"description contains that company name as a substring; header "
                       f"item b, the filer's own statement of who filed, says "
                       f"{exc.found or '(no company id)'}. The file is associated with "
                       f"{entity['entity_key']} and OWNED by "
                       f"{exc.found or 'no established entity'}; no row of it was written "
                       f"under {entity['entity_key']}. "
                       f"Search description: {c['description'][:200]}")[:800],
            "destination": "data_review_queue", "source_system": SOURCE_SYSTEM,
            "filing_id": c["accession"], "accession_number": c["accession"],
            "document_id": None, "docket": None,
            "reporting_date": None, "source_filed_date": c["filed_date"],
            "source_posted_date": c["posted_date"], "effective_date": None,
            "first_seen_at": utcnow(), "is_backfill": 1,
            "comparison_basis": f"header item b {exc.found} vs requested {exc.expected}",
            "confidence_note": ("company id read from header item b of the native file; a "
                                "company NAME match is never treated as identity")})
    if rows:
        _PENDING_EVENTS.setdefault(entity["entity_key"], []).extend(rows)


def _record_skipped(ctx, entity, skipped, filings, candidates) -> None:
    """Accessions that were located but never requested, recorded as exactly that.

    The delivered code labelled every non-`P` availability code
    "DownloadP8File returns HTTP 401" while making no request at all. An HTTP
    status that was never observed may not be reported, and an availability code
    is not a confidentiality determination (audit A15). What IS reported here:
    the metadata code, that no request was attempted, the filer's own
    description, and which public accession carries the same snapshot.
    """
    rows = []
    for c, why in skipped:
        scope = f"{entity['entity_key']}:{c['accession']}"
        if why != "metadata_not_public":
            rows.append(_skip_event(entity, c, "no_data_attachment",
                                    f"{c['accession']}: IOC accession carries a transmittal "
                                    f"letter and no data file",
                                    c["description"][:400],
                                    "attachment list read from the eLibrary search hit"))
            continue

        alternatives = _public_alternatives(c, candidates)
        satisfied = [f for f in filings
                     if f["reporting_year"] == c["snapshot_year"]
                     and f["reporting_period"] == c["snapshot_quarter"]]
        evidence = {
            "metadata_availability_code": c["avail_code"] or "(blank)",
            "metadata_description": c["description"],
            "http_request_attempted": False,
            "http_status_observed": None,
            "confidentiality_established": False,
            "basis": ("availCode is a field of the eLibrary search response. This adapter "
                      "made no request for this accession, so it observed no HTTP status, "
                      "and it does not infer confidentiality, CEII, or a reporting gap from "
                      "a metadata code. No access control was probed or bypassed."),
            "public_alternatives_in_the_same_search": [
                {"accession": a["accession"], "availCode": a["avail_code"],
                 "filed_date": a["filed_date"],
                 "data_files": [t.get("fileName") for t in a["data_files"]],
                 "description": a["description"][:160]} for a in alternatives],
            "period_satisfied_by": [
                {"filing_id": f["filing_id"], "snapshot_date": f["snapshot_date"],
                 "is_canonical": f["is_canonical"]} for f in satisfied],
            "official_routes_not_yet_exhausted": (
                [] if satisfied else
                [f"eLibrary docinfo for the same quarter: "
                 f"{redact(DOCINFO.format(c['accession']))}",
                 "the FERC quarterly Index of Customers bulk download published on ferc.gov, "
                 "which is a separate official route and was NOT attempted by this adapter"]),
        }
        bid = ctx.staging.open_blocker(
            ADAPTER, "access",
            f"{entity['entity_key']} {c['accession']}: eLibrary metadata declares availCode "
            f"{c['avail_code'] or 'blank'}; NO HTTP request was attempted for this accession, "
            f"so no HTTP status was observed and no access restriction is asserted"
            + (f" -- the {c['snapshot_quarter']} {c['snapshot_year']} snapshot is carried by "
               f"public accession {satisfied[0]['filing_id']}" if satisfied else ""),
            scope=scope, attempts="0",
            exact_error=json.dumps(evidence, sort_keys=True)[:4000],
            human_decision=not satisfied)
        if satisfied:
            # The period is answered by a public occurrence, so nothing is
            # blocked -- but the erroneous occurrence is NOT erased: its event
            # below, and this blocker's own resolved row, both survive.
            _resolve_blocker(
                ctx, bid,
                f"resolved without any retrieval attempt on {c['accession']}: the "
                f"{c['snapshot_quarter']} {c['snapshot_year']} snapshot is carried by public "
                f"accession {satisfied[0]['filing_id']} (availCode P), which is stored and "
                f"canonical. The metadata-non-public occurrence is retained as an event and "
                f"as this blocker record; it is not deleted and it is not counted as a "
                f"reporting gap.")
        rows.append(_skip_event(
            entity, c, "metadata_declared_not_public",
            f"{c['accession']}: eLibrary metadata declares availCode "
            f"{c['avail_code'] or 'blank'}; no request was made and no HTTP status observed",
            (f"Filer's own description: {c['description'][:200]} | "
             f"No HTTP request was attempted for this accession, so no status was observed "
             f"and no confidentiality is asserted. "
             + (f"The {c['snapshot_quarter']} {c['snapshot_year']} snapshot is satisfied by "
                f"public accession {satisfied[0]['filing_id']}; this occurrence is retained "
                f"in its own right and is not a reporting gap."
                if satisfied else
                f"No public accession carrying the {c['snapshot_quarter']} "
                f"{c['snapshot_year']} snapshot has been retrieved yet"
                + (f"; the search did return public candidate(s) "
                   f"{', '.join(a['accession'] for a in alternatives)} for this period, which "
                   f"a run with network access will retrieve" if alternatives else
                   "; the period remains unresolved, NOT established as restricted")))[:800],
            "availCode read from the eLibrary search hit; no HTTP request was made"))
    if rows:
        _PENDING_EVENTS.setdefault(entity["entity_key"], []).extend(rows)


def _skip_event(entity, c, event_type, headline, detail, confidence) -> dict:
    return {
        "event_id": "evt-" + hashlib.sha256(
            f"ioc-skip|{c['accession']}".encode()).hexdigest()[:24],
        "entity_key": entity["entity_key"],
        "asset_ids": json.dumps(sorted(a["asset_id"] for a in entity["assets"])),
        "event_class": "data_quality", "event_type": event_type,
        "headline": headline, "detail": detail,
        "destination": "data_review_queue", "source_system": SOURCE_SYSTEM,
        "filing_id": c["accession"], "accession_number": c["accession"],
        "document_id": None, "docket": None,
        "reporting_date": None, "source_filed_date": c["filed_date"],
        "source_posted_date": c["posted_date"], "effective_date": None,
        "first_seen_at": utcnow(), "is_backfill": 1,
        "comparison_basis": "", "confidence_note": confidence}


def _public_alternatives(c: dict, candidates: list[dict]) -> list[dict]:
    """Other accessions in the SAME official search covering the same snapshot
    quarter that the metadata marks public and that carry a data file."""
    return [a for a in candidates
            if a["accession"] != c["accession"]
            and a["avail_code"] == "P" and a["data_files"]
            and (a["snapshot_year"], a["snapshot_quarter"])
            == (c["snapshot_year"], c["snapshot_quarter"])]


def _resolve_blocker(ctx, blocker_id: str, resolution: str) -> None:
    """Close one blocker, recording WHY. The row is retained, never deleted."""
    rows = ctx.staging.query("SELECT * FROM blockers WHERE blocker_id=?", (blocker_id,))
    if not rows:
        return
    row = dict(rows[0])
    row["resolved_at"] = utcnow()
    row["exact_error"] = (row.get("exact_error") or "") + f"\nRESOLUTION: {resolution}"
    row["human_decision_needed"] = 0
    ctx.staging.upsert("blockers", [row], ["blocker_id"])


# ================================================================= expected

def _requirement(has_filings: bool, metric) -> tuple[str, str]:
    if not has_filings:
        # We cannot distinguish "this filer has no Part 284 firm customers" from
        # "it files under a different legal name" without a FERC source that says
        # so, and we will not guess. That is an UNKNOWN applicability, not a gap.
        return coverage.UNKNOWN, (
            "no Index of Customers filing was found in eLibrary under this legal name in "
            "the requested window; 18 CFR 284.13(c) applies to pipelines with firm "
            "transportation or storage customers under Part 284 and whether this filer "
            "carries that obligation was not established from a FERC source")
    if metric.id == "ioc_firm_transport_mdq":
        return coverage.REQUIRED, (
            "18 CFR 284.13(c): the quarterly index reports every firm transportation "
            "customer under contract as of the first day of the calendar quarter "
            "(D record item o)")
    if metric.id == "ioc_contracted_storage_quantity":
        return coverage.CONDITIONAL, (
            "18 CFR 284.13(c): reported where the filer has firm storage customers "
            "(D record item p, the quantity the pipeline is obligated to store)")
    return coverage.CONDITIONAL, (
        "derived from the D, P and A records of the same snapshot; required when those "
        "records are present")


def freeze_expected(ctx, entity, filings: list[dict], assets: list[dict]) -> list[dict]:
    """Frozen BEFORE any value is computed.

    ONE SLOT PER SNAPSHOT PER METRIC. Each quarterly index is its own requested
    observation with its own as-of date, so the slot carries that date and the
    denominator is the work actually requested of the filer: a filer with twelve
    quarterly indexes is asked for twelve of each metric, not one.

    The frozen grid is built from which filings EXIST, never from what they
    contain -- no value is looked at here.
    """
    template = _template_of(entity, assets)
    asset_id = assets[0]["asset_id"] if assets else ""
    metrics = [m for m in BY_ADAPTER[ADAPTER] if template in m.templates]
    canonical = sorted((f for f in filings if f.get("is_canonical")),
                       key=lambda f: f["snapshot_date"])

    # No index located: the applicability itself is unresolved, and there is no
    # source-dated slot to hang it on, so one undated slot per metric records
    # exactly that -- and says so in its evidence.
    periods_wanted = ([(f["snapshot_date"], f["reporting_year"], f["reporting_period"])
                       for f in canonical]
                      or [("", ctx.args.year_to, "Q4")])

    out, seen = [], set()
    for as_of, year, period in periods_wanted:
        for m in metrics:
            requirement, evidence = _requirement(bool(canonical), m)
            if as_of and m.id == "ioc_mdq_change" and as_of == periods_wanted[0][0]:
                # a change needs a predecessor: the first snapshot in the window
                # has none, and that is a property of the window, not a gap
                requirement = coverage.CONDITIONAL
                evidence = ("a snapshot-to-snapshot change requires a preceding snapshot; "
                            "this is the earliest snapshot retrieved in the window")
            slot = coverage.build_expected(
                entity["entity_key"], asset_id, template, m, REGIME, periods.SNAPSHOT,
                year, period, requirement, evidence, APPLICABILITY_BASIS,
                ctx.staging.run_id, as_of=as_of)
            if slot["slot_id"] not in seen:
                seen.add(slot["slot_id"])
                out.append(slot)
    return out


# ================================================================= observations

def _obs(entity_key, m, *, instant, year, period, scope, unit, value_text, value_num,
         availability, origin=Origin.ELIBRARY_DOCUMENT, method=Method.FILED,
         version_status=VersionStatus.ORIGINAL, validation=Validation.PASS,
         qa_flags="", missing_reason="", filing_id="", source_fact_id=None,
         context_id=None, document_id=None, accession=None, selector="",
         derivation="", notes="", candidate_count=None,
         applicability_evidence="") -> dict:
    return _flag_unit_family({
        "observation_id": observation_id(entity_key, m.id, REGIME, periods.SNAPSHOT,
                                         "", "", instant or "", scope, unit or "", method),
        "entity_key": entity_key, "metric_id": m.id, "source_regime": REGIME,
        "period_basis": periods.SNAPSHOT, "period_start": None, "period_end": None,
        "instant_date": instant or None, "reporting_year": year,
        "reporting_period": period,
        "period_label": f"{year}{period} snapshot {instant}" if instant else f"{year}{period}",
        "scope": scope, "unit": unit, "value_text": value_text, "value_num": value_num,
        "normalized_iso": instant or None,
        "availability": availability, "origin": origin, "method": method,
        "version_status": version_status, "validation": validation,
        "source_system": SOURCE_SYSTEM, "filing_id": filing_id or None,
        "source_fact_id": source_fact_id, "source_context_id": context_id,
        "document_id": document_id, "accession_number": accession,
        "candidate_count": candidate_count,
        "selector": selector or m.selector, "derivation": derivation,
        "concept_local": m.concept or None, "concept_qname": None,
        "taxonomy_version": FORMAT_VERSION, "registry_version": REGISTRY_VERSION,
        "applicability_version": APPLICABILITY_BASIS,
        "schedule_page": m.schedule, "taxonomy_label": None,
        "qa_flags": qa_flags, "review_status": "",
        "missing_reason": missing_reason,
        "applicability_evidence": applicability_evidence, "notes": notes,
    }, m)


def declared_unit_family(metric) -> str:
    """The metric's PRIMARY declared unit family, or '' if it declares none.

    `unit_rule` is prose with a token in it -- `Dth/day`, `Dth (quantity, not
    per-day)`, `Dth/day by bucket`, `percent`, `codes` -- so the first token the
    registry's own vocabulary recognises is taken and nothing is inferred from
    the rest. An unrecognised rule claims no family rather than a wildcard.
    """
    canonical = (getattr(metric, "canonical_unit", "") or "").strip()
    if known_unit(canonical):
        return unit_family_of(canonical)
    for token in re.split(r"[\s,]+", (getattr(metric, "unit_rule", "") or "")):
        if known_unit(token):
            return unit_family_of(token)
    return ""


#: `unit_rule` phrasings that DECLINE to name a unit on purpose, as opposed to
#: naming one this reader cannot parse. The two are different facts and the
#: distinction is the whole point of `unit_rule_state`.
_DELIBERATELY_OPEN_RULE = ("as reported", "as stated", "as filed", "no unit",
                           "narrative", "date")


def unit_rule_state(metric) -> str:
    """`resolved` | `open` | `unparseable` | `absent`, for a metric's `unit_rule`.

    `declared_unit_family()` returns `""` for three different reasons -- the
    metric declares no rule, declares one that deliberately leaves the unit open,
    or declares one in prose no reader can resolve -- and callers cannot tell
    which. That is the permissive-default pattern in my own accessor: an
    unparseable rule read as "no unit constraint", which silently switches the
    unit check off instead of reporting that it could not run.

    Registry-wide today, 102 rules resolve (74 directly, 28 more only because
    this module reads them by first-recognised-token), 17 are deliberately open,
    and exactly one -- `i311_contract_expiry`, "by weight" -- resolves for
    nobody. No IOC metric is in that last group, so this has no occupant here --
    but 19 metrics already use a `utr:dth | ferc:dth (...)` rule style that
    survives only token-scanning, so an IOC rule rewritten that way would
    silently disarm every unit check in this file. Reported rather than guessed.
    """
    rule = (getattr(metric, "unit_rule", "") or "").strip()
    if not rule:
        return "absent"
    if declared_unit_family(metric):
        return "resolved"
    if any(o in rule.lower() for o in _DELIBERATELY_OPEN_RULE):
        return "open"
    return "unparseable"


def admissible_unit_families(metric) -> set[str]:
    """The families the registry ADMITS for a metric.

    `admissible_units` is the registry's explicit list, and for
    `ioc_expiry_profile` it deliberately spans two families -- FERC's Index of
    Customers profiles a transport MDQ (a per-day rate) and a storage quantity
    (not per-day) in one report, distinguished by scope rather than by unit.
    An empty list means the metric makes no such declaration, and then the
    primary family is the only one claimed.
    """
    units = tuple(getattr(metric, "admissible_units", ()) or ())
    families = {unit_family_of(u) for u in units if known_unit(u)}
    return families or {declared_unit_family(metric)} - {""}


def _flag_unit_family(o: dict, metric) -> dict:
    """Qualify a value whose unit is not the one its metric's slot asked for.

    ADMISSIBLE IS NOT THE SAME AS ACCEPTED, and keeping those apart is the whole
    job here. `ioc_expiry_profile` is computed on whichever contracted quantity
    a snapshot carries -- a transportation MDQ (item o, a RATE) or a contracted
    storage quantity (item p, a STOCK) -- and the registry now admits both, with
    its own evidence for why. So a storage-weighted profile is not an error and
    must not be refused. But it is also not the rate the metric's primary rule
    names, so it is reported as filed and QUALIFIED rather than validated. The
    value stands; only the claim about it is reduced.

    A unit the registry does not admit at all is a stronger condition and is
    flagged as such. Nothing is ever converted and no family is inferred from a
    unit the registry's vocabulary does not recognise.
    """
    unit = o.get("unit") or ""
    if not unit or not known_unit(unit):
        return o
    primary = declared_unit_family(metric)
    admissible_units = tuple(getattr(metric, "admissible_units", ()) or ())
    actual = unit_family_of(unit)
    note = ""
    if admissible_units and unit not in admissible_units:
        note = (f"UNIT NOT ADMITTED: this value is in {unit} ({actual}), which is not among "
                f"the units the registry admits for {metric.id} "
                f"({', '.join(admissible_units)}). No conversion is applied and no family is "
                f"inferred from the mismatch; the figure is reported exactly as filed and "
                f"qualified")
    elif admissible_units and actual != primary:
        # The registry has looked at this and said yes, with its reasons. Quote
        # them rather than paraphrasing, so the claim cannot drift from the
        # declaration it rests on.
        evidence = (getattr(metric, "admissible_units_evidence", "") or "").strip()
        note = (f"UNIT FAMILY: this value is in {unit} ({actual}) while {metric.id}'s primary "
                f"declared unit is {metric.unit_rule!r} ({primary}). The registry ADMITS this "
                f"unit"
                + (f" -- {evidence}" if evidence else "")
                + f" -- so it is not a conversion error, and the weight this figure is "
                  f"computed over is named in this observation's scope and in its denominator "
                  f"lineage edge. Admissible is not the same as accepted against the primary "
                  f"rule, so the figure is reported as filed and qualified rather than "
                  f"validated")
    elif not primary and unit_rule_state(metric) == "unparseable":
        # THE CHECK COULD NOT RUN, which is not the same as the check passing.
        # Without this branch an unresolvable rule silently switches unit
        # validation off and the value still reports `pass` -- the accessor's own
        # version of the permissive default. Validation drops to
        # NOT_YET_VALIDATED rather than UNIT_WARNING, because there is no
        # evidence of a unit problem; there is an absence of evidence either way.
        note = (f"UNIT NOT CHECKED: {metric.id} declares {metric.unit_rule!r}, which does not "
                f"resolve to a unit in the registry's vocabulary and lists no "
                f"`admissible_units`, so this value's unit ({unit}) was compared against "
                f"nothing. That is an unrun check, not a passed one, and no family is "
                f"inferred from the failure to parse")
        o["qa_flags"] = "; ".join(x for x in (o.get("qa_flags"), note) if x)
        if o.get("validation") == Validation.PASS:
            o["validation"] = Validation.NOT_YET_VALIDATED
        return o
    elif primary and actual != primary:
        # NO admissibility declaration exists. Saying "the registry admits this"
        # here would assert something nobody has established -- silence is not
        # permission, and it is not refusal either.
        note = (f"UNIT FAMILY: this value is in {unit} ({actual}) while {metric.id} declares "
                f"{metric.unit_rule!r} ({primary}). The registry lists no admissible units "
                f"for this metric, so nothing establishes that {unit} is acceptable for it "
                f"and nothing establishes that it is not. No conversion is applied, no family "
                f"is inferred from the mismatch; the figure is reported exactly as filed and "
                f"qualified, and an admissible-unit declaration is what would settle it")
    if not note:
        return o
    o["qa_flags"] = "; ".join(x for x in (o.get("qa_flags"), note) if x)
    if o.get("validation") == Validation.PASS:
        o["validation"] = Validation.UNIT_WARNING
    return o


def _edge(obs, order, role, sign, src_filing, value, unit, *, concept="",
          observation_id_="", fact_id=None, version="", context_id=None,
          population_id=None) -> dict:
    return {"observation_id": obs["observation_id"], "input_order": order,
            "input_role": role, "operator_sign": sign, "coefficient": 1.0,
            "input_source_system": SOURCE_SYSTEM, "input_filing_id": src_filing,
            "input_source_fact_id": fact_id, "input_observation_id": observation_id_ or None,
            "input_context_id": context_id, "input_concept": concept,
            "input_period": obs.get("instant_date"), "input_value": str(value),
            "input_unit": unit, "input_version_status": version,
            "input_population_id": population_id}


# ------------------------------------------------------- set-based lineage

def population_id_for(observation_id_: str, purpose: str) -> str:
    return "pop-" + hashlib.sha256(
        f"{observation_id_}|{purpose}".encode()).hexdigest()[:32]


def member_key(filing_id: str, source_fact_id: str) -> str:
    """A population member names its FILING OCCURRENCE as well as its row.

    Byte-identical filings share source_fact_ids, so `D00042` alone resolves
    under either occurrence and a set drawn from the wrong submission would
    still look valid. The occurrence is part of the key (contract rule 3.2).
    """
    return f"{filing_id}:{source_fact_id}"


def member_digest(members) -> str:
    return hashlib.sha256("\n".join(sorted(members)).encode("utf-8")).hexdigest()


def _population(obs, purpose: str, *, filing_ids, inclusion: str, exclusion: str,
                members, candidate_count: int, aggregate_value=None,
                aggregate_unit=None, empty_reason: str = "", note: str = "",
                source_table: str = "source_facts") -> dict:
    """One `lineage_populations` row: the contributing set, stated exactly.

    An aggregate over a whole contract book cannot carry one edge per row and
    stay readable, so the schema allows exactly one alternative: a set defined
    by its inclusion rule, its exclusion rule, its size, and a digest over its
    members. The digest is what makes the claim checkable without re-running
    this adapter -- change, add or drop a member and it no longer matches.

    Both rules are mandatory. A set described only by what it includes cannot
    be checked for what it wrongly kept.
    """
    members = sorted(members)
    if not members and not empty_reason:
        raise ValueError(f"population {purpose} for {obs['observation_id']} is empty and "
                         f"states no empty_reason; 'no rows qualified' and 'no data was "
                         f"retrieved' are different facts and one of them is not a zero")
    # The docstring above says both rules are mandatory, so enforce it here
    # rather than leaving `audit_lineage` to notice afterwards. A promise made in
    # prose beside code that does not keep it is worse than no promise: it stops
    # the next reader checking (w4-coverage's `lineage is None` instance survived
    # for exactly that reason -- a correct docstring above permissive code).
    if not (inclusion or "").strip() or not (exclusion or "").strip():
        raise ValueError(f"population {purpose} for {obs['observation_id']} is missing an "
                         f"inclusion or exclusion rule; a set defined only by what it "
                         f"includes cannot be checked for what it wrongly kept")
    # `purpose` drives the independent persisted redraw, so it is data rather
    # than explanatory prose.  Persist it in a versioned machine-readable note
    # and retain any human detail alongside it.  Older databases are still
    # readable through `_purpose_of`'s rule fallback, but new builds never have
    # to infer this control field from sentence wording.
    structured_note = json.dumps({
        "detail": note or None,
        "purpose": purpose,
        "schema": "ioc_population_note_v1",
    }, sort_keys=True, separators=(",", ":"))
    return {
        "population_id": population_id_for(obs["observation_id"], purpose),
        "observation_id": obs["observation_id"],
        "source_system": SOURCE_SYSTEM, "source_table": source_table,
        "filing_ids": json.dumps(sorted(set(filing_ids))),
        "inclusion_rule": inclusion, "exclusion_rule": exclusion,
        "row_count": len(members), "candidate_count": candidate_count,
        "excluded_count": max(candidate_count - len(members), 0),
        "member_key": "filing_id:source_fact_id",
        "member_digest": member_digest(members),
        "members_sample": json.dumps(members[:25]),
        "aggregate_value": None if aggregate_value is None else f"{aggregate_value:.10g}"
        if isinstance(aggregate_value, float) else str(aggregate_value),
        "aggregate_unit": aggregate_unit,
        "empty_reason": empty_reason or None,
        "note": structured_note,
        "created_at": utcnow(),
    }


class _Lineage:
    """Collects edges and populations for one entity, keeping `input_order`
    unique per observation without every call site having to count."""

    def __init__(self, entity_key: str):
        self.entity_key = entity_key
        self.edges: list[dict] = []
        self.populations: list[dict] = []
        self._n: dict[str, int] = {}

    def _next(self, obs) -> int:
        oid = obs["observation_id"]
        self._n[oid] = self._n.get(oid, 0) + 1
        return self._n[oid]

    def edge(self, obs, role, sign, src_filing, value, unit, **kw) -> dict:
        e = _edge(obs, self._next(obs), role, sign, src_filing, value, unit, **kw)
        self.edges.append(e)
        return e

    def contributions(self, obs, purpose, rows, *, filing_id, inclusion, exclusion,
                      value_of, unit, aggregate_value=None, candidate_count=None,
                      empty_reason="", note="", concept="", edge_role="population",
                      version=""):
        """Persist the contributing source rows of one aggregate.

        Always a population, so the set is verifiable; ALSO one edge per row
        when the set is small enough for a reviewer to read. A pointer to
        another aggregate is never sufficient on its own (audit A08).
        """
        rows = list(rows)
        members = [member_key(filing_id, f"D{r['line']:05d}" if r.get("_kind") != "P"
                              else f"P{r['line']:05d}") for r in rows]
        pop = _population(obs, purpose, filing_ids=[filing_id], inclusion=inclusion,
                          exclusion=exclusion, members=members,
                          candidate_count=(candidate_count if candidate_count is not None
                                           else len(rows)),
                          aggregate_value=aggregate_value, aggregate_unit=unit,
                          empty_reason=empty_reason, note=note)
        self.populations.append(pop)
        self.edge(obs, edge_role, "+", filing_id,
                  pop["aggregate_value"] if pop["aggregate_value"] is not None
                  else pop["row_count"], unit, concept=concept or purpose,
                  population_id=pop["population_id"], version=version)
        if 0 < len(rows) <= PER_ROW_EDGE_LIMIT:
            for r in rows:
                fid = f"{'P' if r.get('_kind') == 'P' else 'D'}{r['line']:05d}"
                # each per-row edge names the population it belongs to, so the
                # two representations of one set can be checked against each
                # other rather than merely coexisting
                self.edge(obs, "contributing_row", "+", filing_id,
                          value_of(r), unit, concept=concept or purpose, fact_id=fid,
                          population_id=pop["population_id"],
                          context_id=(f"contract{r['_group']:04d}"
                                      if r.get("_group") is not None else None))
        return pop


def canonicalise(ctx, entity, filings: list[dict], expected: list[dict]) -> tuple[list, list]:
    entity_key = entity["entity_key"]
    template = _template_of(entity, entity.get("assets") or [])
    metrics = {m.id: m for m in BY_ADAPTER[ADAPTER] if template in m.templates}
    observations: list[dict] = []
    lin = _Lineage(entity_key)

    canonical = sorted((f for f in filings if f.get("is_canonical")),
                       key=lambda f: f["snapshot_date"])
    for f in canonical:
        # Ownership is asserted once more at the point of USE. A filing whose
        # header names another company can never reach an observation, whatever
        # sequence of runs put the row there (audit A02).
        gate = (f.get("_parsed") or {}).get("header", {}).get("entity_gate", {})
        if gate.get("header_cid") and gate["header_cid"] != normalise_cid(entity_key):
            raise IOCEntityMismatch(
                f"refusing to derive {entity_key} observations from {f['filing_id']}, whose "
                f"header names {gate['header_cid']}",
                expected=normalise_cid(entity_key), found=gate["header_cid"],
                found_name=gate.get("header_name", ""), accession=f["filing_id"])
        observations.extend(_snapshot_observations(ctx, entity, f, metrics, lin))

    observations.extend(_snapshot_diffs(ctx, entity, canonical, metrics, observations, lin))

    observations.extend(_account_for_remainder(entity, metrics, expected, observations,
                                               canonical))
    observations = _dedupe(ctx, observations)
    kept = {o["observation_id"] for o in observations}
    edges = [e for e in lin.edges if e["observation_id"] in kept]
    _PENDING_POPULATIONS[entity_key] = [p for p in lin.populations
                                        if p["observation_id"] in kept]
    events = _PENDING_EVENTS.pop(entity_key, [])
    if events and observations:
        observations[0]["_events"] = events
    return observations, edges


def write_pending_populations(ctx, entity_key: str) -> int:
    """Persist this entity's `lineage_populations` rows.

    Called by the runner AFTER the observations are committed and BEFORE (or
    with) the edges that reference them: `lineage_populations.observation_id` is
    a foreign key onto `observations`, and `lineage_edges.input_population_id` is
    a foreign key onto this table, so the three have exactly one legal order.
    Buffered rather than attached to an observation dict because an unrecognised
    key inside an observation goes straight into the column list of an INSERT.
    """
    rows = _PENDING_POPULATIONS.pop(entity_key, [])
    if not rows:
        return 0
    if not _has_table(ctx, "lineage_populations"):
        ctx.log("warn", f"{entity_key}: {len(rows)} IOC lineage populations were computed but "
                        f"the lineage_populations table does not exist, so the set-based "
                        f"lineage of every IOC aggregate is NOT persisted for this entity",
                adapter=ADAPTER, entity_cid=entity_key)
        return 0
    return ctx.staging.upsert("lineage_populations", rows, ["population_id"])


def pending_populations(entity_key: str) -> list[dict]:
    """Read-only view, for a harness that writes the three tables itself."""
    return list(_PENDING_POPULATIONS.get(entity_key, []))


# ------------------------------------------------------- lineage self-audit

#: How a stored population can be REDRAWN from `source_facts` alone, without
#: this adapter. Each entry is (purpose prefix, record kind, predicate over the
#: parsed fields the fact carries). Anything not listed here is checked
#: structurally only, and says so.
def _redraw_predicates():
    return {
        "contracts_with_transport_mdq":
            ("D", lambda d: d.get("transport_mdq") is not None,
             lambda d: float(d["transport_mdq"])),
        "contracts_excluded_blank_transport_mdq":
            ("D", lambda d: d.get("transport_mdq") is None, None),
        "contracts_with_storage_quantity":
            ("D", lambda d: d.get("storage_quantity") is not None
             and float(d["storage_quantity"]) > 0,
             lambda d: float(d["storage_quantity"])),
        "point_records":
            ("P", lambda d: True, None),
    }


def _redraw_spec(purpose: str):
    """The redraw rule for one population purpose, including the per-point-code
    censuses, whose code is carried in the purpose itself."""
    base = _redraw_predicates()
    if purpose in base:
        return base[purpose]
    if purpose.startswith("point_records::"):
        code = purpose.split("::", 1)[1]
        return ("P", lambda d, code=code: (d.get("point_code") or "").upper() == code, None)
    return None


def _same_number(actual, expected, *, relative=5e-10, absolute=1e-9) -> bool:
    """Compare persisted decimal text without making binary formatting evidence.

    IOC populations store aggregates to ten significant figures, while the
    observation retains the full computed float. The tolerance admits only that
    declared formatting boundary; it is far below the four-decimal presentation
    precision of the published concentration percentage.
    """
    try:
        got = float(actual)
        want = float(expected)
    except (TypeError, ValueError):
        return False
    return abs(got - want) <= max(absolute, abs(want) * relative)


def _population_differences(pop, *, members, candidate_count, aggregate_value,
                            aggregate_unit, filing_id) -> list[str]:
    """Exact set/occurrence checks shared by IOC top-five numerator components."""
    differences: list[str] = []
    members = sorted(members)
    try:
        filing_ids = json.loads(pop["filing_ids"] or "[]")
    except (TypeError, json.JSONDecodeError):
        filing_ids = None
    try:
        sample = json.loads(pop["members_sample"] or "[]")
    except (TypeError, json.JSONDecodeError):
        sample = None
    if filing_ids != [filing_id]:
        differences.append("filing_ids")
    if pop["source_system"] != SOURCE_SYSTEM or pop["source_table"] != "source_facts":
        differences.append("source_location")
    if pop["member_key"] != "filing_id:source_fact_id":
        differences.append("member_key")
    def integer_equals(actual, expected) -> bool:
        try:
            return int(actual) == expected
        except (TypeError, ValueError):
            return False

    if not integer_equals(pop["row_count"], len(members)):
        differences.append("row_count")
    if not integer_equals(pop["candidate_count"], candidate_count):
        differences.append("candidate_count")
    if not integer_equals(pop["excluded_count"], candidate_count - len(members)):
        differences.append("excluded_count")
    if pop["member_digest"] != member_digest(members):
        differences.append("member_digest")
    if sample != members[:25]:
        differences.append("members_sample")
    if not _same_number(pop["aggregate_value"], aggregate_value):
        differences.append("aggregate_value")
    if (pop["aggregate_unit"] or "") != (aggregate_unit or ""):
        differences.append("aggregate_unit")
    return differences


def _audit_top5_lineage(staging, observations, populations, facts_of) -> list[dict]:
    """Redraw every top-five output from stored D rows, including large sets.

    The generic population audit can compare per-row edges for readable sets and
    a 25-member sample for large ones. That is not sufficient for top-five
    concentration: a changed 41st contract can change the ranked shippers and
    the percentage without touching the sample. This check therefore redraws
    the denominator, each displayed shipper, the collective numerator and the
    final percentage from *all* source facts. It relies on persisted facts, not
    adapter state or a regenerated reference answer.
    """
    problems: list[dict] = []
    pops_by_id = {p["population_id"]: p for p in populations}

    def problem(code: str, obs, detail: str, *, population_id="") -> None:
        row = {"problem": code, "observation_id": obs["observation_id"],
               "metric_id": obs["metric_id"], "detail": detail}
        if population_id:
            row["population_id"] = population_id
        problems.append(row)

    for obs in observations:
        if (obs["availability"] != Availability.PRESENT
                or obs["metric_id"] != "ioc_top5_shipper_concentration"):
            continue
        edges = staging.query(
            "SELECT * FROM lineage_edges WHERE observation_id=? ORDER BY input_order",
            (obs["observation_id"],))
        collective_links = [e for e in edges
                            if e["input_role"] == "population"
                            and e["input_concept"] == "ioc_top5_shipper_concentration"]
        if len(collective_links) != 1:
            problem("top5_collective_population_link_count", obs,
                    f"expected one collective top-five population edge, found "
                    f"{len(collective_links)}")
            continue
        collective_edge = collective_links[0]
        collective = pops_by_id.get(collective_edge["input_population_id"])
        if collective is None:
            problem("top5_collective_population_missing", obs,
                    f"edge names absent population "
                    f"{collective_edge['input_population_id']!r}",
                    population_id=collective_edge["input_population_id"] or "")
            continue

        try:
            filing_ids = json.loads(collective["filing_ids"] or "[]")
        except (TypeError, json.JSONDecodeError):
            filing_ids = []
        if len(filing_ids) != 1:
            problem("top5_collective_filing_count", obs,
                    f"the collective population names {filing_ids!r}; exactly one filing "
                    "occurrence is required",
                    population_id=collective["population_id"])
            continue
        filing_id = str(filing_ids[0])
        rule = (collective["inclusion_rule"] or "").lower()
        if "transportation mdq" in rule:
            weight_field = "transport_mdq"
            keep = lambda value: value is not None
        elif "contracted storage quantity" in rule:
            weight_field = "storage_quantity"
            keep = lambda value: value is not None and value > 0
        else:
            problem("top5_weight_rule_unrecognised", obs,
                    "collective population does not identify transportation MDQ or "
                    "contracted storage quantity",
                    population_id=collective["population_id"])
            continue

        all_d_facts = facts_of(collective["source_system"], filing_id, "D")
        candidates: list[tuple[str, dict, float]] = []
        invalid_weights: list[str] = []
        for fact_id, dims in all_d_facts.items():
            raw = dims.get(weight_field)
            try:
                value = None if raw is None else float(raw)
            except (TypeError, ValueError):
                invalid_weights.append(fact_id)
                continue
            if keep(value):
                candidates.append((fact_id, dims, value))
        if invalid_weights:
            problem("top5_source_weight_unparseable", obs,
                    f"{weight_field} is not numeric on {invalid_weights[:10]!r}")
        if not candidates:
            problem("top5_denominator_empty", obs,
                    "a present concentration output has no source row in its denominator")
            continue

        by_shipper: dict[str, dict] = {}
        for fact_id, dims, value in candidates:
            key = normalise_shipper(dims.get("shipper_name") or "")
            state = by_shipper.setdefault(
                key, {"name": dims.get("shipper_name") or "", "weight": 0.0,
                      "members": []})
            state["weight"] += value
            state["members"].append(member_key(filing_id, fact_id))
        ranked = sorted(by_shipper.items(), key=lambda item: -item[1]["weight"])
        expected_groups = ranked[:5]
        expected_members = sorted(
            member for _key, state in expected_groups for member in state["members"])
        denominator_value = sum(value for _fact, _dims, value in candidates)
        numerator_value = sum(state["weight"] for _key, state in expected_groups)
        weight_unit = collective["aggregate_unit"]

        collective_diff = _population_differences(
            collective, members=expected_members, candidate_count=len(candidates),
            aggregate_value=numerator_value, aggregate_unit=weight_unit,
            filing_id=filing_id)
        if collective_diff:
            problem("top5_collective_population_disagrees", obs,
                    "full redraw differs in " + ", ".join(collective_diff),
                    population_id=collective["population_id"])
        collective_edge_diff: list[str] = []
        if not _same_number(collective_edge["input_value"], numerator_value):
            collective_edge_diff.append("value")
        if collective_edge["input_filing_id"] != filing_id:
            collective_edge_diff.append("filing_occurrence")
        if (collective_edge["input_unit"] or "") != (weight_unit or ""):
            collective_edge_diff.append("unit")
        if collective["observation_id"] != obs["observation_id"]:
            collective_edge_diff.append("population_output")
        if collective_edge_diff:
            problem("top5_collective_edge_disagrees", obs,
                    "collective edge differs in " + ", ".join(collective_edge_diff),
                    population_id=collective["population_id"])

        denominator_edges = [e for e in edges if e["input_role"] == "denominator"]
        reserved_population_ids = {collective["population_id"]}
        if len(denominator_edges) != 1:
            problem("top5_denominator_edge_count", obs,
                    f"expected one denominator edge, found {len(denominator_edges)}")
        else:
            denominator_edge = denominator_edges[0]
            inputs = staging.query(
                "SELECT observation_id,value_num,unit FROM observations "
                "WHERE observation_id=?", (denominator_edge["input_observation_id"],))
            denominator_diff: list[str] = []
            if len(inputs) != 1:
                denominator_diff.append("input_observation")
            elif (not _same_number(inputs[0]["value_num"], denominator_value)
                  or (inputs[0]["unit"] or "") != (weight_unit or "")):
                denominator_diff.append("input_observation_value_or_unit")
            if not _same_number(denominator_edge["input_value"], denominator_value):
                denominator_diff.append("edge_value")
            if (denominator_edge["input_unit"] or "") != (weight_unit or ""):
                denominator_diff.append("edge_unit")
            if denominator_edge["input_filing_id"] != filing_id:
                denominator_diff.append("filing_occurrence")
            if denominator_diff:
                problem("top5_denominator_disagrees", obs,
                        "full redraw differs in " + ", ".join(denominator_diff))

            denominator_population_links = [
                e for e in edges if e["input_role"] == "population"
                and e["input_concept"] == denominator_edge["input_concept"]]
            if len(denominator_population_links) != 1:
                problem("top5_denominator_population_link_count", obs,
                        f"expected one denominator population, found "
                        f"{len(denominator_population_links)}")
            else:
                denominator_pop = pops_by_id.get(
                    denominator_population_links[0]["input_population_id"])
                if denominator_pop is None:
                    problem("top5_denominator_population_missing", obs,
                            "denominator population edge does not resolve")
                else:
                    reserved_population_ids.add(denominator_pop["population_id"])
                    denominator_members = [
                        member_key(filing_id, fact_id)
                        for fact_id, _dims, _value in candidates]
                    diff = _population_differences(
                        denominator_pop, members=denominator_members,
                        candidate_count=len(all_d_facts),
                        aggregate_value=denominator_value, aggregate_unit=weight_unit,
                        filing_id=filing_id)
                    if denominator_pop["observation_id"] != obs["observation_id"]:
                        diff.append("population_output")
                    if diff:
                        problem("top5_denominator_population_disagrees", obs,
                                "full redraw differs in " + ", ".join(diff),
                                population_id=denominator_pop["population_id"])

        group_edges = [e for e in edges if e["input_role"] == "group_member"]
        if len(group_edges) != len(expected_groups):
            problem("top5_group_member_count_disagrees", obs,
                    f"stored {len(group_edges)} group summaries; redraw found "
                    f"{len(expected_groups)}")
        missing_group_pops: list[str] = []
        group_differences: list[str] = []
        seen_group_populations: set[str] = set()
        for position, (edge, (shipper_key, state)) in enumerate(
                zip(group_edges, expected_groups), 1):
            if ((edge["input_concept"] or "") != state["name"]
                    or not _same_number(edge["input_value"], state["weight"])
                    or edge["input_filing_id"] != filing_id
                    or (edge["input_unit"] or "") != (weight_unit or "")):
                group_differences.append(f"summary[{position}]")
            pid = edge["input_population_id"] or ""
            group_pop = pops_by_id.get(pid)
            if not pid or group_pop is None:
                missing_group_pops.append(f"summary[{position}]={pid or '<blank>'}")
                continue
            if pid in seen_group_populations or pid in reserved_population_ids:
                group_differences.append(f"summary[{position}].population_not_specific")
            seen_group_populations.add(pid)
            diff = _population_differences(
                group_pop, members=state["members"], candidate_count=len(candidates),
                aggregate_value=state["weight"], aggregate_unit=weight_unit,
                filing_id=filing_id)
            if group_pop["observation_id"] != obs["observation_id"]:
                diff.append("population_output")
            if diff:
                group_differences.append(
                    f"summary[{position}].population({','.join(diff)})")
        if missing_group_pops:
            problem("top5_group_population_missing", obs,
                    "; ".join(missing_group_pops))
        if group_differences:
            problem("top5_group_summary_disagrees", obs,
                    "; ".join(group_differences))

        if denominator_value:
            expected_output = 100.0 * numerator_value / denominator_value
            output_diff: list[str] = []
            if not _same_number(obs["value_num"], expected_output):
                output_diff.append("value_num")
            if obs["value_text"] != f"{expected_output:.4f}":
                output_diff.append("value_text")
            try:
                candidate_count_matches = int(obs["candidate_count"]) == len(by_shipper)
            except (TypeError, ValueError):
                candidate_count_matches = False
            if not candidate_count_matches:
                output_diff.append("candidate_count")
            if obs["filing_id"] != filing_id:
                output_diff.append("filing_occurrence")
            if (obs["unit"] or "") != "percent":
                output_diff.append("unit")
            if output_diff:
                problem("top5_output_disagrees", obs,
                        f"full redraw differs in {', '.join(output_diff)}; stored "
                        f"{obs['value_num']!r} {obs['unit']!r}, expected "
                        f"{expected_output:.10g} percent")
    return problems


def audit_lineage(staging, entity_key: str = "") -> list[dict]:
    """Check that every IOC aggregate really is traversable to its source rows.

    This is the invariant this adapter promises, expressed so that it can be
    checked against STORED DATA ONLY -- no re-run, no adapter state. Five things
    are asserted:

      1. every `present` IOC observation carries at least one lineage edge, and
         a derived one carries more than a pointer at another aggregate;
      2. every population edge resolves to a `lineage_populations` row, and the
         row's counts are internally consistent;
      3. where per-row edges were emitted, they agree with the population --
         same members, same digest, and they sum to the recorded aggregate. So
         DELETING ONE CONTRIBUTING EDGE FAILS, which is the point;
      4. where the set can be redrawn from `source_facts` (the filed totals and
         the point censuses), it IS redrawn, and the recomputed member digest
         and aggregate must match what was stored;
      5. every top-five result is fully redrawn regardless of population size:
         denominator, each displayed shipper, collective numerator, complete
         member digests and final percentage must all agree.

    Returns a list of problems. Empty means the lineage stands up.
    """
    # Bind the audit to the adapter's declared metric population rather than a
    # name prefix or source system.  Both are incidental: another adapter can
    # use eLibrary while a future IOC metric need not begin with ``ioc_``.
    metric_ids = tuple(sorted(metric.id for metric in BY_ADAPTER[ADAPTER]))
    metric_marks = ",".join("?" for _ in metric_ids)
    where = " AND o.entity_key=?" if entity_key else ""
    args = metric_ids + ((entity_key,) if entity_key else ())
    problems: list[dict] = []

    obs_rows = staging.query(
        "SELECT o.observation_id, o.metric_id, o.entity_key, o.method, o.availability, "
        "       o.filing_id, o.value_text, o.value_num, o.unit, o.scope, "
        "       o.candidate_count, o.missing_reason "
        f"FROM observations o WHERE o.metric_id IN ({metric_marks})" + where, args)
    obs_by_id = {row["observation_id"]: row for row in obs_rows}
    point_metric = next(metric for metric in BY_ADAPTER[ADAPTER]
                        if metric.id == "ioc_points")
    have_pop_table = bool(staging.query(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lineage_populations'"))

    for o in obs_rows:
        if o["availability"] != Availability.PRESENT:
            continue
        edges = staging.query(
            "SELECT * FROM lineage_edges WHERE observation_id=? ORDER BY input_order",
            (o["observation_id"],))
        if not edges:
            problems.append({"problem": "aggregate_without_lineage",
                             "observation_id": o["observation_id"],
                             "metric_id": o["metric_id"],
                             "detail": "a present IOC value with no lineage edge at all"})
            continue
        roles = {e["input_role"] for e in edges}
        if roles <= {"denominator"}:
            problems.append({
                "problem": "anchored_only_to_another_aggregate",
                "observation_id": o["observation_id"], "metric_id": o["metric_id"],
                "detail": ("the only lineage is a denominator pointer at another "
                           "aggregate; the contributing source rows are not persisted")})
        # A WEIGHTED figure must be denominated in the unit of the weight it is
        # actually computed over. ioc_expiry_profile is legitimately reported on
        # either weight -- a transportation MDQ, which is a RATE, or a contracted
        # storage quantity, which is a STOCK -- because the manual defines both
        # and a filer may have either or both. A single per-metric unit rule
        # cannot express that; this per-row check can, and it is strictly
        # stronger than admitting two families for the metric as a whole.
        for e in edges:
            if e["input_role"] != "denominator":
                continue
            if (o["unit"] or "") in ("percent", ""):
                continue
            if (o["unit"] or "") != (e["input_unit"] or ""):
                problems.append({
                    "problem": "weighted_value_not_in_the_unit_of_its_weight",
                    "observation_id": o["observation_id"], "metric_id": o["metric_id"],
                    "detail": f"the value is in {o['unit']!r} but is weighted by "
                              f"{e['input_concept']} in {e['input_unit']!r} "
                              f"({unit_family_of(o['unit'])} vs "
                              f"{unit_family_of(e['input_unit'])})"})

    if not have_pop_table:
        problems.append({"problem": "no_lineage_populations_table",
                         "detail": ("set-based lineage cannot be stored, so every aggregate "
                                    "over more than "
                                    f"{PER_ROW_EDGE_LIMIT} rows is unverifiable")})
        return problems

    # This validator owns IOC populations only.  Other adapters deliberately
    # use different member-key vocabularies (for example the 549D and capacity
    # adapters use source-row keys that are not ``filing_id:source_fact_id``).
    # Sweeping every lineage population in the database made valid foreign
    # populations look like malformed IOC sets and, worse, obscured genuine IOC
    # redraw defects in the resulting noise.
    pop_where = (
        " WHERE p.observation_id IN (SELECT o2.observation_id FROM observations o2 "
        f"WHERE o2.metric_id IN ({metric_marks})"
    )
    if entity_key:
        pop_where += " AND entity_key=?"
    pop_where += ")"
    pops = staging.query(f"SELECT p.* FROM lineage_populations p{pop_where}", args)

    # One pass over a filing's facts serves every population drawn from it. A
    # per-member query would be ~200,000 round trips on a full database, which
    # is the difference between a check that runs in validate.py and one that
    # gets skipped.
    facts_cache: dict[tuple[str, str, str], dict[str, dict]] = {}

    def facts_of(source_system: str, filing_id: str, kind: str) -> dict[str, dict]:
        key = (source_system, filing_id, kind)
        if key not in facts_cache:
            facts_cache[key] = {
                r["source_fact_id"]: json.loads(r["typed_dims_json"] or "{}")
                for r in staging.query(
                    "SELECT source_fact_id, typed_dims_json FROM source_facts "
                    "WHERE source_system=? AND filing_id=? AND source_fact_id LIKE ? "
                    "ORDER BY source_fact_id",
                    (source_system, filing_id, f"{kind}%"))}
        return facts_cache[key]

    for p in pops:
        pid = p["population_id"]
        if p["row_count"] + p["excluded_count"] != p["candidate_count"]:
            problems.append({"problem": "population_counts_inconsistent",
                             "population_id": pid,
                             "detail": f"row_count {p['row_count']} + excluded "
                                       f"{p['excluded_count']} != candidate "
                                       f"{p['candidate_count']}"})
        if p["row_count"] == 0 and not (p["empty_reason"] or "").strip():
            problems.append({"problem": "empty_population_without_reason",
                             "population_id": pid,
                             "detail": "a zero result must say whether nothing qualified or "
                                       "nothing was retrieved"})
        if not (p["inclusion_rule"] or "").strip() or not (p["exclusion_rule"] or "").strip():
            problems.append({"problem": "population_rule_missing", "population_id": pid,
                             "detail": "both an inclusion and an exclusion rule are required"})

        sample = json.loads(p["members_sample"] or "[]")
        filing_ids = json.loads(p["filing_ids"] or "[]")
        if p["row_count"] <= len(sample) and member_digest(sample) != p["member_digest"]:
            problems.append({"problem": "member_digest_mismatch", "population_id": pid,
                             "detail": "the stored digest does not match the stored members"})
        for mk in sample:
            fid, _, fact = mk.partition(":")
            if fid not in filing_ids:
                problems.append({"problem": "member_outside_declared_filings",
                                 "population_id": pid, "detail": mk})
                continue
            # the FILING OCCURRENCE is part of the key, so a member that exists
            # under a byte-identical sibling filing does not satisfy this
            if fact not in facts_of(p["source_system"], fid, fact[:1]):
                problems.append({"problem": "member_does_not_resolve",
                                 "population_id": pid, "detail": mk})

        # -- per-row edges must agree with the set, so deleting one is detected
        row_edges = staging.query(
            "SELECT * FROM lineage_edges WHERE observation_id=? AND input_role="
            "'contributing_row' AND input_population_id=?",
            (p["observation_id"], pid))
        if 0 < p["row_count"] <= PER_ROW_EDGE_LIMIT:
            keys = {member_key(e["input_filing_id"], e["input_source_fact_id"])
                    for e in row_edges}
            if len(row_edges) != p["row_count"]:
                problems.append({
                    "problem": "contributing_edges_do_not_match_population",
                    "population_id": pid,
                    "detail": f"{len(row_edges)} contributing_row edge(s) for a population of "
                              f"{p['row_count']}; a removed contributor is a removed "
                              f"contribution"})
            elif member_digest(keys) != p["member_digest"]:
                problems.append({"problem": "contributing_edges_are_not_the_population",
                                 "population_id": pid,
                                 "detail": "the per-row edges name a different set of rows "
                                           "than the population digest"})
            if p["aggregate_value"] not in (None, "") and row_edges:
                try:
                    total = sum(float(e["input_value"]) for e in row_edges)
                except (TypeError, ValueError):
                    total = None
                if total is not None and abs(total - float(p["aggregate_value"])) > 1e-6:
                    problems.append({
                        "problem": "contributing_edges_do_not_sum_to_the_aggregate",
                        "population_id": pid,
                        "detail": f"edges sum to {total:.10g}, aggregate is "
                                  f"{p['aggregate_value']}"})

        # -- redraw the set from source_facts, independently of this adapter
        purpose = _purpose_of(p)
        if purpose == "point_records::":
            problems.append({
                "problem": "blank_point_code_promoted_as_present",
                "population_id": pid,
                "detail": ("P records whose item yh point code is blank must remain "
                           "source rows and may contribute to the file-level census, but "
                           "cannot become a present categorical point-code observation"),
            })
        # Do not make the blank-code control depend on the prose used for an
        # inclusion rule.  A file-level point census may retain blank P rows;
        # every other present ioc_points population must have a declared
        # purpose and may not promote a blank typed code.  Small populations
        # carry occurrence-specific row edges, so check those exact source
        # facts as an independent semantic boundary.
        observation = obs_by_id.get(p["observation_id"])
        blank_contributing_facts = []
        if observation and observation["metric_id"] == "ioc_points" \
                and observation["availability"] == Availability.PRESENT:
            for edge in row_edges:
                facts = facts_of(
                    edge["input_source_system"], edge["input_filing_id"], "P")
                dims = facts.get(edge["input_source_fact_id"])
                if dims is not None and not str(dims.get("point_code") or "").strip():
                    blank_contributing_facts.append(
                        member_key(edge["input_filing_id"], edge["input_source_fact_id"]))
            if purpose not in {"point_records", "point_records::"} \
                    and blank_contributing_facts:
                problems.append({
                    "problem": "blank_point_code_promoted_as_present",
                    "population_id": pid,
                    "detail": ("present point-code population names blank typed item-yh "
                               "source facts: " + ", ".join(blank_contributing_facts[:10])),
                })
            if not purpose:
                problems.append({
                    "problem": "ioc_population_purpose_missing",
                    "population_id": pid,
                    "detail": ("a present IOC point population must carry a structured "
                               "purpose; prose is not a control field"),
                })

        if purpose == "point_records" and observation and len(filing_ids) == 1:
            point_facts = facts_of(p["source_system"], filing_ids[0], "P")
            census = _census(dims.get("point_code") for dims in point_facts.values())
            differences = []
            try:
                candidate_count_matches = \
                    int(observation["candidate_count"]) == len(point_facts)
            except (TypeError, ValueError):
                candidate_count_matches = False
            if not candidate_count_matches:
                differences.append("candidate_count")
            if observation["filing_id"] != filing_ids[0]:
                differences.append("filing_occurrence")
            if observation["scope"] != point_metric.scope:
                differences.append("scope")
            if census:
                if observation["availability"] != Availability.PRESENT:
                    differences.append("availability")
                if observation["value_text"] != census:
                    differences.append("value_text")
                if observation["unit"] != "codes":
                    differences.append("unit")
            else:
                if observation["availability"] != Availability.SOURCE_BLANK:
                    differences.append("availability")
                if observation["value_text"] not in (None, ""):
                    differences.append("value_text")
                if observation["unit"] not in (None, ""):
                    differences.append("unit")
                if not str(observation["missing_reason"] or "").strip():
                    differences.append("missing_reason")
            if differences:
                problems.append({
                    "problem": "point_file_census_output_disagrees",
                    "population_id": pid,
                    "observation_id": observation["observation_id"],
                    "detail": ("file-level point census redraw differs in "
                               + ", ".join(differences)
                               + f"; expected {census or 'source_blank'} over "
                               f"{len(point_facts)} P fact(s)"),
                })

        spec = _redraw_spec(purpose)
        if spec and len(filing_ids) == 1:
            kind, keep, value_of = spec
            members, total = [], 0.0
            for fact_id, d in facts_of(p["source_system"], filing_ids[0], kind).items():
                if not keep(d):
                    continue
                members.append(member_key(filing_ids[0], fact_id))
                if value_of:
                    total += value_of(d)
            if member_digest(members) != p["member_digest"]:
                problems.append({
                    "problem": "population_cannot_be_redrawn_from_source_facts",
                    "population_id": pid, "purpose": purpose,
                    "detail": f"redrawing the inclusion rule over source_facts yields "
                              f"{len(members)} member(s); the population records "
                              f"{p['row_count']} and a different digest"})
            elif value_of and p["aggregate_value"] not in (None, ""):
                if abs(total - float(p["aggregate_value"])) > 1e-6:
                    problems.append({
                        "problem": "redrawn_population_disagrees_on_the_total",
                        "population_id": pid, "purpose": purpose,
                        "detail": f"redrawn total {total:.10g} vs stored "
                                  f"{p['aggregate_value']}"})
    problems.extend(_audit_top5_lineage(staging, obs_rows, pops, facts_of))
    return problems


def _purpose_of(pop_row) -> str:
    """The purpose a population_id was minted for, recovered from its rules.

    population_id is a digest, so the purpose is carried in `note`/rules rather
    than parsed back out of the id. The concept on the population's own edge is
    the reliable handle.
    """
    note = pop_row["note"] or ""
    if note:
        try:
            structured = json.loads(note)
        except (TypeError, json.JSONDecodeError):
            structured = None
        if isinstance(structured, dict) \
                and structured.get("schema") == "ioc_population_note_v1" \
                and isinstance(structured.get("purpose"), str) \
                and structured["purpose"].strip():
            return structured["purpose"].strip()

    incl = (pop_row["inclusion_rule"] or "")
    if "item o (transportation MDQ" in incl and "parses as a number" in incl:
        return "contracts_with_transport_mdq"
    if "item o is blank or unparseable" in incl:
        return "contracts_excluded_blank_transport_mdq"
    if "item p" in incl and "above zero" in incl:
        return "contracts_with_storage_quantity"
    if incl.startswith("every P (point) record"):
        return "point_records"
    # The production writer surrounds the exact filed item-yh token with this
    # fixed prefix/delimiter.  Parse the whole field: IOC codes are not uniformly
    # two characters (the captured universe includes ``130``), and a blank token
    # is itself a detectable semantic defect rather than a prefix match.
    point_prefix = "item yh (point code) is "
    if point_prefix in incl:
        remainder = incl.split(point_prefix, 1)[1]
        code, delimiter, _meaning = remainder.partition(" -- ")
        if delimiter:
            return f"point_records::{code.strip().upper()}"
    return ""


def _dedupe(ctx, observations: list[dict]) -> list[dict]:
    """The observation grain is a uniqueness constraint in the schema. Two rows at
    one grain would silently overwrite each other, so a collision is refused and
    logged rather than committed."""
    seen: dict[str, dict] = {}
    for o in observations:
        prior = seen.get(o["observation_id"])
        if prior is None:
            seen[o["observation_id"]] = o
        elif prior.get("value_text") != o.get("value_text"):
            ctx.log("error", f"observation grain collision on {o['metric_id']} "
                             f"scope={o['scope']!r} instant={o['instant_date']}: "
                             f"{prior.get('value_text')!r} vs {o.get('value_text')!r}; "
                             "the first is kept and the second dropped",
                    adapter=ADAPTER, entity_cid=o["entity_key"])
    return list(seen.values())


def _snapshot_observations(ctx, entity, f, metrics, lin: "_Lineage") -> list[dict]:
    parsed = f["_parsed"]
    header = parsed["header"]
    contracts = parsed["contracts"]
    entity_key = entity["entity_key"]
    snap = header["snapshot_date"]
    year, period = f["reporting_year"], f["reporting_period"]
    acc = f["filing_id"]
    # An ABSENT version status is not an original filing. `original` is a
    # positive claim -- that nothing precedes this submission -- and defaulting
    # to it would assert that claim for a filing whose revision history was
    # never established. `unresolved` is in the schema's own vocabulary for
    # exactly this. (Same class as the UOM default: see _uom_state.)
    vstat = f.get("version_status") or VersionStatus.UNRESOLVED
    uom_t = header["uom_transport"]
    uom_s = header["uom_storage"]
    # header items f and g carry the unit vocabulary (B=MMBtu, T=Dth, F=Mcf). A
    # blank item is a unit that was NOT stated -- storage-only filers such as
    # Young Gas Storage leave item f empty -- and no unit is assumed for it.
    unit_transport = f"{uom_t}/day" if uom_t else None
    unit_storage = uom_s or None

    common = dict(instant=snap, year=year, period=period, filing_id=acc, accession=acc,
                  document_id=f.get("_document_id"), version_status=vstat)

    obs: list[dict] = []

    # UNIT ADMISSIBILITY. A contracted transport MDQ is a RATE (energy_rate) and
    # a contracted storage quantity is a STOCK (energy). The registry's unit
    # families are what stop the two being added, and an unrecognised spelling is
    # never treated as a wildcard.
    unit_flags = []
    for item, label, u, state, code in (
            ("f", "transport", unit_transport, header["uom_transport_state"],
             header["uom_transport_code"]),
            ("g", "storage", unit_storage, header["uom_storage_state"],
             header["uom_storage_code"])):
        if state == "unrecognised":
            unit_flags.append(
                f"header item {item} ({label} unit of measure) carries {code!r}, which is not "
                f"in the Form 549B manual's vocabulary (B = MMBtu, T = Dth, F = Mcf). The "
                f"filer DID state a unit and this adapter cannot read it -- that is a "
                f"different fact from stating none -- so no unit is attached, nothing is "
                f"assumed, and the code is retained verbatim for review")
        if u and not known_unit(u):
            unit_flags.append(f"{label} (header item {item}) unit {u!r} is not in the "
                              f"registry's unit vocabulary, so no family is claimed for it "
                              f"and it is comparable to nothing")
    if unit_transport and unit_storage and units_compatible(unit_transport, unit_storage):
        unit_flags.append(f"ANOMALY: header items f and g resolve to the same unit family "
                          f"({unit_family_of(unit_transport)}); the transport MDQ and the "
                          f"contracted storage quantity are still never added")

    # ---- source provenance shared by every figure in this snapshot
    quarter_start = (int(snap[5:7]) in (1, 4, 7, 10) and int(snap[8:10]) == 1)
    header_note = (
        f"as-of date is header item e (first day of the calendar quarter) = {snap}; "
        f"the report date in header item c is {header['report_date']} and is NOT used as "
        f"the effective date; eLibrary filedDate {f['filed_date']}; header indicator "
        f"{header['original_revised'] or '?'}; units read from header item f/g "
        f"(f={header['uom_transport_code'] or 'blank'} -> {uom_t or 'not stated'}, "
        f"g={header['uom_storage_code'] or 'blank'} -> {uom_s or 'not stated'}), "
        f"never assumed to be MMBtu")
    if unit_flags:
        header_note += "; " + "; ".join(unit_flags)
    if parsed["provenance"].get("bom") or parsed["provenance"].get("decode_fallback"):
        header_note += (f"; the attachment was decoded as {parsed['provenance']['encoding']}"
                        + (f" (byte-order mark: {parsed['provenance']['bom']})"
                           if parsed["provenance"].get("bom") else "")
                        + "; the original bytes are what is hashed and cached")
    if parsed["provenance"].get("preamble_rows_skipped"):
        header_note += "; " + parsed["provenance"]["preamble_note"]
    if parsed["provenance"]["header_repair"].get("applied"):
        header_note += (f"; HEADER REPAIRED: "
                        f"{parsed['provenance']['header_repair']['transformation']} -- "
                        f"{'; '.join(_repair_checks(parsed))}. "
                        f"The original header row is retained verbatim as the H source fact's "
                        f"value_as_filed and no D, A, P or F row was altered")
    if parsed["provenance"].get("unclassified_note"):
        header_note += "; " + parsed["provenance"]["unclassified_note"]
    if not quarter_start:
        header_note += (
            f"; ANOMALY: header item e is {snap}, which is NOT the first day of a calendar "
            f"quarter as 18 CFR 284.13(c) and the Form 549B manual require. The date is "
            f"kept exactly as filed and never repaired, and this file is treated as its own "
            f"snapshot rather than as a revision of the quarter-start index")

    # ---- transportation MDQ ------------------------------------------------
    with_mdq = [c for c in contracts if c["transport_mdq"] is not None]
    blank_mdq = [c for c in contracts if c["transport_mdq"] is None]
    total_mdq = sum(c["transport_mdq"] for c in with_mdq)
    m = metrics.get("ioc_firm_transport_mdq")
    mdq_obs = None
    if m is not None:
        if not contracts:
            o = _obs(entity_key, m, scope=m.scope, unit=None, value_text=None,
                     value_num=None, availability=Availability.SOURCE_BLANK,
                     missing_reason="the file parsed but contains no D (detail) records",
                     qa_flags=header_note, **common)
            obs.append(o)
            lin.contributions(
                o, "contracts_with_transport_mdq", [], filing_id=acc,
                inclusion="D records of this filing occurrence carrying a parseable item o",
                exclusion="no row was excluded; there was nothing to exclude",
                value_of=lambda c: None, unit=None, candidate_count=0,
                empty_reason=("the filing WAS retrieved and parsed and contains no D "
                              "(detail) record at all, so there is no contract population "
                              "to sum. This is an absent statement by the filer, not a "
                              "total of zero"),
                concept="ioc_firm_transport_mdq")
        elif not with_mdq:
            o = _obs(entity_key, m, scope=m.scope, unit=None, value_text=None,
                     value_num=None, availability=Availability.NOT_APPLICABLE,
                     missing_reason=(f"none of the {len(contracts)} contracts in this "
                                     "snapshot reports a transportation MDQ; a blank "
                                     "MDQ is not zero"),
                     qa_flags=header_note, **common)
            obs.append(o)
            lin.contributions(
                o, "contracts_with_transport_mdq", [], filing_id=acc,
                inclusion="D records of this filing occurrence carrying a parseable item o",
                exclusion=(f"all {len(contracts)} D records are excluded because item o is "
                           f"blank or unparseable on every one of them"),
                value_of=lambda c: None, unit=None, candidate_count=len(contracts),
                empty_reason=(f"the filing WAS retrieved and parsed and its {len(contracts)} "
                              f"D records were examined; none states a transportation MDQ. "
                              f"No qualifying row is not a total of zero"),
                concept="ioc_firm_transport_mdq")
        else:
            flags = [header_note,
                     f"denominator: {len(with_mdq)} of {len(contracts)} D records carry a "
                     f"parseable transportation MDQ (item o)",
                     "contracted firm transportation as filed -- not physical capacity, "
                     "not firm revenue, and never derived by summing P records"]
            validation = Validation.PASS
            if not uom_t:
                if header["uom_transport_state"] == "unrecognised":
                    flags.append(f"header item f (transportation unit of measure) carries "
                                 f"{header['uom_transport_code']!r}, which is not one of the "
                                 f"manual's B/T/F codes: the filer DID state a unit and it "
                                 f"cannot be read, which is not the same as stating none. No "
                                 f"unit is attached and none is assumed")
                else:
                    flags.append("header item f (transportation unit of measure) is BLANK: "
                                 "the filer states no transportation unit, so none is assumed "
                                 "and the figure carries no unit")
                validation = Validation.UNIT_WARNING
            if blank_mdq:
                flags.append(f"{len(blank_mdq)} contract(s) have a BLANK transportation MDQ; "
                             "they are excluded from this total and are NOT counted as zero")
                validation = Validation.SOURCE_ANOMALY_REVIEW
            mdq_obs = _obs(entity_key, m, scope=m.scope, unit=unit_transport,
                           value_text=f"{total_mdq:.10g}", value_num=total_mdq,
                           availability=Availability.PRESENT, validation=validation,
                           qa_flags="; ".join(flags),
                           candidate_count=len(with_mdq),
                           notes=f"rate schedules: "
                                 f"{_census(c['rate_schedule'] for c in contracts)}",
                           **common)
            obs.append(mdq_obs)
            # A filed TOTAL over a contract book is still an aggregate, and in
            # the delivered run 313 of them carried no lineage at all (audit
            # A08). Every contributing D record is now persisted, and the rows
            # left OUT are persisted as their own set, so the exclusion can be
            # checked rather than taken on trust.
            lin.contributions(
                mdq_obs, "contracts_with_transport_mdq", with_mdq, filing_id=acc,
                inclusion=("D (detail) records of this filing occurrence whose item o "
                           "(transportation MDQ, column 11) parses as a number"),
                exclusion=(f"{len(blank_mdq)} D record(s) whose item o is blank or "
                           f"unparseable are EXCLUDED and are not read as zero; A, P, F, H "
                           f"and unclassified rows never contribute; no row is excluded on "
                           f"the value it carries"),
                value_of=lambda c: c["transport_mdq"], unit=unit_transport,
                aggregate_value=total_mdq, candidate_count=len(contracts),
                concept="ioc_firm_transport_mdq")
            if blank_mdq:
                lin.contributions(
                    mdq_obs, "contracts_excluded_blank_transport_mdq", blank_mdq,
                    filing_id=acc,
                    inclusion=("D records of this filing occurrence whose item o is blank or "
                               "unparseable -- the EXCLUDED set, enumerated so that the "
                               "exclusion is independently checkable"),
                    exclusion="D records carrying a parseable item o are not in this set",
                    value_of=lambda c: c["transport_mdq_text"], unit=unit_transport,
                    aggregate_value=None, candidate_count=len(contracts),
                    concept="excluded_blank_item_o",
                    note=("a blank MDQ is an absent statement, not a zero; these contracts "
                          "are counted in candidate_count and contribute nothing"))

    # ---- contracted storage quantity (a STOCK) -----------------------------
    with_storage = [c for c in contracts
                    if c["storage_quantity"] is not None and c["storage_quantity"] > 0]
    total_storage = sum(c["storage_quantity"] for c in with_storage)
    m = metrics.get("ioc_contracted_storage_quantity")
    storage_obs = None
    if m is not None:
        if not with_storage:
            o = _obs(entity_key, m, scope=m.scope, unit=None, value_text=None,
                     value_num=None, availability=Availability.NOT_APPLICABLE,
                     missing_reason=(f"no contract in this snapshot reports a "
                                     f"contracted storage quantity (item p); "
                                     f"{len(contracts)} D records examined"),
                     qa_flags=header_note, **common)
            obs.append(o)
            lin.contributions(
                o, "contracts_with_storage_quantity", [], filing_id=acc,
                inclusion=("D records of this filing occurrence whose item p (contracted "
                           "storage quantity, column 12) parses as a number above zero"),
                exclusion=(f"all {len(contracts)} D records are excluded: item p is blank, "
                           f"unparseable or zero on every one of them"),
                value_of=lambda c: None, unit=None, candidate_count=len(contracts),
                empty_reason=(f"the filing WAS retrieved and parsed and its {len(contracts)} "
                              f"D records were examined; none reports a contracted storage "
                              f"obligation. This is a filer with no firm storage customers "
                              f"in this snapshot, not a storage obligation of zero"),
                concept="ioc_contracted_storage_quantity")
        else:
            p_storage = sum(p["storage_qty"] or 0 for c in with_storage for p in c["points"]
                            if p["point_code"] in ("IJ", "WR"))
            storage_obs = _obs(
                entity_key, m, scope=m.scope, unit=unit_storage,
                value_text=f"{total_storage:.10g}", value_num=total_storage,
                availability=Availability.PRESENT, candidate_count=len(with_storage),
                qa_flags="; ".join([
                    header_note,
                    f"{len(with_storage)} storage contracts of {len(contracts)} D records",
                    "STOCK, not a rate: item p is captioned 'For Storage, max Daily "
                    "Quantity' but the manual's instruction is 'the largest quantity of "
                    "natural gas the pipeline is obligated to store'. It is never added to "
                    "transportation MDQ, inventory capacity or daily withdrawal",
                    f"for contrast, the P-record injection/withdrawal rates on the same "
                    f"contracts sum to {p_storage:.10g} "
                    f"{(uom_s + '/day') if uom_s else '(unit not stated)/day'} -- a "
                    f"DIFFERENT dimension, never reconciled against this figure"]),
                validation=(Validation.PASS if uom_s else Validation.UNIT_WARNING),
                **common)
            obs.append(storage_obs)
            zero_or_blank = [c for c in contracts if c not in with_storage]
            lin.contributions(
                storage_obs, "contracts_with_storage_quantity", with_storage, filing_id=acc,
                inclusion=("D (detail) records of this filing occurrence whose item p "
                           "(contracted storage quantity, column 12) parses as a number "
                           "above zero. Item p is a STOCK -- the manual's instruction is "
                           "'the largest quantity of natural gas the pipeline is obligated "
                           "to store' -- so it is never mixed with item o, which is a rate"),
                exclusion=(f"{len(zero_or_blank)} D record(s) whose item p is blank, "
                           f"unparseable or zero are excluded; a blank is not a zero and a "
                           f"zero storage obligation adds nothing to a total"),
                value_of=lambda c: c["storage_quantity"], unit=unit_storage,
                aggregate_value=total_storage, candidate_count=len(contracts),
                concept="ioc_contracted_storage_quantity")

    # ---- weighted shares ---------------------------------------------------
    # Every share metric names its own denominator. A storage-only index has no
    # transportation MDQ at all, so the same measures are also computed on the
    # contracted storage quantity -- as a SEPARATELY LABELLED weight, never mixed
    # with the MDQ weight and never added to it.
    weights = []
    if with_mdq and total_mdq > 0:
        weights.append({"label": "transportation MDQ (D item o)", "unit": unit_transport,
                        "total": total_mdq, "rows": with_mdq, "anchor": mdq_obs,
                        "anchor_metric": "ioc_firm_transport_mdq",
                        "get": lambda c: c["transport_mdq"] or 0.0})
    if with_storage and total_storage > 0:
        weights.append({"label": "contracted storage quantity (D item p)",
                        "unit": unit_storage, "total": total_storage, "rows": with_storage,
                        "anchor": storage_obs,
                        "anchor_metric": "ioc_contracted_storage_quantity",
                        "get": lambda c: c["storage_quantity"] or 0.0})

    no_weight_reason = (
        f"this snapshot offers no non-zero denominator to weight a share by: matched "
        f"transportation MDQ is {total_mdq:.10g} over {len(with_mdq)} contracts and "
        f"contracted storage quantity is {total_storage:.10g} over {len(with_storage)} "
        f"contracts. A share over a zero denominator is undefined and is not published")

    def denom_edge(o, w):
        """The DENOMINATOR anchor -- necessary, and on its own never sufficient.

        In the delivered run every derived IOC share carried this edge and
        nothing else, so an aggregate could only be traversed to another
        aggregate (audit A08). Each caller additionally persists the source rows
        that make up its own numerator, via `lin.contributions`.
        """
        if w["anchor"]:
            lin.edge(o, "denominator", "/", acc, w["total"], w["unit"],
                     concept=w["anchor_metric"],
                     observation_id_=w["anchor"]["observation_id"], version=vstat)
        # the weight's own contract population, so the denominator itself is a
        # named set of source rows and not just a number on another observation
        lin.contributions(
            o, f"denominator_rows::{w['label']}", w["rows"], filing_id=acc,
            inclusion=(f"D records of this filing occurrence contributing to the "
                       f"{w['label']} denominator"),
            exclusion=(f"D records with no parseable {w['label']} value are excluded from "
                       f"the denominator and are not read as zero"),
            value_of=w["get"], unit=w["unit"], aggregate_value=w["total"],
            candidate_count=len(contracts), concept=w["anchor_metric"])

    def no_weight_population(o):
        """The examined population behind an unweightable share.

        These observations carry no value, so no lineage EDGE is owed -- but data
        WAS retrieved and examined, and `no_weight_reason` states the counts in
        prose. Prose is what A08 exists to replace, so the examined set is
        persisted here too. That closes the category: every IOC observation
        derived from a parsed filing carries either contributing edges or an
        explicitly persisted examined population, and none rests on prose alone.
        """
        # TWO DIFFERENT FACTS reach this branch and must not share one sentence:
        # a filing with no D record at all (Fayetteville Express files exactly
        # that, 11 snapshots running), and a filing whose contracts exist but
        # supply no non-zero weight. "Its 0 D records were examined" and "all 0 D
        # records are excluded" are both nonsense, and writing them would be the
        # same defect this file spent the day removing -- one message serving two
        # facts.
        if not contracts:
            exclusion = ("nothing is excluded, because the filing carries no D (detail) "
                         "record to exclude; A, P, F, H and unclassified rows are not "
                         "contracts and never supply a weight")
            empty_reason = ("the filing WAS retrieved and parsed and contains no D (detail) "
                            "record at all, so there is no contract population to weight a "
                            "share by. A share with no population is UNDEFINED, not zero, "
                            "and this is an absent statement by the filer rather than "
                            "missing data")
        else:
            exclusion = (f"all {len(contracts)} D record(s) are excluded on the weight they "
                         f"carry: matched transportation MDQ totals {total_mdq:.10g} over "
                         f"{len(with_mdq)} contract(s) and contracted storage quantity totals "
                         f"{total_storage:.10g} over {len(with_storage)}, so neither weight "
                         f"is non-zero")
            empty_reason = (f"the filing WAS retrieved and parsed and its {len(contracts)} D "
                            f"record(s) were examined; none supplies a non-zero weighting "
                            f"denominator, so a share over it is UNDEFINED and is not "
                            f"published. Undefined is not zero, and this is not missing data")
        lin.contributions(
            o, "examined_contracts_no_weighting_denominator", [], filing_id=acc,
            inclusion=("D records of this filing occurrence that could have supplied a "
                       "weighting denominator, i.e. a parseable item o above zero or a "
                       "parseable item p above zero"),
            exclusion=exclusion, value_of=lambda c: None, unit=None,
            candidate_count=len(contracts), empty_reason=empty_reason,
            concept="no_weighting_denominator")

    # ---- identity coverage -------------------------------------------------
    m = metrics.get("ioc_identity_coverage")
    id_pct_by_weight = {}
    if m is not None:
        if not weights:
            obs.append(_obs(entity_key, m, scope=m.scope, unit=None, value_text=None,
                            value_num=None, availability=Availability.NOT_APPLICABLE,
                            method=Method.DERIVED, missing_reason=no_weight_reason,
                            qa_flags=header_note, **common))
            no_weight_population(obs[-1])
        for i, w in enumerate(weights):
            with_id = [c for c in w["rows"] if c["shipper_id"].strip()]
            pct = 100.0 * sum(w["get"](c) for c in with_id) / w["total"]
            id_pct_by_weight[w["label"]] = pct
            # the PRIMARY weight answers the requested slot and therefore carries
            # the registry scope verbatim; any further weight is an additional,
            # separately scoped assertion. Both name their weight in qa_flags.
            o = _obs(entity_key, m,
                     scope=m.scope if i == 0 else f"{m.scope} | weight: {w['label']}",
                     unit="percent", value_text=f"{pct:.4f}", value_num=pct,
                     availability=Availability.PRESENT, method=Method.DERIVED,
                     selector="ioc_coverage",
                     derivation=f"share(weight of contracts carrying a shipper id, "
                                f"{w['label']})",
                     candidate_count=len(with_id),
                     qa_flags="; ".join([
                         f"denominator: {w['total']:.10g} {w['unit'] or '(unit not stated)'} "
                         f"of {w['label']} across {len(w['rows'])} contracts",
                         f"{len(with_id)} of {len(w['rows'])} contracts carry a shipper id "
                         f"(item ya)",
                         "shipper ids are FILER-LOCAL: item ya is nominally a DUNS number but "
                         "is not reliably 9 digits (Transco zero-pads to 9 and leaves some "
                         "blank, TGP emits 4- to 9-digit values), so they are not joined "
                         "across filers without evidence supporting a wider identity"]),
                     **common)
            obs.append(o)
            denom_edge(o, w)
            without_id = [c for c in w["rows"] if not c["shipper_id"].strip()]
            lin.contributions(
                o, f"numerator_contracts_with_shipper_id::{w['label']}", with_id,
                filing_id=acc,
                inclusion=(f"contracts in the {w['label']} denominator whose item ya "
                           f"(shipper id) is non-blank"),
                exclusion=(f"{len(without_id)} contract(s) in the denominator carry no "
                           f"shipper id and form the complement; nothing else is excluded"),
                value_of=w["get"], unit=w["unit"],
                aggregate_value=sum(w["get"](c) for c in with_id),
                candidate_count=len(w["rows"]),
                empty_reason=("every contract in the denominator was examined and none "
                              "carries a shipper id; the coverage is a measured 0%, not an "
                              "absence of data" if not with_id else ""),
                concept="ioc_identity_coverage")

    # ---- expiry profile ----------------------------------------------------
    m = metrics.get("ioc_expiry_profile")
    if m is not None:
        if not weights:
            obs.append(_obs(entity_key, m, scope=m.scope, unit=None, value_text=None,
                            value_num=None, availability=Availability.NOT_APPLICABLE,
                            method=Method.DERIVED, missing_reason=no_weight_reason,
                            qa_flags=header_note, **common))
            no_weight_population(obs[-1])
        rollover = _census(c["rollover_days"] for c in contracts if c["rollover_days"])
        for i, w in enumerate(weights):
            buckets, unknown_share = _expiry_buckets(w["rows"], snap, w["total"], w["get"])
            if i == 0:
                # the profile itself answers the requested slot: one observation
                # at the registry scope carrying every bucket, with the buckets
                # also emitted individually below for detail. The buckets are
                # never summed into a single "expiry" number.
                profile = "; ".join(f"{b}={buckets[b]['weight']:.10g}"
                                    for b in EXPIRY_BUCKETS)
                head = _obs(entity_key, m, scope=m.scope, unit=w["unit"],
                            value_text=profile, value_num=None,
                            availability=Availability.PRESENT, method=Method.DERIVED,
                            selector="ioc_expiry_buckets",
                            derivation="bucket(primary term expiry item m - snapshot)",
                            candidate_count=len(w["rows"]),
                            qa_flags="; ".join([
                                f"denominator: {w['total']:.10g} "
                                f"{w['unit'] or '(unit not stated)'} of {w['label']} across "
                                f"{len(w['rows'])} contracts",
                                f"unknown-expiry share of this weight: {unknown_share:.2f}%",
                                "buckets are reported separately and are never added into a "
                                "single expiry figure; each bucket is also emitted as its "
                                "own observation",
                                "item m is the PRIMARY TERM expiry, not the contract's end "
                                "date: a date already past means the contract has rolled "
                                "over and is still in force, and is reported in the "
                                "continuing-after-primary-term bucket, never as an expiry",
                                "the continuation field (item n) is a ROLLOVER PERIOD IN "
                                "DAYS, not a guaranteed termination and not a notice date; "
                                "no termination date is computed from m + n"]),
                            notes=f"item n values in this file: "
                                  f"{rollover or 'blank on every record'}",
                            **common)
                obs.append(head)
                denom_edge(head, w)
                lin.contributions(
                    head, f"bucketed_contracts::{w['label']}", w["rows"], filing_id=acc,
                    inclusion=(f"every contract in the {w['label']} denominator, each placed "
                               f"in exactly one remaining-primary-term bucket by comparing "
                               f"item m with the snapshot date {snap}"),
                    exclusion=("no contract in the denominator is dropped: a contract with no "
                               "item m goes to the 'unknown' bucket and one whose item m has "
                               "already passed goes to 'continuing_after_primary_term', "
                               "never to an expiry bucket"),
                    value_of=w["get"], unit=w["unit"], aggregate_value=w["total"],
                    candidate_count=len(w["rows"]), concept="ioc_expiry_profile")
            for bucket in EXPIRY_BUCKETS:
                b = buckets[bucket]
                o = _obs(entity_key, m,
                         scope=(f"{m.scope} | remaining primary term bucket {bucket} "
                                f"| weight: {w['label']}"),
                         unit=w["unit"], value_text=f"{b['weight']:.10g}",
                         value_num=b["weight"], availability=Availability.PRESENT,
                         method=Method.DERIVED, selector="ioc_expiry_buckets",
                         derivation=f"bucket(primary term expiry item m - snapshot, {bucket})",
                         candidate_count=b["n"],
                         qa_flags="; ".join([
                             f"{b['n']} contracts; {b['weight']:.10g} "
                             f"{w['unit'] or '(unit not stated)'}; "
                             f"{100.0 * b['weight'] / w['total']:.2f}% of the "
                             f"{w['total']:.10g} denominator ({w['label']})",
                             f"unknown-expiry share of this weight in this snapshot: "
                             f"{unknown_share:.2f}%",
                             "item m is the PRIMARY TERM expiry, not the contract's end "
                             "date: a date already past means the contract has rolled over "
                             "and is still in force, and is reported in the "
                             "continuing-after-primary-term bucket, never as an expiry",
                             "the continuation field (item n) is a ROLLOVER PERIOD IN DAYS, "
                             "not a guaranteed termination and not a notice date; no "
                             "termination date is computed from m + n"]),
                         notes=f"item n values in this file: "
                               f"{rollover or 'blank on every record'}",
                         **common)
                obs.append(o)
                denom_edge(o, w)
                # A ZERO BUCKET IS A MEASURED ZERO, and it says so. The weight
                # is 0 because every one of the denominator's contracts was
                # examined and none fell in this bucket -- which is a different
                # fact from having retrieved nothing, and only the first of the
                # two is a zero (audit A08).
                lin.contributions(
                    o, f"bucket::{bucket}::{w['label']}", b["rows"], filing_id=acc,
                    inclusion=(f"contracts in the {w['label']} denominator whose remaining "
                               f"primary term at {snap}, from item m, falls in bucket "
                               f"{bucket}"),
                    exclusion=(f"the other {len(w['rows']) - b['n']} contract(s) of the "
                               f"denominator fall in one of the other "
                               f"{len(EXPIRY_BUCKETS) - 1} buckets; the buckets partition "
                               f"the denominator and no contract is counted twice"),
                    value_of=w["get"], unit=w["unit"], aggregate_value=b["weight"],
                    candidate_count=len(w["rows"]),
                    empty_reason=(f"all {len(w['rows'])} contracts of the {w['label']} "
                                  f"denominator were retrieved, parsed and examined and none "
                                  f"falls in the {bucket} bucket; this 0 is a measured "
                                  f"result, not missing data" if not b["rows"] else ""),
                    concept="ioc_expiry_profile")

    # ---- top-five shipper concentration ------------------------------------
    m = metrics.get("ioc_top5_shipper_concentration")
    if m is not None:
        if not weights:
            obs.append(_obs(entity_key, m, scope=m.scope, unit=None, value_text=None,
                            value_num=None, availability=Availability.NOT_APPLICABLE,
                            method=Method.DERIVED, missing_reason=no_weight_reason,
                            qa_flags=header_note, **common))
            no_weight_population(obs[-1])
        for i, w in enumerate(weights):
            by_shipper: dict[str, dict] = {}
            for c in w["rows"]:
                key = normalise_shipper(c["shipper_name"])
                st = by_shipper.setdefault(key, {"w": 0.0, "n": 0, "name": c["shipper_name"]})
                st["w"] += w["get"](c)
                st["n"] += 1
            ranked = sorted(by_shipper.items(), key=lambda kv: -kv[1]["w"])
            top5 = ranked[:5]
            share = 100.0 * sum(st["w"] for _k, st in top5) / w["total"]
            unknown_share = _expiry_buckets(w["rows"], snap, w["total"], w["get"])[1]
            o = _obs(entity_key, m,
                     scope=m.scope if i == 0 else f"{m.scope} | weight: {w['label']}",
                     unit="percent", value_text=f"{share:.4f}", value_num=share,
                     availability=Availability.PRESENT, method=Method.DERIVED,
                     selector="ioc_aggregate_then_rank",
                     derivation="aggregate contracts to the legal shipper, then rank",
                     candidate_count=len(ranked),
                     qa_flags="; ".join([
                         f"denominator: {w['total']:.10g} {w['unit'] or '(unit not stated)'} "
                         f"of {w['label']} across {len(w['rows'])} contracts",
                         f"contracts aggregated to {len(ranked)} distinct LEGAL SHIPPER "
                         f"names BEFORE ranking",
                         f"identity coverage on this weight: "
                         f"{id_pct_by_weight.get(w['label'], float('nan')):.2f}% carries a "
                         f"shipper id",
                         f"unknown-expiry share of this weight: {unknown_share:.2f}%",
                         "share of the stated denominator only -- not credit exposure and "
                         "not revenue concentration",
                         "no corporate-family merging is applied: separately named legal "
                         "entities stay separate, so this is a floor on family-level "
                         "concentration, not an estimate of it"]),
                     notes="; ".join(f"{st['name']}={st['w']:.10g}" for _k, st in top5),
                     **common)
            obs.append(o)
            denom_edge(o, w)
            # Each displayed shipper summary is an aggregate in its own right.
            # Point it at the exact D rows for THAT shipper, rather than leaving
            # a filing/name/value description that resolves to no row or pointing
            # all five summaries at the same non-specific union population.
            for shipper_key, st in top5:
                shipper_rows = [c for c in w["rows"]
                                if normalise_shipper(c["shipper_name"]) == shipper_key]
                lin.contributions(
                    o, f"top5_shipper_member::{w['label']}::{shipper_key}",
                    shipper_rows, filing_id=acc,
                    inclusion=(f"contracts in the {w['label']} denominator whose legal "
                               f"shipper name, after whitespace/case normalisation only, "
                               f"equals {shipper_key!r}"),
                    exclusion=(f"the other {len(w['rows']) - len(shipper_rows)} contract(s) "
                               f"in the denominator belong to a different normalized legal "
                               f"shipper name; no corporate-family merging is applied"),
                    value_of=w["get"], unit=w["unit"], aggregate_value=st["w"],
                    candidate_count=len(w["rows"]), concept=st["name"],
                    edge_role="group_member", version=vstat)
            # A shipper-level group member is itself an aggregate over contracts.
            # The CONTRACTS behind the top five are persisted too, so the chain
            # ends at source rows rather than at another summary.
            top_keys = {k for k, _st in top5}
            top_rows = [c for c in w["rows"]
                        if normalise_shipper(c["shipper_name"]) in top_keys]
            lin.contributions(
                o, f"top5_shipper_contracts::{w['label']}", top_rows, filing_id=acc,
                inclusion=(f"contracts in the {w['label']} denominator held by the five "
                           f"legal shipper names with the largest aggregated weight; "
                           f"contracts are aggregated to the shipper FIRST and the shippers "
                           f"ranked afterwards, per the manual's shipper-level reading"),
                exclusion=(f"the {len(w['rows']) - len(top_rows)} contract(s) of the "
                           f"denominator held by the other {max(len(ranked) - 5, 0)} legal "
                           f"shipper name(s) are excluded; no corporate-family merging is "
                           f"applied, so separately named legal entities are separate "
                           f"shippers and this is a floor on family-level concentration"),
                value_of=w["get"], unit=w["unit"],
                aggregate_value=sum(st["w"] for _k, st in top5),
                candidate_count=len(w["rows"]),
                concept="ioc_top5_shipper_concentration")

    # ---- affiliate share ---------------------------------------------------
    m = metrics.get("ioc_affiliate_share")
    if m is not None:
        if not weights:
            obs.append(_obs(entity_key, m, scope=m.scope, unit=None, value_text=None,
                            value_num=None, availability=Availability.NOT_APPLICABLE,
                            method=Method.DERIVED, missing_reason=no_weight_reason,
                            qa_flags=header_note, **common))
            no_weight_population(obs[-1])
        for i, w in enumerate(weights):
            groups = {"Y": 0.0, "N": 0.0, "unknown": 0.0}
            counts = {"Y": 0, "N": 0, "unknown": 0}
            for c in w["rows"]:
                key = c["affiliate_flag"] if c["affiliate_flag"] in ("Y", "N") else "unknown"
                groups[key] += w["get"](c)
                counts[key] += 1
            rows_by_key = {"Y": [], "N": [], "unknown": []}
            for c in w["rows"]:
                rows_by_key[c["affiliate_flag"] if c["affiliate_flag"] in ("Y", "N")
                            else "unknown"].append(c)
            for key, label in (("Y", "affiliate flag = Y"),
                               ("unknown", "affiliate flag blank or unrecognised")):
                pct = 100.0 * groups[key] / w["total"]
                # the affiliate share on the primary weight is the requested
                # figure; the unknown-flag share and any further weight are
                # separate assertions and are never merged into it
                headline = (i == 0 and key == "Y")
                o = _obs(entity_key, m,
                         scope=(m.scope if headline
                                else f"{m.scope} | {label} | weight: {w['label']}"),
                         unit="percent", value_text=f"{pct:.4f}", value_num=pct,
                         availability=Availability.PRESENT, method=Method.DERIVED,
                         selector="ioc_flag_share",
                         derivation=f"share(weight where {label}, {w['label']})",
                         candidate_count=counts[key],
                         qa_flags="; ".join([
                             f"denominator: {w['total']:.10g} "
                             f"{w['unit'] or '(unit not stated)'} of {w['label']} across "
                             f"{len(w['rows'])} contracts",
                             f"contract counts -- affiliate Y: {counts['Y']}, N: "
                             f"{counts['N']}, blank/unrecognised: {counts['unknown']}",
                             "unknown affiliate rows are counted separately and are never "
                             "merged into non-affiliate",
                             "the affiliate flag is D item yb (column 4); the far more "
                             "common Y/N flag at column 10 is the NEGOTIATED-RATE flag and "
                             "is a different field"]),
                         **common)
                obs.append(o)
                denom_edge(o, w)
                lin.contributions(
                    o, f"affiliate::{key}::{w['label']}", rows_by_key[key], filing_id=acc,
                    inclusion=(f"contracts in the {w['label']} denominator whose D item yb "
                               f"(column 4, shipper affiliation) is "
                               + ("'Y'" if key == "Y"
                                  else "blank or a value other than Y or N")),
                    exclusion=(f"the remaining {len(w['rows']) - counts[key]} contract(s) of "
                               f"the denominator are excluded (Y: {counts['Y']}, "
                               f"N: {counts['N']}, blank/unrecognised: {counts['unknown']}); "
                               f"an unknown flag is never merged into non-affiliate, and "
                               f"item yd at column 10 is the negotiated-rate flag, a "
                               f"different field that is never read as affiliation"),
                    value_of=w["get"], unit=w["unit"], aggregate_value=groups[key],
                    candidate_count=len(w["rows"]),
                    empty_reason=(f"all {len(w['rows'])} contracts of the denominator were "
                                  f"examined and none carries {label}; the 0% is measured"
                                  if not rows_by_key[key] else ""),
                    concept="ioc_affiliate_share")

    # ---- point / segment records -------------------------------------------
    m = metrics.get("ioc_points")
    if m is not None:
        points = [p for c in contracts for p in c["points"]]
        if not points:
            o = _obs(entity_key, m, scope=m.scope, unit=None, value_text=None,
                     value_num=None, availability=Availability.SOURCE_BLANK,
                     candidate_count=0,
                     missing_reason="this snapshot carries no P (point) records",
                     qa_flags=header_note, **common)
            obs.append(o)
            lin.contributions(
                o, "point_records", [], filing_id=acc,
                inclusion="P (point) records of this filing occurrence",
                exclusion="no row was excluded; there was nothing to exclude",
                value_of=lambda r: None, unit=None, candidate_count=0,
                empty_reason=(f"the filing WAS retrieved and parsed and its "
                              f"{len(contracts)} D records were examined; it carries no P "
                              f"record at all. The filer published no location detail, "
                              f"which is not a count of zero points on the system"),
                concept="ioc_points")
        else:
            doubling = _doubling_diagnostic(contracts)
            coded_points = [p for p in points if (p.get("point_code") or "").strip()]
            blank_point_codes = len(points) - len(coded_points)
            census = _census(p["point_code"] for p in coded_points)
            # The registry declares this metric CATEGORICAL (unit_rule "codes"),
            # and that is the right reading: a P record is a location statement,
            # so what the snapshot asserts is WHICH NAESB point codes appear,
            # not a quantity. Publishing the row count as value_num invited it to
            # be read as a measure of the pipeline; the count belongs in
            # candidate_count, which is what that column is for.
            point_code_note = (
                f"{blank_point_codes} P record(s) have blank item yh; they remain in "
                "source_facts and the file-level population but are not promoted to a "
                "present point-code observation"
                if blank_point_codes else
                "every P record carries a nonblank item yh point code")
            head_flags = "; ".join([
                f"{len(points)} P records retained for location detail, by NAESB point "
                f"code: {census or '(none stated)'}",
                point_code_note,
                "CATEGORICAL: the value is the census of NAESB point codes present in "
                "this snapshot. The record COUNT is candidate_count, and it is a "
                "property of the file, not a quantity of anything on the system",
                "LOCATION DETAIL ONLY: per-point quantities are caps, never an additive "
                "decomposition of the contract, and no allocation is published from "
                "them. Contract capacity is D item o",
                "S8 and S9 are SEGMENT ENDPOINTS, not receipt codes",
                f"NO 'P sum = 2 x D total' rule is applied -- measured on this snapshot "
                f"it holds for {doubling}",
                "each nonblank point code is also emitted as its own observation, and "
                "every P record is retained in source_facts"])
            if coded_points:
                head = _obs(
                    entity_key, m, scope=m.scope, unit="codes",
                    value_text=census, value_num=None,
                    availability=Availability.PRESENT, selector="ioc_p_records",
                    validation=Validation.SCOPE_INCOMPATIBLE,
                    candidate_count=len(points), qa_flags=head_flags, **common)
            else:
                head = _obs(
                    entity_key, m, scope=m.scope, unit=None,
                    value_text=None, value_num=None,
                    availability=Availability.SOURCE_BLANK,
                    selector="ioc_p_records", candidate_count=len(points),
                    missing_reason=(f"this snapshot carries {len(points)} P record(s), but "
                                    "item yh (point code) is blank throughout"),
                    qa_flags=head_flags, **common)
            obs.append(head)
            lin.contributions(
                head, "point_records", points, filing_id=acc,
                inclusion=("every P (point) record of this filing occurrence, whatever its "
                           "NAESB point code"),
                exclusion=("no P record is dropped; D, A, F, H and unclassified rows are not "
                           "point records and never contribute"),
                value_of=lambda r: r["point_code"], unit="codes", aggregate_value=None,
                candidate_count=len(points), concept="ioc_points")
            for code in sorted({p["point_code"] for p in coded_points}):
                rows = [p for p in coded_points if p["point_code"] == code]
                t_sum = sum(p["transport_qty"] or 0 for p in rows)
                s_sum = sum(p["storage_qty"] or 0 for p in rows)
                meaning = POINT_CODES.get(code, "code not in the Form 549B manual's table")
                o = _obs(
                    entity_key, m, scope=f"{m.scope} | point code {code} ({meaning})",
                    unit="codes", value_text=f"{code} ({meaning})", value_num=None,
                    availability=Availability.PRESENT, selector="ioc_p_records",
                    validation=Validation.SCOPE_INCOMPATIBLE,
                    candidate_count=len(rows),
                    qa_flags="; ".join([
                        f"CATEGORICAL: this snapshot carries the NAESB point code {code}; "
                        f"the {len(rows)} records that carry it are candidate_count, not a "
                        f"published quantity",
                        f"{len(rows)} P records; transport quantities sum to {t_sum:.10g} "
                        f"{unit_transport} and storage quantities to {s_sum:.10g} "
                        f"{uom_s}/day at this code",
                        "LOCATION DETAIL ONLY: these are per-point caps, never an additive "
                        "decomposition of the contract, and no allocation is published from "
                        "them. Contract capacity is D item o",
                        "S8 and S9 are SEGMENT ENDPOINTS, not receipt codes; flow direction "
                        "cannot be inferred from S8-vs-S9 (Transco emits S8 before S9, "
                        "against the manual's convention, and the counts are not 1:1)",
                        f"NO 'P sum = 2 x D total' rule is applied -- measured on this "
                        f"snapshot it holds for {doubling}",
                        "the P-record storage quantity (item yn) is a DAILY RATE and is "
                        "dimensionally different from the D-record storage quantity"]),
                    notes=f"zones: {_census(p['zone'] for p in rows)}",
                    **common)
                obs.append(o)
                lin.contributions(
                    o, f"point_records::{code}", rows, filing_id=acc,
                    inclusion=(f"P records of this filing occurrence whose item yh (point "
                               f"code) is {code} -- {meaning}"),
                    exclusion=(f"the other {len(points) - len(rows)} P record(s) carry a "
                               f"different point code; S8 and S9 are segment endpoints and "
                               f"are never folded into the receipt code M2"),
                    value_of=lambda r: r["point_code"], unit="codes",
                    aggregate_value=None, candidate_count=len(points),
                    concept="ioc_points")

    if not quarter_start:
        for o in obs:
            if o["validation"] == Validation.PASS:
                o["validation"] = Validation.SOURCE_ANOMALY_REVIEW
    return obs


def _repair_checks(parsed: dict) -> list[str]:
    return parsed["provenance"]["header_repair"].get("checks_passed", [])


def _census(values) -> str:
    counts: dict[str, int] = {}
    for v in values:
        v = (v or "").strip()
        if v:
            counts[v] = counts.get(v, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:12])


def _doubling_diagnostic(contracts) -> str:
    """The observed frequency of 'sum of all P transport == 2 x MDQ', reported as
    a measured statistic and explicitly NOT used as a rule."""
    eligible = [c for c in contracts if (c["transport_mdq"] or 0) > 0 and c["points"]]
    if not eligible:
        return "no eligible contract in this snapshot"
    hits = 0
    for c in eligible:
        total = sum(p["transport_qty"] or 0 for p in c["points"])
        if abs(total - 2 * c["transport_mdq"]) < 1e-6:
            hits += 1
    return (f"{hits}/{len(eligible)} contracts "
            f"({100.0 * hits / len(eligible):.1f}%) -- a statistic, not an invariant")


def _expiry_buckets(contracts, snapshot: str, total: float, get):
    """Bucket a contract population by REMAINING PRIMARY TERM, on a named weight.

    Returns ({bucket: {"weight", "n"}}, unknown share of the weight).
    """
    buckets = {b: {"weight": 0.0, "n": 0, "rows": []} for b in EXPIRY_BUCKETS}
    snap = dt.date.fromisoformat(snapshot)
    for c in contracts:
        weight = get(c)
        expiry = c["primary_term_expiry"]
        if not expiry:
            key = "unknown"
        else:
            d = dt.date.fromisoformat(expiry)
            if d <= snap:
                # the primary term has already passed and the contract is still
                # listed: it continues under a rollover provision. It is NOT
                # given an expiry, and item n is a duration, not a date.
                key = "continuing_after_primary_term"
            else:
                years = (d - snap).days / 365.25
                key = ("0-1y" if years <= 1 else "1-2y" if years <= 2
                       else "2-5y" if years <= 5 else "5y+")
        buckets[key]["weight"] += weight
        buckets[key]["n"] += 1
        buckets[key]["rows"].append(c)
    unknown_share = (100.0 * buckets["unknown"]["weight"] / total) if total else 0.0
    return buckets, unknown_share


# ---------------------------------------------------------------- snapshot diff

def _snapshot_diffs(ctx, entity, canonical, metrics, produced, lin: "_Lineage") -> list:
    """Change in matched MDQ between consecutive snapshots of the same filer.

    A contract present in one snapshot and absent from the next is NOT a
    termination event and none is raised here: an index can be incomplete, a
    contract can be restated, and only the pipeline's own filing says a contract
    ended. The count of such contracts is reported as a diagnostic with that
    caveat attached, and never as churn.
    """
    m = metrics.get("ioc_mdq_change")
    if m is None or len(canonical) < 2:
        return []
    entity_key = entity["entity_key"]
    by_instant = {o["instant_date"]: o for o in produced
                  if o["metric_id"] == "ioc_firm_transport_mdq"
                  and o["availability"] == Availability.PRESENT}
    out = []
    for prev, cur in zip(canonical, canonical[1:]):
        a = by_instant.get(prev["snapshot_date"])
        b = by_instant.get(cur["snapshot_date"])
        if a is None or b is None:
            continue
        delta = b["value_num"] - a["value_num"]
        pc, cc = prev["_parsed"]["contracts"], cur["_parsed"]["contracts"]
        prev_keys = {c["contract_number"] for c in pc if c["contract_number"]}
        cur_keys = {c["contract_number"] for c in cc if c["contract_number"]}
        gone, new = prev_keys - cur_keys, cur_keys - prev_keys
        incomplete = bool(pc) and len(cc) < 0.75 * len(pc)

        flags = [
            f"{prev['snapshot_date']} -> {cur['snapshot_date']} "
            f"({a['value_num']:.10g} -> {b['value_num']:.10g} {b['unit']})",
            f"contract numbers present in the earlier snapshot and absent from the later: "
            f"{len(gone)}; newly present: {len(new)}. THESE ARE NOT TERMINATION OR AWARD "
            f"EVENTS: an index can be incomplete or restated, and only the pipeline's own "
            f"filing establishes that a contract ended",
            f"contract counts {len(pc)} -> {len(cc)}",
            "both sides are the canonical file for their snapshot; the arrival of a revised "
            "file is recorded as a separate revision event and never as an economic change",
        ]
        validation = Validation.PASS
        if incomplete:
            flags.append("the later snapshot carries materially fewer D records than the "
                         "earlier one; it may be incomplete, so this difference is flagged "
                         "for review rather than published as a change in contracting")
            validation = Validation.SOURCE_ANOMALY_REVIEW
        for side, filing in (("earlier", prev), ("later", cur)):
            if (filing.get("version_status") or "") in (VersionStatus.REVISED,
                                                        VersionStatus.IDENTICAL_RESUBMISSION):
                flags.append(f"the {side} snapshot's canonical file is a "
                             f"{filing['version_status']} of that same snapshot date, so "
                             f"this comparison is computed on the RESTATED file; the "
                             f"arrival of the revision is a separate revision event and is "
                             f"not itself an economic change")
        for v in (a["validation"], b["validation"]):
            if v in Validation.MUST_PROPAGATE:
                validation = v
                flags.append(f"inherited_from_input: {v}")

        o = _obs(entity_key, m, instant=cur["snapshot_date"],
                 year=cur["reporting_year"], period=cur["reporting_period"],
                 scope=m.scope,
                 unit=b["unit"], value_text=f"{delta:.10g}", value_num=delta,
                 availability=Availability.PRESENT, method=Method.DERIVED,
                 version_status=cur.get("version_status") or VersionStatus.UNRESOLVED,
                 validation=validation, selector="ioc_snapshot_diff",
                 derivation="difference(matched MDQ at the later snapshot, matched MDQ at "
                            "the earlier snapshot)",
                 qa_flags="; ".join(flags), filing_id=cur["filing_id"],
                 accession=cur["filing_id"], document_id=cur.get("_document_id"),
                 candidate_count=len(cc))
        out.append(o)
        # OCCURRENCE COHERENCE. Each edge names both the input observation and
        # the filing occurrence that observation was selected from, and the two
        # must agree. Byte-identical filings share source fact ids, so an edge
        # repointed at the identical-but-wrong submission still RESOLVES; only
        # comparing the occurrences rejects it (audit A08).
        assert a.get("filing_id") == prev["filing_id"], (
            f"the earlier MDQ observation was selected from {a.get('filing_id')} but the "
            f"earlier snapshot's canonical filing is {prev['filing_id']}")
        assert b.get("filing_id") == cur["filing_id"], (
            f"the later MDQ observation was selected from {b.get('filing_id')} but the "
            f"later snapshot's canonical filing is {cur['filing_id']}")
        lin.edge(o, "minuend", "+", cur["filing_id"], b["value_num"], b["unit"],
                 concept="ioc_firm_transport_mdq", observation_id_=b["observation_id"],
                 version=cur.get("version_status") or "")
        lin.edge(o, "subtrahend", "-", prev["filing_id"], a["value_num"],
                 a["unit"], concept="ioc_firm_transport_mdq",
                 observation_id_=a["observation_id"],
                 version=prev.get("version_status") or "")
    return out


# ---------------------------------------------------------------- remainder

def _account_for_remainder(entity, metrics, expected, produced, canonical) -> list[dict]:
    """Every frozen slot ends in a measured status with a reason.

    A slot left silently empty is indistinguishable from unfinished engineering,
    so anything still unmatched after selection gets an explicit observation
    naming why -- and the reason distinguishes a source condition from our gap.
    """
    entity_key = entity["entity_key"]
    # slots are now per (metric, snapshot), so the remainder is too: a snapshot
    # that legitimately has no value for one metric gets its OWN answer, and is
    # never left to be satisfied by another snapshot's value.
    have = {(o["metric_id"], o["instant_date"] or "") for o in produced}
    out = []
    first_snapshot = canonical[0]["snapshot_date"] if canonical else ""
    for slot in expected:
        key = (slot["metric_id"], slot.get("instant_date") or "")
        if key in have:
            continue
        m = metrics.get(slot["metric_id"])
        if m is None:
            continue
        year, period = slot["reporting_year"], slot["reporting_period"]
        instant = slot.get("instant_date") or ""
        if not canonical:
            state, detail = _RETRIEVAL_STATE.get(entity_key, ("state_not_recorded", ""))
            if state in ("search_failed", "download_failed"):
                # OUR failure, not FERC's silence
                avail = Availability.RETRIEVAL_FAILED
                reason = detail
            elif state == "no_owned_filings":
                # A THIRD state, distinct from both: documents were located and
                # read, and every one of them names a different filer in its own
                # header. That is neither our failure nor FERC's silence -- it is
                # a specific finding about this entity, and flattening it into
                # "nothing was found" is what let another pipeline's contract
                # book stand in for this one's (audit A02).
                avail = Availability.UNVERIFIED_AVAILABILITY
                reason = detail
            elif state == "state_not_recorded":
                # No retrieval outcome was recorded for this entity at all. That
                # is NOT the same as "eLibrary returned nothing", and saying so
                # would assert a search result nobody obtained. The delivered
                # default did exactly that -- a missing state read as FERC's
                # silence. Same class as the UOM and version-status defaults.
                avail = Availability.UNVERIFIED_AVAILABILITY
                reason = ("no retrieval outcome was recorded for this entity, so it is not "
                          "established whether eLibrary was searched, whether it returned "
                          "anything, or whether retrieval failed. This is an unrecorded "
                          "outcome, NOT a finding that FERC holds no Index of Customers for "
                          "this filer, and it should be treated as an engine gap to "
                          "investigate rather than as a source condition")
            else:
                avail = Availability.UNVERIFIED_AVAILABILITY
                reason = ("no Index of Customers filing was located in eLibrary for this "
                          "filer in the requested window, so whether the obligation applies "
                          "could not be established; this is an unresolved applicability, "
                          "not a retrieval failure and not an unimplemented metric")
        elif m.id == "ioc_mdq_change" and instant == first_snapshot:
            avail = Availability.NOT_APPLICABLE
            reason = (f"a snapshot-to-snapshot change needs a preceding snapshot; "
                      f"{instant} is the earliest snapshot retrieved in the requested "
                      f"window, so no comparison exists for it")
        elif m.id == "ioc_mdq_change":
            avail = Availability.NOT_APPLICABLE
            reason = (f"no comparable predecessor snapshot for {instant}; "
                      f"{len(canonical)} canonical snapshots were retrieved")
        else:
            avail = Availability.NOT_IMPLEMENTED
            reason = (f"no observation was produced for this metric at the {instant} "
                      f"snapshot grain")
        out.append(_obs(entity_key, m, instant=instant,
                        year=year, period=period, scope=m.scope, unit=None,
                        value_text=None, value_num=None, availability=avail,
                        method=Method.DERIVED if m.dependencies else Method.FILED,
                        validation=Validation.NOT_YET_VALIDATED,
                        missing_reason=reason,
                        applicability_evidence=slot["requirement_evidence"]))
    return out
