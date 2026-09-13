from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import re


REGISTRY_PATH = Path("config/project_registry.json")
DISCOVERY_CACHE = Path("data/discovery")


def normalize_docket(value: str) -> str | None:
    if not value:
        return None
    match = re.search(r"\bCP\d{2}-\d+(?:-\d{3})?\b", value.upper())
    return match.group(0) if match else None


def normalize_company(value: str) -> str:
    return str(value or "").strip().upper()


def record_text(record: dict) -> str:
    fields = (
        "applicant_name",
        "applicant",
        "company_name",
        "project_name",
        "description",
        "title",
        "document_title",
    )
    return " ".join(
        str(record.get(field) or "")
        for field in fields
    )


def discover_candidate_dockets(
    records: list[dict],
    company_aliases: list[str],
) -> dict[str, dict]:
    aliases = [a.upper() for a in company_aliases]
    candidates = {}

    for record in records:
        text = record_text(record).upper()

        if aliases and not any(alias in text for alias in aliases):
            continue

        docket = normalize_docket(
            str(record.get("docket") or "")
            or str(record.get("docket_no") or "")
            or str(record.get("docketNumber") or "")
            or text
        )
        if not docket:
            continue

        item = candidates.setdefault(
            docket,
            {
                "docket": docket,
                "records": 0,
                "latest_date": None,
                "project_names": set(),
                "sample_titles": [],
            },
        )

        item["records"] += 1

        date = (
            record.get("filed_date")
            or record.get("filedDate")
            or record.get("issued_date")
            or record.get("issuedDate")
        )
        if date:
            date_text = str(date)
            if (
                item["latest_date"] is None
                or date_text > item["latest_date"]
            ):
                item["latest_date"] = date_text

        for field in ("project_name", "projectName"):
            value = str(record.get(field) or "").strip()
            if value:
                item["project_names"].add(value)

        title = str(
            record.get("title")
            or record.get("document_title")
            or ""
        ).strip()
        if title and len(item["sample_titles"]) < 5:
            item["sample_titles"].append(title)

    for item in candidates.values():
        item["project_names"] = sorted(item["project_names"])

    return candidates


def load_registry():
    if not REGISTRY_PATH.exists():
        raise SystemExit(f"Registry not found: {REGISTRY_PATH}")
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def tracked_dockets(registry, company_id: str) -> set[str]:
    projects = registry.get("projects", [])
    if isinstance(projects, dict):
        projects = list(projects.values())

    result = set()
    for project in projects:
        owner = normalize_company(
            project.get("company_id")
            or project.get("company")
            or project.get("ticker")
        )
        if owner != company_id:
            continue

        docket = normalize_docket(
            str(project.get("docket") or project.get("docket_number") or "")
        )
        if docket:
            result.add(docket)

    return result


def load_records(company_id: str, input_path: Path | None):
    if input_path:
        return json.loads(input_path.read_text(encoding="utf-8"))

    company_dir = DISCOVERY_CACHE / company_id
    if not company_dir.exists():
        raise SystemExit(
            f"No discovery input found for {company_id}: {company_dir}\n"
            f"Pass --input with a JSON export from eLibrary."
        )

    files = sorted(company_dir.glob("*.json"))
    records = []
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            records.extend(payload)
        elif isinstance(payload, dict):
            for key in ("records", "results", "data"):
                if isinstance(payload.get(key), list):
                    records.extend(payload[key])
                    break

    return records


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Discover FERC project/docket candidates for a company without "
            "hardcoding the project list."
        )
    )
    parser.add_argument("--company", required=True)
    parser.add_argument(
        "--alias",
        action="append",
        default=[],
        help="Company/applicant text alias; may be repeated.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="JSON export containing eLibrary records.",
    )
    args = parser.parse_args()

    company = normalize_company(args.company)
    aliases = args.alias or [company]

    registry = load_registry()
    tracked = tracked_dockets(registry, company)
    records = load_records(company, args.input)

    candidates = discover_candidate_dockets(records, aliases)

    print("=" * 100)
    print("FERC COMPANY PROJECT DISCOVERY v0.1")
    print("=" * 100)
    print()
    print(f"Company: {company}")
    print(f"Records scanned: {len(records)}")
    print(f"Tracked dockets: {len(tracked)}")
    print(f"Candidate dockets discovered: {len(candidates)}")
    print()

    new_count = 0
    for docket, item in sorted(candidates.items()):
        status = "TRACKED" if docket in tracked else "NEW CANDIDATE"
        if status == "NEW CANDIDATE":
            new_count += 1

        print(f"{status:13} | {docket}")
        if item["project_names"]:
            print("  Project names:", "; ".join(item["project_names"][:3]))
        print("  Records:", item["records"])
        print("  Latest:", item["latest_date"] or "unknown")
        for title in item["sample_titles"][:3]:
            print("  Filing:", title)
        print()

    print(f"New candidate dockets: {new_count}")
    print()
    print(
        "This tool discovers candidate dockets from filings. It does not "
        "automatically promote every candidate into the tracked project "
        "registry; project/materiality classification remains a separate step."
    )


if __name__ == "__main__":
    main()
