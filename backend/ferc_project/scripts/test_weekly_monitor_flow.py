import json
from pathlib import Path
from tempfile import TemporaryDirectory

from ferc_filter.project_changes import compare_project_snapshots
from ferc_filter.company_weekly_summary import build_company_weekly_summary


def project(docket, stage, blocker=None):
    return {
        "project": docket,
        "docket": docket,
        "identity": {"project": docket, "docket": docket},
        "scale": {
            "capex": {"value": 2.0, "unit": "billion"},
            "capacity": {"value": None, "unit": None},
            "target_in_service": {"value": "Q4 2027"},
        },
        "status": {
            "stage": stage,
            "headline": "Construction underway",
        },
        "timeline": {
            "endpoint": "commercial_operation",
            "projected_endpoint": {"value": "Q4 2027"},
        },
        "attention": {
            "blocking_action": blocker,
            "responsible_party": "FERC" if blocker else None,
            "requested_timing": None,
        },
        "evidence": {
            "material_filings": [
                {"accession": f"{docket}-A1", "title": "Material"}
            ],
        },
    }


with TemporaryDirectory() as tmp:
    previous = project("CP1", "construction")
    current = project("CP1", "post_certificate_pre_construction", "notice_to_proceed")

    changes = compare_project_snapshots(previous, current)
    kinds = {item.change_type for item in changes}

    assert "lifecycle_change" in kinds
    assert "new_blocker" in kinds

    summary = build_company_weekly_summary(
        "Williams",
        [current],
    )
    assert summary.project_count == 1
    assert summary.attention_count == 1

    path = Path(tmp) / "snapshot.json"
    payload = {"projects": [previous]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["projects"][0]["docket"] == "CP1"

print("PASS | Prior snapshot can be used for weekly comparison")
print("PASS | Current project changes feed weekly summary")
print("PASS | Blocking action remains visible in company output")
print()
print("ALL WEEKLY MONITOR FLOW v0.1 TESTS PASSED")
