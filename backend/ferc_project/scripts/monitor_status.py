from collections import Counter
from datetime import datetime

from ferc_filter.monitor import MONITORED_PROJECTS
from ferc_filter.run_log import read_recent_runs
from ferc_filter.state import MonitorState


state = MonitorState()

runs = read_recent_runs(
    limit=500,
)


print("=" * 80)
print("FERC MONITOR STATUS")
print("=" * 80)


for project in MONITORED_PROJECTS:

    print()
    print(project.name)
    print("-" * len(project.name))

    checkpoint = state.get_checkpoint(
        project.docket
    )

    if checkpoint:
        try:
            parsed = datetime.fromisoformat(
                checkpoint
            )

            checkpoint_display = (
                parsed.strftime(
                    "%Y-%m-%d %H:%M:%S %Z"
                )
            )

        except ValueError:
            checkpoint_display = checkpoint
    else:
        checkpoint_display = "Never"

    print(
        "Docket:",
        project.docket,
    )

    print(
        "Last successful check:",
        checkpoint_display,
    )

    project_runs = [
        run
        for run in runs
        if run.get("docket")
        == project.docket
    ]

    if not project_runs:
        print("Recorded monitor runs: 0")
        print("New filings observed: 0")
        continue

    total_new = sum(
        run.get("new_count", 0)
        for run in project_runs
    )

    total_processed = sum(
        run.get("processed_count", 0)
        for run in project_runs
    )

    total_alerts = sum(
        run.get("alerts", 0)
        for run in project_runs
    )

    total_reviews = sum(
        run.get("reviews", 0)
        for run in project_runs
    )

    total_suppressed = sum(
        run.get("suppressed", 0)
        for run in project_runs
    )

    print(
        "Recorded monitor runs:",
        len(project_runs),
    )

    print(
        "New filings observed:",
        total_new,
    )

    print(
        "Processed:",
        total_processed,
    )

    print(
        "ALERT:",
        total_alerts,
        "| REVIEW:",
        total_reviews,
        "| SUPPRESS:",
        total_suppressed,
    )

    last_new_run = next(
        (
            run
            for run in project_runs
            if run.get("new_count", 0) > 0
        ),
        None,
    )

    if last_new_run:

        accessions = last_new_run.get(
            "new_accessions",
            [],
        )

        print(
            "Last new filing(s):",
            ", ".join(accessions)
            if accessions
            else "Unknown",
        )

        print(
            "Last new filing observed:",
            last_new_run.get(
                "logged_at",
                "Unknown",
            ),
        )

    else:
        print(
            "Last new filing observed: None"
        )


print()
print("=" * 80)
print("SYSTEM")
print("=" * 80)

print(
    "Stored filings:",
    state.count_filings(),
)

print(
    "Cached enrichments:",
    state.count_cached_enrichments(),
)