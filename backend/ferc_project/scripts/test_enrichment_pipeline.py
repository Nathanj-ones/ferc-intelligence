from ferc_filter.elibrary import ELibraryClient
from ferc_filter.elibrary_enrichment import (
    ELibraryEnrichmentClient,
)


DOCKET = "CP25-514"


docket_client = ELibraryClient()
enrichment_client = ELibraryEnrichmentClient()


result = docket_client.get_docket(
    docket=DOCKET,
    start_date="01-01-2026",
    end_date="09-03-2026",
)


print()
print("=== DOCKET ===")
print("Records:", len(result.records))


# Test the first five accession numbers.
for record in result.records[:5]:

    accession = record.get("accession_no")

    print()
    print("=" * 70)
    print("ACCESSION:", accession)

    enriched = enrichment_client.get_by_accession(
        accession
    )

    if enriched is None:
        print("ENRICHMENT: NOT FOUND")
        continue

    print("Category:", enriched.get("category"))

    print("Class / Type:")

    for item in enriched.get("classTypes", []):
        print(
            " ",
            item.get("documentClass"),
            "->",
            item.get("documentType"),
        )

    print("Family:", enriched.get("familyValue"))
    print("Libraries:", enriched.get("libraries"))
    print("Availability:", enriched.get("availCode"))

    print("Files:")

    for file in enriched.get("transmittals", []):
        print(
            " ",
            file.get("fileType"),
            "|",
            file.get("fileName"),
            "|",
            file.get("fileId"),
        )