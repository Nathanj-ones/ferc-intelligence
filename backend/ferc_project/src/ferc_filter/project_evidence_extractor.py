from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any


MONEY_RE = re.compile(
    r"(?:approximately|about|estimated(?:\s+at)?|cost(?:\s+of)?|"
    r"capital\s+cost(?:\s+of)?|project\s+cost(?:\s+of)?)?"
    r"\s*\$?\s*([0-9]+(?:\.[0-9]+)?)\s*"
    r"(billion|million)\b",
    re.I,
)

CAPACITY_RE = re.compile(
    r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*"
    r"(Bcf/d|MMcf/d|Mcf/d|Dth/d|dekatherms?\s+per\s+day|"
    r"million\s+dekatherms?\s+per\s+day)\b",
    re.I,
)

PROJECT_NAME_PATTERNS = (
    re.compile(
        r"(?:re(?:garding)?|for)\s+(?:the\s+)?"
        r"([A-Z][A-Za-z0-9&'./() -]{2,100}?\s+Project)\b"
    ),
    re.compile(
        r"\b([A-Z][A-Za-z0-9&'./() -]{2,100}?\s+"
        r"(?:Expansion|Connector|Pipeline|Liquefaction|Compression)\s+Project)\b"
    ),
)

IN_SERVICE_PATTERNS = (
    re.compile(
        r"(?:in[- ]service|placed?\s+in\s+service|available\s+for\s+service)"
        r".{0,60}?"
        r"((?:Q[1-4]\s+)?20\d{2}|"
        r"(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+\d{1,2},?\s+20\d{2})",
        re.I,
    ),
    re.compile(
        r"((?:Q[1-4]\s+)?20\d{2}).{0,50}?"
        r"(?:in[- ]service|placed?\s+in\s+service|available\s+for\s+service)",
        re.I,
    ),
)


@dataclass
class ProjectEvidence:
    docket: str
    project_name: str | None
    applicant: str | None
    capex: float | None
    capex_unit: str | None
    capacity: float | None
    capacity_unit: str | None
    target_in_service: str | None
    source_accessions: list[str]
    confidence: str


def _description(filing: Any) -> str:
    if isinstance(filing, dict):
        return str(filing.get("description") or "")
    return str(getattr(filing, "description", "") or "")


def _field(filing: Any, name: str, default=""):
    if isinstance(filing, dict):
        return filing.get(name, default)
    return getattr(filing, name, default)


def _project_name(text: str) -> str | None:
    for pattern in PROJECT_NAME_PATTERNS:
        match = pattern.search(text)
        if match:
            value = " ".join(match.group(1).split())
            # Avoid swallowing filing boilerplate.
            if len(value) <= 120:
                return value
    return None


def _capex(text: str) -> tuple[float | None, str | None]:
    # Require cost/capital context near the money value. This avoids treating
    # unrelated monetary amounts as project capex.
    lower = text.lower()
    for match in MONEY_RE.finditer(text):
        start = max(0, match.start() - 80)
        context = lower[start:match.end() + 40]
        if not any(
            term in context
            for term in (
                "cost",
                "capital",
                "estimated",
                "project",
                "facilities",
            )
        ):
            continue
        value = float(match.group(1))
        unit = match.group(2).lower()
        dollars = value * (1_000_000_000 if unit == "billion" else 1_000_000)
        return dollars, "USD"
    return None, None


def _capacity(text: str) -> tuple[float | None, str | None]:
    match = CAPACITY_RE.search(text)
    if not match:
        return None, None
    value = float(match.group(1).replace(",", ""))
    return value, match.group(2)


def _target_in_service(text: str) -> str | None:
    for pattern in IN_SERVICE_PATTERNS:
        match = pattern.search(text)
        if match:
            return " ".join(match.group(1).split())
    return None


def extract_project_evidence(
    docket: str,
    filings: list[Any],
) -> ProjectEvidence:
    """
    Extract only explicitly disclosed project evidence from FERC filing
    metadata/descriptions. Missing values remain None.
    """

    project_name = None
    applicant = None
    capex = None
    capex_unit = None
    capacity = None
    capacity_unit = None
    target_in_service = None
    accessions: list[str] = []

    for filing in filings:
        text = _description(filing)
        accession = str(
            _field(filing, "accession", "")
            or _field(filing, "accessionNumber", "")
        )
        if accession and accession not in accessions:
            accessions.append(accession)

        if project_name is None:
            project_name = _project_name(text)

        if applicant is None:
            value = str(
                _field(filing, "applicant", "")
                or _field(filing, "applicant_name", "")
            ).strip()
            applicant = value or None

        if capex is None:
            capex, capex_unit = _capex(text)

        if capacity is None:
            capacity, capacity_unit = _capacity(text)

        if target_in_service is None:
            target_in_service = _target_in_service(text)

    evidence_count = sum(
        value is not None
        for value in (
            project_name,
            applicant,
            capex,
            capacity,
            target_in_service,
        )
    )

    confidence = (
        "high" if evidence_count >= 4
        else "medium" if evidence_count >= 2
        else "low"
    )

    return ProjectEvidence(
        docket=docket,
        project_name=project_name,
        applicant=applicant,
        capex=capex,
        capex_unit=capex_unit,
        capacity=capacity,
        capacity_unit=capacity_unit,
        target_in_service=target_in_service,
        source_accessions=accessions,
        confidence=confidence,
    )


def project_evidence_to_dict(value: ProjectEvidence) -> dict[str, Any]:
    return asdict(value)
