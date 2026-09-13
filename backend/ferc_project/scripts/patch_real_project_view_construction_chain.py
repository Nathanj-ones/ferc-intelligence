from pathlib import Path

path = Path("scripts/test_real_project_view.py")
if not path.exists():
    raise SystemExit(f"Not found: {path}")

text = path.read_text(encoding="utf-8")

import_anchor = "from ferc_filter.state import MonitorState\n"
imports = (
    "from ferc_filter.construction_authorization_integration import (\n"
    "    analyze_construction_authorization_chain,\n"
    "    filter_resolved_construction_actions,\n"
    "    apply_construction_chain_to_lifecycle,\n"
    ")\n"
)
if "construction_authorization_integration" not in text:
    if import_anchor not in text:
        raise SystemExit("Could not find import anchor.")
    text = text.replace(import_anchor, import_anchor + imports, 1)

start = text.find("lifecycle = build_project_lifecycle(")
pending = text.find("pending_actions = find_pending_regulatory_actions(", start)
if start < 0 or pending < 0:
    raise SystemExit("Could not find lifecycle/pending block.")

# Keep the existing lifecycle construction call; insert the chain immediately
# after it and filter pending actions after they are computed.
lifecycle_end = text.find(")", text.find("build_project_lifecycle(", start))
lifecycle_end = text.find("\n", lifecycle_end) + 1

chain_block = (
    "\n"
    "construction_chain = analyze_construction_authorization_chain(\n"
    "    classified_filings\n"
    ")\n"
    "lifecycle = apply_construction_chain_to_lifecycle(\n"
    "    lifecycle,\n"
    "    construction_chain,\n"
    ")\n"
)

if "construction_chain = analyze_construction_authorization_chain(" not in text:
    text = text[:lifecycle_end] + chain_block + text[lifecycle_end:]

# Re-find pending section after insertion.
pending = text.find("pending_actions = find_pending_regulatory_actions(")
pending_end = text.find("\n", text.find(")", pending)) + 1
filter_block = (
    "\n"
    "pending_actions = filter_resolved_construction_actions(\n"
    "    pending_actions,\n"
    "    construction_chain,\n"
    ")\n"
)
if "pending_actions = filter_resolved_construction_actions(" not in text:
    text = text[:pending_end] + filter_block + text[pending_end:]

path.write_text(text, encoding="utf-8")
compile(text, str(path), "exec")
print(f"PASS | Patched {path}")
