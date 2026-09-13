from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from ferc_filter.company_project_discovery import FERCDiscoveryClient, root_docket


def recent_cp_search(
    client: FERCDiscoveryClient,
    start_date: str,
    end_date: str,
    per_page: int = 100,
    max_pages: int | None = None,
):
    def body(page: int):
        return {
            "searchText": "*",
            "searchFullText": False,
            "searchDescription": True,
            "dateSearches": [{
                "dateType": "filed_date",
                "startDate": start_date,
                "endDate": end_date,
            }],
            "availability": None,
            "affiliations": [],
            "categories": [],
            "libraries": [],
            "accessionNumber": None,
            "eFiling": False,
            "docketSearches": [],
            "resultsPerPage": per_page,
            "curPage": page,
            "classTypes": [],
            "sortBy": "",
            "groupBy": "NONE",
            "idolResultID": "",
            "allDates": False,
        }

    first = client._post("Search/AdvancedSearch", body(1))
    total = int(first.get("totalHits") or 0)
    pages = max(1, (total + per_page - 1) // per_page)
    if max_pages is not None:
        pages = min(pages, max_pages)

    hits = [client._normalize_hit(x) for x in (first.get("searchHits") or [])]
    for page in range(2, pages + 1):
        payload = client._post("Search/AdvancedSearch", body(page))
        hits.extend(client._normalize_hit(x) for x in (payload.get("searchHits") or []))

    return [hit for hit in hits if root_docket(hit.docket)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--out", type=Path, default=Path("data/discovery/recent_cp_feed.json"))
    args = parser.parse_args()

    end = date.today()
    start = end - timedelta(days=args.days)
    client = FERCDiscoveryClient()
    hits = recent_cp_search(
        client,
        start.strftime("%m/%d/%Y"),
        end.strftime("%m/%d/%Y"),
        max_pages=args.max_pages,
    )

    dockets = sorted({root_docket(hit.docket) for hit in hits if root_docket(hit.docket)})
    payload = {
        "schema_version": "recent_cp_feed_v03",
        "window": {
            "start_date": start.strftime("%m/%d/%Y"),
            "end_date": end.strftime("%m/%d/%Y"),
        },
        "records": [
            {
                "accession": hit.accession,
                "docket": root_docket(hit.docket),
                "filed_date": hit.filed_date,
                "description": hit.description,
                "applicant": hit.applicant,
                "category": hit.category,
                "document_class": hit.document_class,
                "document_type": hit.document_type,
            }
            for hit in hits
        ],
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print("=" * 90)
    print("RECENT FERC CP FEED v0.3")
    print("=" * 90)
    print(f"Window: {start.strftime('%m/%d/%Y')} -> {end.strftime('%m/%d/%Y')}")
    print("Records:", len(hits))
    print("Root CP dockets:", len(dockets))
    print("Saved:", args.out)
    print()
    for hit in hits[:30]:
        print(f"{root_docket(hit.docket)} | {hit.applicant or 'applicant unknown'} | {hit.filed_date}")
        print("  ", hit.description[:220])


if __name__ == "__main__":
    main()
