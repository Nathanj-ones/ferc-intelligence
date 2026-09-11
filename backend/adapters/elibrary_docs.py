"""
Rate and regulatory-proceeding adapter: eLibrary documents for interstate gas,
gas storage, liquids (ICA) and intrastate NGPA s.311 filers.

The four rules this exists to enforce, each of them a live finding rather than a
design preference (discovery/docs_discovery.md s.4):

  1. FILING, ACCEPTANCE, SUSPENSION, SETTLEMENT APPROVAL, RATE EFFECTIVENESS AND
     REFUND COMPLETION ARE DIFFERENT STATES. `STAGES` is the controlled
     vocabulary; nothing collapses two of them.

  2. AN EXPLICITLY SOURCED LEGAL DATE IS MARKED DIFFERENTLY FROM A STAGE
     INFERRED FROM METADATA. A FERC issuance (Order/Opinion, ALJ Issuance)
     yields `sourced_legal_date`; a filer's own "to be effective 10/1/2024" in an
     eTariff description yields `inferred_from_metadata` and never a published
     legal date.

  3. THE MOST RECENT FILING IN A DOCKET DOES NOT SUPERSEDE EVERY EARLIER ORDER.
     RP24-1035's newest document is a refund-report supplement that implements
     the 2025-12-30 settlement; RP25-1189's newest is a D.C. Circuit petition
     that changes no rate; TGP's operative rate authority is a 2019 order. The
     stage is the highest ORDER-BACKED stage reached, and the newest document is
     reported separately with what it actually is.

  4. NO DOLLAR AMOUNT IS PUBLISHED FOR REFUND EXPOSURE. `refund_exposure_window`
     identifies the WINDOW only, and `_refuse_dollars` makes that structural.

`tariff_operative_rate` is gated, not deferred: the tariff sheets ARE eLibrary
attachments (Magellan's "Clean Tariff.pdf", 1.29 MB, public, text-layered), so
the adapter retrieves them, stores every retrieved record identifier as a
document fact with its span, and then lets the gate decide per filer. Where the
gate fails, the retrieved values are RETAINED with an explicit
unresolved-operative-rate status -- they are not thrown away and not published.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ferclib import coverage, elibrary, periods                             # noqa: E402
from ferclib.http import FetchError                                         # noqa: E402
from ferclib.registry import BY_ADAPTER, BY_ID, REGISTRY_VERSION            # noqa: E402
from ferclib.staging import observation_id                                  # noqa: E402
from ferclib.status import Availability, Method, Origin, Validation, VersionStatus  # noqa: E402

ADAPTER = "elibrary_docs"
SOURCE_SYSTEM = elibrary.SOURCE_SYSTEM
REGIME = "eLibrary document"
FORM = "eLibrary document"
HERE = pathlib.Path(__file__).resolve().parent.parent
SEED_CSV = (HERE / "inputs" / "official_ferc" / "elibrary" /
            "rate_proceedings_v1.csv")

SOURCED = "sourced_legal_date"
INFERRED = "inferred_from_metadata"

#: config/universe.csv is the authoritative roster. The verified rate-proceeding
#: discovery pass recorded Tennessee Gas Pipeline under CID C000662; the roster
#: says C000020. The alias keeps the sourced findings -- including the
#: affirmative "no NGA s.4 general rate case since 2015" result -- attached to
#: the roster entity instead of silently losing them.
CID_ALIASES: dict[str, list[str]] = {"C000020": ["C000020", "C000662"]}

# ---------------------------------------------------------------- vocabulary

#: (stage, rank, label). Rank orders the lifecycle; it does NOT order documents
#: by date, because a docket's stage is the furthest point it has reached under
#: an order, not whatever was filed most recently.
STAGES: list[tuple[str, int, str]] = [
    ("filed", 1, "rate change filed with FERC, no Commission action located"),
    ("accepted", 2, "tariff records accepted"),
    ("accepted_and_suspended", 3,
     "accepted AND SUSPENDED, subject to refund -- refund clock started"),
    ("rates_effective_subject_to_refund", 4,
     "suspension ended; motion rates in effect SUBJECT TO REFUND"),
    ("settlement_offered", 5, "offer of settlement filed (Rule 602)"),
    ("settlement_certified", 6, "uncontested settlement certified by the presiding judge"),
    ("settlement_approved", 7, "settlement APPROVED by the Commission"),
    ("settlement_rates_effective", 8, "settlement tariff records accepted and effective"),
    ("refunds_reported", 9, "refund report filed"),
    ("refunds_accepted", 10,
     "refund report ACCEPTED; refund-obligation closure NOT established without "
     "separate operative text"),
]
RANK = {s: r for s, r, _ in STAGES}
LABEL = {s: l for s, _, l in STAGES}

#: states that exist but do not advance a rate proceeding
NON_ADVANCING = {
    "rate_case_update": "rate case update filing (18 CFR 154.311)",
    "compliance_filing": "compliance / effective tariff record filing (18 CFR 154.203)",
    "non_rate_tariff_change": "non-rate tariff change (18 CFR 154.204/154.205(b)/341.4)",
    "refund_report_supplement": "SUPPLEMENT to a refund report -- implements, does not supersede",
    "complaint_filed": "complaint against the pipeline -- NOT a rate case",
    "complaint_denied": "complaint DENIED",
    "rehearing_denied": "rehearing denied",
    "rehearing_addressed": "arguments on rehearing addressed",
    "under_judicial_review": "on petition for review in a U.S. Court of Appeals -- "
                             "changes no rate by itself",
    "hearing_terminated": "hearing and settlement judge procedures terminated",
    "other_issuance": "Commission or delegated issuance, stage not classified",
    "other_submittal": "filing, stage not classified",
}

#: eTariff regulation -> what the filing actually is. The class/type is always
#: `Application/Petition/Request :: Tariff Filing`; the regulation carried IN THE
#: DESCRIPTION is what separates a general rate case from a housekeeping sheet.
REGULATIONS = [
    (re.compile(r"per\s+154\.312\b"), "filed", "NGA s.4 GENERAL RATE CASE (18 CFR 154.312, FT 690)"),
    (re.compile(r"per\s+342\.3\b"), "filed", "ICA INDEXED rate change (18 CFR 342.3, FT 860)"),
    (re.compile(r"per\s+342\.2\s+or\s+342\.4"), "filed",
     "ICA cost-of-service or market-based rate change (18 CFR 342.2/342.4, FT 830)"),
    (re.compile(r"per\s+284\.123\(b\)\(2\)"), "filed",
     "NGPA s.311 RATE PETITION (18 CFR 284.123(b)(2), FT 760)"),
    (re.compile(r"per\s+385\.602\b"), "settlement_offered",
     "OFFER OF SETTLEMENT (Rule 602, FT 1400)"),
    (re.compile(r"per\s+154\.501\b"), "refunds_reported", "REFUND REPORT (18 CFR 154.501, FT 670)"),
    (re.compile(r"per\s+154\.206\b"), "rates_effective_subject_to_refund",
     "motion to place suspended tariff records into effect (18 CFR 154.206)"),
    (re.compile(r"per\s+154\.311\b"), "rate_case_update", "rate case update (18 CFR 154.311, FT 710)"),
    (re.compile(r"per\s+154\.203\b"), "compliance_filing",
     "compliance / effective tariff record (18 CFR 154.203, FT 580)"),
    (re.compile(r"per\s+(154\.204|154\.205\(b\)|341\.4|284\.123\(b\),?\(?e?\)?)"),
     "non_rate_tariff_change", "non-rate tariff change or data response"),
]

#: FERC issuances, classified by WHAT THEY DECIDED. Class/type cannot do this:
#: 20251230-3015 is typed Commission Order/Opinion and worded "Letter order
#: approving...", so the description is the only discriminator.
ISSUANCES = [
    (re.compile(r"accepting\s+and\s+suspending[^.]*subject\s+to\s+refund", re.I),
     "accepted_and_suspended"),
    (re.compile(r"suspending\s+tariff\s+records?,?\s+subject\s+to\s+refund", re.I),
     "accepted_and_suspended"),
    (re.compile(r"place\s+(?:into|in)\s+effect\s+the\s+rates\s+suspended|motion\s+to\s+place\s+"
                r"(?:compliance|suspended)\s+tariff\s+records?\s+into\s+effect", re.I),
     "rates_effective_subject_to_refund"),
    (re.compile(r"certification\s+(?:by\s+presiding\s+judge\s+)?of\s+(?:an?\s+)?"
                r"(?:uncontested\s+)?settlement", re.I), "settlement_certified"),
    # Must precede the settlement-approval rule. "Letter order accepting TGP's
    # 09/09/2022 filing of tariff records to comply with the Commission's May 24,
    # 2019 order APPROVING its Amended and Restated SETTLEMENT" is a compliance
    # acceptance that CITES the approval; classifying it as the approval would
    # restate a 2019 legal date as a 2022 one.
    (re.compile(r"accepting[^.]{0,200}tariff\s+records?[^.]{0,90}"
                r"(?:to\s+comply|in\s+compliance)", re.I), "settlement_rates_effective"),
    (re.compile(r"approving[^.]{0,80}(?:settlement|stipulation\s+and\s+agreement)", re.I),
     "settlement_approved"),
    (re.compile(r"terminating\s+hearing", re.I), "hearing_terminated"),
    (re.compile(r"accepting[^.]{0,120}report\s+of\s+refunds|refunds\s+detailing", re.I),
     "refunds_accepted"),
    (re.compile(r"denying\s+complaint", re.I), "complaint_denied"),
    (re.compile(r"denial\s+of\s+rehearing|denying\s+request\s+for\s+rehearing", re.I),
     "rehearing_denied"),
    (re.compile(r"addressing\s+arguments\s+raised\s+on\s+rehearing", re.I), "rehearing_addressed"),
    (re.compile(r"accepting[^.]{0,120}tariff", re.I), "accepted"),
]

RX_SUPPLEMENT = re.compile(r"supplement\s+to\b", re.I)
RX_PETITION_REVIEW = re.compile(r"petition\s+for\s+review", re.I)
RX_FILER_EFFECTIVE = re.compile(r"to\s+be\s+effective\s+(\d{1,2}/\d{1,2}/\d{4})", re.I)
RX_ORDER_EFFECTIVE = re.compile(
    r"(?:effective|shall\s+(?:become|be)\s+effective|in\s+effect)\s+(?:on\s+)?"
    r"((?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2},\s*\d{4}|\d{1,2}/\d{1,2}/\d{4})", re.I)
RX_SUBJECT_REFUND = re.compile(r"subject\s+to\s+refund", re.I)
#: run against elibrary.squeeze()d text: a tariff PDF's text layer is
#: letter-spaced ("F .E .R. C. N o . 2 0 1 . 5 . 0"), so only a whitespace-free
#: pattern matches. The span is mapped back to the real text for the doc fact.
RX_TARIFF_RECORD = re.compile(
    r"F\.?E\.?R\.?C\.?(?:Tariff)?No\.?(\d+(?:\.\d+)*)"
    r"(?:\((?:Cancel|Cancels|Cancelling|Canceling)F\.?E\.?R\.?C\.?No\.?(\d+(?:\.\d+)*)\))?",
    re.I)
RX_DOLLARS = re.compile(r"[$]\s*[\d,]|\bUSD\b|\bdollars?\b", re.I)
#: a filed numeric rate WITH its unit, run against squeezed text. Locating one is
#: gate condition 5: without a numeric rate carrying a unit there is nothing to
#: publish even when the record and the order reconcile.
#: the GAS eTariff record identifier. Gas tariffs are not numbered "F.E.R.C. No.
#: X.Y.Z" like an oil tariff: Transco's RP24-1035 package identifies its records
#: as "Tariff Title: Fifth Revised Volume No. 1" in the FERC-generated record.
RX_TARIFF_RECORD_GAS = re.compile(
    r"TariffTitle:((?:First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|"
    r"Eleventh|Twelfth|Original)(?:Revised)?VolumeNo\.?\d+)", re.I)
#: attachment names that can carry a tariff record or a rate line
RX_TARIFF_MEMBER = re.compile(
    r"tariff|transmittal|ferc[ _]generated|ltr|letter|appendix|rate|ceiling|order", re.I)
RX_RATE_LINE = re.compile(
    r"\$?(\d+\.\d{2,5})(?:per|/)(Dth|Dekatherm|MMBtu|Barrel|Bbl|Mcf)", re.I)

SWEEP_SUBMITTALS = [
    elibrary.class_type("Application/Petition/Request", "Tariff Filing"),
    elibrary.class_type("Application/Petition/Request", "Complaints"),
]
SWEEP_ISSUANCES = [
    elibrary.class_type("Order/Opinion", "Commission Order/Opinion"),
    elibrary.class_type("Order/Opinion", "Delegated Order"),
    elibrary.class_type("ALJ Issuance", "Certification of Settlement"),
    elibrary.class_type("ALJ Issuance", "Report to the Commission"),
    elibrary.class_type("Pleading/Motion", "Petition for Review"),
]
#: verified live: 6 records industry-wide 2015-2026, so the route works and a
#: zero result for one filer is a SOURCE BLANK, not an unverified route.
INTERRUPTION_TYPES = [elibrary.class_type("Report/Form", "260.9 Service Interruption Report")]
#: replacement facilities are reported through the construction-report types;
#: verified live on Transco (7 records 2024-2026).
REPLACEMENT_TYPES = [
    elibrary.class_type("Report/Form", "2.55 Annual Construction Report"),
    elibrary.class_type("Report/Form", "157.207 Annual Construction Report"),
    elibrary.class_type("Report/Form", "284.11 Annual Construction Report"),
]

GAS_TEMPLATES = {"interstate_gas", "gas_storage"}

#: the published FERC oil index is industry-wide, so it is fetched once per run
_INDEX_CACHE: dict[str, list[dict]] = {}


# ---------------------------------------------------------------- seeds

def _seed_dockets(cid: str) -> tuple[list[str], list[dict]]:
    """(dockets, rows) from the versioned proceeding seed for one CID.

    Rows with no accession are recorded NEGATIVE findings -- "no order exists in
    PR18-59", "no NGA s.4 rate case for TGP in the window" -- and are carried
    through as evidence, never dropped.
    """
    if not SEED_CSV.is_file():
        raise FileNotFoundError(
            "mandatory versioned eLibrary proceeding seed is absent: "
            f"{SEED_CSV}")
    accept = set(CID_ALIASES.get(cid, [cid]))
    rows = []
    with SEED_CSV.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if (r.get("cid") or "").strip() in accept:
                rows.append(r)
    # only the PRIMARY (first) docket of each row: a shared order row lists
    # every docket it covers, and those belong to other companies. Magellan's
    # acceptance order 20241018-3061 spans six IS dockets for six carriers.
    when: dict[str, str] = {}
    for r in rows:
        first = (r.get("docket") or "").split(",")[0].strip()
        d = elibrary.docket_base(first)
        if not d or d.startswith("("):
            continue
        when[d] = max(when.get(d, ""), r.get("filed_date") or "")
    dockets = [d for d, _ in sorted(when.items(), key=lambda kv: kv[1], reverse=True)]
    return dockets, rows


# ---------------------------------------------------------------- retrieval

def retrieve(ctx, entity, *, year_from: int, year_to: int) -> list[dict]:
    key = entity["entity_key"]
    template = entity["template"]
    elib = elibrary.Elibrary(ctx.client, log=ctx.log)
    # Rate proceedings run for years; a 2024-2026 window would hide the 2019
    # order that still governs TGP's rates. The lifecycle window is deliberately
    # wider than the financial-reporting window.
    start = f"{min(year_from, 2015):04d}-01-01"
    end = f"{year_to:04d}-12-31"

    seed_dockets, seed_rows = _seed_dockets(key)
    hits: list[dict] = []

    # 1. name-led census of this filer's tariff filings and complaints. One call
    #    gives every rate docket, every regulation code and the 154.312 test.
    #    A description search matches text, not filers, so the result is
    #    post-filtered on the filer's own name: without that, a Chevron v.
    #    Colonial order that merely mentions a joint tariff would be attributed
    #    to Magellan.
    census = _search(ctx, elib, key, f"census:{start}",
                     text=entity["legal_name"], start=start, end=end,
                     class_types=SWEEP_SUBMITTALS, max_pages=2)
    census = [h for h in census if _target_owned_hit(entity["legal_name"], h)]
    hits += census

    # 2. the dockets that matter, swept for BOTH the filings and the orders --
    #    the order is what makes a stage a sourced legal date, and the filing is
    #    what carries the tariff package.
    dockets = seed_dockets or _dockets_from(census)
    depth = 4 if seed_dockets else 2
    relevant = dockets[:depth]
    for docket in relevant:
        swept = _search(ctx, elib, key, f"docket:{docket}",
                        docket=docket, start=start, end=end,
                        class_types=SWEEP_ISSUANCES + SWEEP_SUBMITTALS, max_pages=3)
        # Commission issuances can legitimately govern several parties.  A
        # tariff submission, by contrast, declares one actor in the official
        # description and cannot be borrowed by a similarly named sibling.
        hits += [h for h in swept if not _is_tariff_submission(h)
                 or _tariff_submitter_matches(entity["legal_name"], h)]

    # 3. reportable interruptions and replacement/construction reports (gas only).
    # eLibrary's text search is not a filer field.  In particular, a search for
    # MountainWest Overthrust also returns MountainWest Pipeline reports.  Apply
    # the same legal-name boundary as the census before a result can be
    # canonicalised or persisted.
    if template in GAS_TEMPLATES:
        reports = _search(ctx, elib, key, "interruption",
                          text=entity["legal_name"], start=start, end=end,
                          class_types=INTERRUPTION_TYPES + REPLACEMENT_TYPES, max_pages=1)
        hits += [h for h in reports
                 if _mentions(entity["legal_name"], h.get("description") or "")]

    # A search window ends at the reporting year's December 31 so one pinned
    # request shape can cover the whole year.  That response may be captured
    # after the declared build date, however, and can then contain filings that
    # did not exist at the release's as-of boundary.  Keep the raw official
    # response in the cache, but never let a later occurrence leak into the
    # point-in-time database, events, lineage or exports.
    as_of = str(getattr(ctx, "as_of_iso", "") or end)
    future = [h for h in hits if _after_as_of(h, as_of)]
    if future:
        ctx.log(
            "warn",
            f"{key}: excluded {len(future)} filing occurrence(s) dated after "
            f"the pinned as-of boundary {as_of}: "
            + ",".join(sorted({str(h.get('accession') or '') for h in future})),
            adapter=ADAPTER,
            entity_cid=key,
        )
        hits = [h for h in hits if not _after_as_of(h, as_of)]

    canonical, twins = elibrary.dedupe_availability(hits)
    for h in canonical:
        h["classification"] = classify(h)
    for t in twins:
        t["classification"] = classify(t)

    # 4. read the two documents that unlock the gated fields: the governing order
    #    (for a sourced effective date) and the tariff package (for the record).
    for target in _documents_to_read(canonical, relevant, seed_rows):
        _fetch_document(ctx, elib, key, target)

    # A11: the first-observed picture is taken BEFORE anything is written,
    # because `_persist` upserts every column and would otherwise make every
    # filing look newly discovered on every run.
    baseline = elibrary.NewsBaseline(ctx.staging, ADAPTER, key, SOURCE_SYSTEM)

    filings = [dict(h, is_twin=False) for h in canonical]
    filings += [dict(t, is_twin=True) for t in twins]
    for f in filings:
        _persist(ctx, entity, f, baseline=baseline)
    for f in filings:
        f["_seed_rows"] = seed_rows
        f["_relevant"] = relevant
        f["_baseline"] = baseline
    baseline.establish()
    ctx.log("info", f"{key}: {len(hits)} hits -> {len(canonical)} distinct filings, "
                    f"{len(twins)} availability twins; proceedings reported for "
                    f"{relevant or 'none'} (of {len(dockets)} located)",
            adapter=ADAPTER, entity_cid=key)
    return filings


def _after_as_of(filing: dict, as_of: str) -> bool:
    """Whether an occurrence is known to post/file/issue after ``as_of``.

    Blank or malformed dates remain unresolved data; they are not guessed into
    the future bucket.  For known ISO dates, any later official date is enough
    to keep the occurrence out of a point-in-time build.
    """
    dates = [str(filing.get(field) or "").strip()
             for field in ("posted_date", "filed_date", "issued_date")]
    known = [value for value in dates if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)]
    return bool(known and max(known) > as_of)


def _mentions(legal_name: str, description: str) -> bool:
    """Does the FERC description actually name this filer?

    Legal suffixes and industry nouns do not distinguish neighbouring FERC
    filers. Requiring only a shared token such as ``MountainWest`` or
    ``Pipeline`` is how MountainWest Pipeline and MountainWest Overthrust can
    claim each other's reports. Where a name has only one distinctive token,
    require its complete non-suffix phrase in order (``mountainwest pipeline``
    does not occur in ``mountainwest overthrust pipeline``). Names with several
    distinctive tokens may still use FERC's reordered abbreviation style.
    """
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower())
    name = norm(legal_name).split()
    stop = {"company", "companies", "l", "p", "llc", "lp", "inc", "the",
            "corporation", "corp", "co", "partners", "holdings", "limited"}
    core = [w for w in name if w not in stop and len(w) > 2]
    if not core:
        return False
    industry = {"gas", "natural", "pipeline", "pipelines", "pipe", "line",
                "energy", "lng", "terminal", "storage", "transmission"}
    distinctive = [w for w in core if w not in industry] or core
    described_words = norm(description).split()
    if len(distinctive) == 1:
        phrase = " ".join(core)
        return f" {phrase} " in f" {' '.join(described_words)} "
    described = set(described_words)
    return all(w in described for w in distinctive)


RX_TARIFF_SUBMITTER = re.compile(
    r"^\s*(?P<actor>.+?)\s+submits\s+tariff\s+filing\b", re.I)
_TERMINAL_LEGAL_SUFFIXES = {
    "llc", "lp", "inc", "incorporated", "corporation", "corp", "company", "co",
    "limited", "plc",
}


def _legal_actor_tokens(value: str) -> Counter:
    """Normalise an actor while retaining every business-name qualifier."""
    value = re.sub(r"\bL\s*\.\s*L\s*\.\s*C\s*\.?", " LLC ", value or "", flags=re.I)
    value = re.sub(r"\bL\s*\.\s*P\s*\.?", " LP ", value, flags=re.I)
    words = re.findall(r"[a-z0-9]+", value.lower())
    while words and words[-1] in _TERMINAL_LEGAL_SUFFIXES:
        words.pop()
    return Counter(words)


def _tariff_submitter_matches(legal_name: str, filing_or_description) -> bool:
    """Match the complete source-declared submitter, not a token subset."""
    description = (filing_or_description.get("description")
                   if isinstance(filing_or_description, dict)
                   else filing_or_description) or ""
    match = RX_TARIFF_SUBMITTER.search(description)
    return bool(match and _legal_actor_tokens(match.group("actor"))
                == _legal_actor_tokens(legal_name))


def _is_tariff_submission(hit: dict) -> bool:
    return any(c == "Application/Petition/Request" and t == "Tariff Filing"
               for c, t in hit.get("class_pairs", ()))


def _target_owned_hit(legal_name: str, hit: dict) -> bool:
    if _is_tariff_submission(hit):
        return _tariff_submitter_matches(legal_name, hit)
    return _mentions(legal_name, hit.get("description") or "")


def _search(ctx, elib, key, tag, **kw) -> list[dict]:
    try:
        hits = elib.search(**kw)
    except FetchError as exc:
        ctx.staging.checkpoint(ADAPTER, key, tag, "failed", error=exc.detail)
        ctx.staging.open_blocker(ADAPTER, "source", f"{key}: search {tag} failed",
                                 scope=f"{key}:{tag}", attempts=str(exc.attempts),
                                 exact_error=exc.detail)
        ctx.log("error", f"{key} {tag}: {exc.detail}", adapter=ADAPTER, entity_cid=key)
        return []
    ctx.staging.checkpoint(ADAPTER, key, tag, "done")
    return hits


def _dockets_from(hits) -> list[str]:
    """Rate dockets, most recent first. Procedural-motion docket lists are
    excluded upstream by `links_assets`; here we additionally ignore dockets that
    appear on only one hit alongside twenty others."""
    seen: dict[str, str] = {}
    for h in sorted(hits, key=lambda x: x["filed_date"], reverse=True):
        if not elibrary.links_assets(h) or len(h["docket_bases"]) > 4:
            continue
        for d in h["docket_bases"]:
            if re.match(r"^(RP|IS|PR|OR|RM)\d", d):
                seen.setdefault(d, h["filed_date"])
    return [d for d, _ in sorted(seen.items(), key=lambda kv: kv[1], reverse=True)]


#: which orders actually state an effective date, most useful first. The
#: highest-RANKED order is not the most useful one to read: RP24-1035's
#: rank-10 order accepting the refund report is 1,435 characters and states no
#: effective date, while the rank-4 order placing the suspended rates into
#: effect is what sets one.
EFFECTIVE_ORDER_PRIORITY = ["rates_effective_subject_to_refund", "settlement_approved",
                           "settlement_rates_effective", "accepted_and_suspended",
                           "accepted", "refunds_accepted"]


#: A10: the refund window cannot be asserted from metadata, so the orders that
#: state its bounds have to be READ. Per swept docket, that is the suspension
#: order, the order placing the suspended rates into effect, and the order
#: accepting the refund report. The pre-repair cap of four documents per ENTITY
#: is what left both Transco suspension bodies unread -- and the delivered data
#: then dated both windows from the metadata instead.
REFUND_BOUND_STAGES = ("accepted_and_suspended", "rates_effective_subject_to_refund",
                       "refunds_accepted")
#: reserved slots per purpose. A22's failing gate test was, at bottom, a filer
#: whose tariff package never got a slot: `tariff_operative_rate` then reported a
#: retrieval gap forever and no amount of re-running could resolve it.
REFUND_BOUND_SLOTS = 4
PACKAGE_SLOTS = 2
EFFECTIVE_ORDER_SLOTS = 2
MAX_DOCUMENTS_PER_ENTITY = REFUND_BOUND_SLOTS + PACKAGE_SLOTS + EFFECTIVE_ORDER_SLOTS


def _documents_to_read(hits, relevant, seed_rows) -> list[dict]:
    """The documents whose BODIES a gated assertion needs.

    Ordered by what they unlock: refund-window bounds first (a window dated from
    an issuance date is the A10 defect), then an effective date, then the tariff
    packages.
    """
    scoped = [h for h in hits if not relevant or (set(h["docket_bases"]) & set(relevant))]
    # Slots are RESERVED per purpose rather than filled first-come. A single
    # overall cap let the refund-bound orders crowd out the tariff package for a
    # filer with several live dockets, and a purpose that never gets a slot
    # produces a permanently unresolvable gate.
    bounds: list[dict] = []
    for docket in (relevant or []):
        in_docket = [h for h in scoped if docket in h["docket_bases"]]
        if not any(h["classification"]["stage"] == "accepted_and_suspended"
                   for h in in_docket):
            continue                     # no suspension order: no refund window to bound
        for stage in REFUND_BOUND_STAGES:
            pool = [h for h in in_docket if h["classification"]["stage"] == stage
                    and h["classification"]["is_issuance"]]
            if pool:
                bounds.append(min(pool, key=lambda h: h["filed_date"]))
    orders = [h for h in scoped if h["classification"]["is_issuance"]
              and h["classification"]["stage"] in EFFECTIVE_ORDER_PRIORITY]
    orders.sort(key=lambda h: (EFFECTIVE_ORDER_PRIORITY.index(h["classification"]["stage"]),
                               -int(h["filed_date"].replace("-", "") or 0)))
    # Packages are chosen from ALL of this filer's hits, NOT from the two most
    # recent dockets. `_tariff_rate` picks the newest rate-setting package over
    # the whole canonical set, so restricting the read to `relevant` made the two
    # disagree: Southern Natural Gas's only 154.312 package, 20240501-5258 in
    # RP24-744, was named by the gate as "identified but not downloaded" while
    # never being eligible for download at all. That is the A22 failure, and it
    # would have survived any number of re-runs.
    packages = [h for h in hits if h["classification"]["stage"] == "filed"]
    seeded = {(r.get("accession") or "").strip() for r in seed_rows
              if r.get("proceeding_type") == "general_rate_case_application"}
    package_picks: list[dict] = []
    if packages:
        package_picks.append(max(packages, key=lambda h: h["filed_date"]))
    package_picks += [h for h in packages if h["accession"] in seeded][:1]

    seen, out = set(), []
    for group, cap in ((bounds, REFUND_BOUND_SLOTS), (package_picks, PACKAGE_SLOTS),
                       (orders, EFFECTIVE_ORDER_SLOTS)):
        taken = 0
        for h in group:
            if taken >= cap:
                break
            if h["accession"] in seen:
                continue
            seen.add(h["accession"])
            out.append(h)
            taken += 1
    return out[:MAX_DOCUMENTS_PER_ENTITY]


def _fetch_document(ctx, elib, key, filing) -> None:
    accession = filing["accession"]
    scope_key = f"doc:{accession}"
    ctx.staging.checkpoint(ADAPTER, key, scope_key, "in_progress")
    try:
        files = elib.file_list(accession)
    except FetchError as exc:
        _unretrieved(ctx, key, filing, scope_key, "GetFileListFromP8", exc)
        return
    public = [f for f in files if f["avail_code"] == elibrary.Avail.PUBLIC]
    filing["file_count"] = len(files)
    if not public:
        filing["retrieval"] = "nonpublic"
        filing["text"] = ""
        filing["text_layer"] = "no"
        ctx.staging.checkpoint(ADAPTER, key, scope_key, "done")
        return
    try:
        blob, entry = elib.download(
            accession, [f["attachment_id"] for f in public], expected_files=public)
    except FetchError as exc:
        _unretrieved(ctx, key, filing, scope_key, "DownloadP8File", exc)
        return
    parts = elibrary.members(blob, hint=f"{accession}.bin")
    # A general rate case package is enormous (Transco's RP24-1035 filing is 34
    # attachments and 130 MB of testimony exhibits). Text is extracted from the
    # members that can carry a tariff record -- the tariff sheets, the transmittal
    # and the eTariff-generated record -- plus the largest remaining few. Members
    # that were not read are recorded as such rather than silently dropped.
    ranked = sorted(parts, key=lambda p: (0 if RX_TARIFF_MEMBER.search(p[0]) else 1,
                                          -len(p[1])))
    read_names = {p[0] for p in ranked[:8]}
    texts = []
    for name, data, kind in parts:
        if name not in read_names or len(data) > 25_000_000:
            texts.append({"name": name, "text": "", "method": "not_extracted",
                          "layer": "unknown", "kind": kind, "bytes": len(data)})
            continue
        text, method, layer = elibrary.extract_text(data, name)
        texts.append({"name": name, "text": elibrary.flatten(text), "method": method,
                      "layer": layer, "kind": kind, "bytes": len(data)})
    best = max(texts, key=lambda t: len(t["text"])) if texts else None
    skipped = [t["name"] for t in texts if t["method"] == "not_extracted"]
    filing["members_not_read"] = skipped
    filing["retrieval"] = "retrieved"
    filing["members"] = texts
    filing["text"] = (best or {}).get("text", "")
    filing["text_layer"] = (best or {}).get("layer", "no")
    filing["extraction_method"] = (best or {}).get("method", "")
    filing["attachment_name"] = (best or {}).get("name", "")
    filing["attachment_id"] = public[0]["attachment_id"]
    filing["content_hash"] = entry["content_hash"]
    filing["media_type"] = entry["media_type"]
    filing["byte_size"] = entry["byte_size"]
    filing["document_first_seen_at"] = elibrary.cache_first_seen_at(entry)
    filing["document_retrieved_at"] = elibrary.cache_retrieved_at(entry)
    ctx.staging.checkpoint(ADAPTER, key, scope_key, "done")
    ctx.log("info", f"{accession}: {len(parts)} member(s); best '{filing['attachment_name']}' "
                    f"({filing['extraction_method']}, text_layer={filing['text_layer']}, "
                    f"{len(filing['text']):,} chars)", adapter=ADAPTER, entity_cid=key)


def _unretrieved(ctx, key, filing, scope_key, endpoint, exc) -> None:
    filing["retrieval"] = "failed"
    filing["text"] = ""
    filing["text_layer"] = "unknown"
    filing["retrieval_error"] = f"{endpoint}: {exc.detail} (status {exc.status}, " \
                                f"{exc.attempts} attempt(s))"
    ctx.staging.checkpoint(ADAPTER, key, scope_key, "failed", error=exc.detail)
    ctx.staging.open_blocker(
        ADAPTER, "source", f"{filing['accession']}: {endpoint} failed; document not parsed",
        scope=f"{key}:{filing['accession']}", attempts=str(exc.attempts),
        exact_error=f"{endpoint} {exc.detail} status={exc.status}")
    ctx.log("error", f"{filing['accession']} {endpoint}: {exc.detail}",
            adapter=ADAPTER, entity_cid=key)


def _persist(ctx, entity, filing, *, baseline=None) -> None:
    if _is_tariff_submission(filing) and not _tariff_submitter_matches(
            entity.get("legal_name") or "", filing):
        raise ValueError(
            f"{filing.get('accession')}: tariff submitter does not match "
            f"{entity.get('legal_name')!r}")
    accession = filing["accession"]
    capture = elibrary._merge_occurrence_capture_times(
        {"first_seen_at": filing.get("first_seen_at"),
         "retrieved_at": filing.get("retrieved_at")},
        {"first_seen_at": filing.get("document_first_seen_at"),
         "retrieved_at": filing.get("document_retrieved_at")})
    filing["first_seen_at"] = (
        baseline.first_seen(accession, capture.get("first_seen_at"))
        if baseline else capture.get("first_seen_at"))
    filing["retrieved_at"] = capture.get("retrieved_at")
    year = int(filing["filed_date"][:4]) if filing.get("filed_date") else None
    doc_id = f"{SOURCE_SYSTEM}|{accession}|{filing.get('attachment_id') or 'listing'}"
    # A11: byte-identical content under a DIFFERENT accession is an identical
    # resubmission -- a real occurrence and a version record, never an economic
    # change. Content identity deduplicates BYTES; it never merges occurrences.
    if filing.get("content_hash"):
        version_status, supersedes = ctx.staging.classify_version(
            SOURCE_SYSTEM, entity["entity_key"], FORM, year or 0, "as_of",
            accession, filing["content_hash"])
    else:
        version_status, supersedes = VersionStatus.ORIGINAL, None
    filing["version_status"] = version_status
    availability = ("nonpublic" if filing.get("avail_code") in ("N", "C")
                    else {"retrieved": "retrieved", "failed": "failed"}.get(
                        filing.get("retrieval", ""), "not_retrieved"))
    row = {
        "source_system": SOURCE_SYSTEM, "filing_id": accession,
        "entity_key": entity["entity_key"], "form": FORM, "accession_number": accession,
        "reporting_year": year, "reporting_period": "as_of",
        "period_start": None, "period_end": None,
        "filed_date": filing.get("filed_date") or None,
        "posted_date": filing.get("posted_date") or None,
        "issued_date": filing.get("issued_date") or None,
        "effective_date": _filer_effective(filing) or None,
        "submitted_on": filing.get("filed_date") or None, "snapshot_date": None,
        "acceptance_status": elibrary.Avail.LABEL.get(filing.get("avail_code", ""), "unknown"),
        "taxonomy_version": None, "schema_ref": None,
        "content_hash": filing.get("content_hash"),
        "is_canonical": 0 if filing.get("is_twin") else 1,
        "canonical_reason": (f"availability twin of {filing.get('twin_of')}; excluded from "
                             "event counting" if filing.get("is_twin")
                             else filing["classification"]["label"]),
        "version_status": version_status, "supersedes_filing_id": supersedes,
        "data_origin": "document",
        "retrieved_at": filing.get("retrieved_at") or None,
        "first_seen_at": filing.get("first_seen_at") or None,
        "source_url": elibrary.docinfo_url(accession),
    }
    documents = [{
        "document_id": doc_id, "source_system": SOURCE_SYSTEM, "filing_id": accession,
        "accession_number": accession, "attachment_id": filing.get("attachment_id") or "",
        "title": filing.get("attachment_name") or filing.get("description", "")[:200],
        "class_type": "|".join(filing.get("class_types") or []),
        "media_type": filing.get("media_type") or "",
        "byte_size": filing.get("byte_size"), "content_hash": filing.get("content_hash"),
        "cache_path": None, "text_layer": filing.get("text_layer") or "unknown",
        "availability": availability,
        "retrieved_at": (filing.get("document_retrieved_at")
                         or filing.get("retrieved_at") or None),
        "source_url": elibrary.filelist_url(accession)}]
    dockets = [{"source_system": SOURCE_SYSTEM, "filing_id": accession, "docket": d}
               for d in sorted(set(filing.get("dockets") or []))]
    is_issuance = bool(filing.get("classification", {}).get("is_issuance"))
    association = {
        "entity_key": entity["entity_key"],
        "association_role": ("commission_docket_subject" if is_issuance else
                             "named_filer" if _is_tariff_submission(filing) else
                             "source_entity"),
        "facility_key": "",
        "evidence_ref": (
            f"eLibrary:{accession};description_sha256="
            f"{hashlib.sha256((filing.get('description') or '').encode()).hexdigest()};"
            f"dockets={','.join(sorted(filing.get('dockets') or []))}"),
    }
    ctx.staging.write_filing_bundle(
        row, documents=documents, filing_dockets=dockets,
        filing_entities=[association], allow_shared_entities=is_issuance)
    filing["document_id"] = doc_id
    # ``retrieve`` returns these dictionaries to the run-level, append-only
    # input inventory and input-digest boundary.  Preserve the raw API fields,
    # but also expose the canonical occurrence keys expected by that boundary;
    # otherwise every eLibrary occurrence is recorded with a blank filing and
    # source identity and content changes cannot invalidate resume correctly.
    filing.update({
        "source_system": row["source_system"],
        "filing_id": row["filing_id"],
        "accession_number": row["accession_number"],
        "submitted_on": row["submitted_on"],
        "form": row["form"],
        "reporting_year": row["reporting_year"],
        "reporting_period": row["reporting_period"],
        "snapshot_date": row["snapshot_date"],
        "is_canonical": row["is_canonical"],
    })


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _filer_effective(filing) -> str:
    m = RX_FILER_EFFECTIVE.search(filing.get("description") or "")
    if not m:
        return ""
    d = m.group(1).split("/")
    return f"{int(d[2]):04d}-{int(d[0]):02d}-{int(d[1]):02d}"


# ---------------------------------------------------------------- classify

def classify(hit: dict) -> dict:
    """What this document IS, from its description. Never from its recency."""
    d = hit["description"]
    is_issuance = any(c in ("Order/Opinion", "ALJ Issuance", "Notice",
                            "FERC Correspondence With Applicant")
                      for c, _t in hit["class_pairs"])
    stage, why = "", ""
    if is_issuance:
        for rx, st in ISSUANCES:
            if rx.search(d):
                stage, why = st, f"issuance wording: '{rx.pattern[:52]}'"
                break
        if not stage and RX_PETITION_REVIEW.search(d):
            stage, why = "under_judicial_review", "petition for review"
        if not stage:
            stage, why = "other_issuance", "FERC issuance, decision wording not matched"
    if RX_PETITION_REVIEW.search(d) and not stage:
        stage, why = "under_judicial_review", "petition for review"
    if not stage:
        for rx, st, label in REGULATIONS:
            if rx.search(d):
                stage, why = st, label
                break
        if stage == "refunds_reported" and RX_SUPPLEMENT.search(d):
            stage, why = "refund_report_supplement", "supplement to a refund report"
    if not stage and any(t == "Complaints" for _c, t in hit["class_pairs"]):
        stage, why = "complaint_filed", "Application/Petition/Request::Complaints"
    if not stage:
        stage, why = "other_submittal", "no regulation or decision wording matched"
    return {
        "stage": stage, "is_issuance": is_issuance, "why": why,
        "label": LABEL.get(stage) or NON_ADVANCING.get(stage, stage),
        "evidence_class": SOURCED if is_issuance else INFERRED,
        "advancing": stage in RANK,
    }


def _proceedings(filings, relevant, legal_name="") -> dict[str, list[dict]]:
    """Group canonical filings by base docket, restricted to the proceedings this
    run actually swept.

    Without the restriction a name-led census over a decade turns every one of
    Magellan's ~190 index dockets into a reported proceeding, most of them with
    no order retrieved and therefore no defensible stage.
    """
    keep = set(relevant or [])
    out: dict[str, list[dict]] = {}
    for f in filings:
        if f.get("is_twin") or not elibrary.links_assets(f):
            continue
        for d in f["docket_bases"]:
            if keep and d not in keep:
                continue
            out.setdefault(d, []).append(f)
    # A docket reached only through a SHARED order belongs to another company.
    # Magellan's acceptance order 20241018-3061 spans six IS dockets for six
    # carriers; reporting IS24-804 as a Magellan proceeding would attribute
    # Calnev's docket to Magellan.
    if legal_name:
        for d in list(out):
            if not any(_mentions(legal_name, f["description"]) for f in out[d]):
                out.pop(d)
    return out


# ---------------------------------------------------------------- expected

def _metrics(template: str):
    return [m for m in BY_ADAPTER[ADAPTER] if template in m.templates]


def freeze_expected(ctx, entity, filings, assets) -> list[dict]:
    key, template = entity["entity_key"], entity["template"]
    asset_id = assets[0]["asset_id"] if assets else ""
    year = getattr(ctx.args, "year_to", 2026)
    out, seen = [], set()
    for m in _metrics(template):
        if m.role == "gated":
            requirement, evidence = coverage.CONDITIONAL, (
                "GATED: a numeric operative rate is published only when the tariff record, "
                "its scope, its effective status and the governing orders reconcile. "
                "The tariff sheets themselves ARE eLibrary attachments, so retrieval is "
                "implemented and the gate decides per filer.")
        elif m.id == "refund_exposure_window":
            requirement, evidence = coverage.CONDITIONAL, (
                "a refund window exists only where an order accepted and SUSPENDED rates "
                "subject to refund; an outright acceptance starts no refund clock")
        elif m.id in ("event_reportable_interruption", "event_replacement_report"):
            requirement, evidence = coverage.CONDITIONAL, (
                "reported only when the filer files one; the FERC class/type route was "
                "verified live (260.9 Service Interruption Report: 6 records industry-wide "
                "2015-2026; 2.55/157.207/284.11 Annual Construction Report: 7 records for "
                "Transco 2024-2026), so a zero count for one filer is a source blank")
        elif m.id == "liq_oil_price_index":
            requirement, evidence = coverage.CONDITIONAL, (
                "an industry-wide published FERC index, not a carrier rate; requested once "
                "per carrier and resolved from the FERC issuance that publishes it")
        else:
            requirement, evidence = coverage.REQUIRED, (
                "every FERC-jurisdictional rate filer has a rate authority of some kind; "
                "where no proceeding exists that is itself a reportable state, not a gap")
        slot = coverage.build_expected(
            key, asset_id, template, m, REGIME, periods.AS_OF, year, "Q4",
            requirement, evidence, "elibrary-classtypes-2026-09-07", ctx.staging.run_id)
        if slot["slot_id"] not in seen:
            seen.add(slot["slot_id"])
            out.append(slot)
    return out


# ---------------------------------------------------------------- observations

def _obs(entity_key, m, *, instant, value_text, value_num=None, unit=None,
         availability=Availability.PRESENT, method=Method.DOCUMENT_EXTRACTED,
         validation=Validation.PASS, version_status=VersionStatus.ORIGINAL,
         scope="", qa_flags="", missing_reason="", accession="", document_id=None,
         selector="document_span", notes="", evidence="", normalized_iso="") -> dict:
    scope = scope or m.scope
    year = int(instant[:4]) if instant[:4].isdigit() else 0
    return {
        "observation_id": observation_id(entity_key, m.id, REGIME, periods.AS_OF, "", "",
                                         instant, scope, unit or "", method),
        "entity_key": entity_key, "metric_id": m.id, "source_regime": REGIME,
        "period_basis": periods.AS_OF, "period_start": None, "period_end": None,
        "instant_date": instant or None, "reporting_year": year or None,
        "reporting_period": "as_of", "period_label": instant or "as of (undated)",
        # Keep the filed/legal scope for consumers and the independent registry
        # acceptance contract for coverage.  Concatenating docket/facility
        # evidence onto actual scope must not make an otherwise valid record
        # impossible to match to its requirement.
        "scope": scope, "scope_rule": m.scope,
        "unit": unit, "value_text": value_text, "value_num": value_num,
        "normalized_iso": normalized_iso or None,
        "availability": availability, "origin": Origin.ELIBRARY_DOCUMENT, "method": method,
        "version_status": version_status, "validation": validation,
        "source_system": SOURCE_SYSTEM, "filing_id": accession or None,
        "source_fact_id": None, "source_context_id": None,
        "document_id": document_id, "accession_number": accession or None,
        "candidate_count": None, "selector": selector, "derivation": "",
        "concept_local": None, "concept_qname": None, "taxonomy_version": None,
        "registry_version": REGISTRY_VERSION,
        "applicability_version": "elibrary-classtypes-2026-09-07",
        "schedule_page": m.schedule or "", "taxonomy_label": None,
        "qa_flags": qa_flags, "review_status": "", "missing_reason": missing_reason,
        "applicability_evidence": evidence, "notes": notes,
    }


def canonicalise(ctx, entity, filings, expected) -> tuple[list, list]:
    key, template = entity["entity_key"], entity["template"]
    metrics = {m.id: m for m in _metrics(template)}
    obs: list[dict] = []
    docfacts: list[dict] = []
    events: list[dict] = []
    seed_rows = (filings[0].get("_seed_rows") if filings else []) or []
    canonical = [f for f in filings if not f.get("is_twin")]
    relevant = (filings[0].get("_relevant") if filings else []) or []
    procs = _proceedings(canonical, relevant, entity["legal_name"])
    baseline = next((f.get("_baseline") for f in filings if f.get("_baseline")), None) \
        or elibrary.NewsBaseline(ctx.staging, ADAPTER, key, SOURCE_SYSTEM)

    if "rate_case_status" in metrics:
        obs += _rate_case_status(key, entity, procs, seed_rows, docfacts)
    if "rate_effective_date" in metrics:
        obs += _rate_effective_date(key, procs, docfacts)
    if "refund_exposure_window" in metrics:
        obs += _refund_window(key, procs, docfacts)
    if "tariff_operative_rate" in metrics:
        obs += _tariff_rate(key, procs, canonical, docfacts)
    if "liq_oil_price_index" in metrics:
        obs += _oil_index(ctx, key)
    if "event_reportable_interruption" in metrics:
        obs += _report_metric(key, canonical, "event_reportable_interruption",
                              INTERRUPTION_TYPES,
                              "260.9 Service Interruption Report", docfacts, events,
                              entity["assets"],
                              "FERC-only interruption reports are NOT comprehensive "
                              "availability statistics: 18 CFR 260.9 covers INTERSTATE GAS "
                              "service interruptions reported to FERC and nothing else",
                              baseline=baseline)
    if "event_replacement_report" in metrics:
        obs += _report_metric(key, canonical, "event_replacement_report", REPLACEMENT_TYPES,
                              "2.55 / 157.207 / 284.11 Annual Construction Report", docfacts,
                              events, entity["assets"],
                              "a routine annual construction/replacement report is NOT "
                              "maintenance-capex data and does not by itself establish "
                              "financial materiality", baseline=baseline)

    events += _rate_events(key, entity, canonical, relevant, baseline=baseline)
    obs += _account_for_remainder(key, metrics, expected, obs)
    if docfacts:
        ctx.staging.write_document_facts(docfacts)
    if events and obs:
        obs[0]["_events"] = events
    ctx.log("info", f"{key}: {len(obs)} observations, {len(docfacts)} document facts, "
                    f"{len(events)} events, {len(procs)} proceeding(s)",
            adapter=ADAPTER, entity_cid=key)
    return obs, []


# ---------------------------------------------------------------- status

def _stage_of(docs) -> tuple[str, dict | None, dict | None]:
    """(stage, the document that established it, the newest document).

    The stage is the highest ORDER-BACKED rank reached, which is why the newest
    document is returned separately: in RP24-1035 the newest is a refund-report
    supplement and in RP25-1189 it is a D.C. Circuit petition.
    """
    # At equal rank the EARLIEST order established the state. TGP's operative
    # rate authority is the 2019-05-24 settlement order, not the 2022 compliance
    # letter that merely cites it -- picking the latest at equal rank would
    # silently restate a seven-year-old legal date as a recent one.
    def _pick(pool):
        if not pool:
            return None
        top = max(RANK[d["classification"]["stage"]] for d in pool)
        same = [d for d in pool if RANK[d["classification"]["stage"]] == top]
        return min(same, key=lambda d: d["filed_date"])

    best = _pick([d for d in docs if d["classification"]["advancing"]
                  and d["classification"]["is_issuance"]])
    if best is None:
        best = _pick([d for d in docs if d["classification"]["advancing"]])
    newest = max(docs, key=lambda d: d["filed_date"], default=None)
    return (best["classification"]["stage"] if best else ""), best, newest


def _governing(docs) -> dict | None:
    """The order that actually SET the rates, as distinct from the furthest
    procedural stage. TGP's rates run off the 2019-05-24 settlement approval even
    though the docket has since reached 'settlement rates accepted and
    effective' three more times."""
    for stage in ("settlement_approved", "accepted_and_suspended", "accepted"):
        pool = [d for d in docs if d["classification"]["stage"] == stage
                and d["classification"]["is_issuance"]]
        if pool:
            return min(pool, key=lambda d: d["filed_date"])
    return None


def _rate_case_status(key, entity, procs, seed_rows, docfacts) -> list[dict]:
    m = BY_ID["rate_case_status"]
    out = []
    if not procs:
        negatives = [r for r in seed_rows if not r.get("accession")]
        reason = ("; ".join(r["description"][:300] for r in negatives) if negatives else
                  "no rate docket was located for this filer in the search window; the "
                  "name-led Tariff Filing census returned nothing. That is 'not located', "
                  "not 'no source exists'")
        return [_obs(key, m, instant="", value_text=None, unit="categorical",
                     availability=Availability.EXPECTED_NOT_LOCATED,
                     validation=Validation.NOT_YET_VALIDATED, missing_reason=reason)]
    for docket, docs in sorted(procs.items(),
                               key=lambda kv: max(d["filed_date"] for d in kv[1]),
                               reverse=True):
        stage, src, newest = _stage_of(docs)
        if not stage:
            continue
        cls = src["classification"]
        newest_cls = newest["classification"]
        gov = _governing(docs)
        no_action = (stage == "filed" and not cls["is_issuance"])
        qa = [f"stage = the highest ORDER-BACKED stage reached in {docket}, established by "
              f"{src['accession']} ({src['filed_date']}); NOT the newest document",
              f"newest document in {docket} is {newest['accession']} ({newest['filed_date']}): "
              f"{newest_cls['label']}" +
              ("" if newest_cls["advancing"] and RANK.get(newest_cls["stage"], 0) >= RANK[stage]
               else " -- it does NOT supersede the stage above"),
              f"evidence class: {cls['evidence_class']} ({cls['why']})"]
        if gov is not None and gov["accession"] != src["accession"]:
            qa.append(f"GOVERNING RATE AUTHORITY: {gov['accession']} ({gov['filed_date']}) "
                      f"-- {gov['classification']['label']}. Later compliance filings "
                      "implement it and do not replace it")
        if no_action:
            qa.append("NO Commission order and no letter order was located in this docket: "
                      "whether the filing was granted, and by what instrument, is "
                      "UNRESOLVED from eLibrary")
        a, b, verbatim = elibrary.span(src["description"], 0, len(src["description"]), pad=0)
        out.append(_obs(
            key, m, instant=src["filed_date"],
            value_text=f"{docket}: {LABEL[stage]}", unit="categorical",
            availability=Availability.PRESENT, normalized_iso=src["filed_date"],
            scope=f"{m.scope} | docket {docket}",
            accession=src["accession"], document_id=src.get("document_id"),
            validation=Validation.PASS if cls["evidence_class"] == SOURCED
            else Validation.NOT_YET_VALIDATED,
            qa_flags="; ".join(qa),
            missing_reason=("no Commission action located in this docket" if no_action else ""),
            evidence=f"{cls['evidence_class']}: {cls['why']}"))
        docfacts.append(_docfact(key, src, "rate_case_stage", "rate_case_status",
                                 f"{docket}: {stage}", None, "categorical",
                                 cls["evidence_class"], f"docket {docket}", a, b, verbatim,
                                 "elibrary_description_span",
                                 "sourced_order" if cls["evidence_class"] == SOURCED
                                 else "metadata_only"))
    # affirmative negative findings carried from the verified discovery pass
    for r in seed_rows:
        if r.get("accession") or not (r.get("description") or "").strip():
            continue
        out.append(_obs(
            key, m, instant="", value_text=f"{r.get('proceeding_type')}: NONE LOCATED",
            unit="categorical", availability=Availability.NOT_APPLICABLE,
            scope=f"{m.scope} | {r.get('docket') or 'no docket'} | "
                  f"{r.get('proceeding_type')} | affirmative negative finding",
            # A supplementary negative finding is never the entity's headline
            # stage: it is carried from the verified discovery pass and was not
            # re-established by this run's searches, so it stays
            # not_yet_validated and its own evidence and confidence travel with it.
            validation=Validation.NOT_YET_VALIDATED,
            missing_reason=r["description"][:900],
            qa_flags=f"stage_evidence={r.get('stage_evidence')}; "
                     f"confidence={r.get('confidence')}; SUPPLEMENTARY affirmative-negative "
                     "finding carried from the verified discovery pass of 2026-09-07 -- a "
                     "VALID STATE established affirmatively, not missing data, and not this "
                     "entity's headline proceeding stage"))
    return out


# ---------------------------------------------------------------- effective date

def _rate_effective_date(key, procs, docfacts) -> list[dict]:
    m = BY_ID["rate_effective_date"]
    out = []
    for docket, docs in procs.items():
        stage, src, _newest = _stage_of(docs)
        if src is None:
            continue
        read = [d for d in docs if d.get("text")]
        sourced = None
        for d in read:
            if not d["classification"]["is_issuance"]:
                continue
            hit = RX_ORDER_EFFECTIVE.search(d["text"])
            if hit:
                sourced = (d, hit)
                break
        if sourced:
            d, hit = sourced
            a, b, verbatim = elibrary.span(d["text"], hit.start(), hit.end())
            iso = _iso_any(hit.group(1))
            out.append(_obs(
                key, m, instant=d["filed_date"], value_text=hit.group(1), unit="(date)",
                normalized_iso=iso, availability=Availability.PRESENT,
                scope=f"{m.scope} | docket {docket} | from ORDER TEXT",
                accession=d["accession"], document_id=d.get("document_id"),
                qa_flags=f"extracted from the ORDER TEXT of {d['accession']}; "
                         f"evidence class {SOURCED}",
                notes=f"span[{a}:{b}]", evidence=f"order {d['accession']} text"))
            docfacts.append(_docfact(key, d, "rate_effective_date", "rate_effective_date",
                                     hit.group(1), None, "(date)", "",
                                     f"docket {docket} | order text", a, b, verbatim,
                                     d.get("extraction_method", ""), "sourced_order"))
            continue
        stated = [d for d in docs if _filer_effective(d)]
        if stated:
            d = max(stated, key=lambda x: x["filed_date"])
            iso = _filer_effective(d)
            a, b, verbatim = elibrary.span(d["description"], 0, len(d["description"]), pad=0)
            docfacts.append(_docfact(key, d, "filer_stated_effective_date",
                                     "rate_effective_date", iso, None, "(date)",
                                     "filer-stated, not adjudicated",
                                     f"docket {docket} | eTariff description", a, b, verbatim,
                                     "elibrary_description_span", "metadata_only"))
            out.append(_obs(
                key, m, instant=d["filed_date"], value_text=None, unit="(date)",
                availability=Availability.UNVERIFIED_AVAILABILITY,
                validation=Validation.NOT_YET_VALIDATED,
                scope=f"{m.scope} | docket {docket} | filer-stated only",
                accession=d["accession"], document_id=d.get("document_id"),
                missing_reason=(f"the only date located for {docket} is the FILER'S OWN "
                                f"'to be effective {iso}' in the eTariff description "
                                f"({d['accession']}). The registry gate requires order text; "
                                f"a metadata inference is {INFERRED} and is NOT published as "
                                "a legal effective date. The retrieved value is retained as "
                                "a document fact."),
                qa_flags=f"filer-stated effective date retained: {iso}; evidence class "
                         f"{INFERRED}"))
            continue
        out.append(_obs(
            key, m, instant="", value_text=None, unit="(date)",
            availability=Availability.KNOWN_NOT_RETRIEVED,
            validation=Validation.NOT_YET_VALIDATED,
            scope=f"{m.scope} | docket {docket}",
            missing_reason=(f"the governing order for {docket} is identified "
                            f"({src['accession']}) but its text was not retrieved in this "
                            "run, so no effective date is asserted. OUR retrieval gap.")))
    return out


def _iso_any(s: str) -> str:
    s = s.strip()
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    try:
        return dt.datetime.strptime(s.replace(",", ""), "%B %d %Y").date().isoformat()
    except ValueError:
        return ""


# ---------------------------------------------------------------- refund window

def _refuse_dollars(text: str) -> str:
    """Structural guard for rule 4: this metric identifies a WINDOW.

    A dollar figure would need a sourced calculation of the affected rates and
    volumes, which this adapter does not have, so it is refused at the point of
    construction rather than left to reviewer discipline.
    """
    if text and RX_DOLLARS.search(text):
        raise ValueError("refund_exposure_window refuses to publish a monetary amount "
                         "without a sourced calculation of affected rates and volumes")
    return text


#: A10. The dates in a rate proceeding have DIFFERENT LEGAL ROLES and only one
#: of them opens a refund window. The stored data conflated two of them:
#: Targa IS26-24's window began on 2025-11-26, which is the day the order ISSUED,
#: while the order's own ordering paragraph reads "Targa's Tariff No. 1.0.0 is
#: accepted and suspended, to become effective December 1, 2025, subject to
#: refund". Collections before 1 December are not subject to that refund
#: obligation, because there were no rates in effect to collect under.
ROLE_ISSUED = "order_issued"
ROLE_TARIFF_FILED = "tariff_filed"
ROLE_PROPOSED_EFFECTIVE = "proposed_effective"
ROLE_SUSPENSION_ORDERED = "suspension_ordered"
ROLE_REFUND_EFFECTIVE = "rates_effective_subject_to_refund"
ROLE_REFUND_CLOSED = "refund_obligation_closed"

#: "accepted and suspended, to become effective December 1, 2025, subject to
#: refund" / "accepted and suspended effective March 1, 2019, subject to refund"
#: / "accepted and suspended for five months, to be effective upon motion
#: March 1, 2025, subject to refund". All three are real FERC wordings from the
#: three orders behind the three stored windows.
_DATE = (r"(?:January|February|March|April|May|June|July|August|September|October|"
         r"November|December)\s+\d{1,2},\s*\d{4}|\d{1,2}/\d{1,2}/\d{4}")
RX_SUSPENDED_EFFECTIVE = re.compile(
    r"accepted\s+and\s+suspended[^.]{0,180}?"
    r"(?:to\s+become\s+effective|to\s+be\s+effective(?:\s+upon\s+motion)?|effective)\s+"
    rf"({_DATE})[^.]{{0,160}}?subject\s+to\s+refund", re.I)
#: The legal-status half of a two-order chain.  Some suspension orders make the
#: rates effective only "upon motion" and therefore carry no calendar date.
#: They still prove that the later-effective rates remain subject to refund.
RX_SUSPENSION_SUBJECT_REFUND = re.compile(
    r"(?:accepted\s+and\s+suspended|suspending\s+(?:the\s+)?"
    r"(?:rates?|tariff\s+records?))[^.]{0,320}?\bsubject\s+to\s+refund\b", re.I)
#: the motion order that actually puts the suspended rates into effect:
#: "the tariff records listed in Appendix A are accepted, effective March 1, 2025"
RX_MOTION_EFFECTIVE = re.compile(
    rf"tariff\s+records?[^.]{{0,120}}?are\s+accepted,?\s+effective\s+({_DATE})"
    rf"|Tariff\s+Records?\s+Accepted\s+Effective\s+({_DATE})"
    rf"|accepted\s+effective\s+({_DATE})", re.I)
#: The later order must itself say that the records/rates whose effective date
#: it fixes are the suspended ones.  A free-standing acceptance with a date is
#: not enough to inherit the earlier order's subject-to-refund status.
RX_SUSPENDED_RATE_REFERENCE = re.compile(
    r"(?:rates?|tariff\s+records?)[^.]{0,180}?\bsuspended\b"
    r"|\bsuspended\b[^.]{0,180}?(?:rates?|tariff\s+records?)", re.I)
#: an order accepting the refund report. Here the ISSUANCE date IS the operative
#: date, because acceptance is an act performed on the day the order issues --
#: which is exactly why the role has to be recorded rather than assumed.
RX_REFUND_ACCEPTED = re.compile(
    r"report\s+of\s+refunds[^.]{0,200}?is\s+accepted"
    r"|refund\s+report(?:\s+as\s+supplemented)?\s+is\s+accepted", re.I)
#: Acceptance of a report for informational purposes proves only that FERC
#: accepted that filing.  It does not, without more, say that every refund
#: obligation is discharged.  A window is therefore closed only by explicit
#: operative closure language in the order body.
RX_REFUND_OBLIGATION_CLOSED = re.compile(
    r"(?:refund\s+obligations?|obligation\s+to\s+(?:make|pay|issue)\s+refunds?)"
    r"[^.]{0,160}?\b(?:satisfied|discharged|fulfilled|completed|closed)\b", re.I)
#: The order's own dateline: "Issued: April 28, 2026", "(Issued November 26,
#: 2025)". Where a role's operative date IS the issuance date -- acceptance of a
#: refund report takes effect on the day the order issues -- this is the passage
#: that establishes it. The eLibrary metadata is NOT: a filing date is a property
#: of the record, and the whole of A10 is that a date must come from text that
#: states it.
RX_ISSUED_DATE = re.compile(rf"\(?\s*Issued:?\s+({_DATE})", re.I)


def _boundary(docs, roles, docfacts, key, docket, m) -> dict | None:
    """One refund-window boundary, resolved to the passage that establishes it.

    Returns ``{iso, role, accession, span, verbatim, basis}`` or None. A boundary
    that cannot be resolved to text is NOT invented from metadata: the caller
    reports the window as unvalidated instead.
    """
    candidates = []
    seen = set()
    for role_rank, (role, stage, patterns, requires) in enumerate(roles):
        # Inspect every eligible issuance.  Choosing the earliest filed record
        # before reading its body made a later, operative motion order invisible.
        pool = [d for d in docs if d.get("classification", {}).get("stage") == stage
                and d.get("classification", {}).get("is_issuance")]
        for doc in pool:
            text = doc.get("text") or ""
            if not text:
                continue                # no body, no bound. There is no metadata route.
            required_hits = list(requires.finditer(text)) if requires is not None else []
            if requires is not None and not required_hits:
                continue                # the body does not do what this role claims
            for pattern_rank, rx in enumerate(patterns):
                for hit in rx.finditer(text):
                    raw = next((g for g in hit.groups() if g), "")
                    iso = _iso_any(raw)
                    if not iso:
                        continue
                    required_hit = None
                    if required_hits:
                        # Cite the closest qualifying operative passage when an
                        # order contains more than one historical recital.
                        def distance(other):
                            if other.end() < hit.start():
                                return hit.start() - other.end()
                            if hit.end() < other.start():
                                return other.start() - hit.end()
                            return 0
                        required_hit = min(required_hits,
                                           key=lambda other: (distance(other), other.start()))
                    support_start = (min(hit.start(), required_hit.start())
                                     if required_hit else hit.start())
                    support_end = (max(hit.end(), required_hit.end())
                                   if required_hit else hit.end())
                    a, b, verbatim = elibrary.span(text, support_start, support_end, pad=110)
                    # The structural guard. All three delivered spans were the
                    # eLibrary heading and contained neither date; nothing may be
                    # stored that way again.
                    elibrary.require_span_support(
                        verbatim, iso, what=f"refund window {role}")
                    identity = (role_rank, role, doc.get("accession") or "", iso, a, b)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    candidates.append({
                        "iso": iso, "role": role,
                        "accession": doc.get("accession") or "",
                        "span": (a, b), "verbatim": verbatim, "doc": doc,
                        "role_rank": role_rank, "pattern_rank": pattern_rank,
                    })
    if not candidates:
        return None

    # `roles` is a legal-evidence priority supplied by the caller.  Within that
    # lane the operative/effective date establishes the boundary; filed_date is
    # metadata and is deliberately not a selection criterion.  The remaining
    # source identifiers make equal-date replay deterministic.
    chosen = min(candidates, key=lambda c: (
        c["role_rank"], c["iso"], c["accession"],
        c["doc"].get("document_id") or "", c["pattern_rank"], c["span"][0]))
    doc = chosen["doc"]
    a, b = chosen["span"]
    docfacts.append(_docfact(
        key, doc, f"refund_window_{chosen['role']}", m.id, chosen["iso"], None,
        "(date)", f"legal role: {chosen['role']}",
        f"docket {docket} | ORDER TEXT of {chosen['accession']}",
        a, b, chosen["verbatim"],
        doc.get("extraction_method", "") or "document_span",
        "sourced_order_text", stable_key=f"{docket}|{chosen['role']}"))
    chosen["candidate_count"] = len(candidates)
    chosen["basis"] = (f"{chosen['accession']} order text, span[{a}:{b}]; selected "
                       f"by legal-role priority then operative date from "
                       f"{len(candidates)} supported candidate(s)")
    return chosen


def _refund_suspension_support(docs) -> dict | None:
    """A sourced order passage establishing the subject-to-refund predicate."""
    candidates = []
    for doc in docs:
        cls = doc.get("classification", {})
        if (cls.get("stage") != "accepted_and_suspended"
                or not cls.get("is_issuance")):
            continue
        text = doc.get("text") or ""
        for hit in RX_SUSPENSION_SUBJECT_REFUND.finditer(text):
            a, b, verbatim = elibrary.span(text, hit.start(), hit.end(), pad=110)
            elibrary.require_span_support(
                verbatim, "subject to refund",
                what="refund window subject-to-refund suspension")
            candidates.append({"doc": doc, "accession": doc.get("accession") or "",
                               "span": (a, b), "verbatim": verbatim})
    if not candidates:
        return None
    # This chooses a source occurrence, not a legal boundary.  Accession and
    # document identity provide stable replay ordering without turning a filing
    # date into the date on which the refund window began.
    return min(candidates, key=lambda c: (
        c["accession"], c["doc"].get("document_id") or "", c["span"][0]))


def _refund_window(key, procs, docfacts) -> list[dict]:
    m = BY_ID["refund_exposure_window"]
    out = []
    for docket, docs in procs.items():
        susp = [d for d in docs
                if d["classification"]["stage"] == "accepted_and_suspended"]
        if not susp:
            continue
        suspension_support = _refund_suspension_support(docs)
        start_doc = (suspension_support["doc"] if suspension_support else
                     min(susp, key=lambda d: (d.get("accession") or "",
                                              d.get("document_id") or "")))
        effective = [d for d in docs
                     if d["classification"]["stage"] == "rates_effective_subject_to_refund"
                     and d["classification"]["is_issuance"]]
        eff_doc = min(effective, key=lambda d: (d.get("accession") or "",
                                                d.get("document_id") or "")) if effective else None
        closed = [d for d in docs if d["classification"]["stage"] == "refunds_accepted"]
        end_doc = max(closed, key=lambda d: (d.get("accession") or "",
                                             d.get("document_id") or "")) if closed else None

        # START: the day rates became collectible SUBJECT TO REFUND.  The passage
        # must state both the effective date and that legal status.  A later
        # motion order that merely says "accepted effective March 1" proves the
        # date but not, by itself, the subject-to-refund role; the separately
        # cited suspension-order passage is required.
        start = None
        if suspension_support is not None:
            support_doc = suspension_support["doc"]
            support_order = (support_doc.get("filed_date") or "",
                             support_doc.get("accession") or "")
            # A motion/order can complete the two-document chain only after the
            # sourced suspension.  filed_date is used solely to establish source
            # chronology here; the published boundary still comes from the
            # operative date written in an order body.
            boundary_docs = [
                d for d in docs
                if (d["classification"]["stage"] != "rates_effective_subject_to_refund"
                    or ((d.get("filed_date") or "", d.get("accession") or "")
                        > support_order))]
            start = _boundary(
                boundary_docs,
                [
                 # Prefer the order that actually accepts the motion and puts
                 # the suspended rates into effect.  Its body must identify the
                 # rates as suspended; the earlier sourced order supplies the
                 # subject-to-refund legal status.
                 (ROLE_REFUND_EFFECTIVE, "rates_effective_subject_to_refund",
                  [RX_MOTION_EFFECTIVE], RX_SUSPENDED_RATE_REFERENCE),
                 # One-order control: some suspension orders state both the
                 # operative date and subject-to-refund status themselves.
                 (ROLE_REFUND_EFFECTIVE, "accepted_and_suspended",
                  [RX_SUSPENDED_EFFECTIVE], None)],
                docfacts, key, docket, m)
            if (start is not None
                    and start["accession"] != suspension_support["accession"]):
                a, b = suspension_support["span"]
                support_accession = suspension_support["accession"]
                docfacts.append(_docfact(
                    key, support_doc, "refund_window_suspension_subject_to_refund",
                    m.id, "subject to refund", None, "categorical",
                    "legal role: suspension establishes subject-to-refund status",
                    f"docket {docket} | ORDER TEXT of {support_accession}; paired with "
                    f"effective-date order {start['accession']}",
                    a, b, suspension_support["verbatim"],
                    support_doc.get("extraction_method", "") or "document_span",
                    "sourced_order_text",
                    stable_key=f"{docket}|suspension_subject_to_refund"))
                start["support_accession"] = support_accession
                start["basis"] += (f"; subject-to-refund status established by "
                                   f"{support_accession} order text, span[{a}:{b}]")
        # The closure bound's operative date IS the issuance date, because an
        # acceptance takes effect when the order issues. That does NOT license
        # taking it from metadata: the date is read from the order's own dateline
        # ("Issued: April 28, 2026") and the span must contain it, exactly like
        # every other bound. Storing the eLibrary heading as its evidence was the
        # A10 defect reintroduced, and the companion span-support check caught it.
        end = _boundary(
            docs, [(ROLE_REFUND_CLOSED, "refunds_accepted", [RX_ISSUED_DATE],
                    RX_REFUND_OBLIGATION_CLOSED)],
            docfacts, key, docket, m)

        procedural = (f"eLibrary identifies {start_doc['accession']} "
                      f"({start_doc['filed_date']}) as an order accepting and SUSPENDING "
                      f"tariff records subject to refund; the clock is established only "
                      f"if that role and its effective date are supported by retrieved text"
                      + (f"; eLibrary identifies the suspension-ending motion accepted by "
                         f"{eff_doc['accession']} ({eff_doc['filed_date']})" if eff_doc else "")
                      + (f"; eLibrary identifies a refund-report acceptance at "
                         f"{end_doc['accession']} "
                         f"({end_doc['filed_date']})" if end_doc else
                         "; no order accepting a refund report has been located"))

        if start is None:
            # The bound cannot be resolved to text. The issuance date is NOT used
            # as a substitute: that is precisely the defect A10 records.
            unread = sorted({d["accession"] for d in (susp + effective) if not d.get("text")})
            out.append(_obs(
                key, m, instant="", value_text=None, unit="(date range)",
                availability=Availability.KNOWN_NOT_RETRIEVED,
                validation=Validation.NOT_YET_VALIDATED,
                scope=f"{m.scope} | docket {docket} | WINDOW ONLY",
                accession=start_doc["accession"],
                document_id=start_doc.get("document_id"),
                missing_reason=(
                    "the refund window is NOT asserted. A refund window opens on the day the "
                    "rates became effective subject to refund, and that date lives in the "
                    "ORDERING PARAGRAPHS of the order"
                    + (f"; the supporting bod(y/ies) {', '.join(unread)} were not retrieved "
                       "in this run" if unread else
                       "; the retrieved order text states no such date in a form this "
                       "adapter recognises")
                    + ". The order's ISSUANCE date is in the metadata and is deliberately "
                      "NOT used as the window start: issuance and effectiveness are "
                      "different legal facts. " + procedural),
                qa_flags=("WINDOW ONLY, and not asserted: every bound must resolve to text "
                          "that supports its legal role"),
                evidence=f"{SOURCED}: order accepting and suspending, subject to refund"))
            continue

        window_end = end["iso"] if end else "open"
        value = _refuse_dollars(f"{start['iso']} .. {window_end}")
        out.append(_obs(
            key, m, instant=start["iso"], value_text=value, unit="(date range)",
            availability=Availability.PRESENT, normalized_iso=start["iso"],
            scope=f"{m.scope} | docket {docket} | WINDOW ONLY",
            accession=start["accession"], document_id=start["doc"].get("document_id"),
            qa_flags="; ".join([
                "WINDOW ONLY. NO dollar amount is published: that would require a sourced "
                "calculation of the affected rates and volumes, which this adapter does not "
                "have",
                f"START {start['iso']} -- legal role {start['role']}: {start['basis']}",
                (f"END {end['iso']} -- legal role {end['role']}: {end['basis']}" if end else
                 "END open -- no order passage explicitly stating that the refund "
                 "obligation is satisfied or closed has been located. Acceptance of a "
                 "refund report for informational purposes is not treated as closure"),
                "the order's ISSUANCE date is NOT the window start; issuance, the filer's "
                "proposed effective date, the suspension, the date rates became effective "
                "subject to refund, and the closure of the refund obligation are five "
                "separate legal facts and are never interchanged",
                procedural]),
            notes=re.sub(r"\s+", " ", start["verbatim"])[:400],
            evidence=(f"{SOURCED}: ordering paragraph of {start['accession']}"
                      + (f" paired with subject-to-refund suspension order "
                         f"{start['support_accession']}"
                         if start.get("support_accession") else ""))))
        docfacts.append(_docfact(
            key, start["doc"], "refund_exposure_window", "refund_exposure_window",
            value, None, "(date range)", "window only, no amount",
            f"docket {docket} | START resolved to the ordering paragraph of "
            f"{start['accession']}; END "
            + (f"resolved to {end['accession']}" if end else
               "open, no explicit obligation-closure passage located"),
            start["span"][0], start["span"][1], start["verbatim"],
            start["doc"].get("extraction_method", "") or "document_span",
            "sourced_order_text", stable_key=docket))
    if not out:
        for docket, docs in procs.items():
            if not any(d["classification"]["stage"] in ("filed", "accepted") for d in docs):
                continue
            out.append(_obs(
                key, m, instant="", value_text=None, unit="(date range)",
                availability=Availability.NOT_APPLICABLE, validation=Validation.PASS,
                scope=f"{m.scope} | docket {docket}",
                missing_reason=("no order accepting and SUSPENDING rates subject to refund "
                                f"was located in {docket}, so no refund clock is evidenced"
                                + (". Oil pipeline index filings in particular are typically "
                                   "accepted outright rather than accepted-and-suspended."
                                   if docket.startswith("IS") else
                                   ". A filing accepted outright starts no refund clock.")),
                qa_flags="no refund exposure asserted; absence of a suspension order is the "
                         "reason, and it is stated rather than left blank"))
            break
    return out


# ---------------------------------------------------------------- tariff rate

#: Every condition the gate can report, so a failing outcome always names the
#: whole gate and not just the conditions that happened to be evaluated. A
#: condition that was never reached is FAIL, never silently absent.
GATE_TEMPLATE = {
    "0_package_retrieved": False,
    "1_record_retrieved": False,
    "2_scope_identified": False,
    "3_accepting_order_located": False,
    "4_not_superseded": False,
    "5_numeric_rate_located": False,
}


def _tariff_rate(key, procs, canonical, docfacts) -> list[dict]:
    """Retrieve the tariff package, store what it says, then let the GATE decide.

    Six conditions, all of which must hold:
      0. the tariff package's bytes were actually retrieved;
      1. a tariff record identifier was read out of them;
      2. its scope (the tariff and the cancelled predecessor) is identified;
      3. an order accepting that record was located;
      4. no later rate filing without an order supersedes it;
      5. a numeric rate carrying a unit was located.
    """
    m = BY_ID["tariff_operative_rate"]
    out = []
    packages = [f for f in canonical if f["classification"]["stage"] == "filed"]
    if not packages:
        return [_obs(key, m, instant="", value_text=None, unit="as filed",
                     availability=Availability.EXPECTED_NOT_LOCATED,
                     validation=Validation.NOT_YET_VALIDATED,
                     missing_reason=(
                         "no rate-setting tariff filing (18 CFR 154.312 / 342.2 / 342.3 / "
                         "342.4 / 284.123(b)(2)) was located for this filer in the window, "
                         "so there is no as-filed tariff package to retrieve. Where a filer's "
                         "rates run off a settlement instead, the operative records arrive as "
                         "154.203 settlement-rate compliance filings under the settlement "
                         "docket; retrieving and reconciling those is a separate route that "
                         "is NOT implemented here."))]
    newest = max(packages, key=lambda f: f["filed_date"])
    read = [f for f in packages if f.get("text")]
    if not read:
        failed = [f for f in packages if f.get("retrieval") == "failed"]
        if failed:
            f = failed[0]
            return [_obs(key, m, instant=f["filed_date"], value_text=None, unit="as filed",
                         availability=Availability.RETRIEVAL_FAILED,
                         validation=Validation.NOT_YET_VALIDATED,
                         accession=f["accession"], document_id=f.get("document_id"),
                         scope=f"{m.scope} | {','.join(f['docket_bases'])}",
                         missing_reason=(
                             "UNRESOLVED OPERATIVE RATE. The gate failed on "
                             f"0_package_retrieved: the tariff package {f['accession']} was "
                             f"located and its attachments listed, but the download failed: "
                             f"{f.get('retrieval_error')}. This is a RETRIEVAL failure -- "
                             "OURS -- not a missing source, not a nonpublic record and not "
                             "evidence that the filer has no operative rate. No rate is "
                             "published and no value is retained, because none was "
                             "retrieved."),
                         qa_flags="; ".join(
                             [f"{k}={'pass' if v else 'FAIL'}"
                              for k, v in sorted(GATE_TEMPLATE.items())]
                             + ["tariff sheets exist in eLibrary and are public; the "
                                "failure is on the retrieval path"]))]
        # A22. This branch used to report the retrieval gap in prose only, with
        # no gate vocabulary, so the delivered suite's gate test failed on it.
        # Conflating it with a gate FAILURE would have been the wrong repair --
        # "we did not fetch the package" and "we fetched it and the record does
        # not reconcile" are different facts and both must stay sayable. So
        # retrieval is now condition 0 OF THE GATE: the outcome is the same
        # (nothing is published), the reason is explicit, and the row is honest
        # that the gap is ours and not FERC's.
        gate = dict(GATE_TEMPLATE, **{"0_package_retrieved": False})
        return [_obs(key, m, instant=newest["filed_date"], value_text=None, unit="as filed",
                     availability=Availability.KNOWN_NOT_RETRIEVED,
                     validation=Validation.NOT_YET_VALIDATED,
                     accession=newest["accession"], document_id=newest.get("document_id"),
                     scope=f"{m.scope} | {','.join(newest['docket_bases'])}",
                     missing_reason=(
                         "UNRESOLVED OPERATIVE RATE. The gate failed on "
                         "0_package_retrieved: the tariff package is identified "
                         f"({newest['accession']}) and is public in eLibrary, but its bytes "
                         "were not downloaded in this run, so no tariff record, no scope and "
                         "no numeric rate could be read from it. This is OUR retrieval gap, "
                         "NOT a FERC source gap and NOT evidence that the filer has no "
                         "operative rate. No rate is published and no value is retained, "
                         "because none was retrieved."),
                     qa_flags="; ".join(
                         f"{k}={'pass' if v else 'FAIL'}" for k, v in sorted(gate.items())))]

    f = max(read, key=lambda x: x["filed_date"])
    # The tariff records live in the tariff sheet, not in the transmittal letter,
    # and the transmittal letter is usually the longest member. Every member is
    # scanned; the record identifier and the rate line may come from different
    # attachments of the same filing.
    records, rate_lines = [], []
    for part in (f.get("members") or [{"name": f.get("attachment_name", ""),
                                       "text": f.get("text", ""),
                                       "method": f.get("extraction_method", "")}]):
        text = part.get("text") or ""
        if not text:
            continue
        squeezed, index_map = elibrary.squeeze(text)
        for hit in RX_TARIFF_RECORD.finditer(squeezed):
            oa, ob = elibrary.map_span(index_map, hit.start(), hit.end())
            a, b, verbatim = elibrary.span(text, oa, ob)
            rec = ("F.E.R.C. No. " + hit.group(1)
                   + (f" (cancels F.E.R.C. No. {hit.group(2)})" if hit.group(2) else ""))
            if rec in [r[0] for r in records]:
                continue
            records.append((rec, a, b, verbatim, part.get("name", ""), bool(hit.group(2))))
            docfacts.append(_docfact(
                key, f, "tariff_record_identifier", "tariff_operative_rate", rec, None,
                "tariff record", "",
                f"{','.join(f['docket_bases'])} | as-filed package member "
                f"{part.get('name', '')}", a, b, verbatim,
                part.get("method", ""), "verified_span"))
            if len(records) >= 12:
                break
        for hit in RX_TARIFF_RECORD_GAS.finditer(squeezed):
            oa, ob = elibrary.map_span(index_map, hit.start(), hit.end())
            a, b, verbatim = elibrary.span(text, oa, ob)
            rec = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", hit.group(1))
            if rec in [r[0] for r in records]:
                continue
            records.append((rec, a, b, verbatim, part.get("name", ""), True))
            docfacts.append(_docfact(
                key, f, "tariff_record_identifier", "tariff_operative_rate", rec, None,
                "tariff record", "gas eTariff volume identifier",
                f"{','.join(f['docket_bases'])} | as-filed package member "
                f"{part.get('name', '')}", a, b, verbatim,
                part.get("method", ""), "verified_span"))
            break
        for hit in RX_RATE_LINE.finditer(squeezed):
            oa, ob = elibrary.map_span(index_map, hit.start(), hit.end())
            a, b, verbatim = elibrary.span(text, oa, ob)
            line = f"{hit.group(1)} per {hit.group(2)}"
            if line in [r[0] for r in rate_lines]:
                continue
            rate_lines.append((line, a, b, verbatim, part.get("name", "")))
            docfacts.append(_docfact(
                key, f, "tariff_rate_line", "tariff_operative_rate", line,
                float(hit.group(1)), hit.group(2), "as filed, scope not yet reconciled",
                f"{','.join(f['docket_bases'])} | as-filed package member "
                f"{part.get('name', '')} | RETAINED, NOT PUBLISHED as an operative rate",
                a, b, verbatim, part.get("method", ""), "retained_pending_gate"))
            if len(rate_lines) >= 20:
                break

    docket = f["docket_bases"][0] if f["docket_bases"] else ""
    in_docket = procs.get(docket, [])
    accepted = [d for d in in_docket
                if d["classification"]["stage"] in ("accepted", "settlement_rates_effective",
                                                    "rates_effective_subject_to_refund")
                and d["classification"]["is_issuance"]]
    # anything filed AFTER this package that changes the operative record: a
    # later rate filing, a compliance/settlement tariff record, or an amendment.
    superseding = [d for d in in_docket
                   if d["filed_date"] > f["filed_date"]
                   and d["classification"]["stage"] in (
                       "filed", "compliance_filing", "settlement_rates_effective",
                       "non_rate_tariff_change")]
    gate = dict(GATE_TEMPLATE, **{
        "0_package_retrieved": True,
        "1_record_retrieved": bool(records),
        "2_scope_identified": any(r[5] for r in records),
        "3_accepting_order_located": bool(accepted),
        "4_not_superseded": not superseding,
        "5_numeric_rate_located": bool(rate_lines),
    })
    failed_conditions = sorted(k for k, v in gate.items() if not v)
    scope = (f"{m.scope} | {docket} | as-filed package {f['accession']}")
    if not failed_conditions:
        return [_obs(key, m, instant=f["filed_date"],
                     value_text="; ".join(r[0] for r in rate_lines[:4])
                                + " | records: " + "; ".join(r[0] for r in records[:4]),
                     unit="as filed", availability=Availability.PRESENT,
                     scope=scope, accession=f["accession"], document_id=f.get("document_id"),
                     validation=Validation.PASS,
                     qa_flags=("GATE PASSED: record, scope, effective status and the "
                               "accepting order reconcile"),
                     evidence=f"accepted by {accepted[0]['accession']}")]
    later = ", ".join(sorted({f"{p['accession']} {p['classification']['stage']} "
                              f"({p['filed_date']})" for p in superseding})[:6]) or "none"
    return [_obs(
        key, m, instant=f["filed_date"], value_text=None, unit="tariff record",
        availability=Availability.UNVERIFIED_AVAILABILITY,
        validation=Validation.BLOCKED_AMBIGUITY, scope=scope,
        accession=f["accession"], document_id=f.get("document_id"),
        missing_reason=(
            "UNRESOLVED OPERATIVE RATE. The tariff package was retrieved and "
            f"{len(records)} tariff record identifier(s) are retained as document facts, "
            f"and {len(rate_lines)} rate line(s) are retained as document facts, "
            f"but the gate failed on: {', '.join(failed_conditions)}. "
            f"Later record-changing filing(s) in {docket}: {later}. "
            "The retrieved source values are kept; no numeric rate is published."),
        qa_flags=("; ".join(f"{k}={'pass' if v else 'FAIL'}" for k, v in sorted(gate.items()))
                  + f"; retained records: {'; '.join(r[0] for r in records[:4]) or 'none'}"
                  + f"; retained rate lines: "
                    f"{'; '.join(r[0] for r in rate_lines[:4]) or 'none'}"),
        evidence="gate evaluated per filer; the tariff sheets themselves were retrieved")]


# ---------------------------------------------------------------- oil index

def _oil_index(ctx, key) -> list[dict]:
    m = BY_ID["liq_oil_price_index"]
    if "hits" not in _INDEX_CACHE:
        elib = elibrary.Elibrary(ctx.client, log=ctx.log)
        try:
            _INDEX_CACHE["hits"] = elib.search(
                text="Oil Pipeline Index", start="2020-01-01", end="2026-12-31",
                class_types=[elibrary.class_type("Order/Opinion", "Commission Order/Opinion"),
                             elibrary.class_type("Notice", "Formal Notice")],
                max_pages=1)
        except FetchError as exc:
            _INDEX_CACHE["hits"] = []
            _INDEX_CACHE["error"] = exc.detail
    hits = _INDEX_CACHE.get("hits") or []
    if not hits:
        return [_obs(key, m, instant="", value_text=None, unit="percent",
                     availability=Availability.KNOWN_NOT_RETRIEVED,
                     validation=Validation.NOT_YET_VALIDATED,
                     missing_reason=(
                         "the FERC oil pipeline index is published in an RM/PL rulemaking "
                         "docket rather than in a carrier's own docket. The description-led "
                         "search for it returned nothing"
                         + (f" ({_INDEX_CACHE['error']})" if _INDEX_CACHE.get("error") else "")
                         + ". The retrieval route for the index issuance is NOT yet "
                           "established: this is OUR unfinished work, not a FERC gap."),
                     qa_flags="industry-wide ceiling adjustment, never a carrier rate")]
    newest = max(hits, key=lambda h: h["filed_date"])
    return [_obs(key, m, instant=newest["filed_date"],
                 value_text=newest["description"][:220], unit="percent",
                 availability=Availability.UNVERIFIED_AVAILABILITY,
                 validation=Validation.NOT_YET_VALIDATED,
                 scope=f"{m.scope} | {','.join(newest['docket_bases'])}",
                 accession=newest["accession"],
                 missing_reason=("the index ISSUANCE is located but its numeric value was "
                                 "not extracted from the order text in this run"),
                 qa_flags=("the published FERC index changes rate CEILINGS industry-wide; "
                           "it is NEVER an individual carrier's actual tariff change"))]


# ---------------------------------------------------------------- report metrics

def _report_metric(key, canonical, metric_id, types, route, docfacts, events,
                   assets, caveat, baseline=None) -> list[dict]:
    m = BY_ID[metric_id]
    wanted = {(ct["documentClass"], ct["documentType"]) for ct in types}
    rows = [f for f in canonical if wanted & set(f["class_pairs"])]
    if not rows:
        return [_obs(key, m, instant="", value_text=None, unit="categorical",
                     availability=Availability.SOURCE_BLANK, validation=Validation.PASS,
                     missing_reason=(f"the FERC route `{route}` is VERIFIED to work (it "
                                     "returns records for other filers in the same window), "
                                     "and this filer has none in the window. That is a "
                                     "source blank for this filer, NOT evidence that no "
                                     "source exists and NOT an unimplemented route."),
                     qa_flags=caveat)]
    out = []
    published = 0
    for f in sorted(rows, key=lambda x: x["filed_date"], reverse=True):
        a, b, verbatim = elibrary.span(f["description"], 0, len(f["description"]), pad=0)
        docfacts.append(_docfact(key, f, metric_id, metric_id, f["description"][:220],
                                 None, "categorical", "", route, a, b, verbatim,
                                 "elibrary_description_span", "verified_span"))
        events.append(_event(key, f, metric_id, "operational", assets, caveat,
                             baseline=baseline))
        published += 1
        if published > 6:
            continue
        out.append(_obs(
            key, m, instant=f["filed_date"], value_text=f["description"][:220],
            unit="categorical", availability=Availability.PRESENT,
            normalized_iso=f["filed_date"],
            scope=f"{m.scope} | {route} | filing {f['accession']}",
            accession=f["accession"], document_id=f.get("document_id"),
            qa_flags=(caveat + f"; {len(rows)} record(s) located on this route in the "
                      "window; the 6 most recent are published as observations and ALL "
                      "of them are retained as document facts and events"),
            evidence=f"route: {route}"))
    return out


# ---------------------------------------------------------------- events

#: A11. Which rate stages are an ECONOMIC CHANGE a reader could act on, as
#: distinct from a filing occurrence in the docket. A company's own tariff
#: submittal is not a change until FERC acts on it; a petition for review changes
#: no rate by itself; a compliance sheet implements a decision already reported.
#: These are the FERC ACTS that move money.
ECONOMIC_CHANGE_STAGES = {
    "accepted", "accepted_and_suspended", "rates_effective_subject_to_refund",
    "settlement_approved", "settlement_rates_effective", "refunds_accepted",
    "complaint_denied", "rehearing_denied", "hearing_terminated",
}


def _event(key, f, event_type, event_class, assets, note, baseline=None) -> dict:
    asset_ids = [a["asset_id"] for a in assets]
    shared = len(f["docket_bases"]) > 1
    if baseline is None:
        destination, is_backfill, why = ("filing_archive", 1,
                                         "no first-observed baseline was available, so this "
                                         "event is archived rather than published")
    else:
        destination, is_backfill, why = baseline.route(
            f["accession"], f.get("filed_date") or "",
            version_status=f.get("version_status") or "")
    economic = event_class == "rate" and event_type in ECONOMIC_CHANGE_STAGES
    if not economic and destination == elibrary.INVESTOR:
        destination = elibrary.ARCHIVE
        why = (f"{why} '{event_type}' is a filing occurrence or a procedural step, not an "
               "economic change: it is archived rather than published as current news.")
    return {
        "event_id": "evt-" + hashlib.sha256(
            f"docs|{f['accession']}|{event_type}".encode()).hexdigest()[:24],
        "entity_key": key, "asset_ids": json.dumps(sorted(asset_ids)),
        "event_class": event_class, "event_type": event_type,
        "headline": f["description"][:240],
        "detail": (f"accession {f['accession']}, dockets "
                   f"{','.join(f['docket_bases']) or 'none'}"
                   + ("; SHARED ORDER spanning several dockets -- only this entity's assets "
                      "are linked and the order is NOT attributed wholly to it" if shared
                      else "")
                   + f". ROUTING: {why}"),
        "destination": destination,
        "source_system": SOURCE_SYSTEM, "filing_id": f["accession"],
        "accession_number": f["accession"], "document_id": f.get("document_id"),
        "docket": ",".join(f["docket_bases"]), "reporting_date": f.get("filed_date"),
        "source_filed_date": f.get("filed_date"),
        "source_posted_date": f.get("posted_date"),
        # filing date, effective date and discovery date stay three separate
        # facts. The effective date here is the FILER'S OWN stated one and is
        # never promoted to a legal date -- see `_rate_effective_date`.
        "effective_date": _filer_effective(f) or None,
        "first_seen_at": (baseline.first_seen(
            f["accession"], f.get("first_seen_at") or _now()) if baseline
            else f.get("first_seen_at") or _now()),
        "is_backfill": is_backfill,
        "comparison_basis": "FERC eLibrary description and class/type",
        "confidence_note": note,
    }


def _rate_events(key, entity, canonical, relevant, baseline=None) -> list[dict]:
    keep = set(relevant or [])
    out = []
    for f in canonical:
        stage = f["classification"]["stage"]
        if stage in ("other_submittal", "other_issuance"):
            continue
        if not elibrary.links_assets(f):
            continue
        if keep and not (set(f["docket_bases"]) & keep):
            continue
        out.append(_event(key, f, stage, "rate", entity["assets"],
                          f"{f['classification']['evidence_class']}: "
                          f"{f['classification']['why']}", baseline=baseline))
    return out


# ---------------------------------------------------------------- helpers

def _docfact(entity_key, filing, assertion, metric_id, value_text, value_num, unit,
             qualifier, scope_note, char_start, char_end, verbatim, method,
             confidence, stable_key: str = "") -> dict:
    """One document fact.

    The identity is normally the SPAN -- a document legitimately carries many
    facts of one assertion type (a dozen tariff record identifiers, a page of
    capacity figures) and the offsets are what tell them apart.

    Where a document carries exactly ONE fact of a type, pass `stable_key`
    instead. The offsets then play no part in the identity, so re-reading the
    same bound from a different passage REPLACES the fact rather than adding a
    sibling. This matters more than it looks: `commit_unit` prunes observations
    and lineage edges for a unit of work but does NOT prune document facts, so an
    offset-keyed fact whose extraction changes leaves its predecessor behind for
    good. That is how the removed refund-closure carve-out survived a full
    regeneration and kept failing the span-support check from the grave.
    """
    acc = (filing or {}).get("accession", "")
    raw = (f"{acc}|{assertion}|{stable_key}" if stable_key
           else f"{acc}|{assertion}|{char_start}|{char_end}|{value_text}")
    return {
        "document_fact_id": "dfact-" + hashlib.sha256(raw.encode()).hexdigest()[:28],
        "document_id": (filing or {}).get("document_id"),
        "source_system": SOURCE_SYSTEM, "filing_id": acc, "source_fact_id": None,
        "entity_key": entity_key, "assertion_type": assertion, "metric_id": metric_id,
        "value_text": value_text, "value_num": value_num, "unit": unit,
        "qualifier": qualifier, "scope_note": scope_note,
        "page": "", "paragraph": str(verbatim.count("\n")),
        "char_start": char_start, "char_end": char_end, "verbatim_span": verbatim,
        "extraction_method": method or "elibrary_description_span",
        "content_hash": (filing or {}).get("content_hash") or "",
        "confidence": confidence, "review_state": "", "reviewer_note": "",
        "first_seen_at": _now()}


def _account_for_remainder(entity_key, metrics, expected, produced) -> list[dict]:
    have = {o["metric_id"] for o in produced}
    out = []
    for slot in expected:
        if slot["metric_id"] in have:
            continue
        m = metrics.get(slot["metric_id"])
        if m is None:
            continue
        out.append(_obs(entity_key, m, instant="", value_text=None, unit=None,
                        availability=Availability.EXPECTED_NOT_LOCATED,
                        validation=Validation.NOT_YET_VALIDATED,
                        missing_reason=("the searches for this filer produced no document on "
                                        "this assertion's verified route in the window"),
                        evidence=slot["requirement_evidence"]))
    return out
