from pathlib import Path
import json

registry_path = Path("config/project_registry.json")
if not registry_path.exists():
    raise SystemExit(f"Not found: {registry_path}")

payload = json.loads(registry_path.read_text(encoding="utf-8"))

print("=" * 90)
print("REGISTRY SCHEMA DIAGNOSTIC")
print("=" * 90)

print("\nCompanies:")
companies = payload.get("companies", [])
if isinstance(companies, dict):
    companies = list(companies.values())
for i, company in enumerate(companies):
    print(f"[{i}] {company!r}")

print("\nProjects:")
projects = payload.get("projects", [])
if isinstance(projects, dict):
    projects = list(projects.values())
for i, project in enumerate(projects):
    print(f"[{i}] {project!r}")

print("\nProject company-related keys:")
keys = set()
for project in projects:
    for key in project:
        if "company" in key.lower() or "ticker" in key.lower():
            keys.add(key)
print(sorted(keys))

print("\nProject docket-related keys:")
keys = set()
for project in projects:
    for key in project:
        if "docket" in key.lower():
            keys.add(key)
print(sorted(keys))
