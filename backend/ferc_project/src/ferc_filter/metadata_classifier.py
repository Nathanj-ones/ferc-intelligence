"""First-pass metadata classifier for FERC project filings."""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass
class Classification:
    decision: str
    reason: str
    rule: str


SUPPRESS_PATTERNS = {
    "weekly_noise_report": [
        "weekly noise data report",
    ],
    "routine_monthly_status": [
        "monthly status report",
    ],
    "routine_emergency_plan_update": [
        "quarterly update related to the emergency response plan",
    ],
    "intervention": [
        "motion to intervene",
    ],
}


ALERT_PATTERNS = {
    "extension_of_time": [
        "request for three-year extension of time",
        "request for extension of time",
        "extension of time until",
        "extension of time to complete",
        "extend the deadline",
        "extending the deadline",
    ],
    "major_construction_authorization": [
        "proceed with construction of trains",
        "authorization to construct trains",
        "construction of trains 4 and 5",
    ],
    "in_service": [
        "place in service",
        "placed in service",
        "authorization to commence service",
        "authorization to place",
    ],
    "project_suspension": [
        "suspend construction",
        "suspension of construction",
        "stop work",
    ],
    "project_cancellation": [
        "cancel the project",
        "project cancellation",
        "abandon the project",
    ],
}


REVIEW_PATTERNS = {
    "variance": [
        "variance request",
        "design modification",
    ],
    "engineering_request": [
        "engineering information request",
    ],
    "inspection": [
        "inspection report",
    ],
    "rehearing": [
        "request for rehearing",
        "rehearing",
    ],
    "stay": [
        "motion for stay",
        "request for stay",
    ],
    "construction_authorization": [
        "request for authorization to install",
        "request for authorization to construct",
        "granting the",
    ],
    "implementation_plan": [
        "implementation plan",
    ],
    "court": [
        "court of appeals",
        "court related",
        "petition for review",
    ],
}


def normalize_description(description: str) -> str:
    """Normalize descriptions for comparison/deduplication."""

    text = description.lower()

    # Remove punctuation.
    text = re.sub(r"[^a-z0-9\s]", " ", text)

    # Collapse repeated whitespace.
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def filing_family_key(record: dict) -> tuple[str, str]:
    """
    Build a provisional filing-family key.

    eLibrary sometimes exposes multiple accession records for one
    underlying filing. We use date + normalized description as a
    conservative first-pass grouping mechanism.
    """

    filed_date = str(record.get("filed_date", ""))[:10]
    description = normalize_description(
        record.get("doc_desc", "") or ""
    )

    return filed_date, description


def _match_patterns(
    description: str,
    patterns: dict[str, list[str]],
):
    text = description.lower()

    for rule_name, phrases in patterns.items():
        for phrase in phrases:
            if phrase in text:
                return rule_name, phrase

    return None


def classify_metadata(record: dict) -> Classification:
    description = record.get("doc_desc", "") or ""

    match = _match_patterns(description, ALERT_PATTERNS)

    if match:
        rule, phrase = match

        return Classification(
            decision="ALERT",
            reason=f"Matched material-event phrase: '{phrase}'",
            rule=rule,
        )

    match = _match_patterns(description, SUPPRESS_PATTERNS)

    if match:
        rule, phrase = match

        return Classification(
            decision="SUPPRESS",
            reason=f"Matched routine filing phrase: '{phrase}'",
            rule=rule,
        )

    match = _match_patterns(description, REVIEW_PATTERNS)

    if match:
        rule, phrase = match

        return Classification(
            decision="REVIEW",
            reason=f"Matched ambiguous-event phrase: '{phrase}'",
            rule=rule,
        )

    return Classification(
        decision="REVIEW",
        reason="No deterministic rule matched.",
        rule="unknown",
    )