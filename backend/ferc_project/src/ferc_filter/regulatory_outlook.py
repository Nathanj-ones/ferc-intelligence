from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ==============================================================
# DATA MODELS
# ==============================================================

@dataclass
class OutlookMilestone:
    accession: str
    date: Optional[datetime]
    event_type: str
    title: str
    description: str


@dataclass
class ScheduleWatch:
    kind: str
    request_accession: str
    request_date: Optional[datetime]
    estimated_due_date: Optional[datetime]
    response_date: Optional[datetime]
    detail: str


@dataclass
class RegulatoryOutlook:
    project: str
    docket: str
    current_stage: str

    regulatory_status: str

    open_ferc_requests: int
    tracked_ferc_requests: int
    responded_ferc_requests: int

    latest_material_milestone: Optional[
        OutlookMilestone
    ] = None

    schedule_watch: list[ScheduleWatch] = field(
        default_factory=list
    )

    progression_counts: dict[str, int] = field(
        default_factory=dict
    )

    next_expected_activity: str = (
        "Further regulatory action not established"
    )

    investor_summary: str = ""


# ==============================================================
# HELPERS
# ==============================================================

def _date_text(
    value: Optional[datetime],
) -> str:
    if value is None:
        return "unknown"

    return value.strftime(
        "%Y-%m-%d"
    )


def _display_event_type(
    event_type: str,
) -> str:
    labels = {
        "environmental_milestone":
            "Environmental Assessment",
        "regulatory_decision":
            "Regulatory Decision",
        "construction_or_service_authorization":
            "Construction / Service Authorization",
        "major_construction_authorization":
            "Construction Authorization",
        "delegated_order":
            "Delegated Authorization",
    }

    return labels.get(
        event_type,
        event_type.replace(
            "_",
            " ",
        ).title(),
    )


# ==============================================================
# OPEN REQUESTS
# ==============================================================

def count_open_requests(
    requests,
) -> int:
    return sum(
        1
        for request in requests
        if not request.response_found
    )


# ==============================================================
# LATEST MILESTONE
# ==============================================================

def find_latest_material_milestone(
    milestones,
) -> Optional[OutlookMilestone]:
    if not milestones:
        return None

    ordered = sorted(
        milestones,
        key=lambda milestone: (
            milestone.date
            or datetime.min
        ),
        reverse=True,
    )

    milestone = ordered[0]

    return OutlookMilestone(
        accession=milestone.accession,
        date=milestone.date,
        event_type=milestone.event_type,
        title=(
            milestone.title
            or _display_event_type(
                milestone.event_type
            )
        ),
        description=(
            milestone.description
            or ""
        ),
    )


# ==============================================================
# SCHEDULE WATCH
# ==============================================================

def build_schedule_watch(
    requests,
) -> list[ScheduleWatch]:
    watches = []

    for request in requests:

        # ------------------------------------------------------
        # No response
        # ------------------------------------------------------

        if not request.response_found:

            watches.append(
                ScheduleWatch(
                    kind="pending_sponsor_action",
                    request_accession=(
                        request.request_accession
                    ),
                    request_date=(
                        request.request_date
                    ),
                    estimated_due_date=(
                        request.estimated_due_date
                    ),
                    response_date=None,
                    detail=(
                        "FERC information request has "
                        "no explicitly matched sponsor response."
                    ),
                )
            )

            continue

        # ------------------------------------------------------
        # Response after mechanically estimated date
        #
        # Deliberately call this a WATCH rather than a delay.
        # ------------------------------------------------------

        if (
            request.response_timing
            == "after_estimated_due_date"
        ):

            watches.append(
                ScheduleWatch(
                    kind="deadline_timing_uncertain",
                    request_accession=(
                        request.request_accession
                    ),
                    request_date=(
                        request.request_date
                    ),
                    estimated_due_date=(
                        request.estimated_due_date
                    ),
                    response_date=(
                        request.first_response_date
                    ),
                    detail=(
                        "Sponsor response occurred after "
                        "the mechanically estimated due date. "
                        "This is not treated as a confirmed delay."
                    ),
                )
            )

    return watches


# ==============================================================
# PROGRESSION COUNTS
# ==============================================================

def count_progression_statuses(
    progressions,
) -> dict[str, int]:

    counts: dict[str, int] = {}

    for progression in progressions:

        status = (
            progression.progression_status
        )

        counts[status] = (
            counts.get(
                status,
                0,
            )
            + 1
        )

    return counts


# ==============================================================
# REGULATORY STATUS
# ==============================================================

def determine_regulatory_status(
    open_requests: int,
    progressions,
) -> str:

    if open_requests > 0:
        return "attention_required"

    if any(
        progression.progression_status
        == "adverse"
        for progression in progressions
    ):
        return "adverse_signal"

    if any(
        progression.progression_status
        in {
            "approved",
            "accepted",
            "progressed",
        }
        for progression in progressions
    ):
        return "progressing"

    if progressions:
        return "regulatory_activity_active"

    return "no_status_established"


# ==============================================================
# NEXT EXPECTED ACTIVITY
# ==============================================================

def determine_next_expected_activity(
    current_stage: str,
    open_requests: int,
    latest_milestone: Optional[OutlookMilestone],
    progressions,
) -> str:

    if open_requests > 0:
        return (
            "Sponsor response to outstanding FERC "
            "information request"
        )

    if current_stage.lower() == "construction":
        return (
            "Ongoing construction, compliance, and "
            "project-status reporting"
        )

    if latest_milestone is not None:

        if latest_milestone.event_type == (
            "environmental_milestone"
        ):
            return (
                "Further FERC environmental review or "
                "regulatory decision"
            )

        if latest_milestone.event_type in {
            "regulatory_decision",
            "construction_or_service_authorization",
            "major_construction_authorization",
        }:
            return (
                "Project activity associated with the "
                "next implementation stage"
            )

    if any(
        progression.progression_status
        == "further_information_requested"
        for progression in progressions
    ):
        return (
            "Sponsor response to additional FERC "
            "information request"
        )

    return (
        "Further regulatory or sponsor action not established"
    )


# ==============================================================
# INVESTOR SUMMARY
# ==============================================================

def build_investor_summary(
    current_stage: str,
    regulatory_status: str,
    tracked_requests: int,
    responded_requests: int,
    open_requests: int,
    latest_milestone: Optional[OutlookMilestone],
    schedule_watch: list[ScheduleWatch],
) -> str:

    parts = []

    # ----------------------------------------------------------
    # Current position
    # ----------------------------------------------------------

    parts.append(
        f"Project is currently in the "
        f"{current_stage} stage."
    )

    # ----------------------------------------------------------
    # FERC request status
    # ----------------------------------------------------------

    if tracked_requests == 0:

        parts.append(
            "No explicit FERC sponsor-directed "
            "information requests were identified."
        )

    elif open_requests == 0:

        parts.append(
            f"{tracked_requests} explicit FERC "
            f"information requests were identified; "
            f"all {responded_requests} have matched "
            f"sponsor responses."
        )

    else:

        parts.append(
            f"{open_requests} explicit FERC information "
            f"request(s) remain without a matched sponsor "
            f"response."
        )

    # ----------------------------------------------------------
    # Milestone
    # ----------------------------------------------------------

    if latest_milestone is not None:

        parts.append(
            "Latest material milestone: "
            f"{latest_milestone.title} on "
            f"{_date_text(latest_milestone.date)}."
        )

    # ----------------------------------------------------------
    # Schedule watch
    # ----------------------------------------------------------

    if schedule_watch:

        confirmed_pending = sum(
            1
            for watch in schedule_watch
            if watch.kind
            == "pending_sponsor_action"
        )

        uncertain_timing = sum(
            1
            for watch in schedule_watch
            if watch.kind
            == "deadline_timing_uncertain"
        )

        if confirmed_pending:
            parts.append(
                f"{confirmed_pending} outstanding "
                f"sponsor action(s) require attention."
            )

        if uncertain_timing:
            parts.append(
                f"{uncertain_timing} historical response "
                f"timing case(s) warrant review; they are "
                f"not treated as confirmed delays."
            )

    else:

        parts.append(
            "No current schedule-watch items were identified."
        )

    # ----------------------------------------------------------
    # Overall status
    # ----------------------------------------------------------

    if regulatory_status == "progressing":
        parts.append(
            "Available filing evidence indicates "
            "the regulatory process is progressing."
        )

    elif regulatory_status == "attention_required":
        parts.append(
            "Current filing evidence indicates that "
            "additional sponsor action may be required."
        )

    elif regulatory_status == "adverse_signal":
        parts.append(
            "An adverse regulatory signal was identified "
            "and requires review."
        )

    else:
        parts.append(
            "Available filings do not establish a "
            "clear current regulatory status."
        )

    return " ".join(
        parts
    )


# ==============================================================
# BUILD OUTLOOK
# ==============================================================

def build_regulatory_outlook(
    project: str,
    docket: str,
    current_stage: str,
    requests,
    progressions,
    milestones,
) -> RegulatoryOutlook:

    open_requests = count_open_requests(
        requests
    )

    tracked_requests = len(
        requests
    )

    responded_requests = sum(
        1
        for request in requests
        if request.response_found
    )

    latest_milestone = (
        find_latest_material_milestone(
            milestones
        )
    )

    schedule_watch = (
        build_schedule_watch(
            requests
        )
    )

    progression_counts = (
        count_progression_statuses(
            progressions
        )
    )

    regulatory_status = (
        determine_regulatory_status(
            open_requests,
            progressions,
        )
    )

    next_expected_activity = (
        determine_next_expected_activity(
            current_stage,
            open_requests,
            latest_milestone,
            progressions,
        )
    )

    investor_summary = (
        build_investor_summary(
            current_stage=current_stage,
            regulatory_status=(
                regulatory_status
            ),
            tracked_requests=(
                tracked_requests
            ),
            responded_requests=(
                responded_requests
            ),
            open_requests=(
                open_requests
            ),
            latest_milestone=(
                latest_milestone
            ),
            schedule_watch=(
                schedule_watch
            ),
        )
    )

    return RegulatoryOutlook(
        project=project,
        docket=docket,
        current_stage=current_stage,
        regulatory_status=(
            regulatory_status
        ),
        open_ferc_requests=(
            open_requests
        ),
        tracked_ferc_requests=(
            tracked_requests
        ),
        responded_ferc_requests=(
            responded_requests
        ),
        latest_material_milestone=(
            latest_milestone
        ),
        schedule_watch=(
            schedule_watch
        ),
        progression_counts=(
            progression_counts
        ),
        next_expected_activity=(
            next_expected_activity
        ),
        investor_summary=(
            investor_summary
        ),
    )