from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Iterable


@dataclass
class ProjectQualification:
    docket: str
    decision: str
    score: int
    reasons: list[str]
    confidence: str


PROMOTE = "PROMOTE"
WATCH = "WATCH"
IGNORE = "IGNORE"


def _blob(candidate) -> str:
    fields = [
        getattr(candidate, "docket", ""),
        " ".join(getattr(candidate, "project_names", []) or []),
        " ".join(getattr(candidate, "sample_filings", []) or []),
        " ".join(getattr(candidate, "reasons", []) or []),
    ]
    return " ".join(fields).lower()


def qualify_candidate(candidate) -> ProjectQualification:
    text = _blob(candidate)
    docket = getattr(candidate, "docket", "")
    project_names = getattr(candidate, "project_names", []) or []
    filing_count = int(getattr(candidate, "filing_count", 0) or 0)

    active_terms = (
        "abbreviated application",
        "application for a certificate",
        "prior notice request",
        "notice to proceed",
        "commence construction",
        "construction commenced",
        "data request",
        "response to ferc",
        "supplemental information",
        "certificate of public convenience and necessity",
    )

    routine_terms = (
        "semi-annual summary of operations",
        "semi-annual operating report",
        "annual informational off-system capacity report",
        "annual operating report",
        "quarterly operating report",
        "storage report",
        "post-construction noise survey",
        "monthly status report",
    )

    non_growth_terms = (
        "abandonment project",
        "notice of abandonment",
        "2.55(b) notification",
        "facilities replacement",
        "pipeline replacement project",
        "vaporizers replacement project",
    )

    active_hits = [term for term in active_terms if term in text]
    routine_hits = [term for term in routine_terms if term in text]
    non_growth_hits = [term for term in non_growth_terms if term in text]

    if routine_hits and not active_hits:
        return ProjectQualification(
            docket=docket,
            decision=IGNORE,
            score=-2 * max(1, len(routine_hits)),
            reasons=[f"routine-reporting:{term}" for term in routine_hits],
            confidence="high",
        )

    if non_growth_hits and not active_hits:
        return ProjectQualification(
            docket=docket,
            decision=IGNORE,
            score=-2 * max(1, len(non_growth_hits)),
            reasons=[f"non-growth:{term}" for term in non_growth_hits],
            confidence="high",
        )

    score = 0
    reasons: list[str] = []

    for term in active_hits:
        score += 2
        reasons.append(f"active-project-signal:{term}")

    if project_names:
        score += 1
        reasons.append("named-project")

    if filing_count >= 3:
        score += 1
        reasons.append("multiple-filings")

    latest = getattr(candidate, "latest_filing_date", None)
    if latest:
        try:
            latest_dt = datetime.strptime(latest, "%m/%d/%Y")
            age_days = (datetime.now() - latest_dt).days
            if age_days <= 90:
                score += 2
                reasons.append("recent-activity")
            elif age_days <= 365:
                score += 1
                reasons.append("activity-within-year")
            else:
                reasons.append("stale-activity")
        except ValueError:
            pass

    for term in routine_hits:
        score -= 1
        reasons.append(f"routine-reporting:{term}")

    for term in non_growth_hits:
        score -= 3
        reasons.append(f"non-growth:{term}")

    application_signal = any(
        term in text
        for term in (
            "abbreviated application",
            "application for a certificate",
            "prior notice request",
            "notice to proceed",
            "commence construction",
            "construction commenced",
        )
    )

    regulatory_followup = any(
        term in text
        for term in (
            "data request",
            "response to ferc",
            "supplemental information",
        )
    )

    if application_signal and score >= 6:
        decision = PROMOTE
        confidence = "high"
    elif application_signal or regulatory_followup:
        decision = WATCH
        confidence = "medium"
    elif project_names and filing_count >= 2:
        decision = WATCH
        confidence = "medium"
    else:
        decision = IGNORE
        confidence = "medium"

    return ProjectQualification(
        docket=docket,
        decision=decision,
        score=score,
        reasons=reasons,
        confidence=confidence,
    )


def qualify_candidates(candidates: Iterable) -> list[ProjectQualification]:
    results = [qualify_candidate(candidate) for candidate in candidates]
    order = {PROMOTE: 2, WATCH: 1, IGNORE: 0}
    results.sort(
        key=lambda item: (
            order[item.decision],
            item.score,
            item.confidence == "high",
        ),
        reverse=True,
    )
    return results


def qualification_to_dict(
    qualification: ProjectQualification,
) -> dict:
    return asdict(qualification)
