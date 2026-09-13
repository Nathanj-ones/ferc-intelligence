from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from ferc_filter.project_display import (
    build_project_display,
)


PROJECTS = {
    "CP25-10": "Southeast Supply Enhancement",
    "CP25-528": "Appalachian Reliability",
    "CP25-219": "Southeast Compression Utility and Reliability",
}


def parse_dt(value):
    if not value:
        return None
    return datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )


def ns(value):
    """Recursively convert dicts to attribute-style objects."""
    if isinstance(value, dict):
        return SimpleNamespace(
            **{key: ns(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return [ns(item) for item in value]
    return value


def load_project_view(docket: str):
    path = Path("data") / f"project_view_{docket}.json"

    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Run test_real_project_view.py "
            f"{docket} first."
        )

    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    lifecycle = ns(payload.get("lifecycle"))
    investor = ns(payload.get("investor_summary"))

    pending_actions = []
    for item in payload.get("pending_regulatory_actions", []):
        pending_actions.append(
            SimpleNamespace(
                blocking_next_stage=item.get(
                    "blocking_next_stage", False
                ),
                action_requested=item.get("action_requested"),
                responsible_party=item.get("responsible_party"),
                requested_timing=parse_dt(
                    item.get("requested_timing")
                ),
                timing_source=item.get("timing_source"),
            )
        )

    milestones = []
    for item in payload.get("milestones", []):
        milestones.append(
            SimpleNamespace(
                accession=item.get("accession", ""),
                date=parse_dt(item.get("date")),
                title=item.get("title", ""),
                event_type=item.get("event_type", "unknown"),
            )
        )

    return SimpleNamespace(
        project=payload.get("project", PROJECTS.get(docket, docket)),
        docket=payload.get("docket", docket),
        lifecycle=lifecycle,
        investor_summary=investor,
        pending_regulatory_actions=pending_actions,
        milestones=milestones,
    )


def label(value):
    if value is None:
        return "-"
    labels = {
        "post_certificate_pre_construction":
            "Post-Certificate / Pre-Construction",
        "construction": "Construction",
        "construction_authorization":
            "Construction Authorization",
        "in_service": "In-Service Authorization",
        "notice_to_proceed": "Notice to Proceed",
        "progressing": "Progressing",
        "sponsor_requested": "Sponsor requested",
        "construction_underway_or_authorized":
            "Construction underway / authorized",
        "pending_gate_with_requested_timing":
            "Pending gate / requested timing",
    }
    return labels.get(
        value,
        str(value).replace("_", " ").title(),
    )


def date_text(value):
    return value.strftime("%d %b %Y") if value else "-"


def compact(display):
    latest = display.latest_material_event
    return {
        "Project": display.project,
        "Docket": display.docket,
        "Stage": label(display.stage),
        "Health": label(display.regulatory_health),
        "Next gate": label(display.next_gate),
        "Gate status": label(display.gate_status),
        "Blocking action": label(display.blocking_action),
        "Waiting on": display.responsible_party or "-",
        "Relevant timing": date_text(display.requested_timing),
        "Timing type": label(display.timing_type),
        "Timeline signal": label(display.timeline_signal),
        "Latest milestone": (
            f"{latest.title} ({date_text(latest.date)})"
            if latest else "-"
        ),
    }


def print_comparison(left, right):
    a = compact(left)
    b = compact(right)

    print()
    print("=" * 118)
    print("PROJECT COMPARISON")
    print("=" * 118)
    print()
    print(
        f"{'FIELD':<22}"
        f"{a['Project'][:43]:<46}"
        f"{b['Project'][:43]:<46}"
    )
    print("-" * 118)

    for field in (
        "Docket",
        "Stage",
        "Health",
        "Next gate",
        "Gate status",
        "Blocking action",
        "Waiting on",
        "Relevant timing",
        "Timing type",
        "Timeline signal",
        "Latest milestone",
    ):
        print(
            f"{field:<22}"
            f"{str(a[field])[:43]:<46}"
            f"{str(b[field])[:43]:<46}"
        )


def main():
    dockets = sys.argv[1:]

    if not dockets:
        dockets = ["CP25-10", "CP25-528", "CP25-219"]

    displays = {}

    print("=" * 80)
    print("REAL PROJECT DISPLAY TEST")
    print("=" * 80)

    for raw_docket in dockets:
        docket = raw_docket.strip().upper().replace(" ", "-")
        view = load_project_view(docket)
        display = build_project_display(view)
        displays[docket] = display

        print()
        print(f"PASS | {docket} | {display.headline}")

    # Default comparison requested for the first product-design test.
    if "CP25-10" in displays and "CP25-528" in displays:
        print_comparison(
            displays["CP25-10"],
            displays["CP25-528"],
        )
    elif len(displays) >= 2:
        values = list(displays.values())
        print_comparison(values[0], values[1])

    print()
    print("ALL REAL PROJECT DISPLAY TESTS PASSED")


if __name__ == "__main__":
    main()
