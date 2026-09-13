from collections import Counter

from ferc_filter.elibrary import ELibraryClient


client = ELibraryClient()

result = client.search_metadata(
    docket="CP16-454",
    start_date="2026-01-01",
    end_date="2026-09-03",
)

print("FERC reported total:", result.total_hits)
print("Unique accessions retrieved:", len(result.records))


type_counts = Counter()

for record in result.records:
    for class_type in record.get("classTypes", []):
        key = (
            class_type.get("documentClass"),
            class_type.get("documentType"),
        )

        type_counts[key] += 1


print("\n=== FERC CLASS / TYPE COUNTS ===")

for (document_class, document_type), count in type_counts.most_common():
    print(
        f"{count:>4} | "
        f"{document_class} -> {document_type}"
    )