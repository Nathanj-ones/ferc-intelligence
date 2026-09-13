"""Persistent JSON run log for the FERC monitor."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_LOG_PATH = Path("data") / "ferc_monitor_runs.jsonl"


def append_run(
    summary: dict,
    log_path: str | Path = DEFAULT_LOG_PATH,
) -> None:
    """Append one completed monitor run as a JSON line."""

    path = Path(log_path)

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    payload = dict(summary)

    payload["logged_at"] = (
        datetime.now(timezone.utc).isoformat()
    )

    with path.open(
        "a",
        encoding="utf-8",
    ) as handle:

        handle.write(
            json.dumps(
                payload,
                ensure_ascii=False,
            )
            + "\n"
        )


def read_recent_runs(
    limit: int = 20,
    log_path: str | Path = DEFAULT_LOG_PATH,
) -> list[dict]:
    """Read the most recent monitor runs."""

    path = Path(log_path)

    if not path.exists():
        return []

    with path.open(
        "r",
        encoding="utf-8",
    ) as handle:

        lines = handle.readlines()

    results = []

    for line in reversed(lines[-limit:]):

        line = line.strip()

        if not line:
            continue

        results.append(
            json.loads(line)
        )

    return results