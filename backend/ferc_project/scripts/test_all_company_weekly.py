from ferc_filter.discovery_monitor_integration import (
    promote_pipeline_tracks,
)
from run_all_company_weekly import enabled_project_company_ids


registry = {
    "companies": [
        {"id": "A", "name": "Company A", "enabled": True},
        {"id": "B", "name": "Company B", "enabled": True},
        {"id": "DISABLED", "name": None, "enabled": False},
    ],
    "projects": [],
}

pipeline_a = {
    "evaluated": [
        {
            "docket": "CP26-100",
            "materiality": {"decision": "TRACK"},
            "evidence": {
                "project_name": "Alpha Expansion Project",
                "capex": None,
                "capacity": None,
                "target_in_service": None,
                "source_accessions": ["A1"],
                "confidence": "medium",
            },
        }
    ]
}

pipeline_b = {
    "evaluated": [
        {
            "docket": "CP26-200",
            "materiality": {"decision": "WATCH"},
            "evidence": {
                "project_name": "Beta Project",
                "source_accessions": ["B1"],
                "confidence": "medium",
            },
        }
    ]
}

added_a = promote_pipeline_tracks(registry, "A", pipeline_a)
added_b = promote_pipeline_tracks(registry, "B", pipeline_b)

assert len(added_a) == 1
assert len(added_b) == 0
assert registry["projects"][0]["project"] == "Alpha Expansion Project"
print("PASS | TRACK project promoted for enabled company")
print("PASS | WATCH project not promoted")

added_again = promote_pipeline_tracks(registry, "A", pipeline_a)
assert added_again == []
print("PASS | Weekly promotion remains idempotent")

enabled = [
    company
    for company in registry["companies"]
    if company.get("enabled") is True
    and company.get("name")
]
assert len(enabled) == 2
print("PASS | Disabled unnamed company is skipped")

project_company_ids = enabled_project_company_ids(registry)
assert project_company_ids == {"A"}
print("PASS | Company with TRACK project is monitor-eligible")
print("PASS | Company with no tracked project is a normal monitor skip")

print()
print("ALL ALL-COMPANY WEEKLY LOOP v0.1 TESTS PASSED")
