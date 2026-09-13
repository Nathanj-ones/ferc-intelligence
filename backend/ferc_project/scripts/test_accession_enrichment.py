import json
import requests


URL = (
    "https://elibrary.ferc.gov/"
    "eLibraryWebAPI/api/Search/AdvancedSearch"
)


# Known MSX certificate order.
ACCESSION = "20260731-3085"


payload = {
    "searchText": "*",
    "searchFullText": True,
    "searchDescription": True,
    "dateSearches": [],
    "availability": None,
    "affiliations": [],
    "categories": [],
    "libraries": [],
    "accessionNumber": ACCESSION,
    "eFiling": False,
    "docketSearches": [],
    "resultsPerPage": 100,
    "curPage": 0,
    "classTypes": [],
    "sortBy": "",
    "groupBy": "NONE",
    "idolResultID": "",
    "allDates": True,
}


response = requests.post(
    URL,
    json=payload,
    timeout=30,
)

response.raise_for_status()

data = response.json()


print("Request successful")
print("Total hits:", data.get("totalHits"))
print("Records returned:", len(data.get("searchHits", [])))


for hit in data.get("searchHits", []):

    print()
    print("=== RECORD ===")

    print(
        "Accession:",
        hit.get("acesssionNumber"),
    )

    print(
        "Category:",
        hit.get("category"),
    )

    print(
        "Description:",
        hit.get("description"),
    )

    print("Class / Type:")

    for class_type in hit.get("classTypes", []):
        print(
            " ",
            class_type.get("documentClass"),
            "->",
            class_type.get("documentType"),
        )

    print("Dockets:", hit.get("docketNumbers"))

    print("Availability:", hit.get("availCode"))

    print("Files:")

    for file in hit.get("transmittals", []):
        print(
            " ",
            file.get("fileType"),
            "|",
            file.get("fileName"),
            "|",
            file.get("fileId"),
        )


print()
print("=== RAW RESPONSE ===")
print(json.dumps(data, indent=2)[:5000])