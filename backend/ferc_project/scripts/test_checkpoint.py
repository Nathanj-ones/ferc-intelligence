from datetime import datetime, timezone

from ferc_filter.state import MonitorState


state = MonitorState(
    "data/test_checkpoint.db"
)

docket = "CP16-454"


print(
    "Before:",
    state.get_checkpoint(docket),
)


timestamp = datetime.now(
    timezone.utc
).isoformat()


state.save_checkpoint(
    docket=docket,
    project_name="Rio Grande LNG",
    checked_at=timestamp,
)


print(
    "Saved:",
    timestamp,
)

print(
    "After:",
    state.get_checkpoint(docket),
)