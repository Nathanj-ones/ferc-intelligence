from pathlib import Path
import json

from ferc_filter.company_project_discovery import root_docket
from scripts.run_weekly_company_discovery import tracked_roots


registry_path = Path("config/project_registry.json")
registry = json.loads(registry_path.read_text(encoding="utf-8"))

print("=" * 90)
print("WEEKLY TRACKED-DOCKET DIAGNOSTIC")
print("=" * 90)

print()
print("WMB registry projects:")

projects = registry.get("projects") or []
for project in projects:
    owner = str(project.get("company_id") or "").strip().upper()
    if owner == "WMB":
        docket = project.get("docket")
        print(
            f"  company_id={owner!r} "
            f"docket={docket!r} "
            f"root={root_docket(docket)!r} "
            f"project={project.get('project')!r}"
        )

roots = tracked_roots(registry, "WMB")

print()
print("tracked_roots(WMB):")
for value in sorted(roots):
    print(" ", value)

print()
print("Expected roots:")
print("  CP25-10")
print("  CP21-465")
print("  CP17-101")

missing = {
    "CP25-10",
    "CP21-465",
    "CP17-101",
} - roots

print()
if not missing:
    print("PASS | Weekly runner sees all 3 Williams tracked projects")
else:
    print(
        "FAIL | Weekly runner is missing: "
        + ", ".join(sorted(missing))
    )

print()
print("Runner file:")
runner = Path("scripts/run_weekly_company_discovery.py")
print(runner)
print()
for i, line in enumerate(
    runner.read_text(encoding="utf-8").splitlines(),
    start=1,
):
    if 1 <= i <= 120 and (
        "def tracked_roots" in line
        or "project.get" in line
        or "owner" in line
        or "company_id" in line
    ):
        print(f"{i:4}: {line}")
