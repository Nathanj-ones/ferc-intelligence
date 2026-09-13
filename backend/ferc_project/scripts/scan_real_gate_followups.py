from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import sys
from pathlib import Path

from ferc_filter.activity_grouper import (
    ActivityRecord,
    extract_referenced_accessions,
    extract_referenced_dates,
    group_review_records,
)
from ferc_filter.project_view import (
    build_project_view,
    project_view_to_dict,
)
from ferc_filter.regulatory_tracker import (
    RegulatoryFiling,
    track_regulatory_requests,
)
from ferc_filter.regulatory_progression import (
    ProgressionFiling,
    build_progressions_from_requests,
)
from ferc_filter.regulatory_outlook import (
    build_regulatory_outlook,
)
from ferc_filter.project_lifecycle import (
    LifecycleFiling,
    build_project_lifecycle,
)
from ferc_filter.investor_summary import (
    build_investor_summary,
)
from ferc_filter.elibrary import ELibraryClient
from ferc_filter.elibrary_enrichment import (
    ELibraryEnrichmentClient,
)
from ferc_filter.event_classifier import classify_event
from ferc_filter.project_lifecycle import (
    LifecycleFiling,
    build_project_lifecycle,
)
from ferc_filter.investor_summary import (
    build_investor_summary,
)
from ferc_filter.pending_regulatory_action import (
    find_pending_regulatory_actions,
)
from ferc_filter.state import MonitorState


# ==============================================================
# CONFIG
# ==============================================================

PROJECTS = {
    "CP25-10": {
        "name": "Southeast Supply Enhancement",
    },
    "CP25-528": {
        "name": "Appalachian Reliability",
    },
    "CP25-219": {
        "name": "Southeast Compression Utility and Reliability",
    },
    "CP17-14": {
        "name": "Temple Truck Rack Expansion",
    },
}


def get_project_config():
    if len(sys.argv) < 2:
        print()
        print("Usage:")
        print("  python scripts\\test_real_project_view.py <DOCKET>")
        print()
        print("Available projects:")

        for docket, config in PROJECTS.items():
            print(f"  {docket:<10} {config['name']}")

        raise SystemExit(1)

    docket = sys.argv[1].strip().upper()

    # Allow convenient input such as "CP25 528".
    docket = docket.replace(" ", "-")

    if docket not in PROJECTS:
        print()
        print(f"Unknown docket: {docket}")
        print()
        print("Available projects:")

        for known_docket, config in PROJECTS.items():
            print(f"  {known_docket:<10} {config['name']}")

        raise SystemExit(1)

    return PROJECTS[docket]["name"], docket


PROJECT, DOCKET = get_project_config()

if DOCKET == "CP17-14":
    # Historical validation window for the 2019 implementation /
    # Notice-to-Proceed sequence.
    START_DATE = "01-01-2019"
    END_DATE = "12-31-2019"
else:
    START_DATE = "01-01-2024"
    END_DATE = "09-08-2026"

MAX_ACTIVITY_GROUPS = 15


# ==============================================================
# CLIENTS
# ==============================================================

docket_client = ELibraryClient()
enrichment_client = ELibraryEnrichmentClient()
state = MonitorState()


# ==============================================================
# HELPERS
# ==============================================================

def format_date_range(start_date, end_date):
    if start_date is None and end_date is None:
        return "Unknown"

    if start_date == end_date:
        return start_date.strftime("%Y-%m-%d")

    start_text = (
        start_date.strftime("%Y-%m-%d")
        if start_date
        else "Unknown"
    )

    end_text = (
        end_date.strftime("%Y-%m-%d")
        if end_date
        else "Unknown"
    )

    return f"{start_text} -> {end_text}"

def deduplicate_records(records):
    seen = set()
    deduped = []

    for record in records:

        accession = record.get(
            "accession_no"
        )

        if not accession:
            continue

        if accession in seen:
            continue

        seen.add(accession)
        deduped.append(record)

    return deduped


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

    enriched_record = dict(record)

    if enriched is None:
        return enriched_record

    class_types = enriched.get(
        "classTypes",
        [],
    )

    if class_types:

        primary = class_types[0]

        enriched_record[
            "document_class"
        ] = primary.get(
            "documentClass"
        )

        enriched_record[
            "document_type"
        ] = primary.get(
            "documentType"
        )

    enriched_record[
        "enriched_class_types"
    ] = class_types

    enriched_record[
        "availability_code"
    ] = enriched.get(
        "availCode"
    )

    return enriched_record


def make_activity_record(
    record,
    enriched_record,
    decision,
):
    accession = record.get(
        "accession_no"
    )

    description = (
        record.get("doc_desc")
        or ""
    )

    activity_record = ActivityRecord(
        project=PROJECT,
        docket=DOCKET,
        accession=accession,
        date=parse_record_date(
            record
        ),
        event_type=decision.event_type,
        description=description,
        document_class=(
            enriched_record.get(
                "document_class"
            )
        ),
        document_type=(
            enriched_record.get(
                "document_type"
            )
        ),
    )

    activity_record.referenced_accessions = (
        extract_referenced_accessions(
            description,
            accession,
        )
    )

    activity_record.referenced_dates = (
        extract_referenced_dates(
            description
        )
    )

    return activity_record


def json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()

    raise TypeError(
        f"Object of type {type(value).__name__} "
        "is not JSON serializable"
    )


# ==============================================================
# RETRIEVE
# ==============================================================

print()
print("=" * 100)
print("REAL GATE FOLLOW-UP CANDIDATE SCAN")
print("=" * 100)
print()
print(
    f"Project: {PROJECT}"
)
print(
    f"Docket: {DOCKET}"
)
print(
    "Classifier: frozen v0.2"
)
print(
    "Scan: construction-gate requests → later FERC information requests"
)


result = docket_client.get_docket(
    docket=DOCKET,
    start_date=START_DATE,
    end_date=END_DATE,
)


raw_records = result.records

records = deduplicate_records(
    raw_records
)


# ==============================================================
# CLASSIFY
# ==============================================================

alerts = []
review_records = []
classified_filings = []
suppressed_count = 0

total = len(records)

for index, record in enumerate(
    records,
    start=1,
):

    if (
        index % 25 == 0
        or index == total
    ):
        print(
            f"Processed {index}/{total}..."
        )

    enriched_record = enrich_record(
        record
    )

    decision = classify_event(
        enriched_record
    )

    classified_filings.append(
        {
            "accession": record.get("accession_no") or "",
            "date": parse_record_date(record),
            "event_type": decision.event_type or "unknown",
            "description": record.get("doc_desc") or "",
        }
    )

    if decision.decision == "ALERT":

        alerts.append(
            {
                "accession": (
                    record.get(
                        "accession_no"
                    )
                ),
                "date": (
                    parse_record_date(
                        record
                    )
                ),
                "event_type": (
                    decision.event_type
                ),
                "description": (
                    record.get(
                        "doc_desc"
                    )
                    or ""
                ),
            }
        )

    elif decision.decision == "REVIEW":

        review_records.append(
            make_activity_record(
                record=record,
                enriched_record=(
                    enriched_record
                ),
                decision=decision,
            )
        )

    else:
        suppressed_count += 1



# ==============================================================
# FIND REAL GREY-AREA CANDIDATES
# ==============================================================

def norm(value):
    return " ".join((value or "").lower().split())


def is_gate_approval(item):
    text = norm(item["description"])
    return (
        ("granting" in text or "approving" in text)
        and (
            "notice to proceed" in text
            or "commence construction" in text
            or "construction" in text
        )
    )


def is_gate_request(item):
    """
    Mirror the validated pending-action rule: an approval containing
    quoted request language must never become a new sponsor request.
    """
    if is_gate_approval(item):
        return False

    text = norm(item["description"])
    return any(
        phrase in text
        for phrase in (
            "request for notice to proceed with construction",
            "request for notice to proceed",
            "request to commence construction",
            "request to proceed with construction",
        )
    )


ordered = sorted(
    classified_filings,
    key=lambda item: item["date"] or datetime.min,
)

# Regulatory Tracker is the authoritative source for genuine FERC
# sponsor-directed information requests. Do not trust classifier
# event_type alone for this scan.
regulatory_filings = [
    RegulatoryFiling(**item)
    for item in classified_filings
]
tracked_requests = track_regulatory_requests(
    regulatory_filings
)

tracked_by_accession = {
    request.request_accession: request
    for request in tracked_requests
}

filings_by_accession = {
    item["accession"]: item
    for item in ordered
}

candidates = []

for i, gate in enumerate(ordered):
    if not is_gate_request(gate):
        continue

    gate_date = gate["date"]
    if gate_date is None:
        continue

    approval_date = None
    for later in ordered[i + 1:]:
        if later["date"] is None or later["date"] <= gate_date:
            continue
        if is_gate_approval(later):
            approval_date = later["date"]
            break

    intervening = []

    for request in tracked_requests:
        request_date = request.request_date
        if request_date is None or request_date <= gate_date:
            continue

        if approval_date is not None and request_date >= approval_date:
            continue

        filing = filings_by_accession.get(
            request.request_accession
        )
        if filing is None:
            continue

        intervening.append(
            (request, filing)
        )

    if intervening:
        candidates.append(
            (gate, approval_date, intervening)
        )


print()
print("=" * 100)
print("REAL GATE FOLLOW-UP CANDIDATES")
print("=" * 100)

if not candidates:
    print()
    print(
        "No validated FERC sponsor information request was identified "
        "between a construction-gate request and its approval/resolution "
        "in this docket."
    )
else:
    for gate, approval_date, followups in candidates:
        print()
        print(
            f"GATE REQUEST | {gate['date'].strftime('%Y-%m-%d')} | "
            f"{gate['accession']}"
        )
        print(gate["description"])

        if approval_date is not None:
            print(
                "Gate approval date:",
                approval_date.strftime("%Y-%m-%d"),
            )
        else:
            print("Gate approval date: not identified")

        for request, filing in followups:
            print()
            print(
                f"  VALIDATED FERC FOLLOW-UP | "
                f"{request.request_date.strftime('%Y-%m-%d')} | "
                f"{request.request_accession}"
            )
            print(
                "  Tracker response found:",
                request.response_found,
            )
            print(
                "  Estimated due:",
                (
                    request.estimated_due_date.strftime("%Y-%m-%d")
                    if request.estimated_due_date
                    else "-"
                ),
            )
            print("  ", filing["description"])

print()
print("Candidate count:", len(candidates))
print("END")
