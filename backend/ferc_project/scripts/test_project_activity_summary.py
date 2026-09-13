from __future__ import annotations

from datetime import datetime, timezone

from ferc_filter.activity_grouper import (
    ActivityRecord,
    extract_referenced_accessions,
    extract_referenced_dates,
    group_review_records,
)
from ferc_filter.activity_summarizer import (
    activity_type_label,
    summarize_activity_groups,
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

PROJECTS = [
    {
        "name": "Southeast Supply Enhancement",
        "docket": "CP25-10",
    },
    {
        "name": "Appalachian Reliability",
        "docket": "CP25-528",
    },
    {
        "name": (
            "Southeast Compression Utility "
            "and Reliability"
        ),
        "docket": "CP25-219",
    },
]

START_DATE = "01-01-2024"
END_DATE = "09-08-2026"

# Keep the console output readable.
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

    formats = (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m-%d-%Y",
    )

    for fmt in formats:

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
    """
    Reuse our persistent enrichment cache.
    """

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
    project_name,
    docket,
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
        project=project_name,
        docket=docket,
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


def format_date(value):
    if value is None:
        return "Unknown"

    return value.strftime(
        "%Y-%m-%d"
    )


def format_date_range(
    start_date,
    end_date,
):
    if (
        start_date is None
        and end_date is None
    ):
        return "Unknown date"

    if start_date == end_date:
        return format_date(
            start_date
        )

    return (
        f"{format_date(start_date)}"
        f" -> "
        f"{format_date(end_date)}"
    )


# ==============================================================
# RUN
# ==============================================================

print()
print("=" * 100)
print("PROJECT ACTIVITY SUMMARY")
print("=" * 100)
print()
print("Classifier: frozen v0.2")
print(
    "View: ALERT milestones + grouped REVIEW activity"
)


for project_config in PROJECTS:

    project_name = (
        project_config["name"]
    )

    docket = (
        project_config["docket"]
    )

    print()
    print("=" * 100)
    print(project_name.upper())
    print(docket)
    print("=" * 100)

    result = docket_client.get_docket(
        docket=docket,
        start_date=START_DATE,
        end_date=END_DATE,
    )

    raw_records = result.records

    records = deduplicate_records(
        raw_records
    )

    alerts = []
    review_records = []

    suppress_count = 0

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
                f"Processed "
                f"{index}/{total}..."
            )

        enriched_record = enrich_record(
            record
        )

        decision = classify_event(
            enriched_record
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
                    project_name=(
                        project_name
                    ),
                    docket=docket,
                    record=record,
                    enriched_record=(
                        enriched_record
                    ),
                    decision=decision,
                )
            )

        else:
            suppress_count += 1

    groups = group_review_records(
        review_records
    )

    summaries = (
        summarize_activity_groups(
            groups
        )
    )

    alerts.sort(
        key=lambda item: (
            item["date"]
            or datetime.min
        ),
        reverse=True,
    )

    # ----------------------------------------------------------
    # PROJECT COUNTS
    # ----------------------------------------------------------

    print()
    print("=== OVERVIEW ===")

    print(
        "Raw records:",
        len(raw_records),
    )

    print(
        "After dedup:",
        len(records),
    )

    print(
        "ALERT milestones:",
        len(alerts),
    )

    print(
        "REVIEW filings:",
        len(review_records),
    )

    print(
        "Activity groups:",
        len(groups),
    )

    print(
        "SUPPRESS filings:",
        suppress_count,
    )

    if review_records:

        reduction = (
            1
            - len(groups)
            / len(review_records)
        ) * 100

        print(
            f"REVIEW compression: "
            f"{reduction:.1f}%"
        )

    # ----------------------------------------------------------
    # ALERTS
    # ----------------------------------------------------------

    print()
    print("=== MATERIAL MILESTONES ===")

    if not alerts:

        print(
            "No ALERT milestones."
        )

    else:

        for alert in alerts:

            event_label = (
                alert[
                    "event_type"
                ]
                .replace(
                    "_",
                    " ",
                )
                .title()
            )

            print()
            print(
                f"{format_date(alert['date'])}"
                f" | "
                f"{event_label}"
            )

            print(
                f"Accession: "
                f"{alert['accession']}"
            )

            print(
                alert["description"]
            )

    # ----------------------------------------------------------
    # ACTIVITY GROUPS
    # ----------------------------------------------------------

    print()
    print("=== RECENT PROJECT ACTIVITY ===")

    if not summaries:

        print(
            "No REVIEW activity."
        )

    else:

        for summary in summaries[
            :MAX_ACTIVITY_GROUPS
        ]:

            print()
            print(
                activity_type_label(
                    summary.activity_type
                )
            )

            print(
                format_date_range(
                    summary.start_date,
                    summary.end_date,
                )
            )

            print(
                f"{summary.filing_count} "
                f"filing"
                f"{'s' if summary.filing_count != 1 else ''}"
                f" | "
                f"{summary.relationship}"
            )

            print(
                summary.summary
            )

            if (
                summary.representative_accessions
            ):

                print(
                    "Representative accessions:",
                    ", ".join(
                        summary
                        .representative_accessions
                    ),
                )

        remaining = (
            len(summaries)
            - MAX_ACTIVITY_GROUPS
        )

        if remaining > 0:

            print()
            print(
                f"... {remaining} older "
                f"activity groups omitted."
            )


print()
print("=" * 100)
print("END")
print("=" * 100)