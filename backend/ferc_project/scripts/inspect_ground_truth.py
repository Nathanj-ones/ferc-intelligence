from ferc_filter.elibrary import ELibraryClient
from ferc_filter.elibrary_enrichment import ELibraryEnrichmentClient
from ferc_filter.features import extract_features
from ferc_filter.event_classifier import classify_event


ACCESSIONS = [
    "20260220-5151",
    "20260312-3060",
    "20260130-3001",
    "20260626-3002",
    "20260312-4000",
    "20240628-3000",
]


docket_client = ELibraryClient()
enrichment_client = ELibraryEnrichmentClient()


DOCKETS = {
    "20260220-5151": ("CP16-454", "01-01-2026", "09-03-2026"),
    "20260312-3060": ("CP16-454", "01-01-2026", "09-03-2026"),
    "20260130-3001": ("CP25-514", "01-01-2026", "09-03-2026"),
    "20260626-3002": ("CP25-514", "01-01-2026", "09-03-2026"),
    "20260312-4000": ("CP25-514", "01-01-2026", "09-03-2026"),
    "20240628-3000": ("CP24-200", "04-01-2024", "09-04-2026"),
}


records = {}

for accession, (docket, start, end) in DOCKETS.items():

    result = docket_client.get_docket(
        docket=docket,
        start_date=start,
        end_date=end,
    )

    for record in result.records:

        if record.get("accession_no") == accession:
            records[accession] = record
            break


for accession in ACCESSIONS:

    print()
    print("=" * 80)
    print(accession)
    print("=" * 80)

    record = records.get(accession)

    if record is None:
        print("DOCKET RECORD NOT FOUND")
        continue

    enriched = enrichment_client.get_by_accession(
        accession
    )

    if enriched:
        record = dict(record)

        class_types = enriched.get("classTypes", [])

        if class_types:
            record["document_class"] = (
                class_types[0].get("documentClass")
            )
            record["document_type"] = (
                class_types[0].get("documentType")
            )

    features = extract_features(record)
    decision = classify_event(record)

    print("Description:")
    print(record.get("doc_desc"))

    print()
    print("FERC category:")
    print(record.get("category"))

    print()
    print("FERC class:")
    print(record.get("document_class"))

    print()
    print("FERC type:")
    print(record.get("document_type"))

    print()
    print("Actor:")
    print(features.actor)

    print("Action:")
    print(features.action)

    print("Event area:")
    print(features.event_area)

    print("Subject:")
    print(features.subject_role)

    print("Materiality:")
    print(features.materiality_signal)

    print("Routine:")
    print(features.routine_signal)

    print("Recurring:")
    print(features.recurring_signal)

    print("Follow-up:")
    print(features.followup_signal)

    print()
    print("CURRENT DECISION:")
    print(decision.decision)

    print("CURRENT EVENT:")
    print(decision.event_type)