from __future__ import annotations

import argparse
import json
from pathlib import Path

from ferc_filter.company_project_discovery import (
    FERCDiscoveryClient,
    root_docket,
)
from ferc_filter.project_evidence_extractor import (
    extract_project_evidence,
    project_evidence_to_dict,
)


def main():
    parser = argparse.ArgumentParser(
        description="Extract explicit project evidence for a discovered docket."
    )
    parser.add_argument("--docket", required=True)
    parser.add_argument("--company-name", required=True)
    parser.add_argument("--days", type=int, default=1095)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/discovery/evidence"),
    )
    args = parser.parse_args()

    from ferc_filter.company_project_discovery import iso_window

    root = root_docket(args.docket)
    if not root:
        raise SystemExit(f"Invalid CP docket: {args.docket}")

    start_date, end_date = iso_window(args.days)
    client = FERCDiscoveryClient()
    hits = client.search_company(
        company_name=args.company_name,
        start_date=start_date,
        end_date=end_date,
    )

    docket_hits = [
        hit for hit in hits
        if root_docket(hit.docket) == root
    ]

    evidence = extract_project_evidence(root, docket_hits)

    print("=" * 100)
    print("FERC PROJECT EVIDENCE EXTRACTOR v0.1")
    print("=" * 100)
    print()
    print("Docket:", evidence.docket)
    print("Project:", evidence.project_name)
    print("Applicant:", evidence.applicant)
    print("Capex:", evidence.capex)
    print("Capacity:", evidence.capacity, evidence.capacity_unit)
    print("Target in-service:", evidence.target_in_service)
    print("Confidence:", evidence.confidence)
    print("Source filings:", len(evidence.source_accessions))

    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{root}.json"
    path.write_text(
        json.dumps(
            project_evidence_to_dict(evidence),
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print("Saved:", path)


if __name__ == "__main__":
    main()
