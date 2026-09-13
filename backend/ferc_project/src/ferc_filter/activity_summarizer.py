from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ferc_filter.activity_grouper import ActivityGroup


# ==============================================================
# USER-FACING LABELS
# ==============================================================

EVENT_LABELS = {
    "ferc_information_request": "FERC information request",
    "applicant_followup": "applicant response / follow-up",
    "third_party_comment": "stakeholder comment",
    "environmental_comment": "environmental comment",
    "authorization_request": "authorization request",
    "legal_activity": "legal filing",
    "delegated_order": "delegated order",
    "context_dependent": "project implementation filing",
    "unknown": "other project filing",
}


ACTIVITY_TYPE_LABELS = {
    "information_exchange": (
        "Regulatory Information Exchange"
    ),
    "stakeholder_activity": (
        "Stakeholder Activity"
    ),
    "environmental_review": (
        "Environmental Review"
    ),
    "construction_compliance": (
        "Construction / Compliance"
    ),
    "authorization": (
        "Authorization Activity"
    ),
    "legal": (
        "Legal Activity"
    ),
    "delegated_authorization": (
        "Delegated Authorization"
    ),
    "administrative": (
        "Administrative Activity"
    ),
    "project_context": (
        "Project Context"
    ),
    "other": (
        "Other Activity"
    ),
}


# ==============================================================
# DATA MODEL
# ==============================================================

@dataclass
class ActivitySummary:
    """
    Dashboard-facing summary of one activity group.

    The summary remains traceable to the underlying filing
    accessions through representative_accessions.
    """

    project: str
    docket: str
    activity_type: str
    start_date: Optional[datetime]
    end_date: Optional[datetime]
    filing_count: int
    event_types: dict[str, int]
    relationship: str
    representative_accessions: list[str]
    summary: str


# ==============================================================
# FORMATTING HELPERS
# ==============================================================

def _filing_word(count: int) -> str:
    """
    Return singular/plural filing wording.
    """

    return (
        "filing"
        if count == 1
        else "filings"
    )


def _format_date(
    value: Optional[datetime],
) -> str:
    if value is None:
        return "Unknown"

    return value.strftime(
        "%d %b %Y"
    )


def _date_range(
    group: ActivityGroup,
) -> tuple[
    Optional[datetime],
    Optional[datetime],
]:
    dates = [
        record.date
        for record in group.records
        if record.date is not None
    ]

    if not dates:
        return None, None

    return min(dates), max(dates)


def _event_label(
    event_type: str,
) -> str:
    return EVENT_LABELS.get(
        event_type,
        event_type.replace(
            "_",
            " ",
        ),
    )


def _event_type_text(
    counts: Counter,
) -> str:
    """
    Convert internal event types to readable text.
    """

    parts = []

    for event_type, count in (
        counts.most_common()
    ):
        label = _event_label(
            event_type
        )

        if count == 1:
            parts.append(label)

        else:
            # Special-case wording so we don't create
            # awkward strings such as "response / follow-ups".
            if event_type == "applicant_followup":
                parts.append(
                    f"{count} applicant "
                    f"response / follow-ups"
                )
            else:
                parts.append(
                    f"{count} {label}"
                    f"{'s' if not label.endswith('s') else ''}"
                )

    if not parts:
        return "project activity"

    if len(parts) == 1:
        return parts[0]

    if len(parts) == 2:
        return (
            f"{parts[0]} and {parts[1]}"
        )

    return (
        ", ".join(parts[:-1])
        + f", and {parts[-1]}"
    )


# ==============================================================
# GROUP SUMMARIES
# ==============================================================

def _summary_for_group(
    group: ActivityGroup,
    event_counts: Counter,
) -> str:
    """
    Generate deterministic dashboard-facing text.

    This deliberately uses only the classifier/grouping data;
    no speculative interpretation or LLM-generated claims.
    """

    activity_type = (
        group.activity_type
    )

    filing_count = len(
        group.records
    )

    filing_word = _filing_word(
        filing_count
    )

    start_date, end_date = (
        _date_range(group)
    )

    if start_date == end_date:
        date_text = _format_date(
            start_date
        )

    else:
        date_text = (
            f"{_format_date(start_date)}"
            f" to "
            f"{_format_date(end_date)}"
        )

    # ----------------------------------------------------------
    # INFORMATION EXCHANGE
    # ----------------------------------------------------------

    if activity_type == "information_exchange":

        requests = sum(
            1
            for record in group.records
            if record.event_type
            == "ferc_information_request"
        )

        responses = sum(
            1
            for record in group.records
            if record.event_type
            == "applicant_followup"
        )

        parts = []

        if requests:
            parts.append(
                f"{requests} FERC "
                f"information request"
                f"{'s' if requests != 1 else ''}"
            )

        if responses:
            parts.append(
                f"{responses} applicant "
                f"response"
                f"{'s' if responses != 1 else ''}"
            )

        if parts:

            if len(parts) == 2:
                detail = (
                    f"{parts[0]} and "
                    f"{parts[1]}"
                )
            else:
                detail = parts[0]

        else:
            detail = _event_type_text(
                event_counts
            )

        return (
            f"Information exchange during "
            f"{date_text}: "
            f"{filing_count} {filing_word}, "
            f"including {detail}."
        )

    # ----------------------------------------------------------
    # STAKEHOLDER ACTIVITY
    # ----------------------------------------------------------

    if activity_type == "stakeholder_activity":

        comment_count = event_counts.get(
            "third_party_comment",
            filing_count,
        )

        return (
            f"Stakeholder activity during "
            f"{date_text}: "
            f"{comment_count} stakeholder "
            f"comment"
            f"{'s' if comment_count != 1 else ''}."
        )

    # ----------------------------------------------------------
    # ENVIRONMENTAL REVIEW
    # ----------------------------------------------------------

    if activity_type == "environmental_review":

        return (
            f"Environmental-review activity "
            f"during {date_text}: "
            f"{filing_count} {filing_word}."
        )

    # ----------------------------------------------------------
    # CONSTRUCTION / COMPLIANCE
    # ----------------------------------------------------------

    if activity_type == "construction_compliance":

        return (
            f"Construction/compliance activity "
            f"during {date_text}: "
            f"{filing_count} {filing_word} "
            f"related to project implementation "
            f"and compliance."
        )

    # ----------------------------------------------------------
    # AUTHORIZATION
    # ----------------------------------------------------------

    if activity_type == "authorization":

        return (
            f"Authorization activity during "
            f"{date_text}: "
            f"{filing_count} {filing_word}."
        )

    # ----------------------------------------------------------
    # LEGAL
    # ----------------------------------------------------------

    if activity_type == "legal":

        return (
            f"Legal activity during "
            f"{date_text}: "
            f"{filing_count} {filing_word}."
        )

    # ----------------------------------------------------------
    # DELEGATED AUTHORIZATION
    # ----------------------------------------------------------

    if activity_type == (
        "delegated_authorization"
    ):

        return (
            f"Delegated authorization activity "
            f"on {date_text}: "
            f"{filing_count} {filing_word}."
        )

    # ----------------------------------------------------------
    # ADMINISTRATIVE
    # ----------------------------------------------------------

    if activity_type == "administrative":

        return (
            f"Administrative activity during "
            f"{date_text}: "
            f"{filing_count} {filing_word}."
        )

    # ----------------------------------------------------------
    # PROJECT CONTEXT
    # ----------------------------------------------------------

    if activity_type == "project_context":

        return (
            f"Project context during "
            f"{date_text}: "
            f"{filing_count} {filing_word}."
        )

    # ----------------------------------------------------------
    # OTHER
    # ----------------------------------------------------------

    return (
        f"Project activity during "
        f"{date_text}: "
        f"{filing_count} {filing_word}."
    )


# ==============================================================
# PUBLIC SUMMARY FUNCTIONS
# ==============================================================

def summarize_activity_group(
    group: ActivityGroup,
    representative_limit: int = 3,
) -> ActivitySummary:
    """
    Convert one ActivityGroup into a dashboard-ready summary.
    """

    start_date, end_date = (
        _date_range(group)
    )

    event_counts = Counter(
        record.event_type
        for record in group.records
    )

    representative_accessions = [
        record.accession
        for record in group.records[
            :representative_limit
        ]
    ]

    summary = _summary_for_group(
        group,
        event_counts,
    )

    return ActivitySummary(
        project=group.project,
        docket=group.docket,
        activity_type=group.activity_type,
        start_date=start_date,
        end_date=end_date,
        filing_count=len(
            group.records
        ),
        event_types=dict(
            event_counts
        ),
        relationship=group.relationship,
        representative_accessions=(
            representative_accessions
        ),
        summary=summary,
    )


def summarize_activity_groups(
    groups: list[ActivityGroup],
    representative_limit: int = 3,
) -> list[ActivitySummary]:
    """
    Convert activity groups to dashboard-facing summaries.

    Latest activity appears first.
    """

    summaries = [
        summarize_activity_group(
            group,
            representative_limit=(
                representative_limit
            ),
        )
        for group in groups
    ]

    summaries.sort(
        key=lambda summary: (
            summary.start_date
            or datetime.min
        ),
        reverse=True,
    )

    return summaries


def activity_type_label(
    activity_type: str,
) -> str:
    """
    Convert internal activity types into dashboard labels.
    """

    return ACTIVITY_TYPE_LABELS.get(
        activity_type,
        activity_type.replace(
            "_",
            " ",
        ).title(),
    )