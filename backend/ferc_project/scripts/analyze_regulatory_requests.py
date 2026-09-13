from __future__ import annotations

from datetime import datetime, timezone

from ferc_filter.elibrary import ELibraryClient
from ferc_filter.elibrary_enrichment import (
    ELibraryEnrichmentClient,
)
from ferc_filter.event_classifier import classify_event
from ferc_filter.regulatory_tracker import (
    RegulatoryFiling,
    track_regulatory_requests,
)
from ferc_filter.state import MonitorState
from ferc_filter.regulatory_progression import (
    ProgressionFiling,
    build_progressions_from_requests,
)

# ==============================================================
# CONFIG
# ==============================================================

PROJECT = "Southeast Supply Enhancement"
DOCKET = "CP25-10"

START_DATE = "01-01-2024"
END_DATE = "09-08-2026"


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
            pass

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


def format_date(value):
    if value is None:
        return "-"

    return value.strftime(
        "%Y-%m-%d"
    )


def format_type(
    request_type,
):
    labels = {
        "environmental_information": (
            "environmental info"
        ),
        "data_request": (
            "data request"
        ),
        "information_request": (
            "information request"
        ),
        "other_information_request": (
            "other request"
        ),
    }

    return labels.get(
        request_type,
        request_type,
    )


def timing_label(
    timing,
):
    labels = {
        "on_or_before_estimated_due_date": (
            "ON TIME"
        ),
        "after_estimated_due_date": (
            "AFTER EST. DUE DATE"
        ),
        "no_response": (
            "NO RESPONSE"
        ),
        "response_found_due_date_unknown": (
            "RESPONSE / NO DUE DATE"
        ),
        "unknown": (
            "UNKNOWN"
        ),
    }

    return labels.get(
        timing,
        timing.upper(),
    )


# ==============================================================
# RETRIEVE
# ==============================================================

print()
print("=" * 100)
print("CP25-10 REGULATORY REQUEST TRACKER")
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

print(
    "Classifier: frozen v0.2"
)

print(
    "Tracker: explicit FERC request / sponsor response matching"
)

print(
    "No classifier rules modified."
)


result = docket_client.get_docket(
    docket=DOCKET,
    start_date=START_DATE,
    end_date=END_DATE,
)


raw_records = result.records

print()
print(
    "Raw records:",
    len(raw_records),
)


# ==============================================================
# CLASSIFY
# ==============================================================

regulatory_filings = []

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

    event_type = (
        decision.event_type
        or "unknown"
    )

    regulatory_filings.append(
        RegulatoryFiling(
            accession=(
                record.get(
                    "accession_no"
                )
                or ""
            ),
            date=parse_record_date(
                record
            ),
            event_type=event_type,
            description=(
                record.get(
                    "doc_desc"
                )
                or ""
            ),
        )
    )


# ==============================================================
# TRACK
# ==============================================================

requests = track_regulatory_requests(
    regulatory_filings
)

# ==============================================================
# BUILD PROGRESSION INPUT FROM ALL FILINGS
# ==============================================================

progression_filings = []

for record in raw_records:

    enriched = enrich_record(
        record
    )

    decision = classify_event(
        enriched
    )

    progression_filings.append(
        ProgressionFiling(
            accession=(
                record.get(
                    "accession_no"
                )
                or ""
            ),
            date=parse_record_date(
                record
            ),
            event_type=(
                decision.event_type
                or "unknown"
            ),
            description=(
                record.get(
                    "doc_desc"
                )
                or ""
            ),
        )
    )


# ==============================================================
# BUILD PROGRESSIONS
# ==============================================================

progressions = (
    build_progressions_from_requests(
        requests,
        progression_filings,
    )
)

# ==============================================================
# COMPACT TABLE
# ==============================================================

print()
print("=" * 100)
print("REGULATORY PROGRESSION")
print("=" * 100)

print()

header = (
    f"{'REQUEST':<12} "
    f"{'RESPONDED':<11} "
    f"{'DISPOSITION':<18} "
    f"{'PROGRESSION':<28} "
    f"{'SCHEDULE SIGNAL':<35}"
)

print(header)
print("-" * len(header))

for progression in progressions:

    responded = (
        "YES"
        if progression.response_accessions
        else "NO"
    )

    disposition = (
        progression.explicit_disposition
        or "-"
    )

    print(
        f"{(
            progression.request_date.strftime('%Y-%m-%d')
            if progression.request_date
            else '-'
        ):<12} "
        f"{responded:<11} "
        f"{disposition:<18} "
        f"{progression.progression_status:<28} "
        f"{progression.schedule_signal:<35}"
    )


print()
print("=" * 100)
print("PROGRESSION DETAILS")
print("=" * 100)

for progression in progressions:

    print()

    print(
        f"Request: "
        f"{(
            progression.request_date.strftime('%Y-%m-%d')
            if progression.request_date
            else '-'
        )} | "
        f"{progression.request_accession}"
    )

    print(
        "Responses:",
        ", ".join(
            progression.response_accessions
        )
        if progression.response_accessions
        else "none",
    )

    print(
        "Explicit FERC disposition:",
        progression.explicit_disposition
        or "NOT ESTABLISHED",
    )

    if progression.disposition_accession:
        print(
            "Disposition accession:",
            progression.disposition_accession,
        )

    print(
        "Subsequent FERC requests:",
        ", ".join(
            progression.subsequent_request_accessions
        )
        if progression.subsequent_request_accessions
        else "none",
    )

    if progression.subsequent_milestone_accession:
        print(
            "Subsequent milestone:",
            progression.subsequent_milestone_accession,
        )

        print(
            "Milestone type:",
            progression.subsequent_milestone_type,
        )

        print(
            "Milestone date:",
            (
                progression.subsequent_milestone_date.strftime(
                    "%Y-%m-%d"
                )
                if progression.subsequent_milestone_date
                else "-"
            ),
        )

    print(
        "Progression status:",
        progression.progression_status,
    )

    print(
        "Progression evidence:",
        progression.progression_evidence
        or "none identified",
    )

    print(
        "Next expected action:",
        progression.next_expected_action
        or "none established",
    )

    print(
        "Schedule signal:",
        progression.schedule_signal,
    )


# ==============================================================
# DETAILS
# ==============================================================

print()
print("=" * 100)
print("REQUEST DETAILS")
print("=" * 100)


for request in requests:

    print()
    print(
        f"{format_date(request.request_date)} | "
        f"{request.request_accession}"
    )

    print(
        f"Type: "
        f"{format_type(request.request_type)}"
    )

    if (
        request.response_period_days
        is not None
    ):
        print(
            "Response period:",
            request.response_period_days,
            request.response_period_type,
        )
    else:
        print(
            "Response period: not identified"
        )

    print(
        "Estimated due:",
        format_date(
            request.estimated_due_date
        ),
    )

    print(
        "Response found:",
        request.response_found,
    )

    print(
        "Response accessions:",
        ", ".join(
            request.response_accessions
        )
        if request.response_accessions
        else "none",
    )

    print(
        "Response timing:",
        timing_label(
            request.response_timing
        ),
    )

    print(
        "Status:",
        request.status,
    )

    print(
        "FERC disposition:",
        request.ferc_disposition
        or "NOT ESTABLISHED",
    )

    print(
        "Next expected action:",
        request.next_expected_action,
    )


# ==============================================================
# OPEN REQUESTS
# ==============================================================

open_requests = [
    request
    for request in requests
    if not request.response_found
]


print()
print("=" * 100)
print("OPEN / NEEDS ATTENTION")
print("=" * 100)

if not open_requests:

    print()
    print(
        "No explicitly matched FERC information "
        "requests are currently awaiting a sponsor response."
    )

else:

    for request in open_requests:

        print()
        print(
            f"{format_date(request.request_date)} | "
            f"{request.request_accession}"
        )

        print(
            f"Type: "
            f"{format_type(request.request_type)}"
        )

        print(
            f"Estimated due: "
            f"{format_date(request.estimated_due_date)}"
        )

        print(
            "Status:",
            request.status,
        )

        print(
            "Next expected action:",
            request.next_expected_action,
        )


# ==============================================================
# SUMMARY
# ==============================================================

response_count = sum(
    1
    for request in requests
    if request.response_found
)

open_count = len(
    open_requests
)

print()
print("=" * 100)
print("SUMMARY")
print("=" * 100)

print()
print(
    "Tracked FERC requests:",
    len(requests),
)

print(
    "With matched sponsor response:",
    response_count,
)

print(
    "Still awaiting matched response:",
    open_count,
)

if requests:

    print(
        "Response rate:",
        f"{response_count / len(requests) * 100:.1f}%",
    )

print()
print(
    "IMPORTANT:"
)

print(
    "A sponsor response is not treated as FERC acceptance."
)

print(
    "FERC disposition remains "
    "'NOT ESTABLISHED' until explicit FERC action is found."
)

print()
print("=" * 100)
print("END")
print("=" * 100)