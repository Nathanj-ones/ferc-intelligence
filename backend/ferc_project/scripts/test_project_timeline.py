from datetime import datetime
from types import SimpleNamespace
from ferc_filter.project_timeline import build_project_timeline, project_timeline_to_dict

def m(a,d,t,e):
    return SimpleNamespace(accession=a,date=datetime.fromisoformat(d),title=t,event_type=e)

ctx = SimpleNamespace(target_in_service="Q3 2027", target_in_service_source="company_materials")
ms = [
    m("20260225-3042","2026-02-25","Construction Authorization","construction_or_service_authorization"),
    m("20260129-3076","2026-01-29","Certificate","regulatory_decision"),
    m("20251031-3000","2025-10-31","Environmental","environmental_milestone"),
]
d = SimpleNamespace(stage="construction", key_milestones=ms, requested_timing=None)
p = project_timeline_to_dict(build_project_timeline(ctx,d))
assert [x["status"] for x in p["steps"]] == ["completed","completed","completed","current","future"]
assert p["projected_endpoint"]["value"] == "Q3 2027"
print("PASS | SSE big-picture timeline")

ctx2 = SimpleNamespace(target_in_service=None, target_in_service_source=None)
d2 = SimpleNamespace(
    stage="post_certificate_pre_construction",
    key_milestones=ms[1:],
    requested_timing=datetime(2026,9,11),
)
p2 = project_timeline_to_dict(build_project_timeline(ctx2,d2))
assert p2["current_step"] == "construction_authorization"
assert p2["steps"][-2]["status"] == "current"
assert p2["steps"][-2]["date"] == "2026-09-11"
assert p2["steps"][-1]["status"] == "future"
assert p2["steps"][-1]["date"] is None
print("PASS | Pending construction gate timeline")
print()
print("ALL PROJECT TIMELINE v0.1 TESTS PASSED")
