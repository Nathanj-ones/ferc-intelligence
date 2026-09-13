"""Persistent local state for the FERC monitoring pipeline."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path("data") / "ferc_monitor.db"


class MonitorState:
    """SQLite-backed state and enrichment cache."""

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
    ) -> None:
        self.db_path = Path(db_path)

        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path
        )

        connection.row_factory = sqlite3.Row

        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS filings (
                    accession TEXT PRIMARY KEY,
                    docket TEXT NOT NULL,
                    project_name TEXT,
                    filed_date TEXT,
                    category TEXT,
                    description TEXT,
                    document_class TEXT,
                    document_type TEXT,
                    decision TEXT,
                    event_type TEXT,
                    processed_at TEXT NOT NULL,
                    raw_record_json TEXT,
                    enriched_record_json TEXT
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS enrichment_cache (
                    accession TEXT PRIMARY KEY,
                    enriched_json TEXT NOT NULL,
                    cached_at TEXT NOT NULL
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS monitor_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_name TEXT,
                    docket TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    raw_count INTEGER,
                    new_count INTEGER,
                    processed_count INTEGER,
                    error TEXT
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS monitor_checkpoints (
                    docket TEXT PRIMARY KEY,
                    project_name TEXT,
                    last_successful_check TEXT NOT NULL
                )
                """
            )

    def get_checkpoint(
        self,
        docket: str,
    ) -> str | None:
        """Return the last successful monitor checkpoint for a docket."""

        with self._connect() as connection:

            row = connection.execute(
                """
                SELECT last_successful_check
                FROM monitor_checkpoints
                WHERE docket = ?
                """,
                (docket,),
            ).fetchone()

        if row is None:
            return None

        return str(
            row["last_successful_check"]
        )


    def save_checkpoint(
        self,
        *,
        docket: str,
        project_name: str | None,
        checked_at: str,
    ) -> None:
        """Save the last successful check time for a docket."""

        with self._connect() as connection:

            connection.execute(
                """
                INSERT INTO monitor_checkpoints (
                    docket,
                    project_name,
                    last_successful_check
                )
                VALUES (?, ?, ?)
                ON CONFLICT(docket)
                DO UPDATE SET
                    project_name = excluded.project_name,
                    last_successful_check = excluded.last_successful_check
                """,
                (
                    docket,
                    project_name,
                    checked_at,
                ),
            )

    def has_filing(
        self,
        accession: str,
    ) -> bool:
        """Return True if an accession has already been processed."""

        with self._connect() as connection:

            row = connection.execute(
                """
                SELECT 1
                FROM filings
                WHERE accession = ?
                LIMIT 1
                """,
                (accession,),
            ).fetchone()

        return row is not None

    def get_enrichment(
        self,
        accession: str,
    ) -> dict[str, Any] | None:
        """Return cached enrichment metadata, if available."""

        with self._connect() as connection:

            row = connection.execute(
                """
                SELECT enriched_json
                FROM enrichment_cache
                WHERE accession = ?
                """,
                (accession,),
            ).fetchone()

        if row is None:
            return None

        return json.loads(
            row["enriched_json"]
        )

    def save_enrichment(
        self,
        accession: str,
        enriched: dict[str, Any],
        cached_at: str,
    ) -> None:
        """Save eLibrary enrichment metadata to the local cache."""

        with self._connect() as connection:

            connection.execute(
                """
                INSERT INTO enrichment_cache (
                    accession,
                    enriched_json,
                    cached_at
                )
                VALUES (?, ?, ?)
                ON CONFLICT(accession)
                DO UPDATE SET
                    enriched_json = excluded.enriched_json,
                    cached_at = excluded.cached_at
                """,
                (
                    accession,
                    json.dumps(
                        enriched,
                        ensure_ascii=False,
                    ),
                    cached_at,
                ),
            )

    def save_filing(
        self,
        *,
        accession: str,
        docket: str,
        project_name: str | None,
        filed_date: str | None,
        category: str | None,
        description: str | None,
        document_class: str | None,
        document_type: str | None,
        decision: str,
        event_type: str,
        processed_at: str,
        raw_record: dict[str, Any],
        enriched_record: dict[str, Any],
    ) -> None:
        """Persist a processed filing."""

        with self._connect() as connection:

            connection.execute(
                """
                INSERT INTO filings (
                    accession,
                    docket,
                    project_name,
                    filed_date,
                    category,
                    description,
                    document_class,
                    document_type,
                    decision,
                    event_type,
                    processed_at,
                    raw_record_json,
                    enriched_record_json
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(accession)
                DO UPDATE SET
                    docket = excluded.docket,
                    project_name = excluded.project_name,
                    filed_date = excluded.filed_date,
                    category = excluded.category,
                    description = excluded.description,
                    document_class = excluded.document_class,
                    document_type = excluded.document_type,
                    decision = excluded.decision,
                    event_type = excluded.event_type,
                    processed_at = excluded.processed_at,
                    raw_record_json = excluded.raw_record_json,
                    enriched_record_json = excluded.enriched_record_json
                """,
                (
                    accession,
                    docket,
                    project_name,
                    filed_date,
                    category,
                    description,
                    document_class,
                    document_type,
                    decision,
                    event_type,
                    processed_at,
                    json.dumps(
                        raw_record,
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        enriched_record,
                        ensure_ascii=False,
                    ),
                ),
            )

    def start_run(
        self,
        *,
        project_name: str | None,
        docket: str,
        started_at: str,
    ) -> int:
        """Create a monitor-run record and return its ID."""

        with self._connect() as connection:

            cursor = connection.execute(
                """
                INSERT INTO monitor_runs (
                    project_name,
                    docket,
                    started_at
                )
                VALUES (?, ?, ?)
                """,
                (
                    project_name,
                    docket,
                    started_at,
                ),
            )

            return int(cursor.lastrowid)

    def finish_run(
        self,
        *,
        run_id: int,
        completed_at: str,
        raw_count: int,
        new_count: int,
        processed_count: int,
        error: str | None = None,
    ) -> None:
        """Finalize a monitor-run record."""

        with self._connect() as connection:

            connection.execute(
                """
                UPDATE monitor_runs
                SET
                    completed_at = ?,
                    raw_count = ?,
                    new_count = ?,
                    processed_count = ?,
                    error = ?
                WHERE id = ?
                """,
                (
                    completed_at,
                    raw_count,
                    new_count,
                    processed_count,
                    error,
                    run_id,
                ),
            )

    def count_filings(self) -> int:
        """Return the number of persisted filings."""

        with self._connect() as connection:

            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM filings
                """
            ).fetchone()

        return int(row["count"])

    def count_cached_enrichments(self) -> int:
        """Return the number of cached enrichment records."""

        with self._connect() as connection:

            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM enrichment_cache
                """
            ).fetchone()

        return int(row["count"])

    def get_recent_runs(
        self,
        limit: int = 20,
    ) -> list[dict]:
        """Return recent monitor runs."""

        with self._connect() as connection:

            rows = connection.execute(
                """
                SELECT
                    id,
                    project_name,
                    docket,
                    started_at,
                    completed_at,
                    raw_count,
                    new_count,
                    processed_count,
                    error
                FROM monitor_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [dict(row) for row in rows]