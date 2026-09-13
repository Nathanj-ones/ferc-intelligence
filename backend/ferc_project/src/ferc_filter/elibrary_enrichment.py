"""Enrichment client for FERC eLibrary filing metadata."""

from __future__ import annotations

from typing import Any

import requests


ELIBRARY_ADVANCED_SEARCH_URL = (
    "https://elibrary.ferc.gov/"
    "eLibraryWebAPI/api/Search/AdvancedSearch"
)


class ELibraryEnrichmentClient:
    """Retrieve detailed eLibrary metadata by accession number."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()

        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def get_by_accession(
        self,
        accession_number: str,
    ) -> dict[str, Any] | None:
        """Retrieve the single eLibrary record for an accession number."""

        payload = {
            "searchText": "*",
            "searchFullText": True,
            "searchDescription": True,
            "dateSearches": [],
            "availability": None,
            "affiliations": [],
            "categories": [],
            "libraries": [],
            "accessionNumber": accession_number,
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

        response = self.session.post(
            ELIBRARY_ADVANCED_SEARCH_URL,
            json=payload,
            timeout=self.timeout,
        )

        response.raise_for_status()

        data = response.json()

        if not data.get("success", True):
            raise RuntimeError(
                "eLibrary enrichment request failed: "
                f"{data.get('errorMessage')}"
            )

        hits = data.get("searchHits", [])

        if not hits:
            return None

        # An accession lookup should normally return one record.
        return hits[0]