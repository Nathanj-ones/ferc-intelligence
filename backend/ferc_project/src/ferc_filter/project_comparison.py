from __future__ import annotations

from dataclasses import dataclass

from ferc_filter.project_context import ProjectContext
from ferc_filter.project_display import ProjectDisplay


@dataclass
class ProjectComparisonItem:
    context: ProjectContext
    regulatory: ProjectDisplay


def build_project_comparison_item(
    context: ProjectContext,
    regulatory: ProjectDisplay,
) -> ProjectComparisonItem:
    if context.project != regulatory.project:
        raise ValueError(
            "ProjectContext and ProjectDisplay project names do not match"
        )

    if (
        context.docket
        and regulatory.docket
        and context.docket != regulatory.docket
    ):
        raise ValueError(
            "ProjectContext and ProjectDisplay dockets do not match"
        )

    return ProjectComparisonItem(
        context=context,
        regulatory=regulatory,
    )


def project_comparison_item_to_dict(
    item: ProjectComparisonItem,
) -> dict:
    c = item.context
    r = item.regulatory

    return {
        "project": r.project,
        "docket": r.docket,
        "context": {
            "company": c.company,
            "project_type": c.project_type,
            "capacity": {
                "value": c.capacity_value,
                "unit": c.capacity_unit,
                "source": c.capacity_source,
            },
            "capex": {
                "value": c.capex_value,
                "currency": c.capex_currency,
                "unit": c.capex_unit,
                "source": c.capex_source,
            },
            "target_in_service": {
                "value": c.target_in_service,
                "source": c.target_in_service_source,
            },
        },
        "regulatory": {
            "headline": r.headline,
            "stage": r.stage,
            "stage_status": r.stage_status,
            "regulatory_health": r.regulatory_health,
            "next_gate": r.next_gate,
            "gate_status": r.gate_status,
            "blocking_action": r.blocking_action,
            "responsible_party": r.responsible_party,
            "requested_timing": (
                r.requested_timing.isoformat()
                if r.requested_timing
                else None
            ),
            "timing_type": r.timing_type,
            "timeline_signal": r.timeline_signal,
            "next_expected_event": r.next_expected_event,
            "latest_material_event": (
                {
                    "accession": r.latest_material_event.accession,
                    "date": (
                        r.latest_material_event.date.isoformat()
                        if r.latest_material_event.date
                        else None
                    ),
                    "title": r.latest_material_event.title,
                    "event_type": r.latest_material_event.event_type,
                }
                if r.latest_material_event
                else None
            ),
            "confidence": r.confidence,
        },
    }
