from collections import Counter, defaultdict

from ferc_filter.elibrary import ELibraryClient
from ferc_filter.metadata_classifier import classify_metadata


client = ELibraryClient()

result = client.get_docket(
    docket="CP16-454",
    start_date="01-01-2026",
    end_date="09-03-2026",
)

classified = []

for record in result.records:
    decision = classify_metadata(record)

    class_types = []

    for item in record.get("classTypes", []):
        class_types.append(
            f"{item.get('documentClass')} -> "
            f"{item.get('documentType')}"
        )

    classified.append(
        {
            "accession": record.get("accession_no"),
            "date": record.get("filed_date"),
            "category": record.get("category"),
            "description": record.get("doc_desc"),
            "decision": decision.decision,
            "rule": decision.rule,
            "class_type": "; ".join(class_types),
        }
    )


print("\n=== DECISION COUNTS ===")

decision_counts = Counter(
    item["decision"]
    for item in classified
)

for decision in ["ALERT", "REVIEW", "SUPPRESS"]:
    print(
        f"{decision:10} {decision_counts.get(decision, 0)}"
    )


print("\n=== REVIEW COUNT BY RULE ===")

review_rules = Counter(
    item["rule"]
    for item in classified
    if item["decision"] == "REVIEW"
)

for rule, count in review_rules.most_common():
    print(f"{count:4} | {rule}")


print("\n=== REVIEW COUNT BY FERC CLASS / TYPE ===")

review_types = Counter(
    item["class_type"]
    for item in classified
    if item["decision"] == "REVIEW"
)

for class_type, count in review_types.most_common():
    print(f"{count:4} | {class_type}")


print("\n=== REVIEW EXAMPLES ===")

shown_by_rule = defaultdict(int)

for item in classified:
    if item["decision"] != "REVIEW":
        continue

    rule = item["rule"]

    # Show up to 5 examples of each review rule.
    if shown_by_rule[rule] >= 5:
        continue

    print(
        f"{item['accession']} | "
        f"{item['date']} | "
        f"{rule} | "
        f"{item['class_type']} | "
        f"{item['description']}"
    )

    shown_by_rule[rule] += 1