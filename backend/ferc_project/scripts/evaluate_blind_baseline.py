from collections import Counter

from ferc_filter.elibrary import ELibraryClient
from ferc_filter.elibrary_enrichment import ELibraryEnrichmentClient
from ferc_filter.event_classifier import classify_event
from ferc_filter.blind_ground_truth import BLIND_GROUND_TRUTH


DOCKET = "CP25-10"
START_DATE = "10-01-2024"
END_DATE = "09-04-2026"

EXPECTED_TO_DECISION = {
    "MATERIAL": "ALERT",
    "CONTEXT": "REVIEW",
    "SUPPRESS": "SUPPRESS",
}

docket_client = ELibraryClient()
enrichment_client = ELibraryEnrichmentClient()


result = docket_client.get_docket(
    docket=DOCKET,
    start_date=START_DATE,
    end_date=END_DATE,
)


records_by_accession = {
    record.get("accession_no"): record
    for record in result.records
    if record.get("accession_no")
}


results = []


for test in BLIND_GROUND_TRUTH:

    accession = test["accession"]
    expected_label = test["expected"]

    expected = EXPECTED_TO_DECISION[
        expected_label
    ]
    record = records_by_accession.get(accession)

    if record is None:
        results.append(
            {
                "accession": accession,
                "expected": expected,
                "actual": "NOT_FOUND",
                "pass": False,
            }
        )
        continue

    enriched = enrichment_client.get_by_accession(
        accession
    )

    enriched_record = dict(record)

    if enriched is not None:

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

    if accession in {
        "20241112-3035",
        "20241203-5159",
        "20260817-3020",
    }:
        print()
        print("=" * 70)
        print("DEBUG:", accession)
        print("CATEGORY:", enriched_record.get("category"))
        print(
            "DOCUMENT CLASS:",
            enriched_record.get("document_class"),
        )
        print(
            "DOCUMENT TYPE:",
            enriched_record.get("document_type"),
        )
        print(
            "DESCRIPTION:",
            enriched_record.get("doc_desc"),
        )

    decision = classify_event(
        enriched_record
    )

    results.append(
        {
            "accession": accession,
            "expected_label": expected_label,
            "expected": expected,
            "actual": decision.decision,
            "pass": decision.decision == expected,
        }
    )


print()
print("=" * 80)
print("CP25-10 BLIND BASELINE")
print("=" * 80)

print()
print("=== RESULTS ===")

for result in results:

    status = "PASS" if result["pass"] else "FAIL"

    print(
        f"{status:4} | "
        f"{result['accession']} | "
        f"expected={result['expected']} | "
        f"actual={result['actual']}"
    )


accuracy = (
    sum(result["pass"] for result in results)
    / len(results)
)


print()
print("=== ACCURACY ===")
print(
    f"Overall decision accuracy: {accuracy:.1%}"
)


print()
print("=== CONFUSION COUNTS ===")

pairs = Counter(
    (
        result["expected"],
        result["actual"],
    )
    for result in results
)

for (expected, actual), count in pairs.most_common():

    print(
        f"{count:3} | "
        f"expected={expected:8} | "
        f"actual={actual}"
    )


print()
print("=== FAILURES ===")

failures = [
    result
    for result in results
    if not result["pass"]
]

if not failures:
    print("None")
else:
    for result in failures:
        print(
            result["accession"],
            "| expected:",
            result["expected"],
            "| actual:",
            result["actual"],
        )