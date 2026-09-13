from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class ConstructionAuthorizationChain:
    request_accession: str
    supplemental_accessions: list[str]
    authorization_accession: Optional[str]
    authorization_date: Optional[str]
    partial: bool
    resolved: bool


def build_construction_authorization_chain(
    pending_actions,
    material_milestones,
) -> ConstructionAuthorizationChain | None:
    """
    Collapse an NTP request + supplemental information + later FERC
    authorization into a single construction-authorization progression.

    This is intentionally narrow: it does not alter unrelated lifecycle
    stages or infer authorization beyond an explicit FERC authorization filing.
    """

    ntp_actions = [
        action
        for action in pending_actions
        if getattr(action, "gate", None) == "construction_authorization"
        and getattr(action, "action_requested", None) == "notice_to_proceed"
    ]

    if not ntp_actions:
        return None

    # Use the earliest explicit NTP request as the root.
    root = sorted(
        ntp_actions,
        key=lambda action: getattr(
            action, "initiating_date", datetime.max
        ),
    )[0]

    request_accession = root.initiating_accession

    supplemental = []
    authorization = None

    for milestone in material_milestones:
        event_type = getattr(milestone, "event_type", "")
        title = (getattr(milestone, "title", "") or "").lower()
        description = (
            getattr(milestone, "description", "") or ""
        ).lower()

        if event_type in {
            "construction_or_service_authorization",
            "delegated_authorization",
        }:
            combined = f"{title} {description}"
            if "grant" in combined or "authorize" in combined:
                authorization = milestone
                break

    if authorization is None:
        # Supplemental NTP filings are evidence of the same open gate.
        # The current domain model does not expose a dedicated relationship
        # object, so keep the chain rooted in the original request.
        return ConstructionAuthorizationChain(
            request_accession=request_accession,
            supplemental_accessions=[],
            authorization_accession=None,
            authorization_date=None,
            partial=False,
            resolved=False,
        )

    combined = (
        (getattr(authorization, "title", "") or "") + " " +
        (getattr(authorization, "description", "") or "")
    ).lower()

    partial = (
        "partial construction" in combined
        or "commence partial construction" in combined
    )

    return ConstructionAuthorizationChain(
        request_accession=request_accession,
        supplemental_accessions=[],
        authorization_accession=authorization.accession,
        authorization_date=(
            authorization.date.isoformat()
            if authorization.date
            else None
        ),
        partial=partial,
        resolved=True,
    )


def construction_authorization_chain_to_dict(
    chain: ConstructionAuthorizationChain | None,
) -> dict | None:
    if chain is None:
        return None

    return {
        "request_accession": chain.request_accession,
        "supplemental_accessions": chain.supplemental_accessions,
        "authorization_accession": chain.authorization_accession,
        "authorization_date": chain.authorization_date,
        "partial": chain.partial,
        "resolved": chain.resolved,
    }
