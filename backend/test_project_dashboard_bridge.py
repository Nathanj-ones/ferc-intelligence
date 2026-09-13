from pathlib import Path
import json
import tempfile

ROOT = Path(__file__).resolve().parents[1]

# Validate the generated dashboard payload if it exists.
path = ROOT / "lib" / "ferc" / "projects-live.json"
if not path.exists():
    raise SystemExit(
        "projects-live.json not found. Run: python backend/build_project_dashboard.py"
    )

projects = json.loads(path.read_text(encoding="utf-8"))
assert isinstance(projects, list)
assert projects, "Expected at least one project"

required = {
    "project",
    "docket",
    "current_stage",
    "generated_at",
    "company",
    "counts",
    "milestones",
    "regulatory_outlook",
    "activity",
}

for project in projects:
    missing = required - set(project)
    assert not missing, f"{project.get('docket')}: missing {sorted(missing)}"
    assert project["company"] != "Company not mapped"
    assert project["docket"].startswith("CP")

print(f"PASS | {len(projects)} dashboard project(s) exported")
print("PASS | Every project has a real company mapping")
print("PASS | Dashboard ProjectView contract fields present")
print()
print("ALL PROJECT DASHBOARD BRIDGE v0.1 TESTS PASSED")
