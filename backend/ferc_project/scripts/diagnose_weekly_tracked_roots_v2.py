from pathlib import Path
import json
import re


def root_docket(value):
    if not value:
        return None
    match = re.search(
        r"\b(CP\d{2}-\d+)(?:-\d{3})?\b",
        str(value).upper(),
    )
    return match.group(1) if match else None


registry_path = Path("config/project_registry.json")
registry = json.loads(registry_path.read_text(encoding="utf-8"))

print("=" * 90)
print("WEEKLY TRACKED-DOCKET DIAGNOSTIC v2")
print("=" * 90)

projects = registry.get("projects") or []

roots = set()

print()
print("WMB registry projects:")
for project in projects:
    owner = str(project.get("company_id") or "").strip().upper()
    if owner != "WMB":
        continue

    docket = project.get("docket")
    root = root_docket(docket)
    if root:
        roots.add(root)

    print(
        f"  company_id={owner!r} | "
        f"docket={docket!r} | "
        f"root={root!r} | "
        f"project={project.get('project')!r}"
    )

print()
print("Roots calculated directly from registry:")
for root in sorted(roots):
    print(" ", root)

expected = {"CP25-10", "CP21-465", "CP17-101"}
missing = expected - roots

print()
if missing:
    print("FAIL | Direct registry calculation missing:", sorted(missing))
else:
    print("PASS | Registry directly contains all 3 Williams root dockets")

print()
print("WEEKLY RUNNER tracked_roots() SOURCE")
print("-" * 90)

runner_path = Path("scripts/run_weekly_company_discovery.py")
runner_text = runner_path.read_text(encoding="utf-8")
lines = runner_text.splitlines()

start = None
for index, line in enumerate(lines):
    if line.startswith("def tracked_roots("):
        start = index
        break

if start is None:
    print("FAIL | tracked_roots() not found in weekly runner")
else:
    end = min(len(lines), start + 45)
    for number in range(start, end):
        print(f"{number + 1:4}: {lines[number]}")

print()
print("No imports from scripts are used by this diagnostic.")
