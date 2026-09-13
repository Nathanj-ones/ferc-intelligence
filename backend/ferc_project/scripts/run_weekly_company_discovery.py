from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from ferc_filter.company_project_discovery import (
    FERCDiscoveryClient,
    ProjectCandidate,
    iso_window,
    root_docket,
)
from ferc_filter.project_candidate_qualifier import (
    qualify_candidate,
)
from ferc_filter.project_materiality import (
    TRACK,
    WATCH,
    DROP,
    assess_materiality,
)
from ferc_filter.project_evidence_extractor import (
    extract_project_evidence,
    project_evidence_to_dict,
)
from ferc_filter.ferc_docket_discovery import match_company
from ferc_filter.company_ferc_identity_learner import (
    learn_entities,
    merge_learned_entities,
)
from ferc_filter.company_multi_entity_search import (
    search_company_identities,
)
from ferc_filter.discovery_registry_pipeline import (
    apply_track_records,
)
from ferc_filter.reverse_cp_discovery import (
    load_recent_cp_records,
    merge_filings,
    reverse_filings_for_company,
)


def load_registry(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_registry(path: Path, registry: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def company_by_id(registry: dict[str, Any], company_id: str) -> dict[str, Any]:
    for company in registry.get("companies", []):
        current = str(
            company.get("id")
            or company.get("company_id")
            or company.get("ticker")
            or ""
        ).upper()
        if current == company_id.upper():
            return company
    raise SystemExit(f"Unknown company: {company_id}")


def tracked_roots(
    registry: dict[str, Any],
    company_id: str,
) -> set[str]:
    # Exact project_registry.json schema:
    # company_id + docket
    projects = registry.get("projects") or []
    if isinstance(projects, dict):
        projects = list(projects.values())

    wanted = company_id.upper()
    result = set()

    for project in projects:
        owner = str(project.get("company_id") or "").strip().upper()
        if owner != wanted:
            continue

        docket = root_docket(project.get("docket"))
        if docket:
            result.add(docket)

    return result



def filter_filings_to_company(
    filings,
    registry: dict[str, Any],
    company_id: str,
):
    """Prevent company-search leakage across monitored companies."""
    companies = registry.get('companies') or []
    identity_path = Path('config/company_ferc_identities.json')
    identities = json.loads(identity_path.read_text(encoding='utf-8'))
    identity_map = {
        cid.upper(): data.get('ferc_entities') or []
        for cid, data in (identities.get('companies') or {}).items()
    }

    eligible_roots = set()

    for filing in filings:
        applicant = str(getattr(filing, 'applicant', '') or '').strip()
        if not applicant:
            continue

        matched_id, _, _ = match_company(
            applicant,
            companies,
            identity_map,
        )
        if matched_id and matched_id.upper() == company_id.upper():
            root = root_docket(filing.docket)
            if root:
                eligible_roots.add(root)

    return [
        filing
        for filing in filings
        if root_docket(filing.docket) in eligible_roots
    ]

def candidate_groups(
    filings,
) -> dict[str, list]:
    groups = defaultdict(list)
    for filing in filings:
        root = root_docket(filing.docket)
        if not root:
            continue
        filing.docket = root
        groups[root].append(filing)
    return dict(groups)


def candidate_from_group(
    company_id: str,
    company_name: str,
    docket: str,
    filings,
) -> ProjectCandidate:
    # Build one candidate from the already-fetched company filing set.
    project_names = set()
    samples = []

    # Reuse the existing evidence extractor's project-name parsing so the
    # qualifier sees the same named-project evidence used later downstream.
    from ferc_filter.project_evidence_extractor import _project_name

    for filing in filings:
        description = filing.description.strip()
        if description:
            samples.append(description)
            project_name = _project_name(description)
            if project_name:
                project_names.add(project_name)

    from ferc_filter.company_project_discovery import score_candidate

    score, reasons = score_candidate(docket, filings)
    latest = max(
        (
            filing.filed_date
            for filing in filings
            if filing.filed_date
        ),
        default=None,
    )

    return ProjectCandidate(
        company_id=company_id,
        company_name=company_name,
        docket=docket,
        score=score,
        reasons=reasons,
        filing_count=len(filings),
        latest_filing_date=latest,
        project_names=sorted(project_names),
        sample_filings=samples[:5],
        tracked=False,
    )


def run_company(
    registry_path: Path,
    company_id: str,
    *,
    days: int,
    apply: bool,
    reverse_feed: Path | None = None,
) -> dict[str, Any]:
    registry = load_registry(registry_path)
    company = company_by_id(registry, company_id)
    company_name = str(company.get("name") or "").strip()

    start_date, end_date = iso_window(days)

    client = FERCDiscoveryClient()

    # Bootstrap from the parent-company search.
    parent_filings = client.search_company(
        company_name=company_name,
        start_date=start_date,
        end_date=end_date,
    )

    # Learn recurring FERC-facing legal entities from those filings.
    learned = learn_entities(
        parent_filings,
        minimum_filings=2,
    )
    identity_path = Path("config/company_ferc_identities.json")
    learned_added = merge_learned_entities(
        company_id,
        learned,
        path=identity_path,
    )

    # Search parent + all persisted identities and deduplicate the result.
    filings, searched_aliases = search_company_identities(
        client,
        company_id,
        company_name,
        start_date,
        end_date,
    )

    filings = filter_filings_to_company(
        filings,
        registry,
        company_id,
    )

    forward_filings = list(filings)
    reverse_stats = {
        "enabled": False,
        "feed_records": 0,
        "discovery_role_records": 0,
        "matched_seed_records": 0,
        "ownership_registry_matches": 0,
        "unmatched_seed_records": 0,
        "owned_dockets": [],
        "selected_records": 0,
    }

    if reverse_feed is not None and reverse_feed.exists():
        identity_payload = json.loads(identity_path.read_text(encoding="utf-8"))
        identity_map = {
            cid.upper(): data.get("ferc_entities") or []
            for cid, data in (identity_payload.get("companies") or {}).items()
        }
        reverse_records = load_recent_cp_records(reverse_feed)
        reverse_filings, reverse_stats = reverse_filings_for_company(
            reverse_records,
            registry.get("companies") or [],
            identity_map,
            company_id,
        )
        reverse_stats["enabled"] = True
        filings = merge_filings(forward_filings, reverse_filings)

    groups = candidate_groups(filings)
    tracked = tracked_roots(registry, company_id)

    candidates = []
    evaluated = []

    for docket, docket_filings in sorted(groups.items()):
        candidate = candidate_from_group(
            company_id,
            company_name,
            docket,
            docket_filings,
        )

        if docket in tracked:
            candidate.tracked = True
            candidates.append(candidate)
            evaluated.append(
                {
                    "docket": docket,
                    "status": "already_tracked",
                }
            )
            continue

        qualification = qualify_candidate(candidate)

        if qualification.decision == "IGNORE":
            evaluated.append(
                {
                    "docket": docket,
                    "status": "qualification_ignore",
                    "qualification": qualification.__dict__,
                }
            )
            continue

        evidence = extract_project_evidence(
            docket,
            docket_filings,
        )

        materiality = assess_materiality(
            candidate,
            qualification,
            capex=evidence.capex,
            capacity=evidence.capacity,
            target_in_service=evidence.target_in_service,
        )

        evaluated.append(
            {
                "docket": docket,
                "status": "evaluated",
                "qualification": qualification.__dict__,
                "evidence": project_evidence_to_dict(evidence),
                "materiality": materiality.__dict__,
            }
        )

        if materiality.decision == TRACK:
            record = {
                "company_id": company_id,
                "name": evidence.project_name
                or f"FERC Project {docket}",
                "docket": docket,
                "capex": evidence.capex,
                "capacity": evidence.capacity,
                "target_in_service": evidence.target_in_service,
                "discovery_source": "ferc_elibrary",
                "discovered_at": evidence.__dict__.get(
                    "generated_at"
                ),
                "auto_discovered": True,
            }
            evaluated[-1]["registry_record"] = record

    track_records = [
        {
            "docket": item["docket"],
            "registry_record": item["registry_record"],
        }
        for item in evaluated
        if item.get("materiality", {}).get("decision") == TRACK
    ]

    watch_records = [
        item for item in evaluated
        if item.get("materiality", {}).get("decision") == WATCH
    ]

    drop_records = [
        item for item in evaluated
        if item.get("status") == "evaluated"
        and item.get("materiality", {}).get("decision") == DROP
    ]

    result = {
        "company_id": company_id,
        "company_name": company_name,
        "window": {
            "start_date": start_date,
            "end_date": end_date,
        },
        "filings_scanned": len(filings),
        "forward_filings_scanned": len(forward_filings),
        "reverse_cp": reverse_stats,
        "dockets_seen": len(groups),
        "ferc_identities_searched": searched_aliases,
        "ferc_identities_learned": learned_added,
        "existing_tracked": len([
            item for item in evaluated
            if item["status"] == "already_tracked"
        ]),
        "track": track_records,
        "watch": watch_records,
        "drop": drop_records,
        "evaluated": evaluated,
    }

    output = (
        Path("data/discovery")
        / company_id.upper()
        / "weekly_pipeline.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if apply and track_records:
        wrapper = {
            "company_id": company_id,
            "track": track_records,
        }
        added = apply_track_records(
            registry,
            wrapper,
        )
        save_registry(registry_path, registry)
        result["registry_added"] = added
    else:
        result["registry_added"] = 0

    return result


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Run one weekly discovery pass: one FERC company search, "
            "docket grouping, qualification, evidence extraction, "
            "materiality, and optional registry promotion."
        )
    )
    parser.add_argument("--company", required=True)
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Discovery window in days for weekly runs.",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("config/project_registry.json"),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Promote TRACK results into the registry.",
    )
    parser.add_argument(
        "--reverse-feed",
        type=Path,
        help=(
            "Optional recent CP feed JSON. Safely matched reverse-discovery "
            "records are merged with normal company-search filings."
        ),
    )
    args = parser.parse_args()

    result = run_company(
        args.registry,
        args.company.upper(),
        days=args.days,
        apply=args.apply,
        reverse_feed=args.reverse_feed,
    )

    print("=" * 100)
    print("WEEKLY COMPANY DISCOVERY LOOP v0.1")
    print("=" * 100)
    print()
    print(
        f"{result['company_name']} ({result['company_id']})"
    )
    print(
        f"Window: {result['window']['start_date']} -> "
        f"{result['window']['end_date']}"
    )
    print(
        "FERC identities searched:",
        len(result["ferc_identities_searched"]),
    )
    for identity in result["ferc_identities_searched"]:
        print("  -", identity)
    if result["ferc_identities_learned"]:
        print(
            "New identities learned:",
            len(result["ferc_identities_learned"]),
        )
        for identity in result["ferc_identities_learned"]:
            print("  +", identity)
    print(f"Filings scanned: {result['filings_scanned']}")
    if result["reverse_cp"].get("enabled"):
        reverse = result["reverse_cp"]
        print(
            "Reverse CP: "
            f"{reverse['selected_records']} filing(s) from "
            f"{len(reverse['owned_dockets'])} owned docket(s)"
        )
        if reverse.get("ownership_registry_matches"):
            print(
                "  Evidence-backed ownership matches: "
                f"{reverse['ownership_registry_matches']}"
            )
    print(f"Dockets seen: {result['dockets_seen']}")
    registry = load_registry(args.registry)
    total_tracked = len(tracked_roots(registry, args.company.upper()))
    print(f"Tracked projects in registry: {total_tracked}")
    print("Tracked dockets seen in discovery window:", result["existing_tracked"])
    print(f"TRACK: {len(result['track'])}")
    print(f"WATCH: {len(result['watch'])}")
    print(f"DROP: {len(result['drop'])}")
    print(f"Registry added: {result['registry_added']}")
    print()
    print(
        "DRY RUN" if not args.apply else "REGISTRY UPDATED"
    )
    print(
        f"Saved: data\\discovery\\{result['company_id']}\\weekly_pipeline.json"
    )


if __name__ == "__main__":
    main()
