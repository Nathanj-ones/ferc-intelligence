from collections import Counter, defaultdict

from ferc_filter.elibrary import ELibraryClient
from ferc_filter.elibrary_enrichment import ELibraryEnrichmentClient
from ferc_filter.metadata_classifier import filing_family_key


DOCKET = "CP24-200"
START_DATE = "04-01-2024"
END_DATE = "09-04-2026"


def deduplicate(records):
    families = {}

    for record in records:
        key = filing_family_key(record)

        if key not in families:
            families[key] = record
            continue

        existing = families[key]

        existing_orgs = existing.get(
            "Affiliation_Organization", []
        )
        new_orgs = record.get(
            "Affiliation_Organization", []
        )

        if len(new_orgs) > len(existing_orgs):
            families[key] = record

    return list(families.values())


docket_client = ELibraryClient()
enrichment_client = ELibraryEnrichmentClient()


print("=== LEA COUNTY EXPANSION ===")

result = docket_client.get_docket(
    docket=DOCKET,
    start_date=START_DATE,
    end_date=END_DATE,
)

records = deduplicate(result.records)

print("Raw records:", len(result.records))
print("After dedup:", len(records))


counts = Counter()
examples = defaultdict(list)

for index, record in enumerate(records, start=1):

    accession = record.get("accession_no")

    enriched = enrichment_client.get_by_accession(
        accession
    )

    if enriched is None:
        print(
            "WARNING: enrichment not found:",
            accession,
        )
        continue

    class_types = enriched.get("classTypes", [])

    if not class_types:
        key = (
            "NOT_AVAILABLE",
            "NOT_AVAILABLE",
        )

        counts[key] += 1

        if len(examples[key]) < 5:
            examples[key].append(
                (
                    accession,
                    record.get("doc_desc"),
                )
            )

    else:
        for class_type in class_types:

            key = (
                class_type.get("documentClass"),
                class_type.get("documentType"),
            )

            counts[key] += 1

            if len(examples[key]) < 5:
                examples[key].append(
                    (
                        accession,
                        record.get("doc_desc"),
                    )
                )

    if index % 25 == 0:
        print(f"Enriched {index}/{len(records)}...")


print()
print("=== LEA COUNTY CLASS / TYPE INVENTORY ===")

for (document_class, document_type), count in counts.most_common():

    print(
        f"{count:4} | "
        f"{document_class} -> {document_type}"
    )

    for accession, description in examples[
        (document_class, document_type)
    ]:
        print(
            f"       {accession} | {description}"
        )