from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROJECT_ROOT = ROOT / "backend" / "ferc_project" / "data" / "projects"
DEFAULT_OUTPUT = ROOT / "lib" / "ferc" / "projects-live.json"


def company_from_path(path: Path, project_root: Path) -> str:
    relative = path.relative_to(project_root)
    if len(relative.parts) < 3:
        raise ValueError(f"Unexpected project-view path: {path}")
    return relative.parts[0]


def normalize_project_view(payload: dict[str, Any], company: str) -> dict[str, Any]:
    counts = payload.get("counts") or {}
    outlook = payload.get("regulatory_outlook")

    return {
        "project": str(payload.get("project") or payload.get("name") or ""),
        "docket": str(payload.get("docket") or ""),
        "current_stage": str(payload.get("current_stage") or "Unknown"),
        "generated_at": str(payload.get("generated_at") or ""),
        "company": company,
        "counts": {
            "raw": int(counts.get("raw") or 0),
            "deduped": int(counts.get("deduped") or 0),
            "alerts": int(counts.get("alerts") or 0),
            "review": int(counts.get("review") or 0),
            "suppressed": int(counts.get("suppressed") or 0),
            "activity_groups": int(counts.get("activity_groups") or 0),
        },
        "milestones": payload.get("milestones") or [],
        "regulatory_outlook": outlook,
        "activity": payload.get("activity") or [],
    }


def build(project_root: Path, output: Path) -> list[dict[str, Any]]:
    if not project_root.exists():
        raise SystemExit(f"Project data root not found: {project_root}")

    views = []
    files = sorted(project_root.glob("**/project_view.json"))

    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        company = company_from_path(path, project_root)
        view = normalize_project_view(payload, company)
        if not view["docket"]:
            print(f"SKIP | missing docket | {path}")
            continue
        views.append(view)
        print(
            f"ADD  | {company:<20} | {view['docket']:<12} | "
            f"{view['project']}"
        )

    views.sort(key=lambda item: (item["company"], item["project"], item["docket"]))

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(views, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print()
    print(f"Projects exported: {len(views)}")
    print(f"Saved: {output.relative_to(ROOT)}")
    return views


def main():
    parser = argparse.ArgumentParser(
        description="Export ferc_project ProjectView JSON for Nathan's dashboard."
    )
    parser.add_argument("--project-root", type=Path, default=DEFAULT_PROJECT_ROOT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    build(args.project_root, args.out)


if __name__ == "__main__":
    main()
