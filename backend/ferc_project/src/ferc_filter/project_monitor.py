from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .activity_grouper import (
    ActivityRecord,
    extract_referenced_accessions,
    extract_referenced_dates,
    group_review_records,
)
from .app_project import (
    app_project_to_dict,
    build_app_project,
    validate_app_project,
)
from .elibrary import ELibraryClient
from .elibrary_enrichment import ELibraryEnrichmentClient
from .event_classifier import classify_event
from .investor_summary import build_investor_summary
from .pending_regulatory_action import find_pending_regulatory_actions

from .construction_authorization_integration import (
    analyze_construction_authorization_chain,
    filter_resolved_construction_actions,
    apply_construction_chain_to_lifecycle,
)
from .project_context import ProjectContext
from .project_display import build_project_display
from .project_evidence import evidence_from_display
from .project_lifecycle import (
    LifecycleFiling,
    build_project_lifecycle,
)
from .project_registry import ProjectConfig, Registry
from .project_timeline import build_project_timeline
from .project_view import build_project_view, project_view_to_dict
from .regulatory_outlook import build_regulatory_outlook
from .regulatory_progression import (
    ProgressionFiling,
    build_progressions_from_requests,
)
from .regulatory_tracker import (
    RegulatoryFiling,
    track_regulatory_requests,
)


class ProjectMonitor:
    """Orchestrate the already-validated project pipeline."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.docket_client = ELibraryClient()
        self.enrichment_client = ELibraryEnrichmentClient()
        self.state_path = Path("data/monitor/monitor_state.json")
        self.raw_root = Path("data/monitor/raw")
        self.enrichment_path = Path("data/monitor/enrichment_cache.json")
        self._enrichment_unavailable = False
        self._enrichment_cached = 0
        self._enrichment_new = 0
        self._enrichment_deferred = 0

    def _load_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_json(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _state(self) -> dict[str, Any]:
        return self._load_json(self.state_path, {})

    def _enrichment_cache(self) -> dict[str, Any]:
        return self._load_json(self.enrichment_path, {})

    def _parse_date(self, record: dict[str, Any]) -> datetime | None:
        value = (
            record.get("filed_date")
            or record.get("filedDate")
            or record.get("issued_date")
            or record.get("issuedDate")
        )
        if not value:
            return None
        value = str(value).strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
            try:
                return datetime.strptime(value[:10], fmt)
            except ValueError:
                pass
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _dedupe(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        result = []
        for record in records:
            accession = record.get("accession_no")
            if not accession or accession in seen:
                continue
            seen.add(accession)
            result.append(record)
        return result

    def _enrich(
        self,
        record: dict[str, Any],
        cache: dict[str, Any],
    ) -> dict[str, Any] | None:
        accession = record.get("accession_no") or record.get("accession")
        if not accession:
            return dict(record)

        cached = cache.get(accession)
        if cached is not None:
            self._enrichment_cached += 1
            return self._merge_enrichment(record, cached)

        if self._enrichment_unavailable:
            self._enrichment_deferred += 1
            return None

        import time
        import requests

        last_error = None
        for attempt in range(3):
            try:
                cached = self.enrichment_client.get_by_accession(
                    accession
                )
                last_error = None
                if cached is not None:
                    cache[accession] = cached
                    self._enrichment_new += 1
                    return self._merge_enrichment(record, cached)
                break
            except requests.RequestException as exc:
                last_error = exc
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None)

                if (
                    status is not None
                    and status not in {
                        500, 502, 503, 504,
                        520, 521, 522, 523, 524,
                    }
                ):
                    raise

                if attempt < 2:
                    wait = 2 ** (attempt + 1)
                    error_label = (
                        str(status)
                        if status is not None
                        else type(exc).__name__
                    )
                    print(
                        f"  eLibrary enrichment transient error "
                        f"{error_label}; retrying in {wait}s..."
                    )
                    time.sleep(wait)

        if last_error is not None:
            self._enrichment_unavailable = True
            self._enrichment_deferred += 1
            print(
                "  Enrichment service unavailable after retries; "
                "circuit breaker OPEN."
            )
            print(
                "  Remaining uncached filings will be deferred "
                "until a later run."
            )
            return None

        try:
            enriched = self.enrichment_client.enrich(record)
        except requests.RequestException as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            if (
                status is None
                or status in {
                    500, 502, 503, 504,
                    520, 521, 522, 523, 524,
                }
            ):
                self._enrichment_unavailable = True
                self._enrichment_deferred += 1
                print(
                    f"  eLibrary enrichment transient error {status}; "
                    "circuit breaker OPEN."
                )
                return None
            raise

        cache[accession] = enriched
        self._enrichment_new += 1
        return self._merge_enrichment(record, enriched)

    @staticmethod
    def _merge_enrichment(
        record: dict[str, Any],
        cached: dict[str, Any],
    ) -> dict[str, Any]:
        enriched = dict(record)
        class_types = cached.get("classTypes", [])
        if class_types:
            primary = class_types[0]
            enriched["document_class"] = primary.get("documentClass")
            enriched["document_type"] = primary.get("documentType")
        enriched["enriched_class_types"] = class_types
        enriched["availability_code"] = cached.get("availCode")
        return enriched

    def _activity_record(
        self,
        project: str,
        docket: str,
        record: dict[str, Any],
        enriched: dict[str, Any],
        decision: Any,
    ) -> ActivityRecord:
        accession = record.get("accession_no") or ""
        description = record.get("doc_desc") or ""
        item = ActivityRecord(
            project=project,
            docket=docket,
            accession=accession,
            date=self._parse_date(record),
            event_type=decision.event_type,
            description=description,
            document_class=enriched.get("document_class"),
            document_type=enriched.get("document_type"),
        )
        item.referenced_accessions = extract_referenced_accessions(
            description, accession
        )
        item.referenced_dates = extract_referenced_dates(description)
        return item

    def _fetch_window(self, project: ProjectConfig) -> tuple[str, str]:
        state = self._state()
        prior = state.get(project.docket, {})
        if prior.get("last_run"):
            prior_dt = datetime.fromisoformat(prior["last_run"])
            start = prior_dt.date() - timedelta(days=self.registry.overlap_days)
            return (
                start.strftime("%m-%d-%Y"),
                datetime.now().strftime("%m-%d-%Y"),
            )
        return (
            self.registry.history_start_date,
            datetime.now().strftime("%m-%d-%Y"),
        )

    def _load_raw_cache(self, docket: str) -> list[dict[str, Any]]:
        return self._load_json(
            self.raw_root / f"{docket}.json",
            [],
        )

    def _save_raw_cache(self, docket: str, records: list[dict[str, Any]]) -> None:
        self._save_json(
            self.raw_root / f"{docket}.json",
            records,
        )

    @staticmethod
    def _context(company_name: str, project: ProjectConfig) -> ProjectContext:
        values = dict(project.context)
        return ProjectContext(
            project=project.project,
            docket=project.docket,
            company=company_name,
            **values,
        )

    def run_project(self, project: ProjectConfig) -> dict[str, Any]:
        self._enrichment_unavailable = False
        self._enrichment_cached = 0
        self._enrichment_new = 0
        self._enrichment_deferred = 0

        companies = self.registry.company_map()
        company = companies[project.company_id]
        if not company.name:
            raise ValueError(f"Project has no enabled company name: {project.docket}")

        start_date, end_date = self._fetch_window(project)
        print(f"  {project.docket}: {start_date} -> {end_date}")

        import time
        import requests

        fetched: list[dict[str, Any]] = []
        prior = self._load_raw_cache(project.docket)
        fetch_error = None

        for attempt in range(3):
            try:
                result = self.docket_client.get_docket(
                    docket=project.docket,
                    start_date=start_date,
                    end_date=end_date,
                )
                fetched = list(result.records)
                fetch_error = None
                break
            except requests.RequestException as exc:
                fetch_error = exc
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None)

                if (
                    status is not None
                    and status not in {
                        500, 502, 503, 504,
                        520, 521, 522, 523, 524,
                    }
                ):
                    raise

                if attempt < 2:
                    wait = 2 ** (attempt + 1)
                    print(
                        f"  eLibrary docket transient error {status}; "
                        f"retrying in {wait}s..."
                    )
                    time.sleep(wait)

        if fetch_error is not None and not fetched:
            if not prior:
                print(
                    "  eLibrary docket unavailable and no cache exists; "
                    "project deferred."
                )
                return {
                    "company": company.name,
                    "project": project.project,
                    "docket": project.docket,
                    "raw_records": 0,
                    "app_path": None,
                    "project_view_path": None,
                    "status": "fetch_failed",
                    "error": str(fetch_error),
                    "enrichment_cached": 0,
                    "enrichment_new": 0,
                    "enrichment_deferred": 0,
                }
            print(
                f"  eLibrary docket unavailable; using cached "
                f"{len(prior)} records."
            )

        records = self._dedupe(prior + fetched)
        self._save_raw_cache(project.docket, records)

        cache = self._enrichment_cache()
        alerts = []
        reviews = []
        classified = []

        print(f"  Processing {len(records)} records")
        for index, record in enumerate(records, start=1):
            enriched = self._enrich(record, cache)
            if enriched is None:
                continue

            decision = classify_event(enriched)
            item = {
                "accession": record.get("accession_no") or "",
                "date": self._parse_date(record),
                "event_type": decision.event_type or "unknown",
                "description": record.get("doc_desc") or "",
            }
            classified.append(item)

            if index % 25 == 0 or index == len(records):
                print(
                    f"  Processed {index}/{len(records)} "
                    f"(cached {self._enrichment_cached}, "
                    f"new {self._enrichment_new}, "
                    f"deferred {self._enrichment_deferred})"
                )

            if decision.decision == "ALERT":
                alerts.append(item)
            elif decision.decision == "REVIEW":
                reviews.append(
                    self._activity_record(
                        project.project,
                        project.docket,
                        record,
                        enriched,
                        decision,
                    )
                )

        self._save_json(self.enrichment_path, cache)

        activity_groups = group_review_records(reviews)
        view = build_project_view(
            project=project.project,
            docket=project.docket,
            raw_count=len(records),
            deduped_count=len(records),
            alerts=alerts,
            review_count=len(reviews),
            suppressed_count=len(records) - len(alerts) - len(reviews),
            activity_groups=activity_groups,
        )

        regulatory_filings = [RegulatoryFiling(**item) for item in classified]
        requests = track_regulatory_requests(regulatory_filings)
        progression_filings = [ProgressionFiling(**item) for item in classified]
        progressions = build_progressions_from_requests(
            requests,
            progression_filings,
        )
        outlook = build_regulatory_outlook(
            project=project.project,
            docket=project.docket,
            current_stage=view.current_stage,
            requests=requests,
            progressions=progressions,
            milestones=view.milestones,
        )
        lifecycle = build_project_lifecycle(
            [LifecycleFiling(**item) for item in classified]
        )

        construction_chain = analyze_construction_authorization_chain(
            classified
        )
        lifecycle = apply_construction_chain_to_lifecycle(
            lifecycle,
            construction_chain,
        )
        pending = find_pending_regulatory_actions(
            classified,
            tracked_requests=requests,
        )

        pending = filter_resolved_construction_actions(
            pending,
            construction_chain,
        )
        view.regulatory_outlook = outlook
        view.current_stage = lifecycle.current_stage
        view.lifecycle = lifecycle
        view.pending_regulatory_actions = pending
        investor = build_investor_summary(
            lifecycle=lifecycle,
            outlook=outlook,
            pending_actions=pending,
        )
        view.investor_summary = investor
        display = build_project_display(view)
        context = self._context(company.name, project)
        timeline = build_project_timeline(context, display)
        evidence = evidence_from_display(display)
        app_project = build_app_project(
            context,
            display,
            timeline,
            evidence,
        )
        errors = validate_app_project(app_project)
        if errors:
            raise ValueError(
                f"App Project validation failed for {project.docket}: {errors}"
            )

        output_root = Path(self.registry.output_root) / company.name / project.docket
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "project_view.json").write_text(
            json.dumps(project_view_to_dict(view), indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        app_root = Path(self.registry.app_output_root) / company.name
        app_root.mkdir(parents=True, exist_ok=True)
        app_path = app_root / f"{project.docket}.json"
        app_path.write_text(
            json.dumps(app_project_to_dict(app_project), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        state = self._state()
        state[project.docket] = {
            "project": project.project,
            "company": company.name,
            "last_run": datetime.now(timezone.utc).isoformat(),
            "raw_record_count": len(records),
        }
        self._save_json(self.state_path, state)

        return {
            "company": company.name,
            "project": project.project,
            "docket": project.docket,
            "raw_records": len(records),
            "enrichment_cached": self._enrichment_cached,
            "enrichment_new": self._enrichment_new,
            "enrichment_deferred": self._enrichment_deferred,
            "enrichment_status": (
                "partial" if self._enrichment_deferred else "complete"
            ),
            "app_path": str(app_path),
            "project_view_path": str(output_root / "project_view.json"),
        }
