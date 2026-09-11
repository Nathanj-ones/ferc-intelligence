"""
FERC Oil Pipeline Index adapter (A18).

WHAT THIS MEASURES, AND WHAT IT IS NOT
--------------------------------------
This is the **FERC Oil Pipeline Index**: the annual ceiling-level adjustment
applied to interstate oil pipeline rates under 18 CFR 342.3. It is NOT a
commodity oil price, it is NOT a carrier's actual tariff change, and it is NOT
a revenue or cost measure. The legacy metric name ``liq_oil_price_index`` reads
as a commodity price series and is therefore RENAMED here; see
``METRIC`` / ``LEGACY_METRIC`` / ``metric_crosswalk()`` below.

The index has two distinct published quantities, and they are NOT the same
number. Both are carried, because FERC publishes both and they are used
differently:

  * the **multiplier** (e.g. ``1.014290``) -- the operative quantity. 18 CFR
    342.3(d) works on this directly: "Oil pipelines must multiply their July 1,
    2025, through June 30, 2026 index ceiling levels by positive 1.014290 to
    compute their index ceiling levels for July 1, 2026, through June 30, 2027".
    It is DIMENSIONLESS (a ratio of ceiling to ceiling), never a percent.
  * the **index figure** (e.g. ``0.014290``) -- the fractional change, which
    FERC states "expressed as a decimal". Displayed as 1.4290%.

Storing the multiplier under a ``percent`` unit is a category error: 1.014290
is not 1.014290%. That is why this adapter declares two metrics with different
unit contracts rather than one.

EFFECTIVE INTERVALS
-------------------
The index year runs 1 July to 30 June. Each annual notice states the multiplier
that carries the PREVIOUS index year's ceiling forward into the NEXT one, so a
record's ``interval_start``/``interval_end`` is the interval the factor
PRODUCES, not the interval it reads from.

VERSION AND ERRATUM HISTORY
---------------------------
A published factor is not necessarily final. The July 1, 2021 - June 30, 2022
index year passed through three successive official states, and all three are
retained:

  1. ORIGINAL    0.994188, published 2021-05-24, computed on PPI-FG+0.78%.
  2. SUPERSEDED  0.984288, published 2022-01-26, "to recompute their July 1,
     2021 through June 30, 2022 index ceiling levels, to be effective March 1,
     2022", computed on PPI-FG-0.21% per the Order on Rehearing.
  3. REINSTATED  the D.C. Circuit vacated that Order on Rehearing (LEPA v.
     FERC) and the Commission reinstated the original index level, so
     0.994188 stands again and 0.984288 is vacated.

Note the shape: the superseding factor had its own EFFECTIVE date (2022-03-01)
falling INSIDE the index year it restated, so an index year needs both the
interval a factor applies to and the date it became operative. Two functions
keep those apart and neither substitutes for the other:

  * ``current_record(interval)``  -- what the Commission's currently-standing
    orders say. Vacated factors excluded.
  * ``operative_record(date)``    -- what was in force ON a date, vacated
    factors INCLUDED, because a vacated factor really did govern filed rates
    while it stood.

HOW VERSION HISTORY IS PERSISTED (and why not as extra observations)
--------------------------------------------------------------------
``ux_observation_grain`` enforces one observation per
(entity, metric, regime, basis, period, scope, unit, method). Two published
states of one index year share every one of those columns, so they cannot both
be observations -- verified: the second insert is rejected outright -- and
forcing them apart by inventing a grain difference would be a lie about the
data. Nor is the constraint wrong: A08 asks for a "deterministic current view",
which is exactly one row per grain.

So provenance is preserved in the three layers built for it, and the canonical
observation carries only the currently-standing factor:

  * ``filings``       -- one occurrence per published notice, with
                         ``is_canonical=0`` on a superseded or vacated one.
  * ``source_facts``  -- the as-filed value of EVERY published state, keyed
                         ``{fr_document}:{metric}``. This is the layer that
                         answers "what did FERC publish for 2021-22 before the
                         restatement?", and it is why a lineage edge naming a
                         superseded state resolves to a real row rather than to
                         a synthesised id.
  * ``lineage_edges`` -- one ``basis`` edge from the canonical observation to
                         each superseded state's fact, carrying the superseded
                         factor as ``input_value``.

The history is therefore traversable from the observation and queryable without
it, while the observations table keeps exactly one answer per index year.

SOURCE PROVENANCE
-----------------
Every factor below was extracted from the official published text of the
Commission's notice.  The package carries the authoritative electronic PDF
from GPO govinfo, the FederalRegister.gov metadata response, and the government
text rendition used for deterministic value checks.  Every captured response
has its request URL/time/status, final URL, byte count, and SHA-256 in a
versioned manifest.  ``ferc.gov``'s summary table is a convenience reproduction
of these notices, not the authority and not a runtime dependency.

SOURCE ANOMALIES ARE RECORDED, NOT CORRECTED
--------------------------------------------
Several official texts contain internal wording defects. None is silently
"fixed"; material conflicts travel as ``source_anomaly`` (and remain in review)
while an unrelated historical typo travels as a prominent ``note``:

  * FR 2025-09243 states the BASE interval as "July 1, 2025, through June 30,
    2026" -- identical to the target interval. The arithmetic (2023->2024 PPI-FG
    plus 0.78%) shows the base must be the 2024-25 index year. The FACTOR and
    the TARGET interval are unambiguous; only the base statement is defective.
  * FR 03-12949 states "by negative 0.987207". A multiplier below 1 is not a
    negative number; the adjacent index figure is what is negative
    (-0.012793). Compare FR 2016-12423, which says index figure "negative
    0.020135" but multiplier "positive 0.979865".
  * FR 00-13115 states the operative multiplier as 1.007598 in its summary and
    operative body, but prints 1.007698 once in the closing historical guide.
  * FR E7-12192 gives PPI-FG figures for 2005 and 2006, then calls the comparison
    "from 2006 to 2007". The factor and target interval remain explicit.
  * FR 97-13604 calls one ancillary 1995 series "PPI-FD" and prints a different
    prior-period multiplier than later notices. Neither typo changes the
    notice's repeated 1997-98 operative factor, so this is a warning, not a gate.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
import sys
import urllib.parse
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ferclib import coverage, periods                                        # noqa: E402
from ferclib.applicability import EvidenceKind, SourceHealth                 # noqa: E402
from ferclib.registry import BY_ID, REGISTRY_VERSION                         # noqa: E402
from ferclib.staging import observation_id, slot_id                          # noqa: E402
from ferclib.status import (Availability, Method, Origin,                    # noqa: E402
                            Validation, VersionStatus)

ADAPTER = "oil_index"
SOURCE_SYSTEM = "FederalRegister_FERC_Notice"

#: `source_regime` for every row this adapter emits.
REGIME = "FERC Oil Pipeline Index"

#: Stable machine-readable token marking a factor computed on a five-year index
#: level that was later struck down, for which FERC published no restatement.
#: The factor stands -- it is the only one published for its index year -- but a
#: consumer is entitled to learn that from the value, not only from a report.
VACATED_LEVEL_MARKER = "[VACATED_FIVE_YEAR_LEVEL]"

#: The renamed metric. `liq_oil_price_index` reads as a commodity price series;
#: this name says what the number actually is and what it does.
METRIC = "liq_oil_pipeline_index_factor"

#: The fractional change FERC publishes alongside the multiplier.
METRIC_CHANGE = "liq_oil_pipeline_index_change"

#: Retained ONLY so existing references resolve. Never emitted as a metric_id.
LEGACY_METRIC = "liq_oil_price_index"

#: The index is industry-wide. It is not attributable to a carrier, and it must
#: never be presented as one carrier's rate or rate change.
SCOPE = "industry-wide oil pipeline rate-ceiling adjustment, not a carrier rate"

#: The single synthetic key this instrument is published under.
#:
#: The baseline fanned ONE national index across 28 individual carriers, as if
#: each had its own. That is a scope error under repair-contract rule 3.3 --
#: the filing entity and an industry-wide instrument are different scopes -- and
#: it would let a consumer sum one national index 28 times. The index is
#: published exactly once per index year against this key, which is deliberately
#: NOT a FERC CID and NOT a filing entity.
INDUSTRY_KEY = "FERC-OIL-INDEX"
_INDUSTRY_NOTE = (
    "Not a filing entity. A synthetic key carrying the industry-wide "
    "rate-ceiling index published under 18 CFR 342.3 in Docket No. "
    "RM93-11-000. No carrier may be attributed this value, and it must "
    "never be aggregated across carriers."
)

# Database-ready identity.  Keep this separate from GLOBAL_RUN_ENTITY: the
# latter deliberately contains runner-only keys (template/assets), while this
# mapping is safe to pass directly to Staging.write_entities().
INDUSTRY_ENTITY = {
    "entity_key": INDUSTRY_KEY,
    "cid": None,
    "local_key": INDUSTRY_KEY,
    "legal_name": "FERC Oil Pipeline Index (industry-wide instrument)",
    "parent": None,
    "ticker": None,
    "jurisdiction": "United States interstate oil pipeline rate regulation",
    "note": _INDUSTRY_NOTE,
}

# Runner-ready identity.  Normal adapters receive the shape produced by
# run.entities_from(); this synthetic regulatory instrument must enter through
# the same explicit surface, but it has no asset mapping and no carrier CID.
# Orchestration can therefore route this one record without ever presenting the
# adapter with each liquids carrier in the operating universe.
GLOBAL_RUN_ENTITY = {
    "entity_key": INDUSTRY_KEY,
    "legal_name": INDUSTRY_ENTITY["legal_name"],
    "template": "liquids",
    "parent": "",
    "ticker": "",
    "assets": [],
}

DOCKET = "RM93-11-000"

# The table and its captured source bodies are versioned independently of the
# package build.  This lets a later source correction become a new evidence
# input rather than silently changing the bytes behind an old assertion.
SOURCE_TABLE_VERSION = "oil-index-table-2026-09-09-v3"
SOURCE_BINDING_REVISION = 3
SOURCE_BUNDLE_VERSION = "oil-index-official-sources-2026-09-09-v1"
SOURCE_BUNDLE_ROOT = (
    pathlib.Path(__file__).resolve().parent.parent
    / "inputs" / "official_ferc" / "oil_pipeline_index" / "v1")
SOURCE_MANIFEST_PATH = SOURCE_BUNDLE_ROOT / "manifest.json"

#: The three failure classes below are deliberately distinct. Collapsing any of
#: them into "FERC outage" was the A16 defect and is forbidden here.
CLASS_MISSING_CREDENTIAL = "local_configuration_missing_credential"
CLASS_NOT_ATTEMPTED = "no_request_was_attempted"
CLASS_CACHE_MISS = "offline_replay_cache_miss"
CLASS_ACCESS_BLOCKED = "host_access_blocked_by_publisher"
CLASS_FERC_UNAVAILABLE = "ferc_source_unavailable"


# --------------------------------------------------------------------------
# The official table.
#
# `factor`   : the multiplier, as a string so no float ever rounds it.
# `change`   : the index figure (fractional change), as published.
# `level`    : the five-year index level the factor was computed on.
# `applies`  : (interval_start, interval_end) the factor PRODUCES.
# `effective`: the date the factor became operative. Equal to interval_start
#              except where a later order restated the interval mid-year.
# --------------------------------------------------------------------------

def _rec(applies, factor, *, fr_doc, published, change="", level="",
         version=VersionStatus.ORIGINAL, effective="", supersedes="",
         superseded_by="", note="", anomaly="", vacated="", reinstated=""):
    start, end = applies
    return {
        # `vacated` names the order that struck this factor down; a vacated
        # factor is NEVER the currently-standing value for its index year, but
        # it is retained because it governed filed rates while in force.
        "vacated_by": vacated,
        # `reinstated` names the order that restored this factor after it had
        # been superseded. A reinstated factor stands again.
        "reinstated_by": reinstated,
        "interval_start": start,
        "interval_end": end,
        "effective_from": effective or start,
        "factor": factor,
        "index_change": change,
        "five_year_level": level,
        "fr_document": fr_doc,
        "published": published,
        "version_status": version,
        "supersedes": supersedes,
        "superseded_by": superseded_by,
        "note": note,
        "source_anomaly": anomaly,
        "docket": DOCKET,
        "citation": f"Federal Register doc. {fr_doc}, published {published} "
                    f"(Docket No. {DOCKET})",
    }


INDEX_TABLE: list[dict] = [
    _rec(("1997-07-01", "1998-06-30"), "1.016583", change="0.016583",
         level="PPI-FG-1%", fr_doc="97-13604", published="1997-05-23",
         note="ANCILLARY SOURCE TYPO: the historical guidance calls the 1995 "
              "series 'PPI-FD' and prints prior-period multiplier 0.996514, "
              "where later notices print 0.996415. Neither affects this "
              "notice's repeated 1997-98 factor/change/target passage."),
    _rec(("1998-07-01", "1999-06-30"), "0.993808", change="-0.006192",
         level="PPI-FG-1%", fr_doc="98-12930", published="1998-05-15"),
    _rec(("1999-07-01", "2000-06-30"), "0.981654", change="-0.018346",
         level="PPI-FG-1%", fr_doc="99-13121", published="1999-05-26"),
    _rec(("2000-07-01", "2001-06-30"), "1.007598", change="0.007598",
         level="PPI-FG-1%", fr_doc="00-13115", published="2000-05-25",
         anomaly="the summary and operative body twice publish 1.007598, but "
                 "the closing historical guidance prints 1.007698 once. The "
                 "record retains the operative value and flags the conflicting "
                 "alternate text for review."),
    _rec(("2001-07-01", "2002-06-30"), "1.027594", change="0.027594",
         level="PPI-FG-1%", fr_doc="01-13093", published="2001-05-24"),
    _rec(("2002-07-01", "2003-06-30"), "1.009565", change="0.009565",
         level="PPI-FG-1%", fr_doc="02-12748", published="2002-05-22"),
    _rec(("2003-07-01", "2004-06-30"), "0.987207", change="-0.012793",
         level="PPI-FG-1%", fr_doc="03-12949", published="2003-05-23",
         anomaly="the notice reads 'by negative 0.987207'; a multiplier below 1 "
                 "is not a negative number. The word 'negative' belongs to the "
                 "index figure (-0.012793). Recorded as published, not corrected."),
    _rec(("2004-07-01", "2005-06-30"), "1.031677", change="0.031677",
         level="PPI-FG+1.3%", fr_doc="E4-1200", published="2004-05-24"),
    _rec(("2005-07-01", "2006-06-30"), "1.036288", change="0.036288",
         level="PPI-FG+1.3%", fr_doc="E5-2623", published="2005-05-25"),
    _rec(("2006-07-01", "2007-06-30"), "1.061485", change="0.061485",
         level="PPI-FG+1.3%", fr_doc="E6-7963", published="2006-05-24"),
    _rec(("2007-07-01", "2008-06-30"), "1.043186", change="0.043186",
         level="PPI-FG+1.3%", fr_doc="E7-12192", published="2007-06-25",
         anomaly="the notice states PPI-FG figures 155.7 for 2005 and 160.4 "
                 "for 2006, but the next sentence calls the comparison 'from "
                 "2006 to 2007'. The published factor, change and target "
                 "interval are explicit; the base-year wording is inconsistent."),
    _rec(("2008-07-01", "2009-06-30"), "1.051653", change="0.051653",
         level="PPI-FG+1.3%", fr_doc="E8-11912", published="2008-05-29"),
    _rec(("2009-07-01", "2010-06-30"), "1.076025", change="0.076025",
         level="PPI-FG+1.3%", fr_doc="E9-11794", published="2009-05-21"),
    _rec(("2010-07-01", "2011-06-30"), "0.987026", change="-0.012974",
         level="PPI-FG+1.3%", fr_doc="2010-12622", published="2010-05-26"),
    _rec(("2011-07-01", "2012-06-30"), "1.068819", change="0.068819",
         level="PPI-FG+2.65%", fr_doc="2011-12303", published="2011-05-19"),
    _rec(("2012-07-01", "2013-06-30"), "1.086011", change="0.086011",
         level="PPI-FG+2.65%", fr_doc="2012-12198", published="2012-05-21"),
    _rec(("2013-07-01", "2014-06-30"), "1.045923", change="0.045923",
         level="PPI-FG+2.65%", fr_doc="2013-12128", published="2013-05-22"),
    _rec(("2014-07-01", "2015-06-30"), "1.038858", change="0.038858",
         level="PPI-FG+2.65%", fr_doc="2014-11717", published="2014-05-21"),
    _rec(("2015-07-01", "2016-06-30"), "1.045829", change="0.045829",
         level="PPI-FG+2.65%", fr_doc="2015-12182", published="2015-05-20"),
    _rec(("2016-07-01", "2017-06-30"), "0.979865", change="-0.020135",
         level="PPI-FG+1.23%", fr_doc="2016-12423", published="2016-05-26"),
    _rec(("2017-07-01", "2018-06-30"), "1.001985", change="0.001985",
         level="PPI-FG+1.23%", fr_doc="2017-10125", published="2017-05-19"),
    _rec(("2018-07-01", "2019-06-30"), "1.044087", change="0.044087",
         level="PPI-FG+1.23%", fr_doc="2018-10756", published="2018-05-21"),
    _rec(("2019-07-01", "2020-06-30"), "1.043108", change="0.043108",
         level="PPI-FG+1.23%", fr_doc="2019-10297", published="2019-05-17"),
    _rec(("2020-07-01", "2021-06-30"), "1.020139", change="0.020139",
         level="PPI-FG+1.23%", fr_doc="2020-10968", published="2020-05-21"),

    # ---- the contested index year: three official states, all retained ----
    _rec(("2021-07-01", "2022-06-30"), "0.994188", change="-0.005812",
         level="PPI-FG+0.78%", fr_doc="2021-10860", published="2021-05-24",
         version=VersionStatus.ORIGINAL, superseded_by="2022-01521",
         reinstated="188 FERC para 61,173 (2024) (Order Reinstating Index)",
         note="Original annual notice, computed on the five-year level set by "
              "the December 2020 Order (Five-Year Review of the Oil Pipeline "
              "Index, 173 FERC para 61,245). Superseded by FR 2022-01521 "
              "effective 1 March 2022, then REINSTATED when the D.C. Circuit "
              "vacated the Order on Rehearing that FR 2022-01521 implemented "
              "(LEPA v. FERC) and the Commission reinstated the original index "
              "level by order issued 17 September 2024. THIS IS THE FACTOR THAT "
              "CURRENTLY STANDS for the 2021-22 index year; 0.984288 governed "
              "filed rates between 2022-03-01 and the vacatur and is retained "
              "as provenance."),
    _rec(("2021-07-01", "2022-06-30"), "0.984288", change="-0.015712",
         level="PPI-FG-0.21%", fr_doc="2022-01521", published="2022-01-26",
         version=VersionStatus.SUPERSEDED, effective="2022-03-01",
         supersedes="2021-10860",
         vacated="LEPA v. FERC (D.C. Cir. 2024), implemented by 188 FERC para "
                 "61,173 (2024) (Order Reinstating Index)",
         note="RECOMPUTATION, not a new index year. 'In accordance with the "
              "Order on Rehearing, oil pipelines must multiply their July 1, "
              "2020 through June 30, 2021 index ceiling levels by positive "
              "0.984288 to recompute their July 1, 2021 through June 30, 2022 "
              "index ceiling levels, to be effective March 1, 2022.' Issued "
              "under Five-Year Review of the Oil Pipeline Index, 178 FERC para "
              "61,023 at P 105-106 (2022) (Order on Rehearing); rehearing "
              "denied, 179 FERC para 61,100 (2022). That Order on Rehearing was "
              "later VACATED by the D.C. Circuit in LEPA v. FERC, and the "
              "Commission reinstated the original index level by order issued "
              "17 September 2024 (188 FERC para 61,173). This factor is "
              "retained because it governed filed rates from 2022-03-01 until "
              "the vacatur; it is not the currently operative factor."),

    _rec(("2022-07-01", "2023-06-30"), "1.087107", change="0.087107",
         level="PPI-FG-0.21%", fr_doc="2022-11037", published="2022-05-23",
         note=VACATED_LEVEL_MARKER + " Computed on the Rehearing Index "
              "(PPI-FG-0.21%), the five-year level later VACATED in LEPA v. "
              "FERC. FERC published NO restated multiplier for this index year "
              "after the reinstatement, so this remains the only published "
              "factor for it and it stands -- but it rests on an index level "
              "that no longer stands, and a rate computed from it inherits that "
              "qualification."),
    _rec(("2023-07-01", "2024-06-30"), "1.133194", change="0.133194",
         level="PPI-FG-0.21%", fr_doc="2023-10832", published="2023-05-22",
         note=VACATED_LEVEL_MARKER + " Computed on the Rehearing Index "
              "(PPI-FG-0.21%), later VACATED in LEPA v. FERC. No restated "
              "multiplier was published for this index year, so this factor "
              "stands as the only published value while resting on an index "
              "level that no longer stands."),
    _rec(("2024-07-01", "2025-06-30"), "1.012647", change="0.012647",
         level="PPI-FG-0.21%", fr_doc="2024-11147", published="2024-05-21",
         note=VACATED_LEVEL_MARKER + " Computed on the Rehearing Index "
              "(PPI-FG-0.21%), later VACATED in LEPA v. FERC. No restated "
              "multiplier was published for this index year, so this factor "
              "stands as the only published value while resting on an index "
              "level that no longer stands."),
    _rec(("2025-07-01", "2026-06-30"), "1.019976", change="0.019976",
         level="PPI-FG+0.78%", fr_doc="2025-09243", published="2025-05-22",
         note="First annual notice computed on the reinstated PPI-FG+0.78% "
              "level (Order Reinstating Index, 188 FERC para 61,173 (2024)).",
         anomaly="the notice states the BASE interval as 'July 1, 2025, through "
                 "June 30, 2026', identical to the target interval. The stated "
                 "arithmetic (annual average PPI-FG 254.6 for 2023 and 257.7 "
                 "for 2024, plus 0.78%) shows the base must be the 2024-25 "
                 "index year. Factor and target interval are unambiguous; the "
                 "base statement is defective and is recorded as published."),
    _rec(("2026-07-01", "2027-06-30"), "1.014290", change="0.014290",
         level="PPI-FG-0.55%", fr_doc="2026-09998", published="2026-05-19",
         note="Five-year level PPI-FG-0.55% established for the period "
              "commencing 1 July 2026 by the Order Establishing Index Level "
              "issued 24 April 2026, Five-Year Review of the Oil Pipeline "
              "Index, 195 FERC para 61,062 (2026). Annual average PPI-FG 257.7 "
              "for 2024 and 262.8 for 2025; [262.8-257.7]/257.7 = 0.019790 - "
              "0.0055 = 0.014290; 1 + 0.014290 = 1.014290."),
]


#: Exactly what was attempted against the publisher, so a local or publisher-side
#: failure is never reported as a FERC outage.
RETRIEVAL_ATTEMPTS: list[dict] = [
    {"target": "https://www.ferc.gov/general-information-1/oil-pipeline-index",
     "what": "FERC's own summary table of all prior multipliers",
     "attempted": True,
     "requested_at": "2026-09-09T20:57:38+00:00",
     "outcome": "HTTP 403; 5,608-byte challenge response captured",
     "classification": CLASS_ACCESS_BLOCKED,
     "evidence_path": (
         "inputs/official_ferc/oil_pipeline_index/v1/supplemental/"
         "ferc_summary_http_403.html"),
     "explicitly_not": "This is NOT a FERC outage and NOT evidence that the "
                       "official notices are unavailable. The summary page is "
                       "not the cited authority and no value depends on it."},
    {"target": "inputs/official_ferc/oil_pipeline_index/v1/manifest.json",
     "what": "pinned official Federal Register occurrence evidence",
     "attempted": False,
     "outcome": "offline read of captured bytes; per-request URL, time, HTTP "
                "status, final URL, byte count and SHA-256 are in the manifest",
     "classification": "captured_official_evidence",
     "explicitly_not": "No network retrieval occurs in the runtime adapter and "
                       "no HTTP outcome is inferred from a citation."},
]


# --------------------------------------------------------------------------
# Lookup
# --------------------------------------------------------------------------

def metric_crosswalk() -> dict:
    """Compatibility map from the old ambiguous name to the new ones.

    The old metric conflated a multiplier with a percent. It therefore maps to
    TWO metrics, and a consumer must choose; there is no silent single answer.
    """
    return {
        LEGACY_METRIC: {
            "renamed_to": METRIC,
            "companion": METRIC_CHANGE,
            "reason": "'oil_price_index' names a commodity price series. The "
                      "measure is the FERC Oil Pipeline Index: a regulatory "
                      "rate-CEILING adjustment under 18 CFR 342.3.",
            "unit_change": "the legacy metric declared unit 'percent' while "
                           "carrying no value. The multiplier is dimensionless "
                           "and must never be labelled percent; the fractional "
                           "change is the percent-displayable quantity.",
            "not_equivalent": "liq_oil_price_index is NOT a drop-in alias for "
                              "either new metric: factor = 1 + change, so a "
                              "consumer reading the old name must decide which "
                              "quantity it meant.",
        }
    }


def _d(s: str) -> Decimal:
    return Decimal(s)


def records_for_interval(start: str) -> list[dict]:
    """Every published state of one index year, oldest publication first."""
    return sorted((r for r in INDEX_TABLE if r["interval_start"] == start),
                  key=lambda r: r["published"])


def current_record(interval_start: str) -> dict | None:
    """The factor that CURRENTLY STANDS for one index year.

    Distinct from `operative_record`, and the distinction is load-bearing:

      * `current_record`  -- what the Commission's currently-standing orders say
                             for this index year. Vacated factors are excluded.
                             This is the "single current canonical view" the
                             observation grain permits one row of.
      * `operative_record` -- what was in force on a given DATE, vacated factors
                             included, because a vacated factor really did
                             govern filed rates while it stood.

    Neither is a substitute for the other, and neither deletes anything.
    """
    live = [r for r in INDEX_TABLE
            if r["interval_start"] == interval_start and not r["vacated_by"]]
    if not live:
        return None
    # Latest publication that has not been struck down.
    return sorted(live, key=lambda r: r["published"])[-1]


def superseded_states(interval_start: str) -> list[dict]:
    """Every published state of an index year that is NOT the current one."""
    cur = current_record(interval_start)
    return [r for r in records_for_interval(interval_start)
            if cur is None or r["fr_document"] != cur["fr_document"]]


def operative_record(on_date: str) -> dict | None:
    """The factor operative for the index year containing `on_date`.

    Where an index year has several published states, the operative one is the
    latest whose `effective_from` has arrived. A later-vacated state can be
    returned for dates when it actually governed; it is never deleted merely
    because it is not the currently-standing answer.
    """
    cands = [r for r in INDEX_TABLE
             if r["interval_start"] <= on_date <= r["interval_end"]
             and r["effective_from"] <= on_date]
    if not cands:
        return None
    return sorted(cands, key=lambda r: (r["effective_from"], r["published"]))[-1]


def verify_table() -> list[str]:
    """Internal consistency of the published table. Returns problems, not bools.

    Every captured notice explicitly publishes both the change/index figure and
    the multiplier. Both must be retained, and their arithmetic must agree;
    this is a source-backed completeness control, not a derived-value fill.
    """
    problems = []
    for r in INDEX_TABLE:
        if not r["index_change"]:
            problems.append(
                f"{r['interval_start']} {r['fr_document']}: published change omitted")
        else:
            lhs = _d(r["factor"])
            rhs = Decimal(1) + _d(r["index_change"])
            if lhs != rhs:
                problems.append(
                    f"{r['interval_start']} {r['fr_document']}: factor {lhs} != "
                    f"1 + change {r['index_change']} = {rhs}")
        if r["effective_from"] < r["interval_start"]:
            problems.append(f"{r['interval_start']} {r['fr_document']}: "
                            "effective_from precedes the interval")
        if not (r["fr_document"] and r["published"]):
            problems.append(f"{r['interval_start']}: record has no source document")
    # every interval must have exactly one operative factor at its start
    for start in {r["interval_start"] for r in INDEX_TABLE}:
        if operative_record(start) is None:
            problems.append(f"{start}: no operative factor at interval start")
    return problems


def _official_https_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(str(url or ""))
    return (parsed.scheme == "https"
            and parsed.hostname in {
                "www.federalregister.gov", "federalregister.gov",
                "www.govinfo.gov", "govinfo.gov",
                "www.ferc.gov", "ferc.gov"})


def _text_has_decimal(text: str, value: str) -> bool:
    """Match a published decimal without conflating factor and change.

    Older Federal Register text often omits the leading zero (``.016583``) and
    sometimes spells a sign as ``negative``.  A digit boundary prevents the
    change from being found merely as the tail of its ``1 + change`` factor.
    """
    if not value:
        return True
    negative = value.startswith("-")
    digits = re.escape(value.lstrip("+-0"))
    sign = r"(?:negative\s+|-\s*)" if negative else r"(?:positive\s+|\+\s*)?"
    return re.search(
        rf"(?<![0-9]){sign}0?{digits}(?![0-9])",
        " ".join(text.split()), flags=re.IGNORECASE) is not None


def _text_supports_interval(text: str, record: dict) -> bool:
    """Require an operative multiply/compute passage with the target interval."""
    flat = " ".join(text.split())
    start_year = int(record["interval_start"][:4])
    end_year = int(record["interval_end"][:4])
    target_start = re.compile(rf"July 1,?\s*{start_year}", re.IGNORECASE)
    target_end = re.compile(rf"June 30,?\s*{end_year}", re.IGNORECASE)
    for match in re.finditer(re.escape(record["factor"]), flat):
        passage = flat[max(0, match.start() - 260):match.end() + 420]
        if ("multiply" in passage.lower()
                and "compute" in passage.lower()
                and target_start.search(passage)
                and target_end.search(passage)):
            if record["effective_from"] != record["interval_start"]:
                effective = dt.date.fromisoformat(record["effective_from"])
                label = f"{effective.strftime('%B')} {effective.day}, {effective.year}"
                if label.lower() not in passage.lower():
                    continue
            return True
    return False


def _safe_bundle_file(root: pathlib.Path, relative: object) -> tuple[pathlib.Path | None, str]:
    value = str(relative or "")
    rel = pathlib.PurePosixPath(value)
    if not value or rel.is_absolute() or ".." in rel.parts:
        return None, f"unsafe evidence path {value!r}"
    path = root.joinpath(*rel.parts)
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None, f"evidence path escapes bundle root: {value!r}"
    if path.is_symlink():
        return None, f"evidence path is a symlink: {value!r}"
    return path, ""


def verify_source_bundle(*, filing_ids=None, bundle_root=None,
                         manifest_path=None) -> list[str]:
    """Validate captured official bytes and their semantic binding to the table.

    ``filing_ids`` permits bounded refreshes to validate only their selected
    occurrences while a full call requires the complete table.  This function
    performs no network I/O.  It returns precise problems so callers can fail
    before publishing a transcribed value.
    """
    root = pathlib.Path(bundle_root or SOURCE_BUNDLE_ROOT)
    manifest_file = pathlib.Path(manifest_path or (root / "manifest.json"))
    if not manifest_file.is_file() or manifest_file.is_symlink():
        return [f"source manifest is missing or unsafe: {manifest_file}"]
    try:
        manifest_raw = manifest_file.read_bytes()
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"source manifest cannot be read: {exc}"]

    problems: list[str] = []
    if manifest.get("manifest_version") != 1:
        problems.append("source manifest_version is not 1")
    if manifest.get("binding_revision") != SOURCE_BINDING_REVISION:
        problems.append(
            f"source binding revision mismatch: {manifest.get('binding_revision')!r}")
    if manifest.get("bundle_version") != SOURCE_BUNDLE_VERSION:
        problems.append(
            f"source bundle version mismatch: {manifest.get('bundle_version')!r}")
    if manifest.get("runtime_network_required") is not False:
        problems.append("source manifest does not declare offline runtime use")

    history = manifest.get("binding_history") or []
    if not isinstance(history, list):
        problems.append("source binding_history is not a list")
        history = []
    history_hashes = []
    for prior in history:
        if not isinstance(prior, dict):
            problems.append("source binding_history contains a non-object entry")
            continue
        receipt_path, unsafe = _safe_bundle_file(root, prior.get("receipt"))
        if unsafe:
            problems.append("source binding receipt " + unsafe)
            continue
        assert receipt_path is not None
        if not receipt_path.is_file() or receipt_path.is_symlink():
            problems.append(f"source binding receipt is missing: {prior.get('receipt')}")
            continue
        receipt_hash = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        history_hashes.append(receipt_hash)
        if receipt_hash != prior.get("manifest_sha256"):
            problems.append(
                f"source binding receipt hash mismatch: {prior.get('receipt')}")
    if history_hashes and manifest.get("previous_manifest_sha256") != history_hashes[-1]:
        problems.append("previous_manifest_sha256 does not match latest binding receipt")
    try:
        captured = dt.datetime.fromisoformat(str(manifest.get("captured_at", "")))
        if captured.tzinfo is None:
            raise ValueError("timezone is absent")
    except ValueError:
        problems.append("source manifest captured_at is not timezone-aware ISO-8601")

    by_expected = {row["fr_document"]: row for row in INDEX_TABLE}
    if filing_ids is None:
        selected = set(by_expected)
    else:
        selected = {str(value) for value in filing_ids if str(value or "")}
        unknown = sorted(selected - set(by_expected))
        if unknown:
            problems.append("unknown selected source document(s): "
                            + ", ".join(unknown))
    entries = manifest.get("documents")
    if not isinstance(entries, list):
        return problems + ["source manifest documents is not a list"]
    by_document: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append("source manifest contains a non-object document entry")
            continue
        document = str(entry.get("fr_document") or "")
        if not document:
            problems.append("source manifest contains an entry without fr_document")
        elif document in by_document:
            problems.append(f"duplicate source manifest document: {document}")
        else:
            by_document[document] = entry
    if filing_ids is None:
        extra = sorted(set(by_document) - set(by_expected))
        if extra:
            problems.append("unexpected source manifest document(s): "
                            + ", ".join(extra))
    missing = sorted(selected - set(by_document))
    if missing:
        problems.append("missing source manifest document(s): " + ", ".join(missing))

    attempts = manifest.get("supplemental_attempts")
    if not isinstance(attempts, list) or len(attempts) != 1:
        problems.append("source manifest must contain the one bounded FERC summary probe")
    else:
        attempt = attempts[0]
        expected_probe = RETRIEVAL_ATTEMPTS[0]
        if attempt.get("request_url") != expected_probe["target"]:
            problems.append("FERC summary probe request URL mismatch")
        if attempt.get("final_url") != expected_probe["target"]:
            problems.append("FERC summary probe final URL mismatch")
        if attempt.get("attempted") is not True or attempt.get("http_status") != 403:
            problems.append("FERC summary probe does not record the captured HTTP 403")
        if attempt.get("classification") != CLASS_ACCESS_BLOCKED:
            problems.append("FERC summary probe classification mismatch")
        if (attempt.get("path") != "supplemental/ferc_summary_http_403.html"
                or not str(expected_probe.get("evidence_path") or "").endswith(
                    "/" + str(attempt.get("path") or ""))):
            problems.append("FERC summary probe evidence identity mismatch")
        probe_path, unsafe = _safe_bundle_file(root, attempt.get("path"))
        if unsafe:
            problems.append("FERC summary probe " + unsafe)
        elif probe_path is not None and not probe_path.is_file():
            problems.append("FERC summary probe response body is missing")
        elif probe_path is not None:
            probe = probe_path.read_bytes()
            if attempt.get("bytes") != len(probe):
                problems.append("FERC summary probe byte count mismatch")
            if attempt.get("sha256") != hashlib.sha256(probe).hexdigest():
                problems.append("FERC summary probe SHA-256 mismatch")
        if not _official_https_url(str(attempt.get("request_url") or "")):
            problems.append("FERC summary probe request URL is not official HTTPS")
        try:
            requested = dt.datetime.fromisoformat(
                str(attempt.get("requested_at", "")))
            if requested.tzinfo is None:
                raise ValueError("timezone is absent")
        except ValueError:
            problems.append("FERC summary probe requested_at is invalid")

    used_paths: set[str] = set()
    for document in sorted(selected & set(by_document) & set(by_expected)):
        entry = by_document[document]
        expected = by_expected[document]
        prefix = f"{document}: "
        if entry.get("publication_date") != expected["published"]:
            problems.append(prefix + "publication date does not match INDEX_TABLE")
        if entry.get("docket") != DOCKET:
            problems.append(prefix + f"docket is not {DOCKET}")
        consumer = entry.get("consumer") or {}
        expected_metrics = [METRIC] + ([METRIC_CHANGE]
                                       if expected["index_change"] else [])
        if consumer.get("adapter") != ADAPTER:
            problems.append(prefix + "consumer adapter is not oil_index")
        if consumer.get("source_table_version") != SOURCE_TABLE_VERSION:
            problems.append(prefix + "consumer source-table version mismatch")
        if consumer.get("metrics") != expected_metrics:
            problems.append(prefix + "consumer metric list does not match published values")
        asserted = entry.get("asserted_values") or {}
        for field in ("factor", "index_change", "interval_start",
                      "interval_end", "effective_from"):
            if asserted.get(field) != expected[field]:
                problems.append(prefix + f"asserted {field} does not match INDEX_TABLE")

        bodies: dict[str, bytes] = {}
        artifact_entries: dict[str, dict] = {}
        for kind in ("metadata", "official_pdf", "extraction_text"):
            artifact = entry.get(kind)
            if not isinstance(artifact, dict):
                problems.append(prefix + f"{kind} provenance is absent")
                continue
            artifact_entries[kind] = artifact
            relative = str(artifact.get("path") or "")
            if relative in used_paths:
                problems.append(prefix + f"artifact path is reused: {relative!r}")
            used_paths.add(relative)
            path, unsafe = _safe_bundle_file(root, relative)
            if unsafe:
                problems.append(prefix + unsafe)
                continue
            assert path is not None
            if not path.is_file():
                problems.append(prefix + f"artifact is missing: {relative}")
                continue
            try:
                raw = path.read_bytes()
            except OSError as exc:
                problems.append(prefix + f"artifact cannot be read: {relative}: {exc}")
                continue
            bodies[kind] = raw
            if artifact.get("http_status") != 200:
                problems.append(prefix + f"{kind} does not record HTTP 200")
            if artifact.get("bytes") != len(raw):
                problems.append(prefix + f"{kind} byte count mismatch")
            if artifact.get("sha256") != hashlib.sha256(raw).hexdigest():
                problems.append(prefix + f"{kind} SHA-256 mismatch")
            for url_field in ("request_url", "final_url"):
                if not _official_https_url(str(artifact.get(url_field) or "")):
                    problems.append(prefix + f"{kind} has invalid {url_field}")
            try:
                requested = dt.datetime.fromisoformat(
                    str(artifact.get("requested_at", "")))
                if requested.tzinfo is None:
                    raise ValueError("timezone is absent")
            except ValueError:
                problems.append(prefix + f"{kind} requested_at is invalid")

        pdf = bodies.get("official_pdf", b"")
        if pdf and not pdf.startswith(b"%PDF-"):
            problems.append(prefix + "official govinfo body is not a PDF")
        metadata = None
        if "metadata" in bodies:
            try:
                metadata = json.loads(bodies["metadata"].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                problems.append(prefix + f"metadata body is invalid JSON: {exc}")
        if isinstance(metadata, dict):
            if metadata.get("document_number") != document:
                problems.append(prefix + "metadata body has wrong document number")
            if metadata.get("publication_date") != expected["published"]:
                problems.append(prefix + "metadata body has wrong publication date")
            agencies = {row.get("name") for row in metadata.get("agencies", [])
                        if isinstance(row, dict)}
            if "Federal Energy Regulatory Commission" not in agencies:
                problems.append(prefix + "metadata body does not identify FERC")
            if not any(DOCKET in str(value)
                       for value in metadata.get("docket_ids", [])):
                problems.append(prefix + "metadata body has wrong docket")
            if ("official_pdf" in artifact_entries
                    and metadata.get("pdf_url")
                    != artifact_entries["official_pdf"].get("request_url")):
                problems.append(prefix + "PDF request URL does not match metadata")
            if ("extraction_text" in artifact_entries
                    and metadata.get("raw_text_url")
                    != artifact_entries["extraction_text"].get("request_url")):
                problems.append(prefix + "text request URL does not match metadata")
        if "extraction_text" in bodies:
            try:
                text = bodies["extraction_text"].decode("utf-8")
            except UnicodeDecodeError as exc:
                problems.append(prefix + f"extraction text is not UTF-8: {exc}")
            else:
                if DOCKET not in text:
                    problems.append(prefix + "extraction text omits docket")
                if document.lower() not in text.lower():
                    problems.append(prefix + "extraction text omits document number")
                if expected["factor"] not in text:
                    problems.append(prefix + "extraction text omits factor")
                if not _text_has_decimal(text, expected["index_change"]):
                    problems.append(prefix + "extraction text omits published change")
                if not _text_supports_interval(text, expected):
                    problems.append(prefix + "no operative passage supports target interval")
    return problems


def require_source_bundle(records) -> None:
    """Fail closed before any selected transcribed occurrence is persisted."""
    documents = [record["fr_document"] for record in records]
    if not documents:
        return
    problems = verify_source_bundle(filing_ids=documents)
    if problems:
        raise RuntimeError("oil-index official source bundle is invalid: "
                           + "; ".join(problems))


# --------------------------------------------------------------------------
# Source record. Every observation must resolve to one of these.
# --------------------------------------------------------------------------

def filing_record(rec: dict) -> dict:
    """A real filing row for one published notice, so no observation dangles.

    The A18 defect was 28 observations pointing at a filing_id with no `filings`
    row. Identity here is (source_system, filing_id) with filing_id the FR
    document number -- a real, citable, unique published document.
    """
    # Column set is pinned to `filings` in ferclib/schema.sql. `docket`,
    # `period_label` and `notes` are NOT columns of that table -- emitting them
    # fails the insert outright -- and `is_canonical` is NOT NULL, so it must be
    # supplied. The docket and the citation travel in `canonical_reason`, which
    # is a real column, rather than being invented as new ones.
    superseded = rec["version_status"] == VersionStatus.SUPERSEDED
    return {
        "source_system": SOURCE_SYSTEM,
        "filing_id": rec["fr_document"],
        "entity_key": INDUSTRY_KEY,             # industry-wide: never a carrier
        "form": "Notice of Annual Change in the Producer Price Index for "
                "Finished Goods",
        "accession_number": rec["fr_document"],
        "reporting_year": int(rec["interval_start"][:4]),
        "reporting_period": "index_year",
        "period_start": rec["interval_start"],
        "period_end": rec["interval_end"],
        "filed_date": rec["published"],
        "issued_date": rec["published"],
        "effective_date": rec["effective_from"],
        # A superseded notice is a real published occurrence and is retained;
        # it simply is not the operative one for its index year.
        "is_canonical": 0 if superseded else 1,
        "canonical_reason": (
            f"Docket No. {rec['docket']}; {rec['citation']}; captured source "
            f"bundle {SOURCE_BUNDLE_VERSION}"
            + ("; SUPERSEDED -- retained because it governed ceilings while in "
               f"force (superseded by FR {rec['superseded_by']})" if superseded
               else "")),
        "version_status": rec["version_status"],
        "supersedes_filing_id": rec["supersedes"] or None,
        "data_origin": Origin.ELIBRARY_DOCUMENT,
        "source_url": (f"https://www.govinfo.gov/content/pkg/"
                       f"FR-{rec['published']}/pdf/{rec['fr_document']}.pdf"),
    }


def source_records() -> list[dict]:
    """One filing row per distinct published notice."""
    seen, out = set(), []
    for r in INDEX_TABLE:
        if r["fr_document"] in seen:
            continue
        seen.add(r["fr_document"])
        out.append(filing_record(r))
    return out


def fact_id(rec: dict, metric: str) -> str:
    """The as-filed fact key. One per (published notice, quantity)."""
    return f"{rec['fr_document']}:{metric}"


def source_facts(rec: dict) -> list[dict]:
    """The as-filed values of one published notice.

    These exist for a specific reason. A lineage edge pointing at a superseded
    state must resolve to a real row: the integrated validator correctly refused
    an edge whose `input_source_fact_id` was a synthesised string with no
    `source_facts` row behind it. Transcribing the published value here makes
    every index observation -- canonical or superseded -- traceable to an
    as-filed fact, and makes the superseded factor RETRIEVABLE rather than
    readable only as text inside an edge.

    `concept_local` names the quantity rather than an XBRL element, because
    these are transcribed publications and not XBRL instances. Saying so plainly
    is better than borrowing a taxonomy name that does not exist.
    """
    out = []
    for metric, value, concept in (
            (METRIC, rec["factor"], "PublishedIndexCeilingMultiplier"),
            (METRIC_CHANGE, rec["index_change"], "PublishedIndexFigureChange")):
        if not value:
            continue
        out.append({
            "source_system": SOURCE_SYSTEM,
            "filing_id": rec["fr_document"],
            "source_fact_id": fact_id(rec, metric),
            "document_order": 1 if metric == METRIC else 2,
            "concept_local": concept,
            "concept_qname": f"ferc-oil-index:{concept}",
            "unit_id": "multiplier" if metric == METRIC else "fraction",
            "unit_text": "multiplier" if metric == METRIC else "fraction",
            "value_as_filed": value,
            "is_nil": 0,
            "period_class": "duration",
            "period_start": rec["interval_start"],
            "period_end": rec["interval_end"],
            # 'prior' means "not the currently-standing state". A factor that was
            # superseded and then REINSTATED is current again, so `superseded_by`
            # alone does not demote it -- only an unreversed supersession or a
            # vacatur does.
            "current_or_prior": (
                "prior" if (rec["vacated_by"]
                            or (rec["superseded_by"] and not rec["reinstated_by"]))
                else "current"),
            "taxonomy_version": (
                f"FR {rec['fr_document']} ({rec['published']}); "
                f"{SOURCE_TABLE_VERSION}; {SOURCE_BUNDLE_VERSION}"),
        })
    return out


def all_source_facts() -> list[dict]:
    """Every published state's as-filed values, across all notices."""
    return [f for r in INDEX_TABLE for f in source_facts(r)]


# --------------------------------------------------------------------------
# Adapter surface
# --------------------------------------------------------------------------

def _require_industry_entity(entity) -> None:
    """Refuse routing an industry instrument through a carrier unit."""
    key = entity.get("entity_key") if isinstance(entity, dict) else None
    if key != INDUSTRY_KEY:
        raise AssertionError(
            f"oil_index was asked to run under {key!r}. The FERC Oil Pipeline "
            f"Index is industry-wide and must run exactly once under "
            f"{INDUSTRY_KEY!r}, never once per carrier.")


def _records_for_filing_ids(filing_ids) -> list[dict]:
    """Resolve a retrieved occurrence set to complete current intervals.

    A narrow run is allowed, but a half-present revision family is not.  The
    canonical factor for an interval can carry lineage to a superseded notice;
    publishing that factor without persisting the superseded source fact would
    create precisely the dangling provenance that A18 required us to remove.
    """
    supplied = {str(value) for value in filing_ids if value not in (None, "")}
    if not supplied:
        return []
    by_document = {r["fr_document"]: r for r in INDEX_TABLE}
    unknown = sorted(supplied - set(by_document))
    if unknown:
        raise ValueError(
            "oil-index retrieval returned unknown Federal Register document(s): "
            + ", ".join(unknown))

    starts = {by_document[document]["interval_start"] for document in supplied}
    selected = []
    for start in sorted(starts):
        required = {r["fr_document"] for r in records_for_interval(start)}
        missing = sorted(required - supplied)
        if missing:
            raise RuntimeError(
                f"oil-index interval {start} has an incomplete source-version "
                f"family; missing Federal Register document(s): {', '.join(missing)}")
        current = current_record(start)
        if current is None or current["fr_document"] not in supplied:
            raise RuntimeError(
                f"oil-index interval {start} has no persisted currently-standing notice")
        selected.append(current)
    return selected


def _current_records(filing_ids=None) -> list[dict]:
    if filing_ids is not None:
        return _records_for_filing_ids(filing_ids)
    records = []
    for start in sorted({r["interval_start"] for r in INDEX_TABLE}):
        current = current_record(start)
        if current is None:
            raise RuntimeError(f"oil-index interval {start} has no current record")
        records.append(current)
    return records


def expected_slots(run_id: str, built_at: str, *, filing_ids=None) -> list[dict]:
    """Return the frozen industry-instrument denominator, without database I/O.

    With no ``filing_ids`` this is the complete declared history: one factor
    and one explicitly published change slot for every currently-standing
    interval. Passing
    the exact IDs returned by :func:`retrieve` produces the bounded subset and
    refuses an incomplete source-version family.

    These slots are occurrence-driven, not a fabricated annual filing calendar.
    Consequently ``due_date`` is deliberately null: 18 CFR 342.3 supplies the
    authority for the instrument but does not make these rows a carrier filing
    deadline.  Every row is nevertheless due/open because its cited notice is
    already published and pinned in ``INDEX_TABLE``.
    """
    if not str(run_id or "").strip():
        raise ValueError("run_id is required for oil-index expected slots")
    if not str(built_at or "").strip():
        raise ValueError("built_at is required for oil-index expected slots")
    problems = verify_table()
    if problems:
        raise RuntimeError("oil-index source table is inconsistent: " + "; ".join(problems))

    basis = getattr(periods, "INTERVAL", "interval")
    rows = []
    for record in _current_records(filing_ids):
        quantities = (
            (METRIC, record["factor"], "ceiling multiplier"),
            (METRIC_CHANGE, record["index_change"], "published index figure"),
        )
        for metric_id, value, quantity in quantities:
            if not value:
                # Defensive only: verify_table() rejects this before the loop.
                # No missing change is silently converted into a derived value.
                continue
            metric = BY_ID.get(metric_id)
            if metric is None or metric.adapter != ADAPTER:
                raise RuntimeError(f"oil-index metric contract is absent: {metric_id}")
            if metric.scope != SCOPE:
                raise RuntimeError(
                    f"oil-index metric scope drift for {metric_id}: {metric.scope!r}")
            evidence = (
                f"{record['citation']}; currently-standing source occurrence "
                f"{record['fr_document']} explicitly publishes the {quantity} "
                f"as {value}; source fact {fact_id(record, metric_id)}; captured "
                f"evidence {SOURCE_BUNDLE_VERSION}")
            rows.append({
                "slot_id": slot_id(
                    INDUSTRY_KEY, metric_id, REGIME, basis,
                    record["interval_start"], record["interval_end"], "", SCOPE),
                "entity_key": INDUSTRY_KEY,
                "asset_id": "",
                "template": "liquids",
                "metric_id": metric_id,
                "source_regime": REGIME,
                "period_basis": basis,
                "period_start": record["interval_start"],
                "period_end": record["interval_end"],
                "instant_date": None,
                "reporting_year": int(record["interval_start"][:4]),
                "reporting_period": "index_year",
                "scope": SCOPE,
                "unit_rule": metric.unit_rule,
                "requirement": coverage.CONDITIONAL,
                "requirement_evidence": evidence,
                "applicability_version": (
                    f"{REGISTRY_VERSION}; 18 CFR 342.3; current-source="
                    f"FR-{record['fr_document']}@{record['published']}"),
                "frozen_at": built_at,
                "frozen_run_id": run_id,
                "due_date": None,
                "slot_state": coverage.SLOT_OPEN,
                "obligation_form": (
                    "Notice of Annual Change in the Producer Price Index for "
                    "Finished Goods"),
                "obligation_authority": f"18 CFR 342.3; Docket No. {DOCKET}",
                "obligation_evidence_kind": EvidenceKind.FILED_OCCURRENCE,
                "source_health": SourceHealth.OK,
                "source_health_detail": (
                    f"official Federal Register occurrence FR {record['fr_document']} "
                    f"published {record['published']} is the currently-standing "
                    f"source version for {record['interval_start']}.."
                    f"{record['interval_end']}"),
                "denominator_origin": "regulatory_instrument_history",
                "docket": DOCKET,
                "facility": "",
                "shipped_requirement": None,
            })

    rows.sort(key=lambda row: (
        row["period_start"], row["metric_id"], row["slot_id"]))
    ids = [row["slot_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise RuntimeError("oil-index expected-slot identity collision")
    return rows

def retrieve(ctx, entity, *, year_from: int, year_to: int) -> list[dict]:
    """Persist the published notices that cover the requested window.

    This source is a fixed set of published Commission notices, transcribed with
    their citations. Nothing is fetched at run time, so nothing here can fail
    with a network error -- and nothing here may ever be reported as one.
    """
    _require_industry_entity(entity)
    # An index year belongs to the year in which its 1 July interval starts.
    # Including rows merely because their June end fell in the window made a
    # 2024 run emit a 2023 observation, which the bounded swap quite correctly
    # would not prune on the next 2024 refresh.
    recs = [r for r in INDEX_TABLE
            if year_from <= int(r["interval_start"][:4]) <= year_to]
    # The adapter is deliberately offline at runtime, but a static table is not
    # evidence by itself.  Verify every selected publication's captured bytes
    # and semantic binding before writing even the first filing/fact row.
    require_source_bundle(recs)
    filings, seen = [], set()
    for r in recs:
        if r["fr_document"] in seen:
            continue
        seen.add(r["fr_document"])
        f = filing_record(r)
        # The as-filed values travel WITH the filing, so a lineage edge naming a
        # superseded state always has a row to resolve to.
        ctx.staging.write_filing_bundle(f, facts=source_facts(r))
        filings.append(f)
    return filings


def freeze_expected(ctx, entity, filings, assets) -> list[dict]:
    """Freeze the bounded retrieved subset via the pure slot builder."""
    _require_industry_entity(entity)
    args = getattr(ctx, "args", None)
    built_at = getattr(args, "built_at", "") if args is not None else ""
    if not built_at:
        built_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    run_id = getattr(getattr(ctx, "staging", None), "run_id", "")
    return expected_slots(
        run_id, built_at,
        filing_ids=[filing.get("filing_id") for filing in filings])


def observations_for(entity_key: str = INDUSTRY_KEY, *,
                     run_id: str = "", filing_ids=None) -> tuple[list, list]:
    """Build the current observations for complete selected intervals.

    With no ``filing_ids`` the full pinned history is used.  A bounded adapter
    run passes the occurrence IDs returned by ``retrieve``; an incomplete
    revision family is refused before any observation can carry dangling
    lineage.  Superseded values remain source facts/filing occurrences and are
    linked from the one currently-standing observation for their interval.
    """
    obs, edges = [], []
    # ONE observation per (index year, metric). `ux_observation_grain` enforces a
    # single row per grain, and two published states of one index year share
    # every grain column -- so emitting both would collide, and forcing them
    # apart by inventing a grain difference would be a lie about the data.
    #
    # A08's requirement is "preserve versioned provenance even when maintaining a
    # single current canonical view", and that is exactly what happens here: the
    # canonical row carries the currently-standing factor, while every superseded
    # or vacated state survives as (a) its own `filings` occurrence with
    # is_canonical=0, and (b) a lineage edge from this observation to that
    # occurrence carrying the superseded factor as its input value. The history
    # is therefore traversable from the observation, not merely narrated.
    for r in _current_records(filing_ids):
        start = r["interval_start"]
        prior = superseded_states(start)
        for metric, value, unit, kind in (
                (METRIC, r["factor"], "multiplier", "ceiling multiplier"),
                (METRIC_CHANGE, r["index_change"], "fraction", "index figure")):
            if not value:
                continue
            qa = [
                "FERC Oil Pipeline Index: an industry-wide rate-CEILING "
                "adjustment under 18 CFR 342.3. It is NOT a commodity oil "
                "price and NOT any carrier's actual tariff change.",
                f"quantity: {kind}. The multiplier is DIMENSIONLESS; the index "
                "figure is a fraction. factor = 1 + change; they are never "
                "interchangeable and neither is a percent as stored.",
                f"five-year index level in force: {r['five_year_level'] or 'not stated'}",
                f"source: {r['citation']}",
                f"captured source evidence: {SOURCE_BUNDLE_VERSION}",
            ]
            if r["note"]:
                qa.append(r["note"])
            if r["source_anomaly"]:
                qa.append("SOURCE ANOMALY (recorded, not corrected): "
                          + r["source_anomaly"])
            if r["reinstated_by"]:
                qa.append(f"REINSTATED by {r['reinstated_by']} after having been "
                          f"superseded by FR {r['superseded_by']}. This is the "
                          "factor that currently stands for this index year.")
            for p in prior:
                qa.append(
                    f"SUPERSEDED STATE RETAINED: FR {p['fr_document']} "
                    f"(published {p['published']}) put the factor for this index "
                    f"year at {p['factor']} effective {p['effective_from']}"
                    + (f", later vacated by {p['vacated_by']}" if p["vacated_by"]
                       else "")
                    + ". It governed filed rates while in force and is preserved "
                      "as its own filing occurrence plus a lineage edge; it is "
                      "not the currently-standing factor.")
            # The effective date is NOT a column of `observations`; emitting it
            # fails the insert. It belongs to the filing occurrence (where it is
            # a real column) and is restated in qa_flags for the reader.
            qa.append(f"effective from {r['effective_from']}"
                      + (" -- inside the index year it restates"
                         if r["effective_from"] > r["interval_start"] else ""))
            # Deterministic id over the full grain (adapter contract rule 12), so
            # a re-run regenerates the same row rather than duplicating it.
            oid = observation_id(entity_key, metric, REGIME, "interval",
                                 r["interval_start"], r["interval_end"], "",
                                 SCOPE, unit, Method.MANUALLY_CURATED)
            obs.append({
                "observation_id": oid,
                "entity_key": entity_key,
                "metric_id": metric,
                "source_regime": REGIME,
                "period_basis": "interval",
                "period_start": r["interval_start"],
                "period_end": r["interval_end"],
                "instant_date": "",
                "period_label": f"{r['interval_start']}..{r['interval_end']}",
                "reporting_year": int(r["interval_start"][:4]),
                "reporting_period": "index_year",
                "scope": SCOPE,
                "unit": unit,
                "value_text": value,
                "value_num": float(value),
                "availability": Availability.PRESENT,
                "origin": Origin.ELIBRARY_DOCUMENT,
                "method": Method.MANUALLY_CURATED,
                "version_status": r["version_status"],
                "validation": (Validation.SOURCE_ANOMALY_REVIEW
                               if r["source_anomaly"] else Validation.PASS),
                "review_status": ("open" if r["source_anomaly"] else ""),
                "source_system": SOURCE_SYSTEM,
                "filing_id": r["fr_document"],
                "source_fact_id": fact_id(r, metric),
                "accession_number": r["fr_document"],
                "candidate_count": 1,
                "selector": "published_index_notice",
                "derivation": "",
                "registry_version": REGISTRY_VERSION,
                "applicability_evidence": r["citation"],
                "qa_flags": " || ".join(qa),
            })
            # Provenance made traversable: one edge per superseded state, naming
            # the filing occurrence and the factor it published.
            for i, p in enumerate(prior, 1):
                edges.append({
                    "observation_id": oid, "input_order": i,
                    "input_role": "basis", "operator_sign": "",
                    "coefficient": 1.0,
                    "input_source_system": SOURCE_SYSTEM,
                    "input_filing_id": p["fr_document"],
                    # Resolves to a real `source_facts` row written by retrieve().
                    "input_source_fact_id": fact_id(p, metric),
                    "input_observation_id": None, "input_context_id": None,
                    "input_concept": "superseded_index_factor",
                    "input_period": f"{p['interval_start']}..{p['interval_end']}",
                    "input_value": (p["factor"] if metric == METRIC
                                    else p["index_change"]),
                    "input_unit": unit,
                    "input_version_status": p["version_status"],
                    "input_population_id": None,
                })
    return obs, edges


def canonicalise(ctx, entity, filings, expected) -> tuple[list, list]:
    """The index is published once, against the industry key -- never per carrier.

    A carrier entity reaching this adapter is a routing mistake, not a licence to
    attribute a national instrument to that carrier, so it is refused loudly.
    """
    _require_industry_entity(entity)
    # Preserve the long-standing pure probe used by the A18 routing test: with
    # no runner context at all it asks for the complete in-memory series.  A
    # real adapter run always supplies a context, and an empty retrieval there
    # produces no rows (never uncited all-history observations).
    filing_ids = (None if ctx is None and not filings else
                  [filing.get("filing_id") for filing in filings])
    return observations_for(
        INDUSTRY_KEY,
        run_id=getattr(getattr(ctx, "staging", None), "run_id", ""),
        filing_ids=filing_ids)


# --------------------------------------------------------------------------
# Truthful failure classification
# --------------------------------------------------------------------------

def classify_failure(exc_or_text, *, request_attempted: bool,
                     offline: bool = False) -> dict:
    """Say what actually went wrong. A local misconfiguration is never an outage.

    The A16 defect was two 549D blockers whose exact_error read
    "FERC_API_KEY is not set in the environment or .env" while being filed as
    `kind="source"` -- i.e. a missing local credential presented as a FERC
    source failure. The distinctions enforced here:

      * a credential this machine does not have          -> local configuration
      * a request that was never issued                  -> not attempted
      * offline replay with nothing in the cache         -> cache miss
      * the publisher refusing THIS host                 -> access blocked
      * FERC returning an error to a real request        -> source unavailable

    Only the last is a FERC condition, and only it may be described as one.
    """
    text = str(getattr(exc_or_text, "detail", "") or exc_or_text)
    low = text.lower()

    if "not set in the environment" in low or "api_key" in low and "not set" in low:
        return {
            "classification": CLASS_MISSING_CREDENTIAL,
            "is_ferc_condition": False,
            "kind": "configuration",
            "summary": "a required credential is not configured on this machine",
            "detail": "The adapter needs an API key that this environment does "
                      "not provide. No request reached FERC, so nothing is "
                      "known about FERC's availability.",
            "explicitly_not": "NOT a FERC outage, NOT a FERC data gap, and NOT "
                              "evidence that the data is unavailable.",
            "remedy": "Run in credential-free cached replay, or supply the "
                      "credential for live retrieval. These are different "
                      "modes and must be reported separately.",
            "human_decision_needed": False,
        }
    if offline and ("cache" in low and "miss" in low or "offlinecachemiss" in low):
        return {
            "classification": CLASS_CACHE_MISS,
            "is_ferc_condition": False,
            "kind": "configuration",
            "summary": "offline replay found no cached response for this request",
            "detail": "Replay is credential-free by design; the response simply "
                      "is not in the local cache.",
            "explicitly_not": "NOT a FERC outage. FERC was never contacted.",
            "remedy": "Populate the cache with one credentialled live run, or "
                      "narrow the window to what the cache holds.",
            "human_decision_needed": False,
        }
    if not request_attempted:
        return {
            "classification": CLASS_NOT_ATTEMPTED,
            "is_ferc_condition": False,
            "kind": "configuration",
            "summary": "no HTTP request was attempted",
            "detail": f"The failure occurred before any request was issued: {text}",
            "explicitly_not": "NOT a FERC outage. An unattempted request carries "
                              "no information about the source at all.",
            "remedy": "Fix the precondition, then retry.",
            "human_decision_needed": False,
        }
    if "403" in low or "forbidden" in low:
        return {
            "classification": CLASS_ACCESS_BLOCKED,
            "is_ferc_condition": False,
            "kind": "access",
            "summary": "the publisher refused this host",
            "detail": text,
            "explicitly_not": "NOT a FERC outage: the document is published and "
                              "reachable from other hosts.",
            "remedy": "Use the official published text from the authenticated "
                      "publication of record.",
            "human_decision_needed": False,
        }
    return {
        "classification": CLASS_FERC_UNAVAILABLE,
        "is_ferc_condition": True,
        "kind": "source",
        "summary": "FERC returned an error to a request that was actually made",
        "detail": text,
        "explicitly_not": "",
        "remedy": "Retry later; record the exact error.",
        "human_decision_needed": False,
    }


def write_evidence(path: pathlib.Path) -> pathlib.Path:
    """Dump the table, its provenance and the retrieval log for the record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest_bytes = SOURCE_MANIFEST_PATH.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    path.write_text(json.dumps({
        "metric": METRIC,
        "companion_metric": METRIC_CHANGE,
        "legacy_metric": LEGACY_METRIC,
        "crosswalk": metric_crosswalk(),
        "docket": DOCKET,
        "regulation": "18 CFR 342.3",
        "source_table_version": SOURCE_TABLE_VERSION,
        "source_bundle_version": SOURCE_BUNDLE_VERSION,
        "source_manifest": str(SOURCE_MANIFEST_PATH.relative_to(
            pathlib.Path(__file__).resolve().parent.parent)),
        "source_manifest_bytes": len(manifest_bytes),
        "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "captured_document_occurrences": len(manifest.get("documents", [])),
        # Evidence identity must not depend on the workstation's wall clock.
        # The bundle's timezone-aware capture timestamp is the declared event.
        "generated": str(manifest.get("captured_at") or ""),
        "intervals": len({r["interval_start"] for r in INDEX_TABLE}),
        "records": len(INDEX_TABLE),
        "superseded_records": [r for r in INDEX_TABLE
                               if r["version_status"] == VersionStatus.SUPERSEDED],
        "source_anomalies": [r for r in INDEX_TABLE if r["source_anomaly"]],
        "retrieval_attempts": RETRIEVAL_ATTEMPTS,
        "table": INDEX_TABLE,
        "consistency_problems": verify_table(),
        "source_bundle_problems": verify_source_bundle(),
    }, indent=2, sort_keys=True), encoding="utf-8")
    return path
