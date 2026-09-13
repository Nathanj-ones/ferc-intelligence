from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from ferc_filter.project_view import ProjectView

@dataclass
class DisplayMilestone:
    accession: str
    date: Optional[datetime]
    title: str
    event_type: str

@dataclass
class ProjectDisplay:
    project: str
    docket: str
    headline: str
    stage: str
    stage_status: str
    regulatory_health: str
    next_gate: Optional[str]
    gate_status: str
    next_expected_event: str
    blocking_action: Optional[str]
    responsible_party: Optional[str]
    requested_timing: Optional[datetime]
    timing_type: Optional[str]
    timeline_signal: str
    latest_material_event: Optional[DisplayMilestone]
    key_milestones: list[DisplayMilestone] = field(default_factory=list)
    confidence: str = "medium"

def _headline(stage, next_gate, blocking_action, responsible_party):
    if blocking_action and responsible_party:
        if next_gate == "construction_authorization":
            return "Construction authorization pending"
        return f"{next_gate.replace('_', ' ').title()} pending" if next_gate else "Regulatory action pending"
    labels = {
        "construction": "Construction underway or authorized",
        "post_certificate_pre_construction": "Certificate granted; pre-construction",
        "environmental_review": "Environmental review",
        "in_service": "In service",
    }
    return labels.get(stage, stage.replace("_", " ").title())

def _timing_type(source):
    if source is None:
        return None
    return {
        "filing_text_requested_timing": "sponsor_requested",
        "tracker_estimated_due_date": "estimated_response_due",
        "ferc_imposed_deadline": "ferc_imposed",
    }.get(source, source)

def build_project_display(view: ProjectView, *, max_milestones: int = 3) -> ProjectDisplay:
    """Create a compact display object without performing new FERC analysis."""
    if view.lifecycle is None:
        raise ValueError("ProjectDisplay requires ProjectView.lifecycle")
    if view.investor_summary is None:
        raise ValueError("ProjectDisplay requires ProjectView.investor_summary")

    lifecycle = view.lifecycle
    investor = view.investor_summary
    blocking = next(
        (a for a in view.pending_regulatory_actions if a.blocking_next_stage),
        None,
    )
    milestones = [
        DisplayMilestone(m.accession, m.date, m.title, m.event_type)
        for m in view.milestones[:max_milestones]
    ]

    return ProjectDisplay(
        project=view.project,
        docket=view.docket,
        headline=_headline(
            lifecycle.current_stage,
            lifecycle.next_gate,
            blocking.action_requested if blocking else None,
            blocking.responsible_party if blocking else None,
        ),
        stage=lifecycle.current_stage,
        stage_status=lifecycle.stage_status,
        regulatory_health=investor.regulatory_health,
        next_gate=lifecycle.next_gate,
        gate_status=lifecycle.gate_status,
        next_expected_event=investor.next_expected_event,
        blocking_action=blocking.action_requested if blocking else None,
        responsible_party=blocking.responsible_party if blocking else None,
        requested_timing=blocking.requested_timing if blocking else None,
        timing_type=_timing_type(blocking.timing_source) if blocking else None,
        timeline_signal=investor.timeline_signal,
        latest_material_event=milestones[0] if milestones else None,
        key_milestones=milestones,
        confidence=investor.confidence,
    )

def _milestone_to_dict(m):
    if m is None:
        return None
    return {
        "accession": m.accession,
        "date": m.date.isoformat() if m.date else None,
        "title": m.title,
        "event_type": m.event_type,
    }

def project_display_to_dict(display: ProjectDisplay) -> dict:
    return {
        "project": display.project,
        "docket": display.docket,
        "headline": display.headline,
        "position": {
            "stage": display.stage,
            "stage_status": display.stage_status,
            "regulatory_health": display.regulatory_health,
        },
        "next_step": {
            "gate": display.next_gate,
            "gate_status": display.gate_status,
            "next_expected_event": display.next_expected_event,
            "blocking_action": display.blocking_action,
            "responsible_party": display.responsible_party,
            "requested_timing": display.requested_timing.isoformat() if display.requested_timing else None,
            "timing_type": display.timing_type,
        },
        "timeline_signal": display.timeline_signal,
        "latest_material_event": _milestone_to_dict(display.latest_material_event),
        "key_milestones": [_milestone_to_dict(m) for m in display.key_milestones],
        "confidence": display.confidence,
    }
