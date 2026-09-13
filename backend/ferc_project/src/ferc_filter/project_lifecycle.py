from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

@dataclass
class LifecycleFiling:
    accession: str
    date: Optional[datetime]
    event_type: str
    description: str

@dataclass
class ProjectLifecycle:
    current_stage: str
    stage_status: str
    next_gate: Optional[str]
    gate_status: str
    expected_next_event: str
    schedule_signal: str
    evidence_accessions: list[str] = field(default_factory=list)
    confidence: str = "medium"

CONSTRUCTION_AUTHORIZATION_TYPES = {
    "major_construction_authorization",
    "construction_or_service_authorization",
}

def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())

def _is_certificate(filing: LifecycleFiling) -> bool:
    if filing.event_type != "regulatory_decision":
        return False
    text = _normalize(filing.description)
    return any(p in text for p in (
        "issuing certificate",
        "certificate of public convenience",
        "certificate order",
        "issuing a certificate",
    ))

def _is_construction_authorization(filing: LifecycleFiling) -> bool:
    if filing.event_type in CONSTRUCTION_AUTHORIZATION_TYPES:
        return True
    text = _normalize(filing.description)
    return filing.event_type == "regulatory_decision" and any(
        p in text for p in (
            "commence construction",
            "construction authorization",
            "notice to proceed",
        )
    )

def _is_environmental_milestone(filing: LifecycleFiling) -> bool:
    return filing.event_type == "environmental_milestone"

def _is_implementation_signal(filing: LifecycleFiling) -> bool:
    text = _normalize(filing.description)
    return any(p in text for p in (
        "implementation plan",
        "request for notice to proceed",
        "request to proceed with construction",
        "request to commence construction",
        "limited notice to proceed",
    ))

def _latest(filings, predicate):
    matches = [f for f in filings if f.date is not None and predicate(f)]
    return max(matches, key=lambda f: f.date) if matches else None

def build_project_lifecycle(filings: list[LifecycleFiling]) -> ProjectLifecycle:
    construction = _latest(filings, _is_construction_authorization)
    certificate = _latest(filings, _is_certificate)
    environmental = _latest(filings, _is_environmental_milestone)

    if construction is not None:
        evidence = [construction.accession]
        if certificate is not None:
            evidence.append(certificate.accession)
        return ProjectLifecycle(
            current_stage="construction",
            stage_status="construction_authorized",
            next_gate="in_service",
            gate_status="pending",
            expected_next_event=(
                "Construction progress, compliance reporting, and eventual "
                "authorization to place facilities in service"
            ),
            schedule_signal="construction_underway_or_authorized",
            evidence_accessions=evidence,
            confidence="high",
        )

    if certificate is not None:
        implementation = sorted(
            [
                f for f in filings
                if f.date is not None
                and certificate.date is not None
                and f.date > certificate.date
                and _is_implementation_signal(f)
            ],
            key=lambda f: f.date,
        )
        evidence = [certificate.accession] + [
            f.accession for f in implementation[-3:]
        ]
        return ProjectLifecycle(
            current_stage="post_certificate_pre_construction",
            stage_status=(
                "certificate_granted_implementation_underway"
                if implementation else "certificate_granted"
            ),
            next_gate="construction_authorization",
            gate_status="pending",
            expected_next_event=(
                "FERC action on implementation filings or request for "
                "construction authorization / Notice to Proceed"
                if implementation else
                "Post-certificate implementation filings and request for "
                "construction authorization / Notice to Proceed"
            ),
            schedule_signal=(
                "awaiting_regulatory_action"
                if implementation else "pre_construction"
            ),
            evidence_accessions=evidence,
            confidence="high",
        )

    if environmental is not None:
        return ProjectLifecycle(
            current_stage="environmental_review",
            stage_status="environmental_review_complete",
            next_gate="certificate_decision",
            gate_status="pending",
            expected_next_event=(
                "FERC certificate decision or further environmental/regulatory action"
            ),
            schedule_signal="awaiting_certificate_decision",
            evidence_accessions=[environmental.accession],
            confidence="high",
        )

    return ProjectLifecycle(
        current_stage="application_or_unknown",
        stage_status="material_gate_not_established",
        next_gate="environmental_review",
        gate_status="not_established",
        expected_next_event="Further project-specific regulatory evidence",
        schedule_signal="insufficient_evidence",
        evidence_accessions=[],
        confidence="low",
    )

def project_lifecycle_to_dict(lifecycle: ProjectLifecycle) -> dict:
    return {
        "current_stage": lifecycle.current_stage,
        "stage_status": lifecycle.stage_status,
        "next_gate": lifecycle.next_gate,
        "gate_status": lifecycle.gate_status,
        "expected_next_event": lifecycle.expected_next_event,
        "schedule_signal": lifecycle.schedule_signal,
        "evidence_accessions": lifecycle.evidence_accessions,
        "confidence": lifecycle.confidence,
    }
