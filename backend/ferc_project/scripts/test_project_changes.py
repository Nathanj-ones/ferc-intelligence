from ferc_filter.project_changes import compare_project_snapshots

base = {
    "project": "Example",
    "docket": "CP1",
    "status": {
        "stage": "construction",
        "headline": "Construction underway",
    },
    "attention": {
        "blocking_action": None,
        "requested_timing": None,
    },
    "timeline": {
        "projected_endpoint": {"value": "Q3 2027"},
    },
    "evidence": {
        "material_filings": [
            {"accession": "A1", "title": "Certificate", "importance": "high"}
        ]
    },
}

changed = {
    **base,
    "status": {
        "stage": "post_certificate_pre_construction",
        "headline": "Construction authorization pending",
    },
    "attention": {
        "blocking_action": "notice_to_proceed",
        "requested_timing": "2026-09-11T00:00:00",
    },
    "timeline": {
        "projected_endpoint": {"value": "Q4 2028"},
    },
    "evidence": {
        "material_filings": [
            base["evidence"]["material_filings"][0],
            {"accession": "A2", "title": "New Decision", "importance": "high"},
        ]
    },
}

changes = compare_project_snapshots(base, changed)
types = {item.change_type for item in changes}

assert "lifecycle_change" in types
assert "new_blocker" in types
assert "timing_change" in types
assert "endpoint_change" in types
assert "new_material_filing" in types
print("PASS | Lifecycle changes detected")
print("PASS | Blocker changes detected")
print("PASS | Timing and endpoint changes detected")
print("PASS | New material filings detected")

none = compare_project_snapshots(base, base)
assert none[0].change_type == "no_material_change"
print("PASS | No-change state detected")

print()
print("ALL PROJECT CHANGES v0.1 TESTS PASSED")
