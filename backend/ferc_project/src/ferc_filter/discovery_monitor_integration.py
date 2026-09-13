from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ferc_filter.company_project_discovery import root_docket


def build_monitor_registry_record(
    company_id: str,
    docket: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    root = root_docket(docket) or docket
    project_name = evidence.get("project_name") or f"FERC Project {root}"
    context: dict[str, Any] = {}

    if evidence.get("capacity") is not None:
        context["capacity_value"] = evidence["capacity"]
        context["capacity_unit"] = evidence.get("capacity_unit")
        context["capacity_source"] = "ferc_filing"

    if evidence.get("capex") is not None:
        context["capex_value"] = evidence["capex"] / 1_000_000_000
        context["capex_currency"] = "USD"
        context["capex_unit"] = "billion"
        context["capex_source"] = "ferc_filing"

    if evidence.get("target_in_service"):
        context["target_in_service"] = evidence["target_in_service"]
        context["target_in_service_source"] = "ferc_filing"

    return {
        "company_id": company_id,
        "project": project_name,
        "docket": root,
        "enabled": True,
        "context": context,
        "discovery": {
            "auto_discovered": True,
            "source": "ferc_elibrary",
            "source_accessions": evidence.get("source_accessions") or [],
            "evidence_confidence": evidence.get("confidence"),
            "promoted_at": datetime.now().isoformat(),
        },
    }


def promote_pipeline_tracks(
    registry: dict[str, Any],
    company_id: str,
    pipeline_result: dict[str, Any],
) -> list[dict[str, Any]]:
    projects = registry.get("projects")
    if not isinstance(projects, list):
        raise ValueError("Registry projects must be list-based.")

    existing = {
        root_docket(project.get("docket"))
        for project in projects
        if str(project.get("company_id") or "").upper() == company_id.upper()
    }

    added = []

    for item in pipeline_result.get("evaluated", []):
        materiality = item.get("materiality") or {}
        if materiality.get("decision") != "TRACK":
            continue

        docket = root_docket(item.get("docket"))
        if not docket or docket in existing:
            continue

        record = build_monitor_registry_record(
            company_id,
            docket,
            item.get("evidence") or {},
        )
        projects.append(record)
        existing.add(docket)
        added.append(record)

    return added


def save_registry(path: Path, registry: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
