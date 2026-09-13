from pathlib import Path

path = Path("scripts/test_real_project_view.py")
if not path.exists():
    raise SystemExit(f"Not found: {path}")

text = path.read_text(encoding="utf-8")

# Ensure imports exist.
if "construction_authorization_integration" not in text:
    anchors = [
        "from ferc_filter.state import MonitorState",
        "from ferc_filter.pending_regulatory_action import",
    ]
    inserted = False
    for anchor in anchors:
        pos = text.find(anchor)
        if pos >= 0:
            line_end = text.find("\n", pos)
            imports = (
                "\nfrom ferc_filter.construction_authorization_integration import (\n"
                "    analyze_construction_authorization_chain,\n"
                "    filter_resolved_construction_actions,\n"
                "    apply_construction_chain_to_lifecycle,\n"
                ")\n"
            )
            text = text[:line_end + 1] + imports + text[line_end + 1:]
            inserted = True
            break
    if not inserted:
        raise SystemExit("Could not find a safe import insertion point.")
    print("PASS | Added construction-chain imports")
else:
    print("PASS | Construction-chain imports already present")

# Locate the exact lifecycle assignment by variable name, not function spelling.
lifecycle_marker = "lifecycle ="
lifecycle_pos = text.find(lifecycle_marker)
if lifecycle_pos < 0:
    raise SystemExit("Could not find a lifecycle assignment.")

# Only patch if the integration block isn't already present.
if "lifecycle = apply_construction_chain_to_lifecycle(" not in text:
    # Insert after the existing lifecycle assignment statement. In the
    # current diagnostic this assignment is a small multiline call.
    end = text.find("\n)", lifecycle_pos)
    if end < 0:
        raise SystemExit("Could not find end of lifecycle assignment.")
    insert_at = end + 2

    block = (
        "\n\n"
        "construction_chain = analyze_construction_authorization_chain(\n"
        "    classified_filings\n"
        ")\n"
        "lifecycle = apply_construction_chain_to_lifecycle(\n"
        "    lifecycle,\n"
        "    construction_chain,\n"
        ")\n"
        "# Keep ProjectView's broad stage synchronized with lifecycle intelligence.\n"
        "view.current_stage = lifecycle.current_stage\n"
    )
    text = text[:insert_at] + block + text[insert_at:]
    print("PASS | Added lifecycle construction-chain integration")
else:
    print("PASS | Lifecycle construction-chain integration already present")

# Locate pending action assignment by variable name.
pending_marker = "pending_actions ="
pending_pos = text.find(pending_marker)
if pending_pos < 0:
    raise SystemExit("Could not find pending_actions assignment.")

if "pending_actions = filter_resolved_construction_actions(" not in text:
    end = text.find("\n)", pending_pos)
    if end < 0:
        raise SystemExit("Could not find end of pending_actions assignment.")
    insert_at = end + 2

    block = (
        "\n\n"
        "pending_actions = filter_resolved_construction_actions(\n"
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
print("Next:")
print("  python scripts/test_real_project_view.py CP21-465")
