import json

import requests

from ferc_filter.config import get_api_key


URL = "https://elibrary.ferc.gov/eLibraryWebAPI/api/Search/AdvancedSearch"


def search_elibrary(class_types):
    payload = {
        "searchText": "*",
        "searchFullText": True,
        "searchDescription": True,
        "dateSearches": [
            {
                "dateType": "filed_date",
                "startDate": "2026-01-01",
                "endDate": "2026-09-03",
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
                "docketNumber": "CP16-454",
                "subDocketNumbers": [],
            }
        ],
        "resultsPerPage": 100,
        "curPage": 0,
        "classTypes": class_types,
        "sortBy": "",
        "groupBy": "NONE",
        "idolResultID": "",
        "allDates": False,
    }

    response = requests.post(
        URL,
        json=payload,
        timeout=30,
    )
    response.raise_for_status()

    return response.json()


class_types = [
    {
        "documentClass": "Report/Form",
        "documentType": "Certificate of Compliance Report",
    }
]


data = search_elibrary(class_types)

print("Filtered eLibrary request successful")
print("Total matching records:", data.get("totalHits"))
print("Records returned:", len(data.get("searchHits", [])))
print()

for hit in data.get("searchHits", []):
    print(
        hit.get("acesssionNumber"),
        "|",
        hit.get("filedDate"),
        "|",
        hit.get("description"),
    )