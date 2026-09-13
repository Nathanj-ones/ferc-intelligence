from collections import Counter

from ferc_filter.elibrary import ELibraryClient


client = ELibraryClient()

result = client.get_docket(
    docket="CP25-514",
    start_date="01-01-2026",
    end_date="09-03-2026",
)


print("\n=== CATEGORY COUNTS ===")

counts = Counter(
    record.get("category")
    for record in result.records
)

for category, count in counts.most_common():
    print(f"{count:4} | {category}")


print("\n=== ISSUANCES ===")

for record in result.records:

    if record.get("category") != "Issuance":
        continue

    print(
        record.get("accession_no"),
        "|",
        str(record.get("filed_date", ""))[:10],
        "|",
        record.get("doc_desc"),
    )