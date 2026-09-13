from __future__ import annotations

import argparse
import json
from pathlib import Path

from ferc_filter.company_project_discovery import ProjectCandidate
from ferc_filter.project_candidate_qualifier import (
    IGNORE,
    PROMOTE,
    WATCH,
    qualification_to_dict,
    qualify_candidates,
)


def main():
    parser = argparse.ArgumentParser(
        description="Qualify discovered FERC project candidates by materiality."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    candidates = [
        ProjectCandidate(**item)
        for item in payload.get("candidates", [])
    ]
    results = qualify_candidates(candidates)

    print("=" * 100)
    print("FERC PROJECT CANDIDATE QUALIFICATION v0.2")
    print("=" * 100)
    print()

    counts = {PROMOTE: 0, WATCH: 0, IGNORE: 0}

    for result in results:
        counts[result.decision] += 1
        print(
            f"{result.decision:7} | "
            f"{result.docket:<14} | "
            f"score={result.score:<2} | "
            f"{result.confidence}"
        )
        for reason in result.reasons[:8]:
            print("  ", reason)

    print()
    print("PROMOTE:", counts[PROMOTE])
    print("WATCH:", counts[WATCH])
    print("IGNORE:", counts[IGNORE])

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                [qualification_to_dict(item) for item in results],
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"Saved: {args.out}")


if __name__ == "__main__":
    main()
