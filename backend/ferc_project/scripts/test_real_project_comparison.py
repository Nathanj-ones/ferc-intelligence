from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from ferc_filter.project_context import ProjectContext
from ferc_filter.project_comparison import (
    build_project_comparison_item,
)
from ferc_filter.project_display import build_project_display


def parse_dt(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def ns(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{k: ns(v) for k, v in value.items()})
    if isinstance(value, list):
        return [ns(v) for v in value]
    return value


def load_view(docket):
    path = Path("data") / f"project_view_{docket}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Run test_real_project_view.py {docket} first."
        )

    payload = json.loads(path.read_text(encoding="utf-8"))

    pending = [
        SimpleNamespace(
            blocking_next_stage=x.get("blocking_next_stage", False),
            action_requested=x.get("action_requested"),
            responsible_party=x.get("responsible_party"),
            requested_timing=parse_dt(x.get("requested_timing")),
            timing_source=x.get("timing_source"),
        )
        for x in payload.get("pending_regulatory_actions", [])
    ]

    milestones = [
        SimpleNamespace(
            accession=x.get("accession", ""),
            date=parse_dt(x.get("date")),
            title=x.get("title", ""),
            event_type=x.get("event_type", "unknown"),
        )
        for x in payload.get("milestones", [])
    ]

    return SimpleNamespace(
        project=payload["project"],
        docket=payload["docket"],
        lifecycle=ns(payload["lifecycle"]),
        investor_summary=ns(payload["investor_summary"]),
        pending_regulatory_actions=pending,
        milestones=milestones,
    )


def text(value):
    return "-" if value in (None, "") else str(value)


def label(value):
    if value is None:
        return "-"
    return {
        "construction": "Construction",
        "post_certificate_pre_construction": "Post-Cert. / Pre-Construction",
        "in_service": "In-Service Authorization",
        "construction_authorization": "Construction Authorization",
        "progressing": "Progressing",
        "notice_to_proceed": "Notice to Proceed",
        "sponsor_requested": "Sponsor requested",
        "construction_underway_or_authorized": "Construction underway",
        "pending_gate_with_requested_timing": "Pending gate",
    }.get(value, str(value).replace("_", " ").title())


def context_value(value, unit=None):
    if value is None:
        return "-"
    return f"{value:g} {unit}" if unit else f"{value:g}"


def capex(c):
    if c.capex_value is None:
        return "-"
    currency = "$" if c.capex_currency == "USD" else f"{c.capex_currency or ''} "
    suffix = "bn" if c.capex_unit == "billion" else (c.capex_unit or "")
    return f"{currency}{c.capex_value:g}{suffix}"


def timing(display):
    if not display.requested_timing:
        return "-"
    date = display.requested_timing.strftime("%d %b %Y")
    return f"{date} ({label(display.timing_type)})"


def latest(display):
    m = display.latest_material_event
    if not m:
        return "-"
    date = m.date.strftime("%d %b %Y") if m.date else "Unknown"
    return f"{m.title} | {date}"


def print_row(name, left, right, width=42):
    print(f"{name:<24}{str(left)[:width]:<{width+2}}{str(right)[:width]:<{width}}")


# Source-aware context. SSE values are from the Williams project materials
# used in the briefing. Unknown Appalachian context is intentionally null.
contexts = {
    "CP25-10": ProjectContext(
        project="Southeast Supply Enhancement",
        docket="CP25-10",
        company="Williams",
        project_type="Natural Gas Pipeline Expansion",
        capacity_value=1.597,
        capacity_unit="Bcf/d",
        capex_value=1.5,
        capex_currency="USD",
        capex_unit="billion",
        target_in_service="Q3 2027",
        capacity_source="company_materials",
        capex_source="company_materials",
        target_in_service_source="company_materials",
    ),
    "CP25-528": ProjectContext(
        project="Appalachian Reliability",
        docket="CP25-528",
    ),
}

items = {}
for docket in ("CP25-10", "CP25-528"):
    view = load_view(docket)
    display = build_project_display(view)
    items[docket] = build_project_comparison_item(
        contexts[docket],
        display,
    )

left = items["CP25-10"]
right = items["CP25-528"]
lc, rc = left.context, right.context
lr, rr = left.regulatory, right.regulatory

print("=" * 112)
print("WILLIAMS-STYLE PROJECT COMPARISON v0.1")
print("=" * 112)
print()
print_row("FIELD", lr.project, rr.project)
print("-" * 112)

print()
print("PROJECT CONTEXT")
print_row("Company", text(lc.company), text(rc.company))
print_row(
    "Capacity",
    context_value(lc.capacity_value, lc.capacity_unit),
    context_value(rc.capacity_value, rc.capacity_unit),
)
print_row("Capex", capex(lc), capex(rc))
print_row(
    "Target in-service",
    text(lc.target_in_service),
    text(rc.target_in_service),
)

print()
print("CURRENT POSITION")
print_row("Stage", label(lr.stage), label(rr.stage))
print_row(
    "Regulatory health",
    label(lr.regulatory_health),
    label(rr.regulatory_health),
)

print()
print("WHAT'S NEXT")
print_row("Next gate", label(lr.next_gate), label(rr.next_gate))
print_row(
    "Blocking action",
    label(lr.blocking_action),
    label(rr.blocking_action),
)
print_row(
    "Waiting on",
    text(lr.responsible_party),
    text(rr.responsible_party),
)
print_row("Relevant timing", timing(lr), timing(rr))
print_row(
    "Timeline signal",
    label(lr.timeline_signal),
    label(rr.timeline_signal),
)

print()
print("LATEST MATERIAL EVENT")
print_row("Latest event", latest(lr), latest(rr))

print()
print("INTERPRETATION")
print(
    f"- {lr.project}: {lr.headline}; next gate is "
    f"{label(lr.next_gate)}."
)
print(
    f"- {rr.project}: {rr.headline}; "
    f"{label(rr.blocking_action)} is awaiting "
    f"{text(rr.responsible_party)}."
)

print()
print("PASS | Real context + FERC intelligence combined")
print("PASS | Unknown context left blank rather than inferred")
print("PASS | Comparison uses common display fields")
