from collections import Counter
import re

from ferc_filter.elibrary import ELibraryClient


client = ELibraryClient()

result = client.get_docket(
    docket="CP25-514",
    start_date="01-01-2026",
    end_date="09-03-2026",
)


def normalize(text):
    text = (text or "").lower()

    # Remove dates and accession-like numbers.
    text = re.sub(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", " DATE ", text)
    text = re.sub(r"\b\d{8}-\d{4}\b", " ACCESSION ", text)

    # Remove punctuation.
    text = re.sub(r"[^a-z0-9\s]", " ", text)

    # Collapse whitespace.
    return re.sub(r"\s+", " ", text).strip()


phrases = Counter()

for record in result.records:
    description = normalize(record.get("doc_desc"))

    words = description.split()

    # Count 3-word phrases.
    for i in range(len(words) - 2):
        phrase = " ".join(words[i:i + 3])
        phrases[phrase] += 1


print("\n=== MOST COMMON 3-WORD PHRASES ===")

for phrase, count in phrases.most_common(50):
    print(f"{count:4} | {phrase}")


print("\n=== ALL ISSUANCES ===")

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