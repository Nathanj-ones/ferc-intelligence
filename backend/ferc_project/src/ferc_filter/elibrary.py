"""Client for retrieving filing metadata from FERC eLibrary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


ELIBRARY_DOCKET_URL = (
    "https://elibrary.ferc.gov/"
    "eLibraryWebAPI/api/Docket/GetSingleDocketSheet"
)


@dataclass
class DocketSearchResult:
    """Result returned from an eLibrary docket-sheet search."""

    records: list[dict[str, Any]]
    reported_total: int


class ELibraryClient:
    """Client for the FERC eLibrary docket-sheet backend."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()

        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def _request_page(
        self,
        docket: str,
        start_date: str,
        end_date: str,
        page_number: int,
        num_hits: int = 100,
        subdockets: str = "All",
        complete_flag: int = 0,
    ) -> dict[str, Any]:
        """Retrieve one page from the eLibrary docket sheet."""

        payload = {
            "dockets": docket,
            "subdockets": subdockets,
            "filed_date_beg": start_date,
            "filed_date_end": end_date,
            "complete_flag": complete_flag,
            "numHits": num_hits,
            "pageNumber": page_number,
        }

        response = self.session.post(
            ELIBRARY_DOCKET_URL,
            json=payload,
            timeout=self.timeout,
        )

        response.raise_for_status()

        data = response.json()

        if not data.get("success", True):
            raise RuntimeError(
                f"eLibrary docket request failed: "
                f"{data.get('errorMessage')}"
            )

        return data

    @staticmethod
    def _flatten_records(data: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Flatten eLibrary's DataList/DocumentsItem structure.

        The docket endpoint returns one DataList item per filing, with
        the actual filing record nested inside DocumentsItem.
        """

        records: list[dict[str, Any]] = []

        for item in data.get("DataList", []):
            for document in item.get("DocumentsItem", []):
                records.append(document)

        return records

    def get_docket(
        self,
        docket: str,
        start_date: str,
        end_date: str,
        subdockets: str = "All",
        complete_flag: int = 0,
        num_hits: int = 100,
    ) -> DocketSearchResult:
        """
        Retrieve all filing metadata for a docket.

        Dates must use the format expected by eLibrary, e.g.
        '01-01-2026' and '09-03-2026'.
        """

        all_records: list[dict[str, Any]] = []
        page_number = 0
        reported_total: int | None = None

        while True:
            data = self._request_page(
                docket=docket,
                start_date=start_date,
                end_date=end_date,
                page_number=page_number,
                num_hits=num_hits,
                subdockets=subdockets,
                complete_flag=complete_flag,
            )

            page_records = self._flatten_records(data)

            if reported_total is None:
                # The endpoint does not appear to expose a universal
                # top-level total field, so we'll infer it from the
                # first page if necessary.
                reported_total = data.get("totalHits")

            if not page_records:
                break

            all_records.extend(page_records)

            print(
                f"Page {page_number}: "
                f"{len(page_records)} records"
            )

            # A final page should contain fewer records than requested.
            if len(page_records) < num_hits:
                break

            page_number += 1

            # Safety guard while we're still validating the endpoint.
            if page_number >= 1000:
                raise RuntimeError(
                    "Stopped after 1000 pages. "
                    "Check eLibrary pagination behaviour."
                )

        # Deduplicate by accession number.
        unique_records: dict[str, dict[str, Any]] = {}

        for record in all_records:
            accession = record.get("accession_no")

            if accession:
                unique_records[accession] = record

        final_records = list(unique_records.values())

        if reported_total is None:
            reported_total = len(final_records)

        return DocketSearchResult(
            records=final_records,
            reported_total=reported_total,
        )