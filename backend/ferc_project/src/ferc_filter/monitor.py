"""Incremental FERC docket monitor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .elibrary import ELibraryClient
from .elibrary_enrichment import ELibraryEnrichmentClient
from .event_classifier import classify_event
from .state import MonitorState
from .run_log import append_run

@dataclass(frozen=True)
class MonitoredProject:
    """A docket to monitor."""

    name: str
    docket: str


MONITORED_PROJECTS = [
    MonitoredProject(
        name="Rio Grande LNG",
        docket="CP16-454",
    ),
    MonitoredProject(
        name="Mississippi Crossing",
        docket="CP25-514",
    ),
    MonitoredProject(
        name="Lea County Expansion",
        docket="CP24-200",
    ),
]


class FERCMonitor:
    """Detect and process newly observed FERC filings."""

    def __init__(
        self,
        state: MonitorState | None = None,
        docket_client: ELibraryClient | None = None,
        enrichment_client: ELibraryEnrichmentClient | None = None,
    ) -> None:

        self.state = state or MonitorState()
        self.docket_client = (
            docket_client
            or ELibraryClient()
        )
        self.enrichment_client = (
            enrichment_client
            or ELibraryEnrichmentClient()
        )

    def run_project(
        self,
        project: MonitoredProject,
        initial_lookback_days: int = 7,
        overlap_days: int = 1,
    ) -> dict:
        """Check one docket incrementally using a persistent checkpoint."""

        started_at = (
            datetime.now(timezone.utc)
            .isoformat()
        )

        run_id = self.state.start_run(
            project_name=project.name,
            docket=project.docket,
            started_at=started_at,
        )

        raw_count = 0
        new_count = 0
        processed_count = 0

        try:
            checkpoint = self.state.get_checkpoint(
                project.docket
            )

            now = datetime.now(timezone.utc)

            if checkpoint is None:
                start_datetime = (
                    now
                    - timedelta(
                        days=initial_lookback_days
                    )
                )
            else:
                checkpoint_datetime = (
                    datetime.fromisoformat(
                        checkpoint
                    )
                )

                start_datetime = (
                    checkpoint_datetime
                    - timedelta(
                        days=overlap_days
                    )
                )

            start_date = start_datetime.strftime(
                "%m-%d-%Y"
            )

            end_date = now.strftime(
                "%m-%d-%Y"
            )

            result = self.docket_client.get_docket(
                docket=project.docket,
                start_date=start_date,
                end_date=end_date,
            )

            raw_count = len(result.records)

            new_records = []

            for record in result.records:

                accession = record.get(
                    "accession_no"
                )

                if not accession:
                    continue

                if self.state.has_filing(
                    accession
                ):
                    continue

                new_records.append(record)

            new_count = len(new_records)

            results = []

            for record in new_records:

                processed = self._process_record(
                    record=record,
                    project=project,
                )

                if processed is not None:
                    results.append(processed)
                    processed_count += 1

            completed_at = (
                datetime.now(timezone.utc)
                .isoformat()
            )

            # Only advance the checkpoint after the run
            # completes successfully.
            self.state.save_checkpoint(
                docket=project.docket,
                project_name=project.name,
                checked_at=completed_at,
            )

            self.state.finish_run(
                run_id=run_id,
                completed_at=completed_at,
                raw_count=raw_count,
                new_count=new_count,
                processed_count=processed_count,
            )

            append_run(
                {
                    "project": project.name,
                    "docket": project.docket,
                    "start_date": start_date,
                    "end_date": end_date,
                    "raw_count": raw_count,
                    "new_count": new_count,
                    "processed_count": processed_count,
                    "alerts": sum(
                        1
                        for item in results
                        if item["decision"] == "ALERT"
                    ),
                    "reviews": sum(
                        1
                        for item in results
                        if item["decision"] == "REVIEW"
                    ),
                    "suppressed": sum(
                        1
                        for item in results
                        if item["decision"] == "SUPPRESS"
                    ),
                    "new_accessions": [
                        item["accession"]
                        for item in results
                    ],
                }
            )

            return {
                "project": project.name,
                "docket": project.docket,
                "start_date": start_date,
                "end_date": end_date,
                "checkpoint_before": checkpoint,
                "checkpoint_after": completed_at,
                "raw_count": raw_count,
                "new_count": new_count,
                "processed_count": processed_count,
                "results": results,
            }

        except Exception as exc:

            completed_at = (
                datetime.now(timezone.utc)
                .isoformat()
            )

            self.state.finish_run(
                run_id=run_id,
                completed_at=completed_at,
                raw_count=raw_count,
                new_count=new_count,
                processed_count=processed_count,
                error=str(exc),
            )
            append_run(
                {
                    "project": project.name,
                    "docket": project.docket,
                    "start_date": start_date,
                    "end_date": end_date,
                    "raw_count": raw_count,
                    "new_count": new_count,
                    "processed_count": processed_count,
                    "alerts": sum(
                        1
                        for item in results
                        if item["decision"] == "ALERT"
                    ),
                    "reviews": sum(
                        1
                        for item in results
                        if item["decision"] == "REVIEW"
                    ),
                    "suppressed": sum(
                        1
                        for item in results
                        if item["decision"] == "SUPPRESS"
                    ),
                    "new_accessions": [
                        item["accession"]
                        for item in results
                    ],
                }
            )
            # Deliberately do NOT advance the checkpoint
            # when the run fails.
            raise

    def _process_record(
        self,
        *,
        record: dict,
        project: MonitoredProject,
    ) -> dict | None:
        """Enrich, classify, and persist one new filing."""

        accession = record.get(
            "accession_no"
        )

        if not accession:
            return None

        enriched = self.state.get_enrichment(
            accession
        )

        if enriched is None:

            enriched = (
                self.enrichment_client
                .get_by_accession(
                    accession
                )
            )

            if enriched is None:
                return None

            cached_at = (
                datetime.now(timezone.utc)
                .isoformat()
            )

            self.state.save_enrichment(
                accession=accession,
                enriched=enriched,
                cached_at=cached_at,
            )

        enriched_record = dict(record)

        class_types = enriched.get(
            "classTypes",
            [],
        )

        if class_types:

            primary = class_types[0]

            enriched_record[
                "document_class"
            ] = primary.get(
                "documentClass"
            )

            enriched_record[
                "document_type"
            ] = primary.get(
                "documentType"
            )

        enriched_record[
            "enriched_class_types"
        ] = class_types

        enriched_record[
            "availability_code"
        ] = enriched.get(
            "availCode"
        )

        decision = classify_event(
            enriched_record
        )

        processed_at = (
            datetime.now(timezone.utc)
            .isoformat()
        )

        self.state.save_filing(
            accession=accession,
            docket=project.docket,
            project_name=project.name,
            filed_date=record.get(
                "filed_date"
            ),
            category=record.get(
                "category"
            ),
            description=record.get(
                "doc_desc"
            ),
            document_class=enriched_record.get(
                "document_class"
            ),
            document_type=enriched_record.get(
                "document_type"
            ),
            decision=decision.decision,
            event_type=decision.event_type,
            processed_at=processed_at,
            raw_record=record,
            enriched_record=enriched_record,
        )

        return {
            "project": project.name,
            "docket": project.docket,
            "accession": accession,
            "filed_date": record.get(
                "filed_date"
            ),
            "category": record.get(
                "category"
            ),
            "document_class": enriched_record.get(
                "document_class"
            ),
            "document_type": enriched_record.get(
                "document_type"
            ),
            "decision": decision.decision,
            "event_type": decision.event_type,
            "description": record.get(
                "doc_desc"
            ),
        }


def default_date_window(
    days: int = 7,
) -> tuple[str, str]:
    """Return a recent date window suitable for an incremental check."""

    today = datetime.now(
        timezone.utc
    ).date()

    start = today - timedelta(
        days=days
    )

    return (
        start.strftime("%m-%d-%Y"),
        today.strftime("%m-%d-%Y"),
    )