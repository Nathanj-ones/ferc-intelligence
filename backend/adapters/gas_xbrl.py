"""
Gas regime configuration: Forms 2, 2-A and 3-Q.

All logic lives in `ferclib.xbrl_adapter`, which gas and liquids share. This
file is configuration only: which forms, which of them are annual, and the
narrative-profile patterns for the disclosures Form 2-A carries in prose because
it has no dedicated physical schedule.
"""

from __future__ import annotations

import re
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ferclib import ecollection, xbrl_adapter                       # noqa: E402

ADAPTER = "gas_xbrl"

#: Narrative profile patterns for the Form 2-A p.211.1 textblock.
#:
#: Each captures a span, a qualifier and a unit from FERC-filed text. These are
#: separate assertions with their own scope: never labelled p.514 totals or
#: certified system horsepower, described sections never added together, and no
#: unit conversion applied without a sourced basis.
GAS_TEXTBLOCK_PATTERNS = [
    ("profile_miles_narrative",
     re.compile(r"(approximately|about|roughly)?\s*([\d,]+(?:\.\d+)?)\s*miles", re.I),
     "miles"),
    ("profile_diameter_narrative",
     re.compile(r"([\d,]+(?:\.\d+)?)[\s-]*inch(?:es)?\s*(?:diameter|pipeline|line)", re.I),
     "inches"),
    ("profile_compressor_narrative",
     re.compile(r"([\d,]+)\s*compressor\s*units?[^.]{0,80}?([\d,]+)\s*(?:total\s*)?horsepower", re.I),
     "units / horsepower"),
    ("profile_capacity_narrative",
     re.compile(r"([\d,]+(?:\.\d+)?)\s*(Bcf|MMcf|MMBtu|Dth)\s*(?:per|/)\s*day", re.I),
     "as stated"),
    ("profile_stations_narrative",
     re.compile(r"([\d,]+)\s+receipt\s+(?:\w+\s+){0,2}?stations?\s+and\s+([\d,]+)\s+"
                r"deliver\w*\s+(?:\w+\s+){0,2}?stations?", re.I),
     "count"),
    ("profile_flow_statement_narrative",
     re.compile(r"((?:no|not)\s+(?:currently\s+)?(?:any\s+)?(?:active|scheduled)\s+flow[^.]{0,120}\.)",
                re.I),
     "(statement)"),
]

CFG = xbrl_adapter.XbrlConfig(
    adapter=ADAPTER,
    forms=ecollection.GAS_FORMS,
    annual_forms={"Form 2", "Form 2A"},
    quarterly_forms={"Form 3Q Gas"},
    textblock_concept="GeneralInformationOnPlantAndOperationsTextblock",
    textblock_regime="XBRL textblock",
    textblock_patterns=GAS_TEXTBLOCK_PATTERNS,
    textblock_page="211.1",
)


def retrieve(ctx, entity, *, year_from, year_to):
    return xbrl_adapter.retrieve(CFG, ctx, entity, year_from=year_from, year_to=year_to)


def freeze_expected(ctx, entity, filings, assets):
    return xbrl_adapter.freeze_expected(CFG, ctx, entity, filings, assets)


def canonicalise(ctx, entity, filings, expected):
    return xbrl_adapter.canonicalise(CFG, ctx, entity, filings, expected)
