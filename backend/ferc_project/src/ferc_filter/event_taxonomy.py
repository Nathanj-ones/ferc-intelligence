"""General regulatory event taxonomy for pre-COD FERC projects."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EventClassification:
    event_type: str
    stage: str
    significance: str
    decision: str
    reason: str


# ------------------------------------------------------------------
# HIGH-SIGNAL EVENTS
# These represent major regulatory/project milestones.
# ------------------------------------------------------------------

ALERT_EVENTS = {
    "certificate_order": {
        "patterns": [
            "order issuing certificate",
            "order issuing certificates",
            "order granting certificate",
            "certificate order",
        ],
        "stage": "Authorization",
        "significance": "Major regulatory approval",
    },

    "final_eis": {
        "patterns": [
            "final environmental impact statement",
        ],
        "stage": "Environmental Review",
        "significance": "Major environmental milestone",
    },

    "draft_eis": {
        "patterns": [
            "draft environmental impact statement",
        ],
        "stage": "Environmental Review",
        "significance": "Major environmental milestone",
    },

    "major_construction_authorization": {
        "patterns": [
            "proceed with construction of trains",
            "authorization to construct trains",
            "construct trains 4",
            "construct trains 5",
        ],
        "stage": "Construction",
        "significance": "Major construction authorization",
    },

    "extension_of_time": {
        "patterns": [
            "request for three-year extension of time",
            "request for extension of time",
            "extension of time until",
            "extension of time to complete",
            "extend the deadline",
            "extending the deadline",
        ],
        "stage": "Schedule / Authorization",
        "significance": "Material project timing change",
    },

    "in_service_authorization": {
        "patterns": [
            "authorization to place in service",
            "authorized to place in service",
            "authorization to commence service",
            "placed into service",
            "place it into service",
        ],
        "stage": "Commissioning / In Service",
        "significance": "Project reaches operating stage",
    },

    "construction_suspension": {
        "patterns": [
            "suspend construction",
            "suspension of construction",
            "stop work order",
            "cease construction",
        ],
        "stage": "Construction",
        "significance": "Material interruption to construction",
    },

    "cancellation_abandonment": {
        "patterns": [
            "cancel the project",
            "project cancellation",
            "abandon the project",
            "application to abandon",
        ],
        "stage": "Project Status",
        "significance": "Project cancellation or abandonment",
    },
}


# ------------------------------------------------------------------
# ROUTINE EVENTS
# High-confidence noise unless exceptional circumstances exist.
# ------------------------------------------------------------------

SUPPRESS_EVENTS = {
    "weekly_noise_report": [
        "weekly noise data report",
    ],

    "monthly_status_report": [
        "monthly status report",
    ],

    "routine_emergency_plan": [
        "quarterly update related to the emergency response plan",
    ],

    "scoping_transcript": [
        "transcripts of the",
        "public scoping meeting",
    ],

    "routine_intervention": [
        "motion to intervene",
        "notice granting interventions",
    ],

    "errata": [
        "errata notice",
        "errata to the",
    ],
}


# ------------------------------------------------------------------
# AMBIGUOUS EVENTS
# Need additional metadata/content/context.
# ------------------------------------------------------------------

REVIEW_EVENTS = {
    "data_request": [
        "data request",
        "environmental information request",
        "engineering information request",
    ],

    "variance": [
        "variance request",
        "design modification",
    ],

    "inspection": [
        "inspection report",
    ],

    "implementation_plan": [
        "implementation plan",
    ],

    "construction_authorization": [
        "request for authorization to construct",
        "request for authorization to install",
        "granting the",
    ],

    "rehearing": [
        "request for rehearing",
        "arguments raised on rehearing",
        "rehearing",
    ],

    "stay": [
        "motion for stay",
        "request for stay",
        "stay of construction",
    ],

    "court": [
        "court of appeals",
        "petition for review",
        "court related",
    ],

    "environmental_consultation": [
        "formal consultation",
        "tribal consultation",
        "fish and wildlife service",
    ],
}


def _contains(text: str, patterns: list[str]) -> str | None:
    """Return the first matching phrase."""

    text = text.lower()

    for pattern in patterns:
        if pattern in text:
            return pattern

    return None


def classify_event(record: dict) -> EventClassification:
    """
    Classify a FERC filing into a general regulatory event.

    Priority:
        ALERT -> SUPPRESS -> REVIEW -> unknown REVIEW

    The default remains REVIEW so an unfamiliar filing is never
    silently discarded.
    """

    description = (record.get("doc_desc") or "").lower()

    # --------------------------------
    # High-signal regulatory milestones
    # --------------------------------

    for event_type, config in ALERT_EVENTS.items():

        match = _contains(
            description,
            config["patterns"],
        )

        if match:
            return EventClassification(
                event_type=event_type,
                stage=config["stage"],
                significance=config["significance"],
                decision="ALERT",
                reason=f"Matched major event: '{match}'",
            )

    # ----------------
    # Routine activity
    # ----------------

    for event_type, patterns in SUPPRESS_EVENTS.items():

        match = _contains(description, patterns)

        if match:
            return EventClassification(
                event_type=event_type,
                stage="Routine / Compliance",
                significance="Low",
                decision="SUPPRESS",
                reason=f"Matched routine event: '{match}'",
            )

    # ------------------
    # Ambiguous activity
    # ------------------

    for event_type, patterns in REVIEW_EVENTS.items():

        match = _contains(description, patterns)

        if match:
            return EventClassification(
                event_type=event_type,
                stage="Requires Context",
                significance="Unknown",
                decision="REVIEW",
                reason=f"Matched ambiguous event: '{match}'",
            )

    # -----------------
    # Safety-first rule
    # -----------------

    return EventClassification(
        event_type="unknown",
        stage="Unknown",
        significance="Unknown",
        decision="REVIEW",
        reason="No known regulatory event matched.",
    )