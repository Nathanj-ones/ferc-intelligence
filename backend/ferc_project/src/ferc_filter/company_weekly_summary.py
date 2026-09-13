from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CompanyProjectSummary:
    project: str
    docket: str
    stage: str | None
    headline: str | None
    endpoint: str | None
    target_in_service: str | None
    blocking_action: str | None
    waiting_on: str | None
    relevant_timing: str | None
    latest_material_event: dict[str, Any] | None
    capex_value: float | None
    capex_unit: str | None
    capacity_value: float | None
    capacity_unit: str | None
    data_quality: str


@dataclass
class CompanyWeeklySummary:
    company: str
    projects: list[CompanyProjectSummary]
    project_count: int
    attention_count: int
    partial_count: int
    schema_version: str = "company_weekly_summary_v0.1"


def _project_from_app(payload: dict[str, Any]) -> CompanyProjectSummary:
    identity = payload.get("identity", {})
    scale = payload.get("scale", {})
    status = payload.get("status", {})
    timeline = payload.get("timeline", {})
    attention = payload.get("attention", {})
    evidence = payload.get("evidence", {})

    capex = scale.get("capex", {}) or {}
    capacity = scale.get("capacity", {}) or {}
    target = scale.get("target_in_service", {}) or {}

    material = evidence.get("material_filings", []) or []
    latest = material[0] if material else None

    quality = payload.get("monitor", {}).get(
        "enrichment_status",
        "complete",
    )

    return CompanyProjectSummary(
        project=payload.get("project") or identity.get("project"),
        docket=payload.get("docket") or identity.get("docket"),
        stage=status.get("stage"),
        headline=status.get("headline"),
        endpoint=timeline.get("endpoint"),
        target_in_service=target.get("value"),
        blocking_action=attention.get("blocking_action"),
        waiting_on=attention.get("responsible_party"),
        relevant_timing=attention.get("requested_timing"),
        latest_material_event=latest,
        capex_value=capex.get("value"),
        capex_unit=capex.get("unit"),
        capacity_value=capacity.get("value"),
        capacity_unit=capacity.get("unit"),
        data_quality=quality,
    )


def build_company_weekly_summary(
    company: str,
    app_projects: list[dict[str, Any]],
) -> CompanyWeeklySummary:
    projects = [_project_from_app(item) for item in app_projects]

    # Thomas' preferred large-company ordering: disclosed capex first.
    projects.sort(
        key=lambda item: (
            item.capex_value is None,
            -(item.capex_value or 0),
            item.project,
        )
    )

    attention_count = sum(
        1 for item in projects if item.blocking_action
    )
    partial_count = sum(
        1 for item in projects if item.data_quality != "complete"
    )

    return CompanyWeeklySummary(
        company=company,
        projects=projects,
        project_count=len(projects),
        attention_count=attention_count,
        partial_count=partial_count,
    )


def company_weekly_summary_to_dict(
    value: CompanyWeeklySummary,
) -> dict[str, Any]:
    return {
        "schema_version": value.schema_version,
        "company": value.company,
        "project_count": value.project_count,
        "attention_count": value.attention_count,
        "partial_count": value.partial_count,
        "projects": [
            {
                "project": item.project,
                "docket": item.docket,
                "stage": item.stage,
                "headline": item.headline,
                "endpoint": item.endpoint,
                "target_in_service": item.target_in_service,
                "blocking_action": item.blocking_action,
                "waiting_on": item.waiting_on,
                "relevant_timing": item.relevant_timing,
                "latest_material_event": item.latest_material_event,
                "capex": {
                    "value": item.capex_value,
                    "unit": item.capex_unit,
                },
                "capacity": {
                    "value": item.capacity_value,
                    "unit": item.capacity_unit,
                },
                "data_quality": item.data_quality,
            }
            for item in value.projects
        ],
    }
