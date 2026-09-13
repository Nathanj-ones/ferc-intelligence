from ferc_filter.monitor import (
    FERCMonitor,
    MonitoredProject,
)
from ferc_filter.state import MonitorState


PROJECT = MonitoredProject(
    name="Rio Grande LNG",
    docket="CP16-454",
)


state = MonitorState(
    "data/test_checkpoint_monitor.db"
)

monitor = FERCMonitor(
    state=state
)


print()
print("=" * 80)
print("MONITOR TEST")
print("=" * 80)


result = monitor.run_project(
    project=PROJECT,
    initial_lookback_days=7,
    overlap_days=1,
)

print(
    "Start date:",
    result["start_date"],
)

print(
    "End date:",
    result["end_date"],
)

print(
    "Checkpoint before:",
    result["checkpoint_before"],
)

print(
    "Checkpoint after:",
    result["checkpoint_after"],
)

print()
print("Project:", result["project"])
print("Docket:", result["docket"])
print("Raw records:", result["raw_count"])
print("New records:", result["new_count"])
print(
    "Processed records:",
    result["processed_count"],
)


print()
print("=== RESULTS ===")

for item in result["results"]:

    print(
        item["accession"],
        "|",
        item["decision"],
        "|",
        item["event_type"],
    )

    print(
        " ",
        item["document_class"],
        "->",
        item["document_type"],
    )

    print(
        " ",
        item["description"],
    )