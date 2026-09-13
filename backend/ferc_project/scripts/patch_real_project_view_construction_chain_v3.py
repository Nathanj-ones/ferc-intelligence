from pathlib import Path
import re

path = Path("scripts/test_real_project_view.py")
if not path.exists():
    raise SystemExit(f"Not found: {path}")

text = path.read_text(encoding="utf-8")

# Add the integration import if needed.
if "from ferc_filter.construction_authorization_integration import" not in text:
    anchor = "from ferc_filter.state import MonitorState"
    match = re.search(re.escape(anchor), text)
    if not match:
        raise SystemExit("Could not find MonitorState import.")
    line_end = text.find("\n", match.end())
    imports = """
from ferc_filter.construction_authorization_integration import (
    analyze_construction_authorization_chain,
    filter_resolved_construction_actions,
    apply_construction_chain_to_lifecycle,
)
"""
    text = text[:line_end + 1] + imports + text[line_end + 1:]
    print("PASS | Added construction-chain imports")
else:
    print("PASS | Construction-chain imports already present")

# Find the lifecycle assignment with flexible whitespace/line breaks.
lifecycle_pattern = re.compile(
    r"lifecycle\s*=\s*build_project_lifecycle\s*\("
    r".*?"
    r"\n\s*\)",
    re.DOTALL,
)

if "construction_chain = analyze_construction_authorization_chain(" not in text:
    match = lifecycle_pattern.search(text)
    if not match:
        # Helpful diagnostic: show nearby lifecycle references.
        refs = [m.start() for m in re.finditer("build_project_lifecycle", text)]
        print("DEBUG | build_project_lifecycle references:", len(refs))
        for pos in refs[:5]:
            print(repr(text[max(0, pos - 120):pos + 240]))
        raise SystemExit("Could not locate lifecycle construction call.")

    call = match.group(0)
    insert = (
        call
        + "\n\nconstruction_chain = analyze_construction_authorization_chain(\n"
        + "    classified_filings\n"
        + ")\n"
        + "lifecycle = apply_construction_chain_to_lifecycle(\n"
        + "    lifecycle,\n"
        + "    construction_chain,\n"
        + ")\n"
    )
    text = text[:match.start()] + insert + text[match.end():]
    print("PASS | Added lifecycle construction-chain integration")
else:
    print("PASS | Lifecycle construction-chain integration already present")

# Find pending action assignment with flexible whitespace/line breaks.
pending_pattern = re.compile(
    r"pending_actions\s*=\s*find_pending_regulatory_actions\s*\("
    r".*?"
    r"\n\s*\)",
    re.DOTALL,
)

if "pending_actions = filter_resolved_construction_actions(" not in text:
    match = pending_pattern.search(text)
    if not match:
        refs = [m.start() for m in re.finditer("find_pending_regulatory_actions", text)]
        print("DEBUG | find_pending_regulatory_actions references:", len(refs))
        for pos in refs[:5]:
            print(repr(text[max(0, pos - 120):pos + 280]))
        raise SystemExit("Could not locate pending-action construction call.")

    call = match.group(0)
    insert = (
        call
        + "\n\npending_actions = filter_resolved_construction_actions(\n"
        + "    pending_actions,\n"
        + "    construction_chain,\n"
        + ")\n"
    )
    text = text[:match.start()] + insert + text[match.end():]
    print("PASS | Added resolved pending-action filter")
else:
    print("PASS | Pending-action filter already present")

compile(text, str(path), "exec")
path.write_text(text, encoding="utf-8")

print()
print(f"PASS | Updated and syntax-checked {path}")
print("Next:")
print("  python scripts/test_real_project_view.py CP21-465")
