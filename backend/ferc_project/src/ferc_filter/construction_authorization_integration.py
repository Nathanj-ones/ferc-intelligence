from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any


def _text(filing: dict[str, Any]) -> str:
    return str(filing.get("description") or "").lower()


def _is_ntp_request(filing: dict[str, Any]) -> bool:
    return "request for notice to proceed" in _text(filing)


def _is_partial_authorization(filing: dict[str, Any]) -> bool:
    text = _text(filing)
    return "granting" in text and "partial construction" in text


def _is_construction_authorization(filing: dict[str, Any]) -> bool:
    text = _text(filing)
    return (
        ("granting" in text or "authoriz" in text)
        and (
            "commence construction" in text
            or "commence partial construction" in text
            or "notice to proceed" in text
        )
    )


def analyze_construction_authorization_chain(
    filings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    ordered = sorted(
        filings,
        key=lambda item: item.get("date") or datetime.min,
    )

    requests = [f for f in ordered if _is_ntp_request(f)]
    if not requests:
        return None

    root = requests[0]
    root_date = root.get("date")

    supplemental = []
    authorization = None

    for filing in ordered:
        if (
            root_date is not None
            and filing.get("date") is not None
            and filing.get("date") <= root_date
        ):
            continue

        text = _text(filing)
        if (
            "supplemental information" in text
            and "request for notice to proceed" in text
        ):
            accession = filing.get("accession") or ""
            if accession:
                supplemental.append(accession)
            continue

        if _is_construction_authorization(filing):
            authorization = filing
            break

    return {
        "request_accession": root.get("accession") or "",
        "request_accessions": [
            f.get("accession") or ""
            for f in requests
            if f.get("accession")
        ],
        "supplemental_accessions": supplemental,
        "authorization_accession": (
            authorization.get("accession")
            if authorization else None
        ),
        "authorization_date": (
            authorization.get("date")
            if authorization else None
        ),
        "partial": (
            _is_partial_authorization(authorization)
            if authorization else False
        ),
        "resolved": authorization is not None,
    }


def filter_resolved_construction_actions(
    pending_actions,
    chain: dict[str, Any] | None,
):
    if not chain or not chain.get("resolved"):
        return pending_actions

    resolved_accessions = set(
        chain.get("request_accessions") or []
    ) | set(
        chain.get("supplemental_accessions") or []
    )

    return [
        action
        for action in pending_actions
        if not (
            getattr(action, "gate", None) == "construction_authorization"
            and getattr(action, "initiating_accession", None)
            in resolved_accessions
        )
    ]


def apply_construction_chain_to_lifecycle(
    lifecycle,
    chain: dict[str, Any] | None,
):
    if not chain or not chain.get("resolved"):
        return lifecycle

    if chain.get("partial"):
        return replace(
            lifecycle,
            current_stage="construction",
            stage_status="partial_construction_authorized",
            next_gate="remaining_construction_authorization",
            gate_status="pending",
            expected_next_event=(
                "Further FERC authorization or project implementation activity"
            ),
            schedule_signal="implementation_underway",
        )

    return replace(
        lifecycle,
        current_stage="construction",
        stage_status="construction_authorized",
        next_gate="in_service_authorization",
        gate_status="pending",
        expected_next_event=(
            "Construction progress and eventual authorization to place "
            "facilities in service"
        ),
        schedule_signal="implementation_underway",
    )
