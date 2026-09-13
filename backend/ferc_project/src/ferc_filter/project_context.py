from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ProjectContext:
    project: str
    docket: Optional[str] = None
    company: Optional[str] = None
    project_type: Optional[str] = None

    capacity_value: Optional[float] = None
    capacity_unit: Optional[str] = None

    capex_value: Optional[float] = None
    capex_currency: Optional[str] = None
    capex_unit: Optional[str] = None

    target_in_service: Optional[str] = None

    # Source labels are deliberately explicit because these fields may
    # come from company materials, FERC records, or another source.
    capacity_source: Optional[str] = None
    capex_source: Optional[str] = None
    target_in_service_source: Optional[str] = None


def project_context_to_dict(context: ProjectContext) -> dict:
    return {
        "project": context.project,
        "docket": context.docket,
        "company": context.company,
        "project_type": context.project_type,
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
    }
