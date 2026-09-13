from collections import Counter

from ferc_filter.elibrary import ELibraryClient
from ferc_filter.features import extract_features


PROJECTS = {
    "Rio Grande LNG": "CP16-454",
    "Mississippi Crossing": "CP25-514",
}


client = ELibraryClient()


for project_name, docket in PROJECTS.items():

    print()
    print("=" * 70)
    print(project_name)
    print("=" * 70)

    result = client.get_docket(
        docket=docket,
        start_date="01-01-2026",
        end_date="09-03-2026",
    )

    actors = Counter()
    actions = Counter()
    event_areas = Counter()
    categories = Counter()

    for record in result.records:

        features = extract_features(record)

        actors[features.actor] += 1
        actions[features.action] += 1
        event_areas[features.event_area] += 1
        categories[record.get("category")] += 1

    print("\n=== CATEGORY ===")
    for key, count in categories.most_common():
        print(f"{count:4} | {key}")

    print("\n=== ACTOR ===")
    for key, count in actors.most_common():
        print(f"{count:4} | {key}")

    print("\n=== ACTION ===")
    for key, count in actions.most_common():
        print(f"{count:4} | {key}")

    print("\n=== EVENT AREA ===")
    for key, count in event_areas.most_common():
        print(f"{count:4} | {key}")

    print("\n=== SAMPLE UNKNOWN RECORDS ===")

    shown = 0

    for record in result.records:

        features = extract_features(record)

        if features.event_area != "other":
            continue

        print(
            record.get("accession_no"),
            "|",
            record.get("category"),
            "|",
            record.get("doc_desc"),
        )

        shown += 1

        if shown >= 20:
            break