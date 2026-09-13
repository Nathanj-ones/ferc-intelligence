from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from ferc_filter.project_changes import (
    compare_project_snapshots,
    changes_to_dict,
    load_previous_weekly,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", required=True)
    args = parser.parse_args()

    company = args.company
    app_dir = Path("data") / "app" / company
    weekly_dir = Path("data") / "weekly"
    current_path = weekly_dir / f"{company}.json"
    previous_path = weekly_dir / f"{company}_previous.json"

    if not app_dir.exists():
        raise SystemExit(f"No app project directory found: {app_dir}")

    files = sorted(app_dir.glob("*.json"))
    if not files:
        raise SystemExit(f"No app project JSON files found in {app_dir}")

    current_projects = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in files
    ]

    previous_projects = load_previous_weekly(previous_path)
    all_changes = []

    for project in current_projects:
        previous = previous_projects.get(project.get("docket"))
        all_changes.extend(
            compare_project_snapshots(previous, project)
        )

    payload = {
        "schema_version": "company_weekly_changes_v0.1",
        "company": company,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "project_count": len(current_projects),
        "changes": changes_to_dict(all_changes),
    }

    weekly_dir.mkdir(parents=True, exist_ok=True)
    out_path = weekly_dir / f"{company}_changes.json"
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("=" * 90)
    print(f"{company.upper()} - WEEKLY CHANGES")
    print("=" * 90)

    for change in all_changes:
        print(
            f"{change.project} | {change.docket} | "
            f"{change.change_type} | {change.summary}"
        )

    print()
    print(f"Changes: {len(all_changes)}")
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
