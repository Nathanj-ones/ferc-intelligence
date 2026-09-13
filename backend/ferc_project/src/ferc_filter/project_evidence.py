from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ferc_filter.project_display import ProjectDisplay


@dataclass
class ProjectEvidence:
    accession: str
    date: Optional[str]
    title: str
    event_type: str
    importance: str
    role: str
    source: str = "FERC eLibrary"


def evidence_from_display(display: ProjectDisplay) -> list[ProjectEvidence]:
    """
    Package already-selected material milestones as clickable evidence.

    This layer does not decide which filings are important; it uses the
    milestones already selected by ProjectDisplay.
    """

    role_by_event = {
        "environmental_milestone": "environmental_review",
        "regulatory_decision": "regulatory_decision",
        "construction_or_service_authorization": (
            "construction_gate"
        ),
    }

    evidence = []

    for milestone in display.key_milestones:
        role = role_by_event.get(
            milestone.event_type,
            "material_project_event",
        )

        evidence.append(
            ProjectEvidence(
                accession=milestone.accession,
                date=(
                    milestone.date.isoformat()
                    if milestone.date
                    else None
                ),
                title=milestone.title,
                event_type=milestone.event_type,
                importance="high",
                role=role,
            )
        )

    return evidence


def project_evidence_to_dict(
    evidence: list[ProjectEvidence],
) -> list[dict]:
    return [
        {
            "accession": item.accession,
            "date": item.date,
            "title": item.title,
            "event_type": item.event_type,
            "importance": item.importance,
            "role": item.role,
            "source": item.source,
        }
        for item in evidence
    ]
