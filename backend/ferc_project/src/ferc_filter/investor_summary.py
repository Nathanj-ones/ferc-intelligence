from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ferc_filter.project_lifecycle import ProjectLifecycle
from ferc_filter.regulatory_outlook import RegulatoryOutlook
from ferc_filter.pending_regulatory_action import PendingRegulatoryAction


@dataclass
class InvestorSummary:
    current_stage: str
    stage_status: str
    next_gate: Optional[str]
    gate_status: str
    regulatory_health: str
    open_ferc_requests: int
    tracked_ferc_requests: int
    responded_ferc_requests: int
    schedule_watch_items: int
    pending_regulatory_actions: int
    blocking_pending_actions: int
    timeline_signal: str
    next_expected_event: str
    investor_summary: str
    key_evidence_accessions: list[str] = field(default_factory=list)
    confidence: str = "medium"


def _stage_label(stage: str) -> str:
    labels = {
        "construction": "Construction",
        "post_certificate_pre_construction": "Post-Certificate / Pre-Construction",
        "environmental_review": "Environmental Review",
        "application_or_unknown": "Application / Unknown",
        "in_service": "In Service",
    }
    return labels.get(
        stage,
        stage.replace("_", " ").title(),
    )


def _gate_label(gate: Optional[str]) -> str:
    if gate is None:
        return "Not established"
    labels = {
        "construction_authorization": "Construction Authorization",
        "in_service": "In-Service Authorization",
        "certificate_decision": "Certificate Decision",
        "environmental_review": "Environmental Review",
    }
    return labels.get(
        gate,
        gate.replace("_", " ").title(),
    )


def _gate_sentence(
    gate: Optional[str],
    gate_status: str,
) -> str:
    label = _gate_label(gate)
    if gate_status == "pending":
        return f"Next gate: {label} is pending."
    if gate_status == "complete":
        return f"Next gate: {label} is complete."
    return f"Next gate: {label}."


def build_investor_summary(
    *,
    lifecycle: ProjectLifecycle,
    outlook: RegulatoryOutlook,
    pending_actions: Optional[list[PendingRegulatoryAction]] = None,
) -> InvestorSummary:
    """Combine lifecycle position and regulatory health into a compact investor view."""

    schedule_watch_count = len(outlook.schedule_watch)
    pending_actions = pending_actions or []
    blocking_actions = [
        action for action in pending_actions
        if action.blocking_next_stage
    ]
    stage_label = _stage_label(lifecycle.current_stage)

    parts: list[str] = [
        f"Project is in the {stage_label} stage",
        f"with lifecycle status {lifecycle.stage_status.replace('_', ' ')}.",
    ]

    if lifecycle.next_gate:
        if lifecycle.gate_status == "pending":
            parts.append(
                f"The next gate is {_gate_label(lifecycle.next_gate)}, "
                "which is still pending."
            )
        else:
            parts.append(
                f"The next gate is {_gate_label(lifecycle.next_gate)}."
            )

    if blocking_actions:
        action = blocking_actions[0]
        if action.status == "awaiting_ferc_action":
            sentence = (
                f"FERC action is pending on {action.action_requested.replace('_', ' ')} "
                f"filed on {action.initiating_date.strftime('%Y-%m-%d') if action.initiating_date else 'an unknown date'}; "
                "this action blocks progression through the next project gate."
            )
            if action.requested_timing is not None:
                sentence += (
                    f" The filing requests action by "
                    f"{action.requested_timing.strftime('%Y-%m-%d')}; "
                    "this is requested timing, not treated as a FERC-imposed deadline."
                )
            parts.append(sentence)

    if outlook.open_ferc_requests:
        parts.append(
            f"{outlook.open_ferc_requests} FERC request(s) remain open, "
            "so sponsor action may be required."
        )
    elif outlook.tracked_ferc_requests:
        parts.append(
            f"All {outlook.tracked_ferc_requests} tracked FERC requests "
            "have matched sponsor responses."
        )

    if schedule_watch_count:
        parts.append(
            f"{schedule_watch_count} historical timing watch item(s) "
            "warrant review; these are not treated as confirmed delays."
        )

    parts.append(
        f"Regulatory health is {outlook.regulatory_status.replace('_', ' ')}."
    )

    return InvestorSummary(
        current_stage=lifecycle.current_stage,
        stage_status=lifecycle.stage_status,
        next_gate=lifecycle.next_gate,
        gate_status=lifecycle.gate_status,
        regulatory_health=outlook.regulatory_status,
        open_ferc_requests=outlook.open_ferc_requests,
        tracked_ferc_requests=outlook.tracked_ferc_requests,
        responded_ferc_requests=outlook.responded_ferc_requests,
        schedule_watch_items=schedule_watch_count,
        pending_regulatory_actions=len(pending_actions),
        blocking_pending_actions=len(blocking_actions),
        timeline_signal=(
            blocking_actions[0].timeline_signal
            if blocking_actions
            else lifecycle.schedule_signal
        ),
        next_expected_event=(
            blocking_actions[0].expected_resolution
            if blocking_actions
            else lifecycle.expected_next_event
        ),
        investor_summary=" ".join(parts),
        key_evidence_accessions=list(lifecycle.evidence_accessions),
        confidence=lifecycle.confidence,
    )


def investor_summary_to_dict(
    summary: InvestorSummary,
) -> dict:
    return {
        "current_stage": summary.current_stage,
        "stage_status": summary.stage_status,
        "next_gate": summary.next_gate,
        "gate_status": summary.gate_status,
        "regulatory_health": summary.regulatory_health,
        "open_ferc_requests": summary.open_ferc_requests,
        "tracked_ferc_requests": summary.tracked_ferc_requests,
        "responded_ferc_requests": summary.responded_ferc_requests,
        "schedule_watch_items": summary.schedule_watch_items,
        "pending_regulatory_actions": summary.pending_regulatory_actions,
        "blocking_pending_actions": summary.blocking_pending_actions,
        "timeline_signal": summary.timeline_signal,
        "next_expected_event": summary.next_expected_event,
        "investor_summary": summary.investor_summary,
        "key_evidence_accessions": summary.key_evidence_accessions,
        "confidence": summary.confidence,
    }
