from ferc_filter.discovery_monitor_integration import (
    build_monitor_registry_record,
    promote_pipeline_tracks,
)

evidence = {
    "project_name": "Example Expansion Project",
    "applicant": "Example Pipeline Company, LLC",
    "capex": 1_250_000_000,
    "capacity": 750000,
    "capacity_unit": "Dth/d",
    "target_in_service": "Q4 2028",
    "source_accessions": ["20260901-5000"],
    "confidence": "high",
}

record = build_monitor_registry_record("TEST", "CP26-999-000", evidence)
assert record["company_id"] == "TEST"
assert record["project"] == "Example Expansion Project"
assert record["docket"] == "CP26-999"
assert record["context"]["capex_value"] == 1.25
assert record["context"]["capacity_value"] == 750000
assert record["context"]["target_in_service"] == "Q4 2028"
print("PASS | TRACK evidence maps to exact monitor registry schema")

missing = build_monitor_registry_record(
    "TEST",
    "CP26-998",
    {
        "project_name": "Example Connector Project",
        "source_accessions": ["20260902-5000"],
        "confidence": "medium",
    },
)
assert "capex_value" not in missing["context"]
assert "capacity_value" not in missing["context"]
print("PASS | Missing economic evidence is not invented")

registry = {
    "companies": [{"id": "TEST", "name": "Example Energy"}],
    "projects": [{
        "company_id": "TEST",
        "project": "Existing Project",
        "docket": "CP25-10",
        "enabled": True,
        "context": {},
    }],
}

pipeline = {
    "evaluated": [
        {
            "docket": "CP26-999",
            "materiality": {"decision": "TRACK"},
            "evidence": evidence,
        },
        {
            "docket": "CP26-997",
            "materiality": {"decision": "WATCH"},
            "evidence": {},
        },
    ]
}

added = promote_pipeline_tracks(registry, "TEST", pipeline)
assert len(added) == 1
assert len(registry["projects"]) == 2
print("PASS | Only TRACK projects are promoted")

added_again = promote_pipeline_tracks(registry, "TEST", pipeline)
assert added_again == []
assert len(registry["projects"]) == 2
print("PASS | Promotion is idempotent")

print()
print("ALL DISCOVERY -> MONITOR INTEGRATION v0.1 TESTS PASSED")
