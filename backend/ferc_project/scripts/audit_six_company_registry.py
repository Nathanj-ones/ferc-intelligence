from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_json(path: Path):
    if not path.exists():
        raise SystemExit(f"Registry not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(
        description="Audit the FERC project registry before six-company expansion."
    )
    parser.add_argument(
        "--config",
        default="config/project_registry.json",
    )
    parser.add_argument(
        "--expected-companies",
        type=int,
        default=6,
    )
    args = parser.parse_args()

    path = Path(args.config)
    payload = load_json(path)

    companies = payload.get("companies", [])
    projects = payload.get("projects", [])

    if isinstance(companies, dict):
        companies = list(companies.values())
    if isinstance(projects, dict):
        projects = list(projects.values())

    by_company = {}
    for company in companies:
        company_id = str(company.get("company_id") or company.get("id") or "").strip()
        if company_id:
            by_company[company_id] = company

    project_counts = {company_id: 0 for company_id in by_company}
    missing_project_company = []
    duplicate_dockets = {}
    docket_seen = {}

    for project in projects:
        company_id = str(
            project.get("company_id")
            or project.get("company")
            or ""
        ).strip()
        docket = str(project.get("docket") or "").strip().upper()

        if company_id in project_counts:
            project_counts[company_id] += 1
        elif company_id:
            missing_project_company.append(company_id)

        if docket:
            docket_seen.setdefault(docket, []).append(company_id)

    for docket, owners in docket_seen.items():
        if len(owners) > 1:
            duplicate_dockets[docket] = owners

    print("=" * 100)
    print("SIX-COMPANY PROJECT REGISTRY AUDIT")
    print("=" * 100)
    print()
    print(f"Registry: {path}")
    print(f"Companies configured: {len(by_company)}")
    print(f"Projects configured: {len(projects)}")
    print()

    for company_id in sorted(by_company):
        company = by_company[company_id]
        name = company.get("name") or company_id
        print(
            f"{company_id:<10} | {name:<40} | "
            f"{project_counts.get(company_id, 0)} projects"
        )

    print()
    if len(by_company) < args.expected_companies:
        print(
            f"ATTENTION | Registry has {len(by_company)} companies; "
            f"{args.expected_companies} are expected."
        )
        print(
            "The six-company list must be supplied/configured before "
            "we invent or infer any company mapping."
        )
    elif len(by_company) == args.expected_companies:
        print(f"PASS | Exactly {args.expected_companies} companies configured")
    else:
        print(
            f"ATTENTION | Registry has {len(by_company)} companies, "
            f"more than expected {args.expected_companies}"
        )

    zero_project = [
        company_id for company_id, count in project_counts.items()
        if count == 0
    ]
    if zero_project:
        print(
            "ATTENTION | Companies with no projects: "
            + ", ".join(sorted(zero_project))
        )
    else:
        print("PASS | Every configured company has at least one project")

    if duplicate_dockets:
        print("ATTENTION | Duplicate docket mappings:")
        for docket, owners in sorted(duplicate_dockets.items()):
            print(f"  {docket}: {owners}")
    else:
        print("PASS | No duplicate docket mappings")

    if missing_project_company:
        print(
            "ATTENTION | Projects reference unconfigured companies: "
            + ", ".join(sorted(set(missing_project_company)))
        )
    else:
        print("PASS | All project company references are configured")

    print()
    print("Next step:")
    print(
        "Populate the remaining company/project mappings from the agreed "
        "six-company list, then run this audit again."
    )


if __name__ == "__main__":
    main()
