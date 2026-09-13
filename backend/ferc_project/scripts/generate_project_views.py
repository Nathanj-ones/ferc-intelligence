from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ferc_filter.activity_grouper import (
    ActivityRecord,
    extract_referenced_accessions,
    extract_referenced_dates,
    group_review_records,
)
from ferc_filter.elibrary import ELibraryClient
from ferc_filter.elibrary_enrichment import ELibraryEnrichmentClient
from ferc_filter.event_classifier import classify_event
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
        "name": "Southeast Compression Utility and Reliability",
        "docket": "CP25-219",
    },
]

START_DATE = "01-01-2024"
END_DATE = "09-08-2026"

OUTPUT_DIR = Path("data")


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
    result = []

    for record in records:
        accession = record.get("accession_no")

        if not accession:
            continue

        if accession in seen:
            continue

        seen.add(accession)
        result.append(record)

    return result


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
            value.replace("Z", "+00:00")
        )
    except ValueError:
        return None


def enrich_record(record):
    """
    Use the persistent enrichment cache before requesting
    enrichment from FERC.
    """

    accession = record.get("accession_no")

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
                cached_at=datetime.now(
                    timezone.utc
                ).isoformat(),
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
        date=parse_record_date(record),
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


# ==============================================================
# PROJECT GENERATOR
# ==============================================================

def generate_project_view(
    project_name,
    docket,
):
    print()
    print("=" * 90)
    print(project_name)
    print(docket)
    print("=" * 90)

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
    suppressed_count = 0
    regulatory_filings = []
    progression_filings = []

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

        accession = record.get("accession_no") or ""
        filed_date = parse_record_date(record)
        description = record.get("doc_desc") or ""
        event_type = decision.event_type or "unknown"

        regulatory_filings.append(
            RegulatoryFiling(
                accession=accession,
                date=filed_date,
                event_type=event_type,
                description=description,
            )
        )

        progression_filings.append(
            ProgressionFiling(
                accession=accession,
                date=filed_date,
                event_type=event_type,
                description=description,
            )
        )

        if decision.decision == "ALERT":
            alerts.append(
                {
                    "accession": record.get(
                        "accession_no"
                    ),
                    "date": parse_record_date(
                        record
                    ),
                    "event_type": (
                        decision.event_type
                    ),
                    "description": (
                        record.get("doc_desc")
                        or ""
                    ),
                }
            )

        elif decision.decision == "REVIEW":
            review_records.append(
                make_activity_record(
                    project_name=project_name,
                    docket=docket,
                    record=record,
                    enriched_record=(
                        enriched_record
                    ),
                    decision=decision,
                )
            )

        else:
            suppressed_count += 1

    activity_groups = (
        group_review_records(
            review_records
        )
    )

    view = build_project_view(
        project=project_name,
        docket=docket,
        raw_count=len(raw_records),
        deduped_count=len(records),
        alerts=alerts,
        review_count=len(
            review_records
        ),
        suppressed_count=(
            suppressed_count
        ),
        activity_groups=(
            activity_groups
        ),
    )

    requests = track_regulatory_requests(
        regulatory_filings
    )

    progressions = build_progressions_from_requests(
        requests,
        progression_filings,
    )

    view.regulatory_outlook = build_regulatory_outlook(
        project=project_name,
        docket=docket,
        current_stage=view.current_stage,
        requests=requests,
        progressions=progressions,
        milestones=view.milestones,
    )

    return view


# ==============================================================
# SAVE
# ==============================================================

def save_project_view(view):
    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    view_dict = project_view_to_dict(
        view
    )

    safe_docket = (
        view.docket.replace(
            "/",
            "_",
        )
    )

    output_path = (
        OUTPUT_DIR
        / f"project_view_{safe_docket}.json"
    )

    with output_path.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            view_dict,
            file,
            indent=2,
            ensure_ascii=False,
        )

    return output_path


# ==============================================================
# MAIN
# ==============================================================

def main():
    print()
    print("=" * 90)
    print("PROJECT VIEW FIXTURE GENERATOR")
    print("=" * 90)
    print()
    print(
        "Classifier: frozen v0.2"
    )
    print(
        "Generating dashboard fixtures..."
    )

    generated = []

    for project in PROJECTS:
        view = generate_project_view(
            project_name=project[
                "name"
            ],
            docket=project[
                "docket"
            ],
        )

        output_path = save_project_view(
            view
        )

        generated.append(
            (
                view,
                output_path,
            )
        )

        print()
        print(
            "Current stage:",
            view.current_stage,
        )

        print(
            "ALERT milestones:",
            view.counts.alerts,
        )

        print(
            "REVIEW filings:",
            view.counts.review,
        )

        print(
            "Activity groups:",
            view.counts.activity_groups,
        )

        print(
            "SUPPRESS filings:",
            view.counts.suppressed,
        )

        print(
            "Saved:",
            output_path,
        )

    # ----------------------------------------------------------
    # COMPACT FINAL SUMMARY
    # ----------------------------------------------------------

    print()
    print("=" * 90)
    print("GENERATED PROJECT VIEWS")
    print("=" * 90)

    for view, output_path in generated:
        print()
        print(
            f"{view.project} "
            f"({view.docket})"
        )

        print(
            f"Stage: "
            f"{view.current_stage}"
        )

        print(
            f"Milestones: "
            f"{len(view.milestones)}"
        )

        print(
            f"Activity groups: "
            f"{len(view.activity)}"
        )

        print(
            f"File: "
            f"{output_path}"
        )


if __name__ == "__main__":
    main()