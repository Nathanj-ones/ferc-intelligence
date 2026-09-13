from __future__ import annotations

import argparse
import json
from pathlib import Path

from ferc_filter.company_ferc_identity import load_identity_registry
from ferc_filter.ferc_docket_discovery import match_company


def main():
    parser = argparse.ArgumentParser(
        description="Inspect company/entity matching without project hardcoding."
    )
    parser.add_argument("--applicant", required=True)
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("config/project_registry.json"),
    )
    args = parser.parse_args()

    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    identities = load_identity_registry()

    identity_map = {
        company_id: data.get("ferc_entities") or []
        for company_id, data in identities.get("companies", {}).items()
    }

    company_id, reason, confidence = match_company(
        args.applicant,
        registry.get("companies", []),
        identity_map,
    )

    print("Applicant:", args.applicant)
    print("Matched company:", company_id)
    print("Reason:", reason)
    print("Confidence:", confidence)


if __name__ == "__main__":
    main()
