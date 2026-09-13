from collections import Counter, defaultdict
from datetime import datetime

from ferc_filter.elibrary import ELibraryClient
from ferc_filter.elibrary_enrichment import ELibraryEnrichmentClient
from ferc_filter.event_classifier import classify_event


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

THREAD_GAP_DAYS = 14


docket_client = ELibraryClient()
enrichment_client = ELibraryEnrichmentClient()


def deduplicate_records(records):
    seen = set()
    deduped = []

    for record in records:
        accession = record.get("accession_no")

        if not accession:
            continue

        if accession in seen:
            continue

        seen.add(accession)
        deduped.append(record)

    return deduped


def enrich_record(record):
    accession = record.get("accession_no")

    if not accession:
        return record

    enriched = enrichment_client.get_by_accession(
        accession
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

        enriched_record["document_class"] = (
            primary.get("documentClass")
        )

        enriched_record["document_type"] = (
            primary.get("documentType")
        )

    enriched_record["enriched_class_types"] = (
        class_types
    )

    return enriched_record


def parse_record_date(record):
    """
    Parse the best available filing date.
    """

    value = (
        record.get("filed_date")
        or record.get("filedDate")
        or record.get("issued_date")
        or record.get("issuedDate")
    )

    if not value:
        return None

    formats = (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%m/%d/%Y",
    )

    for fmt in formats:
        try:
            return datetime.strptime(
                value[:19],
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


def build_threads(records):
    """
    Group REVIEW records of the same event type when
    consecutive records occur within THREAD_GAP_DAYS.
    """

    grouped = defaultdict(list)

    for item in records:
        grouped[
            item["event_type"]
        ].append(item)

    threads = []

    for event_type, items in grouped.items():

        dated_items = [
            item
            for item in items
            if item["date"] is not None
        ]

        undated_items = [
            item
            for item in items
            if item["date"] is None
        ]

        dated_items.sort(
            key=lambda item: item["date"]
        )

        current_thread = []

        for item in dated_items:

            if not current_thread:
                current_thread = [item]
                continue

            previous = current_thread[-1]

            gap = (
                item["date"]
                - previous["date"]
            ).days

            if gap <= THREAD_GAP_DAYS:
                current_thread.append(item)

            else:
                threads.append(
                    {
                        "event_type": event_type,
                        "records": current_thread,
                    }
                )

                current_thread = [item]

        if current_thread:
            threads.append(
                {
                    "event_type": event_type,
                    "records": current_thread,
                }
            )

        # Keep undated records visible rather than
        # silently merging them.
        for item in undated_items:
            threads.append(
                {
                    "event_type": event_type,
                    "records": [item],
                }
            )

    return threads


all_project_results = []


print()
print("=" * 100)
print("REVIEW THREAD ANALYSIS")
print("=" * 100)
print(
    f"Thread gap: {THREAD_GAP_DAYS} days"
)
print(
    "Classifier: frozen v0.2"
)


for project in PROJECTS:

    print()
    print("=" * 100)
    print(project["name"])
    print(project["docket"])
    print("=" * 100)

    result = docket_client.get_docket(
        docket=project["docket"],
        start_date=START_DATE,
        end_date=END_DATE,
    )

    records = deduplicate_records(
        result.records
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
                f"Processed {index}/{total}..."
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
            {
                "accession": record.get(
                    "accession_no"
                ),
                "event_type": (
                    decision.event_type
                ),
                "date": parse_record_date(
                    record
                ),
                "description": record.get(
                    "doc_desc"
                ),
                "document_class": (
                    enriched_record.get(
                        "document_class"
                    )
                ),
                "document_type": (
                    enriched_record.get(
                        "document_type"
                    )
                ),
            }
        )

    threads = build_threads(
        review_records
    )

    thread_counts = Counter(
        thread["event_type"]
        for thread in threads
    )

    review_count = len(
        review_records
    )

    thread_count = len(
        threads
    )

    reduction = (
        (
            1
            - thread_count / review_count
        )
        * 100
        if review_count
        else 0
    )

    print()
    print("=== WORKLOAD ===")
    print(
        "REVIEW filings:",
        review_count,
    )
    print(
        "Activity threads:",
        thread_count,
    )
    print(
        f"Potential workload reduction: "
        f"{reduction:.1f}%"
    )

    print()
    print("=== THREADS BY EVENT TYPE ===")

    for event_type, count in (
        thread_counts.most_common()
    ):
        filing_count = sum(
            len(thread["records"])
            for thread in threads
            if thread["event_type"]
            == event_type
        )

        print(
            f"{event_type:<28} | "
            f"{filing_count:4} filings | "
            f"{count:3} threads"
        )

    print()
    print("=== LARGEST THREADS ===")

    largest = sorted(
        threads,
        key=lambda thread: len(
            thread["records"]
        ),
        reverse=True,
    )[:10]

    for thread in largest:

        items = thread["records"]

        start = items[0]["date"]
        end = items[-1]["date"]

        start_text = (
            start.strftime("%Y-%m-%d")
            if start
            else "unknown"
        )

        end_text = (
            end.strftime("%Y-%m-%d")
            if end
            else "unknown"
        )

        print()
        print(
            f"{thread['event_type']} | "
            f"{len(items)} filings | "
            f"{start_text} -> {end_text}"
        )

        for item in items[:3]:
            print(
                f"  {item['accession']} | "
                f"{item['description']}"
            )

        if len(items) > 3:
            print(
                f"  ... +"
                f"{len(items) - 3} more"
            )

    all_project_results.append(
        {
            "project": project["name"],
            "review_count": review_count,
            "thread_count": thread_count,
            "threads": threads,
        }
    )


print()
print("=" * 100)
print("COMBINED THREAD ANALYSIS")
print("=" * 100)


total_reviews = sum(
    result["review_count"]
    for result in all_project_results
)

total_threads = sum(
    result["thread_count"]
    for result in all_project_results
)

combined_reduction = (
    (
        1
        - total_threads / total_reviews
    )
    * 100
    if total_reviews
    else 0
)


print()
print(
    "Total REVIEW filings:",
    total_reviews,
)

print(
    "Total activity threads:",
    total_threads,
)

print(
    f"Potential workload reduction: "
    f"{combined_reduction:.1f}%"
)


combined_type_filings = Counter()
combined_type_threads = Counter()


for result in all_project_results:

    for thread in result["threads"]:

        event_type = thread[
            "event_type"
        ]

        combined_type_threads[
            event_type
        ] += 1

        combined_type_filings[
            event_type
        ] += len(
            thread["records"]
        )


print()
print(
    "=== COMBINED THREADS BY EVENT TYPE ==="
)

for event_type, filings in (
    combined_type_filings.most_common()
):

    threads = combined_type_threads[
        event_type
    ]

    reduction = (
        (
            1
            - threads / filings
        )
        * 100
        if filings
        else 0
    )

    print(
        f"{event_type:<28} | "
        f"{filings:4} filings | "
        f"{threads:3} threads | "
        f"{reduction:5.1f}% reduction"
    )