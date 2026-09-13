from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from ferc_filter.company_project_discovery import (
    ProjectCandidate,
    root_docket,
)
from ferc_filter.project_candidate_qualifier import (
    qualify_candidate,
)
from ferc_filter.project_materiality import (
    DROP,
    TRACK,
    WATCH,
    assess_materiality,
)


def _projects(registry: dict[str, Any]) -> list[dict[str, Any]]:
    projects = registry.get("projects") or []
    if isinstance(projects, list):
        return projects
    if isinstance(projects, dict):
        return list(projects.values())
    raise ValueError("Registry projects must be a list or dictionary.")


def existing_roots(
    registry: dict[str, Any],
    company_id: str,
) -> set[str]:
    roots = set()
    for project in _projects(registry):
        owner = str(
            project.get("company_id")
            or project.get("company")
            or project.get("ticker")
            or ""
        ).upper()
        if owner != company_id.upper():
            continue
        docket = root_docket(
            str(
                project.get("docket")
                or project.get("docket_number")
                or ""
            )
        )
        if docket:
            roots.add(docket)
    return roots


def candidate_project_name(candidate: ProjectCandidate) -> str:
    if candidate.project_names:
        return candidate.project_names[0]

    # Discovery may not have extracted a structured project name yet.
    # Do not invent one: use the docket as a temporary display label.
    return f"FERC Project {candidate.docket}"


def build_registry_record(
    candidate: ProjectCandidate,
    *,
    capex: float | None = None,
    capacity: float | None = None,
    target_in_service: str | None = None,
) -> dict[str, Any]:
    return {
        "company_id": candidate.company_id,
        "name": candidate_project_name(candidate),
        "docket": root_docket(candidate.docket) or candidate.docket,
        "capex": capex,
        "capacity": capacity,
        "target_in_service": target_in_service,
        "discovery_source": "ferc_elibrary",
        "discovered_at": datetime.now().isoformat(),
        "auto_discovered": True,
    }


def process_candidates(
    registry: dict[str, Any],
    company_id: str,
    candidates: list[ProjectCandidate],
    evidence: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    evidence = evidence or {}
    tracked = existing_roots(registry, company_id)

    track_records = []
    watch_records = []
    drop_records = []
    existing_records = []

    for candidate in candidates:
        root = root_docket(candidate.docket) or candidate.docket

        if root in tracked:
            existing_records.append(
                {
                    "docket": root,
                    "status": "already_tracked",
                }
            )
            continue

        qualification = qualify_candidate(candidate)
        known = evidence.get(root, {})

        materiality = assess_materiality(
            candidate,
            qualification,
            capex=known.get("capex"),
            capacity=known.get("capacity"),
            target_in_service=known.get("target_in_service"),
        )

        result = {
            "docket": root,
            "qualification": asdict(qualification),
            "materiality": asdict(materiality),
        }

        if materiality.decision == TRACK:
            result["registry_record"] = build_registry_record(
                candidate,
                capex=materiality.capex,
                capacity=materiality.capacity,
                target_in_service=materiality.target_in_service,
            )
            track_records.append(result)
        elif materiality.decision == WATCH:
            watch_records.append(result)
        else:
            drop_records.append(result)

    return {
        "company_id": company_id,
        "existing": existing_records,
        "track": track_records,
        "watch": watch_records,
        "drop": drop_records,
    }


def apply_track_records(
    registry: dict[str, Any],
    result: dict[str, Any],
) -> int:
    """
    Add only TRACK records to a list-based registry.
    Existing docket roots are never duplicated.
    """
    projects = registry.get("projects")
    if not isinstance(projects, list):
        raise ValueError(
            "Automatic registry update currently requires registry['projects'] "
            "to be a list."
        )

    company_id = result["company_id"]
    roots = existing_roots(registry, company_id)
    added = 0

    for item in result["track"]:
        record = item["registry_record"]
        root = root_docket(record["docket"]) or record["docket"]
        if root in roots:
            continue
        projects.append(record)
        roots.add(root)
        added += 1

    return added
