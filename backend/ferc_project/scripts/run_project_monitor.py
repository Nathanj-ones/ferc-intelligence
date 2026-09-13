from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from ferc_filter.company_weekly_summary import (
    build_company_weekly_summary,
    company_weekly_summary_to_dict,
)
from ferc_filter.project_changes import (
    compare_project_snapshots,
    changes_to_dict,
)
from ferc_filter.project_monitor import ProjectMonitor
from ferc_filter.project_registry import load_registry


def _load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _load_current_app_projects(
    company: str,
    dockets: set[str],
) -> list[dict]:
    app_dir = Path("data") / "app" / company
    projects = []

    for path in sorted(app_dir.glob("*.json")):
        payload = _load_json(path, {})
        if payload.get("docket") in dockets:
            projects.append(payload)

    return projects


def _build_weekly_outputs(
    company: str,
    projects: list[dict],
) -> tuple[Path, Path, Path]:
    weekly_dir = Path("data") / "weekly"
    weekly_dir.mkdir(parents=True, exist_ok=True)

    snapshot_path = weekly_dir / f"{company}_snapshot.json"
    legacy_previous_path = weekly_dir / f"{company}_previous.json"

    previous = _load_json(snapshot_path, {})
    if not previous and legacy_previous_path.exists():
        previous = _load_json(legacy_previous_path, {})

    previous_by_docket = {
        item.get("docket"): item
        for item in previous.get("projects", [])
        if item.get("docket")
    }

    changes = []
    for project in projects:
        changes.extend(
            compare_project_snapshots(
                previous_by_docket.get(project.get("docket")),
                project,
            )
        )

    summary = build_company_weekly_summary(
        company,
        projects,
    )

    summary_path = weekly_dir / f"{company}.json"
    changes_path = weekly_dir / f"{company}_changes.json"

    _save_json(
        summary_path,
        company_weekly_summary_to_dict(summary),
    )
    _save_json(
        changes_path,
        {
            "schema_version": "company_weekly_changes_v0.1",
            "company": company,
            "generated_at": datetime.now().isoformat(
                timespec="seconds"
            ),
            "project_count": len(projects),
            "changes": changes_to_dict(changes),
        },
    )
    snapshot_payload = {
        "schema_version": "company_project_snapshot_v0.1",
        "company": company,
        "generated_at": datetime.now().isoformat(
            timespec="seconds"
        ),
        "projects": projects,
    }

    # Write the new baseline only after summary/change generation succeeds.
    # Use a temporary file then replace, so an interrupted run cannot leave
    # a partially written weekly baseline.
    temp_snapshot_path = weekly_dir / f".{company}_snapshot.tmp"
    _save_json(temp_snapshot_path, snapshot_payload)
    temp_snapshot_path.replace(snapshot_path)

    return summary_path, changes_path, snapshot_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the registered FERC project monitor and "
            "build the weekly company snapshot/change outputs."
        )
    )
    parser.add_argument(
        "--config",
        default="config/project_registry.json",
    )
    parser.add_argument("--company")
    parser.add_argument("--docket")
    args = parser.parse_args()

    registry = load_registry(Path(args.config))
    projects = registry.enabled_projects()

    if args.company:
        projects = [
            item for item in projects
            if item.company_id.upper() == args.company.upper()
        ]

    if args.docket:
        projects = [
            item for item in projects
            if item.docket.upper() == args.docket.upper()
        ]

    if not projects:
        raise SystemExit(
            "No enabled projects matched the requested scope."
        )

    company_map = registry.company_map()
    companies = {
        company_map[item.company_id].name
        for item in projects
    }

    if len(companies) != 1:
        raise SystemExit(
            "Weekly company output requires exactly one company scope."
        )

    company = next(iter(companies))
    monitor = ProjectMonitor(registry)

    print("=" * 90)
    print("FERC PROJECT MONITOR")
    print("=" * 90)
    print(f"Projects: {len(projects)}")

    results = []
    for project in projects:
        print()
        print(f"{project.project} | {project.docket}")
        try:
            result = monitor.run_project(project)
            result["run_status"] = "ok"
            results.append(result)
        except Exception as exc:
            print(
                f"  Project failed: {project.docket} | {exc}"
            )
            results.append(
                {
                    "company": company,
                    "project": project.project,
                    "docket": project.docket,
                    "raw_records": 0,
                    "app_path": None,
                    "project_view_path": None,
                    "run_status": "failed",
                    "error": str(exc),
                }
            )

    dockets = {item.docket for item in projects}
    app_projects = _load_current_app_projects(
        company,
        dockets,
    )

    summary_path = changes_path = snapshot_path = None
    if app_projects:
        (
            summary_path,
            changes_path,
            snapshot_path,
        ) = _build_weekly_outputs(
            company,
            app_projects,
        )

    print()
    print("=" * 90)
    print("MONITOR COMPLETE")
    print("=" * 90)

    completed = 0
    failed = 0

    for result in results:
        status = result.get("run_status", "unknown")
        if status == "ok":
            completed += 1
        else:
            failed += 1

        print(
            f"{result['company']} | {result['project']} | "
            f"{result['docket']} | "
            f"{result.get('raw_records', 0)} records | "
            f"{status}"
        )
        if result.get("app_path"):
            print(f"  App: {result['app_path']}")
        if result.get("project_view_path"):
            print(
                f"  ProjectView: "
                f"{result['project_view_path']}"
            )
        if result.get("error"):
            print(f"  Error: {result['error']}")

    print()
    print(f"Completed: {completed}")
    print(f"Failed: {failed}")

    if summary_path:
        print(f"Weekly summary: {summary_path}")
        print(f"Weekly changes: {changes_path}")
        print(f"Weekly snapshot: {snapshot_path}")


if __name__ == "__main__":
    main()
