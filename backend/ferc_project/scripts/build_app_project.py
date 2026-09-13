import json
import sys
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from ferc_filter.app_project import build_app_project, app_project_to_dict, validate_app_project
from ferc_filter.project_context import ProjectContext
from ferc_filter.project_display import build_project_display
from ferc_filter.project_timeline import build_project_timeline
from ferc_filter.project_evidence import evidence_from_display

def dt(v):
    return datetime.fromisoformat(v.replace("Z","+00:00")) if v else None

def ns(v):
    if isinstance(v, dict):
        return SimpleNamespace(**{k: ns(x) for k, x in v.items()})
    if isinstance(v, list):
        return [ns(x) for x in v]
    return v

docket = sys.argv[1].upper().replace(" ","-") if len(sys.argv) > 1 else None
if docket is None:
    raise SystemExit("Usage: python scripts\\build_app_project.py <DOCKET>")

path = Path("data") / f"project_view_{docket}.json"
if not path.exists():
    raise SystemExit(f"Missing {path}. Run the real ProjectView test first.")

payload = json.loads(path.read_text(encoding="utf-8"))
pending = [
    SimpleNamespace(
        blocking_next_stage=x.get("blocking_next_stage", False),
        action_requested=x.get("action_requested"),
        responsible_party=x.get("responsible_party"),
        requested_timing=dt(x.get("requested_timing")),
        timing_source=x.get("timing_source"),
    )
    for x in payload.get("pending_regulatory_actions", [])
]
milestones = [
    SimpleNamespace(
        accession=x.get("accession",""),
        date=dt(x.get("date")),
        title=x.get("title",""),
        event_type=x.get("event_type","unknown"),
    )
    for x in payload.get("milestones", [])
]
view = SimpleNamespace(
    project=payload["project"],
    docket=payload["docket"],
    lifecycle=ns(payload["lifecycle"]),
    investor_summary=ns(payload["investor_summary"]),
    pending_regulatory_actions=pending,
    milestones=milestones,
)

contexts = {
    "CP25-10": ProjectContext(
        project="Southeast Supply Enhancement", docket="CP25-10", company="Williams",
        project_type="Natural Gas Pipeline Expansion", capacity_value=1.597,
        capacity_unit="Bcf/d", capex_value=1.5, capex_currency="USD",
        capex_unit="billion", target_in_service="Q3 2027",
        capacity_source="company_materials", capex_source="company_materials",
        target_in_service_source="company_materials",
    ),
    "CP25-528": ProjectContext(project="Appalachian Reliability", docket="CP25-528"),
    "CP25-219": ProjectContext(project="Southeast Compression Utility and Reliability", docket="CP25-219"),
}
context = contexts.get(docket)
if context is None:
    raise SystemExit(f"No ProjectContext configured for {docket}")

display = build_project_display(view)
project_timeline = build_project_timeline(
    context,
    display,
)
project_evidence = evidence_from_display(
    display,
)
app_project = build_app_project(
    context,
    display,
    project_timeline,
    project_evidence,
)
errors = validate_app_project(app_project)
if errors:
    raise SystemExit("Validation failed: " + "; ".join(errors))

out = Path("data") / "app"
out.mkdir(parents=True, exist_ok=True)
path_out = out / f"project_{docket}.json"
path_out.write_text(json.dumps(app_project_to_dict(app_project), indent=2, ensure_ascii=False), encoding="utf-8")
print(f"PASS | App Project v1 generated | {docket}")
print(f"Saved JSON: {path_out}")
