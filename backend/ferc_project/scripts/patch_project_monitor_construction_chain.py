from pathlib import Path

path = Path("src/ferc_filter/project_monitor.py")
if not path.exists():
    raise SystemExit(f"Not found: {path}")

text = path.read_text(encoding="utf-8")

# Import the validated integration helpers.
if "construction_authorization_integration" not in text:
    anchor = "from .pending_regulatory_action import find_pending_regulatory_actions"
    pos = text.find(anchor)
    if pos < 0:
        raise SystemExit("Could not find pending_regulatory_action import.")
    line_end = text.find("\n", pos)
    imports = (
        "\nfrom .construction_authorization_integration import (\n"
        "    analyze_construction_authorization_chain,\n"
        "    filter_resolved_construction_actions,\n"
        "    apply_construction_chain_to_lifecycle,\n"
        ")\n"
    )
    text = text[:line_end + 1] + imports + text[line_end + 1:]
    print("PASS | Added production construction-chain imports")
else:
    print("PASS | Production construction-chain imports already present")

# The diagnostic found these actual production anchors:
# lifecycle = build_project_lifecycle(...) around line 435
# pending = find_pending_regulatory_actions(...) around line 438

if "construction_chain = analyze_construction_authorization_chain(" not in text:
    marker = "        lifecycle = build_project_lifecycle("
    start = text.find(marker)
    if start < 0:
        raise SystemExit("Could not find production lifecycle assignment.")

    end = text.find("\n        )", start)
    if end < 0:
        raise SystemExit("Could not find end of production lifecycle call.")
    insert_at = end + len("\n        )")

    block = (
        "\n\n"
        "        construction_chain = analyze_construction_authorization_chain(\n"
        "            classified\n"
        "        )\n"
        "        lifecycle = apply_construction_chain_to_lifecycle(\n"
        "            lifecycle,\n"
        "            construction_chain,\n"
        "        )"
    )
    text = text[:insert_at] + block + text[insert_at:]
    print("PASS | Added production lifecycle integration")
else:
    print("PASS | Production lifecycle integration already present")

if "pending = filter_resolved_construction_actions(" not in text:
    marker = "        pending = find_pending_regulatory_actions("
    start = text.find(marker)
    if start < 0:
        raise SystemExit("Could not find production pending assignment.")

    end = text.find("\n        )", start)
    if end < 0:
        raise SystemExit("Could not find end of production pending call.")
    insert_at = end + len("\n        )")

    block = (
        "\n\n"
        "        pending = filter_resolved_construction_actions(\n"
        "            pending,\n"
        "            construction_chain,\n"
        "        )"
    )
    text = text[:insert_at] + block + text[insert_at:]
    print("PASS | Added production resolved-action filter")
else:
    print("PASS | Production resolved-action filter already present")

# Keep ProjectView's broad stage synchronized with the authoritative lifecycle.
sync = "        view.lifecycle = lifecycle"
if "        view.current_stage = " not in text:
    pos = text.find(sync)
    if pos < 0:
        raise SystemExit("Could not find view.lifecycle assignment.")
    text = (
        text[:pos]
        + "        view.current_stage = lifecycle.current_stage\n"
        + text[pos:]
    )
    print("PASS | Synchronized ProjectView current stage")
else:
    print("PASS | ProjectView stage synchronization already present")

compile(text, str(path), "exec")
path.write_text(text, encoding="utf-8")

print()
print(f"PASS | Updated and syntax-checked {path}")
print()
print("Next run:")
print("  python scripts/run_project_monitor.py --company WMB")
