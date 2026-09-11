"""
LNG adapter: NGA section 3 facilities, from eLibrary documents only.

Three corrections to the Day-3 specification are implemented here rather than
carried forward, each of them established live on 7 September 2026 and recorded
in discovery/docs_discovery.md:

  A. 284.126(g) IS NOT AN LNG OBLIGATION. Grepping all 261 rows of FERC's own
     class/type vocabulary for "LNG" returns 0 and for "Liquef" returns 0: there
     is no LNG class or type at all. Every LNG operational report located in this
     universe carries `Report/Form :: Certificate of Compliance Report`. The one
     counter-example, 20210212-5147, is a filer mis-classification whose own
     same-day twin 20210212-5148 is typed Certificate of Compliance Report.
     Selecting on 284.126(g) would return roughly one report in thirty plus a
     large body of unrelated interstate-storage filings. See CORRECTION_284_126G.

  B. THE REPORTING OBLIGATION COMES FROM THE FACILITY'S OWN NGA SECTION 3 ORDER
     CONDITION, not from a form number and not from a Class/Type. The adapter
     cites the order, quotes the condition verbatim with its span, and where the
     order was not retrieved says so -- it never infers the obligation from
     metadata.

  C. THE PUBLIC COPY OF AN OPERATIONAL REPORT IS USUALLY THE COVER LETTER ONLY.
     The operating data lives in the N/C twin as CUI//PRIV. That is
     Availability.NONPUBLIC -- a source condition -- and is recorded with the
     twin's accession. It is never "no source identified" and never a retrieval
     failure.

Capacity discipline: liquefaction, regasification/send-out and storage are three
different assertions with three different unit families, and the Corpus Christi
order states all three in one document. A pattern bound to one metric can never
emit another, and a match whose unit family does not fit the metric is refused
outright. Sabine Pass's 2.6 Bcf/d is a REGAS figure and is never presented as
liquefaction.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ferclib import coverage, elibrary, periods                             # noqa: E402
from ferclib.http import FetchError                                         # noqa: E402
from ferclib.registry import BY_ADAPTER, BY_ID, REGISTRY_VERSION            # noqa: E402
from ferclib.staging import observation_id                                  # noqa: E402
from ferclib.status import Availability, Method, Origin, Validation, VersionStatus  # noqa: E402

ADAPTER = "lng"
SOURCE_SYSTEM = elibrary.SOURCE_SYSTEM
REGIME = "eLibrary document"
FORM = "eLibrary document"
HERE = pathlib.Path(__file__).resolve().parent.parent
SEED_CSV = (HERE / "inputs" / "official_ferc" / "elibrary" /
            "lng_facility_sources_v1.csv")
SEED_SHA256 = "1d3bce4f7d9ecc0b0c40ad5d93f6aaf22b8eea2dcea0eb9da0fe64e285cc2e00"

CORRECTION_284_126G = (
    "CORRECTION to the Day-3 spec: `Report/Form :: 284.126 (g) Semi-Annual Storage "
    "Report` is NOT the identifier of an LNG reporting obligation. FERC's class/type "
    "vocabulary (261 rows) contains no LNG class and no LNG type; every LNG "
    "operational report in this universe is typed `Report/Form :: Certificate of "
    "Compliance Report`. The sole 284.126(g)-typed LNG report, 20210212-5147, is "
    "contradicted by its own same-day twin 20210212-5148. The obligation itself "
    "comes from the facility's NGA s.3 order condition, not from any Class/Type.")

#: A19: how many FERC-issued inspection letter bodies to read per facility. The
#: outcome cannot be extracted from a description, and reading every letter in a
#: decade is neither necessary nor polite to FERC's servers.
INSPECTION_BODIES_PER_FACILITY = 2

OBLIGATION_TYPE = ("Report/Form", "Certificate of Compliance Report")
STORAGE_REPORT_TYPE = ("Report/Form", "284.126 (g) Semi-Annual Storage Report")

#: one combined, correctly shaped filter for the current-activity sweep
SWEEP_TYPES = [
    elibrary.class_type("Report/Form", "Certificate of Compliance Report"),
    elibrary.class_type("Order/Opinion", "Delegated Order"),
    elibrary.class_type("Order/Opinion", "Commission Order/Opinion"),
    elibrary.class_type("FERC Correspondence With Applicant", "General Correspondence"),
    elibrary.class_type("FERC Correspondence With Applicant", "Compliance Directives"),
    elibrary.class_type("Application/Petition/Request",
                        "Certificate of Public Convenience and Necessity"),
]

# --------------------------------------------------------------- facility table
# Dockets, assets and the orders worth reading, per filing entity. Every accession
# below is a row of discovery/lng_facility_sources.csv, verified live.
# `process` says which capacity metrics can exist AT ALL for the asset: it is a
# property of what FERC authorised, frozen before any result is looked at.

FACILITIES: dict[str, dict] = {
    "NO-FERC-CID:Sabine Pass Liquefaction, LLC": {
        "facility": "Sabine Pass LNG Terminal (Cameron Parish, LA)",
        "assets": ["lng-sabine-pass-liquefaction-trains-1-4",
                   "lng-sabine-pass-liquefaction-trains-5-6"],
        "sweep_dockets": ["CP11-72", "CP13-552"],
        "all_dockets": ["CP04-47", "CP05-396", "CP11-72", "CP13-2", "CP13-552",
                        "CP15-482", "CP19-11", "CP19-515"],
        "process": {"liquefaction"},
        "fetch": ["20120416-3033"],
        "obligation_order": "20120416-3033",
        "note": "Joint semi-annual report with Sabine Pass LNG, L.P. across eight dockets.",
    },
    "NO-FERC-CID:Sabine Pass LNG, L.P.": {
        "facility": "Sabine Pass LNG Terminal (Cameron Parish, LA)",
        "assets": ["lng-sabine-pass-lng-terminal"],
        "sweep_dockets": ["CP04-47", "CP05-396"],
        "all_dockets": ["CP04-47", "CP05-396", "CP11-72", "CP13-2", "CP13-552"],
        "process": {"regas", "storage"},
        "fetch": ["20041221-3094"],
        "obligation_order": "20041221-3094",
        "note": "Import/regasification terminal. Its 2.6 Bcf/d figure is REGAS ONLY.",
    },
    "NO-FERC-CID:Corpus Christi Liquefaction, LLC": {
        "facility": "Corpus Christi LNG Terminal (San Patricio/Nueces Cos., TX)",
        "assets": ["lng-corpus-christi-liquefaction-trains-1-3",
                   "lng-corpus-christi-stage-3-trains-1-7"],
        "sweep_dockets": ["CP12-507", "CP18-512"],
        "all_dockets": ["CP12-507", "CP18-512", "CP18-513"],
        "process": {"liquefaction", "regas", "storage"},
        "fetch": ["20141230-3043"],
        "obligation_order": "20141230-3043",
        "note": "Stage III (CP18-512) is a separate authorisation and was partly "
                "vacated by 20230518-3048; its capacity is never added to CP12-507's.",
    },
    "NO-FERC-CID:Elba Liquefaction Company, L.L.C.": {
        "facility": "Elba Island LNG Terminal (Chatham County, GA)",
        "assets": ["kmi-elba-liquefaction"],
        "sweep_dockets": ["CP23-375", "CP14-103"],
        "all_dockets": ["CP14-103", "CP23-375", "CP14-115"],
        "process": {"liquefaction"},
        "fetch": ["20241121-3047", "20160601-4008"],
        "obligation_order": "20160601-4008",
        "note": "2.5 -> 2.9 MTPA amendment (20241121-3047), errata 20241122-3097.",
    },
    "C000039": {
        "facility": "Elba Island LNG Terminal (Chatham County, GA)",
        "assets": ["kmi-southern-lng-elba-island"],
        "sweep_dockets": ["CP06-470", "CP99-579"],
        "all_dockets": ["CP71-264", "CP99-579", "CP99-580", "CP99-581", "CP99-582",
                        "CP02-379", "CP02-380", "CP06-470", "CP14-103", "CP23-375"],
        "process": {"regas", "storage"},
        "fetch": ["20111011-3020", "20070920-3066"],
        "obligation_order": "20070920-3066",
        "note": "Elba III (CP06-470) was VACATED IN PART by 20111011-3020; the 2007 "
                "8.44 Bcf / 900 MMcf/d figures are authorised-then-vacated, not as-built.",
    },
    "NO-FERC-CID:Gulf LNG Energy, LLC": {
        "facility": "Gulf LNG / Clean Energy Project (Jackson County, MS)",
        "assets": ["kmi-gulf-lng-terminal"],
        "sweep_dockets": ["CP06-12"],
        "all_dockets": ["CP06-12", "CP06-13", "CP06-14"],
        "process": {"regas", "storage"},
        "fetch": ["20070216-3045"],
        "obligation_order": "20070216-3045",
        "note": "Biennial inspection cadence, not annual. Gulf LNG Liquefaction "
                "(CP15-521) is authorised but never built and is a different entity.",
    },
    "NO-FERC-CID:Roadrunner Gas Transmission, LLC": {
        "facility": "Roadrunner border-crossing facilities (Presidio County, TX)",
        "assets": ["oke-roadrunner-export-pipeline"],
        "sweep_dockets": ["CP15-161"],
        "all_dockets": ["CP15-161"],
        "process": set(),
        "fetch": [],
        "obligation_order": "",
        "note": "NGA s.3 BORDER-CROSSING PIPELINE, not an LNG process facility: no "
                "liquefaction, regasification or LNG storage is authorised for it.",
    },
}

#: docket -> [(entity_key, asset_id)], for linking one shared filing to many
#: assets WITHOUT duplicating any value or event.
DOCKET_ASSETS: dict[str, list[tuple[str, str]]] = {}
for _ek, _cfg in FACILITIES.items():
    for _dk in _cfg["all_dockets"]:
        for _aid in _cfg["assets"]:
            DOCKET_ASSETS.setdefault(_dk, []).append((_ek, _aid))

FACILITY_ENTITY_NAMES = {
    "NO-FERC-CID:Sabine Pass Liquefaction, LLC": "Sabine Pass Liquefaction, LLC",
    "NO-FERC-CID:Sabine Pass LNG, L.P.": "Sabine Pass LNG, L.P.",
    "NO-FERC-CID:Corpus Christi Liquefaction, LLC": "Corpus Christi Liquefaction, LLC",
    "NO-FERC-CID:Elba Liquefaction Company, L.L.C.":
        "Elba Liquefaction Company, L.L.C.",
    "C000039": "Southern LNG Company, L.L.C.",
    "NO-FERC-CID:Gulf LNG Energy, LLC": "Gulf LNG Energy, LLC",
    "NO-FERC-CID:Roadrunner Gas Transmission, LLC":
        "Roadrunner Gas Transmission, LLC",
}
FACILITY_KEYS = {
    "Sabine Pass LNG Terminal (Cameron Parish, LA)": "sabine-pass",
    "Corpus Christi LNG Terminal (San Patricio/Nueces Cos., TX)": "corpus-christi",
    "Elba Island LNG Terminal (Chatham County, GA)": "elba",
    "Gulf LNG / Clean Energy Project (Jackson County, MS)": "gulf-lng",
    "Roadrunner border-crossing facilities (Presidio County, TX)": "roadrunner",
}
FACILITY_MARKERS = {
    "sabine-pass": ("sabine", "pass"),
    "corpus-christi": ("corpus", "christi"),
    "elba": ("elba",),
    "gulf-lng": ("gulf", "lng"),
    "roadrunner": ("roadrunner",),
}
AUTHORITY_ROLE = "official_lng_facility_authority_v1"


# --------------------------------------------------------------- descriptions

RX_OPERATIONAL = re.compile(r"semi-?annual\s+operat(?:ional|ing)\s+report", re.I)
RX_REPORT_NO = re.compile(r"operat(?:ional|ing)\s+report\s+no\.?\s*(\d+)", re.I)
RX_PERIOD = re.compile(
    r"(?:period\s+of\s+)?(\d{1,2}/\d{1,2}/\d{4})\s*(?:to|through|-|–)\s*(\d{1,2}/\d{1,2}/\d{4})", re.I)
RX_PERIOD_WORDS = re.compile(
    r"([A-Z][a-z]+\s+\d{1,2},\s*\d{4})\s*(?:to|through|-|–)\s*([A-Z][a-z]+\s+\d{1,2},\s*\d{4})")
RX_REQUEST = re.compile(
    r"application[^.]{0,140}?(?:under\s+)?section\s+3|authorization\s+under\s+section\s+3"
    r"|limited\s+amendment\s+of\s+authorization|application\s+for\s+authorization", re.I)
RX_INSPECTION = re.compile(
    r"inspection|post-?authorization\s+review", re.I)
#: operation stated IN A REPORT BODY, in the filer's own words. The existence of
#: a report is not this; only a sentence in the retrieved text is.
RX_OPERATION_STATED = re.compile(
    r"(?:the\s+)?(?:facility|terminal|train|plant)[^.]{0,80}?"
    r"(?:operated|was\s+in\s+(?:commercial\s+)?service|remained\s+in\s+service)[^.]{0,160}\."
    r"|(?:liquefied|exported|sent\s+out|delivered)\s+[\d.,]+\s*(?:Bcf|MMcf|MTPA|MMTPA|cargoes)",
    re.I)
RX_ORDER_DECIDES = re.compile(
    r"order\s+(amending|vacating|granting|denying|approving|issuing|accepting|"
    r"terminating|dismissing|remanding)[^.]{0,90}", re.I)


# ======================================================================= A03
# FOUR STATES, NOT ONE. The 8 September audit found the adapter running a single
# keyword regex (`RX_INSERVICE`) over the FERC description and treating every
# match as "authorised to enter service". That regex contained
# "introduce hazardous fluids", so six COMMISSIONING permissions became service
# approvals; and it was applied to company submissions as well as to FERC
# issuances, so eight company REQUESTS became `authorised_to_enter_service`
# investor events. A request is not an approval and cannot become one from a
# description keyword.
#
# The four states, each of which must be supported by the OPERATIVE text and
# carry the specific facility component it concerns:
#
#   service_request           a company asks FERC for permission
#   commissioning_request     a company asks to introduce hazardous fluids
#   commissioning_authorisation  FERC permits introduction of hazardous fluids
#                             into a named component. NOT service.
#   service_authorisation     FERC authorises entry into / return to service
#
# 20200618-3040 is deliberately preserved: its own body reads "I grant your
# June 5, 2020 request for Corpus Christi Liquefaction, LLC (CCL) to introduce
# hazardous fluids and, upon successful completion of those activities within
# design specifications, commence service for export from the East Jetty
# facilities." That is one order carrying BOTH states, and a blanket exclusion
# of the words "hazardous fluids" would silently delete a real service
# authorisation. The discriminator is what the GRANTING VERB governs, not which
# words appear somewhere in the sentence.

#: classes whose documents are FERC's own act. Everything else -- Report/Form,
#: Application/Petition/Request, Pleading/Motion -- is a party's submission and
#: can never be an authorisation, however it is worded.
FERC_DECISION_CLASSES = {"Order/Opinion", "ALJ Issuance", "Notice",
                         "FERC Correspondence With Applicant"}

RX_GRANT_VERB = re.compile(
    r"\b(grant(?:s|ed|ing)?|approv(?:e|es|ed|ing)|authoriz(?:e|es|ed|ing)|"
    r"permit(?:s|ted|ting)?)\b", re.I)
RX_REFUSAL_VERB = re.compile(
    r"\b(den(?:y|ies|ied|ying)|reject(?:s|ed|ing)?|dismiss(?:es|ed|ing)|"
    r"revok(?:e|es|ed|ing)|suspend(?:s|ed|ing))\b", re.I)
#: an explicit statement that nothing has been decided. Whatever else the text
#: says, this can never become an approval.
RX_NO_DECISION = re.compile(
    r"no\s+(?:decision|order|action|ruling)\s+(?:has\s+been\s+|was\s+)?(?:issued|taken|made)"
    r"|no\s+(?:commission\s+)?action\s+(?:has\s+been\s+)?located"
    r"|(?:remains?\s+)?pending\s+before\s+the\s+commission"
    r"|awaiting\s+(?:a\s+)?(?:decision|action|authorization)"
    r"|not\s+(?:yet\s+)?(?:been\s+)?act(?:ed)?\s+(?:up)?on", re.I)
#: Entry into, or return to, commercial service. Every alternative names an ACT.
#: A bare `in-?service\b` was tried and removed: it matched 20230724-3048,
#: "request to modify the LNG Storage Tank Corrosion Mitigation In-Service Proof
#: of Concept Test Plan", which authorises a test procedure and no service at all.
RX_SERVICE_ACT = re.compile(
    r"commenc\w*\s+(?:of\s+)?service"
    r"|plac(?:e|es|ed|ing)?\s+(?:it\s+)?(?:in-?service|into\s+service|in\s+service)"
    r"|back\s+into\s+service|return\w*\s+to\s+service"
    r"|(?:begin|start)\s+(?:commercial\s+)?service", re.I)
#: Commissioning: hazardous fluids, feed gas or refrigerants may be INTRODUCED,
#: or commissioning activities may begin. A start-up step, never permission to
#: serve customers. The verb is required: "aboveground hazardous fluid piping"
#: names a pipe, not an introduction, and 20250221-3017 -- a permission to
#: commence CONSTRUCTION of that piping -- must not be read as commissioning.
RX_HAZARDOUS_ACT = re.compile(
    r"introduc\w*\s+(?:of\s+)?(?:[A-Za-z0-9 ,–-]{0,70}?\s)?"
    r"(?:hazardous\s+fluids?|feed\s+gas|refrigerants)"
    r"|by\s+introducing\s+(?:hazardous\s+fluids?|feed\s+gas)"
    r"|commissioning\s+activities"
    r"|(?:commence|continue|resume)\s+commissioning", re.I)
#: A third FERC permission that is neither of the above. Recorded as a material
#: order, never as commissioning and never as service.
RX_CONSTRUCTION_ACT = re.compile(
    r"commence\s+construction|notice\s+to\s+proceed\s+with\s+construction"
    r"|construction\s+activities", re.I)
#: a party asking for something
RX_ASKS = re.compile(r"\brequest(?:s|ed|ing)?\b|\bapplication\b|\bapplies\b|\bpetition\b",
                     re.I)

ACT_SERVICE_AUTHORISATION = "service_authorisation"
ACT_COMMISSIONING_AUTHORISATION = "commissioning_authorisation"
ACT_SERVICE_REQUEST = "service_request"
ACT_COMMISSIONING_REQUEST = "commissioning_request"


def operative_act(filing: dict) -> dict:
    """What this document DID, from its operative wording and its class.

    Returns ``{kind, combined, actor, verb, governed, why}``. ``kind`` is "" when
    the description supports no state at all -- which is the correct answer far
    more often than the old keyword scan allowed.
    """
    d = filing.get("description") or ""
    pairs = filing.get("class_pairs") or []
    is_ferc_act = any(c in FERC_DECISION_CLASSES for c, _t in pairs)
    blank = {"kind": "", "combined": False, "actor": "", "verb": "", "governed": "",
             "why": ""}
    if not d:
        return blank

    if RX_NO_DECISION.search(d):
        # An explicit "no decision issued" is decisive in one direction only: it
        # can never be an approval. It may still be a request.
        if RX_ASKS.search(d):
            kind = (ACT_COMMISSIONING_REQUEST
                    if RX_HAZARDOUS_ACT.search(d) and not RX_SERVICE_ACT.search(d)
                    else ACT_SERVICE_REQUEST if RX_SERVICE_ACT.search(d) else "")
            if kind:
                return {"kind": kind, "combined": False, "actor": "party", "verb": "",
                        "governed": d,
                        "why": "the text states that no decision has been issued; this is "
                               "recorded as a REQUEST and can never become an approval"}
        return dict(blank, why="the text states that no decision has been issued")

    if is_ferc_act:
        grant = RX_GRANT_VERB.search(d)
        refusal = RX_REFUSAL_VERB.search(d)
        if grant is None:
            return dict(blank, why="FERC issuance with no granting verb: it decides nothing "
                                   "about entry into service")
        if refusal is not None and refusal.start() < grant.start():
            return dict(blank, why=f"the operative verb is {refusal.group(0)!r}, not a grant")
        # What the grant GOVERNS is the text after the granting verb. "granting the
        # 07/27/2026 request to introduce hazardous fluids into MMLS 6" grants
        # commissioning; "granting ... request to introduce hazardous fluids and
        # commence service of East Jetty facilities" grants both.
        governed = d[grant.start():]
        service = RX_SERVICE_ACT.search(governed)
        hazardous = RX_HAZARDOUS_ACT.search(governed)
        construction = RX_CONSTRUCTION_ACT.search(governed)
        if construction and not service and not hazardous:
            return dict(blank, why="the grant governs CONSTRUCTION, which is neither "
                                   "commissioning nor entry into service")
        if service:
            return {"kind": ACT_SERVICE_AUTHORISATION, "combined": bool(hazardous),
                    "actor": "FERC", "verb": grant.group(0), "governed": governed,
                    "why": ("the granting verb governs an entry-into-service act"
                            + ("; the same order also permits introduction of hazardous "
                               "fluids, so BOTH states are recorded" if hazardous else ""))}
        if hazardous:
            return {"kind": ACT_COMMISSIONING_AUTHORISATION, "combined": False,
                    "actor": "FERC", "verb": grant.group(0), "governed": governed,
                    "why": ("the granting verb governs introduction of hazardous fluids "
                            "ONLY. Commissioning permission is not service authorisation "
                            "and no service state is recorded from it")}
        return dict(blank, why="FERC grant that concerns neither commissioning nor service")

    # A party's own submission. However it is worded, it decides nothing.
    if not RX_ASKS.search(d):
        return dict(blank, why="party submission with no request wording")
    service = RX_SERVICE_ACT.search(d)
    hazardous = RX_HAZARDOUS_ACT.search(d)
    if RX_CONSTRUCTION_ACT.search(d) and not service and not hazardous:
        return dict(blank, why="a party asks to proceed with CONSTRUCTION, which is neither "
                               "commissioning nor entry into service")
    if service:
        return {"kind": ACT_SERVICE_REQUEST, "combined": bool(hazardous), "actor": "party",
                "verb": "", "governed": d,
                "why": ("a party asks FERC for permission to enter or return to service; "
                        "the class is "
                        + ", ".join(f"{c}::{t}" for c, t in pairs)
                        + ", which is a submission and not a FERC act")}
    if hazardous:
        return {"kind": ACT_COMMISSIONING_REQUEST, "combined": False, "actor": "party",
                "verb": "", "governed": d,
                "why": "a party asks for permission to introduce hazardous fluids"}
    return dict(blank, why="party submission that asks for something else")


def _iso_from_us(s: str) -> str:
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})$", s.strip())
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    try:
        return dt.datetime.strptime(s.strip().replace(",", ""), "%B %d %Y").date().isoformat()
    except ValueError:
        return ""


def report_period(description: str) -> tuple[str, str, str]:
    """(start_iso, end_iso, verbatim). The report PERIOD is the operating
    evidence and it is parseable from the FERC-published description alone."""
    m = RX_PERIOD.search(description) or RX_PERIOD_WORDS.search(description)
    if not m:
        return "", "", ""
    return _iso_from_us(m.group(1)), _iso_from_us(m.group(2)), m.group(0)


# --------------------------------------------------------------- capacity rules
# (metric_id, kind, regex, unit_family, unit_label, note). A pattern is bound to
# exactly one metric; a regasification sentence can therefore never produce a
# liquefaction observation. UNIT_FAMILY is then re-checked on the captured unit.

MASS_RATE = {"MMTPA", "MTPA", "MILLION METRIC TONS PER ANNUM",
             "MILLION TONNES PER ANNUM", "MILLION METRIC TONNES PER ANNUM",
             "MILLION TONS PER ANNUM"}
VOL_RATE = {"MMCF", "BCF", "MCF", "MILLION CUBIC FEET", "BILLION CUBIC FEET",
            "THOUSAND CUBIC FEET"}
VOLUME = {"M3", "M³", "CUBIC METER", "CUBIC METERS", "CUBIC METRE", "CUBIC METRES",
          "BCF", "MMCF", "TANKS", "TANK"}

UNIT_FAMILY = {
    "lng_liquefaction_capacity": ("mass per annum", MASS_RATE),
    "lng_regas_sendout_capacity": ("volume per day", VOL_RATE),
    "lng_storage_capacity": ("volume or tank count", VOLUME),
}

#: Every rule captures exactly three groups: (1) qualifier-or-count, (2) value,
#: (3) unit. `g1_role` says which. The rule is bound to ONE metric, so a
#: regasification sentence can never emit a liquefaction assertion, and the
#: captured unit is then re-checked against that metric's unit family.
CAPACITY_RULES = [
    ("lng_liquefaction_capacity", "qualifier",
     re.compile(r"(?:capabilit\w+\s+to\s+liquefy|liquefy)\s+"
                r"(?:for\s+export\s+|and\s+export\s+)?"
                r"(approximately|about|up\s+to)?\s*([\d.,]+)\s*"
                r"(million\s+(?:metric\s+)?tons?\s+per\s+annum|MMTPA|MTPA)", re.I),
     "as authorised"),
    ("lng_liquefaction_capacity", "qualifier",
     re.compile(r"((?:total|each\s+with\s+an?)\s+)?liquefaction\s+capacit\w+\s+of\s+"
                r"(?:approximately|about|up\s+to)?\s*([\d.,]+)\s*"
                r"(MMTPA|MTPA|million\s+metric\s+tons?\s+per\s+annum)", re.I),
     "as authorised"),
    ("lng_liquefaction_capacity", "qualifier",
     re.compile(r"production\s+capacity\s+from\s+[\d.,]+\s*million\s+metric\s+tons\s+per\s+"
                r"annum\s*\(MTPA\)\s*to\s*(\s*)([\d.,]+)\s*(MTPA)", re.I),
     "facility total, as AMENDED"),
    ("lng_regas_sendout_capacity", "qualifier",
     re.compile(r"vaporiz\w+\s+(approximately|about|up\s+to)?\s*([\d.,]+)\s*"
                r"(million\s+cubic\s+feet|MMcf|billion\s+cubic\s+feet|Bcf)\s*"
                r"\(?[^)\d]{0,12}\)?\s*(?:per|/)\s*day", re.I),
     "vaporisation, as authorised"),
    ("lng_regas_sendout_capacity", "qualifier",
     re.compile(r"import,\s*store,?\s*and\s+vaporize\s+an\s+average\s+of\s+"
                r"(approximately|about)?\s*([\d.,]+)\s*"
                r"(billion\s+cubic\s+feet|Bcf|million\s+cubic\s+feet|MMcf)\s*(?:per|/)\s*day",
                re.I),
     "average import/vaporisation, as authorised"),
    ("lng_regas_sendout_capacity", "qualifier",
     re.compile(r"(?:firm\s+)?send[-\s]?out\s+capacity\s+of\s+(approximately|about|up\s+to)?\s*"
                r"([\d.,]+)\s*(MMcf|Bcf|million\s+cubic\s+feet|billion\s+cubic\s+feet)\s*"
                r"/?\s*(?:per\s+)?day", re.I),
     "firm send-out, as authorised"),
    ("lng_regas_sendout_capacity", "qualifier",
     re.compile(r"vaporization\s+capacity\s+(?:of\s+the\s+terminal\s+)?by\s+"
                r"(approximately|about)?\s*([\d.,]+)\s*"
                r"(million\s+cubic\s+feet|MMcf|Bcf)\s*(?:per|/)\s*day", re.I),
     "authorised increment - reconcile against any later vacatur"),
    ("lng_regas_sendout_capacity", "qualifier",
     re.compile(r"peak\s+deliverability\s+of\s+(approximately|about|up\s+to)?\s*([\d.,]+)\s*"
                r"(Bcf|MMcf|billion\s+cubic\s+feet|million\s+cubic\s+feet)\s*(?:per|/)\s*day",
                re.I),
     "peak deliverability, as authorised"),
    ("lng_storage_capacity", "count",
     re.compile(r"(three|four|five|six|seven|eight|nine|ten|two|\d+)\s+(?:proposed\s+)?"
                r"([\d.,]+)\s*(cubic\s+met(?:er|re)s?|m3|m³)"
                r"[^.]{0,45}?LNG\s+storage\s+tanks?", re.I),
     "per tank, as authorised (count x volume)"),
    ("lng_storage_capacity", "qualifier",
     re.compile(r"(?:new\s+storage\s+tank\s+with|storage\s+capacity\s+of)\s+"
                r"(approximately|about)?\s*([\d.,]+)\s*(Bcf|MMcf)", re.I),
     "as authorised"),
    ("lng_storage_capacity", "qualifier",
     re.compile(r"storage\s+capacity\s+of\s+[^.]{0,140}?\bby\s+(approximately|about)?\s*"
                r"([\d.,]+)\s*(Bcf|MMcf)", re.I),
     "authorised increment - reconcile against any later vacatur"),
    ("lng_storage_capacity", "count",
     re.compile(r"(two|three|four|five|six|seven|eight|nine|ten|\d+)\s+storage\s+tanks?\s+"
                r"capable\s+of\s+storing\s+a?\s*total\s+of\s+([\d.,]+)\s*"
                r"(cubic\s+met(?:er|re)s?|m3|m\u00b3)", re.I),
     "facility total across tanks, as authorised"),
    ("lng_storage_capacity", "count",
     re.compile(r"(\s*)(three|four|five|six|seven|eight|nine|ten|two)\s+LNG\s+storage\s+"
                r"(tanks?)", re.I),
     "TANK COUNT ONLY - not a volume"),
]

WORD_NUM = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10}

#: the semi-annual reporting condition, quoted from the facility's own order
#: A capacity figure recited inside an order that VACATES an authorisation is
#: authorised-then-vacated, not current authorised capacity. Elba III's 8.44 Bcf
#: and 900 MMcf/d live in exactly such an order.
RX_VACATUR = re.compile(r"\bvacat(?:e|es|ed|ing|ur)\b", re.I)

RX_OBLIGATION = re.compile(
    r"[^.]{0,160}semi-?annual\s+operational\s+reports?\s+(?:shall\s+be\s+filed|"
    r"with\s+the\s+Commission)[^.]{0,600}\.", re.I)


# --------------------------------------------------------------- retrieval

def retrieve(ctx, entity, *, year_from: int, year_to: int) -> list[dict]:
    """Sweep each facility's own dockets for current activity, then retrieve the
    specific orders that carry the capacity and obligation text.

    One accession is one atomic unit: a download failure leaves that accession
    recorded as unretrieved with its exact error and the rest continue.
    """
    key = entity["entity_key"]
    cfg = FACILITIES.get(key)
    if cfg is None:
        ctx.log("warn", f"{key}: no LNG facility configuration; nothing swept",
                adapter=ADAPTER, entity_cid=key)
        return []
    elib = elibrary.Elibrary(ctx.client, log=ctx.log)
    start = f"{year_from:04d}-01-01"
    end = f"{year_to:04d}-12-31"

    raw_hits: list[dict] = []
    for docket in cfg["sweep_dockets"]:
        scope_key = f"sweep:{docket}:{start}:{end}"
        try:
            hits = elib.search(docket=docket, start=start, end=end,
                               class_types=SWEEP_TYPES, max_pages=2)
        except FetchError as exc:
            ctx.staging.checkpoint(ADAPTER, key, scope_key, "failed", error=exc.detail)
            ctx.staging.open_blocker(
                ADAPTER, "source", f"{key}: docket sweep {docket} failed",
                scope=scope_key, attempts=str(exc.attempts), exact_error=exc.detail)
            ctx.log("error", f"{key} {docket}: {exc.detail}", adapter=ADAPTER, entity_cid=key)
            continue
        if not hits:
            # An empty window is not an empty docket. Roadrunner's NGA s.3
            # border-crossing authorisation is from 2015-2016, so a 2024-2026
            # window finds nothing; one widened retry distinguishes "no activity
            # in the requested window" from "nothing exists".
            try:
                hits = elib.search(docket=docket, start="1990-01-01", end=end,
                                   class_types=SWEEP_TYPES, max_pages=2)
                if hits:
                    ctx.log("info", f"{key} {docket}: 0 hits in {start}..{end}; widened to "
                                    f"1990-01-01 and found {len(hits)}",
                            adapter=ADAPTER, entity_cid=key)
                    for h in hits:
                        h["outside_window"] = True
            except FetchError as exc:
                ctx.log("warn", f"{key} {docket} widened retry: {exc.detail}",
                        adapter=ADAPTER, entity_cid=key)
        ctx.staging.checkpoint(ADAPTER, key, scope_key, "done")
        raw_hits.extend(hits)

    canonical, twins = elibrary.dedupe_availability(raw_hits)
    ctx.log("info", f"{key}: {len(raw_hits)} hits over {len(cfg['sweep_dockets'])} docket(s) "
                    f"-> {len(canonical)} distinct filings, {len(twins)} availability twins",
            adapter=ADAPTER, entity_cid=key)

    seeds = _seed_rows(key, entity["legal_name"])
    filings = [dict(h, source="sweep") for h in canonical]
    # a seed row must never re-enter as a canonical filing when the live sweep
    # already classified it as an availability twin
    have = {f["accession"] for f in filings} | {t["accession"] for t in twins}
    for s in seeds:
        if s["accession"] and s["accession"] not in have:
            filings.append(s)
            have.add(s["accession"])

    # A11: the first-observed picture must be taken BEFORE anything is written,
    # because `_persist` upserts every column and would otherwise reset it.
    baseline = elibrary.NewsBaseline(ctx.staging, ADAPTER, key, SOURCE_SYSTEM)

    # retrieve the bytes of the orders that actually carry the numbers
    for accession in list(dict.fromkeys(cfg["fetch"] + ([cfg["obligation_order"]]
                                                        if cfg.get("obligation_order") else []))):
        target = next((f for f in filings if f["accession"] == accession), None)
        if target is None:
            continue
        _fetch_document(ctx, elib, key, target)

    # A19: seek the supporting FERC bodies for inspection outcomes. Discovery is
    # not findings, and findings cannot be extracted from a description -- they
    # live in the letter. A bounded number of the most recent FERC-ISSUED
    # inspection letters is read; company responses are not read for findings,
    # because a company's own account of an inspection is not FERC's finding.
    inspection_letters = [
        f for f in filings
        if RX_INSPECTION.search(f.get("description") or "")
        and any(c == "FERC Correspondence With Applicant" for c, _t in f.get("class_pairs", []))
        and not re.search(r"response\s+to\s+FERC|submits?\s+response|submits?\s+supplemental",
                          f.get("description") or "", re.I)]
    inspection_letters.sort(key=lambda f: f.get("filed_date") or "", reverse=True)
    for f in inspection_letters[:INSPECTION_BODIES_PER_FACILITY]:
        if not f.get("text"):
            _fetch_document(ctx, elib, key, f)

    for f in filings:
        _persist(ctx, entity, cfg, f, is_twin=False, baseline=baseline)
    for t in twins:
        _persist(ctx, entity, cfg, dict(t, source="availability_twin"), is_twin=True,
                 baseline=baseline)

    filings.extend(dict(t, source="availability_twin", is_twin=True) for t in twins)
    for f in filings:
        f["_baseline"] = baseline
    baseline.establish()
    return filings


def _reviewed_seed_rows() -> list[dict]:
    """Load the exact versioned FERC-derived LNG routing index.

    The CSV is configuration/evidence routing metadata, not a substitute for
    the official occurrence/document bytes named by each row.  Its v1 byte
    identity is nevertheless mandatory because changing it changes discovery,
    docket authority, source retrieval and cross-entity attribution.
    """
    if not SEED_CSV.is_file():
        raise FileNotFoundError(
            f"mandatory versioned LNG facility source index is absent: {SEED_CSV}")
    body = SEED_CSV.read_bytes()
    actual = hashlib.sha256(body).hexdigest()
    if actual != SEED_SHA256:
        raise ValueError(
            f"LNG facility source index hash mismatch: expected {SEED_SHA256}, got {actual}")
    with SEED_CSV.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    required = {"entity", "facility", "assertion_type", "docket", "accession",
                "filed_date", "class_type", "document_title", "evidence_note",
                "confidence"}
    if len(rows) != 64 or not rows or not required.issubset(rows[0]):
        raise ValueError("LNG facility source index has the wrong schema or row population")
    return rows


def _seed_rows(entity_key: str, legal_name: str) -> list[dict]:
    """Verified source rows from the versioned LNG facility source index.

    These are FERC records located and checked live; the adapter retrieves their
    bytes rather than re-searching for them. A row whose `entity` names two
    companies is a SHARED filing and is returned for both of them.
    """
    out = []
    for row in _reviewed_seed_rows():
        if not row.get("accession"):
            continue
        names = [n.strip() for n in (row.get("entity") or "").split("/")]
        if not any(_name_matches(legal_name, n) for n in names):
            continue
        cls_types = [c for c in (row.get("class_type") or "").split("|") if c]
        pairs = []
        for c in cls_types:
            bits = c.split("::")
            pairs.append((bits[0], bits[1] if len(bits) > 1 else ""))
        out.append({
            "accession": row["accession"], "description": row.get("document_title") or "",
            "filed_date": row.get("filed_date") or "", "issued_date": row.get("filed_date") or "",
            "posted_date": "", "class_pairs": pairs, "class_types": cls_types,
            "avail_code": "N" if row.get("retrievable") == "nonpublic" else "P",
            "dockets": [d.strip() for d in (row.get("docket") or "").split(",") if d.strip()],
            "docket_bases": [elibrary.docket_base(d)
                             for d in (row.get("docket") or "").split(",") if d.strip()],
            "transmittals": [], "affiliations": [], "libraries": ["GAS"], "category": "",
            "document_id": "", "availability_twins": [],
            "source": "versioned_lng_source_index",
            "seed_assertion": row.get("assertion_type") or "",
            "seed_evidence": row.get("evidence_note") or "",
            "seed_confidence": row.get("confidence") or "",
            "seed_text_layer": row.get("text_layer") or "unknown",
            "shared_filers": names,
        })
    return out


def _name_matches(legal_name: str, other: str) -> bool:
    def norm(s):
        return re.sub(r"[^a-z0-9]", "", (s or "").lower()).replace("llc", "").replace("lp", "")
    a, b = norm(legal_name), norm(other)
    return bool(a) and bool(b) and (a in b or b in a)


def reviewed_asset_docket_rows(universe_path: pathlib.Path) -> list[dict]:
    """Return the exact reviewed LNG asset-authority map.

    ``filing_dockets`` records where an occurrence appeared; it is never legal
    authority for an asset.  These rows instead come from the versioned LNG
    source index and the frozen universe mapping.  Every relation carries its
    configuration evidence separately from the official occurrence bytes.
    """
    seed = _reviewed_seed_rows()
    by_docket: dict[str, set[str]] = {}
    for row in seed:
        for raw in (row.get("docket") or "").split(","):
            docket = elibrary.docket_base(raw.strip())
            if docket:
                by_docket.setdefault(docket, set()).add(row.get("accession") or "")

    if not pathlib.Path(universe_path).is_file():
        raise FileNotFoundError(f"frozen universe is absent: {universe_path}")
    with pathlib.Path(universe_path).open(newline="", encoding="utf-8") as handle:
        universe_rows = list(csv.DictReader(handle))
    universe_pairs = {(row.get("entity_key") or "", row.get("asset_id") or "")
                      for row in universe_rows}
    universe_by_asset = {row.get("asset_id") or "": row for row in universe_rows}

    out = []
    for docket, pairs in sorted(DOCKET_ASSETS.items()):
        for entity_key, asset_id in sorted(set(pairs)):
            if (entity_key, asset_id) not in universe_pairs:
                raise ValueError(
                    f"LNG authority map is outside the frozen universe: "
                    f"{entity_key}/{asset_id}/{docket}")
            accessions = sorted(value for value in by_docket.get(docket, set()) if value)
            if accessions:
                evidence = (
                    f"inputs/official_ferc/elibrary/lng_facility_sources_v1.csv"
                    f"#sha256={SEED_SHA256};docket={docket};"
                    f"accessions={','.join(accessions)}")
            else:
                row = universe_by_asset.get(asset_id) or {}
                frozen = " ".join(str(row.get(name) or "")
                                  for name in ("capacity", "note", "authority"))
                if docket != "CP15-161" or docket not in frozen:
                    raise ValueError(
                        f"LNG authority map has no versioned supporting route: "
                        f"{entity_key}/{asset_id}/{docket}")
                evidence = f"config/universe.csv#asset_id={asset_id};docket={docket}"
            out.append({"asset_id": asset_id, "docket": docket,
                        "role": AUTHORITY_ROLE, "evidence_ref": evidence})
    if len(out) != 44 or len({(row["asset_id"], row["docket"]) for row in out}) != 44:
        raise ValueError("reviewed LNG authority map is not the expected 44 unique rows")
    return out


def _filing_entity_associations(entity: dict, cfg: dict, filing: dict) -> list[dict]:
    """Occurrence-specific legal/facility relations for one LNG document."""
    description = str(filing.get("description") or "")
    text = " ".join((description, str(filing.get("text") or ""))).lower()
    dockets = {elibrary.docket_base(value)
               for value in (filing.get("docket_bases") or filing.get("dockets") or [])
               if value}
    facility = str(cfg.get("facility") or "")
    facility_key = FACILITY_KEYS[facility]
    markers = FACILITY_MARKERS[facility_key]
    supports_facility = all(marker in text for marker in markers)
    shared_kind = bool(RX_OPERATIONAL.search(description) or RX_INSPECTION.search(description))
    declared_names = [str(value) for value in filing.get("shared_filers") or []]
    description_hash = hashlib.sha256(description.encode("utf-8")).hexdigest()
    evidence = (
        f"eLibrary:{filing['accession']};description_sha256={description_hash};"
        f"routing_index_sha256={SEED_SHA256};dockets={','.join(sorted(dockets))}")

    relations = []
    for candidate_key, candidate_cfg in sorted(FACILITIES.items()):
        if candidate_cfg.get("facility") != facility:
            continue
        candidate_name = FACILITY_ENTITY_NAMES[candidate_key]
        named = any(_name_matches(candidate_name, value) for value in declared_names)
        named = named or _name_matches(candidate_name, description)
        docket_supported = bool(dockets & set(candidate_cfg.get("all_dockets") or ()))
        facility_subject = shared_kind and supports_facility and docket_supported
        if not named and not facility_subject:
            continue
        relations.append({
            "entity_key": candidate_key,
            "association_role": "named_filer" if named else "facility_subject",
            "facility_key": facility_key,
            "evidence_ref": evidence,
        })

    current = str(entity["entity_key"])
    if current not in {row["entity_key"] for row in relations}:
        relations.append({
            "entity_key": current,
            "association_role": "source_entity",
            "facility_key": facility_key,
            "evidence_ref": evidence,
        })
    return sorted(relations, key=lambda row: row["entity_key"])


def _fetch_document(ctx, elib, entity_key: str, filing: dict) -> None:
    """List, download and extract one accession. Never raises."""
    accession = filing["accession"]
    scope_key = f"doc:{accession}"
    if ctx.staging.is_done(ADAPTER, entity_key, scope_key) and not ctx.force:
        pass                                    # bytes are in the content-addressed cache
    ctx.staging.checkpoint(ADAPTER, entity_key, scope_key, "in_progress")
    try:
        files = elib.file_list(accession)
    except FetchError as exc:
        _record_unretrieved(ctx, entity_key, filing, scope_key, "GetFileListFromP8", exc)
        return
    public = [f for f in files if f["avail_code"] == elibrary.Avail.PUBLIC]
    filing["file_count"] = len(files)
    filing["file_avail"] = sorted({f["avail_code"] for f in files})
    if not public:
        filing["retrieval"] = "nonpublic"
        filing["text"] = ""
        filing["text_layer"] = "no"
        ctx.staging.checkpoint(ADAPTER, entity_key, scope_key, "done")
        return
    try:
        blob, entry = elib.download(
            accession, [f["attachment_id"] for f in public], expected_files=public)
    except FetchError as exc:
        _record_unretrieved(ctx, entity_key, filing, scope_key, "DownloadP8File", exc)
        return

    parts = elibrary.members(blob, hint=f"{accession}.bin")
    best_text, best_name, best_method, best_layer = "", "", "", "no"
    for name, data, _kind in parts:
        text, method, layer = elibrary.extract_text(data, name)
        if len(text) > len(best_text):
            best_text, best_name, best_method, best_layer = text, name, method, layer
    filing["retrieval"] = "retrieved"
    filing["text"] = elibrary.flatten(best_text)
    filing["text_raw_len"] = len(best_text)
    filing["text_layer"] = best_layer
    filing["extraction_method"] = best_method or "unsupported"
    filing["attachment_name"] = best_name
    filing["attachment_id"] = next((f["attachment_id"] for f in public), "")
    filing["content_hash"] = entry["content_hash"]
    filing["media_type"] = entry["media_type"]
    filing["byte_size"] = entry["byte_size"]
    filing["document_first_seen_at"] = elibrary.cache_first_seen_at(entry)
    filing["document_retrieved_at"] = elibrary.cache_retrieved_at(entry)
    filing["members"] = [(n, len(d), k) for n, d, k in parts]
    ctx.staging.checkpoint(ADAPTER, entity_key, scope_key, "done")
    ctx.log("info", f"{accession}: {len(parts)} member(s), best '{best_name}' "
                    f"({best_method}, text_layer={best_layer}, {len(best_text):,} chars)",
            adapter=ADAPTER, entity_cid=entity_key)


def _record_unretrieved(ctx, entity_key, filing, scope_key, endpoint, exc) -> None:
    filing["retrieval"] = "failed"
    filing["text"] = ""
    filing["text_layer"] = "unknown"
    filing["retrieval_error"] = f"{endpoint}: {exc.detail}"
    ctx.staging.checkpoint(ADAPTER, entity_key, scope_key, "failed", error=exc.detail)
    ctx.staging.open_blocker(
        ADAPTER, "source",
        f"{filing['accession']}: {endpoint} failed; document not parsed",
        scope=f"{entity_key}:{filing['accession']}", attempts=str(exc.attempts),
        exact_error=f"{endpoint} {exc.detail}")
    ctx.log("error", f"{filing['accession']} {endpoint}: {exc.detail}",
            adapter=ADAPTER, entity_cid=entity_key)


def _persist(ctx, entity, cfg, filing, *, is_twin: bool, baseline=None) -> None:
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
    twin_of = filing.get("twin_of")
    doc_id = f"{SOURCE_SYSTEM}|{accession}|{filing.get('attachment_id') or 'listing'}"
    # A11: an identical resubmission is a real occurrence and a version record,
    # but never an economic change. Content identity is decided on the BYTES; the
    # occurrence identity (source system + filing + fact id) is never collapsed.
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
        "entity_key": entity["entity_key"], "form": FORM,
        "accession_number": accession,
        "reporting_year": year, "reporting_period": "as_of",
        "period_start": None, "period_end": None,
        "filed_date": filing.get("filed_date") or None,
        "posted_date": filing.get("posted_date") or None,
        "issued_date": filing.get("issued_date") or None,
        "effective_date": None, "submitted_on": filing.get("filed_date") or None,
        "snapshot_date": None,
        "acceptance_status": elibrary.Avail.LABEL.get(filing.get("avail_code", ""), "unknown"),
        "taxonomy_version": None, "schema_ref": None,
        "content_hash": filing.get("content_hash"),
        "is_canonical": 0 if is_twin else 1,
        "canonical_reason": (f"availability twin ({filing.get('avail_code')}) of {twin_of}; "
                             "retained for evidence, excluded from event counting"
                             if is_twin else
                             f"most-public copy; twins={[t['accession'] for t in filing.get('availability_twins', [])] or 'none'}"),
        "version_status": version_status, "supersedes_filing_id": supersedes,
        "data_origin": "document",
        "retrieved_at": filing.get("retrieved_at") or None,
        # A11: the durable first-observation time. `_upsert` overwrites every
        # non-key column, so re-stamping now() here would make every filing look
        # newly discovered on every run and destroy the only signal that
        # separates history from news.
        "first_seen_at": filing.get("first_seen_at") or None,
        "source_url": elibrary.docinfo_url(accession),
    }
    documents = [{
        "document_id": doc_id, "source_system": SOURCE_SYSTEM, "filing_id": accession,
        "accession_number": accession, "attachment_id": filing.get("attachment_id") or "",
        "title": filing.get("attachment_name") or filing.get("description", "")[:200],
        "class_type": "|".join(filing.get("class_types") or []),
        "media_type": filing.get("media_type") or "",
        "byte_size": filing.get("byte_size"),
        "content_hash": filing.get("content_hash"),
        "cache_path": None,
        "text_layer": filing.get("text_layer") or "unknown",
        "availability": availability,
        "retrieved_at": (filing.get("document_retrieved_at")
                         or filing.get("retrieved_at") or None),
        "source_url": elibrary.filelist_url(accession)}]
    dockets = [{"source_system": SOURCE_SYSTEM, "filing_id": accession, "docket": d}
               for d in sorted(set(filing.get("dockets") or []))]
    associations = _filing_entity_associations(entity, cfg, filing)
    ctx.staging.write_filing_bundle(
        row, documents=documents, filing_dockets=dockets,
        filing_entities=associations,
        allow_shared_entities=len(associations) > 1)
    filing["document_id"] = doc_id


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------- expected grid

def freeze_expected(ctx, entity, filings, assets) -> list[dict]:
    """Frozen from the TEMPLATE and the facility's authorised process set, before
    any result is inspected. A terminal with no liquefaction train can never have
    a liquefaction capacity, and that is a NOT_REQUIRED slot with evidence -- not
    a gap."""
    key = entity["entity_key"]
    cfg = FACILITIES.get(key, {"process": set(), "note": "no facility configuration"})
    asset_id = assets[0]["asset_id"] if assets else ""
    year = ctx.args.year_to if hasattr(ctx, "args") else 2026
    out, seen = [], set()
    for m in BY_ADAPTER[ADAPTER]:
        need = {"lng_liquefaction_capacity": "liquefaction",
                "lng_regas_sendout_capacity": "regas",
                "lng_storage_capacity": "storage"}.get(m.id)
        if need is None:
            requirement = coverage.REQUIRED
            evidence = ("NGA s.3 facility: FERC publishes an application, an authorising "
                        "order, per-component in-service letter orders, semi-annual "
                        "operational reports and inspection letters for every one. "
                        + CORRECTION_284_126G if m.id == "lng_operational_report"
                        else "NGA s.3 facility: this assertion has a located FERC route "
                             "in the class/type vocabulary (see discovery/docs_discovery.md s.2.3).")
        elif need in cfg["process"]:
            requirement = coverage.CONDITIONAL
            evidence = (f"the facility's own FERC authorisation covers {need}; the figure "
                        "exists only where an order states it in that unit family")
        else:
            requirement = coverage.NOT_REQUIRED
            evidence = (f"no {need} facility is authorised for this asset: {cfg.get('note', '')}")
        slot = coverage.build_expected(
            key, asset_id, "lng", m, REGIME, periods.AS_OF, year, "Q4",
            requirement, evidence, "elibrary-classtypes-2026-09-07", ctx.staging.run_id)
        if slot["slot_id"] not in seen:
            seen.add(slot["slot_id"])
            out.append(slot)
    return out


# --------------------------------------------------------------- observations

def _obs(entity_key, m, *, instant, value_text, value_num, unit, availability,
         method=Method.DOCUMENT_EXTRACTED, validation=Validation.PASS,
         version_status=VersionStatus.ORIGINAL, scope="", qa_flags="", missing_reason="",
         accession="", document_id=None, selector="document_span", notes="",
         evidence="", normalized_iso="") -> dict:
    scope = scope or m.scope
    year = int(instant[:4]) if instant[:4].isdigit() else 0
    return {
        "observation_id": observation_id(entity_key, m.id, REGIME, periods.AS_OF, "", "",
                                         instant, scope, unit or "", method),
        "entity_key": entity_key, "metric_id": m.id, "source_regime": REGIME,
        "period_basis": periods.AS_OF, "period_start": None, "period_end": None,
        "instant_date": instant or None, "reporting_year": year or None,
        "reporting_period": "as_of", "period_label": instant or "as of (undated)",
        # Actual facility/component scope and selector contract are different
        # facts.  Preserve both just as the XBRL adapter does.
        "scope": scope, "scope_rule": m.scope,
        "unit": unit, "value_text": value_text, "value_num": value_num,
        "normalized_iso": normalized_iso or None,
        "availability": availability, "origin": Origin.ELIBRARY_DOCUMENT, "method": method,
        "version_status": version_status, "validation": validation,
        "source_system": SOURCE_SYSTEM, "filing_id": accession or None,
        "source_fact_id": None, "source_context_id": None,
        "document_id": document_id, "accession_number": accession or None,
        "candidate_count": None, "selector": selector, "derivation": "",
        "concept_local": None, "concept_qname": None,
        "taxonomy_version": None, "registry_version": REGISTRY_VERSION,
        "applicability_version": "elibrary-classtypes-2026-09-07",
        "schedule_page": m.schedule or "", "taxonomy_label": None,
        "qa_flags": qa_flags, "review_status": "", "missing_reason": missing_reason,
        "applicability_evidence": evidence, "notes": notes,
    }


def canonicalise(ctx, entity, filings, expected) -> tuple[list, list]:
    key = entity["entity_key"]
    cfg = FACILITIES.get(key)
    metrics = {m.id: m for m in BY_ADAPTER[ADAPTER]}
    obs: list[dict] = []
    docfacts: list[dict] = []
    events: list[dict] = []
    if cfg is None:
        return _account_for_remainder(key, metrics, expected, obs), []

    live = [f for f in filings if not f.get("is_twin")]
    twins = [f for f in filings if f.get("is_twin")]

    populations: list[dict] = []

    obs += _capacity(ctx, key, cfg, live, docfacts)
    obs += _status_requested(key, cfg, live, docfacts)
    obs += _status_commissioning(key, cfg, live, docfacts)
    obs += _status_authorised(key, cfg, live, docfacts)
    op_obs, op_pops = _operational(ctx, key, cfg, live, twins, docfacts)
    obs += op_obs
    populations += op_pops
    insp_obs, insp_pops = _inspection(key, cfg, live, docfacts)
    obs += insp_obs
    populations += insp_pops
    obs += _material_orders(key, cfg, live, docfacts)
    baseline = next((f.get("_baseline") for f in filings if f.get("_baseline")), None) \
        or elibrary.NewsBaseline(ctx.staging, ADAPTER, key, SOURCE_SYSTEM)
    events += _events(key, cfg, live, baseline)

    obs += _account_for_remainder(key, metrics, expected, obs)
    if docfacts:
        ctx.staging.write_document_facts(docfacts)
    # A08: set-based lineage travels with the observation it belongs to, so the
    # single writer commits the population, the observation and the edge that
    # references it inside ONE unit of work.
    edges = _population_edges(populations)
    by_obs = {o["observation_id"]: o for o in obs}
    for pop in populations:
        owner = by_obs.get(pop["observation_id"])
        if owner is not None:
            owner.setdefault("_populations", []).append(pop)
    if events and obs:
        # the single writer commits these alongside the observations
        obs[0]["_events"] = events
    ctx.log("info", f"{key}: {len(obs)} observations, {len(docfacts)} document facts, "
                    f"{len(events)} events, {len(populations)} lineage population(s)",
            adapter=ADAPTER, entity_cid=key)
    return obs, edges


def _population_edges(populations: list[dict]) -> list[dict]:
    """One `input_role='population'` edge per persisted population.

    A08's rule is that an input must be traversable. A set-derived row is
    traversable through the population that defines it -- inclusion rule,
    exclusion rule, member digest and all -- and an edge with every input column
    null is not lineage at all.
    """
    out = []
    for i, p in enumerate(populations, 1):
        out.append({
            "observation_id": p["observation_id"], "input_order": i,
            "input_role": "population", "operator_sign": "", "coefficient": 1.0,
            "input_source_system": p["source_system"],
            "input_filing_id": None, "input_source_fact_id": None,
            "input_observation_id": None, "input_context_id": None,
            "input_concept": p["source_table"],
            "input_period": None,
            "input_value": p.get("aggregate_value"),
            "input_unit": p.get("aggregate_unit"),
            "input_version_status": None,
            "input_population_id": p["population_id"]})
    return out


# ---------------------------------------------------------------- capacity

PROCESS_OF = {"lng_liquefaction_capacity": "liquefaction",
              "lng_regas_sendout_capacity": "regas",
              "lng_storage_capacity": "storage"}


def _capacity(ctx, key, cfg, filings, docfacts) -> list[dict]:
    out = []
    by_metric: dict[str, list[dict]] = {}
    for f in filings:
        text = f.get("text") or ""
        if not text:
            continue
        seen_here: set[tuple] = set()
        for metric_id, g1_role, rx, default_note in CAPACITY_RULES:
            m = BY_ID[metric_id]
            if PROCESS_OF[metric_id] not in cfg["process"]:
                continue                       # never assert a process FERC did not authorise
            family_label, family = UNIT_FAMILY[metric_id]
            for hit in rx.finditer(text):
                g1 = (hit.group(1) or "").strip()
                value_raw, unit_raw = hit.group(2), hit.group(3)
                if _unit_key(unit_raw) not in family:
                    continue                   # HARD GUARD: wrong unit family for this metric
                qualifier = g1 if g1_role == "qualifier" else ""
                count = WORD_NUM.get(g1.lower(), _num(g1)) if g1_role == "count" and g1 else None
                value_num = (float(WORD_NUM[value_raw.lower()])
                             if value_raw.lower() in WORD_NUM else _num(value_raw))
                if value_num is None:
                    continue
                a, b, verbatim = elibrary.span(text, hit.start(), hit.end())
                matched = hit.group(0).lower()
                note = default_note
                if "total" in matched:
                    note = "facility total, as authorised"
                elif "each with" in matched:
                    note = "PER TRAIN/UNIT, as authorised - never a facility total"
                value_text = (f"{value_raw} {unit_raw}".strip()
                              + (f" x {int(count)} tanks" if count else ""))
                dedup = (metric_id, value_text, note)
                if dedup in seen_here:
                    continue                   # the same wording repeated in one order
                seen_here.add(dedup)
                vacated = bool(RX_VACATUR.search(f.get("description", ""))
                               or RX_VACATUR.search(verbatim))
                scope = (f"{m.scope} | {cfg['facility']} | {note} | "
                         f"order {f['accession']} char {a}"
                         + (" | AUTHORISED-THEN-VACATED" if vacated else ""))
                o = _obs(key, m, instant=f.get("filed_date") or "",
                         value_text=None if vacated else value_text,
                         value_num=None if vacated else value_num,
                         unit=unit_raw.strip(),
                         availability=(Availability.UNVERIFIED_AVAILABILITY if vacated
                                       else Availability.PRESENT),
                         validation=(Validation.BLOCKED_AMBIGUITY if vacated
                                     else Validation.PASS),
                         missing_reason=(
                             f"the figure {value_text} appears in {f['accession']}, an order "
                             "that VACATES the authorisation it recites. Authorised-then-"
                             "vacated is not current authorised capacity, and reconciling "
                             "what survives needs both the granting order and this vacatur "
                             "read in full. The value is RETAINED as a document fact and is "
                             "NOT published as an authorised capacity." if vacated else ""),
                         scope=scope,
                         accession=f["accession"], document_id=f.get("document_id"),
                         qa_flags=(("VACATUR: value retained, not asserted; " if vacated else "")
                                   + "authorised FACILITY capacity from the order text; NOT "
                                   "actual output and NOT a DOE export volume"
                                   + (f"; qualifier as ordered: '{qualifier}'" if qualifier else "")
                                   + f"; unit family enforced: {family_label}"),
                         notes=f"span[{a}:{b}] of {f.get('extraction_method', '')}",
                         evidence=f"order {f['accession']} ({f.get('filed_date')})")
                out.append(o)
                if not vacated:
                    by_metric.setdefault(m.id, []).append(o)
                docfacts.append(_docfact(
                    key, f, m.id, m.id, value_text, value_num, unit_raw.strip(), qualifier,
                    scope, a, b, verbatim, f.get("extraction_method", ""),
                    "retained_vacated_not_asserted" if vacated else "verified_span"))
    # CROSS-DOCUMENT VACATUR. A figure taken from a granting order is not current
    # authorised capacity if a later order vacated that authorisation in whole or
    # part. Elba III (CP06-470) was vacated in part by 20111011-3020, so the 2007
    # 8.44 Bcf / 900 MMcf/d figures cannot be carried forward. The values stay;
    # the assertion is downgraded to unresolved with the vacatur named.
    vacaturs = [v for v in filings
                if RX_VACATUR.search(v.get("description", ""))
                and any(c == "Order/Opinion" for c, _t in v.get("class_pairs", []))]
    if vacaturs:
        vacated_dockets = {d for v in vacaturs for d in (v.get("docket_bases") or [])}
        for rows in by_metric.values():
            for o in list(rows):
                src = next((x for x in filings
                            if x["accession"] == o["accession_number"]), None)
                if src is None or not (set(src.get("docket_bases") or []) & vacated_dockets):
                    continue
                names = ", ".join(f"{v['accession']} ({v['filed_date']})" for v in vacaturs)
                o["availability"] = Availability.UNVERIFIED_AVAILABILITY
                o["validation"] = Validation.BLOCKED_AMBIGUITY
                o["missing_reason"] = (
                    f"the authorisation this figure comes from ({o['accession_number']}, "
                    f"dockets {','.join(src.get('docket_bases') or [])}) was VACATED IN WHOLE "
                    f"OR PART by {names}. Authorised-then-vacated is not as-built and not "
                    "current authorised capacity; reconciling the two orders is required "
                    "before any figure is asserted. The value is retained as a document fact.")
                o["qa_flags"] += ("; UNRESOLVED against a later vacatur; value retained, "
                                  "not asserted")
                o["value_text"], o["value_num"] = None, None
                rows.remove(o)

    # Amendments are a SEQUENCE, kept rather than overwritten. Only figures with
    # the SAME scope role from DIFFERENT orders are an amendment chain: the 15
    # MMTPA facility total and the 5 MMTPA per-train figure in the Corpus Christi
    # order are two coexisting assertions, not a supersession.
    chains: dict[tuple, list[dict]] = {}
    for metric_id, rows in by_metric.items():
        for o in rows:
            role = o["scope"].split(" | ")[2] if o["scope"].count(" | ") >= 2 else ""
            chains.setdefault((metric_id, role), []).append(o)
    for metric_id, rows in by_metric.items():
        amended = [o for o in rows if "AMENDED" in (o["scope"] or "")]
        if not amended:
            continue
        current = max(amended, key=lambda o: (o["instant_date"] or "", o["value_text"] or ""))
        for older in rows:
            if older is current or "AMENDED" in (older["scope"] or ""):
                continue
            if older["value_text"] == current["value_text"]:
                continue
            older["version_status"] = VersionStatus.SUPERSEDED
            older["qa_flags"] += (
                f"; SUPERSEDED by the amendment to {current['value_text']} granted in "
                f"{current['accession_number']} ({current['instant_date']}); RETAINED so the "
                "amendment sequence survives rather than being overwritten")
            current["qa_flags"] += (f"; amends {older['value_text']} "
                                    f"({older['accession_number']})")
    for (metric_id, role), rows in chains.items():
        dated = sorted(rows, key=lambda o: (o["instant_date"] or "", o["accession_number"] or ""))
        distinct_orders = {o["accession_number"] for o in dated}
        if len(distinct_orders) < 2:
            continue
        for older in dated[:-1]:
            older["version_status"] = VersionStatus.SUPERSEDED
            older["qa_flags"] += (f"; amended by order {dated[-1]['accession_number']} "
                                  f"({dated[-1]['instant_date']}); RETAINED so the amendment "
                                  "sequence survives rather than being overwritten")
        dated[-1]["qa_flags"] += (
            f"; current value of a {len(dated)}-step amendment sequence for '{role}': "
            + " -> ".join(f"{o['value_text']} ({o['accession_number']})" for o in dated))

    # anything the facility authorises but no retrieved order stated
    for need, metric_id in (("liquefaction", "lng_liquefaction_capacity"),
                            ("regas", "lng_regas_sendout_capacity"),
                            ("storage", "lng_storage_capacity")):
        if need not in cfg["process"] or by_metric.get(metric_id) or \
                any(o["metric_id"] == metric_id for o in out):
            continue
        m = BY_ID[metric_id]
        unread = [f["accession"] for f in filings
                  if f.get("seed_assertion", "").startswith(need[:5])
                  and not (f.get("text") or "")]
        failed = [f for f in filings if f.get("retrieval") == "failed"]
        if failed:
            avail, reason = Availability.RETRIEVAL_FAILED, (
                "the authorising order was located but not retrieved: "
                + "; ".join(f"{f['accession']} {f.get('retrieval_error', '')}" for f in failed))
        elif unread:
            avail, reason = Availability.KNOWN_NOT_RETRIEVED, (
                f"the authorising order is identified ({', '.join(unread)}) but its bytes "
                "were not retrieved in this run; this is OUR retrieval gap, not a FERC "
                "source gap")
        else:
            avail, reason = Availability.KNOWN_NOT_RETRIEVED, (
                f"the order text retrieved in this run states no {need} figure in the "
                f"{UNIT_FAMILY[metric_id][0]} unit family (a figure in another unit family "
                "is a different assertion and was refused), and the facility's other "
                "authorising orders were NOT RETRIEVED in this run. OUR retrieval gap, "
                "not a FERC source gap.")
        out.append(_obs(key, m, instant="", value_text=None, value_num=None, unit=None,
                        availability=avail, validation=Validation.NOT_YET_VALIDATED,
                        missing_reason=reason,
                        scope=f"{m.scope} | {cfg['facility']}",
                        qa_flags="capacity not asserted; no substitution from another process"))
    return out


def _unit_key(u: str) -> str:
    """Normalised unit token used for the unit-family guard. 'MMcf' and
    'million cubic feet' are the same family; 'MTPA' is a different one and no
    amount of plausibility lets a mass-per-annum figure stand in for a
    volume-per-day one."""
    return re.sub(r"\s+", " ", (u or "").strip().upper()).rstrip("S")


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- status

def _status_requested(key, cfg, filings, docfacts) -> list[dict]:
    """State 1: somebody ASKED. Never an approval, whatever the wording.

    Two routes, both of them party submissions: the NGA s.3 application itself,
    and the per-component requests to commence or resume service that Elba,
    Sabine Pass and Corpus Christi file as Certificate of Compliance Reports. The
    second route is the one the audit found being emitted as
    `authorised_to_enter_service` investor events.
    """
    m = BY_ID["lng_status_requested"]
    out = []
    for f in _typed(filings, "Application/Petition/Request",
                    "Certificate of Public Convenience and Necessity"):
        d = f["description"]
        if not RX_REQUEST.search(d):
            continue
        a, b, verbatim = elibrary.span(d, 0, len(d), pad=0)
        out.append(_obs(
            key, m, instant=f["filed_date"],
            value_text=f"application filed {f['filed_date']} ({f['accession']})",
            value_num=None, unit="categorical", availability=Availability.PRESENT,
            normalized_iso=f["filed_date"],
            scope=f"{m.scope} | {cfg['facility']} | AUTHORISATION APPLICATION | dockets "
                  f"{','.join(f['docket_bases'])} | filing {f['accession']}",
            accession=f["accession"], document_id=f.get("document_id"),
            qa_flags=("REQUESTED only. An application is not an authorisation and not an "
                      "operating facility; the type used is 'Certificate of Public "
                      "Convenience and Necessity' because the dedicated NGA s.3 request "
                      "type returns 0 hits in every docket of this universe"),
            evidence="Application/Petition/Request::Certificate of Public Convenience and Necessity"))
        docfacts.append(_docfact(key, f, "lng_authorisation_application",
                                 "lng_status_requested",
                                 f"application filed {f['filed_date']}", None, "categorical",
                                 "", f"{cfg['facility']} | {','.join(f['docket_bases'])}",
                                 a, b, verbatim, "elibrary_description_span", "verified_span"))

    for f in filings:
        act = operative_act(f)
        if act["kind"] not in (ACT_SERVICE_REQUEST, ACT_COMMISSIONING_REQUEST):
            continue
        d = f["description"]
        a, b, verbatim = elibrary.span(d, 0, len(d), pad=0)
        component = _component(d)
        asked = ("permission to enter or return to service"
                 if act["kind"] == ACT_SERVICE_REQUEST
                 else "permission to introduce hazardous fluids (commissioning)")
        out.append(_obs(
            key, m, instant=f["filed_date"],
            value_text=f"{component}: {asked} REQUESTED by the filer on "
                       f"{f['filed_date']} ({f['accession']})",
            value_num=None, unit="categorical", availability=Availability.PRESENT,
            normalized_iso=f["filed_date"],
            scope=f"{m.scope} | {cfg['facility']} | {component} | REQUEST BY THE FILER | "
                  f"filing {f['accession']}",
            accession=f["accession"], document_id=f.get("document_id"),
            qa_flags=("REQUEST BY THE FILER, NOT A FERC DECISION. This row records only that "
                      "the company asked. No FERC order is asserted from it, no in-service "
                      "date is implied, and it is never promoted to an authorisation: "
                      + act["why"]),
            missing_reason=("whether FERC granted this request is a SEPARATE assertion that "
                            "requires FERC's own order; none is asserted here"),
            validation=Validation.PASS,
            evidence="; ".join(f["class_types"]) or "party submission"))
        docfacts.append(_docfact(
            key, f, "lng_service_request" if act["kind"] == ACT_SERVICE_REQUEST
            else "lng_commissioning_request", "lng_status_requested",
            f"{component}: {asked}", None, "categorical", "requested, not granted",
            f"{cfg['facility']} | {component} | party submission", a, b, verbatim,
            "elibrary_description_span", "verified_span"))
    return _latest_first(out)


def _status_commissioning(key, cfg, filings, docfacts) -> list[dict]:
    """State 2: FERC permits HAZARDOUS FLUIDS into a named component.

    A construction/start-up permission, not permission to serve customers. Six of
    these were stored as service authorisations before this repair.

    The registry has no dedicated metric yet (requested from the integrator in
    requests/w5-documents_shared_requests.md). Until it exists these are carried
    on `lng_material_order` -- "a decision affecting the facility, described by
    what it decided" -- which is exactly what a commissioning permission is, and
    NEVER on `lng_status_authorised`, whose meaning is entry into service.
    """
    m = BY_ID.get("lng_status_commissioning") or BY_ID["lng_material_order"]
    dedicated = m.id == "lng_status_commissioning"
    out = []
    for f in filings:
        act = operative_act(f)
        if act["kind"] != ACT_COMMISSIONING_AUTHORISATION:
            continue
        d = f["description"]
        a, b, verbatim = elibrary.span(d, 0, len(d), pad=0)
        component = _component(d)
        out.append(_obs(
            key, m, instant=f["filed_date"],
            value_text=f"{component}: introduction of hazardous fluids AUTHORISED "
                       f"{f['filed_date']} ({f['accession']})",
            value_num=None, unit="categorical", availability=Availability.PRESENT,
            normalized_iso=f["filed_date"],
            scope=f"{m.scope} | {cfg['facility']} | {component} | COMMISSIONING "
                  f"(HAZARDOUS FLUIDS) AUTHORISATION | order {f['accession']}",
            accession=f["accession"], document_id=f.get("document_id"),
            qa_flags=("COMMISSIONING PERMISSION ONLY. FERC permitted hazardous fluids to be "
                      "introduced into this component. That is NOT authorisation to enter "
                      "service, NOT evidence of an in-service date and NOT evidence of "
                      "operation. Service authorisation for this component is a separate "
                      f"order and is not asserted here. Operative reading: {act['why']}"
                      + ("" if dedicated else
                         "; carried on lng_material_order because the registry has no "
                         "lng_status_commissioning metric yet")),
            evidence="Order/Opinion + granting verb governing introduction of hazardous fluids"))
        docfacts.append(_docfact(key, f, "lng_commissioning_authorisation", m.id,
                                 f"{component}: hazardous fluids authorised", None,
                                 "categorical", "commissioning only, not service",
                                 f"{cfg['facility']} | {component}", a, b, verbatim,
                                 "elibrary_description_span", "verified_span"))
    return _latest_first(out)


def _status_authorised(key, cfg, filings, docfacts) -> list[dict]:
    """State 3: a SEQUENCE of per-component service authorisations.

    Requires a FERC issuance whose GRANTING VERB governs an entry-into-service
    act. A commissioning permission never satisfies that, and a company request
    never satisfies it however the request is worded.
    """
    m = BY_ID["lng_status_authorised"]
    out = []
    for f in filings:
        act = operative_act(f)
        if act["kind"] != ACT_SERVICE_AUTHORISATION:
            continue
        d = f["description"]
        a, b, verbatim = elibrary.span(d, 0, len(d), pad=0)
        component = _component(d)
        combined = act["combined"]
        out.append(_obs(
            key, m, instant=f["filed_date"],
            value_text=f"{component} authorised to enter service {f['filed_date']} "
                       f"({f['accession']})",
            value_num=None, unit="categorical", availability=Availability.PRESENT,
            normalized_iso=f["filed_date"],
            scope=f"{m.scope} | {cfg['facility']} | {component} | order {f['accession']}"
                  + (" | COMBINED COMMISSIONING AND SERVICE DECISION" if combined else ""),
            accession=f["accession"], document_id=f.get("document_id"),
            qa_flags=("AUTHORISED TO ENTER SERVICE. This is not proof of an actual start "
                      "and not proof of continuous operation. Authorisation is issued "
                      "per component (train, tank, berth), so this is one step of a "
                      "sequence, never a facility commercial-operation date"
                      + ("; this ONE order authorises both the introduction of hazardous "
                         "fluids and the commencement of service for this component, and "
                         "both states are recorded from it" if combined else "")
                      + f"; operative reading: {act['why']}"),
            evidence="FERC issuance whose granting verb governs an entry-into-service act"))
        docfacts.append(_docfact(key, f, "lng_service_authorisation", "lng_status_authorised",
                                 component, None, "categorical",
                                 "combined commissioning and service" if combined else "",
                                 f"{cfg['facility']} | {component}", a, b, verbatim,
                                 "elibrary_description_span", "verified_span"))
        if combined:
            docfacts.append(_docfact(
                key, f, "lng_commissioning_authorisation", "lng_status_authorised",
                f"{component}: hazardous fluids authorised", None, "categorical",
                "same order also authorises service",
                f"{cfg['facility']} | {component} | commissioning limb of a combined order",
                a, b, verbatim, "elibrary_description_span", "verified_span"))
    return _latest_first(out)


#: The component matters: an authorisation is issued per train, tank, jetty or
#: plant, never facility-wide. The unit NUMBER is part of the component -- the
#: pre-repair pattern required "No." before it, so "Movable Modular Liquefaction
#: System 6", "MMLS 02" and "System Unit 9" all collapsed onto one nameless
#: "Movable Modular Liquefaction System" and eleven distinct Elba authorisations
#: became indistinguishable.
RX_COMPONENT = re.compile(
    r"(Trains?\s+\d+(?:\s*(?:,|and|-|through)\s*\d+)*"
    r"|(?:Mov(?:e)?able\s+Modular\s+Liquefaction\s+System|MMLS)"
    r"(?:\s+(?:Unit\s*)?(?:No\.?\s*)?#?\s*\d+)?"
    r"|LNG\s+Storage\s+Tank\s+\w+|Tank\s+S-\d+"
    r"|East\s+Jetty|West\s+Jetty|Condensate\s+(?:Tank\s+[\w-]+|Plant)"
    r"|terminal\s+supply\s+line|liquid\s+nitrogen\s+vaporizers"
    r"|Stage\s+\d+|import\s+terminal|terminal)", re.I)

_MMLS_NUM = re.compile(
    r"(?:Mov(?:e)?able\s+Modular\s+Liquefaction\s+System|MMLS)\s*(?:Unit\s*)?(?:No\.?\s*)?#?\s*0*(\d+)",
    re.I)


def _component(description: str) -> str:
    """The named component, normalised so 'MMLS 02', 'MMLS 2' and 'Movable
    Modular Liquefaction System Unit #2' are one component and not three."""
    m = RX_COMPONENT.search(description or "")
    if not m:
        return "unnamed component"
    raw = re.sub(r"\s+", " ", m.group(0)).strip()
    # FERC spells it both "Movable" and "Moveable" -- 20250307-3020 and
    # 20200827-3006 use the second. One component, one label.
    raw = re.sub(r"Mov(?:e)?able Modular Liquefaction System",
                 "Movable Modular Liquefaction System", raw, flags=re.I)
    num = _MMLS_NUM.match(raw)
    if num:
        return f"Movable Modular Liquefaction System {int(num.group(1))}"
    return raw


def _operational(ctx, key, cfg, filings, twins, docfacts) -> tuple[list[dict], list[dict]]:
    """Operational reports, operating status and the obligation that requires them."""
    m_rep = BY_ID["lng_operational_report"]
    m_op = BY_ID["lng_status_operating"]
    out: list[dict] = []
    reports = []
    for f in filings:
        if not RX_OPERATIONAL.search(f["description"]):
            continue
        start, end, verbatim = report_period(f["description"])
        if not end:
            continue
        f["_period"] = (start, end)
        reports.append(f)
    reports.sort(key=lambda f: f["_period"][1])

    for f in reports:
        start, end = f["_period"]
        n = RX_REPORT_NO.search(f["description"])
        typed_284 = STORAGE_REPORT_TYPE in f["class_pairs"]
        a, b, verbatim = elibrary.span(f["description"], 0, len(f["description"]), pad=0)
        qa = ["report period is the operating evidence, parsed from FERC's own description"]
        if n:
            qa.append(f"filer's own report sequence number: {n.group(1)}")
        if typed_284:
            qa.append("FILER MIS-CLASSIFICATION: this row carries "
                      "`Report/Form :: 284.126 (g) Semi-Annual Storage Report`, which is a "
                      "Part 284 blanket-certificate STORAGE-PROVIDER type and is NOT an LNG "
                      "obligation. " + CORRECTION_284_126G)
        out.append(_obs(
            key, m_rep, instant=end,
            value_text=f"{start} to {end} ({f['accession']})", value_num=None,
            unit="(period)", availability=Availability.PRESENT, normalized_iso=end,
            scope=f"{m_rep.scope} | {cfg['facility']} | dockets "
                  f"{','.join(f['docket_bases'])} | report {f['accession']}",
            accession=f["accession"], document_id=f.get("document_id"),
            qa_flags="; ".join(qa),
            validation=Validation.SOURCE_ANOMALY_REVIEW if typed_284 else Validation.PASS,
            evidence=f"Report/Form::Certificate of Compliance Report; {CORRECTION_284_126G}"))
        docfacts.append(_docfact(key, f, "lng_report_period_coverage",
                                 "lng_operational_report",
                                 f"{start}..{end}", None, "(period)",
                                 "report coverage, not observed operation",
                                 f"{cfg['facility']} | {','.join(f['docket_bases'])}",
                                 a, b, verbatim, "elibrary_description_span", "verified_span"))

    continuity = _reporting_continuity(key, cfg, reports, twins, m_op)
    out += continuity
    populations = ([_reporting_population(key, cfg, reports, continuity[0]["observation_id"])]
                   if continuity else [])

    # the substantive content is withheld -- a source condition, precisely named
    for f in reports:
        withheld = [t for t in twins
                    if t.get("twin_of") == f["accession"]] + \
                   [{"accession": t["accession"], "avail_code": t["avail_code"]}
                    for t in f.get("availability_twins", [])]
        if not withheld:
            continue
        accs = sorted({w["accession"] for w in withheld})
        bands = sorted({w.get("avail_code", "N") for w in withheld})
        out.append(_obs(
            key, m_rep, instant=f["_period"][1],
            value_text=None, value_num=None, unit="(operating data)",
            availability=Availability.NONPUBLIC,
            scope=f"{m_rep.scope} | {cfg['facility']} | OPERATING DATA CONTENT "
                  "(send-out, liquefied quantities, cargoes, boil-off)",
            accession=f["accession"], document_id=f.get("document_id"),
            missing_reason=(f"the public accession {f['accession']} carries the COVER LETTER "
                            f"only; the operating data is filed as CUI//PRIV under "
                            f"{', '.join(accs)} (availability band {', '.join(bands)}) and is "
                            "not retrievable without privileged/CEII access. This is a source "
                            "access condition, not a missing source and not a retrieval "
                            "failure; no privileged route was attempted."),
            qa_flags="nonpublic twin recorded with its accession; never bypassed",
            validation=Validation.NOT_YET_VALIDATED))

    out += _obligation(ctx, key, cfg, filings, docfacts)
    return out, populations


def _reporting_continuity(key, cfg, reports, twins, m_op) -> list[dict]:
    """State 4, honestly named.

    Before this repair the adapter emitted one `lng_status_operating` row per
    semi-annual report, PRESENT and validation=pass, valued "operation reported
    for <period>" -- 35 of them across the universe. The evidence behind every
    one of them was that a report EXISTS with that period in its FERC-published
    description. That is report presence and reporting continuity. It is not
    proof that the facility operated, and it is not proof of compliance:

      * the public copy of these reports is the cover letter; the operating
        data is filed as CUI//PRIV in the non-public twin (see rule C at the top
        of this module), so nobody here has read an output figure;
      * a report can be filed for a period in which a train was down;
      * a facility that stopped operating would still file.

    So the continuity fact is published under its own name, and the operating
    ASSERTION is left explicitly unavailable with the reason. Where a report body
    HAS been retrieved and states operation in its own words, that is a different
    matter and the row is upgraded -- but only then.
    """
    if not reports:
        return [_obs(key, m_op, instant="", value_text=None, value_num=None,
                     unit="categorical",
                     availability=Availability.EXPECTED_NOT_LOCATED,
                     validation=Validation.NOT_YET_VALIDATED,
                     scope=f"{m_op.scope} | {cfg['facility']} | REPORTED ACTUAL OPERATION",
                     missing_reason=("no semi-annual operational report was located for this "
                                     "facility in the window, so there is no reporting "
                                     "record to read and no operation is asserted either "
                                     "way. Absence of a report is not evidence that the "
                                     "facility did not operate."),
                     qa_flags="no report population to draw on")]
    periods_seen = sorted((f["_period"][0], f["_period"][1], f["accession"])
                          for f in reports)
    first, last = periods_seen[0][0], periods_seen[-1][1]
    nonpublic = sorted({t["accession"] for t in twins
                        if t.get("twin_of") in {f["accession"] for f in reports}}
                       | {tw["accession"] for f in reports
                          for tw in (f.get("availability_twins") or [])})
    read = [f for f in reports if f.get("text")]
    stated = [f for f in read if RX_OPERATION_STATED.search(f.get("text") or "")]
    if stated:
        f = stated[-1]
        hit = RX_OPERATION_STATED.search(f["text"])
        a, b, verbatim = elibrary.span(f["text"], hit.start(), hit.end())
        return [_obs(
            key, m_op, instant=f["_period"][1],
            value_text=verbatim.strip()[:280], value_num=None, unit="categorical",
            availability=Availability.PRESENT, validation=Validation.PASS,
            normalized_iso=f["_period"][1],
            scope=f"{m_op.scope} | {cfg['facility']} | REPORTED ACTUAL OPERATION | "
                  f"report {f['accession']} span[{a}:{b}]",
            accession=f["accession"], document_id=f.get("document_id"),
            qa_flags=("OPERATION STATED IN THE REPORT BODY, quoted with its span. Distinct "
                      "from 'authorised to enter service' and from the mere existence of a "
                      "report"),
            evidence=f"body of {f['accession']}")]
    return [_obs(
        key, m_op, instant=periods_seen[-1][1], value_text=None, value_num=None,
        unit="categorical",
        availability=(Availability.NONPUBLIC if nonpublic
                      else Availability.KNOWN_NOT_RETRIEVED),
        validation=Validation.NOT_YET_VALIDATED,
        normalized_iso=periods_seen[-1][1],
        scope=f"{m_op.scope} | {cfg['facility']} | REPORTED ACTUAL OPERATION",
        accession=periods_seen[-1][2],
        missing_reason=(
            f"{len(reports)} semi-annual operational report(s) are on file for this "
            f"facility covering {first} to {last} without a gap in the sequence. That is "
            "REPORTING CONTINUITY and it is published as such. It is NOT evidence that the "
            "facility operated: the public accession carries the cover letter, and the "
            + (f"operating data is filed as CUI//PRIV under {', '.join(nonpublic)}, which "
               "was not retrieved and was not bypassed" if nonpublic else
               "report bodies were not retrieved in this run")
            + ". No output, send-out, cargo or availability figure has been read, so no "
              "operation and no compliance conclusion is asserted."),
        qa_flags=("reporting continuity, not operation; the report-presence fact is stored "
                  "separately as assertion_type='lng_report_period_coverage'"),
        evidence="the filed semi-annual operational report periods")]


def _reporting_population(key, cfg, reports, observation_id_value) -> dict | None:
    """The report set behind the continuity/operation row, persisted so a reviewer
    can redraw it. A08 requires that a derived row be traversable to its inputs;
    where the inputs are a SET of filings rather than one fact, the set itself is
    the lineage and its emptiness has to carry a reason."""
    members = sorted(f["accession"] for f in reports)
    digest = hashlib.sha256("|".join(members).encode()).hexdigest()
    return {
        "population_id": "pop-" + hashlib.sha256(
            f"lng|operating|{key}|{digest}".encode()).hexdigest()[:24],
        "observation_id": observation_id_value,
        "source_system": SOURCE_SYSTEM, "source_table": "filings",
        "filing_ids": json.dumps(members),
        "inclusion_rule": ("eLibrary filings on this facility's own dockets whose "
                           "FERC-published description matches "
                           f"{RX_OPERATIONAL.pattern!r} and states a report period"),
        "exclusion_rule": ("availability twins of an included filing (the same submission "
                           "under a different availability band) are excluded so one "
                           "submission is counted once; filings on other facilities' "
                           "dockets are excluded"),
        "row_count": len(members), "candidate_count": len(members),
        "excluded_count": 0,
        "member_key": "accession_number", "member_digest": digest,
        "members_sample": json.dumps(members[:20]),
        "aggregate_value": (f"{len(members)} operational reports on file" if members
                            else None),
        "aggregate_unit": "(count of filings)",
        "empty_reason": (None if members else
                         "no filing on this facility's dockets carries semi-annual "
                         "operational-report wording in the requested window; the route is "
                         "verified and this is a source blank for this facility, not an "
                         "unimplemented route"),
        "note": (f"{cfg['facility']}: this population evidences REPORTING CONTINUITY only. "
                 "It is never evidence of operation or of compliance."),
        "created_at": _now()}


def _obligation(ctx, key, cfg, filings, docfacts) -> list[dict]:
    """The obligation is a CONDITION OF THE FACILITY'S OWN NGA s.3 ORDER.

    Never inferred from a Class/Type. Where the order text was retrieved the
    condition is quoted verbatim with its span; where it was not, the accession
    is named and the row says so.
    """
    m = BY_ID["lng_operational_report"]
    acc = cfg.get("obligation_order")
    if not acc:
        return [_obs(key, m, instant="", value_text=None, value_num=None,
                     unit="(order condition)",
                     availability=Availability.KNOWN_NOT_RETRIEVED,
                     scope=f"{m.scope} | REPORTING OBLIGATION SOURCE",
                     missing_reason=("the semi-annual reporting obligation for this facility "
                                     "comes from its own NGA s.3 order condition; that order "
                                     "has not been identified and its text was NOT RETRIEVED "
                                     "in this pass. OUR gap, not a FERC source gap. "
                                     + CORRECTION_284_126G),
                     validation=Validation.NOT_YET_VALIDATED)]
    f = next((x for x in filings if x["accession"] == acc), None)
    text = (f or {}).get("text") or ""
    hit = RX_OBLIGATION.search(text) if text else None
    if hit:
        a, b, verbatim = elibrary.span(text, hit.start(), hit.end(), pad=0)
        docfacts.append(_docfact(key, f, "lng_reporting_obligation", "lng_operational_report",
                                 verbatim[:400], None, "(order condition)", "",
                                 f"{cfg['facility']} | NGA s.3 order condition, {acc}",
                                 a, b, verbatim, f.get("extraction_method", ""), "verified_span"))
        return [_obs(key, m, instant=f["filed_date"],
                     value_text=verbatim[:300], value_num=None, unit="(order condition)",
                     availability=Availability.PRESENT,
                     scope=f"{m.scope} | REPORTING OBLIGATION SOURCE, order {acc}",
                     accession=acc, document_id=f.get("document_id"),
                     qa_flags=("obligation quoted VERBATIM from the facility's own NGA s.3 "
                               "order condition, with its span. " + CORRECTION_284_126G),
                     notes=f"span[{a}:{b}]",
                     evidence=f"order {acc} condition text")]
    return [_obs(key, m, instant=(f or {}).get("filed_date", ""),
                 value_text=None, value_num=None, unit="(order condition)",
                 availability=(Availability.RETRIEVAL_FAILED if (f or {}).get("retrieval") == "failed"
                               else Availability.KNOWN_NOT_RETRIEVED),
                 scope=f"{m.scope} | REPORTING OBLIGATION SOURCE, order {acc}",
                 accession=acc,
                 missing_reason=(f"the obligation is imposed by order {acc}; its text was "
                                 + ((f or {}).get("retrieval_error") or
                                    "not retrieved or carries no readable text layer")
                                 + ". The obligation is NOT inferred from a Class/Type. "
                                 + CORRECTION_284_126G),
                 validation=Validation.NOT_YET_VALIDATED)]


RX_INSPECTION_WHEN = re.compile(
    r"(?:the\s+)?(\d{1,2}/\d{1,2}/\d{4}|[A-Z][a-z]+\s+\d{1,2}(?:\s*[-–]\s*\d{1,2})?,\s*\d{4})"
    r"\s*(?:et\s+al\.?\s*)?(?:annual|biennial|technical|site|post[-\s]?)?\s*"
    r"(?:post[-\s]?)?(?:inspection|review)", re.I)
RX_INSPECTION_TOOK_PLACE = re.compile(
    r"(?:inspection|review)[^.]{0,140}?took\s+place\s+on\s+"
    r"([A-Z][a-z]+\s+\d{1,2}(?:\s*[-–]\s*\d{1,2})?,\s*\d{4})"
    r"|(?:conducted|held)\s+(?:on|from)\s+"
    r"([A-Z][a-z]+\s+\d{1,2}(?:\s*[-–]\s*\d{1,2})?,\s*\d{4}|\d{1,2}/\d{1,2}/\d{4})", re.I)
#: FERC's own stated result. "no recommendations at this time" is a FINDING --
#: it is what staff concluded -- and it is NOT a statement that the facility is
#: compliant, closed, or free of outstanding items.  A bare "we request that"
#: is deliberately excluded: Gulf LNG 20230824-3057 uses that wording for the
#: administrative instruction to file any extension request 30 days early.  Its
#: actual safety recommendation is the numbered item under the document's
#: ``Inspection Recommendations`` heading and is handled separately below.
RX_INSPECTION_RESULT = re.compile(
    r"(?:based\s+on\s+(?:this|the)\s+(?:technical\s+)?review[^.]{0,120}?\.)"
    r"|(?:we\s+have\s+(?:no\s+)?recommendations?[^.]{0,160}\.)"
    r"|(?:we\s+recommend\s+that[^.]{0,220}\.)", re.I)

_INSPECTION_DUE_DATE = (
    r"(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2},\s*\d{4}|\d{1,2}/\d{1,2}/\d{4}")
#: A structured recommendation is stronger than a keyword hit in the covering
#: letter.  The bounded passage starts under FERC's own heading, includes the
#: numbered operative command and its due date, and stops before explanatory
#: material.  This preserves the complete Gulf LNG command through "Rev. 3
#: October 2017." despite the periods in U.S./NRC citations.
RX_INSPECTION_NUMBERED_RECOMMENDATION = re.compile(
    r"\binspection\s+recommendations?\s*:\s*(?:recommendation\s+)?\d+\.\s*"
    r"(?P<operative>"
    rf"(?:by\s+(?:{_INSPECTION_DUE_DATE})\s*,\s*)?"
    r"(?:provide|submit|file|install|develop|implement|correct|repair|replace|"
    r"conduct|complete|address)\b.{20,900}?)"
    r"(?=\s+(?:Note\s+that\b|The\s+(?:plan|response)\s+should\b|"
    r"See\s+Enclosure\b)|\Z)", re.I | re.S)
RX_INSPECTION_OUTSTANDING = re.compile(
    r"(?:items?\s+that\s+(?:are\s+)?in\s+the\s+process\s+of\s+being\s+addressed[^.]{0,160}\.)"
    r"|(?:file\s+a\s+complete\s+response\s+within\s+\d+\s+days[^.]{0,120}\.)"
    r"|(?:outstanding|unresolved)\s+(?:item|recommendation|condition)s?[^.]{0,160}\.", re.I)
RX_INSPECTION_CLOSURE = re.compile(
    r"(?:this\s+(?:matter|item|recommendation)\s+is\s+(?:now\s+)?closed[^.]{0,120}\.)"
    r"|(?:we\s+consider\s+(?:this|these)[^.]{0,60}?\s+closed[^.]{0,120}\.)"
    r"|(?:no\s+further\s+action\s+is\s+(?:required|necessary)[^.]{0,120}\.)", re.I)


def _inspection_finding(text: str) -> tuple[int, int, str] | None:
    """Return only an operative FERC finding passage.

    Numbered recommendations outrank narrative outcome sentences.  This is
    material for Gulf LNG 20230824-3057: the covering letter's extension-filing
    instruction precedes the enclosure, but it is not an inspection finding.
    """
    structured = RX_INSPECTION_NUMBERED_RECOMMENDATION.search(text or "")
    if structured:
        a, b = structured.span("operative")
        return a, b, re.sub(r"\s+", " ", text[a:b]).strip()
    narrative = RX_INSPECTION_RESULT.search(text or "")
    if narrative:
        return (narrative.start(), narrative.end(),
                re.sub(r"\s+", " ", narrative.group(0)).strip())
    return None

#: The five capabilities A19 requires be kept apart. Every one of them is
#: published for every facility, so a reader always sees which of the five is
#: supported and which is not -- rather than one PRESENT row that implies all
#: five.
INSPECTION_CAPABILITIES = ("discovery", "inspection_date", "findings",
                           "corrective_actions", "closure")


def _inspection(key, cfg, filings, docfacts) -> tuple[list[dict], list[dict]]:
    """A19: inspection DISCOVERY is not inspection FINDINGS.

    Before this repair one keyword on the FERC description produced 59 PRESENT
    `lng_inspection` rows on a metric whose registry meaning is "the latest
    inspection, extracted outcomes, follow-up evidence and any unresolved
    conditions". Discovery had been implemented; the other four had not, and the
    row did not say so in a way a coverage count could see.

    Five capabilities are now separate, and each is answered on its own evidence:

      discovery          the letter or response exists -- always supportable
      inspection_date    WHEN the inspection happened. The FERC description
                         states it ("the 04/21/2026 et al. annual inspection")
                         and the letter body states it ("took place on April
                         21-22, 2026"). It is NEVER the filing date: 20260819-3015
                         was filed on 19 August 2026 for an inspection held on
                         21-22 April 2026.
      findings           what staff concluded, quoted from the body
      corrective_actions items still being addressed, quoted from the body
      closure            an explicit statement that a matter is closed

    Absence of a finding is never a compliance conclusion, and the presence of
    inspection correspondence is never a finding.
    """
    m = BY_ID["lng_inspection"]
    out: list[dict] = []
    populations: list[dict] = []
    letters, responses = [], []
    for f in _typed(filings, "FERC Correspondence With Applicant", "General Correspondence") + \
            _typed(filings, "FERC Correspondence With Applicant", "Compliance Directives") + \
            _typed(filings, "Report/Form", "Certificate of Compliance Report"):
        d = f["description"]
        if not RX_INSPECTION.search(d):
            continue
        if any(f is x for x in letters + responses):
            continue
        (responses if re.search(r"response\s+to\s+FERC|submits?\s+response|submits?\s+"
                                r"supplemental", d, re.I) else letters).append(f)

    discovered = letters + responses
    if not discovered:
        return [_obs(key, m, instant="", value_text=None, value_num=None, unit="categorical",
                     availability=Availability.EXPECTED_NOT_LOCATED,
                     validation=Validation.NOT_YET_VALIDATED,
                     scope=f"{m.scope} | {cfg['facility']} | DISCOVERY",
                     missing_reason=("no inspection letter or inspection response was located "
                                     "for this facility on the verified route in the window. "
                                     "That is 'not located'; it is NOT evidence that no "
                                     "inspection occurred and NOT a compliance conclusion."),
                     qa_flags="inspection discovery found nothing on this route")], []

    # ---- capability 1: discovery -------------------------------------------
    for f in discovered:
        d = f["description"]
        a, b, verbatim = elibrary.span(d, 0, len(d), pad=0)
        is_response = f in responses
        directives = ("FERC Correspondence With Applicant", "Compliance Directives") \
            in f["class_pairs"]
        docfacts.append(_docfact(
            key, f, "lng_inspection_discovery", "lng_inspection",
            f"{'company response' if is_response else 'FERC inspection letter'} filed "
            f"{f['filed_date']}", None, "categorical",
            "discovery of a filing; not an outcome",
            f"{cfg['facility']} | {'response' if is_response else 'FERC letter'}"
            + (" | metadata carries Compliance Directives" if directives else ""),
            a, b, verbatim, "elibrary_description_span", "verified_span"))
    newest = max(discovered, key=lambda f: f["filed_date"])
    directive_rows = [f for f in discovered
                      if ("FERC Correspondence With Applicant", "Compliance Directives")
                      in f["class_pairs"]]
    disc = _obs(
        key, m, instant=newest["filed_date"],
        value_text=f"{len(discovered)} inspection-related filing(s) located "
                   f"({len(letters)} FERC letter(s), {len(responses)} company response(s)); "
                   f"most recent {newest['accession']} filed {newest['filed_date']}",
        value_num=None, unit="categorical", availability=Availability.PRESENT,
        validation=Validation.PASS, normalized_iso=newest["filed_date"],
        scope=f"{m.scope} | {cfg['facility']} | DISCOVERY (filings located)",
        accession=newest["accession"], document_id=newest.get("document_id"),
        missing_reason=(
            "INSPECTION OUTCOME NOT EXTRACTED FROM THIS ROW. This row is DISCOVERY: it "
            "reports which inspection-related documents exist and when they were FILED, "
            "read from the FERC-published metadata alone. No finding, no corrective "
            "action, no closure and no compliance status is asserted by it. Those are "
            "four separate rows on this metric, each answered on its own evidence; where "
            "one of them is unavailable it says so in its own right."),
        qa_flags=("DISCOVERY ONLY. This row says which inspection-related documents exist "
                  "and when they were FILED. The filing date is not the inspection date, "
                  "the existence of correspondence is not a finding, and none of it is a "
                  "compliance status. The inspection date, findings, corrective actions and "
                  "closure are four SEPARATE rows on this metric"
                  + (f"; {len(directive_rows)} of these also carry the class/type pair "
                     "`FERC Correspondence With Applicant::Compliance Directives`, which "
                     "signals conditions FERC considered unresolved AT THE TIME OF FILING "
                     "and is itself metadata, not a finding" if directive_rows else "")),
        evidence="FERC Correspondence With Applicant / Certificate of Compliance Report")
    out.append(disc)
    members = sorted(f["accession"] for f in discovered)
    populations.append(_inspection_population(
        key, cfg, disc["observation_id"], members, len(discovered), 0,
        "discovery", None))

    # ---- capability 2: the actual inspection date ---------------------------
    dated: list[tuple[dict, str, str, int, int, str]] = []
    for f in discovered:
        found = _inspection_date(f)
        if found:
            dated.append((f,) + found)
    if dated:
        f, iso, verbatim, a, b, source = max(dated, key=lambda r: r[1])
        elibrary.require_span_support(verbatim, iso,
                                      what="lng_inspection_date")
        docfacts.append(_docfact(key, f, "lng_inspection_date", "lng_inspection",
                                 iso, None, "(date)", f"stated in the {source}",
                                 f"{cfg['facility']} | inspection date, NOT the filing date",
                                 a, b, verbatim, f"elibrary_{source}_span", "verified_span"))
        out.append(_obs(
            key, m, instant=f["filed_date"], value_text=iso, value_num=None, unit="(date)",
            availability=Availability.PRESENT, validation=Validation.PASS,
            normalized_iso=iso,
            scope=f"{m.scope} | {cfg['facility']} | ACTUAL INSPECTION DATE",
            accession=f["accession"], document_id=f.get("document_id"),
            missing_reason=(
                "INSPECTION OUTCOME NOT EXTRACTED FROM THIS ROW. This row establishes WHEN "
                "an inspection took place and nothing else. What the inspection found, what "
                "was to be corrected and whether anything was closed are three separate "
                "rows, each answered on its own evidence."),
            qa_flags=(f"the inspection took place on {iso}; the document reporting it was "
                      f"FILED on {f['filed_date']}. These are different dates and the filing "
                      f"date is never substituted for the inspection date. Source: the "
                      f"{source} of {f['accession']}, span[{a}:{b}]"),
            evidence=f"{source} of {f['accession']}"))
    else:
        out.append(_obs(
            key, m, instant="", value_text=None, value_num=None, unit="(date)",
            availability=Availability.KNOWN_NOT_RETRIEVED,
            validation=Validation.NOT_YET_VALIDATED,
            scope=f"{m.scope} | {cfg['facility']} | ACTUAL INSPECTION DATE",
            missing_reason=("no document located for this facility states when an inspection "
                            "took place. The FILING DATE of the correspondence is NOT used "
                            "as the inspection date. This is our unresolved evidence, not a "
                            "statement that no inspection occurred."),
            qa_flags="inspection date not established from any retrieved FERC text"))

    # ---- capabilities 3-5: findings, corrective actions, closure ------------
    read = [f for f in discovered if f.get("text")]
    for capability, extractor, label, caveat in (
            ("findings", _inspection_finding, "SUBSTANTIVE FINDINGS",
             "a stated staff conclusion is a finding; it is not a certificate of compliance "
             "and it does not close anything"),
            ("corrective_actions", RX_INSPECTION_OUTSTANDING.search, "CORRECTIVE ACTIONS",
             "items recorded as being addressed are outstanding work, not a compliance "
             "failure and not a closure"),
            # The wording of these caveats matters, not only their meaning. This
            # one used to read "...that a matter is closed", which put an OUTCOME
            # WORD into the row's own qa_flags. Where FERC states a closure in
            # other words -- "No further action is required" -- the adapter would
            # then be the only thing on the row asserting "closed", with nothing
            # in the cited span behind it. A caveat must not introduce vocabulary
            # the source does not supply.
            ("closure", RX_INSPECTION_CLOSURE.search, "FOLLOW-UP / CLOSURE",
             "this row is published ONLY where the letter body states a closure in FERC's "
             "own words, quoted above with its span; it is never inferred from the absence "
             "of findings")):
        hits = [(f, extractor(f["text"])) for f in read]
        hits = [(f, h) for f, h in hits if h]
        if hits:
            f, h = max(hits, key=lambda r: r[0]["filed_date"])
            if isinstance(h, tuple):
                hit_start, hit_end, quote = h
            else:
                hit_start, hit_end = h.start(), h.end()
                quote = re.sub(r"\s+", " ", h.group(0)).strip()
            a, b, verbatim = elibrary.span(f["text"], hit_start, hit_end)
            elibrary.require_span_support(
                verbatim, quote, what=f"lng inspection {capability}")
            docfacts.append(_docfact(
                key, f, f"lng_inspection_{capability}", "lng_inspection", quote[:400],
                None, "categorical", "quoted from the letter body",
                f"{cfg['facility']} | {label}", a, b, verbatim,
                f.get("extraction_method", "") or "document_span", "verified_span"))
            out.append(_obs(
                key, m, instant=f["filed_date"], value_text=quote[:280], value_num=None,
                unit="categorical", availability=Availability.PRESENT,
                validation=Validation.PASS, normalized_iso=f["filed_date"],
                scope=f"{m.scope} | {cfg['facility']} | {label}",
                accession=f["accession"], document_id=f.get("document_id"),
                qa_flags=f"quoted VERBATIM from the body of {f['accession']}, span[{a}:{b}]. "
                         f"{caveat}",
                evidence=f"body of {f['accession']}"))
            continue
        if read:
            avail = Availability.SOURCE_BLANK
            reason = (f"{len(read)} inspection letter body/ies were retrieved and read for "
                      f"this facility and none of them states a {label.lower()}. The absence "
                      "of a stated outcome is NOT a compliance conclusion and NOT evidence "
                      "that the facility is clear.")
        else:
            avail = Availability.KNOWN_NOT_RETRIEVED
            reason = (f"{len(discovered)} inspection-related filing(s) are known for this "
                      "facility but no letter body was retrieved in this run, so no "
                      f"{label.lower()} could be read. OUR retrieval gap. Correspondence "
                      "PRESENCE is not a finding, and its absence is not a compliance "
                      "conclusion.")
        blocked = _obs(
            key, m, instant="", value_text=None, value_num=None, unit="categorical",
            availability=avail, validation=Validation.NOT_YET_VALIDATED,
            scope=f"{m.scope} | {cfg['facility']} | {label}",
            missing_reason=reason, qa_flags=caveat,
            evidence="separate capability, answered on its own evidence")
        out.append(blocked)
        populations.append(_inspection_population(
            key, cfg, blocked["observation_id"], sorted(f["accession"] for f in read),
            len(discovered), len(discovered) - len(read), capability, reason))
    return out, populations


def _inspection_date(f: dict) -> tuple[str, str, int, int, str] | None:
    """(iso, verbatim, char_start, char_end, source) for WHEN the inspection was.

    The FERC description is authoritative for this and states it plainly:
    "Letter to Corpus Christi Liquefaction, LLC discussing the 04/21/2026 et al.
    annual inspection of the terminal". The body says it too. Neither is the
    filing date, and the filing date is never used.
    """
    for source, text, rx in (("body", f.get("text") or "", RX_INSPECTION_TOOK_PLACE),
                             ("description", f.get("description") or "",
                              RX_INSPECTION_WHEN)):
        if not text:
            continue
        hit = rx.search(text)
        if not hit:
            continue
        raw = next((g for g in hit.groups() if g), "")
        iso = _iso_from_us(raw) or _iso_long(raw)
        if not iso:
            continue
        a, b, verbatim = elibrary.span(text, hit.start(), hit.end(), pad=60)
        return iso, verbatim, a, b, source
    return None


def _iso_long(text: str) -> str:
    """'April 21-22, 2026' -> 2026-04-21. A multi-day inspection is dated by its
    FIRST day and the range is preserved in the verbatim span."""
    m = re.match(r"([A-Z][a-z]+)\s+(\d{1,2})(?:\s*[-–]\s*\d{1,2})?,\s*(\d{4})",
                 (text or "").strip())
    if not m:
        return ""
    months = {n: i for i, n in enumerate(
        ["January", "February", "March", "April", "May", "June", "July", "August",
         "September", "October", "November", "December"], 1)}
    mon = months.get(m.group(1).capitalize())
    return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}" if mon else ""


def _inspection_population(key, cfg, observation_id_value, members, candidates,
                           excluded, capability, empty_reason) -> dict:
    digest = hashlib.sha256("|".join(members).encode()).hexdigest()
    return {
        "population_id": "pop-" + hashlib.sha256(
            f"lng|inspection|{capability}|{key}|{digest}".encode()).hexdigest()[:24],
        "observation_id": observation_id_value,
        "source_system": SOURCE_SYSTEM, "source_table": "filings",
        "filing_ids": json.dumps(members),
        "inclusion_rule": (
            "eLibrary filings on this facility's dockets typed `FERC Correspondence With "
            "Applicant::General Correspondence|Compliance Directives` or `Report/Form::"
            "Certificate of Compliance Report` whose description matches "
            f"{RX_INSPECTION.pattern!r}"
            + ("; further restricted to those whose BODY was retrieved and read"
               if capability != "discovery" else "")),
        "exclusion_rule": ("availability twins are excluded so one submission counts once"
                           + ("; filings whose body was not retrieved are excluded from this "
                              "capability, which is why the capability is unavailable rather "
                              "than empty" if capability != "discovery" else "")),
        "row_count": len(members), "candidate_count": candidates,
        "excluded_count": excluded,
        "member_key": "accession_number", "member_digest": digest,
        "members_sample": json.dumps(members[:20]),
        "aggregate_value": str(len(members)), "aggregate_unit": "(count of filings)",
        "empty_reason": empty_reason if not members else None,
        "note": (f"{cfg['facility']}: inspection capability '{capability}'. Discovery, "
                 "inspection date, findings, corrective actions and closure are separate "
                 "assertions; none of them implies any of the others."),
        "created_at": _now()}


def _material_orders(key, cfg, filings, docfacts) -> list[dict]:
    m = BY_ID["lng_material_order"]
    out = []
    for f in _typed(filings, "Order/Opinion", "Commission Order/Opinion") + \
            _typed(filings, "Order/Opinion", "Delegated Order"):
        d = f["description"]
        decided = RX_ORDER_DECIDES.search(d)
        if not decided:
            continue
        if operative_act(f)["kind"] in (ACT_SERVICE_AUTHORISATION,
                                        ACT_COMMISSIONING_AUTHORISATION):
            continue    # those have their own states and must not be counted twice
        a, b, verbatim = elibrary.span(d, decided.start(), decided.end(), pad=40)
        linked = _linked_assets(f)
        out.append(_obs(
            key, m, instant=f["filed_date"],
            value_text=decided.group(0).strip(), value_num=None, unit="categorical",
            availability=Availability.PRESENT, normalized_iso=f["filed_date"],
            scope=f"{m.scope} | {cfg['facility']} | dockets "
                  f"{','.join(f['docket_bases'])} | order {f['accession']}",
            accession=f["accession"], document_id=f.get("document_id"),
            qa_flags=(f"described by WHAT IT DECIDED, not as 'a new filing'; links "
                      f"{len(linked)} asset(s) through its docket set without duplicating "
                      "any capacity or event"),
            evidence="Order/Opinion; decision verb taken from the FERC description"))
        docfacts.append(_docfact(key, f, "lng_material_order", "lng_material_order",
                                 decided.group(0).strip(), None, "categorical", "",
                                 f"{cfg['facility']} | {','.join(f['docket_bases'])}",
                                 a, b, verbatim, "elibrary_description_span", "verified_span"))
    return _latest_first(out)


# ---------------------------------------------------------------- events

def _linked_assets(filing) -> list[str]:
    """Assets reachable from this filing's docket set.

    Refused entirely for Procedural Motion / Intervention rows: one service-list
    update in RP23-863 carried 300+ unrelated dockets and would fully connect the
    asset graph.
    """
    if not elibrary.links_assets(filing):
        return []
    out: set[str] = set()
    for d in filing.get("docket_bases") or []:
        for _ek, aid in DOCKET_ASSETS.get(d, []):
            out.add(aid)
    return sorted(out)


#: which event types are an ECONOMIC CHANGE a reader could act on, as distinct
#: from a filing that merely happened. A request is deliberately NOT one: a
#: company asking for permission changes nothing until FERC decides.
ECONOMIC_CHANGE_EVENTS = {"authorised_to_enter_service", "material_order"}


def _events(key, cfg, filings, baseline) -> list[dict]:
    """One event per (accession, type). asset_ids is the JSON array of every
    asset the filing touches, computed GLOBALLY from the docket map, so two
    entities processing the same shared filing emit the identical row and the
    upsert is a no-op rather than a duplicate.

    A03 + A11. The event type now comes from `operative_act`, so the eight Elba
    "submit request for commencement of service" filings become
    `service_requested` archive entries instead of `authorised_to_enter_service`
    investor events, and a commissioning permission gets its own type. The
    destination comes from the first-observed baseline, so a first ingestion
    archives rather than publishing a decade of history as current news.
    """
    out = []
    for f in filings:
        if f.get("is_twin"):
            continue                              # an availability twin is not a second event
        d = f["description"]
        act = operative_act(f)
        kind = cls = None
        if RX_OPERATIONAL.search(d):
            kind, cls = "operational_report", "operational"
        elif act["kind"] == ACT_SERVICE_AUTHORISATION:
            kind, cls = "authorised_to_enter_service", "authority"
        elif act["kind"] == ACT_COMMISSIONING_AUTHORISATION:
            kind, cls = "commissioning_authorised", "authority"
        elif act["kind"] in (ACT_SERVICE_REQUEST, ACT_COMMISSIONING_REQUEST):
            # `authority` is the lifecycle a request belongs to, and it is the
            # declared vocabulary in schema.sql. What it is NOT is said by the
            # event TYPE and by its archive destination -- a request decides
            # nothing and is never an economic-change event.
            kind, cls = ("service_requested"
                         if act["kind"] == ACT_SERVICE_REQUEST
                         else "commissioning_requested"), "authority"
        elif RX_INSPECTION.search(d):
            kind, cls = "inspection", "operational"
        elif RX_ORDER_DECIDES.search(d):
            kind, cls = "material_order", "authority"
        if kind is None:
            continue
        assets = _linked_assets(f)
        if not assets:
            continue
        owners = sorted({ek for dk in (f.get("docket_bases") or [])
                         for ek, _a in DOCKET_ASSETS.get(dk, [])})
        destination, is_backfill, why = baseline.route(
            f["accession"], f.get("filed_date") or "",
            version_status=f.get("version_status") or "")
        if kind not in ECONOMIC_CHANGE_EVENTS and destination == elibrary.INVESTOR:
            destination = elibrary.ARCHIVE
            why = (f"{why} The event type '{kind}' is a filing occurrence, not an economic "
                   "change: a request, a report and an inspection letter are archive "
                   "entries however recent they are.")
        out.append({
            "event_id": "evt-" + hashlib.sha256(
                f"lng|{f['accession']}|{kind}".encode()).hexdigest()[:24],
            "entity_key": owners[0] if owners else key,
            "asset_ids": json.dumps(assets),
            "event_class": cls, "event_type": kind,
            "headline": d[:240],
            "detail": (f"accession {f['accession']}, dockets "
                       f"{','.join(f.get('docket_bases') or [])}; one filing linked to "
                       f"{len(assets)} asset(s) and {len(owners)} filing entit(y/ies) "
                       "without duplicating any capacity, metric or event. "
                       f"ROUTING: {why}"),
            "destination": destination,
            "source_system": SOURCE_SYSTEM, "filing_id": f["accession"],
            "accession_number": f["accession"], "document_id": f.get("document_id"),
            "docket": ",".join(f.get("docket_bases") or []),
            "reporting_date": f.get("filed_date"),
            "source_filed_date": f.get("filed_date"),
            "source_posted_date": f.get("posted_date"),
            "effective_date": None,
            "first_seen_at": baseline.first_seen(
                f["accession"], f.get("first_seen_at") or _now()),
            "is_backfill": is_backfill,
            "comparison_basis": "FERC eLibrary description and class/type only",
            "confidence_note": (
                "classified from the FERC-published description; the document body was not "
                "read for this classification. Operative reading: " + (act["why"] or "n/a")),
        })
    return out


# ---------------------------------------------------------------- helpers

def _typed(filings, cls, typ) -> list[dict]:
    return [f for f in filings if (cls, typ) in f.get("class_pairs", [])]


def _latest_first(rows) -> list[dict]:
    return sorted(rows, key=lambda o: o["instant_date"] or "", reverse=True)


def _docfact(entity_key, filing, assertion, metric_id, value_text, value_num, unit,
             qualifier, scope_note, char_start, char_end, verbatim, method,
             confidence) -> dict:
    acc = (filing or {}).get("accession", "")
    raw = f"{acc}|{assertion}|{char_start}|{char_end}|{value_text}"
    return {
        "document_fact_id": "dfact-" + hashlib.sha256(raw.encode()).hexdigest()[:28],
        "document_id": (filing or {}).get("document_id"),
        "source_system": SOURCE_SYSTEM, "filing_id": acc, "source_fact_id": None,
        "entity_key": entity_key, "assertion_type": assertion, "metric_id": metric_id,
        "value_text": value_text, "value_num": value_num, "unit": unit,
        "qualifier": qualifier, "scope_note": scope_note,
        "page": "", "paragraph": str(verbatim.count("\n")),
        "char_start": char_start, "char_end": char_end,
        "verbatim_span": verbatim,
        "extraction_method": method or "elibrary_description_span",
        "content_hash": (filing or {}).get("content_hash") or "",
        "confidence": confidence, "review_state": "", "reviewer_note": "",
        "first_seen_at": _now()}


def _account_for_remainder(entity_key, metrics, expected, produced) -> list[dict]:
    """Every frozen slot ends in a measured status with a reason."""
    have = {o["metric_id"] for o in produced}
    out = []
    for slot in expected:
        if slot["metric_id"] in have:
            continue
        m = metrics.get(slot["metric_id"])
        if m is None:
            continue
        if slot["requirement"] == coverage.NOT_REQUIRED:
            avail = Availability.NOT_APPLICABLE
            reason = slot["requirement_evidence"]
        else:
            avail = Availability.EXPECTED_NOT_LOCATED
            reason = ("the docket sweep and the verified source list produced no document "
                      "matching this assertion's route in the requested window; the route "
                      "itself is verified (see discovery/docs_discovery.md s.2.3), so this "
                      "is 'not located', not 'no source identified'")
        out.append(_obs(entity_key, m, instant="", value_text=None, value_num=None, unit=None,
                        availability=avail, validation=Validation.NOT_YET_VALIDATED,
                        missing_reason=reason, evidence=slot["requirement_evidence"]))
    return out
