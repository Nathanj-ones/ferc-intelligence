from ferc_filter.company_weekly_summary import (
    build_company_weekly_summary,
    company_weekly_summary_to_dict,
)


def app(project, docket, capex, stage, blocker=None):
    return {
        "project": project,
        "docket": docket,
        "identity": {
            "project": project,
            "docket": docket,
        },
        "scale": {
            "capex": {
                "value": capex,
                "unit": "billion" if capex is not None else None,
            },
            "capacity": {
                "value": None,
                "unit": None,
            },
            "target_in_service": {
                "value": "Q3 2027",
            },
        },
        "status": {
            "stage": stage,
            "headline": "Construction underway",
        },
        "timeline": {
            "endpoint": "commercial_operation",
        },
        "attention": {
            "blocking_action": blocker,
            "responsible_party": "FERC" if blocker else None,
            "requested_timing": None,
        },
        "evidence": {
            "material_filings": [
                {
                    "accession": "20260101-0001",
                    "date": "2026-01-01T00:00:00",
                    "title": "Material Event",
                }
            ],
        },
    }


summary = build_company_weekly_summary(
    "Williams",
    [
        app("Small", "CP1", 0.9, "construction"),
        app("Large", "CP2", 1.5, "construction"),
        app("Unknown", "CP3", None, "certificate", "notice_to_proceed"),
    ],
)

assert summary.project_count == 3
assert summary.attention_count == 1
assert summary.projects[0].project == "Large"
assert summary.projects[1].project == "Small"
assert summary.projects[2].project == "Unknown"

payload = company_weekly_summary_to_dict(summary)
assert payload["schema_version"] == "company_weekly_summary_v0.1"
assert payload["projects"][2]["blocking_action"] == "notice_to_proceed"

print("PASS | Company projects summarized")
print("PASS | Projects ranked by disclosed capex")
print("PASS | Blocking actions surfaced")
print("PASS | Missing capex retained without inference")
print()
print("ALL COMPANY WEEKLY SUMMARY v0.1 TESTS PASSED")
