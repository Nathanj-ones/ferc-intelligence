from __future__ import annotations

import argparse
import json
from pathlib import Path

from ferc_filter.company_weekly_summary import (
    build_company_weekly_summary,
    company_weekly_summary_to_dict,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", required=True)
    args = parser.parse_args()

    company = args.company
    app_dir = Path("data") / "app" / company

    if not app_dir.exists():
        raise SystemExit(
            f"No app project directory found: {app_dir}"
        )

    files = sorted(app_dir.glob("*.json"))
    if not files:
        raise SystemExit(
            f"No app project JSON files found in {app_dir}"
        )

    projects = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in files
    ]

    summary = build_company_weekly_summary(
        company,
        projects,
    )
    payload = company_weekly_summary_to_dict(summary)

    out_dir = Path("data") / "weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{company}.json"
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("=" * 90)
    print(f"{company.upper()} - WEEKLY PROJECT SUMMARY")
    print("=" * 90)
    print(f"Projects: {summary.project_count}")
    print(f"Projects requiring attention: {summary.attention_count}")
    print(f"Partial-data projects: {summary.partial_count}")
    print()

    for item in summary.projects:
        print(f"{item.project} | {item.docket}")
        print(f"  Position: {item.headline or item.stage or '-'}")
        print(
            f"  Target in-service: "
            f"{item.target_in_service or 'not established'}"
        )
        if item.blocking_action:
            print(
                f"  Attention: {item.blocking_action} | "
                f"waiting on {item.waiting_on or 'unknown'}"
            )
        else:
            print("  Attention: no current blocking action")
        if item.latest_material_event:
            event = item.latest_material_event
            print(
                f"  Latest material event: "
                f"{event.get('title', '-')} | "
                f"{event.get('date', '-')}"
            )
        print(f"  Data quality: {item.data_quality}")
        print()

    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
