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
    "CP21-465": {
        "name": "Driftwood Line 200",
    },
    "CP17-101": {
        "name": "Northeast Supply Enhancement",
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
print("REAL PROJECT VIEW TEST")
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
    "View: FERC → classification → activity groups "
    "→ project view"
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
# GROUP
# ==============================================================

activity_groups = group_review_records(
    review_records
)


# ==============================================================
# BUILD PROJECT VIEW + REGULATORY OUTLOOK
# ==============================================================

view = build_project_view(
    project=PROJECT,
    docket=DOCKET,
    raw_count=len(raw_records),
    deduped_count=len(records),
    alerts=alerts,
    review_count=len(review_records),
    suppressed_count=suppressed_count,
    activity_groups=activity_groups,
)

regulatory_filings = [
    RegulatoryFiling(**item)
    for item in classified_filings
]
requests = track_regulatory_requests(regulatory_filings)

progression_filings = [
    ProgressionFiling(**item)
    for item in classified_filings
]
progressions = build_progressions_from_requests(
    requests,
    progression_filings,
)

regulatory_outlook = build_regulatory_outlook(
    project=PROJECT,
    docket=DOCKET,
    current_stage=view.current_stage,
    requests=requests,
    progressions=progressions,
    milestones=view.milestones,
)

# ProjectView v2 serializes this field.
view.regulatory_outlook = regulatory_outlook
view_dict = project_view_to_dict(view)


# ==============================================================
# COMPACT HUMAN-READABLE OUTPUT
# ==============================================================

print()
print("=" * 100)
print("PROJECT VIEW")
print("=" * 100)
print()
print("Project:", view.project)
print("Docket:", view.docket)
print("Current stage:", view.current_stage)

print()
print("=== COUNTS ===")
print("Raw:", view.counts.raw)
print("Deduped:", view.counts.deduped)
print("Alerts:", view.counts.alerts)
print("Review:", view.counts.review)
print("Suppressed:", view.counts.suppressed)
print("Activity groups:", view.counts.activity_groups)

print()
print("=== MATERIAL MILESTONES ===")
if not view.milestones:
    print("None")
else:
    for milestone in view.milestones:
        date_text = milestone.date.strftime("%Y-%m-%d") if milestone.date else "Unknown"
        print(f"{date_text} | {milestone.title} | {milestone.accession}")

print()
print("=== REGULATORY OUTLOOK ===")
print("Status:", regulatory_outlook.regulatory_status)
print("Tracked FERC requests:", regulatory_outlook.tracked_ferc_requests)
print("Responded FERC requests:", regulatory_outlook.responded_ferc_requests)
print("Open FERC requests:", regulatory_outlook.open_ferc_requests)
print("Schedule watch items:", len(regulatory_outlook.schedule_watch))
print("Next expected activity:", regulatory_outlook.next_expected_activity)

if regulatory_outlook.latest_material_milestone:
    latest = regulatory_outlook.latest_material_milestone
    latest_date = latest.date.strftime("%Y-%m-%d") if latest.date else "Unknown"
    print("Latest material milestone:", f"{latest_date} | {latest.title} | {latest.accession}")

print()
print("Investor summary:")
print(regulatory_outlook.investor_summary)

if regulatory_outlook.schedule_watch:
    print()
    print("Schedule watch:")
    for watch in regulatory_outlook.schedule_watch:
        print(f"- {watch.kind} | {watch.request_accession}")

print()
print("=== RECENT ACTIVITY ===")
for summary in view.activity[:MAX_ACTIVITY_GROUPS]:
    suffix = "" if summary.filing_count == 1 else "s"
    print(
        f"{format_date_range(summary.start_date, summary.end_date)} | "
        f"{summary.activity_type} | {summary.filing_count} filing{suffix} | "
        f"{summary.relationship}"
    )

remaining = len(view.activity) - MAX_ACTIVITY_GROUPS
if remaining > 0:
    print(f"... {remaining} older activity groups omitted.")

# ==============================================================
# SAVE JSON - ONE FILE PER DOCKET
# ==============================================================

output_dir = Path("data")
output_dir.mkdir(parents=True, exist_ok=True)
output_path = output_dir / f"project_view_{DOCKET}.json"

with output_path.open("w", encoding="utf-8") as output_file:
    json.dump(
        view_dict,
        output_file,
        indent=2,
        ensure_ascii=False,
        default=json_default,
    )

print()
print("Saved JSON:", output_path)
