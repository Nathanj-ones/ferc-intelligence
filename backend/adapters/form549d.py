"""
Form 549D adapter: the FERC Quarterly 311 Transaction Report.

Source: the Public FERC Data Platform (`https://api.data.ferc.gov/v1`), asset
"FERC Form 549D: Quarterly 311 Transaction Report". The asset carries two bulk
tables -- Respondent Information (one row per filing) and Shipper and Contract
Information (one row per contract/service line). There is NO data dictionary:
`dictionary/` 404s and `details/` reports `contains-dictionary: false`, so every
column meaning used here is inferred from the data plus the asset description
and is documented in `discovery/form549d_fields.csv`.

Everything this adapter publishes covers **FERC-reportable NGPA 311 / NGA
Hinshaw services only -- not necessarily the whole intrastate pipeline**. That
label is attached to every observation.

Design decisions that are load-bearing, each forced by a verified source fact:

  A. DATASET IDS ARE RESOLVED LIVE, EVERY RUN. 28 and 29 are hints, not
     identifiers. `_resolve()` reads the catalog and matches on asset and
     dataset TITLE; the ids it finds are recorded in
     `discovery/549d_resolution.json` with the pagination evidence.

  B. EACH TABLE IS FETCHED ONCE PER PROCESS AND SHARED BY ALL 28 ENTITIES.
     There is no server-side filtering (any query parameter other than
     api_key/limit/offset is HTTP 400), so a per-asset fetch would download
     113k rows 28 times. The content-addressed client cache makes a re-run free.

  C. COMPLETENESS IS PROVEN, NOT ASSUMED. Every refresh asserts
     `has_more == false`, `len(row_data) == details.row_count`, and re-reads the
     final row through `limit=1&offset=row_count-1` and compares it to the last
     row of the unpaged body. A silently short table can therefore never look
     like missing contracts.

  D. `Shipper_ID` IS NOT A SHIPPER. It is a globally unique surrogate ROW key
     (113,255 distinct over 113,255 rows). The natural key is
     `(Form549D_ID, Sequence_Number)`. Shippers are aggregated by legal NAME,
     filer-locally, before any concentration ranking, and identity coverage is
     published with the result.

  E. UNKNOWN AFFILIATE IS THE EMPTY STRING (22.9% of rows and rising). Blank
     never becomes "No", and unknown is never merged into non-affiliate.

  F. ANNUAL REVENUE LIVES IN THE Q4 REPORT AND IS TRANSPORTATION-ONLY BY THE
     FORM'S OWN DEFINITION. This was reverified against the official FERC text
     on 8 September 2026 rather than taken from the audit register; the sources
     are quoted in `ORDER_735A` and `FIELD_72` below. Three points settle it:

       * Order No. 735-A, 133 FERC para 61,216, Docket No. RM09-2-001 (issued
         16 December 2010), P 22: the Commission grants rehearing "and will
         revise 18 CFR 284.126(b)(1)(viii) and the analogous lines of Form No.
         549D so as to (1) collect per-customer revenue information only on an
         annual basis and (2) exclude storage revenues from the report."
       * The regulation as revised by that order reads: "(viii) Annual revenues
         received for each shipper, EXCLUDING revenues from storage services.
         ... and need only be reported every fourth quarter."
       * The Corrected Appendix to Order No. 735-A defines field 72
         (`Rev_GrandTotal`, the column this source exposes as `Total_Rev`) as
         "The total amount of annual revenue charged for services as permitted
         in the SOC for TRANSPORTATION service. [The field is sum of the
         amounts in fields #68-71.]" None of fields 68-71 is a storage
         component, and the same appendix instructs that respondents "are no
         longer required to report per-customer revenues in fields 68-72 for
         storage services".

     So the SCOPE question is not an open choice between two readings: the
     field is annual and transportation-only as a matter of FERC definition,
     and excluding Storage rows from the transportation total is the definition
     being applied, not an interpretation being chosen. What remains genuinely
     open is narrower and is NOT resolved here:

       * WHY a filer put a figure on a Storage row -- misreporting into a field
         that no longer collects storage revenue, or a deliberate disclosure --
         is not determinable from the filing. The amount is preserved as filed.
       * The storage-labelled amount is NOT a storage revenue total for the
         entity. Form 549D stopped collecting per-customer storage revenue;
         complete storage revenue lives in the semi-annual storage reports
         under 18 CFR 284.126(c)(5). Presenting the residue as storage revenue
         would overstate what it is.
       * The GRAIN question (below, point H) is untouched by any of this. The
         Order fixes what the field means, not how many rows a filer wrote.

  F2. TWO POPULATIONS, BOTH LABELLED, NEITHER A TARGET. The audit reproduced a
     historical table-wide population (544 Q4 Storage rows, $237,017,275, all
     filing occurrences 2011-2025) and an included-universe subset (179 rows,
     $39,299,027, 2024-2025). These are AUDITED BASELINE POPULATIONS to be
     reproduced and labelled. They are NOT targets: if a corrected run produces
     different figures, both are reported and the difference explained. See
     `AUDITED_POPULATIONS` and `compare_storage_population()`.

  G. `Total_Rev` IS NOT THE SUM OF ITS COMPONENTS (773 Q4 mismatches). The
     reported total is the total; components are reconciled AGAINST it, never
     summed INTO it.

  H. GRAIN IS PROBED PER (FILER, PERIOD). Where a contract group cannot be
     shown to be distinct service legs, the aggregate is BLOCKED and both
     treatments are retained as labelled audit scenarios. There is deliberately
     NO "same amount means duplicate" rule and NO divergence threshold: a
     percentage gap is not evidence about which reading is right.

     A grain-gated aggregate carries `Availability.INTERPRETATION_BLOCKED`:
     the source was retrieved and is provably complete, and only its MEANING is
     open. That is a different statement from `UNVERIFIED_AVAILABILITY`, which
     this adapter reserves for point I below -- there we cannot even tell what
     the source is saying. The two states are held apart by one invariant, and
     `tests/test_549d.py` enforces it:

         validation == BLOCKED_AMBIGUITY  <=>  availability == INTERPRETATION_BLOCKED

  I. 0 IS NOT A MEASURED ZERO. Every numeric column in this source is 0-filled
     and never null, so a filed zero and an omitted figure are indistinguishable
     at the cell level. An aggregate whose every input is 0 is therefore
     published as unverified with its arithmetic sum recorded as evidence --
     never as a measured zero, and always as `UNVERIFIED_AVAILABILITY` --
     never as `INTERPRETATION_BLOCKED`, because the open question there is what
     the source SAYS, not what it MEANS.

Grain note: a tabular source has no XBRL facts, so one `source_facts` row is one
SOURCE ROW, stored verbatim as JSON under
`source_fact_id = "{Form549D_ID}:{Sequence_Number}"`. Lineage edges name the
column and the cell value they consumed, so per-cell provenance survives.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
import statistics
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ferclib import coverage, periods                                       # noqa: E402
from ferclib.http import FetchError, OfflineCacheMiss, api_key, redact      # noqa: E402
from ferclib.http import is_offline as http_is_offline                      # noqa: E402
from ferclib.registry import BY_ADAPTER, REGISTRY_VERSION                   # noqa: E402
from ferclib.staging import observation_id                                  # noqa: E402
from ferclib.status import Availability, Method, Origin, Validation, VersionStatus  # noqa: E402

from adapters import oil_index                                              # noqa: E402

ADAPTER = "form549d"
SOURCE_SYSTEM = "DataFERC"
FORM = "Form 549D"
REGIME = "Form 549D"                       # ferclib.registry.F549D
BASE = "https://api.data.ferc.gov/v1"
HERE = pathlib.Path(__file__).resolve().parent.parent

#: attached to EVERY observation this adapter emits
SCOPE_LABEL = ("FERC-reportable NGPA 311/Hinshaw services - not necessarily the whole "
               "intrastate pipeline")
ZERO_FILL_NOTE = ("this source 0-fills every numeric column and never nulls it, so a filed "
                  "zero and an omitted figure are indistinguishable at the cell level")

QUARTER_METRICS = ("i311_reporting_state", "i311_billed_transport_usage",
                   "i311_storage_determinants", "i311_firm_share",
                   "i311_top5_shipper_share", "i311_affiliate_activity",
                   "i311_contract_expiry", "i311_component_rates",
                   "i311_discounts_and_schedules", "i311_points_decoded")
ANNUAL_METRICS = ("i311_annual_transport_revenue", "i311_revenue_components",
                  "i311_revenue_grain_diagnostic")

REV_COMPONENTS = ("Reservation_Annual_Rev", "Annual_Vol_Annual_Rev",
                  "Useage_Annual_Rev", "Other_Services_Annual_Rev")

# =========================================================================
# VERIFIED FERC SOURCE (A16). Retrieved and quoted 8 September 2026 from the
# official texts, NOT taken from the audit register's bare "paragraph 22"
# reference and NOT from any task description.
# =========================================================================

ORDER_735A = {
    "order": "Order No. 735-A",
    "title": "Contract Reporting Requirements of Intrastate Natural Gas Companies",
    "citation": "133 FERC para 61,216",
    "docket": "RM09-2-001",
    "issued": "2010-12-16",
    "action": "Order on Rehearing",
    "effective": "2011-04-01",
    "retrieved_from": "https://www.ferc.gov/sites/default/files/2020-06/OrderNo.735-A.pdf",
    "retrieved_on": "2026-09-08",
    "p15_quote": (
        "the Commission eliminates the increased per-customer revenue reporting "
        "requirements by requiring such revenues to be reported only on an annual "
        "basis and excluding storage revenues from the report."),
    "p22_quote": (
        "We grant rehearing in part on this issue, and will revise 18 CFR "
        "284.126(b)(1)(viii) and the analogous lines of Form No. 549D so as to "
        "(1) collect per-customer revenue information only on an annual basis and "
        "(2) exclude storage revenues from the report. This will return the "
        "per-customer revenue reporting requirement to the status quo before "
        "Order No. 735."),
    "regulation_as_revised": (
        "18 CFR 284.126(b)(1)(viii): Annual revenues received for each shipper, "
        "excluding revenues from storage services. The report should separately "
        "state revenues received under each component, and need only be reported "
        "every fourth quarter."),
    "storage_revenue_lives_here_instead": (
        "18 CFR 284.126(c)(5) (section 311/Hinshaw) and 18 CFR 284.13(e)(5) "
        "(interstate): per-customer storage revenue is collected in the "
        "SEMI-ANNUAL storage reports, not on Form 549D. P 22: 'Both sets of "
        "pipelines will continue to be required to report per-customer revenues "
        "for storage services in the semi-annual storage reports'."),
}

#: The Corrected Appendix to Order No. 735-A -- the Form 549D data dictionary.
FIELD_72 = {
    "source": "Corrected Appendix, Form No. 549D in Docket No. RM09-2-001",
    "retrieved_from": "https://www.ferc.gov/sites/default/files/2020-04/errata-appendix-order-735a.pdf",
    "retrieved_on": "2026-09-08",
    "field_no": 72,
    "field_id": "Rev_GrandTotal",
    "field_name": "Total Revenues",
    "source_column": "Total_Rev",
    "definition_quote": (
        "The total amount of annual revenue charged for services as permitted in "
        "the SOC for transportation service. [The field is sum of the amounts in "
        "fields #68-71.]"),
    "components_quote": {
        68: "annual revenue from the peak day reservation",
        69: "annual revenue from the annual volume contract reservation rate",
        70: "The amount of annual revenue from gas transported.",
        71: "The amount of revenue from other transportation service charges.",
    },
    "recent_changes_quote": (
        "As a result of Order No. 735-A, Form No. 549D has been modified as "
        "follows: ... iii. Respondents are no longer required to report "
        "per-customer revenues in fields 68-72 for storage services. For all "
        "other transportation services, Respondents are now only required to "
        "report per-customer revenues on an annual basis in the form filed for "
        "the fourth quarter (due every March 1)."),
    # The single most load-bearing quote for the Q4-usage gate:
    "annual_revenue_is_independent_of_q4_volumes": (
        "Fields 68-71 instructions: 'If there are no volumes for the current "
        "transaction in the 4th quarter, you must enter annual revenues and "
        "leave blank Fields 33 and 54.' FERC therefore requires annual revenue "
        "to be reported even when the fourth quarter carries no volumes at all. "
        "Q4 usage availability is consequently NEITHER necessary NOR sufficient "
        "evidence about annual-revenue coverage."),
}

#: Baseline populations the 8 September audit measured. Reproduced and LABELLED.
#: Explicitly NOT targets -- see header note F2.
AUDITED_POPULATIONS = {
    "historical_all_occurrences": {
        "definition": "Q4; Service_Type case-insensitively equals Storage; "
                      "non-zero Total_Rev; ALL historical filing occurrences",
        "window": "2011-Q4 .. 2025-Q4",
        "rows": 544,
        "revenue": "237017275",
        "is_a_target": False,
        "meaning": "a table-wide historical anomaly count. It is NOT current-"
                   "universe revenue and NOT any entity's storage revenue.",
    },
    "included_universe_2024_2025": {
        "definition": "the same rule restricted to the included entity universe "
                      "and to 2024-2025",
        "window": "2024-Q4 .. 2025-Q4",
        "rows": 179,
        "revenue": "39299027",
        "is_a_target": False,
        "meaning": "the subset that the delivered universe actually covers.",
    },
    "relationship": ("The two populations are nested, not alternative estimates "
                     "of one quantity. Quoting the 544-row total beside a "
                     "current-universe figure invites exactly the confusion the "
                     "audit flagged, so both are always labelled with window and "
                     "occurrence rule."),
    "if_a_rerun_disagrees": ("Report BOTH the audited figure and the recomputed "
                            "figure with the difference explained. NEVER adjust "
                            "the computation to reproduce the audited number: "
                            "these are observations of a baseline, not targets."),
}

#: Policy-blocker identity. The 8 September register carried TWO rows for one
#: policy because the table-wide dollar figure was embedded in the SUMMARY, and
#: `open_blocker` hashes (adapter, scope, summary) into the blocker_id -- so
#: editing that sentence minted a second blocker for the same policy question.
#: The summary is now a FIXED sentence; every volatile figure lives in
#: exact_error, which is not part of the identity.
#: Stable machine-readable token in `qa_flags` marking a filing that reports
#: Total_Rev outside transportation. Prose describing WHY may be sharpened as
#: source evidence improves; this token is the keying contract and must not
#: change with it. Paired with the `order_735a_scope_divergence` event, which
#: carries the same condition in the events table.
SCOPE_DIVERGENCE_MARKER = "[ORDER_735A_SCOPE_DIVERGENCE]"

POLICY_BLOCKER_SCOPE = "549D annual revenue scope"
POLICY_BLOCKER_SUMMARY = (
    "Form 549D field 72 is transportation-only by definition, yet filers report "
    "Total_Rev on Storage rows; a display policy is required for the as-filed "
    "out-of-scope amounts")
#: Blocker ids from the 8 September register that this consolidation replaces.
#: These are POLICY duplicates. Source-row blockers are never consolidated.
SUPERSEDED_POLICY_BLOCKERS = ("blk-c29e1a69a09275d1", "blk-df2155105afc957e")
DETERMINANTS = [
    ("Usage_BU", "Usage_BU_Descriptor",
     "quarterly billed TRANSPORTATION usage; effectively transportation-only "
     "(12 of 15,365 storage rows table-wide)"),
    ("Reservation_BU", "Reservation_BU_Descriptor",
     "reservation/demand determinant; GENUINELY SPANS BOTH transportation firm demand and "
     "storage contracted capacity, so it is always reported with its Service_Type and time base"),
    ("Injection_BU", "Injection_BU_Descriptor",
     "STORAGE + PARKING injection; a different physical quantity from withdrawal and never "
     "summed with it"),
    ("Withdrawal_BU", "Withdrawal_BU_Descriptor",
     "STORAGE + PARKING withdrawal; never summed with injection"),
    ("Park__SLASH__Loan_BU", "Park__SLASH__Loan_BU_Descriptor", "park/loan determinant, day-1 tier"),
    ("Park__SLASH__Loan_BU__DASH__Day_2", "Park__SLASH__Loan_BU_Descriptor",
     "park/loan determinant, day-2 tier; a SEPARATE tier, not a duplicate of day 1"),
    ("Annual_Volume_BU", "Annual_Volume_BU_Descriptor",
     "ANNUAL contract quantity sitting on a quarterly row; NEVER summed across the four "
     "quarters of a year"),
]
RATE_COLUMNS = [
    ("Reservation_Rate", "Reservation_Rate_Descriptor", "Discounted_Reservation_Rates"),
    ("Annual_Volume_Rate", "Annual_Volume_Rate_Descriptor", "Discounted_Annual_Volume_Rates"),
    ("Usage_Rate", "Usage_Rate_Descriptor", "Discounted_Usage_Rates"),
    ("Injection_Rate", "Injection_Rate_Descriptor", "Discounted_Injection_Rates"),
    ("Withdrawal_Rate", "Withdrawal_Rate_Descriptor", "Discounted_Withdrawal_Rates"),
    ("Park__SLASH__Loan_Rate", "Park__SLASH__Loan_Rate_Descriptor",
     "Discounted_Park__SLASH__Loan_Rate"),
    ("Park__SLASH__Loan_Rate__DASH__Day_2", "Park__SLASH__Loan_Rate_Descriptor",
     "Discounted_Park__SLASH__Loan_Rates__DASH__Day_2"),
]
POINT_COLUMNS = [
    ("receipt", "Receipt_Point_Name", "Receipt_Point_Common_Code",
     "Add__APOS__l_Receipt_Point", "Add__APOS__l_Point_Common_Code"),
    ("delivery", "Delivery_Point_Name", "Delivery_Point_Common_Code",
     "Add__APOS__l_Delivery_Point", "Add__APOS__l_Delivery_Point_Common_Code"),
]

#: fields whose variation inside one (filing, contract) group PROVES the rows are
#: distinct service legs rather than one contract reported once per shipper
SERVICE_FIELDS = ("Service_Type", "Character_of_Service", "Service_Rate_Schedule",
                  "Other_Services_Description", "Docket_Current_Rate",
                  "Receipt_Point_Name", "Delivery_Point_Name",
                  "Receipt_Point_Common_Code", "Delivery_Point_Common_Code",
                  "Add__APOS__l_Receipt_Point", "Add__APOS__l_Delivery_Point",
                  "Reservation_Rate", "Annual_Volume_Rate", "Usage_Rate", "Injection_Rate",
                  "Withdrawal_Rate", "Park__SLASH__Loan_Rate",
                  "Park__SLASH__Loan_Rate__DASH__Day_2")
#: fields that identify the counterparty or the contract term. Variation here alone
#: does NOT distinguish legs -- it is exactly the ambiguous case.
IDENTITY_FIELDS = ("Shipper_Name", "Filer_Proprietary_Shipper_ID", "Affiliate_Status",
                   "Contract_Begin", "Contract_End")


# ================================================================== primitives

def _s(v) -> str:
    return str(v if v is not None else "").strip()


def _f(v):
    """Numeric cell -> float, or None when it is not a number. Never defaults to 0."""
    t = _s(v).replace(",", "").replace("$", "")
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _f0(v) -> float:
    x = _f(v)
    return 0.0 if x is None else x


def _pct(part: float, whole: float):
    return None if not whole else round(100.0 * part / whole, 4)


_SERVICE_CANON = {"TRANSPORTATION": "Transportation", "STORAGE": "Storage",
                  "PARKING/LENDING": "Parking/Lending", "OTHER": "Other",
                  "OTHER (SPECIFY)": "Other (Specify)"}


def norm_service(v) -> str:
    """Case-normalised Service_Type. '' means the filer left it blank -- UNKNOWN
    service scope, never silently treated as transportation."""
    return _SERVICE_CANON.get(_s(v).upper(), _s(v))


def norm_character(v) -> str:
    t = _s(v).upper()
    return "Firm" if t == "FIRM" else "Interruptible" if t == "INTERRUPTIBLE" else ""


def norm_affiliate(v) -> str:
    """Yes/No/unknown. The EMPTY STRING is how this form represents unknown --
    there is no 'Unknown' token -- and it is 22.9% of rows and growing."""
    t = _s(v).upper()
    if t in ("Y", "YES"):
        return "yes"
    if t in ("N", "NO"):
        return "no"
    return "unknown"


_UNIT_BASE = {"MMBTU": "MMBtu|Dth", "DTH": "MMBtu|Dth", "DEKATHERM": "MMBtu|Dth",
              "DEKATHERMS": "MMBtu|Dth", "MCF": "Mcf", "TH": "th", "THERM": "th",
              "THERMS": "th"}


def unit_family(descriptor) -> str:
    """(energy unit, time base) as one comparable key.

    MMBtu and Dth are definitionally the same energy quantity (1 Dth = 1 MMBtu)
    and are the only pair merged. Mcf is a VOLUME and `th` is 1/10 MMBtu; both
    would need a conversion this source does not supply, so they stay separate.
    The `-day` / `-mo.` suffix is a TIME BASE, not a unit, and a per-day quantity
    is never added to a period total.
    """
    t = _s(descriptor)
    if not t:
        return "(unit not reported)"
    base, tb = t, ""
    m = re.search(r"[-\s](day|mo\.?|month|yr|year)$", t, re.I)
    if m:
        base, tb = t[:m.start()], "-" + m.group(1).lower().rstrip(".")
    return _UNIT_BASE.get(base.strip().upper(), base.strip()) + tb


def natural_key(row) -> str:
    """`(Form549D_ID, Sequence_Number)` -- the verified natural key of one
    contract/service row. `Shipper_ID` is NOT used: it is a surrogate row key."""
    return f"{_s(row.get('Form549D_ID'))}:{_s(row.get('Sequence_Number'))}"


def shipper_key(row) -> str:
    """Filer-LOCAL legal-shipper key, built from the legal NAME.

    There is no FERC shipper CID in Form 549D. `Filer_Proprietary_Shipper_ID` is
    the filer's own number and is not stable across filers, and `Shipper_ID` is a
    row key. So contracts are aggregated by normalised legal name WITHIN one
    filer, and the identity is never asserted to be the same shipper at another
    filer.
    """
    n = re.sub(r"[^A-Z0-9 ]+", " ", _s(row.get("Shipper_Name")).upper())
    n = re.sub(r"\b(LLC|L L C|LP|L P|INC|CORP|CORPORATION|COMPANY|CO|LTD|HOLDINGS)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def parse_dockets(v) -> list[str]:
    """`Docket_Current_Rate` is a LIST: 'PR03-17-000, PR10-13-000' and
    'Docket No. PR24-95-000' both occur. Unmatched text is preserved verbatim."""
    t = _s(v)
    if not t:
        return []
    found = re.findall(r"\b((?:PR|ST|SA|RP|CP)-?\d{2,4}-\d+(?:-\d+)?)\b", t, re.I)
    if found:
        return sorted({x.upper().replace("PR-", "PR") for x in found})
    return [t]


def parse_ym(v):
    """`YYYY/MM` contract dates. 2100/12 is the evergreen sentinel and is kept
    as such, never treated as a real expiry date."""
    m = re.match(r"^\s*(\d{4})[/-](\d{1,2})\s*$", _s(v))
    return (int(m.group(1)), int(m.group(2))) if m else None


def parse_mdy(v) -> str:
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})", _s(v))
    return f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else ""


def split_period(v) -> tuple[int, str]:
    m = re.match(r"^\s*(\d{4})-(Q[1-4])\s*$", _s(v))
    return (int(m.group(1)), m.group(2)) if m else (0, "")


def is_placeholder(row) -> bool:
    """The `Performed_Transport_This_Quarter = No` placeholder: every descriptive
    field blank and every numeric field 0. It is filing METADATA, not a contract."""
    if any(_s(row.get(c)) for c in ("Shipper_Name", "Contract_Number", "Service_Type",
                                    "Receipt_Point_Name", "Delivery_Point_Name",
                                    "Docket_Current_Rate", "Service_Rate_Schedule")):
        return False
    cols = [c for c, _d, _n in DETERMINANTS] + [c for c, _d, _x in RATE_COLUMNS] \
        + list(REV_COMPONENTS) + ["Total_Rev"]
    return not any(_f0(row.get(c)) for c in cols)


# ============================================================ table resolution

_LOCK = threading.Lock()
_TABLES: dict | None = None


def classify_retrieval_failure(exc, *, request_attempted: bool | None = None) -> dict:
    """Name the real cause of a retrieval failure. A local fault is never an outage.

    `oil_index.classify_failure` holds the shared taxonomy; this wrapper decides
    whether a request was actually issued. A `FetchError` whose url is the
    literal `<FERC_API_KEY>` sentinel was raised by `ferclib.http.api_key`
    BEFORE any socket was opened, so nothing is known about FERC's availability.
    """
    # Classify the exception TYPE before falling back to its prose.  The real
    # OfflineCacheMiss detail says an object is "not in the source cache"; it
    # does not happen to contain the literal word "miss" expected by the shared
    # text classifier.  Hand-written classifier tests therefore passed while
    # the production exception took the generic FERC-unavailable branch.
    if isinstance(exc, OfflineCacheMiss):
        return oil_index.classify_failure(
            f"OfflineCacheMiss: cache miss for {exc.url}; {exc.detail}",
            request_attempted=False,
            offline=True,
        )
    if request_attempted is None:
        url = _s(getattr(exc, "url", ""))
        request_attempted = not (url.startswith("<") and url.endswith(">"))
    return oil_index.classify_failure(
        exc, request_attempted=request_attempted, offline=http_is_offline())


def _url(path: str, **params) -> str:
    """Build a request URL. The key is read at the point of use and the URL is
    never logged, returned or stored except through redact()."""
    q = "".join(f"{k}={v}&" for k, v in params.items())
    return f"{BASE}{path}?{q}api_key={api_key()}"


def _resolve(ctx) -> dict:
    """Resolve the 549D dataset ids from the LIVE catalog by title.

    28 / 29 are today's ids, not permanent identifiers, so nothing here is
    hard-coded: the asset is found by title and its two datasets by theirs.
    """
    assets = ctx.client.get_json(_url("/data-assets/"), source_system=SOURCE_SYSTEM)["data-assets"]
    hit = [a for a in assets if "549D" in _s(a.get("title")).upper()]
    if not hit:
        raise FetchError(f"{BASE}/data-assets/", "no Form 549D asset in the live catalog")
    asset = hit[0]
    ids = {}
    for d in asset["data-sets"]:
        t = _s(d.get("title")).upper()
        if "RESPONDENT" in t:
            ids["respondent"] = d["id"]
        elif "SHIPPER" in t or "CONTRACT" in t:
            ids["contract"] = d["id"]
        ids.setdefault("_titles", {})[d["id"]] = d.get("title")
    missing = [k for k in ("respondent", "contract") if k not in ids]
    if missing:
        raise FetchError(f"{BASE}/data-assets/",
                         f"549D asset resolved but dataset(s) {missing} not found by title; "
                         f"datasets present: {ids.get('_titles')}")
    reg = None
    for a in assets:
        for d in a["data-sets"]:
            if "IDENTIFIER LISTING" in _s(d.get("title")).upper() or (
                    "REGISTRATION" in _s(a.get("title")).upper()
                    and "IDENTIFIER" in _s(d.get("title")).upper()):
                reg = d["id"]
    return {"asset_title": asset.get("title"), "contact": asset.get("point_of_contact_email"),
            "respondent_id": ids["respondent"], "contract_id": ids["contract"],
            "registration_id": reg, "dataset_titles": ids["_titles"],
            "asset_count": len(assets)}


def _fetch_table(ctx, dsid: int, *, strict: bool = True) -> tuple[list[dict], dict]:
    """Fetch one bulk table completely, and PROVE it is complete.

    A single unpaged GET returns the whole table; the proof that nothing is
    missing is three independent checks, all recorded:
      1. `has_more == false` on the unpaged body;
      2. row count equals the server's own `details.row_count`;
      3. the last row re-read through `limit=1&offset=row_count-1` is identical
         to the last row of the unpaged body.
    Checks 1 and 2 always raise on failure. Check 3 additionally proves that the
    server's row ORDER is stable across calls; that is required of the two 549D
    tables and is only advisory for the supporting company-registration lookup,
    whose order is NOT stable (verified live) and which is never used as a
    quantity.
    """
    det = ctx.client.get_json(_url(f"/dataset/{dsid}/details/"), source_system=SOURCE_SYSTEM)
    declared = int(det.get("row_count") or 0)
    md = (det.get("metadata") or [{}])[0]
    data_url = _url(f"/dataset/{dsid}/data/")          # key read once, here
    body = ctx.client.get_json(data_url, source_system=SOURCE_SYSTEM)
    rows = body.get("row_data") or []
    ev = {"dataset_id": dsid, "declared_row_count": declared, "fetched_rows": len(rows),
          "has_more": bool(body.get("has_more")),
          "last_updated": md.get("last-updated"),
          "contains_dictionary": md.get("contains-dictionary"),
          "url": redact(data_url)}
    if ev["has_more"]:
        raise FetchError(ev["url"], f"dataset {dsid}: has_more=true on the unpaged fetch; "
                                    "the table is short and must not be used")
    if len(rows) != declared:
        raise FetchError(ev["url"], f"dataset {dsid}: fetched {len(rows)} rows but details/ "
                                    f"declares row_count={declared}")
    if declared:
        tail = ctx.client.get_json(_url(f"/dataset/{dsid}/data/", limit=1, offset=declared - 1),
                                   source_system=SOURCE_SYSTEM)
        probe = (tail.get("row_data") or [None])[0]
        ev["boundary_probe_offset"] = declared - 1
        ev["boundary_probe_matches_last_row"] = (probe == rows[-1])
        ev["row_order_stable_across_calls"] = ev["boundary_probe_matches_last_row"]
        if not ev["boundary_probe_matches_last_row"]:
            if strict:
                raise FetchError(ev["url"],
                                 f"dataset {dsid}: boundary probe at offset {declared - 1} "
                                 "does not reproduce the last unpaged row")
            ev["note"] = ("row order is NOT stable across calls for this dataset; the count "
                          "checks still hold, and this table is used only as a per-CID lookup "
                          "where order is irrelevant")
    ev["complete"] = True
    return rows, ev


def _tables(ctx) -> dict:
    """Fetch BOTH bulk tables ONCE per process and share them across all 28
    entities. Never per asset: there is no server-side filtering, so a per-entity
    fetch would download 113k rows 28 times."""
    global _TABLES
    with _LOCK:
        if _TABLES is not None:
            return _TABLES
        res = _resolve(ctx)
        ctx.log("info", f"549D catalog resolved live: asset {res['asset_title']!r}; "
                        f"respondent=dataset {res['respondent_id']}, "
                        f"contract=dataset {res['contract_id']}, "
                        f"registry=dataset {res['registration_id']} "
                        f"({res['asset_count']} assets in the catalog)", adapter=ADAPTER)
        ds28, ev28 = _fetch_table(ctx, res["respondent_id"])
        ds29, ev29 = _fetch_table(ctx, res["contract_id"])
        reg_rows, ev26 = ([], {})
        if res["registration_id"] is not None:
            try:
                reg_rows, ev26 = _fetch_table(ctx, res["registration_id"], strict=False)
            except FetchError as exc:
                ctx.log("warn", f"company registration table unavailable: {exc.detail}",
                        adapter=ADAPTER)

        torn = ev28.get("last_updated") != ev29.get("last_updated")
        if torn:
            ctx.staging.open_blocker(
                ADAPTER, "source",
                "the two 549D tables carry different last-updated stamps: a torn snapshot",
                scope="datasets", exact_error=f"{ev28.get('last_updated')} vs "
                                              f"{ev29.get('last_updated')}",
                human_decision=True)

        by_filing: dict[str, list[dict]] = {}
        for r in ds29:
            by_filing.setdefault(_s(r.get("Form549D_ID")), []).append(r)
        for v in by_filing.values():
            v.sort(key=lambda r: int(_f0(r.get("Sequence_Number"))))
        collected = sorted({_s(r.get("Filing_Year__DASH__Quarter")) for r in ds28})

        _TABLES = {
            "resolution": res, "evidence": {"respondent": ev28, "contract": ev29,
                                            "registration": ev26},
            "torn_snapshot": torn,
            "snapshot": ev29.get("last_updated") or ev28.get("last_updated") or "",
            "ds28": ds28, "rows_by_filing": by_filing,
            "registry": {_s(r.get("CID")).upper(): r for r in reg_rows if _s(r.get("CID"))},
            "collected_periods": collected,
            # Resolved ONCE, already redacted. Recording provenance on a filing
            # must never require the credential: a per-filing api_key() lookup
            # reads the shared .env hundreds of times per run for no network
            # call, and a transient read failure then fails an entity that had
            # nothing wrong with it.
            "source_url": ev29["url"],
        }
        _write_resolution_evidence(ctx, _TABLES)
        ctx.log("info", "549D completeness proven: "
                        + "; ".join(f"dataset {e['dataset_id']} {e['fetched_rows']:,} rows "
                                    f"= declared {e['declared_row_count']:,}, has_more=False, "
                                    f"boundary probe "
                                    f"{'matches' if e.get('boundary_probe_matches_last_row') else 'n/a'}"
                                    for e in (ev28, ev29)), adapter=ADAPTER)
        return _TABLES


def _write_resolution_evidence(ctx, t: dict) -> pathlib.Path:
    """Record what was resolved and how completeness was proven. URLs are
    redacted by construction; no credential can reach this file.

    This is a run output, never an input-tree mutation. Requiring the context's
    declared output root keeps clean Build B and worker runs from writing into
    the immutable extracted candidate.
    """
    out = {"recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "adapter": ADAPTER, "source_system": SOURCE_SYSTEM,
           "resolution": t["resolution"], "pagination_evidence": t["evidence"],
           "torn_snapshot": t["torn_snapshot"],
           "collected_periods": t["collected_periods"],
           "note": ("dataset ids are resolved from the live catalog by asset and dataset "
                    "title on every run; they are not permanent identifiers")}
    p = ctx.output_dir / "discovery" / "549d_resolution.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=1, sort_keys=True), encoding="utf-8")
    return p


# ================================================================== retrieval

def retrieve(ctx, entity, *, year_from: int, year_to: int) -> list[dict]:
    """Persist every 549D filing for one entity, from the shared bulk tables."""
    t = _tables(ctx)
    cid = _s(entity["entity_key"]).upper()
    heads = [h for h in t["ds28"]
             if _s(h.get("Filer_CID")).upper() == cid
             and year_from <= split_period(h.get("Filing_Year__DASH__Quarter"))[0] <= year_to]
    if not heads:
        ctx.log("info", f"{cid}: no Form 549D filings in {year_from}-{year_to}", adapter=ADAPTER)
        return []

    # Resubmissions REPLACE the prior filing. Dedupe on Submission_Date -- the
    # Original/Resubmission flag alone cannot break the tie, because a duplicated
    # (CID, period) has been seen with BOTH rows flagged 'Original'. Every
    # occurrence is still written; only canonicality differs.
    by_period: dict[str, list[dict]] = {}
    for h in heads:
        by_period.setdefault(_s(h.get("Filing_Year__DASH__Quarter")), []).append(h)

    kept: list[dict] = []
    for period_label, group in sorted(by_period.items()):
        group.sort(key=lambda h: (parse_mdy(h.get("Submission_Date")),
                                  _s(h.get("Form549D_ID"))))
        canonical_id = _s(group[-1].get("Form549D_ID"))
        for h in group:
            fid = _s(h.get("Form549D_ID"))
            rows = t["rows_by_filing"].get(fid, [])
            scope_key = f"{FORM}:{period_label}:{fid}:{t['snapshot']}"
            rec = _filing_record(ctx, entity, h, rows, t, fid == canonical_id, len(group))
            if ctx.staging.is_done(ADAPTER, cid, scope_key) and not ctx.force:
                kept.append(rec)
                continue
            ctx.staging.checkpoint(ADAPTER, cid, scope_key, "in_progress")
            try:
                _persist(ctx, entity, rec, h, rows, t)
            except Exception as exc:                                        # noqa: BLE001
                ctx.staging.checkpoint(ADAPTER, cid, scope_key, "failed", error=str(exc)[:400])
                # A16: two 549D blockers in the 8 September register read
                # "FERC_API_KEY is not set in the environment or .env" while
                # being filed as kind="source" -- a missing local credential
                # presented as a FERC source failure. Classify honestly: a
                # configuration fault says nothing whatever about FERC.
                cls = classify_retrieval_failure(exc)
                ctx.staging.open_blocker(
                    ADAPTER, cls["kind"],
                    f"{cid} {period_label}: filing {fid} not persisted -- {cls['summary']}",
                    scope=scope_key,
                    exact_error=(f"[{cls['classification']}] {str(exc)[:280]}"
                                 + (f" || {cls['explicitly_not']}" if cls["explicitly_not"]
                                    else "")
                                 + f" || remedy: {cls['remedy']}"))
                ctx.log("error", f"{cid} {period_label}: {type(exc).__name__}: "
                                 f"[{cls['classification']}] {exc}",
                        adapter=ADAPTER, entity_cid=cid)
                continue
            kept.append(rec)
            ctx.staging.checkpoint(ADAPTER, cid, scope_key, "done")
    return kept


def _filing_record(ctx, entity, head, rows, t, canonical: bool, n_occurrences: int) -> dict:
    fid = _s(head.get("Form549D_ID"))
    year, quarter = split_period(head.get("Filing_Year__DASH__Quarter"))
    start, end = periods.quarter_interval(year, quarter) if quarter else ("", "")
    payload = json.dumps({"header": head, "rows": rows}, sort_keys=True,
                         ensure_ascii=False).encode("utf-8")
    return {
        "source_system": SOURCE_SYSTEM, "filing_id": fid, "entity_key": entity["entity_key"],
        "form": FORM, "accession_number": None,
        "reporting_year": year, "reporting_period": quarter,
        "period_start": start, "period_end": end,
        "filed_date": None, "posted_date": None, "issued_date": None, "effective_date": None,
        "submitted_on": parse_mdy(head.get("Submission_Date")) or None,
        # Data.FERC calls this dataset metadata field ``last-updated``.  It is
        # the version timestamp for the two bulk tables, not an occurrence or
        # reporting as-of date.  Keep it in ``retrieved_at`` and in the pinned
        # applicability/source-version identity below, but never put a
        # timestamp (or a fabricated date truncated from it) in snapshot_date.
        "snapshot_date": None,
        "acceptance_status": _s(head.get("Original__SLASH__Resubmission")).title() or None,
        "taxonomy_version": _applicability_version(t), "schema_ref": None,
        "content_hash": hashlib.sha256(payload).hexdigest(),
        "is_canonical": int(canonical),
        "canonical_reason": ("latest Submission_Date for this (Filer_CID, period); "
                             f"{n_occurrences} occurrence(s) retained"
                             if canonical else
                             "superseded by a later Submission_Date for the same "
                             "(Filer_CID, period); retained as occurrence history"),
        "version_status": VersionStatus.UNRESOLVED, "supersedes_filing_id": None,
        "data_origin": "structured_bulk",
        "retrieved_at": t["snapshot"], "first_seen_at": None,
        "source_url": t["source_url"],
        "_header": head, "_rows": rows,
        "_declared_state": _s(head.get("Performed_Transport_This_Quarter")),
    }


def _persist(ctx, entity, rec, head, rows, t) -> None:
    fid = rec["filing_id"]
    vstat, supersedes = ctx.staging.classify_version(
        SOURCE_SYSTEM, entity["entity_key"], FORM, rec["reporting_year"],
        rec["reporting_period"], fid, rec["content_hash"])
    rec["version_status"] = vstat
    rec["supersedes_filing_id"] = supersedes
    filing = {k: v for k, v in rec.items() if not k.startswith("_")}

    tax = _applicability_version(t)
    facts = [{
        "source_system": SOURCE_SYSTEM, "filing_id": fid,
        "source_fact_id": f"{fid}:header", "document_order": 0,
        "concept_qname": f"dataFERC:{t['resolution']['respondent_id']}.RespondentInformation",
        "concept_local": "Form549D_RespondentHeader", "context_id": None, "unit_id": None,
        "unit_text": "", "decimals": None, "precision": None,
        "value_as_filed": json.dumps(head, sort_keys=True, ensure_ascii=False),
        "is_nil": 0, "period_class": periods.QUARTER, "instant": None,
        "period_start": rec["period_start"], "period_end": rec["period_end"],
        "duration_days": str(periods.days_between(rec["period_start"], rec["period_end"]) or ""),
        "current_or_prior": "current", "explicit_dims_json": "", "typed_dims_json": "",
        "taxonomy_version": tax}]
    for order, r in enumerate(rows, 1):
        facts.append({
            "source_system": SOURCE_SYSTEM, "filing_id": fid,
            "source_fact_id": natural_key(r), "document_order": order,
            "concept_qname": f"dataFERC:{t['resolution']['contract_id']}.ShipperAndContract",
            "concept_local": "Form549D_ShipperContractRow", "context_id": None, "unit_id": None,
            "unit_text": unit_family(r.get("Usage_BU_Descriptor")),
            "decimals": None, "precision": None,
            "value_as_filed": json.dumps(r, sort_keys=True, ensure_ascii=False),
            "is_nil": int(is_placeholder(r)), "period_class": periods.QUARTER, "instant": None,
            "period_start": rec["period_start"], "period_end": rec["period_end"],
            "duration_days": str(periods.days_between(rec["period_start"],
                                                      rec["period_end"]) or ""),
            "current_or_prior": "current",
            "explicit_dims_json": json.dumps(
                {"Service_Type": norm_service(r.get("Service_Type")),
                 "Character_of_Service": norm_character(r.get("Character_of_Service")),
                 "Affiliate_Status": norm_affiliate(r.get("Affiliate_Status"))}),
            "typed_dims_json": json.dumps(
                {"Contract_Number": _s(r.get("Contract_Number")),
                 "Shipper_Name": _s(r.get("Shipper_Name")),
                 "Sequence_Number": _s(r.get("Sequence_Number"))}),
            "taxonomy_version": tax})

    dockets = sorted({d for r in rows for d in parse_dockets(r.get("Docket_Current_Rate"))})
    ctx.staging.write_filing_bundle(
        filing, facts=facts,
        filing_dockets=[{"source_system": SOURCE_SYSTEM, "filing_id": fid, "docket": d}
                        for d in dockets])
    ctx.log("info", f"{fid} {FORM} {rec['reporting_year']}{rec['reporting_period']} "
                    f"({vstat}, declared '{rec['_declared_state']}'): {len(rows)} contract rows",
            adapter=ADAPTER, entity_cid=entity["entity_key"])


def _applicability_version(t: dict) -> str:
    r = t["resolution"]
    return (f"549D-ds{r['respondent_id']}/{r['contract_id']}@{t['snapshot']}")


# =================================================================== expected

def freeze_expected(ctx, entity, filings: list[dict], assets: list[dict]) -> list[dict]:
    """Freeze the requested grid from the PERIODS FERC HAS COLLECTED, not from
    what this entity filed.

    A quarter that exists in the respondent table but carries no row for this
    filer is a genuine coverage gap, and it can only be seen if the denominator
    is built independently of the filer's own filings.
    """
    t = _tables(ctx)
    metrics = {m.id: m for m in BY_ADAPTER[ADAPTER]}
    asset_id = assets[0]["asset_id"] if assets else ""
    template = assets[0]["template"] if assets else "intrastate_549d"
    yf = getattr(ctx.args, "year_from", None) or min(
        (f["reporting_year"] for f in filings), default=dt.date.today().year)
    yt = getattr(ctx.args, "year_to", None) or max(
        (f["reporting_year"] for f in filings), default=dt.date.today().year)
    tax = _applicability_version(t)

    wanted = [p for p in t["collected_periods"] if yf <= split_period(p)[0] <= yt]
    q4_years = sorted({split_period(p)[0] for p in wanted if p.endswith("Q4")})
    out, seen = [], set()

    for label in wanted:
        year, quarter = split_period(label)
        for mid in QUARTER_METRICS:
            m = metrics.get(mid)
            if m is None:
                continue
            if mid == "i311_reporting_state":
                req = coverage.REQUIRED
                ev = (f"the respondent table has collected {label} for all filers, so the "
                      "reporting state of any 549D respondent is determinable for it")
            else:
                req = coverage.CONDITIONAL
                ev = ("required when the respondent reports 311/Hinshaw activity for the "
                      "quarter; a filed 'no reportable activity' declaration is a valid "
                      "state with nothing to report, not a gap")
            slot = coverage.build_expected(entity["entity_key"], asset_id, template, m,
                                           REGIME, periods.QUARTER, year, quarter,
                                           req, ev, tax, ctx.staging.run_id)
            if slot["slot_id"] not in seen:
                seen.add(slot["slot_id"])
                out.append(slot)

    for year in q4_years:
        for mid in ANNUAL_METRICS:
            m = metrics.get(mid)
            if m is None:
                continue
            ev = ("annual customer revenue is carried in the Q4 report (Order 735-A); "
                  f"the respondent table has collected {year}-Q4, so an annual figure is "
                  "requested of every respondent that filed reportable transportation "
                  "service in that Q4")
            slot = coverage.build_expected(entity["entity_key"], asset_id, template, m,
                                           REGIME, periods.ANNUAL, year, "Q4",
                                           coverage.CONDITIONAL, ev, tax, ctx.staging.run_id)
            if slot["slot_id"] not in seen:
                seen.add(slot["slot_id"])
                out.append(slot)
    return out


# ================================================================ grain probe

def probe_grain(rows: list[dict]) -> dict:
    """Detect the reporting grain of one (filer, period).

    Contract groups holding more than one row are the whole question. A group is
    SERVICE LEGS when some service-defining field differs -- different service
    type, points, rate schedule, docket or rate -- because then the rows describe
    different billable things and summing them is right. A group in which only
    the counterparty or the contract term differs is AMBIGUOUS: it is equally
    consistent with (A) one contract whose value was written once per shipper and
    (B) two genuine relationships under one contract number, and this source does
    not distinguish them.

    There is deliberately no 'same amount means duplicate' rule and no divergence
    threshold. Both are excluded because a value coincidence and a percentage gap
    are not evidence about which reading is correct.
    """
    real = [r for r in rows if not is_placeholder(r)]
    groups: dict[str, list[dict]] = {}
    blank_cn = 0
    for r in real:
        cn = _s(r.get("Contract_Number")).upper()
        if not cn:
            blank_cn += 1
            continue
        groups.setdefault(cn, []).append(r)
    legs, ambiguous, amb_rows = [], [], {}
    for cn, g in sorted(groups.items()):
        if len(g) < 2:
            continue
        varies = [f for f in SERVICE_FIELDS if len({_s(x.get(f)).upper() for x in g}) > 1]
        if varies:
            legs.append({"contract": cn, "rows": len(g), "distinguished_by": varies[:5]})
        else:
            ivar = [f for f in IDENTITY_FIELDS if len({_s(x.get(f)).upper() for x in g}) > 1]
            ambiguous.append({"contract": cn, "rows": len(g),
                              "varies_only_in": ivar or ["nothing examined"],
                              "row_keys": [natural_key(x) for x in g]})
            amb_rows[cn] = g
    klass = ("ambiguous_contract_group" if ambiguous
             else "contract_x_service_leg" if legs
             else "one_row_per_contract")
    return {"grain_class": klass, "rows": len(rows), "contract_rows": len(real),
            "placeholder_rows": len(rows) - len(real),
            "distinct_contracts": len(groups), "blank_contract_number_rows": blank_cn,
            "service_leg_groups": legs, "ambiguous_groups": ambiguous,
            "resolved": not ambiguous, "_ambiguous_rows": amb_rows}


def grain_scenarios(grain: dict, rows: list[dict], column: str, row_filter=None) -> dict:
    """The two treatments a reader would otherwise have to choose between, kept as
    LABELLED AUDIT SCENARIOS -- never as confidence bounds on a single answer.

    Only the AMBIGUOUS groups are re-treated. Groups shown to be distinct service
    legs are resolved and are summed under both readings, so collapsing them too
    would manufacture a disagreement that the data does not contain.

    `treatments_coincide` is a STRUCTURAL test, not a tolerance: the two readings
    can differ at all only when an ambiguous group contributes more than one
    non-zero row to this column. No amounts are compared, so no 'same amount
    means duplicate' rule and no divergence threshold is involved -- and note the
    direction: two IDENTICAL non-zero values make the readings differ and BLOCK
    the aggregate, which is the opposite of the forbidden rule.
    """
    keep = (lambda r: not is_placeholder(r) and (row_filter is None or row_filter(r)))
    sel = [r for r in rows if keep(r)]
    row_sum = sum(_f0(r.get(column)) for r in sel)
    per_group, adjust, coincide = {}, 0.0, True
    for cn, g in sorted(grain.get("_ambiguous_rows", {}).items()):
        gs = [r for r in g if keep(r)]
        if not gs:
            continue
        vals = [_f0(r.get(column)) for r in gs]
        nonzero = sum(1 for v in vals if v)
        per_group[cn] = {"rows_in_scope": len(gs), "values": vals,
                         "rows_with_a_non_zero_value": nonzero}
        if nonzero > 1:
            coincide = False
            adjust += sum(vals) - max(vals)
    return {
        "column": column, "rows_in_scope": len(sel),
        "scenario_B_row_sum": row_sum,
        "scenario_A_one_value_per_ambiguous_contract": row_sum - adjust,
        "treatments_coincide": coincide,
        "per_ambiguous_group": per_group,
        "labels": {
            "scenario_B_row_sum": "reading B - every row is a distinct billable "
                                  "relationship under a shared contract number; sum the rows",
            "scenario_A_one_value_per_ambiguous_contract":
                "reading A - the contract's figure was written once per shipper; take one "
                "value per ambiguous contract",
            "treatments_coincide": "structural: true only when every ambiguous group "
                                   "contributes at most one non-zero row to this column, so "
                                   "the two readings cannot give different totals"},
    }


def sign_split(sel: list[dict], column: str) -> dict:
    """Positive and negative sides of a signed column, kept apart.

    Several 549D determinants are SIGNED and the form does not label the sign:
    `Park/Loan_BU-Day_2` carries 7,009 negative rows table-wide (the loan
    direction against the park direction), `Usage_BU` 119 and `Total_Rev` 73
    (credits and reversals). Netting opposite directions into one denominator
    understates the base and can push a share above 100%, so the two sides are
    always reported separately and a share is taken of the GROSS POSITIVE side.
    """
    vals = [_f0(r.get(column)) for r in sel]
    pos = sum(v for v in vals if v > 0)
    neg = sum(v for v in vals if v < 0)
    return {"positive": pos, "negative": neg, "net": pos + neg,
            "rows_positive": sum(1 for v in vals if v > 0),
            "rows_negative": sum(1 for v in vals if v < 0),
            "rows_zero": sum(1 for v in vals if not v)}


def select_weight(real: list[dict]) -> dict | None:
    """Pick the ONE billed determinant a share may be weighted by, and name it.

    Concentration and affiliate shares need a weight. Billed transportation usage
    is preferred whenever the filer reports any, because it is the metric the
    registry names. A filer that reports only storage or parking/lending service
    has no transportation usage at all, and substituting one determinant for
    another silently would merge scopes -- so instead a SINGLE determinant is
    chosen, restricted to one service type and one unit family, and its identity
    travels with every number derived from it. Determinants are never summed
    together to build a weight.
    """
    cands = []
    for col, desc, _note in DETERMINANTS:
        buckets: dict[tuple, list[dict]] = {}
        for r in real:
            if not _f0(r.get(col)):
                continue
            svc = norm_service(r.get("Service_Type")) or "(blank)"
            if col == "Usage_BU" and svc != "Transportation":
                continue           # Usage_BU is transportation-only by evidence
            buckets.setdefault((svc, unit_family(r.get(desc))), []).append(r)
        for (svc, unit), sel in buckets.items():
            sg = sign_split(sel, col)
            cands.append({"column": col, "service_type": svc, "unit": unit, "sel": sel,
                          "rows": len(sel), "sum": sg["net"], "signs": sg,
                          "gross_positive": sg["positive"],
                          "preferred": col == "Usage_BU" and svc == "Transportation"})
    cands = [c for c in cands if c["gross_positive"] > 0]
    if not cands:
        return None
    best = sorted(cands, key=lambda c: (not c["preferred"], -c["rows"],
                                        -c["gross_positive"], c["column"]))[0]
    best["label"] = (f"{best['column']} on {best['service_type']} rows in {best['unit']} "
                     f"(gross positive {best['gross_positive']:,.0f} over "
                     f"{best['signs']['rows_positive']} rows"
                     + (f"; {best['signs']['rows_negative']} negative rows totalling "
                        f"{best['signs']['negative']:,.0f} reported separately, NOT netted "
                        "into the denominator" if best["signs"]["rows_negative"] else "")
                     + ")")
    return best


def attribution_ambiguous(grain: dict, column: str, field: str, row_filter=None) -> list[dict]:
    """Groups where the unresolved grain changes WHICH counterparty/term a
    non-zero weight belongs to. A single non-zero row in a group whose shipper
    name differs is still ambiguous for a concentration or affiliate split, even
    though the TOTAL is unaffected."""
    keep = (lambda r: not is_placeholder(r) and (row_filter is None or row_filter(r)))
    out = []
    for cn, g in sorted(grain.get("_ambiguous_rows", {}).items()):
        gs = [r for r in g if keep(r)]
        if not gs or not any(_f0(r.get(column)) for r in gs):
            continue
        vals = sorted({_s(r.get(field)) for r in gs})
        if len(vals) > 1:
            out.append({"contract": cn, "field": field, "competing_values": vals})
    return out


# ================================================================ observation

def _obs(entity_key, m, basis, start, end, instant, year, period, *, value_text, value_num,
         unit, availability, origin=Origin.DATAFERC_STRUCTURED, method=Method.DERIVED,
         version_status=VersionStatus.ORIGINAL, validation=Validation.PASS,
         qa_flags="", missing_reason="", filing_id="", source_fact_id=None,
         selector="", derivation="", tax="", applicability_evidence="",
         candidate_count=None, notes="") -> dict:
    label = f"FY{year}" if basis == periods.ANNUAL else f"{year}{period}"
    qa = [SCOPE_LABEL] + [q for q in (qa_flags.split(" || ") if qa_flags else []) if q]
    return {
        "observation_id": observation_id(entity_key, m.id, REGIME, basis, start, end,
                                         instant, m.scope, unit or "", method),
        "entity_key": entity_key, "metric_id": m.id, "source_regime": REGIME,
        "period_basis": basis, "period_start": start or None, "period_end": end or None,
        "instant_date": instant or None, "reporting_year": year, "reporting_period": period,
        "period_label": label, "scope": m.scope, "unit": unit,
        "value_text": value_text, "value_num": value_num, "normalized_iso": None,
        "availability": availability, "origin": origin, "method": method,
        "version_status": version_status, "validation": validation,
        "source_system": SOURCE_SYSTEM, "filing_id": filing_id or None,
        "source_fact_id": source_fact_id, "source_context_id": None,
        "document_id": None, "accession_number": None, "candidate_count": candidate_count,
        "selector": selector or m.selector, "derivation": derivation,
        "concept_local": "Form549D_ShipperContractRow", "concept_qname": None,
        "taxonomy_version": tax, "registry_version": REGISTRY_VERSION,
        "applicability_version": tax, "schedule_page": m.schedule or FORM,
        "taxonomy_label": None, "qa_flags": " || ".join(qa),
        "review_status": "open" if validation in Validation.MUST_PROPAGATE else "",
        "missing_reason": missing_reason, "applicability_evidence": applicability_evidence,
        "notes": notes,
    }


def _event(entity_key, assets, event_type, headline, detail, *, filing_id, reporting_date,
           is_backfill: bool, comparison_basis: str = "", confidence: str = "") -> dict:
    """A data-quality event, written through the single writer by run.py.

    These are conditions a human must see and decide about -- they are not
    observation values, and they must not be smuggled into one."""
    eid = "evt-" + hashlib.sha256(
        f"{ADAPTER}|{entity_key}|{event_type}|{filing_id}|{reporting_date}".encode()
    ).hexdigest()[:20]
    return {"event_id": eid, "entity_key": entity_key,
            "asset_ids": json.dumps(sorted(assets)), "event_class": "data_quality",
            "event_type": event_type, "headline": headline, "detail": detail,
            "destination": "data_review_queue", "source_system": SOURCE_SYSTEM,
            "filing_id": filing_id, "accession_number": None, "document_id": None,
            "docket": None, "reporting_date": reporting_date,
            "source_filed_date": None, "source_posted_date": None, "effective_date": None,
            "first_seen_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "is_backfill": int(is_backfill), "comparison_basis": comparison_basis,
            "confidence_note": confidence}


def _edge(obs_id, order, role, sign, filing_id, fact_id, concept, period, value, unit,
          vstat, obs_input=None) -> dict:
    # `input_population_id` is present on EVERY edge, defaulting to None, even
    # though only set-based edges use it. `Staging._upsert` derives its column
    # list from rows[0] alone, so a key missing from the first edge is silently
    # dropped from all of them -- which is exactly how the population FK went
    # NULL on 41 edges while the population rows themselves wrote fine. Keeping
    # the key on every edge makes the batch homogeneous and the write correct
    # regardless of ordering. See requests/w3-financial_shared_requests.md R9.
    return {"observation_id": obs_id, "input_order": order, "input_role": role,
            "operator_sign": sign, "coefficient": 1.0,
            "input_population_id": None,
            "input_source_system": SOURCE_SYSTEM, "input_filing_id": filing_id,
            "input_source_fact_id": fact_id, "input_observation_id": obs_input,
            "input_context_id": None, "input_concept": concept, "input_period": period,
            "input_value": value, "input_unit": unit, "input_version_status": vstat}


# ===================================================== set-based lineage (A08)

def _populations_are_persisted() -> bool:
    """Is the `lineage_populations` writer wired yet?

    `ferclib/schema.sql` defines the table, but `Staging.commit_unit` and
    `run.py` do not yet unpack a `_populations` key, so a population row would
    never be written. Emitting an edge whose `input_population_id` points at a
    row that does not exist would create exactly the dangling reference that
    A18 is about -- so until the writer lands, the edge carries the population
    INLINE instead of by reference. Nothing is lost and nothing dangles.

    See `requests/w3-financial_shared_requests.md`, request 3.
    """
    import inspect
    try:
        from ferclib.staging import Staging
        sig = inspect.signature(Staging.commit_unit)
        return "populations" in sig.parameters
    except Exception:                                                  # noqa: BLE001
        return False


_PERSIST_POPULATIONS = _populations_are_persisted()


def _population(obs_id, *, filing_ids, inclusion, exclusion, members,
                candidates, member_key="Form549D_ID:Sequence_Number",
                aggregate_value="", aggregate_unit="", empty_reason="",
                note="", source_table="d549_rows") -> tuple[dict, dict]:
    """Persist the SOURCE POPULATION behind an aggregate, and one edge to it.

    A08: "A ZERO MUST HAVE A SUPPORTED POPULATION. 'We retrieved nothing' is
    not zero." Four of this adapter's derived metrics built their edges by
    comprehension over a row selection, so an empty selection produced an
    observation with NO lineage at all -- 52 of them in the audited baseline.
    A share of zero then looked identical to a share nobody measured.

    The distinction this makes checkable:

        row_count = 0, candidate_count = N  -> N rows were considered and none
                                               qualified. That IS a zero.
        row_count = 0, candidate_count = 0  -> nothing was retrieved. That is
                                               NOT a zero and must not be
                                               published as one.

    `exclusion` is required even when nothing was excluded, because a set
    described only by what it kept cannot be audited for what it wrongly kept.
    """
    keys = sorted(natural_key(r) for r in members)
    digest = hashlib.sha256("|".join(keys).encode("utf-8")).hexdigest()
    pid = "pop-" + hashlib.sha256(
        f"{obs_id}|{member_key}|{digest}|{candidates}".encode("utf-8")).hexdigest()[:24]
    if not members and not empty_reason:
        empty_reason = (f"{candidates} candidate row(s) were considered and none "
                        "satisfied the inclusion rule"
                        if candidates else
                        "NO rows were retrieved for this scope, so this is an "
                        "ABSENCE OF DATA and not a measured zero")
    pop = {
        "population_id": pid,
        "observation_id": obs_id,
        "source_system": SOURCE_SYSTEM,
        "source_table": source_table,
        "filing_ids": json.dumps(sorted({str(f) for f in filing_ids})),
        "inclusion_rule": inclusion,
        "exclusion_rule": exclusion,
        "row_count": len(members),
        "candidate_count": candidates,
        "excluded_count": max(candidates - len(members), 0),
        "member_key": member_key,
        "member_digest": digest,
        "members_sample": json.dumps(keys[:25]),
        "aggregate_value": str(aggregate_value),
        "aggregate_unit": aggregate_unit,
        "empty_reason": empty_reason,
        "note": note,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    # The edge references the population row ONLY when that row will actually be
    # written. Otherwise the population travels inline, so an aggregate always
    # has traversable lineage and never points at a row that does not exist.
    inline = json.dumps({k: pop[k] for k in
                         ("inclusion_rule", "exclusion_rule", "row_count",
                          "candidate_count", "excluded_count", "member_key",
                          "member_digest", "members_sample", "aggregate_value",
                          "aggregate_unit", "empty_reason", "filing_ids")},
                        sort_keys=True)
    edge = {"observation_id": obs_id, "input_order": 0, "input_role": "population",
            "operator_sign": "", "coefficient": 1.0,
            "input_source_system": SOURCE_SYSTEM,
            "input_filing_id": (sorted({str(f) for f in filing_ids}) or [None])[0],
            "input_source_fact_id": None, "input_observation_id": None,
            "input_context_id": None, "input_concept": member_key,
            "input_period": None,
            "input_value": (str(aggregate_value) if _PERSIST_POPULATIONS else inline),
            "input_unit": aggregate_unit, "input_version_status": None,
            "input_population_id": (pid if _PERSIST_POPULATIONS else None)}
    return pop, edge


# ============================================ A16 invariants, asserted in code

def transport_rows(rows: list[dict]) -> list[dict]:
    """The ONLY definition of the transportation population used anywhere here."""
    return [r for r in rows if norm_service(r.get("Service_Type")) == "Transportation"]


def storage_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if norm_service(r.get("Service_Type")) == "Storage"]


def assert_transport_excludes_storage(sel: list[dict]) -> None:
    """No Storage row may reach a transportation total. Definitional, not policy.

    Form 549D field 72 is "annual revenue charged for services as permitted in
    the SOC for TRANSPORTATION service", summing fields 68-71, none of which is
    a storage component; and 18 CFR 284.126(b)(1)(viii) excludes storage
    revenues outright. A Storage row inside a transportation selection is
    therefore a defect, never a judgement call.
    """
    leaked = [natural_key(r) for r in sel
              if norm_service(r.get("Service_Type")) != "Transportation"]
    if leaked:
        raise AssertionError(
            "non-transportation rows reached a 549D transportation selection: "
            f"{leaked[:8]}{'...' if len(leaked) > 8 else ''}. "
            + ORDER_735A["regulation_as_revised"])


def assert_revenue_not_inferred_from_usage(rev_rows, usage_rows, published) -> None:
    """Annual revenue may never be derived from, or gated on, Q4 usage.

    FERC's own instruction for fields 68-71 is explicit that the two are
    independent: "If there are no volumes for the current transaction in the
    4th quarter, you must enter annual revenues and leave blank Fields 33 and
    54." A published annual revenue whose only support is Q4 usage would invert
    that instruction.
    """
    if published and not rev_rows:
        raise AssertionError(
            "an annual transportation revenue figure was published with NO row "
            f"carrying a non-zero annual Total_Rev, while {len(usage_rows)} row(s) "
            "carry Q4 usage. Q4 usage is not evidence of annual revenue. "
            + FIELD_72["annual_revenue_is_independent_of_q4_volumes"])


def compare_storage_population(rows: int, revenue) -> dict:
    """Label a recomputed storage population against the audited baseline.

    Never coerces agreement. A difference is reported with both figures, which
    is the whole point: the audited numbers are observations of a baseline, not
    a target the computation should be bent to reproduce.
    """
    base = AUDITED_POPULATIONS["historical_all_occurrences"]
    same = (int(rows) == base["rows"]
            and str(int(float(revenue))) == base["revenue"])
    return {
        "audited_baseline_rows": base["rows"],
        "audited_baseline_revenue": base["revenue"],
        "recomputed_rows": int(rows),
        "recomputed_revenue": str(revenue),
        "agrees": same,
        "population_definition": base["definition"],
        "window": base["window"],
        "is_a_target": False,
        "note": ("agrees with the audited baseline" if same else
                 "DIFFERS from the audited baseline. Both figures are reported "
                 "and the difference must be explained. The computation is NOT "
                 "adjusted to reproduce the baseline: "
                 + AUDITED_POPULATIONS["if_a_rerun_disagrees"]),
    }


# ================================================================ canonicalise

def canonicalise(ctx, entity, filings: list[dict], expected: list[dict]) -> tuple[list, list]:
    t = _tables(ctx)
    entity_key = entity["entity_key"]
    asset_ids = [a["asset_id"] for a in entity.get("assets", [])]
    latest_period = t["collected_periods"][-1] if t["collected_periods"] else ""
    metrics = {m.id: m for m in BY_ADAPTER[ADAPTER]}
    tax = _applicability_version(t)
    observations: list[dict] = []
    edges: list[dict] = []

    canonical = {f"{f['reporting_year']}-{f['reporting_period']}": f
                 for f in filings if f["is_canonical"]}
    torn_flag = ("SNAPSHOT WARNING: the respondent and contract tables carry different "
                 "last-updated stamps; this read may be torn") if t["torn_snapshot"] else ""
    reg = t["registry"].get(_s(entity_key).upper(), {})
    if reg:
        torn_flag = " || ".join(filter(None, [
            torn_flag,
            f"company registry (dataset {t['resolution']['registration_id']}) resolves this "
            f"CID to {_s(reg.get('Organization_Name'))!r} with Program "
            f"{_s(reg.get('Program'))!r} -- the authoritative regime label, taken from the "
            "registry by CID and never from the 549D Filer_Name, which is not an identity "
            "(319 name strings for 164 CIDs, and filers have filed under another company's "
            "name)"]))

    q_slots = [s for s in expected if s["period_basis"] == periods.QUARTER]
    a_slots = [s for s in expected if s["period_basis"] == periods.ANNUAL]

    by_period: dict[str, dict] = {}
    for slot in q_slots:
        label = f"{slot['reporting_year']}-{slot['reporting_period']}"
        by_period.setdefault(label, {})[slot["metric_id"]] = slot

    for label, slots in sorted(by_period.items()):
        filing = canonical.get(label)
        try:
            obs, eds = _quarter(ctx, entity_key, metrics, slots, label, filing, tax, torn_flag,
                                asset_ids, latest_period)
        except Exception as exc:                                            # noqa: BLE001
            ctx.staging.open_blocker(ADAPTER, "source",
                                     f"{entity_key} {label}: quarter canonicalisation failed",
                                     scope=f"{entity_key}:{label}", exact_error=str(exc)[:400])
            ctx.log("error", f"{entity_key} {label}: {type(exc).__name__}: {exc}",
                    adapter=ADAPTER, entity_cid=entity_key)
            continue
        observations.extend(obs)
        edges.extend(eds)

    for year in sorted({s["reporting_year"] for s in a_slots}):
        slots = {s["metric_id"]: s for s in a_slots if s["reporting_year"] == year}
        filing = canonical.get(f"{year}-Q4")
        try:
            obs, eds = _annual(ctx, entity_key, metrics, slots, year, filing, tax, torn_flag,
                               asset_ids, latest_period)
        except Exception as exc:                                            # noqa: BLE001
            ctx.staging.open_blocker(ADAPTER, "source",
                                     f"{entity_key} FY{year}: annual canonicalisation failed",
                                     scope=f"{entity_key}:{year}", exact_error=str(exc)[:400])
            ctx.log("error", f"{entity_key} FY{year}: {type(exc).__name__}: {exc}",
                    adapter=ADAPTER, entity_cid=entity_key)
            continue
        observations.extend(obs)
        edges.extend(eds)

    observations.extend(_account_for_remainder(entity_key, metrics, expected, observations, tax))
    return observations, edges


def _absent(entity_key, m, slot, tax, *, availability, reason, validation, qa="") -> dict:
    return _obs(entity_key, m, slot["period_basis"], slot.get("period_start") or "",
                slot.get("period_end") or "", slot.get("instant_date") or "",
                slot["reporting_year"], slot["reporting_period"],
                value_text=None, value_num=None, unit=None, availability=availability,
                method=Method.FILED, validation=validation, missing_reason=reason,
                qa_flags=qa, tax=tax, applicability_evidence=slot["requirement_evidence"])


# -------------------------------------------------------------- quarter block

def _quarter(ctx, entity_key, metrics, slots, label, filing, tax, torn,
             asset_ids, latest_period) -> tuple[list, list]:
    year, quarter = split_period(label)
    start, end = periods.quarter_interval(year, quarter)
    obs: list[dict] = []
    edges: list[dict] = []

    def slot(mid):
        return slots.get(mid)

    # ------------------------------------------------ no filing located at all
    if filing is None:
        for mid, s in slots.items():
            m = metrics[mid]
            obs.append(_absent(
                entity_key, m, s, tax, availability=Availability.SOURCE_BLANK,
                validation=Validation.NOT_YET_VALIDATED,
                reason=(f"the respondent table has collected {label} and contains NO row for "
                        "this CID. The fetch was proven complete against the server's own "
                        "row_count, so this is an absent filing in the SOURCE, not a retrieval "
                        "failure of ours. Whether it is a missed filing or no obligation "
                        "cannot be determined from Form 549D alone; it is not proof that "
                        "contracts terminated and it is not 'pipeline inactive'."),
                qa=("no filing-due determination is made: ferclib.periods.DEADLINES carries no "
                    "Form 549D rule, so 'overdue' is not asserted")))
        return obs, edges

    rows = filing["_rows"]
    fid = filing["filing_id"]
    vstat = filing["version_status"]
    declared = filing["_declared_state"]
    real = [r for r in rows if not is_placeholder(r)]
    grain = probe_grain(rows)
    unresolved = not grain["resolved"]
    grain_note = ""
    if unresolved:
        grain_note = ("UNRESOLVED GRAIN: " + "; ".join(
            f"contract {g['contract']} has {g['rows']} rows differing only in "
            f"{', '.join(g['varies_only_in'])}" for g in grain["ambiguous_groups"])
            + ". These rows cannot be shown to be distinct service legs, so reading A "
              "(one contract's figure written once per shipper) and reading B (several "
              "genuine relationships under one contract number) are both consistent with "
              "this filing. Whether that changes a particular aggregate is decided "
              "structurally per measure -- never by comparing amounts and never by a "
              "divergence threshold.")
    is_transport = (lambda r: norm_service(r.get("Service_Type")) == "Transportation")

    def common(extra=""):
        bits = [f"declared reporting state: '{declared or 'not stated'}'",
                f"filing {fid}, {len(rows)} contract rows "
                f"({grain['placeholder_rows']} all-zero placeholder rows excluded)",
                f"grain: {grain['grain_class']}"]
        if torn:
            bits.append(torn)
        if extra:
            bits.append(extra)
        return " || ".join(bits)

    def row_edges(obs_id, sel, role, concept, sign="+", first=None):
        out = list(first or [])
        for i, r in enumerate(sel, len(out) + 1):
            out.append(_edge(obs_id, i, role, sign, fid, natural_key(r), concept,
                             f"{start}..{end}", _s(r.get(concept)),
                             unit_family(r.get("Usage_BU_Descriptor")), vstat))
        return out

    # ------------------------------------------------------- reporting state
    s = slot("i311_reporting_state")
    if s is not None:
        m = metrics["i311_reporting_state"]
        if declared.upper() == "NO":
            state, avail = "no_reportable_activity_declared", Availability.PRESENT
            reason = ""
        elif declared.upper() == "YES":
            state, avail = "activity_reported", Availability.PRESENT
            reason = ""
        else:
            state, avail = "declaration_absent", Availability.SOURCE_BLANK
            reason = "the filing carries no Performed_Transport_This_Quarter declaration"
        extra = []
        if state == "no_reportable_activity_declared":
            extra.append("a declared 'no reportable activity' filing is a VALID FILING STATE "
                         "and must never be displayed as 'no filing' or 'pipeline inactive'")
        if state == "activity_reported" and not real:
            extra.append(f"INCONSISTENT FILING: activity is declared but all {len(rows)} "
                         "contract rows are all-zero placeholders")
        if state == "activity_reported" and grain["placeholder_rows"]:
            extra.append(f"{grain['placeholder_rows']} of {len(rows)} rows are all-zero, "
                         "all-blank placeholders and are excluded from every aggregate")
        offq = sum(_f0(r.get("Total_Rev")) for r in rows) if quarter != "Q4" else 0.0
        if offq:
            extra.append(f"FILER ERROR TO SURFACE: this non-Q4 filing reports ${offq:,.0f} of "
                         "Total_Rev. Annual revenue is read only from the Q4 report and this "
                         "figure is NOT summed into it")
        o = _obs(entity_key, m, periods.QUARTER, start, end, "", year, quarter,
                 value_text=state, value_num=None, unit="categorical", availability=avail,
                 method=Method.FILED, version_status=vstat,
                 validation=Validation.PASS if avail == Availability.PRESENT
                 else Validation.NOT_YET_VALIDATED,
                 qa_flags=common(" || ".join(extra)), missing_reason=reason,
                 filing_id=fid, source_fact_id=f"{fid}:header",
                 tax=tax, applicability_evidence=s["requirement_evidence"])
        if offq:
            o["_events"] = [_event(
                entity_key, asset_ids, "off_quarter_annual_revenue",
                f"{entity_key} reports ${offq:,.0f} of annual Total_Rev in a non-Q4 "
                f"Form 549D filing ({label})",
                ("Order 735-A carries annual customer revenue in the Q4 report. This "
                 f"{quarter} filing populates Total_Rev anyway on "
                 f"{sum(1 for r in rows if _f0(r.get('Total_Rev')))} row(s). The figure is "
                 "surfaced as a filer reporting condition and is NOT added to the annual "
                 "revenue metric, which reads the Q4 filing only."),
                filing_id=fid, reporting_date=end,
                is_backfill=label != latest_period,
                comparison_basis="quarterly filing vs the Q4-only annual revenue rule",
                confidence="verified from the filed row values")]
        obs.append(o)

    # --------------- nothing more to report when no activity was declared
    if not real:
        why = ("the respondent declared no reportable 311/Hinshaw activity for this quarter; "
               "the filing's only contract rows are all-zero placeholders, which are filing "
               "metadata and not contracts"
               if declared.upper() == "NO" else
               "the filing contains no non-placeholder contract rows")
        for mid, sl in slots.items():
            if mid == "i311_reporting_state":
                continue
            obs.append(_absent(entity_key, metrics[mid], sl, tax,
                               availability=Availability.NOT_APPLICABLE,
                               validation=Validation.PASS, reason=why,
                               qa=common("missing rows do not prove contracts terminated")))
        return obs, edges

    transport = [r for r in real if norm_service(r.get("Service_Type")) == "Transportation"]
    svc_mix = {}
    for r in real:
        svc_mix[norm_service(r.get("Service_Type")) or "(blank - unknown service scope)"] = \
            svc_mix.get(norm_service(r.get("Service_Type")) or
                        "(blank - unknown service scope)", 0) + 1

    # Whether an unresolved grain actually changes a given aggregate is decided
    # per measure and structurally. Totals move only when an ambiguous group
    # holds more than one non-zero row of that column; an attribution split
    # (which shipper, which affiliate flag, which expiry) is uncertain as soon as
    # the group carries any weight at all and the field disagrees inside it.
    usage_scen = grain_scenarios(grain, rows, "Usage_BU", is_transport)
    usage_blocked = unresolved and not usage_scen["treatments_coincide"]
    expiry_amb = attribution_ambiguous(grain, "Usage_BU", "Contract_End")

    # The share metrics need ONE named billed weight. Transportation usage is
    # preferred; a storage- or parking-only filer has none, and rather than
    # merging determinants a single determinant is chosen and named.
    weight = select_weight(real)
    w_keys = {natural_key(r) for r in (weight or {}).get("sel", [])}
    w_filter = (lambda r: natural_key(r) in w_keys)
    w_col = (weight or {}).get("column", "Usage_BU")
    w_scen = grain_scenarios(grain, rows, w_col, w_filter) if weight else usage_scen
    w_blocked = unresolved and not w_scen["treatments_coincide"]
    shipper_amb = attribution_ambiguous(grain, w_col, "Shipper_Name", w_filter)
    affiliate_amb = attribution_ambiguous(grain, w_col, "Affiliate_Status", w_filter)
    coincide_note = ("the grain of this filing is unresolved, but every ambiguous contract "
                     "group contributes at most one non-zero row to this column, so reading A "
                     "and reading B give the SAME total and the figure is published. This is a "
                     "structural fact about the rows, not a tolerance and not a judgement that "
                     "the grain question has been settled")

    # ------------------------------------------- billed transportation usage
    usage_obs = None
    s = slot("i311_billed_transport_usage")
    if s is not None:
        m = metrics["i311_billed_transport_usage"]
        if not transport:
            obs.append(_absent(entity_key, m, s, tax, availability=Availability.NOT_APPLICABLE,
                               validation=Validation.PASS,
                               reason=("the filed quarter contains no transportation-service "
                                       f"rows; service mix is {svc_mix}"),
                               qa=common("Usage_BU is transportation-only; this filer's "
                                         "activity is reported under other service types "
                                         "and appears in i311_storage_determinants")))
        else:
            fams: dict[str, dict] = {}
            for r in transport:
                u = unit_family(r.get("Usage_BU_Descriptor"))
                d = fams.setdefault(u, {"sum": 0.0, "rows": 0, "nonzero": 0, "sel": []})
                d["sum"] += _f0(r.get("Usage_BU"))
                d["rows"] += 1
                d["nonzero"] += 1 if _f0(r.get("Usage_BU")) else 0
                d["sel"].append(r)
            multi = len(fams) > 1
            usage_cov = _pct(sum(1 for r in transport if _f0(r.get("Usage_BU"))), len(transport))
            blocked = usage_blocked
            for unit, d in sorted(fams.items(), key=lambda kv: (-kv[1]["rows"], kv[0])):
                qa = [f"denominator: {d['rows']} transportation rows in this unit family",
                      f"Q4/quarter usage coverage: {d['nonzero']}/{d['rows']} rows carry a "
                      f"non-zero Usage_BU ({usage_cov}% of all transportation rows). "
                      "This is USAGE coverage only and says nothing about annual revenue "
                      "coverage, which is measured separately on the Q4 report",
                      "Usage_BU is transportation-only; injection, withdrawal, park/loan and "
                      "reservation determinants are reported separately and are never merged "
                      "into this figure"]
                if multi:
                    qa.append("UNIT WARNING: more than one unit family is present and they are "
                              "NOT summed together: "
                              + "; ".join(f"{k}={v['sum']:,.0f} over {v['rows']} rows"
                                          for k, v in sorted(fams.items())))
                if unit == "(unit not reported)":
                    qa.append("the filer left Usage_BU_Descriptor blank on these rows; the "
                              "quantity is reported with an explicitly unknown unit and is "
                              "never merged with a stated unit")
                if unresolved:
                    qa.append(grain_note)
                    qa.append("audit scenarios (labelled treatments, NOT confidence bounds): "
                              + json.dumps(usage_scen, sort_keys=True))
                    if not blocked:
                        qa.append(coincide_note)
                if blocked:
                    o = _obs(entity_key, m, periods.QUARTER, start, end, "", year, quarter,
                             value_text=None, value_num=None, unit=unit,
                             availability=Availability.INTERPRETATION_BLOCKED,
                             version_status=vstat, validation=Validation.BLOCKED_AMBIGUITY,
                             qa_flags=common(" || ".join(qa)), filing_id=fid,
                             missing_reason=("the reporting grain of this quarter is "
                                             "unresolved AND an ambiguous contract group "
                                             "contributes more than one non-zero Usage_BU, so "
                                             "the two readings give different totals and "
                                             "neither is asserted"),
                             derivation="sum(Usage_BU) over transportation rows",
                             tax=tax, candidate_count=d["rows"],
                             applicability_evidence=s["requirement_evidence"])
                elif d["nonzero"] == 0:
                    qa.append(f"arithmetic row sum over {d['rows']} rows is 0; {ZERO_FILL_NOTE}, "
                              "so this is NOT published as a measured zero")
                    o = _obs(entity_key, m, periods.QUARTER, start, end, "", year, quarter,
                             value_text=None, value_num=None, unit=unit,
                             availability=Availability.UNVERIFIED_AVAILABILITY,
                             version_status=vstat, validation=Validation.NOT_YET_VALIDATED,
                             qa_flags=common(" || ".join(qa)), filing_id=fid,
                             missing_reason=("every Usage_BU on the transportation rows of "
                                             f"this quarter is 0; {ZERO_FILL_NOTE}"),
                             derivation="sum(Usage_BU) over transportation rows",
                             tax=tax, candidate_count=d["rows"],
                             applicability_evidence=s["requirement_evidence"])
                else:
                    sg = sign_split(d["sel"], "Usage_BU")
                    if sg["rows_negative"]:
                        qa.append(
                            f"SIGNED COLUMN: {sg['rows_negative']} row(s) carry a NEGATIVE "
                            f"Usage_BU totalling {sg['negative']:,.0f} against "
                            f"{sg['positive']:,.0f} positive. The value published is the NET "
                            "sum exactly as filed; the form does not label what a negative "
                            "billed usage means (backhaul, a correction or a reversal), so it "
                            "is neither dropped nor made positive")
                    o = _obs(entity_key, m, periods.QUARTER, start, end, "", year, quarter,
                             value_text=f"{d['sum']:.10g}", value_num=d["sum"], unit=unit,
                             availability=Availability.PRESENT, version_status=vstat,
                             validation=(Validation.SOURCE_ANOMALY_REVIEW
                                         if sg["rows_negative"] else
                                         Validation.UNIT_WARNING if multi else
                                         Validation.PASS),
                             qa_flags=common(" || ".join(qa)), filing_id=fid,
                             derivation="sum(Usage_BU) over transportation rows",
                             tax=tax, candidate_count=d["rows"],
                             applicability_evidence=s["requirement_evidence"])
                edges.extend(row_edges(o["observation_id"], d["sel"], "addend", "Usage_BU"))
                obs.append(o)
                if usage_obs is None and o["availability"] == Availability.PRESENT:
                    usage_obs = (o, d)

    # ------------------------------------------------- storage determinants
    s = slot("i311_storage_determinants")
    if s is not None:
        m = metrics["i311_storage_determinants"]
        payload, any_value, contributors = {}, False, []
        for col, desc, scope_note in DETERMINANTS:
            if col == "Usage_BU":
                continue
            per: dict[str, dict] = {}
            for r in real:
                v = _f0(r.get(col))
                if not v:
                    continue
                k = f"{norm_service(r.get('Service_Type')) or '(blank)'}|{unit_family(r.get(desc))}"
                d = per.setdefault(k, {"net_sum": 0.0, "positive": 0.0, "negative": 0.0,
                                       "rows": 0, "rows_negative": 0})
                d["net_sum"] += v
                d["positive" if v > 0 else "negative"] += v
                d["rows"] += 1
                d["rows_negative"] += 1 if v < 0 else 0
                contributors.append((r, col))
            if per:
                any_value = True
            payload[col] = {"scope": scope_note, "by_service_and_unit": per}
        det_scen = {col: grain_scenarios(grain, rows, col)
                    for col, _d, _n in DETERMINANTS if col != "Usage_BU"}
        blocked = unresolved and not all(v["treatments_coincide"] for v in det_scen.values())
        qa = ["each determinant is scoped separately and NEVER merged into transportation "
              "usage or into another determinant",
              "Annual_Volume_BU is an ANNUAL contract quantity sitting on a quarterly row "
              "and must not be summed across the four quarters of a year",
              "a '-day' or '-mo.' time base is a per-period rate, not a period total, and is "
              "kept in its own unit family",
              "SIGNED COLUMNS: park/loan determinants carry both directions in one column "
              "(7,009 negative Park/Loan day-2 rows table-wide). Each bucket therefore "
              "publishes its positive side, its negative side and their net separately; the "
              "two directions are never collapsed into one quantity"]
        if unresolved:
            qa.append(grain_note)
            qa.append("audit scenarios per determinant (labelled treatments, NOT confidence "
                      "bounds): " + json.dumps(det_scen, sort_keys=True))
            if not blocked:
                qa.append(coincide_note)
        if any_value:
            o = _obs(entity_key, m, periods.QUARTER, start, end, "", year, quarter,
                     value_text=json.dumps(payload, sort_keys=True), value_num=None,
                     unit="as reported per determinant",
                     availability=Availability.INTERPRETATION_BLOCKED if blocked
                     else Availability.PRESENT,
                     version_status=vstat,
                     validation=Validation.BLOCKED_AMBIGUITY if blocked else Validation.PASS,
                     qa_flags=common(" || ".join(qa)), filing_id=fid,
                     missing_reason=("the grain is unresolved and an ambiguous contract group "
                                     "contributes more than one non-zero row to at least one "
                                     "determinant, so the two readings differ"
                                     if blocked else ""),
                     derivation="per-determinant sums scoped by Service_Type and unit family",
                     tax=tax, candidate_count=len(real),
                     applicability_evidence=s["requirement_evidence"])
            for i, (r, col) in enumerate(contributors, 1):
                edges.append(_edge(o["observation_id"], i, "addend", "+", fid, natural_key(r),
                                   col, f"{start}..{end}", _s(r.get(col)),
                                   unit_family(r.get(dict((c, d) for c, d, _n in DETERMINANTS)
                                                     .get(col))), vstat))
            obs.append(o)
        else:
            no_storage = not [r for r in real
                              if norm_service(r.get("Service_Type"))
                              in ("Storage", "Parking/Lending")]
            obs.append(_absent(entity_key, m, s, tax,
                               availability=(Availability.NOT_APPLICABLE if no_storage
                                             else Availability.UNVERIFIED_AVAILABILITY),
                               validation=(Validation.PASS if no_storage
                                           else Validation.NOT_YET_VALIDATED),
                               reason=(
                                   ("this filer reports only transportation service in the "
                                    "quarter, so injection, withdrawal and park/loan "
                                    "determinants do not apply; the reservation and "
                                    "annual-volume determinants, which DO apply to "
                                    "transportation, are all 0 and "
                                    if no_storage
                                    else "every non-usage billing determinant on this "
                                         f"quarter's {len(real)} contract rows is 0 and ")
                                   + ZERO_FILL_NOTE),
                               qa=common(f"service mix: {svc_mix}")))

    # The three share metrics below all run on the SAME named weight, so their
    # denominators are mutually consistent and never silently different.
    if weight is not None:
        w_sel, w_tot = weight["sel"], weight["gross_positive"]
        w_signed = bool(weight["signs"]["rows_negative"])
        w_valid = Validation.SOURCE_ANOMALY_REVIEW if w_signed else Validation.PASS
        w_sign_note = (
            "SIGNED COLUMN: " + str(weight["signs"]["rows_negative"]) + " row(s) carry a "
            "NEGATIVE value totalling " + f"{weight['signs']['negative']:,.0f}" + ". This form "
            "does not label the sign -- for park/loan it is the loan direction, for usage and "
            "revenue it is a credit or reversal -- so the two directions are NOT netted into "
            "the denominator and the negative side is reported here separately. Shares "
            "therefore do not sum to 100%." if w_signed else "")
        w_unit, w_label = weight["unit"], weight["label"]
        w_base = usage_obs[0] if (usage_obs and w_col == "Usage_BU") else None
        w_pref = ("billed transportation usage, the weight this metric names"
                  if weight["preferred"] else
                  "this filer reports NO transportation usage in the quarter, so the weight is "
                  "a SINGLE named determinant restricted to one service type and one unit "
                  "family; determinants are never summed together to build a weight")

        def w_edges(obs_id, sel, role, sign="+"):
            first = []
            if w_base is not None:
                first = [_edge(obs_id, 1, "denominator", "/", fid, None,
                               "i311_billed_transport_usage", f"{start}..{end}",
                               w_base["value_text"], w_base["unit"], vstat,
                               obs_input=w_base["observation_id"])]
            return first + [
                _edge(obs_id, i, role, sign, fid, natural_key(r), w_col, f"{start}..{end}",
                      _s(r.get(w_col)), w_unit, vstat)
                for i, r in enumerate(sel, len(first) + 1)]

    # ------------------------------------------------------------ firm share
    s = slot("i311_firm_share")
    if s is not None:
        m = metrics["i311_firm_share"]
        if weight is None:
            obs.append(_absent(
                entity_key, m, s, tax, availability=Availability.UNVERIFIED_AVAILABILITY,
                validation=Validation.NOT_YET_VALIDATED,
                reason=(f"no billed determinant on this quarter's {len(real)} contract rows "
                        f"carries a non-zero value, so there is no weight to take a share of; "
                        f"{ZERO_FILL_NOTE}"),
                qa=common(grain_note if unresolved else "")))
        elif w_blocked:
            obs.append(_absent(
                entity_key, m, s, tax, availability=Availability.INTERPRETATION_BLOCKED,
                validation=Validation.BLOCKED_AMBIGUITY,
                reason=("the reporting grain of this quarter is unresolved, so the "
                        "denominator -- " + w_label + " -- is not asserted: an ambiguous "
                        "contract group holds more than one non-zero row of it and the two "
                        "readings give different totals"),
                qa=common(" || ".join([grain_note, "audit scenarios: "
                                       + json.dumps(w_scen, sort_keys=True)]))))
        else:
            # every side of the split is measured on ONE basis: the gross positive
            # weight. The negative side is reported separately, never netted in.
            pos = (lambda rs: sum(v for v in (_f0(r.get(w_col)) for r in rs) if v > 0))
            firm = pos([r for r in w_sel
                        if norm_character(r.get("Character_of_Service")) == "Firm"])
            inter = pos([r for r in w_sel
                         if norm_character(r.get("Character_of_Service")) == "Interruptible"])
            unk = w_tot - firm - inter
            qa = [f"explicit denominator: {w_label}; {w_pref}",
                  f"interruptible {_pct(inter, w_tot)}%; UNKNOWN Character_of_Service "
                  f"{_pct(unk, w_tot)}% reported separately and never folded into either side",
                  "SERVICE MIX ONLY: this is not the percentage of revenue protected by "
                  "fixed fees, and it is not a firm-capacity utilisation measure"]
            if w_sign_note:
                qa.append(w_sign_note)
            if unresolved:
                qa.append(grain_note + " Character_of_Service is a service-defining field, so "
                          "it never varies inside an ambiguous group by construction: the "
                          "firm/interruptible split of this denominator is unaffected by the "
                          "grain question")
            o = _obs(entity_key, m, periods.QUARTER, start, end, "", year, quarter,
                     value_text=f"{_pct(firm, w_tot)}", value_num=_pct(firm, w_tot),
                     unit="percent", availability=Availability.PRESENT, version_status=vstat,
                     validation=w_valid, qa_flags=common(" || ".join(qa)),
                     filing_id=fid,
                     derivation=f"share(firm {w_col}, {w_label})",
                     tax=tax, candidate_count=len(w_sel),
                     applicability_evidence=s["requirement_evidence"],
                     notes=json.dumps({"weight": w_label, "weight_column": w_col,
                                       "weight_is_transportation_usage": weight["preferred"]},
                                      sort_keys=True))
            firm_rows = [r for r in w_sel
                         if norm_character(r.get("Character_of_Service")) == "Firm"]
            edges.extend(w_edges(o["observation_id"], firm_rows, "numerator"))
            # A08: a share of 0% has no numerator rows, so the comprehension
            # above emits NOTHING and the observation used to carry no lineage
            # at all. The denominator population is recorded unconditionally so
            # a measured zero is distinguishable from an unmeasured one.
            pop, pedge = _population(
                o["observation_id"], filing_ids=[fid],
                inclusion=(f"rows of filing {fid} in the {w_label} denominator "
                           f"whose Character_of_Service normalises to 'Firm'"),
                exclusion=("rows outside this unit family; placeholder all-zero "
                           "rows; rows whose Character_of_Service is "
                           "Interruptible or UNKNOWN (reported separately and "
                           "never folded into either side); negative values "
                           "(the split is measured on the gross positive weight)"),
                members=firm_rows, candidates=len(w_sel),
                aggregate_value=_pct(firm, w_tot), aggregate_unit="percent",
                empty_reason=("" if firm_rows else
                              f"{len(w_sel)} row(s) carry the denominator weight "
                              "and none is Firm, so the firm share is a MEASURED "
                              "zero, not an absence of data"),
                note=f"denominator: {w_label}; weight column {w_col}")
            edges.append(pedge)
            o.setdefault("_populations", []).append(pop)
            obs.append(o)

    # ----------------------------------------------- top-five shipper share
    s = slot("i311_top5_shipper_share")
    if s is not None:
        m = metrics["i311_top5_shipper_share"]
        if weight is None:
            obs.append(_absent(
                entity_key, m, s, tax, availability=Availability.UNVERIFIED_AVAILABILITY,
                validation=Validation.NOT_YET_VALIDATED,
                reason=("no billed determinant on this quarter carries a non-zero value, so "
                        "there is no weight to rank concentration on. No count-weighted "
                        f"substitute is published: the weight is part of the metric. "
                        f"{ZERO_FILL_NOTE}"),
                qa=common(grain_note if unresolved else "")))
        elif w_blocked or shipper_amb:
            obs.append(_absent(
                entity_key, m, s, tax, availability=Availability.INTERPRETATION_BLOCKED,
                validation=Validation.BLOCKED_AMBIGUITY,
                reason=("the unresolved grain changes the concentration: "
                        + json.dumps({"total_ambiguous": w_blocked,
                                      "shipper_attribution_ambiguous": shipper_amb},
                                     sort_keys=True)
                        + ". A concentration ranking is exactly the quantity that this "
                          "ambiguity destroys, so no top-five share is asserted"),
                qa=common(" || ".join([grain_note, "audit scenarios: "
                                       + json.dumps(w_scen, sort_keys=True)]))))
        else:
            # ranking, coverage and the unknown bucket all use ONE basis: the
            # gross positive weight. Mixing a net numerator with a gross
            # denominator is what makes a "share" exceed 100%.
            named: dict[str, float] = {}
            named_negative: dict[str, float] = {}
            unknown = 0.0
            propids: dict[str, set] = {}
            for r in w_sel:
                k = shipper_key(r)
                v = _f0(r.get(w_col))
                if not k or k in ("N A", "NA"):
                    unknown += max(v, 0.0)
                    continue
                if v > 0:
                    named[k] = named.get(k, 0.0) + v
                elif v < 0:
                    named_negative[k] = named_negative.get(k, 0.0) + v
                    named.setdefault(k, 0.0)
                pid = _s(r.get("Filer_Proprietary_Shipper_ID"))
                if pid:
                    propids.setdefault(k, set()).add(pid)
            top = sorted(named.values(), reverse=True)[:5]
            coverage_pct = _pct(sum(named.values()), w_tot)
            split = sum(1 for k, v in propids.items() if len(v) > 1)
            qa = [f"explicit denominator: {w_label}; {w_pref}. The weight is BILLED ACTIVITY, "
                  "not revenue and not credit exposure",
                  f"contracts were aggregated to the legal shipper FIRST: {len(w_sel)} contract "
                  f"rows collapse to {len(named)} distinct legal shippers before ranking",
                  f"shipper identity coverage: {coverage_pct}% of the weight resolved to a "
                  f"named legal shipper; {_pct(unknown, w_tot)}% has no usable shipper name "
                  "and is reported separately, never merged into a named shipper",
                  "shipper identity is FILER-LOCAL: Form 549D carries no FERC shipper CID, "
                  "Shipper_ID is a surrogate ROW key (113,255 distinct over 113,255 rows) and "
                  "Filer_Proprietary_Shipper_ID is the filer's own number, so no shipper is "
                  "asserted to be the same company at another filer",
                  f"{split} name group(s) carry more than one Filer_Proprietary_Shipper_ID"]
            if w_sign_note:
                qa.append(w_sign_note)
            if unresolved:
                qa.append(grain_note)
            o = _obs(entity_key, m, periods.QUARTER, start, end, "", year, quarter,
                     value_text=f"{_pct(sum(top), w_tot)}", value_num=_pct(sum(top), w_tot),
                     unit="percent", availability=Availability.PRESENT, version_status=vstat,
                     validation=w_valid, qa_flags=common(" || ".join(qa)),
                     filing_id=fid,
                     derivation="aggregate contracts to legal shipper, rank, take top five",
                     tax=tax, candidate_count=len(named),
                     applicability_evidence=s["requirement_evidence"],
                     notes=json.dumps({"weight": w_label, "weight_column": w_col,
                                       "weight_basis": "gross positive",
                                       "distinct_shippers": len(named),
                                       "identity_coverage_pct": coverage_pct,
                                       "unknown_identity_weight": unknown,
                                       "negative_weight_by_shipper": named_negative},
                                      sort_keys=True))
            edges.extend(w_edges(o["observation_id"], w_sel, "group_member"))
            obs.append(o)

    # ------------------------------------------------------ affiliate share
    s = slot("i311_affiliate_activity")
    if s is not None:
        m = metrics["i311_affiliate_activity"]
        if weight is None:
            obs.append(_absent(
                entity_key, m, s, tax, availability=Availability.UNVERIFIED_AVAILABILITY,
                validation=Validation.NOT_YET_VALIDATED,
                reason=("no billed determinant on this quarter carries a non-zero value, so "
                        f"there is no weight to split by affiliate flag. {ZERO_FILL_NOTE}"),
                qa=common(grain_note if unresolved else "")))
        elif w_blocked or affiliate_amb:
            obs.append(_absent(
                entity_key, m, s, tax, availability=Availability.INTERPRETATION_BLOCKED,
                validation=Validation.BLOCKED_AMBIGUITY,
                reason=("the unresolved grain changes which affiliate flag carries the weight: "
                        + json.dumps({"total_ambiguous": w_blocked,
                                      "affiliate_attribution_ambiguous": affiliate_amb},
                                     sort_keys=True)
                        + ". Because unknown-affiliate must never be merged into "
                          "non-affiliate, an ambiguous flag cannot be resolved by preference"),
                qa=common(" || ".join([grain_note, "audit scenarios: "
                                       + json.dumps(w_scen, sort_keys=True)]))))
        else:
            buckets = {"yes": 0.0, "no": 0.0, "unknown": 0.0}
            negative = {"yes": 0.0, "no": 0.0, "unknown": 0.0}
            counts = {"yes": 0, "no": 0, "unknown": 0}
            for r in w_sel:
                k = norm_affiliate(r.get("Affiliate_Status"))
                v = _f0(r.get(w_col))
                buckets[k] += max(v, 0.0)          # one basis: gross positive
                negative[k] += min(v, 0.0)
                counts[k] += 1
            qa = [f"explicit denominator: {w_label}; {w_pref}",
                  f"non-affiliate {_pct(buckets['no'], w_tot)}% ({counts['no']} rows); "
                  f"UNKNOWN AFFILIATE {_pct(buckets['unknown'], w_tot)}% "
                  f"({counts['unknown']} rows)",
                  "UNKNOWN AFFILIATE IS THE EMPTY STRING in this form -- there is no 'Unknown' "
                  "token -- and it is kept STRICTLY SEPARATE from non-affiliate. Blank is "
                  "never read as 'No'. Table-wide the blank share rises from 1.9% in 2011 to "
                  "45.1% in 2024, so it is not a residual",
                  "this share is weighted by BILLED ACTIVITY, not by revenue; blank revenue is "
                  "not zero and is never used as a weight"]
            if w_sign_note:
                qa.append(w_sign_note)
            if unresolved:
                qa.append(grain_note)
            o = _obs(entity_key, m, periods.QUARTER, start, end, "", year, quarter,
                     value_text=f"{_pct(buckets['yes'], w_tot)}",
                     value_num=_pct(buckets["yes"], w_tot), unit="percent",
                     availability=Availability.PRESENT, version_status=vstat,
                     validation=w_valid, qa_flags=common(" || ".join(qa)),
                     filing_id=fid,
                     derivation=f"share(affiliate-flagged {w_col}, {w_label})",
                     tax=tax, candidate_count=len(w_sel),
                     applicability_evidence=s["requirement_evidence"],
                     notes=json.dumps({"weight": w_label, "weight_column": w_col,
                                       "weight_basis": "gross positive",
                                       "weight_by_flag": buckets,
                                       "negative_weight_by_flag": negative,
                                       "rows_by_flag": counts}, sort_keys=True))
            aff_rows = [r for r in w_sel
                        if norm_affiliate(r.get("Affiliate_Status")) == "yes"]
            edges.extend(w_edges(o["observation_id"], aff_rows, "numerator"))
            # A08: 25 of the 52 edgeless baseline observations were this metric
            # at 0% -- no affiliate-flagged row, therefore no numerator edge,
            # therefore no lineage. The population is recorded either way.
            pop, pedge = _population(
                o["observation_id"], filing_ids=[fid],
                inclusion=(f"rows of filing {fid} in the {w_label} denominator "
                           "whose Affiliate_Status normalises to 'yes'"),
                exclusion=("rows outside this unit family; placeholder all-zero "
                           "rows; rows flagged non-affiliate; rows with a BLANK "
                           "Affiliate_Status -- blank is UNKNOWN and is never "
                           "read as 'No' nor merged into either side; negative "
                           "values (the split is measured on gross positive)"),
                members=aff_rows, candidates=len(w_sel),
                aggregate_value=_pct(buckets["yes"], w_tot), aggregate_unit="percent",
                empty_reason=("" if aff_rows else
                              f"{len(w_sel)} row(s) carry the denominator weight, "
                              f"{counts['no']} flagged non-affiliate and "
                              f"{counts['unknown']} blank/unknown, and none is "
                              "affiliate-flagged. The 0% is a MEASURED zero over "
                              "a retrieved population, not an absence of data"),
                note=f"denominator: {w_label}; weight column {w_col}; "
                     f"rows_by_flag={json.dumps(counts, sort_keys=True)}")
            edges.append(pedge)
            o.setdefault("_populations", []).append(pop)
            obs.append(o)


    # ---------------------------------------------------------- expiry profile
    s = slot("i311_contract_expiry")
    if s is not None:
        m = metrics["i311_contract_expiry"]
        wcol = weight["column"] if weight else "Usage_BU"
        wname = weight["label"] if weight else "Usage_BU (no non-zero determinant filed)"
        buckets = {k: {"contract_rows": 0, "billed_weight": 0.0} for k in
                   ("expired_or_ending_this_quarter", "0-1y", "1-2y", "2-5y", "5y+",
                    "evergreen_sentinel_2100_12", "unknown_no_end_date")}
        end_y, end_m = int(end[:4]), int(end[5:7])
        for r in real:
            ym = parse_ym(r.get("Contract_End"))
            if ym is None:
                k = "unknown_no_end_date"
            elif ym == (2100, 12):
                k = "evergreen_sentinel_2100_12"
            else:
                months = (ym[0] - end_y) * 12 + (ym[1] - end_m)
                k = ("expired_or_ending_this_quarter" if months <= 0 else
                     "0-1y" if months <= 12 else "1-2y" if months <= 24 else
                     "2-5y" if months <= 60 else "5y+")
            buckets[k]["contract_rows"] += 1
            buckets[k]["billed_weight"] += _f0(r.get(wcol))
        qa = ["THE WEIGHT IS NAMED: two weights are published side by side -- contract-row "
              f"count and {wname} -- and neither is presented as the other",
              "2100/12 is the evergreen sentinel and is its own bucket, never a real expiry",
              "a blank Contract_End is 'unknown', and among Firm rows it usually means "
              "evergreen or interruptible rather than a missing value",
              "NO 'earnings at risk' is inferred from this distribution: a billed usage weight "
              "is not a revenue weight and this form does not disclose contract value"]
        blocked = bool(expiry_amb)
        if unresolved:
            qa.append(grain_note)
            if blocked:
                qa.append("the unresolved grain puts competing end dates on the same weight: "
                          + json.dumps(expiry_amb, sort_keys=True))
            else:
                qa.append("Contract_End agrees inside every ambiguous contract group, so the "
                          "BUCKET assignment is unaffected; the contract-row COUNT weight "
                          "would differ between the two readings by the ambiguous rows and is "
                          "read with that in mind")
        o = _obs(entity_key, m, periods.QUARTER, start, end, "", year, quarter,
                 value_text=json.dumps(buckets, sort_keys=True), value_num=None,
                 unit=f"contract rows and {wcol}, both stated",
                 availability=Availability.INTERPRETATION_BLOCKED if blocked
                 else Availability.PRESENT,
                 version_status=vstat,
                 validation=Validation.BLOCKED_AMBIGUITY if blocked else Validation.PASS,
                 qa_flags=common(" || ".join(qa)), filing_id=fid,
                 missing_reason=("the unresolved grain puts competing Contract_End values on "
                                 "the same billed weight, so the expiry bucket cannot be "
                                 "assigned" if blocked else ""),
                 derivation="bucket Contract_End relative to the period end",
                 tax=tax, candidate_count=len(real),
                 applicability_evidence=s["requirement_evidence"])
        edges.extend(row_edges(o["observation_id"], real, "group_member", "Contract_End"))
        obs.append(o)

    # ---------------------------------------------------------- component rates
    s = slot("i311_component_rates")
    if s is not None:
        m = metrics["i311_component_rates"]
        payload, contributors = {}, []
        for col, desc, disc in RATE_COLUMNS:
            per: dict[str, dict] = {}
            for r in real:
                v = _f0(r.get(col))
                if not v:
                    continue
                k = f"{_s(r.get(desc)) or '(rate unit not reported)'}|" \
                    f"{norm_service(r.get('Service_Type')) or '(blank)'}"
                d = per.setdefault(k, {"rows": 0, "values": []})
                d["rows"] += 1
                d["values"].append(v)
                contributors.append((r, col))
            for k, d in per.items():
                vals = sorted(d.pop("values"))
                d.update({"min": vals[0], "median": statistics.median(vals), "max": vals[-1]})
            if per:
                payload[col] = per
        qa = ([grain_note + " A rate summary groups distinct filed rates and never sums "
               "rows, so the grain question does not change it"] if unresolved else []) + [
              "rates are grouped on MATCHING component, unit and time base; the descriptor "
              "carries both (e.g. '$/Dth-mo.' is a monthly reservation rate, '$/Dth' is not) "
              "and the currency unit itself varies between $ and cents in this form",
              "these are DESCRIPTOR-SPECIFIC filed rates. No fleet comparability is asserted "
              "and no all-in tariff is synthesised from missing components",
              "ANNUAL REVENUE IS NEVER DIVIDED BY QUARTERLY USAGE: Total_Rev covers a year "
              "and Usage_BU covers a quarter, so no implied rate is computed from them",
              "a 0 rate is not published as a measured zero: only non-zero filed rates are "
              f"summarised, because {ZERO_FILL_NOTE}"]
        if payload:
            o = _obs(entity_key, m, periods.QUARTER, start, end, "", year, quarter,
                     value_text=json.dumps(payload, sort_keys=True), value_num=None,
                     unit="as reported per descriptor", availability=Availability.PRESENT,
                     version_status=vstat, validation=Validation.PASS,
                     qa_flags=common(" || ".join(qa)), filing_id=fid,
                     derivation="group non-zero filed rates by component and descriptor",
                     tax=tax, candidate_count=len(real),
                     applicability_evidence=s["requirement_evidence"])
            for i, (r, col) in enumerate(contributors, 1):
                edges.append(_edge(o["observation_id"], i, "group_member", "", fid,
                                   natural_key(r), col, f"{start}..{end}", _s(r.get(col)),
                                   _s(r.get(dict((c, d) for c, d, _x in RATE_COLUMNS)[col])),
                                   vstat))
            obs.append(o)
        else:
            obs.append(_absent(entity_key, m, s, tax,
                               availability=Availability.UNVERIFIED_AVAILABILITY,
                               validation=Validation.NOT_YET_VALIDATED,
                               reason=(f"every component rate on this quarter's {len(real)} "
                                       f"contract rows is 0; {ZERO_FILL_NOTE}"),
                               qa=common()))

    # ------------------------------------------- discounts, schedules, dockets
    s = slot("i311_discounts_and_schedules")
    if s is not None:
        m = metrics["i311_discounts_and_schedules"]
        disc = {}
        for col, _d, dcol in RATE_COLUMNS:
            vals = [_s(r.get(dcol)) for r in real]
            flagged = [v for v in vals if v and v.upper() not in ("N/A", "NA")]
            if flagged:
                disc[dcol] = {"rows_with_a_discount_indicator": len(flagged),
                              "distinct_values": sorted(set(flagged))[:20]}
        sched: dict[str, int] = {}
        for r in real:
            sched[_s(r.get("Service_Rate_Schedule")) or "(blank)"] = \
                sched.get(_s(r.get("Service_Rate_Schedule")) or "(blank)", 0) + 1
        dockets: dict[str, int] = {}
        for r in real:
            for d in parse_dockets(r.get("Docket_Current_Rate")) or ["(blank)"]:
                dockets[d] = dockets.get(d, 0) + 1
        payload = {"discount_indicators": disc, "rate_schedules": sched,
                   "pr_dockets": dockets,
                   "other_charges_free_text_rows":
                       sum(1 for r in real if _s(r.get("Other_Changes")))}
        qa = ([grain_note + " A census of discount indicators, schedules and dockets "
               "counts distinct filed values and never sums rows"] if unresolved else []) + [
              "the Discounted_* columns are FREE TEXT, not numbers: comma-separated lists, "
              "'N/A', 'EXCESS INJECTION' and a literal '0' all occur. Presence of a non-blank, "
              "non-'N/A' value is the discount INDICATOR and no value is cast to a float",
              "Docket_Current_Rate is parsed as a LIST: multi-docket strings such as "
              "'PR03-17-000, PR10-13-000' are common and 'Docket No. ' prefixes occur",
              "rate authority applies to PARTICULAR SERVICES; a docket on one row does not "
              "authorise another row's rate",
              "a corrected filing is kept distinct from changed trading activity: resubmissions "
              "are retained as occurrence history and deduped on Submission_Date",
              "Service_Rate_Schedule is an uncontrolled vocabulary (codes and full sentences "
              "both occur) and is reported verbatim"]
        o = _obs(entity_key, m, periods.QUARTER, start, end, "", year, quarter,
                 value_text=json.dumps(payload, sort_keys=True), value_num=None,
                 unit="categorical", availability=Availability.PRESENT, version_status=vstat,
                 validation=Validation.PASS, qa_flags=common(" || ".join(qa)), filing_id=fid,
                 derivation="census of discount indicators, rate schedules and PR dockets",
                 tax=tax, candidate_count=len(real),
                 applicability_evidence=s["requirement_evidence"])
        edges.extend(row_edges(o["observation_id"], real, "group_member",
                               "Docket_Current_Rate"))
        obs.append(o)

    # ------------------------------------------------------------------ points
    s = slot("i311_points_decoded")
    if s is not None:
        m = metrics["i311_points_decoded"]
        payload = {}
        evasive = 0
        for side, name_c, code_c, addl_c, addl_code_c in POINT_COLUMNS:
            seen: dict[str, dict] = {}
            for r in real:
                nm, cd = _s(r.get(name_c)), _s(r.get(code_c))
                if not nm and not cd:
                    continue
                if re.search(r"see\s+attach", f"{nm} {cd}", re.I):
                    evasive += 1
                k = f"{nm}|{cd}"
                d = seen.setdefault(k, {"name": nm, "common_code": cd, "rows": 0,
                                        "additional_points": set()})
                d["rows"] += 1
                if _s(r.get(addl_c)):
                    d["additional_points"].add(_s(r.get(addl_c)))
            payload[side] = [{**v, "additional_points": sorted(v["additional_points"])[:5]}
                             for v in seen.values()]
        if unresolved:
            grain_qa_points = [grain_note + " A point census is a set of distinct filed "
                               "values, not a sum, so the grain question does not change it"]
        else:
            grain_qa_points = []
        qa = grain_qa_points + [
              "UNKNOWN PROPRIETARY POINT CODES REMAIN CODES: no external cross-reference "
              "record was located in this source, so no code is given an invented name and "
              "no code is expanded into a location",
              f"{evasive} row-sides carry an evasive placeholder such as 'See Attachment' in "
              "the point name or common code; these are preserved verbatim, not dropped",
              "point common codes are ~21% blank table-wide and polluted with free text, so "
              "they are a WEAK identifier and are never used as a join key"]
        o = _obs(entity_key, m, periods.QUARTER, start, end, "", year, quarter,
                 value_text=json.dumps(payload, sort_keys=True), value_num=None, unit="codes",
                 availability=Availability.PRESENT, version_status=vstat,
                 validation=Validation.PASS, qa_flags=common(" || ".join(qa)), filing_id=fid,
                 derivation="census of filed receipt and delivery points and their codes",
                 tax=tax, candidate_count=len(real),
                 applicability_evidence=s["requirement_evidence"])
        edges.extend(row_edges(o["observation_id"], real, "group_member", "Receipt_Point_Name"))
        obs.append(o)

    return obs, edges


# --------------------------------------------------------------- annual block

def _annual(ctx, entity_key, metrics, slots, year, filing, tax, torn,
            asset_ids, latest_period) -> tuple[list, list]:
    start, end = periods.annual_interval(year)
    obs: list[dict] = []
    edges: list[dict] = []

    if filing is None:
        for mid, s in slots.items():
            obs.append(_absent(
                entity_key, metrics[mid], s, tax,
                availability=Availability.SOURCE_BLANK,
                validation=Validation.NOT_YET_VALIDATED,
                reason=(f"annual customer revenue is carried in the Q4 report and the "
                        f"respondent table contains no {year}-Q4 row for this CID. The fetch "
                        "was proven complete, so this is an absent filing in the source, not "
                        "a retrieval failure; it is not proof that contracts terminated"),
                qa="no filing-due determination is made: ferclib.periods.DEADLINES carries "
                   "no Form 549D rule"))
        return obs, edges

    rows = filing["_rows"]
    fid = filing["filing_id"]
    vstat = filing["version_status"]
    real = [r for r in rows if not is_placeholder(r)]
    transport = transport_rows(real)
    # Definitional, not stylistic: Form 549D field 72 is transportation-only and
    # 18 CFR 284.126(b)(1)(viii) excludes storage revenue outright. A Storage row
    # inside this selection is a defect, so it is checked rather than trusted.
    assert_transport_excludes_storage(transport)
    grain = probe_grain(rows)
    unresolved = not grain["resolved"]
    is_transport = (lambda r: norm_service(r.get("Service_Type")) == "Transportation")
    scen = grain_scenarios(grain, rows, "Total_Rev", is_transport)
    # The annual total moves between the two readings only when an ambiguous
    # contract group holds more than one revenue-bearing row. That is a
    # structural test on the rows, not a comparison of amounts.
    blocked = unresolved and not scen["treatments_coincide"]

    # Order 735-A divergence: what the source ACTUALLY reports outside transportation
    by_service: dict[str, float] = {}
    for r in real:
        v = _f0(r.get("Total_Rev"))
        if v:
            k = norm_service(r.get("Service_Type")) or "(blank - unknown service scope)"
            by_service[k] = by_service.get(k, 0.0) + v
    non_transport = {k: v for k, v in by_service.items() if k != "Transportation"}
    storage_rev = non_transport.get("Storage", 0.0)

    def common(extra=""):
        bits = [f"annual figure read from the {year}-Q4 filing {fid}",
                f"grain: {grain['grain_class']}"]
        if torn:
            bits.append(torn)
        if extra:
            bits.append(extra)
        return " || ".join(bits)

    block_note = ""
    if unresolved:
        block_note = (("GRAIN BLOCKED: " if blocked else "UNRESOLVED GRAIN: ") + "; ".join(
            f"contract {g['contract']} has {g['rows']} rows differing only in "
            f"{', '.join(g['varies_only_in'])}" for g in grain["ambiguous_groups"])
            + ". Reading A (one contract's annual figure written once per shipper) and "
              "reading B (several genuine relationships under one contract number) are both "
              "consistent with this filing"
            + (". An ambiguous group holds more than one revenue-bearing row, so the two "
               "readings give DIFFERENT annual totals and neither is asserted."
               if blocked else
               ". Every ambiguous group holds at most one revenue-bearing row, so both "
               "readings give the SAME annual total and the figure is published -- a "
               "structural fact about the rows, not a settlement of the grain question.")
            + " NO 'same amount means duplicate' rule and NO divergence threshold is used "
              "anywhere here; note the direction, two identical non-zero values BLOCK the "
              "total rather than collapsing it.")

    # ------------------------------------------------- annual transport revenue
    rev_obs = None
    s = slots.get("i311_annual_transport_revenue")
    if s is not None:
        m = metrics["i311_annual_transport_revenue"]
        rev_rows = [r for r in transport if _f0(r.get("Total_Rev"))]
        usage_rows = [r for r in transport if _f0(r.get("Usage_BU"))]
        divergence = ""
        if non_transport:
            divergence = (
                # Stable machine-readable marker. The prose below is the substance
                # and may be reworded as the source evidence sharpens; this token
                # is the contract a test or a consumer keys on, so it must NOT
                # change when the wording does.
                SCOPE_DIVERGENCE_MARKER + " "
                "OUT-OF-SCOPE REVENUE REPORTED ON THIS FORM: field 72 (Total_Rev) is defined "
                "by FERC as 'the total amount of annual revenue charged for services as "
                "permitted in the SOC for TRANSPORTATION service', summing fields 68-71, none "
                "of which is a storage component; and 18 CFR 284.126(b)(1)(viii) as revised by "
                "Order No. 735-A, 133 FERC para 61,216 (2010) at P 22 excludes storage revenue "
                "outright. This filing nevertheless reports "
                + "; ".join(f"{k} ${v:,.0f}" for k, v in sorted(non_transport.items()))
                + " of Total_Rev outside transportation. The EXCLUSION IS NOT A CHOICE -- it is "
                  "the field's own definition -- so that revenue is excluded from this "
                  "transportation figure and from its denominator, and is reported as filed in "
                  "i311_revenue_components. What remains open is only (a) WHY the filer "
                  "populated a field that no longer collects storage revenue, which the filing "
                  "does not say, and (b) how to DISPLAY the out-of-scope amount. The amount is "
                  "NOT a storage-revenue total for this entity: per-customer storage revenue is "
                  "collected in the semi-annual storage reports under 18 CFR 284.126(c)(5), not "
                  "here, so this residue understates storage revenue by an unknown amount.")
        if not transport:
            obs.append(_absent(
                entity_key, m, s, tax, availability=Availability.NOT_APPLICABLE,
                validation=Validation.PASS,
                reason=("the Q4 filing carries no transportation-service rows, so there is no "
                        "FERC-reportable 311/Hinshaw TRANSPORTATION revenue to report for this "
                        "year. Revenue reported under other service types is NOT substituted "
                        "for it and is reported separately"),
                qa=common(divergence or "this respondent reported no revenue under any "
                                        "service type in the Q4 filing")))
        elif blocked:
            obs.append(_absent(
                entity_key, m, s, tax, availability=Availability.INTERPRETATION_BLOCKED,
                validation=Validation.BLOCKED_AMBIGUITY,
                reason=("the reporting grain of the Q4 filing is unresolved and an ambiguous "
                        "contract group holds more than one revenue-bearing row, so the two "
                        "readings give different annual totals and neither is asserted"),
                qa=common(" || ".join([block_note,
                                       "audit scenarios (labelled treatments, NOT confidence "
                                       "bounds): " + json.dumps(scen, sort_keys=True)] +
                                      ([divergence] if divergence else [])))))
        elif not rev_rows:
            obs.append(_absent(
                entity_key, m, s, tax, availability=Availability.UNVERIFIED_AVAILABILITY,
                validation=Validation.NOT_YET_VALIDATED,
                reason=(f"the Q4 filing carries {len(transport)} transportation rows and every "
                        f"Total_Rev is 0; {ZERO_FILL_NOTE}, so this is neither published as a "
                        "measured zero nor called a blank"),
                qa=common(" || ".join(filter(None, [
                    f"arithmetic row sum over {len(transport)} transportation rows = $0",
                    divergence])))))
        else:
            total = scen["scenario_B_row_sum"]
            qa = [f"reported total: Total_Rev summed over {len(transport)} transportation rows "
                  f"of the {year}-Q4 filing; the four component columns are NOT summed to "
                  "produce it (773 Q4 rows table-wide disagree with their own components)",
                  f"ANNUAL REVENUE COVERAGE: {len(rev_rows)}/{len(transport)} transportation "
                  f"rows carry a non-zero annual Total_Rev ({_pct(len(rev_rows), len(transport))}%)",
                  f"Q4 USAGE COVERAGE OF THE SAME ROWS: {len(usage_rows)}/{len(transport)} "
                  f"({_pct(len(usage_rows), len(transport))}%). THESE TWO ARE INDEPENDENT: "
                  f"{sum(1 for r in rev_rows if not _f0(r.get('Usage_BU')))} rows carry annual "
                  "revenue with zero Q4 usage, which is expected because the revenue is ANNUAL "
                  "while the determinants are QUARTERLY. Zero Q4 usage is never evidence that "
                  "the revenue figure is wrong",
                  "SCOPE: transportation rows only. Storage and parking revenue is excluded "
                  "from BOTH the numerator and the denominator here, so unreported storage "
                  "revenue cannot depress transportation-revenue coverage",
                  "annual revenue is NEVER divided by quarterly usage to imply a rate",
                  "Q4 USAGE DOES NOT ESTABLISH ANNUAL-REVENUE COVERAGE, and FERC says so "
                  "directly: " + FIELD_72["annual_revenue_is_independent_of_q4_volumes"],
                  "ARITHMETIC SUPPORT ONLY: this figure is the sum of the Total_Rev cells "
                  "named in its lineage population, over the stated inclusion and exclusion "
                  "rules. It is NOT a certified whole-entity economic total, NOT the "
                  "respondent's total revenue, and NOT audited by FERC. It covers "
                  "FERC-reportable NGPA 311/Hinshaw transportation only"]
            if divergence:
                qa.append(divergence)
            if grain["service_leg_groups"]:
                qa.append(f"{len(grain['service_leg_groups'])} contract group(s) hold several "
                          "rows and are distinguished as service legs by "
                          + "; ".join(f"{g['contract']}: {', '.join(g['distinguished_by'])}"
                                      for g in grain["service_leg_groups"][:4]))
            if unresolved:
                qa.append(block_note)
                qa.append("audit scenarios (labelled treatments, NOT confidence bounds): "
                          + json.dumps(scen, sort_keys=True))
            rsg = sign_split(transport, "Total_Rev")
            if rsg["rows_negative"]:
                qa.append(f"SIGNED COLUMN: {rsg['rows_negative']} transportation row(s) carry "
                          f"a NEGATIVE Total_Rev totalling {rsg['negative']:,.0f} against "
                          f"{rsg['positive']:,.0f} positive. The published figure is the NET "
                          "sum exactly as filed; a negative annual revenue is a credit or "
                          "reversal the form does not label, and it is neither dropped nor "
                          "made positive")
            o = _obs(entity_key, m, periods.ANNUAL, start, end, "", year, "Q4",
                     value_text=f"{total:.2f}", value_num=total, unit="iso4217:USD",
                     availability=Availability.PRESENT, version_status=vstat,
                     validation=(Validation.SOURCE_ANOMALY_REVIEW if rsg["rows_negative"]
                                 else Validation.PASS),
                     qa_flags=common(" || ".join(qa)),
                     filing_id=fid, derivation="sum(Total_Rev) over Q4 transportation rows",
                     tax=tax, candidate_count=len(transport),
                     applicability_evidence=s["requirement_evidence"])
            # A published annual revenue must never rest on Q4 usage.
            assert_revenue_not_inferred_from_usage(rev_rows, usage_rows, True)
            edges.extend([_edge(o["observation_id"], i, "addend", "+", fid, natural_key(r),
                                "Total_Rev", f"{start}..{end}", _s(r.get("Total_Rev")),
                                "iso4217:USD", vstat)
                          for i, r in enumerate(transport, 1)])
            # A16: the billing population is recorded as DATA, not as prose, so
            # a reviewer can redraw it and w6-acceptance can prove by digest
            # that no Storage row entered the transportation total.
            pop, pedge = _population(
                o["observation_id"], filing_ids=[fid],
                inclusion=(f"every non-placeholder contract row of the {year}-Q4 filing {fid} "
                           "whose Service_Type normalises to 'Transportation'; the annual "
                           "Total_Rev cell of each such row is summed as filed"),
                exclusion=("Service_Type Storage, Parking/Lending, Other and blank "
                           "(EXCLUDED BY DEFINITION: Form 549D field 72 is transportation-"
                           "only and 18 CFR 284.126(b)(1)(viii) excludes storage revenue); "
                           "all-zero placeholder rows; the four component columns 68-71, "
                           "which are reconciled against the reported total and never summed "
                           "into it. Negative Total_Rev cells are NOT excluded -- the figure "
                           "is the net sum exactly as filed"),
                members=transport, candidates=len(real),
                aggregate_value=f"{total:.2f}", aggregate_unit="iso4217:USD",
                note=("annual figure carried in the Q4 report per 18 CFR "
                      "284.126(b)(1)(viii) ('need only be reported every fourth "
                      f"quarter'). Non-transportation Total_Rev on this filing: "
                      + (json.dumps(non_transport, sort_keys=True) if non_transport
                         else "none")))
            edges.append(pedge)
            o.setdefault("_populations", []).append(pop)
            obs.append(o)
            rev_obs = o

        if non_transport and obs:
            obs[-1].setdefault("_events", []).append(_event(
                entity_key, asset_ids, "order_735a_scope_divergence",
                (f"{entity_key} {year}-Q4 reports "
                 + "; ".join(f"{k} ${v:,.0f}" for k, v in sorted(non_transport.items()))
                 + " of annual Total_Rev outside transportation"),
                ("Order 735-A states that the annual customer revenue collected on Form 549D "
                 "EXCLUDES storage services. The data does not obey: table-wide, 544 Q4 "
                 "storage rows carry $237,017,275. This filer's non-transportation revenue is "
                 "reported here as filed, is excluded from the transportation revenue metric "
                 "and from its denominator, and NEITHER reading of the Order's scope has been "
                 "applied to the data. A display decision is required."),
                filing_id=fid, reporting_date=end,
                is_backfill=f"{year}-Q4" != latest_period,
                comparison_basis="Order 735-A stated scope vs Service_Type as filed",
                confidence="verified from the filed Service_Type and Total_Rev columns"))
        if storage_rev:
            # ONE policy record, plus one SOURCE-ANCHORED record per affected
            # filer-year. The 8 September register carried SIX rows here, which
            # the audit correctly read as 4 storage filer-years plus 2 duplicate
            # policy records -- not 4 distinct ambiguous filer-period blockers.
            #
            # The duplication was an IDENTITY defect, not a logic defect:
            # `open_blocker` derives blocker_id from (adapter, scope, summary),
            # and the table-wide dollar figure used to live in the SUMMARY. When
            # that sentence was edited between runs the same policy question
            # minted a second blocker_id. The summary is now a fixed constant and
            # every volatile figure lives in exact_error, which is not hashed.
            #
            # The four filer-year records are NOT consolidated. Each is anchored
            # to a distinct (entity, year) source population, and merging source
            # rows would destroy exactly the detail a reviewer needs.
            ctx.staging.open_blocker(
                ADAPTER, "semantic", POLICY_BLOCKER_SUMMARY,
                scope=POLICY_BLOCKER_SCOPE,
                exact_error=(
                    "SCOPE IS SETTLED BY FERC SOURCE, DISPLAY IS NOT. Field 72 is "
                    "transportation-only by definition (Corrected Appendix to Order No. "
                    "735-A) and 18 CFR 284.126(b)(1)(viii) excludes storage revenue, so "
                    "excluding Storage rows from the transportation total is not an "
                    "interpretation and needs no decision. The open question is how to "
                    "DISPLAY the as-filed out-of-scope amounts, which must not be "
                    "presented as storage revenue: Form 549D no longer collects "
                    "per-customer storage revenue (that is 18 CFR 284.126(c)(5), the "
                    "semi-annual storage reports), so these amounts understate storage "
                    "revenue by an unknown margin. Populations, both LABELLED and neither "
                    "a target: "
                    + json.dumps(AUDITED_POPULATIONS, sort_keys=True)
                    + " Per-filer amounts are carried as order_735a_scope_divergence "
                      "events and as one blocker per affected filer-year. This record "
                      "CONSOLIDATES the duplicate policy blockers "
                    + ", ".join(SUPERSEDED_POLICY_BLOCKERS) + "."),
                human_decision=True)
            ctx.staging.open_blocker(
                ADAPTER, "semantic", POLICY_BLOCKER_SUMMARY, scope=f"{entity_key}:{year}",
                exact_error=(f"{entity_key} {year}-Q4 reports ${storage_rev:,.0f} of Total_Rev "
                             f"on {len(storage_rows(real))} Storage row(s). Excluded from the "
                             "transportation figure by the field's own definition and "
                             "preserved as filed. This is a source-anchored record for one "
                             "filer-year and is NEVER consolidated with another filer-year "
                             "or with the policy record. Historical context (a LABEL, not a "
                             "target): the audited table-wide population is "
                             f"{AUDITED_POPULATIONS['historical_all_occurrences']['rows']} Q4 "
                             "Storage rows carrying $"
                             f"{int(AUDITED_POPULATIONS['historical_all_occurrences']['revenue']):,} "
                             "across all filing occurrences 2011-2025; the included 2024-2025 "
                             f"universe subset is "
                             f"{AUDITED_POPULATIONS['included_universe_2024_2025']['rows']} rows "
                             "and $"
                             f"{int(AUDITED_POPULATIONS['included_universe_2024_2025']['revenue']):,}."),
                human_decision=True)

    # ---------------------------------------------------------- revenue components
    s = slots.get("i311_revenue_components")
    if s is not None:
        m = metrics["i311_revenue_components"]
        comps = {c: sum(_f0(r.get(c)) for r in transport) for c in REV_COMPONENTS}
        reported = sum(_f0(r.get("Total_Rev")) for r in transport)
        mism = [natural_key(r) for r in transport
                if abs(sum(_f0(r.get(c)) for c in REV_COMPONENTS)
                       - _f0(r.get("Total_Rev"))) > 0.005]
        payload = {
            "reported_total_transportation": reported,
            "sign_split_transportation_total_rev": sign_split(transport, "Total_Rev"),
            "components_transportation": comps,
            "component_sum_transportation": sum(comps.values()),
            "rows_where_components_disagree_with_their_own_total": mism,
            "revenue_by_service_type_as_filed": by_service,
            "order_735a_note": ("Order 735-A states annual customer revenue excludes storage "
                                "services; the rows above are what the source actually "
                                "reports, by Service_Type, without applying that exclusion"),
        }
        qa = ["COMPONENTS ARE RECONCILED TO THEIR OWN REPORTED TOTAL, never summed to produce "
              f"one: {len(mism)} of {len(transport)} transportation rows disagree with their "
              "own Total_Rev, and table-wide the worst cases carry every component at 0 while "
              "Total_Rev holds the whole figure",
              "revenue_by_service_type_as_filed carries the NON-transportation revenue "
              "(storage, parking, other) that Order 735-A says should not be here. It is "
              "reported, and it is never added into the transportation figure",
              f"a 0 component is not a measured zero: {ZERO_FILL_NOTE}"]
        if unresolved:
            qa.append(block_note)
        if not real:
            obs.append(_absent(entity_key, m, s, tax, availability=Availability.NOT_APPLICABLE,
                               validation=Validation.PASS,
                               reason="the Q4 filing carries no non-placeholder contract rows",
                               qa=common()))
        else:
            o = _obs(entity_key, m, periods.ANNUAL, start, end, "", year, "Q4",
                     value_text=json.dumps(payload, sort_keys=True), value_num=None,
                     unit="iso4217:USD",
                     availability=Availability.INTERPRETATION_BLOCKED if blocked
                     else Availability.PRESENT,
                     version_status=vstat,
                     validation=Validation.BLOCKED_AMBIGUITY if blocked else Validation.PASS,
                     qa_flags=common(" || ".join(qa)), filing_id=fid,
                     missing_reason=("the grain of the Q4 filing is unresolved and an "
                                     "ambiguous group holds more than one revenue-bearing row"
                                     if blocked else ""),
                     derivation="component sums compared against the reported Total_Rev",
                     tax=tax, candidate_count=len(transport),
                     applicability_evidence=s["requirement_evidence"])
            first = []
            if rev_obs is not None:
                first = [_edge(o["observation_id"], 1, "basis", "", fid, None,
                               "i311_annual_transport_revenue", f"{start}..{end}",
                               rev_obs["value_text"], "iso4217:USD", vstat,
                               obs_input=rev_obs["observation_id"])]
            edges.extend(first + [
                _edge(o["observation_id"], i, "addend", "+", fid, natural_key(r),
                      "|".join(REV_COMPONENTS), f"{start}..{end}",
                      json.dumps({c: r.get(c) for c in REV_COMPONENTS}), "iso4217:USD", vstat)
                for i, r in enumerate(transport, len(first) + 1)])
            # A08: a storage-only or parking-only filer has NO transportation
            # rows, so the comprehension above is empty and this observation
            # previously carried no lineage. The population records that the
            # transportation set is empty and, crucially, WHY.
            pop, pedge = _population(
                o["observation_id"], filing_ids=[fid],
                inclusion=("non-placeholder Transportation rows of the Q4 filing; their "
                           "four annual revenue components (fields 68-71) are summed and "
                           "RECONCILED AGAINST the reported Total_Rev, never summed to "
                           "produce it"),
                exclusion=("non-transportation Service_Type rows -- their as-filed revenue "
                           "is reported in revenue_by_service_type_as_filed but is outside "
                           "the field's transportation-only definition; all-zero "
                           "placeholder rows"),
                members=transport, candidates=len(real),
                aggregate_value=f"{reported:.2f}", aggregate_unit="iso4217:USD",
                empty_reason=("" if transport else
                              f"{len(real)} non-placeholder row(s) were retrieved and NONE "
                              "is a Transportation row, so the transportation component "
                              "total is a measured absence of transportation service, not "
                              "an unretrieved figure"),
                note=(f"{len(mism)} of {len(transport)} transportation rows disagree with "
                      "their own Total_Rev"))
            edges.append(pedge)
            o.setdefault("_populations", []).append(pop)
            obs.append(o)

    # --------------------------------------------------------- grain diagnostic
    s = slots.get("i311_revenue_grain_diagnostic")
    if s is not None:
        m = metrics["i311_revenue_grain_diagnostic"]
        payload = {k: v for k, v in grain.items() if not k.startswith("_")}
        payload["audit_scenarios_total_rev_transportation"] = scen
        payload["grain_resolved"] = grain["resolved"]
        payload["annual_revenue_total_asserted"] = not blocked
        payload["attribution_ambiguity"] = {
            "shipper": attribution_ambiguous(grain, "Total_Rev", "Shipper_Name", is_transport),
            "affiliate": attribution_ambiguous(grain, "Total_Rev", "Affiliate_Status",
                                               is_transport),
            "contract_end": attribution_ambiguous(grain, "Total_Rev", "Contract_End",
                                                  is_transport)}
        qa = ["the grain is probed PER (filer, period): a multi-row contract group is only "
              "called service legs when a SERVICE-DEFINING field differs (service type, "
              "points, rate schedule, docket or a rate). A group differing only in "
              "counterparty or contract term is AMBIGUOUS",
              "whether an ambiguous group actually blocks a given aggregate is then decided "
              "structurally: a TOTAL moves only when a group holds more than one non-zero row "
              "of that column; an ATTRIBUTION (which shipper, which affiliate flag, which "
              "expiry) is uncertain as soon as the group carries weight and the field "
              "disagrees inside it",
              "FORBIDDEN AND NOT USED: any 'same amount means duplicate' rule, and any "
              "divergence threshold used as permission to call an uncertain sum correct. "
              "The two scenarios are labelled treatments, NOT statistical confidence bounds. "
              "Two identical non-zero values BLOCK the total here rather than collapsing it",
              "a grain change over time is not smoothed: this diagnostic is per period, so a "
              "filer whose grain changes -- as Bridgeline's did around 2015, from contract x "
              "service-type x point-leg to one row per contract -- is visible rather than "
              "averaged away, and no continuous series is built across the break"]
        if unresolved:
            qa.append(block_note)
        o = _obs(entity_key, m, periods.ANNUAL, start, end, "", year, "Q4",
                 value_text=json.dumps(payload, sort_keys=True, default=str),
                 value_num=None, unit="categorical", availability=Availability.PRESENT,
                 version_status=vstat, validation=Validation.PASS,
                 qa_flags=common(" || ".join(qa)), filing_id=fid,
                 derivation="probe_grain over the Q4 filing's contract rows",
                 tax=tax, candidate_count=len(real),
                 applicability_evidence=s["requirement_evidence"])
        edges.extend([_edge(o["observation_id"], i, "group_member", "", fid, natural_key(r),
                            "Contract_Number", f"{start}..{end}", _s(r.get("Contract_Number")),
                            "", vstat) for i, r in enumerate(real, 1)])
        # A08: a filing of nothing but placeholder rows leaves `real` empty and
        # left this diagnostic with no lineage.
        pop, pedge = _population(
            o["observation_id"], filing_ids=[fid],
            inclusion="every non-placeholder contract row of the Q4 filing, of any "
                      "Service_Type: the grain probe is about ROW STRUCTURE and is "
                      "deliberately not scoped to transportation",
            exclusion=f"all-zero placeholder rows ({ZERO_FILL_NOTE})",
            members=real, candidates=len(rows),
            aggregate_value=grain["grain_class"], aggregate_unit="categorical",
            empty_reason=("" if real else
                          f"the filing carries {len(rows)} row(s) and every one is an "
                          "all-zero placeholder, so there is no grain to probe. This is "
                          "a property of the filing, not a retrieval failure"),
            note="grain diagnostic population")
        edges.append(pedge)
        o.setdefault("_populations", []).append(pop)
        obs.append(o)

    return obs, edges


# ------------------------------------------------------------------ remainder

def _account_for_remainder(entity_key, metrics, expected, produced, tax) -> list[dict]:
    """Every frozen slot must end in a measured status with a reason. A slot left
    silently empty is indistinguishable from unfinished engineering."""
    have = {(o["metric_id"], o["period_basis"], o.get("period_start") or "",
             o.get("period_end") or "") for o in produced}
    out = []
    for slot in expected:
        key = (slot["metric_id"], slot["period_basis"], slot.get("period_start") or "",
               slot.get("period_end") or "")
        if key in have:
            continue
        m = metrics.get(slot["metric_id"])
        if m is None:
            continue
        out.append(_absent(entity_key, m, slot, tax,
                           availability=Availability.NOT_IMPLEMENTED,
                           validation=Validation.NOT_YET_VALIDATED,
                           reason=("no observation was produced for this requested grain; this "
                                   "is OUR unfinished engineering, not a FERC data condition")))
    return out
