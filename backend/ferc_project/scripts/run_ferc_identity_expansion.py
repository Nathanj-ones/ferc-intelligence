from __future__ import annotations

import argparse

from ferc_filter.company_project_discovery import (
    FERCDiscoveryClient,
    iso_window,
    root_docket,
)
from ferc_filter.ferc_identity_expansion import expand_identities


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--company-name", required=True)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--max-rounds", type=int, default=3)
    args = parser.parse_args()

    start_date, end_date = iso_window(args.days)
    client = FERCDiscoveryClient()

    def search(identity):
        print("SEARCH |", identity)
        return client.search_company(
            company_name=identity,
            start_date=start_date,
            end_date=end_date,
        )

    result = expand_identities(
        [args.company_name],
        search,
        minimum_filings=2,
        max_rounds=args.max_rounds,
    )

    dockets = sorted({
        root_docket(filing.docket)
        for filing in result.filings
        if root_docket(filing.docket)
    })

    print()
    print("=" * 90)
    print("ITERATIVE FERC IDENTITY EXPANSION v0.1")
    print("=" * 90)
    print("Rounds:", result.rounds)
    print("Filings:", len(result.filings))
    print("Identities:", len(result.identities))
    for identity in result.identities:
        print("  -", identity)
    print("CP docket families:", len(dockets))
    for docket in dockets:
        print("  *", docket)


if __name__ == "__main__":
    main()
