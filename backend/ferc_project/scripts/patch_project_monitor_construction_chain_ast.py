from __future__ import annotations

import ast
from pathlib import Path


TARGET = Path("src/ferc_filter/project_monitor.py")

if not TARGET.exists():
    raise SystemExit(f"Not found: {TARGET}")

text = TARGET.read_text(encoding="utf-8")


def assignment_call(tree, function_name: str):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        name = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name == function_name:
            targets = [
                t.id for t in node.targets if isinstance(t, ast.Name)
            ]
            if targets:
                return node, targets[0]
    return None, None


tree = ast.parse(text)

lifecycle_node, lifecycle_var = assignment_call(
    tree,
    "build_project_lifecycle",
)
pending_node, pending_var = assignment_call(
    tree,
    "find_pending_regulatory_actions",
)

if lifecycle_node is None:
    raise SystemExit("Could not locate build_project_lifecycle assignment.")
if pending_node is None:
    raise SystemExit("Could not locate find_pending_regulatory_actions assignment.")

print(
    f"PASS | Found lifecycle assignment: "
    f"{lifecycle_var} at line {lifecycle_node.lineno}"
)
print(
    f"PASS | Found pending-action assignment: "
    f"{pending_var} at line {pending_node.lineno}"
)

# Add imports immediately after the existing pending-regulatory import if absent.
if "from .construction_authorization_integration import" not in text:
    anchor = "from .pending_regulatory_action import find_pending_regulatory_actions"
    pos = text.find(anchor)
    if pos < 0:
        raise SystemExit("Could not find pending_regulatory_action import anchor.")

    line_end = text.find("\n", pos)
    imports = (
        "\nfrom .construction_authorization_integration import (\n"
        "    analyze_construction_authorization_chain,\n"
        "    filter_resolved_construction_actions,\n"
        "    apply_construction_chain_to_lifecycle,\n"
        ")\n"
    )
    text = text[:line_end + 1] + imports + text[line_end + 1:]
    print("PASS | Added construction-chain imports")
else:
    print("PASS | Construction-chain imports already present")


# Reparse after import insertion so line offsets remain accurate.
tree = ast.parse(text)
lifecycle_node, lifecycle_var = assignment_call(tree, "build_project_lifecycle")
pending_node, pending_var = assignment_call(tree, "find_pending_regulatory_actions")

# Python AST end_lineno gives us the true end of multiline calls.
lines = text.splitlines(keepends=True)


def absolute_end_offset(node):
    return sum(len(line) for line in lines[: node.end_lineno - 1]) + node.end_col_offset


# Insert lifecycle integration immediately after lifecycle assignment.
if "analyze_construction_authorization_chain(" not in text:
    lifecycle_end = absolute_end_offset(lifecycle_node)

    block = (
        "\n"
        f"        construction_chain = analyze_construction_authorization_chain(\n"
        f"            {lifecycle_var if lifecycle_var in {'classified', 'classified_filings'} else 'classified'}\n"
        "        )\n"
        f"        {lifecycle_var} = apply_construction_chain_to_lifecycle(\n"
        f"            {lifecycle_var},\n"
        "            construction_chain,\n"
        "        )\n"
        "        view.current_stage = "
        f"{lifecycle_var}.current_stage\n"
    )

    # Preserve function indentation by deriving it from the assignment line.
    line_start = sum(len(line) for line in lines[: lifecycle_node.lineno - 1])
    line = lines[lifecycle_node.lineno - 1]
    indent = line[: len(line) - len(line.lstrip())]
    block = block.replace("        ", indent)

    text = text[:lifecycle_end] + block + text[lifecycle_end:]
    print("PASS | Added production lifecycle integration")
else:
    print("PASS | Lifecycle integration already present")


# Reparse because offsets changed.
tree = ast.parse(text)
pending_node, pending_var = assignment_call(
    tree,
    "find_pending_regulatory_actions",
)

if "filter_resolved_construction_actions(" not in text:
    pending_end = absolute_end_offset(pending_node)
    line = text.splitlines(keepends=True)[pending_node.lineno - 1]
    indent = line[: len(line) - len(line.lstrip())]

    block = (
        "\n"
        f"{indent}{pending_var} = filter_resolved_construction_actions(\n"
        f"{indent}    {pending_var},\n"
        f"{indent}    construction_chain,\n"
        f"{indent})\n"
    )

    text = text[:pending_end] + block + text[pending_end:]
    print("PASS | Added resolved pending-action filter")
else:
    print("PASS | Pending-action filter already present")


compile(text, str(TARGET), "exec")
TARGET.write_text(text, encoding="utf-8")

print()
print(f"PASS | Updated and syntax-checked {TARGET}")
print()
print("Next:")
print("  python scripts/run_project_monitor.py --company WMB")
