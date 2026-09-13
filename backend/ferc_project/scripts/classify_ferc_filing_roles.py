from __future__ import annotations
import argparse, json
from pathlib import Path
from ferc_filter.ferc_filing_role import classify_filing_role, project_discovery_roles

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    records = payload.get("records") or payload.get("searchHits") or []
    counts = {}
    roles_by_docket = {}
    for record in records:
        role = classify_filing_role(record)
        counts[role.role] = counts.get(role.role, 0) + 1
        roles_by_docket.setdefault(role.docket or "UNKNOWN", []).append(role.role)
    print("=" * 90)
    print("FERC FILING ROLE CLASSIFIER v0.1")
    print("=" * 90)
    for role, count in sorted(counts.items()):
        print(f"{role:22} | {count}")
    eligible = project_discovery_roles()
    eligible_dockets = {d for d, roles in roles_by_docket.items() if any(r in eligible for r in roles)}
    print()
    print("Dockets with project-discovery-role filings:", len(eligible_dockets))
    print("Public-comment-only dockets are not promoted here.")
    print("Routine-reporting-only dockets are not promoted here.")

if __name__ == "__main__":
    main()
