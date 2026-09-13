from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone

from ferc_filter.activity_grouper import (
    ActivityRecord,
    extract_referenced_accessions,
    extract_referenced_dates,
    activity_family,
    group_review_records,
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

TOP_GROUPS = 12


# ==============================================================
# CLIENTS / STATE
# ==============================================================

docket_client = ELibraryClient()
enrichment_client = ELibraryEnrichmentClient()
state = MonitorState()


# ==============================================================
# HELPERS
# ==============================================================

def deduplicate_records(records):
    """
    Keep one record per accession number.
    """

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
    """
    Parse the best available filing / issuance date.
    """

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
    Reuse the SQLite enrichment cache where possible.
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
    project,
    docket,
    record,
    enriched_record,
    decision,
):
    """
    Convert one classifier REVIEW result into the
    reusable ActivityRecord expected by activity_grouper.
    """

    accession = record.get(
        "accession_no"
    )

    description = (
        record.get("doc_desc")
        or ""
    )

    activity_record = ActivityRecord(
        project=project,
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


def print_group(
    group,
    max_examples=4,
):
    """
    Print one activity group in a readable form.
    """

    items = group.records

    dates = [
        item.date
        for item in items
        if item.date is not None
    ]

    if dates:

        start = min(dates)
        end = max(dates)

        date_range = (
            f"{start:%Y-%m-%d}"
            f" -> "
            f"{end:%Y-%m-%d}"
        )

    else:
        date_range = "unknown"

    event_counts = Counter(
        item.event_type
        for item in items
    )

    print()
    print(
        f"{group.activity_type} | "
        f"{len(items)} filings | "
        f"{group.relationship} | "
        f"{date_range}"
    )

    print(
        "Event types:",
        ", ".join(
            f"{name}={count}"
            for name, count
            in event_counts.most_common()
        ),
    )

    for item in items[
        :max_examples
    ]:

        print(
            f"{item.accession} | "
            f"{item.document_class} -> "
            f"{item.document_type}"
        )

        print(
            item.description
        )

    if len(items) > max_examples:

        print(
            f"... +"
            f"{len(items) - max_examples}"
            f" more"
        )


# ==============================================================
# ANALYSIS
# ==============================================================

all_project_results = []


print()
print("=" * 100)
print("RELATIONSHIP-AWARE REVIEW ANALYSIS")
print("=" * 100)
print()
print(
    "Classifier: frozen v0.2"
)
print(
    "Grouping: src/ferc_filter/activity_grouper.py"
)
print(
    "No classifier rules are modified."
)


for project_config in PROJECTS:

    project_name = project_config[
        "name"
    ]

    docket = project_config[
        "docket"
    ]

    print()
    print("=" * 100)
    print(project_name)
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

    review_records = []

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

        if decision.decision != "REVIEW":
            continue

        review_records.append(
            make_activity_record(
                project=project_name,
                docket=docket,
                record=record,
                enriched_record=(
                    enriched_record
                ),
                decision=decision,
            )
        )

    groups = group_review_records(
        review_records
    )

    group_type_counts = Counter(
        group.activity_type
        for group in groups
    )

    group_type_filings = Counter()

    for group in groups:

        group_type_filings[
            group.activity_type
        ] += len(
            group.records
        )

    unlinked = sum(
        1
        for group in groups
        if (
            len(group.records)
            == 1
        )
        and group.relationship
        == "unlinked"
    )

    review_count = len(
        review_records
    )

    group_count = len(
        groups
    )

    reduction = (
        (
            1
            - group_count / review_count
        )
        * 100
        if review_count
        else 0
    )

    print()
    print(
        "=== RELATIONSHIP THREADS ==="
    )

    print(
        "REVIEW filings:",
        review_count,
    )

    print(
        "Activity groups:",
        group_count,
    )

    print(
        "Unlinked filings:",
        unlinked,
    )

    print(
        f"Potential workload reduction: "
        f"{reduction:.1f}%"
    )

    print()
    print(
        "=== GROUPS BY ACTIVITY TYPE ==="
    )

    for activity_type, group_count_for_type in (
        group_type_counts.most_common()
    ):

        filing_count = (
            group_type_filings[
                activity_type
            ]
        )

        type_reduction = (
            (
                1
                - group_count_for_type
                / filing_count
            )
            * 100
            if filing_count
            else 0
        )

        print(
            f"{activity_type:<28} | "
            f"{filing_count:4} filings | "
            f"{group_count_for_type:3} groups | "
            f"{type_reduction:5.1f}% reduction"
        )

    print()
    print(
        "=== LARGEST ACTIVITY GROUPS ==="
    )

    largest_groups = sorted(
        groups,
        key=lambda group: len(
            group.records
        ),
        reverse=True,
    )[:TOP_GROUPS]

    for group in largest_groups:
        print_group(
            group
        )

    all_project_results.append(
        {
            "project": project_name,
            "docket": docket,
            "review_count": review_count,
            "group_count": group_count,
            "unlinked": unlinked,
            "groups": groups,
        }
    )


# ==============================================================
# COMBINED RESULTS
# ==============================================================

total_review = sum(
    result["review_count"]
    for result in all_project_results
)

total_groups = sum(
    result["group_count"]
    for result in all_project_results
)

total_unlinked = sum(
    result["unlinked"]
    for result in all_project_results
)

overall_reduction = (
    (
        1
        - total_groups / total_review
    )
    * 100
    if total_review
    else 0
)


print()
print("=" * 100)
print("COMBINED RELATIONSHIP ANALYSIS")
print("=" * 100)

print()
print(
    "Total REVIEW filings:",
    total_review,
)

print(
    "Total activity groups:",
    total_groups,
)

print(
    "Total unlinked filings:",
    total_unlinked,
)

print(
    f"Potential workload reduction: "
    f"{overall_reduction:.1f}%"
)


combined_activity_filings = Counter()
combined_activity_groups = Counter()
combined_relationships = Counter()


for result in all_project_results:

    for group in result["groups"]:

        combined_activity_filings[
            group.activity_type
        ] += len(
            group.records
        )

        combined_activity_groups[
            group.activity_type
        ] += 1

        combined_relationships[
            group.relationship
        ] += 1


print()
print(
    "=== COMBINED RELATIONSHIP TYPES ==="
)

for relationship, count in (
    combined_relationships.most_common()
):

    print(
        f"{count:4} | {relationship}"
    )


print()
print(
    "=== COMBINED GROUPS BY ACTIVITY TYPE ==="
)

for activity_type, filing_count in (
    combined_activity_filings.most_common()
):

    group_count = (
        combined_activity_groups[
            activity_type
        ]
    )

    reduction = (
        (
            1
            - group_count / filing_count
        )
        * 100
        if filing_count
        else 0
    )

    print(
        f"{activity_type:<28} | "
        f"{filing_count:4} filings | "
        f"{group_count:3} groups | "
        f"{reduction:5.1f}% reduction"
    )


print()
print(
    "=== LARGEST COMBINED GROUPS ==="
)

all_groups = [
    group
    for result in all_project_results
    for group in result["groups"]
]

largest_combined = sorted(
    all_groups,
    key=lambda group: len(
        group.records
    ),
    reverse=True,
)[:TOP_GROUPS]


for group in largest_combined:

    print()
    print(
        f"{group.project} | "
        f"{group.activity_type} | "
        f"{len(group.records)} filings | "
        f"{group.relationship}"
    )

    for item in group.records[
        :4
    ]:

        print(
            f"  {item.accession} | "
            f"{item.description}"
        )

    if len(group.records) > 4:

        print(
            f"  ... +"
            f"{len(group.records) - 4}"
            f" more"
        )