from dataclasses import dataclass
from typing import Optional

@dataclass
class ProjectTimelineStep:
    key: str
    label: str
    status: str
    date: Optional[str] = None
    accession: Optional[str] = None
    source: Optional[str] = None

@dataclass
class ProjectTimeline:
    steps: list[ProjectTimelineStep]
    current_step: Optional[str]
    endpoint: str
    projected_endpoint: Optional[str]
    projected_endpoint_source: Optional[str]

def build_project_timeline(context, display):
    by_type = {m.event_type: m for m in reversed(display.key_milestones)}
    steps = []

    for event_type, key, label in (
        ("environmental_milestone", "environmental_review", "Environmental Review"),
        ("regulatory_decision", "certificate", "Certificate"),
        ("construction_or_service_authorization", "construction_authorization", "Construction Authorization"),
    ):
        m = by_type.get(event_type)
        if m:
            steps.append(ProjectTimelineStep(
                key=key, label=label, status="completed",
                date=m.date.date().isoformat() if m.date else None,
                accession=m.accession, source="FERC",
            ))

    if display.stage == "post_certificate_pre_construction":
        # Avoid duplicating a completed construction authorization.
        if not any(s.key == "construction_authorization" for s in steps):
            steps.append(ProjectTimelineStep(
                key="construction_authorization",
                label="Construction Authorization",
                status="current",
                date=display.requested_timing.date().isoformat() if display.requested_timing else None,
                source="Sponsor requested timing" if display.requested_timing else None,
            ))
        current = "construction_authorization"
    elif display.stage == "construction":
        steps.append(ProjectTimelineStep(
            key="construction", label="Construction",
            status="current", source="FERC lifecycle",
        ))
        current = "construction"
    elif display.stage == "in_service":
        current = "commercial_operation"
    else:
        current = display.stage or None

    steps.append(ProjectTimelineStep(
        key="commercial_operation",
        label="Commercial Operation / In-Service",
        status="current" if display.stage == "in_service" else "future",
        date=context.target_in_service,
        source=context.target_in_service_source,
    ))

    return ProjectTimeline(
        steps=steps,
        current_step=current,
        endpoint="commercial_operation",
        projected_endpoint=context.target_in_service,
        projected_endpoint_source=context.target_in_service_source,
    )

def project_timeline_to_dict(value):
    return {
        "current_step": value.current_step,
        "endpoint": value.endpoint,
        "projected_endpoint": {
            "value": value.projected_endpoint,
            "source": value.projected_endpoint_source,
        },
        "steps": [
            {
                "key": s.key, "label": s.label, "status": s.status,
                "date": s.date, "accession": s.accession, "source": s.source,
            } for s in value.steps
        ],
    }
