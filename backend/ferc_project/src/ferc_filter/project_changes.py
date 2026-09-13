from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class ProjectChange:
    project: str
    docket: str
    change_type: str
    summary: str
    importance: str
    current: Any = None
    prior: Any = None


def _load(path: Path) -> dict[str, Any]:
    return __import__("json").loads(path.read_text(encoding="utf-8"))


def _pick(payload: dict[str, Any], *keys: str) -> Any:
    value: Any = payload
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def compare_project_snapshots(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
) -> list[ProjectChange]:
    changes: list[ProjectChange] = []

    project = current.get("project")
    docket = current.get("docket")

    if previous is None:
        return [
            ProjectChange(
                project=project,
                docket=docket,
                change_type="new_project_snapshot",
                summary="No prior weekly snapshot exists.",
                importance="low",
            )
        ]

    prev_stage = _pick(previous, "status", "stage")
    curr_stage = _pick(current, "status", "stage")
    if prev_stage != curr_stage:
        changes.append(
            ProjectChange(
                project=project,
                docket=docket,
                change_type="lifecycle_change",
                summary=f"Lifecycle changed from {prev_stage or 'unknown'} to {curr_stage or 'unknown'}.",
                importance="high",
                current=curr_stage,
                prior=prev_stage,
            )
        )

    prev_headline = _pick(previous, "status", "headline")
    curr_headline = _pick(current, "status", "headline")
    if prev_headline != curr_headline and (
        prev_stage == curr_stage
    ):
        changes.append(
            ProjectChange(
                project=project,
                docket=docket,
                change_type="status_change",
                summary=f"Project status changed from '{prev_headline or 'unknown'}' to '{curr_headline or 'unknown'}'.",
                importance="high",
                current=curr_headline,
                prior=prev_headline,
            )
        )

    prev_block = _pick(previous, "attention", "blocking_action")
    curr_block = _pick(current, "attention", "blocking_action")
    if prev_block != curr_block:
        if curr_block:
            text = f"New blocking action: {curr_block}."
            change_type = "new_blocker"
        elif prev_block:
            text = f"Blocking action resolved: {prev_block}."
            change_type = "blocker_resolved"
        else:
            text = "Blocking-action state changed."
            change_type = "blocking_state_change"

        changes.append(
            ProjectChange(
                project=project,
                docket=docket,
                change_type=change_type,
                summary=text,
                importance="high",
                current=curr_block,
                prior=prev_block,
            )
        )

    prev_timing = _pick(previous, "attention", "requested_timing")
    curr_timing = _pick(current, "attention", "requested_timing")
    if prev_timing != curr_timing:
        changes.append(
            ProjectChange(
                project=project,
                docket=docket,
                change_type="timing_change",
                summary=f"Relevant timing changed from {prev_timing or 'not established'} to {curr_timing or 'not established'}.",
                importance="medium",
                current=curr_timing,
                prior=prev_timing,
            )
        )

    prev_endpoint = _pick(
        previous, "timeline", "projected_endpoint", "value"
    )
    curr_endpoint = _pick(
        current, "timeline", "projected_endpoint", "value"
    )
    if prev_endpoint != curr_endpoint:
        changes.append(
            ProjectChange(
                project=project,
                docket=docket,
                change_type="endpoint_change",
                summary=f"Projected endpoint changed from {prev_endpoint or 'not established'} to {curr_endpoint or 'not established'}.",
                importance="high",
                current=curr_endpoint,
                prior=prev_endpoint,
            )
        )

    prev_filings = {
        item.get("accession")
        for item in (_pick(previous, "evidence", "material_filings") or [])
        if item.get("accession")
    }
    curr_material = _pick(current, "evidence", "material_filings") or []
    new_filings = [
        item for item in curr_material
        if item.get("accession") not in prev_filings
    ]

    for item in new_filings:
        changes.append(
            ProjectChange(
                project=project,
                docket=docket,
                change_type="new_material_filing",
                summary=f"New material filing: {item.get('title') or item.get('accession')}.",
                importance=item.get("importance", "high"),
                current=item,
            )
        )

    if not changes:
        changes.append(
            ProjectChange(
                project=project,
                docket=docket,
                change_type="no_material_change",
                summary="No material lifecycle, blocker, timing, endpoint, or selected filing change detected.",
                importance="low",
            )
        )

    return changes


def changes_to_dict(changes: list[ProjectChange]) -> list[dict[str, Any]]:
    return [
        {
            "project": item.project,
            "docket": item.docket,
            "change_type": item.change_type,
            "summary": item.summary,
            "importance": item.importance,
            "current": item.current,
            "prior": item.prior,
        }
        for item in changes
    ]


def load_previous_weekly(
    weekly_path: Path,
) -> dict[str, dict[str, Any]]:
    if not weekly_path.exists():
        return {}

    payload = _load(weekly_path)
    return {
        item["docket"]: item
        for item in payload.get("projects", [])
        if item.get("docket")
    }
