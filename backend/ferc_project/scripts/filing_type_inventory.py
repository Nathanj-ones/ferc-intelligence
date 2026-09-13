from collections import Counter, defaultdict

from ferc_filter.elibrary import ELibraryClient


PROJECTS = {
    "Rio Grande LNG": "CP16-454",
    "Mississippi Crossing": "CP25-514",
}


client = ELibraryClient()


for project_name, docket in PROJECTS.items():

    print()
    print("=" * 80)
    print(project_name)
    print("=" * 80)

    result = client.get_docket(
        docket=docket,
        start_date="01-01-2026",
        end_date="09-03-2026",
    )

    counts = Counter()
    examples = defaultdict(list)

    for record in result.records:

        class_types = record.get("classTypes", [])

        # The docket-sheet endpoint does not currently provide Class/Type.
        # We therefore record the metadata we do have and flag these
        # records for later enrichment if Class/Type is needed.
        if not class_types:
            key = ("NOT_AVAILABLE", "NOT_AVAILABLE")
            counts[key] += 1

            if len(examples[key]) < 5:
                examples[key].append(
                    record.get("doc_desc")
                )

            continue

        for class_type in class_types:

            key = (
                class_type.get("documentClass"),
                class_type.get("documentType"),
            )

            counts[key] += 1

            if len(examples[key]) < 5:
                examples[key].append(
                    record.get("doc_desc")
                )

    print()
    print("=== CLASS / TYPE INVENTORY ===")

    for (document_class, document_type), count in counts.most_common():

        print(
            f"\n{count:4} | "
            f"{document_class} -> {document_type}"
        )

        for example in examples[(document_class, document_type)]:
            print(f"     {example}")