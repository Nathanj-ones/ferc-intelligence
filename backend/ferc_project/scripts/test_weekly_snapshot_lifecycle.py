import json
from pathlib import Path
from tempfile import TemporaryDirectory


def save(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


with TemporaryDirectory() as tmp:
    root = Path(tmp)
    weekly = root / "data" / "weekly"
    weekly.mkdir(parents=True)

    snapshot = weekly / "Williams_snapshot.json"
    temp = weekly / ".Williams_snapshot.tmp"

    first = {
        "schema_version": "company_project_snapshot_v0.1",
        "company": "Williams",
        "projects": [
            {"project": "SSE", "docket": "CP25-10"}
        ],
    }

    save(temp, first)
    temp.replace(snapshot)

    assert snapshot.exists()
    loaded = json.loads(snapshot.read_text(encoding="utf-8"))
    assert loaded["projects"][0]["docket"] == "CP25-10"
    print("PASS | First run creates weekly baseline snapshot")

    second = {
        **first,
        "projects": [
            {"project": "SSE", "docket": "CP25-10"},
            {"project": "NESE", "docket": "CP17-101"},
        ],
    }

    previous = json.loads(snapshot.read_text(encoding="utf-8"))
    assert len(previous["projects"]) == 1

    save(temp, second)
    temp.replace(snapshot)

    current = json.loads(snapshot.read_text(encoding="utf-8"))
    assert len(current["projects"]) == 2
    print("PASS | Prior snapshot remains readable before replacement")
    print("PASS | Successful run atomically replaces baseline")

print()
print("ALL WEEKLY SNAPSHOT LIFECYCLE v0.1 TESTS PASSED")
