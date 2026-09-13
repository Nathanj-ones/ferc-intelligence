from ferc_filter.run_log import (
    append_run,
    read_recent_runs,
)


append_run(
    {
        "project": "Test Project",
        "docket": "CP00-000",
        "start_date": "09-01-2026",
        "end_date": "09-07-2026",
        "raw_count": 3,
        "new_count": 2,
        "processed_count": 2,
        "alerts": 1,
        "reviews": 1,
        "suppressed": 0,
        "new_accessions": [
            "TEST-001",
            "TEST-002",
        ],
    },
    "data/test_monitor_runs.jsonl",
)


runs = read_recent_runs(
    limit=5,
    log_path="data/test_monitor_runs.jsonl",
)


print("Runs found:", len(runs))

for run in runs:
    print(run)