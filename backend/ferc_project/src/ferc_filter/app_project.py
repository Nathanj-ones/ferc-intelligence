from dataclasses import dataclass
from typing import Optional
from .project_context import ProjectContext
from .project_display import ProjectDisplay
from .project_timeline import ProjectTimeline, project_timeline_to_dict
from .project_evidence import ProjectEvidence, project_evidence_to_dict

@dataclass
class AppProject:
    project: str
    docket: str
    identity: dict
    scale: dict
    status: dict
    timeline: dict
    attention: dict
    evidence: dict
    schema_version: str = "app_project_v1"

def build_app_project(
    context: ProjectContext,
    display: ProjectDisplay,
    project_timeline: ProjectTimeline,
    project_evidence: list[ProjectEvidence] | None = None,
) -> AppProject:
    if context.project != display.project:
        raise ValueError("ProjectContext and ProjectDisplay project names do not match")
    if context.docket and context.docket != display.docket:
        raise ValueError("ProjectContext and ProjectDisplay dockets do not match")
    if project_timeline.current_step is None:
        raise ValueError("ProjectTimeline requires a current_step")
    latest = display.latest_material_event
    return AppProject(
        project=display.project,
        docket=display.docket,
        identity={
            "project": display.project,
            "docket": display.docket,
            "company": context.company,
            "project_type": context.project_type,
        },
        scale={
            "capacity": {
                "value": context.capacity_value,
                "unit": context.capacity_unit,
                "source": context.capacity_source,
            },
            "capex": {
                "value": context.capex_value,
                "currency": context.capex_currency,
                "unit": context.capex_unit,
                "source": context.capex_source,
            },
            "target_in_service": {
                "value": context.target_in_service,
                "source": context.target_in_service_source,
            },
        },
        status={
            "headline": display.headline,
            "stage": display.stage,
            "stage_status": display.stage_status,
            "regulatory_health": display.regulatory_health,
            "confidence": display.confidence,
        },
        timeline=project_timeline_to_dict(project_timeline),
        attention={
            "blocking_action": display.blocking_action,
            "responsible_party": display.responsible_party,
            "requested_timing": display.requested_timing.isoformat() if display.requested_timing else None,
            "timing_type": display.timing_type,
        },
        evidence={
            "latest_material_accession": latest.accession if latest else None,
            "key_milestone_accessions": [
                m.accession for m in display.key_milestones
            ],
            "material_filings": project_evidence_to_dict(
                project_evidence or []
            ),
        },
    )

def app_project_to_dict(value: AppProject) -> dict:
    return {
        "schema_version": value.schema_version,
        "project": value.project,
        "docket": value.docket,
        "identity": value.identity,
        "scale": value.scale,
        "status": value.status,
        "timeline": value.timeline,
        "attention": value.attention,
        "evidence": value.evidence,
    }

def validate_app_project(value: AppProject) -> list[str]:
    errors = []
    if value.schema_version != "app_project_v1":
        errors.append("Unsupported schema_version")
    if not value.project:
        errors.append("Missing project")
    if not value.docket:
        errors.append("Missing docket")
    for name in ("identity", "scale", "status", "timeline", "attention", "evidence"):
        if not isinstance(getattr(value, name), dict):
            errors.append(f"Section '{name}' must be an object")
    return errors
