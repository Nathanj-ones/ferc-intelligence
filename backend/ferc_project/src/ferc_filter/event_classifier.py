"""Generalizable FERC project-event classifier."""

from __future__ import annotations

from dataclasses import dataclass

from .features import FilingFeatures, extract_features

from ferc_filter.elibrary_enrichment import ELibraryEnrichmentClient

@dataclass(frozen=True)
class EventDecision:
    """Classification result for a filing."""

    decision: str
    event_type: str
    stage: str
    reason: str
    features: FilingFeatures


def classify_event(record: dict) -> EventDecision:
    """
    Classify a FERC filing using the v0.2 decision policy.

    ALERT:
        Filing itself establishes a high-confidence material outcome.

    REVIEW:
        Filing may contain material information, but the metadata
        does not establish a material project change.

    SUPPRESS:
        High-confidence routine, recurring, or procedural activity.
    """

    features = extract_features(record)

    document_class = (
        features.document_class or ""
    ).lower()

    document_type = (
        features.document_type or ""
    ).lower()

    text = (
        record.get("doc_desc") or ""
    ).lower()

    # ============================================================
    # 1. HIGH-CONFIDENCE ROUTINE / PROCEDURAL ACTIVITY
    # ============================================================

    if (
        features.routine_signal
        and features.recurring_signal
        and features.action == "report"
    ):
        return EventDecision(
            decision="SUPPRESS",
            event_type="routine_reporting",
            stage="Routine / Compliance",
            reason="Recurring routine reporting.",
            features=features,
        )

    if (
        features.action == "transcript"
        and features.routine_signal
    ):
        return EventDecision(
            decision="SUPPRESS",
            event_type="routine_procedural_record",
            stage="Regulatory Process",
            reason="Routine conference or meeting transcript.",
            features=features,
        )

    if (
        "intervention" in document_class
        or "motion/notice of intervention" in document_type
        or "motion to intervene out of time" in document_type
    ):
        return EventDecision(
            decision="SUPPRESS",
            event_type="routine_intervention",
            stage="Regulatory Process",
            reason="Procedural intervention filing.",
            features=features,
        )

    if any(
        phrase in text
        for phrase in (
            "update service list",
            "update the service list",
            "removed from the service list",
            "removed from the mail list",
            "withdrawal of counsel",
        )
    ):
        return EventDecision(
            decision="SUPPRESS",
            event_type="administrative_update",
            stage="Regulatory Process",
            reason="Administrative or service-list activity.",
            features=features,
        )

    if (
        "errata" in text
        or "erratum" in text
        or "erroneously filed" in text
    ):
        return EventDecision(
            decision="SUPPRESS",
            event_type="procedural_correction",
            stage="Regulatory Process",
            reason="Procedural correction or erroneous filing.",
            features=features,
        )

    # ============================================================
    # 2. HIGH-CONFIDENCE MATERIAL OUTCOMES
    # ============================================================

    # Major FERC environmental milestone.
    if (
        features.actor == "ferc"
        and features.event_area == "environmental_review"
        and (
            "environmental assessment" in document_type
            or "environmental impact statement" in document_type
        )
        and "comment" not in document_class
    ):
        return EventDecision(
            decision="ALERT",
            event_type="environmental_milestone",
            stage="Environmental Review",
            reason="FERC issued a major environmental-review document.",
            features=features,
        )

    # Commission-level authorization/order.
    if (
        features.actor == "ferc"
        and features.action == "grant_or_issue"
        and "commission order/opinion" in document_type
        and features.event_area in {
            "authorization",
            "construction_or_service",
            "schedule",
        }
    ):
        return EventDecision(
            decision="ALERT",
            event_type="regulatory_decision",
            stage=features.event_area,
            reason="Commission order establishes a project regulatory outcome.",
            features=features,
        )

    # FERC authorization to commence/proceed with construction
    # or place facilities into service.
    if (
        features.actor == "ferc"
        and features.action == "grant_or_issue"
        and any(
            phrase in text
            for phrase in (
                "commence construction",
                "proceed with construction",
                "authorization to construct",
                "authorized to construct",
                "place in service",
                "commence service",
            )
        )
    ):
        return EventDecision(
            decision="ALERT",
            event_type="construction_or_service_authorization",
            stage="Construction / Service",
            reason="FERC authorized a major construction or service transition.",
            features=features,
        )

    # Actual material schedule/status outcome.
    if (
        features.actor == "ferc"
        and features.action == "grant_or_issue"
        and any(
            phrase in text
            for phrase in (
                "granting the",
                "granted the",
                "approving the",
                "denying the",
            )
        )
        and any(
            phrase in text
            for phrase in (
                "extension of time",
                "completion date",
                "in-service date",
                "place in service",
                "suspend construction",
                "suspension",
                "abandonment",
            )
        )
    ):
        return EventDecision(
            decision="ALERT",
            event_type="timing_or_status_change",
            stage="Project Status",
            reason="FERC action establishes a material timing or status change.",
            features=features,
        )

    # ============================================================
    # 3. POTENTIALLY MATERIAL — REVIEW
    # ============================================================

    # Applications and authorization requests are important, but
    # do not themselves establish that authorization was granted.
    if (
        "application/petition/request" in document_class
        or (
            features.actor == "applicant"
            and features.action == "request"
            and features.event_area in {
                "authorization",
                "construction_or_service",
            }
        )
    ):
        return EventDecision(
            decision="REVIEW",
            event_type="authorization_request",
            stage="Authorization",
            reason="Applicant request requires review; no completed regulatory outcome is established.",
            features=features,
        )

    # FERC information/data requests.
    if (
        features.actor == "ferc"
        and features.action == "request"
    ):
        return EventDecision(
            decision="REVIEW",
            event_type="ferc_information_request",
            stage="Regulatory Review",
            reason="FERC information request may indicate an issue requiring review.",
            features=features,
        )

    # Applicant responses/supplements.
    if (
        features.actor == "applicant"
        and (
            features.action == "response"
            or features.followup_signal
        )
    ):
        return EventDecision(
            decision="REVIEW",
            event_type="applicant_followup",
            stage="Regulatory Review",
            reason="Applicant response or supplemental filing may contain material new information.",
            features=features,
        )

    # Environmental comments should not automatically alert.
    if (
        features.event_area == "environmental_review"
        and features.actor == "third_party"
    ):
        return EventDecision(
            decision="REVIEW",
            event_type="environmental_comment",
            stage="Environmental Review",
            reason="Third-party environmental filing requires contextual review.",
            features=features,
        )

    # Legal challenge/rehearing/stay.
    if (
        features.event_area == "legal_or_challenge"
        or "rehearing" in document_type
        or "petition for review" in document_type
    ):
        return EventDecision(
            decision="REVIEW",
            event_type="legal_activity",
            stage="Legal / Regulatory",
            reason="Legal or rehearing activity requires assessment of project impact.",
            features=features,
        )

    # Delegated orders are not automatically material.
    if (
        features.actor == "ferc"
        and "delegated order" in document_type
    ):
        return EventDecision(
            decision="REVIEW",
            event_type="delegated_order",
            stage="Regulatory Process",
            reason="Delegated order requires scope assessment before treating it as material.",
            features=features,
        )

    # Inspection / compliance / implementation activity.
    if features.event_area in {
        "compliance",
        "scope_or_design_change",
        "construction_or_service",
    }:
        return EventDecision(
            decision="REVIEW",
            event_type="context_dependent",
            stage=features.event_area,
            reason="Project-related filing requires contextual materiality assessment.",
            features=features,
        )

    # General third-party comments/protests.
    if (
        features.actor == "third_party"
        or "comments/protest" in document_class
    ):
        return EventDecision(
            decision="REVIEW",
            event_type="third_party_comment",
            stage="Regulatory Process",
            reason="Third-party filing requires contextual review.",
            features=features,
        )

    # ============================================================
    # 4. CONSERVATIVE FALLBACK
    # ============================================================

    return EventDecision(
        decision="REVIEW",
        event_type="unknown",
        stage="Unknown",
        reason="No high-confidence material or routine rule matched.",
        features=features,
    )