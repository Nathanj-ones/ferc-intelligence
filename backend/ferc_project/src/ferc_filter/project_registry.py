from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CompanyConfig:
    id: str
    name: str | None
    enabled: bool = True


@dataclass
class ProjectConfig:
    company_id: str
    project: str
    docket: str
    enabled: bool = True
    context: dict[str, Any] = field(default_factory=dict)
    discovery: dict[str, Any] = field(default_factory=dict)

@dataclass
class Registry:
    history_start_date: str
    overlap_days: int
    output_root: str
    app_output_root: str
    companies: list[CompanyConfig]
    projects: list[ProjectConfig]

    def company_map(self) -> dict[str, CompanyConfig]:
        return {item.id: item for item in self.companies}

    def enabled_projects(self) -> list[ProjectConfig]:
        companies = self.company_map()
        return [
            project
            for project in self.projects
            if project.enabled
            and project.company_id in companies
            and companies[project.company_id].enabled
            and companies[project.company_id].name
        ]


def load_registry(path: str | Path) -> Registry:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))

    monitor = payload.get("monitor", {})
    companies = [CompanyConfig(**item) for item in payload.get("companies", [])]
    projects = [ProjectConfig(**item) for item in payload.get("projects", [])]

    if len(companies) != 6:
        raise ValueError(
            f"Registry must contain six company slots; found {len(companies)}"
        )

    seen_dockets: set[str] = set()
    for project in projects:
        if project.docket in seen_dockets:
            raise ValueError(
                f"Duplicate project docket in registry: {project.docket}"
            )
        seen_dockets.add(project.docket)

    return Registry(
        history_start_date=monitor.get("history_start_date", "01-01-2024"),
        overlap_days=int(monitor.get("overlap_days", 14)),
        output_root=monitor.get("output_root", "data/projects"),
        app_output_root=monitor.get("app_output_root", "data/app"),
        companies=companies,
        projects=projects,
    )
