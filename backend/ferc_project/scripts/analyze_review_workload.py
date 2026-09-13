from collections import Counter, defaultdict

from ferc_filter.elibrary import ELibraryClient
from ferc_filter.elibrary_enrichment import ELibraryEnrichmentClient
from ferc_filter.event_classifier import classify_event


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

EXAMPLES_PER_TYPE = 3


# ==============================================================
# CLIENTS
# ==============================================================

docket_client = ELibraryClient()
enrichment_client = ELibraryEnrichmentClient()


# ==============================================================
# HELPERS
# ==============================================================

def deduplicate_records(records):
    """
    Provisional accession-based deduplication.

    Keeps one record per accession number.
    """

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
    """
    Add FERC Class / Type metadata using the same accession
    enrichment approach used by the classifier validation.
    """

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

    enriched_record["availability_code"] = (
        enriched.get("availCode")
    )

    return enriched_record


# ==============================================================
# ANALYSIS
# ==============================================================

all_review_records = []
project_summaries = []


print()
print("=" * 100)
print("REVIEW WORKLOAD ANALYSIS")
print("=" * 100)
print()
print(
    "Classifier: frozen v0.2 "
    "(diagnostic only — no classifier changes)"
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

    raw_records = result.records

    records = deduplicate_records(
        raw_records
    )

    decisions = Counter()
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

        decisions[
            decision.decision
        ] += 1

        if decision.decision != "REVIEW":
            continue

        item = {
            "project": project["name"],
            "docket": project["docket"],
            "accession": record.get(
                "accession_no"
            ),
            "event_type": (
                decision.event_type
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
            "description": record.get(
                "doc_desc"
            ),
        }

        review_records.append(item)
        all_review_records.append(item)

    project_summaries.append(
        {
            "project": project["name"],
            "docket": project["docket"],
            "raw": len(raw_records),
            "dedup": len(records),
            "alert": decisions["ALERT"],
            "review": decisions["REVIEW"],
            "suppress": decisions["SUPPRESS"],
        }
    )

    counts = Counter(
        item["event_type"]
        for item in review_records
    )

    print()
    print("=== SUMMARY ===")
    print(
        "Raw:",
        len(raw_records),
    )
    print(
        "After dedup:",
        len(records),
    )
    print(
        "ALERT:",
        decisions["ALERT"],
    )
    print(
        "REVIEW:",
        decisions["REVIEW"],
    )
    print(
        "SUPPRESS:",
        decisions["SUPPRESS"],
    )

    if records:
        review_pct = (
            decisions["REVIEW"]
            / len(records)
        ) * 100
    else:
        review_pct = 0

    print(
        f"REVIEW workload: "
        f"{review_pct:.1f}%"
    )

    print()
    print("=== REVIEW BY EVENT TYPE ===")

    for event_type, count in (
        counts.most_common()
    ):
        print(
            f"{count:4} | {event_type}"
        )

    print()
    print("=== REPRESENTATIVE EXAMPLES ===")

    examples = defaultdict(list)

    for item in review_records:

        event_type = item[
            "event_type"
        ]

        if (
            len(examples[event_type])
            < EXAMPLES_PER_TYPE
        ):
            examples[event_type].append(
                item
            )

    for event_type, count in (
        counts.most_common()
    ):

        print()
        print(
            f"{event_type} "
            f"({count})"
        )
        print("-" * 80)

        for item in examples[event_type]:

            print(
                f"{item['accession']} | "
                f"{item['document_class']} -> "
                f"{item['document_type']}"
            )

            print(
                item["description"]
            )

            print()


# ==============================================================
# COMBINED ANALYSIS
# ==============================================================

combined_counts = Counter(
    item["event_type"]
    for item in all_review_records
)

combined_examples = defaultdict(list)

for item in all_review_records:

    event_type = item["event_type"]

    if (
        len(combined_examples[event_type])
        < EXAMPLES_PER_TYPE
    ):
        combined_examples[event_type].append(
            item
        )


print()
print("=" * 100)
print("COMBINED REVIEW WORKLOAD")
print("=" * 100)


total_records = sum(
    item["dedup"]
    for item in project_summaries
)

total_alert = sum(
    item["alert"]
    for item in project_summaries
)

total_review = sum(
    item["review"]
    for item in project_summaries
)

total_suppress = sum(
    item["suppress"]
    for item in project_summaries
)


print()
print("=== PROJECT SUMMARY ===")

for summary in project_summaries:

    print(
        f"{summary['project']:<50} | "
        f"Records {summary['dedup']:4} | "
        f"ALERT {summary['alert']:3} | "
        f"REVIEW {summary['review']:3} | "
        f"SUPPRESS {summary['suppress']:3}"
    )


print()
print("=== TOTAL ===")

print(
    "Records:",
    total_records,
)

print(
    "ALERT:",
    total_alert,
)

print(
    "REVIEW:",
    total_review,
)

print(
    "SUPPRESS:",
    total_suppress,
)

if total_records:

    print(
        f"Overall REVIEW workload: "
        f"{(
            total_review
            / total_records
        ) * 100:.1f}%"
    )


print()
print(
    "=== COMBINED REVIEW BY EVENT TYPE ==="
)

for event_type, count in (
    combined_counts.most_common()
):

    percentage = (
        count / total_review * 100
        if total_review
        else 0
    )

    print(
        f"{count:4} | "
        f"{percentage:5.1f}% | "
        f"{event_type}"
    )


print()
print(
    "=== COMBINED REPRESENTATIVE EXAMPLES ==="
)

for event_type, count in (
    combined_counts.most_common()
):

    print()
    print(
        f"{event_type} ({count})"
    )
    print("-" * 80)

    for item in (
        combined_examples[event_type]
    ):

        print(
            f"{item['project']} | "
            f"{item['accession']}"
        )

        print(
            f"{item['document_class']} -> "
            f"{item['document_type']}"
        )

        print(
            item["description"]
        )

        print()