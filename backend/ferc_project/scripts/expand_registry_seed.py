from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path


REGISTRY = Path("config/project_registry.json")

# High-confidence FERC projects identified from current FERC/company sources.
# These are additions for companies that currently have zero projects in the
# registry. We do not touch existing KMI, NXDT, or WMB entries.
ADDITIONS = [
    {
        "company_id": "LNG",
        "docket": "CP24-75-001",
        "name": "Sabine Pass Stage 5 Expansion Project",
        "priority": "high",
        "source": "FERC",
    },
    {
        "company_id": "LNG",
        "docket": "CP25-505",
        "name": "Sabine Pass Stage V Project",
        "priority": "high",
        "source": "FERC",
    },
    {
        "company_id": "LNG",
        "docket": "CP25-506",
        "name": "Sabine Crossing Pipeline Project",
        "priority": "high",
        "source": "FERC",
    },
    {
        "company_id": "LNG",
        "docket": "CP26-82",
        "name": "Corpus Christi Liquefaction Stage 4 Project",
        "priority": "high",
        "source": "FERC",
    },
    {
        "company_id": "LNG",
        "docket": "CP26-87",
        "name": "Corpus Christi Pipeline Expansion Project",
        "priority": "high",
        "source": "FERC",
    },
    {
        "company_id": "TRGP",
        "docket": "CP26-34",
        "name": "Forza Pipeline Project",
        "priority": "high",
        "source": "FERC",
    },
    {
        "company_id": "TRGP",
        "docket": "CP26-35",
        "name": "Bull Run Pipeline Project",
        "priority": "high",
        "source": "FERC",
    },
]


def load():
    if not REGISTRY.exists():
        raise SystemExit(f"Registry not found: {REGISTRY}")
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def save(payload):
    REGISTRY.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def normalize_docket(value):
    return str(value or "").strip().upper().replace("-000", "")


def project_container(payload):
    projects = payload.get("projects")
    if isinstance(projects, list):
        return projects, "list"

    if isinstance(projects, dict):
        return projects, "dict"

    raise SystemExit("Registry 'projects' must be a list or dictionary.")


def company_key(item):
    return str(
        item.get("company_id")
        or item.get("company")
        or item.get("ticker")
        or item.get("id")
        or ""
    ).strip()


def project_key(item):
    return normalize_docket(item.get("docket") or item.get("docket_number"))


def build_project(template, addition):
    # Preserve the local schema by starting from an existing project and
    # replacing only fields we can identify confidently.
    item = deepcopy(template) if template else {}

    fields = {
        "company_id": addition["company_id"],
        "docket": addition["docket"],
        "name": addition["name"],
        "priority": addition["priority"],
    }

    for key, value in fields.items():
        if template and key not in template:
            # Map to common alternatives when the local schema uses them.
            if key == "company_id":
                for alt in ("company", "ticker"):
                    if alt in template:
                        item[alt] = value
                        break
                else:
                    item[key] = value
            elif key == "name" and "project_name" in template:
                item["project_name"] = value
            elif key == "docket" and "docket_number" in template:
                item["docket_number"] = value
            else:
                item[key] = value
        else:
            item[key] = value

    if "source" in (template or {}) or not template:
        item["source"] = addition["source"]

    return item


def main():
    payload = load()
    projects, mode = project_container(payload)

    existing_dockets = {
        project_key(item)
        for item in (projects if isinstance(projects, list) else projects.values())
        if project_key(item)
    }

    by_company = {}
    items = projects if isinstance(projects, list) else list(projects.values())
    for item in items:
        by_company.setdefault(company_key(item), []).append(item)

    # Find a template with the closest existing company/project schema.
    global_template = items[0] if items else None

    added = 0
    skipped = 0

    for addition in ADDITIONS:
        docket = normalize_docket(addition["docket"])
        if docket in existing_dockets:
            print(f"SKIP | Already present: {docket} | {addition['name']}")
            skipped += 1
            continue

        template = by_company.get(addition["company_id"], [global_template])[0]
        item = build_project(template, addition)

        if isinstance(projects, list):
            projects.append(item)
        else:
            projects[docket] = item

        existing_dockets.add(docket)
        by_company.setdefault(addition["company_id"], []).append(item)
        added += 1
        print(f"PASS | Added {addition['company_id']} | {docket} | {addition['name']}")

    if isinstance(payload.get("projects"), list):
        payload["projects"] = projects
    else:
        payload["projects"] = projects

    save(payload)

    print()
    print(f"Added: {added}")
    print(f"Skipped existing: {skipped}")
    print()
    print("COMPANY_6 remains untouched because its real identity is not")
    print("established in the current project materials.")
    print()
    print("Run:")
    print("  python scripts/audit_six_company_registry.py")


if __name__ == "__main__":
    main()
