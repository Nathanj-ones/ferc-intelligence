from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ferc_filter.company_project_discovery import (
    FERCDiscoveryClient,
    ProjectCandidate,
    iso_window,
    root_docket,
)
from ferc_filter.project_candidate_qualifier import qualify_candidate
from ferc_filter.project_materiality import assess_materiality
from ferc_filter.project_evidence_extractor import (
    extract_project_evidence,
    project_evidence_to_dict,
)


def load_candidates(path: Path) -> list[ProjectCandidate]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        ProjectCandidate(**item)
        for item in payload.get("candidates", [])
    ]


def registry_roots(
    registry_path: Path,
    company_id: str,
) -> set[str]:
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    projects = payload.get("projects") or []
    if isinstance(projects, dict):
        projects = list(projects.values())

    roots = set()
    for project in projects:
        owner = str(project.get("company_id") or "").upper()
        if owner != company_id.upper():
            continue
        docket = root_docket(
            str(
                project.get("docket")
                or project.get("docket_number")
                or ""
            )
        )
        if docket:
            roots.add(docket)
    return roots


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Automatically enrich qualified FERC project candidates with "
            "explicit filing evidence before materiality decisions."
        )
    )
    parser.add_argument("--company", required=True)
    parser.add_argument("--company-name", required=True)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("config/project_registry.json"),
    )
    parser.add_argument(
        "--days",
        type=int,
        default=1095,
        help="FERC filing search window for project evidence.",
    )
    parser.add_argument(
        "--out",
        type=Path,
    )
    args = parser.parse_args()

    candidates = load_candidates(args.discovery)
    tracked = registry_roots(args.registry, args.company)

    start_date, end_date = iso_window(args.days)
    client = FERCDiscoveryClient()

    print("=" * 100)
    print("AUTOMATIC PROJECT EVIDENCE + MATERIALITY v0.1")
    print("=" * 100)
    print()
    print(f"Company: {args.company_name} ({args.company.upper()})")
    print(f"Evidence window: {start_date} -> {end_date}")
    print()

    results: list[dict[str, Any]] = []

    for candidate in candidates:
        root = root_docket(candidate.docket) or candidate.docket

        if root in tracked or candidate.tracked:
            results.append(
                {
                    "docket": root,
                    "status": "already_tracked",
                }
            )
            continue

        qualification = qualify_candidate(candidate)

        # Only investigate non-ignored candidates. This keeps discovery broad
        # but avoids expensive enrichment for routine/legacy dockets.
        if qualification.decision == "IGNORE":
            results.append(
                {
                    "docket": root,
                    "status": "qualification_ignore",
                    "qualification": qualification.__dict__,
                }
            )
            continue

        hits = client.search_company(
            company_name=args.company_name,
            start_date=start_date,
            end_date=end_date,
        )
        docket_hits = [
            hit for hit in hits
            if root_docket(hit.docket) == root
        ]

        evidence = extract_project_evidence(
            root,
            docket_hits,
        )

        materiality = assess_materiality(
            candidate,
            qualification,
            capex=evidence.capex,
            capacity=evidence.capacity,
            target_in_service=evidence.target_in_service,
        )

        results.append(
            {
                "docket": root,
                "status": "evaluated",
                "qualification": qualification.__dict__,
                "evidence": project_evidence_to_dict(evidence),
                "materiality": materiality.__dict__,
            }
        )

        print(
            f"{materiality.decision:5} | {root:<12} | "
            f"{evidence.project_name or 'name unavailable'}"
        )
        print("  Capex:", evidence.capex)
        print("  Capacity:", evidence.capacity, evidence.capacity_unit)
        print("  In-service:", evidence.target_in_service)
        print("  Evidence confidence:", evidence.confidence)
        print()

    out = args.out or (
        Path("data/discovery") /
        f"{args.company.upper()}_evidence_materiality.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(results, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("Saved:", out)


if __name__ == "__main__":
    main()
