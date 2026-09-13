from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence


@dataclass
class PendingRegulatoryAction:
    gate: str
    status: str

    initiating_accession: str
    initiating_party: str
    initiating_date: Optional[datetime]

    action_requested: str
    responsible_party: str

    requested_timing: Optional[datetime]
    timing_source: Optional[str]

    expected_resolution: str
    blocking_next_stage: bool

    timeline_signal: str
    confidence: str = "medium"


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def _is_construction_approval(description: str) -> bool:
    text = _normalize(description)
    return (
        "granting the" in text
        and (
            "commence construction" in text
            or "notice to proceed" in text
        )
    )


def _is_construction_request(description: str) -> bool:
    if _is_construction_approval(description):
        return False

    text = _normalize(description)
    return any(
        phrase in text
        for phrase in (
            "request for notice to proceed with construction",
            "request for notice to proceed",
            "request to commence construction",
        )
    )


def _parse_forward_timing(
    description: str,
) -> tuple[Optional[datetime], Optional[str]]:
    import re

    match = re.search(
        r"(?:by|before|on or before)\s+"
        r"(\d{1,2}/\d{1,2}/\d{4})",
        description or "",
        flags=re.IGNORECASE,
    )

    if not match:
        return None, None

    try:
        value = datetime.strptime(
            match.group(1),
            "%m/%d/%Y",
        )
    except ValueError:
        return None, None

    return value, "filing_text_requested_timing"



def _is_ferc_information_request(filing: dict) -> bool:
    event_type = str(filing.get("event_type") or "")
    text = _normalize(filing.get("description", ""))

    if event_type == "ferc_information_request":
        return True

    return (
        ("requesting" in text or "requests" in text)
        and ("information request" in text or "data request" in text)
        and ("file a response" in text or "provide" in text)
    )

def _filing_by_accession(
    filings: Sequence[dict],
    accession: str,
) -> Optional[dict]:
    for filing in filings:
        if str(filing.get("accession") or "") == accession:
            return filing
    return None


def find_pending_regulatory_actions(
    filings: list[dict],
    tracked_requests=None,
) -> list[PendingRegulatoryAction]:
    """
    Identify pending project-level regulatory actions.

    Construction authorization requests are evaluated directly from
    project filings because they are lifecycle-gate actions.

    FERC information-request obligations are NOT matched here. When
    `tracked_requests` is supplied, it is the single source of truth
    for whether a FERC request still awaits a sponsor response. This
    avoids duplicating Regulatory Tracker matching logic.

    For backward compatibility, the argument may be omitted; in that
    case only construction-gate actions are returned.
    """

    ordered = sorted(
        filings,
        key=lambda item: item.get("date") or datetime.min,
    )

    pending: list[PendingRegulatoryAction] = []

    for index, filing in enumerate(ordered):
        description = filing.get("description", "")

        if not _is_construction_request(description):
            continue

        accession = str(filing.get("accession") or "")
        date = filing.get("date")
        requested_timing, timing_source = _parse_forward_timing(
            description
        )

        resolved = False
        gate_info_request = None

        for later in ordered[index + 1:]:
            later_date = later.get("date")
            if later_date is None or date is None or later_date <= date:
                continue

            if _is_construction_approval(
                later.get("description", "")
            ):
                resolved = True
                break

            if (
                gate_info_request is None
                and _is_ferc_information_request(later)
            ):
                gate_info_request = later

        if resolved:
            continue

        if gate_info_request is not None:
            info_accession = str(
                gate_info_request.get("accession") or ""
            )
            info_date = gate_info_request.get("date")
            info_timing, info_timing_source = _parse_forward_timing(
                gate_info_request.get("description", "")
            )

            # If the validated tracker already has this request and a
            # response, do not leave the gate in awaiting-sponsor state.
            tracker_match = None
            if tracked_requests is not None:
                for request in tracked_requests:
                    if request.request_accession == info_accession:
                        tracker_match = request
                        break

            if tracker_match is None or not tracker_match.response_found:
                pending.append(
                    PendingRegulatoryAction(
                        gate="construction_authorization",
                        status="awaiting_sponsor_action",
                        initiating_accession=info_accession,
                        initiating_party="FERC",
                        initiating_date=info_date,
                        action_requested="additional_information",
                        responsible_party="applicant",
                        requested_timing=(
                            tracker_match.estimated_due_date
                            if tracker_match is not None
                            and tracker_match.estimated_due_date is not None
                            else info_timing
                        ),
                        timing_source=(
                            "tracker_estimated_due_date"
                            if tracker_match is not None
                            and tracker_match.estimated_due_date is not None
                            else info_timing_source
                        ),
                        expected_resolution=(
                            "Sponsor response to FERC information request"
                        ),
                        blocking_next_stage=True,
                        timeline_signal="pending_gate_awaiting_sponsor",
                        confidence="high",
                    )
                )
                continue

        pending.append(
            PendingRegulatoryAction(
                gate="construction_authorization",
                status="awaiting_ferc_action",
                initiating_accession=accession,
                initiating_party="applicant",
                initiating_date=date,
                action_requested="notice_to_proceed",
                responsible_party="FERC",
                requested_timing=requested_timing,
                timing_source=timing_source,
                expected_resolution=(
                    "FERC approval, further information request, "
                    "or other disposition"
                ),
                blocking_next_stage=True,
                timeline_signal=(
                    "pending_gate_with_requested_timing"
                    if requested_timing is not None
                    else "pending_gate"
                ),
                confidence="high",
            )
        )

    # Reuse Regulatory Tracker as the authoritative source for
    # outstanding sponsor obligations.
    if tracked_requests is not None:
        for request in tracked_requests:
            if request.response_found:
                continue

            pending.append(
                PendingRegulatoryAction(
                    gate="regulatory_information",
                    status="awaiting_sponsor_action",
                    initiating_accession=(
                        request.request_accession
                    ),
                    initiating_party="FERC",
                    initiating_date=request.request_date,
                    action_requested=(
                        "requested information / response"
                    ),
                    responsible_party="applicant",
                    requested_timing=(
                        request.estimated_due_date
                    ),
                    timing_source=(
                        "tracker_estimated_due_date"
                        if request.estimated_due_date is not None
                        else None
                    ),
                    expected_resolution=(
                        "Sponsor response or additional FERC action"
                    ),
                    blocking_next_stage=False,
                    timeline_signal="pending_sponsor_action",
                    confidence="high",
                )
            )

    return pending


def pending_action_to_dict(
    action: PendingRegulatoryAction,
) -> dict:
    return {
        "gate": action.gate,
        "status": action.status,
        "initiating_accession": action.initiating_accession,
        "initiating_party": action.initiating_party,
        "initiating_date": action.initiating_date,
        "action_requested": action.action_requested,
        "responsible_party": action.responsible_party,
        "requested_timing": action.requested_timing,
        "timing_source": action.timing_source,
        "expected_resolution": action.expected_resolution,
        "blocking_next_stage": action.blocking_next_stage,
        "timeline_signal": action.timeline_signal,
        "confidence": action.confidence,
    }
