from ferc_filter.elibrary import ELibraryClient
from ferc_filter.features import extract_features


TESTS = [
    {
        "project": "Rio Grande LNG",
        "docket": "CP16-454",
        "start": "01-01-2026",
        "end": "09-03-2026",
    },
    {
        "project": "Mississippi Crossing",
        "docket": "CP25-514",
        "start": "01-01-2026",
        "end": "09-03-2026",
    },
    {
        "project": "Lea County Expansion",
        "docket": "CP24-200",
        "start": "04-01-2024",
        "end": "09-04-2026",
    },
]


# These are general concepts, not project-specific classifier rules.
# We use them only to choose useful examples for this diagnostic.
INTERESTING_TERMS = (
    "extension of time",
    "environmental impact statement",
    "environmental assessment",
    "authorization",
    "granting",
    "certificate",
    "rehearing",
    "stay",
    "weekly",
    "monthly",
    "variance",
    "inspection",
    "response to",
    "prior notice",
)


client = ELibraryClient()


for config in TESTS:

    print()
    print("=" * 80)
    print(config["project"])
    print("=" * 80)

    result = client.get_docket(
        docket=config["docket"],
        start_date=config["start"],
        end_date=config["end"],
    )

    shown = 0

    for record in result.records:

        description = record.get("doc_desc") or ""
        text = description.lower()

        if not any(term in text for term in INTERESTING_TERMS):
            continue

        features = extract_features(record)

        print()
        print("ACCESSION:", record.get("accession_no"))
        print("CATEGORY:", record.get("category"))
        print("ACTOR:", features.actor)
        print("ACTION:", features.action)
        print("EVENT AREA:", features.event_area)
        print("SUBJECT:", features.subject_role)
        print("MATERIALITY:", features.materiality_signal)
        print("ROUTINE:", features.routine_signal)
        print("RECURRING:", features.recurring_signal)
        print("FOLLOW-UP:", features.followup_signal)
        print("DESCRIPTION:", description)

        shown += 1

        # Enough examples to diagnose each project without
        # dumping the whole docket.
        if shown >= 20:
            break