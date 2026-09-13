from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from ferc_filter.discovery_monitor_integration import (
    promote_pipeline_tracks,
    save_registry,
)


REGISTRY = Path("config/project_registry.json")
DISCOVERY_DIR = Path("data/discovery")
RECENT_CP_FEED = DISCOVERY_DIR / "recent_cp_feed.json"


def load_registry(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def enabled_companies(registry: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for company in registry.get("companies", []):
        if company.get("enabled") is not True:
            continue
        company_id = str(company.get("id") or "").strip()
        company_name = str(company.get("name") or "").strip()
        if not company_id or not company_name:
            continue
        result.append(company)
    return result


def enabled_project_company_ids(registry: dict[str, Any]) -> set[str]:
    return {
        str(project.get("company_id") or "").strip().upper()
        for project in registry.get("projects", [])
        if project.get("enabled") is True
        and str(project.get("company_id") or "").strip()
    }


def run_command(command: list[str]) -> int:
    print()
    print("$", " ".join(command))
    completed = subprocess.run(command)
    return completed.returncode


def load_weekly_pipeline(company_id: str) -> dict[str, Any]:
    path = DISCOVERY_DIR / company_id / "weekly_pipeline.json"
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Run weekly discovery for every enabled company, optionally "
            "promote TRACK projects, then run the existing project monitor."
        )
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Promote TRACK projects and run the project monitor.",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=REGISTRY,
    )
    parser.add_argument(
        "--skip-reverse-cp",
        action="store_true",
        help="Skip the FERC-wide recent CP feed and use forward company search only.",
    )
    args = parser.parse_args()

    registry = load_registry(args.registry)
    companies = enabled_companies(registry)

    print("=" * 100)
    print("ALL-COMPANY WEEKLY FERC LOOP v0.1")
    print("=" * 100)
    print()
    print("Enabled companies:", len(companies))
    print(
        "Mode:",
        "APPLY + MONITOR" if args.apply else "DRY RUN",
    )

    failures = []
    warnings = []
    promoted_total = 0
    monitor_skipped_no_projects = []
    monitored_companies = []

    reverse_feed_ready = False
    if not args.skip_reverse_cp:
        print()
        print("Building shared recent CP feed for reverse discovery...")
        reverse_rc = run_command(
            [
                sys.executable,
                "scripts/run_recent_cp_feed.py",
                "--days",
                str(args.days),
                "--out",
                str(RECENT_CP_FEED),
            ]
        )
        reverse_feed_ready = reverse_rc == 0 and RECENT_CP_FEED.exists()
        if reverse_feed_ready:
            print(f"REVERSE CP READY | {RECENT_CP_FEED}")
        else:
            warnings.append(
                {
                    "stage": "reverse_cp_feed",
                    "returncode": reverse_rc,
                    "message": "Reverse CP feed unavailable; continued with forward discovery only.",
                }
            )
            print("WARN | reverse CP feed unavailable; continuing forward-only")

    for company in companies:
        company_id = company["id"]
        company_name = company["name"]

        print()
        print("-" * 100)
        print(f"{company_name} ({company_id})")
        print("-" * 100)

        discovery_command = [
            sys.executable,
            "scripts/run_weekly_company_discovery.py",
            "--company",
            company_id,
            "--days",
            str(args.days),
        ]
        if reverse_feed_ready:
            discovery_command.extend(["--reverse-feed", str(RECENT_CP_FEED)])

        rc = run_command(discovery_command)

        if rc != 0:
            failures.append(
                {
                    "company_id": company_id,
                    "stage": "discovery",
                    "returncode": rc,
                }
            )
            print(
                f"FAIL | {company_id} discovery failed; "
                "continuing to next company."
            )
            continue

        pipeline = load_weekly_pipeline(company_id)

        if args.apply:
            # Reload before each company so previous promotions are retained.
            registry = load_registry(args.registry)
            added = promote_pipeline_tracks(
                registry,
                company_id,
                pipeline,
            )
            if added:
                save_registry(args.registry, registry)
            promoted_total += len(added)
            print(
                f"PROMOTED | {company_id} | "
                f"{len(added)} new project(s)"
            )
        else:
            proposed = [
                item
                for item in pipeline.get("evaluated", [])
                if (
                    item.get("materiality") or {}
                ).get("decision") == "TRACK"
            ]
            print(
                f"DRY RUN | {company_id} | "
                f"{len(proposed)} TRACK proposal(s)"
            )

    monitor_failures = []

    if args.apply:
        # Run each enabled company separately. One company failure must not
        # prevent the others from completing.
        registry = load_registry(args.registry)
        project_company_ids = enabled_project_company_ids(registry)
        for company in enabled_companies(registry):
            company_id = company["id"]

            if company_id.upper() not in project_company_ids:
                monitor_skipped_no_projects.append(company_id)
                print()
                print(
                    f"SKIP | {company_id} | "
                    "no enabled tracked projects"
                )
                continue

            rc = run_command(
                [
                    sys.executable,
                    "scripts/run_project_monitor.py",
                    "--company",
                    company_id,
                ]
            )

            if rc != 0:
                monitor_failures.append(company_id)
                failures.append(
                    {
                        "company_id": company_id,
                        "stage": "monitor",
                        "returncode": rc,
                    }
                )
            else:
                monitored_companies.append(company_id)

    summary = {
        "companies_attempted": len(companies),
        "promoted_projects": promoted_total,
        "failures": failures,
        "warnings": warnings,
        "reverse_cp_enabled": reverse_feed_ready,
        "monitor_failures": monitor_failures,
        "monitored_companies": monitored_companies,
        "monitor_skipped_no_projects": monitor_skipped_no_projects,
        "mode": "apply" if args.apply else "dry_run",
    }

    out = Path("data/weekly/all_company_run.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    print()
    print("=" * 100)
    print("ALL-COMPANY WEEKLY LOOP COMPLETE")
    print("=" * 100)
    print()
    print("Companies attempted:", len(companies))
    if args.apply:
        print("Companies monitored:", len(monitored_companies))
        print(
            "Companies with no tracked projects:",
            len(monitor_skipped_no_projects),
        )
    print("Projects promoted:", promoted_total)
    print("Warnings:", len(warnings))
    print("Failures:", len(failures))
    print("Saved:", out)

    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
