from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any


class Decision(str, Enum):
    ALERT = "ALERT"
    REVIEW = "REVIEW"
    SUPPRESS = "SUPPRESS"


@dataclass
class FilingRecord:
    accession: str | None = None
    docket: str | None = None
    filing_date: date | None = None
    category: str | None = None
    classification: str | None = None
    filing_type: str | None = None
    description: str | None = None
    filer: str | None = None
    raw: dict[str, Any] | None = None


@dataclass
class FilterResult:
    decision: Decision
    rationale: str
