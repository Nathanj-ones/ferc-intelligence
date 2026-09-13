import requests


URL = "https://elibrary.ferc.gov/eLibraryWebAPI/api/Search/AdvancedSearch"

DOCKET = "CP16-454"
START_DATE = "2026-01-01"
END_DATE = "2026-09-03"

# Filings we already manually identified as material.
KNOWN_MATERIAL = {
    "20260312-3060": "Authorization to construct Trains 4 and 5",
    "20260424-5284": "Request for Extension of Time",
    "20260526-3043": "FERC approval of Extension of Time",
}

SELECTED_CLASS_TYPES = [
    {
        "documentClass": "Report/Form",
        "documentType": "Certificate of Compliance Report",
    },
    {
        "documentClass": "Order/Opinion",
        "documentType": "Commission Order/Opinion",
    },
]


def build_payload(class_types, page):
    return {
        "searchText": "*",
        "searchFullText": True,
        "searchDescription": True,
        "dateSearches": [
            {
                "dateType": "filed_date",
                "startDate": START_DATE,
                "endDate": END_DATE,
            }
        ],
        "availability": None,
        "affiliations": [],
        "categories": [],
        "libraries": [],
        "accessionNumber": None,
        "eFiling": False,
        "docketSearches": [
            {
                "docketNumber": DOCKET,
                "subDocketNumbers": [],
            }
        ],
        "resultsPerPage": 100,
        "curPage": page,
        "classTypes": class_types,
        "sortBy": "",
        "groupBy": "NONE",
        "idolResultID": "",
        "allDates": False,
    }


def get_accession(hit):
    # FERC currently misspells this field in its JSON response.
    return hit.get("acesssionNumber") or hit.get("accessionNumber")


def fetch_all(class_types):
    records = []
    page = 0

    while True:
        response = requests.post(
            URL,
            json=build_payload(class_types, page),
            timeout=30,
        )
        response.raise_for_status()

        data = response.json()
        hits = data.get("searchHits", [])

        records.extend(hits)

        print(
            f"Page {page + 1}: "
            f"{len(hits)} records "
            f"(reported total: {data.get('totalHits')})"
        )

        if len(records) >= data.get("totalHits", 0):
            break

        if not hits:
            break

        page += 1

    return records


def unique_accessions(records):
    return {
        get_accession(record)
        for record in records
        if get_accession(record)
    }


def find_record(records, accession):
    for record in records:
        if get_accession(record) == accession:
            return record
    return None


print("\n=== A. FULL 2026 UNIVERSE ===")
full_records = fetch_all([])

print("\n=== B. SELECTED CLASS/TYPES ===")
selected_records = fetch_all(SELECTED_CLASS_TYPES)

full_accessions = unique_accessions(full_records)
selected_accessions = unique_accessions(selected_records)

missing = full_accessions - selected_accessions

print("\n=== SUMMARY ===")
print("Full raw records:", len(full_records))
print("Full unique accessions:", len(full_accessions))
print("Selected raw records:", len(selected_records))
print("Selected unique accessions:", len(selected_accessions))
print("Unique accessions excluded by selected types:", len(missing))

if full_accessions:
    coverage = len(selected_accessions & full_accessions) / len(full_accessions)
    print(f"Selected-type coverage of full universe: {coverage:.1%}")


print("\n=== KNOWN MATERIAL EVENT TEST ===")

for accession, event_name in KNOWN_MATERIAL.items():
    in_full = accession in full_accessions
    in_selected = accession in selected_accessions

    if in_selected:
        result = "CAPTURED"
    elif in_full:
        result = "MISSED"
    else:
        result = "NOT FOUND IN FULL SEARCH"

    print(f"{accession} | {result} | {event_name}")

    record = find_record(full_records, accession)

    if record:
        class_types = record.get("classTypes", [])

        print("  FERC Class/Type:")
        for item in class_types:
            print(
                "   ",
                item.get("documentClass"),
                "->",
                item.get("documentType"),
            )

        print("  Description:", record.get("description"))


print("\n=== EXAMPLES EXCLUDED BY SELECTED TYPES ===")

shown = 0

for record in full_records:
    accession = get_accession(record)

    if accession not in missing:
        continue

    class_types = record.get("classTypes", [])

    type_text = "; ".join(
        f"{x.get('documentClass')} -> {x.get('documentType')}"
        for x in class_types
    )

    print(
        accession,
        "|",
        record.get("filedDate"),
        "|",
        type_text,
        "|",
        record.get("description"),
    )

    shown += 1

    if shown >= 20:
        break