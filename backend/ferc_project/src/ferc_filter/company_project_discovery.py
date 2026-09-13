from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests


DEFAULT_BASE_URL = "https://elibrary.ferc.gov/eLibraryWebAPI/api"
TRANSIENT_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}

PROJECT_DOCKET_RE = re.compile(r"\bCP\d{2}-\d+(?:-\d{3})?\b", re.I)
ROOT_DOCKET_RE = re.compile(r"\b(CP\d{2}-\d+)(?:-\d{3})?\b", re.I)


def root_docket(value: str) -> str | None:
    """Normalize CP25-10-000 and CP25-10-001 to the root CP25-10."""
    if not value:
        return None
    match = ROOT_DOCKET_RE.search(str(value).upper())
    return match.group(1) if match else None


def normalize_docket(value: str) -> str | None:
    return root_docket(value)


@dataclass
class FilingHit:
    accession: str
    docket: str
    filed_date: str
    description: str
    category: str = ""
    applicant: str = ""
    document_class: str = ""
    document_type: str = ""


@dataclass
class ProjectCandidate:
    company_id: str
    company_name: str
    docket: str
    score: int
    reasons: list[str]
    filing_count: int
    latest_filing_date: str | None
    project_names: list[str]
    sample_filings: list[str]
    tracked: bool = False


class FERCDiscoveryClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 45.0,
        retries: int = 4,
        backoff_seconds: float = 2.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "FERC-Project-Monitor/1.0",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = self.session.post(
                    f"{self.base_url}/{path.lstrip('/')}",
                    json=body,
                    timeout=self.timeout,
                )
                if response.status_code in TRANSIENT_STATUS:
                    last_error = requests.HTTPError(
                        f"Transient FERC HTTP {response.status_code}"
                    )
                    if attempt + 1 < self.retries:
                        time.sleep(self.backoff_seconds * (2 ** attempt))
                        continue
                response.raise_for_status()
                payload = response.json()
                if payload.get("success") is False:
                    raise RuntimeError(
                        payload.get("errorMessage")
                        or "FERC search reported failure"
                    )
                return payload
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    time.sleep(self.backoff_seconds * (2 ** attempt))
                    continue
        raise RuntimeError(
            f"FERC discovery request failed after {self.retries} attempts"
        ) from last_error

    @staticmethod
    def _body(
        company_name: str,
        start_date: str,
        end_date: str,
        page: int,
        per_page: int,
    ) -> dict[str, Any]:
        return {
            "searchText": "*",
            "searchFullText": False,
            "searchDescription": True,
            "dateSearches": [
                {
                    "dateType": "filed_date",
                    "startDate": start_date,
                    "endDate": end_date,
                }
            ],
            "availability": None,
            "affiliations": [{"affiliation": company_name}],
            "categories": [],
            "libraries": [],
            "accessionNumber": None,
            "eFiling": False,
            "docketSearches": [],
            "resultsPerPage": per_page,
            "curPage": page,
            "classTypes": [],
            "sortBy": "",
            "groupBy": "NONE",
            "idolResultID": "",
            "allDates": False,
        }

    @staticmethod
    def _normalize_hit(hit: dict[str, Any]) -> FilingHit:
        class_types = hit.get("classTypes") or []
        primary_class = class_types[0] if class_types else {}
        docket_values = hit.get("docketNumbers") or []

        docket = ""
        if docket_values:
            docket = normalize_docket(str(docket_values[0])) or ""

        if not docket:
            docket = normalize_docket(
                str(hit.get("description") or "")
            ) or ""

        affiliations = hit.get("affiliations") or []
        applicant = ""
        if affiliations:
            applicant = str(
                affiliations[0].get("affiliation") or ""
            ).strip()

        return FilingHit(
            accession=str(
                hit.get("acesssionNumber")
                or hit.get("accessionNumber")
                or ""
            ),
            docket=docket,
            filed_date=str(hit.get("filedDate") or ""),
            description=str(hit.get("description") or ""),
            category=str(hit.get("category") or ""),
            applicant=applicant,
            document_class=str(primary_class.get("documentClass") or ""),
            document_type=str(primary_class.get("documentType") or ""),
        )

    def search_company(
        self,
        company_name: str,
        start_date: str,
        end_date: str,
        per_page: int = 100,
        max_pages: int | None = None,
    ) -> list[FilingHit]:
        first = self._post(
            "Search/AdvancedSearch",
            self._body(
                company_name,
                start_date,
                end_date,
                1,
                per_page,
            ),
        )

        total_hits = int(first.get("totalHits") or 0)
        pages = max(1, (total_hits + per_page - 1) // per_page)
        if max_pages is not None:
            pages = min(pages, max_pages)

        hits = [
            self._normalize_hit(item)
            for item in (first.get("searchHits") or [])
        ]

        for page in range(2, pages + 1):
            payload = self._post(
                "Search/AdvancedSearch",
                self._body(
                    company_name,
                    start_date,
                    end_date,
                    page,
                    per_page,
                ),
            )
            hits.extend(
                self._normalize_hit(item)
                for item in (payload.get("searchHits") or [])
            )

        return hits


def score_candidate(
    docket: str,
    filings: list[FilingHit],
) -> tuple[int, list[str]]:
    text = " ".join(
        f"{item.description} {item.document_type}"
        for item in filings
    ).lower()

    score = 0
    reasons = []

    if docket.upper().startswith("CP"):
        score += 3
        reasons.append("certificate-docket-family")

    project_terms = (
        "project",
        "certificate",
        "construction",
        "pipeline",
        "liquefaction",
        "expansion",
        "compression",
        "facilities",
        "terminal",
        "lng",
    )

    for term in project_terms:
        if term in text:
            score += 1
            reasons.append(f"project-term:{term}")
            if len(reasons) >= 6:
                break

    if len(filings) >= 3:
        score += 1
        reasons.append("multiple-filings")

    return score, reasons


def discover_projects(
    company_id: str,
    company_name: str,
    filings: list[FilingHit],
    tracked_dockets: set[str],
    minimum_score: int = 4,
) -> list[ProjectCandidate]:
    tracked_roots = {
        root_docket(docket) or docket.upper()
        for docket in tracked_dockets
    }

    by_docket: dict[str, list[FilingHit]] = {}
    for filing in filings:
        docket = root_docket(filing.docket)
        if not docket:
            continue
        filing.docket = docket
        by_docket.setdefault(docket, []).append(filing)

    candidates: list[ProjectCandidate] = []

    for docket, docket_filings in sorted(by_docket.items()):
        score, reasons = score_candidate(docket, docket_filings)
        if score < minimum_score:
            continue

        project_names = set()
        sample_filings = []

        for filing in docket_filings:
            text = filing.description.strip()
            if text:
                sample_filings.append(text)

        latest = max(
            (
                item.filed_date
                for item in docket_filings
                if item.filed_date
            ),
            default=None,
        )

        candidates.append(
            ProjectCandidate(
                company_id=company_id,
                company_name=company_name,
                docket=docket,
                score=score,
                reasons=reasons,
                filing_count=len(docket_filings),
                latest_filing_date=latest,
                project_names=sorted(project_names),
                sample_filings=sample_filings[:5],
                tracked=docket in tracked_roots,
            )
        )

    candidates.sort(
        key=lambda item: (
            item.tracked,
            item.score,
            item.latest_filing_date or "",
        ),
        reverse=True,
    )
    return candidates


def load_registry(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def tracked_dockets(registry: dict[str, Any], company_id: str) -> set[str]:
    projects = registry.get("projects") or []
    if isinstance(projects, dict):
        projects = list(projects.values())

    result = set()
    for project in projects:
        owner = str(project.get("company_id") or "").upper()
        if owner != company_id.upper():
            continue
        docket = root_docket(
            str(project.get("docket") or project.get("docket_number") or "")
        )
        if docket:
            result.add(docket)
    return result


def write_discovery(
    path: Path,
    company_id: str,
    company_name: str,
    candidates: list[ProjectCandidate],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "company_project_discovery_v2",
        "generated_at": datetime.now().isoformat(),
        "company_id": company_id,
        "company_name": company_name,
        "candidates": [asdict(item) for item in candidates],
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def iso_window(days: int) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=days)
    return start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y")
