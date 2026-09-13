from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from ferc_filter.elibrary import ELibraryClient
from ferc_filter.elibrary_enrichment import (
    ELibraryEnrichmentClient,
)
from ferc_filter.event_classifier import classify_event
from ferc_filter.state import MonitorState


# ==============================================================
# CONFIG
# ==============================================================

PROJECT = "Southeast Supply Enhancement"
DOCKET = "CP25-10"

START_DATE = "01-01-2024"
END_DATE = "09-08-2026"


# ==============================================================
# HIGH-VALUE EVENT TYPES
# ==============================================================

IMPORTANT_EVENT_TYPES = {
    "ferc_information_request",
    "applicant_followup",
    "environmental_milestone",
    "environmental_comment",
    "regulatory_decision",
    "major_construction_authorization",
    "construction_or_service_authorization",
    "authorization_request",
    "delegated_order",
    "timing_or_status_change",
    "context_dependent",
}


# ==============================================================
# KEYWORD GROUPS
# ==============================================================

DEADLINE_TERMS = (
    "within ",
    "by ",
    "deadline",
    "due ",
    "days",
    "business days",
    "calendar days",
    "response due",
    "respond within",
)

REQUEST_TERMS = (
    "request",
    "requests",
    "requesting",
    "data request",
    "information request",
    "environmental information request",
    "additional information",
    "supplemental information",
    "supplement",
    "clarification",
    "clarifications",
)

DECISION_TERMS = (
    "order",
    "decision",
    "approved",
    "approving",
    "approval",
    "denied",
    "denial",
    "reject",
    "rejected",
    "accept",
    "accepted",
    "acceptance",
    "granted",
    "granting",
    "dismissed",
    "dismissal",
)

CONSTRUCTION_TERMS = (
    "notice to proceed",
    "commence construction",
    "commence construction activities",
    "construction",
    "construction activities",
    "construction authorization",
    "limited notice to proceed",
    "lnTP",
    "ntp",
)

ENVIRONMENTAL_TERMS = (
    "environmental assessment",
    "environmental impact statement",
    "environmental information",
    "environmental review",
    "environmental issues",
    "scoping",
    "biological assessment",
    "cultural resource",
)

SCHEDULE_TERMS = (
    "schedule",
    "scheduled",
    "delay",
    "delayed",
    "extension",
    "extend",
    "extended",
    "status",
    "anticipated",
    "expected",
    "deadline",
    "milestone",
)

ACCEPTANCE_TERMS = (
    "acceptance",
    "accepted",
    "approve",
    "approved",
    "approval",
    "grant",
    "granted",
)

REJECTION_TERMS = (
    "reject",
    "rejected",
    "denied",
    "denial",
    "not accepted",
    "does not accept",
)

FOLLOWUP_TERMS = (
    "response to",
    "in response to",
    "responds to",
    "supplement to",
    "supplemental to",
    "reply to",
)


# ==============================================================
# CLIENTS
# ==============================================================

docket_client = ELibraryClient()
enrichment_client = ELibraryEnrichmentClient()
state = MonitorState()


# ==============================================================
# HELPERS
# ==============================================================

def parse_record_date(record):
    value = (
        record.get("filed_date")
        or record.get("filedDate")
        or record.get("issued_date")
        or record.get("issuedDate")
    )

    if not value:
        return None

    value = str(value).strip()

    for fmt in (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m-%d-%Y",
    ):
        try:
            return datetime.strptime(
                value[:10],
                fmt,
            )
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00",
            )
        )
    except ValueError:
        return None


def enrich_record(record):
    accession = record.get(
        "accession_no"
    )

    if not accession:
        return dict(record)

    enriched = state.get_enrichment(
        accession
    )

    if enriched is None:

        enriched = (
            enrichment_client.get_by_accession(
                accession
            )
        )

        if enriched is not None:

            state.save_enrichment(
                accession=accession,
                enriched=enriched,
                cached_at=(
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                ),
            )

    result = dict(record)

    if enriched is None:
        return result

    class_types = enriched.get(
        "classTypes",
        [],
    )

    if class_types:

        primary = class_types[0]

        result[
            "document_class"
        ] = primary.get(
            "documentClass"
        )

        result[
            "document_type"
        ] = primary.get(
            "documentType"
        )

    result[
        "enriched_class_types"
    ] = class_types

    result[
        "availability_code"
    ] = enriched.get(
        "availCode"
    )

    return result


def normalize_text(text):
    return (
        text or ""
    ).lower().replace(
        "\n",
        " ",
    )


def contains_any(
    text,
    terms,
):
    return any(
        term in text
        for term in terms
    )


def extract_signal_categories(
    event_type,
    description,
):
    """
    Identify why a filing is interesting for the
    regulatory-status investigation.

    These are diagnostic tags only. They do NOT change
    the v0.2 classifier decision.
    """

    text = normalize_text(
        description
    )

    signals = []

    if event_type in IMPORTANT_EVENT_TYPES:
        signals.append(
            "important_event_type"
        )

    if contains_any(
        text,
        DEADLINE_TERMS,
    ):
        signals.append(
            "deadline_or_timing"
        )

    if contains_any(
        text,
        REQUEST_TERMS,
    ):
        signals.append(
            "request_information"
        )

    if contains_any(
        text,
        DECISION_TERMS,
    ):
        signals.append(
            "decision_language"
        )

    if contains_any(
        text,
        CONSTRUCTION_TERMS,
    ):
        signals.append(
            "construction"
        )

    if contains_any(
        text,
        ENVIRONMENTAL_TERMS,
    ):
        signals.append(
            "environmental"
        )

    if contains_any(
        text,
        SCHEDULE_TERMS,
    ):
        signals.append(
            "schedule_status"
        )

    if contains_any(
        text,
        ACCEPTANCE_TERMS,
    ):
        signals.append(
            "acceptance"
        )

    if contains_any(
        text,
        REJECTION_TERMS,
    ):
        signals.append(
            "rejection"
        )

    if contains_any(
        text,
        FOLLOWUP_TERMS,
    ):
        signals.append(
            "followup"
        )

    # Remove duplicates while preserving order.
    return list(
        dict.fromkeys(
            signals
        )
    )


def classify_actor(
    event_type,
    description,
):
    """
    Diagnostic actor classification.

    Deliberately simple and conservative:
        FERC
        Applicant / Sponsor
        Third Party
        Unknown

    This is not a classifier replacement.
    """

    text = normalize_text(
        description
    )

    if event_type == "ferc_information_request":
        return "FERC"

    if event_type in {
        "regulatory_decision",
        "environmental_milestone",
        "delegated_order",
        "construction_or_service_authorization",
        "major_construction_authorization",
    }:
        return "FERC"

    if event_type == "applicant_followup":
        return "Applicant / Sponsor"

    if event_type == "authorization_request":
        return "Applicant / Sponsor"

    if (
        "transcontinental gas pipe line company"
        in text
    ):
        if any(
            phrase in text
            for phrase in (
                "submits",
                "requests",
                "files",
                "filed by",
                "responds",
            )
        ):
            return "Applicant / Sponsor"

    if any(
        phrase in text
        for phrase in (
            "comment of ",
            "comments of ",
            "comments from ",
            "protest of ",
            "motion of ",
            "petition of ",
        )
    ):
        return "Third Party"

    return "Unknown"


def classify_status_signal(
    event_type,
    description,
):
    """
    Give a very conservative status signal.

    This intentionally does not claim that a filing means
    the project was accepted/rejected. It only identifies
    explicit language worth reviewing.
    """

    text = normalize_text(
        description
    )

    if contains_any(
        text,
        REJECTION_TERMS,
    ):
        return "explicit_rejection_language"

    if contains_any(
        text,
        (
            "approved",
            "approving",
            "acceptance",
            "accepted",
            "granted",
            "granting",
        ),
    ):
        return "explicit_positive_action_language"

    if event_type == "ferc_information_request":
        return "information_requested"

    if event_type == "applicant_followup":
        return "response_submitted"

    return "no_explicit_status"


def is_high_value(
    event_type,
    description,
):
    """
    Determine whether a filing belongs in the diagnostic
    timeline.
    """

    if event_type in IMPORTANT_EVENT_TYPES:
        return True

    text = normalize_text(
        description
    )

    diagnostic_terms = (
        DEADLINE_TERMS
        + REQUEST_TERMS
        + DECISION_TERMS
        + CONSTRUCTION_TERMS
        + SCHEDULE_TERMS
    )

    return contains_any(
        text,
        diagnostic_terms,
    )


def print_filing(
    filing,
):
    print()
    print("-" * 100)

    print(
        filing["date"]
        or "UNKNOWN DATE"
    )

    print(
        f"{filing['accession']} | "
        f"{filing['event_type']} | "
        f"{filing['actor']}"
    )

    print(
        "Status signal:",
        filing["status_signal"],
    )

    print(
        "Signals:",
        ", ".join(
            filing["signals"]
        )
        if filing["signals"]
        else "none",
    )

    print(
        filing["description"]
    )


# ==============================================================
# MAIN
# ==============================================================

def main():

    print()
    print("=" * 100)
    print("REGULATORY STATUS ANALYSIS")
    print("=" * 100)
    print()
    print(
        f"Project: {PROJECT}"
    )
    print(
        f"Docket: {DOCKET}"
    )
    print(
        f"Window: {START_DATE} -> {END_DATE}"
    )
    print()
    print(
        "Classifier: frozen v0.2"
    )
    print(
        "Purpose: diagnostic timeline only"
    )
    print(
        "No classifier or ProjectView rules are modified."
    )

    result = docket_client.get_docket(
        docket=DOCKET,
        start_date=START_DATE,
        end_date=END_DATE,
    )

    raw_records = result.records

    print()
    print(
        f"Raw records retrieved: "
        f"{len(raw_records)}"
    )

    filings = []

    for index, record in enumerate(
        raw_records,
        start=1,
    ):

        if (
            index % 50 == 0
            or index == len(raw_records)
        ):
            print(
                f"Processed "
                f"{index}/{len(raw_records)}..."
            )

        enriched = enrich_record(
            record
        )

        decision = classify_event(
            enriched
        )

        description = (
            record.get(
                "doc_desc"
            )
            or ""
        )

        event_type = (
            decision.event_type
            or "unknown"
        )

        if not is_high_value(
            event_type,
            description,
        ):
            continue

        date = parse_record_date(
            record
        )

        filings.append(
            {
                "accession": (
                    record.get(
                        "accession_no"
                    )
                ),
                "date": (
                    date.strftime(
                        "%Y-%m-%d"
                    )
                    if date
                    else None
                ),
                "event_type": event_type,
                "decision": (
                    decision.decision
                ),
                "actor": classify_actor(
                    event_type,
                    description,
                ),
                "status_signal": (
                    classify_status_signal(
                        event_type,
                        description,
                    )
                ),
                "signals": (
                    extract_signal_categories(
                        event_type,
                        description,
                    )
                ),
                "description": description,
            }
        )

    filings.sort(
        key=lambda item: (
            item["date"]
            or "9999-99-99"
        )
    )

    # ==========================================================
    # SUMMARY
    # ==========================================================

    print()
    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)

    print()
    print(
        "High-value filings:",
        len(filings),
    )

    decision_counts = Counter(
        filing["decision"]
        for filing in filings
    )

    print()
    print(
        "By classifier decision:"
    )

    for decision, count in (
        decision_counts.most_common()
    ):
        print(
            f"{decision:<10} | {count}"
        )

    actor_counts = Counter(
        filing["actor"]
        for filing in filings
    )

    print()
    print(
        "By actor:"
    )

    for actor, count in (
        actor_counts.most_common()
    ):
        print(
            f"{actor:<20} | {count}"
        )

    event_counts = Counter(
        filing["event_type"]
        for filing in filings
    )

    print()
    print(
        "By event type:"
    )

    for event_type, count in (
        event_counts.most_common()
    ):
        print(
            f"{event_type:<40} | {count}"
        )

    signal_counts = Counter()

    for filing in filings:
        signal_counts.update(
            filing["signals"]
        )

    print()
    print(
        "By diagnostic signal:"
    )

    for signal, count in (
        signal_counts.most_common()
    ):
        print(
            f"{signal:<32} | {count}"
        )

    # ==========================================================
    # TIMELINE
    # ==========================================================

    print()
    print("=" * 100)
    print("REGULATORY TIMELINE")
    print("=" * 100)

    for filing in filings:
        print_filing(
            filing
        )

    # ==========================================================
    # REQUEST / RESPONSE CANDIDATES
    # ==========================================================

    requests = [
        filing
        for filing in filings
        if filing["event_type"]
        == "ferc_information_request"
    ]

    responses = [
        filing
        for filing in filings
        if filing["event_type"]
        == "applicant_followup"
    ]

    print()
    print("=" * 100)
    print("FERC REQUEST / APPLICANT RESPONSE CANDIDATES")
    print("=" * 100)

    print()

    if not requests:
        print(
            "No FERC information requests found."
        )
    else:

        for request in requests:

            print()
            print(
                f"{request['date']} | "
                f"{request['accession']}"
            )

            print(
                "REQUEST:"
            )

            print(
                request["description"]
            )

            # Find responses occurring after the request.
            request_date = (
                request["date"]
            )

            candidates = []

            for response in responses:

                if (
                    response["date"]
                    and request_date
                    and response["date"]
                    >= request_date
                ):
                    candidates.append(
                        response
                    )

            if candidates:

                print(
                    "POTENTIAL FOLLOW-UPS:"
                )

                for candidate in candidates[
                    :5
                ]:
                    print(
                        f"  {candidate['date']} | "
                        f"{candidate['accession']} | "
                        f"{candidate['description']}"
                    )

            else:

                print(
                    "POTENTIAL FOLLOW-UPS: none found"
                )

    # ==========================================================
    # DECISION / STATUS CANDIDATES
    # ==========================================================

    print()
    print("=" * 100)
    print("DECISION / STATUS CANDIDATES")
    print("=" * 100)

    decision_filings = [
        filing
        for filing in filings
        if (
            filing["status_signal"]
            in {
                "explicit_rejection_language",
                "explicit_positive_action_language",
            }
            or filing["event_type"]
            in {
                "regulatory_decision",
                "major_construction_authorization",
                "construction_or_service_authorization",
                "delegated_order",
            }
        )
    ]

    if not decision_filings:

        print(
            "No decision/status candidates found."
        )

    else:

        for filing in decision_filings:

            print()
            print(
                f"{filing['date']} | "
                f"{filing['accession']}"
            )

            print(
                f"Event: "
                f"{filing['event_type']}"
            )

            print(
                f"Status signal: "
                f"{filing['status_signal']}"
            )

            print(
                filing["description"]
            )


    print()
    print("=" * 100)
    print("END")
    print("=" * 100)


if __name__ == "__main__":
    main()