from __future__ import annotations

import argparse
import json
from pathlib import Path

from ferc_filter.company_project_discovery import (
    FERCDiscoveryClient,
    discover_projects,
    iso_window,
    load_registry,
    tracked_dockets,
    write_discovery,
)


def main():
    parser = argparse.ArgumentParser(
        description="Discover project dockets directly from FERC eLibrary."
    )
    parser.add_argument("--company", required=True)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("config/project_registry.json"),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/discovery"),
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=4,
    )
    args = parser.parse_args()

    registry = load_registry(args.registry)
    company = next(
        (
            item
            for item in registry.get("companies", [])
            if str(item.get("id") or "").upper()
            == args.company.upper()
        ),
        None,
    )

    if not company:
        raise SystemExit(f"Unknown company: {args.company}")

    company_name = str(company.get("name") or "").strip()
    if not company_name:
        raise SystemExit(
            f"Company {args.company} has no name; discovery cannot run."
        )

    start_date, end_date = iso_window(args.days)
    client = FERCDiscoveryClient()

    print("=" * 100)
    print("FERC COMPANY PROJECT DISCOVERY v1")
    print("=" * 100)
    print()
    print(f"Company: {company_name} ({args.company.upper()})")
    print(f"Window: {start_date} -> {end_date}")
    print()

    filings = client.search_company(
        company_name=company_name,
        start_date=start_date,
        end_date=end_date,
    )
    tracked = tracked_dockets(
        registry,
        args.company.upper(),
    )

    candidates = discover_projects(
        company_id=args.company.upper(),
        company_name=company_name,
        filings=filings,
        tracked_dockets=tracked,
        minimum_score=args.min_score,
    )

    for candidate in candidates:
        state = "TRACKED" if candidate.tracked else "NEW CANDIDATE"
        print(
            f"{state:13} | {candidate.docket:<12} | "
            f"score={candidate.score} | filings={candidate.filing_count}"
        )
        if candidate.project_names:
            print(
                "  Project:",
                "; ".join(candidate.project_names[:3]),
            )
        print("  Reasons:", ", ".join(candidate.reasons))
        if candidate.latest_filing_date:
            print("  Latest:", candidate.latest_filing_date)
        for sample in candidate.sample_filings[:2]:
            print("  Filing:", sample[:220])
        print()

    output = args.out / f"{args.company.upper()}.json"
    write_discovery(
        output,
        args.company.upper(),
        company_name,
        candidates,
    )

    print(f"Filings scanned: {len(filings)}")
    print(f"Candidates: {len(candidates)}")
    print(f"New candidates: {sum(not c.tracked for c in candidates)}")
    print(f"Saved: {output}")


if __name__ == "__main__":
    main()
