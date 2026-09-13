from collections import Counter, defaultdict

from ferc_filter.elibrary import ELibraryClient
from ferc_filter.metadata_classifier import (
    classify_metadata,
    filing_family_key,
)


DOCKET = "CP16-454"

# Test one month of Rio Grande activity.
START_DATE = "08-01-2026"
END_DATE = "08-31-2026"


client = ELibraryClient()

result = client.get_docket(
    docket=DOCKET,
    start_date=START_DATE,
    end_date=END_DATE,
)


# -----------------------------------
# Provisional duplicate consolidation
# -----------------------------------

families = {}

for record in result.records:
    key = filing_family_key(record)

    if key not in families:
        families[key] = record
        continue

    existing = families[key]

    existing_orgs = existing.get("Affiliation_Organization", [])
    new_orgs = record.get("Affiliation_Organization", [])

    if len(new_orgs) > len(existing_orgs):
        families[key] = record


records = list(families.values())


# ----------------
# Classify records
# ----------------

classified = []

for record in records:
    classification = classify_metadata(record)

    classified.append(
        {
            "accession": record.get("accession_no"),
            "date": str(record.get("filed_date", ""))[:10],
            "category": record.get("category"),
            "description": record.get("doc_desc"),
            "decision": classification.decision,
            "rule": classification.rule,
        }
    )


counts = Counter(
    item["decision"]
    for item in classified
)


print("\n=== MONITOR SIMULATION ===")
print(f"Docket: {DOCKET}")
print(f"Period: {START_DATE} to {END_DATE}")

print()
print("Raw FERC records:       ", len(result.records))
print("After deduplication:    ", len(records))
print("ALERT:                  ", counts.get("ALERT", 0))
print("REVIEW:                 ", counts.get("REVIEW", 0))
print("SUPPRESS:               ", counts.get("SUPPRESS", 0))


# ------------------------
# Candidate workload
# ------------------------

candidate_count = (
    counts.get("ALERT", 0)
    + counts.get("REVIEW", 0)
)

print()
print("Candidate events shown:", candidate_count)

if records:
    reduction = (
        1 - candidate_count / len(records)
    ) * 100

    print(
        f"Metadata workload reduction: {reduction:.1f}%"
    )


# ------------------------
# Breakdown by rule
# ------------------------

print("\n=== CANDIDATES BY RULE ===")

candidate_rules = Counter(
    item["rule"]
    for item in classified
    if item["decision"] in {"ALERT", "REVIEW"}
)

for rule, count in candidate_rules.most_common():
    print(f"{count:3} | {rule}")


# ------------------------
# Candidate details
# ------------------------

print("\n=== CANDIDATE EVENTS ===")

for item in classified:

    if item["decision"] == "SUPPRESS":
        continue

    print(
        f"{item['date']} | "
        f"{item['decision']:6} | "
        f"{item['rule']:30} | "
        f"{item['accession']} | "
        f"{item['description']}"
    )


# ------------------------
# Weekly workload
# ------------------------

weekly = defaultdict(
    lambda: {
        "ALERT": 0,
        "REVIEW": 0,
        "SUPPRESS": 0,
    }
)

for item in classified:

    day = int(item["date"][-2:])

    if day <= 7:
        week = "Week 1 (1-7)"
    elif day <= 14:
        week = "Week 2 (8-14)"
    elif day <= 21:
        week = "Week 3 (15-21)"
    elif day <= 28:
        week = "Week 4 (22-28)"
    else:
        week = "Week 5 (29-31)"

    weekly[week][item["decision"]] += 1


print("\n=== WEEKLY USER WORKLOAD ===")

for week, values in weekly.items():

    candidates = (
        values["ALERT"]
        + values["REVIEW"]
    )

    print(
        f"{week:16} | "
        f"ALERT {values['ALERT']:2} | "
        f"REVIEW {values['REVIEW']:2} | "
        f"SUPPRESS {values['SUPPRESS']:2} | "
        f"USER SEES {candidates:2}"
    )