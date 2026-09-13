from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ferc_filter.activity_grouper import ActivityGroup
from ferc_filter.regulatory_outlook import RegulatoryOutlook
from ferc_filter.project_lifecycle import ProjectLifecycle, project_lifecycle_to_dict
from ferc_filter.investor_summary import InvestorSummary, investor_summary_to_dict
from ferc_filter.pending_regulatory_action import (
    PendingRegulatoryAction,
    pending_action_to_dict,
)
from ferc_filter.activity_summarizer import (
    ActivitySummary,
    activity_type_label,
    summarize_activity_groups,
)


# ==============================================================
# DATA MODELS
# ==============================================================

@dataclass
class ProjectMilestone:
    accession: str
    date: Optional[datetime]
    event_type: str
    title: str
    description: str


@dataclass
class ProjectCounts:
    raw: int
    deduped: int
    alerts: int
    review: int
    suppressed: int
    activity_groups: int


@dataclass
class ProjectView:
    project: str
    docket: str
    current_stage: str

    counts: ProjectCounts

    milestones: list[
        ProjectMilestone
    ] = field(
        default_factory=list
    )

    activity: list[
        ActivitySummary
    ] = field(
        default_factory=list
    )

    regulatory_outlook: Optional[RegulatoryOutlook] = None

    lifecycle: Optional[ProjectLifecycle] = None

    investor_summary: Optional[InvestorSummary] = None

    pending_regulatory_actions: list[PendingRegulatoryAction] = field(
        default_factory=list
    )

    generated_at: str = ""


# ==============================================================
# MILESTONE LABELS
# ==============================================================

EVENT_TYPE_LABELS = {
    "major_construction_authorization": (
        "Major Construction Authorization"
    ),
    "construction_or_service_authorization": (
        "Construction / Service Authorization"
    ),
    "environmental_milestone": (
        "Environmental Milestone"
    ),
    "regulatory_decision": (
        "Regulatory Decision"
    ),
    "timing_or_status_change": (
        "Timing / Status Change"
    ),
    "authorization_request": (
        "Authorization Request"
    ),
    "environmental_comment": (
        "Environmental Comment"
    ),
}


# ==============================================================
# LIFECYCLE STAGE
# ==============================================================

def _stage_rank(
    stage: str,
) -> int:

    ranks = {
        "Unknown": 0,
        "Application": 1,
        "Environmental Review": 2,
        "Certificate": 3,
        "Construction": 4,
        "In Service": 5,
    }

    return ranks.get(
        stage,
        0,
    )


def _infer_stage_from_event(
    event_type: str,
    description: str,
) -> Optional[str]:
    """
    Infer lifecycle stage from a material milestone.

    This is project-view logic only.
    It does not modify v0.2 classifier behavior.
    """

    text = (
        description or ""
    ).lower()

    if event_type in {
        "major_construction_authorization",
        "construction_or_service_authorization",
    }:
        return "Construction"

    if event_type == "environmental_milestone":
        return "Environmental Review"

    if event_type == "regulatory_decision":

        if any(
            phrase in text
            for phrase in (
                "issuing certificate",
                "certificate of public convenience",
                "certificate order",
                "issuing a certificate",
            )
        ):
            return "Certificate"

        if any(
            phrase in text
            for phrase in (
                "commence construction",
                "construction authorization",
                "notice to proceed",
            )
        ):
            return "Construction"

        return "Certificate"

    return None


def infer_current_stage(
    milestones: list[ProjectMilestone],
) -> str:
    """
    Infer the latest lifecycle stage represented by
    material milestones.
    """

    if not milestones:
        return "Unknown"

    current = "Unknown"

    for milestone in milestones:

        stage = _infer_stage_from_event(
            milestone.event_type,
            milestone.description,
        )

        if stage is None:
            continue

        if _stage_rank(stage) > _stage_rank(
            current
        ):
            current = stage

    return current


# ==============================================================
# MILESTONES
# ==============================================================

def build_project_milestones(
    alerts: list[dict],
) -> list[ProjectMilestone]:
    """
    Convert classifier ALERT records into structured milestones.
    """

    milestones = []

    for alert in alerts:

        event_type = (
            alert.get(
                "event_type"
            )
            or "unknown"
        )

        description = (
            alert.get(
                "description"
            )
            or ""
        )

        title = EVENT_TYPE_LABELS.get(
            event_type,
            event_type.replace(
                "_",
                " ",
            ).title(),
        )

        milestones.append(
            ProjectMilestone(
                accession=(
                    alert.get(
                        "accession"
                    )
                    or ""
                ),
                date=alert.get(
                    "date"
                ),
                event_type=event_type,
                title=title,
                description=description,
            )
        )

    milestones.sort(
        key=lambda item: (
            item.date
            or datetime.min
        ),
        reverse=True,
    )

    return milestones


# ==============================================================
# PROJECT VIEW BUILDER
# ==============================================================

def build_project_view(
    *,
    project: str,
    docket: str,
    raw_count: int,
    deduped_count: int,
    alerts: list[dict],
    review_count: int,
    suppressed_count: int,
    activity_groups: list[ActivityGroup],
    regulatory_outlook: Optional[RegulatoryOutlook] = None,
    lifecycle: Optional[ProjectLifecycle] = None,
    investor_summary: Optional[InvestorSummary] = None,
    pending_regulatory_actions: Optional[list[PendingRegulatoryAction]] = None,
    generated_at: Optional[str] = None,
) -> ProjectView:
    """
    Build the dashboard-ready project object.

    Inputs are expected to come from:
        FERC ingestion
        enrichment
        frozen v0.2 classifier
        activity grouping
        activity summarization

    No classifier logic is changed here.
    """

    milestones = build_project_milestones(
        alerts
    )

    activity = summarize_activity_groups(
        activity_groups
    )

    current_stage = infer_current_stage(
        milestones
    )

    counts = ProjectCounts(
        raw=raw_count,
        deduped=deduped_count,
        alerts=len(alerts),
        review=review_count,
        suppressed=suppressed_count,
        activity_groups=len(
            activity_groups
        ),
    )

    if generated_at is None:
        generated_at = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

    return ProjectView(
        project=project,
        docket=docket,
        current_stage=current_stage,
        counts=counts,
        milestones=milestones,
        activity=activity,
        regulatory_outlook=regulatory_outlook,
        lifecycle=lifecycle,
        investor_summary=investor_summary,
        pending_regulatory_actions=(pending_regulatory_actions or []),
        generated_at=generated_at,
    )


# ==============================================================
# SERIALIZATION
# ==============================================================

def project_view_to_dict(
    view: ProjectView,
) -> dict:
    """
    Convert ProjectView into a JSON-compatible dictionary.

    This is the dashboard/API contract.
    """

    return {
        "project": view.project,
        "docket": view.docket,
        "current_stage": view.current_stage,
        "generated_at": view.generated_at,
        "counts": {
            "raw": view.counts.raw,
            "deduped": view.counts.deduped,
            "alerts": view.counts.alerts,
            "review": view.counts.review,
            "suppressed": view.counts.suppressed,
            "activity_groups": (
                view.counts.activity_groups
            ),
        },
        "milestones": [
            {
                "accession": milestone.accession,
                "date": (
                    milestone.date.isoformat()
                    if milestone.date
                    else None
                ),
                "event_type": milestone.event_type,
                "title": milestone.title,
                "description": milestone.description,
            }
            for milestone in view.milestones
        ],
        "lifecycle": (
            project_lifecycle_to_dict(view.lifecycle)
            if view.lifecycle
            else None
        ),
        "pending_regulatory_actions": [
            pending_action_to_dict(action)
            for action in view.pending_regulatory_actions
        ],
        "investor_summary": (
            investor_summary_to_dict(view.investor_summary)
            if view.investor_summary
            else None
        ),
        "regulatory_outlook": (
            {
                "regulatory_status": view.regulatory_outlook.regulatory_status,
                "tracked_ferc_requests": view.regulatory_outlook.tracked_ferc_requests,
                "responded_ferc_requests": view.regulatory_outlook.responded_ferc_requests,
                "open_ferc_requests": view.regulatory_outlook.open_ferc_requests,
                "latest_material_milestone": (
                    {
                        "accession": view.regulatory_outlook.latest_material_milestone.accession,
                        "date": (
                            view.regulatory_outlook.latest_material_milestone.date.isoformat()
                            if view.regulatory_outlook.latest_material_milestone.date
                            else None
                        ),
                        "event_type": view.regulatory_outlook.latest_material_milestone.event_type,
                        "title": view.regulatory_outlook.latest_material_milestone.title,
                        "description": view.regulatory_outlook.latest_material_milestone.description,
                    }
                    if view.regulatory_outlook.latest_material_milestone
                    else None
                ),
                "schedule_watch": [
                    {
                        "kind": watch.kind,
                        "request_accession": watch.request_accession,
                        "request_date": (
                            watch.request_date.isoformat()
                            if watch.request_date
                            else None
                        ),
                        "estimated_due_date": (
                            watch.estimated_due_date.isoformat()
                            if watch.estimated_due_date
                            else None
                        ),
                        "response_date": (
                            watch.response_date.isoformat()
                            if watch.response_date
                            else None
                        ),
                        "detail": watch.detail,
                    }
                    for watch in view.regulatory_outlook.schedule_watch
                ],
                "progression_counts": view.regulatory_outlook.progression_counts,
                "next_expected_activity": view.regulatory_outlook.next_expected_activity,
                "investor_summary": view.regulatory_outlook.investor_summary,
            }
            if view.regulatory_outlook
            else None
        ),
        "activity": [
            {
                "activity_type": (
                    summary.activity_type
                ),
                "label": activity_type_label(
                    summary.activity_type
                ),
                "start_date": (
                    summary.start_date.isoformat()
                    if summary.start_date
                    else None
                ),
                "end_date": (
                    summary.end_date.isoformat()
                    if summary.end_date
                    else None
                ),
                "filing_count": (
                    summary.filing_count
                ),
                "event_types": (
                    summary.event_types
                ),
                "relationship": (
                    summary.relationship
                ),
                "representative_accessions": (
                    summary.representative_accessions
                ),
                "summary": summary.summary,
            }
            for summary in view.activity
        ],
    }