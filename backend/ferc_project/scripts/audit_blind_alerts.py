from ferc_filter.elibrary import ELibraryClient
from ferc_filter.elibrary_enrichment import ELibraryEnrichmentClient
from ferc_filter.event_classifier import classify_event


DOCKET = "CP25-10"
START_DATE = "10-01-2024"
END_DATE = "09-04-2026"


def deduplicate(records):
    """
    Keep this aligned with the current provisional deduplication
    used elsewhere in the project.
    """
    from ferc_filter.metadata_classifier import filing_family_key

    families = {}

    for record in records:
        key = filing_family_key(record)

        if key not in families:
            families[key] = record
            continue

        existing = families[key]

        existing_orgs = existing.get(
            "Affiliation_Organization",
            [],
        )
        new_orgs = record.get(
            "Affiliation_Organization",
            [],
        )

        if len(new_orgs) > len(existing_orgs):
            families[key] = record

    return list(families.values())


def enrich(record, client):
    accession = record.get("accession_no")

    enriched = client.get_by_accession(accession)

    result = dict(record)

    if enriched is None:
        return result

    class_types = enriched.get("classTypes", [])

    if class_types:
        primary = class_types[0]

        result["document_class"] = (
            primary.get("documentClass")
        )

        result["document_type"] = (
            primary.get("documentType")
        )

    result["enriched_class_types"] = class_types

    return result


docket_client = ELibraryClient()
enrichment_client = ELibraryEnrichmentClient()


result = docket_client.get_docket(
    docket=DOCKET,
    start_date=START_DATE,
    end_date=END_DATE,
)

records = deduplicate(result.records)

alerts = []

for record in records:

    enriched_record = enrich(
        record,
        enrichment_client,
    )

    decision = classify_event(
        enriched_record
    )

    if decision.decision != "ALERT":
        continue

    alerts.append(
        (
            enriched_record,
            decision,
        )
    )


print()
print("=" * 100)
print("BLIND TEST ALERT AUDIT")
print("=" * 100)

print(
    f"Raw records: {len(result.records)}"
)
print(
    f"After deduplication: {len(records)}"
)
print(
    f"Current ALERT count: {len(alerts)}"
)

print()
print("For each alert, assess it manually as:")
print("  MATERIAL     = should definitely alert")
print("  CONTEXT      = potentially important, needs deeper analysis")
print("  FALSE_POS    = should not reach alert stage")
print()
print("Do NOT change the classifier while doing this.")
print()


for record, decision in alerts:

    print("-" * 100)

    print(
        "ACCESSION:",
        record.get("accession_no"),
    )

    print(
        "DATE:",
        str(
            record.get(
                "filed_date",
                "",
            )
        )[:10],
    )

    print(
        "CATEGORY:",
        record.get("category"),
    )

    print(
        "FERC CLASS:",
        record.get("document_class"),
    )

    print(
        "FERC TYPE:",
        record.get("document_type"),
    )

    print(
        "ACTOR:",
        decision.features.actor,
    )

    print(
        "ACTION:",
        decision.features.action,
    )

    print(
        "EVENT AREA:",
        decision.features.event_area,
    )

    print(
        "SUBJECT:",
        decision.features.subject_role,
    )

    print(
        "MATERIALITY:",
        decision.features.materiality_signal,
    )

    print(
        "FOLLOW-UP:",
        decision.features.followup_signal,
    )

    print(
        "CURRENT EVENT:",
        decision.event_type,
    )

    print(
        "DESCRIPTION:",
        record.get("doc_desc"),
    )