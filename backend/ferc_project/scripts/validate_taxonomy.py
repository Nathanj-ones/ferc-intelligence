from collections import Counter

from ferc_filter.elibrary import ELibraryClient
from ferc_filter.event_taxonomy import classify_event
from ferc_filter.metadata_classifier import filing_family_key


PROJECTS = {
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
}


def deduplicate(records):
    families = {}

    for record in records:
        key = filing_family_key(record)

        if key not in families:
            families[key] = record

    return list(families.values())


client = ELibraryClient()


for project_name, config in PROJECTS.items():

    print()
    print("=" * 70)
    print(project_name)
    print("=" * 70)

    result = client.get_docket(
        docket=config["docket"],
        start_date=config["start"],
        end_date=config["end"],
    )

    records = deduplicate(result.records)

    classified = [
        (record, classify_event(record))
        for record in records
    ]

    decisions = Counter(
        event.decision
        for _, event in classified
    )

    event_types = Counter(
        event.event_type
        for _, event in classified
    )

    print()
    print("Raw records:       ", len(result.records))
    print("After dedup:       ", len(records))
    print("ALERT:             ", decisions["ALERT"])
    print("REVIEW:            ", decisions["REVIEW"])
    print("SUPPRESS:          ", decisions["SUPPRESS"])

    print()
    print("=== EVENT TYPES ===")

    for event_type, count in event_types.most_common():
        print(f"{count:4} | {event_type}")

    print()
    print("=== ALERTS ===")

    for record, event in classified:

        if event.decision != "ALERT":
            continue

        print(
            record.get("accession_no"),
            "|",
            str(record.get("filed_date", ""))[:10],
            "|",
            event.event_type,
            "|",
            event.stage,
            "|",
            record.get("doc_desc"),
        )