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
from ferc_filter.elibrary import ELibraryClient
from ferc_filter.elibrary_enrichment import (
    ELibraryEnrichmentClient,
)
from ferc_filter.event_classifier import classify_event
from ferc_filter.project_lifecycle import (
    LifecycleFiling,
    build_project_lifecycle,
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
print("REAL PROJECT LIFECYCLE TEST")
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
    "View: FERC → enrichment → frozen classification "
    "→ project lifecycle"
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
# BUILD REAL PROJECT LIFECYCLE
# ==============================================================

lifecycle_filings = [
    LifecycleFiling(
        accession=item["accession"],
        date=item["date"],
        event_type=item["event_type"],
        description=item["description"],
    )
    for item in classified_filings
]

lifecycle = build_project_lifecycle(
    lifecycle_filings
)


# ==============================================================
# COMPACT OUTPUT
# ==============================================================

print()
print("=" * 100)
print("PROJECT LIFECYCLE")
print("=" * 100)
print()
print("Project:", PROJECT)
print("Docket:", DOCKET)
print(
    "Records:",
    len(records),
)

print()
print("Current stage:", lifecycle.current_stage)
print("Stage status:", lifecycle.stage_status)
print("Next gate:", lifecycle.next_gate)
print("Gate status:", lifecycle.gate_status)
print(
    "Expected next event:",
    lifecycle.expected_next_event,
)
print(
    "Schedule signal:",
    lifecycle.schedule_signal,
)
print("Confidence:", lifecycle.confidence)

print()
print("Evidence accessions:")
if lifecycle.evidence_accessions:
    for accession in lifecycle.evidence_accessions:
        print(f"- {accession}")
else:
    print("- none")

print()
print(
    "PASS | Real-project lifecycle generated"
)
