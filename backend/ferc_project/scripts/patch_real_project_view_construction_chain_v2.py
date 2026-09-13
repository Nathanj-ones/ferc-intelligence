from pathlib import Path

path = Path("scripts/test_real_project_view.py")
if not path.exists():
    raise SystemExit(f"Not found: {path}")

text = path.read_text(encoding="utf-8")

# 1. Add imports.
if "construction_authorization_integration" not in text:
    anchor = "from ferc_filter.state import MonitorState"
    pos = text.find(anchor)
    if pos < 0:
        raise SystemExit("Could not find MonitorState import anchor.")
    line_end = text.find("\n", pos)
    imports = (
        "\nfrom ferc_filter.construction_authorization_integration import (\n"
        "    analyze_construction_authorization_chain,\n"
        "    filter_resolved_construction_actions,\n"
        "    apply_construction_chain_to_lifecycle,\n"
        ")\n"
    )
    text = text[:line_end + 1] + imports + text[line_end + 1:]
    print("PASS | Added construction-chain imports")
else:
    print("PASS | Construction-chain imports already present")

# 2. Insert chain analysis after build_project_lifecycle(...) call.
if "construction_chain = analyze_construction_authorization_chain(" not in text:
    marker = "lifecycle = build_project_lifecycle("
    start = text.find(marker)
    if start < 0:
        raise SystemExit("Could not find build_project_lifecycle call.")

    # Find the closing line of this simple multi-line function call.
    close = text.find("\n)", start)
    if close < 0:
        raise SystemExit("Could not find end of build_project_lifecycle call.")
    insert_at = close + 2

    block = (
        "\n\nconstruction_chain = analyze_construction_authorization_chain(\n"
        "    classified_filings\n"
        ")\n"
        "lifecycle = apply_construction_chain_to_lifecycle(\n"
        "    lifecycle,\n"
        "    construction_chain,\n"
        ")\n"
    )
    text = text[:insert_at] + block + text[insert_at:]
    print("PASS | Added lifecycle construction-chain integration")
else:
    print("PASS | Lifecycle construction-chain integration already present")

# 3. Insert pending-action filter after find_pending_regulatory_actions(...).
if "pending_actions = filter_resolved_construction_actions(" not in text:
    marker = "pending_actions = find_pending_regulatory_actions("
    start = text.find(marker)
    if start < 0:
        raise SystemExit("Could not find find_pending_regulatory_actions call.")

    close = text.find("\n)", start)
    if close < 0:
        raise SystemExit("Could not find end of pending-action call.")
    insert_at = close + 2

    block = (
        "\n\npending_actions = filter_resolved_construction_actions(\n"
        "    pending_actions,\n"
        "    construction_chain,\n"
        ")\n"
    )
    text = text[:insert_at] + block + text[insert_at:]
    print("PASS | Added resolved pending-action filter")
else:
    print("PASS | Pending-action filter already present")

compile(text, str(path), "exec")
path.write_text(text, encoding="utf-8")

print()
print(f"PASS | Updated and syntax-checked {path}")
print("Run:")
print("  python scripts/test_real_project_view.py CP21-465")
