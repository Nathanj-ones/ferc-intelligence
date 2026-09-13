from collections import Counter

from ferc_filter.elibrary import ELibraryClient


client = ELibraryClient()

result = client.get_docket(
    docket="CP16-454",
    start_date="01-01-2026",
    end_date="09-03-2026",
)

print()
print("=== SUMMARY ===")
print("Reported total:", result.reported_total)
print("Unique records retrieved:", len(result.records))

print()
print("=== FIRST 10 FILINGS ===")

for record in result.records[:10]:
    print(
        record.get("accession_no"),
        "|",
        record.get("filed_date"),
        "|",
        record.get("category"),
        "|",
        record.get("doc_desc"),
    )

print()
print("=== CATEGORY COUNTS ===")

category_counts = Counter(
    record.get("category")
    for record in result.records
)

for category, count in category_counts.most_common():
    print(count, "|", category)