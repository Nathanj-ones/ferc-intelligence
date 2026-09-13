from __future__ import annotations

import argparse
import json
from pathlib import Path

from ferc_filter.company_project_discovery import (
    ProjectCandidate,
)
from ferc_filter.discovery_registry_pipeline import (
    apply_track_records,
    process_candidates,
)


def main():
    parser = argparse.ArgumentParser(
        description="Run qualification/materiality and optionally update registry."
    )
    parser.add_argument("--company", required=True)
    parser.add_argument("--discovery", type=Path, required=True)
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("config/project_registry.json"),
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        help="Optional JSON keyed by root docket with capex/capacity/timing.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually add TRACK projects to the registry.",
    )
    args = parser.parse_args()

    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    payload = json.loads(args.discovery.read_text(encoding="utf-8"))
    candidates = [
        ProjectCandidate(**item)
        for item in payload.get("candidates", [])
    ]

    evidence = {}
    if args.evidence and args.evidence.exists():
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))

    result = process_candidates(
        registry,
        args.company.upper(),
        candidates,
        evidence=evidence,
    )

    print("=" * 100)
    print("DISCOVERY -> QUALIFICATION -> MATERIALITY -> REGISTRY v0.1")
    print("=" * 100)
    print()
    print("Existing:", len(result["existing"]))
    print("TRACK:", len(result["track"]))
    print("WATCH:", len(result["watch"]))
    print("DROP:", len(result["drop"]))
    print()

    for item in result["track"]:
        m = item["materiality"]
        print(
            f"TRACK | {item['docket']} | "
            f"score={m['score']} | {m['confidence']}"
        )
        print("  Registry name:", item["registry_record"]["name"])
        print("  Capex:", m["capex"])
        print("  Capacity:", m["capacity"])

    print()
    for item in result["watch"][:20]:
        m = item["materiality"]
        print(
            f"WATCH | {item['docket']} | "
            f"score={m['score']} | {m['confidence']}"
        )

    output = Path("data/discovery") / (
        f"{args.company.upper()}_pipeline.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print()
    print("Saved:", output)

    if args.apply:
        added = apply_track_records(registry, result)
        args.registry.write_text(
            json.dumps(registry, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Registry updated: {added} project(s) added")
    else:
        print("DRY RUN: registry was not modified.")
        print("Use --apply only after reviewing TRACK results.")


if __name__ == "__main__":
    main()
