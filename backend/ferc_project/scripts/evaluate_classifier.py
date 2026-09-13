from collections import Counter

from ferc_filter.elibrary import ELibraryClient
from ferc_filter.event_classifier import classify_event
from ferc_filter.ground_truth import GROUND_TRUTH
from ferc_filter.elibrary_enrichment import ELibraryEnrichmentClient

TEST_CONFIG = {
    "Rio Grande LNG": {
        "docket": "CP16-454",
        "start": "01-01-2026",
        "end": "09-03-2026",
    },
    "Mississippi Crossing": {
        "docket": "CP25-514",
        "start": "01-01-2026",
        "end": "09-03-2026",
    },
    "Lea County Expansion": {
        "docket": "CP24-200",
        "start": "04-01-2024",
        "end": "09-04-2026",
    },
}


# Retrieve each docket once.
client = ELibraryClient()
enrichment_client = ELibraryEnrichmentClient()
records_by_accession = {}

for project_name, config in TEST_CONFIG.items():

    result = client.get_docket(
        docket=config["docket"],
        start_date=config["start"],
        end_date=config["end"],
    )

    for record in result.records:

        accession = record.get("accession_no")

        if accession:
            records_by_accession[accession] = record


print()
print("=" * 80)
print("GROUND-TRUTH CLASSIFIER EVALUATION")
print("=" * 80)


results = []

for test in GROUND_TRUTH:

    accession = test["accession"]

    record = records_by_accession.get(accession)

    if record is None:

        results.append(
            {
                "accession": accession,
                "project": test["project"],
                "expected": test["expected_decision"],
                "actual": "NOT_FOUND",
                "expected_event": test["event_type"],
                "actual_event": "NOT_FOUND",
                "decision_match": False,
                "event_match": False,
            }
        )

        continue

    enriched = enrichment_client.get_by_accession(
        accession
    )

    enriched_record = dict(record)

    if enriched is not None:

        class_types = enriched.get("classTypes", [])

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

    decision = classify_event(enriched_record)

    decision_match = (
        decision.decision
        == test["expected_decision"]
    )

    event_match = (
        decision.event_type
        == test["event_type"]
    )

    results.append(
        {
            "accession": accession,
            "project": test["project"],
            "expected": test["expected_decision"],
            "actual": decision.decision,
            "expected_event": test["event_type"],
            "actual_event": decision.event_type,
            "decision_match": decision_match,
            "event_match": event_match,
        }
    )


print()
print("=== TEST RESULTS ===")

for result in results:

    status = (
        "PASS"
        if result["decision_match"]
        else "FAIL"
    )

    print(
        f"{status:4} | "
        f"{result['project']:24} | "
        f"{result['accession']} | "
        f"expected={result['expected']} | "
        f"actual={result['actual']} | "
        f"expected_event={result['expected_event']} | "
        f"actual_event={result['actual_event']}"
    )


decision_accuracy = sum(
    result["decision_match"]
    for result in results
) / len(results)


event_accuracy = sum(
    result["event_match"]
    for result in results
) / len(results)


print()
print("=== ACCURACY ===")
print(
    f"Decision accuracy: {decision_accuracy:.1%}"
)
print(
    f"Event-type accuracy: {event_accuracy:.1%}"
)


print()
print("=== FAILURES ===")

for result in results:

    if result["decision_match"]:
        continue

    print(
        result["accession"],
        "|",
        result["project"],
        "| expected:",
        result["expected"],
        "| actual:",
        result["actual"],
    )


print()
print("=== RESULTS BY PROJECT ===")

by_project = {}

for result in results:

    by_project.setdefault(
        result["project"],
        [],
    ).append(result)


for project, project_results in by_project.items():

    accuracy = sum(
        result["decision_match"]
        for result in project_results
    ) / len(project_results)

    print(
        f"{project:24} | "
        f"{accuracy:.1%}"
    )