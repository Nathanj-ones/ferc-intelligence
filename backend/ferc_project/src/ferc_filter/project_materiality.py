from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


TRACK = "TRACK"
WATCH = "WATCH"
DROP = "DROP"


@dataclass
class MaterialityDecision:
    docket: str
    decision: str
    score: int
    reasons: list[str]
    confidence: str
    capex: float | None = None
    capacity: float | None = None
    target_in_service: str | None = None


def _text(candidate: Any) -> str:
    return " ".join(
        [
            " ".join(getattr(candidate, "project_names", []) or []),
            " ".join(getattr(candidate, "sample_filings", []) or []),
        ]
    ).lower()


def assess_materiality(
    candidate: Any,
    qualification: Any,
    *,
    capex: float | None = None,
    capacity: float | None = None,
    target_in_service: str | None = None,
) -> MaterialityDecision:
    """
    Investor-materiality gate for a previously unknown FERC project.

    Missing capex/capacity is retained as missing. No values are inferred.
    Project/company names and docket IDs are never hardcoded.
    """

    docket = getattr(candidate, "docket", "")
    text = _text(candidate)
    score = 0
    reasons: list[str] = []

    # Only qualified active candidates can become tracked.
    q_decision = getattr(qualification, "decision", "")
    if q_decision == "IGNORE":
        return MaterialityDecision(
            docket=docket,
            decision=DROP,
            score=0,
            reasons=["qualification-ignore"],
            confidence="high",
            capex=capex,
            capacity=capacity,
            target_in_service=target_in_service,
        )

    # Explicitly disclosed economic scale is strongest evidence.
    if capex is not None:
        score += 4
        reasons.append("disclosed-capex")

    if capacity is not None:
        score += 3
        reasons.append("disclosed-capacity")

    # Generic growth-project evidence.
    growth_terms = (
        "expansion project",
        "new pipeline",
        "greenfield",
        "liquefaction",
        "compression expansion",
        "new interconnection",
        "additional capacity",
        "increase capacity",
        "construct",
    )
    growth_hits = [term for term in growth_terms if term in text]
    if growth_hits:
        score += min(3, len(growth_hits))
        reasons.extend(f"growth:{term}" for term in growth_hits[:3])

    # Regulatory progression matters, but is not by itself materiality.
    progression_terms = (
        "application for a certificate",
        "prior notice request",
        "notice to proceed",
        "commence construction",
        "construction commenced",
        "ferc's",
        "data request",
    )
    if any(term in text for term in progression_terms):
        score += 2
        reasons.append("active-regulatory-progression")

    if target_in_service:
        score += 1
        reasons.append("disclosed-in-service-timing")

    # These are normally not investor growth projects.
    non_growth_terms = (
        "abandonment project",
        "abandonment authorization",
        "facilities replacement",
        "pipeline replacement",
        "reclassification project",
        "reacquire capacity",
        "lease agreement project",
    )
    non_growth_hits = [term for term in non_growth_terms if term in text]
    if non_growth_hits:
        score -= 5
        reasons.extend(
            f"non-growth:{term}" for term in non_growth_hits[:3]
        )

    # Qualification PROMOTE is useful evidence, but not enough on its own.
    if q_decision == "PROMOTE":
        score += 1
        reasons.append("active-project-qualified")

    if score >= 6 and not non_growth_hits:
        decision = TRACK
        confidence = "high" if (capex is not None or capacity is not None) else "medium"
    elif score >= 2:
        decision = WATCH
        confidence = "medium"
    else:
        decision = DROP
        confidence = "medium"

    return MaterialityDecision(
        docket=docket,
        decision=decision,
        score=score,
        reasons=reasons,
        confidence=confidence,
        capex=capex,
        capacity=capacity,
        target_in_service=target_in_service,
    )


def materiality_to_dict(value: MaterialityDecision) -> dict:
    return asdict(value)
