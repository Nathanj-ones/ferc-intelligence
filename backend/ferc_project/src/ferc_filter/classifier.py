from __future__ import annotations

from .models import Decision, FilingRecord, FilterResult


def classify_filing(filing: FilingRecord) -> FilterResult:
    """Very early deterministic classifier.

    This is deliberately conservative. It is a research baseline, not the final model.
    """
    text = " ".join(
        part for part in [filing.classification, filing.filing_type, filing.description] if part
    ).lower()

    if any(term in text for term in [
        "certificate order",
        "order issuing certificates",
        "environmental assessment",
        "final environmental impact statement",
        "draft environmental impact statement",
        "prior notice request",
        "extension of time",
    ]):
        return FilterResult(Decision.ALERT, "Known high-signal project milestone/document family.")

    if any(term in text for term in [
        "motion for stay",
        "request for rehearing",
        "data request",
        "information request",
        "variance",
        "alternative measure",
        "field inspection",
        "implementation plan",
    ]):
        return FilterResult(Decision.REVIEW, "Potentially material; content is needed to assess impact.")

    if any(term in text for term in [
        "monthly status report",
        "quarterly construction status report",
        "weekly noise",
        "intervention",
        "motion to intervene",
        "comment on filing",
    ]):
        return FilterResult(Decision.SUPPRESS, "Recurring/procedural filing family; inspect only for exceptions.")

    return FilterResult(Decision.REVIEW, "Unknown filing family; retain until taxonomy is validated.")
