from datetime import datetime
from pathlib import Path
from collections import Counter

from ferc_filter.elibrary import ELibraryClient
from ferc_filter.elibrary_enrichment import ELibraryEnrichmentClient
from ferc_filter.event_classifier import classify_event
from ferc_filter.ground_truth import GROUND_TRUTH


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

    decision = classify_event(
        enriched_record
    )

    results.append(
        {
            "accession": accession,
            "project": test["project"],
            "expected": test["expected_decision"],
            "actual": decision.decision,
            "expected_event": test["event_type"],
            "actual_event": decision.event_type,
            "decision_match": (
                decision.decision
                == test["expected_decision"]
            ),
            "event_match": (
                decision.event_type
                == test["event_type"]
            ),
        }
    )


decision_accuracy = (
    sum(r["decision_match"] for r in results)
    / len(results)
)

event_accuracy = (
    sum(r["event_match"] for r in results)
    / len(results)
)


timestamp = datetime.now().strftime(
    "%Y%m%d_%H%M%S"
)

output_dir = Path("reports")
output_dir.mkdir(exist_ok=True)

output_file = (
    output_dir
    / f"classifier_evaluation_{timestamp}.txt"
)


with output_file.open(
    "w",
    encoding="utf-8",
) as f:

    f.write("FERC CLASSIFIER EVALUATION\n")
    f.write("=" * 80 + "\n\n")

    f.write(
        f"Timestamp: {datetime.now().isoformat()}\n"
    )

    f.write(
        f"Ground-truth cases: {len(results)}\n\n"
    )

    f.write(
        f"Decision accuracy: "
        f"{decision_accuracy:.1%}\n"
    )

    f.write(
        f"Event-type accuracy: "
        f"{event_accuracy:.1%}\n\n"
    )

    f.write("RESULTS\n")
    f.write("-" * 80 + "\n")

    for result in results:

        status = (
            "PASS"
            if result["decision_match"]
            else "FAIL"
        )

        f.write(
            f"{status} | "
            f"{result['project']} | "
            f"{result['accession']} | "
            f"expected={result['expected']} | "
            f"actual={result['actual']} | "
            f"expected_event={result['expected_event']} | "
            f"actual_event={result['actual_event']}\n"
        )

    f.write("\nFAILURES\n")
    f.write("-" * 80 + "\n")

    failures = [
        r for r in results
        if not r["decision_match"]
    ]

    if not failures:
        f.write("None\n")
    else:
        for result in failures:
            f.write(
                f"{result['accession']} | "
                f"{result['project']} | "
                f"expected={result['expected']} | "
                f"actual={result['actual']}\n"
            )


print()
print("Evaluation report saved:")
print(output_file)
print()
print(
    f"Decision accuracy: {decision_accuracy:.1%}"
)
print(
    f"Event-type accuracy: {event_accuracy:.1%}"
)