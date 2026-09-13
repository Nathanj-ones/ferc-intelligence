from pathlib import Path
import json
import re

REGISTRY = Path("config/project_registry.json")
RUNNER = Path("scripts/run_weekly_company_discovery.py")

BAD_SEED_DOCKETS = {
    "LNG": {"CP24-75", "CP25-505", "CP25-506", "CP26-82", "CP26-87"},
    "TRGP": {"CP26-34", "CP26-35"},
}


def root_docket(value):
    match = re.search(
        r"\b(CP\d{2}-\d+)(?:-\d{3})?\b",
        str(value or "").upper(),
    )
    return match.group(1) if match else None


payload = json.loads(REGISTRY.read_text(encoding="utf-8"))
projects = payload.get("projects") or []

kept = []
removed = []

for project in projects:
    company_id = str(project.get("company_id") or "").upper()
    docket = root_docket(project.get("docket"))
    project_name = str(project.get("project") or "").strip()

    if (
        company_id in BAD_SEED_DOCKETS
        and docket in BAD_SEED_DOCKETS[company_id]
        and project_name == "Southeast Supply Enhancement"
    ):
        removed.append((company_id, project.get("docket"), project_name))
    else:
        kept.append(project)

payload["projects"] = kept
REGISTRY.write_text(
    json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)

text = RUNNER.read_text(encoding="utf-8")

start = text.find("def tracked_roots(")
end = text.find("\n\ndef candidate_groups", start)

if start < 0 or end < 0:
    raise SystemExit("Could not locate tracked_roots() in weekly runner.")

new_func = '''def tracked_roots(
    registry: dict[str, Any],
    company_id: str,
) -> set[str]:
    # Exact project_registry.json schema:
    # company_id + docket
    projects = registry.get("projects") or []
    if isinstance(projects, dict):
        projects = list(projects.values())

    wanted = company_id.upper()
    result = set()

    for project in projects:
        owner = str(project.get("company_id") or "").strip().upper()
        if owner != wanted:
            continue

        docket = root_docket(project.get("docket"))
        if docket:
            result.add(docket)

    return result
'''

text = text[:start] + new_func + text[end:]
compile(text, str(RUNNER), "exec")
RUNNER.write_text(text, encoding="utf-8")

print("=" * 90)
print("REGISTRY + WEEKLY LOOKUP REPAIR")
print("=" * 90)

for company_id, docket, project_name in removed:
    print(
        f"REMOVED | {company_id} | {docket} | {project_name} "
        f"(incorrect seeded record)"
    )

if not removed:
    print("No incorrect seeded records matched.")

print()
print(f"Registry projects after repair: {len(payload['projects'])}")
print("PASS | Weekly discovery uses exact company_id/docket schema")
print()
print("Next:")
print("  python scripts/audit_six_company_registry.py")
print("  python scripts/run_weekly_company_discovery.py --company WMB --days 30")
