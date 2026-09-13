import json

import requests

from ferc_filter.config import get_api_key


URL = "https://elibrary.ferc.gov/eLibraryWebAPI/api/Search/AdvancedSearch"


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
    "resultsPerPage": 10,
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

print("eLibrary request successful")
print("Top-level keys:", list(data.keys()))
print()

# Print a small, readable sample of the response.
print(json.dumps(data, indent=2)[:12000])